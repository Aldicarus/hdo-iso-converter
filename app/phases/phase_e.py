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
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from models import Chapter, Session
from phases.phase_a import ISO639_TO_ENGLISH
from phases.phase_d import MkvmergePlaylistError, is_playlist_assertion_line

MKVMERGE_BIN    = "mkvmerge"
MKVPROPEDIT_BIN = "mkvpropedit"
OUTPUT_DIR      = os.environ.get("OUTPUT_DIR", "/mnt/output")
# Watchdog: si mkvmerge (con --gui-mode) pasa MKVMERGE_INACTIVITY_S sin emitir
# NINGUNA línea de progreso, lo matamos — un cuelgue no debe bloquear la cola
# FIFO indefinidamente (audit #20). Holgado: un remux activo emite cada pocos s.
MKVMERGE_INACTIVITY_S = 900


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

    # Reordenación real: ¿el orden DESEADO (included_tracks, Castellano
    # primero) coincide con el orden FÍSICO del source? Mapeamos cada pista
    # incluida a su ID por contenido (idioma+codec+canales) y miramos si la
    # secuencia queda ascendente. Si no, hay que reordenar → ruta directa.
    # El antiguo check `positions != sorted(positions)` NO servía: Fase B
    # asigna position=i siempre en orden Castellano-primero, así que las
    # posiciones eran SIEMPRE [0,1,2,…] monótonas y nunca disparaba — los
    # discos con el audio VO físicamente antes del Castellano caían a la
    # ruta propedit posicional y se etiquetaban cruzados (P0 de la auditoría).
    audio_map = _match_tracks_to_source(audio_tracks, mkv_audio_ids, track_map)
    sub_map   = _match_tracks_to_source(sub_tracks,   mkv_sub_ids,   track_map)
    audio_seq = [audio_map[i] for i in range(len(audio_tracks)) if i in audio_map]
    sub_seq   = [sub_map[i]   for i in range(len(sub_tracks))   if i in sub_map]
    return audio_seq != sorted(audio_seq) or sub_seq != sorted(sub_seq)


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
    # session.mkv_name puede contener subdirectorios (modo serie:
    # "Serie/Season 01/Serie - S01E01.mkv"). Creamos el árbol antes de
    # invocar mkvmerge — sin esto, mkvmerge falla si el directorio padre
    # no existe.
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
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
            "[Fase E] 📋 Generando el MKV final con un solo mkvmerge: selección "
            "de pistas, reordenación, nombres, flags y capítulos en una sola pasada. "
            "Ahorra una copia completa respecto a la ruta con intermedio."
        )
        await log_callback(f"[Fase E] ┌─ Escribiendo: {output_path}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if proc_callback:
        proc_callback(proc)
    last_line_at = time.monotonic()
    hung = False
    playlist_assert = False

    async def _watchdog():
        nonlocal hung
        while proc.returncode is None:
            await asyncio.sleep(30)
            if time.monotonic() - last_line_at > MKVMERGE_INACTIVITY_S:
                hung = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return

    wd_task = asyncio.create_task(_watchdog())
    try:
        async for line in proc.stdout:
            last_line_at = time.monotonic()
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            # Bug de mkvmerge con playlists UHD multi-segmento: aborta con un
            # assertion en add_filelists_for_playlists. Lo detectamos para que
            # el orquestador reintente con el M2TS principal directo.
            if is_playlist_assertion_line(text):
                playlist_assert = True
            # --gui-mode: "#GUI#progress 45%" → "Progress: 45%"
            if text.startswith("#GUI#progress "):
                text = "Progress: " + text.removeprefix("#GUI#progress ")
            elif text.startswith("#GUI#"):
                continue
            if log_callback:
                await log_callback(text)
        await proc.wait()
    finally:
        wd_task.cancel()
        try:
            await wd_task
        except asyncio.CancelledError:
            pass

    # mkvmerge terminó (éxito o fallo): el XML de capítulos ya no se necesita.
    if chapters_xml:
        Path(chapters_xml).unlink(missing_ok=True)

    if hung:
        raise RuntimeError(
            f"mkvmerge sin actividad >{MKVMERGE_INACTIVITY_S // 60} min — "
            "abortado (probable cuelgue, no bloquea la cola)"
        )

    if playlist_assert:
        raise MkvmergePlaylistError(
            "mkvmerge abortó al ensamblar la lista de ficheros del playlist "
            "(bug conocido con discos UHD multi-segmento / multi-ángulo)."
        )

    # returncode 0 = OK · 1 = warnings no fatales · resto = fallo. Incluye los
    # códigos negativos por señal (SIGABRT = -6): el `>= 2` antiguo NO los
    # capturaba, el flujo seguía hasta el stat() del output inexistente y
    # reventaba con un críptico "[Errno 2] No such file or directory".
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"mkvmerge terminó de forma anómala (código {proc.returncode})")

    if not Path(output_path).exists():
        raise RuntimeError(f"mkvmerge no generó el MKV final en {output_path}")

    if log_callback:
        size_gb = Path(output_path).stat().st_size / 1e9
        await log_callback(f"[Fase E] ✓ MKV final: {Path(output_path).name} ({size_gb:.1f} GB)")
        await log_callback(
            "[Fase E] 🎯 Resultado: MKV en /mnt/output con pistas, nombres, flags "
            "y capítulos correctos. Ruta directa — una sola copia."
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

    # Metadatos de pistas: mapeo POR CONTENIDO (idioma+codec+canales), igual
    # que la ruta directa. Antes era posicional (`mkv_audio_ids[i]`) y cruzaba
    # las etiquetas en discos con el audio VO físicamente antes del Castellano.
    # mkvpropedit usa índice 1-based.
    for tid, label, flag_default, flag_forced in _propedit_track_edits(session, track_map):
        cmd += ["--edit", f"track:{tid + 1}"]
        cmd += ["--set", f"name={label}"]
        cmd += ["--set", f"flag-default={'1' if flag_default else '0'}"]
        cmd += ["--set", f"flag-forced={'1' if flag_forced else '0'}"]

    if log_callback:
        await log_callback(
            "[Fase E] 📋 Aplicando los metadatos al MKV intermedio con mkvpropedit: "
            "edita solo las cabeceras del contenedor (nombres de pistas, flags "
            "default/forced y capítulos) sin recopiar los datos. El MKV se mueve "
            "después al directorio final con un rename atómico."
        )
        await log_callback(f"[Fase E] ┌─ Editando metadatos en: {Path(intermediate_mkv).name}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if proc_callback:
        proc_callback(proc)
    _, stderr = await proc.communicate()
    if proc.returncode >= 2:
        if chapters_xml:
            Path(chapters_xml).unlink(missing_ok=True)
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
        await log_callback(f"[Fase E] └─ ✓ MKV final: {Path(output_path).name} ({size_gb:.1f} GB)")
        await log_callback(
            "[Fase E] 🎯 Resultado: MKV en /mnt/output con metadatos aplicados sin "
            "recopiar el contenido (solo cabeceras editadas)."
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
            props = track.get("properties", {}) or {}
            track_map[idx] = {
                "type":     track.get("type", ""),
                "codec":    track.get("codec", ""),
                "language": props.get("language", ""),
                # Canales físicos de audio. Crítico para desambiguar
                # dos pistas con mismo idioma+codec pero distinto mix
                # (ej: Castellano DD 2.0 vs Castellano DD 5.1 — ambas
                # son AC-3 spa). Sin esto el matcher cogía siempre la
                # de menor ID independientemente de la selección.
                "audio_channels": props.get("audio_channels", 0) or 0,
            }
        return track_map
    except (json.JSONDecodeError, KeyError):
        return {}


# ══════════════════════════════════════════════════════════════════════
#  MAPEO DE PISTAS INCLUIDAS → IDs DEL SOURCE
# ══════════════════════════════════════════════════════════════════════

# ISO 639-2 → nombre en inglés (lowercase) para el matcher de pistas.
# Se DERIVA del mapa completo de phase_a (ISO639_TO_ENGLISH) en lugar de
# mantener un subset propio. Un subset desincronizado descartaba pistas de
# idiomas que phase_a sí conoce: la pista incluida llega con raw.language en
# inglés (ej. "Catalan") y el source la identifica por código ISO ("cat"); si
# el código no está aquí, _ISO639.get("cat","cat")="cat" ≠ "catalan" → el
# matcher no la encontraba y la pista (catalán, tailandés, griego…) se perdía
# en silencio. Derivarlo garantiza que ambos extremos hablen el mismo idioma.
_ISO639 = {code: name.lower() for code, name in ISO639_TO_ENGLISH.items()}


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


def _parse_channels(description: str) -> int:
    """
    Extrae el número de canales físicos de la descripción de la pista
    incluida (campo raw.description). Formatos típicos:

      ``2.0 / 48 kHz``           → 2
      ``5.1 / 48 kHz / 1509 kbps`` → 6
      ``7.1+11 objects / 48 kHz`` → 8 (cuenta solo el layout físico)
      ``2.0-Atmos / 48 kHz``     → 2 (raro, pero por compat)

    Devuelve 0 si no se puede parsear. Se usa para desambiguar el
    matching entre pistas con mismo idioma+codec pero distinto mix.
    """
    if not description:
        return 0
    # Localiza el primer dígito y parsea M.N a partir de ahí.
    s = description.lstrip()
    if not s or not s[0].isdigit():
        return 0
    i = 0
    while i < len(s) and s[i].isdigit():
        i += 1
    if i == 0:
        return 0
    major_str = s[:i]
    if i >= len(s) or s[i] != ".":
        return 0
    j = i + 1
    while j < len(s) and s[j].isdigit():
        j += 1
    if j == i + 1:
        return 0
    minor_str = s[i + 1:j]
    try:
        return int(major_str) + int(minor_str)
    except ValueError:
        return 0


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
            # Audio: matching por idioma + codec + canales (desambig).
            # Recogemos TODOS los candidatos que matchean lang+codec y
            # entre ellos preferimos el que coincide en canales físicos.
            # Sin esto, dos pistas Castellano AC-3 (DD 2.0 + DD 5.1)
            # se distinguían solo por orden de source ID, así que el
            # matcher siempre cogía la primera (ID menor) ignorando la
            # selección del usuario. Caso real: GoT S01 — usuario
            # selecciona DD 5.1, matcher entrega DD 2.0.
            channels_inc = _parse_channels(getattr(raw, "description", ""))
            candidates: list[tuple[int, int]] = []  # (sid, src_channels)
            for sid in source_ids:
                if sid in used_ids:
                    continue
                src = track_map.get(sid, {})
                src_lang = _ISO639.get(src.get("language", ""), src.get("language", "")).lower()
                if src_lang != lang_inc:
                    continue
                src_codec = src.get("codec", "").lower()
                if _codec_matches(codec_inc, src_codec):
                    candidates.append((sid, src.get("audio_channels", 0) or 0))

            if candidates:
                # 1ª preferencia: canales exactos.
                if channels_inc > 0:
                    for sid, ch in candidates:
                        if ch == channels_inc:
                            best_id = sid
                            break
                # Fallback: primer candidato por orden de source_ids
                # (comportamiento legacy para sesiones sin info de
                # canales en raw.description o sources sin
                # audio_channels en track_map).
                if best_id is None:
                    best_id = candidates[0][0]

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


def _propedit_track_edits(session, track_map: dict) -> list[tuple[int, str, bool, bool]]:
    """Mapea cada pista incluida a su track del MKV intermedio POR CONTENIDO
    (idioma+codec+canales) — igual que la ruta directa vía
    ``_match_tracks_to_source``. Devuelve la lista de ediciones para mkvpropedit:
    ``[(source_track_id, label, flag_default, flag_forced), …]``.

    La ruta propedit antes mapeaba por POSICIÓN (``mkv_audio_ids[i]``); en discos
    con el audio VO físicamente antes del Castellano eso ponía la etiqueta
    'Castellano…' sobre el stream inglés (P0 de la auditoría). Reusar el matcher
    por contenido lo evita y además desambigua por canales (dos Castellano AC-3
    2.0 vs 5.1).
    """
    audio_tracks  = [t for t in session.included_tracks if t.track_type == "audio"]
    sub_tracks    = [t for t in session.included_tracks if t.track_type == "subtitle"]
    mkv_audio_ids = [idx for idx, t in sorted(track_map.items()) if t["type"] == "audio"]
    mkv_sub_ids   = [idx for idx, t in sorted(track_map.items()) if t["type"] == "subtitles"]
    audio_id_map  = _match_tracks_to_source(audio_tracks, mkv_audio_ids, track_map)
    sub_id_map    = _match_tracks_to_source(sub_tracks,   mkv_sub_ids,   track_map)

    edits: list[tuple[int, str, bool, bool]] = []
    for i, track in enumerate(audio_tracks):
        if i in audio_id_map:
            edits.append((audio_id_map[i], track.label, track.flag_default, track.flag_forced))
    for i, track in enumerate(sub_tracks):
        if i in sub_id_map:
            edits.append((sub_id_map[i], track.label, track.flag_default, track.flag_forced))
    return edits


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
