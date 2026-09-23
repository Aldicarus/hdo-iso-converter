"""«Cambiar película» y «Consulta rápida» comparten caja: y la estructura.

Los dos modales usan `.cmv40-lookup-modal-box`, que es un flex en columna
con **`padding: 0`** —lo ponen `head`, `body` y `foot` cada uno— y
`overflow: hidden`, porque el scroll vive dentro del body.

`ficha-modal` reusaba la clase pero NO la estructura: metía los resultados y
el pie directamente en la caja, con un `.modal-actions` en vez de
`.cmv40-lookup-foot`. Resultado: esos dos trozos se quedaban sin padding y
sin contenedor de scroll, y el modal se veía descuadrado y desbordado. Lo
reportó el usuario el 2026-09-23 abriéndolo desde la ficha TMDb de un
proyecto CMv4.0.

Reusar una clase de layout es un contrato: o se cumple entero o se copia el
CSS. Aquí se cumple entero, y este guard lo fija — el estático corre en
cualquier sitio y el de Chrome mide lo que de verdad se ve.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_modal_de_ficha -v
"""
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
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import html, stub_catalogo_es  # noqa: E402

PAREJA = ("cmv40-lookup-modal", "ficha-modal")

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)


def _caja_de(src: str, mid: str) -> str:
    """El marcado del `.modal-box` de un modal, por id."""
    i = src.index(f'id="{mid}"')
    j = src.index('<div class="modal-box', i)
    # Hasta el cierre del overlay: dos `</div>` seguidos al final del bloque.
    fin = src.index("\n</div>", j)
    return src[j:fin]


class TestElEsqueletoEsElMismo(unittest.TestCase):
    """Sin Chrome: es el guard, y no puede depender de tenerlo."""

    @classmethod
    def setUpClass(cls):
        cls.src = html()

    def _clases_de_seccion(self, mid: str) -> list[str]:
        caja = _caja_de(self.src, mid)
        return re.findall(r'<div class="(cmv40-lookup-(?:head|body|foot))"', caja)

    def test_los_dos_traen_head_body_y_foot(self):
        for mid in PAREJA:
            with self.subTest(modal=mid):
                self.assertEqual(
                    self._clases_de_seccion(mid),
                    ["cmv40-lookup-head", "cmv40-lookup-body",
                     "cmv40-lookup-foot"],
                    f"\n«{mid}» no respeta el contrato de "
                    f"`.cmv40-lookup-modal-box`: su padding es 0 y lo ponen "
                    f"head/body/foot. Sin los tres, el contenido sale pegado "
                    f"al borde y sin scroll.")

    def test_ninguno_usa_modal_actions_como_pie(self):
        for mid in PAREJA:
            with self.subTest(modal=mid):
                self.assertNotIn("modal-actions", _caja_de(self.src, mid))

    def test_los_resultados_van_dentro_del_body(self):
        for mid, res in (("cmv40-lookup-modal", "cmv40-lookup-results"),
                         ("ficha-modal", "ficha-resultados")):
            with self.subTest(modal=mid):
                caja = _caja_de(self.src, mid)
                cuerpo = caja.index('class="cmv40-lookup-body"')
                self.assertGreater(caja.index(f'id="{res}"'), cuerpo)

    def test_el_formulario_trae_las_tres_columnas_que_el_grid_espera(self):
        """`.cmv40-lookup-form` es `grid-template-columns: 1fr 120px auto`:
        título, año y botón. Con dos hijos el botón se va a la columna del
        año y el año se estira."""
        for mid in PAREJA:
            with self.subTest(modal=mid):
                caja = _caja_de(self.src, mid)
                form = caja[caja.index('class="cmv40-lookup-form"'):]
                self.assertEqual(form.count('class="cmv40-lookup-row'), 2)
                self.assertIn("cmv40-lookup-btn", form)

    def test_el_ano_es_numerico_en_los_dos(self):
        for mid in PAREJA:
            with self.subTest(modal=mid):
                caja = _caja_de(self.src, mid)
                fila = caja[caja.index("cmv40-lookup-row-year"):]
                self.assertIn('type="number"', fila)
                self.assertIn("cmv40-lookup-input-year", fila)

    def test_los_campos_con_hueco_para_el_boton_lo_tienen(self):
        """`.cmv40-lookup-input-title` y `-year` reservan 28 px a la derecha
        para el botón de limpiar. Sin el botón queda el hueco y nada dentro."""
        for mid in PAREJA:
            with self.subTest(modal=mid):
                caja = _caja_de(self.src, mid)
                con_hueco = len(re.findall(r"cmv40-lookup-input-(?:title|year)", caja))
                self.assertEqual(caja.count("cmv40-lookup-year-clear"), con_hueco)


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestSeVenIgual(unittest.TestCase):
    """Leer el CSS no basta: las reglas se leen bien en los dos casos y lo
    que estaba mal era DÓNDE colgaba cada nodo."""

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_abren_sin_errores(self):
        self.assertEqual(self.m["errores"], [])
        self.assertNotIn("error", self.m, self.m.get("error"))

    def test_la_caja_se_comporta_igual(self):
        a, b = (self.m[k]["caja"] for k in PAREJA)
        self.assertEqual(a, b)

    def test_el_cuerpo_scrollea_en_los_dos(self):
        for mid in PAREJA:
            with self.subTest(modal=mid):
                self.assertEqual(self.m[mid]["cuerpo"]["overflowY"], "auto")

    def test_el_padding_lo_ponen_las_secciones_y_no_la_caja(self):
        for mid in PAREJA:
            with self.subTest(modal=mid):
                self.assertEqual(self.m[mid]["caja"]["padding"], "0px")
                self.assertNotEqual(self.m[mid]["cuerpo"]["padding"], "0px")

    def test_el_formulario_reparte_igual(self):
        a, b = (self.m[k]["form"] for k in PAREJA)
        self.assertEqual(a["columnas"], b["columnas"])

    def test_nada_se_sale_de_la_caja(self):
        """El síntoma que reportó el usuario: el contenido desbordaba."""
        for mid in PAREJA:
            with self.subTest(modal=mid):
                d = self.m[mid]
                self.assertLessEqual(d["desborde"], 1,
                                     f"el contenido se sale {d['desborde']} px")


def _medir() -> dict:
    sonda = ("<style>*{transition:none!important;animation:none!important}</style>"
             "<script>window.__errores=[];"
             "window.addEventListener('error',e=>window.__errores.push("
             "(e.message||'')+' @ '+(e.filename||'').split('/').pop()));</script>")
    cuerpo = """
<pre id="__out"></pre>
<script>
(function () {
  const props = (el, ks) => {
    const cs = getComputedStyle(el); const o = {};
    for (const k of ks) o[k] = cs[k];
    return o;
  };
  setTimeout(() => {
    const out = {errores: window.__errores};
    try {
      for (const mid of ['cmv40-lookup-modal', 'ficha-modal']) {
        const ov = document.getElementById(mid);
        ov.classList.add('open');
        ov.style.display = 'flex';
        const caja = ov.querySelector('.modal-box');
        const cuerpo = ov.querySelector('.cmv40-lookup-body');
        const form = ov.querySelector('.cmv40-lookup-form');
        out[mid] = {
          caja: props(caja, ['display','flexDirection','padding','overflow',
                             'maxWidth','maxHeight','borderRadius']),
          cuerpo: props(cuerpo, ['overflowY','padding','flexGrow','minHeight']),
          form: {columnas: getComputedStyle(form).gridTemplateColumns
                   .split(' ').length},
          // Lo que el usuario vio: contenido fuera de la caja.
          desborde: Math.max(0, caja.scrollHeight - caja.clientHeight),
        };
        ov.classList.remove('open');
        ov.style.display = '';
      }
    } catch (e) { out.error = String(e && e.stack || e); }
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 700);
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
             "--window-size=1600,1000", "--virtual-time-budget=6000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    return json.loads(_html.unescape(m.group(1)))


if __name__ == "__main__":
    unittest.main()
