"""Un armazón de detalle para los cinco tipos de trabajo.

Antes solo una fase CMv4.0 tenía vista de detalle, y su overlay se abría SOLO
y tapaba el panel entero — de esa familia era el bug de agosto en que el banner
de ACK se veía pero no se podía pulsar. El resto de trabajos no tenía nada
equivalente: un rip se miraba en una sub-pestaña del centro que las otras dos
pestañas no tienen, y la copia desde biblioteca solo enseñaba una barra.

Lo que este fichero fija:

- **El armazón no sabe de ningún tipo.** Cada pestaña registra el suyo, así
  que añadir un tipo de trabajo no obliga a tocar `workbar.js`. Es el mismo
  patrón que los adaptadores del backend.
- **El detalle no es siempre un log.** La copia y la creación de una serie no
  producen uno; ahí el detalle son bytes y episodios. Forzar un log vacío
  habría sido mentir sobre lo que hay.
- **La barra sigue la misma regla que la columna**: sin porcentaje medido no se
  pinta una que avanza.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_modal_de_trabajo -v
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


def _fn(nombre: str) -> str:
    i = JS.index(f"function {nombre}(")
    return JS[JS.rindex("\n", 0, i) + 1:JS.index("\n}\n", i) + 3]


def _bloque(marca: str) -> str:
    """Un objeto literal de nivel superior, tal cual."""
    i = JS.index(marca)
    return JS[i:JS.index("\n};\n", i) + 4]


def _iconos() -> str:
    """Lo que hace falta para que el marcado de los iconos se pueda evaluar."""
    return "\n".join([_bloque("const _ICONOS_TRABAJO = {"),
                      _bloque("const _ICONOS_ESTADO = {"),
                      _fn("_svg"), _fn("_chipIcono"),
                      _fn("iconoDeTrabajo"), _fn("iconoDeEstado")])


def _node(guion: str) -> dict:
    r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(f"node falló:\n{r.stderr[:900]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


ACTIVO = {
    "id": "p1", "tab": "cmv40", "tipo": "fase_cmv40", "que": "Fase C de Predator",
    "fase": "extract", "fase_label": "Extrayendo BL/EL", "fase_n": 3,
    "fases_total": 7, "pct": 40, "pct_medido": True, "segundos": 300,
    "eta_s": 450, "eta_fuente": "medido", "cancelable": True, "detalle": "cmv40",
}


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElArmazonPinta(unittest.TestCase):

    def _pintar(self, activo, vista) -> dict:
        guion = f"""
const _els = {{}};
for (const id of ['trabajo-modal-icono','trabajo-modal-titulo','trabajo-modal-sub',
                  'trabajo-modal-timeline','trabajo-modal-barra-wrap','trabajo-modal-barra',
                  'trabajo-modal-tiempos','trabajo-modal-cuerpo','trabajo-modal-copiar',
                  'trabajo-modal-cancelar','trabajo-modal-paso','trabajo-modal-pct',
                  'trabajo-modal-eta','trabajo-modal-cartel','trabajo-modal-cartel-poster',
                  'trabajo-modal-cartel-titulo','trabajo-modal-cartel-meta']) {{
  _els[id] = {{ textContent: '', innerHTML: '', style: {{}}, dataset: {{}},
    // El armazón consulta el log para decidir si baja el scroll y busca la
    // caja para plegar el lateral: sin estos dos, el DOM falso revienta.
    querySelector: () => null, closest: () => null, classList: {{
    _v: new Set(),
    toggle(c, on) {{ on ? this._v.add(c) : this._v.delete(c); }},
    has(c) {{ return this._v.has(c); }} }} }};
}}
globalThis.document = {{ getElementById: id => _els[id] || null,
                         querySelector: () => null }};
globalThis.escHtml = t => String(t);
{_iconos()}
{_fn('_workbarTiempo')}
{_fn('timelineDeTrabajo')}
{_fn('_trabajoCartelPinta')}
{_fn('_trabajoModalPinta')}
_trabajoModalPinta({json.dumps(activo)}, {json.dumps(vista)});
console.log(JSON.stringify({{
  icono: _els['trabajo-modal-icono'].innerHTML,
  titulo: _els['trabajo-modal-titulo'].textContent,
  sub: _els['trabajo-modal-sub'].textContent,
  pasos: _els['trabajo-modal-timeline'].innerHTML,
  tiempos: _els['trabajo-modal-tiempos'].textContent,
  paso: _els['trabajo-modal-paso'].textContent,
  pct: _els['trabajo-modal-pct'].textContent,
  eta: _els['trabajo-modal-eta'].textContent,
  cartelVisible: _els['trabajo-modal-cartel'].style.display !== 'none',
  cartelTitulo: _els['trabajo-modal-cartel-titulo'].textContent,
  cartelMeta: _els['trabajo-modal-cartel-meta'].textContent,
  cartelPoster: _els['trabajo-modal-cartel-poster'].innerHTML,
  cuerpo: _els['trabajo-modal-cuerpo'].innerHTML,
  indeterminada: _els['trabajo-modal-barra-wrap'].classList.has('indeterminada'),
  anchoBarra: _els['trabajo-modal-barra'].style.width,
  copiar: _els['trabajo-modal-copiar'].style.display,
  cancelar: _els['trabajo-modal-cancelar'].style.display,
}}));
"""
        return _node(guion)

    def test_la_cabecera_dice_la_FASE_no_el_nombre_del_fichero(self):
        """El nombre del MKV lo enseña la cartela de la columna. Ponerlo
        también aquí dejaba tres líneas con el mismo título."""
        r = self._pintar(ACTIVO, {"titulo": "Predator.mkv",
                                  "sub": "salida.mkv", "pasos": [], "cuerpo": ""})
        self.assertEqual(r["titulo"], ACTIVO["fase_label"])
        self.assertEqual(r["sub"], "salida.mkv")

    def test_sin_fase_cae_en_el_titulo_de_la_vista(self):
        """Un trabajo sin fases (la copia) sigue necesitando cabecera."""
        r = self._pintar(dict(ACTIVO, fase_label=""),
                         {"titulo": "Copia a Output", "pasos": []})
        self.assertEqual(r["titulo"], "Copia a Output")

    def test_el_icono_lo_deriva_del_TIPO_no_de_la_vista(self):
        """Si cada vista trajera el suyo, la columna y el modal podrían acabar
        enseñando iconos distintos para el mismo trabajo."""
        r = self._pintar(ACTIVO, {"titulo": "X", "pasos": []})
        self.assertIn("icono-naranja", r["icono"], "fase_cmv40 va en naranja")
        self.assertIn("icono-chip-lg", r["icono"])

    def test_las_fases_van_a_la_COLUMNA_no_a_una_tira(self):
        """Los cinco tipos las enseñan en el mismo sitio. Antes la fase CMv4.0
        tenía su timeline a la izquierda y los otros cuatro una tira horizontal
        sobre la barra: dos trabajos de la misma aplicación se miraban en
        sitios distintos."""
        r = self._pintar(ACTIVO, {"pasos": ["A", "B", "C", "D"]})
        self.assertEqual(r["pasos"].count("trabajo-tl-fase done"), 2,
                         "las dos anteriores")
        self.assertEqual(r["pasos"].count("trabajo-tl-fase active"), 1)
        self.assertEqual(r["pasos"].count("icono-girando"), 1)
        self.assertIn("trabajo-paso-punto", r["pasos"])

    def test_el_lateral_propio_de_un_tipo_gana_a_la_lista(self):
        r = self._pintar(ACTIVO, {"pasos": ["A", "B"], "lateral": "<i>mía</i>"})
        self.assertEqual(r["pasos"], "<i>mía</i>")

    def test_el_paso_dentro_de_la_fase_se_ve(self):
        """Era lo que enseñaba el overlay de CMv4.0 sobre la barra y se perdió
        al unificar: sin él, diez minutos de demux se ven igual que diez de
        merge."""
        r = self._pintar(dict(ACTIVO, paso="Demuxing BL/EL"), {"pasos": []})
        self.assertEqual(r["paso"], "Demuxing BL/EL")

    def test_sin_paso_cae_en_el_nombre_de_la_fase(self):
        r = self._pintar(dict(ACTIVO, paso=""), {"pasos": []})
        self.assertEqual(r["paso"], ACTIVO["fase_label"])

    def test_con_porcentaje_medido_la_barra_avanza(self):
        r = self._pintar(ACTIVO, {"pasos": []})
        self.assertFalse(r["indeterminada"])
        self.assertEqual(r["anchoBarra"], "40%")
        self.assertEqual(r["pct"], "40%")
        self.assertIn("Lleva", r["tiempos"])

    def test_sin_porcentaje_medido_la_barra_es_indeterminada(self):
        a = dict(ACTIVO, pct=None, pct_medido=False, eta_s=None, eta_fuente=None)
        r = self._pintar(a, {"pasos": []})
        self.assertTrue(r["indeterminada"])
        self.assertEqual(r["anchoBarra"], "")
        self.assertEqual(r["pct"], "—", "un guion, no un cero inventado")
        self.assertEqual(r["eta"], "")

    def test_el_eta_de_modelo_se_marca(self):
        r = self._pintar(dict(ACTIVO, eta_fuente="modelo"), {"pasos": []})
        self.assertIn("(aprox.)", r["eta"])

    def test_la_cartela_sale_de_la_vista_del_tipo(self):
        """El póster y el título largo los tenía el overlay de CMv4.0 y se
        perdieron al unificar. No vienen del contrato de progreso: los trae la
        pestaña, que ya tiene el `tmdb_info` delante."""
        r = self._pintar(ACTIVO, {"pasos": [], "cartel": {
            "url": "https://img/p.jpg", "titulo": "Predator: Tierra de Ojos",
            "meta": "2026 · 1h 47min · Acción"}})
        self.assertTrue(r["cartelVisible"])
        self.assertEqual(r["cartelTitulo"], "Predator: Tierra de Ojos")
        self.assertEqual(r["cartelMeta"], "2026 · 1h 47min · Acción")
        self.assertIn("<img", r["cartelPoster"])

    def test_sin_poster_queda_el_icono_del_tipo_no_un_hueco(self):
        r = self._pintar(ACTIVO, {"pasos": [], "cartel": {
            "url": "", "titulo": "Sin ficha", "meta": "", "icono": "✨"}})
        self.assertTrue(r["cartelVisible"])
        self.assertNotIn("<img", r["cartelPoster"])
        self.assertIn("✨", r["cartelPoster"])

    def test_sin_cartela_no_se_pinta_una_vacia(self):
        r = self._pintar(ACTIVO, {"pasos": []})
        self.assertFalse(r["cartelVisible"])

    def test_copiar_el_log_solo_aparece_si_hay_log(self):
        con = self._pintar(ACTIVO, {"pasos": [], "conLog": True})
        sin = self._pintar(ACTIVO, {"pasos": [], "conLog": False})
        self.assertEqual(con["copiar"], "")
        self.assertEqual(sin["copiar"], "none")

    def test_cancelar_solo_si_el_trabajo_lo_admite(self):
        r = self._pintar(dict(ACTIVO, cancelable=False), {"pasos": []})
        self.assertEqual(r["cancelar"], "none")


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElCuerpoSegunElTipo(unittest.TestCase):

    def _correr(self, fn_nombre, arg) -> str:
        guion = f"""
globalThis.escHtml = t => String(t);
{_iconos()}
{_fn(fn_nombre)}
console.log(JSON.stringify({fn_nombre}({json.dumps(arg)})));
"""
        return _node(guion)

    def test_un_log_vacio_lo_dice_en_vez_de_dejar_un_hueco(self):
        self.assertIn("Todavía no hay líneas", self._correr("_trabajoLogHTML", []))
        self.assertIn("Todavía no hay líneas", self._correr("_trabajoLogHTML", None))

    def test_el_log_se_recorta_por_arriba(self):
        """400 líneas es lo que cabe mirar; un rip emite ~136 y una fase
        CMv4.0 puede pasar de 2.000."""
        h = self._correr("_trabajoLogHTML", [f"linea {i}" for i in range(500)])
        self.assertNotIn("linea 0<", h)
        self.assertIn("linea 499", h)

    def test_los_pares_vacios_no_se_pintan(self):
        """Una fila «Error: —» en un trabajo que va bien es ruido."""
        h = self._correr("_trabajoKvHTML", [["Copiado", "3 GB"], ["Error", None],
                                            ["Destino", ""]])
        self.assertIn("Copiado", h)
        self.assertNotIn("Error", h)
        self.assertNotIn("Destino", h)


class TestCadaTipoRegistraSuVista(unittest.TestCase):
    """El armazón no sabe de ningún tipo: cada pestaña aporta el suyo."""

    def test_los_cinco_estan_registrados(self):
        for clave in ("rip", "serie", "analisis_extendido", "copia_biblioteca",
                      "cmv40"):
            self.assertIn(f"registrarDetalleDeTrabajo('{clave}'", JS,
                          f"nadie registró la vista de detalle de {clave}")

    def test_cada_uno_lo_registra_SU_pestana(self):
        """Si se registraran todos en `workbar.js`, añadir un tipo obligaría a
        tocar el armazón — que es justo lo que el registro evita."""
        esperado = {"tab1.js": ("rip", "serie"),
                    "tab2.js": ("analisis_extendido", "copia_biblioteca"),
                    "tab3.js": ("cmv40",)}
        for funcion, claves in (("_serie_vista_marcador", ()),):
            pass
        for pieza, claves in esperado.items():
            src = (APP_DIR / "static" / pieza).read_text(encoding="utf-8")
            for c in claves:
                self.assertIn(f"registrarDetalleDeTrabajo('{c}'", src,
                              f"{c} debería registrarse en {pieza}")

    def test_los_dos_que_no_producen_log_no_lo_fingen(self):
        """La copia y la creación de una serie no generan log: su detalle son
        bytes y episodios. Un log vacío sería peor que decirlo."""
        for pieza, clave in (("tab1.js", "serie"), ("tab2.js", "copia_biblioteca")):
            src = (APP_DIR / "static" / pieza).read_text(encoding="utf-8")
            i = src.index(f"registrarDetalleDeTrabajo('{clave}'")
            bloque = src[i:src.index("});", i)]
            self.assertIn("conLog: false", bloque)
            self.assertIn("_trabajoKvHTML", bloque)


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElModalSobreviveAlCambioDeFase(unittest.TestCase):
    """Un proyecto CMv4.0 encadena siete fases, y entre una y la siguiente el
    contrato deja de traer `activo` un instante. El modal se comparaba por
    TIPO, se daba por terminado, se sustituía por un armazón vacío —sin la
    columna de fases y con «Todavía no hay líneas de log» en medio— y apagaba
    su propio timer, así que no se recuperaba nunca. Visto en el NAS.
    """

    def _correr(self, secuencia) -> dict:
        """Pinta el modal con esa sucesión de valores de `activo`."""
        guion = f"""
const _els = {{}};
const _ids = ['trabajo-modal-icono','trabajo-modal-titulo','trabajo-modal-sub',
  'trabajo-modal-timeline','trabajo-modal-barra-wrap','trabajo-modal-barra',
  'trabajo-modal-tiempos','trabajo-modal-cuerpo','trabajo-modal-copiar',
  'trabajo-modal-cancelar','trabajo-modal-paso','trabajo-modal-pct',
  'trabajo-modal-eta','trabajo-modal-cartel','trabajo-modal-cartel-poster',
  'trabajo-modal-cartel-titulo','trabajo-modal-cartel-meta'];
for (const id of _ids) {{
  _els[id] = {{ textContent: '', innerHTML: '', style: {{}}, dataset: {{}},
    querySelector: () => null, closest: () => null, classList: {{
      _v: new Set(), toggle(c, on) {{ on ? this._v.add(c) : this._v.delete(c); }},
      has(c) {{ return this._v.has(c); }} }} }};
}}
globalThis.document = {{ getElementById: id => _els[id] || null,
                         querySelector: () => null }};
globalThis.escHtml = t => String(t);
globalThis.openModal = () => {{}};
globalThis.setInterval = () => 1;      // el bucle lo dirige el test
globalThis.clearInterval = () => {{ _timerApagado = true; }};
let _timerApagado = false;
{_iconos()}
{_fn('_workbarTiempo')}
{_fn('timelineDeTrabajo')}
{_fn('_trabajoCartelPinta')}
{_fn('_trabajoModalPinta')}
let workbarEstado = {{ activo: null, cola: [] }};
let _trabajoModalTimer = null, _trabajoModalTipo = null;
let _trabajoModalRef = null, _trabajoModalUltimo = null;
let _trabajoModalSinActivo = 0;
const _workbarDetalles = {{}};
// La vista del tipo lee su propia sesión, no el contrato: siempre tiene algo
// que enseñar aunque el trabajo ya no esté activo.
_workbarDetalles['cmv40'] = async (a) => ({{
  titulo: 'Predator.mkv', lateral: '<div>timeline de siete fases</div>',
  conLog: true, cuerpo: '<div class="cmv40-log">línea</div>',
  cartel: {{ url: '', titulo: 'Predator', meta: '2026' }},
}});
{_fn('_trabajoModalRefrescar')}
{_fn('_trabajoModalAbrir')}
(async () => {{
  const secuencia = {json.dumps(secuencia)};
  workbarEstado.activo = secuencia[0];
  await _trabajoModalAbrir(secuencia[0]);
  const fotos = [];
  for (const act of secuencia) {{
    workbarEstado.activo = act;
    await _trabajoModalRefrescar();
    fotos.push({{ timeline: _els['trabajo-modal-timeline'].innerHTML,
                 cuerpo: _els['trabajo-modal-cuerpo'].innerHTML,
                 paso: _els['trabajo-modal-paso'].textContent,
                 titulo: _els['trabajo-modal-titulo'].textContent }});
  }}
  console.log(JSON.stringify({{ fotos, timerApagado: _timerApagado }}));
}})();
"""
        return _node(guion)

    _FASE_C = {"id": "p1", "sobre": "p1", "tab": "cmv40", "tipo": "fase_cmv40",
               "que": "Fase C de Predator", "fase": "extract", "fase_n": 3,
               "fases_total": 7, "pct": 40, "pct_medido": True, "segundos": 300,
               "cancelable": True, "detalle": "cmv40", "paso": "Demuxing"}
    _FASE_F = dict(_FASE_C, fase="inject", fase_n=6,
                   que="Fase F de Predator", paso="Inyectando el RPU")

    def test_el_hueco_entre_dos_fases_no_vacia_el_modal(self):
        r = self._correr([self._FASE_C, None, self._FASE_F])
        for i, foto in enumerate(r["fotos"]):
            self.assertIn("timeline de siete fases", foto["timeline"],
                          f"foto {i}: se perdió la columna de fases")
            self.assertIn("cmv40-log", foto["cuerpo"],
                          f"foto {i}: se quedó sin log")
        self.assertEqual(r["fotos"][1]["paso"], "Cambiando de fase…")
        self.assertFalse(r["timerApagado"],
                         "apagar el timer en el hueco lo deja clavado")

    def test_un_trabajo_de_OTRO_proyecto_no_secuestra_el_modal(self):
        otro = dict(self._FASE_C, id="p2", sobre="p2", que="Fase A de Otra")
        r = self._correr([self._FASE_C, otro])
        self.assertEqual(r["fotos"][1]["paso"], "Cambiando de fase…",
                         "el modal sigue con SU trabajo, no salta al nuevo")


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElLateralNoSeReescribeSinMotivo(unittest.TestCase):
    """Reemplazar el innerHTML de la columna en cada tick tiene dos efectos que
    solo se ven usándola: el scroll vuelve al principio en cuanto el usuario lo
    mueve, y la animación del icono de la fase en curso se reinicia cada 1,5 s,
    así que se ve parada. Las dos las reportó el usuario probando en el NAS, y
    las dos las evitaba ya el overlay con su actualización incremental.
    """

    def _correr(self, laterales) -> dict:
        guion = f"""
const _els = {{}};
for (const id of ['trabajo-modal-icono','trabajo-modal-titulo','trabajo-modal-sub',
  'trabajo-modal-timeline','trabajo-modal-barra-wrap','trabajo-modal-barra',
  'trabajo-modal-tiempos','trabajo-modal-cuerpo','trabajo-modal-copiar',
  'trabajo-modal-cancelar','trabajo-modal-paso','trabajo-modal-pct',
  'trabajo-modal-eta','trabajo-modal-cartel','trabajo-modal-cartel-poster',
  'trabajo-modal-cartel-titulo','trabajo-modal-cartel-meta']) {{
  _els[id] = {{ textContent: '', _html: '', dataset: {{}}, style: {{}},
    escrituras: 0,
    get innerHTML() {{ return this._html; }},
    set innerHTML(v) {{ this._html = v; this.escrituras += 1; }},
    querySelector: () => null, closest: () => null, classList: {{
      _v: new Set(), toggle(c, on) {{ on ? this._v.add(c) : this._v.delete(c); }},
      has(c) {{ return this._v.has(c); }} }} }};
}}
globalThis.document = {{ getElementById: id => _els[id] || null,
                         querySelector: () => null }};
globalThis.escHtml = t => String(t);
{_iconos()}
{_fn('_workbarTiempo')}
{_fn('timelineDeTrabajo')}
{_fn('_trabajoCartelPinta')}
{_fn('_trabajoModalPinta')}
const _fnLlamadas = [];
const laterales = {json.dumps(laterales)}.map(
  l => l === '@fn' ? ((el) => _fnLlamadas.push(el === _els['trabajo-modal-timeline'])) : l);
for (const lateral of laterales) {{
  _trabajoModalPinta({json.dumps(ACTIVO)}, {{ lateral, pasos: [] }});
}}
console.log(JSON.stringify({{
  escrituras: _els['trabajo-modal-timeline'].escrituras,
  html: _els['trabajo-modal-timeline']._html,
  fnLlamadas: _fnLlamadas,
}}));
"""
        return _node(guion)

    def test_el_mismo_html_tres_veces_se_escribe_UNA(self):
        r = self._correr(["<div>fases</div>"] * 3)
        self.assertEqual(r["escrituras"], 1,
                         "cada reescritura devuelve el scroll al principio y "
                         "reinicia la animación de la fase activa")

    def test_si_cambia_si_se_reescribe(self):
        r = self._correr(["<div>a</div>", "<div>a</div>", "<div>b</div>"])
        self.assertEqual(r["escrituras"], 2)
        self.assertIn("b", r["html"])

    def test_un_lateral_que_es_FUNCION_actualiza_en_sitio(self):
        """Es como CMv4.0 conserva su timeline: la función recibe el
        contenedor y la modifica, sin destruir el DOM."""
        r = self._correr(["@fn", "@fn"])
        self.assertEqual(r["fnLlamadas"], [True, True])
        self.assertEqual(r["escrituras"], 0)


@unittest.skipIf(NODE is None, "node no está instalado")
class TestUnaVistaVaciaNoBorraLaAnterior(unittest.TestCase):
    """Las cinco vistas piden su estado al backend con `.catch(() => null)`, y
    con el NAS ocupado ese GET tarda. Visto en el NAS: en mitad de la Fase A el
    modal perdió la columna, la cartela y el log durante un minuto, y luego
    volvió solo."""

    def test_el_refresco_sin_datos_conserva_lo_ultimo_bueno(self):
        guion = f"""
const _els = {{}};
for (const id of ['trabajo-modal-icono','trabajo-modal-titulo','trabajo-modal-sub',
  'trabajo-modal-timeline','trabajo-modal-barra-wrap','trabajo-modal-barra',
  'trabajo-modal-tiempos','trabajo-modal-cuerpo','trabajo-modal-copiar',
  'trabajo-modal-cancelar','trabajo-modal-paso','trabajo-modal-pct',
  'trabajo-modal-eta','trabajo-modal-cartel','trabajo-modal-cartel-poster',
  'trabajo-modal-cartel-titulo','trabajo-modal-cartel-meta']) {{
  _els[id] = {{ textContent: '', innerHTML: '', dataset: {{}}, style: {{}},
    querySelector: () => null, closest: () => null, classList: {{
      _v: new Set(), toggle(c, on) {{ on ? this._v.add(c) : this._v.delete(c); }},
      has(c) {{ return this._v.has(c); }} }} }};
}}
globalThis.document = {{ getElementById: id => _els[id] || null,
                         querySelector: () => null }};
globalThis.escHtml = t => String(t);
globalThis.openModal = () => {{}};
globalThis.setInterval = () => 1;
globalThis.clearInterval = () => {{}};
{_iconos()}
{_fn('_workbarTiempo')}
{_fn('timelineDeTrabajo')}
{_fn('_trabajoCartelPinta')}
{_fn('_trabajoModalPinta')}
let workbarEstado = {{ activo: {json.dumps(ACTIVO)}, cola: [] }};
let _trabajoModalTimer = null, _trabajoModalTipo = null;
let _trabajoModalRef = null, _trabajoModalUltimo = null;
let _trabajoModalSinActivo = 0, _trabajoModalVista = null;
const _workbarDetalles = {{}};
let _vacia = false;
_workbarDetalles['cmv40'] = async () => _vacia ? {{}} : {{
  lateral: '<div>siete fases</div>', cuerpo: '<div class="cmv40-log">log</div>',
  cartel: {{ url: '', titulo: 'Predator', meta: '2026' }},
}};
{_fn('_trabajoModalRefrescar')}
{_fn('_trabajoModalAbrir')}
(async () => {{
  await _trabajoModalAbrir({json.dumps(ACTIVO)});
  _vacia = true;                       // el GET se cae
  await _trabajoModalRefrescar();
  console.log(JSON.stringify({{
    timeline: _els['trabajo-modal-timeline'].innerHTML,
    cuerpo: _els['trabajo-modal-cuerpo'].innerHTML,
    cartel: _els['trabajo-modal-cartel-titulo'].textContent,
  }}));
}})();
"""
        r = _node(guion)
        self.assertIn("siete fases", r["timeline"])
        self.assertIn("cmv40-log", r["cuerpo"])
        self.assertEqual(r["cartel"], "Predator")


@unittest.skipIf(NODE is None, "node no está instalado")
class TestCancelarNuncaSeVaDeVacio(unittest.TestCase):
    """Se vio en el NAS: el usuario pulsa Cancelar y no pasa NADA — ni
    petición en el log del servidor, ni toast, ni error de JS. La función leía
    siempre el activo del último poll, y en el hueco entre dos fases eso es
    `null`, así que salía por un `return` mudo.

    Un botón que no hace nada y no lo dice es el peor modo de fallo que tiene
    esta aplicación; ya pasó con el overlay que se comía los clics.
    """

    def _pulsar(self, activo, ultimo=None, arg="undefined") -> dict:
        guion = f"""
let workbarEstado = {{ activo: {json.dumps(activo)}, cola: [] }};
let _trabajoModalUltimo = {json.dumps(ultimo)};
const _llamadas = [], _toasts = [];
globalThis.apiFetch = async (url, o) => {{ _llamadas.push([url, o?.method]); }};
globalThis.showToast = (t, k) => _toasts.push([t, k]);
globalThis._mkvQualityCancel = () => _llamadas.push(['_mkvQualityCancel']);
globalThis.cmv40TrasCancelar = (id) => _llamadas.push(['cmv40TrasCancelar', id]);
globalThis.cancelMkvApply = () => _llamadas.push(['cancelMkvApply']);
globalThis.refrescarWorkbar = () => {{}};
let _confirm = null;
globalThis.showConfirm = (t, m, fn, label) => {{ _confirm = {{t, m, label}}; fn(); }};
{_fn('cancelarTrabajoActivo')}
cancelarTrabajoActivo({arg});
setTimeout(() => console.log(JSON.stringify(
  {{ llamadas: _llamadas, toasts: _toasts, confirm: _confirm }})), 10);
"""
        return _node(guion)

    _ACTIVO = {"id": "p1", "sobre": "p1", "tab": "cmv40", "que": "Fase C",
               "detalle": "cmv40"}

    def test_con_activo_manda_la_peticion(self):
        r = self._pulsar(self._ACTIVO)
        self.assertEqual(r["llamadas"][0], ["/api/cmv40/p1/cancel", "POST"])

    def test_cancelar_una_fase_corta_la_cadena_automatica(self):
        """El poller del auto-pipeline mira `running_phase`; el cancel lo deja
        a null con la fase todavía en `created`, así que lo interpreta como
        «hay que empezar» y vuelve a lanzar el pre-flight. Visto en el NAS al
        cancelar la Fase A."""
        r = self._pulsar(self._ACTIVO)
        self.assertIn(["cmv40TrasCancelar", "p1"], r["llamadas"])

    def test_sin_activo_cae_en_el_que_el_modal_esta_mirando(self):
        """El hueco entre dos fases duraba más que la paciencia del usuario."""
        r = self._pulsar(None, ultimo=self._ACTIVO)
        self.assertEqual(r["llamadas"][0], ["/api/cmv40/p1/cancel", "POST"])

    def test_el_modal_manda_su_trabajo_aunque_haya_otro_activo(self):
        otro = dict(self._ACTIVO, id="p2", tab="rip", detalle="rip")
        r = self._pulsar(otro, arg=json.dumps(self._ACTIVO))
        self.assertEqual(r["llamadas"][0], ["/api/cmv40/p1/cancel", "POST"])

    def test_sin_nada_que_cancelar_LO_DICE(self):
        r = self._pulsar(None)
        self.assertEqual(r["llamadas"], [])
        self.assertTrue(r["toasts"], "un return mudo deja al usuario a ciegas")

    def test_una_pestana_que_no_sabe_cancelar_LO_DICE(self):
        r = self._pulsar(dict(self._ACTIVO, tab="marciano"))
        self.assertEqual(r["llamadas"], [])
        self.assertEqual(r["toasts"][0][1], "error")

    def test_el_dialogo_no_ofrece_cancelar_dos_veces(self):
        """El botón de confirmar decía «Cancelar el trabajo» al lado del de
        descartar, que dice «Cancelar»: dos botones «Cancelar» con sentidos
        opuestos en el mismo diálogo."""
        r = self._pulsar(self._ACTIVO)
        self.assertNotIn("Cancelar", r["confirm"]["label"])
        self.assertNotIn("Cancelar", r["confirm"]["t"])


class TestLaSubPestanaDeColaSeRetiro(unittest.TestCase):
    """Era la asimetría: Tab 2 y Tab 3 no tienen nada equivalente en el centro,
    y además duplicaba lo que ahora dice la columna."""

    def test_no_queda_el_boton_ni_la_cortinilla(self):
        h = html()
        self.assertNotIn("subtab-btn-cola", h)
        self.assertNotIn("cola-expand-tab", h)
        self.assertNotIn("toggleColaSidebar", JS)

    def test_y_el_panel_con_el(self):
        """El panel vivía pegado a la sub-pestaña y a su propio poller. Lo que
        daba —las fases con su círculo y su transcurrido— lo da ahora el
        lateral del modal, que es el mismo sitio en el que se mira una fase
        CMv4.0. Sin ids `pc-*` sueltos que nadie actualiza."""
        h = html()
        for ident in ('id="panel-cola"', "pc-step-mount", "pc-elapsed-extract",
                      "pc-eta-extract", "pc-bar-extract"):
            self.assertNotIn(ident, h, f"{ident} quedó huérfano en el HTML")
        self.assertNotIn("pc-step-mount", JS)

    @unittest.skipIf(NODE is None, "node no está instalado")
    def test_el_lateral_del_rip_da_las_fases_con_su_transcurrido(self):
        """Lo que se perdería si el lateral se quedara vacío: en qué fase va,
        cuáles pasó y cuánto costó cada una."""
        guion = f"""
{_fn("escHtml")}
{_fn("_workbarTiempo")}
{_iconos()}
{_fn("_ripTimelineHTML")}
const a = {{ fase_n: 2, segundos: 754 }};
const sesion = {{ execution_history: [
  {{ phase_elapsed: {{ mount: 12, extract: 754 }} }}] }};
console.log(JSON.stringify({{ html: _ripTimelineHTML(a, sesion) }}));
"""
        h = _node(guion)["html"]
        for titulo in ("Abrir origen", "Extraer pistas", "Metadatos", "Cerrar origen"):
            self.assertIn(titulo, h)
        self.assertIn("12 s", h)            # la fase 1, ya terminada
        self.assertIn("12 min", h)          # la 2, en curso: el total del contrato
        self.assertIn("trabajo-tl-fase done", h)
        self.assertIn("trabajo-tl-fase active", h)
        self.assertIn("trabajo-tl-fase pending", h)

    def test_switch_sub_tab_no_conserva_ramas_muertas(self):
        src = pieza_de("switchSubTab")[1]
        self.assertNotIn("'cola'", src,
                         "quedan condiciones que ya nunca pueden ser ciertas")


if __name__ == "__main__":
    unittest.main()
