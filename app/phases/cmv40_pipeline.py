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


async def check_disk_space_preflight(
    session: CMv40Session,
    log_callback=None,
) -> None:
    """Verifica que hay espacio en /mnt/tmp y /mnt/output antes de empezar.

    Requisitos (empíricos, conservadores):
      - TMP:    2 × size(source.mkv)  → source.hevc + (BL+EL o source_injected) + buffers
      - OUTPUT: 1.1 × size(source.mkv) → .mkv.tmp durante Fase G

    Si falla, lanza RuntimeError con mensaje explícito. El pipeline aborta
    ANTES de gastar tiempo en ffmpeg/dovi_tool que se estrellarían a mitad.
    """
    src = Path(session.source_mkv_path)
    try:
        src_size = src.stat().st_size if src.exists() else 0
    except OSError:
        src_size = 0
    if src_size <= 0:
        return  # no podemos verificar

    # En drop-in FEL el requisito de TMP baja a ~1.5× (source.hevc + source_injected)
    tmp_mult    = 1.5 if is_drop_in_fel(session) else 2.0
    output_mult = 1.1

    required_tmp    = int(src_size * tmp_mult)
    required_output = int(src_size * output_mult)

    try:
        tmp_free = shutil.disk_usage(CMV40_WORK_BASE if CMV40_WORK_BASE.exists() else TMP_DIR).free
    except Exception:
        tmp_free = -1
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_free = shutil.disk_usage(OUTPUT_DIR).free
    except Exception:
        out_free = -1

    problems: list[str] = []
    if tmp_free >= 0 and tmp_free < required_tmp:
        problems.append(
            f"/mnt/tmp: necesita {required_tmp/1e9:.1f} GB, disponibles {tmp_free/1e9:.1f} GB"
        )
    if out_free >= 0 and out_free < required_output:
        problems.append(
            f"/mnt/output: necesita {required_output/1e9:.1f} GB, disponibles {out_free/1e9:.1f} GB"
        )

    if problems:
        raise RuntimeError(
            "Espacio insuficiente para ejecutar el pipeline:\n  - "
            + "\n  - ".join(problems)
            + "\nLibera espacio o mueve el MKV origen antes de continuar."
        )
    if log_callback and tmp_free > 0 and out_free > 0:
        await log_callback(
            f"[Preflight] Espacio OK — tmp:{tmp_free/1e9:.0f} GB libres "
            f"(necesita ~{required_tmp/1e9:.0f}), output:{out_free/1e9:.0f} GB "
            f"(necesita ~{required_output/1e9:.0f})"
        )


def compute_file_sha256(path: Path) -> str:
    """Calcula SHA-256 hex de un fichero. Usado para huella del bin target."""
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cleanup_orphan_tmp(session: CMv40Session) -> int:
    """Borra {OUTPUT_DIR}/{output_mkv_name}.tmp si existe. Devuelve bytes liberados.

    Se llama desde el wrapper de fase cuando Fase G o H fallan — sin esto, el
    .mkv.tmp queda huérfano en /mnt/output contaminando el directorio final.
    """
    if not session.output_mkv_name:
        return 0
    tmp_path = OUTPUT_DIR / f"{session.output_mkv_name}.tmp"
    if tmp_path.exists() and tmp_path.is_file():
        try:
            freed = tmp_path.stat().st_size
            tmp_path.unlink()
            return freed
        except OSError as e:
            _logger.warning("No pude borrar .mkv.tmp huérfano: %s", e)
    return 0


def is_drop_in_fel(session: CMv40Session) -> bool:
    """True si el pipeline debe operar en modo drop-in sobre BL+EL sin demux/mux.

    Condiciones: source workflow p7_fel + target P7 FEL CMv4.0 ya cocinado +
    gates de trust OK + usuario en modo auto. En este modo inject-rpu se
    ejecuta directamente sobre source.hevc (BL+EL combinados) — evita demux
    en Fase C y mux en Fase G, ahorra ~90 GB de I/O temporal.
    """
    return (
        (session.source_workflow or "p7_fel") == "p7_fel"
        and session.target_type == "trusted_p7_fel_final"
        and bool(session.target_trust_ok)
        and session.trust_override != "force_interactive"
    )


# Artefactos requeridos para haber completado cada fase.
# Excluye artefactos que el housekeeping borra intencionalmente (source.hevc
# tras Fase C para p7_fel/mel, EL.hevc para p7_mel, per_frame_data si trusted).
# Cada fase al ejecutarse valida sus entradas específicas (fallando con
# mensaje claro si algo falta), así que esta lista solo cubre los ficheros
# que siempre existen tras la fase para cualquier workflow.
PHASE_REQUIRED_ARTIFACTS: dict[str, list[tuple[str, int]]] = {
    # Fase A: source.hevc puede ser borrado por housekeeping tras Fase C,
    # así que solo RPU_source.bin es estable como marcador de "A hecha".
    "source_analyzed": [("RPU_source.bin", 1_000)],
    "target_provided": [("RPU_source.bin", 1_000), ("RPU_target.bin", 1_000)],
    # extracted: los outputs dependen del workflow (BL.hevc para p7_*,
    # source.hevc ya re-extractable para p8). Solo los RPUs son universales.
    "extracted":       [("RPU_source.bin", 1_000), ("RPU_target.bin", 1_000)],
    "sync_verified":   [("RPU_source.bin", 1_000), ("RPU_target.bin", 1_000)],
    # injected: uno de los dos outputs según workflow
    "injected":        [("RPU_source.bin", 1_000), ("RPU_target.bin", 1_000)],
    "remuxed":         [("output.mkv", 1_000_000)],
}


def validate_artifacts(session: CMv40Session) -> dict:
    """Valida que los artefactos de la fase actual existan.

    Devuelve {
      'valid_phase': str,            # fase coherente con lo que hay en disco
      'changed': bool,               # True si valid_phase != session.phase
      'missing': list[str],          # artefactos faltantes para la fase actual
      'message': str,                # descripción para UI
      'all_missing': bool,           # True si no hay ningún artefacto utilizable
    }

    Estrategia: desde la fase actual, retrocede hasta encontrar la fase más
    reciente cuyos artefactos estén todos presentes. Si nada encaja, devuelve
    'created' y all_missing=True.

    NO se ejecuta sobre proyectos archivados (sus artefactos fueron borrados
    a propósito) ni sobre proyectos en fase 'done' (el output vive en /mnt/output).
    """
    from models import CMV40_PHASES_ORDER  # import tardío para evitar ciclos

    result = {
        "valid_phase": session.phase,
        "changed": False,
        "missing": [],
        "message": "",
        "all_missing": False,
    }
    if session.archived:
        result["message"] = "Proyecto archivado — artefactos borrados intencionadamente."
        return result
    if session.phase == "done":
        # Validar que el MKV final sigue existiendo en /mnt/output
        if session.output_mkv_path and Path(session.output_mkv_path).exists():
            return result
        result["valid_phase"] = "remuxed"
        result["changed"] = True
        result["missing"] = [session.output_mkv_path or "output.mkv"]
        result["message"] = "El MKV final no existe en /mnt/output — revertido a fase remuxed."
        return result
    if session.phase == "created":
        return result

    wd = get_workdir(session)
    cur_idx = CMV40_PHASES_ORDER.index(session.phase)

    def _missing_for(phase_key: str) -> list[str]:
        out = []
        for name, min_size in PHASE_REQUIRED_ARTIFACTS.get(phase_key, []):
            p = wd / name
            if not p.exists() or not p.is_file() or p.stat().st_size < min_size:
                out.append(name)
        return out

    # Comprobar la fase actual primero
    missing_now = _missing_for(session.phase)
    if not missing_now:
        return result  # todo OK

    # Retroceder buscando la última fase válida
    for i in range(cur_idx - 1, 0, -1):
        phase_key = CMV40_PHASES_ORDER[i]
        if phase_key not in PHASE_REQUIRED_ARTIFACTS:
            continue
        if not _missing_for(phase_key):
            result["valid_phase"] = phase_key
            result["changed"] = True
            result["missing"] = missing_now
            result["message"] = (
                f"Faltan artefactos de la fase {session.phase}: {', '.join(missing_now)}. "
                f"Revertido a fase {phase_key} — se puede reanudar desde ahí."
            )
            return result

    # Nada válido hasta 'created'
    result["valid_phase"] = "created"
    result["changed"] = True
    result["missing"] = missing_now
    result["all_missing"] = True
    result["message"] = (
        f"No se encuentra ningún artefacto intermedio. Faltan: {', '.join(missing_now)}. "
        f"Hay que empezar desde Fase A."
    )
    return result


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


async def _probe_frame_count(media_path: str) -> int:
    """Devuelve nb_frames del stream v:0 (0 si falla). Rápido — lee metadata."""
    try:
        rc, out, _ = await _run([
            FFPROBE_BIN, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=nb_frames",
            "-of", "csv=p=0",
            media_path,
        ], timeout=15)
        if rc == 0 and out.strip() and out.strip() != "N/A":
            return int(out.strip())
    except Exception:
        pass
    # Fallback: calcular desde duration × fps
    try:
        dur = await _probe_duration(media_path)
        if dur > 0:
            return int(dur * 23.976)
    except Exception:
        pass
    return 0


# Ratios medidos vs wall-time de ffmpeg HEVC extract (anclaje empírico).
# Todas las operaciones silenciosas comparten el cuello de botella I/O del NAS,
# por eso escalan linealmente con el ffmpeg previo. Medido en 2 runs del mismo
# MKV con NAS a distinta carga (116s y 157s de ffmpeg) — los ratios se mantienen.
RATIO_EXTRACT_RPU  = 0.92    # extract-rpu / ffmpeg
RATIO_DEMUX        = 1.30    # demux / ffmpeg
RATIO_EXPORT       = 0.19    # export -d all (por RPU) / ffmpeg
RATIO_INJECT       = 1.77    # inject-rpu / ffmpeg
RATIO_MUX          = 1.88    # dovi_tool mux / ffmpeg

# Fallbacks si no conocemos ffmpeg_wall_seconds (sesiones antiguas o sin A).
FPS_FFMPEG_EXTRACT = 1336.0
FPS_EXTRACT_RPU    = 1450.0
FPS_DEMUX          = 1100.0
FPS_EXPORT         = 7000.0
FPS_INJECT         = 760.0
FPS_MUX            = 711.0
FPS_MKVMERGE       = 429.0


def _estimate_from_ffmpeg(session: CMv40Session, ratio: float, fps_fallback: float) -> float:
    """Devuelve estimación wall-time en segundos.

    Preferente: ffmpeg_wall_seconds * ratio (adapta a la carga actual del NAS).
    Fallback: frame_count / fps_fallback (constante, cuando no hay ancla).
    """
    if session.ffmpeg_wall_seconds and session.ffmpeg_wall_seconds > 5:
        return session.ffmpeg_wall_seconds * ratio
    if session.source_frame_count:
        return session.source_frame_count / fps_fallback
    return 120.0


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
          'duration': float,           # duración conocida a priori (ffmpeg, s); o 0
          'time_estimate_s': float,    # alternativa: estimación wall-clock (para comandos silenciosos)
          'offset': float,             # pct base (0-100)
          'weight': float,             # peso de este paso en la fase (0-100)
          'label': str,                # etiqueta a mostrar
        }
    Prioridad: ffmpeg time= > mkvmerge Progress: > time_estimate_s (ticker).
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
    duration = float(progress_ctx["duration"]) if progress_ctx and progress_ctx.get("duration") else 0.0
    offset   = float(progress_ctx.get("offset", 0.0)) if progress_ctx else 0.0
    weight   = float(progress_ctx.get("weight", 100.0)) if progress_ctx else 100.0
    label    = progress_ctx.get("label", "") if progress_ctx else ""
    time_est = float(progress_ctx.get("time_estimate_s", 0.0)) if progress_ctx else 0.0
    step_start = time.monotonic()
    has_real_progress = False  # se pone True si detectamos ffmpeg time= o mkvmerge Progress:

    async def _emit(text: str) -> None:
        nonlocal last_throttle, last_progress_push, duration, has_real_progress
        if not text:
            return
        if text.startswith("#GUI#progress "):
            text = "Progress: " + text.removeprefix("#GUI#progress ")
        elif text.startswith("#GUI#"):
            return

        # mkvmerge "Progress: XX%" — progreso real
        if progress_ctx is not None and text.startswith("Progress:"):
            m = re.search(r"Progress:\s*(\d+)%", text)
            if m:
                has_real_progress = True
                step_pct = float(m.group(1))
                phase_pct = offset + step_pct * weight / 100.0
                wall = time.monotonic() - step_start
                eta = (wall / step_pct) * (100 - step_pct) if step_pct > 1 else None
                await _emit_progress(log_callback, phase_pct, label, eta)

        # Detectar Duration en el header si aún no la tenemos
        if progress_ctx is not None and duration <= 0:
            m = _FFMPEG_DURATION_RE.search(text)
            if m:
                duration = _hms_to_seconds(*m.groups())

        is_ffmpeg_progress = text.startswith("frame=") and ("fps=" in text or "time=" in text)
        if is_ffmpeg_progress:
            now = time.monotonic()
            if progress_ctx is not None and duration > 0 and (now - last_progress_push) >= 1.0:
                tm = _FFMPEG_TIME_RE.search(text)
                if tm:
                    has_real_progress = True
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

    # Ticker time-based: solo si nos han dado time_estimate_s y no vemos progreso real
    stop_ticker = asyncio.Event()

    async def _ticker():
        if time_est <= 0:
            return
        # Esperar 3s antes del primer tick — si llega progreso real antes, nos callamos
        try:
            await asyncio.wait_for(stop_ticker.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            pass
        while not stop_ticker.is_set():
            if has_real_progress:
                return
            elapsed = time.monotonic() - step_start
            step_pct = min(95.0, (elapsed / time_est) * 100.0)
            phase_pct = offset + step_pct * weight / 100.0
            eta = max(0.0, time_est - elapsed)
            await _emit_progress(log_callback, phase_pct, label, eta)
            try:
                await asyncio.wait_for(stop_ticker.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    tick_task = asyncio.create_task(_ticker()) if time_est > 0 else None

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

    # Parar ticker si estaba activo
    stop_ticker.set()
    if tick_task:
        try:
            await tick_task
        except Exception:
            pass

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
      - source.hevc (puede ser borrado tras Fase C en p7_fel/p7_mel)
      - RPU_source.bin

    Actualiza session.source_dv_info, source_frame_count.
    """
    wd = get_workdir(session)
    source_hevc = wd / "source.hevc"
    rpu_source  = wd / "RPU_source.bin"

    # Pre-flight: abortar si no hay espacio suficiente en /mnt/tmp y /mnt/output
    await check_disk_space_preflight(session, log_callback)

    # Pesos: ffmpeg 50% · extract-rpu 45% · info 5%
    W_FFMPEG, W_RPU, W_INFO = 50.0, 45.0, 5.0

    # Pre-probe duración + frame count para estimar progreso
    duration   = await _probe_duration(session.source_mkv_path)
    frame_count = await _probe_frame_count(session.source_mkv_path)

    # Paso 1: Extraer HEVC del MKV origen
    await _emit_progress(log_callback, 0, "Extrayendo HEVC del MKV origen")
    if log_callback:
        await log_callback(
            "[Fase A] 📋 Plan: extraer el stream HEVC del MKV del Blu-ray, "
            "sacar el RPU Dolby Vision (la metadata que le dice al TV cómo "
            "hacer tone-mapping escena a escena) y detectar el profile. El "
            "RPU extraído es la referencia que luego usaremos para comparar "
            "contra el target y decidir si el upgrade es posible."
        )
        await log_callback(
            "[Fase A] ┌─ Paso 1/3: Extrayendo stream HEVC del MKV origen con ffmpeg…"
        )
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

    # Guardar wall-time de ffmpeg como ancla para estimaciones futuras
    if ffmpeg_elapsed > 5:
        session.ffmpeg_wall_seconds = ffmpeg_elapsed

    # Paso 2: Extraer RPU (silencioso con pipe → progreso estimado por tiempo)
    if log_callback:
        await log_callback("[Fase A] ├─ Paso 2/3: Extrayendo RPU del HEVC con dovi_tool extract-rpu…")
    # Ancla: wall time de ffmpeg × ratio empírico (extract-rpu ≈ 0.92x ffmpeg)
    if ffmpeg_elapsed > 5:
        est_rpu = ffmpeg_elapsed * RATIO_EXTRACT_RPU
    elif frame_count > 0:
        est_rpu = frame_count / FPS_EXTRACT_RPU
    else:
        est_rpu = 120.0
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
        await log_callback("[Fase A] └─ Paso 3/3: Analizando metadata del RPU con dovi_tool info --summary…")
    rc, summary, err = await _run([
        DOVI_TOOL_BIN, "info", "--summary", str(rpu_source),
    ], timeout=30)
    if rc != 0:
        raise RuntimeError(f"dovi_tool info falló: {err[:300]}")

    dovi_info = _parse_dovi_summary(summary)
    session.source_dv_info = dovi_info
    session.source_frame_count = dovi_info.frame_count

    # Detectar workflow según perfil y subperfil (ver CMv40Session.source_workflow)
    session.source_workflow = _detect_workflow(dovi_info)
    workflow_label = {
        "p7_fel": "P7 FEL — demux + merge CMv4.0 + preserva FEL",
        "p7_mel": "P7 MEL — descarta EL, inyecta RPU target → P8.1 CMv4.0",
        "p8":     "P8.1 — inject directo de RPU target → P8.1 CMv4.0",
    }.get(session.source_workflow, session.source_workflow)

    # Plot eliminado: generaba plot_source.png que la UI no consume.
    # Si se quiere reintroducir, añadir también el render en el panel.

    await _emit_progress(log_callback, 100, "Análisis completado")
    if log_callback:
        await log_callback(
            f"[Fase A] ✓ RPU analizado — Profile {dovi_info.profile} ({dovi_info.el_type}), "
            f"CM {dovi_info.cm_version}, {dovi_info.frame_count} frames"
        )
        # Resumen con implicación para las siguientes fases
        next_fase_hint = {
            "p7_fel": ("En Fase B obtendremos un RPU CMv4.0 target; si pasa los trust gates "
                      "usaremos drop-in (sin demux) o merge (preservando FEL)."),
            "p7_mel": ("El MEL no aporta frente a un target CMv4.0 → en Fase F descartaremos "
                      "el EL y dejaremos un MKV single-layer P8.1."),
            "p8":     ("Source ya es single-layer → en Fase F sustituiremos el RPU directamente "
                      "sin tocar las capas."),
        }.get(session.source_workflow, "")
        await log_callback(
            f"[Fase A] 🎯 Resultado: workflow {workflow_label}. {next_fase_hint} "
            f"El RPU source queda guardado como referencia para los trust gates de Fase B."
        )


def _detect_workflow(dovi_info: DoviInfo) -> str:
    """Determina el pipeline según perfil y subperfil del RPU source.

    - P7 FEL → 'p7_fel': demux + merge CMv4.0 + mux preservando dual-layer
    - P7 MEL → 'p7_mel': descartar EL, inject RPU target → P8.1 CMv4.0
    - P8.x   → 'p8':     inject directo sobre el HEVC single-layer
    """
    profile = dovi_info.profile
    el_type = (dovi_info.el_type or "").upper()
    if profile == 7 and el_type == "FEL":
        return "p7_fel"
    if profile == 7 and el_type == "MEL":
        return "p7_mel"
    if profile == 8:
        return "p8"
    raise RuntimeError(
        f"Perfil DV no soportado: Profile {profile} ({el_type}). "
        f"Soportados: P7 FEL, P7 MEL, P8.x"
    )


# ══════════════════════════════════════════════════════════════════════
#  FASE B — Proporcionar RPU target
# ══════════════════════════════════════════════════════════════════════

# Ficheros/dirs típicos de /tmp de QTS — si aparecen, el mount está mal
_QTS_TMP_MARKERS = (
    ".qcloud-vars-cache",
    ".qpkg_start.log",
    "mariadb10_mmc.sock",
    "myconvertserver.sock",
    "netmgr.sock",
    "qpkg_status.conf",
)


def list_available_rpus() -> list[dict]:
    """Lista .bin regulares del nivel superior de /mnt/cmv40_rpus/.

    NO recursivo: un recorrido profundo en ZFS/QNAP pescaba basura del
    sistema (@Recycle/, .@__thumb/, subcarpetas tipo smart/ con data
    packages, snapshots .zfs/). Si el usuario quiere organizar por
    subcarpetas tendrá que colocar los .bin en la raíz del mount.

    Match .bin case-insensitive. Ignora ocultos, AppleDouble y todo lo
    que no sea fichero regular (dirs, symlinks rotos, FIFOs...).

    Detecta cuando el mount apunta por error al /tmp de QTS (si el
    CMV40_RPU_PATH del .env no está seteado y cae al fallback) y
    devuelve lista vacía con un warning explícito en el log.
    """
    if not CMV40_RPU_DIR.exists():
        return []

    try:
        entries = list(CMV40_RPU_DIR.iterdir())
    except OSError:
        return []

    # Defensa contra mount mal configurado: si aparecen markers de /tmp de QTS
    names = {p.name for p in entries}
    if any(m in names for m in _QTS_TMP_MARKERS):
        _logger.warning(
            "CMV40_RPU_DIR (%s) parece ser el /tmp de QTS (contiene sockets/logs "
            "del sistema QNAP). Revisa CMV40_RPU_PATH en el .env del compose y "
            "recrea el contenedor. Se ignoran todos los ficheros.",
            CMV40_RPU_DIR,
        )
        return []

    result: list[dict] = []
    for p in entries:
        name = p.name
        if name.startswith((".", "_")):
            continue
        if not name.lower().endswith(".bin"):
            continue
        try:
            if not p.is_file():
                continue
            size = p.stat().st_size
        except OSError:
            continue
        result.append({
            "name": name,
            "path": str(p),
            "size_bytes": size,
        })

    result.sort(key=lambda r: r["name"].lower())
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
        await log_callback(
            "[Fase B] 📋 Plan: copiar el RPU target desde carpeta local al "
            "workdir del proyecto, analizar su metadata y compararla con el "
            "RPU del Blu-ray (trust gates). Si pasa → auto-pipeline sin "
            "revisión manual. Si falla → pausa en Fase D para revisar a mano."
        )
        await log_callback(f"[Fase B] ┌─ Copiando RPU target local: {src.name}")
    shutil.copy2(src, rpu_target)
    await _emit_progress(log_callback, 70, "Analizando RPU")

    session.target_rpu_source = "path"
    session.target_rpu_path = str(src)
    await _analyze_target_rpu(session, rpu_target, log_callback)
    await _emit_progress(log_callback, 100, "Completado")


async def run_phase_b_target_from_drive(
    session: CMv40Session,
    file_id: str,
    file_name: str,
    log_callback=None,
) -> None:
    """Descarga un .bin del repositorio de REC_9999 en Drive al workdir
    y lo analiza. `file_name` se usa solo para el log."""
    from services.rec999_drive import download_file

    wd = get_workdir(session)
    rpu_target = wd / "RPU_target.bin"

    await _emit_progress(log_callback, 0, f"Descargando del repositorio: {file_name}")
    if log_callback:
        await log_callback(
            "[Fase B] 📋 Plan: descargar el RPU target del repositorio público "
            "DoviTools (Google Drive), analizar su metadata y compararla con el "
            "RPU del Blu-ray (trust gates). Si pasa → auto-pipeline sin revisión "
            "manual (ruta más rápida). Si algún gate crítico falla → pausa en "
            "Fase D para revisar a mano."
        )
        await log_callback(f"[Fase B] ┌─ Descargando RPU target del repo DoviTools: {file_name}")

    last_emit = 0.0

    async def _progress(done: int, total: int | None) -> None:
        nonlocal last_emit
        now = time.monotonic()
        if now - last_emit < 0.3:
            return
        last_emit = now
        if total and total > 0:
            pct = 0.0 + (done / total) * 70.0  # reserva 30% para el analyze
            label = f"Descargando… {done/1024/1024:.1f}/{total/1024/1024:.1f} MB"
        else:
            pct = min(60.0, done / 1024 / 1024)  # aprox sin total
            label = f"Descargando… {done/1024/1024:.1f} MB"
        await _emit_progress(log_callback, pct, label)

    try:
        written = await download_file(file_id, rpu_target, progress_cb=_progress)
    except Exception as e:
        raise RuntimeError(f"Descarga de Drive falló: {e}")

    if log_callback:
        await log_callback(
            f"[Fase B] Descargados {written/1024/1024:.1f} MB a {rpu_target.name}"
        )
    await _emit_progress(log_callback, 70, "Analizando RPU descargado")

    session.target_rpu_source = "drive"
    session.target_rpu_path = f"drive://{file_id}/{file_name}"
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
    duration    = await _probe_duration(source_mkv_path)
    frame_count = await _probe_frame_count(source_mkv_path)

    try:
        await _emit_progress(log_callback, 0, "Extrayendo HEVC del MKV target")
        if log_callback:
            await log_callback(
                "[Fase B] 📋 Plan: extraer el RPU CMv4.0 de un MKV propio que ya "
                "tiene el grading que quieres aplicar (p. ej. WEB-DL moderno), "
                "analizar su metadata y comparar contra el RPU del Blu-ray. "
                "Esta ruta siempre pasa por Fase D manual — no hay pre-validación "
                "comunitaria que garantice la alineación."
            )
            await log_callback(f"[Fase B] ┌─ Extrayendo HEVC del MKV target: {Path(source_mkv_path).name}")
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
        # Ancla: wall time del ffmpeg que acabamos de medir (mejor que del source)
        if ffmpeg_elapsed > 5:
            est_rpu = ffmpeg_elapsed * RATIO_EXTRACT_RPU
        elif frame_count > 0:
            est_rpu = frame_count / FPS_EXTRACT_RPU
        else:
            est_rpu = 120.0
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
    """Ejecuta dovi_tool info sobre el RPU target, clasifica el tipo y
    evalúa los gates de trust."""
    # SHA-256 del .bin — huella para detectar repacks del repo DoviTools.
    # Si REC_9999 republica el bin con correcciones, el hash cambiará y
    # podrás decidir si rehacer el MKV. El .bin es pequeño (<10 MB).
    try:
        session.target_rpu_sha256 = await asyncio.to_thread(compute_file_sha256, rpu_path)
        if log_callback:
            await log_callback(
                f"[Fase B] SHA-256 del bin target: {session.target_rpu_sha256[:12]}…"
            )
    except Exception as e:
        _logger.warning("No se pudo calcular SHA-256 del bin target: %s", e)
        session.target_rpu_sha256 = ""

    rc, summary, err = await _run([
        DOVI_TOOL_BIN, "info", "--summary", str(rpu_path),
    ], timeout=30)
    if rc != 0:
        raise RuntimeError(f"dovi_tool info falló sobre RPU target: {err[:300]}")

    dovi_info = _parse_dovi_summary(summary)
    session.target_dv_info = dovi_info
    session.target_frame_count = dovi_info.frame_count
    session.sync_delta = dovi_info.frame_count - session.source_frame_count

    # Clasificar el tipo de target (v1.9 — integración DoviTools bins)
    session.target_type = _classify_target_type(dovi_info)
    # Evaluar gates de trust (solo si tipo no es 'generic' ni 'incompatible')
    gates, trust_ok = _evaluate_trust_gates(session.source_dv_info, dovi_info,
                                             session.source_frame_count,
                                             session.target_frame_count)
    session.target_trust_gates = gates
    # Solo confiamos si tipo permite skip Y todos los gates críticos pasaron
    trusted_types = (
        "trusted_p7_fel_final", "trusted_p7_mel_final", "trusted_p8_source",
    )
    session.target_trust_ok = (session.target_type in trusted_types) and trust_ok

    if log_callback:
        await log_callback(
            f"[Fase B] ✓ RPU target analizado — Profile {dovi_info.profile}"
            f"{' (' + dovi_info.el_type + ')' if dovi_info.el_type else ''}, "
            f"CM {dovi_info.cm_version}, {dovi_info.frame_count} frames "
            f"(Δ = {session.sync_delta:+d} frames vs source)"
        )
        # Log detallado de gates que fallan (útil para diagnóstico)
        failing = [k for k, v in gates.items() if isinstance(v, dict) and not v.get("ok", True)]
        if failing:
            await log_callback(
                f"[Fase B] ⚠ Gates que NO pasan: {', '.join(failing)}"
            )
        # Resultado con implicación para las siguientes fases — depende del
        # source_workflow + target_type porque Fase F elige ruta segun la
        # combinacion (drop-in / direct-inject / merge).
        sw = session.source_workflow or "p7_fel"
        if session.target_trust_ok:
            if session.target_type == "trusted_p7_fel_final":
                if sw == "p7_fel":
                    implication = (
                        "bin P7 FEL CMv4.0 + source P7 FEL → drop-in. Fase C saltará demux, "
                        "Fase D saltará revisión visual, Fase F hará inject-rpu directo "
                        "sobre source.hevc (BL+EL intactos). Ahorro ~90 GB de I/O."
                    )
                else:
                    implication = (
                        f"bin P7 FEL CMv4.0 + source {sw.upper()} → no drop-in (profiles "
                        f"distintos). Fase D saltará revisión visual; Fase F mergeará los "
                        f"levels CMv4.0 del target en el RPU del source preservando su "
                        f"estructura single-layer. Resultado: P8.1 CMv4.0."
                    )
            elif session.target_type == "trusted_p7_mel_final":
                if sw == "p7_mel":
                    implication = (
                        "bin P7 MEL CMv4.0 + source P7 MEL → mismo profile. Fase D saltará "
                        "revisión visual. Fase F descarta el EL MEL e inyecta el RPU target "
                        "en BL directamente → P8.1 CMv4.0."
                    )
                elif sw == "p7_fel":
                    implication = (
                        "bin P7 MEL CMv4.0 + source P7 FEL → Fase F mergeará CMv4.0 en el "
                        "RPU P7 del source preservando la FEL. Resultado: P7 FEL CMv4.0."
                    )
                else:  # p8
                    implication = (
                        "bin P7 MEL CMv4.0 + source P8.1 → Fase F mergeará los levels "
                        "CMv4.0 del target en el RPU P8 del source (via dovi_tool editor "
                        "allow_cmv4_transfer). Resultado: P8.1 CMv4.0."
                    )
            elif session.target_type == "trusted_p8_source":
                if sw == "p7_fel":
                    implication = (
                        "bin P8 retail CMv4.0 + source P7 FEL → Fase F mergeará CMv4.0 "
                        "en el RPU P7 preservando la FEL. Resultado: P7 FEL CMv4.0."
                    )
                elif sw == "p7_mel":
                    implication = (
                        "bin P8 retail CMv4.0 + source P7 MEL → Fase F descartará EL e "
                        "inyectará el RPU P8 directamente sobre BL. Resultado: P8.1 CMv4.0."
                    )
                else:  # p8
                    implication = (
                        "bin P8 retail CMv4.0 + source P8.1 → mismo profile. Fase F "
                        "inyectará el RPU target directamente sobre source.hevc. "
                        "Resultado: P8.1 CMv4.0 refinado."
                    )
            else:
                implication = "se salta revisión manual."
            await log_callback(
                f"[Fase B] 🎯 Resultado: target clasificado como {session.target_type} "
                f"— TRUSTED ✓ gates OK. {implication}"
            )
        else:
            crit_fail = any(gates.get(k, {}).get("critical") and not gates.get(k, {}).get("ok")
                            for k in gates if isinstance(gates.get(k), dict))
            if crit_fail:
                implication = "algún gate crítico ha fallado — el pipeline pasará por Fase D obligatoriamente y podrá requerir corrección de sync manual en Fase E."
            else:
                implication = "gates soft con avisos (divergencias no críticas) — Fase D obligatoria para revisar el chart pero sin abortar."
            await log_callback(
                f"[Fase B] 🎯 Resultado: target clasificado como {session.target_type} "
                f"— NO trusted. {implication}"
            )

    # Hard aborts tras evaluar gates — evitan gastar Fase C/D/etc. en targets
    # estructuralmente inservibles. El usuario puede elegir otro bin en lugar
    # de perder tiempo con una revisión manual condenada al fracaso.
    #
    # (a0) Target sin CMv4.0: no hay metadata que transferir — ni el chart ni
    #      correcciones manuales pueden materializarla. Es punto muerto absoluto.
    if session.target_type == "incompatible":
        cm = (dovi_info.cm_version or "desconocido")
        abort_msg = (
            f"Target sin CMv4.0 (CM {cm}). No hay metadata L8-L11 que transferir "
            f"al RPU del BD — este pipeline solo puede inyectar CMv4.0 sobre CMv2.9. "
            f"Usa un bin del repo DoviTools o extrae de un MKV que SÍ tenga CMv4.0 "
            f"(mkvinfo mostrará 'dv_cm_version: v4.0' o dovi_tool info 'CM v4.0')."
        )
        session.compat_warning = abort_msg
        if log_callback:
            await log_callback(f"[Fase B] ⛔ {abort_msg}")
        raise RuntimeError(abort_msg)

    # NOTA: el spec define "L5 div >30 aborta" pero en la practica hay casos
    # legitimos donde L5 diverge sin que el bin sea inservible: fuentes
    # convertidas (MEL→P8.1 con dovi_tool) pueden tener L5 con variaciones
    # vs retail bins limpios aun siendo del mismo master. En lugar de abortar,
    # marcamos trust_ok=False (ya hecho por el gate) -> pausa en Fase D, el
    # usuario ve el chart y decide. Para el caso claro de edicion distinta
    # (WEB-DL scope vs BD, ~276 px) el chart tambien muestra la incompatibilidad
    # y el usuario puede cancelar sin haber inyectado nada irrecuperable.

    # (b) Compatibilidad estructural source × target (ej. source single-layer
    #     + target P7 dual-layer drop-in). En Fase F hay un safety net por
    #     si se bypassea este check.
    if session.source_workflow:
        compat_ok, compat_msg = _check_source_target_compat(
            session.source_workflow, session.target_type
        )
        session.compat_warning = "" if compat_ok else compat_msg
        if not compat_ok:
            if log_callback:
                await log_callback(f"[Fase B] ⛔ {compat_msg}")
            raise RuntimeError(compat_msg)


def _check_source_target_compat(source_workflow: str, target_type: str) -> tuple[bool, str]:
    """Valida compatibilidad estructural source + target.

    Ya no aborta para `source single-layer + target P7 dual-layer`: aunque el
    target no se puede aplicar como drop-in, su RPU sirve como DONANTE de
    metadata CMv4.0 (levels L1-L11) via merge. `dovi_tool editor` con
    `allow_cmv4_transfer` copia los levels entre RPUs independientemente del
    profile; el profile de SALIDA hereda del source (P8.1 o MEL), que es lo
    que el HEVC del BD ya tiene. La Fase F decide entre drop-in / merge /
    direct-inject segun source y target_type.

    Actualmente no hay combinaciones estructuralmente imposibles — siempre
    hay un camino via merge. Devuelve (True, "") siempre, pero mantenemos la
    firma por si aparece un caso futuro que justifique abort aqui.
    """
    return True, ""


def _classify_target_type(info: DoviInfo) -> str:
    """Clasifica el RPU target según perfil + CM version + L8 presente.

    Ver docs en CMv40Session.target_type para los valores posibles.
    """
    cm = (info.cm_version or "").lower()
    has_cmv4 = cm in ("v4.0", "4.0")
    el = (info.el_type or "").upper()

    if not has_cmv4:
        # No tiene CMv4.0 → no sirve como fuente de transfer (source de BD
        # sí es v2.9 pero eso NO es un target).
        return "incompatible"

    # Con CMv4.0 confirmado, distinguimos por profile
    if info.profile == 7 and el == "FEL":
        # Bin P7 FEL con CMv4.0 ya cocinado — drop-in candidate
        return "trusted_p7_fel_final"
    if info.profile == 7 and el == "MEL":
        return "trusted_p7_mel_final"
    if info.profile == 8:
        # Bin P8 con CMv4.0 — sirve como source para el transfer (rama B)
        # Requerimos L8 presente: sin L8 no aporta nada al merge
        if info.has_l8:
            return "trusted_p8_source"
        # Sin L8 es un P8 "plano" — igual funciona pero el merge no tendrá
        # trims CMv4.0 útiles que transferir. Lo marcamos como generic.
        return "generic"
    # P5 o profile desconocido con CMv4.0 — caso raro, tratamos como generic
    return "generic"


def _evaluate_trust_gates(source_info: DoviInfo | None, target_info: DoviInfo,
                           source_frames: int, target_frames: int) -> tuple[dict, bool]:
    """Evalúa los gates (frame count + L1/L5/L6 divergence) y devuelve:
      - dict con resultado de cada gate
      - bool `trust_ok`: True si todos los críticos pasan

    Umbrales (spec §5):
      - frames: 0 tolerancia (crítico)
      - cm_version: debe ser v4.0 (crítico)
      - has_l8: requerido para trusted_p8_source (crítico en ese caso)
      - L5 div: ≤5 px = ok, 5-30 = warn, >30 = crítico abort
      - L6 MaxCLL: diff ≤ 50 nits = ok, soft warn si más
      - L1 MaxCLL: diff ≤ 5% = ok, soft warn
    """
    gates: dict = {}

    # Frame count — crítico, sin tolerancia
    gates["frames"] = {
        "ok": source_frames > 0 and source_frames == target_frames,
        "bd": source_frames,
        "target": target_frames,
        "critical": True,
    }

    # CM version — crítico, debe ser v4.0
    cm = (target_info.cm_version or "").lower()
    gates["cm_version"] = {
        "ok": cm in ("v4.0", "4.0"),
        "value": target_info.cm_version or "(desconocido)",
        "critical": True,
    }

    # L8 presente — crítico para transfer útil
    gates["has_l8"] = {
        "ok": bool(target_info.has_l8),
        "critical": True,
    }

    # Gates comparativos (solo si tenemos source_info)
    if source_info is not None:
        # L5 divergencia — distancia Chebyshev en los 4 offsets
        l5_diffs = [
            abs(source_info.l5_top    - target_info.l5_top),
            abs(source_info.l5_bottom - target_info.l5_bottom),
            abs(source_info.l5_left   - target_info.l5_left),
            abs(source_info.l5_right  - target_info.l5_right),
        ]
        l5_max = max(l5_diffs) if any(l5_diffs) else 0
        gates["l5_div"] = {
            "ok": l5_max <= 30,          # crítico si >30
            "px_max": l5_max,
            "soft_px": 5,
            "critical_px": 30,
            "warn": 5 < l5_max <= 30,
            "critical": True,            # crítico si >30
        }

        # L6 MaxCLL diff — soft
        l6_diff = abs((source_info.l6_max_cll or 0) - (target_info.l6_max_cll or 0))
        gates["l6_div"] = {
            "ok": l6_diff <= 50,
            "nits_diff": l6_diff,
            "threshold": 50,
            "critical": False,           # soft
        }

        # L1 MaxCLL diff %
        src_l1 = source_info.l1_max_cll or 0
        tgt_l1 = target_info.l1_max_cll or 0
        if src_l1 > 0 and tgt_l1 > 0:
            pct = abs(src_l1 - tgt_l1) / max(src_l1, tgt_l1) * 100.0
            gates["l1_div"] = {
                "ok": pct <= 5.0,
                "pct_diff": round(pct, 2),
                "threshold_pct": 5.0,
                "critical": False,       # soft
            }

    # trust_ok: TODOS los críticos deben pasar
    trust_ok = all(
        g.get("ok", False) for g in gates.values()
        if isinstance(g, dict) and g.get("critical", False)
    )
    return gates, trust_ok


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

    # source.hevc puede haberse borrado tras un Fase C previo (housekeeping
    # v1.9). Si el usuario rehace Fase C lo re-extraemos en ~30s con
    # ffmpeg -c copy desde el MKV origen.
    if not source_hevc.exists():
        if Path(session.source_mkv_path).exists():
            if log_callback:
                await log_callback(
                    "[Fase C] source.hevc no encontrado (fue borrado tras un "
                    "demux previo) — re-extrayéndolo del MKV origen…"
                )
            rc = await _run_streaming([
                FFMPEG_BIN, "-y", "-i", session.source_mkv_path,
                "-map", "0:v:0", "-c:v", "copy",
                "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", str(source_hevc),
            ], log_callback=log_callback, proc_callback=proc_callback)
            if rc != 0 or not source_hevc.exists():
                raise RuntimeError("Re-extracción de source.hevc falló")
        else:
            raise RuntimeError("source.hevc no existe y el MKV origen no está accesible")
    if not rpu_target.exists():
        raise RuntimeError("RPU_target.bin no existe — ejecuta Fase B primero")

    workflow = session.source_workflow or "p7_fel"
    drop_in_fel = is_drop_in_fel(session)

    # Para p8 no hace falta demux — el source ya es single-layer. Se mantiene
    # source.hevc como "capa única" y no se generan BL.hevc/EL.hevc.
    # Para p7_mel/p7_fel sí hay demux, SALVO en drop-in FEL (bin ya cocinado):
    # ahí inject-rpu irá directo sobre source.hevc (BL+EL), ahorra ~90 GB I/O.
    # MEL: se conserva BL y se descarta EL lógicamente.
    needs_demux = workflow in ("p7_fel", "p7_mel") and not drop_in_fel
    if drop_in_fel:
        for skip in ("demux_dual_layer", "mux_dual_layer"):
            if skip not in session.phases_skipped:
                session.phases_skipped.append(skip)

    # ¿Saltamos `per_frame_data.json`? Sí cuando el target es trusted y los
    # gates pasan — Fase D se saltará → el chart no se mostrará. Ahorra
    # ~2-5 min de CPU (2 pasadas de dovi_tool export sobre ambos RPUs).
    # Si el usuario fuerza revisión manual tardía, el endpoint /sync-data
    # regenera on-demand.
    skip_pfd = bool(session.target_trust_ok) and session.trust_override != "force_interactive"

    # Pesos de progreso: demux 70% / PFD 30% si ambos, o 100% al que toque
    if needs_demux and not skip_pfd:
        W_DEMUX, W_PFD = 70.0, 30.0
    elif needs_demux and skip_pfd:
        W_DEMUX, W_PFD = 100.0, 0.0
    elif not needs_demux and not skip_pfd:
        W_DEMUX, W_PFD = 0.0, 100.0
    else:
        W_DEMUX, W_PFD = 0.0, 0.0  # no-op (poco frecuente: p8 + trusted)

    # Plan de la fase segun lo que realmente vamos a hacer
    if log_callback:
        plan_parts = []
        if needs_demux:
            plan_parts.append("separar el HEVC dual-layer en BL.hevc + EL.hevc (dovi_tool demux)")
        if not skip_pfd:
            plan_parts.append("generar per_frame_data.json con la luminancia por frame de source y target (para el chart de Fase D)")
        if not plan_parts:
            plan_parts.append("no hacer nada — tanto el demux como el per-frame se saltan porque el target es trusted drop-in")
        await log_callback(
            "[Fase C] 📋 Plan: " + " y ".join(plan_parts) + "."
        )

    if needs_demux:
        est_demux = _estimate_from_ffmpeg(session, RATIO_DEMUX, FPS_DEMUX)
        await _emit_progress(log_callback, 0, "Separando BL + EL")
        if log_callback:
            label = "BL + EL" if workflow == "p7_fel" else "BL (EL MEL será ignorado)"
            await log_callback(f"[Fase C] Separando {label} (dovi_tool demux)…")
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
    else:
        if log_callback:
            if drop_in_fel:
                await log_callback(
                    "[Fase C] ⏭ Demux omitido — drop-in FEL: inject-rpu irá "
                    "directo sobre source.hevc (BL+EL), no hace falta separar capas. "
                    "Ahorro ~90 GB I/O."
                )
            else:
                await log_callback(
                    "[Fase C] Workflow P8: sin demux necesario (source ya es single-layer)"
                )

    if skip_pfd:
        if log_callback:
            await log_callback(
                "[Fase C] ⏭ per_frame_data.json omitido (target trusted — "
                "Fase D se saltará, no hace falta generar datos del chart). "
                "Se regenerará on-demand si el usuario fuerza revisión manual."
            )
        if "per_frame_data_skipped" not in session.phases_skipped:
            session.phases_skipped.append("per_frame_data_skipped")
    else:
        if log_callback:
            await log_callback("[Fase C] Generando datos per-frame para el chart…")
        est_export = max(10.0, _estimate_from_ffmpeg(session, RATIO_EXPORT, FPS_EXPORT))
        await _generate_per_frame_data(
            session, rpu_source, rpu_target, per_frame, log_callback,
            progress_offset=W_DEMUX, progress_weight=W_PFD,
            est_export_s=est_export,
        )

    # ── Housekeeping: borrar artefactos ya innecesarios ────────────────
    # source.hevc (~40 GB): lo necesitan Fase F en workflow p8 Y en drop-in FEL
    #   (inject-rpu directo sobre BL+EL). Para p7_fel con merge clásico y
    #   p7_mel ya tenemos BL.hevc — liberamos disco.
    #   Si el usuario rehace Fase C, Fase A la regenerará (fast: ffmpeg -c copy).
    # EL.hevc (~3–5 GB): en p7_mel el EL MEL no se usa (se descarta para
    #   producir P8.1). Lo borramos tras demux.
    if needs_demux and source_hevc.exists():
        try:
            sz = source_hevc.stat().st_size
            source_hevc.unlink()
            if log_callback:
                await log_callback(
                    f"[Fase C] 🧹 Borrado source.hevc ({sz / 1024**3:.1f} GB) — "
                    f"ya no se necesita para workflow {workflow}"
                )
        except OSError as e:
            if log_callback:
                await log_callback(f"[Fase C] No pude borrar source.hevc: {e}")
    if workflow == "p7_mel" and el_hevc.exists():
        try:
            sz = el_hevc.stat().st_size
            el_hevc.unlink()
            if log_callback:
                await log_callback(
                    f"[Fase C] 🧹 Borrado EL.hevc ({sz / 1024**3:.1f} GB) — "
                    f"MEL se descarta en workflow p7_mel"
                )
        except OSError as e:
            if log_callback:
                await log_callback(f"[Fase C] No pude borrar EL.hevc: {e}")

    await _emit_progress(log_callback, 100, "Completado")

    # Resultado de la fase: qué ha quedado preparado para Fase F/G
    if log_callback:
        result_parts = []
        if needs_demux:
            result_parts.append("BL.hevc" + (" + EL.hevc" if workflow == "p7_fel" else ""))
        if not skip_pfd:
            result_parts.append("per_frame_data.json para el chart")
        if not result_parts:
            result_parts.append("sin artefactos intermedios — la cadena drop-in usará directamente source.hevc")
        await log_callback(
            "[Fase C] 🎯 Resultado: " + ", ".join(result_parts) + "."
        )


async def _generate_per_frame_data(
    session: CMv40Session,
    rpu_source: Path,
    rpu_target: Path,
    output: Path,
    log_callback=None,
    progress_offset: float = 0.0,
    progress_weight: float = 100.0,
    est_export_s: float = 30.0,
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
                                        progress_offset=progress_offset, progress_weight=half,
                                        est_s=est_export_s)
    await _emit_progress(log_callback, progress_offset + half, "Exportando frames target")
    tgt_data = await _export_rpu_frames(rpu_target, log_callback, label="target",
                                        progress_offset=progress_offset + half, progress_weight=half,
                                        est_s=est_export_s)
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
    est_s: float = 30.0,
) -> list[dict]:
    """
    Exporta datos por frame de un RPU usando `dovi_tool export`.

    Intenta primero export JSON; si no está disponible, hace muestreo cada N frames.
    """
    # Intento 1: dovi_tool export (versión reciente). Estimación basada en fps real.
    try:
        wd = rpu_path.parent
        export_json = wd / f"_export_{label}.json"
        if progress_weight > 0:
            rc, out, err = await _run_with_time_estimate([
                DOVI_TOOL_BIN, "export", "-i", str(rpu_path),
                "-d", f"all={export_json}",
            ], estimated_s=est_s, log_callback=log_callback, timeout=300,
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

    # Regenerar per_frame_data.json usando el RPU corregido como target,
    # para que el chart y la métrica de confianza reflejen la corrección.
    rpu_source = wd / "RPU_source.bin"
    per_frame  = wd / "per_frame_data.json"
    if rpu_source.exists() and rpu_synced.exists():
        if log_callback:
            await log_callback("[Fase E] Regenerando datos per-frame con el target corregido…")
        est_export = max(10.0, _estimate_from_ffmpeg(session, RATIO_EXPORT, FPS_EXPORT))
        await _generate_per_frame_data(
            session, rpu_source, rpu_synced, per_frame, log_callback,
            progress_offset=0.0, progress_weight=100.0, est_export_s=est_export,
        )


# ══════════════════════════════════════════════════════════════════════
#  FASE F — Inyectar RPU en EL
# ══════════════════════════════════════════════════════════════════════

async def run_phase_f_inject(
    session: CMv40Session,
    log_callback=None,
    proc_callback=None,
) -> None:
    """Inyecta el RPU final en el EL.hevc preservando P7 FEL si aplica.

    Estrategia para preservar P7 FEL + añadir CMv4.0 del target (community-standard):

    Si source es P7 FEL y target es P8 (o cualquier no-FEL), NO se inyecta el
    RPU target directamente — eso degradaría el stream a P8.1 (single-layer).
    En su lugar:

      1. Export target RPU → JSON con cmv40_metadata per-frame (L8/L9/L10/L11)
      2. Merge: copiar esos bloques CMv4.0 en el RPU source (P7) preservando
         L1/L2/L5/L6 originales y la estructura P7 FEL
      3. Inyectar el RPU merged → EL_injected.hevc mantiene P7 FEL

    Si target ya es P7 FEL CMv4.0, se inyecta directamente (no hace falta merge).
    """
    wd = get_workdir(session)
    source_hevc  = wd / "source.hevc"
    source_injected = wd / "source_injected.hevc"
    bl_hevc      = wd / "BL.hevc"
    el_hevc      = wd / "EL.hevc"
    rpu_source   = wd / "RPU_source.bin"
    rpu_synced   = wd / "RPU_synced.bin"
    rpu_target   = wd / "RPU_target.bin"
    rpu_merged   = wd / "RPU_merged.bin"
    el_injected  = wd / "EL_injected.hevc"
    bl_injected  = wd / "BL_injected.hevc"

    workflow = session.source_workflow or "p7_fel"
    drop_in_fel = is_drop_in_fel(session)

    # Safety net: compatibilidad estructural source × target. Aborta antes de
    # tocar ffmpeg/dovi_tool si la combinación es imposible (p.ej. source P8
    # single-layer + target P7 FEL drop-in → metadata incoherente).
    compat_ok, compat_msg = _check_source_target_compat(workflow, session.target_type or "")
    if not compat_ok:
        raise RuntimeError(compat_msg)

    # Inputs requeridos según workflow/modo
    if drop_in_fel:
        if not source_hevc.exists():
            raise RuntimeError(
                "source.hevc no existe — ejecuta Fase A primero (drop-in opera sobre BL+EL)"
            )
    elif workflow == "p7_fel":
        if not el_hevc.exists():
            raise RuntimeError("EL.hevc no existe — ejecuta Fase C primero")
    elif workflow == "p7_mel":
        if not bl_hevc.exists():
            raise RuntimeError("BL.hevc no existe — ejecuta Fase C primero")
    elif workflow == "p8":
        if not source_hevc.exists():
            raise RuntimeError("source.hevc no existe — ejecuta Fase A primero")

    # RPU target a usar: synced si el usuario aplicó sync, si no target original
    rpu_target_effective = rpu_synced if rpu_synced.exists() else rpu_target
    if not rpu_target_effective.exists():
        raise RuntimeError("No hay RPU target disponible")

    # Plan de la fase — elige estrategia según workflow y trust
    if log_callback:
        if drop_in_fel:
            await log_callback(
                "[Fase F] 📋 Plan: target P7 FEL CMv4.0 ya cocinado y gates trusted → "
                "ruta DROP-IN. Inyectamos el RPU target directamente en source.hevc "
                "(BL+EL intactos, sin demux previo ni mux posterior). Es la vía más "
                "rápida y limpia — el byte-identical del RPU queda garantizado."
            )
        elif workflow == "p7_fel":
            await log_callback(
                "[Fase F] 📋 Plan: source P7 FEL + target P8.x (retail/generated) → "
                "MERGE clásico. Transferimos L3/L8-L11 del target al RPU P7 del source "
                "preservando la FEL, luego inyectamos el RPU merged en EL.hevc. "
                "Resultado: P7 FEL con trims CMv4.0."
            )
        elif workflow == "p7_mel":
            if session.target_type in ("trusted_p7_fel_final", "trusted_p7_mel_final", "generic"):
                await log_callback(
                    "[Fase F] 📋 Plan: source P7 MEL + target P7/generic → descartamos "
                    "el EL MEL del source y mergeamos los levels CMv4.0 del target "
                    "en el RPU del source preservando profile. Inyectamos el RPU "
                    "merged en BL.hevc. Resultado: MKV single-layer P8.1 CMv4.0."
                )
            else:
                await log_callback(
                    "[Fase F] 📋 Plan: source P7 MEL + target P8 retail → descartamos "
                    "EL MEL e inyectamos el RPU target directamente en BL.hevc. "
                    "Resultado: MKV single-layer P8.1 CMv4.0 — mismo profile, sin merge."
                )
        else:  # p8
            if session.target_type in ("trusted_p7_fel_final", "trusted_p7_mel_final", "generic"):
                await log_callback(
                    "[Fase F] 📋 Plan: source P8.1 + target P7/generic → mergeamos los "
                    "levels CMv4.0 del target (L1-L11) en el RPU P8 del source. El output "
                    "hereda el profile P8.1 del source (no se mezclan capas, solo metadata). "
                    "Inyectamos el RPU merged en source.hevc. Resultado: P8.1 CMv4.0."
                )
            else:
                await log_callback(
                    "[Fase F] 📋 Plan: source P8.1 + target P8 retail → mismo profile, "
                    "inyectamos el RPU target directamente en source.hevc (reemplaza el "
                    "RPU CMv2.9 existente). Resultado: P8.1 con CMv4.0 refinado."
                )

    # ── Determinar qué RPU inyectar y en qué HEVC ────────────────────
    # p7_fel + target NO trusted (o user forzó interactivo):
    #   → merge CMv4.0 en RPU P7 del source (rama A/B de la spec)
    # p7_fel + target trusted_p7_fel_final + trust_ok + auto:
    #   → DROP-IN sobre BL+EL: inject-rpu directo sobre source.hevc (sin demux/mux)
    # p7_mel: descarta EL, usa RPU target directo → inyecta en BL (→ P8 single layer)
    # p8:     usa RPU target directo → inyecta sobre source.hevc (reemplaza RPU)
    if drop_in_fel:
        if log_callback:
            await log_callback(
                "[Fase F] 🚀 DROP-IN BL+EL: target es P7 FEL CMv4.0 ya cocinado y "
                "los gates de trust pasan. inject-rpu directo sobre source.hevc "
                "(sin demux ni mux posterior)."
            )
        rpu_to_inject = rpu_target_effective
        hevc_input    = source_hevc
        hevc_output   = source_injected
        inject_label  = "Inyectando RPU trusted directo sobre BL+EL (drop-in)"
        # Marcar fase omitida para UI/log
        if "merge_cmv40_transfer" not in session.phases_skipped:
            session.phases_skipped.append("merge_cmv40_transfer")
    elif workflow == "p7_fel":
        # Merge preservando FEL (flujo clásico)
        if log_callback:
            await log_callback(
                "[Fase F] P7 FEL: merge CMv4.0 del target en RPU P7 del source…"
            )
        await _merge_cmv40_into_p7(
            rpu_source_p7=rpu_source,
            rpu_target_v40=rpu_target_effective,
            output=rpu_merged,
            log_callback=log_callback,
        )
        rpu_to_inject = rpu_merged
        hevc_input    = el_hevc
        hevc_output   = el_injected
        inject_label  = "Inyectando RPU merged en EL (preserva FEL)"
    elif workflow == "p7_mel":
        # P7 MEL: tras demux quedamos con BL single-layer (EL MEL descartada).
        # Si el target es P8 (trusted_p8_source): inject directo — el P8 RPU
        # encaja sobre el BL y produce un HEVC P8.1 CMv4.0.
        # Si el target es P7 (fel o mel) o generic: mergeamos los levels CMv4.0
        # del target en el RPU P7 MEL del source — el output hereda la profile
        # P7 MEL pero al inyectarlo en BL single-layer el reproductor lo lee
        # como P8.1-equivalente con CMv4.0 completo.
        target_needs_merge = session.target_type in (
            "trusted_p7_fel_final", "trusted_p7_mel_final", "generic",
        )
        if target_needs_merge:
            if log_callback:
                await log_callback(
                    f"[Fase F] P7 MEL + target {session.target_type}: merge CMv4.0 "
                    f"del target en RPU P7 MEL del source (dovi_tool allow_cmv4_transfer)…"
                )
            await _merge_cmv40_into_p7(
                rpu_source_p7=rpu_source,
                rpu_target_v40=rpu_target_effective,
                output=rpu_merged,
                log_callback=log_callback,
            )
            rpu_to_inject = rpu_merged
            inject_label  = "Inyectando RPU merged en BL (MEL descartado → P8.1 CMv4.0)"
        else:
            rpu_to_inject = rpu_target_effective
            inject_label  = "Inyectando RPU target en BL (MEL descartado → P8.1)"
            if log_callback:
                await log_callback(
                    "[Fase F] P7 MEL + target P8 retail: inject directo sobre BL → P8.1 CMv4.0"
                )
        hevc_input    = bl_hevc
        hevc_output   = bl_injected
    else:  # workflow == "p8"
        # P8 source single-layer: si el target es P8 CMv4.0 (mismo profile),
        # inject directo. Si el target es P7 (fel o mel) o generic, mergeamos
        # los levels CMv4.0 del target en el RPU P8 del source — el output
        # hereda el profile P8.1 del source con los levels CMv4.0 copiados
        # del target. Evita que un RPU P7 dual-layer acabe inyectado en un
        # HEVC single-layer con metadata incoherente.
        target_needs_merge = session.target_type in (
            "trusted_p7_fel_final", "trusted_p7_mel_final", "generic",
        )
        if target_needs_merge:
            if log_callback:
                await log_callback(
                    f"[Fase F] P8 + target {session.target_type}: merge CMv4.0 "
                    f"del target en RPU P8 del source (dovi_tool allow_cmv4_transfer)…"
                )
            await _merge_cmv40_into_p7(
                rpu_source_p7=rpu_source,
                rpu_target_v40=rpu_target_effective,
                output=rpu_merged,
                log_callback=log_callback,
            )
            rpu_to_inject = rpu_merged
            inject_label  = "Inyectando RPU merged en source.hevc (P8.1 CMv4.0)"
        else:
            rpu_to_inject = rpu_target_effective
            inject_label  = "Inyectando RPU target en source.hevc (P8 → P8.1 CMv4.0)"
            if log_callback:
                await log_callback(
                    "[Fase F] P8 + target P8 retail: inject directo sobre source HEVC"
                )
        hevc_input    = source_hevc
        hevc_output   = bl_injected  # reutilizamos el slot de artefacto

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
            f"[Fase F] {inject_label} (RPU: {rpu_to_inject.name}, {rpu_frames} frames)…"
        )
    est_inject = _estimate_from_ffmpeg(session, RATIO_INJECT, FPS_INJECT)
    await _emit_progress(log_callback, 0, inject_label)
    rc = await _run_streaming([
        DOVI_TOOL_BIN, "inject-rpu",
        "-i", str(hevc_input),
        "--rpu-in", str(rpu_to_inject),
        "-o", str(hevc_output),
    ], log_callback=log_callback, proc_callback=proc_callback,
       progress_ctx={
           "time_estimate_s": est_inject,
           "offset": 0.0, "weight": 100.0,
           "label": inject_label,
       })
    if rc != 0:
        raise RuntimeError(f"dovi_tool inject-rpu falló (código {rc})")

    await _emit_progress(log_callback, 100, "RPU inyectado")
    if log_callback:
        await log_callback(f"[Fase F] ✓ HEVC con RPU inyectado generado: {hevc_output.name} (workflow {workflow})")
        # Resultado con implicación para Fase G
        if drop_in_fel:
            impl = ("El HEVC conserva BL+EL intactos — Fase G puede saltarse el "
                    "dovi_tool mux y usar mkvmerge directamente.")
        elif workflow == "p7_fel":
            impl = ("Mantenemos la FEL original — Fase G combinará BL.hevc + "
                    "EL_injected.hevc con dovi_tool mux y luego mkvmerge añadirá "
                    "audio/subs/capítulos.")
        elif workflow == "p7_mel":
            impl = ("MKV final será single-layer P8.1 — Fase G solo hará mkvmerge, "
                    "no necesita mux dual-layer (más rápido y archivo más pequeño).")
        else:  # p8
            impl = ("Source ya era single-layer — Fase G hará mkvmerge directo.")
        await log_callback(f"[Fase F] 🎯 Resultado: RPU CMv4.0 integrado en el stream. {impl}")


async def _merge_cmv40_into_p7(
    rpu_source_p7: Path,
    rpu_target_v40: Path,
    output: Path,
    log_callback=None,
) -> None:
    """Transfiere los niveles CMv4.0 del RPU streaming al RPU P7 del BD preservando FEL.

    Usa la primitiva nativa de dovi_tool ``allow_cmv4_transfer`` que transfiere
    los niveles especificados frame-a-frame desde ``source_rpu`` hacia el RPU
    input del editor. L254 se añade implícitamente con valor default.

    Config JSON:
        {
          "source_rpu": "/abs/path/RPU_target_v40.bin",
          "rpu_levels": [3, 8, 9, 10, 11],
          "allow_cmv4_transfer": true
        }

    Resultado:
      - Estructura P7 FEL preservada (header, mapping, BL/EL info del BD)
      - L1/L2/L5/L6 originales del BD preservados (CMv2.9)
      - L3/L8/L9/L10/L11 transferidos frame-a-frame desde el streaming CMv4.0
      - L254 añadido implícitamente por dovi_tool (marca el CMv4.0)
    """
    # ── Pre-check: frame count debe coincidir ────────────────────────
    rc_s, sum_s, _ = await _run([
        DOVI_TOOL_BIN, "info", "-s", "-i", str(rpu_source_p7),
    ], timeout=30)
    rc_t, sum_t, _ = await _run([
        DOVI_TOOL_BIN, "info", "-s", "-i", str(rpu_target_v40),
    ], timeout=30)
    frames_bd = _parse_dovi_summary(sum_s).frame_count if rc_s == 0 else 0
    frames_tgt = _parse_dovi_summary(sum_t).frame_count if rc_t == 0 else 0
    if frames_bd == 0 or frames_tgt == 0:
        raise RuntimeError("No se pudo leer frame count de uno de los RPUs con dovi_tool info")
    if frames_bd != frames_tgt:
        raise RuntimeError(
            f"Frame count mismatch ANTES del merge CMv4.0:\n"
            f"  RPU BD (source P7):    {frames_bd} frames\n"
            f"  RPU target (streaming): {frames_tgt} frames\n"
            f"Diferencia: {frames_tgt - frames_bd:+d}\n\n"
            f"→ Vuelve a la Fase D («Verificar sincronización») y aplica la corrección "
            f"(remove/duplicate) hasta que Δ = 0. Después reanuda la inyección."
        )
    if log_callback:
        await log_callback(
            f"[Fase F] Frame counts OK: BD={frames_bd}, target={frames_tgt} (match)"
        )

    # ── Merge según bbeny123/remuxer (PR #351 en dovi_tool 2.3.0+) ──
    # Config exactamente como la genera remuxer.sh:
    #   {"allow_cmv4_transfer": true, "source_rpu": "...", "rpu_levels": [...]}
    # Levels para FEL según remuxer.sh línea 2090: 1,2,3,6,8,9,10,11,254
    # L254 (no L255) — L254 es el Dolby Vision Metadata Version marker.
    wd = rpu_source_p7.parent
    cfg_path = wd / "_merge_cmv4_transfer.json"
    cfg = {
        "allow_cmv4_transfer": True,
        "source_rpu": str(rpu_target_v40.resolve()),
        "rpu_levels": [1, 2, 3, 6, 8, 9, 10, 11, 254],
    }
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    if log_callback:
        await log_callback(
            "[Fase F] Transferencia CMv4.0 [1,2,3,6,8,9,10,11,254] frame-a-frame "
            f"desde {rpu_target_v40.name} → RPU P7 del BD "
            "(allow_cmv4_transfer=true, según remuxer.sh de bbeny123)…"
        )

    try:
        rc, out, err = await _run([
            DOVI_TOOL_BIN, "editor",
            "-i", str(rpu_source_p7),
            "-j", str(cfg_path),
            "-o", str(output),
        ], log_callback=log_callback, timeout=300)
    finally:
        cfg_path.unlink(missing_ok=True)

    if rc != 0:
        err_lc = err.lower()
        if "same length" in err_lc or "mismatch" in err_lc:
            raise RuntimeError(
                f"Frame count mismatch durante el merge: {err[:200]}\n"
                f"→ Ir a Fase D para re-sincronizar."
            )
        raise RuntimeError(
            f"dovi_tool editor (cmv4 transfer) falló:\n{err[:500]}"
        )

    # ── Verificación post-merge ──────────────────────────────────────
    rc, summary, _ = await _run([
        DOVI_TOOL_BIN, "info", "-s", "-i", str(output),
    ], timeout=30)
    if rc != 0:
        raise RuntimeError("No se pudo leer el RPU merged con dovi_tool info")
    result_info = _parse_dovi_summary(summary)

    # Checkpoints esperados: FEL preservado, CM v4.0, mismo frame count
    errors: list[str] = []
    if result_info.el_type != "FEL":
        errors.append(
            f"el_type={result_info.el_type!r} (esperado 'FEL' — se perdió la estructura dual-layer)"
        )
    if result_info.cm_version != "v4.0":
        errors.append(
            f"cm_version={result_info.cm_version!r} (esperado 'v4.0' — la transferencia no se aplicó)"
        )
    if result_info.frame_count != frames_bd:
        errors.append(
            f"frame_count={result_info.frame_count} (esperado {frames_bd} — frames perdidos/añadidos)"
        )
    # Comprobar que L8 está presente: parseamos el summary textual
    # (dovi_tool info -s lista "L8 trims: ..." si existen bloques L8)
    has_l8 = "l8" in summary.lower() or "level 8" in summary.lower()
    if not has_l8:
        errors.append("no se detectan bloques L8 en el RPU merged (L8 trims ausentes)")

    if errors:
        raise RuntimeError(
            "Verificación post-merge falló. El RPU resultante no es P7 FEL CMv4.0 válido:\n  - "
            + "\n  - ".join(errors)
            + "\n\nSe aborta la inyección para no generar un MKV incorrecto."
        )

    if log_callback:
        await log_callback(
            f"[Fase F] ✓ Merge verificado: Profile {result_info.profile} ({result_info.el_type}), "
            f"CM {result_info.cm_version}, {result_info.frame_count} frames, L8 presente"
        )


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

    Optimización: mkvmerge escribe DIRECTAMENTE al destino final de /mnt/output
    con sufijo ``.mkv.tmp``. Fase H valida y hace ``os.rename`` (atómico dentro
    del mismo filesystem) — evita copiar 42GB entre ZFS datasets.

    Devuelve la ruta del MKV final provisional (``{name}.mkv.tmp`` en /mnt/output).
    """
    wd = get_workdir(session)
    bl_hevc         = wd / "BL.hevc"
    el_injected     = wd / "EL_injected.hevc"
    bl_injected     = wd / "BL_injected.hevc"
    source_injected = wd / "source_injected.hevc"
    dv_dual         = wd / "DV_dual.hevc"

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_mkv = OUTPUT_DIR / f"{session.output_mkv_name}.tmp"
    if output_mkv.exists():
        try: output_mkv.unlink()
        except Exception: pass

    workflow = session.source_workflow or "p7_fel"
    drop_in_fel = is_drop_in_fel(session)
    frames = session.source_frame_count or 0
    est_mkv = frames / FPS_MKVMERGE if frames > 0 else 360.0

    # Plan de la fase
    if log_callback:
        if drop_in_fel:
            await log_callback(
                "[Fase G] 📋 Plan: ensamblar el MKV final. source_injected.hevc ya es "
                "BL+EL dual-layer con el RPU CMv4.0 inyectado (drop-in) — solo "
                "necesitamos mkvmerge para añadir audio/subs/capítulos del origen. "
                "Saltamos el dovi_tool mux (innecesario, el stream ya está íntegro)."
            )
        elif workflow == "p7_fel":
            await log_callback(
                "[Fase G] 📋 Plan: ensamblar el MKV final. Workflow P7 FEL con merge "
                "— primero dovi_tool mux combina BL.hevc + EL_injected.hevc en un "
                "HEVC dual-layer, luego mkvmerge añade audio/subs/capítulos del origen."
            )
        elif workflow == "p7_mel":
            await log_callback(
                "[Fase G] 📋 Plan: ensamblar el MKV final single-layer. El EL MEL "
                "se descarta (no aporta) → mkvmerge directo sobre BL_injected.hevc "
                "con audio/subs/capítulos del origen. Resultado: P8.1 CMv4.0 ligero."
            )
        else:  # p8
            await log_callback(
                "[Fase G] 📋 Plan: ensamblar el MKV final. Source era P8.1 single-layer "
                "→ mkvmerge directo sobre source_injected.hevc con audio/subs/"
                "capítulos del origen."
            )

    # ── Determinar qué HEVC multiplexar según workflow/modo ──────────
    if drop_in_fel:
        # Drop-in: source_injected.hevc ya es BL+EL con el RPU CMv4.0 inyectado.
        # No se ejecuta dovi_tool mux — el stream ya es dual-layer íntegro.
        if not source_injected.exists():
            raise RuntimeError(
                "source_injected.hevc no existe — ejecuta Fase F primero (drop-in FEL)"
            )
        if log_callback:
            await log_callback(
                "[Fase G] ┌─ Ruta drop-in: mkvmerge directo sobre source_injected.hevc "
                "(sin dovi_tool mux, el BL+EL ya está intacto)"
            )
        hevc_for_mkv = source_injected
        remux_offset = 0.0
        remux_weight = 100.0
    elif workflow == "p7_fel":
        # Dual-layer clásico: primero mux BL + EL_injected, luego mkvmerge
        if not bl_hevc.exists() or not el_injected.exists():
            raise RuntimeError("BL.hevc o EL_injected.hevc no existen")

        W_MUX, W_MKV = 38.0, 62.0
        est_mux = _estimate_from_ffmpeg(session, RATIO_MUX, FPS_MUX)

        await _emit_progress(log_callback, 0, "Combinando BL + EL_injected (P7 FEL)")
        if log_callback:
            await log_callback("[Fase G] P7 FEL: mux dual-layer (dovi_tool mux)…")
        rc = await _run_streaming([
            DOVI_TOOL_BIN, "mux",
            "--bl", str(bl_hevc),
            "--el", str(el_injected),
            "-o", str(dv_dual),
        ], log_callback=log_callback, proc_callback=proc_callback,
           progress_ctx={
               "time_estimate_s": est_mux,
               "offset": 0.0, "weight": W_MUX,
               "label": "Combinando BL + EL (dovi_tool mux)",
           })
        if rc != 0:
            raise RuntimeError(f"dovi_tool mux falló (código {rc})")
        await _emit_progress(log_callback, W_MUX, "Dual-layer HEVC generado")
        hevc_for_mkv = dv_dual
        remux_offset = W_MUX
        remux_weight = W_MKV
    else:
        # p7_mel y p8: single-layer, sin dovi_tool mux. BL_injected.hevc es el stream final.
        if not bl_injected.exists():
            raise RuntimeError(
                f"BL_injected.hevc no existe para workflow {workflow} — ejecuta Fase F primero"
            )
        if log_callback:
            label = "P7 MEL (EL descartado)" if workflow == "p7_mel" else "P8 (sin demux)"
            await log_callback(f"[Fase G] {label}: sin mux dual-layer, mkvmerge directo…")
        hevc_for_mkv = bl_injected
        remux_offset = 0.0
        remux_weight = 100.0

    # mkvmerge: MKV final con audio/subs/capítulos del origen (progreso real).
    # --track-name deja una huella visible del procesado (visible en cualquier
    # inspector MKV / mediainfo) sin depender de session.json externo.
    if log_callback:
        await log_callback("[Fase G] Remuxando a MKV final (mkvmerge)…")
    title = session.output_mkv_name.removesuffix(".mkv")
    if drop_in_fel or workflow == "p7_fel":
        video_track_name = "HEVC DV P7 FEL CMv4.0"
    elif workflow == "p7_mel":
        video_track_name = "HEVC DV P8.1 CMv4.0 (from P7 MEL)"
    else:
        video_track_name = "HEVC DV P8.1 CMv4.0"
    rc = await _run_streaming([
        MKVMERGE_BIN, "--gui-mode", "-o", str(output_mkv),
        "--title", title,
        "--track-name", f"0:{video_track_name}",
        str(hevc_for_mkv),
        "--no-video", session.source_mkv_path,
    ], log_callback=log_callback, proc_callback=proc_callback,
       progress_ctx={
           "time_estimate_s": est_mkv,
           "offset": remux_offset, "weight": remux_weight,
           "label": "Remuxando MKV final (mkvmerge)",
       })
    if rc not in (0, 1):
        raise RuntimeError(f"mkvmerge falló (código {rc})")
    await _emit_progress(log_callback, 100, "Remux completado")

    # Cleanup intermedio: NO se borra aquí el pre-mux HEVC (source_injected /
    # dv_dual). Fase H los necesita para `extract-rpu` como alternativa al
    # MKV (dovi_tool 2.3.x falla con "Invalid PPS index" al parsear ciertos
    # MKVs). El unlink se pospone al final de Fase H tras validar. Los
    # ~45 GB extra durante el ventana G→H no son un problema: TMP tiene
    # margen holgado (preflight lo comprobó).

    if log_callback:
        size_gb = output_mkv.stat().st_size / 1e9
        await log_callback(
            f"[Fase G] ✓ MKV ensamblado: {output_mkv.name} ({size_gb:.2f} GB, workflow {workflow})"
        )
        await log_callback(
            "[Fase G] 🎯 Resultado: MKV completo escrito con sufijo .tmp. "
            "Fase H validará que el RPU del resultante es CMv4.0 antes de "
            "renombrar al nombre final (rename atómico)."
        )
    return str(output_mkv)


# ══════════════════════════════════════════════════════════════════════
#  FASE H — Validación final
# ══════════════════════════════════════════════════════════════════════

async def run_phase_h_validate(
    session: CMv40Session,
    log_callback=None,
    proc_callback=None,
) -> dict:
    """
    Valida que el MKV resultante tiene DV CMv4.0 correctamente.

    Si OK, mueve el MKV a /mnt/output/. Devuelve info de validación.
    """
    wd = get_workdir(session)
    # El MKV final provisional vive en /mnt/output/{name}.mkv.tmp (Fase G lo
    # escribió directamente allí). Si no existe, fallback al path antiguo
    # (workdir/output.mkv) para compatibilidad con proyectos viejos.
    output_mkv_tmp = OUTPUT_DIR / f"{session.output_mkv_name}.tmp"
    output_mkv_legacy = wd / "output.mkv"
    if output_mkv_tmp.exists():
        output_mkv = output_mkv_tmp
    elif output_mkv_legacy.exists():
        output_mkv = output_mkv_legacy
    else:
        raise RuntimeError(
            f"MKV final no existe: ni {output_mkv_tmp} ni {output_mkv_legacy} — ejecuta Fase G primero"
        )

    if log_callback:
        mkv_gb = output_mkv.stat().st_size / 1e9
        await log_callback(
            "[Fase H] 📋 Plan: validar el resultado antes de mover el MKV al output "
            "final. Leemos el RPU del HEVC resultante, confirmamos que tiene CMv4.0 "
            "y que el frame count coincide con el source. Si todo OK, rename atómico "
            ".tmp → .mkv (instantáneo, mismo filesystem) y cleanup de artefactos "
            "intermedios."
        )
        await log_callback(f"[Fase H] ┌─ Validando DV del MKV resultante ({mkv_gb:.1f} GB)…")

    # Validación completa: extract-rpu DIRECTO sobre el MKV final (escanea todo
    # el stream HEVC, ~segundos-minutos según tamaño, mucho más barato que
    # ffmpeg + re-extract). Cubre desincronización intermedia que una muestra
    # de 30s no detectaría. Luego:
    #   1. Parseamos el RPU con dovi_tool info (frame count, CM, profile, el_type)
    #   2. Comparamos con RPU_target.bin (byte-idéntico en drop-in puro)
    drop_in_fel = is_drop_in_fel(session)
    rpu_target = wd / "RPU_target.bin"
    temp_rpu   = wd / "_validate_rpu.bin"
    # Preferimos extraer el RPU del HEVC pre-mux que está en workdir, no del
    # MKV final. dovi_tool 2.3.x falla con "Invalid PPS index" al parsear
    # MKVs donde mkvmerge guardó el PPS en CodecPrivate en vez de inline —
    # problema del parser matroska de dovi_tool, no del stream. El stream
    # HEVC que muxó mkvmerge es byte-idéntico al pre-mux (mkvmerge no
    # re-encoda el vídeo), así que extraer del pre-mux es equivalente y
    # evita el bug.
    pre_mux_candidates = [
        wd / "source_injected.hevc",   # drop-in FEL
        wd / "DV_dual.hevc",           # workflow p7_fel dual-layer
        wd / "EL_injected.hevc",       # fallback
        wd / "BL_injected.hevc",       # workflow p7_mel (single-layer)
    ]
    pre_mux_hevc = next((p for p in pre_mux_candidates if p.exists()), None)
    extract_input = str(pre_mux_hevc) if pre_mux_hevc else str(output_mkv)
    try:
        if log_callback:
            msg_source = (f"del HEVC pre-mux ({pre_mux_hevc.name})"
                          if pre_mux_hevc else "del MKV final")
            await log_callback(
                f"[Fase H] Paso 1/3: extrayendo RPU completo {msg_source} "
                "(escanea todo el stream, no solo 30s)…"
            )
        await _emit_progress(log_callback, 5, "Extrayendo RPU completo")
        rc, _, err = await _run([
            DOVI_TOOL_BIN, "extract-rpu", extract_input, "-o", str(temp_rpu),
        ], timeout=900)
        if rc != 0:
            raise RuntimeError(f"extract-rpu falló sobre {extract_input}: {err[:200]}")

        if log_callback:
            await log_callback("[Fase H] Paso 2/3: analizando metadata DV del RPU…")
        await _emit_progress(log_callback, 30, "Analizando metadata DV")
        rc, summary, err = await _run([DOVI_TOOL_BIN, "info", "--summary", str(temp_rpu)], timeout=30)
        if rc != 0:
            raise RuntimeError(f"dovi_tool info falló: {err[:200]}")
        result_info = _parse_dovi_summary(summary)
        if log_callback:
            await log_callback(
                f"[Fase H] DV detectado: Profile {result_info.profile} ({result_info.el_type}), "
                f"CM {result_info.cm_version}, {result_info.frame_count} frames"
            )

        # ── Frame count completo (no solo una muestra) ─────────────────
        expected_frames = session.target_frame_count or session.source_frame_count or 0
        if expected_frames and result_info.frame_count and result_info.frame_count != expected_frames:
            raise RuntimeError(
                f"Frame count del MKV final ({result_info.frame_count}) distinto "
                f"del esperado ({expected_frames}) — indica corrupción o desincronización."
            )

        # ── Comparación byte-idéntica con RPU_target (solo drop-in) ────
        # En drop-in puro el RPU del output DEBE ser idéntico al bin de REC_9999
        # porque literalmente lo inyectamos sin tocarlo. Cualquier divergencia
        # indica un problema en el inject-rpu.
        rpu_identical = None
        if drop_in_fel and rpu_target.exists():
            try:
                import filecmp
                rpu_identical = await asyncio.to_thread(
                    filecmp.cmp, str(temp_rpu), str(rpu_target), False
                )
            except Exception as e:
                _logger.warning("filecmp falló: %s", e)
                rpu_identical = None
            if rpu_identical is True:
                if log_callback:
                    await log_callback(
                        "[Fase H] ✓ RPU del MKV byte-idéntico a RPU_target.bin "
                        "(drop-in preservó el bin sin modificaciones)"
                    )
            elif rpu_identical is False:
                raise RuntimeError(
                    "RPU del MKV final DIFIERE de RPU_target.bin. En drop-in puro "
                    "deberían ser byte-idénticos — algo en inject-rpu alteró el RPU."
                )
    finally:
        temp_rpu.unlink(missing_ok=True)

    if result_info.cm_version != "v4.0":
        raise RuntimeError(
            f"El MKV resultante tiene CM {result_info.cm_version} (esperado v4.0)"
        )

    # Validación de subprofile según workflow
    expected_el = "FEL" if (session.source_workflow or "p7_fel") == "p7_fel" else None
    if expected_el and result_info.el_type != expected_el:
        raise RuntimeError(
            f"El MKV resultante tiene el_type={result_info.el_type!r} "
            f"(esperado '{expected_el}' porque source_workflow='{session.source_workflow}')"
        )
    if log_callback:
        await log_callback(
            f"[Fase H] ✓ Validación DV OK: Profile {result_info.profile} ({result_info.el_type}), "
            f"CM {result_info.cm_version}, {result_info.frame_count} frames"
        )

    # Validar pistas con mkvmerge -J
    if log_callback:
        await log_callback("[Fase H] Paso 3/3: validando pistas (mkvmerge -J)…")
    await _emit_progress(log_callback, 50, "Validando pistas (mkvmerge -J)")
    rc, out, err = await _run([MKVMERGE_BIN, "-J", str(output_mkv)], timeout=60)
    if rc not in (0, 1):
        raise RuntimeError(f"mkvmerge -J falló sobre MKV final: {err[:200]}")

    # Renombrar .tmp → .mkv (atómico si mkvmerge escribió ya en /mnt/output,
    # fallback a move con monitor de progreso si viene de workdir legacy).
    final_path = OUTPUT_DIR / session.output_mkv_name
    if final_path.exists():
        raise RuntimeError(f"Ya existe un MKV con ese nombre: {session.output_mkv_name}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Intento 1: os.rename (instantáneo si mismo filesystem — caso normal
    # porque Fase G escribe directamente a /mnt/output)
    same_fs_rename_ok = False
    try:
        os.rename(str(output_mkv), str(final_path))
        same_fs_rename_ok = True
        if log_callback:
            await log_callback("[Fase H] Rename atómico .tmp → .mkv (instantáneo, mismo filesystem)")
        await _emit_progress(log_callback, 95, "Renombrado a nombre final")
    except OSError:
        # Distintos filesystems (legacy workdir→output): fallback a copy+delete
        pass

    if not same_fs_rename_ok:
        total_bytes = output_mkv.stat().st_size
        if log_callback:
            await log_callback(
                f"[Fase H] Rename cross-fs: copiando {total_bytes / 1e9:.1f} GB a /mnt/output "
                f"(fallback lento — considera poner TMP_PATH y OUTPUT_PATH en el mismo dataset)…"
            )

        stop_mon = asyncio.Event()
        start_mon = time.monotonic()

        async def _monitor_move():
            while not stop_mon.is_set():
                try:
                    if final_path.exists():
                        cur = final_path.stat().st_size
                        pct = min(99.0, (cur / total_bytes) * 100.0)
                        phase_pct = 55.0 + pct * 0.4
                        elapsed = time.monotonic() - start_mon
                        eta = (elapsed / pct * (100 - pct)) if pct > 1 else None
                        await _emit_progress(
                            log_callback, phase_pct,
                            f"Copiando a /mnt/output ({cur / 1e9:.1f}/{total_bytes / 1e9:.1f} GB)",
                            int(eta) if eta else 0,
                        )
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop_mon.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

        mon_task = asyncio.create_task(_monitor_move())
        try:
            await asyncio.to_thread(shutil.move, str(output_mkv), str(final_path))
        finally:
            stop_mon.set()
            try:
                await mon_task
            except Exception:
                pass

    session.output_mkv_path = str(final_path)
    await _emit_progress(log_callback, 100, "Validación completada")

    # Cleanup DIFERIDO del pre-mux HEVC: tras validación exitosa ya no los
    # necesitamos. Antes se borraban al final de Fase G, pero Fase H los
    # requería como input alternativo a extract-rpu sobre el MKV final
    # (evita "Invalid PPS index" de dovi_tool 2.3.x con ciertos MKVs).
    for hevc_name in ("source_injected.hevc", "DV_dual.hevc",
                      "EL_injected.hevc", "BL_injected.hevc"):
        (wd / hevc_name).unlink(missing_ok=True)

    if log_callback:
        await log_callback(
            f"[Fase H] ✓ MKV validado y movido a ubicación final: {final_path}"
        )
        await log_callback(
            f"[Fase H] 🎯 Resultado: upgrade CMv4.0 completado con éxito — "
            f"Profile {result_info.profile}{' ' + result_info.el_type if result_info.el_type else ''}, "
            f"CM {result_info.cm_version}, {result_info.frame_count} frames. "
            f"El fichero está listo para reproducir en cualquier cadena DV compatible."
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
