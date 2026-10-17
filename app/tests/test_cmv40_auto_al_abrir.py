"""Abrir un proyecto CMv4.0 con el auto APAGADO no lanza nada.

`openCMv40Project` tenía un respaldo «para sesiones anteriores al campo
`auto_pipeline`»: a mitad de pipeline con target de confianza, o con una fase
en marcha, daba el proyecto por automático. Pero el modelo tiene
`auto_pipeline: bool = False`, así que «falso» y «ausente» llegan igual y el
respaldo se aplicaba a TODO proyecto manual: abrirlo lanzaba la Fase C en
menos de un segundo y la cabecera decía «Auto ON», cuando el backend —que
encadena con ese campo y con ningún otro— no la habría lanzado (auditoría con
Codex del 2026-10-17, reproducido en Chrome).

El test ejecuta `openCMv40Project` de verdad en node, con el DOM y la red
sustituidos, y mira las dos cosas que el usuario ve: el modo con que queda el
proyecto y si se dispara el auto-avance.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_auto_al_abrir -v
"""
import json
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


def _fn(nombre):
    i = JS.index(f"function {nombre}(")
    return JS[i:JS.index("\n}\n", i) + 3]


def _abrir(sesion):
    guion = f"""
{_fn('_cmv40AutoAlAbrir')}
{_fn('openCMv40Project')}
const openCMv40Projects = [];
const MAX_CMV40_PROJECTS = 5;
const avances = [];
for (const n of ['switchCMv40SubTab', '_cmv40RehydratePendingTarget', '_createCMv40SubTab',
                 '_createCMv40Panel', '_connectCMv40WebSocket', '_cmv40StartSafetyPoller',
                 '_cmv40VerifyArtifacts', 'showToast']) globalThis[n] = () => {{}};
globalThis._cmv40MaybeAutoAdvance = (p) => avances.push(p.id);
const p = openCMv40Project({json.dumps(sesion)});
setTimeout(() => console.log(JSON.stringify({{auto: p.autoContinue, avances}})), 250);
"""
    r = subprocess.run(argv_node(guion), capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr[-1500:])
    return json.loads(r.stdout)


BASE = {"id": "cmv40_x_2024_1", "phase": "target_provided", "target_trust_ok": True,
        "error_message": "", "archived": False, "running_phase": None}


@unittest.skipUnless(NODE, "node no disponible")
class TestAbrirConElAutoApagado(unittest.TestCase):

    def test_un_proyecto_manual_a_medias_no_arranca(self):
        r = _abrir(dict(BASE, auto_pipeline=False))
        self.assertFalse(r["auto"], "la cabecera diría «Auto ON»")
        self.assertEqual(r["avances"], [], "abrirlo ha lanzado la fase siguiente")

    def test_ni_con_una_fase_en_marcha(self):
        r = _abrir(dict(BASE, auto_pipeline=False, running_phase="remux"))
        self.assertFalse(r["auto"])

    def test_con_el_auto_encendido_reanuda(self):
        r = _abrir(dict(BASE, auto_pipeline=True))
        self.assertTrue(r["auto"])
        self.assertEqual(r["avances"], ["cmv40_x_2024_1"])


if __name__ == "__main__":
    unittest.main()
