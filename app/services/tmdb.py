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
    # Campos adicionales para el selector multi-candidato (/tmdb-search).
    # Vienen directo del endpoint /search/movie de TMDb sin llamadas extra.
    poster_path: str = ""     # ruta relativa (prepend /t/p/w185 para thumb)
    poster_url: str = ""      # URL absoluta ya construida (w185)
    overview: str = ""        # sinopsis corta en es-ES
    vote_average: float = 0.0


# Base de imágenes de TMDb (declarada arriba para poder usarla en search_movies)
_IMG_BASE = "https://image.tmdb.org/t/p"


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
    tmp_path = CACHE_PATH.with_suffix(CACHE_PATH.suffix + ".tmp")
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps(_cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, CACHE_PATH)  # atómico en POSIX mismo-FS
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _cache_key(title: str, year: int | None) -> str:
    return f"{title.lower().strip()}|{year or ''}"


def _safe_err(e: Exception) -> str:
    """Mensaje de error sin filtrar la api_key. La key viaja como query param
    en la URL, y httpx la incrusta en str(HTTPStatusError) → loguear la
    excepción cruda la escribiría al log del NAS. Devolvemos solo el código de
    estado HTTP (o el tipo de excepción para errores de red/parseo)."""
    resp = getattr(e, "response", None)
    if resp is not None:
        return f"HTTP {resp.status_code}"
    return type(e).__name__


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
        # Si el cache trae [] (de antes del fallback sin año), tratamos
        # como miss para que el flujo nuevo reintente sin filtro de año.
        # Sin esto, cualquier titulo que ya haya fallado una vez con
        # filtro estricto se quedaria sin traduccion 30 dias.
        if cached:
            return [TmdbMatch(**r) for r in cached]

    api_key = get_tmdb_api_key()
    if not api_key:
        return []

    base_params: dict[str, str] = {
        "api_key": api_key,
        "query": title_es,
        "language": "es-ES",
        "include_adult": "false",
    }

    matches: list[TmdbMatch] = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Primer intento: si tenemos año, filtramos por él.
            # TMDb usa filtro estricto sobre primary_release_year — útil para
            # desambiguar remakes pero peligroso si el año del filename
            # discrepa del release_date oficial (estrenos en festivales,
            # diferencia ES vs original, etc).
            params = {**base_params}
            if year:
                params["year"] = str(year)
            resp = await client.get(
                "https://api.themoviedb.org/3/search/movie", params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            results = (data.get("results") or [])[:limit]
            # Fallback sin año si el filtro estricto devolvio vacio. La
            # tolerancia ±1 año vive en _best_match / rank_candidates, no
            # aqui — asi que si luego el match real no cuadra, igualmente
            # se descarta. Pero si cuadra (caso "Devuelvemela 2024" vs
            # "Bring Her Back" 2025 en TMDb), recuperamos la traduccion.
            if not results and year:
                params_noy = {**base_params}
                resp2 = await client.get(
                    "https://api.themoviedb.org/3/search/movie",
                    params=params_noy,
                )
                resp2.raise_for_status()
                data2 = resp2.json()
                results = (data2.get("results") or [])[:limit]
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
                poster = r.get("poster_path") or ""
                matches.append(TmdbMatch(
                    tmdb_id=tmdb_id,
                    title_en=title_en,
                    title_es=title_es_r,
                    year=tmdb_year,
                    poster_path=poster,
                    poster_url=(f"{_IMG_BASE}/w185{poster}" if poster else ""),
                    overview=r.get("overview") or "",
                    vote_average=float(r.get("vote_average") or 0.0),
                ))
    except Exception as e:
        _logger.warning("TMDb search falló: %s", _safe_err(e))
        return []

    # No cachear resultados vacios — el fallback sin año puede recuperar
    # traducciones que el filtro estricto perdió (caso "Devuelvemela 2024" /
    # "Bring Her Back 2025"). Si el cache se quedó con [], la siguiente
    # llamada saltaría a la API y reintentaría sola sin esperar 30 días.
    if matches:
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


# Tamaños de imagen TMDb. w342 = card medio, w780 = backdrop.
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
        _logger.warning("TMDb details falló (id=%s): %s", tmdb_id, _safe_err(e))
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


# ══════════════════════════════════════════════════════════════════════
#  TV SERIES SUPPORT (v2.5+)
#
#  Endpoints TMDb usados:
#    GET /3/search/tv?query=X&first_air_date_year=Y
#      Devuelve series candidatas con tmdb_id, name, first_air_date.
#    GET /3/tv/{id}?language=es-ES
#      Detalles: number_of_seasons, seasons[], etc.
#    GET /3/tv/{id}/season/{N}?language=es-ES
#      episodes[] con episode_number, name, runtime (min), air_date.
#
#  Cache: las series viven en el mismo /config/tmdb_cache.json pero con
#  prefijo de clave para no chocar con películas:
#    "tv:search:Mad Men|2007"
#    "tv:details:1104"
#    "tv:season:1104:1"
# ══════════════════════════════════════════════════════════════════════


class TvSearchResult(BaseModel):
    """Una serie candidata devuelta por search_tv_series."""
    tmdb_id: int
    name: str = ""               # nombre localizado (es-ES)
    original_name: str = ""
    first_air_date: str = ""     # YYYY-MM-DD
    year: int | None = None
    poster_url: str = ""         # URL completa (w185 thumb)
    overview: str = ""
    vote_average: float = 0.0


class TvSeasonSummary(BaseModel):
    """Resumen de una temporada devuelto en TvDetails.seasons[]."""
    season_number: int
    episode_count: int = 0
    air_date: str = ""
    name: str = ""
    poster_url: str = ""


class TvDetails(BaseModel):
    """Detalles extendidos de una serie."""
    tmdb_id: int
    name: str = ""
    original_name: str = ""
    first_air_date: str = ""
    year: int | None = None
    overview: str = ""
    poster_url: str = ""         # w342
    backdrop_url: str = ""       # w780
    number_of_seasons: int = 0
    number_of_episodes: int = 0
    vote_average: float = 0.0
    seasons: list[TvSeasonSummary] = []
    tmdb_url: str = ""


class TvEpisode(BaseModel):
    """Un episodio de una temporada."""
    episode_number: int
    name: str = ""
    overview: str = ""
    air_date: str = ""
    runtime_minutes: int = 0       # 0 si TMDb no lo tiene (común en specials)
    still_url: str = ""            # screenshot del episodio


_TV_POSTER_THUMB = "w185"
_TV_STILL_SIZE = "w300"


async def search_tv_series(
    query: str, year: int | None = None,
) -> list[TvSearchResult]:
    """Busca series en TMDb por título (es-ES). Devuelve top 5 candidatos.

    Usa `first_air_date_year` que filtra por año de la PREMIERE (no por
    cualquier air_date de cualquier episodio — ese es el quirk de TMDb
    documentado en https://www.themoviedb.org/talk/65af32bfd100b6010c82cc2d).

    Cache: 30 días (mismo TTL que películas)."""
    if not is_configured() or not query.strip():
        return []
    api_key = get_tmdb_api_key()
    if not api_key:
        return []

    cache = _load_cache()
    ck = f"tv:search:{query.lower().strip()}|{year or ''}"
    cached = cache.get(ck)
    if cached and time.time() - cached.get("fetched_at", 0) < CACHE_TTL_SECONDS:
        return [TvSearchResult(**r) for r in cached.get("result", [])]

    params = {
        "api_key": api_key,
        "query": query,
        "language": "es-ES",
        "include_adult": "false",
    }
    if year:
        params["first_air_date_year"] = str(year)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.themoviedb.org/3/search/tv", params=params,
            )
            r.raise_for_status()
            payload = r.json()
    except Exception:
        return []

    results = []
    for item in (payload.get("results") or [])[:5]:
        air = item.get("first_air_date") or ""
        yr = int(air[:4]) if air[:4].isdigit() else None
        poster = item.get("poster_path") or ""
        results.append(TvSearchResult(
            tmdb_id=int(item.get("id") or 0),
            name=item.get("name") or "",
            original_name=item.get("original_name") or "",
            first_air_date=air,
            year=yr,
            poster_url=f"{_IMG_BASE}/{_TV_POSTER_THUMB}{poster}" if poster else "",
            overview=item.get("overview") or "",
            vote_average=float(item.get("vote_average") or 0.0),
        ))

    cache[ck] = {
        "fetched_at": time.time(),
        "result": [x.model_dump() for x in results],
    }
    _save_cache()
    return results


async def fetch_tv_details(tmdb_id: int) -> TvDetails | None:
    """Obtiene detalles de una serie incluyendo el listado de temporadas.

    Cache: 30 días."""
    if not tmdb_id:
        return None
    if not is_configured():
        return None
    api_key = get_tmdb_api_key()
    if not api_key:
        return None

    cache = _load_cache()
    ck = f"tv:details:{tmdb_id}"
    cached = cache.get(ck)
    if cached and time.time() - cached.get("fetched_at", 0) < CACHE_TTL_SECONDS:
        return TvDetails(**cached.get("result", {}))

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}",
                params={"api_key": api_key, "language": "es-ES"},
            )
            r.raise_for_status()
            raw = r.json()
    except Exception:
        return None

    poster = raw.get("poster_path") or ""
    backdrop = raw.get("backdrop_path") or ""
    air = raw.get("first_air_date") or ""
    yr = int(air[:4]) if air[:4].isdigit() else None
    seasons = []
    for sraw in (raw.get("seasons") or []):
        s_poster = sraw.get("poster_path") or ""
        seasons.append(TvSeasonSummary(
            season_number=int(sraw.get("season_number") or 0),
            episode_count=int(sraw.get("episode_count") or 0),
            air_date=sraw.get("air_date") or "",
            name=sraw.get("name") or "",
            poster_url=f"{_IMG_BASE}/{_TV_POSTER_THUMB}{s_poster}" if s_poster else "",
        ))
    details = TvDetails(
        tmdb_id=tmdb_id,
        name=raw.get("name") or "",
        original_name=raw.get("original_name") or "",
        first_air_date=air,
        year=yr,
        overview=raw.get("overview") or "",
        poster_url=f"{_IMG_BASE}/{_POSTER_SIZE}{poster}" if poster else "",
        backdrop_url=f"{_IMG_BASE}/{_BACKDROP_SIZE}{backdrop}" if backdrop else "",
        number_of_seasons=int(raw.get("number_of_seasons") or 0),
        number_of_episodes=int(raw.get("number_of_episodes") or 0),
        vote_average=float(raw.get("vote_average") or 0.0),
        seasons=seasons,
        tmdb_url=f"https://www.themoviedb.org/tv/{tmdb_id}",
    )
    cache[ck] = {"fetched_at": time.time(), "result": details.model_dump()}
    _save_cache()
    return details


async def fetch_tv_season(
    tmdb_id: int, season_number: int,
) -> list[TvEpisode]:
    """Devuelve los episodios de una temporada con runtime, name y air_date.

    Cache: 30 días. El runtime es por episodio y se usa para el match
    heurístico contra los MPLS del disco."""
    if not tmdb_id or season_number is None:
        return []
    if not is_configured():
        return []
    api_key = get_tmdb_api_key()
    if not api_key:
        return []

    cache = _load_cache()
    ck = f"tv:season:{tmdb_id}:{season_number}"
    cached = cache.get(ck)
    if cached and time.time() - cached.get("fetched_at", 0) < CACHE_TTL_SECONDS:
        return [TvEpisode(**e) for e in cached.get("result", [])]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}",
                params={"api_key": api_key, "language": "es-ES"},
            )
            r.raise_for_status()
            raw = r.json()
    except Exception:
        return []

    episodes = []
    for eraw in (raw.get("episodes") or []):
        still = eraw.get("still_path") or ""
        episodes.append(TvEpisode(
            episode_number=int(eraw.get("episode_number") or 0),
            name=eraw.get("name") or "",
            overview=eraw.get("overview") or "",
            air_date=eraw.get("air_date") or "",
            runtime_minutes=int(eraw.get("runtime") or 0),
            still_url=f"{_IMG_BASE}/{_TV_STILL_SIZE}{still}" if still else "",
        ))

    cache[ck] = {
        "fetched_at": time.time(),
        "result": [x.model_dump() for x in episodes],
    }
    _save_cache()
    return episodes


def match_episodes_to_mpls(
    mpls_durations_min: list[float],
    tmdb_episodes: list[TvEpisode],
    tolerance_min: float = 1.0,
) -> list[dict]:
    """Heurística de matching MPLS↔episodio basada en runtime.

    Asume que los MPLS están ordenados por número y son CONSECUTIVOS
    dentro de la temporada — pero NO necesariamente empezando en E01.
    Casos reales (disco 2 de una temporada, mid-season set, etc.):
      - 3 MPLS [50, 50, 51] min, TMDb temp 1-8 con runtimes
        [60, 60, 60, 50, 50, 50, 70, 70] → mejor offset = E04.

    Algoritmo: probamos cada posible offset de inicio (E01, E02, …) y
    nos quedamos con el que MINIMIZA la suma de |delta runtime| entre
    cada MPLS y su episodio correspondiente. Si ningún offset tiene
    runtime útil (TMDb sin runtimes), fallback al legacy (offset E01).

    Args:
        mpls_durations_min: duraciones de los MPLS en minutos, en el
                            orden que se devolverá.
        tmdb_episodes: episodios TMDb de la temporada (idealmente sorted
                       por episode_number).
        tolerance_min: ±N minutos para considerar match individual 'high'.

    Returns:
        Lista de dicts (mismo orden que mpls_durations_min):
          {
            "suggested_episode_number": int | None,
            "matched_episode": TvEpisode | None (model_dump),
            "confidence": "high" | "low" | "unknown",
            "runtime_delta_min": float | None,
          }
    """
    by_number = {e.episode_number: e for e in tmdb_episodes}
    sorted_eps = sorted(tmdb_episodes, key=lambda e: e.episode_number)

    # Encontrar el offset (ep_num inicial) que minimiza la suma de
    # deltas de runtime entre MPLS consecutivos y episodios consecutivos.
    # Fallback: si ningún episodio tiene runtime, offset = E01 (legacy).
    best_start_ep_num = sorted_eps[0].episode_number if sorted_eps else 1
    if sorted_eps and mpls_durations_min:
        best_score = float("inf")
        max_offsets = max(1, len(sorted_eps) - len(mpls_durations_min) + 1)
        for offset_idx in range(max_offsets):
            total_delta = 0.0
            valid = 0
            for i, dur in enumerate(mpls_durations_min):
                if offset_idx + i >= len(sorted_eps):
                    break
                ep = sorted_eps[offset_idx + i]
                if ep.runtime_minutes > 0:
                    total_delta += abs(dur - ep.runtime_minutes)
                    valid += 1
            if valid > 0:
                # Usamos mean delta para no penalizar offsets con menos
                # episodios disponibles que MPLS (caso último-disco).
                mean_delta = total_delta / valid
                if mean_delta < best_score:
                    best_score = mean_delta
                    best_start_ep_num = sorted_eps[offset_idx].episode_number

    results = []
    for idx, dur in enumerate(mpls_durations_min):
        ep_num = best_start_ep_num + idx
        ep = by_number.get(ep_num)
        if ep is None:
            results.append({
                "suggested_episode_number": ep_num if tmdb_episodes else None,
                "matched_episode": None,
                "confidence": "unknown",
                "runtime_delta_min": None,
            })
            continue
        if ep.runtime_minutes <= 0:
            # TMDb no tiene runtime para este episodio
            results.append({
                "suggested_episode_number": ep_num,
                "matched_episode": ep.model_dump(),
                "confidence": "low",
                "runtime_delta_min": None,
            })
            continue
        delta = abs(dur - ep.runtime_minutes)
        conf = "high" if delta <= tolerance_min else "low"
        results.append({
            "suggested_episode_number": ep_num,
            "matched_episode": ep.model_dump(),
            "confidence": conf,
            "runtime_delta_min": round(delta, 2),
        })
    return results
