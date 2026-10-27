"""
routers/mux.py — Tab 4 (Multiplexar MKV): dos MKV, y de cada uno lo que se
elija —vídeo, audio, subtítulos, capítulos y adjuntos— en un MKV final.

Tres trabajos, con las tres clases de `workload`:

  · **Leer los dos MKV** al crear el proyecto (`POST /api/mux/create`):
    INTERACTIVO y síncrono, ~25 s con el usuario delante y su modal.
  · **Medir la sincronía** (`POST /api/mux/{id}/medir`, y sola al crear):
    INTERACTIVO y fire-and-forget, como los pre-flight de CMv4.0. Son ~20 s
    leyendo ~2 GB; encolarla detrás de un rip de 40 minutos dejaría el
    proyecto parado sin motivo, y por eso no va a la cola. Una a la vez.
  · **Multiplexar** (`POST /api/mux/{id}/ejecutar`): DIFERIDO, a la cola
    única, con sus tres fases seguidas.

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
from models import (ESQUEMA_MUX, MuxCapitulo, MuxEjecucion, MuxPista, MuxProject,
                    MuxSincronia)
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
    from i18n import idioma_activo
    p = await _cargar_o_404(pid)
    plan = _plan_vivo(p)
    datos = p.model_dump(mode="json")
    datos["plan"] = plan
    datos["relato"] = _relato(p, plan)
    datos["tamano_estimado"] = mux_plan.tamano_estimado(p)
    datos["posicion_en_cola"] = _posicion_en_cola(pid)
    datos["midiendo"] = pid in _medidas
    # Lo que la interfaz necesita para pintar las secciones sin replicar
    # reglas: qué patrones tiene cada una y qué idiomas son «los míos» (los
    # mismos que usa el patrón, para que lo resaltado y lo elegido casen).
    datos["patrones"] = {k: list(v) for k, v in mux_plan.PATRONES.items()}
    datos["mis_idiomas"] = sorted(mux_plan.idiomas_del_usuario(
        idioma_activo(), (p.tmdb_info or {}).get("original_language")))
    return _con_progreso_de_medida(datos)


def _posicion_en_cola(pid: str) -> int | None:
    jobs = queue_manager.get_status().get("jobs") or []
    for i, j in enumerate(jobs):
        if j.get("tipo") == TIPO and j.get("clave") == pid:
            return i + 1
    return None


@router.get("/api/mux", summary="Los proyectos de multiplexado (columna izquierda)")
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


@router.get("/api/mux/{project_id}", summary="Un proyecto de multiplexado")
async def mux_obtener(project_id: str):
    return await _payload(project_id)


# ═══════════════════════════════════════════════════════════════════════════
#  Crear
# ═══════════════════════════════════════════════════════════════════════════

class CrearMux(BaseModel):
    a: str
    b: str


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
    ruta_a = _ruta_segura(body.a)
    ruta_b = _ruta_segura(body.b)
    if ruta_a == ruta_b:
        raise HTTPException(status_code=400, detail=tr('mux.los_dos_son_el_mismo'))
    sello = job or uuid.uuid4().hex[:12]
    _creacion.update(activo=True, job=sello, fuente=0, paso="", error=None)

    def paso_de(i):
        async def _paso(nombre):
            if _creacion.get("job") == sello:
                _creacion.update(fuente=i, paso=nombre)
        return _paso

    try:
        f0 = await mux_pipeline.leer_fuente(ruta_a, al_paso=paso_de(0))
        f1 = await mux_pipeline.leer_fuente(ruta_b, al_paso=paso_de(1))
        provisional = mux_pipeline.proyecto_nuevo("provisional", f0, f1)
        # El id y el guardado, juntos y de uno en uno: `make_mux_project_id`
        # mira qué existe en disco, y dos creaciones de la misma película en
        # el mismo segundo sacaban el mismo id y la segunda se guardaba
        # encima de la primera (revisión de Codex del 2026-10-27).
        async with _cerrojo("__crear__"):
            provisional.id = await asyncio.to_thread(storage.make_mux_project_id,
                                                     provisional.nombre_salida)
            await _guardar(provisional)
        titulo, _ = _cartel(provisional)
        workload.detallar_actual(que=tr('mux.wl_lectura_de', p1=titulo or provisional.nombre_salida),
                                 titulo=titulo)
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


def _nombre_automatico(p: MuxProject) -> str:
    """El nombre por defecto, con el título y el año de la ficha de TMDb si la
    hay (en el idioma de la app): sin papeles ya no hay un «MKV del doblaje»
    del que fiarse para el título en castellano."""
    ficha = p.tmdb_info or {}
    anio = ficha.get("year")
    return mux_plan.nombre_por_defecto(p, ficha.get("title") or "",
                                       str(anio) if anio else "")


async def _hidratar_tmdb(pid: str, tmdb_id: int | None = None) -> bool:
    """La ficha de TMDb, best-effort. Busca por el nombre de A y, si no da,
    por el de B. Con la ficha, el nombre de salida pasa a llevar su título y
    su año, salvo que el usuario lo haya editado."""
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
            for f in p.fuentes:
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
            antes = _volcado(p)
            p.tmdb_info = {**ficha.model_dump(), "idioma": idioma_activo()}
            # Por `_tras_cambiar`, como cualquier cambio: con el nombre nuevo,
            # un proyecto terminado ya no describe el MKV que hay en Output
            # (revisión de Codex del 2026-10-27).
            if p.status not in ("queued", "running"):
                _tras_cambiar(p, antes, recalcular_nombre=True)
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


class CapituloMuxIn(BaseModel):
    fuente: int
    edicion: int = 0
    indice: int


class AdjuntoMuxIn(BaseModel):
    fuente: int
    id: int


class ActualizarMux(BaseModel):
    """`None` = no tocar. `esquema` lo manda la interfaz de ahora: una pestaña
    del navegador con el código de antes de multiplexar no lo trae, y sus
    guardados borrarían el vídeo (que antes no iba en la lista) — se le pide
    recargar (revisión de Codex del plan, 2026-10-27)."""
    esquema: int | None = None
    pistas: list[PistaMuxIn] | None = None
    capitulos: list[CapituloMuxIn] | None = None
    adjuntos: list[AdjuntoMuxIn] | None = None
    titulo: str | None = None
    nombre_salida: str | None = None
    pareja: list[int] | None = None
    acepta_riesgo: bool | None = None
    retraso_manual_ms: int | None = None


class PatronMux(BaseModel):
    esquema: int | None = None
    seccion: str
    patron: str


def _exigir_esquema(esquema: int | None) -> None:
    if esquema != ESQUEMA_MUX:
        raise HTTPException(status_code=409, detail=tr('mux.recarga_la_pagina'))


def _validar_pistas(p: MuxProject, pistas: list[PistaMuxIn]) -> list[MuxPista]:
    """La selección de pistas que llega del cliente, o un 400.

    Lo que el PUT no deja guardar, porque produciría un MKV roto o uno que no
    es lo que se ve: una pista que no existe o repetida, vídeo de los dos MKV
    (el vídeo sale de uno solo) y medio vídeo de uno (una BL y su EL en pistas
    aparte van juntas). Lo demás se normaliza: el orden vídeo → audio →
    subtítulos y una sola marca «por defecto» por tipo."""
    vistas = set()
    out = []
    for q in pistas:
        letra = "AB"[q.fuente] if q.fuente in (0, 1) else str(q.fuente)
        orig = (next((x for x in p.fuentes[q.fuente].pistas if x.id == q.id), None)
                if q.fuente in (0, 1) else None)
        if orig is None:
            raise HTTPException(status_code=400, detail=tr('mux.pista_desconocida',
                                                           fuente=letra, id=q.id))
        if (q.fuente, q.id) in vistas:
            raise HTTPException(status_code=400, detail=tr('mux.pista_repetida',
                                                           fuente=letra, id=q.id))
        vistas.add((q.fuente, q.id))
        out.append(MuxPista(fuente=q.fuente, id=q.id, nombre=q.nombre.strip(),
                            por_defecto=q.por_defecto, forzada=q.forzada))
    con_video = {q.fuente for q in out
                 if next(x for x in p.fuentes[q.fuente].pistas if x.id == q.id).tipo == "video"}
    if len(con_video) > 1:
        raise HTTPException(status_code=400, detail=tr('mux.video_de_un_solo_mkv'))
    for f in con_video:
        todos = {x.id for x in p.fuentes[f].pistas if x.tipo == "video"}
        if not todos <= {q.id for q in out if q.fuente == f}:
            raise HTTPException(status_code=400, detail=tr('mux.video_entero'))
    return mux_plan.normalizar_pistas(p, out)


def _validar_capitulos(p: MuxProject, capitulos: list[CapituloMuxIn]) -> list[MuxCapitulo]:
    vistos, out = set(), []
    for c in capitulos:
        clave = (c.fuente, c.edicion, c.indice)
        existe = (c.fuente in (0, 1) and any(
            x.edicion == c.edicion and x.indice == c.indice for x in p.fuentes[c.fuente].capitulos))
        if not existe or clave in vistos:
            raise HTTPException(status_code=400, detail=tr('mux.capitulo_desconocido'))
        vistos.add(clave)
        out.append(MuxCapitulo(fuente=c.fuente, edicion=c.edicion, indice=c.indice))
    return out


def _validar_adjuntos(p: MuxProject, adjuntos: list[AdjuntoMuxIn]) -> list[dict]:
    """Un adjunto que no existe, o repetido, es un 400: se descartaba sin
    decir nada y lo guardado no era lo enviado (revisión de Codex)."""
    validos = {(i, a.id) for i, f in enumerate(p.fuentes) for a in f.adjuntos}
    vistos, out = set(), []
    for a in adjuntos:
        clave = (a.fuente, a.id)
        if clave not in validos or clave in vistos:
            raise HTTPException(status_code=400, detail=tr('mux.adjunto_desconocido'))
        vistos.add(clave)
        out.append({"fuente": a.fuente, "id": a.id})
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


# Lo que cambia el MKV que saldría: tocar cualquiera de estos campos en un
# proyecto terminado lo devuelve a pendiente, o el banner seguiría diciendo
# «multiplexado» sobre un MKV que ya no corresponde a lo que hay en pantalla.
# Antes solo contaban las pistas, el nombre y los capítulos: el título, los
# adjuntos y el retraso manual dejaban el `done` (revisión de Codex).
_CAMBIAN_EL_RESULTADO = ("pistas", "capitulos", "adjuntos", "titulo", "nombre_salida",
                         "retraso_manual_ms")


def _volcado(p: MuxProject) -> dict:
    return {k: p.model_dump(mode="json").get(k) for k in _CAMBIAN_EL_RESULTADO}


def _tras_cambiar(p: MuxProject, antes: dict, *, recalcular_nombre: bool) -> None:
    """Lo que sigue a cualquier cambio de la selección: el nombre automático
    se recalcula (sus tags salen de lo elegido) y un proyecto terminado vuelve
    a pendiente si cambió algo del resultado."""
    if recalcular_nombre and not p.nombre_manual:
        p.nombre_salida = _nombre_automatico(p)
    if p.status in ("done", "error") and _volcado(p) != antes:
        p.status = "pending"
        p.error_message = None


@router.put("/api/mux/{project_id}", summary="Cambia la selección, el nombre o las decisiones")
async def mux_actualizar(project_id: str, body: ActualizarMux):
    _exigir_esquema(body.esquema)
    volver_a_medir = False
    async with _cerrojo(project_id):
        p = await _cargar_o_404(project_id)
        if p.status in ("queued", "running"):
            raise HTTPException(status_code=409, detail=tr('mux.no_se_edita_en_marcha'))
        antes = _volcado(p)
        if body.pistas is not None:
            p.pistas = _validar_pistas(p, body.pistas)
        if body.capitulos is not None:
            p.capitulos = _validar_capitulos(p, body.capitulos)
        if body.adjuntos is not None:
            p.adjuntos = _validar_adjuntos(p, body.adjuntos)
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
                # Con una medida en marcha, la nueva no arrancaría
                # (`_lanzar_medida` dice que ya hay una) y la que termina
                # guardaría su resultado —el de las pistas de antes— junto a
                # la pareja nueva, que es la que usa la comprobación final
                # (revisión de Codex del 2026-10-27).
                viva = _medidas.get(project_id)
                tarea = (viva or {}).get("task")
                if viva and not (tarea is not None and tarea.done()):
                    raise HTTPException(status_code=409,
                                        detail=tr('mux.pareja_con_medida_en_marcha'))
                p.pareja = pareja
                volver_a_medir = True
        if body.acepta_riesgo is not None:
            p.acepta_riesgo = bool(body.acepta_riesgo)
        if body.retraso_manual_ms is not None:
            p.retraso_manual_ms = int(body.retraso_manual_ms)
        _tras_cambiar(p, antes, recalcular_nombre=body.pistas is not None)
        await _guardar(p)
    if volver_a_medir:
        _lanzar_medida(project_id)
    return await _payload(project_id)


@router.post("/api/mux/{project_id}/patron", summary="Aplica un patrón de selección a una sección")
async def mux_patron(project_id: str, body: PatronMux):
    """Los botones de patrón («Todo de A», «Mis idiomas», «Solo forzados»…).

    Se resuelven AQUÍ, sobre lo guardado, y no en el GET de antes: un patrón
    calculado con un estado viejo —el que había antes de un cambio que aún se
    está guardando— mezclaría las dos cosas (revisión de Codex). La interfaz
    espera a tener guardado lo que tocó antes de pedirlo."""
    from i18n import idioma_activo
    _exigir_esquema(body.esquema)
    async with _cerrojo(project_id):
        p = await _cargar_o_404(project_id)
        if p.status in ("queued", "running"):
            raise HTTPException(status_code=409, detail=tr('mux.no_se_edita_en_marcha'))
        antes = _volcado(p)
        idiomas = mux_plan.idiomas_del_usuario(
            idioma_activo(), (p.tmdb_info or {}).get("original_language"))
        try:
            cambio = mux_plan.aplicar_patron(p, body.seccion, body.patron, idiomas)
        except ValueError:
            raise HTTPException(status_code=400, detail=tr('mux.patron_desconocido'))
        for campo, valor in cambio.items():
            setattr(p, campo, valor)
        _tras_cambiar(p, antes, recalcular_nombre="pistas" in cambio)
        await _guardar(p)
    return await _payload(project_id)


@router.delete("/api/mux/{project_id}", summary="Borra el proyecto (no el MKV multiplexado)")
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


def _lo_que_decide(s: MuxSincronia | None):
    """De una medida, lo que cambia el MKV que se multiplexaría."""
    if s is None or s.estado != "medida":
        return None
    return (s.veredicto, s.alfa_num, s.alfa_den, s.beta_ms, s.pista_a, s.pista_b)


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
        previa = p.sincronia
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
                    # Una medida que dice otra cosa cambia el MKV que saldría:
                    # lo terminado deja de describirlo (revisión de Codex del
                    # 2026-10-27). La misma medida otra vez, no.
                    if (p2.status in ("done", "error")
                            and _lo_que_decide(resultado) != _lo_que_decide(previa)):
                        p2.status = "pending"
                        p2.error_message = None
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
#  Multiplexar
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


@router.post("/api/mux/{project_id}/ejecutar", summary="Encola el multiplexado")
async def mux_ejecutar(project_id: str):
    async with _cerrojo(project_id):
        p = await _cargar_o_404(project_id)
        if p.status in ("queued", "running"):
            raise HTTPException(status_code=409, detail=tr('mux.ya_esta_en_marcha'))
        plan = _plan_vivo(p)
        # La medida en marcha solo hace esperar si lo elegido la necesita:
        # todo de A no tiene nada que medir (revisión de Codex).
        if project_id in _medidas and plan["necesita_sincronia"]:
            raise HTTPException(status_code=409, detail=tr('mux.esperando_a_la_medida'))
        if not plan["puede_multiplexar"]:
            motivos = ", ".join(tr(f"mux.motivo_corto_{m['id']}") for m in plan["motivos"])
            raise HTTPException(status_code=409,
                                detail=tr('mux.no_se_puede_multiplexar', motivos=motivos))
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
        que=tr('mux.cola_multiplexado', p1=titulo or p.nombre_salida),
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


@router.post("/api/mux/{project_id}/cancelar", summary="Para el multiplexado o lo saca de la cola")
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


async def _multiplexar(pid: str) -> None:
    """El multiplexado, con el turno ya concedido."""
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
                       tr('mux.wl_multiplexado_de', p1=titulo or p.nombre_salida))
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
    if plan.get("velocidad_video"):
        _progreso["chips"] = [tr('mux.chip_velocidad', fps=mux_sync.texto_de_cadencia(
            mux_plan._fr(plan["velocidad_video"]["a"])))]
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
                trabajo, capitulos_ns = await mux_pipeline.fase_b_multiplexar(
                    p, plan, log, al_lanzar=_registrar_proceso(pid),
                    cancelado=cancelado, al_progreso=al_pct)
            else:
                validacion = await mux_pipeline.fase_c_comprobar_y_guardar(
                    p, plan, trabajo, log, al_lanzar=_registrar_proceso(pid),
                    cancelado=cancelado, capitulos_ns=capitulos_ns)
            tiempos[fase] = round(time.monotonic() - t0, 1)
        estado_final = "done"
    except mux_pipeline.MultiplexadoCancelado:
        estado_final = "cancelled"
        await log('[Pipeline] 🛑 Cancelado ' + tr('tab1.por_el_usuario'))
    except mux_pipeline.ValidacionFallida as e:
        validacion = e.validacion
        error = str(e)
        await log('[Pipeline] ' + tr('tab1.error', e=e))
    except Exception as e:                              # noqa: BLE001
        _logger.exception("[mux] el multiplexado falló")
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
                         que=tr('mux.hist_multiplexado', p1=titulo or p.nombre_salida),
                         titulo=titulo, poster=poster, inicio=inicio, fin=fin,
                         estado=estado_final, error=error,
                         # La ejecución concreta, como en Tab 1: un proyecto
                         # multiplexado dos veces tiene dos logs, y la línea
                         # del historial habla de uno.
                         ref_log=f"mux:{pid}#{run_n}" if run_n else f"mux:{pid}")
        _progreso.update(id="", fase="", pct=None, paso="", chips=[])
        _cancelar.pop(pid, None)
        _procesos.pop(pid, None)


async def _runner_multiplexar(trabajo) -> None:
    await _multiplexar(trabajo.clave)


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


queue_manager.registrar_runner(TIPO, _runner_multiplexar)
queue_manager.registrar_descarte(TIPO, _descartada)
trabajos.registrar(TIPO, _adaptador)
relato.registrar(workload.TAB_MUX, mux_relato.resolver)
