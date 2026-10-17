"""El panel de Limpieza solo ofrece —y solo borra— lo que sigue siendo huérfano.

El arreglo de la auditoría con Codex del 2026-10-17 (paquete P4 #4) tenía
dos partes y ninguna se ejecutaba en un test:

  · el `.mkv.tmp` de un proyecto CMv4.0 —en marcha o PARADO, esperando a
    que alguien lance la Fase H— no es un remux huérfano: es su remux. Pasada
    una hora el panel lo marcaba de serie, y borrarlo obligaba a rehacer la
    Fase G;
  · el borrado vuelve a escanear al ejecutar: un preview viejo —o una
    petición a mano— se llevaba el workdir de un proyecto que ya tenía sesión.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_limpieza_solo_lo_huerfano -v
"""
import os
import sys
import time
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402


class TestLaLimpieza(ApiTestCase):

    def _viejo(self, ruta: Path) -> Path:
        hace_dos_horas = time.time() - 7200
        os.utime(ruta, (hace_dos_horas, hace_dos_horas))
        return ruta

    def _ofrecidos(self) -> set[str]:
        r = self.client.get("/api/cleanup/scan")
        self.assertEqual(r.status_code, 200, r.text)
        return {i["path"] for i in r.json()["items"]}

    def _borrar(self, ruta: Path) -> dict:
        r = self.client.post("/api/cleanup/execute", json={"paths": [str(ruta)]})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_el_remux_de_un_proyecto_parado_no_es_huerfano(self):
        self.crear_sesion(phase="remuxed", output_mkv_name="Peli (2024).mkv")
        remux = self.output_dir / "Peli (2024).mkv.tmp"
        remux.write_bytes(b"x" * 100)
        self._viejo(remux)
        self.assertNotIn(str(remux), self._ofrecidos(),
                         "el remux de un proyecto a la espera de la Fase H, ofrecido para borrar")
        datos = self._borrar(remux)
        self.assertEqual(datos["deleted"], [])
        self.assertTrue(remux.exists(), "se ha borrado el remux de un proyecto")

    def test_el_de_ningun_proyecto_si(self):
        suelto = self.output_dir / "Nadie (2020).mkv.tmp"
        suelto.write_bytes(b"x" * 100)
        self._viejo(suelto)
        self.assertIn(str(suelto), self._ofrecidos())
        self.assertEqual(len(self._borrar(suelto)["deleted"]), 1)
        self.assertFalse(suelto.exists())

    def test_un_workdir_con_sesion_no_se_borra_aunque_se_pida(self):
        sid = self.crear_sesion()
        workdir = self.work_base / sid
        (workdir / "BL.hevc").write_bytes(b"x" * 100)
        self.assertNotIn(str(workdir), self._ofrecidos())
        datos = self._borrar(workdir)
        self.assertEqual(datos["deleted"], [])
        self.assertTrue(datos["failed"])
        self.assertTrue((workdir / "BL.hevc").exists(),
                        "un preview viejo se llevó el workdir de un proyecto vivo")


if __name__ == "__main__":
    unittest.main()
