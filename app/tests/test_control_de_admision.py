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

    def test_un_segundo_proyecto_da_409(self):
        """El lock de fases es por `session_id`, así que antes corrían N a la
        vez. Es el caso que más daño hacía: dos `dovi_tool` a la vez."""
        a = self.crear_sesion(sid="cmv40_a", phase="extracted")
        b = self.crear_sesion(sid="cmv40_b", phase="extracted")
        workload.registrar(a, workload.TAB_CMV40, "inject de A")
        r = self.client.post(f"/api/cmv40/{b}/inject")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("CMv4.0", r.json()["detail"])
        self.assertEqual(self.fases_lanzadas, [])

    def test_el_mismo_proyecto_encadena_sus_fases(self):
        """Sin esto el auto-pipeline se detendría solo tras la primera fase."""
        a = self.crear_sesion(sid="cmv40_a", phase="extracted")
        workload.registrar(a, workload.TAB_CMV40, "extract de A")
        r = self.client.post(f"/api/cmv40/{a}/inject")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.fase_lanzada()["phase"], "inject")

    def test_los_nueve_endpoints_de_fase_lo_aplican(self):
        """Igual que el guard de error pendiente: si uno se queda sin él, ese
        es el hueco por el que se solapan dos jobs."""
        import re
        src = (APP_DIR / "routers" / "cmv40.py").read_text(encoding="utf-8")
        n = len(re.findall(r"_cmv40_guard_sin_trabajo_pesado\(session\)", src))
        self.assertGreaterEqual(
            n, 9, f"solo {n} sitios con el guard; deberían ser los 9 de fase "
                  "más los dos pre-flight")

    def test_con_la_casa_libre_arranca(self):
        a = self.crear_sesion(sid="cmv40_a", phase="extracted")
        r = self.client.post(f"/api/cmv40/{a}/inject")
        self.assertEqual(r.status_code, 200, r.text)


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


class TestLaColaEspera(unittest.IsolatedAsyncioTestCase):
    """La cola de Tab 1 espera en vez de fallar un trabajo ya encolado."""

    def setUp(self):
        workload.limpiar()
        self.addCleanup(workload.limpiar)

    async def test_no_arranca_mientras_haya_trabajo_pesado(self):
        import asyncio

        from queue_manager import QueueManager
        cola = QueueManager()
        cola._persist_state = lambda: None
        arrancados = []

        async def _run(sid):
            arrancados.append(sid)

        cola.set_run_fn(_run)
        workload.registrar("otro", workload.TAB_MKV, "análisis extendido")
        cola._queue.append("job1")
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


if __name__ == "__main__":
    unittest.main()
