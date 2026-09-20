"""El relato de un MKV analizado (Tab 2): qué análisis tiene y si sirve.

Tab 2 no persiste proyectos —su estado vive en `openMkvProjects`, en memoria
del navegador— así que su columna no lista trabajos sino **los MKVs que ya se
han analizado**, que es lo que la caché de `/config/mkv_audits/` conoce.

Aun así el estado de cada fila se derivaba en el JS (`_mkvRecienteEstado`) y
el vocabulario no era el de las otras dos pestañas: la misma idea —«esto ya
está hecho», «esto hay que rehacerlo»— se decía con otras palabras y se
pintaba con otros colores según la columna en la que estuvieras.

Aquí se resuelve una vez, con el vocabulario compartido. Dos situaciones son
NUEVAS y las aporta esta pestaña, porque describen algo que las otras dos no
tenían cómo decir:

- **`no_disponible`** — el fichero ya no está donde se analizó. No es un
  error: el análisis sigue siendo válido y se reaprovecha en cuanto el MKV
  reaparezca, porque la caché va por fingerprint y la ruta es solo una pista.
- **`caducado`** — hay análisis, pero de una versión anterior de la app, así
  que `read_mkv_cache` no lo sirve y abrir ese MKV lo reanaliza. Anunciarlo
  como hecho sería prometer algo que la app va a recalcular.

Es puro: recibe la tarjeta que `_mkv_recientes_desde_cache` ya compone y no
toca disco.
"""
from __future__ import annotations

import relato
from i18n import t as tr


def _rotulo(situacion: str) -> str:
    """El rótulo lo pone la pestaña, el id lo comparten las tres.

    «Sin iniciar» describe un proyecto de Tab 1 que nunca se lanzó; el mismo
    id en Tab 2 es un MKV que SÍ está analizado, solo que sin el extendido.
    El estado es el mismo —queda trabajo por hacer— y la palabra no puede
    serlo.
    """
    return tr(f"relato.mkv_situacion_{situacion}")


def _situacion(t: dict) -> str:
    """Excluyentes y en orden fijo.

    El fichero ausente gana: sin MKV que abrir, qué análisis tenga guardado
    es secundario — y es además lo único que exige una acción del usuario
    (devolverlo a su sitio o borrar la entrada).
    """
    if not t.get("existe"):
        return relato.NO_DISPONIBLE
    if t.get("tiene_extendido"):
        return relato.TERMINADO
    if t.get("tiene_basico"):
        return relato.PREPARANDO
    return relato.CADUCADO


def _hechos(t: dict) -> list[dict]:
    """Qué análisis tiene este MKV, con el dato que lo sostiene."""
    return [
        relato.hecho(
            "fichero", tr('relato.mkv_hecho_fichero'),
            relato.HECHO_OK if t.get("existe") else relato.HECHO_AVISO,
            t.get("ruta") or ""),
        relato.hecho(
            "analisis_basico", tr('relato.mkv_hecho_basico'),
            relato.HECHO_OK if t.get("tiene_basico") else relato.HECHO_PENDIENTE,
            tr('relato.mkv_basico_evidencia') if t.get("tiene_basico") else ""),
        relato.hecho(
            "analisis_extendido", tr('relato.mkv_hecho_extendido'),
            relato.HECHO_OK if t.get("tiene_extendido") else relato.HECHO_PENDIENTE,
            tr('relato.mkv_extendido_evidencia') if t.get("tiene_extendido") else ""),
        relato.hecho(
            "perfil_luminancia", tr('relato.mkv_hecho_luminancia'),
            relato.HECHO_OK if t.get("tiene_luminancia") else relato.HECHO_PENDIENTE,
            tr('relato.mkv_luminancia_evidencia') if t.get("tiene_luminancia") else ""),
    ]


def _porque(situacion: str) -> str:
    return {
        relato.NO_DISPONIBLE: tr('relato.mkv_porque_no_disponible'),
        relato.CADUCADO: tr('relato.mkv_porque_caducado'),
        relato.PREPARANDO: tr('relato.mkv_porque_solo_basico'),
        relato.TERMINADO: tr('relato.mkv_porque_completo'),
    }.get(situacion, "")


def resolver(t: dict) -> dict:
    """El relato de una fila de la columna de Tab 2."""
    situacion = _situacion(t)
    return {
        "situacion": situacion,
        "situacion_rotulo": _rotulo(situacion),
        # Un MKV analizado no tiene etapas: no es un trabajo por fases, es un
        # fichero con más o menos cosas sabidas. Inventarle una tira de fases
        # sería maquinaria que no usa.
        "etapas": [],
        "etapa": {},
        "porque": _porque(situacion),
        "decision": {"estado": relato.DECISION_NO_PROCEDE},
        "hechos": _hechos(t),
        "siguiente": "",
    }
