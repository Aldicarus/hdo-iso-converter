"""
settings_store.py — Settings persistentes editables desde la UI.

Persiste en `/config/app_settings.json`. Los valores guardados aquí GANAN
sobre variables de entorno, para que el usuario pueda cambiar secretos
(TMDb API key…) sin reconstruir el contenedor.

Campos soportados:
  - tmdb_api_key: str          — opcional, habilita traducción ES→EN en Tab 3
  - google_api_key: str        — opcional, habilita listado+descarga de RPUs
                                 del repositorio de REC_9999 en Google Drive
  - cmv40_drive_folder_url: str — URL (o ID) de la carpeta Drive del repo de
                                 REC_9999. El acceso requiere donación previa
                                 (ver UI). Tratada como secret (no devuelta
                                 cruda al frontend) porque compartirla equivale
                                 a regalar acceso pagado.
  - cmv40_sheet_url: str       — URL del Google Sheet de recomendaciones.
                                 Tiene default público (sheet oficial DoviTools)
                                 pero se puede override. NO es secret.

Los secretos NUNCA se devuelven crudos en el endpoint GET — solo se
envía `{configured: bool, last4: str}`. POST permite setear, vaciar o
dejar sin cambios (omitiendo la clave).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from threading import Lock
from typing import Any

_logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
SETTINGS_PATH = CONFIG_DIR / "app_settings.json"

# Default público del sheet de DoviTools (pestaña GRADE CHECK).
# El usuario puede overrideearlo en Configuración.
DEFAULT_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "15i0a84uiBtWiHZ5CXZZ7wygLFXwYOd84/edit?gid=828864432"
)

# El Drive folder ID NO tiene default hardcoded — requiere donación al autor
# del repo. Hasta que el usuario no lo configure, toda la sección Repo queda
# deshabilitada con explicación del paywall.
DEFAULT_DRIVE_FOLDER_URL = ""


# ── Parseo de URLs de Google ────────────────────────────────────────────

_DRIVE_FOLDER_RE = re.compile(r"/folders/([A-Za-z0-9_\-]{10,})")
_SHEET_ID_RE     = re.compile(r"/spreadsheets/d/([A-Za-z0-9_\-]{10,})")
_SHEET_GID_RE    = re.compile(r"[#?&]gid=(\d+)")


def parse_drive_folder_id(value: str) -> str:
    """Acepta URL completa de Drive o ID pelado. Devuelve el ID o ''."""
    v = (value or "").strip()
    if not v:
        return ""
    m = _DRIVE_FOLDER_RE.search(v)
    if m:
        return m.group(1)
    # Si no hay path /folders/, asumimos que es el ID suelto si encaja.
    if re.fullmatch(r"[A-Za-z0-9_\-]{10,}", v):
        return v
    return ""


def parse_sheet_id_gid(value: str) -> tuple[str, str]:
    """Extrae (sheet_id, gid) de una URL de Google Sheets.
    Devuelve ('', '') si no se puede parsear."""
    v = (value or "").strip()
    if not v:
        return "", ""
    m_id = _SHEET_ID_RE.search(v)
    m_gid = _SHEET_GID_RE.search(v)
    sid = m_id.group(1) if m_id else ""
    gid = m_gid.group(1) if m_gid else "0"  # gid=0 = primera pestaña por defecto
    return sid, gid

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


# ── Getters/setters genéricos para campos no-secret ─────────────────────

def get_settings_value(key: str, default: Any = None) -> Any:
    """Lee un campo arbitrario del store. Para secrets usa los getters
    dedicados (que también consideran env como fallback)."""
    with _lock:
        return _load().get(key, default)


def set_settings_value(key: str, value: Any) -> None:
    """Escribe un campo arbitrario en el store con persistencia atómica."""
    with _lock:
        data = dict(_load())
        data[key] = value
        _save(data)


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


def get_cmv40_drive_folder_url() -> str:
    """URL cruda del repo Drive configurado por el usuario. Vacío si no existe."""
    with _lock:
        stored = _load().get("cmv40_drive_folder_url", "").strip()
    if stored:
        return stored
    return os.environ.get("CMV40_DRIVE_FOLDER_URL", "").strip()


def get_cmv40_drive_folder_id() -> str:
    """ID del folder Drive extraído de la URL configurada (o env). Vacío si no."""
    raw = get_cmv40_drive_folder_url()
    if not raw:
        # Back-compat: env var legacy CMV40_DRIVE_FOLDER_ID si alguien lo tenía
        legacy = os.environ.get("CMV40_DRIVE_FOLDER_ID", "").strip()
        if legacy:
            return legacy
        return ""
    return parse_drive_folder_id(raw)


def get_cmv40_sheet_url() -> str:
    """URL cruda del sheet configurado. Fallback al default público."""
    with _lock:
        stored = _load().get("cmv40_sheet_url", "").strip()
    if stored:
        return stored
    env = os.environ.get("CMV40_SHEET_URL", "").strip()
    if env:
        return env
    return DEFAULT_SHEET_URL


def get_cmv40_sheet_id_gid() -> tuple[str, str]:
    """(sheet_id, gid) del sheet configurado. Fallback al default público."""
    return parse_sheet_id_gid(get_cmv40_sheet_url())


# ── API pública consumida por main.py ───────────────────────────────────

def _status_for(stored: str, env: str) -> dict[str, Any]:
    effective = stored or env
    return {
        "configured": bool(effective),
        "source": "settings" if stored else ("env" if env else "none"),
        "last4": effective[-4:] if effective else "",
    }


def get_public_settings() -> dict[str, Any]:
    """Devuelve el estado de config sin exponer secretos crudos.

    `cmv40_drive_folder_url` se trata como secret (el acceso es de pago,
    compartirlo es regalar acceso) → solo devolvemos configured+last4.
    `cmv40_sheet_url` NO es secret; devolvemos la URL cruda para que el
    usuario vea qué está usando y pueda restaurar el default.
    """
    with _lock:
        stored = _load()
    drive_url_stored = stored.get("cmv40_drive_folder_url", "").strip()
    drive_url_env    = os.environ.get("CMV40_DRIVE_FOLDER_URL", "").strip()
    drive_legacy_env = os.environ.get("CMV40_DRIVE_FOLDER_ID", "").strip()
    drive_effective  = drive_url_stored or drive_url_env or drive_legacy_env
    drive_folder_id  = parse_drive_folder_id(drive_effective) if drive_effective else ""

    sheet_url_stored = stored.get("cmv40_sheet_url", "").strip()
    sheet_url_env    = os.environ.get("CMV40_SHEET_URL", "").strip()
    sheet_effective  = sheet_url_stored or sheet_url_env or DEFAULT_SHEET_URL
    sheet_id, sheet_gid = parse_sheet_id_gid(sheet_effective)

    return {
        "tmdb": _status_for(
            stored.get("tmdb_api_key", "").strip(),
            os.environ.get("TMDB_API_KEY", "").strip(),
        ),
        "google": _status_for(
            stored.get("google_api_key", "").strip(),
            os.environ.get("GOOGLE_API_KEY", "").strip(),
        ),
        "drive_folder": {
            "configured": bool(drive_folder_id),
            "source": "settings" if drive_url_stored else ("env" if (drive_url_env or drive_legacy_env) else "none"),
            "last4": drive_effective[-4:] if drive_effective else "",
            "folder_id_last6": drive_folder_id[-6:] if drive_folder_id else "",
        },
        "sheet": {
            "configured": bool(sheet_id),
            "source": "settings" if sheet_url_stored else ("env" if sheet_url_env else "default"),
            "url": sheet_effective,           # NO es secret
            "default_url": DEFAULT_SHEET_URL,
            "sheet_id_last6": sheet_id[-6:] if sheet_id else "",
            "gid": sheet_gid,
            "is_default": sheet_effective == DEFAULT_SHEET_URL,
        },
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
    _invalidate_sheet_cache()
    _invalidate_drive_cache()


def update_cmv40_drive_folder_url(new_value: str | None) -> None:
    if new_value is None:
        return
    _update_field("cmv40_drive_folder_url", new_value)
    _invalidate_drive_cache()


def update_cmv40_sheet_url(new_value: str | None) -> None:
    if new_value is None:
        return
    _update_field("cmv40_sheet_url", new_value)
    _invalidate_sheet_cache()


def _invalidate_sheet_cache() -> None:
    try:
        from services.rec999_sheet import invalidate_cache as _inv_sheet
        _inv_sheet()
    except Exception:
        pass


def _invalidate_drive_cache() -> None:
    try:
        import services.rec999_drive as _drv
        _drv._cache_files = None
        _drv._cache_fetched_at = 0.0
    except Exception:
        pass
