"""Cancelar hace lo que la interfaz anuncia, y el resultado de un trabajo no
se pierde porque otro ocupe el progreso.

Cuatro contratos del backend que salieron de la auditoría completa con Codex
del 2026-10-17, y que la interfaz da por buenos:

  · **Cancelar una fase CMv4.0 apaga el auto-pipeline en el servidor.** La
    interfaz lo apagaba solo en su copia y avisaba «auto-avance desactivado»:
    relanzar una fase a mano encadenaba el resto, y al reabrir el proyecto
    volvía a salir «Auto ON».
  · **Cancelar una copia desde biblioteca EN COLA la saca de la cola.** Antes
    contestaba «no está copiando» con un 200, la interfaz lo daba por
    cancelado y la copia corría después.
  · **El resultado de una creación de serie y de un análisis extendido se
    guarda por trabajo.** El progreso es un singleton del trabajo que corre:
    con dos seguidos en la cola, el modal del primero leía ya el segundo y el
    suyo se perdía — y en el de series, hacía poll cada 700 ms para siempre.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_contratos_de_cancelar_y_resultados -v
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402


class TestCancelarApagaElAuto(ApiTestCase):

    def test_en_disco_y_en_la_sesion_viva(self):
        sid = self.crear_sesion(phase="source_analyzed", auto_pipeline=True)
        r = self.cmv40
        # La fase que corre tiene SU objeto en la mano y lo guarda al terminar.
        viva = self.leer_sesion(sid)
        r._cmv40_marcar_activa(viva, "extract")
        self.addCleanup(r._cmv40_marcar_libre, viva)
        resp = self.client.post(f"/api/cmv40/{sid}/cancel")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(self.leer_sesion(sid).auto_pipeline, "el disco sigue en auto")
        self.assertFalse(viva.auto_pipeline,
                         "la fase cancelada volvería a guardar el auto encendido")

    def test_sin_fase_en_marcha_tambien(self):
        sid = self.crear_sesion(phase="source_analyzed", auto_pipeline=True)
        self.assertEqual(self.client.post(f"/api/cmv40/{sid}/cancel").status_code, 200)
        self.assertFalse(self.leer_sesion(sid).auto_pipeline)


class TestCancelarUnaCopiaEnCola(ApiTestCase):

    def setUp(self):
        super().setUp()
        from routers import tab2
        import queue_manager as qm
        import workload
        self.tab2, self.qm = tab2, qm
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        self.src = self.library_dir / "Peli (2024).mkv"
        self.src.write_bytes(b"\x1aE\xdf\xa3" + b"\0" * 4096)
        r = self.client.post("/api/mkv/apply", json={
            "file_path": str(self.src), "copy_to_output": True,
            "audio_tracks": [], "subtitle_tracks": []})
        self.assertEqual(r.status_code, 200, r.text)
        [(tipo, clave, datos, _)] = [t for t in self.trabajos_encolados
                                     if t[0] == qm.TIPO_COPIA_BIBLIOTECA]
        self.cola = self.main.queue_manager
        self.cola._queue.append(qm.TrabajoEnCola(tab="mkv", tipo=tipo, clave=clave,
                                                 datos=datos))
        self.addCleanup(self.cola._queue.clear)

    def test_la_saca_de_la_cola_y_lo_dice(self):
        self.assertEqual(asyncio.run(self.tab2.mkv_apply_cancel()),
                         {"ok": True, "descartada": True})
        self.assertEqual(list(self.cola._queue), [], "sigue en la cola: correría después")
        st = self.tab2._mkv_apply_state
        self.assertEqual((st["step"], st["active"]), ("cancelled", False))
        self.assertFalse((self.output_dir / self.src.name).exists())

    def test_si_ya_no_estaba_en_la_cola_no_finge(self):
        self.cola._queue.clear()
        self.assertEqual(asyncio.run(self.tab2.mkv_apply_cancel()),
                         {"ok": False, "reason": "not_in_queue"})
        self.assertTrue(self.tab2._mkv_apply_state["active"])


class TestElResultadoDeUnaSeriePorTrabajo(ApiTestCase, unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        import workload
        from routers import tab1
        self.tab1 = tab1
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        self.addCleanup(tab1._series_resultados.clear)
        tab1._series_resultados.clear()

    def _cuerpo(self, temporada, carpeta="Serie (2024)"):
        return self.tab1.CreateSeriesSessionsRequest(
            source_type="bdmv_folder", source_path=carpeta, series_name="Serie",
            series_year=2024, season_number=temporada,
            episodes=[{"mpls_path": "00800.mpls", "episode_number": 1,
                       "episode_title": "Ep 1"}])

    async def _crear(self, temporada, carpeta):
        """El runner de verdad. Sin carpeta BDMV, `Source.open` falla: es el
        fallo entero de un trabajo, el caso que dejaba al modal sin saberlo."""
        cuerpo = self._cuerpo(temporada, carpeta)
        await self.tab1._ejecutar_creacion_de_serie(
            cuerpo, "bdmv_folder", carpeta, str(self.isos_dir / carpeta),
            cuerpo.episodes, [], [], "huella")
        return self.tab1._clave_de_serie(carpeta, temporada)

    def _progreso(self):
        r = self.client.get("/api/series-create-progress")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def test_un_fallo_se_lee_aunque_otro_trabajo_ocupe_el_progreso(self):
        job_a = await self._crear(1, "No existe A")
        # El segundo disco empieza: ocupa el singleton.
        self.tab1._series_create_progress.update({"job": "otro", "running": True,
                                                  "resultado": None})
        p = self._progreso()
        self.assertEqual(p["job"], "otro")
        self.assertIn(job_a, p["resultados_por_job"])
        self.assertEqual(p["resultados_por_job"][job_a]["created"], [])
        self.assertTrue(p["resultados_por_job"][job_a]["error"])

    async def test_un_fallo_no_deja_el_hueco_ni_el_historial_sin_apuntar(self):
        import historial
        import workload
        job = await self._crear(1, "No existe B")
        self.assertIsNone(workload.bloqueado_por(), "el hueco del trabajo sigue ocupado")
        lineas = [h for h in historial.leer(10) if h.get("id") == job]
        self.assertEqual([h["estado"] for h in lineas], ["error"])

    async def test_un_trabajo_bueno_tambien(self):
        from test_serie_auto_ejecuta import _bdinfo
        carpeta = self.isos_dir / "Serie (2024)"
        for sub in ("PLAYLIST", "STREAM"):
            (carpeta / "BDMV" / sub).mkdir(parents=True, exist_ok=True)
        (carpeta / "BDMV" / "PLAYLIST" / "00800.mpls").write_bytes(b"\0" * 64)
        (carpeta / "BDMV" / "STREAM" / "00800.m2ts").write_bytes(b"\0" * 4096)
        with patch("phases.phase_a.run_full_analysis_for_mpls", return_value=(_bdinfo(), [])):
            job = await self._crear(2, "Serie (2024)")
        self.tab1._series_create_progress.update({"job": "otro", "running": True})
        guardado = self._progreso()["resultados_por_job"][job]
        self.assertEqual(len(guardado["created"]), 1)
        self.assertNotIn("error", guardado)

    async def test_la_misma_temporada_otra_vez_no_ve_el_resultado_de_la_anterior(self):
        """La clave de una serie es el disco y la temporada: volver a crearla
        repite la clave, y el modal nuevo tomaría el resultado viejo por el
        suyo mientras el trabajo nuevo todavía corre."""
        job = await self._crear(1, "No existe C")
        self.tab1._series_create_progress.update({"job": job, "running": True})
        self.assertNotIn(job, self._progreso()["resultados_por_job"])
        self.tab1._series_create_progress["running"] = False
        self.assertIn(job, self._progreso()["resultados_por_job"])

    async def test_saltar_todos_los_existentes_tambien_publica_el_resultado(self):
        """El runner ignora lo que devuelve la función: sin resultado en el
        progreso, el modal lo leía como un fallo."""
        cuerpo = self._cuerpo(3)
        saltados = [{"season_number": 3, "episode_number": 1, "existing_id": "x"}]
        await self.tab1._ejecutar_creacion_de_serie(
            cuerpo, "bdmv_folder", "Serie (2024)", str(self.isos_dir / "Serie (2024)"),
            [], saltados, [], "huella")
        p = self._progreso()
        self.assertFalse(p["running"])
        self.assertEqual(p["resultado"]["skipped_existing"], saltados)
        self.assertIn(self.tab1._clave_de_serie("Serie (2024)", 3), p["resultados_por_job"])

    def test_se_guardan_los_ultimos_y_el_repetido_cuenta_como_reciente(self):
        g = self.tab1._guardar_resultado_de_serie
        for j in ("a", "b", "c"):
            g(j, {"created": [j]})
        g("a", {"created": ["a2"]})
        g("d", {"created": ["d"]})
        self.assertEqual(list(self.tab1._series_resultados), ["c", "a", "d"])


class TestElResultadoDeUnaAuditoriaPorId(ApiTestCase):

    def setUp(self):
        super().setUp()
        from routers import tab2
        self.tab2 = tab2
        tab2._mkv_quality_resultados.clear()
        self.addCleanup(tab2._mkv_quality_resultados.clear)

    def _progreso(self):
        r = self.client.get("/api/mkv/quality-audit/progress")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_un_fallo_de_a_se_lee_con_b_en_marcha(self):
        t = self.tab2
        t._mkv_quality_reset(file_name="A.mkv", audit_id="aaa")
        t._mkv_quality_state_finalize_if("aaa", "se rompió", step="error")
        t._mkv_quality_reset(file_name="B.mkv", audit_id="bbb")
        p = self._progreso()
        self.assertEqual(p["audit_id"], "bbb")
        self.assertEqual(p["resultados_por_audit"]["aaa"],
                         {"step": "error", "result": None, "error": "se rompió"})
        self.assertNotIn("bbb", p["resultados_por_audit"], "B no ha terminado")

    def test_el_resultado_bueno_de_a_tambien(self):
        """Por el runner de verdad, con el análisis sustituido: el resultado
        se guarda por su id aunque el estado ya sea de otro."""
        t = self.tab2
        mkv = self.output_dir / "A.mkv"
        mkv.write_bytes(b"\x1aE\xdf\xa3" + b"\0" * 64)

        async def _analisis(*a, **k):
            # Mientras A termina, B ya ha reclamado el estado.
            t._mkv_quality_reset(file_name="B.mkv", audit_id="bbb")
            return {"l8_classification": "real"}
        from datetime import datetime, timezone
        from phases import mkv_analyze
        with patch.object(mkv_analyze, "analyze_rpu_quality_for_mkv", _analisis), \
                patch.object(mkv_analyze, "persist_mkv_quality_to_cache",
                             lambda *a, **k: None):
            asyncio.run(t._ejecutar_analisis_extendido(
                "aaa", str(mkv), mkv, datetime.now(timezone.utc)))
        p = self._progreso()
        self.assertEqual(p["resultados_por_audit"]["aaa"]["step"], "done")
        self.assertEqual(p["resultados_por_audit"]["aaa"]["result"],
                         {"l8_classification": "real"})
        self.assertIsNone(p.get("result"), "el resultado de A ha pisado el estado de B")


if __name__ == "__main__":
    unittest.main()
