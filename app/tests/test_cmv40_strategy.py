"""
La matriz de workflows CMv4.0, recorrida entera.

`cmv40_strategy` es puro, así que las 48 combinaciones de
(source_workflow × target_type × trust × override) caben en un test que
tarda milisegundos. Antes esta tabla vivía como cascadas `if/elif`
repartidas por cuatro fases y no había forma de comprobarla sin un NAS.

Lo que se fija aquí son invariantes, no la implementación: cosas que si
dejan de cumplirse producen un MKV incorrecto o tiran trabajo a la basura.
"""
import sys
import unittest
from itertools import product
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from phases.cmv40_strategy import (  # noqa: E402
    BL_HEVC, BL_INJECTED, DV_DUAL, EL_HEVC, EL_INJECTED, SOURCE_HEVC,
    SOURCE_INJECTED, WORKFLOWS, WorkflowInputs, plan_for,
)

TARGET_TYPES = ("generic", "trusted_p8_source",
                "trusted_p7_fel_final", "trusted_p7_mel_final")
OVERRIDES = ("auto", "force_interactive")

ALL_INPUTS = [
    WorkflowInputs(wf, tt, trust, ov)
    for wf, tt, trust, ov in product(WORKFLOWS, TARGET_TYPES, (True, False), OVERRIDES)
]


class TestCoberturaDeLaMatriz(unittest.TestCase):

    def test_las_48_combinaciones_producen_un_plan(self):
        self.assertEqual(len(ALL_INPUTS), 48)
        for inp in ALL_INPUTS:
            with self.subTest(inp=inp):
                plan = plan_for(inp)
                self.assertTrue(plan.inject.hevc_input)
                self.assertTrue(plan.inject.hevc_output)
                self.assertTrue(plan.remux.hevc_for_mkv)
                self.assertTrue(plan.inject.plan_text.startswith("[Fase F] 📋 Plan:"))
                self.assertTrue(plan.remux.plan_text.startswith("[Fase G] 📋 Plan:"))
                self.assertTrue(plan.validate.plan_text.startswith("[Fase H] 📋 Plan"))

    def test_solo_una_combinacion_de_source_y_target_da_drop_in(self):
        drop_ins = [i for i in ALL_INPUTS if i.drop_in]
        for i in drop_ins:
            self.assertEqual(i.source_workflow, "p7_fel")
            self.assertEqual(i.target_type, "trusted_p7_fel_final")
            self.assertTrue(i.target_trust_ok)
            self.assertEqual(i.trust_override, "auto")
        self.assertEqual(len(drop_ins), 1)


class TestInvariantesDeSeguridad(unittest.TestCase):
    """Reglas cuyo incumplimiento produce un fichero incorrecto."""

    def test_todo_stream_single_layer_exige_rpu_profile_8(self):
        # El fallo de "Te van a matar": un RPU P7 en un fichero sin EL lo
        # anuncia como dual-layer y el reproductor espera una capa ausente.
        for inp in ALL_INPUTS:
            with self.subTest(inp=inp):
                plan = plan_for(inp)
                if inp.single_layer_output:
                    self.assertTrue(plan.inject.needs_profile8)
                else:
                    self.assertFalse(
                        plan.inject.needs_profile8,
                        "convertir a P8 un dual-layer destruiría la señalización de la FEL")

    def test_ningun_plan_convierte_a_p8_conservando_la_fel(self):
        for inp in ALL_INPUTS:
            plan = plan_for(inp)
            if plan.remux.video_track_name.endswith("P7 FEL CMv4.0"):
                with self.subTest(inp=inp):
                    self.assertFalse(plan.inject.needs_profile8)

    def test_la_fase_g_lee_lo_que_la_fase_f_escribe(self):
        # Si los dos nombres se desalinean, el remux falla al final del job
        # con los 40 min de inject ya gastados.
        for inp in ALL_INPUTS:
            with self.subTest(inp=inp):
                plan = plan_for(inp)
                if plan.remux.needs_dovi_mux:
                    self.assertIn(plan.inject.hevc_output, plan.remux.mux_inputs)
                    self.assertEqual(plan.remux.hevc_for_mkv, DV_DUAL)
                else:
                    self.assertEqual(plan.remux.hevc_for_mkv, plan.inject.hevc_output)

    def test_la_fase_f_solo_pide_artefactos_que_la_fase_c_deja(self):
        for inp in ALL_INPUTS:
            with self.subTest(inp=inp):
                plan = plan_for(inp)
                if plan.inject.required_input in (BL_HEVC, EL_HEVC):
                    self.assertTrue(
                        plan.extract.needs_demux,
                        "pedir BL/EL sin haber demuxeado deja la fase sin entrada")
                if plan.inject.required_input == SOURCE_HEVC:
                    self.assertFalse(
                        plan.extract.needs_demux,
                        "el housekeeping borra source.hevc justo después del demux")

    def test_el_mux_dual_layer_solo_ocurre_con_capa_de_mejora(self):
        for inp in ALL_INPUTS:
            plan = plan_for(inp)
            if plan.remux.needs_dovi_mux:
                with self.subTest(inp=inp):
                    self.assertEqual(inp.source_workflow, "p7_fel")
                    self.assertFalse(inp.drop_in)
                    self.assertEqual(plan.inject.hevc_output, EL_INJECTED)

    def test_el_fast_path_de_validacion_es_exclusivo_del_drop_in(self):
        # Saltarse el extract-rpu completo solo es legítimo cuando la cadena
        # upstream es determinista; en la rama merge dejaría pasar un RPU
        # incompleto.
        for inp in ALL_INPUTS:
            with self.subTest(inp=inp):
                self.assertEqual(plan_for(inp).validate.fast_path, inp.drop_in)

    def test_solo_p7_fel_espera_una_fel_en_el_rpu_final(self):
        for inp in ALL_INPUTS:
            with self.subTest(inp=inp):
                esperado = "FEL" if inp.source_workflow == "p7_fel" else None
                self.assertEqual(plan_for(inp).validate.expected_el_type, esperado)


class TestAhorrosDelDropIn(unittest.TestCase):
    """El drop-in existe para no gastar: si gasta, no sirve de nada."""

    def setUp(self):
        self.plan = plan_for(WorkflowInputs("p7_fel", "trusted_p7_fel_final",
                                            True, "auto"))

    def test_no_demuxea_ni_muxea(self):
        self.assertFalse(self.plan.extract.needs_demux)
        self.assertFalse(self.plan.remux.needs_dovi_mux)
        self.assertIn("demux_dual_layer", self.plan.extract.skipped_markers)
        self.assertIn("mux_dual_layer", self.plan.extract.skipped_markers)

    def test_no_mergea(self):
        self.assertFalse(self.plan.inject.needs_merge)
        self.assertIn("merge_cmv40_transfer", self.plan.inject.skipped_markers)

    def test_no_adelanta_el_rpu_de_validacion(self):
        self.assertFalse(self.plan.remux.prewarm_validation)

    def test_opera_sobre_el_stream_completo(self):
        self.assertEqual(self.plan.inject.hevc_input, SOURCE_HEVC)
        self.assertEqual(self.plan.inject.hevc_output, SOURCE_INJECTED)
        self.assertEqual(self.plan.remux.hevc_for_mkv, SOURCE_INJECTED)


class TestTrustEfectivo(unittest.TestCase):
    """La condición que decide saltarse Fase D. Estaba escrita a mano en
    varios sitios del backend y siete veces en `app.js`."""

    def test_exige_gates_ok_y_modo_auto(self):
        self.assertTrue(WorkflowInputs("p7_fel", "x", True, "auto").trust_effective)
        self.assertFalse(WorkflowInputs("p7_fel", "x", False, "auto").trust_effective)
        self.assertFalse(
            WorkflowInputs("p7_fel", "x", True, "force_interactive").trust_effective)

    def test_el_chart_se_omite_exactamente_cuando_hay_trust_efectivo(self):
        for inp in ALL_INPUTS:
            with self.subTest(inp=inp):
                plan = plan_for(inp)
                self.assertEqual(plan.extract.skip_per_frame_data, inp.trust_effective)
                self.assertEqual(
                    "per_frame_data_skipped" in plan.extract.skipped_markers,
                    inp.trust_effective)

    def test_force_interactive_desactiva_el_drop_in(self):
        con = WorkflowInputs("p7_fel", "trusted_p7_fel_final", True, "auto")
        sin = WorkflowInputs("p7_fel", "trusted_p7_fel_final", True, "force_interactive")
        self.assertTrue(plan_for(con).drop_in)
        self.assertFalse(plan_for(sin).drop_in)
        self.assertTrue(plan_for(sin).inject.needs_merge)


class TestMergeSegunTarget(unittest.TestCase):
    """Un bin del mismo profile que el source se inyecta directo; el resto
    solo sirve como donante de levels."""

    def test_target_p8_sobre_source_single_layer_no_necesita_merge(self):
        for wf in ("p7_mel", "p8"):
            with self.subTest(workflow=wf):
                plan = plan_for(WorkflowInputs(wf, "trusted_p8_source", True, "auto"))
                self.assertFalse(plan.inject.needs_merge)

    def test_target_p7_sobre_source_single_layer_necesita_merge(self):
        for wf, tt in product(("p7_mel", "p8"),
                              ("trusted_p7_fel_final", "trusted_p7_mel_final", "generic")):
            with self.subTest(workflow=wf, target=tt):
                self.assertTrue(plan_for(WorkflowInputs(wf, tt, True, "auto")).inject.needs_merge)

    def test_p7_fel_sin_drop_in_siempre_mergea(self):
        for tt in TARGET_TYPES:
            for trust, ov in product((True, False), OVERRIDES):
                inp = WorkflowInputs("p7_fel", tt, trust, ov)
                if inp.drop_in:
                    continue
                with self.subTest(inp=inp):
                    self.assertTrue(plan_for(inp).inject.needs_merge)


class TestElTextoNoPuedeDivergirDeLaDecision(unittest.TestCase):
    """El `📋 Plan` y la decisión salen del mismo objeto. Estas
    comprobaciones fijan que sigan describiendo lo mismo."""

    def test_solo_la_rama_drop_in_dice_drop_in(self):
        for inp in ALL_INPUTS:
            with self.subTest(inp=inp):
                plan = plan_for(inp)
                self.assertEqual("DROP-IN" in plan.inject.plan_text, inp.drop_in)

    def test_quien_dice_merge_mergea(self):
        for inp in ALL_INPUTS:
            plan = plan_for(inp)
            if "mergeamos" in plan.inject.plan_text or "MERGE" in plan.inject.plan_text:
                with self.subTest(inp=inp):
                    self.assertTrue(plan.inject.needs_merge)

    def test_quien_dice_sin_merge_no_mergea(self):
        for inp in ALL_INPUTS:
            plan = plan_for(inp)
            if "sin merge" in plan.inject.plan_text or "directamente" in plan.inject.plan_text:
                with self.subTest(inp=inp):
                    self.assertFalse(plan.inject.needs_merge)

    def test_quien_anuncia_single_layer_produce_single_layer(self):
        for inp in ALL_INPUTS:
            plan = plan_for(inp)
            if "single-layer" in plan.inject.plan_text:
                with self.subTest(inp=inp):
                    self.assertTrue(inp.single_layer_output)
                    self.assertTrue(plan.inject.needs_profile8)

    def test_el_plan_de_la_fase_c_lista_lo_que_va_a_hacer(self):
        for inp in ALL_INPUTS:
            with self.subTest(inp=inp):
                plan = plan_for(inp)
                texto = plan.extract.plan_text
                self.assertEqual("dovi_tool demux" in texto, plan.extract.needs_demux)
                self.assertEqual("per_frame_data.json" in texto,
                                 not plan.extract.skip_per_frame_data)

    def test_el_plan_de_la_fase_g_nombra_los_ficheros_que_toca(self):
        # Cuando hay mux previo el plan nombra sus entradas (es lo que el
        # usuario ve moverse); cuando no, el HEVC que va directo a mkvmerge.
        for inp in ALL_INPUTS:
            with self.subTest(inp=inp):
                plan = plan_for(inp)
                if plan.remux.needs_dovi_mux:
                    for entrada in plan.remux.mux_inputs:
                        self.assertIn(entrada, plan.remux.plan_text)
                else:
                    self.assertIn(plan.remux.hevc_for_mkv, plan.remux.plan_text)

    def test_el_nombre_de_pista_refleja_el_profile_del_resultado(self):
        for inp in ALL_INPUTS:
            with self.subTest(inp=inp):
                plan = plan_for(inp)
                nombre = plan.remux.video_track_name
                if inp.single_layer_output:
                    self.assertIn("P8.1", nombre)
                else:
                    self.assertIn("P7 FEL", nombre)


class TestDesdeUnaSesion(unittest.TestCase):

    def test_lee_las_entradas_de_la_sesion(self):
        from models import CMv40Session
        from phases.cmv40_strategy import resolve_plan
        s = CMv40Session(
            id="x", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            source_workflow="p7_mel", target_type="trusted_p8_source",
            target_trust_ok=True, trust_override="auto",
        )
        plan = resolve_plan(s)
        self.assertFalse(plan.drop_in)
        self.assertTrue(plan.inject.needs_profile8)
        self.assertEqual(plan.inject.hevc_input, BL_HEVC)

    def test_una_sesion_sin_workflow_asume_p7_fel(self):
        from models import CMv40Session
        from phases.cmv40_strategy import resolve_plan
        s = CMv40Session(id="x", source_mkv_path="/x.mkv", source_mkv_name="x.mkv")
        self.assertEqual(resolve_plan(s).inputs.source_workflow, "p7_fel")


if __name__ == "__main__":
    unittest.main()
