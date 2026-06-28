"""Arnés de regresión del mapeo de pistas (Fase E) + routing de reordenación.

Cubre el hueco que dejó pasar el P0 de la auditoría: discos con el audio VO
(inglés) físicamente ANTES del Castellano caían a la ruta propedit, que mapeaba
las pistas por POSICIÓN en vez de por contenido → el stream inglés recibía la
etiqueta "Castellano…" + flag_default. La causa raíz era doble:

  1. `_check_reordering` nunca detectaba reordenación real (Fase B asigna
     position=i en orden Castellano-primero, así que las posiciones eran SIEMPRE
     monótonas y el check `positions != sorted(positions)` jamás disparaba).
  2. `run_phase_e_propedit` mapeaba por posición (`mkv_audio_ids[i]`) en vez de
     reusar `_match_tracks_to_source` como la ruta directa.

Estos tests fijan el comportamiento correcto de ambas piezas. Sin discos reales:
construimos `included_tracks` (Pydantic) y un `track_map` (estilo mkvmerge
--identify) a mano.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_track_mapping -v
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import (  # noqa: E402
    IncludedAudioTrack, IncludedSubtitleTrack, RawAudioTrack, RawSubtitleTrack,
)
from phases.phase_e import (  # noqa: E402
    _check_reordering, _match_tracks_to_source, _propedit_track_edits,
)


# ── Builders de fixtures ────────────────────────────────────────────────

def _audio(language, codec, description, label, *, flag_default=False, position=0):
    """IncludedAudioTrack con su RawAudioTrack anidado. `language` (inglés) y
    `codec` (estilo BDInfo, p. ej. 'Dolby TrueHD/Atmos Audio') como los produce
    Fase B."""
    return IncludedAudioTrack(
        position=position,
        raw=RawAudioTrack(
            codec=codec, language=language, bitrate_kbps=0, description=description,
        ),
        language_literal=label.split()[0],
        codec_literal=label,
        label=label,
        flag_default=flag_default,
        selection_reason="test",
    )


def _sub(language, subtitle_type, label, *, flag_default=False, flag_forced=False, position=0):
    return IncludedSubtitleTrack(
        position=position,
        raw=RawSubtitleTrack(language=language, bitrate_kbps=0.0, description=""),
        language_literal=label.split()[0],
        subtitle_type=subtitle_type,
        label=label,
        flag_default=flag_default,
        flag_forced=flag_forced,
        selection_reason="test",
    )


def _amap(idx, *, codec, lang, ch=0):
    """Entrada de track_map de audio (estilo mkvmerge --identify)."""
    return (idx, {"type": "audio", "codec": codec, "language": lang, "audio_channels": ch})


def _smap(idx, *, lang):
    return (idx, {"type": "subtitles", "codec": "", "language": lang, "audio_channels": 0})


def _track_map(*entries):
    return {idx: props for idx, props in entries}


def _session(*tracks):
    """Stub mínimo: _check_reordering / _propedit_track_edits sólo leen
    .included_tracks, así que evitamos construir un Session completo."""
    return types.SimpleNamespace(included_tracks=list(tracks))


# ── _match_tracks_to_source: matching por contenido (ya correcto) ────────

class TestMatchTracksToSource(unittest.TestCase):
    def test_audio_vo_first_physical_order(self):
        """included = [Castellano, Inglés] (orden deseado), pero el source tiene
        el inglés físicamente primero (id=0) y el español segundo (id=1). El
        matcher asigna por contenido, no por posición."""
        included = [
            _audio("Spanish", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz", "Castellano TrueHD Atmos 7.1", position=0),
            _audio("English", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz", "Inglés TrueHD Atmos 7.1", position=1),
        ]
        tmap = _track_map(
            _amap(0, codec="TrueHD Atmos", lang="eng", ch=8),
            _amap(1, codec="TrueHD Atmos", lang="spa", ch=8),
        )
        self.assertEqual(_match_tracks_to_source(included, [0, 1], tmap), {0: 1, 1: 0})

    def test_audio_channel_disambiguation(self):
        """Dos pistas Castellano AC-3 (DD 2.0 id=0, DD 5.1 id=1). El usuario
        incluyó la 5.1 → mapea a id=1 por canales, no a la primera por orden."""
        included = [_audio("Spanish", "Dolby Digital Audio", "5.1 / 48 kHz / 448 kbps", "Castellano DD 5.1", position=0)]
        tmap = _track_map(
            _amap(0, codec="AC-3", lang="spa", ch=2),
            _amap(1, codec="AC-3", lang="spa", ch=6),
        )
        self.assertEqual(_match_tracks_to_source(included, [0, 1], tmap), {0: 1})

    def test_audio_one_to_one_no_reuse(self):
        included = [
            _audio("Spanish", "DTS-HD Master Audio", "5.1 / 48 kHz", "Castellano DTS-HD MA 5.1", position=0),
            _audio("English", "DTS-HD Master Audio", "5.1 / 48 kHz", "Inglés DTS-HD MA 5.1", position=1),
        ]
        tmap = _track_map(
            _amap(0, codec="DTS-HD Master Audio", lang="spa", ch=6),
            _amap(1, codec="DTS-HD Master Audio", lang="eng", ch=6),
        )
        result = _match_tracks_to_source(included, [0, 1], tmap)
        self.assertEqual(result, {0: 0, 1: 1})
        self.assertEqual(len(set(result.values())), 2)


# ── _check_reordering: decisión ruta directa vs propedit ─────────────────

class TestCheckReordering(unittest.TestCase):
    def test_vo_first_triggers_direct(self):
        """REGRESIÓN P0: source con audio VO físicamente primero (eng id=0,
        spa id=1) e included en orden Castellano-primero. Debe detectar
        reordenación → ruta directa (True). Antes devolvía False → propedit
        posicional → etiquetas cruzadas."""
        session = _session(
            _audio("Spanish", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz", "Castellano TrueHD Atmos 7.1", position=0),
            _audio("English", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz", "Inglés TrueHD Atmos 7.1", position=1),
        )
        tmap = _track_map(
            _amap(0, codec="TrueHD Atmos", lang="eng", ch=8),
            _amap(1, codec="TrueHD Atmos", lang="spa", ch=8),
        )
        self.assertTrue(_check_reordering(session, tmap))

    def test_aligned_order_uses_propedit(self):
        """Orden físico == orden deseado (Castellano id=0, Inglés id=1) → sin
        reordenación → ruta propedit (False)."""
        session = _session(
            _audio("Spanish", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz", "Castellano TrueHD Atmos 7.1", position=0),
            _audio("English", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz", "Inglés TrueHD Atmos 7.1", position=1),
        )
        tmap = _track_map(
            _amap(0, codec="TrueHD Atmos", lang="spa", ch=8),
            _amap(1, codec="TrueHD Atmos", lang="eng", ch=8),
        )
        self.assertFalse(_check_reordering(session, tmap))

    def test_exclusion_triggers_direct(self):
        """Menos pistas incluidas que en el source → exclusión → True."""
        session = _session(
            _audio("Spanish", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz", "Castellano TrueHD Atmos 7.1", position=0),
        )
        tmap = _track_map(
            _amap(0, codec="TrueHD Atmos", lang="spa", ch=8),
            _amap(1, codec="DTS", lang="fre", ch=6),
        )
        self.assertTrue(_check_reordering(session, tmap))


# ── _propedit_track_edits: etiquetas/flags al stream correcto (P0) ───────

class TestPropeditTrackEdits(unittest.TestCase):
    def test_labels_map_to_correct_stream_vo_first(self):
        """REGRESIÓN P0 (la pieza que corrompía en silencio): en un disco con
        el inglés físicamente primero, la ruta propedit ponía 'Castellano…'
        sobre el stream inglés + flag_default. Ahora cada etiqueta y flag va a
        su track real por contenido."""
        session = _session(
            _audio("Spanish", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz",
                   "Castellano TrueHD Atmos 7.1", flag_default=True, position=0),
            _audio("English", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz",
                   "Inglés TrueHD Atmos 7.1", flag_default=False, position=1),
        )
        tmap = _track_map(
            _amap(0, codec="TrueHD Atmos", lang="eng", ch=8),
            _amap(1, codec="TrueHD Atmos", lang="spa", ch=8),
        )
        by_id = {tid: (label, fd) for tid, label, fd, ff in _propedit_track_edits(session, tmap)}
        # Stream español físico (id=1) → etiqueta Castellano + default=True
        self.assertEqual(by_id[1][0], "Castellano TrueHD Atmos 7.1")
        self.assertTrue(by_id[1][1])
        # Stream inglés físico (id=0) → etiqueta Inglés, sin default
        self.assertEqual(by_id[0][0], "Inglés TrueHD Atmos 7.1")
        self.assertFalse(by_id[0][1])

    def test_aligned_order_labels_correct(self):
        """Orden alineado (Castellano id=0, Inglés id=1): cada etiqueta a su id."""
        session = _session(
            _audio("Spanish", "DTS-HD Master Audio", "5.1 / 48 kHz",
                   "Castellano DTS-HD MA 5.1", flag_default=True, position=0),
            _audio("English", "DTS-HD Master Audio", "5.1 / 48 kHz",
                   "Inglés DTS-HD MA 5.1", flag_default=False, position=1),
        )
        tmap = _track_map(
            _amap(0, codec="DTS-HD Master Audio", lang="spa", ch=6),
            _amap(1, codec="DTS-HD Master Audio", lang="eng", ch=6),
        )
        by_id = {tid: label for tid, label, fd, ff in _propedit_track_edits(session, tmap)}
        self.assertEqual(by_id[0], "Castellano DTS-HD MA 5.1")
        self.assertEqual(by_id[1], "Inglés DTS-HD MA 5.1")


class TestLanguageMapCoverage(unittest.TestCase):
    """Regresión: el matcher debe reconocer idiomas fuera del antiguo subset de
    phase_e (catalán, tailandés…). El source los identifica por código ISO
    ('cat') y la pista incluida los trae en inglés ('Catalan'); _ISO639 debe
    normalizar el código al mismo nombre. Antes el subset no tenía 'cat' →
    'cat' ≠ 'catalan' → la pista se perdía en silencio (caso real: Avatar
    Fuego y Ceniza — catalán DTS + sub forzado catalán descartados)."""

    def test_catalan_audio_matches(self):
        included = [_audio("Catalan", "DTS Audio", "5.1 / 48 kHz", "Catalan DTS 5.1")]
        tmap = _track_map(_amap(10, codec="DTS", lang="cat", ch=6))
        self.assertEqual(_match_tracks_to_source(included, [10], tmap), {0: 10})

    def test_catalan_subtitle_matches(self):
        included = [_sub("Catalan", "forced", "Catalan Forzados (PGS)", flag_forced=True)]
        tmap = _track_map(_smap(16, lang="cat"))
        self.assertEqual(_match_tracks_to_source(included, [16], tmap), {0: 16})

    def test_thai_audio_matches(self):
        included = [_audio("Thai", "DTS Audio", "5.1 / 48 kHz", "Thai DTS 5.1")]
        tmap = _track_map(_amap(5, codec="DTS", lang="tha", ch=6))
        self.assertEqual(_match_tracks_to_source(included, [5], tmap), {0: 5})

    def test_avatar_es_en_cat_all_match(self):
        # Caso Avatar simplificado: spa + eng + cat → los tres mapean.
        included = [
            _audio("Spanish", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz", "Castellano TrueHD Atmos 7.1", position=0),
            _audio("English", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz", "Inglés TrueHD Atmos 7.1", position=1),
            _audio("Catalan", "DTS Audio", "5.1 / 48 kHz", "Catalan DTS 5.1", position=2),
        ]
        tmap = _track_map(
            _amap(2,  codec="TrueHD Atmos", lang="eng", ch=8),
            _amap(7,  codec="TrueHD Atmos", lang="spa", ch=8),
            _amap(10, codec="DTS",          lang="cat", ch=6),
        )
        self.assertEqual(_match_tracks_to_source(included, [2, 7, 10], tmap), {0: 7, 1: 2, 2: 10})


if __name__ == "__main__":
    unittest.main()
