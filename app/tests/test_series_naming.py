"""Tests para build_series_mkv_name — nomenclatura Plex/Jellyfin.

Verifica todas las combinaciones: con/sin year, con/sin episode title,
tags FEL/DCP, sanitización de caracteres problemáticos.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.phase_b import build_series_mkv_name  # noqa: E402


class TestBuildSeriesMkvName(unittest.TestCase):

    def test_basic_with_year_and_title(self):
        # Caso canónico Plex:
        # "Mad Men (2007)/Season 01/Mad Men (2007) - S01E01 - Smoke Gets in Your Eyes.mkv"
        result = build_series_mkv_name(
            series_name="Mad Men",
            series_year=2007,
            season_number=1,
            episode_number=1,
            episode_title="Smoke Gets in Your Eyes",
        )
        self.assertEqual(
            result,
            "Mad Men (2007)/Season 01/Mad Men (2007) - S01E01 - Smoke Gets in Your Eyes.mkv",
        )

    def test_without_year(self):
        result = build_series_mkv_name(
            series_name="Twin Peaks",
            series_year=None,
            season_number=1,
            episode_number=1,
            episode_title="Pilot",
        )
        self.assertEqual(result, "Twin Peaks/Season 01/Twin Peaks - S01E01 - Pilot.mkv")

    def test_without_episode_title(self):
        result = build_series_mkv_name(
            series_name="Mad Men",
            series_year=2007,
            season_number=2,
            episode_number=5,
        )
        self.assertEqual(result, "Mad Men (2007)/Season 02/Mad Men (2007) - S02E05.mkv")

    def test_with_dv_fel_tag(self):
        result = build_series_mkv_name(
            series_name="The Crown",
            series_year=2016,
            season_number=4,
            episode_number=3,
            episode_title="Fairytale",
            has_fel=True,
        )
        self.assertEqual(
            result,
            "The Crown (2016)/Season 04/The Crown (2016) - S04E03 - Fairytale [DV FEL].mkv",
        )

    def test_with_both_tags(self):
        # Patrón consistente con _build_mkv_name de películas: cada tag
        # con espacio prefijo. "[DV FEL] [Audio DCP]" — no "[DV FEL][Audio DCP]".
        result = build_series_mkv_name(
            series_name="Test",
            series_year=2024,
            season_number=1,
            episode_number=1,
            episode_title="",
            has_fel=True,
            audio_dcp=True,
        )
        self.assertEqual(
            result,
            "Test (2024)/Season 01/Test (2024) - S01E01 [DV FEL] [Audio DCP].mkv",
        )

    def test_zero_padding_season_episode(self):
        # Season 11 / Episode 12 → S11E12 (no doble padding sobre 2 dígitos).
        result = build_series_mkv_name(
            series_name="S",
            series_year=None,
            season_number=11,
            episode_number=12,
        )
        self.assertEqual(result, "S/Season 11/S - S11E12.mkv")

    def test_sanitize_slashes_in_title(self):
        # Episode title con "/" → reemplazado por "-" (los slashes harían
        # que el path tuviera otro nivel de jerarquía no deseado).
        result = build_series_mkv_name(
            series_name="Black/White",  # también en el nombre de serie
            series_year=2020,
            season_number=1,
            episode_number=1,
            episode_title="Yes/No",
        )
        # Reemplaza / con -
        self.assertIn("Black-White (2020)", result)
        self.assertIn("Yes-No", result)
        # Solo 2 separadores / en la ruta (serie/season/file).
        self.assertEqual(result.count("/"), 2)

    def test_sanitize_colons(self):
        # Caso real: "Star Trek: The Next Generation"
        result = build_series_mkv_name(
            series_name="Star Trek: The Next Generation",
            series_year=1987,
            season_number=1,
            episode_number=1,
            episode_title="Encounter at Farpoint",
        )
        # ":" sustituido por "-"
        self.assertIn("Star Trek- The Next Generation", result)
        # NO contiene ":"
        self.assertNotIn(":", result)

    def test_sanitize_question_mark_and_quotes(self):
        # Caracteres prohibidos en NTFS: ? " < > |
        result = build_series_mkv_name(
            series_name="What?",
            series_year=None,
            season_number=1,
            episode_number=1,
            episode_title='"Quote" <test>',
        )
        for forbidden in '?"<>|':
            self.assertNotIn(forbidden, result)

    def test_collapse_multiple_spaces(self):
        # Espacios dobles colapsados a uno.
        result = build_series_mkv_name(
            series_name="Foo  Bar",   # doble espacio
            series_year=None,
            season_number=1,
            episode_number=1,
            episode_title="Hello  World",
        )
        self.assertNotIn("  ", result)

    def test_preserves_unicode_accents(self):
        # Acentos y caracteres no-ASCII se preservan (utf-8 en ext4/ZFS OK).
        result = build_series_mkv_name(
            series_name="Élite",
            series_year=2018,
            season_number=1,
            episode_number=1,
            episode_title="Bienvenidos",
        )
        self.assertIn("Élite", result)
        self.assertIn("Bienvenidos", result)


if __name__ == "__main__":
    unittest.main()
