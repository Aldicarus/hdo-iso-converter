"""El historial de la columna: consultable, y sin pelearse con el poll.

Eran **cinco entradas fijas** —un `slice(0, 5)` sobre un endpoint que servía
ocho— cuando `GET /api/historial` da hasta mil. Y viajaba con el poll de cada
2 s, con dos consecuencias que no se ven leyendo el código: bajar por él era
imposible (volvía al principio en la vuelta siguiente) y las carátulas se
volvían a decodificar en cada una.

Ahora vive en su propio contenedor y se carga aparte: cuando el servidor dice
que el historial ha cambiado, y cuando el usuario pide más.

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

from frontend_sources import sistema_de_iconos, js_completo  # noqa: E402

SISTEMA_ICONOS = sistema_de_iconos()

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
        return _node(self._guion(lineas, hay_mas, tope) + """
_workbarRenderHistorial();
console.log(JSON.stringify({html: _els['workbar-historial'].innerHTML}));
""")["html"]

    def _guion(self, lineas, hay_mas=False, tope=25):
        return f"""
globalThis.escHtml = t => String(t);
const _els = {{}};
for (const id of ['workbar-historial', 'workbar-search', 'workbar-scroll']) {{
  _els[id] = {{ value: '', scrollTop: 0, innerHTML: '' }};
}}
globalThis.document = {{ getElementById: id => _els[id] || null }};
globalThis.Date_ = Date;
{SISTEMA_ICONOS}
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
{_fn('_workbarChips')}
{_fn('_workbarTarjeta')}
{_fn('_workbarDia')}
{_fn('_workbarHace')}
{_fn('_workbarTarjetaReciente')}
{_fn('_workbarConservandoElScroll')}
{_fn('_workbarRenderHistorial')}
"""


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


class TestUnaCancelacionSeLeeComoTal(HistorialCase):
    """En rojo y con el motivo, igual que un fallo.

    En gris la tarjeta no contaba nada —y el gris es el de «en cola», así que
    ni siquiera se leía como un final—. De los cinco tipos, solo el análisis
    extendido daba el porqué, y a costa de marcarse como error.
    """

    def test_sale_en_rojo(self):
        h = self._render([_linea_hist("p1", estado="cancelled",
                                      error="Cancelado por el usuario")])
        self.assertIn("icono-rojo", h)
        self.assertNotIn("icono-gris", h)

    def test_y_con_el_motivo_a_la_vista(self):
        h = self._render([_linea_hist("p1", estado="cancelled",
                                      error="Cancelado por el usuario")])
        self.assertIn("Cancelado por el usuario", h)

    def test_lo_que_termina_bien_sigue_en_verde_y_sin_aviso(self):
        h = self._render([_linea_hist("p1")])
        self.assertIn("icono-verde", h)
        self.assertNotIn("wb-card-error", h)


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


    def test_el_historial_y_lo_que_corre_comparten_UN_scroll(self):
        """Uno por zona era peor que ninguno: el historial se reservaba su
        parte del alto y lo que estaba en marcha quedaba detrás de un scroll
        de media columna. (La geometría la mide
        `test_columna_como_pestana`; esto guarda las reglas.)"""
        import re
        css = (APP_DIR / "static" / "style.css").read_text(encoding="utf-8")
        # Sin comentarios: si no, el bloque de arriba entra en el selector.
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        reglas = re.findall(r"([^{}]+)\{([^}]*)\}", css)
        inner = [c for sel, c in reglas if sel.strip() == ".workbar-inner"]
        self.assertEqual(len(inner), 1)
        self.assertIn("overflow-y: auto", inner[0])
        for sel, cuerpo in reglas:
            if "#workbar-body" in sel or "#workbar-historial" in sel:
                self.assertNotIn("overflow", cuerpo,
                                 f"«{sel.strip()}» vuelve a scrollear solo")

    def test_y_vive_en_su_propio_contenedor(self):
        from frontend_sources import html
        h = html()
        self.assertIn('id="workbar-historial"', h)
        # Fuera de #workbar-body, que es lo que el poll repinta.
        cuerpo = h[h.index('<div id="workbar-body">'):]
        self.assertNotIn('id="workbar-historial"',
                         cuerpo[:cuerpo.index("</div>")])


@unittest.skipIf(NODE is None, "node no está instalado")
@unittest.skipIf(NODE is None, "node no está instalado")
class TestElScrollNoSaltaAlRepintar(HistorialCase):
    """El scroll es del contenedor padre, así que reemplazar el HTML de una
    zona lo arrastra: mientras está vacía, el navegador recorta el `scrollTop`
    al nuevo máximo y ya no vuelve. El cuerpo se repinta cada 2 s."""

    def _scroll_tras_repintar(self, y):
        guion = self._guion([_linea_hist(f"p{i}") for i in range(12)]) + f"""
_workbarRenderHistorial();
_els['workbar-scroll'].scrollTop = {y};
_workbarRenderHistorial();
console.log(JSON.stringify({{y: _els['workbar-scroll'].scrollTop}}));
"""
        return _node(guion)["y"]

    def test_se_conserva_el_sitio_por_el_que_iba(self):
        self.assertEqual(self._scroll_tras_repintar(240), 240)


@unittest.skipIf(NODE is None, "node no está instalado")
class TestSeRecargaCuandoElHistorialCambia(unittest.TestCase):
    """Y no cuando cambia otra cosa.

    La señal era «cambió lo que está en marcha», que solo acierta con las
    líneas NUEVAS. Una ya escrita que se resuelve no mueve nada: al contestar
    «mantener el MKV», la línea del pre-flight pasa de «requiere decisión» a
    terminada, pero si lo que estaba corriendo seguía corriendo —un rip, que
    dura 40 minutos— la tarjeta se quedaba pidiendo una decisión ya tomada y
    ofreciendo el botón de tomarla. Ahora el servidor cuenta las veces que ha
    cambiado el historial y esa es la señal.
    """

    def _ticks(self, ticks, fallos=0):
        """Corre `refrescarWorkbar` una vez por tick y cuenta las recargas.

        `fallos` — cuántas de las primeras peticiones del historial se caen,
        para comprobar que se reintenta.
        """
        guion = f"""
let workbarEstado = {{ activo: null, cola: [], interactivo: [], recientes: [] }};
globalThis.document = {{ getElementById: () => null, querySelector: () => null }};
const _workbarOyentes = [];
let _workbarUltimaFirma = null;
let _workbarUltimaRevHistorial = null;
let _workbarTopeHistorial = 25;
let _workbarHayMasHistorial = false;
globalThis._workbarRender = () => {{}};
globalThis._workbarRenderHistorial = () => {{}};
{_fn('_workbarFirma')}
{_fn('_workbarCargarHistorial')}
{_fn('refrescarWorkbar')}
const TICKS = {json.dumps(ticks)};
let _recargas = 0, _fallan = {fallos}, _tick = null;
globalThis.apiFetch = async (url) => {{
  if (url.startsWith('/api/historial')) {{
    _recargas++;
    if (_fallan-- > 0) return null;      // como un fallo de red
    return {{ trabajos: [] }};
  }}
  return _tick;
}};
(async () => {{
  for (const t of TICKS) {{
    _tick = t;
    await refrescarWorkbar();
    await new Promise(r => setTimeout(r, 0));   // la carga va sin await
  }}
  console.log(JSON.stringify({{ recargas: _recargas }}));
}})();
"""
        return _node(guion)["recargas"]

    @staticmethod
    def _tick(rev, activo=None, cola=()):
        return {"activo": activo, "cola": list(cola), "interactivo": [],
                "historial_rev": rev}

    def test_la_primera_vuelta_lo_carga(self):
        self.assertEqual(self._ticks([self._tick(0)]), 1)

    def test_y_si_no_cambia_no_se_vuelve_a_pedir(self):
        """Es cada 2 s: recargarlo por costumbre le tira el scroll al usuario
        que esté leyéndolo."""
        self.assertEqual(self._ticks([self._tick(7)] * 4), 1)

    def test_una_linea_nueva_lo_recarga(self):
        self.assertEqual(
            self._ticks([self._tick(7), self._tick(7), self._tick(8)]), 2)

    def test_una_decision_contestada_TAMBIEN_aunque_siga_el_mismo_trabajo(self):
        """El caso que fallaba: el rip de siempre corriendo, y el pre-flight
        contestado. No se mueve nada salvo la línea."""
        rip = {"id": "rip1", "tab": "rip", "tipo": "rip", "que": "Conversión"}
        self.assertEqual(
            self._ticks([self._tick(4, activo=rip),
                         self._tick(5, activo=rip)]), 2)

    def test_moverse_la_cola_por_si_solo_NO_lo_recarga(self):
        """Un trabajo que arranca no escribe ninguna línea: su sitio es «En
        curso». Recargar ahí era pedir el historial entero para nada."""
        j = {"id": "j1", "tab": "mkv", "tipo": "analisis_extendido"}
        self.assertEqual(
            self._ticks([self._tick(3), self._tick(3, cola=[j])]), 1)

    def test_si_la_carga_falla_se_reintenta_en_la_vuelta_siguiente(self):
        """La revisión se apunta al recibirla, no al pedirla. Si no, un fallo
        de red dejaba el historial viejo hasta el cambio siguiente."""
        self.assertEqual(self._ticks([self._tick(9)] * 3, fallos=1), 2)

    def test_un_fallo_del_poll_no_cuenta_como_cambio(self):
        """`apiFetch` devuelve null y la columna conserva lo último bueno; sin
        `historial_rev` no hay nada que comparar."""
        self.assertEqual(self._ticks([self._tick(2), None, self._tick(2)]), 1)



if __name__ == "__main__":
    unittest.main()

sys.path.insert(0, str(APP_DIR))          # para `historial`, que es del backend


class TestElNombreViejoNoSeQuedaEnLaColumna(unittest.TestCase):
    """Un trabajo que cambia de nombre no deja dos nombres en la columna.

    `historial.jsonl` es append-only y **no se migra**: reescribir el
    `/config` de un usuario para cambiar una palabra no compensa. Pero lo que
    se ve sí tiene que estar al día, así que el nombre se actualiza al LEER.
    Sin esto, «Análisis extendido · Avatar» seguía en «Recientes» junto a los
    nuevos «Análisis RPU/Luz MKV · …» — dos nombres para el mismo trabajo.
    """

    def test_el_analisis_extendido_se_lee_con_su_nombre_de_hoy(self):
        import historial
        r = historial._con_el_nombre_de_hoy(
            {"que": "Análisis extendido · Avatar (2022)"})
        self.assertEqual(r["que"], "Análisis RPU/Luz MKV · Avatar (2022)")

    def test_y_tambien_el_nombre_intermedio(self):
        """Hubo un despliegue con «Análisis RPU/Luz» sin el «MKV»."""
        import historial
        r = historial._con_el_nombre_de_hoy({"que": "Análisis RPU/Luz · X"})
        self.assertEqual(r["que"], "Análisis RPU/Luz MKV · X")

    def test_lo_demas_no_se_toca(self):
        import historial
        for que in ("Conversión a MKV · Peli", "Upgrade CMv4.0 · Otra"):
            self.assertEqual(historial._con_el_nombre_de_hoy({"que": que})["que"], que)

    def test_un_registro_sin_que_no_revienta(self):
        import historial
        self.assertEqual(historial._con_el_nombre_de_hoy({"tipo": "rip"}),
                         {"tipo": "rip"})

    def test_no_se_muta_lo_que_se_acaba_de_leer(self):
        import historial
        original = {"que": "Análisis extendido · X"}
        historial._con_el_nombre_de_hoy(original)
        self.assertEqual(original["que"], "Análisis extendido · X")

