"""La cola única: trabajos tipados, runners por tipo y compatibilidad.

`queue_manager` era «una lista de `session_id` y una función que los ejecuta».
Para que sirva a las tres pestañas cada entrada pasa a ser un `TrabajoEnCola`
con `(tab, tipo, clave)`, y el runner se resuelve **por el tipo, al despachar**
— que es lo único que permite que la cola sobreviva a un reinicio: un callable
no se persiste, un literal sí.

Lo que este fichero fija, que no es obvio:

- **La identidad para deduplicar es `(tipo, clave)`, no la clave.** Un proyecto
  CMv4.0 encola fases sucesivas con la misma clave (su session id).
- **Reordenar NO borra.** El panel de Tab 1 solo conoce sus rips; si `reorder`
  filtrase a lo mencionado, arrastrar una tarjeta se llevaría por delante las
  fases CMv4.0 que hubiera detrás. Descartar es otra intención: `descartar()`.
- **El formato del WS no cambia.** `running` y `queue` siguen siendo ids de
  sesión de Tab 1 porque el frontend los lee así en una veintena de sitios.
- **Un tipo sin runner se descarta con ruido**, no bloquea la cola para
  siempre.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cola_unica -v
"""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import queue_manager as qm  # noqa: E402
import workload  # noqa: E402


def _t(tipo, clave, tab="rip", **kw):
    return qm.TrabajoEnCola(tab=tab, tipo=tipo, clave=clave,
                            que=f"{tipo} de {clave}", **kw)


class ColaCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        self.cola = qm.QueueManager()
        self.cola._persist_state = lambda: None
        self.hechos: list[str] = []

    def _runner(self, tipo, *, cuerpo=None):
        async def _fn(trabajo):
            self.hechos.append(trabajo.id)
            if cuerpo:
                await cuerpo(trabajo)
        self.cola.registrar_runner(tipo, _fn)


class TestElRunnerSeResuelvePorTipo(ColaCase):

    async def test_cada_tipo_va_a_su_runner(self):
        self._runner(qm.TIPO_RIP)
        self._runner(qm.TIPO_FASE_CMV40)
        await self.cola.encolar(_t(qm.TIPO_RIP, "peli"))
        await self.cola.encolar(_t(qm.TIPO_FASE_CMV40, "proj", tab="cmv40"))
        await asyncio.sleep(0.1)
        self.assertEqual(self.hechos, ["rip:peli", "fase_cmv40:proj"])

    async def test_se_ejecutan_de_uno_en_uno(self):
        """El punto entero de la cola: dos trabajos largos a la vez contaminan
        `ffmpeg_wall_seconds`, del que salen los timeouts y el ETA."""
        simultaneos, pico = 0, 0

        async def _cuerpo(_):
            nonlocal simultaneos, pico
            simultaneos += 1
            pico = max(pico, simultaneos)
            await asyncio.sleep(0.05)
            simultaneos -= 1

        self._runner(qm.TIPO_RIP, cuerpo=_cuerpo)
        for i in range(4):
            await self.cola.encolar(_t(qm.TIPO_RIP, f"p{i}"))
        await asyncio.sleep(0.5)
        self.assertEqual(pico, 1, "la cola dejó correr dos a la vez")
        self.assertEqual(len(self.hechos), 4)

    async def test_un_tipo_sin_runner_no_bloquea_la_cola(self):
        """Una entrada que sobrevivió a un cambio de versión. Dejarla al frente
        pararía la cola entera para siempre."""
        self._runner(qm.TIPO_RIP)
        await self.cola.encolar(_t("tipo_que_ya_no_existe", "x"))
        await self.cola.encolar(_t(qm.TIPO_RIP, "peli"))
        await asyncio.sleep(0.2)
        self.assertEqual(self.hechos, ["rip:peli"])
        self.assertEqual(self.cola._queue, [])

    async def test_set_run_fn_sigue_valiendo_para_los_rips(self):
        """Tab 1 registra su pipeline con la firma vieja `(session_id)`."""
        vistos = []

        async def _viejo(session_id):
            vistos.append(session_id)

        self.cola.set_run_fn(_viejo)
        await self.cola.enqueue("peli_2024")
        await asyncio.sleep(0.1)
        self.assertEqual(vistos, ["peli_2024"])


class TestLaIdentidadEsTipoMasClave(ColaCase):
    """Con algo ya en marcha, para que la cola no consuma nada.

    Depender de que `create_task(_process())` haya corrido ataría el test al
    reloj: `encolar` programa la tarea pero no cede el control, así que lo que
    se observe depende de cuándo mire el planificador.
    """

    def setUp(self):
        super().setUp()
        self.cola._running = _t(qm.TIPO_RIP, "algo-en-marcha")

    async def test_el_mismo_trabajo_dos_veces_no_se_duplica(self):
        await self.cola.encolar(_t(qm.TIPO_RIP, "otra"))
        await self.cola.encolar(_t(qm.TIPO_RIP, "otra"))
        self.assertEqual([t.clave for t in self.cola._queue], ["otra"])

    async def test_tampoco_si_ya_es_el_que_corre(self):
        await self.cola.encolar(_t(qm.TIPO_RIP, "algo-en-marcha"))
        self.assertEqual(self.cola._queue, [])

    async def test_dos_tipos_con_la_misma_clave_SI_conviven(self):
        """Un proyecto CMv4.0 encola fases sucesivas con su session id. Con la
        clave sola como identidad, la Fase F se descartaría por duplicada
        mientras la Fase C sigue en la cola."""
        await self.cola.encolar(_t(qm.TIPO_FASE_CMV40, "proj", tab="cmv40"))
        await self.cola.encolar(_t(qm.TIPO_ANALISIS_EXTENDIDO, "proj", tab="mkv"))
        self.assertEqual([t.tipo for t in self.cola._queue],
                         [qm.TIPO_FASE_CMV40, qm.TIPO_ANALISIS_EXTENDIDO])


class TestLaCabezaDeLaCola(ColaCase):

    def setUp(self):
        super().setUp()
        self.cola._running = _t(qm.TIPO_RIP, "algo-en-marcha")

    async def test_a_la_cabeza_adelanta(self):
        """La fase siguiente de un proyecto ya empezado no puede esperar detrás
        de rips de 40 min con 250-400 GB de artefactos ocupando /mnt/tmp."""
        await self.cola.encolar(_t(qm.TIPO_RIP, "p1"))
        await self.cola.encolar(_t(qm.TIPO_RIP, "p2"))
        await self.cola.encolar(_t(qm.TIPO_FASE_CMV40, "proj", tab="cmv40"),
                                a_la_cabeza=True)
        self.assertEqual([t.id for t in self.cola._queue],
                         ["fase_cmv40:proj", "rip:p1", "rip:p2"])

    async def test_sin_a_la_cabeza_va_al_final(self):
        await self.cola.encolar(_t(qm.TIPO_RIP, "p1"))
        await self.cola.encolar(_t(qm.TIPO_RIP, "p2"))
        self.assertEqual([t.clave for t in self.cola._queue], ["p1", "p2"])


class TestReordenarNoBorra(ColaCase):

    async def test_lo_no_mencionado_se_conserva_al_final(self):
        """El panel de la cola de Tab 1 solo conoce sus rips: arrastrar una
        tarjeta no puede llevarse las fases CMv4.0 que haya detrás."""
        self.cola._queue = [_t(qm.TIPO_RIP, "a"), _t(qm.TIPO_RIP, "b"),
                            _t(qm.TIPO_FASE_CMV40, "proj", tab="cmv40")]
        await self.cola.reorder(["b", "a"])
        self.assertEqual([t.clave for t in self.cola._queue],
                         ["b", "a", "proj"])

    async def test_descartar_si_borra(self):
        self.cola._queue = [_t(qm.TIPO_RIP, "a"), _t(qm.TIPO_RIP, "b")]
        n = await self.cola.descartar({"a"})
        self.assertEqual(n, 1)
        self.assertEqual([t.clave for t in self.cola._queue], ["b"])

    async def test_descartar_lo_que_no_esta_no_revienta(self):
        self.assertEqual(await self.cola.descartar({"fantasma"}), 0)


class TestElFormatoDelWsNoCambia(ColaCase):
    """`running` y `queue` los lee el frontend en una veintena de sitios."""

    async def test_running_y_queue_son_ids_de_sesion_de_tab1(self):
        self.cola._running = _t(qm.TIPO_RIP, "corriendo")
        self.cola._queue = [_t(qm.TIPO_RIP, "a"), _t(qm.TIPO_RIP, "b")]
        st = self.cola.get_status()
        self.assertEqual(st["running"], "corriendo")
        self.assertEqual(st["queue"], ["a", "b"])

    async def test_un_trabajo_de_otra_pestana_no_se_cuela_en_los_campos_viejos(self):
        """Meter una fase CMv4.0 en `queue` rompería el panel de Tab 1, que
        busca esos ids en su caché de sesiones."""
        self.cola._running = _t(qm.TIPO_FASE_CMV40, "proj", tab="cmv40")
        self.cola._queue = [_t(qm.TIPO_ANALISIS_EXTENDIDO, "aud", tab="mkv"),
                            _t(qm.TIPO_RIP, "peli")]
        st = self.cola.get_status()
        self.assertIsNone(st["running"])
        self.assertEqual(st["queue"], ["peli"])

    async def test_pero_la_vista_completa_esta_en_los_campos_nuevos(self):
        self.cola._running = _t(qm.TIPO_FASE_CMV40, "proj", tab="cmv40")
        self.cola._queue = [_t(qm.TIPO_RIP, "peli")]
        st = self.cola.get_status()
        self.assertEqual(st["running_job"]["tipo"], qm.TIPO_FASE_CMV40)
        self.assertEqual([j["clave"] for j in st["jobs"]], ["peli"])


class TestLaColaSobreviveAUnReinicio(unittest.TestCase):
    """El motivo de que el tipo sea un literal y no un callable."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cola_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = (qm._CONFIG_DIR, qm._QUEUE_STATE_FILE)
        qm._CONFIG_DIR = self.tmp
        qm._QUEUE_STATE_FILE = self.tmp / "queue_state.json"
        self.addCleanup(lambda: (setattr(qm, "_CONFIG_DIR", self._orig[0]),
                                 setattr(qm, "_QUEUE_STATE_FILE", self._orig[1])))

    def test_ida_y_vuelta_por_disco(self):
        c1 = qm.QueueManager()
        c1._queue = [_t(qm.TIPO_RIP, "peli"),
                     _t(qm.TIPO_FASE_CMV40, "proj", tab="cmv40")]
        c1._persist_state()
        c2 = qm.QueueManager()
        self.assertEqual([(t.tipo, t.clave) for t in c2._queue],
                         [(qm.TIPO_RIP, "peli"), (qm.TIPO_FASE_CMV40, "proj")])

    def test_un_queue_state_de_la_version_anterior_se_lee_como_rips(self):
        """Era una lista de `session_id` pelados. Un usuario que actualice con
        la cola llena no puede perderla."""
        qm._QUEUE_STATE_FILE.write_text(
            json.dumps({"running": None, "queue": ["peli_1", "peli_2"]}),
            encoding="utf-8")
        c = qm.QueueManager()
        self.assertEqual([(t.tipo, t.clave) for t in c._queue],
                         [(qm.TIPO_RIP, "peli_1"), (qm.TIPO_RIP, "peli_2")])

    def test_un_json_corrupto_no_impide_arrancar(self):
        qm._QUEUE_STATE_FILE.write_text("{roto", encoding="utf-8")
        self.assertEqual(qm.QueueManager()._queue, [])


if __name__ == "__main__":
    unittest.main()
