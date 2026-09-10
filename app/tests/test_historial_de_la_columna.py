"""El historial de la columna: consultable, y sin pelearse con el poll.

Eran **cinco entradas fijas** —un `slice(0, 5)` sobre un endpoint que servía
ocho— cuando `GET /api/historial` da hasta mil. Y viajaba con el poll de cada
2 s, con dos consecuencias que no se ven leyendo el código: bajar por él era
imposible (volvía al principio en la vuelta siguiente) y las carátulas se
volvían a decodificar en cada una.

Ahora vive en su propio contenedor y se carga aparte, cuando cambia lo que
está en marcha —que es justo cuando aparece una línea nueva— y cuando el
usuario pide más.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_historial_de_la_columna -v
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


def _node(guion: str) -> dict:
    r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True,
                       timeout=30)
    if r.returncode != 0:
        raise AssertionError(f"node falló:\n{r.stderr[:900]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def _linea_hist(id, **kw):
    base = {"id": id, "tab": "rip", "tipo": "rip", "titulo": f"Peli {id}",
            "que": f"Conversión a MKV · Peli {id}", "poster": "",
            "segundos": 600, "estado": "done", "error": None,
            "inicio": "2026-09-11T08:00:00+00:00",
            "fin": "2026-09-11T08:10:00+00:00", "ref_log": None}
    base.update(kw)
    return base


@unittest.skipIf(NODE is None, "node no está instalado")
class HistorialCase(unittest.TestCase):

    def _render(self, lineas, hay_mas=False, tope=25, ahora=None):
        guion = f"""
globalThis.escHtml = t => String(t);
const _els = {{}};
for (const id of ['workbar-historial', 'workbar-search']) {{
  _els[id] = {{ value: '', scrollTop: 0, innerHTML: '' }};
}}
globalThis.document = {{ getElementById: id => _els[id] || null }};
globalThis.Date_ = Date;
{_fn('_svg')}
{_linea('const _TONO_POR_TAB = ')}
{_bloque('const _GLIFOS_TRABAJO = {')}
{_bloque('const _ICONOS_ESTADO = {')}
{_fn('_chipIcono')}
{_fn('iconoDeTrabajo')}
{_fn('iconoDeEstado')}
{_fn('normalizeSearch')}
{_fn('_workbarTiempo')}
let _workbarFiltroTab = 'all';
let _workbarSeleccion = null;
let _workbarHayMasHistorial = {json.dumps(hay_mas)};
const _WORKBAR_HISTORIAL_PASO = {tope};
let workbarEstado = {{ activo: null, cola: [], interactivo: [],
                       recientes: {json.dumps(lineas)} }};
{_fn('_workbarBusqueda')}
{_fn('_workbarFiltrando')}
{_fn('_workbarPasaFiltro')}
{_fn('_workbarRefReciente')}
{_fn('_workbarMini')}
{_fn('_workbarDescripcion')}
{_fn('_workbarTarjeta')}
{_fn('_workbarDia')}
{_fn('_workbarHace')}
{_fn('_workbarTarjetaReciente')}
{_fn('_workbarRenderHistorial')}
_workbarRenderHistorial();
console.log(JSON.stringify({{html: _els['workbar-historial'].innerHTML}}));
"""
        return _node(guion)["html"]


class TestYaNoSeCortaEnCinco(HistorialCase):

    def test_salen_todas_las_que_haya(self):
        h = self._render([_linea_hist(f"p{i}") for i in range(12)])
        # `data-ref` solo lo lleva la raíz de cada tarjeta; `wb-card` sale
        # también en sus clases hijas.
        self.assertEqual(h.count('data-ref="rec:'), 12)

    def test_con_mas_por_cargar_sale_el_boton(self):
        h = self._render([_linea_hist("p1")], hay_mas=True)
        self.assertIn("verMasHistorial()", h)

    def test_y_si_no_hay_mas_no_sale(self):
        h = self._render([_linea_hist("p1")], hay_mas=False)
        self.assertNotIn("verMasHistorial()", h)


class TestSeAgrupaPorDia(HistorialCase):

    def test_una_cabecera_por_dia_y_no_una_por_linea(self):
        import datetime as dt
        hoy = dt.datetime.now(dt.timezone.utc)
        ayer = hoy - dt.timedelta(days=1)
        h = self._render([
            _linea_hist("a", inicio=hoy.isoformat()),
            _linea_hist("b", inicio=hoy.isoformat()),
            _linea_hist("c", inicio=ayer.isoformat()),
        ])
        self.assertEqual(h.count('class="wb-dia"'), 2)
        self.assertIn(">Hoy<", h)
        self.assertIn(">Ayer<", h)

    def test_lo_viejo_lleva_su_fecha(self):
        h = self._render([_linea_hist("a", inicio="2026-09-08T10:00:00+00:00")])
        self.assertNotIn(">Hoy<", h)
        self.assertIn("sept", h.lower())


class TestSeVeCuandoPasoYPorQueFallo(HistorialCase):

    def test_ademas_de_lo_que_duro_dice_cuando_fue(self):
        import datetime as dt
        hace = (dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(minutes=12)).isoformat()
        h = self._render([_linea_hist("a", fin=hace)])
        self.assertIn("10 min", h)      # lo que duró
        self.assertIn("hace 12 min", h)  # cuándo fue

    def test_el_motivo_del_fallo_se_lee_sin_abrir_el_detalle(self):
        """Estaba guardado en el registro y no se enseñaba en ninguna parte."""
        h = self._render([_linea_hist("a", estado="error",
                                      error="dovi_tool se cayó\ncon rastro")])
        self.assertIn("wb-card-error", h)
        self.assertIn("dovi_tool se cayó", h)
        # Solo la primera línea: el rastro entero no cabe en una tarjeta.
        self.assertNotIn("con rastro", h)

    def test_uno_que_termina_bien_no_lleva_esa_línea(self):
        h = self._render([_linea_hist("a")])
        self.assertNotIn("wb-card-error", h)


class TestNoSePeleaConElPoll(unittest.TestCase):
    """Las dos razones por las que el historial dejó de viajar con el poll,
    dichas sobre el código: no se ven ejecutando nada, pero sí rompen."""

    @staticmethod
    def _cuerpo(nombre):
        """El cuerpo SIN comentarios: si no, un `assertIn` se conforma con
        encontrar lo que busca en la explicación de al lado — pasó con este
        mismo test."""
        import re
        i = JS.index(f"function {nombre}(")
        cuerpo = JS[i:JS.index("\n}\n", i)]
        return re.sub(r"//.*|/\*(?:.|\n)*?\*/", "", cuerpo)

    def test_el_poll_NO_pide_el_historial(self):
        cuerpo = self._cuerpo("refrescarWorkbar")
        self.assertIn("'/api/trabajos?recientes=0'", cuerpo,
                      "el poll volvió a traerse el historial entero cada 2 s")

    def test_se_recarga_cuando_algo_deja_de_estar_en_marcha(self):
        cuerpo = self._cuerpo("refrescarWorkbar")
        self.assertIn("_workbarCargarHistorial()", cuerpo)
        # Y la firma incluye lo interactivo: un pre-flight que acaba pidiendo
        # decisión deja su línea y no aparece ni en `activo` ni en `cola`.
        self.assertIn("interactivo", cuerpo)

    def test_se_conserva_el_scroll_al_recargar(self):
        cuerpo = self._cuerpo("_workbarRenderHistorial")
        self.assertIn("scrollTop", cuerpo,
                      "al recargar, el historial vuelve al principio")

    def test_y_vive_en_su_propio_contenedor(self):
        from frontend_sources import html
        h = html()
        self.assertIn('id="workbar-historial"', h)
        # Fuera de #workbar-body, que es lo que el poll repinta.
        cuerpo = h[h.index('<div id="workbar-body">'):]
        self.assertNotIn('id="workbar-historial"',
                         cuerpo[:cuerpo.index("</div>")])


if __name__ == "__main__":
    unittest.main()
