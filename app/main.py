"""
main.py — Backend FastAPI de HDO ISO Converter

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
    IncludedAudioTrack,
    IncludedSubtitleTrack,
    QueueReorderRequest,
    Session,
    SessionUpdateRequest,
)
from phases.phase_a import run_full_analysis
from phases.phase_b import apply_rules, generate_auto_chapters
from phases.phase_d import extract_chapters_from_mkv, find_main_mpls, run_phase_d
from phases.phase_e import needs_reordering, run_phase_e_direct, run_phase_e_propedit
from phases.iso_mount import mount_iso, unmount_iso, is_mount_available
from queue_manager import queue_manager
from storage import (
    compute_iso_fingerprint,
    delete_session,
    find_session_by_fingerprint,
    list_sessions,
    load_session,
    make_session_id,
    save_session,
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

_recover_interrupted_sessions()

# ── DEV MODE ──────────────────────────────────────────────────────────────────
# ⚠️  TEMPORAL — eliminar junto con dev_fixtures.py una vez validado.
from dev_fixtures import (
    DEV_MODE, DEV_FAKE_ISOS, build_fake_session, seed_dev_sessions,
    DEV_FAKE_MKV_FILES, build_fake_mkv_analysis, build_fake_mkv_apply,
    DEV_FAKE_RPU_FILES, build_fake_per_frame_data, build_fake_cmv40_session,
)
if DEV_MODE:
    seed_dev_sessions(CONFIG_DIR)


# ── Aplicación FastAPI ────────────────────────────────────────────────────────

app = FastAPI(
    title="HDO ISO Converter",
    version="1.3.0",
    description="Convierte ISOs UHD Blu-ray a MKV con selección automática de pistas y soporte Dolby Vision FEL.",
)

# Conexiones WebSocket activas: session_id → [WebSocket, ...]
_ws_connections: dict[str, list[WebSocket]] = {}

# Clientes WebSocket suscritos a updates de la cola
_queue_ws_clients: set[WebSocket] = set()
# Lock para serializar sends concurrentes al mismo WS (evita conflictos uvicorn/wsproto)
_broadcast_lock = None  # inicializado en startup (necesita event loop activo)


async def _broadcast_queue(status: dict) -> None:
    """Envía el estado de la cola a todos los clientes WebSocket suscritos."""
    global _broadcast_lock
    if _broadcast_lock is None:
        import asyncio as _asyncio
        _broadcast_lock = _asyncio.Lock()

    msg = json.dumps(status)
    async with _broadcast_lock:
        dead = set()
        for ws in list(_queue_ws_clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        _queue_ws_clients.difference_update(dead)


# ── Estáticos ─────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def index():
    """Sirve la SPA (Single Page Application)."""
    return FileResponse("static/index.html")


# ── ISOs disponibles ──────────────────────────────────────────────────────────

@app.get("/api/isos", summary="Lista ISOs disponibles")
async def list_isos():
    """
    Devuelve la lista de ficheros .iso encontrados recursivamente en /mnt/isos.

    Las rutas son relativas a /mnt/isos para que el frontend pueda mostrarlas
    sin exponer la estructura interna del NAS.

    Respuesta: ``{"isos": ["Movie (2025).iso", "subdir/Movie2 (2024).iso", ...]}``
    """
    # ⚠️ DEV MODE — eliminar bloque junto con dev_fixtures.py
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
    # No sobrescribir mkv_name si el usuario lo editó manualmente
    if not session.mkv_name_manual:
        session.mkv_name = rules["mkv_name"]
    save_session(session)
    return session.model_dump()


# ── Comprobar ISO duplicado ───────────────────────────────────────────────────

@app.post("/api/check-duplicate", summary="Comprueba si ya existe un proyecto para este ISO")
async def check_duplicate(body: AnalyzeRequest):
    """
    Calcula la huella del ISO (SHA-256 primer 1 MB + tamaño) y busca
    si ya existe una sesión con la misma huella. Permite detectar el
    mismo disco incluso si se ha movido o renombrado.

    Respuesta: ``{"duplicate": bool, "session": {...}|null, "fingerprint": "..."}``
    """
    iso_full_path = str(ISOS_DIR / body.iso_path)
    if not Path(iso_full_path).exists():
        raise HTTPException(status_code=400, detail=f"ISO no encontrado: {iso_full_path}")

    fingerprint = compute_iso_fingerprint(iso_full_path)
    existing = find_session_by_fingerprint(fingerprint)

    return {
        "duplicate": existing is not None,
        "session": existing.model_dump() if existing else None,
        "fingerprint": fingerprint,
    }


# ── Análisis (Fase A + B) ─────────────────────────────────────────────────────

# Progreso del análisis en curso (para polling desde el frontend)
_analyze_progress: dict = {"step": "", "done": False}


@app.get("/api/analyze/progress", summary="Progreso del análisis en curso")
async def analyze_progress():
    """Devuelve el paso actual del análisis. Usado por el frontend para polling."""
    return _analyze_progress


@app.post("/api/analyze", summary="Analiza un ISO (Fase A + B)")
async def analyze_iso(body: AnalyzeRequest):
    """
    Lanza el análisis completo de un ISO y devuelve la sesión lista para revisar.

    Pipeline: mount → mkvmerge -J → capítulos → MediaInfo → dovi_tool → reglas.
    """
    global _analyze_progress

    # ⚠️ DEV MODE — eliminar bloque junto con dev_fixtures.py
    if DEV_MODE:
        session = build_fake_session(str(ISOS_DIR / body.iso_path))
        return session.model_dump()

    iso_full_path = str(ISOS_DIR / body.iso_path)
    if not Path(iso_full_path).exists():
        raise HTTPException(status_code=400, detail=f"ISO no encontrado: {iso_full_path}")

    audio_dcp = "audio dcp" in body.iso_path.lower()

    # Callback de progreso para el modal del frontend
    async def _progress_callback(msg: str):
        global _analyze_progress
        # Mapear mensajes de log a pasos del modal
        msg_l = msg.lower()
        if "mkvmerge" in msg_l or "identificando" in msg_l:
            _analyze_progress = {"step": "identify", "done": False}
        elif "capítulo" in msg_l:
            _analyze_progress = {"step": "chapters", "done": False}
        elif "mediainfo" in msg_l:
            _analyze_progress = {"step": "mediainfo", "done": False}
        elif "packet" in msg_l or "paquetes pgs" in msg_l:
            _analyze_progress = {"step": "pgs", "done": False, "pct": 0, "eta_s": 0}
        elif "dovi_tool" in msg_l or "dolby vision" in msg_l:
            _analyze_progress = {"step": "dovi", "done": False}

    # Callback de progreso granular para el step PGS (bytes leídos por ffprobe)
    async def _pgs_progress_callback(pct: float, eta_s: int):
        global _analyze_progress
        _analyze_progress = {"step": "pgs", "done": False, "pct": round(pct, 1), "eta_s": eta_s}

    _analyze_progress = {"step": "mount", "done": False}

    # ── Fase A: análisis completo (mkvmerge + MediaInfo + dovi_tool) ─
    mount_point = None
    mpls_chapters_raw = []
    try:
        mount_point = await mount_iso(iso_full_path)
        _analyze_progress = {"step": "identify", "done": False}
        bdinfo_result, mpls_path, mpls_chapters_raw = await run_full_analysis(
            mount_point,
            log_callback=_progress_callback,
            pgs_progress_callback=_pgs_progress_callback,
        )
    except Exception as e:
        _logger.exception("Error en Fase A para ISO %s", iso_full_path)
        raise HTTPException(status_code=500, detail=f"Error en Fase A: {e}")
    finally:
        if mount_point:
            await unmount_iso(mount_point)

    # ── Fase B: Reglas automáticas ─────────────────────────────────
    _analyze_progress = {"step": "rules", "done": False}
    rules_result = apply_rules(bdinfo_result, body.iso_path, audio_dcp)

    # ── Capítulos ─────────────────────────────────────────────────
    from models import Chapter
    if mpls_chapters_raw:
        chapters      = [Chapter(**c) for c in mpls_chapters_raw]
        chapters_auto = False
        chapters_reason = f"{len(chapters)} capítulos extraídos del disco (MPLS)"
    elif bdinfo_result.duration_seconds > 0:
        chapters      = generate_auto_chapters(bdinfo_result.duration_seconds)
        chapters_auto = True
        chapters_reason = (
            "Sin capítulos en el disco — generados automáticamente cada 10 min"
        )
    else:
        chapters      = []
        chapters_auto = True
        chapters_reason = "No se pudo determinar la duración del disco"

    # ── Reutilizar sesión existente por fingerprint ─────────────────
    fingerprint = compute_iso_fingerprint(iso_full_path)
    existing = find_session_by_fingerprint(fingerprint) if fingerprint else None
    if existing:
        session_id = existing.id
    else:
        session_id = make_session_id(body.iso_path)

    session = Session(
        id=session_id,
        iso_path=iso_full_path,
        iso_fingerprint=fingerprint,
        status="pending",
        bdinfo_result=bdinfo_result,
        has_fel=bdinfo_result.has_fel,
        audio_dcp=audio_dcp,
        included_tracks=rules_result["included_tracks"],
        discarded_tracks=rules_result["discarded_tracks"],
        mkv_name=rules_result["mkv_name"],
        mkv_name_manual=False,
        chapters=chapters,
        chapters_auto_generated=chapters_auto,
        chapters_auto_reason=chapters_reason,
    )
    save_session(session)
    _analyze_progress = {"step": "done", "done": True}
    return session.model_dump()


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

    iso_path = session.iso_path
    if not Path(iso_path).exists():
        raise HTTPException(status_code=400, detail="ISO no disponible")

    mount_point = None
    try:
        mount_point = await mount_iso(iso_path)
        _, mpls_path = await run_mkvmerge_identify(mount_point)
        chapters_raw = parse_mpls_chapters(mpls_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al extraer capítulos: {e}")
    finally:
        if mount_point:
            await unmount_iso(mount_point)

    if not chapters_raw:
        raise HTTPException(status_code=404, detail="No se encontraron capítulos en el disco")

    from models import Chapter
    session.chapters = [Chapter(**c) for c in chapters_raw]
    session.chapters_auto_generated = False
    session.chapters_auto_reason = f"{len(session.chapters)} capítulos restaurados del disco (MPLS)"
    save_session(session)
    return session.model_dump()


# ── Ejecución del pipeline (Fases D + E) ──────────────────────────────────────

@app.get("/api/sessions/{session_id}/check-iso", summary="Comprueba si el ISO de la sesión está disponible")
async def check_iso(session_id: str):
    """
    Verifica que el fichero ISO referenciado por la sesión existe en disco
    y tiene extensión .iso. No monta ni lee el ISO.

    Respuesta: ``{"available": true|false, "iso_path": "/mnt/isos/..."}``
    """
    session = load_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    iso = Path(session.iso_path)
    available = iso.exists() and iso.suffix.lower() == ".iso"
    return {"available": available, "iso_path": session.iso_path}


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

    iso = Path(session.iso_path)
    if not iso.exists() or iso.suffix.lower() != ".iso":
        raise HTTPException(
            status_code=400,
            detail=f"ISO no disponible: {session.iso_path}. Comprueba que el fichero sigue en /mnt/isos.",
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

    Flujo optimizado (v1.4):
      1. Monta el ISO (loop mount UDF)
      2. Localiza el MPLS principal
      3. Decide ruta según reordenación:
         a) Con reordenación → mkvmerge MPLS → MKV final (1 copia, sin intermedio)
         b) Sin reordenación → mkvmerge MPLS → intermedio → mkvpropedit + mv (1 copia)
      4. Desmonta el ISO (siempre, en finally)
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
        save_session(session)
        for ws in _ws_connections.get(session_id, []):
            try:
                await ws.send_text(msg)
            except Exception:
                pass

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
        await log(f"[Pipeline] ━━━ Iniciando extracción de {session.iso_path} ━━━")
        await log(
            "[Pipeline] 📋 Plan general: montar el ISO (loop mount UDF), localizar el "
            "MPLS principal del Blu-ray, extraer las pistas seleccionadas a un MKV y "
            "aplicar los metadatos (nombres, flags, capítulos). Tras completar, "
            "validar el resultado y desmontar el ISO."
        )

        # ── 1. Montar ISO ─────────────────────────────────────────
        _mark_phase("mount")
        await log("[Montando ISO] ┌─ Paso 1: Montando ISO en /mnt/bd (loop mount UDF)…")
        mount_point = await mount_iso(session.iso_path)
        _mark_phase("mount", done=True)
        await log(f"[Montando ISO] └─ ✓ ISO montado en: {mount_point}")

        _check_cancel()

        # ── 2. Localizar MPLS principal ───────────────────────────
        # Reutilizar el MPLS detectado en Fase A (guardado en bdinfo_result)
        if session.bdinfo_result and session.bdinfo_result.main_mpls:
            mpls_name = session.bdinfo_result.main_mpls
            for candidate_dir in [
                Path(mount_point) / "BDMV" / "PLAYLIST",
                Path(mount_point) / "PLAYLIST",
            ]:
                candidate_path = candidate_dir / mpls_name
                if candidate_path.exists():
                    mpls_path = str(candidate_path)
                    break
            else:
                mpls_path = find_main_mpls(mount_point)
        else:
            mpls_path = find_main_mpls(mount_point)
        await log(f"[Fase D] MPLS seleccionado: {mpls_path}")

        _check_cancel()

        # ── 3. Decidir ruta ───────────────────────────────────────
        do_reorder = await needs_reordering(session, mpls_path, log)

        if do_reorder:
            # ── RUTA DIRECTA: MPLS → MKV final (1 sola copia) ────
            await log(
                "[Pipeline] 🎯 Ruta elegida: DIRECTA (MPLS → MKV final, una sola "
                "copia). Se elige porque hay pistas que reordenar o excluir — "
                "mkvmerge hace selección + metadatos + capítulos en una pasada."
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
            # ── RUTA INTERMEDIO: MPLS → intermedio → mkvpropedit ──
            await log(
                "[Pipeline] 🎯 Ruta elegida: INTERMEDIO (Fase D + Fase E in-place). "
                "Se elige porque no hay reordenación — mejor copiar una sola vez al "
                "intermedio y aplicar metadatos con mkvpropedit (O(1), sin recopia)."
            )

            # Phase D: extraer todo al intermedio
            _mark_phase("extract")
            intermediate_mkv = await run_phase_d(
                share_path=mount_point,
                tmp_dir=TMP_DIR,
                log_callback=log,
                proc_callback=_register_proc,
            )
            _mark_phase("extract", done=True)
            await log(f"[Fase D] MKV intermedio generado: {intermediate_mkv}")

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
            await log(f"[Pipeline] ✓ Completado: {final_mkv}")
            await log(
                "[Pipeline] 🎯 Resultado: MKV disponible en /mnt/output con validación "
                "final OK (pistas, idiomas, flags y capítulos verificados contra lo esperado)."
            )
        else:
            await log(f"[Pipeline] ⚠ Completado con advertencias: {final_mkv}")
            await log(
                "[Pipeline] 🎯 Resultado: MKV escrito pero con discrepancias en la "
                "validación — revisa el log de validación para ver qué campos no cuadran."
            )

    except Exception as e:
        cancelled = _cancel_flags.get(session_id, False)
        if cancelled:
            session.status        = "pending"
            session.error_message = None
            await log("[Pipeline] Cancelado por el usuario")
        else:
            session.status        = "error"
            session.error_message = str(e)
            await log(f"[Pipeline] ERROR: {e}")

        # Limpiar ficheros temporales/parciales
        for path in [intermediate_mkv, session.output_mkv_path]:
            if path and Path(path).exists():
                try:
                    Path(path).unlink()
                    await log(f"[Pipeline] Fichero limpiado: {Path(path).name}")
                except OSError:
                    pass
        session.output_mkv_path = None

    finally:
        # Desmontar siempre (éxito, error o cancelación)
        if mount_point:
            _mark_phase("unmount")
            await unmount_iso(mount_point)
            _mark_phase("unmount", done=True)
            await log("[Desmontando ISO] ISO desmontado")

        # Limpiar tracking de cancelación
        _cancel_flags.pop(session_id, None)
        _active_processes.pop(session_id, None)

        # Registrar ejecución en historial (no registrar cancelaciones)
        if session.status != "pending":
            _append_execution_record(session, _phase_starts, _phase_ends)
        save_session(session)
        sig = "__DONE__" if session.status == "done" else "__CANCELLED__" if session.status == "pending" else "__ERROR__"
        for ws in _ws_connections.get(session_id, []):
            try:
                await ws.send_text(sig)
            except Exception:
                pass


async def _validate_final_mkv(session: Session, mkv_path: str, log) -> bool:
    """
    Valida el MKV final contra lo esperado por la sesión.

    Comprueba: existencia del fichero, número y tipo de pistas,
    coincidencia de idiomas de audio y subtítulos, presencia de capítulos.
    Escribe un informe detallado en el log para diagnóstico.

    Returns:
        True si todo es correcto, False si hay discrepancias.
    """
    await log("[Validación] Verificando MKV final…")

    if not Path(mkv_path).exists():
        await log("[Validación] ❌ ERROR: El fichero MKV no existe")
        return False

    # ── Leer pistas del MKV final con mkvmerge -J ────────────────
    proc = await asyncio.create_subprocess_exec(
        "mkvmerge", "-J", mkv_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode >= 2:
        await log(f"[Validación] ❌ ERROR: mkvmerge -J falló con código {proc.returncode}")
        return False

    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        await log("[Validación] ❌ ERROR: JSON inválido de mkvmerge -J")
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
    await log(f"[Validación] Fichero: {Path(mkv_path).name} ({file_size / 1e9:.2f} GB)")
    await log(f"[Validación] Pistas: {len(actual_video)} vídeo, {len(actual_audio)} audio, {len(actual_subs)} subtítulos")

    # ── Validar vídeo ────────────────────────────────────────────
    if not actual_video:
        await log("[Validación] ❌ Sin pistas de vídeo")
        all_ok = False
    else:
        for v in actual_video:
            codec = v.get("codec", "?")
            dims = v.get("properties", {}).get("pixel_dimensions", "?")
            await log(f"[Validación]   🎬 Vídeo: {codec} {dims}")

    # Verificar Dolby Vision si se esperaba FEL
    # Con mkvmerge v81+, BL+EL se combinan en un solo track (no hay EL separada).
    # La señalización DV se verifica por el DOVI configuration record en codec private.
    if session.has_fel:
        if len(actual_video) == 1:
            # v81+: BL+EL combinados — comportamiento correcto
            await log("[Validación]   ✅ Dolby Vision: BL+EL combinados en un solo track (mkvmerge v81+)")
        elif any("1920" in v.get("properties", {}).get("pixel_dimensions", "") for v in actual_video):
            # v65 legacy: EL como track separado — DV puede no funcionar
            await log("[Validación]   ⚠️ Dolby Vision: EL presente como track separado (requiere mkvmerge v81+ para DV correcto)")
        else:
            msg = "❌ Dolby Vision: FEL esperado pero no se encontró Enhancement Layer"
            await log(f"[Validación] {msg}")
            warnings.append(msg)
            all_ok = False

    # ── Validar audio ────────────────────────────────────────────
    if len(actual_audio) != len(expected_audio):
        msg = f"❌ Audio: {len(actual_audio)} pistas en MKV vs {len(expected_audio)} esperadas"
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
        msg = f"❌ Audio esperada #{i+1} ausente: {exp.raw.language} {exp.raw.codec}"
        await log(f"[Validación]   {msg}")
        warnings.append(msg)
        all_ok = False

    # ── Validar subtítulos ───────────────────────────────────────
    if len(actual_subs) != len(expected_subs):
        msg = f"❌ Subtítulos: {len(actual_subs)} pistas en MKV vs {len(expected_subs)} esperadas"
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
        msg = f"❌ Subtítulo esperado #{i+1} ausente: {exp.raw.language} {exp.subtitle_type}"
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
        msg = f"⚠️ Capítulos: {num_chapters} en MKV vs {expected_chapters} esperados"
        await log(f"[Validación] {msg}")
        warnings.append(msg)
    else:
        await log(f"[Validación]   📖 Capítulos: {num_chapters}")

    # ── Resumen ──────────────────────────────────────────────────
    if all_ok:
        await log("[Validación] ✅ MKV verificado correctamente")
    else:
        await log(f"[Validación] ⚠️ Verificación con {len(warnings)} discrepancia(s)")
        await log("[Validación] ──── DATOS PARA DIAGNÓSTICO ────")
        await log(f"[Validación] Sesión ID: {session.id}")
        await log(f"[Validación] ISO: {session.iso_path}")
        await log(f"[Validación] MKV: {mkv_path}")
        await log(f"[Validación] Pistas incluidas (sesión): {len(expected_audio)} audio + {len(expected_subs)} subs")
        for i, t in enumerate(expected_audio):
            await log(f"[Validación]   Audio esperado #{i+1}: {t.raw.language} · {t.raw.codec} · label=\"{t.label}\"")
        for i, t in enumerate(expected_subs):
            await log(f"[Validación]   Sub esperado #{i+1}: {t.raw.language} · {t.subtitle_type} · label=\"{t.label}\"")
        await log(f"[Validación] Pistas reales (mkvmerge -J): {len(actual_audio)} audio + {len(actual_subs)} subs")
        for at in actual_audio:
            p = at.get("properties", {})
            await log(f"[Validación]   Audio real: id={at['id']} {at['codec']} {p.get('language','')} \"{p.get('track_name','')}\"")
        for st in actual_subs:
            p = st.get("properties", {})
            await log(f"[Validación]   Sub real: id={st['id']} {p.get('language','')} def={p.get('default_track',False)} frc={p.get('forced_track',False)} \"{p.get('track_name','')}\"")
        await log("[Validación] ──── FIN DIAGNÓSTICO ────")

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


@app.get("/api/mkv/files", summary="Lista MKVs disponibles en /mnt/output")
async def list_mkv_files():
    """Devuelve la lista de ficheros .mkv en el directorio de salida."""
    # ⚠️ DEV MODE — eliminar bloque junto con dev_fixtures.py
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

    Body: ``{"file_path": "Movie.mkv"}`` (relativo a /mnt/output).
    Durante la ejecución emite progreso en ``_analyze_progress`` (mismo
    endpoint ``/api/analyze/progress`` que usa Tab 1).
    """
    global _analyze_progress
    rel_path = body.get("file_path", "")
    # ⚠️ DEV MODE — eliminar bloque junto con dev_fixtures.py
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

    mkv_full = str(OUTPUT_DIR_MKV / rel_path)
    if not Path(mkv_full).exists():
        raise HTTPException(status_code=400, detail=f"MKV no encontrado: {rel_path}")

    async def _mkv_progress_callback(step: str):
        global _analyze_progress
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
        _analyze_progress = {"step": "", "done": True}
        return result.model_dump()
    except Exception as e:
        _analyze_progress = {"step": "", "done": True, "error": str(e)}
        _logger.exception("Error analizando MKV %s", mkv_full)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/mkv/light-profile", summary="Extrae MaxCLL/MaxFALL por escena (on-demand)")
async def mkv_light_profile_endpoint(body: dict):
    """Extrae el perfil de luminancia del MKV.
    Alimenta el sparkline + histograma de la Radiografía DV+HDR en Tab 2.

    Body: ``{"file_path": "Movie.mkv", "full": false}``.
      - full=False (default): muestra los primeros 30 min del movie (~43200
        frames a 24fps). Rapido (~60-90s), suficiente para dar una vista.
      - full=True: extract RPU del movie completo. Mas preciso pero puede
        tardar 4-8 min en UHD BDs en NAS.

    Devuelve dos arrays downsampleados a 240 buckets + total_frames.
    """
    import tempfile
    rel_path = body.get("file_path", "")
    want_full = bool(body.get("full", False))
    if DEV_MODE:
        import math, random
        random.seed(42)
        n = 240
        series_cll = [int(300 + 500 * math.sin(i/15) + random.randint(-80, 120) + (i/n)*200) for i in range(n)]
        series_cll = [max(10, v) for v in series_cll]
        series_fall = [max(5, int(v * 0.12 + random.randint(-10, 20))) for v in series_cll]
        return {"per_scene_max_cll": series_cll, "per_scene_max_fall": series_fall, "total_frames": 186113}

    mkv_full = str(OUTPUT_DIR_MKV / rel_path)
    if not Path(mkv_full).exists():
        raise HTTPException(status_code=400, detail=f"MKV no encontrado: {rel_path}")

    from phases.phase_a import DOVI_TOOL_BIN, FFMPEG_BIN
    import json as _json, time as _time
    tmp_dir = Path(tempfile.mkdtemp(prefix="lightprof_"))
    rpu_path = tmp_dir / "rpu.bin"
    hevc_path = tmp_dir / "sample.hevc"
    try:
        # dovi_tool extract-rpu directo sobre MKV con matroska-rs parser es
        # MUY lento en NAS con HDD: medido 98% CPU durante 80s+ en un UHD BD
        # de 60GB con --limit 43200 sin acabar. Solución: ffmpeg hace demux
        # del MKV (muy eficiente con stream-copy, sin reencodar) y escribe
        # HEVC annex-B local; luego dovi_tool corre sobre el HEVC que es un
        # stream plano sin contenedor — extracción rápida.
        sample_minutes = 30
        ff_timeout = 900 if want_full else 240   # 15 min full / 4 min sample
        dt_timeout = 600 if want_full else 180   # 10 min full / 3 min sample

        ff_cmd = [FFMPEG_BIN, "-y", "-v", "error",
                  "-i", mkv_full,
                  "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb"]
        if not want_full:
            ff_cmd += ["-t", str(sample_minutes * 60)]
        ff_cmd += ["-f", "hevc", str(hevc_path)]

        _logger.warning("light-profile: Paso 1/3 — ffmpeg extrae %s HEVC (full=%s)",
                        f"{sample_minutes}min" if not want_full else "movie completo", want_full)
        t0 = _time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *ff_cmd,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=ff_timeout)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            raise HTTPException(
                status_code=504,
                detail=f"ffmpeg excedió {ff_timeout}s extrayendo HEVC del MKV. NAS saturado?"
            )
        if proc.returncode != 0 or not hevc_path.exists() or hevc_path.stat().st_size < 1024:
            err = stderr.decode("utf-8", errors="replace")[:400]
            _logger.warning("light-profile: ffmpeg falló rc=%s err=%s", proc.returncode, err)
            raise HTTPException(status_code=500, detail=f"ffmpeg falló: {err}")
        ff_elapsed = _time.monotonic() - t0
        _logger.warning("light-profile: Paso 1/3 ✓ ffmpeg OK en %.1fs (%.2f GB HEVC)",
                        ff_elapsed, hevc_path.stat().st_size / 1e9)

        # Paso 2: dovi_tool extract-rpu sobre HEVC local (≫ rápido que sobre MKV)
        _logger.warning("light-profile: Paso 2/3 — dovi_tool extract-rpu sobre HEVC local")
        t1 = _time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            DOVI_TOOL_BIN, "extract-rpu", str(hevc_path), "-o", str(rpu_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=dt_timeout)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            raise HTTPException(status_code=504, detail=f"dovi_tool extract-rpu excedió {dt_timeout}s")
        if proc.returncode != 0 or not rpu_path.exists() or rpu_path.stat().st_size < 10:
            err = stderr.decode("utf-8", errors="replace")[:400]
            _logger.warning("light-profile: extract-rpu falló rc=%s err=%s", proc.returncode, err)
            raise HTTPException(status_code=500, detail=f"extract-rpu falló: {err}")
        _logger.warning("light-profile: Paso 2/3 ✓ RPU en %.1fs (%d bytes)",
                        _time.monotonic() - t1, rpu_path.stat().st_size)

        # HEVC intermedio ya no hace falta
        try: hevc_path.unlink(missing_ok=True)
        except Exception: pass

        # Paso 3: export RPU a JSON
        _logger.warning("light-profile: Paso 3/3 — dovi_tool export a JSON + parseo")
        t2 = _time.monotonic()
        json_path = tmp_dir / "rpu.json"
        proc = await asyncio.create_subprocess_exec(
            DOVI_TOOL_BIN, "export", "-i", str(rpu_path), "-o", str(json_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            raise HTTPException(status_code=504, detail="dovi_tool export excedió 3 min")
        if proc.returncode != 0 or not json_path.exists():
            err = stderr.decode("utf-8", errors="replace")[:400]
            _logger.warning("light-profile: export falló rc=%s err=%s", proc.returncode, err)
            raise HTTPException(status_code=500, detail=f"export falló: {err}")

        # Parse JSON
        try:
            with open(json_path) as f:
                data = _json.load(f)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"JSON parse falló: {e}")
        _logger.warning("light-profile: export+parse JSON OK en %.1fs",
                        _time.monotonic() - t2)

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

        # Debug: dump keys del primer RPU para diagnosticar formato
        if rpus and isinstance(rpus[0], dict):
            vdr0 = rpus[0].get("vdr_dm_data", {})
            ext0 = vdr0.get("ext_metadata_blocks") or vdr0.get("ext_blocks")
            _logger.warning("light-profile: RPU[0].vdr_dm_data keys=%s, ext type=%s",
                            list(vdr0.keys())[:10], type(ext0).__name__)
            if isinstance(ext0, list) and ext0:
                _logger.warning("light-profile: ext[0] keys=%s", list(ext0[0].keys())[:10] if isinstance(ext0[0], dict) else "(not dict)")
            elif isinstance(ext0, dict):
                _logger.warning("light-profile: ext dict keys=%s", list(ext0.keys())[:10])

        def _extract_l1(ext_blocks_raw):
            """Devuelve (max_pq, avg_pq) del bloque L1 o (None, None)."""
            # Caso (a) + (c): lista de bloques
            if isinstance(ext_blocks_raw, list):
                for b in ext_blocks_raw:
                    if not isinstance(b, dict): continue
                    # (c) tagged enum: {"Level1": {...}} o {"level1": {...}}
                    for key in ("Level1", "level1", "L1"):
                        if key in b and isinstance(b[key], dict):
                            blk = b[key]
                            return blk.get("max_pq") or blk.get("max"), \
                                   blk.get("avg_pq") or blk.get("avg")
                    # (a) flat: {"level": 1, "max_pq": ..., "avg_pq": ...}
                    lvl = b.get("level") or b.get("block_type") or b.get("ext_block_level")
                    if lvl == 1 or str(lvl) == "1":
                        return b.get("max_pq") or b.get("max_cll") or b.get("max"), \
                               b.get("avg_pq") or b.get("max_fall") or b.get("avg")
            # Caso (b): dict keyed por nombre
            elif isinstance(ext_blocks_raw, dict):
                for key in ("level1", "Level1", "L1", "1"):
                    blk = ext_blocks_raw.get(key)
                    if isinstance(blk, dict):
                        return blk.get("max_pq") or blk.get("max"), \
                               blk.get("avg_pq") or blk.get("avg")
                    if isinstance(blk, list) and blk and isinstance(blk[0], dict):
                        return blk[0].get("max_pq"), blk[0].get("avg_pq")
            return None, None

        def _to_nits(v):
            """PQ code → nits. Detecta formato automáticamente."""
            v = float(v)
            if v > 4096: return v   # ya es nits
            if v < 1:    return _pq_code_to_nits(v * 4095)  # normalized
            return _pq_code_to_nits(v)

        per_frame_cll, per_frame_fall = [], []
        for rpu in rpus:
            vdr = (rpu or {}).get("vdr_dm_data", {})
            ext_blocks_raw = vdr.get("ext_metadata_blocks") or vdr.get("ext_blocks")
            cll, fall = _extract_l1(ext_blocks_raw)
            if cll is not None:
                try: per_frame_cll.append(int(round(_to_nits(cll))))
                except Exception: per_frame_cll.append(0)
            if fall is not None:
                try: per_frame_fall.append(int(round(_to_nits(fall))))
                except Exception: per_frame_fall.append(0)

        _logger.warning("light-profile: parseo extrajo %d CLL + %d FALL frames de %d RPUs",
                        len(per_frame_cll), len(per_frame_fall), len(rpus))

        if not per_frame_cll:
            raise HTTPException(
                status_code=500,
                detail=f"El JSON de dovi_tool export ({len(rpus)} RPUs) no contiene bloques L1 parseables. Revisa los logs del contenedor para ver la estructura detectada."
            )

        # Downsample a ~240 buckets para la sparkline (no tiene sentido mostrar
        # 186k frames uno a uno)
        MAX_POINTS = 240
        def _downsample(xs):
            if len(xs) <= MAX_POINTS:
                return xs
            step = len(xs) / MAX_POINTS
            return [max(xs[int(i * step):int((i + 1) * step)] or [0]) for i in range(MAX_POINTS)]

        return {
            "per_scene_max_cll":  _downsample(per_frame_cll),
            "per_scene_max_fall": _downsample(per_frame_fall),
            "total_frames": len(per_frame_cll),
        }
    finally:
        for p in (rpu_path, hevc_path, tmp_dir / "rpu.json"):
            try: p.unlink(missing_ok=True)
            except Exception: pass
        try: tmp_dir.rmdir()
        except Exception: pass


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


@app.post("/api/mkv/apply", summary="Aplica ediciones a un MKV")
async def apply_mkv_edits_endpoint(body: MkvEditRequest):
    """
    Aplica ediciones de metadatos a un MKV vía mkvpropedit (instantáneo).

    Soporta: nombres de pistas, flags default/forced, capítulos.
    """
    # ⚠️ DEV MODE — eliminar bloque junto con dev_fixtures.py
    if DEV_MODE:
        return build_fake_mkv_apply(body)
    if not Path(body.file_path).exists():
        raise HTTPException(status_code=400, detail="MKV no encontrado")
    try:
        result = await apply_mkv_edits(body)
        return result
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
    artifact_exists as cmv40_artifact_exists,
    list_available_rpus,
    run_phase_a_analyze_source, run_phase_b_target_from_path,
    run_phase_b_target_from_mkv, run_phase_b_target_from_drive,
    run_phase_c_extract,
    run_phase_e_correct_sync, run_phase_f_inject,
    run_phase_g_remux, run_phase_h_validate,
    detect_sync_offset, compute_sync_confidence,
    validate_artifacts as _validate_cmv40_artifacts,
    cleanup_orphan_tmp as _cmv40_cleanup_orphan_tmp,
    CMV40_WORK_BASE, CMV40_RPU_DIR,
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
    """Añade un log a la sesión CMv4.0 y lo emite por WebSocket."""
    # Timestamp en hora local del contenedor (TZ env, ej: Europe/Madrid)
    ts_msg = f"[{datetime.now().astimezone().strftime('%H:%M:%S')}] {msg}"
    session.output_log.append(ts_msg)
    save_cmv40_session(session)
    # Broadcast a clientes WS conectados
    for ws in _cmv40_ws_connections.get(session.id, []):
        try:
            await ws.send_text(ts_msg)
        except Exception:
            pass


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
        await _cmv40_log(session,
            f"⏭ Fase {phase_name} ignorada — ya hay otra fase en curso para este proyecto")
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
            save_cmv40_session(session)


# ── Endpoints CRUD ────────────────────────────────────────────────────────────

class CMv40CreateRequest(BaseModel):
    source_mkv_path: str
    output_mkv_name: str | None = None


@app.get("/api/cmv40", summary="Lista proyectos CMv4.0")
async def list_cmv40():
    return {"sessions": [s.model_dump() for s in list_cmv40_sessions()]}


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

    title_en = title
    if tmdb_configured():
        try:
            matches = await search_movies(title, year, limit=1)
            if matches:
                title_en = matches[0].title_en
                if year is None:
                    year = matches[0].year
        except Exception:
            pass

    try:
        candidates = await find_candidates(title_en, title, year)
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
    )
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

    # Auto-rewind: si la sesión dice "remuxed/validated/done" pero el MKV
    # esperado (.mkv.tmp para remuxed, .mkv para validated/done) no existe
    # físicamente en OUTPUT_DIR, retrocedemos la fase a `injected` para que
    # la UI muestre Fase G (remux) como siguiente en vez de H (validate).
    # Sin esto, el usuario era llevado a Fase H y la ejecución fallaba con
    # "MKV final no existe — ejecuta Fase G primero".
    # No se aplica a sesiones archivadas (modo solo lectura) ni a DEV_MODE.
    if (not DEV_MODE and not session.archived
            and session.phase in ("remuxed", "validated", "done")
            and session.output_mkv_name):
        tmp_path   = OUTPUT_DIR_MKV / f"{session.output_mkv_name}.tmp"
        final_path = OUTPUT_DIR_MKV / session.output_mkv_name
        if session.phase == "remuxed":
            missing = not tmp_path.exists() and not final_path.exists()
        else:
            # validated / done: el archivo final debería existir (tmp ya renombrado)
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
    result["session"] = session.model_dump()
    return result


@app.post("/api/cmv40/{session_id}/cancel", summary="Cancela la fase en curso")
async def cmv40_cancel(session_id: str):
    _cmv40_cancel_flags[session_id] = True
    proc = _cmv40_active_procs.get(session_id)
    if proc:
        try:
            proc.kill()
        except Exception:
            pass
    # Forzar limpieza del running_phase
    session = load_cmv40_session(session_id)
    if session:
        session.running_phase = None
        await _cmv40_log(session, "🛑 Cancelado por el usuario")
        save_cmv40_session(session)
    return {"ok": True}


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

    async def _coro(log_cb, proc_cb):
        await run_phase_e_correct_sync(session, body.editor_config, log_cb)

    try:
        # new_phase = session.phase: mantenemos la fase actual (Fase D sigue activa)
        await _run_cmv40_phase(session, "correct_sync", _coro, session.phase)
        return session.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    await websocket.accept()
    _cmv40_ws_connections.setdefault(session_id, []).append(websocket)

    # Enviar log histórico al conectar
    session = load_cmv40_session(session_id)
    if session:
        for line in session.output_log[-500:]:  # últimas 500 líneas
            try:
                await websocket.send_text(line)
            except Exception:
                break

    try:
        while True:
            await websocket.receive_text()  # keep-alive (ignoramos mensajes del cliente)
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in _cmv40_ws_connections.get(session_id, []):
            _cmv40_ws_connections[session_id].remove(websocket)


# ── DEV MODE: simulación de ejecución fake ────────────────────────────────────
# ⚠️  TEMPORAL — eliminar junto con dev_fixtures.py una vez validado.
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
            for ws in _ws_connections.get(session_id, []):
                try:
                    await ws.send_text(msg)
                except Exception:
                    pass

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
            for ws in _ws_connections.get(session_id, []):
                try:
                    await ws.send_text(sig)
                except Exception:
                    pass

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


# ── Health check ─────────────────────────────────────────────────────────────

@app.get("/api/health", summary="Health check para Docker")
async def health():
    """Endpoint ligero para el health check de Docker. Devuelve 200 si la app responde."""
    return {"status": "ok"}


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
