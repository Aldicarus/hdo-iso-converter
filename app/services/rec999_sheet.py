"""
rec999_sheet.py — Lectura live del Google Sheet de R3S3T_9999 con
información de qué películas son convertibles a CMv4.0 y cuáles no.

El sheet tiene tres secciones por fila:
  - Izquierda (col 0-4): películas NO factibles (BD-FEL exclusivas o con
    problemas detallados en la columna Notes)
  - Derecha  (col 6-11): películas convertibles via workflow 2-3
    (transferencia del bloque CMv4.0 desde un WEB-DL al RPU P7 del BD)
  - Muy a la derecha: encoder groups a evitar (ignorado aquí)

La lectura se hace live sin API key usando el endpoint público
`/export?format=csv&gid=...` de Google Sheets (el sheet tiene permisos
"cualquiera con el enlace puede ver"). Caché en memoria + disco como
fallback.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
import time
from pathlib import Path

import httpx
from pydantic import BaseModel

_logger = logging.getLogger(__name__)

# Defaults: sheet de R3S3T_9999 pestaña "GRADE CHECK"
DEFAULT_SHEET_ID = "15i0a84uiBtWiHZ5CXZZ7wygLFXwYOd84"
DEFAULT_SHEET_GID = "828864432"

SHEET_ID  = os.environ.get("CMV40_SHEET_ID", DEFAULT_SHEET_ID)
SHEET_GID = os.environ.get("CMV40_SHEET_GID", DEFAULT_SHEET_GID)
SHEET_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    f"/export?format=csv&gid={SHEET_GID}"
)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CACHE_PATH = CONFIG_DIR / "rec999_sheet_cache.csv"
CACHE_TTL_SECONDS = 3600  # 1h

# Año entre 1950 y 2099 — elimina falsos positivos con números random
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20\d{2})\b")

# Frases que identifican filas de cabecera o instrucciones (no películas)
_NON_MOVIE_MARKERS = (
    "incorrect p8", "when fel is not", "generate dolby", "restore cmv",
    "how to make proper", "dolby vision test", "hdr10", "fel vs",
    "dv metadata", "fel bluray", "best player", "questions",
    "aspect ratio", "encoder groups",
)


class RecommendationRow(BaseModel):
    """Una fila parseada del sheet."""
    feasible: bool
    title_raw: str
    title_normalized: str
    year: int | None = None
    dv_source: str = ""           # 'BD FEL', 'iTunes', 'DSNP', 'MA'…
    sync_offset: str = ""         # '(+24)', '0', '(-8 T280/B280)'…
    sync_offset_frames: int | None = None
    comparisons: str = ""         # 'HDR COMP', 'plot', 'Nits', 'L1'…
    notes: str = ""               # razón detallada / workflow


# Caché en memoria
_cache_rows: list[RecommendationRow] | None = None
_cache_fetched_at: float = 0.0


def _normalize_title(raw: str) -> str:
    """Slug: minúsculas, sin puntuación, sin año, espacios colapsados."""
    s = raw.lower()
    s = re.sub(r"\[[^\]]+\]", " ", s)
    s = re.sub(r"[\._\-/:]+", " ", s)
    s = _YEAR_RE.sub("", s)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_year(raw: str) -> int | None:
    m = _YEAR_RE.search(raw)
    return int(m.group(1)) if m else None


def _parse_offset_frames(s: str) -> int | None:
    """De '(+24)', '(-8 T280/B280)', '0' saca el primer entero con signo."""
    if not s:
        return None
    m = re.search(r"([+-]?\d+)", s)
    return int(m.group(1)) if m else None


def _is_instructional(title: str) -> bool:
    low = title.lower()
    return any(marker in low for marker in _NON_MOVIE_MARKERS)


def _parse_row(row: list[str]) -> list[RecommendationRow]:
    """Devuelve 0, 1 o 2 RecommendationRow por fila del CSV."""
    out: list[RecommendationRow] = []
    if len(row) < 5:
        return out

    def col(i: int) -> str:
        return (row[i] if i < len(row) else "").strip()

    # Izquierda: NO factible (cols 0=title, 1=dv_source, 2=comparisons, 4=notes)
    left_title = col(0)
    if left_title and _extract_year(left_title) and not _is_instructional(left_title):
        out.append(RecommendationRow(
            feasible=False,
            title_raw=left_title,
            title_normalized=_normalize_title(left_title),
            year=_extract_year(left_title),
            dv_source=col(1),
            comparisons=col(2),
            notes=col(4),
        ))

    # Derecha: factible (cols 6=title, 7=sync, 8=dv_source, 9=comparisons, 11=notes)
    right_title = col(6)
    if right_title and _extract_year(right_title) and right_title.upper() != "OLD":
        out.append(RecommendationRow(
            feasible=True,
            title_raw=right_title,
            title_normalized=_normalize_title(right_title),
            year=_extract_year(right_title),
            sync_offset=col(7),
            sync_offset_frames=_parse_offset_frames(col(7)),
            dv_source=col(8),
            comparisons=col(9),
            notes=col(11),
        ))

    return out


def parse_csv_text(csv_text: str) -> list[RecommendationRow]:
    """Parsea el CSV completo y devuelve todas las filas válidas."""
    rows: list[RecommendationRow] = []
    reader = csv.reader(io.StringIO(csv_text))
    for i, row in enumerate(reader):
        if i == 0:  # header
            continue
        rows.extend(_parse_row(row))
    return rows


async def _fetch_csv() -> str:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(SHEET_URL)
        resp.raise_for_status()
        return resp.text


async def get_recommendations(force_refresh: bool = False) -> list[RecommendationRow]:
    """Devuelve las filas parseadas. Live fetch con TTL + fallback a disco."""
    global _cache_rows, _cache_fetched_at

    now = time.time()
    if (not force_refresh and _cache_rows is not None
            and now - _cache_fetched_at < CACHE_TTL_SECONDS):
        return _cache_rows

    csv_text: str | None = None
    live_ok = False
    try:
        csv_text = await _fetch_csv()
        live_ok = True
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(csv_text, encoding="utf-8")
        except OSError as e:
            _logger.warning("No pude persistir cache del sheet: %s", e)
    except Exception as e:
        _logger.warning("Fetch live del sheet REC_9999 falló: %s", e)

    if csv_text is None and CACHE_PATH.exists():
        try:
            csv_text = CACHE_PATH.read_text(encoding="utf-8")
        except OSError:
            csv_text = None

    if not csv_text:
        _logger.error("Sin datos del sheet REC_9999 (ni live ni cache)")
        _cache_rows = []
        _cache_fetched_at = now
        return []

    _cache_rows = parse_csv_text(csv_text)
    _cache_fetched_at = now
    _logger.info("REC_9999 sheet: %d filas parseadas (live=%s)",
                 len(_cache_rows), live_ok)
    return _cache_rows
