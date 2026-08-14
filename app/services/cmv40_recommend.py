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


# ══════════════════════════════════════════════════════════════════════
#  CLASIFICACIÓN DE MOTIVOS DE NO-FACTIBILIDAD
#
#  La columna izquierda del sheet evalúa la conversión del disco a **P8.1
#  single-layer**, que es el objetivo mayoritario de la comunidad. Esta app
#  hace lo contrario: preserva el FEL (workflow `p7_fel` = "demux + merge
#  CMv4.0 + preserva FEL"; el EL solo se descarta en `p7_mel`, donde está
#  vacío). Por eso el motivo más frecuente del sheet — 219 de 326 filas
#  no-factibles, medido sobre la hoja real — NO nos aplica.
#
#  Clasificar el motivo permite distinguir "esto no va a funcionar" de
#  "esto no funciona para una ruta que no usamos".
# ══════════════════════════════════════════════════════════════════════

BLOCKER_P8_ONLY   = "p8_only"
BLOCKER_STATIC_DV = "static_dv"
BLOCKER_GRADING   = "grading"
BLOCKER_NO_BD     = "no_bd"
BLOCKER_OTHER     = "other"
BLOCKER_UNSPEC    = "unspecified"

# Orden irrelevante: se devuelven TODAS las clasificaciones que matcheen.
# Una nota puede mezclar motivos ("static dv + non-cropped rpu l5") y
# perder el relevante por quedarnos con el primero sería el mismo error
# que colapsar las dos filas del sheet en un veredicto único.
_BLOCKER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"can'?t be converted to p8|only be played on a fel"
                r"|baking fel|bake fel", re.I), BLOCKER_P8_ONLY),
    (re.compile(r"static", re.I), BLOCKER_STATIC_DV),
    (re.compile(r"mdl mismatch|different grade|not the same grade"
                r"|brighter|shots are different", re.I), BLOCKER_GRADING),
    (re.compile(r"no bd yet", re.I), BLOCKER_NO_BD),
]

BLOCKER_LABELS: dict[str, str] = {
    BLOCKER_P8_ONLY: "Solo afecta a la conversión a P8.1 — esta app preserva el FEL",
    BLOCKER_STATIC_DV: "Metadata DV estática en la fuente — el upgrade puede no aportar",
    BLOCKER_GRADING: "El master de referencia tiene otro grading (MDL / brillo / cortes)",
    BLOCKER_NO_BD: "Aún no hay Blu-ray — la fila documenta solo la fuente streaming",
    BLOCKER_OTHER: "Motivo específico — ver la nota del sheet",
    BLOCKER_UNSPEC: "El sheet no indica el motivo",
}


def classify_blockers(notes: str) -> list[str]:
    """Clasifica el motivo de no-factibilidad de una fila del sheet.

    Devuelve TODAS las categorías detectadas (una nota puede mezclar
    varias). Lista vacía nunca: sin texto → ['unspecified'], con texto no
    reconocido → ['other'].
    """
    text = (notes or "").strip()
    if not text:
        return [BLOCKER_UNSPEC]
    found = [tag for pat, tag in _BLOCKER_PATTERNS if pat.search(text)]
    return found or [BLOCKER_OTHER]


def blockers_apply_to_fel_workflow(blockers: list[str]) -> bool:
    """True si algún motivo afecta al flujo de esta app.

    `p8_only` es el único que se descarta: describe una limitación de la
    conversión a single-layer que nunca ejecutamos.
    """
    return any(b != BLOCKER_P8_ONLY for b in blockers)


class SheetMatchRow(BaseModel):
    """Una fila del sheet que matchea el título consultado.

    Se devuelven todas (no solo la mejor) porque un título puede estar
    catalogado en varias secciones con veredictos distintos.
    """
    feasible: bool
    section: str = "feasible"         # 'infeasible' | 'feasible' | 'probably_ok'
    dv_source: str = ""
    sync_offset: str = ""
    sync_offset_frames: int | None = None
    comparisons: str = ""
    comparisons_2: str = ""
    notes: str = ""
    blockers: list[str] = []          # vacío en filas factibles
    blocker_labels: list[str] = []
    applies_to_our_workflow: bool = True
    match_confidence: float = 0.0
    # Hyperlinks de la fila
    title_link: str = ""
    sync_link: str = ""
    dv_source_link: str = ""
    comparisons_link: str = ""
    comparisons_2_link: str = ""
    notes_link: str = ""


class CMv40RecommendationResult(BaseModel):
    """Respuesta del endpoint /api/cmv40/recommend."""
    status: str
    """Veredicto para NUESTRO flujo (preserva FEL):
      - 'recommended'   — hay ruta de restore verificada en el sheet
      - 'caveats'       — viable pero con avisos que sí nos afectan, o fila
                          en la sección "Not Sure! / probably ok"
      - 'p8_only_note'  — lo único que el sheet documenta es que no se puede
                          aplanar a P8.1; irrelevante aquí
      - 'not_feasible'  — motivos que sí comprometen el resultado
      - 'unknown'       — el título no está en la hoja
    """
    input_title: str
    input_year: int | None = None
    title_en: str = ""
    match_title: str = ""             # título tal cual aparece en el sheet
    match_year: int | None = None
    match_source: str = "filename"    # 'tmdb' | 'filename'
    match_confidence: float = 0.0
    tmdb_configured: bool = False

    # ── Todas las filas del sheet para este título ────────────────────
    rows: list[SheetMatchRow] = []
    """Filas que aportan algo a ESTA app (ver `_relevant_rows`): las viables
    y las que traen un impedimento real. Cuando hay ruta viable se omiten las
    que solo dicen "no convertible a P8.1" — es una conversión que este
    pipeline no hace, así que mostrarlas sería ruido."""
    rows_omitted: int = 0
    """Cuántas filas del sheet se han omitido de `rows` por no aportar."""
    feasible_row_count: int = 0
    infeasible_row_count: int = 0
    """Contadores sobre TODAS las filas encontradas en la hoja, incluidas las
    omitidas — no sobre `rows`."""
    blockers: list[str] = []
    """Unión de los motivos de las filas no factibles."""
    blockers_apply_to_our_workflow: bool = False
    verdict_label: str = ""           # etiqueta corta para la UI (ES)
    verdict_detail: str = ""          # explicación de una línea (ES)

    # ── Fila primaria: la que gobierna los campos planos ──────────────
    # Se prefiere una fila FACTIBLE si existe. Antes se cogía la de mejor
    # score de similitud y, en empate, ganaba la primera emitida por el
    # parser — que es siempre la de la sección "no factible". Resultado
    # medido: 31 de los 32 títulos presentes en ambas secciones mostraban
    # ❌ aunque el sheet documentara la ruta de restore.
    primary_section: str = ""
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
        # Caso 2: año (1950-2099) como token suelto. Si hay varios (p.ej.
        # 'Blade.Runner.2049.2017.UHD.BluRay'), el año de estreno es el
        # ÚLTIMO — los anteriores forman parte del título. Truncamos en su
        # posición para que el título conserve '2049' (luego _normalize_title
        # lo limpia).
        matches = list(_YEAR_RE.finditer(stem))
        if matches:
            last = matches[-1]
            year = int(last.group(1))
            title = stem[:last.start()]

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
    """Bonus cuando el título corto es el núcleo del largo, medido sobre
    TOKENS significativos (no subcadena de caracteres).

    El corto debe ser subconjunto de las palabras del largo Y cubrir una
    fracción sustancial de ellas (≥60%). Sin el gate de cobertura un título
    corto como 'the ring' matchearía 'the lord of the rings the fellowship of
    the ring' por ser subcadena de caracteres (1 palabra de 4 → falso
    positivo: son films distintos). Casos legítimos que SÍ debe cazar:
        'dark knight' ⊂ 'the dark knight'   (cobertura 2/2)
        'zootopia 2'  ⊂ 'zootopia 2 2025'    (cobertura 2/3)
    """
    ta = [t for t in _tokens(a) if t not in _STOP_WORDS]
    tb = [t for t in _tokens(b) if t not in _STOP_WORDS]
    if not ta or not tb:
        return 0.0
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    sset, lset = set(short), set(long_)
    if not sset.issubset(lset):
        return 0.0
    coverage = len(sset) / len(lset)
    if coverage < 0.6:
        return 0.0
    # Escala por cobertura: cubrir casi todo el largo ≈ mismo título.
    return min(1.0, 0.85 + coverage * 0.15)


def _similarity(a: str, b: str) -> float:
    """Score compuesto: max(seq-matcher, token-set Jaccard, containment).

    Cap anti-falso-positivo: si el token-set Jaccard es 0 (sin overlap real
    de palabras significativas tras quitar stop words) Y no hay containment
    (ningún slug está incluido en el otro), capamos a 0.5 — la similitud
    de SequenceMatcher en ese caso viene solo del esqueleto compartido (ej.
    'the ', espacios, letras sueltas) y NO del contenido. Caso real:
        'the pianist' vs 'the ring' → seq_ratio 0.63 (comparten 'the ' +
        4 letras) → +0.05 año = 0.68 → pasaba el umbral 0.65 incorrectamente.
    Con el cap: 0.5 → no pasa.

    No afecta a casos legítimos:
      - 'deadpool & wolverine' vs 'deadpool wolverine' → ts > 0 (palabras
        compartidas), sin cap.
      - 'dark knight' vs 'the dark knight' → containment > 0, sin cap.
    """
    if not a or not b:
        return 0.0
    seq  = _seq_ratio(a, b)
    ts   = _token_set_ratio(a, b)
    cont = _containment_score(a, b)
    score = max(seq, ts, cont)
    if ts == 0 and cont == 0:
        score = min(score, 0.5)
    return score


def _best_match(title_slug: str, year: int | None,
                rows: list[RecommendationRow]) -> tuple[RecommendationRow | None, float]:
    """Mejor fila y su score. Con varias filas empatadas (el mismo título
    catalogado en dos secciones) prefiere la FACTIBLE — es la que describe
    una ruta que esta app puede ejecutar.
    """
    best: RecommendationRow | None = None
    best_score = 0.0
    for r in rows:
        if year and r.year and abs(r.year - year) > 1:
            continue
        sim = _similarity(title_slug, r.title_normalized)
        if sim > best_score or (sim == best_score and best is not None
                                and r.feasible and not best.feasible):
            best_score = sim
            best = r
    return best, best_score


def _matching_rows(title_slug: str, year: int | None,
                   rows: list[RecommendationRow],
                   threshold: float) -> list[tuple[RecommendationRow, float]]:
    """TODAS las filas que superan el umbral para este slug+año.

    Un título puede estar en la sección "no factible" (no convertible a
    P8.1) y a la vez en la "factible" (bloque CMv4.0 restaurable sobre el
    RPU P7). Quedarse con una sola fila pierde la mitad de la información.
    """
    out: list[tuple[RecommendationRow, float]] = []
    for r in rows:
        if year and r.year and abs(r.year - year) > 1:
            continue
        sim = _similarity(title_slug, r.title_normalized)
        if sim >= threshold:
            out.append((r, sim))
    # Factibles primero, y dentro de cada grupo por score descendente:
    # la fila que gobierna los campos planos es la primera.
    out.sort(key=lambda t: (not t[0].feasible, -t[1]))
    return out


def _to_match_row(row: RecommendationRow, score: float) -> SheetMatchRow:
    """RecommendationRow (fila cruda del sheet) → SheetMatchRow (con el
    motivo ya clasificado para la UI)."""
    blockers = [] if row.feasible else classify_blockers(row.notes)
    return SheetMatchRow(
        feasible=row.feasible,
        section=row.section,
        dv_source=row.dv_source,
        sync_offset=row.sync_offset,
        sync_offset_frames=row.sync_offset_frames,
        comparisons=row.comparisons,
        comparisons_2=row.comparisons_2,
        notes=row.notes,
        blockers=blockers,
        blocker_labels=[BLOCKER_LABELS.get(b, b) for b in blockers],
        applies_to_our_workflow=(
            blockers_apply_to_fel_workflow(blockers) if blockers else True
        ),
        match_confidence=round(score, 3),
        title_link=row.title_link,
        sync_link=row.sync_link,
        dv_source_link=row.dv_source_link,
        comparisons_link=row.comparisons_link,
        comparisons_2_link=row.comparisons_2_link,
        notes_link=row.notes_link,
    )


def _relevant_rows(match_rows: list[SheetMatchRow]) -> list[SheetMatchRow]:
    """Filas que APORTAN algo al usuario de esta app.

    Cuando el sheet documenta una ruta viable, las filas cuyo único
    impedimento es "no convertible a P8.1" se descartan: describen una
    conversión que este pipeline no hace (preserva el FEL), así que
    mostrarlas solo añade información que el usuario tiene que descartar
    mentalmente. Sin fila viable sí se muestran — son lo único que hay.
    """
    if not any(r.feasible for r in match_rows):
        return match_rows
    return [r for r in match_rows if r.feasible or r.applies_to_our_workflow]


def _build_verdict(match_rows: list[SheetMatchRow]) -> tuple[str, str, str]:
    """Veredicto para el flujo de esta app (que preserva el FEL).

    Returns:
        (status, verdict_label, verdict_detail)
    """
    if not match_rows:
        return "unknown", "Sin datos", ""

    feasible_rows = [r for r in match_rows if r.feasible]
    infeasible_rows = [r for r in match_rows if not r.feasible]
    all_blockers = sorted({b for r in infeasible_rows for b in r.blockers})
    relevant = blockers_apply_to_fel_workflow(all_blockers) if all_blockers else False

    if feasible_rows:
        only_probably_ok = all(r.section == "probably_ok" for r in feasible_rows)
        if relevant:
            labels = [BLOCKER_LABELS.get(b, b) for b in all_blockers
                      if b != BLOCKER_P8_ONLY]
            return ("caveats", "Viable con avisos",
                    "El sheet documenta la ruta de restore CMv4.0, pero hay avisos: "
                    + " · ".join(labels))
        if only_probably_ok:
            return ("caveats", "Probablemente OK",
                    "El sheet lo cataloga como \"Not Sure!\" — viable pero sin "
                    "verificación completa. Conviene revisar la sincronización a mano.")
        # Si además hay filas cuyo único impedimento es la conversión a P8.1,
        # no se mencionan: hablan de una ruta que esta app no ejecuta y su
        # única aportación sería ruido.
        return ("recommended", "Factible",
                "El sheet confirma que el bloque CMv4.0 se puede restaurar sobre el RPU.")

    # Solo filas no factibles
    if all_blockers and not relevant:
        return ("p8_only_note", "No convertible a P8.1",
                "El único impedimento que documenta el sheet es aplanar el disco a "
                "P8.1 single-layer — algo que esta app no hace. No hay fila de restore "
                "verificada para este título, así que la sincronización no está "
                "comprobada por la comunidad.")
    labels = [BLOCKER_LABELS.get(b, b) for b in all_blockers]
    return ("not_feasible", "No recomendado",
            "Motivos que sí afectan al resultado: " + " · ".join(labels))


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

    # El slug ganador puede matchear VARIAS filas del sheet (mismo título
    # catalogado en dos secciones). Recogemos todas con el mismo umbral y
    # construimos el veredicto sobre el conjunto, no sobre una fila.
    slug_win = _normalize_title(best_row.title_raw)
    matches = _matching_rows(slug_win, best_year_for_threshold, rows, threshold)
    if not matches:                      # defensivo: al menos la fila ganadora
        matches = [(best_row, best_score)]
    # El veredicto y los contadores se calculan sobre TODAS las filas
    # encontradas; `rows` solo lleva las que aportan algo al usuario.
    all_rows = [_to_match_row(r, s) for r, s in matches]
    result.feasible_row_count = sum(1 for r in all_rows if r.feasible)
    result.infeasible_row_count = len(all_rows) - result.feasible_row_count
    result.blockers = sorted({b for r in all_rows for b in r.blockers})
    result.blockers_apply_to_our_workflow = (
        blockers_apply_to_fel_workflow(result.blockers) if result.blockers else False
    )

    status, label, detail = _build_verdict(all_rows)
    result.status = status
    result.verdict_label = label
    result.verdict_detail = detail

    result.rows = _relevant_rows(all_rows)
    result.rows_omitted = len(all_rows) - len(result.rows)

    # Campos planos ← fila primaria (factible si existe; _matching_rows ya
    # las ordenó con las factibles delante).
    primary = result.rows[0]
    primary_row = matches[0][0]
    result.match_title = primary_row.title_raw
    result.match_year = primary_row.year
    result.match_confidence = round(matches[0][1], 3)
    result.match_source = best_source
    result.primary_section = primary.section
    result.feasible = primary.feasible
    result.dv_source = primary.dv_source
    result.sync_offset = primary.sync_offset
    result.sync_offset_frames = primary.sync_offset_frames
    result.notes = primary.notes
    result.comparisons = primary.comparisons
    result.comparisons_2 = primary.comparisons_2
    # Hyperlinks de la hoja (solo llenos si vinieron vía XLSX+openpyxl o Sheets API v4)
    result.title_link = primary.title_link
    result.sync_link = primary.sync_link
    result.dv_source_link = primary.dv_source_link
    result.comparisons_link = primary.comparisons_link
    result.comparisons_2_link = primary.comparisons_2_link
    result.notes_link = primary.notes_link
    return result
