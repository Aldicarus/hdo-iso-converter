"""La columna de trabajo es una pestaña más, fijada a la derecha.

Vivía dentro de `#app-body`, así que su cabecera arrancaba por debajo de la
tira de pestañas y medía 22 px contra los 63 de la franja del sidebar y del
`#subtab-bar`. El resultado es que todo lo de la columna quedaba desplazado
hacia arriba respecto a las otras dos columnas y la pieza se leía como algo
pegado por fuera de la aplicación.

Ahora `#workbar` es hermana de `#app-main-stack` y es dueña de su cabecera:
una pestaña con la misma altura y forma que las tres de arriba, y debajo una
franja de 63 px. Que las alturas casen es el objetivo del bloque, así que se
**mide en Chrome**; leer el CSS no lo demostraría, porque las reglas se leen
igual de bien estén alineadas o no.

Dos decisiones que este fichero fija, y que no son evidentes:

- **La pestaña NO va en navy.** En esta aplicación el navy significa «el panel
  que estás viendo» y habría dos encendidos a la vez. Lleva el filete navy
  arriba, que la ata al hub sin reclamarlo.
- **El buscador vive FUERA de `#workbar-body`.** El cuerpo se repinta entero
  con cada poll (2 s); un input ahí dentro perdería el foco y el cursor dos
  veces por segundo. Se comprueba escribiendo y forzando un render.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_columna_como_pestana -v
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

from frontend_sources import html, js_completo  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)

VENTANA = (1600, 1000)

_TRABAJOS = {
    "activo": {
        "id": "p1", "sobre": "p1", "tab": "cmv40", "tipo": "fase_cmv40",
        "que": "Fase C — Extrayendo BL/EL · Predator (2026)",
        "fase": "extract", "fase_label": "Fase C — Extrayendo BL/EL",
        "paso": "Demuxing BL/EL", "fase_n": 3, "fases_total": 7, "pct": 41,
        "pct_medido": True, "segundos": 742, "eta_s": 1020,
        "eta_fuente": "medido", "cancelable": True, "detalle": "cmv40"},
    "cola": [
        {"id": "rip:d1", "sobre": "d1", "tab": "rip", "tipo": "rip",
         "que": "Conversión a MKV · Dune (2024)", "posicion": 1}],
    "interactivo": [
        {"id": "a1", "sobre": "a1", "tab": "mkv", "tipo": "analisis_extendido",
         "que": "Apertura de un MKV · Blade Runner (1982)", "segundos": 12,
         "detalle": "", "cancelable": False}],
    "recientes": [
        {"id": "h1", "tab": "rip", "tipo": "rip", "que": "Conversión a MKV · Alien (1979)",
         "inicio": "2026-09-11T08:00:00+00:00", "fin": "2026-09-11T08:31:00+00:00",
         "segundos": 1860, "estado": "done", "error": None, "ref_log": None}],
}


def _fn(nombre: str) -> str:
    i = JS.index(f"function {nombre}(")
    return JS[JS.rindex("\n", 0, i) + 1:JS.index("\n}\n", i) + 3]


def _node(guion: str) -> dict:
    r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True,
                       timeout=30)
    if r.returncode != 0:
        raise AssertionError(f"node falló:\n{r.stderr[:900]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def _medir() -> dict:
    # Sin transiciones ni animaciones: `getBoundingClientRect` durante la
    # transición de ancho devuelve el valor de PARTIDA (headless no produce
    # frames), así que la columna se medía a 28 px estando abierta.
    sonda = ("<style>*{transition:none!important;animation:none!important}</style>"
             "<script>window.__errores=[];"
             "window.addEventListener('error',e=>window.__errores.push("
             "(e.message||'')+' @ '+(e.filename||'').split('/').pop()"
             f"+':'+e.lineno));window.__T={json.dumps(_TRABAJOS)};</script>")
    cuerpo = """
<pre id="__out"></pre>
<script>
(function () {
  window.apiFetch = async (url) =>
    url.startsWith('/api/trabajos') ? window.__T : null;
  const r = el => { const b = el && el.getBoundingClientRect();
    return b ? {t: +b.top.toFixed(1), l: +b.left.toFixed(1),
                w: +b.width.toFixed(1), h: +b.height.toFixed(1),
                b: +b.bottom.toFixed(1), r: +b.right.toFixed(1)} : null; };
  const q = s => document.querySelector(s);
  setTimeout(async () => {
    // La tira, con la casa libre y con trabajo: tiene que medir lo mismo.
    _workbarRender({activo: null, cola: [], interactivo: [], recientes: []});
    await new Promise(r => setTimeout(r, 30));
    const barVacia = r(q('#tab-bar')).h;
    workbarEstado = window.__T;
    _workbarRender(workbarEstado);
    await new Promise(r => setTimeout(r, 80));

    const tab = q('.workbar-tab');
    const out = {
      errores: window.__errores,
      ventana: {w: window.innerWidth, h: window.innerHeight},
      columna:    r(q('#workbar')),
      tabColumna: r(tab),
      franja:     r(q('.workbar-shelf')),
      cuerpo:     r(q('#workbar-body')),
      cuerpoMax:  getComputedStyle(q('#workbar-body')).maxHeight,
      pills:      r(q('.workbar-pills')),
      tabBar:     r(q('#tab-bar')),
      barVacia,
      puntoDesplegada: getComputedStyle(q('.workbar-toggle'), '::after').content,
      tabActiva:  r(q('#tab-bar .tab.active')),
      tabInactiva:r(q('#tab-bar .tab:not(.active)')),
      franjaSidebar: r(q('#sidebar-tab-1 .sidebar-new-project-area')),
      franjaCentro:  r(q('#subtab-bar')),
      estiloColumna: { bg: getComputedStyle(q('#workbar')).backgroundColor },
      estiloTab: {
        bg:     getComputedStyle(tab).backgroundColor,
        // El navy de la pestaña activa es un GRADIENTE, así que mirar solo el
        // backgroundColor deja pasar justo la mutación que hay que cazar.
        img:    getComputedStyle(tab).backgroundImage,
        sombra: getComputedStyle(tab).boxShadow,
        radio:  getComputedStyle(tab).borderTopLeftRadius,
      },
      estiloTabActiva: {
        bg: getComputedStyle(q('#tab-bar .tab.active')).backgroundImage,
      },
      // El foco tiene que sobrevivir a un repintado del cuerpo.
      foco: null, valor: null,
    };
    const inp = document.getElementById('workbar-search');
    inp.focus();
    inp.value = 'preda';
    inp.dispatchEvent(new Event('input'));
    await new Promise(r => setTimeout(r, 30));
    _workbarRender(workbarEstado);            // el poll, otra vez
    out.foco = document.activeElement && document.activeElement.id;
    out.valor = inp.value;
    out.htmlFiltrado = document.getElementById('workbar-body').innerHTML;
    out.cuentaConFiltro = document.getElementById('workbar-count').textContent;

    // Y plegada. Se pliega llamando a `_aplicarEstadoWorkbar`, que es quien lo
    // hace de verdad, en vez de poniendo las clases a mano: si no, el test no
    // puede ver que la pestaña —que vive en otro subárbol— se quede sin
    // plegar. Se fuerza el predicado en lugar de usar `toggleWorkbar` porque
    // bajo file:// el `localStorage.setItem` no persiste.
    inp.value = '';
    workbarAbierta = () => false;
    _aplicarEstadoWorkbar();
    await new Promise(r => setTimeout(r, 30));
    out.plegada = r(q('#workbar'));
    out.tabPlegada = r(q('.workbar-tab'));
    out.puntoPlegada = getComputedStyle(q('.workbar-toggle'), '::after').content;
    out.franjaPlegada = r(q('.workbar-shelf'));

    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 900);
})();
</script>
"""
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
             f"--window-size={VENTANA[0]},{VENTANA[1]}",
             "--virtual-time-budget=7000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    import html as _h
    return json.loads(_h.unescape(m.group(1)))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestLaColumnaEsUnaPestanaMas(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_la_pagina_carga_sin_errores(self):
        self.assertEqual(self.m["errores"], [])

    def test_sigue_midiendo_288_pegada_al_borde(self):
        self.assertEqual(self.m["columna"]["w"], 288)
        self.assertAlmostEqual(self.m["columna"]["r"], self.m["ventana"]["w"],
                               delta=1)

    def test_la_pestana_esta_justo_encima_de_la_columna(self):
        """Misma anchura y mismo borde derecho: la pestaña es la cabecera de
        ESA columna, no una cuarta pestaña que flota al final de la tira."""
        self.assertEqual(self.m["tabColumna"]["w"], self.m["columna"]["w"])
        self.assertAlmostEqual(self.m["tabColumna"]["r"],
                               self.m["columna"]["r"], delta=1)
        self.assertAlmostEqual(self.m["tabColumna"]["b"],
                               self.m["columna"]["t"], delta=1.5,
                               msg="hay un hueco entre la pestaña y la columna")

    def test_su_pestana_ocupa_la_misma_franja_que_las_otras(self):
        """Lo da `align-self: stretch` compartiendo fila. Fuera de #tab-bar no
        casaba: el alto de una pestaña lo marca el emoji de su icono, y con un
        SVG se quedaba 3,5 px más baja.

        Lo que se afirma son los DOS bordes, no el alto: la activa se desplaza
        1 px hacia abajo por su `margin-bottom:-1px` —así solapa el filete— y
        con eso su alto y su borde superior ya no coinciden con los de las
        inactivas. La pestaña de la columna hace las dos cosas: arranca al
        nivel de las inactivas y cierra en la línea de la tira."""
        self.assertAlmostEqual(self.m["tabColumna"]["b"],
                               self.m["tabBar"]["b"], delta=0.5,
                               msg="no cierra en la línea de la tira")
        # Contra la tira y no contra otra pestaña: el alto de una pestaña lo
        # marca su emoji, y el emoji no siempre está disponible con el mismo
        # tipo en el momento de medir (visto 43 y 41 en dos vueltas). Los
        # 8 px son el `padding-top` de #tab-bar, que no depende de la fuente.
        self.assertAlmostEqual(self.m["tabColumna"]["t"],
                               self.m["tabBar"]["t"] + 8, delta=0.5,
                               msg="su borde superior no arranca con la tira")
        self.assertGreaterEqual(self.m["tabColumna"]["h"],
                                self.m["tabActiva"]["h"] - 0.5,
                                "más baja que las otras: quedaría un escalón")

    def test_la_franja_de_63_cruza_la_ventana_entera(self):
        """Es el punto del bloque: la línea horizontal que separa el hub del
        contenido no puede escalonarse al llegar a la columna."""
        self.assertAlmostEqual(self.m["franja"]["h"], 63, delta=1)
        self.assertAlmostEqual(self.m["franja"]["b"],
                               self.m["franjaSidebar"]["b"], delta=1,
                               msg="la franja de la columna no casa con la del sidebar")
        self.assertAlmostEqual(self.m["franja"]["b"],
                               self.m["franjaCentro"]["b"], delta=1,
                               msg="la franja de la columna no casa con el subtab-bar")

    def test_la_pestana_NO_va_en_navy(self):
        """Decisión tomada: el navy significa «el panel que estás viendo» y
        habría dos encendidos a la vez."""
        pintura = " ".join([self.m["estiloTab"]["bg"], self.m["estiloTab"]["img"],
                            self.m["estiloTab"]["sombra"]])
        self.assertEqual(self.m["estiloTab"]["img"], "none",
                         "la pestaña de la columna no lleva gradiente")
        self.assertNotIn("34, 67, 108", pintura)      # --active-hub
        self.assertNotIn("38, 74, 120", pintura)      # --active-hub-top
        self.assertIn("gradient", self.m["estiloTabActiva"]["bg"],
                      "la pestaña de contenido sí debe seguir en navy")

    def test_la_pestana_va_del_color_de_SU_columna(self):
        """Es lo que la convierte en su cabecera en vez de en una cuarta
        pestaña que flota al final de la tira. Sustituye al filete navy, que
        con el redondeo se curvaba envolviendo la esquina."""
        self.assertEqual(self.m["estiloTab"]["bg"],
                         self.m["estiloColumna"]["bg"])

    def test_ese_color_sale_de_la_paleta(self):
        """`--cola-surface`, el azul que la app ya reservaba para la superficie
        de la cola. Ni un gris inventado ni el del sidebar de proyectos."""
        self.assertEqual(self.m["estiloColumna"]["bg"], "rgb(237, 244, 255)")

    def test_tiene_forma_de_pestana(self):
        self.assertEqual(self.m["estiloTab"]["radio"], "8px")

    def test_arrancar_un_trabajo_no_mueve_la_tira_de_pestanas(self):
        """El punto verde de «hay trabajo» iba en el flujo del botón: crecía
        10 px y con él la pestaña, así que la fila entera daba un salto de
        3 px en cuanto empezaba un job. Va posicionado."""
        self.assertEqual(self.m["tabBar"]["h"], self.m["barVacia"])

    def test_el_punto_de_aviso_solo_sale_plegada(self):
        """Desplegada ya está el contador al lado y el aro girando en la
        tarjeta del activo; ahí el punto quedaba incrustado en la curva de la
        esquina de la pestaña."""
        self.assertEqual(self.m["puntoDesplegada"], "none")
        self.assertNotEqual(self.m["puntoPlegada"], "none")

    def test_plegada_se_encoge_la_columna_Y_su_pestana(self):
        """Las dos, o la pestaña quedaría en voladizo sobre 28 px de columna."""
        self.assertEqual(self.m["plegada"]["w"], 28)
        self.assertEqual(self.m["tabPlegada"]["w"], 28)
        # `display:none` da un rect a cero, no null.
        self.assertEqual(self.m["franjaPlegada"]["h"], 0)


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestElBuscadorSobreviveAlPoll(unittest.TestCase):
    """El cuerpo se repinta entero cada 2 s. Un input dentro de
    `#workbar-body` perdería el foco y el cursor mientras escribes — por eso
    vive en el HTML, fuera del cuerpo."""

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_el_foco_y_el_texto_siguen_ahi_tras_repintar(self):
        self.assertEqual(self.m["foco"], "workbar-search")
        self.assertEqual(self.m["valor"], "preda")

    def test_y_mientras_tanto_filtra(self):
        h = self.m["htmlFiltrado"]
        self.assertIn("Predator", h)
        self.assertNotIn("Dune", h)
        self.assertNotIn("Blade Runner", h)

    def test_el_contador_NO_se_filtra(self):
        """Es el indicador de «hay trabajo» y es lo que lleva la tira plegada:
        buscar una película no puede apagar el aviso de que algo corre."""
        self.assertEqual(self.m["cuentaConFiltro"], "3")


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElFiltroCubreLasCuatroSecciones(unittest.TestCase):

    def _render(self, tab="all", texto=""):
        guion = f"""
globalThis.escHtml = t => String(t);
const _els = {{}};
for (const id of ['workbar-body', 'workbar-count', 'workbar-toggle', 'workbar-search', 'workbar-historial']) {{
  _els[id] = {{ value: '', style: {{}}, dataset: {{}}, textContent: '',
    innerHTML: '', classList: {{ _v: new Set(),
      toggle(c, on) {{ on ? this._v.add(c) : this._v.delete(c); }},
      has(c) {{ return this._v.has(c); }} }} }};
}}
_els['workbar-search'].value = {json.dumps(texto)};
globalThis.document = {{ getElementById: id => _els[id] || null,
                         querySelector: () => null }};
globalThis.Sortable = undefined;
let workbarEstado = {json.dumps(_TRABAJOS)};
let _workbarSeleccion = null;
let _workbarFiltroTab = {json.dumps(tab)};
{_fn('normalizeSearch')}
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
{_fn('_workbarBusqueda')}
{_fn('_workbarFiltrando')}
{_fn('_workbarPasaFiltro')}
{_fn('_workbarRefReciente')}
{_fn('_workbarListaHTML')}
{_fn('_workbarActivoHTML')}
{_fn('_instalarReordenDeCola')}
globalThis.iconoDeTrabajo = () => '';
globalThis.iconoDeEstado = () => '';
const _CMV40_FIN = {{done: 'Terminado'}};
{_fn('_workbarMini')}
{_fn('_workbarDescripcion')}
{_fn('_workbarPips')}
{_fn('_workbarChips')}
{_fn('_workbarTarjeta')}
{_fn('_workbarDia')}
{_fn('_workbarHace')}
{_fn('_workbarTarjetaReciente')}
let _workbarHayMasHistorial = false;
const _WORKBAR_HISTORIAL_PASO = 25;
{_fn('_workbarRenderHistorial')}
{_fn('_workbarRender')}
_workbarRender(workbarEstado);
console.log(JSON.stringify({{html: (_els['workbar-body'].innerHTML || '') + (_els['workbar-historial'].innerHTML || ''),
                            cuenta: _els['workbar-count'].textContent}}));
"""
        return _node(guion)

    def test_sin_filtro_salen_los_cuatro(self):
        h = self._render()["html"]
        for peli in ("Predator", "Dune", "Blade Runner", "Alien"):
            self.assertIn(peli, h)

    def test_el_pill_de_una_pestana_deja_solo_lo_suyo(self):
        h = self._render(tab="rip")["html"]
        self.assertIn("Dune", h)        # cola
        self.assertIn("Alien", h)       # historial
        self.assertNotIn("Predator", h)
        self.assertNotIn("Blade Runner", h)

    def test_el_buscador_atraviesa_las_cuatro_secciones(self):
        h = self._render(texto="alien")["html"]
        self.assertIn("Alien", h)
        self.assertNotIn("Predator", h)

    def test_sin_coincidencias_lo_dice_en_vez_de_fingir_calma(self):
        r = self._render(texto="zzz")
        self.assertIn("coincide con el filtro", r["html"])
        self.assertNotIn("No hay nada en ejecución", r["html"])

    def test_y_el_contador_sigue_contando_todo(self):
        self.assertEqual(self._render(texto="zzz")["cuenta"], "3")


class TestLosPillsSonLosDeLasPestanas(unittest.TestCase):
    """Un SVG aquí no se leería como «la pestaña del disco»: el pill tiene que
    llevar el mismo glifo que la pestaña a la que se refiere. Es la excepción
    documentada a la regla de iconografía de la columna — y si alguien cambia
    el icono de una pestaña, el pill tiene que seguirlo."""

    def test_cada_pill_lleva_el_glifo_de_su_pestana(self):
        h = html()
        tabs = dict(re.findall(
            r'onclick="switchTab\((\d)\)".*?<span class="tab-icon">(.+?)</span>',
            h, re.S))
        # switchTab(1)=ISO→MKV (rip) · (2)=Editar (mkv) · (3)=CMv4.0
        esperado = {"rip": tabs["1"], "mkv": tabs["2"], "cmv40": tabs["3"]}
        pills = dict(re.findall(r'data-tab="([a-z0-9]+)"[^>]*>\s*(.+?)</button>',
                                h[h.index('<div class="workbar-pills">'):], re.S))
        for tab, glifo in esperado.items():
            self.assertEqual(pills[tab].strip(), glifo.strip(),
                             f"el pill de {tab} no lleva el glifo de su pestaña")


class TestElBuscadorNoViveEnElCuerpo(unittest.TestCase):
    """La razón por la que sobrevive al poll, dicha sobre el marcado: si
    alguien lo mueve dentro de `#workbar-body`, el test de Chrome tardaría en
    contarlo y este lo dice en el sitio."""

    def test_el_input_esta_fuera_de_workbar_body(self):
        h = html()
        cuerpo = h[h.index('<div id="workbar-body">'):]
        self.assertNotIn('id="workbar-search"', cuerpo)
        self.assertNotIn('class="wb-pill', cuerpo)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestSinHistorialNoQuedaMediaColumnaEnBlanco(unittest.TestCase):
    """La zona de «ahora» tuvo un `max-height: 62%`, y con el historial vacío
    —que se oculta con `:empty`— el 38 % restante se quedaba en gris: media
    columna de nada que además empujaba el contenido fuera de la vista."""

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_la_zona_de_ahora_llega_hasta_abajo(self):
        col = self.m["columna"]
        cuerpo = self.m["cuerpo"]
        self.assertIsNotNone(cuerpo, "no se midió #workbar-body")
        # Sin historial, el cuerpo llega al fondo de la columna (o hasta donde
        # llegue su contenido, que aquí es más corto).
        self.assertLessEqual(cuerpo["b"], col["b"] + 1)
        self.assertGreater(cuerpo["h"], 0)

    def test_y_no_se_le_pone_un_tope_de_alto(self):
        self.assertEqual(self.m["cuerpoMax"], "none")
