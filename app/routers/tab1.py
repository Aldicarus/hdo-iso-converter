"""
routers/tab1.py — endpoints y orquestación del Tab 1 (Blu-Ray ISO → MKV).

El último de los tres en salir de `main.py`, que era el único fichero de la
aplicación y llegó a 6.000 líneas con las tres pestañas dentro. Aquí viven
2.830: los orígenes (`/api/sources`), las sesiones, el análisis (Fases A+B),
los endpoints de series TV, la cola FIFO, el orquestador del pipeline y los
dos WebSockets. `main.py` se queda con lo que es de toda la app —salud,
versión, ajustes, limpieza, los estáticos— y con el arranque.

Contiene tres capas que conviene no confundir:

  * **Estado de proceso** — flags de cancelación, procesos activos, el
    throttle de saves por sesión y las conexiones WebSocket. No sobrevive a
    un reinicio; de eso se encarga el recovery de arranque.
  * **Orquestación** — `_run_pipeline` (Fase D + Fase E, con el reintento
    sobre el M2TS cuando mkvmerge aborta con el assertion de playlist),
    `_validate_final_mkv` y el registro en `queue_manager`. No es HTTP: es
    lo que la cola ejecuta.
  * **Endpoints** — los de Tab 1 más los WS `/ws/{id}` y `/ws/queue`.

Dos cosas que van de `main` a este módulo, y no al revés:
`recuperar_sesiones_interrumpidas()` (resetea a `pending` las sesiones que
quedaron en `running`/`queued`) se llama en el arranque, y el
`include_router`. Ningún router importa `main`.

El contrato HTTP está fijado en `tests/test_endpoints_tab1_tab2.py`; las
fases, en `test_fases_de_tab1.py`; el orquestador con sus dos rutas y el
reintento, en `test_orquestador_tab1.py`; y que las URLs no cambien al
mover el código, en `test_rutas_no_cambian.py` contra un golden.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel as _BaseModel

import analysis_progress
import paths
import workload
from dev_fixtures import DEV_FAKE_ISOS, DEV_MODE, build_fake_session
from models import (
    AnalyzeRequest,
    ExecutionRecord,
    QueueReorderRequest,
    Session,
    SessionUpdateRequest,
)
from phases.phase_a import ISO639_TO_ENGLISH, run_full_analysis
from phases.phase_b import (
    CODEC_TIER_NAMES, _codec_key, apply_rules, codec_key_de_mkvmerge,
    estimate_output_size_bytes, generate_auto_chapters,
)
from phases.phase_d import (
    MkvmergePlaylistError,
    find_main_mpls,
    m2ts_covers_title,
    run_phase_d,
)
from phases.phase_e import needs_reordering, run_phase_e_direct, run_phase_e_propedit
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

_logger = logging.getLogger(__name__)

router = APIRouter()

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

def recuperar_sesiones_interrumpidas() -> None:
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

# Conexiones WebSocket activas: session_id → [WebSocket, ...]
_ws_connections: dict[str, list[WebSocket]] = {}

# Clientes WebSocket suscritos a updates de la cola
_queue_ws_clients: set[WebSocket] = set()


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

# ── ISOs disponibles ──────────────────────────────────────────────────────────

@router.get("/api/isos", summary="Lista ISOs disponibles (legacy — usar /api/sources)")
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
    if not paths.ISOS_DIR.exists():
        return {"isos": []}

    def _escanear() -> list[str]:
        # En un thread: `rglob` sobre /mnt/isos son miles de `stat` contra el
        # NAS y en el bucle paraba el log del job en curso. `/api/sources`, su
        # reemplazo, ya lo hacía así.
        return sorted(
            str(p.relative_to(paths.ISOS_DIR))
            for p in paths.ISOS_DIR.rglob("*")
            if p.is_file() and p.suffix.lower() == ".iso"
        )

    return {"isos": await asyncio.to_thread(_escanear)}


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
    """Escanea paths.ISOS_DIR y devuelve lista clasificada. Operación síncrona —
    el caller la ejecuta en thread pool si quiere evitar bloquear el event
    loop con discos lentos."""
    import time
    if not paths.ISOS_DIR.exists():
        return []

    root = paths.ISOS_DIR.resolve()
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


@router.get("/api/sources", summary="Lista fuentes disponibles (ISO, carpeta BDMV, m2ts suelto)")
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


def _session_payload(session: Session) -> dict:
    """El dump de la sesión **más los campos calculados** que la UI espera.

    `estimated_size_bytes` no se persiste porque depende de qué pistas están
    incluidas, y eso cambia en cuanto el usuario toca la selección: se calcula
    al servir. Vivía SOLO en `GET /api/sessions/{id}`, pero el panel de Tab 1
    se pinta con lo que devuelvan CINCO endpoints — el detalle, `/api/analyze`,
    `check-duplicate` (el botón "Abrir" del diálogo de duplicado),
    `reapply-rules` (los toggles de modo audio/subs) y
    `create-series-sessions`. Por las cuatro últimas el chip "💾 Ocupará ~N GB"
    salía **oculto**, que es justo como se abre un proyecto recién analizado:
    el frontend esconde el chip cuando el campo es `null` o falta, y desde
    fuera las dos cosas se ven igual.

    Un campo calculado tiene que salir de UN sitio; repartido por endpoint se
    olvida en el siguiente que se añada.
    """
    payload = session.model_dump()
    payload["estimated_size_bytes"] = estimate_output_size_bytes(session)
    return payload


@router.get("/api/sessions", summary="Lista todas las sesiones")
async def get_sessions():
    # Summary ligero (sin output_log/analysis_log/bdinfo_result) cacheado por
    # fichero + invalidación por stat, en el thread pool para no bloquear el
    # event loop leyendo/serializando N JSON. El detalle completo (logs,
    # bdinfo) está en GET /api/sessions/{id}, que es lo que usa el frontend al
    # abrir un proyecto. Mismo patrón que GET /api/cmv40.
    from storage import list_sessions_summary
    sessions = await asyncio.to_thread(list_sessions_summary)
    return {"sessions": sessions}


@router.get("/api/sessions/{session_id}", summary="Obtiene una sesión")
async def get_session(session_id: str):
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    return _session_payload(session)


@router.delete("/api/sessions/{session_id}", summary="Elimina una sesión")
async def remove_session(session_id: str):
    if not delete_session(session_id):
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    return {"ok": True}


@router.put("/api/sessions/{session_id}", summary="Actualiza una sesión (Fase C)")
async def update_session(session_id: str, body: SessionUpdateRequest):
    """
    Aplica las ediciones del usuario sobre una sesión existente (partial update).
    Solo se actualizan los campos presentes en el body.
    """
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    # has_fel / audio_dcp NO se aceptan: los fija el análisis del disco.
    if body.mkv_name          is not None: session.mkv_name          = body.mkv_name
    if body.mkv_name_manual   is not None: session.mkv_name_manual   = body.mkv_name_manual
    if body.included_tracks   is not None: session.included_tracks   = body.included_tracks
    if body.discarded_tracks  is not None: session.discarded_tracks  = body.discarded_tracks
    if body.chapters          is not None: session.chapters          = body.chapters

    save_session(session)
    return _session_payload(session)


from pydantic import BaseModel, BaseModel as _BaseModel


class ReapplyModeRequest(_BaseModel):
    audio_mode: str | None = None       # 'filtered' | 'keep_all'
    subtitle_mode: str | None = None


@router.post("/api/sessions/{session_id}/reapply-rules",
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
    return _session_payload(session)


# ── Comprobar ISO duplicado ───────────────────────────────────────────────────

@router.post("/api/check-duplicate", summary="Comprueba si ya existe un proyecto para este origen")
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
        source_abs = safe_source_path(spath, str(paths.ISOS_DIR))
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
        "sessions": [_session_payload(s) for s in sessions],
        "session": _session_payload(sessions[0]) if sessions else None,  # legacy/compat
        "fingerprint": fingerprint,
    }


# ── Análisis (Fase A + B) ─────────────────────────────────────────────────────

# El progreso del análisis vive en `analysis_progress` porque lo escriben
# DOS pestañas (el análisis de disco del Tab 1 y la apertura de MKV del
# Tab 2) y lo lee este único endpoint.


@router.get("/api/analyze/progress", summary="Progreso del análisis en curso")
async def analyze_progress():
    """Devuelve el paso actual del análisis. Usado por el frontend para polling."""
    return analysis_progress.leer()


# Progreso del disc-probe (modal "Detectando contenido"). Sin esto el modal
# mostraba una barra estática y un texto genérico ("Conectando con el
# servidor…") aunque la operación tarde 10-30s en discos grandes.
_disc_probe_progress: dict = {
    "running": False,
    "current_label": "",
    "pct": 0,         # 0-100; 0 = indeterminado
    "step": "",       # 'mount' | 'scan' | 'analyze' | 'classify' | 'done'
}


@router.get("/api/disc-probe/progress", summary="Progreso del disc-probe en curso")
async def disc_probe_progress():
    """Estado actual del disc-probe (single-job singleton). Usado por el
    modal "Detectando contenido" para mostrar el paso real con su % o
    barra indeterminada cuando no se puede medir."""
    return _disc_probe_progress


@router.post("/api/analyze", summary="Analiza un ISO (Fase A + B)")
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
        session = build_fake_session(str(paths.ISOS_DIR / (spath or body.iso_path or "")))
        return _session_payload(session)

    # Validación path-traversal estricta
    try:
        source_abs = safe_source_path(spath, str(paths.ISOS_DIR))
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
            analysis_progress.fijar(step="identify", done=False)
        elif "paso 2/4" in msg_l or "extrayendo capítulos" in msg_l:
            analysis_progress.fijar(step="chapters", done=False)
        elif "ejecutando mediainfo" in msg_l:
            analysis_progress.fijar(step="mediainfo", done=False)
        elif "contando paquetes pgs" in msg_l:
            analysis_progress.fijar(step="pgs", done=False, pct=0, eta_s=0)
        elif "paso 4/4" in msg_l or "analizando dolby vision" in msg_l:
            analysis_progress.fijar(step="dovi", done=False)

    # Callback de progreso granular para el step PGS (bytes leídos por ffprobe)
    async def _pgs_progress_callback(pct: float, eta_s: int):
        analysis_progress.fijar(step="pgs", done=False, pct=round(pct, 1), eta_s=eta_s)

    analysis_progress.fijar(step="mount", done=False)

    # ── Fase A: análisis completo (mkvmerge + MediaInfo + dovi_tool) ─
    # Source context manager: monta el ISO si es necesario, no-op para
    # bdmv_folder y m2ts. Cleanup automático en __aexit__.
    mpls_path = ""
    mpls_chapters_raw: list[dict] = []
    bdinfo_result = None
    try:
        async with await Source.open(source_abs) as src:
            analysis_progress.fijar(step="identify", done=False)
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
    analysis_progress.fijar(step="rules", done=False)
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

    # Enriquece con TMDb: rellena la ficha de la cabecera y, si el nombre del
    # ISO no traía año, completa el mkv_name con el año de TMDb. Inline con
    # timeout corto (el análisis ya tardó minutos; +unos segundos es
    # irrelevante); si TMDb tarda más sigue en background y el frontend lo
    # hidrata en el siguiente render. Mismo patrón que Tab 3.
    try:
        await asyncio.wait_for(_hydrate_session_tmdb(session.id), timeout=4.0)
        refreshed = load_session(session.id)
        if refreshed:
            session = refreshed
    except (asyncio.TimeoutError, Exception):
        asyncio.create_task(_hydrate_session_tmdb(session.id))

    analysis_progress.fijar(step="done", done=True)
    return _session_payload(session)


async def _hydrate_session_tmdb(session_id: str) -> None:
    """Rellena `session.tmdb_info` (ficha de la cabecera de Tab 1) y, si el
    nombre del ISO no traía año, completa el `mkv_name` con el año de TMDb.

    El query a TMDb usa `parse_mkv_filename` (normaliza separadores y año
    suelto, igual que el resto de lookups); el título visible del MKV lo
    sigue fijando `_extract_title_year` sobre el nombre del fichero — de TMDb
    solo tomamos el año. Best-effort: cualquier fallo se ignora (no crítico).
    """
    from services.cmv40_recommend import parse_mkv_filename
    from services.tmdb import search_movies, fetch_details, is_configured
    from phases.phase_b import _extract_title_year, _build_mkv_name

    if not is_configured():
        return
    try:
        session = load_session(session_id)
        if not session:
            return
        name = session.source_path or session.iso_path or ""
        query_title, query_year = parse_mkv_filename(name)
        if not query_title:
            return
        matches = await search_movies(query_title, query_year, limit=1)
        if not matches:
            return
        details = await fetch_details(matches[0].tmdb_id)
        if not details:
            return
        # Recarga en caliente por si otra operación escribió entretanto.
        fresh = load_session(session_id)
        if not fresh:
            return
        fresh.tmdb_info = details.model_dump()
        # Completa el año del nombre SOLO si el fichero no lo traía y el
        # usuario no lo editó a mano. El del fichero (si existe) tiene
        # prioridad: no se pisa lo que ya venía bien.
        title, file_year = _extract_title_year(name)
        if not fresh.mkv_name_manual and file_year == "0000" and details.year:
            fresh.mkv_name = _build_mkv_name(
                title, str(details.year), fresh.has_fel, fresh.audio_dcp
            )
        save_session(fresh)
    except Exception as e:
        _logger.warning("TMDb hydrate (Tab 1) falló para %s: %s", session_id, e)


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


@router.post("/api/disc-probe",
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
                safe_source_path(p, str(paths.ISOS_DIR)) for p in body.m2ts_paths
            ]
            spath_abs = validated_paths[0] if validated_paths else ""
        else:
            spath_abs = safe_source_path(spath, str(paths.ISOS_DIR))
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
            # OJO con el nombre: `paths` es el MÓDULO de los directorios, y
            # este ámbito lee `paths.ISOS_DIR` unas líneas más arriba. Una
            # local con ese nombre lo sombrea en TODA la función y el
            # `paths.ISOS_DIR` de antes revienta con UnboundLocalError.
            m2ts_paths = validated_paths or [spath_abs]
            if hint == "movie":
                # El usuario eligió película → solo permitimos 1 m2ts.
                if len(m2ts_paths) > 1:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Modo película seleccionado con {len(m2ts_paths)} ficheros M2TS. "
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
                    "current_label": f"Analizando {len(m2ts_paths)} ficheros M2TS…",
                    "pct": 0,
                    "step": "scan",
                })
                candidates = await identify_episode_candidates_from_m2ts_list(
                    m2ts_paths, progress_callback=_scan_progress,
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
                if len(m2ts_paths) == 1:
                    media_type = "movie"
                    candidates = []
                else:
                    candidates = await identify_episode_candidates_from_m2ts_list(
                        m2ts_paths, progress_callback=_scan_progress,
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


@router.get("/api/tv-search",
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


@router.get("/api/tv-details/{tmdb_id}",
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


@router.get("/api/tv-season/{tmdb_id}/{season_number}",
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


@router.get("/api/series-create-progress",
         summary="Polling del progreso de /api/create-series-sessions")
async def series_create_progress():
    """Devuelve el estado actual del único job de creación de sesiones en
    curso. Si no hay ninguno, `running=False` y los campos quedan vacíos."""
    return _series_create_progress


@router.post("/api/create-series-sessions",
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
                safe_source_path(p, str(paths.ISOS_DIR))
            source_abs = safe_source_path(body.m2ts_paths[0], str(paths.ISOS_DIR))
        else:
            source_abs = safe_source_path(spath, str(paths.ISOS_DIR))
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
                        ep_source_path = safe_source_path(ep.mpls_path, str(paths.ISOS_DIR))
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
                created_sessions.append(_session_payload(session))
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

@router.post(
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
    title, year      = _extract_title_year(session.source_path or session.iso_path)
    # Si el nombre del fichero no traía año, usar el de TMDb ya guardado en la
    # ficha (mismo criterio que el hydrate: fichero > TMDb > nada). Así, al
    # cambiar los flags FEL/DCP, el nombre conserva el año.
    if year == "0000":
        tmdb_year = (session.tmdb_info or {}).get("year")
        if tmdb_year:
            year = str(tmdb_year)
    new_name         = _build_mkv_name(title, year, session.has_fel, session.audio_dcp)
    session.mkv_name = new_name
    save_session(session)
    return {"mkv_name": new_name, "manual": False}


# ── Restaurar capítulos originales del disco ─────────────────────────────────

@router.post(
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
            # Import local: `parse_mpls_chapters` y `run_mkvmerge_identify`
            # nunca estuvieron importados en este módulo, así que esta rama
            # levantaba NameError. El `except Exception` de abajo lo
            # convertía en un 500 "Error al extraer capítulos: name ... is
            # not defined", de modo que el botón «🔄 Restaurar del disco»
            # fallaba siempre con un mensaje que no señalaba la causa.
            from phases.phase_a import parse_mpls_chapters, run_mkvmerge_identify

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


@router.get("/api/sessions/{session_id}/check-iso", summary="Comprueba si el origen de la sesión está disponible")
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


@router.post("/api/sessions/{session_id}/execute", summary="Encola la sesión para Fases D+E")
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

    # Nada de solapar trabajo pesado entre pestañas: 4 núcleos y un pool de
    # discos. Se comprueba ANTES de encolar para que el usuario lo sepa al
    # pulsar, no cuando la cola llegue a este job.
    workload.exigir_libre(session_id)

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


async def _mkvmerge_container_duration_s(path: str) -> float:
    """Duración en segundos del contenedor (MPLS o M2TS) vía ``mkvmerge -J``.

    Reutiliza ``_extract_duration`` de phase_a (lee ``playlist_duration`` o
    ``duration`` en ns). Devuelve 0.0 si no se puede determinar — ``mkvmerge -J``
    no dispara el assertion de playlist (solo el mux real lo hace), así que es
    seguro invocarlo sobre el .mpls problemático.
    """
    from phases.phase_a import _extract_duration

    proc = await asyncio.create_subprocess_exec(
        "mkvmerge", "-J", path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode not in (0, 1):
        return 0.0
    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return 0.0
    return _extract_duration((data.get("container", {}) or {}).get("properties", {}) or {})


async def _resolve_main_m2ts_for_fallback(
    mount_point: "str | None",
    session: Session,
    mpls_path: str,
    log,
) -> str:
    """Resuelve el M2TS principal del disco como alternativa a un playlist que
    mkvmerge no puede procesar (assertion ``add_filelists_for_playlists``).

    Workaround estándar de la comunidad para discos UHD multi-segmento: pasar
    el M2TS principal directamente. Antes de aceptarlo verifica que su duración
    cubre el título completo — la validación final NO comprueba duración, así
    que un M2TS corto (seamless branching real) produciría un MKV truncado en
    silencio.

    Raises:
        RuntimeError: si no hay M2TS principal o no cubre el título.
    """
    from phases.phase_a import find_main_m2ts

    await log(
        "[Pipeline] ⚠️ mkvmerge no pudo procesar el playlist de este disco "
        "(bug conocido de mkvmerge con UHD multi-segmento). Probando con el "
        "M2TS principal directamente…"
    )

    # 1. Localizar el M2TS principal — preferimos el detectado en Fase A.
    m2ts_path = None
    main_name = (
        session.bdinfo_result.main_m2ts
        if session.bdinfo_result and session.bdinfo_result.main_m2ts
        else ""
    )
    if main_name and mount_point:
        for stream_dir in (
            Path(mount_point) / "BDMV" / "STREAM",
            Path(mount_point) / "bdmv" / "stream",
        ):
            cand = stream_dir / main_name
            if cand.exists():
                m2ts_path = str(cand)
                break
    if not m2ts_path and mount_point:
        m2ts_path = find_main_m2ts(mount_point)
    if not m2ts_path:
        raise RuntimeError(
            "No se encontró el M2TS principal para sortear el fallo del playlist."
        )

    # 2. Verificar que el M2TS cubre el título completo.
    pl_dur   = await _mkvmerge_container_duration_s(mpls_path)
    m2ts_dur = await _mkvmerge_container_duration_s(m2ts_path)
    if not m2ts_covers_title(pl_dur, m2ts_dur):
        raise RuntimeError(
            f"El M2TS principal (~{m2ts_dur / 60:.0f} min) no cubre el título "
            f"completo del playlist (~{pl_dur / 60:.0f} min): el disco usa "
            "seamless branching real y no puede ripearse por esta vía. "
            "Usa MakeMKV o dgdemux para este disco concreto."
        )

    cover = f" (~{m2ts_dur / 60:.0f} min)" if m2ts_dur > 0 else ""
    await log(
        f"[Pipeline] 🔁 Reintentando la extracción con {Path(m2ts_path).name}{cover}"
    )
    return m2ts_path


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

    workload.registrar(session_id, workload.TAB_RIP,
                       f"rip de {session.mkv_name or session.id}")

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

        # ── 3. Decidir ruta y ejecutar la extracción ──────────────
        # El source normal es el MPLS (iso/bdmv). Si mkvmerge aborta al
        # procesar el playlist (bug con discos UHD multi-segmento), se reintenta
        # UNA vez con el M2TS principal directo — workaround estándar. Un M2TS
        # ya es lectura directa, así que no puede caer en ese assertion.
        extraction_source   = mpls_path
        m2ts_fallback_active = (stype == "m2ts")

        while True:
            do_reorder = await needs_reordering(session, extraction_source, log)
            try:
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
                        mpls_path=extraction_source,
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

                    # Phase D: extraer todo al intermedio. Para m2ts (o tras el
                    # fallback) pasamos source_path explícito; para iso/bdmv
                    # pasamos share_path y el helper busca el MPLS.
                    _mark_phase("extract")
                    intermediate_mkv = await run_phase_d(
                        share_path=mount_point or "",
                        tmp_dir=paths.TMP_DIR,
                        log_callback=log,
                        proc_callback=_register_proc,
                        source_path=(
                            extraction_source
                            if (stype == "m2ts" or m2ts_fallback_active)
                            else None
                        ),
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

                break  # extracción completada

            except MkvmergePlaylistError:
                if m2ts_fallback_active:
                    # El source ya era un M2TS directo: no hay más alternativa.
                    raise RuntimeError(
                        "mkvmerge no pudo procesar este título ni desde el "
                        "playlist ni desde el M2TS principal."
                    )
                # Descartar cualquier intermedio parcial del intento fallido.
                if intermediate_mkv and Path(intermediate_mkv).exists():
                    Path(intermediate_mkv).unlink(missing_ok=True)
                    intermediate_mkv = None
                extraction_source = await _resolve_main_m2ts_for_fallback(
                    mount_point, session, mpls_path, log
                )
                m2ts_fallback_active = True
                _check_cancel()
                # vuelve a iterar el bucle con el M2TS principal

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
        # Libera el hueco de trabajo pesado SIEMPRE: si esto se escapara, la
        # app quedaría bloqueada para todo lo demás hasta reiniciar.
        workload.liberar(session_id)
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

    # ISO 639-2 → nombre inglés (lowercase) para comparar con raw.language de
    # la sesión. DERIVADO del mapa canónico de phase_a (mismo patrón que
    # phase_e._ISO639) — un subset propio daba falsos ❌ en idiomas como el
    # catalán ("cat"), que en included_tracks figura como "Catalan".
    _iso = {code: name.lower() for code, name in ISO639_TO_ENGLISH.items()}

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
                warnings.append(f"Audio #{i+1}: idioma {lang_name} ≠ {exp_lang}")
                all_ok = False

            # Tier del codec: ¿el stream que quedó dentro es el que anuncia la
            # etiqueta? Hasta ahora se comparaban idioma, flags y capítulos, así
            # que una pista llamada "Castellano TrueHD Atmos 7.1" con un AC-3
            # dentro salía en verde. Es la firma de los bugs del matcher de Fase
            # E —el P0 de la auditoría y el core AC-3 subordinado—, y la única
            # forma de que se vean en PRODUCCIÓN y no solo en un test.
            tier_real = codec_key_de_mkvmerge(codec, props.get("audio_channels", 0) or 0)
            tier_esp = _codec_key(exp.raw)
            if (tier_real != "unknown" and tier_esp != "unknown"
                    and tier_real != tier_esp):
                nom_esp = CODEC_TIER_NAMES.get(tier_esp, tier_esp)
                nom_real = CODEC_TIER_NAMES.get(tier_real, tier_real)
                status = "❌"
                detail += f" (codec esperado: {nom_esp}, real: {nom_real})"
                warnings.append(
                    f"Audio #{i+1}: la etiqueta dice {nom_esp} pero el stream "
                    f"es {nom_real} ({codec})"
                )
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
                warnings.append(f"Subtítulo #{i+1}: idioma {lang_name} ≠ {exp_lang}")
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

@router.get("/api/queue", summary="Estado de la cola de ejecución")
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


@router.delete("/api/queue/{session_id}", summary="Cancela un trabajo encolado")
async def cancel_queue_job(session_id: str):
    """Elimina session_id de la cola si aún no ha empezado a ejecutarse."""
    cancelled = await queue_manager.cancel(session_id)
    if cancelled:
        session = load_session(session_id)
        if session:
            session.status = "pending"
            save_session(session)
    return {"ok": cancelled, "session_id": session_id}


@router.post(
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


@router.post("/api/queue/reorder", summary="Reordena la cola de ejecución")
async def reorder_queue(body: QueueReorderRequest):
    """Reordena la cola según la lista ordered_ids proporcionada."""
    await queue_manager.reorder(body.ordered_ids)
    return queue_manager.get_status()

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

    @router.post("/api/dev/simulate", summary="[DEV] Encola sesiones fake para simular ejecución")
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


@router.websocket("/ws/queue")
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

@router.websocket("/ws/{session_id}")
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

# ── Registro del queue_manager ────────────────────────────────────────────────
# Se hace al final del módulo para que _run_pipeline y _broadcast_queue
# estén ya definidas antes de registrarlas.
# En DEV_MODE _run_fake_pipeline ya fue registrada arriba; no sobreescribir.
if not DEV_MODE:
    queue_manager.set_run_fn(_run_pipeline)
queue_manager.on_update(_broadcast_queue)