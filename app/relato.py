"""El relato de un trabajo: qué pasa, dónde estoy, por qué y qué se decidió.

Un job de CMv4.0 le habla al usuario por **cinco superficies** —el modal del
pre-flight, el log, la card de análisis, las cards de fase y la columna de
trabajo— y hasta ahora cada una derivaba por su cuenta qué estaba pasando. De
ahí salían las incoherencias que el usuario reportó el 2026-09-19: la ficha
decía «Análisis pendiente» en dos proyectos con estados opuestos, y tras
cancelar uno no mencionaba la cancelación por ninguna parte — el hecho más
importante que le había pasado al proyecto vivía solo en el log.

No es un problema de textos: es que **no había un sitio donde se resolviera
«qué está pasando»**. Este módulo es ese sitio. Es el mismo movimiento que
`cmv40_strategy` hizo con la matriz de workflows y `trabajos` con el progreso,
y por los mismos motivos: una réplica de una regla se desincroniza en
silencio, y arreglarlas de una en una es lo que se estuvo haciendo.

## Cómo se usa

Cada pestaña aporta su resolutor con `registrar(tab, fn)` —igual que
`trabajos.registrar` y `queue_manager.registrar_runner`— así que este módulo
no conoce ninguna pestaña y la dependencia sigue en un solo sentido.

    relato.registrar(workload.TAB_CMV40, resolver_cmv40)
    data["relato"] = relato.resolver(workload.TAB_CMV40, session)

## Lo que NO es

- **No se persiste.** Se calcula al servir, como `session.plan`. El modelo de
  78 campos no se toca: Pydantic ignora en silencio lo que no reconoce, y un
  save posterior borraría lo que no supiera leer.
- **No decide nada.** Lee el estado y lo cuenta. Quien decide sigue siendo
  `cmv40_strategy` para la ruta y el pipeline para el resto.
- **`siguiente` NUNCA se escribe en el log.** Es un rótulo de interfaz, que se
  repinta y se corrige solo; en el log sería la promesa colgando que la regla
  del proyecto prohíbe desde que una fase anunciaba la siguiente y al cancelar
  quedaba dicha. Lo guarda `test_relato::TestElSiguienteNoLlegaAlLog`.
"""
from __future__ import annotations

from typing import Callable

# ── El vocabulario, compartido por las tres pestañas ────────────────────────
#
# Son EXCLUYENTES y se resuelven en un orden fijo: un proyecto archivado que
# arrastra un error es «archivado», no «detenido_por_error». El orden vive en
# cada resolutor porque los estados de origen son suyos; la lista está aquí
# para que las tres pestañas usen las mismas palabras y la UI pinte igual.
PREPARANDO          = "preparando"            # creado, sin trabajo todavía
EN_MARCHA           = "en_marcha"             # hay una fase corriendo
ESPERANDO_TURNO     = "esperando_turno"       # en la cola, sin empezar
ESPERANDO_DECISION  = "esperando_decision"    # parado, te toca a ti
DETENIDO_POR_ERROR  = "detenido_por_error"    # parado, hay que resolver algo
CANCELADO           = "cancelado"             # lo paraste tú
TERMINADO           = "terminado"             # hay fichero
ARCHIVADO           = "archivado"             # terminado y limpiado
# Las dos últimas las aporta Tab 2, y describen algo que las otras dos no
# tenían cómo decir: el fichero ya no está donde se analizó (que NO es un
# error — la caché va por fingerprint y se reaprovecha en cuanto reaparezca),
# y hay trabajo hecho pero de una versión anterior, así que no sirve.
NO_DISPONIBLE       = "no_disponible"         # el fichero no está donde estaba
CADUCADO            = "caducado"              # hay trabajo, pero ya no vale

SITUACIONES = (PREPARANDO, EN_MARCHA, ESPERANDO_TURNO, ESPERANDO_DECISION,
               DETENIDO_POR_ERROR, CANCELADO, TERMINADO, ARCHIVADO,
               NO_DISPONIBLE, CADUCADO)

# Estado de un hecho comprobado. Mismos nombres que los chips del modal del
# pre-flight, que es de donde salen: reusar su vocabulario evita traducir
# entre dos escalas al pintarlos.
HECHO_OK        = "ok"
HECHO_AVISO     = "aviso"
HECHO_DUDA      = "duda"
HECHO_FALLO     = "fallo"
HECHO_PENDIENTE = "pendiente"
HECHO_EN_CURSO  = "en_curso"

ESTADOS_DE_HECHO = (HECHO_OK, HECHO_AVISO, HECHO_DUDA, HECHO_FALLO,
                    HECHO_PENDIENTE, HECHO_EN_CURSO)

# Estado de la decisión del usuario.
DECISION_NO_PROCEDE = "no_procede"   # no hay nada que decidir
DECISION_PENDIENTE  = "pendiente"    # se está esperando su respuesta
DECISION_TOMADA     = "tomada"       # contestó, y consta qué y cuándo

ESTADOS_DE_DECISION = (DECISION_NO_PROCEDE, DECISION_PENDIENTE, DECISION_TOMADA)


_resolutores: dict[str, Callable] = {}


def registrar(tab: str, fn: Callable) -> None:
    """Asocia una pestaña con quien sabe contar el relato de sus trabajos."""
    _resolutores[tab] = fn


def resolver(tab: str, *args, **kw) -> dict | None:
    """El relato del trabajo, o `None` si esa pestaña no tiene resolutor.

    **Que falle no puede costar la petición.** El relato es lo que hace la
    pantalla entendible; no verlo es un inconveniente, no poder abrir el
    proyecto no lo es. Es el mismo criterio que el resto de textos derivados
    de `GET /api/cmv40/{id}`.
    """
    fn = _resolutores.get(tab)
    if fn is None:
        return None
    try:
        return fn(*args, **kw)
    except Exception:           # noqa: BLE001 — a propósito, ver el docstring
        import logging
        logging.getLogger(__name__).warning(
            "No se pudo componer el relato de %s", tab, exc_info=True)
        return None


def hecho(id: str, que: str, estado: str, evidencia: str = "") -> dict:
    """Un hecho comprobado, con el dato que lo sostiene.

    `evidencia` no es decorado: es lo que convierte «El bin aporta CMv4.0» en
    algo que el usuario puede contrastar («Perfil 7 FEL · CM v4.0 · 222.274
    frames»). Un hecho sin evidencia se pinta igual, pero se nota.
    """
    assert estado in ESTADOS_DE_HECHO, estado
    return {"id": id, "que": que, "estado": estado, "evidencia": evidencia}
