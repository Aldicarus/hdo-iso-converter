"""Las pestañas de proyecto de Tab 1: qué se dice al abrir, al cerrar y qué se suelta.

Tres fallos que compartían causa —código escrito mirando a Tab 3, donde el
proyecto tiene otros campos— y ninguno daba un error:

1. **El toast decía siempre "creado"**, incluso re-analizando un ISO ya abierto.
   La comprobación buscaba `p.subTab`, que es de Tab 3; en Tab 1 el objeto de
   proyecto nunca ha tenido ese campo, así que el `find` era siempre
   `undefined`.

2. **El botón "🔄 Restaurar del disco" salía con orígenes M2TS**, donde no hay
   MPLS del que restaurar capítulos. Mismo patrón: `p.subTabId === activeSubTabId`
   —otro campo de Tab 3— daba `undefined`, así que `_resetHasMpls` era siempre
   verdadero.

3. **Los dos Sortable del panel se quedaban vivos al cerrar la pestaña.** Se
   destruía `project.sortable`, que Tab 1 no asigna nunca, mientras
   `sortableAudio` y `sortableSubs` seguían con sus listeners sobre un DOM ya
   borrado.

Y lo que faltaba: **cerrar la pestaña de un rip en marcha no decía nada**.
No es destructivo (la cola vive en el backend y el log se persiste), pero el
usuario pierde la consola en vivo sin enterarse de que el trabajo continúa.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_tab1_pestanas_proyecto -v
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

from frontend_sources import (motor_i18n, argv_node, pintar_en,  # noqa: E402
                              html, js_completo, pieza_de)

NODE = shutil.which("node")
JS = js_completo()


def _fn(nombre: str) -> str:
    marca = f"function {nombre}("
    i = JS.index(marca)
    return JS[i:JS.index("\n}\n", i) + 3]


def _node(guion: str) -> dict:
    """El guion con el MOTOR de traducción delante, y el texto ya resuelto.

    El frontend llama a `tr('clave')` para todo lo que se lee, así que un
    arnés sin el motor muere con «tr is not defined» — y con el motor, las
    aserciones siguen viendo castellano, que es lo que ya esperaban. Va aquí y
    no en cada test por lo mismo que `sistema_de_iconos()`.
    """
    r = subprocess.run(argv_node(motor_i18n() + guion),
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(f"node falló:\n{r.stderr[:900]}")
    return pintar_en(json.loads(r.stdout.strip().splitlines()[-1]))


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElToastAlCrearOReanalizar(unittest.TestCase):
    """El texto tiene que distinguir un proyecto nuevo de uno re-analizado."""

    def _correr(self, abiertos: list) -> dict:
        """Evalúa la cola real de `_doAnalyzeSource`, la que decide el texto.

        Los dos extremos del recorte están en cualquier versión del bloque
        (`if (!session)` y `await loadSessions()`), a propósito: anclarlo al
        código nuevo haría que una mutación se detectara por reventar el
        `index()` en vez de por dar el toast equivocado.
        """
        cuerpo = JS[JS.index("async function _doAnalyzeSource("):]
        ini = cuerpo.index("  if (!session) {")
        bloque = cuerpo[ini:cuerpo.index("  await loadSessions();", ini)]
        guion = f"""
const openProjects = {json.dumps(abiertos)};
const session = {{ id: 's-nueva', mkv_name: 'Peli (2024).mkv' }};
const sourceName = 'peli.iso';
const abiertosVistos = [];
globalThis.openProject = s => abiertosVistos.push(s.id);
globalThis.escHtml = t => t;
let toast = null;
globalThis.showToast = (t) => {{ toast = t; }};
(() => {{
{bloque}
}})();
console.log(JSON.stringify({{ toast, abiertosVistos }}));
"""
        return _node(guion)

    def test_proyecto_nuevo_dice_creado(self):
        r = self._correr([{"id": "p1", "sessionId": "otra"}])
        self.assertIn("Proyecto creado", r["toast"])
        self.assertIn("Peli (2024).mkv", r["toast"])

    def test_reanalizar_uno_abierto_dice_reanalizado(self):
        r = self._correr([{"id": "p1", "sessionId": "s-nueva"}])
        self.assertIn("Proyecto re-analizado", r["toast"])

    def test_reanalizar_refresca_la_pestana_existente(self):
        """`openProject` es idempotente: se llama en los dos casos.

        La versión vieja NO lo llamaba al re-analizar (creía que la rama del
        `find` ya había refrescado la sesión), así que el panel se quedaba con
        el análisis anterior en pantalla.
        """
        r = self._correr([{"id": "p1", "sessionId": "s-nueva"}])
        self.assertEqual(r["abiertosVistos"], ["s-nueva"])

    def test_sin_nada_abierto_dice_creado(self):
        r = self._correr([])
        self.assertIn("Proyecto creado", r["toast"])

    def test_el_campo_subtab_de_tab3_no_confunde(self):
        """Un proyecto con `subTab` pero de OTRA sesión no cuenta como abierto.

        Es el caso que la versión vieja acertaba por casualidad: buscaba por
        `p.subTab && p.session.id`, así que un objeto con esa forma pasaba el
        primer filtro. Con la búsqueda por `sessionId` da igual qué campos
        traiga.
        """
        r = self._correr([{"id": "p1", "sessionId": "otra", "subTab": True,
                           "session": {"id": "otra"}}])
        self.assertIn("Proyecto creado", r["toast"])


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElBotonDeRestaurarCapitulos(unittest.TestCase):
    """Solo con orígenes que tengan MPLS (iso / bdmv), nunca con m2ts."""

    def _hay_boton(self, source_type) -> bool:
        i = JS.index("  // `subTabId` es un campo de Tab 3")
        bloque = JS[i:JS.index("\n", JS.index("const _resetHasMpls", i))]
        guion = f"""
const sesion = {{ id: 's1', source_type: {json.dumps(source_type)} }};
globalThis.getActiveProject = () => ({{ id: 'p1', session: sesion }});
{bloque}
console.log(JSON.stringify({{ hay: !!_resetHasMpls }}));
"""
        return _node(guion)["hay"]

    def test_iso_lo_muestra(self):
        self.assertTrue(self._hay_boton("iso"))

    def test_bdmv_lo_muestra(self):
        self.assertTrue(self._hay_boton("bdmv_folder"))

    def test_m2ts_no_lo_muestra(self):
        """El fallo de verdad: sin MPLS no hay nada del disco que restaurar."""
        self.assertFalse(self._hay_boton("m2ts"))

    def test_sesion_legacy_sin_source_type_lo_muestra(self):
        """`source_type` tiene default `iso` desde v2.6; las sesiones anteriores
        no lo traen y son todas de ISO. El `|| 'iso'` de la expresión es ese
        default, no un descuido — quitarlo escondería el botón en el parque
        viejo."""
        self.assertTrue(self._hay_boton(None))


@unittest.skipIf(NODE is None, "node no está instalado")
class TestCerrarLaPestana(unittest.TestCase):
    """Qué se suelta y qué se le dice al usuario."""

    def _cerrar(self, proyecto: dict) -> dict:
        """Ejecuta el cuerpo REAL de `_doCloseProject` hasta que toca el DOM.

        Reimplementarlo aquí no valdría: quitar de producción la llamada al
        aviso dejaría el test en verde, porque estaría probando la copia del
        test. Los dos extremos del recorte (`const project = openProjects[idx]`
        y el primer `document.getElementById`) existen en cualquier versión.
        """
        cuerpo = JS[JS.index("function _doCloseProject("):]
        ini = cuerpo.index("  const project = openProjects[idx];")
        bloque = cuerpo[ini:cuerpo.index("  document.getElementById(", ini)]
        guion = f"""
{_fn('_avisarSiCerramosUnRipEnMarcha')}
const destruidos = [];
const openProjects = [{json.dumps(proyecto)}];
const idx = 0;
for (const k of ['sortable', 'sortableAudio', 'sortableSubs']) {{
  if (openProjects[0][k]) openProjects[0][k] = {{ destroy: () => destruidos.push(k) }};
}}
let toast = null;
globalThis.showToast = (t) => {{ toast = t; }};
{bloque}
console.log(JSON.stringify({{ destruidos, toast, sueltos: {{
  sortableAudio: project.sortableAudio, sortableSubs: project.sortableSubs }} }}));
"""
        return _node(guion)

    def _base(self, **extra) -> dict:
        p = {"id": "p1", "name": "Peli (2024)", "session": {"status": "pending"},
             "sortableAudio": True, "sortableSubs": True}
        p.update(extra)
        return p

    def test_destruye_los_dos_sortable_reales(self):
        r = self._cerrar(self._base())
        self.assertEqual(sorted(r["destruidos"]), ["sortableAudio", "sortableSubs"])

    def test_suelta_las_referencias(self):
        """Sin ponerlas a null, el objeto retiene el Sortable aunque se saque
        de `openProjects` — y con él el nodo del DOM que ya no está."""
        r = self._cerrar(self._base())
        self.assertIsNone(r["sueltos"]["sortableAudio"])
        self.assertIsNone(r["sueltos"]["sortableSubs"])

    def test_sin_sortable_no_revienta(self):
        r = self._cerrar(self._base(sortableAudio=None, sortableSubs=None))
        self.assertEqual(r["destruidos"], [])

    def test_rip_en_curso_avisa(self):
        r = self._cerrar(self._base(session={"status": "running"}))
        self.assertIsNotNone(r["toast"])
        self.assertIn("Peli (2024)", r["toast"])
        self.assertIn("Cola", r["toast"])

    def test_encolado_avisa(self):
        r = self._cerrar(self._base(session={"status": "queued"}))
        self.assertIn("en la cola", r["toast"])

    def test_pendiente_no_avisa(self):
        self.assertIsNone(self._cerrar(self._base())["toast"])

    def test_terminado_no_avisa(self):
        self.assertIsNone(self._cerrar(self._base(session={"status": "done"}))["toast"])

    def test_sin_sesion_no_revienta(self):
        self.assertIsNone(self._cerrar(self._base(session=None))["toast"])


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElBannerDeUnaEjecucionCancelada(unittest.TestCase):
    """Cancelar un rip no dejaba rastro en la ficha.

    El `except` del pipeline devuelve `status` a `pending`, limpia
    `error_message` y **no** apila un `ExecutionRecord` —el proyecto queda
    listo para relanzarse— así que ni el banner (que solo miraba
    running/queued) ni la tabla de ejecuciones (que no tiene fila) lo decían.
    El hecho más importante que le había pasado al proyecto vivía solo en el
    log.
    """

    def _banner(self, sesion: dict) -> dict:
        from frontend_sources import sistema_de_iconos
        guion = f"""
{sistema_de_iconos()}
{_fn('_situacionDeSesion')}
{_fn('renderExecResultBanner')}
const nodos = {{}};
for (const id of ['exec-result-banner', 'exec-result-icon',
                  'exec-result-title', 'exec-result-detail',
                  'exec-result-actions']) {{
  nodos[id] = {{ style: {{}}, className: '', innerHTML: '', textContent: '' }};
}}
globalThis.E = (id) => nodos[id];
globalThis.escHtml = (s) => String(s);
renderExecResultBanner({json.dumps(sesion)});
console.log(JSON.stringify({{
  visible: nodos['exec-result-banner'].style.display !== 'none',
  clase: nodos['exec-result-banner'].className,
  titulo: nodos['exec-result-title'].textContent,
  detalle: nodos['exec-result-detail'].textContent,
  acciones: nodos['exec-result-actions'].innerHTML,
}}));
"""
        return _node(guion)

    #: cómo llega una sesión cancelada del servidor
    CANCELADA = {"id": "p1", "status": "pending",
                 "last_cancelled_at": "2026-09-19T10:00:00Z",
                 "relato": {"situacion": "cancelado",
                            "situacion_rotulo": "Cancelado",
                            "porque": "La ejecución se canceló."}}

    def test_sale_y_dice_que_lo_paraste(self):
        b = self._banner(self.CANCELADA)
        self.assertTrue(b["visible"])
        # El rótulo y el motivo los escribe el SERVIDOR: aquí se comprueba
        # que el banner los pinta, no cómo están redactados.
        self.assertEqual(b["titulo"], self.CANCELADA["relato"]["situacion_rotulo"])
        self.assertEqual(b["detalle"], self.CANCELADA["relato"]["porque"])

    def test_en_ambar_y_no_en_azul(self):
        """El azul es el de «esto está pasando»; esto ya pasó y no va a
        seguir."""
        self.assertIn("warning", self._banner(self.CANCELADA)["clase"])

    def test_sin_botones_porque_no_hay_nada_que_seguir(self):
        """«Ver progreso» y «Cancelar» son de un trabajo vivo."""
        self.assertEqual(self._banner(self.CANCELADA)["acciones"].strip(), "")

    def test_una_que_nunca_se_lanzo_no_saca_banner(self):
        self.assertFalse(self._banner(
            {"id": "p1", "status": "pending",
             "relato": {"situacion": "preparando", "situacion_rotulo": "Sin ejecutar",
                        "porque": ""}})["visible"])

    def test_y_la_que_corre_sigue_en_azul_con_sus_botones(self):
        b = self._banner({"id": "p1", "status": "running",
                          "relato": {"situacion": "en_marcha",
                                     "situacion_rotulo": "En curso", "porque": ""}})
        self.assertTrue(b["visible"])
        self.assertIn("info", b["clase"])
        self.assertIn("cancelRunningSession", b["acciones"])


class TestNoQuedanCamposDeTab3EnTab1(unittest.TestCase):
    """Guard: los campos que Tab 1 no tiene no pueden volver a consultarse.

    Los tres bugs de arriba eran la misma equivocación repetida. Un `find` que
    devuelve `undefined` no falla: se lee como "no está abierto" o "no hay
    proyecto", y el síntoma aparece a tres capas de distancia.
    """

    def test_tab1_no_busca_por_subtab_ni_subtabid(self):
        """La pieza se resuelve por la función, no por el nombre del fichero:
        `p.subTabId` SÍ es legítimo en `tab3.js`, así que esto no se puede
        comprobar sobre el JS concatenado."""
        for funcion in ("_doAnalyzeSource", "renderChapters"):
            nombre, src = pieza_de(funcion)
            for campo in ("p.subTab ", "p.subTabId"):
                self.assertNotIn(
                    campo, src,
                    f"`{campo}` es de Tab 3 y aparece en {nombre} (que declara "
                    f"`{funcion}`); en Tab 1 el proyecto se identifica por "
                    "`id`/`sessionId` y esa búsqueda devolvería siempre undefined")

    def test_no_queda_el_sortable_inexistente(self):
        self.assertNotIn("if (project.sortable)", pieza_de("_doCloseProject")[1])

    def test_no_queda_appendconsole(self):
        """Escribía en `console-wrap`, un elemento que no existe en el HTML:
        los errores de red con `silent` no dejaban rastro en ninguna parte."""
        self.assertNotIn("appendConsole", JS)
        self.assertNotIn("console-wrap", html())


if __name__ == "__main__":
    unittest.main()
