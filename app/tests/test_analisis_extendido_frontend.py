"""Un solo botón: el perfil de luminancia llega con el análisis de calidad.

Los dos análisis de Tab 2 se separaron en su momento porque cada uno era caro.
Ya no lo son por separado: extraer el RPU del MKV es el ~97 % del coste (medido:
~650 s frente a ~7 s del export por niveles) y ahora se comparte. Así que hay un
solo botón, un solo job y un solo modal.

Del lado del frontend eso significa dos cosas que este test fija:

  · el perfil viaja en `dovi.light_profile` (viene del cache, junto a los
    campos `quality_*`) y `_mkvAplicarPerfilLuminancia` lo copia a los campos
    planos que lee el render — que los lee así desde cuando eran dos endpoints;
  · no queda un segundo camino. `_rgrfAnalyzeLight` y los helpers `_dvLight*`
    eran ~370 líneas calcadas del audit de calidad, con su propio modal,
    polling, cancelación y teardown.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_analisis_extendido_frontend -v
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

NODE = shutil.which("node")
sys.path.insert(0, str(APP_DIR / "tests"))
from frontend_sources import js_completo  # noqa: E402

JS = js_completo()
HTML = (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")


def _extraer(nombre: str) -> str:
    """El fuente de la función, DESDE EL PRINCIPIO DE SU LÍNEA.

    Cortar en `function` se come el `async` de las declaraciones asíncronas, y
    lo que sale es una función normal con `await` dentro: SyntaxError en node.
    """
    i = JS.index(f"function {nombre}(")
    return JS[JS.rindex("\n", 0, i) + 1:JS.index("\n}\n", i) + 3]


@unittest.skipUnless(NODE, "node no disponible")
class TestMapeoDelPerfil(unittest.TestCase):
    """`_mkvAplicarPerfilLuminancia` evaluado en node."""

    def _correr(self, dv):
        guion = (_extraer("_mkvAplicarPerfilLuminancia")
                 + f"\nconst dv = {json.dumps(dv)};\n"
                 + "const ok = _mkvAplicarPerfilLuminancia(dv);\n"
                 + "console.log(JSON.stringify({ok, dv}));\n")
        r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    PERFIL = {
        "per_scene_max_cll": [100, 200, 300],
        "per_scene_max_fall": [30, 60, 90],
        "per_scene_min": [1, 2, 3],
        "total_frames": 1000,
        "stats": {"peak": 300, "p95": 250},
        "references": {"l2_trim_targets_nits": [100, 600]},
    }

    def test_copia_el_perfil_a_los_campos_del_render(self):
        r = self._correr({"light_profile": self.PERFIL})
        self.assertTrue(r["ok"])
        dv = r["dv"]
        self.assertEqual(dv["per_scene_max_cll"], [100, 200, 300])
        self.assertEqual(dv["per_scene_max_fall"], [30, 60, 90])
        self.assertEqual(dv["per_scene_min"], [1, 2, 3])
        self.assertEqual(dv["l1_stats"]["peak"], 300)
        self.assertEqual(dv["l1_references"]["l2_trim_targets_nits"], [100, 600])

    def test_sin_perfil_no_toca_nada(self):
        r = self._correr({"cm_version": "v4.0"})
        self.assertFalse(r["ok"])
        self.assertNotIn("per_scene_max_cll", r["dv"])

    def test_un_perfil_vacio_no_cuenta(self):
        """Un análisis que no produjo perfil (export por niveles no disponible)
        no debe encender el gráfico con series vacías."""
        r = self._correr({"light_profile": {"per_scene_max_cll": []}})
        self.assertFalse(r["ok"])

    def test_null_no_revienta(self):
        r = self._correr({"light_profile": None})
        self.assertFalse(r["ok"])


@unittest.skipUnless(NODE, "node no disponible")
class TestVariosAnalisisALaVez(unittest.TestCase):
    """El resultado va al panel del MKV que se analizó, no al de al lado.

    El lanzador guardaba UNA sesión global (`window._mkvQualitySession`) con
    su poller y su POST abierto, así que pedir un segundo análisis mientras el
    primero esperaba turno pisaba la del primero: su poller se auto-detenía,
    la espera terminaba de golpe y lo que leía del estado —que es un
    singleton— era del otro análisis. Ahora se apunta qué se ha pedido y sobre
    qué MKV, y cada resultado se recoge por su `audit_id`.
    """

    def _correr(self, pedidos, vistos, estado, cola, activo=None,
                proyectos=("/mnt/output/A.mkv", "/mnt/output/B.mkv")):
        guion = f"""
const _toasts = [], _aplicados = [], _pintados = [];
globalThis.showToast = (t) => _toasts.push(t);
globalThis.refrescarMkvRecientes = () => {{}};
globalThis.cerrarModalDeTrabajo = () => {{}};
globalThis._renderMkvEditPanel = (p) => _pintados.push(p.filePath);
globalThis.openMkvProjects = {json.dumps(list(proyectos))}
  .map(r => ({{ filePath: r, analysis: {{ file_path: r }} }}));
globalThis.mkvProject = openMkvProjects[0];
globalThis.apiFetch = async () => ({json.dumps(estado)});
const _mkvAnalisisPedidos = new Map({json.dumps(list(pedidos.items()))});
const _mkvAnalisisVistos = new Set({json.dumps(list(vistos))});
{_extraer('_mkvRutaAnalisis')}
{_extraer('_mkvAplicarPerfilLuminancia')}
{_extraer('_mkvRecogerAnalisis')}
{_extraer('_mkvAplicarAnalisisTerminado')}
(async () => {{
  _mkvRecogerAnalisis({{ activo: {json.dumps(activo)}, cola: {json.dumps(cola)} }});
  for (let i = 0; i < 20; i++) await new Promise(r => setTimeout(r, 0));
  console.log(JSON.stringify({{
    toasts: _toasts, pintados: _pintados,
    pendientes: [..._mkvAnalisisPedidos.keys()],
    dovi: openMkvProjects.map(p => p.analysis.dovi || null),
  }}));
}})();
"""
        r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[:900])
        return json.loads(r.stdout.strip().splitlines()[-1])

    RESULTADO = {"quality_classification": "real",
                 "quality_verdict_text": "Master CMv4.0 FULL"}

    def test_el_que_termina_se_aplica_a_SU_panel(self):
        r = self._correr(
            pedidos={"a1": "/mnt/output/A.mkv", "b2": "/mnt/output/B.mkv"},
            vistos=("a1", "b2"),
            estado={"audit_id": "a1", "active": False, "result": self.RESULTADO},
            cola=[{"id": "b2"}])
        self.assertEqual(r["pendientes"], ["b2"], "el que sigue en la cola")
        self.assertEqual(r["dovi"][0]["quality_classification"], "real")
        self.assertIsNone(r["dovi"][1], "el otro MKV no se toca")

    def test_y_el_que_sigue_esperando_NO_recoge_el_resultado_ajeno(self):
        """Con el filtro por `audit_id` fuera, este es el bug: B se lleva el
        resultado de A y su panel enseña la película equivocada."""
        r = self._correr(
            pedidos={"b2": "/mnt/output/B.mkv"}, vistos=("b2",),
            estado={"audit_id": "a1", "active": False, "result": self.RESULTADO},
            cola=[])
        self.assertEqual(r["dovi"], [None, None])
        self.assertEqual(r["toasts"], [], "ni un aviso: no es asunto suyo")

    def test_uno_retirado_de_la_cola_no_da_error(self):
        """Desaparece sin dejar estado: no hay resultado, pero tampoco un
        fallo que contar. Antes salía «respuesta vacía del servidor»."""
        r = self._correr(
            pedidos={"b2": "/mnt/output/B.mkv"}, vistos=("b2",),
            estado={"audit_id": "otro", "active": False, "error": "x"},
            cola=[])
        self.assertEqual(r["toasts"], [])
        self.assertEqual(r["pendientes"], [])

    def test_el_que_todavia_no_ha_salido_en_la_columna_no_se_da_por_muerto(self):
        """Entre el POST y el poll siguiente el trabajo no está en ninguna
        lista. Sin la marca de «visto» se recogería como terminado."""
        r = self._correr(
            pedidos={"nuevo": "/mnt/output/A.mkv"}, vistos=(),
            estado={"audit_id": "nuevo", "active": True}, cola=[])
        self.assertEqual(r["pendientes"], ["nuevo"])

    def test_el_error_se_cuenta_una_vez_y_con_su_texto(self):
        r = self._correr(
            pedidos={"a1": "/mnt/output/A.mkv"}, vistos=("a1",),
            estado={"audit_id": "a1", "active": False,
                    "step": "error", "error": "extract-rpu falló"},
            cola=[])
        self.assertEqual(len(r["toasts"]), 1)
        self.assertIn("extract-rpu falló", r["toasts"][0])

    def test_y_una_cancelacion_no_se_cuenta_como_error(self):
        r = self._correr(
            pedidos={"a1": "/mnt/output/A.mkv"}, vistos=("a1",),
            estado={"audit_id": "a1", "active": False, "step": "cancelled",
                    "error": "Cancelado por el usuario"},
            cola=[])
        self.assertIn("cancelada", r["toasts"][0])

    def test_si_el_MKV_se_cerro_el_resultado_queda_en_cache(self):
        r = self._correr(
            pedidos={"a1": "/mnt/output/Cerrada.mkv"}, vistos=("a1",),
            estado={"audit_id": "a1", "active": False, "result": self.RESULTADO},
            cola=[])
        self.assertIn("caché", r["toasts"][0])
        self.assertEqual(r["pintados"], [])


class TestNoQuedanDosCaminos(unittest.TestCase):
    """El segundo pipeline de la UI tiene que haber desaparecido de verdad."""

    def test_no_queda_el_analisis_de_luminancia_aparte(self):
        for muerto in ("_rgrfAnalyzeLight", "_dvLightSetStep",
                       "_dvLightSetProgress", "_dvLightSetElapsed",
                       "_dvLightSession"):
            # Las menciones en comentarios explican por qué ya no está; lo que
            # no puede quedar es una llamada o una definición.
            self.assertNotIn(f"{muerto}(", JS, f"{muerto} sigue invocándose")

    def test_no_queda_el_endpoint_del_perfil(self):
        """Las URLs muertas son EXACTAMENTE estas dos, no todo lo que empiece
        por `light-profile`. La comprobación era por subcadena y también
        prohibía `/api/mkv/light-profile-cached`, que es el comparador A/B —
        otro endpoint, de sólo lectura y que no lanza ningún análisis. Un guard
        que caza código legítimo empuja a renombrar para esquivarlo, que es
        justo lo contrario de lo que se busca."""
        for muerta in ("'/api/mkv/light-profile'", '"/api/mkv/light-profile"',
                       "/api/mkv/light-profile?", "/api/mkv/light-profile/progress",
                       "/api/mkv/light-profile/cancel"):
            self.assertNotIn(muerta, JS, f"vuelve a llamarse a {muerta}")

    def test_el_modal_del_perfil_ya_no_esta_en_el_html(self):
        self.assertNotIn('id="dv-light-modal"', HTML)
        self.assertNotIn('id="dv-light-step-1"', HTML)

    def test_los_dos_botones_llaman_al_mismo_analisis(self):
        """El de la card de calidad y el del bloque de luminancia."""
        self.assertGreaterEqual(JS.count("_rgrfAuditQuality(event)"), 3,
                                "faltan botones apuntando al análisis extendido")

    def test_el_backend_ya_no_expone_el_endpoint(self):
        # Se miran los dos ficheros: los endpoints de Tab 2 viven ahora en
        # `routers/tab2.py`, así que buscar solo en `main.py` pasaría en verde
        # aunque el endpoint volviera.
        for rel in ("main.py", "routers/tab2.py"):
            src = (APP_DIR / rel).read_text(encoding="utf-8")
            # Con la comilla final: `/api/mkv/light-profile-cached` es otro
            # endpoint (el comparador A/B) y sí puede existir.
            self.assertNotIn('/api/mkv/light-profile"', src, rel)
            self.assertNotIn('/api/mkv/light-profile/progress', src, rel)
            self.assertNotIn("_light_profile_state: dict", src, rel)

    def test_el_perfil_esta_en_el_modelo(self):
        """Si `DoviInfo` no lo declara, la re-inyección del cache lo descarta en
        silencio: `analyze_mkv` filtra por `hasattr`."""
        src = (APP_DIR / "models.py").read_text(encoding="utf-8")
        self.assertIn("light_profile: dict | None = None", src)


if __name__ == "__main__":
    unittest.main()
