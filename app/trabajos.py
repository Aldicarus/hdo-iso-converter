"""
trabajos.py — Una sola forma para «qué está pasando», sea el trabajo que sea.

El problema
───────────
Los cinco tipos de trabajo pesado medían su progreso de cinco maneras
distintas, y ninguna se parecía a las otras:

| trabajo             | fase              | %              | ETA        |
|---------------------|-------------------|----------------|------------|
| rip                 | 4 fases           | **el cliente** lo parseaba del log | **el cliente** |
| crear serie         | 6 subpasos × N ep | interpolado en el cliente | no |
| análisis extendido  | 3 pasos           | servidor       | no         |
| copia de biblioteca | 2 pasos           | servidor       | sí         |
| fase CMv4.0         | 10 fases          | servidor       | sí + modelo|

Con eso, una columna que vigile el trabajo de toda la aplicación necesitaría
cinco renderizadores distintos, y el del rip **no funcionaría con la pestaña
cerrada**: su barra solo existía mientras un navegador estuviera escuchando el
WebSocket.

Qué hace
────────
Define **un** diccionario de progreso y un registro de adaptadores: cada
pestaña aporta el suyo (`registrar(tipo, fn)`) y aquí se compone la respuesta.
Es el mismo patrón que `queue_manager.registrar_runner` y por el mismo motivo:
mantiene la dependencia en un solo sentido —`main` sirve, los routers
aportan— sin que este módulo tenga que conocer a ninguno.

La regla que hereda del resto del proyecto
──────────────────────────────────────────
**El porcentaje sale de evidencia o no sale.** `pct_medido` distingue una barra
real de un hueco, y `eta_fuente` distingue una medida de una extrapolación. Las
constantes de tipo `elapsed / constante` envejecen con cada cambio del
pipeline: el pipe de la Fase A dejó las suyas desfasadas en horas y un job de
26 minutos llegó a anunciar 49. Una cifra inventada con pinta de dato es peor
que un hueco.
"""
import logging
import re
from typing import Callable

logger = logging.getLogger(__name__)


# ── Cómo se llama un trabajo ────────────────────────────────────────────────
#
# Lo que se lee en la columna es la PELÍCULA, no el fichero: «Drive (2011)» y
# no «Drive.2011.UHD.BluRay.x265 [DV FEL].mkv». Antes cada punto componía su
# texto por su cuenta y salían cuatro estilos distintos —el nombre del MKV con
# sus tags, la ruta interna del destino, el session id crudo cuando faltaba el
# nombre— para la misma pregunta.
#
# Vive aquí porque aquí está el vocabulario común de los trabajos, y porque
# `trabajos.py` no depende de ningún router: lo pueden llamar los tres.

_TMDB_ANCHO = re.compile(r"(/t/p/)w\d+(/)")


def nombre_de_trabajo(tmdb: dict | None = None, fichero: str = "",
                      serie: dict | None = None) -> str:
    """El nombre de la película tal y como se le enseña al usuario.

    Por orden de fiabilidad: lo que dijo TMDb (que es el título real, con su
    año), y si no hubo match, el fichero **sin tags ni extensión** — con el
    mismo parser que usa la recomendación CMv4.0, para no tener dos.

    `serie` es `{nombre, anio, temporada, episodio}`: en una sesión de serie el
    `title` de TMDb es el del EPISODIO, así que enseñarlo solo dejaría
    «Pilot (2011)» sin decir de qué serie.
    """
    if serie and serie.get("nombre"):
        cabeza = _con_anio(serie["nombre"], serie.get("anio"))
        t, e = serie.get("temporada"), serie.get("episodio")
        if t is not None and e is not None:
            return f"{cabeza} · S{int(t):02d}E{int(e):02d}"
        return cabeza
    if tmdb and (tmdb.get("title") or "").strip():
        return _con_anio(tmdb["title"].strip(), tmdb.get("year"))
    if fichero:
        from services.cmv40_recommend import parse_mkv_filename
        titulo, anio = parse_mkv_filename(fichero)
        if titulo:
            return _con_anio(titulo, anio)
    return ""


def _con_anio(titulo: str, anio) -> str:
    return f"{titulo} ({anio})" if anio else titulo


def cartel_de(tmdb: dict | None = None, fichero: str = "",
              serie: dict | None = None) -> tuple[str, str]:
    """La película y su miniatura: **lo que la tarjeta necesita, de un tiro**.

    Es la única forma de pedirlas, y por eso el respaldo vale para todos los
    tipos por igual. La carátula sale de `tmdb_info` cuando la sesión la
    tiene, y si no —los dos trabajos de Tab 2 no tienen sesión, y 11 de las 49
    sesiones del NAS no tienen ficha— de **la caché de TMDb en disco**, que
    para ese fichero ya suele estar llena porque abrirlo pidió su ficha.

    **Nunca sale a la red.** La columna se refresca cada 2 s: pedir una
    carátula por tarjeta sería una petición por fila y por vuelta, y encima
    metería la latencia de TMDb en el camino de encolar. Sin caché se devuelve
    vacía y la tarjeta cae a su icono.
    """
    titulo = nombre_de_trabajo(tmdb, fichero, serie)
    poster = poster_de(tmdb)
    if not poster and fichero:
        try:
            from services.cmv40_recommend import parse_mkv_filename
            from services.tmdb import poster_en_cache
            poster = poster_de(
                {"poster_url": poster_en_cache(*parse_mkv_filename(fichero))})
        except Exception:                               # noqa: BLE001
            # Quedarse sin miniatura es cosmético; que reviente el encolado,
            # no.
            poster = ""
    return titulo, poster


def poster_de(tmdb: dict | None = None, ancho: str = "w92") -> str:
    """La miniatura del póster, o cadena vacía.

    TMDb da la URL ya construida a w342, que para una miniatura de 40 px son
    ~30 KB por fila. El tamaño va en la propia ruta, así que se reescribe: la
    misma imagen a w92 son ~4 KB. Si la URL no tiene esa forma se devuelve tal
    cual — es preferible una miniatura pesada a ninguna.
    """
    url = (tmdb or {}).get("poster_url") or ""
    return _TMDB_ANCHO.sub(rf"\g<1>{ancho}\g<2>", url) if url else ""

# `tipo` de trabajo (los de `queue_manager`) → función que devuelve su progreso.
# La firma es `fn(trabajo) -> dict | None`: recibe el `TrabajoEnCola` que está
# corriendo y devuelve los campos de progreso, o None si todavía no sabe nada.
_adaptadores: dict[str, Callable] = {}


def registrar(tipo: str, fn: Callable) -> None:
    """Asocia un tipo de trabajo con quien sabe describir su progreso."""
    _adaptadores[tipo] = fn


def _vacio(trabajo) -> dict:
    """Lo que se sabe de cualquier trabajo sin preguntar a nadie."""
    return {
        "id": trabajo.clave,
        "sobre": getattr(trabajo, "sobre", "") or trabajo.clave,
        "tab": trabajo.tab,
        "tipo": trabajo.tipo,
        "que": trabajo.que,
        # La película y su miniatura. Viajan en la entrada de la cola desde que
        # se encoló —donde la sesión estaba en la mano— en vez de resolverse en
        # cada poll: la columna se refresca cada 2 s y esto no puede costar una
        # lectura de disco por vuelta.
        "titulo": getattr(trabajo, "titulo", "") or "",
        "poster": getattr(trabajo, "poster", "") or "",
        "fase": "",
        "fase_label": "",
        # El PASO dentro de la fase ("Demuxing BL/EL", "Episodio 3 · PGS").
        # Va aparte de `fase_label` porque son dos cosas: el overlay viejo de
        # CMv4.0 enseñaba las dos —la fase en el título y el paso sobre la
        # barra— y al unificar se colapsaron en una, así que dejó de verse en
        # qué punto de la fase iba. Vacío cuando la fase no tiene pasos.
        "paso": "",
        "fase_n": 0,
        "fases_total": 0,
        "pct": None,
        "pct_medido": False,
        "segundos": 0,
        "eta_s": None,
        "eta_fuente": None,
        "cancelable": True,
    }


def progreso_de(trabajo) -> dict:
    """El progreso del trabajo que está corriendo, en la forma común.

    Si su pestaña no registró adaptador —o el adaptador falla— se devuelve lo
    que se sabe sin preguntar: qué es y de qué pestaña. **Un fallo aquí no
    puede tumbar la columna**: quedarse sin saber el porcentaje es un
    inconveniente; quedarse sin saber que hay algo corriendo, no.
    """
    base = _vacio(trabajo)
    fn = _adaptadores.get(trabajo.tipo)
    if fn is None:
        return base
    try:
        extra = fn(trabajo)
    except Exception as e:                              # noqa: BLE001
        logger.warning("[trabajos] el adaptador de %s falló: %s", trabajo.tipo, e)
        return base
    if isinstance(extra, dict):
        base.update({k: v for k, v in extra.items() if k in base or k == "detalle"})
    return base


def eta_por_porcentaje(segundos: float, pct: float | None) -> int | None:
    """Los segundos que faltan, extrapolando del avance ya observado.

    Es una MEDIDA, no un modelo: sale del tiempo que ha costado el trozo ya
    hecho, no de una constante calibrada. Devuelve None mientras no haya
    suficiente para decir algo —por debajo del 1 % cualquier división explota— y
    también al llegar al 100 %, donde lo que queda no es tiempo sino el cierre.
    """
    if not pct or pct <= 1 or pct >= 100 or segundos <= 0:
        return None
    return max(0, round(segundos * (100.0 - pct) / pct))
