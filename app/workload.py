"""
workload.py — Qué trabajo pesado hay en marcha, en toda la aplicación.

El problema
───────────
Cada pestaña serializaba lo suyo y ninguna sabía de las otras:

  · Tab 1 tiene una cola FIFO de uno.
  · Tab 2 permite un análisis extendido y una copia desde Library.
  · Tab 3 bloquea **por `session_id`**, así que N proyectos podían correr
    fases a la vez.

Sumado: tres o más procesos pesados (`mkvmerge`, `ffmpeg`, `dovi_tool`)
peleándose por 4 cores y un solo pool ZFS. Lo evidente es que todo va más
lento; lo que no se ve es peor: **`_adaptive_timeout` y el modelo de ETA se
anclan en `ffmpeg_wall_seconds`**, así que una medición tomada con contención
envenena en silencio las dos calibraciones de las que depende todo el progreso
medido — y un timeout calculado a partir de ella puede quedarse corto en el
siguiente job.

Qué hace
────────
Un registro en memoria de **lo que está corriendo ahora mismo**, para que la
UI pueda decirlo y para que las mediciones sepan si se tomaron con contención.
Este proceso es el único que arranca trabajo, así que la memoria es la fuente
de verdad (igual que `_cmv40_activas` para el punto verde del tab).

**Ya no rechaza nada.** Hubo un `exigir_libre` que devolvía 409 cuando otra
pestaña tenía trabajo pesado; con la cola única eso es justo lo que no hay que
hacer —lo diferido espera turno— y el último llamador se fue con el bloque 3.
Lo que queda es `bloqueado_por`, que la cola consulta para esperar, y
`hay_contencion`, que protege las calibraciones.

Qué NO entra en la cola, a propósito
────────────────────────────────────
Lo INTERACTIVO: abrir un MKV, analizar un disco, `disc-probe`, los pre-flight,
un borrado. Es cómo se navega, dura segundos o pocos minutos y el usuario está
delante; encolarlo dejaría la pestaña inservible mientras corre un rip de 40
minutos. Igual con `mkvpropedit`, que es O(1). Medido: que una consulta se
solape con un trabajo largo le cuesta a este un **+15 %**.
"""
import contextlib
import itertools
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Las tres clases de trabajo
# ─────────────────────────
# El criterio no es "cuánto tarda" sino **quién está esperando**, porque es lo
# que decide qué se puede hacer con ello.
#
#   LIGERO       Acotado y de navegación: leer metadata, listar, `mkvpropedit`
#                (que es O(1)), una consulta a TMDb. NO se registra: apuntarlo
#                sería ruido en el dashboard y coste en el hot path.
#
#   INTERACTIVO  Pesado —escala con el disco o con la peli— pero con el usuario
#                delante esperando la respuesta. Se registra para que se VEA,
#                pero **no bloquea a nadie**: negarle abrir un MKV a alguien
#                porque hay un rip en curso deja la pestaña inservible durante
#                media hora. En el bloque 4 es la clase que expropia.
#
#   DIFERIDO     Pesado y puede esperar: un rip, una fase CMv4.0, el análisis
#                extendido. Se registra **y bloquea** — hoy con un 409, en el
#                bloque 3 con la cola única.
#
# La clase describe cómo trata la app al trabajo HOY, no una aspiración.
#
# La política, decidida el 2026-09-07 con las medidas delante: **solo las
# consultas van en paralelo**. Todo lo largo —rips, las nueve fases, el
# análisis extendido, las copias— comparte una cola. El coste medido de que una
# consulta se solape con un trabajo largo es **+15 %** para el largo, que es
# barato a cambio de que la pestaña siga usable durante los 20-40 min de un
# rip; el de solapar dos trabajos largos no es la lentitud sino que **contamina
# `ffmpeg_wall_seconds`**, y de ahí salen `_adaptive_timeout` y el ETA.
CLASE_LIGERO = "ligero"
CLASE_INTERACTIVO = "interactivo"
CLASE_DIFERIDO = "diferido"

# Etiquetas de pestaña tal y como las ve el usuario en la UI, para que el
# mensaje del 409 se pueda leer sin saber cómo se llaman los módulos.
TAB_RIP = "💿 Blu-Ray ISO → MKV"
TAB_MKV = "✏️ Consultar / Editar MKV"
TAB_CMV40 = "✨ Upgrade Dolby Vision CMv4.0"

# Las etiquetas de arriba son para leerlas; esto es para compararlas. La UI
# necesita saber de qué pestaña es un trabajo, y hacerlo contra el literal con
# emoji la ata a un texto que existe para poder cambiarse.
TAB_IDS = {TAB_RIP: "rip", TAB_MKV: "mkv", TAB_CMV40: "cmv40"}


@dataclass(frozen=True)
class Trabajo:
    clave: str      # id con el que se libera (session id, job id…)
    tab: str        # dónde lo lanzó el usuario
    que: str        # descripción legible: "rip de Peli (2024)"
    desde: float    # time.monotonic() al registrarlo
    clase: str = CLASE_DIFERIDO   # ver las tres clases arriba

    @property
    def bloquea(self) -> bool:
        """Solo lo diferido impide arrancar otra cosa.

        Lo interactivo se registra para verse, no para vetar: el usuario está
        delante esperando y hacerle esperar a un rip de 40 minutos convierte la
        pestaña en un cartel de "vuelve luego".
        """
        return self.clase == CLASE_DIFERIDO

    @property
    def segundos(self) -> float:
        return max(0.0, time.monotonic() - self.desde)

    def describir(self) -> str:
        mins = int(self.segundos // 60)
        tiempo = f"{mins} min" if mins else f"{int(self.segundos)} s"
        return f"{self.tab} — {self.que} (lleva {tiempo})"


_activos: dict[str, Trabajo] = {}


def registrar(clave: str, tab: str, que: str,
              clase: str = CLASE_DIFERIDO) -> None:
    """Marca un trabajo pesado como en curso. Idempotente por clave."""
    _activos[clave] = Trabajo(clave=clave, tab=tab, que=que,
                              desde=time.monotonic(), clase=clase)
    logger.info("[workload] arranca [%s] %s", clase, _activos[clave].describir())


@contextlib.contextmanager
def ocupado(clave: str, tab: str, que: str, clase: str = CLASE_DIFERIDO):
    """`registrar` + `liberar` con el `finally` puesto.

    La regla del proyecto es que el hueco se suelta SIEMPRE en un `finally` y
    por la clave propia; si eso se escapa, la app queda bloqueada para todo lo
    demás hasta reiniciar el contenedor. Escribirlo a mano en cada punto de
    entrada es exactamente el sitio donde se olvida.
    """
    registrar(clave, tab, que, clase)
    try:
        yield
    finally:
        liberar(clave)


def liberar(clave: str) -> None:
    """Lo saca del registro. Silencioso si no estaba."""
    t = _activos.pop(clave, None)
    if t is not None:
        # Con la clase delante, un `grep "\[workload\]"` del log del contenedor
        # basta para medir cuánta contención real hay antes de tocar la
        # política — que es para lo que se clasificó todo esto.
        logger.info("[workload] termina [%s] %s", t.clase, t.describir())


def en_curso() -> list[Trabajo]:
    return sorted(_activos.values(), key=lambda t: t.desde)


def bloqueado_por(excepto: str | None = None,
                  ignorar_tab: str | None = None) -> Trabajo | None:
    """El trabajo que impide arrancar otro, o None si la casa está libre.

    `excepto` es la clave del que pregunta: un proyecto de Tab 3 que avanza a
    su fase siguiente NO se bloquea a sí mismo — es el mismo job, no uno nuevo.

    `ignorar_tab` es para **la pestaña que ya se serializa sola**. Tab 1 tiene
    una cola FIFO que ejecuta de uno en uno, así que un rip en curso no puede
    impedir *encolar* el siguiente: esperar es justo lo que la cola hace. Sin
    esto, lanzar tres ISOs seguidos —el flujo normal— daba 409 en el segundo.
    NO vale para Tab 3, que bloquea por `session_id` y por eso necesita este
    registro; ahí la pestaña no se serializa sola.
    """
    for t in en_curso():
        if not t.bloquea:
            continue
        if excepto is not None and t.clave == excepto:
            continue
        if ignorar_tab is not None and t.tab == ignorar_tab:
            continue
        return t
    return None


def hay_contencion(excepto: str | None = None) -> bool:
    """¿Se está midiendo con otro trabajo pesado por medio?

    Lo consultan las mediciones que alimentan `_adaptive_timeout` y el modelo
    de ETA: un `ffmpeg_wall_seconds` tomado con contención no describe el NAS,
    describe ese momento, y usarlo como ancla arrastra el error a los jobs
    siguientes.

    Cuenta **también lo interactivo**, al revés que `bloqueado_por`: para una
    medición un `ffmpeg` es un `ffmpeg` lo lance quien lo lance. Que no vetemos
    abrir un MKV durante un rip no significa que el rip no lo note.
    """
    return any(t.clave != excepto for t in en_curso())


# Contador para dar clave única a cada petición interactiva. No vale el id de
# sesión: dos "abrir MKV" simultáneos compartirían clave y el `liberar` del
# primero soltaría el hueco del segundo — `registrar` es idempotente por clave.
_secuencia = itertools.count(1)


def marca(que: str, tab: str):
    """Dependencia de FastAPI que registra el endpoint como INTERACTIVO.

    Se pone en el decorador de la ruta:

        @router.post("/api/analyze",
                     dependencies=[Depends(workload.marca("análisis del disco",
                                                          workload.TAB_RIP))])

    Registra al entrar y libera al salir, pase lo que pase — la teardown de una
    dependencia con `yield` corre también si el endpoint lanza o si el cliente
    se desconecta. No bloquea a nadie: la clase interactiva se apunta para
    **verse** (en `/api/activity` y en el dashboard), no para vetar.

    No recibe la `Request` a propósito: así este módulo no importa FastAPI, que
    es lo que le permite cargarse en un test puro sin levantar la app. El texto
    es el que ve el usuario, que además se lee mejor que una ruta con llaves.
    """
    async def _dep():
        with ocupado(f"{tab}#{next(_secuencia)}", tab, que, CLASE_INTERACTIVO):
            yield
    _dep.__wl_interactivo__ = True   # ← lo que busca el test de cobertura
    return _dep


def limpiar() -> None:
    """Vacía el registro. Al arrancar (nada puede estar corriendo todavía) y
    entre tests."""
    _activos.clear()


# ─────────────────────────────────────────────────────────────────────────────
# La clase de cada punto de entrada
# ─────────────────────────────────────────────────────────────────────────────
# Antes de tocar la política había que saber qué hay. El resultado del censo:
# de los 94 endpoints, **la inmensa mayoría es navegación** y solo una docena
# hace trabajo que escala con el vídeo.
#
# La tabla existe para dos cosas: que el dashboard sepa qué está pasando sin
# adivinarlo, y que **añadir un endpoint obligue a decidir su clase** — lo
# vigila `test_clasificacion_del_trabajo.py`, que compara esta tabla contra el
# esquema OpenAPI real y falla si sobra o falta una ruta.
#
# Las notas `→ bloque N` marcan las dos entradas que el plan mueve más
# adelante; hoy la tabla describe lo que la app hace, no lo que hará.
CLASE_POR_RUTA: dict[str, str] = {
    # ── Trabajo DIFERIDO: pesado y puede esperar. Bloquea (409 hoy, cola en
    #    el bloque 3). Son los doce de siempre.
    "POST /api/sessions/{session_id}/execute":        CLASE_DIFERIDO,  # el rip D+E
    "POST /api/mkv/quality-audit":                    CLASE_DIFERIDO,  # análisis extendido
    "POST /api/mkv/apply":                            CLASE_DIFERIDO,  # copia desde biblioteca
    # ~30 s de montaje más 15-30 s por episodio: para una temporada de diez,
    # cinco minutos largos de disco.
    "POST /api/create-series-sessions":               CLASE_DIFERIDO,
    "POST /api/cmv40/{session_id}/analyze-source":    CLASE_DIFERIDO,  # Fase A
    "POST /api/cmv40/{session_id}/target-rpu-from-mkv":   CLASE_DIFERIDO,  # Fase B2
    "POST /api/cmv40/{session_id}/extract":           CLASE_DIFERIDO,  # Fase C
    "POST /api/cmv40/{session_id}/apply-sync":        CLASE_DIFERIDO,  # Fase E
    "POST /api/cmv40/{session_id}/inject":            CLASE_DIFERIDO,  # Fase F
    "POST /api/cmv40/{session_id}/remux":             CLASE_DIFERIDO,  # Fase G
    "POST /api/cmv40/{session_id}/validate":          CLASE_DIFERIDO,  # Fase H

    # ── Trabajo INTERACTIVO: pesado con el usuario delante. Se ve, no bloquea.
    "POST /api/analyze":                    CLASE_INTERACTIVO,  # Fase A+B del disco
    "POST /api/disc-probe":                 CLASE_INTERACTIVO,  # escaneo de candidatos
    "POST /api/mkv/analyze":                CLASE_INTERACTIVO,  # abrir un MKV
    # Las dos formas rápidas de dar el RPU target. Medido sobre los proyectos
    # del NAS: mediana de 2 s y 3 s (p90 10 s). Encolar una descarga de tres
    # segundos detrás de un rip de 40 minutos no protegería nada y dejaría al
    # usuario mirando el asistente. La tercera (`from-mkv`) sí es pesada.
    "POST /api/cmv40/{session_id}/target-rpu-path":       CLASE_INTERACTIVO,
    "POST /api/cmv40/{session_id}/target-rpu-from-drive": CLASE_INTERACTIVO,
    # Y los dos pre-flight: mediana **9 s**, p90 49 s y máximo 116 s sobre los
    # 91 del NAS. Son lo PRIMERO que corre al crear un proyecto, así que
    # bloquearlos dejaba el flujo muerto nada más empezar — y encolarlos
    # detrás de un rip, peor: el asistente se queda esperando 40 minutos por
    # nueve segundos de trabajo.
    "POST /api/cmv40/{session_id}/preflight-target":      CLASE_INTERACTIVO,
    "POST /api/cmv40/{session_id}/preflight-source":      CLASE_INTERACTIVO,
    "POST /api/sessions/{session_id}/reset-chapters": CLASE_INTERACTIVO,  # re-monta el ISO
    # Borrados: `rmtree` de decenas o cientos de GB sobre ZFS.
    "DELETE /api/cmv40/{session_id}":       CLASE_INTERACTIVO,
    "POST /api/cmv40/{session_id}/cleanup": CLASE_INTERACTIVO,
    "POST /api/cmv40/cleanup/bulk":         CLASE_INTERACTIVO,
    "POST /api/cleanup/execute":            CLASE_INTERACTIVO,
    "POST /api/cmv40/{session_id}/reset-to/{target_phase}": CLASE_INTERACTIVO,

    # ── LIGERO: todo lo demás. Navegación, metadata, red y ediciones O(1).
    #    No se registra.
    "GET /api/health":                      CLASE_LIGERO,
    "GET /api/status":                      CLASE_LIGERO,
    "GET /api/activity":                    CLASE_LIGERO,
    "GET /api/historial":                   CLASE_LIGERO,
    "GET /api/version":                     CLASE_LIGERO,
    "GET /api/version/check-updates":       CLASE_LIGERO,
    "POST /api/version/ignore-update":      CLASE_LIGERO,
    "GET /api/settings":                    CLASE_LIGERO,
    "POST /api/settings":                   CLASE_LIGERO,
    "POST /api/settings/test-tmdb":         CLASE_LIGERO,
    "POST /api/settings/test-google":       CLASE_LIGERO,
    "POST /api/settings/test-sheet":        CLASE_LIGERO,
    "POST /api/settings/test-drive-folder": CLASE_LIGERO,
    "GET /api/cleanup/scan":                CLASE_LIGERO,
    "GET /api/library/browse":              CLASE_LIGERO,
    # Tab 1
    "GET /api/sources":                     CLASE_LIGERO,
    "GET /api/isos":                        CLASE_LIGERO,
    "GET /api/sessions":                    CLASE_LIGERO,
    "GET /api/sessions/{session_id}":       CLASE_LIGERO,
    "PUT /api/sessions/{session_id}":       CLASE_LIGERO,
    "DELETE /api/sessions/{session_id}":    CLASE_LIGERO,
    "GET /api/sessions/{session_id}/check-iso":        CLASE_LIGERO,
    "POST /api/sessions/{session_id}/cancel":          CLASE_LIGERO,
    "POST /api/sessions/{session_id}/reapply-rules":   CLASE_LIGERO,
    "POST /api/sessions/{session_id}/recalculate-name": CLASE_LIGERO,
    "POST /api/check-duplicate":            CLASE_LIGERO,  # SHA del primer MB
    "GET /api/analyze/progress":            CLASE_LIGERO,
    "GET /api/disc-probe/progress":         CLASE_LIGERO,
    "GET /api/series-create-progress":      CLASE_LIGERO,
    "GET /api/queue":                       CLASE_LIGERO,
    "POST /api/queue/reorder":              CLASE_LIGERO,
    "DELETE /api/queue/{session_id}":       CLASE_LIGERO,
    "GET /api/tv-search":                   CLASE_LIGERO,
    "GET /api/tv-details/{tmdb_id}":        CLASE_LIGERO,
    "GET /api/tv-season/{tmdb_id}/{season_number}": CLASE_LIGERO,
    # Tab 2
    "GET /api/mkv/files":                   CLASE_LIGERO,
    "GET /api/mkv/files-in-isos":           CLASE_LIGERO,
    "GET /api/mkv/cache-info":              CLASE_LIGERO,
    "DELETE /api/mkv/cache-info":           CLASE_LIGERO,
    "GET /api/mkv/apply/progress":          CLASE_LIGERO,
    "POST /api/mkv/apply/cancel":           CLASE_LIGERO,
    "GET /api/mkv/quality-audit/progress":  CLASE_LIGERO,
    "POST /api/mkv/quality-audit/cancel":   CLASE_LIGERO,
    "GET /api/mkv/light-profile-cached":    CLASE_LIGERO,  # solo lee la caché
    # Tab 3
    "GET /api/cmv40":                       CLASE_LIGERO,
    "GET /api/cmv40-active":                CLASE_LIGERO,
    "GET /api/cmv40/{session_id}":          CLASE_LIGERO,
    "POST /api/cmv40/create":               CLASE_LIGERO,
    "GET /api/cmv40/eta-model":             CLASE_LIGERO,
    "GET /api/cmv40/rpu-files":             CLASE_LIGERO,
    "GET /api/cmv40/recommend":             CLASE_LIGERO,
    "GET /api/cmv40/recommend-from-filename": CLASE_LIGERO,
    "GET /api/cmv40/repo-rpus":             CLASE_LIGERO,
    "GET /api/cmv40/repo-survey":           CLASE_LIGERO,
    "GET /api/cmv40/cleanup/preview":       CLASE_LIGERO,
    "GET /api/cmv40/{session_id}/sync-data": CLASE_LIGERO,  # cacheado, en un thread
    "GET /api/cmv40/{session_id}/reset-preview/{target_phase}": CLASE_LIGERO,
    "POST /api/cmv40/tmdb-lookup":          CLASE_LIGERO,
    "POST /api/cmv40/tmdb-search":          CLASE_LIGERO,
    "POST /api/cmv40/{session_id}/tmdb-refresh":   CLASE_LIGERO,
    "POST /api/cmv40/{session_id}/refresh-sheet":  CLASE_LIGERO,
    "POST /api/cmv40/{session_id}/rename-output":  CLASE_LIGERO,
    "POST /api/cmv40/{session_id}/auto-pipeline":  CLASE_LIGERO,
    "POST /api/cmv40/{session_id}/clear-error":    CLASE_LIGERO,
    "POST /api/cmv40/{session_id}/cancel":         CLASE_LIGERO,
    "POST /api/cmv40/{session_id}/mark-synced":    CLASE_LIGERO,
    "POST /api/cmv40/{session_id}/reset-sync":     CLASE_LIGERO,
    "POST /api/cmv40/{session_id}/accept-keep":    CLASE_LIGERO,
    "POST /api/cmv40/{session_id}/override-recommendation": CLASE_LIGERO,
    "POST /api/cmv40/{session_id}/acknowledge-critical-gates": CLASE_LIGERO,
    "POST /api/cmv40/{session_id}/verify-artifacts": CLASE_LIGERO,
}
