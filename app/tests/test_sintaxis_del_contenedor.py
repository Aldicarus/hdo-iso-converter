"""Todo el repo tiene que parsear con la minor de Python del contenedor.

El Mac va con 3.12 y `ubuntu:22.04` con **3.10**. La diferencia no es
académica: PEP 701 admite desde 3.12 una barra invertida dentro de la
expresión de una f-string, así que

    f"| {x.replace('|', '\\|')} |"

compila en el Mac y revienta con `SyntaxError` en la imagen. Pasó el
2026-09-23 en `tools/generar_avisos_python.py`, y el síntoma fue un
**build de Docker muerto**, no un test en rojo.

CI lo caza porque corre sobre 3.10, pero **por accidente**: lo destaparon
dos guards que recorren el fuente por otros motivos (`test_disc_probe_m2ts`,
`test_helpers_en_su_ambito`), y su mensaje no dice de qué va el problema.
Sobre todo, no fallan en el Mac: el bucle era «subir, esperar a CI, mirar
por qué». Este guard falla donde se escribe el código.

La versión NO se escribe aquí: sale de `.github/workflows/tests.yml`, que
es el sitio que CLAUDE.md señala como la minor del contenedor. Una
constante local se desincronizaría el día que la imagen suba de base.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_sintaxis_del_contenedor -v
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
APP = RAIZ / "app"
CI = RAIZ / ".github" / "workflows" / "tests.yml"

TRIPLES = ('"""', "'''")


def _minor_del_contenedor() -> tuple[int, int]:
    m = re.search(r"python-version:\s*['\"](\d+)\.(\d+)['\"]",
                  CI.read_text(encoding="utf-8"))
    assert m, "no se encuentra `python-version` en el workflow de tests"
    return int(m.group(1)), int(m.group(2))


def _comilla_de(literal: str) -> str:
    """La comilla que abre una f-string, saltándose el prefijo (`f`, `rf`…)."""
    cuerpo = literal.lstrip("fFrRbBuU")
    for t in TRIPLES:
        if cuerpo.startswith(t):
            return t
    return cuerpo[:1]


class TestNadieUsaSintaxisMasNuevaQueElContenedor(unittest.TestCase):

    def setUp(self):
        self.objetivo = _minor_del_contenedor()
        self.ficheros = sorted(APP.rglob("*.py"))
        self.assertGreater(len(self.ficheros), 100, "¿se está mirando el repo?")

    def test_todo_el_arbol_parsea(self):
        """Caza lo que `feature_version` sí sabe gatear: `except*` (3.11),
        los parámetros de tipo de PEP 695 (3.12)…"""
        if sys.version_info[:2] < self.objetivo:
            self.skipTest(f"intérprete anterior al objetivo {self.objetivo}")
        rotos = []
        for p in self.ficheros:
            try:
                ast.parse(p.read_text(encoding="utf-8"),
                          filename=str(p), feature_version=self.objetivo)
            except SyntaxError as e:
                rotos.append(f"{p.relative_to(RAIZ)}:{e.lineno} — {e.msg}")
        self.assertEqual(rotos, [], (
            f"\nsintaxis que Python {self.objetivo[0]}.{self.objetivo[1]} "
            f"—la del contenedor— no acepta:\n  · " + "\n  · ".join(rotos)))

    def test_ninguna_f_string_usa_lo_que_pep_701_abrio(self):
        """`feature_version` NO basta, y por poco se queda un guard mudo.

        Solo gatea features de nivel AST, y PEP 701 cambió el TOKENIZADOR:
        en 3.12 la expresión de una f-string se lexa como código normal, así
        que admite barras invertidas y comillas iguales a las de fuera.
        `ast.parse(..., feature_version=(3,10))` acepta las dos sin
        rechistar — comprobado: el `replace('|', '\\|')` que mató el build
        pasaba el chequeo de arriba.

        Se mira la EXPRESIÓN de cada hueco, que es el único trozo donde 3.10
        no lo admite: una barra en la parte literal es legal desde siempre.
        """
        rotos = []
        for p in self.ficheros:
            src = p.read_text(encoding="utf-8")
            try:
                arbol = ast.parse(src)
            except SyntaxError:
                continue                    # lo denuncia el test de arriba
            for n in ast.walk(arbol):
                if not isinstance(n, ast.JoinedStr):
                    continue
                comilla = _comilla_de(ast.get_source_segment(src, n) or "")
                for hueco in n.values:
                    if not isinstance(hueco, ast.FormattedValue):
                        continue
                    expr = ast.get_source_segment(src, hueco.value) or ""
                    donde = f"{p.relative_to(RAIZ)}:{hueco.lineno}"
                    if "\\" in expr:
                        rotos.append(f"{donde} — barra invertida en `{expr}`")
                    elif comilla and comilla in expr:
                        rotos.append(
                            f"{donde} — comilla {comilla} repetida en `{expr}`")
        self.assertEqual(rotos, [], (
            "\nf-strings que solo compilan desde Python 3.12 (PEP 701). El "
            "contenedor va con 3.10: en el Mac pasan y el build de Docker "
            "muere.\nSaca la expresión a una variable antes de la f-string:"
            "\n  · " + "\n  · ".join(rotos)))

    def test_el_objetivo_se_lee_del_workflow(self):
        """Si el workflow deja de declarar la versión, este guard estaría
        comparando contra nada."""
        self.assertEqual(self.objetivo[0], 3)
        self.assertGreaterEqual(self.objetivo[1], 10)


if __name__ == "__main__":
    unittest.main()
