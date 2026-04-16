"""
phase_b.py — Fase B: Aplicación de reglas automáticas

Responsabilidad:
  Transformar el BDInfoResult (salida de Fase A) en las listas de pistas
  incluidas/descartadas con sus literales de etiqueta, flags y razones,
  más los capítulos auto-generados y el nombre inicial del MKV.

─────────────────────────────────────────────────────────────────────
REGLAS DE AUDIO (spec §5.1)
─────────────────────────────────────────────────────────────────────

Idiomas seleccionados:
  - Siempre Castellano (Spanish).
  - VO (idioma de la primera pista del disco) si es distinta del castellano.
  - Resto de idiomas → descartados.

Por idioma, solo la pista de mayor calidad según la jerarquía:
  TrueHD Atmos (1) > DD+ Atmos (2) > DTS-HD MA (3) > DTS (4) > DD (5)
  Si hay empate de codec, gana la de mayor bitrate.

Flags:
  - Castellano → flag_default=True, flag_forced=False.
  - VO → flag_default=False, flag_forced=False.

Literal de etiqueta: ``{Idioma} {Codec} {Canales}``
  Ej: ``Castellano TrueHD Atmos 7.1``, ``Inglés DTS-HD MA 5.1``
  Si audio_dcp=True y el codec es TrueHD Atmos: añade ``(DCP 9.1.6)``.

─────────────────────────────────────────────────────────────────────
REGLAS DE SUBTÍTULOS (spec §5.2)
─────────────────────────────────────────────────────────────────────

Regla 1: Descartar pistas con código de idioma ``qad`` (Audio Description).

Clasificación Forma A (por idioma, si hay ≥ 2 pistas PGS):
  - bitrate < 3 kbps → Forzados (flag_forced=True).
  - bitrate ≥ 3 kbps → Completos (flag_forced=False).
  (Los bitrates son sintéticos: 1.0 para forzados, 30.0 para completos,
  asignados por la heurística de estructura de bloques en phase_a.)

Orden de inclusión:
  1. Forzados Castellano (flag_default=True, flag_forced=True)
  2. Completos VO
  3. Completos Castellano (si VO ≠ Castellano)
  4. Forzados VO (si VO ≠ Castellano)
  5. Completos Inglés   (si VO ≠ Inglés)

Labels: ``{Idioma} Forzados (PGS)``, ``{Idioma} Completos (PGS)``

─────────────────────────────────────────────────────────────────────
CAPÍTULOS AUTOMÁTICOS (spec §5.3) — solo fallback
─────────────────────────────────────────────────────────────────────

Los capítulos reales se extraen en Fase A (mkvmerge + mkvextract).
Esta función solo se usa como fallback cuando el disco no tiene
capítulos: genera automáticos cada 10 min desde el primer intervalo.

─────────────────────────────────────────────────────────────────────
NOMBRE DEL MKV (spec §5.4)
─────────────────────────────────────────────────────────────────────

Patrón: ``{Título} ({Año})[tags].mkv``
Tags opcionales (añadidos en este orden):
  - ``[DV FEL]``    si has_fel=True
  - ``[Audio DCP]`` si audio_dcp=True

Ej: ``El Rey de Reyes (2025) [DV FEL] [Audio DCP].mkv``

Título y año se extraen del nombre del ISO con el patrón
``{Título} ({Año}) [...]``. Si el nombre no sigue el patrón, se usa
el stem completo como título y ``0000`` como año.

Ref: spec §5.1, §5.2, §5.3, §5.4
"""
import math
import re
from pathlib import Path

from models import (
    BDInfoResult,
    Chapter,
    DiscardedTrack,
    IncludedAudioTrack,
    IncludedSubtitleTrack,
    RawAudioTrack,
    RawSubtitleTrack,
    Session,
)


# ── Tablas de conversión (spec §5.1.1, §5.2.1) ───────────────────────────────

LANGUAGE_MAP: dict[str, str] = {
    "spanish": "Castellano",
    "english": "Inglés",
    "french": "Francés",
    "german": "Alemán",
    "italian": "Italiano",
    "japanese": "Japonés",
    "portuguese": "Portugués",
    "chinese": "Chino",
    "korean": "Coreano",
    "dutch": "Holandés",
    "russian": "Ruso",
    "polish": "Polaco",
    "czech": "Checo",
    "hungarian": "Húngaro",
    "swedish": "Sueco",
    "norwegian": "Noruego",
    "danish": "Danés",
    "finnish": "Finlandés",
    "turkish": "Turco",
}

# Prioridad de codec: menor número = mayor calidad (spec §5.1.6)
CODEC_PRIORITY: dict[str, int] = {
    "truehd_atmos": 1,
    "ddplus_atmos": 2,
    "dts_hd_ma": 3,
    "dts": 4,
    "dd": 5,
}


def apply_rules(bdinfo: BDInfoResult, iso_path: str, audio_dcp: bool) -> dict:
    """
    Punto de entrada principal de la Fase B.

    Aplica todas las reglas automáticas sobre el BDInfoResult y devuelve
    un diccionario con los campos que se fusionarán en la sesión:

        included_tracks  — pistas seleccionadas (audio + subtítulos) en orden.
        discarded_tracks — pistas descartadas con su razón.
        mkv_name         — nombre propuesto para el fichero de salida.
        mkv_name_manual  — False (el nombre es automático en Fase B).
        vo_warning       — advertencia si la VO no puede determinarse (vacío si OK).

    Args:
        bdinfo:    Resultado de parsear el report de BDInfoCLI (Fase A).
        iso_path:  Ruta al ISO; se usa para extraer título y año del nombre.
        audio_dcp: True si el nombre del ISO contiene el tag 'Audio DCP'.

    Returns:
        Dict directamente asignable a los campos de un Session.
    """
    title, year = _extract_title_year(iso_path)
    vo_language, vo_warning = _detect_vo_language(bdinfo.audio_tracks)

    included_audio, discarded_audio = _select_audio_tracks(bdinfo.audio_tracks, vo_language, audio_dcp)
    included_subs, discarded_subs = _select_subtitle_tracks(bdinfo.subtitle_tracks, vo_language)

    # Asignar posiciones: video implícita (makemkvcon la incluye siempre), luego audio, luego subs
    all_included: list[IncludedAudioTrack | IncludedSubtitleTrack] = []
    for i, t in enumerate(included_audio):
        t.position = i
        all_included.append(t)
    for i, t in enumerate(included_subs):
        t.position = len(included_audio) + i
        all_included.append(t)

    all_discarded = discarded_audio + discarded_subs

    mkv_name = _build_mkv_name(title, year, bdinfo.has_fel, audio_dcp)

    return {
        "included_tracks": all_included,
        "discarded_tracks": all_discarded,
        "mkv_name": mkv_name,
        "mkv_name_manual": False,
        "vo_warning": vo_warning,
    }


# ── Detección de VO ──────────────────────────────────────────────────────────

def _detect_vo_language(tracks: list[RawAudioTrack]) -> tuple[str, str]:
    """
    Determina el idioma de la VO según las reglas actualizadas.

    Regla principal:  la VO es siempre English.
    Fallback 1:       si no hay inglés, la VO es Spanish.
    Fallback 2:       si no hay inglés ni español, se usa el idioma de la
                      primera pista como emergencia y se genera una advertencia
                      para que el usuario ajuste manualmente.

    Returns:
        Tupla (vo_language_lower, warning_message).
        warning_message es '' si la VO se determinó sin ambigüedad.
    """
    langs_lower = [t.language.lower() for t in tracks]

    if "english" in langs_lower:
        return "english", ""

    if "spanish" in langs_lower:
        return "spanish", (
            "Fallback VO: el disco no contiene pistas en inglés. "
            "Se usa Castellano como VO. Verifica que la selección de pistas es correcta."
        )

    # Emergencia: ningún idioma esperado
    first_lang = langs_lower[0] if langs_lower else "english"
    first_display = tracks[0].language if tracks else "English"
    return first_lang, (
        f"⚠️ No se puede determinar la VO automáticamente: el disco no contiene "
        f"pistas en inglés ni en español. Se ha usado '{first_display}' como VO provisional. "
        f"Revisa las pistas incluidas y ajusta manualmente."
    )


# ── Audio (spec §5.1) ─────────────────────────────────────────────────────────

def _select_audio_tracks(
    tracks: list[RawAudioTrack],
    vo_language: str,
    audio_dcp: bool,
) -> tuple[list[IncludedAudioTrack], list[DiscardedTrack]]:
    """
    Selecciona las pistas de audio según las reglas de la spec §5.1.

    Algoritmo:
      1. Agrupar pistas por idioma normalizado (lower-case).
      2. Descartar todos los idiomas que no sean Castellano ni VO.
      3. Para Castellano y VO (en ese orden), elegir la pista de mayor
         calidad usando CODEC_PRIORITY; si empatan, mayor bitrate.
      4. Descartar las pistas restantes del mismo idioma.

    Args:
        tracks:      Lista de pistas raw de BDInfo (en orden del disco).
        vo_language: Idioma de la primera pista del disco (VO); puede ser
                     'Spanish', 'English', 'French', etc.
        audio_dcp:   Si True, añade el sufijo '(DCP 9.1.6)' a TrueHD Atmos.

    Returns:
        Tupla (incluidas, descartadas). Las incluidas están en orden
        Castellano → VO, listas para asignar posición en apply_rules.
    """
    vo_norm = vo_language.lower()
    target_langs = {"spanish"}
    if vo_norm != "spanish":
        target_langs.add(vo_norm)

    # Agrupar por idioma
    by_lang: dict[str, list[RawAudioTrack]] = {}
    for t in tracks:
        lang_norm = t.language.lower()
        if lang_norm not in by_lang:
            by_lang[lang_norm] = []
        by_lang[lang_norm].append(t)

    included: list[IncludedAudioTrack] = []
    discarded: list[DiscardedTrack] = []

    # Idiomas descartados
    for lang_norm, lang_tracks in by_lang.items():
        if lang_norm not in target_langs:
            lang_lit = _language_literal(lang_norm)
            for t in lang_tracks:
                discarded.append(DiscardedTrack(
                    track_type="audio",
                    raw=t,
                    discard_reason=f"Descartada: idioma {lang_lit} no es Castellano ni VO ({_language_literal(vo_language.lower())})",
                ))

    # Idiomas incluidos: seleccionar mejor pista de cada uno
    def _select_best(lang_tracks: list[RawAudioTrack]) -> tuple[RawAudioTrack, list[RawAudioTrack]]:
        ranked = sorted(lang_tracks, key=lambda t: (_codec_priority(t), -t.bitrate_kbps))
        return ranked[0], ranked[1:]

    # Castellano primero, luego VO
    order = ["spanish"]
    if vo_norm != "spanish":
        order.append(vo_norm)

    for lang_norm in order:
        if lang_norm not in by_lang:
            continue
        best, rest = _select_best(by_lang[lang_norm])
        lang_lit = _language_literal(lang_norm)
        is_castellano = lang_norm == "spanish"
        codec_lit = _codec_literal(best, audio_dcp and is_castellano)
        label = f"{lang_lit} {codec_lit}"

        # Razón de selección
        if is_castellano:
            reason = f"Seleccionada: mejor calidad para {best.language}. {_quality_ladder_text(best)}"
        else:
            reason = "Seleccionada: VO (idioma de la primera pista del disco)"

        included.append(IncludedAudioTrack(
            position=0,  # se reasigna después
            raw=best,
            language_literal=lang_lit,
            codec_literal=codec_lit,
            label=label,
            flag_default=is_castellano,
            flag_forced=False,
            selection_reason=reason,
        ))

        # Descartar el resto del mismo idioma
        best_codec_lit = _codec_literal(best, False)
        for t in rest:
            t_codec_lit = _codec_literal(t, False)
            discarded.append(DiscardedTrack(
                track_type="audio",
                raw=t,
                discard_reason=f"Descartada: segunda pista {_language_literal(t.language.lower())}. Menor calidad: {t_codec_lit} < {best_codec_lit}",
            ))

    return included, discarded


def _codec_priority(track: RawAudioTrack) -> int:
    """
    Devuelve el valor numérico de prioridad de codec para ordenar pistas.

    Menor número = mayor calidad. Los codecs desconocidos reciben prioridad 99
    (más baja) para que siempre queden al final. Se usa como clave de sort
    primaria en _select_best; el bitrate actúa como desempate secundario.
    """
    key = _codec_key(track)
    return CODEC_PRIORITY.get(key, 99)


def _codec_key(track: RawAudioTrack) -> str:
    """Identifica el codec normalizado de una pista (spec §5.1.2, §5.1.6)."""
    codec = track.codec.lower()
    desc = track.description.lower()
    # Detección definitiva via MediaInfo Format_Commercial (si disponible)
    fc = getattr(track, "format_commercial", "").lower()
    if fc:
        if "atmos" in fc and "truehd" in fc:
            return "truehd_atmos"
        if "truehd" in fc and "atmos" not in fc:
            return "truehd"
        if "atmos" in fc and ("digital plus" in fc or "e-ac-3" in fc):
            return "ddplus_atmos"
        if "digital plus" in fc or "e-ac-3" in fc:
            return "ddplus"
        if "dts-hd master" in fc or "dts-hd ma" in fc:
            return "dts_hd_ma"
        if "dts" in fc:
            return "dts"
        if "dolby digital" in fc and "plus" not in fc:
            return "dd"
    # Fallback: heurística por nombre de codec (sin MediaInfo)
    if "truehd" in codec and "atmos" in codec:
        return "truehd_atmos"
    if "truehd" in codec:
        return "truehd"
    if "digital plus" in codec and "atmos" in desc:
        return "ddplus_atmos"
    if "digital plus" in codec:
        return "ddplus"
    if "dts-hd master" in codec or ("dts" in codec and "hd" in codec and "master" in codec):
        return "dts_hd_ma"
    if "dts" in codec and "hd" not in codec:
        return "dts"
    if "dolby digital" in codec and "plus" not in codec:
        return "dd"
    return "unknown"


def _codec_literal(track: RawAudioTrack, audio_dcp: bool) -> str:
    """Construye el literal de codec con canales (spec §5.1.2, §5.1.3, §5.1.4, §5.1.5)."""
    channels = _extract_channels(track.description)
    key = _codec_key(track)

    base_map = {
        "truehd_atmos": f"TrueHD Atmos {channels}",
        "truehd":       f"TrueHD {channels}",
        "ddplus_atmos": f"DD+ Atmos {channels}",
        "ddplus":       f"DD+ {channels}",
        "dts_hd_ma":    f"DTS-HD MA {channels}",
        "dts":          f"DTS {channels}",
        "dd":           f"DD {channels}",
    }
    lit = base_map.get(key, f"{track.codec} {channels}")

    # Sufijo DCP (spec §5.1.4)
    if audio_dcp and key == "truehd_atmos":
        lit += " (DCP 9.1.6)"

    return lit


def _extract_channels(description: str) -> str:
    """
    Extrae canales del primer campo de Description (spec §5.1.3).
    "7.1+11 objects / 48 kHz / ..." → "7.1"
    "7.1-Atmos / ..." → "7.1"
    "5.1 / ..." → "5.1"
    """
    part = description.split("/")[0].strip()
    if "+" in part:
        part = part.split("+")[0].strip()
    if "-Atmos" in part or "-atmos" in part:
        part = re.split(r"-[Aa]tmos", part)[0].strip()
    return part or "?"


def _language_literal(lang_norm: str) -> str:
    """
    Convierte un código de idioma normalizado (lower-case) al literal en español.

    Usa LANGUAGE_MAP; si el idioma no está en la tabla, capitaliza el código.
    Ej: 'spanish' → 'Castellano', 'english' → 'Inglés', 'thai' → 'Thai'.
    """
    return LANGUAGE_MAP.get(lang_norm, lang_norm.capitalize())


def _quality_ladder_text(track: RawAudioTrack) -> str:
    """Devuelve el texto de la jerarquía de calidad de audio para la UI."""
    return "TrueHD Atmos > DD+ Atmos > DTS-HD MA > DTS > DD"


# ── Subtítulos (spec §5.2) ────────────────────────────────────────────────────

def _select_subtitle_tracks(
    tracks: list[RawSubtitleTrack],
    vo_language: str,
) -> tuple[list[IncludedSubtitleTrack], list[DiscardedTrack]]:
    """
    Selecciona las pistas de subtítulos según las reglas de la spec §5.2.

    Algoritmo:
      1. Descartar pistas con código de idioma 'qad' (Audio Description).
      2. Por idioma con ≥ 2 pistas PGS, clasificar en Forzados (Forma A,
         bitrate < 3 kbps) y Completos (bitrate ≥ 3 kbps).
      3. Incluir en el orden definido:
           1. Forzados Castellano (default=True, forced=True)
           2. Completos VO
           3. Completos Castellano (si VO ≠ Castellano)
           4. Forzados VO (si VO ≠ Castellano)
           5. Completos Inglés (si VO ≠ Inglés)
      4. Descartar los idiomas que queden fuera.

    Args:
        tracks:      Lista de pistas de subtítulos raw de BDInfo.
        vo_language: Idioma VO (primera pista de audio del disco).

    Returns:
        Tupla (incluidas, descartadas) en el orden final de la spec.
    """
    vo_norm = vo_language.lower()

    included: list[IncludedSubtitleTrack] = []
    discarded: list[DiscardedTrack] = []

    # Descartar Audio Description (spec §5.2.2 Regla 1: código qad)
    valid_tracks = []
    for t in tracks:
        if t.language.lower() == "qad":
            discarded.append(DiscardedTrack(
                track_type="subtitle",
                raw=t,
                discard_reason="Descartada: código de idioma qad (estándar ISO 639 para Audio Description)",
            ))
        else:
            valid_tracks.append(t)

    # Agrupar por idioma
    by_lang: dict[str, list[RawSubtitleTrack]] = {}
    for t in valid_tracks:
        lang = t.language.lower()
        by_lang.setdefault(lang, []).append(t)

    def _classify_lang(lang_tracks: list[RawSubtitleTrack]) -> tuple[
        RawSubtitleTrack | None, RawSubtitleTrack | None, list[RawSubtitleTrack]
    ]:
        """
        Clasifica las pistas de un idioma en (forzados, completos, sobrantes).
        Forma A: si hay ≥2 pistas y una tiene bitrate < 3 kbps → es la de forzados.
        Las sobrantes son pistas que no se clasifican como forzada ni completa
        principal — deben descartarse explícitamente para que nunca desaparezcan.
        """
        if len(lang_tracks) >= 2:
            low = [t for t in lang_tracks if t.bitrate_kbps < 3.0]
            high = [t for t in lang_tracks if t.bitrate_kbps >= 3.0]
            if low:
                forced = low[0]
                complete = max(high, key=lambda t: t.bitrate_kbps) if high else None
                used = {id(forced)}
                if complete:
                    used.add(id(complete))
                leftover = [t for t in lang_tracks if id(t) not in used]
                return forced, complete, leftover
        # Una sola pista o sin forzados → completos
        if lang_tracks:
            return None, lang_tracks[0], lang_tracks[1:]
        return None, None, []

    # Clasificar pistas por idioma (forzados, completos, sobrantes)
    classified: dict[str, tuple[RawSubtitleTrack | None, RawSubtitleTrack | None]] = {}
    target_langs = {"spanish"}
    if vo_norm != "spanish":
        target_langs.add(vo_norm)
    if "english" not in target_langs:
        target_langs.add("english")

    for lang_norm in target_langs:
        if lang_norm in by_lang:
            forced, complete, leftover = _classify_lang(by_lang[lang_norm])
            classified[lang_norm] = (forced, complete)
            # Descartar pistas sobrantes del mismo idioma
            for t in leftover:
                discarded.append(DiscardedTrack(
                    track_type="subtitle",
                    raw=t,
                    discard_reason=f"Descartada: pista adicional {_language_literal(lang_norm)} (ya incluida la mejor de cada tipo)",
                ))

    # Orden de inclusión:
    # 1. Forzados Castellano (forced+default)
    # 2. Completos VO (English si es VO)
    # 3. Completos Castellano
    # 4. Forzados VO (English si es VO)
    # Si VO == Spanish, se adapta para no duplicar.
    ordered_entries: list[tuple[str, str]] = []  # (lang_norm, "forced"|"complete")

    # 1. Forzados Castellano
    ordered_entries.append(("spanish", "forced"))
    # 2. Completos VO
    ordered_entries.append((vo_norm, "complete"))
    # 3. Completos Castellano (si VO no es castellano, para no duplicar)
    if vo_norm != "spanish":
        ordered_entries.append(("spanish", "complete"))
    # 4. Forzados VO (si VO no es castellano)
    if vo_norm != "spanish":
        ordered_entries.append((vo_norm, "forced"))
    # 5. Completos Inglés (si VO no es inglés, incluir inglés como extra)
    if vo_norm != "english":
        ordered_entries.append(("english", "complete"))

    # Emitir pistas en el orden definido
    seen: set[tuple[str, str]] = set()
    for lang_norm, track_type in ordered_entries:
        key = (lang_norm, track_type)
        if key in seen:
            continue
        seen.add(key)
        if lang_norm not in classified:
            continue
        forced_track, complete_track = classified[lang_norm]
        lang_lit = _language_literal(lang_norm)
        is_castellano = lang_norm == "spanish"

        if track_type == "forced" and forced_track:
            flag_default = is_castellano
            reason_forced = (
                f"Forzados Forma A: bitrate {forced_track.bitrate_kbps:.3f} kbps < umbral 3 kbps. "
                f"Pista completa {lang_norm.capitalize()} presente"
                + (f" con bitrate {complete_track.bitrate_kbps:.3f} kbps" if complete_track else "")
            )
            included.append(IncludedSubtitleTrack(
                position=0,
                raw=forced_track,
                language_literal=lang_lit,
                subtitle_type="forced",
                label=f"{lang_lit} Forzados (PGS)",
                flag_default=flag_default,
                flag_forced=True,
                selection_reason=reason_forced + (". flag default=yes: primera pista de subtítulos forzados en castellano" if flag_default else ""),
            ))

        if track_type == "complete" and complete_track:
            reason_complete = f"Completos: {'única pista' if not forced_track else 'pista completa'} para {lang_norm.capitalize()}"
            included.append(IncludedSubtitleTrack(
                position=0,
                raw=complete_track,
                language_literal=lang_lit,
                subtitle_type="complete",
                label=f"{lang_lit} Completos (PGS)",
                flag_default=False,
                flag_forced=False,
                selection_reason=reason_complete,
            ))

    # Descartar idiomas que no son target
    for lang_norm, lang_tracks in by_lang.items():
        if lang_norm not in target_langs:
            for t in lang_tracks:
                discarded.append(DiscardedTrack(
                    track_type="subtitle",
                    raw=t,
                    discard_reason=f"Descartada: idioma {_language_literal(lang_norm)} no es Castellano, VO ({_language_literal(vo_language.lower())}) ni Inglés",
                ))

    # Descartar pistas clasificadas de idiomas target que no se incluyeron
    # (ej: forzados de un idioma sin slot en ordered_entries)
    included_raws = {id(t.raw) for t in included}
    discarded_raws = {id(t.raw) for t in discarded}
    for lang_norm in target_langs:
        if lang_norm not in classified:
            continue
        forced_track, complete_track = classified[lang_norm]
        for t, tipo in [(forced_track, "forzado"), (complete_track, "completo")]:
            if t and id(t) not in included_raws and id(t) not in discarded_raws:
                discarded.append(DiscardedTrack(
                    track_type="subtitle",
                    raw=t,
                    discard_reason=f"Descartada: pista {tipo} {_language_literal(lang_norm)} sin posición asignada en el orden de inclusión",
                ))

    return included, discarded


# ── Capítulos (spec §5.3) ─────────────────────────────────────────────────────

def generate_auto_chapters(duration_seconds: float, interval_seconds: int = 600) -> list[Chapter]:
    """
    Genera una lista de capítulos automáticos espaciados uniformemente (spec §5.3).

    Se crea un capítulo cada ``interval_seconds`` segundos empezando en 00:00:00.000.
    El último capítulo es el que cae justo antes de que ``t`` supere la duración
    total, por lo que nunca se genera un capítulo en el punto exacto del final.

    Args:
        duration_seconds: Duración total de la película en segundos.
        interval_seconds: Intervalo entre capítulos (por defecto 600 = 10 min).

    Returns:
        Lista de Chapter con número correlativo, timestamp y nombre
        ``Capítulo 01``, ``Capítulo 02``, etc.
    """
    chapters = []
    t = float(interval_seconds)  # Empieza en el primer intervalo, nunca en 00:00
    num = 1
    while t < duration_seconds:
        chapters.append(Chapter(
            number=num,
            timestamp=_seconds_to_timestamp(t),
            name=f"Capítulo {num:02d}",
        ))
        t += interval_seconds
        num += 1
    return chapters


def _seconds_to_timestamp(seconds: float) -> str:
    """
    Convierte segundos (float) al formato de timestamp de capítulo Matroska.

    Formato de salida: ``HH:MM:SS.mmm`` (p.ej. ``01:32:45.500``).
    """
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    ms = int((s - int(s)) * 1000)
    return f"{h:02d}:{m:02d}:{int(s):02d}.{ms:03d}"


# ── Nombre del MKV (spec §5.4) ────────────────────────────────────────────────

def _extract_title_year(iso_path: str) -> tuple[str, str]:
    """
    Extrae el título y el año del nombre del fichero ISO.

    Busca el patrón ``{Título} ({Año})`` en el stem del nombre. Si el nombre
    no sigue el patrón (p.ej. no tiene año entre paréntesis), devuelve el
    stem completo como título y '0000' como año.

    Ej: 'The Brutalist (2024) [DV FEL].iso' → ('The Brutalist', '2024')
        'pelicula_sin_anno.iso'              → ('pelicula_sin_anno', '0000')
    """
    stem = Path(iso_path).stem
    m = re.match(r"^(.+?)\s*\((\d{4})\)", stem)
    if m:
        return m.group(1).strip(), m.group(2)
    return stem, "0000"


def build_mkv_name(title: str, year: str, has_fel: bool, audio_dcp: bool) -> str:
    """
    API pública para construir el nombre del MKV desde componentes externos.

    Permite que main.py regenere el nombre cuando el usuario cambia los flags
    FEL o DCP en la pantalla de revisión (endpoint /recalculate-name).

    Args:
        title:     Título de la película (sin año ni extensión).
        year:      Año de estreno como string de 4 dígitos.
        has_fel:   True si el disco tiene Dolby Vision FEL.
        audio_dcp: True si el audio tiene mezcla DCP.

    Returns:
        Nombre de fichero con extensión .mkv y tags opcionales.
    """
    return _build_mkv_name(title, year, has_fel, audio_dcp)


def _build_mkv_name(title: str, year: str, has_fel: bool, audio_dcp: bool) -> str:
    """
    Construye el nombre del MKV según la spec §5.4.

    Patrón: ``{Título} ({Año})[tags].mkv``
    Tags: ``[DV FEL]`` si has_fel, ``[Audio DCP]`` si audio_dcp, en ese orden.
    """
    name = f"{title} ({year})"
    if has_fel:
        name += " [DV FEL]"
    if audio_dcp:
        name += " [Audio DCP]"
    return name + ".mkv"
