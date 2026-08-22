"""Las URLs de la API no cambian, aunque el código se mueva de fichero.

`main.py` se está partiendo en routers por pestaña, y eso es puro movimiento de
código: cero funcionalidad nueva y mucho sitio donde equivocarse en silencio.
Un endpoint que cambie de path, que pierda un parámetro de query o que deje de
existir rompe el frontend sin que ningún otro test se entere — `app.js` llama
por URL, no por función.

Este test es el golden: la lista de rutas con sus parámetros, capturada del
esquema OpenAPI de la app real. Si mover código cambia una URL, falla aquí y
dice cuál.

Para actualizarlo a propósito (añadir o quitar un endpoint es un cambio
legítimo):
    python3 -m unittest app.tests.test_rutas_no_cambian   # falla y lista la diferencia
y entonces se edita `RUTAS` con lo que toque, en el mismo commit que el cambio.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_rutas_no_cambian -v
"""
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

GOLDEN = Path(__file__).parent / "golden_rutas.json"


def _rutas_actuales():
    import main
    esquema = main.app.openapi()
    rutas = {}
    for path, ops in esquema["paths"].items():
        for metodo, op in ops.items():
            rutas[f"{metodo.upper()} {path}"] = {
                "params": sorted([p["name"], p["in"], p.get("required", False)]
                                 for p in op.get("parameters", [])),
                "body": sorted((op.get("requestBody") or {}).get("content", {}).keys()),
            }
    ws = sorted(r.path for r in main.app.routes
                if r.__class__.__name__ == "APIWebSocketRoute")
    return {"rutas": rutas, "websockets": ws}


class TestSuperficieDeLaApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.actual = _rutas_actuales()
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_no_desaparece_ninguna_ruta(self):
        faltan = sorted(set(self.golden["rutas"]) - set(self.actual["rutas"]))
        self.assertEqual(faltan, [], f"rutas que ya no existen: {faltan}")

    def test_no_aparece_ninguna_ruta_sin_registrar(self):
        """Un endpoint nuevo es legítimo, pero tiene que entrar en el golden en
        el mismo commit — si no, el golden deja de valer para nada."""
        nuevas = sorted(set(self.actual["rutas"]) - set(self.golden["rutas"]))
        self.assertEqual(nuevas, [], f"rutas nuevas sin añadir al golden: {nuevas}")

    def test_los_parametros_de_cada_ruta_no_cambian(self):
        distintas = []
        for clave, esperado in self.golden["rutas"].items():
            real = self.actual["rutas"].get(clave)
            if real is None:
                continue                     # ya lo dice el test de arriba
            if [list(p) for p in real["params"]] != [list(p) for p in esperado["params"]]:
                distintas.append(f"{clave}: params {esperado['params']} → {real['params']}")
            if real["body"] != esperado["body"]:
                distintas.append(f"{clave}: body {esperado['body']} → {real['body']}")
        self.assertEqual(distintas, [], "\n  ".join([""] + distintas))

    def test_los_websockets_siguen_ahi(self):
        self.assertEqual(self.actual["websockets"], self.golden["websockets"])


if __name__ == "__main__":
    unittest.main()
