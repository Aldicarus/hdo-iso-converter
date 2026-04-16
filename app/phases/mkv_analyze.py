"""
mkv_analyze.py — Tab 2: Análisis y edición de MKVs existentes

Responsabilidades:
  1. Analizar un MKV con ``mkvmerge -J`` + ``mkvextract chapters``.
  2. Aplicar ediciones in-place con ``mkvpropedit`` (O(1), sin remux).
  3. Si hay reorden de pistas, remuxar con ``mkvmerge -o`` (copia completa).

Todas las operaciones son stateless — no se persiste nada en disco.
El estado de edición vive en el frontend.
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

from models import Chapter, MkvAnalysisResult, MkvEditRequest, MkvTrackInfo

_logger = logging.getLogger(__name__)

MKVMERGE_BIN    = "mkvmerge"
MKVPROPEDIT_BIN = "mkvpropedit"
MKVEXTRACT_BIN  = "mkvextract"

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/mnt/output")


# ══════════════════════════════════════════════════════════════════════
#  ANÁLISIS
# ══════════════════════════════════════════════════════════════════════

async def analyze_mkv(mkv_path: str) -> MkvAnalysisResult:
    """
    Analiza un MKV existente: pistas, capítulos, metadatos.

    Ejecuta ``mkvmerge -J`` para la identificación de pistas y
    ``mkvextract chapters --simple`` para los capítulos.
    """
    if not Path(mkv_path).exists():
        raise RuntimeError(f"Fichero no encontrado: {mkv_path}")

    # ── mkvmerge -J ──────────────────────────────────────────────
    proc = await asyncio.create_subprocess_exec(
        MKVMERGE_BIN, "-J", mkv_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode >= 2:
        raise RuntimeError(f"mkvmerge -J falló: {stderr.decode()[:300]}")

    data = json.loads(stdout.decode("utf-8", errors="replace"))

    # ── Pistas ───────────────────────────────────────────────────
    tracks = []
    for t in data.get("tracks", []):
        props = t.get("properties", {})
        tracks.append(MkvTrackInfo(
            id=t.get("id", 0),
            type=t.get("type", "video"),
            codec=t.get("codec", ""),
            language=props.get("language", ""),
            name=props.get("track_name", ""),
            flag_default=props.get("default_track", False),
            flag_forced=props.get("forced_track", False),
            channels=props.get("audio_channels"),
            sample_rate=props.get("audio_sampling_frequency"),
            pixel_dimensions=props.get("pixel_dimensions", ""),
        ))

    # ── Metadatos del contenedor ─────────────────────────────────
    container = data.get("container", {}).get("properties", {})
    title = container.get("title", "")
    duration_ns = container.get("duration")
    duration_s = (duration_ns / 1_000_000_000) if duration_ns else 0.0

    # ── FEL: segundo track HEVC a 1080p ──────────────────────────
    hevc_count = 0
    has_fel = False
    for t in tracks:
        if t.type == "video" and ("HEVC" in t.codec or "H.265" in t.codec):
            hevc_count += 1
            if hevc_count > 1 and "1920" in t.pixel_dimensions:
                has_fel = True

    # ── Capítulos ────────────────────────────────────────────────
    chapters = _extract_chapters(mkv_path)

    # ── Fichero ──────────────────────────────────────────────────
    p = Path(mkv_path)

    return MkvAnalysisResult(
        file_path=mkv_path,
        file_name=p.name,
        file_size_bytes=p.stat().st_size,
        duration_seconds=duration_s,
        title=title,
        tracks=tracks,
        chapters=chapters,
        has_fel=has_fel,
    )


def _extract_chapters(mkv_path: str) -> list[Chapter]:
    """Extrae capítulos del MKV con mkvextract --simple."""
    try:
        result = subprocess.run(
            [MKVEXTRACT_BIN, mkv_path, "chapters", "--simple"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    chapters = []
    timestamps = {}
    names = {}

    for line in result.stdout.strip().splitlines():
        line = line.strip()
        m = re.match(r"CHAPTER(\d+)=([\d:.]+)", line)
        if m:
            timestamps[int(m.group(1))] = m.group(2)
            continue
        m = re.match(r"CHAPTER(\d+)NAME=(.*)", line)
        if m:
            names[int(m.group(1))] = m.group(2).strip()

    for num in sorted(timestamps.keys()):
        raw_name = names.get(num, "")
        is_generic = bool(re.match(r"^Chapter\s+\d+$", raw_name, re.IGNORECASE))
        if is_generic or not raw_name:
            name = f"Capítulo {num:02d}"
            name_custom = False
        else:
            name = raw_name
            name_custom = True

        chapters.append(Chapter(
            number=num,
            timestamp=timestamps[num],
            name=name,
            name_custom=name_custom,
        ))

    return chapters


# ══════════════════════════════════════════════════════════════════════
#  APLICAR EDICIONES
# ══════════════════════════════════════════════════════════════════════

async def apply_mkv_edits(request: MkvEditRequest) -> dict:
    """
    Aplica ediciones de metadatos a un MKV existente vía mkvpropedit (O(1)).

    Soporta: nombres de pistas, flags default/forced, capítulos.
    No soporta: eliminación ni reorden de pistas (requeriría remux).

    Returns:
        {"ok": True, "new_path": str, "output": str}
    """
    mkv_path = request.file_path
    if not Path(mkv_path).exists():
        raise RuntimeError(f"Fichero no encontrado: {mkv_path}")

    output = await _apply_propedit(mkv_path, request)
    return {"ok": True, "new_path": mkv_path, "output": output}


async def _apply_propedit(mkv_path: str, request: MkvEditRequest) -> str:
    """Aplica ediciones de metadatos con mkvpropedit. Devuelve el output."""
    cmd = [MKVPROPEDIT_BIN, mkv_path]

    # Título del contenedor
    if request.title is not None:
        cmd += ["--edit", "info", "--set", f"title={request.title}"]

    # Pistas de audio
    for t in request.audio_tracks:
        cmd += ["--edit", f"track:{t.id + 1}"]  # mkvpropedit usa 1-based
        if t.name is not None:
            cmd += ["--set", f"name={t.name}"]
        if t.flag_default is not None:
            cmd += ["--set", f"flag-default={'1' if t.flag_default else '0'}"]
        if t.flag_forced is not None:
            cmd += ["--set", f"flag-forced={'1' if t.flag_forced else '0'}"]

    # Pistas de subtítulos
    for t in request.subtitle_tracks:
        cmd += ["--edit", f"track:{t.id + 1}"]
        if t.name is not None:
            cmd += ["--set", f"name={t.name}"]
        if t.flag_default is not None:
            cmd += ["--set", f"flag-default={'1' if t.flag_default else '0'}"]
        if t.flag_forced is not None:
            cmd += ["--set", f"flag-forced={'1' if t.flag_forced else '0'}"]

    # Capítulos
    chapters_xml = None
    if request.chapters is not None:
        chapters_xml = _write_chapters_xml(request.chapters)
        cmd += ["--chapters", chapters_xml]

    _logger.info("mkvpropedit: %d argumentos sobre %s", len(cmd), Path(mkv_path).name)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if chapters_xml:
        Path(chapters_xml).unlink(missing_ok=True)

    output = (stdout.decode("utf-8", errors="replace") +
              stderr.decode("utf-8", errors="replace")).strip()

    if proc.returncode >= 2:
        raise RuntimeError(
            f"mkvpropedit falló (código {proc.returncode}): {output[:300]}"
        )

    return output


def _write_chapters_xml(chapters: list[Chapter]) -> str:
    """Serializa capítulos a XML Matroska temporal."""
    root = ET.Element("Chapters")
    edition = ET.SubElement(root, "EditionEntry")

    for ch in chapters:
        atom = ET.SubElement(edition, "ChapterAtom")
        ET.SubElement(atom, "ChapterTimeStart").text = ch.timestamp
        ET.SubElement(atom, "ChapterFlagHidden").text = "0"
        ET.SubElement(atom, "ChapterFlagEnabled").text = "1"
        display = ET.SubElement(atom, "ChapterDisplay")
        ET.SubElement(display, "ChapterString").text = ch.name
        ET.SubElement(display, "ChapterLanguage").text = "spa"

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    tmp = tempfile.NamedTemporaryFile(
        suffix=".xml", prefix="chapters_", delete=False, mode="wb"
    )
    tree.write(tmp, encoding="utf-8", xml_declaration=True)
    tmp.close()
    return tmp.name
