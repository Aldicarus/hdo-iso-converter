"""El pipe de la Fase A, ejercitado de punta a punta.

`_ffmpeg_extract_rpu_piped` conecta ffmpeg y `dovi_tool extract-rpu` por un
`os.pipe()` para que las dos pasadas grandes de la Fase A —una limitada por
disco, la otra por CPU— corran a la vez en lugar de una detrás de la otra.
Sobre John Wick 4 (243.552 frames, MKV de 88 GB) eso es la diferencia entre
946 s en serie y ~574 s solapados.

Hasta ahora ningún test llegaba a ejecutarlo: el binario falso de ffmpeg
trataba `pipe:1` como una ruta y creaba un fichero con ese nombre, así que
el pipe se quedaba vacío, la función devolvía False y todo el mundo caía al
camino en dos pasos. Verde, pero probando la rama que NO corre en el NAS.

Lo que se comprueba aquí:
  - el camino piped produce el RPU, y con el muxer `tee` también el HEVC;
  - el RPU que sale contiene el DV del MKV de entrada (que es el punto: si
    el pipe perdiera el contenido, el resto del pipeline trabajaría sobre
    metadata equivocada sin que nada avisase);
  - ante un fallo devuelve False SIN lanzar, porque el contrato con el
    caller es que pueda recurrir al camino clásico.
"""
import asyncio
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
for _p in (str(APP_DIR), str(APP_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cmv40_harness import (  # noqa: E402
    CollectingLog, FakeToolbox, PhaseTestCase, RpuProps, write_artifacts,
)


class TestPipeFaseA(PhaseTestCase):

    def setUp(self):
        super().setUp()
        self.mkv = self.tmp / "Origen.mkv"
        self.props = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                              frames=243552, scenes=1101, has_l8=True)
        write_artifacts(self.tmp, self.mkv.name, props=self.props)

    async def _piped(self, hevc_out=None, **kw):
        from phases.cmv40_pipeline import _ffmpeg_extract_rpu_piped
        rpu = self.tmp / "RPU_source.bin"
        log = CollectingLog()
        # Acotado a 30 s: con binarios falsos esto tarda menos de un
        # segundo, y el timeout interno de la función es adaptativo con
        # suelo de 20 min. Sin este límite, un fallo que dejase el pipe
        # sin cerrar —el consumidor no vería nunca el EOF— colgaría la
        # suite en vez de fallar.
        ok = await asyncio.wait_for(_ffmpeg_extract_rpu_piped(
            str(self.mkv), rpu, hevc_out=hevc_out, log_callback=log, **kw),
            timeout=30)
        return ok, rpu, log

    async def test_el_camino_piped_produce_el_rpu(self):
        ok, rpu, _ = await self._piped()
        self.assertTrue(ok, "el pipe debe completarse con las herramientas OK")
        self.assertTrue(rpu.exists())
        self.assertGreater(rpu.stat().st_size, 0)

    async def test_con_tee_produce_tambien_el_hevc(self):
        hevc = self.tmp / "source.hevc"
        ok, rpu, _ = await self._piped(hevc_out=hevc)
        self.assertTrue(ok)
        self.assertTrue(hevc.exists(), "el muxer tee escribe fichero y pipe")
        self.assertTrue(rpu.exists())

    async def test_el_rpu_conserva_el_dv_del_mkv_de_entrada(self):
        # Lo que de verdad importa: que por el pipe no se pierda el
        # contenido. Un RPU con metadata de otro disco pasaría los guards
        # de tamaño y reventaría mucho más tarde, en la Fase H.
        _, rpu, _ = await self._piped(hevc_out=self.tmp / "source.hevc")
        salida = self.tb.props_of(rpu)
        self.assertIsNotNone(salida)
        self.assertEqual(salida.profile, 7)
        self.assertEqual(salida.el_type, "FEL")
        self.assertEqual(salida.cm_version, "v4.0")
        self.assertEqual(salida.frames, 243552)
        self.assertTrue(salida.has_l8)

    async def test_no_deja_un_fichero_llamado_pipe_1(self):
        antes = set(self.tmp.rglob("*"))
        await self._piped(hevc_out=self.tmp / "source.hevc")
        nuevos = {p.name for p in set(self.tmp.rglob("*")) - antes}
        self.assertNotIn("pipe:1", nuevos)
        self.assertFalse([n for n in nuevos if n.startswith("pipe:")],
                         "`pipe:1` es stdout, no una ruta")

    async def test_si_ffmpeg_falla_devuelve_false_sin_lanzar(self):
        self.tb.fail("ffmpeg", "*", rc=1, stderr="Invalid data found")
        ok, _, _ = await self._piped()
        self.assertFalse(ok, "el caller necesita un False para poder "
                             "recurrir al camino en dos pasos")

    async def test_si_extract_rpu_falla_devuelve_false_sin_lanzar(self):
        self.tb.fail("dovi_tool", "extract-rpu", rc=1, stderr="no RPU found")
        ok, _, _ = await self._piped()
        self.assertFalse(ok)

    async def test_registra_el_comando_completo_en_el_log(self):
        # El log del NAS es la única forma de saber a posteriori si un job
        # fue por el pipe o por el camino largo.
        _, _, log = await self._piped(hevc_out=self.tmp / "source.hevc")
        texto = "\n".join(log.lines)
        self.assertIn("|", texto, "debe verse que son dos procesos en pipe")
        self.assertIn("extract-rpu", texto)

    async def test_una_sola_pasada_de_ffmpeg(self):
        # El ahorro viene de no leer el MKV dos veces.
        await self._piped(hevc_out=self.tmp / "source.hevc")
        self.assertEqual(len(self.tb.find("ffmpeg")), 1)
        self.assertEqual(len(self.tb.find("dovi_tool", "extract-rpu")), 1)


if __name__ == "__main__":
    unittest.main()


class TestProgresoSinFicheroDeSalida(PhaseTestCase):
    """Sin `hevc_out` el progreso también tiene que salir.

    El consumidor mide su avance como `rchar / total`, y el total lo
    extrapolaba del TAMAÑO DEL FICHERO de salida. Con `hevc_out=None` no hay
    fichero, así que se rendía y no emitía nada. Daba igual mientras el único
    caller sin fichero era el pre-flight (un sniff de 30 s), pero el perfil de
    luminancia de Tab 2 recorre la película entera: medido en el NAS, 300 s con
    la barra clavada en 0 % sobre un MKV de 72 GB.

    Ahora el total sale del `size=` que ffmpeg reporta en su stderr, que mide
    exactamente lo mismo: los bytes que ha metido en el pipe. El binario falso
    tuvo que aprender a emitirlo — la línea que escribía antes no llevaba
    `size=` ni `time=`, o sea justo los dos campos de los que sale el
    porcentaje, y por eso ningún test lo cazó.
    """

    def setUp(self):
        super().setUp()
        self.mkv = self.tmp / "Origen.mkv"
        write_artifacts(self.tmp, self.mkv.name,
                        props=RpuProps(profile=7, el_type="FEL",
                                       cm_version="v4.0", frames=243552))

    async def _correr(self, hevc_out=None):
        from phases.cmv40_pipeline import _ffmpeg_extract_rpu_piped
        log = CollectingLog()
        ok = await asyncio.wait_for(_ffmpeg_extract_rpu_piped(
            str(self.mkv), self.tmp / "RPU.bin", hevc_out=hevc_out,
            duration=7200.0, log_callback=log), timeout=30)
        return ok, log

    def test_sin_fichero_de_salida_el_total_sale_del_size_de_ffmpeg(self):
        """EL BUG. `_total_del_stream` es el denominador del progreso: sin
        total no se emite nada."""
        from phases.cmv40_pipeline import _total_del_stream
        # ffmpeg lleva 70 GB escritos y va por la mitad del vídeo.
        total = _total_del_stream(None, 70_000_000_000, media_frac=0.5,
                                 ff_eof=False)
        self.assertIsNotNone(total, "sin fichero de salida no hay progreso")
        self.assertAlmostEqual(total, 140_000_000_000, delta=1)

    def test_cuando_ffmpeg_cierra_lo_escrito_es_el_total(self):
        from phases.cmv40_pipeline import _total_del_stream
        self.assertEqual(
            _total_del_stream(None, 72_433_350 * 1024, 0.99, ff_eof=True),
            float(72_433_350 * 1024))

    def test_con_fichero_manda_el_fichero(self):
        """Con `tee`, el tamaño real en disco es el dato de verdad; a ffmpeg no
        se le puede preguntar porque con dos salidas emite `Lsize=N/A`."""
        from phases.cmv40_pipeline import _total_del_stream
        hevc = self.tmp / "source.hevc"
        hevc.write_bytes(b"x" * 5000)
        total = _total_del_stream(hevc, ff_bytes=999_999_999,
                                 media_frac=0.5, ff_eof=True)
        self.assertEqual(total, 5000.0)

    def test_al_principio_no_se_extrapola(self):
        """Con media_frac de 0,001 el total saldría multiplicado por mil."""
        from phases.cmv40_pipeline import _total_del_stream
        self.assertIsNone(_total_del_stream(None, 1_000_000, 0.001, False))

    def test_sin_nada_escrito_no_hay_total(self):
        from phases.cmv40_pipeline import _total_del_stream
        self.assertIsNone(_total_del_stream(None, 0, 0.5, False))
        self.assertIsNone(_total_del_stream(None, 0, 0.5, True))

    async def test_el_ffmpeg_falso_emite_size_y_time(self):
        """Fidelidad del fake: sin estos dos campos el progreso no se puede
        calcular, y un test sobre un fake mudo pasa en verde con la barra
        muerta. Corre en cualquier plataforma."""
        ok, log = await self._correr(hevc_out=self.tmp / "source.hevc")
        self.assertTrue(ok)
        from phases.cmv40_pipeline import _FFMPEG_SIZE_RE, _FFMPEG_TIME_RE
        crudas = "\n".join(log.lines)
        # El pipeline no reenvía las líneas crudas, así que se comprueba sobre
        # lo que el fake escribe, invocándolo directamente.
        import subprocess
        r = subprocess.run(["ffmpeg", "-i", str(self.mkv), "-f", "hevc",
                            str(self.tmp / "x.hevc")],
                           capture_output=True, text=True)
        self.assertTrue(_FFMPEG_SIZE_RE.search(r.stderr), r.stderr)
        self.assertTrue(_FFMPEG_TIME_RE.search(r.stderr), r.stderr)

    def test_el_regex_de_size_entiende_las_unidades_reales(self):
        """Del log de un job real: `size=72402713KiB` y `Lsize=72433350KiB`.
        El segundo lleva prefijo, así que el patrón tiene que casar dentro."""
        from phases.cmv40_pipeline import _FFMPEG_SIZE_RE, _SIZE_UNIT_MB
        for linea, esperado_mb in (
            ("frame=207166 fps=1038 q=-1.0 size=72402713KiB time=02:24:00.54", 72402713 / 1024),
            ("frame=209389 fps=1048 q=-1.0 Lsize=72433350KiB time=02:25:33.22", 72433350 / 1024),
            ("size=  1024 kB time=00:00:10.00", 1.0),
        ):
            m = _FFMPEG_SIZE_RE.search(linea)
            self.assertIsNotNone(m, linea)
            mb = int(m.group(1)) * _SIZE_UNIT_MB[m.group(2)]
            self.assertAlmostEqual(mb, esperado_mb, places=2, msg=linea)
