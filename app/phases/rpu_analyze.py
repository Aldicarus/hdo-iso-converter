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

from i18n import t as tr

import asyncio
import json
import logging
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from models import L2Combo, L8Combo
from phases.cmv40_strategy import va_por_drop_in

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

# Rama "real minimal": masters CMv4.0 con look global uniforme + brackets
# de toning por shot. Pocos combos únicos pero con trabajo CMv4.0 real:
# campos exclusivos (mid_contrast / clip_trim) poblados + al menos un
# combo con delta > 50 unidades del neutro en alguno de los trims básicos.
# Calibrados con Black Phone 2 (3 combos, slope +117, mid_c=2121) y
# Expediente Warren (5 combos, slope -276, sat -410, clip=1901).
L8_REAL_MINIMAL_MIN_COMBOS = 3
L8_REAL_MINIMAL_SIGNIFICANT_DELTA = 50

# ── L3, y por qué su umbral es el de L8 ───────────────────────────────────
#
# L3 son los offsets sobre el L1 y **solo los trae el bin**: el export de
# `level3` sobre el RPU de un BD (P7 MEL, CM v2.9) sale vacío. Es además
# una señal INDEPENDIENTE de L8 — sobre los mismos 2.159 frames de una
# muestra de 21 películas de la biblioteca, Transformers One da L8=1 y
# L3=83, Pulp Fiction 1 y 8, Supergirl 1 y 7.
#
# **El umbral está HEREDADO de L8, no calibrado sobre L3.** Los conteos que
# hay son de un sniff de 90 s, que sirve para ver que las dos señales
# divergen pero no para fijar un corte. Por eso L3 **solo puede RESCATAR**:
# nunca degrada una clasificación ni baja un tier, así que en el peor caso
# de que el umbral esté mal, lo que pasa es que un bin sintético deja de
# llamarse sintético y el usuario decide — no que uno bueno se descarte.
# Revisar cuando haya conteos completos de unos cuantos jobs reales.
L3_REAL_MIN_UNIQUE_COMBOS = L8_REAL_MIN_UNIQUE_COMBOS


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

    # L3 — offsets sobre el L1 (min/max/avg PQ) por escena. Es metadata
    # EXCLUSIVA del bin CMv4.0 en este flujo: medido con un sniff sobre el
    # RPU de un BD (P7 MEL, CM v2.9) el export de `level3` sale **vacío**,
    # así que el merge que lo transfiere no pisa nada del disco.
    #
    # Es una señal INDEPENDIENTE de L8, no la misma vista dos veces. Sobre
    # los mismos 2.159 frames de una muestra de 21 películas de la
    # biblioteca: Transformers One da **L8=1 y L3=83**, Pulp Fiction 1 y 8,
    # Supergirl 1 y 7. O sea que un máster con el L8 plano puede llevar
    # grading L3 real, y mirando solo L8 se le llama sintético.
    l3_unique_count: int = 0
    l3_frames: int = 0
    l3_neutral_pct: float = 0.0


async def _run_export(
    rpu_path: Path,
    out_path: Path,
    timeout: int = 60,
    log_callback=None,
    register_proc=None,
    export_args: list[str] | None = None,
) -> tuple[int, str]:
    """Ejecuta `dovi_tool export -i <rpu> -d all=<out>` y devuelve (rc, stderr).

    Para RPUs pequeños (bins de comunidad ~1-5 MB, Tab 3) 60s es de sobra.
    Para RPUs full-movie (UHD BD ~100-200 MB, Tab 2 quality audit) hay que
    pasar timeout=900+ — dovi_tool tarda 5-15 min generando el JSON 300-500 MB.

    Si se pasa ``log_callback``, hace streaming de stdout+stderr línea a
    línea al log durante la ejecución. Indispensable para operaciones largas
    (sin esto el usuario no ve progreso y parece que el modal está colgado).

    Cleanup robusto: si dispara timeout, SIGTERM → wait 5s → SIGKILL → wait
    10s para garantizar reap. Sin esto, file handles abiertos sobre NAS
    pueden bloquear el siguiente run.
    """
    args = export_args if export_args is not None else ["-d", f"all={out_path}"]
    proc = await asyncio.create_subprocess_exec(
        DOVI_TOOL_BIN, "export",
        "-i", str(rpu_path),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,  # merge para poder hacer streaming simple
    )
    if register_proc:
        register_proc(proc)

    collected: list[str] = []

    async def _stream_reader():
        """Lee stdout línea a línea (con stderr mergeado) y emite al log."""
        while True:
            try:
                line = await proc.stdout.readline()
            except Exception:
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            collected.append(text)
            if log_callback:
                try:
                    log_callback('  ' + str(text))
                except Exception:
                    pass

    reader_task = asyncio.create_task(_stream_reader())
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        # Drena cualquier línea pendiente del reader antes de retornar
        try:
            await asyncio.wait_for(reader_task, timeout=2.0)
        except asyncio.TimeoutError:
            reader_task.cancel()
    except asyncio.TimeoutError:
        # Cleanup robusto: SIGTERM → reap → SIGKILL si necesario → reap.
        # Sin este reap, los siguientes runs pueden chocar con file handles
        # abiertos sobre el NAS (estado D del proceso).
        logger.warning("dovi_tool export excedió %ss, matando proceso", timeout)
        if log_callback:
            try:
                log_callback('  ' + tr('rpu_analyze.dovi_tool_export_excedio_s_matando', timeout=timeout))
            except Exception:
                pass
        try: proc.terminate()
        except Exception: pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("dovi_tool export: subprocess no reaped tras SIGKILL+10s")
        try: reader_task.cancel()
        except Exception: pass
        return -1, "timeout"
    return proc.returncode, "\n".join(collected)


async def _run_export_simple(rpu_path: Path, out_path: Path) -> tuple[int, str]:
    """Wrapper retro-compatible para Tab 3 (sin streaming + timeout 60s)."""
    return await _run_export(rpu_path, out_path, timeout=60)


# Niveles que necesita el análisis de combos. L1 sirve de censo de frames
# (un registro por frame), L2/L8 son los combos y `scenes` da los cortes.
# L3 va en la lista base: alimenta el classifier igual que L2 y L8, y su
# coste es un fichero más en un export que ya corre (12,3 MB medidos sobre
# 155.001 frames, frente a los ~115 MB del conjunto).
_EXPORT_LEVELS = ("level1", "level2", "level3", "level8")


async def export_levels(
    rpu_path: Path,
    levels,
    out_dir: Path | None = None,
    timeout: int = 300,
    log_callback=None,
    register_proc=None,
) -> dict[str, list] | None:
    """Exporta SOLO los niveles pedidos y devuelve sus registros parseados.

    Alternativa a `dovi_tool export -d all`, que vuelca el RPU entero: sobre
    un bin real de 145.303 frames son 682 MB y ~100 s, frente a 8 MB y ~1 s
    si lo único que quieres es el L1. Requiere dovi_tool >= 2.3.3.

    Formato de cada nivel (lista plana, un registro por frame y bloque):
        level1: {"frame","min_pq","max_pq","avg_pq"}
        level5: {"frame","active_area_{left,right,top,bottom}_offset"}
        level6: {"frame","max_display_mastering_luminance",
                 "max_content_light_level","max_frame_average_light_level",…}
        level9: {"frame","length","source_primary_index"}

    Devuelve None si dovi_tool no conoce `--levels` o si el export falla:
    el caller debe conservar su camino alternativo.
    """
    levels = tuple(levels)
    if not levels:
        return None
    out = out_dir or rpu_path.parent
    stem = f".lvl_{os.getpid()}_{rpu_path.stem[:24]}"
    paths = {lv: out / f"{stem}_{lv}.json" for lv in levels}
    try:
        rc, stderr = await _run_export(
            rpu_path, out, timeout=timeout,
            log_callback=log_callback, register_proc=register_proc,
            export_args=[
                "-f", "json",
                "--levels", ",".join(f"{lv}={paths[lv]}" for lv in levels),
            ],
        )
        if rc != 0:
            logger.info("export --levels %s falló sobre %s (rc=%s): %s",
                        levels, rpu_path.name, rc, stderr[:160])
            return None

        def _read_all() -> dict[str, list]:
            data: dict[str, list] = {}
            for lv, p in paths.items():
                if not p.exists() or p.stat().st_size == 0:
                    data[lv] = []
                    continue
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    data[lv] = loaded if isinstance(loaded, list) else []
                except (ValueError, OSError):
                    data[lv] = []
            return data

        return await asyncio.to_thread(_read_all)
    except Exception as e:  # noqa: BLE001
        logger.info("export --levels %s no disponible sobre %s: %s",
                    levels, rpu_path.name, e)
        return None
    finally:
        for p in paths.values():
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


async def _run_export_levels(
    rpu_path: Path,
    out_dir: Path,
    stem: str,
    timeout: int = 60,
    log_callback=None,
    register_proc=None,
    niveles_extra: tuple[str, ...] = (),
) -> tuple[int, str, dict[str, Path]]:
    """Export SELECTIVO por niveles — la vía rápida (dovi_tool >= 2.3.3).

    `niveles_extra` añade niveles al export por encima de los que necesitan
    los combos. Lo usa el análisis extendido de Tab 2, que pide también L5 y L6
    para el perfil de luminancia: sacarlos en la MISMA pasada es gratis, y
    volver a extraer el RPU para ellos costaría los ~650 s de la extracción.

    `export -d all` vuelca el RPU entero: medido sobre un bin real de 61 MB /
    145.303 frames son **682 MB en ~100 s**, que además hay que releer y
    parsear (varios GB de objetos Python, con el NAS ya tirando de swap).
    De todo ese volcado solo usamos L1, L2, L8 y los cortes de escena.

    `--levels` los saca directamente: **115 MB en 4 s**, y el parseo baja de
    ~15 s a 1,9 s. Formato JSON y no CSV a propósito: el writer CSV de 2.3.3
    aborta con "found record with 11 fields, but the previous record has 9"
    en cuanto un bloque L8 trae los campos CMv4.0 (`target_mid_contrast`,
    `clip_trim`) — justo los masters FULL que más nos interesan.

    Devuelve (rc, stderr, paths) con los ficheros generados.
    """
    pedidos = tuple(_EXPORT_LEVELS) + tuple(
        lv for lv in niveles_extra if lv not in _EXPORT_LEVELS)
    paths = {lv: out_dir / f".{stem}_{lv}.json" for lv in pedidos}
    paths["scenes"] = out_dir / f".{stem}_scenes.json"
    levels_arg = ",".join(f"{lv}={paths[lv]}" for lv in pedidos)
    rc, stderr = await _run_export(
        rpu_path, out_dir, timeout=timeout,
        log_callback=log_callback, register_proc=register_proc,
        export_args=[
            "-f", "json",
            "-d", f"scenes={paths['scenes']}",
            "--levels", levels_arg,
        ],
    )
    return rc, stderr, paths


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


def cargar_niveles(paths: dict[str, Path]) -> dict[str, list]:
    """Lee los ficheros de `--levels` y devuelve las listas planas por nivel.

    Es la entrada de `luminance.perfil_desde_niveles`, que consume el mismo
    formato que `export_levels` devuelve ya parseado. Aquí se lee de FICHERO
    porque el análisis extendido hace un único export cuyos ficheros alimentan
    a los dos consumidores: los combos L8/L2 y el perfil de luminancia.
    """
    fuera: dict[str, list] = {}
    for clave, ruta in paths.items():
        if clave == "scenes" or not ruta or not ruta.exists():
            continue
        try:
            if ruta.stat().st_size == 0:
                continue
            with open(ruta, "r", encoding="utf-8") as f:
                datos = json.load(f)
        except (ValueError, OSError) as e:
            logger.warning("No se pudo leer el export de %s: %s", clave, e)
            continue
        if isinstance(datos, list):
            fuera[clave] = datos
    return fuera


def _contar_cortes_de_escena(ruta) -> int:
    """Cuenta los cortes de escena del export `-d scenes` de dovi_tool.

    **Ese fichero NO es JSON**, aunque el export lleve `-f json` y la ruta
    acabe en `.json`: `dovi_tool export -d scenes=…` escribe un índice de
    frame por línea, en texto plano. Comprobado contra un bin del repo
    DoviTools (2.585 líneas, la primera un `0` suelto).

    Pasarlo por `json.load` falla con «Extra data: line 2 column 1 (char 2)»
    —el primer entero ya es un documento JSON completo y el segundo es
    basura para el parser—, así que desde `48e369f` (el export por niveles)
    `scene_cuts` valía **siempre 0**: el criterio relativo del tier CORE+
    (`combos/scene_cuts >= 0.1`) quedó muerto y solo podía salir CORE+ por
    el respaldo absoluto de 400 combos. Medido en el `/config` del NAS: los
    23 proyectos con `scene_cuts > 0` son todos anteriores a ese commit.

    Un array JSON se acepta igual, por si una versión futura lo emite. Y
    ante cualquier OTRO formato se devuelve 0 con un aviso en vez de una
    cuenta a medias: un número inventado con pinta de dato es peor que el
    hueco, que además tiene respaldo (el umbral absoluto de combos).
    """
    if not ruta or not ruta.exists():
        return 0
    try:
        texto = ruta.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.warning("No se pudo leer el export de scenes: %s", e)
        return 0
    if not texto:
        return 0
    if texto[0] == "[":
        try:
            datos = json.loads(texto)
        except ValueError as e:
            logger.warning("No se pudo leer el export de scenes: %s", e)
            return 0
        return len(datos) if isinstance(datos, list) else 0
    cortes = 0
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        if not linea.lstrip("-").isdigit():
            logger.warning(
                "El export de scenes no tiene el formato esperado "
                "(un indice de frame por linea); linea: %r", linea[:40])
            return 0
        cortes += 1
    return cortes


def analysis_desde_paths(paths: dict[str, Path]) -> RpuAnalysis:
    """`RpuAnalysis` a partir de ficheros de `--levels` ya generados."""
    return _parse_export_levels(paths)


def _parse_export_levels(paths: dict[str, Path]) -> RpuAnalysis:
    """Construye el RpuAnalysis desde los exports POR NIVEL de dovi_tool 2.3.3.

    Equivalente a `_parse_export` pero leyendo `--levels level1/level2/level8`
    (+ `-d scenes`) en vez del volcado completo del RPU. Mismos campos, ~6x
    menos bytes y ~25x menos tiempo — ver `_run_export_levels`.

    Cada fichero es una lista plana de registros con el índice de frame:
        L1: {"frame":0,"min_pq":0,"max_pq":2081,"avg_pq":819}
        L2: {"frame":40,"target_max_pq":2081,"trim_slope":2019,...}
        L8: {"frame":0,"length":10,"target_display_index":1,...,
             "target_mid_contrast":…,"clip_trim":…}   ← los dos últimos
             solo aparecen en bloques CMv4.0 extendidos
    """
    def _load(key: str) -> list:
        p = paths.get(key)
        if not p or not p.exists() or p.stat().st_size == 0:
            return []
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError) as e:
            logger.warning("No se pudo leer el export de %s: %s", key, e)
            return []
        return data if isinstance(data, list) else []

    l1 = _load("level1")
    l2 = _load("level2")
    l3 = _load("level3")
    l8 = _load("level8")

    analysis = RpuAnalysis()
    # L1 tiene exactamente un registro por frame del RPU.
    analysis.total_frames = len(l1)
    # `scenes` son los índices con scene_refresh_flag=1, y NO van en JSON:
    # uno por línea. Por eso tiene su propio lector y no pasa por `_load`.
    analysis.scene_cuts = _contar_cortes_de_escena(paths.get("scenes"))

    l2_counter: Counter = Counter()
    l2_pq_set: set[int] = set()
    for r in l2:
        if not isinstance(r, dict):
            continue
        combo = (
            r.get("target_max_pq"), r.get("trim_slope"),
            r.get("trim_offset"), r.get("trim_power"),
            r.get("trim_chroma_weight"), r.get("trim_saturation_gain"),
            r.get("ms_weight"),
        )
        l2_counter[combo] += 1
        if combo[0] is not None:
            l2_pq_set.add(combo[0])

    # L3: offsets sobre el L1. El neutro es 2048 en los tres, igual que en
    # los trims de L2/L8 — pero ojo, en los RPUs reales medidos el
    # `avg_pq_offset` **nunca** vale 2048 (The Amateur: (2048,2048,1909) en
    # los 176.448 frames), así que el porcentaje de neutros de L3 no es
    # comparable con el de L8 y no se usa como umbral. Lo que discrimina es
    # el número de combos.
    l3_counter: Counter = Counter()
    for r in l3:
        if not isinstance(r, dict):
            continue
        l3_counter[(r.get("min_pq_offset"), r.get("max_pq_offset"),
                    r.get("avg_pq_offset"))] += 1
    analysis.l3_frames = sum(l3_counter.values())
    analysis.l3_unique_count = len(l3_counter)
    if analysis.l3_frames:
        neutros = sum(n for c, n in l3_counter.items()
                      if all(v in (None, 2048) for v in c))
        analysis.l3_neutral_pct = neutros / analysis.l3_frames

    l8_counter: Counter = Counter()
    l8_idx_set: set[int] = set()
    # Frames con al menos un L8 = frames con metadata CMv4.0. Es el denominador
    # de l8_neutral_pct, igual que `frames_with_cmv40` en el parser del volcado.
    frames_with_l8: set = set()
    frames_worked: set = set()
    for r in l8:
        if not isinstance(r, dict):
            continue
        combo = (
            r.get("target_display_index"), r.get("trim_slope"),
            r.get("trim_offset"), r.get("trim_power"),
            r.get("trim_chroma_weight"), r.get("trim_saturation_gain"),
            r.get("ms_weight"),
            r.get("target_mid_contrast"), r.get("clip_trim"),
        )
        l8_counter[combo] += 1
        if combo[0] is not None:
            l8_idx_set.add(combo[0])
        frame = r.get("frame")
        frames_with_l8.add(frame)
        if not _is_l8_neutral(combo):
            frames_worked.add(frame)
        # Solo cuenta como campo CMv4.0-only usado si NO es neutro (audit #14).
        if combo[7] is not None and combo[7] != 2048:
            analysis.l8_has_mid_contrast = True
        if combo[8] is not None and combo[8] != 2048:
            analysis.l8_has_clip_trim = True

    analysis.frames_with_cmv40 = len(frames_with_l8)
    analysis.l2_combos = _materialize_l2(l2_counter)
    analysis.l2_unique_count = len(l2_counter)
    analysis.l2_target_pqs = sorted(l2_pq_set)
    analysis.l8_combos = _materialize_l8(l8_counter)
    analysis.l8_unique_count = len(l8_counter)
    analysis.l8_target_indices = sorted(l8_idx_set)
    if analysis.frames_with_cmv40 > 0:
        analysis.l8_neutral_pct = 1.0 - (len(frames_worked) / analysis.frames_with_cmv40)
    else:
        analysis.l8_neutral_pct = 0.0
    return analysis


def _materialize_l2(counter: Counter) -> list[L2Combo]:
    return [
        L2Combo(
            target_max_pq=k[0] or 0, trim_slope=k[1] or 0, trim_offset=k[2] or 0,
            trim_power=k[3] or 0, trim_chroma_weight=k[4] or 0,
            trim_saturation_gain=k[5] or 0, ms_weight=k[6] or 0,
            occurrence_count=c,
        )
        for k, c in counter.most_common()
    ]


def _materialize_l8(counter: Counter) -> list[L8Combo]:
    return [
        L8Combo(
            target_display_index=k[0] or 0, trim_slope=k[1] or 0,
            trim_offset=k[2] or 0, trim_power=k[3] or 0,
            trim_chroma_weight=k[4] or 0, trim_saturation_gain=k[5] or 0,
            ms_weight=k[6] or 0, target_mid_contrast=k[7], clip_trim=k[8],
            occurrence_count=c,
        )
        for k, c in counter.most_common()
    ]


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
                # Solo cuenta como "campo CMv4.0-only usado" si está poblado con
                # un valor NO neutro. Un target_mid_contrast/clip_trim presente
                # pero a 2048 (neutro) no es trabajo del colorista — marcarlo
                # inflaba el tier a [CMv4 FULL] (audit #14). Alineado con
                # _is_l8_neutral, que también trata None/2048 como neutro.
                if combo[7] is not None and combo[7] != 2048:
                    analysis.l8_has_mid_contrast = True
                if combo[8] is not None and combo[8] != 2048:
                    analysis.l8_has_clip_trim = True
        if l8_worked_in_this_frame:
            frames_with_any_l8_worked += 1

    # Materializar combos
    analysis.l2_combos = _materialize_l2(l2_counter)
    analysis.l2_unique_count = len(l2_counter)
    analysis.l2_target_pqs = sorted(l2_pq_set)

    analysis.l8_combos = _materialize_l8(l8_counter)
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


async def _try_levels_export(
    rpu_path: Path,
    tmp_dir: Path,
    stem: str,
    export_timeout: int,
    log_callback,
    register_proc,
) -> RpuAnalysis | None:
    """Intenta el export por niveles. None si no está disponible o falla —
    el caller cae entonces al volcado completo, que funciona en cualquier
    versión de dovi_tool."""
    paths: dict[str, Path] = {}
    try:
        rc, stderr, paths = await _run_export_levels(
            rpu_path, tmp_dir, stem,
            timeout=export_timeout,
            log_callback=log_callback,
            register_proc=register_proc,
        )
        if rc != 0:
            logger.info(
                "export --levels no disponible sobre %s (rc=%s): %s — "
                "usando el volcado completo", rpu_path.name, rc, stderr[:160])
            return None
        generated = {k: p for k, p in paths.items() if p.exists() and p.stat().st_size > 0}
        if "level1" not in generated or "level8" not in generated:
            logger.info("export --levels incompleto sobre %s — usando el volcado completo",
                        rpu_path.name)
            return None
        if log_callback:
            try:
                total_mb = sum(p.stat().st_size for p in generated.values()) / (1024 * 1024)
                log_callback('  ' + tr('rpu_analyze.niveles_exportados_mb_parseando_combos', p1=format(total_mb, '.1f')))
            except Exception:
                pass
        return await asyncio.to_thread(_parse_export_levels, generated)
    except Exception as e:  # noqa: BLE001 — nunca debe tumbar el análisis
        logger.info("export --levels falló sobre %s (%s) — usando el volcado completo",
                    rpu_path.name, e)
        return None
    finally:
        for p in paths.values():
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


async def analyze_rpu_combos(
    rpu_path: Path,
    export_timeout: int = 60,
    log_callback=None,
    register_proc=None,
) -> RpuAnalysis:
    """Ejecuta dovi_tool export -d all y parsea L2/L8 combos + stats.

    Devuelve un RpuAnalysis. Si dovi_tool falla, devuelve un RpuAnalysis
    vacío — el caller decide cómo continuar (típicamente: log warning y
    seguir sin los datos enriquecidos).

    El JSON intermedio se borra siempre, incluso si la operación falla.

    Args:
        export_timeout: segundos máx para dovi_tool export. Default 60s
            (suficiente para bins de comunidad pequeños usados por Tab 3).
            Para RPUs full-movie (Tab 2 quality audit con ~100-200 MB)
            pasar 900s+ — dovi_tool tarda 5-15 min generando el JSON.
        log_callback: opcional, recibe cada línea de stdout/stderr de
            dovi_tool en streaming. Sin esto el usuario no ve progreso
            durante los minutos del export.
        register_proc: opcional, registra el subprocess para cancel externo.
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
        # ── Vía rápida: export por niveles (dovi_tool >= 2.3.3) ──────────
        # 115 MB / 4 s frente a los 682 MB / 100 s del volcado completo.
        # Si el binario es anterior no conoce `--levels` y sale con error de
        # parseo de argumentos: caemos al `-d all` de siempre.
        fast = await _try_levels_export(
            rpu_path, tmp_dir, tmp_path.stem,
            export_timeout, log_callback, register_proc)
        if fast is not None:
            return fast

        rc, stderr = await _run_export(
            rpu_path, tmp_path,
            timeout=export_timeout,
            log_callback=log_callback,
            register_proc=register_proc,
        )
        if rc != 0:
            logger.warning("dovi_tool export falló sobre %s (rc=%s): %s",
                           rpu_path.name, rc, stderr[:200])
            return RpuAnalysis()
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            logger.warning("dovi_tool export no generó JSON sobre %s", rpu_path.name)
            return RpuAnalysis()

        if log_callback:
            try:
                size_mb = tmp_path.stat().st_size / (1024 * 1024)
                log_callback('  ' + tr('rpu_analyze.json_generado_mb_parseando_combos', p1=format(size_mb, '.1f')))
            except Exception:
                pass

        # El parseo del JSON puede ser costoso (cientos de MB en algunos RPUs).
        # Lo movemos al thread pool para no bloquear el event loop durante
        # varios segundos — otras corutinas (WS, polling REST) siguen vivas.
        return await asyncio.to_thread(_parse_export, tmp_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def numeros_de_l8(analysis: RpuAnalysis) -> dict:
    """Los NÚMEROS de los que dependen el motivo y el tier, sin los combos.

    El veredicto de un RPU se guarda en la caché de Tab 2 como TEXTO, así
    que un análisis hecho con la app en castellano se servía en castellano
    para siempre. La salida es no guardar el texto sino re-derivarlo al
    leer, y para eso hace falta que la explicación dependa solo de lo que
    la caché sí tiene: estos ocho números.

    `l8_combos` queda fuera a propósito — es la lista completa de combos
    (miles en un UHD) y no se persiste; lo único que decide es la RAMA
    «real minimal» de `_clasificacion_de_l8`, cuyo resultado sí está
    cacheado como `quality_classification`.
    """
    return {
        "frames_with_cmv40": analysis.frames_with_cmv40,
        "scene_cuts": analysis.scene_cuts,
        "l8_unique_count": analysis.l8_unique_count,
        "l8_neutral_pct": analysis.l8_neutral_pct,
        "l8_has_mid_contrast": analysis.l8_has_mid_contrast,
        "l8_has_clip_trim": analysis.l8_has_clip_trim,
        "l2_unique_count": analysis.l2_unique_count,
        "l2_target_pqs": len(analysis.l2_target_pqs),
        "l3_unique_count": analysis.l3_unique_count,
        "l3_frames": analysis.l3_frames,
    }


def _l8_sin_bloques(n: dict) -> bool:
    """Sin bloques CMv4.0 → no aplica (caso degenerado, se da por default)."""
    return n["frames_with_cmv40"] == 0 or n["l8_unique_count"] == 0


def _l8_real_por_combos(n: dict) -> bool:
    """Muchos combos únicos y pocos frames neutros: master trabajado."""
    return (n["l8_unique_count"] >= L8_REAL_MIN_UNIQUE_COMBOS
            and n["l8_neutral_pct"] < L8_REAL_MAX_NEUTRAL_PCT)


def _l3_trabajado(n: dict) -> bool:
    """¿El L3 del bin tiene trabajo por escena?

    Solo se consulta para RESCATAR un bin que el L8 daría por sintético.
    Un `default` significa «no proceses, el reproductor hace lo mismo al
    vuelo», y eso deja de ser cierto si el bin trae offsets L3 que el
    reproductor no puede inventarse — el BD no los tiene.
    """
    return n.get("l3_unique_count", 0) >= L3_REAL_MIN_UNIQUE_COMBOS


def _l8_sintetico(n: dict) -> bool:
    """Muy pocos combos únicos (1-2 = look global de conversión al vuelo), O
    bien mayoría de frames neutros PERO sin alcanzar un nº de combos propio
    de un master trabajado. El antiguo `OR neutral>=95%` a secas marcaba
    "default" másters CORE reales de pelis OSCURAS (50+ combos reales, pero
    la mayoría de frames en escenas oscuras → trims a neutro), recomendando
    Mantener y descartando un bin válido (audit #3). `combos<=2` sigue
    siendo disparador INDEPENDIENTE (1-2 combos = sintético siempre, aunque
    el combo sea no-neutro) para no regresar la detección de bins sintéticos
    de 1 combo con trim global no-neutro."""
    return (n["l8_unique_count"] <= L8_DEFAULT_MAX_UNIQUE_COMBOS
            or (n["l8_neutral_pct"] >= L8_DEFAULT_MIN_NEUTRAL_PCT
                and n["l8_unique_count"] < L8_REAL_MIN_UNIQUE_COMBOS))


def _l8_trim_significativo(combos) -> bool:
    """¿Algún combo se aparta más de 50 unidades del neutro en algún trim?

    Es lo que separa un master real con look uniforme de un sintético con
    jitter: los dos tienen pocos combos, y el sintético los tiene todos
    pegados al 2048.
    """
    d = L8_REAL_MINIMAL_SIGNIFICANT_DELTA
    return any(
        abs((c.trim_slope or 2048) - 2048) > d
        or abs((c.trim_offset or 2048) - 2048) > d
        or abs((c.trim_power or 2048) - 2048) > d
        or abs((c.trim_saturation_gain or 2048) - 2048) > d
        for c in combos
    )


def _clasificacion_de_l8(analysis: RpuAnalysis) -> str:
    """Decide si el bin del target tiene L8 "real", "default" o "indeterminate".

    Es la única parte que necesita la lista de combos, y por eso es la única
    que no se puede recalcular desde la caché.
    """
    n = numeros_de_l8(analysis)
    if _l8_sin_bloques(n):
        return "default"
    if _l8_real_por_combos(n):
        return "real"
    # Rama "real minimal": pocos combos pero con campos CMv4.0-only poblados
    # Y al menos un combo con trim significativo (>50 unidades del neutro).
    # Casos validados: Black Phone 2 (3 combos, mid_c=2121, clip=2503,
    # slope+117/+119), Expediente Warren (5 combos, clip=1901, slope=-276,
    # sat=-410). Sin esta rama caerían en "indeterminate" y la UI los marcaba
    # como ambiguos cuando son masters reales con look global uniforme.
    if (n["l8_unique_count"] >= L8_REAL_MINIMAL_MIN_COMBOS
            and (n["l8_has_mid_contrast"] or n["l8_has_clip_trim"])
            and analysis.l8_combos
            and _l8_trim_significativo(analysis.l8_combos)):
        return "real"
    if _l8_sintetico(n):
        # …salvo que el L3 diga lo contrario. Se sube a «indeterminate» y no
        # a «real» a propósito: el umbral de L3 está heredado de L8, así que
        # lo honesto es «no puedo afirmar que sea sintético», que es
        # exactamente lo que esa clasificación significa. Nunca al revés —
        # un L3 pobre no degrada un L8 bueno.
        return "indeterminate" if _l3_trabajado(n) else "default"
    return "indeterminate"


def motivo_de_l8(n: dict, classification: str) -> str:
    """El MOTIVO legible, derivado de los números y de la clasificación ya
    decidida — nunca al revés.

    Con `classification` dada, la rama que produjo el texto queda
    determinada por los números: si no es el caso degenerado, un "real"
    viene de `_l8_real_por_combos` o, si esa no se cumple, de la rama
    minimal; un "default" viene de `_l8_sintetico`. Así el texto se puede
    reconstruir desde la caché sin tener los combos delante.
    """
    if _l8_sin_bloques(n):
        return tr('rpu_analyze.el_bin_no_tiene_bloques_l8_cmv4')

    if classification == "real":
        if _l8_real_por_combos(n):
            # Refinamiento: detectar perfil "FULL" (mid_contrast + clip_trim
            # poblados) para emitir motivo descriptivo. No cambia la decisión.
            if n["l8_has_mid_contrast"] or n["l8_has_clip_trim"]:
                profile = "FULL"
            else:
                profile = "CORE"
            return tr('rpu_analyze.l8_trabajado_por_colorista_l8_unique_count', l8_unique_count=n["l8_unique_count"], p2=format((1.0 - n["l8_neutral_pct"]) * 100, '.0f'), profile=profile)
        extras = []
        if n["l8_has_mid_contrast"]:
            extras.append("mid_contrast")
        if n["l8_has_clip_trim"]:
            extras.append("clip_trim")
        return tr('rpu_analyze.l8_minimal_trabajado_l8_unique_count_combos', l8_unique_count=n["l8_unique_count"], p2=', '.join(extras))

    if classification == "default":
        return tr('rpu_analyze.bin_sintetico_l8_unique_count_combos_l8', l8_unique_count=n["l8_unique_count"], p2=format(n["l8_neutral_pct"] * 100, '.0f'))

    return tr('rpu_analyze.l8_ambiguo_l8_unique_count_combos_unicos', l8_unique_count=n["l8_unique_count"], p2=format(n["l8_neutral_pct"] * 100, '.0f'))


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
    classification = _clasificacion_de_l8(analysis)
    return (classification, motivo_de_l8(numeros_de_l8(analysis), classification))


# Umbral combos-por-shot para distinguir "CORE+" de "CORE":
# - Spider-Man: 69/2887 = 0.024 → CORE
# - Karate Kid: 64/1720 = 0.037 → CORE
# - Smashing Machine: 152/593 = 0.256 → pasa a FULL por mid_c/clip
# - 28 después: 1119/2617 = 0.428 → CORE+ (master con cambios casi cada shot)
# Umbral 0.1 separa claramente "core estándar" de "core rico".
L8_RICH_COMBOS_PER_SCENE_CUT = 0.1
# Fallback si no tenemos scene_cuts (raro pero posible): valor absoluto.
L8_RICH_MIN_COMBOS = 400


def tier_de_l8(n: dict, classification: str) -> tuple[str, str, str]:
    """El tier, su label y su descripción — puros sobre los números.

    No necesita los combos: `classify_l8_quality` solo los usaba para
    llamar a `classify_l8` y quedarse con la clasificación, que aquí llega
    ya decidida (y en la caché está persistida).
    """
    if classification != "real":
        return ("", "", "")

    # FULL: el master usa los campos CMv4.0-only
    if n["l8_has_mid_contrast"] or n["l8_has_clip_trim"]:
        extras = []
        if n["l8_has_mid_contrast"]:
            extras.append("target_mid_contrast")
        if n["l8_has_clip_trim"]:
            extras.append("clip_trim")
        # Subtipo "minimal" si tiene pocos combos (look global) pero igual
        # poblados los campos CMv4.0-only — masters tipo Black Phone 2,
        # Expediente Warren: poca variación shot-a-shot, look uniforme.
        if n["l8_unique_count"] < L8_REAL_MIN_UNIQUE_COMBOS:
            return (
                "full",
                "CMv4 FULL",
                tr('rpu_analyze.master_cmv4_0_full_minimal_l8_unique', l8_unique_count=n["l8_unique_count"], p2=', '.join(extras)),
            )
        return (
            "full",
            "CMv4 FULL",
            tr('rpu_analyze.master_cmv4_0_full_l8_unique_count', l8_unique_count=n["l8_unique_count"], p2=', '.join(extras)),
        )

    # CORE+: muchos combos relativos a la longitud de la peli
    combos_per_cut = (
        n["l8_unique_count"] / n["scene_cuts"]
        if n["scene_cuts"] > 0 else 0.0
    )
    # L3 cuenta con el MISMO criterio relativo, y solo para sumar: un L3
    # pobre no puede bajar de CORE+ a CORE un máster cuyo L8 ya lo merece.
    l3_por_corte = (
        n.get("l3_unique_count", 0) / n["scene_cuts"]
        if n["scene_cuts"] > 0 else 0.0
    )
    is_rich = (
        combos_per_cut >= L8_RICH_COMBOS_PER_SCENE_CUT
        or n["l8_unique_count"] >= L8_RICH_MIN_COMBOS
        or l3_por_corte >= L8_RICH_COMBOS_PER_SCENE_CUT
        or n.get("l3_unique_count", 0) >= L8_RICH_MIN_COMBOS
    )
    if is_rich:
        return (
            "core_rich",
            "CMv4 CORE+",
            tr('rpu_analyze.master_cmv4_0_core_l8_unique_count', l8_unique_count=n["l8_unique_count"], combos_per_cut=format(combos_per_cut, '.2f')),
        )

    # CORE: estándar streaming — funcional pero no excepcional
    return (
        "core",
        "CMv4 CORE",
        tr('rpu_analyze.master_cmv4_0_core_l8_unique_count', l8_unique_count=n["l8_unique_count"], combos_per_cut=format(combos_per_cut, '.2f')),
    )


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
    return tier_de_l8(numeros_de_l8(analysis), _clasificacion_de_l8(analysis))


def filename_label_from_tier(tier: str) -> str:
    """Devuelve el texto para insertar en [CMv4 XXX] del filename.
    Devuelve "" si tier no es válido (no aplica al filename)."""
    return {
        "core":      "CMv4 CORE",
        "core_rich": "CMv4 CORE+",
        "full":      "CMv4 FULL",
    }.get(tier, "")


def _combo_to_tuple_l2(combo) -> tuple:
    """Convierte un L2Combo a tupla hasheable para comparación.
    occurrence_count se excluye intencionalmente — comparamos VALORES, no
    cuántas veces aparece cada combo (un colorista puede haber aplicado el
    mismo trim a más o menos shots según el corte y aún ser "el mismo L2")."""
    return (
        combo.target_max_pq,
        combo.trim_slope,
        combo.trim_offset,
        combo.trim_power,
        combo.trim_chroma_weight,
        combo.trim_saturation_gain,
        combo.ms_weight,
    )


def compare_l2(source_combos: list, target_combos: list) -> tuple[str, str]:
    """Compara el L2 del source RPU vs el L2 del bin target.

    Devuelve (verdict, reason) donde verdict es:
      - "identical": el SET de combinaciones únicas de valores es idéntico.
        Implica que RESET_9999 preservó el L2 del BD al generar el bin
        (caso de los 4+ bins reales analizados empíricamente).
      - "different": los sets de combos difieren — el L2 del bin viene de
        otro grading. La regla conservadora del modelo dice: preservar
        L2 del source (merge selectivo [3,8,9,11,254], no transferir L2
        del bin). "Nunca pegar un L2 peor".
      - "unknown": falta uno de los dos análisis (no se puede comparar).
        Tratar como "different" por seguridad.

    Solo compara los VALORES de cada combo, no su frecuencia (occurrence_count).
    Dos RPUs con los mismos sets de trim values son funcionalmente
    equivalentes para chips CMv2.9 aunque las frecuencias difieran.
    """
    if not source_combos and not target_combos:
        return ("unknown", tr('rpu_analyze.no_hay_datos_l2_de_ningun_lado'))
    if not source_combos or not target_combos:
        return ("unknown",
                tr('rpu_analyze.falta_analisis_l2_de_p1_no_se', p1="source" if not source_combos else "target"))

    source_set = {_combo_to_tuple_l2(c) for c in source_combos}
    target_set = {_combo_to_tuple_l2(c) for c in target_combos}

    if source_set == target_set:
        return ("identical",
                tr('rpu_analyze.l2_byte_a_byte_identico_p1_combos', p1=len(source_set)))

    only_in_source = source_set - target_set
    only_in_target = target_set - source_set
    common = source_set & target_set
    return ("different",
            tr('rpu_analyze.l2_distinto_p1_combos_comunes_p2_solo', p1=len(common), p2=len(only_in_source), p3=len(only_in_target)))


def recommend_action(session) -> tuple[str, str, str]:
    """Calcula la recomendación del modelo de 4 caminos para una sesión CMv4.0.

    Devuelve (action, label, reason) donde action es:
      - "keep":     no procesar. El reproductor compatible con CMv4.0 (p3i T4
                    con Auto append) hace la conversión al vuelo con resultado
                    equivalente. Casos: bin sintético, sin bin, no aporta.
      - "drop_in":  inyección directa del RPU del bin (rápido, ~30s).
                    Solo posible si profile match + L2 idéntico.
      - "merge":    merge selectivo con rpu_levels=[3,8,9,11,254].
                    Cualquier otro caso "real" — preserva L2 source.
      - "unknown":  faltan datos (Fase A no ejecutada o análisis vacío).

    El árbol de decisión coincide con el modelo cerrado tras el chequeo
    empírico (ver ESTUDIO en histórico de la conversación):

        ¿Hay bin?
        ├─ NO → KEEP
        └─ SÍ → ¿L8 del bin trabajado?
                ├─ NO → KEEP
                └─ SÍ → ¿Profile match?
                        ├─ NO → MERGE [3,8,9,11,254]
                        └─ SÍ → ¿L2 idéntico?
                                ├─ SÍ → DROP-IN
                                └─ NO → MERGE [3,8,9,11,254]
    """
    # Decisión inmediata si pre-flight ya lo resolvió
    if session.preflight_decision in ("keep_l8_default", "keep_no_l8", "abort_no_cmv40"):
        return (
            "keep",
            tr('rpu_analyze.mantener_mkv_actual'),
            session.preflight_message or tr('rpu_analyze.el_pre_flight_detecto_que_procesar_este'),
        )

    # Sin bin descargado / sin pre-flight OK → KEEP por defecto
    if not session.target_preflight_ok:
        return (
            "keep",
            tr('rpu_analyze.mantener_mkv_actual'),
            tr('rpu_analyze.el_bin_no_esta_validado_sin_pre'),
        )

    # Si llegamos aquí el bin está validado y tiene L8 trabajado.
    # Necesitamos Fase A completa para comparar L2.
    if session.source_l2_unique_count == 0:
        return (
            "unknown",
            tr('rpu_analyze.analisis_pendiente'),
            tr('rpu_analyze.fase_a_no_se_ha_ejecutado_todavia'),
        )

    # Comparación L2 source vs target
    l2_verdict, l2_reason = compare_l2(
        session.source_l2_combos, session.target_l2_combos
    )

    # Profile match (source workflow vs bin)
    source_wf = session.source_workflow or ""
    source_is_fel = source_wf == "p7_fel"
    source_is_mel = source_wf == "p7_mel"
    source_is_p8  = source_wf == "p8"
    target = session.target_dv_info
    target_is_fel = bool(target and target.profile == 7 and target.el_type == "FEL")
    target_is_mel = bool(target and target.profile == 7 and target.el_type == "MEL")
    target_is_p8  = bool(target and target.profile == 8)
    profile_match = (
        (source_is_fel and target_is_fel)
        or (source_is_mel and target_is_mel)
        or (source_is_p8  and target_is_p8)
    )

    quality_label = session.target_l8_quality_label or "CMv4"
    perfil_mkv = source_wf.upper().replace('_', ' ') if source_wf else '?'

    # La RUTA la decide `cmv40_strategy`, que es quien la ejecuta. Aquí
    # estaba replicada con otras reglas —perfil coincidente en las tres
    # combinaciones más L2 idéntico, sin mirar `target_type` ni los gates—
    # y prometía «~30 segundos» a jobs que acababan haciendo el merge
    # entero: 10 de los 41 proyectos con recomendación del `/config` del
    # NAS, con los 8 terminados en `output_workflow=restore_merge`.
    if va_por_drop_in(session):
        return (
            "drop_in",
            tr('rpu_analyze.inyectar_rpu_cmv4_0_rapido'),
            tr('rpu_analyze.el_perfil_del_bin_coincide_con_el', p1=perfil_mkv, quality_label=quality_label),
        )

    # Merge selectivo. El motivo, del más raíz al más fino: `profile_match`
    # y el L2 ya no deciden nada, pero siguen siendo lo que hay que contarle
    # al usuario cuando son ellos los que impiden la ruta rápida.
    if not profile_match:
        reason = tr(
            'rpu_analyze.perfil_no_coincide',
            perfil_bin=target.profile if target else '?',
            el_bin=(' ' + target.el_type) if target and target.el_type else '',
            perfil_mkv=perfil_mkv,
            calidad=quality_label)
    elif l2_verdict != "identical":
        reason = tr('rpu_analyze.l2_difiere', motivo=l2_reason,
                    calidad=quality_label)
    elif not (source_wf == "p7_fel"
              and session.target_type == "trusted_p7_fel_final"):
        # Coincide todo y aun así no hay ruta rápida: sustituir el RPU
        # entero solo existe para P7 FEL. Es el caso de 7 de los 10.
        reason = tr('rpu_analyze.drop_in_solo_fel',
                    perfil_mkv=perfil_mkv, calidad=quality_label)
    elif (session.trust_override or "auto") == "force_interactive":
        reason = tr('rpu_analyze.drop_in_revision_manual',
                    calidad=quality_label)
    else:
        caidos = ", ".join(
            k for k, v in (session.target_trust_gates or {}).items()
            if isinstance(v, dict) and not v.get("ok", True)
        )
        reason = tr('rpu_analyze.drop_in_gates_caidos',
                    gates=caidos or "?", calidad=quality_label)

    return ("merge", tr('rpu_analyze.inyectar_rpu_cmv4_0_preserva_l2'), reason)


# ── Procedencia declarada en el nombre del bin ───────────────────────────────
#
# Los bins del repositorio DoviTools se nombran a mano y quien los genera
# suele DECLARAR en el nombre que el L5 (active area / letterbox) varía a lo
# largo de la película — títulos IMAX, open matte, escenas que se abren a
# pantalla completa.
#
# Tokens en orden de tabla. `openmatte` va aparte de `open matte` porque la
# normalización solo convierte `.`, `_` y `-` en espacio: un `openmatte` pegado
# sigue siendo una sola palabra.
_TOKENS_L5_VARIABLE = (
    "variable l5",
    "var l5",
    "l5 variable",
    "imax",
    "open matte",
    "openmatte",
)

# Separadores típicos de los nombres de release. Sin esto se escapa
# `Hail.Mary.2026.UHD-BD_P7 MEL_variable_L5_(retail…)`, que es un caso real.
_SEPARADORES_NOMBRE = re.compile(r"[._\-]+")
_ESPACIOS = re.compile(r"\s+")

# Máximo que se muestra de la nota de la hoja (texto libre de la comunidad,
# a veces son párrafos enteros con URLs).
_MAX_NOTA_SHEET = 200

_RE_L5_EN_NOTAS = re.compile(r"\bl5\b")


def _normalizar_nombre_bin(nombre: str) -> str:
    """Minúsculas + `.`/`_`/`-` a espacio, colapsando espacios."""
    return _ESPACIOS.sub(" ", _SEPARADORES_NOMBRE.sub(" ", (nombre or "").lower())).strip()


def pistas_de_procedencia(nombre_bin: str, notas_sheet: str = "") -> dict:
    """Pistas sobre el L5 del bin que se leen de su NOMBRE, no del RPU.

    Función pura (sin IO, sin subprocess, sin logging). Tolera ``None`` y ``""``
    en los dos argumentos.

    Devuelve::

        {
          "declara_l5_variable": bool,    # algún token encontrado en el NOMBRE
          "tokens": ["variable l5", …],   # los encontrados, en orden de tabla
          "nota_sheet_l5": str | None,    # la nota recortada si menciona 'l5'
        }

    Medido sobre los 99 proyectos reales del NAS con nombre de bin y perfil L5
    conocido::

        n=99   TP=6   FP=0   FN=7   TN=86   →   precisión 100%, recall 46%

    **Regla de uso: esta señal NUNCA relaja un veredicto, solo avisa de
    contradicciones.** La asimetría de los números es la que manda: cuando el
    nombre lo declara siempre es verdad (cero falsos positivos en todo el
    corpus), pero se le escapa más de la mitad de los casos. Por tanto:

      - nombre dice "variable" + medimos "constante"  → el error es NUESTRO,
        hay que avisar de la contradicción;
      - nombre calla                                  → no concluye nada, el
        silencio NO es evidencia de L5 constante y no puede saltarse ningún
        chequeo.

    ``notas_sheet`` es texto libre de la hoja de recomendaciones de la
    comunidad. Solo se mira si menciona L5, y **como contexto para mostrar al
    usuario, no como detector**: en el corpus solo 1 de los 24 proyectos con
    notas menciona L5, así que no da para clasificar. Por eso no toca
    ``declara_l5_variable``.
    """
    normalizado = _normalizar_nombre_bin(nombre_bin)

    tokens: list[str] = []
    for token in _TOKENS_L5_VARIABLE:
        # Con frontera de palabra: `imax` suelto es señal, pero dentro de
        # `Climax` no lo es. La normalización ya dejó los separadores como
        # espacios, así que `\b` basta.
        if re.search(rf"\b{re.escape(token)}\b", normalizado) and token not in tokens:
            tokens.append(token)

    notas = (notas_sheet or "").strip()
    nota_l5: str | None = None
    if notas and _RE_L5_EN_NOTAS.search(notas.lower()):
        nota_l5 = notas if len(notas) <= _MAX_NOTA_SHEET else notas[:_MAX_NOTA_SHEET].rstrip() + "…"

    return {
        "declara_l5_variable": bool(tokens),
        "tokens": tokens,
        "nota_sheet_l5": nota_l5,
    }
