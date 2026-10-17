"""Los pasos del análisis de Tab 1 no esperan para siempre, y lo que matan
no se queda vivo.

`MEDIAINFO_TIMEOUT`, `FFMPEG_EL_TIMEOUT` y `DOVI_TOOL_TIMEOUT` estaban
definidas y no las usaba nadie: un MediaInfo colgado en una lectura del
origen dejaba el análisis —y la creación de una serie, que pasa por la
cola— esperando para siempre. El `ffprobe` de la duración sí tenía tope,
pero al agotarse devolvía 0.0 dejando el proceso vivo. Y la muestra de la EL
y su RPU se llamaban por el pid del proceso, que comparten dos análisis a la
vez (auditoría con Codex del 2026-10-17).

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_topes_del_analisis -v
"""
import asyncio
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import FakeToolbox  # noqa: E402

TARDA = 4.0


class TopesCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        from phases import phase_a, phase_e
        self.a, self.e = phase_a, phase_e
        self.tmp = Path(tempfile.mkdtemp(prefix="topes_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp / "tb").install()
        self.addCleanup(self.tb.uninstall)
        self.m2ts = self.tmp / "00800.m2ts"
        self.m2ts.write_bytes(b"\x00" * 8192)
        self.trabajo = self.tmp / "trabajo"
        self.trabajo.mkdir()
        self.parchear(phase_a, "TMP_DIR", str(self.trabajo))

    def parchear(self, modulo, nombre, valor):
        self.addCleanup(setattr, modulo, nombre, getattr(modulo, nombre))
        setattr(modulo, nombre, valor)


class TestMediainfo(TopesCase):

    async def test_colgado_devuelve_vacio_sin_esperar(self):
        self.tb.retrasar_cuando_arg("mediainfo", "--Output=JSON", TARDA)
        self.parchear(self.a, "MEDIAINFO_TIMEOUT", 0.5)
        t0 = time.monotonic()
        r = await self.a.run_mediainfo(str(self.m2ts))
        self.assertLess(time.monotonic() - t0, TARDA - 1, "ha esperado al MediaInfo colgado")
        self.assertEqual(r.tracks, [])


class TestLaMuestraDeLaEl(TopesCase):

    async def test_un_ffmpeg_colgado_muere_y_no_deja_la_muestra(self):
        """Muerto, no abandonado: el ffmpeg falso escribe su salida DESPUÉS
        de la espera, así que uno que siguiera vivo dejaría la muestra en
        /mnt/tmp después de que la función la limpiara."""
        self.tb.retrasar_cuando_arg("ffmpeg", "hevc_mp4toannexb", TARDA)
        self.parchear(self.a, "FFMPEG_EL_TIMEOUT", 0.5)
        t0 = time.monotonic()
        self.assertIsNone(await self.a.run_dovi_analysis(str(self.m2ts)))
        self.assertLess(time.monotonic() - t0, TARDA - 1)
        await asyncio.sleep(TARDA)
        self.assertEqual(list(self.trabajo.iterdir()), [], "el ffmpeg seguía vivo")

    async def test_dos_analisis_no_comparten_la_muestra(self):
        await self.a.run_dovi_analysis(str(self.m2ts))
        await self.a.run_dovi_analysis(str(self.m2ts))
        salidas = [c.argv[-1] for c in self.tb.find("ffmpeg")]
        self.assertEqual(len(salidas), 2)
        self.assertNotEqual(salidas[0], salidas[1])


class TestLaDuracionConFfprobe(TopesCase):

    async def test_colgado_devuelve_cero_sin_esperar(self):
        self.tb.retrasar_cuando_arg("ffprobe", "format=duration", TARDA)
        self.parchear(self.a, "FFPROBE_DURACION_TIMEOUT", 0.5)
        t0 = time.monotonic()
        self.assertEqual(await self.a._ffprobe_duration_seconds(str(self.m2ts)), 0.0)
        self.assertLess(time.monotonic() - t0, TARDA - 1)


class TestLaFaseE(TopesCase):

    async def test_un_identify_colgado_es_un_error_y_no_una_lista_vacia(self):
        """Un mapa vacío haría que el matcher no encontrara nada: mejor un
        error que un MKV sin pistas."""
        self.tb.retrasar_cuando_arg("mkvmerge", "--identify", TARDA)
        self.parchear(self.e, "IDENTIFY_TIMEOUT", 0.5)
        t0 = time.monotonic()
        with self.assertRaises(RuntimeError):
            await self.e._identify_tracks(str(self.m2ts))
        self.assertLess(time.monotonic() - t0, TARDA - 1)


if __name__ == "__main__":
    unittest.main()
