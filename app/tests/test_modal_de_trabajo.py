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
    # `_svg` va PRIMERO: `_GLIFOS_TRABAJO` la llama al construirse.
    i = JS.index("const _TONO_POR_TAB = ")
    return "\n".join([_fn("_svg"),
                      JS[i:JS.index("\n", i) + 1],
                      _bloque("const _GLIFOS_TRABAJO = {"),
                      _bloque("const _ICONOS_ESTADO = {"),
                      _fn("_chipIcono"),
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
  _els[id] = {{ textContent: '', innerHTML: '', className: '', style: {{}}, dataset: {{}},
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
{_fn('_relojHTML')}
{_fn('timelineDeTrabajo')}
{_fn('_trabajoCartelPinta')}
{_fn('anclajeDeLog')}
{_fn('restaurarAnclajeDeLog')}
{_fn('_trabajoModalPinta')}
_trabajoModalPinta({json.dumps(activo)}, {json.dumps(vista)});
console.log(JSON.stringify({{
  icono: _els['trabajo-modal-icono'].innerHTML,
  iconoClase: _els['trabajo-modal-icono'].className,
  titulo: _els['trabajo-modal-titulo'].textContent,
  sub: _els['trabajo-modal-sub'].textContent,
  pasos: _els['trabajo-modal-timeline'].innerHTML,
  tiempos: _els['trabajo-modal-tiempos'].innerHTML,
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

    def test_mientras_hay_trabajo_el_icono_es_el_ARO_que_gira(self):
        """Es el `.cmv40-running-spinner` del overlay. Un chip quieto no dice
        que la cosa siga viva, y es lo primero que se echó en falta."""
        r = self._pintar(ACTIVO, {"titulo": "X", "pasos": []})
        self.assertEqual(r["iconoClase"], "cmv40-running-spinner")

    def test_parado_vuelve_el_icono_del_TIPO(self):
        """Si cada vista trajera el suyo, la columna y el modal podrían acabar
        enseñando iconos distintos para el mismo trabajo."""
        r = self._pintar(dict(ACTIVO, cancelable=False),
                         {"titulo": "X", "pasos": []})
        self.assertIn("icono-naranja", r["icono"], "fase_cmv40 va en naranja")
        self.assertIn("icono-chip-lg", r["icono"])

    def test_las_fases_van_a_la_COLUMNA_con_EL_MISMO_marcado_de_cmv40(self):
        """Los cinco tipos las enseñan en el mismo sitio Y con las mismas
        clases. No se parece a la timeline de CMv4.0: **es** ella, así que
        hereda el raíl que conecta las fases, el resalte de la activa y el
        punto que late. Una versión propia se veía distinta dentro de la misma
        aplicación, que es justo lo que este modal vino a arreglar."""
        r = self._pintar(ACTIVO, {"pasos": ["A", "B", "C", "D"]})
        self.assertEqual(r["pasos"].count("cmv40-tl-step cmv40-tl-done"), 2,
                         "las dos anteriores")
        self.assertEqual(r["pasos"].count("cmv40-tl-step cmv40-tl-running"), 1)
        self.assertEqual(r["pasos"].count("cmv40-tl-step cmv40-tl-pending"), 1)
        # El raíl es lo que las conecta, y el icono de la activa el que late.
        self.assertEqual(r["pasos"].count("cmv40-tl-rail"), 4)
        self.assertIn('cmv40-tl-status-icon running', r["pasos"])
        self.assertIn("<ol class=\"cmv40-tl-steps\">", r["pasos"])
        # Y la cabecera con el reloj y el chip de progreso, como la de CMv4.0.
        self.assertIn("cmv40-tl-progress-pct", r["pasos"])
        self.assertIn("cmv40-tl-timer-elapsed", r["pasos"])

    def test_ninguna_pieza_usa_clases_propias(self):
        """Si la timeline genérica tuviera las suyas, cambiar el aspecto de la
        de CMv4.0 dejaría a las otras cuatro atrás — que es lo que pasó."""
        r = self._pintar(ACTIVO, {"pasos": ["A", "B"]})
        self.assertNotIn("trabajo-tl-", r["pasos"])

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

    def test_el_transcurrido_lo_cuenta_el_NAVEGADOR(self):
        """Venía del contrato y solo cambiaba con el refresco de 1,5 s, así
        que por debajo del minuto saltaba de dos en dos segundos."""
        r = self._pintar(dict(ACTIVO, segundos=7), {"pasos": []})
        self.assertIn("workbar-reloj", r["tiempos"])
        self.assertIn("data-desde=", r["tiempos"])
        self.assertIn("Lleva 7 s", r["tiempos"])

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
  _els[id] = {{ textContent: '', innerHTML: '', className: '', style: {{}}, dataset: {{}},
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
{_fn('_relojHTML')}
{_fn('timelineDeTrabajo')}
{_fn('_trabajoCartelPinta')}
{_fn('anclajeDeLog')}
{_fn('restaurarAnclajeDeLog')}
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
{_fn('_relojHTML')}
{_fn('timelineDeTrabajo')}
{_fn('_trabajoCartelPinta')}
{_fn('anclajeDeLog')}
{_fn('restaurarAnclajeDeLog')}
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
{_fn('_relojHTML')}
{_fn('timelineDeTrabajo')}
{_fn('_trabajoCartelPinta')}
{_fn('anclajeDeLog')}
{_fn('restaurarAnclajeDeLog')}
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
@unittest.skipIf(NODE is None, "node no está instalado")
class TestUnTerminadoNoSeSIGUE(unittest.TestCase):
    """Abrir el detalle de un trabajo acabado entraba en la rama de «cambiando
    de fase» —pensada para el hueco entre dos fases de un job vivo— y salía
    animado, con el botón de cancelar puesto y polleando veinte veces algo que
    ya no cambia."""

    def _correr(self, terminal, vacia=False) -> dict:
        guion = f"""
const _els = {{}};
for (const id of ['trabajo-modal-icono','trabajo-modal-titulo','trabajo-modal-sub',
  'trabajo-modal-timeline','trabajo-modal-barra-wrap','trabajo-modal-barra',
  'trabajo-modal-tiempos','trabajo-modal-cuerpo','trabajo-modal-copiar',
  'trabajo-modal-cancelar','trabajo-modal-paso','trabajo-modal-pct',
  'trabajo-modal-eta','trabajo-modal-cartel','trabajo-modal-cartel-poster',
  'trabajo-modal-cartel-titulo','trabajo-modal-cartel-meta']) {{
  _els[id] = {{ textContent: '', innerHTML: '', className: '', style: {{}},
    dataset: {{}}, querySelector: () => null, closest: () => null,
    classList: {{ _v: new Set(),
      toggle(c, on) {{ on ? this._v.add(c) : this._v.delete(c); }},
      has(c) {{ return this._v.has(c); }} }} }};
}}
globalThis.document = {{ getElementById: id => _els[id] || null,
                         querySelector: () => null }};
globalThis.escHtml = t => String(t);
globalThis.openModal = () => {{}};
let _apagado = false;
globalThis.setInterval = () => 7;
globalThis.clearInterval = () => {{ _apagado = true; }};
{_iconos()}
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
{_fn('timelineDeTrabajo')}
{_fn('_trabajoCartelPinta')}
{_fn('_trabajoKvHTML')}
{_fn('_trabajoModalConResumen')}
const _CMV40_FIN = {{ done: 'Terminado', cancelled: 'Cancelado',
                      error: 'Terminado con error' }};
{_bloque('const _MOTIVO_SIN_LOG = {')}
{_fn('anclajeDeLog')}
{_fn('restaurarAnclajeDeLog')}
{_fn('_trabajoModalPinta')}
let workbarEstado = {{ activo: null, cola: [] }};
let _trabajoModalTimer = null, _trabajoModalTipo = null;
let _trabajoModalRef = null, _trabajoModalUltimo = null;
let _trabajoModalSinActivo = 0, _trabajoModalVista = null;
let _llamadas = 0;
const _VACIA = {{vacia}};
const _workbarDetalles = {{ rip: async () => {{
  _llamadas += 1;
  return _VACIA ? {{ conLog: true, cuerpo: '' }}
                : {{ lateral: '<div>fases</div>',
                    cuerpo: '<div class="cmv40-log">log</div>' }};
}} }};
{_fn('_trabajoModalRefrescar')}
{_fn('_trabajoModalAbrir')}
(async () => {{
  await _trabajoModalAbrir({json.dumps(terminal)});
  await _trabajoModalRefrescar();
  console.log(JSON.stringify({{
    llamadas: _llamadas, apagado: _apagado,
    paso: _els['trabajo-modal-paso'].textContent,
    tiempos: _els['trabajo-modal-tiempos'].innerHTML,
    pct: _els['trabajo-modal-pct'].textContent,
    icono: _els['trabajo-modal-icono'].innerHTML,
    cancelar: _els['trabajo-modal-cancelar'].style.display,
    cuerpo: _els['trabajo-modal-cuerpo'].innerHTML,
  }}));
}})();
"""
        return _node(guion.replace("{vacia}", "true" if vacia else "false"))

    _TERMINADO = {
        "id": "s1", "sobre": "s1", "tab": "rip", "tipo": "rip",
        "que": "conversión a MKV de Dune", "detalle": "rip",
        "terminal": True, "paso": "Terminado", "segundos": 1830,
        "cancelable": False, "pct_medido": False, "fase_n": 0,
        "historial": {"estado": "done", "segundos": 1830,
                      "inicio": "2026-09-09T10:00:00+00:00"},
    }

    def test_ni_se_anima_ni_ofrece_cancelar(self):
        r = self._correr(self._TERMINADO)
        self.assertNotIn("cmv40-running-spinner", r["icono"])
        self.assertEqual(r["cancelar"], "none")

    def test_dice_cuanto_duro_no_cuanto_lleva(self):
        r = self._correr(self._TERMINADO)
        self.assertIn("Duró", r["tiempos"])
        self.assertNotIn("Lleva", r["tiempos"])
        self.assertEqual(r["pct"], "", "no hay porcentaje que dar")

    def test_no_dice_que_esta_cambiando_de_fase(self):
        r = self._correr(self._TERMINADO)
        self.assertEqual(r["paso"], "Terminado")

    def test_para_el_reloj_en_vez_de_pollear_veinte_veces(self):
        r = self._correr(self._TERMINADO)
        self.assertTrue(r["apagado"])

    def _motivo(self, sinDetalle) -> str:
        guion = f"""
globalThis.escHtml = t => String(t);
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
{_fn('_trabajoKvHTML')}
const _CMV40_FIN = {{ done: 'Terminado', cancelled: 'Cancelado' }};
{_bloque('const _MOTIVO_SIN_LOG = {')}
{_fn('_trabajoModalConResumen')}
console.log(JSON.stringify(_trabajoModalConResumen(
  {{ terminal: true, historial: {{ estado: 'done' }} }},
  {{ conLog: true, cuerpo: '', sinDetalle: {json.dumps(sinDetalle)} }})));
"""
        return _node(guion)["cuerpo"]

    def test_un_proyecto_BORRADO_no_es_un_estado_efimero(self):
        """Son casos distintos y contarlos como uno es decirle al usuario algo
        que no ha pasado. Visto en el NAS: fases CMv4.0 de proyectos ya
        borrados a las que se les explicaba que «su estado lo sustituye el
        siguiente trabajo»."""
        borrado = self._motivo("borrado")
        self.assertIn("El proyecto ya no existe", borrado)
        self.assertNotIn("lo sustituye el siguiente", borrado)
        efimero = self._motivo("efimero")
        self.assertIn("un solo trabajo a la vez", efimero)
        self.assertNotIn("ya no existe", efimero)

    def test_sin_motivo_no_se_inventa_uno(self):
        self.assertIn("No hay registro guardado", self._motivo(""))

    def test_cada_vista_declara_POR_QUE_no_tiene_registro(self):
        """Es lo único que puede saberlo: la de CMv4.0 y la del rip miran si
        su sesión sigue existiendo; las de Tab 2 saben que su estado es de un
        solo trabajo."""
        esperado = {"cmv40": "borrado", "rip": "borrado", "serie": "efimero",
                    "analisis_extendido": "efimero", "copia_biblioteca": "efimero"}
        for clave, motivo in esperado.items():
            with self.subTest(clave=clave):
                i = JS.index(f"registrarDetalleDeTrabajo('{clave}'")
                bloque = JS[i:JS.index("});", i)]
                self.assertIn("sinDetalle:", bloque)
                self.assertIn(f"'{motivo}'", bloque)

    def test_sin_log_guardado_cuenta_lo_que_SI_consta(self):
        """Las dos vistas de Tab 2 leen un estado que se resetea con el
        trabajo siguiente: al abrir una ejecución vieja devolvían un cuerpo
        vacío. Antes que un modal en blanco, lo que el historial sabe."""
        guion = f"""
globalThis.escHtml = t => String(t);
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
{_fn('_trabajoKvHTML')}
const _CMV40_FIN = {{ done: 'Terminado', cancelled: 'Cancelado' }};
{_bloque('const _MOTIVO_SIN_LOG = {')}
{_fn('_trabajoModalConResumen')}
const v = _trabajoModalConResumen({{
  terminal: true, segundos: 78,
  historial: {{ estado: 'cancelled', segundos: 78,
               inicio: '2026-09-09T09:00:00+00:00',
               fin: '2026-09-09T09:01:18+00:00', error: null }},
}}, {{ conLog: true, cuerpo: '', sinDetalle: 'efimero' }});
console.log(JSON.stringify(v));
"""
        v = _node(guion)
        self.assertIn("Cancelado", v["cuerpo"])
        self.assertIn("Duración", v["cuerpo"])
        self.assertIs(v["conLog"], False, "no hay log que copiar")
        self.assertIn("no se conserva", v["cuerpo"])

    def test_el_ciclo_APLICA_el_resumen_cuando_la_vista_viene_vacia(self):
        """No basta con que `_trabajoModalConResumen` sepa hacerlo: hay que
        llamarlo. Sin esto, el modal de un análisis extendido viejo salía en
        blanco — que es como se vio."""
        r = self._correr(self._TERMINADO, vacia=True)
        self.assertIn("Duración", r["cuerpo"])
        self.assertIn("trabajo-detalle-nota", r["cuerpo"],
                      "y con el motivo de que no haya registro")

    def test_pero_si_la_vista_trae_log_no_se_pisa(self):
        guion = f"""
globalThis.escHtml = t => String(t);
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
{_fn('_trabajoKvHTML')}
const _CMV40_FIN = {{ done: 'Terminado' }};
{_bloque('const _MOTIVO_SIN_LOG = {')}
{_fn('_trabajoModalConResumen')}
console.log(JSON.stringify(_trabajoModalConResumen(
  {{ terminal: true, historial: {{ estado: 'done' }} }},
  {{ conLog: true, cuerpo: '<div class="cmv40-log">hay log</div>' }})));
"""
        v = _node(guion)
        self.assertIn("hay log", v["cuerpo"])
        self.assertIs(v["conLog"], True)


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
globalThis.cerrarModalDeTrabajo = () => _llamadas.push(['cerrar']);
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


@unittest.skipIf(NODE is None, "node no está instalado")
class TestUnTrabajoTerminadoSeVuelveAMirar(unittest.TestCase):
    """«Últimos trabajos» solo listaba. Ni se podía volver al log de una
    ejecución ni quitarla de la lista."""

    _RECIENTES = [
        {"id": "dune_1", "tab": "rip", "tipo": "rip", "estado": "done",
         "que": "conversión a MKV de Dune", "segundos": 1830,
         "inicio": "2026-09-09T10:00:00+00:00"},
        {"id": "dune_1", "tab": "rip", "tipo": "rip", "estado": "cancelled",
         "que": "conversión a MKV de Dune", "segundos": 40,
         "inicio": "2026-09-09T09:00:00+00:00"},
    ]

    def _pintar(self, seleccion=None) -> dict:
        return self._pintar_con(self._RECIENTES, seleccion)

    def _pintar_con(self, recientes, seleccion=None) -> dict:
        guion = f"""
const _els = {{}};
for (const id of ['workbar-body', 'workbar-count', 'workbar-toggle', 'workbar-historial']) {{
  _els[id] = {{ innerHTML: '', textContent: '', style: {{}}, dataset: {{}},
    classList: {{ _v: new Set(),
      toggle(c, on) {{ on ? this._v.add(c) : this._v.delete(c); }},
      has(c) {{ return this._v.has(c); }} }} }};
}}
globalThis.document = {{ getElementById: id => _els[id] || null,
                         querySelector: () => null }};
globalThis.escHtml = t => String(t);
{_iconos()}
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
{_fn('_workbarRefReciente')}
{_fn('_workbarActivoHTML')}
{_fn('_workbarConsultasHTML')}
{_fn('_workbarListaHTML')}
{_fn('_instalarReordenDeCola')}
let _workbarSeleccion = {json.dumps(seleccion)};
{_fn('normalizeSearch')}
let _workbarFiltroTab = 'all';
{_fn('_workbarBusqueda')}
{_fn('_workbarFiltrando')}
{_fn('_workbarPasaFiltro')}
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
{_fn('_workbarConservandoElScroll')}
{_fn('_workbarRenderHistorial')}
{_fn('_workbarRender')}
let workbarEstado = {{ activo: null, cola: [], interactivo: [],
                       recientes: {json.dumps(recientes)} }};
_workbarRender(workbarEstado);
console.log(JSON.stringify({{ html: (_els['workbar-body'].innerHTML || '') + (_els['workbar-historial'].innerHTML || '') }}));
"""
        return _node(guion)

    def test_sin_seleccionar_no_hay_botones(self):
        h = self._pintar()["html"]
        self.assertIn("seleccionarTrabajo('rec:", h)
        self.assertNotIn("abrirDetalleDeReciente(", h)

    def test_el_que_espera_decision_se_distingue_y_invita(self):
        """Un pre-flight que acaba recomendando «mantener el MKV» no ha
        fallado ni ha terminado: depende del usuario. Sin distinguirlo se leía
        como un trabajo más de la lista y no había forma de responder."""
        guion_recientes = [dict(self._RECIENTES[0], estado="esperando",
                                tipo="preflight",
                                que="Validación previa · El padrino.mkv")]
        h = self._pintar_con(guion_recientes,
                             "rec:dune_1|2026-09-09T10:00:00+00:00")["html"]
        self.assertIn("wb-card", h)
        self.assertIn("wb-espera", h)
        self.assertIn("Requiere decisión", h)
        self.assertIn(">Decidir</button>", h)
        self.assertNotIn(">Detalle</button>", h)

    def test_uno_normal_sigue_diciendo_Detalle(self):
        h = self._pintar("rec:dune_1|2026-09-09T10:00:00+00:00")["html"]
        self.assertIn(">Detalle</button>", h)
        self.assertNotIn("Requiere decisión", h)

    def test_al_abrirlo_NO_se_trata_como_terminado(self):
        """Su modal tiene que ofrecer las salidas, no un resumen de lo que
        pasó: `terminal` apagaría los botones y pararía el poll."""
        i = JS.index("function abrirDetalleDeReciente(")
        cuerpo = JS[i:JS.index("\n}\n", i)]
        self.assertIn("terminal: r.estado !== 'esperando'", cuerpo)

    def test_al_seleccionar_salen_los_DOS(self):
        h = self._pintar("rec:dune_1|2026-09-09T10:00:00+00:00")["html"]
        self.assertIn("abrirDetalleDeReciente('rec:dune_1|2026-09-09T10:00:00+00:00')", h)
        self.assertIn("borrarReciente('rec:dune_1|2026-09-09T10:00:00+00:00')", h)

    def test_solo_se_selecciona_UNA_con_el_mismo_id(self):
        """Una sesión re-ejecutada deja varias líneas con el mismo id: la
        referencia lleva el `inicio` para poder distinguirlas."""
        h = self._pintar("rec:dune_1|2026-09-09T09:00:00+00:00")["html"]
        self.assertEqual(h.count("wb-card wb-tab-rip selected"), 1)
        self.assertEqual(h.count("abrirDetalleDeReciente("), 1)
        self.assertIn("09:00:00", h[h.index("selected"):])

    def _timeline(self, a, pasos):
        guion = f"""
globalThis.escHtml = t => String(t);
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
{_iconos()}
{_fn('timelineDeTrabajo')}
console.log(JSON.stringify({{ html: timelineDeTrabajo(
  {json.dumps(pasos)}, {json.dumps(a)}, 'Fases') }}));
"""
        return _node(guion)["html"]

    def test_un_terminado_no_deja_la_columna_en_gris(self):
        """`fase_n` describe el PRESENTE y en una línea del historial vale 0,
        así que sin tratar el caso terminal la columna salía entera pendiente
        —como si el trabajo no hubiera hecho nada— con «0/4 · 0%»."""
        a = {"terminal": True, "historial": {"estado": "done"},
             "fase_n": 0, "segundos": 1830}
        h = self._timeline(a, ["A", "B", "C", "D"])
        self.assertEqual(h.count("cmv40-tl-step cmv40-tl-done"), 4)
        self.assertNotIn("cmv40-tl-running", h)
        self.assertIn("4/4 · 100%", h)

    def test_un_terminado_no_habla_de_lo_que_queda(self):
        a = {"terminal": True, "historial": {"estado": "done"}, "fase_n": 0,
             "segundos": 100, "eta_s": 300, "eta_fuente": "medido"}
        self.assertNotIn("Restante", self._timeline(a, ["A", "B"]))

    def test_una_cancelada_enseña_hasta_donde_llego(self):
        """La fila fija su estado: el rip sabe por su historial de ejecución
        cuáles corrieron, y eso es lo que se quiere ver de una cancelada."""
        a = {"terminal": True, "historial": {"estado": "cancelled"},
             "fase_n": 0, "segundos": 40}
        h = self._timeline(a, [
            {"titulo": "A", "estado": "done", "nota": "completado · 9 s"},
            {"titulo": "B", "estado": "pending"},
        ])
        self.assertEqual(h.count("cmv40-tl-done"), 1)
        self.assertIn("no llegó a ejecutarse", h)
        self.assertIn("1/2 · 50%", h)

    def test_vivo_sigue_marcando_la_fase_en_curso(self):
        h = self._timeline({"fase_n": 2, "segundos": 60}, ["A", "B", "C"])
        self.assertEqual(h.count("cmv40-tl-running"), 1)
        self.assertIn("en curso…", h)

    def test_el_detalle_reusa_el_MISMO_modal(self):
        """No hay una vista aparte para lo terminado: las cinco leen su propia
        sesión, no el contrato de progreso, y por eso siguen teniendo algo que
        enseñar cuando ya no corre nada."""
        i = JS.index("function abrirDetalleDeReciente(")
        cuerpo = JS[i:JS.index("\n}\n", i)]
        self.assertIn("_trabajoModalAbrir", cuerpo)
        self.assertIn("_DETALLE_POR_TIPO", cuerpo)
        self.assertIn("cancelable: false", cuerpo,
                      "lo terminado no se cancela")

    def test_quitar_avisa_de_lo_que_NO_borra(self):
        i = JS.index("function borrarReciente(")
        cuerpo = JS[i:JS.index("\n}\n", i)]
        self.assertIn("showConfirm", cuerpo)
        self.assertIn("no se tocan", cuerpo)
        self.assertIn("method: 'DELETE'", cuerpo)
        self.assertIn("inicio=", cuerpo, "la clave es id + inicio")


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
{_fn("_relojHTML")}
{_iconos()}
{_fn("timelineDeTrabajo")}
{_fn("_ripTimelineHTML")}
const a = {{ fase_n: 2, segundos: 754 }};
const sesion = {{ execution_history: [
  {{ phase_elapsed: {{ mount: 12, extract: 754 }} }}] }};
console.log(JSON.stringify({{ html: _ripTimelineHTML(a, sesion) }}));
"""
        h = _node(guion)["html"]
        for titulo in ("Apertura del origen", "Extracción de pistas",
                       "Escritura de metadatos", "Cierre del origen"):
            self.assertIn(titulo, h)
        self.assertIn("completado · 12 s", h)   # la fase 1, ya terminada
        self.assertIn("cmv40-tl-step cmv40-tl-done", h)
        self.assertIn("cmv40-tl-step cmv40-tl-running", h)
        self.assertIn("cmv40-tl-step cmv40-tl-pending", h)

    @unittest.skipIf(NODE is None, "node no está instalado")
    def test_la_fase_en_curso_no_se_marca_como_completada(self):
        """El historial registra también el transcurrido de la que está
        corriendo: fiarse de que el dato exista la daba por terminada."""
        guion = f"""
{_fn("escHtml")}
{_fn("_workbarTiempo")}
{_fn("_relojHTML")}
{_iconos()}
{_fn("timelineDeTrabajo")}
{_fn("_ripTimelineHTML")}
const a = {{ fase_n: 2, segundos: 754, pct: 63, pct_medido: true }};
const sesion = {{ execution_history: [
  {{ phase_elapsed: {{ mount: 12, extract: 754 }} }}] }};
console.log(JSON.stringify({{ html: _ripTimelineHTML(a, sesion) }}));
"""
        h = _node(guion)["html"]
        self.assertNotIn("completado · 12 min", h)
        self.assertIn("en curso…", h)

    def test_switch_sub_tab_no_conserva_ramas_muertas(self):
        src = pieza_de("switchSubTab")[1]
        self.assertNotIn("'cola'", src,
                         "quedan condiciones que ya nunca pueden ser ciertas")


if __name__ == "__main__":
    unittest.main()
