"""El banner que contrasta la hoja de DoviTools con lo que medimos.

El offset de la hoja es una corrección que la comunidad **ya aplicó** al bin,
no un desfase pendiente. El banner decía lo contrario: felicitaba cuando el
desfase medido coincidía con el documentado ("confirmación fuerte de que el
bin está bien alineado") y avisaba cuando no. Con la lectura correcta eso está
al revés, y sobre los proyectos reales del NAS marcaba "algo no cuadra" en los
16 que tienen offset documentado — ninguno de los cuales necesitó corregir
nada.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_sheet_sync_banner -v
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import js_completo  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()


def _fn(nombre: str) -> str:
    i = JS.index(f"function {nombre}(")
    return JS[i:JS.index("\n}\n", i) + 3]


def _render(sheet_sync) -> str:
    guion = f"""
globalThis.escHtml = s => String(s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
{_fn('_cmv40SheetSyncBannerHTML')}
console.log(JSON.stringify(_cmv40SheetSyncBannerHTML({json.dumps(sheet_sync)})));
"""
    r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr[:2000])
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node no está instalado")
class TestElBannerDelSheet(unittest.TestCase):

    BASE = {"sheet_offset": 16, "sheet_offset_text": "(+16)",
            "match_title": "Minions and Monsters 2026"}

    def test_residuo_cero_sale_en_verde(self):
        """Caso real: Minions — hoja +16, medido 0. El bin es el corregido."""
        h = _render({**self.BASE, "detected_offset": 0, "corregido": True,
                     "parece_sin_corregir": False})
        self.assertIn("banner success", h)
        self.assertIn("ya corrigió", h)
        self.assertNotIn("banner warning", h)

    def test_detectar_lo_documentado_avisa(self):
        h = _render({**self.BASE, "detected_offset": 16, "corregido": False,
                     "parece_sin_corregir": True})
        self.assertIn("banner warning", h)
        self.assertIn("no</b> es el corregido", h)

    def test_un_desfase_que_la_hoja_no_explica_avisa(self):
        h = _render({**self.BASE, "detected_offset": 300, "corregido": False,
                     "parece_sin_corregir": False})
        self.assertIn("banner warning", h)
        self.assertIn("no\n        explica", h.replace("\r", ""))

    def test_sin_medida_propia_es_informativo(self):
        h = _render({**self.BASE, "detected_offset": None, "corregido": None,
                     "parece_sin_corregir": False})
        self.assertIn("banner info", h)
        self.assertNotIn("banner warning", h)

    def test_sin_offset_en_la_hoja_no_se_pinta_nada(self):
        self.assertEqual(_render({"sheet_offset": None}), "")
        self.assertEqual(_render(None), "")

    def test_hoja_a_cero_y_medida_a_cero(self):
        h = _render({"sheet_offset": 0, "sheet_offset_text": "0.0",
                     "match_title": "X", "detected_offset": 0,
                     "corregido": True, "parece_sin_corregir": False})
        self.assertIn("banner success", h)
        self.assertIn("no documenta ningún desfase", h)

    def test_el_titulo_de_la_fila_va_escapado(self):
        h = _render({**self.BASE, "match_title": "<script>x</script>",
                     "detected_offset": 0, "corregido": True,
                     "parece_sin_corregir": False})
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)


if __name__ == "__main__":
    unittest.main()
