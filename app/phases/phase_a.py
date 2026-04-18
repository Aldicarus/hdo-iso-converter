"""
phase_a.py — Fase A: Análisis del disco (pipeline extendido v1.6)

Pipeline de 4 herramientas ejecutadas mientras el ISO está montado:

  1. ``mkvmerge -J`` (requerido) — identificación de pistas, codecs, idiomas
  2. ``mediainfo --Output=JSON`` (opcional) — bitrate real, HDR10, codecs comerciales
  3. ``dovi_tool`` (opcional, solo si hay EL) — Profile DV, FEL/MEL, CM version
  4. ``mkvextract chapters`` — capítulos del MPLS

Si MediaInfo o dovi_tool fallan, el análisis continúa con datos de mkvmerge.

─────────────────────────────────────────────────────────────────────
HERRAMIENTAS
─────────────────────────────────────────────────────────────────────

mkvmerge -J <mpls_path>
  Devuelve JSON con pistas, info de playlist y capítulos.
  Selecciona el MPLS principal (el de más pistas audio entre los 10 más grandes).

mediainfo --Output=JSON <m2ts_path>
  Ejecutado sobre el m2ts más grande (feature principal).
  Proporciona: bitrate real, Format_Commercial_IfAny (Atmos/DTS:X definitivo),
  HDR10 metadata, channel layout, compression mode.

ffmpeg + dovi_tool (3 pasos):
  1. ffmpeg extrae 30s del Enhancement Layer como raw HEVC
  2. dovi_tool extract-rpu → RPU.bin
  3. dovi_tool info --summary → Profile, FEL/MEL, CM version, L1-L6

─────────────────────────────────────────────────────────────────────
ENRIQUECIMIENTO
─────────────────────────────────────────────────────────────────────

enrich_tracks_with_mediainfo():
  Inyecta bitrate, format_commercial, channel_layout, HDR metadata
  en los modelos de BDInfoResult. Mapeo por orden dentro de cada tipo.
  Filtra cores AC-3 embebidos por PID compartido.

enrich_dovi():
  Actualiza has_fel y fel_reason con dato definitivo de dovi_tool.
  Adjunta DoviInfo a la pista BL.

run_full_analysis() — orquestador:
  Ejecuta todo secuencialmente, captura errores de herramientas opcionales.
"""
import asyncio
import json
import logging
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import os

from models import (
    BDInfoResult, DoviInfo, HdrMetadata, MediaInfoResult, MediaInfoTrack,
    RawAudioTrack, RawSubtitleTrack, VideoTrack,
)

_logger = logging.getLogger(__name__)

MKVMERGE_BIN = "mkvmerge"
MEDIAINFO_BIN = "mediainfo"
FFMPEG_BIN = "ffmpeg"
DOVI_TOOL_BIN = "dovi_tool"
IDENTIFY_TIMEOUT = 30      # segundos — mkvmerge -J es rápido
MEDIAINFO_TIMEOUT = 60     # MediaInfo sobre m2ts grande
FFMPEG_EL_TIMEOUT = 60     # Extracción 30s del EL
DOVI_TOOL_TIMEOUT = 30     # RPU extract + analysis
TMP_DIR = os.environ.get("TMP_DIR", "/mnt/tmp")


# ══════════════════════════════════════════════════════════════════════
#  MAPEOS DE TRADUCCIÓN
# ══════════════════════════════════════════════════════════════════════

# ISO 639-2 → nombre en inglés (como los generaba BDInfo)
ISO639_TO_ENGLISH: dict[str, str] = {
    "eng": "English",  "spa": "Spanish",  "fre": "French",   "fra": "French",
    "ger": "German",   "deu": "German",   "ita": "Italian",  "jpn": "Japanese",
    "por": "Portuguese","chi": "Chinese",  "zho": "Chinese",  "kor": "Korean",
    "dut": "Dutch",    "nld": "Dutch",    "rus": "Russian",  "pol": "Polish",
    "cze": "Czech",    "ces": "Czech",    "hun": "Hungarian","swe": "Swedish",
    "nor": "Norwegian","dan": "Danish",   "fin": "Finnish",  "tur": "Turkish",
    "tha": "Thai",     "ara": "Arabic",   "heb": "Hebrew",   "hin": "Hindi",
    "vie": "Vietnamese","rum": "Romanian", "ron": "Romanian",
    "gre": "Greek",    "ell": "Greek",    "bul": "Bulgarian","cat": "Catalan",
    "hrv": "Croatian", "slk": "Slovak",   "slv": "Slovenian","ukr": "Ukrainian",
    "ind": "Indonesian","may": "Malay",   "msa": "Malay",
    "qad": "qad",      # Audio Description — passthrough para que phase_b lo descarte
    "und": "Undetermined",
}

# mkvmerge codec → codec estilo BDInfo (para que _codec_key() en phase_b funcione)
MKVMERGE_CODEC_TO_BDINFO: dict[str, str] = {
    "TrueHD Atmos":           "Dolby TrueHD/Atmos Audio",
    "TrueHD":                 "Dolby TrueHD Audio",
    "E-AC-3":                 "Dolby Digital Plus Audio",
    "AC-3":                   "Dolby Digital Audio",
    "DTS-HD Master Audio":    "DTS-HD Master Audio",
    "DTS-HD High Resolution Audio": "DTS-HD High Resolution Audio",
    "DTS":                    "DTS Audio",
    "DTS-ES":                 "DTS Audio",
    "PCM":                    "LPCM Audio",
    "FLAC":                   "FLAC Audio",
    "AAC":                    "AAC Audio",
    "Opus":                   "Opus Audio",
    "Vorbis":                 "Vorbis Audio",
    "MP3":                    "MP3 Audio",
    "MP2":                    "MP2 Audio",
}

# Mapeo de audio_channels → string de canales estilo BDInfo
CHANNELS_MAP: dict[int, str] = {
    8: "7.1",
    7: "6.1",
    6: "5.1",
    4: "4.0",
    3: "2.1",
    2: "2.0",
    1: "1.0",
}


# ══════════════════════════════════════════════════════════════════════
#  EJECUCIÓN: mkvmerge -J
# ══════════════════════════════════════════════════════════════════════

async def run_mkvmerge_identify(share_path: str, log_callback=None) -> tuple[dict, str]:
    """
    Encuentra el MPLS principal y ejecuta ``mkvmerge -J`` sobre él.

    Selección inteligente del MPLS: ejecuta ``mkvmerge -J`` sobre los
    candidatos y elige el que tiene más pistas de audio (el título
    principal siempre tiene más pistas que menús o playlists de
    navegación). Se descartan MPLS sin pistas de audio.

    Args:
        share_path:   Ruta al directorio raíz del disco montado
                      (ej: '/mnt/bd/Movie_2025_1234'). Debe contener BDMV/.
        log_callback: Corutina opcional ``async def(str)`` para streaming.

    Returns:
        Tupla (data, mpls_path): JSON de mkvmerge -J y ruta absoluta al MPLS.

    Raises:
        RuntimeError: Si no se encuentra MPLS o mkvmerge falla.
    """
    # ── 1. Localizar todos los MPLS ──────────────────────────────
    playlist_dir = None
    for candidate in [
        Path(share_path) / "BDMV" / "PLAYLIST",
        Path(share_path) / "PLAYLIST",
    ]:
        if candidate.exists():
            playlist_dir = candidate
            break

    if playlist_dir is None:
        raise RuntimeError(
            f"No se encontró BDMV/PLAYLIST/ bajo {share_path}."
        )

    mpls_files = sorted(playlist_dir.glob("*.mpls"), key=lambda p: p.name)
    if not mpls_files:
        raise RuntimeError(
            f"No hay ficheros .mpls en {playlist_dir}."
        )

    if log_callback:
        await log_callback(f"[Fase A] {len(mpls_files)} ficheros MPLS encontrados")

    # ── 2. Identificar cada MPLS con mkvmerge -J ─────────────────
    # Limitamos a los 10 más grandes por tamaño de fichero para no
    # ralentizar el análisis en discos con muchos playlists.
    candidates_by_size = sorted(mpls_files, key=lambda p: p.stat().st_size, reverse=True)[:10]

    best_data = None
    best_audio_count = -1
    best_mpls = None

    for mpls_path in candidates_by_size:
        data = await _run_mkvmerge_j(str(mpls_path))
        if data is None:
            continue
        audio_count = sum(1 for t in data.get("tracks", []) if t.get("type") == "audio")
        if audio_count > best_audio_count:
            best_audio_count = audio_count
            best_data = data
            best_mpls = mpls_path

    if best_data is None or best_audio_count == 0:
        raise RuntimeError(
            f"Ningún MPLS válido con pistas de audio en {playlist_dir}."
        )

    if log_callback:
        n_tracks = len(best_data.get("tracks", []))
        await log_callback(
            f"[Fase A] MPLS principal: {best_mpls.name} "
            f"({best_audio_count} pistas audio, {n_tracks} pistas total)"
        )

    return best_data, str(best_mpls)


async def _run_mkvmerge_j(mpls_path: str) -> dict | None:
    """Ejecuta ``mkvmerge -J`` sobre un MPLS y devuelve el JSON, o None si falla."""
    proc = await asyncio.create_subprocess_exec(
        MKVMERGE_BIN, "-J", mpls_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=IDENTIFY_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        _logger.warning("mkvmerge -J timeout para %s", mpls_path)
        return None

    if proc.returncode >= 2:
        return None

    try:
        return json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


# ══════════════════════════════════════════════════════════════════════
#  CONVERSIÓN: mkvmerge JSON → BDInfoResult
# ══════════════════════════════════════════════════════════════════════

def parse_mkvmerge_json(data: dict) -> BDInfoResult:
    """
    Convierte el JSON de ``mkvmerge -J`` al modelo BDInfoResult.

    Aplica el patrón adapter: traduce codecs, idiomas y heurísticas
    para producir exactamente los mismos modelos que consumía phase_b
    cuando la fuente era BDInfoCLI.

    Args:
        data: Dict completo del output de ``mkvmerge -J``.

    Returns:
        BDInfoResult con todas las pistas y metadatos del disco.
    """
    tracks = data.get("tracks", [])
    container = data.get("container", {}).get("properties", {})

    # ── Filtrar AC-3 cores multiplexados con TrueHD ──────────────
    subordinate_ids = _find_subordinate_track_ids(tracks)

    # ── Video ────────────────────────────────────────────────────
    video_tracks = _build_video_tracks(tracks)

    # ── Audio (excluyendo cores subordinados) ────────────────────
    audio_tracks = _build_audio_tracks(tracks, subordinate_ids)

    # ── Subtítulos (con heurística de forzados) ──────────────────
    subtitle_tracks = _build_subtitle_tracks(tracks)

    # ── Duración ─────────────────────────────────────────────────
    duration = _extract_duration(container)

    # ── FEL ──────────────────────────────────────────────────────
    has_fel, fel_bitrate, fel_reason = _detect_fel(video_tracks, tracks)

    # ── VO ───────────────────────────────────────────────────────
    vo_language = audio_tracks[0].language if audio_tracks else "English"

    # ── MPLS seleccionado (para reutilizar en Fase D) ────────────
    main_mpls = Path(data.get("file_name", "")).name

    return BDInfoResult(
        video_tracks=video_tracks,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
        duration_seconds=duration,
        has_fel=has_fel,
        fel_bitrate_kbps=fel_bitrate,
        fel_reason=fel_reason,
        vo_language=vo_language,
        main_mpls=main_mpls,
        mkvmerge_raw=data,
    )


# ══════════════════════════════════════════════════════════════════════
#  BUILDERS DE PISTAS
# ══════════════════════════════════════════════════════════════════════

def _find_subordinate_track_ids(tracks: list[dict]) -> set[int]:
    """
    Identifica los IDs de pistas AC-3 core que son subordinadas de TrueHD.

    Cuando mkvmerge reporta TrueHD + AC-3 core multiplexados, ambos
    comparten ``multiplexed_tracks``. El AC-3 core es redundante y
    debe excluirse para no duplicar pistas en el análisis.
    """
    subordinate = set()
    for t in tracks:
        if t.get("type") != "audio":
            continue
        codec = t.get("codec", "")
        props = t.get("properties", {})
        mux_ids = props.get("multiplexed_tracks", [])
        # Si es TrueHD (con o sin Atmos) y tiene pistas multiplexadas,
        # las otras son cores subordinados
        if "TrueHD" in codec and mux_ids:
            for mid in mux_ids:
                if mid != t.get("id"):
                    subordinate.add(mid)
    return subordinate


def _build_video_tracks(tracks: list[dict]) -> list[VideoTrack]:
    """Construye VideoTrack[] desde las pistas de vídeo del JSON."""
    video_tracks: list[VideoTrack] = []
    hevc_count = 0

    for t in tracks:
        if t.get("type") != "video":
            continue
        codec = t.get("codec", "")
        props = t.get("properties", {})
        pixel_dim = props.get("pixel_dimensions", "")

        # Normalizar resolución para el campo description
        if "3840" in pixel_dim:
            resolution = "2160p"
        elif "1920" in pixel_dim:
            resolution = "1080p"
        elif "1280" in pixel_dim:
            resolution = "720p"
        else:
            resolution = pixel_dim

        # Detectar EL: segundo track HEVC a 1080p
        is_hevc = "HEVC" in codec or "H.265" in codec
        is_el = False
        if is_hevc:
            hevc_count += 1
            if hevc_count > 1 and resolution == "1080p":
                is_el = True

        # Codec estilo BDInfo
        bdinfo_codec = "MPEG-H HEVC Video" if is_hevc else codec

        video_tracks.append(VideoTrack(
            codec=bdinfo_codec,
            bitrate_kbps=0,  # mkvmerge -J no proporciona bitrate
            description=resolution,
            is_el=is_el,
        ))

    return video_tracks


def _build_audio_tracks(
    tracks: list[dict], subordinate_ids: set[int]
) -> list[RawAudioTrack]:
    """
    Construye RawAudioTrack[] desde las pistas de audio del JSON.

    Excluye cores AC-3 subordinados (multiplexados con TrueHD).
    Traduce codecs e idiomas al formato que espera phase_b.
    """
    audio_tracks: list[RawAudioTrack] = []

    for t in tracks:
        if t.get("type") != "audio":
            continue
        if t.get("id") in subordinate_ids:
            continue

        codec_raw = t.get("codec", "")
        props = t.get("properties", {})
        lang_iso = props.get("language", "und")
        channels = props.get("audio_channels", 0)
        sample_rate = props.get("audio_sampling_frequency", 48000)

        # Traducir codec al estilo BDInfo
        bdinfo_codec = MKVMERGE_CODEC_TO_BDINFO.get(codec_raw, codec_raw)

        # Traducir idioma ISO 639-2 → nombre en inglés
        lang_english = ISO639_TO_ENGLISH.get(lang_iso, lang_iso.capitalize())

        # Construir description sintético (phase_b usa esto para extraer canales
        # y detectar Atmos en DD+)
        channels_str = CHANNELS_MAP.get(channels, f"{channels}ch")
        desc_parts = [channels_str]

        # DD+ Atmos: mkvmerge no lo distingue en el codec, pero 8 canales
        # en E-AC-3 es virtualmente siempre Atmos (JOC)
        if codec_raw == "E-AC-3" and channels >= 8:
            desc_parts[0] = f"{channels_str}-Atmos"

        desc_parts.append(f"{sample_rate // 1000} kHz")
        description = " / ".join(desc_parts)

        audio_tracks.append(RawAudioTrack(
            codec=bdinfo_codec,
            language=lang_english,
            bitrate_kbps=0,
            description=description,
        ))

    return audio_tracks


def _build_subtitle_tracks(tracks: list[dict]) -> list[RawSubtitleTrack]:
    """
    Construye RawSubtitleTrack[] con detección de forzados por dos patrones.

    Los Blu-ray usan dos patrones para organizar subtítulos forzados:

    **Patrón 1 — Bloques separados** (discos multi-idioma):
      Bloque 1 (completos): eng, fre, spa, jpn (todos los idiomas, uno por pista)
      Bloque 2 (forzados): fre, spa, jpn (subconjunto de idiomas)
      → Se detecta cuando un idioma repite con otros idiomas diferentes entre medio.
      → Validación: idiomas del bloque 2 ⊂ idiomas del bloque 1.

    **Patrón 2 — Duplicados adyacentes** (discos con pocos idiomas):
      spa, spa, eng (dos pistas del mismo idioma seguidas)
      → La primera es forzada, la segunda completa.
      → Se detecta cuando un idioma repite inmediatamente (posición i e i+1).

    Si ningún patrón aplica, todas las pistas se marcan como completas.

    Bitrate sintético: 1.0 kbps (forzados) / 30.0 kbps (completos) para
    preservar la lógica Forma A de phase_b (umbral < 3 kbps).
    """
    raw_subs: list[tuple[str, str]] = []
    for t in tracks:
        if t.get("type") != "subtitles":
            continue
        props = t.get("properties", {})
        lang_iso = props.get("language", "und")
        raw_subs.append((lang_iso, t.get("codec", "")))

    if not raw_subs:
        return []

    # Contar ocurrencias por idioma
    lang_counts = Counter(lang for lang, _ in raw_subs)

    # ── Detectar patrón ──────────────────────────────────────────
    # Buscar la primera repetición de idioma para decidir el patrón.
    first_repeat_idx = None
    seen_langs: set[str] = set()
    for i, (lang, _) in enumerate(raw_subs):
        if lang in seen_langs:
            first_repeat_idx = i
            break
        seen_langs.add(lang)

    pattern = "none"
    split_idx = len(raw_subs)
    adjacent_forced: set[int] = set()  # índices de pistas forzadas (patrón 2)

    if first_repeat_idx is not None:
        # ¿Es adyacente? (misma posición que la anterior)
        prev_lang = raw_subs[first_repeat_idx - 1][0]
        curr_lang = raw_subs[first_repeat_idx][0]
        is_adjacent = (prev_lang == curr_lang)

        if is_adjacent:
            # ── Patrón 2: duplicados adyacentes ──────────────────
            # Recorrer todas las pistas: cuando un idioma repite
            # inmediatamente, la primera ocurrencia es forzada.
            pattern = "adjacent"
            lang_first_seen: dict[str, int] = {}
            for i, (lang, _) in enumerate(raw_subs):
                if lang in lang_first_seen:
                    # Este idioma ya apareció → la primera fue forzada
                    adjacent_forced.add(lang_first_seen[lang])
                else:
                    lang_first_seen[lang] = i

            _logger.info(
                "Subtítulos patrón adyacente: forzados en posiciones %s",
                sorted(adjacent_forced),
            )
        else:
            # ── Patrón 1: bloques separados ──────────────────────
            split_idx = first_repeat_idx
            block1_langs = {lang for lang, _ in raw_subs[:split_idx]}
            block2_langs = {lang for lang, _ in raw_subs[split_idx:]}

            if block2_langs.issubset(block1_langs):
                pattern = "blocks"
                _logger.info(
                    "Subtítulos patrón bloques: corte en posición %d, "
                    "bloque 1 = %s, bloque 2 = %s",
                    split_idx, block1_langs, block2_langs,
                )
            else:
                _logger.warning(
                    "Subtítulos: bloque 2 tiene idiomas no presentes en bloque 1: %s. "
                    "No se detectan forzados.",
                    block2_langs - block1_langs,
                )
                split_idx = len(raw_subs)

    # ── Construir pistas con bitrate sintético ───────────────────
    block1_langs = {lang for lang, _ in raw_subs[:split_idx]} if pattern == "blocks" else set()
    subtitle_tracks: list[RawSubtitleTrack] = []

    for i, (lang_iso, codec) in enumerate(raw_subs):
        lang_english = ISO639_TO_ENGLISH.get(lang_iso, lang_iso.capitalize())

        if pattern == "blocks":
            if i < split_idx:
                bitrate = 30.0  # bloque 1 → completo
            elif lang_iso in block1_langs:
                bitrate = 1.0   # bloque 2, idioma en bloque 1 → forzado
            else:
                bitrate = 30.0  # bloque 2, idioma nuevo → completo
        elif pattern == "adjacent":
            bitrate = 1.0 if i in adjacent_forced else 30.0
        else:
            bitrate = 30.0  # sin patrón → todo completo

        subtitle_tracks.append(RawSubtitleTrack(
            language=lang_english,
            bitrate_kbps=bitrate,
            description="",
        ))

    return subtitle_tracks


# ══════════════════════════════════════════════════════════════════════
#  DURACIÓN
# ══════════════════════════════════════════════════════════════════════

def _extract_duration(container_props: dict) -> float:
    """
    Extrae la duración del playlist en segundos.

    ``playlist_duration`` de mkvmerge está en nanosegundos.
    Se aplica un sanity check y se loguea si el valor es inesperado.
    """
    raw = container_props.get("playlist_duration", 0)
    if raw <= 0:
        return 0.0

    seconds = raw / 1_000_000_000

    if seconds < 60:
        _logger.warning(
            "Duración del playlist sospechosamente corta: %.1fs (raw=%d). "
            "Las unidades de playlist_duration podrían no ser nanosegundos.",
            seconds, raw,
        )
    elif seconds > 36000:
        _logger.warning(
            "Duración del playlist sospechosamente larga: %.1fs (raw=%d).",
            seconds, raw,
        )

    return seconds


# ══════════════════════════════════════════════════════════════════════
#  DETECCIÓN FEL (spec §4.2)
# ══════════════════════════════════════════════════════════════════════

def _detect_fel(
    video_tracks: list[VideoTrack],
    raw_tracks: list[dict],
) -> tuple[bool, int | None, str]:
    """
    Detecta Dolby Vision (FEL/MEL) — compatible con mkvmerge v65 y v81+.

    Estrategia múltiple:

    1. **v65 (legacy)**: la EL aparece como segundo track HEVC a 1080p
       (is_el=True en video_tracks). Detección directa.

    2. **v81+ (DV combinado)**: mkvmerge combina BL+EL en un solo track.
       La EL ya no aparece en la lista de pistas. Se detecta por un gap
       en los IDs de tracks del JSON raw: si el primer track de audio tiene
       id ≥ 2 pero solo hay 1 track de vídeo (id=0), entonces id=1 fue
       absorbido como EL → hay Dolby Vision dual-layer.

    En ambos casos, asumimos FEL (los discos MEL prácticamente no existen
    en el catálogo actual de UHD Blu-ray).

    Returns:
        Tupla (has_fel, el_bitrate_kbps, reason_string).
    """
    # ── Método 1: EL explícita como segundo track (mkvmerge v65) ──
    el_track = next(
        (
            t for t in video_tracks
            if t.is_el
            and "hevc" in t.codec.lower()
            and "1080p" in t.description.lower()
        ),
        None,
    )
    if el_track is not None:
        return True, None, (
            "FEL detectado: Enhancement Layer HEVC 1080p presente como track separado."
        )

    # ── Método 2: Gap en IDs de tracks (mkvmerge v81+ combina BL+EL) ──
    # Si hay 1 solo track de vídeo (id=0) pero el siguiente track tiene
    # id ≥ 2, significa que id=1 (la EL) fue absorbida en el BL.
    video_ids = [t.get("id") for t in raw_tracks if t.get("type") == "video"]
    non_video_ids = [t.get("id") for t in raw_tracks if t.get("type") != "video"]

    if len(video_ids) == 1 and video_ids[0] == 0 and non_video_ids:
        first_non_video = min(non_video_ids)
        if first_non_video >= 2:
            return True, None, (
                f"FEL detectado: gap en IDs de tracks (video id=0, siguiente id={first_non_video}). "
                f"mkvmerge v81+ combinó BL+EL en un solo track con señalización Dolby Vision."
            )

    return False, None, "Sin capa de mejora Dolby Vision detectada"


# ══════════════════════════════════════════════════════════════════════
#  EXTRACCIÓN DE CAPÍTULOS VIA MKVMERGE + MKVEXTRACT
# ══════════════════════════════════════════════════════════════════════

MKVEXTRACT_BIN = "mkvextract"


def parse_mpls_chapters(mpls_path: str) -> list[dict]:
    """
    Extrae los capítulos del MPLS creando un MKV mínimo (sin streams) y
    leyendo los capítulos con mkvextract.

    mkvmerge calcula los timestamps con precisión completa y extrae
    los nombres reales del disco si existen. Es instantáneo (~1s)
    porque no copia datos de vídeo/audio.

    Los nombres genéricos ("Chapter 01") se traducen a español
    ("Capítulo 01"). Los nombres custom del disco se preservan.

    Args:
        mpls_path: Ruta absoluta al fichero .mpls (el ISO debe estar montado)

    Returns:
        Lista de dicts compatibles con Chapter:
        [{"number": 1, "timestamp": "00:00:00.000", "name": "Capítulo 01", "name_custom": false}, ...]
        Lista vacía si falla o no hay capítulos.
    """
    try:
        with tempfile.NamedTemporaryFile(suffix=".mkv", delete=False) as tmp:
            tmp_mkv = tmp.name

        # Crear MKV mínimo (solo capítulos, sin streams)
        result = subprocess.run(
            [MKVMERGE_BIN, "--no-audio", "--no-video", "--no-subtitles",
             "-o", tmp_mkv, mpls_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode >= 2:
            _logger.warning("mkvmerge falló al crear MKV de capítulos: %s", result.stderr[:200])
            return []

        # Extraer capítulos en formato simple
        result = subprocess.run(
            [MKVEXTRACT_BIN, tmp_mkv, "chapters", "--simple"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            _logger.warning("mkvextract falló: %s", result.stderr[:200])
            return []

        chapters = _parse_simple_chapters(result.stdout)

        if chapters:
            _logger.info(
                "MPLS %s: %d capítulos extraídos via mkvmerge+mkvextract",
                Path(mpls_path).name, len(chapters),
            )

        return chapters

    except Exception as e:
        _logger.warning("Error extrayendo capítulos de %s: %s", mpls_path, e)
        return []
    finally:
        Path(tmp_mkv).unlink(missing_ok=True)


def _parse_simple_chapters(output: str) -> list[dict]:
    """
    Parsea el formato simple de mkvextract chapters.

    Formato:
        CHAPTER01=00:00:00.000
        CHAPTER01NAME=Chapter 01
        CHAPTER02=00:06:29.847
        CHAPTER02NAME=Chapter 02

    Los nombres genéricos ("Chapter XX") se traducen a "Capítulo XX".
    Los nombres custom del disco se preservan tal cual con name_custom=True.
    """
    chapters = []
    timestamps = {}
    names = {}

    for line in output.strip().splitlines():
        line = line.strip()
        # CHAPTERXX=HH:MM:SS.mmm
        m = re.match(r"CHAPTER(\d+)=([\d:.]+)", line)
        if m:
            num = int(m.group(1))
            timestamps[num] = m.group(2)
            continue
        # CHAPTERXXNAME=...
        m = re.match(r"CHAPTER(\d+)NAME=(.*)", line)
        if m:
            num = int(m.group(1))
            names[num] = m.group(2).strip()

    for num in sorted(timestamps.keys()):
        raw_name = names.get(num, "")
        # Detectar nombre genérico de mkvmerge ("Chapter XX" en cualquier idioma)
        is_generic = bool(re.match(r"^Chapter\s+\d+$", raw_name, re.IGNORECASE))

        if is_generic or not raw_name:
            name = f"Capítulo {num:02d}"
            name_custom = False
        else:
            name = raw_name
            name_custom = True

        chapters.append({
            "number": num,
            "timestamp": timestamps[num],
            "name": name,
            "name_custom": name_custom,
        })

    return chapters


# ══════════════════════════════════════════════════════════════════════
#  MEDIAINFO + DOVI_TOOL — Análisis extendido
# ══════════════════════════════════════════════════════════════════════

def find_main_m2ts(share_path: str) -> str | None:
    """Localiza el m2ts más grande en BDMV/STREAM/ (feature principal)."""
    stream_dir = Path(share_path) / "BDMV" / "STREAM"
    if not stream_dir.exists():
        stream_dir = Path(share_path) / "bdmv" / "stream"
        if not stream_dir.exists():
            return None
    m2ts_files = list(stream_dir.glob("*.m2ts")) + list(stream_dir.glob("*.M2TS"))
    if not m2ts_files:
        return None
    return str(max(m2ts_files, key=lambda p: p.stat().st_size))


async def run_pgs_packet_counts(m2ts_path: str) -> dict[int, int]:
    """Cuenta paquetes por pista PGS del m2ts usando ffprobe -count_packets.

    El número de paquetes PES es el proxy más fiable del volumen real de
    subtítulos (forzado vs completo vs audiodescripción), ya que MediaInfo
    y el bit_rate de ffprobe devuelven N/A para PGS.

    Devuelve {stream_index: packet_count}. Dict vacío si ffprobe falla.
    Tarda ~1-3 min en m2ts de 60GB.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-select_streams", "s",
            "-count_packets",
            "-show_entries", "stream=index,nb_read_packets",
            "-of", "csv=p=0",
            m2ts_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode != 0:
            _logger.warning("ffprobe packet count falló (%d): %s", proc.returncode, stderr.decode()[:200])
            return {}
        result: dict[int, int] = {}
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.strip().split(",")
            if len(parts) >= 2:
                try:
                    idx, pkt = int(parts[0]), int(parts[1])
                    result[idx] = pkt
                except ValueError:
                    continue
        return result
    except asyncio.TimeoutError:
        _logger.warning("ffprobe packet count: timeout tras 10 min")
        return {}
    except Exception as e:
        _logger.warning("ffprobe packet count error: %s", e)
        return {}


async def run_mediainfo(file_path: str) -> MediaInfoResult:
    """
    Ejecuta ``mediainfo --Output=JSON`` sobre un m2ts o MKV.
    Devuelve MediaInfoResult con datos por pista.
    """
    proc = await asyncio.create_subprocess_exec(
        MEDIAINFO_BIN, "--Output=JSON", file_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        _logger.warning("mediainfo falló (%d): %s", proc.returncode, stderr.decode()[:200])
        return MediaInfoResult(source_path=file_path)

    raw = json.loads(stdout.decode("utf-8", errors="replace"))
    tracks_raw = raw.get("media", {}).get("track", [])

    mi_tracks: list[MediaInfoTrack] = []
    source_size = 0

    for t in tracks_raw:
        ttype = t.get("@type", "").lower()

        if ttype == "general":
            size_str = t.get("FileSize")
            if size_str:
                try:
                    source_size = int(size_str)
                except (ValueError, TypeError):
                    pass
            continue

        if ttype == "menu":
            continue

        mapped_type = {"video": "video", "audio": "audio", "text": "text"}.get(ttype, ttype)

        bitrate = 0
        br_str = t.get("BitRate") or t.get("BitRate_Nominal") or ""
        if br_str:
            try:
                bitrate = int(float(br_str)) // 1000
            except (ValueError, TypeError):
                pass

        stream_order = -1
        so_str = t.get("StreamOrder") or t.get("@typeorder") or ""
        if so_str:
            try:
                stream_order = int(so_str)
            except (ValueError, TypeError):
                pass

        mi_tracks.append(MediaInfoTrack(
            track_type=mapped_type,
            stream_order=stream_order,
            bitrate_kbps=bitrate,
            format_commercial=t.get("Format_Commercial_IfAny", ""),
            channels=int(t.get("Channels", 0) or 0),
            channel_layout=t.get("ChannelLayout", ""),
            compression_mode=t.get("Compression_Mode", ""),
            bit_depth=int(t.get("BitDepth", 0) or 0),
            color_primaries=t.get("colour_primaries", ""),
            transfer_characteristics=t.get("transfer_characteristics", ""),
            resolution=(
                f"{t.get('Width', '')}x{t.get('Height', '')}"
                if t.get("Width") and ttype == "text" else ""
            ),
        ))

    return MediaInfoResult(
        source_path=file_path,
        source_size_bytes=source_size,
        tracks=mi_tracks,
        raw_json=raw,
    )


async def run_dovi_analysis(m2ts_path: str) -> DoviInfo | None:
    """
    Pipeline dovi_tool: ffmpeg extrae 30s del EL → extract-rpu → info --summary.
    Devuelve DoviInfo o None si falla. Limpia ficheros temporales siempre.
    """
    pid = os.getpid()
    tmp_el = str(Path(TMP_DIR) / f"_el_sample_{pid}.hevc")
    tmp_rpu = str(Path(TMP_DIR) / f"_rpu_{pid}.bin")

    try:
        # Paso 1: Extraer 30s del Enhancement Layer
        proc = await asyncio.create_subprocess_exec(
            FFMPEG_BIN, "-y", "-i", m2ts_path,
            "-map", "0:v:1", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
            "-t", "30", "-f", "hevc", tmp_el,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            _logger.info("ffmpeg EL extraction falló (sin EL?): %s", stderr.decode()[:200])
            return None

        if not Path(tmp_el).exists() or Path(tmp_el).stat().st_size < 1000:
            _logger.info("EL sample demasiado pequeño o inexistente")
            return None

        # Paso 2: Extraer RPU
        proc = await asyncio.create_subprocess_exec(
            DOVI_TOOL_BIN, "extract-rpu", tmp_el, "-o", tmp_rpu,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            _logger.warning("dovi_tool extract-rpu falló: %s", stderr.decode()[:200])
            return None

        # Paso 3: Analizar RPU
        proc = await asyncio.create_subprocess_exec(
            DOVI_TOOL_BIN, "info", "--summary", tmp_rpu,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            _logger.warning("dovi_tool info falló: %s", stderr.decode()[:200])
            return None

        summary = stdout.decode("utf-8", errors="replace")
        return _parse_dovi_summary(summary)

    except Exception as e:
        _logger.warning("dovi_tool pipeline falló: %s", e)
        return None
    finally:
        Path(tmp_el).unlink(missing_ok=True)
        Path(tmp_rpu).unlink(missing_ok=True)


def _parse_dovi_summary(summary: str) -> DoviInfo:
    """Parsea el output de ``dovi_tool info --summary``."""
    info = DoviInfo(raw_summary=summary)

    m = re.search(r"Profile:\s+(\d+)\s+\((FEL|MEL)\)", summary)
    if m:
        info.profile = int(m.group(1))
        info.el_type = m.group(2)

    m = re.search(r"DM version:\s+\d+\s+\(CM (v[\d.]+)\)", summary)
    if m:
        info.cm_version = m.group(1)

    m = re.search(r"Scene/shot count:\s+(\d+)", summary)
    if m:
        info.scene_count = int(m.group(1))

    m = re.search(r"Frames:\s+(\d+)", summary)
    if m:
        info.frame_count = int(m.group(1))

    info.has_l1 = "content light level (L1)" in summary
    info.has_l2 = "L2 trims" in summary
    info.has_l5 = "L5 offsets" in summary and "N/A" not in summary.split("L5 offsets")[1][:50] if "L5 offsets" in summary else False
    info.has_l6 = "L6 metadata" in summary

    return info


def enrich_tracks_with_mediainfo(bdinfo: BDInfoResult, mi: MediaInfoResult) -> None:
    """Enriquece BDInfoResult con datos de MediaInfo (bitrate, HDR, codecs)."""
    mi_video = [t for t in mi.tracks if t.track_type == "video"]
    mi_audio = [t for t in mi.tracks if t.track_type == "audio"]
    mi_subs  = [t for t in mi.tracks if t.track_type == "text"]

    # Vídeo: BL
    bl_tracks = [v for v in bdinfo.video_tracks if not v.is_el]
    if bl_tracks and mi_video:
        bl = bl_tracks[0]
        mv = mi_video[0]
        bl.bitrate_kbps = mv.bitrate_kbps
        hdr_fmt = "HDR10" if mv.transfer_characteristics == "PQ" else ("HLG" if mv.transfer_characteristics == "HLG" else "")
        bl.hdr = HdrMetadata(
            hdr_format=hdr_fmt,
            color_primaries=mv.color_primaries,
            transfer_characteristics=mv.transfer_characteristics,
            bit_depth=mv.bit_depth,
        )
        # MaxCLL/MaxFALL del JSON raw
        if mi.raw_json:
            for t in mi.raw_json.get("media", {}).get("track", []):
                if t.get("@type") == "Video" and t.get("@typeorder", "1") == "1":
                    try:
                        bl.hdr.max_cll = int(t["MaxCLL"]) if t.get("MaxCLL") else None
                    except (ValueError, TypeError):
                        pass
                    try:
                        bl.hdr.max_fall = int(t["MaxFALL"]) if t.get("MaxFALL") else None
                    except (ValueError, TypeError):
                        pass
                    bl.hdr.mastering_display_luminance = t.get("MasteringDisplay_Luminance", "")
                    break

    # Audio: filtrar cores embebidos, luego mapear 1:1
    mi_audio_filtered = _filter_mediainfo_audio_cores(mi_audio, mi)
    for i, at in enumerate(bdinfo.audio_tracks):
        if i < len(mi_audio_filtered):
            ma = mi_audio_filtered[i]
            at.bitrate_kbps = ma.bitrate_kbps
            at.format_commercial = ma.format_commercial
            at.channel_layout = ma.channel_layout
            at.compression_mode = ma.compression_mode

    # Subtítulos
    for i, st in enumerate(bdinfo.subtitle_tracks):
        if i < len(mi_subs):
            st.resolution = mi_subs[i].resolution


def _filter_mediainfo_audio_cores(mi_audio: list[MediaInfoTrack], mi: MediaInfoResult) -> list[MediaInfoTrack]:
    """Filtra cores AC-3 embebidos en TrueHD (mismo PID en MediaInfo)."""
    if not mi.raw_json:
        return mi_audio

    audio_raw = [t for t in mi.raw_json.get("media", {}).get("track", []) if t.get("@type") == "Audio"]
    seen_ids: dict[str, int] = {}
    core_indices: set[int] = set()

    for idx, t in enumerate(audio_raw):
        tid = t.get("ID", str(idx))
        if tid in seen_ids:
            fmt = t.get("Format", "").lower()
            if "ac-3" in fmt or "ac3" in fmt:
                core_indices.add(idx)
            else:
                core_indices.add(seen_ids[tid])
        else:
            seen_ids[tid] = idx

    if not core_indices:
        return mi_audio

    return [t for i, t in enumerate(mi_audio) if i not in core_indices]


def enrich_dovi(bdinfo: BDInfoResult, dovi: DoviInfo) -> None:
    """Actualiza BDInfoResult con datos definitivos de dovi_tool."""
    bl_tracks = [v for v in bdinfo.video_tracks if not v.is_el]
    if bl_tracks:
        bl_tracks[0].dovi = dovi

    bdinfo.has_fel = (dovi.el_type == "FEL")
    bdinfo.fel_reason = (
        f"Dolby Vision Profile {dovi.profile} ({dovi.el_type}) "
        f"detectado via dovi_tool — CM {dovi.cm_version}"
    )


# ══════════════════════════════════════════════════════════════════════
#  ORQUESTADOR: Análisis completo
# ══════════════════════════════════════════════════════════════════════

async def run_full_analysis(
    share_path: str,
    log_callback=None,
) -> tuple[BDInfoResult, str, list[dict]]:
    """
    Análisis completo del disco montado:
      1. mkvmerge -J (requerido)
      2. Capítulos del MPLS (requerido)
      3. MediaInfo sobre m2ts principal (opcional)
      4. dovi_tool RPU analysis (opcional, solo si hay EL)

    Devuelve (bdinfo_result, mpls_path, chapters_raw).
    """
    # 1. mkvmerge -J
    if log_callback:
        await log_callback("[Fase A] Identificando MPLS y ejecutando mkvmerge -J…")
    mkvmerge_data, mpls_path = await run_mkvmerge_identify(share_path, log_callback)
    bdinfo = parse_mkvmerge_json(mkvmerge_data)

    # 2. Capítulos
    if log_callback:
        await log_callback("[Fase A] Extrayendo capítulos del MPLS…")
    chapters_raw = parse_mpls_chapters(mpls_path)

    # 3. MediaInfo sobre m2ts principal
    m2ts_path = find_main_m2ts(share_path)
    if m2ts_path:
        bdinfo.main_m2ts = Path(m2ts_path).name
        size_gb = Path(m2ts_path).stat().st_size / 1e9
        if log_callback:
            await log_callback(f"[Fase A] M2TS principal: {bdinfo.main_m2ts} ({size_gb:.1f} GB)")

        try:
            if log_callback:
                await log_callback("[Fase A] Ejecutando MediaInfo…")
            mi = await run_mediainfo(m2ts_path)
            bdinfo.mediainfo_result = mi
            enrich_tracks_with_mediainfo(bdinfo, mi)
            if log_callback:
                await log_callback(f"[Fase A] MediaInfo: {len(mi.tracks)} pistas analizadas")
        except Exception as e:
            _logger.warning("MediaInfo falló (no bloquea): %s", e)
            if log_callback:
                await log_callback(f"[Fase A] ⚠️ MediaInfo falló: {e}")

        # 3b. Contar paquetes PES de cada subtítulo PGS (ffprobe -count_packets).
        # Proxy fiable del volumen de subtítulo — permite distinguir forzado,
        # completo y audiodescripción con precisión. Tarda 1-3 min en m2ts de 60GB.
        try:
            if log_callback:
                await log_callback("[Fase A] Contando paquetes PGS (ffprobe, 1-3 min)…")
            pgs_packets = await run_pgs_packet_counts(m2ts_path)
            if pgs_packets:
                # ffprobe devuelve índices ascendentes; asignamos por posición
                # a subtitle_tracks (ambos en el mismo orden del m2ts).
                sorted_counts = [pgs_packets[k] for k in sorted(pgs_packets.keys())]
                for raw_sub, pc in zip(bdinfo.subtitle_tracks, sorted_counts):
                    raw_sub.packet_count = pc
                if log_callback:
                    preview = ", ".join(
                        f"{s.language[:3]}={s.packet_count}"
                        for s in bdinfo.subtitle_tracks[:12]
                    )
                    await log_callback(f"[Fase A] PGS packets: {preview}…")
        except Exception as e:
            _logger.warning("ffprobe packet count falló (no bloquea): %s", e)
            if log_callback:
                await log_callback(f"[Fase A] ⚠️ ffprobe packet count falló: {e}")

        # 4. dovi_tool (solo si hay EL)
        has_el = any(t.is_el for t in bdinfo.video_tracks) or bdinfo.has_fel
        if has_el:
            try:
                if log_callback:
                    await log_callback("[Fase A] Analizando Dolby Vision con dovi_tool…")
                dovi = await run_dovi_analysis(m2ts_path)
                if dovi:
                    enrich_dovi(bdinfo, dovi)
                    if log_callback:
                        await log_callback(
                            f"[Fase A] Dolby Vision: Profile {dovi.profile} ({dovi.el_type}), CM {dovi.cm_version}"
                        )
            except Exception as e:
                _logger.warning("dovi_tool falló (no bloquea): %s", e)
                if log_callback:
                    await log_callback(f"[Fase A] ⚠️ dovi_tool falló: {e}")
    else:
        if log_callback:
            await log_callback("[Fase A] ⚠️ No se encontró m2ts — análisis extendido omitido")

    return bdinfo, mpls_path, chapters_raw
