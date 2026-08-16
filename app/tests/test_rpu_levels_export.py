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



class TestRpusFromLevels(unittest.TestCase):
    """El adaptador debe producir exactamente la forma que espera el parser
    del light-profile, que está validado contra `dovi_tool info --summary`.
    """

    def setUp(self):
        import os
        self._cwd = os.getcwd()
        os.chdir(APP_DIR)
        import main
        self.main = main

    def tearDown(self):
        import os
        os.chdir(self._cwd)

    def test_forma_anidada_que_espera_el_parser(self):
        levels = {
            "level1": [{"frame": 0, "min_pq": 0, "max_pq": 2081, "avg_pq": 819}],
            "level8": [{"frame": 0, "target_display_index": 1, "trim_slope": 2048}],
        }
        rpus = self.main._rpus_from_levels(levels)
        self.assertEqual(len(rpus), 1)
        vdr = rpus[0]["vdr_dm_data"]
        # L1 va a CMv2.9, L8 a CMv4.0
        l1 = vdr["cmv29_metadata"]["ext_metadata_blocks"][0]["Level1"]
        self.assertEqual(l1["max_pq"], 2081)
        l8 = vdr["cmv40_metadata"]["ext_metadata_blocks"][0]["Level8"]
        self.assertEqual(l8["target_display_index"], 1)

    def test_el_parser_real_encuentra_el_l1(self):
        """Prueba de contrato: el parser del endpoint sobre el adaptador."""
        levels = {"level1": [
            {"frame": 0, "min_pq": 10, "max_pq": 3079, "avg_pq": 800},
            {"frame": 1, "min_pq": 12, "max_pq": 2500, "avg_pq": 700},
        ]}
        rpus = self.main._rpus_from_levels(levels)
        self.assertEqual(len(rpus), 2)
        # Réplica de la extracción del endpoint: busca max_pq/avg_pq dentro
        # de vdr_dm_data.cmv29_metadata.ext_metadata_blocks[].Level1
        found = []
        for rpu in rpus:
            for blk in rpu["vdr_dm_data"]["cmv29_metadata"]["ext_metadata_blocks"]:
                l1 = blk.get("Level1")
                if l1 and "max_pq" in l1 and "avg_pq" in l1:
                    found.append((l1["min_pq"], l1["avg_pq"], l1["max_pq"]))
        self.assertEqual(found, [(10, 800, 3079), (12, 700, 2500)])
        # min <= avg <= max, que es el sanity check del parser
        for mn, av, mx in found:
            self.assertLessEqual(mn, av)
            self.assertLessEqual(av, mx)

    def test_varios_bloques_del_mismo_nivel_en_un_frame(self):
        """L2 y L8 traen un bloque por target display."""
        levels = {
            "level1": [{"frame": 0, "min_pq": 0, "max_pq": 2081, "avg_pq": 819}],
            "level2": [{"frame": 0, "target_max_pq": 2081},
                       {"frame": 0, "target_max_pq": 2851},
                       {"frame": 0, "target_max_pq": 3079}],
        }
        rpus = self.main._rpus_from_levels(levels)
        blocks = rpus[0]["vdr_dm_data"]["cmv29_metadata"]["ext_metadata_blocks"]
        pqs = sorted(b["Level2"]["target_max_pq"] for b in blocks if "Level2" in b)
        self.assertEqual(pqs, [2081, 2851, 3079])

    def test_frames_sin_bloques_no_rompen_la_secuencia(self):
        """El L5 solo aparece en algunos frames; los huecos deben existir
        igualmente para que los índices sigan cuadrando con el vídeo."""
        levels = {
            "level1": [{"frame": i, "min_pq": 0, "max_pq": 2081, "avg_pq": 819}
                       for i in range(5)],
            "level5": [{"frame": 3, "active_area_top_offset": 276,
                        "active_area_bottom_offset": 276,
                        "active_area_left_offset": 0,
                        "active_area_right_offset": 0}],
        }
        rpus = self.main._rpus_from_levels(levels)
        self.assertEqual(len(rpus), 5)
        self.assertNotIn("cmv40_metadata", rpus[0]["vdr_dm_data"])
        blocks3 = rpus[3]["vdr_dm_data"]["cmv29_metadata"]["ext_metadata_blocks"]
        self.assertTrue(any("Level5" in b for b in blocks3))

    def test_sin_datos_devuelve_lista_vacia(self):
        self.assertEqual(self.main._rpus_from_levels({}), [])
        self.assertEqual(self.main._rpus_from_levels({"level1": []}), [])

if __name__ == "__main__":
    unittest.main()
