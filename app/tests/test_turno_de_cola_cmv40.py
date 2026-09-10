"""Un turno de cola es el proyecto CMv4.0 entero, no una fase.

Cada fase era su propia entrada en la cola, y eso tenía dos problemas. El
visible: el historial se llenaba de siete «Fase X terminada» por película, y un
proyecto parado esperando respuesta se leía igual que uno acabado. El de fondo:
**una conversión a medias no es un resultado** — no hay MKV que enseñar y los
250-400 GB de artefactos siguen ocupando `/mnt/tmp`—, así que soltar el turno
entre fases para que se cuele un rip de 40 minutos no ayuda a nadie.

Ahora el turno se retiene hasta que el proyecto termina, falla, se cancela o
**necesita al usuario**. Ahí lo suelta, y al contestar se encola otro.

Lo que fija este fichero:

- Un turno ejecuta las fases seguidas, **sin volver a la cola** entre ellas.
- Al pararse deja **UNA** línea, con el estado del proyecto.
- «Esperando» es un predicado (no ha terminado, no corre, no está en cola, no
  ha fallado), así que cubre los cuatro sitios donde el pipeline se para sin
  enumerarlos.
- Lo que el usuario lanza a mano SÍ pasa por la cola: ahí no hay turno.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_turno_de_cola_cmv40 -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import historial  # noqa: E402
import queue_manager as qm  # noqa: E402
from api_harness import ApiTestCase  # noqa: E402


class TurnoCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        import storage
        from routers import cmv40
        self.cmv40 = cmv40
        self.storage = storage
        # El arnés espía `_run_cmv40_phase` para que los endpoints no ejecuten
        # nada; aquí se necesita el de verdad, porque lo que se prueba es el
        # encadenado. Lo que se sustituye son las fases.
        self.ejecutar_fases_de_verdad()
        # Las fases se sustituyen por corutinas que solo apuntan su nombre:
        # lo que se prueba es el ENCADENADO, no lo que hace cada una.
        self.ejecutadas: list[str] = []

    def _sesion(self, **campos):
        sid = self.crear_sesion(sid="cmv40_turno")
        s = self.storage.load_cmv40_session(sid)
        s.auto_pipeline = True
        s.target_preflight_ok = True
        s.output_mkv_name = "Predator (2026) [CMv4].mkv"
        for k, v in campos.items():
            setattr(s, k, v)
        self.storage.save_cmv40_session(s)
        return sid

    def _fases_falsas(self, avances):
        """`avances` = {fase: nueva_phase}. Cada fase apunta su nombre y
        mueve la sesión a la fase destino, como haría el pipeline."""
        cmv40 = self.cmv40
        original = cmv40._cmv40_construir_fase

        def _construir(session, fase, datos):
            if fase not in avances:
                return original(session, fase, datos)

            async def _coro(log_cb, proc_cb):
                self.ejecutadas.append(fase)

            return _coro, avances[fase]

        cmv40._cmv40_construir_fase = _construir
        self.addCleanup(setattr, cmv40, "_cmv40_construir_fase", original)


class TestUnTurnoEsTodoElProyecto(TurnoCase):

    def test_las_fases_se_encadenan_sin_volver_a_la_cola(self):
        import asyncio
        from models import CMv40Phase
        sid = self._sesion(phase="target_provided")
        self._fases_falsas({
            "extract": CMv40Phase.SYNC_VERIFIED,   # trusted → sin Fase D
            "inject":  CMv40Phase.INJECTED,
            "remux":   CMv40Phase.REMUXED,
            "validate": CMv40Phase.DONE,
        })
        asyncio.run(self.cmv40._cmv40_runner_de_la_cola(
            qm.TrabajoEnCola(tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
                             datos={"fase": "extract"})))
        self.assertEqual(self.ejecutadas,
                         ["extract", "inject", "remux", "validate"])
        # Y ni una vuelta a la cola: el turno no se suelta entre fases.
        self.assertEqual(
            [t for t in self.trabajos_encolados if t[1] == sid], [],
            "una fase volvió a la cola en medio del turno")

    def test_al_terminar_deja_UNA_linea_y_es_del_proyecto(self):
        import asyncio
        from models import CMv40Phase
        sid = self._sesion(phase="target_provided")
        self._fases_falsas({
            "extract": CMv40Phase.SYNC_VERIFIED, "inject": CMv40Phase.INJECTED,
            "remux": CMv40Phase.REMUXED, "validate": CMv40Phase.DONE,
        })
        asyncio.run(self.cmv40._cmv40_runner_de_la_cola(
            qm.TrabajoEnCola(tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
                             datos={"fase": "extract"})))
        t = historial.leer()
        self.assertEqual(len(t), 1, t)
        self.assertEqual(t[0]["estado"], historial.ESTADO_HECHO)
        self.assertIn("Upgrade CMv4.0", t[0]["que"])
        self.assertIn("Predator", t[0]["que"])
        self.assertNotIn("Fase", t[0]["que"])

    def test_el_tiempo_de_la_linea_es_el_de_PROCESO(self):
        """La suma de las fases, no el reloj de pared: un proyecto puede
        pasarse tres días esperando una respuesta."""
        import asyncio
        from models import CMv40Phase
        sid = self._sesion(phase="remuxed")
        self._fases_falsas({"validate": CMv40Phase.DONE})
        asyncio.run(self.cmv40._cmv40_runner_de_la_cola(
            qm.TrabajoEnCola(tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
                             datos={"fase": "validate"})))
        s = self.storage.load_cmv40_session(sid)
        suma = sum((r.elapsed_seconds or 0) for r in s.phase_history)
        self.assertAlmostEqual(historial.leer()[0]["segundos"], suma, delta=0.2)


class TestSeParaCuandoTeNecesita(TurnoCase):

    def test_un_ACK_pendiente_suelta_el_turno_y_queda_esperando(self):
        """El caso real del NAS: el proyecto de Drive se quedó en
        `awaiting_critical_ack` y en la columna solo había dos «Fase done»."""
        import asyncio
        from models import CMv40Phase
        sid = self._sesion(phase="target_provided", awaiting_critical_ack=True)
        self._fases_falsas({"extract": CMv40Phase.EXTRACTED})
        asyncio.run(self.cmv40._cmv40_runner_de_la_cola(
            qm.TrabajoEnCola(tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
                             datos={"fase": "extract"})))
        self.assertEqual(self.ejecutadas, ["extract"])
        t = historial.leer()
        self.assertEqual(len(t), 1, t)
        self.assertEqual(t[0]["estado"], historial.ESTADO_ESPERANDO)
        self.assertIsNone(t[0]["error"], "esperar no es fallar")

    def test_la_revision_de_sync_tambien(self):
        """Fase D es una parada legítima: el usuario mira el gráfico."""
        import asyncio
        from models import CMv40Phase
        sid = self._sesion(phase="target_provided", target_trust_ok=False,
                           target_type="generic")
        self._fases_falsas({"extract": CMv40Phase.EXTRACTED})
        asyncio.run(self.cmv40._cmv40_runner_de_la_cola(
            qm.TrabajoEnCola(tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
                             datos={"fase": "extract"})))
        self.assertEqual(self.ejecutadas, ["extract"])
        self.assertEqual(historial.leer()[0]["estado"],
                         historial.ESTADO_ESPERANDO)

    def test_un_fallo_para_el_turno_y_queda_como_error(self):
        import asyncio
        cmv40 = self.cmv40
        sid = self._sesion(phase="target_provided")
        original = cmv40._cmv40_construir_fase

        def _construir(session, fase, datos):
            async def _coro(log_cb, proc_cb):
                self.ejecutadas.append(fase)
                raise RuntimeError("dovi_tool se cayó")
            return _coro, "extracted"

        cmv40._cmv40_construir_fase = _construir
        self.addCleanup(setattr, cmv40, "_cmv40_construir_fase", original)
        asyncio.run(cmv40._cmv40_runner_de_la_cola(
            qm.TrabajoEnCola(tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
                             datos={"fase": "extract"})))
        self.assertEqual(self.ejecutadas, ["extract"])
        t = historial.leer()
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["estado"], historial.ESTADO_ERROR)
        self.assertIn("dovi_tool", t[0]["error"])

    def test_al_contestar_se_encola_otro_turno(self):
        """Y la línea de «esperando» se retira: mientras corra, el sitio
        donde se ve es «En curso»."""
        import historial as h
        from datetime import datetime, timezone
        sid = self._sesion(phase="extracted")
        h.registrar_estado(id=sid, tab=h.TAB_CMV40, tipo=h.TIPO_FASE_CMV40,
                           que="Upgrade CMv4.0 · Predator (2026)",
                           inicio=datetime.now(timezone.utc),
                           estado=h.ESTADO_ESPERANDO)
        r = self.client.post(f"/api/cmv40/{sid}/mark-synced?force=true")
        self.assertEqual(r.status_code, 200, r.text)
        # Primero el encolado: si eso pasó, la línea se retiró justo antes
        # (van en la misma función, en ese orden).
        self.assertTrue([t for t in self.trabajos_encolados if t[1] == sid],
                        "no se encoló la continuación")
        self.assertEqual(h.leer(), [], "la línea seguía pidiendo")


class TestLoQueLanzaElUsuarioSIPasaPorLaCola(TurnoCase):

    def test_una_fase_pedida_a_mano_se_encola(self):
        """Fuera de un turno no hay nada que retener: el endpoint encola y
        responde, que es lo que hace la columna visible desde el primer
        segundo."""
        sid = self._sesion(phase="target_provided")
        r = self.client.post(f"/api/cmv40/{sid}/extract")
        self.assertEqual(r.status_code, 200, r.text)
        encolados = [t for t in self.trabajos_encolados if t[1] == sid]
        self.assertEqual(len(encolados), 1, self.trabajos_encolados)
        self.assertEqual(encolados[0][2].get("fase"), "extract")


if __name__ == "__main__":
    unittest.main()


class TestElPorcentajeEsDelProceso(TurnoCase):
    """Un turno es el proyecto entero, así que el porcentaje y el tiempo que
    se enseñan son del proceso completo: la fase se dice al lado, con su letra
    y su puesto.

    Y salen de **`_cmv40_job_pct`**, que ya existía y está calibrado con el
    reparto real de las 83 sesiones del histórico. Calcular aquí un segundo
    total es lo que produjo tres cifras distintas para la misma pregunta: la
    de la fase, la del panel del proyecto y la de la columna.
    """

    def _progreso(self, prog, **kw):
        from models import CMv40PhaseRecord
        from datetime import datetime, timedelta, timezone
        sid = self._sesion(phase="injected")
        s = self.storage.load_cmv40_session(sid)
        ahora = datetime.now(timezone.utc)
        s.phase_history = [
            CMv40PhaseRecord(phase="analyze_source", status="done",
                             started_at=ahora - timedelta(seconds=700),
                             elapsed_seconds=600),
            CMv40PhaseRecord(phase="extract", status="done",
                             started_at=ahora - timedelta(seconds=400),
                             elapsed_seconds=300),
            CMv40PhaseRecord(phase="remux", status="running",
                             started_at=ahora - timedelta(seconds=100)),
        ]
        s.running_phase = "remux"
        for k, v in kw.items():
            setattr(s, k, v)
        self.storage.save_cmv40_session(s)
        self.storage.write_cmv40_progress(sid, prog)
        return self.cmv40._cmv40_adaptador(
            qm.TrabajoEnCola(tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
                             datos={"fase": "remux"}))

    def test_el_transcurrido_es_la_suma_de_las_fases(self):
        """Tiempo de PROCESO, no de reloj: el proyecto puede haber pasado tres
        días esperando una respuesta entre dos fases."""
        p = self._progreso({"pct": 50, "job_pct": 70.0})
        self.assertAlmostEqual(p["segundos"], 600 + 300 + 100, delta=3)

    def test_el_pct_es_el_del_job_no_el_de_la_fase(self):
        p = self._progreso({"pct": 50, "job_pct": 70.0})
        self.assertEqual(p["pct"], 70)

    def test_el_restante_se_extrapola_de_ESE_porcentaje(self):
        """Del mismo número que la barra, así que no pueden contradecirse."""
        p = self._progreso({"pct": 50, "job_pct": 70.0})
        # 1000 s son el 70 % → faltan 1000 × 30/70 = 429 s.
        self.assertAlmostEqual(p["eta_s"], 429, delta=5)
        self.assertEqual(p["eta_fuente"], "modelo")

    def test_si_el_marcador_no_lo_trae_se_recalcula_igual(self):
        """Sesiones anteriores al campo. Con la MISMA función, no con otra."""
        p = self._progreso({"pct": 50})
        self.assertIsNotNone(p["pct"])
        esperado = self.cmv40._cmv40_job_pct(
            self.storage.load_cmv40_session("cmv40_turno"), 50)
        self.assertEqual(p["pct"], round(esperado))

    def test_la_fase_se_sigue_diciendo_al_lado(self):
        p = self._progreso({"pct": 50, "job_pct": 70.0})
        self.assertEqual(p["fase"], "remux")
        self.assertIn("Fase G", p["fase_label"])
        self.assertEqual(p["fases_total"], 7)

    def test_sin_porcentaje_del_job_no_se_inventa_uno(self):
        p = self._progreso({"pct": 50}, running_phase="")
        self.assertIsNone(p["pct"])
        self.assertFalse(p["pct_medido"])
        self.assertIsNone(p["eta_s"])


class TestElTotalLlegaHastaLaColumna(TurnoCase):
    """La cadena ENTERA por HTTP, que es lo que el usuario mira.

    Que el adaptador calcule bien no basta: entre él y la tarjeta están
    `queue_manager.get_status()`, `trabajos.progreso_de` y el endpoint, y el
    contrato tiene un `pct` y un `segundos` que cualquiera de los tres podría
    dejar en el de la fase.
    """

    def _job_en_marcha(self):
        from models import CMv40PhaseRecord
        from datetime import datetime, timedelta, timezone
        sid = self._sesion(phase="injected")
        s = self.storage.load_cmv40_session(sid)
        ahora = datetime.now(timezone.utc)
        s.phase_history = [
            CMv40PhaseRecord(phase="analyze_source", status="done",
                             started_at=ahora - timedelta(seconds=900),
                             elapsed_seconds=600),
            CMv40PhaseRecord(phase="extract", status="done",
                             started_at=ahora - timedelta(seconds=300),
                             elapsed_seconds=240),
            CMv40PhaseRecord(phase="remux", status="running",
                             started_at=ahora - timedelta(seconds=60)),
        ]
        s.running_phase = "remux"
        self.storage.save_cmv40_session(s)
        # El progreso de la FASE: 90 % y 30 s para acabarla. Lo que la columna
        # tiene que enseñar NO es esto.
        self.storage.write_cmv40_progress(sid, {"pct": 90, "eta_s": 30,
                                                "job_pct": 62.0,
                                                "label": "Muxeando"})
        cola = self.main.queue_manager
        # `_running` guarda el objeto, no su JSON: `get_status` es quien
        # serializa.
        cola._running = qm.TrabajoEnCola(
            tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
            que="Upgrade CMv4.0 · Predator (2026)",
            titulo="Predator (2026)", datos={"fase": "remux"})
        self.addCleanup(setattr, cola, "_running", None)
        return self.client.get("/api/trabajos").json()["activo"]

    def test_el_pct_del_endpoint_es_el_del_proceso(self):
        a = self._job_en_marcha()
        self.assertEqual(a["pct"], 62)
        self.assertNotEqual(a["pct"], 90, "sigue siendo el de la fase")

    def test_el_transcurrido_tambien(self):
        a = self._job_en_marcha()
        self.assertAlmostEqual(a["segundos"], 900, delta=3)

    def test_y_el_restante(self):
        a = self._job_en_marcha()
        # 900 s son el 62 % → faltan 900 × 38/62 = 552 s.
        self.assertAlmostEqual(a["eta_s"], 552, delta=6)
        self.assertNotEqual(a["eta_s"], 30, "sigue siendo el de la fase")
        self.assertEqual(a["eta_fuente"], "modelo")

    def test_la_fase_sigue_saliendo_para_los_puntitos(self):
        a = self._job_en_marcha()
        self.assertEqual((a["fase_n"], a["fases_total"]), (6, 7))
        self.assertEqual(a["paso"], "Muxeando")


class TestDuranteLaFaseA(TurnoCase):
    """El caso que el usuario cazó: con la Fase A al 90 % el trabajo iba por
    la cuarta parte del proceso, y la columna anunciaba 90.

    El reparto lo da `_cmv40_job_pct`, que ya existía y está calibrado con el
    histórico: la Fase A pesa 0,29 del job en la ruta merge, así que al 90 %
    de la fase el trabajo va por el 26 %.
    """

    def _en_fase_a(self, prog):
        from models import CMv40PhaseRecord
        from datetime import datetime, timedelta, timezone
        sid = self._sesion(phase="created")
        s = self.storage.load_cmv40_session(sid)
        s.phase_history = [CMv40PhaseRecord(
            phase="analyze_source", status="running",
            started_at=datetime.now(timezone.utc) - timedelta(seconds=315))]
        s.running_phase = "analyze_source"
        self.storage.save_cmv40_session(s)
        self.storage.write_cmv40_progress(sid, prog)
        return self.cmv40._cmv40_adaptador(qm.TrabajoEnCola(
            tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
            datos={"fase": "analyze_source"}))

    def test_el_90_por_ciento_de_la_fase_A_NO_es_el_90_del_trabajo(self):
        # Es el payload real que devolvía el NAS: 90,2 % y sin ETA.
        a = self._en_fase_a({"pct": 90.2, "eta_s": None, "label": "x"})
        self.assertEqual(a["pct"], 26)
        self.assertNotEqual(a["pct"], 90)

    def test_sin_ETA_de_la_fase_sigue_habiendo_restante_del_job(self):
        """Era lo que fallaba: el restante del job se estimaba desde el
        `eta_s` de la fase, y `_ReadProgress` lo deja de emitir justo en su
        recta final."""
        a = self._en_fase_a({"pct": 90.2, "eta_s": None, "label": "x"})
        self.assertIsNotNone(a["eta_s"])
        self.assertEqual(a["eta_fuente"], "modelo")

    def test_al_principio_de_todo_no_hay_restante_que_dar(self):
        """Con la fase a cero el job va por 0 y extrapolar de ahí sería
        dividir por nada. La app prefiere el hueco."""
        a = self._en_fase_a({"label": "Extrayendo el RPU"})
        self.assertIsNone(a["eta_s"])
        # Pero el transcurrido sigue siendo el del trabajo.
        self.assertAlmostEqual(a["segundos"], 315, delta=3)

    def test_sin_fase_en_curso_tampoco_se_enseña_el_de_la_fase(self):
        """El respaldo que había: con la Fase A al 90 % la barra decía 90 con
        el trabajo por el 20. El número era una medida de verdad, pero de otra
        cosa — y eso es peor que un hueco."""
        import storage
        sid = self._sesion(phase="created")
        s = storage.load_cmv40_session(sid)
        s.running_phase = ""
        storage.save_cmv40_session(s)
        storage.write_cmv40_progress(sid, {"pct": 90.2, "eta_s": 42})
        a = self.cmv40._cmv40_adaptador(qm.TrabajoEnCola(
            tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
            datos={"fase": "analyze_source"}))
        self.assertIsNone(a["pct"], "se coló el porcentaje de la fase")
        self.assertIsNone(a["eta_s"], "se coló el restante de la fase")


class TestUnaSolaCifraParaLaMismaPregunta(TurnoCase):
    """Había TRES restantes distintos a la vista a la vez: 25 min la fase,
    26 el panel del proyecto y 24 la columna. Ninguno de los dos últimos podía
    ser mayor que la fase más lo que viene detrás, así que estaban mal.

    La causa era que cada uno tenía su cuenta. Ahora sale de
    `_cmv40_job_pct` —el estimador calibrado con el histórico— y el restante
    se extrapola de ESE porcentaje, así que la barra y el tiempo no pueden
    contradecirse: son el mismo número.
    """

    def test_el_backend_no_calcula_su_propio_porcentaje(self):
        """`_cmv40_progreso_total` tiene que APOYARSE en `_cmv40_job_pct`, no
        ponderar por su cuenta. Dos ponderaciones divergen en cuanto una se
        recalibra."""
        import inspect
        fuente = inspect.getsource(self.cmv40._cmv40_progreso_total)
        self.assertIn("_cmv40_job_pct", fuente)
        for otro in ("_ETA_MODEL_CACHE", "ratios", "factor"):
            self.assertNotIn(otro, fuente,
                             f"vuelve a haber una segunda cuenta ({otro})")

    def test_el_restante_es_coherente_con_el_porcentaje(self):
        """Si el trabajo va por el X %, lo que queda tiene que ser lo que
        cuesta el (100-X) % al ritmo observado. Es la única forma de que la
        barra y el reloj digan lo mismo."""
        from models import CMv40PhaseRecord
        from datetime import datetime, timedelta, timezone
        sid = self._sesion(phase="injected")
        s = self.storage.load_cmv40_session(sid)
        s.phase_history = [CMv40PhaseRecord(
            phase="analyze_source", status="done",
            started_at=datetime.now(timezone.utc) - timedelta(seconds=400),
            elapsed_seconds=400)]
        s.running_phase = "remux"
        self.storage.save_cmv40_session(s)
        self.storage.write_cmv40_progress(sid, {"pct": 10, "job_pct": 40.0})
        a = self.cmv40._cmv40_adaptador(qm.TrabajoEnCola(
            tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
            datos={"fase": "remux"}))
        total = a["segundos"] + a["eta_s"]
        self.assertAlmostEqual(100.0 * a["segundos"] / total, a["pct"], delta=1)

    def test_el_panel_del_proyecto_lee_el_MISMO_numero(self):
        """La suma local de fases pendientes se queda de respaldo, pero con la
        columna sabiendo del trabajo manda ella. Si cada vista calculara lo
        suyo volveríamos a las tres cifras."""
        from frontend_sources import js_completo
        js = js_completo()
        i = js.index("function _cmv40RestanteDelJob(")
        cuerpo = js[i:js.index("\n}\n", i)]
        self.assertIn("trabajoSobre", cuerpo)
        self.assertIn("eta_s", cuerpo)
        # Y los dos sitios que pintan el restante pasan por aquí (la tercera
        # aparición es la definición).
        self.assertEqual(js.count("_cmv40RestanteDelJob(s, steps"), 3)
        self.assertEqual(js.count("_cmv40ComputeRemainingSecs(s, steps"), 2,
                         "algún sitio sigue sumando por su cuenta")
