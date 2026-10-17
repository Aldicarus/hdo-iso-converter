"""`app/tools/mutar.py`, sobre un proyecto de juguete.

La herramienta toca ficheros de producción y los restaura, así que lo que se
prueba es justo eso: que el fichero vuelve a ser byte a byte el de antes —en
las tres salidas—, que una mutación que sobrevive da código 1 (un script que
siempre sale con 0 no sirve en un CI), que con la base en rojo no muta nada, y
que recupera el fichero que una ejecución anterior dejó mutado al morir.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_herramienta_de_mutaciones -v
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
HERRAMIENTA = APP_DIR / "tools" / "mutar.py"

CODIGO = "def suma(a, b):\n    return a + b\n\n\ndef doble(x):\n    return x * 2\n"
TEST = ("import sys, unittest\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        "from calc import suma\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_suma(self):\n        self.assertEqual(suma(2, 3), 5)\n")


class TestMutar(unittest.TestCase):

    def setUp(self):
        self.raiz = Path(tempfile.mkdtemp(prefix="mutar_juguete_"))
        self.addCleanup(shutil.rmtree, self.raiz, ignore_errors=True)
        (self.raiz / "app" / "tests").mkdir(parents=True)
        self.codigo = self.raiz / "app" / "calc.py"
        self.codigo.write_text(CODIGO)
        (self.raiz / "app" / "tests" / "test_calc.py").write_text(TEST)
        sys.path.insert(0, str(APP_DIR / "tools"))
        self.addCleanup(sys.path.remove, str(APP_DIR / "tools"))
        import mutar
        self.mutar = mutar
        mutar.recuperar_respaldos(self.raiz)

    def _correr(self, *muts):
        f = self.raiz / "muts.json"
        f.write_text(json.dumps([dict(m, fichero="app/calc.py", modulos=["test_calc"])
                                 for m in muts]))
        r = subprocess.run([sys.executable, str(HERRAMIENTA), str(f),
                            "--raiz", str(self.raiz)], capture_output=True, text=True,
                           timeout=120)
        return r.returncode, r.stdout

    def test_una_mutacion_cazada_sale_con_0_y_restaura(self):
        rc, out = self._correr({"id": "suma resta", "viejo": "return a + b",
                                "nuevo": "return a - b"})
        self.assertEqual(rc, 0, out)
        self.assertIn("CAZADA", out)
        self.assertEqual(self.codigo.read_text(), CODIGO)

    def test_una_que_sobrevive_sale_con_1_y_restaura(self):
        rc, out = self._correr({"id": "doble sin probar", "viejo": "return x * 2",
                                "nuevo": "return x * 3"})
        self.assertEqual(rc, 1, out)
        self.assertIn("SOBREVIVE", out)
        self.assertEqual(self.codigo.read_text(), CODIGO)

    def test_una_que_no_casa_cuenta_como_fallo(self):
        rc, out = self._correr({"id": "no está", "viejo": "return a * b",
                                "nuevo": "return 0"})
        self.assertEqual(rc, 1, out)
        self.assertIn("NO CASA", out)

    def test_con_la_base_en_rojo_no_muta_nada(self):
        self.codigo.write_text(CODIGO.replace("a + b", "a + b + 1"))
        roto = self.codigo.read_text()
        rc, out = self._correr({"id": "x", "viejo": "return x * 2", "nuevo": "return 0"})
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.codigo.read_text(), roto)

    def test_recupera_lo_que_dejo_mutado_una_ejecucion_muerta(self):
        d = self.mutar._respaldo_dir(self.raiz.resolve())
        d.mkdir(parents=True, exist_ok=True)
        (d / "1.orig").write_bytes(CODIGO.encode())
        (d / "1.orig.json").write_text(json.dumps({"fichero": "app/calc.py", "copia": "1.orig"}))
        self.codigo.write_text("def suma(a, b):\n    return 0\n")      # quedó mutado
        rc, out = self._correr({"id": "suma resta", "viejo": "return a + b",
                                "nuevo": "return a - b"})
        self.assertIn("restaurados", out)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.codigo.read_text(), CODIGO)

    def test_la_base_es_cada_seleccion_no_la_union(self):
        """`test_b` solo pasa si `test_a` se importó antes: la unión está en
        verde y `test_b` solo, en rojo. Una mutación asignada a `test_b` no
        puede contar como cazada (revisión de Codex de la suite)."""
        t = self.raiz / "app" / "tests"
        (t / "test_a.py").write_text("import builtins\nbuiltins.PREPARADO = True\n"
                                     "import unittest\nclass A(unittest.TestCase):\n"
                                     "    def test_a(self):\n        pass\n")
        (t / "test_b.py").write_text("import builtins, unittest\nclass B(unittest.TestCase):\n"
                                     "    def test_b(self):\n"
                                     "        self.assertTrue(getattr(builtins, 'PREPARADO', False))\n")
        f = self.raiz / "muts.json"
        f.write_text(json.dumps([
            {"id": "con a", "fichero": "app/calc.py", "viejo": "return a + b",
             "nuevo": "return a - b", "modulos": ["test_a", "test_b", "test_calc"]},
            {"id": "solo b", "fichero": "app/calc.py", "viejo": "return x * 2",
             "nuevo": "return 0", "modulos": ["test_b"]}]))
        r = subprocess.run([sys.executable, str(HERRAMIENTA), str(f), "--raiz", str(self.raiz)],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("test_b", r.stdout)
        self.assertEqual(self.codigo.read_text(), CODIGO)

    def test_un_respaldo_que_no_se_puede_restaurar_no_se_borra(self):
        d = self.mutar._respaldo_dir(self.raiz.resolve())
        d.mkdir(parents=True, exist_ok=True)
        (d / "2.orig").write_bytes(CODIGO.encode())
        (d / "2.orig.json").write_text(json.dumps({"fichero": "app/calc.py", "copia": "2.orig"}))
        self.codigo.write_text("def suma(a, b):\n    return 0\n")     # quedó mutado
        self.codigo.chmod(0o444)
        self.addCleanup(self.codigo.chmod, 0o644)
        rc, out = self._correr({"id": "x", "viejo": "return a + b", "nuevo": "return a - b"})
        self.assertEqual(rc, 2, out)
        self.assertIn("no se pudieron restaurar", out)
        self.assertTrue((d / "2.orig").exists(), "se ha borrado el único original")
        self.codigo.chmod(0o644)
        self.mutar.recuperar_respaldos(self.raiz.resolve())
        self.assertEqual(self.codigo.read_text(), CODIGO)



class TestElCatalogoSigueCasando(unittest.TestCase):
    """Cada mutación del catálogo tiene que casar con el código de HOY.

    Un arreglo que reescribe el código de una mutación la deja «no casa», y
    eso solo se ve al ejecutar `mutar.py` entero —minutos, y nadie lo hace en
    cada commit—. Cinco se quedaron así con la tanda 2 de la auditoría del
    2026-10-17 (la copia de Tab 2 y la política de niveles) sin que nada
    avisara: el catálogo decía cubrir lo que ya no cubría."""

    def test_cada_viejo_aparece_una_vez_y_sus_modulos_existen(self):
        raiz = APP_DIR.parent
        catalogo = json.loads((APP_DIR / "tools" / "mutaciones.json").read_text())
        malas = []
        for m in catalogo:
            fichero = raiz / m["fichero"]
            n = fichero.read_text().count(m["viejo"]) if fichero.exists() else -1
            if n != 1:
                malas.append(f"{m['id']}: «viejo» aparece {n} veces en {m['fichero']}")
            for mod in m["modulos"]:
                if not (APP_DIR / "tests" / f"{mod}.py").exists():
                    malas.append(f"{m['id']}: no existe el módulo {mod}")
        self.assertEqual(malas, [])

    def test_los_ids_no_se_repiten(self):
        catalogo = json.loads((APP_DIR / "tools" / "mutaciones.json").read_text())
        ids = [m["id"] for m in catalogo]
        self.assertEqual(sorted({i for i in ids if ids.count(i) > 1}), [])


if __name__ == "__main__":
    unittest.main()
