"""
mkv_analyze.py — Tab 2: Análisis y edición de MKVs existentes

Responsabilidades:
  1. Analizar un MKV con ``mkvmerge -J`` + ``mkvextract chapters``.
  2. Aplicar ediciones in-place con ``mkvpropedit`` (O(1), sin remux).
  3. Si hay reorden de pistas, remuxar con ``mkvmerge -o`` (copia completa).

Todas las operaciones son stateless — no se persiste nada en disco.
El estado de edición vive en el frontend.

Cache: el resultado del análisis se persiste en /config/mkv_audits/ vía
triple-fingerprint del MKV (ver storage.read_mkv_cache). Re-abrir el
mismo MKV es instantáneo. La invalidación es automática si el MKV
cambia (mtime/size/SHA-1MB) o si se bumpea CACHE_VERSION_BASIC tras
mejorar un motor del pipeline (mkvmerge parsing, MediaInfo, PGS, dovi).
"""
from i18n import t as tr
import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET

from models import Chapter, ContainerInfo, DoviInfo, HdrMetadata, MkvAnalysisResult, MkvEditRequest, MkvTrackInfo

_logger = logging.getLogger(__name__)


async def _probe_duration_seconds(media_path: str) -> float:
    """Duración en segundos vía ffprobe (0.0 si no se puede determinar).

    La necesita el pipeline de extracción para calcular el % de avance: sin
    fichero intermedio que medir, el progreso sale del `time=` que ffmpeg
    escribe en stderr, y eso solo es un porcentaje si se sabe el total.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", media_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return float(out.decode().strip())
    except Exception:
        return 0.0

MKVMERGE_BIN    = "mkvmerge"
MKVPROPEDIT_BIN = "mkvpropedit"

# `MaxCLL`/`MaxFALL` de MediaInfo llegan como "300 cd/m2".
_RE_PRIMER_ENTERO = re.compile(r"\s*(\d+)")
MKVEXTRACT_BIN  = "mkvextract"
FFMPEG_BIN      = "ffmpeg"
DOVI_TOOL_BIN   = "dovi_tool"

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/mnt/output")
TMP_DIR    = os.environ.get("TMP_DIR", "/mnt/tmp")

# Versión del clasificador del análisis básico (mkvmerge + MediaInfo + PGS
# + dovi sample). Bumpear cuando se cambie la LÓGICA de cualquiera de esos
# motores (no cuando se arregle un typo del log). El cache de cualquier MKV
# analizado con una versión distinta se invalida automáticamente y se
# re-analiza al próximo open. Historial:
#   v1 (mayo 2026) — versión inicial del cache.
#   v2 (sep 2026)  — la ficha técnica de MediaInfo: `ContainerInfo`, el
#                    HDR declarado (formato crudo, compatibilidad, perfil
#                    DV, capas) y los campos de pista (perfil/nivel/tier,
#                    modo de tasa, tamaño y % del fichero, delay). Un
#                    bloque v1 no los trae, y sin invalidar se leerían
#                    como «no presentes» en vez de «no medidos».
CACHE_VERSION_BASIC = 2

# Versión del análisis profundo del RPU (L8/L2 combos + classify_l8 +
# classify_l8_quality). Bumpear cuando cambien los umbrales del clasificador
# en rpu_analyze.py o se añadan campos cuantitativos nuevos.
#   v1 (mayo 2026) — versión inicial del quality audit.
#   v2 (jun 2026)  — recalibrado classify_l8 (default ya no salta solo por
#                    %neutro alto con muchos combos: audit #3) + flags
#                    mid_contrast/clip_trim sólo si != 2048 (audit #14).
CACHE_VERSION_QUALITY = 2


# ── La ficha técnica de MediaInfo ────────────────────────────────────

def _pistas_raw(raw: dict | None, tipo: str) -> list[dict]:
    """Los tracks de un tipo del JSON de MediaInfo, en orden de stream.

    MediaInfo NO garantiza el orden de la lista, así que se ordena por
    `StreamOrder` — el mismo criterio que ya usa el enriquecimiento de
    audio, y por el mismo motivo: emparejar por posición metía el bitrate
    en la pista de al lado.
    """
    if not raw:
        return []
    ts = [t for t in (raw.get("media") or {}).get("track") or []
          if t.get("@type") == tipo]
    return sorted(ts, key=lambda t: int(str(t.get("StreamOrder") or 0) or 0))


def _parte_dv(valor: str) -> str:
    """La parte Dolby Vision de un campo de MediaInfo con dos mitades.

    Con DV y HDR10 a la vez, MediaInfo emite los campos emparejados por
    ` / `: `HDR_Format_Profile` vale `'dvhe.07 / '` —la primera mitad es
    la del Dolby Vision y la segunda, vacía, la del SMPTE ST 2086—. Sin
    esto el valor llega con la barra y el espacio pegados detrás.
    """
    return str(valor or "").split("/")[0].strip()


def _entero(valor) -> int:
    """El primer entero de un campo de MediaInfo, o 0."""
    try:
        return int(float(str(valor).strip()))
    except (TypeError, ValueError):
        return 0


def _volcar_hdr_declarado(hdr_meta, rv: dict) -> None:
    """Lo que MediaInfo dice del HDR, sin reinterpretar.

    `hdr_format` lo DERIVA la app de la curva de transferencia («PQ» →
    «HDR10»), así que en un disco con Dolby Vision decía «HDR10» a secas
    y se perdían el perfil declarado, las capas y —sobre todo— con qué es
    compatible, que es la pregunta práctica de «¿esto lo reproduce mi
    equipo?».

    Es una función aparte y no cuatro líneas dentro de `analyze_mkv`
    porque ahí no hay forma de ejercitarla: una mutación que la vaciara
    entera pasaba en verde.
    """
    hdr_meta.max_cll = _nits_de_mediainfo(rv.get("MaxCLL"))
    hdr_meta.max_fall = _nits_de_mediainfo(rv.get("MaxFALL"))
    hdr_meta.mastering_display_luminance = str(rv.get("MasteringDisplay_Luminance") or "")
    hdr_meta.mastering_display_primaries = str(rv.get("MasteringDisplay_ColorPrimaries") or "")
    hdr_meta.hdr_format_raw = str(rv.get("HDR_Format") or "")
    hdr_meta.hdr_format_compatibility = str(rv.get("HDR_Format_Compatibility") or "")
    hdr_meta.dv_profile_string = _parte_dv(rv.get("HDR_Format_Profile"))
    hdr_meta.dv_level = _parte_dv(rv.get("HDR_Format_Level"))
    hdr_meta.dv_layers = _parte_dv(rv.get("HDR_Format_Settings"))
    hdr_meta.matrix_coefficients = str(rv.get("matrix_coefficients") or "")
    hdr_meta.colour_range = str(rv.get("colour_range") or "")
    hdr_meta.chroma_subsampling = str(rv.get("ChromaSubsampling") or "")


def _contenedor_de(raw: dict | None, tamano_total: int) -> "ContainerInfo | None":
    """`ContainerInfo` del track General, o None si MediaInfo no lo trae."""
    generales = _pistas_raw(raw, "General")
    if not generales:
        return None
    g = generales[0]
    extra = g.get("extra") or {}
    tasa = _entero(g.get("OverallBitRate"))
    return ContainerInfo(
        format=str(g.get("Format") or ""),
        format_version=str(g.get("Format_Version") or ""),
        title=str(g.get("Title") or g.get("Movie") or ""),
        overall_bitrate_kbps=tasa // 1000,
        overall_bitrate_mode=str(g.get("OverallBitRate_Mode") or ""),
        encoded_application=str(g.get("Encoded_Application") or ""),
        encoded_library=str(g.get("Encoded_Library") or ""),
        encoded_date=str(g.get("Encoded_Date") or ""),
        imdb_id=str(extra.get("IMDB") or ""),
        tmdb_id=str(extra.get("TMDB") or ""),
        is_streamable=str(g.get("IsStreamable") or "").lower() == "yes",
    )


def _ficha_de_pista(pista, raw_track: dict, tamano_total: int) -> None:
    """Vuelca en la pista los campos de ficha técnica de su track raw.

    Muta en vez de devolver porque el enriquecimiento de MediaInfo ya
    funciona así, pista a pista, y mezclar los dos estilos en la misma
    función la haría ilegible.
    """
    pista.format_profile = str(raw_track.get("Format_Profile") or "")
    pista.format_level = str(raw_track.get("Format_Level") or "")
    pista.format_tier = str(raw_track.get("Format_Tier") or "")
    pista.framerate_mode = str(raw_track.get("FrameRate_Mode") or "")
    pista.bitrate_mode = str(raw_track.get("BitRate_Mode") or "")
    pista.channel_positions = str(raw_track.get("ChannelPositions") or "")
    tam = _entero(raw_track.get("StreamSize"))
    pista.stream_size_bytes = tam
    if tam and tamano_total:
        pista.stream_size_pct = round(tam * 100 / tamano_total, 2)
    try:
        pista.delay_ms = round(float(raw_track.get("Delay") or 0) * 1000, 1)
    except (TypeError, ValueError):
        pista.delay_ms = 0.0



def _quality_workdir_base() -> str | None:
    """Base donde van los temporales de auditoría (HEVC ~45 GB, RPU, JSON).

    Devuelve TMP_DIR (/mnt/tmp, SSD grande) si es usable, o None para caer al
    tempdir por defecto solo en dev local sin /mnt/tmp. Centraliza el `dir=`
    de los mkdtemp para que el HEVC NUNCA caiga al /tmp del contenedor —
    tmpfs pequeño en QNAP → "No space left on device" a mitad de ffmpeg
    (docker-compose.yml avisa: "NUNCA dejar fallback a /tmp")."""
    try:
        Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
        return TMP_DIR
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════
#  ANÁLISIS
# ══════════════════════════════════════════════════════════════════════

def _nits_de_mediainfo(valor) -> int | None:
    """Extrae el número de un campo de luminancia de MediaInfo.

    MediaInfo devuelve `MaxCLL` y `MaxFALL` **con unidad**: `'300 cd/m2'`, no
    `'300'`. El código hacía `int(valor)` directamente, así que lanzaba
    ValueError y un `except: pass` lo dejaba en None — verificado sobre los 20
    MKVs cacheados del NAS: los 20 tenían `max_cll: None`, y esos dos valores
    nunca han llegado a la radiografía DV+HDR ni a las líneas de referencia
    del perfil de luminancia.

    Se acepta también el número puro, por si alguna versión de MediaInfo lo
    diera así.
    """
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    m = _RE_PRIMER_ENTERO.match(texto)
    return int(m.group(1)) if m else None


async def analyze_mkv(
    mkv_path: str,
    progress_callback=None,
    pgs_progress_callback=None,
    use_cache: bool = True,
) -> MkvAnalysisResult:
    """
    Analiza un MKV existente: pistas, capítulos, metadatos.

    Pipeline: mkvmerge -J + mkvextract chapters + MediaInfo + ffprobe packet
    counts + dovi_tool info. En un MKV grande (40-60 GB) puede tardar 1-3 min,
    dominado por el conteo de paquetes PGS.

    Si se pasa ``progress_callback(step: str)``, se notifica al arrancar cada
    paso costoso para que el frontend pueda mostrar un modal de progreso.
    Pasos emitidos: ``identify``, ``mediainfo``, ``pgs``, ``dovi`` y
    ``cache_hit`` cuando se sirve del cache.

    Si se pasa ``pgs_progress_callback(pct: float, eta_s: int)``, durante el
    conteo de paquetes ffprobe se emite progreso real basado en bytes leídos
    (vía /proc/{pid}/io), exactamente como en Tab 1.

    Si ``use_cache=True`` (default), antes de ejecutar el pipeline se busca
    en /config/mkv_audits/ por fingerprint. Hit válido → return inmediato.
    Tras un miss, el resultado se persiste para servir openings posteriores
    instantáneamente. ``use_cache=False`` fuerza pipeline fresh y reescribe
    el cache (usado por el botón "↻ Re-analizar" del frontend).
    """
    from storage import compute_mkv_fingerprint, read_mkv_cache

    async def _emit(step: str):
        if progress_callback:
            try:
                await progress_callback(step)
            except Exception:
                pass

    if not Path(mkv_path).exists():
        raise RuntimeError(tr('mkv_analyze.fichero_no_encontrado', mkv_path=mkv_path))

    # ── Cache check ──────────────────────────────────────────────────
    # Triple-fingerprint barato (~20 ms para SHA del primer 1 MB).
    fingerprint = compute_mkv_fingerprint(mkv_path) if use_cache else None
    if fingerprint:
        cached = read_mkv_cache(fingerprint, CACHE_VERSION_BASIC, CACHE_VERSION_QUALITY)
        if cached and cached.get("basic"):
            await _emit("cache_hit")
            try:
                # Reconstruir el MkvAnalysisResult desde el JSON cacheado.
                # mediainfo_raw se excluyó del cache (50-80 KB cada uno) →
                # llega como None. El modal "Datos MKV" no lo consume, así
                # que el comportamiento es idéntico para el usuario.
                result = MkvAnalysisResult.model_validate(cached["basic"])
                # Si hay quality cache VÁLIDO, inyectarlo en el DoviInfo del
                # resultado para que la card de Auditoría aparezca poblada
                # directamente al abrir el MKV. Si el cache.quality es basura
                # (resultado de un pipeline anterior que falló silenciosamente
                # con frames=0), lo IGNORAMOS — la card mostrará el estado "no
                # auditado" con CTA y el usuario podrá relanzar el audit.
                quality_block = cached.get("quality")
                if quality_block and result.dovi:
                    if _quality_payload_is_valid(quality_block):
                        # El veredicto se cacheó como TEXTO, en el idioma que
                        # la app tenía cuando se auditó el MKV. Los números
                        # son neutros, así que la prosa se rehace al servirla
                        # en vez de invalidar el bloque — re-auditar son ~10
                        # min de extract-rpu por MKV.
                        quality_block = regenerar_textos_del_veredicto(
                            quality_block, flags_dv_de(result.dovi),
                        )
                        for k, v in quality_block.items():
                            if hasattr(result.dovi, k):
                                setattr(result.dovi, k, v)
                        _logger.info(
                            "MKV cache HIT (basic+quality) para %s",
                            Path(mkv_path).name,
                        )
                    else:
                        _logger.warning(
                            "MKV cache HIT (basic) para %s — quality descartado (frames=%s, cls=%r)",
                            Path(mkv_path).name,
                            quality_block.get("quality_total_frames_rpu"),
                            quality_block.get("quality_classification"),
                        )
                else:
                    _logger.info("MKV cache HIT (basic) para %s", Path(mkv_path).name)
                return result
            except Exception as e:
                _logger.warning(
                    "MKV cache inválido para %s (re-analizando): %s",
                    Path(mkv_path).name, e,
                )
                # Sigue al pipeline normal — el cache se sobrescribirá.

    # ── mkvmerge -J ──────────────────────────────────────────────
    await _emit("identify")
    proc = await asyncio.create_subprocess_exec(
        MKVMERGE_BIN, "-J", mkv_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode >= 2:
        raise RuntimeError(tr('mkv_analyze.mkvmerge_j_fallo', p1=stderr.decode()[:300]))

    data = json.loads(stdout.decode("utf-8", errors="replace"))

    # ── Pistas ───────────────────────────────────────────────────
    tracks = []
    for t in data.get("tracks", []):
        props = t.get("properties", {})
        # FPS desde default_duration (nanosegundos por frame) — solo vídeo.
        # mkvmerge la expone para tracks de video con framerate constante.
        fps_val = 0.0
        default_dur = props.get("default_duration")
        if t.get("type") == "video" and default_dur:
            try:
                # default_duration en ns; fps = 1e9 / dur. Redondeo a 3
                # decimales para coincidir con valores estandar (23.976,
                # 24.000, 25.000, 29.970, etc.).
                fps_val = round(1_000_000_000.0 / float(default_dur), 3)
            except (TypeError, ValueError, ZeroDivisionError):
                fps_val = 0.0
        tracks.append(MkvTrackInfo(
            id=t.get("id", 0),
            type=t.get("type", "video"),
            codec=t.get("codec", ""),
            language=props.get("language", ""),
            name=props.get("track_name", ""),
            flag_default=props.get("default_track", False),
            flag_forced=props.get("forced_track", False),
            channels=props.get("audio_channels"),
            sample_rate=props.get("audio_sampling_frequency"),
            pixel_dimensions=props.get("pixel_dimensions", ""),
            fps=fps_val,
        ))

    # ── Metadatos del contenedor ─────────────────────────────────
    container = data.get("container", {}).get("properties", {})
    title = container.get("title", "")
    duration_ns = container.get("duration")
    duration_s = (duration_ns / 1_000_000_000) if duration_ns else 0.0

    # ── Frame count total = duration × fps (solo vídeo) ──────────
    # mkvmerge no siempre expone NUMBER_OF_FRAMES como propiedad simple;
    # lo derivamos de duration × fps que es exacto para CFR.
    if duration_s > 0:
        for t in tracks:
            if t.type == "video" and t.fps > 0:
                t.frame_count = int(round(duration_s * t.fps))

    # ── FEL: segundo track HEVC a 1080p ──────────────────────────
    hevc_count = 0
    has_fel = False
    for t in tracks:
        if t.type == "video" and ("HEVC" in t.codec or "H.265" in t.codec):
            hevc_count += 1
            if hevc_count > 1 and "1920" in t.pixel_dimensions:
                has_fel = True

    # ── Capítulos ────────────────────────────────────────────────
    chapters = _extract_chapters(mkv_path)

    # ── Fichero ──────────────────────────────────────────────────
    p = Path(mkv_path)

    # ── MediaInfo (enriquecimiento opcional) ────────────────────────
    await _emit("mediainfo")
    hdr_meta = None
    mediainfo_raw = None
    container_info = None
    try:
        from phases.phase_a import run_mediainfo
        mi = await run_mediainfo(mkv_path)
        mediainfo_raw = mi.raw_json

        mi_video = [t for t in mi.tracks if t.track_type == "video"]
        # Ordenadas por stream_order (= orden de pista en el contenedor) para
        # alinearlas con el orden de mkvmerge (por id ascendente). Antes se
        # emparejaban por posición en la lista de MediaInfo, que NO siempre
        # respeta el orden del contenedor → el bitrate/format_commercial (Atmos)
        # podía caer en la pista equivocada (audit #16, solo display). Con
        # stream_order=-1 (no reportado) sorted es estable → sin cambio.
        mi_audio = sorted((t for t in mi.tracks if t.track_type == "audio"),
                          key=lambda t: t.stream_order)
        mi_subs  = sorted((t for t in mi.tracks if t.track_type == "text"),
                          key=lambda t: t.stream_order)
        # Los tracks CRUDOS, para los campos de ficha que `MediaInfoTrack`
        # no modela. Van por separado en vez de ampliar ese modelo porque
        # lo comparte Tab 1 y aquí sólo hace falta leer.
        raw_audio = _pistas_raw(mi.raw_json, "Audio")
        raw_text = _pistas_raw(mi.raw_json, "Text")
        tamano_fichero = p.stat().st_size if p.exists() else 0
        container_info = _contenedor_de(mi.raw_json, tamano_fichero)

        # Enriquecer pistas de vídeo
        video_tracks_list = [t for t in tracks if t.type == "video"]
        if video_tracks_list and mi_video:
            mv = mi_video[0]
            video_tracks_list[0].bitrate_kbps = mv.bitrate_kbps
            video_tracks_list[0].bit_depth = mv.bit_depth
            video_tracks_list[0].color_primaries = mv.color_primaries
            hdr_fmt = "HDR10" if mv.transfer_characteristics == "PQ" else ("HLG" if mv.transfer_characteristics == "HLG" else "")
            video_tracks_list[0].hdr_format = hdr_fmt
            rv = (_pistas_raw(mi.raw_json, "Video") or [{}])[0]
            _ficha_de_pista(video_tracks_list[0], rv, tamano_fichero)
            if hdr_fmt:
                hdr_meta = HdrMetadata(
                    hdr_format=hdr_fmt,
                    color_primaries=mv.color_primaries,
                    transfer_characteristics=mv.transfer_characteristics,
                    bit_depth=mv.bit_depth,
                )
                _volcar_hdr_declarado(hdr_meta, rv)

        # Enriquecer pistas de audio
        audio_idx = 0
        for t in tracks:
            if t.type == "audio" and audio_idx < len(mi_audio):
                ma = mi_audio[audio_idx]
                t.bitrate_kbps = ma.bitrate_kbps
                t.format_commercial = ma.format_commercial
                t.channel_layout = ma.channel_layout
                t.compression_mode = ma.compression_mode
                if audio_idx < len(raw_audio):
                    _ficha_de_pista(t, raw_audio[audio_idx], tamano_fichero)
                audio_idx += 1

        # Enriquecer pistas de subtítulos — resolution del bitmap PGS + bitrate
        sub_idx = 0
        for t in tracks:
            if t.type == "subtitles" and sub_idx < len(mi_subs):
                ms = mi_subs[sub_idx]
                if ms.resolution:
                    t.pixel_dimensions = ms.resolution
                if ms.bitrate_kbps:
                    t.bitrate_kbps = ms.bitrate_kbps
                if sub_idx < len(raw_text):
                    _ficha_de_pista(t, raw_text[sub_idx], tamano_fichero)
                sub_idx += 1

    except Exception as e:
        _logger.warning("MediaInfo falló para MKV %s (no bloquea): %s", mkv_path, e)

    # ── Packet counts de subtítulos bitmap (ffprobe) ─────────────────
    # Proxy fiable de forzado vs completo cuando el flag no está seteado.
    # Reutiliza la función de phase_a que monitoriza bytes leídos por ffprobe
    # para emitir progreso real durante el conteo (~1-3 min en MKVs grandes).
    try:
        sub_tracks_list = [t for t in tracks if t.type == "subtitles"]
        if sub_tracks_list:
            await _emit("pgs")
            from phases.phase_a import run_pgs_packet_counts
            # Pasamos duration_s para que run_pgs_packet_counts muestree los
            # primeros 20 min (si el MKV dura >30 min) y escale por proporción
            # -> misma cadencia, misma clasificación, 5-10× más rápido.
            pkt_counts = await run_pgs_packet_counts(
                mkv_path,
                progress_callback=pgs_progress_callback,
                total_duration_seconds=duration_s,
            )
            # ffprobe devuelve stream_index absoluto dentro del MKV,
            # que coincide con mkvmerge "id" de pista.
            for t in sub_tracks_list:
                if t.id in pkt_counts:
                    t.packet_count = pkt_counts[t.id]
    except Exception as e:
        _logger.warning("ffprobe packet count falló para MKV %s (no bloquea): %s", mkv_path, e)

    # ── dovi_tool (opcional — añade profile, FEL/MEL, CM version, L levels) ──
    await _emit("dovi")
    dovi_info = None
    try:
        hevc_count_val = sum(
            1 for t in tracks
            if t.type == "video" and ("HEVC" in t.codec.upper() or "H.265" in t.codec.upper())
        )
        dovi_info = await _run_dovi_on_mkv(mkv_path, hevc_count_val)
    except Exception as e:
        _logger.warning("dovi_tool falló para MKV %s (no bloquea): %s", mkv_path, e)

    return MkvAnalysisResult(
        file_path=mkv_path,
        file_name=p.name,
        file_size_bytes=p.stat().st_size,
        duration_seconds=duration_s,
        title=title,
        tracks=tracks,
        chapters=chapters,
        has_fel=has_fel,
        hdr=hdr_meta,
        dovi=dovi_info,
        container=container_info,
        mediainfo_raw=mediainfo_raw,
    )


def _compute_provenance_hints(
    n: dict,
    classification: str,
    tier: str,
    dv_flags: dict,
    is_cmv29_only: bool,
) -> list[str]:
    """Heurísticas interpretativas sobre la procedencia del RPU.

    Combina los flags has_l* del DoviInfo básico con el resultado del
    classifier para emitir frases legibles. Devuelve una lista de strings
    (puede estar vacía si nada concluyente). El frontend las muestra como
    bullets debajo del veredicto.

    `n` son los números de `rpu_analyze.numeros_de_l8` — no el RpuAnalysis,
    para que las pistas se puedan regenerar desde la caché (ver
    `_textos_de_calidad`).

    Las heurísticas reflejan patrones observados empíricamente:
      - L11 + L254 + CMv4.0 → master nativo reciente
      - L8 default + L4 ausente → bin sintético (conversión al vuelo)
      - L8 default + L4 presente → bin convertido (p3i, avdvplus)
      - L8 real + sin L11 → master CMv4.0 pre-IQ (antes de 2020)
      - L9 + L10 + L11 todos presentes → metadata completa del master
      - CMv2.9 puro → release pre-CMv4 o BD original sin upgrade
    """
    hints: list[str] = []
    has_l4   = bool(dv_flags.get("has_l4"))
    has_l9   = bool(dv_flags.get("has_l9"))
    has_l10  = bool(dv_flags.get("has_l10"))
    has_l11  = bool(dv_flags.get("has_l11"))
    has_l254 = bool(dv_flags.get("has_l254"))

    if is_cmv29_only:
        hints.append(tr('mkv_analyze.rpu_cmv2_9_puro_blu_ray_original'))
        if n["l2_unique_count"] >= 30:
            hints.append(tr('mkv_analyze.l2_trabajado_por_colorista_grading_dinamico_nativo'))
        if has_l4:
            hints.append(tr('mkv_analyze.l4_presente_compat_trim_legacy'))
        return hints

    # CMv4.0 — combina con classifier
    if classification == "real" and tier in ("full", "core_rich"):
        if has_l11 and has_l254:
            hints.append(tr('mkv_analyze.master_nativo_cmv4_0_reciente_l11_l254'))
        elif has_l254:
            hints.append(tr('mkv_analyze.master_cmv4_0_con_marker_l254_cmv4'))
        if has_l9 and has_l10 and has_l11:
            hints.append(tr('mkv_analyze.metadata_dv_completa_source_primaries_l9_target'))
        if not has_l11 and (has_l9 or has_l10):
            hints.append(tr('mkv_analyze.master_cmv4_0_pre_l11_anterior_a'))

    elif classification == "real" and tier == "core":
        hints.append(tr('mkv_analyze.master_cmv4_0_estandar_calidad_de_release'))
        if not has_l11:
            hints.append(tr('mkv_analyze.sin_l11_dolby_vision_iq_no_aplicable'))

    elif classification == "default":
        if has_l4:
            hints.append(tr('mkv_analyze.bin_convertido_l4_compat_cmv2_9_l8'))
        else:
            hints.append(tr('mkv_analyze.bin_sintetico_equivalente_a_la_conversion_al'))
        if not has_l11:
            hints.append(tr('mkv_analyze.sin_l11_confirma_origen_automatico_los_conversores'))

    elif classification == "tone_mapping":
        # El tercer veredicto, que esta pestaña no conocía: sin trims de
        # colorista pero con el análisis automático de Dolby (L3/L9/L11),
        # que el Blu-ray no trae. `classify_l8` dejó de devolver
        # «indeterminate» hace meses, así que la rama que había aquí era
        # inalcanzable y este caso caía al `else` — veredicto gris
        # «ambiguo» sobre un bin perfectamente descrito.
        hints.append(tr('mkv_analyze.hint_tone_mapping'))
        if has_l11:
            hints.append(tr('mkv_analyze.hint_tone_mapping_l11'))

    return hints


# Los campos del bloque `quality` que son TEXTO — los que `_textos_de_calidad`
# regenera. Van juntos aquí porque son exactamente los que NO se pueden
# confiar a la caché: se escribieron en el idioma que la app tenía cuando se
# hizo el análisis, y el análisis cuesta ~10 min por MKV.
CAMPOS_DE_TEXTO_DEL_VEREDICTO = (
    "quality_reason",
    "quality_tier_label",
    "quality_tier_description",
    "quality_verdict_text",
    "quality_verdict_color",
    "quality_provenance_hints",
)


def flags_dv_de(dovi) -> dict:
    """Los has_l* que las pistas de procedencia necesitan, de un DoviInfo.

    El camino del análisis los saca del bloque `basic` de la caché (en
    `routers/tab2`); al servir una auditoría cacheada ya están en el
    `DoviInfo` reconstruido, y tienen que ser los MISMOS seis o las pistas
    regeneradas no coincidirían con las que se guardaron.
    """
    return {
        "has_l3":   bool(getattr(dovi, "has_l3", False)),
        "has_l4":   bool(getattr(dovi, "has_l4", False)),
        "has_l9":   bool(getattr(dovi, "has_l9", False)),
        "has_l10":  bool(getattr(dovi, "has_l10", False)),
        "has_l11":  bool(getattr(dovi, "has_l11", False)),
        "has_l254": bool(getattr(dovi, "has_l254", False)),
    }


def numeros_del_bloque_quality(q: dict) -> dict:
    """Traduce el bloque `quality` de la caché a los números de `rpu_analyze`.

    La caché lleva los nombres con prefijo (`quality_l8_unique_count`) y las
    funciones puras del classifier los usan sin él; `quality_l2_target_pqs`
    además se guarda como lista y lo que se mira es cuántas hay.
    """
    return {
        "frames_with_cmv40": q.get("quality_frames_with_cmv40") or 0,
        "scene_cuts": q.get("quality_scene_cuts") or 0,
        "l8_unique_count": q.get("quality_l8_unique_count") or 0,
        "l8_neutral_pct": q.get("quality_l8_neutral_pct") or 0.0,
        "l8_has_mid_contrast": bool(q.get("quality_l8_has_mid_contrast")),
        "l8_has_clip_trim": bool(q.get("quality_l8_has_clip_trim")),
        "l2_unique_count": q.get("quality_l2_unique_count") or 0,
        "l2_target_pqs": len(q.get("quality_l2_target_pqs") or []),
        # Los dos que los MOTIVOS interpolan. Faltaban, y como el motivo se
        # re-deriva desde la caché al servirla —para que un análisis hecho
        # en castellano se lea en inglés—, el texto salía con ceros: «hasta
        # 0 unidades sobre el neutro», «aporta 0 combinaciones L3». Un cero
        # con pinta de dato, que es lo que este repo persigue.
        "l8_max_delta": q.get("quality_l8_max_delta") or 0,
        "l3_unique_count": q.get("quality_l3_unique_count") or 0,
    }


def _textos_de_calidad(
    n: dict,
    classification: str,
    tier: str,
    is_cmv29_only: bool,
    dv_flags: dict,
) -> dict:
    """Los seis campos de TEXTO del veredicto, derivados de los números.

    Es el único sitio que los escribe, y se llama desde los dos caminos: al
    terminar la auditoría y al servir una auditoría cacheada. Esto último es
    el motivo de que exista: `/config/mkv_audits/` guarda el veredicto como
    texto, así que un MKV analizado con la app en castellano seguía diciendo
    «CMv2.9 estándar — trims básicos del master» con la app en inglés. Los
    NÚMEROS sí son neutros, y de ellos sale todo esto.

    La alternativa era bumpear `CACHE_VERSION_QUALITY`, que invalida las
    auditorías de todos los usuarios y cuesta ~10 min de `extract-rpu` por
    MKV al reabrirlo — pagar un re-análisis por un cambio de idioma.

    `classification` y `tier` llegan YA decididos (los decide el classifier
    con los combos delante, y la caché los persiste). Aquí solo se explica
    lo que ya está decidido; si esto los recalculara, un bloque cacheado
    podría cambiar de veredicto al leerlo.
    """
    from phases.rpu_analyze import motivo_de_l8, tier_de_l8

    if is_cmv29_only:
        # RPU CMv2.9 puro: el veredicto es sobre L2 (Tier 1 del modelo).
        # Mismo umbral cualitativo: muchos combos = master nativo; pocos = generado.
        l2_count = n["l2_unique_count"]
        l2_pqs = n["l2_target_pqs"]
        if l2_count >= 30 and l2_pqs >= 3:
            verdict = tr('mkv_analyze.veredicto_cmv29_nativo')
            color = "green"
            reason = tr('mkv_analyze.motivo_cmv29_nativo',
                        combos=l2_count, pqs=l2_pqs)
            tier_label = tr('mkv_analyze.tier_cmv29_nativo')
        elif l2_count >= 10:
            verdict = tr('mkv_analyze.veredicto_cmv29_estandar')
            color = "yellow"
            reason = tr('mkv_analyze.motivo_cmv29_estandar',
                        combos=l2_count, pqs=l2_pqs)
            tier_label = "CMv2.9 CORE"
        else:
            verdict = tr('mkv_analyze.veredicto_cmv29_minimo')
            color = "red"
            reason = tr('mkv_analyze.motivo_cmv29_minimo', combos=l2_count)
            tier_label = "CMv2.9 MIN"
        return {
            "quality_reason": reason,
            "quality_tier_label": tier_label,
            "quality_tier_description": reason,
            "quality_verdict_text": verdict,
            "quality_verdict_color": color,
            "quality_provenance_hints": _compute_provenance_hints(
                n, classification, "", dv_flags or {}, True,
            ),
        }

    reason = motivo_de_l8(n, classification)
    _, tier_label, tier_desc = tier_de_l8(n, classification)

    if classification == "real" and tier == "full":
        verdict = tr('mkv_analyze.veredicto_cmv40_full')
        color = "green"
    elif classification == "real" and tier == "core_rich":
        verdict = tr('mkv_analyze.veredicto_cmv40_core_rich')
        color = "green"
    elif classification == "real" and tier == "core":
        verdict = tr('mkv_analyze.veredicto_cmv40_core')
        color = "yellow"
    elif classification == "real":
        # "real" sin tier — minimal real
        verdict = tr('mkv_analyze.veredicto_cmv40_minimal')
        color = "yellow"
    elif classification == "tone_mapping":
        verdict = tr('mkv_analyze.veredicto_cmv40_tone_mapping')
        color = "yellow"
    elif classification == "default":
        verdict = tr('mkv_analyze.veredicto_cmv40_sintetico')
        color = "red"
    else:
        # No debería llegar nada más: `classify_l8` devuelve exactamente
        # `real`, `tone_mapping` o `default`. Se deja el gris como red por
        # si un bloque cacheado trae una clasificación de otra época.
        verdict = tr('mkv_analyze.veredicto_cmv40_ambiguo')
        color = "gray"

    return {
        "quality_reason": reason,
        "quality_tier_label": tier_label,
        "quality_tier_description": tier_desc,
        "quality_verdict_text": verdict,
        "quality_verdict_color": color,
        "quality_provenance_hints": _compute_provenance_hints(
            n, classification, tier, dv_flags or {}, False,
        ),
    }


def regenerar_textos_del_veredicto(q: dict, dv_flags: dict | None = None) -> dict:
    """Devuelve el bloque `quality` con sus textos rehechos en el idioma de ahora.

    Lo que la caché conserva y se respeta son las DECISIONES
    (`quality_classification`, `quality_tier`) y los números; lo que se
    rehace es la prosa. Si el bloque no trae clasificación no se toca nada:
    es una auditoría a medias y `_quality_payload_is_valid` ya la descarta.
    """
    if not isinstance(q, dict) or not q.get("quality_classification"):
        return q
    n = numeros_del_bloque_quality(q)
    is_cmv29_only = n["frames_with_cmv40"] == 0 and n["l8_unique_count"] == 0
    return {
        **q,
        **_textos_de_calidad(
            n,
            str(q.get("quality_classification") or ""),
            str(q.get("quality_tier") or ""),
            is_cmv29_only,
            dv_flags or {},
        ),
    }


def _build_quality_audit_from_rpu_analysis(
    rpu_analysis,
    is_cmv29_only: bool,
    dv_flags: dict | None = None,
) -> dict:
    """Construye el dict de campos quality_* a partir de un RpuAnalysis.

    Devuelve un dict ya con shape para inyectar en DoviInfo.model_validate(
    {**dovi_existing, **quality_dict}). Centraliza la lógica de verdict
    text/color para que el frontend reciba un payload coherente sin
    re-implementar el árbol de decisión en JavaScript.

    is_cmv29_only: True si el RPU NO tiene bloques CMv4.0 (l8_unique_count==0
    Y frames_with_cmv40==0). En ese caso el veredicto se basa en L2.

    dv_flags: dict opcional con has_l4/has_l9/has_l10/has_l11/has_l254 del
    análisis básico (DoviInfo enriquecido por _enrich_dovi_from_json_export).
    Si se pasa, se calculan provenance_hints — si no, lista vacía.
    """
    from phases.rpu_analyze import classify_l8, classify_l8_quality, numeros_de_l8

    base = {
        "quality_total_frames_rpu": rpu_analysis.total_frames,
        "quality_frames_with_cmv40": rpu_analysis.frames_with_cmv40,
        "quality_scene_cuts": rpu_analysis.scene_cuts,
        "quality_l2_unique_count": rpu_analysis.l2_unique_count,
        "quality_l8_max_delta": rpu_analysis.l8_max_delta,
        "quality_l8_frames_sig_pct": rpu_analysis.l8_frames_significativos_pct,
        "quality_l3_unique_count": rpu_analysis.l3_unique_count,
        "quality_l3_frames": rpu_analysis.l3_frames,
        "quality_l2_target_pqs": list(rpu_analysis.l2_target_pqs),
        "quality_l8_unique_count": rpu_analysis.l8_unique_count,
        "quality_l8_neutral_pct": rpu_analysis.l8_neutral_pct,
        "quality_l8_has_mid_contrast": rpu_analysis.l8_has_mid_contrast,
        "quality_l8_has_clip_trim": rpu_analysis.l8_has_clip_trim,
    }
    n = numeros_de_l8(rpu_analysis)

    if is_cmv29_only:
        classification = "real" if rpu_analysis.l2_unique_count >= 10 else "default"
        tier = ""  # tiers son solo CMv4.0
    else:
        # CMv4.0: aplica el classifier de Tab 3 íntegro.
        classification, _ = classify_l8(rpu_analysis)
        tier, _, _ = classify_l8_quality(rpu_analysis)

    return {
        **base,
        "quality_classification": classification,
        "quality_tier": tier,
        **_textos_de_calidad(n, classification, tier, is_cmv29_only, dv_flags or {}),
    }


def _fmt_bytes(n: int) -> str:
    """Formatea bytes como KB / MB / GB legible para el log."""
    if n is None or n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n); i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    return f"{f:.2f} {units[i]}" if i >= 2 else f"{f:.0f} {units[i]}"


def _fmt_elapsed(secs: float) -> str:
    """Formatea segundos como '4m 12s' / '47s' / '1h 23m'."""
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


async def _exportar_una_vez(
    rpu_path: Path,
    tmpdir: Path,
    con_luminancia: bool,
    export_timeout: int,
    log_callback=None,
    register_proc=None,
):
    """Un solo `dovi_tool export --levels` que alimenta a los DOS análisis.

    Devuelve `(RpuAnalysis, payload_de_luminancia | None)`.

    Extraer el RPU del MKV cuesta ~650 s medidos (ffmpeg limitado por disco +
    extract-rpu limitado por CPU, solapados por el pipe) y el export por
    niveles ~7 s: la extracción es el **97 %** del análisis. Hacer los combos
    L8/L2 y el perfil de luminancia por separado significaba pagar dos veces
    ese 97 % para compartir el 3 %. Pidiendo L5 y L6 en la misma pasada, el
    segundo análisis sale prácticamente gratis.

    Si `--levels` no está disponible (dovi_tool anterior a 2.3.3) se cae al
    volcado completo, que da los combos pero **no** el perfil: es el precio de
    un binario viejo, y se avisa en el log.
    """
    from phases.luminance import payload_de_luminancia
    from phases.rpu_analyze import (
        _run_export_levels, analysis_desde_paths, analyze_rpu_combos,
        cargar_niveles,
    )

    extra = ("level5", "level6") if con_luminancia else ()
    rc, stderr, paths = await _run_export_levels(
        rpu_path, tmpdir, "combinado", timeout=export_timeout,
        log_callback=log_callback, register_proc=register_proc,
        niveles_extra=extra,
    )
    utiles = {k: v for k, v in paths.items()
              if v.exists() and v.stat().st_size > 0}
    if rc != 0 or "level1" not in utiles or "level8" not in utiles:
        if log_callback:
            log_callback('[Audit] ' + tr('mkv_analyze.export_levels_no_disponible_se_usa'))
        analisis = await analyze_rpu_combos(
            rpu_path, export_timeout=export_timeout,
            log_callback=log_callback, register_proc=register_proc)
        return analisis, None

    analisis = await asyncio.to_thread(analysis_desde_paths, utiles)
    luz = None
    if con_luminancia:
        niveles = await asyncio.to_thread(cargar_niveles, utiles)
        luz = await asyncio.to_thread(payload_de_luminancia, niveles)
        # `_raw` son los valores PQ crudos, solo para el log: no forman parte
        # del contrato con el frontend.
        crudo = luz.pop("_raw", {})
        if log_callback and crudo:
            log_callback('[Audit] ' + tr('mkv_analyze.l1_crudo_peak_max_pq_avg', p1=crudo.get('max_pq', 0), p2=format(crudo.get('avg_pq', 0), '.0f')))
    for ruta in utiles.values():
        try:
            ruta.unlink(missing_ok=True)
        except OSError:
            pass
    return analisis, luz


async def analyze_rpu_quality_for_mkv(
    mkv_path: str,
    progress_callback=None,
    cancel_check=None,
    register_proc=None,
    dv_flags: dict | None = None,
    log_callback=None,
    con_luminancia: bool = False,
) -> dict:
    """Pipeline de auditoría profunda del RPU de un MKV (Tab 2, on-demand).

    Con `con_luminancia=True` produce ADEMÁS el perfil de luminancia L1, bajo
    la clave `light_profile` del resultado. Sale casi gratis: la extracción del
    RPU es el ~97 % del coste (medido: ~650 s frente a ~7 s de export) y se
    comparte, con L5 y L6 pedidos en la misma pasada del export.

    Pasos (con timings típicos en UHD BD 60 GB):
      1. ffmpeg → HEVC annex-B (2-7 min, I/O-bound NAS).
      2. dovi_tool extract-rpu sobre HEVC (1-2 min, CPU-bound).
      3. analyze_rpu_combos (export -d all + parse JSON, 1-3 min).
      4. classify_l8 + classify_l8_quality + verdict (instantáneo).

    Devuelve un dict con los campos quality_* listos para inyectar en
    DoviInfo. Lanza RuntimeError ante cualquier fallo o cancelación.

    Callbacks:
      - progress_callback(step: str, pct: float, label: str)
      - cancel_check() — debe raise RuntimeError si el usuario canceló.
      - register_proc(proc) — registra el subprocess para que el cancel
        pueda matarlo.
      - log_callback(msg: str) — opcional, recibe líneas detalladas con
        marcadores semánticos (━━━ separadores de paso, $ comandos
        ejecutados, ✓ éxitos, 📋 plan, 🎯 resultado). Si no se pasa,
        el log solo aparece via progress_callback (label por step).

    Ficheros intermedios (HEVC ~45 GB, RPU ~100-200 MB, JSON ~300-500 MB)
    se borran SIEMPRE en finally — nunca se cachean.
    """
    import tempfile
    import time as _t
    from phases.rpu_analyze import analyze_rpu_combos

    def _emit(step: str, pct: float = 0.0, label: str = ""):
        if progress_callback:
            try:
                progress_callback(step, pct, label)
            except Exception:
                pass

    def _log(msg: str):
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    def _check():
        if cancel_check:
            cancel_check()

    p = Path(mkv_path)
    if not p.exists():
        raise RuntimeError(tr('mkv_analyze.mkv_no_encontrado', mkv_path=mkv_path))

    import shutil
    mkv_size = p.stat().st_size
    expected_hevc = int(mkv_size * 0.75)
    # Workdir en /mnt/tmp (NO el /tmp del contenedor — ver _quality_workdir_base).
    workdir_base = _quality_workdir_base()
    # Pre-flight de espacio: un error claro AQUÍ es mucho mejor que un
    # "ffmpeg falló" críptico a los 4 min de extraer 45 GB.
    if workdir_base:
        try:
            free = shutil.disk_usage(workdir_base).free
            if free < int(expected_hevc * 1.1):
                raise RuntimeError(
                    tr('mkv_analyze.espacio_insuficiente_en_la_extraccion_hevc', workdir_base=workdir_base, p2=_fmt_bytes(int(expected_hevc * 1.1)), free=_fmt_bytes(free))
                )
        except FileNotFoundError:
            pass
    tmpdir = Path(tempfile.mkdtemp(prefix="mkv_quality_audit_", dir=workdir_base))
    hevc_path = tmpdir / "video.hevc"
    rpu_path = tmpdir / "rpu.bin"

    audit_start = _t.monotonic()
    _log('[Audit] 📋 Plan' + tr('mkv_analyze.extraer_hevc_del_mkv_extraer_rpu', mkv_size=_fmt_bytes(mkv_size)))
    _log('[Audit] ' + tr('mkv_analyze.workdir_temporal_se_borrara_al_terminar', tmpdir=tmpdir))

    try:
        # ── Pasos 1+2 en una sola pasada ─────────────────────────────
        # El HEVC (45 GB) se extraía entero a disco solo para que
        # extract-rpu lo releyera y borrarlo acto seguido. Aquí no hace
        # falta conservarlo, así que va por un pipe: ffmpeg escribe a
        # stdout y dovi_tool lee de stdin. Misma técnica que la Fase A de
        # CMv4.0, verificada bit a bit (mismo md5 del RPU).
        _check()
        _emit("ffmpeg", 0.0, tr('mkv_analyze.extrayendo_el_rpu_ffmpeg_dovi_tool'))
        _log('━━━ ' + tr('mkv_analyze.fase_a_extraccion_del_rpu_ffmpeg') + ' ━━━')
        _log('[Audit] 📋 Plan' + tr('mkv_analyze.ffmpeg_lee_el_v_0_del', expected_hevc=_fmt_bytes(expected_hevc)))

        async def _pipe_log(msg: str) -> None:
            """Adapta el log del pipeline (async, con marcadores de progreso)
            a los callbacks síncronos de este módulo."""
            if msg.startswith("§§PROGRESS§§"):
                try:
                    d = json.loads(msg[len("§§PROGRESS§§"):])
                    _emit("ffmpeg", float(d.get("pct") or 0), d.get("label") or "")
                except Exception:
                    pass
                return
            _log(msg)

        piped_ok = False
        try:
            from phases.cmv40_pipeline import _ffmpeg_extract_rpu_piped
            t_step = _t.monotonic()
            piped_ok = await _ffmpeg_extract_rpu_piped(
                str(p), rpu_path, hevc_out=None,
                duration=await _probe_duration_seconds(str(p)),
                log_callback=_pipe_log, proc_callback=register_proc,
                offset=0.0, weight=80.0,
                label=tr('mkv_analyze.extrayendo_el_rpu_ffmpeg_dovi_tool'),
                estimated_s=0.0,
            )
        except Exception as e:
            _logger.info("quality audit: pipeline no disponible (%s)", e)
            piped_ok = False
        if piped_ok:
            _check()
            rpu_size = rpu_path.stat().st_size
            _log('[Audit] ' + tr('mkv_analyze.rpu_extraido_en_sin_volcar_el', t_step=_fmt_elapsed(_t.monotonic() - t_step), rpu_size=_fmt_bytes(rpu_size)))
            _emit("extract_rpu", 80.0, tr('cmv40_pipeline.rpu_extraido'))

        if not piped_ok:
            # ── Paso 1: ffmpeg → HEVC annex-B ────────────────────────────
            _check()
            _emit("ffmpeg", 0.0, tr('mkv_analyze.extrayendo_el_hevc_con_ffmpeg'))
            _log('━━━ ' + tr('mkv_analyze.fase_a_extraccion_del_hevc') + ' ━━━')
            _log('[Audit] 📋 Plan' + tr('mkv_analyze.ffmpeg_stream_copy_del_v_0', expected_hevc=_fmt_bytes(expected_hevc)))
            ff_cmd = [
                FFMPEG_BIN, "-y", "-v", "error",
                "-i", str(p),
                "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", str(hevc_path),
            ]
            _log("$ " + " ".join(ff_cmd))
            t_step = _t.monotonic()
            ff_proc = await asyncio.create_subprocess_exec(
                *ff_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            if register_proc:
                register_proc(ff_proc)
            stop_mon = asyncio.Event()
            last_logged_pct = -10

            async def _ff_monitor():
                nonlocal last_logged_pct
                while not stop_mon.is_set():
                    try:
                        if hevc_path.exists() and expected_hevc > 0:
                            size = hevc_path.stat().st_size
                            local_pct = min(99, size * 100 / expected_hevc)
                            global_pct = local_pct * 0.55
                            _emit("ffmpeg", global_pct, tr('mkv_analyze.extrayendo_el_hevc_con_ffmpeg'))
                            # Loguear progreso cada 10% para no saturar
                            if int(local_pct) >= last_logged_pct + 10:
                                last_logged_pct = int(local_pct // 10) * 10
                                _log('[Audit] ' + tr('mkv_analyze.hevc_esperado', local_pct=int(local_pct), size=_fmt_bytes(size), expected_hevc=_fmt_bytes(expected_hevc)))
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(stop_mon.wait(), timeout=1.5)
                    except asyncio.TimeoutError:
                        pass

            mon_task = asyncio.create_task(_ff_monitor())
            try:
                _, stderr_bytes = await asyncio.wait_for(ff_proc.communicate(), timeout=2400)
            except asyncio.TimeoutError:
                try: ff_proc.kill()
                except Exception: pass
                raise RuntimeError(tr('mkv_analyze.ffmpeg_excedio_40_min_extrayendo_hevc'))
            finally:
                stop_mon.set()
                try: await mon_task
                except Exception: pass

            _check()
            ff_stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            if ff_stderr:
                # Emitir cada línea (max 30) — ffmpeg con -v error sólo escupe si
                # hay problema, así que vale la pena verlo todo.
                for ln in ff_stderr.splitlines()[:30]:
                    _log(f"  {ln}")
            if ff_proc.returncode != 0 or not hevc_path.exists() or hevc_path.stat().st_size < 1024:
                err = ff_stderr[:400] or f"rc={ff_proc.returncode}"
                _log('[Audit] ' + tr('mkv_analyze.ffmpeg_fallo', err=err))
                raise RuntimeError(tr('mkv_analyze.ffmpeg_fallo_2', err=err))
            hevc_size = hevc_path.stat().st_size
            _emit("ffmpeg", 55.0, tr('cmv40_pipeline.hevc_extraido'))
            _log('[Audit] ' + tr('mkv_analyze.hevc_extraido_en', t_step=_fmt_elapsed(_t.monotonic() - t_step), hevc_size=_fmt_bytes(hevc_size)))

            # ── Paso 2: dovi_tool extract-rpu ────────────────────────────
            _check()
            _emit("extract_rpu", 55.0, tr('mkv_analyze.extrayendo_el_rpu_dolby_vision_del_hevc'))
            _log('━━━ ' + tr('mkv_analyze.fase_a_extraccion_del_rpu_dolby') + ' ━━━')
            _log('[Audit] 📋 Plan' + tr('mkv_analyze.dovi_tool_extract_rpu_lee_el'))
            dt_cmd = [DOVI_TOOL_BIN, "extract-rpu", str(hevc_path), "-o", str(rpu_path)]
            _log("$ " + " ".join(dt_cmd))
            t_step = _t.monotonic()
            dt_proc = await asyncio.create_subprocess_exec(
                *dt_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            if register_proc:
                register_proc(dt_proc)
            # Sin esto la barra saltaba de 55 % a 80 % de golpe y se quedaba
            # ahí 1-2 min. dovi_tool no dice nada, pero el kernel sabe cuánto
            # lleva leído del HEVC (_ReadProgress → /proc/<pid>/fdinfo).
            from phases.cmv40_pipeline import _ReadProgress
            dt_reader = _ReadProgress(dt_proc.pid, hevc_path)
            stop_dt = asyncio.Event()

            async def _dt_progress():
                while not stop_dt.is_set():
                    pct = dt_reader.sample()
                    if pct is not None:
                        _emit("extract_rpu", 55.0 + pct * 0.25,
                              tr('mkv_analyze.extrayendo_el_rpu_dolby_vision_del_hevc'))
                    try:
                        await asyncio.wait_for(stop_dt.wait(), timeout=1.5)
                    except asyncio.TimeoutError:
                        pass

            dt_mon = asyncio.create_task(_dt_progress())
            try:
                dt_out_bytes, _ = await asyncio.wait_for(dt_proc.communicate(), timeout=1800)
            except asyncio.TimeoutError:
                try: dt_proc.kill()
                except Exception: pass
                raise RuntimeError(tr('mkv_analyze.dovi_tool_extract_rpu_excedio_30'))
            finally:
                stop_dt.set()
                try: await dt_mon
                except Exception: pass
            _check()
            dt_output = dt_out_bytes.decode("utf-8", errors="replace").strip()
            if dt_output:
                # dovi_tool escupe líneas de progreso ("Parsing RPU...") + summary.
                # Mostrar las últimas 20 (las útiles).
                lines = dt_output.splitlines()
                for ln in lines[-20:]:
                    if ln.strip():
                        _log(f"  {ln.strip()}")
            if dt_proc.returncode != 0 or not rpu_path.exists() or rpu_path.stat().st_size < 10:
                err = dt_output[-400:] or f"rc={dt_proc.returncode}"
                _log('[Audit] ' + tr('mkv_analyze.dovi_tool_extract_rpu_fallo_el', err=err))
                raise RuntimeError(
                    tr('mkv_analyze.dovi_tool_extract_rpu_fallo_el_2', err=err)
                )
            rpu_size = rpu_path.stat().st_size
            _log('[Audit] ' + tr('mkv_analyze.rpu_extraido_en', t_step=_fmt_elapsed(_t.monotonic() - t_step), rpu_size=_fmt_bytes(rpu_size)))
            # Liberar HEVC en cuanto tenemos el RPU — son 45 GB que ya no
            # necesitamos. Reduce uso de disco durante el paso 3.
            try:
                hevc_path.unlink(missing_ok=True)
                _log('[Audit] ' + tr('mkv_analyze.hevc_intermedio_liberado_no_se_vuelve'))
            except Exception:
                pass
            _emit("extract_rpu", 80.0, tr('cmv40_pipeline.rpu_extraido'))

        # ── Paso 3: analyze_rpu_combos (export -d all + parse) ───────
        _check()
        _emit("combos", 80.0, tr('mkv_analyze.exportando_niveles_del_rpu_y_agregando_combos'))
        _log('━━━ ' + tr('mkv_analyze.fase_b_combos_l8_l2_y') + ' ━━━')
        _niveles_txt = (tr('mkv_analyze.niveles_con_luminancia')
                        if con_luminancia else "L1, L2, L8")
        _log("[Audit] 📋 Plan: "
             + tr('mkv_analyze.plan_audit_export', niveles=_niveles_txt)
             + (tr('mkv_analyze.plan_audit_y_perfil')
                if con_luminancia else "."))
        _log('[Audit] ' + tr('mkv_analyze.el_export_por_niveles_son_segundos'))
        t_step = _t.monotonic()
        # UN solo export, dos consumidores. Timeout amplio (30 min) porque el
        # export de un RPU full-movie escala con los frames, y streaming del
        # stderr para que el log muestre progreso en vez de callarse minutos.
        rpu_analysis, luz = await _exportar_una_vez(
            rpu_path, tmpdir, con_luminancia,
            export_timeout=1800, log_callback=_log, register_proc=register_proc,
        )
        _check()
        if rpu_analysis.total_frames == 0:
            _log('[Audit] ' + tr('mkv_analyze.dovi_tool_export_devolvio_0_frames'))
            raise RuntimeError(
                tr('mkv_analyze.dovi_tool_export_devolvio_0_frames_2')
            )
        cmv40_pct = (rpu_analysis.frames_with_cmv40 * 100 / rpu_analysis.total_frames
                     if rpu_analysis.total_frames > 0 else 0)
        _log('[Audit] ' + tr('mkv_analyze.frames_analizados_cmv4_0_cobertura_scene', p1=format(rpu_analysis.total_frames, ','), p2=format(cmv40_pct, '.0f'), p3=format(rpu_analysis.scene_cuts, ',')))
        if rpu_analysis.l8_unique_count > 0:
            l8_extras = []
            if rpu_analysis.l8_has_mid_contrast: l8_extras.append("mid_contrast")
            if rpu_analysis.l8_has_clip_trim:    l8_extras.append("clip_trim")
            extras_str = (" · " + " · ".join(l8_extras)) if l8_extras else ""
            _log('[Audit] ' + tr('mkv_analyze.l8_combos_unicos_frames_neutros', p1=format(rpu_analysis.l8_unique_count, ','), p2=format(rpu_analysis.l8_neutral_pct * 100, '.0f'), extras_str=extras_str))
        if rpu_analysis.l2_unique_count > 0:
            _log('[Audit] ' + tr('mkv_analyze.l2_combos_unicos_target_pqs', p1=format(rpu_analysis.l2_unique_count, ','), l2_target_pqs=len(rpu_analysis.l2_target_pqs), l2_target_pqs2=rpu_analysis.l2_target_pqs))
        _log('[Audit] ' + tr('mkv_analyze.combos_agregados_en', t_step=_fmt_elapsed(_t.monotonic() - t_step)))
        _emit("combos", 95.0, tr('mkv_analyze.combos_agregados'))

        # ── Paso 4: classify + verdict ───────────────────────────────
        is_cmv29_only = (rpu_analysis.frames_with_cmv40 == 0
                         and rpu_analysis.l8_unique_count == 0)
        result = _build_quality_audit_from_rpu_analysis(
            rpu_analysis, is_cmv29_only, dv_flags=dv_flags,
        )
        if luz is not None:
            # El perfil viaja aparte del bloque quality_*: son dos análisis del
            # mismo RPU y se cachean en bloques distintos, con su propia versión.
            result["light_profile"] = luz
            _log('[Audit] ' + tr('mkv_analyze.perfil_de_luminancia_frames_peak_nits', p1=format(luz['total_frames'], ','), p2=luz['stats']['peak'], p3=luz['stats']['p95'], p4=len(luz['references']['l5_zones'])))
        elif con_luminancia:
            _log('[Audit] ' + tr('mkv_analyze.sin_perfil_de_luminancia_el_export'))
        _emit("done", 100.0, tr('cmv40_pipeline.analisis_completado'))
        _log(f"[Audit] 🎯 Resultado: {result.get('quality_verdict_text', '—')}")
        if result.get("quality_tier_label"):
            _log('[Audit] ' + tr('mkv_analyze.tier', p1=result['quality_tier_label']))
        if result.get("quality_reason"):
            _log(f"[Audit] {result['quality_reason']}")
        for hint in (result.get("quality_provenance_hints") or [])[:5]:
            _log('[Audit] ├─ ' + str(hint))
        _log(tr('mkv_analyze.auditoria_completada_en', audit_start=_fmt_elapsed(_t.monotonic() - audit_start)))
        return result

    finally:
        # Cleanup atómico — nunca dejamos basura en /mnt/tmp
        try: hevc_path.unlink(missing_ok=True)
        except Exception: pass
        try: rpu_path.unlink(missing_ok=True)
        except Exception: pass
        try: tmpdir.rmdir()
        except Exception: pass


def _quality_payload_is_valid(payload: dict) -> bool:
    """Heurística: ¿el resultado del audit tiene datos reales o es basura?

    Un audit válido siempre tiene total_frames_rpu > 0 (los frames del MKV
    extraídos por dovi_tool export). Si vale 0, fue un export que terminó
    en error sin que el caller lanzara — guardarlo contamina el cache y la
    card de Auditoría aparece "auditada" con stats vacías hasta que el
    usuario fuerza re-auditar.

    También filtramos clasificaciones vacías (sin tier ni veredicto) como
    señal de pipeline incompleto.
    """
    if not isinstance(payload, dict):
        return False
    if (payload.get("quality_total_frames_rpu") or 0) <= 0:
        return False
    if not payload.get("quality_classification"):
        return False
    return True


def persist_mkv_quality_to_cache(mkv_path: str, quality_payload: dict) -> None:
    """Persiste el dict de quality_* en el bloque 'quality' del cache MKV.

    Preserva el bloque 'basic' existente (storage.write_mkv_cache_quality
    lo lee y lo re-escribe). Si el cache no existe todavía (caso edge:
    análisis básico no se hizo por la app), crea el fichero solo con
    quality y los versions queda incompleto — la próxima apertura
    re-analizará basic y mantendrá quality.

    NO persiste si el payload no pasa _quality_payload_is_valid — evita
    cachear resultados basura (frames=0) de un pipeline que falló silenciosamente
    en algún paso. Sin este filtro, un timeout de dovi_tool sin lanzar dejaba
    un quality cacheado vacío que después contaminaba la UI hasta un re-audit
    explícito.
    """
    from storage import compute_mkv_fingerprint, write_mkv_cache_quality
    if not _quality_payload_is_valid(quality_payload):
        _logger.warning(
            "Quality payload no válido (frames=%s, classification=%r) — NO se persiste el cache para %s",
            (quality_payload or {}).get("quality_total_frames_rpu"),
            (quality_payload or {}).get("quality_classification"),
            Path(mkv_path).name,
        )
        return
    try:
        fingerprint = compute_mkv_fingerprint(mkv_path)
        if not fingerprint:
            return
        write_mkv_cache_quality(
            fingerprint=fingerprint,
            cache_version_basic_existing=CACHE_VERSION_BASIC,
            cache_version_quality=CACHE_VERSION_QUALITY,
            quality_payload=quality_payload,
            original_file_path=mkv_path,
        )
        _logger.info("MKV cache WRITE quality para %s", Path(mkv_path).name)
    except Exception as e:
        _logger.warning("Fallo escribiendo quality cache para %s: %s",
                        Path(mkv_path).name, e)


def persist_mkv_basic_to_cache(mkv_path: str, result: MkvAnalysisResult) -> None:
    """Persiste un MkvAnalysisResult en el cache de Tab 2.

    Llamado por el endpoint /api/mkv/analyze tras un análisis exitoso, una
    vez se le ha asignado el ``analysis_log`` capturado durante la operación.

    Excluye ``mediainfo_raw`` del payload: son 50-80 KB de diagnóstico que
    el frontend no consume (el modal "Datos MKV" usa solo analysis_log +
    tracks). Con 10.000 MKVs cacheados, el ahorro es 500-800 MB.

    Errores se loguean pero no se propagan — el cache es best-effort, el
    usuario ya tiene el resultado en memoria. Si falla la escritura, el
    próximo open simplemente re-analizará.
    """
    from storage import compute_mkv_fingerprint, write_mkv_cache_basic
    try:
        fingerprint = compute_mkv_fingerprint(mkv_path)
        if not fingerprint:
            return
        payload = result.model_dump(exclude={"mediainfo_raw"})
        write_mkv_cache_basic(
            fingerprint=fingerprint,
            cache_version_basic=CACHE_VERSION_BASIC,
            cache_version_quality_existing=CACHE_VERSION_QUALITY,
            basic_payload=payload,
            original_file_path=mkv_path,
        )
        _logger.info("MKV cache WRITE para %s", Path(mkv_path).name)
    except Exception as e:
        _logger.warning(
            "Fallo escribiendo cache MKV para %s: %s",
            Path(mkv_path).name, e,
        )


async def _run_dovi_on_mkv(mkv_path: str, hevc_count: int) -> DoviInfo | None:
    """
    Analiza el RPU Dolby Vision de un MKV.

    - Si hay 2+ pistas HEVC (P7 FEL/MEL) usa la Enhancement Layer (v:1).
    - Si hay 1 pista HEVC (P8/P5 single-layer) usa la Base Layer (v:0).
    - Si no hay DV, ffmpeg/extract-rpu fallan y se devuelve None.

    Usa ``dovi_tool extract-rpu --limit 720 mkv_path`` directamente — soporte
    nativo de MKV + limit de frames desde 2.3.0. Evita la pre-extracción HEVC
    con ffmpeg (antes 30s + fichero intermedio de ~300 MB).

    Si el MKV tiene el EL en una pista separada (raro, pero ocurre en algunos
    rips antiguos), dovi_tool puede fallar leyendo el MKV — en ese caso
    fallback al flujo ffmpeg + extract-rpu sobre HEVC intermedio.

    Reutiliza el parser de ``phases.phase_a._parse_dovi_summary``.
    """
    from phases.phase_a import _parse_dovi_summary

    # Token único por llamada (NO os.getpid(): es constante en el proceso, así
    # que dos análisis concurrentes — p. ej. dos pestañas en Tab 2 — usaban los
    # mismos ficheros temporales y uno leía el RPU del otro). audit #15.
    token = uuid.uuid4().hex[:12]
    tmp_rpu = str(Path(TMP_DIR) / f"_mkv_rpu_{token}.bin")
    # Limit de frames para muestreo: 720 ≈ 30s a 24fps, suficiente para
    # identificar profile + CM version + niveles presentes.
    _LIMIT_FRAMES = "720"

    try:
        # Vía rápida: extract-rpu directo del MKV (sin intermedio HEVC)
        proc = await asyncio.create_subprocess_exec(
            DOVI_TOOL_BIN, "extract-rpu",
            "--limit", _LIMIT_FRAMES,
            mkv_path, "-o", tmp_rpu,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0 or not Path(tmp_rpu).exists() or Path(tmp_rpu).stat().st_size < 10:
            # Fallback para MKVs con EL en pista separada: ffmpeg extrae el
            # stream correcto y luego dovi_tool extract-rpu sobre el HEVC.
            _logger.info("dovi_tool MKV direct falló, fallback a ffmpeg (%s): %s",
                         "EL en pista separada" if hevc_count >= 2 else "estructura no estándar",
                         stderr.decode()[:200])
            tmp_hevc = str(Path(TMP_DIR) / f"_mkv_hevc_{token}.hevc")
            map_arg = "0:v:1" if hevc_count >= 2 else "0:v:0"
            try:
                proc = await asyncio.create_subprocess_exec(
                    FFMPEG_BIN, "-y", "-i", mkv_path,
                    "-map", map_arg, "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                    "-t", "30", "-f", "hevc", tmp_hevc,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0 or not Path(tmp_hevc).exists() or Path(tmp_hevc).stat().st_size < 1000:
                    return None

                proc = await asyncio.create_subprocess_exec(
                    DOVI_TOOL_BIN, "extract-rpu", tmp_hevc, "-o", tmp_rpu,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0 or not Path(tmp_rpu).exists() or Path(tmp_rpu).stat().st_size < 10:
                    return None
            finally:
                Path(tmp_hevc).unlink(missing_ok=True)

        # dovi_tool info --summary sobre el RPU (ambas vías dejan aquí tmp_rpu listo)
        proc = await asyncio.create_subprocess_exec(
            DOVI_TOOL_BIN, "info", "--summary", tmp_rpu,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            _logger.warning("dovi_tool info falló: %s", stderr.decode()[:200])
            return None

        dovi = _parse_dovi_summary(stdout.decode("utf-8", errors="replace"))
        # Guardamos el tamaño del RPU para la radiografia (indicador de
        # metadata richness). Nota: con --limit 720 es sample-scoped, no
        # movie-scoped — util igualmente para calcular bytes/frame.
        try:
            dovi.rpu_size_bytes = Path(tmp_rpu).stat().st_size
        except Exception:
            pass
        # Enriquecimiento via `dovi_tool export` a JSON — mucho mas fiable que
        # parsear el texto de info --summary (formato varia entre versiones).
        # De aqui obtenemos L8 target_display_index/nits, L9/L10 primaries y
        # L11 content_type+intended_white+reference_mode de forma estructurada.
        try:
            await _enrich_dovi_from_json_export(dovi, tmp_rpu)
        except Exception as e:
            _logger.info("Enriquecimiento JSON falló (no bloquea): %s", e)
        return dovi
    finally:
        Path(tmp_rpu).unlink(missing_ok=True)


# Índices de target display predefinidos por la spec de Dolby Vision. Sobre
# RPUs reales del repo DoviTools aparecen el 1 y el 28; el resto es el subset
# habitual, para no dejar un índice conocido sin nits.
from phases.rpu_analyze import L8_NITS_POR_INDICE as _L8_NITS_POR_INDICE

# `source_primary_index` de L9 (y `target_primaries_index` de L10).
async def _enrich_dovi_from_json_export(dovi: DoviInfo, rpu_path: str) -> None:
    """Rellena los niveles L8/L9/L11 de DoviInfo leyendo el RPU exportado.

    Delega en `rpu_analyze.export_levels`, que usa `dovi_tool export -f json
    --levels`: devuelve una lista plana de registros por frame, y es el mismo
    parser que ya alimenta la auditoría de calidad (con 37 tests detrás).

    ESTA FUNCIÓN NO RELLENABA NADA. Recorría
    `vdr_dm_data.ext_metadata_blocks`, y en el volcado real de `dovi_tool
    export` los bloques viven un nivel más abajo, separados en
    `cmv29_metadata` y `cmv40_metadata`; además el nivel no es un campo
    (`{"level": 8}`) sino la CLAVE del bloque (`{"Level8": {...}}`). Con las
    dos incompatibilidades, la lista quedaba vacía, el bucle no iteraba y la
    función retornaba sin tocar un solo campo — ni siquiera lanzaba, así que
    el `except: pass` del caller no tenía nada que registrar. Verificado
    ejecutándola sobre un P7 FEL CMv4.0 real de 176.448 frames: 0 campos
    rellenados.

    Formatos verificados contra los RPUs del repo DoviTools:
      level8:  {"frame", "length", "target_display_index", "trim_*"}
      level9:  {"frame", "length", "source_primary_index"}
      level11: {"frame", "content_type", "whitepoint", "reference_mode_flag"}

    L10 existe como opción de `--levels` pero salió vacío en los dos RPUs
    reales probados, así que no se pide: el gamut del target display se
    muestra desde el master display de HDR10.
    """
    from phases.rpu_analyze import export_levels, rellenar_l9_l11

    niveles = await export_levels(
        Path(rpu_path),
        ("level3", "level4", "level8", "level9", "level10", "level11"),
        timeout=900)
    if not niveles:
        _logger.info("export --levels no disponible sobre %s", rpu_path)
        return

    # ── L8: los target displays para los que hay trims ────────────────
    indices = {r["target_display_index"] for r in niveles.get("level8", [])
               if isinstance(r, dict) and r.get("target_display_index") is not None}
    if indices:
        nits = sorted({_L8_NITS_POR_INDICE[i] for i in indices
                       if i in _L8_NITS_POR_INDICE})
        if nits:
            dovi.l8_trim_nits = nits
            dovi.l8_trim_count = len(nits)
        dovi.has_l8 = True

    # ── L9 y L11 ──────────────────────────────────────────────────────
    # El parseo vive en `rpu_analyze`: el pipeline de CMv4.0 lo necesita
    # igual y una segunda copia es lo que dejó su tabla sin estos dos.
    rellenar_l9_l11(niveles, dovi)

    # ── L3, L4 y L10: presencia ───────────────────────────────────────
    # Los tres se «detectaban» con un regex sobre `dovi_tool info --summary`
    # que NO PUEDE casar: ese summary emite exactamente cuatro líneas de
    # niveles —`L5 offsets`, `L2 trims`, `L8 trims`, `L9 MDP`— y ninguna de
    # L3, L4, L10, L11 ni L254. Así que `has_l3` valía False en el 100 % de
    # los casos, y el pill «L3 · local scene trim» de la radiografía nunca
    # se encendió. Medido sobre 21 MKVs CMv4.0 de la biblioteca: **los 21
    # tienen L3**, de 1 a 224 combos en 90 s de muestra.
    #
    for nivel, campo in (("level3", "has_l3"), ("level4", "has_l4"),
                         ("level10", "has_l10")):
        if any(isinstance(r, dict) for r in niveles.get(nivel, [])):
            setattr(dovi, campo, True)

    # ── L254 · el marcador CMv4.0 ─────────────────────────────────────
    # `--levels` llega hasta `level11` y no lo acepta (comprobado contra
    # dovi_tool 2.3.3), así que se daba por no medible: el volcado entero
    # (`-d all`) son 682 MB sobre un UHD.
    #
    # Pero **`info -f N` imprime UN frame**, y ese frame trae los nueve
    # niveles, L254 incluido. Medido sobre un RPU de 114 MB: 3,5 s, y el
    # coste no depende del frame que se pida. Sobre el RPU del sniff, que
    # es lo que hay aquí, son milisegundos.
    await _marcar_l254(dovi, rpu_path)
    # Llegar aquí significa que el export corrió y sus niveles se leyeron:
    # a partir de ahora un `has_lN` en False es una ausencia COMPROBADA.
    dovi.niveles_medidos = True


async def _marcar_l254(dovi: DoviInfo, rpu_path: str) -> None:
    """Pone `has_l254` mirando dos frames sueltos del RPU.

    **Dos y no uno**: con uno, un primer frame atípico haría decir
    «ausente» de un RPU que sí lo lleva. Y no más, porque cada llamada
    reparsea el fichero: dos bastan para lo que este dato es —un
    marcador que acompaña a la metadata CMv4.0— y el alcance que se
    enseña ya dice que es una muestra.

    No bloquea: si `dovi_tool` falla, el flag se queda como estaba.
    """
    frames = [0]
    if dovi.frame_count and dovi.frame_count > 2:
        frames.append(dovi.frame_count // 2)
    for f in frames:
        try:
            proc = await asyncio.create_subprocess_exec(
                DOVI_TOOL_BIN, "info", "-i", rpu_path, "-f", str(f),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        except (OSError, asyncio.TimeoutError) as e:
            _logger.info("info -f %s falló sobre %s: %s", f, rpu_path, e)
            return
        # El nivel es la CLAVE del bloque (`{"Level254": {…}}`), igual que
        # en el volcado de `export`: basta con buscarla en el texto.
        if b'"Level254"' in out:
            dovi.has_l254 = True
            return


def _pq_code_to_nits_local(code_value: float) -> float:
    """PQ inverse EOTF — copia local para no depender de main.py."""
    v = max(0.0, min(1.0, code_value / 4095.0))
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 4096.0 * 128.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 4096.0 * 32.0
    c3 = 2392.0 / 4096.0 * 32.0
    vm2 = v ** (1.0 / m2)
    num = max(0.0, vm2 - c1)
    den = c2 - c3 * vm2
    if den <= 0: return 0.0
    return 10000.0 * (num / den) ** (1.0 / m1)


def _extract_chapters(mkv_path: str) -> list[Chapter]:
    """Extrae capítulos del MKV con mkvextract --simple."""
    try:
        result = subprocess.run(
            [MKVEXTRACT_BIN, mkv_path, "chapters", "--simple"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    chapters = []
    timestamps = {}
    names = {}

    for line in result.stdout.strip().splitlines():
        line = line.strip()
        m = re.match(r"CHAPTER(\d+)=([\d:.]+)", line)
        if m:
            timestamps[int(m.group(1))] = m.group(2)
            continue
        m = re.match(r"CHAPTER(\d+)NAME=(.*)", line)
        if m:
            names[int(m.group(1))] = m.group(2).strip()

    for num in sorted(timestamps.keys()):
        raw_name = names.get(num, "")
        is_generic = bool(re.match(r"^Chapter\s+\d+$", raw_name, re.IGNORECASE))
        if is_generic or not raw_name:
            name = tr('mkv_analyze.capitulo_n', n=f"{num:02d}")
            name_custom = False
        else:
            name = raw_name
            name_custom = True

        chapters.append(Chapter(
            number=num,
            timestamp=timestamps[num],
            name=name,
            name_custom=name_custom,
        ))

    return chapters


# ══════════════════════════════════════════════════════════════════════
#  APLICAR EDICIONES
# ══════════════════════════════════════════════════════════════════════

async def apply_mkv_edits(request: MkvEditRequest) -> dict:
    """
    Aplica ediciones de metadatos a un MKV existente vía mkvpropedit (O(1)).

    Soporta: nombres de pistas, flags default/forced, capítulos.
    No soporta: eliminación ni reorden de pistas (requeriría remux).

    Returns:
        {"ok": True, "new_path": str, "output": str}
    """
    mkv_path = request.file_path
    if not Path(mkv_path).exists():
        raise RuntimeError(tr('mkv_analyze.fichero_no_encontrado', mkv_path=mkv_path))

    output = await _apply_propedit(mkv_path, request)
    return {"ok": True, "new_path": mkv_path, "output": output}


async def _apply_propedit(mkv_path: str, request: MkvEditRequest) -> str:
    """Aplica ediciones de metadatos con mkvpropedit. Devuelve el output."""
    cmd = [MKVPROPEDIT_BIN, mkv_path]

    # Título del contenedor
    if request.title is not None:
        cmd += ["--edit", "info", "--set", f"title={request.title}"]

    # Pistas de audio
    for t in request.audio_tracks:
        cmd += ["--edit", f"track:{t.id + 1}"]  # mkvpropedit usa 1-based
        if t.name is not None:
            cmd += ["--set", f"name={t.name}"]
        if t.flag_default is not None:
            cmd += ["--set", f"flag-default={'1' if t.flag_default else '0'}"]
        if t.flag_forced is not None:
            cmd += ["--set", f"flag-forced={'1' if t.flag_forced else '0'}"]

    # Pistas de subtítulos
    for t in request.subtitle_tracks:
        cmd += ["--edit", f"track:{t.id + 1}"]
        if t.name is not None:
            cmd += ["--set", f"name={t.name}"]
        if t.flag_default is not None:
            cmd += ["--set", f"flag-default={'1' if t.flag_default else '0'}"]
        if t.flag_forced is not None:
            cmd += ["--set", f"flag-forced={'1' if t.flag_forced else '0'}"]

    # Capítulos
    chapters_xml = None
    if request.chapters is not None:
        chapters_xml = _write_chapters_xml(request.chapters)
        cmd += ["--chapters", chapters_xml]

    _logger.info("mkvpropedit: %d argumentos sobre %s", len(cmd), Path(mkv_path).name)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if chapters_xml:
        Path(chapters_xml).unlink(missing_ok=True)

    output = (stdout.decode("utf-8", errors="replace") +
              stderr.decode("utf-8", errors="replace")).strip()

    if proc.returncode >= 2:
        raise RuntimeError(
            tr('mkv_analyze.mkvpropedit_fallo_codigo', returncode=proc.returncode, p2=output[:300])
        )

    return output


def _write_chapters_xml(chapters: list[Chapter]) -> str:
    """Serializa capítulos a XML Matroska temporal."""
    root = ET.Element("Chapters")
    edition = ET.SubElement(root, "EditionEntry")

    for ch in chapters:
        atom = ET.SubElement(edition, "ChapterAtom")
        ET.SubElement(atom, "ChapterTimeStart").text = ch.timestamp
        ET.SubElement(atom, "ChapterFlagHidden").text = "0"
        ET.SubElement(atom, "ChapterFlagEnabled").text = "1"
        display = ET.SubElement(atom, "ChapterDisplay")
        ET.SubElement(display, "ChapterString").text = ch.name
        ET.SubElement(display, "ChapterLanguage").text = "spa"

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    tmp = tempfile.NamedTemporaryFile(
        suffix=".xml", prefix="chapters_", delete=False, mode="wb"
    )
    tree.write(tmp, encoding="utf-8", xml_declaration=True)
    tmp.close()
    return tmp.name
