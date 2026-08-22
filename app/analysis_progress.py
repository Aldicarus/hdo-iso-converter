"""
analysis_progress.py — El paso del análisis en curso, compartido por dos tabs.

Un solo endpoint, `GET /api/analyze/progress`, alimenta DOS modales: el de
"Analizando disco" del Tab 1 y el de "Abriendo MKV" del Tab 2. Los dos
análisis pasan por las mismas herramientas (mkvmerge → MediaInfo → conteo
PGS → dovi_tool), así que comparten los nombres de paso y el frontend
reutiliza el mismo poller.

Mientras los dos escritores vivían en `main.py` esto era una global y una
línea de `global _analyze_progress`. Al partir `main` por pestañas deja de
poder serlo: el `global` de un módulo rebindea **su** nombre, así que con el
dict declarado en `main` el Tab 2 habría empezado a escribir en una variable
propia y el modal se habría quedado en blanco sin un solo error. Vive aquí
para que la compartición sea explícita.

La consecuencia de compartir estado —dos análisis a la vez se pisan el
progreso— es anterior a la separación y sigue igual. Es cosmética (el paso
mostrado, no el resultado) y quien impide de verdad los solapamientos es
`workload`. Cambiarlo es un cambio de comportamiento, y no toca hacerlo en
un movimiento de código.
"""

# Se MUTA en vez de reasignarse: quien tenga la referencia (el endpoint que
# la devuelve) tiene que ver los cambios.
_estado: dict = {"step": "", "done": False}


def fijar(step: str = "", done: bool = False, **extra) -> None:
    """Sustituye el estado por completo, igual que la asignación de antes.

    `extra` recoge los campos que solo aparecen en algunos pasos (`pct` y
    `eta_s` del conteo PGS, `error` del fallo), que es exactamente lo que
    hacían los dicts literales que había repartidos por los callbacks.
    """
    _estado.clear()
    _estado.update({"step": step, "done": done, **extra})


def leer() -> dict:
    """Copia del estado, para devolver por el endpoint."""
    return dict(_estado)
