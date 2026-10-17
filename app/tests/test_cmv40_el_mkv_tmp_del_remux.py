"""El `.mkv.tmp` que la Fase G deja en /mnt/output es del proyecto, y tiene
que seguirle cuando el proyecto cambia.

Tres sitios lo olvidaban (auditoría de la suite, 2026-10-17):

  · **«Rehacer» una fase anterior a la G lo dejaba**, y el forward-roll del
    `GET` —«hay un `.mkv.tmp` y el historial tiene un remux `done`: la fase
    es `remuxed`»— DESHACÍA el reset en la siguiente lectura: el proyecto
    volvía a la Fase H con el MKV hecho con el bin ANTERIOR. Es el caso de
    «Cambiar target».
  · **Borrar el proyecto con sus artefactos** borraba el workdir pero no ese
    fichero: decenas de GB huérfanos en el directorio de salida del usuario.
  · **Renombrar el MKV de salida** con el remux ya escrito cambiaba el nombre
    esperado y dejaba el temporal con el viejo: la Fase H no lo encontraba, el
    auto-rewind mandaba a repetir el remux y el temporal viejo se quedaba.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_el_mkv_tmp_del_remux -v
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402

REMUX_HECHO = [{"phase": "remux", "status": "done",
                "started_at": datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat(),
                "finished_at": datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat()}]


class ElTmpCase(ApiTestCase):

    def _remuxado(self, **campos):
        campos.setdefault("phase", "remuxed")
        sid = self.crear_sesion(phase_history=REMUX_HECHO,
                                target_type="trusted_p7_fel_final",
                                target_trust_ok=True, **campos)
        tmp = self.output_dir / "Peli (2024).mkv.tmp"
        tmp.write_bytes(b"\x00" * 4096)
        return sid, tmp


class TestRehacerSeLlevaElRemuxViejo(ElTmpCase):

    def test_rehacer_la_fase_f_borra_el_tmp(self):
        sid, tmp = self._remuxado()
        r = self.client.post(f"/api/cmv40/{sid}/reset-to/sync_verified")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(tmp.exists(), "el remux de la pasada anterior sigue en /mnt/output")

    def test_y_la_siguiente_lectura_no_deshace_el_reset(self):
        """El forward-roll adelantaba la fase en cuanto veía el temporal."""
        sid, _ = self._remuxado()
        self.client.post(f"/api/cmv40/{sid}/reset-to/source_analyzed")
        r = self.client.get(f"/api/cmv40/{sid}")
        self.assertEqual(r.json()["phase"], "source_analyzed")

    def test_rehacer_la_fase_h_lo_conserva(self):
        """Volver a `remuxed` es justo para revalidar ESE fichero."""
        sid, tmp = self._remuxado(**{"phase": "validated"})
        self.client.post(f"/api/cmv40/{sid}/reset-to/remuxed")
        self.assertTrue(tmp.exists())


class TestBorrarElProyectoConSusArtefactos(ElTmpCase):

    def test_se_lleva_el_tmp(self):
        sid, tmp = self._remuxado()
        r = self.client.delete(f"/api/cmv40/{sid}?clean_artifacts=true")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(tmp.exists())

    def test_sin_borrar_artefactos_no_lo_toca(self):
        sid, tmp = self._remuxado()
        self.client.delete(f"/api/cmv40/{sid}")
        self.assertTrue(tmp.exists())


class TestRenombrarConElRemuxEscrito(ElTmpCase):

    def test_el_tmp_cambia_de_nombre_con_el_proyecto(self):
        sid, tmp = self._remuxado()
        r = self.client.post(f"/api/cmv40/{sid}/rename-output",
                             json={"output_mkv_name": "Peli (2024) [CMv4 FULL].mkv"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(tmp.exists())
        self.assertTrue((self.output_dir / "Peli (2024) [CMv4 FULL].mkv.tmp").exists())
        r = self.client.get(f"/api/cmv40/{sid}")
        self.assertEqual(r.json()["phase"], "remuxed", "el auto-rewind no lo ha encontrado")

    def test_un_nombre_con_ruta_no_se_acepta(self):
        """El nombre acaba en un `os.rename` de la Fase H hacia /mnt/output:
        con `../` dentro, el MKV saldría del directorio de salida."""
        sid, tmp = self._remuxado()
        for malo in ("../fuera.mkv", "sub/dir.mkv", "..\\fuera.mkv"):
            with self.subTest(malo):
                r = self.client.post(f"/api/cmv40/{sid}/rename-output",
                                     json={"output_mkv_name": malo})
                self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.leer_sesion(sid).output_mkv_name, "Peli (2024).mkv")

    def test_si_el_nombre_nuevo_ya_tiene_tmp_no_lo_pisa(self):
        sid, tmp = self._remuxado()
        otro = self.output_dir / "Otra.mkv.tmp"
        otro.write_bytes(b"de otro proyecto")
        r = self.client.post(f"/api/cmv40/{sid}/rename-output",
                             json={"output_mkv_name": "Otra.mkv"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(otro.read_bytes(), b"de otro proyecto")
        self.assertTrue(tmp.exists())


if __name__ == "__main__":
    unittest.main()
