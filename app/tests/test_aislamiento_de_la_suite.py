"""La suite no puede depender del orden en que se importan sus módulos.

Los directorios de la app se resuelven del entorno AL IMPORTAR, y cuatro
módulos fijaban `os.environ["CONFIG_DIR"]` antes de importar `storage`: eso
solo funciona si nadie lo importó antes. La suite entera pasaba porque uno de
ellos se importa de los primeros; con un subconjunto, `test_mkv_cache` acabó
escribiendo en el `/config` real (auditoría del 2026-10-17). Hoy los
directorios se redirigen parcheando los atributos ya resueltos
(`aislamiento.directorios_aislados`, `api_harness`), y para la suite entera
los fija `correr_suite.py` antes de importar nada.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_aislamiento_de_la_suite -v
"""
import ast
import re
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
APP = TESTS.parent
RAIZ = APP.parent
sys.path.insert(0, str(TESTS))

import correr_suite  # noqa: E402


def _nivel_de_modulo(arbol):
    """Las sentencias que se ejecutan al importar (no las de dentro de una
    función o una clase)."""
    for nodo in arbol.body:
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from ast.walk(nodo)


def _es_os_environ(nodo):
    return (isinstance(nodo, ast.Attribute) and nodo.attr == "environ"
            and isinstance(nodo.value, ast.Name) and nodo.value.id == "os")


class TestNingunModuloFijaLosDirectoriosAlImportarse(unittest.TestCase):

    def test_ni_por_asignacion_ni_por_setdefault(self):
        culpables = []
        for f in sorted(TESTS.glob("test_*.py")):
            for n in _nivel_de_modulo(ast.parse(f.read_text(encoding="utf-8"))):
                clave = None
                if (isinstance(n, ast.Assign) and n.targets
                        and isinstance(n.targets[0], ast.Subscript)
                        and _es_os_environ(n.targets[0].value)):
                    clave = n.targets[0].slice
                elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr in ("setdefault", "update", "__setitem__")
                      and _es_os_environ(n.func.value) and n.args):
                    clave = n.args[0]
                if isinstance(clave, ast.Constant) and clave.value in correr_suite.DIRECTORIOS:
                    culpables.append(f"{f.name}:{n.lineno} {clave.value}")
        self.assertEqual(culpables, [],
                         "fijar un directorio en el entorno al importar depende del orden: "
                         "usa aislamiento.directorios_aislados en setUpModule")


class TestCadaRutaDerivadaSeRedirige(unittest.TestCase):
    """Una constante de módulo como `X = CONFIG_DIR / "y.json"` se resuelve en
    el import: redirigir `CONFIG_DIR` después no la mueve. `api_harness` las
    parchea una a una, y cuando se olvida una —el estado del apply de Tab 2,
    las cachés de TMDb y DoviTools— el test escribe en el /config real."""

    DIRECTORIOS = {"CONFIG_DIR", "TMP_DIR", "OUTPUT_DIR", "OUTPUT_DIR_MKV",
                   "LIBRARY_DIR", "ISOS_DIR"}

    def _derivadas(self):
        for f in sorted(APP.rglob("*.py")):
            if "tests" in f.parts or "tools" in f.parts:
                continue
            arbol = ast.parse(f.read_text(encoding="utf-8"))
            for n in arbol.body:
                objetivo = None
                if isinstance(n, ast.Assign) and len(n.targets) == 1:
                    objetivo, valor = n.targets[0], n.value
                elif isinstance(n, ast.AnnAssign) and n.value is not None:
                    objetivo, valor = n.target, n.value
                if not isinstance(objetivo, ast.Name) or not isinstance(valor, ast.BinOp):
                    continue
                nombres = {x.id for x in ast.walk(valor) if isinstance(x, ast.Name)}
                nombres |= {x.attr for x in ast.walk(valor) if isinstance(x, ast.Attribute)}
                if nombres & self.DIRECTORIOS:
                    yield f.relative_to(APP), objetivo.id

    def test_api_harness_las_redirige_todas(self):
        arnes = (TESTS / "api_harness.py").read_text(encoding="utf-8")
        derivadas = list(self._derivadas())
        self.assertTrue(derivadas, "el barrido no encuentra ninguna: mira el vacío")
        faltan = [f"{f}:{nombre}" for f, nombre in derivadas
                  if f'"{nombre}"' not in arnes]
        self.assertEqual(faltan, [])


class TestElLanzadorAislaTodosLosDirectorios(unittest.TestCase):

    def test_cada_directorio_que_la_app_lee_del_entorno(self):
        """Uno nuevo que no estuviera en la lista se resolvería al de verdad."""
        leidos = set()
        for f in APP.rglob("*.py"):
            if "tests" in f.parts:
                continue
            leidos |= set(re.findall(r'os\.environ\.get\("([A-Z0-9_]*(?:_DIR|_BASE))"',
                                     f.read_text(encoding="utf-8")))
        self.assertTrue(leidos, "el barrido no ha encontrado nada: está mirando el vacío")
        self.assertEqual(sorted(leidos - set(correr_suite.DIRECTORIOS)), [])

    def test_el_ci_usa_el_lanzador_en_modo_estricto(self):
        wf = (RAIZ / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("app/tests/correr_suite.py --estricto", wf)
        self.assertNotIn("unittest discover", wf,
                         "el discover a pelo no aísla nada ni vigila los skips")

    def test_los_skips_previstos_existen(self):
        """Es lo que el modo estricto comprueba al final, adelantado: una
        entrada vieja parece cobertura y no cubre nada."""
        import unittest as u
        ids = set()

        def _recorrer(s):
            for t in s:
                if isinstance(t, u.TestSuite):
                    _recorrer(t)
                else:
                    ids.add(t.id())
        for k in correr_suite.SKIPS_PREVISTOS:
            modulo = k.split(".")[0]
            _recorrer(u.TestLoader().loadTestsFromName(modulo))
            with self.subTest(k):
                self.assertTrue(any(i.startswith(k) for i in ids), k)


if __name__ == "__main__":
    unittest.main()
