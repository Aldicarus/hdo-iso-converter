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
    """Las sentencias que se ejecutan al importar: las del módulo y las del
    CUERPO de cada clase (que también corren al importar), no las de dentro
    de una función. Las clases se saltaban enteras (revisión de Codex)."""
    for nodo in getattr(arbol, "body", []):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(nodo, ast.ClassDef):
            yield from _nivel_de_modulo(nodo)
            continue
        yield from ast.walk(nodo)


def _claves_fijadas(n):
    """Las claves de entorno que fija el nodo, por las tres formas: `os.environ[k]
    = v`, `os.environ.setdefault(k, v)` y `os.environ.update({k: v} | k=v)`. El
    `update` con un diccionario —su forma habitual— se escapaba."""
    if (isinstance(n, ast.Assign) and n.targets and isinstance(n.targets[0], ast.Subscript)
            and _es_os_environ(n.targets[0].value)):
        return [n.targets[0].slice]
    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and _es_os_environ(n.func.value)):
        if n.func.attr in ("setdefault", "__setitem__") and n.args:
            return [n.args[0]]
        if n.func.attr == "update":
            claves = [ast.Constant(k.arg) for k in n.keywords if k.arg]
            for a in n.args:
                if isinstance(a, ast.Dict):
                    claves += [k for k in a.keys if k is not None]
            return claves
    return []


def _es_os_environ(nodo):
    return (isinstance(nodo, ast.Attribute) and nodo.attr == "environ"
            and isinstance(nodo.value, ast.Name) and nodo.value.id == "os")


class TestNingunModuloFijaLosDirectoriosAlImportarse(unittest.TestCase):

    def test_ni_por_asignacion_ni_por_setdefault(self):
        culpables = []
        for f in sorted(TESTS.glob("test_*.py")):
            for n in _nivel_de_modulo(ast.parse(f.read_text(encoding="utf-8"))):
                for clave in _claves_fijadas(n):
                    if isinstance(clave, ast.Constant) and clave.value in correr_suite.DIRECTORIOS:
                        culpables.append(f"{f.name}:{n.lineno} {clave.value}")
        self.assertEqual(culpables, [],
                         "fijar un directorio en el entorno al importar depende del orden: "
                         "usa aislamiento.directorios_aislados en setUpModule")


class TestElDetectorDeEntornoVeLasTresFormas(unittest.TestCase):
    """El detector, sobre fuente sintética: si deja de ver una forma, el guard
    de arriba pasa en verde vigilando el vacío."""

    def _ve(self, codigo):
        arbol = ast.parse(codigo)
        return sorted(c.value for n in _nivel_de_modulo(arbol) for c in _claves_fijadas(n)
                      if isinstance(c, ast.Constant))

    def test_las_formas(self):
        self.assertEqual(self._ve('import os\nos.environ["CONFIG_DIR"] = "/x"'), ["CONFIG_DIR"])
        self.assertEqual(self._ve('import os\nos.environ.setdefault("TMP_DIR", "/x")'), ["TMP_DIR"])
        self.assertEqual(self._ve('import os\nos.environ.update({"CONFIG_DIR": "/x"})'),
                         ["CONFIG_DIR"])
        self.assertEqual(self._ve('import os\nos.environ.update(TMP_DIR="/x")'), ["TMP_DIR"])

    def test_el_cuerpo_de_una_clase_cuenta_y_una_funcion_no(self):
        self.assertEqual(self._ve('import os\nclass A:\n    os.environ["CONFIG_DIR"] = "/x"'),
                         ["CONFIG_DIR"])
        self.assertEqual(self._ve('import os\ndef f():\n    os.environ["CONFIG_DIR"] = "/x"'), [])


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


class TestTrasElArnesNadaApuntaAlDirectorioOriginal(unittest.TestCase):
    """El guard de arriba busca el NOMBRE de cada ruta derivada en el texto de
    `api_harness`, y eso no prueba que se redirija el atributo correcto: la
    revisión de Codex de la suite (2026-10-17) cambió el módulo de una fila y
    siguió en verde, y un alias importado (`from … import CMV40_WORK_BASE`)
    ni siquiera es una ruta derivada. Este mira el EFECTO: monta el arnés de
    verdad y recorre los módulos de la app cargados; un atributo que siga
    apuntando a un directorio de los originales es un sitio donde un test
    escribiría fuera de su caja. Al escribirlo salieron trece."""

    def test_ninguna_ruta_de_modulo_queda_fuera_de_la_caja(self):
        import importlib
        import os
        import api_harness
        api_harness._load_app()
        for m in ("services.tmdb", "services.rec999_drive", "services.rec999_sheet",
                  "services.settings_store", "phases.mkv_analyze", "phases.cmv40_pipeline",
                  "phases.phase_a", "phases.phase_d", "phases.phase_e", "phases.iso_mount",
                  "historial", "storage", "firma", "queue_manager"):
            importlib.import_module(m)
        import paths
        raices = {str(Path(v)) for v in (paths.CONFIG_DIR, paths.TMP_DIR, paths.OUTPUT_DIR_MKV,
                                         paths.LIBRARY_DIR, paths.ISOS_DIR)}
        raices |= {os.environ[d] for d in correr_suite.DIRECTORIOS if os.environ.get(d)}

        class _Caso(api_harness.ApiTestCase):
            def runTest(self):
                pass

        _Caso.setUpClass()
        caso = _Caso()
        caso.setUp()
        try:
            fuera = []
            for nombre, mod in list(sys.modules.items()):
                f = getattr(mod, "__file__", None)
                if not f or APP not in Path(f).resolve().parents or "tests" in Path(f).parts:
                    continue
                for attr, valor in list(vars(mod).items()):
                    if isinstance(valor, (str, Path)):
                        s = str(valor)
                        if any(s == r or s.startswith(r.rstrip("/") + "/") for r in raices):
                            fuera.append(f"{nombre}.{attr} = {s}")
        finally:
            caso.doCleanups()
        self.assertEqual(fuera, [])


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

    def test_un_modulo_que_no_aporta_tests_se_detecta(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for n in ("test_a.py", "test_b.py", "helpers.py"):
                (Path(d) / n).write_text("")
            ids = ["test_a.T.test_x"]
            self.assertEqual(correr_suite.modulos_sin_tests(ids, Path(d)), ["test_b"])

    def test_hoy_todos_los_modulos_aportan_tests(self):
        """La premisa del trinquete: con la colección real no sobra ninguno."""
        import unittest as u
        ids = []

        def _recorrer(s):
            for x in s:
                if isinstance(x, u.TestSuite):
                    _recorrer(x)
                else:
                    ids.append(x.id())
        _recorrer(u.TestLoader().discover(str(TESTS)))
        self.assertEqual(correr_suite.modulos_sin_tests(ids, TESTS), [])

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
