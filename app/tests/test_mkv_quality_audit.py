"""Tests de la auditoría de calidad RPU del Tab 2.

Cubre la lógica de verdict (color + texto + tier) y los provenance hints
para todos los caminos del classifier:
  - CMv4.0 real FULL / CORE+ / CORE / minimal
  - CMv4.0 default (sintético)
  - CMv4.0 indeterminate
  - CMv2.9 puro (sin L8) en sus 3 tiers
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.mkv_analyze import (  # noqa: E402
    _build_quality_audit_from_rpu_analysis,
    _compute_provenance_hints,
    regenerar_textos_del_veredicto,
)
import i18n  # noqa: E402
from unittest import mock  # noqa: E402


def idioma(cual: str):
    """Fija el idioma del servidor durante el bloque.

    El idioma sale de `settings_store.get_idioma()`, o sea del `/config` de
    la instalación; en un test se parchea el resolutor, que es el único
    sitio que lo dice.
    """
    return mock.patch.object(i18n, "idioma_activo", lambda: cual)
from phases.rpu_analyze import RpuAnalysis, numeros_de_l8  # noqa: E402
from models import L8Combo  # noqa: E402


def _make_rpu(*, l8=0, l2=0, neutral=0.0, cmv40_frames=100, scene_cuts=0,
              mid_c=False, clip=False, l2_pqs=None, l2_combos=0) -> RpuAnalysis:
    a = RpuAnalysis()
    a.l8_unique_count = l8
    a.l2_unique_count = l2
    a.l8_neutral_pct = neutral
    a.frames_with_cmv40 = cmv40_frames
    a.total_frames = 1000
    a.scene_cuts = scene_cuts
    a.l8_has_mid_contrast = mid_c
    a.l8_has_clip_trim = clip
    a.l2_target_pqs = l2_pqs or []
    a.l2_combos = [None] * l2_combos if l2_combos else []
    # Para "real minimal" — al menos un combo con delta significativo
    if mid_c or clip:
        a.l8_combos = [L8Combo(
            target_display_index=1, trim_slope=2200,  # +152 del neutro
            trim_offset=2048, trim_power=2048, trim_chroma_weight=2048,
            trim_saturation_gain=2048, ms_weight=2048,
            target_mid_contrast=2121 if mid_c else None,
            clip_trim=2503 if clip else None,
            occurrence_count=10,
        )]
    return a


class TestVerdictCMv4(unittest.TestCase):

    def test_full_master_green(self):
        rpu = _make_rpu(l8=2547, neutral=0.11, scene_cuts=1487,
                        mid_c=True, clip=True)
        out = _build_quality_audit_from_rpu_analysis(rpu, False)
        self.assertEqual(out["quality_classification"], "real")
        self.assertEqual(out["quality_tier"], "full")
        self.assertEqual(out["quality_verdict_color"], "green")
        self.assertIn("FULL", out["quality_tier_label"])

    def test_core_rich_green(self):
        rpu = _make_rpu(l8=1119, neutral=0.001, scene_cuts=2617)
        out = _build_quality_audit_from_rpu_analysis(rpu, False)
        self.assertEqual(out["quality_tier"], "core_rich")
        self.assertEqual(out["quality_verdict_color"], "green")
        self.assertIn("CORE+", out["quality_tier_label"])

    def test_core_yellow(self):
        rpu = _make_rpu(l8=69, neutral=0.30, scene_cuts=2887)
        out = _build_quality_audit_from_rpu_analysis(rpu, False)
        self.assertEqual(out["quality_tier"], "core")
        self.assertEqual(out["quality_verdict_color"], "yellow")

    def test_default_red(self):
        rpu = _make_rpu(l8=1, neutral=1.0, cmv40_frames=100)
        out = _build_quality_audit_from_rpu_analysis(rpu, False)
        self.assertEqual(out["quality_classification"], "default")
        self.assertEqual(out["quality_verdict_color"], "red")
        self.assertIn("sintético", out["quality_verdict_text"].lower())

    def test_indeterminate_gray(self):
        rpu = _make_rpu(l8=5, neutral=0.80, cmv40_frames=100)
        out = _build_quality_audit_from_rpu_analysis(rpu, False)
        self.assertEqual(out["quality_classification"], "indeterminate")
        self.assertEqual(out["quality_verdict_color"], "gray")


class TestVerdictCMv29(unittest.TestCase):

    def test_native_master_green(self):
        rpu = _make_rpu(l2=73, l2_pqs=[62, 2081, 2851, 3079])
        out = _build_quality_audit_from_rpu_analysis(rpu, True)
        self.assertEqual(out["quality_verdict_color"], "green")
        self.assertIn("nativo", out["quality_verdict_text"].lower())
        self.assertEqual(out["quality_classification"], "real")

    def test_core_streaming_yellow(self):
        rpu = _make_rpu(l2=15, l2_pqs=[100, 1000])
        out = _build_quality_audit_from_rpu_analysis(rpu, True)
        self.assertEqual(out["quality_verdict_color"], "yellow")

    def test_minimal_red(self):
        rpu = _make_rpu(l2=3, l2_pqs=[1000])
        out = _build_quality_audit_from_rpu_analysis(rpu, True)
        self.assertEqual(out["quality_verdict_color"], "red")
        self.assertEqual(out["quality_classification"], "default")


class TestProvenanceHints(unittest.TestCase):

    def test_native_recent_master_with_l11_l254(self):
        rpu = _make_rpu(l8=2547, neutral=0.11, scene_cuts=1487,
                        mid_c=True, clip=True)
        flags = {"has_l4": False, "has_l9": True, "has_l10": True,
                 "has_l11": True, "has_l254": True}
        hints = _compute_provenance_hints(numeros_de_l8(rpu), "real", "full", flags, False)
        self.assertTrue(any("nativo CMv4.0 reciente" in h for h in hints))
        self.assertTrue(any("Metadata DV completa" in h for h in hints))

    def test_synthetic_bin_no_l4(self):
        rpu = _make_rpu(l8=1, neutral=1.0, cmv40_frames=100)
        flags = {"has_l4": False, "has_l11": False}
        hints = _compute_provenance_hints(numeros_de_l8(rpu), "default", "", flags, False)
        self.assertTrue(any("sintético" in h.lower() for h in hints))
        self.assertTrue(any("Sin L11" in h for h in hints))

    def test_converted_bin_with_l4(self):
        rpu = _make_rpu(l8=2, neutral=0.96, cmv40_frames=100)
        flags = {"has_l4": True, "has_l11": False}
        hints = _compute_provenance_hints(numeros_de_l8(rpu), "default", "", flags, False)
        self.assertTrue(any("convertido" in h.lower() for h in hints))

    def test_cmv4_pre_l11_master(self):
        rpu = _make_rpu(l8=2547, neutral=0.11, scene_cuts=1487, mid_c=True)
        flags = {"has_l4": False, "has_l9": True, "has_l10": True,
                 "has_l11": False, "has_l254": True}
        hints = _compute_provenance_hints(numeros_de_l8(rpu), "real", "full", flags, False)
        self.assertTrue(any("pre-L11" in h or "pre-IQ" in h for h in hints))

    def test_cmv29_native_with_l2(self):
        rpu = _make_rpu(l2=73, l2_pqs=[62, 2081, 2851, 3079])
        flags = {"has_l4": False}
        hints = _compute_provenance_hints(numeros_de_l8(rpu), "real", "", flags, True)
        self.assertTrue(any("CMv2.9 puro" in h for h in hints))
        self.assertTrue(any("trabajado por colorista" in h for h in hints))

    def test_cmv29_with_l4_compat(self):
        rpu = _make_rpu(l2=15, l2_pqs=[100, 1000])
        flags = {"has_l4": True}
        hints = _compute_provenance_hints(numeros_de_l8(rpu), "real", "", flags, True)
        self.assertTrue(any("L4 presente" in h for h in hints))

    def test_no_flags_no_hints(self):
        """Si dv_flags vacío, no peta y devuelve hints solo basados en classifier."""
        rpu = _make_rpu(l8=1, neutral=1.0, cmv40_frames=100)
        hints = _compute_provenance_hints(numeros_de_l8(rpu), "default", "", {}, False)
        # Siempre hay al menos 1 (default → "Bin sintético" o "Bin convertido")
        self.assertTrue(len(hints) >= 1)


class TestBuilderIntegratesHints(unittest.TestCase):

    def test_builder_with_flags_emits_hints(self):
        rpu = _make_rpu(l8=2547, neutral=0.11, scene_cuts=1487,
                        mid_c=True, clip=True)
        flags = {"has_l9": True, "has_l10": True, "has_l11": True,
                 "has_l254": True}
        out = _build_quality_audit_from_rpu_analysis(rpu, False, dv_flags=flags)
        self.assertIn("quality_provenance_hints", out)
        self.assertTrue(len(out["quality_provenance_hints"]) >= 1)

    def test_builder_without_flags_empty_hints(self):
        rpu = _make_rpu(l8=2547, neutral=0.11, scene_cuts=1487, mid_c=True, clip=True)
        out = _build_quality_audit_from_rpu_analysis(rpu, False)
        self.assertIn("quality_provenance_hints", out)
        # Con tier=full + classification=real, sin flags, no hay hints (todos
        # los hints CMv4.0 dependen de has_l*).
        self.assertEqual(out["quality_provenance_hints"], [])


class TestCancelByAuditId(unittest.TestCase):
    """Cancel viejo no debe afectar a un audit nuevo lanzado tras él.

    Reproduce el bug "cancelar audit + relanzar inmediatamente → el segundo
    audit falla con 'Cancelado por el usuario'" mediante una secuencia
    determinista sobre los singletons de estado.

    Los singletons viven en `routers/tab2.py` desde que Tab 2 salió de
    `main.py`; el cwd tiene que estar en app/ porque la app monta
    StaticFiles("static") con path relativo.
    """

    @classmethod
    def setUpClass(cls):
        import os
        cls._orig_cwd = os.getcwd()
        app_dir = Path(__file__).parent.parent  # .../app
        os.chdir(str(app_dir))

    @classmethod
    def tearDownClass(cls):
        import os
        os.chdir(cls._orig_cwd)

    def setUp(self):
        from routers.tab2 import (
            _mkv_quality_state, _mkv_quality_cancel,
            _mkv_quality_reset, _mkv_quality_check_cancel,
        )
        self.state = _mkv_quality_state
        self.cancel = _mkv_quality_cancel
        self.reset = _mkv_quality_reset
        self.check = _mkv_quality_check_cancel
        # Reset clean state
        self.state.update({"active": False, "audit_id": None})
        self.cancel["requested_for_id"] = None

    def test_check_cancel_fires_when_targeted(self):
        """Cancel del audit actual → _check raise."""
        audit_id = self.reset(file_name="movie.mkv")
        self.cancel["requested_for_id"] = audit_id
        with self.assertRaises(RuntimeError):
            self.check()

    def test_check_cancel_silent_when_obsolete(self):
        """Cancel de un audit anterior → _check NO raise sobre el nuevo audit."""
        first_id = self.reset(file_name="first.mkv")
        # Simula: cancel del primer audit marca requested_for_id=first_id
        self.cancel["requested_for_id"] = first_id
        # Usuario relanza → reset asigna audit_id nuevo y limpia el viejo
        second_id = self.reset(file_name="second.mkv")
        self.assertNotEqual(first_id, second_id)
        # En el endpoint nuevo, reset limpia requested_for_id (idempotencia)
        self.assertIsNone(self.cancel.get("requested_for_id"))
        # _check no debe disparar para el nuevo audit
        self.check()  # no raise

    def test_check_cancel_silent_when_no_cancel_pending(self):
        """Sin cancel pendiente, _check es no-op."""
        self.reset(file_name="movie.mkv")
        self.check()  # no raise

    def test_log_skipped_when_audit_id_obsolete(self):
        """Si _mkv_quality_log se llama con target_audit_id de un audit
        anterior, la línea NO debe añadirse al state.log_lines (que pertenece
        al audit actual). Sin esto, el except del primer endpoint en race
        contra el reset del segundo audit metía 'Cancelado por el usuario'
        en el log del audit nuevo."""
        from routers.tab2 import _mkv_quality_log
        first_id = self.reset(file_name="first.mkv")
        # Reset al segundo audit (cambia audit_id)
        second_id = self.reset(file_name="second.mkv")
        n_before = len(self.state["log_lines"])
        # Cancel/except del primer audit intenta loguear con target=first_id
        _mkv_quality_log("contaminación del primer audit", target_audit_id=first_id)
        # NO debe aparecer en el log del segundo audit
        self.assertEqual(len(self.state["log_lines"]), n_before)

    def test_log_accepted_when_audit_id_matches(self):
        """Si target_audit_id coincide con el actual, la línea se añade
        normalmente. Caso del flujo legítimo (el audit actual loguea de sí mismo)."""
        from routers.tab2 import _mkv_quality_log
        audit_id = self.reset(file_name="movie.mkv")
        n_before = len(self.state["log_lines"])
        _mkv_quality_log("línea propia", target_audit_id=audit_id)
        self.assertEqual(len(self.state["log_lines"]), n_before + 1)

    def test_log_accepted_when_no_target_passed(self):
        """Sin target_audit_id, comportamiento legacy: siempre añade."""
        from routers.tab2 import _mkv_quality_log
        self.reset(file_name="movie.mkv")
        n_before = len(self.state["log_lines"])
        _mkv_quality_log("línea sin guard")
        self.assertEqual(len(self.state["log_lines"]), n_before + 1)

    def test_finalize_if_skipped_when_audit_obsolete(self):
        """_state_finalize_if NO modifica el state si audit_id ya cambió."""
        from routers.tab2 import _mkv_quality_state_finalize_if
        first_id = self.reset(file_name="first.mkv")
        # Simula que el segundo audit ya empezó
        second_id = self.reset(file_name="second.mkv")
        self.state["active"] = True
        result = _mkv_quality_state_finalize_if(first_id, "obsoleto")
        self.assertFalse(result)
        # El state del segundo audit NO se ha tocado
        self.assertTrue(self.state["active"])
        self.assertNotEqual(self.state.get("error"), "obsoleto")


# `TestLightProfileCancelByJobId` vivía aquí y cubría `_lp_reset` /
# `_lp_check_cancel`, el job singleton del perfil de luminancia. Ese pipeline ya
# no existe: el perfil se calcula junto a la auditoría, compartiendo la
# extracción, así que hay UN solo job y su cancelación por audit_id es la que
# cubre `TestCancelByAuditId` justo arriba — el mismo patrón que probaba esta
# clase, sobre el único singleton que queda.


if __name__ == "__main__":
    unittest.main()


class TestElVeredictoCacheadoNoCongelaElIdioma(unittest.TestCase):
    """La caché de `/config/mkv_audits/` guarda el veredicto como TEXTO.

    Un MKV auditado con la app en castellano seguía diciendo «CMv2.9
    estándar — trims básicos del master» con la app en inglés, porque el
    bloque `quality` se reinyecta tal cual en el `DoviInfo`. Lo reportó el
    usuario sobre su histórico el 2026-09-17.

    La salida no es bumpear `CACHE_VERSION_QUALITY` —eso invalida la
    auditoría de todo el mundo y cuesta ~10 min de `extract-rpu` por MKV—
    sino re-derivar la prosa de los NÚMEROS, que son neutros y sí están
    cacheados. Lo que la caché decide sigue decidiéndolo la caché.
    """

    def _bloque(self, **kw):
        """Un bloque `quality` como el que hay en disco, con textos ES."""
        rpu = _make_rpu(**kw)
        return _build_quality_audit_from_rpu_analysis(rpu, kw.pop("_cmv29", False))

    def test_los_textos_se_rehacen_en_el_idioma_de_ahora(self):
        # CMv2.9 puro: es el caso del usuario («CMv2.9 estándar — trims
        # básicos del master» + tier «CMv2.9 CORE»).
        rpu = _make_rpu(l8=0, cmv40_frames=0, l2=1026, l2_pqs=[2081, 3079])
        with idioma("es"):
            en_disco = _build_quality_audit_from_rpu_analysis(rpu, True)
        # Las dos cadenas que el usuario pegó del navegador, literales.
        self.assertIn("trims básicos del master", en_disco["quality_verdict_text"])
        self.assertIn("combos únicos", en_disco["quality_reason"])
        self.assertEqual("CMv2.9 CORE", en_disco["quality_tier_label"])

        with idioma("en"):
            servido = regenerar_textos_del_veredicto(en_disco, {})
        self.assertNotIn("básicos", servido["quality_verdict_text"])
        self.assertNotIn("únicos", servido["quality_reason"])
        # El tier es un rótulo neutro y NO cambia: no era una fuga.
        self.assertEqual("CMv2.9 CORE", servido["quality_tier_label"])

    def test_las_decisiones_cacheadas_no_cambian(self):
        """Lo que se rehace es la prosa; la clasificación es un dato."""
        rpu = _make_rpu(l8=1132, neutral=0.0, scene_cuts=2000, mid_c=True, clip=True)
        with idioma("es"):
            en_disco = _build_quality_audit_from_rpu_analysis(rpu, False)
        with idioma("en"):
            servido = regenerar_textos_del_veredicto(en_disco, {})
        for k in ("quality_classification", "quality_tier", "quality_verdict_color",
                  "quality_l8_unique_count", "quality_total_frames_rpu"):
            self.assertEqual(servido[k], en_disco[k], k)
        self.assertEqual(servido["quality_tier_label"], "CMv4 FULL")

    def test_un_bloque_sin_clasificacion_no_se_toca(self):
        """Una auditoría a medias no se reinterpreta: se deja como está."""
        self.assertEqual(regenerar_textos_del_veredicto({"a": 1}, {}), {"a": 1})

    def test_el_veredicto_regenerado_es_el_mismo_que_el_recien_calculado(self):
        """Regenerar desde la caché tiene que dar EXACTAMENTE lo que daría
        un análisis nuevo. Si no, la card cambiaría de contenido al cerrar
        y reabrir el MKV, que es indistinguible de un bug del classifier."""
        casos = [
            dict(l8=1132, neutral=0.0, scene_cuts=2000, mid_c=True, clip=True),
            dict(l8=69, neutral=0.30, scene_cuts=2887),
            dict(l8=1119, neutral=0.10, scene_cuts=2617),
            dict(l8=1, neutral=0.99, scene_cuts=500),
            dict(l8=5, neutral=0.80, scene_cuts=500),
            dict(l8=3, neutral=0.20, scene_cuts=100, mid_c=True),
        ]
        flags = {"has_l9": True, "has_l11": True, "has_l254": True}
        for kw in casos:
            rpu = _make_rpu(**kw)
            fresco = _build_quality_audit_from_rpu_analysis(rpu, False, dv_flags=flags)
            self.assertEqual(regenerar_textos_del_veredicto(fresco, flags), fresco, kw)
        # Y el camino CMv2.9 puro.
        for l2 in (1026, 40, 12, 4):
            rpu = _make_rpu(l8=0, cmv40_frames=0, l2=l2, l2_pqs=[2081, 3079, 3696])
            fresco = _build_quality_audit_from_rpu_analysis(rpu, True, dv_flags=flags)
            self.assertEqual(regenerar_textos_del_veredicto(fresco, flags), fresco, l2)
