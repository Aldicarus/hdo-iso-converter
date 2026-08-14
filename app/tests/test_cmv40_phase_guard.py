"""
Una fase CMv4.0 no puede ejecutarse dos veces por un disparo duplicado.

Bug real ("Robot Salvaje", 2026-08-14): la Fase H corrió dos veces seguidas
—22:22:50→54 y 22:22:57→23:02— y el modal de ejecución apareció y desapareció
varias veces. Dos orquestadores disparan las mismas fases (el auto-pipeline
del backend y `_cmv40MaybeAutoAdvance` del frontend, que reintenta a los 5 s),
y el guard de `_run_cmv40_phase` no los frenaba:

  if lock.locked(): return      ← check
  async with lock:              ← adquisición (hay un await entre medias)

Entre el check y la adquisición hay un punto de suspensión, así que dos
disparos simultáneos ven el lock libre, pasan el check y el segundo se ENCOLA
en el lock: al terminar el primero, ejecuta la fase otra vez.

Cubre las dos capas del arreglo:
  1. `_cmv40_phases_in_flight` — descarta el disparo concurrente (atómico).
  2. Guard de "trabajo ya hecho" — descarta el disparo que llega tarde.
"""
import asyncio
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


class TestPhaseGuard(unittest.IsolatedAsyncioTestCase):
    """Usa el _run_cmv40_phase real con una sesión en memoria."""

    def setUp(self):
        self.main = main
        self.saved = {}
        # storage en memoria: evita tocar /config en los tests
        self._orig_load = main.load_cmv40_session
        self._orig_save = main.save_cmv40_session
        self._orig_save_async = main._save_cmv40_session_async
        self._orig_log = main._cmv40_log
        main.load_cmv40_session = lambda sid: self.saved.get(sid)
        main.save_cmv40_session = lambda s: self.saved.__setitem__(s.id, s.model_copy(deep=True))

        async def _fake_save_async(s):
            self.saved[s.id] = s.model_copy(deep=True)

        async def _fake_log(s, msg):
            self.logs.append(msg)

        main._save_cmv40_session_async = _fake_save_async
        main._cmv40_log = _fake_log
        self.logs = []
        main._cmv40_phases_in_flight.clear()
        main._cmv40_phase_locks.clear()

    def tearDown(self):
        self.main.load_cmv40_session = self._orig_load
        self.main.save_cmv40_session = self._orig_save
        self.main._save_cmv40_session_async = self._orig_save_async
        self.main._cmv40_log = self._orig_log
        self.main._cmv40_phases_in_flight.clear()

    def _session(self, phase="remuxed"):
        from models import CMv40Session
        s = CMv40Session(
            id="sX", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="out.mkv", artifacts_dir="/tmp/sX", phase=phase,
        )
        self.saved[s.id] = s.model_copy(deep=True)
        return s

    async def test_dos_disparos_simultaneos_ejecutan_la_fase_una_vez(self):
        from models import CMv40Phase
        session = self._session("remuxed")
        runs = []

        async def _coro(log_cb, proc_cb):
            runs.append(1)
            await asyncio.sleep(0.05)      # simula trabajo con await dentro

        await asyncio.gather(
            self.main._run_cmv40_phase(session, "validate", _coro, CMv40Phase.DONE),
            self.main._run_cmv40_phase(session, "validate", _coro, CMv40Phase.DONE),
        )
        self.assertEqual(len(runs), 1, "la fase debe ejecutarse UNA sola vez")

    async def test_disparo_tardio_no_repite_la_fase(self):
        from models import CMv40Phase
        session = self._session("remuxed")
        runs = []

        async def _coro(log_cb, proc_cb):
            runs.append(1)

        # Primera ejecución: completa y deja phase=done
        await self.main._run_cmv40_phase(session, "validate", _coro, CMv40Phase.DONE)
        self.assertEqual(len(runs), 1)
        self.assertEqual(self.saved["sX"].phase, CMv40Phase.DONE)

        # Segundo disparo (el frontend reintenta con estado viejo): no repite
        await self.main._run_cmv40_phase(session, "validate", _coro, CMv40Phase.DONE)
        self.assertEqual(len(runs), 1, "un disparo tardío no debe repetir la fase")
        self.assertTrue(any("omitida" in m for m in self.logs),
                        "debe dejar constancia en el log de que se omitió")

    async def test_la_fase_E_si_puede_repetirse(self):
        # correct_sync usa new_phase = fase actual porque las correcciones de
        # sync son acumulativas: el guard no puede bloquearla.
        session = self._session("extracted")
        runs = []

        async def _coro(log_cb, proc_cb):
            runs.append(1)

        for _ in range(3):
            await self.main._run_cmv40_phase(
                session, "correct_sync", _coro, "extracted")
        self.assertEqual(len(runs), 3, "Fase E debe poder aplicarse varias veces")

    async def test_rehacer_tras_reset_vuelve_a_ejecutar(self):
        from models import CMv40Phase
        session = self._session("remuxed")
        runs = []

        async def _coro(log_cb, proc_cb):
            runs.append(1)

        await self.main._run_cmv40_phase(session, "validate", _coro, CMv40Phase.DONE)
        # El botón "Rehacer" retrocede el estado (reset-to) antes de relanzar.
        session.phase = CMv40Phase.REMUXED
        self.saved["sX"].phase = CMv40Phase.REMUXED
        await self.main._run_cmv40_phase(session, "validate", _coro, CMv40Phase.DONE)
        self.assertEqual(len(runs), 2, "tras un reset la fase debe re-ejecutarse")

    async def test_una_fase_con_error_previo_puede_reintentarse(self):
        from models import CMv40Phase
        session = self._session("remuxed")
        runs = []

        async def _coro(log_cb, proc_cb):
            runs.append(1)

        await self.main._run_cmv40_phase(session, "validate", _coro, CMv40Phase.DONE)
        # Estado terminal pero con error: el usuario reintenta desde la UI.
        self.saved["sX"].error_message = "fallo transitorio"
        await self.main._run_cmv40_phase(session, "validate", _coro, CMv40Phase.DONE)
        self.assertEqual(len(runs), 2, "con error persistido debe permitirse reintentar")

    async def test_el_set_se_libera_aunque_la_fase_falle(self):
        from models import CMv40Phase
        session = self._session("injected")

        async def _boom(log_cb, proc_cb):
            raise RuntimeError("fallo simulado")

        await self.main._run_cmv40_phase(session, "remux", _boom, CMv40Phase.REMUXED)
        self.assertNotIn("sX", self.main._cmv40_phases_in_flight,
                         "el guard debe liberarse en el finally")


if __name__ == "__main__":
    unittest.main()
