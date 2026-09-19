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


def _write_scenes(d: Path, name: str, cortes) -> Path:
    """Escribe el export de `-d scenes` con el formato REAL de dovi_tool.

    NO es JSON, aunque el export lleve `-f json` y el fichero acabe en
    `.json`: es un índice de frame por línea. Este fichero se escribía con
    `json.dumps` y por eso el test daba `scene_cuts == 3` mientras en el NAS
    valía **siempre 0** — el mismo patrón que el `MaxCLL` de mediainfo sin
    su unidad. Copiado de un bin del repo DoviTools.
    """
    p = d / name
    p.write_text("".join(f"{c}\n" for c in cortes), encoding="utf-8")
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
                "scenes": _write_scenes(d, "scenes.json", scenes if scenes is not None else []),
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
                "scenes": _write_scenes(d, "scenes.json", list(range(0, 200, 10))),
            }
            a = _parse_export_levels(paths)
            self.assertEqual(a.scene_cuts, 20)
            kind, reason = classify_l8(a)
            self.assertEqual(kind, "real")
            # El tier va en su propio campo; el motivo cuenta los ajustes.
            from phases.rpu_analyze import classify_l8_quality
            self.assertEqual(classify_l8_quality(a)[0], "full")



class TestElExportDeScenesNoEsJson(unittest.TestCase):
    """`dovi_tool export -d scenes=…` escribe un índice por línea, no JSON.

    El parser lo pasaba por `json.load`, que falla con «Extra data: line 2
    column 1 (char 2)» en cuanto hay un segundo número, así que `scene_cuts`
    valía 0 desde que se cambió el volcado completo por el export por
    niveles. Con 0, el criterio relativo del tier CORE+
    (`combos/scene_cuts >= 0.1`) no puede dispararse nunca.
    """

    def _contar(self, texto: str) -> int:
        from phases.rpu_analyze import _contar_cortes_de_escena
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "scenes.json"
            p.write_text(texto, encoding="utf-8")
            return _contar_cortes_de_escena(p)

    def test_el_formato_real_un_indice_por_linea(self):
        # Primeras líneas literales del export de un bin del repo DoviTools.
        self.assertEqual(
            self._contar("0\n40\n423\n857\n1304\n1330\n1335\n"), 7)

    def test_un_solo_corte_no_es_un_json_valido_por_casualidad(self):
        # Con un único número `json.load` SÍ funciona (es un int válido) y
        # devolvía 0 por el `isinstance(data, list)`. O sea que ni el caso
        # degenerado se contaba.
        self.assertEqual(self._contar("0\n"), 1)

    def test_un_array_json_se_acepta_igual(self):
        # Por si una versión futura lo emite así.
        self.assertEqual(self._contar("[0, 40, 423]"), 3)

    def test_un_formato_desconocido_da_cero_y_no_una_cuenta_a_medias(self):
        # Preferimos el hueco —que tiene respaldo, el umbral absoluto de
        # combos— a un número con pinta de dato.
        self.assertEqual(self._contar("frame,scene\n0,1\n40,1\n"), 0)

    def test_un_fichero_vacio_o_ausente_da_cero(self):
        from phases.rpu_analyze import _contar_cortes_de_escena
        self.assertEqual(self._contar(""), 0)
        self.assertEqual(self._contar("\n\n"), 0)
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                _contar_cortes_de_escena(Path(td) / "no_existe.json"), 0)
        self.assertEqual(_contar_cortes_de_escena(None), 0)

    def test_el_tier_core_mas_sale_por_el_criterio_RELATIVO(self):
        """El caso que el bug se comía: pocos combos, pero muchos por plano.

        Con `scene_cuts` a 0 esto salía `core`; el `[CMv4 CORE+]` del nombre
        del MKV solo aparecía con >= 400 combos absolutos.
        """
        from phases.rpu_analyze import _parse_export_levels, classify_l8_quality
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            l1 = [{"frame": i, "min_pq": 0, "max_pq": 2081, "avg_pq": 819}
                  for i in range(1000)]
            # 50 combos L8 con trabajo real, sin campos CMv4.0 (no es FULL).
            l8 = [{"frame": i, "length": 20, "target_display_index": 1,
                   "trim_slope": 2048 + (i % 50) * 13, "trim_offset": 2048,
                   "trim_power": 2048, "trim_chroma_weight": 2048,
                   "trim_saturation_gain": 2048, "ms_weight": 2048}
                  for i in range(1000)]
            paths = {
                "level1": _write(d, "l1.json", l1),
                "level2": _write(d, "l2.json", []),
                "level8": _write(d, "l8.json", l8),
                # 100 cortes: 50/100 = 0.5 >= 0.1 -> CORE+
                "scenes": _write_scenes(d, "scenes.json", range(0, 1000, 10)),
            }
            a = _parse_export_levels(paths)
            self.assertEqual(a.scene_cuts, 100)
            self.assertEqual(a.l8_unique_count, 50)
            tier, label, _ = classify_l8_quality(a)
            self.assertEqual(tier, "core_rich")
            self.assertIn("CORE+", label)


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
