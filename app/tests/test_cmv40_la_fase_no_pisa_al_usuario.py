"""Lo que el usuario cambia mientras corre una fase CMv4.0 no se pierde, un
remux a medias no se da por bueno y lo que espera en la cola cuenta.

Fallos del router de CMv4.0 que encontró la auditoría completa con Codex del
2026-10-17, reproducidos antes de arreglarlos:

  · **La fase guardaba su copia vieja de la sesión al terminar.** Apagar el
    auto-pipeline mientras corría se deshacía —y la fase siguiente se
    encadenaba igual—, y la ficha de TMDb y el veredicto de la hoja que se
    hidratan al crear el proyecto los borraba el pre-flight. Hoy el endpoint
    lleva el cambio también al objeto que la fase tiene en la mano.
  · **Un remux cancelado o cortado por un reinicio dejaba su `.mkv.tmp` a
    medias**, y el forward-roll lo daba por bueno con que hubiera CUALQUIER
    remux terminado en el historial.
  · **Una fase que espera en la cola no tiene `running_phase`**: limpiar el
    proyecto borraba los artefactos y la fase corría igual después, sobre el
    proyecto archivado; y reintentar el pre-flight con la Fase A en cola la
    perdía.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_la_fase_no_pisa_al_usuario -v
"""
import asyncio
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402


class TestLosCambiosDuranteLaFaseSobreviven(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.ejecutar_fases_de_verdad()
        self.despachos = []

        async def _no_despachar(session_id):
            self.despachos.append(session_id)
        orig = self.cmv40._cmv40_dispatch_next_phase
        self.cmv40._cmv40_dispatch_next_phase = _no_despachar
        self.addCleanup(setattr, self.cmv40, "_cmv40_dispatch_next_phase", orig)

    def _correr_fase(self, sid, durante):
        r = self.cmv40
        s = self.leer_sesion(sid)

        async def coro(log_cb, proc_cb):
            await durante()

        async def main():
            await r._run_cmv40_phase(s, "remux", coro, "remuxed")
            await asyncio.sleep(0.05)
        asyncio.run(main())

    def test_apagar_el_auto_durante_la_fase_no_se_deshace(self):
        sid = self.crear_sesion(phase="injected", auto_pipeline=True)
        r = self.cmv40

        async def durante():
            await r.cmv40_set_auto_pipeline(sid, r.CMv40AutoPipelineRequest(enabled=False))
        self._correr_fase(sid, durante)
        self.assertFalse(self.leer_sesion(sid).auto_pipeline,
                         "la fase ha vuelto a encender el auto al guardar")
        self.assertEqual(self.despachos, [], "ha encadenado la fase siguiente con el auto apagado")

    def test_la_hoja_hidratada_durante_la_fase_no_se_borra(self):
        sid = self.crear_sesion(phase="injected")
        r = self.cmv40

        async def durante():
            fresh = self.leer_sesion(sid)
            fresh.sheet_recommendation = {"status": "recommended"}
            import storage
            storage.save_cmv40_session(fresh)
            r._cmv40_propagar_a_la_viva(sid, sheet_recommendation=fresh.sheet_recommendation)
        self._correr_fase(sid, durante)
        self.assertEqual((self.leer_sesion(sid).sheet_recommendation or {}).get("status"),
                         "recommended")

    def test_la_hidratacion_real_lo_lleva_a_la_sesion_viva(self):
        """El escritor de verdad (`_cmv40_hydrate_sheet_recommendation`), no
        la propagación a mano."""
        sid = self.crear_sesion(phase="created", source_mkv_name="Peli (2024).mkv")
        r = self.cmv40
        viva = self.leer_sesion(sid)
        r._cmv40_marcar_activa(viva, "preflight")
        self.addCleanup(r._cmv40_marcar_libre, viva)

        class _Rec:
            def model_dump(self):
                return {"status": "recommended"}

        async def _recommend(*a, **k):
            return _Rec()
        import services.cmv40_recommend as rec
        orig = rec.recommend
        rec.recommend = _recommend
        self.addCleanup(setattr, rec, "recommend", orig)
        asyncio.run(r._cmv40_hydrate_sheet_recommendation(sid))
        self.assertEqual((viva.sheet_recommendation or {}).get("status"), "recommended",
                         "el pre-flight guardaría su copia sin el veredicto")


class TestElRemuxAMediasNoSeDaPorBueno(ApiTestCase):

    def _tmp(self, sid):
        from phases import cmv40_pipeline
        return cmv40_pipeline.OUTPUT_DIR / f"{self.leer_sesion(sid).output_mkv_name}.tmp"

    def test_cancelar_el_remux_borra_su_tmp(self):
        self.ejecutar_fases_de_verdad()
        sid = self.crear_sesion(phase="injected")
        r = self.cmv40
        tmp = self._tmp(sid)

        async def coro(log_cb, proc_cb):
            tmp.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 1024)     # a medias
            raise r._cmv40_pipeline_mod.CMv40Cancelled()
        asyncio.run(r._run_cmv40_phase(self.leer_sesion(sid), "remux", coro, "remuxed"))
        self.assertFalse(tmp.exists(), "el .mkv.tmp a medias se ha quedado")

    def _con_historial(self, sid, estados, **campos):
        from datetime import datetime, timezone
        from models import CMv40PhaseRecord
        import storage
        s = self.leer_sesion(sid)
        s.phase_history = [CMv40PhaseRecord(phase="remux", status=e,
                                            started_at=datetime.now(timezone.utc))
                           for e in estados]
        for k, v in campos.items():
            setattr(s, k, v)
        storage.save_cmv40_session(s)
        self._tmp(sid).write_bytes(b"\x00" * 1024)

    def _fase_tras_el_get(self, sid):
        import dev_fixtures
        orig = self.cmv40.DEV_MODE
        self.cmv40.DEV_MODE = False
        self.addCleanup(setattr, self.cmv40, "DEV_MODE", orig)
        self.assertEqual(self.client.get(f"/api/cmv40/{sid}").status_code, 200)
        return self.leer_sesion(sid).phase

    def test_el_forward_roll_mira_el_ultimo_remux(self):
        sid = self.crear_sesion(phase="injected")
        self._con_historial(sid, ["done", "cancelled"])
        self.assertEqual(self._fase_tras_el_get(sid), "injected",
                         "un remux bueno de ayer da por bueno el de hoy, cancelado")

    def test_el_forward_roll_no_actua_con_una_fase_en_marcha(self):
        sid = self.crear_sesion(phase="injected")
        self._con_historial(sid, ["done"], running_phase="remux")
        self.assertEqual(self._fase_tras_el_get(sid), "injected")

    def test_el_forward_roll_sigue_recuperando_lo_legitimo(self):
        sid = self.crear_sesion(phase="injected")
        self._con_historial(sid, ["done"])
        self.assertEqual(self._fase_tras_el_get(sid), "remuxed")

    def test_el_recovery_borra_el_tmp_del_remux_cortado(self):
        sid = self.crear_sesion(phase="injected")
        self._con_historial(sid, ["running"], running_phase="remux")
        self.cmv40.recuperar_sesiones_interrumpidas()
        self.assertFalse(self._tmp(sid).exists())
        self.assertEqual(self.leer_sesion(sid).phase_history[-1].status, "error")


class TestUnaFaseEnColaCuenta(ApiTestCase):

    def _en_cola(self, sid, fase="inject"):
        import queue_manager as qm
        cola = self.main.queue_manager
        cola._queue.append(qm.TrabajoEnCola(tab="cmv40", tipo=qm.TIPO_FASE_CMV40,
                                            clave=sid, que="x", datos={"fase": fase}))
        self.addCleanup(cola._queue.clear)

    def test_limpiar_con_una_fase_en_cola_da_409(self):
        sid = self.crear_sesion(phase="injected")
        self._en_cola(sid)
        r = self.client.post(f"/api/cmv40/{sid}/cleanup")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertFalse(self.leer_sesion(sid).archived)

    def test_borrar_con_limpieza_y_una_fase_en_cola_da_409(self):
        sid = self.crear_sesion(phase="injected")
        self._en_cola(sid)
        r = self.client.delete(f"/api/cmv40/{sid}?clean_artifacts=true")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIsNotNone(self.leer_sesion(sid))

    def test_renombrar_con_una_fase_en_marcha_da_409(self):
        sid = self.crear_sesion(phase="injected", running_phase="remux")
        r = self.client.post(f"/api/cmv40/{sid}/rename-output",
                             json={"output_mkv_name": "Otro.mkv"})
        self.assertEqual(r.status_code, 409, r.text)

    def test_el_runner_no_corre_una_fase_sobre_un_proyecto_archivado(self):
        import queue_manager as qm
        sid = self.crear_sesion(phase="injected", archived=True)
        llamados = self.mockear_runners()
        self.ejecutar_fases_de_verdad()
        trabajo = qm.TrabajoEnCola(tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
                                   que="x", datos={"fase": "remux"})
        asyncio.run(self.cmv40._cmv40_runner_de_la_cola(trabajo))
        self.assertEqual(llamados, [])


if __name__ == "__main__":
    unittest.main()
