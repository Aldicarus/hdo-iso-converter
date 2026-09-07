"""Tab 2 admite varios MKV abiertos a la vez, como Tab 1 y Tab 3.

Tab 2 tenía UN MKV en una variable (`let mkvProject`) y un único panel con 51
ids fijos en el DOM. Abrir otro descartaba el anterior. Ahora hay una colección
`openMkvProjects` con su barra de sub-pestañas, y **los 12 ids que viven dentro
del panel llevan el id del proyecto como sufijo** (`mkv-audio-list-m1`), igual
que hace Tab 3.

Los otros 39 ids se quedan globales a propósito: 3 son del armazón de la
pestaña y 36 de los tres modales, que siguen siendo únicos porque los trabajos
pesados que muestran son singleton.

Lo que estos tests protegen, y por qué cada cosa:

* **Los ids de dos paneles abiertos no se solapan.** Es el fallo silencioso de
  este cambio: con un id repetido, `getElementById` devuelve el PRIMERO del
  documento, así que editar los capítulos del segundo MKV repintaría los del
  primero sin dar ningún error. Por eso el test no mira el fuente: ejecuta el
  render real de dos proyectos y compara los ids que salen.
* **`mkvProject` sigue existiendo y devuelve el activo.** Es un getter, no una
  variable espejo, para que no pueda desincronizarse — y `showRawMkvData`, que
  vive en `tab1.js`, lo lee desde fuera.
* **Los gradientes del histograma.** `hist-0`…`hist-6` eran fijos por índice
  (los otros tres visualizadores ya llevaban sufijo aleatorio). Con dos paneles
  a la vez, el segundo SVG redefine los `<linearGradient>` del primero.

Las funciones se evalúan en **node** sobre un DOM mínimo escrito aquí, con el
mismo patrón que `test_cmv40_plan_frontend`. Las fuentes salen de
`frontend_sources`, nunca de una ruta a `tab2.js`.

Ejecutar desde la raíz del repo:
    .venv/bin/python -m unittest app.tests.test_tab2_subpestanas -v
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import html, js_completo  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()
HTML = html()

# Los 12 ids que viven DENTRO del panel de un proyecto y que, por tanto, tienen
# que llevar sufijo. Los otros 39 de Tab 2 se quedan globales a propósito.
IDS_DEL_PANEL = [
    "mkv-edit-tmdb-card", "mkv-audio-list", "mkv-sub-list",
    "mkv-chapters-generic-btn", "mkv-chapters-banner", "mkv-chapters-icon",
    "mkv-chapters-text", "mkv-chapters-autogen-btn",
    "mkv-chapter-timeline-wrap", "mkv-timeline-marks", "mkv-timeline-cursor",
    "mkv-chapters-list",
]

# Los que NO se tocan: el armazón de la pestaña y los tres modales (singleton).
IDS_GLOBALES = ["mkv-action-bar", "mkv-empty-state", "mkv-edit-panel",
                "mkv-analyze-modal", "mkv-quality-modal", "mkv-apply-modal"]


def _funcion(nombre: str) -> str:
    """El fuente de una función top-level, tal cual lo carga el navegador."""
    marca = f"\nfunction {nombre}("
    i = JS.index(marca) + 1
    return JS[i:JS.index("\n}\n", i) + 3]


def _bloque(desde: str, hasta: str) -> str:
    """Un tramo literal del fuente, entre dos anclas."""
    i = JS.index(desde)
    return JS[i:JS.index(hasta, i)]


# El estado, el getter y los helpers de proyecto: un tramo continuo del fichero.
BLOQUE_ESTADO = _bloque("const openMkvProjects = [];", "let _mkvPickerSelected")
# La apertura y toda la maquinaria de sub-pestañas: otro tramo continuo.
BLOQUE_SUBTABS = _bloque("function openMkvProject(analysis) {",
                         "/**\n * Re-analiza el MKV")

# DOM mínimo: sólo lo que este código toca. No es jsdom y no pretende serlo.
DOM = r"""
'use strict';
globalThis.window = globalThis;

const _porId = new Map();

class FakeEl {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.dataset = {};
    this.onclick = null;
    this.disabled = false;
    this.scrollWidth = 0;
    this.clientWidth = 0;
    this.scrollLeft = 0;
    this._id = '';
    this._html = '';
    this._clases = new Set();
    const yo = this;
    this.classList = {
      add:      c => yo._clases.add(c),
      remove:   c => yo._clases.delete(c),
      contains: c => yo._clases.has(c),
      toggle:   (c, on) => { if (on) yo._clases.add(c); else yo._clases.delete(c); },
    };
  }
  get id() { return this._id; }
  set id(v) {
    if (this._id) _porId.delete(this._id);
    this._id = v;
    if (v) _porId.set(v, this);
  }
  get className() { return [...this._clases].join(' '); }
  set className(v) { this._clases = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); }
  appendChild(hijo) { hijo.parentNode = this; this.children.push(hijo); return hijo; }
  remove() {
    if (this.parentNode) {
      const i = this.parentNode.children.indexOf(this);
      if (i !== -1) this.parentNode.children.splice(i, 1);
      this.parentNode = null;
    }
    if (this._id) _porId.delete(this._id);
  }
  addEventListener() {}
}

function _descendientes(el) {
  const fuera = [];
  for (const h of el.children) { fuera.push(h); fuera.push(..._descendientes(h)); }
  return fuera;
}

globalThis.document = {
  getElementById: id => _porId.get(id) || null,
  createElement: t => new FakeEl(t),
  querySelectorAll(sel) {
    // Sólo los dos selectores que usa este código: "#host > .clase" y "#host .clase".
    const m = sel.match(/^#([\w-]+)\s*(>?)\s*\.([\w-]+)$/);
    if (!m) return [];
    const host = _porId.get(m[1]);
    if (!host) return [];
    const pool = m[2] === '>' ? host.children : _descendientes(host);
    return pool.filter(e => e.classList.contains(m[3]));
  },
};
globalThis.addEventListener = () => {};

// El armazón de #tab-panel-2 que el código espera encontrar.
for (const id of ['mkv-subtab-projects', 'mkv-subtab-projects-area',
                  'mkv-edit-panel', 'mkv-empty-state',
                  'mkv-subtab-scroll-left', 'mkv-subtab-scroll-right']) {
  const el = new FakeEl('div');
  el.id = id;
}

// Lo que vive en otras piezas y no es lo que se mide aquí.
// Los chevrones de scroll de la barra los sirve `core.js` para las TRES
// pestañas desde `_SUBTAB_SCROLLERS`; aquí solo hace falta que existan.
function _installSubtabScrollBindings() {}
function _updateSubtabScrollState() {}
globalThis.__toasts = [];
function showToast(msg, tipo) { globalThis.__toasts.push({ msg, tipo }); }
globalThis.__confirms = [];
function showConfirm(titulo, texto, onOk, etiqueta) {
  globalThis.__confirms.push({ titulo, texto, onOk, etiqueta });
}
function _mkvAplicarPerfilLuminancia() { return false; }
// Hojas del render: lo que se mide es la plantilla del panel, no su contenido.
function _renderMkvDvRadiography() { return ''; }
function hydrateTmdbCard() {}
function _renderMkvTracks() {}
function _renderMkvChapters() {}
function _attachSparklineHover() {}

function analisisFalso(nombre, ruta) {
  return {
    file_name: nombre, file_path: ruta,
    file_size_bytes: 42e9, duration_seconds: 7200,
    tracks: [], chapters: [],
  };
}
function panelHtml(pid) {
  const el = document.getElementById(`mkv-panel-${pid}`);
  return el ? el.innerHTML : null;
}
function idsDelPanel(pid) {
  const h = panelHtml(pid) || '';
  return [...h.matchAll(/\sid="([^"]+)"/g)].map(m => m[1]);
}
function pestanas() {
  return document.getElementById('mkv-subtab-projects').children.map(b => b.id);
}
"""


@unittest.skipUnless(NODE, "node no disponible")
class Tab2EnNode(unittest.TestCase):
    """Base: monta el DOM falso + el código real de Tab 2 y evalúa un guion."""

    #: Funciones reales que hacen falta además de los dos bloques continuos.
    SUELTAS = ("escHtml", "_fmtBytes", "_fmtDuration", "_renderMkvEditPanel",
               "_rgrfDistributionSvg")

    def evaluar(self, guion: str):
        script = "\n".join([
            DOM, BLOQUE_ESTADO, BLOQUE_SUBTABS,
            *(_funcion(n) for n in self.SUELTAS),
            guion,
        ])
        r = subprocess.run([NODE, "-e", script],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1200:]}")
        return json.loads(r.stdout)

    def salida(self, expr: str, preludio: str = "") -> str:
        return preludio + f"\nprocess.stdout.write(JSON.stringify({expr}));"


class TestVariosMkvALaVez(Tab2EnNode):

    ABRE_DOS = """
    openMkvProject(analisisFalso('Dune.mkv', '/mnt/output/Dune.mkv'));
    openMkvProject(analisisFalso('Alien.mkv', '/mnt/output/Alien.mkv'));
    """

    def test_se_pueden_abrir_dos_mkv_a_la_vez(self):
        r = self.evaluar(self.salida(
            "({n: openMkvProjects.length, "
            " nombres: openMkvProjects.map(p => p.fileName), "
            " pestanas: pestanas(), "
            " paneles: [panelHtml('m1') !== null, panelHtml('m2') !== null]})",
            self.ABRE_DOS))
        self.assertEqual(r["n"], 2)
        self.assertEqual(r["nombres"], ["Dune.mkv", "Alien.mkv"])
        self.assertEqual(r["pestanas"], ["mkv-stab-m1", "mkv-stab-m2"])
        self.assertEqual(r["paneles"], [True, True])

    def test_los_paneles_de_los_dos_no_comparten_un_solo_id(self):
        """El fallo silencioso: con un id repetido `getElementById` devuelve el
        primero, así que el segundo panel edita el primero sin dar error."""
        r = self.evaluar(self.salida(
            "({a: idsDelPanel('m1'), b: idsDelPanel('m2')})", self.ABRE_DOS))
        self.assertTrue(r["a"], "el panel del primer MKV salió sin ids")
        comunes = sorted(set(r["a"]) & set(r["b"]))
        self.assertEqual(comunes, [], f"ids compartidos entre paneles: {comunes}")

    def test_los_doce_ids_del_panel_llevan_el_id_del_proyecto(self):
        r = self.evaluar(self.salida(
            "({a: idsDelPanel('m1'), b: idsDelPanel('m2')})", self.ABRE_DOS))
        for base in IDS_DEL_PANEL:
            with self.subTest(id=base):
                self.assertIn(f"{base}-m1", r["a"])
                self.assertIn(f"{base}-m2", r["b"])
                # …y el desnudo no puede seguir ahí.
                self.assertNotIn(base, r["a"])

    def test_el_tope_son_cinco_pestanas(self):
        r = self.evaluar(self.salida(
            "({n: openMkvProjects.length, max: MAX_MKV_PROJECTS, "
            " aviso: __toasts.filter(t => t.tipo === 'warning').length})",
            """
            for (let i = 1; i <= 7; i++) {
              openMkvProject(analisisFalso(`P${i}.mkv`, `/mnt/output/P${i}.mkv`));
            }
            """))
        self.assertEqual(r["max"], 5)
        self.assertEqual(r["n"], 5)
        self.assertEqual(r["aviso"], 2, "los dos rechazados deben avisar")

    def test_reabrir_el_mismo_fichero_refresca_su_pestana_sin_duplicarla(self):
        r = self.evaluar(self.salida(
            "({n: openMkvProjects.length, activo: activeMkvProjectId, "
            " nombre: mkvProject.fileName, "
            " duracion: mkvProject.analysis.duration_seconds})",
            """
            openMkvProject(analisisFalso('Dune.mkv', '/mnt/output/Dune.mkv'));
            openMkvProject(analisisFalso('Alien.mkv', '/mnt/output/Alien.mkv'));
            const otra = analisisFalso('Dune.mkv', '/mnt/output/Dune.mkv');
            otra.duration_seconds = 9999;          // como un re-análisis
            openMkvProject(otra);
            """))
        self.assertEqual(r["n"], 2, "el re-análisis no debe abrir una tercera")
        self.assertEqual(r["activo"], "m1")
        self.assertEqual(r["nombre"], "Dune.mkv")
        self.assertEqual(r["duracion"], 9999, "no se refrescó el análisis")


class TestElGetterMkvProject(Tab2EnNode):
    """`mkvProject` es el compat shim que lee `showRawMkvData` desde tab1.js."""

    def test_devuelve_el_proyecto_activo(self):
        r = self.evaluar(self.salida(
            "({sinNada: antes, trasAbrirDos: mkvProject.fileName, "
            " trasVolver: (switchMkvSubTab('m1'), mkvProject.fileName)})",
            """
            const antes = mkvProject;
            openMkvProject(analisisFalso('Dune.mkv', '/mnt/output/Dune.mkv'));
            openMkvProject(analisisFalso('Alien.mkv', '/mnt/output/Alien.mkv'));
            """))
        self.assertIsNone(r["sinNada"], "sin MKV abierto tiene que ser null")
        self.assertEqual(r["trasAbrirDos"], "Alien.mkv", "el activo es el último")
        self.assertEqual(r["trasVolver"], "Dune.mkv")

    def test_no_tiene_setter_para_que_una_asignacion_no_pase_desapercibida(self):
        """Un setter que no hiciera nada dejaría un segundo estado en la sombra;
        sin setter, el error salta en el acto."""
        r = self.evaluar(self.salida("({lanzo})", """
            let lanzo = false;
            try { mkvProject = {fileName: 'colado'}; } catch (_) { lanzo = true; }
            """))
        self.assertTrue(r["lanzo"])

    def test_switch_activa_el_panel_que_toca_y_apaga_el_otro(self):
        r = self.evaluar(self.salida(
            "({m1: document.getElementById('mkv-panel-m1').style.display, "
            " m2: document.getElementById('mkv-panel-m2').style.display, "
            " activa: document.getElementById('mkv-stab-m1').classList.contains('active')})",
            """
            openMkvProject(analisisFalso('Dune.mkv', '/mnt/output/Dune.mkv'));
            openMkvProject(analisisFalso('Alien.mkv', '/mnt/output/Alien.mkv'));
            switchMkvSubTab('m1');
            """))
        self.assertEqual(r["m1"], "block")
        self.assertEqual(r["m2"], "none")
        self.assertTrue(r["activa"])


class TestCerrarPestanas(Tab2EnNode):

    def test_cerrar_uno_deja_el_otro_intacto(self):
        r = self.evaluar(self.salida(
            "({n: openMkvProjects.length, activo: activeMkvProjectId, "
            " nombre: mkvProject.fileName, "
            " sigueM2: panelHtml('m2') !== null, "
            " idsM2: idsDelPanel('m2').length, "
            " borradoM1: panelHtml('m1') === null, "
            " pestanas: pestanas()})",
            """
            openMkvProject(analisisFalso('Dune.mkv', '/mnt/output/Dune.mkv'));
            openMkvProject(analisisFalso('Alien.mkv', '/mnt/output/Alien.mkv'));
            closeMkvProject('m1');
            """))
        self.assertEqual(r["n"], 1)
        self.assertEqual(r["nombre"], "Alien.mkv")
        self.assertEqual(r["activo"], "m2")
        self.assertTrue(r["sigueM2"])
        self.assertGreater(r["idsM2"], 0, "el panel superviviente se quedó vacío")
        self.assertTrue(r["borradoM1"])
        self.assertEqual(r["pestanas"], ["mkv-stab-m2"])

    def test_cerrar_el_activo_pasa_el_foco_al_que_queda(self):
        r = self.evaluar(self.salida(
            "({activo: activeMkvProjectId, "
            " display: document.getElementById('mkv-panel-m1').style.display})",
            """
            openMkvProject(analisisFalso('Dune.mkv', '/mnt/output/Dune.mkv'));
            openMkvProject(analisisFalso('Alien.mkv', '/mnt/output/Alien.mkv'));
            closeMkvProject('m2');   // el activo
            """))
        self.assertEqual(r["activo"], "m1")
        self.assertEqual(r["display"], "block")

    def test_con_cambios_sin_guardar_avisa_antes_de_cerrar(self):
        r = self.evaluar(self.salida(
            "({pregunto: __confirms.length, tras: sinConfirmar, "
            " titulo: __confirms[0] ? __confirms[0].titulo : null, "
            " finales: cerradas})",
            """
            openMkvProject(analisisFalso('Dune.mkv', '/mnt/output/Dune.mkv'));
            _mkvMarkDirty();
            closeMkvProject('m1');
            const sinConfirmar = openMkvProjects.length;
            __confirms[0].onOk();          // el usuario confirma
            const cerradas = openMkvProjects.length;
            """))
        self.assertEqual(r["pregunto"], 1, "cerrar con cambios debe preguntar")
        self.assertEqual(r["titulo"], "Cambios sin guardar")
        self.assertEqual(r["tras"], 1, "no puede cerrarse antes de confirmar")
        self.assertEqual(r["finales"], 0)

    def test_al_cerrar_el_ultimo_vuelve_el_empty_state(self):
        r = self.evaluar(self.salida(
            "({activo: activeMkvProjectId, obj: mkvProject, "
            " empty: document.getElementById('mkv-empty-state').style.display, "
            " panel: document.getElementById('mkv-edit-panel').style.display, "
            " barra: document.getElementById('mkv-subtab-projects-area').style.display})",
            """
            openMkvProject(analisisFalso('Dune.mkv', '/mnt/output/Dune.mkv'));
            closeMkvEditor();
            """))
        self.assertIsNone(r["activo"])
        self.assertIsNone(r["obj"])
        self.assertEqual(r["empty"], "")
        self.assertEqual(r["panel"], "none")
        self.assertEqual(r["barra"], "none")

    def test_el_punto_de_cambios_sin_guardar_es_por_pestana(self):
        """Con cinco pestañas hay que ver CUÁL tiene cambios."""
        r = self.evaluar(self.salida(
            "({m1: openMkvProjects[0].dirty, m2: openMkvProjects[1].dirty})",
            """
            openMkvProject(analisisFalso('Dune.mkv', '/mnt/output/Dune.mkv'));
            openMkvProject(analisisFalso('Alien.mkv', '/mnt/output/Alien.mkv'));
            switchMkvSubTab('m1');
            _mkvMarkDirty();
            """))
        self.assertTrue(r["m1"])
        self.assertFalse(r["m2"], "el dirty se contagió a la otra pestaña")


class TestLosGradientesDelHistograma(Tab2EnNode):
    """`hist-0`…`hist-6` eran fijos por índice.

    Los otros tres visualizadores (`l5g-`, `l8g-`, `sp-`) ya llevaban sufijo
    aleatorio. Con dos paneles abiertos, el segundo SVG redefine los
    `<linearGradient>` del primero y las barras se pintan con la paleta que no
    es — sin un solo error en consola.
    """

    def test_dos_histogramas_no_definen_los_mismos_ids(self):
        r = self.evaluar(self.salida("({a, b})", """
            const serie = [5, 50, 200, 900, 2500, 8000, 12000];
            const a = [...(_rgrfDistributionSvg(serie).matchAll(
              /<linearGradient id="([^"]+)"/g))].map(m => m[1]);
            const b = [...(_rgrfDistributionSvg(serie).matchAll(
              /<linearGradient id="([^"]+)"/g))].map(m => m[1]);
            """))
        self.assertEqual(len(r["a"]), 7, "deberían ser los 7 tramos del histograma")
        self.assertEqual(sorted(set(r["a"]) & set(r["b"])), [],
                         "dos histogramas comparten ids de gradiente")

    def test_cada_barra_apunta_a_su_propio_gradiente(self):
        r = self.evaluar(self.salida("({defs, usos})", """
            const svg = _rgrfDistributionSvg([5, 50, 200, 900, 2500, 8000, 12000]);
            const defs = [...svg.matchAll(/<linearGradient id="([^"]+)"/g)].map(m => m[1]);
            const usos = [...svg.matchAll(/fill="url\\(#([^)]+)\\)"/g)].map(m => m[1]);
            """))
        self.assertTrue(r["usos"])
        self.assertEqual(sorted(set(r["usos"]) - set(r["defs"])), [],
                         "hay barras apuntando a un gradiente que no se define")


class TestElArmazonYLosModalesSeQuedanGlobales(unittest.TestCase):
    """Los otros 39 ids no se tocan, y hay que poder demostrarlo.

    Prefijar los del armazón rompería `core.js` (que llama a
    `_mkvCheckActiveApply`) y prefijar los de los modales no tendría sentido:
    los trabajos que muestran son singleton en el backend, así que sólo puede
    haber uno a la vez.
    """

    def test_el_html_sigue_declarando_los_ids_del_armazon_y_los_modales(self):
        for id_ in IDS_GLOBALES:
            with self.subTest(id=id_):
                self.assertIn(f'id="{id_}"', HTML)

    def test_el_html_trae_la_barra_de_sub_pestanas_de_tab_2(self):
        for id_ in ("mkv-subtab-projects", "mkv-subtab-projects-area",
                    "mkv-subtab-scroll-left", "mkv-subtab-scroll-right"):
            with self.subTest(id=id_):
                self.assertIn(f'id="{id_}"', HTML)

    def test_la_barra_vive_dentro_de_tab_panel_2(self):
        panel2 = HTML[HTML.index('id="tab-panel-2"'):HTML.index('id="tab-panel-3"')]
        self.assertIn('id="mkv-subtab-projects"', panel2)

    def test_los_chevrones_llaman_al_scroller_local_de_tab_2(self):
        # Tab 1 y Tab 3 usan `_SUBTAB_SCROLLERS` de core.js; Tab 2 no lo toca.
        self.assertIn("scrollMkvSubtabProjects('left')", HTML)
        self.assertIn("scrollMkvSubtabProjects('right')", HTML)
        self.assertIn("function scrollMkvSubtabProjects(", JS)


class TestNoQuedaEstadoDeUnSoloProyecto(unittest.TestCase):
    """Guard de forma, para lo que no se puede comprobar ejecutando.

    El comportamiento lo cubren las clases de arriba; esto vigila que no
    reaparezca una variable global de las que sólo valen para un MKV.
    """

    def test_nadie_asigna_mkvProject(self):
        """Es un getter sin setter: una asignación lanzaría en producción."""
        malas = [l for l in JS.splitlines()
                 if re.search(r"(?<![.\w])mkvProject\s*=(?!=)", l)
                 and "Object.defineProperty" not in l
                 and not l.strip().startswith(("//", "*", "/*"))]
        self.assertEqual(malas, [], "asignación a `mkvProject`: es un getter; "
                                    "usa `openMkvProjects` / `switchMkvSubTab`")

    def test_el_comparador_ab_ya_no_es_una_global(self):
        """`_mkvComparacion` era única: con dos pestañas, la curva de
        referencia de un MKV se pintaba sobre el de al lado."""
        self.assertNotIn("let _mkvComparacion", JS)
        self.assertIn("project.comparacion", JS)


if __name__ == "__main__":
    unittest.main()
