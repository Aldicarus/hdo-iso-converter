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
    """El criterio es la MAGNITUD de los trims L8, no cuántos combos hay.

    Recalibrado el 2026-09-18 sobre 40 bins del repo DoviTools (18 retail,
    22 generados) más dos pares controlados del mismo título. Lo que se
    midió, y por qué el conteo de combos no vale:

        generados por análisis .......  maxΔ  0 – 30
        másters con colorista ........  maxΔ  126 – 2.046

    Dogma trae 2 combos con maxΔ 606 (retail) y un generado trae 2 combos
    con maxΔ 0. Contando combos se perdían 5 de 18 retail. Y el % de
    frames neutros tampoco separa: hay retail al 81 % y generados al 33 %.

    El umbral (50) es el `L8_REAL_MINIMAL_SIGNIFICANT_DELTA` que ya estaba
    calibrado; lo que cambia es que pasa de rama secundaria a criterio.
    """

    def _make(self, *, l8_count=0, neutral_pct=0.0, cmv40_frames=100,
              has_mid_contrast=False, has_clip_trim=False,
              delta=0, mid_contrast=None, clip_trim=None) -> RpuAnalysis:
        """`delta` es la desviación del trim respecto al neutro: lo que decide."""
        a = RpuAnalysis()
        a.l8_unique_count = l8_count
        a.l8_neutral_pct = neutral_pct
        a.frames_with_cmv40 = cmv40_frames
        a.l8_has_mid_contrast = has_mid_contrast
        a.l8_has_clip_trim = has_clip_trim
        a.l8_combos = [
            L8Combo(target_display_index=1,
                    trim_slope=2048 + (delta if i == 0 else 0),
                    trim_offset=2048, trim_power=2048,
                    trim_chroma_weight=2048, trim_saturation_gain=2048,
                    ms_weight=0,
                    target_mid_contrast=mid_contrast, clip_trim=clip_trim,
                    occurrence_count=cmv40_frames)
            for i in range(max(0, l8_count))
        ]
        return a

    # ── el bin trae trims de colorista ──────────────────────────────

    def test_real_cuando_los_trims_se_apartan_del_neutro(self):
        a = self._make(l8_count=69, neutral_pct=0.30, delta=200)
        cls, _ = classify_l8(a)
        self.assertEqual(cls, "real")

    def test_el_caso_posesion_infernal_dos_combos_y_trims_fuertes(self):
        """Retail con 2 combos y `power −184`, `sat −328` en 158.942 frames.

        Con el criterio viejo (combos <= 2 -> sintético) se descartaba, y
        es un máster con trabajo real. Es el caso que motivó recalibrar.
        """
        a = self._make(l8_count=2, delta=328)
        self.assertEqual(classify_l8(a)[0], "real")

    def test_el_caso_dogma_dos_combos_y_delta_606(self):
        a = self._make(l8_count=2, delta=606, clip_trim=1442)
        self.assertEqual(classify_l8(a)[0], "real")

    def test_real_con_mid_contrast_poblado(self):
        """`mid_contrast` y `clip_trim` son CMv4.0-only y el análisis no los
        rellena: 11 de 18 retail los traen, 1 de 22 generados."""
        a = self._make(l8_count=3, has_mid_contrast=True, mid_contrast=2121)
        self.assertEqual(classify_l8(a)[0], "real")

    # ── el bin NO trae trims de colorista ───────────────────────────

    def test_default_cuando_los_trims_son_neutros(self):
        """El caso de los generados: haya los combos que haya, si no se
        apartan del neutro es lo que `cm_analyze` haría sobre tu disco."""
        a = self._make(l8_count=2, delta=0)
        self.assertEqual(classify_l8(a)[0], "default")

    def test_default_con_muchos_combos_pero_todos_pegados_al_neutro(self):
        a = self._make(l8_count=400, delta=0)
        self.assertEqual(classify_l8(a)[0], "default")

    def test_una_desviacion_por_debajo_del_umbral_no_cuenta(self):
        """El generado más fuerte medido se queda en 30; el retail más flojo
        con L8 real, en 126. El umbral de 50 cae en medio."""
        self.assertEqual(classify_l8(self._make(l8_count=2, delta=30))[0], "default")
        self.assertEqual(classify_l8(self._make(l8_count=2, delta=126))[0], "real")

    def test_default_sin_bloques_cmv40(self):
        a = self._make(l8_count=0, cmv40_frames=0)
        self.assertEqual(classify_l8(a)[0], "default")

    def test_avatar_fire_and_ash_un_combo_enteramente_neutro(self):
        """Retail que la regla descarta, y con razón: su L8 no tiene trims."""
        a = self._make(l8_count=1, delta=0)
        self.assertEqual(classify_l8(a)[0], "default")

    # ── el contrato ────────────────────────────────────────────────

    def test_solo_hay_DOS_veredictos(self):
        """«Indeterminate» se retiró: no era accionable. A un usuario no se
        le puede pedir que decida sobre un bin que la app no sabe clasificar."""
        vistos = set()
        for combos in (0, 1, 2, 3, 10, 400):
            for d in (0, 30, 51, 600):
                vistos.add(classify_l8(self._make(l8_count=combos, delta=d))[0])
        self.assertEqual(vistos, {"real", "default"})

    def test_ms_weight_no_cuenta_como_trim(self):
        """Su neutro es 0, no 2048. Incluirlo daba un combo neutro por
        trabajado y dejaba `l8_neutral_pct` a 0 % en bins enteramente
        neutros — el bug que destapó Evil Dead Burn."""
        from phases.rpu_analyze import _is_l8_neutral
        self.assertTrue(_is_l8_neutral((1, 2048, 2048, 2048, 2048, 2048, 0, None, None)))
        self.assertTrue(_is_l8_neutral((1, 2048, 2048, 2048, 2048, 2048, 2048, None, None)))
        self.assertFalse(_is_l8_neutral((1, 2048, 2048, 1864, 2048, 1720, 0, None, None)))

    def test_l3_no_decide_nada(self):
        """57 % de acierto sobre 40 bins —azar— y mediana MAYOR en los
        generados. El mid tone offset lo produce el análisis de Dolby."""
        pobre = self._make(l8_count=2, delta=0)
        pobre.l3_unique_count, pobre.l3_frames = 2583, 150000
        self.assertEqual(classify_l8(pobre)[0], "default")
        rico = self._make(l8_count=2, delta=328)
        rico.l3_unique_count, rico.l3_frames = 1, 88
        self.assertEqual(classify_l8(rico)[0], "real")



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

    def test_neutral_mid_contrast_clip_not_flagged(self):
        # audit #14: target_mid_contrast/clip_trim PRESENTES pero a 2048 (neutro)
        # no son trabajo del colorista — antes activaban el flag → tier [CMv4
        # FULL] inflado. Ahora el flag sólo se activa con valor != 2048.
        f = {
            "vdr_dm_data": {
                "cmv40_metadata": {
                    "ext_metadata_blocks": [
                        {"Level8": {
                            "target_display_index": 1,
                            "trim_slope": 2272, "trim_offset": 2048, "trim_power": 2048,
                            "trim_chroma_weight": 2048, "trim_saturation_gain": 2048,
                            "ms_weight": 2048,
                            "target_mid_contrast": 2048, "clip_trim": 2048,  # neutros
                        }}
                    ]
                }
            }
        }
        path = _write_json([f] * 50)
        try:
            res = _parse_export(path)
            self.assertFalse(res.l8_has_mid_contrast)
            self.assertFalse(res.l8_has_clip_trim)
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
        """Un análisis que `classify_l8` da por «real».

        El tier solo se calcula sobre un bin ya clasificado como real, y
        desde la recalibración eso exige trims que se aparten del neutro —
        no basta con el conteo de combos. Por eso el fixture trae un combo
        con desviación: sin él, `classify_l8` diría «default» y el tier
        saldría vacío, que es lo que rompió estos cinco tests al cambiar
        el criterio.
        """
        a = RpuAnalysis()
        a.l8_unique_count = l8_count
        a.l8_neutral_pct = neutral_pct
        a.frames_with_cmv40 = 100000
        a.scene_cuts = scene_cuts
        a.l8_has_mid_contrast = has_mid_contrast
        a.l8_has_clip_trim = has_clip_trim
        a.l8_combos = [L8Combo(
            target_display_index=1, trim_slope=2048 + 300, trim_offset=2048,
            trim_power=2048, trim_chroma_weight=2048, trim_saturation_gain=2048,
            ms_weight=0,
            target_mid_contrast=2121 if has_mid_contrast else None,
            clip_trim=1901 if has_clip_trim else None,
            occurrence_count=100000)]
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
        # Lo que la matriz mira: el tipo que le puso el pre-flight y los
        # gates que evaluó la Fase B. Sin ellos el bin es `generic` → merge.
        s.target_type = "trusted_p7_fel_final"
        s.target_trust_ok = True
        s.target_trust_gates = {"frames": {"ok": True, "critical": True}}
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


class TestLaRecomendacionNoSeInventaLaRuta(unittest.TestCase):
    """La ruta la decide `cmv40_strategy`; aquí solo se consulta.

    `recommend_action` la tenía replicada con otras reglas: perfil
    coincidente en las TRES combinaciones (FEL/FEL, MEL/MEL, P8/P8) más L2
    idéntico, sin mirar `target_type` ni los trust gates. El drop-in de la
    matriz exige otra cosa —`p7_fel` + `trusted_p7_fel_final` + trust
    efectivo— así que la card prometía «~30 segundos» a jobs que acababan
    haciendo el merge completo.

    Medido sobre el `/config` del NAS: **10 de los 41 proyectos con
    recomendación**, y los 8 que llegaron al final salieron con
    `output_workflow=restore_merge`. Dos familias: 7 con source P7 MEL (el
    drop-in es exclusivo de FEL) y 3 con FEL/FEL y los gates caídos.
    """

    def _sesion(self, *, wf, tipo, trust, gates=True, override="auto"):
        s = CMv40Session(id="t", source_mkv_path="/tmp/x.mkv",
                         source_mkv_name="x.mkv", output_mkv_name="x.mkv")
        s.target_preflight_ok = True
        s.preflight_decision = "ok"
        s.source_workflow = wf
        s.source_l2_combos = [_make_l2(2081, slope=2000)]
        s.source_l2_unique_count = 1
        s.target_l2_combos = [_make_l2(2081, slope=2000)]   # idéntico
        s.target_type = tipo
        s.target_trust_ok = trust
        s.trust_override = override
        el = "MEL" if wf == "p7_mel" else "FEL"
        perfil = 8 if wf == "p8" else 7
        s.target_dv_info = DoviInfo(profile=perfil, el_type=("" if perfil == 8 else el),
                                    cm_version="v4.0", frame_count=100)
        if gates:
            s.target_trust_gates = {"frames": {"ok": True, "critical": True}}
        return s

    def test_p7_mel_no_es_drop_in_aunque_coincida_todo(self):
        """El caso de 7 de los 10: MEL↔MEL, L2 idéntico y gates OK."""
        s = self._sesion(wf="p7_mel", tipo="trusted_p7_mel_final", trust=True)
        accion, label, motivo = recommend_action(s)
        self.assertEqual(accion, "merge")
        self.assertNotIn("30 segundos", motivo)
        self.assertIn("P7 FEL", motivo)

    def test_un_bin_fel_con_los_gates_caidos_va_por_merge(self):
        """El caso de los otros 3: el bug literal de no mirar los gates."""
        s = self._sesion(wf="p7_fel", tipo="trusted_p7_fel_final", trust=False)
        s.target_trust_gates = {
            "frames": {"ok": True, "critical": True},
            "l5_div": {"ok": False, "critical": True},
        }
        accion, _, motivo = recommend_action(s)
        self.assertEqual(accion, "merge")
        self.assertIn("l5_div", motivo)

    def test_pedir_revision_manual_tambien_quita_la_ruta_rapida(self):
        s = self._sesion(wf="p7_fel", tipo="trusted_p7_fel_final", trust=True,
                         override="force_interactive")
        accion, _, motivo = recommend_action(s)
        self.assertEqual(accion, "merge")
        self.assertIn("manual", motivo.lower())

    def test_antes_de_fase_b_se_predice_por_la_estructura(self):
        """Sin gates evaluados, `target_trust_ok` todavía no existe.

        Exigirlo diría «merge» durante toda la Fase A a un job que va a ir
        por drop-in. La predicción usa lo que ya se sabe: el tipo lo puso el
        pre-flight y el workflow, la Fase A.
        """
        s = self._sesion(wf="p7_fel", tipo="trusted_p7_fel_final",
                         trust=False, gates=False)
        self.assertEqual(recommend_action(s)[0], "drop_in")
        # …y en cuanto la Fase B los evalúa, manda el dato real.
        s.target_trust_gates = {"frames": {"ok": False, "critical": True}}
        self.assertEqual(recommend_action(s)[0], "merge")

    def test_la_recomendacion_coincide_con_el_plan_que_se_ejecuta(self):
        """El invariante que evita que vuelvan a divergir.

        Con los gates ya evaluados, `drop_in` de la recomendación y
        `plan.drop_in` tienen que ser el mismo booleano en TODAS las
        combinaciones. Es lo que no se cumplía en 10 proyectos reales.
        """
        from phases.cmv40_strategy import resolve_plan, WORKFLOWS
        tipos = ("trusted_p7_fel_final", "trusted_p7_mel_final",
                 "trusted_p8_source", "generic", "")
        vistos = set()
        for wf in WORKFLOWS:
            for tipo in tipos:
                for trust in (True, False):
                    for override in ("auto", "force_interactive"):
                        s = self._sesion(wf=wf, tipo=tipo, trust=trust,
                                         override=override)
                        accion = recommend_action(s)[0]
                        self.assertIn(accion, ("drop_in", "merge"))
                        self.assertEqual(
                            accion == "drop_in", resolve_plan(s).drop_in,
                            f"wf={wf} tipo={tipo!r} trust={trust} ov={override}")
                        vistos.add(accion)
        self.assertEqual(vistos, {"drop_in", "merge"}, "el barrido no ejercita las dos ramas")


if __name__ == "__main__":
    unittest.main(verbosity=2)
