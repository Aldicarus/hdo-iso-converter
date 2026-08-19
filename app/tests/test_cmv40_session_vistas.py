"""
Vistas agrupadoras e invariantes de `CMv40Session`, sin tocar el JSON.

`CMv40Session` tiene 78 campos planos con grupos evidentes por prefijo. Se
evaluó anidarlos y se descartó por dos razones medidas:

  * `session.model_dump()` va tal cual a la UI, que lee **61** de esos campos.
    Anidarlos no da error: da `undefined` en 61 puntos.
  * Pydantic ignora en silencio las claves que no reconoce, así que un JSON
    existente cargado con un modelo anidado devuelve los sub-modelos VACÍOS y
    el primer save reescribe el fichero **sin** el análisis L8 ni los combos
    L2 — horas de `dovi_tool export` perdidas sin un mensaje. Con proyectos en
    el /config de usuarios que no podemos inspeccionar, eso no tiene vuelta
    atrás.

Lo que sí se hizo: properties calculadas (que **no** se serializan) para la
legibilidad, e invariantes que **normalizan y avisan, sin lanzar** — un
validador que reventara convertiría una incoherencia menor en "la app no
carga tu proyecto".
"""
import logging
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from models import CMv40Session, CMv40Phase  # noqa: E402

BASE = dict(id="cmv40_test_1700000000", source_mkv_path="/x/a.mkv",
            source_mkv_name="a.mkv")


def sesion(**extra) -> CMv40Session:
    return CMv40Session(**BASE, **extra)


class TestElJsonNoCambia(unittest.TestCase):
    """La razón de ser del diseño: el contrato con la UI se mantiene."""

    def test_las_vistas_no_se_serializan(self):
        d = sesion(target_l8_classification="real").model_dump()
        for vista in ("l8", "pending_target", "recommendation"):
            self.assertNotIn(vista, d,
                             f"'{vista}' no debe aparecer en el JSON: la UI lee "
                             f"los campos planos")

    def test_los_campos_planos_siguen_ahí(self):
        d = sesion(target_l8_classification="real",
                   target_l8_unique_count=412).model_dump()
        self.assertEqual(d["target_l8_classification"], "real")
        self.assertEqual(d["target_l8_unique_count"], 412)

    def test_el_numero_de_campos_no_cambia(self):
        # 78 campos. Si esto sube, alguien añadió un campo al modelo: revisar
        # que el summary del sidebar no lo vacíe y que la UI lo espere.
        self.assertEqual(len(sesion().model_dump()), 78)

    def test_un_json_plano_se_carga_intacto(self):
        # El caso que hace inviable anidar: aquí NO se pierde nada.
        crudo = {**BASE, "target_l8_classification": "real",
                 "target_l8_unique_count": 412,
                 "target_l8_quality_tier": "core_rich"}
        s = CMv40Session.model_validate(crudo)
        self.assertEqual(s.l8.classification, "real")
        self.assertEqual(s.l8.unique_count, 412)
        self.assertEqual(s.model_dump()["target_l8_classification"], "real")


class TestVistaL8(unittest.TestCase):

    def test_agrupa_los_once_campos(self):
        s = sesion(target_l8_classification="real", target_l8_quality_tier="full",
                   target_l8_quality_label="[CMv4 FULL]", target_l8_unique_count=3,
                   target_l8_scene_cuts=1101, target_l8_has_mid_contrast=True)
        self.assertEqual(s.l8.classification, "real")
        self.assertEqual(s.l8.quality_tier, "full")
        self.assertEqual(s.l8.quality_label, "[CMv4 FULL]")
        self.assertEqual(s.l8.unique_count, 3)
        self.assertEqual(s.l8.scene_cuts, 1101)
        self.assertTrue(s.l8.has_mid_contrast)

    def test_is_real_e_is_synthetic(self):
        self.assertTrue(sesion(target_l8_classification="real").l8.is_real)
        self.assertTrue(sesion(target_l8_classification="default").l8.is_synthetic)
        indet = sesion(target_l8_classification="indeterminate").l8
        self.assertFalse(indet.is_real)
        self.assertFalse(indet.is_synthetic)

    def test_las_listas_se_copian(self):
        # La vista no debe dar acceso mutable al estado de la sesión.
        from models import L8Combo
        combo = L8Combo(target_display_index=1, trim_slope=2048, trim_offset=2048,
                        trim_power=2048, trim_chroma_weight=2048,
                        trim_saturation_gain=2048, ms_weight=2048)
        s = sesion(target_l8_combos=[combo], target_l8_target_indices=[1, 2])
        s.l8.combos.append(combo)
        s.l8.target_indices.append(99)
        self.assertEqual(len(s.target_l8_combos), 1)
        self.assertEqual(s.target_l8_target_indices, [1, 2])

    def test_es_inmutable(self):
        from dataclasses import FrozenInstanceError
        with self.assertRaises(FrozenInstanceError):
            sesion().l8.classification = "otro"      # type: ignore[misc]


class TestVistasPendingTargetYRecomendacion(unittest.TestCase):

    def test_pending_target_exists(self):
        self.assertFalse(sesion().pending_target.exists)
        self.assertTrue(sesion(pending_target_kind="drive").pending_target.exists)

    def test_pending_target_agrupa(self):
        s = sesion(pending_target_kind="drive", pending_target_file_id="abc",
                   pending_target_file_name="x.bin")
        self.assertEqual(s.pending_target.kind, "drive")
        self.assertEqual(s.pending_target.file_id, "abc")
        self.assertEqual(s.pending_target.file_name, "x.bin")

    def test_recommendation_says_keep(self):
        self.assertTrue(sesion(recommended_action="keep").recommendation.says_keep)
        self.assertFalse(sesion(recommended_action="restore_dropin")
                         .recommendation.says_keep)


class TestInvariantesNormalizanSinLanzar(unittest.TestCase):
    """Ninguno debe impedir cargar una sesión: solo corregir y avisar."""

    def assertAvisa(self, **extra) -> CMv40Session:
        with self.assertLogs("models", level="WARNING") as cm:
            s = sesion(**extra)
        self.assertTrue(cm.output, "la corrección debe quedar en el log")
        return s

    def test_trust_ok_con_target_inservible_se_baja(self):
        for tt in ("", "incompatible"):
            with self.subTest(target_type=tt):
                s = self.assertAvisa(target_trust_ok=True, target_type=tt)
                self.assertFalse(s.target_trust_ok)

    def test_ack_pendiente_sin_motivos_se_levanta(self):
        # Sin motivos no hay banner, así que el usuario no tendría con qué
        # desbloquear el proyecto.
        s = self.assertAvisa(awaiting_critical_ack=True, critical_gate_failures=[])
        self.assertFalse(s.awaiting_critical_ack)

    def test_ack_pendiente_ya_aceptado_se_levanta(self):
        s = self.assertAvisa(awaiting_critical_ack=True,
                             user_acknowledged_degradation=True,
                             critical_gate_failures=[{"gate": "l6_div"}])
        self.assertFalse(s.awaiting_critical_ack)
        self.assertTrue(s.user_acknowledged_degradation)

    def test_done_con_error_lo_descarta(self):
        s = self.assertAvisa(phase=CMv40Phase.DONE, error_message="algo falló")
        self.assertEqual(s.error_message, "")

    def test_done_con_running_phase_lo_limpia(self):
        s = self.assertAvisa(phase=CMv40Phase.DONE, running_phase="remux")
        self.assertIsNone(s.running_phase)

    def test_ninguno_lanza(self):
        # El punto entero del diseño: con datos de usuarios que no podemos
        # restaurar, cargar siempre gana a validar estrictamente.
        todos = dict(target_trust_ok=True, target_type="incompatible",
                     awaiting_critical_ack=True, user_acknowledged_degradation=True,
                     phase=CMv40Phase.DONE, error_message="x",
                     running_phase="remux")
        s = sesion(**todos)     # no debe lanzar
        self.assertIsInstance(s, CMv40Session)


class TestLosCasosLegitimosNoSeTocan(unittest.TestCase):

    def test_un_proyecto_trusted_conserva_el_trust(self):
        s = sesion(target_trust_ok=True, target_type="trusted_p7_fel_final",
                   phase="injected")
        self.assertTrue(s.target_trust_ok)

    def test_un_ack_pendiente_con_motivos_se_conserva(self):
        s = sesion(awaiting_critical_ack=True,
                   critical_gate_failures=[{"gate": "l6_div", "why": "…"}])
        self.assertTrue(s.awaiting_critical_ack)

    def test_un_error_en_fase_intermedia_se_conserva(self):
        s = sesion(phase="injected", error_message="mkvmerge falló")
        self.assertEqual(s.error_message, "mkvmerge falló")

    def test_una_fase_en_curso_se_conserva(self):
        s = sesion(phase="injected", running_phase="remux")
        self.assertEqual(s.running_phase, "remux")

    def test_cargar_no_emite_avisos_en_el_caso_normal(self):
        logger = logging.getLogger("models")
        with self.assertLogs(logger, level="WARNING") as cm:
            logger.warning("centinela")
            sesion(target_trust_ok=True, target_type="trusted_p8_source")
        self.assertEqual(len(cm.output), 1, "no debe avisar de nada más")


if __name__ == "__main__":
    unittest.main()
