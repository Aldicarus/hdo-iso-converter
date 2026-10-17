"""«Aplicar cambios» de Tab 2 mirado en el MKV que queda, no en el argv.

El `mkvpropedit` falso solo entendía la firma y el título, así que aplicar los
nombres, los flags o los capítulos no cambiaba nada que el `mkvmerge -J`
siguiente pudiera ver: invertir el `flag-forced` o poner todos los capítulos a
cero pasaba en verde (las 17 mutaciones de Tab 2 de la auditoría completa con
Codex del 2026-10-17 sobrevivían). Hoy el fake aplica las ediciones, y este
módulo comprueba lo que hay DESPUÉS en el fichero:

  · nombres y flags en la pista correcta;
  · los capítulos SOLO si se mandan: `None` los deja como estaban (antes se
    reescribían siempre, con una única edición plana en `spa`);
  · una lista vacía los quita, con `--chapters ''`: escribía una
    `EditionEntry` vacía, que el mkvpropedit real rechaza con código 2, y
    «Aplicar cambios» fallaba siempre sobre un MKV sin capítulos;
  · un mkvpropedit muerto por una señal es un fallo, no un éxito.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_tab2_aplicar_en_el_mkv -v
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import FakeToolbox  # noqa: E402


class AplicarCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aplicar_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp / "tb").install()
        self.addCleanup(self.tb.uninstall)
        self.mkv = self.tmp / "Peli (2024).mkv"
        self.mkv.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 64)
        self.tb.define_mkv(self.mkv.name, tracks=[
            {"type": "video", "codec": "HEVC/H.265/MPEG-H"},
            {"type": "audio", "codec": "TrueHD Atmos", "language": "spa", "default": True},
            {"type": "subtitles", "codec": "HDMV PGS", "language": "spa", "forced": True},
            {"type": "subtitles", "codec": "HDMV PGS", "language": "spa"}])
        self.tb.define_chapters(self.mkv.name, [("00:00:00.000", "Uno"), ("00:12:00.000", "Dos")])

    async def aplicar(self, **campos):
        from models import MkvEditRequest
        from phases.mkv_analyze import apply_mkv_edits
        return await apply_mkv_edits(MkvEditRequest(file_path=str(self.mkv), **campos))

    def pistas(self):
        r = subprocess.run(["mkvmerge", "-J", str(self.mkv)], capture_output=True, text=True)
        return json.loads(r.stdout)["tracks"]

    def capitulos(self):
        r = subprocess.run(["mkvextract", str(self.mkv), "chapters", "--simple"],
                           capture_output=True, text=True)
        lineas = r.stdout.splitlines()
        return [(lineas[i].split("=", 1)[1], lineas[i + 1].split("=", 1)[1])
                for i in range(0, len(lineas), 2)]


class TestNombresYFlags(AplicarCase):

    async def test_quedan_en_la_pista_que_tocaba(self):
        await self.aplicar(
            audio_tracks=[{"id": 1, "name": "Castellano TrueHD Atmos 7.1", "flag_default": True}],
            subtitle_tracks=[{"id": 2, "name": "Castellano Forzados (PGS)", "flag_forced": True,
                              "flag_default": True},
                             {"id": 3, "name": "Castellano Completos (PGS)", "flag_forced": False,
                              "flag_default": False}])
        p = self.pistas()
        self.assertEqual(p[1]["properties"]["track_name"], "Castellano TrueHD Atmos 7.1")
        self.assertEqual((p[2]["properties"]["track_name"], p[2]["properties"]["forced_track"],
                          p[2]["properties"]["default_track"]),
                         ("Castellano Forzados (PGS)", True, True))
        self.assertEqual((p[3]["properties"]["forced_track"], p[3]["properties"]["default_track"]),
                         (False, False))


class TestLosCapitulos(AplicarCase):

    async def test_sin_tocarlos_se_quedan_como_estaban(self):
        await self.aplicar(audio_tracks=[{"id": 1, "name": "X"}])
        self.assertEqual(self.capitulos(), [("00:00:00.000", "Uno"), ("00:12:00.000", "Dos")])

    async def test_editados_se_escriben_con_sus_tiempos(self):
        await self.aplicar(chapters=[
            {"number": 1, "timestamp": "00:00:00.000", "name": "Inicio"},
            {"number": 2, "timestamp": "00:30:00.000", "name": "Final"}])
        self.assertEqual(self.capitulos(), [("00:00:00.000", "Inicio"), ("00:30:00.000", "Final")])

    async def test_una_lista_vacia_los_quita_sin_fallar(self):
        await self.aplicar(chapters=[])
        self.assertEqual(self.capitulos(), [])

    async def test_sobre_un_mkv_sin_capitulos_se_puede_aplicar(self):
        self.tb.define_chapters(self.mkv.name, [])
        r = await self.aplicar(audio_tracks=[{"id": 1, "name": "X"}])
        self.assertTrue(r["ok"])


class TestUnMkvpropeditMuertoNoEsUnExito(AplicarCase):

    async def test_por_senal_lanza(self):
        import signal
        self.tb.fail("mkvpropedit", "*", senal=signal.SIGTERM)
        with self.assertRaises(RuntimeError):
            await self.aplicar(audio_tracks=[{"id": 1, "name": "X"}])


if __name__ == "__main__":
    unittest.main()
