"""Un contenedor con scroll dentro de un modal invalida el desenfoque.

`.modal-overlay` lleva `backdrop-filter: blur(8px)`. Cuando algo hace scroll
por debajo de ese desenfoque, el navegador lo da por inválido y **repinta el
viewport entero**: se ve como un flash blanco. Es intermitente —depende de
que el compositor decida rasterizar otra vez— y se manifiesta sobre todo con
contenidos largos, los que se desplazan de verdad.

El arreglo se conocía y estaba copiado en once bloques del CSS, comentario
incluido. Aun así faltaba en once contenedores más, entre ellos el
`.cmv40-log`: el flash se parcheaba en el sitio donde aparecía, y el
siguiente sitio aparecía más tarde. Reportado por el usuario el 2026-09-20
sobre el log de un trabajo CMv4.0 terminado, y ya van varias.

Esto lo mide en Chrome sobre el `index.html` REAL: abre todos los overlays,
recorre sus descendientes y comprueba el estilo COMPUTADO de los que hacen
scroll. Leer el CSS no valdría — la declaración vive en una regla compartida
y un selector nuevo se olvida igual; lo que hay que comprobar es qué le llega
al elemento.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_scroll_aislado_en_modales -v
"""
import html as _h
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import html  # noqa: E402

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)

_SONDA = """<script>
window.__errores = [];
window.addEventListener('error', e => window.__errores.push(e.message || ''));
window.fetch = () => new Promise(() => {});
window.WebSocket = function () { this.close = () => {}; };
</script>"""

_MEDIR = """
<pre id="__out"></pre>
<script>
setTimeout(() => {
  const out = { errores: window.__errores, overlays: 0, scrollers: [], malos: [] };
  // Los overlays nacen en `display:none`: sin enseñarlos, sus descendientes
  // no tienen caja y `overflow` computado no dice nada útil.
  for (const ov of document.querySelectorAll('.modal-overlay')) {
    ov.style.display = 'flex';
    out.overlays += 1;
  }
  for (const ov of document.querySelectorAll('.modal-overlay')) {
    for (const el of ov.querySelectorAll('*')) {
      const cs = getComputedStyle(el);
      const desplaza = ['auto', 'scroll'].includes(cs.overflowY)
                    || ['auto', 'scroll'].includes(cs.overflowX);
      if (!desplaza) continue;
      const nombre = (el.id ? '#' + el.id : '')
                   + (el.className ? '.' + String(el.className).trim().split(/\\s+/).join('.') : '');
      out.scrollers.push(nombre);
      if (!String(cs.willChange).includes('scroll-position')) out.malos.push(nombre);
    }
  }
  document.getElementById('__out').textContent = JSON.stringify(out);
}, 500);
</script>
"""


def _medir() -> dict:
    pagina = html().replace("</head>", _SONDA + "</head>")
    pagina = pagina.replace("</body>", _MEDIR + "</body>")
    pagina = (pagina.replace('src="/static/', 'src="')
                    .replace('href="/static/', 'href="'))
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                      encoding="utf-8", dir=str(APP_DIR / "static"))
    tmp.write(pagina)
    tmp.close()
    try:
        dom = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--allow-file-access-from-files",
             "--dump-dom", "--window-size=1600,1000",
             "--virtual-time-budget=6000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    return json.loads(_h.unescape(m.group(1)))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestNingunScrollDeModalSinAislar(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_la_pagina_carga(self):
        self.assertEqual(self.m["errores"], [])

    def test_hay_overlays_que_mirar(self):
        """Sin overlays el test pasaría en verde vigilando el vacío."""
        self.assertGreater(self.m["overlays"], 5)

    def test_y_scrollers_dentro(self):
        self.assertGreater(len(self.m["scrollers"]), 3,
                           "no se encontró ningún contenedor con scroll: el "
                           "guard no estaría comprobando nada")

    def test_todos_van_aislados(self):
        self.assertEqual(sorted(set(self.m["malos"])), [], (
            "\n  · ".join(["estos hacen scroll bajo el desenfoque del overlay "
                           "y repintan el viewport entero:"]
                          + sorted(set(self.m["malos"])))))


if __name__ == "__main__":
    unittest.main()
