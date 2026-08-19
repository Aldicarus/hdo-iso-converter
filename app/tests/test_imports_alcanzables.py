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

MODULOS = [
    "main.py",
    "storage.py",
    "models.py",
    "dev_fixtures.py",
    "routers/cmv40.py",
    "phases/cmv40_pipeline.py",
    "phases/cmv40_strategy.py",
    "phases/phase_a.py",
    "phases/phase_b.py",
    "phases/phase_d.py",
    "phases/phase_e.py",
    "phases/mkv_analyze.py",
    "phases/rpu_analyze.py",
    "phases/iso_mount.py",
]

# `__file__`, `__name__`… existen en tiempo de ejecución pero no como
# asignación en el AST.
DUNDER = {"__file__", "__name__", "__doc__", "__package__", "__spec__"}


def _nombres_no_resueltos(ruta: Path) -> set[str]:
    tree = ast.parse(ruta.read_text(encoding="utf-8"))
    definidos = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definidos.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            definidos.add(node.id)
        elif isinstance(node, ast.arg):
            definidos.add(node.arg)
        elif isinstance(node, ast.alias):
            definidos.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            definidos.add(node.name)
        elif isinstance(node, ast.Global):
            definidos.update(node.names)

    leidos = {n.id for n in ast.walk(tree)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    # `X.algo` también exige que X exista.
    leidos |= {n.value.id for n in ast.walk(tree)
               if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    return leidos - definidos - set(dir(builtins)) - DUNDER


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


class TestElRestauradorDeCapitulos(unittest.TestCase):
    """La regresión concreta: el endpoint que estaba roto."""

    def test_importa_lo_que_usa(self):
        src = (APP_DIR / "main.py").read_text(encoding="utf-8")
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
