"""
tmdb.py — Traducción ES→EN de títulos de película via The Movie Database.

Opcional: si `TMDB_API_KEY` no está seteado, el servicio devuelve None y
el flujo de recomendación cae a matching con el título crudo del fichero.
Caché persistente en `/config/tmdb_cache.json` — TMDb tiene cuota
generosa pero es absurdo reconsultar los mismos títulos.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx
from pydantic import BaseModel

_logger = logging.getLogger(__name__)

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
CONFIG_DIR   = Path(os.environ.get("CONFIG_DIR", "/config"))
CACHE_PATH   = CONFIG_DIR / "tmdb_cache.json"
CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 días


class TmdbMatch(BaseModel):
    tmdb_id: int
    title_en: str
    title_es: str = ""
    year: int | None = None


_cache: dict[str, dict] | None = None


def _load_cache() -> dict[str, dict]:
    global _cache
    if _cache is not None:
        return _cache
    if CACHE_PATH.exists():
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if not isinstance(_cache, dict):
                _cache = {}
        except Exception:
            _cache = {}
    else:
        _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(_cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _cache_key(title: str, year: int | None) -> str:
    return f"{title.lower().strip()}|{year or ''}"


def is_configured() -> bool:
    return bool(TMDB_API_KEY)


async def search_movie(title_es: str, year: int | None) -> TmdbMatch | None:
    """Busca en TMDb. Si no hay API key o falla, devuelve None."""
    if not title_es:
        return None

    cache = _load_cache()
    key = _cache_key(title_es, year)
    hit = cache.get(key)
    if hit and (time.time() - hit.get("fetched_at", 0)) < CACHE_TTL_SECONDS:
        result = hit.get("result")
        return TmdbMatch(**result) if result else None

    if not TMDB_API_KEY:
        return None

    params: dict[str, str] = {
        "api_key": TMDB_API_KEY,
        "query": title_es,
        "language": "es-ES",
        "include_adult": "false",
    }
    if year:
        params["year"] = str(year)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.themoviedb.org/3/search/movie", params=params,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        _logger.warning("TMDb search falló: %s", e)
        return None

    results = data.get("results") or []
    if not results:
        cache[key] = {"fetched_at": time.time(), "result": None}
        _save_cache()
        return None

    best = results[0]
    release = best.get("release_date") or ""
    tmdb_year = int(release[:4]) if release[:4].isdigit() else None
    match = TmdbMatch(
        tmdb_id=int(best.get("id") or 0),
        title_en=best.get("original_title") or best.get("title") or title_es,
        title_es=best.get("title") or title_es,
        year=tmdb_year,
    )
    cache[key] = {"fetched_at": time.time(), "result": match.model_dump()}
    _save_cache()
    return match
