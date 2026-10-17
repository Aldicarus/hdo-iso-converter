"""
Nada debe usar un nombre que su módulo no define.

Un `NameError` en una rama poco transitada no se nota: si además está dentro
de un `try/except Exception` que traduce a HTTP 500, el usuario ve un error
de dominio ("Error al extraer capítulos: ...") y nadie sospecha del código.

Fue exactamente el caso del endpoint «🔄 Restaurar capítulos del disco»
(Tab 1): usaba `run_mkvmerge_identify` y `parse_mpls_chapters` sin
importarlas. El botón fallaba siempre, y el mensaje apuntaba al disco.
Salió al inventariar los nombres libres de main.py para extraer el router.

Este test recorre los módulos de la app y comprueba que cada nombre global
que leen está definido, importado o es un builtin.
"""
import ast
import builtins
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

# Todos los módulos de la app: una lista escrita a mano se queda vieja en
# cuanto alguien añade un fichero.
MODULOS = sorted(str(f.relative_to(APP_DIR)) for f in APP_DIR.rglob("*.py")
                 if "tests" not in f.relative_to(APP_DIR).parts)

# **Por ámbitos, no por módulo.** La primera versión daba por definido en
# todo el fichero cualquier nombre importado en cualquier sitio, así que el
# `from phases.iso_mount import … safe_source_path` LOCAL de un endpoint le
# «definía» el nombre al runner de la creación de series, que lo usaba sin
# importarlo: crear una serie desde ficheros M2TS lanzaba `NameError` desde
# `d90859a` (v3.0.0 a v3.3.1) y el guard estaba en verde (auditoría con Codex
# del 2026-10-17). Ahora cada función ve sus nombres, los de las funciones
# que la encierran y los del módulo —no los de otra función ni los de su
# clase—, que es la regla de Python.
DUNDER = {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__builtins__", "__loader__"}
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _hijos_del_ambito(nodo):
    """Los nodos que se evalúan en el ámbito de `nodo` (sin entrar en las
    funciones ni las clases anidadas, salvo en lo que se evalúa fuera de su
    cuerpo: decoradores, valores por defecto, bases)."""
    pila = list(ast.iter_child_nodes(nodo))
    while pila:
        n = pila.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pila.extend(n.decorator_list)
            pila.extend(d for d in n.args.defaults + n.args.kw_defaults if d is not None)
            continue
        if isinstance(n, ast.Lambda):
            pila.extend(d for d in n.args.defaults + n.args.kw_defaults if d is not None)
            continue
        if isinstance(n, ast.ClassDef):
            pila.extend(n.decorator_list + n.bases + [k.value for k in n.keywords])
            continue
        # Las anotaciones no se evalúan (from __future__ import annotations)
        if isinstance(n, ast.arg):
            continue
        if isinstance(n, ast.AnnAssign):
            pila.append(n.target)
            if n.value is not None:
                pila.append(n.value)
            continue
        pila.extend(ast.iter_child_nodes(n))


def _definidos(nodo):
    d = set()
    if isinstance(nodo, FUNCS):
        a = nodo.args
        for x in a.posonlyargs + a.args + a.kwonlyargs + ([a.vararg] if a.vararg else []) + \
                ([a.kwarg] if a.kwarg else []):
            d.add(x.arg)
    for n in _hijos_del_ambito(nodo):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            d.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            d.add(n.id)
        elif isinstance(n, ast.alias):
            d.add(n.asname or n.name.split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            d.add(n.name)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            d.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            d.add(n.rest)
    return d


def _globales(arbol):
    g = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Global):
            g.update(n.names)
    return g


def no_resueltos(arbol):
    base = set(dir(builtins)) | DUNDER
    modulo = _definidos(arbol) | _globales(arbol)
    fallos = set()

    def visitar(nodo, visibles, es_clase=False):
        for n in _hijos_del_ambito(nodo):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in visibles:
                fallos.add((n.id, n.lineno))
        for n in _hijos_del_ambito(nodo):
            if isinstance(n, FUNCS):
                # el cuerpo de un método NO ve los nombres de su clase
                visitar(n, visibles | _definidos(n))
            elif isinstance(n, ast.ClassDef):
                visitar(n, visibles | _definidos(n), es_clase=True)

    def visitar_raiz(arbol):
        visibles = base | modulo
        for n in _hijos_del_ambito(arbol):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in visibles:
                fallos.add((n.id, n.lineno))
        for n in _hijos_del_ambito(arbol):
            if isinstance(n, FUNCS):
                visitar(n, visibles | _definidos(n))
            elif isinstance(n, ast.ClassDef):
                visitar_clase(n, visibles)

    def visitar_clase(clase, exteriores):
        propios = _definidos(clase)
        for n in _hijos_del_ambito(clase):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) \
                    and n.id not in exteriores | propios:
                fallos.add((n.id, n.lineno))
        for n in _hijos_del_ambito(clase):
            if isinstance(n, FUNCS):
                visitar(n, exteriores | _definidos(n))
            elif isinstance(n, ast.ClassDef):
                visitar_clase(n, exteriores | propios)

    visitar_raiz(arbol)
    return fallos


def _nombres_no_resueltos(ruta: Path) -> set[str]:
    return {n for n, _ in no_resueltos(ast.parse(ruta.read_text(encoding="utf-8")))}


class TestNombresResueltos(unittest.TestCase):

    def test_ningun_modulo_usa_nombres_que_no_define(self):
        for rel in MODULOS:
            ruta = APP_DIR / rel
            if not ruta.exists():
                continue
            with self.subTest(modulo=rel):
                huerfanos = _nombres_no_resueltos(ruta)
                self.assertEqual(
                    huerfanos, set(),
                    f"{rel} lee nombres que no define ni importa: "
                    f"{sorted(huerfanos)}. Si están en una rama poco "
                    f"transitada, es un NameError esperando a que alguien "
                    f"pulse ese botón.")


class TestElDetectorRespetaLosAmbitos(unittest.TestCase):
    """El detector sobre fuente sintética: si vuelve a mirar el módulo entero,
    el `NameError` de las series vuelve a pasar en verde."""

    def _fallos(self, codigo):
        return sorted(n for n, _ in no_resueltos(ast.parse(codigo)))

    def test_un_import_local_no_vale_para_otra_funcion(self):
        codigo = ("def a():\n    from os import path\n    return path\n"
                  "def b():\n    return path\n")
        self.assertEqual(self._fallos(codigo), ["path"])

    def test_una_funcion_anidada_ve_la_que_la_encierra(self):
        codigo = "def a():\n    from os import path\n    def b():\n        return path\n    return b\n"
        self.assertEqual(self._fallos(codigo), [])

    def test_un_metodo_no_ve_los_nombres_de_su_clase(self):
        codigo = "class A:\n    X = 1\n    def f(self):\n        return X\n"
        self.assertEqual(self._fallos(codigo), ["X"])

    def test_lo_del_modulo_lo_ve_todo_el_mundo(self):
        codigo = "import os\nclass A:\n    def f(self):\n        return os\n"
        self.assertEqual(self._fallos(codigo), [])


class TestElRestauradorDeCapitulos(unittest.TestCase):
    """La regresión concreta: el endpoint que estaba roto."""

    def test_importa_lo_que_usa(self):
        src = (APP_DIR / "routers" / "tab1.py").read_text(encoding="utf-8")
        i = src.index("async def reset_chapters")
        cuerpo = src[i:i + 3000]
        self.assertIn("run_mkvmerge_identify(", cuerpo)
        self.assertIn("from phases.phase_a import", cuerpo,
                      "usa parse_mpls_chapters/run_mkvmerge_identify: hay que "
                      "importarlas o la rama levanta NameError")

    def test_las_funciones_existen_donde_se_importan(self):
        from phases.phase_a import parse_mpls_chapters, run_mkvmerge_identify
        self.assertTrue(callable(parse_mpls_chapters))
        self.assertTrue(callable(run_mkvmerge_identify))


if __name__ == "__main__":
    unittest.main()
