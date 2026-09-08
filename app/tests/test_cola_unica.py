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

from api_harness import ApiTestCase  # noqa: E402
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

    async def test_un_tipo_sin_runner_se_descarta_diciendolo(self):
        """Una entrada que sobrevivió a un cambio de versión.

        Que la cola siga NO distingue el guard de un `KeyError`: el `finally`
        reanuda igual en los dos casos. Lo que cambia es el rastro — un aviso
        que nombra el tipo, contra un «Task exception was never retrieved» que
        asyncio suelta cuando le viene bien y no dice de qué trabajo era.
        """
        self._runner(qm.TIPO_RIP)
        with self.assertLogs("queue_manager", level="WARNING") as cm:
            await self.cola.encolar(_t("tipo_que_ya_no_existe", "x"))
            await self.cola.encolar(_t(qm.TIPO_RIP, "peli"))
            await asyncio.sleep(0.2)
        self.assertTrue(
            any("tipo_que_ya_no_existe" in l and "x" in l for l in cm.output),
            f"el aviso no nombra el tipo ni el trabajo: {cm.output}")
        self.assertEqual(self.hechos, ["rip:peli"], "la cola se bloqueó")
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


class TestLaColumnaMandaLaIdentidad(ColaCase):
    """La columna de trabajo pinta `data-clave="${j.id}"`, o sea `tipo:clave`,
    porque es lo que identifica una entrada sin ambigüedad. La cola nació con
    rips y su endpoint recibía el session id pelado; si solo se aceptara ese,
    quitar y reordenar desde la columna **no harían nada** —sin error: la
    entrada simplemente no se encuentra— que es como se vio en el NAS.
    """

    async def test_quitar_por_el_id_compuesto(self):
        self.cola._queue = [_t(qm.TIPO_FASE_CMV40, "proj", tab="cmv40"),
                            _t(qm.TIPO_RIP, "a")]
        self.assertTrue(await self.cola.cancel("fase_cmv40:proj"))
        self.assertEqual([t.clave for t in self.cola._queue], ["a"])

    async def test_y_por_la_clave_pelada_que_es_lo_que_manda_tab1(self):
        self.cola._queue = [_t(qm.TIPO_RIP, "a"), _t(qm.TIPO_RIP, "b")]
        self.assertTrue(await self.cola.cancel("a"))
        self.assertEqual([t.clave for t in self.cola._queue], ["b"])

    async def test_reordenar_por_el_id_compuesto(self):
        self.cola._queue = [_t(qm.TIPO_RIP, "a"),
                            _t(qm.TIPO_FASE_CMV40, "proj", tab="cmv40"),
                            _t(qm.TIPO_ANALISIS_EXTENDIDO, "mkv", tab="mkv")]
        await self.cola.reorder(
            ["analisis_extendido:mkv", "fase_cmv40:proj", "rip:a"])
        self.assertEqual([t.clave for t in self.cola._queue],
                         ["mkv", "proj", "a"])

    async def test_mezclar_las_dos_formas_no_duplica_una_entrada(self):
        """`indice` guarda cada trabajo bajo su id Y su clave: sin el control
        de vistos, una lista que mencione las dos lo colocaría dos veces y la
        cola crecería sola."""
        self.cola._queue = [_t(qm.TIPO_RIP, "a"), _t(qm.TIPO_RIP, "b")]
        await self.cola.reorder(["rip:b", "b", "a"])
        self.assertEqual([t.clave for t in self.cola._queue], ["b", "a"])

    async def test_el_endpoint_devuelve_la_clave_no_el_id(self):
        """Quien lo llama la usa para refrescar la sesión."""
        from routers import tab1 as r1
        self.cola._queue = [_t(qm.TIPO_RIP, "peli_2024_1")]
        previa, r1.queue_manager = r1.queue_manager, self.cola
        self.addCleanup(setattr, r1, "queue_manager", previa)
        r = await r1.cancel_queue_job("rip:peli_2024_1")
        self.assertEqual(r, {"ok": True, "session_id": "peli_2024_1"})


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


class TestTab3PasaPorLaCola(ApiTestCase):
    """El contrato nuevo de Tab 3: encolar en vez de rechazar."""

    def setUp(self):
        super().setUp()
        workload.limpiar()
        self.addCleanup(workload.limpiar)

    def test_la_posicion_en_la_cola_se_ve_en_el_GET(self):
        """Sin esto, encolar una fase deja al usuario mirando un botón que ya
        pulsó: el endpoint responde al instante pero `running_phase` no se pone
        hasta que la cola despacha, que puede ser cuarenta minutos después."""
        sid = self.crear_sesion(sid="cmv40_q", phase="extracted")
        cola = self.main.queue_manager
        cola._running = qm.TrabajoEnCola(
            tab="rip", tipo=qm.TIPO_RIP, clave="rip1", que="rip de Peli (2024)")
        cola._queue = [qm.TrabajoEnCola(
            tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
            que="Fase inject", datos={"fase": "inject"})]
        c = self.client.get(f"/api/cmv40/{sid}").json()["cola"]
        self.assertEqual(c["fase"], "inject")
        self.assertEqual(c["posicion"], 1)
        self.assertEqual(c["por_delante"], "rip de Peli (2024)")

    def test_un_proyecto_que_no_espera_no_trae_cola(self):
        sid = self.crear_sesion(sid="cmv40_q", phase="extracted")
        self.assertIsNone(self.client.get(f"/api/cmv40/{sid}").json()["cola"])

    def test_los_parametros_viajan_con_el_trabajo(self):
        """La cola reconstruye la fase; no reusa la closure del endpoint.

        El campo de la sesión existe como respaldo, pero lo que manda es
        `datos`: es lo único que sigue siendo cierto si la sesión cambió entre
        encolar y despachar, y lo único que sobrevive a un reinicio con la
        cola llena.
        """
        from routers import cmv40 as r
        from storage import load_cmv40_session
        sid = self.crear_sesion(sid="cmv40_q", phase="target_provided")
        session = load_cmv40_session(sid)
        session.pending_target_source_mkv_path = "/mnt/library/VIEJO.mkv"
        coro, _ = r._cmv40_construir_fase(session, "target_rpu_mkv",
                                          {"mkv": "/mnt/library/EL BUENO.mkv"})
        # El path acaba dentro del closure que se acaba de construir.
        capturado = coro.__closure__ and [
            c.cell_contents for c in coro.__closure__
            if isinstance(c.cell_contents, str)]
        self.assertIn("/mnt/library/EL BUENO.mkv", capturado or [],
                      "ganó el campo de la sesión sobre lo que se encoló")

    def test_cancelar_saca_de_la_cola(self):
        """Desde la cola única, «cancelar» tiene dos significados según dónde
        esté el trabajo — y para el usuario es el mismo botón."""
        sid = self.crear_sesion(sid="cmv40_q", phase="extracted")
        cola = self.main.queue_manager
        cola._queue = [qm.TrabajoEnCola(
            tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid,
            que="Fase inject", datos={"fase": "inject"})]
        self.client.post(f"/api/cmv40/{sid}/cancel")
        self.assertEqual(cola._queue, [])


class TestUnTrabajoEnColaNoTapaElPanel(unittest.TestCase):
    """Lo que aquella clase comprobaba, dicho de la forma que hoy aplica.

    Se probaba que `_cmv40ShouldShowOverlay` devolviera `false` con el trabajo
    en cola. Esa función ya no existe: **el overlay entero se retiró** porque
    la premisa era mala —abrir un modal bloqueante por su cuenta— y se le
    fueron añadiendo excepciones sin arreglarlo. Ver
    `test_cmv40_overlay_bloqueante`.
    """

    def test_no_queda_nada_que_tape_el_panel_por_su_cuenta(self):
        from frontend_sources import js_completo
        js = js_completo()
        self.assertNotIn("_cmv40ShouldShowOverlay", js)
        self.assertNotIn("cmv40-running-overlay", js)


class TestTab2PasaPorLaCola(ApiTestCase):
    """Los dos trabajos largos de Tab 2, que eran POST síncronos.

    El análisis extendido son ~10 min y el navegador mantenía el POST abierto
    **hasta una hora**; la copia desde biblioteca son decenas de GB con un
    tope de cuatro horas. Encolarlos obliga a que respondan al instante y a
    que el resultado viaje por el estado del job — que es lo que el modal ya
    polleaba para pintar la barra.
    """

    def setUp(self):
        super().setUp()
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        self.mkv = self.output_dir / "Peli.mkv"
        self.mkv.write_bytes(b"x" * 4096)

    def _encolados(self, tipo):
        return [t for t in self.trabajos_encolados if t[0] == tipo]

    def test_el_analisis_extendido_responde_al_instante(self):
        r = self.client.post("/api/mkv/quality-audit",
                             json={"file_path": str(self.mkv)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["queued"])
        self.assertTrue(r.json()["audit_id"])

    def test_y_el_modal_ve_que_esta_esperando(self):
        """Sin esto el modal se queda con el primer paso en ⏳ sin que nada
        esté pasando todavía."""
        self.client.post("/api/mkv/quality-audit",
                         json={"file_path": str(self.mkv)})
        st = self.client.get("/api/mkv/quality-audit/progress").json()
        self.assertEqual(st["step"], "en_cola")
        self.assertTrue(st["active"])

    def test_el_trabajo_lleva_lo_que_el_runner_necesita(self):
        """La cola puede despachar mucho después: el `body` de Pydantic no se
        persiste, así que lo que haga falta viaja serializado."""
        self.client.post("/api/mkv/quality-audit",
                         json={"file_path": str(self.mkv)})
        _, clave, datos, _ = self._encolados(qm.TIPO_ANALISIS_EXTENDIDO)[0]
        # `resolve()` en los dos lados: en macOS el tmpdir es /var, que es
        # un symlink a /private/var, y el backend resuelve la ruta.
        self.assertEqual(Path(datos["mkv"]).resolve(), self.mkv.resolve())
        self.assertEqual(datos["nombre"], "Peli.mkv")
        self.assertTrue(datos["inicio"])

    def test_cancelar_saca_de_la_cola_el_analisis(self):
        cola = self.main.queue_manager
        self.client.post("/api/mkv/quality-audit",
                         json={"file_path": str(self.mkv)})
        aid = self.client.get("/api/mkv/quality-audit/progress").json()["audit_id"]
        cola._queue = [qm.TrabajoEnCola(tab="mkv",
                                        tipo=qm.TIPO_ANALISIS_EXTENDIDO,
                                        clave=aid, que="análisis")]
        self.client.post("/api/mkv/quality-audit/cancel", json={})
        self.assertEqual(cola._queue, [])

    def test_la_copia_desde_biblioteca_se_encola(self):
        src = self.library_dir / "Desde.mkv"
        src.write_bytes(b"x" * 4096)
        r = self.client.post("/api/mkv/apply",
                             json={"file_path": str(src), "copy_to_output": True,
                                   "audio_tracks": [], "subtitle_tracks": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["queued"])
        _, _, datos, _ = self._encolados(qm.TIPO_COPIA_BIBLIOTECA)[0]
        self.assertEqual(Path(datos["src"]).resolve(), src.resolve())
        self.assertIn("body", datos, "el runner necesita el body serializado")

    def test_pero_una_edicion_SIN_copia_sigue_siendo_instantanea(self):
        """`mkvpropedit` es O(1) y el mismo endpoint hace las dos cosas.
        Encolar una edición de cabeceras detrás de un rip sería absurdo."""
        r = self.client.post("/api/mkv/apply",
                             json={"file_path": str(self.mkv),
                                   "audio_tracks": [], "subtitle_tracks": []})
        # Comprobar solo "no se encoló nada" no basta: si la rama de copia se
        # tragara este caso, el endpoint moriría con un 409 de «ya existe un
        # MKV con ese nombre» —el destino ES el origen— y la aserción se
        # cumpliría por el motivo equivocado. El 500 de «Nothing to do» es
        # correcto aquí: la petición no trae ninguna edición, y lo que importa
        # es que llegó hasta `mkvpropedit`.
        self.assertNotIn("Ya existe un MKV", r.text,
                         "una edición sin copia tomó la rama de copia")
        self.assertEqual(self._encolados(qm.TIPO_COPIA_BIBLIOTECA), [],
                         f"se encoló una edición sin copia: {r.text}")

    def test_ya_no_queda_ningun_409_de_admision_en_tab_2(self):
        src = (APP_DIR / "routers" / "tab2.py").read_text(encoding="utf-8")
        self.assertNotIn("workload.exigir_libre", src)


class TestLaSerieEnteraPasaPorLaCola(ApiTestCase):
    """`create-series-sessions` era el último POST síncrono largo.

    ~30 s de montaje más 15-30 s por episodio: para una temporada de diez,
    cinco minutos largos de disco con un timeout de 10 min en el navegador.
    """

    def setUp(self):
        super().setUp()
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        self.carpeta = self.isos_dir / "Serie (2024)"
        (self.carpeta / "BDMV" / "PLAYLIST").mkdir(parents=True, exist_ok=True)
        (self.carpeta / "BDMV" / "PLAYLIST" / "00801.mpls").write_bytes(b"\0" * 64)
        (self.carpeta / "BDMV" / "STREAM").mkdir(parents=True, exist_ok=True)
        (self.carpeta / "BDMV" / "STREAM" / "00801.m2ts").write_bytes(b"\0" * 4096)

    def _crear(self, episodios=2):
        return self.client.post("/api/create-series-sessions", json={
            "source_type": "bdmv_folder",
            "source_path": "Serie (2024)",
            "series_name": "Serie",
            "season_number": 1,
            "episodes": [{"mpls_path": "00801.mpls", "episode_number": i + 1,
                          "episode_title": f"Ep {i + 1}"} for i in range(episodios)],
        })

    def test_responde_al_instante(self):
        r = self._crear()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["queued"])
        self.assertEqual(r.json()["total"], 2)

    def test_el_modal_ve_que_espera_turno(self):
        self._crear()
        prog = self.client.get("/api/series-create-progress").json()
        self.assertEqual(prog["current_episode_step"], "en_cola")
        self.assertTrue(prog["running"])

    def test_lo_que_el_endpoint_decidio_viaja_con_el_trabajo(self):
        """El diálogo de conflictos (saltar / reemplazar / cancelar) se
        responde en el acto, no cuando la cola llegue al trabajo. Su resultado
        tiene que ir con él: recalcularlo daría otra cosa si el /config cambió
        entretanto."""
        self._crear()
        _, _, datos, _ = [t for t in self.trabajos_encolados
                          if t[0] == qm.TIPO_SERIE][0]
        self.assertEqual(len(datos["episodios"]), 2)
        self.assertIn("skipped_existing", datos)
        self.assertIn("replaced_ids", datos)
        self.assertIn("fingerprint", datos)
        self.assertIn("body", datos, "el runner necesita el body serializado")

    def test_los_episodios_que_se_saltan_NO_viajan(self):
        """La distinción que importa: con `skip_existing`, el trabajo lleva
        los episodios FILTRADOS, no los que pidió el usuario. Mandar
        `body.episodes` volvería a analizar los que ya existen — media hora de
        disco por nada — y machacaría sesiones que el usuario decidió
        conservar.
        """
        import storage
        from models import Session
        from phases.phase_a import find_main_m2ts
        from storage import compute_iso_fingerprint

        fp = compute_iso_fingerprint(find_main_m2ts(str(self.carpeta)))
        storage.save_session(Session(
            id="serie_s01e01", iso_path=str(self.carpeta),
            mkv_name="Serie - S01E01.mkv", status="done",
            iso_fingerprint=fp, media_type="series",
            season_number=1, episode_number=1))

        r = self.client.post("/api/create-series-sessions", json={
            "source_type": "bdmv_folder",
            "source_path": "Serie (2024)",
            "series_name": "Serie",
            "season_number": 1,
            "mode": "skip_existing",
            "episodes": [{"mpls_path": "00801.mpls", "episode_number": i + 1,
                          "episode_title": f"Ep {i + 1}"} for i in range(2)],
        })
        self.assertEqual(r.status_code, 200, r.text)
        _, _, datos, _ = [t for t in self.trabajos_encolados
                          if t[0] == qm.TIPO_SERIE][0]
        self.assertEqual([e["episode_number"] for e in datos["episodios"]], [2],
                         "viajó el episodio que el usuario pidió saltar")
        self.assertEqual(len(datos["skipped_existing"]), 1)

    def test_va_al_final_de_la_cola(self):
        """No es una fase de un proyecto a medias: no tiene por qué colarse."""
        self._crear()
        self.assertFalse([t for t in self.trabajos_encolados
                          if t[0] == qm.TIPO_SERIE][0][3])


class TestElEndpointDeLaColaLoDiceTodo(ApiTestCase):
    """El WS y `GET /api/queue` no pueden discrepar: el dashboard usará el
    segundo y se quedaría sin ver el trabajo de Tab 2 y Tab 3."""

    def test_trae_la_vista_completa_ademas_de_los_campos_de_compat(self):
        cola = self.main.queue_manager
        cola._running = qm.TrabajoEnCola(
            tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave="proj",
            que="Fase C de Predator", datos={"fase": "extract"})
        cola._queue = [qm.TrabajoEnCola(tab="mkv",
                                        tipo=qm.TIPO_ANALISIS_EXTENDIDO,
                                        clave="aud1", que="análisis de X")]
        r = self.client.get("/api/queue").json()
        self.assertEqual(r["running_job"]["que"], "Fase C de Predator")
        self.assertEqual([j["clave"] for j in r["jobs"]], ["aud1"])
        # Y los de siempre siguen siendo solo de Tab 1.
        self.assertIsNone(r["running"])
        self.assertEqual(r["queue"], [])


class TestYaNoQuedaNingun409DeAdmision(unittest.TestCase):
    """El final del bloque 3: la app pasa de «no puedes» a «cuando toque»."""

    def test_en_ninguno_de_los_tres_routers(self):
        for f in ("tab1.py", "tab2.py", "cmv40.py"):
            src = (APP_DIR / "routers" / f).read_text(encoding="utf-8")
            self.assertNotIn("exigir_libre", src, f)

    def test_ni_en_main(self):
        self.assertNotIn("exigir_libre",
                         (APP_DIR / "main.py").read_text(encoding="utf-8"))

    def test_workload_conserva_lo_que_si_se_usa(self):
        """`bloqueado_por` lo consulta la cola para esperar y `hay_contencion`
        protege las calibraciones de `_adaptive_timeout` y el ETA."""
        self.assertTrue(hasattr(workload, "bloqueado_por"))
        self.assertTrue(hasattr(workload, "hay_contencion"))
        self.assertTrue(hasattr(workload, "marca"))
