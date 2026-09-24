"""Cada pestaña de proyecto recuerda dónde se estaba leyendo.

Las tres pestañas tienen **un solo contenedor con scroll** y los paneles de
proyecto dentro —`#subtab-main`, `#mkv-edit-panel`, `#cmv40-subtab-content`—,
porque el `overflow` va en el padre a propósito: en WebKit, ponerlo en un
panel hijo flex deja sin scroll las zonas vacías. La consecuencia es que la
posición era del CONTENEDOR y no del proyecto, así que cambiar de pestaña la
perdía, por dos caminos distintos:

- **Tab 1** la ponía a cero explícitamente (`main.scrollTop = 0`).
- **Tab 2 y Tab 3** la recortaba el navegador al ocultar el panel saliente.
  Medido en Chrome antes de tocar nada: 450 → se oculta el hijo → 0, y al
  volver a mostrarlo **se queda en 0**.

Y un panel de proyecto es largo —la radiografía de Tab 2, las cards por fase
de Tab 3—, así que volver arriba cada vez que se mira otra cosa obliga a
buscar de nuevo dónde se estaba.

Lo que este fichero fija:

- **La posición es de cada panel y sobrevive a ir y volver**, en las tres.
- **Guardar va ANTES de ocultar el saliente.** Al cambiar a un panel más
  corto el contenedor encoge, así que leer el scroll después del cambio
  devuelve un cero y el panel que se deja pierde su sitio. Hace falta un
  panel corto en el fixture para verlo: con dos igual de altos la mutación
  pasa en verde, que es como estuvo el primer intento.
- Restaurar va al final por orden natural y **no** por necesidad: medido que
  en Chrome escribir el `scrollTop` con el contenedor vacío y poblarlo a
  continuación conserva el valor, incluso forzando un layout en medio. No se
  afirma en ningún test, porque pasaría en los dos órdenes.
- **Un panel nuevo empieza arriba**, aunque el contenedor viniera scrolleado.
- **La posición se va con el panel al cerrar**: se guarda en su `dataset`, no
  en un registro por id, así que no hay nada que limpiar — un registro
  tendría que enterarse de cada cierre y el que se olvidara filtraría en
  silencio.

También se midió que ocultar la pestaña PRINCIPAL (`display:none` sobre el
`tab-panel`) **no** pierde la posición: Chrome la devuelve al volver a
mostrarla (450 → 450). Por eso `switchTab` no toca nada, y hay un test que lo
fija para que nadie añada un reseteo «por simetría».

Se mide en Chrome sobre el `index.html` real porque es el navegador quien
recorta: leer el fuente no demostraría ninguna de las dos reglas de orden.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_scroll_por_proyecto -v
"""
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

from frontend_sources import html, stub_catalogo_es  # noqa: E402

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
import shutil  # noqa: E402
_CHROME_CANDIDATOS += [shutil.which("google-chrome") or "",
                       shutil.which("chromium") or ""]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)

_SONDA = ("<script>window.__errores=[];"
          "window.addEventListener('error',e=>window.__errores.push("
          "(e.message||'')+' @ '+(e.filename||'').split('/').pop()+':'+e.lineno));"
          "window.fetch=()=>new Promise(()=>{});"
          "window.WebSocket=function(){this.close=()=>{};};</script>")

# El guion mide las TRES pestañas con el mismo recorrido, usando en cada una
# su función real de creación de panel y su `switch*` real. El relleno le da
# al panel la altura que tendría un proyecto de verdad; lo que se prueba es
# la mecánica del scroll, no el contenido.
_CUERPO = """
<pre id="__out"></pre>
<script>
(function () {
  const alto = el => {
    const d = document.createElement('div');
    d.style.height = '2500px';
    el.appendChild(d);
    return el;
  };

  // Un recorrido idéntico para las tres: scrollear en A, ir a B (nuevo),
  // scrollear en B, volver a A y volver a B. Y luego el paso por C, que es
  // CORTO: es el único que destapa el orden de las dos llamadas.
  function recorrido(contId, panelDe, ir) {
    const c = document.getElementById(contId);
    const r = {};
    ir('A');
    // Después del primer cambio: en Tab 2 el contenedor está oculto hasta
    // que hay un proyecto abierto.
    r.visible = !!(c && c.clientHeight > 0);
    c.scrollTop = 600;         r.enA = c.scrollTop;
    ir('B');                   r.bNuevo = c.scrollTop;
    c.scrollTop = 200;         r.enB = c.scrollTop;
    ir('A');                   r.vueltaA = c.scrollTop;
    ir('B');                   r.vueltaB = c.scrollTop;
    // Y lo que quedó escrito en cada panel.
    r.datosA = panelDe('A').dataset.scroll;
    r.datosB = panelDe('B').dataset.scroll;

    // El paso por un panel CORTO. Con dos paneles igual de altos, ocultar
    // uno y mostrar otro en el mismo tick mantiene la altura y el scroll
    // aguanta aunque se lea tarde; con el entrante más corto el contenedor
    // encoge y lo que se lea después del cambio ya viene recortado. Es el
    // caso normal —un MKV sin análisis junto a uno con la radiografía
    // entera— y el único que distingue guardar a tiempo de guardar tarde.
    ir('A');
    c.scrollTop = 600;
    ir('C');                   r.enC = c.scrollTop;
    ir('A');                   r.trasElCorto = c.scrollTop;
    return r;
  }

  setTimeout(() => {
    const out = {errores: []};
    try {
      // ── Tab 1 ────────────────────────────────────────────────────
      switchTab(1);
      const p1 = {id: 'sA', sessionId: 'x', session: {}},
            p2 = {id: 'sB', sessionId: 'y', session: {}},
            p3 = {id: 'sC', sessionId: 'w', session: {}};
      openProjects.push(p1, p2, p3);
      [p1, p2].forEach(p => { createProjectPanel(p);
        alto(document.getElementById('panel-project-' + p.id)); });
      createProjectPanel(p3);   // corto: sin relleno
      out.tab1 = recorrido('subtab-main',
        k => document.getElementById('panel-project-s' + k),
        k => switchSubTab('s' + k));

      // ── Tab 2 ────────────────────────────────────────────────────
      switchTab(2);
      const m1 = {id: 'mA'}, m2 = {id: 'mB'}, m3 = {id: 'mC'};
      openMkvProjects.push(m1, m2, m3);
      [m1, m2].forEach(p => { _mkvCreatePanel(p);
        alto(document.getElementById('mkv-panel-' + p.id)); });
      _mkvCreatePanel(m3);      // corto: sin relleno
      out.tab2 = recorrido('mkv-edit-panel',
        k => document.getElementById('mkv-panel-m' + k),
        k => switchMkvSubTab('m' + k));

      // ── Tab 3 ────────────────────────────────────────────────────
      switchTab(3);
      const host = document.getElementById('cmv40-subtab-content');
      ['cA', 'cB', 'cC'].forEach(id => {
        openCMv40Projects.push({id, session: {id}});
        const d = document.createElement('div');
        d.className = 'cmv40-panel subtab-panel';
        d.id = 'cmv40-panel-' + id;
        d.style.display = 'none';
        host.appendChild(id === 'cC' ? d : alto(d));   // cC, corto
      });
      out.tab3 = recorrido('cmv40-subtab-content',
        k => document.getElementById('cmv40-panel-c' + k),
        k => switchCMv40SubTab('c' + k));

      // ── Cambiar de pestaña PRINCIPAL no pierde la posición ───────
      switchTab(1);
      switchSubTab('sA');
      const c1 = document.getElementById('subtab-main');
      c1.scrollTop = 350;
      switchTab(3);
      switchTab(1);
      out.entrePestanas = c1.scrollTop;

      // ── Y al cerrar, la posición se va con el panel ──────────────
      closeProject('sB');
      out.trasCerrar = {
        panel: !!document.getElementById('panel-project-sB'),
        // Un panel nuevo con el MISMO hueco empieza arriba.
        nuevo: (() => {
          const p = {id: 'sD', sessionId: 'z', session: {}};
          openProjects.push(p);
          createProjectPanel(p);
          alto(document.getElementById('panel-project-sD'));
          switchSubTab('sA');
          c1.scrollTop = 500;
          switchSubTab('sD');
          return c1.scrollTop;
        })(),
      };
    } catch (e) { out.errores.push('EXCEPCIÓN: ' + e.message); }
    out.errores = out.errores.concat(window.__errores);
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 700);
})();
</script>
"""


def _medir() -> dict:
    pagina = html().replace("</head>", _SONDA + stub_catalogo_es() + "</head>")
    pagina = pagina.replace("</body>", _CUERPO + "</body>")
    pagina = (pagina.replace('src="/static/', 'src="')
                    .replace('href="/static/', 'href="'))
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                      encoding="utf-8", dir=str(APP_DIR / "static"))
    tmp.write(pagina)
    tmp.close()
    try:
        dom = subprocess.run(
            [CHROME, "--headless", "--disable-gpu",
             "--allow-file-access-from-files", "--dump-dom",
             "--window-size=1280,1000", "--virtual-time-budget=7000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    return json.loads(m.group(1))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class ScrollCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def tabs(self):
        return [("Tab 1", self.m["tab1"]), ("Tab 2", self.m["tab2"]),
                ("Tab 3", self.m["tab3"])]


class TestElRecorridoSePinta(ScrollCase):

    def test_cero_errores_de_js(self):
        self.assertEqual(self.m["errores"], [])

    def test_los_tres_contenedores_se_ven_y_scrollean(self):
        # Sin altura visible no se puede medir nada y todo daría 0: el test
        # pasaría sin haber comprobado el scroll de ninguna pestaña.
        for nombre, r in self.tabs():
            with self.subTest(nombre):
                self.assertTrue(r["visible"], f"{nombre}: el contenedor no se ve")
                self.assertEqual(r["enA"], 600, f"{nombre}: no admite scroll")


class TestCadaPanelRecuerdaSuSitio(ScrollCase):

    def test_al_volver_se_recupera_la_posicion(self):
        for nombre, r in self.tabs():
            with self.subTest(nombre):
                self.assertEqual(r["vueltaA"], 600, f"{nombre}: A perdió su sitio")
                self.assertEqual(r["vueltaB"], 200, f"{nombre}: B perdió su sitio")

    def test_un_panel_nunca_visitado_empieza_arriba(self):
        # No hereda la posición del anterior, que es el otro fallo posible.
        for nombre, r in self.tabs():
            with self.subTest(nombre):
                self.assertEqual(r["bNuevo"], 0, f"{nombre}: B heredó el scroll de A")

    def test_la_posicion_se_guarda_en_el_panel(self):
        # En el `dataset`, no en un registro por id: así se va con el nodo.
        for nombre, r in self.tabs():
            with self.subTest(nombre):
                self.assertEqual(r["datosA"], "600", nombre)
                self.assertEqual(r["datosB"], "200", nombre)


class TestGuardarVaAntesDeOcultar(ScrollCase):
    """La única regla de orden que existe, y sólo el navegador la demuestra.

    De las dos mitades que parecían simétricas, ésta es la real. La otra
    —restaurar después de mostrar— **no se manifiesta**: comprobado por
    mutación que en Chrome escribir el `scrollTop` con el contenedor todavía
    vacío y poblarlo a continuación conserva el valor, incluso forzando un
    layout en medio. No hay test de eso porque pasaría en los dos órdenes, y
    un test que no distingue el comportamiento sólo aparenta cobertura.
    """

    def test_guardar_ocurre_antes_de_ocultar_el_saliente(self):
        # Al pasar por un panel corto el contenedor encoge, así que leer el
        # scroll después del cambio devuelve el valor ya recortado: A
        # volvería arriba. Con dos paneles igual de altos esto NO se ve —
        # el fixture no lo tenía y la mutación pasaba en verde.
        for nombre, r in self.tabs():
            with self.subTest(nombre):
                self.assertEqual(r["enC"], 0, f"{nombre}: el corto no encogió")
                self.assertEqual(r["trasElCorto"], 600,
                                 f"{nombre}: se guardó tarde y A perdió su sitio")


class TestLoQueNoHayQueTocar(ScrollCase):

    def test_cambiar_de_pestana_principal_no_pierde_la_posicion(self):
        # Chrome la devuelve solo al volver a mostrar el `tab-panel`. Si
        # alguien añade un reseteo «por simetría», esto lo caza.
        self.assertEqual(self.m["entrePestanas"], 350)


class TestAlCerrarNoQuedaNada(ScrollCase):

    def test_el_panel_desaparece_del_dom(self):
        self.assertFalse(self.m["trasCerrar"]["panel"])

    def test_un_proyecto_nuevo_empieza_arriba(self):
        self.assertEqual(self.m["trasCerrar"]["nuevo"], 0)


if __name__ == "__main__":
    unittest.main()
