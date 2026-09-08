"""
queue_manager.py — La cola única de trabajo diferido.

Empezó siendo la cola de Fases D+E de Tab 1: una lista de `session_id` y una
función que los ejecuta. Hoy es la cola de **todo el trabajo diferido de las
tres pestañas**, porque la política de concurrencia (medida el 2026-09-07) es:

  · lo INTERACTIVO —abrir un MKV, analizar un disco, un borrado— va siempre en
    paralelo, cueste lo que cueste. Medido: +15 % al trabajo largo, barato a
    cambio de que la pestaña siga usable durante los 20-40 min de un rip.
  · lo DIFERIDO —rips, las nueve fases CMv4.0, el análisis extendido, las
    copias— pasa por aquí, de uno en uno. El daño de solaparlos no es la
    lentitud: es que **contamina `ffmpeg_wall_seconds`**, del que salen
    `_adaptive_timeout` y el modelo de ETA. Esa regresión es silenciosa.

El detalle que decide el diseño
───────────────────────────────
La cola se persiste en `queue_state.json` y **un callable no se persiste**. Así
que cada entrada guarda `(tab, tipo, clave)` y hay un **registro de runners**
`tipo → función(trabajo)`: al arrancar se reconstruye la cola desde el JSON y
cada entrada encuentra su runner por el tipo. Es el patrón de `_CMV40_RUNNERS`,
que ya funciona en Tab 3.

Compatibilidad del formato
──────────────────────────
`get_status()` sigue devolviendo `running` y `queue` como **ids de sesión de
Tab 1**, porque el panel de la cola y el WS los leen así en una veintena de
sitios. Lo demás viaja en campos nuevos (`running_job`, `jobs`). Un
`queue_state.json` de la versión anterior es una lista de strings y se lee como
rips.
"""
import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

# Fichero de persistencia de la cola
_CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
_QUEUE_STATE_FILE = _CONFIG_DIR / "queue_state.json"

# Tipos de trabajo diferido. El `tipo` es lo que resuelve el runner, así que
# tiene que sobrevivir a un reinicio: son literales, no referencias.
TIPO_RIP = "rip"
TIPO_FASE_CMV40 = "fase_cmv40"
TIPO_ANALISIS_EXTENDIDO = "analisis_extendido"
TIPO_COPIA_BIBLIOTECA = "copia_biblioteca"
TIPO_SERIE = "crear_serie"


@dataclass(frozen=True)
class TrabajoEnCola:
    """Una entrada de la cola. Tiene que poder serializarse entera."""

    tab: str        # "rip" | "mkv" | "cmv40" (los tab_id de workload)
    tipo: str       # uno de los TIPO_* de arriba
    clave: str      # id con el que se identifica: session id, audit id…
    que: str = ""   # descripción legible para la UI
    datos: dict = field(default_factory=dict)   # lo que el runner necesita
    # SOBRE QUÉ actúa, con el identificador que usa su pestaña para pintarlo:
    # el session id de un rip o de un proyecto CMv4.0, la ruta del MKV en un
    # análisis extendido. Sirve para que la lista de proyectos de cada pestaña
    # pueda marcar el suyo como «en cola» sin adivinarlo del texto de `que`.
    # No es la clave: la de un análisis extendido es su `audit_id`.
    sobre: str = ""

    def __post_init__(self) -> None:
        # Para rip, serie y fase CMv4.0 la clave YA es el identificador del
        # proyecto, así que el default correcto es ella: solo los dos trabajos
        # de Tab 2 —cuya clave es un audit id— tienen que decirlo.
        if not self.sobre:
            object.__setattr__(self, "sobre", self.clave)

    @property
    def id(self) -> str:
        """Identidad a efectos de duplicados.

        Es `(tipo, clave)` y no la clave a secas: un proyecto CMv4.0 encola
        fases sucesivas con la MISMA clave (su session id), y dos rips de
        sesiones distintas nunca comparten clave. Con la clave sola, encolar la
        Fase F de un proyecto cuya Fase C sigue en la cola se descartaría como
        duplicado.
        """
        return f"{self.tipo}:{self.clave}"

    def a_json(self) -> dict:
        return {"tab": self.tab, "tipo": self.tipo, "clave": self.clave,
                "que": self.que, "datos": self.datos, "sobre": self.sobre}

    @staticmethod
    def de_json(x) -> "TrabajoEnCola":
        # Una entrada de la versión anterior es un session_id pelado.
        if isinstance(x, str):
            return TrabajoEnCola(tab="rip", tipo=TIPO_RIP, clave=x, que=x)
        return TrabajoEnCola(
            tab=x.get("tab") or "rip",
            tipo=x.get("tipo") or TIPO_RIP,
            clave=x.get("clave") or "",
            que=x.get("que") or "",
            datos=x.get("datos") or {},
            # Ausente en las entradas escritas antes de que existiera; se cae a
            # la clave, que es lo correcto para los tres tipos que la usan como
            # identificador de su proyecto.
            sobre=x.get("sobre") or x.get("clave") or "",
        )


class QueueManager:
    """Cola FIFO con ejecución secuencial del trabajo diferido."""

    def __init__(self) -> None:
        self._queue: list[TrabajoEnCola] = []
        self._running: Optional[TrabajoEnCola] = None
        self._lock = asyncio.Lock()
        self._runners: dict[str, Callable[[TrabajoEnCola], Coroutine]] = {}
        self._update_callbacks: list[Callable] = []
        self._load_state()

    # ── Runners ───────────────────────────────────────────────────────

    def registrar_runner(self, tipo: str,
                         fn: Callable[["TrabajoEnCola"], Coroutine]) -> None:
        """Asocia un tipo de trabajo con la corutina que lo ejecuta.

        Se resuelve **al despachar**, no al encolar, que es lo que permite que
        la cola sobreviva a un reinicio: el JSON guarda el tipo y el runner se
        vuelve a registrar al importar el router.
        """
        self._runners[tipo] = fn

    def set_run_fn(self, fn: Callable[[str], Coroutine]) -> None:
        """Compat: registra el runner de los rips, que recibe el `session_id`.

        Tab 1 lleva así desde el principio y su firma no tiene por qué cambiar
        para que la cola sea genérica.
        """
        async def _adaptador(trabajo: TrabajoEnCola):
            return await fn(trabajo.clave)
        self.registrar_runner(TIPO_RIP, _adaptador)

    # ── Persistencia ──────────────────────────────────────────────────

    def _load_state(self) -> None:
        """Carga la cola persistida en disco (si existe)."""
        if not _QUEUE_STATE_FILE.exists():
            return
        try:
            data = json.loads(_QUEUE_STATE_FILE.read_text(encoding="utf-8"))
            self._queue = [TrabajoEnCola.de_json(x) for x in data.get("queue", [])]
            # running no se restaura — se recupera como sesión interrumpida
            logger.info("[QueueManager] Cola restaurada desde disco: %s",
                        [t.id for t in self._queue])
        except Exception as e:
            logger.warning("[QueueManager] No se pudo leer queue_state.json: %s", e)
            self._queue = []

    def _persist_state(self) -> None:
        """Guarda el estado actual de la cola a disco con escritura atómica
        (.tmp + rename) — sin esto, un kill mid-write dejaba la cola
        truncada y al rearrancar la cola entera se perdía."""
        try:
            _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "running": self._running.clave if self._running else None,
                "queue": [t.a_json() for t in self._queue],
            }
            tmp = _QUEUE_STATE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, _QUEUE_STATE_FILE)
        except Exception as e:
            logger.warning("[QueueManager] No se pudo guardar queue_state.json: %s", e)

    # ── API ───────────────────────────────────────────────────────────

    def on_update(self, cb: Callable) -> None:
        """Registra un callback async invocado tras cada cambio de estado."""
        self._update_callbacks.append(cb)

    async def encolar(self, trabajo: TrabajoEnCola, *,
                      a_la_cabeza: bool = False) -> dict:
        """Añade un trabajo a la cola. Si nada corre, lo arranca.

        `a_la_cabeza` es para **la fase siguiente de un proyecto CMv4.0ya
        empezado**: dejarla al final la pondría detrás de rips de 40 minutos
        con 250-400 GB de artefactos intermedios ocupando `/mnt/tmp` mientras
        tanto. Un proyecto a medias se termina antes de empezar otra cosa.
        """
        async with self._lock:
            ids = {t.id for t in self._queue}
            if (self._running and self._running.id == trabajo.id) or trabajo.id in ids:
                return self.get_status()
            if a_la_cabeza:
                self._queue.insert(0, trabajo)
            else:
                self._queue.append(trabajo)
            self._persist_state()

        await self._notify()
        asyncio.create_task(self._process())
        return self.get_status()

    async def enqueue(self, session_id: str) -> dict:
        """Compat: encola un rip de Tab 1 por su `session_id`."""
        return await self.encolar(TrabajoEnCola(
            tab="rip", tipo=TIPO_RIP, clave=session_id, que=f"rip de {session_id}"))

    @staticmethod
    def _es(trabajo: "TrabajoEnCola", ref: str) -> bool:
        """¿Se refiere `ref` a este trabajo?

        La identidad de una entrada es `tipo:clave` —la columna de trabajo la
        manda así— pero la cola nació con rips y su endpoint recibe el session
        id pelado, que es lo que sigue enviando todo lo de Tab 1. Se aceptan
        las dos: un session id no lleva dos puntos, así que no hay ambigüedad.
        """
        return ref in (trabajo.id, trabajo.clave)

    def buscar(self, ref: str):
        """La entrada encolada a la que apunta `ref`, o None."""
        return next((t for t in self._queue if self._es(t, ref)), None)

    async def cancel(self, session_id: str) -> bool:
        """Elimina de la cola lo que tenga esa clave, si aún no ha empezado."""
        cancelled = False
        async with self._lock:
            restantes = [t for t in self._queue if not self._es(t, session_id)]
            if len(restantes) != len(self._queue):
                self._queue = restantes
                cancelled = True
                self._persist_state()
        if cancelled:
            await self._notify()
        return cancelled

    async def reorder(self, ordered_ids: list[str]) -> None:
        """Reordena la cola según la lista de CLAVES. Solo mueve lo ya encolado.

        Lo que no aparezca en `ordered_ids` **se conserva al final**, en su
        orden: un cliente que solo conozca una parte de la cola no puede tirar
        el resto arrastrando una entrada.
        """
        async with self._lock:
            indice = {}
            for t in self._queue:
                indice.setdefault(t.id, t)
                indice.setdefault(t.clave, t)
            movidos, vistos = [], set()
            for ref in ordered_ids:
                t = indice.get(ref)
                if t is not None and t.id not in vistos:
                    movidos.append(t)
                    vistos.add(t.id)
            self._queue = movidos + [t for t in self._queue if t.id not in vistos]
            self._persist_state()
        await self._notify()

    async def descartar(self, claves) -> int:
        """Saca de la cola todo lo que tenga una de esas claves. Devuelve
        cuántos.

        Existe porque **reordenar no puede borrar**: `reorder` conserva lo que
        no se menciona, para que una reordenación del panel de Tab 1 —que solo
        conoce sus rips— no se lleve por delante las fases CMv4.0 que haya
        detrás. Descartar es otra intención y necesita decirse.
        """
        claves = set(claves)
        async with self._lock:
            restantes = [t for t in self._queue
                         if not any(self._es(t, c) for c in claves)]
            n = len(self._queue) - len(restantes)
            if n:
                self._queue = restantes
                self._persist_state()
        if n:
            await self._notify()
        return n

    def get_status(self) -> dict:
        """Estado de la cola.

        `running` y `queue` son **ids de sesión de Tab 1**, no todo lo que hay:
        el panel de la cola y el WS los leen así en una veintena de sitios y
        meter ahí una fase CMv4.0 los rompería. La vista completa va en
        `running_job` y `jobs`, que es lo que consume el dashboard.
        """
        rips = [t for t in self._queue if t.tipo == TIPO_RIP]
        corriendo_rip = self._running if (
            self._running and self._running.tipo == TIPO_RIP) else None
        return {
            "running": corriendo_rip.clave if corriendo_rip else None,
            "queue": [t.clave for t in rips],
            # Vista completa, con todo lo diferido de las tres pestañas.
            "running_job": self._running.a_json() if self._running else None,
            "jobs": [t.a_json() for t in self._queue],
        }

    # ── Internos ──────────────────────────────────────────────────────

    async def _notify(self) -> None:
        status = self.get_status()
        for cb in self._update_callbacks:
            try:
                await cb(status)
            except Exception:
                pass

    async def _process(self) -> None:
        """Toma el siguiente trabajo de la cola y lo ejecuta.

        Espera si hay trabajo pesado que esta cola no controla. Con todo lo
        diferido pasando por aquí eso solo puede ser un resto de la migración,
        pero **esperar es lo correcto y fallar no**: cargarse un trabajo ya
        encolado por algo que el usuario hizo después sería gratuito, y la cola
        es precisamente el sitio donde esperar es lo natural.

        Lo que corre desde AQUÍ no cuenta como bloqueo (se compara la clave):
        si no, la cola se tomaría su propio trabajo por un bloqueo ajeno y se
        esperaría a sí misma.
        """
        import workload
        while True:
            propia = self._running.clave if self._running else None
            bloqueo = workload.bloqueado_por(excepto=propia,
                                             ignorar_tab=workload.TAB_RIP)
            if bloqueo is None:
                break
            async with self._lock:
                if not self._queue:
                    return          # ya no hay nada que esperar
            logger.info("[QueueManager] en espera: %s", bloqueo.describir())
            await asyncio.sleep(5)

        async with self._lock:
            if self._running is not None:
                return
            if not self._queue:
                return
            self._running = self._queue.pop(0)
            self._persist_state()

        await self._notify()

        trabajo = self._running
        try:
            runner = self._runners.get(trabajo.tipo)
            if runner is None:
                # Un tipo sin runner es una entrada que sobrevivió a un cambio
                # de versión. Se descarta con ruido: dejarla al frente
                # bloquearía la cola entera para siempre.
                logger.warning(
                    "[QueueManager] sin runner para el tipo %r (%s) — descartado",
                    trabajo.tipo, trabajo.clave)
            else:
                await runner(trabajo)
        finally:
            async with self._lock:
                self._running = None
                self._persist_state()
            await self._notify()
            if self._queue:
                asyncio.create_task(self._process())


queue_manager = QueueManager()
