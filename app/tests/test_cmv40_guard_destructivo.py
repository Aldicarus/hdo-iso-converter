"""Las operaciones destructivas no corren con una fase en marcha.

`cleanup` y `delete?clean_artifacts=true` hacían `rmtree` del workdir sin
comprobar `running_phase`, así que se podían borrar los artefactos por debajo
del `dovi_tool` que los estaba escribiendo — y lo que ve el usuario es un
fallo incomprensible en el log, no un mensaje. Los nueve endpoints de fase ya
tienen su guard, y `cleanup-bulk` ya saltaba los proyectos en ejecución: la
omisión estaba solo en los de sesión única.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_guard_destructivo -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402


class TestGuardDestructivoConFaseEnCurso(ApiTestCase):

    def test_cleanup_con_fase_en_curso_devuelve_409(self):
        sid = self.crear_sesion(phase="extracted", running_phase="inject")
        artefacto = Path(self.leer_sesion(sid).artifacts_dir) / "EL_injected.hevc"
        artefacto.write_bytes(b"x" * 10)
        r = self.client.post(f"/api/cmv40/{sid}/cleanup")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("inject", r.json()["detail"])
        self.assertTrue(artefacto.exists(), "el artefacto se ha borrado bajo la fase")
        self.assertFalse(self.leer_sesion(sid).archived)

    def test_delete_con_artefactos_y_fase_en_curso_devuelve_409(self):
        sid = self.crear_sesion(phase="extracted", running_phase="extract")
        r = self.client.delete(f"/api/cmv40/{sid}?clean_artifacts=true")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIsNotNone(self.leer_sesion(sid), "la sesión se ha borrado")

    def test_cleanup_sin_fase_en_curso_sigue_funcionando(self):
        sid = self.crear_sesion(phase="done")
        artefacto = Path(self.leer_sesion(sid).artifacts_dir) / "BL.hevc"
        artefacto.write_bytes(b"x" * 20)
        r = self.client.post(f"/api/cmv40/{sid}/cleanup")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.leer_sesion(sid).archived)
        self.assertFalse(artefacto.exists())


if __name__ == "__main__":
    unittest.main()
