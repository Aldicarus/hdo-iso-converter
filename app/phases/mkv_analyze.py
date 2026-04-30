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

from models import Chapter, DoviInfo, HdrMetadata, MkvAnalysisResult, MkvEditRequest, MkvTrackInfo

_logger = logging.getLogger(__name__)

MKVMERGE_BIN    = "mkvmerge"
MKVPROPEDIT_BIN = "mkvpropedit"
MKVEXTRACT_BIN  = "mkvextract"
FFMPEG_BIN      = "ffmpeg"
DOVI_TOOL_BIN   = "dovi_tool"

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/mnt/output")
TMP_DIR    = os.environ.get("TMP_DIR", "/mnt/tmp")


# ══════════════════════════════════════════════════════════════════════
#  ANÁLISIS
# ══════════════════════════════════════════════════════════════════════

async def analyze_mkv(
    mkv_path: str,
    progress_callback=None,
    pgs_progress_callback=None,
) -> MkvAnalysisResult:
    """
    Analiza un MKV existente: pistas, capítulos, metadatos.

    Pipeline: mkvmerge -J + mkvextract chapters + MediaInfo + ffprobe packet
    counts + dovi_tool info. En un MKV grande (40-60 GB) puede tardar 1-3 min,
    dominado por el conteo de paquetes PGS.

    Si se pasa ``progress_callback(step: str)``, se notifica al arrancar cada
    paso costoso para que el frontend pueda mostrar un modal de progreso.
    Pasos emitidos: ``identify``, ``mediainfo``, ``pgs``, ``dovi``.

    Si se pasa ``pgs_progress_callback(pct: float, eta_s: int)``, durante el
    conteo de paquetes ffprobe se emite progreso real basado en bytes leídos
    (vía /proc/{pid}/io), exactamente como en Tab 1.
    """
    async def _emit(step: str):
        if progress_callback:
            try:
                await progress_callback(step)
            except Exception:
                pass

    if not Path(mkv_path).exists():
        raise RuntimeError(f"Fichero no encontrado: {mkv_path}")

    # ── mkvmerge -J ──────────────────────────────────────────────
    await _emit("identify")
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

    # ── MediaInfo (enriquecimiento opcional) ────────────────────────
    await _emit("mediainfo")
    hdr_meta = None
    mediainfo_raw = None
    try:
        from phases.phase_a import run_mediainfo
        mi = await run_mediainfo(mkv_path)
        mediainfo_raw = mi.raw_json

        mi_video = [t for t in mi.tracks if t.track_type == "video"]
        mi_audio = [t for t in mi.tracks if t.track_type == "audio"]
        mi_subs  = [t for t in mi.tracks if t.track_type == "text"]

        # Enriquecer pistas de vídeo
        video_tracks_list = [t for t in tracks if t.type == "video"]
        if video_tracks_list and mi_video:
            mv = mi_video[0]
            video_tracks_list[0].bitrate_kbps = mv.bitrate_kbps
            video_tracks_list[0].bit_depth = mv.bit_depth
            video_tracks_list[0].color_primaries = mv.color_primaries
            hdr_fmt = "HDR10" if mv.transfer_characteristics == "PQ" else ("HLG" if mv.transfer_characteristics == "HLG" else "")
            video_tracks_list[0].hdr_format = hdr_fmt
            if hdr_fmt:
                hdr_meta = HdrMetadata(
                    hdr_format=hdr_fmt,
                    color_primaries=mv.color_primaries,
                    transfer_characteristics=mv.transfer_characteristics,
                    bit_depth=mv.bit_depth,
                )
                # MaxCLL/MaxFALL del raw
                if mi.raw_json:
                    for rt in mi.raw_json.get("media", {}).get("track", []):
                        if rt.get("@type") == "Video" and rt.get("@typeorder", "1") == "1":
                            try:
                                hdr_meta.max_cll = int(rt["MaxCLL"]) if rt.get("MaxCLL") else None
                            except (ValueError, TypeError):
                                pass
                            try:
                                hdr_meta.max_fall = int(rt["MaxFALL"]) if rt.get("MaxFALL") else None
                            except (ValueError, TypeError):
                                pass
                            hdr_meta.mastering_display_luminance = rt.get("MasteringDisplay_Luminance", "")
                            hdr_meta.mastering_display_primaries = rt.get("MasteringDisplay_ColorPrimaries", "")
                            break

        # Enriquecer pistas de audio
        audio_idx = 0
        for t in tracks:
            if t.type == "audio" and audio_idx < len(mi_audio):
                ma = mi_audio[audio_idx]
                t.bitrate_kbps = ma.bitrate_kbps
                t.format_commercial = ma.format_commercial
                t.channel_layout = ma.channel_layout
                t.compression_mode = ma.compression_mode
                audio_idx += 1

        # Enriquecer pistas de subtítulos — resolution del bitmap PGS + bitrate
        sub_idx = 0
        for t in tracks:
            if t.type == "subtitles" and sub_idx < len(mi_subs):
                ms = mi_subs[sub_idx]
                if ms.resolution:
                    t.pixel_dimensions = ms.resolution
                if ms.bitrate_kbps:
                    t.bitrate_kbps = ms.bitrate_kbps
                sub_idx += 1

    except Exception as e:
        _logger.warning("MediaInfo falló para MKV %s (no bloquea): %s", mkv_path, e)

    # ── Packet counts de subtítulos bitmap (ffprobe) ─────────────────
    # Proxy fiable de forzado vs completo cuando el flag no está seteado.
    # Reutiliza la función de phase_a que monitoriza bytes leídos por ffprobe
    # para emitir progreso real durante el conteo (~1-3 min en MKVs grandes).
    try:
        sub_tracks_list = [t for t in tracks if t.type == "subtitles"]
        if sub_tracks_list:
            await _emit("pgs")
            from phases.phase_a import run_pgs_packet_counts
            # Pasamos duration_s para que run_pgs_packet_counts muestree los
            # primeros 20 min (si el MKV dura >30 min) y escale por proporción
            # -> misma cadencia, misma clasificación, 5-10× más rápido.
            pkt_counts = await run_pgs_packet_counts(
                mkv_path,
                progress_callback=pgs_progress_callback,
                total_duration_seconds=duration_s,
            )
            # ffprobe devuelve stream_index absoluto dentro del MKV,
            # que coincide con mkvmerge "id" de pista.
            for t in sub_tracks_list:
                if t.id in pkt_counts:
                    t.packet_count = pkt_counts[t.id]
    except Exception as e:
        _logger.warning("ffprobe packet count falló para MKV %s (no bloquea): %s", mkv_path, e)

    # ── dovi_tool (opcional — añade profile, FEL/MEL, CM version, L levels) ──
    await _emit("dovi")
    dovi_info = None
    try:
        hevc_count_val = sum(
            1 for t in tracks
            if t.type == "video" and ("HEVC" in t.codec.upper() or "H.265" in t.codec.upper())
        )
        dovi_info = await _run_dovi_on_mkv(mkv_path, hevc_count_val)
    except Exception as e:
        _logger.warning("dovi_tool falló para MKV %s (no bloquea): %s", mkv_path, e)

    return MkvAnalysisResult(
        file_path=mkv_path,
        file_name=p.name,
        file_size_bytes=p.stat().st_size,
        duration_seconds=duration_s,
        title=title,
        tracks=tracks,
        chapters=chapters,
        has_fel=has_fel,
        hdr=hdr_meta,
        dovi=dovi_info,
        mediainfo_raw=mediainfo_raw,
    )


async def _run_dovi_on_mkv(mkv_path: str, hevc_count: int) -> DoviInfo | None:
    """
    Analiza el RPU Dolby Vision de un MKV.

    - Si hay 2+ pistas HEVC (P7 FEL/MEL) usa la Enhancement Layer (v:1).
    - Si hay 1 pista HEVC (P8/P5 single-layer) usa la Base Layer (v:0).
    - Si no hay DV, ffmpeg/extract-rpu fallan y se devuelve None.

    Usa ``dovi_tool extract-rpu --limit 720 mkv_path`` directamente — soporte
    nativo de MKV + limit de frames desde 2.3.0. Evita la pre-extracción HEVC
    con ffmpeg (antes 30s + fichero intermedio de ~300 MB).

    Si el MKV tiene el EL en una pista separada (raro, pero ocurre en algunos
    rips antiguos), dovi_tool puede fallar leyendo el MKV — en ese caso
    fallback al flujo ffmpeg + extract-rpu sobre HEVC intermedio.

    Reutiliza el parser de ``phases.phase_a._parse_dovi_summary``.
    """
    from phases.phase_a import _parse_dovi_summary

    pid = os.getpid()
    tmp_rpu = str(Path(TMP_DIR) / f"_mkv_rpu_{pid}.bin")
    # Limit de frames para muestreo: 720 ≈ 30s a 24fps, suficiente para
    # identificar profile + CM version + niveles presentes.
    _LIMIT_FRAMES = "720"

    try:
        # Vía rápida: extract-rpu directo del MKV (sin intermedio HEVC)
        proc = await asyncio.create_subprocess_exec(
            DOVI_TOOL_BIN, "extract-rpu",
            "--limit", _LIMIT_FRAMES,
            mkv_path, "-o", tmp_rpu,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0 or not Path(tmp_rpu).exists() or Path(tmp_rpu).stat().st_size < 10:
            # Fallback para MKVs con EL en pista separada: ffmpeg extrae el
            # stream correcto y luego dovi_tool extract-rpu sobre el HEVC.
            _logger.info("dovi_tool MKV direct falló, fallback a ffmpeg (%s): %s",
                         "EL en pista separada" if hevc_count >= 2 else "estructura no estándar",
                         stderr.decode()[:200])
            tmp_hevc = str(Path(TMP_DIR) / f"_mkv_hevc_{pid}.hevc")
            map_arg = "0:v:1" if hevc_count >= 2 else "0:v:0"
            try:
                proc = await asyncio.create_subprocess_exec(
                    FFMPEG_BIN, "-y", "-i", mkv_path,
                    "-map", map_arg, "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                    "-t", "30", "-f", "hevc", tmp_hevc,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0 or not Path(tmp_hevc).exists() or Path(tmp_hevc).stat().st_size < 1000:
                    return None

                proc = await asyncio.create_subprocess_exec(
                    DOVI_TOOL_BIN, "extract-rpu", tmp_hevc, "-o", tmp_rpu,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0 or not Path(tmp_rpu).exists() or Path(tmp_rpu).stat().st_size < 10:
                    return None
            finally:
                Path(tmp_hevc).unlink(missing_ok=True)

        # dovi_tool info --summary sobre el RPU (ambas vías dejan aquí tmp_rpu listo)
        proc = await asyncio.create_subprocess_exec(
            DOVI_TOOL_BIN, "info", "--summary", tmp_rpu,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            _logger.warning("dovi_tool info falló: %s", stderr.decode()[:200])
            return None

        dovi = _parse_dovi_summary(stdout.decode("utf-8", errors="replace"))
        # Guardamos el tamaño del RPU para la radiografia (indicador de
        # metadata richness). Nota: con --limit 720 es sample-scoped, no
        # movie-scoped — util igualmente para calcular bytes/frame.
        try:
            dovi.rpu_size_bytes = Path(tmp_rpu).stat().st_size
        except Exception:
            pass
        # Enriquecimiento via `dovi_tool export` a JSON — mucho mas fiable que
        # parsear el texto de info --summary (formato varia entre versiones).
        # De aqui obtenemos L8 target_display_index/nits, L9/L10 primaries y
        # L11 content_type+intended_white+reference_mode de forma estructurada.
        try:
            await _enrich_dovi_from_json_export(dovi, tmp_rpu)
        except Exception as e:
            _logger.info("Enriquecimiento JSON falló (no bloquea): %s", e)
        return dovi
    finally:
        Path(tmp_rpu).unlink(missing_ok=True)


async def _enrich_dovi_from_json_export(dovi: DoviInfo, rpu_path: str) -> None:
    """Rellena campos granulares de DoviInfo leyendo el JSON export del RPU.

    Formato producido por `dovi_tool export -i rpu.bin -o rpu.json` (v2.3.x):
    lista de RPUs, cada una con `vdr_dm_data.ext_metadata_blocks` (o
    `ext_blocks`) con los niveles L1-L11. Para L8/L9/L10/L11 el JSON tiene
    los campos concretos que texto --summary no siempre expone.
    """
    import tempfile, json as _json
    from pathlib import Path as _Path
    tmpdir = _Path(tempfile.mkdtemp(prefix="dovi_export_"))
    json_path = tmpdir / "rpu.json"
    try:
        proc = await asyncio.create_subprocess_exec(
            DOVI_TOOL_BIN, "export", "-i", rpu_path, "-o", str(json_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not json_path.exists():
            _logger.info("dovi_tool export falló: %s", stderr.decode()[:200])
            return
        with open(json_path) as f:
            data = _json.load(f)
        rpus = data.get("rpus") if isinstance(data, dict) else data
        if not isinstance(rpus, list) or not rpus:
            return

        # Escaneamos TODOS los RPUs (no solo el primero) para capturar L8/L10
        # con target_display_index diferente — cada frame puede tener su bloque.
        l8_indices: set[int] = set()
        l8_trim_data: dict[int, dict] = {}   # {index: {min_nits, max_nits, ...}}
        l10_by_index: dict[int, dict] = {}   # {target_display_index: primaries + lum}
        l9_primaries_idx: int | None = None
        l11_content_type: int | None = None
        l11_intended_white: int | None = None
        l11_reference_mode: int | None = None

        for rpu in rpus:
            vdr = (rpu or {}).get("vdr_dm_data", {}) if isinstance(rpu, dict) else {}
            ext = vdr.get("ext_metadata_blocks") or vdr.get("ext_blocks") or []
            # En algunas versiones estan en un subobject
            if isinstance(ext, dict):
                ext = ext.get("blocks") or ext.get("level1") or []
            if not isinstance(ext, list):
                continue
            for b in ext:
                if not isinstance(b, dict):
                    continue
                lvl = b.get("level") or b.get("block_type") or b.get("ext_block_level")
                try: lvl = int(lvl)
                except Exception: continue

                if lvl == 8:
                    tdi = b.get("target_display_index") or b.get("l8_target_display_index")
                    if tdi is not None:
                        try:
                            tdi = int(tdi)
                            l8_indices.add(tdi)
                            if tdi not in l8_trim_data:
                                l8_trim_data[tdi] = b
                        except Exception:
                            pass
                elif lvl == 9:
                    if l9_primaries_idx is None:
                        idx = b.get("source_primaries_index") or b.get("source_primaries") or b.get("primaries_index")
                        try: l9_primaries_idx = int(idx)
                        except Exception: pass
                elif lvl == 10:
                    tdi = b.get("target_display_index") or b.get("l10_target_display_index")
                    try: tdi = int(tdi) if tdi is not None else None
                    except Exception: tdi = None
                    if tdi is not None and tdi not in l10_by_index:
                        l10_by_index[tdi] = b
                elif lvl == 11:
                    if l11_content_type is None:
                        ct = b.get("content_type") or b.get("l11_content_type")
                        try: l11_content_type = int(ct)
                        except Exception: pass
                    if l11_intended_white is None:
                        iw = b.get("intended_white_point") or b.get("intended_white") or b.get("whitepoint") or b.get("white_point")
                        try: l11_intended_white = int(iw)
                        except Exception: pass
                    if l11_reference_mode is None:
                        rm = b.get("reference_mode_flag")
                        try: l11_reference_mode = int(rm)
                        except Exception: pass

        # ── Construir L8 trim nits desde L10 blocks (cuando referencian target
        # displays) o desde los índices estándar de Dolby ──
        L8_STANDARD_NITS = {
            # Indices predefinidos segun Dolby Vision spec (subset comun)
            0: 100, 1: 350, 2: 600, 3: 1000, 4: 2000, 5: 4000,
            32: 100, 33: 350, 34: 600, 35: 1000, 36: 2000, 37: 4000,
            48: 1000, 49: 600, 50: 350,
            64: 2000, 65: 4000,
        }
        nits_list: list[int] = []
        for idx in sorted(l8_indices):
            nits = None
            # (a) Si hay L10 con mismo target_display_index, lee max_lum
            l10 = l10_by_index.get(idx)
            if l10:
                maxl = (l10.get("target_max_pq") or l10.get("max_display_mastering_luminance")
                        or l10.get("target_max_luminance"))
                if maxl:
                    try:
                        v = float(maxl)
                        # Si viene en PQ code (0-4095), convertir. Si viene en nits directos (>4096), usar.
                        if v > 4096: nits = int(v)
                        elif v > 1:  nits = int(_pq_code_to_nits_local(v))
                        else:        nits = int(_pq_code_to_nits_local(v * 4095))
                    except Exception: pass
            # (b) Fallback a mapping estandar de Dolby
            if nits is None:
                nits = L8_STANDARD_NITS.get(idx)
            if nits is not None:
                nits_list.append(nits)

        if nits_list:
            # Reemplazamos el posible parseo aproximado del summary con datos JSON
            dovi.l8_trim_nits = sorted(set(nits_list))
            dovi.l8_trim_count = len(dovi.l8_trim_nits)
            dovi.has_l8 = True

        # ── L9 source primaries ──
        PRIMARIES_MAP = {
            0: "BT.709", 1: "Reserved", 2: "Reserved", 3: "Reserved",
            4: "BT.470M", 5: "BT.470BG", 6: "BT.601", 7: "SMPTE 240M",
            8: "Generic",
            9: "BT.2020", 10: "SMPTE ST 428",
            11: "DCI-P3", 12: "DCI-P3 D65",
        }
        if l9_primaries_idx is not None:
            dovi.l9_primaries = PRIMARIES_MAP.get(l9_primaries_idx, f"Index {l9_primaries_idx}")
            dovi.has_l9 = True

        # ── L10 target primaries (primer target display con primaries utiles) ──
        if l10_by_index:
            for tdi, blk in l10_by_index.items():
                pidx = (blk.get("target_primaries_index") or blk.get("primaries_index")
                        or blk.get("target_primary_index"))
                if pidx is not None:
                    try:
                        dovi.l10_primaries = PRIMARIES_MAP.get(int(pidx), f"Index {pidx}")
                        dovi.has_l10 = True
                        break
                    except Exception: continue

        # ── L11 content type + intended white + reference mode ──
        CONTENT_TYPE_MAP = {
            0: "Reserved", 1: "Cinema", 2: "Games",
            3: "Sport", 4: "User Generated",
            5: "Movies", 6: "TV",
        }
        INTENDED_WHITE_MAP = {
            0: "D65 Reference",
            1: "D65 Enhanced",
            2: "D93",
        }
        if l11_content_type is not None:
            dovi.l11_content_type = CONTENT_TYPE_MAP.get(l11_content_type, f"Type {l11_content_type}")
            dovi.has_l11 = True
            if l11_intended_white is not None:
                dovi.l11_intended_application = INTENDED_WHITE_MAP.get(
                    l11_intended_white, f"White {l11_intended_white}"
                )
                if l11_reference_mode == 1:
                    dovi.l11_intended_application += " · Reference"

        # L4 presence — check si hay bloques level=4 en el JSON
        for rpu in rpus:
            vdr = (rpu or {}).get("vdr_dm_data", {}) if isinstance(rpu, dict) else {}
            ext = vdr.get("ext_metadata_blocks") or vdr.get("ext_blocks") or []
            if isinstance(ext, list):
                for b in ext:
                    if isinstance(b, dict):
                        lvl = b.get("level") or b.get("block_type")
                        try:
                            if int(lvl) == 4:
                                dovi.has_l4 = True
                                break
                        except Exception:
                            continue
            if dovi.has_l4: break

        # L254 presence: en JSON export puede no aparecer explicitamente; lo
        # detectamos por la presencia de CMv4.0 (v4.0) y L8+L11, que juntos son
        # el sentinel de CMv4.0 bien marcado.
        if (dovi.cm_version and "4.0" in dovi.cm_version
                and dovi.has_l8 and dovi.has_l11):
            dovi.has_l254 = True

    finally:
        try: json_path.unlink(missing_ok=True)
        except Exception: pass
        try: tmpdir.rmdir()
        except Exception: pass


def _pq_code_to_nits_local(code_value: float) -> float:
    """PQ inverse EOTF — copia local para no depender de main.py."""
    v = max(0.0, min(1.0, code_value / 4095.0))
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 4096.0 * 128.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 4096.0 * 32.0
    c3 = 2392.0 / 4096.0 * 32.0
    vm2 = v ** (1.0 / m2)
    num = max(0.0, vm2 - c1)
    den = c2 - c3 * vm2
    if den <= 0: return 0.0
    return 10000.0 * (num / den) ** (1.0 / m1)


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
