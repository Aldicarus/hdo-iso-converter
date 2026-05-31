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
import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

from models import Chapter, DoviInfo, HdrMetadata, MkvAnalysisResult, MkvEditRequest, MkvTrackInfo

_logger = logging.getLogger(__name__)

MKVMERGE_BIN    = "mkvmerge"
MKVPROPEDIT_BIN = "mkvpropedit"
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
CACHE_VERSION_BASIC = 1

# Versión del análisis profundo del RPU (L8/L2 combos + classify_l8 +
# classify_l8_quality). Bumpear cuando cambien los umbrales del clasificador
# en rpu_analyze.py o se añadan campos cuantitativos nuevos.
#   v1 (mayo 2026) — versión inicial del quality audit.
CACHE_VERSION_QUALITY = 1


# ══════════════════════════════════════════════════════════════════════
#  ANÁLISIS
# ══════════════════════════════════════════════════════════════════════

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
        raise RuntimeError(f"Fichero no encontrado: {mkv_path}")

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
                # Si hay quality cache válido, inyectarlo en el DoviInfo del
                # resultado para que la card de Auditoría aparezca poblada
                # directamente al abrir el MKV.
                quality_block = cached.get("quality")
                if quality_block and result.dovi:
                    for k, v in quality_block.items():
                        if hasattr(result.dovi, k):
                            setattr(result.dovi, k, v)
                _logger.info("MKV cache HIT para %s", Path(mkv_path).name)
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
        raise RuntimeError(f"mkvmerge -J falló: {stderr.decode()[:300]}")

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
    try:
        from phases.phase_a import run_mediainfo
        mi = await run_mediainfo(mkv_path)
        mediainfo_raw = mi.raw_json

        mi_video = [t for t in mi.tracks if t.track_type == "video"]
        mi_audio = [t for t in mi.tracks if t.track_type == "audio"]
        mi_subs  = [t for t in mi.tracks if t.track_type == "text"]

        # Enriquecer pistas de vídeo
        video_tracks_list = [t for t in tracks if t.type == "video"]
        if video_tracks_list and mi_video:
            mv = mi_video[0]
            video_tracks_list[0].bitrate_kbps = mv.bitrate_kbps
            video_tracks_list[0].bit_depth = mv.bit_depth
            video_tracks_list[0].color_primaries = mv.color_primaries
            hdr_fmt = "HDR10" if mv.transfer_characteristics == "PQ" else ("HLG" if mv.transfer_characteristics == "HLG" else "")
            video_tracks_list[0].hdr_format = hdr_fmt
            if hdr_fmt:
                hdr_meta = HdrMetadata(
                    hdr_format=hdr_fmt,
                    color_primaries=mv.color_primaries,
                    transfer_characteristics=mv.transfer_characteristics,
                    bit_depth=mv.bit_depth,
                )
                # MaxCLL/MaxFALL del raw
                if mi.raw_json:
                    for rt in mi.raw_json.get("media", {}).get("track", []):
                        if rt.get("@type") == "Video" and rt.get("@typeorder", "1") == "1":
                            try:
                                hdr_meta.max_cll = int(rt["MaxCLL"]) if rt.get("MaxCLL") else None
                            except (ValueError, TypeError):
                                pass
                            try:
                                hdr_meta.max_fall = int(rt["MaxFALL"]) if rt.get("MaxFALL") else None
                            except (ValueError, TypeError):
                                pass
                            hdr_meta.mastering_display_luminance = rt.get("MasteringDisplay_Luminance", "")
                            hdr_meta.mastering_display_primaries = rt.get("MasteringDisplay_ColorPrimaries", "")
                            break

        # Enriquecer pistas de audio
        audio_idx = 0
        for t in tracks:
            if t.type == "audio" and audio_idx < len(mi_audio):
                ma = mi_audio[audio_idx]
                t.bitrate_kbps = ma.bitrate_kbps
                t.format_commercial = ma.format_commercial
                t.channel_layout = ma.channel_layout
                t.compression_mode = ma.compression_mode
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
        mediainfo_raw=mediainfo_raw,
    )


def _compute_provenance_hints(
    rpu_analysis,
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
        hints.append("RPU CMv2.9 puro — Blu-ray original sin upgrade a CMv4.0")
        if rpu_analysis.l2_unique_count >= 30:
            hints.append("L2 trabajado por colorista — grading dinámico nativo")
        if has_l4:
            hints.append("L4 presente — compat trim legacy")
        return hints

    # CMv4.0 — combina con classifier
    if classification == "real" and tier in ("full", "core_rich"):
        if has_l11 and has_l254:
            hints.append("Master nativo CMv4.0 reciente — L11 + L254 presentes")
        elif has_l254:
            hints.append("Master CMv4.0 con marker L254 (CMv4.0 bien señalado)")
        if has_l9 and has_l10 and has_l11:
            hints.append("Metadata DV completa — source primaries (L9) + target primaries (L10) + content type (L11)")
        if not has_l11 and (has_l9 or has_l10):
            hints.append("Master CMv4.0 pre-L11 — anterior a Dolby Vision IQ (~2020)")

    elif classification == "real" and tier == "core":
        hints.append("Master CMv4.0 estándar — calidad de release streaming")
        if not has_l11:
            hints.append("Sin L11 — Dolby Vision IQ no aplicable en este master")

    elif classification == "default":
        if has_l4:
            hints.append("Bin convertido — L4 (compat CMv2.9) + L8 sintético sugieren transfer P5/P8→CMv4.0")
        else:
            hints.append("Bin sintético — equivalente a la conversión al vuelo de p3i / avdvplus / appletvplus")
        if not has_l11:
            hints.append("Sin L11 — confirma origen automático (los conversores no añaden content type)")

    elif classification == "indeterminate":
        hints.append("L8 ambiguo — el clasificador no puede decidir con seguridad")
        if has_l11 and has_l254:
            hints.append("Pero L11 + L254 sugieren master nativo, dudoso por densidad de combos")

    return hints


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
    from phases.rpu_analyze import classify_l8, classify_l8_quality

    base = {
        "quality_total_frames_rpu": rpu_analysis.total_frames,
        "quality_frames_with_cmv40": rpu_analysis.frames_with_cmv40,
        "quality_scene_cuts": rpu_analysis.scene_cuts,
        "quality_l2_unique_count": rpu_analysis.l2_unique_count,
        "quality_l2_target_pqs": list(rpu_analysis.l2_target_pqs),
        "quality_l8_unique_count": rpu_analysis.l8_unique_count,
        "quality_l8_neutral_pct": rpu_analysis.l8_neutral_pct,
        "quality_l8_has_mid_contrast": rpu_analysis.l8_has_mid_contrast,
        "quality_l8_has_clip_trim": rpu_analysis.l8_has_clip_trim,
    }

    if is_cmv29_only:
        # RPU CMv2.9 puro: el veredicto es sobre L2 (Tier 1 del modelo).
        # Mismo umbral cualitativo: muchos combos = master nativo; pocos = generado.
        l2_count = rpu_analysis.l2_unique_count
        l2_pqs = len(rpu_analysis.l2_target_pqs)
        if l2_count >= 30 and l2_pqs >= 3:
            verdict = "Master CMv2.9 nativo — grading rico del Blu-ray UHD"
            color = "green"
            reason = (f"L2 trabajado por colorista — {l2_count} combos únicos "
                      f"sobre {l2_pqs} target_pqs. Grading nativo de masterizado "
                      f"UHD BD, no conversión algorítmica.")
            tier_label = "CMv2.9 NATIVO"
        elif l2_count >= 10:
            verdict = "CMv2.9 estándar — trims básicos del master"
            color = "yellow"
            reason = (f"L2 con {l2_count} combos únicos sobre {l2_pqs} target_pqs. "
                      f"Funcional pero no excepcional; típico de release streaming "
                      f"o conversión a CMv2.9.")
            tier_label = "CMv2.9 CORE"
        else:
            verdict = "CMv2.9 mínimo — grading limitado"
            color = "red"
            reason = (f"L2 con solo {l2_count} combos únicos. El RPU aporta poco "
                      f"trabajo de tone-mapping dinámico; un upgrade a CMv4.0 sería "
                      f"un salto sustancial.")
            tier_label = "CMv2.9 MIN"
        classification_cmv29 = "real" if l2_count >= 10 else "default"
        return {
            **base,
            "quality_classification": classification_cmv29,
            "quality_reason": reason,
            "quality_tier": "",  # tiers son solo CMv4.0
            "quality_tier_label": tier_label,
            "quality_tier_description": reason,
            "quality_verdict_text": verdict,
            "quality_verdict_color": color,
            "quality_provenance_hints": _compute_provenance_hints(
                rpu_analysis, classification_cmv29, "", dv_flags or {}, True,
            ),
        }

    # CMv4.0: aplica el classifier de Tab 3 íntegro.
    classification, reason = classify_l8(rpu_analysis)
    tier, tier_label, tier_desc = classify_l8_quality(rpu_analysis)

    if classification == "real" and tier == "full":
        verdict = "Master CMv4.0 FULL — calidad máxima"
        color = "green"
    elif classification == "real" and tier == "core_rich":
        verdict = "Master CMv4.0 CORE+ — grading dinámico shot-a-shot"
        color = "green"
    elif classification == "real" and tier == "core":
        verdict = "Master CMv4.0 CORE — streaming estándar"
        color = "yellow"
    elif classification == "real":
        # "real" sin tier — minimal real
        verdict = "Master CMv4.0 minimal — look global trabajado"
        color = "yellow"
    elif classification == "default":
        verdict = "CMv4.0 sintético — equivale a Auto on-the-fly"
        color = "red"
    else:  # indeterminate
        verdict = "CMv4.0 ambiguo — caso límite"
        color = "gray"

    return {
        **base,
        "quality_classification": classification,
        "quality_reason": reason,
        "quality_tier": tier,
        "quality_tier_label": tier_label,
        "quality_tier_description": tier_desc,
        "quality_verdict_text": verdict,
        "quality_verdict_color": color,
        "quality_provenance_hints": _compute_provenance_hints(
            rpu_analysis, classification, tier, dv_flags or {}, False,
        ),
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


async def analyze_rpu_quality_for_mkv(
    mkv_path: str,
    progress_callback=None,
    cancel_check=None,
    register_proc=None,
    dv_flags: dict | None = None,
    log_callback=None,
) -> dict:
    """Pipeline de auditoría profunda del RPU de un MKV (Tab 2, on-demand).

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
        raise RuntimeError(f"MKV no encontrado: {mkv_path}")

    tmpdir = Path(tempfile.mkdtemp(prefix="mkv_quality_audit_"))
    hevc_path = tmpdir / "video.hevc"
    rpu_path = tmpdir / "rpu.bin"
    mkv_size = p.stat().st_size
    expected_hevc = int(mkv_size * 0.75)

    audit_start = _t.monotonic()
    _log(f"[Audit] 📋 Plan: extraer HEVC del MKV → extraer RPU Dolby Vision → "
         f"agregar combos L8/L2 y clasificar. ~5-10 min en UHD BD (~{_fmt_bytes(mkv_size)}).")
    _log(f"[Audit] Workdir temporal: {tmpdir} · se borrará al terminar")

    try:
        # ── Paso 1: ffmpeg → HEVC annex-B ────────────────────────────
        _check()
        _emit("ffmpeg", 0.0, "Extrayendo HEVC del MKV con ffmpeg…")
        _log("━━━ Paso 1/3 · Extracción HEVC ━━━")
        _log(f"[Audit] 📋 Plan: ffmpeg stream-copy del v:0 del MKV a HEVC annex-B local. "
             f"Tamaño esperado del HEVC: ~{_fmt_bytes(expected_hevc)} (75% del MKV, sin audio/subs).")
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
                        _emit("ffmpeg", global_pct, "Extrayendo HEVC del MKV…")
                        # Loguear progreso cada 10% para no saturar
                        if int(local_pct) >= last_logged_pct + 10:
                            last_logged_pct = int(local_pct // 10) * 10
                            _log(f"[Audit] HEVC: {int(local_pct)}% "
                                 f"({_fmt_bytes(size)} / {_fmt_bytes(expected_hevc)} esperado)")
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
            raise RuntimeError("ffmpeg excedió 40 min extrayendo HEVC")
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
            _log(f"[Audit] ✗ ffmpeg falló: {err}")
            raise RuntimeError(f"ffmpeg falló: {err}")
        hevc_size = hevc_path.stat().st_size
        _emit("ffmpeg", 55.0, "✓ HEVC extraído")
        _log(f"[Audit] ✓ HEVC extraído en {_fmt_elapsed(_t.monotonic() - t_step)} · {_fmt_bytes(hevc_size)}")

        # ── Paso 2: dovi_tool extract-rpu ────────────────────────────
        _check()
        _emit("extract_rpu", 55.0, "Extrayendo RPU Dolby Vision del HEVC…")
        _log("━━━ Paso 2/3 · Extracción RPU Dolby Vision ━━━")
        _log("[Audit] 📋 Plan: dovi_tool extract-rpu lee el HEVC bitstream y "
             "extrae las NALUs DV RPU. CPU-bound, ~1-2 min para UHD.")
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
        try:
            dt_out_bytes, _ = await asyncio.wait_for(dt_proc.communicate(), timeout=1800)
        except asyncio.TimeoutError:
            try: dt_proc.kill()
            except Exception: pass
            raise RuntimeError("dovi_tool extract-rpu excedió 30 min")
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
            _log(f"[Audit] ✗ dovi_tool extract-rpu falló (el MKV no tiene DV o el RPU es inválido): {err}")
            raise RuntimeError(
                f"dovi_tool extract-rpu falló (el MKV no tiene DV o el RPU es inválido): {err}"
            )
        rpu_size = rpu_path.stat().st_size
        _log(f"[Audit] ✓ RPU extraído en {_fmt_elapsed(_t.monotonic() - t_step)} · {_fmt_bytes(rpu_size)}")
        # Liberar HEVC en cuanto tenemos el RPU — son 45 GB que ya no
        # necesitamos. Reduce uso de disco durante el paso 3.
        try:
            hevc_path.unlink(missing_ok=True)
            _log("[Audit] ⏬ HEVC intermedio liberado (no se vuelve a usar)")
        except Exception:
            pass
        _emit("extract_rpu", 80.0, "✓ RPU extraído")

        # ── Paso 3: analyze_rpu_combos (export -d all + parse) ───────
        _check()
        _emit("combos", 80.0, "Exportando metadata y agregando combos L8/L2…")
        _log("━━━ Paso 3/3 · Análisis de combos L8/L2 + clasificación ━━━")
        _log("[Audit] 📋 Plan: dovi_tool export -d all sobre el RPU → JSON grande "
             "(~3-5× el tamaño del RPU) → parsear y agregar combos únicos por frame.")
        _log(f"$ dovi_tool export -i {rpu_path} -d all=<json_temp>")
        t_step = _t.monotonic()
        rpu_analysis = await analyze_rpu_combos(rpu_path)
        _check()
        if rpu_analysis.total_frames == 0:
            _log("[Audit] ✗ dovi_tool export devolvió 0 frames — el RPU no es legible o no hay metadata DV.")
            raise RuntimeError(
                "dovi_tool export devolvió 0 frames — el RPU no es legible o no hay metadata DV."
            )
        cmv40_pct = (rpu_analysis.frames_with_cmv40 * 100 / rpu_analysis.total_frames
                     if rpu_analysis.total_frames > 0 else 0)
        _log(f"[Audit] Frames analizados: {rpu_analysis.total_frames:,} · "
             f"CMv4.0 cobertura: {cmv40_pct:.0f}% · scene cuts: {rpu_analysis.scene_cuts:,}")
        if rpu_analysis.l8_unique_count > 0:
            l8_extras = []
            if rpu_analysis.l8_has_mid_contrast: l8_extras.append("mid_contrast")
            if rpu_analysis.l8_has_clip_trim:    l8_extras.append("clip_trim")
            extras_str = (" · " + " · ".join(l8_extras)) if l8_extras else ""
            _log(f"[Audit] L8: {rpu_analysis.l8_unique_count:,} combos únicos · "
                 f"{rpu_analysis.l8_neutral_pct * 100:.0f}% frames neutros{extras_str}")
        if rpu_analysis.l2_unique_count > 0:
            _log(f"[Audit] L2: {rpu_analysis.l2_unique_count:,} combos únicos · "
                 f"{len(rpu_analysis.l2_target_pqs)} target_pqs ({rpu_analysis.l2_target_pqs})")
        _log(f"[Audit] ✓ Combos agregados en {_fmt_elapsed(_t.monotonic() - t_step)}")
        _emit("combos", 95.0, "✓ Combos agregados")

        # ── Paso 4: classify + verdict ───────────────────────────────
        is_cmv29_only = (rpu_analysis.frames_with_cmv40 == 0
                         and rpu_analysis.l8_unique_count == 0)
        result = _build_quality_audit_from_rpu_analysis(
            rpu_analysis, is_cmv29_only, dv_flags=dv_flags,
        )
        _emit("done", 100.0, "✓ Auditoría completada")
        _log(f"[Audit] 🎯 Resultado: {result.get('quality_verdict_text', '—')}")
        if result.get("quality_tier_label"):
            _log(f"[Audit] Tier: {result['quality_tier_label']}")
        if result.get("quality_reason"):
            _log(f"[Audit] {result['quality_reason']}")
        for hint in (result.get("quality_provenance_hints") or [])[:5]:
            _log(f"[Audit] ├─ {hint}")
        _log(f"✓ Auditoría completada en {_fmt_elapsed(_t.monotonic() - audit_start)}")
        return result

    finally:
        # Cleanup atómico — nunca dejamos basura en /mnt/tmp
        try: hevc_path.unlink(missing_ok=True)
        except Exception: pass
        try: rpu_path.unlink(missing_ok=True)
        except Exception: pass
        try: tmpdir.rmdir()
        except Exception: pass


def persist_mkv_quality_to_cache(mkv_path: str, quality_payload: dict) -> None:
    """Persiste el dict de quality_* en el bloque 'quality' del cache MKV.

    Preserva el bloque 'basic' existente (storage.write_mkv_cache_quality
    lo lee y lo re-escribe). Si el cache no existe todavía (caso edge:
    análisis básico no se hizo por la app), crea el fichero solo con
    quality y los versions queda incompleto — la próxima apertura
    re-analizará basic y mantendrá quality."""
    from storage import compute_mkv_fingerprint, write_mkv_cache_quality
    try:
        fingerprint = compute_mkv_fingerprint(mkv_path)
        if not fingerprint:
            return
        write_mkv_cache_quality(
            fingerprint=fingerprint,
            cache_version_basic_existing=CACHE_VERSION_BASIC,
            cache_version_quality=CACHE_VERSION_QUALITY,
            quality_payload=quality_payload,
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

    pid = os.getpid()
    tmp_rpu = str(Path(TMP_DIR) / f"_mkv_rpu_{pid}.bin")
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
            tmp_hevc = str(Path(TMP_DIR) / f"_mkv_hevc_{pid}.hevc")
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


async def _enrich_dovi_from_json_export(dovi: DoviInfo, rpu_path: str) -> None:
    """Rellena campos granulares de DoviInfo leyendo el JSON export del RPU.

    Formato producido por `dovi_tool export -i rpu.bin -o rpu.json` (v2.3.x):
    lista de RPUs, cada una con `vdr_dm_data.ext_metadata_blocks` (o
    `ext_blocks`) con los niveles L1-L11. Para L8/L9/L10/L11 el JSON tiene
    los campos concretos que texto --summary no siempre expone.
    """
    import tempfile, json as _json
    from pathlib import Path as _Path
    tmpdir = _Path(tempfile.mkdtemp(prefix="dovi_export_"))
    json_path = tmpdir / "rpu.json"
    try:
        proc = await asyncio.create_subprocess_exec(
            DOVI_TOOL_BIN, "export", "-i", rpu_path, "-o", str(json_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not json_path.exists():
            _logger.info("dovi_tool export falló: %s", stderr.decode()[:200])
            return
        with open(json_path) as f:
            data = _json.load(f)
        rpus = data.get("rpus") if isinstance(data, dict) else data
        if not isinstance(rpus, list) or not rpus:
            return

        # Escaneamos TODOS los RPUs (no solo el primero) para capturar L8/L10
        # con target_display_index diferente — cada frame puede tener su bloque.
        l8_indices: set[int] = set()
        l8_trim_data: dict[int, dict] = {}   # {index: {min_nits, max_nits, ...}}
        l10_by_index: dict[int, dict] = {}   # {target_display_index: primaries + lum}
        l9_primaries_idx: int | None = None
        l11_content_type: int | None = None
        l11_intended_white: int | None = None
        l11_reference_mode: int | None = None

        for rpu in rpus:
            vdr = (rpu or {}).get("vdr_dm_data", {}) if isinstance(rpu, dict) else {}
            ext = vdr.get("ext_metadata_blocks") or vdr.get("ext_blocks") or []
            # En algunas versiones estan en un subobject
            if isinstance(ext, dict):
                ext = ext.get("blocks") or ext.get("level1") or []
            if not isinstance(ext, list):
                continue
            for b in ext:
                if not isinstance(b, dict):
                    continue
                lvl = b.get("level") or b.get("block_type") or b.get("ext_block_level")
                try: lvl = int(lvl)
                except Exception: continue

                if lvl == 8:
                    tdi = b.get("target_display_index") or b.get("l8_target_display_index")
                    if tdi is not None:
                        try:
                            tdi = int(tdi)
                            l8_indices.add(tdi)
                            if tdi not in l8_trim_data:
                                l8_trim_data[tdi] = b
                        except Exception:
                            pass
                elif lvl == 9:
                    if l9_primaries_idx is None:
                        idx = b.get("source_primaries_index") or b.get("source_primaries") or b.get("primaries_index")
                        try: l9_primaries_idx = int(idx)
                        except Exception: pass
                elif lvl == 10:
                    tdi = b.get("target_display_index") or b.get("l10_target_display_index")
                    try: tdi = int(tdi) if tdi is not None else None
                    except Exception: tdi = None
                    if tdi is not None and tdi not in l10_by_index:
                        l10_by_index[tdi] = b
                elif lvl == 11:
                    if l11_content_type is None:
                        ct = b.get("content_type") or b.get("l11_content_type")
                        try: l11_content_type = int(ct)
                        except Exception: pass
                    if l11_intended_white is None:
                        iw = b.get("intended_white_point") or b.get("intended_white") or b.get("whitepoint") or b.get("white_point")
                        try: l11_intended_white = int(iw)
                        except Exception: pass
                    if l11_reference_mode is None:
                        rm = b.get("reference_mode_flag")
                        try: l11_reference_mode = int(rm)
                        except Exception: pass

        # ── Construir L8 trim nits desde L10 blocks (cuando referencian target
        # displays) o desde los índices estándar de Dolby ──
        L8_STANDARD_NITS = {
            # Indices predefinidos segun Dolby Vision spec (subset comun)
            0: 100, 1: 350, 2: 600, 3: 1000, 4: 2000, 5: 4000,
            32: 100, 33: 350, 34: 600, 35: 1000, 36: 2000, 37: 4000,
            48: 1000, 49: 600, 50: 350,
            64: 2000, 65: 4000,
        }
        nits_list: list[int] = []
        for idx in sorted(l8_indices):
            nits = None
            # (a) Si hay L10 con mismo target_display_index, lee max_lum
            l10 = l10_by_index.get(idx)
            if l10:
                maxl = (l10.get("target_max_pq") or l10.get("max_display_mastering_luminance")
                        or l10.get("target_max_luminance"))
                if maxl:
                    try:
                        v = float(maxl)
                        # Si viene en PQ code (0-4095), convertir. Si viene en nits directos (>4096), usar.
                        if v > 4096: nits = int(v)
                        elif v > 1:  nits = int(_pq_code_to_nits_local(v))
                        else:        nits = int(_pq_code_to_nits_local(v * 4095))
                    except Exception: pass
            # (b) Fallback a mapping estandar de Dolby
            if nits is None:
                nits = L8_STANDARD_NITS.get(idx)
            if nits is not None:
                nits_list.append(nits)

        if nits_list:
            # Reemplazamos el posible parseo aproximado del summary con datos JSON
            dovi.l8_trim_nits = sorted(set(nits_list))
            dovi.l8_trim_count = len(dovi.l8_trim_nits)
            dovi.has_l8 = True

        # ── L9 source primaries ──
        PRIMARIES_MAP = {
            0: "BT.709", 1: "Reserved", 2: "Reserved", 3: "Reserved",
            4: "BT.470M", 5: "BT.470BG", 6: "BT.601", 7: "SMPTE 240M",
            8: "Generic",
            9: "BT.2020", 10: "SMPTE ST 428",
            11: "DCI-P3", 12: "DCI-P3 D65",
        }
        if l9_primaries_idx is not None:
            dovi.l9_primaries = PRIMARIES_MAP.get(l9_primaries_idx, f"Index {l9_primaries_idx}")
            dovi.has_l9 = True

        # ── L10 target primaries (primer target display con primaries utiles) ──
        if l10_by_index:
            for tdi, blk in l10_by_index.items():
                pidx = (blk.get("target_primaries_index") or blk.get("primaries_index")
                        or blk.get("target_primary_index"))
                if pidx is not None:
                    try:
                        dovi.l10_primaries = PRIMARIES_MAP.get(int(pidx), f"Index {pidx}")
                        dovi.has_l10 = True
                        break
                    except Exception: continue

        # ── L11 content type + intended white + reference mode ──
        CONTENT_TYPE_MAP = {
            0: "Reserved", 1: "Cinema", 2: "Games",
            3: "Sport", 4: "User Generated",
            5: "Movies", 6: "TV",
        }
        INTENDED_WHITE_MAP = {
            0: "D65 Reference",
            1: "D65 Enhanced",
            2: "D93",
        }
        if l11_content_type is not None:
            dovi.l11_content_type = CONTENT_TYPE_MAP.get(l11_content_type, f"Type {l11_content_type}")
            dovi.has_l11 = True
            if l11_intended_white is not None:
                dovi.l11_intended_application = INTENDED_WHITE_MAP.get(
                    l11_intended_white, f"White {l11_intended_white}"
                )
                if l11_reference_mode == 1:
                    dovi.l11_intended_application += " · Reference"

        # L4 presence — check si hay bloques level=4 en el JSON
        for rpu in rpus:
            vdr = (rpu or {}).get("vdr_dm_data", {}) if isinstance(rpu, dict) else {}
            ext = vdr.get("ext_metadata_blocks") or vdr.get("ext_blocks") or []
            if isinstance(ext, list):
                for b in ext:
                    if isinstance(b, dict):
                        lvl = b.get("level") or b.get("block_type")
                        try:
                            if int(lvl) == 4:
                                dovi.has_l4 = True
                                break
                        except Exception:
                            continue
            if dovi.has_l4: break

        # L254 presence: en JSON export puede no aparecer explicitamente; lo
        # detectamos por la presencia de CMv4.0 (v4.0) y L8+L11, que juntos son
        # el sentinel de CMv4.0 bien marcado.
        if (dovi.cm_version and "4.0" in dovi.cm_version
                and dovi.has_l8 and dovi.has_l11):
            dovi.has_l254 = True

    finally:
        try: json_path.unlink(missing_ok=True)
        except Exception: pass
        try: tmpdir.rmdir()
        except Exception: pass


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
            name = f"Capítulo {num:02d}"
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
        raise RuntimeError(f"Fichero no encontrado: {mkv_path}")

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
            f"mkvpropedit falló (código {proc.returncode}): {output[:300]}"
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
