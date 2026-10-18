"""Lo que se sabe de un bin no sobrevive a otro bin.

Dos cosas que salieron de la auditoría de la suite (2026-10-17):

  · **La corrección de sync de un bin se aplicaba al siguiente.** La Fase F
    usa `RPU_synced.bin` solo con que el fichero exista, y los endpoints de la
    Fase B no comprueban la fase del proyecto: un bin nuevo provisto sin pasar
    por «rehacer» recibía los frames quitados y duplicados del de antes. El
    reset de la interfaz sí lo borraba.
  · **La línea de trims L8 se leía hasta el primer valor.** El formato real
    lleva la unidad detrás de CADA valor —«100 nits, 600 nits»—, y el patrón
    se detenía en el primer «nits».

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_bin_nuevo -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import PhaseTestCase, RpuProps, make_session, write_artifacts  # noqa: E402

TGT = RpuProps(profile=7, el_type="FEL", cm_version="v4.0", frames=1000, has_l8=True)
CORRECCION = {"steps": [{"remove_start": 24}], "total_removed": 24, "total_duplicated": 0}


class TestUnBinNuevoNoHeredaLaCorreccion(PhaseTestCase):

    def _proyecto_con_correccion(self):
        nuevo = self.tmp / "nuevo.bin"
        write_artifacts(self.tmp, nuevo.name, props=TGT)
        nuevo.write_bytes(b"\x00" * 4096)          # pasa el mínimo de tamaño
        self.tb.define_rpu(nuevo.name, **TGT.as_dict())
        self.tb.define_rpu("RPU_target.bin", **TGT.as_dict())
        for n in ("RPU_target.bin", "RPU_synced.bin", "editor_config.json"):
            write_artifacts(self.wd, n, props=TGT)
        s = make_session(self.wd, target_rpu_source="path",
                         target_rpu_path=str(self.tmp / "anterior.bin"),
                         sync_config=dict(CORRECCION))
        return s, nuevo

    async def test_por_la_fase_b(self):
        from phases.cmv40_pipeline import run_phase_b_target_from_path
        s, nuevo = self._proyecto_con_correccion()
        await run_phase_b_target_from_path(s, str(nuevo), self.log)
        self.assertFalse((self.wd / "RPU_synced.bin").exists(),
                         "la Fase F usaría la corrección del bin anterior")
        self.assertFalse((self.wd / "editor_config.json").exists())
        self.assertIsNone(s.sync_config)

    async def test_por_el_pre_flight(self):
        from phases.cmv40_pipeline import preflight_target_path
        s, nuevo = self._proyecto_con_correccion()
        await preflight_target_path(s, str(nuevo), self.log)
        self.assertFalse((self.wd / "RPU_synced.bin").exists())

    async def test_por_el_pre_flight_desde_otro_mkv_por_el_pipe(self):
        """El pipe ffmpeg|extract-rpu escribe `RPU_target.bin` directamente, y
        el descarte solo estaba en el camino en dos pasos (auditoría con
        Codex del 2026-10-17, reproducido)."""
        from phases.cmv40_pipeline import preflight_target_mkv
        s, _ = self._proyecto_con_correccion()
        otro = self.tmp / "Otro.mkv"
        write_artifacts(self.tmp, otro.name, props=TGT)
        self.tb.define_media(otro.name, duration=60.0, frames=1000)
        await preflight_target_mkv(s, str(otro), self.log)
        self.assertTrue(self.tb.find("ffmpeg"), "no se ha extraído nada")
        self.assertTrue(any("pipe:1" in " ".join(c.argv) for c in self.tb.find("ffmpeg")),
                        "el escenario no ha ido por el pipe")
        self.assertFalse((self.wd / "RPU_synced.bin").exists(),
                         "la Fase F inyectaría la corrección del bin anterior")
        self.assertIsNone(s.sync_config)

    async def test_el_mismo_bin_la_conserva(self):
        """La Fase B que vuelve a analizar el bin que ya bajó el pre-flight no
        cambia de bin: la corrección sigue siendo suya."""
        from phases.cmv40_pipeline import run_phase_b_target_from_path
        s, nuevo = self._proyecto_con_correccion()
        s.target_rpu_path = str(nuevo)
        write_artifacts(self.wd, "RPU_target.bin", props=TGT)
        (self.wd / "RPU_target.bin").write_bytes(b"\x00" * 4096)
        await run_phase_b_target_from_path(s, str(nuevo), self.log)
        self.assertTrue((self.wd / "RPU_synced.bin").exists())
        self.assertEqual(s.sync_config, CORRECCION)


class TestUnaDescargaFallidaNoSeLlevaLaCorreccion(PhaseTestCase):
    """Desde Drive el descarte iba ANTES de descargar: un 404 o la cuota
    agotada —lo normal con el repo— dejaba el bin de antes sin su corrección
    (revisión con Codex de la v3.4.0). Se descarga a un `.part` y solo con el
    bin entero se descarta y se pone en su sitio."""

    def _proyecto(self):
        self.tb.define_rpu("RPU_target.bin", **TGT.as_dict())
        for n in ("RPU_target.bin", "RPU_synced.bin", "editor_config.json"):
            write_artifacts(self.wd, n, props=TGT)
        return make_session(self.wd, target_rpu_source="drive",
                            target_rpu_path="drive://anterior/anterior.bin",
                            sync_config=dict(CORRECCION))

    @staticmethod
    async def _drive_caido(file_id, dest, progress_cb=None):
        raise RuntimeError("Drive download falló (HTTP 404)")

    @staticmethod
    async def _drive_bien(file_id, dest, progress_cb=None):
        Path(dest).write_bytes(b"\x00" * 4096)
        return 4096

    async def _lanzar(self, fase, descarga):
        from unittest import mock
        from phases import cmv40_pipeline as P
        s = self._proyecto()
        f = {"pre-flight": P.preflight_target_drive,
             "fase B": P.run_phase_b_target_from_drive}[fase]
        with mock.patch("services.rec999_drive.download_file", descarga):
            try:
                await f(s, "nuevo", "nuevo.bin", self.log)
            except RuntimeError:
                pass
        return s

    async def test_con_drive_caido_la_correccion_sigue(self):
        for fase in ("pre-flight", "fase B"):
            with self.subTest(fase=fase):
                s = await self._lanzar(fase, self._drive_caido)
                self.assertTrue((self.wd / "RPU_synced.bin").exists(),
                                "la corrección del bin que sigue puesto se ha perdido")
                self.assertTrue((self.wd / "editor_config.json").exists())
                self.assertEqual(s.sync_config, CORRECCION)
                self.assertTrue((self.wd / "RPU_target.bin").exists())

    async def test_con_el_bin_entero_se_descarta(self):
        for fase in ("pre-flight", "fase B"):
            with self.subTest(fase=fase):
                s = await self._lanzar(fase, self._drive_bien)
                self.assertFalse((self.wd / "RPU_synced.bin").exists())
                self.assertIsNone(s.sync_config)
                self.assertFalse((self.wd / "RPU_target.bin.part").exists())
                self.assertEqual((self.wd / "RPU_target.bin").stat().st_size, 4096)


class TestUnBinEnMayusculasSeAcepta(PhaseTestCase):
    """El listado de la carpeta local no distingue mayúsculas y ofrecía
    `Peli.BIN`; el pre-flight y la Fase B lo rechazaban por el sufijo
    (auditoría con Codex del 2026-10-17)."""

    def _bin(self):
        b = self.tmp / "Peli.BIN"
        write_artifacts(self.tmp, b.name, props=TGT)
        self.tb.define_rpu(b.name, **TGT.as_dict())
        self.tb.define_rpu("RPU_target.bin", **TGT.as_dict())
        return b

    async def test_el_pre_flight(self):
        from phases.cmv40_pipeline import preflight_target_path
        await preflight_target_path(make_session(self.wd), str(self._bin()), self.log)
        self.assertTrue((self.wd / "RPU_target.bin").exists())

    async def test_la_fase_b(self):
        from phases.cmv40_pipeline import run_phase_b_target_from_path
        await run_phase_b_target_from_path(make_session(self.wd), str(self._bin()), self.log)
        self.assertTrue((self.wd / "RPU_target.bin").exists())


class TestLosTrimsL8SeLeenTodos(unittest.TestCase):

    def test_el_formato_real_con_la_unidad_en_cada_valor(self):
        from phases.phase_a import _parse_dovi_summary
        info = _parse_dovi_summary(
            "Summary:\n  L8 trims: 100 nits, 600 nits, 1000 nits\n  L9 MDP: BT.2020\n")
        self.assertEqual(info.l8_trim_nits, [100, 600, 1000])
        self.assertEqual(info.l8_trim_count, 3)

    def test_uno_solo(self):
        from phases.phase_a import _parse_dovi_summary
        info = _parse_dovi_summary("Summary:\n  L8 trims: 100 nits\n")
        self.assertEqual(info.l8_trim_nits, [100])

    def test_no_se_mete_en_la_linea_siguiente(self):
        from phases.phase_a import _parse_dovi_summary
        info = _parse_dovi_summary("  L8 trims: 100 nits\n  L9 MDP: DCI-P3 D65\n")
        self.assertEqual(info.l8_trim_nits, [100])


if __name__ == "__main__":
    unittest.main()
