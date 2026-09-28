"""El MKV de Tab 3 se entrega con su análisis de Tab 2 ya hecho.

El pipeline CMv4.0 extrae el RPU del stream que muxea —para inyectarlo y,
en la rama merge, otra vez para validar— y eso es el **~97 %** de lo que
cuesta el análisis extendido de Tab 2: ~650 s medidos frente a ~7 s del
export por niveles. Con el RPU delante, dejar la radiografía DV+HDR y el
perfil de luminancia listos cuesta segundos.

Tres cosas se pueden romper en silencio, y de ahí salen los tests de aquí:

  · **el RPU equivocado.** Hay cuatro en el workdir y solo uno describe lo
    que acabó dentro del MKV. La Fase F anota el que inyectó —con el valor
    de después del merge y de después de la conversión a Profile 8— en vez
    de dejar que la Fase H lo deduzca de la matriz de workflows;
  · **el orden.** El fingerprint de la caché es el SHA del primer 1 MB, así
    que esto tiene que correr con el MKV en su nombre definitivo y **ya
    firmado**. Un solo paso antes y la caché nace huérfana, sin que nada
    falle;
  · **el bloque que falta.** Sin `basic` cacheado, la re-inyección del
    extendido no ocurre hasta la SEGUNDA apertura: el usuario abriría el
    MKV, no vería nada y concluiría que no funciona.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_precache_tab2 -v
"""
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import (  # noqa: E402
    PhaseTestCase, RpuProps, make_session, write_artifacts,
)

FRAMES = 50
SRC_FEL = RpuProps(profile=7, el_type="FEL", cm_version="v2.9", frames=FRAMES)
TGT_V40 = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                   frames=FRAMES, has_l8=True)
INJ_V40 = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                   frames=FRAMES, has_l8=True)


# ══════════════════════════════════════════════════════════════════════
#  `quality_desde_rpu`: el tramo sin los pasos caros
# ══════════════════════════════════════════════════════════════════════

class TestQualityDesdeRpu(PhaseTestCase):

    def setUp(self):
        super().setUp()
        from phases import mkv_analyze
        self.mod = mkv_analyze
        self.rpu = self.wd / "RPU_target.bin"
        write_artifacts(self.wd, self.rpu.name, props=TGT_V40)
        self.tb.define_rpu(self.rpu.name, **TGT_V40.as_dict())
        self.tb.define_rpu_levels(self.rpu.name, l8_indices=[1, 28],
                                  l9_primary=0, l11_content_type=1)

    async def test_da_los_combos_sin_extraer_nada(self):
        payload = await self.mod.quality_desde_rpu(self.rpu, self.wd)
        self.assertIsNotNone(payload)
        self.assertGreater(payload["quality_total_frames_rpu"], 0)
        self.assertFalse(self.tb.ran("ffmpeg"),
                         "el RPU ya está: extraerlo otra vez es el 97 % del coste")
        self.assertFalse(self.tb.ran("dovi_tool", "extract-rpu"))

    async def test_trae_el_perfil_de_luminancia(self):
        """Es la mitad del valor: en Tab 2 cuesta lo mismo que los combos y
        antes no se persistía en ninguna parte."""
        payload = await self.mod.quality_desde_rpu(self.rpu, self.wd)
        self.assertIn("light_profile", payload)
        self.assertGreater(payload["light_profile"]["total_frames"], 0)

    async def test_pide_L5_y_L6_en_la_MISMA_pasada(self):
        """Un export por cada análisis pagaría dos veces el mismo trabajo."""
        await self.mod.quality_desde_rpu(self.rpu, self.wd)
        exports = self.tb.find("dovi_tool", "export")
        self.assertEqual(len(exports), 1, [c.argv for c in exports])
        argv = " ".join(exports[0].argv)
        for nivel in ("level1", "level5", "level6", "level8"):
            self.assertIn(nivel, argv, nivel)

    async def test_si_el_export_falla_devuelve_None_sin_lanzar(self):
        self.tb.fail("dovi_tool", "export", rc=2)
        self.assertIsNone(await self.mod.quality_desde_rpu(self.rpu, self.wd))

    async def test_no_lanza_pase_lo_que_pase_dentro(self):
        """El contrato es «devuelve None, nunca lanza», y hay que probarlo
        inyectando el fallo en la dependencia.

        Los fallos que se pueden provocar desde fuera NO llegan al `except`:
        un export con rc distinto de 0 cae al camino legacy y devuelve un
        análisis vacío, que atrapa el guard de `total_frames`; y un tmpdir
        inexistente no molesta porque el export lo crea. Así que sin esto el
        `except` podría desaparecer sin que nada fallara — lo destapó la
        mutación, que se estaba cazando con un test ya en rojo.
        """
        from unittest.mock import patch
        async def revienta(*a, **kw):
            raise RuntimeError("lo que sea, dentro")
        with patch.object(self.mod, "_exportar_una_vez", revienta):
            self.assertIsNone(
                await self.mod.quality_desde_rpu(self.rpu, self.wd))

    async def test_un_rpu_que_no_existe_devuelve_None_sin_lanzar(self):
        self.assertIsNone(
            await self.mod.quality_desde_rpu(self.wd / "no_existe.bin", self.wd))


# ══════════════════════════════════════════════════════════════════════
#  La Fase F anota QUÉ RPU inyectó
# ══════════════════════════════════════════════════════════════════════

class TestLaFaseFAnotaElRpu(PhaseTestCase):
    """Hay cuatro RPU en el workdir y solo uno está dentro del MKV.

    Se anota en la Fase F y no se deduce en la Fase H a propósito: deducirlo
    sería replicar la matriz de `cmv40_strategy`, que es exactamente lo que
    ese módulo existe para evitar — y la clase de divergencia que produjo el
    bug de "Te van a matar".
    """

    def _sesion(self, **kw):
        self.tb.define_rpu("RPU_source.bin", **SRC_FEL.as_dict())
        self.tb.define_rpu("RPU_target.bin", **TGT_V40.as_dict())
        write_artifacts(self.wd, "RPU_source.bin", props=SRC_FEL)
        write_artifacts(self.wd, "RPU_target.bin", props=TGT_V40)
        write_artifacts(self.wd, "source.hevc", props=SRC_FEL)
        write_artifacts(self.wd, "BL.hevc", props=SRC_FEL)
        write_artifacts(self.wd, "EL.hevc", props=SRC_FEL)
        write_artifacts(self.wd, "source.mkv")
        base = dict(source_frame_count=FRAMES, target_frame_count=FRAMES,
                    target_trust_ok=True, trust_override="auto")
        base.update(kw)
        s = make_session(self.wd, **base)
        self.tb.define_media(Path(s.source_mkv_path).name,
                             duration=7200.0, frames=FRAMES)
        return s

    async def test_en_drop_in_anota_el_bin_que_se_inyecta(self):
        from phases.cmv40_pipeline import run_phase_f_inject
        s = self._sesion(source_workflow="p7_fel",
                         target_type="trusted_p7_fel_final")
        await run_phase_f_inject(s, self.log)
        self.assertEqual(s.rpu_inyectado, "RPU_target.bin")

    async def test_en_merge_anota_el_RESULTADO_del_merge(self):
        """No el target: lo que va dentro es la mezcla."""
        from phases.cmv40_pipeline import run_phase_f_inject
        s = self._sesion(source_workflow="p7_fel", target_type="generic",
                         target_trust_ok=False)
        await run_phase_f_inject(s, self.log)
        self.assertEqual(s.rpu_inyectado, "RPU_merged.bin")

    async def test_con_conversion_a_profile8_anota_el_CONVERTIDO(self):
        """En single-layer el RPU se pasa a Profile 8 antes de inyectar, y es
        ese el que acaba dentro — anotar el previo describiría otro fichero."""
        from phases.cmv40_pipeline import run_phase_f_inject
        s = self._sesion(source_workflow="p7_mel",
                         target_type="trusted_p7_mel_final")
        await run_phase_f_inject(s, self.log)
        # El nombre EXACTO del convertido (`_ensure_profile8_rpu` produce
        # `{stem}_p81.bin`). Comprobar solo «distinto de RPU_target.bin» era
        # débil: este caso pasa por el merge, así que el previo ya se llama
        # `RPU_merged.bin` y la comprobación se cumplía sin conversión —
        # lo destapó la mutación.
        self.assertTrue(s.rpu_inyectado.endswith("_p81.bin"),
                        f"anotó {s.rpu_inyectado!r}, no el convertido")
        self.assertTrue((self.wd / s.rpu_inyectado).exists(),
                        f"{s.rpu_inyectado} no está en el workdir")


# ══════════════════════════════════════════════════════════════════════
#  La Fase H lo deja en la caché de Tab 2
# ══════════════════════════════════════════════════════════════════════

class TestLaFaseHPrecachea(PhaseTestCase):

    def setUp(self):
        super().setUp()
        import storage
        from phases import mkv_analyze
        self.cache = self.tmp / "mkv_audits"
        self.cache.mkdir()
        for mod, attr, val in ((storage, "MKV_AUDIT_DIR", self.cache),
                               (mkv_analyze, "TMP_DIR", str(self.tmp))):
            orig = getattr(mod, attr)
            setattr(mod, attr, val)
            self.addCleanup(setattr, mod, attr, orig)

    def _sesion(self, *, rpu_inyectado="RPU_target.bin", prewarm=False):
        self.tb.define_rpu("RPU_source.bin", **SRC_FEL.as_dict())
        self.tb.define_rpu("RPU_target.bin", **TGT_V40.as_dict())
        write_artifacts(self.wd, "RPU_source.bin", props=SRC_FEL)
        write_artifacts(self.wd, "RPU_target.bin", props=TGT_V40)
        write_artifacts(self.wd, "source_injected.hevc", props=INJ_V40)
        write_artifacts(self.wd, "source.mkv")
        for nombre in ("RPU_target.bin",):
            self.tb.define_rpu_levels(nombre, l8_indices=[1, 28],
                                      l9_primary=0, l11_content_type=1)
        if prewarm:
            write_artifacts(self.wd, "_validate_full_rpu.bin", props=INJ_V40)
            self.tb.define_rpu("_validate_full_rpu.bin", **INJ_V40.as_dict())
            self.tb.define_rpu_levels("_validate_full_rpu.bin", l8_indices=[1, 9, 28],
                                      l9_primary=0, l11_content_type=1)
        s = make_session(
            self.wd, source_workflow="p7_fel",
            target_type="trusted_p7_fel_final", target_trust_ok=True,
            trust_override="auto", source_frame_count=FRAMES,
            target_frame_count=FRAMES, rpu_inyectado=rpu_inyectado,
        )
        self.tb.define_media(Path(s.source_mkv_path).name,
                             duration=7200.0, frames=FRAMES)
        # El `.mkv.tmp` que la Fase G habría dejado, y su ficha para el
        # análisis básico que el precache lanza sobre el MKV final.
        tmp = self.output_dir / f"{s.output_mkv_name}.tmp"
        write_artifacts(self.output_dir, tmp.name, props=INJ_V40)
        for nombre in (tmp.name, s.output_mkv_name):
            self.tb.define_media(nombre, duration=7200.0, frames=FRAMES)
            self.tb.define_mkv(nombre, duration_s=7200.0)
            self.tb.define_mediainfo(nombre)
            self.tb.define_pgs_packets(nombre, {})
        return s

    async def _correr(self, s):
        from phases.cmv40_pipeline import run_phase_h_validate
        await run_phase_h_validate(s, self.log)
        return self.output_dir / s.output_mkv_name

    def _cache(self):
        ficheros = list(self.cache.glob("*.json"))
        self.assertEqual(len(ficheros), 1,
                         f"se esperaba 1 entrada de caché, hay {len(ficheros)}")
        return json.loads(ficheros[0].read_text(encoding="utf-8"))

    async def test_deja_los_DOS_bloques(self):
        """Sin `basic`, la re-inyección del extendido no ocurre hasta la
        SEGUNDA apertura: el usuario abriría el MKV y no vería nada."""
        await self._correr(self._sesion())
        d = self._cache()
        self.assertTrue(d.get("basic"), "falta el análisis básico")
        self.assertTrue(d.get("quality"), "falta el análisis extendido")

    async def test_el_extendido_trae_el_perfil_de_luminancia(self):
        await self._correr(self._sesion())
        q = self._cache()["quality"]
        self.assertGreater(q["quality_total_frames_rpu"], 0)
        self.assertIn("light_profile", q)

    async def test_SIN_volver_a_extraer_el_rpu_completo(self):
        """Es el punto de todo esto: el RPU ya está, y extraerlo otra vez
        serían los ~650 s que el pipeline acaba de pagar.

        El análisis básico sí hace un `extract-rpu`, pero **con `--limit`**:
        es el sniff DV acotado de `_run_dovi_on_mkv`, que ya se hacía y cuesta
        segundos. El discriminante es ese flag y no la extensión del fichero
        —el sniff opera sobre el propio MKV—, así que filtrar por `.mkv`
        señalaba al comando bueno.
        """
        await self._correr(self._sesion())
        completos = [c for c in self.tb.find("dovi_tool", "extract-rpu")
                     if "--limit" not in c.argv]
        self.assertEqual(completos, [], [c.argv for c in completos])

    async def test_el_fingerprint_es_del_fichero_FIRMADO(self):
        """El orden importa y no falla si se hace mal: el fingerprint es el
        SHA del primer 1 MB, donde vive la cabecera que la firma cambia. Un
        paso antes y la caché nace huérfana, en silencio."""
        from storage import compute_mkv_fingerprint
        final = await self._correr(self._sesion())
        guardado = self._cache()["fingerprint"]["sha256_1mb"]
        self.assertEqual(guardado, compute_mkv_fingerprint(str(final))["sha256_1mb"])

    async def test_prefiere_el_rpu_EXTRAIDO_del_stream(self):
        """Los dos describen el mismo RPU, pero el del prewarm es evidencia
        de lo que hay dentro del fichero y el inyectado es lo que se
        pretendía meter."""
        await self._correr(self._sesion(prewarm=True))
        exports = self.tb.find("dovi_tool", "export")
        self.assertTrue(exports)
        self.assertTrue(any("_validate_full_rpu.bin" in " ".join(c.argv)
                            for c in exports),
                        [c.argv for c in exports])

    async def test_sin_rpu_no_precachea_y_la_fase_sigue_bien(self):
        s = self._sesion(rpu_inyectado="")
        (self.wd / "RPU_target.bin").unlink()
        final = await self._correr(s)
        self.assertTrue(final.exists(), "la fase tenía que entregar el MKV")
        self.assertEqual(list(self.cache.glob("*.json")), [])

    async def test_un_fallo_del_extendido_no_tumba_la_fase(self):
        """El MKV ya está entregado y validado: perder un análisis que se
        relanza con un botón no justifica manchar un job que salió bien.

        Y el básico SÍ se queda: se persiste antes del export, así que un
        export que falle deja el MKV con media ficha en vez de con ninguna.
        """
        self.tb.fail("dovi_tool", "export", rc=2)
        final = await self._correr(self._sesion())
        self.assertTrue(final.exists(), "la fase tenía que entregar el MKV")
        d = self._cache()
        self.assertTrue(d.get("basic"), "el básico sí debería quedarse")
        self.assertIsNone(d.get("quality"))


if __name__ == "__main__":
    unittest.main()
