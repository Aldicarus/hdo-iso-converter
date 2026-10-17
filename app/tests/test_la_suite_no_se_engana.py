"""Dos formas en que un test pasa en verde sin comprobar nada.

  · **Un bucle cuyo cuerpo es solo `pass`.** Salieron dos en la auditoría del
    2026-10-17 (`test_iconos_de_trabajo` y `test_modal_de_trabajo`), los dos
    con pinta de comprobación —recorrían los identificadores que el test
    decía vigilar— y sin hacer nada con ellos.
  · **Un aviso de sintaxis.** Una secuencia de escape inválida en un
    docstring es un `SyntaxWarning` hoy y un `SyntaxError` en una versión
    próxima de Python: el día que llegue, el módulo entero deja de cargarse.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_la_suite_no_se_engana -v
"""
import ast
import unittest
import warnings
from pathlib import Path

TESTS = Path(__file__).resolve().parent


class TestLaSuiteNoSeEngana(unittest.TestCase):

    def test_ningun_bucle_es_solo_pass(self):
        vacios = []
        for f in sorted(TESTS.glob("*.py")):
            for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if (isinstance(n, (ast.For, ast.While, ast.AsyncFor))
                        and all(isinstance(b, ast.Pass) for b in n.body)):
                    vacios.append(f"{f.name}:{n.lineno}")
        self.assertEqual(vacios, [])

    def test_ningun_modulo_da_avisos_de_sintaxis(self):
        avisos = []
        for f in sorted(TESTS.glob("*.py")):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                compile(f.read_text(encoding="utf-8"), str(f), "exec")
            avisos += [f"{f.name}: {x.message}" for x in w]
        self.assertEqual(avisos, [])


if __name__ == "__main__":
    unittest.main()
