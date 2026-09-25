"""La lista de bins del repo se pide una vez, no en cada repintado.

El panel de un proyecto CMv4.0 se repinta cada pocos segundos, y la carga de
la lista de bins del repo DoviTools colgaba de ese repintado: cada vuelta
borraba la lista, ponía «Buscando en Drive…» y volvía a pedirla. Antes no se
notaba porque la Fase B activa duraba lo que tardaba el auto-pipeline en
pasar por ella; desde que un proyecto puede quedarse ahí esperando a que el
usuario escoja bin, el parpadeo es continuo —y la petición también—.
Reportado el 2026-09-25.

Se pide **una vez por fichero de origen**. El botón «Refrescar» fuerza;
volver a la pestaña no, que sería el mismo parpadeo con otro disparador. Y si
la petición falla se olvida la marca, para que el siguiente intento lo vuelva
a pedir en vez de dejar el error puesto para siempre.

También se fija que **tras «Cambiar target» la vista va a la Fase B**. El
scroll es del contenedor de la pestaña, así que al repintar se quedaba donde
estaba —el final del panel— y había que buscar la card a mano con el toast
puesto. Como la app ya lleva allí, ese toast dejó de mandar buscarla.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_repo_no_parpadea -v
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

from frontend_sources import argv_node, js_completo, motor_i18n  # noqa: E402

NODE = shutil.which("node")


def _fn(nombre: str) -> str:
    js = js_completo()
    for marca in (f"\nfunction {nombre}(", f"\nasync function {nombre}("):
        i = js.find(marca)
        if i != -1:
            return js[i + 1:js.index("\n}\n", i + 1) + 3]
    raise AssertionError(f"no se encuentra `{nombre}`")


# El arnés: la función real con un DOM y un `fetch` de mentira, contando
# CUÁNTAS veces sale a la red.
_DRIVER = """
'use strict';
const nodos = {};
const nodo = () => ({ innerHTML: '', textContent: '' });
globalThis.document = {
  getElementById: id => (nodos[id] = nodos[id] || nodo()),
};
let peticiones = 0;
globalThis.apiFetch = async () => {
  peticiones++;
  return { drive_configured: true, candidates: [], total: 0 };
};
globalThis.openCMv40Projects = [];
globalThis.escHtml = s => String(s == null ? '' : s);
globalThis._cmv40PanelRepoReqIds = {};
globalThis._cmv40RenderRepoCandidates = () => {};

__CUERPO__

const proyecto = { id: 'p1', session: { source_mkv_path: '/mnt/output/Drive (2011).mkv' } };
openCMv40Projects.push(proyecto);

async function medir(nombre, fn) {
  peticiones = 0;
  await fn();
  return [nombre, peticiones];
}

(async () => {
  const out = {};
  // Cinco repintados seguidos, que es lo que hace el poller.
  out.cincoRepintados = (await medir('x', async () => {
    for (let i = 0; i < 5; i++) await _cmv40LoadRepoForPanel('p1');
  }))[1];
  // El botón «Refrescar» sí vuelve a pedirla.
  out.refrescar = (await medir('x', async () => {
    await _cmv40LoadRepoForPanel('p1', true);
  }))[1];
  // Otro MKV es otra búsqueda.
  out.otroFichero = (await medir('x', async () => {
    proyecto.session.source_mkv_path = '/mnt/output/Otra (2020).mkv';
    await _cmv40LoadRepoForPanel('p1');
  }))[1];
  // Si la petición falla, el siguiente repintado lo reintenta.
  const bueno = globalThis.apiFetch;
  globalThis.apiFetch = async () => { peticiones++; return null; };
  await _cmv40LoadRepoForPanel('p1', true);
  globalThis.apiFetch = bueno;
  out.trasFallar = (await medir('x', async () => {
    await _cmv40LoadRepoForPanel('p1');
  }))[1];
  process.stdout.write(JSON.stringify(out));
})();
"""


@unittest.skipUnless(NODE, "node no disponible")
class TestElRepoSePideUnaVez(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        guion = motor_i18n() + "\n" + _DRIVER.replace(
            "__CUERPO__", _fn("_cmv40LoadRepoForPanel"))
        r = subprocess.run(argv_node(guion), capture_output=True, text=True,
                           timeout=60)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        cls.m = json.loads(r.stdout)

    def test_cinco_repintados_una_sola_peticion(self):
        # Era una por vuelta del poller, cada ~4 s, indefinidamente.
        self.assertEqual(self.m["cincoRepintados"], 1)

    def test_el_boton_refrescar_si_la_vuelve_a_pedir(self):
        self.assertEqual(self.m["refrescar"], 1)

    def test_otro_mkv_es_otra_busqueda(self):
        # La clave es el fichero, no el proyecto: si cambia, la lista de
        # candidatos ya no vale.
        self.assertEqual(self.m["otroFichero"], 1)

    def test_si_falla_el_siguiente_intento_lo_pide(self):
        # Sin esto, un fallo de red dejaba el error puesto hasta recargar.
        self.assertEqual(self.m["trasFallar"], 1)


class TestLaVistaVaALaFaseB(unittest.TestCase):
    """Sin Chrome: lo que se comprueba es a QUÉ card se le pide el scroll."""

    @unittest.skipUnless(NODE, "node no disponible")
    def test_cambiar_target_lleva_a_la_card_de_la_fase_b(self):
        guion = motor_i18n() + "\n" + "\n".join([
            "'use strict';",
            "let pedido = null;",
            "globalThis.document = { querySelector: sel => ({",
            "  scrollIntoView: () => { pedido = sel; } }) };",
            _fn("_cmv40IrALaFase"),
            "_cmv40IrALaFase('p1', 'B');",
            "process.stdout.write(JSON.stringify({pedido}));",
        ])
        r = subprocess.run(argv_node(guion), capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        sel = json.loads(r.stdout)["pedido"]
        self.assertIn('data-fase-key="B"', sel)
        self.assertIn("cmv40-panel-p1", sel)

    def test_el_toast_ya_no_manda_buscar_la_card(self):
        # Si la app lleva allí, decirlo describe un trabajo que no hay que
        # hacer. Se mira el catálogo, que es donde vive el texto.
        cat = json.loads((APP_DIR / "static" / "i18n" / "es.json")
                         .read_text(encoding="utf-8"))
        self.assertNotIn("tab3.listo_para_escoger_otro_target_abre", cat)
        self.assertNotIn("Fase B", cat["tab3.listo_para_escoger_otro_target"])

    @unittest.skipUnless(NODE, "node no disponible")
    def test_y_el_handler_lo_pide_de_verdad(self):
        """Ejecutando `_cmv40ChangeTarget`, no leyendo su fuente.

        Una función suelta no sirve de nada si nadie la llama, y un
        `assertIn` sobre el código pasaría en verde con la llamada dentro de
        una rama muerta.
        """
        guion = motor_i18n() + "\n" + "\n".join([
            "'use strict';",
            "const hechos = [];",
            "globalThis.document = { querySelector: sel => ({",
            "  scrollIntoView: () => hechos.push('scroll:' + sel) }) };",
            "globalThis.apiFetch = async () => ({id: 'p1', phase: 'source_analyzed'});",
            "globalThis.showToast = (t) => hechos.push('toast');",
            "globalThis._cmv40AssignSession = (p, d) => { p.session = d; };",
            "globalThis._updateCMv40Panel = () => hechos.push('repintado');",
            "globalThis.openCMv40Projects = [{id: 'p1', session: {},",
            "  _repoCargadoPara: '/viejo.mkv'}];",
            _fn("_cmv40IrALaFase"),
            _fn("_cmv40ChangeTarget"),
            "_cmv40ChangeTarget('p1').then(() => {",
            "  process.stdout.write(JSON.stringify({hechos,",
            "    repo: openCMv40Projects[0]._repoCargadoPara}));",
            "});",
        ])
        r = subprocess.run(argv_node(guion), capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        m = json.loads(r.stdout)
        scroll = [h for h in m["hechos"] if h.startswith("scroll:")]
        self.assertTrue(scroll, f"no pidió llevar la vista: {m['hechos']}")
        self.assertIn('data-fase-key="B"', scroll[0])
        # Y DESPUÉS de repintar: sobre el panel viejo, la card de la Fase B
        # todavía no existe donde va a quedar.
        self.assertLess(m["hechos"].index("repintado"),
                        m["hechos"].index(scroll[0]))
        # La lista de bins se olvida: el que se acaba de descartar sigue en
        # la cargada, así que al volver a la Fase B hay que pedirla de nuevo.
        self.assertIsNone(m["repo"])


if __name__ == "__main__":
    unittest.main()
