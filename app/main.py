"""
main.py — Backend FastAPI de HDO Blu-ray Toolkit

Punto de entrada de la aplicación. Sirve la SPA y expone la API REST
y el endpoint WebSocket para streaming de output en tiempo real.

─────────────────────────────────────────────────────────────────────
RUTAS DE LA API
─────────────────────────────────────────────────────────────────────

  Estáticos
    GET  /                              → index.html (SPA)
    GET  /static/*                      → ficheros estáticos

  ISOs
    GET  /api/isos                      → lista de ISOs en /mnt/isos

  Sesiones (unidades de trabajo persistentes)
    GET  /api/sessions                  → lista todas las sesiones
    GET  /api/sessions/{id}             → obtiene una sesión
    PUT  /api/sessions/{id}             → actualiza campos de la sesión (Fase C)
    DELETE /api/sessions/{id}           → elimina una sesión
    POST /api/sessions/{id}/recalculate-name  → recalcula el nombre del MKV

  Análisis
    POST /api/analyze                   → lanza Fase A + B, devuelve sesión nueva

  Ejecución
    POST /api/sessions/{id}/execute     → lanza Fases D + E en background

  Estado
    GET  /api/status                    → estado de la app

  WebSocket
    WS   /ws/{id}                       → streaming de output de la ejecución

─────────────────────────────────────────────────────────────────────
ACCESO AL ISO — LOOP MOUNT DIRECTO (UDF 2.50)
─────────────────────────────────────────────────────────────────────

Los ISOs se montan directamente dentro del contenedor Docker usando
``mount -t udf -o ro,loop``. Requiere ``privileged: true`` en Docker.

  Análisis (Fase A):
    1. mount_iso(iso_path) → loop mount en /mnt/bd/{nombre}/
    2. BDInfoCLI lee desde /mnt/bd/{nombre}/
    3. unmount_iso() en finally (siempre, éxito o error)

  Ejecución (Fase D):
    1. mount_iso(iso_path) → loop mount
    2. mkvmerge lee el MPLS desde /mnt/bd/{nombre}/BDMV/PLAYLIST/
    3. unmount_iso() en finally
    4. Fase E procesa el MKV intermedio ya en /mnt/tmp
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from models import (
    AnalyzeRequest,
    ExecutionRecord,
    QueueReorderRequest,
    Session,
    SessionUpdateRequest,
)
from phases.phase_a import run_full_analysis
from phases.phase_b import apply_rules, generate_auto_chapters
from phases.phase_d import find_main_mpls, run_phase_d
from phases.phase_e import needs_reordering, run_phase_e_direct, run_phase_e_propedit
from phases.iso_mount import mount_iso, unmount_iso, is_mount_available
from queue_manager import queue_manager
from storage import (
    compute_iso_fingerprint,
    delete_session,
    find_session_by_fingerprint,
    find_sessions_by_fingerprint,
    list_sessions,
    load_session,
    make_session_id,
    save_session,
    save_session_async,
)

# ── Constantes de entorno ─────────────────────────────────────────────────────

ISOS_DIR   = Path(os.environ.get("ISOS_DIR", "/mnt/isos"))
TMP_DIR    = os.environ.get("TMP_DIR", "/mnt/tmp")
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))

# ── Recuperación de sesiones interrumpidas ───────────────────────────────────
# Al arrancar, las sesiones que quedaron en 'running' o 'queued' (por un
# reinicio inesperado) se resetean a 'pending' para que el usuario pueda
# relanzarlas. Esto se aplica siempre, no solo en DEV_MODE.

import logging as _logging
_logger = _logging.getLogger(__name__)

# Tracking de cancelación — permite matar el subprocess activo de un pipeline
_cancel_flags: dict[str, bool] = {}          # session_id → True si cancelado
_active_processes: dict[str, asyncio.subprocess.Process] = {}  # session_id → proc

# Throttle + lock por sesión Tab 1 — mismo patrón que CMv4.0 para no
# bloquear el event loop con model_dump_json y serializar saves al
# mismo JSON. Ver doc en _cmv40_maybe_persist_log para detalles.
_session_save_throttle: dict[str, dict] = {}
_session_save_locks: dict[str, asyncio.Lock] = {}


def _get_session_save_lock(sid: str) -> asyncio.Lock:
    if sid not in _session_save_locks:
        _session_save_locks[sid] = asyncio.Lock()
    return _session_save_locks[sid]


async def _maybe_save_session_throttled(session) -> None:
    """Persistencia non-blocking con throttle para Tab 1.

    Reglas (igual que CMv4.0):
      - >1s desde último save → trigger
      - >=20 líneas pendientes → trigger
      - Si lock libre, lanza task background con `save_session_async`.
      - Si lock ocupado, descarta — la línea ya está en RAM, el save en
        curso o el siguiente trigger la persistirá.
    """
    import time as _t
    sid = session.id
    state = _session_save_throttle.setdefault(sid, {"last_save_ts": 0.0, "lines_since": 0})
    state["lines_since"] += 1
    elapsed = _t.monotonic() - state["last_save_ts"]
    should_save = elapsed > 1.0 or state["lines_since"] >= 20
    if not should_save:
        return
    lock = _get_session_save_lock(sid)
    if lock.locked():
        return
    state["last_save_ts"] = _t.monotonic()
    state["lines_since"] = 0

    async def _bg_save():
        try:
            async with lock:
                await save_session_async(session)
        except Exception as e:
            _logger.warning("[session throttled save] sid=%s error: %s", sid, e)

    asyncio.create_task(_bg_save())


async def _flush_session_save(session) -> None:
    """Fuerza save inmediato para Tab 1 al terminar la fase. Espera al
    lock para garantizar durabilidad antes de avanzar el estado."""
    import time as _t
    sid = session.id
    state = _session_save_throttle.setdefault(sid, {"last_save_ts": 0.0, "lines_since": 0})
    lock = _get_session_save_lock(sid)
    async with lock:
        await save_session_async(session)
        state["last_save_ts"] = _t.monotonic()
        state["lines_since"] = 0


def _recover_interrupted_sessions() -> None:
    """Resetea sesiones zombie (running/queued) a pending tras un reinicio."""
    count = 0
    for s in list_sessions():
        if s.status in ("running", "queued"):
            s.status = "pending"
            s.error_message = "Sesión interrumpida por reinicio del servidor"
            save_session(s)
            count += 1
    if count:
        _logger.info("[Startup] %d sesión(es) interrumpida(s) reseteada(s) a 'pending'", count)


def _recover_interrupted_cmv40_sessions() -> None:
    """Limpia sesiones CMv4.0 con running_phase != null tras un reinicio.

    Sin esto, un proyecto que estaba en mid-fase cuando el contenedor cae
    queda con running_phase persistido — la UI lo muestra eternamente como
    "fase ejecutándose" cuando en realidad no hay ningún proceso vivo
    corriendo. El usuario solo puede deshacerlo manualmente con cancelar
    (que falla porque no hay proc) o forzando un reset de fase.

    Estrategia (paralela a _recover_interrupted_sessions de Tab 1):
      - running_phase=None
      - El último phase_history record con status="running" → status="error"
        + error_message="Sesión interrumpida por reinicio del servidor"
      - session.error_message también poblado para que el banner rojo de la
        UI sea visible al cargar
      - phase NO se modifica — el rebobinado/forward-roll del GET decidirán
        a qué punto llevar al usuario en función de los artefactos en disco
    """
    from storage import list_cmv40_sessions, save_cmv40_session
    count = 0
    msg = "Sesión interrumpida por reinicio del servidor"
    for s in list_cmv40_sessions():
        if not s.running_phase:
            continue
        s.running_phase = None
        s.error_message = msg
        # Marcar el último phase_history en running como error
        if s.phase_history:
            last = s.phase_history[-1]
            if getattr(last, "status", "") == "running":
                last.status = "error"
                last.error_message = msg
                from datetime import datetime as _dt, timezone as _tz
                last.finished_at = _dt.now(_tz.utc)
        save_cmv40_session(s)
        count += 1
    if count:
        _logger.info("[Startup] %d sesión(es) CMv4.0 interrumpida(s) limpiada(s)", count)


_recover_interrupted_sessions()
_recover_interrupted_cmv40_sessions()


# ── Auto-cleanup de huérfanos obvios al arrancar ─────────────────────────────
# Limpia ficheros/dirs que claramente NO deben existir tras un reinicio del
# contenedor: tmps de light-profile mayores de 1h y mount points de ISO que
# no están realmente montados según /proc/mounts. Otros tipos (workdirs CMv4.0,
# .mkv.tmp grandes) se dejan para limpieza manual con preview en la UI.

def _cleanup_obvious_orphans_at_startup() -> None:
    """Borra silenciosamente huérfanos triviales (tmps cortos, mount points
    sin entry en /proc/mounts). Logging info por cada item borrado."""
    import shutil as _shutil_so
    import time as _time_so
    from pathlib import Path as _Path_so

    # 1. /tmp/lightprof_* mayores de 1 hora
    try:
        for lp in _Path_so("/tmp").glob("lightprof_*"):
            if not lp.is_dir():
                continue
            try:
                age = _time_so.time() - lp.stat().st_mtime
            except OSError:
                continue
            if age > 3600:
                try:
                    _shutil_so.rmtree(lp)
                    _logger.info("[Startup cleanup] light-profile tmp removed: %s (age %ds)", lp, int(age))
                except Exception as e:
                    _logger.warning("[Startup cleanup] failed to remove %s: %s", lp, e)
    except Exception as e:
        _logger.warning("[Startup cleanup] lightprof scan failed: %s", e)

    # 2. /mnt/bd/* sin entry en /proc/mounts (mount points zombies)
    try:
        mount_base = _Path_so("/mnt/bd")
        if mount_base.exists():
            mounted_paths: set[str] = set()
            try:
                with open("/proc/mounts") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) > 1:
                            mounted_paths.add(parts[1])
            except Exception:
                pass
            for mp in mount_base.iterdir():
                if not mp.is_dir():
                    continue
                if str(mp) in mounted_paths:
                    continue
                # Solo borramos si está vacío (sino podría ser un montaje
                # detectado mal — preferimos no tocar)
                try:
                    is_empty = not any(mp.iterdir())
                except OSError:
                    is_empty = False
                if is_empty:
                    try:
                        mp.rmdir()
                        _logger.info("[Startup cleanup] iso mount point removed: %s", mp)
                    except Exception as e:
                        _logger.warning("[Startup cleanup] failed to rmdir %s: %s", mp, e)
    except Exception as e:
        _logger.warning("[Startup cleanup] mount points scan failed: %s", e)


_cleanup_obvious_orphans_at_startup()

# ── DEV MODE ──────────────────────────────────────────────────────────────────
# Activado con DEV_MODE=1 (ver dev_fixtures.py). Cuando está apagado (default
# en producción), los bloques `if DEV_MODE:` no se ejecutan y los fixtures
# quedan inertes — no hay impacto runtime. Mantener: util para iterar UI sin
# discos reales y para demos.
from dev_fixtures import (
    DEV_MODE, DEV_FAKE_ISOS, build_fake_session, seed_dev_sessions,
    DEV_FAKE_MKV_FILES, build_fake_mkv_analysis, build_fake_mkv_apply,
    DEV_FAKE_RPU_FILES, build_fake_per_frame_data,
)
if DEV_MODE:
    seed_dev_sessions(CONFIG_DIR)


# ── Aplicación FastAPI ────────────────────────────────────────────────────────

app = FastAPI(
    title="HDO Blu-ray Toolkit",
    version="1.3.0",
    description="Convierte ISOs UHD Blu-ray a MKV con selección automática de pistas y soporte Dolby Vision FEL.",
)

# Conexiones WebSocket activas: session_id → [WebSocket, ...]
_ws_connections: dict[str, list[WebSocket]] = {}

# Clientes WebSocket suscritos a updates de la cola
_queue_ws_clients: set[WebSocket] = set()
# Lock para serializar sends concurrentes al mismo WS (evita conflictos uvicorn/wsproto)
_broadcast_lock = None  # inicializado en startup (necesita event loop activo)


async def _send_ws_with_timeout(
    connections: dict[str, list],
    session_id: str,
    ws,
    msg: str,
    timeout: float = 2.0,
) -> None:
    """Envía un mensaje a un WebSocket con timeout corto. Si tarda más
    de `timeout` segundos asumimos cliente zombie (Mac dormido, red rota,
    cliente muy lento): cerramos el WS y lo quitamos de la lista de
    conexiones activas. El frontend reconectará al detectar el cierre.

    Crítico para no bloquear el event loop. Si en `_run_streaming` la
    coroutine de log esperara en `await ws.send_text(...)` minutos hasta
    el timeout TCP del kernel, el subprocess reader se quedaba sin leer
    el pipe → ffmpeg se bloqueaba en write → gap visible al usuario.
    Con esta función lanzada en `asyncio.create_task`, el log loop sigue
    sin esperar al cliente zombie."""
    try:
        await asyncio.wait_for(ws.send_text(msg), timeout=timeout)
    except (asyncio.TimeoutError, Exception):
        try:
            await ws.close()
        except Exception:
            pass
        try:
            connections.get(session_id, []).remove(ws)
        except ValueError:
            pass


async def _broadcast_queue(status: dict) -> None:
    """Envía el estado de la cola a todos los clientes WebSocket suscritos.

    Cada send va en task paralela con timeout corto — un cliente zombie
    NO debe bloquear los demás (mismo razonamiento que _send_ws_with_timeout).
    """
    msg = json.dumps(status)
    async def _send_one(ws):
        try:
            await asyncio.wait_for(ws.send_text(msg), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            try:
                await ws.close()
            except Exception:
                pass
            _queue_ws_clients.discard(ws)
    for ws in list(_queue_ws_clients):
        asyncio.create_task(_send_one(ws))


# ── Estáticos ─────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def index():
    """Sirve la SPA (Single Page Application)."""
    return FileResponse("static/index.html")


# ── ISOs disponibles ──────────────────────────────────────────────────────────

@app.get("/api/isos", summary="Lista ISOs disponibles (legacy — usar /api/sources)")
async def list_isos():
    """
    Devuelve la lista de ficheros .iso encontrados recursivamente en /mnt/isos.

    Las rutas son relativas a /mnt/isos para que el frontend pueda mostrarlas
    sin exponer la estructura interna del NAS.

    Respuesta: ``{"isos": ["Movie (2025).iso", "subdir/Movie2 (2024).iso", ...]}``

    Legacy: /api/sources es el reemplazo (lista los 3 tipos de fuente). Este
    endpoint se mantiene por compat con el frontend antiguo.
    """
    # ⚠️ DEV MODE — branch que devuelve fixtures sin tocar el filesystem
    if DEV_MODE:
        return {"isos": DEV_FAKE_ISOS}
    if not ISOS_DIR.exists():
        return {"isos": []}
    isos = sorted(
        str(p.relative_to(ISOS_DIR))
        for p in ISOS_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() == ".iso"
    )
    return {"isos": isos}


# ══════════════════════════════════════════════════════════════════════
#  /api/sources — listado unificado de fuentes (v2.6+)
#
#  Escanea /mnt/isos recursivamente (depth máx 3) y devuelve cada
#  entrada clasificada como uno de:
#    - 'iso':         fichero .iso
#    - 'bdmv_folder': carpeta con BDMV/PLAYLIST/ dentro
#    - 'm2ts':        fichero .m2ts (típicamente dentro de BDMV/STREAM/
#                     pero también suelto en carpetas)
#
#  Cuando un mismo árbol tiene tanto carpeta BDMV completa como ficheros
#  .m2ts dentro, devolvemos SOLO la carpeta BDMV (es el origen "canónico"
#  — el usuario raramente quiere procesar un m2ts suelto dentro de un
#  BDMV completo, eso es bypass avanzado).
#
#  Cache 60s en memoria — el filesystem no cambia constantemente.
# ══════════════════════════════════════════════════════════════════════


_SOURCES_SCAN_MAX_DEPTH = 3
_SOURCES_CACHE: dict = {"ts": 0.0, "data": None}
_SOURCES_CACHE_TTL = 60.0


def _scan_sources_in_dir() -> list[dict]:
    """Escanea ISOS_DIR y devuelve lista clasificada. Operación síncrona —
    el caller la ejecuta en thread pool si quiere evitar bloquear el event
    loop con discos lentos."""
    import time
    if not ISOS_DIR.exists():
        return []

    root = ISOS_DIR.resolve()
    results: list[dict] = []
    # Dirs que ya hemos identificado como BDMV_folder — para no
    # devolver sus m2ts internos como entradas separadas.
    bdmv_folders: set[Path] = set()

    # Primera pasada: detectar carpetas BDMV. Las carpetas BDMV se
    # identifican por tener BDMV/PLAYLIST/ dentro a profundidad 1.
    # Esto es eficiente y cubre la convención estándar.
    for entry in root.rglob("*"):
        # Limitar profundidad
        try:
            depth = len(entry.relative_to(root).parts)
        except ValueError:
            continue
        if depth > _SOURCES_SCAN_MAX_DEPTH:
            continue

        if entry.is_dir() and entry.name == "BDMV":
            # La carpeta BDMV está aquí — la carpeta padre es el source.
            parent = entry.parent
            if (entry / "PLAYLIST").exists():
                bdmv_folders.add(parent)

    # Segunda pasada: clasificar cada entrada.
    for entry in root.rglob("*"):
        try:
            depth = len(entry.relative_to(root).parts)
        except ValueError:
            continue
        if depth > _SOURCES_SCAN_MAX_DEPTH:
            continue

        # Skip si está dentro de una BDMV folder ya identificada (no
        # listar los m2ts/MPLS internos como sources independientes).
        is_inside_bdmv = any(
            bdmv in entry.parents for bdmv in bdmv_folders
        )

        if entry.is_file():
            ext = entry.suffix.lower()
            if ext == ".iso":
                results.append({
                    "type": "iso",
                    "path": str(entry.relative_to(root)),
                    "name": entry.name,
                    "size_bytes": entry.stat().st_size,
                })
            elif ext == ".m2ts" and not is_inside_bdmv:
                # Solo m2ts sueltos (no dentro de un BDMV folder)
                results.append({
                    "type": "m2ts",
                    "path": str(entry.relative_to(root)),
                    "name": entry.name,
                    "size_bytes": entry.stat().st_size,
                })
        elif entry.is_dir() and entry in bdmv_folders:
            # Tamaño total = suma del directorio BDMV/STREAM/
            stream_dir = entry / "BDMV" / "STREAM"
            total = 0
            if stream_dir.exists():
                try:
                    total = sum(
                        f.stat().st_size for f in stream_dir.glob("*.m2ts")
                    )
                except OSError:
                    pass
            results.append({
                "type": "bdmv_folder",
                "path": str(entry.relative_to(root)),
                "name": entry.name,
                "size_bytes": total,
            })

    results.sort(key=lambda r: r["path"].lower())
    return results


@app.get("/api/sources", summary="Lista fuentes disponibles (ISO, carpeta BDMV, m2ts suelto)")
async def list_sources():
    """Escanea /mnt/isos recursivamente (max depth 3) y clasifica cada
    entrada en uno de los 3 tipos. Cache 60s para no martillear el FS.

    DEV_MODE: solo devuelve las ISOs fake (carpetas BDMV y m2ts no
    aplican en DEV)."""
    import time
    if DEV_MODE:
        return {
            "sources": [
                {"type": "iso", "path": f, "name": Path(f).name, "size_bytes": 0}
                for f in DEV_FAKE_ISOS
            ]
        }

    now = time.time()
    if _SOURCES_CACHE["data"] is not None and now - _SOURCES_CACHE["ts"] < _SOURCES_CACHE_TTL:
        return {"sources": _SOURCES_CACHE["data"], "cached": True}

    sources = await asyncio.to_thread(_scan_sources_in_dir)
    _SOURCES_CACHE["data"] = sources
    _SOURCES_CACHE["ts"] = now
    return {"sources": sources, "cached": False}


# ── CRUD de sesiones ──────────────────────────────────────────────────────────

@app.get("/api/sessions", summary="Lista todas las sesiones")
async def get_sessions():
    sessions = list_sessions()
    return {"sessions": [s.model_dump() for s in sessions]}


@app.get("/api/sessions/{session_id}", summary="Obtiene una sesión")
async def get_session(session_id: str):
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    return session.model_dump()


@app.delete("/api/sessions/{session_id}", summary="Elimina una sesión")
async def remove_session(session_id: str):
    if not delete_session(session_id):
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    return {"ok": True}


@app.put("/api/sessions/{session_id}", summary="Actualiza una sesión (Fase C)")
async def update_session(session_id: str, body: SessionUpdateRequest):
    """
    Aplica las ediciones del usuario sobre una sesión existente (partial update).
    Solo se actualizan los campos presentes en el body.
    """
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    if body.has_fel           is not None: session.has_fel           = body.has_fel
    if body.audio_dcp         is not None: session.audio_dcp         = body.audio_dcp
    if body.mkv_name          is not None: session.mkv_name          = body.mkv_name
    if body.mkv_name_manual   is not None: session.mkv_name_manual   = body.mkv_name_manual
    if body.included_tracks   is not None: session.included_tracks   = body.included_tracks
    if body.discarded_tracks  is not None: session.discarded_tracks  = body.discarded_tracks
    if body.chapters          is not None: session.chapters          = body.chapters

    save_session(session)
    return session.model_dump()


from pydantic import BaseModel as _BaseModel


class ReapplyModeRequest(_BaseModel):
    audio_mode: str | None = None       # 'filtered' | 'keep_all'
    subtitle_mode: str | None = None


@app.post("/api/sessions/{session_id}/reapply-rules",
          summary="Re-ejecuta Fase B con modos de audio/subtítulos distintos")
async def reapply_rules(session_id: str, body: ReapplyModeRequest):
    """Re-aplica las reglas de selección con el modo indicado sin re-montar
    el ISO. Regenera included_tracks / discarded_tracks a partir del
    bdinfo_result ya cacheado en la sesión.
    """
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    if not session.bdinfo_result:
        raise HTTPException(status_code=400, detail="La sesión no tiene bdinfo_result — re-analiza el ISO primero")

    if body.audio_mode is not None:
        if body.audio_mode not in ("filtered", "keep_all"):
            raise HTTPException(status_code=400, detail=f"audio_mode inválido: {body.audio_mode}")
        session.audio_mode = body.audio_mode
    if body.subtitle_mode is not None:
        if body.subtitle_mode not in ("filtered", "keep_all"):
            raise HTTPException(status_code=400, detail=f"subtitle_mode inválido: {body.subtitle_mode}")
        session.subtitle_mode = body.subtitle_mode

    # Re-aplicar reglas con los modos actuales
    rules = apply_rules(
        session.bdinfo_result,
        session.iso_path,
        session.audio_dcp,
        audio_mode=session.audio_mode,
        subtitle_mode=session.subtitle_mode,
    )
    session.included_tracks = rules["included_tracks"]
    session.discarded_tracks = rules["discarded_tracks"]
    session.ambiguous_audio_langs = rules.get("ambiguous_audio_langs", [])
    session.ambiguous_subtitle_langs = rules.get("ambiguous_subtitle_langs", [])
    # No sobrescribir mkv_name si el usuario lo editó manualmente
    if not session.mkv_name_manual:
        session.mkv_name = rules["mkv_name"]
    save_session(session)
    return session.model_dump()


# ── Comprobar ISO duplicado ───────────────────────────────────────────────────

@app.post("/api/check-duplicate", summary="Comprueba si ya existe un proyecto para este origen")
async def check_duplicate(body: AnalyzeRequest):
    """
    Calcula la huella del origen (SHA-256 primer 1 MB + tamaño) y busca
    sesiones existentes con la misma huella. Permite detectar el mismo
    contenido incluso si se ha movido o renombrado.

    Acepta los 3 tipos de origen (iso, bdmv_folder, m2ts) — para
    bdmv_folder huella sobre el m2ts más grande, para m2ts sobre el
    fichero directo.

    Respuesta:
      - duplicate: True si hay ≥1 sesión con el mismo fingerprint.
      - sessions: lista completa de sesiones que comparten fingerprint
        (BDMV/ISO de serie pueden tener N episodios procesados → N).
      - session: legacy — la primera de la lista (compat con flujo
        película que solo espera 1 match).
    """
    from phases.iso_mount import safe_source_path, SourceError, Source
    from phases.phase_a import find_main_m2ts

    if body.source_type:
        stype = body.source_type
        spath = body.source_path or body.iso_path or ""
    elif body.iso_path:
        stype = "iso"
        spath = body.iso_path
    else:
        raise HTTPException(status_code=400, detail="Falta source_type/source_path o iso_path")

    try:
        source_abs = safe_source_path(spath, str(ISOS_DIR))
    except SourceError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not Path(source_abs).exists():
        raise HTTPException(status_code=400, detail=f"Origen no encontrado: {source_abs}")

    # Resolver target del fingerprint según tipo
    if stype == "bdmv_folder":
        fp_target = find_main_m2ts(source_abs) or source_abs
    else:
        fp_target = source_abs  # iso o m2ts directo

    fingerprint = compute_iso_fingerprint(fp_target) if Path(fp_target).is_file() else ""
    sessions = find_sessions_by_fingerprint(fingerprint) if fingerprint else []
    # Orden estable: por episode_number ascendente, luego por id (caso
    # mixto serie+película o duplicados de movie sin episode_number).
    sessions.sort(key=lambda s: (s.episode_number or 0, s.id))

    return {
        "duplicate": len(sessions) > 0,
        "sessions": [s.model_dump() for s in sessions],
        "session": sessions[0].model_dump() if sessions else None,  # legacy/compat
        "fingerprint": fingerprint,
    }


# ── Análisis (Fase A + B) ─────────────────────────────────────────────────────

# Progreso del análisis en curso (para polling desde el frontend)
_analyze_progress: dict = {"step": "", "done": False}


@app.get("/api/analyze/progress", summary="Progreso del análisis en curso")
async def analyze_progress():
    """Devuelve el paso actual del análisis. Usado por el frontend para polling."""
    return _analyze_progress


# Progreso del disc-probe (modal "Detectando contenido"). Sin esto el modal
# mostraba una barra estática y un texto genérico ("Conectando con el
# servidor…") aunque la operación tarde 10-30s en discos grandes.
_disc_probe_progress: dict = {
    "running": False,
    "current_label": "",
    "pct": 0,         # 0-100; 0 = indeterminado
    "step": "",       # 'mount' | 'scan' | 'analyze' | 'classify' | 'done'
}


@app.get("/api/disc-probe/progress", summary="Progreso del disc-probe en curso")
async def disc_probe_progress():
    """Estado actual del disc-probe (single-job singleton). Usado por el
    modal "Detectando contenido" para mostrar el paso real con su % o
    barra indeterminada cuando no se puede medir."""
    return _disc_probe_progress


@app.post("/api/analyze", summary="Analiza un ISO (Fase A + B)")
async def analyze_iso(body: AnalyzeRequest):
    """
    Lanza el análisis completo de un origen (ISO, carpeta BDMV o M2TS).
    Devuelve la sesión lista para revisar.

    Pipeline:
      - ISO / bdmv_folder: mount (si ISO) → mkvmerge -J MPLS → capítulos
        → MediaInfo → dovi_tool → reglas.
      - m2ts: mkvmerge -J sobre el m2ts → capítulos auto-generados →
        MediaInfo → dovi_tool → reglas (PGS counting con rango default).
    """
    global _analyze_progress
    from phases.iso_mount import Source, SourceError, safe_source_path

    # Resolver source_type/source_path con compat para iso_path legacy
    if body.source_type:
        stype = body.source_type
        spath = body.source_path or body.iso_path or ""
    elif body.iso_path:
        stype = "iso"
        spath = body.iso_path
    else:
        raise HTTPException(status_code=400, detail="Falta source_type/source_path o iso_path")

    # ⚠️ DEV MODE — branch que devuelve fixtures sin tocar el filesystem
    if DEV_MODE:
        session = build_fake_session(str(ISOS_DIR / (spath or body.iso_path or "")))
        return session.model_dump()

    # Validación path-traversal estricta
    try:
        source_abs = safe_source_path(spath, str(ISOS_DIR))
    except SourceError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not Path(source_abs).exists():
        raise HTTPException(status_code=400, detail=f"Origen no encontrado: {source_abs}")

    audio_dcp = "audio dcp" in (spath or "").lower()

    # Captura el log emitido durante Fase A para guardarlo en la sesión.
    # Sirve para diagnóstico desde el modal "Datos ISO" sin tener que pedir
    # el log del container (PGS counting vacío, MediaInfo fallando, etc.).
    analysis_log: list[str] = []

    # Callback de progreso para el modal del frontend
    async def _progress_callback(msg: str):
        global _analyze_progress
        # Capturamos cada línea — barata, ~unas decenas por análisis.
        try:
            from datetime import datetime as _dt
            ts = _dt.now().strftime("%H:%M:%S")
            analysis_log.append(f"[{ts}] {msg}")
        except Exception:
            analysis_log.append(msg)
        # Mapear mensajes de log a pasos del modal. Matches especificos
        # para evitar falsos positivos (ej. el resumen final menciona
        # "packet_count" pero NO debe disparar el step pgs otra vez).
        msg_l = msg.lower()
        if "paso 1/4" in msg_l or "identificando mpls" in msg_l:
            _analyze_progress = {"step": "identify", "done": False}
        elif "paso 2/4" in msg_l or "extrayendo capítulos" in msg_l:
            _analyze_progress = {"step": "chapters", "done": False}
        elif "ejecutando mediainfo" in msg_l:
            _analyze_progress = {"step": "mediainfo", "done": False}
        elif "contando paquetes pgs" in msg_l:
            _analyze_progress = {"step": "pgs", "done": False, "pct": 0, "eta_s": 0}
        elif "paso 4/4" in msg_l or "analizando dolby vision" in msg_l:
            _analyze_progress = {"step": "dovi", "done": False}

    # Callback de progreso granular para el step PGS (bytes leídos por ffprobe)
    async def _pgs_progress_callback(pct: float, eta_s: int):
        global _analyze_progress
        _analyze_progress = {"step": "pgs", "done": False, "pct": round(pct, 1), "eta_s": eta_s}

    _analyze_progress = {"step": "mount", "done": False}

    # ── Fase A: análisis completo (mkvmerge + MediaInfo + dovi_tool) ─
    # Source context manager: monta el ISO si es necesario, no-op para
    # bdmv_folder y m2ts. Cleanup automático en __aexit__.
    mpls_path = ""
    mpls_chapters_raw: list[dict] = []
    bdinfo_result = None
    try:
        async with await Source.open(source_abs) as src:
            _analyze_progress = {"step": "identify", "done": False}
            if src.bdmv_root:
                # ISO ya montado o bdmv_folder directo
                bdinfo_result, mpls_path, mpls_chapters_raw = await run_full_analysis(
                    src.bdmv_root,
                    log_callback=_progress_callback,
                    pgs_progress_callback=_pgs_progress_callback,
                )
            else:
                # m2ts suelto (sin BDMV)
                from phases.phase_a import run_full_analysis_for_m2ts
                bdinfo_result, mpls_chapters_raw = await run_full_analysis_for_m2ts(
                    src.m2ts_paths[0],
                    log_callback=_progress_callback,
                    pgs_progress_callback=_pgs_progress_callback,
                )
                mpls_path = src.m2ts_paths[0]
    except SourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _logger.exception("Error en Fase A para %s", source_abs)
        raise HTTPException(status_code=500, detail=f"Error en Fase A: {e}")

    # ── Fase B: Reglas automáticas ─────────────────────────────────
    _analyze_progress = {"step": "rules", "done": False}
    # Las reglas usan el nombre del path para detectar etiquetas en el
    # filename (FEL, Audio DCP). Pasamos spath (path original del usuario)
    # que es lo más representativo del nombre semántico del contenido.
    rules_result = apply_rules(bdinfo_result, spath, audio_dcp)

    # ── Capítulos ─────────────────────────────────────────────────
    # Textos dinámicos según el tipo de origen — antes hablaban siempre de
    # "el disco" aunque el origen fuera carpeta BDMV o fichero M2TS suelto.
    from models import Chapter
    source_label = (
        "el disco" if stype == "iso"
        else "la carpeta BDMV" if stype == "bdmv_folder"
        else "el fichero M2TS"
    )
    if mpls_chapters_raw:
        chapters      = [Chapter(**c) for c in mpls_chapters_raw]
        chapters_auto = False
        # Para m2ts no hay MPLS — esta rama no se ejecuta (mpls_chapters_raw
        # está vacío para run_full_analysis_for_m2ts), así que el texto
        # menciona MPLS sin problema.
        chapters_reason = f"{len(chapters)} capítulos extraídos del disco (MPLS)"
    elif bdinfo_result.duration_seconds > 0:
        chapters      = generate_auto_chapters(bdinfo_result.duration_seconds)
        chapters_auto = True
        chapters_reason = (
            f"Sin capítulos en {source_label} — generados automáticamente cada 10 min"
        )
    else:
        chapters      = []
        chapters_auto = True
        chapters_reason = f"No se pudo determinar la duración de {source_label}"

    # ── Reutilizar sesión existente por fingerprint ─────────────────
    # Para ISO: huella del fichero .iso (1 MB + tamaño). Para
    # bdmv_folder: huella del m2ts más grande. Para m2ts: huella del
    # m2ts directo. compute_iso_fingerprint funciona sobre ficheros
    # arbitrarios, así que necesitamos resolver el "fichero principal":
    if stype == "iso":
        fp_target = source_abs
    elif stype == "m2ts":
        fp_target = source_abs  # el m2ts directo
    else:
        # bdmv_folder → el m2ts más grande de BDMV/STREAM/
        from phases.phase_a import find_main_m2ts
        fp_target = find_main_m2ts(source_abs) or source_abs
    fingerprint = compute_iso_fingerprint(fp_target) if Path(fp_target).is_file() else ""
    existing = find_session_by_fingerprint(fingerprint) if fingerprint else None
    if existing:
        session_id = existing.id
    else:
        session_id = make_session_id(spath or "source")

    session = Session(
        id=session_id,
        iso_path=source_abs,         # path absoluto del origen (compat)
        iso_fingerprint=fingerprint,
        status="pending",
        bdinfo_result=bdinfo_result,
        has_fel=bdinfo_result.has_fel,
        audio_dcp=audio_dcp,
        included_tracks=rules_result["included_tracks"],
        discarded_tracks=rules_result["discarded_tracks"],
        ambiguous_audio_langs=rules_result.get("ambiguous_audio_langs", []),
        ambiguous_subtitle_langs=rules_result.get("ambiguous_subtitle_langs", []),
        mkv_name=rules_result["mkv_name"],
        mkv_name_manual=False,
        chapters=chapters,
        chapters_auto_generated=chapters_auto,
        chapters_auto_reason=chapters_reason,
        analysis_log=analysis_log,
        source_type=stype,
        source_path=spath,
    )
    save_session(session)
    _analyze_progress = {"step": "done", "done": True}
    return session.model_dump()


# ══════════════════════════════════════════════════════════════════════
#  SERIES TV — endpoints (v2.5+)
#
#  Soporte para ISOs Blu-ray con múltiples episodios. Flujo:
#
#    1. POST /api/disc-probe           → monta, detecta tipo, devuelve
#                                         candidatos. NO crea sesión.
#    2. GET  /api/tv-search             → busca serie en TMDb.
#    3. GET  /api/tv-details/{id}       → temporadas disponibles.
#    4. GET  /api/tv-season/{id}/{N}    → episodios con runtime.
#    5. POST /api/create-series-sessions → crea N sesiones (una por
#                                          episodio seleccionado).
#
#  El endpoint /api/analyze original NO cambia — sigue siendo el flujo
#  película. El frontend decide qué endpoint llamar tras disc-probe.
# ══════════════════════════════════════════════════════════════════════


class DiscProbeRequest(_BaseModel):
    """Payload de POST /api/disc-probe. Soporta los 3 tipos de fuente.

    Compat: si solo se pasa `iso_path` (sin source_type), se asume
    source_type='iso' (sesiones legacy / frontend antiguo).

    Para los nuevos tipos:
      source_type='bdmv_folder' + source_path='Mad Men S1 D1'
      source_type='m2ts'        + source_path='raw/X.m2ts' (1 fichero)
      source_type='m2ts'        + m2ts_paths=['raw/E01.m2ts', 'raw/E02.m2ts', ...]
                                  (multi-fichero → modo serie)

    `media_type_hint` (v2.7+): el usuario elige explícitamente 'movie' o
    'series' en el modal del frontend. El backend respeta esa elección y
    omite la auto-detección. Si es None (frontend antiguo), se usa
    auto-detect como antes.
    """
    iso_path: str | None = None       # legacy compat
    source_type: str | None = None    # 'iso' | 'bdmv_folder' | 'm2ts'
    source_path: str | None = None    # path para iso/bdmv_folder/m2ts único
    m2ts_paths: list[str] | None = None  # solo para m2ts multi-fichero
    media_type_hint: str | None = None   # 'movie' | 'series' | None=auto


@app.post("/api/disc-probe",
          summary="Detecta tipo y devuelve candidatos. Soporta ISO, carpeta BDMV y m2ts sueltos")
async def disc_probe(body: DiscProbeRequest):
    """Detecta media_type y devuelve candidatos a episodio para los 3
    tipos de fuente. NO crea sesión.

    Comportamiento según source_type:
      - 'iso': monta el ISO, lista MPLS candidatos, desmonta.
      - 'bdmv_folder': lista MPLS candidatos sin mount.
      - 'm2ts' (1 fichero): devuelve media_type='movie' siempre — un
        solo m2ts es una película (no hay heurística que diga lo
        contrario sin BDMV).
      - 'm2ts' (N ficheros): cada m2ts es un candidato a episodio →
        media_type='series', sin auto-detect (el usuario eligió varios).

    Coste:
      - iso: ~10-20s (mount + identify)
      - bdmv_folder: ~5-15s (identify sin mount)
      - m2ts: ~1-5s por fichero (mkvmerge -J de cada uno)
    """
    from phases.phase_a import (
        identify_episode_candidates, detect_disc_type,
        identify_episode_candidates_from_m2ts_list,
    )
    from phases.phase_b import _extract_title_year
    from phases.iso_mount import Source, SourceError, safe_source_path

    # Resolver source_type — compat con frontend antiguo (solo iso_path).
    if body.source_type:
        stype = body.source_type
        spath = body.source_path or body.iso_path or ""
    elif body.iso_path:
        stype = "iso"
        spath = body.iso_path
    else:
        raise HTTPException(status_code=400, detail="Falta source_type/source_path o iso_path")

    # Validación path-traversal estricta. Para m2ts multi-fichero
    # validamos cada path individualmente.
    try:
        if stype == "m2ts" and body.m2ts_paths:
            validated_paths = [
                safe_source_path(p, str(ISOS_DIR)) for p in body.m2ts_paths
            ]
            spath_abs = validated_paths[0] if validated_paths else ""
        else:
            spath_abs = safe_source_path(spath, str(ISOS_DIR))
            validated_paths = None
    except SourceError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Título sugerido extraído del nombre del path (idéntico al flujo
    # película — el frontend lo usa como query inicial para TMDb).
    suggested_title, suggested_year_str = _extract_title_year(
        spath if stype != "m2ts" else Path(spath_abs).parent.name + ".x"
    )
    suggested_year: int | None = None
    if suggested_year_str.isdigit() and suggested_year_str != "0000":
        suggested_year = int(suggested_year_str)

    candidates: list[dict] = []
    media_type: str = "movie"
    movie_warning: str | None = None
    hint = body.media_type_hint  # 'movie' | 'series' | None

    # Inicializa el progreso global del disc-probe. Single-job singleton:
    # si dos llamadas concurrentes se cruzan, la segunda sobrescribe a la
    # primera (el frontend no permite lanzar dos a la vez, pero por si
    # acaso). El modal hace polling de /api/disc-probe/progress y muestra
    # current_label + pct en lugar del texto genérico "Conectando…".
    global _disc_probe_progress
    _disc_probe_progress = {
        "running": True,
        "current_label": "Preparando origen…",
        "pct": 0,
        "step": "mount",
    }

    async def _scan_progress(idx: int, total: int, item_name: str):
        """Callback que reciben identify_episode_candidates* en cada MPLS/m2ts.
        Actualiza el progreso global con el % real y el nombre del fichero."""
        if total > 0:
            _disc_probe_progress["pct"] = round((idx / total) * 100, 1)
        _disc_probe_progress["current_label"] = (
            f"Analizando candidato {idx}/{total}: {item_name}"
        )
        _disc_probe_progress["step"] = "analyze"

    try:
        if stype == "m2ts":
            paths = validated_paths or [spath_abs]
            if hint == "movie":
                # El usuario eligió película → solo permitimos 1 m2ts.
                if len(paths) > 1:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Modo película seleccionado con {len(paths)} ficheros M2TS. "
                            f"Para procesar varios episodios, vuelve al modal y cambia a "
                            f"modo serie."
                        ),
                    )
                _disc_probe_progress.update({
                    "current_label": "Película + 1 fichero M2TS — no requiere análisis previo",
                    "pct": 100,
                    "step": "classify",
                })
                media_type = "movie"
                candidates = []
            elif hint == "series":
                # El usuario eligió serie → cada m2ts es un episodio.
                _disc_probe_progress.update({
                    "current_label": f"Analizando {len(paths)} ficheros M2TS…",
                    "pct": 0,
                    "step": "scan",
                })
                candidates = await identify_episode_candidates_from_m2ts_list(
                    paths, progress_callback=_scan_progress,
                )
                if not candidates:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Ningún fichero M2TS pasó los filtros de candidato a "
                            "episodio (sin audio o duración no determinable)."
                        ),
                    )
                media_type = "series"
            else:
                # hint=None (frontend antiguo o flujo legacy) → auto-detect.
                if len(paths) == 1:
                    media_type = "movie"
                    candidates = []
                else:
                    candidates = await identify_episode_candidates_from_m2ts_list(
                        paths, progress_callback=_scan_progress,
                    )
                    media_type = "series" if candidates else "movie"

        elif stype in ("iso", "bdmv_folder"):
            # Etiqueta inicial según tipo — el mount del ISO tarda varios
            # segundos sin progreso medible (no podemos predecir el tiempo
            # de un loop mount UDF), así que mostramos un texto descriptivo
            # con barra indeterminada (pct=0 hasta que arranque el scan).
            _disc_probe_progress.update({
                "current_label": (
                    "Montando el ISO…" if stype == "iso"
                    else "Leyendo la carpeta BDMV…"
                ),
                "pct": 0,
                "step": "mount",
            })
            async with await Source.open(spath_abs) as src:
                if not src.bdmv_root:
                    raise HTTPException(
                        status_code=400,
                        detail="No se pudo acceder al BDMV del origen.",
                    )
                if hint == "movie":
                    # El usuario eligió película → no listamos candidatos.
                    # Pero contamos los m2ts grandes (>5GB) en BDMV/STREAM/
                    # para advertir si el disco parece tener varios episodios.
                    _disc_probe_progress.update({
                        "current_label": "Contando ficheros M2TS de gran tamaño…",
                        "pct": 50,
                        "step": "classify",
                    })
                    media_type = "movie"
                    candidates = []
                    big_count = _count_big_m2ts(src.bdmv_root, min_size_gb=5.0)
                    if big_count >= 3:
                        movie_warning = (
                            f"Este origen tiene {big_count} ficheros M2TS de más de 5 GB. "
                            f"Parece un disco de serie con varios episodios. "
                            f"Si confirmas modo película se usará el MPLS principal "
                            f"(el de mayor duración). Cambia a modo serie si quieres "
                            f"procesar todos los episodios."
                        )
                    _disc_probe_progress.update({"pct": 100, "step": "done"})
                elif hint == "series":
                    _disc_probe_progress.update({
                        "current_label": "Buscando episodios candidatos…",
                        "pct": 0,
                        "step": "scan",
                    })
                    candidates = await identify_episode_candidates(
                        src.bdmv_root, progress_callback=_scan_progress,
                    )
                    if not candidates:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                "Ningún MPLS pasó los filtros de candidato a episodio "
                                "(duración 15-90 min, ≥1 audio, m2ts ≥40% mediana). "
                                "Cambia a modo película si es un disco de un solo título."
                            ),
                        )
                    media_type = "series"
                else:
                    # Legacy auto-detect
                    _disc_probe_progress.update({
                        "current_label": "Buscando episodios candidatos…",
                        "pct": 0,
                        "step": "scan",
                    })
                    candidates = await identify_episode_candidates(
                        src.bdmv_root, progress_callback=_scan_progress,
                    )
                    media_type = detect_disc_type(candidates)
        else:
            raise HTTPException(status_code=400, detail=f"source_type desconocido: {stype}")
        _disc_probe_progress.update({
            "current_label": f"Detección completada ({media_type})",
            "pct": 100,
            "step": "done",
            "running": False,
        })
    except HTTPException:
        _disc_probe_progress["running"] = False
        raise
    except SourceError as e:
        _disc_probe_progress["running"] = False
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _disc_probe_progress["running"] = False
        _logger.exception("Error en disc-probe para %s", spath_abs)
        raise HTTPException(status_code=500, detail=f"Error analizando disco: {e}")

    # Lista de candidatos en formato ligero (sin el JSON crudo de mkvmerge)
    episode_candidates = []
    if media_type in ("series", "ambiguous"):
        for c in candidates:
            episode_candidates.append({
                "mpls_name": c["mpls_name"],
                "mpls_path": c["mpls_path"],
                "duration_minutes": round(c["duration_minutes"], 2),
                "audio_track_count": c["audio_track_count"],
            })

    return {
        "media_type": media_type,
        "source_type": stype,
        "source_path": spath,
        "iso_path": body.iso_path,         # compat
        "suggested_title": suggested_title,
        "suggested_year": suggested_year,
        "episode_candidates": episode_candidates,
        # Warning informativo cuando el usuario eligió "película" pero el
        # origen parece tener varios episodios. None si no aplica.
        "movie_warning": movie_warning,
    }


def _count_big_m2ts(bdmv_root: str, min_size_gb: float = 5.0) -> int:
    """Cuenta los ficheros .m2ts en BDMV/STREAM/ por encima de un umbral.

    Usado para detectar si un disco "modo película" tiene en realidad
    pinta de serie (varios m2ts grandes = episodios independientes). No
    abre los ficheros — solo stat() — por lo que es ~ms incluso para
    BDMV con cientos de ficheros.
    """
    stream_dir = Path(bdmv_root) / "BDMV" / "STREAM"
    if not stream_dir.exists():
        return 0
    threshold = int(min_size_gb * 1_000_000_000)
    count = 0
    try:
        for f in stream_dir.glob("*.m2ts"):
            try:
                if f.stat().st_size >= threshold:
                    count += 1
            except OSError:
                continue
    except OSError:
        return 0
    return count


@app.get("/api/tv-search",
         summary="Busca series en TMDb por título (selector multi-candidato)")
async def tv_search(query: str, year: int | None = None):
    """Top 5 candidatos TMDb para una query de serie. `year` filtra por
    first_air_date_year (premiere de la serie, no air_date de episodios).
    El frontend muestra los resultados y el usuario elige."""
    from services.tmdb import search_tv_series, is_configured
    if not is_configured():
        return {"tmdb_configured": False, "results": []}
    results = await search_tv_series(query, year)
    return {
        "tmdb_configured": True,
        "query": query,
        "year": year,
        "results": [r.model_dump() for r in results],
    }


@app.get("/api/tv-details/{tmdb_id}",
         summary="Detalles de una serie TMDb (temporadas, número de episodios)")
async def tv_details(tmdb_id: int):
    """Detalles extendidos de una serie: name, year, number_of_seasons,
    seasons[]. El frontend usa esto para poblar el combo de temporadas
    antes de pedir los episodios."""
    from services.tmdb import fetch_tv_details, is_configured
    if not is_configured():
        return {"tmdb_configured": False, "details": None}
    details = await fetch_tv_details(tmdb_id)
    if not details:
        raise HTTPException(status_code=404, detail=f"Serie TMDb {tmdb_id} no encontrada")
    return {"tmdb_configured": True, "details": details.model_dump()}


@app.get("/api/tv-season/{tmdb_id}/{season_number}",
         summary="Episodios de una temporada + match heurístico contra MPLS candidatos")
async def tv_season(
    tmdb_id: int,
    season_number: int,
    mpls_durations: str | None = None,
):
    """Devuelve episodes[] de la temporada con runtime, name, air_date.

    Si se pasa `mpls_durations` (lista coma-separada de duraciones en
    minutos, en el orden de los MPLS), incluye también un array
    `mpls_matches[]` con la heurística de match: para cada MPLS,
    sugested_episode_number + matched_episode + confidence (high/low/
    unknown) + runtime_delta_min.

    El frontend usa los matches para pre-rellenar el mapping y dar el
    hint visual (🟢🟡) sin tener que computarlo client-side.
    """
    from services.tmdb import fetch_tv_season, match_episodes_to_mpls, is_configured
    if not is_configured():
        return {"tmdb_configured": False, "episodes": [], "mpls_matches": []}
    episodes = await fetch_tv_season(tmdb_id, season_number)
    mpls_matches = []
    if mpls_durations:
        try:
            durations = [float(x) for x in mpls_durations.split(",") if x.strip()]
            mpls_matches = match_episodes_to_mpls(durations, episodes)
        except ValueError:
            # mpls_durations malformado: devolvemos sin matches.
            mpls_matches = []
    return {
        "tmdb_configured": True,
        "season_number": season_number,
        "episodes": [e.model_dump() for e in episodes],
        "mpls_matches": mpls_matches,
    }


class SeriesEpisodeSelection(_BaseModel):
    """Una selección de episodio en POST /api/create-series-sessions."""
    mpls_path: str
    episode_number: int
    episode_title: str = ""
    runtime_minutes: int = 0
    # Datos opcionales del episodio para enriquecer session.tmdb_info
    # (cabecera de la pestaña). Si vienen vacíos, no se persiste tmdb_info.
    episode_overview: str = ""
    episode_still_url: str = ""


class CreateSeriesSessionsRequest(_BaseModel):
    """Payload de POST /api/create-series-sessions. Soporta 3 tipos de
    origen como el resto del flujo.

    Compat: si solo se pasa iso_path, se asume source_type='iso'.

    `mode` controla qué hacer cuando algún episodio ya tiene sesión
    persistida (mismo session_id derivado del fingerprint + episode_number):
      - "add_only" (default): falla con 409 si hay conflictos. El frontend
        muestra la lista y deja al usuario confirmar reemplazo. Es el modo
        seguro — evita sobrescribir ediciones del usuario.
      - "replace": sobrescribe los conflictos sin preguntar (legacy).
      - "skip_existing": ignora los conflictos y crea solo los nuevos.
    """
    iso_path: str | None = None        # legacy compat
    source_type: str | None = None     # 'iso' | 'bdmv_folder' | 'm2ts'
    source_path: str | None = None
    m2ts_paths: list[str] | None = None  # solo si source_type='m2ts' multi-fichero
    series_tmdb_id: int | None = None
    series_name: str
    series_year: int | None = None
    # Datos opcionales de la serie para enriquecer la cabecera de cada
    # episodio (poster/backdrop comunes a toda la temporada).
    series_poster_url: str = ""
    series_backdrop_url: str = ""
    series_overview: str = ""
    series_genres: list[str] = []
    series_vote_average: float = 0.0
    season_number: int
    episodes: list[SeriesEpisodeSelection]
    mode: str = "add_only"  # 'add_only' | 'replace' | 'skip_existing'


# Progreso global de create_series_sessions (single-job singleton).
# El frontend lo polleeará via /api/series-create-progress para mostrar
# feedback durante el bucle de N episodios.
_series_create_progress: dict = {
    "running": False,
    "current_index": 0,
    "total": 0,
    "current_label": "",
    "completed": [],  # nombres de episodios ya procesados (success)
    "failed": [],
    # Sub-progreso dentro del episodio en curso. step ∈ {identify, mediainfo,
    # pgs, dovi, rules, save}. pgs_pct (0-100) granular para la fase larga.
    "current_episode_step": "",
    "current_episode_title": "",
    "pgs_pct": 0,
    "pgs_eta_s": 0,
}


@app.get("/api/series-create-progress",
         summary="Polling del progreso de /api/create-series-sessions")
async def series_create_progress():
    """Devuelve el estado actual del único job de creación de sesiones en
    curso. Si no hay ninguno, `running=False` y los campos quedan vacíos."""
    return _series_create_progress


@app.post("/api/create-series-sessions",
          summary="Crea N sesiones (una por episodio) tras confirmar el mapping serie")
async def create_series_sessions(body: CreateSeriesSessionsRequest):
    """Analiza cada MPLS/M2TS seleccionado completamente y crea una
    sesión `pending` por episodio.

    Soporta los 3 tipos de origen:
      - 'iso': monta el ISO una vez, re-deriva cada MPLS del mount actual.
      - 'bdmv_folder': los MPLS del payload son paths relativos a la
        carpeta BDMV; los resuelve en su ubicación real.
      - 'm2ts': el ep.mpls_path apunta directamente al fichero .m2ts
        (cada m2ts = un episodio).

    El usuario lanza cada sesión manualmente desde el panel del proyecto.
    Coste: ~30s + N × 15-30s.
    """
    from phases.phase_a import run_full_analysis_for_mpls, run_full_analysis_for_m2ts, find_main_m2ts
    from phases.phase_b import apply_rules, build_series_mkv_name
    from phases.iso_mount import Source, SourceError, safe_source_path
    from models import Chapter

    if DEV_MODE:
        raise HTTPException(
            status_code=400,
            detail="DEV_MODE no soporta create-series-sessions (no hay sources reales)",
        )

    if not body.episodes:
        raise HTTPException(status_code=400, detail="Lista de episodios vacía")

    # Resolver source_type/source_path con compat para iso_path legacy
    if body.source_type:
        stype = body.source_type
        spath = body.source_path or body.iso_path or ""
    elif body.iso_path:
        stype = "iso"
        spath = body.iso_path
    else:
        raise HTTPException(status_code=400, detail="Falta source_type/source_path o iso_path")

    # Validar path principal (no aplica para m2ts multi-fichero donde
    # cada ep.mpls_path es el path directo)
    try:
        if stype == "m2ts" and body.m2ts_paths:
            for p in body.m2ts_paths:
                safe_source_path(p, str(ISOS_DIR))
            source_abs = safe_source_path(body.m2ts_paths[0], str(ISOS_DIR))
        else:
            source_abs = safe_source_path(spath, str(ISOS_DIR))
    except SourceError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not Path(source_abs).exists():
        raise HTTPException(status_code=400, detail=f"Origen no encontrado: {source_abs}")

    audio_dcp = "audio dcp" in (spath or "").lower()

    # Fingerprint: para iso del .iso, para bdmv del m2ts más grande,
    # para m2ts del primero (compartido entre todos los episodios).
    if stype == "bdmv_folder":
        fp_target = find_main_m2ts(source_abs) or source_abs
    else:
        fp_target = source_abs
    fingerprint = compute_iso_fingerprint(fp_target) if Path(fp_target).is_file() else ""

    # ── Detección de conflictos por (fingerprint, season, episode_number) ──
    # Para BDMV/ISO de serie, todas las sesiones de episodios del mismo
    # disco comparten fingerprint. Si el usuario está rehaciendo solo
    # uno (o añadiendo nuevos), NO queremos crear duplicados ni
    # sobrescribir silenciosamente las existentes. find_sessions_by_
    # fingerprint nos da el conjunto; cruzamos con (season, episode_number)
    # para identificar conflictos exactos.
    existing_by_episode: dict[tuple[int, int], "Session"] = {}
    if fingerprint:
        for s in find_sessions_by_fingerprint(fingerprint):
            if s.media_type == "series" and s.season_number and s.episode_number:
                existing_by_episode[(s.season_number, s.episode_number)] = s

    requested_keys = [(body.season_number, ep.episode_number) for ep in body.episodes]
    conflicts = [
        existing_by_episode[k] for k in requested_keys if k in existing_by_episode
    ]
    mode = (body.mode or "add_only").lower()
    if mode not in ("add_only", "replace", "skip_existing"):
        raise HTTPException(status_code=400, detail=f"mode inválido: {body.mode}")
    if conflicts and mode == "add_only":
        # El frontend muestra la lista y deja al usuario elegir si quiere
        # reemplazar o saltar los conflictos. Sin esta protección, el bug
        # del usuario: rehacer 1 episodio duplicaba en disco (timestamps)
        # o sobrescribía sesiones hermanas según el flujo.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "episode_conflicts",
                "message": (
                    f"{len(conflicts)} episodio(s) ya tienen una sesión existente. "
                    f"Reenvía con mode='replace' para sobrescribir o "
                    f"mode='skip_existing' para crear solo los nuevos."
                ),
                "conflicts": [
                    {
                        "id": s.id,
                        "mkv_name": s.mkv_name,
                        "season_number": s.season_number,
                        "episode_number": s.episode_number,
                        "episode_title": s.episode_title,
                        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                    }
                    for s in conflicts
                ],
            },
        )

    # Determinar qué episodios procesar y cuáles saltar/reemplazar
    skipped_existing: list[dict] = []
    to_replace_ids: list[str] = []
    episodes_to_process = []
    for ep in body.episodes:
        key = (body.season_number, ep.episode_number)
        existing = existing_by_episode.get(key)
        if existing and mode == "skip_existing":
            skipped_existing.append({
                "season_number": body.season_number,
                "episode_number": ep.episode_number,
                "existing_id": existing.id,
            })
            continue
        if existing and mode == "replace":
            to_replace_ids.append(existing.id)
        episodes_to_process.append(ep)

    # Borrar las sesiones a reemplazar antes del bucle — evita ambigüedad
    # si dos episodios en la misma petición apuntaran al mismo id (no
    # debería pasar, pero por seguridad).
    for sid in to_replace_ids:
        try:
            delete_session(sid)
        except Exception as _e:
            _logger.warning("No se pudo borrar sesión existente %s: %s", sid, _e)

    created_sessions = []
    failed_episodes: list[dict] = []
    # Reset del progreso global. Si otro job estaba en curso, lo
    # sobrescribimos (el endpoint es single-job).
    global _series_create_progress
    # Etiqueta de origen amigable según tipo — sin jerga ('source_type' /
    # 'stype' eran términos internos). El usuario ve "Montando el ISO…",
    # no "Montando origen (iso)…".
    _prep_label = (
        "Montando el ISO…" if stype == "iso"
        else "Preparando carpeta BDMV…" if stype == "bdmv_folder"
        else "Preparando ficheros M2TS…"
    )
    _series_create_progress = {
        "running": True,
        "current_index": 0,
        "total": len(body.episodes),
        "current_label": _prep_label,
        "completed": [],
        "failed": [],
        "current_episode_step": "mount",
        "current_episode_title": "",
        "pgs_pct": 0,
        "pgs_eta_s": 0,
    }

    # Callback de progreso intra-episodio. Mapea líneas del log de
    # phase_a a los sub-pasos del modal para que la barra avance gradual
    # dentro de cada episodio (antes saltaba 33%/66%/100% sin detalle).
    # El orden de los elif importa: lo más específico primero.
    async def _ep_progress_callback(msg: str):
        msg_l = msg.lower()
        if "contando paquetes pgs" in msg_l:
            _series_create_progress["current_episode_step"] = "pgs"
            _series_create_progress["pgs_pct"] = 0
            _series_create_progress["pgs_eta_s"] = 0
        elif "paso 4/4" in msg_l or "analizando dolby vision" in msg_l or "dolby vision con dovi_tool" in msg_l:
            _series_create_progress["current_episode_step"] = "dovi"
        elif "paso 3/4" in msg_l or ("enriqueciendo con mediainfo" in msg_l) or ("analizando m2ts del episodio" in msg_l):
            _series_create_progress["current_episode_step"] = "mediainfo"
        elif "paso 2/4" in msg_l or ("extrayendo capítulos" in msg_l) or ("sin mpls" in msg_l and "auto-generarán" in msg_l):
            _series_create_progress["current_episode_step"] = "chapters"
        elif "paso 1/4" in msg_l or "identificando pistas" in msg_l:
            _series_create_progress["current_episode_step"] = "identify"

    async def _ep_pgs_progress_callback(pct: float, eta_s: int):
        _series_create_progress["current_episode_step"] = "pgs"
        _series_create_progress["pgs_pct"] = round(pct, 1)
        _series_create_progress["pgs_eta_s"] = eta_s

    # Si tras filtrar conflictos no queda nada que procesar (skip_existing
    # con todos los episodios pedidos ya existentes), devolvemos respuesta
    # vacía sin entrar al bucle de análisis.
    if not episodes_to_process:
        _series_create_progress["running"] = False
        return {
            "created": [],
            "failed": [],
            "skipped_existing": skipped_existing,
            "iso_path": body.iso_path,
        }

    # Recalculamos `total` del progreso para reflejar solo lo que vamos a
    # procesar de verdad (los saltados ya no cuentan).
    _series_create_progress["total"] = len(episodes_to_process)

    try:
        # Context manager: monta el ISO si stype='iso', no-op si bdmv/m2ts.
        async with await Source.open(source_abs) as src:
            _series_create_progress["current_label"] = "Origen preparado · empezando con el primer episodio"
            for idx, ep in enumerate(episodes_to_process):
                _series_create_progress["current_index"] = idx + 1
                _series_create_progress["current_episode_step"] = "identify"
                _series_create_progress["pgs_pct"] = 0
                _series_create_progress["pgs_eta_s"] = 0
                ep_label = ep.episode_title or f"Episodio S{body.season_number:02d}E{ep.episode_number:02d}"
                _series_create_progress["current_episode_title"] = ep_label
                _series_create_progress["current_label"] = (
                    f"Analizando episodio {idx+1}/{len(episodes_to_process)}: {ep_label}"
                )
                # Localizar el MPLS/M2TS de este episodio según source_type
                ep_source_path: str | None = None
                if stype in ("iso", "bdmv_folder") and src.bdmv_root:
                    # Re-derivar del mount/carpeta actual usando nombre del MPLS
                    mpls_name = Path(ep.mpls_path).name
                    for candidate in [
                        Path(src.bdmv_root) / "BDMV" / "PLAYLIST" / mpls_name,
                        Path(src.bdmv_root) / "PLAYLIST" / mpls_name,
                    ]:
                        if candidate.exists():
                            ep_source_path = str(candidate)
                            break
                    if ep_source_path is None:
                        failed_episodes.append({
                            "episode_number": ep.episode_number,
                            "error": f"MPLS {mpls_name} no encontrado en {src.bdmv_root}",
                        })
                        continue
                else:
                    # m2ts: ep.mpls_path apunta directamente al fichero
                    try:
                        ep_source_path = safe_source_path(ep.mpls_path, str(ISOS_DIR))
                    except SourceError as e:
                        failed_episodes.append({
                            "episode_number": ep.episode_number,
                            "error": str(e),
                        })
                        continue
                    if not Path(ep_source_path).exists():
                        failed_episodes.append({
                            "episode_number": ep.episode_number,
                            "error": f"M2TS {ep.mpls_path} no encontrado",
                        })
                        continue

                # Análisis: para iso/bdmv usamos for_mpls (necesita bdmv_root);
                # para m2ts usamos for_m2ts (sin BDMV). Los callbacks
                # actualizan _series_create_progress["current_episode_step"]
                # en tiempo real → la barra avanza gradual.
                try:
                    if stype == "m2ts":
                        bdinfo, mpls_chapters_raw = await run_full_analysis_for_m2ts(
                            ep_source_path,
                            log_callback=_ep_progress_callback,
                            pgs_progress_callback=_ep_pgs_progress_callback,
                        )
                    else:
                        bdinfo, mpls_chapters_raw = await run_full_analysis_for_mpls(
                            src.bdmv_root, ep_source_path,
                            log_callback=_ep_progress_callback,
                            pgs_progress_callback=_ep_pgs_progress_callback,
                        )
                except Exception as e:
                    _logger.warning(
                        "Error analizando episodio %s de %s: %s",
                        ep.episode_number, source_abs, e,
                    )
                    failed_episodes.append({
                        "episode_number": ep.episode_number,
                        "error": str(e),
                    })
                    continue

                _series_create_progress["current_episode_step"] = "rules"
                rules_result = apply_rules(bdinfo, spath, audio_dcp)

                # Capítulos: igual que película — del MPLS si los hay,
                # sino auto. Texto del reason adaptado al tipo de origen
                # para que el panel del proyecto no mencione "MPLS" cuando
                # el episodio viene de un m2ts directo.
                ep_origin_label = (
                    "el MPLS del episodio" if stype in ("iso", "bdmv_folder")
                    else "el fichero M2TS"
                )
                if mpls_chapters_raw:
                    chapters = [Chapter(**c) for c in mpls_chapters_raw]
                    chapters_auto = False
                    chapters_reason = f"{len(chapters)} capítulos extraídos de {ep_origin_label}"
                elif bdinfo.duration_seconds > 0:
                    chapters = generate_auto_chapters(bdinfo.duration_seconds)
                    chapters_auto = True
                    chapters_reason = f"Sin capítulos en {ep_origin_label} — generados cada 10 min"
                else:
                    chapters = []
                    chapters_auto = True
                    chapters_reason = "No se pudo determinar la duración del episodio"

                # Nombre del MKV con jerarquía Plex/Jellyfin
                mkv_name = build_series_mkv_name(
                    series_name=body.series_name,
                    series_year=body.series_year,
                    season_number=body.season_number,
                    episode_number=ep.episode_number,
                    episode_title=ep.episode_title,
                    has_fel=bdinfo.has_fel,
                    audio_dcp=audio_dcp,
                )

                # ID único por episodio. Incluye S/E para que sea legible.
                import time as _t
                session_id = f"{_sanitize_id(body.series_name)}_S{body.season_number:02d}E{ep.episode_number:02d}_{int(_t.time())}"

                # mpls_path persistido: solo el nombre del MPLS (para iso/
                # bdmv el mount o la carpeta puede cambiar de ruta absoluta
                # entre montajes). Para m2ts guardamos el path absoluto
                # (es estable — no se monta).
                mpls_persist = (
                    Path(ep_source_path).name if stype != "m2ts"
                    else ep_source_path
                )

                # Construye tmdb_info por episodio para que la cabecera de
                # cada pestaña muestre datos del EPISODIO concreto, no de
                # la serie genérica (que es lo que hace hydrateTmdbCard
                # parseando el filename con search/movie — devolvía
                # falsos positivos tipo "Juego de Tronos: La última
                # guardia" para todos los episodios).
                #
                # Estructura compatible con renderTmdbCardHTML del
                # frontend: title (del episodio), year (serie), overview
                # (episodio si lo tiene, si no la serie), poster_url
                # (still del episodio si TMDb lo trae, si no poster de
                # serie como fallback), backdrop_url (serie).
                ep_tmdb_info: dict | None = None
                if body.series_name:
                    full_title = (
                        f"{body.series_name} · S{body.season_number:02d}E{ep.episode_number:02d}"
                        + (f" — {ep.episode_title}" if ep.episode_title else "")
                    )
                    ep_tmdb_info = {
                        "title": full_title,
                        "original_title": ep.episode_title or body.series_name,
                        "year": body.series_year,
                        "overview": ep.episode_overview or body.series_overview or "",
                        "poster_url": ep.episode_still_url or body.series_poster_url or "",
                        "backdrop_url": body.series_backdrop_url or "",
                        "runtime_minutes": ep.runtime_minutes or 0,
                        "vote_average": body.series_vote_average,
                        "vote_count": 0,
                        "genres": body.series_genres or [],
                        "tagline": "",
                        "imdb_id": "",
                        "homepage": "",
                        "tmdb_url": (
                            f"https://www.themoviedb.org/tv/{body.series_tmdb_id}/season/{body.season_number}/episode/{ep.episode_number}"
                            if body.series_tmdb_id else ""
                        ),
                    }

                session = Session(
                    id=session_id,
                    iso_path=source_abs,
                    iso_fingerprint=fingerprint,
                    status="pending",
                    bdinfo_result=bdinfo,
                    has_fel=bdinfo.has_fel,
                    audio_dcp=audio_dcp,
                    included_tracks=rules_result["included_tracks"],
                    discarded_tracks=rules_result["discarded_tracks"],
                    ambiguous_audio_langs=rules_result.get("ambiguous_audio_langs", []),
                    ambiguous_subtitle_langs=rules_result.get("ambiguous_subtitle_langs", []),
                    mkv_name=mkv_name,
                    mkv_name_manual=False,
                    chapters=chapters,
                    chapters_auto_generated=chapters_auto,
                    chapters_auto_reason=chapters_reason,
                    # Campos del modo serie
                    media_type="series",
                    series_tmdb_id=body.series_tmdb_id,
                    series_name=body.series_name,
                    series_year=body.series_year,
                    season_number=body.season_number,
                    episode_number=ep.episode_number,
                    episode_title=ep.episode_title,
                    episode_runtime_minutes=ep.runtime_minutes or None,
                    mpls_path=mpls_persist,
                    source_type=stype,
                    source_path=spath,
                    tmdb_info=ep_tmdb_info,
                )
                _series_create_progress["current_episode_step"] = "save"
                save_session(session)
                created_sessions.append(session.model_dump())
                _series_create_progress["completed"].append(ep_label)
                _series_create_progress["current_episode_step"] = "done"
    except SourceError as e:
        _series_create_progress["running"] = False
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _series_create_progress["running"] = False
        _logger.exception("Error global en create-series-sessions")
        raise HTTPException(status_code=500, detail=f"Error creando sesiones: {e}")

    # Marca progreso como terminado
    _series_create_progress["running"] = False
    _series_create_progress["current_label"] = (
        f"✓ {len(created_sessions)} proyecto{'' if len(created_sessions) == 1 else 's'} creado"
        f"{'' if len(created_sessions) == 1 else 's'}"
    )
    _series_create_progress["failed"] = failed_episodes

    return {
        "created": created_sessions,
        "failed": failed_episodes,
        "skipped_existing": skipped_existing,  # mode=skip_existing
        "replaced_ids": to_replace_ids,        # mode=replace — sesiones borradas antes de crear las nuevas
        "iso_path": body.iso_path,
    }


def _sanitize_id(s: str) -> str:
    """Sanitiza un string para usarlo como parte de un session_id.
    Reemplaza espacios y caracteres no alfanuméricos por guiones bajos.
    Resultado: ASCII safe y legible en logs."""
    import re as _re
    return _re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_") or "series"


# ── Recalcular nombre del MKV ─────────────────────────────────────────────────

@app.post(
    "/api/sessions/{session_id}/recalculate-name",
    summary="Recalcula el nombre del MKV",
)
async def recalculate_mkv_name(session_id: str):
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    if session.mkv_name_manual:
        return {"mkv_name": session.mkv_name, "manual": True}

    from phases.phase_b import _build_mkv_name, _extract_title_year
    title, year      = _extract_title_year(session.iso_path)
    new_name         = _build_mkv_name(title, year, session.has_fel, session.audio_dcp)
    session.mkv_name = new_name
    save_session(session)
    return {"mkv_name": new_name, "manual": False}


# ── Restaurar capítulos originales del disco ─────────────────────────────────

@app.post(
    "/api/sessions/{session_id}/reset-chapters",
    summary="Restaura los capítulos originales del disco",
)
async def reset_chapters(session_id: str):
    """
    Re-extrae los capítulos del MPLS original del disco y reemplaza
    los capítulos actuales de la sesión. Útil si el usuario ha hecho
    ediciones manuales y quiere volver a los capítulos del disco.

    Requiere montar el ISO temporalmente para leer el MPLS.
    """
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    # Restaurar capítulos del MPLS requiere mount → solo aplica a iso o
    # bdmv_folder. Para m2ts no hay MPLS — no se puede restaurar.
    available, source_type, source_path = _check_source_available(session)
    if not available:
        raise HTTPException(
            status_code=400,
            detail=f"Origen no disponible: {source_path}",
        )
    if source_type == "m2ts":
        raise HTTPException(
            status_code=400,
            detail="No hay capítulos que restaurar para fuentes M2TS sin MPLS. "
                   "Los capítulos auto-generados se mantienen.",
        )

    # Para iso/bdmv_folder usamos Source context manager (mount ISO si aplica)
    try:
        from phases.iso_mount import Source
        async with await Source.open(source_path) as src:
            if not src.bdmv_root:
                raise HTTPException(status_code=400, detail="BDMV no accesible")
            _, mpls_path = await run_mkvmerge_identify(src.bdmv_root)
            chapters_raw = parse_mpls_chapters(mpls_path)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al extraer capítulos: {e}")

    if not chapters_raw:
        raise HTTPException(status_code=404, detail="No se encontraron capítulos en el disco")

    from models import Chapter
    session.chapters = [Chapter(**c) for c in chapters_raw]
    session.chapters_auto_generated = False
    session.chapters_auto_reason = f"{len(session.chapters)} capítulos restaurados del disco (MPLS)"
    save_session(session)
    return session.model_dump()


# ── Ejecución del pipeline (Fases D + E) ──────────────────────────────────────

def _check_source_available(session) -> tuple[bool, str, str]:
    """Verifica que el origen referenciado por la sesión sigue disponible
    según su source_type. Devuelve (available, source_type, source_label).

    Para los 3 tipos de origen (v2.6+):
      - 'iso': el fichero .iso debe existir.
      - 'bdmv_folder': la carpeta debe existir Y contener BDMV/PLAYLIST/.
      - 'm2ts': el fichero .m2ts debe existir (uno o el primero del set).

    Compat: sesiones legacy sin source_type lo asumen 'iso'.
    """
    source_type = getattr(session, 'source_type', '') or "iso"
    p = Path(session.iso_path)
    if not p.exists():
        return False, source_type, str(p)
    if source_type == "iso":
        available = p.is_file() and p.suffix.lower() == ".iso"
    elif source_type == "bdmv_folder":
        available = p.is_dir() and (
            (p / "BDMV" / "PLAYLIST").exists() or (p / "PLAYLIST").exists()
        )
    elif source_type == "m2ts":
        available = p.is_file() and p.suffix.lower() == ".m2ts"
    else:
        # Desconocido — comprobación mínima de existencia
        available = True
    return available, source_type, str(p)


@app.get("/api/sessions/{session_id}/check-iso", summary="Comprueba si el origen de la sesión está disponible")
async def check_iso(session_id: str):
    """
    Verifica que el origen referenciado por la sesión sigue disponible.
    Funciona para los 3 tipos: ISO, carpeta BDMV y ficheros M2TS.
    No monta ni lee el origen — solo verifica existencia/estructura.

    Respuesta:
      {
        "available": true|false,
        "iso_path": "..." (path del origen, mantiene nombre legacy),
        "source_type": "iso"|"bdmv_folder"|"m2ts",
        "source_label": "ISO"|"carpeta BDMV"|"fichero M2TS"
      }
    """
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    available, source_type, source_path = _check_source_available(session)
    source_label = {
        "iso": "ISO",
        "bdmv_folder": "carpeta BDMV",
        "m2ts": "fichero M2TS",
    }.get(source_type, "origen")
    return {
        "available": available,
        "iso_path": session.iso_path,
        "source_type": source_type,
        "source_label": source_label,
    }


@app.post("/api/sessions/{session_id}/execute", summary="Encola la sesión para Fases D+E")
async def execute_session(session_id: str):
    """
    Añade la sesión a la cola de ejecución (Fase D + Fase E).

    Si no hay ningún trabajo en curso, se inicia inmediatamente.
    En caso contrario, queda en estado 'queued' hasta que le toque.
    El output se transmite por WebSocket a ``/ws/{session_id}``.
    Devuelve 400 si el ISO referenciado no existe o no es un .iso válido.
    """
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    # Solo se puede ejecutar desde estados pending, error o done (re-ejecución tras editar)
    if session.status in ("running", "queued"):
        raise HTTPException(
            status_code=400,
            detail=f"La sesión ya está en ejecución o encolada (estado: {session.status}). "
                   f"Espera a que termine o cancela el trabajo actual.",
        )

    # Verificación del origen — soporta los 3 tipos (iso, bdmv_folder, m2ts)
    available, source_type, source_path = _check_source_available(session)
    if not available:
        type_label = {
            "iso": "ISO",
            "bdmv_folder": "carpeta BDMV",
            "m2ts": "fichero M2TS",
        }.get(source_type, "origen")
        raise HTTPException(
            status_code=400,
            detail=f"{type_label} no disponible: {source_path}. Comprueba que sigue en /mnt/isos.",
        )

    session.status        = "queued"
    session.output_log    = []
    session.error_message = None
    save_session(session)

    queue_status = await queue_manager.enqueue(session_id)
    return {"ok": True, "session_id": session_id, **queue_status}


async def _run_pipeline(session_id: str) -> None:
    """
    Corutina interna que ejecuta el pipeline de extracción en background.
    Llamada por queue_manager cuando le toca el turno al trabajo.

    Flujo optimizado (compatible con los 3 tipos de origen):
      1. Prepara el origen vía Source:
         - 'iso' → monta loop mount UDF en /mnt/bd
         - 'bdmv_folder' → no-op (carpeta directa)
         - 'm2ts' → no-op (fichero directo, sin BDMV)
      2. Localiza la fuente para mkvmerge:
         - iso/bdmv_folder → MPLS principal o el del episodio (modo serie)
         - m2ts → el fichero m2ts directamente
      3. Decide ruta según reordenación:
         a) Con reordenación → mkvmerge → MKV final (1 copia, sin intermedio)
         b) Sin reordenación → mkvmerge → intermedio → mkvpropedit + mv (1 copia)
      4. Limpia el origen (ISO desmontado, etc.) en finally.
    """
    session = load_session(session_id)
    if not session:
        return

    # Marcar como ejecutando
    session.status              = "running"
    session.output_log          = []
    session.error_message       = None
    session.execution_started_at = datetime.now(timezone.utc)
    session.output_mkv_path     = None
    save_session(session)

    # Tracking de tiempos por fase
    _phase_starts: dict[str, datetime] = {}
    _phase_ends:   dict[str, datetime] = {}

    def _mark_phase(phase: str, done: bool = False) -> None:
        now = datetime.now(timezone.utc)
        if not done:
            _phase_starts[phase] = now
        else:
            _phase_ends[phase] = now

    async def log(msg: str) -> None:
        if not msg.startswith("Progress:"):
            ts = datetime.now().strftime("%H:%M:%S")  # hora local (TZ del contenedor)
            msg = f"[{ts}] {msg}"
        session.output_log.append(msg)
        # Persist NON-BLOCKING con throttle + lock — mismo patrón que
        # _cmv40_maybe_persist_log para evitar:
        #   1. Bloqueo del event loop por model_dump_json (movido a thread).
        #   2. Saves concurrentes que corromperían el JSON (lock serializa).
        #   3. Demasiados writes al disco con NAS lento (throttle 1s/20 líneas).
        await _maybe_save_session_throttled(session)
        # Fire-and-forget broadcast con timeout corto — un cliente zombie
        # NO debe bloquear este log loop (mismo razonamiento que en Tab 3
        # _cmv40_log: TCP send buffer lleno bloquea minutos hasta timeout
        # del kernel, congelando el event loop).
        for ws in list(_ws_connections.get(session_id, [])):
            asyncio.create_task(_send_ws_with_timeout(_ws_connections, session_id, ws, msg))

    # Tracking del origen abierto. `source_obj` se setea tras Source.open()
    # + __aenter__ y se cierra en finally. `mount_point` se mantiene como
    # alias para mantener compatibilidad con logs/checks que aún lo usan.
    source_obj: "Source | None" = None
    mount_point      = None
    intermediate_mkv = None
    _cancel_flags[session_id] = False

    def _check_cancel():
        """Lanza RuntimeError si la sesión fue cancelada."""
        if _cancel_flags.get(session_id):
            raise RuntimeError("Cancelado por el usuario")

    def _register_proc(proc):
        """Registra el subprocess activo para poder matarlo desde el endpoint cancel."""
        _active_processes[session_id] = proc

    try:
        from phases.iso_mount import Source, SourceError

        stype = session.source_type or "iso"
        source_basename = Path(session.iso_path).name
        await log(f"[Pipeline] ━━━ Iniciando: {source_basename} ━━━")

        # Plan general dinámico según el tipo de origen. Describe SOLO los
        # pasos que se van a ejecutar — los específicos (ruta directa /
        # intermedio, capítulos auto) se anuncian en sus propios markers
        # cuando llegan, no aquí.
        if stype == "iso":
            plan_text = (
                "[Pipeline] 📋 Plan: montar el ISO, localizar el playlist principal "
                "del Blu-ray, extraer las pistas elegidas a un MKV con sus metadatos "
                "(nombres, flags, capítulos), validar el resultado y desmontar."
            )
        elif stype == "bdmv_folder":
            plan_text = (
                "[Pipeline] 📋 Plan: leer la carpeta BDMV, localizar el playlist "
                "principal, extraer las pistas elegidas a un MKV con sus metadatos "
                "(nombres, flags, capítulos) y validar el resultado."
            )
        else:  # m2ts
            plan_text = (
                "[Pipeline] 📋 Plan: leer el fichero M2TS, extraer las pistas elegidas "
                "a un MKV con sus metadatos. El M2TS no contiene marcas de capítulo, "
                "así que se generan automáticamente cada 10 minutos."
            )
        await log(plan_text)

        # ── 1. Preparar origen (Source abstraction) ───────────────
        _mark_phase("mount")
        if stype == "iso":
            await log("[Origen] ┌─ Paso 1: Montando el ISO en /mnt/bd…")
        elif stype == "bdmv_folder":
            await log("[Origen] ┌─ Paso 1: Origen directo — leyendo la carpeta BDMV")
        else:  # m2ts
            await log("[Origen] ┌─ Paso 1: Origen directo — leyendo el fichero M2TS")

        source_obj = await Source.open(session.iso_path)
        await source_obj.__aenter__()
        # Alias para que código posterior que lee `mount_point` siga
        # funcionando (típicamente referencias al bdmv_root del montaje).
        mount_point = source_obj.bdmv_root  # None para m2ts
        _mark_phase("mount", done=True)

        if stype == "iso":
            await log(f"[Origen] └─ ✓ ISO montado en: {mount_point}")
        elif stype == "bdmv_folder":
            await log(f"[Origen] └─ ✓ Carpeta lista: {mount_point}")
        else:
            await log(f"[Origen] └─ ✓ Fichero listo: {session.iso_path}")

        _check_cancel()

        # ── 2. Localizar la fuente para mkvmerge ──────────────────
        # iso/bdmv_folder → MPLS (principal o del episodio en serie)
        # m2ts → el propio fichero
        if stype == "m2ts":
            # session.iso_path apunta al m2ts directo. Para serie
            # multi-fichero, session.mpls_path tiene el path absoluto
            # del m2ts del episodio (lo guardamos así en create_series).
            mkvmerge_source = (
                session.mpls_path
                if session.mpls_path and Path(session.mpls_path).exists()
                else session.iso_path
            )
            await log(f"[Origen] Fichero de origen: {Path(mkvmerge_source).name}")
        else:
            # Buscar el MPLS dentro del bdmv_root. Prioridades:
            #   1. session.mpls_path (modo serie — nombre del MPLS específico)
            #   2. session.bdinfo_result.main_mpls (modo película — detectado en Fase A)
            #   3. find_main_mpls (fallback general)
            mkvmerge_source = None
            preferred_mpls_name = None
            if session.mpls_path:
                preferred_mpls_name = Path(session.mpls_path).name
            elif session.bdinfo_result and session.bdinfo_result.main_mpls:
                preferred_mpls_name = session.bdinfo_result.main_mpls

            if preferred_mpls_name:
                for candidate_dir in [
                    Path(mount_point) / "BDMV" / "PLAYLIST",
                    Path(mount_point) / "PLAYLIST",
                ]:
                    candidate_path = candidate_dir / preferred_mpls_name
                    if candidate_path.exists():
                        mkvmerge_source = str(candidate_path)
                        break
            if not mkvmerge_source:
                mkvmerge_source = find_main_mpls(mount_point)
            await log(f"[Origen] Playlist principal: {Path(mkvmerge_source).name}")

        # Alias mpls_path para no romper código posterior — semánticamente
        # ahora puede ser MPLS o m2ts según el tipo de origen.
        mpls_path = mkvmerge_source

        _check_cancel()

        # ── 3. Decidir ruta ───────────────────────────────────────
        do_reorder = await needs_reordering(session, mpls_path, log)

        if do_reorder:
            # ── RUTA DIRECTA: source → MKV final (1 sola copia) ──
            await log(
                "[Pipeline] 🎯 Ruta directa: hay pistas reordenadas o excluidas, "
                "así que un solo mkvmerge hace selección + reorganización + "
                "metadatos + capítulos en una pasada (ahorra una copia)."
            )
            _mark_phase("extract")
            final_mkv = await run_phase_e_direct(
                session=session,
                mpls_path=mpls_path,
                log_callback=log,
                proc_callback=_register_proc,
            )
            _mark_phase("extract", done=True)

        else:
            # ── RUTA INTERMEDIO: source → intermedio → mkvpropedit ─
            await log(
                "[Pipeline] 🎯 Ruta con intermedio: no hay reordenación de pistas, "
                "así que es más rápido copiar una vez al intermedio (mkvmerge) y "
                "aplicar después los metadatos sobre las cabeceras (mkvpropedit), "
                "sin volver a copiar el contenido."
            )

            # Phase D: extraer todo al intermedio. Para m2ts pasamos
            # source_path explícito (no hay MPLS que buscar); para
            # iso/bdmv pasamos share_path y el helper busca el MPLS.
            _mark_phase("extract")
            intermediate_mkv = await run_phase_d(
                share_path=mount_point or "",
                tmp_dir=TMP_DIR,
                log_callback=log,
                proc_callback=_register_proc,
                source_path=mpls_path if stype == "m2ts" else None,
            )
            _mark_phase("extract", done=True)
            await log(f"[Fase D] Intermedio listo en: {intermediate_mkv}")

            # Phase E: mkvpropedit in-place + mv
            _mark_phase("write")
            _check_cancel()
            final_mkv = await run_phase_e_propedit(
                session=session,
                intermediate_mkv=intermediate_mkv,
                log_callback=log,
                proc_callback=_register_proc,
            )
            _mark_phase("write", done=True)

        session.output_mkv_path = final_mkv

        # ── Validación final del MKV ─────────────────────────────
        validation_ok = await _validate_final_mkv(session, final_mkv, log)

        session.status         = "done" if validation_ok else "done"
        session.last_executed  = datetime.now(timezone.utc)

        if validation_ok:
            await log(f"[Pipeline] ✓ Listo: {final_mkv}")
            await log(
                "[Pipeline] 🎯 Resultado: MKV disponible en /mnt/output. Pistas, "
                "idiomas, flags y capítulos verificados contra lo configurado en "
                "la sesión."
            )
        else:
            await log(f"[Pipeline] ⚠ Completado con avisos: {final_mkv}")
            await log(
                "[Pipeline] 🎯 Resultado: MKV escrito en /mnt/output pero con "
                "discrepancias en la verificación. Revisa los avisos marcados con "
                "⚠️ o ❌ arriba para ver qué campos no cuadran."
            )

    except Exception as e:
        cancelled = _cancel_flags.get(session_id, False)
        if cancelled:
            session.status        = "pending"
            session.error_message = None
            await log("[Pipeline] 🛑 Cancelado por el usuario")
        else:
            session.status        = "error"
            session.error_message = str(e)
            await log(f"[Pipeline] ✗ Error: {e}")

        # Limpiar ficheros temporales/parciales
        for path in [intermediate_mkv, session.output_mkv_path]:
            if path and Path(path).exists():
                try:
                    Path(path).unlink()
                    await log(f"[Pipeline] 🧹 Temporal eliminado: {Path(path).name}")
                except OSError:
                    pass
        session.output_mkv_path = None

    finally:
        # Cierre del origen (siempre — éxito, error o cancelación). Para
        # ISO ejecuta el unmount; para bdmv_folder y m2ts es no-op pero
        # lo invocamos para mantener el contrato del context manager.
        if source_obj is not None:
            _mark_phase("unmount")
            try:
                await source_obj.__aexit__(None, None, None)
            except Exception as _cleanup_err:
                _logger.warning("Error cerrando Source: %s", _cleanup_err)
            _mark_phase("unmount", done=True)
            stype_cleanup = session.source_type or "iso"
            if stype_cleanup == "iso":
                await log("[Origen] ✓ ISO desmontado")
            elif stype_cleanup == "bdmv_folder":
                await log("[Origen] ✓ Origen cerrado (carpeta BDMV)")
            else:
                await log("[Origen] ✓ Origen cerrado (fichero M2TS)")

        # Limpiar tracking de cancelación
        _cancel_flags.pop(session_id, None)
        _active_processes.pop(session_id, None)

        # Registrar ejecución en historial (no registrar cancelaciones)
        if session.status != "pending":
            _append_execution_record(session, _phase_starts, _phase_ends)
        # Flush garantizado del log antes de marcar terminada la sesión:
        # cualquier línea que el throttle hubiera dejado en buffer se
        # vuelca a disco AHORA. Sin esto, las últimas N líneas del log
        # podrían perderse si el server cae justo aquí.
        await _flush_session_save(session)
        sig = "__DONE__" if session.status == "done" else "__CANCELLED__" if session.status == "pending" else "__ERROR__"
        # Fire-and-forget con timeout — los señalizadores terminales son
        # importantes pero no debemos bloquear si un cliente está zombie.
        for ws in list(_ws_connections.get(session_id, [])):
            asyncio.create_task(_send_ws_with_timeout(_ws_connections, session_id, ws, sig))


async def _validate_final_mkv(session: Session, mkv_path: str, log) -> bool:
    """
    Valida el MKV final contra lo esperado por la sesión.

    Comprueba: existencia del fichero, número y tipo de pistas,
    coincidencia de idiomas de audio y subtítulos, presencia de capítulos.
    Escribe un informe detallado en el log para diagnóstico.

    Returns:
        True si todo es correcto, False si hay discrepancias.
    """
    await log("[Validación] 📋 Verificando el MKV final contra lo configurado en la sesión…")

    if not Path(mkv_path).exists():
        await log("[Validación] ❌ El fichero MKV no existe")
        return False

    # ── Leer pistas del MKV final con mkvmerge -J ────────────────
    proc = await asyncio.create_subprocess_exec(
        "mkvmerge", "-J", mkv_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode >= 2:
        await log(f"[Validación] ❌ mkvmerge no pudo leer el MKV (código {proc.returncode})")
        return False

    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        await log("[Validación] ❌ mkvmerge devolvió un JSON inválido")
        return False

    actual_tracks = data.get("tracks", [])
    actual_chapters = data.get("chapters", [])
    file_size = Path(mkv_path).stat().st_size

    # ── Clasificar pistas del MKV ────────────────────────────────
    actual_video = [t for t in actual_tracks if t.get("type") == "video"]
    actual_audio = [t for t in actual_tracks if t.get("type") == "audio"]
    actual_subs  = [t for t in actual_tracks if t.get("type") == "subtitles"]

    # ── Pistas esperadas de la sesión ────────────────────────────
    expected_audio = [t for t in session.included_tracks if t.track_type == "audio"]
    expected_subs  = [t for t in session.included_tracks if t.track_type == "subtitle"]

    all_ok = True
    warnings = []

    # ── Log de información general ───────────────────────────────
    await log(f"[Validación] 📁 {Path(mkv_path).name} ({file_size / 1e9:.2f} GB)")
    await log(f"[Validación] 🎞️ Pistas: {len(actual_video)} vídeo · {len(actual_audio)} audio · {len(actual_subs)} subtítulos")

    # ── Validar vídeo ────────────────────────────────────────────
    if not actual_video:
        await log("[Validación] ❌ El MKV no tiene pistas de vídeo")
        all_ok = False
    else:
        for v in actual_video:
            codec = v.get("codec", "?")
            dims = v.get("properties", {}).get("pixel_dimensions", "?")
            await log(f"[Validación]   🎬 Vídeo: {codec} · {dims}")

    # Verificar Dolby Vision si se esperaba FEL
    # Con mkvmerge v81+, BL+EL se combinan en un solo track (no hay EL separada).
    # La señalización DV se verifica por el DOVI configuration record en codec private.
    if session.has_fel:
        if len(actual_video) == 1:
            # v81+: BL+EL combinados — comportamiento correcto
            await log("[Validación]   ✅ Dolby Vision FEL: base + enhancement combinados en una sola pista")
        elif any("1920" in v.get("properties", {}).get("pixel_dimensions", "") for v in actual_video):
            # v65 legacy: EL como track separado — DV puede no funcionar
            await log("[Validación]   ⚠️ Dolby Vision FEL: enhancement layer en pista separada (requiere mkvmerge v81+ para DV correcto)")
        else:
            msg = "❌ Dolby Vision FEL esperado pero no se ha encontrado el enhancement layer"
            await log(f"[Validación] {msg}")
            warnings.append(msg)
            all_ok = False

    # ── Validar audio ────────────────────────────────────────────
    if len(actual_audio) != len(expected_audio):
        msg = f"❌ Audio: {len(actual_audio)} pistas en el MKV vs {len(expected_audio)} esperadas en la sesión"
        await log(f"[Validación] {msg}")
        warnings.append(msg)
        all_ok = False

    # Mapeo ISO 639-2 → nombre inglés lowered para comparación
    _iso = {
        "spa": "spanish", "eng": "english", "fre": "french", "fra": "french",
        "ger": "german", "deu": "german", "ita": "italian", "jpn": "japanese",
        "por": "portuguese", "dut": "dutch", "nld": "dutch", "rus": "russian",
    }

    for i, at in enumerate(actual_audio):
        props = at.get("properties", {})
        lang_iso = props.get("language", "und")
        lang_name = _iso.get(lang_iso, lang_iso)
        codec = at.get("codec", "?")
        name = props.get("track_name", "")
        is_default = props.get("default_track", False)

        status = "✅"
        detail = ""
        if i < len(expected_audio):
            exp = expected_audio[i]
            exp_lang = exp.raw.language.lower()
            if lang_name != exp_lang:
                status = "❌"
                detail = f" (esperado: {exp_lang}, real: {lang_name})"
                all_ok = False
        else:
            status = "⚠️"
            detail = " (pista extra no esperada)"

        flag_str = " [DEFAULT]" if is_default else ""
        await log(f"[Validación]   🔊 Audio #{i+1}: {codec} · {lang_iso}{flag_str} · \"{name}\"{detail} {status}")

    # Pistas esperadas que no están en el MKV
    for i in range(len(actual_audio), len(expected_audio)):
        exp = expected_audio[i]
        msg = f"❌ Falta la pista de audio #{i+1} esperada: {exp.raw.language} {exp.raw.codec}"
        await log(f"[Validación]   {msg}")
        warnings.append(msg)
        all_ok = False

    # ── Validar subtítulos ───────────────────────────────────────
    if len(actual_subs) != len(expected_subs):
        msg = f"❌ Subtítulos: {len(actual_subs)} pistas en el MKV vs {len(expected_subs)} esperadas en la sesión"
        await log(f"[Validación] {msg}")
        warnings.append(msg)
        all_ok = False

    for i, st in enumerate(actual_subs):
        props = st.get("properties", {})
        lang_iso = props.get("language", "und")
        lang_name = _iso.get(lang_iso, lang_iso)
        name = props.get("track_name", "")
        is_default = props.get("default_track", False)
        is_forced = props.get("forced_track", False)

        status = "✅"
        detail = ""
        if i < len(expected_subs):
            exp = expected_subs[i]
            exp_lang = exp.raw.language.lower()
            if lang_name != exp_lang:
                status = "❌"
                detail = f" (esperado: {exp_lang}, real: {lang_name})"
                all_ok = False
        else:
            status = "⚠️"
            detail = " (pista extra)"

        flags = []
        if is_default: flags.append("DEF")
        if is_forced: flags.append("FRC")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        await log(f"[Validación]   💬 Sub #{i+1}: {lang_iso}{flag_str} · \"{name}\"{detail} {status}")

    for i in range(len(actual_subs), len(expected_subs)):
        exp = expected_subs[i]
        msg = f"❌ Falta el subtítulo #{i+1} esperado: {exp.raw.language} {exp.subtitle_type}"
        await log(f"[Validación]   {msg}")
        warnings.append(msg)
        all_ok = False

    # ── Validar capítulos (extracción real, no num_entries) ─────
    import subprocess as _sp
    try:
        _ch_result = _sp.run(
            ["mkvextract", mkv_path, "chapters", "--simple"],
            capture_output=True, text=True, timeout=10,
        )
        num_chapters = sum(1 for l in _ch_result.stdout.splitlines() if l.startswith("CHAPTER") and "NAME" not in l)
    except Exception:
        num_chapters = 0
    expected_chapters = len(session.chapters)
    if num_chapters != expected_chapters and expected_chapters > 0:
        msg = f"⚠️ Capítulos: {num_chapters} en el MKV vs {expected_chapters} esperados en la sesión"
        await log(f"[Validación] {msg}")
        warnings.append(msg)
    else:
        await log(f"[Validación]   📖 Capítulos: {num_chapters}")

    # ── Resumen ──────────────────────────────────────────────────
    if all_ok:
        await log("[Validación] ✅ Verificación correcta — el MKV coincide con lo configurado en la sesión")
    else:
        await log(f"[Validación] ⚠️ Verificación con {len(warnings)} discrepancia{'s' if len(warnings) != 1 else ''} — revisa las líneas con ❌ o ⚠️ arriba")
        await log("[Validación] ── Datos para diagnóstico ──")
        await log(f"[Validación] Sesión ID: {session.id}")
        await log(f"[Validación] Origen: {session.iso_path}")
        await log(f"[Validación] MKV final: {mkv_path}")
        await log(f"[Validación] Pistas configuradas en la sesión: {len(expected_audio)} audio + {len(expected_subs)} subtítulos")
        for i, t in enumerate(expected_audio):
            await log(f"[Validación]   Audio esperado #{i+1}: {t.raw.language} · {t.raw.codec} · etiqueta=\"{t.label}\"")
        for i, t in enumerate(expected_subs):
            await log(f"[Validación]   Subtítulo esperado #{i+1}: {t.raw.language} · {t.subtitle_type} · etiqueta=\"{t.label}\"")
        await log(f"[Validación] Pistas reales en el MKV: {len(actual_audio)} audio + {len(actual_subs)} subtítulos")
        for at in actual_audio:
            p = at.get("properties", {})
            await log(f"[Validación]   Audio real: id={at['id']} · {at['codec']} · {p.get('language','')} · \"{p.get('track_name','')}\"")
        for st in actual_subs:
            p = st.get("properties", {})
            flags = []
            if p.get('default_track'): flags.append('default')
            if p.get('forced_track'): flags.append('forzado')
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            await log(f"[Validación]   Subtítulo real: id={st['id']} · {p.get('language','')}{flag_str} · \"{p.get('track_name','')}\"")
        await log("[Validación] ── Fin del diagnóstico ──")

    return all_ok


def _append_execution_record(
    session: Session,
    phase_starts: dict[str, datetime],
    phase_ends: dict[str, datetime],
) -> None:
    """Construye un ExecutionRecord y lo añade al historial de la sesión."""
    now = datetime.now(timezone.utc)
    phase_elapsed: dict[str, float | None] = {}
    for phase in ("mount", "extract", "unmount", "write"):
        start = phase_starts.get(phase)
        end   = phase_ends.get(phase)
        if start and end:
            phase_elapsed[phase] = round((end - start).total_seconds(), 1)
        elif start:
            # Fase iniciada pero no completada (error durante la fase)
            phase_elapsed[phase] = round((now - start).total_seconds(), 1)
        else:
            phase_elapsed[phase] = None

    record = ExecutionRecord(
        run_number      = len(session.execution_history) + 1,
        started_at      = session.execution_started_at or now,
        finished_at     = now,
        status          = session.status,
        error_message   = session.error_message,
        output_mkv_path = session.output_mkv_path,
        phase_elapsed   = phase_elapsed,
        output_log      = list(session.output_log),
    )
    session.execution_history.append(record)


# ── Cola de ejecución ────────────────────────────────────────────────────────

@app.get("/api/queue", summary="Estado de la cola de ejecución")
async def get_queue():
    """Devuelve el estado de la cola con objetos de sesión completos."""
    status = queue_manager.get_status()
    result: dict = {"running": None, "queue": []}

    if status["running"]:
        s = load_session(status["running"])
        if s:
            result["running"] = s.model_dump()

    for sid in status["queue"]:
        s = load_session(sid)
        if s:
            result["queue"].append(s.model_dump())

    return result


@app.delete("/api/queue/{session_id}", summary="Cancela un trabajo encolado")
async def cancel_queue_job(session_id: str):
    """Elimina session_id de la cola si aún no ha empezado a ejecutarse."""
    cancelled = await queue_manager.cancel(session_id)
    if cancelled:
        session = load_session(session_id)
        if session:
            session.status = "pending"
            save_session(session)
    return {"ok": cancelled, "session_id": session_id}


@app.post(
    "/api/sessions/{session_id}/cancel",
    summary="Cancela la ejecución en curso de una sesión",
)
async def cancel_running_session(session_id: str):
    """
    Cancela la ejecución activa de una sesión. Mata el subprocess en curso
    (mkvmerge, mkvpropedit, etc.) y señaliza al pipeline para que haga
    limpieza (desmontar ISO, eliminar temporales).

    Solo funciona si la sesión está en estado 'running'.
    Si está 'queued', usar DELETE /api/queue/{id} en su lugar.
    """
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    if session.status != "running":
        raise HTTPException(status_code=400, detail=f"Sesión no está en ejecución (status={session.status})")

    # Señalizar cancelación
    _cancel_flags[session_id] = True

    # Matar el subprocess activo si existe
    proc = _active_processes.get(session_id)
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    return {"ok": True, "session_id": session_id}


@app.post("/api/queue/reorder", summary="Reordena la cola de ejecución")
async def reorder_queue(body: QueueReorderRequest):
    """Reordena la cola según la lista ordered_ids proporcionada."""
    await queue_manager.reorder(body.ordered_ids)
    return queue_manager.get_status()


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 2 — EDITAR MKV (endpoints stateless)
# ══════════════════════════════════════════════════════════════════════════════

from models import MkvEditRequest
from phases.mkv_analyze import analyze_mkv, apply_mkv_edits

OUTPUT_DIR_MKV = Path(os.environ.get("OUTPUT_DIR", "/mnt/output"))
LIBRARY_DIR    = Path(os.environ.get("LIBRARY_DIR", "/mnt/library"))


# Roots disponibles para el file browser (Tab 2 y Tab 3).
# Cada key apunta a una Path mountada en el contenedor. El frontend pide
# `?root=library`, `?root=output` o `?root=downloaded` para conmutar entre
# arboles. Tab 2 expone library + output; Tab 3 expone library + downloaded
# (carpeta de descargas, que contiene ISOs y tambien MKVs sueltos).
# Solo paths bajo estos roots son aceptados por endpoints downstream
# (analyze, light-profile) — proteccion contra path traversal arbitrario.
LIBRARY_ROOTS: dict[str, Path] = {
    "library":    LIBRARY_DIR,
    "output":     OUTPUT_DIR_MKV,
    "downloaded": ISOS_DIR,
}


# Directorios "no peliculas" que NUNCA queremos exponer en el browser:
#   - .zfs/snapshot — ZFS snapshots ocultos en QuTS hero (recursivos eternos)
#   - @eaDir, .DS_Store, Thumbs.db — metadata de Synology/macOS/Windows
#   - .Recycle, #recycle, $RECYCLE.BIN — papeleras varias
#   - dotfiles — ocultos en general
_LIBRARY_HIDDEN_DIRS = {
    ".zfs", "@eaDir", ".DS_Store", ".Recycle", "#recycle",
    "$RECYCLE.BIN", "@Recycle", ".Trash", "lost+found",
}

def _safe_library_path(rel_path: str, root_key: str = "library") -> tuple[Path, Path]:
    """Resuelve `rel_path` relativo al root indicado validando que no escape.
    Lanza HTTPException 400 si root es desconocido o si la path escapa.
    Devuelve (path_absoluta_resuelta, path_base_resuelta).
    """
    base = LIBRARY_ROOTS.get(root_key)
    if base is None:
        raise HTTPException(status_code=400, detail=f"Root desconocido: {root_key}")
    rel = (rel_path or "").strip().lstrip("/")
    candidate = (base / rel).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="Ruta fuera del root permitido")
    return candidate, base_resolved


def _resolve_mkv_path_safe(input_path: str) -> Path:
    """Resuelve una ruta de MKV (absoluta o relativa) validando que cae bajo
    un root permitido. Usado por endpoints que reciben file_path desde el
    frontend (analyze, light-profile, etc.) — antes asumian /mnt/output como
    prefijo, ahora aceptan cualquier root para soportar el browser de Library.
    """
    p = (input_path or "").strip()
    if not p:
        raise HTTPException(status_code=400, detail="file_path vacío")
    candidate = Path(p) if p.startswith("/") else (OUTPUT_DIR_MKV / p)
    candidate = candidate.resolve()
    # Acepta si cae bajo CUALQUIER root configurado
    for root in LIBRARY_ROOTS.values():
        try:
            candidate.relative_to(root.resolve())
            return candidate
        except ValueError:
            continue
    raise HTTPException(
        status_code=400,
        detail=f"Ruta fuera de los directorios permitidos: {p}"
    )


@app.get("/api/library/browse", summary="Navega árboles de MKVs/ISOs/M2TS/BDMV")
async def library_browse(
    root: str = "library",
    path: str = "",
    filter: str = "mkv",
):
    """Lista subdirectorios + ficheros bajo LIBRARY_ROOTS[root]/<path>
    filtrando por extensión según `filter`.

    Roots soportados:
      - "library": /mnt/library
      - "output":  /mnt/output
      - "downloaded": /mnt/isos (también usado por Tab 1 como "sources")

    Filtros (qué ficheros listar — las carpetas siempre se muestran para
    permitir navegación):
      - "mkv":   ficheros .mkv (default — compat Tab 2/3)
      - "iso":   ficheros .iso
      - "m2ts":  ficheros .m2ts
      - "bdmv":  ninguno (solo carpetas; las que tengan BDMV/PLAYLIST/
                 dentro se marcan con is_bdmv=True)

    Entries devueltas con campos extra:
      - is_bdmv: true si la carpeta contiene BDMV/PLAYLIST/ dentro
        (el frontend la pinta como seleccionable directamente sin navegar).
    """
    if DEV_MODE:
        # Fixtures distintas según el root seleccionado
        if root == "output":
            return {
                "root": root, "path": path, "parent": "" if path else None,
                "base": "/mnt/output",
                "entries": [
                    {"name": "Movie A [DV FEL CMv4.0].mkv", "type": "file", "size_bytes": 78_000_000_000},
                    {"name": "Movie B [DV FEL CMv4.0].mkv", "type": "file", "size_bytes": 65_000_000_000},
                ],
            }
        return {
            "root": root, "path": path, "parent": "" if path else None,
            "base": "/mnt/library",
            "entries": [
                {"name": "Action", "type": "dir", "size_bytes": 0},
                {"name": "Drama", "type": "dir", "size_bytes": 0},
                {"name": "Movie 1 (2024) [DV FEL].mkv", "type": "file", "size_bytes": 52_000_000_000},
                {"name": "Movie 2 (2023) [DV FEL].mkv", "type": "file", "size_bytes": 48_000_000_000},
            ],
        }

    base_dir = LIBRARY_ROOTS.get(root)
    if base_dir is None:
        raise HTTPException(status_code=400, detail=f"Root desconocido: {root}")
    if not base_dir.exists() or not base_dir.is_dir():
        return {"root": root, "path": path, "parent": None, "base": str(base_dir),
                "entries": [], "error": f"Root '{root}' no configurado o inaccesible"}

    target, base_resolved = _safe_library_path(path, root_key=root)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"Directorio no encontrado: {path}")

    # Validar filter
    valid_filters = {"mkv", "iso", "m2ts", "bdmv"}
    if filter not in valid_filters:
        raise HTTPException(status_code=400, detail=f"filter desconocido: {filter}")

    # Mapa filter → extensión a listar (None = solo carpetas)
    file_ext = {
        "mkv": ".mkv",
        "iso": ".iso",
        "m2ts": ".m2ts",
        "bdmv": None,
    }[filter]

    entries: list[dict] = []
    try:
        for child in target.iterdir():
            name = child.name
            # Skip ocultos y especiales
            if name.startswith(".") and name not in {".", ".."}:
                if name == ".zfs" or name in _LIBRARY_HIDDEN_DIRS:
                    continue
                # Otros dotfiles también ocultos
                continue
            if name in _LIBRARY_HIDDEN_DIRS:
                continue
            if child.is_dir():
                # En modo BDMV no exponemos la propia carpeta "BDMV" como
                # navegable (no aporta — el usuario selecciona la padre).
                if filter == "bdmv" and name == "BDMV":
                    continue
                # Detecta si esta carpeta es un BDMV root (tiene BDMV/PLAYLIST/
                # dentro). Solo computamos para los filtros que lo necesitan
                # para no penalizar perf en navegación normal.
                is_bdmv = False
                if filter == "bdmv":
                    try:
                        is_bdmv = (
                            (child / "BDMV" / "PLAYLIST").exists() or
                            (child / "PLAYLIST").exists()
                        )
                    except OSError:
                        is_bdmv = False
                entries.append({
                    "name": name,
                    "type": "dir",
                    "size_bytes": 0,
                    "is_bdmv": is_bdmv,
                })
            elif file_ext and child.is_file() and name.lower().endswith(file_ext):
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                entries.append({"name": name, "type": "file", "size_bytes": size})
    except PermissionError:
        raise HTTPException(status_code=403, detail="Sin permisos para listar este directorio")

    # Sort: dirs primero, luego files; todo case-insensitive alfabético
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))

    rel_current = str(target.relative_to(base_resolved)) if target != base_resolved else ""
    parent = None
    if rel_current:
        parent_path = "/".join(rel_current.split("/")[:-1])
        parent = parent_path

    return {
        "root": root,
        "path": rel_current,
        "parent": parent,
        "base": str(base_dir),
        "entries": entries,
    }


@app.get("/api/mkv/files", summary="Lista MKVs disponibles en /mnt/output")
async def list_mkv_files():
    """Devuelve la lista de ficheros .mkv en el directorio de salida."""
    # ⚠️ DEV MODE — branch que devuelve fixtures sin tocar el filesystem
    if DEV_MODE:
        return {"files": DEV_FAKE_MKV_FILES}
    if not OUTPUT_DIR_MKV.exists():
        return {"files": []}
    files = sorted(
        [p.name for p in OUTPUT_DIR_MKV.glob("*.mkv")],
        key=str.lower,
    )
    return {"files": files}


@app.get("/api/mkv/files-in-isos", summary="Lista MKVs en el directorio de ISOs (para Tab 3 Fase B)")
async def list_mkv_files_in_isos():
    """Devuelve .mkv presentes en ISOS_DIR con su ruta absoluta.

    Búsqueda NO recursiva — solo ficheros directos en ISOS_DIR. Evita recursar
    en snapshots ZFS ocultos (`.zfs/snapshot/…`) de QNAP QuTS, que devolverían
    cientos de ficheros históricos con el mismo nombre.
    """
    if DEV_MODE:
        return {"files": [{"name": f, "path": f"/mnt/isos/{f}"} for f in DEV_FAKE_MKV_FILES]}
    if not ISOS_DIR.exists():
        return {"files": []}
    files = sorted(
        [{"name": p.name, "path": str(p)} for p in ISOS_DIR.glob("*.mkv") if p.is_file()],
        key=lambda x: x["name"].lower(),
    )
    return {"files": files}


@app.post("/api/mkv/analyze", summary="Analiza un MKV existente")
async def analyze_mkv_endpoint(body: dict):
    """
    Ejecuta mkvmerge -J + MediaInfo + ffprobe (packet counts) + dovi_tool
    sobre un MKV y devuelve toda la información de pistas, capítulos y
    metadatos. Puede tardar 1-3 min en MKVs grandes.

    Body: ``{"file_path": "Movie.mkv", "force_refresh": false}``.

    El primer análisis persiste el resultado en /config/mkv_audits/. Re-abrir
    el mismo MKV es instantáneo (cache HIT). ``force_refresh: true`` invalida
    el cache (botón "↻ Re-analizar" del frontend) y rehace el pipeline.

    Durante la ejecución emite progreso en ``_analyze_progress`` (mismo
    endpoint ``/api/analyze/progress`` que usa Tab 1).
    """
    global _analyze_progress
    rel_path = body.get("file_path", "")
    force_refresh = bool(body.get("force_refresh", False))
    # ⚠️ DEV MODE — branch que devuelve fixtures sin tocar el filesystem
    if DEV_MODE:
        # Simulación de progreso para que el modal se vea en dev — incluye
        # barra PGS animada del 0 al 100% para probar la UX.
        import asyncio as _aio
        _analyze_progress = {"step": "identify", "done": False}
        await _aio.sleep(0.4)
        _analyze_progress = {"step": "mediainfo", "done": False}
        await _aio.sleep(0.6)
        for pct in (5, 20, 45, 70, 90, 100):
            eta = max(0, int((100 - pct) * 0.04))
            _analyze_progress = {"step": "pgs", "done": False, "pct": pct, "eta_s": eta}
            await _aio.sleep(0.35)
        _analyze_progress = {"step": "dovi", "done": False}
        await _aio.sleep(0.5)
        _analyze_progress = {"step": "", "done": True}
        return build_fake_mkv_analysis(rel_path)

    # Acepta tanto rutas absolutas (file browser nuevo de Tab 2/3) como
    # filenames relativos a /mnt/output (compat legacy). Valida que la ruta
    # cae bajo un root permitido (LIBRARY_ROOTS).
    mkv_path_obj = _resolve_mkv_path_safe(rel_path)
    mkv_full = str(mkv_path_obj)
    if not mkv_path_obj.exists():
        raise HTTPException(status_code=400, detail=f"MKV no encontrado: {rel_path}")

    # Force refresh: borra el cache antes de delegar para que analyze_mkv
    # caiga al pipeline completo.
    if force_refresh:
        try:
            from storage import invalidate_mkv_cache_by_path
            invalidate_mkv_cache_by_path(mkv_full)
            _logger.info("MKV cache invalidado por force_refresh: %s", mkv_path_obj.name)
        except Exception as e:
            _logger.warning("invalidate_mkv_cache_by_path falló (no bloquea): %s", e)

    # Captura el log emitido durante el análisis para guardarlo en el
    # resultado. Sirve para diagnóstico desde el modal "Datos MKV" sin pedir
    # el log del container — paridad con Tab 1's Session.analysis_log.
    analysis_log: list[str] = []
    _MKV_STEP_LABELS = {
        "identify": "Identificando pistas con mkvmerge -J",
        "mediainfo": "Analizando metadata extendida con MediaInfo",
        "pgs": "Contando paquetes PGS por subtítulo (ffprobe)",
        "dovi": "Analizando Dolby Vision (dovi_tool)",
        "cache_hit": "Resultado servido desde caché (sin re-analizar)",
    }
    cache_was_hit = False

    async def _mkv_progress_callback(step: str):
        global _analyze_progress
        nonlocal cache_was_hit
        try:
            from datetime import datetime as _dt
            ts = _dt.now().strftime("%H:%M:%S")
            label = _MKV_STEP_LABELS.get(step, step)
            analysis_log.append(f"[{ts}] [Análisis MKV] {label}…")
        except Exception:
            analysis_log.append(step)
        if step == "cache_hit":
            cache_was_hit = True
            _analyze_progress = {"step": "", "done": True}
            return
        if step == "pgs":
            _analyze_progress = {"step": "pgs", "done": False, "pct": 0, "eta_s": 0}
        else:
            _analyze_progress = {"step": step, "done": False}

    async def _mkv_pgs_progress_callback(pct: float, eta_s: int):
        global _analyze_progress
        _analyze_progress = {"step": "pgs", "done": False, "pct": round(pct, 1), "eta_s": eta_s}

    _analyze_progress = {"step": "identify", "done": False}
    try:
        result = await analyze_mkv(
            mkv_full,
            progress_callback=_mkv_progress_callback,
            pgs_progress_callback=_mkv_pgs_progress_callback,
        )
        # Si el cache HIT, el analysis_log devuelto viene del cache antiguo
        # (con sus timestamps originales). NO lo sobrescribimos — preserva
        # el contexto temporal del análisis original. Solo añadimos al log
        # cuando el pipeline corrió de verdad.
        if not cache_was_hit:
            result.analysis_log = analysis_log
            # Persistir tras pipeline fresh. Encapsulado en mkv_analyze
            # para mantener la lógica de exclude(mediainfo_raw) cerca del
            # modelo.
            try:
                from phases.mkv_analyze import persist_mkv_basic_to_cache
                persist_mkv_basic_to_cache(mkv_full, result)
            except Exception as e:
                _logger.warning("Cache write falló (no bloquea): %s", e)
        _analyze_progress = {"step": "", "done": True}
        return result.model_dump()
    except Exception as e:
        _analyze_progress = {"step": "", "done": True, "error": str(e)}
        _logger.exception("Error analizando MKV %s", mkv_full)
        raise HTTPException(status_code=500, detail=str(e))


# Estado global del analisis de perfil de luminancia — lo consume el endpoint
# de progreso /api/mkv/light-profile/progress (polling desde frontend).
# Single-writer (solo hay un analisis activo a la vez en la UI).
_light_profile_state: dict = {
    "active": False,
    "step": 0,           # 0=idle, 1=ffmpeg, 2=extract-rpu, 3=export+parse, 4=done
    "step_label": "",
    "step_pct": 0,       # 0-100 dentro del paso actual
    "global_pct": 0,     # 0-100 total
    "elapsed_s": 0,
    "log_lines": [],     # rolling log (max 60 lineas)
    "error": None,
    "started_at": 0.0,
}


# Tracking del subprocess activo (ffmpeg/dovi_tool) para cancelacion +
# flag de cancelacion. Single-job singleton igual que el state. Patron
# similar al _cmv40_active_procs de Tab 3.
_lp_active_proc: dict = {"proc": None}
_lp_cancel: dict = {"requested": False}


def _lp_reset():
    _lp_active_proc["proc"] = None
    _lp_cancel["requested"] = False
    _light_profile_state.update({
        "active": True,
        "step": 0, "step_label": "", "step_pct": 0, "global_pct": 0,
        "elapsed_s": 0, "log_lines": [], "error": None,
        "result": None,
        "started_at": __import__("time").monotonic(),
    })


def _lp_register_proc(proc):
    """Registra el subprocess activo para que cancel() pueda matarlo."""
    _lp_active_proc["proc"] = proc


def _lp_check_cancel():
    """Lanza RuntimeError si el usuario cancelo. Llamar en cada paso."""
    if _lp_cancel["requested"]:
        raise RuntimeError("Cancelado por el usuario")


def _lp_log(msg: str):
    import time as _t
    ts = _t.strftime("%H:%M:%S")
    _light_profile_state["log_lines"].append(f"[{ts}] {msg}")
    if len(_light_profile_state["log_lines"]) > 60:
        _light_profile_state["log_lines"] = _light_profile_state["log_lines"][-60:]
    started = _light_profile_state.get("started_at") or _t.monotonic()
    _light_profile_state["elapsed_s"] = int(_t.monotonic() - started)
    _logger.info("light-profile: %s", msg)


def _lp_set_step(step: int, label: str, global_pct_base: int):
    _light_profile_state["step"] = step
    _light_profile_state["step_label"] = label
    _light_profile_state["step_pct"] = 0
    _light_profile_state["global_pct"] = global_pct_base
    _lp_log(f"Paso {step}/3 — {label}")


def _lp_set_step_pct(step_pct: float, global_pct: float):
    _light_profile_state["step_pct"] = int(max(0, min(100, step_pct)))
    _light_profile_state["global_pct"] = int(max(0, min(100, global_pct)))
    import time as _t
    _light_profile_state["elapsed_s"] = int(_t.monotonic() - (_light_profile_state.get("started_at") or _t.monotonic()))


@app.get("/api/mkv/light-profile/progress")
async def mkv_light_profile_progress_endpoint():
    """Estado actual del analisis de perfil de luminancia (polling)."""
    return dict(_light_profile_state)


@app.post("/api/mkv/light-profile/cancel")
async def mkv_light_profile_cancel_endpoint():
    """Cancela el analisis activo: SIGTERM → SIGKILL al subprocess +
    cancel flag. CRITICO: esperar a que el subprocess sea totalmente
    reaped antes de retornar — sino el siguiente analisis puede chocar
    con file handles/locks del NAS aun no liberados (especialmente sobre
    VPN), causando que el nuevo ffmpeg se cuelgue al intentar leer el
    mismo MKV.
    """
    if not _light_profile_state.get("active"):
        return {"ok": False, "reason": "no hay analisis activo"}
    _lp_cancel["requested"] = True
    proc = _lp_active_proc.get("proc")
    if proc:
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                # No respondio a SIGTERM en 3s — escalamos a SIGKILL.
                try:
                    proc.kill()
                except Exception:
                    pass
                # Espera bloqueante a que SIGKILL sea procesado y el
                # subprocess sea reaped por el kernel. Sin esto, el
                # cancel handler retorna mientras el proceso sigue en
                # estado D (uninterruptible sleep) con file handles
                # abiertos sobre el NAS — colgando el siguiente run.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    _logger.warning(
                        "light-profile cancel: subprocess no reaped tras 10s "
                        "(zombie probable)"
                    )
        except Exception as e:
            _logger.warning("light-profile cancel: kill subprocess fallo: %s", e)
    _lp_active_proc["proc"] = None
    _lp_log("✗ Cancelado por el usuario")
    _light_profile_state["error"] = "Cancelado por el usuario"
    _light_profile_state["active"] = False
    return {"ok": True}


@app.post("/api/mkv/light-profile", summary="Extrae MaxCLL/MaxFALL por escena (on-demand)")
async def mkv_light_profile_endpoint(body: dict):
    """Extrae el perfil de luminancia del MOVIE COMPLETO del MKV.
    Alimenta el sparkline + histograma de la Radiografía DV+HDR en Tab 2.

    Body: ``{"file_path": "Movie.mkv"}``.
    Duracion tipica en UHD BD 60GB: 3-5 min (ffmpeg ~120-180s + extract-rpu
    ~60-120s + export+parse ~15s).

    Devuelve dos arrays downsampleados a 240 buckets + total_frames.
    """
    import tempfile
    rel_path = body.get("file_path", "")
    _lp_reset()
    if DEV_MODE:
        import math, random
        random.seed(42)
        n = 240
        series_cll = [int(300 + 500 * math.sin(i/15) + random.randint(-80, 120) + (i/n)*200) for i in range(n)]
        series_cll = [max(10, v) for v in series_cll]
        series_fall = [max(5, int(v * 0.12 + random.randint(-10, 20))) for v in series_cll]
        _light_profile_state["active"] = False
        return {"per_scene_max_cll": series_cll, "per_scene_max_fall": series_fall, "total_frames": 186113}

    # Acepta rutas absolutas (file browser) o filenames relativos a /mnt/output (legacy)
    try:
        mkv_path_obj = _resolve_mkv_path_safe(rel_path)
    except HTTPException:
        _light_profile_state["active"] = False
        raise
    mkv_full = str(mkv_path_obj)
    if not mkv_path_obj.exists():
        _light_profile_state["active"] = False
        raise HTTPException(status_code=400, detail=f"MKV no encontrado: {rel_path}")

    from phases.phase_a import DOVI_TOOL_BIN, FFMPEG_BIN
    import json as _json, time as _time
    tmp_dir = Path(tempfile.mkdtemp(prefix="lightprof_"))
    rpu_path = tmp_dir / "rpu.bin"
    hevc_path = tmp_dir / "sample.hevc"

    # Estimacion del HEVC final segun bitrate del MKV (para el pct de ffmpeg)
    try:
        mkv_size = Path(mkv_full).stat().st_size
        # HEVC suele ser 70-85% del MKV (excluyendo audio/subs). Usamos 75%.
        expected_hevc_size = int(mkv_size * 0.75)
    except Exception:
        expected_hevc_size = 0

    try:
        # Estrategia: ffmpeg hace demux eficiente del MKV (stream-copy sin
        # reencodar) → HEVC annex-B local → dovi_tool extract-rpu sobre HEVC.
        # Mucho más rápido que dovi_tool directo sobre MKV (matroska-rs
        # lento en NAS con HDD).
        ff_timeout = 1200  # 20 min max para full movie
        dt_timeout = 900   # 15 min max para extract-rpu

        ff_cmd = [FFMPEG_BIN, "-y", "-v", "error",
                  "-i", mkv_full,
                  "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                  "-f", "hevc", str(hevc_path)]

        # ═══════ Paso 1 · ffmpeg con monitor de progreso por file size ═══
        _lp_check_cancel()
        _lp_set_step(1, "Extrayendo HEVC del MKV con ffmpeg", 0)
        t0 = _time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *ff_cmd,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _lp_register_proc(proc)
        # Monitor task: cada 1s leemos el tamaño del HEVC y actualizamos pct.
        # Paso 1 ocupa 55% del pct global (55/35/10 aprox para las 3 fases).
        stop_mon = asyncio.Event()

        async def _ff_monitor():
            while not stop_mon.is_set():
                try:
                    if hevc_path.exists() and expected_hevc_size > 0:
                        size = hevc_path.stat().st_size
                        pct = min(99, size * 100 / expected_hevc_size)
                        _lp_set_step_pct(pct, pct * 0.55)
                        if int(pct) % 10 == 0 and int(pct) > 0:
                            pass  # evitamos spam del log
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop_mon.wait(), timeout=1.5)
                except asyncio.TimeoutError:
                    pass

        mon_task = asyncio.create_task(_ff_monitor())
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=ff_timeout)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            stop_mon.set()
            _light_profile_state["error"] = f"ffmpeg excedió {ff_timeout}s"
            raise HTTPException(status_code=504,
                                detail=f"ffmpeg excedió {ff_timeout}s extrayendo HEVC del MKV.")
        finally:
            stop_mon.set()
            try: await mon_task
            except Exception: pass

        if proc.returncode != 0 or not hevc_path.exists() or hevc_path.stat().st_size < 1024:
            err = stderr.decode("utf-8", errors="replace")[:400]
            _light_profile_state["error"] = f"ffmpeg falló: {err}"
            raise HTTPException(status_code=500, detail=f"ffmpeg falló: {err}")
        ff_gb = hevc_path.stat().st_size / 1e9
        _lp_log(f"Paso 1/3 ✓ ffmpeg OK en {_time.monotonic()-t0:.1f}s ({ff_gb:.2f} GB HEVC)")

        # ═══════ Paso 2 · dovi_tool extract-rpu ═══════════════════════════
        _lp_check_cancel()
        _lp_set_step(2, "Extrayendo RPU del HEVC con dovi_tool", 55)
        t1 = _time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            DOVI_TOOL_BIN, "extract-rpu", str(hevc_path), "-o", str(rpu_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _lp_register_proc(proc)
        # dovi_tool no expone progreso estructurado; estimamos linealmente
        # con ETA basada en tiempo de ffmpeg (aprox ratio 0.5x ffmpeg time).
        expected_dt_s = max(20, int((_time.monotonic() - t0) * 0.5))
        stop_mon2 = asyncio.Event()

        async def _dt_monitor():
            while not stop_mon2.is_set():
                elapsed = _time.monotonic() - t1
                pct = min(99, elapsed * 100 / expected_dt_s)
                _lp_set_step_pct(pct, 55 + pct * 0.35)
                try:
                    await asyncio.wait_for(stop_mon2.wait(), timeout=1.5)
                except asyncio.TimeoutError:
                    pass

        mon_task2 = asyncio.create_task(_dt_monitor())
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=dt_timeout)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            stop_mon2.set()
            _light_profile_state["error"] = f"dovi_tool excedió {dt_timeout}s"
            raise HTTPException(status_code=504, detail=f"dovi_tool extract-rpu excedió {dt_timeout}s")
        finally:
            stop_mon2.set()
            try: await mon_task2
            except Exception: pass

        if proc.returncode != 0 or not rpu_path.exists() or rpu_path.stat().st_size < 10:
            err = stderr.decode("utf-8", errors="replace")[:400]
            _light_profile_state["error"] = f"extract-rpu falló: {err}"
            raise HTTPException(status_code=500, detail=f"extract-rpu falló: {err}")
        _lp_log(f"Paso 2/3 ✓ RPU extraído en {_time.monotonic()-t1:.1f}s ({rpu_path.stat().st_size/1e6:.2f} MB)")

        # HEVC intermedio ya no hace falta (libera ~10-20 GB)
        try: hevc_path.unlink(missing_ok=True)
        except Exception: pass

        # ═══════ Paso 3 · export JSON + parseo ════════════════════════════
        _lp_check_cancel()
        _lp_set_step(3, "Exportando metadata a JSON + parseo", 90)
        t2 = _time.monotonic()
        json_path = tmp_dir / "rpu.json"
        proc = await asyncio.create_subprocess_exec(
            DOVI_TOOL_BIN, "export", "-i", str(rpu_path), "-o", str(json_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _lp_register_proc(proc)
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            _light_profile_state["error"] = "dovi_tool export excedió 5 min"
            raise HTTPException(status_code=504, detail="dovi_tool export excedió 5 min")
        if proc.returncode != 0 or not json_path.exists():
            err = stderr.decode("utf-8", errors="replace")[:400]
            _light_profile_state["error"] = f"export falló: {err}"
            raise HTTPException(status_code=500, detail=f"export falló: {err}")

        # Parse JSON
        try:
            with open(json_path) as f:
                data = _json.load(f)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"JSON parse falló: {e}")
        _lp_log(f"Paso 3/3 ✓ export + parse JSON OK en {_time.monotonic() - t2:.1f}s")
        _lp_set_step_pct(100, 98)

        # El formato de dovi_tool export varia entre versiones. Soportamos:
        #   (a) data.rpus[*].vdr_dm_data.ext_metadata_blocks = [{level:1, ...}, ...]
        #       (array de bloques con clave 'level')
        #   (b) data.rpus[*].vdr_dm_data.ext_metadata_blocks = {level1:{...}, ...}
        #       (dict keyed por nombre de nivel)
        #   (c) data.rpus[*].vdr_dm_data.ext_metadata_blocks = [{Level1:{...}}, ...]
        #       (tagged enum de serde — variante más común en dovi_tool 2.3.x)
        rpus = data.get("rpus") if isinstance(data, dict) else data
        if not isinstance(rpus, list):
            raise HTTPException(status_code=500, detail="Formato JSON inesperado (sin 'rpus')")

        # Busqueda del bloque L1 de CMv2.9/v4.0. Antes era recursiva ciega
        # buscando "max_pq + avg_pq" → en BR2049 devolvia ~2329 (peak ~176
        # nits) cuando el peak real es ~3079 (~1000 nits). Causa probable:
        # algun bloque hermano (L8, L2 con "target_max_pq" alias en alguna
        # version de dovi_tool, o un fallback con datos historicos) tenia
        # esos campos y se encontraba antes en el orden de iteracion.
        #
        # Ahora intentamos paths conocidos primero (rapido y especifico) y
        # solo caemos al fallback recursivo si no encontramos nada. Validamos
        # min_pq <= avg_pq <= max_pq para descartar bloques con datos
        # incoherentes.

        def _l1_from_block(b):
            """Devuelve (max_pq, avg_pq, min_pq) si b parece un bloque L1 valido."""
            if not isinstance(b, dict):
                return None, None, None
            if "max_pq" not in b or "avg_pq" not in b:
                return None, None, None
            try:
                mx = int(b.get("max_pq", 0))
                av = int(b.get("avg_pq", 0))
                mn = int(b.get("min_pq", 0))
            except Exception:
                return None, None, None
            # Sanity: min<=avg<=max y todos en rango 12-bit.
            if not (0 <= mn <= av <= mx <= 8191):
                return None, None, None
            return mx, av, mn

        def _find_l1_block_full(obj, max_depth=8):
            """Busca el bloque L1 en obj. Probamos paths explicitos primero
            y caemos al fallback recursivo. Retorna (max_pq, avg_pq, min_pq)
            en 12-bit PQ, o (None, None, None)."""
            if not isinstance(obj, dict):
                return None, None, None
            # 1) Paths explicitos conocidos (dovi_tool 2.x estables)
            cm29 = obj.get("cmv29_metadata") or {}
            cm40 = obj.get("cmv40_metadata") or {}
            for parent in (cm29, cm40, obj):
                if not isinstance(parent, dict):
                    continue
                for key in ("level1", "Level1", "L1"):
                    blk = parent.get(key)
                    if isinstance(blk, dict):
                        mx, av, mn = _l1_from_block(blk)
                        if mx is not None:
                            return mx, av, mn
            # 2) ext_metadata_blocks list (formato tagged enum CMv4.0)
            for src in (obj, cm29, cm40):
                if not isinstance(src, dict):
                    continue
                blocks = src.get("ext_metadata_blocks")
                if isinstance(blocks, list):
                    for item in blocks:
                        if not isinstance(item, dict):
                            continue
                        for key in ("Level1", "level1", "L1"):
                            inner = item.get(key)
                            if isinstance(inner, dict):
                                mx, av, mn = _l1_from_block(inner)
                                if mx is not None:
                                    return mx, av, mn
                        # Quiza el item ES directamente el L1
                        mx, av, mn = _l1_from_block(item)
                        if mx is not None:
                            return mx, av, mn
            # 3) Fallback recursivo (defensivo — versiones futuras)
            def _walk(o, d):
                if d <= 0:
                    return None, None, None
                if isinstance(o, dict):
                    mx, av, mn = _l1_from_block(o)
                    if mx is not None:
                        return mx, av, mn
                    for v in o.values():
                        mx, av, mn = _walk(v, d - 1)
                        if mx is not None:
                            return mx, av, mn
                elif isinstance(o, list):
                    for v in o:
                        mx, av, mn = _walk(v, d - 1)
                        if mx is not None:
                            return mx, av, mn
                return None, None, None
            return _walk(obj, max_depth)

        # Sanity check ligero — log si no se encuentra el L1 en RPU[0]. El
        # parser ya esta validado (BR2049: nuestro peak coincide exacto con
        # dovi_tool info --summary), asi que el sample/dump diagnostico se
        # quito tras commit 5023c3e.
        if rpus and isinstance(rpus[0], dict):
            vdr0 = rpus[0].get("vdr_dm_data", {})
            test_cll, _test_avg, _test_min = _find_l1_block_full(vdr0)
            if test_cll is None:
                _logger.warning("light-profile: NO L1 en RPU[0]. top keys=%s",
                                list(rpus[0].keys())[:12])
                _logger.warning("  vdr keys=%s", list(vdr0.keys()))

        def _to_nits(v):
            """PQ code → nits. Detecta formato automáticamente."""
            v = float(v)
            if v > 4096: return v
            if v < 1:    return _pq_code_to_nits(v * 4095)
            return _pq_code_to_nits(v)

        per_frame_cll, per_frame_fall, per_frame_min = [], [], []
        # Tracking de stats raw del max_pq (12-bit) para el log
        raw_max_pq_max = 0
        raw_max_pq_sum = 0
        raw_max_pq_count = 0
        # L5 zonas: tupla (top, bottom, left, right) por frame para detectar
        # cambios de active area a lo largo del film (caso "L5 zoneado": films
        # con barras dinamicas tipo IMAX 1.43:1 ↔ 2.40:1).
        per_frame_l5: list[tuple] = []
        # L8 trims a lo largo del film: target_display_index → max_pq.
        # Iteramos TODOS los frames (vs solo el sample del extract-rpu --limit
        # del flow original), capturamos cualquier target adicional.
        l8_targets_full: dict[int, int] = {}  # {target_display_index: max_pq_seen}

        def _scan_blocks_l5l8(vdr_obj: dict) -> tuple[tuple, dict]:
            """Devuelve (l5_offsets|None, l8_trims_dict) del frame."""
            l5 = None
            l8_local: dict[int, int] = {}
            for src_key in ("cmv29_metadata", "cmv40_metadata"):
                src = vdr_obj.get(src_key) if isinstance(vdr_obj, dict) else None
                if not isinstance(src, dict):
                    continue
                blocks = src.get("ext_metadata_blocks") or src.get("ext_blocks")
                if not isinstance(blocks, list):
                    continue
                for item in blocks:
                    if not isinstance(item, dict):
                        continue
                    # L5 active area
                    l5_obj = item.get("Level5") or item.get("level5")
                    if isinstance(l5_obj, dict) and l5 is None:
                        try:
                            l5 = (
                                int(l5_obj.get("active_area_top_offset", 0)),
                                int(l5_obj.get("active_area_bottom_offset", 0)),
                                int(l5_obj.get("active_area_left_offset", 0)),
                                int(l5_obj.get("active_area_right_offset", 0)),
                            )
                        except Exception:
                            pass
                    # L8 saturation/tone-mapping per target display
                    l8_obj = item.get("Level8") or item.get("level8")
                    if isinstance(l8_obj, dict):
                        try:
                            tdi = int(l8_obj.get("target_display_index", 0))
                            mxpq = int(l8_obj.get("target_mid_pq", 0)) or int(l8_obj.get("trim_slope", 0))
                            # target_max_pq es el field clave para L8 (peak nits del display objetivo)
                            mxpq2 = int(l8_obj.get("target_max_pq", 0))
                            if mxpq2:
                                mxpq = mxpq2
                            if tdi:
                                l8_local[tdi] = mxpq or l8_local.get(tdi, 0)
                        except Exception:
                            pass
            return l5, l8_local

        for rpu in rpus:
            vdr = (rpu or {}).get("vdr_dm_data", {})
            cll, fall, mn = _find_l1_block_full(vdr)
            if cll is not None:
                if cll > raw_max_pq_max:
                    raw_max_pq_max = cll
                raw_max_pq_sum += cll
                raw_max_pq_count += 1
                try: per_frame_cll.append(int(round(_to_nits(cll))))
                except Exception: per_frame_cll.append(0)
            if fall is not None:
                try: per_frame_fall.append(int(round(_to_nits(fall))))
                except Exception: per_frame_fall.append(0)
            if mn is not None:
                try: per_frame_min.append(int(round(_to_nits(mn))))
                except Exception: per_frame_min.append(0)
            l5_frame, l8_frame = _scan_blocks_l5l8(vdr)
            if l5_frame is not None:
                per_frame_l5.append(l5_frame)
            for tdi, mxpq in l8_frame.items():
                # Guarda el max_pq mas alto visto para cada target_display_index
                if mxpq > l8_targets_full.get(tdi, 0):
                    l8_targets_full[tdi] = mxpq

        raw_avg = (raw_max_pq_sum / raw_max_pq_count) if raw_max_pq_count else 0
        _logger.info(
            "light-profile: parseo extrajo %d CLL + %d FALL frames de %d RPUs · "
            "L1 peak max_pq=%d avg max_pq=%.0f",
            len(per_frame_cll), len(per_frame_fall), len(rpus),
            raw_max_pq_max, raw_avg,
        )

        if not per_frame_cll:
            raise HTTPException(
                status_code=500,
                detail=f"El JSON de dovi_tool export ({len(rpus)} RPUs) no contiene bloques L1 con max_pq+avg_pq. Revisa los logs del contenedor para ver la estructura real de vdr_dm_data."
            )

        # ── Referencias del RPU para overlay del chart ────────────────
        # L2 trim targets (target_max_pq) → nits. Estos son los "displays
        # objetivo" para los que el colorista hizo trims (tipico: 100/600/1000).
        # Coleccionamos todos los unicos (en target_max_pq 12-bit) y los
        # convertimos a nits.
        l2_targets_pq = set()
        # L6 mastering display (rango del display master)
        l6_master_max_nits = 0
        l6_master_min_nits = 0.0
        # L6 max_content_light_level (raramente seteado pero util si lo esta)
        l6_max_cll = 0
        l6_max_fall = 0
        # source_max_pq del top-level vdr_dm_data — peak del display master
        # en PQ; suele coincidir con L6.max_display_mastering_luminance pero
        # se mantiene por separado por si difieren.
        if rpus and isinstance(rpus[0], dict):
            vdr0 = rpus[0].get("vdr_dm_data", {})
            # Recorrer ext_metadata_blocks para encontrar L2 + L6
            cm29 = vdr0.get("cmv29_metadata", {}) or {}
            for src in (cm29, vdr0.get("cmv40_metadata", {}) or {}):
                blocks = src.get("ext_metadata_blocks") if isinstance(src, dict) else None
                if not isinstance(blocks, list):
                    continue
                for item in blocks:
                    if not isinstance(item, dict):
                        continue
                    # L2 trims: target_max_pq en 12-bit
                    l2 = item.get("Level2") or item.get("level2")
                    if isinstance(l2, dict) and "target_max_pq" in l2:
                        try:
                            l2_targets_pq.add(int(l2["target_max_pq"]))
                        except Exception:
                            pass
                    # L6 mastering display (en NITS directos, no PQ)
                    l6 = item.get("Level6") or item.get("level6")
                    if isinstance(l6, dict):
                        try:
                            l6_master_max_nits = int(l6.get("max_display_mastering_luminance", 0))
                            l6_master_min_nits = float(l6.get("min_display_mastering_luminance", 0)) / 10000.0
                            l6_max_cll = int(l6.get("max_content_light_level", 0))
                            l6_max_fall = int(l6.get("max_frame_average_light_level", 0))
                        except Exception:
                            pass

        l2_targets_nits = sorted({int(round(_to_nits(pq))) for pq in l2_targets_pq})

        # ── L5 zonas: agrupar offsets unicos a lo largo del film ──────
        # Si el film tiene letterbox dinamico (IMAX 1.43 ↔ 2.40, etc.), aqui
        # detectamos las distintas zonas. Devolvemos la lista ordenada por
        # frequencia (la mas comun primero) con conteo de frames.
        from collections import Counter
        l5_zones_counter = Counter(per_frame_l5)
        l5_zones_list = []
        for offsets, count in l5_zones_counter.most_common():
            l5_zones_list.append({
                "top": offsets[0],
                "bottom": offsets[1],
                "left": offsets[2],
                "right": offsets[3],
                "frames": count,
                "pct": round(count / max(1, len(per_frame_l5)) * 100, 1),
            })

        # ── L8 trims: convertir target_max_pq a nits, ordenar ASC ────
        # l8_targets_full = {target_display_index: target_max_pq}. Convertimos
        # max_pq → nits y devolvemos lista unica ordenada.
        l8_trim_nits_full = sorted({
            int(round(_to_nits(mxpq))) for mxpq in l8_targets_full.values() if mxpq
        })

        # ── Stats: percentiles + buckets de clasificacion ─────────────
        sorted_cll = sorted(per_frame_cll)

        def _percentile(xs, p):
            if not xs: return 0
            idx = max(0, min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1)))))
            return xs[idx]

        peak = max(per_frame_cll) if per_frame_cll else 0
        p50 = _percentile(sorted_cll, 50)
        p95 = _percentile(sorted_cll, 95)
        p99 = _percentile(sorted_cll, 99)
        avg_of_max = sum(per_frame_cll) / len(per_frame_cll) if per_frame_cll else 0
        # Buckets sobre per_frame_cll (peak por escena/frame)
        bucket_dim = sum(1 for v in per_frame_cll if v < 100)
        bucket_mid = sum(1 for v in per_frame_cll if 100 <= v < 300)
        bucket_high = sum(1 for v in per_frame_cll if v >= 300)
        total = max(1, len(per_frame_cll))

        # Downsample a ~240 buckets para la sparkline (no tiene sentido mostrar
        # 186k frames uno a uno)
        MAX_POINTS = 240
        def _downsample_max(xs):
            if len(xs) <= MAX_POINTS:
                return xs
            step = len(xs) / MAX_POINTS
            return [max(xs[int(i * step):int((i + 1) * step)] or [0]) for i in range(MAX_POINTS)]

        def _downsample_avg(xs):
            if len(xs) <= MAX_POINTS:
                return xs
            step = len(xs) / MAX_POINTS
            out = []
            for i in range(MAX_POINTS):
                seg = xs[int(i * step):int((i + 1) * step)]
                out.append(int(round(sum(seg) / len(seg))) if seg else 0)
            return out

        def _downsample_min(xs):
            if len(xs) <= MAX_POINTS:
                return xs
            step = len(xs) / MAX_POINTS
            return [min(xs[int(i * step):int((i + 1) * step)] or [0]) for i in range(MAX_POINTS)]

        _lp_log(f"✓ Listo — {len(per_frame_cll):,} frames analizados, downsample a {MAX_POINTS} buckets")
        _light_profile_state["step"] = 4
        _light_profile_state["step_label"] = "Listo"
        _light_profile_state["step_pct"] = 100
        _light_profile_state["global_pct"] = 100
        # Resultado tambien guardado en el state para que el frontend pueda
        # recuperarlo via polling si el POST aborta (timeout en MKVs muy
        # grandes que tardan >60 min). El POST sigue devolviendo el dato
        # como antes — esto es solo un "buffer" para fallback.
        result = {
            "per_scene_max_cll":  _downsample_max(per_frame_cll),
            # avg_pq por escena (anteriormente per_scene_max_fall, mantenemos
            # nombre por compat) — promediado en cada bucket
            "per_scene_max_fall": _downsample_avg(per_frame_fall) if per_frame_fall else [],
            "per_scene_min":      _downsample_min(per_frame_min) if per_frame_min else [],
            "total_frames": len(per_frame_cll),
            # Stats globales (sobre per_frame_cll antes del downsample)
            "stats": {
                "peak":        peak,
                "p99":         p99,
                "p95":         p95,
                "p50":         p50,
                "avg_of_max":  int(round(avg_of_max)),
                "bucket_dim":  bucket_dim,
                "bucket_mid":  bucket_mid,
                "bucket_high": bucket_high,
                "total":       total,
            },
            # Referencias del RPU para overlay y leyenda
            "references": {
                "l2_trim_targets_nits":  l2_targets_nits,        # ej. [100, 600, 1000]
                "l6_master_max_nits":    l6_master_max_nits,     # ej. 4000
                "l6_master_min_nits":    l6_master_min_nits,     # ej. 0.005
                "l6_max_cll":            l6_max_cll,              # ej. 0 (no seteado en BR2049)
                "l6_max_fall":           l6_max_fall,
                # L5 zonas detectadas a lo largo del film. Si len > 1,
                # el film tiene active area dinamica (letterbox cambiante).
                # Si len == 1, el active area es uniforme (caso normal).
                "l5_zones":              l5_zones_list,
                # L8 trim nits del film completo (reemplaza al sample).
                # Usar este array en vez del DoviInfo.l8_trim_nits si esta
                # presente — captura targets que solo aparecen en frames mid/late.
                "l8_trim_nits_full":     l8_trim_nits_full,
            },
        }
        _light_profile_state["result"] = result
        return result
    finally:
        for p in (rpu_path, hevc_path, tmp_dir / "rpu.json"):
            try: p.unlink(missing_ok=True)
            except Exception: pass
        try: tmp_dir.rmdir()
        except Exception: pass
        _light_profile_state["active"] = False


# ══════════════════════════════════════════════════════════════════════
#  TAB 2 — Quality audit (RPU L8/L2 combos + classify) on-demand
# ══════════════════════════════════════════════════════════════════════
#
# Pipeline costoso (~5-10 min UHD BD) que se dispara cuando el usuario
# pulsa "🔬 Auditar calidad" en la card del panel. Reusa el infra del
# light-profile (single-job singleton + state + cancel cooperativo).
# Al terminar, persiste el bloque 'quality' del cache MKV — futuras
# aperturas del mismo fichero muestran la card poblada directamente.

_mkv_quality_state: dict = {
    "active": False,
    "step": "",           # "ffmpeg" | "extract_rpu" | "combos" | "done" | "error"
    "step_label": "",
    "global_pct": 0,      # 0-100 total
    "elapsed_s": 0,
    "log_lines": [],      # rolling log (max 60 líneas)
    "error": None,
    "started_at": 0.0,
    "result": None,       # dict con los campos quality_* tras éxito
    "file_name": "",
}

_mkv_quality_active_proc: dict = {"proc": None}
_mkv_quality_cancel: dict = {"requested": False}


def _mkv_quality_reset(file_name: str = ""):
    import time as _t
    _mkv_quality_active_proc["proc"] = None
    _mkv_quality_cancel["requested"] = False
    _mkv_quality_state.update({
        "active": True,
        "step": "ffmpeg",
        "step_label": "Iniciando auditoría…",
        "global_pct": 0,
        "elapsed_s": 0,
        "log_lines": [],
        "error": None,
        "result": None,
        "started_at": _t.monotonic(),
        "file_name": file_name,
    })


def _mkv_quality_log(msg: str):
    import time as _t
    ts = _t.strftime("%H:%M:%S")
    # Los markers semánticos (━━━, $, ✓, 📋, 🎯, ├─) deben mantener su
    # prefijo intacto para que el clasificador del frontend (.cmv40-log)
    # asigne la clase correcta. Solo prefijamos el timestamp.
    _mkv_quality_state["log_lines"].append(f"[{ts}] {msg}")
    # Buffer rolling generoso — el log enriquecido genera 30-50 líneas por
    # run típico. 200 deja margen sobrado y son ~25 KB en el peor caso.
    if len(_mkv_quality_state["log_lines"]) > 200:
        _mkv_quality_state["log_lines"] = _mkv_quality_state["log_lines"][-200:]
    started = _mkv_quality_state.get("started_at") or _t.monotonic()
    _mkv_quality_state["elapsed_s"] = int(_t.monotonic() - started)


def _mkv_quality_check_cancel():
    """Lanza RuntimeError si el usuario canceló. Llamar entre pasos."""
    if _mkv_quality_cancel["requested"]:
        raise RuntimeError("Cancelado por el usuario")


@app.get("/api/mkv/quality-audit/progress")
async def mkv_quality_audit_progress():
    """Polling endpoint para el modal de auditoría."""
    return dict(_mkv_quality_state)


@app.post("/api/mkv/quality-audit/cancel")
async def mkv_quality_audit_cancel():
    """Solicita cancelación cooperativa + SIGTERM/SIGKILL del subprocess.

    Espera a que el subprocess sea reaped antes de retornar — sin esto,
    el siguiente intento puede chocar con file handles aún abiertos sobre
    el NAS (mismo patrón que el cancel del light-profile)."""
    if not _mkv_quality_state.get("active"):
        return {"ok": False, "reason": "no_active_job"}
    _mkv_quality_cancel["requested"] = True
    proc = _mkv_quality_active_proc.get("proc")
    if proc:
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try: proc.kill()
                except Exception: pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    _logger.warning(
                        "quality-audit cancel: subprocess no reaped tras 10s "
                        "(zombie probable)"
                    )
        except Exception as e:
            _logger.warning("quality-audit cancel: kill subprocess falló: %s", e)
    _mkv_quality_active_proc["proc"] = None
    _mkv_quality_log("✗ Cancelado por el usuario")
    _mkv_quality_state["error"] = "Cancelado por el usuario"
    _mkv_quality_state["active"] = False
    return {"ok": True}


@app.get("/api/mkv/cache-info", summary="Diagnóstico del cache de un MKV")
async def mkv_cache_info_endpoint(file_path: str = ""):
    """Inspecciona el cache /config/mkv_audits/ para un MKV concreto.

    Útil para diagnosticar "el audit acabó en pocos segundos sin error" —
    suele ser cache hit con un quality cacheado de un intento previo basura.

    Devuelve fingerprint actual, fingerprint cacheado, versiones de los
    bloques basic/quality, y un resumen del quality si está poblado.
    """
    if not file_path:
        raise HTTPException(status_code=400, detail="file_path requerido")
    mkv_path_obj = _resolve_mkv_path_safe(file_path)
    mkv_full = str(mkv_path_obj)
    if not mkv_path_obj.exists():
        raise HTTPException(status_code=404, detail=f"MKV no encontrado: {file_path}")
    from storage import compute_mkv_fingerprint, _mkv_audit_path
    from phases.mkv_analyze import (
        CACHE_VERSION_BASIC, CACHE_VERSION_QUALITY, _quality_payload_is_valid,
    )
    fp = compute_mkv_fingerprint(mkv_full)
    cache_path = _mkv_audit_path(fp["sha256_1mb"]) if fp else None
    cache_exists = bool(cache_path and cache_path.exists())
    raw = None
    if cache_exists:
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception as e:
            raw = {"_parse_error": str(e)}
    out = {
        "mkv_path": mkv_full,
        "current_fingerprint": fp,
        "cache_file": str(cache_path) if cache_path else None,
        "cache_exists": cache_exists,
        "cache_app_versions": {"basic": CACHE_VERSION_BASIC, "quality": CACHE_VERSION_QUALITY},
        "cache_persisted_versions": (raw or {}).get("versions") if isinstance(raw, dict) else None,
        "cache_persisted_fingerprint": (raw or {}).get("fingerprint") if isinstance(raw, dict) else None,
        "cache_cached_at": (raw or {}).get("cached_at") if isinstance(raw, dict) else None,
        "basic_present": bool(isinstance(raw, dict) and raw.get("basic")),
        "quality_present": bool(isinstance(raw, dict) and raw.get("quality")),
    }
    quality = (raw or {}).get("quality") if isinstance(raw, dict) else None
    if quality:
        out["quality_summary"] = {
            "is_valid_payload": _quality_payload_is_valid(quality),
            "total_frames_rpu": quality.get("quality_total_frames_rpu"),
            "frames_with_cmv40": quality.get("quality_frames_with_cmv40"),
            "scene_cuts": quality.get("quality_scene_cuts"),
            "l8_unique_count": quality.get("quality_l8_unique_count"),
            "l2_unique_count": quality.get("quality_l2_unique_count"),
            "classification": quality.get("quality_classification"),
            "tier": quality.get("quality_tier"),
            "verdict_text": quality.get("quality_verdict_text"),
        }
    return out


@app.delete("/api/mkv/cache-info", summary="Borra el cache de un MKV")
async def mkv_cache_delete_endpoint(file_path: str = ""):
    """Borra el fichero de cache de un MKV. Útil para forzar reanálisis
    cuando se sospecha que el cache está corrupto o tiene un quality basura."""
    if not file_path:
        raise HTTPException(status_code=400, detail="file_path requerido")
    mkv_path_obj = _resolve_mkv_path_safe(file_path)
    mkv_full = str(mkv_path_obj)
    if not mkv_path_obj.exists():
        raise HTTPException(status_code=404, detail=f"MKV no encontrado: {file_path}")
    from storage import invalidate_mkv_cache_by_path
    removed = invalidate_mkv_cache_by_path(mkv_full)
    return {"ok": True, "cache_removed": removed, "file_path": mkv_full}


@app.post("/api/mkv/quality-audit", summary="Auditoría profunda del RPU (on-demand)")
async def mkv_quality_audit_endpoint(body: dict):
    """Ejecuta el pipeline de auditoría L8/L2 sobre el RPU completo del MKV.

    Body: ``{"file_path": "/mnt/.../movie.mkv"}``.

    Tarda 5-10 min en UHD BD (ffmpeg 2-7 min + extract-rpu 1-2 min + export
    & parse 1-3 min). Devuelve un dict con los campos `quality_*` para
    inyectar en `DoviInfo`. El resultado se persiste en el bloque `quality`
    del cache MKV — re-abrir el mismo MKV en Tab 2 muestra la card poblada.

    Estado durante la ejecución expuesto via /api/mkv/quality-audit/progress.
    """
    rel_path = body.get("file_path", "")

    if DEV_MODE:
        # Fixture fake: simula el log enriquecido (DEV) con marcadores
        # semánticos, comandos y tiempos para validar la UI sin pipeline real.
        import asyncio as _aio
        _mkv_quality_reset(file_name=Path(rel_path).name or "fixture.mkv")
        _mkv_quality_log("[Audit] 📋 Plan: extraer HEVC del MKV → extraer RPU Dolby Vision → agregar combos L8/L2 y clasificar. ~5-10 min en UHD BD (~62 GB).")
        _mkv_quality_log("[Audit] Workdir temporal: /tmp/mkv_quality_audit_DEV · se borrará al terminar")
        _mkv_quality_log("━━━ Paso 1/3 · Extracción HEVC ━━━")
        _mkv_quality_log("[Audit] 📋 Plan: ffmpeg stream-copy del v:0 del MKV a HEVC annex-B local. Tamaño esperado del HEVC: ~46 GB (75% del MKV, sin audio/subs).")
        _mkv_quality_log("$ ffmpeg -y -v error -i /mnt/output/Movie.mkv -map 0:v:0 -c:v copy -bsf:v hevc_mp4toannexb -f hevc /tmp/.../video.hevc")
        for pct in (10, 30, 55):
            _mkv_quality_state["global_pct"] = pct
            _mkv_quality_state["step"] = "ffmpeg"
            _mkv_quality_log(f"[Audit] HEVC: {int(pct/0.55)}% ({pct/2:.1f} GB / 46 GB esperado)")
            await _aio.sleep(0.5)
        _mkv_quality_log("[Audit] ✓ HEVC extraído en 4m 12s · 44.18 GB")
        _mkv_quality_log("━━━ Paso 2/3 · Extracción RPU Dolby Vision ━━━")
        _mkv_quality_log("[Audit] 📋 Plan: dovi_tool extract-rpu lee el HEVC bitstream y extrae las NALUs DV RPU. CPU-bound, ~1-2 min para UHD.")
        _mkv_quality_log("$ dovi_tool extract-rpu /tmp/.../video.hevc -o /tmp/.../rpu.bin")
        for pct in (65, 75, 80):
            _mkv_quality_state["global_pct"] = pct
            _mkv_quality_state["step"] = "extract_rpu"
            await _aio.sleep(0.4)
        _mkv_quality_log("  Parsing RPU... ████████ 100%")
        _mkv_quality_log("[Audit] ✓ RPU extraído en 1m 47s · 142 MB")
        _mkv_quality_log("[Audit] ⏬ HEVC intermedio liberado (no se vuelve a usar)")
        _mkv_quality_log("━━━ Paso 3/3 · Análisis de combos L8/L2 + clasificación ━━━")
        _mkv_quality_log("[Audit] 📋 Plan: dovi_tool export -d all sobre el RPU → JSON grande (~3-5× el tamaño del RPU) → parsear y agregar combos únicos por frame.")
        _mkv_quality_log("$ dovi_tool export -i /tmp/.../rpu.bin -d all=<json_temp>")
        for pct in (88, 95):
            _mkv_quality_state["global_pct"] = pct
            _mkv_quality_state["step"] = "combos"
            await _aio.sleep(0.3)
        _mkv_quality_log("[Audit] Frames analizados: 189,123 · CMv4.0 cobertura: 100% · scene cuts: 1,487")
        _mkv_quality_log("[Audit] L8: 2,547 combos únicos · 11% frames neutros · mid_contrast · clip_trim")
        _mkv_quality_log("[Audit] L2: 73 combos únicos · 4 target_pqs ([62, 2081, 2851, 3079])")
        _mkv_quality_log("[Audit] ✓ Combos agregados en 1m 04s")
        _mkv_quality_state["global_pct"] = 100
        _mkv_quality_state["step"] = "combos"
        await _aio.sleep(0.3)
        _mkv_quality_log("[Audit] 🎯 Resultado: Master CMv4.0 FULL — calidad máxima")
        _mkv_quality_log("[Audit] Tier: CMv4 FULL")
        _mkv_quality_log("[Audit] L8 trabajado por colorista — 2547 combos únicos, 89% frames con trim (FULL).")
        _mkv_quality_log("[Audit] ├─ Master nativo CMv4.0 reciente — L11 + L254 presentes")
        _mkv_quality_log("[Audit] ├─ Metadata DV completa — source primaries (L9) + target primaries (L10) + content type (L11)")
        _mkv_quality_log("✓ Auditoría completada en 7m 03s")
        fake_result = {
            "quality_total_frames_rpu": 189123,
            "quality_frames_with_cmv40": 189123,
            "quality_scene_cuts": 1487,
            "quality_l2_unique_count": 73,
            "quality_l2_target_pqs": [62, 2081, 2851, 3079],
            "quality_l8_unique_count": 2547,
            "quality_l8_neutral_pct": 0.11,
            "quality_l8_has_mid_contrast": True,
            "quality_l8_has_clip_trim": True,
            "quality_classification": "real",
            "quality_reason": "L8 trabajado por colorista — 2547 combos únicos, 89% frames con trim (FULL).",
            "quality_tier": "full",
            "quality_tier_label": "CMv4 FULL",
            "quality_tier_description": "Master CMv4.0 FULL — campos exclusivos CMv4.0 poblados.",
            "quality_verdict_text": "Master CMv4.0 FULL — calidad máxima",
            "quality_verdict_color": "green",
            "quality_provenance_hints": [
                "Master nativo CMv4.0 reciente — L11 + L254 presentes",
                "Metadata DV completa — source primaries (L9) + target primaries (L10) + content type (L11)",
            ],
        }
        _mkv_quality_state["result"] = fake_result
        _mkv_quality_state["active"] = False
        return fake_result

    mkv_path_obj = _resolve_mkv_path_safe(rel_path)
    mkv_full = str(mkv_path_obj)
    if not mkv_path_obj.exists():
        raise HTTPException(status_code=400, detail=f"MKV no encontrado: {rel_path}")

    if _mkv_quality_state.get("active"):
        raise HTTPException(
            status_code=409,
            detail="Ya hay una auditoría en curso. Cancélala o espera a que termine.",
        )

    _mkv_quality_reset(file_name=mkv_path_obj.name)

    def _progress_cb(step: str, pct: float, label: str):
        # SOLO actualiza estado para el modal/polling. NO loguea — la lógica
        # de logging detallado vive en analyze_rpu_quality_for_mkv via
        # log_callback, con marcadores semánticos ricos (━━━ pasos, comandos
        # $, ✓ resultados). Hacer log aquí duplicaría líneas.
        _mkv_quality_state["step"] = step
        _mkv_quality_state["global_pct"] = int(pct)
        if label:
            _mkv_quality_state["step_label"] = label

    def _register(proc):
        _mkv_quality_active_proc["proc"] = proc

    try:
        from phases.mkv_analyze import (
            analyze_rpu_quality_for_mkv, persist_mkv_quality_to_cache,
            CACHE_VERSION_BASIC, CACHE_VERSION_QUALITY,
        )
        from storage import compute_mkv_fingerprint, read_mkv_cache
        # Extraer los has_l* del análisis básico si está cacheado — los
        # usa el classifier para calcular provenance_hints. Si el básico
        # no está cacheado (caso edge: usuario ejecutó la auditoría sin
        # análisis básico previo), los hints quedan vacíos.
        dv_flags = {}
        try:
            fp = compute_mkv_fingerprint(mkv_full)
            if fp:
                basic_cached = read_mkv_cache(fp, CACHE_VERSION_BASIC, CACHE_VERSION_QUALITY)
                if basic_cached and basic_cached.get("basic"):
                    dv = (basic_cached["basic"].get("dovi") or {})
                    dv_flags = {
                        "has_l3":   dv.get("has_l3", False),
                        "has_l4":   dv.get("has_l4", False),
                        "has_l9":   dv.get("has_l9", False),
                        "has_l10":  dv.get("has_l10", False),
                        "has_l11":  dv.get("has_l11", False),
                        "has_l254": dv.get("has_l254", False),
                    }
        except Exception as e:
            _logger.warning("No se pudieron leer flags has_l* del cache (provenance hints vacíos): %s", e)
        result = await analyze_rpu_quality_for_mkv(
            mkv_full,
            progress_callback=_progress_cb,
            cancel_check=_mkv_quality_check_cancel,
            register_proc=_register,
            dv_flags=dv_flags,
            log_callback=_mkv_quality_log,
        )
        # Persistir en el cache MKV (bloque quality)
        persist_mkv_quality_to_cache(mkv_full, result)
        _mkv_quality_state["result"] = result
        _mkv_quality_state["step"] = "done"
        _mkv_quality_state["global_pct"] = 100
        return result
    except RuntimeError as e:
        msg = str(e)
        _mkv_quality_state["error"] = msg
        _mkv_quality_state["step"] = "error"
        _mkv_quality_log(f"✗ Error: {msg}")
        status = 499 if "Cancelado" in msg else 500
        raise HTTPException(status_code=status, detail=msg)
    except Exception as e:
        _logger.exception("quality-audit falló inesperadamente sobre %s", mkv_full)
        _mkv_quality_state["error"] = str(e)
        _mkv_quality_state["step"] = "error"
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _mkv_quality_active_proc["proc"] = None
        _mkv_quality_state["active"] = False


def _pq_code_to_nits(code_value: float) -> float:
    """Convierte valor PQ (0-4095) a nits via SMPTE ST 2084 EOTF inverse."""
    # PQ inverse EOTF: L = 10000 * ((max(0, V^(1/m2) - c1)) / (c2 - c3 * V^(1/m2)))^(1/m1)
    # V = code_value / 4095
    v = max(0.0, min(1.0, code_value / 4095.0))
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 4096.0 * 128.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 4096.0 * 32.0
    c3 = 2392.0 / 4096.0 * 32.0
    vm2 = v ** (1.0 / m2)
    num = max(0.0, vm2 - c1)
    den = c2 - c3 * vm2
    if den <= 0:
        return 0.0
    return 10000.0 * (num / den) ** (1.0 / m1)


# Estado global de la operación de apply (single-job singleton). Permite
# polling de progreso mientras la copia/edición está en curso. Solo se
# usa cuando el MKV está en /mnt/library y necesita copia previa a
# /mnt/output (operación que puede tardar minutos para UHD ~50-70 GB).
#
# Persistido en /config/mkv_apply_state.json para sobrevivir a:
#   - Refresh de pestaña en cliente (Tab 2 lo carga al abrir y reabre el
#     modal si hay un job activo).
#   - Restart del contenedor (al arrancar, recovery: si hay job "active"
#     pero no hay subprocess vivo → marcar error + cleanup destino parcial).
#
# Se incluye `src_path` y `dst_path` para que tras un restart sepamos qué
# fichero parcial limpiar y qué nombre/destino tenía la operación. NO
# necesitamos el subprocess en sí — el reset de state al cargar deja el
# job en estado "error" que el frontend muestra correctamente.
_mkv_apply_state: dict = {
    "active": False,
    "step": "",          # "copying" | "applying" | "done" | "error" | "cancelled"
    "step_label": "",
    "bytes_copied": 0,
    "total_bytes": 0,
    "pct": 0,            # 0-100 (de la copia)
    "eta_s": 0,
    "elapsed_s": 0,
    "started_at": 0.0,
    "error": None,
    "src_path": "",
    "dst_path": "",
    "file_name": "",
}

# Flag de cancelación cooperativa. El usuario lo setea via POST
# /api/mkv/apply/cancel. El thread de copia lo chequea antes de cada chunk
# y aborta limpiamente, dejando la app borrar el destino parcial.
_mkv_apply_cancel = {"requested": False}

# Path donde persistimos el estado. Lo cargamos al arrancar para auto-resume
# (cliente) o cleanup (server restart).
_MKV_APPLY_STATE_FILE = Path(os.environ.get("CONFIG_DIR", "/config")) / "mkv_apply_state.json"


def _persist_mkv_apply_state() -> None:
    """Escribe el estado a disco con atomicidad .tmp + rename. Throttled
    durante 'copying' a 1 update/segundo (escribir 8MB/chunk × N chunks
    sería ~3MB/seg de I/O innecesaria — el cliente polea cada 1s, así que
    suficiente granularidad). En transiciones de step (done/error/cancelled)
    se persiste inmediatamente."""
    import json as _json
    try:
        _MKV_APPLY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _MKV_APPLY_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(_mkv_apply_state, indent=2), encoding="utf-8")
        os.replace(tmp, _MKV_APPLY_STATE_FILE)
    except Exception as e:
        _logger.warning("[mkv_apply_state] persist falló: %s", e)


def _load_mkv_apply_state() -> dict | None:
    """Carga el estado persistido del último apply al arrancar. Devuelve
    None si no existe o está corrupto."""
    import json as _json
    if not _MKV_APPLY_STATE_FILE.exists():
        return None
    try:
        return _json.loads(_MKV_APPLY_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        _logger.warning("[mkv_apply_state] load falló: %s", e)
        return None


def _recover_interrupted_mkv_apply() -> None:
    """Si al arrancar encontramos _mkv_apply_state con active=True (señal
    de que el server cayó mid-copia), limpiar:
      - Marcar step="error" con mensaje claro
      - Borrar el destino parcial (.mkv en /mnt/output) si existe
      - Persistir el estado actualizado para que el frontend lo vea
    """
    persisted = _load_mkv_apply_state()
    if not persisted or not persisted.get("active"):
        return
    if persisted.get("step") in ("done", "cancelled", "error"):
        # Estado terminal previo, nada que recuperar — aún así limpiamos
        # active=False para que el próximo arranque no se confunda.
        _mkv_apply_state.update(persisted)
        _mkv_apply_state["active"] = False
        _persist_mkv_apply_state()
        return
    dst = persisted.get("dst_path") or ""
    file_name = persisted.get("file_name") or "(desconocido)"
    freed = 0
    if dst:
        try:
            dst_path = Path(dst)
            if dst_path.exists() and dst_path.is_file():
                freed = dst_path.stat().st_size
                dst_path.unlink()
                _logger.info(
                    "[Startup] Cleanup .mkv parcial (%s GB) tras interrupción de apply: %s",
                    f"{freed / 1e9:.2f}", dst,
                )
        except Exception as e:
            _logger.warning("[Startup] no se pudo borrar destino parcial %s: %s", dst, e)
    # Estado en error con info útil para el frontend
    _mkv_apply_state.update(persisted)
    _mkv_apply_state["active"] = False
    _mkv_apply_state["step"] = "error"
    _mkv_apply_state["error"] = (
        f"Operación interrumpida por reinicio del servidor. Destino parcial "
        f"borrado ({freed / 1e9:.2f} GB liberados). Vuelve a aplicar los cambios "
        f"sobre {file_name}."
    )
    _persist_mkv_apply_state()


_recover_interrupted_mkv_apply()


def _mkv_needs_copy_to_output(file_path: str) -> bool:
    """True si el path cae bajo LIBRARY_DIR — read-only, hay que copiar."""
    try:
        Path(file_path).resolve().relative_to(LIBRARY_DIR.resolve())
        return True
    except ValueError:
        return False


def _mkv_apply_reset(total_bytes: int = 0, src_path: str = "", dst_path: str = "", file_name: str = ""):
    import time as _t
    _mkv_apply_state.update({
        "active": True, "step": "", "step_label": "",
        "bytes_copied": 0, "total_bytes": total_bytes,
        "pct": 0, "eta_s": 0, "elapsed_s": 0,
        "started_at": _t.monotonic(), "error": None,
        "src_path": src_path, "dst_path": dst_path, "file_name": file_name,
    })
    _mkv_apply_cancel["requested"] = False
    _persist_mkv_apply_state()


def _mkv_apply_set_step(step: str, label: str = ""):
    import time as _t
    _mkv_apply_state["step"] = step
    _mkv_apply_state["step_label"] = label
    started = _mkv_apply_state.get("started_at") or _t.monotonic()
    _mkv_apply_state["elapsed_s"] = int(_t.monotonic() - started)
    # Persistir inmediatamente cualquier transición de step — son eventos
    # que el frontend NO debe perder ante crash (especialmente done/error).
    _persist_mkv_apply_state()


class MkvApplyCancelled(Exception):
    """Levantada cuando el usuario cancela la copia via /api/mkv/apply/cancel."""


async def _mkv_copy_to_output_with_progress(src: Path, dst: Path) -> None:
    """Copia src → dst en chunks, actualizando _mkv_apply_state en vivo.

    Usa un thread para no bloquear el event loop durante la copia. Lee/escribe
    en chunks de 8 MB. Cada update del estado sucede al terminar cada chunk
    (~10-50 veces por segundo en disco rápido). El frontend hace polling cada
    1s, así que ve un progreso suave sin spam.

    Cancelación cooperativa: el thread chequea `_mkv_apply_cancel["requested"]`
    al inicio de cada chunk. Si está set, raise MkvApplyCancelled y la rutina
    superior limpia el destino parcial.

    Persistencia: el estado se persiste a /config/mkv_apply_state.json
    throttled a 1/s — sobrevive reinicios del cliente y permite recovery
    al arrancar el server (cleanup del .mkv parcial).
    """
    import time as _t
    total = src.stat().st_size
    _mkv_apply_state["total_bytes"] = total
    _mkv_apply_set_step("copying", f"Copiando MKV a /mnt/output ({total / 1e9:.1f} GB)…")

    CHUNK = 8 * 1024 * 1024  # 8 MB

    def _copy_thread():
        copied = 0
        last_persist = _t.monotonic()
        with src.open("rb") as fin, dst.open("wb") as fout:
            while True:
                if _mkv_apply_cancel["requested"]:
                    raise MkvApplyCancelled()
                buf = fin.read(CHUNK)
                if not buf:
                    break
                fout.write(buf)
                copied += len(buf)
                _mkv_apply_state["bytes_copied"] = copied
                _mkv_apply_state["pct"] = min(99, int(copied * 100 / total)) if total else 0
                started = _mkv_apply_state.get("started_at") or _t.monotonic()
                elapsed = max(0.001, _t.monotonic() - started)
                _mkv_apply_state["elapsed_s"] = int(elapsed)
                if copied > 0:
                    rate = copied / elapsed
                    remaining = total - copied
                    _mkv_apply_state["eta_s"] = int(remaining / rate) if rate > 0 else 0
                # Persistir cada 1s mientras copia. El frontend polea a 1s
                # también, así que es la granularidad útil. El polling REST
                # devuelve el state in-memory, no el persistido — la
                # persistencia es solo para survival ante crash.
                if _t.monotonic() - last_persist > 1.0:
                    _persist_mkv_apply_state()
                    last_persist = _t.monotonic()

    try:
        await asyncio.to_thread(_copy_thread)
    except MkvApplyCancelled:
        # Cancelación: borra destino parcial y propaga. La rutina superior
        # marca step="cancelled" en el estado para que el frontend cierre el
        # modal con el mensaje correcto en el siguiente poll.
        try:
            if dst.exists():
                dst.unlink()
        except Exception:
            pass
        raise
    except Exception as e:
        # Best-effort cleanup: borra destino parcial para no dejar basura
        try:
            if dst.exists():
                dst.unlink()
        except Exception:
            pass
        raise RuntimeError(f"Error copiando MKV: {e}") from e
    _mkv_apply_state["bytes_copied"] = total
    _mkv_apply_state["pct"] = 100


@app.get("/api/mkv/apply/progress", summary="Progreso de la operación apply (copia + edición)")
async def mkv_apply_progress():
    """Polling endpoint para el modal de aplicar cambios. El frontend lo
    consulta cada 1s mientras espera la respuesta del POST /api/mkv/apply."""
    return dict(_mkv_apply_state)


@app.post("/api/mkv/apply/cancel", summary="Cancela la copia en curso de un MKV de Library")
async def mkv_apply_cancel():
    """Solicita la cancelación cooperativa de la copia. El thread la detecta
    al inicio del siguiente chunk (típicamente <1s) y aborta limpiamente,
    borrando el destino parcial. No tiene efecto si el step actual no es
    'copying' (mkvpropedit es instantáneo, no hay nada útil que cancelar)."""
    if not _mkv_apply_state.get("active"):
        return {"ok": False, "reason": "no_active_job"}
    if _mkv_apply_state.get("step") != "copying":
        return {"ok": False, "reason": "not_in_copying_step"}
    _mkv_apply_cancel["requested"] = True
    return {"ok": True}


@app.post("/api/mkv/apply", summary="Aplica ediciones a un MKV")
async def apply_mkv_edits_endpoint(body: MkvEditRequest):
    """
    Aplica ediciones de metadatos a un MKV vía mkvpropedit (instantáneo).

    Soporta: nombres de pistas, flags default/forced, capítulos.

    Si el MKV está en /mnt/library (read-only), requiere `copy_to_output=true`:
    la app primero copia el fichero a /mnt/output (con monitoreo de progreso
    via /api/mkv/apply/progress) y aplica los cambios sobre la copia.
    """
    # ⚠️ DEV MODE — branch que devuelve fixtures sin tocar el filesystem
    if DEV_MODE:
        return build_fake_mkv_apply(body)
    src_path = Path(body.file_path)
    if not src_path.exists():
        raise HTTPException(status_code=400, detail="MKV no encontrado")

    # Detección de library read-only — exige confirmación explícita del usuario
    needs_copy = _mkv_needs_copy_to_output(body.file_path)
    if needs_copy and not body.copy_to_output:
        raise HTTPException(
            status_code=409,
            detail="MKV en biblioteca read-only — confirma `copy_to_output=true` "
                   "para copiarlo a /mnt/output antes de editar."
        )

    try:
        if needs_copy and body.copy_to_output:
            dst_path = OUTPUT_DIR_MKV / src_path.name
            if dst_path.exists():
                raise HTTPException(
                    status_code=409,
                    detail=f"Ya existe un MKV con ese nombre en /mnt/output: "
                           f"{src_path.name}. Renómbralo o muévelo antes de continuar."
                )
            OUTPUT_DIR_MKV.mkdir(parents=True, exist_ok=True)
            _mkv_apply_reset(
                total_bytes=src_path.stat().st_size,
                src_path=str(src_path),
                dst_path=str(dst_path),
                file_name=src_path.name,
            )
            try:
                await _mkv_copy_to_output_with_progress(src_path, dst_path)
                _mkv_apply_set_step("applying", "Aplicando cambios con mkvpropedit…")
                body.file_path = str(dst_path)
                result = await apply_mkv_edits(body)
                _mkv_apply_set_step("done", "Cambios aplicados correctamente")
                # mkvpropedit cambia mtime y posiblemente el primer 1MB del
                # MKV → cache previo (del source o del destino si existía)
                # debe quedar invalidado para que el próximo open re-analice.
                try:
                    from storage import invalidate_mkv_cache_by_path
                    invalidate_mkv_cache_by_path(str(dst_path))
                except Exception as e:
                    _logger.warning("invalidate_mkv_cache_by_path falló (no bloquea): %s", e)
                # Devolvemos el nuevo path para que el frontend actualice el state
                if isinstance(result, dict):
                    result["new_file_path"] = str(dst_path)
                    result["copied_from_library"] = True
                return result
            except MkvApplyCancelled:
                _mkv_apply_set_step("cancelled", "Copia cancelada por el usuario")
                raise HTTPException(
                    status_code=499,  # Client closed request
                    detail="Copia cancelada por el usuario antes de completar."
                )
            except HTTPException:
                raise
            except Exception as e:
                _mkv_apply_state["error"] = str(e)
                _mkv_apply_set_step("error", f"Error: {e}")
                raise
            finally:
                # Mantenemos active=True hasta done/error/cancelled → el
                # frontend cierra el modal en el siguiente poll. Limpiamos a
                # los 5s para que un poll tardío no se confunda con el
                # próximo job.
                async def _delayed_clear():
                    await asyncio.sleep(5)
                    _mkv_apply_state["active"] = False
                    _mkv_apply_cancel["requested"] = False
                    _persist_mkv_apply_state()
                asyncio.create_task(_delayed_clear())

        # Ruta directa (MKV en /mnt/output u otro root editable)
        result = await apply_mkv_edits(body)
        # mkvpropedit modifica el MKV in-place → invalidar cache para que
        # la próxima apertura desde Tab 2 re-analice y refleje los nuevos
        # metadatos (nombres de pistas, flags, capítulos).
        try:
            from storage import invalidate_mkv_cache_by_path
            invalidate_mkv_cache_by_path(body.file_path)
        except Exception as e:
            _logger.warning("invalidate_mkv_cache_by_path falló (no bloquea): %s", e)
        return result
    except HTTPException:
        raise
    except Exception as e:
        _logger.exception("Error aplicando ediciones a %s", body.file_path)
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 3 — CMv4.0 BD Pipeline endpoints
# ══════════════════════════════════════════════════════════════════════════════

import shutil as _cmv40_shutil
from pydantic import BaseModel
from models import CMv40Session, CMv40Phase, CMv40PhaseRecord, CMV40_PHASES_ORDER
from storage import (
    save_cmv40_session, load_cmv40_session, list_cmv40_sessions,
    delete_cmv40_session, make_cmv40_session_id,
)
from phases.cmv40_pipeline import (
    get_workdir as cmv40_get_workdir,
    list_available_rpus,
    run_phase_a_analyze_source, run_phase_b_target_from_path,
    run_phase_b_target_from_mkv, run_phase_b_target_from_drive,
    preflight_target_path, preflight_target_mkv, preflight_target_drive,
    run_phase_c_extract,
    run_phase_e_correct_sync, run_phase_f_inject,
    run_phase_g_remux, run_phase_h_validate,
    detect_sync_offset, compute_sync_confidence,
    validate_artifacts as _validate_cmv40_artifacts,
    cleanup_orphan_tmp as _cmv40_cleanup_orphan_tmp,
    CMV40_WORK_BASE,
)

# Conexiones WebSocket específicas de CMv4.0
_cmv40_ws_connections: dict[str, list[WebSocket]] = {}
_cmv40_active_procs: dict[str, asyncio.subprocess.Process] = {}
_cmv40_cancel_flags: dict[str, bool] = {}
# Locks por sesión para serializar la regeneración on-demand de per_frame_data.json
# (evita N procesos `dovi_tool export` concurrentes cuando el frontend dispara
# fetches a sync-data en paralelo durante transiciones del auto-pipeline).
_cmv40_perframe_locks: dict[str, asyncio.Lock] = {}


async def _dev_simulate_phase(session: CMv40Session, phase_name: str,
                              log_lines: list[str], new_phase: str,
                              apply_fn=None, total_seconds: float = 3.0,
                              progress_label: str = "") -> None:
    """
    Simula una fase en DEV mode emitiendo log_lines con delays y progreso sintético.
    Al final aplica apply_fn(session) y avanza a new_phase.
    """
    import json as _json
    session.running_phase = phase_name
    session.error_message = ""
    save_cmv40_session(session)
    label = progress_label or phase_name
    try:
        n = max(1, len(log_lines))
        delay_per = total_seconds / n
        await _cmv40_log(session, f"§§PROGRESS§§{_json.dumps({'pct': 0, 'label': label, 'eta_s': int(total_seconds)})}")
        for i, line in enumerate(log_lines):
            if _cmv40_cancel_flags.get(session.id):
                await _cmv40_log(session, "🛑 Cancelado por el usuario")
                return
            await _cmv40_log(session, line)
            await asyncio.sleep(delay_per)
            pct = round(((i + 1) / n) * 100, 1)
            eta = max(0, int(total_seconds - delay_per * (i + 1)))
            await _cmv40_log(session, f"§§PROGRESS§§{_json.dumps({'pct': pct, 'label': label, 'eta_s': eta})}")
        if apply_fn:
            apply_fn(session)
        session.phase = new_phase
        await _cmv40_log(session, f"§§PROGRESS§§{_json.dumps({'pct': 100, 'label': 'Completado', 'eta_s': 0})}")
        await _cmv40_log(session, f"✓ Fase {phase_name} completada")
    finally:
        session.running_phase = None
        _cmv40_cancel_flags.pop(session.id, None)
        save_cmv40_session(session)


async def _cmv40_log(session: CMv40Session, msg: str) -> None:
    """Añade un log a la sesión CMv4.0, lo persiste con throttling y lo
    emite por WebSocket inmediatamente.

    Throttling + non-blocking I/O: en jobs intensos (CMv4.0 con UHD 50+ GB)
    `_cmv40_log` se llama miles de veces. Cada save reescribe el JSON
    completo (cientos de KB con miles de líneas). Sin cuidado, esto:
      a) sería ~1 GB+ de I/O por job (mata throughput del NAS)
      b) bloquearía el event loop varios segundos durante contención del
         NAS (los fetchs REST y los `ws.send_text` se quedan colgados)

    Estrategia:
      - Throttle: marcadores clave (cambios de fase, errores) → save
        inmediato; output ruidoso de ffmpeg/dovi_tool → save throttled
        a 2s o 25 líneas. Garantía: máximo 2s de pérdida ante kill -9.
      - I/O en thread (asyncio.to_thread): aunque el throttle dispare un
        save, no bloqueamos el event loop esperando al disco. Mientras el
        thread escribe, el endpoint /api/cmv40/{id} sigue respondiendo y
        el WS sigue entregando líneas. Sin esto, durante I/O intensivo
        del NAS las líneas que llegaban del subprocess se acumulaban en
        buffer y se entregaban en burst tras varios segundos.

    El WebSocket SÍ se notifica inmediatamente — el frontend ve el log
    en tiempo real aunque la persistencia esté throttled o async.
    """
    # Timestamp en hora local del contenedor (TZ env, ej: Europe/Madrid)
    ts_msg = f"[{datetime.now().astimezone().strftime('%H:%M:%S')}] {msg}"
    session.output_log.append(ts_msg)
    await _cmv40_maybe_persist_log(session, ts_msg)
    # Broadcast a clientes WS — en TASKS PARALELOS con timeout corto.
    #
    # CRÍTICO: NO usar `await ws.send_text(...)` directo en este loop.
    # Si el cliente está zombie (Mac dormido, red caída), el TCP send
    # buffer se llena y el `send_text` puede bloquear minutos hasta que
    # el kernel detecte el timeout TCP. Ese bloqueo congelaba el event
    # loop entero: ffmpeg seguía emitiendo líneas pero el subprocess
    # reader de `_run_streaming` no las leía → buffer del pipe se llena
    # → ffmpeg se bloquea en write → gap de log visible al usuario.
    #
    # Solución: cada send es una task aislada con `asyncio.wait_for(
    # timeout=2)`. Si el cliente no responde en 2s, lo desconectamos y
    # eliminamos de la lista — el `ws.onclose` del frontend hará el
    # reconnect cuando vuelva a estar visible. Mientras tanto, el log
    # sigue fluyendo a otros clientes y al disco sin atascos.
    ws_list = list(_cmv40_ws_connections.get(session.id, []))
    if ws_list:
        sid = session.id
        for ws in ws_list:
            asyncio.create_task(_cmv40_send_with_timeout(sid, ws, ts_msg))


async def _cmv40_send_with_timeout(sid: str, ws, msg: str) -> None:
    """Envía un mensaje a un WebSocket con timeout corto. Si el send tarda
    más de 2s asumimos zombie: cerramos el ws y lo quitamos de la lista
    para no volver a intentarlo (el frontend reconectará al detectar el
    cierre). Esto evita que un cliente lento congele el log para todos."""
    try:
        await asyncio.wait_for(ws.send_text(msg), timeout=2.0)
    except (asyncio.TimeoutError, Exception):
        try:
            await ws.close()
        except Exception:
            pass
        try:
            _cmv40_ws_connections.get(sid, []).remove(ws)
        except ValueError:
            pass


# Estado del throttle por sesión: { session_id: {last_save_ts: monotonic, lines_since: int} }
_cmv40_log_throttle: dict[str, dict] = {}

# Lock por sesión para serializar saves concurrentes — sin esto dos saves
# en paralelo pueden corromper el JSON (ambos escriben al mismo .tmp y
# el rename ganador sobrescribe). El asyncio.Lock garantiza ejecución
# secuencial sin bloquear el event loop (otras corutinas siguen).
_cmv40_save_locks: dict[str, asyncio.Lock] = {}


def _get_cmv40_save_lock(sid: str) -> asyncio.Lock:
    """Lock dedicado por sesión. Singleton lazy."""
    if sid not in _cmv40_save_locks:
        _cmv40_save_locks[sid] = asyncio.Lock()
    return _cmv40_save_locks[sid]


# Marcadores que fuerzan persistencia inmediata (no se pueden perder).
#
# CRÍTICO: deben ser strings que SOLO emitamos NOSOTROS, nunca ffmpeg ni
# dovi_tool. Si un patrón es demasiado genérico (ej. "Error", "FALLÓ"),
# cualquier línea de stderr de ffmpeg con esa palabra dispararía un save
# AWAIT bloqueante. Con NAS lento, ese save bloquea el reader del
# subprocess durante segundos → pipe lleno → ffmpeg detenido → gap visible.
#
# Por eso TODOS los markers ahora llevan emoji o frase única:
#   - "━━━" no aparece nunca en stdout/stderr de ffmpeg/dovi_tool
#   - "✓ Fase" / "✗ Fase" — "Fase" en español, ffmpeg habla inglés
#   - Resto: emoji distintivos
_CMV40_LOG_FORCE_PERSIST_MARKERS = (
    "━━━",          # separador inicio/fin de fase
    "✓ Fase",       # fase completada (cubre tambien "✗ Fase ... FALLO")
    "✗ Fase",       # fase fallida
    "🎯 Resultado",
    "📋 Plan",
    "🛑 Cancelado",  # con emoji para evitar match accidental
    "ℹ️ Auto",       # auto-rewind
    "ℹ️ Forward",    # forward-roll
)


# Reexporta el helper centralizado de storage. Mantenemos el nombre
# `_save_cmv40_session_async` por compatibilidad con tests existentes.
from storage import save_cmv40_session_async as _save_cmv40_session_async  # noqa: E402


async def _cmv40_maybe_persist_log(session: CMv40Session, line: str) -> None:
    """Decide si persistir ahora o postponer.

    Diseño FIRE-AND-FORGET para TODOS los triggers (markers + ruidosos):

    Antes los markers hacían `await save` bloqueante para garantizar
    durabilidad. PROBLEMA: si había un bg_save en curso (NAS lento), el
    marker esperaba al lock que tardaba segundos. Mientras tanto el
    reader del subprocess (`_run_streaming`) estaba bloqueado en
    `await log_callback(...)` → ffmpeg llenaba el pipe → ffmpeg se
    detenía → gap visible al usuario.

    La durabilidad la garantiza `_cmv40_flush_log` que se invoca con
    `await` al terminar cada fase desde `_run_cmv40_phase`. Entre saves,
    las líneas viven en `session.output_log` RAM y el WS las entrega en
    vivo al cliente.

    Reglas:
      1. Marker o >1s o >=20 líneas → trigger
      2. Si lock libre → lanza task background, retorna inmediato (ms)
      3. Si lock ocupado → descarta trigger, la línea queda en RAM y el
         próximo trigger la captura
    """
    import time as _t
    sid = session.id
    state = _cmv40_log_throttle.setdefault(sid, {"last_save_ts": 0.0, "lines_since": 0})
    state["lines_since"] += 1
    is_marker = any(m in line for m in _CMV40_LOG_FORCE_PERSIST_MARKERS)
    elapsed = _t.monotonic() - state["last_save_ts"]
    should_save = is_marker or elapsed > 1.0 or state["lines_since"] >= 20
    if not should_save:
        return

    lock = _get_cmv40_save_lock(sid)
    if lock.locked():
        # Save en curso. NO esperamos — la línea está en session.output_log
        # RAM y el siguiente trigger la persistirá cuando el lock se libere.
        # Esto es la diferencia crítica vs el código anterior: NUNCA
        # bloqueamos el reader del subprocess esperando al disco.
        return

    state["last_save_ts"] = _t.monotonic()
    state["lines_since"] = 0

    async def _bg_save():
        try:
            async with lock:
                await _save_cmv40_session_async(session)
        except Exception as e:
            _logger.warning("[cmv40 throttled save] sid=%s error: %s", sid, e)

    asyncio.create_task(_bg_save())


async def _cmv40_flush_log(session: CMv40Session) -> None:
    """Fuerza un save inmediato ignorando el throttle. Llamado al
    completar cada fase. Espera al lock (saves previos terminan), luego
    hace su propio save bajo el mismo lock — al volver de esta función,
    `session.output_log` está garantizado en disco.
    """
    import time as _t
    sid = session.id
    state = _cmv40_log_throttle.setdefault(sid, {"last_save_ts": 0.0, "lines_since": 0})
    lock = _get_cmv40_save_lock(sid)
    async with lock:
        await _save_cmv40_session_async(session)
        state["last_save_ts"] = _t.monotonic()
        state["lines_since"] = 0


def _cmv40_proc_register(session_id: str, proc: asyncio.subprocess.Process) -> None:
    """Registra un subprocess activo para permitir cancelación."""
    _cmv40_active_procs[session_id] = proc


def _check_cmv40_cancel(session_id: str) -> None:
    if _cmv40_cancel_flags.get(session_id):
        raise RuntimeError("Cancelado por el usuario")


# Lock por sesión para evitar ejecuciones concurrentes de la misma fase
# (protege contra race conditions en el auto-pipeline cuando el frontend
# dispara la misma transición dos veces antes de que running_phase se
# haya persistido en disco).
_cmv40_phase_locks: dict[str, asyncio.Lock] = {}


def _get_cmv40_phase_lock(session_id: str) -> asyncio.Lock:
    if session_id not in _cmv40_phase_locks:
        _cmv40_phase_locks[session_id] = asyncio.Lock()
    return _cmv40_phase_locks[session_id]


async def _run_cmv40_phase(
    session: CMv40Session,
    phase_name: str,
    coro_factory,
    new_phase: str,
) -> None:
    """
    Wrapper para ejecutar una fase CMv4.0: registra inicio/fin en phase_history,
    captura errores, actualiza el estado de la sesión.

    Está protegido por un asyncio.Lock por session_id — si una fase ya está
    corriendo para esa sesión, cualquier invocación concurrente se ignora
    silenciosamente (evita doble-fire del auto-pipeline que escribía a los
    mismos artefactos y fallaba al final intentando renombrar .mkv.tmp).

    coro_factory: función que recibe (log_callback, proc_callback) y devuelve coroutine.
    """
    lock = _get_cmv40_phase_lock(session.id)
    if lock.locked():
        _logger.info(
            "Fase %s ignorada para sesión %s: ya hay una fase en curso (lock)",
            phase_name, session.id,
        )
        # Si el intento es de la MISMA fase ya en curso, skip silente: el
        # frontend re-dispara la siguiente fase en cada WS update (polling
        # del safety + WS message del log), y todos los intentos durante
        # una fase larga (extract-rpu de Fase H sobre 60+ GB) llegan aquí.
        # Loguear cada intento llenaba el log con docenas de líneas de ruido.
        # Solo logueamos si la fase intentada es DISTINTA de la que corre
        # (caso anómalo — race de orquestadores).
        fresh = load_cmv40_session(session.id)
        running = (fresh.running_phase if fresh else "") or ""
        if running and running != phase_name:
            await _cmv40_log(
                session,
                f"⏭ Fase {phase_name} ignorada — ya hay otra fase ({running}) en curso para este proyecto"
            )
        return
    async with lock:
        started = datetime.now(timezone.utc)
        previous_phase = session.phase
        record = CMv40PhaseRecord(phase=phase_name, started_at=started, status="running")
        session.phase_history.append(record)
        session.running_phase = phase_name  # ← bloquea la UI en modo modal
        save_cmv40_session(session)

        async def _log_cb(msg: str):
            await _cmv40_log(session, msg)

        def _proc_cb(proc):
            _cmv40_proc_register(session.id, proc)

        try:
            await _cmv40_log(session, f"━━━ Inicio fase: {phase_name} ━━━")
            await coro_factory(_log_cb, _proc_cb)

            record.status = "done"
            record.finished_at = datetime.now(timezone.utc)
            record.elapsed_seconds = (record.finished_at - started).total_seconds()
            session.phase = new_phase
            session.error_message = ""
            # Si el pipeline alcanza DONE sin output_workflow ya marcado
            # (caso Keep ya lo puso desde accept-keep), distinguimos en el
            # historial entre los dos tipos de restore — drop-in (rápido,
            # RPU del bin sustituido íntegro) vs merge (frame-a-frame con
            # [3,8,9,11,254]). El marker "merge_cmv40_transfer" en
            # phases_skipped lo deja Fase F cuando hace drop-in.
            if new_phase == CMv40Phase.DONE and not session.output_workflow:
                session.output_workflow = (
                    "restore_dropin"
                    if "merge_cmv40_transfer" in (session.phases_skipped or [])
                    else "restore_merge"
                )
            await _cmv40_log(session, f"✓ Fase {phase_name} completada en {record.elapsed_seconds:.1f}s")
        except Exception as e:
            record.status = "error"
            record.finished_at = datetime.now(timezone.utc)
            record.error_message = str(e)
            session.phase = previous_phase
            session.error_message = str(e)
            await _cmv40_log(session, f"✗ Fase {phase_name} FALLÓ: {e}")
            # Fase G (remux) fallando → .mkv.tmp puede estar parcial/corrupto →
            # borrar es correcto. Fase H (validate) fallando → la mux ya
            # terminó ok, el MKV está completo; la validación es solo sanity
            # check. Preservamos el .mkv.tmp para que el usuario pueda
            # inspeccionarlo o renombrarlo manualmente sin perder 40+ GB.
            if phase_name == "remux":
                freed = _cmv40_cleanup_orphan_tmp(session)
                if freed > 0:
                    await _cmv40_log(
                        session,
                        f"🧹 Borrado .mkv.tmp huérfano ({freed / 1e9:.2f} GB liberados)"
                    )
            elif phase_name == "validate":
                from phases.cmv40_pipeline import OUTPUT_DIR as _OUT_DIR
                tmp_path = _OUT_DIR / f"{session.output_mkv_name}.tmp"
                if tmp_path.exists():
                    size_gb = tmp_path.stat().st_size / 1e9
                    await _cmv40_log(
                        session,
                        f"ℹ️ .mkv.tmp preservado ({size_gb:.2f} GB) — la mux de Fase G "
                        f"terminó ok, solo falló la validación. Inspecciona o renombra "
                        f"manualmente: mv '{tmp_path.name}' '{session.output_mkv_name}'"
                    )
        finally:
            _cmv40_active_procs.pop(session.id, None)
            session.running_phase = None  # ← desbloquea la UI
            # Flush garantizado del log al terminar fase: cualquier línea
            # que el throttle hubiera dejado en buffer se vuelca a disco
            # ANTES del save final del estado. Sin esto, las últimas N
            # líneas del log podrían perderse si el server cae justo aquí.
            await _cmv40_flush_log(session)
            save_cmv40_session(session)

    # ── AUTO-PIPELINE BACKEND-DRIVEN ──────────────────────────────────
    # Tras completar una fase con éxito (sin error_message poblado en
    # el except), si session.auto_pipeline=True el backend dispara
    # automáticamente la siguiente fase SIN depender del frontend. Esto
    # hace el pipeline resiliente al estado del cliente: Mac dormido,
    # pestaña cerrada, navegador crashado — el job avanza solo hasta
    # done. El orquestador respeta pausas legítimas (Fase D manual si no
    # trusted, awaiting_critical_ack, error, archived).
    if (session.auto_pipeline
            and not session.error_message
            and session.phase != previous_phase):  # solo si avanzó (no error)
        asyncio.create_task(_cmv40_dispatch_next_phase(session.id))


async def _cmv40_dispatch_next_phase(session_id: str) -> None:
    """Orquestador del auto-pipeline backend-driven.

    Invocado tras cada fase exitosa si session.auto_pipeline=True. Determina
    la siguiente acción según `session.phase` y la dispara como task asyncio.
    Re-loadea la sesión desde disco para tener el estado más fresco (otra
    coroutine puede haber modificado entre el finally y este dispatch).

    Pausas legítimas (NO dispara, queda esperando acción manual del usuario):
      - error_message poblado o archived
      - awaiting_critical_ack=True (gates degradados pendientes de ACK)
      - phase='extracted' y target NO trusted (Fase D manual visual)
      - running_phase != null (otra fase ya en marcha — no doble-fire)
      - phase='source_analyzed' (Fase B requiere acción manual del user)

    Transiciones automáticas (dispara siguiente fase):
      - target_provided  → Fase C (extract)
      - extracted (trusted o user_acked) → marca sync_verified + Fase F (recursivo)
      - sync_verified    → Fase F (inject)
      - sync_corrected   → Fase F (inject) — tras correctión manual de Fase E
      - injected         → Fase G (remux)
      - remuxed          → Fase H (validate)
      - validated/done   → terminal, no hace nada
    """
    fresh = load_cmv40_session(session_id)
    if not fresh:
        return
    if not fresh.auto_pipeline:
        return
    if fresh.error_message or fresh.archived:
        return
    if fresh.awaiting_critical_ack:
        return
    if fresh.running_phase:
        return  # otra fase ya corriendo, no doble-fire

    phase = fresh.phase

    if phase == CMv40Phase.CREATED:
        # Recién creado. Si tiene pending_target, ejecutar preflight; si
        # ya pasó preflight (target_preflight_ok=True), arrancar Fase A.
        # Si el preflight ya emitió una decisión NO-OK (keep_l8_default,
        # keep_no_l8, abort_no_cmv40), respetar y NO re-disparar — la
        # decisión queda esperando acción del usuario (aceptar Keep o
        # forzar Restore).
        if (fresh.preflight_decision and fresh.preflight_decision != "ok"):
            return
        if not fresh.target_preflight_ok and fresh.pending_target_kind:
            await _cmv40_dispatch_preflight(fresh)
        elif fresh.target_preflight_ok or not fresh.pending_target_kind:
            # Sin pending target O ya con preflight: arrancar Fase A.
            # (el caso "sin pending target" pasaría si el usuario creó el
            # proyecto sin elegir target — Fase A puede correr igualmente
            # y luego Fase B requiere acción manual.)
            await _cmv40_dispatch_analyze_source(fresh)
    elif phase == CMv40Phase.SOURCE_ANALYZED:
        # Fase A completada. Si hay pending_target persistido, dispara Fase B.
        # Si NO hay pending_target, pausa — Fase B requiere acción manual
        # del usuario (escoger target en el panel).
        if fresh.pending_target_kind:
            await _cmv40_dispatch_target_provision(fresh)
    elif phase == CMv40Phase.TARGET_PROVIDED:
        await _cmv40_dispatch_extract(fresh)
    elif phase == CMv40Phase.EXTRACTED:
        # Trusted target → auto mark-synced → seguir cadena
        trusted_auto = (
            fresh.target_trust_ok
            and fresh.trust_override != "force_interactive"
        )
        user_acked = fresh.user_acknowledged_degradation
        if trusted_auto or user_acked:
            fresh.phase = CMv40Phase.SYNC_VERIFIED
            if "sync_verification_pause" not in fresh.phases_skipped:
                fresh.phases_skipped.append("sync_verification_pause")
            save_cmv40_session(fresh)
            await _cmv40_log(
                fresh,
                "🤖 Auto: target trusted — sync verification omitida, "
                "avanzando directo a Fase F."
            )
            # Recursivo: ahora phase=sync_verified, lanzar Fase F
            await _cmv40_dispatch_next_phase(session_id)
        # else: pausa manual Fase D (no trusted) — espera acción del usuario
    elif phase in (CMv40Phase.SYNC_VERIFIED, CMv40Phase.SYNC_CORRECTED):
        await _cmv40_dispatch_inject(fresh)
    elif phase == CMv40Phase.INJECTED:
        await _cmv40_dispatch_remux(fresh)
    elif phase == CMv40Phase.REMUXED:
        await _cmv40_dispatch_validate(fresh)
    # phase == VALIDATED / DONE → terminal, no hace nada
    # phase == SOURCE_ANALYZED → requiere target manual, no avanzamos
    # phase == CREATED → requiere target/source — flow normal Fase A
    #   ya disparado por endpoint de creación


async def _cmv40_dispatch_extract(session: CMv40Session) -> None:
    """Dispara Fase C como task asyncio."""
    from phases.cmv40_pipeline import run_phase_c_extract

    async def _coro(log_cb, proc_cb):
        await run_phase_c_extract(session, log_cb, proc_cb)

    async def _run():
        try:
            await _run_cmv40_phase(session, "extract", _coro, CMv40Phase.EXTRACTED)
        except Exception:
            pass

    asyncio.create_task(_run())


async def _cmv40_dispatch_inject(session: CMv40Session) -> None:
    """Dispara Fase F como task asyncio."""
    from phases.cmv40_pipeline import run_phase_f_inject
    _cmv40_cancel_flags.pop(session.id, None)

    async def _coro(log_cb, proc_cb):
        await run_phase_f_inject(session, log_cb, proc_cb)

    async def _run():
        try:
            await _run_cmv40_phase(session, "inject", _coro, CMv40Phase.INJECTED)
        except Exception:
            pass

    asyncio.create_task(_run())


async def _cmv40_dispatch_remux(session: CMv40Session) -> None:
    """Dispara Fase G como task asyncio."""
    from phases.cmv40_pipeline import run_phase_g_remux
    _cmv40_cancel_flags.pop(session.id, None)

    async def _coro(log_cb, proc_cb):
        await run_phase_g_remux(session, log_cb, proc_cb)

    async def _run():
        try:
            await _run_cmv40_phase(session, "remux", _coro, CMv40Phase.REMUXED)
        except Exception:
            pass

    asyncio.create_task(_run())


async def _cmv40_dispatch_validate(session: CMv40Session) -> None:
    """Dispara Fase H como task asyncio."""
    from phases.cmv40_pipeline import run_phase_h_validate
    _cmv40_cancel_flags.pop(session.id, None)

    async def _coro(log_cb, proc_cb):
        result = await run_phase_h_validate(session, log_cb, proc_cb)
        session.output_log.append(f"Validación final: {result}")

    async def _run():
        try:
            await _run_cmv40_phase(session, "validate", _coro, CMv40Phase.DONE)
        except Exception:
            pass

    asyncio.create_task(_run())


async def _cmv40_dispatch_analyze_source(session: CMv40Session) -> None:
    """Dispara Fase A (analyze_source) como task asyncio."""
    from phases.cmv40_pipeline import run_phase_a_analyze_source
    _cmv40_cancel_flags.pop(session.id, None)

    async def _coro(log_cb, proc_cb):
        await run_phase_a_analyze_source(session, log_cb, proc_cb)

    async def _run():
        try:
            await _run_cmv40_phase(session, "analyze_source", _coro, CMv40Phase.SOURCE_ANALYZED)
        except Exception:
            pass

    asyncio.create_task(_run())


async def _cmv40_preflight_analyze_target(session: CMv40Session, log_cb) -> bool:
    """Análisis profundo del bin target (L2/L8 combos) + decisión Keep/continuar.

    Llamado al final del preflight tras descargar/extraer el bin. Devuelve:
      - True  → bin con L8 trabajado o ambiguo. Continuar pipeline (Fase A).
      - False → bin sintético/default. Recomendar Keep. NO avanzar.

    En el caso False, también puebla session.preflight_decision y
    session.preflight_message para que el frontend muestre el motivo.
    Si el análisis no se puede completar (dovi_tool falla), devuelve True
    para no bloquear el pipeline — la decisión cae al modelo legacy.

    Bloque 1 del modelo Keep/Drop-in/Merge.
    """
    from phases.rpu_analyze import (
        analyze_rpu_combos, classify_l8, classify_l8_quality,
    )

    wd = cmv40_get_workdir(session)
    target_bin = wd / "RPU_target.bin"
    await log_cb("[Pre-flight] Analizando combos L2/L8 del bin (dovi_tool export)…")
    analysis = await analyze_rpu_combos(target_bin)

    if analysis.total_frames == 0:
        await log_cb(
            "[Pre-flight] ⚠ Análisis de combos no pudo completarse "
            "(continuamos sin enriquecimiento)"
        )
        return True

    # Persistir todos los datos del bin en la session
    session.target_l2_combos = analysis.l2_combos
    session.target_l2_unique_count = analysis.l2_unique_count
    session.target_l2_target_pqs = analysis.l2_target_pqs
    session.target_l8_combos = analysis.l8_combos
    session.target_l8_unique_count = analysis.l8_unique_count
    session.target_l8_target_indices = analysis.l8_target_indices
    session.target_l8_neutral_frames_pct = analysis.l8_neutral_pct
    session.target_l8_has_mid_contrast = analysis.l8_has_mid_contrast
    session.target_l8_has_clip_trim = analysis.l8_has_clip_trim
    session.target_l8_scene_cuts = analysis.scene_cuts
    session.target_frames_analyzed = analysis.total_frames

    classification, reason = classify_l8(analysis)
    session.target_l8_classification = classification

    await log_cb(
        f"[Pre-flight] L2: {analysis.l2_unique_count} combos únicos · "
        f"L8: {analysis.l8_unique_count} combos únicos, "
        f"{(1.0 - analysis.l8_neutral_pct) * 100:.0f}% frames con trim · "
        f"clasificación: {classification.upper()}"
    )

    # Veredicto visual con emoji por clasificación + el reason calculado por
    # classify_l8 (incluye el por qué de la decisión). Permite al usuario
    # entender la recomendación final sin abrir la card "Análisis y
    # recomendación". El caso "default" emite su 🛑 propio más abajo.
    if classification == "real":
        await log_cb(f"[Pre-flight] 🟢 L8 real — {reason}")
    elif classification == "indeterminate":
        await log_cb(
            f"[Pre-flight] 🟡 L8 ambiguo — {reason} El pipeline avanza "
            f"igualmente; la decisión Mantener/Inyectar se afinará tras "
            f"analizar el L2 del source en Fase A."
        )

    if classification == "default":
        # Recomendación firme: mantener MKV actual. No avanzar.
        session.preflight_decision = "keep_l8_default"
        session.preflight_message = reason
        session.target_preflight_ok = False
        # Persistir la recomendación del modelo (modelo Bloque 2)
        from phases.rpu_analyze import recommend_action
        action, action_label, action_reason = recommend_action(session)
        session.recommended_action = action
        session.recommended_action_label = action_label
        session.recommended_action_reason = action_reason
        await log_cb(
            f"🛑 Pre-flight: el bin no tiene un L8 trabajado real. {reason} "
            f"Recomendación: mantener el MKV actual (no procesar). Un "
            f"reproductor compatible con CMv4.0 (p3i T4 / avdvplus / Sony / "
            f"LG modernos) hará la conversión al vuelo con el mismo resultado "
            f"visible que tendría inyectar este RPU."
        )
        return False

    # Sub-clasificación de calidad (CORE / CORE+ / FULL) para "real" e
    # "indeterminate" (en indeterminate solo es informativo, pero se calcula
    # igual por si avanzamos). Solo poblamos si tier != "" (i.e. real).
    tier, label, description = classify_l8_quality(analysis)
    if tier:
        session.target_l8_quality_tier = tier
        session.target_l8_quality_label = label
        session.target_l8_quality_description = description
        await log_cb(f"[Pre-flight] 🎯 Calidad del bin: {label} — {description}")
        # Actualizar output_mkv_name con el label correcto si está en formato
        # auto (contiene "[CMv4.0]" o "[CMv4 XXX]"). No tocamos si el usuario
        # lo editó a algo personalizado.
        _cmv40_apply_quality_label_to_output_name(session, label)

    return True


def _cmv40_apply_quality_label_to_output_name(session: CMv40Session, new_label: str) -> None:
    """Sustituye el `[CMv4...]` del output_mkv_name por `[<new_label>]`.

    Patrones reconocidos como "formato auto" (editables sin avisar al usuario):
      - `[CMv4.0]`
      - `[CMv4 CORE]`, `[CMv4 CORE+]`, `[CMv4 FULL]`, `[CMv4 MINIMAL]`

    Si el usuario lo cambió a algo distinto (ej. `[Hybrid DV4]` o sin
    bracket alguno), NO se toca. La idea: mantener el label coherente con
    la calidad detectada cuando el usuario no se ha metido a renombrar.
    """
    import re
    if not session.output_mkv_name or not new_label:
        return
    pattern = r"\[CMv4(?:\.0| (?:CORE\+?|FULL|MINIMAL))\]"
    new_token = f"[{new_label}]"
    new_name = re.sub(pattern, new_token, session.output_mkv_name)
    if new_name != session.output_mkv_name:
        session.output_mkv_name = new_name


async def _cmv40_dispatch_preflight(session: CMv40Session) -> None:
    """Dispara el preflight del bin target persistido en pending_target_*.
    Tras éxito, el orquestador (en finally) detecta target_preflight_ok=True
    y dispara Fase A automáticamente."""
    from phases.cmv40_pipeline import (
        preflight_source, preflight_target_drive,
        preflight_target_path, preflight_target_mkv,
    )
    if not session.pending_target_kind:
        return  # nada que validar

    # Guard contra re-disparo (defensa en profundidad — el llamador
    # _cmv40_dispatch_next_phase ya lo chequea, pero protegemos por si en el
    # futuro otro caller invoca este helper directamente).
    if session.preflight_decision and session.preflight_decision != "ok":
        return

    # Lock por sesión para no doble-fire
    lock = _get_cmv40_phase_lock(session.id)
    if lock.locked():
        return

    _cmv40_cancel_flags.pop(session.id, None)

    async def _run():
        async with lock:
            session.running_phase = "preflight"
            session.error_message = ""
            session.target_preflight_ok = False
            save_cmv40_session(session)
            await _cmv40_log(session, "━━━ Inicio fase: preflight ━━━")

            async def _log_cb(msg: str):
                await _cmv40_log(session, msg)

            def _proc_cb(proc):
                _cmv40_proc_register(session.id, proc)

            try:
                await preflight_source(session, log_callback=_log_cb, proc_callback=_proc_cb)

                kind = session.pending_target_kind
                if kind == "drive" or kind == "repo":
                    await preflight_target_drive(
                        session,
                        session.pending_target_file_id,
                        session.pending_target_file_name,
                        _log_cb,
                    )
                elif kind == "path":
                    await preflight_target_path(
                        session, session.pending_target_rpu_path, _log_cb,
                    )
                elif kind == "mkv":
                    await preflight_target_mkv(
                        session, session.pending_target_source_mkv_path,
                        _log_cb, _proc_cb,
                    )
                # Análisis profundo del bin + decisión Keep/continuar
                avanzar = await _cmv40_preflight_analyze_target(session, _log_cb)
                if avanzar:
                    session.preflight_decision = "ok"
                    session.preflight_message = ""
                    session.target_preflight_ok = True
                    # El cierre canónico de fase no anuncia la siguiente — si
                    # auto_pipeline=True el dispatcher emitirá su propio
                    # ━━━ Inicio fase: analyze_source ━━━; si está desactivado,
                    # el usuario decide cuándo lanzar Fase A.
                    next_hint = (
                        " — auto-pipeline encadenará Fase A a continuación."
                        if session.auto_pipeline
                        else " — auto-pipeline desactivado: pulsa ▶ para lanzar Fase A."
                    )
                    await _cmv40_log(
                        session,
                        f"✓ Fase preflight completada — origen y bin validos.{next_hint}"
                    )
                # Si NO avanzar, la helper ya pobló preflight_decision/message
            except Exception as e:
                msg = str(e)
                await _cmv40_log(session, f"✗ Fase preflight FALLÓ: {msg}")
                session.error_message = msg
                session.target_preflight_ok = False
            finally:
                _cmv40_active_procs.pop(session.id, None)
                _cmv40_cancel_flags.pop(session.id, None)
                session.running_phase = None
                save_cmv40_session(session)
        # Tras finally, si auto_pipeline + preflight OK + no error → orquestar
        # siguiente: en este caso CREATED → dispatch llevará a Fase A porque
        # target_preflight_ok=True ahora.
        if session.auto_pipeline and not session.error_message:
            asyncio.create_task(_cmv40_dispatch_next_phase(session.id))

    asyncio.create_task(_run())


async def _cmv40_dispatch_target_provision(session: CMv40Session) -> None:
    """Dispara Fase B usando el pending_target persistido. Tras éxito, el
    orquestador detecta phase=TARGET_PROVIDED y dispara Fase C."""
    from phases.cmv40_pipeline import (
        run_phase_b_target_from_path, run_phase_b_target_from_drive,
        run_phase_b_target_from_mkv,
    )
    kind = session.pending_target_kind
    if not kind:
        return

    if kind == "path":
        rpu_path = session.pending_target_rpu_path
        async def _coro(log_cb, proc_cb):
            await run_phase_b_target_from_path(session, rpu_path, log_cb)
        phase_name = "target_rpu_path"
    elif kind == "drive" or kind == "repo":
        file_id = session.pending_target_file_id
        file_name = session.pending_target_file_name
        async def _coro(log_cb, proc_cb):
            await run_phase_b_target_from_drive(session, file_id, file_name, log_cb)
        phase_name = "target_rpu_drive"
    elif kind == "mkv":
        mkv_path = session.pending_target_source_mkv_path
        async def _coro(log_cb, proc_cb):
            await run_phase_b_target_from_mkv(session, mkv_path, log_cb, proc_cb)
        phase_name = "target_rpu_mkv"
    else:
        await _cmv40_log(session, f"⚠ pending_target_kind desconocido: {kind!r}")
        return

    async def _run():
        try:
            await _run_cmv40_phase(session, phase_name, _coro, CMv40Phase.TARGET_PROVIDED)
        except Exception:
            pass

    asyncio.create_task(_run())


# ── Endpoints CRUD ────────────────────────────────────────────────────────────

class CMv40PendingTargetSpec(BaseModel):
    """Target seleccionado en el modal de creación. Persistido en
    session.pending_target_* para que el orquestador backend pueda disparar
    Fase B automáticamente tras Fase A sin depender del frontend."""
    kind: str
    rpu_path: str = ""
    file_id: str = ""
    file_name: str = ""
    source_mkv_path: str = ""


class CMv40CreateRequest(BaseModel):
    source_mkv_path: str
    output_mkv_name: str | None = None
    auto_pipeline: bool = False
    """Si True, el backend encadena fases automáticamente sin esperar al
    frontend. Hace el job resiliente al estado del cliente (Mac sleep,
    pestaña cerrada). Frontend lo activa al crear con auto-mode on."""
    pending_target: CMv40PendingTargetSpec | None = None
    """Target seleccionado en el modal. Persistido en la sesión para que
    el orquestador backend pueda continuar el pipeline (preflight + Fase B)
    aunque el cliente desaparezca."""


class CMv40AutoPipelineRequest(BaseModel):
    enabled: bool


@app.get("/api/cmv40", summary="Lista proyectos CMv4.0 (sidebar — sin output_log/phase_history)")
async def list_cmv40():
    """Devuelve metadatos de todas las sesiones para alimentar el sidebar.

    Excluye `output_log` y `phase_history` por tamaño (cada sesión puede
    tener MBs de log tras un job largo). Bajo carga I/O del NAS, devolver
    todo causaba timeouts del sidebar de >30s. Para el detalle completo
    (incluido el log) usar GET /api/cmv40/{id}.
    """
    from storage import list_cmv40_sessions_summary
    # I/O bound: lectura de N JSON. Lo movemos al thread pool para no
    # bloquear el event loop durante operaciones I/O lentas (NAS bajo
    # carga, scan de docenas de ficheros).
    sessions = await asyncio.to_thread(list_cmv40_sessions_summary)
    return {"sessions": sessions}


@app.get("/api/cmv40/rpu-files", summary="Lista RPUs disponibles en /mnt/cmv40_rpus")
async def list_cmv40_rpu_files():
    # ⚠️ DEV MODE
    if DEV_MODE:
        return {"files": DEV_FAKE_RPU_FILES}
    return {"files": list_available_rpus()}


@app.get("/api/cmv40/recommend",
         summary="Recomendación CMv4.0 basada en el sheet live de REC_9999")
async def cmv40_recommend_endpoint(title: str, year: int | None = None):
    """Dado un título (y opcionalmente año) extraídos del MKV origen,
    consulta el sheet de R3S3T_9999 (vía TMDb ES→EN si hay API key) y
    devuelve si la conversión a CMv4.0 es factible y, si no, por qué.
    """
    from services.cmv40_recommend import recommend
    result = await recommend(title, year)
    return result.model_dump()


@app.get("/api/cmv40/recommend-from-filename",
         summary="Recomendación CMv4.0 parseando el filename del MKV")
async def cmv40_recommend_from_filename_endpoint(filename: str):
    """Extrae título+año de un filename tipo 'Zootrópolis 2 (2025) [DV FEL].mkv'
    y delega en /recommend. Atajo para el frontend."""
    from services.cmv40_recommend import parse_mkv_filename, recommend
    title, year = parse_mkv_filename(filename)
    result = await recommend(title, year)
    return result.model_dump()


@app.post("/api/cmv40/tmdb-search",
          summary="Lista de candidatos TMDb para un título — selector multi-resultado")
async def cmv40_tmdb_search(body: dict):
    """Devuelve hasta 10 coincidencias TMDb para un título. Pensado para
    el modal de Consulta rápida cuando el título es ambiguo (ej. 'Predator'
    devuelve 5-6 películas distintas). El frontend muestra un selector y
    luego llama a /tmdb-lookup o /recommend con el título final."""
    from services.tmdb import search_movies, is_configured
    if not is_configured():
        return {"tmdb_configured": False, "candidates": []}
    title = (body.get("title") or "").strip()
    year = body.get("year")
    if isinstance(year, str) and year.strip():
        try:
            year = int(year)
        except ValueError:
            year = None
    if not title:
        return {"tmdb_configured": True, "candidates": []}
    matches = await search_movies(title, year if isinstance(year, int) else None, limit=10)
    return {
        "tmdb_configured": True,
        "query_title": title,
        "query_year": year if isinstance(year, int) else None,
        "candidates": [m.model_dump() for m in matches],
    }


@app.post("/api/cmv40/tmdb-lookup",
          summary="Busca y trae detalles TMDb para un filename (sin crear proyecto)")
async def cmv40_tmdb_lookup(body: dict):
    """Parsea filename → TMDb search → fetch details. Usado por el frontend
    para pintar la ficha de la película en la cabecera del proyecto."""
    from services.cmv40_recommend import parse_mkv_filename
    from services.tmdb import search_movies, fetch_details, is_configured

    if not is_configured():
        return {"tmdb_configured": False, "details": None}

    filename = body.get("filename", "")
    source_mkv_name = body.get("source_mkv_name", "")
    name = filename or source_mkv_name
    if not name:
        return {"tmdb_configured": True, "details": None}

    title, year = parse_mkv_filename(name)
    matches = await search_movies(title, year, limit=1)
    if not matches:
        return {"tmdb_configured": True, "details": None,
                "input_title": title, "input_year": year}
    details = await fetch_details(matches[0].tmdb_id)
    return {
        "tmdb_configured": True,
        "input_title": title,
        "input_year": year,
        "details": details.model_dump() if details else None,
    }


@app.post("/api/cmv40/{session_id}/tmdb-refresh",
          summary="Fuerza re-fetch de detalles TMDb y los guarda en la sesión")
async def cmv40_tmdb_refresh(session_id: str):
    from services.cmv40_recommend import parse_mkv_filename
    from services.tmdb import search_movies, fetch_details, is_configured

    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if not is_configured():
        return {"tmdb_configured": False, "updated": False}

    title, year = parse_mkv_filename(session.source_mkv_name)
    matches = await search_movies(title, year, limit=1)
    if not matches:
        session.tmdb_info = None
        save_cmv40_session(session)
        return {"tmdb_configured": True, "updated": True, "details": None}

    details = await fetch_details(matches[0].tmdb_id)
    session.tmdb_info = details.model_dump() if details else None
    save_cmv40_session(session)
    return {"tmdb_configured": True, "updated": True,
            "details": session.tmdb_info}


@app.get("/api/cmv40/repo-survey",
         summary="Survey de TODOS los .bin del repo DoviTools con su tipo predicho")
async def cmv40_repo_survey(refresh: bool = False):
    """Lista todos los `.bin` del repo de REC_9999 y los clasifica por
    tipo predicho (predict_bin_type sobre el nombre). Útil para entender
    la composición del repo y los casos no clasificados ('unknown').
    """
    from services.rec999_drive import (
        list_bin_files,
        is_configured as drive_configured,
        is_folder_configured as drive_folder_configured,
    )
    from services.rec999_drive_match import predict_bin_type, is_not_target, predict_provenance
    from services.settings_store import get_google_api_key

    if not drive_configured():
        folder_ok = drive_folder_configured()
        key_ok = bool(get_google_api_key())
        if not folder_ok:
            err = "URL del repositorio DoviTools no configurada — configúrala en ⚙︎ Configuración (requiere donación al autor del repo)"
        else:
            err = "Google API key no configurada — añádela en ⚙︎ Configuración"
        return {
            "drive_configured": False,
            "drive_folder_configured": folder_ok,
            "google_key_configured": key_ok,
            "error": err,
        }

    try:
        files = await list_bin_files(force_refresh=refresh)
    except PermissionError as e:
        return {"drive_configured": True, "error": str(e)}

    buckets: dict[str, list[dict]] = {
        "trusted_p7_fel_final": [],
        "trusted_p7_mel_final": [],
        "trusted_p8_source":    [],
        "not_target":           [],   # _CM_Analyze, _Resolve
        "unknown":              [],
    }
    # Cross-tab: target_type × provenance. Permite ver, p.ej., cuántos
    # FEL son retail vs generated.
    cross: dict[str, dict[str, int]] = {}
    for f in files:
        # not_target gana a predict_bin_type: aunque tenga profile/cmv4 en el
        # nombre, si es analysis/working file no lo tratamos como target.
        if is_not_target(f.name):
            pt = "not_target"
        else:
            pt = predict_bin_type(f.name)
        prov = predict_provenance(f.name)
        buckets.setdefault(pt, []).append({
            "name": f.name,
            "path": f.path,
            "size_mb": round((f.size_bytes or 0) / 1024 / 1024, 1),
            "provenance": prov,
        })
        prov_key = prov or "unknown"
        cross.setdefault(pt, {}).setdefault(prov_key, 0)
        cross[pt][prov_key] += 1

    summary = {k: len(v) for k, v in buckets.items()}
    return {
        "drive_configured": True,
        "total": len(files),
        "summary": summary,
        "cross_type_provenance": cross,
        "by_type": buckets,
    }


@app.get("/api/cmv40/repo-rpus",
         summary="Lista de .bin candidatos en el repositorio REC_9999 para un título")
async def cmv40_repo_rpus(title: str = "", year: int | None = None,
                           filename: str | None = None):
    """Candidatos rankeados del repositorio Drive de REC_9999. Si se
    pasa `filename` se parsea; si no, `title`+`year` directos."""
    from services.cmv40_recommend import parse_mkv_filename
    from services.rec999_drive import (
        is_configured as drive_configured,
        is_folder_configured as drive_folder_configured,
    )
    from services.rec999_drive_match import find_candidates
    from services.settings_store import get_google_api_key
    from services.tmdb import search_movies, is_configured as tmdb_configured

    if filename:
        parsed_title, parsed_year = parse_mkv_filename(filename)
        title = title or parsed_title
        year = year if year is not None else parsed_year

    if not drive_configured():
        folder_ok = drive_folder_configured()
        key_ok = bool(get_google_api_key())
        if not folder_ok and not key_ok:
            err = "Falta la URL del repositorio DoviTools Y la Google API key — configura ambas en ⚙︎ Configuración"
        elif not folder_ok:
            err = "URL del repositorio DoviTools no configurada — el acceso al repo es privado (donación al autor). Configúralo en ⚙︎ Configuración"
        else:
            err = "Google API key no configurada — añádela en ⚙︎ Configuración"
        return {
            "drive_configured": False,
            "drive_folder_configured": folder_ok,
            "google_key_configured": key_ok,
            "tmdb_configured": tmdb_configured(),
            "title_en": "",
            "title_es": title,
            "year": year,
            "candidates": [],
            "error": err,
        }

    # Top-5 candidatos TMDb en vez de solo el primero. Caso real:
    # 'Devuélvemela 2024' (ES) → TMDb por popularidad puede devolver como
    # #1 un cortometraje no relacionado, y la peli real ('Bring Her Back'
    # 2025) queda en posicion #2-3. Si nos quedamos con el #1, title_en
    # es incorrecto y el match contra el repo Drive falla.
    # Probamos cada candidato (ES + EN) y nos quedamos con el que produzca
    # mejor score contra el repo. Mismo patron que cmv40_recommend.recommend.
    title_en = title
    tmdb_candidates: list[tuple[str, int | None]] = []  # (title_en, year)
    if tmdb_configured():
        try:
            matches = await search_movies(title, year, limit=5)
            if matches:
                title_en = matches[0].title_en
                if year is None:
                    year = matches[0].year
                for m in matches:
                    tmdb_candidates.append((m.title_en, m.year or year))
        except Exception:
            pass

    # Si no tenemos candidatos TMDb (sin clave o sin matches), intentamos
    # solo con el titulo crudo del filename (en ES). title_en == title.
    if not tmdb_candidates:
        tmdb_candidates.append((title_en, year))

    try:
        # Probamos cada candidato y agregamos resultados. Dedup por file.id
        # (un mismo bin puede ganar con varios candidatos del mismo film).
        seen_files: dict[str, object] = {}
        best_title_en = title_en
        best_year = year
        best_count = -1
        for cand_title_en, cand_year in tmdb_candidates:
            cands = await find_candidates(cand_title_en, title, cand_year)
            # El "ganador" para la response es el candidato TMDb que mas
            # bins matchea — asi title_en/year en la respuesta refleja la
            # peli real (no el cortometraje irrelevante de #1).
            if len(cands) > best_count:
                best_count = len(cands)
                best_title_en = cand_title_en
                best_year = cand_year
            for c in cands:
                fid = c.file.id
                if fid not in seen_files or c.score > seen_files[fid].score:
                    seen_files[fid] = c
        candidates = sorted(seen_files.values(), key=lambda c: c.score, reverse=True)[:8]
        if best_count > 0:
            title_en = best_title_en
            year = best_year
    except PermissionError as e:
        return {
            "drive_configured": True,
            "tmdb_configured": tmdb_configured(),
            "title_en": title_en,
            "title_es": title,
            "year": year,
            "candidates": [],
            "error": str(e),
        }

    # Enriquece cada candidato con su tipo predicho y procedencia por nombre.
    # La clasificación definitiva la hace Fase B tras descargar + dovi_tool info,
    # pero esto da señalización UX inmediata en el modal.
    from services.rec999_drive_match import predict_bin_type
    cand_list = []
    for c in candidates:
        d = c.model_dump()
        d["predicted_type"] = predict_bin_type(c.file.name)
        # provenance ya viene en DriveCandidate.provenance; asegura que está
        # en el dict para el frontend.
        d.setdefault("provenance", c.provenance)
        cand_list.append(d)

    return {
        "drive_configured": True,
        "tmdb_configured": tmdb_configured(),
        "title_en": title_en,
        "title_es": title,
        "year": year,
        "candidates": cand_list,
    }


@app.post("/api/cmv40/create", summary="Crea un proyecto CMv4.0")
async def cmv40_create(body: CMv40CreateRequest):
    mkv_path = body.source_mkv_path
    # ⚠️ DEV MODE: saltar verificación de existencia
    if not DEV_MODE and not Path(mkv_path).exists():
        raise HTTPException(status_code=400, detail=f"MKV no encontrado: {mkv_path}")
    mkv_name = Path(mkv_path).name
    sid = make_cmv40_session_id(mkv_path)
    artifacts_dir = CMV40_WORK_BASE / sid
    # Nombre sugerido: reemplazar [DV FEL] por [DV FEL CMv4.0] o añadirlo
    default_name = body.output_mkv_name or mkv_name.replace(".mkv", " [CMv4.0].mkv")

    # Tamaño del MKV → ETA fallback escalado al tamaño real. Best-effort:
    # si falla (DEV_MODE con rutas fake, permisos) se queda en 0 y el ETA
    # usa el fallback constante.
    try:
        source_size = Path(mkv_path).stat().st_size if Path(mkv_path).exists() else 0
    except Exception:
        source_size = 0

    session = CMv40Session(
        id=sid,
        source_mkv_path=mkv_path,
        source_mkv_name=mkv_name,
        output_mkv_name=default_name,
        artifacts_dir=str(artifacts_dir),
        phase=CMv40Phase.CREATED,
        source_file_size_bytes=source_size,
        auto_pipeline=body.auto_pipeline,
    )
    # Persistir pending_target — clave para que el orquestador backend
    # pueda continuar el pipeline tras Fase A sin depender del frontend.
    if body.pending_target:
        pt = body.pending_target
        session.pending_target_kind = pt.kind
        session.pending_target_rpu_path = pt.rpu_path
        session.pending_target_file_id = pt.file_id
        session.pending_target_file_name = pt.file_name
        session.pending_target_source_mkv_path = pt.source_mkv_path
    save_cmv40_session(session)
    cmv40_get_workdir(session)  # crea el directorio

    # Fetch de TMDb inline con timeout corto. Así la respuesta trae
    # tmdb_info si TMDb es rápido; si no, se lanza en background y se
    # hidrata en la siguiente polling del frontend. Evita race conditions
    # con otras fases que guardan sesión sin esperar al hydrate.
    try:
        await asyncio.wait_for(_cmv40_hydrate_tmdb(sid), timeout=4.0)
        refreshed = load_cmv40_session(sid)
        if refreshed:
            session = refreshed
    except (asyncio.TimeoutError, Exception):
        # Timeout o error: seguimos sin bloquear, tarea en background
        asyncio.create_task(_cmv40_hydrate_tmdb(sid))

    # Si auto_pipeline=True, dispara el orquestador inmediatamente. El
    # orquestador detectará phase=CREATED + pending_target y disparará
    # preflight automático → tras éxito, Fase A → ... → done. Todo sin
    # depender del frontend.
    if session.auto_pipeline:
        asyncio.create_task(_cmv40_dispatch_next_phase(sid))

    return session.model_dump()


async def _cmv40_hydrate_tmdb(session_id: str) -> None:
    """Busca el título en TMDb y rellena `session.tmdb_info`. Best-effort."""
    from services.cmv40_recommend import parse_mkv_filename
    from services.tmdb import search_movies, fetch_details, is_configured

    if not is_configured():
        return
    try:
        session = load_cmv40_session(session_id)
        if not session:
            return
        title, year = parse_mkv_filename(session.source_mkv_name)
        matches = await search_movies(title, year, limit=1)
        if not matches:
            return
        details = await fetch_details(matches[0].tmdb_id)
        if not details:
            return
        # Recarga en caliente por si otra fase ha escrito entretanto
        fresh = load_cmv40_session(session_id)
        if not fresh:
            return
        fresh.tmdb_info = details.model_dump()
        save_cmv40_session(fresh)
    except Exception as e:
        # No crítico
        _logger.warning("TMDb hydrate falló para %s: %s", session_id, e)


def _cmv40_scan_artifacts(session: CMv40Session) -> dict:
    """Escanea el workdir y devuelve {filename: size_bytes} de artefactos existentes."""
    if not session.artifacts_dir:
        return {}
    wd = Path(session.artifacts_dir)
    if not wd.exists():
        return {}
    known = set()
    for arts in _CMV40_PHASE_ARTIFACTS.values():
        known.update(arts)
    sizes = {}
    for name in known:
        p = wd / name
        if p.exists() and p.is_file():
            sizes[name] = p.stat().st_size
    return sizes


_CMV40_FAKE_ARTIFACT_SIZES = {
    "source.hevc":        42_000_000_000,
    "RPU_source.bin":     4_500_000,
    "RPU_target.bin":     4_700_000,
    "BL.hevc":            38_500_000_000,
    "EL.hevc":            3_800_000_000,
    "per_frame_data.json": 12_500_000,
    "RPU_synced.bin":     4_700_000,
    "editor_config.json":  300,
    "EL_injected.hevc":   3_820_000_000,
    "source_injected.hevc": 42_500_000_000,
    "output.mkv":         48_500_000_000,
}


@app.get("/api/cmv40/{session_id}", summary="Obtiene un proyecto CMv4.0")
async def cmv40_get(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto CMv4.0 no encontrado")

    # Auto-rewind: si la sesión dice "remuxed/validated" pero el MKV esperado
    # (.mkv.tmp para remuxed, .mkv para validated) no existe físicamente en
    # OUTPUT_DIR, retrocedemos la fase a `injected` para que la UI muestre
    # Fase G (remux) como siguiente en vez de H (validate). Sin esto, el
    # usuario era llevado a Fase H y la ejecución fallaba con "MKV final no
    # existe — ejecuta Fase G primero".
    #
    # NO se aplica a phase="done": una vez que el job terminó con éxito, el
    # MKV es responsabilidad del usuario. Si lo mueve a su biblioteca final
    # (workflow normal) el proyecto debe seguir mostrándose como completado.
    # Para volver a generar el MKV, el usuario tiene el botón "Rehacer Fase G"
    # en la card done del panel — eso resetea phase a injected explícitamente.
    #
    # No se aplica a sesiones archivadas (modo solo lectura) ni a DEV_MODE.
    if (not DEV_MODE and not session.archived
            and session.phase in ("remuxed", "validated")
            and session.output_mkv_name):
        tmp_path   = OUTPUT_DIR_MKV / f"{session.output_mkv_name}.tmp"
        final_path = OUTPUT_DIR_MKV / session.output_mkv_name
        if session.phase == "remuxed":
            missing = not tmp_path.exists() and not final_path.exists()
        else:
            # validated: el archivo final debería existir (tmp ya renombrado)
            missing = not final_path.exists()
        if missing:
            _logger.info(
                "Auto-rewind de sesión %s: phase=%s pero MKV no existe en %s → injected",
                session.id, session.phase, OUTPUT_DIR_MKV,
            )
            session.phase = "injected"
            session.output_mkv_path = None
            session.error_message = None
            save_cmv40_session(session)
            await _cmv40_log(
                session,
                f"ℹ️ Auto-rewind: MKV final no existe en /mnt/output — "
                f"fase retrocedida a 'injected' para re-ejecutar desde Fase G"
            )

    # Forward-roll: complementario al auto-rewind. Si la sesión está en una
    # fase ≤ injected pero existe un .mkv.tmp con el nombre esperado Y el
    # historial de fases tiene un 'remux' completado con éxito, adelantamos
    # a 'remuxed'. Cubre el escenario donde el auto-rewind disparó por
    # error (p.ej. .mkv.tmp temporalmente invisible por glitch del NAS) y
    # luego el .mkv.tmp reaparece — el usuario quedaría atascado en
    # injected sin saber que el remux ya está hecho. Sin esto, tendría que
    # rehacer Fase G (~7 min, ~70 GB) para nada.
    # No se aplica si session.phase >= remuxed (ya está alineado) ni a
    # archivadas/DEV.
    if (not DEV_MODE and not session.archived
            and session.phase in ("created", "source_analyzed", "target_provided",
                                  "extracted", "sync_verified", "sync_corrected",
                                  "injected")
            and session.output_mkv_name):
        tmp_path = OUTPUT_DIR_MKV / f"{session.output_mkv_name}.tmp"
        had_successful_remux = any(
            (rec.phase == "remux" and rec.status == "done")
            for rec in (session.phase_history or [])
        )
        if tmp_path.exists() and had_successful_remux:
            _logger.info(
                "Forward-roll de sesión %s: phase=%s pero .mkv.tmp existe + remux done en historial → remuxed",
                session.id, session.phase,
            )
            session.phase = "remuxed"
            save_cmv40_session(session)
            await _cmv40_log(
                session,
                f"ℹ️ Forward-roll: el .mkv.tmp del remux ya está escrito en "
                f"/mnt/output ({tmp_path.stat().st_size / 1e9:.2f} GB) — "
                f"fase adelantada a 'remuxed' para validar sin re-mux."
            )

    data = session.model_dump()
    if DEV_MODE:
        # En DEV simulamos tamaños de artefactos según la fase alcanzada
        target_idx = CMV40_PHASES_ORDER.index(session.phase)
        fake_arts = {}
        for phase_name, arts in _CMV40_PHASE_ARTIFACTS.items():
            if CMV40_PHASES_ORDER.index(phase_name) <= target_idx:
                for name in arts:
                    if name in _CMV40_FAKE_ARTIFACT_SIZES:
                        fake_arts[name] = _CMV40_FAKE_ARTIFACT_SIZES[name]
        data["artifacts"] = fake_arts
    else:
        data["artifacts"] = _cmv40_scan_artifacts(session)
    return data


@app.delete("/api/cmv40/{session_id}", summary="Borra un proyecto CMv4.0")
async def cmv40_delete(session_id: str, clean_artifacts: bool = False):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if clean_artifacts and session.artifacts_dir:
        wd = Path(session.artifacts_dir)
        if wd.exists():
            _cmv40_shutil.rmtree(wd, ignore_errors=True)
    delete_cmv40_session(session_id)
    return {"ok": True}


class CMv40RenameRequest(BaseModel):
    output_mkv_name: str


def _cmv40_guard_mutable(session: CMv40Session):
    """Lanza 400 si la sesión no admite más mutaciones (archivada o completada)."""
    if session.archived:
        raise HTTPException(status_code=400, detail="Proyecto archivado — solo lectura")
    if session.phase == "done":
        raise HTTPException(status_code=400, detail="Proyecto completado — usa 'Rehacer' para iterar")


@app.post("/api/cmv40/{session_id}/rename-output", summary="Edita el nombre del MKV de salida")
async def cmv40_rename_output(session_id: str, body: CMv40RenameRequest):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    _cmv40_guard_mutable(session)
    new_name = body.output_mkv_name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Nombre vacío")
    if not new_name.lower().endswith(".mkv"):
        new_name += ".mkv"
    session.output_mkv_name = new_name
    save_cmv40_session(session)
    return session.model_dump()


@app.post("/api/cmv40/{session_id}/cleanup", summary="Borra artefactos intermedios")
async def cmv40_cleanup(session_id: str):
    """
    Borra todos los artefactos intermedios del workdir. Tras esta acción el
    proyecto queda ARCHIVADO (modo solo lectura) — no se pueden rehacer fases
    porque los prerrequisitos ya no existen. Para iterar de nuevo, crear un
    proyecto nuevo.
    """
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    wd = Path(session.artifacts_dir) if session.artifacts_dir else None
    freed = 0
    if wd and wd.exists():
        # Borrar TODOS los artefactos intermedios, preservar session.json (vive en /config)
        for arts in _CMV40_PHASE_ARTIFACTS.values():
            for name in arts:
                f = wd / name
                if f.exists() and f.is_file():
                    freed += f.stat().st_size
                    try:
                        f.unlink()
                    except Exception as e:
                        _logger.warning("No se pudo borrar %s: %s", f, e)
        # Borrar plots + RPU_synced también
        for extra in ["RPU_synced.bin", "editor_config.json"]:
            f = wd / extra
            if f.exists() and f.is_file():
                freed += f.stat().st_size
                try:
                    f.unlink()
                except Exception:
                    pass
    # También borrar .mkv.tmp orfeno en /mnt/output (si Fase G escribió pero
    # Fase H no completó)
    try:
        tmp_path = OUTPUT_DIR_MKV / f"{session.output_mkv_name}.tmp"
        if tmp_path.exists() and tmp_path.is_file():
            freed += tmp_path.stat().st_size
            tmp_path.unlink()
    except Exception as e:
        _logger.warning("No se pudo borrar .mkv.tmp: %s", e)
    session.archived = True
    save_cmv40_session(session)
    await _cmv40_log(session, f"🗃️ Artefactos borrados ({freed / 1e9:.2f} GB liberados). Proyecto archivado en modo solo lectura.")
    return {"ok": True, "freed_bytes": freed, "archived": True}


@app.post("/api/cmv40/{session_id}/clear-error", summary="Descarta el mensaje de error")
async def cmv40_clear_error(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    session.error_message = ""
    save_cmv40_session(session)
    return session.model_dump()


@app.post(
    "/api/cmv40/{session_id}/accept-keep",
    summary="Acepta la recomendación de mantener el MKV actual — cierra el proyecto sin procesar",
)
async def cmv40_accept_keep(session_id: str):
    """Cuando el modelo recomienda mantener el MKV actual (bin sintético, sin
    bin, no aporta), el usuario puede aceptar la recomendación con este
    endpoint. El proyecto se marca como `done` con
    `output_workflow="keep_cmv29"` — sin tocar el MKV original. Aparece en
    el historial como completado vía "mantener MKV".

    El usuario sigue teniendo el MKV original (con CMv2.9) y deja que su
    reproductor (p3i T4 / Sony / LG modernos) haga la conversión a CMv4.0
    al vuelo en runtime con el mismo resultado visible.
    """
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if session.archived:
        raise HTTPException(status_code=400, detail="Proyecto archivado")
    if session.phase == "done":
        raise HTTPException(status_code=400, detail="Proyecto ya está completado")
    if session.recommended_action != "keep":
        raise HTTPException(
            status_code=400,
            detail=f"La recomendación actual no es Keep (es '{session.recommended_action}')",
        )
    # Cierre formal del proyecto vía Keep
    session.phase = "done"
    session.output_workflow = "keep_cmv29"
    session.error_message = ""
    session.running_phase = None
    save_cmv40_session(session)
    await _cmv40_log(
        session,
        "✓ Proyecto cerrado manteniendo el MKV actual — el fichero original "
        "queda sin tocar. Un reproductor compatible con CMv4.0 (p3i T4 / Sony "
        "/ LG modernos) hará la conversión al vuelo en runtime con el mismo "
        "resultado visible que tendría inyectar el RPU."
    )
    return session.model_dump()


@app.post(
    "/api/cmv40/{session_id}/override-recommendation",
    summary="Fuerza la inyección del RPU CMv4.0 ignorando la recomendación de mantener",
)
async def cmv40_override_recommendation(session_id: str):
    """El usuario decide procesar el proyecto aunque el modelo recomiende
    mantener el MKV actual (bin sintético). Útil cuando quiere archivar la
    versión CMv4.0 "completa" por compatibilidad con otros reproductores,
    aunque el resultado visible sea equivalente a la conversión al vuelo
    del p3i T4.

    Resetea `preflight_decision` y `recommended_action` para desbloquear
    el orquestador, y dispara la siguiente fase si auto_pipeline=True.
    """
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if session.archived:
        raise HTTPException(status_code=400, detail="Proyecto archivado")
    if not session.recommended_action or session.recommended_action != "keep":
        # No hay recomendación Keep que sobrescribir — no-op
        return session.model_dump()

    # Reset de la decisión Keep para desbloquear el pipeline
    session.preflight_decision = "ok" if session.target_preflight_ok else ""
    session.recommended_action = ""
    session.recommended_action_label = ""
    session.recommended_action_reason = ""
    session.preflight_message = ""
    # El bin pasó pre-flight como CMv4.0 (es real, no abort_no_cmv40);
    # solo lo marcamos como apto para avanzar.
    if session.target_dv_info and session.target_dv_info.cm_version == "v4.0":
        session.target_preflight_ok = True
    save_cmv40_session(session)
    await _cmv40_log(
        session,
        "🔬 Inyección forzada por el usuario — el pipeline continuará "
        "procesando el MKV aunque el bin sea sintético. El resultado es "
        "funcionalmente equivalente a la conversión al vuelo del reproductor, "
        "pero queda archivado como MKV CMv4.0 'completo' para compatibilidad."
    )
    # Despierta el orquestador si auto está activo
    if session.auto_pipeline:
        asyncio.create_task(_cmv40_dispatch_next_phase(session_id))
    return session.model_dump()


@app.post("/api/cmv40/{session_id}/auto-pipeline",
          summary="Activa/desactiva el auto-pipeline backend-driven para un proyecto")
async def cmv40_set_auto_pipeline(session_id: str, body: CMv40AutoPipelineRequest):
    """Cambia `session.auto_pipeline`. Cuando se activa Y la sesión está en una
    fase intermedia (no done/error/archived/created), dispara INMEDIATAMENTE
    el orquestador para que la cadena reanude desde donde quedó. Esto cubre
    el caso "proyecto atascado: el frontend no avanzó por throttling/sleep,
    el usuario activa auto y backend retoma".
    """
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    session.auto_pipeline = body.enabled
    save_cmv40_session(session)
    if body.enabled:
        await _cmv40_log(session, "🤖 Auto-pipeline backend ACTIVADO — el job avanzará automáticamente sin depender del cliente")
        # Dispara orquestador inmediatamente — si la sesión está en una fase
        # intermedia y no hay running_phase, retoma la cadena.
        asyncio.create_task(_cmv40_dispatch_next_phase(session_id))
    else:
        await _cmv40_log(session, "🤖 Auto-pipeline backend DESACTIVADO — las transiciones requerirán acción manual o frontend activo")
    return session.model_dump()


@app.post("/api/cmv40/{session_id}/acknowledge-critical-gates",
          summary="El usuario reconoce gates críticos fallados y autoriza continuar el pipeline")
async def cmv40_acknowledge_critical_gates(session_id: str):
    """Liberación del pause-point por gates críticos no corregibles. Marca
    user_acknowledged_degradation=True y limpia awaiting_critical_ack para que
    el auto-pipeline pueda continuar. El frontend, en el siguiente tick,
    saltará Fase D explícitamente porque user_acknowledged_degradation hace
    que la rama trustedAuto se active aunque target_trust_ok sea False.

    No borra critical_gate_failures — se preserva como histórico para que
    la UI siga pudiendo mostrar el aviso (en otro estilo, ya reconocido)
    en el panel del proyecto y en el log."""
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if not session.awaiting_critical_ack:
        raise HTTPException(
            status_code=400,
            detail="No hay confirmación pendiente para este proyecto.",
        )
    session.awaiting_critical_ack = False
    session.user_acknowledged_degradation = True
    if session.phases_skipped is None:
        session.phases_skipped = []
    if "sync_verification_pause" not in session.phases_skipped:
        session.phases_skipped.append("sync_verification_pause")
    save_cmv40_session(session)
    # Si auto-pipeline backend activo, retoma la cadena (la sesión ya no
    # tiene awaiting_critical_ack — el orquestador puede avanzar).
    if session.auto_pipeline:
        asyncio.create_task(_cmv40_dispatch_next_phase(session_id))
    return session.model_dump()


# ── Limpieza masiva de artefactos CMv4.0 ─────────────────────────────────────

@app.get(
    "/api/cmv40/cleanup/preview",
    summary="Preview de artefactos CMv4.0 con tamaños y estado por proyecto",
)
async def cmv40_cleanup_preview():
    """Devuelve la lista de proyectos CMv4.0 con info necesaria para decidir
    qué limpiar: tamaño del workdir, fase actual, estado (done/error/archived/
    en progreso), si hay running_phase. NO borra nada — solo lectura."""
    sessions = list_cmv40_sessions()
    items: list[dict] = []
    total_bytes = 0
    for s in sessions:
        wd = Path(s.artifacts_dir) if s.artifacts_dir else None
        size = 0
        files_count = 0
        wd_exists = bool(wd and wd.exists())
        if wd_exists:
            try:
                for f in wd.rglob("*"):
                    if f.is_file():
                        try:
                            size += f.stat().st_size
                            files_count += 1
                        except OSError:
                            pass
            except Exception:
                pass
        # Determinar estado y si es seguro borrar
        running = bool(s.running_phase)
        if running:
            state = "running"
            safe = False
            reason = f"Fase {s.running_phase} en curso — no borrar"
        elif s.archived:
            state = "archived"
            safe = False
            reason = "Ya archivado (sin artefactos)"
        elif s.phase == "done":
            state = "done"
            safe = True
            reason = "Pipeline terminado, listo para limpiar"
        elif s.error_message:
            state = "error"
            safe = True
            reason = f"Última fase falló: {s.error_message[:80]}"
        else:
            state = "in_progress"
            safe = True
            reason = f"Pipeline detenido en fase {s.phase}"

        items.append({
            "id": s.id,
            # Titulo legible para la UI: el output_mkv_name ya viene formateado
            # ("Title (Year) [DV FEL CMv4.0].mkv") cuando hay datos; si no,
            # caemos al source_mkv_name; si tampoco, al id.
            "title": s.output_mkv_name or s.source_mkv_name or s.id,
            "phase": s.phase,
            "running_phase": s.running_phase,
            "state": state,
            "size_bytes": size,
            "files_count": files_count,
            "wd_exists": wd_exists,
            "artifacts_dir": str(wd) if wd else "",
            "safe_to_delete": safe,
            "reason": reason,
            "error_message": s.error_message,
            "output_mkv_name": s.output_mkv_name,
            "output_mkv_path": s.output_mkv_path,
        })
        total_bytes += size
    return {
        "items": items,
        "total_count": len(items),
        "total_bytes": total_bytes,
        "deletable_count": sum(1 for i in items if i["safe_to_delete"]),
        "deletable_bytes": sum(i["size_bytes"] for i in items if i["safe_to_delete"]),
    }


class CMv40CleanupBulkRequest(BaseModel):
    session_ids: list[str]


@app.post(
    "/api/cmv40/cleanup/bulk",
    summary="Limpia artefactos de varios proyectos CMv4.0 a la vez",
)
async def cmv40_cleanup_bulk(body: CMv40CleanupBulkRequest):
    """Borra los artefactos del workdir de cada session_id de la lista. Marca
    cada uno como archived=True (modo solo lectura). NO borra el JSON de
    sesión — el proyecto sigue visible en el listado, solo en estado archivado.
    Saltea proyectos con running_phase activo (no se puede borrar mientras
    una fase corre)."""
    deleted = []
    skipped = []
    failed = []
    total_freed = 0
    for sid in body.session_ids or []:
        session = load_cmv40_session(sid)
        if not session:
            failed.append({"id": sid, "error": "Proyecto no encontrado"})
            continue
        if session.running_phase:
            skipped.append({
                "id": sid,
                "reason": f"Fase {session.running_phase} en curso",
            })
            continue
        wd = Path(session.artifacts_dir) if session.artifacts_dir else None
        freed = 0
        if wd and wd.exists():
            for arts in _CMV40_PHASE_ARTIFACTS.values():
                for name in arts:
                    f = wd / name
                    if f.exists() and f.is_file():
                        try:
                            freed += f.stat().st_size
                            f.unlink()
                        except Exception as e:
                            _logger.warning("[Bulk cleanup] %s: %s", f, e)
            for extra in ["RPU_synced.bin", "editor_config.json"]:
                f = wd / extra
                if f.exists() and f.is_file():
                    try:
                        freed += f.stat().st_size
                        f.unlink()
                    except Exception:
                        pass
        # .mkv.tmp en /mnt/output
        try:
            tmp_path = OUTPUT_DIR_MKV / f"{session.output_mkv_name}.tmp"
            if tmp_path.exists() and tmp_path.is_file():
                freed += tmp_path.stat().st_size
                tmp_path.unlink()
        except Exception as e:
            _logger.warning("[Bulk cleanup] .mkv.tmp %s: %s", sid, e)
        session.archived = True
        save_cmv40_session(session)
        await _cmv40_log(session,
            f"🗃️ Artefactos borrados via cleanup masivo ({freed / 1e9:.2f} GB). "
            f"Proyecto archivado.")
        deleted.append({"id": sid, "freed_bytes": freed})
        total_freed += freed
    return {
        "deleted": deleted,
        "skipped": skipped,
        "failed": failed,
        "total_freed_bytes": total_freed,
    }


# Mapa de artefactos producidos por cada fase (se borran al rehacer esa fase o anterior)
_CMV40_PHASE_ARTIFACTS: dict[str, list[str]] = {
    "source_analyzed": ["source.hevc", "RPU_source.bin"],
    "target_provided": ["RPU_target.bin"],
    "extracted":       ["BL.hevc", "EL.hevc", "per_frame_data.json"],
    "sync_corrected":  ["RPU_synced.bin", "editor_config.json"],
    "injected":        ["EL_injected.hevc", "BL_injected.hevc", "source_injected.hevc", "RPU_merged.bin"],
    "remuxed":         ["output.mkv", "DV_dual.hevc"],
}


def _cmv40_artifacts_to_delete(target_phase: str) -> list[str]:
    """Lista los artefactos que se borrarán al hacer reset-to target_phase."""
    target_idx = CMV40_PHASES_ORDER.index(target_phase)
    files: list[str] = []
    for phase_name, arts in _CMV40_PHASE_ARTIFACTS.items():
        if CMV40_PHASES_ORDER.index(phase_name) > target_idx:
            files.extend(arts)
    return files


@app.get("/api/cmv40/{session_id}/reset-preview/{target_phase}", summary="Previsualiza qué se borrará al rehacer")
async def cmv40_reset_preview(session_id: str, target_phase: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if target_phase not in CMV40_PHASES_ORDER:
        raise HTTPException(status_code=400, detail=f"Fase inválida: {target_phase}")

    wd = Path(session.artifacts_dir) if session.artifacts_dir else None
    files = _cmv40_artifacts_to_delete(target_phase)
    existing: list[dict] = []
    total_bytes = 0
    if wd and wd.exists():
        for name in files:
            p = wd / name
            if p.exists() and p.is_file():
                sz = p.stat().st_size
                total_bytes += sz
                existing.append({"name": name, "size_bytes": sz})
    return {"files": existing, "total_bytes": total_bytes}


@app.post("/api/cmv40/{session_id}/reset-to/{target_phase}", summary="Resetea a una fase anterior (para rehacer)")
async def cmv40_reset_to(session_id: str, target_phase: str):
    """
    Rebobina el estado de la sesión a una fase anterior y borra los
    artefactos de fases posteriores para garantizar consistencia al re-ejecutar.
    """
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    if session.archived:
        raise HTTPException(
            status_code=400,
            detail="Proyecto archivado — los artefactos intermedios fueron borrados. "
                   "Crea un nuevo proyecto CMv4.0 para iterar de nuevo.",
        )

    valid_phases = [p for p in CMV40_PHASES_ORDER if p != "done"]
    if target_phase not in valid_phases:
        raise HTTPException(status_code=400, detail=f"Fase inválida: {target_phase}")

    target_idx = CMV40_PHASES_ORDER.index(target_phase)

    def _clear_from(phase_name: str):
        return CMV40_PHASES_ORDER.index(phase_name) > target_idx

    # Borrar artefactos aguas abajo
    wd = Path(session.artifacts_dir) if session.artifacts_dir else None
    if wd and wd.exists():
        for name in _cmv40_artifacts_to_delete(target_phase):
            p = wd / name
            if p.exists() and p.is_file():
                try:
                    p.unlink()
                except Exception as e:
                    _logger.warning("No se pudo borrar %s: %s", p, e)

    # Limpiar datos de sesión aguas abajo
    if _clear_from("source_analyzed"):
        session.source_dv_info = None
        session.source_frame_count = 0
    if _clear_from("target_provided"):
        session.target_dv_info = None
        session.target_frame_count = 0
        session.target_rpu_path = ""
        session.target_rpu_source = ""
        session.sync_delta = 0
    if _clear_from("sync_corrected"):
        session.sync_config = None
        # Restaurar target_frame_count / sync_delta al valor del RPU original,
        # no al del RPU_synced (que se está borrando aguas abajo).
        if session.artifacts_dir:
            rpu_target_bin = Path(session.artifacts_dir) / "RPU_target.bin"
            if rpu_target_bin.exists() and session.source_frame_count:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "dovi_tool", "info", "--summary", str(rpu_target_bin),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    from phases.phase_a import _parse_dovi_summary
                    dovi_info = _parse_dovi_summary(stdout.decode("utf-8", errors="replace"))
                    if dovi_info.frame_count > 0:
                        session.target_frame_count = dovi_info.frame_count
                        session.sync_delta = dovi_info.frame_count - session.source_frame_count
                except Exception as e:
                    _logger.warning("Re-análisis del RPU target original falló tras reset: %s", e)
    if _clear_from("done"):
        session.output_mkv_path = ""

    session.phase = target_phase
    session.error_message = ""
    save_cmv40_session(session)
    await _cmv40_log(session, f"🔄 Estado reseteado a fase: {target_phase} — artefactos posteriores borrados")
    return session.model_dump()


@app.post("/api/cmv40/{session_id}/verify-artifacts", summary="Valida que los artefactos de la fase actual existan")
async def cmv40_verify_artifacts(session_id: str):
    """Al reanudar un proyecto, verifica que los artefactos existen en disco.

    Si faltan ficheros para la fase actual, retrocede a la última fase válida.
    Si no hay NINGÚN artefacto utilizable, marca el proyecto con error.
    Devuelve {valid_phase, changed, missing, message, all_missing, session}.
    """
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # No validar si está running — los artefactos se están generando ahora
    if session.running_phase:
        return {
            "changed": False,
            "valid_phase": session.phase,
            "missing": [],
            "message": "Proyecto ejecutándose — validación omitida",
            "all_missing": False,
            "session": session.model_dump(),
        }

    result = _validate_cmv40_artifacts(session)
    if result["changed"]:
        session.phase = result["valid_phase"]
        # En caso "all_missing" bloqueamos con error; si hay reversión parcial,
        # solo aviso por error_message descartable.
        if result["all_missing"]:
            session.error_message = result["message"]
        else:
            # Aviso suave que el usuario puede descartar
            session.error_message = result["message"]
        save_cmv40_session(session)
        await _cmv40_log(session, f"⚠ {result['message']}")
    elif result.get("message") and session.phase == "done":
        # Done con MKV movido: solo log informativo, sin tocar el estado
        # ni disparar warning en UI (el usuario lo movio a proposito).
        await _cmv40_log(session, f"ℹ {result['message']}")
    result["session"] = session.model_dump()
    return result


@app.post("/api/cmv40/{session_id}/cancel", summary="Cancela la fase en curso")
async def cmv40_cancel(session_id: str):
    """Cancelación en escalada:
      1. Setea cancel_flag (los puntos de chequeo en código pueden hacer
         raise antes de necesitar tocar el proceso).
      2. SIGTERM al subprocess activo: ffmpeg/dovi_tool/mkvmerge tienen
         handlers que cierran ficheros y dejan el estado consistente.
      3. Espera hasta 5s a que salga limpio.
      4. Si sigue vivo → SIGKILL forzoso (con espera adicional de 2s).
      5. Intenta también killpg (process group) por si el subprocess
         lanzó hijos (algunos ffmpeg lo hacen con hwaccel).
      6. Limpia running_phase y registro de procs en cualquier caso —
         así el pipeline puede arrancar otra fase sin estado zombi.
    """
    import os
    import signal
    _cmv40_cancel_flags[session_id] = True
    proc = _cmv40_active_procs.get(session_id)
    log_lines: list[str] = []

    if proc:
        # Paso 1: SIGTERM
        try:
            proc.terminate()
            log_lines.append("🛑 SIGTERM enviado al proceso, esperando salida limpia (máx. 5s)…")
        except ProcessLookupError:
            log_lines.append("ℹ El proceso ya había terminado antes del cancel.")
        except Exception as e:
            log_lines.append(f"⚠ SIGTERM falló ({e}); intentando SIGKILL directo.")

        # Paso 2: esperar hasta 5s a que salga limpio
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
            log_lines.append(f"✓ Proceso terminado limpiamente (rc={proc.returncode}).")
        except asyncio.TimeoutError:
            # Paso 3: SIGKILL
            log_lines.append("⏱ El proceso no respondió a SIGTERM en 5s — escalando a SIGKILL…")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except Exception as e:
                log_lines.append(f"⚠ SIGKILL falló ({e}).")

            # Paso 4: si sigue vivo, intentar killpg al grupo de procesos
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
                log_lines.append("✓ Proceso terminado por SIGKILL.")
            except asyncio.TimeoutError:
                log_lines.append("⚠ Proceso no muere ni con SIGKILL — intentando matar el grupo de procesos…")
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                    log_lines.append("✓ Grupo de procesos terminado (killpg).")
                except (ProcessLookupError, PermissionError, asyncio.TimeoutError) as e:
                    log_lines.append(f"⚠ killpg también falló ({e}); el proceso queda como zombi pero la sesión se libera.")
                except Exception as e:
                    log_lines.append(f"⚠ killpg error inesperado ({e}).")
        except Exception as e:
            log_lines.append(f"⚠ Error esperando salida del proceso: {e}")

    # Limpieza del estado de sesión — siempre, incluso si el kill falló:
    # mejor sesión liberada con proceso zombi que UI bloqueada esperando.
    session = load_cmv40_session(session_id)
    if session:
        session.running_phase = None
        for line in log_lines:
            await _cmv40_log(session, line)
        await _cmv40_log(session, "🛑 Cancelado por el usuario")
        save_cmv40_session(session)
    _cmv40_active_procs.pop(session_id, None)
    return {"ok": True, "log": log_lines}


# ── Endpoints de fases ───────────────────────────────────────────────────────

@app.post("/api/cmv40/{session_id}/analyze-source", summary="Fase A: analiza MKV origen")
async def cmv40_analyze_source(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # ⚠️ DEV MODE — simular fase A con logs realistas
    if DEV_MODE:
        from models import DoviInfo
        def _apply(s):
            s.source_dv_info = DoviInfo(profile=7, el_type="FEL", cm_version="v2.9", frame_count=137952)
            s.source_frame_count = 137952
            s.source_fps = 23.976
        logs = [
            f"$ ffmpeg -i {session.source_mkv_path} -map 0:v:0 -c:v copy -bsf:v hevc_mp4toannexb -f hevc source.hevc",
            "ffmpeg version 4.4.2 Copyright (c) 2000-2021 the FFmpeg developers",
            "Input #0, matroska,webm, from 'source.mkv':",
            "  Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x2160 [SAR 1:1 DAR 16:9], 23.98 fps",
            "Output #0, hevc, to 'source.hevc':",
            "frame= 45231 fps=245 q=-1.0 size= 8456Mb time=00:31:27.42 bitrate=37552kbps speed=10.2x",
            "frame= 89142 fps=248 q=-1.0 size=16823Mb time=01:01:59.21 bitrate=37105kbps speed=10.3x",
            "frame=137952 fps=250 q=-1.0 Lsize=25921Mb time=01:35:52.08 bitrate=37821kbps speed=10.4x",
            "[Fase A] Extrayendo HEVC completado",
            "$ dovi_tool extract-rpu source.hevc -o RPU_source.bin",
            "Parsing HEVC file...",
            "Found SPS/PPS. Starting RPU extraction.",
            "Scanning for Dolby Vision metadata...",
            "Processed 50000/137952 frames",
            "Processed 100000/137952 frames",
            "Processed 137952/137952 frames",
            "$ dovi_tool info --summary RPU_source.bin",
            "Summary:",
            "  Frames: 137952",
            "  Profile: 7 (FEL)",
            "  DM version: 1 (CM v2.9)",
            "  Scene/shot count: 487",
        ]
        asyncio.create_task(_dev_simulate_phase(session, "analyze_source", logs,
                                                 CMv40Phase.SOURCE_ANALYZED, _apply, total_seconds=4.0))
        return {"ok": True, "started": True}

    _cmv40_cancel_flags.pop(session_id, None)

    async def _coro(log_cb, proc_cb):
        await run_phase_a_analyze_source(session, log_cb, proc_cb)

    async def _run():
        try:
            await _run_cmv40_phase(session, "analyze_source", _coro, CMv40Phase.SOURCE_ANALYZED)
        except Exception:
            pass

    asyncio.create_task(_run())
    return {"ok": True, "started": True}


class CMv40TargetPathRequest(BaseModel):
    rpu_path: str


@app.post("/api/cmv40/{session_id}/target-rpu-path", summary="Fase B1: RPU target desde path")
async def cmv40_target_path(session_id: str, body: CMv40TargetPathRequest):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # ⚠️ DEV MODE
    if DEV_MODE:
        from models import DoviInfo
        session.target_rpu_source = "path"
        session.target_rpu_path = body.rpu_path
        session.target_dv_info = DoviInfo(profile=7, el_type="FEL", cm_version="v4.0", frame_count=137992)
        session.target_frame_count = 137992
        session.sync_delta = 137992 - session.source_frame_count
        session.phase = CMv40Phase.TARGET_PROVIDED
        save_cmv40_session(session)
        await _cmv40_log(session, f"[DEV] RPU target: CM v4.0, 137992 frames (Δ = {session.sync_delta:+d})")
        return session.model_dump()

    async def _coro(log_cb, proc_cb):
        await run_phase_b_target_from_path(session, body.rpu_path, log_cb)

    try:
        await _run_cmv40_phase(session, "target_rpu_path", _coro, CMv40Phase.TARGET_PROVIDED)
        return session.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CMv40TargetMkvRequest(BaseModel):
    source_mkv_path: str


class CMv40TargetDriveRequest(BaseModel):
    file_id: str
    file_name: str = ""


@app.post("/api/cmv40/{session_id}/target-rpu-from-drive",
          summary="Fase B3: RPU target descargado del repositorio REC_9999 en Drive")
async def cmv40_target_from_drive(session_id: str, body: CMv40TargetDriveRequest):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # ⚠️ DEV MODE
    if DEV_MODE:
        from models import DoviInfo
        # DEV: simula un bin trusted_p7_fel_final (drop-in) desde el Drive
        # con frame count que coincide con el source → gates críticos OK.
        src_frames = session.source_frame_count or 137992
        session.target_rpu_source = "drive"
        session.target_rpu_path = f"drive://{body.file_id}/{body.file_name}"
        session.target_dv_info = DoviInfo(
            profile=7, el_type="FEL", cm_version="v4.0",
            frame_count=src_frames,
            has_l8=True, l5_top=276, l5_bottom=276,
            l6_max_cll=900, l6_max_fall=180,
            l1_max_cll=880.0, l1_max_fall=80.0,
        )
        session.target_frame_count = src_frames
        session.sync_delta = 0
        session.target_type = "trusted_p7_fel_final"
        session.target_trust_ok = True
        session.target_trust_gates = {
            "frames":     {"ok": True, "bd": src_frames, "target": src_frames, "critical": True},
            "cm_version": {"ok": True, "value": "v4.0", "critical": True},
            "has_l8":     {"ok": True, "critical": True},
            "l5_div":     {"ok": True, "px_max": 0, "soft_px": 5, "critical_px": 30, "warn": False, "critical": True},
            "l6_div":     {"ok": True, "nits_diff": 0, "threshold": 50, "critical": False},
            "l1_div":     {"ok": True, "pct_diff": 0.0, "threshold_pct": 5.0, "critical": False},
        }
        session.phase = CMv40Phase.TARGET_PROVIDED
        save_cmv40_session(session)
        await _cmv40_log(session,
            f"[DEV] RPU trusted_p7_fel_final simulado desde Drive: "
            f"{src_frames} frames (Δ=0), gates OK → drop-in habilitado")
        return {"ok": True, "started": True}

    _cmv40_cancel_flags.pop(session_id, None)

    async def _coro(log_cb, proc_cb):
        await run_phase_b_target_from_drive(session, body.file_id, body.file_name, log_cb)

    async def _run():
        try:
            await _run_cmv40_phase(session, "target_rpu_drive", _coro, CMv40Phase.TARGET_PROVIDED)
        except Exception:
            pass

    asyncio.create_task(_run())
    return {"ok": True, "started": True}


@app.post("/api/cmv40/{session_id}/target-rpu-from-mkv", summary="Fase B2: RPU target desde otro MKV")
async def cmv40_target_from_mkv(session_id: str, body: CMv40TargetMkvRequest):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # ⚠️ DEV MODE
    if DEV_MODE:
        from models import DoviInfo
        session.target_rpu_source = "mkv"
        session.target_rpu_path = body.source_mkv_path
        session.target_dv_info = DoviInfo(profile=7, el_type="FEL", cm_version="v4.0", frame_count=137992)
        session.target_frame_count = 137992
        session.sync_delta = 137992 - session.source_frame_count
        session.phase = CMv40Phase.TARGET_PROVIDED
        save_cmv40_session(session)
        await _cmv40_log(session, f"[DEV] RPU extraído de MKV: CM v4.0, 137992 frames (Δ = {session.sync_delta:+d})")
        return {"ok": True, "started": True}

    _cmv40_cancel_flags.pop(session_id, None)

    async def _coro(log_cb, proc_cb):
        await run_phase_b_target_from_mkv(session, body.source_mkv_path, log_cb, proc_cb)

    async def _run():
        try:
            await _run_cmv40_phase(session, "target_rpu_mkv", _coro, CMv40Phase.TARGET_PROVIDED)
        except Exception:
            pass

    asyncio.create_task(_run())
    return {"ok": True, "started": True}


class CMv40PreflightRequest(BaseModel):
    """Pre-flight rápido del bin target antes de Fase A.
    Uno y solo uno de los tres conjuntos de campos:
      - kind=path:  rpu_path
      - kind=mkv:   source_mkv_path
      - kind=drive: file_id (+ opcional file_name solo para el log)
    """
    kind: str  # "path" | "mkv" | "drive"
    rpu_path: str | None = None
    source_mkv_path: str | None = None
    file_id: str | None = None
    file_name: str = ""


@app.post(
    "/api/cmv40/{session_id}/preflight-target",
    summary="Pre-flight asíncrono: valida bin target antes de Fase A (ahorra ~12 min si bin sin CMv4.0)",
)
async def cmv40_preflight_target(session_id: str, body: CMv40PreflightRequest):
    """
    Pre-flight asíncrono. Devuelve {ok:true, started:true} de inmediato y
    corre en background con `running_phase="preflight"` (que bloquea el
    auto-pipeline hasta que termine — el frontend ve el estado vía polling
    y respeta running_phase como cualquier otra fase).

    En el background:
      1. running_phase="preflight", target_preflight_ok=False
      2. download/copy/extract del bin target → workdir/RPU_target.bin
      3. dovi_tool info → clasifica
      4a. Si CMv4.0 OK → target_preflight_ok=True → polling dispara Fase A
      4b. Si bin sin CMv4.0 → error_message=<motivo> → polling NO dispara
          Fase A (auto-pipeline se detiene)
      5. running_phase=None

    Errores van al log de la sesión vía WS (igual que cualquier fase) — no
    hay toast en frontend. El usuario ve el motivo escrito en el log del
    proyecto y puede elegir otro bin.
    """
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # Guard contra re-disparo: si el pre-flight ya emitió una decisión
    # firme (keep_l8_default, keep_no_l8, abort_no_cmv40), NO re-ejecutar.
    # La decisión queda esperando acción del usuario (cancelar proyecto
    # o forzar Restore desde la UI). Sin este guard, el frontend con
    # auto-pipeline puede caer en bucle: el poll cada 4s ve
    # target_preflight_ok=False + autoContinue=true + phase=created y
    # re-llama al endpoint indefinidamente.
    if session.preflight_decision and session.preflight_decision != "ok":
        return {
            "ok": True, "started": False,
            "reason": f"preflight_decision={session.preflight_decision} ya emitida — "
                      f"esperando acción del usuario (cancelar o forzar inyección)",
            "preflight_decision": session.preflight_decision,
            "preflight_message": session.preflight_message,
        }

    # ⚠️ DEV MODE: simula un bin trusted (CMv4.0) sin descargar nada
    if DEV_MODE:
        from models import DoviInfo
        src_frames = session.source_frame_count or 137992
        session.target_dv_info = DoviInfo(
            profile=7, el_type="FEL", cm_version="v4.0",
            frame_count=src_frames, has_l8=True,
        )
        session.target_frame_count = src_frames
        session.target_type = "trusted_p7_fel_final"
        session.target_preflight_ok = True
        if body.kind == "drive":
            session.target_rpu_source = "drive"
            session.target_rpu_path = f"drive://{body.file_id}/{body.file_name}"
        elif body.kind == "path":
            session.target_rpu_source = "path"
            session.target_rpu_path = body.rpu_path or ""
        else:
            session.target_rpu_source = "mkv"
            session.target_rpu_path = body.source_mkv_path or ""
        save_cmv40_session(session)
        await _cmv40_log(session, f"[DEV] Pre-flight OK simulado · {body.kind}")
        return {"ok": True, "started": True}

    # Validación temprana del body antes de arrancar el task
    if body.kind == "drive" and not body.file_id:
        raise HTTPException(status_code=400, detail="file_id requerido para kind=drive")
    if body.kind == "path" and not body.rpu_path:
        raise HTTPException(status_code=400, detail="rpu_path requerido para kind=path")
    if body.kind == "mkv" and not body.source_mkv_path:
        raise HTTPException(status_code=400, detail="source_mkv_path requerido para kind=mkv")
    if body.kind not in ("drive", "path", "mkv"):
        raise HTTPException(status_code=400, detail=f"kind desconocido: {body.kind}")

    # Si ya hay otra fase corriendo para esta sesión, no disparamos
    lock = _get_cmv40_phase_lock(session.id)
    if lock.locked():
        return {"ok": True, "started": False, "reason": "ya hay otra fase en curso"}

    _cmv40_cancel_flags.pop(session.id, None)

    async def _run():
        async with lock:
            session.running_phase = "preflight"
            session.error_message = ""
            session.target_preflight_ok = False
            save_cmv40_session(session)
            await _cmv40_log(session, "━━━ Inicio fase: preflight ━━━")

            async def _log_cb(msg: str):
                await _cmv40_log(session, msg)

            def _proc_cb(proc):
                _cmv40_proc_register(session.id, proc)

            try:
                # Source preflight primero (idempotente — skip si ya hecho).
                # Validar el origen ANTES del target evita descargar el bin si
                # el MKV no tiene DV.
                from phases.cmv40_pipeline import preflight_source
                await preflight_source(session, log_callback=_log_cb, proc_callback=_proc_cb)

                if body.kind == "drive":
                    await preflight_target_drive(session, body.file_id, body.file_name, _log_cb)
                elif body.kind == "path":
                    await preflight_target_path(session, body.rpu_path, _log_cb)
                else:  # mkv
                    await preflight_target_mkv(session, body.source_mkv_path, _log_cb, _proc_cb)

                # Análisis profundo del bin + decisión Keep/continuar
                avanzar = await _cmv40_preflight_analyze_target(session, _log_cb)
                if avanzar:
                    session.preflight_decision = "ok"
                    session.preflight_message = ""
                    session.target_preflight_ok = True
                    next_hint = (
                        " — auto-pipeline encadenará Fase A a continuación."
                        if session.auto_pipeline
                        else " — auto-pipeline desactivado: pulsa ▶ para lanzar Fase A."
                    )
                    await _cmv40_log(
                        session,
                        f"✓ Fase preflight completada — origen y bin validos.{next_hint}"
                    )
                # Si NO avanzar, la helper ya pobló preflight_decision/message
                # y dejó target_preflight_ok=False.
            except Exception as e:
                # Igual que el resto de fases: error al log de la sesión +
                # error_message para que la UI lo muestre como banner. SIN
                # toast (es ruido — el log del proyecto ya tiene el motivo).
                msg = str(e)
                await _cmv40_log(session, f"✗ Fase preflight FALLÓ: {msg}")
                session.error_message = msg
                session.target_preflight_ok = False
            finally:
                _cmv40_active_procs.pop(session.id, None)
                _cmv40_cancel_flags.pop(session.id, None)
                session.running_phase = None
                save_cmv40_session(session)
        # Fuera del lock: si auto_pipeline está activo y el preflight pasó,
        # encadena Fase A automáticamente. Sin esto, si el cliente disparó
        # este endpoint manualmente (en lugar del orquestador interno), Fase
        # A no arrancaría sola — fragil ante cliente cerrado tras el POST.
        # Mismo patrón que `_cmv40_dispatch_preflight`.
        if session.auto_pipeline and not session.error_message and session.target_preflight_ok:
            asyncio.create_task(_cmv40_dispatch_next_phase(session.id))

    asyncio.create_task(_run())
    return {"ok": True, "started": True}


@app.post(
    "/api/cmv40/{session_id}/preflight-source",
    summary="Pre-flight asíncrono: valida que el MKV origen tenga DV (sin target)",
)
async def cmv40_preflight_source(session_id: str):
    """Sniff de 30s del MKV origen + dovi_tool extract-rpu. Aborta si no hay
    DV. ~10s. Independiente del target — útil cuando el usuario crea un
    proyecto sin elegir target en el modal y quiere validar el origen antes
    de gastar Fase A.

    Devuelve {ok:true, started:true} de inmediato. Background task setea
    running_phase="preflight" hasta terminar."""
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    if DEV_MODE:
        session.source_preflight_ok = True
        save_cmv40_session(session)
        await _cmv40_log(session, "[DEV] Pre-flight source OK simulado")
        return {"ok": True, "started": True}

    lock = _get_cmv40_phase_lock(session.id)
    if lock.locked():
        return {"ok": True, "started": False, "reason": "ya hay otra fase en curso"}

    _cmv40_cancel_flags.pop(session.id, None)

    async def _run():
        async with lock:
            session.running_phase = "preflight"
            session.error_message = ""
            save_cmv40_session(session)
            await _cmv40_log(session, "━━━ Inicio fase: preflight (source-only) ━━━")

            async def _log_cb(msg: str):
                await _cmv40_log(session, msg)

            def _proc_cb(proc):
                _cmv40_proc_register(session.id, proc)

            try:
                from phases.cmv40_pipeline import preflight_source
                await preflight_source(session, log_callback=_log_cb, proc_callback=_proc_cb)
                await _cmv40_log(
                    session,
                    "✓ Fase preflight (source) completada — origen valido"
                )
            except Exception as e:
                msg = str(e)
                await _cmv40_log(session, f"✗ Fase preflight (source) FALLÓ: {msg}")
                session.error_message = msg
                session.source_preflight_ok = False
            finally:
                _cmv40_active_procs.pop(session.id, None)
                _cmv40_cancel_flags.pop(session.id, None)
                session.running_phase = None
                save_cmv40_session(session)

    asyncio.create_task(_run())
    return {"ok": True, "started": True}


@app.post("/api/cmv40/{session_id}/extract", summary="Fase C: demux BL/EL + per-frame data")
async def cmv40_extract(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # ⚠️ DEV MODE
    if DEV_MODE:
        logs = [
            "$ dovi_tool demux source.hevc --bl-out BL.hevc --el-out EL.hevc",
            "Parsing HEVC file...",
            "Found dual-layer DV content.",
            "Demuxing BL (Base Layer)...",
            "BL written: 38.5 GB (3840x2160)",
            "Demuxing EL (Enhancement Layer)...",
            "EL written: 3.82 GB (1920x1080)",
            "$ dovi_tool export -i RPU_source.bin -d all=_export_source.json",
            "Exporting per-frame metadata...",
            "Processed 50000/137952 frames",
            "Processed 100000/137952 frames",
            "Processed 137952/137952 frames",
            "$ dovi_tool export -i RPU_target.bin -d all=_export_target.json",
            "Processed 137992/137992 frames",
            "per_frame_data.json: 6898 puntos (muestreo cada 20 frames)",
        ]
        asyncio.create_task(_dev_simulate_phase(session, "extract", logs,
                                                 CMv40Phase.EXTRACTED, total_seconds=6.0))
        return {"ok": True, "started": True}

    _cmv40_cancel_flags.pop(session_id, None)

    async def _coro(log_cb, proc_cb):
        await run_phase_c_extract(session, log_cb, proc_cb)

    async def _run():
        try:
            await _run_cmv40_phase(session, "extract", _coro, CMv40Phase.EXTRACTED)
        except Exception:
            pass

    asyncio.create_task(_run())
    return {"ok": True, "started": True}


@app.get("/api/cmv40/{session_id}/sync-data", summary="Devuelve per_frame_data.json + métricas")
async def cmv40_sync_data(session_id: str):
    # ⚠️ DEV MODE: el offset depende del estado (corregido o no)
    if DEV_MODE:
        session = load_cmv40_session(session_id)
        if session and session.sync_config is not None:
            data = build_fake_per_frame_data(offset=session.sync_delta)
        else:
            data = build_fake_per_frame_data()
        data["confidence"] = compute_sync_confidence(data)
        return data
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    wd = Path(session.artifacts_dir)
    pf = wd / "per_frame_data.json"
    # Si no existe (target trusted saltó la generación en Fase C), lo
    # generamos on-demand. Esto soporta el caso donde el usuario cambia
    # `trust_override` a 'force_interactive' tras el skip automático.
    # LOCK serializa — múltiples llamadas concurrentes (el frontend podía
    # dispararlas en ráfaga durante transiciones del auto-pipeline) esperan
    # a que termine la primera en vez de lanzar N `dovi_tool export` en
    # paralelo → I/O thrash → timeouts.
    if not pf.exists():
        # Guard: si hay una fase corriendo + el target es trusted, NO regenerar.
        # La unica razon para generar per_frame_data.json en trusted es que
        # el usuario haya forzado force_interactive — y en ese caso no estamos
        # en running_phase porque el flujo se pauso en Fase D. Si llegamos
        # aqui con running_phase != None es una llamada parasita del render
        # del frontend y regenerar superpone ~2 min de dovi_tool export
        # sobre la fase en curso (contamina Fase F inject).
        if session.running_phase and session.target_trust_ok and session.trust_override != "force_interactive":
            raise HTTPException(
                status_code=409,
                detail=("per_frame_data.json omitido por target trusted; "
                        "hay otra fase ejecutandose. No se regenera durante auto-pipeline "
                        "para no solapar dovi_tool export con la fase activa."),
            )
        lock = _cmv40_perframe_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if not pf.exists():   # re-check después del lock (otra llamada lo pudo generar)
                from phases.cmv40_pipeline import _generate_per_frame_data, FPS_EXPORT
                rpu_source = wd / "RPU_source.bin"
                rpu_target = wd / "RPU_target.bin"
                if not (rpu_source.exists() and rpu_target.exists()):
                    raise HTTPException(status_code=404,
                        detail="per_frame_data.json no existe y no están los RPUs — ejecuta Fase A/B/C primero")
                async def _log_cb(msg: str):
                    await _cmv40_log(session, msg)
                est_export = max(10.0, session.source_frame_count / FPS_EXPORT) if session.source_frame_count else 30.0
                # Log claro: si llegamos aqui sin running_phase ni force_interactive,
                # es una carga legitima (usuario expandio Fase D en proyecto quiescente).
                reason = ("force_interactive"
                          if session.trust_override == "force_interactive"
                          else "apertura manual del chart")
                await _cmv40_log(session,
                    f"[sync-data] per_frame_data.json no existe — regenerando on-demand ({reason})")
                try:
                    await _generate_per_frame_data(
                        session, rpu_source, rpu_target, pf, _log_cb,
                        est_export_s=est_export,
                    )
                    # Remover marca de skip si estaba
                    if "per_frame_data_skipped" in (session.phases_skipped or []):
                        session.phases_skipped.remove("per_frame_data_skipped")
                        save_cmv40_session(session)
                except Exception as e:
                    raise HTTPException(status_code=500,
                        detail=f"Fallo al regenerar per_frame_data on-demand: {e}")
    import json as _json
    data = _json.loads(pf.read_text(encoding="utf-8"))
    data["suggested_offset"] = detect_sync_offset(data)
    data["confidence"] = compute_sync_confidence(data)
    return data


class CMv40SyncRequest(BaseModel):
    editor_config: dict


@app.post("/api/cmv40/{session_id}/apply-sync", summary="Fase E: corrección de sincronización")
async def cmv40_apply_sync(session_id: str, body: CMv40SyncRequest):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # Acumular corrección con la previa (si existía)
    def _count_remove(cfg: dict) -> int:
        total = 0
        for r in cfg.get("remove", []):
            if "-" in r:
                a, b = r.split("-")
                total += int(b) - int(a) + 1
        return total

    def _count_duplicate(cfg: dict) -> int:
        return sum(d.get("length", 0) for d in cfg.get("duplicate", []))

    prev_cfg = session.sync_config or {}
    new_cfg = body.editor_config or {}
    total_remove = _count_remove(prev_cfg) + _count_remove(new_cfg)
    total_dup = _count_duplicate(prev_cfg) + _count_duplicate(new_cfg)

    combined_cfg: dict = {}
    if total_remove > 0:
        combined_cfg["remove"] = [f"0-{total_remove - 1}"]
    if total_dup > 0:
        combined_cfg["duplicate"] = [{"source": 0, "offset": 0, "length": total_dup}]

    # ⚠️ DEV MODE
    if DEV_MODE:
        session.sync_config = combined_cfg or None
        # En DEV: target original simulado = source + 40 frames.
        original_target = session.source_frame_count + 40
        session.target_frame_count = original_target - total_remove + total_dup
        session.sync_delta = session.target_frame_count - session.source_frame_count
        # NO cambiamos session.phase — Fase D sigue activa hasta que el usuario confirme
        save_cmv40_session(session)
        await _cmv40_log(session,
            f"[DEV] Corrección acumulada (+{_count_remove(new_cfg)} remove, +{_count_duplicate(new_cfg)} dup). "
            f"Total: remove={total_remove}, dup={total_dup}. Nuevo Δ = {session.sync_delta:+d}"
        )
        return session.model_dump()

    # Sustituimos el editor_config del request por el acumulado
    body.editor_config = combined_cfg
    # Persistimos sync_config aqui para que el frontend lo vea inmediatamente
    # (la respuesta sale antes de que termine la fase real). El backend lo
    # escribira de nuevo al finalizar — idempotente.
    session.sync_config = combined_cfg or None
    save_cmv40_session(session)

    _cmv40_cancel_flags.pop(session_id, None)

    captured_phase = session.phase  # mantenemos fase D activa

    async def _coro(log_cb, proc_cb):
        await run_phase_e_correct_sync(session, body.editor_config, log_cb)

    async def _run():
        try:
            # new_phase = session.phase capturado: Fase D sigue activa, no
            # avanzamos automaticamente — el usuario confirma manualmente.
            await _run_cmv40_phase(session, "correct_sync", _coro, captured_phase)
        except Exception:
            pass

    # Fire-and-forget como extract/inject/remux: la respuesta vuelve al
    # instante, el log fluye via WebSocket. Antes hacia await sobre la fase
    # entera (1-5 min para dovi_tool editor) y el frontend disparaba el
    # toast 'el servidor no responde en 30s' aunque el backend trabajaba ok.
    asyncio.create_task(_run())
    return {"ok": True, "started": True}


@app.post("/api/cmv40/{session_id}/reset-sync", summary="Descarta la corrección y vuelve al target original")
async def cmv40_reset_sync(session_id: str):
    """Borra la corrección aplicada y re-analiza el RPU_target.bin original."""
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # ⚠️ DEV MODE: restaurar target a valor original simulado (source + 40)
    if DEV_MODE:
        session.sync_config = None
        session.target_frame_count = session.source_frame_count + 40
        session.sync_delta = 40
        save_cmv40_session(session)
        await _cmv40_log(session, "[DEV] Corrección descartada — target restaurado a Δ = +40")
        return session.model_dump()

    # Prod: re-analizar RPU_target.bin original para obtener su frame count
    wd = Path(session.artifacts_dir)
    rpu_target = wd / "RPU_target.bin"
    if not rpu_target.exists():
        raise HTTPException(status_code=400, detail="RPU_target.bin no existe")

    # Borrar RPU_synced.bin + editor_config.json
    (wd / "RPU_synced.bin").unlink(missing_ok=True)
    (wd / "editor_config.json").unlink(missing_ok=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            "dovi_tool", "info", "--summary", str(rpu_target),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        from phases.phase_a import _parse_dovi_summary
        dovi_info = _parse_dovi_summary(stdout.decode("utf-8", errors="replace"))
        session.target_frame_count = dovi_info.frame_count
        session.sync_delta = dovi_info.frame_count - session.source_frame_count
    except Exception as e:
        _logger.warning("Re-análisis del RPU target falló: %s", e)

    session.sync_config = None
    save_cmv40_session(session)
    await _cmv40_log(session, "Corrección descartada — target restaurado a estado original")

    # Regenerar per_frame_data.json desde el RPU target original
    from phases.cmv40_pipeline import _generate_per_frame_data, FPS_EXPORT
    rpu_source = wd / "RPU_source.bin"
    per_frame  = wd / "per_frame_data.json"
    if rpu_source.exists() and rpu_target.exists():
        async def _log_cb(msg: str):
            await _cmv40_log(session, msg)
        est_export = max(10.0, session.source_frame_count / FPS_EXPORT) if session.source_frame_count else 30.0
        try:
            await _generate_per_frame_data(
                session, rpu_source, rpu_target, per_frame, _log_cb,
                est_export_s=est_export,
            )
        except Exception as e:
            _logger.warning("Regeneración de per_frame_data falló: %s", e)
    return session.model_dump()


@app.post("/api/cmv40/{session_id}/mark-synced", summary="Marca sync OK sin corrección")
async def cmv40_mark_synced(session_id: str):
    """Usuario confirma que no hace falta corrección (Δ=0 y curvas alineadas).
    Si el target es trusted, anotamos `sync_verification_pause` en phases_skipped
    para que la UI muestre Fase D como "omitida" incluso tras recargar."""
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    session.phase = CMv40Phase.SYNC_VERIFIED
    trusted_auto = session.target_trust_ok and session.trust_override != "force_interactive"
    if trusted_auto and "sync_verification_pause" not in session.phases_skipped:
        session.phases_skipped.append("sync_verification_pause")
    save_cmv40_session(session)
    return session.model_dump()


@app.post("/api/cmv40/{session_id}/inject", summary="Fase F: inyecta RPU en EL")
async def cmv40_inject(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # ⚠️ DEV MODE
    if DEV_MODE:
        rpu_file = "RPU_synced.bin" if session.sync_config else "RPU_target.bin"
        logs = [
            f"$ dovi_tool inject-rpu -i EL.hevc --rpu-in {rpu_file} -o EL_injected.hevc",
            "Reading EL.hevc...",
            "Reading RPU file...",
            "Verifying frame count: EL has 137952 frames, RPU has 137952 frames ✓",
            "Injecting RPU into HEVC bitstream...",
            "Processed 50000/137952 frames",
            "Processed 100000/137952 frames",
            "Processed 137952/137952 frames",
            "EL_injected.hevc: 3.82 GB",
        ]
        asyncio.create_task(_dev_simulate_phase(session, "inject", logs,
                                                 CMv40Phase.INJECTED, total_seconds=3.0))
        return {"ok": True, "started": True}

    _cmv40_cancel_flags.pop(session_id, None)

    async def _coro(log_cb, proc_cb):
        await run_phase_f_inject(session, log_cb, proc_cb)

    async def _run():
        try:
            await _run_cmv40_phase(session, "inject", _coro, CMv40Phase.INJECTED)
        except Exception:
            pass

    asyncio.create_task(_run())
    return {"ok": True, "started": True}


@app.post("/api/cmv40/{session_id}/remux", summary="Fase G: mux BL+EL + remux final a MKV")
async def cmv40_remux(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # ⚠️ DEV MODE
    if DEV_MODE:
        logs = [
            "$ dovi_tool mux --bl BL.hevc --el EL_injected.hevc -o DV_dual.hevc",
            "Combining Base Layer and Enhancement Layer...",
            "Processed 50000/137952 frames",
            "Processed 100000/137952 frames",
            "Processed 137952/137952 frames",
            "DV_dual.hevc: 42.3 GB",
            f"$ mkvmerge --gui-mode -o output.mkv --title \"{session.output_mkv_name.removesuffix('.mkv')}\" DV_dual.hevc --no-video {session.source_mkv_path}",
            "mkvmerge v81.0.0 ('Demons') 64-bit",
            "Progress: 10%",
            "Progress: 25%",
            "Progress: 50%",
            "Progress: 75%",
            "Progress: 90%",
            "Progress: 100%",
            "output.mkv: 48.5 GB",
        ]
        asyncio.create_task(_dev_simulate_phase(session, "remux", logs,
                                                 CMv40Phase.REMUXED, total_seconds=7.0))
        return {"ok": True, "started": True}

    _cmv40_cancel_flags.pop(session_id, None)

    async def _coro(log_cb, proc_cb):
        await run_phase_g_remux(session, log_cb, proc_cb)

    async def _run():
        try:
            await _run_cmv40_phase(session, "remux", _coro, CMv40Phase.REMUXED)
        except Exception:
            pass

    asyncio.create_task(_run())
    return {"ok": True, "started": True}


@app.post("/api/cmv40/{session_id}/validate", summary="Fase H: validación final y move a output")
async def cmv40_validate(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    # ⚠️ DEV MODE
    if DEV_MODE:
        session.output_mkv_path = f"/mnt/output/{session.output_mkv_name}"
        session.phase = CMv40Phase.DONE
        save_cmv40_session(session)
        await _cmv40_log(session, "[DEV] Fase H OK — MKV CMv4.0 validado")
        return session.model_dump()

    _cmv40_cancel_flags.pop(session_id, None)

    async def _coro(log_cb, proc_cb):
        result = await run_phase_h_validate(session, log_cb, proc_cb)
        session.output_log.append(f"Validación final: {result}")

    async def _run():
        try:
            await _run_cmv40_phase(session, "validate", _coro, CMv40Phase.DONE)
        except Exception:
            pass

    asyncio.create_task(_run())
    return {"ok": True, "started": True}


# ── WebSocket de CMv4.0 ──────────────────────────────────────────────────────

@app.websocket("/ws/cmv40/{session_id}")
async def cmv40_ws(websocket: WebSocket, session_id: str):
    """Stream de líneas de log en vivo. NO envía replay histórico — el
    frontend hidrata el log permanente desde `session.output_log` via el
    GET REST que carga el proyecto, y trackea un watermark para que cada
    línea recibida por el WS se añada exactamente una vez al DOM. Con
    replay aquí se duplicarían las líneas que ya estaban hidratadas (visible
    como repetición al final del log al reconectar tras Mac sleep).
    """
    await websocket.accept()
    _cmv40_ws_connections.setdefault(session_id, []).append(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive (ignoramos mensajes del cliente)
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in _cmv40_ws_connections.get(session_id, []):
            _cmv40_ws_connections[session_id].remove(websocket)


# ── DEV MODE: simulación de ejecución fake ────────────────────────────────────
# Bloque solo definido cuando DEV_MODE=1. En producción no se importa nada
# de aquí — la indentación if-DEV_MODE asegura que ni siquiera se compila el
# random ni las helpers _run_fake_pipeline.
if DEV_MODE:
    import random

    async def _run_fake_pipeline(session_id: str) -> None:
        """
        Simula un pipeline D+E completo emitiendo mensajes WS reales con delays.
        Replica exactamente el mismo flujo de señales que _run_pipeline:
          [Fase D] → Progress: X% → [Fase E] → __DONE__ (o __ERROR__)
        """
        session = load_session(session_id)
        if not session:
            return

        session.status              = "running"
        session.output_log          = []
        session.execution_started_at = datetime.now(timezone.utc)
        session.output_mkv_path     = None
        save_session(session)

        async def log(msg: str) -> None:
            if not msg.startswith("Progress:"):
                ts = datetime.now().strftime("%H:%M:%S")  # hora local (TZ del contenedor)
                msg = f"[{ts}] {msg}"
            session.output_log.append(msg)
            save_session(session)
            for ws in list(_ws_connections.get(session_id, [])):
                asyncio.create_task(_send_ws_with_timeout(_ws_connections, session_id, ws, msg))

        will_error   = random.random() < 0.20
        do_reorder   = random.random() < 0.50  # simular 50% reorder
        _ps: dict[str, datetime] = {}
        _pe: dict[str, datetime] = {}

        try:
            await log(f"[Pipeline] Iniciando extracción de {session.iso_path}")

            # ── Montar ISO ────────────────────────────────────────────
            _ps["mount"] = datetime.now(timezone.utc)
            await asyncio.sleep(0.2)
            await log("[Montando ISO] mount -t udf -o ro,loop …")
            await asyncio.sleep(0.4)
            await log("[Montando ISO] ISO montado en: /mnt/bd/fake_mount_12345")
            _pe["mount"] = datetime.now(timezone.utc)
            await asyncio.sleep(0.1)
            await log("[Fase D] MPLS seleccionado: /mnt/bd/fake_mount_12345/BDMV/PLAYLIST/00800.mpls")

            if do_reorder:
                # ── RUTA DIRECTA: MPLS → MKV final ───────────────────
                await log("[Pipeline] Pistas reordenadas/excluidas → ruta directa (MPLS → MKV final)")
                _ps["extract"] = datetime.now(timezone.utc)
                await log("[Fase E] mkvmerge directo: MPLS → MKV final")
                await asyncio.sleep(0.5)
                for pct in range(5, 101, 5):
                    await log(f"Progress: {pct}%")
                    await asyncio.sleep(0.25)
                if will_error:
                    raise RuntimeError("[DEV] Error simulado — mkvmerge falló")
                _pe["extract"] = datetime.now(timezone.utc)
            else:
                # ── RUTA INTERMEDIO: MPLS → intermedio → mkvpropedit ──
                await log("[Pipeline] Sin reordenación → ruta intermedio (mkvpropedit in-place)")
                _ps["extract"] = datetime.now(timezone.utc)
                await log("[Fase D] mkvmerge: extrayendo todas las pistas…")
                await asyncio.sleep(0.5)
                for pct in range(5, 101, 5):
                    await log(f"Progress: {pct}%")
                    await asyncio.sleep(0.25)
                if will_error:
                    raise RuntimeError("[DEV] Error simulado — mkvmerge falló")
                _pe["extract"] = datetime.now(timezone.utc)
                await log("[Fase D] MKV intermedio generado: /mnt/tmp/fake_intermediate.mkv")

                _ps["write"] = datetime.now(timezone.utc)
                await log("[Fase E] mkvpropedit in-place: configurando metadatos…")
                await asyncio.sleep(0.4)
                await log("[Fase E] mkvpropedit: pistas + capítulos configurados")
                await asyncio.sleep(0.3)
                await log("[Fase E] MKV movido a: /mnt/output/")
                _pe["write"] = datetime.now(timezone.utc)

            mkv_out = f"/mnt/output/{session.mkv_name or 'fake_output.mkv'}"
            await log(f"[Pipeline] Completado: {mkv_out}")

            session.status          = "done"
            session.last_executed   = datetime.now(timezone.utc)
            session.output_mkv_path = mkv_out

        except Exception as e:
            _logger.exception("Error en pipeline para sesión %s", session.id)
            session.status        = "error"
            session.error_message = str(e)
            await log(f"[ERROR] {e}")

        finally:
            # Simular desmontaje
            _ps["unmount"] = datetime.now(timezone.utc)
            await log("[Desmontando ISO] umount loop device…")
            await asyncio.sleep(0.2)
            _pe["unmount"] = datetime.now(timezone.utc)
            await log("[Pipeline] ISO desmontado")

            _append_execution_record(session, _ps, _pe)
            save_session(session)
            sig = "__DONE__" if session.status == "done" else "__ERROR__"
            for ws in list(_ws_connections.get(session_id, [])):
                asyncio.create_task(_send_ws_with_timeout(_ws_connections, session_id, ws, sig))

    # Registrar la función fake como pipeline del queue_manager
    queue_manager.set_run_fn(_run_fake_pipeline)

    @app.post("/api/dev/simulate", summary="[DEV] Encola sesiones fake para simular ejecución")
    async def dev_simulate(body: dict = {}):
        """
        ⚠️ Solo disponible con DEV_MODE=1.

        Encola una o varias sesiones fake para simular el pipeline completo.

        Body (opcional):
          { "session_ids": ["id1", "id2"] }   → encola las indicadas
          {}                                   → encola las 2 primeras sesiones pending
        """
        from storage import list_sessions
        ids: list[str] = body.get("session_ids", [])

        if not ids:
            # Auto-seleccionar la primera sesión disponible.
            # Prioridad: pending/done primero, luego error (reseteable).
            # Excluir las que ya están corriendo o encoladas ahora mismo.
            active = {queue_manager.get_status()["running"]} | set(queue_manager.get_status()["queue"])
            active.discard(None)
            all_sessions = list_sessions()
            priority = [s for s in all_sessions if s.status in ("pending", "done") and s.id not in active]
            fallback  = [s for s in all_sessions if s.status == "error" and s.id not in active]
            candidates = (priority + fallback)[:1]
            ids = [s.id for s in candidates]

        if not ids:
            return {"ok": False, "detail": "No hay sesiones disponibles (todas en ejecución o en cola)"}

        enqueued = []
        for sid in ids:
            session = load_session(sid)
            if not session:
                continue
            session.status     = "queued"
            session.output_log = []
            session.error_message = None
            save_session(session)
            await queue_manager.enqueue(sid)
            enqueued.append(sid)

        return {"ok": True, "enqueued": enqueued, **queue_manager.get_status()}


@app.websocket("/ws/queue")
async def queue_websocket(websocket: WebSocket):
    """WebSocket para recibir updates en tiempo real del estado de la cola."""
    await websocket.accept()
    _queue_ws_clients.add(websocket)
    # Enviar estado actual al conectar
    try:
        await websocket.send_text(json.dumps(queue_manager.get_status()))
    except Exception:
        pass
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _queue_ws_clients.discard(websocket)


# ── WebSocket para streaming de output ───────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """
    WebSocket para recibir el output del proceso en tiempo real.

    Al conectar, se envía primero el log histórico (permite reconectar
    sin perder el output anterior). El mensaje especial ``__DONE__``
    indica que el proceso terminó.
    """
    await websocket.accept()
    _ws_connections.setdefault(session_id, []).append(websocket)

    session = load_session(session_id)
    if session:
        for line in session.output_log:
            try:
                await websocket.send_text(line)
            except Exception:
                break

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if session_id in _ws_connections:
            _ws_connections[session_id] = [
                ws for ws in _ws_connections[session_id] if ws != websocket
            ]


# ── Settings editables desde la UI ──────────────────────────────────────────

class SettingsUpdate(BaseModel):
    """Payload parcial: `None` = no tocar, `""` = borrar/restaurar, otro = setear."""
    tmdb_api_key: str | None = None
    google_api_key: str | None = None
    cmv40_drive_folder_url: str | None = None
    cmv40_sheet_url: str | None = None


@app.get("/api/settings", summary="Lee settings persistentes (sin exponer secretos crudos)")
async def get_settings():
    from services.settings_store import get_public_settings
    return get_public_settings()


@app.post("/api/settings", summary="Actualiza settings persistentes")
async def update_settings(body: SettingsUpdate):
    from services.settings_store import (
        get_public_settings,
        update_tmdb_api_key, update_google_api_key,
        update_cmv40_drive_folder_url, update_cmv40_sheet_url,
    )
    update_tmdb_api_key(body.tmdb_api_key)
    update_google_api_key(body.google_api_key)
    update_cmv40_drive_folder_url(body.cmv40_drive_folder_url)
    update_cmv40_sheet_url(body.cmv40_sheet_url)
    return get_public_settings()


@app.post("/api/settings/test-tmdb", summary="Valida una TMDb API key contra el endpoint oficial")
async def test_tmdb_key(body: SettingsUpdate):
    from services.tmdb import test_api_key
    key = body.tmdb_api_key or ""
    ok, msg = await test_api_key(key)
    return {"ok": ok, "message": msg}


@app.post("/api/settings/test-google",
          summary="Valida una Google API key (Drive + Sheets)")
async def test_google_key(body: SettingsUpdate):
    from services.rec999_drive import test_api_key
    key = body.google_api_key or ""
    ok, msg = await test_api_key(key)
    return {"ok": ok, "message": msg}


@app.post("/api/settings/test-drive-folder",
          summary="Valida el URL/ID del folder Drive del repo DoviTools")
async def test_drive_folder(body: SettingsUpdate):
    from services.rec999_drive import test_folder_access
    folder = body.cmv40_drive_folder_url or ""
    ok, msg, bin_count = await test_folder_access(folder)
    return {"ok": ok, "message": msg, "bin_count_sample": bin_count}


@app.post("/api/settings/test-sheet",
          summary="Valida el URL del sheet de recomendaciones DoviTools")
async def test_sheet_url(body: SettingsUpdate):
    from services.rec999_drive import test_sheet_access
    url = body.cmv40_sheet_url or ""
    ok, msg, rows = await test_sheet_access(url)
    return {"ok": ok, "message": msg, "row_count": rows}


# ── Mantenimiento: scan + cleanup de huérfanos ───────────────────────────────
# Complementa al auto-cleanup de arranque (_cleanup_obvious_orphans_at_startup)
# para los casos donde el usuario quiere ver y aprobar la limpieza antes de
# borrar (workdirs CMv4.0 grandes, .mkv.tmp del remux, etc).

def _scan_orphans() -> list[dict]:
    """Devuelve la lista de huérfanos categorizados con tamaño + edad."""
    import time as _t
    import shutil as _sh

    out: list[dict] = []

    def _dir_size(p: Path) -> int:
        total = 0
        try:
            for f in p.rglob("*"):
                if f.is_file():
                    try: total += f.stat().st_size
                    except OSError: pass
        except Exception:
            pass
        return total

    now = _t.time()

    # 1. Workdirs CMv4.0 sin sesión JSON correspondiente
    cmv40_work = Path("/mnt/tmp/cmv40")
    cmv40_cfg = Path("/config/cmv40")
    if cmv40_work.exists() and cmv40_work.is_dir():
        valid_ids: set[str] = set()
        if cmv40_cfg.exists():
            try:
                for jf in cmv40_cfg.glob("*.json"):
                    valid_ids.add(jf.stem)
            except Exception:
                pass
        try:
            for wd in cmv40_work.iterdir():
                if not wd.is_dir():
                    continue
                if wd.name in valid_ids:
                    continue
                size = _dir_size(wd)
                try: age = int(now - wd.stat().st_mtime)
                except OSError: age = 0
                out.append({
                    "category": "cmv40_workdir",
                    "label": "Workdir CMv4.0 sin sesión",
                    "path": str(wd),
                    "size_bytes": size,
                    "age_seconds": age,
                    "safe": True,
                    "reason": f"No existe /config/cmv40/{wd.name}.json — sesión borrada o nunca persistida",
                })
        except Exception:
            pass

    # 2. Mount points de ISO sin entry en /proc/mounts
    mount_base = Path("/mnt/bd")
    if mount_base.exists():
        mounted: set[str] = set()
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) > 1:
                        mounted.add(parts[1])
        except Exception:
            pass
        try:
            for mp in mount_base.iterdir():
                if not mp.is_dir() or str(mp) in mounted:
                    continue
                size = _dir_size(mp)
                try: age = int(now - mp.stat().st_mtime)
                except OSError: age = 0
                out.append({
                    "category": "iso_mount_zombie",
                    "label": "Mount point ISO huérfano",
                    "path": str(mp),
                    "size_bytes": size,
                    "age_seconds": age,
                    "safe": True,
                    "reason": "Directorio sin montaje activo (umount falló o nunca se hizo)",
                })
        except Exception:
            pass

    # 3. Light-profile tmps en /tmp
    try:
        for lp in Path("/tmp").glob("lightprof_*"):
            if not lp.is_dir():
                continue
            size = _dir_size(lp)
            try: age = int(now - lp.stat().st_mtime)
            except OSError: age = 0
            out.append({
                "category": "lightprofile_tmp",
                "label": "Tmp del análisis de luminancia",
                "path": str(lp),
                "size_bytes": size,
                "age_seconds": age,
                "safe": age > 3600,
                "reason": "Cancelación o crash durante extracción de luminancia (Tab 2)"
                          + ("" if age > 3600 else " — RECIENTE, podría estar activo"),
            })
    except Exception:
        pass

    # 4. .mkv.tmp del remux (Fase G)
    output_base = Path("/mnt/output")
    if output_base.exists():
        try:
            for tf in output_base.glob("*.mkv.tmp"):
                if not tf.is_file():
                    continue
                try: size = tf.stat().st_size
                except OSError: continue
                try: age = int(now - tf.stat().st_mtime)
                except OSError: age = 0
                out.append({
                    "category": "remux_mkv_tmp",
                    "label": "Remux .mkv.tmp incompleto",
                    "path": str(tf),
                    "size_bytes": size,
                    "age_seconds": age,
                    "safe": age > 3600,
                    "reason": "Fase G de CMv4.0 abortada o aún en curso"
                              + ("" if age > 3600 else " — RECIENTE, podría estar siendo escrito"),
                })
        except Exception:
            pass

    # 5. Cache MKV (Tab 2) — 4 sub-categorías:
    #    (a) huérfano: original_file_path no existe en disco
    #    (b) quality basura: payload con frames=0 (bug timeout 60s histórico)
    #    (c) stale-version: versions.basic|quality != actual
    #    (d) corrupt: JSON inválido
    # Los caches válidos NO se listan — el scan solo muestra lo que sobra.
    try:
        from storage import list_mkv_audit_entries
        from phases.mkv_analyze import (
            CACHE_VERSION_BASIC as _CVB, CACHE_VERSION_QUALITY as _CVQ,
            _quality_payload_is_valid as _qpv,
        )
        for entry in list_mkv_audit_entries():
            cache_path = entry["cache_path"]
            size = entry["size_bytes"]
            age = entry["age_seconds"]
            # (d) corrupt
            if entry.get("corrupt"):
                out.append({
                    "category": "mkv_cache_corrupt",
                    "label": "Cache MKV corrupto (JSON inválido)",
                    "path": cache_path,
                    "size_bytes": size,
                    "age_seconds": age,
                    "safe": True,
                    "reason": entry.get("error", "JSON corrupto"),
                })
                continue
            # (a) huérfano — el MKV original ya no existe
            orig = entry.get("original_file_path")
            if orig and not Path(orig).exists():
                out.append({
                    "category": "mkv_cache_orphan",
                    "label": "Cache MKV huérfano (fichero borrado/movido)",
                    "path": cache_path,
                    "size_bytes": size,
                    "age_seconds": age,
                    "safe": True,
                    "reason": f"Fichero original ya no existe: {orig}",
                })
                continue
            # (b) quality basura
            if entry.get("quality_present"):
                quality_summary = {
                    "quality_total_frames_rpu": entry.get("quality_total_frames"),
                    "quality_classification": entry.get("quality_classification"),
                }
                if not _qpv(quality_summary):
                    out.append({
                        "category": "mkv_cache_invalid_quality",
                        "label": "Cache MKV con auditoría inválida",
                        "path": cache_path,
                        "size_bytes": size,
                        "age_seconds": age,
                        "safe": True,
                        "reason": (
                            f"Bloque quality basura (frames={entry.get('quality_total_frames')}, "
                            f"classification={entry.get('quality_classification')!r}) — "
                            f"borrar permite relanzar la auditoría limpia"
                        ),
                    })
                    continue
            # (c) versions obsoletas
            versions = entry.get("versions") or {}
            v_basic = versions.get("basic")
            v_quality = versions.get("quality")
            stale_msgs = []
            if v_basic is not None and v_basic != _CVB:
                stale_msgs.append(f"basic v{v_basic} (actual v{_CVB})")
            if v_quality is not None and v_quality != _CVQ:
                stale_msgs.append(f"quality v{v_quality} (actual v{_CVQ})")
            if stale_msgs:
                out.append({
                    "category": "mkv_cache_stale_version",
                    "label": "Cache MKV con versión obsoleta",
                    "path": cache_path,
                    "size_bytes": size,
                    "age_seconds": age,
                    "safe": True,
                    "reason": "Mejora del clasificador desde el último análisis: "
                              + " · ".join(stale_msgs),
                })
    except Exception as e:
        _logger.warning("[scan_orphans] escaneo de mkv_audits falló: %s", e)

    return out


def _delete_orphan_path(path_str: str, allowed_prefixes: list[str]) -> tuple[bool, int, str]:
    """Borra un huérfano con validación de prefix (whitelist). Devuelve
    (ok, bytes_freed, error_msg)."""
    import shutil as _sh

    # Validación: el path tiene que estar bajo uno de los roots permitidos
    if not any(path_str.startswith(prefix) for prefix in allowed_prefixes):
        return (False, 0, "path fuera de los roots permitidos")
    p = Path(path_str)
    if not p.exists():
        return (False, 0, "path no existe")
    try:
        if p.is_file():
            try: size = p.stat().st_size
            except OSError: size = 0
            p.unlink()
            return (True, size, "")
        if p.is_dir():
            size = 0
            try:
                for f in p.rglob("*"):
                    if f.is_file():
                        try: size += f.stat().st_size
                        except OSError: pass
            except Exception:
                pass
            _sh.rmtree(p, ignore_errors=False)
            return (True, size, "")
    except Exception as e:
        return (False, 0, str(e))
    return (False, 0, "tipo de path desconocido")


@app.get("/api/cleanup/scan", summary="Scan de huérfanos sin borrar nada")
async def cleanup_scan_endpoint():
    """Devuelve la lista de huérfanos detectados (workdirs CMv4.0 sin sesión,
    mount points ISO zombies, lightprof tmps, .mkv.tmp incompletos). Solo
    lectura — para borrar usar POST /api/cleanup/execute."""
    items = _scan_orphans()
    return {
        "items": items,
        "total_count": len(items),
        "total_bytes": sum(i["size_bytes"] for i in items),
        "safe_count": sum(1 for i in items if i["safe"]),
        "safe_bytes": sum(i["size_bytes"] for i in items if i["safe"]),
    }


class CleanupExecuteRequest(BaseModel):
    paths: list[str]


@app.post("/api/cleanup/execute", summary="Borra huérfanos seleccionados")
async def cleanup_execute_endpoint(body: CleanupExecuteRequest):
    """Borra los paths indicados. Solo se aceptan paths bajo prefixes
    conocidos (/mnt/tmp/cmv40/, /mnt/bd/, /tmp/lightprof_, /mnt/output/*.mkv.tmp,
    /config/mkv_audits/). Cada item devuelve {ok, freed, error}."""
    ALLOWED_PREFIXES = [
        "/mnt/tmp/cmv40/",
        "/mnt/bd/",
        "/tmp/lightprof_",
        "/mnt/output/",       # solo .mkv.tmp, validamos abajo
        "/config/mkv_audits/", # cache Tab 2 (orphans, basura, stale-version)
    ]
    deleted = []
    failed = []
    total_freed = 0
    for path in body.paths or []:
        # Salvaguarda extra para /mnt/output/: solo .mkv.tmp
        if path.startswith("/mnt/output/") and not path.endswith(".mkv.tmp"):
            failed.append({"path": path, "error": "/mnt/output/ solo permite borrar *.mkv.tmp"})
            continue
        # Salvaguarda extra para /config/mkv_audits/: solo .json
        if path.startswith("/config/mkv_audits/") and not path.endswith(".json"):
            failed.append({"path": path, "error": "/config/mkv_audits/ solo permite borrar *.json"})
            continue
        ok, freed, err = _delete_orphan_path(path, ALLOWED_PREFIXES)
        if ok:
            deleted.append({"path": path, "freed_bytes": freed})
            total_freed += freed
            _logger.info("[Cleanup] removed %s (%d bytes)", path, freed)
        else:
            failed.append({"path": path, "error": err})
            _logger.warning("[Cleanup] failed %s: %s", path, err)
    return {
        "deleted": deleted,
        "failed": failed,
        "total_freed_bytes": total_freed,
    }


# ── Health check ─────────────────────────────────────────────────────────────

@app.get("/api/health", summary="Health check para Docker")
async def health():
    """Endpoint ligero para el health check de Docker. Devuelve 200 si la app responde."""
    return {"status": "ok"}


# ── Versión de la app + chequeo de actualizaciones ───────────────────────────

import re as _re_version

_VERSION_CACHE: dict = {"value": None}

def _resolve_app_version() -> dict:
    """Resuelve la version actual de la app:
      - En Docker (build con args): lee APP_VERSION + APP_COMMIT del env.
      - En dev local: ejecuta `git describe --tags --always --dirty`.
    Cachea el resultado en memoria — la version no cambia en runtime.

    Devuelve:
      {
        version: str,         # 'v2.1.3' | 'v2.1.3-5-g7d3e8cb' | 'dev-abc1234' | 'dev'
        commit: str,          # SHA full o '' si no disponible
        is_tagged: bool,      # True si version es exactamente un tag (no past-tag, no dev)
        is_dirty: bool,       # True si tree dirty (solo dev local)
        is_dev: bool,         # True si version no se resolvio a un tag
      }
    """
    if _VERSION_CACHE["value"] is not None:
        return _VERSION_CACHE["value"]

    env_version = os.environ.get("APP_VERSION", "").strip()
    env_commit  = os.environ.get("APP_COMMIT", "").strip()

    version = env_version or "dev"
    commit  = env_commit
    is_dirty = False

    # Si el env no tiene version utilizable, prueba ficheros bakeados en
    # build (Docker multi-stage version-detector escribe /app/VERSION +
    # /app/COMMIT con datos de .git del build context). Cubre el caso
    # `compose up -d --build` sin pasar APP_VERSION como build-arg.
    if version in ("", "dev", "unknown"):
        try:
            vfile = Path(__file__).resolve().parent / "VERSION"
            if vfile.exists():
                v = vfile.read_text().strip()
                if v and v not in ("dev", "unknown"):
                    version = v
        except Exception:
            pass
        if not commit:
            try:
                cfile = Path(__file__).resolve().parent / "COMMIT"
                if cfile.exists():
                    c = cfile.read_text().strip()
                    if c and c != "unknown":
                        commit = c
            except Exception:
                pass

    # Ultimo fallback: git describe directo (dev local con .git accesible).
    if version in ("", "dev", "unknown"):
        try:
            import subprocess
            git_root = Path(__file__).resolve().parent.parent
            result = subprocess.run(
                ["git", "describe", "--tags", "--always", "--dirty"],
                cwd=git_root, capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                version = result.stdout.strip()
            if not commit:
                sha_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=git_root, capture_output=True, text=True, timeout=5,
                )
                if sha_result.returncode == 0:
                    commit = sha_result.stdout.strip()
        except Exception:
            pass

    is_dirty = version.endswith("-dirty")
    # Tagged exacto: 'vX.Y.Z' (semver puro, sin sufijo de commits/dirty)
    is_tagged = bool(_re_version.match(r"^v?\d+\.\d+\.\d+$", version))
    is_dev = not is_tagged
    # Distinto de is_dev: refleja el flag DEV_MODE de runtime (fixtures
    # activos, ./run_local.sh, etc.). Builds en NAS post-tag (commits despues
    # del ultimo release) tienen is_dev=True pero NO is_dev_mode=True — ahi
    # NO queremos exponer tooling de simulacion de versiones.
    from dev_fixtures import DEV_MODE as _DEV_MODE_FLAG
    is_dev_mode = bool(_DEV_MODE_FLAG)

    info = {
        "version": version,
        "commit": commit[:12] if commit else "",
        "commit_full": commit,
        "is_tagged": is_tagged,
        "is_dirty": is_dirty,
        "is_dev": is_dev,
        "is_dev_mode": is_dev_mode,
    }
    _VERSION_CACHE["value"] = info
    return info


@app.get("/api/version", summary="Versión actual de la app")
async def app_version():
    return _resolve_app_version()


_UPDATE_CHECK_CACHE_PATH = Path(os.environ.get("CONFIG_DIR", "/config")) / "update_check_cache.json"
_UPDATE_CHECK_TTL_S = 3600  # 1 hora — la API publica de GitHub limita a 60 req/h sin auth

def _semver_tuple(v: str) -> tuple[int, int, int]:
    """Extrae (major, minor, patch) de un tag tipo 'v2.1.3' o '2.1.3'."""
    m = _re_version.match(r"^v?(\d+)\.(\d+)\.(\d+)", v.strip())
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _semver_gt(a: str, b: str) -> bool:
    """True si a > b (semver). 'dev'/inválido se considera < cualquier tag."""
    return _semver_tuple(a) > _semver_tuple(b)


@app.get("/api/version/check-updates", summary="Comprueba si hay una versión más reciente en GHCR (via GitHub releases)")
async def app_version_check_updates(force: bool = False, simulate_current: str = ""):
    """Consulta la API publica de GitHub releases (sin auth, 60 req/h por IP).
    Cachea el resultado en /config/update_check_cache.json con TTL 1h. El
    parametro `force=true` ignora el cache y refresca.

    `simulate_current` (modo dev): override de la version actual para probar
    la UI de update available. Ej. con simulate_current=v2.0.0 y la peli ya
    en v2.1.3 publicada, el banner aparece como si el usuario tuviera v2.0.0.

    Devuelve:
      {
        current: str,            # version actual ('v2.1.3' o 'dev-...')
        latest: str | null,      # tag del ultimo release publicado en GH
        update_available: bool,
        release_url: str,        # URL del release en GitHub
        release_notes: str,      # body del release (markdown)
        published_at: str,
        checked_at: str,
        cached: bool,            # True si vino del disco, False si fresh
        ignored_version: str,    # version que el usuario marco como 'ignorar' (si aplica)
      }
    """
    import time as _time
    current_info = _resolve_app_version()
    current = current_info["version"]
    simulated = False
    if simulate_current.strip():
        current = simulate_current.strip()
        simulated = True

    # Lee version ignorada por el usuario en settings
    from services.settings_store import get_settings_value
    ignored_version = ""
    try:
        ignored_version = get_settings_value("update_ignored_version", "") or ""
    except Exception:
        pass

    # Cache lookup
    cached_data = None
    # En modo simulado siempre saltamos cache (queremos ver el resultado
    # exacto con la version overrideada) y NO persistimos el cache nuevo.
    if not force and not simulated and _UPDATE_CHECK_CACHE_PATH.exists():
        try:
            cached_data = json.loads(_UPDATE_CHECK_CACHE_PATH.read_text(encoding="utf-8"))
            age = _time.time() - cached_data.get("fetched_at", 0)
            if age < _UPDATE_CHECK_TTL_S:
                latest = cached_data.get("latest", "")
                update_available = bool(latest) and _semver_gt(latest, current)
                # Filtra pending_releases del cache para los > current
                # (porque el cache se hizo en otro momento, current cambia)
                cached_pending = cached_data.get("pending_releases", []) or []
                cur_t = _semver_tuple(current)
                pending_releases_filtered = [
                    r for r in cached_pending
                    if _semver_tuple(r.get("tag", "")) > cur_t
                ]
                return {
                    "current": current,
                    "latest": latest,
                    "update_available": update_available and latest != ignored_version,
                    "release_url": cached_data.get("release_url", ""),
                    "release_notes": cached_data.get("release_notes", ""),
                    "pending_releases": pending_releases_filtered,
                    "published_at": cached_data.get("published_at", ""),
                    "checked_at": cached_data.get("checked_at", ""),
                    "cached": True,
                    "simulated": simulated,
                    "ignored_version": ignored_version,
                }
        except Exception:
            cached_data = None

    # Fetch fresh desde GitHub. Estrategia:
    # 1. /releases?per_page=30 — preferido. Una sola llamada trae notas
    #    de TODOS los releases publicados, incluyendo intermedios entre
    #    current y latest. Permite mostrar el changelog completo pendiente.
    # 2. /tags fallback — si no hay releases formales, listamos tags y
    #    no hay notas que mostrar.
    import httpx
    repo = "Aldicarus/hdo-iso-converter"
    data = {}
    release_notes_str = ""
    release_url_str = ""
    published_at_str = ""
    latest_tag = ""
    pending_releases: list[dict] = []
    fetch_error = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {"Accept": "application/vnd.github+json"}

            # Intento 1: /releases (lista completa con notas)
            try:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/releases?per_page=30",
                    headers=headers,
                )
                if resp.status_code == 200:
                    releases_list = resp.json() or []
                    # Filtrar a tags semver, no draft, no prerelease
                    semver_releases = []
                    for r in releases_list:
                        if r.get("draft") or r.get("prerelease"):
                            continue
                        tag = (r.get("tag_name") or "").strip()
                        if _re_version.match(r"^v?\d+\.\d+\.\d+$", tag):
                            semver_releases.append({
                                "tag": tag,
                                "body": r.get("body", "") or "",
                                "url": r.get("html_url", "") or "",
                                "published_at": r.get("published_at", "") or "",
                            })
                    semver_releases.sort(key=lambda r: _semver_tuple(r["tag"]), reverse=True)
                    if semver_releases:
                        top = semver_releases[0]
                        latest_tag = top["tag"]
                        release_url_str = top["url"]
                        release_notes_str = top["body"]
                        published_at_str = top["published_at"]

                        # Pending = todos los releases > current. Persistimos
                        # los TOP 30 (el filtro por current se aplica en cada
                        # call al cache para que sirva a clientes con distintas
                        # versiones instaladas).
                        cur_t = _semver_tuple(current)
                        pending_releases = [
                            r for r in semver_releases
                            if _semver_tuple(r["tag"]) > cur_t
                        ]
            except Exception:
                pass

            # Intento 2: /tags si /releases no dio nada (lo más comun para
            # repos que solo tagean via `git tag` sin crear Releases)
            if not latest_tag:
                resp_tags = await client.get(
                    f"https://api.github.com/repos/{repo}/tags?per_page=30",
                    headers=headers,
                )
                resp_tags.raise_for_status()
                tags_list = resp_tags.json() or []
                semver_tags = []
                for t in tags_list:
                    name = (t.get("name") or "").strip()
                    if _re_version.match(r"^v?\d+\.\d+\.\d+$", name):
                        semver_tags.append(name)
                semver_tags.sort(key=_semver_tuple, reverse=True)
                if semver_tags:
                    latest_tag = semver_tags[0]
                    release_url_str = f"https://github.com/{repo}/releases/tag/{latest_tag}"

            data = {
                "tag_name": latest_tag,
                "html_url": release_url_str,
                "body": release_notes_str,
                "published_at": published_at_str,
                "pending_releases": pending_releases,
            }
    except Exception as e:
        fetch_error = e

    if fetch_error is not None:
        # Si falla la API, devolvemos cached (aunque expirado) o nada
        if cached_data:
            return {
                "current": current,
                "latest": cached_data.get("latest", ""),
                "update_available": False,
                "release_url": cached_data.get("release_url", ""),
                "release_notes": cached_data.get("release_notes", ""),
                "published_at": cached_data.get("published_at", ""),
                "checked_at": cached_data.get("checked_at", ""),
                "cached": True,
                "stale": True,
                "error": str(fetch_error),
                "ignored_version": ignored_version,
            }
        return {
            "current": current,
            "latest": None,
            "update_available": False,
            "release_url": "",
            "release_notes": "",
            "published_at": "",
            "checked_at": "",
            "cached": False,
            "error": str(fetch_error),
            "ignored_version": ignored_version,
        }

    latest = data.get("tag_name", "") or ""
    release_url = data.get("html_url", "") or ""
    release_notes = data.get("body", "") or ""
    published_at = data.get("published_at", "") or ""
    pending_releases_resp = data.get("pending_releases", []) or []
    checked_at = datetime.now(timezone.utc).isoformat()
    update_available = bool(latest) and _semver_gt(latest, current)

    # Persiste cache solo si no estamos simulando (evita contaminar el cache
    # real con valores mock). Cache TODOS los releases recientes (no solo
    # pending) para que clientes con distintos current puedan filtrar luego.
    if not simulated:
        try:
            # Para el cache, almacenamos TODOS los releases recientes — no
            # solo los pending. Asi el filtro por current se hace en lectura
            # (un mismo cache sirve a NAS con v2.1.5 y v2.1.7).
            cache_pending_full: list[dict] = []
            try:
                # Reusa la lista que obtuvimos arriba si esta poblada (la
                # filtramos por > current al rellenar pending_releases_resp,
                # pero queremos guardar TODOS los semver_releases). Como el
                # cache se leera con filtro, esta bien guardar lo que tengamos.
                cache_pending_full = pending_releases_resp
            except Exception:
                cache_pending_full = []

            _UPDATE_CHECK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _UPDATE_CHECK_CACHE_PATH.write_text(json.dumps({
                "fetched_at": _time.time(),
                "latest": latest,
                "release_url": release_url,
                "release_notes": release_notes,
                "pending_releases": cache_pending_full,
                "published_at": published_at,
                "checked_at": checked_at,
            }), encoding="utf-8")
        except Exception:
            pass

    return {
        "current": current,
        "latest": latest,
        "update_available": update_available and latest != ignored_version,
        "release_url": release_url,
        "release_notes": release_notes,
        "pending_releases": pending_releases_resp,
        "published_at": published_at,
        "checked_at": checked_at,
        "cached": False,
        "simulated": simulated,
        "ignored_version": ignored_version,
    }


@app.post("/api/version/ignore-update", summary="Marca una versión como ignorada (no avisar más sobre ella)")
async def app_version_ignore_update(body: dict):
    """Body: {version: 'v2.1.4'}. Persiste en app_settings.json. Para 'dejar de
    ignorar', enviar {version: ''} o llamar /unignore."""
    from services.settings_store import set_settings_value
    version = (body or {}).get("version", "") or ""
    set_settings_value("update_ignored_version", version)
    return {"ignored_version": version}


# ── Estado general de la app ──────────────────────────────────────────────────

@app.get("/api/status", summary="Estado de la aplicación")
async def app_status():
    """
    Devuelve el estado general de la aplicación.

    Respuesta::

        {
          "mount_available": true,
          "dev_mode": false
        }
    """
    return {
        "mount_available": is_mount_available(),
        "dev_mode": DEV_MODE,
    }


# ── Registro del queue_manager ────────────────────────────────────────────────
# Se hace al final del módulo para que _run_pipeline y _broadcast_queue
# estén ya definidas antes de registrarlas.
# En DEV_MODE _run_fake_pipeline ya fue registrada arriba; no sobreescribir.
if not DEV_MODE:
    queue_manager.set_run_fn(_run_pipeline)
queue_manager.on_update(_broadcast_queue)
