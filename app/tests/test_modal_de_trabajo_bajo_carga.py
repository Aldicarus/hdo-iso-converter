"""El modal de trabajo con el NAS saturado de I/O.

Tres síntomas que el usuario reportó juntos el 2026-09-23, y que son el
mismo problema visto desde tres sitios: **el frontend trataba una lectura
lenta o fallida como si fuera un dato**.

1. «El botón Detalles cuesta de abrir o directamente no abre y hay que
   volver a pulsarlo.» El `openModal` iba DETRÁS del `await fn(a)`, que es
   una petición al backend: con el pool cargado el botón parecía muerto, y
   si la petición rechazaba, la excepción se llevaba por delante la
   apertura entera.
2. «Una fase pasa de en ejecución a ejecutada con retraso y parece que
   falle.» Los dos pollers eran `setInterval` con callback `async`: el
   temporizador no espera a que termine, así que bajo carga se apilaban
   peticiones y la que pintaba la última podía ser la más vieja.
3. «El modal cambió por completo a uno sin barra lateral ni log, y al cabo
   de un minuto volvió.» Cuando `/api/cmv40/{id}` falla, el adaptador
   seguía devolviendo `cartel` (el icono de respaldo) y `cuerpo` («Todavía
   no hay líneas de log»): el guard del armazón era `lateral || cuerpo ||
   cartel`, daba cierto, y la vista a medias sustituía a la buena.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_modal_de_trabajo_bajo_carga -v
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import argv_node, js_completo  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()

PIEZAS = ("registrarDetalleDeTrabajo", "_trabajoModalProgramar",
          "_trabajoModalParar", "_trabajoModalRefrescar", "_trabajoModalAbrir")


def _fn(nombre: str) -> str:
    i = JS.index(f"function {nombre}(")
    return JS[JS.rindex("\n", 0, i) + 1:JS.index("\n}\n", i) + 3]


def _estado() -> str:
    """Las `let _trabajoModal*` del fuente, no escritas a mano: una variable
    nueva rompería este arnés con un `ReferenceError` en vez de dejarlo
    midiendo otra cosa."""
    nombres = re.findall(r"^let (_trabajoModal\w+) = .*$", JS, re.M)
    assert nombres, "no se encuentran las variables de estado del modal"
    return "".join(f"let {n} = null;\n" for n in nombres)


# Lo que el armazón usa y aquí no se mide: se sustituye por espías.
STUBS = """
'use strict';
const tr = (k) => k;
const _workbarDetalles = {};
let workbarEstado = { activo: null, cola: [] };
const registro = { abre: 0, cierra: 0, pinta: [], orden: [] };
function openModal() { registro.abre += 1; registro.orden.push('abre'); }
function closeModal() { registro.cierra += 1; registro.orden.push('cierra'); }
// El de verdad para el reloj Y cierra: el armazón lo llama para el tipo
// que tiene su propio sitio.
function cerrarModalDeTrabajo() { _trabajoModalParar(); closeModal(); }
function _trabajoModalConResumen(a, vista) { return vista; }
function _trabajoModalPinta(a, vista) {
  registro.pinta.push(vista || {});
  registro.orden.push('pinta');
}
const espera = (ms) => new Promise(r => setTimeout(r, ms));
"""


def _node(guion: str) -> dict:
    fuente = STUBS + _estado() + "".join(_fn(n) for n in PIEZAS) + guion
    r = subprocess.run(argv_node(fuente), capture_output=True, text=True,
                       timeout=40)
    if r.returncode != 0:
        raise AssertionError(f"node falló:\n{r.stderr[:900]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


TRABAJO = {"id": "p1", "sobre": "p1", "detalle": "prueba",
           "que": "Fase C de Predator", "tipo": "fase_cmv40"}
BUENA = {"lateral": "<timeline/>", "cuerpo": "<log/>", "cartel": "<img/>",
         "titulo": "Predator"}
# Lo que devuelve el adaptador cuando su GET falla: `lateral` vacío pero
# `cartel` y `cuerpo` CON valor — que es lo que engañaba al guard viejo.
A_MEDIAS = {"lateral": "", "cuerpo": "Todavía no hay líneas de log",
            "cartel": "<icono/>", "sinDetalle": "borrado"}


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElArmazonSeAbreAntesDePedirNada(unittest.TestCase):
    """Síntoma 1: el botón parecía muerto, o no abría."""

    def test_abre_antes_de_que_el_detalle_conteste(self):
        out = _node("""
        registrarDetalleDeTrabajo('prueba', async () => {
          registro.orden.push('pide');
          await espera(60);
          registro.orden.push('contesta');
          return %s;
        });
        (async () => {
          await _trabajoModalAbrir(%s);
          _trabajoModalParar();
          console.log(JSON.stringify(registro));
        })();
        """ % (json.dumps(BUENA), json.dumps(TRABAJO)))
        self.assertLess(out["orden"].index("abre"),
                        out["orden"].index("contesta"),
                        "el modal esperó a la red para abrirse")

    def test_un_rechazo_del_detalle_no_impide_abrirlo(self):
        """Con el `await` desnudo, la excepción se llevaba la apertura y
        había que volver a pulsar."""
        out = _node("""
        registrarDetalleDeTrabajo('prueba', async () => {
          throw new Error('timeout');
        });
        (async () => {
          await _trabajoModalAbrir(%s);
          _trabajoModalParar();
          console.log(JSON.stringify(registro));
        })();
        """ % json.dumps(TRABAJO))
        self.assertEqual(out["abre"], 1)
        self.assertEqual(out["cierra"], 0, "un fallo no debe cerrar el modal")

    def test_un_tipo_con_sitio_propio_sigue_sin_dejar_el_armazon_puesto(self):
        """El pre-flight devuelve null: «ya lo he enseñado yo»."""
        out = _node("""
        registrarDetalleDeTrabajo('prueba', async () => null);
        (async () => {
          await _trabajoModalAbrir(%s);
          _trabajoModalParar();
          console.log(JSON.stringify(registro));
        })();
        """ % json.dumps(TRABAJO))
        self.assertEqual(out["cierra"], 1)


@unittest.skipIf(NODE is None, "node no está instalado")
class TestUnaLecturaFallidaNoPisaLaVistaBuena(unittest.TestCase):
    """Síntoma 3: el modal perdía la columna lateral durante un minuto."""

    def _dos_refrescos(self, segunda: dict) -> dict:
        return _node("""
        let n = 0;
        registrarDetalleDeTrabajo('prueba', async () => (++n === 1 ? %s : %s));
        (async () => {
          workbarEstado.activo = %s;
          await _trabajoModalAbrir(%s);
          _trabajoModalParar();
          await _trabajoModalRefrescar();
          console.log(JSON.stringify(registro));
        })();
        """ % (json.dumps(BUENA), json.dumps(segunda),
               json.dumps({**TRABAJO, "pct": 40}), json.dumps(TRABAJO)))

    def test_con_sinDatos_se_conserva_lo_ultimo_bueno(self):
        """La carga útil REAL: la vista fallida trae el marcador «Todavía no
        hay líneas de log», que es *truthy*.

        Por eso no basta la fusión por hueco —conserva lo que el hueco nuevo
        deja vacío, y este no lo está: trae un placeholder— y por eso quien
        tiene que avisar es el adaptador, que es el único que sabe que su
        lectura falló. Es exactamente el log que el usuario vio desaparecer.
        """
        out = self._dos_refrescos({**A_MEDIAS, "sinDatos": True})
        ultima = out["pinta"][-1]
        self.assertEqual(ultima.get("lateral"), BUENA["lateral"])
        self.assertEqual(ultima.get("cuerpo"), BUENA["cuerpo"],
                         "el marcador de «sin log» se comió el log")

    def test_y_aunque_el_adaptador_no_lo_diga_el_hueco_vacio_no_borra(self):
        """Segunda red, para el adaptador que todavía no mande `sinDatos`:
        un hueco vacío conserva el valor anterior. Es justo el caso que el
        guard todo-o-nada dejaba pasar, porque `cartel` y `cuerpo` venían
        con valor y solo `lateral` estaba vacío."""
        out = self._dos_refrescos(A_MEDIAS)
        self.assertEqual(out["pinta"][-1].get("lateral"), BUENA["lateral"],
                         "la vista a medias se comió la columna lateral")


@unittest.skipIf(NODE is None, "node no está instalado")
class TestLosRefrescosNoSeSolapan(unittest.TestCase):
    """Síntoma 2: con `setInterval` y un callback async, el temporizador no
    espera — bajo carga se apilaban y la respuesta vieja podía pintarse la
    última."""

    def test_nunca_hay_dos_refrescos_a_la_vez(self):
        out = _node("""
        let dentro = 0, maximo = 0, vueltas = 0;
        _trabajoModalRefrescar = async () => {
          dentro += 1; maximo = Math.max(maximo, dentro); vueltas += 1;
          await espera(45);          // más lento que el intervalo
          dentro -= 1;
        };
        (async () => {
          _trabajoModalProgramar(10);
          await espera(260);
          _trabajoModalParar();
          console.log(JSON.stringify({maximo, vueltas}));
        })();
        """)
        self.assertEqual(out["maximo"], 1,
                         "dos refrescos en vuelo: las respuestas pueden "
                         "pintarse desordenadas")
        self.assertGreater(out["vueltas"], 1, "el bucle no encadenó")

    def test_parar_detiene_el_bucle_de_verdad(self):
        out = _node("""
        let vueltas = 0;
        _trabajoModalRefrescar = async () => { vueltas += 1; };
        (async () => {
          _trabajoModalProgramar(10);
          await espera(60);
          _trabajoModalParar();
          const alParar = vueltas;
          await espera(80);
          console.log(JSON.stringify({alParar, despues: vueltas}));
        })();
        """)
        self.assertEqual(out["alParar"], out["despues"])


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElAdaptadorDeCmv40DiceCuandoNoHaLeido(unittest.TestCase):
    """Quien sabe que la lectura falló es el adaptador, y tiene que decirlo.

    Antes contestaba `sinDetalle: 'borrado'` —«este proyecto ya no existe»—
    para CUALQUIER `null` de `apiFetch`, que devuelve lo mismo para un 404 y
    para un timeout. Con el NAS cargado eso significa anunciar como borrado
    un proyecto que está corriendo, y devolver una vista a medias que el
    armazón daba por buena.
    """

    def _correr(self, status):
        from frontend_sources import js_completo as _js
        js = _js()
        i = js.index("registrarDetalleDeTrabajo('cmv40'")
        bloque = js[i:js.index("\n});", i) + 4]
        guion = """
'use strict';
const tr = (k) => k;
const est_simulado = %d;
const apiFetch = async (url, opts) => {
  if (opts && opts.estado) opts.estado.status = est_simulado;
  return null;                       // 404 y timeout devuelven lo mismo
};
const icono = () => '<svg/>';
const cartelDeTmdb = () => '<cartel/>';
const escHtml = (t) => String(t);
const openCMv40Projects = [];
const _trabajoLogHTML = () => 'Todavía no hay líneas de log';
const _cmv40UpdateTimelineIncremental = () => {};
const _cmv40CtxTimeline = () => ({});
const abrirProyectoCMv40Para = () => {};
let _vista = null;
function registrarDetalleDeTrabajo(clave, fn) { _vista = fn; }
%s
(async () => {
  const v = await _vista({id: 'p1', que: 'Fase C', historial: {}});
  console.log(JSON.stringify(v || {}));
})();
""" % (status, bloque)
        r = subprocess.run(argv_node(guion), capture_output=True, text=True,
                           timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló:\n{r.stderr[:700]}")
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_un_timeout_pide_conservar_la_vista(self):
        v = self._correr(0)        # 0 = no hubo respuesta
        self.assertTrue(v.get("sinDatos"))
        self.assertNotEqual(v.get("sinDetalle"), "borrado",
                            "un NAS lento no es un proyecto borrado")

    def test_un_500_tampoco_es_un_borrado(self):
        self.assertTrue(self._correr(500).get("sinDatos"))

    def test_un_404_SI_dice_que_se_borro(self):
        v = self._correr(404)
        self.assertEqual(v.get("sinDetalle"), "borrado")
        self.assertFalse(v.get("sinDatos"))


if __name__ == "__main__":
    unittest.main()
