"""
La UI lee el plan del backend, no lo re-deriva.

El trust efectivo aparecía **once veces** en `app.js`, y en dos variantes
sintácticas distintas (`s.trust_override !== 'force_interactive'` y
`(s.trust_override || 'auto') !== ...`), lo que delata que se fue copiando.
El drop-in y el "target necesita merge" estaban replicados otras cinco
veces. Todas son reglas que viven en `phases/cmv40_strategy.py`.

Una réplica de una regla del backend se desincroniza en silencio, y de esa
familia era el bug del overlay (2026-08-19): la UI decidiendo por su cuenta
sobre estado que el servidor ya sabía.

Ahora `session.plan` llega resuelto y `app.js` lo lee con tres helpers. El
fallback local sigue existiendo —el plan no está en sesiones cacheadas de
antes, en el summary del sidebar ni en el modal de creación— pero es UNA
implementación, no once.
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
    marca = f"function {nombre}("
    i = JS.index(marca)
    return JS[i:JS.index("\n}\n", i) + 3]


@unittest.skipUnless(NODE, "node no disponible")
class TestHelpersDelPlan(unittest.TestCase):
    """Evalúa los helpers reales de app.js en node."""

    HELPERS = ("_cmv40Plan", "_cmv40Trust", "_cmv40DropIn",
               "_cmv40SkipSyncReview", "_cmv40TargetNeedsMerge")

    def evaluar(self, fn: str, session: dict):
        script = "".join(_extraer(h) for h in self.HELPERS) + (
            f"\nprocess.stdout.write(JSON.stringify({fn}(JSON.parse(process.argv[1]))));")
        r = subprocess.run([NODE, "-e", script, json.dumps(session)],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[:400]}")
        return json.loads(r.stdout)

    # ── el plan del backend manda ────────────────────────────────────

    def test_el_plan_tiene_prioridad_sobre_la_derivacion_local(self):
        # Sesión cuyos campos sueltos dirían "trusted" pero el plan dice que
        # no: gana el plan, que es quien decide en el pipeline.
        s = {"plan": {"trust_effective": False, "drop_in": False,
                      "target_needs_merge": True},
             "target_trust_ok": True, "trust_override": "auto",
             "target_type": "trusted_p7_fel_final", "source_workflow": "p7_fel"}
        self.assertFalse(self.evaluar("_cmv40Trust", s))
        self.assertFalse(self.evaluar("_cmv40DropIn", s))
        self.assertTrue(self.evaluar("_cmv40TargetNeedsMerge", s))

    def test_lee_los_tres_valores_del_plan(self):
        s = {"plan": {"trust_effective": True, "drop_in": True,
                      "target_needs_merge": False}}
        self.assertTrue(self.evaluar("_cmv40Trust", s))
        self.assertTrue(self.evaluar("_cmv40DropIn", s))
        self.assertFalse(self.evaluar("_cmv40TargetNeedsMerge", s))

    # ── el fallback, para cuando el plan no está ─────────────────────

    def test_sin_plan_deriva_igual_que_el_backend(self):
        from phases.cmv40_strategy import WorkflowInputs, plan_for
        combinaciones = [
            ("p7_fel", "trusted_p7_fel_final", True, "auto"),
            ("p7_fel", "trusted_p7_fel_final", True, "force_interactive"),
            ("p7_fel", "trusted_p8_source", False, "auto"),
            ("p7_mel", "trusted_p7_mel_final", True, "auto"),
            ("p8", "generic", True, "auto"),
            ("p8", "trusted_p8_source", False, "force_interactive"),
        ]
        for wf, tt, trust, ov in combinaciones:
            with self.subTest(wf=wf, target=tt, trust=trust, override=ov):
                s = {"target_trust_ok": trust, "trust_override": ov,
                     "target_type": tt, "source_workflow": wf}
                esperado = plan_for(WorkflowInputs(wf, tt, trust, ov))
                self.assertEqual(self.evaluar("_cmv40Trust", s),
                                 esperado.inputs.trust_effective)
                self.assertEqual(self.evaluar("_cmv40DropIn", s),
                                 esperado.inputs.drop_in)
                self.assertEqual(self.evaluar("_cmv40TargetNeedsMerge", s),
                                 esperado.inputs.target_needs_merge)

    def test_sin_override_asume_auto(self):
        # Sesiones antiguas pueden no traer el campo; el default del modelo es
        # "auto", así que la ausencia no debe apagar el trust.
        s = {"target_trust_ok": True, "target_type": "trusted_p7_fel_final",
             "source_workflow": "p7_fel"}
        self.assertTrue(self.evaluar("_cmv40Trust", s))
        self.assertTrue(self.evaluar("_cmv40DropIn", s))


class TestLaReglaDeSaltarLaRevisionNoVuelveADivergir(unittest.TestCase):
    """`skip_sync_review` es la regla que las dos partes tenían distinta.

    El backend hacía `trusted_auto or user_acked`: con el ACK dado se saltaba
    la Fase D aunque el usuario hubiera pedido revisión manual. El frontend
    exigía además que no hubiera `force_interactive`. Se unificó en esta
    última lectura, y aquí se comprueba que el helper de JS y la tabla de
    Python siguen dando lo mismo en las ocho combinaciones.
    """

    def evaluar(self, session: dict):
        script = "".join(_extraer(h) for h in
                         ("_cmv40Plan", "_cmv40SkipSyncReview")) + (
            "\nprocess.stdout.write(JSON.stringify("
            "_cmv40SkipSyncReview(JSON.parse(process.argv[1]))));")
        r = subprocess.run([NODE, "-e", script, json.dumps(session)],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[:400]}")
        return json.loads(r.stdout)

    @unittest.skipUnless(NODE, "node no disponible")
    def test_el_fallback_js_coincide_con_la_tabla(self):
        from phases.cmv40_strategy import WorkflowInputs
        for trust_ok in (True, False):
            for acked in (True, False):
                for ov in ("auto", "force_interactive"):
                    with self.subTest(trust_ok=trust_ok, acked=acked, override=ov):
                        s = {"target_trust_ok": trust_ok,
                             "user_acknowledged_degradation": acked,
                             "trust_override": ov,
                             "target_type": "trusted_p8_source",
                             "source_workflow": "p7_fel"}
                        esperado = WorkflowInputs(
                            "p7_fel", "trusted_p8_source", trust_ok, ov,
                            acked).skip_sync_review
                        self.assertEqual(self.evaluar(s), esperado)

    @unittest.skipUnless(NODE, "node no disponible")
    def test_el_plan_del_backend_tiene_prioridad(self):
        s = {"plan": {"skip_sync_review": False},
             "target_trust_ok": True, "trust_override": "auto",
             "user_acknowledged_degradation": True}
        self.assertFalse(self.evaluar(s))

class TestNoQuedanReplicas(unittest.TestCase):
    """Guard: la regla no debe volver a escribirse a mano.

    Es un chequeo de forma a propósito — lo que se vigila es justamente que
    nadie vuelva a copiar la expresión, y eso no se puede comprobar
    ejecutando. El comportamiento lo cubre la clase de arriba.
    """

    # Los tres usos legítimos: los fallbacks de `_cmv40Trust` y
    # `_cmv40SkipSyncReview`, y la predicción de drop-in previa a los gates.
    USOS_LEGITIMOS = 3

    def test_la_regla_no_esta_replicada(self):
        lineas = [l for l in JS.splitlines()
                  if "force_interactive" in l
                  and not l.strip().startswith(("//", "*", "/*"))]
        self.assertLessEqual(
            len(lineas), self.USOS_LEGITIMOS,
            "la regla de trust se ha vuelto a escribir a mano; usa "
            "_cmv40Trust() / _cmv40DropIn(), que leen session.plan:\n  "
            + "\n  ".join(l.strip() for l in lineas))

    def test_los_helpers_existen(self):
        for h in ("_cmv40Plan", "_cmv40Trust", "_cmv40DropIn",
                  "_cmv40SkipSyncReview", "_cmv40TargetNeedsMerge"):
            self.assertIn(f"function {h}(", JS)


if __name__ == "__main__":
    unittest.main()
