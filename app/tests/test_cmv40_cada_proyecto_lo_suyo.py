"""Cada proyecto CMv4.0 toca lo suyo: su `.mkv.tmp`, su workdir y su id.

Fallos del router de CMv4.0 que encontró la auditoría completa con Codex del
2026-10-17 (paquete P2), reproducidos antes de arreglarlos:

  · **Dos proyectos con el mismo MKV de salida compartían el `.mkv.tmp`** de
    la Fase G: la G de uno borraba el remux del otro al arrancar, y limpiar,
    borrar o renombrar el que todavía no había llegado al remux se llevaba el
    del que sí. Hoy el temporal es del proyecto que lo escribió.
  · **Crear aceptaba un `output_mkv_name` con ruta** (`../ajeno.mkv`): borrar
    el proyecto o renombrarlo actuaba fuera de /mnt/output.
  · **`artifacts_dir` llegaba tal cual a un `rmtree`**: un JSON editado con
    `/mnt/output` borraba el directorio entero al limpiar.
  · **La limpieza masiva conservaba el defecto que se arregló en la
    individual**: borraba una lista de nombres conocidos, sumaba los bytes
    antes de borrar y archivaba aunque el borrado fallara; y no miraba la
    cola.
  · **Dos creaciones en el mismo segundo con el mismo título y año daban el
    mismo id**: la segunda sobrescribía la primera.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_cada_proyecto_lo_suyo -v
"""
import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402

NOMBRE = "Peli (2024) [CMv4.0].mkv"


class _DosProyectos(ApiTestCase):
    """A ya hizo su remux; B, con el mismo MKV de salida, todavía no."""

    def setUp(self):
        super().setUp()
        self.a = self.crear_sesion("cmv40_A_2024_1", output_mkv_name=NOMBRE,
                                   phase="remuxed")
        self.b = self.crear_sesion("cmv40_B_2024_2", output_mkv_name=NOMBRE,
                                   phase="injected")
        self.tmp = self.output_dir / f"{NOMBRE}.tmp"
        self.tmp.write_bytes(b"remux de A" + b"\x00" * 1024)


class TestElRemuxEsDeQuienLoEscribio(_DosProyectos):

    def test_borrar_b_no_se_lleva_el_remux_de_a(self):
        r = self.client.delete(f"/api/cmv40/{self.b}?clean_artifacts=true")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.tmp.exists(), "borrar B se llevó el remux de A")

    def test_limpiar_b_no_se_lleva_el_remux_de_a(self):
        r = self.client.post(f"/api/cmv40/{self.b}/cleanup")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.tmp.exists(), "limpiar B se llevó el remux de A")

    def test_renombrar_b_no_se_lleva_el_remux_de_a(self):
        r = self.client.post(f"/api/cmv40/{self.b}/rename-output",
                             json={"output_mkv_name": "Otro nombre.mkv"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.tmp.exists(), "renombrar B movió el remux de A")
        self.assertFalse((self.output_dir / "Otro nombre.mkv.tmp").exists())

    def test_borrar_a_si_se_lleva_su_remux(self):
        # El dueño sí lo borra: si no, quedarían decenas de GB huérfanos.
        r = self.client.delete(f"/api/cmv40/{self.a}?clean_artifacts=true")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.tmp.exists())

    def test_renombrar_a_un_nombre_de_otro_proyecto_vivo_es_409(self):
        c = self.crear_sesion("cmv40_C_2024_3", output_mkv_name="Otra.mkv",
                              phase="created")
        r = self.client.post(f"/api/cmv40/{c}/rename-output",
                             json={"output_mkv_name": NOMBRE})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.leer_sesion(c).output_mkv_name, "Otra.mkv")

    def test_el_forward_roll_de_b_no_adopta_el_remux_de_a(self):
        from models import CMv40PhaseRecord
        import storage
        s = self.leer_sesion(self.b)
        s.phase_history = [CMv40PhaseRecord(phase="remux", status="done",
                                            started_at=datetime.now(timezone.utc))]
        storage.save_cmv40_session(s)
        r = self.client.get(f"/api/cmv40/{self.b}?include_log=false")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.leer_sesion(self.b).phase, "injected",
                         "B se ha adelantado a `remuxed` con el remux de A")


class TestLaFaseGDeBNoPisaElRemuxDeA(_DosProyectos):

    def test_la_g_de_b_falla_sin_tocar_el_de_a(self):
        self.ejecutar_fases_de_verdad()
        llamados = self.mockear_runners()
        r = self.cmv40
        s = self.leer_sesion(self.b)
        coro, nueva = r._cmv40_construir_fase(s, "remux", {})
        asyncio.run(r._run_cmv40_phase(s, "remux", coro, nueva))
        fin = self.leer_sesion(self.b)
        self.assertEqual(llamados, [], "la Fase G de B ha corrido sobre el remux de A")
        self.assertEqual(fin.phase_history[-1].status, "error")
        self.assertTrue(self.tmp.exists(), "el fallo de la G de B borró el remux de A")
        self.assertEqual(self.tmp.read_bytes()[:10], b"remux de A")

    def test_sin_otro_dueno_la_g_corre(self):
        self.ejecutar_fases_de_verdad()
        llamados = self.mockear_runners()
        r = self.cmv40
        import storage
        a = self.leer_sesion(self.a)
        a.archived = True
        storage.save_cmv40_session(a)
        s = self.leer_sesion(self.b)
        coro, nueva = r._cmv40_construir_fase(s, "remux", {})
        asyncio.run(r._run_cmv40_phase(s, "remux", coro, nueva))
        self.assertEqual(llamados, ["run_phase_g_remux"])


class TestElNombreDeSalidaNoLlevaRuta(ApiTestCase):

    def setUp(self):
        super().setUp()

        async def _nada(*a, **k):
            return None
        for nombre in ("_cmv40_hydrate_sheet_recommendation", "_cmv40_hydrate_tmdb"):
            orig = getattr(self.cmv40, nombre)
            setattr(self.cmv40, nombre, _nada)
            self.addCleanup(setattr, self.cmv40, nombre, orig)
        self.src = self.tmp / "Peli (2024).mkv"
        self.src.write_bytes(b"x")

    def test_crear_con_ruta_es_400(self):
        ajeno = self.tmp / "ajeno.mkv.tmp"
        ajeno.write_bytes(b"ajeno")
        for nombre in ("../ajeno.mkv", str(self.tmp / "ajeno.mkv"), ".oculto.mkv"):
            r = self.client.post("/api/cmv40/create", json={
                "source_mkv_path": str(self.src), "output_mkv_name": nombre,
                "auto_pipeline": False})
            self.assertEqual(r.status_code, 400, f"{nombre!r} → {r.status_code}")

    def test_un_json_con_ruta_se_normaliza_al_cargar(self):
        from models import CMv40Session
        s = CMv40Session(id="x", source_mkv_path="/a.mkv", source_mkv_name="a.mkv",
                         output_mkv_name="/mnt/tmp/../ajeno.mkv")
        self.assertEqual(s.output_mkv_name, "ajeno.mkv")
        s = CMv40Session(id="x", source_mkv_path="/a.mkv", source_mkv_name="a.mkv",
                         output_mkv_name="Peli (2024).mkv")
        self.assertEqual(s.output_mkv_name, "Peli (2024).mkv")


class TestElWorkdirEsDelProyecto(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.ajeno = self.tmp / "ajeno"
        self.ajeno.mkdir()
        (self.ajeno / "algo.mkv").write_bytes(b"\x00" * 2048)

    def test_limpiar_no_borra_un_artifacts_dir_ajeno(self):
        sid = self.crear_sesion(phase="done", artifacts_dir=str(self.ajeno))
        r = self.client.post(f"/api/cmv40/{sid}/cleanup")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertTrue((self.ajeno / "algo.mkv").exists(), "rmtree fuera del workdir")
        self.assertFalse(self.leer_sesion(sid).archived)

    def test_borrar_no_borra_un_artifacts_dir_ajeno(self):
        sid = self.crear_sesion(phase="done", artifacts_dir=str(self.ajeno))
        r = self.client.delete(f"/api/cmv40/{sid}?clean_artifacts=true")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue((self.ajeno / "algo.mkv").exists(), "rmtree fuera del workdir")

    def test_ni_el_workdir_de_otro_proyecto(self):
        otro = self.work_base / "cmv40_Otro_2024_9"
        otro.mkdir()
        (otro / "BL.hevc").write_bytes(b"\x00" * 2048)
        sid = self.crear_sesion(phase="done", artifacts_dir=str(otro))
        r = self.client.post(f"/api/cmv40/{sid}/cleanup")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertTrue((otro / "BL.hevc").exists())

    def test_el_propio_si_se_borra(self):
        sid = self.crear_sesion(phase="done")
        wd = self.work_base / sid
        (wd / "BL.hevc").write_bytes(b"\x00" * 2048)
        r = self.client.post(f"/api/cmv40/{sid}/cleanup")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(wd.exists())


class TestLaLimpiezaMasivaEsLaIndividual(ApiTestCase):

    def _bulk(self, *ids):
        r = self.client.post("/api/cmv40/cleanup/bulk", json={"session_ids": list(ids)})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_borra_tambien_lo_que_no_esta_en_la_lista(self):
        sid = self.crear_sesion(phase="done")
        wd = self.work_base / sid
        (wd / "artefacto_nuevo_del_pipeline.bin").write_bytes(b"\x00" * 3000)
        res = self._bulk(sid)
        self.assertEqual([d["id"] for d in res["deleted"]], [sid])
        self.assertFalse(wd.exists(), "quedó un artefacto que la lista no conoce")
        self.assertEqual(res["total_freed_bytes"], 3000)

    def test_si_el_borrado_falla_no_archiva_ni_anuncia_bytes(self):
        sid = self.crear_sesion(phase="done")
        (self.work_base / sid / "BL.hevc").write_bytes(b"\x00" * 3000)

        def _falla(*a, **k):
            raise PermissionError("denegado")
        with mock.patch.object(self.cmv40._cmv40_shutil, "rmtree", _falla):
            res = self._bulk(sid)
        self.assertEqual(res["deleted"], [])
        self.assertEqual([f["id"] for f in res["failed"]], [sid])
        self.assertEqual(res["total_freed_bytes"], 0)
        self.assertFalse(self.leer_sesion(sid).archived,
                         "archivado con los artefactos dentro: ya no se puede limpiar")

    def test_una_fase_en_cola_lo_salta(self):
        import queue_manager as qm
        sid = self.crear_sesion(phase="target_provided")
        cola = self.main.queue_manager
        cola._queue.append(qm.TrabajoEnCola(
            tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid, que="x",
            datos={"fase": "extract"}))
        self.addCleanup(cola._queue.clear)
        (self.work_base / sid / "BL.hevc").write_bytes(b"\x00" * 3000)
        res = self._bulk(sid)
        self.assertEqual([s["id"] for s in res["skipped"]], [sid])
        self.assertTrue((self.work_base / sid / "BL.hevc").exists())

    def test_la_individual_tampoco_archiva_si_falla(self):
        sid = self.crear_sesion(phase="done")
        (self.work_base / sid / "BL.hevc").write_bytes(b"\x00" * 3000)

        def _falla(*a, **k):
            raise PermissionError("denegado")
        with mock.patch.object(self.cmv40._cmv40_shutil, "rmtree", _falla):
            r = self.client.post(f"/api/cmv40/{sid}/cleanup")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertFalse(self.leer_sesion(sid).archived)


class TestDosCreacionesEnElMismoSegundo(ApiTestCase):

    def setUp(self):
        super().setUp()

        async def _nada(*a, **k):
            return None
        for nombre in ("_cmv40_hydrate_sheet_recommendation", "_cmv40_hydrate_tmdb"):
            orig = getattr(self.cmv40, nombre)
            setattr(self.cmv40, nombre, _nada)
            self.addCleanup(setattr, self.cmv40, nombre, orig)
        import storage

        class _Reloj(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
        p = mock.patch.object(storage, "datetime", _Reloj)
        p.start()
        self.addCleanup(p.stop)

    def _crear(self, nombre):
        src = self.tmp / nombre
        if not src.exists():
            src.write_bytes(b"x")
        r = self.client.post("/api/cmv40/create", json={
            "source_mkv_path": str(src), "auto_pipeline": False})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def test_dos_ediciones_son_dos_proyectos(self):
        a = self._crear("Peli (2024) [Edición A].mkv")
        b = self._crear("Peli (2024) [Edición B].mkv")
        self.assertNotEqual(a, b, "la segunda creación sobrescribió la primera")
        self.assertEqual(self.leer_sesion(a).source_mkv_name, "Peli (2024) [Edición A].mkv")
        self.assertEqual(self.leer_sesion(b).source_mkv_name, "Peli (2024) [Edición B].mkv")

    def test_el_doble_clic_no_crea_dos(self):
        a = self._crear("Peli (2024).mkv")
        b = self._crear("Peli (2024).mkv")
        self.assertEqual(a, b)
        self.assertEqual(len(list(self.cmv40_dir.glob("cmv40_*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
