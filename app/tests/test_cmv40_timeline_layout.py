"""La fila del reloj de la timeline CMv4.0 no debe partirse en dos.

La columna mide 330px y en esa fila conviven tres cosas: el tiempo
transcurrido, el chip "N/M · XX%" y el tiempo restante. Con el sufijo corto
—"(auto)"— cabía todo; al añadir "(estimado inicial)" dejó de caber y flexbox
encogió los dos: el texto restante partía por la mitad y el chip quedaba como
una pastilla de dos líneas ("1/10 ·" / "0%").

Medirlo es la única forma de saberlo: el markup y el CSS se leen bien en
ambos casos. El test renderiza el markup REAL extraído de app.js con el CSS
REAL en Chrome headless y comprueba la geometría resultante. Si no hay Chrome
en la máquina, se salta.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
APP_JS = APP_DIR / "static" / "app.js"
STYLE_CSS = APP_DIR / "static" / "style.css"

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)

# Valores de muestra para los ${...} del template. El sufijo largo con horas
# es el peor caso real: ninguna combinación produce un texto mayor.
_SUSTITUCIONES = {
    "timerAttrs": "",
    "elapsedLabel": "01:04:12",
    "escHtml(remainingText)": "~1:02:30 restantes (estimado inicial)",
    "doneCount": "10",
    "totalCount": "10",
    "progressPct": "100",
}


def _extraer_fila_meta() -> str:
    """Saca del app.js el markup real de .cmv40-tl-progress-meta y resuelve
    los ${...} con valores de muestra."""
    src = APP_JS.read_text(encoding="utf-8")
    i = src.index('<div class="cmv40-tl-progress-meta">')
    j = src.index("</div>", src.index('class="cmv40-tl-timer-remaining"', i))
    html = src[i:j + len("</div>")]
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    return re.sub(
        r"\$\{([^}]*)\}",
        lambda m: _SUSTITUCIONES.get(m.group(1).strip(), ""),
        html,
    )


_MEDIDOR = """
<script>
const c = document.getElementById('host');
// OJO: el.getClientRects() NO sirve aquí. Estos spans son flex items, así
// que el navegador los blockifica y devuelven UN solo rect aunque su texto
// se haya partido en dos líneas — con eso el test daba verde sobre el bug
// que venía a cazar. Un Range sobre el contenido sí devuelve un rect por
// línea de texto real.
const lineas = el => {
  const r = document.createRange();
  r.selectNodeContents(el);
  return r.getClientRects().length;
};
const pct = c.querySelector('.cmv40-tl-progress-pct');
const rem = c.querySelector('.cmv40-tl-timer-remaining');
const tim = c.querySelector('.cmv40-tl-timer');
document.getElementById('out').textContent = JSON.stringify({
  lineas_pct: lineas(pct),
  lineas_rem: lineas(rem),
  rem_desborda: rem.scrollWidth > rem.clientWidth + 1,
  pct_desborda: pct.scrollWidth > pct.clientWidth + 1,
  pct_en_la_linea_del_timer:
    Math.abs(pct.getBoundingClientRect().top - tim.getBoundingClientRect().top) < 8,
  rem_en_su_propia_linea:
    rem.getBoundingClientRect().top >= tim.getBoundingClientRect().bottom - 2,
  ancho_usado: Math.ceil(Math.max(
    pct.getBoundingClientRect().right, rem.getBoundingClientRect().right)),
});
</script>
"""


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestFilaDelRelojNoSeParte(unittest.TestCase):
    ANCHO_COLUMNA = 330   # .cmv40-running-timeline-wrap

    @classmethod
    def setUpClass(cls):
        pagina = f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<link rel="stylesheet" href="file://{STYLE_CSS}">
<style>body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',sans-serif;}}
 #host{{width:{cls.ANCHO_COLUMNA}px;}}</style></head><body>
<div id="host"><div class="cmv40-running-timeline"><div class="cmv40-tl-header">
<div class="cmv40-tl-progress">{_extraer_fila_meta()}</div>
</div></div></div><pre id="out"></pre>{_MEDIDOR}</body></html>"""

        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(pagina)
            cls._tmp = fh.name

        salida = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--allow-file-access-from-files",
             "--dump-dom", "--virtual-time-budget=3000", cls._tmp],
            capture_output=True, text=True, timeout=90,
        ).stdout
        m = re.search(r'<pre id="out">(.*?)</pre>', salida, re.S)
        if not m:
            raise unittest.SkipTest("Chrome no devolvió medidas")
        cls.medidas = json.loads(m.group(1))

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_tmp", None):
            os.unlink(cls._tmp)

    def test_el_chip_del_porcentaje_ocupa_una_sola_linea(self):
        self.assertEqual(self.medidas["lineas_pct"], 1, self.medidas)
        self.assertFalse(self.medidas["pct_desborda"], self.medidas)

    def test_el_tiempo_restante_ocupa_una_sola_linea(self):
        self.assertEqual(self.medidas["lineas_rem"], 1, self.medidas)
        self.assertFalse(self.medidas["rem_desborda"], self.medidas)

    def test_el_chip_va_arriba_y_el_restante_debajo(self):
        """El reparto previsto: fila 1 reloj + chip, fila 2 el restante."""
        self.assertTrue(self.medidas["pct_en_la_linea_del_timer"], self.medidas)
        self.assertTrue(self.medidas["rem_en_su_propia_linea"], self.medidas)

    def test_nada_se_sale_de_la_columna(self):
        self.assertLessEqual(self.medidas["ancho_usado"], self.ANCHO_COLUMNA,
                             self.medidas)


if __name__ == "__main__":
    unittest.main()
