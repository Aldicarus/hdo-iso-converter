"""Arnés del matcher fuzzy de la recomendación CMv4.0 (cmv40_recommend).

Regresión del falso positivo por containment: el título inglés que TMDb
devuelve para "El señor de los anillos: La comunidad del anillo" es
"The Lord of the Rings: The Fellowship of the Ring", que CONTIENE la
subcadena "the ring". El `_containment_score` viejo daba 0.875 a cualquier
subcadena de ≥3 caracteres, así que matcheaba espuriamente una fila
"The Ring (2002)" de la hoja de DoviTools → el modal de creación mostraba
"The Ring" como película identificada.

Fix (Variante A): el containment opera sobre TOKENS significativos, no sobre
subcadena de caracteres, y exige cobertura ≥60% de las palabras del título
largo. Como `_similarity` toma el max de tres señales, anular el containment
espurio solo quita falsos positivos.

El mismo `_similarity` lo usa `rec999_drive_match.rank_candidates`, así que el
fix también limpia el ranking de bins del repo Drive.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_recommend_match -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.cmv40_recommend import (  # noqa: E402
    _best_match, _containment_score, _similarity, _threshold_for,
)
from services.rec999_sheet import RecommendationRow, _normalize_title  # noqa: E402

# Slugs normalizados como los produce el pipeline real.
FELLOWSHIP = _normalize_title("The Lord of the Rings: The Fellowship of the Ring")
THE_RING = _normalize_title("The Ring")
DARK_KNIGHT = _normalize_title("Dark Knight")
THE_DARK_KNIGHT = _normalize_title("The Dark Knight")
ZOOTOPIA2 = _normalize_title("Zootopia 2")
ZOOTOPIA2_Y = _normalize_title("Zootopia 2 2025")


def _row(title, year, feasible=True):
    return RecommendationRow(
        feasible=feasible,
        title_raw=title,
        title_normalized=_normalize_title(title),
        year=year,
    )


class TestContainmentScore(unittest.TestCase):
    """El containment NO debe disparar para subcadenas que cubren poco del
    título largo, pero SÍ para los casos legítimos (prefijo/núcleo)."""

    def test_the_ring_not_contained_in_fellowship(self):
        # 'the ring' cubre 1 de 4 palabras significativas de Fellowship → 0
        self.assertEqual(_containment_score(THE_RING, FELLOWSHIP), 0.0)

    def test_dark_knight_still_matches(self):
        # 'dark knight' ⊂ 'the dark knight' (cobertura 2/2) → bonus alto
        self.assertGreaterEqual(_containment_score(DARK_KNIGHT, THE_DARK_KNIGHT), 0.85)

    def test_zootopia_sequel_year_still_matches(self):
        # 'zootopia 2' ⊂ 'zootopia 2 2025' (cobertura 2/3 = 0.67) → bonus
        self.assertGreaterEqual(_containment_score(ZOOTOPIA2, ZOOTOPIA2_Y), 0.85)


class TestSimilarity(unittest.TestCase):

    def test_the_ring_vs_fellowship_low(self):
        # Sin el containment espurio, las otras señales (seq/token-set) dicen
        # correctamente que no se parecen.
        self.assertLess(_similarity(THE_RING, FELLOWSHIP), 0.5)

    def test_dark_knight_high(self):
        self.assertGreaterEqual(_similarity(DARK_KNIGHT, THE_DARK_KNIGHT), 0.82)


class TestBestMatchEndToEnd(unittest.TestCase):
    """El corazón de `recommend`: con la hoja real, una fila 'The Ring' no
    debe ganar al candidato Fellowship por encima del umbral."""

    def test_no_spurious_match_when_only_the_ring(self):
        rows = [_row("The Ring", 2002, feasible=True)]
        best, score = _best_match(FELLOWSHIP, 2001, rows)
        # The Ring es el único candidato (best), pero su score NO supera el
        # umbral → recommend lo trata como 'unknown' (no match).
        threshold = _threshold_for(best, 2001)
        self.assertLess(score, threshold,
                        f"The Ring matcheó espuriamente: score={score} ≥ {threshold}")

    def test_real_fellowship_row_wins(self):
        rows = [
            _row("The Ring", 2002, feasible=True),
            _row("The Lord of the Rings: The Fellowship of the Ring", 2001, feasible=True),
        ]
        best, score = _best_match(FELLOWSHIP, 2001, rows)
        self.assertEqual(best.year, 2001)
        self.assertGreaterEqual(score, _threshold_for(best, 2001))

    def test_year_filter_excludes_far_rows(self):
        # Una fila con año lejano (>1) se descarta aunque el título matchee.
        rows = [_row("The Lord of the Rings: The Fellowship of the Ring", 1985)]
        best, score = _best_match(FELLOWSHIP, 2001, rows)
        self.assertIsNone(best)
        self.assertEqual(score, 0.0)


if __name__ == "__main__":
    unittest.main()
