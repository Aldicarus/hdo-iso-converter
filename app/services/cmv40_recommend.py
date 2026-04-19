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
    get_cache_status,
    get_recommendations,
)
from services.tmdb import is_configured as tmdb_is_configured
from services.tmdb import search_movies  # multi-candidate

_logger = logging.getLogger(__name__)

# Umbrales de similitud: distintos según coincidencia de año, porque "año
# exacto + título parecido" es mucho más fiable que "año sin match + título
# parecido".
THRESHOLD_YEAR_EXACT = 0.72
THRESHOLD_YEAR_NEAR  = 0.82  # ±1 año (remaster/re-release)
THRESHOLD_NO_YEAR    = 0.88

# Stop words irrelevantes para matching (se eliminan en token-set)
_STOP_WORDS = {"the", "a", "an", "el", "la", "los", "las", "de", "of", "and", "y"}

# Normalización de numerales romanos (muy común en secuelas)
_ROMAN_MAP = {
    "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
    "vii": "7", "viii": "8", "ix": "9", "x": "10",
    "xi": "11", "xii": "12", "xiii": "13",
}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _tokens(slug: str) -> list[str]:
    """Tokeniza y normaliza romanos → dígitos."""
    if not slug:
        return []
    toks = _strip_accents(slug).split()
    return [_ROMAN_MAP.get(t, t) for t in toks]


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
    comparisons: str = ""             # primera sub-columna Comparisons
    comparisons_2: str = ""           # segunda sub-columna Comparisons
    sheet_rows_loaded: int = 0
    sheet_source: str = "none"        # 'api' | 'csv' | 'disk' | 'none'
    sheets_api_error: str = ""        # motivo si API v4 falló (para UI)
    google_configured: bool = False
    # Hyperlinks de la celda correspondiente del sheet (solo si el sheet se
    # leyó via Sheets API v4; requiere Google API key con Sheets API habilitada)
    title_link: str = ""
    sync_link: str = ""
    dv_source_link: str = ""
    comparisons_link: str = ""
    comparisons_2_link: str = ""
    notes_link: str = ""


def parse_mkv_filename(filename: str) -> tuple[str, int | None]:
    """Extrae título y año del filename, truncando tags (release group,
    codec, HDR, DV FEL, etc.) que casi siempre aparecen DESPUÉS del año.

    Ejemplos::
        'Zootrópolis 2 (2025) [DV FEL] [Audio DCP].mkv' → ('Zootrópolis 2', 2025)
        'Zootopia.2.2025.UHD.BluRay.2160p.HDR.x265.mkv' → ('Zootopia 2', 2025)
        'The.Dark.Knight.2008.UHD.DV.mkv'               → ('The Dark Knight', 2008)
        'Deadpool.2016.Full.UHD.mkv'                     → ('Deadpool', 2016)
        'Dark Knight (2008).mkv'                         → ('Dark Knight', 2008)
    """
    stem = Path(filename).stem
    year: int | None = None
    title = stem

    # Caso 1: "Title (YYYY) …"
    m = re.search(r"\((\d{4})\)", stem)
    if m:
        year = int(m.group(1))
        title = stem[:m.start()]
    else:
        # Caso 2: primer año (1950-2099) como token suelto — truncar TODO a partir de él
        m2 = _YEAR_RE.search(stem)
        if m2:
            year = int(m2.group(1))
            title = stem[:m2.start()]

    # Quitar tags [...] residuales
    title = re.sub(r"\[[^\]]+\]", " ", title)
    # Separadores comunes → espacio
    title = re.sub(r"[\._\-]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"[\-,.\s]+$", "", title).strip()
    return title, year


def _token_set_ratio(a: str, b: str) -> float:
    """Jaccard sobre tokens normalizados sin stop-words. Insensible al
    orden y a palabras comodín (The/A/El/La…)."""
    ta = set(_tokens(a)) - _STOP_WORDS
    tb = set(_tokens(b)) - _STOP_WORDS
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    return len(inter) / len(union)


def _seq_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _strip_accents(a), _strip_accents(b)).ratio()


def _containment_score(a: str, b: str) -> float:
    """Si un slug es sub-string del otro, bonus fuerte. Maneja casos
    tipo 'dark knight' ⊂ 'the dark knight' o 'zootopia 2' ⊂ 'zootopia 2 2025'."""
    if not a or not b:
        return 0.0
    sa = _strip_accents(a)
    sb = _strip_accents(b)
    short, long_ = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if len(short) < 3:
        return 0.0
    if short in long_:
        # Escala por cuán "dominante" es el short sobre el long
        return min(1.0, 0.85 + (len(short) / len(long_)) * 0.15)
    return 0.0


def _similarity(a: str, b: str) -> float:
    """Score compuesto: max(seq-matcher, token-set Jaccard, containment)."""
    if not a or not b:
        return 0.0
    return max(_seq_ratio(a, b), _token_set_ratio(a, b), _containment_score(a, b))


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
    from services.settings_store import get_google_api_key

    rows = await get_recommendations()
    sheet_status = get_cache_status()
    result = CMv40RecommendationResult(
        status="unknown",
        input_title=input_title,
        input_year=input_year,
        sheet_rows_loaded=len(rows),
        sheet_source=sheet_status.get("source", "none"),
        sheets_api_error=sheet_status.get("sheets_api_error", ""),
        google_configured=bool(get_google_api_key()),
        tmdb_configured=tmdb_is_configured(),
    )

    # Construye la lista de (slug, year, source) candidatos a probar
    # contra el sheet. Siempre incluye el título ES crudo; si TMDb está
    # configurado, añade sus top-N candidatos (con título EN resuelto).
    # Así cubrimos:
    #   - Título ya en inglés (Deadpool → Deadpool)
    #   - Traducción TMDb (Jungla de cristal → Die Hard)
    #   - Ambigüedad con múltiples TMDb matches (p.ej. remakes)
    candidates: list[tuple[str, int | None, str]] = []
    slug_es = _normalize_title(input_title)
    candidates.append((slug_es, input_year, "filename"))

    primary_title_en = input_title
    primary_year = input_year
    if tmdb_is_configured():
        tmdb_matches = await search_movies(input_title, input_year, limit=5)
        for i, tm in enumerate(tmdb_matches):
            cand_slug = _normalize_title(tm.title_en)
            cand_year = tm.year or input_year
            candidates.append((cand_slug, cand_year, "tmdb" if i == 0 else f"tmdb#{i+1}"))
        if tmdb_matches:
            primary_title_en = tmdb_matches[0].title_en
            primary_year = tmdb_matches[0].year or input_year
    result.title_en = primary_title_en

    if not rows:
        result.match_source = "filename"
        return result

    # Prueba cada candidato; nos quedamos con el de mejor score bruto.
    # El dedup usa (slug, year) porque TMDb devuelve varias entradas con el
    # mismo título y distinto año (El Rey León 1994 y 2019, p.ej.); si solo
    # deduplicamos por slug perdemos todos menos el primero.
    best_row: RecommendationRow | None = None
    best_score: float = 0.0
    best_source: str = "filename"
    best_year_for_threshold: int | None = input_year
    seen: set[tuple[str, int | None]] = set()
    for slug, cand_year, source in candidates:
        key = (slug, cand_year)
        if not slug or key in seen:
            continue
        seen.add(key)
        row, score = _best_match(slug, cand_year, rows)
        if row and score > best_score:
            best_row = row
            best_score = score
            best_source = source
            # El threshold se elige contra el año del propio candidato que
            # ganó, no contra el del primer TMDb match (podrían ser films
            # distintos con mismo título).
            best_year_for_threshold = cand_year

    threshold = _threshold_for(best_row, best_year_for_threshold)
    if not best_row or best_score < threshold:
        result.match_source = best_source
        return result

    result.match_title = best_row.title_raw
    result.match_year = best_row.year
    result.match_confidence = round(best_score, 3)
    result.match_source = best_source
    result.feasible = best_row.feasible
    result.dv_source = best_row.dv_source
    result.sync_offset = best_row.sync_offset
    result.sync_offset_frames = best_row.sync_offset_frames
    result.notes = best_row.notes
    result.comparisons = best_row.comparisons
    result.comparisons_2 = best_row.comparisons_2
    # Hyperlinks de la hoja (solo llenos si vinieron vía XLSX+openpyxl o Sheets API v4)
    result.title_link = best_row.title_link
    result.sync_link = best_row.sync_link
    result.dv_source_link = best_row.dv_source_link
    result.comparisons_link = best_row.comparisons_link
    result.comparisons_2_link = best_row.comparisons_2_link
    result.notes_link = best_row.notes_link
    result.status = "recommended" if best_row.feasible else "not_feasible"
    return result
