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
from pathlib import Path

MKVMERGE_BIN  = "mkvmerge"
MKVEXTRACT_BIN = "mkvextract"
MIN_MPLS_SIZE = 200  # 200 bytes — ISOs custom tienen MPLS muy pequeños (~1.5 KB)


# ══════════════════════════════════════════════════════════════════════
#  ENTRADA PRINCIPAL
# ══════════════════════════════════════════════════════════════════════

async def run_phase_d(
    share_path: str,
    tmp_dir: str = "/mnt/tmp",
    log_callback=None,
    proc_callback=None,
) -> str:
    """
    Extrae el título principal del BDMV montado al MKV intermedio usando mkvmerge.

    Busca el MPLS de mayor tamaño en share_path/BDMV/PLAYLIST/ y lanza
    mkvmerge para extraer todas las pistas (vídeo, audio, subtítulos) y
    los capítulos al MKV intermedio.

    Args:
        share_path:   Ruta al directorio raíz del disco montado via loop mount
                      (ej: '/mnt/bd/Movie_2025_1'). Debe contener BDMV/.
        tmp_dir:      Directorio de salida para el MKV intermedio.
        log_callback: Corutina opcional ``async def(str)`` para streaming
                      de output en tiempo real.

    Returns:
        Ruta absoluta al MKV intermedio generado en tmp_dir.

    Raises:
        RuntimeError: Si no se encuentran ficheros MPLS, o mkvmerge falla
                      con código ≥ 2 (código 1 = warnings no fatales).
    """
    mpls_path = find_main_mpls(share_path)
    iso_stem  = Path(share_path).name
    out_path  = str(Path(tmp_dir) / f"{iso_stem}_intermediate.mkv")

    cmd = [MKVMERGE_BIN, "--gui-mode", "-o", out_path, mpls_path]

    if log_callback:
        await log_callback(
            "[Fase D] 📋 Plan: extraer el contenido del MPLS principal del disco a "
            "un MKV intermedio en /mnt/tmp, manteniendo todas las pistas. Este "
            "paso usa mkvmerge directamente sobre el MPLS — copia streaming "
            "sin re-encoding. Las reorganizaciones y ediciones se aplicarán en Fase E."
        )
        await log_callback(f"[Fase D] ┌─ MPLS seleccionado: {mpls_path}")
        await log_callback(f"[Fase D] └─ $ {' '.join(cmd)}")

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
            continue  # Descartar otras líneas de control GUI
        if log_callback:
            await log_callback(text)
    await proc.wait()

    # mkvmerge código 1 = warnings no fatales (ej: pista vacía), aceptable
    if proc.returncode >= 2:
        raise RuntimeError(f"mkvmerge falló con código {proc.returncode}")

    if not Path(out_path).exists():
        raise RuntimeError(f"mkvmerge no generó el MKV intermedio en {out_path}")

    if log_callback:
        size_gb = Path(out_path).stat().st_size / 1e9
        await log_callback(f"[Fase D] ✓ MKV intermedio generado: {out_path} ({size_gb:.1f} GB)")
        await log_callback(
            "[Fase D] 🎯 Resultado: MKV temporal con TODAS las pistas del MPLS. "
            "Fase E lo procesará con mkvpropedit (nombres, flags, capítulos) o hará "
            "remux selectivo si hay que reordenar o excluir pistas."
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
