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
from typing import Callable

logger = logging.getLogger(__name__)

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
