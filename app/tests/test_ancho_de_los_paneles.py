"""El contenido de una sub-pestaña de proyecto ocupa TODO el ancho del centro.

Cada pestaña tenía su tope —860 px en Tab 1 (`.panel-project`), 900 en Tab 2
y 1100 en Tab 3, estos dos inline en el `.project-panel-inner` que pinta el
JS— así que con la ventana ancha quedaban huecos a los lados, y distintos en
cada pestaña: al cambiar de una a otra el contenido saltaba de ancho.
Reportado el 2026-10-13. Hoy las tres usan la misma regla de `style.css`, sin
`max-width`.

Se mide en Chrome sobre el `index.html` real, abriendo un proyecto en cada
pestaña con las funciones de la app: un tope puede volver por el CSS o por un
`style=` dentro de una plantilla del JS, y solo la pantalla ve los dos. La
ventana es de 1920 px para que el centro (1312 px) pase de largo cualquiera
de los topes viejos.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_ancho_de_los_paneles -v
"""
import html as _html
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import html, semilla_catalogo  # noqa: E402
import test_las_tres_lenguas_en_pantalla as P  # noqa: E402  (fixtures y sonda)

ANCHO_VENTANA = 1920

# Por pestaña: cómo abrir un proyecto, el contenedor con scroll y el elemento
# que define el ancho del contenido.
_GUION = r"""
(async () => {
  const espera = ms => new Promise(r => setTimeout(r, ms));
  const salida = {medidas: {}, fallos: {}};
  const PASOS = [
    ['tab1', () => { switchTab(1); openProject(%(tab1)s); },
     '#subtab-main', '#subtab-main > .panel-project.active-panel'],
    ['tab2', () => { switchTab(2); openMkvProject(%(tab2)s); },
     '#mkv-edit-panel', '#mkv-edit-panel > .mkv-panel .project-panel-inner'],
    ['tab3', () => { switchTab(3); openCMv40Project(%(tab3)s); },
     '#cmv40-subtab-content', '#cmv40-subtab-content > .cmv40-panel .project-panel-inner'],
  ];
  await espera(600);
  for (const [nombre, abrir, sel_host, sel_contenido] of PASOS) {
    try {
      abrir();
      await espera(700);
      const host = document.querySelector(sel_host);
      const cont = [...document.querySelectorAll(sel_contenido)].find(e => e.offsetParent);
      salida.medidas[nombre] = {
        host: host ? host.clientWidth : null,
        contenido: cont ? Math.round(cont.getBoundingClientRect().width) : null,
        izquierda: cont && host
          ? Math.round(cont.getBoundingClientRect().left - host.getBoundingClientRect().left)
          : null,
      };
    } catch (e) { salida.fallos[nombre] = String(e && e.message || e); }
  }
  const pre = document.createElement('pre');
  pre.id = '__out';
  pre.textContent = JSON.stringify(salida);
  document.body.appendChild(pre);
})();
"""


def _medir() -> dict:
    guion = _GUION % {
        "tab1": json.dumps(P.SESION_TAB1),
        "tab2": json.dumps(P.ANALISIS_MKV),
        "tab3": json.dumps(P.SESION_CMV40),
    }
    pagina = html().replace(
        "</head>", P._sonda(dict(P.RESPUESTAS)) + semilla_catalogo("es") + "</head>")
    pagina = pagina.replace(
        "</body>", f"<script>window.addEventListener('load', () => {{{guion}}});</script></body>")
    pagina = (pagina.replace('src="/static/', 'src="')
                    .replace('href="/static/', 'href="'))
    # En app/static para que los scripts se sirvan por file:// a su lado; el
    # prefijo `tmp` lo cubre el .gitignore si al proceso lo matan antes del unlink.
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", prefix="tmp", delete=False,
                                      encoding="utf-8", dir=str(APP_DIR / "static"))
    tmp.write(pagina)
    tmp.close()
    try:
        dom = subprocess.run(
            [P.CHROME, "--headless", "--disable-gpu", "--allow-file-access-from-files",
             "--dump-dom", f"--window-size={ANCHO_VENTANA},1100",
             "--virtual-time-budget=9000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    return json.loads(_html.unescape(m.group(1)))


@unittest.skipUnless(P.CHROME, "hace falta Chrome para medir la pantalla")
class TestElPanelOcupaTodoElAncho(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.salida = _medir()
        cls.medidas = cls.salida["medidas"]

    def test_se_abren_los_tres_proyectos(self):
        self.assertEqual(self.salida["fallos"], {})
        self.assertEqual(set(self.medidas), {"tab1", "tab2", "tab3"})

    def test_el_centro_es_mas_ancho_que_los_topes_viejos(self):
        # Sin esto, el test pasaría con un tope de 1100 px en una ventana
        # donde el centro no llega a 1100: no estaría midiendo nada.
        for tab, m in self.medidas.items():
            with self.subTest(tab=tab):
                self.assertGreater(m["host"], 1100)

    def test_cada_pestana_llena_su_centro(self):
        for tab, m in self.medidas.items():
            with self.subTest(tab=tab):
                self.assertIsNotNone(m["contenido"], "no se encontró el contenido del panel")
                self.assertEqual(m["contenido"], m["host"])
                self.assertEqual(m["izquierda"], 0)

    def test_las_tres_miden_lo_mismo(self):
        anchos = {tab: m["contenido"] for tab, m in self.medidas.items()}
        self.assertEqual(len(set(anchos.values())), 1, anchos)


if __name__ == "__main__":
    unittest.main()
