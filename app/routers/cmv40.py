"""
routers/cmv40.py — endpoints y orquestación del Tab 3 (CMv4.0).

Salió de `main.py`, donde ocupaba 3.374 líneas entre los endpoints de los
otros dos tabs. El corte fue limpio porque el bloque solo dependía de 26
nombres externos, casi todos stdlib o modelos: la única dependencia real
hacia `main` era `_dev_simulate_phase`, que resultó ser un helper exclusivo
de CMv4.0 y vino con el resto. La dependencia queda unidireccional —
`main` incluye este router y este router no importa `main`.

Contiene tres capas que conviene no confundir:

  * **Estado en memoria del módulo** — conexiones WebSocket, procesos
    activos, flags de cancelación y locks por sesión. Es estado de proceso:
    no sobrevive a un reinicio, y de eso se encarga el recovery de arranque.
  * **Orquestación** — `_run_cmv40_phase` (historial, errores, locks),
    `_cmv40_launch_phase`, la tabla `_CMV40_RUNNERS` y
    `_cmv40_dispatch_next_phase`, que es el auto-pipeline del backend.
  * **Endpoints** — los 37 de `/api/cmv40/*` más el WebSocket del log.

El contrato HTTP está fijado en `tests/test_cmv40_endpoints.py` con
TestClient; el de las fases, en `test_cmv40_fase_f_matriz` y
`test_cmv40_fases_cgh`.
"""
import asyncio
import json
import logging
import os
import shutil as _cmv40_shutil
import time as _time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from dev_fixtures import (
    DEV_FAKE_RPU_FILES,
    DEV_MODE,
    build_fake_per_frame_data,
)
from models import (
    CMV40_PHASES_ORDER,
    CMv40Phase,
    CMv40PhaseRecord,
    CMv40Session,
)
from storage import (
    delete_cmv40_session,
    list_cmv40_sessions,
    load_cmv40_session,
    make_cmv40_session_id,
    save_cmv40_session,
)

_logger = logging.getLogger(__name__)

router = APIRouter()

from phases.cmv40_pipeline import (
    get_workdir as cmv40_get_workdir,
    list_available_rpus,
    run_phase_a_analyze_source, run_phase_b_target_from_path,
    run_phase_b_target_from_mkv, run_phase_b_target_from_drive,
    preflight_target_path, preflight_target_mkv, preflight_target_drive,
    run_phase_c_extract,
    run_phase_e_correct_sync, run_phase_f_inject,
    run_phase_g_remux, run_phase_h_validate,
    detect_sync_offset, compute_sync_confidence, sheet_sync_hint,
    evaluate_sync_gate,
    validate_artifacts as _validate_cmv40_artifacts,
    cleanup_orphan_tmp as _cmv40_cleanup_orphan_tmp,
    CMV40_WORK_BASE,
)

import phases.cmv40_pipeline as _cmv40_pipeline_mod   # noqa: E402
import workload  # noqa: E402
from phases.cmv40_strategy import resolve_plan  # noqa: E402

# ── Qué proyectos tienen una fase en marcha, EN MEMORIA ─────────────────────
#
# Este proceso es el único que arranca fases, y al arrancar
# `recuperar_sesiones_interrumpidas` (aquí abajo) limpia los `running_phase` que
# quedaran en disco: la memoria es la fuente de verdad. El punto verde del tab lo
# consultaba cada 5 s con `list_cmv40_sessions_summary`, que hace un `glob` más
# un `stat` por sesión — con 88 proyectos son ~88 syscalls cada 5 s, 1,5
# millones al día, para decidir si se pinta un punto.
#
# Se mantiene desde `_cmv40_marcar_activa` / `_cmv40_marcar_libre`, que son los
# ÚNICOS sitios que tocan `session.running_phase`. Un test guarda esa regla:
# si alguien vuelve a asignarlo a mano, el registro se queda con un fantasma y
# el punto verde no se apaga nunca.
_cmv40_activas: dict[str, str] = {}


def _cmv40_marcar_activa(session: CMv40Session, fase: str) -> None:
    """Marca la sesión como ocupada por `fase` (en el objeto y en el registro)."""
    session.running_phase = fase
    _cmv40_activas[session.id] = fase


def _cmv40_marcar_libre(session: CMv40Session) -> None:
    """Libera la sesión: ya no hay fase en marcha."""
    session.running_phase = None
    _cmv40_activas.pop(session.id, None)


def recuperar_sesiones_interrumpidas() -> None:
    """Limpia sesiones CMv4.0 con running_phase != null tras un reinicio.

    Sin esto, un proyecto que estaba en mid-fase cuando el contenedor cae
    queda con running_phase persistido — la UI lo muestra eternamente como
    "fase ejecutándose" cuando en realidad no hay ningún proceso vivo
    corriendo. El usuario solo puede deshacerlo manualmente con cancelar
    (que falla porque no hay proc) o forzando un reset de fase.

    Estrategia (paralela a la de Tab 1 en `routers/tab1.py`):
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
        # Por el helper, no a mano: el registro en memoria arranca vacío, así
        # que el pop es un no-op, pero la regla de "solo los helpers tocan
        # running_phase" se queda sin excepciones. Mientras esta función vivía
        # en `main.py` el guard no la veía — de hecho la asignación cruda
        # llevaba aquí desde el principio y salió al traerla al router.
        _cmv40_marcar_libre(s)
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
    _cmv40_marcar_activa(session, phase_name)
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
        _cmv40_marcar_libre(session)
        _cmv40_cancel_flags.pop(session.id, None)
        save_cmv40_session(session)


# Marcadores de progreso: prefijo del token que el frontend parsea y estado
# del último emitido por sesión — {session_id: (payload, monotonic_ts)}.
_CMV40_PROGRESS_PREFIX = "§§PROGRESS§§"
_cmv40_last_progress: dict[str, tuple[str, float]] = {}
# Cada cuánto se reemite un progreso que no ha cambiado. Solo viaja por WS
# (no se persiste), así que su coste es despreciable y conviene que sea corto:
# es lo que hace que un cliente recién reconectado recupere la barra.
_CMV40_PROGRESS_HEARTBEAT_S = 5.0


def _cmv40_progress_should_emit(session_id: str, msg: str) -> bool:
    """True si este marcador de progreso aporta algo nuevo al cliente.

    Los tickers de `cmv40_pipeline` emiten `§§PROGRESS§§` cada 2 s aunque el
    valor no haya cambiado. Durante `inject-rpu` (que no escribe nada más al
    log) el pct se satura en 95 % y el ticker repetía la MISMA línea ~450
    veces en una fase de 15 min. Cada una entraba en `output_log` y disparaba
    el throttle de persistencia, que reescribe el JSON entero de la sesión
    (2,16 MB medidos en John Wick 4): ~1 MB/s de escrituras aleatorias contra
    el mismo pool ZFS por el que el pipeline streamea 70 GB, y ~0,6 cores de
    un NAS que solo tiene 4.

    Filtramos por (pct, label) con un heartbeat corto — así un cliente que se
    reconecta recupera la barra enseguida, pero la repetición deja de tocar
    `output_log` y, con ello, el disco.
    """
    payload = msg[len(_CMV40_PROGRESS_PREFIX):]
    now = _time.monotonic()
    prev = _cmv40_last_progress.get(session_id)
    if prev and prev[0] == payload and (now - prev[1]) < _CMV40_PROGRESS_HEARTBEAT_S:
        return False
    _cmv40_last_progress[session_id] = (payload, now)
    return True


# Cada cuánto se persiste `last_progress` en el JSON de la sesión. Es lo que
# permite que la barra sobreviva a un WS caído o a un F5, así que tiene que
# tocar disco de vez en cuando — pero mucho menos que las ~450 reescrituras
# que provocaba persistir el progreso dentro del log.
_CMV40_PROGRESS_PERSIST_S = 20.0
_cmv40_progress_persist_ts: dict[str, float] = {}


# Peso de cada fase dentro del job completo, en tanto por uno. Derivado del
# reparto real de las 83 sesiones del histórico (analyze 28,6 % · inject
# 28,2 % · remux 31,6 % · validate 9,1 % · extract 2 %), no inventado.
#
# Se ajusta según la ruta porque el reparto cambia mucho: en drop-in no hay
# demux y la validación son 4 s, mientras que en merge la validación pesa.
_CMV40_PHASE_WEIGHTS_MERGE = {
    "preflight": 0.01, "analyze_source": 0.29, "target_rpu_drive": 0.01,
    "target_rpu_path": 0.01, "target_rpu_mkv": 0.03, "extract": 0.08,
    "correct_sync": 0.01, "inject": 0.28, "remux": 0.29, "validate": 0.01,
}
_CMV40_PHASE_WEIGHTS_DROPIN = {
    "preflight": 0.01, "analyze_source": 0.35, "target_rpu_drive": 0.01,
    "target_rpu_path": 0.01, "target_rpu_mkv": 0.03, "extract": 0.00,
    "correct_sync": 0.01, "inject": 0.32, "remux": 0.31, "validate": 0.01,
}
# Orden real de ejecución, para saber qué queda por detrás de la fase actual.
_CMV40_PHASE_RUN_ORDER = [
    "preflight", "analyze_source", "target_rpu_drive", "target_rpu_path",
    "target_rpu_mkv", "extract", "correct_sync", "inject", "remux", "validate",
]


def _cmv40_job_pct(session: CMv40Session, phase_pct: float) -> float | None:
    """Porcentaje del JOB completo, no de la fase.

    La barra del overlay mide la fase en curso, así que llega al 100 % varias
    veces por job y no dice cuánto queda de verdad. Esto reparte el 0-100 %
    entre las fases según lo que pesa cada una, para poder decir "job al 45 %".

    Devuelve None si no hay una fase en curso reconocible.
    """
    running = session.running_phase
    if not running or running not in _CMV40_PHASE_RUN_ORDER:
        return None
    dropin = bool(session.target_type == "trusted_p7_fel_final"
                  and session.target_trust_ok
                  and (session.source_workflow or "p7_fel") == "p7_fel")
    pesos = _CMV40_PHASE_WEIGHTS_DROPIN if dropin else _CMV40_PHASE_WEIGHTS_MERGE
    total = sum(pesos.values()) or 1.0
    idx = _CMV40_PHASE_RUN_ORDER.index(running)
    # Fases ya pasadas: cuentan enteras solo si de verdad se ejecutaron.
    hechas = {r.phase for r in (session.phase_history or []) if r.status == "done"}
    acumulado = sum(
        pesos.get(ph, 0.0)
        for i, ph in enumerate(_CMV40_PHASE_RUN_ORDER)
        if i < idx and ph in hechas
    )
    acumulado += pesos.get(running, 0.0) * max(0.0, min(100.0, phase_pct)) / 100.0
    return round(100.0 * acumulado / total, 1)


def _cmv40_augment_progress(session: CMv40Session, msg: str) -> tuple[str, str]:
    """Añade `job_pct` al marcador de progreso. Devuelve (msg, ts_msg)."""
    try:
        payload = json.loads(msg[len(_CMV40_PROGRESS_PREFIX):])
        if isinstance(payload, dict):
            job = _cmv40_job_pct(session, float(payload.get("pct") or 0))
            if job is not None:
                payload["job_pct"] = job
                msg = _CMV40_PROGRESS_PREFIX + json.dumps(payload)
    except (ValueError, TypeError):
        pass
    return msg, f"[{datetime.now().astimezone().strftime('%H:%M:%S')}] {msg}"


def _cmv40_store_last_progress(session: CMv40Session, msg: str) -> None:
    """Guarda el progreso en la sesión y en su fichero aparte.

    Sin esto, un paso silencioso largo (extract-rpu son 2-3 min sin una sola
    línea) deja la UI sin ninguna señal si el WebSocket se cae: el log no
    lleva progreso y el GET tampoco lo traía.

    Va a un **sidecar** (`{id}.progress.json`), no al JSON de la sesión. Son
    ~50 bytes, y meterlos dentro obligaba a reescribir la sesión entera —0,86
    MB con los 3.914 `L2Combo` de un caso real— cada 20 s durante todo el job.
    Tras mover el log a un fichero, esto se convirtió en el coste dominante:
    0,33 de los 0,38 GB por job que quedaban.
    """
    from storage import write_cmv40_progress
    try:
        payload = json.loads(msg[len(_CMV40_PROGRESS_PREFIX):])
    except (ValueError, TypeError):
        return
    if not isinstance(payload, dict):
        return
    session.last_progress = payload
    now = _time.monotonic()
    last = _cmv40_progress_persist_ts.get(session.id, 0.0)
    if now - last < _CMV40_PROGRESS_PERSIST_S:
        return
    _cmv40_progress_persist_ts[session.id] = now
    asyncio.get_event_loop().create_task(
        asyncio.to_thread(write_cmv40_progress, session.id, payload))


# ── El log va a un fichero, no al JSON de la sesión ─────────────────────────
#
# Persistir UNA línea reescribía la sesión entera: 2,21 MB de JSON con 10.000
# líneas, ~850 saves por job con el throttle a 5 s, **~1 GB escrito en /config**
# contra el mismo pool ZFS por el que el pipeline mueve 70 GB. El coste era
# cuadrático en la longitud del log — el throttle acotaba la frecuencia, no el
# tamaño de cada escritura.
#
# Las líneas se acumulan en este buffer y se vuelcan al fichero cada segundo (o
# cada 100 líneas). No es por ahorrar syscalls —añadir a un fichero es
# barato— sino para no hacer una escritura por línea cuando `_run_streaming`
# entrega una ráfaga. La pérdida máxima ante un `kill -9` baja de 5 s a 1 s.
#
# El buffer forma parte del log a efectos de lectura: `GET /api/cmv40/{id}` lo
# concatena, así que una línea recién emitida nunca falta de la hidratación.
_cmv40_log_buffer: dict[str, list[str]] = {}
_cmv40_log_buffer_ts: dict[str, float] = {}
_CMV40_LOG_FLUSH_S = 1.0
_CMV40_LOG_FLUSH_LINES = 100


def _cmv40_log_completo(session: CMv40Session) -> list[str]:
    """El log entero: el prefijo que quedó en el JSON + el fichero + el buffer.

    El prefijo existe porque las sesiones anteriores a este cambio tienen su
    log dentro del JSON y **no se migran**: un proyecto terminado no vuelve a
    escribir, así que reescribir el /config de un usuario para ahorrarse una
    concatenación no vale la pena.
    """
    from storage import read_cmv40_log
    return ((session.output_log or [])
            + read_cmv40_log(session.id)
            + list(_cmv40_log_buffer.get(session.id) or []))


async def _cmv40_log_volcar(session_id: str, forzar: bool = False) -> None:
    """Vuelca el buffer al fichero si toca (o siempre, con `forzar`)."""
    import time as _t
    from storage import append_cmv40_log

    buf = _cmv40_log_buffer.get(session_id)
    if not buf:
        return
    if not forzar:
        transcurrido = _t.monotonic() - _cmv40_log_buffer_ts.get(session_id, 0.0)
        if transcurrido < _CMV40_LOG_FLUSH_S and len(buf) < _CMV40_LOG_FLUSH_LINES:
            return
    # Se saca el lote ANTES de escribir: si llegan líneas mientras el thread
    # escribe, van al lote siguiente y no se duplican ni se pierden.
    lote, _cmv40_log_buffer[session_id] = buf, []
    _cmv40_log_buffer_ts[session_id] = _t.monotonic()
    await asyncio.to_thread(append_cmv40_log, session_id, lote)


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

    Los marcadores `§§PROGRESS§§` son EFÍMEROS: viajan por WS (alimentan la
    barra del overlay) pero NO entran en `output_log` ni disparan
    persistencia. Ver `_cmv40_progress_should_emit`.
    """
    # Timestamp en hora local del contenedor (TZ env, ej: Europe/Madrid)
    ts_msg = f"[{datetime.now().astimezone().strftime('%H:%M:%S')}] {msg}"
    if msg.startswith(_CMV40_PROGRESS_PREFIX):
        if not _cmv40_progress_should_emit(session.id, msg):
            return
        # Enriquecer con el % del job completo antes de enviarlo: el pipeline
        # solo sabe de su fase, la sesión es quien conoce el resto.
        msg, ts_msg = _cmv40_augment_progress(session, msg)
        _cmv40_store_last_progress(session, msg)
    else:
        _cmv40_log_buffer.setdefault(session.id, []).append(ts_msg)
        await _cmv40_log_volcar(session.id)
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


async def _cmv40_log_phase_failed(
    session: CMv40Session, fase: str, msg: str
) -> None:
    """Cierra una fase con la marca ✗ sin repetir un motivo ya explicado.

    Las fases que abortan con diagnóstico propio (el pre-flight es el caso
    claro) escriben un párrafo largo al log y acto seguido lanzan ese MISMO
    texto como excepción. El handler genérico lo volvía a escribir entero, así
    que el usuario veía el mismo bloque de diez líneas dos veces seguidas y
    parecía que había fallado dos veces. Si el motivo ya está en las últimas
    líneas, aquí basta la línea de cierre.

    El prefijo `✗ Fase` es token de persistencia
    (`_CMV40_LOG_FORCE_PERSIST_MARKERS`) — no tocarlo.
    """
    recientes = _cmv40_log_completo(session)[-3:]
    if msg and any(msg in linea for linea in recientes):
        await _cmv40_log(session, f"✗ Fase {fase} FALLÓ")
    else:
        await _cmv40_log(session, f"✗ Fase {fase} FALLÓ: {msg}")


# Timeouts de envío consecutivos por conexión — {id(ws): n}. Se limpia en
# cuanto un envío va bien o cuando la conexión se descarta.
_cmv40_ws_timeouts: dict = {}
_CMV40_WS_SEND_TIMEOUT_S = 10.0
_CMV40_WS_MAX_TIMEOUTS = 3


async def _cmv40_send_with_timeout(sid: str, ws, msg: str) -> None:
    """Envía un mensaje a un WebSocket sin dejar que un cliente lento congele
    el log para todos.

    El timeout era de 2 s y CUALQUIER excepción cerraba la conexión. Con el
    event loop compitiendo por 4 cores saturados, un send perfectamente sano
    puede pasar de 2 s: el resultado eran 27 reconexiones en pocos minutos
    (2026-08-16), y como el progreso ya no se persiste en el log, cada corte
    dejaba la UI muerta hasta la siguiente reconexión.

    Ahora un timeout aislado NO cierra: hacen falta varios seguidos para
    declarar zombie. Cualquier otra excepción (socket ya cerrado, etc.) sí
    cierra de inmediato, que es el caso real de cliente desaparecido.
    """
    key = id(ws)
    try:
        await asyncio.wait_for(ws.send_text(msg), timeout=_CMV40_WS_SEND_TIMEOUT_S)
        _cmv40_ws_timeouts.pop(key, None)
        return
    except asyncio.TimeoutError:
        n = _cmv40_ws_timeouts.get(key, 0) + 1
        _cmv40_ws_timeouts[key] = n
        if n < _CMV40_WS_MAX_TIMEOUTS:
            return  # transitorio: el cliente sigue vivo, no lo desconectamos
        _logger.info(
            "WS de %s desconectado tras %d timeouts de envío seguidos", sid, n)
    except Exception:
        pass
    _cmv40_ws_timeouts.pop(key, None)
    try:
        await ws.close()
    except Exception:
        pass
    try:
        _cmv40_ws_connections.get(sid, []).remove(ws)
    except ValueError:
        pass


# Estado del throttle por sesión: { session_id: {last_save_ts: monotonic, lines_since: int} }


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
# NOTA (2026-08-21): estos marcadores NACIERON para decidir qué líneas forzaban
# un save del JSON, y esa razón ya no existe — el log va a un fichero y se
# persiste todo. Pero siguen siendo un CONTRATO, ahora con el frontend:
# `app.js` clasifica y colorea el log por ellos, detecta el inicio y el fin de
# fase con `━━━ Inicio fase:` y `✓ Fase X completada en`, y decide el
# auto-avance con eso. Sus prefijos siguen sin poder cambiarse.
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


# `_cmv40_maybe_persist_log` vivía aquí: decidía cuándo reescribir el JSON de
# la sesión para persistir una línea de log, con marcadores que forzaban el save
# y un throttle de 5 s / 50 líneas para el output ruidoso, más un lock por
# sesión y una regla de "si el lock está ocupado, descarta el trigger".
#
# Toda esa maquinaria existía porque escribir una línea costaba megabytes. Con
# el log en un fichero al que se AÑADE, el coste es O(1) y no hay nada que
# throttlear: se vuelca el buffer y punto (`_cmv40_log_volcar`).

async def _cmv40_flush_log(session: CMv40Session) -> None:
    """Vuelca el log pendiente y persiste el estado. Al terminar cada fase.

    Dos cosas distintas: las **líneas** van al fichero de log (donde ya está
    todo lo anterior, porque añadir es O(1)) y el **estado** de la sesión a su
    JSON. Cuando el log vivía dentro del JSON eran la misma escritura, y por eso
    hacía falta un throttle: cada línea costaba megabytes.
    """
    await _cmv40_log_volcar(session.id, forzar=True)
    lock = _get_cmv40_save_lock(session.id)
    async with lock:
        await _save_cmv40_session_async(session)


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


# Sesiones con una fase EN VUELO. Complementa al lock: `if lock.locked()`
# seguido de `async with lock` NO es atómico — entre el check y la adquisición
# hay un await, así que dos disparos simultáneos ven el lock libre, pasan el
# check y el segundo se ENCOLA en el lock; cuando el primero termina, el
# segundo ejecuta la fase OTRA VEZ. Eso es lo que hacía correr la Fase H dos
# veces seguidas (visto en "Robot Salvaje": validate 22:22:50→54 y de nuevo
# 22:22:57→23:02) y dejaba el modal apareciendo y desapareciendo.
#
# Este set se consulta y actualiza SIN await entre medias, así que en el
# bucle de eventos monohilo de asyncio la comprobación es atómica de verdad:
# el segundo disparo se descarta en vez de esperar su turno.
_cmv40_phases_in_flight: set[str] = set()

# Fases que NO avanzan de estado por diseño y por tanto pueden repetirse:
# Fase E aplica correcciones de sync acumulativas sin salir de Fase D
# (`new_phase` = la fase actual). Excluirlas del guard de "trabajo ya hecho".
_CMV40_REPEATABLE_PHASES = {"correct_sync"}


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
    if session.id in _cmv40_phases_in_flight or lock.locked():
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

    # A partir de aquí la sesión queda marcada como ocupada hasta el finally.
    _cmv40_phases_in_flight.add(session.id)
    try:
        # Guard de "trabajo ya hecho": un disparo que llega DESPUÉS de que la
        # fase terminara (el frontend reintenta a los 5s si su copia del estado
        # es vieja) no debe repetirla. Se compara contra el estado en DISCO
        # porque el objeto `session` en memoria puede ser una copia obsoleta —
        # justo el caso que provoca el re-disparo.
        if phase_name not in _CMV40_REPEATABLE_PHASES:
            on_disk = load_cmv40_session(session.id)
            if on_disk and on_disk.phase == new_phase and not on_disk.error_message:
                await _cmv40_log(
                    session,
                    f"⏭ Fase {phase_name} omitida — el proyecto ya está en "
                    f"'{new_phase}': el trabajo de esta fase ya está hecho. "
                    f"(Para rehacerla, usa 🔄 Rehacer, que retrocede el estado.)"
                )
                return
        await _run_cmv40_phase_locked(
            session, phase_name, coro_factory, new_phase, lock)
    finally:
        _cmv40_phases_in_flight.discard(session.id)


async def _run_cmv40_phase_locked(
    session: CMv40Session,
    phase_name: str,
    coro_factory,
    new_phase: str,
    lock: asyncio.Lock,
) -> None:
    """Cuerpo de la fase, ya con los guards de entrada superados.

    Mantiene el `async with lock` original: el lock sigue dando exclusión
    mutua con el resto de operaciones que lo toman (preflight, cancel), que
    no pasan por `_cmv40_phases_in_flight`.
    """
    async with lock:
        started = datetime.now(timezone.utc)
        previous_phase = session.phase
        record = CMv40PhaseRecord(phase=phase_name, started_at=started, status="running")
        session.phase_history.append(record)
        _cmv40_marcar_activa(session, phase_name)   # ← bloquea la UI en modo modal
        # Este save está FUERA del try de la fase, así que una excepción aquí
        # no se registra como fallo de fase: sube hasta el `except: pass` del
        # lanzador y el job muere en silencio con `running_phase` pegado en
        # disco (bug real del 2026-08-16). El estado ya está correcto en
        # memoria y cualquier save posterior lo persiste, así que un fallo de
        # escritura no debe impedir que la fase arranque.
        try:
            save_cmv40_session(session)
        except Exception as e:
            _logger.warning(
                "No se pudo persistir el arranque de la fase %s (sid=%s): %s — "
                "la fase continúa; el estado se persistirá en el siguiente save",
                phase_name, session.id, e,
            )

        async def _log_cb(msg: str):
            await _cmv40_log(session, msg)

        def _proc_cb(proc):
            _cmv40_proc_register(session.id, proc)

        # Cancelación cooperativa: el pipeline consulta este predicado antes
        # de cada subproceso. Sin él, cancelar solo mataba el proceso en curso
        # y la fase seguía con el comando siguiente.
        _cmv40_pipeline_mod.set_cancel_check(
            lambda: bool(_cmv40_cancel_flags.get(session.id)))

        try:
            # Estado del dedup de progreso limpio: la primera barra de esta
            # fase debe emitirse siempre, aunque coincida con la última de
            # la fase anterior.
            _cmv40_last_progress.pop(session.id, None)
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
        except _cmv40_pipeline_mod.CMv40Cancelled:
            # No es un fallo: el usuario canceló. Se registra como cancelada y
            # NO se puebla error_message — así el guard de 409 no bloquea el
            # reintento y el usuario puede relanzar la fase sin descartar nada.
            record.status = "cancelled"
            record.finished_at = datetime.now(timezone.utc)
            record.elapsed_seconds = (record.finished_at - started).total_seconds()
            session.phase = previous_phase
            await _cmv40_log(
                session,
                f"🛑 Cancelado: fase {phase_name} detenida a petición del usuario "
                f"tras {record.elapsed_seconds:.1f}s. Los artefactos completados "
                f"se conservan; relanza la fase cuando quieras.")
        except Exception as e:
            record.status = "error"
            record.finished_at = datetime.now(timezone.utc)
            record.error_message = str(e)
            session.phase = previous_phase
            session.error_message = str(e)
            await _cmv40_log_phase_failed(session, phase_name, str(e))
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
            _cmv40_marcar_libre(session)  # ← desbloquea la UI
            # La barra pertenece a la fase que acaba de terminar: dejarla
            # puesta haría que la siguiente arrancara mostrando el progreso
            # de la anterior hasta su primer tick.
            session.last_progress = None
            _cmv40_progress_persist_ts.pop(session.id, None)
            # Flush garantizado del log al terminar fase: cualquier línea
            # que el throttle hubiera dejado en buffer se vuelca a disco
            # ANTES del save final del estado. Sin esto, las últimas N
            # líneas del log podrían perderse si el server cae justo aquí.
            await _cmv40_flush_log(session)

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


def _cmv40_launch_phase(
    session: CMv40Session,
    phase_name: str,
    coro_factory,
    new_phase: str,
) -> None:
    """Lanza una fase en segundo plano y devuelve al instante.

    Los endpoints de fase son fire-and-forget: responden `started` y el
    progreso viaja por WebSocket. Este helper es el `create_task` con su red
    de seguridad, que estaba copiado literalmente en los nueve endpoints y
    los cinco dispatchers.

    El try/except no es decorativo: `_run_cmv40_phase` ya captura y persiste
    los errores DE LA FASE, así que lo que llega aquí es un fallo del propio
    wrapper (por ejemplo, el save de arranque). Tragárselo mudo dejaba el job
    zombie sin una sola pista en el log — pasó el 2026-08-16 con 'La trama
    fenicia'.
    """
    async def _run():
        # El hueco de trabajo pesado se ocupa con la CLAVE DE LA SESIÓN: así un
        # proyecto que avanza a su fase siguiente no se bloquea a sí mismo, y
        # otro proyecto (o otra pestaña) sí.
        workload.registrar(session.id, workload.TAB_CMV40,
                           f"{phase_name} de {session.output_mkv_name or session.id}")
        try:
            await _run_cmv40_phase(session, phase_name, coro_factory, new_phase)
        except Exception:
            _logger.exception(
                "Fallo no capturado al lanzar una fase CMv4.0 (sid=%s)", session.id)
        finally:
            workload.liberar(session.id)

    asyncio.create_task(_run())


def _cmv40_guard_sin_trabajo_pesado(session: CMv40Session) -> None:
    """409 si hay trabajo pesado en OTRO sitio.

    `excepto=session.id` es lo que permite que el auto-pipeline siga: cuando
    una fase de este proyecto termina y dispara la siguiente, el hueco todavía
    lo tiene él y no debe bloquearse a sí mismo. Lo que sí se bloquea es un
    SEGUNDO proyecto de Tab 3 —el lock de fases es por `session_id`, así que
    antes corrían N a la vez— y cualquier cosa pesada de Tab 1 o Tab 2.
    """
    workload.exigir_libre(session.id)


def _cmv40_guard_no_pending_error(session: CMv40Session) -> None:
    """409 si la sesión arrastra un error sin resolver.

    La siguiente fase la disparan DOS sitios: el orquestador del backend y
    `_cmv40MaybeAutoAdvance` en el frontend. El frontend se protege mirando
    `error_message`, pero sobre el snapshot de su último poll — si ese poll
    cayó en el hueco entre "la fase anterior acabó" y "la siguiente arrancó",
    ve la fase avanzada, sin error y sin `running_phase`, y dispara igual.

    Caso real (2026-08-17): Fase H se ejecutó dos veces con 1,2 s de
    diferencia, la segunda cuando el error de la primera ya estaba escrito en
    disco. En drop-in cuesta segundos; en la rama merge, Fase H son 5-8 min de
    extract-rpu completo repetidos.

    Se comprueba aquí porque el servidor es el único que tiene el estado real.
    Para reintentar, el frontend ya llama a POST /clear-error al descartar el
    banner del error.
    """
    if session.error_message:
        raise HTTPException(
            status_code=409,
            detail=(
                f"La sesión tiene un error sin resolver: "
                f"{session.error_message[:160]} — descártalo antes de "
                f"reintentar la fase."
            ),
        )


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
            await _cmv40_dispatch_phase(fresh, "analyze_source")
    elif phase == CMv40Phase.SOURCE_ANALYZED:
        # Fase A completada. Si hay pending_target persistido, dispara Fase B.
        # Si NO hay pending_target, pausa — Fase B requiere acción manual
        # del usuario (escoger target en el panel).
        if fresh.pending_target_kind:
            await _cmv40_dispatch_target_provision(fresh)
    elif phase == CMv40Phase.TARGET_PROVIDED:
        await _cmv40_dispatch_phase(fresh, "extract")
    elif phase == CMv40Phase.EXTRACTED:
        # ¿Se puede saltar la revisión visual de Fase D? La regla vive en
        # `cmv40_strategy` y la comparten este orquestador, el endpoint de ACK
        # y la UI (via `plan.skip_sync_review`). Antes estaba escrita aquí
        # como `trusted_auto or user_acked`, que con el ACK dado se saltaba
        # Fase D aunque el usuario hubiera pedido revisión manual — el
        # frontend no lo hacía, y las dos partes decidían distinto.
        if resolve_plan(fresh).inputs.skip_sync_review:
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
        await _cmv40_dispatch_phase(fresh, "inject")
    elif phase == CMv40Phase.INJECTED:
        await _cmv40_dispatch_phase(fresh, "remux")
    elif phase == CMv40Phase.REMUXED:
        await _cmv40_dispatch_phase(fresh, "validate")
    # phase == VALIDATED / DONE → terminal, no hace nada
    # phase == SOURCE_ANALYZED → requiere target manual, no avanzamos
    # phase == CREATED → requiere target/source — flow normal Fase A
    #   ya disparado por endpoint de creación


# Fase → (función del pipeline que la ejecuta, fase a la que avanza).
# Los cinco dispatchers eran la misma función copiada cinco veces; lo único
# que cambiaba era este par. El import del pipeline es tardío a propósito: a
# nivel de módulo cerraría un ciclo con `models`.
_CMV40_RUNNERS: dict[str, tuple[str, str]] = {
    "analyze_source": ("run_phase_a_analyze_source", CMv40Phase.SOURCE_ANALYZED),
    "extract":        ("run_phase_c_extract",        CMv40Phase.EXTRACTED),
    "inject":         ("run_phase_f_inject",         CMv40Phase.INJECTED),
    "remux":          ("run_phase_g_remux",          CMv40Phase.REMUXED),
    "validate":       ("run_phase_h_validate",       CMv40Phase.DONE),
}


async def _cmv40_dispatch_phase(session: CMv40Session, phase_name: str) -> None:
    """Arranca una fase del pipeline en segundo plano.

    El flag de cancelación se limpia antes de arrancar: es de la fase
    anterior. (Hoy solo lo consulta el simulador de DEV_MODE — la cancelación
    real llega por SIGTERM al subproceso, ver `cmv40_cancel`.)
    """
    import phases.cmv40_pipeline as pipeline

    runner_name, new_phase = _CMV40_RUNNERS[phase_name]
    runner = getattr(pipeline, runner_name)
    _cmv40_cancel_flags.pop(session.id, None)

    async def _coro(log_cb, proc_cb):
        result = await runner(session, log_cb, proc_cb)
        # Solo Fase H devuelve algo: el resumen de la validación, que se deja
        # en el log para que quede en el historial del proyecto.
        if result is not None:
            _cmv40_log_buffer.setdefault(session.id, []).append(
                f"Validación final: {result}")

    _cmv40_launch_phase(session, phase_name, _coro, new_phase)


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
            _cmv40_marcar_activa(session, "preflight")
            workload.registrar(session.id, workload.TAB_CMV40,
                               f"pre-flight de {session.output_mkv_name or session.id}")
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
                await _cmv40_log_phase_failed(session, "preflight", msg)
                session.error_message = msg
                session.target_preflight_ok = False
            finally:
                _cmv40_active_procs.pop(session.id, None)
                _cmv40_cancel_flags.pop(session.id, None)
                _cmv40_marcar_libre(session)
                workload.liberar(session.id)
                await _save_cmv40_session_async(session)
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

    _cmv40_launch_phase(session, phase_name, _coro, CMv40Phase.TARGET_PROVIDED)


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


@router.get("/api/cmv40", summary="Lista proyectos CMv4.0 (sidebar — sin output_log/phase_history)")
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


_ETA_MODEL_CACHE: dict = {"at": 0.0, "data": None}
_ETA_MODEL_TTL_S = 300.0
# Fases cuyo ratio medimos contra la Fase A (la referencia natural: es la
# primera larga y su coste escala con el tamaño del vídeo igual que el resto).
_ETA_MODEL_PHASES = ("extract", "inject", "remux", "validate")
_ETA_MODEL_WINDOW = 10      # jobs recientes por ruta


def _cmv40_build_eta_model() -> dict:
    """Ratios reales de duración de cada fase respecto a la Fase A.

    Sustituye a las constantes del frontend (`CMV40_ETA.r_inject`, `r_mux`…),
    que estaban calibradas a mano contra runs concretos y envejecen con cada
    cambio del pipeline: el pipe de Fase A, el adelanto de la validación y el
    export por niveles las dejaron desfasadas en cuestión de horas, y el ETA
    de un job de 26 min llegó a anunciar 49.

    Se segmenta por ruta porque el reparto no se parece en nada: en drop-in
    no hay demux y la validación son segundos.

    Solo se emite un ratio con al menos 3 muestras; por debajo, el frontend
    se queda con su constante. Se usan los 25 jobs más recientes para que el
    modelo siga a los cambios del pipeline en vez de arrastrar el pasado.
    """
    import statistics as _st
    from storage import CONFIG_DIR as _CFG
    carpeta = _CFG / "cmv40"
    if not carpeta.exists():
        return {"dropin": {}, "merge": {}, "n": 0}
    # Ventana corta a propósito: la referencia es la Fase A, y su duración
    # cambia cuando cambia el pipeline. Con 25 jobs, los anteriores al pipe
    # de Fase A dominaban la mediana y el modelo subestimaba un 15 %;
    # validado contra M3GAN 2.0 (28,6 min reales), con 10 el error baja al
    # 2 %.
    #
    # La ventana se cuenta POR RUTA, no global: una racha de drop-in deja
    # sin muestras a merge (que es el 82 % de los jobs) y viceversa.
    ficheros = sorted(carpeta.glob("*.json"), key=lambda p: p.stat().st_mtime,
                      reverse=True)
    ratios: dict[str, dict[str, list[float]]] = {"dropin": {}, "merge": {}}
    vistos = {"dropin": 0, "merge": 0}
    usados = 0
    for fp in ficheros:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        hechas = {}
        for rec in (data.get("phase_history") or []):
            if rec.get("status") == "done" and (rec.get("elapsed_seconds") or 0) > 0:
                hechas[rec["phase"]] = rec["elapsed_seconds"]
        base = hechas.get("analyze_source", 0)
        if base < 30:          # sin Fase A medible no hay referencia
            continue
        # Ruta según la definición real de drop-in (la misma condición que
        # `is_drop_in_fel` del pipeline). NO vale mirar si la validación fue
        # rápida: desde que corre dentro del remux, los jobs merge también
        # validan en 2 s, y clasificaba como drop-in jobs que habían hecho
        # un demux de 217 s — contaminando la mediana.
        es_dropin = (
            data.get("source_workflow") == "p7_fel"
            and data.get("target_type") == "trusted_p7_fel_final"
            and bool(data.get("target_trust_ok"))
            and data.get("trust_override") != "force_interactive"
        )
        ruta = "dropin" if es_dropin else "merge"
        if vistos[ruta] >= _ETA_MODEL_WINDOW:
            continue
        vistos[ruta] += 1
        usados += 1
        for ph in _ETA_MODEL_PHASES:
            if ph in hechas:
                ratios[ruta].setdefault(ph, []).append(hechas[ph] / base)
    salida = {"dropin": {}, "merge": {}, "n": usados}
    for ruta, porfase in ratios.items():
        for ph, vals in porfase.items():
            if len(vals) >= 3:
                salida[ruta][ph] = round(_st.median(vals), 3)
                salida[ruta][ph + "_n"] = len(vals)
    # Con qué frecuencia sale cada ruta. Sirve para los primeros segundos de
    # un job, cuando el pre-flight aún no ha clasificado el bin: sin saber la
    # ruta el plan asumía la cara (merge) y anunciaba 48 min para un job que
    # iba a durar 35. Ponderando por lo que suele pasar en esta instalación
    # se acierta mucho más.
    total = vistos["dropin"] + vistos["merge"]
    salida["share_dropin"] = round(vistos["dropin"] / total, 3) if total else 0.5
    return salida


@router.get("/api/cmv40/eta-model", summary="Ratios de ETA medidos del histórico")
async def cmv40_eta_model():
    """Modelo de duración derivado de los jobs ya ejecutados en esta máquina.

    El frontend lo usa para estimar las fases que aún no han empezado. Las
    que están corriendo no lo necesitan: su progreso y su ETA salen de lo que
    el proceso lleva leído (ver `_ReadProgress`).
    """
    ahora = _time.monotonic()
    if _ETA_MODEL_CACHE["data"] and (ahora - _ETA_MODEL_CACHE["at"]) < _ETA_MODEL_TTL_S:
        return _ETA_MODEL_CACHE["data"]
    data = await asyncio.to_thread(_cmv40_build_eta_model)
    _ETA_MODEL_CACHE.update({"at": ahora, "data": data})
    return data


@router.get("/api/cmv40-active", summary="¿Hay algún job CMv4.0 en curso? (indicador de tab)")
async def cmv40_active():
    """Respuesta mínima para el punto verde del tab, SIN tocar el disco.

    El frontend lo consulta cada 5 s. Primero usaba `GET /api/cmv40`, que con
    88 proyectos devuelve 569 KB y tarda 193 ms — ~10 % de un core del NAS
    mientras el pipeline pelea por los 4 que hay. Después, el summary cacheado,
    que sigue haciendo un `glob` más un `stat` por sesión: ~88 syscalls cada
    5 s, 1,5 millones al día.

    Este proceso es el único que arranca fases y el arranque limpia los
    `running_phase` huérfanos del disco, así que la respuesta está en memoria
    (`_cmv40_activas`) y cuesta cero.
    """
    return {"active": bool(_cmv40_activas), "ids": sorted(_cmv40_activas)}


@router.get("/api/cmv40/rpu-files", summary="Lista RPUs disponibles en /mnt/cmv40_rpus")
async def list_cmv40_rpu_files():
    # ⚠️ DEV MODE
    if DEV_MODE:
        return {"files": DEV_FAKE_RPU_FILES}
    return {"files": list_available_rpus()}


@router.get("/api/cmv40/recommend",
         summary="Recomendación CMv4.0 basada en el sheet live de REC_9999")
async def cmv40_recommend_endpoint(title: str, year: int | None = None):
    """Dado un título (y opcionalmente año) extraídos del MKV origen,
    consulta el sheet de R3S3T_9999 (vía TMDb ES→EN si hay API key) y
    devuelve si la conversión a CMv4.0 es factible y, si no, por qué.
    """
    from services.cmv40_recommend import recommend
    result = await recommend(title, year)
    return result.model_dump()


@router.get("/api/cmv40/recommend-from-filename",
         summary="Recomendación CMv4.0 parseando el filename del MKV")
async def cmv40_recommend_from_filename_endpoint(filename: str):
    """Extrae título+año de un filename tipo 'Zootrópolis 2 (2025) [DV FEL].mkv'
    y delega en /recommend. Atajo para el frontend."""
    from services.cmv40_recommend import parse_mkv_filename, recommend
    title, year = parse_mkv_filename(filename)
    result = await recommend(title, year)
    return result.model_dump()


@router.post("/api/cmv40/tmdb-search",
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


@router.post("/api/cmv40/tmdb-lookup",
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


@router.post("/api/cmv40/{session_id}/tmdb-refresh",
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


@router.post("/api/cmv40/{session_id}/refresh-sheet",
          summary="Re-consulta la hoja de DoviTools y guarda el veredicto en la sesión")
async def cmv40_refresh_sheet(session_id: str):
    """Rellena `session.sheet_recommendation`. Útil en proyectos creados
    antes de que el veredicto se persistiera, o tras una actualización de
    la hoja (la caché del sheet tiene TTL de 1h).
    """
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    await _cmv40_hydrate_sheet_recommendation(session_id)
    refreshed = load_cmv40_session(session_id)
    return {
        "updated": bool(refreshed and refreshed.sheet_recommendation),
        "sheet_recommendation": (refreshed.sheet_recommendation
                                 if refreshed else None),
    }


@router.get("/api/cmv40/repo-survey",
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


@router.get("/api/cmv40/repo-rpus",
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


@router.post("/api/cmv40/create", summary="Crea un proyecto CMv4.0")
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

    # Veredicto del sheet de DoviTools: siempre en background (el fetch puede
    # tardar si la caché está fría). El frontend lo recoge en el polling.
    asyncio.create_task(_cmv40_hydrate_sheet_recommendation(sid))

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


async def _cmv40_hydrate_sheet_recommendation(session_id: str) -> None:
    """Consulta la hoja de DoviTools y guarda el veredicto en la sesión.

    Best-effort: el sheet es una integración opcional (sin Google key cae a
    XLSX/CSV público, y puede fallar). Nunca debe impedir crear el proyecto.
    Se guarda para que el panel pueda mostrar los avisos y el offset conocido
    durante todo el pipeline, no solo en el modal de creación.
    """
    from services.cmv40_recommend import parse_mkv_filename, recommend

    try:
        session = load_cmv40_session(session_id)
        if not session:
            return
        title, year = parse_mkv_filename(session.source_mkv_name)
        if not title:
            return
        rec = await recommend(title, year)
        fresh = load_cmv40_session(session_id)     # recarga: otra fase pudo escribir
        if not fresh:
            return
        fresh.sheet_recommendation = rec.model_dump()
        save_cmv40_session(fresh)
    except Exception as e:
        _logger.warning("Sheet hydrate falló para %s: %s", session_id, e)


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


@router.get("/api/cmv40/{session_id}", summary="Obtiene un proyecto CMv4.0")
async def cmv40_get(session_id: str, include_log: bool = True):
    """Detalle completo de un proyecto CMv4.0.

    `include_log=false` devuelve la sesión SIN `output_log`. Lo usan los
    pollers del frontend (el de fase cada 1,5 s y el de seguridad cada 4 s
    mientras hay un job) cuando el
    WebSocket está entregando el log en vivo: el poller solo necesita el
    estado (phase, running_phase, error), pero el payload completo medía
    1,57 MB y costaba 437 ms de serialización por tick — un 11 % de un
    core del NAS dedicado a reenviar un log que el cliente ya tiene.
    Se responde `output_log_omitted: true` + `output_log_len` para que el
    cliente conserve su copia y detecte desincronización.
    """
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
        tmp_path   = _cmv40_pipeline_mod.OUTPUT_DIR / f"{session.output_mkv_name}.tmp"
        final_path = _cmv40_pipeline_mod.OUTPUT_DIR / session.output_mkv_name
        if session.phase == "remuxed":
            missing = not tmp_path.exists() and not final_path.exists()
        else:
            # validated: el archivo final debería existir (tmp ya renombrado)
            missing = not final_path.exists()
        if missing:
            _logger.info(
                "Auto-rewind de sesión %s: phase=%s pero MKV no existe en %s → injected",
                session.id, session.phase, _cmv40_pipeline_mod.OUTPUT_DIR,
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
        tmp_path = _cmv40_pipeline_mod.OUTPUT_DIR / f"{session.output_mkv_name}.tmp"
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
    # `last_progress` vive en su sidecar; el campo del JSON solo lo tienen las
    # sesiones anteriores al cambio, así que el fichero manda si existe.
    from storage import read_cmv40_progress
    progreso = await asyncio.to_thread(read_cmv40_progress, session_id)
    if progreso is not None:
        data["last_progress"] = progreso
    # El log vive en un fichero (`{id}.log`), no en el JSON. Se compone al
    # servirlo para que el contrato con el frontend no cambie: sigue llegando
    # como `output_log`, y el watermark de la UI sigue funcionando igual.
    if include_log:
        data["output_log"] = await asyncio.to_thread(
            _cmv40_log_completo, session)
    else:
        from storage import cmv40_log_line_count
        data["output_log_len"] = (
            len(session.output_log or [])
            + await asyncio.to_thread(cmv40_log_line_count, session_id)
            + len(_cmv40_log_buffer.get(session_id) or []))
        data["output_log"] = []
        data["output_log_omitted"] = True
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
    # El plan de la matriz de workflows, para que la UI no lo re-derive: el
    # trust efectivo, el drop-in y si hay demux/merge/mux estaban calculados a
    # mano en app.js (la regla de trust, once veces y en dos variantes). Cada
    # réplica se desincroniza en silencio de la tabla que manda.
    data["plan"] = resolve_plan(session).to_dict()
    return data


@router.delete("/api/cmv40/{session_id}", summary="Borra un proyecto CMv4.0")
async def cmv40_delete(session_id: str, clean_artifacts: bool = False):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if session.running_phase:
        # Borrar el proyecto entero con una fase corriendo deja el subproceso
        # huérfano escribiendo en un workdir que ya no tiene dueño.
        _cmv40_guard_not_running(session, "borrar el proyecto")
    if clean_artifacts and session.artifacts_dir:
        wd = Path(session.artifacts_dir)
        if wd.exists():
            # A un thread: el workdir de un UHD son cientos de GB y borrarlos
            # sobre ZFS tarda segundos. En el bucle, eso congela la app entera
            # (incluido el healthcheck del contenedor).
            await asyncio.to_thread(_cmv40_shutil.rmtree, wd, ignore_errors=True)
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


def _cmv40_guard_not_running(session: CMv40Session, accion: str) -> None:
    """Lanza 409 si hay una fase en curso. Para las operaciones DESTRUCTIVAS.

    `cleanup` y `delete?clean_artifacts=true` hacían `rmtree` del workdir sin
    comprobarlo, así que se podían borrar los artefactos por debajo del
    `dovi_tool` que los estaba escribiendo — y el resultado es un fallo
    incomprensible en el log, no un mensaje. Los nueve endpoints de fase ya
    tienen su guard (`_cmv40_guard_no_pending_error`); estos dos no tenían
    ninguno. `reset-sync` sí lo comprueba, con el mismo criterio.
    """
    if session.running_phase:
        raise HTTPException(
            status_code=409,
            detail=(f"Hay una fase en curso ({session.running_phase}). "
                    f"Cancélala antes de {accion}."),
        )


@router.post("/api/cmv40/{session_id}/rename-output", summary="Edita el nombre del MKV de salida")
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


@router.post("/api/cmv40/{session_id}/cleanup", summary="Borra artefactos intermedios")
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
    _cmv40_guard_not_running(session, "borrar los artefactos")
    wd = Path(session.artifacts_dir) if session.artifacts_dir else None
    freed = 0
    if wd and wd.exists():
        # Se borra el workdir ENTERO, no una lista de nombres conocidos. El
        # directorio es exclusivo de esta sesión (/mnt/tmp/cmv40/{id}) y el
        # session.json vive en /config, así que no hay nada que preservar.
        #
        # Antes se recorría `_CMV40_PHASE_ARTIFACTS`, así que cualquier
        # artefacto nuevo del pipeline sobrevivía a la limpieza para siempre
        # y el proyecto quedaba archivado (solo lectura) sin forma de volver
        # a limpiarlo desde la UI. Caso real: los ficheros de la conversión a
        # Profile 8.1 dejaron 29 MB huérfanos en "Te van a matar".
        def _medir_y_borrar(directorio: Path) -> int:
            # Recuento + borrado en un thread: el workdir de un UHD son
            # 250-400 GB (source.hevc + BL + EL + EL_injected + output.mkv) y
            # tanto el `rglob` de stats como el unlink sobre ZFS tardan
            # segundos. En el bucle congelaban la app entera — el log del job
            # que estuviera corriendo en otro proyecto incluido.
            total = 0
            for f in directorio.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
            _cmv40_shutil.rmtree(directorio)
            return total

        try:
            freed = await asyncio.to_thread(_medir_y_borrar, wd)
        except Exception as e:
            _logger.warning("No se pudo borrar el workdir %s: %s", wd, e)
            freed = 0
    # También borrar .mkv.tmp orfeno en /mnt/output (si Fase G escribió pero
    # Fase H no completó)
    try:
        tmp_path = _cmv40_pipeline_mod.OUTPUT_DIR / f"{session.output_mkv_name}.tmp"
        if tmp_path.exists() and tmp_path.is_file():
            freed += tmp_path.stat().st_size
            tmp_path.unlink()
    except Exception as e:
        _logger.warning("No se pudo borrar .mkv.tmp: %s", e)
    session.archived = True
    save_cmv40_session(session)
    await _cmv40_log(session, f"🗃️ Artefactos borrados ({freed / 1e9:.2f} GB liberados). Proyecto archivado en modo solo lectura.")
    return {"ok": True, "freed_bytes": freed, "archived": True}


@router.post("/api/cmv40/{session_id}/clear-error", summary="Descarta el mensaje de error")
async def cmv40_clear_error(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    session.error_message = ""
    save_cmv40_session(session)
    return session.model_dump()


@router.post(
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
    _cmv40_marcar_libre(session)
    save_cmv40_session(session)
    await _cmv40_log(
        session,
        "✓ Proyecto cerrado manteniendo el MKV actual — el fichero original "
        "queda sin tocar. Un reproductor compatible con CMv4.0 (p3i T4 / Sony "
        "/ LG modernos) hará la conversión al vuelo en runtime con el mismo "
        "resultado visible que tendría inyectar el RPU."
    )
    return session.model_dump()


@router.post(
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


@router.post("/api/cmv40/{session_id}/auto-pipeline",
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


@router.post("/api/cmv40/{session_id}/acknowledge-critical-gates",
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
    # Marcar Fase D como omitida SOLO si de verdad se va a omitir: con
    # `force_interactive` el usuario pidió revisar el sync a mano, y aceptar
    # que el grading diverge no anula esa petición. Antes se marcaba siempre,
    # así que la UI mostraba la fase omitida y luego el pipeline se paraba
    # ahí.
    if resolve_plan(session).inputs.skip_sync_review:
        if "sync_verification_pause" not in session.phases_skipped:
            session.phases_skipped.append("sync_verification_pause")
    save_cmv40_session(session)
    # Si auto-pipeline backend activo, retoma la cadena (la sesión ya no
    # tiene awaiting_critical_ack — el orquestador puede avanzar).
    if session.auto_pipeline:
        asyncio.create_task(_cmv40_dispatch_next_phase(session_id))
    return session.model_dump()


# ── Limpieza masiva de artefactos CMv4.0 ─────────────────────────────────────

@router.get(
    "/api/cmv40/cleanup/preview",
    summary="Preview de artefactos CMv4.0 con tamaños y estado por proyecto",
)
async def cmv40_cleanup_preview():
    """Devuelve la lista de proyectos CMv4.0 con info necesaria para decidir
    qué limpiar: tamaño del workdir, fase actual, estado (done/error/archived/
    en progreso), si hay running_phase. NO borra nada — solo lectura.

    Todo el trabajo va a un thread, y las sesiones salen del **summary
    cacheado**, no de `list_cmv40_sessions()`. Esa versión completa carga cada
    JSON entero: con 88 proyectos son decenas de MB de `output_log` y, en un
    caso real, 3.914 objetos `L2Combo` en una sola sesión — y de todo eso aquí
    se usan nueve campos. Encima recorría los workdirs con `rglob` en el event
    loop, con lo que un `/mnt/tmp` lento congelaba la app entera.
    """
    from storage import list_cmv40_sessions_summary

    def _recolectar() -> tuple[list[dict], int]:
        sessions = list_cmv40_sessions_summary()
        items: list[dict] = []
        total_bytes = 0
        for s in sessions:
            wd = Path(s["artifacts_dir"]) if s.get("artifacts_dir") else None
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
            # `running_phase` del disco puede ser un fantasma de un reinicio;
            # la verdad está en memoria (ver `_cmv40_activas`).
            fase_activa = _cmv40_activas.get(s["id"])
            error_message = s.get("error_message")
            if fase_activa:
                state, safe = "running", False
                reason = f"Fase {fase_activa} en curso — no borrar"
            elif s.get("archived"):
                state, safe = "archived", False
                reason = "Ya archivado (sin artefactos)"
            elif s.get("phase") == "done":
                state, safe = "done", True
                reason = "Pipeline terminado, listo para limpiar"
            elif error_message:
                state, safe = "error", True
                reason = f"Última fase falló: {error_message[:80]}"
            else:
                state, safe = "in_progress", True
                reason = f"Pipeline detenido en fase {s.get('phase')}"

            items.append({
                "id": s["id"],
                # Titulo legible para la UI: el output_mkv_name ya viene
                # formateado ("Title (Year) [DV FEL CMv4.0].mkv") cuando hay
                # datos; si no, caemos al source_mkv_name; si tampoco, al id.
                "title": s.get("output_mkv_name") or s.get("source_mkv_name") or s["id"],
                "phase": s.get("phase"),
                "running_phase": fase_activa,
                "state": state,
                "size_bytes": size,
                "files_count": files_count,
                "wd_exists": wd_exists,
                "artifacts_dir": str(wd) if wd else "",
                "safe_to_delete": safe,
                "reason": reason,
                "error_message": error_message,
                "output_mkv_name": s.get("output_mkv_name"),
                "output_mkv_path": s.get("output_mkv_path"),
            })
            total_bytes += size
        return items, total_bytes

    items, total_bytes = await asyncio.to_thread(_recolectar)
    return {
        "items": items,
        "total_count": len(items),
        "total_bytes": total_bytes,
        "deletable_count": sum(1 for i in items if i["safe_to_delete"]),
        "deletable_bytes": sum(i["size_bytes"] for i in items if i["safe_to_delete"]),
    }


class CMv40CleanupBulkRequest(BaseModel):
    session_ids: list[str]


@router.post(
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
            for extra in ["RPU_synced.bin", "RPU_synced.tmp.bin", "editor_config.json"]:
                f = wd / extra
                if f.exists() and f.is_file():
                    try:
                        freed += f.stat().st_size
                        f.unlink()
                    except Exception:
                        pass
        # .mkv.tmp en /mnt/output
        try:
            tmp_path = _cmv40_pipeline_mod.OUTPUT_DIR / f"{session.output_mkv_name}.tmp"
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
    "sync_corrected":  ["RPU_synced.bin", "RPU_synced.tmp.bin", "editor_config.json"],
    "injected":        ["EL_injected.hevc", "BL_injected.hevc", "source_injected.hevc",
                        "RPU_merged.bin",
                        # Conversión a Profile 8.1 de los workflows single-layer
                        # (_ensure_profile8_rpu). El sufijo depende del RPU de
                        # entrada, así que se listan las dos variantes posibles.
                        "RPU_merged_p81.bin", "RPU_target_p81.bin",
                        "_profile8_mode.json"],
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


@router.get("/api/cmv40/{session_id}/reset-preview/{target_phase}", summary="Previsualiza qué se borrará al rehacer")
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


@router.post("/api/cmv40/{session_id}/reset-to/{target_phase}", summary="Resetea a una fase anterior (para rehacer)")
async def cmv40_reset_to(session_id: str, target_phase: str):
    """
    Rebobina el estado de la sesión a una fase anterior y borra los
    artefactos de fases posteriores para garantizar consistencia al re-ejecutar.
    """
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    if session.running_phase:
        raise HTTPException(
            status_code=409,
            detail=f"Hay una fase en curso ({session.running_phase}). "
                   "Cancélala antes de resetear (audit #13).",
        )

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
        # El target se re-provee → invalida toda la evaluación de trust,
        # pre-flight y la recomendación Mantener/Inyectar derivadas del bin
        # anterior. Sin esto el workflow quedaba mal etiquetado tras el reset
        # (audit #12). trust_override se conserva (es preferencia del usuario).
        session.target_type = "generic"
        session.target_trust_ok = False
        session.target_preflight_ok = False
        session.preflight_decision = ""
        session.preflight_message = ""
        session.target_l8_classification = ""
        session.phases_skipped = []
        session.awaiting_critical_ack = False
        session.critical_gate_failures = []
        session.user_acknowledged_degradation = False
        session.pipeline_aborted = False
        session.recommended_action = ""
        session.recommended_action_label = ""
        session.recommended_action_reason = ""
        session.output_workflow = ""
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


@router.post("/api/cmv40/{session_id}/verify-artifacts", summary="Valida que los artefactos de la fase actual existan")
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


@router.post("/api/cmv40/{session_id}/cancel", summary="Cancela la fase en curso")
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
        _cmv40_marcar_libre(session)
        for line in log_lines:
            await _cmv40_log(session, line)
        await _cmv40_log(session, "🛑 Cancelado por el usuario")
        save_cmv40_session(session)
    _cmv40_active_procs.pop(session_id, None)
    return {"ok": True, "log": log_lines}


# ── Endpoints de fases ───────────────────────────────────────────────────────

@router.post("/api/cmv40/{session_id}/analyze-source", summary="Fase A: analiza MKV origen")
async def cmv40_analyze_source(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    _cmv40_guard_no_pending_error(session)
    _cmv40_guard_sin_trabajo_pesado(session)

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

    await _cmv40_dispatch_phase(session, "analyze_source")
    return {"ok": True, "started": True}


class CMv40TargetPathRequest(BaseModel):
    rpu_path: str


@router.post("/api/cmv40/{session_id}/target-rpu-path", summary="Fase B1: RPU target desde path")
async def cmv40_target_path(session_id: str, body: CMv40TargetPathRequest):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    _cmv40_guard_no_pending_error(session)
    _cmv40_guard_sin_trabajo_pesado(session)

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

    # El flag de cancelación es de la fase anterior: limpiarlo antes de
    # arrancar. Ahora que el pipeline lo consulta de verdad
    # (`raise_if_cancelled`), no hacerlo abortaría esta fase al instante.
    _cmv40_cancel_flags.pop(session_id, None)

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


@router.post("/api/cmv40/{session_id}/target-rpu-from-drive",
          summary="Fase B3: RPU target descargado del repositorio REC_9999 en Drive")
async def cmv40_target_from_drive(session_id: str, body: CMv40TargetDriveRequest):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    _cmv40_guard_no_pending_error(session)
    _cmv40_guard_sin_trabajo_pesado(session)

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

    _cmv40_launch_phase(session, "target_rpu_drive", _coro, CMv40Phase.TARGET_PROVIDED)
    return {"ok": True, "started": True}


@router.post("/api/cmv40/{session_id}/target-rpu-from-mkv", summary="Fase B2: RPU target desde otro MKV")
async def cmv40_target_from_mkv(session_id: str, body: CMv40TargetMkvRequest):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    _cmv40_guard_no_pending_error(session)
    _cmv40_guard_sin_trabajo_pesado(session)

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

    _cmv40_launch_phase(session, "target_rpu_mkv", _coro, CMv40Phase.TARGET_PROVIDED)
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


@router.post(
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

    _cmv40_guard_sin_trabajo_pesado(session)

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
            _cmv40_marcar_activa(session, "preflight")
            workload.registrar(session.id, workload.TAB_CMV40,
                               f"pre-flight de {session.output_mkv_name or session.id}")
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
                await _cmv40_log_phase_failed(session, "preflight", msg)
                session.error_message = msg
                session.target_preflight_ok = False
            finally:
                _cmv40_active_procs.pop(session.id, None)
                _cmv40_cancel_flags.pop(session.id, None)
                _cmv40_marcar_libre(session)
                workload.liberar(session.id)
                await _save_cmv40_session_async(session)
        # Fuera del lock: si auto_pipeline está activo y el preflight pasó,
        # encadena Fase A automáticamente. Sin esto, si el cliente disparó
        # este endpoint manualmente (en lugar del orquestador interno), Fase
        # A no arrancaría sola — fragil ante cliente cerrado tras el POST.
        # Mismo patrón que `_cmv40_dispatch_preflight`.
        if session.auto_pipeline and not session.error_message and session.target_preflight_ok:
            asyncio.create_task(_cmv40_dispatch_next_phase(session.id))

    asyncio.create_task(_run())
    return {"ok": True, "started": True}


@router.post(
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

    _cmv40_guard_sin_trabajo_pesado(session)

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
            _cmv40_marcar_activa(session, "preflight")
            workload.registrar(session.id, workload.TAB_CMV40,
                               f"pre-flight de {session.output_mkv_name or session.id}")
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
                await _cmv40_log_phase_failed(session, "preflight (source)", msg)
                session.error_message = msg
                session.source_preflight_ok = False
            finally:
                _cmv40_active_procs.pop(session.id, None)
                _cmv40_cancel_flags.pop(session.id, None)
                _cmv40_marcar_libre(session)
                workload.liberar(session.id)
                await _save_cmv40_session_async(session)

    asyncio.create_task(_run())
    return {"ok": True, "started": True}


@router.post("/api/cmv40/{session_id}/extract", summary="Fase C: demux BL/EL + per-frame data")
async def cmv40_extract(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    _cmv40_guard_no_pending_error(session)
    _cmv40_guard_sin_trabajo_pesado(session)

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

    await _cmv40_dispatch_phase(session, "extract")
    return {"ok": True, "started": True}


# ── Caché de las series per-frame del chart de la Fase D ────────────────────
#
# `per_frame_data.json` de un UHD son 24,1 MB para 243.552 frames. Devolverlo
# entero costaba, medido: 97 ms de `json.loads` + 79 ms de Pearson + 92 ms de
# re-serializar (~300 ms de event loop en un Mac, ~1,2 s en el NAS) y 24 MB por
# el cable — para pintar un canvas de ~1500 px, o sea 160 puntos por píxel. Y
# el frontend volvía a pedirlo entero cada vez que se recargaba el proyecto.
#
# Se cachean solo las CUATRO series como `array('i')` (~8 MB para un UHD, contra
# los ~250 MB que ocuparían los dicts) más las métricas, que se calculan una vez.
# La invalidación es por stat del fichero, igual que los summary de sesiones.
_PFD_CACHE: dict[str, dict] = {}
# Cubos por defecto de la ventana devuelta. El canvas ronda los 1000-1500 px;
# 2000 deja margen para que el zoom manual no se vea escalonado.
PFD_CUBOS_DEFECTO = 2000


def _cmv40_pfd_cargar(session_id: str, pf: Path, sync_delta: int | None) -> dict:
    """Series + métricas de `per_frame_data.json`, cacheadas por stat.

    Se llama SIEMPRE en un thread: el parseo del volcado es CPU-bound.
    """
    from array import array
    st = pf.stat()
    cache = _PFD_CACHE.get(session_id)
    if cache and cache["mtime_ns"] == st.st_mtime_ns and cache["size"] == st.st_size:
        return cache

    import json as _json
    data = _json.loads(pf.read_text(encoding="utf-8"))
    filas = data.get("data") or []
    series = {k: array("i", [0]) * 0 for k in
              ("src_maxcll", "src_maxfall", "tgt_maxcll", "tgt_maxfall")}
    frames = array("i")
    for fila in filas:
        if not isinstance(fila, dict):
            continue
        frames.append(int(fila.get("frame") or 0))
        for clave, serie in series.items():
            try:
                serie.append(int(fila.get(clave) or 0))
            except (TypeError, ValueError):
                serie.append(0)

    # Las métricas se calculan sobre la serie COMPLETA, no sobre la ventana que
    # se devuelve: un desfase o una decorrelación fuera del rango visible
    # seguirían siendo reales.
    suggested = detect_sync_offset(data)
    confidence = compute_sync_confidence(data)
    cache = {
        "mtime_ns": st.st_mtime_ns, "size": st.st_size,
        "frames": frames, "series": series,
        "source_frames": data.get("source_frames") or 0,
        "target_frames": data.get("target_frames") or 0,
        "suggested_offset": suggested,
        "confidence": confidence,
        "sync_gate": evaluate_sync_gate(data, sync_delta, confidence=confidence),
    }
    _PFD_CACHE[session_id] = cache
    return cache


def _cmv40_pfd_ventana(cache: dict, desde: int, hasta: int, cubos: int) -> dict:
    """Recorta la ventana pedida y la reduce a `cubos` puntos.

    Por cubo se emiten el MÍNIMO y el MÁXIMO de cada serie, no la media: la
    gráfica sirve para detectar desalineación entre dos curvas y un promedio se
    come justo los picos que delatan un corte de escena desplazado.

    Si la ventana ya cabe en `cubos` puntos se devuelve tal cual — así los zooms
    finos (el preset de 30 s son ~720 frames) siguen viendo el dato exacto.
    """
    frames = cache["frames"]
    series = cache["series"]
    n = len(frames)
    if n == 0:
        return {"data": [], "downsampled": False, "bucket_frames": 1}

    # `frames` es creciente, así que la ventana se localiza por bisección en
    # vez de recorriendo los 243.000 puntos.
    import bisect
    i0 = bisect.bisect_left(frames, desde)
    i1 = bisect.bisect_left(frames, hasta)
    i0 = max(0, min(i0, n))
    i1 = max(i0, min(i1, n))
    ancho = i1 - i0
    if ancho == 0:
        return {"data": [], "downsampled": False, "bucket_frames": 1}

    claves = ("src_maxcll", "src_maxfall", "tgt_maxcll", "tgt_maxfall")
    if ancho <= cubos:
        datos = [{"frame": frames[i], **{k: series[k][i] for k in claves}}
                 for i in range(i0, i1)]
        return {"data": datos, "downsampled": False, "bucket_frames": 1}

    paso = ancho / cubos
    datos = []
    for b in range(cubos):
        a = i0 + int(b * paso)
        z = i0 + int((b + 1) * paso)
        if z <= a:
            z = a + 1
        if a >= i1:
            break
        z = min(z, i1)
        punto = {"frame": frames[a]}
        for k in claves:
            trozo = series[k][a:z]
            punto[k] = max(trozo)
            punto[k + "_min"] = min(trozo)
        datos.append(punto)
    return {"data": datos, "downsampled": True,
            "bucket_frames": max(1, int(round(paso)))}


@router.get("/api/cmv40/{session_id}/sync-data", summary="Devuelve per_frame_data.json + métricas")
async def cmv40_sync_data(session_id: str, desde: int | None = None,
                          hasta: int | None = None,
                          cubos: int = PFD_CUBOS_DEFECTO):
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
        if session.running_phase and resolve_plan(session).inputs.trust_effective:
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
    # El parseo del volcado va a un thread y queda cacheado por stat: el
    # frontend vuelve a pedir esto en cada cambio de zoom.
    cache = await asyncio.to_thread(
        _cmv40_pfd_cargar, session_id, pf, session.sync_delta)
    total = cache["source_frames"] or cache["target_frames"] or len(cache["frames"])
    desde = 0 if desde is None else max(0, desde)
    hasta = total if hasta is None or hasta <= desde else hasta
    cubos = max(50, min(20000, cubos or PFD_CUBOS_DEFECTO))
    ventana = _cmv40_pfd_ventana(cache, desde, hasta, cubos)

    return {
        "source_frames": cache["source_frames"],
        "target_frames": cache["target_frames"],
        "total_frames": total,
        "range": {"from": desde, "to": hasta},
        # Las métricas van sobre la serie COMPLETA, no sobre la ventana: un
        # desfase fuera del rango visible sigue siendo real.
        "suggested_offset": cache["suggested_offset"],
        "confidence": cache["confidence"],
        "sheet_sync": sheet_sync_hint(session, cache["suggested_offset"]),
        # El criterio de avance lo resuelve el backend y la UI lo LEE, igual que
        # el plan de workflows. Antes lo calculaba solo `app.js` y el endpoint
        # de confirmación no lo comprobaba.
        "sync_gate": cache["sync_gate"],
        **ventana,
    }


class CMv40SyncRequest(BaseModel):
    editor_config: dict


@router.post("/api/cmv40/{session_id}/apply-sync", summary="Fase E: corrección de sincronización")
async def cmv40_apply_sync(session_id: str, body: CMv40SyncRequest):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    _cmv40_guard_no_pending_error(session)
    _cmv40_guard_sin_trabajo_pesado(session)

    # Historial de correcciones. La Fase E aplica CADA paso sobre el resultado
    # del anterior (ver `run_phase_e_correct_sync`), así que aquí solo se anota
    # lo que el usuario pidió — sin recomponer nada.
    #
    # Antes se sumaban los frames de todos los pasos y se emitía un único
    # rango en la cabecera, porque la Fase E partía siempre del target
    # original. Con pasos del mismo signo daba lo mismo; alternando quitar y
    # duplicar, no: el orden decide QUÉ frames se tocan, aunque el Δ coincida.
    def _count_remove(cfg: dict) -> int:
        total = 0
        for r in cfg.get("remove") or []:
            r = str(r)
            if "-" in r:
                a, b = r.split("-", 1)
                try:
                    total += int(b) - int(a) + 1
                except ValueError:
                    pass
            else:
                total += 1
        return total

    def _count_duplicate(cfg: dict) -> int:
        return sum(int(d.get("length") or 0)
                   for d in (cfg.get("duplicate") or []) if isinstance(d, dict))

    prev_cfg = session.sync_config or {}
    new_cfg = body.editor_config or {}
    # Compat: las sesiones anteriores a los pasos guardan el config aplanado
    # directamente en `sync_config` (sin clave "steps").
    prev_steps = prev_cfg.get("steps")
    if not isinstance(prev_steps, list):
        prev_steps = [prev_cfg] if prev_cfg else []
    steps = [*prev_steps, new_cfg] if new_cfg else list(prev_steps)

    total_remove = sum(_count_remove(st) for st in steps)
    total_dup = sum(_count_duplicate(st) for st in steps)

    combined_cfg: dict = {
        "steps": steps,
        "total_removed": total_remove,
        "total_duplicated": total_dup,
    } if steps else {}

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

    # A la fase va EL PASO DEL USUARIO, tal cual: se aplica encadenado sobre
    # la corrección anterior. `combined_cfg` es solo el historial que se
    # persiste para la UI.
    paso = new_cfg
    # Persistimos sync_config aqui para que el frontend lo vea inmediatamente
    # (la respuesta sale antes de que termine la fase real). El backend lo
    # escribira de nuevo al finalizar — idempotente.
    session.sync_config = combined_cfg or None
    save_cmv40_session(session)

    _cmv40_cancel_flags.pop(session_id, None)

    captured_phase = session.phase  # mantenemos fase D activa

    async def _coro(log_cb, proc_cb):
        await run_phase_e_correct_sync(session, paso, log_cb)

    # Fire-and-forget como extract/inject/remux: la respuesta vuelve al
    # instante, el log fluye via WebSocket. Antes hacia await sobre la fase
    # entera (1-5 min para dovi_tool editor) y el frontend disparaba el
    # toast 'el servidor no responde en 30s' aunque el backend trabajaba ok.
    #
    # El destino es `captured_phase`, la fase ACTUAL: Fase D sigue activa y no
    # avanzamos solos — el usuario itera sobre el chart hasta que el Δ es 0 y
    # confirma el sync a mano.
    _cmv40_launch_phase(session, "correct_sync", _coro, captured_phase)
    return {"ok": True, "started": True}


@router.post("/api/cmv40/{session_id}/reset-sync", summary="Descarta la corrección y vuelve al target original")
async def cmv40_reset_sync(session_id: str):
    """Borra la corrección aplicada y re-analiza el RPU_target.bin original."""
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")

    if session.running_phase:
        raise HTTPException(
            status_code=409,
            detail=f"Hay una fase en curso ({session.running_phase}). "
                   "Cancélala antes de descartar la corrección (audit #13).",
        )

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
    (wd / "RPU_synced.tmp.bin").unlink(missing_ok=True)
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


def _cmv40_sync_gate_for(session: CMv40Session) -> dict | None:
    """Evalúa el criterio de avance de la Fase D leyendo `per_frame_data.json`.

    Devuelve None si el volcado no está: sin datos no hay criterio que aplicar
    y bloquear al usuario por un fichero ausente sería peor que dejarle pasar
    (la Fase F valida el frame count de todas formas). Se llama en un thread:
    el volcado de un UHD son decenas de MB de JSON.
    """
    if not session.artifacts_dir:
        return None
    pf = Path(session.artifacts_dir) / "per_frame_data.json"
    if not pf.exists():
        return None
    try:
        import json as _json
        data = _json.loads(pf.read_text(encoding="utf-8"))
    except Exception as e:
        _logger.warning("per_frame_data.json ilegible para el gate de sync: %s", e)
        return None
    return evaluate_sync_gate(data, session.sync_delta)


@router.post("/api/cmv40/{session_id}/mark-synced", summary="Marca sync OK sin corrección")
async def cmv40_mark_synced(session_id: str, force: bool = False):
    """Usuario confirma que no hace falta corrección (Δ=0 y curvas alineadas).
    Si el target es trusted, anotamos `sync_verification_pause` en phases_skipped
    para que la UI muestre Fase D como "omitida" incluso tras recargar.

    El criterio (Δ=0 ∧ confianza ≥ 85 %) se comprueba AQUÍ. Vivía solo en
    `app.js`, que deshabilitaba el botón, mientras este endpoint aceptaba
    cualquier cosa: un frontend cacheado viejo o una llamada a mano se lo
    saltaban sin dejar rastro. `force=true` es la salida explícita para el
    caso legítimo (grading que diverge de verdad y el usuario ya lo validó
    mirando el gráfico); antes ese caso simplemente no tenía salida.

    NO se comprueba cuando la Fase D se omite (`plan.skip_sync_review`): ahí
    nadie ha mirado el gráfico porque no había que mirarlo, y
    `per_frame_data.json` puede no existir siquiera.
    """
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    if not force and not resolve_plan(session).inputs.skip_sync_review:
        gate = await asyncio.to_thread(_cmv40_sync_gate_for, session)
        if gate is not None and not gate["ok"]:
            raise HTTPException(status_code=409, detail=gate["reason"])
    session.phase = CMv40Phase.SYNC_VERIFIED
    # Pregunta distinta de la del ACK: aquí el usuario ACABA de confirmar el
    # sync, y lo que se decide es si lo revisó de verdad o solo lo dio por
    # bueno porque el target era trusted. Con `force_interactive` lo revisó,
    # así que no se marca omitida. Es `trust_effective`, no
    # `skip_sync_review` (que además cuenta el ACK de gates degradados).
    if resolve_plan(session).inputs.trust_effective:
        if "sync_verification_pause" not in session.phases_skipped:
            session.phases_skipped.append("sync_verification_pause")
    save_cmv40_session(session)
    return session.model_dump()


@router.post("/api/cmv40/{session_id}/inject", summary="Fase F: inyecta RPU en EL")
async def cmv40_inject(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    _cmv40_guard_no_pending_error(session)
    _cmv40_guard_sin_trabajo_pesado(session)

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

    await _cmv40_dispatch_phase(session, "inject")
    return {"ok": True, "started": True}


@router.post("/api/cmv40/{session_id}/remux", summary="Fase G: mux BL+EL + remux final a MKV")
async def cmv40_remux(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    _cmv40_guard_no_pending_error(session)
    _cmv40_guard_sin_trabajo_pesado(session)

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

    await _cmv40_dispatch_phase(session, "remux")
    return {"ok": True, "started": True}


@router.post("/api/cmv40/{session_id}/validate", summary="Fase H: validación final y move a output")
async def cmv40_validate(session_id: str):
    session = load_cmv40_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    _cmv40_guard_no_pending_error(session)
    _cmv40_guard_sin_trabajo_pesado(session)

    # ⚠️ DEV MODE
    if DEV_MODE:
        session.output_mkv_path = f"/mnt/output/{session.output_mkv_name}"
        session.phase = CMv40Phase.DONE
        save_cmv40_session(session)
        await _cmv40_log(session, "[DEV] Fase H OK — MKV CMv4.0 validado")
        return session.model_dump()

    await _cmv40_dispatch_phase(session, "validate")
    return {"ok": True, "started": True}


# ── WebSocket de CMv4.0 ──────────────────────────────────────────────────────

@router.websocket("/ws/cmv40/{session_id}")
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
