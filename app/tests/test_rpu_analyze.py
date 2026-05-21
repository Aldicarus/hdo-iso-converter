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
)


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
        a = self._make(l8_count=5, neutral_pct=0.80)
        cls, reason = classify_l8(a)
        self.assertEqual(cls, "indeterminate")
        self.assertIn("límite", reason.lower())


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
