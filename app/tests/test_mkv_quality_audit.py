"""Tests de la auditoría de calidad RPU del Tab 2.

Cubre la lógica de verdict (color + texto + tier) y los provenance hints
para todos los caminos del classifier:
  - CMv4.0 real FULL / CORE+ / CORE / minimal
  - CMv4.0 default (sintético)
  - CMv4.0 indeterminate
  - CMv2.9 puro (sin L8) en sus 3 tiers
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.mkv_analyze import (  # noqa: E402
    _build_quality_audit_from_rpu_analysis,
    _compute_provenance_hints,
)
from phases.rpu_analyze import RpuAnalysis  # noqa: E402
from models import L8Combo  # noqa: E402


def _make_rpu(*, l8=0, l2=0, neutral=0.0, cmv40_frames=100, scene_cuts=0,
              mid_c=False, clip=False, l2_pqs=None, l2_combos=0) -> RpuAnalysis:
    a = RpuAnalysis()
    a.l8_unique_count = l8
    a.l2_unique_count = l2
    a.l8_neutral_pct = neutral
    a.frames_with_cmv40 = cmv40_frames
    a.total_frames = 1000
    a.scene_cuts = scene_cuts
    a.l8_has_mid_contrast = mid_c
    a.l8_has_clip_trim = clip
    a.l2_target_pqs = l2_pqs or []
    a.l2_combos = [None] * l2_combos if l2_combos else []
    # Para "real minimal" — al menos un combo con delta significativo
    if mid_c or clip:
        a.l8_combos = [L8Combo(
            target_display_index=1, trim_slope=2200,  # +152 del neutro
            trim_offset=2048, trim_power=2048, trim_chroma_weight=2048,
            trim_saturation_gain=2048, ms_weight=2048,
            target_mid_contrast=2121 if mid_c else None,
            clip_trim=2503 if clip else None,
            occurrence_count=10,
        )]
    return a


class TestVerdictCMv4(unittest.TestCase):

    def test_full_master_green(self):
        rpu = _make_rpu(l8=2547, neutral=0.11, scene_cuts=1487,
                        mid_c=True, clip=True)
        out = _build_quality_audit_from_rpu_analysis(rpu, False)
        self.assertEqual(out["quality_classification"], "real")
        self.assertEqual(out["quality_tier"], "full")
        self.assertEqual(out["quality_verdict_color"], "green")
        self.assertIn("FULL", out["quality_tier_label"])

    def test_core_rich_green(self):
        rpu = _make_rpu(l8=1119, neutral=0.001, scene_cuts=2617)
        out = _build_quality_audit_from_rpu_analysis(rpu, False)
        self.assertEqual(out["quality_tier"], "core_rich")
        self.assertEqual(out["quality_verdict_color"], "green")
        self.assertIn("CORE+", out["quality_tier_label"])

    def test_core_yellow(self):
        rpu = _make_rpu(l8=69, neutral=0.30, scene_cuts=2887)
        out = _build_quality_audit_from_rpu_analysis(rpu, False)
        self.assertEqual(out["quality_tier"], "core")
        self.assertEqual(out["quality_verdict_color"], "yellow")

    def test_default_red(self):
        rpu = _make_rpu(l8=1, neutral=1.0, cmv40_frames=100)
        out = _build_quality_audit_from_rpu_analysis(rpu, False)
        self.assertEqual(out["quality_classification"], "default")
        self.assertEqual(out["quality_verdict_color"], "red")
        self.assertIn("sintético", out["quality_verdict_text"].lower())

    def test_indeterminate_gray(self):
        rpu = _make_rpu(l8=5, neutral=0.80, cmv40_frames=100)
        out = _build_quality_audit_from_rpu_analysis(rpu, False)
        self.assertEqual(out["quality_classification"], "indeterminate")
        self.assertEqual(out["quality_verdict_color"], "gray")


class TestVerdictCMv29(unittest.TestCase):

    def test_native_master_green(self):
        rpu = _make_rpu(l2=73, l2_pqs=[62, 2081, 2851, 3079])
        out = _build_quality_audit_from_rpu_analysis(rpu, True)
        self.assertEqual(out["quality_verdict_color"], "green")
        self.assertIn("nativo", out["quality_verdict_text"].lower())
        self.assertEqual(out["quality_classification"], "real")

    def test_core_streaming_yellow(self):
        rpu = _make_rpu(l2=15, l2_pqs=[100, 1000])
        out = _build_quality_audit_from_rpu_analysis(rpu, True)
        self.assertEqual(out["quality_verdict_color"], "yellow")

    def test_minimal_red(self):
        rpu = _make_rpu(l2=3, l2_pqs=[1000])
        out = _build_quality_audit_from_rpu_analysis(rpu, True)
        self.assertEqual(out["quality_verdict_color"], "red")
        self.assertEqual(out["quality_classification"], "default")


class TestProvenanceHints(unittest.TestCase):

    def test_native_recent_master_with_l11_l254(self):
        rpu = _make_rpu(l8=2547, neutral=0.11, scene_cuts=1487,
                        mid_c=True, clip=True)
        flags = {"has_l4": False, "has_l9": True, "has_l10": True,
                 "has_l11": True, "has_l254": True}
        hints = _compute_provenance_hints(rpu, "real", "full", flags, False)
        self.assertTrue(any("nativo CMv4.0 reciente" in h for h in hints))
        self.assertTrue(any("Metadata DV completa" in h for h in hints))

    def test_synthetic_bin_no_l4(self):
        rpu = _make_rpu(l8=1, neutral=1.0, cmv40_frames=100)
        flags = {"has_l4": False, "has_l11": False}
        hints = _compute_provenance_hints(rpu, "default", "", flags, False)
        self.assertTrue(any("sintético" in h.lower() for h in hints))
        self.assertTrue(any("Sin L11" in h for h in hints))

    def test_converted_bin_with_l4(self):
        rpu = _make_rpu(l8=2, neutral=0.96, cmv40_frames=100)
        flags = {"has_l4": True, "has_l11": False}
        hints = _compute_provenance_hints(rpu, "default", "", flags, False)
        self.assertTrue(any("convertido" in h.lower() for h in hints))

    def test_cmv4_pre_l11_master(self):
        rpu = _make_rpu(l8=2547, neutral=0.11, scene_cuts=1487, mid_c=True)
        flags = {"has_l4": False, "has_l9": True, "has_l10": True,
                 "has_l11": False, "has_l254": True}
        hints = _compute_provenance_hints(rpu, "real", "full", flags, False)
        self.assertTrue(any("pre-L11" in h or "pre-IQ" in h for h in hints))

    def test_cmv29_native_with_l2(self):
        rpu = _make_rpu(l2=73, l2_pqs=[62, 2081, 2851, 3079])
        flags = {"has_l4": False}
        hints = _compute_provenance_hints(rpu, "real", "", flags, True)
        self.assertTrue(any("CMv2.9 puro" in h for h in hints))
        self.assertTrue(any("trabajado por colorista" in h for h in hints))

    def test_cmv29_with_l4_compat(self):
        rpu = _make_rpu(l2=15, l2_pqs=[100, 1000])
        flags = {"has_l4": True}
        hints = _compute_provenance_hints(rpu, "real", "", flags, True)
        self.assertTrue(any("L4 presente" in h for h in hints))

    def test_no_flags_no_hints(self):
        """Si dv_flags vacío, no peta y devuelve hints solo basados en classifier."""
        rpu = _make_rpu(l8=1, neutral=1.0, cmv40_frames=100)
        hints = _compute_provenance_hints(rpu, "default", "", {}, False)
        # Siempre hay al menos 1 (default → "Bin sintético" o "Bin convertido")
        self.assertTrue(len(hints) >= 1)


class TestBuilderIntegratesHints(unittest.TestCase):

    def test_builder_with_flags_emits_hints(self):
        rpu = _make_rpu(l8=2547, neutral=0.11, scene_cuts=1487,
                        mid_c=True, clip=True)
        flags = {"has_l9": True, "has_l10": True, "has_l11": True,
                 "has_l254": True}
        out = _build_quality_audit_from_rpu_analysis(rpu, False, dv_flags=flags)
        self.assertIn("quality_provenance_hints", out)
        self.assertTrue(len(out["quality_provenance_hints"]) >= 1)

    def test_builder_without_flags_empty_hints(self):
        rpu = _make_rpu(l8=2547, neutral=0.11, scene_cuts=1487, mid_c=True, clip=True)
        out = _build_quality_audit_from_rpu_analysis(rpu, False)
        self.assertIn("quality_provenance_hints", out)
        # Con tier=full + classification=real, sin flags, no hay hints (todos
        # los hints CMv4.0 dependen de has_l*).
        self.assertEqual(out["quality_provenance_hints"], [])


class TestCancelByAuditId(unittest.TestCase):
    """Cancel viejo no debe afectar a un audit nuevo lanzado tras él.

    Reproduce el bug "cancelar audit + relanzar inmediatamente → el segundo
    audit falla con 'Cancelado por el usuario'" mediante una secuencia
    determinista sobre los singletons de estado.

    El import de main.py necesita cwd en app/ porque la app monta
    StaticFiles("static") con path relativo.
    """

    @classmethod
    def setUpClass(cls):
        import os
        cls._orig_cwd = os.getcwd()
        app_dir = Path(__file__).parent.parent  # .../app
        os.chdir(str(app_dir))

    @classmethod
    def tearDownClass(cls):
        import os
        os.chdir(cls._orig_cwd)

    def setUp(self):
        from main import (
            _mkv_quality_state, _mkv_quality_cancel,
            _mkv_quality_reset, _mkv_quality_check_cancel,
        )
        self.state = _mkv_quality_state
        self.cancel = _mkv_quality_cancel
        self.reset = _mkv_quality_reset
        self.check = _mkv_quality_check_cancel
        # Reset clean state
        self.state.update({"active": False, "audit_id": None})
        self.cancel["requested_for_id"] = None

    def test_check_cancel_fires_when_targeted(self):
        """Cancel del audit actual → _check raise."""
        audit_id = self.reset(file_name="movie.mkv")
        self.cancel["requested_for_id"] = audit_id
        with self.assertRaises(RuntimeError):
            self.check()

    def test_check_cancel_silent_when_obsolete(self):
        """Cancel de un audit anterior → _check NO raise sobre el nuevo audit."""
        first_id = self.reset(file_name="first.mkv")
        # Simula: cancel del primer audit marca requested_for_id=first_id
        self.cancel["requested_for_id"] = first_id
        # Usuario relanza → reset asigna audit_id nuevo y limpia el viejo
        second_id = self.reset(file_name="second.mkv")
        self.assertNotEqual(first_id, second_id)
        # En el endpoint nuevo, reset limpia requested_for_id (idempotencia)
        self.assertIsNone(self.cancel.get("requested_for_id"))
        # _check no debe disparar para el nuevo audit
        self.check()  # no raise

    def test_check_cancel_silent_when_no_cancel_pending(self):
        """Sin cancel pendiente, _check es no-op."""
        self.reset(file_name="movie.mkv")
        self.check()  # no raise

    def test_log_skipped_when_audit_id_obsolete(self):
        """Si _mkv_quality_log se llama con target_audit_id de un audit
        anterior, la línea NO debe añadirse al state.log_lines (que pertenece
        al audit actual). Sin esto, el except del primer endpoint en race
        contra el reset del segundo audit metía 'Cancelado por el usuario'
        en el log del audit nuevo."""
        from main import _mkv_quality_log
        first_id = self.reset(file_name="first.mkv")
        # Reset al segundo audit (cambia audit_id)
        second_id = self.reset(file_name="second.mkv")
        n_before = len(self.state["log_lines"])
        # Cancel/except del primer audit intenta loguear con target=first_id
        _mkv_quality_log("contaminación del primer audit", target_audit_id=first_id)
        # NO debe aparecer en el log del segundo audit
        self.assertEqual(len(self.state["log_lines"]), n_before)

    def test_log_accepted_when_audit_id_matches(self):
        """Si target_audit_id coincide con el actual, la línea se añade
        normalmente. Caso del flujo legítimo (el audit actual loguea de sí mismo)."""
        from main import _mkv_quality_log
        audit_id = self.reset(file_name="movie.mkv")
        n_before = len(self.state["log_lines"])
        _mkv_quality_log("línea propia", target_audit_id=audit_id)
        self.assertEqual(len(self.state["log_lines"]), n_before + 1)

    def test_log_accepted_when_no_target_passed(self):
        """Sin target_audit_id, comportamiento legacy: siempre añade."""
        from main import _mkv_quality_log
        self.reset(file_name="movie.mkv")
        n_before = len(self.state["log_lines"])
        _mkv_quality_log("línea sin guard")
        self.assertEqual(len(self.state["log_lines"]), n_before + 1)

    def test_finalize_if_skipped_when_audit_obsolete(self):
        """_state_finalize_if NO modifica el state si audit_id ya cambió."""
        from main import _mkv_quality_state_finalize_if
        first_id = self.reset(file_name="first.mkv")
        # Simula que el segundo audit ya empezó
        second_id = self.reset(file_name="second.mkv")
        self.state["active"] = True
        result = _mkv_quality_state_finalize_if(first_id, "obsoleto")
        self.assertFalse(result)
        # El state del segundo audit NO se ha tocado
        self.assertTrue(self.state["active"])
        self.assertNotEqual(self.state.get("error"), "obsoleto")


class TestLightProfileCancelByJobId(unittest.TestCase):
    """Mismo blindaje que TestCancelByAuditId pero para el perfil de luminancia
    (colorimetría). Un cancel de un análisis anterior NO debe matar el nuevo
    lanzado tras él — el cancel va dirigido por job_id."""

    @classmethod
    def setUpClass(cls):
        import os
        cls._orig_cwd = os.getcwd()
        os.chdir(str(Path(__file__).parent.parent))  # .../app

    @classmethod
    def tearDownClass(cls):
        import os
        os.chdir(cls._orig_cwd)

    def setUp(self):
        from main import (
            _light_profile_state, _lp_cancel, _lp_reset, _lp_check_cancel,
        )
        self.state = _light_profile_state
        self.cancel = _lp_cancel
        self.reset = _lp_reset
        self.check = _lp_check_cancel
        self.state.update({"active": False, "job_id": None})
        self.cancel["requested_for_id"] = None

    def test_reset_returns_job_id_and_clears_cancel(self):
        """reset() devuelve un job_id, lo fija en el state y limpia el cancel."""
        # Simula un cancel pendiente del análisis anterior
        self.cancel["requested_for_id"] = "viejo123"
        job_id = self.reset()
        self.assertTrue(job_id)
        self.assertEqual(self.state["job_id"], job_id)
        self.assertIsNone(self.cancel.get("requested_for_id"))

    def test_check_cancel_fires_when_targeted(self):
        """Cancel del análisis actual → _check raise."""
        job_id = self.reset()
        self.cancel["requested_for_id"] = job_id
        with self.assertRaises(RuntimeError):
            self.check()

    def test_check_cancel_silent_when_obsolete(self):
        """Cancel del análisis anterior → _check NO raise sobre el nuevo."""
        first_id = self.reset()
        self.cancel["requested_for_id"] = first_id   # cancel del 1º (tardío)
        second_id = self.reset()                      # usuario relanza
        self.assertNotEqual(first_id, second_id)
        self.assertIsNone(self.cancel.get("requested_for_id"))
        self.check()  # no raise — el cancel viejo no afecta al nuevo

    def test_check_cancel_silent_when_no_cancel_pending(self):
        """Sin cancel pendiente, _check es no-op."""
        self.reset()
        self.check()  # no raise


if __name__ == "__main__":
    unittest.main()
