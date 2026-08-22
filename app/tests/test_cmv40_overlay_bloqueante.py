"""
El overlay de ejecución no debe tapar un panel con el que hay que interactuar.

`.cmv40-running-overlay` es `position:fixed; inset:0` con `z-index:2000`:
mientras está puesto **se come cualquier clic** sobre el panel. Por eso la
condición que decide mostrarlo no es cosmética — si se pinta cuando el
pipeline está esperando una decisión del usuario, deja botones que se ven
pero no se pueden pulsar.

Caso real (2026-08-19, "The Mandalorian and Grogu"): Fase B terminó con el
gate `l6_div` pendiente de ACK. Como la condición sólo trataba como
"parado" los estados done/error/preflight, y `recentRunning` seguía activo
tras la fase recién acabada, el overlay tapó el banner ámbar. El usuario
pulsó "Continuar igualmente", el clic se lo quedó el overlay y —al no haber
POST— tampoco hubo toast de error. El pipeline quedó bloqueado y hubo que
lanzar cada fase a mano.

Se evalúan las funciones reales extraídas de `app.js` en node.
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

NODE = shutil.which("node")
sys.path.insert(0, str(APP_DIR / "tests"))
from frontend_sources import js_completo  # noqa: E402

JS = js_completo()


def _extraer(nombre: str) -> str:
    """Saca una función top-level de app.js por nombre."""
    marca = f"function {nombre}("
    i = JS.index(marca)
    j = JS.index("\n}\n", i) + 3
    return JS[i:j]


@unittest.skipUnless(NODE, "node no disponible")
class OverlayCase(unittest.TestCase):
    """Evalúa `_cmv40ShouldShowOverlay` con las entradas de cada escenario."""

    def should_show(self, session: dict, project: dict) -> bool:
        script = (
            _extraer("_cmv40PipelineHalted")
            + "\n"
            + _extraer("_cmv40ShouldShowOverlay")
            + "\n"
            + "const [s, p] = JSON.parse(process.argv[1]);\n"
            + "process.stdout.write(JSON.stringify(_cmv40ShouldShowOverlay(s, p)));"
        )
        out = subprocess.run(
            [NODE, "-e", script, json.dumps([session, project])],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            raise AssertionError(f"node falló: {out.stderr[:400]}")
        return json.loads(out.stdout)

    # Un proyecto que acaba de terminar una fase y sigue encadenando.
    ENCADENANDO = {"autoContinue": True, "autoChaining": True,
                   "lastRunningPhaseAt": None}


class TestPausePoints(OverlayCase):
    """Si el pipeline espera al usuario, el panel tiene que ser operable."""

    def test_gates_pendientes_de_ack_destapan_el_panel(self):
        # La regresión: el banner ámbar lleva los botones "Cambiar target" y
        # "Continuar igualmente"; con el overlay encima no se pueden pulsar.
        s = {"running_phase": None, "phase": "target_provided",
             "awaiting_critical_ack": True}
        self.assertFalse(self.should_show(s, self.ENCADENANDO))

    def test_preflight_detenido_destapa_el_panel(self):
        s = {"running_phase": None, "phase": "created",
             "preflight_decision": "keep_l8_default"}
        self.assertFalse(self.should_show(s, self.ENCADENANDO))

    def test_error_destapa_el_panel(self):
        s = {"running_phase": None, "phase": "injected",
             "error_message": "mkvmerge falló (código -15)"}
        self.assertFalse(self.should_show(s, self.ENCADENANDO))

    def test_job_terminado_destapa_el_panel(self):
        s = {"running_phase": None, "phase": "done"}
        self.assertFalse(self.should_show(s, self.ENCADENANDO))

    def test_ack_pendiente_manda_sobre_una_fase_recien_acabada(self):
        # `recentRunning` era justo lo que mantenía el overlay puesto en el
        # momento en que el banner aparece.
        import time
        s = {"running_phase": None, "phase": "target_provided",
             "awaiting_critical_ack": True}
        p = {"autoContinue": True, "autoChaining": False,
             "lastRunningPhaseAt": time.time() * 1000}   # hace un instante
        self.assertFalse(self.should_show(s, p))


class TestOverlayCuandoTocaMostrarlo(OverlayCase):
    """El puente entre fases sigue funcionando: no hay que reintroducir el
    parpadeo que estas heurísticas venían a evitar."""

    def test_fase_en_curso_siempre_muestra_el_overlay(self):
        s = {"running_phase": "inject", "phase": "sync_verified"}
        self.assertTrue(self.should_show(s, {"autoContinue": False,
                                             "autoChaining": False,
                                             "lastRunningPhaseAt": None}))

    def test_fase_en_curso_manda_aunque_haya_ack_pendiente(self):
        # Con una fase corriendo no hay nada que pulsar, y ocultar el overlay
        # dejaría al usuario sin el log en vivo.
        s = {"running_phase": "remux", "phase": "injected",
             "awaiting_critical_ack": True}
        self.assertTrue(self.should_show(s, self.ENCADENANDO))

    def test_puente_entre_fases_mantiene_el_overlay(self):
        s = {"running_phase": None, "phase": "extracted"}
        self.assertTrue(self.should_show(s, self.ENCADENANDO))

    def test_puente_por_fase_recien_acabada(self):
        import time
        s = {"running_phase": None, "phase": "extracted"}
        p = {"autoContinue": True, "autoChaining": False,
             "lastRunningPhaseAt": time.time() * 1000}
        self.assertTrue(self.should_show(s, p))

    def test_sin_auto_pipeline_no_hay_puente(self):
        s = {"running_phase": None, "phase": "extracted"}
        p = {"autoContinue": False, "autoChaining": True,
             "lastRunningPhaseAt": None}
        self.assertFalse(self.should_show(s, p))

    def test_pasados_los_15s_sin_encadenar_se_destapa(self):
        import time
        s = {"running_phase": None, "phase": "extracted"}
        p = {"autoContinue": True, "autoChaining": False,
             "lastRunningPhaseAt": (time.time() - 60) * 1000}
        self.assertFalse(self.should_show(s, p))


class TestElOverlayTapaDeVerdad(unittest.TestCase):
    """El motivo por el que lo anterior importa: si el CSS dejara de cubrir
    la pantalla, esto sería cosmético. Mientras siga así, no lo es."""

    def test_cubre_la_pantalla_y_captura_los_clics(self):
        css = (APP_DIR / "static" / "style.css").read_text(encoding="utf-8")
        i = css.index(".cmv40-running-overlay {")
        bloque = css[i:css.index("}", i)]
        self.assertIn("position: fixed", bloque)
        self.assertIn("inset: 0", bloque)
        self.assertIn("z-index: 2000", bloque)
        self.assertNotIn("pointer-events: none", bloque,
                         "si dejara pasar los clics, taparlo sería inocuo")


if __name__ == "__main__":
    unittest.main()
