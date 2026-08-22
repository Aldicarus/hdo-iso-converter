"""
paths.py — Los directorios de la aplicación, en un solo sitio.

Estaban repartidos por `main.py`: `ISOS_DIR`, `TMP_DIR` y `CONFIG_DIR` arriba,
`OUTPUT_DIR_MKV` y `LIBRARY_DIR` dos mil setecientas líneas más abajo junto a
`LIBRARY_ROOTS`. Mientras todo vivía en el mismo módulo daba igual; al partir
`main.py` en routers por pestaña deja de dar igual, porque los tres los
necesitan y **el router no debe importar `main`** — la dependencia va en un solo
sentido, como se estableció al sacar Tab 3.

`LIBRARY_ROOTS` es la lista blanca de raíces que el file browser expone y contra
la que se valida cualquier ruta que llegue del cliente. Qué subconjunto usa cada
pestaña lo decide el frontend en su llamada.

**Los tests parchean estos atributos por módulo** (`paths.ISOS_DIR = tmp`), no
por variable de entorno: el entorno llegaría tarde, porque estas constantes se
resuelven en el import y un módulo solo se importa una vez por proceso. Ver
`tests/api_harness.py`.
"""
import os
from pathlib import Path

# ISOs, carpetas BDMV y m2ts de origen (montado read-only).
ISOS_DIR = Path(os.environ.get("ISOS_DIR", "/mnt/isos"))

# MKV intermedios y workdirs de las operaciones largas. Preferiblemente SSD.
TMP_DIR = os.environ.get("TMP_DIR", "/mnt/tmp")

# Sesiones persistentes, caches y `app_settings.json`.
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))

# MKVs finales: salida de Tab 1, entrada de Tab 2 y Tab 3.
OUTPUT_DIR_MKV = Path(os.environ.get("OUTPUT_DIR", "/mnt/output"))

# Biblioteca de MKVs ya consolidados (read-only en docker-compose).
LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/mnt/library"))

# Las raíces que el file browser puede exponer, y la lista blanca contra la que
# se valida cualquier ruta del cliente.
LIBRARY_ROOTS: dict[str, Path] = {
    "library":    LIBRARY_DIR,
    "output":     OUTPUT_DIR_MKV,
    "downloaded": ISOS_DIR,
}

# Directorios que NUNCA se exponen en el browser:
#   - .zfs/snapshot — snapshots ocultos de ZFS en QuTS hero (recursivos eternos)
#   - @eaDir, .DS_Store, Thumbs.db — metadata de Synology/macOS/Windows
#   - .Recycle, #recycle, $RECYCLE.BIN — papeleras varias
LIBRARY_HIDDEN_DIRS = {
    ".zfs", "@eaDir", ".DS_Store", ".Recycle", "#recycle",
    "$RECYCLE.BIN", "@Recycle", ".Trash", "lost+found",
}
