"""Tests unitarios para phases.rpu_analyze (Bloque 1 modelo Keep/Restore).

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_rpu_analyze -v

O directamente:
    cd app && python3 -m unittest tests.test_rpu_analyze -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

# Permite ejecutar el test sin instalar el paquete
sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.rpu_analyze import (  # noqa: E402
    RpuAnalysis,
    _parse_export,
    classify_l8,
    classify_l8_quality,
    filename_label_from_tier,
    compare_l2,
    recommend_action,
)
from models import L2Combo, L8Combo, CMv40Session, DoviInfo  # noqa: E402


def _write_json(data) -> Path:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return Path(f.name)


# ── classify_l8 ──────────────────────────────────────────────────────────────

class TestClassifyL8(unittest.TestCase):

    def _make(self, *, l8_count=0, neutral_pct=0.0, cmv40_frames=100,
              has_mid_contrast=False, has_clip_trim=False) -> RpuAnalysis:
        a = RpuAnalysis()
        a.l8_unique_count = l8_count
        a.l8_neutral_pct = neutral_pct
        a.frames_with_cmv40 = cmv40_frames
        a.l8_has_mid_contrast = has_mid_contrast
        a.l8_has_clip_trim = has_clip_trim
        return a

    def test_real_when_many_combos_and_low_neutral(self):
        # Caso típico de los bins reales analizados (Spider-Man: 69 combos, 30% neutros)
        a = self._make(l8_count=69, neutral_pct=0.30)
        cls, reason = classify_l8(a)
        self.assertEqual(cls, "real")
        self.assertIn("69 combos", reason)
        self.assertIn("CORE", reason)

    def test_real_full_when_mid_contrast_populated(self):
        # Caso Smashing Machine: combos altos + mid_contrast/clip_trim poblados
        a = self._make(l8_count=152, neutral_pct=0.001,
                       has_mid_contrast=True, has_clip_trim=True)
        cls, reason = classify_l8(a)
        self.assertEqual(cls, "real")
        self.assertIn("FULL", reason)

    def test_default_when_single_combo(self):
        # Caso patológico: 1 combo único en todos los frames
        a = self._make(l8_count=1, neutral_pct=1.0)
        cls, reason = classify_l8(a)
        self.assertEqual(cls, "default")
        self.assertIn("sintético", reason.lower())

    def test_default_when_all_frames_neutral(self):
        # Aunque haya muchos combos, si >=95% están neutros es default
        a = self._make(l8_count=50, neutral_pct=0.98)
        cls, reason = classify_l8(a)
        self.assertEqual(cls, "default")

    def test_default_when_no_cmv40_blocks(self):
        # Edge: RPU CMv2.9 puro sin nada de CMv4.0
        a = self._make(l8_count=0, neutral_pct=0.0, cmv40_frames=0)
        cls, _ = classify_l8(a)
        self.assertEqual(cls, "default")

    def test_indeterminate_when_in_between(self):
        # Entre los dos umbrales: 5 combos (>= 2 y < 10), neutral 80% (< 95%)
        # Sin mid_c/clip y sin l8_combos rehidratados → no salta a "real minimal"
        a = self._make(l8_count=5, neutral_pct=0.80)
        cls, reason = classify_l8(a)
        self.assertEqual(cls, "indeterminate")
        self.assertIn("límite", reason.lower())

    def test_real_minimal_with_few_combos_and_significant_trim(self):
        # Caso Black Phone 2: 3 combos, mid_c poblado, trims significativos
        # (slope=2165 = +117 del neutro)
        a = self._make(l8_count=3, neutral_pct=0.10, has_mid_contrast=True)
        a.l8_combos = [
            L8Combo(target_display_index=1, trim_slope=2048, trim_offset=2048,
                    trim_power=2048, trim_chroma_weight=2048,
                    trim_saturation_gain=2048, ms_weight=2048,
                    target_mid_contrast=2048, clip_trim=None,
                    occurrence_count=100),
            L8Combo(target_display_index=1, trim_slope=2165, trim_offset=2048,
                    trim_power=2183, trim_chroma_weight=2048,
                    trim_saturation_gain=2066, ms_weight=2048,
                    target_mid_contrast=2121, clip_trim=2503,
                    occurrence_count=50),
        ]
        cls, reason = classify_l8(a)
        self.assertEqual(cls, "real")
        self.assertIn("minimal", reason.lower())
        self.assertIn("mid_contrast", reason)

    def test_real_minimal_with_clip_trim_and_negative_delta(self):
        # Caso Expediente Warren: 5 combos, clip poblado, slope=-276
        a = self._make(l8_count=5, neutral_pct=0.05, has_clip_trim=True)
        a.l8_combos = [
            L8Combo(target_display_index=1, trim_slope=1772, trim_offset=2096,
                    trim_power=2239, trim_chroma_weight=2048,
                    trim_saturation_gain=1843, ms_weight=2048,
                    target_mid_contrast=2048, clip_trim=1901,
                    occurrence_count=200),
        ]
        cls, reason = classify_l8(a)
        self.assertEqual(cls, "real")
        self.assertIn("minimal", reason.lower())
        self.assertIn("clip_trim", reason)

    def test_real_minimal_requires_significant_delta(self):
        # 5 combos + mid_c poblado pero todos los trims a delta ≤ 50 → sigue
        # siendo indeterminate (jitter, no master real)
        a = self._make(l8_count=5, neutral_pct=0.20, has_mid_contrast=True)
        a.l8_combos = [
            L8Combo(target_display_index=1, trim_slope=2080, trim_offset=2050,
                    trim_power=2070, trim_chroma_weight=2048,
                    trim_saturation_gain=2055, ms_weight=2048,
                    target_mid_contrast=2049, clip_trim=None,
                    occurrence_count=100),
        ]
        cls, _ = classify_l8(a)
        self.assertEqual(cls, "indeterminate")

    def test_real_minimal_requires_extra_cmv4_field(self):
        # 5 combos con trims significativos pero sin mid_c ni clip → no es
        # "minimal" (sería un CORE incipiente, dejarlo en indeterminate)
        a = self._make(l8_count=5, neutral_pct=0.20)
        a.l8_combos = [
            L8Combo(target_display_index=1, trim_slope=1800, trim_offset=2048,
                    trim_power=2200, trim_chroma_weight=2048,
                    trim_saturation_gain=2048, ms_weight=2048,
                    target_mid_contrast=None, clip_trim=None,
                    occurrence_count=100),
        ]
        cls, _ = classify_l8(a)
        self.assertEqual(cls, "indeterminate")


# ── _parse_export ────────────────────────────────────────────────────────────

class TestParseExport(unittest.TestCase):

    def test_lista_plana_con_l2_y_l8(self):
        # Simula la estructura "lista plana" devuelta por dovi_tool export
        # Tres frames con el mismo L2/L8: 1 combo único, 0% neutro (slope=1500)
        frame = {
            "vdr_dm_data": {
                "cmv29_metadata": {
                    "ext_metadata_blocks": [
                        {"Level2": {
                            "target_max_pq": 2081, "trim_slope": 1500,
                            "trim_offset": 2048, "trim_power": 2048,
                            "trim_chroma_weight": 2048, "trim_saturation_gain": 2048,
                            "ms_weight": 2048,
                        }}
                    ]
                },
                "cmv40_metadata": {
                    "ext_metadata_blocks": [
                        {"Level8": {
                            "target_display_index": 1,
                            "trim_slope": 2200, "trim_offset": 2048, "trim_power": 2048,
                            "trim_chroma_weight": 2048, "trim_saturation_gain": 2048,
                            "ms_weight": 2048,
                            "target_mid_contrast": None, "clip_trim": None,
                        }}
                    ]
                },
            }
        }
        data = [frame, frame, frame]
        path = _write_json(data)
        try:
            res = _parse_export(path)
            self.assertEqual(res.total_frames, 3)
            self.assertEqual(res.frames_with_cmv40, 3)
            self.assertEqual(res.l2_unique_count, 1)
            self.assertEqual(res.l2_target_pqs, [2081])
            self.assertEqual(res.l8_unique_count, 1)
            self.assertEqual(res.l8_target_indices, [1])
            # 100% trabajado (slope=2200 ≠ 2048) → 0% neutro
            self.assertAlmostEqual(res.l8_neutral_pct, 0.0)
            self.assertFalse(res.l8_has_mid_contrast)
            self.assertFalse(res.l8_has_clip_trim)
        finally:
            path.unlink()

    def test_neutral_l8_se_cuenta(self):
        # 1 frame neutro, 1 trabajado → 50% neutro
        f_neutro = {
            "vdr_dm_data": {
                "cmv40_metadata": {
                    "ext_metadata_blocks": [
                        {"Level8": {
                            "target_display_index": 1,
                            "trim_slope": 2048, "trim_offset": 2048, "trim_power": 2048,
                            "trim_chroma_weight": 2048, "trim_saturation_gain": 2048,
                            "ms_weight": 2048,
                            "target_mid_contrast": None, "clip_trim": None,
                        }}
                    ]
                }
            }
        }
        f_trab = {
            "vdr_dm_data": {
                "cmv40_metadata": {
                    "ext_metadata_blocks": [
                        {"Level8": {
                            "target_display_index": 1,
                            "trim_slope": 1900, "trim_offset": 2048, "trim_power": 2048,
                            "trim_chroma_weight": 2048, "trim_saturation_gain": 2048,
                            "ms_weight": 2048,
                            "target_mid_contrast": None, "clip_trim": None,
                        }}
                    ]
                }
            }
        }
        data = [f_neutro, f_trab]
        path = _write_json(data)
        try:
            res = _parse_export(path)
            self.assertEqual(res.total_frames, 2)
            self.assertEqual(res.l8_unique_count, 2)
            self.assertAlmostEqual(res.l8_neutral_pct, 0.5)
        finally:
            path.unlink()

    def test_mid_contrast_y_clip_trim_detectados(self):
        f = {
            "vdr_dm_data": {
                "cmv40_metadata": {
                    "ext_metadata_blocks": [
                        {"Level8": {
                            "target_display_index": 1,
                            "trim_slope": 2272, "trim_offset": 2048, "trim_power": 2048,
                            "trim_chroma_weight": 2048, "trim_saturation_gain": 2048,
                            "ms_weight": 2048,
                            "target_mid_contrast": 1045, "clip_trim": 2109,  # ¡poblados!
                        }}
                    ]
                }
            }
        }
        path = _write_json([f] * 50)
        try:
            res = _parse_export(path)
            self.assertTrue(res.l8_has_mid_contrast)
            self.assertTrue(res.l8_has_clip_trim)
        finally:
            path.unlink()

    def test_rpu_cmv29_puro_sin_l8(self):
        # RPU CMv2.9 puro: sin bloque cmv40_metadata, solo L2
        f = {
            "vdr_dm_data": {
                "cmv29_metadata": {
                    "ext_metadata_blocks": [
                        {"Level2": {
                            "target_max_pq": 2081, "trim_slope": 2048,
                            "trim_offset": 2048, "trim_power": 2048,
                            "trim_chroma_weight": 2048, "trim_saturation_gain": 2048,
                            "ms_weight": 2048,
                        }}
                    ]
                }
            }
        }
        path = _write_json([f, f])
        try:
            res = _parse_export(path)
            self.assertEqual(res.total_frames, 2)
            self.assertEqual(res.frames_with_cmv40, 0)
            self.assertEqual(res.l2_unique_count, 1)
            self.assertEqual(res.l8_unique_count, 0)
            self.assertAlmostEqual(res.l8_neutral_pct, 0.0)
        finally:
            path.unlink()

    def test_dict_con_clave_rpus(self):
        # Algunos exports vienen como dict {"rpus": [...]}, no lista plana.
        f = {"vdr_dm_data": {"cmv40_metadata": {"ext_metadata_blocks": []}}}
        path = _write_json({"rpus": [f, f, f]})
        try:
            res = _parse_export(path)
            self.assertEqual(res.total_frames, 3)
            self.assertEqual(res.frames_with_cmv40, 3)
        finally:
            path.unlink()

    def test_scene_cuts_se_cuentan(self):
        # 3 frames con scene_refresh_flag set + 2 sin → scene_cuts=3
        f_cut = {"vdr_dm_data": {
            "scene_refresh_flag": 1,
            "cmv40_metadata": {"ext_metadata_blocks": []},
        }}
        f_norm = {"vdr_dm_data": {
            "scene_refresh_flag": 0,
            "cmv40_metadata": {"ext_metadata_blocks": []},
        }}
        path = _write_json([f_cut, f_norm, f_cut, f_norm, f_cut])
        try:
            res = _parse_export(path)
            self.assertEqual(res.scene_cuts, 3)
        finally:
            path.unlink()


# ── classify_l8_quality ──────────────────────────────────────────────────────

class TestClassifyL8Quality(unittest.TestCase):

    def _make_real(self, *, l8_count=64, neutral_pct=0.1, scene_cuts=2000,
                   has_mid_contrast=False, has_clip_trim=False) -> RpuAnalysis:
        a = RpuAnalysis()
        a.l8_unique_count = l8_count
        a.l8_neutral_pct = neutral_pct
        a.frames_with_cmv40 = 100000
        a.scene_cuts = scene_cuts
        a.l8_has_mid_contrast = has_mid_contrast
        a.l8_has_clip_trim = has_clip_trim
        return a

    def test_full_when_mid_contrast_populated(self):
        # Caso Smashing Machine: mid_contrast poblado → FULL
        a = self._make_real(l8_count=152, neutral_pct=0.001,
                            scene_cuts=593, has_mid_contrast=True)
        tier, label, desc = classify_l8_quality(a)
        self.assertEqual(tier, "full")
        self.assertEqual(label, "CMv4 FULL")
        self.assertIn("FULL", desc)
        self.assertIn("target_mid_contrast", desc)

    def test_full_when_clip_trim_populated(self):
        # Bin con clip_trim poblado (sin mid_contrast) también es FULL
        a = self._make_real(l8_count=80, neutral_pct=0.05,
                            scene_cuts=2000, has_clip_trim=True)
        tier, label, _ = classify_l8_quality(a)
        self.assertEqual(tier, "full")
        self.assertEqual(label, "CMv4 FULL")

    def test_core_rich_when_many_combos_per_shot(self):
        # Caso 28 después: 1119/2617 = 0.43 combos/shot → CORE+
        a = self._make_real(l8_count=1119, neutral_pct=0.08, scene_cuts=2617)
        tier, label, desc = classify_l8_quality(a)
        self.assertEqual(tier, "core_rich")
        self.assertEqual(label, "CMv4 CORE+")
        self.assertIn("CORE+", desc)

    def test_core_when_standard_streaming(self):
        # Spider-Man: 69/2887 = 0.024 → CORE estándar
        a = self._make_real(l8_count=69, neutral_pct=0.30, scene_cuts=2887)
        tier, label, desc = classify_l8_quality(a)
        self.assertEqual(tier, "core")
        self.assertEqual(label, "CMv4 CORE")
        self.assertIn("CORE", desc)

    def test_returns_empty_for_default_bins(self):
        # Si classify_l8 devuelve "default" → no aplica quality
        a = RpuAnalysis()
        a.l8_unique_count = 1
        a.l8_neutral_pct = 1.0
        a.frames_with_cmv40 = 100
        tier, label, desc = classify_l8_quality(a)
        self.assertEqual(tier, "")
        self.assertEqual(label, "")
        self.assertEqual(desc, "")

    def test_rich_fallback_when_no_scene_cuts(self):
        # Si scene_cuts=0 (raro) usar umbral absoluto de 400 combos
        a = self._make_real(l8_count=500, neutral_pct=0.1, scene_cuts=0)
        tier, _, _ = classify_l8_quality(a)
        self.assertEqual(tier, "core_rich")

    def test_filename_label_helper(self):
        self.assertEqual(filename_label_from_tier("core"), "CMv4 CORE")
        self.assertEqual(filename_label_from_tier("core_rich"), "CMv4 CORE+")
        self.assertEqual(filename_label_from_tier("full"), "CMv4 FULL")
        self.assertEqual(filename_label_from_tier(""), "")
        self.assertEqual(filename_label_from_tier("unknown"), "")


# ── compare_l2 ───────────────────────────────────────────────────────────────

def _make_l2(pq, slope=2048, off=2048, pow_=2048, chr_=2048, sat=2048, msw=2048):
    return L2Combo(
        target_max_pq=pq, trim_slope=slope, trim_offset=off, trim_power=pow_,
        trim_chroma_weight=chr_, trim_saturation_gain=sat, ms_weight=msw,
        occurrence_count=1,
    )


class TestCompareL2(unittest.TestCase):

    def test_identical_when_same_set(self):
        s = [_make_l2(2081, slope=2000), _make_l2(2851, slope=2100)]
        t = [_make_l2(2081, slope=2000), _make_l2(2851, slope=2100)]
        verdict, reason = compare_l2(s, t)
        self.assertEqual(verdict, "identical")
        self.assertIn("byte-a-byte", reason)

    def test_identical_independente_del_orden(self):
        # El orden de combos en la lista no importa — comparamos como SET
        s = [_make_l2(2081, slope=2000), _make_l2(2851, slope=2100)]
        t = [_make_l2(2851, slope=2100), _make_l2(2081, slope=2000)]
        verdict, _ = compare_l2(s, t)
        self.assertEqual(verdict, "identical")

    def test_identical_independiente_del_occurrence_count(self):
        # occurrence_count distinto pero mismos valores → identical
        a = _make_l2(2081, slope=2000); a.occurrence_count = 100
        b = _make_l2(2081, slope=2000); b.occurrence_count = 50
        verdict, _ = compare_l2([a], [b])
        self.assertEqual(verdict, "identical")

    def test_different_when_value_differs(self):
        s = [_make_l2(2081, slope=2000)]
        t = [_make_l2(2081, slope=1500)]
        verdict, reason = compare_l2(s, t)
        self.assertEqual(verdict, "different")
        self.assertIn("distinto", reason.lower())

    def test_different_when_target_has_extra_combo(self):
        s = [_make_l2(2081)]
        t = [_make_l2(2081), _make_l2(2851)]
        verdict, _ = compare_l2(s, t)
        self.assertEqual(verdict, "different")

    def test_unknown_if_empty(self):
        verdict, reason = compare_l2([], [_make_l2(2081)])
        self.assertEqual(verdict, "unknown")
        self.assertIn("source", reason)


# ── recommend_action ─────────────────────────────────────────────────────────

class TestRecommendAction(unittest.TestCase):

    def _base_session(self) -> CMv40Session:
        s = CMv40Session(
            id="test_id",
            source_mkv_path="/tmp/x.mkv",
            source_mkv_name="x.mkv",
            output_mkv_name="x [CMv4.0].mkv",
        )
        return s

    def test_keep_when_preflight_decision_default(self):
        s = self._base_session()
        s.preflight_decision = "keep_l8_default"
        s.preflight_message = "Bin sintético"
        action, label, reason = recommend_action(s)
        self.assertEqual(action, "keep")
        self.assertIn("Mantener", label)

    def test_keep_when_no_preflight_ok(self):
        s = self._base_session()
        s.target_preflight_ok = False
        action, _, _ = recommend_action(s)
        self.assertEqual(action, "keep")

    def test_unknown_when_phase_a_not_done(self):
        s = self._base_session()
        s.target_preflight_ok = True
        s.preflight_decision = "ok"
        # source_l2_unique_count == 0 → Fase A no ejecutada
        action, label, _ = recommend_action(s)
        self.assertEqual(action, "unknown")
        self.assertIn("pendiente", label.lower())

    def test_drop_in_when_profile_match_and_l2_identical(self):
        s = self._base_session()
        s.target_preflight_ok = True
        s.preflight_decision = "ok"
        s.source_workflow = "p7_fel"
        s.source_l2_combos = [_make_l2(2081, slope=2000)]
        s.source_l2_unique_count = 1
        s.target_l2_combos = [_make_l2(2081, slope=2000)]
        s.target_dv_info = DoviInfo(profile=7, el_type="FEL", cm_version="v4.0", frame_count=100)
        s.target_l8_quality_label = "CMv4 FULL"
        action, label, reason = recommend_action(s)
        self.assertEqual(action, "drop_in")
        self.assertIn("Inyectar RPU", label)
        self.assertIn("rápido", label.lower())
        self.assertIn("idéntico", reason)
        self.assertIn("CMv4 FULL", reason)

    def test_merge_when_profile_mismatch(self):
        s = self._base_session()
        s.target_preflight_ok = True
        s.preflight_decision = "ok"
        s.source_workflow = "p8"
        s.source_l2_combos = [_make_l2(2081, slope=2000)]
        s.source_l2_unique_count = 1
        s.target_l2_combos = [_make_l2(2081, slope=2000)]  # idéntico pero da igual
        s.target_dv_info = DoviInfo(profile=7, el_type="MEL", cm_version="v4.0", frame_count=100)
        s.target_l8_quality_label = "CMv4 CORE"
        action, label, reason = recommend_action(s)
        self.assertEqual(action, "merge")
        self.assertIn("Inyectar RPU", label)
        self.assertIn("preserva", label.lower())
        self.assertIn("no coincide", reason.lower())

    def test_merge_when_l2_differs(self):
        s = self._base_session()
        s.target_preflight_ok = True
        s.preflight_decision = "ok"
        s.source_workflow = "p7_fel"
        s.source_l2_combos = [_make_l2(2081, slope=2000)]
        s.source_l2_unique_count = 1
        s.target_l2_combos = [_make_l2(2081, slope=1500)]  # distinto
        s.target_dv_info = DoviInfo(profile=7, el_type="FEL", cm_version="v4.0", frame_count=100)
        action, label, reason = recommend_action(s)
        self.assertEqual(action, "merge")
        self.assertIn("L2 difiere", reason)
        self.assertIn("preserva", label.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
