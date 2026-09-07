"""La cola de Tab 1 se reanuda tras un reinicio del contenedor.

`_load_state()` repuebla la cola desde `queue_state.json` al importar el módulo,
y el recovery de arranque tocaba las sesiones — pero **nadie volvía a mirar la
cola**: `_process()` solo se llama desde `enqueue` y desde su propio `finally`.
Resultado: los ids seguían en el fichero, `GET /api/queue` los devolvía y no los
ejecutaba nadie hasta que un `execute` nuevo despertaba el bucle. El estado del
backend y el del disco divergían sin que nada los reconciliara.

Y había un segundo error de bulto: el recovery mandaba a `pending` tanto lo que
estaba **corriendo** como lo que estaba **esperando**, que no es lo mismo. Un
trabajo que murió a mitad no se puede reanudar a ciegas; uno que ni había
empezado, sí.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_reanudar_cola -v
"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))


def _rip(session_id: str):
    """La cola guarda trabajos tipados desde que es única para las tres
    pestañas; antes era una lista de `session_id` pelados."""
    from queue_manager import TIPO_RIP, TrabajoEnCola
    return TrabajoEnCola(tab="rip", tipo=TIPO_RIP, clave=session_id,
                         que=f"rip de {session_id}")


class ColaCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import paths
        import storage
        from queue_manager import queue_manager

        self.tmp = Path(tempfile.mkdtemp(prefix="cola_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = (storage.CONFIG_DIR, paths.CONFIG_DIR)
        storage.CONFIG_DIR = paths.CONFIG_DIR = self.tmp
        self.addCleanup(lambda: setattr(paths, "CONFIG_DIR", self._orig[1]))
        self.addCleanup(lambda: setattr(storage, "CONFIG_DIR", self._orig[0]))
        cache = getattr(storage, "_sessions_summary_by_file", None)
        if isinstance(cache, dict):
            cache.clear()

        self.cola = queue_manager
        self._estado = (list(self.cola._queue), self.cola._running)
        self.cola._queue.clear()
        self.cola._running = None
        self._persist = self.cola._persist_state
        self.cola._persist_state = lambda: None

        def _restaurar():
            self.cola._queue[:] = self._estado[0]
            self.cola._running = self._estado[1]
            self.cola._persist_state = self._persist
        self.addCleanup(_restaurar)

    def sesion(self, sid, status):
        import storage
        from models import Session
        s = Session(id=sid, iso_path=f"/mnt/isos/{sid}.iso",
                    mkv_name=f"{sid}.mkv", status=status)
        storage.save_session(s)
        return sid


class TestElRecoveryDistingueLosDosCasos(ColaCase):

    def test_lo_que_corria_vuelve_a_pending_con_aviso(self):
        import storage
        from routers import tab1
        self.sesion("corriendo", "running")
        tab1.recuperar_sesiones_interrumpidas()
        s = storage.load_session("corriendo")
        self.assertEqual(s.status, "pending")
        self.assertIn("interrumpida", (s.error_message or "").lower())

    def test_lo_que_esperaba_en_la_cola_sigue_encolado(self):
        import storage
        from routers import tab1
        self.sesion("esperando", "queued")
        self.cola._queue.append(_rip("esperando"))
        tab1.recuperar_sesiones_interrumpidas()
        s = storage.load_session("esperando")
        self.assertEqual(s.status, "queued", "se ha perdido su sitio en la cola")
        self.assertIsNone(s.error_message)

    def test_queued_sin_sitio_en_la_cola_es_incoherente_y_se_corrige(self):
        """`queued` lo concede solo la cola: si no está en ella, es basura."""
        import storage
        from routers import tab1
        self.sesion("huerfana", "queued")
        tab1.recuperar_sesiones_interrumpidas()
        self.assertEqual(storage.load_session("huerfana").status, "pending")


class TestLaColaArranca(ColaCase):

    async def test_reanuda_el_trabajo_que_quedo_esperando(self):
        from routers import tab1
        arrancados = []

        async def _run(sid):
            arrancados.append(sid)

        self.cola.set_run_fn(_run)
        self.sesion("job1", "queued")
        self.cola._queue.append(_rip("job1"))

        await tab1.reanudar_cola()
        for _ in range(60):
            await asyncio.sleep(0.05)
            if arrancados:
                break
        self.assertEqual(arrancados, ["job1"], "la cola no arrancó sola")

    async def test_sin_nada_encolado_no_hace_nada(self):
        from routers import tab1
        arrancados = []
        self.cola.set_run_fn(lambda sid: arrancados.append(sid))
        await tab1.reanudar_cola()
        await asyncio.sleep(0.1)
        self.assertEqual(arrancados, [])

    async def test_descarta_los_ids_cuya_sesion_ya_no_existe(self):
        """Borrar una sesión no tocaba `queue_state.json`."""
        from routers import tab1
        arrancados = []

        async def _run(sid):
            arrancados.append(sid)

        self.cola.set_run_fn(_run)
        self.sesion("viva", "queued")
        self.cola._queue.extend([_rip("fantasma"), _rip("viva")])

        await tab1.reanudar_cola()
        for _ in range(60):
            await asyncio.sleep(0.05)
            if arrancados:
                break
        self.assertNotIn("fantasma",
                         [t.clave for t in self.cola._queue] + arrancados)
        self.assertEqual(arrancados, ["viva"])


if __name__ == "__main__":
    unittest.main()
