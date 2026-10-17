"""El sniff del MKV origen en el pre-flight, ejecutado.

`preflight_source` (30 s del MKV origen → `extract-rpu` → ¿hay Dolby
Vision?) no lo ejecutaba ningún test: los del pre-flight lo sustituyen por
un mock. Con el `ffmpeg` falso ignorando `-t` y el códec sobre un MKV, quitar
el `-t 30` o el `-c:v copy` pasaba en verde (revisión de Codex de la suite,
2026-10-17).

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_preflight_source -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import PhaseTestCase, RpuProps, make_session, write_artifacts  # noqa: E402


class TestElSniffDelOrigen(PhaseTestCase):

    def _sesion(self, props):
        write_artifacts(self.wd, "source.mkv", props=props)
        return make_session(self.wd)

    async def test_un_origen_con_dv_pasa(self):
        from phases.cmv40_pipeline import preflight_source
        s = self._sesion(RpuProps(profile=7, el_type="FEL"))
        await preflight_source(s, self.log)
        self.assertTrue(s.source_preflight_ok)
        self.assertFalse(list(self.wd.glob("_sniff*")), "el sniff deja restos")

    async def test_un_origen_sin_dv_se_para_antes_de_la_fase_a(self):
        from phases.cmv40_pipeline import preflight_source
        s = make_session(self.wd)
        # Un MKV cuyo vídeo no lleva RPU (el `sin_rpu` del arnés).
        (self.wd / "source.mkv").write_bytes(
            b'#HARNESS-PROPS {"sin_rpu": true}\n' + b"\x00" * (2 * 1024 * 1024))
        with self.assertRaises(RuntimeError):
            await preflight_source(s, self.log)
        self.assertFalse(s.source_preflight_ok)
        self.assertFalse(list(self.wd.glob("_sniff*")), "el sniff deja restos")

    async def test_son_treinta_segundos_sin_recodificar(self):
        from phases.cmv40_pipeline import preflight_source
        s = self._sesion(RpuProps(profile=7, el_type="FEL"))
        await preflight_source(s, self.log)
        [m] = self.tb.find("ffmpeg")
        self.assertLessEqual(float(m.opt("-t")), 30, m.argv)
        self.assertEqual(m.opt("-c:v"), "copy", "recodificar pierde el RPU")


if __name__ == "__main__":
    unittest.main()
