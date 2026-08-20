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
cmv40_routes = None


def setUpModule():
    """main.py monta StaticFiles('static') con ruta RELATIVA, así que solo
    importa con el cwd en app/. La suite se lanza desde la raíz del repo."""
    # El `os.chdir(APP_DIR)` que había aquí era para que
    # `StaticFiles(directory="static")` de main.py encontrara el
    # directorio; desde que se monta por ruta absoluta ya no hace
    # falta cambiar el cwd del proceso de test.
    global cmv40_routes
    from routers import cmv40 as _routes
    cmv40_routes = _routes


PROGRESS = '§§PROGRESS§§{"pct": 95.0, "label": "Inyectando RPU"}'


class TestProgressDedup(unittest.TestCase):
    """`_cmv40_progress_should_emit`: filtra repeticiones, deja pasar cambios."""

    def setUp(self):
        self.sid = "sess_dedup"
        cmv40_routes._cmv40_last_progress.pop(self.sid, None)

    def tearDown(self):
        cmv40_routes._cmv40_last_progress.pop(self.sid, None)

    def test_primera_emision_pasa(self):
        self.assertTrue(cmv40_routes._cmv40_progress_should_emit(self.sid, PROGRESS))

    def test_repeticion_identica_se_descarta(self):
        cmv40_routes._cmv40_progress_should_emit(self.sid, PROGRESS)
        for _ in range(10):
            self.assertFalse(cmv40_routes._cmv40_progress_should_emit(self.sid, PROGRESS))

    def test_cambio_de_pct_pasa(self):
        cmv40_routes._cmv40_progress_should_emit(self.sid, PROGRESS)
        otro = '§§PROGRESS§§{"pct": 96.0, "label": "Inyectando RPU"}'
        self.assertTrue(cmv40_routes._cmv40_progress_should_emit(self.sid, otro))

    def test_cambio_de_label_pasa(self):
        cmv40_routes._cmv40_progress_should_emit(self.sid, PROGRESS)
        otro = '§§PROGRESS§§{"pct": 95.0, "label": "Remuxando"}'
        self.assertTrue(cmv40_routes._cmv40_progress_should_emit(self.sid, otro))

    def test_heartbeat_reemite_tras_la_ventana(self):
        """Un cliente que se reconecta debe recuperar la barra aunque el pct
        lleve minutos congelado."""
        cmv40_routes._cmv40_progress_should_emit(self.sid, PROGRESS)
        self.assertFalse(cmv40_routes._cmv40_progress_should_emit(self.sid, PROGRESS))
        # Envejecemos el timestamp más allá del heartbeat
        payload, _ts = cmv40_routes._cmv40_last_progress[self.sid]
        cmv40_routes._cmv40_last_progress[self.sid] = (
            payload, _ts - cmv40_routes._CMV40_PROGRESS_HEARTBEAT_S - 1.0)
        self.assertTrue(cmv40_routes._cmv40_progress_should_emit(self.sid, PROGRESS))

    def test_sesiones_distintas_no_se_pisan(self):
        other = "sess_dedup_2"
        cmv40_routes._cmv40_last_progress.pop(other, None)
        try:
            self.assertTrue(cmv40_routes._cmv40_progress_should_emit(self.sid, PROGRESS))
            self.assertTrue(cmv40_routes._cmv40_progress_should_emit(other, PROGRESS))
        finally:
            cmv40_routes._cmv40_last_progress.pop(other, None)


class TestProgressNoPersiste(unittest.IsolatedAsyncioTestCase):
    """`_cmv40_log`: el progreso va al WS pero nunca a `output_log`."""

    def setUp(self):
        from models import CMv40Session
        self.session = CMv40Session(
            id="sess_log", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="out.mkv")
        cmv40_routes._cmv40_last_progress.pop(self.session.id, None)
        cmv40_routes._cmv40_log_throttle.pop(self.session.id, None)
        self.persisted = []
        self._orig_persist = cmv40_routes._cmv40_maybe_persist_log

        async def _fake_persist(session, line):
            self.persisted.append(line)

        cmv40_routes._cmv40_maybe_persist_log = _fake_persist

    def tearDown(self):
        cmv40_routes._cmv40_maybe_persist_log = self._orig_persist
        cmv40_routes._cmv40_last_progress.pop(self.session.id, None)

    async def test_progreso_no_entra_en_output_log(self):
        await cmv40_routes._cmv40_log(self.session, PROGRESS)
        self.assertEqual(self.session.output_log, [])
        self.assertEqual(self.persisted, [])

    async def test_linea_normal_si_entra_y_persiste(self):
        await cmv40_routes._cmv40_log(self.session, "[Fase F] algo pasó")
        self.assertEqual(len(self.session.output_log), 1)
        self.assertIn("algo pasó", self.session.output_log[0])
        self.assertEqual(len(self.persisted), 1)

    async def test_rafaga_de_progreso_identico_no_infla_nada(self):
        """El escenario real: 450 ticks idénticos durante una fase larga."""
        for _ in range(450):
            await cmv40_routes._cmv40_log(self.session, PROGRESS)
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
        self._orig_load = cmv40_routes.load_cmv40_session
        self._orig_scan = cmv40_routes._cmv40_scan_artifacts
        cmv40_routes.load_cmv40_session = lambda sid: self.session
        cmv40_routes._cmv40_scan_artifacts = lambda s: {}

    def tearDown(self):
        cmv40_routes.load_cmv40_session = self._orig_load
        cmv40_routes._cmv40_scan_artifacts = self._orig_scan

    async def test_por_defecto_trae_el_log(self):
        data = await cmv40_routes.cmv40_get(self.session.id)
        self.assertEqual(data["output_log"], ["a", "b", "c"])
        self.assertNotIn("output_log_omitted", data)

    async def test_include_log_false_lo_omite_pero_informa(self):
        data = await cmv40_routes.cmv40_get(self.session.id, include_log=False)
        self.assertEqual(data["output_log"], [])
        self.assertTrue(data["output_log_omitted"])
        self.assertEqual(data["output_log_len"], 3)
        # El estado, que es lo que el poller viene a buscar, sigue completo
        self.assertEqual(data["phase"], self.session.phase)
        self.assertIn("running_phase", data)


if __name__ == "__main__":
    unittest.main()


class TestLastProgressPersistido(unittest.IsolatedAsyncioTestCase):
    """`session.last_progress`: la barra sobrevive a un WebSocket caído.

    Los pasos silenciosos (extract-rpu, export, demux) tardan minutos sin
    escribir una línea. Desde que el progreso no se persiste en `output_log`,
    la barra dependía solo del WS — y con el WS parpadeando la UI se quedaba
    muerta y parecía un cuelgue (reportado el 2026-08-16 en la Fase A).
    """

    def setUp(self):
        from models import CMv40Session
        self.session = CMv40Session(
            id="sess_prog", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="out.mkv")
        cmv40_routes._cmv40_last_progress.pop(self.session.id, None)
        cmv40_routes._cmv40_progress_persist_ts.pop(self.session.id, None)
        self._orig_persist = cmv40_routes._cmv40_maybe_persist_log
        self._orig_save = cmv40_routes._save_cmv40_session_async
        self.saves = []

        async def _fake_persist(session, line):
            pass

        async def _fake_save(session):
            self.saves.append(session.id)

        cmv40_routes._cmv40_maybe_persist_log = _fake_persist
        cmv40_routes._save_cmv40_session_async = _fake_save

    def tearDown(self):
        cmv40_routes._cmv40_maybe_persist_log = self._orig_persist
        cmv40_routes._save_cmv40_session_async = self._orig_save
        cmv40_routes._cmv40_last_progress.pop(self.session.id, None)
        cmv40_routes._cmv40_progress_persist_ts.pop(self.session.id, None)

    async def test_el_progreso_queda_en_la_sesion(self):
        await cmv40_routes._cmv40_log(self.session, PROGRESS)
        self.assertEqual(self.session.last_progress,
                         {"pct": 95.0, "label": "Inyectando RPU"})
        # …y sigue sin ensuciar el log
        self.assertEqual(self.session.output_log, [])

    async def test_se_actualiza_con_cada_valor_nuevo(self):
        await cmv40_routes._cmv40_log(self.session, PROGRESS)
        await cmv40_routes._cmv40_log(
            self.session, '§§PROGRESS§§{"pct": 96.5, "label": "Inyectando RPU"}')
        self.assertEqual(self.session.last_progress["pct"], 96.5)

    async def test_payload_invalido_no_revienta(self):
        await cmv40_routes._cmv40_log(self.session, "§§PROGRESS§§no-es-json")
        self.assertIsNone(self.session.last_progress)

    async def test_la_persistencia_va_throttled(self):
        """Debe tocar disco, pero no en cada tick."""
        import asyncio as _a
        await cmv40_routes._cmv40_log(self.session, PROGRESS)
        for i in range(20):
            await cmv40_routes._cmv40_log(
                self.session, f'§§PROGRESS§§{{"pct": {i}, "label": "X"}}')
        await _a.sleep(0)  # deja correr las tasks de save en background
        self.assertLessEqual(len(self.saves), 1,
                             "el progreso no debe persistir en cada tick")


class TestEndpointActivo(unittest.IsolatedAsyncioTestCase):
    """`GET /api/cmv40-active`: respuesta mínima para el punto verde del tab.

    El frontend lo consulta cada 5 s. Empezó pidiendo `GET /api/cmv40`, que con
    88 proyectos son 569 KB y 193 ms; pasó al summary cacheado, que aún hace un
    `glob` más un `stat` por sesión (~88 syscalls cada 5 s, 1,5 millones al
    día); y ahora sale del registro en memoria, que cuesta cero.

    El contrato de esa migración lo cubre `test_cmv40_activas_en_memoria`
    (incluido el guard de que nadie asigne `running_phase` a mano); aquí solo
    queda lo que le toca a este módulo: que el payload siga siendo mínimo.
    """

    def setUp(self):
        self.activas = cmv40_routes._cmv40_activas
        self._orig = dict(self.activas)
        self.activas.clear()

    def tearDown(self):
        self.activas.clear()
        self.activas.update(self._orig)

    async def test_sin_jobs(self):
        data = await cmv40_routes.cmv40_active()
        self.assertFalse(data["active"])
        self.assertEqual(data["ids"], [])

    async def test_con_job_en_curso(self):
        self.activas["b"] = "inject"
        data = await cmv40_routes.cmv40_active()
        self.assertTrue(data["active"])
        self.assertEqual(data["ids"], ["b"])

    async def test_no_devuelve_el_listado_entero(self):
        """El payload debe ser mínimo — nada de arrastrar los summaries."""
        for i in range(50):
            self.activas[f"s{i}"] = "inject"
        data = await cmv40_routes.cmv40_active()
        self.assertEqual(set(data.keys()), {"active", "ids"})

    async def test_no_toca_el_disco(self):
        """Lo que hace que el poll de cada 5 s sea gratis."""
        import storage
        llamadas = []
        original = storage.list_cmv40_sessions_summary
        storage.list_cmv40_sessions_summary = lambda: llamadas.append(1) or []
        try:
            await cmv40_routes.cmv40_active()
        finally:
            storage.list_cmv40_sessions_summary = original
        self.assertEqual(llamadas, [])
