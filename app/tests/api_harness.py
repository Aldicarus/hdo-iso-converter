"""
Arnés para ejercitar los endpoints HTTP contra un /config temporal.

Los 37 endpoints de CMv4.0 no tenían ninguna prueba de request/response:
nadie comprobaba qué status devuelven, qué guards aplican ni qué se
persiste. Eso los hace intocables — mover un endpoint de sitio es a ciegas.

`main.py` resuelve sus directorios y ejecuta los `_recover_interrupted_*`
**en el import**, y solo se puede importar una vez por proceso. Por eso el
arnés no toca variables de entorno (llegaría tarde si otro test importó
main antes): parchea los directorios ya resueltos, que es el mismo patrón
que usa `cmv40_harness` con `OUTPUT_DIR`.

Uso:

    class TestAlgo(ApiTestCase):
        def test_x(self):
            sid = self.crear_sesion(phase="injected")
            r = self.client.post(f"/api/cmv40/{sid}/remux")
            self.assertEqual(r.status_code, 409)
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def _load_app():
    """Importa la app una sola vez y devuelve (módulo del router, TestClient).

    El TestClient se construye sobre `main.app`, pero lo que los tests
    parchean vive en `routers.cmv40` desde que los endpoints salieron de
    main.py — por eso se devuelve ese módulo y no `main`.
    """
    from fastapi.testclient import TestClient
    import main
    from routers import cmv40 as cmv40_routes
    return main, cmv40_routes, TestClient(main.app)


class ApiTestCase(unittest.TestCase):
    """Cliente HTTP contra la app real, con /config y /mnt aislados."""

    @classmethod
    def setUpClass(cls):
        cls.main, cls.cmv40, cls.client = _load_app()

    def setUp(self):
        import storage
        from phases import cmv40_pipeline as pipeline

        self.tmp = Path(tempfile.mkdtemp(prefix="api_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config_dir = self.tmp / "config"
        self.cmv40_dir = self.config_dir / "cmv40"
        self.output_dir = self.tmp / "output"
        self.work_base = self.tmp / "work"
        for d in (self.config_dir, self.cmv40_dir, self.output_dir, self.work_base):
            d.mkdir(parents=True, exist_ok=True)

        # Los directorios están resueltos como constantes de módulo en varios
        # sitios; hay que redirigirlos todos o los tests escribirían en el
        # /config real (o fallarían por no existir en el Mac).
        # Cada constante donde de verdad vive. El router referencia el output
        # como `_cmv40_pipeline_mod.OUTPUT_DIR`, así que parchear el módulo del
        # pipeline le llega; `main.OUTPUT_DIR_MKV` sigue siendo la de Tab 1/2.
        self._parches = [
            (storage, "CONFIG_DIR", self.config_dir),
            (storage, "CMV40_DIR", self.cmv40_dir),
            (storage, "MKV_AUDIT_DIR", self.config_dir / "mkv_audits"),
            (self.main, "CONFIG_DIR", self.config_dir),
            (self.main, "OUTPUT_DIR_MKV", self.output_dir),
            (pipeline, "OUTPUT_DIR", self.output_dir),
            (pipeline, "CMV40_WORK_BASE", self.work_base),
        ]
        self._originales = [(mod, attr, getattr(mod, attr, None))
                            for mod, attr, _ in self._parches]
        for mod, attr, nuevo in self._parches:
            setattr(mod, attr, nuevo)
        self.addCleanup(self._restaurar)

        # Caches en memoria que sobreviven entre tests y verían ficheros de
        # otro tmpdir.
        for nombre in ("_sessions_summary_by_file", "_cmv40_summary_by_file"):
            cache = getattr(storage, nombre, None)
            if isinstance(cache, dict):
                cache.clear()

        # Los endpoints de fase hacen `asyncio.create_task(...)` y devuelven al
        # instante. Si se dejara correr de verdad, el TestClient se quedaría
        # colgado al cerrar esperando esa tarea (y encima lanzaría ffmpeg).
        # El espía la sustituye: registra qué fase se pidió arrancar, que es
        # justo lo que un test de endpoint quiere comprobar.
        self.fases_lanzadas: list[dict] = []

        async def _espia(session, phase_name, coro_factory, new_phase):
            self.fases_lanzadas.append({
                "session_id": session.id,
                "phase": phase_name,
                "new_phase": new_phase,
                # Se guarda para poder ejecutarlo con el pipeline mockeado:
                # el nombre de la fase y la función que la ejecuta se pasan
                # por separado, así que sin esto una tabla con las filas
                # cruzadas (`inject` → run_phase_g_remux) pasaría el test.
                "coro_factory": coro_factory,
            })

        self._orig_run_phase = self.cmv40._run_cmv40_phase
        self.cmv40._run_cmv40_phase = _espia
        self.addCleanup(
            lambda: setattr(self.cmv40, "_run_cmv40_phase", self._orig_run_phase))


    def mockear_runners(self) -> list[str]:
        """Sustituye todos los `run_phase_*` del pipeline por falsos.

        Hay que llamarlo ANTES de la petición: `_cmv40_dispatch_phase`
        resuelve el runner con `getattr` al despachar, así que el `_coro`
        que queda guardado ya lleva la referencia capturada.

        Devuelve la lista donde se anotan los runners que se ejecuten.
        """
        import phases.cmv40_pipeline as pipeline

        llamados: list[str] = []
        for nombre in [n for n in dir(pipeline) if n.startswith("run_phase_")]:
            original = getattr(pipeline, nombre)

            async def _falso(*a, _n=nombre, **k):
                llamados.append(_n)

            setattr(pipeline, nombre, _falso)
            self.addCleanup(
                lambda n=nombre, o=original: setattr(pipeline, n, o))
        return llamados

    def ejecutar_fase_lanzada(self) -> None:
        """Corre el `coro_factory` que el endpoint dejó preparado."""
        import asyncio

        async def _noop_log(*a, **k):
            return None

        asyncio.run(self.fase_lanzada()["coro_factory"](_noop_log, lambda *a: None))

    def fase_lanzada(self):
        """La única fase que se pidió arrancar. Falla si hay 0 o más de una."""
        if len(self.fases_lanzadas) != 1:
            raise AssertionError(
                f"se esperaba 1 fase lanzada, hay {len(self.fases_lanzadas)}: "
                f"{self.fases_lanzadas}")
        return self.fases_lanzadas[0]

    def _restaurar(self):
        for mod, attr, original in self._originales:
            setattr(mod, attr, original)

    # ── construcción de estado ───────────────────────────────────────

    def crear_sesion(self, sid: str = "cmv40_Peli_2024_1700000000", **campos):
        """Escribe una CMv40Session en el /config temporal."""
        import storage
        from models import CMv40Session

        base = {
            "id": sid,
            "source_mkv_path": str(self.tmp / "source.mkv"),
            "source_mkv_name": "source.mkv",
            "output_mkv_name": "Peli (2024).mkv",
            "artifacts_dir": str(self.work_base / sid),
            "source_frame_count": 1000,
            "target_frame_count": 1000,
        }
        base.update(campos)
        s = CMv40Session(**base)
        Path(s.artifacts_dir).mkdir(parents=True, exist_ok=True)
        storage.save_cmv40_session(s)
        return sid

    def leer_sesion(self, sid: str):
        import storage
        return storage.load_cmv40_session(sid)
