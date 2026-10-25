"""
routers/mux.py — Tab 4 (Combinar MKV): el vídeo de un MKV con el audio, los
subtítulos y los capítulos de otro.

Tres trabajos, con las tres clases de `workload`:

  · **Leer los dos MKV** al crear el proyecto (`POST /api/mux/create`):
    INTERACTIVO y síncrono, ~25 s con el usuario delante y su modal.
  · **Medir la sincronía** (`POST /api/mux/{id}/medir`, y sola al crear):
    INTERACTIVO y fire-and-forget, como los pre-flight de CMv4.0. Son ~20 s
    leyendo ~2 GB; encolarla detrás de un rip de 40 minutos dejaría el
    proyecto parado sin motivo, y por eso no va a la cola. Una a la vez.
  · **Combinar** (`POST /api/mux/{id}/ejecutar`): DIFERIDO, a la cola única,
    con sus tres fases seguidas.

Las decisiones están en `phases/mux_plan.py` (puro) y la E/S en
`phases/mux_pipeline.py`; aquí vive el estado: el JSON del proyecto, la cola,
el progreso para la columna de trabajo, el historial y el arranque.

La dependencia va en un solo sentido: `main` incluye este router y llama a
`recuperar_proyectos_interrumpidos()`; este router no importa `main` ni a
los otros routers.
"""
from i18n import t as tr
import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import historial
import paths
import queue_manager as queue_manager_mod
import relato
import storage
import trabajos
import workload
from models import MuxEjecucion, MuxPista, MuxProject, MuxSincronia
from phases import mux_pipeline, mux_plan, mux_relato, mux_sync
from queue_manager import queue_manager

_logger = logging.getLogger(__name__)

router = APIRouter()

TIPO = queue_manager_mod.TIPO_COMBINAR


# ═══════════════════════════════════════════════════════════════════════════
#  Cerrojos por proyecto
# ═══════════════════════════════════════════════════════════════════════════
#
# El proyecto lo escriben cinco sitios —el PUT, la medida al terminar, la
# hidratación de TMDb, el encolado y el runner— y casi todos lo hacen
# después de un `await`. Sin un cerrojo, la medida que acaba guardaría su
# copia vieja encima del nombre que el usuario acaba de editar: es la carrera
# que costó la tanda de CMv4.0 del 2026-10-17. Todo cambio va
# «cargar → cambiar → guardar» dentro de `_cerrojo(pid)`.
#
# La clave lleva el id del bucle de eventos: un `asyncio.Lock` queda atado al
# bucle en el que se usa, y cada test de `IsolatedAsyncioTestCase` trae el
# suyo (la trampa de `_session_save_locks` de Tab 1).
_cerrojos: dict[tuple[str, int], asyncio.Lock] = {}


def _cerrojo(pid: str) -> asyncio.Lock:
    clave = (pid, id(asyncio.get_running_loop()))
    lock = _cerrojos.get(clave)
    if lock is None:
        lock = _cerrojos[clave] = asyncio.Lock()
    return lock


async def _cargar(pid: str) -> MuxProject | None:
    return await asyncio.to_thread(storage.load_mux_project, pid)


async def _guardar(p: MuxProject) -> None:
    await asyncio.to_thread(storage.save_mux_project, p)


async def _cargar_o_404(pid: str) -> MuxProject:
    p = await _cargar(pid)
    if p is None:
        raise HTTPException(status_code=404, detail=tr('mux.proyecto_no_encontrado'))
    return p


# ═══════════════════════════════════════════════════════════════════════════
#  Rutas
# ═══════════════════════════════════════════════════════════════════════════

def _ruta_segura(ruta: str) -> str:
    """Una ruta del cliente, resuelta y DENTRO de uno de los tres sitios del
    selector (biblioteca, output, descargas). La regla del proyecto:
    `resolve()` + `relative_to()`, nunca comparar cadenas."""
    texto = (ruta or "").strip()
    if not texto:
        raise HTTPException(status_code=400, detail=tr('mux.falta_un_mkv'))
    candidato = Path(texto).resolve()
    for raiz in paths.LIBRARY_ROOTS.values():
        try:
            candidato.relative_to(Path(raiz).resolve())
        except ValueError:
            continue
        if not candidato.is_file():
            raise HTTPException(status_code=400,
                                detail=tr('mux.fichero_no_encontrado', nombre=candidato.name))
        if candidato.suffix.lower() != ".mkv":
            raise HTTPException(status_code=400,
                                detail=tr('mux.solo_mkv', nombre=candidato.name))
        return str(candidato)
    raise HTTPException(status_code=400, detail=tr('mux.ruta_fuera_de_los_sitios'))


# ═══════════════════════════════════════════════════════════════════════════
#  Lo que se sirve
# ═══════════════════════════════════════════════════════════════════════════

def _cartel(p) -> tuple[str, str]:
    """La película y su miniatura, para la columna de trabajo y el historial."""
    tmdb = p.tmdb_info if isinstance(p, MuxProject) else (p or {}).get("tmdb_info")
    nombre = p.nombre_salida if isinstance(p, MuxProject) else (p or {}).get("nombre_salida", "")
    return trabajos.cartel_de(tmdb, nombre or "")


def _plan_vivo(p: MuxProject) -> dict:
    """El plan, con lo que solo sabe la memoria de este proceso: si hay una
    medida en marcha (el JSON dice `midiendo` también tras un reinicio, y ahí
    el arranque ya lo limpió)."""
    return mux_plan.plan(p)


def _relato(p, plan: dict | None) -> dict | None:
    pid = p.id if isinstance(p, MuxProject) else (p or {}).get("id")
    fase = _progreso["fase"] if _progreso.get("id") == pid else ""
    return relato.resolver(workload.TAB_MUX, p, en_cola=queue_manager.esta_en_cola(pid),
                           fase_en_curso=fase, plan=plan)


def _con_progreso_de_medida(datos: dict) -> dict:
    """La medida en marcha cuenta por dónde va (solo está en memoria)."""
    m = _medidas.get(datos.get("id"))
    s = datos.get("sincronia")
    if m and isinstance(s, dict) and s.get("estado") == "midiendo":
        s["progreso"] = list(m.get("progreso") or [0, 0])
    return datos


async def _payload(pid: str) -> dict:
    """El proyecto entero, con lo que se calcula al servir y NO se guarda:
    el plan, el relato y el tamaño estimado (los tres cambian en cuanto el
    usuario toca algo, y guardarlos sería guardar algo viejo)."""
    p = await _cargar_o_404(pid)
    plan = _plan_vivo(p)
    datos = p.model_dump(mode="json")
    datos["plan"] = plan
    datos["relato"] = _relato(p, plan)
    datos["tamano_estimado"] = mux_plan.tamano_estimado(p)
    datos["posicion_en_cola"] = _posicion_en_cola(pid)
    datos["midiendo"] = pid in _medidas
    return _con_progreso_de_medida(datos)


def _posicion_en_cola(pid: str) -> int | None:
    jobs = queue_manager.get_status().get("jobs") or []
    for i, j in enumerate(jobs):
        if j.get("tipo") == TIPO and j.get("clave") == pid:
            return i + 1
    return None


@router.get("/api/mux", summary="Los proyectos de combinación (columna izquierda)")
async def mux_listar():
    def _leer():
        resumenes = storage.list_mux_projects_summary()
        out = []
        for r in resumenes:
            # Copia superficial: los dicts del caché no se mutan.
            d = dict(r)
            plan = d.pop("_plan", None)
            d["plan"] = plan
            d["relato"] = _relato(d, plan)
            d["midiendo"] = d.get("id") in _medidas
            if isinstance(d.get("sincronia"), dict):
                d["sincronia"] = dict(d["sincronia"])
            out.append(_con_progreso_de_medida(d))
        return out
    return {"proyectos": await asyncio.to_thread(_leer)}


@router.get("/api/mux/{project_id}", summary="Un proyecto de combinación")
async def mux_obtener(project_id: str):
    return await _payload(project_id)


# ═══════════════════════════════════════════════════════════════════════════
#  Crear
# ═══════════════════════════════════════════════════════════════════════════

class CrearMux(BaseModel):
    video: str
    resto: str


# El paso de la lectura, para el modal. Lleva el SELLO de su petición (`job`)
# como el progreso de las series: dos creaciones a la vez —dos pestañas del
# navegador— no pueden leer la una el avance de la otra.
_creacion: dict = {"activo": False, "job": "", "fuente": 0, "paso": "", "error": None}


@router.get("/api/mux/create/progress", summary="Paso de la lectura de los dos MKV")
async def mux_crear_progreso():
    return dict(_creacion)


@router.post("/api/mux/create", summary="Lee los dos MKV y crea el proyecto",
             dependencies=[Depends(workload.marca(
                 lambda: tr('mux.wl_lectura_de_dos_mkv'), workload.TAB_MUX))])
async def mux_crear(body: CrearMux, job: str = ""):
    ruta_v = _ruta_segura(body.video)
    ruta_r = _ruta_segura(body.resto)
    if ruta_v == ruta_r:
        raise HTTPException(status_code=400, detail=tr('mux.los_dos_son_el_mismo'))
    sello = job or uuid.uuid4().hex[:12]
    _creacion.update(activo=True, job=sello, fuente=0, paso="", error=None)

    def paso_de(i):
        async def _paso(nombre):
            if _creacion.get("job") == sello:
                _creacion.update(fuente=i, paso=nombre)
        return _paso

    try:
        f0 = await mux_pipeline.leer_fuente(ruta_v, con_dv=True, al_paso=paso_de(0))
        f1 = await mux_pipeline.leer_fuente(ruta_r, con_dv=False, al_paso=paso_de(1))
        provisional = mux_pipeline.proyecto_nuevo("provisional", f0, f1)
        provisional.id = await asyncio.to_thread(storage.make_mux_project_id,
                                                 provisional.nombre_salida)
        titulo, _ = _cartel(provisional)
        workload.detallar_actual(que=tr('mux.wl_lectura_de', p1=titulo or provisional.nombre_salida),
                                 titulo=titulo)
        await _guardar(provisional)
    except RuntimeError as e:
        if _creacion.get("job") == sello:
            _creacion["error"] = str(e)
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        if _creacion.get("job") == sello:
            _creacion["activo"] = False
    pid = provisional.id
    asyncio.create_task(_hidratar_tmdb(pid))
    _lanzar_medida(pid)
    return await _payload(pid)


async def _hidratar_tmdb(pid: str, tmdb_id: int | None = None) -> bool:
    """La ficha de TMDb, best-effort. Busca por el nombre del MKV del audio
    (en un release de un tracker español trae el título en castellano) y si
    no, por el del vídeo. El año completa el nombre de salida si no lo traía
    y el usuario no lo ha editado; el título del nombre es el del fichero,
    como en Tab 1."""
    from services.cmv40_recommend import parse_mkv_filename
    from services.tmdb import fetch_details, is_configured, search_movies
    from i18n import idioma_activo
    if not is_configured():
        return False
    try:
        p = await _cargar(pid)
        if p is None:
            return False
        if tmdb_id is None:
            for f in (p.fuentes[1], p.fuentes[0]):
                titulo, anio = parse_mkv_filename(f.nombre)
                if not titulo:
                    continue
                hallados = await search_movies(titulo, anio, limit=1)
                if hallados:
                    tmdb_id = hallados[0].tmdb_id
                    break
        if tmdb_id is None:
            return False
        ficha = await fetch_details(int(tmdb_id))
        if not ficha:
            return False
        async with _cerrojo(pid):
            p = await _cargar(pid)
            if p is None:
                return False
            p.tmdb_info = {**ficha.model_dump(), "idioma": idioma_activo()}
            if not p.nombre_manual and p.status not in ("queued", "running"):
                titulo, anio = parse_mkv_filename(p.fuentes[1].nombre)
                if not anio and ficha.year:
                    p.nombre_salida = mux_plan.nombre_por_defecto(
                        p, titulo or "", str(ficha.year))
            await _guardar(p)
        return True
    except Exception as e:                              # noqa: BLE001
        _logger.warning("[mux] TMDb falló para %s (no bloquea): %s", pid, e)
        return False


@router.post("/api/mux/{project_id}/tmdb-refresh", summary="Vuelve a buscar la ficha de TMDb")
async def mux_tmdb_refresh(project_id: str, body: dict | None = None):
    await _cargar_o_404(project_id)
    tmdb_id = (body or {}).get("tmdb_id")
    ok = await _hidratar_tmdb(project_id, int(tmdb_id) if tmdb_id else None)
    return {"ok": ok, **(await _payload(project_id))}


# ═══════════════════════════════════════════════════════════════════════════
#  Editar
# ═══════════════════════════════════════════════════════════════════════════

class PistaMuxIn(BaseModel):
    fuente: int
    id: int
    nombre: str = ""
    por_defecto: bool = False
    forzada: bool = False


class AdjuntoMuxIn(BaseModel):
    fuente: int
    id: int


class ActualizarMux(BaseModel):
    """`None` = no tocar."""
    pistas: list[PistaMuxIn] | None = None
    capitulos_de: Literal["video", "resto", "ninguno"] | None = None
    adjuntos: list[AdjuntoMuxIn] | None = None
    titulo: str | None = None
    nombre_salida: str | None = None
    pareja: list[int] | None = None
    acepta_riesgo: bool | None = None
    retraso_manual_ms: int | None = None


def _validar_pistas(p: MuxProject, pistas: list[PistaMuxIn]) -> list[MuxPista]:
    vistas = set()
    out = []
    for q in pistas:
        if q.fuente not in (0, 1):
            raise HTTPException(status_code=400, detail=tr('mux.pista_desconocida',
                                                           fuente=q.fuente, id=q.id))
        orig = next((x for x in p.fuentes[q.fuente].pistas if x.id == q.id), None)
        if orig is None or orig.tipo == "video":
            # El vídeo no es una fila: va entero del MKV del vídeo.
            raise HTTPException(status_code=400, detail=tr('mux.pista_desconocida',
                                                           fuente=q.fuente, id=q.id))
        if (q.fuente, q.id) in vistas:
            raise HTTPException(status_code=400, detail=tr('mux.pista_repetida',
                                                           fuente=q.fuente, id=q.id))
        vistas.add((q.fuente, q.id))
        out.append(MuxPista(fuente=q.fuente, id=q.id, nombre=q.nombre.strip(),
                            por_defecto=q.por_defecto, forzada=q.forzada))
    return out


def _nombre_valido(nombre: str) -> str:
    from phases.phase_e import ruta_de_salida
    n = (nombre or "").strip()
    if n and not n.lower().endswith(".mkv"):
        n += ".mkv"
    try:
        ruta_de_salida(n)
    except ValueError:
        raise HTTPException(status_code=400, detail=tr('mux.nombre_de_salida_no_valido', nombre=n))
    return n


@router.put("/api/mux/{project_id}", summary="Cambia la selección, el nombre o las decisiones")
async def mux_actualizar(project_id: str, body: ActualizarMux):
    volver_a_medir = False
    async with _cerrojo(project_id):
        p = await _cargar_o_404(project_id)
        if p.status in ("queued", "running"):
            raise HTTPException(status_code=409, detail=tr('mux.no_se_edita_en_marcha'))
        if body.pistas is not None:
            p.pistas = _validar_pistas(p, body.pistas)
        if body.capitulos_de is not None:
            p.capitulos_de = body.capitulos_de
        if body.adjuntos is not None:
            validos = {(i, a.id) for i, f in enumerate(p.fuentes) for a in f.adjuntos}
            p.adjuntos = [{"fuente": a.fuente, "id": a.id} for a in body.adjuntos
                          if (a.fuente, a.id) in validos]
        if body.titulo is not None:
            p.titulo = body.titulo.strip()
        if body.nombre_salida is not None:
            nuevo = _nombre_valido(body.nombre_salida)
            if nuevo != p.nombre_salida:
                p.nombre_salida = nuevo
                p.nombre_manual = True
        if body.pareja is not None:
            pareja = list(body.pareja)[:2]
            audio = lambda i: {x.id for x in p.fuentes[i].pistas if x.tipo == "audio"}
            if len(pareja) != 2 or pareja[0] not in audio(0) or pareja[1] not in audio(1):
                raise HTTPException(status_code=400, detail=tr('mux.pareja_no_valida'))
            if pareja != (p.pareja or []):
                p.pareja = pareja
                volver_a_medir = True
        if body.acepta_riesgo is not None:
            p.acepta_riesgo = bool(body.acepta_riesgo)
        if body.retraso_manual_ms is not None:
            p.retraso_manual_ms = int(body.retraso_manual_ms)
        if p.status in ("done", "error") and (body.pistas is not None or body.nombre_salida is not None
                                               or body.capitulos_de is not None):
            # Cambiar lo que se va a combinar vuelve a dejarlo pendiente: el
            # MKV combinado ya no corresponde a lo que hay en pantalla.
            p.status = "pending"
            p.error_message = None
        await _guardar(p)
    if volver_a_medir:
        _lanzar_medida(project_id)
    return await _payload(project_id)


@router.delete("/api/mux/{project_id}", summary="Borra el proyecto (no el MKV combinado)")
async def mux_borrar(project_id: str):
    async with _cerrojo(project_id):
        p = await _cargar_o_404(project_id)
        if p.status == "running":
            raise HTTPException(status_code=409, detail=tr('mux.no_se_borra_en_marcha'))
        if p.status == "queued":
            await queue_manager.cancel(project_id)
        m = _medidas.get(project_id)
        if m:
            m["cancelar"] = True
            _matar(m.get("procs"))
        await asyncio.to_thread(storage.delete_mux_project, project_id)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════
#  La medida de sincronía
# ═══════════════════════════════════════════════════════════════════════════

# `pid → {"task", "procs", "cancelar", "progreso"}`. En memoria: este proceso
# es el único que mide, y el arranque deja en error las que el JSON diga que
# estaban `midiendo`.
_medidas: dict[str, dict] = {}


def _matar(procs) -> None:
    for proc in list(procs or []):
        try:
            if proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass


def _turno_de_medida():
    """Una medida a la vez en toda la app: dos leyendo del mismo vdev RAIDZ
    se estorban más de lo que se adelantan. El semáforo es por bucle de
    eventos (ver `_cerrojo`)."""
    clave = ("__medida__", id(asyncio.get_running_loop()))
    lock = _cerrojos.get(clave)
    if lock is None:
        lock = _cerrojos[clave] = asyncio.Lock()
    return lock


def _lanzar_medida(pid: str) -> bool:
    """Arranca la medida si no hay una de ese proyecto en marcha."""
    viva = _medidas.get(pid)
    if viva and not viva["task"].done():
        return False
    estado = {"procs": [], "cancelar": False, "progreso": [0, len(mux_sync.PUNTOS)]}
    estado["task"] = asyncio.create_task(_medir(pid, estado))
    _medidas[pid] = estado
    return True


@router.post("/api/mux/{project_id}/medir", summary="Mide la sincronía entre los dos MKV")
async def mux_medir(project_id: str):
    p = await _cargar_o_404(project_id)
    if p.status in ("queued", "running"):
        raise HTTPException(status_code=409, detail=tr('mux.no_se_mide_en_marcha'))
    return {"started": _lanzar_medida(project_id)}


@router.post("/api/mux/{project_id}/medir/cancelar", summary="Para la medida de sincronía")
async def mux_medir_cancelar(project_id: str):
    m = _medidas.get(project_id)
    if not m:
        return {"ok": False}
    m["cancelar"] = True
    _matar(m.get("procs"))
    return {"ok": True}


async def _medir(pid: str, estado: dict) -> None:
    clave = f"mux_sincronia:{pid}"
    inicio = datetime.now(timezone.utc)
    async with _cerrojo(pid):
        p = await _cargar(pid)
        if p is None:
            _medidas.pop(pid, None)
            return
        p.sincronia = MuxSincronia(estado="midiendo",
                                   huellas=[f.huella for f in p.fuentes])
        # Una medida nueva invalida la decisión tomada sobre la anterior:
        # aceptar el riesgo de un montaje distinto no vale para otra medida.
        p.acepta_riesgo = False
        await _guardar(p)
    titulo, poster = _cartel(p)
    que = tr('mux.wl_medida_de', p1=titulo or p.nombre_salida)
    workload.registrar(clave, workload.TAB_MUX, que, workload.CLASE_INTERACTIVO,
                       detalle="sincronia_mux", cancelable=True,
                       titulo=titulo, poster=poster)
    resultado = None
    estado_hist, error = historial.ESTADO_HECHO, None
    try:
        async with _turno_de_medida():
            def _avance(hechos, total):
                estado["progreso"] = [hechos, total]
            resultado = await mux_pipeline.medir_sincronia(
                p, al_avanzar=_avance, al_lanzar=estado["procs"].append,
                cancelado=lambda: estado["cancelar"])
        if resultado.estado == "error":
            estado_hist, error = historial.ESTADO_ERROR, resultado.error
    except mux_sync.MedidaCancelada:
        resultado = MuxSincronia(estado="cancelada", huellas=[f.huella for f in p.fuentes])
        estado_hist = historial.ESTADO_CANCELADO
    except asyncio.CancelledError:
        resultado = MuxSincronia(estado="error", error=historial.motivo_interrumpido())
        estado_hist = historial.ESTADO_INTERRUMPIDO
        raise
    except Exception as e:                              # noqa: BLE001
        _logger.exception("[mux] la medida falló")
        resultado = MuxSincronia(estado="error", error=str(e)[:300])
        estado_hist, error = historial.ESTADO_ERROR, str(e)
    finally:
        _matar(estado.get("procs"))
        workload.liberar(clave)
        try:
            async with _cerrojo(pid):
                p2 = await _cargar(pid)
                if p2 is not None and resultado is not None:
                    p2.sincronia = resultado
                    await _guardar(p2)
        finally:
            _medidas.pop(pid, None)
            historial.anotar(id=clave, tab=historial.TAB_MUX,
                             tipo=historial.TIPO_MEDIDA_SINCRONIA,
                             que=tr('mux.hist_medida', p1=titulo or p.nombre_salida),
                             titulo=titulo, poster=poster, inicio=inicio,
                             estado=estado_hist, error=error,
                             ref_log=f"mux:{pid}")


# ═══════════════════════════════════════════════════════════════════════════
#  Combinar
# ═══════════════════════════════════════════════════════════════════════════

def _otro_con_el_mismo_destino(p: MuxProject) -> str | None:
    """Otro proyecto de esta pestaña que va a escribir —o está escribiendo—
    el mismo fichero. Los dos se pisarían el `.mkv.tmp`."""
    propio = (p.nombre_salida or "").strip().casefold()
    for r in storage.list_mux_projects_summary():
        if (r.get("id") != p.id and r.get("status") in ("queued", "running")
                and (r.get("nombre_salida") or "").strip().casefold() == propio):
            return r.get("nombre_salida") or r.get("id")
    return None


@router.post("/api/mux/{project_id}/ejecutar", summary="Encola la combinación")
async def mux_ejecutar(project_id: str):
    async with _cerrojo(project_id):
        p = await _cargar_o_404(project_id)
        if p.status in ("queued", "running"):
            raise HTTPException(status_code=409, detail=tr('mux.ya_esta_en_marcha'))
        if project_id in _medidas:
            raise HTTPException(status_code=409, detail=tr('mux.esperando_a_la_medida'))
        plan = _plan_vivo(p)
        if not plan["puede_combinar"]:
            motivos = ", ".join(tr(f"mux.motivo_corto_{m['id']}") for m in plan["motivos"])
            raise HTTPException(status_code=409,
                                detail=tr('mux.no_se_puede_combinar', motivos=motivos))
        try:
            final = mux_pipeline.ruta_final(p)
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if final.exists():
            raise HTTPException(status_code=409, detail=tr(
                'mux.ya_existe_un_mkv_con_ese_nombre', nombre=p.nombre_salida))
        otro = await asyncio.to_thread(_otro_con_el_mismo_destino, p)
        if otro:
            raise HTTPException(status_code=409, detail=tr(
                'mux.otro_proyecto_escribe_ese_mkv', nombre=p.nombre_salida))
        p.status = "queued"
        p.error_message = None
        await _guardar(p)
    titulo, poster = _cartel(p)
    await queue_manager.encolar(queue_manager_mod.TrabajoEnCola(
        tab="mux", tipo=TIPO, clave=project_id,
        que=tr('mux.cola_combinacion', p1=titulo or p.nombre_salida),
        titulo=titulo, poster=poster,
        datos={"encolado": datetime.now(timezone.utc).isoformat()}))
    return await _payload(project_id)


# El progreso del que corre, para la columna. Es memoria del proceso: el
# adaptador corre en cada poll de `/api/trabajos` y no puede leer el JSON.
_progreso: dict = {"id": "", "fase": "", "pct": None, "paso": "",
                   "desde": 0.0, "fase_desde": 0.0, "chips": []}
_cancelar: dict[str, bool] = {}
_procesos: dict[str, list] = {}


def _progreso_fase(fase: str) -> None:
    _progreso.update(fase=fase, fase_desde=time.monotonic(), pct=None, paso="")


@router.post("/api/mux/{project_id}/cancelar", summary="Para la combinación o la saca de la cola")
async def mux_cancelar(project_id: str):
    """Dos significados con el mismo botón, como en CMv4.0: si espera turno
    se saca de la cola; si corre, se mata mkvmerge (o la medida de la Fase C)
    y la fase limpia su fichero de trabajo."""
    if await queue_manager.cancel(project_id):
        return {"ok": True, "descartada": True}
    if _progreso.get("id") == project_id:
        _cancelar[project_id] = True
        _matar(_procesos.get(project_id))
        return {"ok": True}
    return {"ok": False}


def _registrar_proceso(pid: str):
    def _al_lanzar(proc):
        _procesos.setdefault(pid, []).append(proc)
        # Si la cancelación ya llegó, el proceso muere al nacer: el endpoint
        # solo mata lo que está registrado (la lección de Tab 1).
        if _cancelar.get(pid) and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    return _al_lanzar


async def _combinar(pid: str) -> None:
    """La combinación, con el turno ya concedido."""
    async with _cerrojo(pid):
        p = await _cargar(pid)
        if p is None or p.status != "queued":
            return
        p.status = "running"
        p.output_log = []
        p.error_message = None
        p.last_cancelled_at = None
        await _guardar(p)
    titulo, poster = _cartel(p)
    workload.registrar(pid, workload.TAB_MUX,
                       tr('mux.wl_combinacion_de', p1=titulo or p.nombre_salida))
    inicio = datetime.now(timezone.utc)
    ahora = time.monotonic()
    _progreso.update(id=pid, fase="", pct=None, paso="", desde=ahora, fase_desde=ahora, chips=[])
    _cancelar[pid] = False
    _procesos[pid] = []
    log_lineas: list[str] = []
    tiempos: dict[str, float | None] = {}
    ultimo_guardado = [time.monotonic()]

    async def log(linea: str) -> None:
        if not linea.startswith("Progress:"):
            linea = f"[{datetime.now().strftime('%H:%M:%S')}] {linea}"
        log_lineas.append(linea)
        # El log va con el proyecto, como en Tab 1 (unas pocas decenas de
        # líneas y el progreso de mkvmerge), guardado como mucho cada 2 s.
        if time.monotonic() - ultimo_guardado[0] > 2.0:
            ultimo_guardado[0] = time.monotonic()
            async with _cerrojo(pid):
                q = await _cargar(pid)
                if q is not None:
                    q.output_log = list(log_lineas)
                    await _guardar(q)

    def cancelado() -> bool:
        return bool(_cancelar.get(pid))

    def al_pct(pct: float) -> None:
        _progreso["pct"] = max(0.0, min(100.0, pct))

    plan = mux_plan.plan(p, ignorar_estado=True)
    if plan.get("retimar_video"):
        _progreso["chips"] = [tr('mux.chip_retimado',
                                 fps=mux_sync.texto_de_cadencia(mux_plan._fr(plan["retimar_video"])))]
    estado_final, error, validacion = "error", None, None
    trabajo = None
    capitulos_ns = None
    try:
        await log('[Pipeline] ━━━ ' + tr('mux.log_inicio', nombre=p.nombre_salida) + ' ━━━')
        for fase in mux_pipeline.FASES:
            _progreso_fase(fase)
            t0 = time.monotonic()
            if fase == "comprobar_origenes":
                await mux_pipeline.fase_a_comprobar_origenes(p, plan, log, cancelado)
            elif fase == "combinar":
                trabajo, capitulos_ns = await mux_pipeline.fase_b_combinar(
                    p, plan, log, al_lanzar=_registrar_proceso(pid),
                    cancelado=cancelado, al_progreso=al_pct)
            else:
                validacion = await mux_pipeline.fase_c_comprobar_y_guardar(
                    p, plan, trabajo, log, al_lanzar=_registrar_proceso(pid),
                    cancelado=cancelado, capitulos_ns=capitulos_ns)
            tiempos[fase] = round(time.monotonic() - t0, 1)
        estado_final = "done"
    except mux_pipeline.CombinacionCancelada:
        estado_final = "cancelled"
        await log('[Pipeline] 🛑 Cancelado ' + tr('tab1.por_el_usuario'))
    except mux_pipeline.ValidacionFallida as e:
        validacion = e.validacion
        error = str(e)
        await log('[Pipeline] ' + tr('tab1.error', e=e))
    except Exception as e:                              # noqa: BLE001
        _logger.exception("[mux] la combinación falló")
        error = str(e)
        await log('[Pipeline] ' + tr('tab1.error', e=e))
    finally:
        workload.liberar(pid)
        _matar(_procesos.get(pid))
        if estado_final != "done" and trabajo is not None and validacion is None:
            # Un fallo que no es de la comprobación final no deja un MKV que
            # inspeccionar, solo uno a medias: fuera. El de una comprobación
            # fallida SÍ se queda (la regla de la Fase H de CMv4.0).
            Path(trabajo).unlink(missing_ok=True)
        fin = datetime.now(timezone.utc)
        run_n = None
        async with _cerrojo(pid):
            q = await _cargar(pid)
            if q is not None:
                run_n = len(q.historial) + 1
                q.output_log = list(log_lineas)
                if estado_final == "done":
                    q.status = "done"
                    q.output_mkv_path = str(mux_pipeline.ruta_final(q))
                elif estado_final == "cancelled":
                    q.status = "pending"
                    q.last_cancelled_at = fin
                else:
                    q.status = "error"
                    q.error_message = error
                if validacion is not None:
                    q.validacion = validacion
                q.historial.append(MuxEjecucion(
                    run_number=run_n, started_at=inicio, finished_at=fin,
                    status=estado_final, error_message=error,
                    output_mkv_path=q.output_mkv_path if estado_final == "done" else None,
                    fases=tiempos, output_log=list(log_lineas), validacion=validacion))
                await _guardar(q)
        historial.anotar(id=pid, tab=historial.TAB_MUX, tipo=historial.TIPO_COMBINAR_MKV,
                         que=tr('mux.hist_combinacion', p1=titulo or p.nombre_salida),
                         titulo=titulo, poster=poster, inicio=inicio, fin=fin,
                         estado=estado_final, error=error,
                         # La ejecución concreta, como en Tab 1: un proyecto
                         # combinado dos veces tiene dos logs, y la línea
                         # del historial habla de uno.
                         ref_log=f"mux:{pid}#{run_n}" if run_n else f"mux:{pid}")
        _progreso.update(id="", fase="", pct=None, paso="", chips=[])
        _cancelar.pop(pid, None)
        _procesos.pop(pid, None)


async def _runner_combinar(trabajo) -> None:
    await _combinar(trabajo.clave)


def _descartada(trabajo) -> None:
    """Sacada de la cola antes de empezar: vuelve a pendiente.

    Síncrono (lo llama la cola), así que no puede esperar al cerrojo: lo
    reescribe un hilo con el mismo patrón cargar → cambiar → guardar. Que
    se cruce con un PUT es imposible: el PUT da 409 mientras está en cola."""
    try:
        p = storage.load_mux_project(trabajo.clave)
        if p is not None and p.status == "queued":
            p.status = "pending"
            storage.save_mux_project(p)
    except Exception as e:                              # noqa: BLE001
        _logger.warning("[mux] no se pudo devolver a pendiente %s: %s", trabajo.clave, e)


# ── Para la columna de trabajo ──────────────────────────────────────────────

def _adaptador(trabajo) -> dict | None:
    if _progreso.get("id") != trabajo.clave:
        return None
    fase = _progreso.get("fase") or ""
    ids = list(mux_pipeline.FASES)
    pct = _progreso.get("pct") if fase == "combinar" else None
    segundos = max(0.0, time.monotonic() - (_progreso.get("desde") or 0.0))
    return {
        "fase": fase,
        "fase_label": tr(f'mux.fase_{fase}') if fase in ids else "",
        "paso": _progreso.get("paso") or "",
        "fase_n": ids.index(fase) + 1 if fase in ids else 0,
        "fases_total": len(ids),
        # El % es el de mkvmerge, que es casi todo el trabajo; en las otras
        # dos fases no hay medida y la barra va indeterminada.
        "pct": round(pct) if pct is not None else None,
        "pct_medido": pct is not None,
        "segundos": round(segundos),
        "eta_s": trabajos.eta_por_porcentaje(
            max(0.0, time.monotonic() - (_progreso.get("fase_desde") or 0.0)), pct),
        "eta_fuente": "medido" if pct is not None else None,
        "chips": list(_progreso.get("chips") or []),
        "detalle": "combinar",
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Arranque
# ═══════════════════════════════════════════════════════════════════════════

def recuperar_proyectos_interrumpidos() -> None:
    """Lo que el reinicio del contenedor dejó a medias.

    · `running` → `error`, con el aviso de siempre, y su `.mkv.tmp` fuera: es
      suyo (dos proyectos no pueden escribir el mismo nombre a la vez).
    · `queued` que ya no está en la cola → `pending`.
    · una medida `midiendo` → `error`: el proceso que medía ya no existe.
    """
    try:
        resumenes = storage.list_mux_projects_summary()
    except Exception as e:                              # noqa: BLE001
        _logger.warning("[mux] no se pudo leer los proyectos al arrancar: %s", e)
        return
    en_cola = {j.get("clave") for j in queue_manager.get_status().get("jobs") or []
               if j.get("tipo") == TIPO}
    for r in resumenes:
        estado = r.get("status")
        midiendo = (r.get("sincronia") or {}).get("estado") == "midiendo"
        if estado not in ("running", "queued") and not midiendo:
            continue
        try:
            p = storage.load_mux_project(r["id"])
            if p is None:
                continue
            if p.status == "running":
                p.status = "error"
                p.error_message = historial.motivo_interrumpido()
                try:
                    mux_pipeline.ruta_de_trabajo(mux_pipeline.ruta_final(p)).unlink(missing_ok=True)
                except (RuntimeError, OSError):
                    pass
            elif p.status == "queued" and p.id not in en_cola:
                p.status = "pending"
            if p.sincronia is not None and p.sincronia.estado == "midiendo":
                p.sincronia.estado = "error"
                p.sincronia.error = historial.motivo_interrumpido()
            storage.save_mux_project(p)
        except Exception as e:                          # noqa: BLE001
            _logger.warning("[mux] recuperación de %s fallida: %s", r.get("id"), e)


queue_manager.registrar_runner(TIPO, _runner_combinar)
queue_manager.registrar_descarte(TIPO, _descartada)
trabajos.registrar(TIPO, _adaptador)
relato.registrar(workload.TAB_MUX, mux_relato.resolver)
