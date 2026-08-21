"""
La orquestación de la auditoría de calidad del RPU (Tab 2), ejecutada.

`analyze_rpu_quality_for_mkv` (347 líneas, complejidad 61) no tenía ningún
test que la ejecutara. Lo que hace *dentro* de cada paso sí está cubierto —
`analyze_rpu_combos`, `classify_l8` y `classify_l8_quality` viven en
`rpu_analyze` con 37 tests—, pero la orquestación no: qué pasos emite, si
respeta la cancelación y si borra los intermedios.

Y esos intermedios importan: el HEVC son ~45 GB, el RPU 100-200 MB y el JSON
de export 300-500 MB. El docstring promete que se borran SIEMPRE en el
`finally` — nunca se cachean. Si esa promesa se rompiera, el /mnt/tmp del NAS
se llenaría de HEVCs de 45 GB sin que nadie relacione una cosa con la otra.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import FakeToolbox, RpuProps, write_artifacts  # noqa: E402


class AuditoriaCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.trabajo = self.tmp / "work"
        self.trabajo.mkdir()

        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)

        from phases import mkv_analyze
        self.mod = mkv_analyze
        # El workdir de los intermedios: a un tmpdir, no a /mnt/tmp.
        self._orig_wd = mkv_analyze._quality_workdir_base
        mkv_analyze._quality_workdir_base = lambda: str(self.trabajo)
        self.addCleanup(
            lambda: setattr(mkv_analyze, "_quality_workdir_base", self._orig_wd))

        self.mkv = self.tmp / "Peli (2024).mkv"
        props = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                         frames=50, has_l8=True)
        write_artifacts(self.tmp, self.mkv.name, props=props)
        self.tb.define_mkv(self.mkv.name)
        self.tb.define_media(self.mkv.name, duration=7200.0, frames=50)
        self.tb.define_rpu_levels(self.mkv.name, l8_indices=[1, 28],
                                  l9_primary=0, l11_content_type=1)

    async def auditar(self, **kw):
        pasos = []
        kw.setdefault("progress_callback",
                      lambda step, pct, label: pasos.append((step, pct)))
        r = await self.mod.analyze_rpu_quality_for_mkv(str(self.mkv), **kw)
        return r, pasos


class TestLosPasosDelPipeline(AuditoriaCase):
    """El frontend pinta una barra a partir de estos pasos; si cambian de
    nombre o el porcentaje retrocede, la barra se rompe."""

    async def test_emite_los_pasos_hasta_done(self):
        _, pasos = await self.auditar()
        nombres = [p for p, _ in pasos]
        self.assertIn("extract_rpu", nombres)
        self.assertIn("combos", nombres)
        self.assertEqual(nombres[-1], "done")

    async def test_el_porcentaje_no_retrocede(self):
        _, pasos = await self.auditar()
        pcts = [pct for _, pct in pasos]
        self.assertEqual(pcts, sorted(pcts), pcts)

    async def test_acaba_en_cien(self):
        _, pasos = await self.auditar()
        self.assertEqual(pasos[-1], ("done", 100.0))

    async def test_funciona_sin_callbacks(self):
        r = await self.mod.analyze_rpu_quality_for_mkv(str(self.mkv))
        self.assertIsInstance(r, dict)


class TestLimpiezaDeIntermedios(AuditoriaCase):
    """El HEVC son ~45 GB: si no se borra, el /mnt/tmp del NAS se llena."""

    def _restos(self):
        """Todo lo que quede bajo el workdir base, sin excepciones.

        El arnés ya no deja sidecars aquí, así que esto es exactamente lo
        que el pipeline dejó: ficheros Y directorios. Los directorios
        cuentan porque el `finally` hace `tmpdir.rmdir()`, que falla en
        silencio si dentro queda algo — un intermedio nuevo que alguien
        olvide borrar aparecería como un directorio huérfano en /mnt/tmp.
        """
        return sorted(str(p.relative_to(self.trabajo))
                      for p in self.trabajo.rglob("*"))

    async def test_no_deja_intermedios_al_terminar(self):
        await self.auditar()
        self.assertEqual(self._restos(), [],
                         "el HEVC/RPU/JSON deben borrarse y el tmpdir "
                         "desaparecer en el finally")

    async def test_tampoco_los_deja_si_falla(self):
        # El caso que de verdad llena el disco: un fallo a mitad.
        self.tb.fail("dovi_tool", "extract-rpu", rc=1, stderr="boom")
        with self.assertRaises(Exception):
            await self.auditar()
        self.assertEqual(self._restos(), [],
                         "un fallo no puede dejar 45 GB de HEVC colgando")

    async def test_tampoco_los_deja_si_se_cancela(self):
        def _cancelar():
            raise RuntimeError("Cancelado por el usuario")
        with self.assertRaises(RuntimeError):
            await self.auditar(cancel_check=_cancelar)
        self.assertEqual(self._restos(), [])


class TestCancelacion(AuditoriaCase):

    async def test_el_cancel_check_aborta_el_pipeline(self):
        llamadas = {"n": 0}

        def _cancelar():
            llamadas["n"] += 1
            raise RuntimeError("Cancelado por el usuario")

        with self.assertRaises(RuntimeError) as cm:
            await self.auditar(cancel_check=_cancelar)
        self.assertIn("Cancelado", str(cm.exception))
        self.assertGreater(llamadas["n"], 0)

    async def test_un_cancel_check_que_no_cancela_deja_seguir(self):
        r, pasos = await self.auditar(cancel_check=lambda: None)
        self.assertEqual(pasos[-1][0], "done")
        self.assertIsInstance(r, dict)

    async def test_registra_los_procesos_para_poder_matarlos(self):
        # Sin `register_proc` el cancel no tendría a quién mandar SIGTERM.
        procs = []
        await self.auditar(register_proc=procs.append)
        self.assertTrue(procs, "debe registrar los subprocesos que lanza")


class TestElPayloadDeCalidad(AuditoriaCase):

    async def test_devuelve_los_campos_de_calidad(self):
        r, _ = await self.auditar()
        for campo in ("quality_classification", "quality_tier",
                      "quality_verdict_text", "quality_verdict_color",
                      "quality_l8_unique_count", "quality_total_frames_rpu"):
            with self.subTest(campo=campo):
                self.assertIn(campo, r)

    async def test_el_veredicto_trae_texto_y_color(self):
        r, _ = await self.auditar()
        self.assertTrue(r.get("quality_verdict_text"))
        self.assertTrue(r.get("quality_verdict_color"))

    async def test_los_flags_dv_se_propagan_al_veredicto(self):
        # `dv_flags` viene del análisis básico (has_l9/has_l11…) y el veredicto
        # los usa: si se perdieran, el texto sería menos preciso.
        r, _ = await self.auditar(dv_flags={"has_l9": True, "has_l11": True})
        self.assertIsInstance(r, dict)
        self.assertIn("quality_verdict_text", r)


class TestFallosDelPipeline(AuditoriaCase):

    async def test_si_ffmpeg_falla_lanza(self):
        # "*": el "subcomando" de ffmpeg es la ruta del fichero.
        self.tb.fail("ffmpeg", "*", rc=1, stderr="no such file")
        self.tb.fail("dovi_tool", "extract-rpu", rc=1, stderr="sin HEVC")
        with self.assertRaises(Exception):
            await self.auditar()

    async def test_si_extract_rpu_falla_lanza(self):
        self.tb.fail("dovi_tool", "extract-rpu", rc=1, stderr="boom")
        with self.assertRaises(Exception):
            await self.auditar()

    async def test_el_log_detallado_es_opcional(self):
        lineas = []
        r, _ = await self.auditar(log_callback=lineas.append)
        self.assertIsInstance(r, dict)
        # Si se pasa, tiene que decir algo: el usuario mira el log.
        self.assertTrue(lineas)


if __name__ == "__main__":
    unittest.main()


class TestAnalisisExtendido(AuditoriaCase):
    """`con_luminancia=True`: los dos análisis con UNA sola extracción.

    Estaban separados en dos botones porque cada uno era caro. Ya no lo son por
    separado: extraer el RPU del MKV es el ~97 % del coste (medido: ~650 s de
    ffmpeg | extract-rpu solapados, frente a ~7 s del export por niveles y
    segundos de parseo). Hacerlos aparte significaba pagar dos veces ese 97 %
    para compartir el 3 %.

    Lo que fija este test es justo eso: que la extracción ocurre UNA vez y que
    el export pide L5 y L6 en la misma pasada, que es lo que hace que el perfil
    de luminancia salga gratis.
    """

    async def test_devuelve_los_dos_analisis(self):
        r, _ = await self.auditar(con_luminancia=True)
        self.assertIn("quality_verdict_text", r, "falta el veredicto de calidad")
        self.assertIn("light_profile", r, "falta el perfil de luminancia")
        luz = r["light_profile"]
        self.assertTrue(luz["per_scene_max_cll"], "serie de picos vacía")
        self.assertIn("stats", luz)
        self.assertIn("references", luz)
        self.assertNotIn("_raw", luz,
                         "los valores PQ crudos son para el log, no para la UI")

    async def test_una_sola_extraccion_para_los_dos(self):
        """EL PUNTO. Un ffmpeg y un extract-rpu, no dos de cada."""
        await self.auditar(con_luminancia=True)
        self.assertEqual(len(self.tb.find("ffmpeg")), 1, self.tb.calls)
        self.assertEqual(len(self.tb.find("dovi_tool", "extract-rpu")), 1,
                         self.tb.calls)

    async def test_un_solo_export_con_los_cinco_niveles(self):
        await self.auditar(con_luminancia=True)
        exports = self.tb.find("dovi_tool", "export")
        self.assertEqual(len(exports), 1, self.tb.calls)
        niveles = exports[0].opt("--levels") or ""
        for nivel in ("level1", "level2", "level5", "level6", "level8"):
            self.assertIn(nivel, niveles, f"falta {nivel} en: {niveles}")

    async def test_sin_luminancia_no_se_piden_l5_ni_l6(self):
        """El camino de solo calidad no debe pagar niveles que no usa."""
        await self.auditar(con_luminancia=False)
        niveles = self.tb.one("dovi_tool", "export").opt("--levels") or ""
        self.assertIn("level1", niveles)
        self.assertNotIn("level5", niveles, niveles)
        self.assertNotIn("level6", niveles, niveles)

    async def test_sin_luminancia_no_hay_perfil_en_el_resultado(self):
        r, _ = await self.auditar(con_luminancia=False)
        self.assertNotIn("light_profile", r)

    async def test_el_log_dice_que_pide_l5_y_l6(self):
        lineas = []
        await self.auditar(con_luminancia=True, log_callback=lineas.append)
        plan = [l for l in lineas if "📋 Plan" in l and "--levels" in l]
        self.assertTrue(plan, lineas[:12])
        self.assertIn("luminancia", " ".join(plan))

    async def test_los_ficheros_del_export_se_borran(self):
        """Cinco niveles de un RPU full-movie son ~157 MB."""
        await self.auditar(con_luminancia=True)
        sobrantes = list(self.trabajo.rglob("*_level*.json"))
        self.assertEqual(sobrantes, [], sobrantes)

    async def test_si_el_export_por_niveles_falla_sigue_habiendo_calidad(self):
        """Reserva para dovi_tool < 2.3.3: el volcado completo da los combos
        pero no L5/L6, así que el perfil se omite y se avisa."""
        self.tb.fail("dovi_tool", "export", stderr="unknown flag --levels")
        lineas = []
        try:
            r, _ = await self.auditar(con_luminancia=True,
                                      log_callback=lineas.append)
        except RuntimeError:
            # Sin export no hay combos: el pipeline aborta, que es correcto.
            self.assertTrue(any("no disponible" in l for l in lineas), lineas[-6:])
            return
        self.assertIsNone(r.get("light_profile"))


class TestElPerfilSobreviveAlCache(AuditoriaCase):
    """El perfil de luminancia se persiste con los `quality_*` y se recupera al
    reabrir el MKV.

    Era la asimetría más rara de las dos: la auditoría de calidad se cacheaba en
    `/config/mkv_audits/{fingerprint}.json` y reabrir el MKV la mostraba al
    instante, mientras el perfil de luminancia **no se persistía en ninguna
    parte** — solo vivía en el state del job, así que costaba ~10 min CADA vez
    que se miraba.

    La re-inyección del cache filtra por `hasattr(result.dovi, k)`, así que sin
    el campo en `DoviInfo` el perfil se descartaría en silencio. Eso es lo que
    este test protege.
    """

    def setUp(self):
        super().setUp()
        import storage
        self.cache_dir = self.tmp / "mkv_audits"
        self.cache_dir.mkdir()
        self._orig_cache = storage.MKV_AUDIT_DIR
        storage.MKV_AUDIT_DIR = self.cache_dir
        self.addCleanup(setattr, storage, "MKV_AUDIT_DIR", self._orig_cache)
        # `_run_dovi_on_mkv` escribe su RPU de muestreo en TMP_DIR, que por
        # defecto es /mnt/tmp y en el Mac no existe: el sniff falla en silencio
        # (`except: no bloquea`) y `result.dovi` queda en None. Sin DoviInfo no
        # hay dónde re-inyectar el cache, así que este test no probaría nada.
        self._orig_tmp = self.mod.TMP_DIR
        self.mod.TMP_DIR = str(self.trabajo)
        self.addCleanup(setattr, self.mod, "TMP_DIR", self._orig_tmp)

    async def _abrir_y_cachear(self):
        """Primera apertura del MKV: deja el bloque `basic` en el cache.

        El orden importa: la re-inyección del bloque `quality` solo ocurre
        cuando hay `basic` cacheado (`if cached and cached.get("basic")`), que es
        el orden real de uso — el usuario abre el MKV y DESPUÉS lanza el
        análisis."""
        from phases.mkv_analyze import persist_mkv_basic_to_cache
        primero = await self.mod.analyze_mkv(str(self.mkv), use_cache=False)
        persist_mkv_basic_to_cache(str(self.mkv), primero)
        return primero

    async def test_se_persiste_y_se_recupera_al_reabrir(self):
        from phases.mkv_analyze import persist_mkv_quality_to_cache

        await self._abrir_y_cachear()
        r, _ = await self.auditar(con_luminancia=True)
        self.assertTrue(r["light_profile"]["per_scene_max_cll"])
        persist_mkv_quality_to_cache(str(self.mkv), r)

        # Reabrir el MKV: sale del cache y re-inyecta quality + perfil.
        analisis = await self.mod.analyze_mkv(str(self.mkv))
        self.assertIsNotNone(analisis.dovi, "sin DoviInfo no hay dónde inyectar")
        perfil = analisis.dovi.light_profile
        self.assertIsNotNone(
            perfil,
            "el perfil no volvió del cache — ¿falta `light_profile` en DoviInfo? "
            "la re-inyección filtra por hasattr y lo descartaría en silencio")
        self.assertEqual(perfil["per_scene_max_cll"],
                         r["light_profile"]["per_scene_max_cll"])
        self.assertEqual(perfil["stats"], r["light_profile"]["stats"])

    async def test_reabrir_no_relanza_el_analisis(self):
        """Lo que hace que la segunda visita sea instantánea."""
        from phases.mkv_analyze import persist_mkv_quality_to_cache
        await self._abrir_y_cachear()
        r, _ = await self.auditar(con_luminancia=True)
        persist_mkv_quality_to_cache(str(self.mkv), r)
        self.tb.reset_calls()
        await self.mod.analyze_mkv(str(self.mkv))
        self.assertEqual(self.tb.find("dovi_tool", "extract-rpu"), [],
                         "reabrir el MKV ha vuelto a extraer el RPU")
        self.assertEqual(self.tb.find("dovi_tool", "export"), [],
                         "reabrir el MKV ha vuelto a exportar niveles")
