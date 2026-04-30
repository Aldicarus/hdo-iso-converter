"""
phase_e.py — Fase E: Escritura final (mkvmerge o mkvpropedit)

Responsabilidad:
  Producir el MKV final en /mnt/output con las pistas correctas,
  el orden deseado, los nombres de pista, los flags y los capítulos.

─────────────────────────────────────────────────────────────────────
FLUJO BIFURCADO OPTIMIZADO
─────────────────────────────────────────────────────────────────────

La decisión se toma ANTES de la extracción, no después:

  Sin reordenación ni pistas excluidas:
    Phase D: MPLS → MKV intermedio (todas las pistas)
    Phase E: mkvpropedit in-place + mv al output
    Total: 1 copia de datos

  Con reordenación o pistas excluidas:
    Phase D: SE SALTA (no se genera intermedio)
    Phase E: mkvmerge MPLS → MKV final directamente
    con selección + reorden + metadatos + capítulos
    Total: 1 copia de datos

─────────────────────────────────────────────────────────────────────
MAPEO DE PISTAS
─────────────────────────────────────────────────────────────────────

Las pistas incluidas se mapean a IDs del source por contenido
(idioma + codec), no por posición. Esto es necesario porque las
pistas incluidas son un subconjunto seleccionado del disco.

  - Audio: coincidencia por idioma (ISO 639-2 → inglés) + subcadenas de codec
  - Subtítulos: solo por idioma (no tienen codec en RawSubtitleTrack)
  - Vídeo: todos los tracks de vídeo se incluyen siempre
"""
import asyncio
import json
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from models import Chapter, Session

MKVMERGE_BIN    = "mkvmerge"
MKVPROPEDIT_BIN = "mkvpropedit"
OUTPUT_DIR      = os.environ.get("OUTPUT_DIR", "/mnt/output")


# ══════════════════════════════════════════════════════════════════════
#  DETECCIÓN DE REORDENACIÓN (usada por el pipeline ANTES de Phase D)
# ══════════════════════════════════════════════════════════════════════

async def needs_reordering(session: Session, source_path: str, log_callback=None) -> bool:
    """
    Determina si el usuario reordenó o excluyó pistas respecto al source.

    El source puede ser un MPLS o un MKV intermedio — en ambos casos se
    usa mkvmerge --identify para obtener el track map del source.

    Devuelve True (→ ruta directa) si:
      - Alguna pista de audio o subtítulos fue excluida.
      - El orden de las pistas incluidas difiere del orden natural.

    Devuelve False (→ ruta intermedio + mkvpropedit) si todo está en orden.
    """
    track_map = await _identify_tracks(source_path, log_callback)
    return _check_reordering(session, track_map)


def _check_reordering(session: Session, track_map: dict) -> bool:
    """Lógica pura de detección de reordenación."""
    mkv_audio_ids = [idx for idx, t in sorted(track_map.items()) if t["type"] == "audio"]
    mkv_sub_ids   = [idx for idx, t in sorted(track_map.items()) if t["type"] == "subtitles"]

    audio_tracks = [t for t in session.included_tracks if t.track_type == "audio"]
    sub_tracks   = [t for t in session.included_tracks if t.track_type == "subtitle"]

    # Si hay menos pistas incluidas que en el source → se excluyeron
    if len(audio_tracks) < len(mkv_audio_ids) or len(sub_tracks) < len(mkv_sub_ids):
        return True

    # Si los valores de position no son monotónicamente crecientes → reordenación
    positions = [t.position for t in session.included_tracks]
    return positions != sorted(positions)


# ══════════════════════════════════════════════════════════════════════
#  RUTA A: DIRECTA — mkvmerge MPLS → MKV final (con reordenación)
# ══════════════════════════════════════════════════════════════════════

async def run_phase_e_direct(
    session: Session,
    mpls_path: str,
    log_callback=None,
    proc_callback=None,
) -> str:
    """
    Produce el MKV final directamente desde el MPLS, sin MKV intermedio.

    Se usa cuando hay reordenación o pistas excluidas. Un solo paso de
    mkvmerge hace selección + reorden + metadatos + capítulos.

    Args:
        session:    Sesión con pistas y capítulos a aplicar.
        mpls_path:  Ruta al fichero MPLS principal del disco montado.
        log_callback: Corutina opcional para streaming.

    Returns:
        Ruta absoluta al MKV final en /mnt/output.
    """
    output_path  = str(Path(OUTPUT_DIR) / session.mkv_name)
    track_map    = await _identify_tracks(mpls_path, log_callback)
    chapters_xml = _write_chapters_xml(session.chapters) if session.chapters else None

    cmd = _build_mkvmerge_cmd(
        source_path=mpls_path,
        output_path=output_path,
        session=session,
        track_map=track_map,
        chapters_xml=chapters_xml,
    )
    if log_callback:
        await log_callback(
            "[Fase E] 📋 Plan: generar el MKV final DIRECTAMENTE desde el MPLS con "
            "un solo mkvmerge — seleccionamos las pistas, les aplicamos nombres/flags "
            "correctos y escribimos capítulos en una sola pasada. Esta ruta se elige "
            "cuando hay reordenación o pistas excluidas, evitando el MKV intermedio."
        )
        await log_callback(f"[Fase E] ┌─ Escribiendo MKV final directo desde MPLS → {output_path}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if proc_callback:
        proc_callback(proc)
    async for line in proc.stdout:
        text = line.decode("utf-8", errors="replace").rstrip()
        if not text:
            continue
        # --gui-mode: "#GUI#progress 45%" → "Progress: 45%"
        if text.startswith("#GUI#progress "):
            text = "Progress: " + text.removeprefix("#GUI#progress ")
        elif text.startswith("#GUI#"):
            continue
        if log_callback:
            await log_callback(text)
    await proc.wait()

    if proc.returncode >= 2:
        raise RuntimeError(f"mkvmerge falló con código {proc.returncode}")

    if chapters_xml:
        Path(chapters_xml).unlink(missing_ok=True)

    if log_callback:
        size_gb = Path(output_path).stat().st_size / 1e9
        await log_callback(f"[Fase E] ✓ MKV final escrito: {output_path} ({size_gb:.1f} GB)")
        await log_callback(
            "[Fase E] 🎯 Resultado: MKV listo en /mnt/output con las pistas "
            "seleccionadas, nombres y flags correctos, y capítulos embebidos. "
            "Ruta directa (sin intermedio) — ahorro de una copia completa."
        )
    return output_path


# ══════════════════════════════════════════════════════════════════════
#  RUTA B: INTERMEDIO — mkvpropedit in-place (sin reordenación)
# ══════════════════════════════════════════════════════════════════════

async def run_phase_e_propedit(
    session: Session,
    intermediate_mkv: str,
    log_callback=None,
    proc_callback=None,
) -> str:
    """
    Edita metadatos in-place con mkvpropedit y mueve el MKV al output.

    Se usa cuando todas las pistas están incluidas y en orden original.
    Operación O(1) en tamaño — no copia datos de pistas.

    Args:
        session:          Sesión con pistas y capítulos a aplicar.
        intermediate_mkv: Ruta al MKV generado por Phase D.
        log_callback:     Corutina opcional para streaming.

    Returns:
        Ruta absoluta al MKV final en /mnt/output.
    """
    output_path  = str(Path(OUTPUT_DIR) / session.mkv_name)
    track_map    = await _identify_tracks(intermediate_mkv, log_callback)
    chapters_xml = _write_chapters_xml(session.chapters) if session.chapters else None

    cmd = [MKVPROPEDIT_BIN, intermediate_mkv]

    # Título del contenedor
    container_title = session.mkv_name.removesuffix(".mkv")
    cmd += ["--edit", "info", "--set", f"title={container_title}"]

    # Capítulos
    if chapters_xml:
        cmd += ["--chapters", chapters_xml]

    # Metadatos de pistas de audio
    mkv_audio_ids = [idx for idx, t in sorted(track_map.items()) if t["type"] == "audio"]
    audio_tracks  = [t for t in session.included_tracks if t.track_type == "audio"]
    for i, track in enumerate(audio_tracks):
        if i < len(mkv_audio_ids):
            tid = mkv_audio_ids[i]
            cmd += ["--edit", f"track:{tid + 1}"]   # mkvpropedit usa índice 1-based
            cmd += ["--set", f"name={track.label}"]
            cmd += ["--set", f"flag-default={'1' if track.flag_default else '0'}"]
            cmd += ["--set", f"flag-forced={'1'  if track.flag_forced  else '0'}"]

    # Metadatos de pistas de subtítulos
    mkv_sub_ids = [idx for idx, t in sorted(track_map.items()) if t["type"] == "subtitles"]
    sub_tracks  = [t for t in session.included_tracks if t.track_type == "subtitle"]
    for i, track in enumerate(sub_tracks):
        if i < len(mkv_sub_ids):
            tid = mkv_sub_ids[i]
            cmd += ["--edit", f"track:{tid + 1}"]
            cmd += ["--set", f"name={track.label}"]
            cmd += ["--set", f"flag-default={'1' if track.flag_default else '0'}"]
            cmd += ["--set", f"flag-forced={'1'  if track.flag_forced  else '0'}"]

    if log_callback:
        await log_callback(
            "[Fase E] 📋 Plan: aplicar ediciones de metadatos (nombres de pistas, "
            "flags default/forced, capítulos) sobre el MKV intermedio de Fase D con "
            "mkvpropedit in-place (O(1), sin copiar datos). Luego se mueve al output "
            "final con un rename atómico. Ahorro: una copia completa del fichero."
        )
        await log_callback(f"[Fase E] ┌─ mkvpropedit in-place sobre: {intermediate_mkv}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if proc_callback:
        proc_callback(proc)
    _, stderr = await proc.communicate()
    if proc.returncode >= 2:
        err_text = stderr.decode("utf-8", errors="replace")[:300] if stderr else ""
        raise RuntimeError(
            f"mkvpropedit falló con código {proc.returncode}: {err_text}"
        )

    if chapters_xml:
        Path(chapters_xml).unlink(missing_ok=True)

    # Mover al directorio de salida
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(intermediate_mkv, output_path)
    if log_callback:
        size_gb = Path(output_path).stat().st_size / 1e9
        await log_callback(f"[Fase E] └─ ✓ MKV movido a ubicación final: {output_path} ({size_gb:.1f} GB)")
        await log_callback(
            "[Fase E] 🎯 Resultado: MKV final con metadatos aplicados sin re-copiar "
            "el stream. Ruta con intermedio — mkvpropedit es O(1), solo edita headers."
        )
    return output_path


# ══════════════════════════════════════════════════════════════════════
#  IDENTIFICACIÓN DE PISTAS
# ══════════════════════════════════════════════════════════════════════

async def _identify_tracks(source_path: str, log_callback=None) -> dict:
    """
    Ejecuta ``mkvmerge --identify --identification-format json`` para obtener
    los IDs y tipos de las pistas del source (MPLS o MKV).

    Returns:
        Diccionario ``{track_id: {"type": "video"|"audio"|"subtitles", ...}}``.
    """
    proc = await asyncio.create_subprocess_exec(
        MKVMERGE_BIN, "--identify", "--identification-format", "json", source_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode not in (0, 1):
        if log_callback:
            await log_callback(
                f"[Fase E] Aviso: mkvmerge --identify devolvió código {proc.returncode}"
            )
        return {}

    try:
        data      = json.loads(stdout.decode("utf-8", errors="replace"))
        track_map = {}
        for track in data.get("tracks", []):
            idx = track["id"]
            track_map[idx] = {
                "type":     track.get("type", ""),
                "codec":    track.get("codec", ""),
                "language": track.get("properties", {}).get("language", ""),
            }
        return track_map
    except (json.JSONDecodeError, KeyError):
        return {}


# ══════════════════════════════════════════════════════════════════════
#  MAPEO DE PISTAS INCLUIDAS → IDs DEL SOURCE
# ══════════════════════════════════════════════════════════════════════

# ISO 639-2 → nombre en inglés (subset del mapa completo de phase_a)
_ISO639 = {
    "spa": "spanish", "eng": "english", "fre": "french", "fra": "french",
    "ger": "german", "deu": "german", "ita": "italian", "jpn": "japanese",
    "por": "portuguese", "chi": "chinese", "zho": "chinese", "kor": "korean",
    "dut": "dutch", "nld": "dutch", "rus": "russian", "pol": "polish",
    "cze": "czech", "ces": "czech", "hun": "hungarian", "swe": "swedish",
    "nor": "norwegian", "dan": "danish", "fin": "finnish", "tur": "turkish",
}


def _classify_sub_source_ids(
    source_ids: list[int],
    track_map: dict,
) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """
    Clasifica los IDs de subtítulos del source como forzados o completos,
    detectando automáticamente el patrón del disco.

    Patrón 1 (bloques separados): primera aparición = completo, posterior = forzado.
    Patrón 2 (adyacentes): primera aparición = forzado, segunda = completo.

    Returns:
        Tupla (forced_ids_by_lang, complete_ids_by_lang).
    """
    # Recoger idiomas en orden
    langs_in_order = []
    for sid in source_ids:
        src = track_map.get(sid, {})
        lang = _ISO639.get(src.get("language", ""), src.get("language", "")).lower()
        langs_in_order.append((sid, lang))

    # Detectar patrón: ¿la primera repetición es adyacente?
    seen: set[str] = set()
    pattern = "none"
    for i, (sid, lang) in enumerate(langs_in_order):
        if lang in seen:
            # ¿Es adyacente? (el anterior tiene el mismo idioma)
            if i > 0 and langs_in_order[i - 1][1] == lang:
                pattern = "adjacent"
            else:
                pattern = "blocks"
            break
        seen.add(lang)

    forced: dict[str, list[int]] = {}
    complete: dict[str, list[int]] = {}
    lang_seen_count: dict[str, int] = {}

    for sid, lang in langs_in_order:
        count = lang_seen_count.get(lang, 0)

        if pattern == "adjacent":
            # Adyacente: primera = forzado, segunda = completo
            if count == 0:
                # ¿Tiene duplicado? Si solo aparece una vez → completo
                total = sum(1 for _, l in langs_in_order if l == lang)
                if total >= 2:
                    forced.setdefault(lang, []).append(sid)
                else:
                    complete.setdefault(lang, []).append(sid)
            else:
                complete.setdefault(lang, []).append(sid)
        elif pattern == "blocks":
            # Bloques: primera = completo, posterior = forzado
            if count == 0:
                complete.setdefault(lang, []).append(sid)
            else:
                forced.setdefault(lang, []).append(sid)
        else:
            # Sin patrón: todo completo
            complete.setdefault(lang, []).append(sid)

        lang_seen_count[lang] = count + 1

    return forced, complete


def _match_tracks_to_source(
    included_tracks: list,
    source_ids: list[int],
    track_map: dict,
) -> dict[int, int]:
    """
    Mapea cada pista incluida a su ID en el source por idioma + codec.

    Las pistas incluidas (de Fase B) tienen ``raw.language`` en inglés
    (ej: 'Spanish') y ``raw.codec`` estilo BDInfo (ej: 'Dolby TrueHD/Atmos Audio').
    El track_map del source tiene idiomas ISO 639-2 (ej: 'spa') y codecs
    mkvmerge (ej: 'TrueHD Atmos').

    El matching es por idioma normalizado + subcadena de codec.
    Si un source ID ya fue asignado, no se reutiliza (1:1).

    Returns:
        Diccionario {included_index: source_track_id}.
    """
    used_ids: set[int] = set()
    result: dict[int, int] = {}

    # Para subtítulos: detectar el patrón del disco (bloques separados vs
    # adyacentes) y clasificar cada source ID como forzado o completo.
    _sub_forced_ids, _sub_complete_ids = _classify_sub_source_ids(source_ids, track_map)

    for i, track in enumerate(included_tracks):
        raw = track.raw
        lang_inc = raw.language.lower()
        codec_inc = getattr(raw, "codec", "").lower()

        best_id = None

        if not codec_inc:
            # Subtítulo: usar tipo para elegir ID correcto
            is_forced = getattr(track, "subtitle_type", "") == "forced"
            # Buscar primero en la categoría correcta
            primary = _sub_forced_ids if is_forced else _sub_complete_ids
            for sid in primary.get(lang_inc, []):
                if sid not in used_ids:
                    best_id = sid
                    break
            # Fallback: buscar en la otra categoría
            if best_id is None:
                secondary = _sub_complete_ids if is_forced else _sub_forced_ids
                for sid in secondary.get(lang_inc, []):
                    if sid not in used_ids:
                        best_id = sid
                        break
            # Último recurso: cualquier ID del idioma
            if best_id is None:
                for sid in source_ids:
                    if sid in used_ids:
                        continue
                    src = track_map.get(sid, {})
                    src_lang = _ISO639.get(src.get("language", ""), src.get("language", "")).lower()
                    if src_lang == lang_inc:
                        best_id = sid
                        break
        else:
            # Audio: matching por idioma + codec
            for sid in source_ids:
                if sid in used_ids:
                    continue
                src = track_map.get(sid, {})
                src_lang = _ISO639.get(src.get("language", ""), src.get("language", "")).lower()
                if src_lang != lang_inc:
                    continue
                src_codec = src.get("codec", "").lower()
                if _codec_matches(codec_inc, src_codec):
                    best_id = sid
                    break

        if best_id is not None:
            result[i] = best_id
            used_ids.add(best_id)

    return result


def _codec_matches(included_codec: str, source_codec: str) -> bool:
    """
    Comprueba si un codec de pista incluida (estilo BDInfo) coincide
    con un codec del source (estilo mkvmerge).

    Ej: 'dolby truehd/atmos audio' matches 'truehd atmos'
        'dolby digital plus audio' matches 'e-ac-3'
        'dolby digital audio' matches 'ac-3'
        'dts-hd master audio' matches 'dts-hd master audio'
        'dts audio' matches 'dts'
    """
    # Mapeo de subcadenas: included → source
    pairs = [
        ("truehd", "truehd"),
        ("atmos", "atmos"),
        ("digital plus", "e-ac-3"),
        ("dts-hd master", "dts-hd master"),
        ("dts", "dts"),
        ("dolby digital", "ac-3"),
    ]
    # Intentar match por la cadena más específica primero
    for inc_key, src_key in pairs:
        if inc_key in included_codec and src_key in source_codec:
            return True
    # Fallback: si ambos son el mismo string
    return included_codec == source_codec


# ══════════════════════════════════════════════════════════════════════
#  CONSTRUCCIÓN DEL COMANDO MKVMERGE
# ══════════════════════════════════════════════════════════════════════

def _build_mkvmerge_cmd(
    source_path: str,
    output_path: str,
    session: Session,
    track_map: dict,
    chapters_xml: str | None,
) -> list[str]:
    """
    Construye la lista de argumentos para mkvmerge.

    source_path puede ser un MPLS (ruta directa) o un MKV intermedio.
    En ambos casos el mapeo de pistas es por posición relativa.
    """
    cmd: list[str] = [MKVMERGE_BIN, "--gui-mode", "-o", output_path]

    # Título del contenedor MKV (sin extensión)
    container_title = session.mkv_name.removesuffix(".mkv")
    cmd += ["--title", container_title]

    # Capítulos
    if chapters_xml:
        cmd += ["--chapters", chapters_xml]

    # Separar pistas por tipo
    audio_tracks = [t for t in session.included_tracks if t.track_type == "audio"]
    sub_tracks   = [t for t in session.included_tracks if t.track_type == "subtitle"]

    # IDs del source por tipo
    mkv_video_ids = [idx for idx, t in sorted(track_map.items()) if t["type"] == "video"]
    mkv_audio_ids = [idx for idx, t in sorted(track_map.items()) if t["type"] == "audio"]
    mkv_sub_ids   = [idx for idx, t in sorted(track_map.items()) if t["type"] == "subtitles"]

    video_id = mkv_video_ids[0] if mkv_video_ids else 0

    # Mapeo por contenido: cada pista incluida se busca en el source
    # por coincidencia de idioma + codec (no por posición)
    audio_id_map = _match_tracks_to_source(audio_tracks, mkv_audio_ids, track_map)
    sub_id_map   = _match_tracks_to_source(sub_tracks,   mkv_sub_ids,   track_map)

    # --track-order: vídeos + audio mapeado + subs mapeados
    track_order = [f"0:{vid}" for vid in mkv_video_ids]
    track_order += [f"0:{audio_id_map[i]}" for i in range(len(audio_tracks)) if i in audio_id_map]
    track_order += [f"0:{sub_id_map[i]}"   for i in range(len(sub_tracks))   if i in sub_id_map]
    cmd += ["--track-order", ",".join(track_order)]

    # NO especificar --video-tracks: mkvmerge incluye todos los tracks de
    # vídeo por defecto y maneja la señalización Dolby Vision (DOVI config
    # record, RPU linkage BL↔EL) correctamente. Especificarlo explícitamente
    # puede interferir con el procesado interno de DV dual-layer.

    # Selección de pistas de audio
    included_audio_ids = set(audio_id_map.values())
    if included_audio_ids:
        cmd += ["--audio-tracks", ",".join(str(i) for i in sorted(included_audio_ids))]
    else:
        cmd += ["--no-audio"]

    # Selección de subtítulos
    included_sub_ids = set(sub_id_map.values())
    if included_sub_ids:
        cmd += ["--subtitle-tracks", ",".join(str(i) for i in sorted(included_sub_ids))]
    else:
        cmd += ["--no-subtitles"]

    # Metadatos de audio
    for i, track in enumerate(audio_tracks):
        if i not in audio_id_map:
            continue
        tid = audio_id_map[i]
        cmd += ["--track-name",   f"{tid}:{track.label}"]
        cmd += ["--default-track", f"{tid}:{'yes' if track.flag_default else 'no'}"]
        cmd += ["--forced-track",  f"{tid}:{'yes' if track.flag_forced  else 'no'}"]

    # Metadatos de subtítulos
    for i, track in enumerate(sub_tracks):
        if i not in sub_id_map:
            continue
        tid = sub_id_map[i]
        cmd += ["--track-name",   f"{tid}:{track.label}"]
        cmd += ["--default-track", f"{tid}:{'yes' if track.flag_default else 'no'}"]
        cmd += ["--forced-track",  f"{tid}:{'yes' if track.flag_forced  else 'no'}"]

    # Suprimir capítulos del source (MPLS) para que no se dupliquen
    # con los que pasamos via --chapters
    if chapters_xml:
        cmd.append("--no-chapters")
    cmd.append(source_path)
    return cmd


# ══════════════════════════════════════════════════════════════════════
#  GENERACIÓN DE XML DE CAPÍTULOS
# ══════════════════════════════════════════════════════════════════════

def _write_chapters_xml(chapters: list[Chapter]) -> str:
    """
    Serializa la lista de capítulos a un fichero XML en formato Matroska.

    Returns:
        Ruta al fichero XML temporal. El llamador es responsable de borrarlo.
    """
    root    = ET.Element("Chapters")
    edition = ET.SubElement(root, "EditionEntry")

    for ch in chapters:
        atom    = ET.SubElement(edition, "ChapterAtom")
        ET.SubElement(atom, "ChapterTimeStart").text  = ch.timestamp
        ET.SubElement(atom, "ChapterFlagHidden").text = "0"
        ET.SubElement(atom, "ChapterFlagEnabled").text= "1"
        display = ET.SubElement(atom, "ChapterDisplay")
        ET.SubElement(display, "ChapterString").text  = ch.name
        ET.SubElement(display, "ChapterLanguage").text= "spa"

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    tmp = tempfile.NamedTemporaryFile(
        suffix=".xml", prefix="chapters_", delete=False, mode="wb"
    )
    tree.write(tmp, encoding="utf-8", xml_declaration=True)
    tmp.close()
    return tmp.name
