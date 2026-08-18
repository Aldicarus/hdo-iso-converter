"""
Fase F ejecutada de verdad, rama por rama de la matriz de workflows.

La Fase F decide tres cosas a partir de (source_workflow, target_type,
trust): qué RPU inyecta, en qué HEVC, y si antes hay que mergear o
convertir el RPU a Profile 8.1. Son ~30 puntos de ramificación repartidos
por el pipeline y hasta ahora no había ni un test que los ejecutara: lo
único que existía era un `src.index("async def run_phase_f_inject")` +
`assertIn` sobre el texto del fuente, que pasa en verde aunque la decisión
esté invertida.

Estos tests invocan `run_phase_f_inject` con binarios falsos (ver
`cmv40_harness`) y afirman sobre las invocaciones reales y sobre el RPU
que acaba dentro del HEVC producido.

El caso que motivó buena parte de esto es "Te van a matar" (2026-08-15):
en `p7_mel` el merge conservaba el Profile 7 del source, el mux remuxaba
solo la BL, y el MKV final se anunciaba como `dvhe.07 / BL+EL+RPU` — un
dual-layer sin capa de mejora. Lo cubre
`TestProfile8EnSingleLayer.test_p7_mel_inyecta_rpu_profile_8`.
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import (  # noqa: E402
    CollectingLog, PhaseTestCase, RpuProps, make_session, write_artifacts,
)

FRAMES = 1000

# Los cuatro RPU de partida que se combinan en los escenarios.
SRC_FEL = RpuProps(profile=7, el_type="FEL", cm_version="v2.9", frames=FRAMES)
SRC_MEL = RpuProps(profile=7, el_type="MEL", cm_version="v2.9", frames=FRAMES)
SRC_P8 = RpuProps(profile=8, el_type="", cm_version="v2.9", frames=FRAMES)

TGT_P7_FEL_V40 = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                          frames=FRAMES, has_l8=True)
TGT_P7_MEL_V40 = RpuProps(profile=7, el_type="MEL", cm_version="v4.0",
                          frames=FRAMES, has_l8=True)
TGT_P8_V40 = RpuProps(profile=8, el_type="", cm_version="v4.0",
                      frames=FRAMES, has_l8=True)


class FaseFTestCase(PhaseTestCase):
    """Monta el workdir con los artefactos que cada workflow necesita."""

    def build(self, *, source_workflow: str, target_type: str,
              source_props: RpuProps, target_props: RpuProps,
              trust_ok: bool = True, trust_override: str = "auto",
              artifacts: tuple[str, ...] = ()):
        self.tb.define_rpu("RPU_source.bin", **source_props.as_dict())
        self.tb.define_rpu("RPU_target.bin", **target_props.as_dict())
        write_artifacts(self.wd, "RPU_source.bin", props=source_props)
        write_artifacts(self.wd, "RPU_target.bin", props=target_props)
        for name in artifacts:
            write_artifacts(self.wd, name, props=source_props)
        return make_session(
            self.wd,
            source_workflow=source_workflow,
            target_type=target_type,
            target_trust_ok=trust_ok,
            trust_override=trust_override,
            source_frame_count=FRAMES,
            target_frame_count=FRAMES,
        )

    async def run_f(self, session):
        from phases.cmv40_pipeline import run_phase_f_inject
        await run_phase_f_inject(session, self.log)
        return self.tb.one("dovi_tool", "inject-rpu")

    # Atajos de lectura sobre lo que hizo la fase
    @property
    def merged(self) -> bool:
        """True si hubo transferencia de levels CMv4.0 (merge)."""
        return any(c.json_args.get("allow_cmv4_transfer")
                   for c in self.tb.find("dovi_tool", "editor"))

    @property
    def merge_levels(self) -> list[int]:
        for c in self.tb.find("dovi_tool", "editor"):
            if c.json_args.get("allow_cmv4_transfer"):
                return c.json_args.get("rpu_levels") or []
        return []

    @property
    def converted_to_p8(self) -> bool:
        """True si se pidió la conversión a Profile 8.1 (`{"mode": 2}`)."""
        return any(c.json_args.get("mode") == 2
                   for c in self.tb.find("dovi_tool", "editor"))


class TestDropInFel(FaseFTestCase):
    """p7_fel + bin P7 FEL CMv4.0 + gates OK → inject directo sobre BL+EL."""

    async def test_inyecta_el_bin_tal_cual_sobre_source_hevc(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p7_fel_final",
            source_props=SRC_FEL, target_props=TGT_P7_FEL_V40,
            artifacts=("source.hevc",),
        )
        inj = await self.run_f(session)
        self.assertEqual(inj.opt_name("-i"), "source.hevc")
        self.assertEqual(inj.opt_name("--rpu-in"), "RPU_target.bin")
        self.assertEqual(inj.opt_name("-o"), "source_injected.hevc")

    async def test_no_mergea_ni_convierte(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p7_fel_final",
            source_props=SRC_FEL, target_props=TGT_P7_FEL_V40,
            artifacts=("source.hevc",),
        )
        await self.run_f(session)
        self.assertFalse(self.merged, "el drop-in no debe mergear nada")
        self.assertFalse(self.converted_to_p8,
                         "el drop-in preserva el Profile 7 FEL: no se convierte")

    async def test_marca_el_merge_como_omitido(self):
        # `output_workflow` se deriva de este marcador al llegar a DONE:
        # su ausencia haría que un drop-in se registrara como restore_merge.
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p7_fel_final",
            source_props=SRC_FEL, target_props=TGT_P7_FEL_V40,
            artifacts=("source.hevc",),
        )
        await self.run_f(session)
        self.assertIn("merge_cmv40_transfer", session.phases_skipped)

    async def test_sin_source_hevc_falla_pidiendo_fase_a(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p7_fel_final",
            source_props=SRC_FEL, target_props=TGT_P7_FEL_V40,
            artifacts=(),
        )
        from phases.cmv40_pipeline import run_phase_f_inject
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_f_inject(session, self.log)
        self.assertIn("source.hevc", str(cm.exception))
        self.assertFalse(self.tb.ran("dovi_tool", "inject-rpu"))


class TestTrustDegradadoNoEsDropIn(FaseFTestCase):
    """El drop-in exige gates OK y modo auto. Sin eso, se pasa por el merge."""

    async def test_gates_no_ok_fuerza_merge(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p7_fel_final",
            source_props=SRC_FEL, target_props=TGT_P7_FEL_V40,
            trust_ok=False, artifacts=("EL.hevc", "source.hevc"),
        )
        inj = await self.run_f(session)
        self.assertTrue(self.merged)
        self.assertEqual(inj.opt_name("-i"), "EL.hevc")
        self.assertEqual(inj.opt_name("--rpu-in"), "RPU_merged.bin")

    async def test_force_interactive_fuerza_merge(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p7_fel_final",
            source_props=SRC_FEL, target_props=TGT_P7_FEL_V40,
            trust_override="force_interactive",
            artifacts=("EL.hevc", "source.hevc"),
        )
        inj = await self.run_f(session)
        self.assertTrue(self.merged)
        self.assertEqual(inj.opt_name("-i"), "EL.hevc")


class TestP7FelMerge(FaseFTestCase):
    """p7_fel + target no drop-in → merge y inyección en la EL, preservando FEL."""

    async def test_inyecta_el_merged_en_la_el(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p8_source",
            source_props=SRC_FEL, target_props=TGT_P8_V40,
            artifacts=("EL.hevc",),
        )
        inj = await self.run_f(session)
        self.assertEqual(inj.opt_name("-i"), "EL.hevc")
        self.assertEqual(inj.opt_name("--rpu-in"), "RPU_merged.bin")
        self.assertEqual(inj.opt_name("-o"), "EL_injected.hevc")

    async def test_preserva_profile_7_fel_sin_convertir(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p8_source",
            source_props=SRC_FEL, target_props=TGT_P8_V40,
            artifacts=("EL.hevc",),
        )
        inj = await self.run_f(session)
        self.assertFalse(self.converted_to_p8,
                         "p7_fel sigue siendo dual-layer: convertir a P8 rompería la FEL")
        props = self.tb.props_of(Path(inj.opt("-o")))
        self.assertEqual(props.profile, 7)
        self.assertEqual(props.el_type, "FEL")
        self.assertEqual(props.cm_version, "v4.0")

    async def test_levels_fel_incluyen_l1_l2_l6(self):
        # Con source FEL, bbeny123/remuxer.sh transfiere también L1/L2/L6
        # porque el L1 del BD describe el BL+EL combinado.
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p8_source",
            source_props=SRC_FEL, target_props=TGT_P8_V40,
            artifacts=("EL.hevc",),
        )
        await self.run_f(session)
        self.assertEqual(self.merge_levels, [1, 2, 3, 6, 8, 9, 10, 11, 254])

    async def test_sin_el_hevc_falla_pidiendo_fase_c(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p8_source",
            source_props=SRC_FEL, target_props=TGT_P8_V40, artifacts=(),
        )
        from phases.cmv40_pipeline import run_phase_f_inject
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_f_inject(session, self.log)
        self.assertIn("EL.hevc", str(cm.exception))


class TestProfile8EnSingleLayer(FaseFTestCase):
    """Regresión de "Te van a matar" (2026-08-15).

    Los workflows single-layer (p7_mel descarta el EL, p8 nunca lo tuvo)
    producen un HEVC sin capa de mejora. Si el RPU inyectado sigue
    declarando Profile 7, el fichero se anuncia como dual-layer y un
    reproductor DV espera una EL que no existe.
    """

    async def test_p7_mel_inyecta_rpu_profile_8(self):
        session = self.build(
            source_workflow="p7_mel", target_type="trusted_p7_mel_final",
            source_props=SRC_MEL, target_props=TGT_P7_MEL_V40,
            artifacts=("BL.hevc",),
        )
        inj = await self.run_f(session)
        self.assertTrue(self.converted_to_p8,
                        "sin la conversión el MKV se anuncia como dvhe.07 sin EL")
        props = self.tb.props_of(Path(inj.opt("-o")))
        self.assertEqual(props.profile, 8, "el stream single-layer debe declarar P8")
        self.assertEqual(props.el_type, "")
        self.assertEqual(props.cm_version, "v4.0")

    async def test_p8_inyecta_rpu_profile_8(self):
        session = self.build(
            source_workflow="p8", target_type="generic",
            source_props=SRC_P8, target_props=TGT_P8_V40,
            artifacts=("source.hevc",),
        )
        inj = await self.run_f(session)
        props = self.tb.props_of(Path(inj.opt("-o")))
        self.assertEqual(props.profile, 8)
        self.assertEqual(props.cm_version, "v4.0")

    async def test_la_conversion_preserva_frame_count_y_cm(self):
        session = self.build(
            source_workflow="p7_mel", target_type="trusted_p7_mel_final",
            source_props=SRC_MEL, target_props=TGT_P7_MEL_V40,
            artifacts=("BL.hevc",),
        )
        inj = await self.run_f(session)
        props = self.tb.props_of(Path(inj.opt("-o")))
        self.assertEqual(props.frames, FRAMES)
        self.assertTrue(props.has_l8)

    async def test_si_la_conversion_falla_se_sigue_con_el_rpu_original(self):
        # El vídeo es correcto igualmente; solo la señalización queda mal. La
        # fase avisa y continúa en vez de tirar el trabajo hecho.
        session = self.build(
            source_workflow="p7_mel", target_type="trusted_p7_mel_final",
            source_props=SRC_MEL, target_props=TGT_P7_MEL_V40,
            artifacts=("BL.hevc",),
        )
        # Solo la conversión falla; el merge previo debe seguir funcionando.
        self.tb.fail_when_json("dovi_tool", "editor", {"mode": 2},
                               stderr="mode 2 no soportado")
        inj = await self.run_f(session)
        self.assertEqual(inj.opt_name("--rpu-in"), "RPU_merged.bin",
                         "se inyecta el merged sin convertir, no se aborta")
        self.assertTrue(self.log.says("conversión a Profile 8.1 falló"))


class TestP7MelSegunTarget(FaseFTestCase):
    """En p7_mel el merge depende del profile del bin: P8 encaja directo."""

    async def test_target_p8_inyecta_directo_sin_merge(self):
        session = self.build(
            source_workflow="p7_mel", target_type="trusted_p8_source",
            source_props=SRC_MEL, target_props=TGT_P8_V40,
            artifacts=("BL.hevc",),
        )
        inj = await self.run_f(session)
        self.assertFalse(self.merged)
        self.assertEqual(inj.opt_name("--rpu-in"), "RPU_target.bin")
        self.assertEqual(inj.opt_name("-i"), "BL.hevc")
        self.assertEqual(inj.opt_name("-o"), "BL_injected.hevc")

    async def test_target_p7_mergea_los_levels_en_el_rpu_del_source(self):
        session = self.build(
            source_workflow="p7_mel", target_type="trusted_p7_fel_final",
            source_props=SRC_MEL, target_props=TGT_P7_FEL_V40,
            artifacts=("BL.hevc",),
        )
        inj = await self.run_f(session)
        self.assertTrue(self.merged)
        self.assertEqual(inj.opt_name("--rpu-in"), "RPU_merged_p81.bin")

    async def test_levels_no_fel_preservan_l1_l2_l6_del_bd(self):
        session = self.build(
            source_workflow="p7_mel", target_type="trusted_p7_mel_final",
            source_props=SRC_MEL, target_props=TGT_P7_MEL_V40,
            artifacts=("BL.hevc",),
        )
        await self.run_f(session)
        self.assertEqual(self.merge_levels, [3, 8, 9, 11, 254])

    async def test_sin_bl_hevc_falla_pidiendo_fase_c(self):
        session = self.build(
            source_workflow="p7_mel", target_type="trusted_p8_source",
            source_props=SRC_MEL, target_props=TGT_P8_V40, artifacts=(),
        )
        from phases.cmv40_pipeline import run_phase_f_inject
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_f_inject(session, self.log)
        self.assertIn("BL.hevc", str(cm.exception))


class TestP8SegunTarget(FaseFTestCase):
    """En p8 el source ya es single-layer: se inyecta sobre source.hevc."""

    async def test_target_p8_inyecta_directo(self):
        session = self.build(
            source_workflow="p8", target_type="trusted_p8_source",
            source_props=SRC_P8, target_props=TGT_P8_V40,
            artifacts=("source.hevc",),
        )
        inj = await self.run_f(session)
        self.assertFalse(self.merged)
        self.assertEqual(inj.opt_name("-i"), "source.hevc")
        self.assertEqual(inj.opt_name("--rpu-in"), "RPU_target.bin")

    async def test_target_p7_mergea(self):
        session = self.build(
            source_workflow="p8", target_type="trusted_p7_fel_final",
            source_props=SRC_P8, target_props=TGT_P7_FEL_V40,
            artifacts=("source.hevc",),
        )
        inj = await self.run_f(session)
        self.assertTrue(self.merged)
        self.assertEqual(inj.opt_name("-i"), "source.hevc")

    async def test_el_output_va_al_slot_bl_injected(self):
        # Fase G lee `BL_injected.hevc` para los dos workflows single-layer;
        # si Fase F escribiera a otro nombre, el remux no lo encontraría.
        session = self.build(
            source_workflow="p8", target_type="trusted_p8_source",
            source_props=SRC_P8, target_props=TGT_P8_V40,
            artifacts=("source.hevc",),
        )
        inj = await self.run_f(session)
        self.assertEqual(inj.opt_name("-o"), "BL_injected.hevc")


class TestGuardsDeFaseF(FaseFTestCase):
    """Comprobaciones que abortan antes de gastar el inject."""

    async def test_frame_count_distinto_aborta_remitiendo_a_fase_d(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p7_fel_final",
            source_props=SRC_FEL,
            target_props=RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                                  frames=FRAMES, has_l8=True),
            artifacts=("source.hevc",),
        )
        session.source_frame_count = FRAMES + 24   # desfase de un logo de estudio
        from phases.cmv40_pipeline import run_phase_f_inject
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_f_inject(session, self.log)
        self.assertIn("Frame count mismatch", str(cm.exception))
        self.assertFalse(self.tb.ran("dovi_tool", "inject-rpu"),
                         "no se debe inyectar con los frames desalineados")

    async def test_merge_con_frames_distintos_aborta_antes_del_editor(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p8_source",
            source_props=SRC_FEL,
            target_props=RpuProps(profile=8, el_type="", cm_version="v4.0",
                                  frames=FRAMES + 10, has_l8=True),
            artifacts=("EL.hevc",),
        )
        from phases.cmv40_pipeline import run_phase_f_inject
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_f_inject(session, self.log)
        self.assertIn("Frame count mismatch", str(cm.exception))
        self.assertFalse(self.tb.ran("dovi_tool", "inject-rpu"))

    async def test_sin_rpu_target_aborta(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p8_source",
            source_props=SRC_FEL, target_props=TGT_P8_V40,
            artifacts=("EL.hevc",),
        )
        (self.wd / "RPU_target.bin").unlink()
        from phases.cmv40_pipeline import run_phase_f_inject
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_f_inject(session, self.log)
        self.assertIn("RPU target", str(cm.exception))

    async def test_usa_el_rpu_sincronizado_si_fase_e_lo_dejo(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p7_fel_final",
            source_props=SRC_FEL, target_props=TGT_P7_FEL_V40,
            artifacts=("source.hevc",),
        )
        synced = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                          frames=FRAMES, has_l8=True)
        self.tb.define_rpu("RPU_synced.bin", **synced.as_dict())
        write_artifacts(self.wd, "RPU_synced.bin", props=synced)
        inj = await self.run_f(session)
        self.assertEqual(inj.opt_name("--rpu-in"), "RPU_synced.bin",
                         "la corrección de sync de Fase E debe tener prioridad")

    async def test_borra_el_rpu_de_validacion_adelantado(self):
        # Fase G adelanta el extract-rpu que Fase H usará. Si Fase F se
        # rehace, ese RPU ya no corresponde al HEVC: validar contra él daría
        # un frame count correcto sin detectar nada.
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p7_fel_final",
            source_props=SRC_FEL, target_props=TGT_P7_FEL_V40,
            artifacts=("source.hevc",),
        )
        stale = self.wd / "_validate_full_rpu.bin"
        stale.write_bytes(b"\x00" * 4096)
        await self.run_f(session)
        self.assertFalse(stale.exists())


class TestPlanCoincideConLaEjecucion(FaseFTestCase):
    """El `📋 Plan` se calcula aparte de la decisión, sobre las mismas
    entradas. Nada garantiza en el código que digan lo mismo, así que se
    comprueba aquí: si divergen, el usuario lee en el log algo distinto de
    lo que la fase hizo."""

    async def test_drop_in_anuncia_drop_in(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p7_fel_final",
            source_props=SRC_FEL, target_props=TGT_P7_FEL_V40,
            artifacts=("source.hevc",),
        )
        await self.run_f(session)
        self.assertIn("DROP-IN", self.log.plan)
        self.assertNotIn("MERGE clásico", self.log.plan)

    async def test_merge_anuncia_merge(self):
        session = self.build(
            source_workflow="p7_fel", target_type="trusted_p8_source",
            source_props=SRC_FEL, target_props=TGT_P8_V40,
            artifacts=("EL.hevc",),
        )
        await self.run_f(session)
        self.assertIn("MERGE", self.log.plan)
        self.assertNotIn("DROP-IN", self.log.plan)

    async def test_single_layer_anuncia_single_layer(self):
        session = self.build(
            source_workflow="p7_mel", target_type="trusted_p8_source",
            source_props=SRC_MEL, target_props=TGT_P8_V40,
            artifacts=("BL.hevc",),
        )
        await self.run_f(session)
        self.assertIn("single-layer", self.log.plan)


if __name__ == "__main__":
    unittest.main()
