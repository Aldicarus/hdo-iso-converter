"""El pipe de la Fase A, ejercitado de punta a punta.

`_ffmpeg_extract_rpu_piped` conecta ffmpeg y `dovi_tool extract-rpu` por un
`os.pipe()` para que las dos pasadas grandes de la Fase A —una limitada por
disco, la otra por CPU— corran a la vez en lugar de una detrás de la otra.
Sobre John Wick 4 (243.552 frames, MKV de 88 GB) eso es la diferencia entre
946 s en serie y ~574 s solapados.

Hasta ahora ningún test llegaba a ejecutarlo: el binario falso de ffmpeg
trataba `pipe:1` como una ruta y creaba un fichero con ese nombre, así que
el pipe se quedaba vacío, la función devolvía False y todo el mundo caía al
camino en dos pasos. Verde, pero probando la rama que NO corre en el NAS.

Lo que se comprueba aquí:
  - el camino piped produce el RPU, y con el muxer `tee` también el HEVC;
  - el RPU que sale contiene el DV del MKV de entrada (que es el punto: si
    el pipe perdiera el contenido, el resto del pipeline trabajaría sobre
    metadata equivocada sin que nada avisase);
  - ante un fallo devuelve False SIN lanzar, porque el contrato con el
    caller es que pueda recurrir al camino clásico.
"""
import asyncio
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
for _p in (str(APP_DIR), str(APP_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cmv40_harness import (  # noqa: E402
    CollectingLog, FakeToolbox, PhaseTestCase, RpuProps, write_artifacts,
)


class TestPipeFaseA(PhaseTestCase):

    def setUp(self):
        super().setUp()
        self.mkv = self.tmp / "Origen.mkv"
        self.props = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                              frames=243552, scenes=1101, has_l8=True)
        write_artifacts(self.tmp, self.mkv.name, props=self.props)

    async def _piped(self, hevc_out=None, **kw):
        from phases.cmv40_pipeline import _ffmpeg_extract_rpu_piped
        rpu = self.tmp / "RPU_source.bin"
        log = CollectingLog()
        ok = await _ffmpeg_extract_rpu_piped(
            str(self.mkv), rpu, hevc_out=hevc_out, log_callback=log, **kw)
        return ok, rpu, log

    async def test_el_camino_piped_produce_el_rpu(self):
        ok, rpu, _ = await self._piped()
        self.assertTrue(ok, "el pipe debe completarse con las herramientas OK")
        self.assertTrue(rpu.exists())
        self.assertGreater(rpu.stat().st_size, 0)

    async def test_con_tee_produce_tambien_el_hevc(self):
        hevc = self.tmp / "source.hevc"
        ok, rpu, _ = await self._piped(hevc_out=hevc)
        self.assertTrue(ok)
        self.assertTrue(hevc.exists(), "el muxer tee escribe fichero y pipe")
        self.assertTrue(rpu.exists())

    async def test_el_rpu_conserva_el_dv_del_mkv_de_entrada(self):
        # Lo que de verdad importa: que por el pipe no se pierda el
        # contenido. Un RPU con metadata de otro disco pasaría los guards
        # de tamaño y reventaría mucho más tarde, en la Fase H.
        _, rpu, _ = await self._piped(hevc_out=self.tmp / "source.hevc")
        salida = self.tb.props_of(rpu)
        self.assertIsNotNone(salida)
        self.assertEqual(salida.profile, 7)
        self.assertEqual(salida.el_type, "FEL")
        self.assertEqual(salida.cm_version, "v4.0")
        self.assertEqual(salida.frames, 243552)
        self.assertTrue(salida.has_l8)

    async def test_no_deja_un_fichero_llamado_pipe_1(self):
        antes = set(self.tmp.rglob("*"))
        await self._piped(hevc_out=self.tmp / "source.hevc")
        nuevos = {p.name for p in set(self.tmp.rglob("*")) - antes}
        self.assertNotIn("pipe:1", nuevos)
        self.assertFalse([n for n in nuevos if n.startswith("pipe:")],
                         "`pipe:1` es stdout, no una ruta")

    async def test_si_ffmpeg_falla_devuelve_false_sin_lanzar(self):
        self.tb.fail("ffmpeg", "*", rc=1, stderr="Invalid data found")
        ok, _, _ = await self._piped()
        self.assertFalse(ok, "el caller necesita un False para poder "
                             "recurrir al camino en dos pasos")

    async def test_si_extract_rpu_falla_devuelve_false_sin_lanzar(self):
        self.tb.fail("dovi_tool", "extract-rpu", rc=1, stderr="no RPU found")
        ok, _, _ = await self._piped()
        self.assertFalse(ok)

    async def test_registra_el_comando_completo_en_el_log(self):
        # El log del NAS es la única forma de saber a posteriori si un job
        # fue por el pipe o por el camino largo.
        _, _, log = await self._piped(hevc_out=self.tmp / "source.hevc")
        texto = "\n".join(log.lines)
        self.assertIn("|", texto, "debe verse que son dos procesos en pipe")
        self.assertIn("extract-rpu", texto)

    async def test_una_sola_pasada_de_ffmpeg(self):
        # El ahorro viene de no leer el MKV dos veces.
        await self._piped(hevc_out=self.tmp / "source.hevc")
        self.assertEqual(len(self.tb.find("ffmpeg")), 1)
        self.assertEqual(len(self.tb.find("dovi_tool", "extract-rpu")), 1)


if __name__ == "__main__":
    unittest.main()
