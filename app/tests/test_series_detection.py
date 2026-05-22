"""Tests para detección de modo serie vs película en phase_a.

Ejecutar:
    cd app && python3 -m unittest tests.test_series_detection -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.phase_a import detect_disc_type  # noqa: E402


def _make_candidate(name: str, duration_min: float, audio: int = 4) -> dict:
    return {
        "mpls_name": name,
        "mpls_path": f"/tmp/{name}",
        "duration_minutes": duration_min,
        "audio_track_count": audio,
        "data": {},
    }


class TestDetectDiscType(unittest.TestCase):

    def test_movie_when_no_candidates(self):
        # Película típica: el MPLS principal es >90 min y queda filtrado
        # fuera del rango episodio por identify_episode_candidates → lista vacía.
        self.assertEqual(detect_disc_type([]), "movie")

    def test_movie_when_one_candidate(self):
        # Caso edge: una featurette de 30 min y nada más. No es serie.
        self.assertEqual(
            detect_disc_type([_make_candidate("00800.mpls", 30.0)]),
            "movie",
        )

    def test_movie_when_two_candidates(self):
        # Caso edge: dos candidatos similares pero por debajo del umbral
        # de 3. Podría ser disco con película + extra documental — no
        # serie hasta confirmar.
        self.assertEqual(
            detect_disc_type([
                _make_candidate("00800.mpls", 42.0),
                _make_candidate("00801.mpls", 43.0),
            ]),
            "movie",
        )

    def test_series_when_4_episodes_similar_duration(self):
        # Caso normal: drama de 42 min, 4 episodios casi idénticos.
        # CV = stddev / mean ≈ 0.5 / 42 ≈ 0.012 < 0.15
        candidates = [
            _make_candidate("00800.mpls", 42.0),
            _make_candidate("00801.mpls", 42.5),
            _make_candidate("00802.mpls", 41.5),
            _make_candidate("00803.mpls", 42.3),
        ]
        self.assertEqual(detect_disc_type(candidates), "series")

    def test_series_with_anime_sitcom_22min(self):
        # Anime / sitcom 22 min — variación natural pequeña.
        candidates = [
            _make_candidate("00800.mpls", 22.0),
            _make_candidate("00801.mpls", 22.3),
            _make_candidate("00802.mpls", 21.8),
        ]
        self.assertEqual(detect_disc_type(candidates), "series")

    def test_ambiguous_when_3_candidates_with_high_cv(self):
        # 3 candidatos pero duración muy dispersa (10, 40, 70 min).
        # No es serie (no son episodios homogéneos) ni puramente película.
        # El usuario decide.
        candidates = [
            _make_candidate("00800.mpls", 20.0),
            _make_candidate("00801.mpls", 40.0),
            _make_candidate("00802.mpls", 70.0),
        ]
        self.assertEqual(detect_disc_type(candidates), "ambiguous")

    def test_series_with_commentary_track(self):
        # Caso edge: 4 episodios + 4 commentary tracks (duración casi
        # idéntica al episodio principal). 8 candidatos, CV bajo.
        # Detecta como serie. La discriminación commentary track vs
        # episodio se hace después (fuera del scope de esta función).
        candidates = [
            _make_candidate(f"008{i:02d}.mpls", 42.0 + (0.5 if i % 2 else 0))
            for i in range(8)
        ]
        self.assertEqual(detect_disc_type(candidates), "series")


if __name__ == "__main__":
    unittest.main()
