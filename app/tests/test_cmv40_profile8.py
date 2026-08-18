"""
Los workflows single-layer deben inyectar un RPU Profile 8.1.

Bug real ("Te van a matar", 2026-08-15): el workflow p7_mel descarta el EL y
remuxa solo la BL, pero el RPU merged conservaba el Profile 7 del source. El
MKV final quedaba anunciándose como `dvhe.07 / BL+EL+RPU` — dual-layer sin
tener capa de mejora — pese a que la pista se llamaba "P8.1 CMv4.0". Un
comentario del código afirmaba que el reproductor lo leería como
"P8.1-equivalente"; MediaInfo sobre el fichero real demostró lo contrario.

`_ensure_profile8_rpu` convierte el RPU con `dovi_tool editor {"mode": 2}`,
verificado sobre el RPU real de esa película: P7 MEL → P8 conservando CM v4.0,
136033 frames y 1101 scene cuts.
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import phases.cmv40_pipeline as pipeline  # noqa: E402


def _summary(profile: int, el_type: str = "", frames: int = 136033,
             scenes: int = 1101) -> str:
    """Texto tal cual lo escupe `dovi_tool info --summary`."""
    prof = f"{profile} ({el_type})" if el_type else str(profile)
    return (
        "Parsing RPU file...\n\nSummary:\n"
        f"  Frames: {frames}\n"
        f"  Profile: {prof}\n"
        "  DM version: 2 (CM v4.0)\n"
        f"  Scene/shot count: {scenes}\n"
    )


class TestEnsureProfile8(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.wd = Path(self._tmp.name)
        self.rpu = self.wd / "RPU_merged.bin"
        self.rpu.write_bytes(b"x" * 4096)
        self.calls = []
        self._orig_run = pipeline._run
        self.logs = []

    def tearDown(self):
        pipeline._run = self._orig_run
        self._tmp.cleanup()

    async def _log(self, msg):
        self.logs.append(msg)

    def _install_run(self, before_profile, el_type="MEL", *,
                     editor_rc=0, after_frames=136033, write_output=True):
        async def _fake_run(cmd, **kwargs):
            self.calls.append(cmd)
            if "info" in cmd:
                target = Path(cmd[-1])
                if target.name.endswith("_p81.bin"):
                    return 0, _summary(8, "", after_frames), ""
                return 0, _summary(before_profile, el_type), ""
            if "editor" in cmd:
                if write_output and editor_rc == 0:
                    out = Path(cmd[cmd.index("-o") + 1])
                    out.write_bytes(b"y" * 4096)
                return editor_rc, "", "boom" if editor_rc else ""
            raise AssertionError(f"comando inesperado: {cmd}")
        pipeline._run = _fake_run

    async def test_rpu_profile7_se_convierte(self):
        self._install_run(7, "MEL")
        out = await pipeline._ensure_profile8_rpu(self.rpu, self.wd, self._log)
        self.assertNotEqual(out, self.rpu)
        self.assertTrue(out.name.endswith("_p81.bin"))
        self.assertTrue(any("editor" in c for c in self.calls))
        # El JSON de config debe pedir exactamente mode 2
        cfg = (self.wd / "_profile8_mode.json").read_text(encoding="utf-8")
        self.assertIn('"mode": 2', cfg)
        self.assertTrue(any("Profile 8" in m for m in self.logs))

    async def test_rpu_ya_profile8_no_se_toca(self):
        self._install_run(8)
        out = await pipeline._ensure_profile8_rpu(self.rpu, self.wd, self._log)
        self.assertEqual(out, self.rpu, "no debe convertir lo que ya es P8")
        self.assertFalse(any("editor" in c for c in self.calls))
        self.assertEqual(self.logs, [], "sin ruido en el log cuando no hay nada que hacer")

    async def test_si_el_editor_falla_se_sigue_con_el_original(self):
        # El vídeo es correcto igualmente: no se aborta el pipeline por esto.
        self._install_run(7, "MEL", editor_rc=1, write_output=False)
        out = await pipeline._ensure_profile8_rpu(self.rpu, self.wd, self._log)
        self.assertEqual(out, self.rpu)
        self.assertTrue(any("⚠" in m for m in self.logs))

    async def test_si_cambia_el_frame_count_se_descarta_la_conversion(self):
        # Un RPU con distinto número de frames rompería el inject: preferimos
        # la señalización imperfecta antes que un fichero inválido.
        self._install_run(7, "MEL", after_frames=136000)
        out = await pipeline._ensure_profile8_rpu(self.rpu, self.wd, self._log)
        self.assertEqual(out, self.rpu)
        self.assertTrue(any("frame count" in m for m in self.logs))


# El guard que comprobaba por `grep` del fuente que la Fase F llama a
# `_ensure_profile8_rpu` se retiró: `test_cmv40_fase_f_matriz` ejecuta la fase
# y comprueba el efecto observable — que el RPU dentro del HEVC producido
# declara Profile 8 — en vez de la presencia de un identificador en un rango
# de caracteres del fichero.


if __name__ == "__main__":
    unittest.main()
