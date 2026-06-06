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
    """Busca UNA sesión existente con el mismo iso_fingerprint.
    Para discos de serie con varios episodios procesados, devuelve la
    primera encontrada (todas comparten fingerprint). Para casos donde
    el caller necesita la lista completa (modo serie, evitar sobrescritura
    silenciosa de episodios hermanos), usar find_sessions_by_fingerprint."""
    if not fingerprint:
        return None
    for session in list_sessions():
        if session.iso_fingerprint == fingerprint:
            return session
    return None


def find_sessions_by_fingerprint(fingerprint: str) -> list["Session"]:
    """Devuelve TODAS las sesiones con el mismo iso_fingerprint.
    Caso típico: BDMV/ISO de serie con N episodios → N sesiones que
    comparten el fingerprint del disco (el del .iso, o el del m2ts más
    grande del BDMV)."""
    if not fingerprint:
        return []
    return [s for s in list_sessions() if s.iso_fingerprint == fingerprint]


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


# ══════════════════════════════════════════════════════════════════════
#  TAB 2 — Cache de análisis de MKV (basic + quality)
# ══════════════════════════════════════════════════════════════════════
#
# Convierte la apertura de un MKV ya analizado en instantánea (~30 ms).
# Cache "solo resultado" — un JSON pequeño (5-15 KB) por MKV. NUNCA
# se cachean ficheros pesados (HEVC intermedio, RPU.bin, JSON export).
#
# Estructura: /config/mkv_audits/{sha256_1mb}.json
#
# Dos bloques independientes dentro del mismo fichero:
#   - basic: MkvAnalysisResult (mkvmerge + MediaInfo + PGS + dovi sample)
#   - quality: análisis profundo del RPU (L8/L2 combos + classifier)
#
# Cada bloque lleva su propia versión. Si la lógica de un motor cambia,
# se bumpea la constante correspondiente (CACHE_VERSION_BASIC en
# mkv_analyze.py, CACHE_VERSION_QUALITY en el endpoint quality-audit) y
# el cache se invalida automáticamente la próxima vez que se lee.

MKV_AUDIT_DIR = CONFIG_DIR / "mkv_audits"


def compute_mkv_fingerprint(mkv_path: str) -> dict | None:
    """Fingerprint de un MKV: SHA-256(primer 1 MB) + size + mtime_ns.

    La COMPARACIÓN de caché usa solo SHA(1 MB) + size (ver
    _fingerprint_content_match) — identifica el contenido. mtime_ns se sigue
    calculando y guardando como referencia/diagnóstico, pero NO entra en el
    match: un cambio de fecha sin cambio de contenido (touch, copia SMB/rsync
    sin preservar times, rollback ZFS, la copia Library→output de Tab 2)
    forzaba antes una re-auditoría de 5-10 min inútil. Las ediciones in-place
    de mkvpropedit se siguen detectando porque cambian el 1er MB (el elemento
    Tracks vive al principio) y porque el endpoint apply invalida el cache
    por path tras editar.

    Devuelve None si el fichero no existe.
    """
    p = Path(mkv_path)
    if not p.exists():
        return None
    st = p.stat()
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read(FINGERPRINT_CHUNK))
    return {
        "sha256_1mb": h.hexdigest(),
        "size_bytes": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }


def _mkv_audit_path(fingerprint_sha: str) -> Path:
    """Ruta del fichero de cache para un fingerprint SHA dado."""
    return MKV_AUDIT_DIR / f"{fingerprint_sha}.json"


def _fingerprint_content_match(persisted_fp: dict, fingerprint: dict) -> bool:
    """¿Dos fingerprints apuntan al mismo CONTENIDO? Compara SHA(1er MB) +
    tamaño. Deliberadamente NO compara mtime_ns (ver compute_mkv_fingerprint):
    el contenido es lo que importa para el cache, y el mtime cambia en copias/
    touch/rollback sin que el RPU ni las pistas cambien. Centralizado para que
    los 3 sitios (read + write basic + write quality) no se desincronicen."""
    if not persisted_fp:
        return False
    return (persisted_fp.get("sha256_1mb") == fingerprint.get("sha256_1mb")
            and persisted_fp.get("size_bytes") == fingerprint.get("size_bytes"))


def read_mkv_cache(
    fingerprint: dict,
    cache_version_basic: int,
    cache_version_quality: int,
) -> dict | None:
    """Lee el cache de un MKV si existe y los tres componentes del fingerprint
    coinciden con los persistidos.

    Devuelve un dict con shape:
        {
            "basic": {...} | None,      # MkvAnalysisResult serializado (o None si no hay)
            "quality": {...} | None,    # quality audit (o None si no hay)
            "cached_at": "...",
        }
    Cada bloque solo se devuelve si su `versions.{basic|quality}` coincide
    con la versión actual del clasificador. Si una versión está obsoleta,
    ese bloque se devuelve como None y el caller decide (re-analizar
    automáticamente vs ofrecer botón).

    Devuelve None si:
      - El fichero de cache no existe.
      - El fingerprint no coincide (MKV modificado externamente o re-encoded).
      - El fichero está corrupto.
    """
    if not fingerprint:
        return None
    path = _mkv_audit_path(fingerprint["sha256_1mb"])
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("MKV cache corrupto, ignorado: %s — %s", path.name, e)
        return None
    persisted_fp = data.get("fingerprint") or {}
    if not _fingerprint_content_match(persisted_fp, fingerprint):
        # MKV cambió desde la última auditoría → cache inválido. NO lo
        # borramos del disco — la próxima escritura lo sobrescribirá con
        # el nuevo fingerprint.
        return None

    versions = data.get("versions") or {}
    basic = data.get("basic") if versions.get("basic") == cache_version_basic else None
    quality = data.get("quality") if versions.get("quality") == cache_version_quality else None
    return {
        "basic": basic,
        "quality": quality,
        "cached_at": data.get("cached_at"),
    }


def _write_mkv_cache_full(
    fingerprint: dict,
    versions: dict,
    basic: dict | None,
    quality: dict | None,
    original_file_path: str | None = None,
) -> None:
    """Helper interno: vuelca el fichero completo con todos los bloques.

    `original_file_path` se persiste top-level como pista del MKV de origen.
    Lo usa el scan de huérfanos (settings → mantenimiento) para detectar
    caches cuyo MKV ya no existe en disco (borrado o renombrado fuera de
    Tab 2). Si el MKV está vivo en el path persistido, no es huérfano —
    si el path no existe Y el usuario no lo movió, el cache ocupa espacio
    sin servir a nada.
    """
    MKV_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = _mkv_audit_path(fingerprint["sha256_1mb"])
    payload = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "versions": versions,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "basic": basic,
        "quality": quality,
    }
    if original_file_path:
        payload["original_file_path"] = original_file_path
    _atomic_write_json(path, json.dumps(payload, indent=2, ensure_ascii=False))


def _read_cache_raw(fingerprint_sha: str) -> dict | None:
    """Lee el JSON del cache sin validar fingerprint ni versiones. Usado
    por los writers para preservar metadata top-level del fichero existente
    (original_file_path, bloque opuesto) y por el scan de huérfanos.

    Devuelve None si el fichero no existe o está corrupto."""
    if not fingerprint_sha:
        return None
    path = _mkv_audit_path(fingerprint_sha)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_mkv_cache_basic(
    fingerprint: dict,
    cache_version_basic: int,
    cache_version_quality_existing: int | None,
    basic_payload: dict,
    original_file_path: str | None = None,
) -> None:
    """Persiste el bloque basic del cache. Si ya existía un bloque quality
    válido para el mismo fingerprint, se preserva (los bloques son
    independientes — re-analizar el basic no debe invalidar la auditoría
    de calidad si el MKV no cambió).

    `cache_version_quality_existing` es la versión del bloque quality
    que el caller leyó previamente (o None si no había). Sirve para
    re-escribir el bloque junto con el basic conservando su versión.

    `original_file_path` se persiste para que el scan de huérfanos pueda
    detectar caches cuyo MKV ya no existe. Si no se pasa pero el cache
    existente ya lo tenía, se preserva.
    """
    existing_quality = None
    existing_orig_path = None
    if fingerprint:
        existing_raw = _read_cache_raw(fingerprint["sha256_1mb"])
        if existing_raw:
            persisted_fp = existing_raw.get("fingerprint") or {}
            # Solo preservar si fingerprint coincide (MKV no cambió)
            if _fingerprint_content_match(persisted_fp, fingerprint):
                versions = existing_raw.get("versions") or {}
                if versions.get("quality") == cache_version_quality_existing:
                    existing_quality = existing_raw.get("quality")
                existing_orig_path = existing_raw.get("original_file_path")
    versions = {"basic": cache_version_basic}
    if existing_quality is not None:
        versions["quality"] = cache_version_quality_existing
    _write_mkv_cache_full(
        fingerprint, versions, basic_payload, existing_quality,
        original_file_path=original_file_path or existing_orig_path,
    )


def write_mkv_cache_quality(
    fingerprint: dict,
    cache_version_basic_existing: int | None,
    cache_version_quality: int,
    quality_payload: dict,
    original_file_path: str | None = None,
) -> None:
    """Persiste el bloque quality del cache. Preserva el bloque basic
    existente si su versión coincide con la actual, y el original_file_path
    si ya estaba persistido (o lo añade si se pasa nuevo)."""
    existing_basic = None
    existing_basic_version = None
    existing_orig_path = None
    if fingerprint:
        existing_raw = _read_cache_raw(fingerprint["sha256_1mb"])
        if existing_raw:
            persisted_fp = existing_raw.get("fingerprint") or {}
            if _fingerprint_content_match(persisted_fp, fingerprint):
                versions = existing_raw.get("versions") or {}
                if versions.get("basic") == cache_version_basic_existing:
                    existing_basic = existing_raw.get("basic")
                    existing_basic_version = versions.get("basic")
                existing_orig_path = existing_raw.get("original_file_path")
    versions = {"quality": cache_version_quality}
    if existing_basic_version is not None:
        versions["basic"] = existing_basic_version
    _write_mkv_cache_full(
        fingerprint, versions, existing_basic, quality_payload,
        original_file_path=original_file_path or existing_orig_path,
    )


def list_mkv_audit_entries() -> list[dict]:
    """Lista resumida de todos los ficheros en MKV_AUDIT_DIR. Para cada
    entry devuelve metadata top-level + flags rápidos de presencia/validez
    sin reconstruir el MkvAnalysisResult entero. Usado por el scan de
    huérfanos (settings → mantenimiento) para detectar:
      - caches con `original_file_path` ausente del filesystem (orphan)
      - caches con quality_payload basura (total_frames_rpu == 0)
      - caches con versions obsoletas vs CACHE_VERSION_BASIC/QUALITY
      - ficheros corruptos (JSON inválido)
    """
    if not MKV_AUDIT_DIR.exists():
        return []
    import time as _t
    now = _t.time()
    out: list[dict] = []
    for path in MKV_AUDIT_DIR.glob("*.json"):
        try:
            stat = path.stat()
            size = stat.st_size
            age = int(now - stat.st_mtime)
        except OSError:
            size, age = 0, 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            out.append({
                "cache_path": str(path),
                "size_bytes": size,
                "age_seconds": age,
                "corrupt": True,
                "error": str(e),
            })
            continue
        quality = data.get("quality") or None
        out.append({
            "cache_path": str(path),
            "fingerprint_sha": (data.get("fingerprint") or {}).get("sha256_1mb", ""),
            "original_file_path": data.get("original_file_path"),
            "versions": data.get("versions") or {},
            "cached_at": data.get("cached_at"),
            "size_bytes": size,
            "age_seconds": age,
            "basic_present": bool(data.get("basic")),
            "quality_present": bool(quality),
            "quality_total_frames": quality.get("quality_total_frames_rpu") if quality else None,
            "quality_classification": quality.get("quality_classification") if quality else None,
            "corrupt": False,
        })
    return out


def invalidate_mkv_cache(fingerprint_sha: str) -> bool:
    """Borra el fichero de cache de un MKV. Devuelve True si existía."""
    if not fingerprint_sha:
        return False
    path = _mkv_audit_path(fingerprint_sha)
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError as e:
            logger.warning("No se pudo borrar cache MKV %s: %s", path.name, e)
    return False


def invalidate_mkv_cache_by_path(mkv_path: str) -> bool:
    """Calcula el fingerprint del MKV y borra su cache si existe.
    Tolerante: si el fichero no existe o no se puede leer, devuelve False
    sin lanzar."""
    try:
        fp = compute_mkv_fingerprint(mkv_path)
        if not fp:
            return False
        return invalidate_mkv_cache(fp["sha256_1mb"])
    except OSError:
        return False
