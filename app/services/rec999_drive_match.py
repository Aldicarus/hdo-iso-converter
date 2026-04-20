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

import re

# Heurísticas de clasificación por nombre de fichero del repo DoviTools.
# La clasificación definitiva se hace tras descarga con `dovi_tool info`
# en Fase B, pero estas predicciones UX dan al usuario señalización
# inmediata sobre qué pipeline esperar antes de crear el proyecto.
#
# Reglas clave:
# - Separador `[\s_\-\.]` (incluye PUNTO): el repo usa mucho `P7.FEL` con dots.
# - MEL se evalúa ANTES que FEL/catch-all: nombres tipo "MEL P7 (cmv4.0)"
#   caerían en la regla genérica "cmv4.0 restored" y se mal-clasificarían como FEL.
_SEP   = r"[\s_\-\.]"
# Lookbehind/lookahead para "límites" que NO dependan de \b. `_` es word char
# en regex, por lo que `_P7 FEL_` no dispara \b; estos asertores sí.
_BOUND = r"(?<![A-Za-z0-9])"
_END   = r"(?![A-Za-z0-9])"
# Alias: "profile 7" equivale a "P7" (nombres tipo "(profile 7 FEL)")
_P7    = rf"(?:p7|profile{_SEP}+7)"

_BIN_PATTERNS = [
    # ── MEL primero (evita falsos positivos en la regla genérica cmv4.0) ──
    # P7 MEL ... cmv4 (ambos órdenes)
    (re.compile(rf"{_P7}{_SEP}*mel[\s\S]*?cmv{_SEP}?4",  re.I), "trusted_p7_mel_final"),
    (re.compile(rf"cmv{_SEP}?4[\s\S]*?{_P7}{_SEP}*mel",  re.I), "trusted_p7_mel_final"),
    # MEL P7 (orden invertido: "retail MEL P7 (cmv4.0 restored)")
    (re.compile(rf"mel{_SEP}+{_P7}[\s\S]*?cmv{_SEP}?4",  re.I), "trusted_p7_mel_final"),
    (re.compile(rf"cmv{_SEP}?4[\s\S]*?mel{_SEP}+{_P7}",  re.I), "trusted_p7_mel_final"),
    # ── FEL con cmv4 (ambos órdenes) ─────────────────────────────────────
    (re.compile(rf"{_P7}{_SEP}*fel[\s\S]*?cmv{_SEP}?4",  re.I), "trusted_p7_fel_final"),
    (re.compile(rf"cmv{_SEP}?4[\s\S]*?{_P7}{_SEP}*fel",  re.I), "trusted_p7_fel_final"),
    (re.compile(rf"fel{_SEP}+{_P7}[\s\S]*?cmv{_SEP}?4",  re.I), "trusted_p7_fel_final"),
    (re.compile(rf"cmv{_SEP}?4[\s\S]*?fel{_SEP}+{_P7}",  re.I), "trusted_p7_fel_final"),
    # ── P5 → P8 transfer (rama B) ────────────────────────────────────────
    (re.compile(rf"p5{_SEP}*(?:to|->|→|=>|–|—){_SEP}*p8", re.I), "trusted_p8_source"),
    # ── Catch-all "cmv4.0 added/restored/baked" sin profile → asume FEL ──
    # (MEL ya se detectó arriba, así que aquí es seguro inferir FEL)
    (re.compile(rf"cmv{_SEP}?4(?:\.0)?{_SEP}*(added|restored|baked)", re.I),
     "trusted_p7_fel_final"),
    # ── Orphan profile tags sin cmv4: P7 MEL / MEL P7 / P7 FEL / FEL P7 ──
    #    Asumimos CMv4.0 por convención del repo DoviTools; los gates de
    #    Fase B lo verifican en runtime. _BOUND/_END evitan "felpa", "melt".
    (re.compile(rf"{_BOUND}{_P7}{_SEP}*mel{_END}", re.I), "trusted_p7_mel_final"),
    (re.compile(rf"{_BOUND}mel{_SEP}+{_P7}{_END}", re.I), "trusted_p7_mel_final"),
    (re.compile(rf"{_BOUND}{_P7}{_SEP}*fel{_END}", re.I), "trusted_p7_fel_final"),
    (re.compile(rf"{_BOUND}fel{_SEP}+{_P7}{_END}", re.I), "trusted_p7_fel_final"),
]

# Artefactos que NO son targets utilizables — son salidas de análisis o
# working files, no RPUs de consumo. Se filtran en rank_candidates y se
# muestran aparte en el survey.
# OJO: NO incluimos `_FlagRPU` — ese sufijo indica que el RPU tiene el flag
# CMv4.0 activado (es un target VÁLIDO, no un artefacto).
_NOT_TARGET_PATTERNS = [
    # "CM_Analyze" / "CM Analyze" / ".CM_Analyze" / "cm_analyze" — separador flex
    re.compile(rf"{_SEP}cm{_SEP}*analyze", re.I),
    # "_Resolve" / " resolve" / ".Resolve" — al final o seguido de sep
    re.compile(rf"{_SEP}resolve(?:{_SEP}|\.bin$|$)", re.I),
]


def predict_bin_type(filename: str) -> str:
    """Devuelve el target_type predicho a partir del nombre de fichero.
    Valores: 'trusted_p7_fel_final', 'trusted_p7_mel_final',
    'trusted_p8_source' o 'unknown' si no coincide con ningún patrón.
    """
    if not filename:
        return "unknown"
    for pat, typ in _BIN_PATTERNS:
        if pat.search(filename):
            return typ
    return "unknown"


def is_not_target(filename: str) -> bool:
    """True si el nombre sugiere un artefacto (análisis/working file) que
    no debería usarse como target. Ejemplos: *_CM_Analyze.bin, *_Resolve.bin.
    """
    if not filename:
        return False
    return any(p.search(filename) for p in _NOT_TARGET_PATTERNS)


# ── Procedencia: calidad del CMv4.0 (ortogonal a target_type) ────────
# Retail  = RPU extraído de un master oficial (iTunes, MA, DSNP, MAX…).
#           Representa el creative intent original del colorista.
# Generated = RPU sintetizado desde HDR10 via dovi_tool generate /
#             cm_analyze / madVR. Tone mapping derivado algorítmicamente
#             de estadísticas de luminancia, no decisión artística.
#             El tuning (T1/T3/…) afecta el look en highlights.
# El pipeline técnico funciona igual con ambos; es señal UX de calidad.
_RETAIL_PATTERNS = [
    re.compile(r"(?<![A-Za-z])retail(?![A-Za-z])", re.I),
    re.compile(rf"cmv{_SEP}?4(?:\.0)?{_SEP}*(added|restored|baked)", re.I),
]
_GENERATED_PATTERNS = [
    re.compile(r"(?<![A-Za-z])generated(?![A-Za-z])", re.I),
]


def predict_provenance(filename: str) -> str:
    """Devuelve la procedencia del CMv4.0 por nombre:
      'retail'    — RPU extraído de master streaming oficial (creative intent)
      'generated' — RPU sintetizado desde HDR10 (algorítmico, depende de tuning)
      ''          — no deducible por nombre
    Retail gana si están los dos tokens presentes (raro pero posible).
    """
    if not filename:
        return ""
    for p in _RETAIL_PATTERNS:
        if p.search(filename):
            return "retail"
    for p in _GENERATED_PATTERNS:
        if p.search(filename):
            return "generated"
    return ""

MIN_SCORE_STRICT = 0.65   # con año exacto
MIN_SCORE_NO_YEAR = 0.78  # sin año fiable


class DriveCandidate(BaseModel):
    file: DriveFile
    score: float
    year_in_filename: int | None = None
    provenance: str = ""  # 'retail' | 'generated' | '' (ver predict_provenance)


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
        # Saltar artefactos de análisis / working files — no son targets válidos
        if is_not_target(f.name):
            continue
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
        # Bonus suave por procedencia retail: cuando varios candidatos del
        # mismo título empatan en título/año, preferimos retail (CMv4.0
        # auténtico del master oficial) sobre generated (sintético).
        prov = predict_provenance(f.name)
        if prov == "retail":
            best = min(1.0, best + 0.03)
        # Umbral adaptativo
        min_score = MIN_SCORE_STRICT if (year and fn_year) else MIN_SCORE_NO_YEAR
        if best < min_score:
            continue
        results.append(DriveCandidate(
            file=f, score=round(best, 3), year_in_filename=fn_year,
            provenance=prov,
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
