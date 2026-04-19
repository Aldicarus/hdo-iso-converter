"""
settings_store.py — Settings persistentes editables desde la UI.

Persiste en `/config/app_settings.json`. Los valores guardados aquí GANAN
sobre variables de entorno, para que el usuario pueda cambiar secretos
(TMDb API key…) sin reconstruir el contenedor.

Campos soportados:
  - tmdb_api_key: str    — opcional, habilita traducción ES→EN en Tab 3
  - google_api_key: str  — opcional, habilita listado+descarga de RPUs
                           del repositorio de REC_9999 en Google Drive

Los secretos NUNCA se devuelven crudos en el endpoint GET — solo se
envía `{configured: bool, last4: str}`. POST permite setear, vaciar o
dejar sin cambios (omitiendo la clave).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from threading import Lock
from typing import Any

_logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
SETTINGS_PATH = CONFIG_DIR / "app_settings.json"

_lock = Lock()
_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    if SETTINGS_PATH.exists():
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _cache = raw
            else:
                _cache = {}
        except Exception as e:
            _logger.warning("app_settings.json corrupto: %s — usando defaults", e)
            _cache = {}
    else:
        _cache = {}
    return _cache


def _save(data: dict[str, Any]) -> None:
    global _cache
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_PATH)
        _cache = data
    except OSError as e:
        _logger.error("No pude persistir app_settings.json: %s", e)
        raise


# ── Getters con fallback a env ──────────────────────────────────────────

def get_tmdb_api_key() -> str:
    """Prioridad: settings.json > TMDB_API_KEY env > vacío."""
    with _lock:
        stored = _load().get("tmdb_api_key", "").strip()
    if stored:
        return stored
    return os.environ.get("TMDB_API_KEY", "").strip()


def get_google_api_key() -> str:
    """Prioridad: settings.json > GOOGLE_API_KEY env > vacío."""
    with _lock:
        stored = _load().get("google_api_key", "").strip()
    if stored:
        return stored
    return os.environ.get("GOOGLE_API_KEY", "").strip()


# ── API pública consumida por main.py ───────────────────────────────────

def _status_for(stored: str, env: str) -> dict[str, Any]:
    effective = stored or env
    return {
        "configured": bool(effective),
        "source": "settings" if stored else ("env" if env else "none"),
        "last4": effective[-4:] if effective else "",
    }


def get_public_settings() -> dict[str, Any]:
    """Devuelve el estado de config sin exponer secretos crudos."""
    with _lock:
        stored = _load()
    return {
        "tmdb": _status_for(
            stored.get("tmdb_api_key", "").strip(),
            os.environ.get("TMDB_API_KEY", "").strip(),
        ),
        "google": _status_for(
            stored.get("google_api_key", "").strip(),
            os.environ.get("GOOGLE_API_KEY", "").strip(),
        ),
    }


def _update_field(field: str, new_value: str | None) -> None:
    """`None` = no tocar, `""` = borrar, otra cosa = setear."""
    if new_value is None:
        return
    with _lock:
        data = dict(_load())
        if new_value == "":
            data.pop(field, None)
        else:
            data[field] = new_value.strip()
        _save(data)


def update_tmdb_api_key(new_value: str | None) -> None:
    _update_field("tmdb_api_key", new_value)


def update_google_api_key(new_value: str | None) -> None:
    if new_value is None:
        return
    _update_field("google_api_key", new_value)
    # Al cambiar la Google key, invalidamos la caché de la hoja para que
    # el próximo fetch intente Sheets API v4 (o vuelva a CSV si se borró).
    try:
        from services.rec999_sheet import invalidate_cache as _inv_sheet
        _inv_sheet()
    except Exception:
        pass
    try:
        from services.rec999_drive import _cache_files as _df  # pragma: no cover
    except Exception:
        pass
