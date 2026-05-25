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
    "dts_hd_ma":    3,
    "dts":          4,
    "truehd":       5,  # TrueHD sin Atmos es raro, pero por si aparece
    "ddplus":       6,  # DD+ sin Atmos (2.0 ó 5.1 clásico)
    "dd":           7,
}


def apply_rules(
    bdinfo: BDInfoResult,
    iso_path: str,
    audio_dcp: bool,
    audio_mode: str = "filtered",
    subtitle_mode: str = "filtered",
) -> dict:
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
        bdinfo:        Resultado de parsear el report de BDInfoCLI (Fase A).
        iso_path:      Ruta al ISO; se usa para extraer título y año del nombre.
        audio_dcp:     True si el nombre del ISO contiene el tag 'Audio DCP'.
        audio_mode:    'filtered' (solo Castellano + VO) o 'keep_all' (todas, con labels).
        subtitle_mode: 'filtered' (Castellano + VO + Inglés) o 'keep_all' (todas, con labels).

    Returns:
        Dict directamente asignable a los campos de un Session.
    """
    title, year = _extract_title_year(iso_path)
    vo_language, vo_warning = _detect_vo_language(bdinfo.audio_tracks)

    included_audio, discarded_audio = _select_audio_tracks(
        bdinfo.audio_tracks, vo_language, audio_dcp, mode=audio_mode,
    )
    included_subs, discarded_subs = _select_subtitle_tracks(
        bdinfo.subtitle_tracks, vo_language, mode=subtitle_mode,
    )

    # Asignar posiciones: video implícita (makemkvcon la incluye siempre), luego audio, luego subs
    all_included: list[IncludedAudioTrack | IncludedSubtitleTrack] = []
    for i, t in enumerate(included_audio):
        t.position = i
        all_included.append(t)
    for i, t in enumerate(included_subs):
        t.position = len(included_audio) + i
        all_included.append(t)

    # Orden de discarded: por posición original en el disco. Antes el
    # orden era el de procesamiento del código (idiomas no-target →
    # segundas pistas de target langs), que daba secuencias confusas
    # tipo "Francés, Alemán, Italiano…, Español 2.0 (id=6), Inglés 5.1
    # (id=2)" donde el orden no se correspondía con el del log raw de
    # mkvmerge. Sort estable por (track_type, índice en bdinfo).
    audio_disc_order = {id(t): i for i, t in enumerate(bdinfo.audio_tracks)}
    sub_disc_order = {id(t): i for i, t in enumerate(bdinfo.subtitle_tracks)}

    def _disc_order_key(d: DiscardedTrack) -> tuple[int, int]:
        is_sub = d.track_type == "subtitle"
        lookup = sub_disc_order if is_sub else audio_disc_order
        return (1 if is_sub else 0, lookup.get(id(d.raw), 9999))

    all_discarded = sorted(discarded_audio + discarded_subs, key=_disc_order_key)

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
    mode: str = "filtered",
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

    # ── Modo "keep_all": incluir TODAS las pistas con labels generados ──
    if mode == "keep_all":
        included_all: list[IncludedAudioTrack] = []
        best_spanish_codec_key: str | None = None
        for t in tracks:
            lang_norm = t.language.lower()
            lang_lit = _language_literal(lang_norm)
            is_castellano = lang_norm == "spanish"
            codec_lit = _codec_literal(t, audio_dcp and is_castellano)
            label = f"{lang_lit} {codec_lit}"
            # Default solo para la primera mejor pista de castellano encontrada
            flag_default = False
            if is_castellano and best_spanish_codec_key is None:
                best_spanish_codec_key = _codec_key(t)
                flag_default = True
            included_all.append(IncludedAudioTrack(
                position=0,
                raw=t,
                language_literal=lang_lit,
                codec_literal=codec_lit,
                label=label,
                flag_default=flag_default,
                flag_forced=False,
                selection_reason=f"Modo «mantener todas»: conservada y etiquetada automáticamente ({label})",
            ))
        return included_all, []

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

        # Detección de ambigüedad: SIEMPRE que haya 2+ pistas del mismo
        # idioma target, marcamos ambiguo. El texto se adapta según
        # similitud técnica:
        #   · Calidades similares (mismo codec + bitrate ±15%): aviso
        #     directo "España vs Latam con misma calidad — revisa".
        #   · Calidades distintas: aviso especial — la heurística eligió
        #     por mayor calidad técnica, pero la PRIMERA del disco
        #     puede ser la que el usuario quería (caso típico: España
        #     dub original DD 2.0 + Latam remix DD 5.1; el orden del
        #     disco es el signal informativo).
        # Sin esta ampliación el caso GoT (DD 2.0 + DD 5.1, ratio 42%)
        # se nos escapaba porque mi filtro de ±15% lo descartaba.
        if rest:
            # Identificar la "primera del disco" entre best y rest, para
            # avisar al usuario si la elegida NO es la primera.
            all_lang_tracks = by_lang[lang_norm]  # ya en orden del disco
            first_of_disc = all_lang_tracks[0]
            picked_is_first = best is first_of_disc
            best_codec_lit_for_msg = _codec_literal(best, False)

            similar_quality = [t for t in rest if _is_audio_ambiguous_with(best, t)]
            different_quality = [t for t in rest if not _is_audio_ambiguous_with(best, t)]

            # Solo construimos aviso si hay caso ambiguo real:
            #   - calidades similares (España/Latam con mismo codec), o
            #   - calidades distintas Y la elegida NO es la primera (caso
            #     España DD 2.0 + Latam DD 5.1: heurística eligió Latam
            #     por más canales, pero la primera del disco es la otra).
            # Si la elegida ES la primera + hay otras de menor calidad,
            # no avisamos: la heurística está siendo coherente (mejor
            # codec + primera del disco = doble señal positiva).
            # El aviso "different_quality + not first" solo aplica a
            # Castellano: el signal "primera del disco = dub original
            # España, segunda = remix Latam" es específico de los UHD
            # españoles. Para VO (Inglés u otros) la primera DD 5.1 + un
            # TrueHD Atmos posterior es solo orden de disco; el codec
            # priority es totalmente fiable. Sin este guard, todos los
            # discos UHD con TrueHD Atmos + DD 5.1 inglés disparaban un
            # falso positivo mencionando "España vs Latam".
            parts_included = []
            if similar_quality:
                if is_castellano:
                    parts_included.append(
                        f"Hay {len(similar_quality)} pista{'s' if len(similar_quality) > 1 else ''} "
                        f"{lang_lit} con calidad muy parecida (mismo codec, bitrate similar). "
                        f"Casos típicos: España vs Latinoamérica con misma calidad."
                    )
                else:
                    parts_included.append(
                        f"Hay {len(similar_quality)} pista{'s' if len(similar_quality) > 1 else ''} "
                        f"{lang_lit} con calidad muy parecida (mismo codec, bitrate similar). "
                        f"Podría ser una mezcla alternativa o comentarios del director."
                    )
            if different_quality and not picked_is_first and is_castellano:
                first_codec_lit = _codec_literal(first_of_disc, False)
                parts_included.append(
                    f"La primera pista {lang_lit} del disco es {first_codec_lit} (descartada "
                    f"por menor calidad técnica frente a {best_codec_lit_for_msg}). En algunos "
                    f"discos esto significa que la primera es España (dub original) y la "
                    f"elegida es Latinoamérica (remix posterior con más canales)."
                )

            if parts_included:
                ambiguity_text_included = (
                    " ".join(parts_included)
                    + " Revisa manualmente y recupera la otra si era la versión que querías."
                )
            else:
                ambiguity_text_included = ""
            ambiguity_text_discarded_similar = (
                f"Calidad similar a la incluida en {lang_lit} (mismo codec, "
                f"bitrate similar). Si es la versión que querías, recupérala "
                f"manualmente."
            )
            # Aviso de descartadas "different" solo cuando: (a) la elegida
            # NO es la primera del disco (caso España DD 2.0 + Latam DD
            # 5.1 donde la heurística pudo equivocarse) Y (b) es Castellano
            # (mismo razonamiento que arriba — para VO el codec priority
            # manda y la "primera del disco" no es informativa).
            ambiguity_text_discarded_different = (
                f"Otra pista {lang_lit} con distinta calidad técnica. Si es "
                f"la versión que querías (p.ej. España dub original vs Latam "
                f"remix), recupérala manualmente."
            ) if (not picked_is_first and is_castellano) else ""
        else:
            ambiguity_text_included = ""
            ambiguity_text_discarded_similar = ""
            ambiguity_text_discarded_different = ""
            similar_quality = []
            different_quality = []

        included.append(IncludedAudioTrack(
            position=0,  # se reasigna después
            raw=best,
            language_literal=lang_lit,
            codec_literal=codec_lit,
            label=label,
            flag_default=is_castellano,
            flag_forced=False,
            selection_reason=reason,
            ambiguity_warning=ambiguity_text_included,
        ))

        # Descartar el resto del mismo idioma. Cada descartada lleva su
        # warning: las de calidad SIMILAR llevan texto explícito de "España
        # vs Latam"; las de calidad DISTINTA llevan texto que apunta al
        # caso "España dub original vs Latam remix" si la primera del
        # disco está entre ellas. Sin "similar+different", solo "diff".
        best_codec_lit = _codec_literal(best, False)
        similar_ids = {id(t) for t in similar_quality}
        different_ids = {id(t) for t in different_quality}
        for t in rest:
            t_codec_lit = _codec_literal(t, False)
            if id(t) in similar_ids:
                reason_disc = (
                    f"Descartada por defecto: hay otra pista {lang_lit} "
                    f"({best_codec_lit}) de calidad similar y nos quedamos "
                    f"con la primera del disco."
                )
                ambig_text = ambiguity_text_discarded_similar
            elif id(t) in different_ids:
                reason_disc = (
                    f"Descartada: segunda pista {_language_literal(t.language.lower())}. "
                    f"Menor calidad técnica: {t_codec_lit} < {best_codec_lit}"
                )
                ambig_text = ambiguity_text_discarded_different
            else:
                # No debería pasar (similar+different cubre rest), pero
                # por defensa: razón clásica sin warning.
                reason_disc = (
                    f"Descartada: segunda pista {_language_literal(t.language.lower())}. "
                    f"Menor calidad: {t_codec_lit} < {best_codec_lit}"
                )
                ambig_text = ""
            discarded.append(DiscardedTrack(
                track_type="audio",
                raw=t,
                discard_reason=reason_disc,
                ambiguity_warning=ambig_text,
            ))

    return included, discarded


def _is_audio_ambiguous_with(a: RawAudioTrack, b: RawAudioTrack) -> bool:
    """Dos pistas de audio son ambiguas si tienen el mismo codec_priority
    (mismo nivel de calidad: TrueHD Atmos == TrueHD Atmos, DTS-HD MA ==
    DTS-HD MA, etc.) y bitrate dentro del ±15% (mismo channel layout
    típicamente). Sin esto la heurística no puede decidir cuál es la
    "correcta" entre p.ej. doblaje España vs Latinoamérica del mismo
    codec, y elegirla a ciegas es peor que avisar al usuario.

    Devuelve True si hay ambigüedad real, False si la diferencia de
    calidad es clara (codec distinto o bitrate muy distinto)."""
    if _codec_priority(a) != _codec_priority(b):
        return False  # Codecs distintos → la heurística decide bien
    if a.bitrate_kbps <= 0 or b.bitrate_kbps <= 0:
        # Sin bitrate fiable, mismo codec_priority basta para ser ambiguo
        return True
    hi = max(a.bitrate_kbps, b.bitrate_kbps)
    lo = min(a.bitrate_kbps, b.bitrate_kbps)
    return (hi - lo) / hi <= 0.15


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
    """Identifica el codec normalizado de una pista (spec §5.1.2, §5.1.6).

    Detección de Atmos: busca en format_commercial, codec y description
    (cualquiera de los tres basta). Esto cubre casos donde MediaInfo reporta
    el format_commercial como "Dolby Digital Plus" sin mencionar Atmos pero
    la description del BD lo indica como "7.1-Atmos".
    """
    codec = track.codec.lower()
    desc = track.description.lower()
    fc = getattr(track, "format_commercial", "").lower()
    # Atmos se detecta en cualquiera de los tres campos
    has_atmos = ("atmos" in fc) or ("atmos" in codec) or ("atmos" in desc)

    # Detección principal: format_commercial (más fiable si existe)
    if fc:
        if "truehd" in fc:
            return "truehd_atmos" if has_atmos else "truehd"
        if "digital plus" in fc or "e-ac-3" in fc:
            return "ddplus_atmos" if has_atmos else "ddplus"
        if "dts-hd master" in fc or "dts-hd ma" in fc:
            return "dts_hd_ma"
        if "dts" in fc:
            return "dts"
        if "dolby digital" in fc and "plus" not in fc:
            return "dd"
    # Fallback: heurística por nombre de codec (sin MediaInfo)
    if "truehd" in codec:
        return "truehd_atmos" if has_atmos else "truehd"
    if "digital plus" in codec:
        return "ddplus_atmos" if has_atmos else "ddplus"
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
    return "TrueHD Atmos > DD+ Atmos > DTS-HD MA > DTS > TrueHD > DD+ > DD"


# ── Subtítulos (spec §5.2) ────────────────────────────────────────────────────

def _select_subtitle_tracks(
    tracks: list[RawSubtitleTrack],
    vo_language: str,
    mode: str = "filtered",
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

    # ── Modo "keep_all": incluir TODAS las pistas con labels generados ──
    if mode == "keep_all":
        included_all: list[IncludedSubtitleTrack] = []
        first_spanish_forced_seen = False
        for t in tracks:
            if t.language.lower() == "qad":
                continue  # Audio Description siempre descartada
            lang_norm = t.language.lower()
            lang_lit = _language_literal(lang_norm)
            is_castellano = lang_norm == "spanish"
            # Clasificar tipo por packets (si hay) o bitrate sintético
            if t.packet_count > 0:
                if t.packet_count < 500:
                    sub_type, type_lit = "forced", "Forzados"
                else:
                    sub_type, type_lit = "complete", "Completos"
            else:
                if t.bitrate_kbps < 3.0:
                    sub_type, type_lit = "forced", "Forzados"
                else:
                    sub_type, type_lit = "complete", "Completos"
            label = f"{lang_lit} {type_lit} (PGS)"
            flag_forced = sub_type == "forced"
            flag_default = False
            # Default=True solo para la primera pista Castellano forzada
            if is_castellano and flag_forced and not first_spanish_forced_seen:
                flag_default = True
                first_spanish_forced_seen = True
            included_all.append(IncludedSubtitleTrack(
                position=0,
                raw=t,
                language_literal=lang_lit,
                subtitle_type=sub_type,
                label=label,
                flag_default=flag_default,
                flag_forced=flag_forced,
                selection_reason=(
                    f"Modo «mantener todas»: conservada y etiquetada automáticamente "
                    f"({label}, {t.packet_count} paquetes)"
                    if t.packet_count > 0
                    else f"Modo «mantener todas»: conservada y etiquetada automáticamente ({label})"
                ),
            ))
        return included_all, []

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
        RawSubtitleTrack | None, RawSubtitleTrack | None, list[RawSubtitleTrack],
        list[RawSubtitleTrack], list[RawSubtitleTrack]
    ]:
        """
        Clasifica las pistas de un idioma en (forzados, completo,
        audio_descripción, alternativas_ambiguas, otros_sobrantes).

        **Modo A — packet-based (si packet_count disponible en alguna pista):**
          - Forzados:  packets < 500
          - Candidatos completo: el resto
          - Audiodescripción: packet_count > 1.3× del MÍNIMO del grupo no-forzado
          - Completo final: primer candidato no-AD por posición original
          - Forzado final:  primer forzado por posición original
          - **Alternativas ambiguas**: si quedan ≥2 candidatos a completo
            tras filtrar AD, las que NO se eligieron son ambiguas
            (mismo idioma, tamaño similar — comentarios del director,
            España vs Latinoamérica, etc.). La heurística no puede
            decidir cuál es la "correcta" automáticamente.

        **Modo B — bitrate sintético (fallback, antes de ffprobe -count_packets):**
          Forma A: si hay ≥2 pistas y una tiene bitrate < 3 kbps → es la
          de forzados. Sin ambigüedad detectable en este modo (no hay
          señal de tamaño relativo).

        Devuelve (forced, complete, audio_descriptions, ambiguous_alts,
        leftover). Las AD, ambiguas y sobrantes se descartan con razones
        distintas.
        """
        has_packets = any(t.packet_count > 0 for t in lang_tracks)

        if has_packets:
            forced_cands = [t for t in lang_tracks if t.packet_count < 500]
            other_cands  = [t for t in lang_tracks if t.packet_count >= 500]
            # Detectar audiodescripción: pistas con packet_count >1.3× del MÍNIMO
            # del grupo no-forzado. Usamos mínimo (no mediana) porque con solo 2
            # pistas la mediana=promedio y los outliers no se detectan.
            audio_descriptions: list[RawSubtitleTrack] = []
            complete_cands = other_cands
            if len(other_cands) >= 2:
                base = min(t.packet_count for t in other_cands)
                threshold_ad = base * 1.3
                complete_cands = [t for t in other_cands if t.packet_count <= threshold_ad]
                audio_descriptions = [t for t in other_cands if t.packet_count > threshold_ad]

            # Escoger primero por posición original (ya vienen en orden del m2ts)
            forced   = forced_cands[0] if forced_cands else None
            complete = complete_cands[0] if complete_cands else None
            # Alternativas ambiguas: el resto de complete_cands (mismo
            # idioma + tamaño dentro del ±30% por el filtro AD previo).
            ambiguous_alts = complete_cands[1:] if len(complete_cands) >= 2 else []
            used = {id(t) for t in (forced, complete) if t}
            ambig_ids = {id(t) for t in ambiguous_alts}
            # Sobrantes: forzados extra (los que no se incluyeron) que
            # no son AD ni ambiguos.
            leftover = [
                t for t in lang_tracks
                if id(t) not in used
                and id(t) not in ambig_ids
                and t not in audio_descriptions
            ]
            return forced, complete, audio_descriptions, ambiguous_alts, leftover

        # ── Fallback: bitrate sintético (cuando ffprobe no pudo medir) ──
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
                return forced, complete, [], [], leftover
        if lang_tracks:
            return None, lang_tracks[0], [], [], lang_tracks[1:]
        return None, None, [], [], []

    # Clasificar pistas por idioma. El tercer slot del valor es la lista
    # de alternativas ambiguas (pistas completas del mismo idioma con
    # tamaño similar al elegido) — el bucle de emisión las usa para
    # poner ambiguity_warning en la incluida y en las descartadas.
    classified: dict[str, tuple[
        RawSubtitleTrack | None, RawSubtitleTrack | None, list[RawSubtitleTrack]
    ]] = {}
    target_langs = {"spanish"}
    if vo_norm != "spanish":
        target_langs.add(vo_norm)
    if "english" not in target_langs:
        target_langs.add("english")

    for lang_norm in target_langs:
        if lang_norm in by_lang:
            forced, complete, audio_descriptions, ambiguous_alts, leftover = _classify_lang(by_lang[lang_norm])
            classified[lang_norm] = (forced, complete, ambiguous_alts)
            lang_lit = _language_literal(lang_norm)
            # Descartar audiodescripción con razón explícita
            for t in audio_descriptions:
                discarded.append(DiscardedTrack(
                    track_type="subtitle",
                    raw=t,
                    discard_reason=(
                        f"Descartada: detectada como audiodescripción "
                        f"(packet_count={t.packet_count} > 1.3× del mínimo del idioma)"
                    ),
                ))
            # Descartar alternativas ambiguas con razón + warning paralelo
            # al de la incluida. El frontend pintará banner ámbar en la
            # incluida + en éstas para que el usuario revise manualmente.
            for t in ambiguous_alts:
                discarded.append(DiscardedTrack(
                    track_type="subtitle",
                    raw=t,
                    discard_reason=(
                        f"Descartada por defecto: hay otra pista de subtítulos "
                        f"{lang_lit} de tamaño similar y nos quedamos con la "
                        f"primera del disco."
                    ),
                    ambiguity_warning=(
                        f"Tamaño similar a la pista de subtítulos {lang_lit} "
                        f"incluida (puede ser comentarios del director, España "
                        f"vs Latinoamérica…). Si es la versión que querías, "
                        f"recupérala manualmente."
                    ),
                ))
            # Descartar pistas sobrantes del mismo idioma
            for t in leftover:
                discarded.append(DiscardedTrack(
                    track_type="subtitle",
                    raw=t,
                    discard_reason=f"Descartada: pista adicional {lang_lit} (ya incluida la mejor de cada tipo)",
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
        forced_track, complete_track, ambiguous_alts = classified[lang_norm]
        lang_lit = _language_literal(lang_norm)
        is_castellano = lang_norm == "spanish"

        if track_type == "forced" and forced_track:
            flag_default = is_castellano
            if forced_track.packet_count > 0:
                reason_forced = (
                    f"Forzados (packet-based): {forced_track.packet_count} paquetes < 500. "
                    f"Primera pista forzada para {lang_norm.capitalize()}"
                )
            else:
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
            if complete_track.packet_count > 0:
                reason_complete = (
                    f"Completos (packet-based): {complete_track.packet_count} paquetes, "
                    f"primera pista completa para {lang_norm.capitalize()}"
                )
            else:
                reason_complete = f"Completos: {'única pista' if not forced_track else 'pista completa'} para {lang_norm.capitalize()}"
            # Si hay alternativas ambiguas (otras pistas completas del
            # mismo idioma con tamaño similar), avisar al usuario en la
            # incluida — la heurística no puede decidir cuál es la
            # correcta (comentarios del director, España vs Latam…).
            ambiguity_text = ""
            if ambiguous_alts:
                alt_counts = ", ".join(
                    f"{t.packet_count} paq." for t in ambiguous_alts
                    if t.packet_count > 0
                )
                detail = f" ({alt_counts})" if alt_counts else ""
                ambiguity_text = (
                    f"Hay {len(ambiguous_alts)} pista{'s' if len(ambiguous_alts) > 1 else ''} "
                    f"de subtítulos {lang_lit} más con tamaño similar{detail}. "
                    f"Casos típicos: comentarios del director, España vs "
                    f"Latinoamérica. Revisa manualmente si la elegida es "
                    f"la correcta."
                )
            included.append(IncludedSubtitleTrack(
                position=0,
                raw=complete_track,
                language_literal=lang_lit,
                subtitle_type="complete",
                label=f"{lang_lit} Completos (PGS)",
                flag_default=False,
                flag_forced=False,
                selection_reason=reason_complete,
                ambiguity_warning=ambiguity_text,
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
        forced_track, complete_track, _ambiguous_alts = classified[lang_norm]
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


# ══════════════════════════════════════════════════════════════════════
#  NOMENCLATURA DE SERIES (v2.5+)
#
#  Plex/Jellyfin compatible. Estructura con subdirectorios:
#
#    Serie Name (Año)/
#      Season NN/
#        Serie Name (Año) - SNNeNN - Episode Title [DV FEL][Audio DCP].mkv
#
#  Sin episode title cuando TMDb no lo trae:
#    Serie Name (Año) - SNNeNN [DV FEL][Audio DCP].mkv
#
#  La función devuelve la RUTA RELATIVA (con subdirectorios). El
#  caller la combina con OUTPUT_DIR. phase_e.py hace `parent.mkdir`
#  antes de mkvmerge para que la jerarquía se cree on-the-fly.
# ══════════════════════════════════════════════════════════════════════


def _sanitize_for_path(s: str) -> str:
    """Sustituye caracteres problemáticos en nombres de fichero/directorio.
    Compatible con ext4, ZFS, NTFS y SMB compartido del NAS.
    Conserva acentos y caracteres no-ASCII (UTF-8 nativo)."""
    # Caracteres prohibidos: / \ : * ? " < > | (Windows reserva éstos)
    bad = '\\/:*?"<>|'
    for c in bad:
        s = s.replace(c, "-")
    # Colapsa espacios múltiples + strip puntos/espacios al final (NTFS no
    # los acepta como último char de un componente de path).
    while "  " in s:
        s = s.replace("  ", " ")
    return s.strip(" .")


def build_series_mkv_name(
    series_name: str,
    series_year: int | None,
    season_number: int,
    episode_number: int,
    episode_title: str = "",
    has_fel: bool = False,
    audio_dcp: bool = False,
) -> str:
    """
    Construye la ruta relativa del MKV de un episodio (estructura Plex/
    Jellyfin con subdirectorios).

    Args:
        series_name:    Nombre canónico de la serie (TMDb).
        series_year:    Año del first_air_date (opcional, si conocido).
        season_number:  1-based.
        episode_number: 1-based dentro de la temporada.
        episode_title:  Título del episodio según TMDb (opcional).
        has_fel:        DV FEL detectado.
        audio_dcp:      Audio DCP detectado en el nombre del ISO.

    Returns:
        Ruta relativa con subdirectorios. Ej:
          "Mad Men (2007)/Season 01/Mad Men (2007) - S01E01 - Smoke Gets in Your Eyes.mkv"
        Sin year:
          "Twin Peaks/Season 01/Twin Peaks - S01E01 - Pilot.mkv"
        Sin episode title:
          "Mad Men (2007)/Season 01/Mad Men (2007) - S01E01.mkv"
        Con tags:
          "...S01E01 - Pilot [DV FEL][Audio DCP].mkv"
    """
    safe_series = _sanitize_for_path(series_name)
    safe_episode_title = _sanitize_for_path(episode_title) if episode_title else ""

    series_with_year = (
        f"{safe_series} ({series_year})" if series_year else safe_series
    )

    season_folder = f"Season {season_number:02d}"

    # Filename: SerieName (Año) - SNNeNN[ - Title][tags].mkv
    sne = f"S{season_number:02d}E{episode_number:02d}"
    filename = f"{series_with_year} - {sne}"
    if safe_episode_title:
        filename += f" - {safe_episode_title}"
    if has_fel:
        filename += " [DV FEL]"
    if audio_dcp:
        filename += " [Audio DCP]"
    filename += ".mkv"

    return f"{series_with_year}/{season_folder}/{filename}"
