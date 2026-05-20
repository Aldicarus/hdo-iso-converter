"""
storage.py — Persistencia de sesiones en disco.

Cada sesión se almacena como un fichero JSON independiente en el
directorio de configuración (por defecto /config):

    /config/
    ├── queue_state.json       ← estado de la cola de ejecución
    ├── El_Rey_de_Reyes_2025_1714000000.json
    └── Dune_2021_1713900000.json

El directorio se puede sobreescribir con la variable de entorno CONFIG_DIR.
Los ficheros se ordenan por fecha de modificación (más reciente primero)
en list_sessions(), que es lo que necesita el sidebar de la UI.

No hay base de datos ni migraciones — cada JSON es autosuficiente y puede
moverse, copiarse o borrarse manualmente desde el NAS.
"""
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from models import CMv40Session, Session

logger = logging.getLogger(__name__)

# Directorio de configuración — puede sobreescribirse con CONFIG_DIR
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))


def _session_path(session_id: str) -> Path:
    """Devuelve la ruta completa del fichero JSON de una sesión."""
    return CONFIG_DIR / f"{session_id}.json"


def _atomic_write_json(path: Path, content: str) -> None:
    """Escritura atómica: vuelca a un .tmp en el mismo directorio y rename
    al destino final. Garantiza que el fichero objetivo nunca queda en
    estado parcial — incluso si el server cae mid-write (kill -9, panic
    del kernel, apagón). El rename dentro del mismo filesystem es atómico
    en POSIX/Linux por contrato.

    Usado para todas las persistencias críticas (Sessions, CMv40Session,
    queue_state, mkv_apply_state). Sin esto, las escrituras de output_log
    a 500 KB+ por línea durante un job intenso eran candidato perfecto
    para corrupción si el contenedor se reiniciaba a medias.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)  # atomic on POSIX same-filesystem
    except Exception:
        # Best-effort cleanup del .tmp si rename falló
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def save_session(session: Session) -> None:
    """
    Persiste una sesión en disco como JSON con indentación.

    Actualiza updated_at al momento actual antes de escribir.
    Escritura atómica via _atomic_write_json — garantiza que un kill -9
    durante el write no deja un JSON corrupto.

    NOTA: VERSIÓN SÍNCRONA. Solo usar en endpoints REST one-shot donde
    un bloqueo de 100-500ms es aceptable. En LOOPS de async (callbacks
    de log, streaming), usar `save_session_async` para NO bloquear el
    event loop con la serialización de output_log grandes.
    """
    session.updated_at = datetime.now(timezone.utc)
    _atomic_write_json(_session_path(session.id), session.model_dump_json(indent=2))


async def save_session_async(session: Session) -> None:
    """
    Versión async no-bloqueante de `save_session`.

    Mueve la serialización JSON + write atómico al thread pool. El event
    loop NUNCA queda bloqueado por session.model_dump_json (que con
    output_log de miles de líneas tarda 100-500ms y bloquea reads del
    subprocess, broadcasts WS y otros endpoints).

    Snapshot consistente: `model_copy(deep=True)` en el async (rápido,
    ~ms) garantiza que el thread serializa una versión inmutable mientras
    otras corutinas pueden seguir mutando `session.output_log` etc.

    REGLA: usar SIEMPRE en lugar de `save_session` cuando el llamador
    está dentro de un loop async (callback de log, stream de subprocess,
    polling). Para REST endpoints one-shot, `save_session` está bien.
    """
    import asyncio as _asyncio
    session.updated_at = datetime.now(timezone.utc)
    snapshot = session.model_copy(deep=True)
    path = _session_path(session.id)

    def _serialize_and_write():
        json_str = snapshot.model_dump_json(indent=2)
        _atomic_write_json(path, json_str)

    await _asyncio.to_thread(_serialize_and_write)


async def save_cmv40_session_async(session: CMv40Session) -> None:
    """Versión async no-bloqueante de `save_cmv40_session`. Mismo
    contrato que `save_session_async` (snapshot en async, serialización
    + write en thread). Ver doc de `save_session_async` para detalles."""
    import asyncio as _asyncio
    session.updated_at = datetime.now(timezone.utc)
    snapshot = session.model_copy(deep=True)
    path = _cmv40_session_path(session.id)

    def _serialize_and_write():
        json_str = snapshot.model_dump_json(indent=2)
        _atomic_write_json(path, json_str)

    await _asyncio.to_thread(_serialize_and_write)


def load_session(session_id: str) -> Session | None:
    """
    Carga una sesión desde su fichero JSON.

    Devuelve None si el fichero no existe. No captura errores de parseo
    (un JSON corrupto debe propagarse para diagnóstico).
    """
    path = _session_path(session_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Session.model_validate(data)


def list_sessions() -> list[Session]:
    """
    Devuelve todas las sesiones ordenadas por fecha de modificación
    (más reciente primero), excluyendo settings.json.

    Los ficheros corruptos o inválidos se omiten silenciosamente para
    no romper el sidebar de la UI.
    """
    if not CONFIG_DIR.exists():
        return []
    sessions = []
    for path in sorted(
        CONFIG_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        # Ficheros conocidos que no son sesiones
        if path.name in (
            "settings.json",
            "queue_state.json",
            "app_settings.json",
            "tmdb_cache.json",
            "rec999_drive_cache.json",
            "rec999_sheet_cache.json",
        ):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sessions.append(Session.model_validate(data))
        except Exception as e:
            logger.warning("Sesión corrupta o inválida, omitida: %s — %s", path.name, e)
            continue
    return sessions


def delete_session(session_id: str) -> bool:
    """
    Elimina el fichero JSON de una sesión.

    Devuelve True si se borró, False si no existía.
    """
    path = _session_path(session_id)
    if path.exists():
        path.unlink()
        return True
    return False


def make_session_id(iso_path: str) -> str:
    """
    Genera un identificador único para una sesión a partir del nombre del ISO.

    Formato: ``{titulo}_{año}_{timestamp_unix}``
    Ej: ``El_Rey_de_Reyes_2025_1714000000``

    El título se extrae del patrón ``{Título} ({Año}) [...]`` del nombre del ISO
    (spec §5.4.1). Si el nombre no sigue el patrón, se usa el stem completo.
    El timestamp garantiza unicidad si se analiza el mismo ISO varias veces.
    """
    stem = Path(iso_path).stem  # nombre sin extensión ni ruta
    m = re.match(r"^(.+?)\s*\((\d{4})\)", stem)
    if m:
        title = re.sub(r"[^\w]", "_", m.group(1).strip())[:40]
        year  = m.group(2)
    else:
        title = re.sub(r"[^\w]", "_", stem)[:40]
        year  = "0000"
    ts = int(datetime.now(timezone.utc).timestamp())
    return f"{title}_{year}_{ts}"


FINGERPRINT_CHUNK = 1024 * 1024  # 1 MB


def compute_iso_fingerprint(iso_path: str) -> str:
    """
    Calcula la huella de un ISO: SHA-256 del primer 1 MB + tamaño del fichero.

    Es instantáneo incluso con ISOs de 50 GB+ y permite identificar
    el mismo disco independientemente de la ruta o nombre del fichero.
    """
    p = Path(iso_path)
    if not p.exists():
        return ""
    size = p.stat().st_size
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read(FINGERPRINT_CHUNK))
    h.update(str(size).encode())
    return h.hexdigest()


def find_session_by_fingerprint(fingerprint: str) -> "Session | None":
    """Busca una sesión existente con el mismo iso_fingerprint."""
    if not fingerprint:
        return None
    for session in list_sessions():
        if session.iso_fingerprint == fingerprint:
            return session
    return None


# ══════════════════════════════════════════════════════════════════════
#  TAB 3 — Storage para CMv40Session
# ══════════════════════════════════════════════════════════════════════

CMV40_DIR = CONFIG_DIR / "cmv40"


def _cmv40_session_path(session_id: str) -> Path:
    """Ruta del JSON de una sesión CMv4.0."""
    return CMV40_DIR / f"{session_id}.json"


def save_cmv40_session(session: CMv40Session) -> None:
    """Persiste una CMv40Session en /config/cmv40/{id}.json (atómico).

    Atomicidad importante porque cada línea de output_log dispara un save
    durante un job intenso (CMv4.0 con UHD genera miles de líneas). Sin
    `.tmp + rename`, un kill mid-write dejaba el JSON corrupto y la sesión
    se perdía al cargar.
    """
    session.updated_at = datetime.now(timezone.utc)
    _atomic_write_json(_cmv40_session_path(session.id), session.model_dump_json(indent=2))


def load_cmv40_session(session_id: str) -> CMv40Session | None:
    """Carga una CMv40Session por ID. Devuelve None si no existe."""
    path = _cmv40_session_path(session_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return CMv40Session.model_validate(data)


def list_cmv40_sessions() -> list[CMv40Session]:
    """Lista todas las sesiones CMv4.0 ordenadas por fecha (más reciente primero)."""
    if not CMV40_DIR.exists():
        return []
    sessions = []
    for path in sorted(
        CMV40_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sessions.append(CMv40Session.model_validate(data))
        except Exception as e:
            logger.warning("CMv40 sesión corrupta, omitida: %s — %s", path.name, e)
            continue
    return sessions


def list_cmv40_sessions_summary() -> list[dict]:
    """Lista resumida (sin output_log ni phase_history) — para el sidebar.

    El sidebar y el indicador de "running" del header solo necesitan
    metadatos: id, nombre, fase, running_phase, target_type, archived…
    NO necesitan el `output_log` (que llega a MBs por sesión bajo jobs
    largos) ni `phase_history` detallado.

    Reduce el payload de GET /api/cmv40 de ~10-30 MB con 20-30 sesiones a
    < 1 MB y elimina la causa principal de timeouts del sidebar bajo
    carga I/O del NAS (visto: durante Fase H extract-rpu con varias
    sesiones acumuladas, el sidebar tardaba >2 min en refrescar el
    spinner). El detalle completo está disponible en GET /api/cmv40/{id}.
    """
    if not CMV40_DIR.exists():
        return []
    summaries: list[dict] = []
    for path in sorted(
        CMV40_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Sustituimos por listas vacías para no romper consumidores
            # que asumen el campo presente (frontend code defensive).
            data["output_log"] = []
            data["phase_history"] = []
            summaries.append(data)
        except Exception as e:
            logger.warning("CMv40 sesión corrupta, omitida: %s — %s", path.name, e)
            continue
    return summaries


def delete_cmv40_session(session_id: str) -> bool:
    """Elimina el JSON de una sesión CMv4.0. No borra los artefactos."""
    path = _cmv40_session_path(session_id)
    if path.exists():
        path.unlink()
        return True
    return False


def make_cmv40_session_id(source_mkv_path: str) -> str:
    """
    Genera un ID para una sesión CMv4.0 a partir del MKV origen.

    Formato: 'cmv40_{titulo}_{año}_{timestamp_unix}'.
    """
    stem = Path(source_mkv_path).stem
    m = re.match(r"^(.+?)\s*\((\d{4})\)", stem)
    if m:
        title = re.sub(r"[^\w]", "_", m.group(1).strip())[:40]
        year  = m.group(2)
    else:
        title = re.sub(r"[^\w]", "_", stem)[:40]
        year  = "0000"
    ts = int(datetime.now(timezone.utc).timestamp())
    return f"cmv40_{title}_{year}_{ts}"
