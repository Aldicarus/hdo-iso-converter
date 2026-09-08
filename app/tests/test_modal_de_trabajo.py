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
                  'trabajo-modal-pasos','trabajo-modal-barra-wrap','trabajo-modal-barra',
                  'trabajo-modal-tiempos','trabajo-modal-cuerpo','trabajo-modal-copiar',
                  'trabajo-modal-cancelar']) {{
  _els[id] = {{ textContent: '', innerHTML: '', style: {{}}, classList: {{
    _v: new Set(),
    toggle(c, on) {{ on ? this._v.add(c) : this._v.delete(c); }},
    has(c) {{ return this._v.has(c); }} }} }};
}}
globalThis.document = {{ getElementById: id => _els[id] || null }};
globalThis.escHtml = t => String(t);
{_iconos()}
{_fn('_workbarTiempo')}
{_fn('_trabajoPasosHTML')}
{_fn('_trabajoModalPinta')}
_trabajoModalPinta({json.dumps(activo)}, {json.dumps(vista)});
console.log(JSON.stringify({{
  icono: _els['trabajo-modal-icono'].innerHTML,
  titulo: _els['trabajo-modal-titulo'].textContent,
  sub: _els['trabajo-modal-sub'].textContent,
  pasos: _els['trabajo-modal-pasos'].innerHTML,
  tiempos: _els['trabajo-modal-tiempos'].innerHTML,
  cuerpo: _els['trabajo-modal-cuerpo'].innerHTML,
  indeterminada: _els['trabajo-modal-barra-wrap'].classList.has('indeterminada'),
  anchoBarra: _els['trabajo-modal-barra'].style.width,
  copiar: _els['trabajo-modal-copiar'].style.display,
  cancelar: _els['trabajo-modal-cancelar'].style.display,
}}));
"""
        return _node(guion)

    def test_la_cabecera_sale_de_la_vista_del_tipo(self):
        r = self._pintar(ACTIVO, {"titulo": "Predator.mkv",
                                  "sub": "origen.mkv", "pasos": [], "cuerpo": ""})
        self.assertEqual(r["titulo"], "Predator.mkv")
        self.assertEqual(r["sub"], "origen.mkv")

    def test_el_icono_lo_deriva_del_TIPO_no_de_la_vista(self):
        """Si cada vista trajera el suyo, la columna y el modal podrían acabar
        enseñando iconos distintos para el mismo trabajo."""
        r = self._pintar(ACTIVO, {"titulo": "X", "pasos": []})
        self.assertIn("icono-naranja", r["icono"], "fase_cmv40 va en naranja")
        self.assertIn("icono-chip-lg", r["icono"])

    def test_la_tira_marca_hechas_activa_y_pendientes(self):
        r = self._pintar(ACTIVO, {"pasos": ["A", "B", "C", "D"]})
        self.assertEqual(r["pasos"].count("trabajo-paso hecha"), 2,
                         "las dos anteriores")
        self.assertEqual(r["pasos"].count("trabajo-paso activa"), 1)
        # La activa gira; las pendientes son un punto, no un cuadro que pesa
        # lo mismo que un paso ya hecho.
        self.assertEqual(r["pasos"].count("icono-girando"), 1)
        self.assertIn("trabajo-paso-punto", r["pasos"])

    def test_con_porcentaje_medido_la_barra_avanza(self):
        r = self._pintar(ACTIVO, {"pasos": []})
        self.assertFalse(r["indeterminada"])
        self.assertEqual(r["anchoBarra"], "40%")
        self.assertIn("40%", r["tiempos"])

    def test_sin_porcentaje_medido_la_barra_es_indeterminada(self):
        a = dict(ACTIVO, pct=None, pct_medido=False, eta_s=None, eta_fuente=None)
        r = self._pintar(a, {"pasos": []})
        self.assertTrue(r["indeterminada"])
        self.assertEqual(r["anchoBarra"], "")
        self.assertIn("sin medir", r["tiempos"])

    def test_el_eta_de_modelo_se_marca(self):
        r = self._pintar(dict(ACTIVO, eta_fuente="modelo"), {"pasos": []})
        self.assertIn("(aprox.)", r["tiempos"])

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
        self.assertIn("rip-tl-fase done", h)
        self.assertIn("rip-tl-fase active", h)
        self.assertIn("rip-tl-fase pending", h)

    def test_switch_sub_tab_no_conserva_ramas_muertas(self):
        src = pieza_de("switchSubTab")[1]
        self.assertNotIn("'cola'", src,
                         "quedan condiciones que ya nunca pueden ser ciertas")


if __name__ == "__main__":
    unittest.main()
