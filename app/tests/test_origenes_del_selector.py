"""Los cuatro selectores de MKV enseñan los tres sitios.

Cada uno exponía su subconjunto, y cada subconjunto tenía su motivo escrito:
«no tiene sentido procesar nuestro propio output como origen de un CMv4.0» y
«un MKV recién descargado no se edita, se ripea». Los dos resultaron falsos en
la práctica —se rehace un upgrade sobre un MKV que salió del converter, y se
abre uno descargado para mirarle la radiografía DV+HDR— y lo que producían era
tener que mover ficheros de sitio para que el selector los viera.

Se comprueba **abriendo los cuatro** y leyendo las pills que pintan, no
mirando el fuente: el `roots` que se le pasa a `openFileBrowser` y lo que
acaba en pantalla son dos cosas distintas —con menos de dos, el selector se
oculta entero— y es lo segundo lo que el usuario usa.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_origenes_del_selector -v
"""
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

# Los cuatro, con lo que hay que hacer para llegar a cada uno.
SELECTORES = {
    "abrir_mkv":       "_openMkvBrowserNow()",
    "comparador":      "abrirComparadorLuminancia()",
    "nuevo_cmv40":     "openNewCMv40Modal()",
    "cambiar_origen":  "openCMv40SourceBrowser()",
}


def _medir() -> dict:
    sonda = ("<style>*{transition:none!important;animation:none!important}</style>"
             "<script>window.__errores=[];"
             "window.addEventListener('error',e=>window.__errores.push("
             "(e.message||'')+' @ '+(e.filename||'').split('/').pop()"
             "+':'+e.lineno));</script>")
    cuerpo = """
<pre id="__out"></pre>
<script>
(function () {
  // El browser pide el contenido del root nada más abrir; da igual qué haya
  // dentro, lo que se mide son las pestañas de sitio.
  window.apiFetch = async () => ({entries: [], breadcrumb: [], base: '/x'});
  const SEL = __SEL__;
  const pills = () => [...document.querySelectorAll('#file-browser-roots .fb-root-btn')]
    .map(b => b.querySelector('.fb-root-label').textContent);
  setTimeout(async () => {
    const out = {errores: window.__errores, selectores: {}};
    for (const [nombre, abrir] of Object.entries(SEL)) {
      try {
        await eval(abrir);
        await new Promise(r => setTimeout(r, 60));
        out.selectores[nombre] = {
          pills: pills(),
          visible: getComputedStyle(
            document.getElementById('file-browser-roots')).display,
        };
        closeModal('file-browser-modal');
        await new Promise(r => setTimeout(r, 30));
      } catch (e) { out.selectores[nombre] = {error: String(e)}; }
    }
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 900);
})();
</script>
"""
    cuerpo = cuerpo.replace("__SEL__", json.dumps(SELECTORES))
    pagina = html().replace("</head>", sonda + "</head>")
    pagina = pagina.replace("</body>", cuerpo + "</body>")
    pagina = (pagina.replace('src="/static/', 'src="')
                    .replace('href="/static/', 'href="'))
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                      encoding="utf-8",
                                      dir=str(APP_DIR / "static"))
    tmp.write(pagina)
    tmp.close()
    try:
        dom = subprocess.run(
            [CHROME, "--headless", "--disable-gpu",
             "--allow-file-access-from-files", "--dump-dom",
             "--window-size=1600,1000", "--virtual-time-budget=7000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    import html as _h
    return json.loads(_h.unescape(m.group(1)))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestLosCuatroEnsenanLosTresSitios(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_se_abren_sin_errores(self):
        self.assertEqual(self.m["errores"], [])

    def test_los_tres_sitios_en_los_cuatro(self):
        for nombre, r in self.m["selectores"].items():
            with self.subTest(selector=nombre):
                self.assertNotIn("error", r, r.get("error"))
                self.assertEqual(r["pills"],
                                 ["Biblioteca", "Output", "Downloaded"])

    def test_y_el_selector_se_ve(self):
        """Con menos de dos roots se oculta entero, así que una lista mal
        pasada no se nota: sale un browser sin pestañas de sitio."""
        for nombre, r in self.m["selectores"].items():
            with self.subTest(selector=nombre):
                self.assertNotEqual(r["visible"], "none")


class TestElBackendLosAcepta(unittest.TestCase):
    """Quién enseña cuál nunca fue una cuestión de permisos: la lista blanca
    del servidor ya eran estos tres."""

    def test_las_tres_claves_existen(self):
        sys.path.insert(0, str(APP_DIR))
        import paths
        self.assertEqual(set(paths.LIBRARY_ROOTS),
                         {"library", "output", "downloaded"})


if __name__ == "__main__":
    unittest.main()
