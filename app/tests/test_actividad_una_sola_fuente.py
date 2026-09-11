"""«Qué está pasando» se pregunta una vez, y el 409 de admisión no es un error.

Cada consumidor preguntaba por su cuenta a los endpoints que le sonaban —el
punto verde a tres, el aviso de fin de trabajo a dos— y ninguno sabía de los
demás. El resultado es que cada uno tenía su propia idea de qué estaba pasando
y **las tres se equivocaban de forma distinta**: el punto de Tab 2 se quedaba
apagado durante un análisis extendido de diez minutos y su tooltip hablaba de
"copia/edición", que es solo la mitad de lo que ese punto significa.

El otro frente es el 409. La app lo usa para cinco cosas más —el MKV de salida
ya existe, hay una fase en curso, el gate de sync no pasa— así que el estado no
lo distingue, y el frontend lo pintaba todo en rojo como «Error:». Pero un 409
de admisión no es un error: es «ahora no, espera», el texto ya dice qué bloquea
y en qué pestaña, y merece quedarse en pantalla más de 3,5 s porque es una
frase entera.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_actividad_una_sola_fuente -v
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

from api_harness import ApiTestCase  # noqa: E402
from frontend_sources import sistema_de_iconos, html, js_completo  # noqa: E402
import workload  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()


def _fn(nombre: str) -> str:
    """El fuente de la función, DESDE EL PRINCIPIO DE SU LÍNEA.

    Cortar en `function` se come el `async` de las declaraciones asíncronas, y
    lo que sale es una función normal con `await` dentro: SyntaxError en node.
    """
    i = JS.index(f"function {nombre}(")
    return JS[JS.rindex("\n", 0, i) + 1:JS.index("\n}\n", i) + 3]


def _bloque(marca: str) -> str:
    i = JS.index(marca)
    return JS[i:JS.index("\n};\n", i) + 4]


def _iconos() -> str:
    """Lo que hace falta para que el marcado de los iconos se pueda evaluar.

    Lo entrega `frontend_sources`: enumerar aquí las piezas del sistema hacía
    que cada vez que gana una —el catálogo `GLIFOS`, la función `icono`— este
    arnés se rompiera con un `ReferenceError`.
    """
    return sistema_de_iconos()


def _node(guion: str) -> dict:
    r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(f"node falló:\n{r.stderr[:900]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


# El DOM mínimo: los tres puntos y nada más.
_SANDBOX = """
const _els = {};
for (const n of [1, 2, 3]) _els[`tab-running-dot-${n}`] = { style: {}, dataset: {} };
globalThis.document = {
  hidden: false,
  getElementById: id => _els[id] || null,
};
globalThis.queueState = QUEUE_STATE;
let _peticiones = [];
globalThis.apiFetch = async (url) => { _peticiones.push(url); return RESPUESTA; };
FUNCIONES
"""


def _sandbox(*funciones, respuesta, queue_state=None):
    return (_SANDBOX
            .replace("QUEUE_STATE", json.dumps(queue_state or {}))
            .replace("RESPUESTA", json.dumps(respuesta))
            .replace("FUNCIONES", "\n".join(_fn(f) for f in funciones)))


def _actividad(*trabajos) -> dict:
    return {"ocupado": any(t.get("bloquea") for t in trabajos),
            "trabajos": list(trabajos)}


def _trabajo(tab_id, que, segundos=30, clase="diferido"):
    return {"clave": f"k-{que}", "tab": "X", "tab_id": tab_id, "que": que,
            "clase": clase, "bloquea": clase == "diferido",
            "segundos": segundos, "descripcion": f"X — {que}"}


@unittest.skipIf(NODE is None, "node no está instalado")
class TestLaColumnaDeTrabajo(unittest.TestCase):
    """Sustituye a los puntos verdes de las pestañas, y hace más.

    Los puntos decían «hay algo» y nada más; la columna dice qué, en qué fase,
    cuánto lleva y cuánto queda, y es la misma en las tres pestañas porque la
    cola es una. Con ella puesta, los tres puntos y su poller se retiraron.
    """

    def _correr(self, respuesta, extra="") -> dict:
        guion = f"""
let workbarEstado = {{ activo: null, cola: [], interactivo: [], recientes: [] }};
const _els = {{}};
for (const id of ['workbar-body', 'workbar-count', 'workbar-toggle', 'workbar-historial']) {{
  _els[id] = {{ style: {{}}, dataset: {{}}, classList: {{
    _v: new Set(),
    toggle(c, on) {{ on ? this._v.add(c) : this._v.delete(c); }},
    has(c) {{ return this._v.has(c); }} }} }};
}}
globalThis.document = {{ hidden: false,
  getElementById: id => _els[id] || null,
  querySelector: sel => (globalThis._listaCola || null) }};
let _peticiones = [];
globalThis.apiFetch = async (url) => {{ _peticiones.push(url); return {json.dumps(None)} ?? RESP; }};
globalThis.escHtml = t => String(t);
{_iconos()}
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
{_fn('_workbarActivoHTML')}
{_fn('_workbarConsultasHTML')}
{_fn('_workbarListaHTML')}
const _workbarOyentes = [];
let _workbarUltimaFirma = null;
{_fn('_workbarFirma')}
{_fn('_instalarReordenDeCola')}
{_fn('normalizeSearch')}
let _workbarFiltroTab = 'all';
{_fn('_workbarBusqueda')}
{_fn('_workbarFiltrando')}
{_fn('_workbarPasaFiltro')}
let _workbarSeleccion = null;
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
let _workbarUltimaRevHistorial = null;
let _workbarTopeHistorial = 25;
globalThis._workbarCargarHistorial = async () => {{}};
{_fn('refrescarWorkbar')}
(async () => {{
  globalThis.apiFetch = async (url) => {{ _peticiones.push(url); return RESP; }};
  await refrescarWorkbar();
  {extra}
  console.log(JSON.stringify({{
    peticiones: _peticiones,
    html: (_els['workbar-body'].innerHTML || '') + (_els['workbar-historial'].innerHTML || ''),
    cuenta: _els['workbar-count'].textContent,
    conTrabajo: _els['workbar-toggle'].classList.has('con-trabajo'),
    estado: workbarEstado,
  }}));
}})();
""".replace("RESP", json.dumps(respuesta))
        return _node(guion)

    def test_un_refresco_es_UNA_peticion(self):
        r = self._correr({"activo": None, "cola": [], "interactivo": [],
                          "recientes": []})
        # Una sola, y **sin el historial**: ese va aparte y solo cuando algo
        # deja de estar en marcha. Con él dentro, el poll traía la lista
        # entera cada 2 s y su scroll saltaba al principio en cada vuelta.
        self.assertEqual(r["peticiones"], ["/api/trabajos?recientes=0"])

    def test_pinta_la_fase_el_porcentaje_y_lo_que_queda(self):
        r = self._correr({"activo": {
            "id": "p1", "tab": "cmv40", "tipo": "fase_cmv40",
            "que": "Fase C de Predator", "fase": "extract",
            "fase_label": "Extrayendo BL/EL", "fase_n": 3, "fases_total": 7,
            "pct": 40, "pct_medido": True, "segundos": 300,
            "eta_s": 450, "eta_fuente": "medido", "cancelable": True,
            "detalle": "cmv40"}, "cola": [], "interactivo": [], "recientes": []})
        self.assertIn("Fase C de Predator", r["html"])
        self.assertIn("Extrayendo BL/EL · 3 de 7", r["html"])
        self.assertIn("40%", r["html"])
        self.assertIn("Restante 7 min", r["html"])

    def test_sin_porcentaje_medido_la_barra_es_indeterminada(self):
        """La regla del proyecto: una cifra inventada con pinta de dato es
        peor que un hueco. `extract-rpu` escribe el RPU de golpe al cerrar y
        ese tramo NO es medible."""
        r = self._correr({"activo": {
            "id": "p1", "tab": "cmv40", "tipo": "fase_cmv40", "que": "Fase H",
            "fase": "validate", "fase_label": "Validando", "fase_n": 7,
            "fases_total": 7, "pct": None, "pct_medido": False,
            "segundos": 60, "eta_s": None, "eta_fuente": None,
            "cancelable": True}, "cola": [], "interactivo": [], "recientes": []})
        self.assertIn("indeterminada", r["html"])
        self.assertIn("Progreso no medible", r["html"])
        self.assertNotIn("%<", r["html"])

    def test_un_eta_de_modelo_se_marca_como_aproximado(self):
        r = self._correr({"activo": {
            "id": "s1", "tab": "rip", "tipo": "crear_serie", "que": "4 episodios",
            "fase": "pgs", "fase_label": "Episodio 2/4", "fase_n": 2,
            "fases_total": 4, "pct": 25, "pct_medido": True, "segundos": 120,
            "eta_s": 360, "eta_fuente": "modelo", "cancelable": True},
            "cola": [], "interactivo": [], "recientes": []})
        self.assertIn("(aprox.)", r["html"])

    def test_la_cola_sale_con_su_puesto(self):
        r = self._correr({"activo": None, "interactivo": [], "recientes": [],
                          "cola": [{"id": "a", "tab": "rip", "que": "rip de A",
                                    "posicion": 1},
                                   {"id": "b", "tab": "mkv", "que": "análisis de B",
                                    "posicion": 2}]})
        self.assertIn("rip de A", r["html"])
        self.assertIn("análisis de B", r["html"])
        self.assertEqual(r["cuenta"], "2")

    def test_lo_interactivo_sale_aparte(self):
        r = self._correr({"activo": None, "cola": [], "recientes": [],
                          "interactivo": [{"id": "k", "tab": "mkv",
                                           "que": "apertura de un MKV",
                                           "segundos": 12}]})
        self.assertIn("En segundo plano", r["html"])
        self.assertIn("apertura de un MKV", r["html"])

    def test_con_la_casa_libre_lo_dice_y_no_enciende_el_aviso(self):
        r = self._correr({"activo": None, "cola": [], "interactivo": [],
                          "recientes": []})
        self.assertIn("No hay nada en ejecución", r["html"])
        self.assertEqual(r["cuenta"], "0")
        self.assertFalse(r["conTrabajo"])

    def test_plegada_el_aviso_del_boton_dice_cuántos(self):
        """Es lo que permite retirar los puntos verdes: si al plegar la columna
        dejara de haber señal, cerrarla te dejaría a ciegas."""
        r = self._correr({"activo": None, "interactivo": [], "recientes": [],
                          "cola": [{"id": "a", "tab": "rip", "que": "rip de A",
                                    "posicion": 1}]})
        self.assertTrue(r["conTrabajo"])

    def test_un_fallo_de_red_conserva_el_ultimo_dato_bueno(self):
        """Vaciar la columna haría creer que el trabajo terminó."""
        guion_extra = "globalThis.apiFetch = async () => null; await refrescarWorkbar();"
        r = self._correr({"activo": None, "interactivo": [], "recientes": [],
                          "cola": [{"id": "a", "tab": "rip", "que": "rip de A",
                                    "posicion": 1}]}, extra=guion_extra)
        self.assertEqual(len(r["estado"]["cola"]), 1)


@unittest.skipIf(NODE is None, "node no está instalado")
class TestCadaPestanaMarcaLoSuyo(unittest.TestCase):
    """La columna dice lo que pasa en la aplicación; la lista de cada pestaña
    tiene que decir en CUÁL. Sin esto, un MKV con el análisis extendido
    esperando turno se veía igual que uno parado y se podía volver a pedir: el
    rechazo llegaba del backend, que es la peor forma de enterarse.

    El emparejamiento va por `sobre` —el identificador con el que la pestaña
    conoce el recurso— y NO por la clave del trabajo: la de un análisis
    extendido es su `audit_id`, que la pestaña no ha visto nunca.
    """

    def _mirar(self, estado, sobre) -> dict:
        guion = f"""
let workbarEstado = {json.dumps(estado)};
globalThis.escHtml = t => String(t);
{_iconos()}
{_fn('trabajoSobre')}
{_fn('insigniaDeTrabajo')}
console.log(JSON.stringify({{
  t: trabajoSobre({json.dumps(sobre)}),
  html: insigniaDeTrabajo({json.dumps(sobre)}),
}}));
"""
        return _node(guion)

    def test_el_activo_se_reconoce_por_sobre_y_no_por_la_clave(self):
        estado = {"activo": {"id": "aud-7f3", "sobre": "/mnt/output/Dune.mkv",
                             "que": "análisis extendido de Dune.mkv"},
                  "cola": []}
        r = self._mirar(estado, "/mnt/output/Dune.mkv")
        self.assertEqual(r["t"]["estado"], "corriendo")
        self.assertIn("En curso", r["html"])
        self.assertIn("icono-girando", r["html"], "la rueda tiene que girar")
        # Y la clave, que es lo que la pestaña NO conoce, no cuela por error.
        self.assertIsNone(self._mirar(estado, "aud-7f3")["t"])

    def test_en_cola_dice_el_puesto(self):
        estado = {"activo": None,
                  "cola": [{"id": "a", "sobre": "otro", "posicion": 1},
                           {"id": "b", "sobre": "/mnt/output/Dune.mkv",
                            "que": "análisis extendido de Dune.mkv",
                            "posicion": 2}]}
        r = self._mirar(estado, "/mnt/output/Dune.mkv")
        self.assertEqual(r["t"]["estado"], "en_cola")
        self.assertEqual(r["t"]["posicion"], 2)
        self.assertIn("2ª", r["html"])

    def test_sin_trabajo_no_hay_insignia(self):
        r = self._mirar({"activo": None, "cola": []}, "/mnt/output/Dune.mkv")
        self.assertIsNone(r["t"])
        self.assertEqual(r["html"], "")

    def test_un_recurso_vacio_no_empareja_con_lo_que_no_tiene_sobre(self):
        """Un trabajo sin `sobre` cae a su id; preguntar por '' no puede
        devolverlo."""
        r = self._mirar({"activo": {"id": "", "que": "x"}, "cola": []}, "")
        self.assertIsNone(r["t"])


class TestElContratoDiceSobreQueActua(unittest.TestCase):
    """`sobre` viaja en `/api/trabajos` para el activo y para la cola."""

    def test_para_los_tres_que_usan_su_clave_es_la_clave(self):
        import queue_manager as qm
        import trabajos
        for tipo in (qm.TIPO_RIP, qm.TIPO_FASE_CMV40, qm.TIPO_SERIE):
            t = qm.TrabajoEnCola(tab="rip", tipo=tipo, clave="proj_1")
            self.assertEqual(trabajos.progreso_de(t)["sobre"], "proj_1")

    def test_y_el_analisis_extendido_lo_dice_aparte(self):
        import queue_manager as qm
        import trabajos
        t = qm.TrabajoEnCola(tab="mkv", tipo=qm.TIPO_ANALISIS_EXTENDIDO,
                             clave="aud-7f3", sobre="/mnt/output/Dune.mkv")
        p = trabajos.progreso_de(t)
        self.assertEqual(p["id"], "aud-7f3")
        self.assertEqual(p["sobre"], "/mnt/output/Dune.mkv")

    def test_una_entrada_vieja_del_fichero_de_cola_no_se_queda_sin_sobre(self):
        import queue_manager as qm
        t = qm.TrabajoEnCola.de_json(
            {"tab": "rip", "tipo": "rip", "clave": "peli_2024_1", "que": "x"})
        self.assertEqual(t.sobre, "peli_2024_1")


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElRelojLoLLevaElNavegador(unittest.TestCase):
    """El transcurrido venía del contrato y solo cambiaba con el poll —cada
    2 s en la columna, 1,5 s en el modal—, así que por debajo del minuto se
    veía saltar de dos en dos segundos. La timeline de CMv4.0 no tenía ese
    problema porque su reloj lo lleva un tick local de 1 s; esto es lo mismo
    para el resto.
    """

    def _correr(self, extra="") -> dict:
        guion = f"""
globalThis.escHtml = t => String(t);
globalThis.window = {{}};
let _cb = null;
globalThis.setInterval = (fn) => {{ _cb = fn; return 1; }};
let _els = [];
globalThis.document = {{ querySelectorAll: () => _els }};
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
{_fn('_relojHTML')}
{_fn('_arrancarRelojes')}
const html = _relojHTML(5, 'lleva ');
// El elemento que el tick va a tocar, con el ancla que acaba de emitirse.
const desde = /data-desde="(\\d+)"/.exec(html)[1];
_els = [{{ dataset: {{ desde, pre: 'lleva ', post: '' }}, textContent: '' }}];
_arrancarRelojes();
{extra}
console.log(JSON.stringify({{ html, texto: _els[0].textContent,
                             hayTick: !!_cb }}));
"""
        return _node(guion)

    def test_la_columna_lo_USA_para_el_trabajo_activo(self):
        """No basta con que el helper sepa hacerlo: hay que emitirlo. Sin
        esto, la tarjeta seguiría pintando el número del servidor y saltando
        de dos en dos segundos."""
        guion = f"""
globalThis.escHtml = t => String(t);
{_iconos()}
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
let _workbarSeleccion = null;
{_fn('_workbarMini')}
{_fn('_workbarDescripcion')}
{_fn('_workbarPips')}
{_fn('_workbarChips')}
{_fn('_workbarTarjeta')}
{_fn('_workbarActivoHTML')}
{_fn('_workbarConsultasHTML')}
console.log(JSON.stringify({{ html: _workbarActivoHTML(
  {{ tipo: 'rip', que: 'x', fase_label: 'F', fase_n: 1, fases_total: 4,
     pct: 10, pct_medido: true, segundos: 7, eta_s: 240,
     eta_fuente: 'medido', cancelable: true }}) }}));
"""
        h = _node(guion)["html"]
        self.assertIn('class="workbar-reloj', h)
        self.assertIn("data-desde=", h)
        # Y el resto de la línea sigue ahí, en el sufijo del reloj.
        self.assertIn("Restante 4 min", h)

    def test_tambien_cuando_no_hay_ETA(self):
        """La otra rama: un trabajo sin porcentaje medido no tiene restante,
        pero el transcurrido corre igual."""
        guion = f"""
globalThis.escHtml = t => String(t);
{_iconos()}
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
let _workbarSeleccion = null;
{_fn('_workbarMini')}
{_fn('_workbarDescripcion')}
{_fn('_workbarPips')}
{_fn('_workbarChips')}
{_fn('_workbarTarjeta')}
{_fn('_workbarActivoHTML')}
{_fn('_workbarConsultasHTML')}
console.log(JSON.stringify({{ html: _workbarActivoHTML(
  {{ tipo: 'rip', que: 'x', fase_label: 'F', fase_n: 1, fases_total: 4,
     pct: null, pct_medido: false, segundos: 7, eta_s: null,
     cancelable: true }}) }}));
"""
        h = _node(guion)["html"]
        self.assertIn('class="workbar-reloj', h)
        self.assertIn("Lleva 7 s", h)

    def test_el_ancla_sale_del_dato_del_servidor(self):
        r = self._correr()
        self.assertIn('class="workbar-reloj', r["html"])
        self.assertIn("lleva 5 s", r["html"])
        self.assertIn("data-desde=", r["html"])
        self.assertTrue(r["hayTick"])

    def test_el_tick_avanza_sin_esperar_al_poll(self):
        """Lo que arregla el salto: entre dos respuestas del servidor el
        navegador sigue contando."""
        r = self._correr("""
// Tres segundos después, sin que haya llegado nada del servidor.
const real = Date.now;
Date.now = () => real() + 3000;
_cb();
Date.now = real;
""")
        self.assertEqual(r["texto"], "lleva 8 s")

    def test_el_prefijo_y_el_sufijo_sobreviven_al_tick(self):
        """El tick reescribe el nodo entero: sin conservarlos se comía el
        «Restante 4 min» que va al lado."""
        guion = f"""
globalThis.escHtml = t => String(t);
globalThis.window = {{}};
let _cb = null;
globalThis.setInterval = (fn) => {{ _cb = fn; return 1; }};
let _els = [{{ dataset: {{ desde: String(Date.now() - 7000), pre: '',
                          post: ' · Restante 4 min' }}, textContent: '' }}];
globalThis.document = {{ querySelectorAll: () => _els }};
{_fn('_workbarTiempo')}
{_fn('_relojHTML')}
{_fn('_arrancarRelojes')}
_arrancarRelojes();
_cb();
console.log(JSON.stringify({{ texto: _els[0].textContent }}));
"""
        self.assertEqual(_node(guion)["texto"], "7 s · Restante 4 min")


class TestNoQuedanLectoresSueltosDeActividad(unittest.TestCase):
    """El punto entero: una sola fuente para «qué está pasando»."""

    def test_todo_el_frontend_pregunta_por_api_trabajos(self):
        apariciones = [l for l in JS.splitlines() if "'/api/activity'" in l]
        self.assertEqual(apariciones, [],
                         "`/api/activity` es el registro crudo de workload; la "
                         "UI va por `/api/trabajos`, que es la vista unificada: "
                         f"{apariciones}")

    def test_ya_no_quedan_los_puntos_verdes_de_las_pestanas(self):
        """Los sustituyó la columna, que dice estrictamente más."""
        self.assertNotIn("tab-running-dot", JS)
        self.assertNotIn("tab-running-dot", html())
        self.assertNotIn("_refreshTabRunningDots", JS)


class TestElBackendYaNoRechazaPorAdmision(ApiTestCase):
    """El 409 de admisión existió entre el bloque 2 y el 3, y ya no.

    Lo sustituyó la cola única: lo diferido espera turno en vez de que se le
    diga que no, y lo interactivo nunca se rechazó. Con el último llamador
    fuera, `exigir_libre`, `motivo_409` y la cabecera `X-Trabajo-En-Curso`
    quedaron sin producir nada, así que se borraron — con ellos también la
    rama de `apiFetch` que los pintaba en ámbar.

    Este test es el que evita que vuelvan a medias: un `exigir_libre` nuevo
    sin la cabecera daría un 409 rojo de «Error:», que es justo lo que el
    bloque 2 arregló.
    """

    def setUp(self):
        super().setUp()
        workload.limpiar()
        self.addCleanup(workload.limpiar)

    def test_encolar_un_rip_con_otra_pestana_ocupada_funciona(self):
        (self.isos_dir / "Peli (2024).iso").write_bytes(b"x" * 4096)
        sid = self.crear_sesion_tab1()
        workload.registrar("otro", workload.TAB_CMV40, "Fase C de Predator")
        r = self.client.post(f"/api/sessions/{sid}/execute")
        self.assertEqual(r.status_code, 200, r.text)

    def test_no_queda_maquinaria_de_rechazo(self):
        self.assertFalse(hasattr(workload, "exigir_libre"))
        self.assertFalse(hasattr(workload, "motivo_409"))
        self.assertFalse(hasattr(workload, "CABECERA_OCUPADO"))
        self.assertNotIn("X-Trabajo-En-Curso", js_completo())


class TestLosTooltiposDeArranqueNoMienten(unittest.TestCase):
    """Lo único que sobrevive de los puntos verdes: el HTML no puede afirmar
    algo que la app ya no hace."""

    def test_no_queda_el_literal_desfasado_de_tab_2(self):
        self.assertNotIn("Hay una copia/edición en curso", html())


if __name__ == "__main__":
    unittest.main()
