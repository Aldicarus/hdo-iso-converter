"""Arnés de regresión de la clasificación de subtítulos por idioma (Fase B).

Cubre la heurística de _select_subtitle_tracks cuando un idioma tiene 2+ pistas
PGS y hay que decidir cuál es la "completa" incluida, cuál es forzado y cuáles
son alternativas ambiguas (España/Latam, normal/SDH, comentarios…).

Regla vigente (Opción A — "primera del disco en la banda casi-idéntica"):

  - ratio (mayor/menor) ≥ 3×  → la pequeña es FORZADO (solo carteles).
  - ratio < 3×                → ambas llevan diálogo completo (ambiguas).
      · banda casi-idéntica (ratio < 2× con la de más paquetes): elegibles
        como completa. Entre ellas se elige la PRIMERA del disco — la de más
        paquetes suele ser la versión SDH (texto extra en momentos sin
        diálogo) y la primera la normal.
      · banda 2×–3×: se quedan ambiguas pero NUNCA se eligen como completa
        (podrían ser un forzado grande de 2000+ paquetes disfrazado).

El conjunto de casos viene de discos reales (commits 4b985d2 y la sesión de
Scream 7) más el caso sintético del "forzado grande" que distingue la Opción A
de la propuesta literal (Opción C).

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_subtitle_classification -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import RawSubtitleTrack  # noqa: E402
from phases.phase_b import _select_subtitle_tracks  # noqa: E402


# ── Builders / helpers ──────────────────────────────────────────────────

def _sub(language, packet_count):
    """RawSubtitleTrack PGS con packet_count (el bitrate es irrelevante cuando
    hay packets; lo ponemos no-cero para que no parezca un forzado Forma A)."""
    return RawSubtitleTrack(
        language=language, bitrate_kbps=20.0, description="", packet_count=packet_count,
    )


def _complete_packets(included, lang_en):
    """packet_count de la pista COMPLETA incluida para un idioma (o None)."""
    for t in included:
        if t.subtitle_type == "complete" and t.raw.language.lower() == lang_en.lower():
            return t.raw.packet_count
    return None


def _forced_packets(included, lang_en):
    """packet_count de la pista FORZADA incluida para un idioma (o None)."""
    for t in included:
        if t.subtitle_type == "forced" and t.raw.language.lower() == lang_en.lower():
            return t.raw.packet_count
    return None


def _included_packets(included, lang_en):
    """Todos los packet_count incluidos para un idioma (cualquier tipo)."""
    return {t.raw.packet_count for t in included if t.raw.language.lower() == lang_en.lower()}


class TestSubtitleClassification(unittest.TestCase):

    # ── Opción A: banda casi-idéntica (<2×) → primera del disco ──────────

    def test_scream7_english_picks_first_of_disc(self):
        """Inglés 13410 (1ª) + 17308 (2ª), ratio 1.29× <2×. La 2ª (más
        paquetes) es la SDH probable → debe incluirse la PRIMERA del disco."""
        tracks = [_sub("English", 13410), _sub("English", 17308)]
        included, discarded = _select_subtitle_tracks(tracks, vo_language="English")
        self.assertEqual(_complete_packets(included, "English"), 13410)
        # La de más paquetes queda descartada como alternativa ambigua.
        self.assertIn(17308, {d.raw.packet_count for d in discarded})

    def test_scream7_spanish_picks_first_of_disc(self):
        """Castellano 12569 (1ª) + 12817 (2ª), ratio 1.02× <2× → primera."""
        tracks = [_sub("Spanish", 12569), _sub("Spanish", 12817)]
        included, discarded = _select_subtitle_tracks(tracks, vo_language="English")
        self.assertEqual(_complete_packets(included, "Spanish"), 12569)
        self.assertIn(12817, {d.raw.packet_count for d in discarded})

    def test_near_identical_three_tracks_picks_first(self):
        """Tres completas casi idénticas → la primera del disco gana."""
        tracks = [_sub("Spanish", 5800), _sub("Spanish", 6000), _sub("Spanish", 5900)]
        included, _ = _select_subtitle_tracks(tracks, vo_language="English")
        self.assertEqual(_complete_packets(included, "Spanish"), 5800)

    # ── Banda 2×–3×: NUNCA elegir como completa (protege del forzado grande) ──

    def test_large_forced_not_picked_even_if_first(self):
        """Completo 5500 + forzado grande 2200 (ratio 2.5×, banda 2-3×). Aunque
        el forzado vaya PRIMERO en el disco, la completa incluida debe ser la de
        5500 — NO el forzado grande. Este caso distingue la Opción A de la
        propuesta literal (que cogería el 2200)."""
        tracks = [_sub("Spanish", 2200), _sub("Spanish", 5500)]
        included, _ = _select_subtitle_tracks(tracks, vo_language="English")
        self.assertEqual(_complete_packets(included, "Spanish"), 5500)
        self.assertNotIn(2200, _included_packets(included, "Spanish"))

    def test_large_forced_not_picked_when_second(self):
        """Mismo caso con el forzado grande en 2ª posición: la completa sigue
        siendo la de 5500 (la de más paquetes)."""
        tracks = [_sub("Spanish", 5500), _sub("Spanish", 2200)]
        included, _ = _select_subtitle_tracks(tracks, vo_language="English")
        self.assertEqual(_complete_packets(included, "Spanish"), 5500)

    # ── Forzados clásicos (ratio ≥3×): la regla nueva NO los toca ─────────

    def test_pulp_fiction_complete_vs_forced(self):
        """Castellano completo 11201 + forzado 753 (ratio 14.9×). Forzado
        primero en disco para asegurar que la detección no depende del orden."""
        tracks = [_sub("Spanish", 753), _sub("Spanish", 11201)]
        included, _ = _select_subtitle_tracks(tracks, vo_language="English")
        self.assertEqual(_complete_packets(included, "Spanish"), 11201)
        self.assertEqual(_forced_packets(included, "Spanish"), 753)

    def test_alien_romulus_complete_vs_forced(self):
        """Castellano completo 7741 + forzado 1831 (ratio 4.23× ≥3×)."""
        tracks = [_sub("Spanish", 7741), _sub("Spanish", 1831)]
        included, _ = _select_subtitle_tracks(tracks, vo_language="English")
        self.assertEqual(_complete_packets(included, "Spanish"), 7741)
        self.assertEqual(_forced_packets(included, "Spanish"), 1831)

    def test_complete_plus_light_forced(self):
        """Completo grande 8000 + forzado ligero 50 (ratio 160×)."""
        tracks = [_sub("Spanish", 8000), _sub("Spanish", 50)]
        included, _ = _select_subtitle_tracks(tracks, vo_language="English")
        self.assertEqual(_complete_packets(included, "Spanish"), 8000)
        self.assertEqual(_forced_packets(included, "Spanish"), 50)

    # ── Pista única ──────────────────────────────────────────────────────

    def test_single_complete(self):
        tracks = [_sub("Spanish", 5000)]
        included, _ = _select_subtitle_tracks(tracks, vo_language="English")
        self.assertEqual(_complete_packets(included, "Spanish"), 5000)
        self.assertIsNone(_forced_packets(included, "Spanish"))

    def test_single_forced(self):
        tracks = [_sub("Spanish", 200)]
        included, _ = _select_subtitle_tracks(tracks, vo_language="English")
        self.assertEqual(_forced_packets(included, "Spanish"), 200)
        self.assertIsNone(_complete_packets(included, "Spanish"))


if __name__ == "__main__":
    unittest.main()
