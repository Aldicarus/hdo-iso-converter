"""
phase_d.py — Fase D: Extracción con mkvmerge desde MPLS

Responsabilidad:
  Leer el playlist principal del BDMV montado (MPLS) con mkvmerge
  y extraer todas las pistas al MKV intermedio en /mnt/tmp.
  Se usa solo en la ruta intermedio (sin reordenación de pistas).

─────────────────────────────────────────────────────────────────────
SELECCIÓN DEL MPLS PRINCIPAL (fallback)
─────────────────────────────────────────────────────────────────────

En condiciones normales, el MPLS se selecciona en Fase A y se
guarda en ``bdinfo_result.main_mpls``. ``find_main_mpls()`` es un
fallback que elige el fichero más grande en BDMV/PLAYLIST/.

Umbral mínimo: 200 bytes (ISOs custom tienen MPLS muy pequeños).

─────────────────────────────────────────────────────────────────────
CAPÍTULOS
─────────────────────────────────────────────────────────────────────

Los capítulos se extraen en Fase A (mkvmerge + mkvextract) y están
disponibles desde la creación del proyecto. Phase D ya no extrae
capítulos — mkvmerge los incluye automáticamente en el MKV.
"""
import asyncio
import os
import subprocess
import json
import time
from pathlib import Path

MKVMERGE_BIN  = "mkvmerge"
MKVEXTRACT_BIN = "mkvextract"
MIN_MPLS_SIZE = 200  # 200 bytes — ISOs custom tienen MPLS muy pequeños (~1.5 KB)
# Watchdog: si mkvmerge (con --gui-mode) pasa MKVMERGE_INACTIVITY_S sin emitir
# NINGUNA línea de progreso, lo consideramos colgado y lo matamos para no
# bloquear la cola FIFO indefinidamente (audit #20). 15 min es muy holgado: un
# remux activo emite progreso cada pocos segundos.
MKVMERGE_INACTIVITY_S = 900


# ══════════════════════════════════════════════════════════════════════
#  FALLO ESPECÍFICO: ASSERTION DE PLAYLIST DE MKVMERGE
# ══════════════════════════════════════════════════════════════════════

class MkvmergePlaylistError(RuntimeError):
    """mkvmerge abortó al ensamblar la lista de ficheros de un playlist MPLS.

    Bug conocido de mkvmerge con ciertos discos UHD Blu-ray (seamless
    branching / multi-ángulo): el assertion
    ``file_names.size() == play_items.size()`` falla en
    ``add_filelists_for_playlists`` y el proceso muere por SIGABRT. Sucede
    solo en el mux real, no en ``mkvmerge -J`` (identify).

    El orquestador la captura para reintentar la extracción pasando el M2TS
    principal directamente (workaround estándar), en lugar del .mpls.
    """


def is_playlist_assertion_line(text: str) -> bool:
    """True si la línea de mkvmerge corresponde al assertion de playlist que
    mata el proceso en discos UHD multi-segmento. Se busca el nombre de la
    función (estable entre versiones) o la expresión del assertion."""
    return "add_filelists_for_playlists" in text or "play_items.size()" in text


def m2ts_covers_title(
    playlist_seconds: float,
    m2ts_seconds: float,
    tol: float = 0.02,
) -> bool:
    """¿El M2TS principal cubre el título completo del playlist?

    Solo devuelve False cuando AMBAS duraciones son medibles (>0) y el M2TS
    es más de ``tol`` (2 %) más corto que el playlist — señal de seamless
    branching real donde el M2TS suelto truncaría la película. Si alguna
    duración es desconocida (0), se asume que cubre: el caso dominante es un
    único M2TS que contiene toda la película, y bloquear por no poder medir
    dejaría sin ripear discos perfectamente válidos.
    """
    if playlist_seconds <= 0 or m2ts_seconds <= 0:
        return True
    return (playlist_seconds - m2ts_seconds) / playlist_seconds <= tol


# ══════════════════════════════════════════════════════════════════════
#  ENTRADA PRINCIPAL
# ══════════════════════════════════════════════════════════════════════

async def run_phase_d(
    share_path: str,
    tmp_dir: str = "/mnt/tmp",
    log_callback=None,
    proc_callback=None,
    source_path: str | None = None,
) -> str:
    """
    Extrae el título principal del origen a un MKV intermedio usando mkvmerge.

    Modos de uso:
      - share_path apunta a la raíz del BDMV (montado o no): se busca el
        MPLS principal automáticamente con find_main_mpls.
      - source_path se pasa explícitamente (m2ts directo o MPLS de un
        episodio concreto en modo serie): se usa tal cual sin buscar.

    Args:
        share_path:   Ruta al directorio raíz del disco montado via loop mount
                      (ej: '/mnt/bd/Movie_2025_1') o de la carpeta BDMV
                      extraída. Vacío "" si source_path se pasa directo.
        tmp_dir:      Directorio de salida para el MKV intermedio.
        log_callback: Corutina opcional ``async def(str)`` para streaming
                      de output en tiempo real.
        source_path:  Path explícito al MPLS o m2ts a extraer. Si está,
                      se ignora share_path para la búsqueda.

    Returns:
        Ruta absoluta al MKV intermedio generado en tmp_dir.

    Raises:
        RuntimeError: Si no se encuentran ficheros MPLS, o mkvmerge falla
                      con código ≥ 2 (código 1 = warnings no fatales).
    """
    if source_path:
        mpls_path = source_path
        # Stem único basado en el origen — m2ts directo tendría stem
        # "00044" (basename sin ext); MPLS de serie sería "00800". Suficiente
        # para diferenciar intermediates de distintas sesiones en /mnt/tmp.
        iso_stem = Path(source_path).stem
    else:
        mpls_path = find_main_mpls(share_path)
        iso_stem  = Path(share_path).name
    out_path  = str(Path(tmp_dir) / f"{iso_stem}_intermediate.mkv")

    cmd = [MKVMERGE_BIN, "--gui-mode", "-o", out_path, mpls_path]

    if log_callback:
        # Plan describe LO QUE VA A HACER ESTA FASE, sin predecir Fase E.
        # "Origen" en vez de "MPLS" porque puede ser un m2ts directo
        # (modo película desde fichero suelto o serie multi-m2ts).
        await log_callback(
            "[Fase D] 📋 Extrayendo todas las pistas del origen a un MKV "
            "intermedio en /mnt/tmp con mkvmerge (lectura directa, sin "
            "re-codificar). La selección y los metadatos se aplican después."
        )
        await log_callback(f"[Fase D] ┌─ Origen: {Path(mpls_path).name}")
        await log_callback(f"[Fase D] └─ $ {' '.join(cmd)}")

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
                continue  # Descartar otras líneas de control GUI
            if log_callback:
                await log_callback(text)
        await proc.wait()
    finally:
        wd_task.cancel()
        try:
            await wd_task
        except asyncio.CancelledError:
            pass

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

    # returncode 0 = OK · 1 = warnings no fatales · resto = fallo. Incluye
    # los códigos negativos por señal (SIGABRT = -6): el `>= 2` antiguo NO
    # los capturaba y el crash se enmascaraba aguas abajo.
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"mkvmerge terminó de forma anómala (código {proc.returncode})")

    if not Path(out_path).exists():
        raise RuntimeError(f"mkvmerge no generó el MKV intermedio en {out_path}")

    if log_callback:
        size_gb = Path(out_path).stat().st_size / 1e9
        await log_callback(f"[Fase D] ✓ Intermedio: {Path(out_path).name} ({size_gb:.1f} GB)")
        await log_callback(
            "[Fase D] 🎯 Resultado: intermedio con todas las pistas del origen, "
            "sin recodificar. Listo para Fase E."
        )

    return out_path


# ══════════════════════════════════════════════════════════════════════
#  SELECCIÓN DEL MPLS PRINCIPAL
# ══════════════════════════════════════════════════════════════════════

def find_main_mpls(share_path: str) -> str:
    """
    Encuentra el fichero MPLS principal del disco (el de mayor tamaño).

    Busca en:
      1. {share_path}/BDMV/PLAYLIST/    (estructura estándar)
      2. {share_path}/PLAYLIST/         (si share_path ya apunta al BDMV)

    Descarta ficheros menores de MIN_MPLS_SIZE (200 bytes) para evitar
    ficheros vacíos o corruptos.

    Raises:
        RuntimeError: Si no se encuentra ningún MPLS válido.
    """
    candidates = [
        Path(share_path) / "BDMV" / "PLAYLIST",
        Path(share_path) / "PLAYLIST",
    ]
    for playlist_dir in candidates:
        if not playlist_dir.exists():
            continue
        mpls_files = [
            p for p in playlist_dir.glob("*.mpls")
            if p.stat().st_size >= MIN_MPLS_SIZE
        ]
        if mpls_files:
            # El MPLS más grande es el título principal
            return str(max(mpls_files, key=lambda p: p.stat().st_size))

    raise RuntimeError(
        f"No se encontraron ficheros MPLS bajo {share_path}. "
        f"Verifica que el disco montado contiene BDMV/PLAYLIST/."
    )


# ══════════════════════════════════════════════════════════════════════
#  EXTRACCIÓN DE CAPÍTULOS DEL MKV INTERMEDIO
# ══════════════════════════════════════════════════════════════════════

def extract_chapters_from_mkv(mkv_path: str) -> list[dict]:
    """
    Extrae los capítulos del MKV intermedio usando mkvmerge --identify.

    mkvmerge ya habrá incluido los capítulos del MPLS en el MKV.
    Este helper los lee para actualizar la sesión con los capítulos reales
    del disco en lugar de los auto-generados de Fase B.

    Returns:
        Lista de dicts ``{"number": int, "timestamp": "HH:MM:SS.mmm", "name": str}``.
        Lista vacía si no hay capítulos o el comando falla.
    """
    result = subprocess.run(
        [MKVEXTRACT_BIN, mkv_path, "chapters", "--simple"],
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1) or not result.stdout.strip():
        return []

    chapters = []
    number   = 1
    current_ts   = None
    current_name = None

    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("CHAPTER") and "NAME" not in line and "=" in line:
            current_ts = line.split("=", 1)[1].strip()
        elif "NAME=" in line and "=" in line:
            current_name = line.split("=", 1)[1].strip()
            if current_ts:
                chapters.append({
                    "number":    number,
                    "timestamp": _normalize_timestamp(current_ts),
                    "name":      current_name or f"Capítulo {number:02d}",
                })
                number     += 1
                current_ts  = None
                current_name = None

    return chapters


def _normalize_timestamp(ts: str) -> str:
    """
    Normaliza timestamp a formato HH:MM:SS.mmm.

    mkvextract --simple devuelve HH:MM:SS.nnnnnnnnn (9 dígitos de nanosegundos).
    Se trunca a 3 dígitos de milisegundos.
    """
    if "." in ts:
        base, frac = ts.rsplit(".", 1)
        return f"{base}.{frac[:3].ljust(3, '0')}"
    return f"{ts}.000"
