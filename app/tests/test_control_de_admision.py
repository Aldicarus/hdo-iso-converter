"""No se solapa trabajo pesado entre pestañas.

Cada pestaña serializaba lo suyo y ninguna sabía de las otras: Tab 1 con su cola
FIFO de uno, Tab 2 con un análisis y una copia, y Tab 3 bloqueando **por
`session_id`** — así que N proyectos podían correr fases a la vez. Sumado: tres
o más procesos pesados peleándose por 4 núcleos y un solo pool ZFS.

Lo evidente es que todo va más lento. Lo que no se ve es peor: **
`_adaptive_timeout` y el modelo de ETA se anclan en `ffmpeg_wall_seconds`**, así
que una medición tomada con contención envenena en silencio las dos
calibraciones de las que depende todo el progreso medido — y un timeout
calculado a partir de ella puede quedarse corto en el job siguiente.

La política elegida es **rechazar con 409 diciendo qué bloquea**. Dos matices
que este test fija porque son los que pueden romper el uso normal:

  · un proyecto de Tab 3 que avanza a su fase siguiente **no se bloquea a sí
    mismo** (es el mismo job, no uno nuevo) — si no, el auto-pipeline se
    detendría solo;
  · la **cola de Tab 1 espera**, no falla: fallar un trabajo ya encolado por
    algo que el usuario hizo después sería gratuito.

Y lo que a propósito NO se bloquea: abrir un MKV en Tab 2 (`/api/mkv/analyze`).
Es cómo se navega, está acotado, y bloquearlo dejaría la pestaña inservible
mientras corre un rip.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_control_de_admision -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
import workload  # noqa: E402


class TestElRegistro(unittest.TestCase):
    """`workload`: función pura sobre un dict en memoria."""

    def setUp(self):
        workload.limpiar()
        self.addCleanup(workload.limpiar)

    def test_vacio_no_bloquea(self):
        self.assertIsNone(workload.bloqueado_por())
        self.assertIsNone(workload.motivo_409())
        self.assertFalse(workload.hay_contencion())

    def test_un_trabajo_bloquea_a_los_demas(self):
        workload.registrar("s1", workload.TAB_RIP, "rip de Peli")
        self.assertIsNotNone(workload.bloqueado_por())
        self.assertIsNotNone(workload.bloqueado_por("otro"))

    def test_nadie_se_bloquea_a_si_mismo(self):
        """Lo que permite que el auto-pipeline de Tab 3 encadene fases."""
        workload.registrar("s1", workload.TAB_CMV40, "inject de Peli")
        self.assertIsNone(workload.bloqueado_por("s1"))

    def test_liberar_desbloquea(self):
        workload.registrar("s1", workload.TAB_RIP, "rip")
        workload.liberar("s1")
        self.assertIsNone(workload.bloqueado_por())

    def test_liberar_algo_que_no_estaba_no_revienta(self):
        workload.liberar("no_existe")

    def test_el_motivo_dice_qué_bloquea_y_dónde(self):
        workload.registrar("s1", workload.TAB_RIP, "rip de Peli (2024)")
        motivo = workload.motivo_409("otro")
        self.assertIn("Blu-Ray ISO", motivo, motivo)
        self.assertIn("rip de Peli (2024)", motivo)

    def test_exigir_libre_lanza_409(self):
        from fastapi import HTTPException
        workload.registrar("s1", workload.TAB_MKV, "análisis extendido")
        with self.assertRaises(HTTPException) as ctx:
            workload.exigir_libre()
        self.assertEqual(ctx.exception.status_code, 409)
        workload.exigir_libre("s1")      # el propio, no lanza


class AdmisionApiCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        workload.limpiar()
        self.addCleanup(workload.limpiar)


class TestTab1(AdmisionApiCase):

    def _sesion_ejecutable(self):
        (self.isos_dir / "Peli (2024).iso").write_bytes(b"x" * 4096)
        return self.crear_sesion_tab1()

    def test_encolar_con_otra_pestana_ocupada_da_409(self):
        sid = self._sesion_ejecutable()
        workload.registrar("otro", workload.TAB_MKV, "análisis extendido de X")
        r = self.client.post(f"/api/sessions/{sid}/execute")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("Consultar / Editar MKV", r.json()["detail"])
        self.assertEqual(self.encolados, [], "no debe haberse encolado")

    def test_encolar_con_la_casa_libre_funciona(self):
        sid = self._sesion_ejecutable()
        r = self.client.post(f"/api/sessions/{sid}/execute")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.encolados, [sid])

    def test_su_propio_trabajo_no_lo_bloquea(self):
        """Re-lanzar la MISMA sesión no debe chocar con su propio hueco."""
        sid = self._sesion_ejecutable()
        workload.registrar(sid, workload.TAB_RIP, "rip de Peli (2024)")
        r = self.client.post(f"/api/sessions/{sid}/execute")
        self.assertEqual(r.status_code, 200, r.text)


class TestTab2(AdmisionApiCase):

    def test_el_analisis_extendido_con_otra_pestana_ocupada_da_409(self):
        mkv = self.output_dir / "Peli.mkv"
        mkv.write_bytes(b"x" * 4096)
        workload.registrar("otro", workload.TAB_RIP, "rip de Otra (2024)")
        r = self.client.post("/api/mkv/quality-audit",
                             json={"file_path": str(mkv)})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("Blu-Ray ISO", r.json()["detail"])

    def test_abrir_un_mkv_NO_se_bloquea(self):
        """Es cómo se navega, no un job: bloquearlo dejaría Tab 2 inservible
        mientras corre un rip."""
        mkv = self.output_dir / "Peli.mkv"
        mkv.write_bytes(b"x" * 4096)
        workload.registrar("otro", workload.TAB_RIP, "rip de Otra (2024)")
        r = self.client.post("/api/mkv/analyze", json={"file_path": str(mkv)})
        self.assertNotEqual(r.status_code, 409, r.text)


class TestTab3(AdmisionApiCase):
    """Desde la cola única, Tab 3 **encola** en vez de rechazar con 409.

    Antes el lock de fases era por `session_id`, así que N proyectos podían
    correr a la vez —el caso que más daño hacía: dos `dovi_tool` peleándose—
    y el guard lo cortaba con un 409. Rechazar era mejor que solaparse, pero
    peor que esperar: el usuario tenía que acordarse de volver.
    """

    def test_un_segundo_proyecto_se_encola_en_vez_de_fallar(self):
        a = self.crear_sesion(sid="cmv40_a", phase="extracted")
        b = self.crear_sesion(sid="cmv40_b", phase="extracted")
        workload.registrar(a, workload.TAB_CMV40, "inject de A")
        r = self.client.post(f"/api/cmv40/{b}/inject")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.fase_encolada()["clave"], b)
        self.assertEqual(self.fases_lanzadas, [],
                         "encolar no puede ejecutar en el acto")

    def test_el_mismo_proyecto_encadena_sus_fases(self):
        """Sin esto el auto-pipeline se detendría solo tras la primera fase."""
        a = self.crear_sesion(sid="cmv40_a", phase="extracted")
        workload.registrar(a, workload.TAB_CMV40, "extract de A")
        r = self.client.post(f"/api/cmv40/{a}/inject")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.fase_encolada()["fase"], "inject")

    def test_la_fase_siguiente_va_a_la_cabeza(self):
        """Un proyecto a medias tiene 250-400 GB de artefactos ocupando
        /mnt/tmp: dejarlo detrás de dos rips de 40 min es peor que
        terminarlo."""
        a = self.crear_sesion(sid="cmv40_a", phase="extracted")
        self.client.post(f"/api/cmv40/{a}/inject")
        self.assertTrue(self.fase_encolada()["a_la_cabeza"])

    def test_pero_la_primera_fase_de_un_proyecto_nuevo_no_se_cuela(self):
        """Ahí todavía no ha gastado nada."""
        a = self.crear_sesion(sid="cmv40_a", phase="created")
        self.client.post(f"/api/cmv40/{a}/analyze-source")
        self.assertFalse(self.fase_encolada()["a_la_cabeza"])

    def test_las_fases_pesadas_encolan_y_las_de_segundos_no(self):
        """`target_rpu_path` y `target_rpu_drive` tienen mediana de 2 s y 3 s
        medidos sobre los proyectos del NAS. Encolar una descarga de tres
        segundos detrás de un rip de 40 minutos no protegería nada."""
        import queue_manager as _qm
        from routers import cmv40 as r
        self.assertEqual(
            r._CMV40_FASES_DIFERIDAS,
            {"analyze_source", "extract", "correct_sync", "inject", "remux",
             "validate", "target_rpu_mkv"})

    def test_solo_los_pre_flight_conservan_el_409(self):
        """No pasan por la cola todavía, así que siguen rechazando."""
        import re
        src = (APP_DIR / "routers" / "cmv40.py").read_text(encoding="utf-8")
        n = len(re.findall(r"_cmv40_guard_sin_trabajo_pesado\(session\)", src))
        self.assertEqual(n, 2, f"{n} sitios con el guard; se esperaban los dos "
                               "pre-flight y nada más")

    def test_con_la_casa_libre_tambien_encola(self):
        """La cola es el camino único: no hay una vía rápida que se salte el
        turno cuando parece que no hay nadie. Esa vía sería una carrera."""
        a = self.crear_sesion(sid="cmv40_a", phase="extracted")
        r = self.client.post(f"/api/cmv40/{a}/inject")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.fase_encolada()["fase"], "inject")


class TestElEndpointDeActividad(AdmisionApiCase):

    def test_vacio(self):
        d = self.client.get("/api/activity").json()
        self.assertFalse(d["ocupado"])
        self.assertEqual(d["trabajos"], [])

    def test_dice_qué_hay_y_desde_cuándo(self):
        workload.registrar("s1", workload.TAB_RIP, "rip de Peli (2024)")
        d = self.client.get("/api/activity").json()
        self.assertTrue(d["ocupado"])
        self.assertEqual(len(d["trabajos"]), 1)
        t = d["trabajos"][0]
        self.assertEqual(t["clave"], "s1")
        self.assertIn("Blu-Ray ISO", t["tab"])
        self.assertIn("rip de Peli (2024)", t["descripcion"])
        self.assertGreaterEqual(t["segundos"], 0)

    def test_lleva_un_id_de_pestaña_estable_para_comparar(self):
        """La etiqueta con emoji es para leerla; comparar contra ella ata la UI
        a un texto que existe justamente para poder cambiarse."""
        workload.registrar("s1", workload.TAB_MKV, "análisis extendido")
        workload.registrar("s2", workload.TAB_CMV40, "Fase A")
        ids = {t["tab_id"] for t in self.client.get("/api/activity").json()["trabajos"]}
        self.assertEqual(ids, {"mkv", "cmv40"})

    def test_el_analisis_extendido_de_tab_2_sale_en_actividad(self):
        """Es el trabajo más largo de esa pestaña y era el único invisible: el
        punto verde solo miraba la copia desde biblioteca."""
        workload.registrar("audit-1", workload.TAB_MKV,
                           "análisis extendido de Peli (2024).mkv")
        trabajos = self.client.get("/api/activity").json()["trabajos"]
        self.assertEqual([t["tab_id"] for t in trabajos], ["mkv"])


class TestLaColaEspera(unittest.IsolatedAsyncioTestCase):
    """La cola de Tab 1 espera en vez de fallar un trabajo ya encolado."""

    def setUp(self):
        workload.limpiar()
        self.addCleanup(workload.limpiar)

    async def test_no_arranca_mientras_haya_trabajo_pesado(self):
        import asyncio

        from queue_manager import TIPO_RIP, QueueManager, TrabajoEnCola
        cola = QueueManager()
        cola._persist_state = lambda: None
        arrancados = []

        async def _run(sid):
            arrancados.append(sid)

        cola.set_run_fn(_run)
        workload.registrar("otro", workload.TAB_MKV, "análisis extendido")
        # La cola guarda trabajos tipados desde que es única.
        cola._queue.append(TrabajoEnCola(tab="rip", tipo=TIPO_RIP,
                                         clave="job1", que="rip de job1"))
        tarea = asyncio.create_task(cola._process())
        await asyncio.sleep(0.05)
        self.assertEqual(arrancados, [], "ha arrancado con la casa ocupada")
        # Al liberar, la cola sigue: no ha fallado el trabajo, estaba esperando.
        workload.liberar("otro")
        try:
            await asyncio.wait_for(tarea, timeout=8)
        except asyncio.TimeoutError:
            tarea.cancel()
            self.fail("la cola no reanudó al liberarse el hueco")
        self.assertEqual(arrancados, ["job1"])
        self.assertEqual(cola._queue, [], "el trabajo no se ha perdido")


class TestElHuecoDuraLoQueDuraLaFase(unittest.IsolatedAsyncioTestCase):
    """Un disparo duplicado no puede soltar el hueco de la fase que corre.

    El hueco se ocupaba en `_cmv40_launch_phase`, **antes** de los guards. Como
    el auto-pipeline tiene dos disparadores —backend y frontend— y el duplicado
    es sistemático, pasaba esto: el segundo disparo hacía `registrar` sobre la
    misma clave, rebotaba en el guard de in-flight y su `finally` llamaba a
    `liberar`. El hueco quedaba libre **con la fase real todavía corriendo**, y
    a partir de ahí otra pestaña podía arrancar trabajo pesado y
    `hay_contencion()` mentía — envenenando `_adaptive_timeout` y el modelo de
    ETA, que es justo lo que este registro existe para impedir.

    Visto en producción el 2026-09-04 con Predator Badlands: «⏭ Fase inject
    ignorada — ya hay otra fase (extract) en curso», y ese inject soltó el hueco.
    """

    def setUp(self):
        import shutil, tempfile
        import storage
        from models import CMv40Session

        workload.limpiar()
        self.addCleanup(workload.limpiar)
        self.tmp = Path(tempfile.mkdtemp(prefix="hueco_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cmv40_dir = self.tmp / "cmv40"
        cmv40_dir.mkdir(parents=True)
        self._orig = (storage.CONFIG_DIR, storage.CMV40_DIR)
        storage.CONFIG_DIR, storage.CMV40_DIR = self.tmp, cmv40_dir
        self.addCleanup(
            lambda: setattr(storage, "CMV40_DIR", self._orig[1]))
        self.addCleanup(
            lambda: setattr(storage, "CONFIG_DIR", self._orig[0]))

        self.session = CMv40Session(
            id="cmv40_hueco", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="y.mkv", artifacts_dir=str(self.tmp),
            phase="extracted",
        )
        storage.save_cmv40_session(self.session)

    async def test_el_duplicado_no_suelta_el_hueco(self):
        import asyncio
        from routers import cmv40 as r

        arrancada, soltar = asyncio.Event(), asyncio.Event()

        def _factory(log_cb, proc_cb):
            async def _fase():
                arrancada.set()
                await soltar.wait()
            return _fase()

        real = asyncio.create_task(
            r._run_cmv40_phase(self.session, "inject", _factory, "injected"))
        await asyncio.wait_for(arrancada.wait(), timeout=5)
        self.assertIsNotNone(
            workload.bloqueado_por(),
            "la fase que corre de verdad debería tener el hueco ocupado")

        # El disparo duplicado: rebota en el guard de in-flight y retorna.
        await r._run_cmv40_phase(self.session, "inject", _factory, "injected")
        self.assertIsNotNone(
            workload.bloqueado_por(),
            "el disparo duplicado soltó el hueco de la fase que sigue corriendo")

        soltar.set()
        await asyncio.wait_for(real, timeout=5)
        self.assertIsNone(workload.bloqueado_por(),
                          "al terminar la fase el hueco tiene que quedar libre")

    async def test_una_fase_que_falla_suelta_el_hueco(self):
        """La garantía es el `finally`, no el camino feliz.

        Si una fase que revienta se queda con el hueco, la aplicación entera
        contesta 409 «ya hay trabajo pesado en curso» hasta que se reinicie el
        contenedor — y sin una sola línea que lo explique, porque el error de
        la fase sí se registra pero el hueco no aparece en ninguna parte.
        """
        import asyncio
        from routers import cmv40 as r

        def _factory(log_cb, proc_cb):
            async def _fase():
                raise RuntimeError("dovi_tool se cayó")
            return _fase()

        await asyncio.wait_for(
            r._run_cmv40_phase(self.session, "inject", _factory, "injected"),
            timeout=5)
        self.assertIsNone(
            workload.bloqueado_por(),
            "una fase que falla tiene que soltar el hueco igual que una que "
            "termina bien")

    async def test_una_fase_cancelada_suelta_el_hueco(self):
        """`cmv40_cancel` libera `running_phase` pero NO toca el registro, a
        propósito: el hueco se suelta por la clave propia en el `finally` de la
        tarea que lo tomó. Si esa cadena se rompe, cancelar deja la casa
        ocupada para siempre — y cancelar es justo lo que hace el usuario
        cuando algo va mal."""
        import asyncio
        import storage
        from routers import cmv40 as r
        from phases.cmv40_pipeline import CMv40Cancelled

        def _factory(log_cb, proc_cb):
            async def _fase():
                raise CMv40Cancelled()
            return _fase()

        await asyncio.wait_for(
            r._run_cmv40_phase(self.session, "inject", _factory, "injected"),
            timeout=5)
        self.assertIsNone(workload.bloqueado_por(),
                          "cancelar dejó el hueco ocupado")
        s = storage.load_cmv40_session(self.session.id)
        self.assertIsNone(s.running_phase, "y la UI se quedaría bloqueada")

    async def test_el_lanzador_ya_no_toca_el_registro(self):
        """El `registrar`/`liberar` vive donde está el trabajo, no en el
        wrapper que solo hace `create_task`."""
        import inspect
        from routers import cmv40 as r
        src = inspect.getsource(r._cmv40_launch_phase)
        self.assertNotIn("workload.registrar", src)
        self.assertNotIn("workload.liberar", src)


if __name__ == "__main__":
    unittest.main()
