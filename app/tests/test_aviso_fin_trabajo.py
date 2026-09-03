"""El aviso al terminar un trabajo largo, evaluado en node.

Un rip son 20-40 min y una fase CMv4.0 puede pasar de la hora. El aviso
existe para no tener que volver a mirar, y tiene dos restricciones que
condicionan todo el diseño y que este test fija:

1. **El NAS va por HTTP**, así que la Notification API del navegador NO está
   disponible ahí (exige contexto seguro). El aviso que siempre funciona es
   el título parpadeante; la notificación de escritorio es un extra.
2. **No se añade tráfico cuando no hay nada que vigilar.** El poller de los
   puntos verdes se salta las vueltas con la pestaña oculta a propósito
   —eran dos peticiones cada 5 s durante horas contra un NAS que está
   procesando vídeo— y eso no se toca. La vigilancia sólo arranca si al
   ocultarse la pestaña había algo corriendo.

Se evalúan las funciones REALES de `core.js` sobre un DOM mínimo: un test
que reimplementara la lógica no probaría nada.
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

NODE = shutil.which("node")
from frontend_sources import js_completo  # noqa: E402

JS = js_completo()

FUNCIONES = (
    "avisoFinActivado", "setAvisoFinActivado", "avisoSonidoActivado",
    "setAvisoSonidoActivado", "avisoNotificacionDisponible", "_pitido",
    "_pararParpadeo", "_arrancarParpadeo", "avisarFinDeTrabajo",
    "_leerTrabajosActivos", "_avisoTick", "_pararVigilancia",
)
CONSTANTES = (
    "const _AVISO_INTERVALO_MS", "const _AVISO_PREF", "const _AVISO_SONIDO_PREF",
    "const _AVISO_NOMBRES",
)


def _extraer_funcion(nombre: str) -> str:
    # `async` primero: buscar "function X(" a secas encuentra el mismo punto
    # DENTRO de "async function X(" y se deja el async fuera — el trozo
    # extraído lleva un `await` de nivel superior y node lo rechaza.
    for marca in (f"async function {nombre}(", f"function {nombre}("):
        i = JS.find(marca)
        if i != -1:
            return JS[i:JS.index("\n}\n", i) + 3]
    raise AssertionError(f"no encuentro {nombre}() en el JS del frontend")


def _extraer_linea(prefijo: str) -> str:
    i = JS.index(prefijo)
    fin = JS.index("\n", i)
    # _AVISO_NOMBRES ocupa una línea; si algún día crece, esto lo delata
    return JS[i:fin + 1]


ENTORNO = r"""
// ── DOM mínimo ────────────────────────────────────────────────────
const _store = {};
globalThis.localStorage = {
  getItem: k => (k in _store ? _store[k] : null),
  setItem: (k, v) => { _store[k] = String(v); },
};
globalThis.__titulos = [];
globalThis.document = {
  title: 'HDO Blu-ray Toolkit',
  hidden: true,
  addEventListener() {},
};
globalThis.window = { isSecureContext: false };
globalThis.__timers = 0;
globalThis.setInterval = () => { globalThis.__timers++; return globalThis.__timers; };
globalThis.clearInterval = () => { globalThis.__timers--; };
globalThis.setTimeout = () => 0;
globalThis.__avisados = [];
globalThis.__peticiones = [];
globalThis.queueState = null;
globalThis.apiFetch = async (url) => {
  globalThis.__peticiones.push(url);
  return globalThis.__respuestas[url] ?? null;
};
globalThis.__respuestas = {};
let _avisoTrabajosPrevios = null, _avisoTimer = null;
let _avisoTituloOriginal = null, _avisoParpadeoTimer = null;
"""


@unittest.skipUnless(NODE, "node no disponible")
class AvisoCase(unittest.IsolatedAsyncioTestCase):

    def correr(self, cuerpo: str, respuestas: dict | None = None,
               queue_state=None, prefs: dict | None = None):
        script = (
            ENTORNO
            + "".join(_extraer_linea(c) for c in CONSTANTES)
            + "".join(_extraer_funcion(f) for f in FUNCIONES)
            + f"\nglobalThis.__respuestas = {json.dumps(respuestas or {})};"
            + f"\nglobalThis.queueState = {json.dumps(queue_state)};"
            + "".join(f"\nlocalStorage.setItem({json.dumps(k)}, {json.dumps(v)});"
                      for k, v in (prefs or {}).items())
            + "\n(async () => {\n" + cuerpo + "\n})();"
        )
        r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[:600]}")
        return json.loads(r.stdout or "null")


class TestLaTransicion(AvisoCase):
    """El aviso salta al pasar de 'hay trabajo' a 'no hay'."""

    RESP_LIBRE = {"/api/mkv/apply/progress": {"active": False},
                  "/api/cmv40-active": {"active": False}}

    async def test_un_job_que_termina_hace_parpadear_el_titulo(self):
        out = self.correr("""
          _avisoTrabajosPrevios = {1: true, 2: false, 3: false};
          _avisoTimer = 1;
          await _avisoTick();
          process.stdout.write(JSON.stringify({titulo: document.title, timer: _avisoTimer}));
        """, respuestas=self.RESP_LIBRE)
        self.assertIn("terminado", out["titulo"])
        self.assertIn("Blu-Ray ISO", out["titulo"])
        self.assertIsNone(out["timer"], "al no quedar trabajo, la vigilancia para")

    async def test_mientras_sigue_corriendo_no_avisa(self):
        out = self.correr("""
          _avisoTrabajosPrevios = {1: false, 2: false, 3: true};
          _avisoTimer = 1;
          await _avisoTick();
          process.stdout.write(JSON.stringify({titulo: document.title, timer: _avisoTimer}));
        """, respuestas={"/api/mkv/apply/progress": {"active": False},
                         "/api/cmv40-active": {"active": True}})
        self.assertNotIn("terminado", out["titulo"])
        self.assertEqual(out["timer"], 1, "sigue vigilando")

    async def test_avisa_del_tab_correcto(self):
        out = self.correr("""
          _avisoTrabajosPrevios = {1: false, 2: false, 3: true};
          await _avisoTick();
          process.stdout.write(JSON.stringify(document.title));
        """, respuestas=self.RESP_LIBRE)
        self.assertIn("CMv4.0", out)

    async def test_desactivado_no_toca_el_titulo(self):
        out = self.correr("""
          _avisoTrabajosPrevios = {1: true, 2: false, 3: false};
          await _avisoTick();
          process.stdout.write(JSON.stringify(document.title));
        """, respuestas=self.RESP_LIBRE, prefs={"hdo_avisar_fin_trabajo": "0"})
        self.assertEqual(out, "HDO Blu-ray Toolkit")


class TestElTitulo(AvisoCase):
    async def test_al_parar_se_restaura_el_original(self):
        out = self.correr("""
          _arrancarParpadeo('✅ terminado');
          const durante = document.title;
          _pararParpadeo();
          process.stdout.write(JSON.stringify({durante, despues: document.title}));
        """)
        self.assertEqual(out["durante"], "✅ terminado")
        self.assertEqual(out["despues"], "HDO Blu-ray Toolkit",
                         "volver a la pestaña debe dejar el título como estaba")

    async def test_dos_avisos_seguidos_no_pierden_el_original(self):
        """El segundo `_arrancarParpadeo` no debe guardar como 'original' el
        título ya modificado por el primero — se quedaría pegado para siempre."""
        out = self.correr("""
          _arrancarParpadeo('✅ uno');
          _arrancarParpadeo('✅ dos');
          _pararParpadeo();
          process.stdout.write(JSON.stringify(document.title));
        """)
        self.assertEqual(out, "HDO Blu-ray Toolkit")


class TestElContextoInseguro(AvisoCase):
    """Sobre el NAS (HTTP) no hay Notification API, y eso no puede romper nada."""

    async def test_sin_contexto_seguro_no_hay_notificacion_pero_si_titulo(self):
        out = self.correr("""
          const disponible = avisoNotificacionDisponible();
          avisarFinDeTrabajo(1);
          process.stdout.write(JSON.stringify({disponible, titulo: document.title}));
        """)
        self.assertFalse(out["disponible"], "window.isSecureContext=false → no disponible")
        self.assertIn("terminado", out["titulo"], "el título parpadeante sí funciona")

    async def test_con_https_y_permiso_se_construye_la_notificacion(self):
        out = self.correr("""
          window.isSecureContext = true;
          globalThis.Notification = function (t, o) { globalThis.__notif = {t, o}; };
          globalThis.Notification.permission = 'granted';
          avisarFinDeTrabajo(3);
          process.stdout.write(JSON.stringify(globalThis.__notif || null));
        """)
        self.assertIsNotNone(out)
        self.assertIn("CMv4.0", out["o"]["body"])
        self.assertEqual(out["o"]["tag"], "hdo-fin-3", "un tag por tab: no se apilan")


class TestElTrafico(AvisoCase):
    """La razón por la que esto no pollea a lo tonto."""

    async def test_un_tick_son_dos_peticiones(self):
        out = self.correr("""
          _avisoTrabajosPrevios = {1: false, 2: false, 3: false};
          await _avisoTick();
          process.stdout.write(JSON.stringify(globalThis.__peticiones));
        """, respuestas={"/api/mkv/apply/progress": {"active": False},
                         "/api/cmv40-active": {"active": False}})
        self.assertEqual(len(out), 2, out)
        self.assertNotIn("/api/cmv40", out, "el endpoint gordo no, el de memoria")

    async def test_tab1_sale_de_queueState_sin_pedir_nada(self):
        """La cola ya llega por WS; preguntar por HTTP sería tráfico de más."""
        out = self.correr("""
          const st = await _leerTrabajosActivos();
          process.stdout.write(JSON.stringify({st, peticiones: globalThis.__peticiones}));
        """, respuestas={"/api/mkv/apply/progress": {"active": False},
                         "/api/cmv40-active": {"active": False}},
             queue_state={"running": "Peli_2024", "queue": []})
        self.assertTrue(out["st"]["1"])
        self.assertNotIn("/api/queue", out["peticiones"])

    async def test_el_intervalo_es_de_20s(self):
        out = self.correr("process.stdout.write(JSON.stringify(_AVISO_INTERVALO_MS));")
        self.assertEqual(out, 20000, "3 peticiones/min sólo mientras estás fuera")


if __name__ == "__main__":
    unittest.main()
