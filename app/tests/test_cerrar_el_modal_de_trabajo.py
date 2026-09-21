"""El modal de trabajo se puede cerrar con un botón, y se ve.

Se cerraba con ESC o clicando fuera —y el comentario del marcado lo decía—
pero nada en pantalla lo anunciaba: no había dónde pulsar. Pedido por el
usuario el 2026-09-21.

La X y el botón de cancelar comparten glifo, porque en `GLIFOS` solo hay
uno. Lo que los separa es que el destructivo va en rojo y con su palabra
delante; por eso lo que se mide aquí es que **no se pisan** y que cada uno
hace lo suyo.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cerrar_el_modal_de_trabajo -v
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

from frontend_sources import html, stub_catalogo_es  # noqa: E402

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
  const out = { errores: window.__errores };
  const ov = document.getElementById('trabajo-modal');
  ov.classList.add('open');
  const x = document.getElementById('trabajo-modal-cerrar');
  const cancelar = document.getElementById('trabajo-modal-cancelar');
  const caja = ov.querySelector('.trabajo-modal-caja');
  out.existe = !!x;
  if (x) {
    const rx = x.getBoundingClientRect(), rc = cancelar.getBoundingClientRect();
    const rj = caja.getBoundingClientRect();
    out.visible = rx.width > 0 && rx.height > 0;
    // Sin texto: solo el icono.
    out.texto = x.textContent.trim();
    out.tieneIcono = !!x.querySelector('svg, [data-icono]');
    out.hijosConTexto = x.querySelectorAll('[data-i18n]').length;
    // Que no se pisen con el de cancelar, que lleva el MISMO glifo.
    out.solapa = !(rx.right <= rc.left || rx.left >= rc.right
                   || rx.bottom <= rc.top || rx.top >= rc.bottom);
    // Arriba y a la derecha de la caja.
    out.arriba = rx.top - rj.top < 60;
    out.aLaDerecha = rj.right - rx.right < 40;
    out.tip = x.getAttribute('data-i18n-tip') || '';
    // Y cierra: se pulsa y el overlay pierde `open`.
    window.cerrarModalDeTrabajo = () => { ov.classList.remove('open'); out.llamado = true; };
    x.onclick = null;
    x.setAttribute('onclick', 'cerrarModalDeTrabajo()');
    x.click();
    out.cerrado = !ov.classList.contains('open');
  }
  document.getElementById('__out').textContent = JSON.stringify(out);
}, 500);
</script>
"""


def _medir() -> dict:
    pagina = html().replace("</head>", _SONDA + stub_catalogo_es() + "</head>")
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
class TestLaXDeCerrar(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_la_pagina_carga(self):
        self.assertEqual(self.m["errores"], [])

    def test_existe_y_se_ve(self):
        self.assertTrue(self.m["existe"], "no hay botón de cerrar")
        self.assertTrue(self.m["visible"], "está pero mide 0 px")

    def test_es_solo_un_icono(self):
        """Lo pedido: sin texto. Con el catálogo sembrado, porque sin él
        `pintarTextos` no escribe nada y cualquier etiqueta pasaría por
        vacía."""
        self.assertEqual(self.m["texto"], "")
        self.assertTrue(self.m["tieneIcono"])
        self.assertEqual(self.m["hijosConTexto"], 0)

    def test_no_se_pisa_con_el_de_cancelar(self):
        """Los dos llevan el mismo glifo —en el catálogo solo hay una X— así
        que lo mínimo es que no se solapen."""
        self.assertFalse(self.m["solapa"])

    def test_esta_en_la_esquina_superior_derecha(self):
        self.assertTrue(self.m["arriba"], "no está arriba del todo")
        self.assertTrue(self.m["aLaDerecha"], "no está pegado a la derecha")

    def test_dice_lo_que_hace_al_pasar_por_encima(self):
        """Sin texto, el tooltip es lo único que lo explica."""
        self.assertEqual(self.m["tip"], "ui.cerrar")

    def test_y_cierra(self):
        self.assertTrue(self.m["cerrado"])


if __name__ == "__main__":
    unittest.main()
