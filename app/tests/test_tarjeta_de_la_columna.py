"""Una sola tarjeta para las cuatro secciones de la columna.

Antes había tres modelos de interacción conviviendo: el trabajo en curso con
sus botones siempre puestos, «En paralelo» igual, y solo los recientes
seleccionables —y encima con las acciones en un bloque HERMANO que empujaba la
lista al desplegarse—. Nada se parecía a nada.

El modelo es el de la `session-card` de los tres sidebars, que es el que
funciona: clic para seleccionar y las acciones DENTRO, tras un separador. La
única excepción es el trabajo en curso, que sale desplegado mientras no se
seleccione otra cosa: es lo que se está mirando, y esconder su «Cancelar»
detrás de un clic costaría más de lo que da la uniformidad.

Lo demás que fija este fichero:

- **La miniatura y el icono se pintan los dos**, uno encima del otro. Si la
  carátula no carga, el `onerror` la quita y debajo sigue el icono; un hueco
  gris no diría de qué es la fila.
- **El acento lateral lleva el color de la PESTAÑA**, no del tipo. Es lo que
  permite saber de dónde viene cada trabajo sin leer.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_tarjeta_de_la_columna -v
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import js_completo  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()


def _fn(nombre: str) -> str:
    i = JS.index(f"function {nombre}(")
    return JS[JS.rindex("\n", 0, i) + 1:JS.index("\n}\n", i) + 3]


def _bloque(marca: str) -> str:
    i = JS.index(marca)
    return JS[i:JS.index("\n};\n", i) + 4]


def _linea(marca: str) -> str:
    i = JS.index(marca)
    return JS[i:JS.index("\n", i) + 1]


_ACTIVO = {
    "id": "p1", "sobre": "p1", "tab": "cmv40", "tipo": "fase_cmv40",
    "que": "Upgrade CMv4.0 · Predator (2026)", "titulo": "Predator (2026)",
    "poster": "https://image.tmdb.org/t/p/w92/p.jpg",
    "fase": "extract", "fase_label": "Fase C — Extrayendo BL/EL",
    "paso": "Demuxing BL/EL", "fase_n": 3, "fases_total": 7, "pct": 41,
    "pct_medido": True, "segundos": 742, "eta_s": 1020,
    "eta_fuente": "medido", "cancelable": True, "detalle": "cmv40",
}
_COLA = [{"id": "rip:d1", "sobre": "d1", "tab": "rip", "tipo": "rip",
          "que": "Conversión a MKV · Dune (2024)", "titulo": "Dune (2024)",
          "poster": "", "posicion": 1}]
_PARALELO = [{"id": "a1", "sobre": "a1", "tab": "mkv",
              "tipo": "analisis_extendido",
              "que": "Apertura de un MKV · Blade Runner (1982)",
              "titulo": "Blade Runner (1982)", "poster": "", "segundos": 12,
              "detalle": "", "cancelable": False}]
_RECIENTES = [{"id": "h1", "tab": "rip", "tipo": "rip",
               "que": "Conversión a MKV · Alien (1979)",
               "titulo": "Alien (1979)", "poster": "", "segundos": 1860,
               "inicio": "2026-09-11T08:00:00+00:00", "estado": "done",
               "error": None, "ref_log": None}]


@unittest.skipIf(NODE is None, "node no está instalado")
class TarjetaCase(unittest.TestCase):

    def _render(self, estado, seleccion=None) -> str:
        guion = f"""
globalThis.escHtml = t => String(t);
const _els = {{}};
for (const id of ['workbar-body', 'workbar-count', 'workbar-toggle', 'workbar-search']) {{
  _els[id] = {{ value: '', style: {{}}, dataset: {{}}, textContent: '',
    innerHTML: '', classList: {{ _v: new Set(),
      toggle(c, on) {{ on ? this._v.add(c) : this._v.delete(c); }},
      has(c) {{ return this._v.has(c); }} }} }};
}}
globalThis.document = {{ getElementById: id => _els[id] || null,
                         querySelector: () => null }};
globalThis.Sortable = undefined;
{_fn('_svg')}
{_linea('const _TONO_POR_TAB = ')}
{_bloque('const _GLIFOS_TRABAJO = {')}
{_bloque('const _ICONOS_ESTADO = {')}
{_fn('_chipIcono')}
{_fn('iconoDeTrabajo')}
{_fn('iconoDeEstado')}
{_fn('normalizeSearch')}
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
let _workbarFiltroTab = 'all';
let _workbarSeleccion = {json.dumps(seleccion)};
{_fn('_workbarBusqueda')}
{_fn('_workbarFiltrando')}
{_fn('_workbarPasaFiltro')}
{_fn('_workbarRefReciente')}
{_fn('_workbarListaHTML')}
{_fn('_workbarMini')}
{_fn('_workbarDescripcion')}
{_fn('_workbarPips')}
{_fn('_workbarTarjeta')}
{_fn('_workbarActivoHTML')}
{_fn('_instalarReordenDeCola')}
const _CMV40_FIN = {{}};
{_fn('_workbarRender')}
let workbarEstado = {json.dumps(estado)};
_workbarRender(workbarEstado);
console.log(JSON.stringify({{html: _els['workbar-body'].innerHTML}}));
"""
        r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True,
                           timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló:\n{r.stderr[:900]}")
        return json.loads(r.stdout.strip().splitlines()[-1])["html"]

    def _todo(self, seleccion=None) -> str:
        return self._render({"activo": _ACTIVO, "cola": _COLA,
                             "interactivo": _PARALELO,
                             "recientes": _RECIENTES}, seleccion)


class TestLaMiniatura(TarjetaCase):

    def test_con_caratula_se_pinta_la_imagen(self):
        h = self._todo()
        self.assertIn('<img src="https://image.tmdb.org/t/p/w92/p.jpg"', h)

    def test_y_el_icono_sigue_DEBAJO_por_si_no_carga(self):
        """Los dos siempre. El `onerror` quita la imagen y debajo queda el
        icono: un hueco gris no diría de qué es la fila."""
        h = self._todo()
        i = h.index('class="wb-mini"')
        trozo = h[i:h.index("</div>", h.index("<img", i))]
        self.assertIn("icono-chip", trozo)
        self.assertIn("onerror", trozo)

    def test_sin_caratula_no_hay_img_vacia(self):
        h = self._render({"activo": None, "cola": _COLA, "interactivo": [],
                          "recientes": []})
        self.assertNotIn("<img", h)
        self.assertIn("icono-chip", h)


class TestElAcentoEsElDeLaPestana(TarjetaCase):

    def test_cada_tarjeta_lleva_la_clase_de_su_pestana(self):
        h = self._todo()
        for clase in ("wb-tab-cmv40", "wb-tab-rip", "wb-tab-mkv"):
            self.assertIn(clase, h, clase)

    def test_el_color_sale_de_la_paleta_y_no_de_un_hex_suelto(self):
        css = (APP_DIR / "static" / "style.css").read_text(encoding="utf-8")
        for clase, var in (("wb-tab-rip", "--blue"), ("wb-tab-mkv", "--teal"),
                           ("wb-tab-cmv40", "--orange")):
            i = css.index(f".{clase}")
            self.assertIn(f"var({var})", css[i:i + 90], clase)


class TestLasAccionesVanDENTRO(TarjetaCase):

    def test_solo_las_de_la_tarjeta_seleccionada(self):
        h = self._todo("rec:h1|2026-09-11T08:00:00+00:00")
        self.assertEqual(h.count("wb-card-acciones"), 1)
        self.assertIn("borrarReciente(", h)

    def test_y_dentro_de_la_tarjeta_no_como_hermano(self):
        """El bloque hermano de antes empujaba la lista al desplegarse."""
        h = self._todo("cola:rip:d1")
        i = h.index('data-ref="cola:rip:d1"')
        # Entre la tarjeta y sus botones no puede empezar otra tarjeta, y
        # `data-ref` solo lo lleva la raíz de cada una.
        self.assertNotIn("data-ref=", h[i + 10:h.index("wb-card-acciones", i)])

    def test_al_seleccionar_otra_solo_queda_una_desplegada(self):
        paralelo = [dict(_PARALELO[0], cancelable=True)]
        h = self._render({"activo": _ACTIVO, "cola": _COLA,
                          "interactivo": paralelo, "recientes": _RECIENTES},
                         "par:a1")
        self.assertEqual(h.count("wb-card-acciones"), 1)
        self.assertIn("cancelarTrabajoInteractivo", h)

    def test_lo_que_no_ofrece_nada_no_pinta_un_separador_vacio(self):
        """Un trabajo interactivo sin vista de detalle y que no se puede
        parar —abrir un MKV, un `disc-probe`— no tiene nada que desplegar."""
        h = self._todo("par:a1")
        self.assertNotIn("wb-card-acciones", h)

    def test_el_clic_en_un_boton_no_cambia_la_seleccion(self):
        """La tarjeta entera es clicable, así que sin `stopPropagation` pulsar
        «Quitar» seleccionaría y desplegaría en vez de actuar."""
        h = self._todo("rec:h1|2026-09-11T08:00:00+00:00")
        for accion in ("abrirDetalleDeReciente", "borrarReciente"):
            i = h.index(accion)
            self.assertIn("event.stopPropagation()", h[i - 60:i], accion)


class TestElTrabajoEnCursoSaleDesplegado(TarjetaCase):

    def test_sin_nada_seleccionado_el_activo_trae_sus_botones(self):
        h = self._todo()
        self.assertIn("cancelarTrabajoActivo()", h)
        self.assertEqual(h.count("wb-card-acciones"), 1)

    def test_y_se_repliega_al_seleccionar_otra(self):
        h = self._todo("rec:h1|2026-09-11T08:00:00+00:00")
        self.assertNotIn("cancelarTrabajoActivo()", h)

    def test_va_en_su_seccion_como_las_otras_tres(self):
        """El envoltorio es lo que le da el título «En curso» y los 14 px de
        aire a los lados. Sin él la tarjeta caía pegada al borde de la ventana
        y al de la columna."""
        h = self._todo()
        i = h.index('data-ref="act"')
        cabeza = h[:i]
        self.assertIn("workbar-seccion-titulo", cabeza)
        self.assertIn("En curso", cabeza)
        # Y dentro de la sección, no antes de que empiece.
        self.assertLess(cabeza.rindex('class="workbar-seccion"'), i)

    def test_todas_las_secciones_tienen_el_mismo_envoltorio(self):
        """Cuatro secciones, cuatro `.workbar-seccion`: si una se queda fuera,
        sus tarjetas van con otro margen que el resto."""
        h = self._todo()
        self.assertEqual(h.count('class="workbar-seccion"'), 4)

    def test_es_el_unico_con_barra_y_tiempos(self):
        h = self._todo()
        self.assertEqual(h.count("workbar-barra-fill"), 1)
        self.assertEqual(h.count("workbar-tiempos"), 1)


class TestLosDosRenglones(TarjetaCase):

    def test_arriba_la_pelicula_y_abajo_lo_que_se_le_hace(self):
        h = self._todo()
        i = h.index('data-ref="cola:rip:d1"')
        trozo = h[i:i + 900]
        self.assertIn(">Dune (2024)<", trozo)
        # Sin repetir la película: el `que` completo la lleva al final.
        self.assertIn(">Conversión a MKV<", trozo)

    def test_sin_titulo_no_se_dice_lo_mismo_dos_veces(self):
        """Una entrada de una cola persistida de antes no trae `titulo`: el
        renglón de arriba ya lleva la línea entera."""
        cola = [dict(_COLA[0], titulo="", que="Conversión a MKV")]
        h = self._render({"activo": None, "cola": cola, "interactivo": [],
                          "recientes": []})
        self.assertEqual(h.count("Conversión a MKV"), 1)
        self.assertNotIn("wb-card-sub", h)

    def test_el_paso_solo_lo_lleva_el_activo(self):
        h = self._todo()
        self.assertEqual(h.count("wb-card-paso"), 1)
        self.assertIn("Demuxing BL/EL", h)


if __name__ == "__main__":
    unittest.main()


class TestLosPuntitosDeFase(TarjetaCase):
    """La barra es del PROCESO completo —un turno de cola es el proyecto
    entero—, así que sin los puntos no se veía por qué fase iba."""

    def test_un_punto_por_fase(self):
        h = self._todo()
        # `<span` para no contar el contenedor `wb-pips`.
        self.assertEqual(h.count('<span class="wb-pip'), 7)

    def test_las_hechas_la_actual_y_las_que_faltan_se_distinguen(self):
        h = self._todo()
        i = h.index('class="wb-pips"')
        tira = h[i:h.index("</div>", i)]
        self.assertEqual(tira.count("wb-pip hecha"), 2)     # 1 y 2 de 7
        self.assertEqual(tira.count("wb-pip ahora"), 1)     # la 3
        self.assertEqual(tira.count('class="wb-pip"'), 4)   # 4..7

    def test_solo_los_lleva_el_activo(self):
        """Una tarjeta de la cola o del historial no está en ninguna fase."""
        h = self._todo("rec:h1|2026-09-11T08:00:00+00:00")
        self.assertEqual(h.count('class="wb-pips"'), 1)

    def test_sin_fases_no_se_pinta_una_tira_vacia(self):
        """Los dos trabajos de Tab 2 y la copia no tienen fases numeradas."""
        activo = dict(_ACTIVO, fases_total=0, fase_n=0)
        h = self._render({"activo": activo, "cola": [], "interactivo": [],
                          "recientes": []})
        self.assertNotIn("wb-pips", h)
