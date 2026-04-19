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

from services.settings_store import get_tmdb_api_key

_logger = logging.getLogger(__name__)

CONFIG_DIR   = Path(os.environ.get("CONFIG_DIR", "/config"))
CACHE_PATH   = CONFIG_DIR / "tmdb_cache.json"
CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 días


class TmdbMatch(BaseModel):
    tmdb_id: int
    title_en: str
    title_es: str = ""
    year: int | None = None


class TmdbDetails(BaseModel):
    """Info extendida de una película para la ficha en la UI."""
    tmdb_id: int
    title: str = ""              # título localizado (es-ES)
    original_title: str = ""
    original_language: str = ""
    release_date: str = ""       # YYYY-MM-DD
    year: int | None = None
    overview: str = ""
    poster_url: str = ""         # URL completa (w342)
    backdrop_url: str = ""       # URL completa (w780)
    runtime_minutes: int = 0
    vote_average: float = 0.0
    vote_count: int = 0
    genres: list[str] = []
    tagline: str = ""
    imdb_id: str = ""
    homepage: str = ""
    tmdb_url: str = ""           # link al movie page en TMDb


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
    return bool(get_tmdb_api_key())


async def test_api_key(api_key: str) -> tuple[bool, str]:
    """Valida una TMDb key contra /configuration. Devuelve (ok, mensaje)."""
    if not api_key.strip():
        return False, "API key vacía"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                "https://api.themoviedb.org/3/configuration",
                params={"api_key": api_key.strip()},
            )
        if resp.status_code == 200:
            return True, "API key válida"
        if resp.status_code == 401:
            return False, "API key inválida"
        return False, f"Error TMDb ({resp.status_code})"
    except Exception as e:
        return False, f"Error de red: {e}"


def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


async def _fetch_english_title(client: httpx.AsyncClient, tmdb_id: int,
                                api_key: str) -> str | None:
    """Para films con original_title no-latino (japonés, chino…) hay que
    pedir explícitamente la versión en-US para el título inglés."""
    try:
        resp = await client.get(
            f"https://api.themoviedb.org/3/movie/{tmdb_id}",
            params={"api_key": api_key, "language": "en-US"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("title") or data.get("original_title")
    except Exception:
        return None


async def search_movies(title_es: str, year: int | None,
                        limit: int = 5) -> list[TmdbMatch]:
    """Devuelve hasta `limit` candidatos ordenados por popularidad de
    TMDb. Para cada uno resuelve el título inglés (prefiriendo
    `original_title` si es ASCII, o la versión en-US en caso contrario).
    """
    if not title_es:
        return []

    cache = _load_cache()
    key = _cache_key(title_es, year) + f"|n={limit}"
    hit = cache.get(key)
    if hit and (time.time() - hit.get("fetched_at", 0)) < CACHE_TTL_SECONDS:
        cached = hit.get("results") or []
        return [TmdbMatch(**r) for r in cached]

    api_key = get_tmdb_api_key()
    if not api_key:
        return []

    params: dict[str, str] = {
        "api_key": api_key,
        "query": title_es,
        "language": "es-ES",
        "include_adult": "false",
    }
    if year:
        params["year"] = str(year)

    matches: list[TmdbMatch] = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.themoviedb.org/3/search/movie", params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            results = (data.get("results") or [])[:limit]
            for r in results:
                tmdb_id = int(r.get("id") or 0)
                release = r.get("release_date") or ""
                tmdb_year = int(release[:4]) if release[:4].isdigit() else None
                title_es_r = r.get("title") or title_es
                original = r.get("original_title") or ""
                # Si el original no es ASCII (anime, cine asiático, etc.),
                # tiramos de /movie/{id}?language=en-US
                if original and _is_ascii(original):
                    title_en = original
                else:
                    fetched_en = await _fetch_english_title(client, tmdb_id, api_key)
                    title_en = fetched_en or original or title_es_r
                matches.append(TmdbMatch(
                    tmdb_id=tmdb_id,
                    title_en=title_en,
                    title_es=title_es_r,
                    year=tmdb_year,
                ))
    except Exception as e:
        _logger.warning("TMDb search falló: %s", e)
        return []

    cache[key] = {
        "fetched_at": time.time(),
        "results": [m.model_dump() for m in matches],
    }
    _save_cache()
    return matches


async def search_movie(title_es: str, year: int | None) -> TmdbMatch | None:
    """Atajo: primer candidato de search_movies (mantiene API antigua)."""
    matches = await search_movies(title_es, year, limit=1)
    return matches[0] if matches else None


# Base de imágenes de TMDb. w342 = 342px ancho (card medio), w780 = backdrop.
_IMG_BASE = "https://image.tmdb.org/t/p"
_POSTER_SIZE   = "w342"
_BACKDROP_SIZE = "w780"


async def fetch_details(tmdb_id: int, lang: str = "es-ES") -> TmdbDetails | None:
    """Trae info extendida de una película. Cache en disco con TTL largo."""
    if not tmdb_id:
        return None

    cache = _load_cache()
    ck = f"details|{tmdb_id}|{lang}"
    hit = cache.get(ck)
    if hit and (time.time() - hit.get("fetched_at", 0)) < CACHE_TTL_SECONDS:
        data = hit.get("result")
        return TmdbDetails(**data) if data else None

    api_key = get_tmdb_api_key()
    if not api_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.themoviedb.org/3/movie/{tmdb_id}",
                params={"api_key": api_key, "language": lang},
            )
            resp.raise_for_status()
            raw = resp.json()
    except Exception as e:
        _logger.warning("TMDb details falló (id=%s): %s", tmdb_id, e)
        return None

    poster_path   = raw.get("poster_path") or ""
    backdrop_path = raw.get("backdrop_path") or ""
    release       = raw.get("release_date") or ""
    year = int(release[:4]) if release[:4].isdigit() else None
    genres = [g.get("name", "") for g in (raw.get("genres") or []) if g.get("name")]

    details = TmdbDetails(
        tmdb_id=tmdb_id,
        title=raw.get("title") or "",
        original_title=raw.get("original_title") or "",
        original_language=raw.get("original_language") or "",
        release_date=release,
        year=year,
        overview=raw.get("overview") or "",
        poster_url=f"{_IMG_BASE}/{_POSTER_SIZE}{poster_path}" if poster_path else "",
        backdrop_url=f"{_IMG_BASE}/{_BACKDROP_SIZE}{backdrop_path}" if backdrop_path else "",
        runtime_minutes=int(raw.get("runtime") or 0),
        vote_average=float(raw.get("vote_average") or 0.0),
        vote_count=int(raw.get("vote_count") or 0),
        genres=genres,
        tagline=raw.get("tagline") or "",
        imdb_id=raw.get("imdb_id") or "",
        homepage=raw.get("homepage") or "",
        tmdb_url=f"https://www.themoviedb.org/movie/{tmdb_id}",
    )
    cache[ck] = {"fetched_at": time.time(), "result": details.model_dump()}
    _save_cache()
    return details
