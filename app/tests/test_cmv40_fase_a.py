"""La Fase A ejecutada de verdad — y el aviso de L2 que mentía.

`run_phase_a_analyze_source` no la ejecutaba ningún test. El único fichero
que la mencionaba era `test_cmv40_endpoints`, y solo para comprobar qué fase
pide arrancar el endpoint.

Lo que salió de no ejecutarla: el `⚠ No se pudo extraer la lista de combos L2
del source` estaba **fuera** del `if source_analysis.total_frames > 0`, así
que se emitía en TODOS los jobs — incluso justo debajo de la línea que
acababa de decir cuántos combos había encontrado. Visto en el NAS con The
Mandalorian and Grogu (2026-09-04):

    [Fase A] L2 source: 3545 combos únicos · peaks [2081, 2851, 3079]
    [Fase A] 🎯 Comparación L2: IDENTICAL — L2 byte-a-byte idéntico …
    [Fase A] ⚠ No se pudo extraer la lista de combos L2 del source …

Dos líneas seguidas contándose lo contrario, y la que asusta es la falsa. Es
la regla de «los textos describen el estado» aplicada al revés: el log decía
que había fallado algo que había ido bien.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_fase_a -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
for _p in (str(APP_DIR), str(APP_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cmv40_harness import (  # noqa: E402
    PhaseTestCase, RpuProps, make_session, write_artifacts,
)

FRAMES = 1000
SRC = RpuProps(profile=7, el_type="FEL", cm_version="v2.9", frames=FRAMES)
TGT = RpuProps(profile=7, el_type="FEL", cm_version="v4.0", frames=FRAMES,
               has_l8=True)

AVISO_L2 = "No se pudo extraer la lista de combos L2 del source"


class FaseACase(PhaseTestCase):

    def prepare(self, **overrides):
        session = make_session(self.wd, **overrides)
        mkv = Path(session.source_mkv_path)
        write_artifacts(self.wd, mkv.name, props=SRC)
        self.tb.define_media(mkv.name, duration=7200.0, frames=FRAMES)
        self.tb.define_rpu("RPU_source.bin", **SRC.as_dict())
        self.tb.define_rpu("RPU_target.bin", **TGT.as_dict())
        write_artifacts(self.wd, "RPU_target.bin", props=TGT)
        return session

    async def correr(self, **overrides):
        from phases.cmv40_pipeline import run_phase_a_analyze_source
        session = self.prepare(**overrides)
        await run_phase_a_analyze_source(session, log_callback=self.log)
        return session, "\n".join(self.log.lines)


class TestElAvisoDeL2(FaseACase):

    async def test_no_avisa_de_fallo_cuando_el_export_funciona(self):
        _, log = await self.correr()
        self.assertIn("L2 source:", log, "el export debería haber funcionado")
        self.assertNotIn(AVISO_L2, log,
                         "avisa de un fallo que no ha ocurrido")

    async def test_avisa_cuando_el_export_falla_de_verdad(self):
        self.tb.fail("dovi_tool", "export")
        _, log = await self.correr()
        self.assertNotIn("L2 source:", log)
        self.assertIn(AVISO_L2, log,
                      "sin combos L2 hay que decirlo")

    async def test_un_export_fallido_no_tumba_la_fase(self):
        """El análisis L2 es informativo: alimenta la recomendación
        Mantener/Inyectar, no la decisión de si el pipeline puede seguir."""
        self.tb.fail("dovi_tool", "export")
        session, log = await self.correr()
        self.assertIn("✓ RPU analizado", log)
        self.assertEqual(session.source_frame_count, FRAMES)


class TestLoQueLaFaseDeja(FaseACase):
    """Los artefactos y campos de los que dependen las fases siguientes."""

    async def test_produce_el_hevc_y_el_rpu(self):
        await self.correr()
        for nombre in ("source.hevc", "RPU_source.bin"):
            self.assertTrue((self.wd / nombre).exists(), nombre)

    async def test_rellena_el_dv_del_source(self):
        session, _ = await self.correr()
        self.assertIsNotNone(session.source_dv_info)
        self.assertEqual(session.source_dv_info.profile, 7)
        self.assertEqual(session.source_dv_info.el_type, "FEL")
        self.assertEqual(session.source_dv_info.cm_version, "v2.9")
        self.assertEqual(session.source_frame_count, FRAMES)

    async def test_clasifica_el_workflow_como_p7_fel(self):
        session, log = await self.correr()
        self.assertEqual(session.source_workflow, "p7_fel")
        self.assertIn("workflow P7 FEL", log)


if __name__ == "__main__":
    unittest.main()
