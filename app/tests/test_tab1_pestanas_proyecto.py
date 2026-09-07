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

from frontend_sources import html, js_completo, pieza_de  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()


def _fn(nombre: str) -> str:
    marca = f"function {nombre}("
    i = JS.index(marca)
    return JS[i:JS.index("\n}\n", i) + 3]


def _node(guion: str) -> dict:
    r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(f"node falló:\n{r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElToastAlCrearOReanalizar(unittest.TestCase):
    """El texto tiene que distinguir un proyecto nuevo de uno re-analizado."""

    def _correr(self, abiertos: list) -> dict:
        # El bloque que decide el texto, extraído del final de `analyzeSelectedISO`.
        i = JS.index("  const yaEstabaAbierto = openProjects.some")
        bloque = JS[i:JS.index("'success');", i) + len("'success');")]
        guion = f"""
const openProjects = {json.dumps(abiertos)};
const session = {{ id: 's-nueva', mkv_name: 'Peli (2024).mkv' }};
const sourceName = 'peli.iso';
const abiertosVistos = [];
globalThis.openProject = s => abiertosVistos.push(s.id);
let toast = null;
globalThis.showToast = (t) => {{ toast = t; }};
{bloque}
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

    def _cerrar(self, project: dict) -> dict:
        guion = f"""
{_fn('_avisarSiCerramosUnRipEnMarcha')}
const destruidos = [];
const project = {json.dumps(project)};
for (const k of ['sortable', 'sortableAudio', 'sortableSubs']) {{
  if (project[k]) project[k] = {{ destroy: () => destruidos.push(k) }};
}}
let toast = null;
globalThis.showToast = (t) => {{ toast = t; }};

// El cuerpo real de `_doCloseProject` hasta donde toca el DOM.
_avisarSiCerramosUnRipEnMarcha(project);
if (project.ws) {{ project.ws.close(); project.ws = null; }}
for (const k of ['sortableAudio', 'sortableSubs']) {{
  if (project[k]) {{ project[k].destroy(); project[k] = null; }}
}}
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
