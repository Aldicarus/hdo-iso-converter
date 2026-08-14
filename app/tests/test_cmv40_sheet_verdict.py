"""
Veredicto del sheet de DoviTools para el flujo de esta app.

El sheet cataloga cada título desde el objetivo de la comunidad (convertir
el disco a P8.1 single-layer). Esta app hace lo contrario: preserva el FEL.
Por eso el motivo más común de "no factible" — 219 de 326 filas en la hoja
real — no nos aplica, y un mismo título puede estar en dos secciones con
veredictos opuestos (32 casos medidos).

Cubre:
  - classify_blockers: clasificación de motivos reales de la hoja
  - _matching_rows: se recogen TODAS las filas del título, factibles primero
  - _best_match: en empate gana la factible (antes ganaba la izquierda)
  - _build_verdict: los 5 estados
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.cmv40_recommend import (  # noqa: E402
    BLOCKER_GRADING,
    BLOCKER_NO_BD,
    BLOCKER_OTHER,
    BLOCKER_P8_ONLY,
    BLOCKER_STATIC_DV,
    BLOCKER_UNSPEC,
    _best_match,
    _build_verdict,
    _matching_rows,
    _to_match_row,
    blockers_apply_to_fel_workflow,
    classify_blockers,
)
from services.rec999_sheet import RecommendationRow  # noqa: E402


# Motivo textual real de la hoja (Obsession y 218 filas más)
P8_NOTE = ("Can only be played on a FEL device and can't be converted to P8 "
           "without baking FEL into BL")
RESTORE_NOTE = "cmv4 0 bloc can be restored to the P7 RPU (workflow 2-3)"


def _row(feasible, section, notes="", title="Obsession 2026", year=2026,
         dv_source="", sync="", frames=None):
    return RecommendationRow(
        feasible=feasible,
        section=section,
        title_raw=title,
        title_normalized=title.rsplit(" ", 1)[0].lower(),
        year=year,
        dv_source=dv_source,
        sync_offset=sync,
        sync_offset_frames=frames,
        notes=notes,
    )


class TestClassifyBlockers(unittest.TestCase):
    def test_p8_only_es_el_motivo_de_obsession(self):
        self.assertEqual(classify_blockers(P8_NOTE), [BLOCKER_P8_ONLY])

    def test_p8_only_no_aplica_a_nuestro_flujo(self):
        self.assertFalse(blockers_apply_to_fel_workflow([BLOCKER_P8_ONLY]))

    def test_static_dv(self):
        for note in ("static dv", "Static metadata",
                     "static crap, only 12 shots", "static dv: only 8 shots for the film"):
            self.assertIn(BLOCKER_STATIC_DV, classify_blockers(note), note)
            self.assertTrue(blockers_apply_to_fel_workflow(classify_blockers(note)))

    def test_grading(self):
        for note in ("mdl mismatch", "different grade + mdl mismatch",
                     "dv is brighter", "web is a different grade",
                     "not the same grade and mdl mismatch",
                     "some shots are different"):
            self.assertIn(BLOCKER_GRADING, classify_blockers(note), note)

    def test_no_bd(self):
        self.assertIn(BLOCKER_NO_BD,
                      classify_blockers("no bd yet but itunes p5 and web available"))

    def test_motivo_mixto_devuelve_todas_las_categorias(self):
        # Una nota que mezcla el tema P8 con un problema real no debe perder
        # el problema real por quedarse con la primera categoría.
        blockers = classify_blockers(P8_NOTE + " + different grade")
        self.assertIn(BLOCKER_P8_ONLY, blockers)
        self.assertIn(BLOCKER_GRADING, blockers)
        self.assertTrue(blockers_apply_to_fel_workflow(blockers))

    def test_sin_texto_y_texto_no_reconocido(self):
        self.assertEqual(classify_blockers(""), [BLOCKER_UNSPEC])
        self.assertEqual(classify_blockers("   "), [BLOCKER_UNSPEC])
        self.assertEqual(classify_blockers("algo raro sin patrón"), [BLOCKER_OTHER])


class TestMatchingRows(unittest.TestCase):
    """Caso Obsession: dos filas, veredictos opuestos, mismo score."""

    def setUp(self):
        self.rows = [
            _row(False, "infeasible", P8_NOTE, dv_source="BD FEL"),
            _row(True, "feasible", RESTORE_NOTE, dv_source="MA", sync="(+24)", frames=24),
        ]

    def test_recoge_las_dos_filas(self):
        got = _matching_rows("obsession", 2026, self.rows, 0.72)
        self.assertEqual(len(got), 2)

    def test_factible_primero(self):
        got = _matching_rows("obsession", 2026, self.rows, 0.72)
        self.assertTrue(got[0][0].feasible)
        self.assertEqual(got[0][0].dv_source, "MA")

    def test_best_match_prefiere_factible_en_empate(self):
        # Antes: `sim > best_score` con la izquierda emitida primero →
        # ganaba la no factible en 31 de 32 títulos duplicados.
        row, score = _best_match("obsession", 2026, self.rows)
        self.assertIsNotNone(row)
        self.assertTrue(row.feasible, "en empate debe ganar la fila factible")

    def test_best_match_no_degrada_score_mayor(self):
        # Una fila no factible con score claramente mejor sigue ganando:
        # la preferencia solo rompe empates.
        rows = [
            _row(False, "infeasible", P8_NOTE, title="Obsession 2026"),
            _row(True, "feasible", RESTORE_NOTE, title="Obsession Returns 2026",
                 year=2026),
        ]
        row, _ = _best_match("obsession", 2026, rows)
        self.assertFalse(row.feasible)

    def test_filtra_por_ano(self):
        rows = [_row(True, "feasible", RESTORE_NOTE, title="Obsession 1999", year=1999)]
        self.assertEqual(_matching_rows("obsession", 2026, rows, 0.72), [])


class TestBuildVerdict(unittest.TestCase):
    def _mr(self, feasible, section, notes=""):
        return _to_match_row(_row(feasible, section, notes), 1.0)

    def test_obsession_es_factible_no_no_factible(self):
        rows = [self._mr(True, "feasible", RESTORE_NOTE),
                self._mr(False, "infeasible", P8_NOTE)]
        status, label, detail = _build_verdict(rows)
        self.assertEqual(status, "recommended")
        self.assertEqual(label, "Factible")
        self.assertIn("P8.1", detail)  # explica de qué hablaba el ❌

    def test_solo_p8_da_nota_informativa_no_rechazo(self):
        status, label, _ = _build_verdict([self._mr(False, "infeasible", P8_NOTE)])
        self.assertEqual(status, "p8_only_note")
        self.assertEqual(label, "No convertible a P8.1")

    def test_motivo_relevante_si_rechaza(self):
        status, label, detail = _build_verdict(
            [self._mr(False, "infeasible", "static dv, only 8 shots")])
        self.assertEqual(status, "not_feasible")
        self.assertEqual(label, "No recomendado")
        self.assertIn("estática", detail)

    def test_factible_con_aviso_relevante_da_caveats(self):
        rows = [self._mr(True, "feasible", RESTORE_NOTE),
                self._mr(False, "infeasible", "mdl mismatch")]
        status, label, detail = _build_verdict(rows)
        self.assertEqual(status, "caveats")
        self.assertEqual(label, "Viable con avisos")
        self.assertIn("grading", detail)

    def test_probably_ok_no_es_verde(self):
        # La sección "Not Sure!" se parsea como feasible=True pero no está
        # verificada: no debe presentarse igual que la columna factible.
        status, label, _ = _build_verdict([self._mr(True, "probably_ok", "")])
        self.assertEqual(status, "caveats")
        self.assertEqual(label, "Probablemente OK")

    def test_sin_filas(self):
        status, label, _ = _build_verdict([])
        self.assertEqual(status, "unknown")
        self.assertEqual(label, "Sin datos")

    def test_fila_factible_no_lleva_blockers(self):
        mr = self._mr(True, "feasible", RESTORE_NOTE)
        self.assertEqual(mr.blockers, [])
        self.assertTrue(mr.applies_to_our_workflow)

    def test_fila_p8_marcada_como_no_aplicable(self):
        mr = self._mr(False, "infeasible", P8_NOTE)
        self.assertEqual(mr.blockers, [BLOCKER_P8_ONLY])
        self.assertFalse(mr.applies_to_our_workflow)
        self.assertTrue(mr.blocker_labels[0].startswith("Solo afecta"))


class TestSheetSyncHint(unittest.TestCase):
    """Contraste offset del sheet vs detectado por cross-correlation."""

    def setUp(self):
        from phases.cmv40_pipeline import sheet_sync_hint
        self.hint = sheet_sync_hint
        from models import CMv40Session
        self.session = CMv40Session(
            id="x", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="y.mkv", artifacts_dir="/tmp/x",
        )

    def test_sin_recomendacion_no_hay_hint(self):
        self.assertIsNone(self.hint(self.session, {"offset": 24}))

    def test_sin_offset_en_el_sheet_no_hay_hint(self):
        self.session.sheet_recommendation = {"rows": [{"sync_offset_frames": None}]}
        self.assertIsNone(self.hint(self.session, {"offset": 24}))

    def test_coincidencia_exacta(self):
        self.session.sheet_recommendation = {
            "sync_offset_frames": 24, "sync_offset": "(+24)",
            "match_title": "Obsession 2026", "rows": [],
        }
        out = self.hint(self.session, {"offset": 24})
        self.assertTrue(out["agrees"])
        self.assertEqual(out["delta"], 0)

    def test_dentro_de_tolerancia(self):
        self.session.sheet_recommendation = {"sync_offset_frames": 24, "rows": []}
        self.assertTrue(self.hint(self.session, {"offset": 26})["agrees"])
        self.assertFalse(self.hint(self.session, {"offset": 30})["agrees"])

    def test_signo_invertido_se_distingue(self):
        self.session.sheet_recommendation = {"sync_offset_frames": 24, "rows": []}
        out = self.hint(self.session, {"offset": -24})
        self.assertFalse(out["agrees"])
        self.assertTrue(out["sign_flipped"])

    def test_offset_tomado_de_la_fila_cuando_falta_en_el_plano(self):
        # La sección izquierda no trae sync; si la primaria es esa, el dato
        # hay que sacarlo de la fila factible.
        self.session.sheet_recommendation = {
            "sync_offset_frames": None,
            "rows": [{"sync_offset_frames": None},
                     {"sync_offset_frames": 48, "sync_offset": "(+48)"}],
        }
        out = self.hint(self.session, {"offset": 48})
        self.assertEqual(out["sheet_offset"], 48)
        self.assertTrue(out["agrees"])


class TestRecommendEndToEnd(unittest.TestCase):
    """Cadena completa: CSV del sheet → parser real → recommend().

    Usa el parser de verdad (no filas construidas a mano) para que el test
    falle si cambia la normalización de títulos o el layout de columnas.
    """

    # 19 columnas: 0-4 izquierda (no factible) · 6-11 derecha (factible)
    CSV = (
        "h0,h1,h2,h3,h4,h5,h6,h7,h8,h9,h10,h11,h12,h13,h14,h15,h16,h17,h18\n"
        f'"Obsession 2026","BD FEL","HDR COMP","sample","{P8_NOTE}",'
        f'"","Obsession 2026","(+24)","MA","HDR COMP","plot","{RESTORE_NOTE}",'
        '"","","","","","",""\n'
        f'"Static Movie 2020","WEB","","","static dv: only 8 shots",'
        '"","","","","","","","","","","","","",""\n'
    )

    def _run(self, title, year):
        import asyncio
        from unittest.mock import patch

        from services.rec999_sheet import parse_csv_text
        import services.cmv40_recommend as mod

        rows = parse_csv_text(self.CSV)

        async def fake_rows():
            return rows

        with patch.object(mod, "get_recommendations", fake_rows), \
             patch.object(mod, "get_cache_status", lambda: {"source": "csv"}), \
             patch.object(mod, "tmdb_is_configured", lambda: False):
            return asyncio.run(mod.recommend(title, year))

    def test_parser_produce_las_dos_secciones(self):
        from services.rec999_sheet import parse_csv_text
        rows = parse_csv_text(self.CSV)
        obsession = [r for r in rows if "obsession" in r.title_normalized]
        self.assertEqual(len(obsession), 2)
        self.assertEqual({r.section for r in obsession}, {"infeasible", "feasible"})

    def test_obsession_sale_factible_con_las_dos_filas(self):
        res = self._run("Obsession", 2025)      # el fichero dice 2025, el sheet 2026
        self.assertEqual(res.status, "recommended")
        self.assertEqual(len(res.rows), 2, "deben viajar ambas filas al frontend")
        self.assertEqual(res.feasible_row_count, 1)
        self.assertEqual(res.infeasible_row_count, 1)

    def test_campos_planos_vienen_de_la_fila_factible(self):
        res = self._run("Obsession", 2025)
        self.assertEqual(res.dv_source, "MA")          # no 'BD FEL'
        self.assertEqual(res.primary_section, "feasible")
        self.assertEqual(res.sync_offset_frames, 24)   # la izquierda no trae sync
        self.assertIn("restored", res.notes)

    def test_el_motivo_p8_se_marca_como_no_aplicable(self):
        res = self._run("Obsession", 2025)
        infeasible = next(r for r in res.rows if not r.feasible)
        self.assertEqual(infeasible.blockers, [BLOCKER_P8_ONLY])
        self.assertFalse(infeasible.applies_to_our_workflow)
        self.assertFalse(res.blockers_apply_to_our_workflow)

    def test_titulo_solo_no_factible_con_motivo_real(self):
        res = self._run("Static Movie", 2020)
        self.assertEqual(res.status, "not_feasible")
        self.assertIn(BLOCKER_STATIC_DV, res.blockers)
        self.assertTrue(res.blockers_apply_to_our_workflow)

    def test_titulo_ausente_queda_unknown(self):
        res = self._run("Pelicula Inexistente", 1999)
        self.assertEqual(res.status, "unknown")
        self.assertEqual(res.rows, [])
        self.assertEqual(res.verdict_label, "")


if __name__ == "__main__":
    unittest.main()
