"""
main.py — Backend FastAPI de HDO Blu-ray Toolkit

Punto de entrada de la aplicación: crea la `app`, sirve la SPA y monta los
routers de las otras dos pestañas.

Lo que queda AQUÍ es el **Tab 1 (Blu-Ray ISO → MKV)** más lo que es de toda
la aplicación:

  * Tab 1 — `/api/sources`, `/api/analyze`, las sesiones, la cola FIFO y el
    orquestador del pipeline (`_run_pipeline`: Fase D + Fase E, cancelación,
    validación final del MKV), los endpoints de series TV y el WebSocket
    `/ws/{id}` del log de ejecución.
  * Transversal — `/api/health`, `/api/status`, `/api/activity`, versión y
    chequeo de actualizaciones, ajustes (API keys), TMDb, el recovery de
    arranque y el montaje de los estáticos.

Lo que NO está aquí:

  * `routers/tab2.py` — Consultar / Editar MKV (`/api/mkv/*`, el file
    browser, la auditoría de calidad del RPU y el apply con copia).
  * `routers/cmv40.py` — Tab 3, el pipeline CMv4.0 (`/api/cmv40/*`).
  * `paths.py` — los directorios. `workload.py` — el control de admisión de
    los trabajos pesados. `analysis_progress.py` — el paso del análisis, que
    escriben Tab 1 y Tab 2 y lee un solo endpoint.

La dependencia va en un solo sentido: `main` importa los routers, y ningún
router importa `main`. Lo que necesita correr al arrancar se expone como
función pública del router y se llama desde aquí (ver
`recuperar_apply_interrumpido`). Que las URLs no cambien al mover código lo
vigila `tests/test_rutas_no_cambian.py` contra un golden.

─────────────────────────────────────────────────────────────────────
ACCESO AL ISO — LOOP MOUNT DIRECTO (UDF 2.50)
─────────────────────────────────────────────────────────────────────

Los ISOs se montan directamente dentro del contenedor Docker usando
``mount -t udf -o ro,loop``. Requiere ``privileged: true`` en Docker.

  Análisis (Fase A):
    1. mount_iso(iso_path) → loop mount en /mnt/bd/{nombre}/
    2. mkvmerge -J lee el MPLS desde /mnt/bd/{nombre}/
    3. unmount_iso() en finally (siempre, éxito o error)

  Ejecución (Fase D):
    1. mount_iso(iso_path) → loop mount
    2. mkvmerge lee el MPLS desde /mnt/bd/{nombre}/BDMV/PLAYLIST/
    3. unmount_iso() en finally
    4. Fase E procesa el MKV intermedio ya en /mnt/tmp

Con origen `bdmv_folder` o `m2ts` no hay montaje: la abstracción `Source`
resuelve los tres casos y las fases de mount/unmount quedan en no-op.
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from models import (
    AnalyzeRequest,
    ExecutionRecord,
    QueueReorderRequest,
    Session,
    SessionUpdateRequest,
)
from phases.phase_a import run_full_analysis, ISO639_TO_ENGLISH
from phases.phase_b import apply_rules, generate_auto_chapters
from phases.phase_d import (
    find_main_mpls,
    run_phase_d,
    MkvmergePlaylistError,
    m2ts_covers_title,
)
from phases.phase_e import needs_reordering, run_phase_e_direct, run_phase_e_propedit
from phases.iso_mount import mount_iso, unmount_iso, is_mount_available
from queue_manager import queue_manager
import workload
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

# Los directorios viven en `paths.py` — los necesitan main y los routers por
# pestaña, y el router no debe importar main. Se referencian SIEMPRE como
# `paths.X` (no `from paths import X`): así un test que parchee `paths.X` lo ve
# todo el mundo, sin bindings que puedan divergir.
import analysis_progress
import paths

# ── Recuperación de sesiones interrumpidas ───────────────────────────────────
# Al arrancar, las sesiones que quedaron en 'running' o 'queued' (por un
# reinicio inesperado) se resetean a 'pending' para que el usuario pueda
# relanzarlas. Esto se aplica siempre, no solo en DEV_MODE.

import logging as _logging
_logger = _logging.getLogger(__name__)

# Nada puede estar corriendo todavía: el registro de trabajo pesado vive en
# memoria y un reinicio lo deja necesariamente vacío. Explícito para que no
# quede un fantasma si algún día se persistiera.
workload.limpiar()


# ── Auto-cleanup de huérfanos obvios al arrancar ─────────────────────────────
# Limpia ficheros/dirs que claramente NO deben existir tras un reinicio del
# contenedor: tmps de light-profile mayores de 1h y mount points de ISO que
# no están realmente montados según /proc/mounts. Otros tipos (workdirs CMv4.0,
# .mkv.tmp grandes) se dejan para limpieza manual con preview en la UI.

# ── Inventario de basura: UNA tabla, tres consumidores ──────────────────────
#
# El barrido de arranque, el panel de Limpieza y el whitelist del borrado
# describían cada uno su propia lista, y se habían desincronizado:
#
#   · el barrido miraba `/tmp` Y `paths.TMP_DIR`; el panel solo `/tmp`, que es donde
#     los workdirs NO están desde que se movieron a `/mnt/tmp` — así que un
#     huérfano de hasta ~90 GB de HEVC era invisible en la UI;
#   · `mkv_quality_audit_*` lo limpiaba el barrido pero el panel no lo listaba;
#   · y el whitelist solo aceptaba `/tmp/lightprof_`, así que la ruta real
#     tampoco se habría podido borrar aunque el panel la hubiera enseñado.
#
# `bases` puede traer varios directorios (los tmp viven en paths.TMP_DIR, pero se
# sigue mirando /tmp por los restos de versiones anteriores). `patron` es un
# glob del nombre RELATIVO a la base, y limita qué se puede borrar ahí:
# `/mnt/output` solo admite `*.mkv.tmp`, no cualquier MKV.
def _cleanup_targets() -> list[dict]:
    """Se calcula al vuelo, no en import: los tests redirigen los directorios."""
    from phases.cmv40_pipeline import CMV40_WORK_BASE
    from phases.iso_mount import MOUNT_BASE
    from storage import MKV_AUDIT_DIR
    tmp_bases = []
    for b in ("/tmp", paths.TMP_DIR):
        if b and b not in tmp_bases:
            tmp_bases.append(b)
    # `category` identifica el TIPO de objetivo. El panel puede desglosar uno
    # en varios hallazgos (el cache de MKV sale como orphan / corrupt /
    # stale-version / invalid-quality); lo que la tabla decide es qué rutas son
    # tocables, no cómo se etiqueta cada hallazgo.
    return [
        {"category": "cmv40_workdir",     "bases": [str(CMV40_WORK_BASE)],
         "patron": "*"},
        {"category": "iso_mount_zombie",  "bases": [str(MOUNT_BASE)],
         "patron": "*"},
        # El perfil de luminancia ya no tiene pipeline propio (va con la
        # auditoría), así que estos no se vuelven a crear. Se siguen barriendo
        # por los que quedaran de antes: son ~45 GB cada uno.
        {"category": "lightprofile_tmp",  "bases": tmp_bases,
         "patron": "lightprof_*"},
        {"category": "quality_audit_tmp", "bases": tmp_bases,
         "patron": "mkv_quality_audit_*"},
        {"category": "remux_mkv_tmp",     "bases": [str(paths.OUTPUT_DIR_MKV)],
         "patron": "*.mkv.tmp"},
        {"category": "mkv_cache",         "bases": [str(MKV_AUDIT_DIR)],
         "patron": "*.json"},
    ]


def _cleanup_path_allowed(path_str: str) -> tuple[bool, str]:
    """¿Es `path_str` un objetivo legítimo de borrado? (ruta ya normalizada).

    Antes se comprobaba con `path_str.startswith(prefix)` sobre la cadena
    CRUDA, así que `"/mnt/tmp/cmv40/../../library"` pasaba el filtro y llegaba
    a `rmtree` — con `/mnt/output`, `/mnt/tmp` y `/config` montados `rw` y el
    contenedor en modo privileged. Los otros dos validadores de rutas de la
    app (`_safe_library_path` y `_resolve_mkv_path_safe`, en `routers/tab2.py`)
    ya resolvían antes de comparar; el único que no lo hacía era justo el que
    borra.
    """
    from fnmatch import fnmatch
    try:
        real = Path(path_str).resolve()
    except OSError as e:
        return (False, f"ruta no resoluble: {e}")
    for target in _cleanup_targets():
        for base in target["bases"]:
            try:
                base_real = Path(base).resolve()
            except OSError:
                continue
            try:
                rel = real.relative_to(base_real)
            except ValueError:
                continue
            if str(rel) in ("", "."):
                return (False, "es el propio directorio raíz, no un huérfano")
            # Solo el primer nivel: nada de borrar un fichero de DENTRO de un
            # workdir suelto, y desde luego nada de subir por el árbol.
            if len(rel.parts) != 1:
                continue
            if fnmatch(rel.parts[0], target["patron"]):
                return (True, target["category"])
    return (False, "path fuera de los roots permitidos")


def _cleanup_obvious_orphans_at_startup() -> None:
    """Borra silenciosamente huérfanos triviales (tmps cortos, mount points
    sin entry en /proc/mounts). Logging info por cada item borrado."""
    import shutil as _shutil_so
    import time as _time_so
    from pathlib import Path as _Path_so

    # 1. Workdirs temporales de Tab 2 mayores de 1 hora. Las bases y los
    #    patrones salen de `_cleanup_targets()`, la misma tabla que usan el
    #    panel de Limpieza y el whitelist del borrado: cuando estaban escritos
    #    a mano en cada sitio, el panel se quedó mirando `/tmp` y los workdirs
    #    llevaban tiempo en `/mnt/tmp`.
    for _t in _cleanup_targets():
        if _t["category"] not in ("lightprofile_tmp", "quality_audit_tmp"):
            continue
        for _base in _t["bases"]:
            try:
                for lp in _Path_so(_base).glob(_t["patron"]):
                    if not lp.is_dir():
                        continue
                    try:
                        age = _time_so.time() - lp.stat().st_mtime
                    except OSError:
                        continue
                    if age > 3600:
                        try:
                            _shutil_so.rmtree(lp)
                            _logger.info("[Startup cleanup] tmp removed: %s (age %ds)", lp, int(age))
                        except Exception as e:
                            _logger.warning("[Startup cleanup] failed to remove %s: %s", lp, e)
            except Exception as e:
                _logger.warning("[Startup cleanup] scan %s/%s failed: %s",
                                _base, _t["patron"], e)

    # 2. Mount points de ISO sin entry en /proc/mounts (zombies)
    try:
        from phases.iso_mount import MOUNT_BASE as _MB
        mount_base = _Path_so(_MB)
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
    seed_dev_sessions(paths.CONFIG_DIR)


# ── Aplicación FastAPI ────────────────────────────────────────────────────────

app = FastAPI(
    title="HDO Blu-ray Toolkit",
    version="1.3.0",
    description="Convierte ISOs UHD Blu-ray a MKV con selección automática de pistas y soporte Dolby Vision FEL.",
)

# ── Estáticos ─────────────────────────────────────────────────────────────────

# Ruta absoluta derivada de este fichero, no relativa al cwd: el contenedor
# arranca uvicorn con WORKDIR /app y funcionaba por eso, pero importar main
# desde cualquier otro sitio reventaba con "Directory 'static' does not
# exist" — de ahí el `os.chdir(APP_DIR)` que arrastraban media docena de
# tests antes de poder importarlo.
_STATIC_DIR = Path(__file__).resolve().parent / "static"

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index():
    """Sirve la SPA (Single Page Application)."""
    return FileResponse(str(_STATIC_DIR / "index.html"))


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 1 — BLU-RAY ISO → MKV
# ══════════════════════════════════════════════════════════════════════════════
#
# Los endpoints, el orquestador del pipeline y los dos WebSockets viven en
# `routers/tab1.py`, con los mismos paths que tenían aquí. Del arranque se
# encarga `main`: `recuperar_sesiones_interrumpidas()` devuelve a `pending`
# las sesiones que quedaron a medias, antes de aceptar peticiones.
from routers import tab1 as _tab1_routes  # noqa: E402

app.include_router(_tab1_routes.router)
_tab1_routes.recuperar_sesiones_interrumpidas()


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 2 — CONSULTAR / EDITAR MKV
# ══════════════════════════════════════════════════════════════════════════════
#
# Los endpoints y su estado viven en `routers/tab2.py`, con los mismos paths
# que tenían aquí. Lo único que va de `main` al router es el arranque:
# `recuperar_apply_interrumpido()` tiene que correr antes de aceptar
# peticiones, porque limpia el destino parcial de una copia que se quedó a
# medias. La dependencia sigue siendo unidireccional.
from routers import tab2 as _tab2_routes  # noqa: E402

app.include_router(_tab2_routes.router)
_tab2_routes.recuperar_apply_interrumpido()

# ══════════════════════════════════════════════════════════════════════════════
#  TAB 3 — CMv4.0 BD Pipeline endpoints
# ══════════════════════════════════════════════════════════════════════════════

# ── Tab 3 (CMv4.0) ────────────────────────────────────────────────────────────
# Endpoints y orquestación viven en routers/cmv40.py. El módulo se importa
# aquí abajo, después de DEV_MODE y los directorios, y registra sus 37 rutas
# más el WebSocket del log con los mismos paths que antes.
from routers import cmv40 as _cmv40_routes  # noqa: E402

app.include_router(_cmv40_routes.router)
_cmv40_routes.recuperar_sesiones_interrumpidas()



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
    from phases.cmv40_pipeline import CMV40_WORK_BASE as _CWB
    cmv40_work = Path(_CWB)
    cmv40_cfg = paths.CONFIG_DIR / "cmv40"
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
    from phases.iso_mount import MOUNT_BASE as _MB
    mount_base = Path(_MB)
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

    # 3. Workdirs temporales de Tab 2 (luminancia y auditoría de calidad).
    #    Las bases salen de `_cleanup_targets()`: escritas a mano aquí, este
    #    bloque miraba `/tmp` mientras los workdirs se creaban en `paths.TMP_DIR`,
    #    así que un huérfano de decenas de GB no aparecía en el panel. Y de
    #    `mkv_quality_audit_*` no había categoría siquiera.
    _etiquetas_tmp = {
        "lightprofile_tmp": ("Tmp del análisis de luminancia",
                             "Cancelación o crash durante extracción de luminancia (Tab 2)"),
        "quality_audit_tmp": ("Tmp de la auditoría de calidad del RPU",
                              "Cancelación o crash durante la auditoría del RPU (Tab 2)"),
    }
    for _t in _cleanup_targets():
        etiqueta = _etiquetas_tmp.get(_t["category"])
        if not etiqueta:
            continue
        label, motivo = etiqueta
        for _base in _t["bases"]:
            try:
                for lp in Path(_base).glob(_t["patron"]):
                    if not lp.is_dir():
                        continue
                    size = _dir_size(lp)
                    try: age = int(now - lp.stat().st_mtime)
                    except OSError: age = 0
                    out.append({
                        "category": _t["category"],
                        "label": label,
                        "path": str(lp),
                        "size_bytes": size,
                        "age_seconds": age,
                        "safe": age > 3600,
                        "reason": motivo + ("" if age > 3600
                                            else " — RECIENTE, podría estar activo"),
                    })
            except Exception:
                pass

    # 4. .mkv.tmp del remux (Fase G)
    output_base = paths.OUTPUT_DIR_MKV
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


def _delete_orphan_path(path_str: str) -> tuple[bool, int, str]:
    """Borra un huérfano validando la ruta contra `_cleanup_targets`.
    Devuelve (ok, bytes_freed, error_msg)."""
    import shutil as _sh

    permitido, motivo = _cleanup_path_allowed(path_str)
    if not permitido:
        return (False, 0, motivo)
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
    conocidos, que salen de `_cleanup_targets()` — la misma tabla que alimenta
    el barrido de arranque y el panel. Cada item devuelve {ok, freed, error}.

    La validación (base + patrón del nombre, sobre la ruta ya resuelta) vive en
    `_cleanup_path_allowed`, así que las salvaguardas por extensión que había
    aquí sueltas (`/mnt/output` solo .mkv.tmp, `/config/mkv_audits` solo .json)
    son ahora el `patron` de su fila."""
    deleted = []
    failed = []
    total_freed = 0
    for path in body.paths or []:
        ok, freed, err = _delete_orphan_path(path)
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
    rate_limited = False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {"Accept": "application/vnd.github+json"}

            # Intento 1: /releases (lista completa con notas)
            try:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/releases?per_page=30",
                    headers=headers,
                )
                # 403 (cuota agotada, X-RateLimit-Remaining=0) o 429 → NO
                # intentar /tags: gastaría otra de las 60 req/h sin auth y
                # fallaría igual (audit #24).
                if resp.status_code in (403, 429):
                    rate_limited = True
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
            # repos que solo tagean via `git tag` sin crear Releases). NO si
            # /releases fue rate-limited: /tags gastaría otra req del cupo
            # 60/h y fallaría igual (audit #24).
            if not latest_tag and not rate_limited:
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

@app.get("/api/activity", summary="Qué trabajo pesado hay en curso (las 3 pestañas)")
async def app_activity():
    """Lo que impide arrancar otro trabajo pesado, y desde cuándo.

    Sale de memoria (`workload`): este proceso es el único que arranca trabajo.
    Es lo que hay detrás del 409 de los endpoints pesados, expuesto para que la
    UI pueda decir *qué* bloquea, no solo que está bloqueado.
    """
    trabajos = [
        {"clave": t.clave, "tab": t.tab, "que": t.que,
         "segundos": int(t.segundos), "descripcion": t.describir()}
        for t in workload.en_curso()
    ]
    return {"ocupado": bool(trabajos), "trabajos": trabajos}


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

