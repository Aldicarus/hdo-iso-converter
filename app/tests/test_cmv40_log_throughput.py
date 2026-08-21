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
import asyncio
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
    """`_cmv40_log`: el progreso va al WS pero nunca al log persistido."""

    def setUp(self):
        import shutil
        import tempfile

        import storage
        from models import CMv40Session
        self.session = CMv40Session(
            id="sess_log", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="out.mkv")
        cmv40_routes._cmv40_last_progress.pop(self.session.id, None)
        self.addCleanup(cmv40_routes._cmv40_last_progress.pop, self.session.id, None)
        # El log vive en `/config/cmv40/{id}.log` desde que dejó de ir dentro
        # del JSON: sin redirigirlo, este test escribiría en el /config real.
        tmp = Path(tempfile.mkdtemp(prefix="log_test_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self._orig_dir = storage.CMV40_DIR
        storage.CMV40_DIR = tmp
        self.addCleanup(setattr, storage, "CMV40_DIR", self._orig_dir)
        for d in (cmv40_routes._cmv40_log_buffer, cmv40_routes._cmv40_log_buffer_ts):
            d.pop(self.session.id, None)
            self.addCleanup(d.pop, self.session.id, None)

    def log(self):
        return cmv40_routes._cmv40_log_completo(self.session)

    async def test_progreso_no_entra_en_el_log(self):
        await cmv40_routes._cmv40_log(self.session, PROGRESS)
        self.assertEqual(self.log(), [])
        self.assertEqual(self.session.output_log, [],
                         "el JSON de la sesión ya no acumula el log")

    async def test_linea_normal_si_entra(self):
        await cmv40_routes._cmv40_log(self.session, "[Fase F] algo pasó")
        self.assertEqual(len(self.log()), 1)
        self.assertIn("algo pasó", self.log()[0])

    async def test_rafaga_de_progreso_identico_no_infla_nada(self):
        """El escenario real: 450 ticks idénticos durante una fase larga."""
        for _ in range(450):
            await cmv40_routes._cmv40_log(self.session, PROGRESS)
        self.assertEqual(self.log(), [])

    async def test_el_json_de_la_sesion_no_crece_con_el_log(self):
        """EL PUNTO de la Tanda 3. Antes, mil líneas eran ~200 KB más de JSON
        reescritos en cada save."""
        antes = len(self.session.model_dump_json())
        for i in range(1000):
            await cmv40_routes._cmv40_log(self.session, f"[Fase C] línea {i}")
        await cmv40_routes._cmv40_log_volcar(self.session.id, forzar=True)
        despues = len(self.session.model_dump_json())
        self.assertEqual(antes, despues,
                         "el JSON de la sesión ha crecido con el log")
        self.assertEqual(len(self.log()), 1000)

    async def test_el_log_sobrevive_a_recargar_la_sesion(self):
        """Está en un fichero, así que no depende del objeto en memoria."""
        for i in range(5):
            await cmv40_routes._cmv40_log(self.session, f"línea {i}")
        await cmv40_routes._cmv40_log_volcar(self.session.id, forzar=True)
        from storage import read_cmv40_log
        self.assertEqual(len(read_cmv40_log(self.session.id)), 5)

    async def test_el_prefijo_legacy_del_json_se_conserva(self):
        """Las sesiones anteriores al cambio tienen su log dentro del JSON y no
        se migran: el log completo es ese prefijo + el fichero."""
        self.session.output_log = ["[10:00:00] línea vieja"]
        await cmv40_routes._cmv40_log(self.session, "línea nueva")
        completo = self.log()
        self.assertEqual(len(completo), 2)
        self.assertIn("línea vieja", completo[0])
        self.assertIn("línea nueva", completo[1])


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
        self._orig_save = cmv40_routes._save_cmv40_session_async
        self.saves = []

        async def _fake_save(session):
            self.saves.append(session.id)

        cmv40_routes._save_cmv40_session_async = _fake_save

    def tearDown(self):
        cmv40_routes._save_cmv40_session_async = self._orig_save
        cmv40_routes._cmv40_last_progress.pop(self.session.id, None)
        cmv40_routes._cmv40_progress_persist_ts.pop(self.session.id, None)

    async def test_el_progreso_queda_en_la_sesion(self):
        await cmv40_routes._cmv40_log(self.session, PROGRESS)
        self.assertEqual(self.session.last_progress,
                         {"pct": 95.0, "label": "Inyectando RPU"})
        # …y sigue sin ensuciar el log (que ya no vive en el JSON)
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


async def _drenar_tasks() -> None:
    """Espera a las tasks que el código bajo prueba haya lanzado.

    Un `await asyncio.sleep(0.2)` aquí sería un test atado al reloj: pasa en un
    Mac ocioso y falla bajo carga, que es la receta del intermitente. Esperar a
    las tasks es determinista.
    """
    pendientes = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pendientes:
        await asyncio.gather(*pendientes, return_exceptions=True)


class TestEscriturasEnConfig(unittest.IsolatedAsyncioTestCase):
    """Lo que la Tanda 3 vino a arreglar: cuánto se escribe en /config.

    Persistir UNA línea de log reescribía la sesión entera. Medido: 0,86 MB de
    JSON con el log vacío y 2,21 MB con 10.000 líneas, y con el throttle a 5 s
    son ~850 saves por job → **~1,9 GB escritos** contra el mismo pool ZFS por
    el que el pipeline mueve 70 GB. El coste era CUADRÁTICO en la longitud del
    log: el throttle acotaba la frecuencia, no el tamaño de cada escritura.

    Y al mover el log a un fichero, el coste dominante pasó a ser el otro dato
    pequeño que vivía dentro del JSON: `last_progress`, ~50 bytes reescritos
    con 0,86 MB cada 20 s. También a un sidecar.
    """

    def setUp(self):
        import shutil
        import tempfile

        import storage
        from models import CMv40Session, L2Combo
        self.tmp = Path(tempfile.mkdtemp(prefix="escrituras_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = storage.CMV40_DIR
        storage.CMV40_DIR = self.tmp
        self.addCleanup(setattr, storage, "CMV40_DIR", self._orig)
        self.storage = storage

        self.session = CMv40Session(
            id="sess_bytes", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="out.mkv")
        # Los 3.914 combos de un caso real: son lo que hace que el JSON de la
        # sesión pese 0,86 MB aunque el log esté vacío.
        self.session.target_l8_combos = [
            L2Combo(**{k: 2048 for k in L2Combo.model_fields if k != "count"})
            for _ in range(3914)]
        for d in (cmv40_routes._cmv40_log_buffer, cmv40_routes._cmv40_log_buffer_ts,
                  cmv40_routes._cmv40_last_progress,
                  cmv40_routes._cmv40_progress_persist_ts):
            d.pop(self.session.id, None)
            self.addCleanup(d.pop, self.session.id, None)

    async def test_mil_lineas_no_cambian_el_tamano_del_json(self):
        antes = len(self.session.model_dump_json(indent=2))
        for i in range(1000):
            await cmv40_routes._cmv40_log(self.session, f"[Fase C] línea {i}")
        await cmv40_routes._cmv40_log_volcar(self.session.id, forzar=True)
        self.assertEqual(len(self.session.model_dump_json(indent=2)), antes)

    async def test_el_log_acaba_completo_en_su_fichero(self):
        for i in range(250):
            await cmv40_routes._cmv40_log(self.session, f"línea {i}")
        await cmv40_routes._cmv40_log_volcar(self.session.id, forzar=True)
        lineas = self.storage.read_cmv40_log(self.session.id)
        self.assertEqual(len(lineas), 250)
        self.assertIn("línea 0", lineas[0])
        self.assertIn("línea 249", lineas[-1])

    async def test_una_linea_con_saltos_no_rompe_el_fichero(self):
        """El log es una línea por registro: un `\\n` dentro rompería la cuenta."""
        await cmv40_routes._cmv40_log(self.session, "primera\\nsegunda\\ntercera")
        await cmv40_routes._cmv40_log_volcar(self.session.id, forzar=True)
        self.assertEqual(len(self.storage.read_cmv40_log(self.session.id)), 1)

    async def test_el_progreso_va_a_su_sidecar_no_al_json(self):
        """Lo que importa no es el tamaño del objeto en memoria —el campo sigue
        en el modelo por compatibilidad— sino que persistir la barra **no
        reescriba el JSON de la sesión**, que son 0,86 MB con los combos."""
        saves = []
        orig = cmv40_routes._save_cmv40_session_async

        async def _espia(session):
            saves.append(session.id)

        cmv40_routes._save_cmv40_session_async = _espia
        self.addCleanup(setattr, cmv40_routes, "_save_cmv40_session_async", orig)

        # Se espera a cada escritura antes de la siguiente. Lanzarlas juntas
        # crearía una carrera que en producción no existe: el guard de
        # `_CMV40_PROGRESS_PERSIST_S` las separa 20 s y cada una tarda ~1 ms.
        # Sin esperar, el fichero acababa con el valor de la que ganase la
        # carrera de threads y el test fallaba 1 de cada 5 veces.
        for pct in (10.0, 40.0, 90.0):
            cmv40_routes._cmv40_progress_persist_ts.pop(self.session.id, None)
            cmv40_routes._cmv40_store_last_progress(
                self.session, '§§PROGRESS§§{"pct": %s, "label": "Inyectando"}' % pct)
            await _drenar_tasks()      # la escritura va en un thread

        self.assertEqual(saves, [],
                         "la barra de progreso ha reescrito el JSON de la sesión")
        guardado = self.storage.read_cmv40_progress(self.session.id)
        self.assertEqual(guardado["pct"], 90.0)
        self.assertLess(self.storage.cmv40_progress_path(self.session.id).stat().st_size,
                        500, "el sidecar del progreso son unos cientos de bytes")

    def test_borrar_la_sesion_se_lleva_el_log_y_el_progreso(self):
        self.storage.append_cmv40_log(self.session.id, ["una línea"])
        self.storage.write_cmv40_progress(self.session.id, {"pct": 1.0})
        self.storage.save_cmv40_session(self.session)
        self.storage.delete_cmv40_session(self.session.id)
        self.assertFalse(self.storage.cmv40_log_path(self.session.id).exists())
        self.assertFalse(self.storage.cmv40_progress_path(self.session.id).exists())

    def test_el_listado_de_sesiones_ignora_los_ficheros_nuevos(self):
        """El sidebar hace `glob("*.json")`: el `.progress.json` NO puede colarse
        como si fuera una sesión."""
        self.storage.save_cmv40_session(self.session)
        self.storage.write_cmv40_progress(self.session.id, {"pct": 1.0})
        self.storage.append_cmv40_log(self.session.id, ["x"])
        cache = getattr(self.storage, "_cmv40_summary_by_file", None)
        if isinstance(cache, dict):
            cache.clear()
        ids = [s["id"] for s in self.storage.list_cmv40_sessions_summary()]
        self.assertEqual(ids, [self.session.id], ids)
