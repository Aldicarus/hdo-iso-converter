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

from models import Session

logger = logging.getLogger(__name__)

# Directorio de configuración — puede sobreescribirse con CONFIG_DIR
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))


def _session_path(session_id: str) -> Path:
    """Devuelve la ruta completa del fichero JSON de una sesión."""
    return CONFIG_DIR / f"{session_id}.json"


def save_session(session: Session) -> None:
    """
    Persiste una sesión en disco como JSON con indentación.

    Actualiza updated_at al momento actual antes de escribir.
    Crea el directorio CONFIG_DIR si no existe. Sobreescribe el fichero
    si ya existe (cada guardado es atómico a nivel de write_text).
    """
    session.updated_at = datetime.now(timezone.utc)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = _session_path(session.id)
    path.write_text(session.model_dump_json(indent=2), encoding="utf-8")


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
        if path.name in ("settings.json", "queue_state.json"):
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
