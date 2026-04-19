"""
rec999_drive_match.py — Matching de películas con los .bin del repositorio
de REC_9999 en Drive. Reusa la normalización y el fuzzy compuesto de
cmv40_recommend.
"""
from __future__ import annotations

import logging
from pydantic import BaseModel

from services.cmv40_recommend import (
    _normalize_title,
    _similarity,
    _tokens,
    _STOP_WORDS,
)
from services.rec999_sheet import _extract_year
from services.rec999_drive import DriveFile, list_bin_files

_logger = logging.getLogger(__name__)

MIN_SCORE_STRICT = 0.65   # con año exacto
MIN_SCORE_NO_YEAR = 0.78  # sin año fiable


class DriveCandidate(BaseModel):
    file: DriveFile
    score: float
    year_in_filename: int | None = None


def _candidate_year(name: str) -> int | None:
    return _extract_year(name)


def _filename_slug(name: str) -> str:
    """Quita la extensión y normaliza."""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return _normalize_title(stem)


def rank_candidates(title_en: str, title_es: str, year: int | None,
                    files: list[DriveFile], limit: int = 8) -> list[DriveCandidate]:
    """Devuelve los mejores candidatos ordenados por score descendente."""
    if not files:
        return []
    slug_en = _normalize_title(title_en) if title_en else ""
    slug_es = _normalize_title(title_es) if title_es else ""
    slugs = [s for s in {slug_en, slug_es} if s]
    if not slugs:
        return []

    results: list[DriveCandidate] = []
    for f in files:
        fn_slug = _filename_slug(f.name)
        if not fn_slug:
            continue
        fn_year = _candidate_year(f.name)
        # Año: si ambos presentes, exige proximidad (±1)
        if year and fn_year and abs(fn_year - year) > 1:
            continue
        best = 0.0
        for s in slugs:
            sim = _similarity(s, fn_slug)
            if sim > best:
                best = sim
        # Bonus si año coincide exactamente
        if year and fn_year and fn_year == year:
            best = min(1.0, best + 0.05)
        # Umbral adaptativo
        min_score = MIN_SCORE_STRICT if (year and fn_year) else MIN_SCORE_NO_YEAR
        if best < min_score:
            continue
        results.append(DriveCandidate(
            file=f, score=round(best, 3), year_in_filename=fn_year,
        ))

    results.sort(key=lambda c: c.score, reverse=True)
    return results[:limit]


async def find_candidates(title_en: str, title_es: str,
                          year: int | None, limit: int = 8) -> list[DriveCandidate]:
    try:
        files = await list_bin_files()
    except PermissionError:
        raise
    return rank_candidates(title_en, title_es, year, files, limit=limit)
