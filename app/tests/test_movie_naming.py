"""Tests para el naming de películas (Tab 1).

Cubre `_extract_title_year` (limpieza de tags entre corchetes/llaves esté o
no presente el año) y `_build_mkv_name` (omisión del `(Año)` cuando es
desconocido). Regresión del bug del ISO sin año cuyo nombre conservaba todos
los tags del release dentro del título.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.phase_b import _extract_title_year, _build_mkv_name  # noqa: E402


class TestExtractTitleYear(unittest.TestCase):

    def test_bug_iso_sin_anno_con_tags(self):
        # El caso reportado: sin año entre paréntesis, antes solo recortaba
        # los tags cuando había año → devolvía el stem entero.
        iso = ("Scream 7[Audio DCP 7.1.4] [Custom UHD Full 2160p HEVC HDR10 "
               "ES True-HD Atmos 7.1.4 Subs] [Grupo Custom HDO].iso")
        self.assertEqual(_extract_title_year(iso), ("Scream 7", "0000"))

    def test_con_anno_y_tag_posterior(self):
        self.assertEqual(
            _extract_title_year("The Brutalist (2024) [DV FEL].iso"),
            ("The Brutalist", "2024"),
        )

    def test_tags_antes_y_despues_del_anno(self):
        # Los corchetes previos al año también se eliminan (antes contaminaban
        # el título por el grupo lazy de la regex).
        self.assertEqual(
            _extract_title_year("Dune [HDR10] (2021) [Grupo].iso"),
            ("Dune", "2021"),
        )

    def test_llaves_como_tags(self):
        self.assertEqual(
            _extract_title_year("Blade Runner {Final Cut} (2007).iso"),
            ("Blade Runner", "2007"),
        )

    def test_sin_anno_ni_tags(self):
        self.assertEqual(
            _extract_title_year("pelicula_sin_anno.iso"),
            ("pelicula_sin_anno", "0000"),
        )

    def test_ruta_absoluta_usa_basename(self):
        self.assertEqual(
            _extract_title_year("/mnt/isos/Scream 7[Grupo HDO].iso"),
            ("Scream 7", "0000"),
        )

    def test_titulo_con_guion_se_conserva(self):
        # _extract_title_year NO normaliza separadores: el guión es parte del
        # título real y debe sobrevivir (a diferencia del parser de TMDb).
        self.assertEqual(
            _extract_title_year("Spider-Man (2002) [DV FEL].iso"),
            ("Spider-Man", "2002"),
        )


class TestBuildMkvName(unittest.TestCase):

    def test_con_anno_fel_y_dcp(self):
        self.assertEqual(
            _build_mkv_name("Scream 7", "2026", has_fel=True, audio_dcp=True),
            "Scream 7 (2026) [DV FEL] [Audio DCP].mkv",
        )

    def test_sin_anno_omite_parentesis(self):
        # year '0000' → sin '(0000)' espurio.
        self.assertEqual(
            _build_mkv_name("Scream 7", "0000", has_fel=True, audio_dcp=True),
            "Scream 7 [DV FEL] [Audio DCP].mkv",
        )

    def test_anno_vacio_omite_parentesis(self):
        self.assertEqual(
            _build_mkv_name("Scream 7", "", has_fel=False, audio_dcp=False),
            "Scream 7.mkv",
        )

    def test_con_anno_sin_tags(self):
        self.assertEqual(
            _build_mkv_name("The Brutalist", "2024", has_fel=False, audio_dcp=False),
            "The Brutalist (2024).mkv",
        )

    def test_sin_anno_sin_tags(self):
        self.assertEqual(
            _build_mkv_name("Movie", "0000", has_fel=False, audio_dcp=False),
            "Movie.mkv",
        )


if __name__ == "__main__":
    unittest.main()
