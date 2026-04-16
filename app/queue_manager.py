"""
queue_manager.py — Cola secuencial para Fases D+E

Solo un trabajo se ejecuta a la vez. El resto espera en cola FIFO.
Al terminar un trabajo, el siguiente se inicia automáticamente.

La cola se persiste a disco (queue_state.json) para sobrevivir reinicios.
Al arrancar, las sesiones que quedaron en 'running' o 'queued' se resetean
a 'pending' con un mensaje explicativo (ver recover_interrupted_sessions).
"""
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

# Fichero de persistencia de la cola
_CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
_QUEUE_STATE_FILE = _CONFIG_DIR / "queue_state.json"


class QueueManager:
    """Cola FIFO con ejecución secuencial de pipelines D+E."""

    def __init__(self) -> None:
        self._queue: list[str] = []
        self._running: Optional[str] = None
        self._lock = asyncio.Lock()
        self._run_fn: Optional[Callable[[str], Coroutine]] = None
        self._update_callbacks: list[Callable] = []
        self._load_state()

    def _load_state(self) -> None:
        """Carga la cola persistida en disco (si existe)."""
        if _QUEUE_STATE_FILE.exists():
            try:
                data = json.loads(_QUEUE_STATE_FILE.read_text(encoding="utf-8"))
                self._queue = data.get("queue", [])
                # running no se restaura — se recupera como sesión interrumpida
                logger.info("[QueueManager] Cola restaurada desde disco: %s", self._queue)
            except Exception as e:
                logger.warning("[QueueManager] No se pudo leer queue_state.json: %s", e)
                self._queue = []

    def _persist_state(self) -> None:
        """Guarda el estado actual de la cola a disco."""
        try:
            _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            data = {"running": self._running, "queue": list(self._queue)}
            _QUEUE_STATE_FILE.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("[QueueManager] No se pudo guardar queue_state.json: %s", e)

    def set_run_fn(self, fn: Callable[[str], Coroutine]) -> None:
        """Registra la corutina que ejecuta el pipeline D+E para un session_id."""
        self._run_fn = fn

    def on_update(self, cb: Callable) -> None:
        """Registra un callback async invocado tras cada cambio de estado."""
        self._update_callbacks.append(cb)

    async def enqueue(self, session_id: str) -> dict:
        """
        Añade session_id a la cola. Si nada está en ejecución, lo inicia.
        Devuelve el estado actual de la cola.
        """
        async with self._lock:
            if session_id == self._running or session_id in self._queue:
                return self.get_status()
            self._queue.append(session_id)
            self._persist_state()

        await self._notify()
        asyncio.create_task(self._process())
        return self.get_status()

    async def cancel(self, session_id: str) -> bool:
        """Elimina session_id de la cola si aún no ha empezado."""
        cancelled = False
        async with self._lock:
            if session_id in self._queue:
                self._queue.remove(session_id)
                cancelled = True
                self._persist_state()
        if cancelled:
            await self._notify()
        return cancelled

    async def reorder(self, ordered_ids: list[str]) -> None:
        """Reordena la cola según la lista. Solo mueve sesiones ya encoladas."""
        async with self._lock:
            current = set(self._queue)
            self._queue = [sid for sid in ordered_ids if sid in current]
            self._persist_state()
        await self._notify()

    def get_status(self) -> dict:
        """Devuelve {'running': str|None, 'queue': [str, ...]}"""
        return {
            "running": self._running,
            "queue": list(self._queue),
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
        """Toma el siguiente trabajo de la cola y lo ejecuta."""
        async with self._lock:
            if self._running is not None:
                return
            if not self._queue:
                return
            self._running = self._queue.pop(0)
            self._persist_state()

        await self._notify()

        try:
            if self._run_fn:
                await self._run_fn(self._running)
        finally:
            async with self._lock:
                self._running = None
                self._persist_state()
            await self._notify()
            if self._queue:
                asyncio.create_task(self._process())


queue_manager = QueueManager()
