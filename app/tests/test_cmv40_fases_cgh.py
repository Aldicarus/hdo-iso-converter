"""
Fases C, G y H ejecutadas de verdad, rama por rama.

Igual que la Fase F, estas tres deciden qué hacer a partir de
(source_workflow, target_type, trust) y hasta ahora no había forma de
ejecutarlas en un test. Aquí se cubren las decisiones y los guards que
nacieron de fallos concretos en el NAS:

  * Fase C reutilizaba BL/EL sin comprobar que el demux hubiera terminado,
    así que un demux muerto a mitad (timeout, kill, reinicio) dejaba
    ficheros truncados que el reintento daba por buenos — Proyecto
    Salvación, 2h36m UHD, BL.hevc con 171.679 de 225.177 frames.
  * Fase G adelanta el `extract-rpu` de la validación mientras remuxa; si
    ese adelanto se hiciera también en drop-in se gastarían minutos de CPU
    en algo que el fast path no usa.
  * Fase H valida el RPU completo en la rama merge y se salta el
    `extract-rpu` en drop-in. Confundir las dos ramas cuesta 5-8 min por
    job, o deja pasar un RPU incompleto.
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import (  # noqa: E402
    PhaseTestCase, RpuProps, make_session, write_artifacts,
)

FRAMES = 1000

SRC_FEL = RpuProps(profile=7, el_type="FEL", cm_version="v2.9", frames=FRAMES)
SRC_MEL = RpuProps(profile=7, el_type="MEL", cm_version="v2.9", frames=FRAMES)
SRC_P8 = RpuProps(profile=8, el_type="", cm_version="v2.9", frames=FRAMES)

TGT_P7_FEL_V40 = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                          frames=FRAMES, has_l8=True)
TGT_P8_V40 = RpuProps(profile=8, el_type="", cm_version="v4.0",
                      frames=FRAMES, has_l8=True)

# El RPU que acaba dentro del HEVC inyectado en cada rama, tal y como lo
# dejaría la Fase F. Fase G y H parten de ahí.
INJ_FEL_V40 = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                       frames=FRAMES, has_l8=True)
INJ_P8_V40 = RpuProps(profile=8, el_type="", cm_version="v4.0",
                      frames=FRAMES, has_l8=True)


class CmvPhaseCase(PhaseTestCase):
    def prepare(self, *, source_workflow="p7_fel",
                target_type="trusted_p7_fel_final",
                source_props=SRC_FEL, target_props=TGT_P7_FEL_V40,
                trust_ok=True, trust_override="auto",
                artifacts=(), injected_props=None):
        self.tb.define_rpu("RPU_source.bin", **source_props.as_dict())
        self.tb.define_rpu("RPU_target.bin", **target_props.as_dict())
        write_artifacts(self.wd, "RPU_source.bin", props=source_props)
        write_artifacts(self.wd, "RPU_target.bin", props=target_props)
        for name in artifacts:
            props = injected_props if ("injected" in name or "DV_dual" in name) else source_props
            write_artifacts(self.wd, name, props=props)
        session = make_session(
            self.wd,
            source_workflow=source_workflow, target_type=target_type,
            target_trust_ok=trust_ok, trust_override=trust_override,
            source_frame_count=FRAMES, target_frame_count=FRAMES,
        )
        self.tb.define_media(Path(session.source_mkv_path).name,
                             duration=7200.0, frames=FRAMES)
        write_artifacts(self.wd, "source.mkv")
        return session


# ════════════════════════════════════════════════════════════════════════
#  FASE C — demux + datos per-frame
# ════════════════════════════════════════════════════════════════════════

class TestFaseCDemux(CmvPhaseCase):

    async def run_c(self, session):
        from phases.cmv40_pipeline import run_phase_c_extract
        await run_phase_c_extract(session, self.log)

    async def test_drop_in_no_demuxea(self):
        session = self.prepare(artifacts=("source.hevc",))
        await self.run_c(session)
        self.assertFalse(self.tb.ran("dovi_tool", "demux"),
                         "el drop-in inyecta sobre BL+EL: separar capas es trabajo tirado")
        self.assertIn("demux_dual_layer", session.phases_skipped)
        self.assertIn("mux_dual_layer", session.phases_skipped)

    async def test_drop_in_conserva_source_hevc(self):
        # El housekeeping borra source.hevc tras el demux, pero en drop-in es
        # justo el fichero que Fase F necesita.
        session = self.prepare(artifacts=("source.hevc",))
        await self.run_c(session)
        self.assertTrue((self.wd / "source.hevc").exists())

    async def test_p7_fel_sin_trust_demuxea_bl_y_el(self):
        session = self.prepare(trust_ok=False, artifacts=("source.hevc",))
        await self.run_c(session)
        demux = self.tb.one("dovi_tool", "demux")
        self.assertEqual(demux.opt_name("--bl-out"), "BL.hevc")
        self.assertEqual(demux.opt_name("--el-out"), "EL.hevc")

    async def test_p8_no_demuxea(self):
        session = self.prepare(source_workflow="p8", target_type="generic",
                               source_props=SRC_P8, target_props=TGT_P8_V40,
                               trust_ok=False, artifacts=("source.hevc",))
        await self.run_c(session)
        self.assertFalse(self.tb.ran("dovi_tool", "demux"),
                         "el source p8 ya es single-layer")

    async def test_p7_mel_descarta_la_el_tras_el_demux(self):
        session = self.prepare(source_workflow="p7_mel",
                               target_type="trusted_p8_source",
                               source_props=SRC_MEL, target_props=TGT_P8_V40,
                               trust_ok=False, artifacts=("source.hevc",))
        await self.run_c(session)
        self.assertTrue((self.wd / "BL.hevc").exists())
        self.assertFalse((self.wd / "EL.hevc").exists(),
                         "el EL MEL no aporta nada al P8.1 resultante")

    async def test_housekeeping_borra_source_hevc_tras_demuxear(self):
        session = self.prepare(trust_ok=False, artifacts=("source.hevc",))
        await self.run_c(session)
        self.assertFalse((self.wd / "source.hevc").exists(),
                         "con BL/EL en disco, source.hevc son ~40 GB de más")

    async def test_marca_el_demux_como_terminado(self):
        session = self.prepare(trust_ok=False, artifacts=("source.hevc",))
        await self.run_c(session)
        self.assertTrue((self.wd / ".demux_done").exists())


class TestFaseCReutilizacionDelDemux(CmvPhaseCase):
    """Regresión de Proyecto Salvación: un demux a medias no se reutiliza."""

    async def run_c(self, session):
        from phases.cmv40_pipeline import run_phase_c_extract
        await run_phase_c_extract(session, self.log)

    async def test_bl_el_sin_marcador_fuerza_re_demux(self):
        session = self.prepare(trust_ok=False, artifacts=("source.hevc",))
        # BL/EL de un demux que murió por timeout: existen pero truncados.
        write_artifacts(self.wd, "BL.hevc", "EL.hevc", props=SRC_FEL)
        await self.run_c(session)
        self.assertTrue(self.tb.ran("dovi_tool", "demux"),
                        "sin .demux_done los BL/EL pueden estar truncados")

    async def test_bl_el_con_marcador_se_reutilizan(self):
        session = self.prepare(trust_ok=False, artifacts=("source.hevc",))
        write_artifacts(self.wd, "BL.hevc", "EL.hevc", props=SRC_FEL)
        (self.wd / ".demux_done").write_text(str(FRAMES))
        await self.run_c(session)
        self.assertFalse(self.tb.ran("dovi_tool", "demux"),
                         "un demux completo no se repite")

    async def test_demux_fallido_no_deja_marcador_ni_parciales(self):
        session = self.prepare(trust_ok=False, artifacts=("source.hevc",))
        self.tb.fail("dovi_tool", "demux", rc=1, stderr="disco lleno")
        from phases.cmv40_pipeline import run_phase_c_extract
        with self.assertRaises(RuntimeError):
            await run_phase_c_extract(session, self.log)
        self.assertFalse((self.wd / ".demux_done").exists())
        self.assertFalse((self.wd / "BL.hevc").exists(),
                         "los parciales deben irse para que el reintento empiece limpio")


class TestFaseCDatosPerFrame(CmvPhaseCase):

    async def run_c(self, session):
        from phases.cmv40_pipeline import run_phase_c_extract
        await run_phase_c_extract(session, self.log)

    async def test_target_trusted_omite_los_datos_del_chart(self):
        session = self.prepare(artifacts=("source.hevc",))
        await self.run_c(session)
        self.assertFalse(self.tb.ran("dovi_tool", "export"),
                         "si Fase D se salta, el chart no se muestra")
        self.assertIn("per_frame_data_skipped", session.phases_skipped)

    async def test_sin_trust_genera_los_datos_de_ambos_rpus(self):
        session = self.prepare(trust_ok=False, artifacts=("source.hevc",))
        await self.run_c(session)
        exports = self.tb.find("dovi_tool", "export")
        self.assertEqual(len(exports), 2, "hace falta la curva del source y la del target")
        exportados = {e.opt_name("-i") for e in exports}
        self.assertEqual(exportados, {"RPU_source.bin", "RPU_target.bin"})
        for e in exports:
            self.assertIn("level1", e.opt("--levels") or "",
                          "el chart solo necesita el L1: volcar el RPU entero "
                          "son 682 MB y ~100 s por RPU")
        self.assertTrue((self.wd / "per_frame_data.json").exists())

    async def test_force_interactive_genera_los_datos_aunque_haya_trust(self):
        session = self.prepare(trust_override="force_interactive",
                               artifacts=("source.hevc",))
        await self.run_c(session)
        self.assertTrue(self.tb.ran("dovi_tool", "export"),
                        "el usuario pidió revisar el sync a mano")


# ════════════════════════════════════════════════════════════════════════
#  FASE G — mux + remux final
# ════════════════════════════════════════════════════════════════════════

class TestFaseGRemux(CmvPhaseCase):

    async def run_g(self, session):
        from phases.cmv40_pipeline import run_phase_g_remux
        return await run_phase_g_remux(session, self.log)

    async def test_drop_in_no_ejecuta_dovi_mux(self):
        session = self.prepare(artifacts=("source_injected.hevc",),
                               injected_props=INJ_FEL_V40)
        await self.run_g(session)
        self.assertFalse(self.tb.ran("dovi_tool", "mux"),
                         "el stream drop-in ya es dual-layer íntegro")
        mkv = self.tb.one("mkvmerge")
        self.assertIn("source_injected.hevc", " ".join(mkv.argv))

    async def test_p7_fel_combina_bl_con_el_inyectada(self):
        session = self.prepare(trust_ok=False,
                               artifacts=("BL.hevc", "EL_injected.hevc"),
                               injected_props=INJ_FEL_V40)
        await self.run_g(session)
        mux = self.tb.one("dovi_tool", "mux")
        self.assertEqual(mux.opt_name("--bl"), "BL.hevc")
        self.assertEqual(mux.opt_name("--el"), "EL_injected.hevc")
        self.assertEqual(mux.opt_name("-o"), "DV_dual.hevc")
        mkv = self.tb.one("mkvmerge")
        self.assertIn("DV_dual.hevc", " ".join(mkv.argv))

    async def test_single_layer_no_ejecuta_dovi_mux(self):
        for wf, src in (("p7_mel", SRC_MEL), ("p8", SRC_P8)):
            with self.subTest(workflow=wf):
                self.tb.reset_calls()
                session = self.prepare(
                    source_workflow=wf, target_type="trusted_p8_source",
                    source_props=src, target_props=TGT_P8_V40, trust_ok=False,
                    artifacts=("BL_injected.hevc",), injected_props=INJ_P8_V40)
                await self.run_g(session)
                self.assertFalse(self.tb.ran("dovi_tool", "mux"))
                self.assertIn("BL_injected.hevc",
                              " ".join(self.tb.one("mkvmerge").argv))

    async def test_escribe_a_output_con_sufijo_tmp(self):
        session = self.prepare(artifacts=("source_injected.hevc",),
                               injected_props=INJ_FEL_V40)
        result = await self.run_g(session)
        expected = self.output_dir / f"{session.output_mkv_name}.tmp"
        self.assertEqual(result, str(expected))
        self.assertTrue(expected.exists(),
                        "Fase H hace el rename atómico desde este .tmp")

    async def test_preserva_audio_y_subs_del_origen(self):
        session = self.prepare(artifacts=("source_injected.hevc",),
                               injected_props=INJ_FEL_V40)
        await self.run_g(session)
        mkv = self.tb.one("mkvmerge")
        self.assertTrue(mkv.has("--no-video"))
        self.assertIn(session.source_mkv_path, mkv.argv)

    async def test_nombre_de_pista_segun_workflow(self):
        casos = [
            ("p7_fel", "trusted_p7_fel_final", SRC_FEL, TGT_P7_FEL_V40, True,
             ("source_injected.hevc",), "HEVC DV P7 FEL CMv4.0"),
            ("p7_mel", "trusted_p8_source", SRC_MEL, TGT_P8_V40, False,
             ("BL_injected.hevc",), "HEVC DV P8.1 CMv4.0 (from P7 MEL)"),
            ("p8", "trusted_p8_source", SRC_P8, TGT_P8_V40, False,
             ("BL_injected.hevc",), "HEVC DV P8.1 CMv4.0"),
        ]
        for wf, tt, src, tgt, trust, arts, esperado in casos:
            with self.subTest(workflow=wf):
                self.tb.reset_calls()
                session = self.prepare(
                    source_workflow=wf, target_type=tt, source_props=src,
                    target_props=tgt, trust_ok=trust, artifacts=arts,
                    injected_props=INJ_P8_V40)
                await self.run_g(session)
                self.assertEqual(self.tb.one("mkvmerge").opt("--track-name"),
                                 f"0:{esperado}")

    async def test_adelanta_el_rpu_de_validacion_solo_en_la_rama_merge(self):
        session = self.prepare(trust_ok=False,
                               artifacts=("BL.hevc", "EL_injected.hevc"),
                               injected_props=INJ_FEL_V40)
        await self.run_g(session)
        self.assertTrue(self.tb.ran("dovi_tool", "extract-rpu"),
                        "Fase H lo necesita; extraerlo en paralelo lo sale gratis")
        self.assertTrue((self.wd / "_validate_full_rpu.bin").exists())

    async def test_no_reutiliza_un_rpu_adelantado_de_otra_pasada(self):
        # Si Fase F se rehizo, el RPU de la pasada anterior ya no corresponde
        # al HEVC: validar contra él daría un frame count correcto sin
        # comprobar nada. Fase G lo borra antes de relanzar el adelanto.
        session = self.prepare(trust_ok=False,
                               artifacts=("BL.hevc", "EL_injected.hevc"),
                               injected_props=INJ_FEL_V40)
        viejo = self.wd / "_validate_full_rpu.bin"
        viejo.write_bytes(b"rancio" * 1000)
        await self.run_g(session)
        self.assertTrue(self.tb.ran("dovi_tool", "extract-rpu"),
                        "el adelanto debe rehacerse, no heredarse")
        self.assertNotEqual(viejo.read_bytes()[:6], b"rancio")

    async def test_drop_in_no_adelanta_nada(self):
        session = self.prepare(artifacts=("source_injected.hevc",),
                               injected_props=INJ_FEL_V40)
        await self.run_g(session)
        self.assertFalse(self.tb.ran("dovi_tool", "extract-rpu"),
                         "el fast path de Fase H no usa ese RPU")

    async def test_sin_el_hevc_inyectado_falla_remitiendo_a_fase_f(self):
        session = self.prepare(artifacts=())
        from phases.cmv40_pipeline import run_phase_g_remux
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_g_remux(session, self.log)
        self.assertIn("Fase F", str(cm.exception))


# ════════════════════════════════════════════════════════════════════════
#  FASE H — validación final
# ════════════════════════════════════════════════════════════════════════

class TestFaseHValidacion(CmvPhaseCase):

    def stage_output(self, session, props=INJ_FEL_V40):
        """Deja el .mkv.tmp que Fase G habría escrito."""
        tmp = self.output_dir / f"{session.output_mkv_name}.tmp"
        write_artifacts(self.output_dir, tmp.name, props=props)
        self.tb.define_media(tmp.name, duration=7200.0, frames=FRAMES)
        return tmp

    async def run_h(self, session):
        from phases.cmv40_pipeline import run_phase_h_validate
        return await run_phase_h_validate(session, self.log)

    async def test_drop_in_no_extrae_el_rpu_completo(self):
        session = self.prepare(artifacts=("source_injected.hevc",),
                               injected_props=INJ_FEL_V40)
        self.stage_output(session)
        result = await self.run_h(session)
        self.assertFalse(self.tb.ran("dovi_tool", "extract-rpu"),
                         "la cadena upstream ya garantiza P7 FEL CMv4.0")
        self.assertEqual(result["profile"], 7)
        self.assertEqual(result["cm_version"], "v4.0")

    async def test_rama_merge_valida_el_rpu_del_hevc_pre_mux(self):
        session = self.prepare(trust_ok=False,
                               artifacts=("BL.hevc", "DV_dual.hevc"),
                               injected_props=INJ_FEL_V40)
        self.stage_output(session)
        result = await self.run_h(session)
        extract = self.tb.one("dovi_tool", "extract-rpu")
        self.assertIn("DV_dual.hevc", " ".join(extract.argv))
        self.assertEqual(result["cm_version"], "v4.0")

    async def test_rama_merge_reutiliza_el_rpu_que_adelanto_fase_g(self):
        session = self.prepare(trust_ok=False,
                               artifacts=("BL.hevc", "DV_dual.hevc"),
                               injected_props=INJ_FEL_V40)
        self.stage_output(session)
        write_artifacts(self.wd, "_validate_full_rpu.bin", props=INJ_FEL_V40)
        await self.run_h(session)
        self.assertFalse(self.tb.ran("dovi_tool", "extract-rpu"),
                         "releer el HEVC costaría 5-8 min para el mismo dato")

    async def test_rechaza_un_rpu_que_no_es_cmv4(self):
        # El merge no aplicó la transferencia: el MKV no debe entregarse.
        sin_v40 = RpuProps(profile=7, el_type="FEL", cm_version="v2.9",
                           frames=FRAMES, has_l8=False)
        session = self.prepare(trust_ok=False,
                               artifacts=("BL.hevc", "DV_dual.hevc"),
                               injected_props=sin_v40)
        self.stage_output(session)
        from phases.cmv40_pipeline import run_phase_h_validate
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_h_validate(session, self.log)
        self.assertIn("v4.0", str(cm.exception))
        self.assertFalse((self.output_dir / session.output_mkv_name).exists(),
                         "un MKV que no valida no llega a su nombre final")

    async def test_rechaza_un_cmv4_sin_l8(self):
        # cm_version dice v4.0 pero no hay trims: un CMv4.0 hueco.
        hueco = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                         frames=FRAMES, has_l8=False)
        session = self.prepare(trust_ok=False,
                               artifacts=("BL.hevc", "DV_dual.hevc"),
                               injected_props=hueco)
        self.stage_output(session)
        from phases.cmv40_pipeline import run_phase_h_validate
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_h_validate(session, self.log)
        self.assertIn("L8", str(cm.exception))

    async def test_rechaza_un_rpu_recortado(self):
        recortado = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                             frames=FRAMES // 2, has_l8=True)
        session = self.prepare(trust_ok=False,
                               artifacts=("BL.hevc", "DV_dual.hevc"),
                               injected_props=recortado)
        self.stage_output(session)
        from phases.cmv40_pipeline import run_phase_h_validate
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_h_validate(session, self.log)
        self.assertIn("Frame count", str(cm.exception))

    async def test_tolera_dos_frames_de_diferencia(self):
        # mkvmerge puede emitir un cluster final corto: ±2 es variación normal.
        casi = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                        frames=FRAMES - 2, has_l8=True)
        session = self.prepare(trust_ok=False,
                               artifacts=("BL.hevc", "DV_dual.hevc"),
                               injected_props=casi)
        self.stage_output(session)
        result = await self.run_h(session)
        self.assertEqual(result["frame_count"], FRAMES - 2)

    async def test_rechaza_una_fel_degradada_a_single_layer(self):
        degradado = RpuProps(profile=8, el_type="", cm_version="v4.0",
                             frames=FRAMES, has_l8=True)
        session = self.prepare(trust_ok=False,
                               artifacts=("BL.hevc", "DV_dual.hevc"),
                               injected_props=degradado)
        self.stage_output(session)
        from phases.cmv40_pipeline import run_phase_h_validate
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_h_validate(session, self.log)
        self.assertIn("el_type", str(cm.exception))

    async def test_al_validar_renombra_el_tmp_al_nombre_final(self):
        session = self.prepare(artifacts=("source_injected.hevc",),
                               injected_props=INJ_FEL_V40)
        tmp = self.stage_output(session)
        await self.run_h(session)
        final = self.output_dir / session.output_mkv_name
        self.assertTrue(final.exists())
        self.assertFalse(tmp.exists())
        self.assertEqual(session.output_mkv_path, str(final))

    async def test_no_sobrescribe_un_mkv_existente(self):
        session = self.prepare(artifacts=("source_injected.hevc",),
                               injected_props=INJ_FEL_V40)
        self.stage_output(session)
        write_artifacts(self.output_dir, session.output_mkv_name)
        from phases.cmv40_pipeline import run_phase_h_validate
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_h_validate(session, self.log)
        self.assertIn("Ya existe", str(cm.exception))

    async def test_un_segundo_disparo_revalida_sin_mover_nada(self):
        # Fase H puede dispararse dos veces (el frontend y el backend la
        # lanzan); el segundo intento llega cuando el rename ya está hecho.
        session = self.prepare(artifacts=("source_injected.hevc",),
                               injected_props=INJ_FEL_V40)
        final = self.output_dir / session.output_mkv_name
        write_artifacts(self.output_dir, final.name, props=INJ_FEL_V40)
        self.tb.define_media(final.name, duration=7200.0, frames=FRAMES)
        result = await self.run_h(session)
        self.assertTrue(result.get("already_validated"))
        self.assertTrue(final.exists())

    async def test_sin_mkv_falla_remitiendo_a_fase_g(self):
        session = self.prepare(artifacts=("source_injected.hevc",),
                               injected_props=INJ_FEL_V40)
        from phases.cmv40_pipeline import run_phase_h_validate
        with self.assertRaises(RuntimeError) as cm:
            await run_phase_h_validate(session, self.log)
        self.assertIn("Fase G", str(cm.exception))



class TestProgresoDeLasFases(CmvPhaseCase):
    """El `progress_ctx` de las operaciones largas no viaja en el argv, así
    que hay que comprobarlo aparte.

    De él salen la barra y el ETA, y la señal correcta depende de qué
    fichero lee y escribe cada proceso (ver `_ReadProgress`): apuntar al
    fichero equivocado deja la barra clavada o dando un ETA inventado
    durante los minutos que dura la operación.
    """

    def capture_streaming(self):
        """Intercepta `_run_streaming` guardando su `progress_ctx`."""
        from phases import cmv40_pipeline as P
        capturado = []
        orig = P._run_streaming

        async def espia(cmd, log_callback=None, proc_callback=None, progress_ctx=None):
            capturado.append((cmd, progress_ctx))
            return await orig(cmd, log_callback=log_callback,
                             proc_callback=proc_callback, progress_ctx=progress_ctx)

        P._run_streaming = espia
        self.addCleanup(lambda: setattr(P, "_run_streaming", orig))
        return capturado

    def ctx_de(self, capturado, subcomando):
        for cmd, ctx in capturado:
            if subcomando in cmd:
                return ctx
        raise AssertionError(
            f"no se lanzó ningún proceso con {subcomando!r}; "
            f"lanzados: {[c[0][:2] for c in capturado]}")

    async def test_el_mux_mide_el_progreso_sobre_bl_y_el_dual_layer(self):
        capturado = self.capture_streaming()
        session = self.prepare(trust_ok=False,
                               artifacts=("BL.hevc", "EL_injected.hevc"),
                               injected_props=INJ_FEL_V40)
        from phases.cmv40_pipeline import run_phase_g_remux
        await run_phase_g_remux(session, self.log)

        ctx = self.ctx_de(capturado, "mux")
        self.assertEqual(Path(ctx["input_path"]).name, "BL.hevc")
        self.assertEqual(Path(ctx["output_path"]).name, "DV_dual.hevc")
        # El dual-layer pesa lo que suman las dos capas: es lo que permite
        # dar un ETA real mientras dovi_tool escribe.
        esperado = ((self.wd / "BL.hevc").stat().st_size
                    + (self.wd / "EL_injected.hevc").stat().st_size)
        self.assertEqual(ctx["expected_out_bytes"], esperado)

    async def test_el_remux_final_arranca_donde_acabo_el_mux(self):
        # La barra de la fase es monotónica: si el remux empezara en 0 tras
        # el mux, retrocedería del 38% al 0%.
        capturado = self.capture_streaming()
        session = self.prepare(trust_ok=False,
                               artifacts=("BL.hevc", "EL_injected.hevc"),
                               injected_props=INJ_FEL_V40)
        from phases.cmv40_pipeline import run_phase_g_remux
        await run_phase_g_remux(session, self.log)

        mux = self.ctx_de(capturado, "mux")
        mkv = self.ctx_de(capturado, "--gui-mode")
        self.assertEqual(mux["offset"], 0.0)
        self.assertEqual(mkv["offset"], mux["weight"])
        self.assertAlmostEqual(mkv["offset"] + mkv["weight"], 100.0)

    async def test_sin_mux_el_remux_ocupa_toda_la_barra(self):
        capturado = self.capture_streaming()
        session = self.prepare(artifacts=("source_injected.hevc",),
                               injected_props=INJ_FEL_V40)
        from phases.cmv40_pipeline import run_phase_g_remux
        await run_phase_g_remux(session, self.log)

        mkv = self.ctx_de(capturado, "--gui-mode")
        self.assertEqual(mkv["offset"], 0.0)
        self.assertEqual(mkv["weight"], 100.0)

    async def test_el_inject_mide_las_dos_pasadas_sobre_su_entrada(self):
        # `inject-rpu` recorre la entrada dos veces (orden de frames y
        # reescritura). Los bytes leídos acumulados son la única señal que
        # cubre ambas sin saltos — ver `_ReadProgress`.
        capturado = self.capture_streaming()
        session = self.prepare(artifacts=("source.hevc",),
                               injected_props=INJ_FEL_V40)
        from phases.cmv40_pipeline import run_phase_f_inject
        await run_phase_f_inject(session, self.log)

        ctx = self.ctx_de(capturado, "inject-rpu")
        entrada = self.wd / "source.hevc"
        self.assertEqual(Path(ctx["input_path"]).name, "source.hevc")
        self.assertEqual(Path(ctx["output_path"]).name, "source_injected.hevc")
        self.assertEqual(ctx["expected_read_bytes"], 2 * entrada.stat().st_size)
        self.assertEqual(ctx["expected_out_bytes"], entrada.stat().st_size)

    async def test_el_inject_arranca_tras_el_merge_cuando_lo_hay(self):
        capturado = self.capture_streaming()
        session = self.prepare(trust_ok=False, artifacts=("EL.hevc",),
                               injected_props=INJ_FEL_V40)
        from phases.cmv40_pipeline import run_phase_f_inject
        await run_phase_f_inject(session, self.log)

        ctx = self.ctx_de(capturado, "inject-rpu")
        self.assertGreater(ctx["offset"], 0.0,
                           "el merge ya movió la barra: el inject no vuelve a 0")
        self.assertAlmostEqual(ctx["offset"] + ctx["weight"], 100.0)

    async def test_el_inject_del_drop_in_ocupa_toda_la_barra(self):
        capturado = self.capture_streaming()
        session = self.prepare(artifacts=("source.hevc",),
                               injected_props=INJ_FEL_V40)
        from phases.cmv40_pipeline import run_phase_f_inject
        await run_phase_f_inject(session, self.log)

        ctx = self.ctx_de(capturado, "inject-rpu")
        self.assertEqual(ctx["offset"], 0.0, "sin merge previo no hay nada que descontar")
        self.assertEqual(ctx["weight"], 100.0)

    async def test_el_demux_mide_sobre_el_hevc_completo(self):
        capturado = []
        from phases import cmv40_pipeline as P
        orig = P._run_with_time_estimate

        async def espia(cmd, **kw):
            capturado.append((cmd, kw))
            return await orig(cmd, **kw)

        P._run_with_time_estimate = espia
        self.addCleanup(lambda: setattr(P, "_run_with_time_estimate", orig))

        session = self.prepare(trust_ok=False, artifacts=("source.hevc",))
        from phases.cmv40_pipeline import run_phase_c_extract
        await run_phase_c_extract(session, self.log)

        demux = next((kw for cmd, kw in capturado if "demux" in cmd), None)
        self.assertIsNotNone(demux, "no se lanzó el demux")
        self.assertEqual(Path(demux["progress_input"]).name, "source.hevc")


if __name__ == "__main__":
    unittest.main()
