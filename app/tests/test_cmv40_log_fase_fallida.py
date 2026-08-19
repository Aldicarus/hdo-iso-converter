"""El cierre de una fase fallida no repite un motivo ya explicado.

Caso real (Annabelle 2014, 2026-08-17): el pre-flight rechaza un bin sin
CMv4.0 escribiendo al log un diagnóstico largo —qué pasa, por qué, qué buscar
en su lugar— y acto seguido lanza ESE MISMO texto como excepción. El handler
genérico lo escribía otra vez entero, así que en pantalla salía el mismo
párrafo dos veces seguidas y parecía que el job había fallado dos veces:

    [08:24:58] [Pre-flight] ⛔ El bin target no aporta CMv4.0 (CM v2.9)…
    [08:24:58] ✗ Fase preflight FALLÓ: El bin target no aporta CMv4.0 (CM v2.9)…

La línea de cierre tiene que seguir estando (es marcador de persistencia y la
usa el frontend para apagar el spinner), pero sin el cuerpo repetido.
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
    """main.py monta StaticFiles('static') con ruta relativa: solo importa
    con el cwd en app/."""
    # El `os.chdir(APP_DIR)` que había aquí era para que
    # `StaticFiles(directory="static")` de main.py encontrara el
    # directorio; desde que se monta por ruta absoluta ya no hace
    # falta cambiar el cwd del proceso de test.
    global cmv40_routes
    from routers import cmv40 as _routes
    cmv40_routes = _routes


# El texto real que emite el pre-flight al rechazar un bin CMv2.9.
MOTIVO = (
    "El bin target no aporta CMv4.0 (CM v2.9). No hay metadata L8-L11 que "
    "transferir al RPU del BD — este pipeline solo puede inyectar CMv4.0 "
    "sobre CMv2.9, no v2.9 sobre v2.9."
)


class TestCierreDeFaseFallida(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from models import CMv40Session
        self.session = CMv40Session(
            id="sess_fallo", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="out.mkv")
        cmv40_routes._cmv40_log_throttle.pop(self.session.id, None)
        self._orig_persist = cmv40_routes._cmv40_maybe_persist_log

        async def _no_persistir(session, line):
            pass

        cmv40_routes._cmv40_maybe_persist_log = _no_persistir

    def tearDown(self):
        cmv40_routes._cmv40_maybe_persist_log = self._orig_persist
        cmv40_routes._cmv40_log_throttle.pop(self.session.id, None)

    async def test_motivo_ya_en_el_log_no_se_repite(self):
        await cmv40_routes._cmv40_log(self.session, f"[Pre-flight] ⛔ {MOTIVO}")
        await cmv40_routes._cmv40_log_phase_failed(self.session, "preflight", MOTIVO)

        veces = sum(1 for l in self.session.output_log if MOTIVO in l)
        self.assertEqual(veces, 1, self.session.output_log)
        self.assertTrue(self.session.output_log[-1].endswith("✗ Fase preflight FALLÓ"),
                        self.session.output_log[-1])

    async def test_motivo_nuevo_si_se_escribe(self):
        """Si la fase murió sin explicarse (excepción cruda de un subprocess),
        la línea de cierre es el único sitio donde el usuario lo va a leer."""
        await cmv40_routes._cmv40_log(self.session, "[Fase C] Ejecutando demux…")
        await cmv40_routes._cmv40_log_phase_failed(self.session, "extract", "dovi_tool rc=137")

        self.assertIn("✗ Fase extract FALLÓ: dovi_tool rc=137",
                      self.session.output_log[-1])

    async def test_motivo_antiguo_si_se_repite(self):
        """Solo se mira la cola del log: un motivo idéntico de hace 50 líneas
        no es el que acaba de explicarse, y callarlo dejaría el fallo mudo."""
        await cmv40_routes._cmv40_log(self.session, f"[Pre-flight] ⛔ {MOTIVO}")
        for i in range(10):
            await cmv40_routes._cmv40_log(self.session, f"[Fase A] línea {i}")
        await cmv40_routes._cmv40_log_phase_failed(self.session, "preflight", MOTIVO)

        self.assertIn(MOTIVO, self.session.output_log[-1])

    async def test_sigue_siendo_marcador_de_persistencia(self):
        """`✗ Fase` fuerza el save inmediato y lo consume el frontend para
        apagar el spinner (regex /✗ Fase \\w+ FALLÓ/)."""
        await cmv40_routes._cmv40_log(self.session, f"[Pre-flight] ⛔ {MOTIVO}")
        await cmv40_routes._cmv40_log_phase_failed(self.session, "preflight", MOTIVO)

        cierre = self.session.output_log[-1]
        self.assertTrue(
            any(cierre.startswith(m) or m in cierre
                for m in cmv40_routes._CMV40_LOG_FORCE_PERSIST_MARKERS),
            cierre)
        import re
        self.assertRegex(cierre, r"✗ Fase \w+ FALLÓ")


class TestNadieEmiteElCierreAMano(unittest.TestCase):
    """Cuatro sitios distintos cerraban la fase fallida con su propio f-string.
    Si vuelve a aparecer uno, la duplicación vuelve solo en esa ruta."""

    def test_todos_pasan_por_el_helper(self):
        # El helper y sus llamantes viven en routers/cmv40.py desde que los
        # endpoints salieron de main.py.
        src = (APP_DIR / "routers" / "cmv40.py").read_text(encoding="utf-8")
        cuerpo_helper = src[src.index("async def _cmv40_log_phase_failed"):]
        cuerpo_helper = cuerpo_helper[:cuerpo_helper.index("\n\n\n")]
        fuera = src.replace(cuerpo_helper, "")
        self.assertNotIn("FALLÓ: {", fuera,
                         "usar _cmv40_log_phase_failed en vez de componer la "
                         "línea a mano")


if __name__ == "__main__":
    unittest.main()
