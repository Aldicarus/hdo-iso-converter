"""
iso_mount.py — Montaje directo de ISOs UHD Blu-ray via loop device

Responsabilidad:
  Montar y desmontar ISOs dentro del contenedor Docker usando
  ``mount -t udf -o ro,loop`` sin depender de APIs externas (QTS).

─────────────────────────────────────────────────────────────────────
REQUISITOS
─────────────────────────────────────────────────────────────────────

  - Docker con ``privileged: true`` (necesario para loop devices)
  - Kernel del host con módulo ``udf.ko`` (soporte UDF 2.50+)
  - ISOs de UHD Blu-ray usan UDF 2.50/2.60 — solo el driver del
    kernel lo soporta; herramientas userspace (fuseiso, 7z, pycdlib)
    no leen UDF 2.50.

─────────────────────────────────────────────────────────────────────
FLUJO DE MONTAJE
─────────────────────────────────────────────────────────────────────

  1. mount_iso(iso_path) → crea directorio temporal en MOUNT_BASE
     → ``mount -t udf -o ro,loop {iso} {mount_point}``
     → devuelve ruta del mount point con BDMV/

  2. (proceso: BDInfoCLI / mkvmerge leen desde el mount point)

  3. unmount_iso(mount_point) → ``umount {mount_point}``
     → limpia el directorio temporal. Siempre en finally.

─────────────────────────────────────────────────────────────────────
DIRECTORIO DE MONTAJE
─────────────────────────────────────────────────────────────────────

  Los ISOs se montan en MOUNT_BASE/{iso_stem}_{pid}/ para evitar
  colisiones si dos procesos montan el mismo ISO simultáneamente
  (poco probable en cola FIFO, pero seguro por diseño).

  MOUNT_BASE por defecto: /mnt/bd (creado en entrypoint.sh).
"""
import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

MOUNT_BASE = Path(os.environ.get("MOUNT_BASE", "/mnt/bd"))


async def mount_iso(iso_path: str) -> str:
    """
    Monta un ISO UDF como directorio de solo lectura via loop device.

    Args:
        iso_path: Ruta absoluta al fichero .iso (dentro de /mnt/isos).

    Returns:
        Ruta del mount point (ej: '/mnt/bd/Movie_2025_12345/').
        BDInfoCLI y mkvmerge leen desde esta ruta.

    Raises:
        RuntimeError: Si el ISO no existe, mount falla, o UDF no soportado.
    """
    iso = Path(iso_path)
    if not iso.exists():
        raise RuntimeError(f"ISO no encontrado: {iso_path}")
    if iso.suffix.lower() != ".iso":
        raise RuntimeError(f"Fichero no es un ISO: {iso_path}")

    # Crear mount point único
    mount_name = f"{iso.stem}_{os.getpid()}"
    mount_point = MOUNT_BASE / mount_name
    mount_point.mkdir(parents=True, exist_ok=True)

    cmd = ["mount", "-t", "udf", "-o", "ro,loop", str(iso), str(mount_point)]
    logger.info("[iso_mount] Montando: %s → %s", iso.name, mount_point)

    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True
    )

    if result.returncode != 0:
        # Limpiar directorio si mount falló
        shutil.rmtree(mount_point, ignore_errors=True)
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"Error montando ISO ({result.returncode}): {stderr}. "
            f"¿El contenedor tiene privileged: true? ¿El kernel soporta UDF 2.50?"
        )

    # Verificar que el BDMV es accesible
    bdmv = mount_point / "BDMV"
    if not bdmv.exists():
        await unmount_iso(str(mount_point))
        raise RuntimeError(
            f"ISO montado pero no contiene BDMV/ en {mount_point}. "
            f"¿Es un ISO de UHD Blu-ray válido?"
        )

    logger.info("[iso_mount] Montado OK: %s (%s)", iso.name, mount_point)
    return str(mount_point)


async def unmount_iso(mount_point: str) -> None:
    """
    Desmonta un ISO previamente montado y limpia el directorio.

    Seguro de llamar en ``finally``: no lanza excepciones.
    Si el desmontaje falla, logea un warning pero no interrumpe.

    Args:
        mount_point: Ruta devuelta por mount_iso().
    """
    mp = Path(mount_point)
    if not mp.exists():
        return

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["umount", str(mp)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning(
                "[iso_mount] umount falló (%d): %s — intentando lazy unmount",
                result.returncode, result.stderr.strip(),
            )
            # Intentar lazy unmount como fallback
            await asyncio.to_thread(
                subprocess.run,
                ["umount", "-l", str(mp)],
                capture_output=True, text=True,
            )
    except Exception as e:
        logger.warning("[iso_mount] Error en unmount: %s", e)

    # Limpiar directorio (puede fallar si aún montado — no es fatal)
    try:
        if mp.exists() and not os.path.ismount(str(mp)):
            shutil.rmtree(mp, ignore_errors=True)
    except Exception:
        pass

    logger.info("[iso_mount] Desmontado: %s", mount_point)


def is_mount_available() -> bool:
    """
    Verifica que el sistema puede montar ISOs UDF.
    Comprueba que ``mount`` existe y que MOUNT_BASE es escribible.
    """
    if shutil.which("mount") is None:
        return False
    try:
        MOUNT_BASE.mkdir(parents=True, exist_ok=True)
        return os.access(str(MOUNT_BASE), os.W_OK)
    except Exception:
        return False
