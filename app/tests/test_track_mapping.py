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


def _sub(language, subtitle_type, label, *, flag_default=False, flag_forced=False,
         position=0, raw_bitrate=None, packet_count=0):
    # El matcher de Fase E decide forzado/completo por la señal ESTRUCTURAL
    # del raw (bitrate sintético de phase_a: <3 kbps = forzado), no por
    # subtitle_type. Por defecto derivamos el bitrate del subtitle_type para
    # que los tests reflejen un disco coherente; raw_bitrate permite forzar
    # una discrepancia (caso Obsession: subtitle_type='complete' en un raw
    # que estructuralmente es forzado).
    if raw_bitrate is None:
        raw_bitrate = 1.0 if subtitle_type == "forced" else 30.0
    return IncludedSubtitleTrack(
        position=position,
        raw=RawSubtitleTrack(language=language, bitrate_kbps=raw_bitrate,
                             description="", packet_count=packet_count),
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

    def test_iso639_no_es_subset_del_canonico(self):
        # Blindaje: phase_e._ISO639 debe DERIVAR del mapa canónico de phase_a,
        # no ser un subset propio. Un subset desincronizado descartaba pistas
        # (catalán, tailandés…) en silencio. Si alguien vuelve a recortarlo,
        # este test falla.
        from phases.phase_e import _ISO639
        from phases.phase_a import ISO639_TO_ENGLISH
        for code, name in ISO639_TO_ENGLISH.items():
            self.assertEqual(_ISO639.get(code), name.lower(), f"_ISO639 no cubre '{code}'")

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

    # ── Subtítulos: matching por identidad estructural del raw, no por
    #    subtitle_type (que puede venir mal) ──────────────────────────────

    def test_obsession_forced_subtitle_type_wrong_still_maps_by_structure(self):
        """Obsession 2025: la pista 'Castellano Forzados' se recuperó a mano y
        quedó con subtitle_type='complete' (el forzado que el muestreo perdió),
        pero su raw es el forzado real (bitrate 1.0, 0 paq.). La 'Castellano
        Completos' tiene el raw completo (bitrate 30.0, 4516 paq.). El matcher
        debe entregar el forzado al source forzado y el completo al completo,
        AUNQUE ambas tengan subtitle_type='complete' — decidiendo por la señal
        estructural del raw. Antes se cruzaban.

        Source (orden disco, patrón bloques eng/spa/fre + spa/fre):
          10=spa completo (primera aparición), 12=spa forzado (segunda)."""
        included = [
            # Reordenada por el usuario: el 'forzado' va primero. subtitle_type
            # MAL ('complete'), raw estructuralmente forzado (bitrate 1.0).
            _sub("Spanish", "complete", "Castellano Forzados (PGS)",
                 flag_forced=True, flag_default=True, raw_bitrate=1.0, packet_count=0),
            _sub("English", "complete", "Inglés Completos (PGS)",
                 raw_bitrate=30.0, packet_count=8730),
            _sub("Spanish", "complete", "Castellano Completos (PGS)",
                 raw_bitrate=30.0, packet_count=4516),
        ]
        # track_map en orden de disco: eng(9), spa(10), fre(11), spa(12), fre(13)
        tmap = _track_map(
            _smap(9,  lang="eng"),
            _smap(10, lang="spa"),
            _smap(11, lang="fre"),
            _smap(12, lang="spa"),
            _smap(13, lang="fre"),
        )
        result = _match_tracks_to_source(included, [9, 10, 11, 12, 13], tmap)
        # 'Castellano Forzados' (idx 0) → source forzado spa = 12
        self.assertEqual(result[0], 12)
        # 'Inglés Completos' (idx 1) → 9
        self.assertEqual(result[1], 9)
        # 'Castellano Completos' (idx 2) → source completo spa = 10
        self.assertEqual(result[2], 10)



# ── La posición en el disco: lo que distingue dos pistas «iguales» ────────

def _raw_sub(language, bitrate, packets):
    return RawSubtitleTrack(language=language, bitrate_kbps=bitrate,
                            description="", packet_count=packets)


def _incluida_sub(raw, label, *, subtitle_type="complete", flag_forced=False):
    return IncludedSubtitleTrack(
        position=0, raw=raw, language_literal=label.split()[0],
        subtitle_type=subtitle_type, label=label, flag_default=False,
        flag_forced=flag_forced, selection_reason="test")


def _raw_audio(language, codec, description, bitrate):
    return RawAudioTrack(codec=codec, language=language,
                         bitrate_kbps=bitrate, description=description)


def _incluida_audio(raw, label):
    return IncludedAudioTrack(
        position=0, raw=raw, language_literal=label.split()[0],
        codec_literal=label, label=label, flag_default=False,
        selection_reason="test")


class TestLaPosicionEnElDisco(unittest.TestCase):
    """Dentro de un idioma y una categoría, el matcher cogía el PRIMER id
    libre. Con dos pistas de la misma categoría —normal y SDH, que son las dos
    completas— recuperar la SDH y descartar la normal entregaba el contenido
    normal con la etiqueta SDH. Es The Mandalorian and Grogu: castellano
    normal 5591, SDH 23022 y forzado 248 paquetes.

    Lo que sí distingue las dos es su POSICIÓN en el disco: la pista incluida
    lleva el mismo `raw` que la Fase A sacó del `mkvmerge -J` del origen, en su
    orden. El matcher la busca ahí y toma el id que ocupa el mismo puesto entre
    las del idioma."""

    # Tres bloques: completos · SDH · forzados (el de The Mandalorian).
    ES_NORMAL = _raw_sub("Spanish", 30.0, 5591)
    EN_NORMAL = _raw_sub("English", 30.0, 6100)
    ES_SDH = _raw_sub("Spanish", 30.0, 23022)
    EN_SDH = _raw_sub("English", 30.0, 21000)
    ES_FORZ = _raw_sub("Spanish", 1.0, 248)
    EN_FORZ = _raw_sub("English", 1.0, 260)
    DISCO = [EN_NORMAL, ES_NORMAL, EN_SDH, ES_SDH, EN_FORZ, ES_FORZ]
    TMAP = _track_map(_smap(10, lang="eng"), _smap(11, lang="spa"),
                      _smap(12, lang="eng"), _smap(13, lang="spa"),
                      _smap(14, lang="eng"), _smap(15, lang="spa"))
    IDS = [10, 11, 12, 13, 14, 15]

    def test_la_sdh_recuperada_recibe_la_sdh_y_no_la_normal(self):
        incluidas = [
            _incluida_sub(self.ES_FORZ, "Castellano Forzados (PGS)",
                          subtitle_type="forced", flag_forced=True),
            _incluida_sub(self.ES_SDH, "Castellano SDH (PGS)"),
        ]
        m = _match_tracks_to_source(incluidas, self.IDS, self.TMAP,
                                    disco=self.DISCO)
        self.assertEqual(m, {0: 15, 1: 13},
                         "la SDH ha recibido el contenido de la pista normal")

    def test_las_dos_completas_cada_una_la_suya(self):
        incluidas = [_incluida_sub(self.ES_SDH, "Castellano SDH (PGS)"),
                     _incluida_sub(self.ES_NORMAL, "Castellano Completos (PGS)")]
        m = _match_tracks_to_source(incluidas, self.IDS, self.TMAP,
                                    disco=self.DISCO)
        self.assertEqual(m, {0: 13, 1: 11})

    def test_si_el_origen_no_cuadra_con_el_disco_cae_a_la_estructura(self):
        """El M2TS del reintento, o un disco que mkvmerge ve distinto: si el
        número de pistas del idioma no coincide con lo que vio la Fase A, el
        puesto no significa nada y se usa la regla de siempre."""
        incluidas = [_incluida_sub(self.ES_FORZ, "Castellano Forzados (PGS)",
                                   subtitle_type="forced")]
        tmap = _track_map(_smap(10, lang="spa"), _smap(11, lang="eng"),
                          _smap(12, lang="spa"))
        m = _match_tracks_to_source(incluidas, [10, 11, 12], tmap,
                                    disco=self.DISCO)
        self.assertEqual(m, {0: 12}, "sin el puesto, la estructura de bloques")

    def test_sin_el_disco_se_comporta_como_antes(self):
        """Sesiones sin `bdinfo_result` (o un llamador que no lo pasa)."""
        incluidas = [_incluida_sub(self.ES_SDH, "Castellano SDH (PGS)")]
        m = _match_tracks_to_source(incluidas, self.IDS, self.TMAP)
        self.assertEqual(m, {0: 11})

    def test_los_comentarios_recuperados_reciben_los_comentarios(self):
        """El mismo fallo en audio: dos AC-3 5.1 inglesas, la principal y la
        de comentarios, no se distinguen ni por códec ni por canales, y ganaba
        el id menor aunque el usuario hubiera elegido la otra."""
        principal = _raw_audio("English", "Dolby Digital Audio", "5.1 / 48 kHz", 640)
        comentarios = _raw_audio("English", "Dolby Digital Audio", "5.1 / 48 kHz", 192)
        castellano = _raw_audio("Spanish", "Dolby Digital Audio", "5.1 / 48 kHz", 640)
        disco = [principal, castellano, comentarios]
        tmap = _track_map(_amap(1, codec="AC-3", lang="eng", ch=6),
                          _amap(2, codec="AC-3", lang="spa", ch=6),
                          _amap(3, codec="AC-3", lang="eng", ch=6))
        avisos = []
        m = _match_tracks_to_source(
            [_incluida_audio(castellano, "Castellano DD 5.1"),
             _incluida_audio(comentarios, "Inglés DD 5.1")],
            [1, 2, 3], tmap, avisos, disco=disco)
        self.assertEqual(m, {0: 2, 1: 3})
        self.assertEqual(avisos, [], "con el puesto no hay empate que avisar")

    def test_un_puesto_con_otro_codec_no_se_acepta(self):
        """El puesto se comprueba contra el códec: si no casa, el disco que se
        está leyendo no es el que vio la Fase A."""
        dts = _raw_audio("English", "DTS-HD Master Audio", "5.1 / 48 kHz", 3000)
        dd = _raw_audio("English", "Dolby Digital Audio", "5.1 / 48 kHz", 640)
        tmap = _track_map(_amap(1, codec="AC-3", lang="eng", ch=6),
                          _amap(2, codec="DTS-HD Master Audio", lang="eng", ch=6))
        m = _match_tracks_to_source([_incluida_audio(dts, "Inglés DTS-HD MA 5.1")],
                                    [1, 2], tmap, disco=[dts, dd])
        self.assertEqual(m, {0: 2})

    def test_un_puesto_con_otros_canales_no_se_acepta(self):
        """Si el origen trae las pistas del idioma en otro orden que el que
        vio la Fase A —el M2TS del reintento no tiene por qué respetar el del
        MPLS—, coinciden idioma, número y códec, y el puesto señala la 2.0
        cuando la incluida es la 5.1. Los canales lo desmienten (revisión con
        Codex de la v3.4.0)."""
        dd51 = _raw_audio("Spanish", "Dolby Digital Audio", "5.1 / 48 kHz", 640)
        dd20 = _raw_audio("Spanish", "Dolby Digital Audio", "2.0 / 48 kHz", 192)
        tmap = _track_map(_amap(1, codec="AC-3", lang="spa", ch=2),
                          _amap(2, codec="AC-3", lang="spa", ch=6))
        m = _match_tracks_to_source([_incluida_audio(dd51, "Castellano DD 5.1")],
                                    [1, 2], tmap, disco=[dd51, dd20])
        self.assertEqual(m, {0: 2}, "la DD 5.1 recibió el contenido de la 2.0")


class TestElLpcmEncuentraSuOrigen(unittest.TestCase):
    """La Fase A traduce el `PCM` de mkvmerge a «LPCM Audio» y el emparejado
    de códecs no tenía ese par: una pista LPCM incluida no encontraba su
    origen y se quedaba fuera del MKV."""

    def test_lpcm(self):
        included = [_audio("English", "LPCM Audio", "2.0 / 48 kHz", "Inglés LPCM 2.0")]
        tmap = _track_map(_amap(1, codec="PCM", lang="eng", ch=2))
        self.assertEqual(_match_tracks_to_source(included, [1], tmap), {0: 1})

    def test_cada_codec_de_la_tabla_encuentra_el_suyo(self):
        """Todo códec que la Fase A puede producir tiene que casar con el de
        mkvmerge del que sale. Es la tabla entera, no una lista aparte."""
        from phases.phase_a import MKVMERGE_CODEC_TO_BDINFO
        from phases.phase_e import _codec_matches
        for mkv, bdinfo in MKVMERGE_CODEC_TO_BDINFO.items():
            self.assertTrue(_codec_matches(bdinfo.lower(), mkv.lower()),
                            f"«{bdinfo}» no casa con el «{mkv}» de mkvmerge")


if __name__ == "__main__":
    unittest.main()
