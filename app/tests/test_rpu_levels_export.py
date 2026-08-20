"""
El export por niveles produce el mismo análisis que el volcado completo.

`dovi_tool export -d all` vuelca el RPU entero. Medido sobre un bin real
(La trama fenicia, 61 MB / 145.303 frames): **682 MB en ~100 s**, más el
coste de releerlo y parsearlo en Python — varios GB de objetos, en un NAS
que ya tira de swap. Y de todo eso solo usamos L1, L2, L8 y los cortes de
escena.

`export --levels level1,level2,level8 -d scenes` da exactamente lo mismo en
**115 MB y 4 s** (parseo: 1,9 s). Este test fija el contrato del parser
contra el formato real que emite dovi_tool 2.3.3.

Formato JSON y no CSV a propósito: el writer CSV aborta con
"found record with 11 fields, but the previous record has 9" en cuanto un
bloque L8 trae los campos CMv4.0.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))


def _write(d: Path, name: str, payload) -> Path:
    p = d / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class TestParseExportLevels(unittest.TestCase):
    """Muestras copiadas del output real de `dovi_tool 2.3.3 export -f json`."""

    def _analyze(self, l1, l2, l8, scenes=None):
        from phases.rpu_analyze import _parse_export_levels
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            paths = {
                "level1": _write(d, "l1.json", l1),
                "level2": _write(d, "l2.json", l2),
                "level8": _write(d, "l8.json", l8),
                "scenes": _write(d, "scenes.json", scenes if scenes is not None else []),
            }
            return _parse_export_levels(paths)

    def test_censo_de_frames_y_cortes(self):
        l1 = [{"frame": i, "min_pq": 0, "max_pq": 2081, "avg_pq": 819} for i in range(10)]
        a = self._analyze(l1, [], [], scenes=[0, 4, 7])
        self.assertEqual(a.total_frames, 10)
        self.assertEqual(a.scene_cuts, 3)

    def test_combos_l2_se_agrupan_y_cuentan(self):
        l1 = [{"frame": i, "min_pq": 0, "max_pq": 2081, "avg_pq": 819} for i in range(3)]
        base = {"target_max_pq": 2081, "trim_slope": 2019, "trim_offset": 2043,
                "trim_power": 1341, "trim_chroma_weight": 2048,
                "trim_saturation_gain": 2048, "ms_weight": 2048}
        otro = dict(base, target_max_pq=2851, trim_slope=2062)
        l2 = [dict(base, frame=0), dict(base, frame=1), dict(otro, frame=2)]
        a = self._analyze(l1, l2, [])
        self.assertEqual(a.l2_unique_count, 2)
        self.assertEqual(a.l2_combos[0].occurrence_count, 2)  # el repetido primero
        self.assertEqual(a.l2_target_pqs, [2081, 2851])

    def test_l8_neutro_no_cuenta_como_trabajado(self):
        l1 = [{"frame": i, "min_pq": 0, "max_pq": 2081, "avg_pq": 819} for i in range(4)]
        neutro = {"length": 10, "target_display_index": 1, "trim_slope": 2048,
                  "trim_offset": 2048, "trim_power": 2048, "trim_chroma_weight": 2048,
                  "trim_saturation_gain": 2048, "ms_weight": 2048}
        trabajado = dict(neutro, trim_slope=2165)
        l8 = [dict(neutro, frame=0), dict(neutro, frame=1),
              dict(trabajado, frame=2), dict(trabajado, frame=3)]
        a = self._analyze(l1, [], l8)
        self.assertEqual(a.frames_with_cmv40, 4)
        self.assertAlmostEqual(a.l8_neutral_pct, 0.5)
        self.assertEqual(a.l8_target_indices, [1])

    def test_campos_cmv40_solo_cuentan_si_no_son_neutros(self):
        """Un clip_trim presente pero a 2048 no es trabajo del colorista
        (audit #14): inflaba el tier a [CMv4 FULL]."""
        l1 = [{"frame": 0, "min_pq": 0, "max_pq": 2081, "avg_pq": 819}]
        base = {"frame": 0, "length": 13, "target_display_index": 1,
                "trim_slope": 2048, "trim_offset": 2048, "trim_power": 2048,
                "trim_chroma_weight": 2048, "trim_saturation_gain": 2048,
                "ms_weight": 2048}
        a = self._analyze(l1, [], [dict(base, target_mid_contrast=2048, clip_trim=2048)])
        self.assertFalse(a.l8_has_mid_contrast)
        self.assertFalse(a.l8_has_clip_trim)

        a2 = self._analyze(l1, [], [dict(base, target_mid_contrast=2121, clip_trim=2056)])
        self.assertTrue(a2.l8_has_mid_contrast)
        self.assertTrue(a2.l8_has_clip_trim)

    def test_l8_sin_campos_cmv40_no_revienta(self):
        """Los bloques L8 cortos (CORE) no traen mid_contrast ni clip_trim."""
        l1 = [{"frame": 0, "min_pq": 0, "max_pq": 2081, "avg_pq": 819}]
        l8 = [{"frame": 0, "length": 10, "target_display_index": 1,
               "trim_slope": 2048, "trim_offset": 2048, "trim_power": 2048,
               "trim_chroma_weight": 2048, "trim_saturation_gain": 2048,
               "ms_weight": 2048}]
        a = self._analyze(l1, [], l8)
        self.assertEqual(a.l8_unique_count, 1)
        self.assertIsNone(a.l8_combos[0].target_mid_contrast)

    def test_ficheros_ausentes_devuelven_analisis_vacio(self):
        from phases.rpu_analyze import _parse_export_levels
        a = _parse_export_levels({})
        self.assertEqual(a.total_frames, 0)
        self.assertEqual(a.l8_unique_count, 0)

    def test_rpu_cmv29_puro_sin_l8(self):
        l1 = [{"frame": i, "min_pq": 0, "max_pq": 2081, "avg_pq": 819} for i in range(5)]
        a = self._analyze(l1, [], [])
        self.assertEqual(a.frames_with_cmv40, 0)
        self.assertEqual(a.l8_neutral_pct, 0.0)


class TestClasificacionSobreLevels(unittest.TestCase):
    """El clasificador consume el RpuAnalysis sin saber de dónde salió."""

    def test_un_master_full_sigue_clasificando_como_real(self):
        from phases.rpu_analyze import _parse_export_levels, classify_l8
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            l1 = [{"frame": i, "min_pq": 0, "max_pq": 2081, "avg_pq": 819}
                  for i in range(200)]
            # 20 combos distintos, todos con trabajo real + campos CMv4.0
            l8 = [{"frame": i, "length": 13, "target_display_index": 1,
                   "trim_slope": 2048 + (i % 20) * 17, "trim_offset": 2048,
                   "trim_power": 2048, "trim_chroma_weight": 2048,
                   "trim_saturation_gain": 2048, "ms_weight": 2048,
                   "target_mid_contrast": 2121, "clip_trim": 2056}
                  for i in range(200)]
            paths = {
                "level1": _write(d, "l1.json", l1),
                "level2": _write(d, "l2.json", []),
                "level8": _write(d, "l8.json", l8),
                "scenes": _write(d, "scenes.json", list(range(0, 200, 10))),
            }
            a = _parse_export_levels(paths)
            self.assertEqual(a.scene_cuts, 20)
            kind, reason = classify_l8(a)
            self.assertEqual(kind, "real")
            self.assertIn("FULL", reason)



# `TestRpusFromLevels` vivía aquí y cubría `main._rpus_from_levels`, el
# adaptador que RECONSTRUÍA la forma anidada del volcado a partir del export
# por niveles para alimentar a un segundo parser del mismo JSON que vivía en el
# endpoint del light-profile. Ese parser ya no existe: el volcado anidado se
# adapta ahora al formato PLANO (`main._niveles_desde_volcado`) y desemboca en
# un solo consumidor (`main._perfil_desde_niveles`).
#
# La cobertura equivalente —las tres formas que emite `dovi_tool export`, los
# varios bloques del mismo nivel por frame, el sanity check min<=avg<=max— está
# en `test_light_profile_pipe.TestUnSoloParserDelExport`.

if __name__ == "__main__":
    unittest.main()
