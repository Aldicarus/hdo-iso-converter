"""
cmv40_recommend.py — Orquestador de la recomendación CMv4.0.

Flujo:
  1. Parseo del filename → (título_es, año)
  2. TMDb opcional: título_es → título_en
  3. Match contra el sheet live de REC_9999 por slug + año
  4. Devuelve estado + detalle (incluyendo motivo completo si NO factible)
"""
from __future__ import annotations

import logging
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import BaseModel

from services.rec999_sheet import (
    RecommendationRow,
    _YEAR_RE,
    _extract_year,
    _normalize_title,
    get_recommendations,
)
from services.tmdb import is_configured as tmdb_is_configured
from services.tmdb import search_movie

_logger = logging.getLogger(__name__)

# Umbrales de similitud: distintos según coincidencia de año, porque "año
# exacto + título parecido" es mucho más fiable que "año sin match + título
# parecido".
THRESHOLD_YEAR_EXACT = 0.72
THRESHOLD_YEAR_NEAR  = 0.82  # ±1 año (remaster/re-release)
THRESHOLD_NO_YEAR    = 0.88


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


class CMv40RecommendationResult(BaseModel):
    """Respuesta del endpoint /api/cmv40/recommend."""
    status: str                       # 'recommended' | 'not_feasible' | 'unknown'
    input_title: str
    input_year: int | None = None
    title_en: str = ""
    match_title: str = ""             # título tal cual aparece en el sheet
    match_year: int | None = None
    match_source: str = "filename"    # 'tmdb' | 'filename'
    match_confidence: float = 0.0
    tmdb_configured: bool = False
    feasible: bool | None = None
    dv_source: str = ""               # 'BD FEL', 'iTunes', 'DSNP'…
    sync_offset: str = ""             # '(+24)', '(-8 T280/B280)'…
    sync_offset_frames: int | None = None
    notes: str = ""                   # motivo/detalle del sheet
    comparisons: str = ""             # 'HDR COMP', 'plot', 'L1'…
    sheet_rows_loaded: int = 0


def parse_mkv_filename(filename: str) -> tuple[str, int | None]:
    """'Zootrópolis 2 (2025) [DV FEL] [Audio DCP].mkv' → ('Zootrópolis 2', 2025)."""
    stem = Path(filename).stem
    m = re.search(r"\((\d{4})\)", stem)
    year = int(m.group(1)) if m else None
    title = re.sub(r"\(\d{4}\)", "", stem)
    title = re.sub(r"\[[^\]]+\]", "", title)
    title = re.sub(r"[_]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    if not year:
        year = _extract_year(title)
        if year:
            title = _YEAR_RE.sub("", title).strip()
    # Quitar trailing dash/coma si quedó
    title = re.sub(r"[\-,.\s]+$", "", title).strip()
    return title, year


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _strip_accents(a), _strip_accents(b)).ratio()


def _best_match(title_slug: str, year: int | None,
                rows: list[RecommendationRow]) -> tuple[RecommendationRow | None, float]:
    best: RecommendationRow | None = None
    best_score = 0.0
    for r in rows:
        if year and r.year and abs(r.year - year) > 1:
            continue
        sim = _similarity(title_slug, r.title_normalized)
        if sim > best_score:
            best_score = sim
            best = r
    return best, best_score


def _threshold_for(best: RecommendationRow | None, year: int | None) -> float:
    """Umbral adaptativo según coincidencia de año."""
    if not best or not best.year or not year:
        return THRESHOLD_NO_YEAR
    if best.year == year:
        return THRESHOLD_YEAR_EXACT
    return THRESHOLD_YEAR_NEAR


async def recommend(input_title: str,
                    input_year: int | None = None) -> CMv40RecommendationResult:
    rows = await get_recommendations()
    result = CMv40RecommendationResult(
        status="unknown",
        input_title=input_title,
        input_year=input_year,
        sheet_rows_loaded=len(rows),
        tmdb_configured=tmdb_is_configured(),
    )

    # 1. Traducción ES→EN (opcional)
    title_en = input_title
    year = input_year
    match_source = "filename"
    tmdb = await search_movie(input_title, input_year)
    if tmdb:
        title_en = tmdb.title_en
        year = tmdb.year or input_year
        match_source = "tmdb"
    result.title_en = title_en
    result.match_source = match_source

    if not rows:
        return result

    # 2. Matching principal (slug EN)
    slug_en = _normalize_title(title_en)
    best, score = _best_match(slug_en, year, rows)

    # 3. Si el slug EN no matchea bien, probamos con el slug ES (muchos
    #    títulos coinciden directo o la traducción TMDb no añade nada
    #    porque no hay key)
    slug_es = _normalize_title(input_title)
    if slug_es != slug_en:
        best_es, score_es = _best_match(slug_es, year, rows)
        if best_es and score_es > score:
            best, score = best_es, score_es

    threshold = _threshold_for(best, year)
    if not best or score < threshold:
        return result

    result.match_title = best.title_raw
    result.match_year = best.year
    result.match_confidence = round(score, 3)
    result.feasible = best.feasible
    result.dv_source = best.dv_source
    result.sync_offset = best.sync_offset
    result.sync_offset_frames = best.sync_offset_frames
    result.notes = best.notes
    result.comparisons = best.comparisons
    result.status = "recommended" if best.feasible else "not_feasible"
    return result
