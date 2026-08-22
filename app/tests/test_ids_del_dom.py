"""Todo `id` que `app.js` busca tiene que existir en algún sitio.

Regresión real (2026-08-21): al quitar el modal del perfil de luminancia del
HTML se cortó por marcadores de comentario y el corte se llevó por delante DOS
modales vecinos — `mkv-analyze-modal` (el de "Analizando MKV") y
`raw-analysis-modal` (el de "🔬 Datos ISO" de Tab 1).

El síntoma fue mudo y desagradable: abrir un MKV **no hacía nada**.
`openModal('mkv-analyze-modal')` sobre un id que no existe no lanza, no avisa y
no pinta; y `document.getElementById(...)` devuelve null, que el código trata
como "no estoy en esa pantalla". Ni la sintaxis del JS ni la suite se enteraron:
la app cargaba sin un solo error de consola.

Este guard compara los ids que el JS pide con los que hay. Solo mira **literales
de cadena**: los ids que se construyen con plantilla (`cmv40-chart-${pid}`) no se
pueden resolver estáticamente y se ignoran a propósito.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_ids_del_dom -v
"""
import re
import unittest
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
sys.path.insert(0, str(STATIC.parent / "tests"))
from frontend_sources import js_completo  # noqa: E402

JS = js_completo()
HTML = (STATIC / "index.html").read_text(encoding="utf-8")

# `getElementById('x')` y `openModal('x')` con literal (sin `${`).
_PEDIDOS = re.compile(
    r"""(?:getElementById|openModal|closeModal)\(\s*['"]([A-Za-z][\w:.-]*)['"]\s*\)""")
# Los ids que existen: los del HTML, los que app.js emite en sus plantillas y
# los que crea al vuelo (`el.id = 'x'`, y después los busca para saber si ya
# los había creado — ahí el null es la respuesta esperada, no un fallo).
_EN_HTML = re.compile(r'id="([A-Za-z][\w:.-]*)"')
_EN_JS = re.compile(r'id="([A-Za-z][\w:.-]*)"')
_CREADOS_EN_JS = re.compile(r"""\.id\s*=\s*['"]([A-Za-z][\w:.-]*)['"]""")


def _ids_disponibles() -> set[str]:
    return (set(_EN_HTML.findall(HTML)) | set(_EN_JS.findall(JS))
            | set(_CREADOS_EN_JS.findall(JS)))


class TestIdsQueElJsBusca(unittest.TestCase):

    def test_todos_los_ids_pedidos_existen(self):
        disponibles = _ids_disponibles()
        faltan = sorted({i for i in _PEDIDOS.findall(JS) if i not in disponibles})
        self.assertEqual(
            faltan, [],
            "app.js busca ids que no existen ni en index.html ni en sus propias "
            "plantillas. `getElementById` devuelve null y `openModal` no hace "
            f"nada, sin un solo error de consola: {faltan}")

    def test_los_modales_clave_estan_en_el_html(self):
        """Los que abre un flujo entero: si falta uno, la pantalla se queda
        muda. Se comprueban por nombre para que borrar uno duela aquí."""
        for modal in ("mkv-analyze-modal",      # abrir un MKV en Tab 2
                      "mkv-quality-modal",      # análisis extendido
                      "raw-analysis-modal",     # 🔬 Datos ISO (Tab 1)
                      "analyze-modal",          # análisis de origen (Tab 1)
                      "series-modal",           # multi-episodio (Tab 1)
                      "file-browser-modal",     # selector de ficheros
                      "settings-modal"):
            with self.subTest(modal=modal):
                self.assertIn(f'id="{modal}"', HTML)

    def test_no_hay_ids_duplicados_en_el_html(self):
        """Dos elementos con el mismo id: `getElementById` devuelve el primero
        y el segundo queda inerte."""
        from collections import Counter
        repes = [i for i, n in Counter(_EN_HTML.findall(HTML)).items() if n > 1]
        self.assertEqual(sorted(repes), [], f"ids duplicados en index.html: {repes}")


if __name__ == "__main__":
    unittest.main()
