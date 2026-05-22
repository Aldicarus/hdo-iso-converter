"""Tests para match_episodes_to_mpls — heurística runtime de TMDb TV.

Solo testea la lógica de matching (sin tocar TMDb real, que necesita
API key). Los endpoints search_tv_series/fetch_tv_details/fetch_tv_season
se validan en integración cuando se llaman desde main.py.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.tmdb import TvEpisode, match_episodes_to_mpls  # noqa: E402


def _ep(num: int, runtime: int, name: str = "") -> TvEpisode:
    return TvEpisode(
        episode_number=num,
        name=name or f"Episode {num}",
        runtime_minutes=runtime,
    )


class TestMatchEpisodesToMpls(unittest.TestCase):

    def test_perfect_match_all_high(self):
        # 3 MPLS de 42 min cada uno, 3 episodios TMDb de 42 min.
        mpls = [42.0, 42.0, 42.0]
        eps = [_ep(1, 42), _ep(2, 42), _ep(3, 42)]
        result = match_episodes_to_mpls(mpls, eps)
        self.assertEqual(len(result), 3)
        for i, r in enumerate(result):
            self.assertEqual(r["suggested_episode_number"], i + 1)
            self.assertEqual(r["confidence"], "high")
            self.assertEqual(r["matched_episode"]["episode_number"], i + 1)
            self.assertLessEqual(r["runtime_delta_min"], 1.0)

    def test_match_within_tolerance(self):
        # MPLS muy ligeramente distintos al runtime TMDb pero dentro ±1 min.
        mpls = [42.5, 41.8, 42.3]
        eps = [_ep(1, 42), _ep(2, 42), _ep(3, 42)]
        result = match_episodes_to_mpls(mpls, eps)
        for r in result:
            self.assertEqual(r["confidence"], "high")

    def test_low_confidence_when_runtime_diverges(self):
        # MPLS 60 min vs TMDb 42 min → fuera de tolerancia → low.
        mpls = [60.0]
        eps = [_ep(1, 42)]
        result = match_episodes_to_mpls(mpls, eps)
        self.assertEqual(result[0]["confidence"], "low")
        self.assertEqual(result[0]["runtime_delta_min"], 18.0)

    def test_low_confidence_when_tmdb_lacks_runtime(self):
        # TMDb no tiene runtime (común en specials, premieres no aired).
        mpls = [42.0]
        eps = [_ep(1, 0)]
        result = match_episodes_to_mpls(mpls, eps)
        self.assertEqual(result[0]["confidence"], "low")
        self.assertIsNone(result[0]["runtime_delta_min"])
        # Pero todavía sugiere el episodio
        self.assertEqual(result[0]["suggested_episode_number"], 1)

    def test_unknown_when_more_mpls_than_episodes(self):
        # 5 MPLS pero solo 3 episodios en TMDb (caso edge: disco con
        # commentary tracks o pilots dobles + episodios extras).
        mpls = [42.0, 42.0, 42.0, 42.0, 42.0]
        eps = [_ep(1, 42), _ep(2, 42), _ep(3, 42)]
        result = match_episodes_to_mpls(mpls, eps)
        # Los 3 primeros matchean, los 2 últimos quedan sin matched_episode
        self.assertEqual(result[0]["confidence"], "high")
        self.assertEqual(result[1]["confidence"], "high")
        self.assertEqual(result[2]["confidence"], "high")
        self.assertIsNone(result[3]["matched_episode"])
        self.assertIsNone(result[4]["matched_episode"])
        self.assertEqual(result[3]["confidence"], "unknown")
        self.assertEqual(result[3]["suggested_episode_number"], 4)

    def test_empty_episodes_returns_unknown(self):
        # Sin TMDb (caso "no encontré la serie"): match devuelve unknown
        # con suggested_episode_number = None — el usuario asigna a mano.
        mpls = [42.0, 42.0]
        result = match_episodes_to_mpls(mpls, [])
        self.assertEqual(len(result), 2)
        for r in result:
            self.assertEqual(r["confidence"], "unknown")
            self.assertIsNone(r["suggested_episode_number"])
            self.assertIsNone(r["matched_episode"])

    def test_custom_tolerance(self):
        # Con tolerance=3 min, un delta de 2.5 min sigue siendo high.
        mpls = [44.5]
        eps = [_ep(1, 42)]
        result = match_episodes_to_mpls(mpls, eps, tolerance_min=3.0)
        self.assertEqual(result[0]["confidence"], "high")
        # Con tolerance=1 (default), el mismo caso es low.
        result_strict = match_episodes_to_mpls(mpls, eps, tolerance_min=1.0)
        self.assertEqual(result_strict[0]["confidence"], "low")


if __name__ == "__main__":
    unittest.main()
