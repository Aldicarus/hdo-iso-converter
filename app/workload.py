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
Un registro en memoria de lo que está corriendo. Los puntos de entrada de
trabajo pesado lo consultan antes de arrancar y **rechazan con 409** si ya hay
algo, diciendo qué lo bloquea. Este proceso es el único que arranca trabajo, así
que la memoria es la fuente de verdad (igual que `_cmv40_activas` para el punto
verde del tab).

Qué NO se bloquea, a propósito
──────────────────────────────
`POST /api/mkv/analyze` — abrir un MKV en Tab 2. Es cómo se navega, no un job:
está acotado (segundos a un par de minutos) y bloquearlo dejaría la pestaña
inservible mientras corre un rip. Igual con `mkvpropedit`, que es O(1).
"""
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Etiquetas de pestaña tal y como las ve el usuario en la UI, para que el
# mensaje del 409 se pueda leer sin saber cómo se llaman los módulos.
TAB_RIP = "💿 Blu-Ray ISO → MKV"
TAB_MKV = "✏️ Consultar / Editar MKV"
TAB_CMV40 = "✨ Upgrade Dolby Vision CMv4.0"


@dataclass(frozen=True)
class Trabajo:
    clave: str      # id con el que se libera (session id, job id…)
    tab: str        # dónde lo lanzó el usuario
    que: str        # descripción legible: "rip de Peli (2024)"
    desde: float    # time.monotonic() al registrarlo

    @property
    def segundos(self) -> float:
        return max(0.0, time.monotonic() - self.desde)

    def describir(self) -> str:
        mins = int(self.segundos // 60)
        tiempo = f"{mins} min" if mins else f"{int(self.segundos)} s"
        return f"{self.tab} — {self.que} (lleva {tiempo})"


_activos: dict[str, Trabajo] = {}


def registrar(clave: str, tab: str, que: str) -> None:
    """Marca un trabajo pesado como en curso. Idempotente por clave."""
    _activos[clave] = Trabajo(clave=clave, tab=tab, que=que,
                              desde=time.monotonic())
    logger.info("[workload] arranca %s", _activos[clave].describir())


def liberar(clave: str) -> None:
    """Lo saca del registro. Silencioso si no estaba."""
    t = _activos.pop(clave, None)
    if t is not None:
        logger.info("[workload] termina %s", t.describir())


def en_curso() -> list[Trabajo]:
    return sorted(_activos.values(), key=lambda t: t.desde)


def bloqueado_por(excepto: str | None = None) -> Trabajo | None:
    """El trabajo que impide arrancar otro, o None si la casa está libre.

    `excepto` es la clave del que pregunta: un proyecto de Tab 3 que avanza a
    su fase siguiente NO se bloquea a sí mismo — es el mismo job, no uno nuevo.
    """
    for t in en_curso():
        if excepto is None or t.clave != excepto:
            return t
    return None


def hay_contencion(excepto: str | None = None) -> bool:
    """¿Se está midiendo con otro trabajo pesado por medio?

    Lo consultan las mediciones que alimentan `_adaptive_timeout` y el modelo
    de ETA: un `ffmpeg_wall_seconds` tomado con contención no describe el NAS,
    describe ese momento, y usarlo como ancla arrastra el error a los jobs
    siguientes.
    """
    return bloqueado_por(excepto) is not None


def motivo_409(excepto: str | None = None) -> str | None:
    """El texto del 409, o None si no hay nada que bloquee."""
    t = bloqueado_por(excepto)
    if t is None:
        return None
    return (
        f"Ya hay trabajo pesado en curso: {t.describir()}. "
        "Espera a que termine o cancélalo — el NAS tiene 4 núcleos y un solo "
        "pool de discos, y solaparlos no va más rápido: además falsea las "
        "estimaciones de tiempo, que se calibran midiendo cuánto tarda ffmpeg."
    )


def exigir_libre(excepto: str | None = None) -> None:
    """Lanza HTTPException 409 si hay trabajo pesado en curso."""
    motivo = motivo_409(excepto)
    if motivo is None:
        return
    from fastapi import HTTPException
    raise HTTPException(status_code=409, detail=motivo)


def limpiar() -> None:
    """Vacía el registro. Al arrancar (nada puede estar corriendo todavía) y
    entre tests."""
    _activos.clear()
