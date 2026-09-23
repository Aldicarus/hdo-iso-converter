"""Las atribuciones obligatorias: están, y NO se traducen.

TMDb y MediaArea no piden «di de dónde salen los datos»: piden una frase
concreta, palabra por palabra. Traducirla la invalida, así que estas tres
cadenas son el único texto de la interfaz que se queda en inglés a
propósito y sin `data-i18n`.

Eso abre un fallo mudo por los dos lados:

- **si desaparecen**, nada falla. La app funciona igual y el incumplimiento
  solo se ve abriendo el modal y sabiendo qué buscar. Estuvo así desde el
  primer día: la app llevaba meses usando la API de TMDb sin una sola
  mención suya en pantalla.
- **si alguien las «arregla»** metiéndolas en el catálogo, tampoco falla:
  los guards de i18n persiguen CASTELLANO suelto, y un literal inglés no
  les dispara. Volvería el incumplimiento, en verde.

De ahí que este guard sea POSITIVO —exige que estén— y que además
compruebe que NO están en los catálogos.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_atribucion_tmdb -v
"""
from __future__ import annotations

import html as _html
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
RAIZ = APP_DIR.parent
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import html, stub_catalogo_es  # noqa: E402

INDEX = APP_DIR / "static" / "index.html"
AVISOS = RAIZ / "THIRD-PARTY-NOTICES.md"
LOGO = APP_DIR / "static" / "img" / "tmdb.svg"
CATALOGOS = [APP_DIR / "static" / "i18n" / f"{l}.json" for l in ("es", "en", "ca")]

# Palabra por palabra. Cambiar una coma aquí es cambiar el requisito, así
# que si un test falla por esto lo que hay que revisar es la fuente, no la
# constante: developer.themoviedb.org y mediaarea.net/en/MediaInfo/License.
TMDB = ("This product uses the TMDB API but is not endorsed or certified "
        "by TMDB.")
MEDIAINFO = ("This product uses MediaInfo library, Copyright (c) 2002-2026 "
             "MediaArea.net SARL.")
DOVI_TOOL = "dovi_tool — MIT License, Copyright (c) 2026 quietvoid"

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)


class TestLasFrasesEstanEnElMarcado(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")

    def test_la_de_tmdb(self):
        self.assertIn(TMDB, self.html)

    def test_la_de_tmdb_tambien_donde_se_configura_la_clave(self):
        """Una está en «Acerca de» y la otra en el bloque de TMDb de
        Integraciones, que es donde está quien pone la clave."""
        self.assertGreaterEqual(self.html.count(TMDB), 2)

    def test_la_de_mediainfo(self):
        self.assertIn(MEDIAINFO, self.html)

    def test_la_de_dovi_tool(self):
        self.assertIn(DOVI_TOOL, self.html)

    def test_tambien_en_los_avisos_de_terceros(self):
        t = AVISOS.read_text(encoding="utf-8")
        for frase in (TMDB, MEDIAINFO, DOVI_TOOL):
            with self.subTest(frase=frase[:40]):
                self.assertIn(frase, t)


class TestNadieLasTraduce(unittest.TestCase):
    """Meterlas en el catálogo las rompería, y ningún guard de i18n lo
    vería: los suyos buscan castellano."""

    def test_no_estan_en_ninguno_de_los_tres_catalogos(self):
        for cat in CATALOGOS:
            datos = json.loads(cat.read_text(encoding="utf-8"))
            for clave, valor in datos.items():
                if not isinstance(valor, str):
                    continue
                for frase in (TMDB, MEDIAINFO):
                    with self.subTest(idioma=cat.stem, clave=clave):
                        self.assertNotIn(frase[:45], valor, (
                            f"\n«{clave}» en {cat.name} contiene una cita que "
                            f"debe quedarse literal en inglés. El catálogo la "
                            f"traduciría y eso la invalida: déjala en el "
                            f"marcado, sin `data-i18n`."))

    def test_el_marcado_no_les_pone_data_i18n(self):
        h = INDEX.read_text(encoding="utf-8")
        for frase in (TMDB, MEDIAINFO, DOVI_TOOL):
            for m in re.finditer(re.escape(frase), h):
                # La etiqueta que la contiene no puede declarar traducción:
                # `pintarTextos` le sobreescribiría el contenido.
                ini = h.rfind("<", 0, m.start())
                etiqueta = h[ini:m.start()]
                with self.subTest(frase=frase[:40]):
                    self.assertNotIn("data-i18n", etiqueta, (
                        "la cita literal está dentro de un elemento con "
                        "`data-i18n`: al pintar se sustituye por el catálogo"))


class TestElLogoEstaYEsElOficial(unittest.TestCase):
    """TMDb exige usar su logo, sin alterarle color ni proporción. Es un
    ACTIVO de marca, no un glifo de `GLIFOS`: lleva su propio gradiente y no
    puede heredar `currentColor` como los 44 del catálogo."""

    def test_el_fichero_esta(self):
        self.assertTrue(LOGO.is_file(), "falta app/static/img/tmdb.svg")
        t = LOGO.read_text(encoding="utf-8")
        self.assertTrue(t.lstrip().startswith("<svg"), "no es un SVG")
        self.assertIn("linear-gradient", t,
                      "el logo oficial lleva gradiente; ¿se ha recoloreado?")

    def test_el_marcado_lo_referencia(self):
        h = INDEX.read_text(encoding="utf-8")
        self.assertRegex(h, r'src="/static/img/tmdb\.svg')

    def test_no_se_colo_en_el_catalogo_de_glifos(self):
        # Por `js_completo()` y no leyendo core.js a pelo: el frontend son
        # nueve scripts y su orden lo manda `index.html`. Una ruta escrita
        # aquí se desincroniza en el primer cambio y el test seguiría en
        # verde midiendo otra cosa. Lo exige `test_frontend_troceado`.
        from frontend_sources import js_completo
        js = js_completo()
        tras = js.split("const GLIFOS")[-1]
        self.assertNotEqual(tras, js, "no se encuentra el catálogo GLIFOS")
        self.assertNotIn("tmdb:", tras[:20000], (
            "el logo de TMDb no puede ser un glifo del catálogo: lleva su "
            "gradiente y su licencia prohíbe recolorearlo, así que no puede "
            "heredar `currentColor` como los demás"))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestSeVENEnLaPantalla(unittest.TestCase):
    """Estar en `index.html` no es estar a la vista: el bloque podría no
    entrar en ninguna sección —`_montarSeccionesDeAjustes` lo dejaría
    fuera del panel sin dar un error— o quedar en una pestaña inalcanzable.
    Es el fallo mudo que ya documenta `test_secciones_de_ajustes`."""

    @classmethod
    def setUpClass(cls):
        cls.visto = _medir_pantalla()

    def test_el_modal_abre_sin_errores(self):
        self.assertEqual(self.visto["errores"], [])
        self.assertNotIn("error", self.visto, self.visto.get("error"))

    def test_la_seccion_acerca_existe_y_se_puede_abrir(self):
        self.assertIn("acerca", self.visto["secciones"])
        self.assertEqual(self.visto["visible"], "acerca")

    def test_la_frase_de_tmdb_se_lee(self):
        self.assertIn(TMDB, self.visto["texto"])

    def test_la_de_mediainfo_tambien(self):
        self.assertIn(MEDIAINFO, self.visto["texto"])

    def test_el_logo_se_pinta(self):
        self.assertEqual(self.visto["logos"], 1)


def _medir_pantalla() -> dict:
    sonda = ("<style>*{transition:none!important;animation:none!important}</style>"
             "<script>window.__errores=[];"
             "window.addEventListener('error',e=>window.__errores.push("
             "(e.message||'')+' @ '+(e.filename||'').split('/').pop()"
             "+':'+e.lineno));</script>")
    cuerpo = """
<pre id="__out"></pre>
<script>
(function () {
  window.apiFetch = async () => ({
    tmdb:   {configured: true, source: 'default', is_default: true},
    google: {configured: false, source: null},
    sheet:  {configured: true, source: 'default', url: 'https://example.invalid/x',
             default_url: 'https://example.invalid/x', is_default: true},
    'drive-folder': {configured: true, source: 'default'},
    idioma: {activo: 'es', disponibles: ['es', 'en', 'ca'], por_defecto: 'es'},
  });
  window.checkForUpdates = async () => {};
  setTimeout(async () => {
    const out = {errores: window.__errores};
    try {
      await openSettingsModal();
      await new Promise(r => setTimeout(r, 120));
      out.secciones = [...document.querySelectorAll(
        '#settings-panel .settings-seccion')].map(c => c.dataset.seccion);
      activarSeccionDeAjustes('acerca');
      await new Promise(r => setTimeout(r, 60));
      const caja = document.querySelector(
        '#settings-panel .settings-seccion[data-seccion="acerca"]');
      out.visible = [...document.querySelectorAll('#settings-panel .settings-seccion')]
        .filter(c => getComputedStyle(c).display !== 'none')
        .map(c => c.dataset.seccion)[0];
      // `innerText` y no `textContent`: lo que se LEE, no lo que hay.
      out.texto = caja ? caja.innerText.replace(/\\s+/g, ' ') : '';
      out.logos = caja
        ? caja.querySelectorAll('img[src*="tmdb.svg"]').length : 0;
    } catch (e) { out.error = String(e && e.stack || e); }
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 900);
})();
</script>
"""
    pagina = html().replace("</head>", sonda + stub_catalogo_es() + "</head>")
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
    return json.loads(_html.unescape(m.group(1)))


if __name__ == "__main__":
    unittest.main()
