"""
routers/tab2.py — endpoints del Tab 2 (Consultar / Editar MKV).

Salió de `main.py`, donde eran 1.266 líneas encajadas entre Tab 1 y el
`include_router` de Tab 3. El corte fue limpio: el bloque era **contiguo**
y solo dependía de 14 nombres externos, doce de ellos imports de stdlib o
de `dev_fixtures`. La dependencia queda unidireccional igual que con
CMv4.0 — `main` incluye este router, este router no importa `main`.

Lo único que va en sentido contrario es el arranque: `recuperar_apply_
interrumpido()` la llama `main` en su startup, porque el recovery de una
copia a medias tiene que correr antes de aceptar peticiones. Es una
función pública de este módulo, no un import de `main` desde aquí.

Tres piezas conviven en el fichero:

  * **File browser multi-root** — `/api/library/browse` lo comparten Tab 2
    y Tab 3 (cada uno pide los roots que le interesan); la validación de
    path traversal vive en `_safe_library_path` y `_resolve_mkv_path_safe`.
  * **Auditoría de calidad del RPU** — job singleton con su estado en
    memoria (`_mkv_quality_state`), cancelación dirigida por `audit_id` y
    dedup por `request_id`, que es lo que evita que el navegador reenvíe
    el POST largo al perder el foco.
  * **Apply** — `mkvpropedit` in-place, o copia previa a `/mnt/output` con
    progreso real y cancelación cooperativa cuando el MKV vive en la
    biblioteca read-only. Su estado se persiste (`mkv_apply_state.json`)
    para sobrevivir a un reinicio del contenedor.

El contrato HTTP está fijado en `tests/test_endpoints_tab1_tab2.py`, y que
las URLs no cambien con este movimiento, en `test_rutas_no_cambian.py`.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

import analysis_progress
import paths
import workload
from dev_fixtures import (
    DEV_FAKE_MKV_FILES,
    DEV_MODE,
    build_fake_mkv_analysis,
    build_fake_mkv_apply,
)
from models import MkvEditRequest
from phases.mkv_analyze import analyze_mkv, apply_mkv_edits

_logger = logging.getLogger(__name__)

router = APIRouter()




# Roots disponibles para el file browser (Tab 2 y Tab 3).
# Cada key apunta a una Path mountada en el contenedor. El frontend pide
# `?root=library`, `?root=output` o `?root=downloaded` para conmutar entre
# arboles. Tab 2 expone library + output; Tab 3 expone library + downloaded
# (carpeta de descargas, que contiene ISOs y tambien MKVs sueltos).
# Solo paths bajo estos roots son aceptados por endpoints downstream
# (analyze, light-profile) — proteccion contra path traversal arbitrario.



# Directorios "no peliculas" que NUNCA queremos exponer en el browser:
#   - .zfs/snapshot — ZFS snapshots ocultos en QuTS hero (recursivos eternos)
#   - @eaDir, .DS_Store, Thumbs.db — metadata de Synology/macOS/Windows
#   - .Recycle, #recycle, $RECYCLE.BIN — papeleras varias
#   - dotfiles — ocultos en general


def _safe_library_path(rel_path: str, root_key: str = "library") -> tuple[Path, Path]:
    """Resuelve `rel_path` relativo al root indicado validando que no escape.
    Lanza HTTPException 400 si root es desconocido o si la path escapa.
    Devuelve (path_absoluta_resuelta, path_base_resuelta).
    """
    base = paths.LIBRARY_ROOTS.get(root_key)
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
    candidate = Path(p) if p.startswith("/") else (paths.OUTPUT_DIR_MKV / p)
    candidate = candidate.resolve()
    # Acepta si cae bajo CUALQUIER root configurado
    for root in paths.LIBRARY_ROOTS.values():
        try:
            candidate.relative_to(root.resolve())
            return candidate
        except ValueError:
            continue
    raise HTTPException(
        status_code=400,
        detail=f"Ruta fuera de los directorios permitidos: {p}"
    )


@router.get("/api/library/browse", summary="Navega árboles de MKVs/ISOs/M2TS/BDMV")
async def library_browse(
    root: str = "library",
    path: str = "",
    filter: str = "mkv",
):
    """Lista subdirectorios + ficheros bajo paths.LIBRARY_ROOTS[root]/<path>
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

    base_dir = paths.LIBRARY_ROOTS.get(root)
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
                if name == ".zfs" or name in paths.LIBRARY_HIDDEN_DIRS:
                    continue
                # Otros dotfiles también ocultos
                continue
            if name in paths.LIBRARY_HIDDEN_DIRS:
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


@router.get("/api/mkv/files", summary="Lista MKVs disponibles en /mnt/output")
async def list_mkv_files():
    """Devuelve la lista de ficheros .mkv en el directorio de salida."""
    # ⚠️ DEV MODE — branch que devuelve fixtures sin tocar el filesystem
    if DEV_MODE:
        return {"files": DEV_FAKE_MKV_FILES}
    if not paths.OUTPUT_DIR_MKV.exists():
        return {"files": []}
    files = sorted(
        [p.name for p in paths.OUTPUT_DIR_MKV.glob("*.mkv")],
        key=str.lower,
    )
    return {"files": files}


@router.get("/api/mkv/files-in-isos", summary="Lista MKVs en el directorio de ISOs (para Tab 3 Fase B)")
async def list_mkv_files_in_isos():
    """Devuelve .mkv presentes en paths.ISOS_DIR con su ruta absoluta.

    Búsqueda NO recursiva — solo ficheros directos en paths.ISOS_DIR. Evita recursar
    en snapshots ZFS ocultos (`.zfs/snapshot/…`) de QNAP QuTS, que devolverían
    cientos de ficheros históricos con el mismo nombre.
    """
    if DEV_MODE:
        return {"files": [{"name": f, "path": f"/mnt/isos/{f}"} for f in DEV_FAKE_MKV_FILES]}
    if not paths.ISOS_DIR.exists():
        return {"files": []}
    files = sorted(
        [{"name": p.name, "path": str(p)} for p in paths.ISOS_DIR.glob("*.mkv") if p.is_file()],
        key=lambda x: x["name"].lower(),
    )
    return {"files": files}


@router.post("/api/mkv/analyze", summary="Analiza un MKV existente")
async def analyze_mkv_endpoint(body: dict):
    """
    Ejecuta mkvmerge -J + MediaInfo + ffprobe (packet counts) + dovi_tool
    sobre un MKV y devuelve toda la información de pistas, capítulos y
    metadatos. Puede tardar 1-3 min en MKVs grandes.

    Body: ``{"file_path": "Movie.mkv", "force_refresh": false}``.

    El primer análisis persiste el resultado en /config/mkv_audits/. Re-abrir
    el mismo MKV es instantáneo (cache HIT). ``force_refresh: true`` invalida
    el cache (botón "↻ Re-analizar" del frontend) y rehace el pipeline.

    Durante la ejecución emite progreso en ``analysis_progress``, que es de
    donde lo lee ``GET /api/analyze/progress`` — el mismo endpoint que pollea
    el modal de Tab 1.
    """
    rel_path = body.get("file_path", "")
    force_refresh = bool(body.get("force_refresh", False))
    # ⚠️ DEV MODE — branch que devuelve fixtures sin tocar el filesystem
    if DEV_MODE:
        # Simulación de progreso para que el modal se vea en dev — incluye
        # barra PGS animada del 0 al 100% para probar la UX.
        import asyncio as _aio
        analysis_progress.fijar(step="identify", done=False)
        await _aio.sleep(0.4)
        analysis_progress.fijar(step="mediainfo", done=False)
        await _aio.sleep(0.6)
        for pct in (5, 20, 45, 70, 90, 100):
            eta = max(0, int((100 - pct) * 0.04))
            analysis_progress.fijar(step="pgs", done=False, pct=pct, eta_s=eta)
            await _aio.sleep(0.35)
        analysis_progress.fijar(step="dovi", done=False)
        await _aio.sleep(0.5)
        analysis_progress.fijar(step="", done=True)
        return build_fake_mkv_analysis(rel_path)

    # Acepta tanto rutas absolutas (file browser nuevo de Tab 2/3) como
    # filenames relativos a /mnt/output (compat legacy). Valida que la ruta
    # cae bajo un root permitido (paths.LIBRARY_ROOTS).
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
            analysis_progress.fijar(step="", done=True)
            return
        if step == "pgs":
            analysis_progress.fijar(step="pgs", done=False, pct=0, eta_s=0)
        else:
            analysis_progress.fijar(step=step, done=False)

    async def _mkv_pgs_progress_callback(pct: float, eta_s: int):
        analysis_progress.fijar(step="pgs", done=False, pct=round(pct, 1), eta_s=eta_s)

    analysis_progress.fijar(step="identify", done=False)
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
        analysis_progress.fijar(step="", done=True)
        return result.model_dump()
    except Exception as e:
        analysis_progress.fijar(step="", done=True, error=str(e))
        _logger.exception("Error analizando MKV %s", mkv_full)
        raise HTTPException(status_code=500, detail=str(e))


# El perfil de luminancia tenía su propio endpoint, su propio job singleton
# (`_light_profile_state` + seis helpers `_lp_*`) y su propio modal con
# polling: ~520 líneas de maquinaria calcada de la auditoría de calidad, para
# un análisis que hacía EXACTAMENTE la misma extracción.
#
# Los dos se separaron porque cada uno era caro. Ya no lo son por separado:
# extraer el RPU del MKV es el ~97 % del coste (medido: ~650 s frente a ~7 s
# del export por niveles) y ahora se comparte — `analyze_rpu_quality_for_mkv`
# con `con_luminancia=True` produce los dos y los cachea juntos, y el perfil
# viaja en `DoviInfo.light_profile`. De paso el perfil pasa a estar CACHEADO,
# que era la asimetría más rara: la calidad costaba 10 min una vez y la
# luminancia 10 min cada vez que se miraba, porque no se persistía.

_mkv_quality_state: dict = {
    "active": False,
    "audit_id": None,     # uuid de la audit actual — discrimina cancels obsoletos
    "request_id": None,   # id de cliente para dedup de re-envíos del POST largo
    "step": "",           # "ffmpeg" | "extract_rpu" | "combos" | "done" | "error"
    "step_label": "",
    "global_pct": 0,      # 0-100 total
    "elapsed_s": 0,
    "log_lines": [],      # rolling log (max 200 líneas)
    "error": None,
    "started_at": 0.0,
    "result": None,       # dict con los campos quality_* tras éxito
    "file_name": "",
}

_mkv_quality_active_proc: dict = {"proc": None}

# Cancel por audit_id en lugar de bool global. Si el cancel del audit "AAA"
# tarda 3-13s en completarse (kill + wait reap), durante ese tiempo el usuario
# puede haber lanzado un nuevo audit "BBB"; el cancel viejo NO debe afectarle.
# El pipeline verifica si requested_for_id coincide con el audit_id actual del
# state — si no, el cancel se considera obsoleto y se ignora.
_mkv_quality_cancel: dict = {"requested_for_id": None}


def _mkv_quality_reset(file_name: str = "") -> str:
    import time as _t
    import uuid as _uuid
    _mkv_quality_active_proc["proc"] = None
    # NO tocamos _mkv_quality_cancel["requested_for_id"] aquí — un cancel viejo
    # del audit anterior referencia un audit_id que ya no coincide con el nuevo,
    # por lo que _mkv_quality_check_cancel lo ignorará automáticamente. Lo
    # limpiamos por higiene para no acumular un valor obsoleto indefinido.
    _mkv_quality_cancel["requested_for_id"] = None
    audit_id = _uuid.uuid4().hex[:12]
    _mkv_quality_state.update({
        "active": True,
        "audit_id": audit_id,
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
    return audit_id


def _mkv_quality_log(msg: str, target_audit_id: str | None = None):
    """Añade una línea al state.log_lines del audit actual.

    Si se pasa `target_audit_id`, se verifica que coincida con el audit_id
    activo — si no, la línea se descarta silenciosamente. Esto evita que
    un cancel/except tardío de un audit anterior contamine el log de un
    audit posterior que ya ha empezado fresco.
    """
    if target_audit_id is not None and target_audit_id != _mkv_quality_state.get("audit_id"):
        return  # audit obsoleto — silencioso
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


def _mkv_quality_state_finalize_if(target_audit_id: str, error_msg: str, step: str = "error"):
    """Marca el audit como terminado en state SOLO si audit_id sigue siendo
    el target. Usado por el cancel endpoint, el except del endpoint principal,
    y cualquier otro path que finalice un audit.

    Idempotente: si el audit YA fue finalizado con el mismo error, no duplica
    log ni re-escribe. Esto evita que el flujo "user cancela → cancel endpoint
    finaliza con 'Cancelado por el usuario' → pipeline detecta cancel y raise
    → except endpoint finaliza otra vez con el mismo msg" añada dos líneas
    idénticas al log."""
    current = _mkv_quality_state.get("audit_id")
    if current != target_audit_id:
        _logger.info(
            "[QualityFinalize] SKIP audit_id=%s (current=%s) msg=%r",
            target_audit_id, current, error_msg,
        )
        return False
    # Idempotencia: ya estaba finalizado con el mismo error → no-op silencioso
    if (not _mkv_quality_state.get("active")
            and _mkv_quality_state.get("error") == error_msg):
        _logger.info(
            "[QualityFinalize] NO-OP audit_id=%s already finalized msg=%r",
            target_audit_id, error_msg,
        )
        return True
    _logger.warning(
        "[QualityFinalize] APPLY audit_id=%s msg=%r step=%s",
        target_audit_id, error_msg, step,
    )
    _mkv_quality_active_proc["proc"] = None
    _mkv_quality_log(f"✗ {error_msg}", target_audit_id=target_audit_id)
    _mkv_quality_state["error"] = error_msg
    _mkv_quality_state["step"] = step
    _mkv_quality_state["active"] = False
    return True


def _mkv_quality_check_cancel():
    """Lanza RuntimeError si el usuario canceló ESTE audit. Llamar entre pasos.

    Compara requested_for_id contra el audit_id activo: si difieren, el cancel
    es de un audit anterior ya finalizado (con cancel POST que tardó en reapear
    su subprocess) y no debe afectar al audit actual.
    """
    target_id = _mkv_quality_cancel.get("requested_for_id")
    if target_id is not None and target_id == _mkv_quality_state.get("audit_id"):
        raise RuntimeError("Cancelado por el usuario")


@router.get("/api/mkv/quality-audit/progress")
async def mkv_quality_audit_progress():
    """Polling endpoint para el modal de auditoría."""
    return dict(_mkv_quality_state)


async def _mkv_quality_reap_proc(proc):
    """Reap un subprocess en background tras SIGTERM (best-effort). Si no
    responde en 5s, escala a SIGKILL. Esto se hace en una task suelta para
    que el endpoint /cancel pueda devolver inmediatamente — sin esto el
    cancel bloqueaba 3-13s y permitía races con re-lanzamientos."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            _logger.warning(
                "quality-audit: subprocess no reaped tras SIGKILL+15s (zombie probable)"
            )
    except Exception as e:
        _logger.warning("quality-audit reap subprocess falló: %s", e)


@router.post("/api/mkv/quality-audit/cancel")
async def mkv_quality_audit_cancel(request: Request):
    """Solicita cancelación cooperativa + SIGTERM (sin esperar el reap).

    Retorna inmediato (< 100ms) — el reap del subprocess se hace en
    background. Diseño anti-race: la espera bloqueante anterior (hasta 13s)
    permitía al usuario relanzar el audit antes de que el cancel terminara,
    creando un fenómeno donde el cancel ORIGINAL pisaba el state del audit
    nuevo al finalizar. Con cancel non-blocking + audit_id guard en
    _mkv_quality_state_finalize_if, el flujo es:

      1. Cancel marca requested_for_id e inmediatamente finaliza el state
         del audit target (si sigue siendo current).
      2. SIGTERM al subprocess y task de reap en background.
      3. El pipeline detecta cancel en su _check() → raise. El except del
         endpoint también usa _state_finalize_if con su audit_id snapshot —
         si el reset del audit nuevo ya pasó, no pisa nada.
    """
    # Logging defensivo para diagnosticar "cancels fantasma" — quién hace
    # POST cancel, sobre qué audit_id, desde qué cliente.
    client_addr = f"{request.client.host}:{request.client.port}" if request.client else "?"
    user_agent = request.headers.get("user-agent", "?")[:60]
    # audit_id que el cliente QUIERE cancelar (capturado del /progress). Sin
    # esto el cancel apuntaba siempre al audit VIVO en el instante del POST:
    # si el usuario cancelaba A y relanzaba B muy rápido, este cancel (tardío)
    # leía audit_id=B y mataba B → falso "Cancelado por el usuario" sobre la
    # auditoría nueva. Con el target explícito, un cancel de A obsoleto se
    # ignora en cuanto B ya tomó el relevo.
    requested_audit_id = None
    try:
        _body = await request.json()
        if isinstance(_body, dict):
            requested_audit_id = _body.get("audit_id") or None
    except Exception:
        requested_audit_id = None
    if not _mkv_quality_state.get("active"):
        _logger.info(
            "[QualityCancel] NO-OP — no hay audit activo (caller=%s, ua=%s)",
            client_addr, user_agent,
        )
        return {"ok": False, "reason": "no_active_job"}
    target_audit_id = _mkv_quality_state.get("audit_id")
    if requested_audit_id and requested_audit_id != target_audit_id:
        _logger.info(
            "[QualityCancel] STALE — cancel para audit_id=%s pero current=%s; "
            "ignorado (caller=%s, ua=%s)",
            requested_audit_id, target_audit_id, client_addr, user_agent,
        )
        return {"ok": False, "reason": "stale_audit_id"}
    target_proc = _mkv_quality_active_proc.get("proc")
    target_step = _mkv_quality_state.get("step")
    started_at = _mkv_quality_state.get("started_at") or 0
    import time as _t
    elapsed = int(_t.monotonic() - started_at) if started_at else 0
    _logger.warning(
        "[QualityCancel] RECIBIDO — audit_id=%s step=%s elapsed=%ds "
        "caller=%s ua=%s",
        target_audit_id, target_step, elapsed, client_addr, user_agent,
    )
    _mkv_quality_cancel["requested_for_id"] = target_audit_id
    # SIGTERM inmediato + reap en background (no bloquea el endpoint)
    if target_proc:
        try:
            target_proc.terminate()
        except Exception as e:
            _logger.warning("quality-audit cancel: terminate falló: %s", e)
        asyncio.create_task(_mkv_quality_reap_proc(target_proc))
    # Finaliza el state del audit target — protegido por audit_id guard,
    # si el usuario ya relanzó NO pisa el state del audit nuevo.
    finalized = _mkv_quality_state_finalize_if(
        target_audit_id, "Cancelado por el usuario", step="error",
    )
    if not finalized:
        _logger.info(
            "quality-audit cancel obsoleto (audit %s ya no activo, "
            "current=%s) — no se pisa el state nuevo",
            target_audit_id, _mkv_quality_state.get("audit_id"),
        )
    return {"ok": True}


@router.get("/api/mkv/cache-info", summary="Diagnóstico del cache de un MKV")
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


@router.delete("/api/mkv/cache-info", summary="Borra el cache de un MKV")
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


@router.post("/api/mkv/quality-audit", summary="Auditoría profunda del RPU (on-demand)")
async def mkv_quality_audit_endpoint(body: dict, request: Request = None):
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

    # Dedup anti re-envío: el POST de la auditoría se queda abierto ~12 min sin
    # respuesta; cuando la pestaña pierde el foco y cae la conexión, el navegador
    # (o un proxy) RE-ENVÍA el POST con el MISMO body → arrancaba un audit
    # duplicado tras completar el anterior (confirmado en logs: 2º START, mismo
    # fichero, puerto nuevo, sin clic). El re-envío trae el mismo request_id →
    # NO arrancamos otro: devolvemos el audit en curso o el resultado ya hecho.
    request_id = (body.get("request_id") or "").strip() or None
    if request_id and request_id == _mkv_quality_state.get("request_id"):
        st = _mkv_quality_state
        if st.get("active"):
            _logger.info("[QualityAudit] re-envío del request_id=%s en curso — ignorado", request_id)
            return {"started": True, "duplicate": True, "audit_id": st.get("audit_id")}
        if st.get("result"):
            _logger.info("[QualityAudit] re-envío del request_id=%s ya completado — devuelvo resultado", request_id)
            return st["result"]
        msg = st.get("error") or "La auditoría anterior no produjo resultado"
        raise HTTPException(status_code=499 if "Cancelado" in msg else 500, detail=msg)

    if _mkv_quality_state.get("active"):
        raise HTTPException(
            status_code=409,
            detail="Ya hay un análisis en curso. Cancélalo o espera a que termine.",
        )
    # Y tampoco si lo pesado está en otra pestaña: extraer el RPU son ~10 min
    # de disco y CPU, y solaparlo con un rip no hace que acaben antes.
    workload.exigir_libre()

    # my_audit_id es el id propio de este audit — se usa para que except y
    # finally NO pisen el state si un audit posterior ya hizo reset (race
    # cuando el usuario cancela y relanza muy rápido).
    my_audit_id = _mkv_quality_reset(file_name=mkv_path_obj.name)
    workload.registrar(my_audit_id, workload.TAB_MKV,
                       f"análisis extendido de {mkv_path_obj.name}")
    _mkv_quality_state["request_id"] = request_id
    client_addr = (f"{request.client.host}:{request.client.port}"
                   if request and request.client else "?")
    _logger.warning(
        "[QualityAudit] START audit_id=%s file=%s caller=%s",
        my_audit_id, mkv_path_obj.name, client_addr,
    )

    # Los 3 callbacks van GUARDADOS por my_audit_id: si el usuario canceló este
    # audit y relanzó otro, el pipeline de ESTE (moribundo, aún vivo unos ms
    # mientras su subprocess muere) NO debe escribir estado/log/proc del audit
    # nuevo. El finalize ya estaba guardado; esto cierra la contaminación
    # cosmética (líneas "✗ ffmpeg falló" del A muerto colándose en el log de B).
    def _progress_cb(step: str, pct: float, label: str):
        # SOLO actualiza estado para el modal/polling. NO loguea — la lógica
        # de logging detallado vive en analyze_rpu_quality_for_mkv via
        # log_callback, con marcadores semánticos ricos (━━━ pasos, comandos
        # $, ✓ resultados). Hacer log aquí duplicaría líneas.
        if _mkv_quality_state.get("audit_id") != my_audit_id:
            return
        _mkv_quality_state["step"] = step
        _mkv_quality_state["global_pct"] = int(pct)
        if label:
            _mkv_quality_state["step_label"] = label

    def _register(proc):
        if _mkv_quality_state.get("audit_id") != my_audit_id:
            return
        _mkv_quality_active_proc["proc"] = proc

    def _log_cb(msg: str):
        # _mkv_quality_log ya descarta la línea si el audit_id no coincide.
        _mkv_quality_log(msg, target_audit_id=my_audit_id)

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
            log_callback=_log_cb,
            # Los dos análisis en la misma pasada. Extraer el RPU es el ~97 %
            # del coste y se comparte; pedir L5/L6 en el mismo export son
            # segundos. Separados eran dos botones y dos extracciones.
            con_luminancia=True,
        )
        # Persistir en el cache MKV (bloque quality). Estos writes al
        # state se hacen condicionados al audit_id: si el usuario ya
        # relanzó otro audit, no pisamos su state con datos viejos.
        persist_mkv_quality_to_cache(mkv_full, result)
        if _mkv_quality_state.get("audit_id") == my_audit_id:
            _mkv_quality_state["result"] = result
            _mkv_quality_state["step"] = "done"
            _mkv_quality_state["global_pct"] = 100
        return result
    except RuntimeError as e:
        msg = str(e)
        # guard: solo pisa state si seguimos siendo el audit actual.
        # finalize_if ya emite "✗ {msg}" al log, no añadimos extra.
        _mkv_quality_state_finalize_if(my_audit_id, msg, step="error")
        status = 499 if "Cancelado" in msg else 500
        raise HTTPException(status_code=status, detail=msg)
    except Exception as e:
        _logger.exception("quality-audit falló inesperadamente sobre %s", mkv_full)
        _mkv_quality_state_finalize_if(my_audit_id, str(e), step="error")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # El hueco de trabajo pesado se libera SIEMPRE y por MI clave: aunque
        # el audit_id haya cambiado, el que ocupó el hueco fui yo.
        workload.liberar(my_audit_id)
        # Mismo guard en el finally: si el audit_id ha cambiado (un nuevo
        # audit ya empezó), NO marcamos active=False — pertenece al nuevo.
        if _mkv_quality_state.get("audit_id") == my_audit_id:
            _mkv_quality_active_proc["proc"] = None
            _mkv_quality_state["active"] = False


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
_MKV_APPLY_STATE_FILE = paths.CONFIG_DIR / "mkv_apply_state.json"


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


def recuperar_apply_interrumpido() -> None:
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


def _mkv_needs_copy_to_output(file_path: str) -> bool:
    """True si el path cae bajo paths.LIBRARY_DIR — read-only, hay que copiar."""
    try:
        Path(file_path).resolve().relative_to(paths.LIBRARY_DIR.resolve())
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


@router.get("/api/mkv/apply/progress", summary="Progreso de la operación apply (copia + edición)")
async def mkv_apply_progress():
    """Polling endpoint para el modal de aplicar cambios. El frontend lo
    consulta cada 1s mientras espera la respuesta del POST /api/mkv/apply."""
    return dict(_mkv_apply_state)


@router.post("/api/mkv/apply/cancel", summary="Cancela la copia en curso de un MKV de Library")
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


@router.post("/api/mkv/apply", summary="Aplica ediciones a un MKV")
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

    if _mkv_apply_state.get("active"):
        raise HTTPException(
            status_code=409,
            detail="Ya hay una copia o edición de MKV en curso. Espera a que "
                   "termine o cancélala.",
        )

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
            dst_path = paths.OUTPUT_DIR_MKV / src_path.name
            if dst_path.exists():
                raise HTTPException(
                    status_code=409,
                    detail=f"Ya existe un MKV con ese nombre en /mnt/output: "
                           f"{src_path.name}. Renómbralo o muévelo antes de continuar."
                )
            # La copia son decenas de GB de lectura y escritura en el NAS.
            workload.exigir_libre()
            _clave_copia = f"apply:{src_path.name}"
            workload.registrar(_clave_copia, workload.TAB_MKV,
                               f"copia de {src_path.name} a /mnt/output")
            paths.OUTPUT_DIR_MKV.mkdir(parents=True, exist_ok=True)
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
                workload.liberar(_clave_copia)
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
