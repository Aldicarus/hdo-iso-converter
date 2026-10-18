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
from i18n import t as tr
import asyncio
import json
import os
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from models import Chapter, Session
from phases.phase_a import (ISO639_TO_ENGLISH, _find_subordinate_track_ids,
                            esperar_con_tope)
from phases.phase_d import (
    MkvmergePlaylistError, _limpiar_parcial, is_playlist_assertion_line,
)

MKVMERGE_BIN    = "mkvmerge"
MKVPROPEDIT_BIN = "mkvpropedit"
OUTPUT_DIR      = os.environ.get("OUTPUT_DIR", "/mnt/output")
# Topes de los dos pasos cortos de la fase: un `--identify` sobre el MPLS y un
# `mkvpropedit`, que solo toca cabeceras. Generosos a propósito —son para que
# un proceso colgado no deje el rip esperando para siempre, no un presupuesto—
# y el `--identify` de un MPLS en un montaje UDF con el pool ocupado puede
# llevar decenas de segundos.
IDENTIFY_TIMEOUT = 600
PROPEDIT_TIMEOUT = 600
# Watchdog: si mkvmerge (con --gui-mode) pasa MKVMERGE_INACTIVITY_S sin emitir
# NINGUNA línea de progreso, lo matamos — un cuelgue no debe bloquear la cola
# FIFO indefinidamente (audit #20). Holgado: un remux activo emite cada pocos s.
MKVMERGE_INACTIVITY_S = 900


def ruta_de_salida(mkv_name: str) -> Path:
    """La ruta del MKV final bajo `OUTPUT_DIR`, o `ValueError` si el nombre
    sale de ahí.

    `mkv_name` es editable por el usuario y puede llevar subdirectorios (el
    modo serie: `Serie/Season 01/…mkv`), pero no `..` ni una ruta absoluta:
    `../biblioteca/Peli.mkv` acababa en la escritura, el rename y la limpieza
    de un parcial fuera de /mnt/output (auditoría con Codex del 2026-10-17).
    Regla del proyecto: `resolve()` + `relative_to()`."""
    nombre = (mkv_name or "").strip()
    rel = Path(nombre)
    if not nombre or rel.is_absolute() or ".." in rel.parts:
        raise ValueError(nombre)
    base = Path(OUTPUT_DIR).resolve()
    try:
        (base / rel).resolve().relative_to(base)
    except (OSError, RuntimeError) as e:
        raise ValueError(nombre) from e
    return Path(OUTPUT_DIR) / rel


def _salida_de(session: Session) -> str:
    try:
        return str(ruta_de_salida(session.mkv_name))
    except ValueError:
        raise RuntimeError(tr('phase_e.nombre_de_salida_no_valido',
                              nombre=session.mkv_name)) from None


def _nombre_de_trabajo(output_path: str) -> str:
    """El nombre con el que se escribe el MKV final hasta que está terminado.

    En el MISMO directorio, para que el paso al definitivo sea un rename
    atómico. Es el `.mkv.tmp` que ya usa la Fase G de CMv4.0, así que el panel
    de Limpieza lo reconoce como remux incompleto si se queda huérfano, y el
    recovery de arranque (`routers.tab1._borrar_salida_a_medias`) lo borra.
    """
    return output_path + ".tmp"


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


def _el_origen_tiene_cores(session) -> bool:
    """¿El disco trae un TrueHD con su núcleo AC-3 multiplexado?

    Lo dice el `mkvmerge -J` que guardó la Fase A, con la misma función con
    la que las dos puntas quitan los cores."""
    bd = getattr(session, "bdinfo_result", None)
    raw = ((bd.mkvmerge_raw if bd is not None else None) or {}).get("tracks") or []
    return bool(_find_subordinate_track_ids(raw))


def _check_reordering(session: Session, track_map: dict) -> bool:
    """Lógica pura de detección de reordenación."""
    # Con cores AC-3, SIEMPRE por la ruta directa. El intermedio es un mux sin
    # selección, así que saca cada núcleo como pista propia —un MKV ya no lleva
    # `multiplexed_tracks`—, y `mkvpropedit` no puede quitar pistas: el MKV
    # final salía con los AC-3 de más. Las dos rutas cuestan una sola copia de
    # los datos, así que esto no tiene precio. Ningún disco del corpus ha
    # caído aquí (los que traen cores tienen más audio y ya iban por la
    # directa), pero un disco con solo castellano e inglés TrueHD, y todos sus
    # subtítulos incluidos, lo haría.
    if _el_origen_tiene_cores(session):
        return True
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
    audio_map = _match_tracks_to_source(audio_tracks, mkv_audio_ids, track_map,
                                        disco=pistas_del_disco(session, "audio"))
    sub_map   = _match_tracks_to_source(sub_tracks,   mkv_sub_ids,   track_map,
                                        disco=pistas_del_disco(session, "subtitle"))
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
    output_path  = _salida_de(session)
    # session.mkv_name puede contener subdirectorios (modo serie:
    # "Serie/Season 01/Serie - S01E01.mkv"). Creamos el árbol antes de
    # invocar mkvmerge — sin esto, mkvmerge falla si el directorio padre
    # no existe.
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    # mkvmerge escribe en un nombre de TRABAJO del mismo directorio, y solo al
    # terminar bien pasa al definitivo con un rename atómico. Escribiendo
    # directamente al definitivo, re-ejecutar una sesión sobrescribía el MKV
    # bueno de la pasada anterior desde el primer byte, y si mkvmerge moría a
    # mitad el parcial se quedaba con ese nombre: indistinguible de un rip
    # terminado. Así, un fallo deja el MKV de antes donde estaba.
    work_path    = _nombre_de_trabajo(output_path)
    track_map    = await _identify_tracks(mpls_path, log_callback)
    chapters_xml = _write_chapters_xml(session.chapters) if session.chapters else None

    avisos_matcher: list[str] = []
    cmd = _build_mkvmerge_cmd(
        source_path=mpls_path,
        output_path=work_path,
        session=session,
        track_map=track_map,
        chapters_xml=chapters_xml,
        avisos=avisos_matcher,
    )
    for aviso in avisos_matcher:
        await _avisar_empate(log_callback, aviso)
    if log_callback:
        await log_callback(
            '[Fase B] ' + tr('phase_e.generando_el_mkv_final_con_un')
        )
        await log_callback('[Fase B] ┌─ ' + tr('phase_e.escribiendo', output_path=output_path))

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

    # Cualquier salida que no sea un éxito deja el fichero de trabajo a medias.
    if hung or playlist_assert or proc.returncode not in (0, 1):
        borrado = _limpiar_parcial(work_path)
        if borrado and log_callback:
            await log_callback('[Fase B] ' + tr('phase_e.mkv_parcial_eliminado', borrado=borrado))

    if hung:
        raise RuntimeError(
            tr('phase_d.mkvmerge_sin_actividad_min_abortado_probable', p1=MKVMERGE_INACTIVITY_S // 60)
        )

    if playlist_assert:
        raise MkvmergePlaylistError(
            tr('phase_d.mkvmerge_aborto_al_ensamblar_la_lista')
        )

    # returncode 0 = OK · 1 = warnings no fatales · resto = fallo. Incluye los
    # códigos negativos por señal (SIGABRT = -6): el `>= 2` antiguo NO los
    # capturaba, el flujo seguía hasta el stat() del output inexistente y
    # reventaba con un críptico "[Errno 2] No such file or directory".
    if proc.returncode not in (0, 1):
        raise RuntimeError(tr('phase_d.mkvmerge_termino_de_forma_anomala_codigo', returncode=proc.returncode))

    if not Path(work_path).exists():
        raise RuntimeError(tr('phase_e.mkvmerge_no_genero_el_mkv_final', output_path=output_path))
    os.replace(work_path, output_path)

    if log_callback:
        size_gb = Path(output_path).stat().st_size / 1e9
        await log_callback('[Fase B] ' + tr('phase_e.mkv_final_gb', p1=Path(output_path).name, p2=format(size_gb, '.1f')))
        await log_callback(
            '[Fase B] 🎯 Resultado' + tr('phase_e.mkv_en_mnt_output_con_pistas')
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
    output_path  = _salida_de(session)
    track_map    = await _identify_tracks(intermediate_mkv, log_callback)
    chapters_xml = _write_chapters_xml(session.chapters) if session.chapters else None

    cmd = [MKVPROPEDIT_BIN, intermediate_mkv]

    # Título del contenedor
    container_title = session.mkv_name.removesuffix(".mkv")
    cmd += ["--edit", "info", "--set", f"title={container_title}"]

    # Capítulos. Sin ninguno en la sesión, se BORRAN: el intermedio trae los
    # del disco (la Fase D no los quita) y una sesión con la lista vacía es
    # que el usuario los quitó todos. Callar aquí los dejaba dentro
    # (auditoría con Codex del 2026-10-17). `--chapters ''` es como
    # mkvpropedit borra los capítulos.
    cmd += ["--chapters", chapters_xml or ""]

    # Metadatos de pistas: mapeo POR CONTENIDO (idioma+codec+canales), igual
    # que la ruta directa. Antes era posicional (`mkv_audio_ids[i]`) y cruzaba
    # las etiquetas en discos con el audio VO físicamente antes del Castellano.
    # mkvpropedit usa índice 1-based.
    avisos_matcher: list[str] = []
    for tid, label, flag_default, flag_forced in _propedit_track_edits(
        session, track_map, avisos_matcher
    ):
        cmd += ["--edit", f"track:{tid + 1}"]
        cmd += ["--set", f"name={label}"]
        cmd += ["--set", f"flag-default={'1' if flag_default else '0'}"]
        cmd += ["--set", f"flag-forced={'1' if flag_forced else '0'}"]

    for aviso in avisos_matcher:
        await _avisar_empate(log_callback, aviso)

    if log_callback:
        await log_callback(
            '[Fase C] ' + tr('phase_e.aplicando_los_metadatos_al_mkv_intermedio')
        )
        await log_callback('[Fase C] ┌─ ' + tr('phase_e.editando_metadatos_en', p1=Path(intermediate_mkv).name))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if proc_callback:
        proc_callback(proc)
    try:
        _, stderr = await esperar_con_tope(proc, PROPEDIT_TIMEOUT)
    except asyncio.TimeoutError:
        if chapters_xml:
            Path(chapters_xml).unlink(missing_ok=True)
        raise RuntimeError(tr('phase_e.mkvpropedit_no_termino', segundos=PROPEDIT_TIMEOUT))
    # 0 = OK · 1 = avisos no fatales · resto = fallo, INCLUIDOS los negativos
    # de un proceso muerto por señal. El `>= 2` que había los daba por buenos
    # —el mismo bug que ya se arregló en mkvmerge— y el intermedio, con los
    # metadatos a medio escribir, se entregaba con el nombre definitivo.
    if proc.returncode not in (0, 1):
        if chapters_xml:
            Path(chapters_xml).unlink(missing_ok=True)
        err_text = stderr.decode("utf-8", errors="replace")[:300] if stderr else ""
        raise RuntimeError(
            tr('phase_e.mkvpropedit_fallo_con_codigo', returncode=proc.returncode, err_text=err_text)
        )

    if chapters_xml:
        Path(chapters_xml).unlink(missing_ok=True)

    # Mover al directorio de salida. Pasa por el nombre de trabajo por lo mismo
    # que la ruta directa: /mnt/tmp y /mnt/output son montajes distintos, así
    # que `shutil.move` COPIA, y un fallo a mitad dejaría el parcial con el
    # nombre definitivo encima del MKV de una pasada anterior.
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    work_path = _nombre_de_trabajo(output_path)
    try:
        # En un thread: entre montajes `move` COPIA decenas de GB, y en el
        # bucle eso congelaba la app entera —cancelar, los WS, el
        # healthcheck— (auditoría con Codex del 2026-10-17). Si a la tarea la
        # cancelan, se ESPERA a que el thread termine antes de limpiar: borrar
        # el parcial mientras el thread aún escribe no lo limpiaría.
        copia = asyncio.ensure_future(asyncio.to_thread(shutil.move, intermediate_mkv, work_path))
        try:
            await asyncio.shield(copia)
        except asyncio.CancelledError:
            try:
                await copia
            finally:
                _limpiar_parcial(work_path)
            raise
        os.replace(work_path, output_path)
    except BaseException:
        _limpiar_parcial(work_path)
        raise
    if log_callback:
        size_gb = Path(output_path).stat().st_size / 1e9
        await log_callback('[Fase C] └─ ' + tr('phase_e.mkv_final_gb', p1=Path(output_path).name, p2=format(size_gb, '.1f')))
        await log_callback(
            '[Fase C] 🎯 Resultado' + tr('phase_e.mkv_en_mnt_output_con_metadatos')
        )
    return output_path


# ══════════════════════════════════════════════════════════════════════
#  IDENTIFICACIÓN DE PISTAS
# ══════════════════════════════════════════════════════════════════════

async def _identify_tracks(source_path: str, log_callback=None) -> dict:
    """
    Ejecuta ``mkvmerge --identify --identification-format json`` para obtener
    los IDs y tipos de las pistas del source (MPLS o MKV).

    **Excluye los cores AC-3 subordinados de un TrueHD**, con la MISMA función
    que usa Fase A (``_find_subordinate_track_ids``). Sin ese filtro las dos
    puntas discrepaban sobre qué pistas existen en el disco: Fase A los quita
    al construir las ``RawAudioTrack``, así que la "Castellano DD 5.1" que el
    usuario ve descartada es la pista INDEPENDIENTE — pero el matcher los veía
    aquí, y el core suele llevar el ID menor, así que ganaba. Recuperar esa
    pista metía en el MKV el core lossy del TrueHD (audio ya presente, duplicado)
    y la DD 5.1 real no entraba nunca. Medido sobre 41 discos reales: 22 casos
    en 19 discos donde el core gana, 4 de ellos en castellano (La Momia,
    Obsession, Scream 7, Te van a matar). Es la misma clase de divergencia que
    los subtítulos de tres bloques: si las dos puntas no parten de la misma
    base, la selección sale bien y el CONTENIDO sale cruzado.

    Returns:
        Diccionario ``{track_id: {"type": "video"|"audio"|"subtitles", ...}}``.
    """
    proc = await asyncio.create_subprocess_exec(
        MKVMERGE_BIN, "--identify", "--identification-format", "json", source_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await esperar_con_tope(proc, IDENTIFY_TIMEOUT)
    except asyncio.TimeoutError:
        raise RuntimeError(tr('phase_e.identify_no_termino', segundos=IDENTIFY_TIMEOUT))
    if proc.returncode not in (0, 1):
        if log_callback:
            await log_callback(
                '[Pipeline] ' + tr('phase_e.aviso_mkvmerge_identify_devolvio_codigo', returncode=proc.returncode)
            )
        return {}

    try:
        data      = json.loads(stdout.decode("utf-8", errors="replace"))
        tracks    = data.get("tracks", [])
        subordinate_ids = _find_subordinate_track_ids(tracks)
        track_map = {}
        for track in tracks:
            idx = track["id"]
            if idx in subordinate_ids:
                continue
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
            # Bloques: el forzado es la ÚLTIMA aparición del idioma; las
            # anteriores son completas. Con dos bloques esto es idéntico a
            # "primera = completo, posterior = forzado"; con TRES —normal +
            # SDH + forzado, caso The Mandalorian and Grogu— aquella regla
            # metía la SDH en la categoría de forzados y el matcher entregaba
            # el contenido CRUZADO. Tiene que usar la MISMA base que el
            # `bitrate_kbps` sintético de phase_a, o los dos extremos dejan de
            # estar alineados.
            total = sum(1 for _, l in langs_in_order if l == lang)
            if total >= 2 and count == total - 1:
                forced.setdefault(lang, []).append(sid)
            else:
                complete.setdefault(lang, []).append(sid)
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


def _idioma_del_origen(track_map: dict, sid: int) -> str:
    src = track_map.get(sid, {})
    return _ISO639.get(src.get("language", ""), src.get("language", "")).lower()


def _puestos_en_el_disco(included_tracks: list, disco: list | None) -> dict[int, int]:
    """El puesto de cada pista incluida entre las del disco DE SU IDIOMA.

    `disco` es la lista de la Fase A (`bdinfo_result.audio_tracks` o
    `.subtitle_tracks`), que sale del `mkvmerge -J` del mismo origen y en su
    orden. Las pistas incluidas y descartadas llevan COPIAS de esos `raw`, así
    que se reconocen por igualdad: es lo único que distingue dos pistas que el
    resto de señales da por iguales —la normal y la SDH, las dos completas; la
    principal y los comentarios, las dos AC-3 5.1—. Dos `raw` idénticos en
    todo son indistinguibles también aquí, y se toman en orden.

    Devuelve `{índice de la incluida: puesto}`; las que no aparecen en el
    disco (una sesión sin `bdinfo_result`) no salen.
    """
    if not disco:
        return {}
    tomadas: set[int] = set()
    puestos: dict[int, int] = {}
    for i, track in enumerate(included_tracks):
        for j, del_disco in enumerate(disco):
            if j in tomadas or del_disco != track.raw:
                continue
            tomadas.add(j)
            idioma = (del_disco.language or "").lower()
            puestos[i] = sum(1 for d in disco[:j] if (d.language or "").lower() == idioma)
            break
    return puestos


def pistas_del_disco(session, tipo: str) -> list | None:
    """Las pistas de la Fase A de ese tipo, o None en una sesión sin ellas."""
    bd = getattr(session, "bdinfo_result", None)
    if bd is None:
        return None
    return list(bd.audio_tracks if tipo == "audio" else bd.subtitle_tracks)


def _match_tracks_to_source(
    included_tracks: list,
    source_ids: list[int],
    track_map: dict,
    avisos: list[str] | None = None,
    disco: list | None = None,
) -> dict[int, int]:
    """
    Mapea cada pista incluida a su ID en el source por idioma + codec.

    Las pistas incluidas (de Fase B) tienen ``raw.language`` en inglés
    (ej: 'Spanish') y ``raw.codec`` estilo BDInfo (ej: 'Dolby TrueHD/Atmos Audio').
    El track_map del source tiene idiomas ISO 639-2 (ej: 'spa') y codecs
    mkvmerge (ej: 'TrueHD Atmos').

    El matching es por idioma normalizado + subcadena de codec.
    Si un source ID ya fue asignado, no se reutiliza (1:1).

    ``avisos`` (opcional): lista donde se anotan las decisiones que el disco
    NO permite desambiguar — varias pistas del mismo idioma, mismo codec y
    mismos canales. Ahí gana el ID menor porque no hay con qué elegir
    (mkvmerge devuelve ``track_name`` vacío en los orígenes Blu-ray), pero el
    usuario merece enterarse en vez de que se resuelva en silencio. Caso
    típico: los comentarios del director, que son AC-3 5.1 en inglés igual
    que la pista principal.

    ``disco`` (opcional): las pistas de ese tipo tal como las vio la Fase A
    (ver `_puestos_en_el_disco`). **Va primero**: si la pista incluida está ahí
    y el origen tiene, de su idioma, exactamente las mismas que vio la Fase A,
    se toma el id del mismo puesto. Sin eso, dentro de una categoría ganaba el
    primer id libre, y con dos pistas que ninguna otra señal separa —la normal
    y la SDH, o la principal y los comentarios— recuperar la segunda entregaba
    el contenido de la primera con la etiqueta de la segunda. Si el origen no
    cuadra (el M2TS del reintento, otro disco), el puesto no significa nada y
    se usan las reglas de siempre.

    Returns:
        Diccionario {included_index: source_track_id}.
    """
    used_ids: set[int] = set()
    result: dict[int, int] = {}

    # Para subtítulos: detectar el patrón del disco (bloques separados vs
    # adyacentes) y clasificar cada source ID como forzado o completo.
    _sub_forced_ids, _sub_complete_ids = _classify_sub_source_ids(source_ids, track_map)

    puestos = _puestos_en_el_disco(included_tracks, disco)
    en_el_disco: dict[str, int] = {}
    for d in disco or []:
        idioma = (d.language or "").lower()
        en_el_disco[idioma] = en_el_disco.get(idioma, 0) + 1

    for i, track in enumerate(included_tracks):
        raw = track.raw
        lang_inc = raw.language.lower()
        codec_inc = getattr(raw, "codec", "").lower()

        best_id = None

        if i in puestos:
            del_idioma = [sid for sid in source_ids
                          if _idioma_del_origen(track_map, sid) == lang_inc]
            if len(del_idioma) == en_el_disco.get(lang_inc, 0):
                sid = del_idioma[puestos[i]]
                codec_src = track_map.get(sid, {}).get("codec", "").lower()
                # Y en audio, los canales: si el origen trae las pistas del
                # idioma en otro orden —el M2TS del reintento no tiene por qué
                # respetar el del MPLS—, el puesto apunta a la 2.0 cuando la
                # incluida es la 5.1 (revisión con Codex de la v3.4.0). Sin
                # canales en un lado no hay con qué contradecir el puesto.
                ch_inc = _parse_channels(getattr(raw, "description", "")) if codec_inc else 0
                ch_src = track_map.get(sid, {}).get("audio_channels", 0) or 0
                canales_ok = not (ch_inc and ch_src and ch_inc != ch_src)
                if (sid not in used_ids and canales_ok
                        and (not codec_inc or _codec_matches(codec_inc, codec_src))):
                    result[i] = sid
                    used_ids.add(sid)
                    continue

        if not codec_inc:
            # Subtítulo: elegir el source id por la identidad ESTRUCTURAL
            # que lleva el propio raw (posición forzado/completo en el
            # disco), NO por track.subtitle_type.
            #
            # Motivo: subtitle_type lo fija la clasificación por paquetes de
            # phase_b y puede venir equivocado — p.ej. un forzado que el
            # muestreo PGS perdió (0 paq. → marcado 'complete'), o una pista
            # recuperada a mano. Renombrar la etiqueta NO cambia
            # subtitle_type, así que el matcher entregaba el contenido
            # CRUZADO (caso real Obsession 2025: la pista 'Castellano
            # Forzados' recibía el PGS completo y viceversa).
            #
            # raw.bitrate_kbps es la señal sintética de phase_a
            # (_detect_subtitle_pattern: <3 kbps = posición de forzado en el
            # disco) — la MISMA base estructural que usa
            # _classify_sub_source_ids sobre el source, así que ambos
            # extremos quedan alineados. En el caso de 1 sola pista del
            # idioma da igual (solo hay un source id que elegir).
            raw_bitrate = getattr(raw, "bitrate_kbps", 30.0)
            is_forced = raw_bitrate < 3.0
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
                exactos = ([sid for sid, ch in candidates if ch == channels_inc]
                           if channels_inc > 0 else [])
                if exactos:
                    best_id = exactos[0]
                else:
                    # Fallback: primer candidato por orden de source_ids
                    # (comportamiento legacy para sesiones sin info de
                    # canales en raw.description o sources sin
                    # audio_channels en track_map).
                    best_id = candidates[0][0]
                # ¿Quedó sin desempatar? Entonces ha ganado el ID menor y no
                # por ningún criterio: hay que decirlo.
                if len(exactos) > 1:
                    empatan = exactos                            # mismos canales
                elif not exactos and len(candidates) > 1:
                    empatan = [sid for sid, _ in candidates]     # sin dato de canales
                else:
                    empatan = []                                 # unívoco o resuelto
                if empatan and avisos is not None:
                    detalle = ", ".join(
                        f"id {sid} ({track_map.get(sid, {}).get('codec', '?')}"
                        f" {track_map.get(sid, {}).get('audio_channels', 0)}ch)"
                        for sid in empatan
                    )
                    avisos.append(
                        tr('phase_e.p1_encaja_con_p2_pistas_del_disco', p1=getattr(track, 'label', raw.language), p2=len(empatan), detalle=detalle, best_id=best_id)
                    )

        if best_id is not None:
            result[i] = best_id
            used_ids.add(best_id)

    return result


async def _avisar_empate(log_callback, aviso: str) -> None:
    """Emite al log un empate que el disco no permite desambiguar."""
    if log_callback:
        await log_callback('[Pipeline] ' + '⚠️ ' + str(aviso))


def _codec_matches(included_codec: str, source_codec: str) -> bool:
    """
    Comprueba si un codec de pista incluida (estilo BDInfo) coincide
    con un codec del source (estilo mkvmerge).

    Ej: 'dolby truehd/atmos audio' matches 'truehd atmos'
        'dolby digital plus audio' matches 'e-ac-3'
        'dolby digital audio' matches 'ac-3'
        'dts-hd master audio' matches 'dts-hd master audio'
        'dts audio' matches 'dts'

    **Es deliberadamente laxo y NO se ha endurecido**, con los números delante.
    Los pares se prueban en orden y se cae al más genérico, así que una pista
    incluida puede cruzar de tier: 'dolby truehd/atmos audio' encaja con
    'e-ac-3 atmos' (por el par atmos) y 'dts-hd master audio' con 'dts' (por el
    par dts). Para que eso hiciera daño harían falta DOS pistas del mismo
    idioma en tiers distintos que compartan la subcadena — y sobre las 41
    sesiones reales del NAS (457 pistas de audio, censo del 2026-09-03) eso
    ocurre **cero veces**: no hay discos con TrueHD Atmos y DD+ Atmos del mismo
    idioma, ni con DTS junto a DTS-HD MA. Endurecerlo pediría una tabla de
    tiers mantenida en paralelo a MKVMERGE_CODEC_TO_BDINFO de phase_a — otra
    réplica que se desincroniza. Si algún día aparece un disco así, el aviso de
    empate de `_match_tracks_to_source` lo dirá en el log.
    """
    # Mapeo de subcadenas: included → source
    pairs = [
        ("truehd", "truehd"),
        ("atmos", "atmos"),
        ("digital plus", "e-ac-3"),
        ("dts-hd master", "dts-hd master"),
        ("dts", "dts"),
        ("dolby digital", "ac-3"),
        # La Fase A traduce el `PCM` de mkvmerge a «LPCM Audio»
        # (`MKVMERGE_CODEC_TO_BDINFO`), y sin este par una pista LPCM incluida
        # no encontraba su origen y se quedaba fuera del MKV.
        ("lpcm", "pcm"),
    ]
    # Intentar match por la cadena más específica primero
    for inc_key, src_key in pairs:
        if inc_key in included_codec and src_key in source_codec:
            return True
    # Fallback: el mismo nombre, con o sin el « audio» que la Fase A añade al
    # traducir (`FLAC` → «FLAC Audio», `AAC` → «AAC Audio»…). Sin quitarlo, lo
    # que la tabla de la Fase A produce no casaba con lo que mkvmerge reporta.
    return (included_codec == source_codec
            or included_codec.removesuffix(" audio") == source_codec)


def _propedit_track_edits(
    session, track_map: dict, avisos: list[str] | None = None,
) -> list[tuple[int, str, bool, bool]]:
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
    audio_id_map  = _match_tracks_to_source(audio_tracks, mkv_audio_ids, track_map, avisos,
                                            disco=pistas_del_disco(session, "audio"))
    sub_id_map    = _match_tracks_to_source(sub_tracks,   mkv_sub_ids,   track_map,
                                            disco=pistas_del_disco(session, "subtitle"))

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
    avisos: list[str] | None = None,
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
    audio_id_map = _match_tracks_to_source(audio_tracks, mkv_audio_ids, track_map, avisos,
                                           disco=pistas_del_disco(session, "audio"))
    sub_id_map   = _match_tracks_to_source(sub_tracks,   mkv_sub_ids,   track_map,
                                           disco=pistas_del_disco(session, "subtitle"))

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

    # Los capítulos del source (MPLS) nunca: o se duplicarían con los de
    # `--chapters`, o —con la sesión sin capítulos— el MKV heredaría los del
    # disco que el usuario quitó. Solo se pasaba con XML (auditoría con Codex
    # del 2026-10-17).
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
