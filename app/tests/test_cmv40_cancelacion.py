"""
Cancelación cooperativa: parar entre dos comandos, no solo matar uno.

`cmv40_cancel` describía una escalada que empieza así: "1. Setea cancel_flag
(los puntos de chequeo en código pueden hacer raise antes de necesitar tocar
el proceso)". Esos puntos de chequeo nunca existieron —
`_check_cmv40_cancel` no lo llamaba nadie y el modelo ya documentaba un
estado `'cancelled'` que jamás se escribía—, así que cancelar solo mataba el
subproceso en curso por SIGTERM: la fase seguía adelante con el comando
siguiente.

Ahora el pipeline consulta el predicado antes de cada subproceso, que es el
punto natural: entre uno y otro es donde una fase puede parar sin dejar un
artefacto a medias.
"""
import asyncio
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import phases.cmv40_pipeline as pipeline  # noqa: E402


class TestPredicadoDeCancelacion(unittest.IsolatedAsyncioTestCase):

    def tearDown(self):
        pipeline.set_cancel_check(None)

    def test_sin_predicado_no_aborta(self):
        # Los tests de fases y los scripts que usan el pipeline directamente
        # no instalan nada: no debe cambiarles el comportamiento.
        pipeline.set_cancel_check(None)
        pipeline.raise_if_cancelled()      # no lanza

    def test_con_predicado_falso_no_aborta(self):
        pipeline.set_cancel_check(lambda: False)
        pipeline.raise_if_cancelled()

    def test_con_predicado_cierto_aborta(self):
        pipeline.set_cancel_check(lambda: True)
        with self.assertRaises(pipeline.CMv40Cancelled):
            pipeline.raise_if_cancelled()

    def test_la_cancelacion_no_es_un_error_del_pipeline(self):
        # Se distingue por tipo para que el orquestador la registre como
        # cancelada y no manche `error_message` (que bloquearía el reintento
        # con el guard de 409).
        self.assertTrue(issubclass(pipeline.CMv40Cancelled, Exception))
        self.assertNotIsInstance(pipeline.CMv40Cancelled(""), RuntimeError)


class TestLosSubprocesosLoConsultan(unittest.IsolatedAsyncioTestCase):
    """Los tres lanzadores de subproceso del pipeline deben abortar antes de
    gastar nada."""

    def tearDown(self):
        pipeline.set_cancel_check(None)

    async def test_run_aborta_sin_lanzar_el_proceso(self):
        pipeline.set_cancel_check(lambda: True)
        with self.assertRaises(pipeline.CMv40Cancelled):
            # Un binario que no existe: si llegara a lanzarlo, el error sería
            # otro (FileNotFoundError).
            await pipeline._run(["binario_que_no_existe_12345"], timeout=5)

    async def test_run_streaming_aborta_sin_lanzar_el_proceso(self):
        pipeline.set_cancel_check(lambda: True)
        with self.assertRaises(pipeline.CMv40Cancelled):
            await pipeline._run_streaming(["binario_que_no_existe_12345"])

    async def test_run_with_time_estimate_aborta(self):
        pipeline.set_cancel_check(lambda: True)
        with self.assertRaises(pipeline.CMv40Cancelled):
            await pipeline._run_with_time_estimate(
                ["binario_que_no_existe_12345"], estimated_s=1.0)

    async def test_sin_cancelar_llega_a_lanzar_el_proceso(self):
        # Control: sin cancelación el flujo sigue y el fallo es el del binario.
        pipeline.set_cancel_check(lambda: False)
        with self.assertRaises((FileNotFoundError, OSError)):
            await pipeline._run(["binario_que_no_existe_12345"], timeout=5)


class TestUnaFaseSeDetieneEntrePasos(unittest.IsolatedAsyncioTestCase):
    """El caso que motivó todo: cancelar mientras una fase encadena comandos."""

    def setUp(self):
        import shutil
        import tempfile
        from cmv40_harness import FakeToolbox, RpuProps, make_session, write_artifacts
        self.tmp = Path(tempfile.mkdtemp(prefix="cancel_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wd = self.tmp / "wd"
        self.wd.mkdir(parents=True)
        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)
        self.addCleanup(lambda: pipeline.set_cancel_check(None))

        src = RpuProps(profile=7, el_type="MEL", cm_version="v2.9", frames=1000)
        tgt = RpuProps(profile=7, el_type="MEL", cm_version="v4.0",
                       frames=1000, has_l8=True)
        self.tb.define_rpu("RPU_source.bin", **src.as_dict())
        self.tb.define_rpu("RPU_target.bin", **tgt.as_dict())
        write_artifacts(self.wd, "RPU_source.bin", props=src)
        write_artifacts(self.wd, "RPU_target.bin", props=tgt)
        write_artifacts(self.wd, "BL.hevc", props=src)
        self.session = make_session(
            self.wd, source_workflow="p7_mel",
            target_type="trusted_p7_mel_final", target_trust_ok=True,
            source_frame_count=1000, target_frame_count=1000)

    async def test_cancelar_a_mitad_corta_la_cadena_de_comandos(self):
        # Fase F en p7_mel encadena: info ×2 → editor (merge) → info →
        # editor (P8.1) → info → inject-rpu. Cancelamos tras los primeros
        # comandos y comprobamos que NO llega al inject.
        from cmv40_harness import CollectingLog
        cancelado = {"si": False}
        pipeline.set_cancel_check(lambda: cancelado["si"])

        async def _log(msg):
            # En cuanto se anuncia el merge, el usuario cancela.
            if "Transferencia CMv4.0 levels" in msg:
                cancelado["si"] = True

        with self.assertRaises(pipeline.CMv40Cancelled):
            await pipeline.run_phase_f_inject(self.session, _log)

        self.assertFalse(self.tb.ran("dovi_tool", "inject-rpu"),
                         "la fase debía cortarse antes de inyectar")

    async def test_sin_cancelar_la_fase_completa(self):
        from cmv40_harness import CollectingLog
        pipeline.set_cancel_check(lambda: False)
        await pipeline.run_phase_f_inject(self.session, CollectingLog())
        self.assertTrue(self.tb.ran("dovi_tool", "inject-rpu"))


if __name__ == "__main__":
    unittest.main()
