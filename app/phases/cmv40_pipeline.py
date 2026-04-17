"""
cmv40_pipeline.py — Tab 3: Pipeline para inyectar RPU Dolby Vision CMv4.0.

Fases (ver CMv40Phase en models.py):
  A. analyze_source  → ffmpeg extrae HEVC + dovi_tool extract-rpu + info
  B. set_target_rpu  → copia .bin del NAS o extrae de otro MKV
  C. extract          → dovi_tool demux BL/EL + per-frame data
  D. verify_sync      → 100% UI (sin backend)
  E. correct_sync     → dovi_tool editor con JSON remove/duplicate
  F. inject           → dovi_tool inject-rpu
  G. remux            → dovi_tool mux + mkvmerge (preserva audio/subs/capítulos)
  H. validate         → dovi_tool info sobre MKV resultante

Cada fase escribe artefactos en /mnt/tmp/cmv40/{session_id}/ y actualiza
el estado de la sesión. Las fases largas streaman progreso via callback.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from models import CMv40Phase, CMv40PhaseRecord, CMv40Session, DoviInfo
from phases.phase_a import _parse_dovi_summary

_logger = logging.getLogger(__name__)

MKVMERGE_BIN    = "mkvmerge"
FFMPEG_BIN      = "ffmpeg"
FFPROBE_BIN     = "ffprobe"
DOVI_TOOL_BIN   = "dovi_tool"

TMP_DIR         = os.environ.get("TMP_DIR", "/mnt/tmp")
CMV40_WORK_BASE = Path(TMP_DIR) / "cmv40"
CMV40_RPU_DIR   = Path(os.environ.get("CMV40_RPU_DIR", "/mnt/cmv40_rpus"))
OUTPUT_DIR      = Path(os.environ.get("OUTPUT_DIR", "/mnt/output"))


# ══════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════

def get_workdir(session: CMv40Session) -> Path:
    """Devuelve el workdir de artefactos y lo crea si no existe."""
    wd = Path(session.artifacts_dir) if session.artifacts_dir else CMV40_WORK_BASE / session.id
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def artifact_exists(session: CMv40Session, name: str, min_size: int = 100) -> bool:
    """Comprueba si un artefacto existe y tiene tamaño mínimo."""
    p = get_workdir(session) / name
    return p.exists() and p.stat().st_size >= min_size


async def _run(cmd: list[str], log_callback=None, timeout: int | None = None) -> tuple[int, str, str]:
    """Ejecuta un comando y devuelve (returncode, stdout, stderr)."""
    if log_callback:
        await log_callback(f"$ {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Timeout tras {timeout}s: {cmd[0]}")
    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


_FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _hms_to_seconds(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


async def _probe_duration(media_path: str) -> float:
    """Devuelve la duración del fichero en segundos (0.0 si falla)."""
    try:
        rc, out, _ = await _run([
            FFPROBE_BIN, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            media_path,
        ], timeout=15)
        if rc == 0:
            return float(out.strip())
    except Exception:
        pass
    return 0.0


async def _emit_progress(log_callback, pct: float, label: str, eta_s: float | None = None) -> None:
    """Emite un marcador estructurado de progreso que el frontend detecta."""
    if not log_callback:
        return
    pct = max(0.0, min(100.0, pct))
    payload = {"pct": round(pct, 1), "label": label}
    if eta_s is not None and eta_s >= 0:
        payload["eta_s"] = int(eta_s)
    await log_callback(f"§§PROGRESS§§{json.dumps(payload)}")


async def _run_streaming(
    cmd: list[str],
    log_callback=None,
    proc_callback=None,
    progress_ctx: dict | None = None,
) -> int:
    """Ejecuta un comando con streaming de stdout+stderr al log_callback.

    Divide por ``\\n`` y ``\\r`` (ffmpeg usa ``\\r`` en sus líneas de progreso).
    Traduce ``#GUI#progress XX%`` de mkvmerge → ``Progress: XX%``.
    Throttle de 500 ms para ffmpeg para no saturar el log.

    Si se pasa ``progress_ctx``, emite eventos ``§§PROGRESS§§`` con pct y ETA:
        progress_ctx = {
          'duration': float,   # duración conocida a priori (s); si 0 intenta extraerla del header
          'offset': float,     # pct base (0-100)
          'weight': float,     # peso de este paso en la fase (0-100)
          'label': str,        # etiqueta a mostrar
        }
    """
    if log_callback:
        await log_callback(f"$ {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if proc_callback:
        proc_callback(proc)

    buffer = b""
    last_throttle = 0.0
    last_progress_push = 0.0
    duration = float(progress_ctx["duration"]) if progress_ctx else 0.0
    offset   = float(progress_ctx.get("offset", 0.0)) if progress_ctx else 0.0
    weight   = float(progress_ctx.get("weight", 100.0)) if progress_ctx else 100.0
    label    = progress_ctx.get("label", "") if progress_ctx else ""
    step_start = time.monotonic()

    async def _emit(text: str) -> None:
        nonlocal last_throttle, last_progress_push, duration
        if not text:
            return
        if text.startswith("#GUI#progress "):
            text = "Progress: " + text.removeprefix("#GUI#progress ")
        elif text.startswith("#GUI#"):
            return

        # Detectar Duration en el header si aún no la tenemos
        if progress_ctx is not None and duration <= 0:
            m = _FFMPEG_DURATION_RE.search(text)
            if m:
                duration = _hms_to_seconds(*m.groups())

        is_ffmpeg_progress = text.startswith("frame=") and ("fps=" in text or "time=" in text)
        if is_ffmpeg_progress:
            now = time.monotonic()
            # Parse time= para progreso si tenemos duration
            if progress_ctx is not None and duration > 0 and (now - last_progress_push) >= 1.0:
                tm = _FFMPEG_TIME_RE.search(text)
                if tm:
                    elapsed_media = _hms_to_seconds(*tm.groups())
                    step_pct = max(0.0, min(100.0, (elapsed_media / duration) * 100.0))
                    phase_pct = offset + step_pct * weight / 100.0
                    wall = now - step_start
                    eta = (wall / elapsed_media) * (duration - elapsed_media) if elapsed_media > 0 else None
                    await _emit_progress(log_callback, phase_pct, label, eta)
                    last_progress_push = now
            # Throttle de emisión al log
            if now - last_throttle < 0.5:
                return
            last_throttle = now

        if log_callback:
            await log_callback(text)

    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            break
        buffer += chunk
        while True:
            nl = buffer.find(b"\n")
            cr = buffer.find(b"\r")
            if nl == -1 and cr == -1:
                break
            if nl == -1:
                idx = cr
            elif cr == -1:
                idx = nl
            else:
                idx = min(nl, cr)
            line = buffer[:idx].decode("utf-8", errors="replace").rstrip()
            buffer = buffer[idx + 1:]
            if line:
                await _emit(line)

    if buffer:
        line = buffer.decode("utf-8", errors="replace").rstrip()
        if line:
            await _emit(line)

    await proc.wait()
    return proc.returncode


async def _run_with_time_estimate(
    cmd: list[str],
    estimated_s: float,
    log_callback=None,
    proc_callback=None,
    timeout: int | None = None,
    label: str = "",
    offset: float = 0.0,
    weight: float = 100.0,
) -> tuple[int, str, str]:
    """Ejecuta un comando silencioso mientras emite progreso estimado cada 2 s.

    Usado para ``dovi_tool extract-rpu`` y similares que no producen salida
    de progreso cuando stdout está conectado a un pipe. El cálculo se basa
    en ``elapsed / estimated_s``, cap al 95 % hasta que termine.
    """
    stop = asyncio.Event()
    start = time.monotonic()

    async def _tick():
        # Emite al inicio y luego cada 2 s
        while not stop.is_set():
            elapsed = time.monotonic() - start
            est = max(estimated_s, 5.0)
            step_pct = min(95.0, (elapsed / est) * 100.0)
            phase_pct = offset + step_pct * weight / 100.0
            eta = max(0.0, est - elapsed)
            await _emit_progress(log_callback, phase_pct, label, eta)
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    tick_task = asyncio.create_task(_tick())
    try:
        if log_callback:
            await log_callback(f"$ {' '.join(cmd)}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if proc_callback:
            proc_callback(proc)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"Timeout tras {timeout}s: {cmd[0]}")
        return (
            proc.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    finally:
        stop.set()
        try:
            await tick_task
        except Exception:
            pass


async def _count_hevc_frames(hevc_path: str) -> int:
    """Cuenta frames de un HEVC stream usando ffprobe (rápido)."""
    rc, out, err = await _run([
        FFPROBE_BIN, "-v", "error", "-count_packets",
        "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_packets",
        "-of", "csv=p=0",
        hevc_path,
    ], timeout=120)
    if rc != 0:
        raise RuntimeError(f"ffprobe falló: {err[:200]}")
    try:
        return int(out.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe output inválido: {out!r}")


# ══════════════════════════════════════════════════════════════════════
#  FASE A — Analizar MKV origen
# ══════════════════════════════════════════════════════════════════════

async def run_phase_a_analyze_source(
    session: CMv40Session,
    log_callback=None,
    proc_callback=None,
) -> None:
    """
    Extrae el HEVC del MKV origen, extrae su RPU y lo analiza con dovi_tool.

    Artefactos generados:
      - source.hevc
      - RPU_source.bin
      - plot_source.png (opcional, fallos silenciosos)

    Actualiza session.source_dv_info, source_frame_count.
    """
    wd = get_workdir(session)
    source_hevc = wd / "source.hevc"
    rpu_source  = wd / "RPU_source.bin"
    plot_source = wd / "plot_source.png"

    # Pesos estimados: ffmpeg 50% · extract-rpu 45% · info+plot 5%
    W_FFMPEG, W_RPU, W_INFO = 50.0, 45.0, 5.0

    # Pre-probe duración para estimar progreso
    duration = await _probe_duration(session.source_mkv_path)

    # Paso 1: Extraer HEVC del MKV origen
    await _emit_progress(log_callback, 0, "Extrayendo HEVC del MKV origen")
    if log_callback:
        await log_callback("[Fase A] Extrayendo stream HEVC del MKV origen (ffmpeg)…")
    ffmpeg_elapsed = 0.0
    if not source_hevc.exists() or source_hevc.stat().st_size < 1_000_000:
        t0 = time.monotonic()
        rc = await _run_streaming([
            FFMPEG_BIN, "-y", "-i", session.source_mkv_path,
            "-map", "0:v:0", "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-f", "hevc", str(source_hevc),
        ], log_callback=log_callback, proc_callback=proc_callback,
           progress_ctx={
               "duration": duration, "offset": 0.0, "weight": W_FFMPEG,
               "label": "Extrayendo HEVC del MKV origen",
           })
        ffmpeg_elapsed = time.monotonic() - t0
        if rc != 0:
            raise RuntimeError(f"ffmpeg falló al extraer HEVC (código {rc})")
    else:
        if log_callback:
            await log_callback("[Fase A] source.hevc ya existe, reutilizando")
    await _emit_progress(log_callback, W_FFMPEG, "HEVC extraído")

    # Paso 2: Extraer RPU (silencioso con pipe → progreso estimado por tiempo)
    if log_callback:
        await log_callback("[Fase A] Extrayendo RPU del HEVC (dovi_tool extract-rpu)…")
    # Estimado: ~mismo tiempo que ffmpeg si se midió; si no, 2 min por defecto
    est_rpu = ffmpeg_elapsed if ffmpeg_elapsed > 5 else 120.0
    rc, out, err = await _run_with_time_estimate([
        DOVI_TOOL_BIN, "extract-rpu", str(source_hevc), "-o", str(rpu_source),
    ], estimated_s=est_rpu, log_callback=log_callback, proc_callback=proc_callback,
       timeout=600, label="Extrayendo RPU del HEVC",
       offset=W_FFMPEG, weight=W_RPU)
    if rc != 0:
        raise RuntimeError(f"dovi_tool extract-rpu falló: {err[:300]}")
    await _emit_progress(log_callback, W_FFMPEG + W_RPU, "RPU extraído")

    # Paso 3: Info del RPU
    if log_callback:
        await log_callback("[Fase A] Analizando RPU (dovi_tool info)…")
    rc, summary, err = await _run([
        DOVI_TOOL_BIN, "info", "--summary", str(rpu_source),
    ], timeout=30)
    if rc != 0:
        raise RuntimeError(f"dovi_tool info falló: {err[:300]}")

    dovi_info = _parse_dovi_summary(summary)
    session.source_dv_info = dovi_info
    session.source_frame_count = dovi_info.frame_count

    # Paso 4: Plot (opcional, no bloquea)
    try:
        await _run([
            DOVI_TOOL_BIN, "plot", str(rpu_source),
            "-t", "RPU origen (CMv2.9)",
            "-o", str(plot_source),
        ], timeout=60)
    except Exception as e:
        _logger.warning("dovi_tool plot falló (no bloquea): %s", e)

    await _emit_progress(log_callback, 100, "Análisis completado")
    if log_callback:
        await log_callback(
            f"[Fase A] OK — Profile {dovi_info.profile} ({dovi_info.el_type}), "
            f"CM {dovi_info.cm_version}, {dovi_info.frame_count} frames"
        )


# ══════════════════════════════════════════════════════════════════════
#  FASE B — Proporcionar RPU target
# ══════════════════════════════════════════════════════════════════════

def list_available_rpus() -> list[dict]:
    """Lista los .bin disponibles en /mnt/cmv40_rpus/."""
    if not CMV40_RPU_DIR.exists():
        return []
    result = []
    for p in sorted(CMV40_RPU_DIR.glob("**/*.bin")):
        size = p.stat().st_size
        rel = p.relative_to(CMV40_RPU_DIR)
        result.append({
            "name": str(rel),
            "path": str(p),
            "size_bytes": size,
        })
    return result


async def run_phase_b_target_from_path(
    session: CMv40Session,
    rpu_path: str,
    log_callback=None,
) -> None:
    """Copia un .bin desde /mnt/cmv40_rpus/ al workdir y lo analiza."""
    wd = get_workdir(session)
    rpu_target = wd / "RPU_target.bin"

    src = Path(rpu_path)
    if not src.exists():
        raise RuntimeError(f"RPU no encontrado: {rpu_path}")
    if not src.is_file() or src.suffix != ".bin":
        raise RuntimeError(f"Fichero no es un .bin válido: {rpu_path}")

    await _emit_progress(log_callback, 0, f"Copiando RPU target: {src.name}")
    if log_callback:
        await log_callback(f"[Fase B] Copiando RPU target: {src.name}")
    shutil.copy2(src, rpu_target)
    await _emit_progress(log_callback, 70, "Analizando RPU")

    session.target_rpu_source = "path"
    session.target_rpu_path = str(src)
    await _analyze_target_rpu(session, rpu_target, log_callback)
    await _emit_progress(log_callback, 100, "Completado")


async def run_phase_b_target_from_mkv(
    session: CMv40Session,
    source_mkv_path: str,
    log_callback=None,
    proc_callback=None,
) -> None:
    """Extrae el RPU de otro MKV que ya tenga CMv4.0."""
    wd = get_workdir(session)
    rpu_target = wd / "RPU_target.bin"
    temp_hevc  = wd / "_target_source.hevc"

    if not Path(source_mkv_path).exists():
        raise RuntimeError(f"MKV no encontrado: {source_mkv_path}")

    # Pesos: ffmpeg 50% · extract-rpu 45% · info 5%
    W_FFMPEG, W_RPU = 50.0, 45.0
    duration = await _probe_duration(source_mkv_path)

    try:
        await _emit_progress(log_callback, 0, "Extrayendo HEVC del MKV target")
        if log_callback:
            await log_callback(f"[Fase B] Extrayendo HEVC del MKV target: {Path(source_mkv_path).name}")
        t0 = time.monotonic()
        rc = await _run_streaming([
            FFMPEG_BIN, "-y", "-i", source_mkv_path,
            "-map", "0:v:0", "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-f", "hevc", str(temp_hevc),
        ], log_callback=log_callback, proc_callback=proc_callback,
           progress_ctx={
               "duration": duration, "offset": 0.0, "weight": W_FFMPEG,
               "label": "Extrayendo HEVC del MKV target",
           })
        ffmpeg_elapsed = time.monotonic() - t0
        if rc != 0:
            raise RuntimeError(f"ffmpeg falló (código {rc})")
        await _emit_progress(log_callback, W_FFMPEG, "HEVC extraído")

        if log_callback:
            await log_callback("[Fase B] Extrayendo RPU del HEVC target…")
        est_rpu = ffmpeg_elapsed if ffmpeg_elapsed > 5 else 120.0
        rc, out, err = await _run_with_time_estimate([
            DOVI_TOOL_BIN, "extract-rpu", str(temp_hevc), "-o", str(rpu_target),
        ], estimated_s=est_rpu, log_callback=log_callback, proc_callback=proc_callback,
           timeout=600, label="Extrayendo RPU del HEVC target",
           offset=W_FFMPEG, weight=W_RPU)
        if rc != 0:
            raise RuntimeError(f"dovi_tool extract-rpu falló: {err[:300]}")
        await _emit_progress(log_callback, W_FFMPEG + W_RPU, "RPU extraído")

        session.target_rpu_source = "mkv"
        session.target_rpu_path = source_mkv_path
        await _analyze_target_rpu(session, rpu_target, log_callback)
        await _emit_progress(log_callback, 100, "Análisis completado")
    finally:
        temp_hevc.unlink(missing_ok=True)


async def _analyze_target_rpu(
    session: CMv40Session,
    rpu_path: Path,
    log_callback=None,
) -> None:
    """Ejecuta dovi_tool info sobre el RPU target y persiste en la sesión."""
    rc, summary, err = await _run([
        DOVI_TOOL_BIN, "info", "--summary", str(rpu_path),
    ], timeout=30)
    if rc != 0:
        raise RuntimeError(f"dovi_tool info falló sobre RPU target: {err[:300]}")

    dovi_info = _parse_dovi_summary(summary)
    session.target_dv_info = dovi_info
    session.target_frame_count = dovi_info.frame_count
    session.sync_delta = dovi_info.frame_count - session.source_frame_count

    if log_callback:
        await log_callback(
            f"[Fase B] RPU target: Profile {dovi_info.profile} ({dovi_info.el_type}), "
            f"CM {dovi_info.cm_version}, {dovi_info.frame_count} frames "
            f"(Δ = {session.sync_delta:+d} vs source)"
        )


# ══════════════════════════════════════════════════════════════════════
#  FASE C — Demux BL/EL + per-frame data
# ══════════════════════════════════════════════════════════════════════

async def run_phase_c_extract(
    session: CMv40Session,
    log_callback=None,
    proc_callback=None,
) -> None:
    """
    Separa BL y EL del source.hevc, genera per_frame_data.json para el chart.

    Artefactos:
      - BL.hevc, EL.hevc (dovi_tool demux)
      - per_frame_data.json (datos para el chart de sincronización)
    """
    wd = get_workdir(session)
    source_hevc = wd / "source.hevc"
    bl_hevc     = wd / "BL.hevc"
    el_hevc     = wd / "EL.hevc"
    rpu_source  = wd / "RPU_source.bin"
    rpu_target  = wd / "RPU_target.bin"
    per_frame   = wd / "per_frame_data.json"

    if not source_hevc.exists():
        raise RuntimeError("source.hevc no existe — ejecuta Fase A primero")
    if not rpu_target.exists():
        raise RuntimeError("RPU_target.bin no existe — ejecuta Fase B primero")

    # Pesos: demux 70% · per-frame 30%
    W_DEMUX, W_PFD = 70.0, 30.0

    # Estimar duración del demux por tamaño del HEVC — similar a extract-rpu
    # Usamos una heurística: ~1GB/min típico en NAS. Mínimo 60s, máximo 600s.
    try:
        hevc_gb = source_hevc.stat().st_size / 1e9
        est_demux = max(60.0, min(600.0, hevc_gb * 60.0))
    except Exception:
        est_demux = 180.0

    # Paso 1: Demux BL + EL (silencioso → progreso estimado)
    await _emit_progress(log_callback, 0, "Separando BL + EL")
    if log_callback:
        await log_callback("[Fase C] Separando BL + EL (dovi_tool demux)…")
    if not (bl_hevc.exists() and el_hevc.exists()):
        rc, out, err = await _run_with_time_estimate([
            DOVI_TOOL_BIN, "demux", str(source_hevc),
            "--bl-out", str(bl_hevc),
            "--el-out", str(el_hevc),
        ], estimated_s=est_demux, log_callback=log_callback, proc_callback=proc_callback,
           timeout=900, label="Separando BL + EL (dovi_tool demux)",
           offset=0.0, weight=W_DEMUX)
        if rc != 0:
            raise RuntimeError(f"dovi_tool demux falló (código {rc}): {err[:300]}")
    else:
        if log_callback:
            await log_callback("[Fase C] BL.hevc y EL.hevc ya existen, reutilizando")
    await _emit_progress(log_callback, W_DEMUX, "BL + EL generados")

    # Paso 2: Generar per_frame_data.json
    if log_callback:
        await log_callback("[Fase C] Generando datos per-frame para el chart…")
    await _generate_per_frame_data(
        session, rpu_source, rpu_target, per_frame, log_callback,
        progress_offset=W_DEMUX, progress_weight=W_PFD,
    )
    await _emit_progress(log_callback, 100, "Completado")


async def _generate_per_frame_data(
    session: CMv40Session,
    rpu_source: Path,
    rpu_target: Path,
    output: Path,
    log_callback=None,
    progress_offset: float = 0.0,
    progress_weight: float = 100.0,
) -> None:
    """
    Genera per_frame_data.json con MaxCLL/MaxFALL de cada frame de ambos RPUs.

    Formato:
      {
        "source_frames": N,
        "target_frames": M,
        "data": [
          {"frame": 0, "src_maxcll": 123, "src_maxfall": 45, "tgt_maxcll": 120, "tgt_maxfall": 42},
          ...
        ]
      }
    """
    # Split del peso: 45% source · 45% target · 10% merge/write
    half = progress_weight * 0.45
    src_data = await _export_rpu_frames(rpu_source, log_callback, label="source",
                                        progress_offset=progress_offset, progress_weight=half)
    await _emit_progress(log_callback, progress_offset + half, "Exportando frames target")
    tgt_data = await _export_rpu_frames(rpu_target, log_callback, label="target",
                                        progress_offset=progress_offset + half, progress_weight=half)
    await _emit_progress(log_callback, progress_offset + half * 2, "Combinando datos per-frame")

    max_len = max(len(src_data), len(tgt_data))
    merged = []
    for i in range(max_len):
        entry = {"frame": i}
        if i < len(src_data):
            entry["src_maxcll"] = src_data[i].get("maxcll", 0)
            entry["src_maxfall"] = src_data[i].get("maxfall", 0)
        if i < len(tgt_data):
            entry["tgt_maxcll"] = tgt_data[i].get("maxcll", 0)
            entry["tgt_maxfall"] = tgt_data[i].get("maxfall", 0)
        merged.append(entry)

    output.write_text(json.dumps({
        "source_frames": len(src_data),
        "target_frames": len(tgt_data),
        "data": merged,
    }), encoding="utf-8")

    if log_callback:
        await log_callback(f"[Fase C] per_frame_data.json: {len(merged)} frames")


async def _export_rpu_frames(
    rpu_path: Path,
    log_callback=None,
    label: str = "",
    progress_offset: float = 0.0,
    progress_weight: float = 0.0,
) -> list[dict]:
    """
    Exporta datos por frame de un RPU usando `dovi_tool export`.

    Intenta primero export JSON; si no está disponible, hace muestreo cada N frames.
    """
    # Intento 1: dovi_tool export (versión reciente) — ~15-30s, estimamos 30s
    try:
        wd = rpu_path.parent
        export_json = wd / f"_export_{label}.json"
        if progress_weight > 0:
            rc, out, err = await _run_with_time_estimate([
                DOVI_TOOL_BIN, "export", "-i", str(rpu_path),
                "-d", f"all={export_json}",
            ], estimated_s=30.0, log_callback=log_callback, timeout=300,
               label=f"Exportando frames {label}",
               offset=progress_offset, weight=progress_weight)
        else:
            rc, out, err = await _run([
                DOVI_TOOL_BIN, "export", "-i", str(rpu_path),
                "-d", f"all={export_json}",
            ], timeout=300)
        if rc == 0 and export_json.exists():
            data = json.loads(export_json.read_text(encoding="utf-8"))
            export_json.unlink(missing_ok=True)
            return _normalize_export_data(data)
    except Exception as e:
        _logger.info("dovi_tool export no disponible: %s — usando muestreo", e)

    # Intento 2: muestreo cada N frames (más lento pero compatible)
    if log_callback:
        await log_callback(f"[Fase C] Muestreando frames de {label} (puede tardar)…")

    rc, summary, err = await _run([DOVI_TOOL_BIN, "info", "--summary", str(rpu_path)], timeout=30)
    frames = 0
    for line in summary.splitlines():
        if "Frames:" in line:
            try:
                frames = int(line.split("Frames:")[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
            break

    step = max(1, frames // 5000) if frames > 5000 else 1
    total_iter = max(1, frames // step)
    data = []
    last_pct_emit = 0.0
    for idx, i in enumerate(range(0, frames, step)):
        try:
            rc, out, err = await _run([
                DOVI_TOOL_BIN, "info", "-i", str(rpu_path), "--frame", str(i),
            ], timeout=10)
            if rc == 0:
                info = _parse_frame_info(out)
                info["frame"] = i
                data.append(info)
        except Exception:
            continue
        # Emitir progreso cada ~2% del paso
        if progress_weight > 0:
            step_pct = ((idx + 1) / total_iter) * 100.0
            phase_pct = progress_offset + step_pct * progress_weight / 100.0
            if phase_pct - last_pct_emit >= 1.0:
                await _emit_progress(log_callback, phase_pct, f"Muestreando frames {label}")
                last_pct_emit = phase_pct
    return data


def _extract_l1_from_frame(frame: dict) -> dict | None:
    """Devuelve {'min_pq', 'max_pq', 'avg_pq'} o None.

    dovi_tool 2.x export -d all vuelca el RPU completo por frame. L1 vive en:
      frame.vdr_dm_data.cmv29_metadata.ext_metadata_blocks[].Level1   (CMv2.9)
      frame.vdr_dm_data.cmv40_metadata.ext_metadata_blocks[].Level1   (CMv4.0)
    """
    if not isinstance(frame, dict):
        return None
    vdr = frame.get("vdr_dm_data")
    if not isinstance(vdr, dict):
        return None
    for key in ("cmv29_metadata", "cmv40_metadata"):
        meta = vdr.get(key)
        if not isinstance(meta, dict):
            continue
        blocks = meta.get("ext_metadata_blocks") or []
        for block in blocks:
            if isinstance(block, dict) and "Level1" in block:
                l1 = block["Level1"]
                if isinstance(l1, dict) and "max_pq" in l1:
                    return l1
    return None


def _normalize_export_data(raw: dict | list) -> list[dict]:
    """Normaliza dovi_tool export (-d all) a lista de {frame, maxcll, maxfall}.

    Como `maxcll` usamos ``max_pq`` (código PQ 0-4095) y como `maxfall` usamos
    ``avg_pq``. La correlación de Pearson mide forma, así que el cambio de
    escala respecto a nits no afecta al cálculo de confianza ni al chart.
    """
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("frames") or raw.get("data") or []
    else:
        return []

    result = []
    for i, it in enumerate(items):
        l1 = _extract_l1_from_frame(it) or {}
        result.append({
            "frame": i,
            "maxcll": float(l1.get("max_pq") or 0),
            "maxfall": float(l1.get("avg_pq") or 0),
        })
    return result


def _parse_frame_info(output: str) -> dict:
    """Parsea el output de `dovi_tool info --frame N` para extraer MaxCLL/MaxFALL."""
    import re
    data = {"maxcll": 0.0, "maxfall": 0.0}
    m = re.search(r"MaxCLL:\s*([\d.]+)", output)
    if m:
        try:
            data["maxcll"] = float(m.group(1))
        except ValueError:
            pass
    m = re.search(r"MaxFALL:\s*([\d.]+)", output)
    if m:
        try:
            data["maxfall"] = float(m.group(1))
        except ValueError:
            pass
    return data


# ══════════════════════════════════════════════════════════════════════
#  FASE E — Aplicar corrección de sincronización
# ══════════════════════════════════════════════════════════════════════

async def run_phase_e_correct_sync(
    session: CMv40Session,
    editor_config: dict,
    log_callback=None,
) -> None:
    """
    Aplica corrección al RPU target usando dovi_tool editor.

    editor_config es un dict con claves `remove` y/o `duplicate`.
    """
    wd = get_workdir(session)
    rpu_target = wd / "RPU_target.bin"
    rpu_synced = wd / "RPU_synced.bin"
    config_json = wd / "editor_config.json"

    if not rpu_target.exists():
        raise RuntimeError("RPU_target.bin no existe")

    config_json.write_text(json.dumps(editor_config, indent=2), encoding="utf-8")
    if log_callback:
        await log_callback(f"[Fase E] Aplicando editor config: {json.dumps(editor_config)}")

    rc, out, err = await _run([
        DOVI_TOOL_BIN, "editor",
        "-i", str(rpu_target),
        "-j", str(config_json),
        "-o", str(rpu_synced),
    ], log_callback=log_callback, timeout=120)
    if rc != 0:
        raise RuntimeError(f"dovi_tool editor falló: {err[:300]}")

    # Actualizar frame count del RPU corregido
    rc, summary, err = await _run([DOVI_TOOL_BIN, "info", "--summary", str(rpu_synced)], timeout=30)
    if rc == 0:
        dovi_info = _parse_dovi_summary(summary)
        session.target_frame_count = dovi_info.frame_count
        session.sync_delta = dovi_info.frame_count - session.source_frame_count

    session.sync_config = editor_config
    if log_callback:
        await log_callback(
            f"[Fase E] RPU corregido: {session.target_frame_count} frames "
            f"(Δ = {session.sync_delta:+d})"
        )


# ══════════════════════════════════════════════════════════════════════
#  FASE F — Inyectar RPU en EL
# ══════════════════════════════════════════════════════════════════════

async def run_phase_f_inject(
    session: CMv40Session,
    log_callback=None,
    proc_callback=None,
) -> None:
    """Inyecta el RPU final (synced o target) en el EL.hevc."""
    wd = get_workdir(session)
    el_hevc      = wd / "EL.hevc"
    rpu_synced   = wd / "RPU_synced.bin"
    rpu_target   = wd / "RPU_target.bin"
    el_injected  = wd / "EL_injected.hevc"

    # Elegir el RPU correcto: synced si existe, si no target
    rpu_to_inject = rpu_synced if rpu_synced.exists() else rpu_target
    if not rpu_to_inject.exists():
        raise RuntimeError("No hay RPU disponible para inyectar")
    if not el_hevc.exists():
        raise RuntimeError("EL.hevc no existe — ejecuta Fase C primero")

    # Validación de frame count antes de inyectar
    rc, summary, err = await _run([DOVI_TOOL_BIN, "info", "--summary", str(rpu_to_inject)], timeout=30)
    rpu_frames = _parse_dovi_summary(summary).frame_count
    if rpu_frames != session.source_frame_count:
        raise RuntimeError(
            f"Frame count mismatch: RPU tiene {rpu_frames} frames, "
            f"vídeo tiene {session.source_frame_count}. Corrige la sincronización (Fase D/E)."
        )

    if log_callback:
        await log_callback(
            f"[Fase F] Inyectando RPU en EL "
            f"(RPU: {rpu_to_inject.name}, {rpu_frames} frames)…"
        )
    rc = await _run_streaming([
        DOVI_TOOL_BIN, "inject-rpu",
        "-i", str(el_hevc),
        "--rpu-in", str(rpu_to_inject),
        "-o", str(el_injected),
    ], log_callback=log_callback, proc_callback=proc_callback)
    if rc != 0:
        raise RuntimeError(f"dovi_tool inject-rpu falló (código {rc})")

    if log_callback:
        await log_callback(f"[Fase F] EL_injected.hevc generado")


# ══════════════════════════════════════════════════════════════════════
#  FASE G — Remux final (dovi_tool mux + mkvmerge)
# ══════════════════════════════════════════════════════════════════════

async def run_phase_g_remux(
    session: CMv40Session,
    log_callback=None,
    proc_callback=None,
) -> str:
    """
    Combina BL + EL_injected en un stream dual-layer y remuxa con
    audio/subs/capítulos del MKV origen.

    Devuelve la ruta del MKV final (en el workdir; se mueve a /mnt/output en validación).
    """
    wd = get_workdir(session)
    bl_hevc      = wd / "BL.hevc"
    el_injected  = wd / "EL_injected.hevc"
    dv_dual      = wd / "DV_dual.hevc"
    output_mkv   = wd / "output.mkv"

    if not bl_hevc.exists() or not el_injected.exists():
        raise RuntimeError("BL.hevc o EL_injected.hevc no existen")

    # Paso 1: dovi_tool mux → dual-layer HEVC
    if log_callback:
        await log_callback("[Fase G] Combinando BL + EL_injected (dovi_tool mux)…")
    rc = await _run_streaming([
        DOVI_TOOL_BIN, "mux",
        "--bl", str(bl_hevc),
        "--el", str(el_injected),
        "-o", str(dv_dual),
    ], log_callback=log_callback, proc_callback=proc_callback)
    if rc != 0:
        raise RuntimeError(f"dovi_tool mux falló (código {rc})")

    # Paso 2: mkvmerge → MKV final con audio/subs/capítulos del origen
    if log_callback:
        await log_callback("[Fase G] Remuxando a MKV final (mkvmerge)…")
    title = session.output_mkv_name.removesuffix(".mkv")
    rc = await _run_streaming([
        MKVMERGE_BIN, "--gui-mode", "-o", str(output_mkv),
        "--title", title,
        str(dv_dual),
        "--no-video", session.source_mkv_path,  # audio/subs/capítulos del MKV origen
    ], log_callback=log_callback, proc_callback=proc_callback)
    if rc not in (0, 1):  # 1 = warning
        raise RuntimeError(f"mkvmerge falló (código {rc})")

    # Cleanup intermedio
    dv_dual.unlink(missing_ok=True)

    if log_callback:
        size_gb = output_mkv.stat().st_size / 1e9
        await log_callback(f"[Fase G] output.mkv generado ({size_gb:.2f} GB)")
    return str(output_mkv)


# ══════════════════════════════════════════════════════════════════════
#  FASE H — Validación final
# ══════════════════════════════════════════════════════════════════════

async def run_phase_h_validate(
    session: CMv40Session,
    log_callback=None,
) -> dict:
    """
    Valida que el MKV resultante tiene DV CMv4.0 correctamente.

    Si OK, mueve el MKV a /mnt/output/. Devuelve info de validación.
    """
    wd = get_workdir(session)
    output_mkv = wd / "output.mkv"

    if not output_mkv.exists():
        raise RuntimeError("output.mkv no existe — ejecuta Fase G primero")

    # Validar DV con dovi_tool
    if log_callback:
        await log_callback("[Fase H] Validando DV del MKV resultante…")

    # Extraer RPU del MKV y comprobar CM version
    temp_hevc = wd / "_validate.hevc"
    temp_rpu  = wd / "_validate_rpu.bin"
    try:
        await _run([
            FFMPEG_BIN, "-y", "-i", str(output_mkv),
            "-map", "0:v:0", "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-t", "30", "-f", "hevc", str(temp_hevc),
        ], timeout=120)
        await _run([DOVI_TOOL_BIN, "extract-rpu", str(temp_hevc), "-o", str(temp_rpu)], timeout=60)
        rc, summary, err = await _run([DOVI_TOOL_BIN, "info", "--summary", str(temp_rpu)], timeout=30)
        if rc != 0:
            raise RuntimeError(f"Validación DV falló: {err[:200]}")
        result_info = _parse_dovi_summary(summary)
    finally:
        temp_hevc.unlink(missing_ok=True)
        temp_rpu.unlink(missing_ok=True)

    if result_info.cm_version != "v4.0":
        raise RuntimeError(
            f"El MKV resultante tiene CM {result_info.cm_version} (esperado v4.0)"
        )

    # Validar pistas con mkvmerge -J
    rc, out, err = await _run([MKVMERGE_BIN, "-J", str(output_mkv)], timeout=60)
    if rc not in (0, 1):
        raise RuntimeError(f"mkvmerge -J falló sobre MKV final: {err[:200]}")

    # Mover a /mnt/output
    final_path = OUTPUT_DIR / session.output_mkv_name
    if final_path.exists():
        raise RuntimeError(f"Ya existe un MKV con ese nombre: {session.output_mkv_name}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(output_mkv), str(final_path))
    session.output_mkv_path = str(final_path)

    if log_callback:
        await log_callback(
            f"[Fase H] OK — MKV movido a {final_path} "
            f"(Profile {result_info.profile} {result_info.el_type}, CM {result_info.cm_version})"
        )

    return {
        "profile": result_info.profile,
        "el_type": result_info.el_type,
        "cm_version": result_info.cm_version,
        "frame_count": result_info.frame_count,
        "output_path": str(final_path),
    }


# ══════════════════════════════════════════════════════════════════════
#  Auto-detección de offset de sincronización (para Fase D)
# ══════════════════════════════════════════════════════════════════════

def compute_sync_confidence(per_frame_data: dict) -> dict:
    """
    Calcula la confianza de sincronización entre source y target usando
    correlación de Pearson sobre MaxCLL.

    La correlación mide similitud de forma (insensible a diferencias de escala),
    que es lo relevante para verificar que source y target están temporalmente
    alineados aunque los valores absolutos de MaxCLL difieran por grading.

    Devuelve:
      {
        "pearson": float [-1, 1],
        "confidence_pct": int [0, 100],
        "rating": "excellent" | "good" | "moderate" | "poor" | "insufficient_data",
        "reason": str,
        "threshold_ok": bool  # True si confidence >= 85%
      }
    """
    data = per_frame_data.get("data", [])
    # Filtrar datapoints con valores válidos en ambas series (>0 para ignorar negros)
    paired = [
        (d.get("src_maxcll", 0) or 0, d.get("tgt_maxcll", 0) or 0)
        for d in data
        if (d.get("src_maxcll", 0) or 0) > 0 and (d.get("tgt_maxcll", 0) or 0) > 0
    ]
    n = len(paired)
    if n < 20:
        return {
            "pearson": 0.0,
            "confidence_pct": 0,
            "rating": "insufficient_data",
            "reason": f"Solo {n} puntos válidos — necesarios al menos 20",
            "threshold_ok": False,
        }

    src = [p[0] for p in paired]
    tgt = [p[1] for p in paired]

    mean_s = sum(src) / n
    mean_t = sum(tgt) / n
    num = sum((s - mean_s) * (t - mean_t) for s, t in zip(src, tgt))
    den_s = (sum((s - mean_s) ** 2 for s in src)) ** 0.5
    den_t = (sum((t - mean_t) ** 2 for t in tgt)) ** 0.5

    if den_s == 0 or den_t == 0:
        return {
            "pearson": 0.0,
            "confidence_pct": 0,
            "rating": "no_variance",
            "reason": "Una de las series no tiene variación (datos planos)",
            "threshold_ok": False,
        }

    pearson = num / (den_s * den_t)
    # Clamp
    pearson = max(-1.0, min(1.0, pearson))
    # Convertir a porcentaje de confianza: -1 → 0%, +1 → 100%
    confidence_pct = int(round((pearson + 1) / 2 * 100))

    if pearson > 0.95:
        rating, reason = "excellent", "Sincronización muy precisa — las curvas coinciden en forma casi perfectamente"
    elif pearson > 0.85:
        rating, reason = "good", "Sincronización correcta — las curvas siguen el mismo patrón temporal"
    elif pearson > 0.70:
        rating, reason = "moderate", "Sincronización aceptable pero con divergencias — revisa varias zonas del gráfico"
    elif pearson > 0.50:
        rating, reason = "poor", "Sincronización baja — revisa que el RPU target corresponda a la misma película"
    else:
        rating, reason = "poor", "Sin sincronización — probablemente masters incompatibles"

    return {
        "pearson": round(pearson, 4),
        "confidence_pct": confidence_pct,
        "rating": rating,
        "reason": reason,
        "threshold_ok": pearson >= 0.85,
    }


def detect_sync_offset(per_frame_data: dict, max_offset: int = 200) -> dict:
    """
    Detecta el offset de frames entre source y target por cross-correlation
    sobre MaxCLL en los primeros N frames no-negros.

    Devuelve {"offset": int, "confidence": float, "reason": str}.
    """
    data = per_frame_data.get("data", [])
    src_vals = [d.get("src_maxcll", 0) for d in data]
    tgt_vals = [d.get("tgt_maxcll", 0) for d in data]

    # Ventana de análisis: primeros 1000 frames con variación significativa
    def _window(vals, size=1000):
        non_zero_idx = next((i for i, v in enumerate(vals) if v > 10), 0)
        return vals[non_zero_idx:non_zero_idx + size]

    src_w = _window(src_vals)
    tgt_w = _window(tgt_vals)

    if len(src_w) < 100 or len(tgt_w) < 100:
        return {"offset": 0, "confidence": 0.0, "reason": "Pocos frames con contenido"}

    # Cross-correlation simple: buscar offset con menor error RMS
    best_offset = 0
    best_error  = float("inf")
    compare_len = min(200, len(src_w) // 2, len(tgt_w) // 2)

    for offset in range(-max_offset, max_offset + 1):
        errors = []
        for i in range(compare_len):
            src_i = i
            tgt_i = i + offset
            if 0 <= tgt_i < len(tgt_w) and src_i < len(src_w):
                errors.append((src_w[src_i] - tgt_w[tgt_i]) ** 2)
        if not errors:
            continue
        rms = (sum(errors) / len(errors)) ** 0.5
        if rms < best_error:
            best_error = rms
            best_offset = offset

    # Confianza: qué tan bajo es el error vs la varianza de la señal
    src_mean = sum(src_w) / len(src_w) if src_w else 1
    confidence = max(0.0, min(1.0, 1.0 - (best_error / (src_mean + 1))))

    reason = (
        f"Offset={best_offset} frames (confianza={confidence:.1%}, RMS error={best_error:.1f})"
        if confidence > 0.5
        else f"Offset={best_offset} frames, pero confianza baja ({confidence:.1%}) — verifica manualmente"
    )
    return {"offset": best_offset, "confidence": confidence, "reason": reason}
