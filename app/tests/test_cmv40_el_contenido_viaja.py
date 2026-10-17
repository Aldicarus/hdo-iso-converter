"""El arnés modela el CONTENIDO, no solo el formato.

Dos infidelidades que la auditoría de la suite del 2026-10-17 encontró en
los binarios falsos, y los dos casos que ahora se pueden escribir:

  · **`inject-rpu` y `mux` copiaban el RPU tal cual**, ignorando los frames
    del HEVC que procesaban. Un BL o una EL truncada —el demux de Proyecto
    Salvación, 171.679 de 225.177 frames— salía entero del resto del pipeline
    y ningún test podía ver que la Fase H lo rechaza.
  · **El editor solo contaba frames.** Quitar el primero o el último, o
    duplicar el primero o el último, daba el mismo RPU; con el rastro de
    ediciones se puede afirmar QUÉ corrección acabó dentro y en qué orden.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_el_contenido_viaja -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import PhaseTestCase, RpuProps, make_session, write_artifacts  # noqa: E402

FRAMES = 1000
SRC = RpuProps(profile=7, el_type="FEL", cm_version="v2.9", frames=FRAMES)
TGT = RpuProps(profile=7, el_type="FEL", cm_version="v4.0", frames=FRAMES, has_l8=True)


class TestUnBlTruncadoNoLlegaAlNombreFinal(PhaseTestCase):
    """F, G y H en cadena, rama merge, con el BL que dejó un demux muerto."""

    async def test_la_fase_h_lo_rechaza(self):
        from phases.cmv40_pipeline import (run_phase_f_inject, run_phase_g_remux,
                                           run_phase_h_validate)
        for nombre, props in (("RPU_source.bin", SRC), ("RPU_target.bin", TGT)):
            self.tb.define_rpu(nombre, **props.as_dict())
            write_artifacts(self.wd, nombre, props=props)
        write_artifacts(self.wd, "EL.hevc", props=SRC)
        write_artifacts(self.wd, "BL.hevc", props=RpuProps(
            profile=7, el_type="FEL", cm_version="v2.9", frames=FRAMES * 3 // 4))
        write_artifacts(self.wd, "source.mkv", props=SRC)
        s = make_session(self.wd, source_workflow="p7_fel", target_type="generic",
                         target_trust_ok=False, source_frame_count=FRAMES,
                         target_frame_count=FRAMES)
        await run_phase_f_inject(s, self.log)
        salida = await run_phase_g_remux(s, self.log)
        self.tb.define_media(Path(salida).name, duration=7200.0, frames=FRAMES)
        self.assertEqual(self.tb.props_of(self.wd / "DV_dual.hevc").frames, FRAMES * 3 // 4)
        with self.assertRaises(RuntimeError) as ctx:
            await run_phase_h_validate(s, self.log)
        self.assertIn(str(FRAMES * 3 // 4), str(ctx.exception))
        self.assertFalse((self.output_dir / s.output_mkv_name).exists())

    async def test_inject_rpu_respeta_los_frames_de_su_hevc(self):
        from phases.cmv40_pipeline import run_phase_f_inject
        for nombre, props in (("RPU_source.bin", SRC), ("RPU_target.bin", TGT)):
            self.tb.define_rpu(nombre, **props.as_dict())
            write_artifacts(self.wd, nombre, props=props)
        write_artifacts(self.wd, "source.hevc", props=RpuProps(
            profile=7, el_type="FEL", cm_version="v2.9", frames=600))
        s = make_session(self.wd, source_workflow="p7_fel",
                         target_type="trusted_p7_fel_final", target_trust_ok=True)
        await run_phase_f_inject(s, self.log)
        self.assertEqual(self.tb.props_of(self.wd / "source_injected.hevc").frames, 600)


class TestLasCorreccionesSeEncadenanEnOrden(PhaseTestCase):

    async def test_quitar_y_luego_duplicar(self):
        from phases.cmv40_pipeline import run_phase_e_correct_sync
        self.tb.define_rpu("RPU_target.bin", **TGT.as_dict())
        write_artifacts(self.wd, "RPU_target.bin", props=TGT)
        s = make_session(self.wd)
        quitar = {"remove": ["0-9"]}
        duplicar = {"duplicate": [{"source": 989, "offset": 990, "length": 5}]}
        await run_phase_e_correct_sync(s, quitar, self.log)
        await run_phase_e_correct_sync(s, duplicar, self.log)
        self.assertEqual(self.tb.ediciones_de(self.wd / "RPU_synced.bin"),
                         [quitar, duplicar])
        self.assertEqual(self.tb.props_of(self.wd / "RPU_synced.bin").frames,
                         FRAMES - 10 + 5)


if __name__ == "__main__":
    unittest.main()
