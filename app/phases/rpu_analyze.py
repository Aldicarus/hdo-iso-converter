"""Análisis profundo del RPU (Dolby Vision) — combos L2/L8 + clasificación L8.

Bloque 1 del modelo Keep/Drop-in/Merge. Ejecuta `dovi_tool export -d all`
sobre un fichero .bin de RPU, parsea la lista de frames, y agrega:

  - L2 combos únicos (CMv2.9 trims) con count por shot
  - L8 combos únicos (CMv4.0 trims) con count por shot
  - Stats: % de frames con L8 neutro, presencia de mid_contrast/clip_trim
  - Clasificación del L8: "real" | "default" | "indeterminate"

La clasificación L8 alimenta la decisión del pre-flight: si el bin es
"default" (sintético), recomendamos Keep — saltarse Fase A entera.

Llamado desde:
  - main.py preflight task (sobre RPU_target.bin descargado)
  - cmv40_pipeline.run_phase_a_analyze_source (sobre RPU_source.bin)
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from models import L2Combo, L8Combo

logger = logging.getLogger("hdo.rpu_analyze")

DOVI_TOOL_BIN = "dovi_tool"

# Umbrales calibrados con los 4 bins reales analizados empíricamente
# (Spider-Man, Karate Kid, 28 después, Smashing Machine — todos con
# combos>=64 y trabajado >=70%). Bin "default" se reconoce porque:
#   - tiene 1-2 combos únicos (todo igual en todos los frames), O
#   - >=95% de los frames tienen los trims a neutro (2048)
# Mantenidos conservadores: si quedamos en medio → "indeterminate" y
# permitimos avanzar (decide después con info del source).
L8_REAL_MIN_UNIQUE_COMBOS = 10
L8_REAL_MAX_NEUTRAL_PCT = 0.95
L8_DEFAULT_MAX_UNIQUE_COMBOS = 2
L8_DEFAULT_MIN_NEUTRAL_PCT = 0.95


@dataclass
class RpuAnalysis:
    """Resultado del análisis de un RPU."""
    total_frames: int = 0
    frames_with_cmv40: int = 0
    scene_cuts: int = 0  # frames con scene_refresh_flag — ~ nº de shots de la peli

    l2_combos: list[L2Combo] = field(default_factory=list)
    l2_unique_count: int = 0
    l2_target_pqs: list[int] = field(default_factory=list)

    l8_combos: list[L8Combo] = field(default_factory=list)
    l8_unique_count: int = 0
    l8_target_indices: list[int] = field(default_factory=list)
    l8_neutral_pct: float = 0.0
    l8_has_mid_contrast: bool = False
    l8_has_clip_trim: bool = False


async def _run_export(rpu_path: Path, out_path: Path) -> tuple[int, str]:
    """Ejecuta `dovi_tool export -i <rpu> -d all=<out>` y devuelve (rc, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        DOVI_TOOL_BIN, "export",
        "-i", str(rpu_path),
        "-d", f"all={out_path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timeout"
    return proc.returncode, stderr.decode("utf-8", errors="replace")


def _extract_frames_from_json(data) -> list:
    """Maneja las dos estructuras conocidas del export de dovi_tool:
    lista plana de frames, o dict con clave 'rpus'/'frames'.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("rpus") or data.get("frames") or []
    return []


def _is_l2_neutral(combo: tuple) -> bool:
    """combo: (pq, slope, off, pow, chr, sat, msw). Neutro = todos los trims a 2048."""
    # combo[0] es target_max_pq, lo ignoramos para el chequeo de neutralidad
    return all(v == 2048 for v in combo[1:6])


def _is_l8_neutral(combo: tuple) -> bool:
    """combo: (idx, slope, off, pow, chr, sat, msw, mid_c, clip).
    Neutro = trims básicos a 2048; mid_c/clip pueden ser None o 2048."""
    # combo[0] es target_display_index
    if not all(v == 2048 for v in combo[1:7]):
        return False
    mid_c, clip = combo[7], combo[8]
    if mid_c is not None and mid_c != 2048:
        return False
    if clip is not None and clip != 2048:
        return False
    return True


def _parse_export(json_path: Path) -> RpuAnalysis:
    """Parsea el JSON del export y agrega combos + stats. Síncrono, llamado
    desde to_thread."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    frames = _extract_frames_from_json(data)

    analysis = RpuAnalysis()
    analysis.total_frames = len(frames)

    l2_counter: Counter = Counter()
    l8_counter: Counter = Counter()
    l2_pq_set: set[int] = set()
    l8_idx_set: set[int] = set()

    # Para contar el % de frames con L8 100% neutro necesitamos saber, por
    # frame, si TODOS sus L8 son neutros. Si un frame tiene aunque sea un L8
    # con trabajo, lo contamos como "trabajado". (Lo mismo aplicaría para L2
    # pero el % neutro de L2 no lo usamos hoy.)
    frames_with_any_l8_worked = 0

    for fr in frames:
        if not isinstance(fr, dict):
            continue
        vdr = fr.get("vdr_dm_data") or {}
        if not isinstance(vdr, dict):
            continue

        cmv40 = vdr.get("cmv40_metadata") or {}
        if cmv40:
            analysis.frames_with_cmv40 += 1
        if vdr.get("scene_refresh_flag"):
            analysis.scene_cuts += 1

        # L2 (CMv2.9 metadata)
        cmv29 = vdr.get("cmv29_metadata") or {}
        for block in (cmv29.get("ext_metadata_blocks") or []):
            if "Level2" in block:
                b = block["Level2"]
                combo = (
                    b.get("target_max_pq"), b.get("trim_slope"),
                    b.get("trim_offset"), b.get("trim_power"),
                    b.get("trim_chroma_weight"), b.get("trim_saturation_gain"),
                    b.get("ms_weight"),
                )
                l2_counter[combo] += 1
                if combo[0] is not None:
                    l2_pq_set.add(combo[0])

        # L8 (CMv4.0 metadata)
        l8_worked_in_this_frame = False
        for block in (cmv40.get("ext_metadata_blocks") or []):
            if "Level8" in block:
                b = block["Level8"]
                combo = (
                    b.get("target_display_index"), b.get("trim_slope"),
                    b.get("trim_offset"), b.get("trim_power"),
                    b.get("trim_chroma_weight"), b.get("trim_saturation_gain"),
                    b.get("ms_weight"),
                    b.get("target_mid_contrast"), b.get("clip_trim"),
                )
                l8_counter[combo] += 1
                if combo[0] is not None:
                    l8_idx_set.add(combo[0])
                if not _is_l8_neutral(combo):
                    l8_worked_in_this_frame = True
                if combo[7] is not None:
                    analysis.l8_has_mid_contrast = True
                if combo[8] is not None:
                    analysis.l8_has_clip_trim = True
        if l8_worked_in_this_frame:
            frames_with_any_l8_worked += 1

    # Materializar combos
    analysis.l2_combos = [
        L2Combo(
            target_max_pq=k[0] or 0,
            trim_slope=k[1] or 0,
            trim_offset=k[2] or 0,
            trim_power=k[3] or 0,
            trim_chroma_weight=k[4] or 0,
            trim_saturation_gain=k[5] or 0,
            ms_weight=k[6] or 0,
            occurrence_count=c,
        )
        for k, c in l2_counter.most_common()
    ]
    analysis.l2_unique_count = len(l2_counter)
    analysis.l2_target_pqs = sorted(l2_pq_set)

    analysis.l8_combos = [
        L8Combo(
            target_display_index=k[0] or 0,
            trim_slope=k[1] or 0,
            trim_offset=k[2] or 0,
            trim_power=k[3] or 0,
            trim_chroma_weight=k[4] or 0,
            trim_saturation_gain=k[5] or 0,
            ms_weight=k[6] or 0,
            target_mid_contrast=k[7],
            clip_trim=k[8],
            occurrence_count=c,
        )
        for k, c in l8_counter.most_common()
    ]
    analysis.l8_unique_count = len(l8_counter)
    analysis.l8_target_indices = sorted(l8_idx_set)

    # % de frames donde TODOS los L8 son neutros (= ninguno trabajado).
    # Si frames_with_cmv40 == 0 (RPU CMv2.9 puro), no aplica L8 → 0.0.
    if analysis.frames_with_cmv40 > 0:
        worked = frames_with_any_l8_worked
        analysis.l8_neutral_pct = 1.0 - (worked / analysis.frames_with_cmv40)
    else:
        analysis.l8_neutral_pct = 0.0

    return analysis


async def analyze_rpu_combos(rpu_path: Path) -> RpuAnalysis:
    """Ejecuta dovi_tool export -d all y parsea L2/L8 combos + stats.

    Devuelve un RpuAnalysis. Si dovi_tool falla, devuelve un RpuAnalysis
    vacío — el caller decide cómo continuar (típicamente: log warning y
    seguir sin los datos enriquecidos).

    El JSON intermedio se borra siempre, incluso si la operación falla.
    """
    if not rpu_path.exists():
        logger.warning("analyze_rpu_combos: RPU no existe: %s", rpu_path)
        return RpuAnalysis()

    # tempfile en el mismo directorio para evitar saltos de filesystem si
    # /tmp es tmpfs pequeño (caso QNAP). Cleanup en finally.
    tmp_dir = rpu_path.parent
    fd, tmp_path_str = tempfile.mkstemp(suffix=".json", prefix=".rpu_export_", dir=str(tmp_dir))
    import os
    os.close(fd)
    tmp_path = Path(tmp_path_str)

    try:
        rc, stderr = await _run_export(rpu_path, tmp_path)
        if rc != 0:
            logger.warning("dovi_tool export falló sobre %s (rc=%s): %s",
                           rpu_path.name, rc, stderr[:200])
            return RpuAnalysis()
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            logger.warning("dovi_tool export no generó JSON sobre %s", rpu_path.name)
            return RpuAnalysis()

        # El parseo del JSON puede ser costoso (cientos de MB en algunos RPUs).
        # Lo movemos al thread pool para no bloquear el event loop durante
        # varios segundos — otras corutinas (WS, polling REST) siguen vivas.
        return await asyncio.to_thread(_parse_export, tmp_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def classify_l8(analysis: RpuAnalysis) -> tuple[str, str]:
    """Decide si el bin del target tiene L8 "real" o "default".

    Devuelve (classification, human_readable_reason) donde classification es:
      - "real": bin con L8 trabajado por colorista. Restore aporta calidad.
      - "default": bin sintético sin trabajo real. Restore == Auto on-the-fly,
        recomendar Keep para ahorrar ~25 min de pipeline.
      - "indeterminate": en medio. Mejor avanzar y dejar al usuario decidir
        (los umbrales son conservadores para no bloquear casos limítrofes).

    Umbrales calibrados con 4 bins reales (Bloque 1, sample mayo 2026).
    """
    # Sin bloques CMv4.0 → no aplica (caso degenerado, se reporta como default)
    if analysis.frames_with_cmv40 == 0 or analysis.l8_unique_count == 0:
        return ("default", "El bin no tiene bloques L8 (CMv4.0).")

    if (analysis.l8_unique_count >= L8_REAL_MIN_UNIQUE_COMBOS
            and analysis.l8_neutral_pct < L8_REAL_MAX_NEUTRAL_PCT):
        # Refinamiento: detectar perfil "FULL" (mid_contrast + clip_trim poblados)
        # para emitir motivo descriptivo. No cambia la decisión.
        if analysis.l8_has_mid_contrast or analysis.l8_has_clip_trim:
            profile = "FULL"
        else:
            profile = "CORE"
        return ("real",
                f"L8 trabajado por colorista — {analysis.l8_unique_count} combos únicos, "
                f"{(1.0 - analysis.l8_neutral_pct) * 100:.0f}% frames con trim ({profile}).")

    if (analysis.l8_unique_count <= L8_DEFAULT_MAX_UNIQUE_COMBOS
            or analysis.l8_neutral_pct >= L8_DEFAULT_MIN_NEUTRAL_PCT):
        return ("default",
                f"Bin sintético — {analysis.l8_unique_count} combos L8 únicos, "
                f"{analysis.l8_neutral_pct * 100:.0f}% frames neutros. "
                f"Equivalente a la conversión CMv4.0 al vuelo de reproductores p3i/avdvplus.")

    return ("indeterminate",
            f"L8 ambiguo — {analysis.l8_unique_count} combos únicos, "
            f"{analysis.l8_neutral_pct * 100:.0f}% frames neutros. "
            f"Caso límite, mejor avanzar y decidir tras Fase A.")


# Umbral combos-por-shot para distinguir "CORE+" de "CORE":
# - Spider-Man: 69/2887 = 0.024 → CORE
# - Karate Kid: 64/1720 = 0.037 → CORE
# - Smashing Machine: 152/593 = 0.256 → pasa a FULL por mid_c/clip
# - 28 después: 1119/2617 = 0.428 → CORE+ (master con cambios casi cada shot)
# Umbral 0.1 separa claramente "core estándar" de "core rico".
L8_RICH_COMBOS_PER_SCENE_CUT = 0.1
# Fallback si no tenemos scene_cuts (raro pero posible): valor absoluto.
L8_RICH_MIN_COMBOS = 400


def classify_l8_quality(analysis: RpuAnalysis) -> tuple[str, str, str]:
    """Subclasifica la calidad del CMv4.0 de un bin clasificado como "real".

    Solo aplica si classify_l8(analysis) devolvió "real". Para "default" o
    "indeterminate" devuelve tier vacío.

    Devuelve (tier, label, description) donde:
      - tier: "core" | "core_rich" | "full" | "" (no aplica)
      - label: texto compacto para el filename ("CMv4 CORE", "CMv4 CORE+",
        "CMv4 FULL"). Va dentro del bracket [CMv4 LABEL].mkv del MKV final.
      - description: explicación legible para el log/UI.

    Criterios:
      - "full":      el L8 puebla `target_mid_contrast` o `clip_trim`
                     (campos exclusivos de CMv4.0 que solo se rellenan en
                     masters "full delivery" de estudios trabajados).
      - "core_rich": L8 con muchos combos por shot (master con grading
                     dinámico shot-a-shot intenso). Umbral: combos/scene_cuts
                     >= 0.1 (1 combo nuevo cada 10 shots o más).
      - "core":      L8 estándar de streaming — trabajado por shot pero con
                     cambios poco frecuentes, sin campos extra.
    """
    classification, _ = classify_l8(analysis)
    if classification != "real":
        return ("", "", "")

    # FULL: el master usa los campos CMv4.0-only
    if analysis.l8_has_mid_contrast or analysis.l8_has_clip_trim:
        extras = []
        if analysis.l8_has_mid_contrast:
            extras.append("target_mid_contrast")
        if analysis.l8_has_clip_trim:
            extras.append("clip_trim")
        return (
            "full",
            "CMv4 FULL",
            f"Master CMv4.0 FULL — {analysis.l8_unique_count} combos L8, "
            f"campos exclusivos CMv4.0 poblados ({', '.join(extras)}). "
            f"Calidad máxima: el colorista usó el toolkit completo de CMv4.0.",
        )

    # CORE+: muchos combos relativos a la longitud de la peli
    combos_per_cut = (
        analysis.l8_unique_count / analysis.scene_cuts
        if analysis.scene_cuts > 0 else 0.0
    )
    is_rich = (
        combos_per_cut >= L8_RICH_COMBOS_PER_SCENE_CUT
        or analysis.l8_unique_count >= L8_RICH_MIN_COMBOS
    )
    if is_rich:
        return (
            "core_rich",
            "CMv4 CORE+",
            f"Master CMv4.0 CORE+ — {analysis.l8_unique_count} combos L8 "
            f"({combos_per_cut:.2f} combos/shot). Grading dinámico shot-a-shot "
            f"intenso; el colorista trabajó casi todas las escenas.",
        )

    # CORE: estándar streaming — funcional pero no excepcional
    return (
        "core",
        "CMv4 CORE",
        f"Master CMv4.0 CORE — {analysis.l8_unique_count} combos L8 "
        f"({combos_per_cut:.2f} combos/shot). Trims básicos por shot, sin "
        f"campos CMv4.0-only. Calidad típica de release streaming "
        f"(Apple TV+, Disney+, Netflix).",
    )


def filename_label_from_tier(tier: str) -> str:
    """Devuelve el texto para insertar en [CMv4 XXX] del filename.
    Devuelve "" si tier no es válido (no aplica al filename)."""
    return {
        "core":      "CMv4 CORE",
        "core_rich": "CMv4 CORE+",
        "full":      "CMv4 FULL",
    }.get(tier, "")
