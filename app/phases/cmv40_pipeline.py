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
    # 'remuxed' deliberadamente AUSENTE de esta lista. El artefacto tras
    # Fase G es el `.mkv.tmp` en /mnt/output (NO en el workdir). Antes
    # listábamos 'output.mkv' en workdir como path legacy, pero hace
    # tiempo que el remux escribe directo a /mnt/output, así que ese
    # fichero NUNCA existe en workdir → la validación retrocedía
    # spuriously a 'injected', luego el forward-roll del GET la subía
    # de nuevo a 'remuxed' → flicker de mensajes contradictorios y
    # potencial re-mux innecesario si autoContinue=true.
    # La verificación real de la integridad post-Fase G la hace el
    # auto-rewind/forward-roll en `cmv40_get` (chequea .mkv.tmp en
    # /mnt/output).
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
        # NO revertir done → remuxed automáticamente: mover/renombrar el MKV
        # final tras done es una accion legitima del usuario (ej. archivar a
        # biblioteca permanente). Si ademas autoContinue sigue activo, la
        # reversion disparaba auto-pipeline → re-ejecucion de validate, que
        # fallaba porque los artefactos intermedios (BL.hevc, EL_injected.hevc)
        # ya los habia borrado el housekeeping de Fase H.
        # Solo informamos: el proyecto sigue como done.
        if not session.output_mkv_path or not Path(session.output_mkv_path).exists():
            result["missing"] = [session.output_mkv_path or "output.mkv"]
            result["message"] = (
                "El MKV final ya no esta en su ubicacion original — probablemente "
                "lo moviste a tu biblioteca. El proyecto sigue completo."
            )
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
    # start_new_session=True para que cancel pueda hacer killpg si el
    # proc no responde a SIGTERM/SIGKILL directo (ver cmv40_cancel).
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
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


def _adaptive_timeout(estimated_s: float, floor_s: int = 1800) -> int:
    """Timeout generoso derivado de la estimación wall-time (3× con suelo).

    Un timeout FIJO no escala con la duración de la peli y mata operaciones
    largas a mitad de escritura. Caso real (Proyecto Salvación, 2h36m UHD):
    el ``dovi_tool demux`` estimaba ~990s (ffmpeg 761s × RATIO_DEMUX 1.30)
    pero el tope era fijo a 900s → timeout garantizado → BL.hevc truncado
    (171679 de 225177 frames) reutilizado por el reintento → MKV corrupto
    (lo cazó Fase H). Escalar con la estimación —que ya se adapta a la carga
    del NAS vía ffmpeg_wall_seconds— da holgura proporcional al tamaño real.
    El suelo cubre estimaciones minúsculas (sesiones sin ancla de ffmpeg).
    """
    est = estimated_s if estimated_s and estimated_s > 0 else 0.0
    return max(floor_s, int(est * 3))


def _demux_output_reusable(bl_hevc: Path, el_hevc: Path, marker: Path) -> bool:
    """True solo si el demux previo terminó por completo y es seguro reutilizarlo.

    No basta con que existan BL/EL: un demux muerto a mitad (timeout, kill -9,
    reinicio del server) deja ficheros truncados en disco. El marcador se
    escribe SOLO tras un demux con rc=0, así que su presencia es la señal
    fiable de completitud. Sin marcador → hay que rehacer el demux.
    """
    return bl_hevc.exists() and el_hevc.exists() and marker.exists()


class _ReadProgress:
    """Progreso REAL de un subproceso, leyendo cuánto lleva leído de su entrada.

    Las operaciones de dovi_tool (`extract-rpu`, `demux`, `inject-rpu`, `mux`)
    no informan de nada por stdout, así que hasta ahora la barra se calculaba
    como `elapsed / estimación`. Y aunque la estimación esté bien calibrada de
    media —medido sobre 87 jobs: RATIO_INJECT 1,77 hardcodeado vs 1,83 real—
    la dispersión entre jobs concretos es de 1,31 a 2,32. Es decir, ninguna
    constante puede predecir un job: la barra mentía y se quedaba clavada en
    el tope del 95 %.

    El kernel sí sabe la verdad: `/proc/<pid>/fdinfo/<fd>` lleva la posición
    de lectura del descriptor. Comparada con el tamaño del fichero da el
    porcentaje exacto, sin estimar nada.

    Verificado contra `dovi_tool extract-rpu` sobre un MKV de 50 GB: la
    posición avanza de forma monotónica y continua. (El tamaño del fichero de
    SALIDA no vale como señal: extract-rpu escribe el RPU entero al cerrar y
    se queda a 0 bytes durante toda la ejecución.)

    Si no se puede leer —otro kernel, entrada por pipe, lectura con mmap— el
    caller se queda con la estimación por reloj de siempre.
    """

    def __init__(self, pid: int, input_path: Path,
                 output_path: Path | None = None, expected_out: int = 0,
                 expected_read: int = 0):
        self.pid = pid
        self.input_path = str(input_path)
        # Señal preferente cuando sabemos cuántos bytes va a leer EN TOTAL el
        # proceso, sumando todas sus pasadas: `rchar` de /proc solo crece, así
        # que atraviesa el cambio de pasada sin saltos ni topes.
        #
        # `inject-rpu` lo necesita: hace dos pasadas y las señales de cada una
        # son distintas (posición de lectura en la primera, fichero de salida
        # en la segunda). Al mezclarlas con el tope monotónico, la primera
        # pasada dejaba `_last_pct` en 100 % y la segunda quedaba clavada ahí
        # —sin barra y sin ETA— durante el 84 % de la fase. Medido en John
        # Wick 4: pasada 1 = 124 s, pasada 2 = 683 s, total 815 s.
        self.expected_read = expected_read
        # Cuando el proceso escribe un fichero grande cuyo tamaño final
        # sabemos, ESE es mejor indicador que la posición de lectura, porque
        # crece monotónicamente por naturaleza. `inject-rpu` obliga a ello:
        # recorre la entrada DOS veces (una para el orden de frames y otra
        # reescribiendo) rebobinando el mismo descriptor, así que la posición
        # de lectura vuelve a cero a mitad de faena. Verificado en el NAS:
        #   2s  [fd3 out=0MB]   [fd4 in=1332MB]   ← pasada 1
        #   4s  [fd3 out=323MB] [fd4 in=324MB]    ← pasada 2, in reinicia
        self.output_path = str(output_path) if output_path else None
        self.expected_out = expected_out
        self.total = 0
        try:
            self.total = os.path.getsize(self.input_path)
        except OSError:
            pass
        self._fdinfo: str | None = None
        self._last_pct = 0.0
        # (monotonic, pct) para calcular el ritmo real — ver eta()
        self._samples: list[tuple[float, float]] = []

    def _find_fd(self) -> str | None:
        """Localiza el descriptor que apunta a la entrada. Se cachea: el fd no
        cambia durante la vida del proceso."""
        if self._fdinfo:
            return self._fdinfo
        try:
            fd_dir = f"/proc/{self.pid}/fd"
            for name in os.listdir(fd_dir):
                try:
                    if os.readlink(os.path.join(fd_dir, name)) == self.input_path:
                        self._fdinfo = f"/proc/{self.pid}/fdinfo/{name}"
                        return self._fdinfo
                except OSError:
                    continue
        except OSError:
            pass
        return None

    def _anotar(self, pct: float) -> float:
        pct = max(0.0, min(100.0, pct))
        pct = max(pct, self._last_pct)     # nunca retroceder delante del usuario
        self._last_pct = pct
        self._samples.append((time.monotonic(), pct))
        del self._samples[:-12]            # ventana de ~20s a 1,5s por muestra
        return pct

    def sample(self) -> float | None:
        """Porcentaje de avance (0-100), o None si no hay señal disponible."""
        # Preferente: bytes leídos en total, si sabemos cuántos van a ser.
        # Vale para procesos de varias pasadas, donde ninguna otra señal es
        # continua de principio a fin.
        if self.expected_read > 0:
            leido = _proc_rchar(self.pid)
            if leido is not None and leido > 0:
                return self._anotar(leido * 100.0 / self.expected_read)
        # Después: lo que lleva escrito, si sabemos cuánto va a escribir.
        if self.output_path and self.expected_out > 0:
            try:
                escrito = os.path.getsize(self.output_path)
            except OSError:
                escrito = 0
            if escrito > 0:
                return self._anotar(escrito * 100.0 / self.expected_out)
            # Todavía no ha empezado a escribir: seguimos con la lectura.
        if self.total <= 0:
            return None
        info = self._find_fd()
        if not info:
            return None
        try:
            with open(info, "r") as f:
                for line in f:
                    if line.startswith("pos:"):
                        pos = int(line.split()[1])
                        break
                else:
                    return None
        except (OSError, ValueError, IndexError):
            return None
        return self._anotar(pos * 100.0 / self.total)

    def eta(self) -> float | None:
        """Segundos restantes según el ritmo OBSERVADO en este job, no según
        una constante. Necesita al menos dos muestras con avance real."""
        if len(self._samples) < 2:
            return None
        (t0, p0), (t1, p1) = self._samples[0], self._samples[-1]
        if p1 <= p0 or t1 <= t0:
            return None
        ritmo = (p1 - p0) / (t1 - t0)     # % por segundo
        if ritmo <= 0:
            return None
        return max(0.0, (100.0 - p1) / ritmo)


async def _emit_progress(log_callback, pct: float, label: str, eta_s: float | None = None) -> None:
    """Emite un marcador estructurado de progreso que el frontend detecta."""
    if not log_callback:
        return
    pct = max(0.0, min(100.0, pct))
    payload = {"pct": round(pct, 1), "label": label}
    if eta_s is not None and eta_s >= 0:
        payload["eta_s"] = int(eta_s)
    await log_callback(f"§§PROGRESS§§{json.dumps(payload)}")


# Cada cuánto una operación silenciosa escribe una línea REAL en el log.
# `extract-rpu`, `export`, `demux` e `inject-rpu` no imprimen nada durante
# minutos: sin esto, el log se queda clavado y parece que el job ha muerto
# (reportado el 2026-08-16 al final de la Fase A). El progreso sí fluye por
# WS, pero es invisible si el WS parpadea — y no deja rastro en el log
# permanente que el usuario revisa después.
HEARTBEAT_EVERY_S = 60.0


async def _emit_heartbeat(log_callback, label: str, elapsed_s: float,
                          eta_s: float | None = None) -> None:
    """Línea de texto 'sigue vivo' para operaciones sin salida propia."""
    if not log_callback:
        return
    mins, secs = int(elapsed_s) // 60, int(elapsed_s) % 60
    eta_txt = ""
    if eta_s is not None and eta_s > 0:
        eta_txt = f" · quedan ~{int(eta_s) // 60}min {int(eta_s) % 60}s"
    await log_callback(f"  ⏱ {label} en curso… ({mins}min {secs}s{eta_txt})")


async def _run_streaming(
    cmd: list[str],
    log_callback=None,
    proc_callback=None,
    progress_ctx: dict | None = None,
) -> int:
    # start_new_session=True: el subprocess arranca como lider de su propio
    # process group (setsid). Permite que cmv40_cancel use os.killpg para
    # alcanzar tambien procesos hijos (algunos ffmpeg con hwaccel lanzan
    # workers; sin esto un kill al padre los dejaria zombis).
    """Ejecuta un comando con streaming de stdout+stderr al log_callback.

    Divide por ``\\n`` y ``\\r`` (ffmpeg usa ``\\r`` en sus líneas de progreso).
    Traduce ``#GUI#progress XX%`` de mkvmerge → ``Progress: XX%``.
    Throttle de 500 ms para ffmpeg para no saturar el log.

    Si se pasa ``progress_ctx``, emite eventos ``§§PROGRESS§§`` con pct y ETA:
        progress_ctx = {
          'duration': float,           # duración conocida a priori (ffmpeg, s); o 0
          'time_estimate_s': float,    # alternativa: estimación wall-clock (para comandos silenciosos)
          'input_path': Path,          # fichero que lee el proceso → progreso REAL (_ReadProgress)
          'output_path': Path,         # fichero que escribe (mejor señal si hay 2 pasadas)
          'expected_out_bytes': int,   # tamaño final esperado de output_path
          'offset': float,             # pct base (0-100)
          'weight': float,             # peso de este paso en la fase (0-100)
          'label': str,                # etiqueta a mostrar
        }
    Prioridad: ffmpeg time= > mkvmerge Progress: > time_estimate_s (ticker).
    """
    # Antes de lanzar nada: el bloque que lee progress_ctx vive más abajo, y
    # estas dos se usan en cuanto existe el pid.
    progress_input = progress_ctx.get("input_path") if progress_ctx else None
    progress_output = progress_ctx.get("output_path") if progress_ctx else None
    progress_expected = int(progress_ctx.get("expected_out_bytes") or 0) if progress_ctx else 0
    progress_read_total = int(progress_ctx.get("expected_read_bytes") or 0) if progress_ctx else 0
    reader: _ReadProgress | None = None
    if log_callback:
        await log_callback(f"$ {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    if proc_callback:
        proc_callback(proc)
    if progress_input is not None:
        reader = _ReadProgress(
            proc.pid, Path(progress_input),
            output_path=Path(progress_output) if progress_output else None,
            expected_out=progress_expected,
            expected_read=progress_read_total)

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
    # Contador de warnings ruidosos suprimidos. Algunos HEVC bitstreams
    # generan cientos de "non monotonically increasing dts" por segundo
    # — el bitstream filter copia bien igualmente, pero el log se infla.
    suppressed_dts_warnings = 0
    suppressed_other_warnings: dict[str, int] = {}

    async def _emit(text: str) -> None:
        nonlocal last_throttle, last_progress_push, duration, has_real_progress
        nonlocal suppressed_dts_warnings
        if not text:
            return
        if text.startswith("#GUI#progress "):
            text = "Progress: " + text.removeprefix("#GUI#progress ")
        elif text.startswith("#GUI#"):
            return

        # Filtro: warning DTS no monotonico de ffmpeg. Cosmetic — el
        # bitstream filter (hevc_mp4toannexb) copia correctamente. Suprimimos
        # todas excepto la primera (avisa al usuario que ocurre) + un
        # resumen al final con el conteo total.
        if "non monotonically increasing dts to muxer" in text:
            suppressed_dts_warnings += 1
            if suppressed_dts_warnings == 1:
                if log_callback:
                    await log_callback(
                        "[ffmpeg] ⚠ DTS no monotonicos detectados en el HEVC source — "
                        "warning cosmetico del bitstream filter, la copia es correcta. "
                        "Mensajes posteriores suprimidos; resumen al final del comando."
                    )
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
        last_hb = time.monotonic()
        while not stop_ticker.is_set():
            if has_real_progress:
                return
            elapsed = time.monotonic() - step_start
            step_pct = None
            eta = None
            # Progreso REAL si sabemos qué fichero está leyendo el proceso
            # (inject-rpu, mux). Ver _ReadProgress.
            real = False
            if reader is not None:
                step_pct = reader.sample()
                if step_pct is not None:
                    real = True
                    eta = reader.eta()
            if step_pct is None:
                step_pct = min(95.0, (elapsed / time_est) * 100.0)
            if eta is None:
                # Aunque el porcentaje sea real, el ETA puede no estarlo (hacen
                # falta dos muestras con avance). Sin este respaldo el "quedan
                # ~Nmin" DESAPARECÍA del log en cuanto había señal real.
                restante = time_est - elapsed
                eta = restante if restante > 0 else None
            phase_pct = offset + step_pct * weight / 100.0
            await _emit_progress(log_callback, phase_pct, label, eta)
            now = time.monotonic()
            if now - last_hb >= HEARTBEAT_EVERY_S:
                last_hb = now
                # El % solo se anuncia cuando es medido: con la estimación por
                # reloj sería una cifra inventada con pinta de dato.
                await _emit_heartbeat(
                    log_callback,
                    f"{label or 'Proceso'} ({step_pct:.0f}%)" if real else (label or "Proceso"),
                    elapsed, eta)
            try:
                await asyncio.wait_for(stop_ticker.wait(), timeout=1.5)
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

    # Resumen de warnings suprimidos (si los hubo)
    if suppressed_dts_warnings > 1 and log_callback:
        await log_callback(
            f"[ffmpeg] ℹ Total de warnings DTS no monotonicos suprimidos: "
            f"{suppressed_dts_warnings - 1} (cosmeticos; el HEVC se copio bien)."
        )

    return proc.returncode


# Cada cuánto el pipeline escribe una línea de avance en el log. Con las
# líneas crudas de ffmpeg serían ~700 por job; así son ~35 y se leen.
PIPE_LOG_EVERY_S = 10.0

# Cada cuánto se consulta al kernel cuánto lleva leído el consumidor del pipe.
# Constante de módulo para que los tests puedan acelerarla; en producción un
# segundo es el ritmo de la barra.
PIPE_TICK_EVERY_S = 1.0

# ffmpeg 4.x escribía `size=  19459840kB`; desde 6.x usa unidades IEC
# (`size=  19003MiB`). Se aceptan las dos, con su factor.
_FFMPEG_SIZE_RE  = re.compile(r"size=\s*(\d+)\s*(kB|KiB|MiB|GiB)")
_SIZE_UNIT_MB    = {"kB": 1 / 1024, "KiB": 1 / 1024, "MiB": 1.0, "GiB": 1024.0}
_FFMPEG_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")
_FFMPEG_FRAME_RE = re.compile(r"frame=\s*(\d+)")


def _fmt_miles(n: int) -> str:
    """58601 → '58.601' (separador de miles español)."""
    return f"{n:,}".replace(",", ".")


def _fmt_ffmpeg_frame(line: str, total: int = 0) -> str:
    """`frame=58601` → `frame 58.601/145.303` (o sin total si no se conoce)."""
    m = _FFMPEG_FRAME_RE.search(line)
    if not m:
        return ""
    cur = _fmt_miles(int(m.group(1)))
    if total > 0:
        return f"frame {cur}/{_fmt_miles(total)} · "
    return f"frame {cur} · "


def _fmt_ffmpeg_size(line: str, out_path: Path | None = None) -> str:
    """Cuánto se lleva escrito, como `19,5 GB`. Cadena vacía si no se sabe.

    Primero se mira el fichero de salida, que es el dato de verdad y siempre
    está. A ffmpeg no se le puede preguntar cuando escribe con el muxer
    `tee`: al haber dos salidas emite `Lsize=N/A`, y el campo salía como "…"
    en el log del pipeline.
    """
    if out_path is not None:
        try:
            n = out_path.stat().st_size
            if n > 0:
                return _gb(n)
        except OSError:
            pass
    m = _FFMPEG_SIZE_RE.search(line)
    if not m:
        return ""
    return _gb(int(m.group(1)) * _SIZE_UNIT_MB[m.group(2)] * 1024 * 1024)


def _fmt_ffmpeg_speed(line: str) -> str:
    """`speed=23.6x` → ` · 23,6x`. Cadena vacía si ffmpeg no lo reporta."""
    m = _FFMPEG_SPEED_RE.search(line)
    if not m:
        return ""
    return f" · {m.group(1).replace('.', ',')}x"


def _fmt_eta(eta_s: float | None) -> str:
    if not eta_s or eta_s <= 0:
        return "casi listo"
    m, s = int(eta_s) // 60, int(eta_s) % 60
    return f"quedan ~{m}min {s}s" if m else f"quedan ~{s}s"


# Tamaño del buffer del pipe entre ffmpeg y dovi_tool. Linux da 64 KB por
# defecto, lo que sobre 48 GB obliga a los dos procesos a sincronizarse casi
# 800.000 veces. Con un buffer mayor se desacoplan: el que va sobrado sigue
# trabajando mientras el otro se pone al día.
PIPE_BUF_BYTES = 4 * 1024 * 1024


def _widen_pipe(write_fd: int) -> int:
    """Amplía el buffer del pipe. Devuelve el tamaño resultante (0 si no se
    pudo). Best-effort: el kernel limita a /proc/sys/fs/pipe-max-size (1 MB
    por defecto) salvo con CAP_SYS_RESOURCE, y el contenedor corre privileged
    pero no damos por hecho que siempre sea así."""
    try:
        import fcntl
        f_setpipe_sz = getattr(fcntl, "F_SETPIPE_SZ", 1031)
        f_getpipe_sz = getattr(fcntl, "F_GETPIPE_SZ", 1032)
        for size in (PIPE_BUF_BYTES, 1024 * 1024):
            try:
                fcntl.fcntl(write_fd, f_setpipe_sz, size)
                return fcntl.fcntl(write_fd, f_getpipe_sz)
            except OSError:
                continue
    except Exception:
        pass
    return 0


def _gb(n: float) -> str:
    """Bytes → '73,2 GB'. GB decimales (1e9) y coma decimal, la misma unidad
    que la línea de cierre de la fase: mezclarla con GiB hacía que el mismo
    fichero apareciera como 67,8 GB mientras crecía y 73,2 GB al terminar."""
    return f"{n / 1e9:.1f} GB".replace(".", ",")


def _proc_rchar(pid: int) -> int | None:
    """Bytes que el proceso lleva leídos en total, de `/proc/<pid>/io`.

    Para el consumidor de un pipe no sirve `fdinfo` (un pipe no tiene
    posición), pero `rchar` sí cuenta lo que ha sacado de él. Incluye las
    lecturas del propio arranque del binario, unos pocos MB: irrelevante
    frente a las decenas de GB del stream.
    """
    try:
        with open(f"/proc/{pid}/io", "r") as fh:
            for linea in fh:
                if linea.startswith("rchar:"):
                    return int(linea.split(":", 1)[1])
    except (OSError, ValueError):
        pass
    return None


def _tamano(p: Path) -> int:
    """Tamaño en bytes, 0 si no se puede leer."""
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _tee_path_is_safe(path: Path) -> bool:
    """El muxer `tee` de ffmpeg usa `|` para separar salidas, `[...]` para las
    opciones de cada una y `:` dentro de ellas. Una ruta con esos caracteres
    rompería el descriptor, así que en ese caso preferimos el camino clásico.
    """
    return not any(c in str(path) for c in "|[]:")


async def _ffmpeg_extract_rpu_piped(
    mkv_path: str,
    rpu_out: Path,
    hevc_out: Path | None = None,
    duration: float = 0.0,
    log_callback=None,
    proc_callback=None,
    offset: float = 0.0,
    weight: float = 100.0,
    label: str = "Extrayendo HEVC + RPU",
    estimated_s: float = 0.0,
    total_frames: int = 0,
) -> bool:
    """Extrae el HEVC y su RPU en UNA sola pasada, con ffmpeg y dovi_tool
    trabajando a la vez.

    En serie, estos dos pasos son la mayor parte de la Fase A y se estorban:
    medido sobre John Wick (243.552 frames), ffmpeg tarda 574 s y está
    limitado por el disco (la CPU ociosa), y `extract-rpu` tarda 372 s y está
    limitado por la CPU al 100 % de un core (el disco medio ocioso). Uno
    detrás de otro son ~946 s usando la mitad de la máquina cada vez.

    Conectados por un pipe, el conjunto va al ritmo del más lento (~574 s) y
    además `extract-rpu` deja de releer del disco los 73 GB del HEVC.

    Si `hevc_out` es None, el HEVC no se guarda: solo interesa el RPU (caso
    del pre-flight sobre otro MKV). Si se pide, ffmpeg lo escribe con el
    muxer `tee` mientras manda una copia por el pipe — verificado bit a bit:
    el HEVC y el RPU salen con el mismo MD5 que por el camino clásico.

    Devuelve True si todo fue bien. False (sin lanzar) si algo falla, para
    que el caller pueda recurrir al camino secuencial de siempre.
    """
    if hevc_out is not None and not _tee_path_is_safe(hevc_out):
        if log_callback:
            await log_callback(
                "[Fase A] La ruta del workdir lleva caracteres que el muxer tee "
                "de ffmpeg no admite — se usa el camino en dos pasos.")
        return False

    if hevc_out is not None:
        out_args = ["-f", "tee", f"[f=hevc]{hevc_out}|[f=hevc]pipe:1"]
    else:
        out_args = ["-f", "hevc", "pipe:1"]
    ff_cmd = [
        FFMPEG_BIN, "-y", "-i", mkv_path,
        "-map", "0:v:0", "-c:v", "copy",
        "-bsf:v", "hevc_mp4toannexb",
        *out_args,
    ]
    dv_cmd = [DOVI_TOOL_BIN, "extract-rpu", "-", "-o", str(rpu_out)]

    if log_callback:
        await log_callback(f"$ {' '.join(ff_cmd)} | {' '.join(dv_cmd)}")

    read_fd, write_fd = os.pipe()
    _widen_pipe(write_fd)
    ff = dv = None
    try:
        ff = await asyncio.create_subprocess_exec(
            *ff_cmd, stdout=write_fd, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        dv = await asyncio.create_subprocess_exec(
            *dv_cmd, stdin=read_fd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        # Arrancar el pipeline no debe poder tumbar la fase: el caller tiene
        # el camino en dos pasos esperando.
        if ff is not None:
            try:
                ff.kill()
            except Exception:
                pass
        _logger.info("No se pudo lanzar el pipeline ffmpeg|extract-rpu: %s", e)
        if log_callback:
            await log_callback(
                f"[Fase A] No se pudo lanzar el pipeline ({e}) — se usa el "
                f"camino en dos pasos.")
        return False
    finally:
        # El padre DEBE cerrar sus copias: si el extremo de escritura sigue
        # abierto aquí, dovi_tool nunca ve el EOF y el pipeline se cuelga.
        os.close(read_fd)
        os.close(write_fd)

    if proc_callback:
        proc_callback(ff)
        proc_callback(dv)

    # Progreso desde el stderr de ffmpeg (las líneas `frame=… time=…`).
    #
    # Alimenta dos cosas distintas:
    #   - La barra del overlay (§§PROGRESS§§), cada segundo.
    #   - Una línea de texto en el log cada PIPE_LOG_EVERY_S. NO se reenvían
    #     las líneas crudas de ffmpeg: son ~700 por job, ilegibles, y se
    #     persisten. Una línea consolidada dice lo mismo mejor.
    # Quién manda en la barra: mientras ffmpeg escupe líneas es él, pero el
    # que decide cuándo acaba la fase es dovi_tool (ver _tick_consumidor).
    last_log = 0.0
    ff_tail: list[str] = []
    media_frac = 0.0          # posición de ffmpeg en el vídeo, 0..1
    ff_eof = False            # ffmpeg ya cerró su stderr → su salida es final
    # ETA del pipeline completo (del consumidor). None mientras no haya dos
    # muestras con avance; en ese hueco manda el ETA de ffmpeg.
    tail_eta: float | None = None

    async def _drain_ffmpeg() -> None:
        nonlocal last_log, media_frac, ff_eof
        buf = b""
        while True:
            chunk = await ff.stderr.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                nl, cr = buf.find(b"\n"), buf.find(b"\r")
                if nl == -1 and cr == -1:
                    break
                idx = min(x for x in (nl, cr) if x != -1)
                line = buf[:idx].decode("utf-8", errors="replace").rstrip()
                buf = buf[idx + 1:]
                if not line:
                    continue
                ff_tail.append(line)
                del ff_tail[:-40]
                now = time.monotonic()
                m = _FFMPEG_TIME_RE.search(line) if duration > 0 else None
                if not m:
                    continue
                elapsed_media = _hms_to_seconds(*m.groups())
                media_frac = max(0.0, min(1.0, elapsed_media / duration))
                step_pct = media_frac * 100.0
                wall = now - step_start
                eta = ((wall / elapsed_media) * (duration - elapsed_media)
                       if elapsed_media > 0 else None)
                if log_callback and (now - last_log) >= PIPE_LOG_EVERY_S:
                    last_log = now
                    tam = _fmt_ffmpeg_size(line, hevc_out)
                    # El ETA de ffmpeg solo cubre SU parte. Si tenemos el del
                    # pipeline completo, ese es el que vale: ffmpeg terminaba
                    # anunciando "quedan ~3s" con 3 minutos de extract-rpu por
                    # delante.
                    await log_callback(
                        f"  ⏱ {step_pct:.0f}% leído del vídeo · "
                        f"{_fmt_ffmpeg_frame(line, total_frames)}"
                        f"{tam + ' · ' if tam else ''}"
                        f"{_fmt_ffmpeg_speed(line).lstrip(' ·').strip()} · "
                        f"{_fmt_eta(tail_eta if tail_eta is not None else eta)}")
        ff_eof = True

    step_start = time.monotonic()
    dv_out = b""
    fin = asyncio.Event()

    async def _drain_dovi() -> None:
        nonlocal dv_out
        try:
            dv_out = await dv.stdout.read()
        finally:
            fin.set()

    async def _tick_consumidor() -> None:
        """Progreso de la fase medido en el consumidor del pipe, no en ffmpeg.

        ffmpeg termina bastante antes que `extract-rpu`: lee del disco a
        varios cientos de MB/s mientras el otro parsea NALs a un core. Medido
        en el NAS, ffmpeg acaba y el pipeline sigue 71 s (Transformers One,
        40 GB), 119 s (Nosferatu, 63 GB) y 198 s (John Wick 4, 73 GB) — entre
        un cuarto y un tercio de la fase. Confirmado por los mtime de los
        artefactos: `source.hevc` quedó fijo a los 321 s y `RPU_source.bin`
        se escribió a los 519 s.

        Todo el progreso salía del stderr de ffmpeg, así que en esa cola no se
        emitía NADA: la barra clavada en 99 %, "quedan ~3s" y el log mudo
        varios minutos, como si el job hubiera muerto.

        La señal correcta es cuántos bytes lleva sacados del pipe el
        consumidor (`rchar`) sobre el total del stream. El total no se conoce
        de antemano, pero se extrapola de lo que ffmpeg ya escribió y de por
        dónde va en el vídeo; cuando ffmpeg cierra, su fichero ES el total.

        Si `/proc` no da la señal, no se emite nada desde aquí y manda el
        progreso de ffmpeg de siempre.
        """
        nonlocal tail_eta
        ultimo_pct = 0.0
        ultimo_log = time.monotonic()
        muestras: list[tuple[float, int]] = []   # (monotonic, bytes leídos)
        while not fin.is_set():
            try:
                await asyncio.wait_for(fin.wait(), timeout=PIPE_TICK_EVERY_S)
                break
            except asyncio.TimeoutError:
                pass
            leido = _proc_rchar(dv.pid)
            escrito = _tamano(hevc_out) if hevc_out is not None else 0
            # Sin fichero de salida (pre-flight sobre otro MKV) no hay con qué
            # extrapolar el total; esas extracciones son cortas y se quedan
            # con el progreso de ffmpeg.
            if leido is None or escrito <= 0 or (not ff_eof and media_frac < 0.02):
                continue
            total = escrito if ff_eof else escrito / media_frac
            if total <= 0:
                continue
            pct = max(ultimo_pct, min(100.0, (leido / total) * 100.0))
            ultimo_pct = pct

            ahora = time.monotonic()
            muestras.append((ahora, leido))
            del muestras[:-15]
            eta = None
            if len(muestras) >= 2:
                dt = muestras[-1][0] - muestras[0][0]
                db = muestras[-1][1] - muestras[0][1]
                if dt > 0 and db > 0:
                    eta = max(0.0, (total - leido) / (db / dt))
            tail_eta = eta
            await _emit_progress(
                log_callback, offset + pct * weight / 100.0, label, eta)

            # Una línea de texto solo cuando ffmpeg ya no escribe ninguna: si
            # no, se solaparían dos avances distintos del mismo paso.
            if (log_callback and ff_eof
                    and (ahora - ultimo_log) >= PIPE_LOG_EVERY_S):
                ultimo_log = ahora
                await log_callback(
                    f"  ⏱ ffmpeg terminó · extract-rpu procesando el stream — "
                    f"{pct:.0f}% ({_gb(leido)} de {_gb(total)}) · {_fmt_eta(eta)}")

    # Timeout del conjunto. Escala con la estimación (que se ancla a la carga
    # real del NAS) y nunca baja de una hora: el pipeline recorre el vídeo
    # entero y un tope corto es justo el bug que documenta
    # test_pipeline_timeouts.
    total_timeout = _adaptive_timeout(estimated_s, floor_s=3600)
    try:
        await asyncio.wait_for(
            asyncio.gather(_drain_ffmpeg(), _drain_dovi(), _tick_consumidor()),
            timeout=total_timeout)
        ff_rc = await asyncio.wait_for(ff.wait(), timeout=60)
        dv_rc = await asyncio.wait_for(dv.wait(), timeout=300)
    except asyncio.TimeoutError:
        fin.set()                      # corta el ticker antes de salir
        for p in (ff, dv):
            try:
                p.kill()
            except Exception:
                pass
        if log_callback:
            await log_callback(
                f"[Fase A] El pipeline ffmpeg|extract-rpu excedió {total_timeout}s — "
                f"abortado; se reintenta en dos pasos.")
        return False

    if ff_rc != 0 or dv_rc != 0:
        if log_callback:
            await log_callback(
                f"[Fase A] El pipeline ffmpeg|extract-rpu falló "
                f"(ffmpeg={ff_rc}, dovi_tool={dv_rc}) — se reintenta en dos pasos."
            )
            for line in ff_tail[-6:]:
                await log_callback(f"  {line}")
            tail = dv_out.decode("utf-8", errors="replace").strip().splitlines()[-4:]
            for line in tail:
                await log_callback(f"  {line}")
        return False
    if not rpu_out.exists() or rpu_out.stat().st_size == 0:
        return False
    return True


async def _run_with_time_estimate(
    cmd: list[str],
    estimated_s: float,
    log_callback=None,
    proc_callback=None,
    timeout: int | None = None,
    label: str = "",
    offset: float = 0.0,
    weight: float = 100.0,
    progress_input: Path | None = None,
) -> tuple[int, str, str]:
    """Ejecuta un comando silencioso emitiendo progreso cada 1,5 s.

    Usado para ``dovi_tool extract-rpu``, ``demux`` y similares, que no
    producen salida de progreso cuando stdout está conectado a un pipe.

    Con `progress_input` se sigue lo que el proceso lleva LEÍDO de ese
    fichero (ver `_ReadProgress`): porcentaje exacto y ETA según el ritmo
    real. Sin él —o si el kernel no lo expone— se cae a la estimación por
    reloj de siempre, `elapsed / estimated_s` con tope al 95 %.
    """
    stop = asyncio.Event()
    start = time.monotonic()
    reader: _ReadProgress | None = None
    used_real = False

    async def _tick():
        nonlocal used_real
        last_hb = time.monotonic()
        while not stop.is_set():
            elapsed = time.monotonic() - start
            step_pct = None
            eta = None
            if reader is not None:
                step_pct = reader.sample()
                if step_pct is not None:
                    used_real = True
                    eta = reader.eta()
            if step_pct is None:
                # Sin señal del kernel: estimación por reloj (lo de antes).
                est = max(estimated_s, 5.0)
                step_pct = min(95.0, (elapsed / est) * 100.0)
            if eta is None:
                restante = max(estimated_s, 5.0) - elapsed
                eta = restante if restante > 0 else None
            phase_pct = offset + step_pct * weight / 100.0
            await _emit_progress(log_callback, phase_pct, label, eta)
            # Heartbeat de texto: estos comandos (extract-rpu, export, demux)
            # no escriben NADA durante minutos. Ver HEARTBEAT_EVERY_S.
            now = time.monotonic()
            if now - last_hb >= HEARTBEAT_EVERY_S:
                last_hb = now
                await _emit_heartbeat(
                    log_callback,
                    f"{label or 'Proceso'} ({step_pct:.0f}%)" if used_real else (label or "Proceso"),
                    elapsed, eta)
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                pass

    tick_task = asyncio.create_task(_tick())
    try:
        if log_callback:
            await log_callback(f"$ {' '.join(cmd)}")
        # start_new_session=True para que cancel pueda usar killpg
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if proc_callback:
            proc_callback(proc)
        if progress_input is not None:
            reader = _ReadProgress(proc.pid, progress_input)
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


# ══════════════════════════════════════════════════════════════════════
#  FASE A — Analizar MKV origen
# ══════════════════════════════════════════════════════════════════════

async def preflight_source(
    session: CMv40Session,
    log_callback=None,
    proc_callback=None,
) -> None:
    """Sniff DV del MKV origen — extrae 30s con ffmpeg, ejecuta extract-rpu
    y aborta si no hay NALs DV. ~10s. Evita los ~5 min de Fase A si el MKV
    está mal etiquetado.

    Idempotente: si session.source_preflight_ok=True, retorna sin hacer nada.
    Setea session.source_preflight_ok=True al pasar (no save aquí — el caller
    lo hace junto con otros campos).

    Args:
        session: Proyecto CMv4.0 con source_mkv_path
        log_callback: opcional, recibe líneas de progreso
        proc_callback: opcional, registra el subprocess para cancelación

    Raises:
        RuntimeError si el MKV origen no tiene Dolby Vision RPU.
    """
    if session.source_preflight_ok:
        if log_callback:
            await log_callback("[Pre-flight] Source ya validado previamente — skip sniff")
        return

    wd = get_workdir(session)
    sniff_hevc = wd / "_sniff.hevc"
    sniff_rpu  = wd / "_sniff.rpu"

    if log_callback:
        await log_callback(
            "[Pre-flight] Sniff rápido del origen (30s) — verificando que el "
            "HEVC contiene Dolby Vision RPU…"
        )
    try:
        rc = await _run_streaming([
            FFMPEG_BIN, "-y", "-t", "30", "-i", session.source_mkv_path,
            "-map", "0:v:0", "-c:v", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-f", "hevc", str(sniff_hevc),
        ], log_callback=None, proc_callback=proc_callback)
        if rc != 0 or not sniff_hevc.exists() or sniff_hevc.stat().st_size == 0:
            raise RuntimeError("ffmpeg no pudo extraer el sniff HEVC del source MKV")
        rc, _out, _err = await _run([
            DOVI_TOOL_BIN, "extract-rpu", str(sniff_hevc), "-o", str(sniff_rpu),
        ], timeout=60)
        if rc != 0 or not sniff_rpu.exists() or sniff_rpu.stat().st_size == 0:
            raise RuntimeError(
                "El MKV origen no contiene Dolby Vision RPU (sniff de 30s sin "
                "NALs DV detectados). Causas típicas: (1) el MKV está etiquetado "
                "como DV pero el video real es solo HDR10/SDR; (2) la conversión "
                "P7 → P8 que generaste perdió el RPU al remuxar (mkvmerge solo "
                "no preserva el EL ni el RPU — usa dovi_tool demux + inject-rpu); "
                "(3) re-encode sin preservar la metadata DV. Verifica con "
                "`dovi_tool info -i <mkv>` antes de relanzar."
            )
        if log_callback:
            await log_callback(
                f"[Pre-flight] ✓ Sniff OK — RPU detectado ({sniff_rpu.stat().st_size} bytes "
                "en 30s del origen)."
            )
        session.source_preflight_ok = True
    finally:
        for p in (sniff_hevc, sniff_rpu):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass


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

    # Validación temprana: si el MKV no tiene duración detectable, ffmpeg
    # seguirá adelante con "File ended prematurely" y exit code 0,
    # produciendo un source.hevc trunco que pasa el rc==0 check pero solo
    # contiene un puñado de frames. Caso real (2026-05-25): MKV de "28
    # años después" sin segmento Matroska finalizado o con la última
    # parte truncada → ffmpeg sacó 97 MB de HEVC con 241 frames; el
    # pipeline continuó hasta Fase B que reventó con un gate l1_div
    # confuso. Mejor parar aquí con mensaje claro.
    mkv_path_obj = Path(session.source_mkv_path)
    mkv_size = mkv_path_obj.stat().st_size if mkv_path_obj.exists() else 0
    if duration <= 0:
        raise RuntimeError(
            f"El MKV origen no tiene duración detectable (ffprobe devuelve "
            f"Duration: N/A). Posibles causas: fichero corrupto, transferencia "
            f"interrumpida (descarga / copia parcial), o MKV sin segmento "
            f"Matroska finalizado. Tamaño del fichero: {mkv_size / 1e9:.2f} GB. "
            f"Verifica con `mediainfo` o `ffprobe` que el MKV está intacto "
            f"antes de relanzar Fase A."
        )

    # Paso 0: Sniff DV del source. Idempotente — preflight_source ya lo hace
    # si el usuario lo dispara (típicamente al crear el proyecto o vía
    # /api/cmv40/{id}/preflight-source). Si por alguna razón Fase A se lanza
    # sin preflight previo (sesión legacy, error transitorio del preflight),
    # actuamos como red de seguridad.
    await preflight_source(session, log_callback=log_callback, proc_callback=proc_callback)

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
    # Threshold de validación del HEVC extraído: debería ser >=50% del
    # tamaño del MKV (en UHD el HEVC es >90% del total). Por debajo de
    # eso, ffmpeg terminó prematuramente. Se usa tanto para invalidar
    # cache (forzar re-extracción) como para abortar tras ffmpeg si
    # produjo basura.
    MIN_HEVC_RATIO = 0.5

    ffmpeg_elapsed = 0.0
    existing_size = source_hevc.stat().st_size if source_hevc.exists() else 0
    existing_too_small = (
        existing_size < 1_000_000
        or (mkv_size > 0 and existing_size < MIN_HEVC_RATIO * mkv_size)
    )

    # ── Pasos 1+2 en una sola pasada ────────────────────────────────────
    # ffmpeg (limitado por disco) y extract-rpu (limitado por CPU) hacen el
    # mismo recorrido sobre decenas de GB, uno detrás de otro. Conectados por
    # un pipe se solapan y el conjunto va al ritmo del más lento. Solo aplica
    # si hay que extraer el HEVC: si ya está en disco de un run anterior, el
    # camino de siempre (extract-rpu leyendo el fichero) es más barato.
    piped_ok = False
    if existing_too_small:
        if existing_size > 0 and log_callback:
            await log_callback(
                f"[Fase A] source.hevc previo ({existing_size / 1e6:.0f} MB) "
                f"es demasiado pequeño respecto al MKV ({mkv_size / 1e9:.2f} GB) — "
                f"se regenera desde cero."
            )
        if log_callback:
            await log_callback(
                "[Fase A] ┌─ Paso 1/3: Extrayendo el HEVC y su RPU a la vez — "
                "ffmpeg escribe source.hevc mientras dovi_tool saca el RPU del "
                "mismo flujo, sin esperar a que el fichero esté completo."
            )
        t0 = time.monotonic()
        piped_ok = await _ffmpeg_extract_rpu_piped(
            session.source_mkv_path, rpu_source, hevc_out=source_hevc,
            duration=duration, log_callback=log_callback,
            proc_callback=proc_callback,
            offset=0.0, weight=W_FFMPEG + W_RPU,
            label="Extrayendo HEVC + RPU (en paralelo)",
            estimated_s=_estimate_from_ffmpeg(session, 1.0, FPS_FFMPEG_EXTRACT),
            total_frames=frame_count,
        )
        if piped_ok:
            ffmpeg_elapsed = time.monotonic() - t0
            hevc_size = source_hevc.stat().st_size if source_hevc.exists() else 0
            if mkv_size > 0 and hevc_size > 0 and (hevc_size / mkv_size) < MIN_HEVC_RATIO:
                # Mismo guard que en el camino clásico: un HEVC truncado con
                # rc=0 es señal de MKV corrupto.
                source_hevc.unlink(missing_ok=True)
                rpu_source.unlink(missing_ok=True)
                raise RuntimeError(
                    f"ffmpeg terminó con rc=0 pero extrajo solo "
                    f"{hevc_size / 1e9:.2f} GB de HEVC desde un MKV de "
                    f"{mkv_size / 1e9:.2f} GB (ratio {hevc_size / mkv_size:.1%}, "
                    f"esperado >={MIN_HEVC_RATIO:.0%}). El MKV probablemente está "
                    f"corrupto o incompleto. Artefactos parciales borrados."
                )
            if log_callback:
                await log_callback(
                    f"[Fase A] ✓ HEVC ({hevc_size / 1e9:.1f} GB) + RPU extraídos "
                    f"en una pasada ({ffmpeg_elapsed:.0f}s)"
                )
            await _emit_progress(log_callback, W_FFMPEG + W_RPU, "HEVC y RPU extraídos")
        else:
            # El pipeline no era viable: limpiamos lo que haya quedado a medias
            # y seguimos por el camino de siempre.
            source_hevc.unlink(missing_ok=True)
            rpu_source.unlink(missing_ok=True)

    if not piped_ok and existing_too_small:
        # Camino en dos pasos: el pipeline no se pudo usar (ruta no apta para
        # el muxer tee, dovi_tool sin soporte de stdin, o falló a medias).
        if log_callback:
            await log_callback(
                "[Fase A] ┌─ Paso 1/4: Extrayendo stream HEVC del MKV origen con ffmpeg…")
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

        # Validación post-ffmpeg: el HEVC debe tener un tamaño razonable
        # respecto al MKV. Sin esto, ffmpeg puede terminar con rc=0 tras
        # ver "File ended prematurely" y dejar un HEVC trunco — caso real
        # 2026-05-25, ffmpeg sacó 97 MB de un MKV de varios GB. Borramos
        # el parcial para que un reintento empiece limpio.
        hevc_size = source_hevc.stat().st_size if source_hevc.exists() else 0
        if mkv_size > 0 and hevc_size > 0:
            ratio = hevc_size / mkv_size
            if ratio < MIN_HEVC_RATIO:
                try:
                    source_hevc.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    f"ffmpeg terminó con rc=0 pero extrajo solo "
                    f"{hevc_size / 1e9:.2f} GB de HEVC desde un MKV de "
                    f"{mkv_size / 1e9:.2f} GB (ratio {ratio:.1%}, esperado "
                    f">={MIN_HEVC_RATIO:.0%}). El MKV probablemente está "
                    f"corrupto o incompleto — busca 'File ended prematurely' "
                    f"en el log de ffmpeg arriba. Verifica con `mediainfo` o "
                    f"`ffprobe -i <mkv>` antes de relanzar Fase A. El "
                    f"source.hevc parcial se ha borrado para que el reintento "
                    f"sea limpio."
                )
    elif not piped_ok:
        if log_callback:
            await log_callback("[Fase A] source.hevc ya existe, reutilizando")
    if not piped_ok:
        await _emit_progress(log_callback, W_FFMPEG, "HEVC extraído")

    # Guardar wall-time de ffmpeg como ancla para estimaciones futuras.
    # Con el pipeline, este tiempo cubre ffmpeg + extract-rpu solapados: es
    # una cota superior del ffmpeg puro, así que las estimaciones derivadas
    # (RATIO_*) quedan algo holgadas, que es el lado seguro.
    if ffmpeg_elapsed > 5:
        session.ffmpeg_wall_seconds = ffmpeg_elapsed
        session.ffmpeg_wall_includes_rpu = piped_ok

    if not piped_ok:
        # Paso 2: Extraer RPU (silencioso con pipe → progreso estimado por tiempo)
        if log_callback:
            await log_callback("[Fase A] ├─ Paso 2/4: Extrayendo RPU del HEVC con dovi_tool extract-rpu…")
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
           timeout=_adaptive_timeout(est_rpu, floor_s=1200), label="Extrayendo RPU del HEVC",
           offset=W_FFMPEG, weight=W_RPU, progress_input=source_hevc)
        if rc != 0:
            raise RuntimeError(f"dovi_tool extract-rpu falló: {err[:300]}")

    # extract-rpu sale rc=0 incluso cuando no encuentra ni un solo NAL DV en
    # el HEVC — solo deja el fichero vacio o inexistente. info --summary
    # daria "Error: No RPU found" pero ese mensaje es opaco. Detectamos aqui
    # para dar un error util que apunte al origen real del problema (MKV
    # marcado como DV pero sin RPU embebido).
    if not rpu_source.exists() or rpu_source.stat().st_size == 0:
        raise RuntimeError(
            "El MKV origen no contiene Dolby Vision RPU. dovi_tool extract-rpu "
            "no encontro ningun NAL DV en el HEVC. Causas tipicas: (1) el MKV "
            "esta etiquetado como DV pero el video real es solo HDR10/SDR; "
            "(2) la conversion P7 -> P8 que generaste perdio el RPU al "
            "remuxar; (3) el video se re-encodeo sin preservar la metadata DV. "
            "Verifica el origen con `mediainfo` o `dovi_tool info -i <mkv>` "
            "antes de relanzar Fase A."
        )
    await _emit_progress(log_callback, W_FFMPEG + W_RPU, "RPU extraído")

    # Paso 3: Info del RPU
    if log_callback:
        await log_callback(
            f"[Fase A] ├─ Paso {'2/3' if piped_ok else '3/4'}: Analizando metadata "
            f"del RPU con dovi_tool info --summary…")
    rc, summary, err = await _run([
        DOVI_TOOL_BIN, "info", "--summary", str(rpu_source),
    ], timeout=30)
    if rc != 0:
        raise RuntimeError(f"dovi_tool info falló: {err[:300]}")

    dovi_info = _parse_dovi_summary(summary)
    session.source_dv_info = dovi_info
    session.source_frame_count = dovi_info.frame_count

    # Validación post-extract-rpu: tercera capa de defensa. Si el HEVC
    # logró pasar las dos capas anteriores pero el RPU resultante tiene
    # muchos menos frames de los esperados, también abortamos. Cubre
    # casos donde ffmpeg sacó tamaño suficiente pero el HEVC tenía
    # corrupciones internas que dovi_tool no pudo procesar entero.
    if frame_count > 0 and dovi_info.frame_count > 0:
        ratio = dovi_info.frame_count / frame_count
        if ratio < 0.5:
            try:
                source_hevc.unlink()
                rpu_source.unlink()
            except OSError:
                pass
            raise RuntimeError(
                f"El RPU extraído tiene {dovi_info.frame_count} frames pero "
                f"el MKV origen esperaba ~{frame_count} (ratio {ratio:.1%}). "
                f"Discrepancia masiva — el HEVC se truncó durante el procesado "
                f"(MKV con corrupciones internas). Artefactos parciales "
                f"borrados — verifica el fichero con `ffprobe -count_frames "
                f"-i <mkv>` antes de relanzar Fase A."
            )

    # Detectar workflow según perfil y subperfil (ver CMv40Session.source_workflow)
    session.source_workflow = _detect_workflow(dovi_info)
    workflow_label = {
        "p7_fel": "P7 FEL — demux + merge CMv4.0 + preserva FEL",
        "p7_mel": "P7 MEL — descarta EL, inyecta RPU target → P8.1 CMv4.0",
        "p8":     "P8.1 — inject directo de RPU target → P8.1 CMv4.0",
    }.get(session.source_workflow, session.source_workflow)

    # Plot eliminado: generaba plot_source.png que la UI no consume.
    # Si se quiere reintroducir, añadir también el render en el panel.

    # ── Bloque 1: análisis profundo del L2 del source ──
    # `dovi_tool export -d all` + parseo de combos únicos. Alimenta el
    # modelo de decisión Keep/Drop-in/Merge (comparación L2 source vs bin).
    # Coste ~3-5s sobre Fase A que dura ~12 min — despreciable.
    from phases.rpu_analyze import analyze_rpu_combos, compare_l2, recommend_action
    if log_callback:
        await log_callback(
            f"[Fase A] └─ Paso {'3/3' if piped_ok else '4/4'}: Analizando combos L2 del source (dovi_tool export) "
            "para comparar con el target y decidir Mantener/Inyectar…"
        )
    source_analysis = await analyze_rpu_combos(rpu_source)
    if source_analysis.total_frames > 0:
        session.source_l2_combos = source_analysis.l2_combos
        session.source_l2_unique_count = source_analysis.l2_unique_count
        session.source_l2_target_pqs = source_analysis.l2_target_pqs
        session.source_frames_analyzed = source_analysis.total_frames
        if log_callback:
            await log_callback(
                f"[Fase A] L2 source: {source_analysis.l2_unique_count} combos únicos · "
                f"peaks {source_analysis.l2_target_pqs}"
            )

        # ── Bloque 2: comparación L2 source vs L2 bin + recomendación ──
        # Tras Fase A ya tenemos los datos necesarios. Computamos:
        #   - l2_comparison: identical | different | unknown
        #   - recommended_action: keep | drop_in | merge
        # Solo informativo en este bloque — la lógica de Fase F NO se
        # modifica todavía (eso es Bloque 3). El frontend leerá estos
        # campos para mostrar el panel "🎯 Análisis y recomendación".
        l2_verdict, l2_reason = compare_l2(
            session.source_l2_combos, session.target_l2_combos
        )
        session.l2_comparison = l2_verdict
        action, action_label, action_reason = recommend_action(session)
        session.recommended_action = action
        session.recommended_action_label = action_label
        session.recommended_action_reason = action_reason
        if log_callback:
            await log_callback(
                f"[Fase A] 🎯 Comparación L2: {l2_verdict.upper()} — {l2_reason}"
            )
            await log_callback(
                f"[Fase A] 🎯 Recomendación del modelo: {action_label} — {action_reason}"
            )
    elif log_callback:
        await log_callback(
            "[Fase A] ⚠ No se pudo extraer la lista de combos L2 del source "
            "(no impide continuar — la recomendación Mantener/Inyectar se calculará "
            "con datos parciales del bin)."
        )

    if log_callback:
        await log_callback(
            f"[Fase A] ✓ RPU analizado — Profile {dovi_info.profile} ({dovi_info.el_type}), "
            f"CM {dovi_info.cm_version}, {dovi_info.frame_count} frames"
        )
        # Resumen del workflow detectado. Sin predicciones de fases futuras —
        # cuando llegue cada fase ya dirá qué va a hacer según el target real.
        workflow_summary = {
            "p7_fel": "stream dual-layer P7 FEL — preservable en drop-in o merge.",
            "p7_mel": "stream dual-layer P7 MEL — el EL no aporta tras añadir CMv4.0.",
            "p8":     "stream single-layer P8.1 — inyección directa del RPU.",
        }.get(session.source_workflow, "")
        await log_callback(
            f"[Fase A] 🎯 Resultado: workflow {workflow_label}. {workflow_summary}"
        )
    # _emit_progress(100) AL FINAL: la barra solo llega al 100% cuando todo el
    # log de cierre se ha emitido, evitando que el usuario vea "100%" antes
    # del 🎯 Resultado y crea que la fase terminó silenciosa.
    await _emit_progress(log_callback, 100, "Análisis completado")


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


# ══════════════════════════════════════════════════════════════════════
#  PRE-FLIGHT — Validación rápida del bin antes de Fase A (~12 min)
# ══════════════════════════════════════════════════════════════════════
#
# Las funciones run_phase_b_target_from_* solo se invocan después de Fase A
# (extracción del HEVC del BD, ~12 min en discos UHD). Si el bin elegido no
# tiene CMv4.0 (caso típico: bins "P5 to P8 transfer" del repo DoviTools que
# solo cambian profile sin upgrade de CM) el pipeline aborta DESPUÉS de
# haber gastado los 12 min.
#
# Estas funciones ejecutan exactamente la misma descarga/copia/extracción +
# dovi_tool info que Fase B, pero ANTES de Fase A — para cortar fast cuando
# el bin es estructuralmente incompatible (CM v2.9). El bin queda guardado
# en RPU_target.bin del workdir, así que cuando Fase B se ejecute después de
# Fase A no necesita re-descargar (short-circuit en run_phase_b_target_*).

async def preflight_target_drive(
    session: CMv40Session,
    file_id: str,
    file_name: str,
    log_callback=None,
) -> None:
    """Pre-flight: descarga un .bin del repo DoviTools y aborta si no
    tiene CMv4.0. Reutiliza el fichero en workdir para Fase B posterior."""
    from services.rec999_drive import download_file

    wd = get_workdir(session)
    rpu_target = wd / "RPU_target.bin"

    if log_callback:
        await log_callback(
            "[Pre-flight] 📋 Validación rápida del bin target ANTES de extraer "
            "el HEVC del BD. Fase A tarda ~12 min en discos UHD; el pre-flight "
            "tarda <5s y aborta si el bin no aporta CMv4.0."
        )
        await log_callback(
            f"[Pre-flight] ┌─ Descargando bin del repo DoviTools: {file_name}"
        )

    try:
        written = await download_file(file_id, rpu_target, progress_cb=None)
    except Exception as e:
        raise RuntimeError(f"Descarga del repo DoviTools falló: {e}")

    if log_callback:
        await log_callback(
            f"[Pre-flight] Descargados {written/1024/1024:.1f} MB a {rpu_target.name}"
        )

    session.target_rpu_source = "drive"
    session.target_rpu_path = f"drive://{file_id}/{file_name}"
    await _preflight_validate_bin(session, rpu_target, log_callback)


async def preflight_target_path(
    session: CMv40Session,
    rpu_path: str,
    log_callback=None,
) -> None:
    """Pre-flight: copia un .bin local al workdir y aborta si no tiene CMv4.0."""
    wd = get_workdir(session)
    rpu_target = wd / "RPU_target.bin"

    src = Path(rpu_path)
    if not src.exists():
        raise RuntimeError(f"RPU no encontrado: {rpu_path}")
    if not src.is_file() or src.suffix != ".bin":
        raise RuntimeError(f"Fichero no es un .bin válido: {rpu_path}")

    if log_callback:
        await log_callback(
            "[Pre-flight] 📋 Validación rápida del bin target ANTES de extraer "
            "el HEVC del BD."
        )
        await log_callback(f"[Pre-flight] ┌─ Copiando bin local: {src.name}")

    shutil.copy2(src, rpu_target)

    session.target_rpu_source = "path"
    session.target_rpu_path = str(src)
    await _preflight_validate_bin(session, rpu_target, log_callback)


async def preflight_target_mkv(
    session: CMv40Session,
    source_mkv_path: str,
    log_callback=None,
    proc_callback=None,
) -> None:
    """Pre-flight: extrae el RPU de otro MKV y aborta si no tiene CMv4.0.
    Más lento que drive/path (~30s-2min) pero igual ahorra los ~12 min de
    Fase A si el MKV no tiene CMv4.0."""
    wd = get_workdir(session)
    rpu_target = wd / "RPU_target.bin"
    temp_hevc = wd / "_target_source.hevc"

    if not Path(source_mkv_path).exists():
        raise RuntimeError(f"MKV no encontrado: {source_mkv_path}")

    duration = await _probe_duration(source_mkv_path)
    W_FFMPEG, W_RPU = 60.0, 35.0

    try:
        if log_callback:
            await log_callback(
                "[Pre-flight] 📋 Validación rápida del bin target ANTES de extraer "
                "el HEVC del BD."
            )
            await log_callback(
                f"[Pre-flight] ┌─ Extrayendo HEVC del MKV target: {Path(source_mkv_path).name}"
            )

        # Aquí el HEVC no interesa, solo su RPU: va directo por un pipe sin
        # tocar disco. Antes se escribían decenas de GB para releerlos y
        # borrarlos a continuación.
        piped = await _ffmpeg_extract_rpu_piped(
            source_mkv_path, rpu_target, hevc_out=None,
            duration=duration, log_callback=log_callback,
            proc_callback=proc_callback,
            offset=0.0, weight=W_FFMPEG + W_RPU,
            label="Pre-flight: extrayendo RPU del MKV target",
            estimated_s=_estimate_from_ffmpeg(session, 1.0, FPS_FFMPEG_EXTRACT),
        )
        if not piped:
            rc = await _run_streaming([
                FFMPEG_BIN, "-y", "-i", source_mkv_path,
                "-map", "0:v:0", "-c:v", "copy",
                "-bsf:v", "hevc_mp4toannexb",
                "-f", "hevc", str(temp_hevc),
            ], log_callback=log_callback, proc_callback=proc_callback,
               progress_ctx={
                   "duration": duration, "offset": 0.0, "weight": W_FFMPEG,
                   "label": "Pre-flight: extrayendo HEVC del MKV target",
               })
            if rc != 0:
                raise RuntimeError(f"ffmpeg falló (código {rc})")

            if log_callback:
                await log_callback("[Pre-flight] Extrayendo RPU del HEVC target…")
            rc, out, err = await _run([
                DOVI_TOOL_BIN, "extract-rpu", str(temp_hevc), "-o", str(rpu_target),
            ], timeout=_adaptive_timeout(
                _estimate_from_ffmpeg(session, RATIO_EXTRACT_RPU, FPS_EXTRACT_RPU),
                floor_s=1200))
            if rc != 0:
                raise RuntimeError(f"dovi_tool extract-rpu falló: {err[:300]}")

        session.target_rpu_source = "mkv"
        session.target_rpu_path = source_mkv_path
        await _preflight_validate_bin(session, rpu_target, log_callback)
    finally:
        temp_hevc.unlink(missing_ok=True)


async def _preflight_validate_bin(
    session: CMv40Session,
    rpu_path: Path,
    log_callback=None,
) -> None:
    """Ejecuta dovi_tool info, clasifica el bin y aborta si target_type ==
    'incompatible' (sin CMv4.0). NO evalúa los gates de trust — esos
    requieren source_dv_info que aún no existe en este punto del pipeline.
    Solo el check 'tiene CMv4.0' que es estructural y rápido. Fase B
    re-correrá el análisis completo con los gates después de Fase A."""
    try:
        session.target_rpu_sha256 = await asyncio.to_thread(compute_file_sha256, rpu_path)
        if log_callback:
            await log_callback(
                f"[Pre-flight] SHA-256 del bin: {session.target_rpu_sha256[:12]}…"
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
    session.target_type = _classify_target_type(dovi_info)

    if log_callback:
        await log_callback(
            f"[Pre-flight] Bin analizado — Profile {dovi_info.profile}"
            f"{' (' + dovi_info.el_type + ')' if dovi_info.el_type else ''}, "
            f"CM {dovi_info.cm_version}, {dovi_info.frame_count} frames, "
            f"L8={'sí' if dovi_info.has_l8 else 'no'}"
        )

    # Hard abort: bin sin CMv4.0 — punto muerto absoluto. Limpia el .bin
    # descargado para no dejar basura y para forzar re-download si el
    # usuario reintenta con el mismo target.
    if session.target_type == "incompatible":
        cm = dovi_info.cm_version or "desconocido"
        abort_msg = (
            f"El bin target no aporta CMv4.0 (CM {cm}). No hay metadata "
            f"L8-L11 que transferir al RPU del BD — este pipeline solo "
            f"puede inyectar CMv4.0 sobre CMv2.9, no v2.9 sobre v2.9. "
            f"Causa típica: bins 'P5 to P8 transfer' del repo DoviTools que "
            f"solo cambian de profile sin upgrade de CM. Busca otro bin con "
            f"'CMv4.0' / 'v4.0' / 'CMv4 transfer' en el nombre, o extrae de "
            f"un MKV cuyo mkvinfo muestre 'dv_cm_version: v4.0'. Pre-flight "
            f"ha ahorrado la extracción del HEVC del BD (~12 min)."
        )
        session.compat_warning = abort_msg
        if log_callback:
            await log_callback(f"[Pre-flight] ⛔ {abort_msg}")
        try:
            rpu_path.unlink(missing_ok=True)
        except OSError:
            pass
        session.target_dv_info = None
        session.target_frame_count = 0
        session.target_rpu_sha256 = ""
        session.target_rpu_source = ""
        session.target_rpu_path = ""
        session.target_type = ""
        raise RuntimeError(abort_msg)

    if log_callback:
        if session.target_type.startswith("trusted_"):
            await log_callback(
                f"[Pre-flight] ✓ Bin válido — clasificado como {session.target_type}."
            )
        else:
            await log_callback(
                f"[Pre-flight] ✓ Bin válido (CM v4.0) pero clasificado como "
                f"{session.target_type} — los gates completos se evaluarán en "
                f"Fase B tras conocer el source."
            )


def _bin_already_cached(
    session: CMv40Session,
    expected_source: str,
    expected_path: str,
    rpu_target: Path,
) -> bool:
    """True si el bin ya fue descargado/copiado/extraído por un preflight
    previo y coincide con la petición actual. Permite que Fase B salte el
    paso lento (download/copy/extract) y solo re-corra la analyze para
    obtener los trust gates con datos del source ya disponibles."""
    if not rpu_target.exists() or rpu_target.stat().st_size < 1024:
        return False
    if session.target_rpu_source != expected_source:
        return False
    if session.target_rpu_path != expected_path:
        return False
    return True


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

    if _bin_already_cached(session, "path", str(src), rpu_target):
        if log_callback:
            await log_callback(
                f"[Fase B] ✓ Bin ya copiado por pre-flight ({rpu_target.stat().st_size/1024/1024:.1f} MB) "
                f"— evaluando trust gates con datos del source."
            )
        await _emit_progress(log_callback, 70, "Re-analizando con datos del source")
        await _analyze_target_rpu(session, rpu_target, log_callback)
        await _emit_progress(log_callback, 100, "Completado")
        return

    await _emit_progress(log_callback, 0, f"Copiando RPU target: {src.name}")
    if log_callback:
        await log_callback(
            "[Fase B] 📋 Plan: copiar el RPU target desde carpeta local al "
            "workdir y re-evaluar los trust gates ahora que tenemos los datos "
            "del source (frame count, L5, L6). Si los gates pasan → auto-"
            "pipeline; si divergen → pausa en Fase D para revisar el chart."
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

    expected_path = f"drive://{file_id}/{file_name}"
    if _bin_already_cached(session, "drive", expected_path, rpu_target):
        if log_callback:
            await log_callback(
                f"[Fase B] ✓ Bin ya descargado por pre-flight ({rpu_target.stat().st_size/1024/1024:.1f} MB) "
                f"— evaluando trust gates con datos del source."
            )
        await _emit_progress(log_callback, 70, "Re-analizando con datos del source")
        await _analyze_target_rpu(session, rpu_target, log_callback)
        await _emit_progress(log_callback, 100, "Completado")
        return

    await _emit_progress(log_callback, 0, f"Descargando del repositorio: {file_name}")
    if log_callback:
        await log_callback(
            "[Fase B] 📋 Plan: descargar el RPU target del repositorio público "
            "DoviTools (Google Drive) y re-evaluar los trust gates ahora que "
            "tenemos los datos del source. Si los gates pasan → auto-pipeline; "
            "si algún crítico falla → pausa en Fase D para revisar el chart."
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

    if _bin_already_cached(session, "mkv", source_mkv_path, rpu_target):
        if log_callback:
            await log_callback(
                f"[Fase B] ✓ RPU ya extraído por pre-flight ({rpu_target.stat().st_size/1024/1024:.1f} MB) "
                f"— evaluando trust gates con datos del source."
            )
        await _emit_progress(log_callback, 70, "Re-analizando con datos del source")
        await _analyze_target_rpu(session, rpu_target, log_callback)
        await _emit_progress(log_callback, 100, "Completado")
        return

    # Pesos: ffmpeg 50% · extract-rpu 45% · info 5%
    W_FFMPEG, W_RPU = 50.0, 45.0
    duration    = await _probe_duration(source_mkv_path)
    frame_count = await _probe_frame_count(source_mkv_path)

    try:
        await _emit_progress(log_callback, 0, "Extrayendo HEVC del MKV target")
        if log_callback:
            await log_callback(
                "[Fase B] 📋 Plan: extraer el RPU CMv4.0 de un MKV propio que ya "
                "tiene el grading que quieres aplicar (p. ej. WEB-DL moderno) y "
                "evaluar los trust gates contra el source. Esta ruta suele pasar "
                "por Fase D manual porque no hay pre-validación comunitaria que "
                "garantice la alineación frame-a-frame con el Blu-ray."
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
           timeout=_adaptive_timeout(est_rpu, floor_s=1200), label="Extrayendo RPU del HEVC target",
           progress_input=temp_hevc,
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

    # Refinamiento del gate L5 con muestreo per-frame: el summary de
    # dovi_tool reporta una sola entrada L5 (frame 0 típicamente), lo que
    # genera falsos positivos en pelis con L5 variable (iMAX expanded,
    # split-aspect, REPACK). Si el gate estático dispara ack_required,
    # muestreamos N frames en ambos RPUs y reclasificamos según patrones
    # reales de active area. RPU_source.bin vive en el mismo workdir que
    # RPU_target.bin (rpu_path.parent), generado en Fase A.
    rpu_source_path = rpu_path.parent / "RPU_source.bin"
    if rpu_source_path.exists():
        await _refine_l5_gate_with_sampling(
            gates, rpu_source_path, rpu_path,
            session.source_frame_count, session.target_frame_count,
            log_callback,
        )

    session.target_trust_gates = gates
    # trust_ok se recalcula tras el refinamiento (puede haber cambiado l5_div)
    trust_ok = all(
        g.get("ok", False) for g in gates.values()
        if isinstance(g, dict) and g.get("critical", False)
    )
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
                        "bin P7 FEL CMv4.0 + source P7 FEL → drop-in viable. "
                        "Sin demux ni revisión visual; inyección directa sobre "
                        "source.hevc (BL+EL intactos). Ahorro ~90 GB de I/O."
                    )
                else:
                    implication = (
                        f"bin P7 FEL CMv4.0 + source {sw.upper()} → no drop-in "
                        f"(profiles distintos). Merge selectivo de levels CMv4.0 "
                        f"sobre el RPU del source preservando su estructura "
                        f"single-layer. Resultado: P8.1 CMv4.0."
                    )
            elif session.target_type == "trusted_p7_mel_final":
                if sw == "p7_mel":
                    implication = (
                        "bin P7 MEL CMv4.0 + source P7 MEL → mismo profile. "
                        "Sin revisión visual; el EL MEL se descarta y el RPU "
                        "target se inyecta directamente en BL → P8.1 CMv4.0."
                    )
                elif sw == "p7_fel":
                    implication = (
                        "bin P7 MEL CMv4.0 + source P7 FEL → merge CMv4.0 sobre "
                        "el RPU P7 del source preservando la FEL. Resultado: "
                        "P7 FEL CMv4.0."
                    )
                else:  # p8
                    implication = (
                        "bin P7 MEL CMv4.0 + source P8.1 → merge selectivo de los "
                        "levels CMv4.0 del target en el RPU P8 del source "
                        "(allow_cmv4_transfer). Resultado: P8.1 CMv4.0."
                    )
            elif session.target_type == "trusted_p8_source":
                if sw == "p7_fel":
                    implication = (
                        "bin P8 retail CMv4.0 + source P7 FEL → merge CMv4.0 "
                        "sobre el RPU P7 preservando la FEL. Resultado: "
                        "P7 FEL CMv4.0."
                    )
                elif sw == "p7_mel":
                    implication = (
                        "bin P8 retail CMv4.0 + source P7 MEL → el EL MEL se "
                        "descarta y el RPU P8 se inyecta directamente sobre BL. "
                        "Resultado: P8.1 CMv4.0."
                    )
                else:  # p8
                    implication = (
                        "bin P8 retail CMv4.0 + source P8.1 → mismo profile. "
                        "Inyección directa del RPU target sobre source.hevc. "
                        "Resultado: P8.1 CMv4.0 refinado."
                    )
            else:
                implication = "se salta la revisión manual del chart de sincronización."
            await log_callback(
                f"[Fase B] 🎯 Resultado: target clasificado como {session.target_type} "
                f"— TRUSTED ✓ gates OK. {implication}"
            )
        else:
            crit_fail = any(gates.get(k, {}).get("critical") and not gates.get(k, {}).get("ok")
                            for k in gates if isinstance(gates.get(k), dict))
            # ¿Hay gates con severity ack_required? — escalada distinta a la
            # de los gates críticos: requieren confirmación explícita del
            # usuario (no son fail-fast, pero sí pausan el auto-pipeline).
            has_ack_required = any(
                isinstance(g, dict) and g.get("severity") == "ack_required"
                for g in gates.values()
            )
            if crit_fail:
                implication = (
                    "algún gate crítico ha fallado — el pipeline se aborta. "
                    "Cambia de target o corrige sincronización manualmente."
                )
            elif has_ack_required:
                implication = (
                    "gates con degradación previsible — el pipeline se detiene "
                    "a la espera de que confirmes la decisión (ver detalles abajo)."
                )
            else:
                implication = (
                    "gates soft con avisos (divergencias no críticas) — el chart "
                    "de sincronización es revisable pero no bloquea el avance."
                )
            await log_callback(
                f"[Fase B] 🎯 Resultado: target clasificado como {session.target_type} "
                f"— NO trusted. {implication}"
            )

    # Hard aborts + ACK required tras evaluar gates — evitan gastar Fase C/D
    # en targets estructuralmente inservibles, y detienen el auto-pipeline
    # cuando hay degradación previsible que el usuario debe reconocer.
    #
    # Tres salidas:
    #   1. hard_abort  → raise RuntimeError; no hay continuación útil
    #      (target sin CMv4.0, target sin L8). El usuario solo puede
    #      cambiar de target.
    #   2. ack_required → guardamos failures en sesión, awaiting_critical_ack
    #      = True, RETURN sin error. El auto-pipeline detecta el flag y
    #      no avanza; la UI muestra banner pidiendo confirmación.
    #   3. nada bloqueante → continuamos al check de compat_source_target.
    has_hard, ack_failures, hard_failures = _classify_gate_failures(gates)

    if has_hard:
        # Caso 1: Hard abort — concatenar todos los motivos
        msgs = [f["why"] for f in hard_failures if f.get("why")]
        abort_msg = " · ".join(msgs) if msgs else "Target estructuralmente inservible."
        session.compat_warning = abort_msg
        session.pipeline_aborted = True
        if log_callback:
            for f in hard_failures:
                await log_callback(f"[Fase B] ⛔ Gate '{f['gate']}': {f['why']}")
            await log_callback(
                f"[Fase B] ⛔ Pipeline abortado — cambia el target para continuar."
            )
        raise RuntimeError(abort_msg)

    # Si una etapa ya quedó reconocida por el usuario en una iteración previa
    # (cambió el target, re-evaluó gates), no volvemos a pedir ACK.
    if ack_failures and not session.user_acknowledged_degradation:
        session.awaiting_critical_ack = True
        session.critical_gate_failures = ack_failures
        if log_callback:
            await log_callback(
                f"[Fase B] ⚠ Gates con degradación previsible: "
                f"{', '.join(f['gate'] for f in ack_failures)}. "
                f"El pipeline se detiene a la espera de confirmación del usuario."
            )
            for f in ack_failures:
                await log_callback(f"[Fase B]   • {f['gate']}: {f['why']}")
        # No raise: la sesión queda en estado válido (target_provided), pero
        # awaiting_critical_ack=True le dice al auto-pipeline que se detenga.
        return
    else:
        # Limpiar flags si veníamos de un re-análisis tras ACK previa
        session.awaiting_critical_ack = False
        session.critical_gate_failures = []

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
    metadata CMv4.0 (levels L3/L8/L9/L11) via merge. `dovi_tool editor` con
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

    Cada gate lleva un campo `severity` que resume el impacto si falla:
      - 'hard_abort'   → no hay forma de continuar útil; el bin se rechaza
                          (target sin CMv4.0, target sin L8). Solo permite
                          al usuario cambiar de target.
      - 'ack_required' → técnicamente se puede inyectar pero el resultado
                          quedará degradado de forma observable (L5 grande,
                          L6 >200 nits, L1 >20%). Pide ACK al usuario antes
                          de continuar — Fase D no puede arreglar nada.
      - 'sync_review'  → Fase D normal puede arreglarlo o el chart ayuda
                          a decidir (frames mismatch, L5 5-30 px).
      - 'warn'         → divergencia tolerable; solo informativa (L6 50-200,
                          L1 5-20%).
      - 'ok'           → gate pasa.

    `critical: True` se mantiene por compatibilidad — equivale a "severity
    distinta de 'ok'/'warn'" para gates que rompen target_trust_ok.
    """
    gates: dict = {}

    # Frame count — mismatch lo arregla Fase D (sync correction)
    frames_ok = source_frames > 0 and source_frames == target_frames
    gates["frames"] = {
        "ok": frames_ok,
        "bd": source_frames,
        "target": target_frames,
        "critical": True,
        "severity": "ok" if frames_ok else "sync_review",
        "why": "" if frames_ok else (
            f"Δ {target_frames - source_frames:+d} frames vs source — "
            f"Fase D usa cross-correlation y permite corregir manualmente."
        ),
    }

    # CM version — sin v4.0 no hay nada que transferir, hard abort
    cm = (target_info.cm_version or "").lower()
    cm_ok = cm in ("v4.0", "4.0")
    gates["cm_version"] = {
        "ok": cm_ok,
        "value": target_info.cm_version or "(desconocido)",
        "critical": True,
        "severity": "ok" if cm_ok else "hard_abort",
        "why": "" if cm_ok else (
            "El bin no es CMv4.0; el pipeline solo puede inyectar metadata "
            "CMv4.0 sobre source CMv2.9. Cambia de target."
        ),
    }

    # L8 presente — sin L8 el resultado sería CMv4.0-stamped pero
    # funcionalmente CMv2.9 (cero beneficio). Hard abort.
    has_l8 = bool(target_info.has_l8)
    gates["has_l8"] = {
        "ok": has_l8,
        "critical": True,
        "severity": "ok" if has_l8 else "hard_abort",
        "why": "" if has_l8 else (
            "El bin dice CMv4.0 pero no tiene trims L8 — el MKV resultante "
            "sería CMv4.0-stamped sin contenido CMv4.0 real (idéntico al "
            "original CMv2.9). El bin está mal etiquetado; cambia de target."
        ),
    }

    # Gates comparativos (solo si tenemos source_info)
    if source_info is not None:
        # L5 divergencia — distancia Chebyshev en los 4 offsets
        # ≤5 ok · 5-30 warn (Fase D para inspección) · >30 ack_required
        l5_diffs = [
            abs(source_info.l5_top    - target_info.l5_top),
            abs(source_info.l5_bottom - target_info.l5_bottom),
            abs(source_info.l5_left   - target_info.l5_left),
            abs(source_info.l5_right  - target_info.l5_right),
        ]
        l5_max = max(l5_diffs) if any(l5_diffs) else 0
        if l5_max <= 5:
            l5_sev, l5_why = "ok", ""
        elif l5_max <= 30:
            l5_sev = "sync_review"
            l5_why = (
                f"Letterbox del target diverge {l5_max} px del BD — Fase D "
                f"permite revisar el chart antes de continuar."
            )
        else:
            l5_sev = "ack_required"
            l5_why = (
                f"Letterbox del target distinto en {l5_max} px (umbral 30). "
                f"El TV calculará active-area y bandas negras según los datos "
                f"del target → tone-mapping desviado en bordes. Fase D no "
                f"puede corregir L5: solo arregla sincronización temporal."
            )
        gates["l5_div"] = {
            "ok": l5_max <= 30,
            "px_max": l5_max,
            "soft_px": 5,
            "critical_px": 30,
            "warn": 5 < l5_max <= 30,
            "critical": True,
            "severity": l5_sev,
            "why": l5_why,
        }

        # L6 MaxCLL diff — ≤50 ok · 50-200 warn · >200 ack_required
        l6_diff = abs((source_info.l6_max_cll or 0) - (target_info.l6_max_cll or 0))
        if l6_diff <= 50:
            l6_sev, l6_why = "ok", ""
        elif l6_diff <= 200:
            l6_sev = "warn"
            l6_why = (
                f"MaxCLL estático diverge {l6_diff} nits (50-200 = master "
                f"regradeado para streaming/HDR distinto, pero usable)."
            )
        else:
            l6_sev = "ack_required"
            l6_why = (
                f"MaxCLL estático diverge {l6_diff} nits (umbral 200). El "
                f"target fue gradeado para un display de pico muy distinto; "
                f"el resultado puede mostrar highlights aplastados o sobre-"
                f"saturados respecto al original."
            )
        gates["l6_div"] = {
            "ok": l6_diff <= 50,
            "nits_diff": l6_diff,
            "threshold": 50,
            "warn_threshold": 200,
            "critical": False,
            "severity": l6_sev,
            "why": l6_why,
        }

        # L1 MaxCLL diff % — ≤5 ok · 5-20 warn · >20 ack_required
        src_l1 = source_info.l1_max_cll or 0
        tgt_l1 = target_info.l1_max_cll or 0
        if src_l1 > 0 and tgt_l1 > 0:
            pct = abs(src_l1 - tgt_l1) / max(src_l1, tgt_l1) * 100.0
            if pct <= 5.0:
                l1_sev, l1_why = "ok", ""
            elif pct <= 20.0:
                l1_sev = "warn"
                l1_why = (
                    f"Brillo medio escena-a-escena diverge {pct:.1f}% (5-20% "
                    f"normal en remasters / regrade)."
                )
            else:
                l1_sev = "ack_required"
                l1_why = (
                    f"Brillo medio diverge {pct:.1f}% (umbral 20%). El master "
                    f"target representa un grading muy distinto; el resultado "
                    f"puede sentirse plano o demasiado contrastado."
                )
            gates["l1_div"] = {
                "ok": pct <= 5.0,
                "pct_diff": round(pct, 2),
                "threshold_pct": 5.0,
                "warn_threshold_pct": 20.0,
                "critical": False,
                "severity": l1_sev,
                "why": l1_why,
            }

    # trust_ok: TODOS los críticos deben pasar
    trust_ok = all(
        g.get("ok", False) for g in gates.values()
        if isinstance(g, dict) and g.get("critical", False)
    )
    return gates, trust_ok


def _extract_l5_from_frame(frame: dict) -> tuple[int, int, int, int] | None:
    """Devuelve (top, bottom, left, right) en pixels, o None si el frame
    no tiene Level5. L5 vive en:
      frame.vdr_dm_data.cmv29_metadata.ext_metadata_blocks[].Level5  (CMv2.9)
      frame.vdr_dm_data.cmv40_metadata.ext_metadata_blocks[].Level5  (CMv4.0)
    Mismo patrón que _extract_l1_from_frame para L1.
    """
    if not isinstance(frame, dict):
        return None
    vdr = frame.get("vdr_dm_data")
    if not isinstance(vdr, dict):
        # Algunas versiones de dovi_tool exportan directamente vdr_dm_data
        vdr = frame
    for key in ("cmv29_metadata", "cmv40_metadata"):
        meta = vdr.get(key)
        if not isinstance(meta, dict):
            continue
        blocks = meta.get("ext_metadata_blocks") or []
        for block in blocks:
            if isinstance(block, dict) and "Level5" in block:
                l5 = block["Level5"]
                if isinstance(l5, dict):
                    try:
                        top   = int(l5.get("active_area_top_offset")    or 0)
                        bot   = int(l5.get("active_area_bottom_offset") or 0)
                        left  = int(l5.get("active_area_left_offset")   or 0)
                        right = int(l5.get("active_area_right_offset")  or 0)
                        return (top, bot, left, right)
                    except (ValueError, TypeError):
                        pass
    return None


async def _sample_l5_per_frame(rpu_path: Path, frame_count: int,
                                 samples: int = 24,
                                 timeout: int = 180) -> list[tuple[int, tuple[int, int, int, int]]]:
    """Exporta el RPU completo a JSON via `dovi_tool export -d all=…` y
    muestrea L5 en N frames distribuidos uniformemente.

    Usamos export en vez de `info --frame` porque el formato texto de
    --frame NO incluye 'L5 offsets:' en muchas versiones de dovi_tool —
    esa línea solo aparece en `info --summary`. Con export+JSON tenemos
    L5 fiable frame-a-frame, mismo enfoque que L1 (_extract_l1_from_frame).

    Coste: ~30-60s una sola vez por RPU (no 24 calls); el JSON puede ser
    grande (~50 MB para una peli de 2h) pero se borra inmediatamente.

    Con dovi_tool >= 2.3.3 se pide SOLO el L5 (`--levels level5`): ~20 MB y
    un par de segundos, en vez de volcar el RPU entero para leer cuatro
    offsets por frame.
    """
    if frame_count <= 0:
        return []

    # Vía rápida: solo el nivel que nos interesa.
    try:
        from phases.rpu_analyze import export_levels
        levels = await export_levels(rpu_path, ("level5",), timeout=timeout)
        if levels is not None:
            rows = levels.get("level5") or []
            if rows:
                step = max(1, len(rows) // max(2, samples))
                return [
                    (i, (
                        int(rows[i].get("active_area_top_offset") or 0),
                        int(rows[i].get("active_area_bottom_offset") or 0),
                        int(rows[i].get("active_area_left_offset") or 0),
                        int(rows[i].get("active_area_right_offset") or 0),
                    ))
                    for i in range(0, len(rows), step)
                    if isinstance(rows[i], dict)
                ]
            # Sin bloques L5 en el RPU: es un resultado válido, no un fallo.
            return []
    except Exception as e:
        _logger.info("export --levels level5 no disponible (%s) — usando export completo", e)

    export_json = rpu_path.parent / f"_l5_export_{rpu_path.stem}.json"
    try:
        try:
            rc, _out, _err = await _run([
                DOVI_TOOL_BIN, "export", "-i", str(rpu_path),
                "-d", f"all={export_json}",
            ], timeout=timeout)
        except Exception as e:
            _logger.warning("dovi_tool export para L5 falló: %s", e)
            return []
        if rc != 0 or not export_json.exists():
            return []
        try:
            data = json.loads(export_json.read_text(encoding="utf-8"))
        except Exception as e:
            _logger.warning("L5 export JSON ilegible: %s", e)
            return []
    finally:
        export_json.unlink(missing_ok=True)

    # Normalizar a lista de frames (puede venir como list o dict.frames)
    if isinstance(data, list):
        frames = data
    elif isinstance(data, dict):
        frames = data.get("frames") or data.get("vdr_dm_data") or []
        if not isinstance(frames, list):
            frames = []
    else:
        frames = []
    if not frames:
        return []

    actual = len(frames)
    step = max(1, actual // max(2, samples))
    out_list: list[tuple[int, tuple[int, int, int, int]]] = []
    for i in range(0, actual, step):
        l5 = _extract_l5_from_frame(frames[i])
        if l5 is not None:
            out_list.append((i, l5))
    return out_list


def _l5_tuple_max_diff(a: tuple[int, int, int, int],
                        b: tuple[int, int, int, int]) -> int:
    """Distancia Chebyshev entre dos tuplas L5 (top/bottom/left/right)."""
    return max(abs(a[i] - b[i]) for i in range(4))


def _l5_zone_for_frame(frame: int, total: int) -> str:
    """Clasifica un frame en zona del timeline:
      - 'intro'  → primer 5% (logos, avisos del estudio)
      - 'outro'  → último 5% (créditos)
      - 'body'   → 90% central (la pelicula real)
    Las zonas intro/outro son cosméticas: divergencias de L5 ahí no
    impactan la experiencia (logos suelen tener letterbox propio que el
    transfer no respeta, y los créditos no usan tone-mapping crítico).
    """
    if total <= 0:
        return "body"
    pct = frame / total
    if pct < 0.05:
        return "intro"
    if pct >= 0.95:
        return "outro"
    return "body"


async def _refine_l5_gate_with_sampling(gates: dict,
                                          rpu_source: Path, rpu_target: Path,
                                          source_frames: int, target_frames: int,
                                          log_callback) -> None:
    """Si el gate L5 estático disparó ack_required, refina la decisión
    muestreando 24 frames de cada RPU AL MISMO frame_number en ambos lados
    y comparando frame-a-frame.

    Estrategia:
      1. Sample N frames distribuidos uniformemente en cada RPU.
      2. Pair samples por frame_number → lista de (frame, src_l5, tgt_l5, diff).
      3. Cada par marcado match (diff ≤30 px) o mismatch.
      4. Mismatches clasificados por zona: intro (primer 5%) / body (90% central) /
         outro (último 5%). Las zonas cosméticas no bloquean; solo body cuenta.
      5. Decisión:
         - body_coverage ≥ 90% → reclasifica a 'warn' (gate pasa).
         - body_coverage ≥ 70% → reclasifica a 'warn' con aviso suave.
         - body_coverage < 70% → mantiene ack_required.

    Mutates `gates["l5_div"]` in-place.
    """
    g = gates.get("l5_div")
    if not isinstance(g, dict):
        return
    if g.get("severity") != "ack_required":
        return

    if log_callback:
        await log_callback(
            "[Fase B] L5 estático sospechoso (>30 px) — muestreando 24 frames "
            "en ambos RPUs para descartar falso positivo por L5 variable…"
        )

    src_samples = await _sample_l5_per_frame(rpu_source, source_frames)
    tgt_samples = await _sample_l5_per_frame(rpu_target, target_frames)

    g["sampled_method"] = "per_frame_zoned_24"

    if not src_samples or not tgt_samples:
        if log_callback:
            await log_callback(
                "[Fase B] ⚠ Muestreo L5 no produjo datos — gate L5 sigue como "
                "ack_required basándose en el summary."
            )
        return

    # Indexar por frame_number y emparejar samples comunes (mismo step en
    # ambos lados normalmente coloca los samples en frames idénticos).
    src_by_frame = dict(src_samples)
    tgt_by_frame = dict(tgt_samples)
    common_frames = sorted(set(src_by_frame.keys()) & set(tgt_by_frame.keys()))

    if not common_frames:
        if log_callback:
            await log_callback(
                "[Fase B] ⚠ Muestreo L5: no hay frames comunes entre source y target "
                "(frame counts muy distintos) — gate L5 sigue como ack_required."
            )
        return

    # Comparar frame-a-frame
    total_frames_for_zone = max(source_frames, target_frames)
    per_sample: list[dict] = []
    for f in common_frames:
        s = src_by_frame[f]
        t = tgt_by_frame[f]
        diff = _l5_tuple_max_diff(s, t)
        per_sample.append({
            "frame": f,
            "src": list(s),
            "tgt": list(t),
            "diff_px": diff,
            "ok": diff <= 30,
            "zone": _l5_zone_for_frame(f, total_frames_for_zone),
        })

    total = len(per_sample)
    matches = sum(1 for s in per_sample if s["ok"])
    mismatches = total - matches

    # Conteos por zona
    zone_counts = {"intro": 0, "body": 0, "outro": 0}
    zone_mismatches = {"intro": 0, "body": 0, "outro": 0}
    body_mismatch_frames: list[int] = []
    for s in per_sample:
        z = s["zone"]
        zone_counts[z] += 1
        if not s["ok"]:
            zone_mismatches[z] += 1
            if z == "body":
                body_mismatch_frames.append(s["frame"])

    body_total = zone_counts["body"]
    body_matches = body_total - zone_mismatches["body"]
    body_coverage = (body_matches / body_total) if body_total > 0 else 1.0

    # Variabilidad detectada — convertimos a tuplas (s["src"]/s["tgt"] ya
    # vienen como list de la asignación previa por compatibilidad JSON).
    # Antes había una linea adicional que hacia s["src"][0:4] (list slice,
    # no hashable) y reventaba el set con 'unhashable type: list'.
    src_distinct = {tuple(s["src"]) for s in per_sample}
    tgt_distinct = {tuple(s["tgt"]) for s in per_sample}

    g["sampled_total"] = total
    g["sampled_matches"] = matches
    g["sampled_mismatches"] = mismatches
    g["sampled_zone_counts"] = zone_counts
    g["sampled_zone_mismatches"] = zone_mismatches
    g["sampled_body_coverage"] = round(body_coverage, 3)
    g["sampled_overall_coverage"] = round(matches / total, 3) if total > 0 else 0.0
    g["src_variable_l5"] = len(src_distinct) > 1
    g["tgt_variable_l5"] = len(tgt_distinct) > 1
    g["sampled_per_frame"] = per_sample  # detalle completo para UI

    # Construir explicación human-friendly
    parts: list[str] = []
    parts.append(
        f"Muestreo de {total} frames: {matches} coinciden ({round(matches/total*100)}%) "
        f"y {mismatches} divergen."
    )
    zone_msg_parts: list[str] = []
    if zone_mismatches["intro"] > 0:
        zone_msg_parts.append(
            f"{zone_mismatches['intro']}/{zone_counts['intro']} en el principio (logos/avisos, primer 5%)"
        )
    if zone_mismatches["body"] > 0:
        zone_msg_parts.append(
            f"{zone_mismatches['body']}/{zone_counts['body']} en el cuerpo principal (90% central)"
        )
    if zone_mismatches["outro"] > 0:
        zone_msg_parts.append(
            f"{zone_mismatches['outro']}/{zone_counts['outro']} al final (créditos, último 5%)"
        )
    if zone_msg_parts:
        parts.append("Discrepancias: " + " · ".join(zone_msg_parts) + ".")

    body_pct = round(body_coverage * 100)

    if body_coverage >= 0.90:
        g["ok"] = True
        g["severity"] = "warn"
        if mismatches == 0:
            extra = "El muestreo per-frame confirma que el patrón de active area es idéntico en ambos masters."
        elif zone_mismatches["body"] == 0:
            extra = (
                "Las discrepancias caen exclusivamente en zonas cosméticas "
                "(logos/intro y créditos) — el cuerpo principal coincide al 100%. "
                "Sin riesgo real."
            )
        else:
            extra = (
                f"El cuerpo principal coincide en {body_pct}%; las pocas discrepancias "
                "no comprometen la experiencia."
            )
        g["why"] = " ".join(parts) + " " + extra
        if log_callback:
            await log_callback(
                f"[Fase B] ✓ L5 reclasificado a 'warn' — body_coverage={body_pct}% "
                f"(matches {matches}/{total} total · body {body_matches}/{body_total})."
            )
    elif body_coverage >= 0.70:
        g["ok"] = True
        g["severity"] = "warn"
        g["why"] = " ".join(parts) + (
            f" El cuerpo principal coincide en {body_pct}% — tolerable pero "
            "merece la pena revisar visualmente la Fase D si quieres confirmar."
        )
        if log_callback:
            await log_callback(
                f"[Fase B] ⚠ L5 reclasificado a 'warn' tolerable — "
                f"body_coverage={body_pct}% (umbral 90% para silenciar)."
            )
    else:
        # body_coverage < 70% → ack legítimo, masters distintos en lo que importa
        sample_frames_str = ", ".join(str(f) for f in body_mismatch_frames[:6])
        if len(body_mismatch_frames) > 6:
            sample_frames_str += f" y {len(body_mismatch_frames) - 6} más"
        g["why"] = " ".join(parts) + (
            f" El cuerpo principal coincide solo en {body_pct}% (umbral 70%) — "
            f"el target tiene un patrón de active area distinto del BD en escenas "
            f"críticas (frames de ejemplo: {sample_frames_str}). "
            f"El TV calculará bandas y tone-mapping mal → resultado degradado."
        )
        if log_callback:
            await log_callback(
                f"[Fase B] ⚠ L5 confirmado divergente: body_coverage={body_pct}%. "
                f"Source distinct={sorted(src_distinct)} · Target distinct={sorted(tgt_distinct)}"
            )


def _classify_gate_failures(gates: dict) -> tuple[bool, list[dict], list[dict]]:
    """Recorre los gates y devuelve:
      - has_hard_abort: True si algún gate tiene severity 'hard_abort'
      - ack_required: lista de gates con severity 'ack_required' (cada item
        es un dict con gate/value/threshold/why para el banner)
      - hard_aborts: lista de gates con severity 'hard_abort' (mismo formato)

    Útil para que Fase B decida si abortar el pipeline (hard) o detenerse
    pidiendo ACK (recoverable).
    """
    hard_aborts: list[dict] = []
    ack_required: list[dict] = []
    for key, g in gates.items():
        if not isinstance(g, dict):
            continue
        sev = g.get("severity", "ok")
        if sev not in ("hard_abort", "ack_required"):
            continue
        item = {
            "gate": key,
            "severity": sev,
            "why": g.get("why", ""),
        }
        # Adjuntar valores específicos del gate para que el banner los muestre
        for f in ("px_max", "nits_diff", "pct_diff", "value", "bd", "target"):
            if f in g:
                item[f] = g[f]
        if sev == "hard_abort":
            hard_aborts.append(item)
        else:
            ack_required.append(item)
    return bool(hard_aborts), ack_required, hard_aborts


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
        demux_done = wd / ".demux_done"

        def _cleanup_partial_demux() -> None:
            """Borra BL/EL parciales + marcador para que el reintento sea limpio."""
            for partial in (bl_hevc, el_hevc, demux_done):
                if partial.exists():
                    try:
                        partial.unlink()
                    except OSError:
                        pass

        if _demux_output_reusable(bl_hevc, el_hevc, demux_done):
            if log_callback:
                await log_callback(
                    "[Fase C] BL.hevc y EL.hevc ya existen y el demux previo se "
                    "completó (marcador verificado), reutilizando"
                )
        else:
            # Un BL/EL sin marcador es un demux muerto a medias (truncado) —
            # nunca se reutiliza. Limpiamos cualquier parcial antes de rehacer.
            _cleanup_partial_demux()
            # Timeout PROPORCIONAL al tamaño real (no fijo): un demux de UHD
            # largo supera de sobra los 900s antiguos. Ver _adaptive_timeout.
            demux_timeout = _adaptive_timeout(est_demux)
            try:
                rc, out, err = await _run_with_time_estimate([
                    DOVI_TOOL_BIN, "demux", str(source_hevc),
                    "--bl-out", str(bl_hevc),
                    "--el-out", str(el_hevc),
                ], estimated_s=est_demux, log_callback=log_callback, proc_callback=proc_callback,
                   timeout=demux_timeout, label="Separando BL + EL (dovi_tool demux)",
                   offset=0.0, weight=W_DEMUX, progress_input=source_hevc)
            except BaseException:
                # Timeout/kill/cancel → los BL/EL a medio escribir NO deben
                # sobrevivir para el siguiente intento.
                _cleanup_partial_demux()
                raise
            if rc != 0:
                _cleanup_partial_demux()
                raise RuntimeError(f"dovi_tool demux falló (código {rc}): {err[:300]}")
            # Marcar el demux como completo (sobrevive a reinicios) → reutilización
            # segura en reintentos de fases posteriores.
            try:
                demux_done.write_text(str(session.source_frame_count or ""))
            except OSError:
                pass
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
    # 100% AL FINAL: barra llena solo cuando el log de cierre se ha emitido.
    await _emit_progress(log_callback, 100, "Completado")


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
    # Intento 0: export SOLO del L1 (dovi_tool >= 2.3.3). El chart únicamente
    # necesita max_pq/avg_pq por frame — y eso son ~8 MB y ~1 s, frente a los
    # ~680 MB y ~100 s del volcado completo del RPU. Como esto corre DOS veces
    # por Fase C (source + target), es el ahorro más directo de la fase.
    try:
        from phases.rpu_analyze import export_levels
        levels = await export_levels(
            rpu_path, ("level1",), timeout=_adaptive_timeout(est_s, floor_s=600))
        if levels is not None and levels.get("level1"):
            rows = levels["level1"]
            if log_callback:
                await log_callback(
                    f"[Fase C] L1 de {label}: {len(rows)} frames "
                    f"(export selectivo — sin volcar el RPU entero)")
            if progress_weight > 0:
                await _emit_progress(log_callback, progress_offset + progress_weight,
                                     f"Frames de {label} exportados")
            return [
                {
                    "frame": i,
                    "maxcll": float(r.get("max_pq") or 0),
                    "maxfall": float(r.get("avg_pq") or 0),
                }
                for i, r in enumerate(rows)
            ]
    except Exception as e:
        _logger.info("export --levels level1 no disponible (%s) — usando export completo", e)

    # Intento 1: dovi_tool export (versión reciente). Estimación basada en fps real.
    try:
        wd = rpu_path.parent
        export_json = wd / f"_export_{label}.json"
        # export -d all de un RPU full-movie tarda 5-15 min y ESCALA con los
        # frames; timeout adaptativo (no fijo) para no morir en NAS lentos.
        export_to = _adaptive_timeout(est_s, floor_s=1200)
        if progress_weight > 0:
            rc, out, err = await _run_with_time_estimate([
                DOVI_TOOL_BIN, "export", "-i", str(rpu_path),
                "-d", f"all={export_json}",
            ], estimated_s=est_s, log_callback=log_callback, timeout=export_to,
               label=f"Exportando frames {label}", progress_input=rpu_path,
               offset=progress_offset, weight=progress_weight)
        else:
            rc, out, err = await _run([
                DOVI_TOOL_BIN, "export", "-i", str(rpu_path),
                "-d", f"all={export_json}",
            ], timeout=export_to)
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

    # Resumen de la corrección aplicada para que el log refleje qué cambió.
    # Útil al releer el log de una sesión con varias iteraciones de Fase E.
    if log_callback:
        ops_parts = []
        if editor_config.get("remove"):
            ops_parts.append(f"remove {len(editor_config['remove'])} rangos")
        if editor_config.get("duplicate"):
            ops_parts.append(f"duplicate {len(editor_config['duplicate'])} rangos")
        ops_summary = ", ".join(ops_parts) if ops_parts else "sin cambios"
        sync_status = (
            "sync perfecto (Δ=0)" if session.sync_delta == 0
            else f"Δ = {session.sync_delta:+d} frames respecto al source"
        )
        await log_callback(
            f"[Fase E] 🎯 Resultado: corrección aplicada ({ops_summary}). "
            f"RPU corregido en RPU_synced.bin — {sync_status}."
        )


# ══════════════════════════════════════════════════════════════════════
#  FASE F — Inyectar RPU en EL
# ══════════════════════════════════════════════════════════════════════

async def _ensure_profile8_rpu(rpu_path: Path, wd: Path, log_callback=None) -> Path:
    """Convierte el RPU a Profile 8.1 si aún declara Profile 7.

    Necesario en los workflows que producen un HEVC **single-layer** (p7_mel
    descarta el EL; p8 nunca lo tuvo). Si el RPU inyectado sigue declarando
    Profile 7, MediaInfo lee el resultado como ``dvhe.07 / BL+EL+RPU`` — un
    fichero que se anuncia como dual-layer sin tener capa de mejora, así que
    un reproductor DV espera una EL que no existe.

    Caso real ("Te van a matar", 2026-08-15): el merge conservaba el Profile 7
    MEL del source, el mux remuxaba solo la BL, y el MKV final quedaba con la
    señalización del origen pese a la pista llamarse "P8.1 CMv4.0". Un
    comentario del código afirmaba que "el reproductor lo lee como
    P8.1-equivalente"; MediaInfo demuestra que no.

    Usa ``dovi_tool editor`` con ``{"mode": 2}`` — "Converts the RPU to be
    profile 8.1 compatible" — que preserva CM version, frame count y scene
    cuts (verificado sobre el RPU real: P7 MEL → P8, 136033 frames y 1101
    escenas intactos).

    Devuelve el path del RPU a inyectar (el convertido, o el original si ya
    era Profile 8 o si la conversión falla — en ese caso se avisa y se sigue,
    porque el contenido de imagen es correcto igualmente).
    """
    rc, summary, _err = await _run(
        [DOVI_TOOL_BIN, "info", "--summary", str(rpu_path)], timeout=60)
    info = _parse_dovi_summary(summary)
    if info.profile == 8:
        return rpu_path

    if log_callback:
        await log_callback(
            f"[Fase F] El RPU declara Profile {info.profile}"
            f"{' ' + info.el_type if info.el_type else ''} pero el stream de "
            f"salida es single-layer → convirtiendo el RPU a Profile 8.1 "
            f"(dovi_tool editor, mode 2) para que la señalización case con el "
            f"contenido."
        )

    config_path = wd / "_profile8_mode.json"
    config_path.write_text(json.dumps({"mode": 2}), encoding="utf-8")
    converted = wd / f"{rpu_path.stem}_p81.bin"
    rc, _out, err = await _run([
        DOVI_TOOL_BIN, "editor",
        "-i", str(rpu_path),
        "-j", str(config_path),
        "-o", str(converted),
    ], timeout=1800)
    if rc != 0 or not converted.exists() or converted.stat().st_size < 1000:
        if log_callback:
            await log_callback(
                f"[Fase F] ⚠ La conversión a Profile 8.1 falló ({err[:150]}) — "
                f"se inyecta el RPU original. El vídeo será correcto, pero el "
                f"fichero se anunciará como dual-layer sin tener capa de mejora."
            )
        return rpu_path

    rc, summary_after, _err = await _run(
        [DOVI_TOOL_BIN, "info", "--summary", str(converted)], timeout=60)
    after = _parse_dovi_summary(summary_after)
    if after.frame_count != info.frame_count:
        if log_callback:
            await log_callback(
                f"[Fase F] ⚠ La conversión cambió el frame count "
                f"({info.frame_count} → {after.frame_count}) — se descarta y "
                f"se inyecta el RPU original."
            )
        return rpu_path

    if log_callback:
        await log_callback(
            f"[Fase F] ✓ RPU convertido a Profile {after.profile} "
            f"(CM {after.cm_version}, {after.frame_count} frames, "
            f"{after.scene_count} escenas — sin pérdida de metadata)."
        )
    return converted


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
    # Esta fase regenera el HEVC inyectado, así que el RPU que Fase G pudiera
    # haber adelantado para la validación de Fase H (ver
    # _prewarm_validation_rpu) deja de corresponderse con él. Se borra aquí
    # para que nadie valide un stream contra el RPU de una pasada anterior:
    # el frame count coincidiría y la comprobación no lo detectaría.
    (wd / "_validate_full_rpu.bin").unlink(missing_ok=True)
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
                    "levels CMv4.0 del target (L3/L8/L9/L11) en el RPU P8 del source. El output "
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
    # El 📋 Plan anterior ya dice qué se va a hacer en cada rama; aquí solo
    # se ejecuta sin repetir el mensaje. Las sub-llamadas (_merge_cmv40_into_p7
    # y _run_streaming del inject-rpu más abajo) emiten su propio detalle
    # cuando arrancan.
    #
    # ── Pesos de progreso: si hay merge previo, le damos MERGE_WEIGHT% del
    # peso de la fase y el inject ocupa el resto. Sin merge (drop-in / direct
    # inject), el inject ocupa el 100%. Asi la barra es monotonica (no salta
    # del 50% al 0% como ocurria al hacer `_emit_progress(0, inject_label)`
    # tras un merge que no reportaba progreso).
    #
    # Calibrado a 15%: dovi_tool editor sobre el RPU (~30-90s en UHD) es ~10%
    # del tiempo de fase; inject-rpu sobre HEVC entero (~5-15 min) el resto.
    # 15% deja margen para que se vea el "saltito" del merge sin desviar
    # mucho del tiempo real.
    MERGE_WEIGHT = 15.0
    had_merge = False

    async def _do_merge():
        """Ejecuta el merge con barra cubriendo [0, MERGE_WEIGHT]."""
        nonlocal had_merge
        await _emit_progress(
            log_callback, 0,
            "Mergeando CMv4.0 levels en RPU source (dovi_tool editor)…"
        )
        await _merge_cmv40_into_p7(
            rpu_source_p7=rpu_source,
            rpu_target_v40=rpu_target_effective,
            output=rpu_merged,
            log_callback=log_callback,
        )
        had_merge = True
        await _emit_progress(log_callback, MERGE_WEIGHT, "RPU merged listo")

    if drop_in_fel:
        rpu_to_inject = rpu_target_effective
        hevc_input    = source_hevc
        hevc_output   = source_injected
        inject_label  = "Inyectando RPU trusted directo sobre BL+EL (drop-in)"
        # Marcar fase omitida para UI/log
        if "merge_cmv40_transfer" not in session.phases_skipped:
            session.phases_skipped.append("merge_cmv40_transfer")
    elif workflow == "p7_fel":
        # Merge preservando FEL (flujo clásico)
        await _do_merge()
        rpu_to_inject = rpu_merged
        hevc_input    = el_hevc
        hevc_output   = el_injected
        inject_label  = "Inyectando RPU merged en EL (preserva FEL)"
    elif workflow == "p7_mel":
        # P7 MEL: tras demux quedamos con BL single-layer (EL MEL descartada).
        # Si el target es P8 (trusted_p8_source): inject directo — el P8 RPU
        # encaja sobre el BL y produce un HEVC P8.1 CMv4.0.
        # Si el target es P7 (fel o mel) o generic: mergeamos los levels CMv4.0
        # del target en el RPU P7 MEL del source. El RPU resultante hereda el
        # Profile 7 del source, así que ANTES de inyectarlo hay que convertirlo
        # a Profile 8.1 (ver `_ensure_profile8_rpu`): sin eso el fichero queda
        # sin capa de mejora pero anunciándose como dual-layer.
        target_needs_merge = session.target_type in (
            "trusted_p7_fel_final", "trusted_p7_mel_final", "generic",
        )
        if target_needs_merge:
            await _do_merge()
            rpu_to_inject = rpu_merged
            inject_label  = "Inyectando RPU merged en BL (MEL descartado → P8.1 CMv4.0)"
        else:
            rpu_to_inject = rpu_target_effective
            inject_label  = "Inyectando RPU target en BL (MEL descartado → P8.1)"
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
            await _do_merge()
            rpu_to_inject = rpu_merged
            inject_label  = "Inyectando RPU merged en source.hevc (P8.1 CMv4.0)"
        else:
            rpu_to_inject = rpu_target_effective
            inject_label  = "Inyectando RPU target en source.hevc (P8 → P8.1 CMv4.0)"
        hevc_input    = source_hevc
        hevc_output   = bl_injected  # reutilizamos el slot de artefacto

    # Los workflows single-layer (p7_mel y p8) producen un HEVC SIN capa de
    # mejora. Si el RPU que vamos a inyectar declara Profile 7, el fichero
    # final se anuncia como dual-layer (dvhe.07 / BL+EL+RPU) sin tener EL:
    # un reproductor DV espera una capa que no existe. Convertimos el RPU a
    # Profile 8.1 para que la señalización case con el contenido real.
    if workflow in ("p7_mel", "p8"):
        rpu_to_inject = await _ensure_profile8_rpu(
            rpu_to_inject, wd, log_callback)

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
    # Inject empieza donde el merge dejó la barra (MERGE_WEIGHT) o desde 0%
    # si no hubo merge. Monotónico.
    inject_offset = MERGE_WEIGHT if had_merge else 0.0
    inject_weight = 100.0 - inject_offset
    await _emit_progress(log_callback, inject_offset, inject_label)
    rc = await _run_streaming([
        DOVI_TOOL_BIN, "inject-rpu",
        "-i", str(hevc_input),
        "--rpu-in", str(rpu_to_inject),
        "-o", str(hevc_output),
    ], log_callback=log_callback, proc_callback=proc_callback,
       progress_ctx={
           "time_estimate_s": est_inject,
           "offset": inject_offset, "weight": inject_weight,
           "label": inject_label,
           "input_path": hevc_input,
           # inject-rpu recorre la entrada dos veces; el fichero de salida,
           # en cambio, solo crece. Pesa lo mismo que la entrada salvo los
           # pocos MB del RPU.
           "output_path": hevc_output,
           "expected_out_bytes": _tamano(hevc_input),
           # Dos pasadas sobre la entrada: una para el orden de frames y otra
           # reescribiendo. Los bytes leídos acumulados son la única señal
           # continua que cubre las dos — ver _ReadProgress. Comprobado con el
           # proceso real a mitad de la pasada 2: rchar 108,97 GB = 73,2 de la
           # entrada + 35,66 ya reescritos.
           "expected_read_bytes": 2 * _tamano(hevc_input),
       })
    if rc != 0:
        raise RuntimeError(f"dovi_tool inject-rpu falló (código {rc})")

    if log_callback:
        await log_callback(f"[Fase F] ✓ HEVC con RPU inyectado generado: {hevc_output.name} (workflow {workflow})")
        # Descripción del artefacto generado (sin prometer qué hará Fase G —
        # cuando Fase G arranque emitirá su propio 📋 Plan según el artefacto
        # que encuentre en el workdir).
        if drop_in_fel:
            artifact_desc = (
                "BL+EL intactos con el RPU CMv4.0 inyectado — stream dual-layer "
                "íntegro listo para multiplexar."
            )
        elif workflow == "p7_fel":
            artifact_desc = (
                "EL con el RPU merged inyectado; BL.hevc original sin tocar — "
                "stream dual-layer P7 FEL listo para combinar."
            )
        elif workflow == "p7_mel":
            artifact_desc = (
                "BL con el RPU inyectado (EL MEL descartado) — stream single-layer "
                "P8.1 CMv4.0 listo para remuxar."
            )
        else:  # p8
            artifact_desc = (
                "HEVC single-layer con el RPU CMv4.0 inyectado — listo para "
                "remuxar."
            )
        await log_callback(f"[Fase F] 🎯 Resultado: RPU CMv4.0 integrado en el stream. {artifact_desc}")
    # 100% AL FINAL: barra llena solo cuando el log de cierre se ha emitido.
    await _emit_progress(log_callback, 100, "RPU inyectado")


async def _merge_cmv40_into_p7(
    rpu_source_p7: Path,
    rpu_target_v40: Path,
    output: Path,
    log_callback=None,
) -> None:
    """Transfiere los niveles CMv4.0 del RPU target al RPU source, preservando
    la estructura (profile + el_type) del source. Funciona para P7 FEL, P7 MEL
    y P8 sources — `allow_cmv4_transfer` de dovi_tool solo copia levels, no
    altera la profile/subprofile del input.

    Usa la primitiva nativa de dovi_tool ``allow_cmv4_transfer`` que transfiere
    los niveles especificados frame-a-frame desde ``source_rpu`` hacia el RPU
    input del editor. L254 se añade implícitamente con valor default.

    La lista de levels depende del source workflow (alineado con
    bbeny123/remuxer.sh — la implementación de referencia más usada):

      * **P7 FEL source** (línea 2090 de remuxer.sh, "FEL detected override"):
        ``[1, 2, 3, 6, 8, 9, 10, 11, 254]``
        Bbeny123 transfiere L1/L2/L6 deliberadamente cuando el source es FEL,
        porque el L1 del BD captura las stats del BL+EL combinado y a veces
        está desactualizado respecto al WEB grading restaurado. Los bins del
        repo DoviTools "P7 FEL restored CMv4.0" están pensados para esto.

      * **P7 MEL / P8 source** (default global remuxer.sh línea 31):
        ``[3, 8, 9, 11, 254]``
        Sin EL real que combinar, el L1/L2/L6 del BD describe correctamente
        los píxeles y debe preservarse. Solo se transfieren los levels
        EXCLUSIVOS de CMv4.0 (L8, L9, L11) + L3 (PQ adj) + L254 (marker).
        Coincide con la docs oficial de dovi_tool: "Allows transferring
        CM v4.0 levels (L3, L8, L9, L10, L11, L254) to CM v2.9 RPU".

    Config JSON resultante (caso no-FEL):
        {
          "source_rpu": "/abs/path/RPU_target_v40.bin",
          "rpu_levels": [3, 8, 9, 11, 254],
          "allow_cmv4_transfer": true
        }

    Resultado:
      - Estructura del source preservada (profile + el_type + BL/EL info)
      - L1/L2/L5/L6 del BD preservados si source es MEL/P8;
        L1/L2/L6 transferidos del target si source es FEL
      - L254 añadido implícitamente por dovi_tool (marca CMv4.0)
    """
    # ── Pre-check: frame count debe coincidir + capturar profile de entrada ─
    rc_s, sum_s, _ = await _run([
        DOVI_TOOL_BIN, "info", "-s", "-i", str(rpu_source_p7),
    ], timeout=30)
    rc_t, sum_t, _ = await _run([
        DOVI_TOOL_BIN, "info", "-s", "-i", str(rpu_target_v40),
    ], timeout=30)
    source_info = _parse_dovi_summary(sum_s) if rc_s == 0 else None
    target_info = _parse_dovi_summary(sum_t) if rc_t == 0 else None
    frames_bd = source_info.frame_count if source_info else 0
    frames_tgt = target_info.frame_count if target_info else 0
    if frames_bd == 0 or frames_tgt == 0:
        raise RuntimeError("No se pudo leer frame count de uno de los RPUs con dovi_tool info")
    if frames_bd != frames_tgt:
        raise RuntimeError(
            f"Frame count mismatch ANTES del merge CMv4.0:\n"
            f"  RPU source:   {frames_bd} frames\n"
            f"  RPU target:   {frames_tgt} frames\n"
            f"Diferencia: {frames_tgt - frames_bd:+d}\n\n"
            f"→ Vuelve a la Fase D («Verificar sincronización») y aplica la corrección "
            f"(remove/duplicate) hasta que Δ = 0. Después reanuda la inyección."
        )
    # Profile/el_type del source ANTES del merge — se usa en la verificacion
    # post-merge para confirmar que el transfer NO altero la estructura.
    # El output hereda profile/el_type del INPUT, asi que estos son los
    # valores esperados independientemente de la profile del target.
    expected_profile = source_info.profile if source_info else 0
    expected_el_type = source_info.el_type if source_info else ""
    if log_callback:
        el_label = f" {expected_el_type}" if expected_el_type else ""
        await log_callback(
            f"[Fase F] Frame counts OK: source={frames_bd}, target={frames_tgt} (match). "
            f"Profile source: P{expected_profile}{el_label} (se preserva en el output)"
        )

    # ── Merge CMv4.0 sobre RPU CMv2.9 (workflow MODE.F 2-3 de DoviScripts) ──
    # Lista de levels según source workflow (alineado con bbeny123/remuxer.sh).
    # Detección por `expected_el_type` capturado del RPU source — es la marca
    # autoritativa: el header del RPU dice si es FEL o no, independientemente
    # del label que la sesión arrastre.
    is_fel_source = (expected_el_type or "").upper() == "FEL"
    if is_fel_source:
        # Lista FEL: transferimos L1/L2/L6 también porque el L1 del BD FEL
        # captura el BL+EL combinado y a veces está desactualizado respecto
        # al WEB grading restaurado. Coincide con override FEL de
        # bbeny123/remuxer.sh línea 2090.
        levels = [1, 2, 3, 6, 8, 9, 10, 11, 254]
        levels_label = "FEL [1,2,3,6,8,9,10,11,254]"
        preserve_note = "L5 preservado del BD"
    else:
        # Lista default conservadora para MEL / P8: solo levels CMv4.0-exclusivos
        # + L3 + marker. L1/L2/L5/L6 del BD se quedan (describen sus píxeles).
        # Coincide con default global de bbeny123/remuxer.sh línea 31 y con la
        # docs oficial de dovi_tool.
        levels = [3, 8, 9, 11, 254]
        levels_label = "[3,8,9,11,254]"
        preserve_note = "L1/L2/L5/L6 preservados del BD"

    # NOTA: el campo `add_cmv4_default_metadata` documentado en docs/editor.md
    # de dovi_tool main está pendiente de liberar — no aparece en ninguna
    # release publicada (la última es 2.3.2, que es la que el contenedor usa).
    # En cuanto dovi_tool publique una release que lo soporte, podemos volver
    # a añadir la rama que rellena L11 default cuando el target carece de él.
    # Por ahora, si el target sin L11 → output sin L11 (CMv4.0 válido, sin
    # Content Type explícito; los displays HDR aplican preset default).
    wd = rpu_source_p7.parent
    cfg_path = wd / "_merge_cmv4_transfer.json"
    cfg: dict = {
        "allow_cmv4_transfer": True,
        "source_rpu": str(rpu_target_v40.resolve()),
        "rpu_levels": levels,
    }
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    target_lacks_l11 = bool(target_info and not target_info.has_l11)
    if log_callback:
        src_label = f"P{expected_profile}{(' ' + expected_el_type) if expected_el_type else ''}"
        l11_note = (
            " · ⚠ target sin L11 → el output quedará sin Content Type "
            "(válido como CMv4.0; los displays HDR aplicarán preset default)"
            if target_lacks_l11 else ""
        )
        await log_callback(
            f"[Fase F] Transferencia CMv4.0 levels {levels_label} frame-a-frame "
            f"desde {rpu_target_v40.name} → RPU {src_label} del source "
            f"({preserve_note}{l11_note})…"
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
    # El allow_cmv4_transfer copia levels (L3/L8/L9/L11) del target al source
    # preservando la ESTRUCTURA del source (profile + el_type). Por eso
    # comparamos contra los valores capturados antes del merge, no contra
    # una expectativa fija como 'FEL'. Asi la funcion sirve para P7 FEL,
    # P7 MEL y P8 sources indistintamente.
    rc, summary, _ = await _run([
        DOVI_TOOL_BIN, "info", "-s", "-i", str(output),
    ], timeout=30)
    if rc != 0:
        raise RuntimeError("No se pudo leer el RPU merged con dovi_tool info")
    result_info = _parse_dovi_summary(summary)

    errors: list[str] = []
    if expected_profile and result_info.profile != expected_profile:
        errors.append(
            f"profile={result_info.profile} (esperado {expected_profile} — "
            f"el transfer alteró la profile del source, deberia haber sido idempotente)"
        )
    if result_info.el_type != expected_el_type:
        errors.append(
            f"el_type={result_info.el_type!r} (esperado {expected_el_type!r} — "
            f"el transfer alteró la estructura de capas del source)"
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
        src_label = f"P{expected_profile}{(' ' + expected_el_type) if expected_el_type else ''}"
        raise RuntimeError(
            f"Verificación post-merge falló. El RPU resultante no es un {src_label} CMv4.0 válido:\n  - "
            + "\n  - ".join(errors)
            + "\n\nSe aborta la inyección para no generar un MKV incorrecto."
        )

    if log_callback:
        el_label = f" ({result_info.el_type})" if result_info.el_type else ""
        await log_callback(
            f"[Fase F] ✓ Merge verificado: Profile {result_info.profile}{el_label}, "
            f"CM {result_info.cm_version}, {result_info.frame_count} frames, L8 presente"
        )


# ══════════════════════════════════════════════════════════════════════
#  FASE G — Remux final (dovi_tool mux + mkvmerge)
# ══════════════════════════════════════════════════════════════════════

async def _prewarm_validation_rpu(
    session: CMv40Session,
    pre_mux_hevc: Path,
    out_rpu: Path,
    log_callback=None,
) -> bool:
    """Extrae el RPU que la Fase H usará para validar, mientras el remux corre.

    Va sin `log_callback` al subproceso a propósito: sus líneas se mezclarían
    con las del mkvmerge y el progreso del remux quedaría ilegible. El
    resultado se anuncia al recogerlo.

    Devuelve True si dejó un RPU utilizable. Nunca lanza: si algo va mal, la
    Fase H lo extrae como siempre.
    """
    try:
        rc, _out, err = await _run([
            DOVI_TOOL_BIN, "extract-rpu", str(pre_mux_hevc), "-o", str(out_rpu),
        ], timeout=_adaptive_timeout(
            _estimate_from_ffmpeg(session, RATIO_EXTRACT_RPU, FPS_EXTRACT_RPU),
            floor_s=1200))
        if rc != 0 or not out_rpu.exists() or out_rpu.stat().st_size == 0:
            _logger.info("prewarm extract-rpu rc=%s: %s", rc, err[:200])
            out_rpu.unlink(missing_ok=True)
            return False
        return True
    except asyncio.CancelledError:
        out_rpu.unlink(missing_ok=True)
        raise
    except Exception as e:
        _logger.info("prewarm extract-rpu falló: %s", e)
        out_rpu.unlink(missing_ok=True)
        return False


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
                "→ mkvmerge directo sobre BL_injected.hevc con audio/subs/"
                "capítulos del origen."
            )

    # ── Determinar qué HEVC multiplexar según workflow/modo ──────────
    # El 📋 Plan anterior ya describe la ruta de cada workflow. Las sub-
    # llamadas (_run_streaming de dovi_tool mux y mkvmerge) emiten el
    # detalle del comando concreto al arrancar.
    if drop_in_fel:
        # Drop-in: source_injected.hevc ya es BL+EL con el RPU CMv4.0 inyectado.
        # No se ejecuta dovi_tool mux — el stream ya es dual-layer íntegro.
        if not source_injected.exists():
            raise RuntimeError(
                "source_injected.hevc no existe — ejecuta Fase F primero (drop-in FEL)"
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
               "input_path": bl_hevc,
               "output_path": dv_dual,
               "expected_out_bytes": _tamano(bl_hevc) + _tamano(el_injected),
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
        hevc_for_mkv = bl_injected
        remux_offset = 0.0
        remux_weight = 100.0

    # ── Validación de Fase H, adelantada y en paralelo ──────────────────
    # La Fase H del camino merge extrae el RPU COMPLETO del HEVC pre-mux para
    # comprobar frame count, CMv4.0, el_type y L8. Son 240 s de media y afecta
    # al 82 % de los jobs (68 de 83 medidos): el drop-in, que se libra con el
    # fast path, resulta ser la excepción y no la norma.
    #
    # Ese extract-rpu lee exactamente el mismo fichero que mkvmerge está a
    # punto de leer, y no depende de su resultado. Lanzándolo ahora se solapa
    # con el remux y la Fase H se encuentra el trabajo hecho.
    #
    # No se cambia NADA del criterio de validación: el extract sigue siendo
    # completo y Fase H sigue comprobando lo mismo sobre el mismo RPU. Si esto
    # falla, se ignora y Fase H lo rehace por su cuenta.
    prewarm_task = None
    prewarm_rpu = wd / "_validate_full_rpu.bin"
    prewarm_rpu.unlink(missing_ok=True)   # nunca reutilizar el de otra pasada
    if not drop_in_fel:
        if log_callback:
            await log_callback(
                "[Fase G] ├─ Extrayendo en paralelo el RPU para la validación "
                "de Fase H (mismo HEVC que va a leer mkvmerge, así no hay que "
                "recorrerlo dos veces seguidas)"
            )
        prewarm_task = asyncio.create_task(_prewarm_validation_rpu(
            session, hevc_for_mkv, prewarm_rpu, log_callback))

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
        if prewarm_task:
            prewarm_task.cancel()
        raise RuntimeError(f"mkvmerge falló (código {rc})")

    # Recoger el extract-rpu adelantado. A estas alturas suele estar hecho
    # (240 s de media contra 460 s de remux); si no, se le espera aquí, que es
    # exactamente el tiempo que la Fase H habría gastado de todos modos.
    if prewarm_task:
        try:
            ok = await prewarm_task
            if ok and log_callback:
                await log_callback(
                    f"[Fase G] ✓ RPU de validación listo "
                    f"({prewarm_rpu.stat().st_size / 1e6:.0f} MB) — Fase H no "
                    f"tendrá que releer el HEVC"
                )
        except Exception as e:
            _logger.info("prewarm del RPU de validación falló: %s", e)

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
        # Descripción del artefacto generado. Fase H, cuando arranque, emitirá
        # su propio 📋 Plan describiendo cómo lo validará.
        await log_callback(
            "[Fase G] 🎯 Resultado: MKV completo escrito con sufijo .tmp en "
            "/mnt/output (pendiente de validar antes del rename atómico al "
            "nombre final)."
        )
    # 100% AL FINAL: barra llena solo cuando el log de cierre se ha emitido.
    await _emit_progress(log_callback, 100, "Remux completado")
    return str(output_mkv)


# ══════════════════════════════════════════════════════════════════════
#  FASE H — Validación final
# ══════════════════════════════════════════════════════════════════════

# Frame count check: ÚNICAMENTE INFORMATIVO, no bloquea.
#
# El antiguo check estricto comparaba `ffprobe nb_frames del MKV` (que
# cuenta frames del HEVC stream) contra `target_frame_count` (que viene
# del RPU). Estas dos métricas NO TIENEN POR QUÉ COINCIDIR:
#   - Caso visto: source P8 single-layer con 135712 frames de vídeo
#     pero solo 135384 RPUs (328 frames sin RPU asociado, válido en P8).
#   - mkvmerge muxa los 135712 frames + 135384 RPUs sin problema.
#   - ffprobe del MKV final reporta 135712 (frame count del HEVC, correcto).
#   - target_frame_count = 135384 (frame count del RPU del bin, correcto).
#   - El check antiguo fallaba con "Δ=328 > 2" como falso positivo.
#
# La validación REAL de la integridad del MKV final ya está cubierta por:
#   1. mkvmerge -J devuelve rc=0 → MKV estructuralmente válido
#   2. Path clásico: HEAD+TAIL extract-rpu confirma CMv4.0 en ambos extremos
#   3. Drop-in: cadena upstream determinista (pre-flight + Fase B + inject-rpu)
#
# Por eso este helper SOLO loguea info, nunca lanza excepción salvo en el
# caso patológico de actual=0 (ffprobe no devuelve nada — probable
# corrupción severa o fichero inaccesible).
_FRAME_COUNT_INFO_DELTA_PCT = 1.0  # Δ relativo > 1% se loguea como warning


async def _check_frame_count(actual: int, expected: int, log_callback) -> None:
    """Loguea info sobre el frame count. NO bloquea salvo actual=0
    (señal de corrupción severa o fichero inaccesible)."""
    if expected <= 0:
        return  # No hay referencia, nada que comparar
    if actual <= 0:
        raise RuntimeError(
            f"ffprobe no pudo determinar el frame count del MKV final — "
            f"posible corrupción tras Fase G (esperado {expected})."
        )
    diff = abs(actual - expected)
    if diff == 0:
        if log_callback:
            await log_callback(
                f"[Fase H] ✓ Frame count: {actual} (coincide con target_frame_count)"
            )
        return
    pct = (diff / expected) * 100.0
    if log_callback:
        if pct > _FRAME_COUNT_INFO_DELTA_PCT:
            await log_callback(
                f"[Fase H] ℹ Frame count del MKV ({actual}) difiere del RPU "
                f"({expected}, Δ={diff} = {pct:.2f}%). Diferencia legítima en "
                f"streams P8 con frames sin RPU asociado, o en MKVs cuyo header "
                f"NUMBER_OF_FRAMES no refleja el conteo real del HEVC. "
                f"La integridad real se valida con mkvmerge -J + HEAD/TAIL CMv4.0."
            )
        else:
            await log_callback(
                f"[Fase H] ✓ Frame count: {actual} vs {expected} (Δ={diff}, dentro de margen)"
            )


def resolve_validation_target(session: CMv40Session, wd: Path) -> tuple[Path, bool]:
    """Decide QUÉ fichero valida la Fase H y si el rename ya está hecho.

    Tres orígenes, en orden:
      1. ``/mnt/output/{nombre}.mkv.tmp`` — lo normal: Fase G lo acaba de escribir.
      2. ``{workdir}/output.mkv`` — proyectos antiguos (Fase G escribía al workdir).
      3. ``/mnt/output/{nombre}.mkv`` — el rename YA se hizo en una ejecución
         previa de esta misma fase.

    El caso 3 existe porque la Fase H puede dispararse dos veces: el frontend
    re-lanza la siguiente fase en cada tick de polling/WS y el lock de
    `_run_cmv40_phase` solo cubre la ventana en que la fase corre — un disparo
    que llega segundos DESPUÉS del rename lo esquiva. Antes eso reventaba con
    "MKV final no existe — ejecuta Fase G primero" y dejaba el proyecto marcado
    con error pese a tener el MKV correcto y completo en su sitio (visto en 2
    de 67 jobs). Revalidar el fichero final es idempotente y conserva el
    sentido de la fase: se comprueba lo que hay, sin moverlo.

    Returns:
        (path_a_validar, already_renamed)
    """
    output_mkv_tmp = OUTPUT_DIR / f"{session.output_mkv_name}.tmp"
    output_mkv_legacy = wd / "output.mkv"
    output_mkv_final = OUTPUT_DIR / session.output_mkv_name
    if output_mkv_tmp.exists():
        return output_mkv_tmp, False
    if output_mkv_legacy.exists():
        return output_mkv_legacy, False
    if output_mkv_final.exists():
        return output_mkv_final, True
    raise RuntimeError(
        f"MKV final no existe: ni {output_mkv_tmp} ni {output_mkv_legacy} "
        f"ni {output_mkv_final} — ejecuta Fase G primero"
    )


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
    output_mkv, already_renamed = resolve_validation_target(session, wd)
    if already_renamed and log_callback:
        await log_callback(
            "[Fase H] El MKV final ya está en su ubicación definitiva "
            "(una ejecución previa completó el rename) — se revalida sin "
            "volver a moverlo."
        )

    drop_in_fel = is_drop_in_fel(session)

    if log_callback:
        mkv_gb = output_mkv.stat().st_size / 1e9
        if drop_in_fel:
            await log_callback(
                "[Fase H] 📋 Plan (fast path drop-in FEL): el RPU del MKV es bit-a-bit "
                "el RPU_target.bin — inject-rpu lo copió íntegro sin tocarlo. El bin ya "
                "pasó pre-flight + Fase B con CMv4.0 confirmado y trust gates OK, así "
                "que la cadena upstream garantiza Profile 7 FEL CMv4.0 en el output. "
                "Validamos integridad del MKV con mkvmerge -J y frame count con ffprobe; "
                "saltamos el extract-rpu completo (ahorra ~5-8 min en UHD)."
            )
        else:
            await log_callback(
                "[Fase H] 📋 Plan: validar el resultado antes de mover el MKV al output "
                "final. Leemos el RPU del HEVC resultante, confirmamos que tiene CMv4.0 "
                "y que el frame count coincide con el source. Si todo OK, rename atómico "
                ".tmp → .mkv (instantáneo, mismo filesystem) y cleanup de artefactos "
                "intermedios."
            )
        await log_callback(f"[Fase H] ┌─ Validando DV del MKV resultante ({mkv_gb:.1f} GB)…")

    # ── result_info: estructura común que ambas ramas rellenan para el log
    # final + return. El path clásico la deriva de extract-rpu + info; el
    # fast path drop-in la construye desde los valores garantizados upstream
    # (target_type=trusted_p7_fel_final + trust_ok + pre-flight CMv4.0).
    expected_frames = session.target_frame_count or session.source_frame_count or 0

    if drop_in_fel:
        # ── FAST PATH (drop-in FEL puro) ────────────────────────────────
        # Lo único que falta verificar tras la cadena upstream es:
        #   1. Que el MKV es leíble y no está truncado (frame count vs expected)
        #   2. Que mkvmerge -J no detecta corrupción
        # Profile 7 FEL CMv4.0 ya están garantizados; un extract-rpu completo
        # solo confirmaría lo mismo a un coste de 5-8 min de CPU sobre el HEVC.
        if log_callback:
            await log_callback("[Fase H] Paso 1/2: leyendo frame count del MKV final (ffprobe)…")
        await _emit_progress(log_callback, 30, "Frame count del MKV final")
        actual_frames = await _probe_frame_count(str(output_mkv))
        await _check_frame_count(actual_frames, expected_frames, log_callback)
        result_info = DoviInfo(
            profile=7,
            el_type="FEL",
            cm_version="v4.0",
            frame_count=actual_frames or expected_frames or 0,
        )
    else:
        # ── PATH CLÁSICO (merge CMv4.0 sobre P7/P8 source) ──────────────
        # El RPU final viene de un merge frame-a-frame (`dovi_tool editor
        # --allow_cmv4_transfer`) que podría — teóricamente — producir
        # asimetrías o frame counts incorrectos.
        #
        # ESTRATEGIA DE VALIDACIÓN (rigurosa):
        #
        # Hacemos `dovi_tool extract-rpu` COMPLETO del HEVC pre-mux
        # (BL_injected.hevc o equivalente) y verificamos:
        #   1. Frame count del RPU == expected_frames (target_frame_count)
        #      → garantía de que el merge produjo el RPU completo y correcto
        #   2. cm_version == v4.0 → CMv4.0 aplicado correctamente
        #   3. el_type según source_workflow (FEL/MEL/P8)
        #
        # POR QUÉ NO usamos ffprobe del MKV: ffprobe cuenta frames del HEVC
        # stream (NAL units IDR/P/B), que NO tiene por qué coincidir con el
        # frame count del RPU. En P8 single-layer es legítimo tener frames
        # de vídeo sin RPU asociado, especialmente cuando el HEVC source ya
        # tenía RPUs y dovi_tool inject-rpu los reemplaza con los del bin
        # nuevo (que puede tener frame count distinto al del HEVC original).
        # Comparar HEVC frames vs RPU frames daba falsos positivos.
        #
        # POR QUÉ NO usamos solo HEAD+TAIL: confirma CMv4.0 en los
        # extremos pero NO valida el frame count total del RPU. Un bug que
        # cortara el RPU a la mitad podría pasar desapercibido.
        #
        # Coste: ~5-8 min en UHD (extract-rpu completo). Aceptable para la
        # garantía de integridad — el merge frame-a-frame es la operación
        # más sensible del pipeline y vale la pena verificarla en serio.
        # Drop-in (que es el caso típico) NO paga este coste — usa el fast
        # path que confía en la cadena upstream determinista.
        pre_mux_candidates = [
            wd / "source_injected.hevc",   # drop-in P7 FEL (force_interactive)
            wd / "DV_dual.hevc",           # workflow p7_fel dual-layer
            wd / "EL_injected.hevc",       # workflow p7_mel
            wd / "BL_injected.hevc",       # workflow p8 single-layer
        ]
        pre_mux_hevc = next((p for p in pre_mux_candidates if p.exists()), None)
        if not pre_mux_hevc:
            raise RuntimeError(
                "No se encontró el HEVC pre-mux para validación. Esperado "
                "uno de: source_injected.hevc, DV_dual.hevc, EL_injected.hevc, "
                "BL_injected.hevc en el workdir."
            )

        full_rpu = wd / "_validate_full_rpu.bin"
        # Fase G lo deja extraído mientras hacía el remux (ver
        # _prewarm_validation_rpu): es el mismo HEVC y el mismo comando, así
        # que aquí solo hay que recogerlo. Si no está —Fase G de una versión
        # anterior, o el adelanto falló— se extrae ahora como siempre.
        prewarmed = full_rpu.exists() and full_rpu.stat().st_size > 0
        try:
            if prewarmed:
                if log_callback:
                    await log_callback(
                        f"[Fase H] Paso 1/3: RPU ya extraído durante el remux "
                        f"({full_rpu.stat().st_size / 1e6:.0f} MB) — se valida "
                        f"directamente, sin releer el HEVC."
                    )
                await _emit_progress(log_callback, 75, "RPU listo (extraído en Fase G)")
            else:
                if log_callback:
                    hevc_gb = pre_mux_hevc.stat().st_size / 1e9
                    # ETA orientativa: dovi_tool extract-rpu sobre NAS ronda
                    # ~3-5 min por cada 30 GB de HEVC (depende de carga I/O).
                    eta_min_lo = max(2, int(hevc_gb / 30 * 3))
                    eta_min_hi = max(5, int(hevc_gb / 30 * 5))
                    await log_callback(
                        f"[Fase H] Paso 1/3: extrayendo RPU completo del HEVC "
                        f"pre-mux ({pre_mux_hevc.name}, {hevc_gb:.1f} GB) — "
                        f"validación rigurosa del frame count y CMv4.0. "
                        f"Operación pesada (lectura del HEVC entero por dovi_tool, "
                        f"~{eta_min_lo}-{eta_min_hi} min sobre NAS)…"
                    )
                await _emit_progress(log_callback, 5, "Extrayendo RPU completo del pre-mux")

            # Heartbeat task: dovi_tool extract-rpu no streamea progreso al log
            # (sale en stderr solo al final), así que sin esto el modal queda
            # sin actividad ~5-8 min y parece colgado. Cada 30s emitimos el
            # tiempo transcurrido para confirmar que sigue trabajando.
            if not prewarmed:
                import time
                hb_start = time.monotonic()

                async def _heartbeat():
                    try:
                        while True:
                            await asyncio.sleep(30)
                            elapsed = int(time.monotonic() - hb_start)
                            if log_callback:
                                await log_callback(
                                    f"[Fase H]  ⏱ extract-rpu en curso… "
                                    f"({elapsed // 60}min {elapsed % 60}s transcurridos)"
                                )
                    except asyncio.CancelledError:
                        return
                hb_task = asyncio.create_task(_heartbeat())
                try:
                    rc, _, err = await _run([
                        DOVI_TOOL_BIN, "extract-rpu", str(pre_mux_hevc),
                        "-o", str(full_rpu),
                    ], log_callback=log_callback,
                       timeout=_adaptive_timeout(
                           _estimate_from_ffmpeg(session, RATIO_EXTRACT_RPU, FPS_EXTRACT_RPU),
                           floor_s=1200),
                    )
                finally:
                    hb_task.cancel()
                    try:
                        await hb_task
                    except asyncio.CancelledError:
                        pass

                if rc != 0 or not full_rpu.exists() or full_rpu.stat().st_size == 0:
                    raise RuntimeError(
                        f"extract-rpu falló sobre {pre_mux_hevc.name}: {err[:200]}"
                    )

            if log_callback:
                await log_callback("[Fase H] Paso 2/3: analizando metadata del RPU…")
            await _emit_progress(log_callback, 80, "Analizando RPU")
            rc, summary, err = await _run(
                [DOVI_TOOL_BIN, "info", "--summary", str(full_rpu)],
                log_callback=log_callback, timeout=60,
            )
            if rc != 0:
                raise RuntimeError(f"dovi_tool info falló sobre RPU completo: {err[:200]}")
            rpu_info = _parse_dovi_summary(summary)

            if log_callback:
                await log_callback(
                    f"[Fase H] RPU del MKV final: Profile {rpu_info.profile} "
                    f"({rpu_info.el_type}), CM {rpu_info.cm_version}, "
                    f"{rpu_info.frame_count} frames"
                )

            # ── Validación rigurosa: frame count del RPU vs expected ──
            # NOTA: comparamos RPU vs RPU (misma métrica). La diferencia
            # con ffprobe del MKV es esperada y NO se valida aquí.
            if expected_frames > 0 and rpu_info.frame_count > 0:
                rpu_diff = abs(rpu_info.frame_count - expected_frames)
                if rpu_diff > 2:
                    raise RuntimeError(
                        f"Frame count del RPU del MKV final ({rpu_info.frame_count}) "
                        f"distinto del esperado ({expected_frames}, Δ={rpu_diff}). "
                        f"Indica que el merge CMv4.0 NO produjo un RPU completo — "
                        f"posible bug del editor de dovi_tool. NO entregar este MKV."
                    )
                if rpu_diff > 0 and log_callback:
                    # Δ de 1-2 frames es normal: mkvmerge puede emitir un
                    # cluster final corto que mueve ±1 frame el conteo. NO es
                    # warning — el merge se considera correcto. Usamos ℹ
                    # (informativo) para alinearlo con la semántica del drop-in
                    # path en _check_frame_count.
                    await log_callback(
                        f"[Fase H] ℹ RPU frame count {rpu_info.frame_count} vs "
                        f"{expected_frames} esperados (Δ={rpu_diff} frame{'s' if rpu_diff != 1 else ''}, "
                        f"dentro de tolerancia ±2 — variación normal del muxer)"
                    )

            # ── Validación CMv4.0 ─────────────────────────────────────
            if rpu_info.cm_version != "v4.0":
                raise RuntimeError(
                    f"RPU del MKV final tiene CM {rpu_info.cm_version} (esperado v4.0) — "
                    "el merge no aplicó CMv4.0."
                )

            # ── Validación el_type según workflow ─────────────────────
            expected_el = "FEL" if (session.source_workflow or "p7_fel") == "p7_fel" else None
            if expected_el and rpu_info.el_type != expected_el:
                raise RuntimeError(
                    f"RPU del MKV final tiene el_type={rpu_info.el_type!r} "
                    f"(esperado '{expected_el}' según source_workflow={session.source_workflow})."
                )

            # ── Validación L8 presente (CMv4.0 trims target display) ───
            # L8 es el marker real de CMv4.0: contiene los trim targets para
            # peak displays distintos del master (100/600/1000/2000 nits).
            # Si por algún bug de dovi_tool editor el merge dejara el RPU sin
            # L8, cm_version=v4.0 podría seguir siendo true (el header lo
            # marca) pero el output sería un CMv4.0 hueco — el HDR display no
            # tendría nada que aplicar. Defensa en profundidad: abortar antes
            # del rename atómico, preservar .mkv.tmp para inspección.
            if not rpu_info.has_l8:
                raise RuntimeError(
                    "RPU del MKV final NO contiene bloques L8 (trims CMv4.0). "
                    "El RPU está marcado como v4.0 pero sin los trim targets "
                    "para peak displays — un display HDR no aplicará tone-mapping "
                    "CMv4.0. Posible bug de dovi_tool editor al transferir."
                )

            # NOTA: antes había aquí un ffprobe del MKV completo solo para
            # log informativo ("frame count del MKV vs RPU"). Eliminado:
            #   - Coste 5-15s extra sobre NAS bajo I/O en MKVs de 60+ GB
            #   - Ruido visual: el modal quedaba sin actualizar mientras
            #     ffprobe leía un MKV enorme, dando aspecto de "colgado"
            #   - Información redundante: la integridad ya se valida con el
            #     extract-rpu completo arriba (RPU vs RPU expected, ±2)
        finally:
            full_rpu.unlink(missing_ok=True)

        # result_info: el RPU completo es la fuente autoritativa
        result_info = DoviInfo(
            profile=rpu_info.profile,
            el_type=rpu_info.el_type,
            cm_version=rpu_info.cm_version,
            frame_count=rpu_info.frame_count,
            has_l8=rpu_info.has_l8,
            has_l11=rpu_info.has_l11,
        )
        if log_callback:
            await log_callback(
                f"[Fase H] ✓ Validación DV OK (RPU completo verificado): "
                f"Profile {result_info.profile} ({result_info.el_type}), "
                f"CM {result_info.cm_version}, {result_info.frame_count} frames"
            )

    # Validar pistas con mkvmerge -J (común a ambos paths)
    if log_callback:
        step_label = "Paso 2/2" if drop_in_fel else "Paso 3/3"
        try:
            mkv_gb_for_log = output_mkv.stat().st_size / 1e9
            size_hint = f" sobre {mkv_gb_for_log:.1f} GB"
        except Exception:
            size_hint = ""
        await log_callback(
            f"[Fase H] {step_label}: validando estructura del MKV con mkvmerge -J"
            f"{size_hint} (lee el contenedor entero, suele tardar unos segundos en NAS)…"
        )
    await _emit_progress(log_callback, 50, "Validando pistas (mkvmerge -J)")
    rc, out, err = await _run(
        [MKVMERGE_BIN, "-J", str(output_mkv)],
        log_callback=log_callback, timeout=60,
    )
    if rc not in (0, 1):
        raise RuntimeError(f"mkvmerge -J falló sobre MKV final: {err[:200]}")

    # Renombrar .tmp → .mkv (atómico si mkvmerge escribió ya en /mnt/output,
    # fallback a move con monitor de progreso si viene de workdir legacy).
    final_path = OUTPUT_DIR / session.output_mkv_name
    if already_renamed:
        # Nada que mover: acabamos de validar el propio fichero final.
        session.output_mkv_path = str(final_path)
        await _emit_progress(log_callback, 100, "Validación completada")
        if log_callback:
            await log_callback(
                f"[Fase H] ✓ Revalidado el MKV ya existente: {final_path}"
            )
            await log_callback(
                f"[Fase H] 🎯 Resultado: el upgrade ya estaba completo — "
                f"Profile {result_info.profile}"
                f"{' ' + result_info.el_type if result_info.el_type else ''}, "
                f"CM {result_info.cm_version}, {result_info.frame_count} frames."
            )
        return {
            "profile": result_info.profile,
            "el_type": result_info.el_type,
            "cm_version": result_info.cm_version,
            "frame_count": result_info.frame_count,
            "output_path": str(final_path),
            "already_validated": True,
        }
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
    #
    # `source.hevc` entra en la lista desde 2026-08-16. Fase C solo lo borra
    # cuando hubo demux, así que en los workflows drop-in (que son la mayoría)
    # sobrevivía al job entero: 227 GB en cuatro ficheros de otros tantos jobs
    # ya terminados. Aquí el MKV final ya está validado y movido, así que el
    # HEVC no sirve para nada — si se rehace una fase, Fase A lo regenera.
    freed = 0
    for hevc_name in ("source.hevc", "source_injected.hevc", "DV_dual.hevc",
                      "EL_injected.hevc", "BL_injected.hevc"):
        artifact = wd / hevc_name
        try:
            if artifact.exists():
                freed += artifact.stat().st_size
        except OSError:
            pass
        artifact.unlink(missing_ok=True)
    if freed > 0 and log_callback:
        await log_callback(
            f"[Fase H] 🧹 Liberados {freed / 1024**3:.1f} GB de HEVC intermedio "
            f"(el MKV final ya está validado en /mnt/output)"
        )

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
    # Porcentaje de confianza en la MISMA escala que el gate de avance
    # (threshold_ok = pearson >= 0.85 → "umbral 85%"). Antes mapeábamos
    # [-1,1]→[0,100], lo que mostraba "Confianza 90% inferior al umbral 85%"
    # (contradicción: 90 > 85). La correlación negativa se satura a 0%.
    confidence_pct = max(0, int(round(pearson * 100)))

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


# Tolerancia al comparar el offset del sheet con el detectado: el dato del
# sheet lo mide la comunidad sobre su propio rip y un par de frames de
# diferencia no invalida la coincidencia.
SHEET_OFFSET_TOLERANCE = 2


def sheet_sync_hint(session: CMv40Session, suggested: dict | None) -> dict | None:
    """Contrasta el offset documentado en la hoja de DoviTools con el que
    la cross-correlation acaba de detectar.

    Son dos medidas independientes del mismo desfase: si coinciden, es una
    confirmación fuerte de que el bin es el correcto y está bien alineado;
    si divergen, algo no cuadra (bin de otra edición, corte distinto) y
    conviene revisar el chart a mano antes de inyectar.

    Devuelve None si el sheet no aporta offset para este título.
    """
    rec = session.sheet_recommendation or {}
    if not rec:
        return None

    # El offset vive en la fila factible (la sección izquierda no lo trae).
    frames: int | None = rec.get("sync_offset_frames")
    text: str = rec.get("sync_offset") or ""
    if frames is None:
        for row in rec.get("rows") or []:
            if row.get("sync_offset_frames") is not None:
                frames = row["sync_offset_frames"]
                text = row.get("sync_offset") or text
                break
    if frames is None:
        return None

    out = {
        "sheet_offset": frames,
        "sheet_offset_text": text,
        "match_title": rec.get("match_title", ""),
        "detected_offset": None,
        "agrees": None,
        "delta": None,
        "sign_flipped": False,
    }
    detected = (suggested or {}).get("offset")
    if isinstance(detected, int):
        out["detected_offset"] = detected
        out["delta"] = detected - frames
        out["agrees"] = abs(out["delta"]) <= SHEET_OFFSET_TOLERANCE
        # Misma magnitud, signo contrario: la comunidad anota el desfase en
        # el sentido inverso en algunas filas. Distinguirlo evita leer como
        # "no cuadra" lo que en realidad es el mismo desfase.
        if not out["agrees"] and frames != 0:
            out["sign_flipped"] = abs(detected + frames) <= SHEET_OFFSET_TOLERANCE
    return out


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
