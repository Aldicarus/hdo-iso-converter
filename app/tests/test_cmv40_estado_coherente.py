"""Lo que un proyecto CMv4.0 cuenta de sí mismo cuadra con lo que es.

Fallos del router, la persistencia y el historial que encontró la auditoría
completa con Codex del 2026-10-17 (paquete P2), reproducidos antes de
arreglarlos:

  · **«Cambiar target» conservaba la respuesta dada para el bin anterior**
    (mantener/inyectar) y su análisis L2/L3/L8: con el bin nuevo pidiendo
    decidir, el relato decía «decisión tomada» y el proyecto se quedaba sin
    botones.
  · **«Restaurar el sync» borraba la corrección ANTES de leer el original**:
    si `dovi_tool info` fallaba, el Δ se quedaba el de la corregida y la
    respuesta decía «restaurado»; y si fallaba la regeneración, el gráfico
    seguía describiendo la corrección borrada. Rebobinar a `extracted`
    también dejaba ese gráfico.
  · **El log tenía un hueco y no tenía reloj**: el lote salía del buffer
    antes de llegar al fichero (un GET en medio no lo veía), y una línea
    seguida de silencio se quedaba solo en RAM.
  · **El relato se componía con la clasificación L8 guardada** y el campo se
    servía re-derivado: dos veredictos opuestos en la misma respuesta.
  · **Una línea del historial cortada por un `kill -9` se tragaba la
    siguiente.**
  · **Las dos Fase B rápidas se registraban como trabajo diferido** y
    `bloqueado_por` las tomaba por un bloqueo.
  · **El porcentaje del job usaba los pesos del drop-in con revisión manual
    forzada**, y el modelo de ETA tenía su propia copia de la regla.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_estado_coherente -v
"""
import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
from cmv40_harness import FakeToolbox, RpuProps, write_artifacts  # noqa: E402


def _combo(delta: int, ocurrencias: int = 100):
    from models import L8Combo
    return L8Combo(target_display_index=1, trim_slope=2048 + delta,
                   trim_offset=2048, trim_power=2048, trim_chroma_weight=2048,
                   trim_saturation_gain=2048, ms_weight=0,
                   occurrence_count=ocurrencias)


class TestCambiarTargetOlvidaLoDelBinViejo(ApiTestCase):

    def _cambiar(self):
        sid = self.crear_sesion(
            phase="target_provided",
            output_mkv_name="Peli (2024) [CMv4 FULL].mkv",
            target_type="trusted_p7_fel_final",
            preflight_decision="ok",
            preflight_user_choice="inject",
            preflight_user_choice_at="2026-10-01T00:00:00+00:00",
            target_l8_combos=[_combo(600)], target_l8_unique_count=1,
            target_l8_max_delta=600, target_l8_classification="real",
            target_l8_quality_tier="full", target_l8_quality_label="CMv4 FULL",
            target_l3_unique_count=40, target_l3_frames=1000,
            target_l2_combos=[], target_l2_unique_count=3,
            target_trust_gates={"frames": {"ok": True, "severity": "ok"}},
            target_rpu_sha256="abc")
        r = self.client.post(f"/api/cmv40/{sid}/reset-to/source_analyzed")
        self.assertEqual(r.status_code, 200, r.text)
        return sid

    def test_la_respuesta_del_usuario_era_del_bin_viejo(self):
        s = self.leer_sesion(self._cambiar())
        self.assertEqual(s.preflight_user_choice, "")
        self.assertEqual(s.preflight_user_choice_at, "")

    def test_y_el_analisis_tambien(self):
        s = self.leer_sesion(self._cambiar())
        self.assertEqual(s.target_l8_combos, [])
        self.assertEqual(s.target_l8_classification, "")
        self.assertEqual(s.target_l8_quality_tier, "")
        self.assertEqual(s.target_l3_frames, 0)
        self.assertEqual(s.target_l2_unique_count, 0)
        self.assertEqual(s.target_trust_gates, {})
        self.assertEqual(s.target_rpu_sha256, "")

    def test_con_el_bin_nuevo_pidiendo_decidir_hay_botones(self):
        sid = self._cambiar()
        import storage
        s = self.leer_sesion(sid)
        s.preflight_decision = "keep_l8_default"     # el pre-flight del bin nuevo
        s.recommended_action = "keep"
        storage.save_cmv40_session(s)
        g = self.client.get(f"/api/cmv40/{sid}?include_log=false").json()
        self.assertEqual(g["relato"]["situacion"], "esperando_decision")
        self.assertEqual(g["relato"]["decision"]["estado"], "pendiente",
                         "decisión «tomada» con la respuesta dada al bin viejo")

    def test_el_tier_del_nombre_era_del_bin_viejo(self):
        s = self.leer_sesion(self._cambiar())
        self.assertEqual(s.output_mkv_name, "Peli (2024) [CMv4.0].mkv")

    def test_cada_campo_listado_existe_en_el_modelo(self):
        from models import CMv40Session
        for campo in self.cmv40._CMV40_CAMPOS_DEL_TARGET:
            self.assertIn(campo, CMv40Session.model_fields, campo)


class _ConToolbox(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tb = FakeToolbox(self.tmp / "tb")
        self.tb.install()
        self.addCleanup(self.tb.uninstall)


class TestRestaurarElSyncNoMiente(_ConToolbox):

    def _corregido(self):
        sid = self.crear_sesion(phase="extracted", source_frame_count=1000,
                                target_frame_count=1000, sync_delta=0,
                                sync_config={"steps": [{"remove": 40}]})
        wd = self.work_base / sid
        tgt = RpuProps(profile=7, el_type="FEL", cm_version="v4.0", frames=1040,
                       has_l8=True)
        src = RpuProps(profile=7, el_type="FEL", cm_version="v2.9", frames=1000)
        write_artifacts(wd, "RPU_target.bin", props=tgt)
        write_artifacts(wd, "RPU_source.bin", props=src)
        write_artifacts(wd, "RPU_synced.bin", props=RpuProps(
            profile=7, el_type="FEL", cm_version="v4.0", frames=1000, has_l8=True))
        self.tb.define_rpu("RPU_target.bin", **tgt.as_dict())
        self.tb.define_rpu("RPU_source.bin", **src.as_dict())
        (wd / "per_frame_data.json").write_text(json.dumps({"marca": "corregido"}))
        return sid, wd

    def test_si_no_puede_leer_el_original_no_borra_nada(self):
        sid, wd = self._corregido()
        self.tb.fail("dovi_tool", "info")
        r = self.client.post(f"/api/cmv40/{sid}/reset-sync")
        self.assertGreaterEqual(r.status_code, 400, r.text)
        self.assertTrue((wd / "RPU_synced.bin").exists(),
                        "borró la corrección sin saber a qué volver")
        s = self.leer_sesion(sid)
        self.assertTrue(s.sync_config)
        self.assertEqual(s.target_frame_count, 1000)

    def test_restaura_el_original(self):
        sid, wd = self._corregido()
        r = self.client.post(f"/api/cmv40/{sid}/reset-sync")
        self.assertEqual(r.status_code, 200, r.text)
        s = self.leer_sesion(sid)
        self.assertEqual(s.target_frame_count, 1040)
        self.assertEqual(s.sync_delta, 40)
        self.assertFalse((wd / "RPU_synced.bin").exists())

    def test_si_falla_la_regeneracion_no_queda_el_grafico_corregido(self):
        sid, wd = self._corregido()
        from phases import cmv40_pipeline as pipeline

        async def _falla(*a, **k):
            raise RuntimeError("export falló")
        orig = pipeline._generate_per_frame_data
        pipeline._generate_per_frame_data = _falla
        self.addCleanup(setattr, pipeline, "_generate_per_frame_data", orig)
        self.client.post(f"/api/cmv40/{sid}/reset-sync")
        pf = wd / "per_frame_data.json"
        self.assertFalse(pf.exists() and "corregido" in pf.read_text(),
                         "el gráfico sigue describiendo la corrección borrada")

    def test_rebobinar_a_extracted_tampoco_deja_el_grafico_corregido(self):
        sid, wd = self._corregido()
        r = self.client.post(f"/api/cmv40/{sid}/reset-to/extracted")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse((wd / "per_frame_data.json").exists())

    def test_sin_correccion_rebobinar_conserva_el_grafico(self):
        sid = self.crear_sesion(phase="sync_verified")
        wd = self.work_base / sid
        (wd / "per_frame_data.json").write_text("{}")
        r = self.client.post(f"/api/cmv40/{sid}/reset-to/extracted")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue((wd / "per_frame_data.json").exists())


class TestLaCacheDelGraficoMiraElDelta(ApiTestCase):

    def test_el_delta_es_parte_de_la_identidad(self):
        sid = self.crear_sesion(phase="extracted")
        pf = self.work_base / sid / "per_frame_data.json"
        pf.write_text(json.dumps({"source_frames": 1000, "target_frames": 1000,
                                  "data": [{"frame": i, "src_maxcll": 100 + i,
                                            "tgt_maxcll": 100 + i}
                                           for i in range(0, 1000, 20)]}))
        r = self.cmv40
        a = r._cmv40_pfd_cargar(sid, pf, 0)
        b = r._cmv40_pfd_cargar(sid, pf, 40)
        self.assertNotEqual(a["sync_gate"], b["sync_gate"],
                            "con otro Δ la caché devolvió el criterio del Δ viejo")


class TestElLogNoTieneHuecos(ApiTestCase):

    def test_un_get_durante_la_escritura_ve_cada_linea_una_vez(self):
        r = self.cmv40
        sid = self.crear_sesion()
        s = self.leer_sesion(sid)
        import storage
        orig = storage.append_cmv40_log
        dentro = threading.Event()
        visto = {}

        def _lenta(session_id, lineas):
            dentro.set()
            time.sleep(0.3)
            orig(session_id, lineas)
        storage.append_cmv40_log = _lenta
        self.addCleanup(setattr, storage, "append_cmv40_log", orig)

        def _leer_en_medio():
            dentro.wait(5)
            time.sleep(0.1)
            visto["log"] = r._cmv40_log_completo(s)

        async def main():
            for i in range(5):
                r._cmv40_log_buffer.setdefault(sid, []).append(f"[00:00:0{i}] línea {i}")
            hilo = threading.Thread(target=_leer_en_medio)
            hilo.start()
            await r._cmv40_log_volcar(sid, forzar=True)
            hilo.join(5)
        asyncio.run(main())
        lineas = [l for l in visto["log"] if "línea" in l]
        self.assertEqual(len(lineas), 5, f"el GET vio {len(lineas)} de 5: {lineas}")

    def test_una_linea_seguida_de_silencio_llega_al_fichero(self):
        r = self.cmv40
        sid = self.crear_sesion()
        s = self.leer_sesion(sid)
        from storage import read_cmv40_log

        async def main():
            r._cmv40_log_buffer_ts[sid] = time.monotonic()     # acaba de volcar
            await r._cmv40_log(s, "una línea y luego nada")
            await asyncio.sleep(r._CMV40_LOG_FLUSH_S + 0.5)
        asyncio.run(main())
        self.assertTrue(any("una línea y luego nada" in l for l in read_cmv40_log(sid)),
                        "la línea se quedó solo en memoria")


class TestElPreflightDejaSuLogEnElFichero(ApiTestCase):

    def test_el_veredicto_esta_en_disco_al_terminar(self):
        from phases import cmv40_pipeline as pipeline
        from storage import read_cmv40_log
        r = self.cmv40
        sid = self.crear_sesion(phase="created", pending_target_kind="path",
                                pending_target_rpu_path="/x.bin")

        async def _nada(*a, **k):
            return None

        async def _analizar(session, log_cb):
            return True
        for nombre in ("preflight_source", "preflight_target_path"):
            orig = getattr(pipeline, nombre)
            setattr(pipeline, nombre, _nada)
            self.addCleanup(setattr, pipeline, nombre, orig)
        orig = r._cmv40_preflight_analyze_target
        r._cmv40_preflight_analyze_target = _analizar
        self.addCleanup(setattr, r, "_cmv40_preflight_analyze_target", orig)
        pipeline.set_cancel_check(None)
        # `asyncio.run` cancela el volcado diferido al cerrar el bucle: lo que
        # esté en disco lo ha escrito el propio pre-flight al terminar.
        asyncio.run(r._cmv40_correr_preflight(
            self.leer_sesion(sid), r._get_cmv40_phase_lock(sid), "path",
            rpu_path="/x.bin"))
        self.assertTrue(any("✓ Fase" in l for l in read_cmv40_log(sid)),
                        "el cierre del pre-flight se quedó en el buffer")


class TestElRelatoUsaLaClasificacionQueSeSirve(ApiTestCase):

    def test_un_veredicto_viejo_no_contradice_al_servido(self):
        sid = self.crear_sesion(
            phase="target_provided", target_l8_combos=[_combo(600)],
            target_l8_unique_count=1, target_l8_classification="default")
        g = self.client.get(f"/api/cmv40/{sid}?include_log=false").json()
        self.assertEqual(g["target_l8_classification"], "real")
        colorista = [h for h in g["relato"]["hechos"] if h["id"] == "bin_colorista"]
        self.assertEqual(colorista[0]["estado"], "ok",
                         "el relato explica el veredicto viejo junto al nuevo")


class TestUnaLineaCortadaNoSeTragaLaSiguiente(ApiTestCase):

    def test_kill_9_a_mitad_de_linea(self):
        import historial
        f = historial.ruta()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('{"id": "viejo", "estado": "done"}\n{"id": "cortad', encoding="utf-8")
        from datetime import datetime, timezone
        historial.anotar(id="nuevo", tab="cmv40", tipo="fase_cmv40", que="x",
                         inicio=datetime.now(timezone.utc))
        ids = [r["id"] for r in historial.buscar(limite=10)[0]]
        self.assertIn("nuevo", ids, "la línea nueva se pegó a la cortada")
        self.assertIn("viejo", ids)


class TestLasFaseBRapidasNoBloquean(ApiTestCase):

    def test_target_rpu_path_se_registra_interactiva(self):
        import workload
        self.ejecutar_fases_de_verdad()
        sid = self.crear_sesion(phase="source_analyzed")
        r = self.cmv40
        clases = {}

        async def coro(log_cb, proc_cb):
            t = [x for x in workload.en_curso() if x.clave == sid]
            clases["clase"] = t[0].clase if t else None
            clases["bloquea"] = workload.bloqueado_por() is not None
        asyncio.run(r._run_cmv40_phase(self.leer_sesion(sid), "target_rpu_path",
                                       coro, "target_provided"))
        self.assertEqual(clases["clase"], workload.CLASE_INTERACTIVO)
        self.assertFalse(clases["bloquea"])


class TestLaRutaDelPorcentajeSaleDeLaMatriz(ApiTestCase):

    def test_fel_trusted_con_revision_manual_suma_como_merge(self):
        from models import CMv40Session
        base = dict(id="x", source_mkv_path="/a.mkv", source_mkv_name="a.mkv",
                    phase="extracted", source_workflow="p7_fel",
                    target_type="trusted_p7_fel_final", target_trust_ok=True,
                    target_trust_gates={"frames": {"ok": True, "severity": "ok"}},
                    running_phase="inject")
        auto = CMv40Session(**base)
        manual = CMv40Session(**base, trust_override="force_interactive")
        r = self.cmv40
        self.assertNotEqual(r._cmv40_job_pct(auto, 50), r._cmv40_job_pct(manual, 50))
        self.assertEqual(r._cmv40_job_pct(manual, 50),
                         r._cmv40_job_pct(CMv40Session(**{**base, "target_type": "generic"}), 50))

    def test_el_modelo_de_eta_cuenta_lo_que_el_job_hizo(self):
        from datetime import datetime, timezone
        from models import CMv40PhaseRecord
        hist = [CMv40PhaseRecord(phase=f, status="done", elapsed_seconds=s,
                                 started_at=datetime.now(timezone.utc))
                for f, s in (("analyze_source", 600), ("inject", 600))]
        for i in range(3):
            # Campos de drop-in, pero el job acabó por merge (revisión manual
            # forzada después de la Fase B, por ejemplo).
            self.crear_sesion(f"cmv40_M_2024_{i}", phase="done",
                              source_workflow="p7_fel",
                              target_type="trusted_p7_fel_final",
                              target_trust_ok=True,
                              output_workflow="restore_merge",
                              phase_history=hist)
        m = self.cmv40._cmv40_build_eta_model()
        self.assertIn("inject", m["merge"])
        self.assertNotIn("inject", m["dropin"])


if __name__ == "__main__":
    unittest.main()
