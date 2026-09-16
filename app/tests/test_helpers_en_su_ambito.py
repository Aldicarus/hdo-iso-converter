"""Ningún helper se llama fuera del ámbito donde está definido.

`cmv40_preflight_target` —el endpoint que llama el modal de «Nuevo proyecto
CMv4.0»— tenía tres `await _paso(...)` y **ninguna definición de `_paso`**.
Lo dejó así `7a3c52e` (2026-09-09), que añadió el reporte de progreso al otro
bloque de pre-flight —hay dos, casi idénticos— y se olvidó de este.

El fallo era invisible por construcción: la llamada vive dentro de un `try`
cuyo `except` escribe `str(e)` en `session.error_message`, así que el usuario
veía un banner con **`name '_paso' is not defined`** y en el log del
contenedor no había ni un traceback. Seis días después, el primer proyecto
CMv4.0 que alguien creó falló.

Un `NameError` así no lo ve nada: no es un error de sintaxis, `ast.parse`
pasa, y solo se manifiesta si esa rama se ejecuta. Este guard recorre la
cadena de ámbitos y lo dice antes.

Se limita a los nombres que empiezan por `_` —los helpers del proyecto— a
propósito: con todos los nombres habría que modelar los builtins, el
`__future__` y los reexports, y el ruido escondería lo que importa.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_helpers_en_su_ambito -v
"""
import ast
import builtins
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]

# Ficheros que NO se miran: los tests tienen sus propios arneses y ahí un
# nombre suelto es un fallo del test, no de la app.
FUERA = {"tests", "__pycache__"}


def _ligaduras(nodo) -> set[str]:
    """Los nombres que un ámbito liga: args, asignaciones, defs, imports…

    NO se entra en los `def` anidados: sus ligaduras son de SU ámbito, no de
    este. Lo que sí se liga aquí es el NOMBRE del def anidado.
    """
    fuera: set[str] = set()
    args = getattr(nodo, "args", None)
    if isinstance(args, ast.arguments):
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            fuera.add(a.arg)
        for a in (args.vararg, args.kwarg):
            if a:
                fuera.add(a.arg)

    def recorre(n, raiz=False):
        for hijo in ast.iter_child_nodes(n):
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                fuera.add(hijo.name)
                continue          # su interior es otro ámbito
            if isinstance(hijo, ast.Lambda):
                continue
            if isinstance(hijo, (ast.Import, ast.ImportFrom)):
                for al in hijo.names:
                    fuera.add((al.asname or al.name).split(".")[0])
            elif isinstance(hijo, ast.Name) and isinstance(hijo.ctx, ast.Store):
                fuera.add(hijo.id)
            elif isinstance(hijo, (ast.Global, ast.Nonlocal)):
                fuera.update(hijo.names)
            elif isinstance(hijo, ast.ExceptHandler) and hijo.name:
                fuera.add(hijo.name)
            recorre(hijo)

    recorre(nodo, raiz=True)
    return fuera


ANIDADO = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _de_este_ambito(nodo):
    """Los nodos que pertenecen a ESTE ámbito, y los `def` que abre.

    `ast.walk` no sirve: entra en los `def` anidados y entonces sus llamadas
    se comparan contra el ámbito de fuera, donde efectivamente no están
    definidas — el guard marcaba como huérfanas las que sí lo estaban. Hay
    que parar en la frontera de cada función.
    """
    propios, hijas = [], []

    def baja(n):
        for h in ast.iter_child_nodes(n):
            if isinstance(h, ANIDADO):
                hijas.append(h)
                continue
            propios.append(h)
            baja(h)

    baja(nodo)
    return propios, hijas


def _llamadas_sin_ambito(arbol: ast.AST) -> list[tuple[str, int, str]]:
    globales = _ligaduras(arbol) | set(dir(builtins))
    fallos: list[tuple[str, int, str]] = []

    def visita(nodo, pila: list[set[str]], donde: str):
        nueva = pila + [_ligaduras(nodo)]
        visibles = globales.union(*nueva)
        propios, hijas = _de_este_ambito(nodo)
        for x in propios:
            if (isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                    and x.func.id.startswith("_")
                    and x.func.id not in visibles):
                fallos.append((x.func.id, x.lineno, donde))
        for h in hijas:
            visita(h, nueva, getattr(h, "name", "<lambda>"))

    _, hijas = _de_este_ambito(arbol)
    for h in hijas:
        visita(h, [], getattr(h, "name", "<lambda>"))
    return fallos


class TestNingunHelperSeLlamaFueraDeSuAmbito(unittest.TestCase):

    def _fuentes(self):
        for p in sorted(APP_DIR.rglob("*.py")):
            if any(parte in FUERA for parte in p.relative_to(APP_DIR).parts):
                continue
            yield p

    def test_ninguna_llamada_a_un_helper_invisible(self):
        fallos = []
        for p in self._fuentes():
            arbol = ast.parse(p.read_text(encoding="utf-8"))
            for nombre, linea, donde in _llamadas_sin_ambito(arbol):
                fallos.append(
                    f"{p.relative_to(APP_DIR)}:{linea}: `{nombre}()` en "
                    f"`{donde}` no está definido en ningún ámbito visible")
        self.assertEqual(fallos, [], (
            f"\\n{len(fallos)} llamada(s) a un helper que no existe en su "
            f"ámbito. Es un `NameError`\\nque solo se ve si esa rama se "
            f"ejecuta:\\n  · " + "\\n  · ".join(fallos[:12])))


class TestElGuardDetectaElCasoReal(unittest.TestCase):
    """Sin esto, el guard podría estar midiendo el vacío."""

    FUENTE = '''
import asyncio

async def bien():
    async def _paso(p):
        pass
    await _paso(1)

async def mal():
    await _paso(1)
'''

    def test_ve_la_llamada_huerfana_y_no_la_buena(self):
        fallos = _llamadas_sin_ambito(ast.parse(self.FUENTE))
        self.assertEqual([(n, d) for n, _, d in fallos], [("_paso", "mal")])

    def test_un_helper_del_modulo_no_se_marca(self):
        fuente = "def _ayuda():\n    pass\n\ndef usa():\n    _ayuda()\n"
        self.assertEqual(_llamadas_sin_ambito(ast.parse(fuente)), [])

    def test_un_helper_importado_no_se_marca(self):
        fuente = ("from storage import _atomic_write_json\n"
                  "def usa():\n    _atomic_write_json({})\n")
        self.assertEqual(_llamadas_sin_ambito(ast.parse(fuente)), [])

    def test_un_helper_del_ambito_de_FUERA_se_ve_desde_dentro(self):
        fuente = ("def externa():\n"
                  "    def _h():\n        pass\n"
                  "    def interna():\n        _h()\n")
        self.assertEqual(_llamadas_sin_ambito(ast.parse(fuente)), [])


if __name__ == "__main__":
    unittest.main()
