"""El overlay de ejecución de CMv4.0 ya no existe, y no debe volver.

Qué era
───────
`.cmv40-running-overlay` era `position:fixed; inset:0` con `z-index:2000`, se
pintaba **solo** en cuanto una fase arrancaba y tapaba el panel del proyecto
entero. Mientras estaba puesto **se comía cualquier clic**, así que la
condición que decidía mostrarlo no era cosmética: si se pintaba con el pipeline
esperando una decisión del usuario, dejaba botones que se veían pero no se
podían pulsar.

Caso real (2026-08-19, «The Mandalorian and Grogu»): Fase B terminó con el gate
`l6_div` pendiente de ACK. Como la condición sólo trataba como «parado» los
estados done/error/preflight, y `recentRunning` seguía activo tras la fase
recién acabada, el overlay tapó el banner ámbar. El usuario pulsó «Continuar
igualmente», el clic se lo quedó el overlay y —al no haber POST— tampoco hubo
toast de error. El pipeline quedó bloqueado y hubo que lanzar cada fase a mano.

Por qué se fue
──────────────
Se le fueron añadiendo excepciones —done, error, preflight detenido, ACK
pendiente, y dos heurísticas (`autoChaining` y `recentRunning`) para que no
parpadeara en el puente entre fases— y aun así seguía tapando cosas. El
problema de fondo era la premisa: **abrir un modal bloqueante por su cuenta**.

Con la columna de trabajo, el progreso se ve sin tapar nada y el detalle se
abre a petición desde un modal común a los cinco tipos de trabajo. Y lo que el
overlay enseñaba de más —la cartela de la película y la timeline de fases— ya
está en el panel, que ahora se ve.

Este fichero es lo que queda: el guardián de que no vuelva. Si alguien
reintroduce un overlay que se abre solo, que lo haga sabiendo esto.
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import js_completo  # noqa: E402

JS = js_completo()


class TestElOverlayNoVuelve(unittest.TestCase):

    def test_no_queda_la_funcion_que_decidia_mostrarlo(self):
        self.assertNotIn("_cmv40ShouldShowOverlay", JS)

    def test_ni_la_que_lo_pintaba(self):
        self.assertNotIn("_renderCMv40RunningOverlay", JS)

    def test_ni_las_dos_heuristicas_del_puente_entre_fases(self):
        """`recentRunning` y `lastRunningPhaseAt` existían SOLO para que el
        overlay no parpadeara entre una fase y la siguiente. Sin overlay no
        tienen nada que sostener, y `recentRunning` fue justo la que mantuvo
        tapado el banner de ACK."""
        self.assertNotIn("recentRunning", JS)
        self.assertNotIn("lastRunningPhaseAt", JS)

    def test_el_flag_del_auto_pipeline_SI_se_queda(self):
        """`_autoChaining` no era del overlay: distingue «el usuario abrió el
        proyecto» de «la cadena está avanzando», que es lo que impide que abrir
        un proyecto en `created` arranque la Fase A sola (bug del 2026-09-04)."""
        self.assertIn("_autoChaining", JS)

    def test_nadie_pinta_un_overlay_que_tape_el_panel(self):
        """La clase sigue en el CSS —limpiarlo es otra pasada— pero nada la
        usa. Que reaparezca en el JS es la señal de alarma."""
        self.assertNotIn("cmv40-running-overlay", JS)


class TestLoQueLoSustituye(unittest.TestCase):
    """El detalle se abre a petición, y es el mismo para los cinco tipos."""

    def test_hay_un_modal_comun_y_cmv40_registra_el_suyo(self):
        self.assertIn("registrarDetalleDeTrabajo('cmv40'", JS)
        self.assertIn("function abrirDetalleDeTrabajo(", JS)

    def test_el_panel_conserva_la_cartela_y_la_timeline(self):
        """Eran lo único que el overlay enseñaba de más, y siguen ahí — ahora
        visibles, porque ya no hay nada encima."""
        self.assertIn("cmv40-running-timeline", JS)
        self.assertIn("cmv40-tl-header", JS)


if __name__ == "__main__":
    unittest.main()
