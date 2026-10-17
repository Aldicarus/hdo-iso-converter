"""La confianza en un bin sale de la SEVERIDAD de sus gates, no de `critical`.

`target_trust_ok` es lo que decide saltarse la Fase D y, en un source P7 FEL
con bin `trusted_p7_fel_final`, inyectar el bin entero (drop-in) en vez de
mezclar sus niveles CMv4.0 sobre el RPU del disco. CLAUDE.md lo define como
«true sólo si todos los gates son ok/warn», pero el código lo calculaba con
el campo `critical`, y los gates L6 y L1 llevaban `critical: False` SIEMPRE.
Dos consecuencias, las dos verificadas leyendo el código:

1. **Un L6 o un L1 que piden ACK no quitaban la confianza.** El bin quedaba
   `trusted` mientras esperaba la confirmación, y tras el ACK el job podía ir
   por drop-in: el L1 del bin —gradeado para OTRO máster, que es justo lo que
   el ACK reconoce— acababa sobre este vídeo. Por merge se conserva el del
   disco.
2. **Un L5 de 5 a 30 px no mandaba a la Fase D.** Su severidad decía
   `sync_review` («la Fase D puede revisarlo») con `ok: True`, y
   `hay_sync_por_revisar` exige un gate sin pasar. Decidido con el usuario el
   2026-10-17: manda CLAUDE.md, se revisa el sync.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_confianza_de_gates -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from models import DoviInfo  # noqa: E402
from phases.cmv40_pipeline import _evaluate_trust_gates  # noqa: E402
from phases.cmv40_strategy import (  # noqa: E402
    WorkflowInputs, gates_dan_confianza, hay_sync_por_revisar, resolve_plan,
)

from cmv40_harness import PhaseTestCase, make_session  # noqa: E402


def _info(**kw):
    base = dict(profile=7, el_type="FEL", rpu_present=True,
                l1_max_cll=1000.0, l6_max_cll=1000,
                l5_top=276, l5_bottom=276, l5_left=0, l5_right=0)
    base.update(kw)
    return DoviInfo(**base)


def _evaluar(*, frames=(1000, 1000), src=None, tgt=None):
    src = _info(cm_version="v2.9", **(src or {}))
    tgt = _info(cm_version="v4.0", has_l8=True, **(tgt or {}))
    return _evaluate_trust_gates(src, tgt, *frames)


class TestLaConfianzaSaleDeLaSeveridad(unittest.TestCase):

    def test_todo_ok_da_confianza(self):
        gates, trust = _evaluar()
        self.assertTrue(trust)
        self.assertTrue(gates_dan_confianza(gates))

    def test_un_l6_que_pide_ack_la_quita(self):
        gates, trust = _evaluar(tgt={"l6_max_cll": 4000})
        self.assertEqual(gates["l6_div"]["severity"], "ack_required")
        self.assertFalse(trust, "un bin que pide ACK por L6 se daba por fiable")

    def test_un_l1_que_pide_ack_la_quita(self):
        gates, trust = _evaluar(tgt={"l1_max_cll": 300.0})
        self.assertEqual(gates["l1_div"]["severity"], "ack_required")
        self.assertFalse(trust, "un bin que pide ACK por L1 se daba por fiable")

    def test_un_aviso_no_la_quita(self):
        """`warn` es informativo: un L6 a 100 nits o un L1 a un 10 %."""
        gates, trust = _evaluar(tgt={"l6_max_cll": 1100, "l1_max_cll": 900.0})
        self.assertEqual(gates["l6_div"]["severity"], "warn")
        self.assertEqual(gates["l1_div"]["severity"], "warn")
        self.assertTrue(trust)

    def test_un_l6_que_no_se_puede_contrastar_no_la_quita(self):
        gates, trust = _evaluar(src={"l6_max_cll": 0})
        self.assertTrue(gates["l6_div"].get("incomparable"))
        self.assertTrue(trust)

    def test_un_desfase_de_frames_la_quita(self):
        _, trust = _evaluar(frames=(1000, 998))
        self.assertFalse(trust)

    def test_el_gate_que_la_rompe_se_marca_critico(self):
        """El chip «crítico» de la card de validaciones sale de `critical`:
        si el gate rompe la confianza, lo dice."""
        gates, _ = _evaluar(tgt={"l6_max_cll": 4000, "l1_max_cll": 300.0})
        self.assertTrue(gates["l6_div"]["critical"])
        self.assertTrue(gates["l1_div"]["critical"])
        gates, _ = _evaluar(tgt={"l6_max_cll": 1100})
        self.assertFalse(gates["l6_div"]["critical"])

    def test_vale_tambien_para_gates_persistidos_sin_severidad(self):
        """Proyectos anteriores a `severity`: manda `ok`."""
        self.assertFalse(gates_dan_confianza({"frames": {"ok": False}}))
        self.assertTrue(gates_dan_confianza({"frames": {"ok": True}}))
        self.assertTrue(gates_dan_confianza({}))


class TestElL5DeCincoATreintaPideRevisarElSync(unittest.TestCase):

    def test_es_un_sync_review_que_no_pasa(self):
        gates, trust = _evaluar(tgt={"l5_top": 288, "l5_bottom": 288})
        g = gates["l5_div"]
        self.assertEqual(g["severity"], "sync_review")
        self.assertFalse(g["ok"])
        self.assertFalse(trust)
        self.assertTrue(hay_sync_por_revisar(gates),
                        "un L5 de 12 px no mandaba a revisar el sync")

    def test_hasta_cinco_px_pasa(self):
        gates, trust = _evaluar(tgt={"l5_top": 281})
        self.assertEqual(gates["l5_div"]["severity"], "ok")
        self.assertTrue(gates["l5_div"]["ok"])
        self.assertTrue(trust)

    def test_la_fase_d_no_se_salta_ni_con_el_ack(self):
        gates, trust = _evaluar(tgt={"l5_top": 288, "l6_max_cll": 4000})
        s = make_session(Path("/tmp/no-se-usa"), target_trust_gates=gates,
                         target_trust_ok=trust,
                         user_acknowledged_degradation=True)
        self.assertFalse(WorkflowInputs.from_session(s).skip_sync_review)
        self.assertFalse(resolve_plan(s).inputs.skip_sync_review)


class TestTrasElAckElBinNoVaPorDropIn(unittest.TestCase):
    """El riesgo concreto de que el ACK no quitara la confianza."""

    def _sesion(self, **tgt):
        gates, trust = _evaluar(tgt=tgt)
        return make_session(Path("/tmp/no-se-usa"), source_workflow="p7_fel",
                            target_type="trusted_p7_fel_final",
                            target_trust_gates=gates, target_trust_ok=trust,
                            user_acknowledged_degradation=True)

    def test_con_l1_divergente_va_por_merge(self):
        plan = resolve_plan(self._sesion(l1_max_cll=300.0))
        self.assertFalse(plan.drop_in,
                         "el L1 de otro máster se inyectaría sobre este vídeo")
        self.assertTrue(plan.inject.needs_merge)

    def test_el_ack_si_salta_la_fase_d(self):
        """Lo que el ACK sigue haciendo: la Fase D no arregla un grading."""
        plan = resolve_plan(self._sesion(l6_max_cll=4000))
        self.assertTrue(plan.inputs.skip_sync_review)

    def test_con_los_gates_bien_sigue_siendo_drop_in(self):
        gates, trust = _evaluar()
        s = make_session(Path("/tmp/no-se-usa"), source_workflow="p7_fel",
                         target_type="trusted_p7_fel_final",
                         target_trust_gates=gates, target_trust_ok=trust)
        self.assertTrue(resolve_plan(s).drop_in)


class TestLosProyectosYaEvaluadosSeCorrigenAlCargar(unittest.TestCase):
    """Un proyecto evaluado con la regla de antes, todavía antes de la Fase C:
    la ruta no se ha decidido, así que se corrige al cargar. Después no se
    toca, porque re-encaminarlo dejaría a la Fase F sin sus entradas."""

    GATES_VIEJOS = {
        "frames": {"ok": True, "critical": True, "severity": "ok"},
        "l5_div": {"ok": True, "px_max": 12, "critical": True,
                   "severity": "sync_review"},
        "l6_div": {"ok": False, "nits_diff": 3000, "critical": False,
                   "severity": "ack_required"},
    }

    def _cargar(self, phase):
        import copy
        from models import CMv40Session
        s = make_session(Path("/tmp/no-se-usa"), phase=phase,
                         target_trust_gates=copy.deepcopy(self.GATES_VIEJOS),
                         target_trust_ok=True)
        return CMv40Session.model_validate_json(s.model_dump_json())

    def test_antes_de_la_fase_c_pierde_la_confianza(self):
        s = self._cargar("target_provided")
        self.assertFalse(s.target_trust_ok)
        self.assertFalse(s.target_trust_gates["l5_div"]["ok"])
        self.assertFalse(resolve_plan(s).drop_in)

    def test_desde_la_fase_c_no_se_toca(self):
        s = self._cargar("extracted")
        self.assertTrue(s.target_trust_ok)
        self.assertTrue(s.target_trust_gates["l5_div"]["ok"])


class TestLaFaseBLoCalculaAsi(PhaseTestCase):
    """Lo mismo, ejecutando el análisis del bin de la Fase B con el
    `dovi_tool` falso: que la sesión salga sin confianza depende de que
    `_analyze_target_rpu` use la regla, no solo de que la regla exista."""

    async def _analizar(self, **props):
        from phases.cmv40_pipeline import _analyze_target_rpu
        rpu = self.wd / "RPU_target.bin"
        rpu.write_bytes(b"\x00" * 64)
        props.setdefault("l5", (276, 276, 0, 0))
        self.tb.define_rpu("RPU_target.bin", profile=7, el_type="FEL",
                           cm_version="v4.0", has_l8=True, frames=1000, **props)
        s = make_session(self.wd, target_type="", target_trust_ok=False,
                         source_dv_info=_info(cm_version="v2.9"),
                         target_trust_gates={})
        await _analyze_target_rpu(s, rpu, self.log)
        return s

    async def test_un_bin_coherente_es_de_confianza(self):
        s = await self._analizar()
        self.assertEqual(s.target_type, "trusted_p7_fel_final")
        self.assertTrue(s.target_trust_ok, s.target_trust_gates)

    async def test_un_l6_divergente_no(self):
        s = await self._analizar(l6_max_cll=4000)
        self.assertEqual(s.target_trust_gates["l6_div"]["severity"], "ack_required")
        self.assertFalse(s.target_trust_ok)

    async def test_un_l5_de_doce_px_no(self):
        s = await self._analizar(l5=(288, 288, 0, 0))
        self.assertEqual(s.target_trust_gates["l5_div"]["severity"], "sync_review")
        self.assertFalse(s.target_trust_ok)


if __name__ == "__main__":
    unittest.main()
