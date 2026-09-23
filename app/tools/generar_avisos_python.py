#!/usr/bin/env python3
"""Genera el apéndice de avisos de las dependencias Python.

Las licencias MIT/BSD exigen que su aviso de copyright acompañe a la
distribución binaria, y la imagen lleva unos 35 paquetes entre los 7
directos de `requirements.txt` y su cierre transitivo. Escribir esa lista a
mano es garantizar que se quede vieja en el primer `pip install` — es el
mismo fallo que el subset de `_ISO639` o la lista de idiomas del frontend.

Se lee de los metadatos YA INSTALADOS, que es la única fuente que sabe qué
hay de verdad. Ejecutar contra el intérprete de la imagen antes de publicar:

    docker exec CONTENEDOR python3 /app/tools/generar_avisos_python.py

o en local contra el venv:

    .venv/bin/python app/tools/generar_avisos_python.py \
        > app/static/licenses/PYTHON-DEPENDENCIES.md
"""
from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

# pip y setuptools son herramientas del constructor: no se importan en
# runtime y no forman parte de lo que la app ejecuta.
FUERA = {"pip", "setuptools", "wheel", "pkg_resources"}

_COPYRIGHT = re.compile(r"^\s*(copyright\s.*)$", re.IGNORECASE | re.MULTILINE)
_ES_AVISO = re.compile(r"\(c\)|©|\b(19|20)\d{2}\b", re.IGNORECASE)


def _licencia(dist: metadata.Distribution) -> str:
    """El nombre de la licencia, de donde el paquete lo haya puesto.

    Los metadatos modernos usan `License-Expression` (SPDX); los viejos, un
    `License:` libre o un clasificador. Se prueban en ese orden.
    """
    md = dist.metadata
    if expr := md.get("License-Expression"):
        return expr.strip()
    lic = (md.get("License") or "").strip()
    if lic and "\n" not in lic and len(lic) < 60:
        return lic
    for clas in md.get_all("Classifier") or []:
        if clas.startswith("License :: "):
            return clas.rsplit(" :: ", 1)[-1]
    return "(sin declarar — ver el fichero de licencia del paquete)"


def _copyright(dist: metadata.Distribution) -> str:
    """La primera línea de copyright de su fichero de licencia.

    Es lo que MIT y BSD obligan a conservar; sin ella el aviso no vale.
    """
    # NOTICE primero: Apache-2.0 pone ahí el aviso real y en el LICENSE
    # solo la plantilla, que habla del copyright en abstracto.
    ficheros = sorted(
        (f for f in dist.files or []
         if Path(str(f)).name.lower().startswith(("notice", "license", "copying"))),
        key=lambda f: 0 if Path(str(f)).name.lower().startswith("notice") else 1,
    )
    for fichero in ficheros:
        # OJO: `PackagePath.read_text` NO acepta `errors=`, y con un
        # `except Exception` alrededor el TypeError se traga en silencio y
        # la columna de copyright sale vacía para TODOS los paquetes.
        try:
            texto = fichero.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _COPYRIGHT.finditer(texto):
            linea = " ".join(m.group(1).split())
            # Un aviso de verdad lleva año o símbolo. Sin ese filtro se
            # cuela la plantilla de Apache-2.0 ("copyright notice that is
            # included in…") y la del Unlicense ("copyright law."), que
            # parecen un dato y no lo son.
            if not _ES_AVISO.search(linea):
                continue
            return linea[:120]
    return "—"


def main() -> int:
    dists = {}
    for dist in metadata.distributions():
        nombre = dist.metadata["Name"]
        if not nombre or nombre.lower() in FUERA:
            continue
        dists[nombre] = dist

    print("# Dependencias Python — licencias y avisos de copyright")
    print()
    print("**Fichero generado.** No editar a mano: lo produce")
    print("`tools/generar_avisos_python.py` leyendo los metadatos instalados.")
    print("Regenerar contra la imagen publicada cada vez que cambie")
    print("`app/requirements.txt`.")
    print()
    print(f"Paquetes: **{len(dists)}**. Intérprete: Python "
          f"{sys.version_info.major}.{sys.version_info.minor}."
          f"{sys.version_info.micro}.")
    print()
    print("Un `—` en la columna de copyright significa que el paquete **no**")
    print("incluye línea de aviso: o su licencia no la lleva (MPL-2.0,")
    print("Unlicense) o no distribuye fichero de licencia. No es un hueco")
    print("del generador — se comprobó paquete a paquete.")
    print()
    print("| Paquete | Versión | Licencia | Copyright |")
    print("|---|---|---|---|")
    for nombre in sorted(dists, key=str.lower):
        d = dists[nombre]
        print(f"| `{nombre}` | {d.version} | {_licencia(d)} "
              f"| {_copyright(d).replace('|', '\\|')} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
