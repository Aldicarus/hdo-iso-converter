"""La columna izquierda de Tab 2: los MKVs analizados.

Tab 1 y Tab 3 tienen columna izquierda desde siempre —primario arriba,
cabecera con contador, búsqueda, ordenación, filtros y tarjetas— y `Tab 2 la
tenía vacía`: un `<div id="sidebar-tab-2">` sin nada dentro y un `switchTab`
que escondía el `#sidebar` entero para que la pestaña ocupara todo el ancho.
Ahora lista lo que sí sobrevive a un refresco: los MKVs que ya se han
analizado, que salen de la caché de `/config/mkv_audits/`.

Lo que estos tests protegen, y por qué cada cosa:

* **El armazón es el MISMO.** El objetivo del bloque es que no se note el
  salto entre pestañas, y eso se consigue reutilizando las clases de Tab 1 y
  Tab 3, no escribiendo unas parecidas. Si alguien inventa `.mkv-sidebar-card`,
  se ve aquí.
* **Ninguna tarjeta se queda sin pill que la alcance.** La caché caducada no
  tiene análisis vigente de ninguna clase; si el pill 📋 exigiera
  `tiene_basico`, esas tarjetas solo saldrían con «Todos» y el usuario que
  filtra las daría por desaparecidas.
* **Un fallo de red no vacía la lista.** Machacarla con `[]` la deja en «0»,
  que es indistinguible de «no has analizado nada» — una conclusión mucho peor
  que un dato viejo.
* **Los pills se re-marcan desde el estado en cada render.** El manejador de
  Tab 1 quita `.active` a TODOS los `.sb-filter-pill` del documento (los suyos
  y los de las otras dos columnas), así que sin esto tocar un filtro allí deja
  los de aquí apagados aunque el filtro siga puesto.
* **Abrir no siempre significa analizar.** Si el MKV ya está en una
  sub-pestaña se cambia a ella, y si el fichero no está no se llama al backend
  (respondería un 404 seco).

Las funciones se evalúan en **node** sobre un DOM mínimo escrito aquí, con el
mismo patrón que `test_tab2_subpestanas`. Las fuentes salen de
`frontend_sources`, nunca de una ruta a `tab2.js`.

Ejecutar desde la raíz del repo:
    .venv/bin/python3 -m unittest app.tests.test_tab2_columna_izquierda -v
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import html, js_completo, pieza_de  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()
HTML = html()


def _funcion(nombre: str) -> str:
    """El fuente de una función top-level, tal cual lo carga el navegador."""
    for marca in (f"\nfunction {nombre}(", f"\nasync function {nombre}("):
        i = JS.find(marca)
        if i != -1:
            return JS[i + 1:JS.index("\n}\n", i + 1) + 3]
    raise AssertionError(f"no se encuentra `{nombre}` en ninguna pieza")


def _bloque(desde: str, hasta: str) -> str:
    i = JS.index(desde)
    return JS[i:JS.index(hasta, i)]


def _constante(marca: str) -> str:
    """Una constante top-level, hasta su cierre. Una línea o un objeto."""
    i = JS.index(marca)
    fin = (JS.index("\n", i) + 1 if marca.rstrip().endswith("=")
           else JS.index("\n};\n", i) + 4)
    return JS[i:fin]


# Las variables de módulo de la columna: un tramo continuo del fichero.
ESTADO = _bloque("let _mkvRecientes = [];", "/** Pide la lista y repinta")

FUNCIONES = (
    "refrescarMkvRecientes", "_renderMkvRecientesErrorDeCarga",
    "filtrarMkvRecientes", "onMkvRecientesSortChange",
    "toggleMkvRecientesSortDir", "_actualizarBotonOrdenMkvRecientes",
    "onMkvRecientesFilterClick", "_mkvRecienteEstado", "_renderMkvRecientes",
    "_mkvToggleSeleccionReciente", "abrirMkvReciente",
    # De otras piezas, pero reales: son las que dan el formato de la tarjeta.
    "escHtml", "normalizeSearch", "formatRelativeDate", "_fmtBytes",
    "_fmtDuration",
    # La tarjeta común de las tres columnas y los iconos que pinta. Van las
    # de verdad y no un doble: el formato de la tarjeta ES lo que este
    # fichero comprueba, y con un `() => '<i></i>'` comprobaría el doble.
    "tarjetaDeProyecto", "nombreYTags", "miniaturaDe",
    "_projChipsHTML", "_projPipsHTML",
    "_svg", "_chipIcono", "iconoDeTrabajo", "iconoDeEstado",
)

# Las constantes que esas funciones leen. No son `function`, así que el
# extractor de arriba no las ve.
CONSTANTES = (
    "const _PROJ_CHIP_LARGO = ",
    "const _TONO_POR_TAB = ",
    "const _GLIFOS_TRABAJO = {",
    "const _ICONOS_ESTADO = {",
)

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
    this.ondblclick = null;
    this.disabled = false;
    this.value = '';
    this.textContent = '';
    this._id = '';
    this._html = '';
    this._clases = new Set();
    // Los hijos de una plantilla de `innerHTML` no se construyen (esto no es
    // un parser de HTML), pero el código sí pide `.querySelector('.fila')`
    // para colgarle los handlers. Se devuelve un doble memoizado por
    // selector, así que el test puede dispararlos.
    this._stubs = new Map();
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
  set innerHTML(v) { this._html = String(v); this.children = []; }
  appendChild(hijo) { hijo.parentNode = this; this.children.push(hijo); return hijo; }
  querySelector(sel) {
    // Devuelve null si el selector no está en la plantilla: sin eso, un
    // `if (boton)` pasaría siempre y el test no vería la diferencia entre la
    // tarjeta que ofrece "Abrir" y la que no.
    const marca = sel.startsWith('[') ? sel.slice(1, -1) : sel.slice(1);
    if (!this._html.includes(marca)) return null;
    if (!this._stubs.has(sel)) this._stubs.set(sel, new FakeEl('div'));
    return this._stubs.get(sel);
  }
  addEventListener() {}
}

globalThis.document = {
  getElementById: id => _porId.get(id) || null,
  createElement: t => new FakeEl(t),
  querySelectorAll(sel) {
    // Los dos selectores que usa este código: "#host .clase".
    const m = sel.match(/^#([\w-]+)\s+\.([\w-]+)$/);
    if (!m) return [];
    const host = _porId.get(m[1]);
    if (!host) return [];
    const fuera = [];
    const rec = el => { for (const h of el.children) { fuera.push(h); rec(h); } };
    rec(host);
    return fuera.filter(e => e.classList.contains(m[2]));
  },
};

// El armazón de #sidebar-tab-2 tal como lo declara index.html.
for (const id of ['mkv-recientes-list', 'mkv-recientes-count',
                  'mkv-recientes-search', 'mkv-recientes-sort',
                  'mkv-recientes-sort-dir', 'sidebar-tab-2']) {
  const el = new FakeEl(id === 'mkv-recientes-search' ? 'input' : 'div');
  el.id = id;
}
for (const f of ['all', 'extendido', 'basico', 'missing']) {
  const pill = new FakeEl('button');
  pill.className = 'sb-filter-pill' + (f === 'all' ? ' active' : '');
  pill.dataset.filter = f;
  document.getElementById('sidebar-tab-2').appendChild(pill);
}

// Lo que vive en otras piezas y no es lo que se mide aquí.
globalThis.__toasts = [];
function showToast(msg, tipo) { globalThis.__toasts.push({ msg, tipo }); }
globalThis.__respuesta = null;            // lo que devuelve el endpoint
globalThis.__peticiones = [];
async function apiFetch(url) { __peticiones.push(url); return globalThis.__respuesta; }
const MAX_MKV_PROJECTS = 5;
const openMkvProjects = [];
function _mkvRutaDe(p) { return p.filePath; }
globalThis.__aperturas = [];
function _mkvAbrirRuta(ruta, nombre) { __aperturas.push({ ruta, nombre }); }
globalThis.__cambiosDePestana = [];
function switchMkvSubTab(pid) { __cambiosDePestana.push(pid); }

// ── utilidades del guion de test ────────────────────────────────────
function entrada(nombre, extra) {
  return Object.assign({
    ruta: `/mnt/library/${nombre}`,
    nombre,
    tamano_bytes: 42e9,
    duracion_segundos: 7200,
    analizado_en: '2026-09-01T10:00:00+00:00',
    existe: true,
    tiene_basico: true,
    tiene_extendido: false,
    tiene_luminancia: false,
  }, extra || {});
}
function cargar(lista, total) {
  _mkvRecientes = lista;
  _mkvRecientesTotal = total === undefined ? lista.length : total;
  _renderMkvRecientes();
}
function tarjetas() {
  return document.getElementById('mkv-recientes-list').children
    .filter(c => c.classList.contains('session-card'));
}
function titulos() {
  return tarjetas().map(c => (c.innerHTML.match(/session-card-title"[^>]*>([^<]*)</) || [])[1]);
}
function tonos() {
  // El de ESTADO, no el del tipo: en la tarjeta hay dos iconos y el primero
  // es el de la miniatura, que lleva el color de la pestaña y es siempre el
  // mismo. El que cambia de fila a fila es el de la derecha.
  return tarjetas().map(c =>
    (c.innerHTML.match(/proj-estado[\s\S]*?icono-chip icono-(\w+)/) || [])[1]);
}
function acentos() {
  return tarjetas().map(c =>
    (String(c.className).split(/\s+/).find(x => x.startsWith('estado-')) || ''));
}
function chips() {
  return tarjetas().map(c =>
    [...c.innerHTML.matchAll(/proj-chip[^>]*>([^<]*)</g)].map(m => m[1]));
}
function contador() { return document.getElementById('mkv-recientes-count').textContent; }
function buscar(txt) {
  document.getElementById('mkv-recientes-search').value = txt;
  _renderMkvRecientes();
}
function filtrar(clave) {
  const pill = document.querySelectorAll('#sidebar-tab-2 .sb-filter-pill')
    .find(p => p.dataset.filter === clave);
  onMkvRecientesFilterClick(pill);
}
function pillsActivos() {
  return document.querySelectorAll('#sidebar-tab-2 .sb-filter-pill')
    .filter(p => p.classList.contains('active')).map(p => p.dataset.filter);
}
function ordenarPor(clave) {
  document.getElementById('mkv-recientes-sort').value = clave;
  onMkvRecientesSortChange();
}
"""


@unittest.skipUnless(NODE, "node no disponible")
class ColumnaEnNode(unittest.TestCase):
    """Base: monta el DOM falso + el código real de la columna."""

    def evaluar(self, guion: str):
        # El guion va dentro de una función async: `refrescarMkvRecientes` lo
        # es, y `node -e` no admite await en el nivel superior.
        envuelto = ("(async () => {\n" + guion
                    + "\n})().catch(e => { console.error(e); process.exit(1); });")
        script = "\n".join([DOM, ESTADO,
                             *(_constante(c) for c in CONSTANTES),
                             *(_funcion(n) for n in FUNCIONES), envuelto])
        r = subprocess.run([NODE, "-e", script],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        return json.loads(r.stdout)

    def salida(self, expr: str, preludio: str = "") -> str:
        return preludio + f"\nprocess.stdout.write(JSON.stringify({expr}));"


class TestLaListaSePinta(ColumnaEnNode):

    def test_una_tarjeta_por_mkv_y_el_contador_cuadra(self):
        r = self.evaluar(self.salida("({n: tarjetas().length, c: contador()})", """
            cargar([entrada('Dune.mkv'), entrada('Alien.mkv')]);
            """))
        self.assertEqual(r["n"], 2)
        self.assertEqual(r["c"], 2)

    def test_sin_nada_analizado_hay_estado_vacío_y_no_tarjetas(self):
        r = self.evaluar(self.salida(
            "({n: tarjetas().length, html: document.getElementById('mkv-recientes-list').innerHTML})",
            "cargar([]);"))
        self.assertEqual(r["n"], 0)
        self.assertIn("Sin MKVs analizados", r["html"])

    def test_la_tarjeta_dice_tamaño_duración_y_cuándo_se_analizó(self):
        r = self.evaluar(self.salida("tarjetas()[0].innerHTML", """
            cargar([entrada('Dune.mkv', {tamano_bytes: 79e9, duracion_segundos: 9155})]);
            """))
        self.assertIn("79.0 GB", r)
        self.assertIn("2h 32min", r)
        self.assertIn('data-iso="2026-09-01T10:00:00+00:00"', r)

    _CUATRO_ESTADOS = """
            cargar([
              entrada('Extendido.mkv', {tiene_extendido: true}),
              entrada('Basico.mkv'),
              entrada('Caducado.mkv', {tiene_basico: false}),
              entrada('Movido.mkv', {existe: false}),
            ]);
            ordenarPor('name');
            """

    def test_el_chip_de_estado_resume_qué_análisis_tiene(self):
        """Básico · caducado · extendido · movido, en ese orden alfabético."""
        r = self.evaluar(self.salida("tonos()", self._CUATRO_ESTADOS))
        self.assertEqual(r, ["gris", "gris", "verde", "naranja"])

    def test_y_el_acento_lateral_dice_lo_mismo_que_el_chip(self):
        """Las dos señales tienen que moverse juntas: si el chip dice una cosa
        y el color del borde otra, la fila se lee mal de un vistazo."""
        r = self.evaluar(self.salida("acentos()", self._CUATRO_ESTADOS))
        self.assertEqual(r, ["", "estado-aviso", "estado-hecho", "estado-aviso"])

    def test_los_análisis_hechos_van_en_etiquetas_y_los_que_no_apagados(self):
        """La etiqueta está siempre: así la posición de cada dato no se mueve
        de una fila a otra."""
        r = self.evaluar(self.salida(
            "tarjetas()[0].innerHTML",
            "cargar([entrada('Dune.mkv', {tiene_extendido: true})]);"))
        self.assertIn(">RPU<", r)
        self.assertIn(">Luz<", r)
        self.assertIn("apagado", r)          # Luz no la tiene

    def test_los_tags_del_nombre_salen_del_título_y_pasan_a_etiquetas(self):
        """Van al final del nombre, o sea que eran lo primero que se comía el
        recorte por la derecha — y son lo que distingue una versión de otra."""
        r = self.evaluar(self.salida(
            "({t: titulos()[0], c: chips()[0]})",
            "cargar([entrada('Dune (2021) [Audio DCP] [CMv4 FULL].mkv')]);"))
        self.assertEqual(r["t"], "Dune (2021)")
        self.assertIn("Audio DCP", r["c"])
        self.assertIn("CMv4 FULL", r["c"])

    def test_la_carátula_se_pide_al_ancho_de_la_miniatura(self):
        """La ficha guarda el póster de 342 px y en la columna se ven 36: sin
        reescribir el ancho son cientos de imágenes de un tamaño que no se ve."""
        r = self.evaluar(self.salida(
            "tarjetas()[0].innerHTML",
            "cargar([entrada('Dune.mkv', "
            "{poster: 'https://image.tmdb.org/t/p/w342/abc.jpg'})]);"))
        self.assertIn("/t/p/w92/abc.jpg", r)
        self.assertIn('loading="lazy"', r)

    def test_el_mkv_ya_abierto_lleva_su_distintivo(self):
        r = self.evaluar(self.salida("tarjetas().map(c => c.innerHTML.includes('abierto'))", """
            openMkvProjects.push({id: 'm1', filePath: '/mnt/library/Dune.mkv'});
            cargar([entrada('Dune.mkv'), entrada('Alien.mkv')]);
            """))
        self.assertEqual(r, [True, False])

    def test_el_recorte_del_servidor_se_dice_en_vez_de_disimularse(self):
        r = self.evaluar(self.salida(
            "document.getElementById('mkv-recientes-list').children.map(c => c.textContent)",
            "cargar([entrada('A.mkv'), entrada('B.mkv')], 240);"))
        self.assertIn("Los 2 más recientes de 240", r)


class TestElMkvQueYaNoEstá(ColumnaEnNode):

    def test_la_tarjeta_sale_apagada_y_sin_botón_de_abrir(self):
        r = self.evaluar(self.salida(
            "({clases: tarjetas()[0].className, html: tarjetas()[0].innerHTML, "
            "  boton: tarjetas()[0].querySelector('[data-abrir]')})",
            "cargar([entrada('Movido.mkv', {existe: false})]);"))
        self.assertIn("no-encontrado", r["clases"])
        self.assertIn("disabled", r["html"])
        self.assertIsNone(r["boton"], "no puede ofrecer un botón que va a dar 404")

    def test_pulsarla_avisa_y_no_llama_al_backend(self):
        r = self.evaluar(self.salida(
            "({aperturas: __aperturas, avisos: __toasts})", """
            cargar([entrada('Movido.mkv', {existe: false})]);
            abrirMkvReciente('/mnt/library/Movido.mkv');
            """))
        self.assertEqual(r["aperturas"], [], "no se puede analizar lo que no está")
        self.assertEqual(len(r["avisos"]), 1)
        self.assertEqual(r["avisos"][0]["tipo"], "warning")


class TestLosFiltrosYLaBúsqueda(ColumnaEnNode):

    CUATRO = """
    cargar([
      entrada('Extendido.mkv', {tiene_extendido: true}),
      entrada('Basico.mkv'),
      entrada('Caducado.mkv', {tiene_basico: false}),
      entrada('Movido.mkv', {existe: false}),
    ]);
    """

    def test_cada_pill_deja_lo_suyo(self):
        r = self.evaluar(self.salida(
            "({ext: (filtrar('extendido'), titulos()), "
            "  bas: (filtrar('basico'), titulos()), "
            "  falta: (filtrar('missing'), titulos()), "
            "  todos: (filtrar('all'), titulos().length)})", self.CUATRO))
        self.assertEqual(r["ext"], ["Extendido"])
        self.assertEqual(sorted(r["falta"]), ["Movido"])
        self.assertEqual(r["todos"], 4)

    def test_la_caché_caducada_es_alcanzable_desde_el_pill_de_sin_extendido(self):
        """Si 📋 exigiera `tiene_basico`, las caducadas solo saldrían con
        «Todos» y quien filtra las daría por desaparecidas."""
        r = self.evaluar(self.salida("titulos()", self.CUATRO + "filtrar('basico');"))
        self.assertEqual(sorted(r), ["Basico", "Caducado"])

    def test_ningún_mkv_se_queda_fuera_de_todos_los_pills(self):
        r = self.evaluar(self.salida(
            "['extendido','basico','missing'].map(f => (filtrar(f), titulos())).flat()",
            self.CUATRO))
        self.assertEqual(sorted(r), ["Basico", "Caducado", "Extendido", "Movido"])

    def test_la_búsqueda_ignora_tildes_y_mayúsculas(self):
        r = self.evaluar(self.salida("({n: titulos(), c: contador()})", """
            cargar([entrada('El Rey León (2019).mkv'), entrada('Dune.mkv')]);
            buscar('rey leon');
            """))
        self.assertEqual(r["n"], ["El Rey León (2019)"])
        self.assertEqual(r["c"], "1 / 2", "filtrando, el contador dice de cuántos")

    def test_sin_resultados_lo_dice_en_vez_de_quedarse_en_blanco(self):
        r = self.evaluar(self.salida(
            "document.getElementById('mkv-recientes-list').innerHTML", """
            cargar([entrada('Dune.mkv')]);
            buscar('zzzz');
            """))
        self.assertIn("Sin resultados", r)

    def test_los_pills_se_remarcan_desde_el_estado_en_cada_render(self):
        """`onSidebarFilterClick` de Tab 1 hace `querySelectorAll('.sb-filter-pill')`
        SIN acotar a su columna, así que apaga también los de aquí. Repintar
        desde el estado es lo que hace que volver a la pestaña los recupere."""
        r = self.evaluar(self.salida("({tras: pillsActivos()})", """
            cargar([entrada('Dune.mkv')]);
            filtrar('extendido');
            // como si el usuario hubiera tocado un filtro de Tab 1:
            document.querySelectorAll('#sidebar-tab-2 .sb-filter-pill')
              .forEach(p => p.classList.remove('active'));
            _renderMkvRecientes();
            """))
        self.assertEqual(r["tras"], ["extendido"])


class TestLaOrdenación(ColumnaEnNode):

    TRES = """
    cargar([
      entrada('Bruta.mkv',   {tamano_bytes: 90e9, analizado_en: '2026-01-01T00:00:00+00:00'}),
      entrada('Antigua.mkv', {tamano_bytes: 10e9, analizado_en: '2026-05-05T00:00:00+00:00'}),
      entrada('Nueva.mkv',   {tamano_bytes: 50e9, analizado_en: '2026-09-08T00:00:00+00:00'}),
    ]);
    """

    def test_por_defecto_lo_último_analizado_va_primero(self):
        r = self.evaluar(self.salida("titulos()", self.TRES))
        self.assertEqual(r, ["Nueva", "Antigua", "Bruta"])

    def test_por_nombre_arranca_de_la_a_a_la_z(self):
        """La fecha y el tamaño se leen de mayor a menor; el nombre, al revés.
        Es lo mismo que hace el sidebar de Tab 1."""
        r = self.evaluar(self.salida(
            "({orden: titulos(), flecha: document.getElementById('mkv-recientes-sort-dir').textContent})",
            self.TRES + "ordenarPor('name');"))
        self.assertEqual(r["orden"], ["Antigua", "Bruta", "Nueva"])
        self.assertEqual(r["flecha"], "↑")

    def test_por_tamaño_de_mayor_a_menor(self):
        r = self.evaluar(self.salida("titulos()", self.TRES + "ordenarPor('size');"))
        self.assertEqual(r, ["Bruta", "Nueva", "Antigua"])

    def test_el_botón_de_dirección_invierte_de_verdad(self):
        r = self.evaluar(self.salida(
            "({antes, despues: titulos(), flecha: document.getElementById('mkv-recientes-sort-dir').textContent})",
            self.TRES + """
            const antes = titulos();
            toggleMkvRecientesSortDir();
            """))
        self.assertEqual(r["despues"], list(reversed(r["antes"])))
        self.assertEqual(r["flecha"], "↑")


class TestAbrirDesdeLaColumna(ColumnaEnNode):

    def test_un_mkv_nuevo_pasa_por_el_flujo_de_siempre(self):
        r = self.evaluar(self.salida("__aperturas", """
            cargar([entrada('Dune.mkv')]);
            abrirMkvReciente('/mnt/library/Dune.mkv');
            """))
        self.assertEqual(r, [{"ruta": "/mnt/library/Dune.mkv", "nombre": "Dune.mkv"}])

    def test_uno_ya_abierto_solo_cambia_de_sub_pestaña(self):
        """Re-analizarlo daría lo mismo (cache hit) pero abriendo el modal
        de análisis para nada."""
        r = self.evaluar(self.salida(
            "({aperturas: __aperturas.length, pestanas: __cambiosDePestana})", """
            openMkvProjects.push({id: 'm3', filePath: '/mnt/library/Dune.mkv'});
            cargar([entrada('Dune.mkv')]);
            abrirMkvReciente('/mnt/library/Dune.mkv');
            """))
        self.assertEqual(r["aperturas"], 0)
        self.assertEqual(r["pestanas"], ["m3"])

    def test_con_cinco_abiertos_avisa_antes_de_gastar_el_análisis(self):
        r = self.evaluar(self.salida(
            "({aperturas: __aperturas.length, avisos: __toasts.map(t => t.tipo)})", """
            for (let i = 0; i < 5; i++) openMkvProjects.push({id: 'm' + i, filePath: '/x' + i});
            cargar([entrada('Dune.mkv')]);
            abrirMkvReciente('/mnt/library/Dune.mkv');
            """))
        self.assertEqual(r["aperturas"], 0)
        self.assertEqual(r["avisos"], ["warning"])

    def test_el_botón_de_la_tarjeta_abre(self):
        r = self.evaluar(self.salida("__aperturas", """
            cargar([entrada('Dune.mkv')]);
            tarjetas()[0].querySelector('[data-abrir]')
              .onclick({stopPropagation() {}});
            """))
        self.assertEqual(r, [{"ruta": "/mnt/library/Dune.mkv", "nombre": "Dune.mkv"}])

    def test_un_apóstrofo_en_el_nombre_no_rompe_el_botón(self):
        """El handler se cuelga desde JS y no como `onclick="…('${ruta}')"`:
        `escHtml` no escapa la comilla simple, así que «Ocean's Eleven» cerraría
        la cadena del atributo y el botón se quedaría mudo — sin un error."""
        r = self.evaluar(self.salida("__aperturas", """
            cargar([entrada("Ocean's Eleven (2001).mkv")]);
            tarjetas()[0].querySelector('[data-abrir]')
              .onclick({stopPropagation() {}});
            """))
        self.assertEqual(r, [{"ruta": "/mnt/library/Ocean's Eleven (2001).mkv",
                              "nombre": "Ocean's Eleven (2001).mkv"}])

    def test_doble_clic_en_la_fila_abre(self):
        r = self.evaluar(self.salida("__aperturas.length", """
            cargar([entrada('Dune.mkv')]);
            tarjetas()[0].querySelector('.session-card-row').ondblclick();
            """))
        self.assertEqual(r, 1)

    def test_un_clic_solo_despliega_las_acciones(self):
        r = self.evaluar(self.salida(
            "({aperturas: __aperturas.length, sel: tarjetas()[0].classList.contains('selected')})",
            """
            cargar([entrada('Dune.mkv')]);
            tarjetas()[0].querySelector('.session-card-row').onclick();
            """))
        self.assertEqual(r["aperturas"], 0)
        self.assertTrue(r["sel"])


class TestElFalloDeRed(ColumnaEnNode):

    def test_no_vacía_la_lista_que_ya_había(self):
        r = self.evaluar(self.salida(
            "({n: tarjetas().length, c: contador()})", """
            globalThis.__respuesta = {recientes: [entrada('Dune.mkv')], total: 1};
            await refrescarMkvRecientes();
            globalThis.__respuesta = null;          // el fetch falla
            await refrescarMkvRecientes();
            """))
        self.assertEqual(r["n"], 1, "un timeout no puede borrar la lista: «0 MKVs» "
                                    "es indistinguible de «no has analizado nada»")

    def test_sin_nada_cacheado_ofrece_reintentar(self):
        r = self.evaluar(self.salida(
            "({html: document.getElementById('mkv-recientes-list').innerHTML, c: contador()})",
            """
            globalThis.__respuesta = null;
            await refrescarMkvRecientes();
            """))
        self.assertIn("refrescarMkvRecientes()", r["html"], "hace falta el botón")
        self.assertEqual(r["c"], "—")


class TestElArmazonEsElMismoQueEnLasOtrasDos(unittest.TestCase):
    """Sin node: lo que se mira es el HTML, que es donde vive el armazón."""

    def _sidebar2(self) -> str:
        i = HTML.index('id="sidebar-tab-2"')
        return HTML[i:HTML.index('id="sidebar-tab-3"', i)]

    def test_ya_no_está_vacío(self):
        self.assertNotIn('<div id="sidebar-tab-2" style="display:none"></div>', HTML)

    def test_trae_las_mismas_clases_que_tab_1_y_tab_3(self):
        s2 = self._sidebar2()
        for clase in ("sidebar-new-project-area", "sidebar-new-project-btn",
                      "sidebar-sessions-header", "sidebar-section-title",
                      "sessions-count", "sidebar-search-wrap",
                      "sidebar-search-input", "sidebar-controls",
                      "sidebar-sort-row", "sidebar-sort-select",
                      "sidebar-sort-dir-btn", "sidebar-status-filters",
                      "sb-filter-pill"):
            with self.subTest(clase=clase):
                self.assertIn(clase, s2)

    def test_el_botón_primario_se_fue_del_centro_a_la_columna(self):
        s2 = self._sidebar2()
        self.assertIn("openMkvPickerModal()", s2)
        barra = HTML[HTML.index('id="mkv-action-bar"'):HTML.index('id="mkv-empty-state"')]
        self.assertNotIn("sidebar-new-project-btn", barra,
                         "el primario de Tab 2 ya no vive en la franja del centro")

    def test_los_cuatro_pills_de_filtro_existen(self):
        s2 = self._sidebar2()
        for f in ("all", "extendido", "basico", "missing"):
            with self.subTest(filtro=f):
                self.assertIn(f'data-filter="{f}"', s2)

    def test_los_tres_criterios_de_ordenación_existen(self):
        s2 = self._sidebar2()
        for v in ("analizado", "name", "size"):
            with self.subTest(criterio=v):
                self.assertIn(f'value="{v}"', s2)

    def test_switchTab_ya_no_esconde_la_columna_en_tab_2(self):
        """Era `sidebar.style.display = (n === 2) ? 'none' : ''`. Con eso puesto
        la columna nueva no se ve, y no hay ningún error que lo delate."""
        _, core = pieza_de("switchTab")
        i = core.index("function switchTab(")
        cuerpo = core[i:core.index("\n}\n", i)]
        self.assertNotIn("style.display = (n === 2)", cuerpo)
        self.assertNotIn("getElementById('sidebar')", cuerpo)

    def test_al_entrar_en_tab_2_se_pide_la_lista(self):
        _, core = pieza_de("switchTab")
        self.assertIn("refrescarMkvRecientes()", core)


if __name__ == "__main__":
    unittest.main()
