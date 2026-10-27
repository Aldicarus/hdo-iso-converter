"""
phases/mux_pipeline.py — Leer los dos MKV, medir su sincronía y multiplexarlos.

Las tres cosas que hace la pestaña «Multiplexar MKV» con E/S de verdad. Las
decisiones viven en `mux_plan` (qué pistas, qué línea de tiempo, qué argv) y
la medida en `mux_sync`; aquí se lanzan los procesos, se leen sus salidas y
se escribe el log. El estado del proyecto (guardarlo, la cola, el historial)
es del router: estas funciones reciben el proyecto y devuelven lo que pasó.

Lo que se reutiliza, y por qué no se reescribe:
  · la muestra de Dolby Vision de 30 s (`mkv_analyze._run_dovi_on_mkv`): es
    la misma que ve la radiografía de Tab 2, y dos lecturas distintas del
    mismo fichero acabarían diciendo cosas distintas;
  · `phase_e.ruta_de_salida` y el nombre de trabajo `.mkv.tmp`: un nombre
    con `..` no sale de /mnt/output, y un MKV a medias nunca lleva el nombre
    definitivo;
  · `cmv40_pipeline._publicar_sin_pisar`: el paso al nombre definitivo no
    sustituye un fichero que haya aparecido entretanto.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import firma
from i18n import t as tr
from models import (ESQUEMA_MUX, MuxAdjunto, MuxCapituloOrigen, MuxFuente,
                    MuxPistaOrigen, MuxProject, MuxPunto, MuxSincronia, MuxVideo)
from phases import mux_plan, mux_sync

_logger = logging.getLogger(__name__)

MKVMERGE_BIN = "mkvmerge"
MKVEXTRACT_BIN = "mkvextract"

# Tope de las lecturas cortas: un `mkvmerge -J` o un `mkvextract chapters`
# sobre un MKV leen la cabecera y el índice, no la película. Generoso porque
# con el pool ocupado por un rip una lectura de cabecera puede tardar decenas
# de segundos; es para que un proceso colgado no deje esperando para siempre.
_TOPE_LECTURA_S = 180
# Sin una línea de mkvmerge en este tiempo, se da por colgado (como en Tab 1:
# un remux vivo emite progreso cada pocos segundos).
MKVMERGE_INACTIVIDAD_S = 900
VIGILANTE_CADA_S = 30


class MultiplexadoCancelado(Exception):
    """El multiplexado se paró a petición. No es un fallo."""


# ═══════════════════════════════════════════════════════════════════════════
#  Procesos
# ═══════════════════════════════════════════════════════════════════════════

async def _ejecutar(cmd: list[str], tope_s: float, al_lanzar=None):
    """`(rc, stdout, stderr)` de un proceso corto, MATÁNDOLO si se pasa del
    tope o si cancelan la tarea: `wait_for` cancela la espera, no el proceso
    (es el arreglo de `mkv_analyze._comunicar_o_matar`)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    if al_lanzar:
        al_lanzar(proc)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=tope_s)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    return (proc.returncode, out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"))


async def identificar(ruta: str) -> dict:
    """El `mkvmerge -J` de un fichero, ya parseado. Lanza si no se puede."""
    try:
        rc, out, err = await _ejecutar([MKVMERGE_BIN, "-J", ruta], _TOPE_LECTURA_S)
    except asyncio.TimeoutError:
        raise RuntimeError(tr('mux.lectura_agotada', nombre=Path(ruta).name))
    if rc not in (0, 1):
        raise RuntimeError(tr('mux.no_se_pudo_leer', nombre=Path(ruta).name,
                              error=(err or out).strip()[:200]))
    try:
        return json.loads(out)
    except ValueError:
        raise RuntimeError(tr('mux.no_se_pudo_leer', nombre=Path(ruta).name,
                              error=out.strip()[:200]))


# ═══════════════════════════════════════════════════════════════════════════
#  Leer una fuente
# ═══════════════════════════════════════════════════════════════════════════

def _int(v, defecto: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return defecto


def _mediainfo_por_numero(raw: dict | None) -> dict[int, dict]:
    """`{número de pista: track de MediaInfo}`. En un MKV el `ID` de MediaInfo
    es el número de pista del Matroska, que es el `properties.number` de
    mkvmerge: emparejar por eso no depende del orden en que cada uno liste."""
    out = {}
    for t in ((raw or {}).get("media") or {}).get("track") or []:
        if t.get("@type") not in ("Video", "Audio", "Text"):
            continue
        n = _int(str(t.get("ID") or "").split("-")[0], -1)
        if n >= 0:
            out[n] = t
    return out


def _frames_fiables(props: dict) -> int:
    """`NUMBER_OF_FRAMES`, solo si la etiqueta la escribió mkvmerge.

    Es una ETIQUETA, no un recuento: la escribe el programa que muxeó el
    fichero, y otro editor puede dejarla vieja. mkvmerge la regenera al
    multiplexar, así que comparar contra una etiqueta ajena daría por fallido un
    MKV correcto (revisión de Codex del plan). Sin ella, 0: «no se sabe»."""
    app = str(props.get("tag__statistics_writing_app") or "")
    return _int(props.get("tag_number_of_frames")) if app.startswith("mkvmerge") else 0


def pistas_de_j(j: dict, mediainfo: dict | None = None) -> list[MuxPistaOrigen]:
    """Las pistas del `mkvmerge -J`, enriquecidas con MediaInfo si lo hay."""
    por_numero = _mediainfo_por_numero(mediainfo)
    pistas = []
    for t in j.get("tracks") or []:
        if t.get("type") not in ("video", "audio", "subtitles"):
            # Un `buttons` (los menús de un DVD) u otro tipo raro no es una
            # pista que se pueda elegir: se leía como vídeo y acababa en el
            # `--video-tracks` (revisión de Codex del 2026-10-27).
            continue
        p = t.get("properties") or {}
        mi = por_numero.get(_int(p.get("number"), -1)) or {}
        bytes_ = _int(p.get("tag_number_of_bytes")) or _int(mi.get("StreamSize"))
        pistas.append(MuxPistaOrigen(
            id=_int(t.get("id")),
            tipo=t.get("type"),
            codec=t.get("codec") or "",
            codec_id=p.get("codec_id") or "",
            idioma=p.get("language") or "und",
            idioma_ietf=p.get("language_ietf") or "",
            nombre=p.get("track_name") or "",
            por_defecto=bool(p.get("default_track")),
            forzada=bool(p.get("forced_track")),
            comentario=bool(p.get("flag_commentary")),
            sordos=bool(p.get("flag_hearing_impaired")),
            canales=p.get("audio_channels"),
            resolucion=p.get("pixel_dimensions") or "",
            formato_comercial=(mi.get("Format_Commercial_IfAny")
                               or mi.get("Format_Commercial") or ""),
            bytes=bytes_,
            bps=_int(p.get("tag_bps")) or _int(mi.get("BitRate")),
            frames=_frames_fiables(p),
            default_duration_ns=_int(p.get("default_duration")),
        ))
    return pistas


def _resumen_de_video(pistas: list[MuxPistaOrigen], mediainfo: dict | None,
                      dovi) -> MuxVideo:
    videos = [p for p in pistas if p.tipo == "video"]
    if not videos:
        return MuxVideo()
    v = videos[0]
    cad = mux_sync.cadencia_de(v.default_duration_ns)
    mi = next((t for t in ((mediainfo or {}).get("media") or {}).get("track") or []
               if t.get("@type") == "Video"), {})
    return MuxVideo(
        codec=v.codec, resolucion=v.resolucion,
        cadencia_num=cad.numerator if cad else 0,
        cadencia_den=cad.denominator if cad else 1,
        frames=v.frames, pistas=len(videos),
        hdr=mi.get("HDR_Format") or "",
        modo_cadencia=str(mi.get("FrameRate_Mode") or "").upper(),
        dv_profile=getattr(dovi, "profile", 0) or 0,
        dv_el=getattr(dovi, "el_type", "") or "",
        dv_cm=getattr(dovi, "cm_version", "") or "",
    )


async def leer_capitulos(ruta: str) -> list[dict] | None:
    """Los capítulos de primer nivel de un MKV, de su XML (`mkvextract
    chapters`, ver `mux_plan.capitulos_de_xml`): `[]` si no tiene, None si no
    se pudieron leer.

    Del XML y no del formato simple: el simple no dice en qué edición está
    cada capítulo, y la Fase B tiene que volver a encontrar ESE capítulo en
    su XML para copiarlo con lo que lleve dentro."""
    carpeta = Path(tempfile.mkdtemp(prefix="mux_caps_"))
    xml = carpeta / "capitulos.xml"
    try:
        rc, out, err = await _ejecutar([MKVEXTRACT_BIN, ruta, "chapters", str(xml)],
                                       _TOPE_LECTURA_S)
        if rc not in (0, 1):
            return None
        texto = xml.read_text(encoding="utf-8", errors="replace") if xml.exists() else ""
        return mux_plan.capitulos_de_xml(texto) if texto.strip() else []
    except asyncio.TimeoutError:
        return None
    except Exception as e:                              # noqa: BLE001
        _logger.warning("[mux] los capítulos de %s no se pudieron leer: %s", Path(ruta).name, e)
        return None
    finally:
        xml.unlink(missing_ok=True)
        try:
            os.rmdir(carpeta)
        except OSError:
            pass


async def leer_fuente(ruta: str, *, al_paso=None) -> MuxFuente:
    """Lo que la pestaña necesita saber de un MKV.

    `mkvmerge -J` (pistas, ids, idiomas IETF, etiquetas de estadísticas,
    adjuntos), los capítulos, MediaInfo (nombre comercial, HDR, tamaños si el
    MKV no trae las etiquetas) y la muestra de Dolby Vision si trae HEVC: de
    cualquiera de los dos puede acabar saliendo el vídeo. MediaInfo y Dolby
    Vision son enriquecimiento: si fallan, se sigue sin ellos. Lo que no
    puede fallar es el `-J`.

    `al_paso(paso)` se llama antes de cada uno (`pistas`, `capitulos`,
    `mediainfo`, `dolby_vision`), para el modal.
    """
    from storage import compute_mkv_fingerprint
    from phases.mkv_analyze import _run_dovi_on_mkv
    from phases.phase_a import run_mediainfo

    async def paso(nombre):
        if al_paso:
            try:
                await al_paso(nombre)
            except Exception:                           # noqa: BLE001
                pass

    p = Path(ruta)
    if not p.is_file():
        raise RuntimeError(tr('mux.fichero_no_encontrado', nombre=p.name))
    await paso("pistas")
    huella = await asyncio.to_thread(compute_mkv_fingerprint, ruta)
    j = await identificar(ruta)
    contenedor = j.get("container") or {}
    if contenedor.get("type") not in ("Matroska", None) and contenedor.get("type"):
        raise RuntimeError(tr('mux.no_es_un_mkv', nombre=p.name,
                              tipo=contenedor.get("type")))
    props = contenedor.get("properties") or {}

    await paso("capitulos")
    capitulos = await leer_capitulos(ruta)
    if capitulos is None:
        # No es «no tiene capítulos»: con una lista vacía la sección diría
        # que no hay, ni siquiera saldría el aviso de sección vacía, y la
        # Fase C compararía cero contra cero (revisión de Codex del
        # 2026-10-27). Crear el proyecto se puede repetir; esto no se vería.
        raise RuntimeError(tr('mux.no_se_pudieron_leer_los_capitulos', nombre=p.name))

    await paso("mediainfo")
    mediainfo = None
    try:
        mediainfo = (await run_mediainfo(ruta)).raw_json
    except Exception as e:                              # noqa: BLE001
        _logger.warning("[mux] MediaInfo falló sobre %s (no bloquea): %s", p.name, e)

    pistas = pistas_de_j(j, mediainfo)
    dovi = None
    hevc = [x for x in pistas if x.tipo == "video" and "HEVC" in x.codec.upper()]
    if hevc:
        await paso("dolby_vision")
        try:
            dovi = await _run_dovi_on_mkv(ruta, len(hevc))
        except Exception as e:                          # noqa: BLE001
            _logger.warning("[mux] muestra de Dolby Vision fallida en %s: %s", p.name, e)

    return MuxFuente(
        ruta=str(p), nombre=p.name,
        tamano=(huella or {}).get("size_bytes") or p.stat().st_size,
        huella=huella,
        duracion_s=round(_int(props.get("duration")) / 1e9, 3),
        titulo_contenedor=props.get("title") or "",
        video=_resumen_de_video(pistas, mediainfo, dovi),
        pistas=pistas,
        capitulos=[MuxCapituloOrigen(**c) for c in capitulos],
        adjuntos=[MuxAdjunto(id=_int(a.get("id")), nombre=a.get("file_name") or "",
                             mime=a.get("content_type") or "",
                             tamano=_int(a.get("size")))
                  for a in j.get("attachments") or []],
    )


def proyecto_nuevo(id_: str, fuente_a: MuxFuente, fuente_b: MuxFuente) -> MuxProject:
    """El proyecto recién leído, SIN NADA ELEGIDO: se va sección a sección
    eligiendo lo que corresponde, y antes de multiplexar se avisa de lo que
    se haya quedado vacío (decisión del usuario, 2026-10-27)."""
    if not any(p.tipo == "video" for f in (fuente_a, fuente_b) for p in f.pistas):
        raise RuntimeError(tr('mux.ninguno_tiene_video', a=fuente_a.nombre, b=fuente_b.nombre))
    proyecto = MuxProject(id=id_, esquema=ESQUEMA_MUX, fuentes=[fuente_a, fuente_b])
    proyecto.nombre_salida = mux_plan.nombre_por_defecto(proyecto)
    return proyecto


# ═══════════════════════════════════════════════════════════════════════════
#  La medida de la sincronía
# ═══════════════════════════════════════════════════════════════════════════

def _audio_como_dicts(fuente: MuxFuente) -> list[dict]:
    return [{"id": p.id, "idioma": p.idioma, "nombre": p.nombre,
             "por_defecto": p.por_defecto, "canales": p.canales,
             "comentario": p.comentario}
            for p in fuente.pistas if p.tipo == "audio"]


def pareja_de(proyecto: MuxProject) -> tuple[int, int] | None:
    """La pareja con la que se mide: la elegida si sigue existiendo, o la de
    `mux_sync.pareja_de_referencia`."""
    a = _audio_como_dicts(proyecto.fuentes[0])
    b = _audio_como_dicts(proyecto.fuentes[1])
    if proyecto.pareja and len(proyecto.pareja) == 2:
        ia, ib = proyecto.pareja
        if any(x["id"] == ia for x in a) and any(x["id"] == ib for x in b):
            return ia, ib
    return mux_sync.pareja_de_referencia(a, b)


duracion_de_video = mux_plan.duracion_de_video


async def medir_sincronia(proyecto: MuxProject, *, al_avanzar=None,
                          al_lanzar=None, cancelado=None) -> MuxSincronia:
    """Mide la sincronía entre las dos fuentes del proyecto.

    No lanza por un fallo de medida: lo devuelve como `estado="error"`, que
    la interfaz enseña con su motivo. Sí lanza `MedidaCancelada`.
    """
    huellas = [f.huella for f in proyecto.fuentes]
    pareja = pareja_de(proyecto)
    if pareja is None:
        return MuxSincronia(estado="medida", veredicto=mux_sync.NO_CONCLUYENTE,
                            error="sin_pareja", huellas=huellas,
                            medido_en=datetime.now(timezone.utc))
    fa, fb = proyecto.fuentes
    cad_a = mux_plan.cadencia(fa)
    cad_b = mux_plan.cadencia(fb)
    alfa = (cad_a / cad_b) if cad_a and cad_b else Fraction(1)
    comun = min(duracion_de_video(fa), duracion_de_video(fb) / float(alfa))
    try:
        r = await mux_sync.medir(
            fa.ruta, _posicion_de_audio(fa, pareja[0]),
            fb.ruta, _posicion_de_audio(fb, pareja[1]), duracion_a_s=comun,
            cadencia_a=cad_a, cadencia_b=cad_b, al_avanzar=al_avanzar,
            al_lanzar=al_lanzar, cancelado=cancelado)
    except mux_sync.MedidaCancelada:
        raise
    except Exception as e:                              # noqa: BLE001
        _logger.exception("[mux] la medida de sincronía falló")
        return MuxSincronia(estado="error", pista_a=pareja[0], pista_b=pareja[1],
                            error=str(e)[:300], huellas=huellas,
                            medido_en=datetime.now(timezone.utc))
    return MuxSincronia(
        estado="medida", pista_a=pareja[0], pista_b=pareja[1],
        veredicto=r["veredicto"], alfa_num=r["alfa_num"], alfa_den=r["alfa_den"],
        beta_ms=r["beta_ms"], dispersion_ms=r["dispersion_ms"],
        pendiente_ppm=r["pendiente_ppm"], salto_entre=r["salto_entre"],
        n_validos=r["n_validos"], n_puntos=r["n_puntos"],
        puntos=[MuxPunto(**{k: v for k, v in p.items() if k in MuxPunto.model_fields})
                for p in r["puntos"]],
        huellas=huellas, medido_en=datetime.now(timezone.utc))


# ═══════════════════════════════════════════════════════════════════════════
#  El multiplexado: fases A, B y C
# ═══════════════════════════════════════════════════════════════════════════

# Los ids NO cambiaron con el nombre de la pestaña: `combinar` es la clave de
# la fase B en los `MuxEjecucion.fases` ya guardados y en la interfaz.
FASES = ("comprobar_origenes", "combinar", "comprobar_y_guardar")


def ruta_final(proyecto: MuxProject) -> Path:
    """El MKV final bajo /mnt/output, o `RuntimeError` si el nombre sale de
    ahí (la regla de `phase_e.ruta_de_salida`)."""
    from phases.phase_e import ruta_de_salida
    try:
        return ruta_de_salida(proyecto.nombre_salida)
    except ValueError:
        raise RuntimeError(tr('mux.nombre_de_salida_no_valido',
                              nombre=proyecto.nombre_salida)) from None


def ruta_de_trabajo(final: Path) -> Path:
    """Donde se escribe hasta que está comprobado: `<final>.tmp`, en el mismo
    directorio para que el paso al definitivo sea atómico. Es el nombre de
    trabajo de Tab 1 (`phase_e._nombre_de_trabajo`), que el panel de Limpieza
    ya reconoce como remux incompleto."""
    from phases.phase_e import _nombre_de_trabajo
    return Path(_nombre_de_trabajo(str(final)))


async def _log(log, linea: str) -> None:
    if log:
        await log(linea)


def _comprobar_cancelado(cancelado) -> None:
    if cancelado and cancelado():
        raise MultiplexadoCancelado()


async def fase_a_comprobar_origenes(proyecto: MuxProject, plan_: dict, log,
                                    cancelado=None) -> None:
    """Lo que tiene que seguir siendo cierto con el turno ya concedido: la
    cola puede despachar cuarenta minutos después de encolar."""
    from storage import _fingerprint_content_match, compute_mkv_fingerprint
    await _log(log, '[Fase A] ' + tr('mux.log_comprobando_origenes'))
    for i, f in enumerate(proyecto.fuentes):
        _comprobar_cancelado(cancelado)
        if not Path(f.ruta).is_file():
            raise RuntimeError(tr('mux.fichero_no_encontrado', nombre=f.nombre))
        ahora = await asyncio.to_thread(compute_mkv_fingerprint, f.ruta)
        if not (ahora and f.huella and _fingerprint_content_match(f.huella, ahora)):
            raise RuntimeError(tr('mux.el_mkv_cambio', nombre=f.nombre))
    if not plan_["puede_multiplexar"]:
        ids = ", ".join(m["id"] for m in plan_["motivos"])
        raise RuntimeError(tr('mux.no_se_puede_multiplexar_motivos', motivos=ids))
    final = ruta_final(proyecto)
    if final.exists():
        raise RuntimeError(tr('mux.ya_existe_un_mkv_con_ese_nombre',
                              nombre=proyecto.nombre_salida))
    necesarios = mux_plan.tamano_estimado(proyecto)
    if necesarios:
        final.parent.mkdir(parents=True, exist_ok=True)
        libre = (await asyncio.to_thread(shutil.disk_usage, str(final.parent))).free
        if libre < necesarios * 1.05:
            raise RuntimeError(tr('mux.sin_espacio',
                                  libre=f"{libre / 1e9:.1f}",
                                  necesarios=f"{necesarios * 1.05 / 1e9:.1f}"))
    await _log(log, '[Fase A] ' + tr('mux.log_origenes_ok'))


def duracion_final(proyecto: MuxProject, plan_: dict) -> float | None:
    """Lo que durará el vídeo del MKV final (ver `mux_plan.duracion_final`)."""
    return mux_plan.duracion_final(proyecto, plan_)


def capitulos_esperados(proyecto: MuxProject, plan_: dict) -> list[int]:
    return mux_plan.capitulos_esperados(proyecto, plan_)


async def _capitulos_xml(proyecto: MuxProject, plan_: dict,
                         carpeta: Path) -> tuple[str | None, list[int]]:
    """El XML de capítulos compuesto con lo elegido (o None si no hace falta:
    sin capítulos, o todos los de una fuente que va tal cual) y los inicios
    que tiene que traer el MKV final."""
    lista = [x for x in plan_["capitulos"]["lista"] if x["queda"] != "fuera"]
    if not lista or mux_plan.capitulos_nativos(proyecto, plan_) is not None:
        return None, capitulos_esperados(proyecto, plan_)
    xmls: dict[int, str] = {}
    for f in sorted({x["fuente"] for x in lista}):
        crudo = carpeta / f"_caps_origen_{proyecto.id}_{f}.xml"
        try:
            rc, out, err = await _ejecutar(
                [MKVEXTRACT_BIN, proyecto.fuentes[f].ruta, "chapters", str(crudo)],
                _TOPE_LECTURA_S)
            if rc not in (0, 1) or not crudo.exists():
                raise RuntimeError(tr('mux.no_se_pudieron_extraer_los_capitulos',
                                      error=(err or out).strip()[:200]))
            xmls[f] = crudo.read_text(encoding="utf-8", errors="replace")
        finally:
            crudo.unlink(missing_ok=True)
    try:
        xml, esperados = mux_plan.componer_capitulos_xml(
            xmls, proyecto, plan_, mux_plan.duracion_final(proyecto, plan_))
    except RuntimeError:
        raise RuntimeError(tr('mux.los_capitulos_cambiaron')) from None
    destino = carpeta / f"_caps_{proyecto.id}.xml"
    destino.write_text(xml, encoding="utf-8")
    return str(destino), esperados


async def fase_b_multiplexar(proyecto: MuxProject, plan_: dict, log, *,
                          al_lanzar=None, cancelado=None,
                          al_progreso=None) -> tuple[Path, list[int]]:
    """mkvmerge al nombre de trabajo. Devuelve su ruta y los inicios (ns) de
    los capítulos que tiene que traer, para la Fase C.

    Cualquier salida que no sea un éxito borra el fichero de trabajo: una
    fase que falla no puede dejar 60 GB a medias con un nombre que parezca un
    MKV.
    """
    final = ruta_final(proyecto)
    trabajo = ruta_de_trabajo(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    trabajo.unlink(missing_ok=True)       # el resto de un intento que murió
    xml, caps_esperados = await _capitulos_xml(proyecto, plan_, final.parent)
    cmd = mux_plan.argv(proyecto, plan_, str(trabajo), xml)
    await _log(log, '[Fase B] 📋 Plan: ' + plan_texto_para_el_log(proyecto, plan_))
    await _log(log, '$ ' + " ".join(_citar(a) for a in cmd))
    _comprobar_cancelado(cancelado)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    if al_lanzar:
        al_lanzar(proc)
    ultima = time.monotonic()
    colgado = False

    async def _vigilante():
        nonlocal colgado
        while proc.returncode is None:
            await asyncio.sleep(VIGILANTE_CADA_S)
            if time.monotonic() - ultima > MKVMERGE_INACTIVIDAD_S:
                colgado = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return

    vigilante = asyncio.create_task(_vigilante())
    avisos = 0
    try:
        async for bruto in proc.stdout:
            ultima = time.monotonic()
            linea = bruto.decode("utf-8", errors="replace").rstrip()
            if not linea:
                continue
            if linea.startswith("#GUI#progress "):
                try:
                    pct = float(linea.removeprefix("#GUI#progress ").rstrip("%"))
                except ValueError:
                    continue
                if al_progreso:
                    al_progreso(pct)
                await _log(log, f"Progress: {int(pct)}%")
                continue
            if linea.startswith("#GUI#"):
                continue
            if linea.lower().startswith("warning"):
                avisos += 1
            await _log(log, linea)
        await proc.wait()
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        trabajo.unlink(missing_ok=True)
        raise
    finally:
        vigilante.cancel()
        try:
            await vigilante
        except asyncio.CancelledError:
            pass
        if xml:
            Path(xml).unlink(missing_ok=True)

    if colgado or proc.returncode not in (0, 1) or (cancelado and cancelado()):
        trabajo.unlink(missing_ok=True)
        if cancelado and cancelado():
            raise MultiplexadoCancelado()
        if colgado:
            raise RuntimeError(tr('mux.mkvmerge_colgado',
                                  minutos=MKVMERGE_INACTIVIDAD_S // 60))
        raise RuntimeError(tr('mux.mkvmerge_fallo', rc=proc.returncode))
    if not trabajo.exists():
        raise RuntimeError(tr('mux.mkvmerge_sin_salida'))
    if avisos:
        await _log(log, '[Fase B] ' + tr('mux.log_mkvmerge_con_avisos_uno' if avisos == 1
                                         else 'mux.log_mkvmerge_con_avisos_varios', n=avisos))
    await _log(log, '[Fase B] ' + tr('mux.log_multiplexado',
                                     gb=f"{trabajo.stat().st_size / 1e9:.1f}"))
    return trabajo, caps_esperados


def _citar(a: str) -> str:
    return a if re.fullmatch(r"[\w./:,=+-]+", a) else "'" + a.replace("'", "'\\''") + "'"


def plan_texto_para_el_log(proyecto: MuxProject, plan_: dict) -> str:
    """El plan en una línea, para el log: qué línea de tiempo manda y qué se
    mueve. Es lo que permite leer, meses después, por qué el MKV salió así."""
    trozos = []
    vel = plan_.get("velocidad_video")
    if vel:
        trozos.append(tr('mux.log_plan_velocidad', letra="AB"[plan_["video_de"]],
                         otra="AB"[1 - plan_["video_de"]],
                         de=mux_sync.texto_de_cadencia(mux_plan._fr(vel["de"])),
                         a=mux_sync.texto_de_cadencia(mux_plan._fr(vel["a"]))))
    movidas = len(plan_["sync"])
    if movidas:
        trozos.append(tr('mux.log_plan_movidas_uno' if movidas == 1
                         else 'mux.log_plan_movidas_varios', n=movidas))
    if not trozos:
        trozos.append(tr('mux.log_plan_tal_cual'))
    return " · ".join(trozos)


def _salida_de_pista(proyecto: MuxProject, fuente: int, tid: int) -> int | None:
    """El puesto (desde 0) que tendrá en el MKV final una pista elegida: el de
    la lista, que ya trae el vídeo delante. Sumarle los vídeos aparte lo
    contaba dos veces (revisión de Codex)."""
    for i, p in enumerate(mux_plan._elegidas(proyecto)):
        if p.fuente == fuente and p.id == tid:
            return i
    return None


async def fase_c_comprobar_y_guardar(proyecto: MuxProject, plan_: dict,
                                     trabajo: Path, log, *, al_lanzar=None,
                                     cancelado=None,
                                     capitulos_ns: list[int] | None = None) -> dict:
    """Comprueba el MKV de trabajo contra el plan y, si está bien, le da su
    nombre definitivo y lo firma. Devuelve la validación.

    Si algo no cuadra, se lanza con el `.mkv.tmp` en su sitio para que se
    pueda inspeccionar: es la regla de la Fase H.
    """
    from phases.cmv40_pipeline import _publicar_sin_pisar
    from phases.mkv_analyze import _run_dovi_on_mkv

    checks: list[dict] = []
    await _log(log, '[Fase C] ' + tr('mux.log_comprobando_mkv'))
    j = await identificar(str(trabajo))
    checks += mux_plan.comprobar_pistas(mux_plan.pistas_esperadas(proyecto, plan_), j)

    # El vídeo del MKV final dura lo que dicen sus frames a la velocidad del
    # plan. Es lo que demuestra que su velocidad se ajustó de verdad: la
    # comprobación de audio de abajo mide el audio contra el ORIGINAL, y sin
    # esto un vídeo sin ajustar con el audio bien puesto pasaría.
    checks += _comprobar_duracion_del_video(proyecto, plan_, j)

    checks += await _comprobar_capitulos(
        trabajo, capitulos_ns if capitulos_ns is not None
        else capitulos_esperados(proyecto, plan_))
    adjuntos = len(j.get("attachments") or [])
    pedidos = len(mux_plan.adjuntos_validos(proyecto))
    checks.append({"id": "adjuntos", "ok": adjuntos == pedidos,
                   "esperado": pedidos, "real": adjuntos})

    v = plan_.get("video_de")
    fuente_v = proyecto.fuentes[v].video if v is not None else None
    if fuente_v is not None and fuente_v.dv_profile:
        _comprobar_cancelado(cancelado)
        claves = mux_plan._por_clave(proyecto)
        hevc = sum(1 for p in mux_plan._elegidas(proyecto, "video")
                   if "HEVC" in (claves[(p.fuente, p.id)].codec or "").upper())
        dovi = None
        try:
            dovi = await _run_dovi_on_mkv(str(trabajo), hevc)
        except Exception as e:                          # noqa: BLE001
            _logger.warning("[mux] muestra DV del MKV final fallida: %s", e)
        real = (f"P{dovi.profile} {dovi.el_type} {dovi.cm_version}".strip()
                if dovi else "")
        esperado = f"P{fuente_v.dv_profile} {fuente_v.dv_el} {fuente_v.dv_cm}".strip()
        checks.append({"id": "dolby_vision", "ok": bool(dovi) and real == esperado,
                       "esperado": esperado, "real": real or None})

    checks += await _comprobar_sincronia_final(proyecto, plan_, trabajo,
                                               al_lanzar, cancelado)

    fallos = [c for c in checks if c.get("ok") is False]
    validacion = {"checks": checks, "fallos": len(fallos),
                  "comprobado_en": datetime.now(timezone.utc).isoformat()}
    for c in checks:
        if c.get("ok") is False:
            await _log(log, '[Fase C] ✗ ' + tr('mux.log_check_fallo', id=c["id"],
                                               pista=c.get("pista", "—"),
                                               esperado=c.get("esperado"),
                                               real=c.get("real")))
    if fallos:
        raise ValidacionFallida(validacion)

    # Lo último antes de publicar: con todo de un MKV sin Dolby Vision no se
    # pasa por la muestra ni por la sincronía, que eran los únicos sitios
    # donde se miraba, y una cancelación aceptada acababa en `done` (revisión
    # de Codex del 2026-10-27).
    _comprobar_cancelado(cancelado)
    final = ruta_final(proyecto)
    if not await asyncio.to_thread(_publicar_sin_pisar, trabajo, final,
                                   proyecto.nombre_salida):
        raise RuntimeError(tr('mux.no_se_pudo_publicar', nombre=proyecto.nombre_salida))
    await firma.firmar(str(final))
    await _log(log, '[Fase C] 🎯 Resultado' + tr('mux.log_resultado',
                                               nombre=proyecto.nombre_salida))
    return validacion


async def _comprobar_capitulos(mkv: Path, esperados_ns: list[int]) -> list[dict]:
    """Los capítulos del MKV final contra los esperados: cuántos y dónde
    empieza cada uno, al milisegundo (contar solo cuántos dejaba pasar un
    mapeo con el signo cambiado; revisión de Codex del plan). Se leen del XML,
    como al crear el proyecto: los de primer nivel, de todas las ediciones."""
    reales = await leer_capitulos(str(mkv))
    if reales is None:
        return [{"id": "capitulos", "ok": None, "aviso": True,
                 "esperado": len(esperados_ns), "real": None}]
    reales_ns = sorted(c["inicio_ns"] for c in reales)
    esperados_ns = sorted(esperados_ns)
    if len(reales_ns) != len(esperados_ns):
        return [{"id": "capitulos", "ok": False,
                 "esperado": len(esperados_ns), "real": len(reales_ns)}]
    # Un milisegundo y medio de tolerancia: la que tenía con el formato simple.
    peor = max((abs(r - e) for r, e in zip(reales_ns, esperados_ns)), default=0)
    return [{"id": "capitulos", "ok": peor <= 1_500_000,
             "esperado": len(esperados_ns), "real": len(reales_ns),
             "desvio_ms": round(peor / 1e6, 1)}]


class ValidacionFallida(RuntimeError):
    """El MKV multiplexado no es lo que el plan pedía. Lleva la validación."""

    def __init__(self, validacion: dict):
        self.validacion = validacion
        super().__init__(tr('mux.validacion_fallida_uno' if validacion["fallos"] == 1
                            else 'mux.validacion_fallida_varios', n=validacion["fallos"]))


def _segundos_de_tag(valor) -> float | None:
    """'01:41:46.750000000' → segundos."""
    try:
        h, m, s = str(valor).split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (TypeError, ValueError):
        return None


def _comprobar_duracion_del_video(proyecto: MuxProject, plan_: dict, j: dict) -> list[dict]:
    v = plan_.get("video_de")
    if v is None:
        return []
    fuente = proyecto.fuentes[v]
    cad = (mux_plan._fr((plan_.get("velocidad_video") or {}).get("a"))
           or mux_plan.cadencia(fuente))
    if not (cad and fuente.video.frames):
        return []
    video = next((t for t in j.get("tracks") or [] if t.get("type") == "video"), None)
    dur = _segundos_de_tag(((video or {}).get("properties") or {}).get("tag_duration"))
    if dur is None:
        return []
    esperada = fuente.video.frames / float(cad)
    # Un frame de margen: la etiqueta cuenta hasta el final del último frame.
    margen = 1.5 / float(cad)
    return [{"id": "duracion_del_video", "ok": abs(dur - esperada) <= margen,
             "esperado": round(esperada, 3), "real": round(dur, 3)}]


async def _comprobar_sincronia_final(proyecto: MuxProject, plan_: dict,
                                     trabajo: Path, al_lanzar, cancelado) -> list[dict]:
    """Cada pista de audio que viene del MKV que NO da el vídeo, ya en el MKV
    final, contra la de referencia del que sí lo da (de su fichero ORIGINAL).

    Con el vídeo de A es lo de siempre —el audio de B contra el de A—; con el
    vídeo de B, al revés. Si se ajustó la velocidad del vídeo, el contenido
    del segundo T del original está en `f_vo·T` del final, y eso es lo que se
    espera (`f_vo` = α con el vídeo de A, 1/α con el de B).

    La pista de la pareja que se midió (o la primera, si esa no va) es la
    comprobación de que mkvmerge hizo lo que el plan pedía —un signo cambiado
    o un factor mal escrito se ven aquí— y es la única que puede tumbar el
    trabajo. Las demás son AVISOS: una que no cuadre viene desfasada dentro
    de su propio release (revisión de Codex del plan), y eso no lo arregla
    multiplexar, pero hay que decirlo. Con el riesgo aceptado, tampoco la de
    referencia tumba nada: el usuario ya decidió seguir sabiendo que no
    cuadraba.

    Cada comprobación guarda contra QUÉ pista se midió y de qué fuente
    (`contra`, `contra_fuente`): la de una validación vieja, sin la fuente,
    era siempre de A (revisión de Codex).
    """
    v = plan_.get("video_de")
    pareja = pareja_de(proyecto)
    if v is None or pareja is None:
        return []
    o = 1 - v
    claves = mux_plan._por_clave(proyecto)
    audio_o = [p for p in mux_plan._elegidas(proyecto, "audio") if p.fuente == o]
    if not audio_o:
        return []
    ref_v, ref_o = pareja[v], pareja[o]
    ref = next((p for p in audio_o if p.id == ref_o), audio_o[0])
    alfa = mux_plan._fr(plan_["alfa"]) or Fraction(1)
    esperado = (alfa if v == 0 else 1 / alfa) if plan_.get("velocidad_video") else Fraction(1)
    riesgo = bool(plan_.get("motivos"))
    video = proyecto.fuentes[v]
    checks = []
    for p in [ref] + [q for q in audio_o if q is not ref]:
        _comprobar_cancelado(cancelado)
        n = _salida_de_audio(proyecto, p)
        if n is None:
            continue
        # Contra qué pista del MKV del vídeo se compara. Medido en el NAS con
        # Toy Story 5 (2026-10-25), y no es lo que parecía:
        #
        # - la de REFERENCIA primero, y con los cuatro puntos: es la que la
        #   medida alineó con el vídeo. Un doblaje contra ella correla poco
        #   —solo la música y los efectos: r ≈ 0,1-0,2—, así que con dos puntos
        #   no concluía nunca y los tres doblajes salían «no se pudo medir»;
        #   con cuatro, los tres concluyen a ~6 ms, lo mismo que la referencia;
        # - las demás pistas del MKV del vídeo NO valen como primera opción:
        #   cada una trae su propio desfase dentro de su fichero (el DD 5.1
        #   inglés del disco va ~50 ms del TrueHD) y la etiqueta no garantiza
        #   el doblaje (el `es` del disco no correla con el castellano del
        #   WEB-DL: es otro doblaje). Solo si la de referencia no concluye,
        #   hasta dos del mismo idioma, la de la misma etiqueta primero.
        if p.id == ref_o:
            contra, mismas = [ref_v], []
        else:
            mismas = [q.id for q in _del_mismo_idioma(video, claves.get((o, p.id)))
                      if q.id != ref_v][:2]
            contra = [ref_v] + mismas
        r = {"ok": None, "puntos": [], "residuo_max_ms": None}
        usada = None
        for id_v in contra:
            v_n = _posicion_de_audio(video, id_v)
            if v_n is None:
                continue
            try:
                r = await mux_sync.comprobar(
                    video.ruta, v_n, str(trabajo), n,
                    duracion_s=duracion_de_video(video), alfa=esperado,
                    al_lanzar=al_lanzar)
            except Exception as e:                          # noqa: BLE001
                _logger.warning("[mux] comprobación de sincronía final fallida: %s", e)
                r = {"ok": None, "puntos": [], "residuo_max_ms": None}
            usada = id_v
            # La de la medida es una sola comparación y estricta. En las demás,
            # unos puntos válidos que no coinciden entre sí son una medida que
            # no concluye: con una correlación tan floja, un pico casual da un
            # residuo de un segundo y se leería como «desfasada».
            if p.id != ref_o and r["ok"] is not None and not _puntos_coinciden(r):
                r = dict(r, ok=None, residuo_max_ms=None)
            if r["ok"] is not None:
                break
            _comprobar_cancelado(cancelado)
        aviso = (p is not ref) or riesgo
        check = {
            "id": "sincronia" if p is ref else "sincronia_pista",
            "pista": (_salida_de_pista(proyecto, o, p.id) or 0) + 1,
            # Lo que es aviso no cuenta como fallo: `ok` False solo si tumba.
            "ok": r["ok"] if not (aviso and r["ok"] is False) else None,
            "aviso": bool(aviso and r["ok"] is False) or r["ok"] is None,
            "esperado": f"≤ {mux_sync.RESIDUO_FINAL_MAX_MS:g} ms",
            "real": (f"{r['residuo_max_ms']} ms"
                     if r.get("residuo_max_ms") is not None else None),
            "contra": usada,
            "contra_fuente": v,
            "puntos": r["puntos"]}
        if r["ok"] is None and p.id != ref_o and not mismas:
            # Sin medida, y sin otra pista de su idioma en el MKV del vídeo con
            # la que compararla: decirlo, que «no se pudo medir» a secas parece
            # un fallo del MKV.
            check["motivo"] = "sin_mismo_doblaje"
        checks.append(check)
    return checks


def _puntos_coinciden(r: dict) -> bool:
    """¿Los puntos válidos de una comprobación dicen lo mismo? Sus residuos
    caben en la tolerancia de la comprobación entre el mayor y el menor. Un
    desfase de verdad desplaza todos los puntos a la vez; un pico casual, uno."""
    residuos = [q["residuo_ms"] for q in r.get("puntos") or []
                if q.get("valido") and q.get("residuo_ms") is not None]
    return len(residuos) >= 2 and max(residuos) - min(residuos) <= mux_sync.RESIDUO_FINAL_MAX_MS


def _del_mismo_idioma(fuente: MuxFuente, o: MuxPistaOrigen | None) -> list[MuxPistaOrigen]:
    """Las pistas de audio de `fuente` en el idioma de `o`, de la que más se
    le parece a la que menos: primero la de la MISMA etiqueta IETF —el
    castellano y el latino comparten idioma y son dos doblajes—, después la
    principal. Sin comentarios: el del director no es el doblaje. Las
    etiquetas no son de fiar del todo («fr» en la canadiense, «fr-FR» en la
    francesa), y por eso esto ordena y no descarta: la medida dice si cuadra."""
    if o is None:
        return []
    idioma = (o.idioma or "").lower()
    if not idioma or idioma == "und":
        return []
    ietf = (o.idioma_ietf or "").lower()
    candidatas = [q for q in fuente.pistas
                  if q.tipo == "audio" and not q.comentario and (q.idioma or "").lower() == idioma]
    return sorted(candidatas, key=lambda q: (bool(ietf) and (q.idioma_ietf or "").lower() != ietf,
                                             not q.por_defecto, -(q.canales or 0), q.id))


def _posicion_de_audio(fuente: MuxFuente, tid: int) -> int | None:
    """La posición de una pista entre las de AUDIO de su fichero (lo que
    `-map 0:a:N` de ffmpeg cuenta)."""
    audio = [p.id for p in fuente.pistas if p.tipo == "audio"]
    return audio.index(tid) if tid in audio else None


def _salida_de_audio(proyecto: MuxProject, pista) -> int | None:
    """La posición, entre las de audio del MKV FINAL, de una pista elegida."""
    claves = {(i, p.id): p for i, f in enumerate(proyecto.fuentes) for p in f.pistas}
    n = 0
    for q in proyecto.pistas:
        o = claves.get((q.fuente, q.id))
        if o is None or o.tipo != "audio":
            continue
        if q.fuente == pista.fuente and q.id == pista.id:
            return n
        n += 1
    return None
