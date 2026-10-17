"""Lanzador de la suite: aísla los directorios de la app y vigila los skips.

    python3 app/tests/correr_suite.py               # toda la suite, -v
    python3 app/tests/correr_suite.py --estricto    # además: un skip no previsto falla (CI)
    python3 app/tests/correr_suite.py test_firma test_relato   # solo esos módulos

Dos agujeros que la auditoría de la suite del 2026-10-17 encontró, y que este
fichero cierra en un sitio:

  · **Los directorios de la app se resuelven del entorno AL IMPORTAR**
    (`storage.CONFIG_DIR`, `paths.TMP_DIR`…). La suite entera funcionaba
    porque un módulo que fija `CONFIG_DIR` se importa de los primeros; con un
    subconjunto de tests, `test_mkv_cache` intentó escribir en el `/config`
    real, que dentro del contenedor es el de verdad. Aquí se fijan TODOS antes
    de importar nada, a una caja temporal, y se quitan del entorno las claves
    y los ajustes que cambian el comportamiento (idioma, tema, claves de API).
  · **El CI no fallaba si se saltaban tests.** Hoy se salta uno y es
    legítimo, pero si un día faltara Chrome se saltarían unos sesenta módulos
    y la ejecución seguiría en verde. Con `--estricto`, un skip que no está en
    `SKIPS_PREVISTOS` hace fallar la ejecución, y una entrada de esa lista que
    no corresponde a ningún test también: una excepción vieja parece cobertura.
"""
import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent

#: Directorios que la app resuelve del entorno al importar.
DIRECTORIOS = ("CONFIG_DIR", "TMP_DIR", "OUTPUT_DIR", "LIBRARY_DIR", "ISOS_DIR",
               "MOUNT_BASE", "CMV40_RPU_DIR")
#: Lo que cambia el comportamiento y no tiene que llegar de la máquina de nadie.
FUERA_DEL_ENTORNO = ("TMDB_API_KEY", "GOOGLE_API_KEY", "CMV40_DRIVE_FOLDER_URL",
                     "CMV40_DRIVE_FOLDER_ID", "CMV40_SHEET_URL", "HDO_IDIOMA",
                     "HDO_TEMA", "HDO_MOSTRAR_AUTORIA", "HDO_FIRMA_CLAVE")

#: Los skips que se esperan en CI (Linux con node y Chrome), con su motivo.
#: La clave es el id del test o un prefijo de él.
SKIPS_PREVISTOS = {
    "test_version_solo_tags_de_release.TestElComportamientoConEsteRepo."
    "test_sin_el_filtro_este_repo_daria_una_version_no_parseable":
        "el contraste solo existe con un tag auxiliar por delante del último de versión",
}


def aislar_entorno() -> Path:
    """Fija los directorios a una caja temporal. Se llama ANTES de importar
    cualquier módulo de la app."""
    caja = Path(tempfile.mkdtemp(prefix="suite_"))
    for nombre in DIRECTORIOS:
        d = caja / nombre.lower()
        d.mkdir()
        os.environ[nombre] = str(d)
    for nombre in FUERA_DEL_ENTORNO:
        os.environ.pop(nombre, None)
    return caja


def _ids(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from _ids(t)
        else:
            yield t.id()


def _previsto(test_id: str) -> bool:
    return any(test_id.startswith(k) for k in SKIPS_PREVISTOS)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("modulos", nargs="*", help="módulos de app/tests; vacío = todos")
    ap.add_argument("--estricto", action="store_true",
                    help="un skip no previsto (o una excepción vieja) hace fallar")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    aislar_entorno()
    sys.path.insert(0, str(TESTS))
    sys.path.insert(0, str(TESTS.parent))
    os.chdir(TESTS.parent.parent)
    cargador = unittest.TestLoader()
    if args.modulos:
        suite = cargador.loadTestsFromNames(args.modulos)
    else:
        suite = cargador.discover(str(TESTS))
    todos = set(_ids(suite))

    res = unittest.TextTestRunner(verbosity=1 if args.quiet else 2).run(suite)

    problemas = []
    if args.estricto:
        for test, motivo in res.skipped:
            if not _previsto(test.id()):
                problemas.append(f"skip no previsto: {test.id()} — {motivo}")
        if not args.modulos:
            for k in SKIPS_PREVISTOS:
                if not any(t.startswith(k) for t in todos):
                    problemas.append(f"SKIPS_PREVISTOS cita un test que no existe: {k}")
    else:
        for test, motivo in res.skipped:
            print(f"  skip: {test.id()} — {motivo}")
    for p in problemas:
        print(f"::error::{p}" if os.environ.get("GITHUB_ACTIONS") else f"✗ {p}")
    return 0 if res.wasSuccessful() and not problemas else 1


if __name__ == "__main__":
    sys.exit(main())
