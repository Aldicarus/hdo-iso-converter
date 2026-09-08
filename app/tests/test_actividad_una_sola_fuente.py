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
from frontend_sources import html, js_completo  # noqa: E402
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
class TestUnaSolaLectura(unittest.TestCase):

    def _correr(self, respuesta, queue_state=None) -> dict:
        guion = _sandbox("leerActividad", "actividadDeTab", "_describirTrabajo",
                         "_refreshTabRunningDots",
                         respuesta=respuesta, queue_state=queue_state)
        guion = "let actividad = { trabajos: [], leidoEn: 0 };\n" + guion + """
(async () => {
  await _refreshTabRunningDots();
  console.log(JSON.stringify({
    peticiones: _peticiones,
    puntos: [1,2,3].map(n => document.getElementById(`tab-running-dot-${n}`).style.display),
    tooltips: [1,2,3].map(n => document.getElementById(`tab-running-dot-${n}`).dataset.tooltip),
    actividad: actividad.trabajos.length,
  }));
})();
"""
        return _node(guion)

    def test_un_refresco_es_una_sola_peticion(self):
        """Eran tres: `/api/mkv/apply/progress`, `/api/cmv40-active` y la cola."""
        r = self._correr(_actividad())
        self.assertEqual(r["peticiones"], ["/api/activity"])

    def test_deja_el_resultado_en_el_estado_compartido(self):
        r = self._correr(_actividad(_trabajo("cmv40", "Fase C de Predator")))
        self.assertEqual(r["actividad"], 1)

    def test_enciende_el_punto_de_la_pestana_correcta(self):
        r = self._correr(_actividad(_trabajo("cmv40", "Fase C de Predator")))
        self.assertEqual(r["puntos"], ["none", "none", ""])

    def test_el_analisis_extendido_enciende_el_punto_de_tab_2(self):
        """El bug que este bloque cierra: el trabajo más largo de esa pestaña
        era el único que corría con el punto apagado."""
        r = self._correr(_actividad(_trabajo("mkv", "análisis extendido de X")))
        self.assertEqual(r["puntos"][1], "")

    def test_el_tooltip_dice_que_esta_corriendo(self):
        r = self._correr(_actividad(_trabajo("cmv40", "Fase C de Predator", 372)))
        self.assertIn("Fase C de Predator", r["tooltips"][2])
        self.assertIn("6 min", r["tooltips"][2])

    def test_con_menos_de_un_minuto_lo_dice_en_segundos(self):
        r = self._correr(_actividad(_trabajo("mkv", "apertura de un MKV", 12)))
        self.assertIn("12 s", r["tooltips"][1])

    def test_dos_trabajos_en_la_misma_pestana_salen_los_dos(self):
        r = self._correr(_actividad(
            _trabajo("cmv40", "Fase C de A"), _trabajo("cmv40", "pre-flight de B")))
        self.assertIn("Fase C de A", r["tooltips"][2])
        self.assertIn("pre-flight de B", r["tooltips"][2])

    def test_tab_1_sale_de_la_cola_no_de_activity(self):
        """El punto de Tab 1 también se enciende con trabajos ENCOLADOS, y
        `activity` solo conoce lo que corre."""
        r = self._correr(_actividad(), queue_state={"running": "s1", "queue": ["s2", "s3"]})
        self.assertEqual(r["puntos"][0], "")
        self.assertIn("1 rip en curso", r["tooltips"][0])
        self.assertIn("2 en cola", r["tooltips"][0])

    def test_solo_encolado_tambien_enciende(self):
        r = self._correr(_actividad(), queue_state={"running": None, "queue": ["s2"]})
        self.assertEqual(r["puntos"][0], "")
        self.assertIn("1 en cola", r["tooltips"][0])
        self.assertNotIn("en curso", r["tooltips"][0])

    def test_con_la_casa_libre_los_tres_apagados(self):
        r = self._correr(_actividad())
        self.assertEqual(r["puntos"], ["none", "none", "none"])


@unittest.skipIf(NODE is None, "node no está instalado")
class TestUnFalloDeRedNoEsSilencio(unittest.TestCase):
    """`null` de la API no significa «no hay nada»."""

    def test_conserva_el_ultimo_dato_bueno(self):
        guion = ("let actividad = { trabajos: [], leidoEn: 0 };\n"
                 + _sandbox("leerActividad", "actividadDeTab", respuesta=None)
                 + """
(async () => {
  actividad = { trabajos: [""" + json.dumps(_trabajo("cmv40", "Fase C")) + """], leidoEn: 1 };
  await leerActividad();
  console.log(JSON.stringify({ trabajos: actividad.trabajos.length, leidoEn: actividad.leidoEn }));
})();
""")
        r = _node(guion)
        self.assertEqual(r["trabajos"], 1,
                         "un fallo de red apagaría los puntos como si nada corriera")
        self.assertEqual(r["leidoEn"], 1, "y la marca de tiempo delata que el dato es viejo")


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


class TestNoQuedanLectoresSueltos(unittest.TestCase):
    """Guard: el punto entero del bloque es que haya UNA fuente."""

    def test_solo_leeractividad_pide_api_activity(self):
        apariciones = [l for l in JS.splitlines() if "'/api/activity'" in l]
        self.assertEqual(
            len(apariciones), 1,
            "más de un sitio pregunta por la actividad; usar `leerActividad()` "
            f"y el estado compartido: {apariciones}")

    def test_nadie_pregunta_ya_por_cmv40_active_ni_por_apply_progress_para_los_puntos(self):
        """Los dos endpoints siguen existiendo y tienen su uso (el modal de
        progreso de la copia), pero ya no son la fuente de «qué corre»."""
        import re
        for fn in ("_refreshTabRunningDots", "_leerTrabajosActivos"):
            # Solo las LLAMADAS: los comentarios de estas funciones cuentan de
            # dónde venían los datos antes, y esa historia debe poder escribirse.
            llamadas = re.findall(r"apiFetch\(\s*'([^']+)'", _fn(fn))
            self.assertNotIn("/api/cmv40-active", llamadas)
            self.assertNotIn("/api/mkv/apply/progress", llamadas)

    def test_los_tooltipos_de_arranque_no_mienten(self):
        """El de Tab 2 decía "copia/edición" cuando el punto se enciende
        también con el análisis extendido."""
        self.assertNotIn("Hay una copia/edición en curso", html())


if __name__ == "__main__":
    unittest.main()
