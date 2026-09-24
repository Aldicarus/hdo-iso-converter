"""Los proyectos aparecen en la lista según se crean, no todos al final.

Con «Ejecutar al crear», los diez episodios de un disco entraban en la cola y
se veían en la columna de trabajo, pero **la lista de proyectos seguía
vacía**: aparecían todos de golpe cuando arrancaba el primer rip. Reportado el
2026-09-24.

La causa: el WebSocket de cola refrescaba la lista sólo cuando cambiaba **el
que corre** (`running`). Encolar diez trabajos no cambia eso —nada corre
todavía—, así que ningún refresco ocurría hasta que el primero arrancó, que es
justo el momento en que el usuario vio salir todos.

Lo que se fija aquí:

- el WS refresca cuando cambia la COMPOSICIÓN de la cola, no sólo el que
  corre: los estados que el sidebar pinta (`pending` ↔ `queued`) dependen de
  ella;
- y no refresca cuando el mensaje no trae ningún cambio, porque `_notify` del
  gestor de cola emite en cada movimiento y repetir la petición por cada uno
  no aportaría nada;
El orden de las dos escrituras del final —el resultado antes de bajar la
bandera de «corriendo»— se comprueba en `test_serie_auto_ejecuta`, que es
donde el runner se ejecuta de verdad: aquí no había con qué hacerlo pasar por
el camino que escribe el resultado, y un test que pasa sin ejercitarlo sólo
aparenta cobertura.

El refresco por episodio creado —el que hace que vayan saliendo también
cuando NO se encolan— vive dentro de `seriesCreateSessions`, que no se puede
invocar sin media pestaña montada; no tiene test propio y su efecto es el
mismo que el de aquí.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_refresco_del_sidebar -v
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


# El arnés: `connectQueueWebSocket` con un WebSocket de mentira y las tres
# funciones que su `onmessage` llama, espiadas. Lo que se mide es CUÁNTAS
# veces pide la lista de proyectos.
_DRIVER = """
'use strict';
const llamadas = [];
globalThis.WebSocket = function () { globalThis.__ws = this; };
globalThis.WebSocket.prototype.close = function () {};
globalThis.location = { protocol: 'http:', host: 'x' };
globalThis.queueState = {};
globalThis.loadSessions = () => { llamadas.push('loadSessions'); };
globalThis.updateSubtabQueuePill = () => {};
globalThis.connectExecutionWebSocket = () => {};
globalThis.refreshOpenProjectState = () => {};
globalThis._resetTerminalToastDedup = () => {};
globalThis.updateColaPanel = () => {};
globalThis.renderQueuePanel = () => {};
globalThis.setTimeout = () => 0;
// Las variables de módulo que la función toca. En modo estricto hay que
// declararlas: asignar a un nombre libre lanza.
var queueWs, _queueWsReconnectDelay = 3000;
const _QUEUE_WS_MAX_DELAY = 30000;

__CUERPO__

connectQueueWebSocket();
const ws = globalThis.__ws;
const mandar = (o) => ws.onmessage({ data: JSON.stringify(o) });

const out = {};
function medir(nombre, mensajes) {
  llamadas.length = 0;
  globalThis.queueState = {};
  for (const m of mensajes) mandar(m);
  out[nombre] = llamadas.length;
}

// Encolar tres rips sin que nada arranque: es «Ejecutar al crear».
medir('encolar_tres', [
  { running: null, queue: ['s1'] },
  { running: null, queue: ['s1', 's2'] },
  { running: null, queue: ['s1', 's2', 's3'] },
]);
// El mismo mensaje repetido no aporta nada.
medir('sin_cambios', [
  { running: null, queue: ['s1'] },
  { running: null, queue: ['s1'] },
  { running: null, queue: ['s1'] },
]);
// Quitar uno de la cola también cambia estados.
medir('descartar', [
  { running: null, queue: ['s1', 's2'] },
  { running: null, queue: ['s1'] },
]);
// Y lo que ya funcionaba: arrancar y terminar.
medir('arranca', [
  { running: null, queue: ['s1'] },
  { running: 's1', queue: [] },
]);
medir('termina', [
  { running: 's1', queue: [] },
  { running: null, queue: [] },
]);
process.stdout.write(JSON.stringify(out));
"""


def _cuerpo() -> str:
    """`connectQueueWebSocket` tal cual la declara el frontend."""
    js = js_completo()
    i = js.index("\nfunction connectQueueWebSocket(")
    return js[i + 1:js.index("\n}\n", i + 1) + 3]


@unittest.skipUnless(NODE, "node no disponible")
class TestElWsDeColaRefrescaLaLista(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        guion = motor_i18n() + "\n" + _DRIVER.replace("__CUERPO__", _cuerpo())
        r = subprocess.run(argv_node(guion), capture_output=True, text=True,
                           timeout=60)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        cls.m = json.loads(r.stdout)

    def test_encolar_sin_que_arranque_nada_refresca_la_lista(self):
        # Tres mensajes, tres refrescos: el sidebar va saliendo. Con el
        # criterio anterior esto valía CERO.
        self.assertEqual(self.m["encolar_tres"], 3)

    def test_un_mensaje_que_no_cambia_nada_no_pide_la_lista(self):
        self.assertEqual(self.m["sin_cambios"], 1,
                         "solo la primera, que sí trae la cola")

    def test_quitar_de_la_cola_tambien_refresca(self):
        self.assertEqual(self.m["descartar"], 2)

    def test_y_lo_de_siempre_sigue(self):
        self.assertGreaterEqual(self.m["arranca"], 1)
        self.assertGreaterEqual(self.m["termina"], 1)


if __name__ == "__main__":
    unittest.main()
