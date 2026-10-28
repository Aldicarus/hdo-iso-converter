# -*- coding: utf-8 -*-
"""Los números del servidor llevan los separadores del idioma de la app.

Cinco sitios escribían el punto de miles y la coma decimal castellanos
fijos (`f"{n:,}".replace(",", ".")`), y en inglés «It carries 2.251
CMv4.0 trims» se lee como dos coma algo. Salió al hacer las capturas de
la portada (2026-10-28). Hoy los formatea `i18n.numero`, y cada test
ejecuta el sitio que lo usa, no el formateador suelto.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_numeros_en_su_idioma -v
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import i18n  # noqa: E402
from api_harness import ApiTestCase  # noqa: E402
from phases import cmv40_pipeline, cmv40_relato  # noqa: E402
from phases.mkv_lectura import lectura_de  # noqa: E402


def _en(idioma):
    return mock.patch.object(i18n, "idioma_activo", lambda: idioma)


class TestElFormateador(unittest.TestCase):

    def test_los_separadores_de_cada_idioma(self):
        casos = {"es": ("2.251", "11,60", "1.234,5"),
                 "ca": ("2.251", "11,60", "1.234,5"),
                 "en": ("2,251", "11.60", "1,234.5")}
        for idioma, (entero, decimal, ambos) in casos.items():
            with self.subTest(idioma=idioma), _en(idioma):
                self.assertEqual(i18n.numero(2251), entero)
                self.assertEqual(i18n.numero(11.6, 2), decimal)
                self.assertEqual(i18n.numero(1234.5, 1), ambos)

    def test_sin_agrupar_la_parte_entera(self):
        with _en("en"):
            self.assertEqual(i18n.numero(1500, 1, miles=False), "1500.0")
        with _en("es"):
            self.assertEqual(i18n.numero(1500, 1, miles=False), "1500,0")

    def test_un_idioma_desconocido_cae_al_castellano(self):
        with _en("pt"):
            self.assertEqual(i18n.numero(2251), "2.251")


class TestLosSitiosQueLoUsan(unittest.TestCase):

    def test_la_frase_del_master(self):
        analisis = {"hdr": {"hdr_format": "HDR10"}, "dovi": {
            "profile": 7, "el_type": "FEL", "quality_l8_unique_count": 2251,
            "quality_l8_max_delta": 606}}
        for idioma, esperado in (("en", "2,251"), ("es", "2.251"), ("ca", "2.251")):
            with self.subTest(idioma=idioma), _en(idioma):
                textos = " ".join(f["texto"] for f in lectura_de(analisis))
                self.assertIn(esperado, textos)

    def test_los_frames_del_relato(self):
        info = SimpleNamespace(profile=7, el_type="FEL", cm_version="v4.0",
                               frame_count=222274)
        with _en("en"):
            self.assertIn("222,274", cmv40_relato._dv(info))
        with _en("es"):
            self.assertIn("222.274", cmv40_relato._dv(info))

    def test_la_linea_de_progreso_de_ffmpeg(self):
        linea = "frame=58601 fps=24 q=-1.0 size= 19003MiB time=00:40:44.08"
        with _en("en"):
            self.assertIn("58,601/145,303",
                          cmv40_pipeline._fmt_ffmpeg_frame(linea, 145303))
        with _en("es"):
            self.assertIn("58.601/145.303",
                          cmv40_pipeline._fmt_ffmpeg_frame(linea, 145303))

    def test_los_gb_del_pipeline(self):
        with _en("en"):
            self.assertEqual(cmv40_pipeline._gb(73.2e9), "73.2 GB")
        with _en("es"):
            self.assertEqual(cmv40_pipeline._gb(73.2e9), "73,2 GB")


class TestLaCopiaDesdeBiblioteca(ApiTestCase):
    """El paso de la copia en la columna: los GB y el «de» que los une."""

    def test_el_paso_habla_el_idioma_de_la_app(self):
        from routers import tab2
        estado = {"active": True, "step": "copying", "pct": 40,
                  "bytes_copied": 3 * 1024 ** 3 + 1024 ** 3 // 2,
                  "total_bytes": 30 * 1024 ** 3}
        with mock.patch.dict(tab2._mkv_apply_state, estado):
            with _en("en"):
                self.assertEqual(tab2._copia_adaptador(None)["paso"],
                                 "3.5 GB of 30.0 GB")
            with _en("es"):
                self.assertEqual(tab2._copia_adaptador(None)["paso"],
                                 "3,5 GB de 30,0 GB")


if __name__ == "__main__":
    unittest.main()
