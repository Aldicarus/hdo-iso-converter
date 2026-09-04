"""Abrir un proyecto CMv4.0 no puede arrancar trabajo.

Bug real (El día de la revelación, 2026-09-04): hacer clic en el proyecto del
sidebar lanzaba la Fase A sola. El usuario tuvo que cancelar dos veces —hay dos
arranques en el `phase_history`, a las 10:45 y las 11:00 UTC— sobre un proyecto
cuyo MKV origen ya ni existe.

El mecanismo: `auto_pipeline` se persiste en el backend, `openCMv40Project` lo
copia a `project.autoContinue` y dispara `_cmv40MaybeAutoAdvance` a los 100 ms
«por si el pipeline se quedó a medias». Para cualquier fase intermedia eso es
reanudar, que es lo que se quería. Pero en `created` no hay nada que reanudar:
se *empieza* el job, doce minutos de Fase A que el usuario no ha pedido. Y el
safety poller lo reintenta cada 4 s, así que cancelar no bastaba.

El toggle de auto ya llevaba escrito el principio correcto —«el switch solo
marca el modo de trabajo, NO dispara fases por sí mismo; lanzar con el toggle
sería sorprendente para el usuario»—; la ruta de apertura no lo tenía.

`_autoChaining` separa los dos casos: lo enciende quien pidió arrancar (la
creación del proyecto) y sigue encendido mientras la cadena avanza, así que el
encadenado pre-flight → Fase A no se toca.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_abrir_no_arranca -v
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

from frontend_sources import js_completo  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()


def _fn(nombre: str) -> str:
    marca = f"function {nombre}("
    i = JS.index(marca)
    return JS[i:JS.index("\n}\n", i) + 3]


def _correr(sesion: dict, *, auto_continue=True, auto_chaining=None,
            pending_target=None) -> dict:
    """Evalúa `_cmv40MaybeAutoAdvance` con las acciones del pipeline espiadas.

    Devuelve qué se llamó y cómo quedó el proyecto.
    """
    guion = f"""
{_fn('_cmv40MaybeAutoAdvance')}
const llamadas = [];
for (const n of ['cmv40DoAnalyzeSource', '_cmv40FirePreflight', 'cmv40DoExtract',
                 '_cmv40AutoMarkSynced', '_cmv40AutoInject', 'cmv40DoRemux',
                 'cmv40DoValidate', '_cmv40AutoTargetPath', '_cmv40AutoTargetDrive',
                 '_cmv40AutoTargetMkv']) {{
  globalThis[n] = (...a) => llamadas.push(n);
}}
globalThis.showToast = () => {{}};
globalThis._cmv40SkipSyncReview = () => true;
globalThis.refreshCMv40Sidebar = () => {{}};
const project = {{
  id: 'p1',
  session: {json.dumps(sesion)},
  autoContinue: {json.dumps(auto_continue)},
  pendingTarget: {json.dumps(pending_target)},
}};
{"project._autoChaining = " + json.dumps(auto_chaining) + ";" if auto_chaining is not None else ""}
_cmv40MaybeAutoAdvance(project);
console.log(JSON.stringify({{
  llamadas,
  autoChaining: project._autoChaining === true,
  dedup: project._lastAutoFiredFor ? project._lastAutoFiredFor.state : null,
}}));
"""
    r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr[:2000])
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node no está instalado")
class TestAbrirNoArranca(unittest.TestCase):

    CREADO = {"phase": "created", "target_preflight_ok": True}

    def test_abrir_un_proyecto_en_created_no_lanza_fase_a(self):
        """El bug: bastaba con hacer clic en el proyecto del sidebar."""
        r = _correr(self.CREADO)
        self.assertEqual(r["llamadas"], [], "abrir el proyecto arrancó el job")

    def test_abrir_tampoco_dispara_el_preflight(self):
        r = _correr({"phase": "created", "target_preflight_ok": False},
                    pending_target={"kind": "repo", "value": {"file_id": "x"}})
        self.assertEqual(r["llamadas"], [])

    def test_y_no_envenena_el_dedup_para_un_arranque_posterior(self):
        """Si el guard marcara `_lastAutoFiredFor`, el arranque legítimo de
        después se descartaría por duplicado durante 12 s."""
        r = _correr(self.CREADO)
        self.assertIsNone(r["dedup"])

    def test_crear_el_proyecto_SI_arranca(self):
        """La ruta de creación enciende `_autoChaining` antes de llamar."""
        r = _correr(self.CREADO, auto_chaining=True)
        self.assertEqual(r["llamadas"], ["cmv40DoAnalyzeSource"])

    def test_el_encadenado_preflight_a_fase_a_sigue_vivo(self):
        """Mientras la cadena avanza `_autoChaining` sigue encendido, así que
        cuando el pre-flight deja `target_preflight_ok=true` se pasa a Fase A."""
        r = _correr(self.CREADO, auto_chaining=True)
        self.assertIn("cmv40DoAnalyzeSource", r["llamadas"])


@unittest.skipUnless(NODE, "node no está instalado")
class TestReanudarSigueFuncionando(unittest.TestCase):
    """El guard es solo para `created`: en cualquier fase intermedia hay algo
    empezado y reanudar al abrir es justo lo que se quiere."""

    def test_target_provided_reanuda_al_abrir(self):
        r = _correr({"phase": "target_provided"})
        self.assertEqual(r["llamadas"], ["cmv40DoExtract"])

    def test_sync_verified_reanuda_al_abrir(self):
        r = _correr({"phase": "sync_verified"})
        self.assertEqual(r["llamadas"], ["_cmv40AutoInject"])

    def test_injected_reanuda_al_abrir(self):
        r = _correr({"phase": "injected"})
        self.assertEqual(r["llamadas"], ["cmv40DoRemux"])


@unittest.skipUnless(NODE, "node no está instalado")
class TestLosOtrosFrenos(unittest.TestCase):
    """Los guards que ya había, para que el nuevo no los tape."""

    def test_con_error_no_arranca_ni_reanuda(self):
        r = _correr({"phase": "target_provided", "error_message": "algo falló"})
        self.assertEqual(r["llamadas"], [])

    def test_con_una_fase_en_curso_no_arranca(self):
        r = _correr({"phase": "target_provided", "running_phase": "extract"})
        self.assertEqual(r["llamadas"], [])

    def test_con_auto_apagado_no_arranca(self):
        r = _correr({"phase": "target_provided"}, auto_continue=False)
        self.assertEqual(r["llamadas"], [])

    def test_esperando_ack_no_arranca(self):
        r = _correr({"phase": "extracted", "awaiting_critical_ack": True})
        self.assertEqual(r["llamadas"], [])


if __name__ == "__main__":
    unittest.main()
