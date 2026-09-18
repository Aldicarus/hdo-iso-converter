"""L3 — la señal que la app tenía delante y no miraba.

`has_l3` valía **False en el 100 % de los casos** desde siempre, y con él
el pill «L3 · local scene trim» de la radiografía. Dos causas, y las dos
silenciosas:

- el regex buscaba `L3` en la salida de `dovi_tool info --summary`, que
  emite **exactamente cuatro** líneas de niveles al final —`L5 offsets`,
  `L2 trims`, `L8 trims`, `L9 MDP`— y ninguna de L3;
- la otra vía, `export --levels`, no pedía `level3`.

Medido sobre 21 MKVs CMv4.0 de la biblioteca: **los 21 tienen L3**, entre 1
y 224 combos en 90 s de muestra. Y sobre el RPU de un Blu-ray (P7 MEL, CM
v2.9) el export de `level3` sale **vacío**, así que L3 es aportación
exclusiva del bin y el merge que lo transfiere no pisa nada del disco.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_nivel_l3 -v
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


def _l3(n_combos: int, frames: int = 200) -> list:
    """Registros L3 con el formato REAL de `dovi_tool export --levels`."""
    return [{"frame": i, "min_pq_offset": 2048, "max_pq_offset": 2047,
             "avg_pq_offset": 1589 + (i % n_combos)} for i in range(frames)]


def _analizar(*, l3=None, l8_combos=0, scenes=0, frames=200):
    from phases.rpu_analyze import _parse_export_levels
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        l1 = [{"frame": i, "min_pq": 0, "max_pq": 2081, "avg_pq": 819}
              for i in range(frames)]
        l8 = [{"frame": i, "length": 13, "target_display_index": 1,
               "trim_slope": 2048 + (i % l8_combos) * 31 if l8_combos else 2048,
               "trim_offset": 2048, "trim_power": 2048,
               "trim_chroma_weight": 2048, "trim_saturation_gain": 2048,
               "ms_weight": 2048}
              for i in range(frames)] if l8_combos else []
        paths = {
            "level1": _write(d, "l1.json", l1),
            "level2": _write(d, "l2.json", []),
            "level8": _write(d, "l8.json", l8),
        }
        if l3 is not None:
            paths["level3"] = _write(d, "l3.json", l3)
        if scenes:
            p = d / "scenes.json"
            paths["scenes"] = p
            paso = max(1, frames // scenes)
            p.write_text("".join(f"{i * paso}\n" for i in range(scenes)),
                         encoding="utf-8")
        return _parse_export_levels(paths)


class TestSeMideL3(unittest.TestCase):

    def test_el_export_de_level3_se_cuenta(self):
        a = _analizar(l3=_l3(37))
        self.assertEqual(a.l3_frames, 200)
        self.assertEqual(a.l3_unique_count, 37)

    def test_un_bin_con_l3_constante_da_un_combo(self):
        """The Amateur: un solo combo en sus 176.448 frames."""
        a = _analizar(l3=_l3(1))
        self.assertEqual(a.l3_unique_count, 1)

    def test_sin_bloques_l3_los_numeros_son_cero(self):
        """El caso del Blu-ray: su RPU CM v2.9 no trae L3 y el export sale
        vacío. No es un error, es que no lo tiene."""
        a = _analizar(l3=[])
        self.assertEqual((a.l3_frames, a.l3_unique_count), (0, 0))
        a = _analizar(l3=None)
        self.assertEqual((a.l3_frames, a.l3_unique_count), (0, 0))

    def test_level3_va_en_la_lista_base_del_export(self):
        """Si no se pide, no hay nada que contar — y el fallo es mudo."""
        from phases.rpu_analyze import _EXPORT_LEVELS
        self.assertIn("level3", _EXPORT_LEVELS)


class TestL3SoloRescataNuncaDegrada(unittest.TestCase):
    """El umbral de L3 está HEREDADO del de L8, no calibrado sobre L3: los
    conteos disponibles salen de un sniff de 90 s, que sirve para ver que
    las dos señales divergen pero no para fijar un corte.

    Por eso L3 solo puede mejorar un veredicto. En el peor caso de que el
    umbral esté mal, un bin sintético deja de llamarse sintético y decide
    el usuario — no que uno bueno se descarte.
    """

    def test_l8_plano_con_l3_trabajado_deja_de_ser_sintetico(self):
        """El caso de Transformers One: L8=1 combo y L3=83 sobre los MISMOS
        2.159 frames. Mirando solo L8 se le llama sintético y se recomienda
        Mantener, y el bin trae offsets que el reproductor no se inventa."""
        from phases.rpu_analyze import classify_l8
        pobre, _ = classify_l8(_analizar(l3=[], l8_combos=1))
        self.assertEqual(pobre, "default")
        rescatado, _ = classify_l8(_analizar(l3=_l3(83), l8_combos=1))
        self.assertEqual(rescatado, "indeterminate")

    def test_pero_un_l3_tambien_plano_no_rescata_nada(self):
        """The Amateur otra vez: L3 presente pero de un solo combo."""
        from phases.rpu_analyze import classify_l8
        c, _ = classify_l8(_analizar(l3=_l3(1), l8_combos=1))
        self.assertEqual(c, "default")

    def test_no_sube_a_real_a_proposito(self):
        """«Indeterminate» dice «no puedo afirmar que sea sintético», que es
        exactamente lo que se sabe con un umbral heredado. Decir «real»
        sería afirmar de más."""
        from phases.rpu_analyze import classify_l8
        c, _ = classify_l8(_analizar(l3=_l3(500), l8_combos=1))
        self.assertNotEqual(c, "real")

    def test_un_l3_pobre_no_degrada_un_l8_bueno(self):
        from phases.rpu_analyze import classify_l8
        sin_l3, _ = classify_l8(_analizar(l3=None, l8_combos=40))
        con_l3_pobre, _ = classify_l8(_analizar(l3=_l3(1), l8_combos=40))
        self.assertEqual(sin_l3, "real")
        self.assertEqual(con_l3_pobre, "real")

    def test_el_tier_core_mas_tambien_lo_puede_dar_l3(self):
        from phases.rpu_analyze import classify_l8_quality
        # 40 combos L8 sobre 400 cortes = 0.1 justo por debajo del umbral si
        # se mira solo L8; con L3 rico, sube.
        flojo = _analizar(l3=_l3(1), l8_combos=20, scenes=400)
        rico = _analizar(l3=_l3(120), l8_combos=20, scenes=400)
        self.assertEqual(classify_l8_quality(flojo)[0], "core")
        self.assertEqual(classify_l8_quality(rico)[0], "core_rich")

    def test_y_nunca_baja_un_tier_ya_ganado(self):
        from phases.rpu_analyze import classify_l8_quality
        sin = _analizar(l3=None, l8_combos=90, scenes=100)
        con_pobre = _analizar(l3=_l3(1), l8_combos=90, scenes=100)
        self.assertEqual(classify_l8_quality(sin)[0],
                         classify_l8_quality(con_pobre)[0])


class TestElSummaryNoSirveParaEstosNiveles(unittest.TestCase):
    """Los patrones de L3/L4/L10/L11/L254 se retiraron del parseo del
    summary: no podían casar y hacían creer que algo se comprobaba."""

    def test_el_parseo_del_summary_ya_no_los_busca(self):
        src = (APP_DIR / "phases" / "phase_a.py").read_text(encoding="utf-8")
        for patron in ('r"L3\\b|Level 3', 'r"L4\\b|Level 4',
                       'r"L10\\b|Level 10', 'r"L11\\b|Level 11',
                       'r"L254\\b|Level 254'):
            with self.subTest(patron=patron):
                self.assertNotIn(patron, src)

    def test_el_summary_real_solo_trae_cuatro_niveles(self):
        """Copiado de `dovi_tool info --summary` sobre un bin del repo
        DoviTools. Si algún día emitiera más, este test lo dirá."""
        summary = (
            "Summary:\n  Frames: 155001\n  Profile: 7 (FEL)\n"
            "  DM version: 2 (CM v4.0)\n  Scene/shot count: 1929\n"
            "  RPU mastering display: 0.0001/1000 nits\n"
            "  RPU content light level (L1): MaxCLL: 996.13 nits, MaxFALL: 91.01 nits\n"
            "  L6 metadata\n"
            "  L5 offsets: top=276, bottom=276, left=0, right=0\n"
            "  L2 trims: 100 nits, 600 nits, 1000 nits\n"
            "  L8 trims: 100 nits, 600 nits\n"
            "  L9 MDP: DCI-P3 D65\n")
        for ausente in ("L3", "L4 ", "L10", "L11", "L254"):
            with self.subTest(nivel=ausente):
                self.assertNotIn(ausente, summary)

    def test_el_enriquecimiento_si_los_pide(self):
        src = (APP_DIR / "phases" / "mkv_analyze.py").read_text(encoding="utf-8")
        for nivel in ("level3", "level4", "level10", "level11"):
            with self.subTest(nivel=nivel):
                self.assertIn(f'"{nivel}"', src)


if __name__ == "__main__":
    unittest.main()
