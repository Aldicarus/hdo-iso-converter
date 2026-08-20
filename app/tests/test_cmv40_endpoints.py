"""
Contrato HTTP de los endpoints CMv4.0.

Ninguno tenía prueba de request/response: nadie comprobaba qué status
devuelven, qué guards aplican ni qué queda persistido. Sin eso, moverlos de
`main.py` a un router es a ciegas — y `main.py` tiene 14 copias literales
del mismo boilerplate esperando a que alguien las unifique.

Lo que se fija aquí es el contrato observable desde fuera, no la
implementación: rutas, códigos y efecto sobre la sesión. Así el refactor
puede cambiar dónde vive el código sin cambiar lo que la UI recibe.

Los endpoints que arrancan una fase se prueban solo por sus guards (el
camino en el que devuelven sin lanzar nada). El comportamiento de las fases
en sí vive en `test_cmv40_fase_f_matriz` y `test_cmv40_fases_cgh`.
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402

# Los nueve que arrancan una fase, con el body que exige cada uno. FastAPI
# valida el body ANTES de entrar al handler: sin él la respuesta es un 422 y
# nunca se llega al 404 ni al guard de 409, así que los tests lo mandan.
ENDPOINTS_DE_FASE = {
    "analyze-source": None,
    "target-rpu-path": {"rpu_path": "/mnt/cmv40_rpus/x.bin"},
    "target-rpu-from-drive": {"file_id": "abc123", "file_name": "x.bin"},
    "target-rpu-from-mkv": {"source_mkv_path": "/mnt/library/otro.mkv"},
    "extract": None,
    "apply-sync": {"editor_config": {"remove": ["0-12"]}},
    "inject": None,
    "remux": None,
    "validate": None,
}


def _post(client, url, body):
    return client.post(url, json=body) if body is not None else client.post(url)


class TestRutasRegistradas(ApiTestCase):
    """Un refactor que mueva endpoints a un router no debe perder ninguno
    ni cambiar su path: la UI llama a estas URLs literales."""

    def rutas(self):
        return {
            (r.path, m)
            for r in self.main.app.routes
            for m in getattr(r, "methods", None) or {"WS"}
        }

    def test_estan_los_endpoints_de_fase(self):
        rutas = self.rutas()
        for nombre in ENDPOINTS_DE_FASE:
            with self.subTest(endpoint=nombre):
                self.assertIn((f"/api/cmv40/{{session_id}}/{nombre}", "POST"), rutas)

    def test_estan_los_endpoints_de_gestion(self):
        rutas = self.rutas()
        esperados = [
            ("/api/cmv40", "GET"), ("/api/cmv40/create", "POST"),
            ("/api/cmv40-active", "GET"), ("/api/cmv40/eta-model", "GET"),
            ("/api/cmv40/{session_id}", "GET"),
            ("/api/cmv40/{session_id}", "DELETE"),
            ("/api/cmv40/{session_id}/cancel", "POST"),
            ("/api/cmv40/{session_id}/clear-error", "POST"),
            ("/api/cmv40/{session_id}/cleanup", "POST"),
            ("/api/cmv40/{session_id}/rename-output", "POST"),
            ("/api/cmv40/{session_id}/mark-synced", "POST"),
            ("/api/cmv40/{session_id}/reset-sync", "POST"),
            ("/api/cmv40/{session_id}/sync-data", "GET"),
            ("/api/cmv40/{session_id}/auto-pipeline", "POST"),
            ("/api/cmv40/{session_id}/acknowledge-critical-gates", "POST"),
            ("/api/cmv40/{session_id}/reset-to/{target_phase}", "POST"),
        ]
        for path, metodo in esperados:
            with self.subTest(ruta=f"{metodo} {path}"):
                self.assertIn((path, metodo), rutas)

    def test_esta_el_websocket_del_log(self):
        paths = {r.path for r in self.main.app.routes}
        self.assertIn("/ws/cmv40/{session_id}", paths)


class TestSesionInexistente(ApiTestCase):
    """404 uniforme: hay 27 copias de este guard y el refactor las unifica."""

    def test_todos_los_endpoints_de_fase_dan_404(self):
        for nombre, body in ENDPOINTS_DE_FASE.items():
            with self.subTest(endpoint=nombre):
                r = _post(self.client, f"/api/cmv40/no_existe/{nombre}", body)
                self.assertEqual(r.status_code, 404)
                self.assertIn("no encontrado", r.json()["detail"].lower())

    def test_lectura_de_sesion_inexistente(self):
        self.assertEqual(self.client.get("/api/cmv40/no_existe").status_code, 404)

    def test_acciones_de_gestion_dan_404(self):
        for path in ("clear-error", "cleanup", "mark-synced", "reset-sync"):
            with self.subTest(endpoint=path):
                r = self.client.post(f"/api/cmv40/no_existe/{path}")
                self.assertEqual(r.status_code, 404)

    def test_cancelar_es_tolerante_a_proposito(self):
        # `cancel` NO da 404: libera flags y procesos aunque la sesión ya no
        # esté en disco. El comentario del endpoint lo justifica — "mejor
        # sesión liberada con proceso zombi que UI bloqueada esperando".
        r = self.client.post("/api/cmv40/no_existe/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])


class TestGuardDeErrorPendiente(ApiTestCase):
    """Un error sin descartar bloquea con 409 los nueve endpoints de fase.

    Existe porque el auto-pipeline tiene dos disparadores y el del frontend
    decide sobre un snapshot que puede estar viejo: sin este guard, Fase H
    llegó a ejecutarse dos veces con 1,2 s de diferencia (5-8 min de
    extract-rpu repetidos en la rama merge).
    """

    def test_los_nueve_endpoints_de_fase_lo_aplican(self):
        for nombre, body in ENDPOINTS_DE_FASE.items():
            with self.subTest(endpoint=nombre):
                sid = self.crear_sesion(
                    sid=f"cmv40_err_{nombre.replace('-', '_')}",
                    phase="injected", error_message="Ya existe un MKV con ese nombre")
                self.fases_lanzadas.clear()
                r = _post(self.client, f"/api/cmv40/{sid}/{nombre}", body)
                self.assertEqual(r.status_code, 409, f"{nombre} no aplica el guard")
                self.assertIn("Ya existe un MKV", r.json()["detail"])
                # El guard tiene que ir ANTES de lanzar: rechazar después de
                # haber arrancado la fase no serviría de nada.
                self.assertEqual(self.fases_lanzadas, [],
                                 f"{nombre} lanzó la fase pese a devolver 409")

    def test_clear_error_desbloquea(self):
        sid = self.crear_sesion(phase="injected", error_message="algo falló")
        self.assertEqual(self.client.post(f"/api/cmv40/{sid}/remux").status_code, 409)
        r = self.client.post(f"/api/cmv40/{sid}/clear-error")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.leer_sesion(sid).error_message, "")

    def test_sin_error_el_guard_deja_pasar(self):
        # Sin error, remux llega a su propia validación (falta el HEVC), que
        # es otro camino: lo que importa es que no lo pare el guard de 409.
        sid = self.crear_sesion(phase="injected")
        r = self.client.post(f"/api/cmv40/{sid}/remux")
        self.assertNotEqual(r.status_code, 409)


class TestProyectoArchivado(ApiTestCase):
    """Tras el cleanup el proyecto es de solo lectura."""

    def test_no_se_puede_renombrar_la_salida(self):
        sid = self.crear_sesion(archived=True)
        r = self.client.post(f"/api/cmv40/{sid}/rename-output",
                             json={"output_mkv_name": "Otro.mkv"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("archivado", r.json()["detail"].lower())

    def test_un_proyecto_completado_tampoco_se_renombra(self):
        sid = self.crear_sesion(phase="done")
        r = self.client.post(f"/api/cmv40/{sid}/rename-output",
                             json={"output_mkv_name": "Otro.mkv"})
        self.assertEqual(r.status_code, 400)

    def test_se_puede_seguir_leyendo(self):
        sid = self.crear_sesion(archived=True)
        self.assertEqual(self.client.get(f"/api/cmv40/{sid}").status_code, 200)


class TestMutacionesSimples(ApiTestCase):
    """Endpoints que solo tocan la sesión: el contrato es lo que persiste."""

    def test_renombrar_la_salida_persiste(self):
        sid = self.crear_sesion()
        r = self.client.post(f"/api/cmv40/{sid}/rename-output",
                             json={"output_mkv_name": "Peli (2024) [CMv4 CORE].mkv"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.leer_sesion(sid).output_mkv_name,
                         "Peli (2024) [CMv4 CORE].mkv")

    def test_auto_pipeline_se_puede_apagar_y_encender(self):
        sid = self.crear_sesion(auto_pipeline=True)
        r = self.client.post(f"/api/cmv40/{sid}/auto-pipeline", json={"enabled": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.leer_sesion(sid).auto_pipeline)
        self.client.post(f"/api/cmv40/{sid}/auto-pipeline", json={"enabled": True})
        self.assertTrue(self.leer_sesion(sid).auto_pipeline)

    def test_mark_synced_avanza_la_fase(self):
        sid = self.crear_sesion(phase="extracted")
        r = self.client.post(f"/api/cmv40/{sid}/mark-synced")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.leer_sesion(sid).phase, "sync_verified")

    def test_borrar_un_proyecto(self):
        sid = self.crear_sesion()
        self.assertEqual(self.client.delete(f"/api/cmv40/{sid}").status_code, 200)
        self.assertIsNone(self.leer_sesion(sid))
        self.assertEqual(self.client.get(f"/api/cmv40/{sid}").status_code, 404)


class TestConfirmacionDeGatesDegradados(ApiTestCase):
    """El pause-point del ACK: es el que se comió el clic del usuario por
    culpa del overlay (ver test_cmv40_overlay_bloqueante)."""

    def sesion_con_ack_pendiente(self, **extra):
        return self.crear_sesion(
            phase="target_provided",
            awaiting_critical_ack=True,
            auto_pipeline=False,   # que el ACK no encadene una fase real
            critical_gate_failures=[{
                "gate": "l6_div", "severity": "ack_required",
                "why": "MaxCLL estático diverge 416 nits (umbral 200).",
            }],
            **extra)

    def test_confirmar_libera_el_bloqueo(self):
        sid = self.sesion_con_ack_pendiente()
        r = self.client.post(f"/api/cmv40/{sid}/acknowledge-critical-gates")
        self.assertEqual(r.status_code, 200)
        s = self.leer_sesion(sid)
        self.assertFalse(s.awaiting_critical_ack)
        self.assertTrue(s.user_acknowledged_degradation)

    def test_confirmar_marca_que_fase_d_se_salta(self):
        # Fase D no puede arreglar una divergencia de grading, así que tras
        # el ACK no tiene sentido parar ahí.
        sid = self.sesion_con_ack_pendiente()
        self.client.post(f"/api/cmv40/{sid}/acknowledge-critical-gates")
        self.assertIn("sync_verification_pause", self.leer_sesion(sid).phases_skipped)

    def test_conserva_el_motivo_como_historico(self):
        sid = self.sesion_con_ack_pendiente()
        self.client.post(f"/api/cmv40/{sid}/acknowledge-critical-gates")
        fallos = self.leer_sesion(sid).critical_gate_failures
        self.assertEqual(len(fallos), 1)
        self.assertEqual(fallos[0]["gate"], "l6_div")

    def test_con_revision_forzada_el_ack_no_marca_la_fase_omitida(self):
        # El ACK dice "acepto que el grading diverge", no "renuncio a revisar
        # el sync". Antes se marcaba `sync_verification_pause` siempre, así
        # que la UI pintaba Fase D omitida y el pipeline se paraba ahí.
        sid = self.sesion_con_ack_pendiente(trust_override="force_interactive")
        r = self.client.post(f"/api/cmv40/{sid}/acknowledge-critical-gates")
        self.assertEqual(r.status_code, 200)
        s = self.leer_sesion(sid)
        self.assertTrue(s.user_acknowledged_degradation)
        self.assertNotIn("sync_verification_pause", s.phases_skipped,
                         "con force_interactive la Fase D NO se omite")

    def test_el_plan_refleja_el_ack(self):
        sid = self.sesion_con_ack_pendiente()
        antes = self.client.get(f"/api/cmv40/{sid}").json()["plan"]
        self.assertFalse(antes["skip_sync_review"])
        self.client.post(f"/api/cmv40/{sid}/acknowledge-critical-gates")
        despues = self.client.get(f"/api/cmv40/{sid}").json()["plan"]
        self.assertTrue(despues["skip_sync_review"],
                        "tras el ACK el pipeline puede saltarse la Fase D")

    def test_sin_confirmacion_pendiente_da_400(self):
        sid = self.crear_sesion(phase="target_provided")
        r = self.client.post(f"/api/cmv40/{sid}/acknowledge-critical-gates")
        self.assertEqual(r.status_code, 400)


class TestQueFaseDisparaCadaEndpoint(ApiTestCase):
    """El mapeo endpoint → (fase, fase destino).

    Es lo que un refactor de routers puede romper sin que nada más se
    entere: si `/inject` acabara lanzando `remux`, el pipeline seguiría
    "funcionando" y produciría un MKV incorrecto. El arnés sustituye el
    lanzador por un espía, así que se comprueba qué se pidió arrancar sin
    ejecutar ffmpeg.
    """

    MAPEO = {
        "analyze-source":        ("analyze_source",   "source_analyzed"),
        "target-rpu-path":       ("target_rpu_path",  "target_provided"),
        "target-rpu-from-drive": ("target_rpu_drive", "target_provided"),
        "target-rpu-from-mkv":   ("target_rpu_mkv",   "target_provided"),
        "extract":               ("extract",          "extracted"),
        "inject":                ("inject",           "injected"),
        "remux":                 ("remux",            "remuxed"),
        "validate":              ("validate",         "done"),
    }

    def test_cada_endpoint_lanza_su_fase(self):
        for endpoint, (fase, destino) in self.MAPEO.items():
            with self.subTest(endpoint=endpoint):
                sid = self.crear_sesion(
                    sid=f"cmv40_map_{endpoint.replace('-', '_')}", phase="extracted")
                self.fases_lanzadas.clear()
                r = _post(self.client, f"/api/cmv40/{sid}/{endpoint}",
                          ENDPOINTS_DE_FASE[endpoint])
                self.assertEqual(r.status_code, 200)
                lanzada = self.fase_lanzada()
                self.assertEqual(lanzada["phase"], fase)
                self.assertEqual(lanzada["new_phase"], destino)
                self.assertEqual(lanzada["session_id"], sid)

    def test_cada_fase_ejecuta_el_runner_del_pipeline_que_le_toca(self):
        """La tabla `_CMV40_RUNNERS` asocia fase → función del pipeline.

        El nombre de la fase y la función que la ejecuta se pasan por
        separado, así que una fila cruzada (`inject` apuntando al remuxer)
        daría un `phase_name` correcto y ejecutaría otra cosa: el MKV
        saldría mal sin que nada lo delatara.
        """
        esperado = {
            "analyze-source": "run_phase_a_analyze_source",
            "extract":        "run_phase_c_extract",
            "inject":         "run_phase_f_inject",
            "remux":          "run_phase_g_remux",
            "validate":       "run_phase_h_validate",
        }
        for endpoint, runner in esperado.items():
            with self.subTest(endpoint=endpoint):
                llamados = self.mockear_runners()
                sid = self.crear_sesion(
                    sid=f"cmv40_run_{endpoint.replace('-', '_')}", phase="extracted")
                self.fases_lanzadas.clear()
                self.client.post(f"/api/cmv40/{sid}/{endpoint}")
                self.ejecutar_fase_lanzada()
                self.assertEqual(llamados, [runner])

    def test_la_correccion_de_sync_no_avanza_de_fase(self):
        # Fase E se puede repetir: el usuario itera sobre el chart hasta que
        # el Δ es 0. Avanzar de fase aquí le impediría corregir otra vez.
        sid = self.crear_sesion(phase="extracted")
        r = self.client.post(f"/api/cmv40/{sid}/apply-sync",
                             json={"editor_config": {"remove": ["0-12"]}})
        self.assertEqual(r.status_code, 200)
        lanzada = self.fase_lanzada()
        self.assertEqual(lanzada["phase"], "correct_sync")
        self.assertEqual(lanzada["new_phase"], "extracted",
                         "correct_sync debe dejar la fase donde estaba")

    def test_los_endpoints_de_fase_responden_sin_esperar(self):
        # Son fire-and-forget: devuelven `started` y el progreso viaja por WS.
        # Si alguno pasara a esperar a la fase, la UI se quedaría colgada.
        sid = self.crear_sesion(phase="extracted")
        r = self.client.post(f"/api/cmv40/{sid}/inject")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get("started"))


class TestElPlanViajaAlFrontend(ApiTestCase):
    """El detalle de la sesión incluye el plan de la matriz de workflows.

    `app.js` calculaba por su cuenta el trust efectivo (once veces, en dos
    variantes sintácticas), el drop-in, si el target necesita merge y si hay
    demux o mux. Son réplicas de reglas que viven en `cmv40_strategy`, y una
    réplica se desincroniza en silencio: el bug del overlay fue de esa misma
    familia — la UI decidiendo por su cuenta sobre estado del backend.
    """

    def plan_de(self, **campos) -> dict:
        sid = self.crear_sesion(**campos)
        r = self.client.get(f"/api/cmv40/{sid}")
        self.assertEqual(r.status_code, 200)
        cuerpo = r.json()
        self.assertIn("plan", cuerpo, "el detalle debe traer el plan")
        return cuerpo["plan"]

    def test_el_drop_in_llega_resuelto(self):
        plan = self.plan_de(source_workflow="p7_fel",
                            target_type="trusted_p7_fel_final",
                            target_trust_ok=True, trust_override="auto")
        self.assertTrue(plan["drop_in"])
        self.assertTrue(plan["trust_effective"])
        self.assertFalse(plan["extract"]["needs_demux"])
        self.assertFalse(plan["inject"]["needs_merge"])
        self.assertFalse(plan["remux"]["needs_dovi_mux"])
        self.assertTrue(plan["validate"]["fast_path"])

    def test_la_rama_merge_llega_resuelta(self):
        plan = self.plan_de(source_workflow="p7_fel",
                            target_type="trusted_p8_source",
                            target_trust_ok=False)
        self.assertFalse(plan["drop_in"])
        self.assertTrue(plan["extract"]["needs_demux"])
        self.assertTrue(plan["inject"]["needs_merge"])
        self.assertTrue(plan["remux"]["needs_dovi_mux"])
        self.assertEqual(plan["inject"]["hevc_input"], "EL.hevc")
        self.assertEqual(plan["inject"]["hevc_output"], "EL_injected.hevc")
        self.assertFalse(plan["validate"]["fast_path"])

    def test_el_single_layer_pide_la_conversion_a_p8(self):
        for wf in ("p7_mel", "p8"):
            with self.subTest(workflow=wf):
                plan = self.plan_de(sid=f"cmv40_plan_{wf}", source_workflow=wf,
                                    target_type="trusted_p8_source",
                                    target_trust_ok=True)
                self.assertTrue(plan["single_layer_output"])
                self.assertTrue(plan["inject"]["needs_profile8"])
                self.assertIn("P8.1", plan["remux"]["video_track_name"])

    def test_force_interactive_apaga_el_trust_y_el_drop_in(self):
        plan = self.plan_de(source_workflow="p7_fel",
                            target_type="trusted_p7_fel_final",
                            target_trust_ok=True,
                            trust_override="force_interactive")
        self.assertFalse(plan["trust_effective"])
        self.assertFalse(plan["drop_in"])
        self.assertTrue(plan["extract"]["needs_demux"])
        # Y con Fase D activa, hacen falta los datos del chart.
        self.assertFalse(plan["extract"]["skip_per_frame_data"])

    def test_una_sesion_recien_creada_ya_trae_un_plan_coherente(self):
        # En `created` no hay target todavía: el plan asume p7_fel y sin
        # trust, que es lo conservador — la UI puede pintar algo sin
        # inventarse nada.
        plan = self.plan_de()
        self.assertFalse(plan["drop_in"])
        self.assertFalse(plan["trust_effective"])
        self.assertIn("needs_demux", plan["extract"])

    def test_el_plan_coincide_con_la_tabla(self):
        # Si la serialización se desviara de la tabla, el frontend recibiría
        # una tercera versión de la verdad.
        from phases.cmv40_strategy import WorkflowInputs, plan_for
        casos = [
            ("p7_fel", "trusted_p7_fel_final", True, "auto"),
            ("p7_mel", "trusted_p7_mel_final", True, "auto"),
            ("p8", "generic", False, "auto"),
        ]
        for wf, tt, trust, ov in casos:
            with self.subTest(wf=wf, target=tt):
                enviado = self.plan_de(
                    sid=f"cmv40_cmp_{wf}_{tt}", source_workflow=wf,
                    target_type=tt, target_trust_ok=trust, trust_override=ov)
                esperado = plan_for(WorkflowInputs(wf, tt, trust, ov)).to_dict()
                self.assertEqual(enviado, esperado)



class TestListadoYResumen(ApiTestCase):

    def test_listado_vacio(self):
        self.assertEqual(self.client.get("/api/cmv40").json()["sessions"], [])

    def test_el_listado_incluye_lo_que_el_sidebar_necesita(self):
        # El summary vacía campos pesados; si se lleva por delante uno que el
        # sidebar pinta, la UI se queda sin él sin que nadie se entere.
        self.crear_sesion(phase="injected", source_workflow="p7_fel",
                          target_type="trusted_p8_source", awaiting_critical_ack=True)
        fila = self.client.get("/api/cmv40").json()["sessions"][0]
        for campo in ("id", "phase", "output_mkv_name", "source_workflow",
                      "target_type", "awaiting_critical_ack", "running_phase"):
            with self.subTest(campo=campo):
                self.assertIn(campo, fila)

    def test_activos_refleja_las_fases_en_curso(self):
        """La respuesta sale del registro EN MEMORIA, no del `running_phase`
        del JSON: este proceso es el único que arranca fases y el arranque
        limpia los fantasmas del disco. Detalle en
        `test_cmv40_activas_en_memoria`."""
        self.cmv40._cmv40_activas.clear()
        self.addCleanup(self.cmv40._cmv40_activas.clear)
        sid = self.crear_sesion(sid="cmv40_parado_1", phase="injected")
        r = self.client.get("/api/cmv40-active").json()
        self.assertFalse(r["active"])
        corriendo = self.crear_sesion(sid="cmv40_corriendo_1", phase="injected")
        self.cmv40._cmv40_marcar_activa(self.leer_sesion(corriendo), "remux")
        r = self.client.get("/api/cmv40-active").json()
        self.assertTrue(r["active"])
        self.assertIn("cmv40_corriendo_1", r["ids"])
        self.assertNotIn(sid, r["ids"])

    def test_el_detalle_puede_omitir_el_log(self):
        # Los pollers piden include_log=false mientras el WS entrega el log:
        # con miles de líneas, mandarlo en cada poll son cientos de KB.
        sid = self.crear_sesion(output_log=["línea 1", "línea 2"])
        con = self.client.get(f"/api/cmv40/{sid}").json()
        sin = self.client.get(f"/api/cmv40/{sid}?include_log=false").json()
        self.assertEqual(len(con["output_log"]), 2)
        self.assertEqual(sin["output_log"], [])


if __name__ == "__main__":
    unittest.main()
