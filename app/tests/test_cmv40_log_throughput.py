"""
El log de un job CMv4.0 no debe costar más CPU/disco que el propio pipeline.

Medido en el NAS (Celeron N5095, 4 cores) durante un `dovi_tool inject-rpu`
real de John Wick 4: `dovi_tool` consumía el 94,7 % de un core y **uvicorn el
62,9 % de otro**. Dos causas, ambas cubiertas aquí:

  1. El ticker de `cmv40_pipeline` emite `§§PROGRESS§§` cada 2 s aunque el
     valor no cambie. Con el pct saturado al 95 % durante los últimos minutos
     de la fase, la MISMA línea se repetía cientos de veces; cada una entraba
     en `output_log` y disparaba la persistencia del JSON entero (2,16 MB).
  2. El snapshot previo a serializar era `model_copy(deep=True)`, que clona
     también los campos inmutables y enormes de la sesión (3.914 objetos
     `L2Combo` en el caso medido).

Y el endpoint de detalle, que el frontend pollea cada 1,5-4 s mientras hay un
job, debe poder responder sin el log (1,57 MB / 437 ms por tick).
"""
import os
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

_orig_cwd = None
main = None


def setUpModule():
    """main.py monta StaticFiles('static') con ruta RELATIVA, así que solo
    importa con el cwd en app/. La suite se lanza desde la raíz del repo."""
    global _orig_cwd, main
    _orig_cwd = os.getcwd()
    os.chdir(APP_DIR)
    import main as _main
    main = _main


def tearDownModule():
    if _orig_cwd:
        os.chdir(_orig_cwd)


PROGRESS = '§§PROGRESS§§{"pct": 95.0, "label": "Inyectando RPU"}'


class TestProgressDedup(unittest.TestCase):
    """`_cmv40_progress_should_emit`: filtra repeticiones, deja pasar cambios."""

    def setUp(self):
        self.sid = "sess_dedup"
        main._cmv40_last_progress.pop(self.sid, None)

    def tearDown(self):
        main._cmv40_last_progress.pop(self.sid, None)

    def test_primera_emision_pasa(self):
        self.assertTrue(main._cmv40_progress_should_emit(self.sid, PROGRESS))

    def test_repeticion_identica_se_descarta(self):
        main._cmv40_progress_should_emit(self.sid, PROGRESS)
        for _ in range(10):
            self.assertFalse(main._cmv40_progress_should_emit(self.sid, PROGRESS))

    def test_cambio_de_pct_pasa(self):
        main._cmv40_progress_should_emit(self.sid, PROGRESS)
        otro = '§§PROGRESS§§{"pct": 96.0, "label": "Inyectando RPU"}'
        self.assertTrue(main._cmv40_progress_should_emit(self.sid, otro))

    def test_cambio_de_label_pasa(self):
        main._cmv40_progress_should_emit(self.sid, PROGRESS)
        otro = '§§PROGRESS§§{"pct": 95.0, "label": "Remuxando"}'
        self.assertTrue(main._cmv40_progress_should_emit(self.sid, otro))

    def test_heartbeat_reemite_tras_la_ventana(self):
        """Un cliente que se reconecta debe recuperar la barra aunque el pct
        lleve minutos congelado."""
        main._cmv40_progress_should_emit(self.sid, PROGRESS)
        self.assertFalse(main._cmv40_progress_should_emit(self.sid, PROGRESS))
        # Envejecemos el timestamp más allá del heartbeat
        payload, _ts = main._cmv40_last_progress[self.sid]
        main._cmv40_last_progress[self.sid] = (
            payload, _ts - main._CMV40_PROGRESS_HEARTBEAT_S - 1.0)
        self.assertTrue(main._cmv40_progress_should_emit(self.sid, PROGRESS))

    def test_sesiones_distintas_no_se_pisan(self):
        other = "sess_dedup_2"
        main._cmv40_last_progress.pop(other, None)
        try:
            self.assertTrue(main._cmv40_progress_should_emit(self.sid, PROGRESS))
            self.assertTrue(main._cmv40_progress_should_emit(other, PROGRESS))
        finally:
            main._cmv40_last_progress.pop(other, None)


class TestProgressNoPersiste(unittest.IsolatedAsyncioTestCase):
    """`_cmv40_log`: el progreso va al WS pero nunca a `output_log`."""

    def setUp(self):
        from models import CMv40Session
        self.session = CMv40Session(
            id="sess_log", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="out.mkv")
        main._cmv40_last_progress.pop(self.session.id, None)
        main._cmv40_log_throttle.pop(self.session.id, None)
        self.persisted = []
        self._orig_persist = main._cmv40_maybe_persist_log

        async def _fake_persist(session, line):
            self.persisted.append(line)

        main._cmv40_maybe_persist_log = _fake_persist

    def tearDown(self):
        main._cmv40_maybe_persist_log = self._orig_persist
        main._cmv40_last_progress.pop(self.session.id, None)

    async def test_progreso_no_entra_en_output_log(self):
        await main._cmv40_log(self.session, PROGRESS)
        self.assertEqual(self.session.output_log, [])
        self.assertEqual(self.persisted, [])

    async def test_linea_normal_si_entra_y_persiste(self):
        await main._cmv40_log(self.session, "[Fase F] algo pasó")
        self.assertEqual(len(self.session.output_log), 1)
        self.assertIn("algo pasó", self.session.output_log[0])
        self.assertEqual(len(self.persisted), 1)

    async def test_rafaga_de_progreso_identico_no_infla_nada(self):
        """El escenario real: 450 ticks idénticos durante una fase larga."""
        for _ in range(450):
            await main._cmv40_log(self.session, PROGRESS)
        self.assertEqual(self.session.output_log, [])
        self.assertEqual(self.persisted, [])


class TestSnapshotBarato(unittest.TestCase):
    """`_log_safe_snapshot`: aísla lo que muta, comparte lo que no."""

    def _session(self):
        from models import CMv40Session
        s = CMv40Session(
            id="sess_snap", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="out.mkv")
        s.output_log = ["l1", "l2"]
        return s

    def test_las_listas_que_crecen_se_copian(self):
        from storage import _log_safe_snapshot
        s = self._session()
        snap = _log_safe_snapshot(s, ("output_log", "phase_history"))
        s.output_log.append("l3")
        # El snapshot conserva la foto del momento — el thread que serializa
        # no ve la línea añadida después.
        self.assertEqual(snap.output_log, ["l1", "l2"])
        self.assertEqual(len(s.output_log), 3)

    def test_los_campos_estables_se_comparten(self):
        """No los copiamos: son inmutables durante el job y clonarlos era el
        coste que queríamos quitar."""
        from storage import _log_safe_snapshot
        from models import L2Combo
        s = self._session()
        s.source_l2_combos = [
            L2Combo(target_max_pq=2081, trim_slope=2048, trim_offset=2048,
                    trim_power=2048, trim_chroma_weight=2048,
                    trim_saturation_gain=2048, ms_weight=2048,
                    occurrence_count=10)
        ]
        snap = _log_safe_snapshot(s, ("output_log", "phase_history"))
        self.assertIs(snap.source_l2_combos, s.source_l2_combos)

    def test_serializa_igual_que_el_original(self):
        from storage import _log_safe_snapshot
        s = self._session()
        snap = _log_safe_snapshot(s, ("output_log", "phase_history"))
        self.assertEqual(snap.model_dump_json(), s.model_dump_json())


class TestIncludeLog(unittest.IsolatedAsyncioTestCase):
    """`GET /api/cmv40/{id}?include_log=false` devuelve estado sin el log."""

    def setUp(self):
        from models import CMv40Session
        self.session = CMv40Session(
            id="sess_get", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="out.mkv")
        self.session.output_log = ["a", "b", "c"]
        self._orig_load = main.load_cmv40_session
        self._orig_scan = main._cmv40_scan_artifacts
        main.load_cmv40_session = lambda sid: self.session
        main._cmv40_scan_artifacts = lambda s: {}

    def tearDown(self):
        main.load_cmv40_session = self._orig_load
        main._cmv40_scan_artifacts = self._orig_scan

    async def test_por_defecto_trae_el_log(self):
        data = await main.cmv40_get(self.session.id)
        self.assertEqual(data["output_log"], ["a", "b", "c"])
        self.assertNotIn("output_log_omitted", data)

    async def test_include_log_false_lo_omite_pero_informa(self):
        data = await main.cmv40_get(self.session.id, include_log=False)
        self.assertEqual(data["output_log"], [])
        self.assertTrue(data["output_log_omitted"])
        self.assertEqual(data["output_log_len"], 3)
        # El estado, que es lo que el poller viene a buscar, sigue completo
        self.assertEqual(data["phase"], self.session.phase)
        self.assertIn("running_phase", data)


if __name__ == "__main__":
    unittest.main()
