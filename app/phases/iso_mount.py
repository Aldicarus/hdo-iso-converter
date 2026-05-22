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


# ══════════════════════════════════════════════════════════════════════
#  ABSTRACCIÓN "Source" (v2.6+)
#
#  Encapsula el origen del contenido independientemente de si es ISO,
#  carpeta BDMV extraída o ficheros M2TS sueltos. El pipeline de Fase A
#  trabaja contra `Source.bdmv_root` (carpeta con BDMV/ accesible) o
#  `Source.m2ts_paths` cuando no hay BDMV.
#
#  Tipos soportados:
#    - 'iso':        fichero .iso → mount UDF en /mnt/bd/
#    - 'bdmv_folder': directorio con BDMV/ dentro → sin mount
#    - 'm2ts':        uno o varios ficheros .m2ts sueltos → sin mount
# ══════════════════════════════════════════════════════════════════════

from typing import Literal


SourceType = Literal["iso", "bdmv_folder", "m2ts"]


class SourceError(RuntimeError):
    """Error específico al preparar un Source — paths inválidos,
    BDMV no encontrado, etc. Permite que main.py devuelva HTTP 400
    con mensaje claro al usuario en lugar de 500 genérico."""


class Source:
    """Origen del contenido a procesar. Context manager async.

    Uso:
        async with await Source.open(user_path) as src:
            if src.bdmv_root:
                candidates = await identify_episode_candidates(src.bdmv_root)
            elif src.m2ts_paths:
                # Flujo m2ts directo
                ...

    Tras `async with`, el ISO se desmonta automáticamente. Para
    bdmv_folder y m2ts no hay cleanup (no se monta nada).
    """

    def __init__(
        self,
        source_type: SourceType,
        original_path: str,
        bdmv_root: str | None = None,
        m2ts_paths: list[str] | None = None,
    ):
        self.source_type: SourceType = source_type
        self.original_path = original_path
        self.bdmv_root = bdmv_root
        self.m2ts_paths: list[str] = m2ts_paths or []
        self._mounted = False  # solo True si this.source_type == 'iso'

    @staticmethod
    def detect_type(path: str) -> SourceType:
        """Clasifica un path en uno de los 3 tipos según extensión y
        estructura. Raises SourceError si no se puede clasificar.

        Reglas (en orden):
          1. termina en .iso (case-insensitive) Y es fichero → 'iso'
          2. termina en .m2ts (case-insensitive) Y es fichero → 'm2ts'
          3. es directorio Y contiene BDMV/PLAYLIST/ → 'bdmv_folder'
          4. resto → SourceError
        """
        p = Path(path)
        if not p.exists():
            raise SourceError(f"Path no encontrado: {path}")
        if p.is_file():
            ext = p.suffix.lower()
            if ext == ".iso":
                return "iso"
            if ext == ".m2ts":
                return "m2ts"
            raise SourceError(
                f"Extensión no reconocida: {ext} (esperado .iso o .m2ts)"
            )
        if p.is_dir():
            for candidate in [p / "BDMV" / "PLAYLIST", p / "PLAYLIST"]:
                if candidate.exists():
                    return "bdmv_folder"
            raise SourceError(
                f"La carpeta {p.name} no contiene BDMV/PLAYLIST/. "
                f"¿Es una carpeta BDMV extraída válida?"
            )
        raise SourceError(f"Path ni fichero ni directorio: {path}")

    @classmethod
    async def open(cls, path: str, m2ts_paths: list[str] | None = None) -> "Source":
        """Crea una Source detectando el tipo automáticamente.

        Para m2ts multi-archivo (series con varios .m2ts), pasar lista
        completa en `m2ts_paths`. `path` puede ser entonces el primero
        de la lista (referencia para fingerprint, logs).

        NOTA: este factory NO entra todavía al context manager — eso
        ocurre cuando se hace `async with src:`.
        """
        if m2ts_paths:
            # Lista explícita de m2ts (caso serie con varios episodios)
            for mp in m2ts_paths:
                if not Path(mp).exists():
                    raise SourceError(f"M2TS no encontrado: {mp}")
                if Path(mp).suffix.lower() != ".m2ts":
                    raise SourceError(f"No es .m2ts: {mp}")
            return cls(
                source_type="m2ts",
                original_path=path or m2ts_paths[0],
                m2ts_paths=list(m2ts_paths),
            )

        # Detectar automáticamente
        stype = cls.detect_type(path)
        if stype == "iso":
            return cls(source_type="iso", original_path=path)
        if stype == "bdmv_folder":
            return cls(
                source_type="bdmv_folder",
                original_path=path,
                bdmv_root=path,
            )
        if stype == "m2ts":
            return cls(
                source_type="m2ts",
                original_path=path,
                m2ts_paths=[path],
            )
        raise SourceError(f"Tipo desconocido: {stype}")

    async def __aenter__(self) -> "Source":
        if self.source_type == "iso":
            self.bdmv_root = await mount_iso(self.original_path)
            self._mounted = True
        # bdmv_folder y m2ts no necesitan mount
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._mounted and self.bdmv_root:
            await unmount_iso(self.bdmv_root)
            self._mounted = False


def safe_source_path(user_path: str, allowed_root: str) -> str:
    """Resuelve `user_path` y verifica que cae bajo `allowed_root`.

    Protección path-traversal: el frontend pasa paths relativos al ISOS_DIR
    o absolutos resolvibles. Cualquier intento de '../' o paths fuera del
    root permitido se rechaza.

    Args:
        user_path: path del usuario (relativo o absoluto).
        allowed_root: directorio raíz permitido (ej. '/mnt/isos').

    Returns:
        Path absoluto validado.

    Raises:
        SourceError si el path resuelve fuera de allowed_root.
    """
    root = Path(allowed_root).resolve()
    target = Path(user_path)
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise SourceError(
            f"Path fuera del directorio permitido: {user_path} "
            f"(esperado bajo {allowed_root})"
        )
    return str(target)
