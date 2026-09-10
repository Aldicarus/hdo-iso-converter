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
