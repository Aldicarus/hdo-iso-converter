"""Los endpoints de Tab 1 y Tab 2, por HTTP.

`api_harness` existía desde el refactor de CMv4.0 pero lo usaba **un solo**
fichero de test: los 51 endpoints de `main.py` —todo Tab 1 y Tab 2— no tenían
ninguna prueba de request/response. Nadie comprobaba qué status devuelven, qué
guards aplican ni qué se persiste, lo que los hace intocables: mover uno de
sitio es a ciegas.

Se cubren los contratos que de verdad importan, no los 51 por rellenar:

  · **path traversal** en el file browser y en la resolución de MKVs — son los
    endpoints que reciben una ruta del cliente;
  · **el whitelist del borrado de huérfanos**, que tiene un `rmtree` detrás;
  · **`PUT /api/sessions/{id}`**: `has_fel` y `audio_dcp` ya NO se aceptan, son
    resultado del análisis. Aceptarlos permitía renombrar un disco MEL como FEL;
  · **los secretos nunca salen en crudo** de `/api/settings`;
  · los 404 y los guards de la cola.

Los endpoints que salen a la red (TMDb, Google, GitHub) se quedan fuera a
propósito: un test que dependa de internet no es un test.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_endpoints_tab1_tab2 -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402


class TestSalud(ApiTestCase):

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_status(self):
        d = self.client.get("/api/status").json()
        self.assertIn("mount_available", d)
        self.assertIn("dev_mode", d)

    def test_version(self):
        d = self.client.get("/api/version").json()
        for campo in ("version", "commit", "is_tagged", "is_dirty", "is_dev"):
            self.assertIn(campo, d)


class TestSesiones(ApiTestCase):

    def test_listado_vacio(self):
        self.assertEqual(self.client.get("/api/sessions").json()["sessions"], [])

    def test_listado_y_detalle(self):
        sid = self.crear_sesion_tab1()
        listado = self.client.get("/api/sessions").json()["sessions"]
        self.assertEqual([s["id"] for s in listado], [sid])
        d = self.client.get(f"/api/sessions/{sid}").json()
        self.assertEqual(d["id"], sid)
        self.assertEqual(d["mkv_name"], "Peli (2024).mkv")

    def test_una_sesion_que_no_existe_da_404(self):
        self.assertEqual(self.client.get("/api/sessions/no_existe").status_code, 404)
        self.assertEqual(self.client.delete("/api/sessions/no_existe").status_code, 404)
        self.assertEqual(
            self.client.put("/api/sessions/no_existe", json={}).status_code, 404)

    def test_borrar_una_sesion(self):
        sid = self.crear_sesion_tab1()
        self.assertEqual(self.client.delete(f"/api/sessions/{sid}").status_code, 200)
        self.assertIsNone(self.leer_sesion_tab1(sid))

    def test_el_sidebar_no_arrastra_los_campos_pesados(self):
        """El listado es un summary: `output_log` y `bdinfo_result` se vacían.
        Con muchos proyectos eran decenas de MB por llamada."""
        sid = self.crear_sesion_tab1(output_log=["línea"] * 500)
        fila = self.client.get("/api/sessions").json()["sessions"][0]
        self.assertEqual(fila["output_log"], [])
        self.assertIsNone(fila["bdinfo_result"])
        # El detalle sí lo trae.
        self.assertEqual(len(self.client.get(f"/api/sessions/{sid}").json()["output_log"]), 500)

    def test_el_put_edita_el_nombre_del_mkv(self):
        sid = self.crear_sesion_tab1()
        r = self.client.put(f"/api/sessions/{sid}",
                            json={"mkv_name": "Otro nombre.mkv",
                                  "mkv_name_manual": True})
        self.assertEqual(r.status_code, 200, r.text)
        s = self.leer_sesion_tab1(sid)
        self.assertEqual(s.mkv_name, "Otro nombre.mkv")
        self.assertTrue(s.mkv_name_manual)

    def test_el_put_NO_acepta_has_fel_ni_audio_dcp(self):
        """Eran toggles de la UI y confundían: no cambiaban el contenido del
        MKV, solo los tags del nombre, así que se podía renombrar un disco MEL
        como FEL. Ahora son resultado del análisis (dovi_tool / nombre del ISO)
        y el escape es editar el nombre, que sigue siendo editable."""
        sid = self.crear_sesion_tab1(has_fel=False)
        r = self.client.put(f"/api/sessions/{sid}",
                            json={"has_fel": True, "audio_dcp": True})
        # FastAPI rechaza los campos desconocidos del body (422) o los ignora;
        # lo que NO puede pasar es que se apliquen.
        self.assertFalse(self.leer_sesion_tab1(sid).has_fel,
                         "has_fel se ha podido cambiar desde la API")


class TestCola(ApiTestCase):

    def test_estado_vacio(self):
        d = self.client.get("/api/queue").json()
        self.assertIsNone(d["running"])
        self.assertEqual(d["queue"], [])

    def test_encolar_una_sesion(self):
        # `execute` comprueba que el origen sigue estando antes de encolar.
        (self.isos_dir / "Peli (2024).iso").write_bytes(b"x" * 4096)
        sid = self.crear_sesion_tab1()
        r = self.client.post(f"/api/sessions/{sid}/execute")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.encolados, [sid],
                         "el endpoint debe encolar exactamente esa sesión")

    def test_encolar_algo_que_no_existe_da_404(self):
        self.assertEqual(
            self.client.post("/api/sessions/no_existe/execute").status_code, 404)
        self.assertEqual(self.encolados, [])

    def test_cancelar_un_trabajo_que_no_esta_en_cola(self):
        r = self.client.delete("/api/queue/no_esta")
        self.assertIn(r.status_code, (200, 404), r.text)


class TestAdmisionDeLaCola(ApiTestCase):
    """Encolar en Tab 1 con trabajo pesado en curso.

    `workload` serializa el trabajo entre pestañas —4 núcleos y un pool de
    discos— y el endpoint de ejecución lo comprobaba **antes de encolar**, sin
    mirar de qué pestaña venía el bloqueo. Resultado: lanzar tres ISOs seguidos
    (el flujo normal, uno corriendo y el resto esperando en FIFO) daba 409 en
    el segundo, que es tanto como decir que no puedes usar la cola mientras la
    cola trabaja. Un rip no puede bloquear a otro rip: para eso está la cola.

    Lo que sí tiene que seguir bloqueando es el trabajo de OTRA pestaña, que es
    para lo que se escribió `workload`.
    """

    def setUp(self):
        super().setUp()
        import workload
        self.workload = workload
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        (self.isos_dir / "Peli (2024).iso").write_bytes(b"x" * 4096)

    def _ejecutar(self, sid):
        return self.client.post(f"/api/sessions/{sid}/execute")

    def test_un_rip_en_curso_no_impide_encolar_el_siguiente(self):
        self.workload.registrar("otro_rip", self.workload.TAB_RIP,
                                "rip de Otra peli (2024)")
        sid = self.crear_sesion_tab1()
        r = self._ejecutar(sid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.encolados, [sid])

    def test_se_pueden_encolar_varios_con_uno_corriendo(self):
        """El caso del usuario: uno en ejecución y el resto en espera."""
        self.workload.registrar("rip_en_curso", self.workload.TAB_RIP,
                                "rip de La primera (2024)")
        ids = [self.crear_sesion_tab1(f"Peli{i}_2024_170000000{i}")
               for i in range(3)]
        for sid in ids:
            self.assertEqual(self._ejecutar(sid).status_code, 200)
        self.assertEqual(self.encolados, ids)

    def test_un_job_de_tab_3_ya_no_bloquea_encolar(self):
        """Encolar detrás de otra pestaña es lo que hace la cola única. El
        409 que había aquí fue el último de admisión de la aplicación."""
        self.workload.registrar("cmv40_x", self.workload.TAB_CMV40,
                                "Fase A de Peli (2024)")
        sid = self.crear_sesion_tab1()
        r = self._ejecutar(sid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.encolados, [sid])

    def test_ni_una_copia_de_tab_2(self):
        self.workload.registrar("mkv_apply", self.workload.TAB_MKV,
                                "copia de Peli (2024).mkv")
        sid = self.crear_sesion_tab1()
        self.assertEqual(self._ejecutar(sid).status_code, 200)
        self.assertEqual(self.encolados, [sid])

    def test_la_cola_no_se_espera_a_si_misma(self):
        """El bucle de espera de `_process` contaba el rip de la propia cola
        como bloqueo ajeno, así que daba vueltas esperándose."""
        self.workload.registrar("rip_en_curso", self.workload.TAB_RIP, "rip")
        self.assertIsNone(
            self.workload.bloqueado_por(ignorar_tab=self.workload.TAB_RIP))
        self.workload.registrar("cmv40_x", self.workload.TAB_CMV40, "Fase A")
        self.assertIsNotNone(
            self.workload.bloqueado_por(ignorar_tab=self.workload.TAB_RIP),
            "un job de otra pestaña sí tiene que hacerla esperar")


class TestFileBrowserYRutas(ApiTestCase):
    """Los endpoints que reciben una ruta del cliente."""

    def test_lista_los_roots_configurados(self):
        (self.library_dir / "Peli.mkv").write_bytes(b"x")
        r = self.client.get("/api/library/browse", params={"root": "library"})
        self.assertEqual(r.status_code, 200, r.text)
        nombres = [e["name"] for e in r.json().get("entries", [])]
        self.assertIn("Peli.mkv", nombres)

    def test_un_root_desconocido_da_400(self):
        self.assertEqual(
            self.client.get("/api/library/browse",
                            params={"root": "inventado"}).status_code, 400)

    def test_no_se_puede_salir_del_root(self):
        """Path traversal. El browser recibe la ruta del cliente."""
        for intento in ("../", "../../etc", "subdir/../../..",
                        "../../../../../../etc/passwd"):
            with self.subTest(intento=intento):
                r = self.client.get("/api/library/browse",
                                    params={"root": "library", "path": intento})
                self.assertEqual(r.status_code, 400, f"{intento} → {r.status_code}")

    def test_analizar_un_mkv_de_fuera_de_los_roots_da_400(self):
        r = self.client.post("/api/mkv/analyze",
                             json={"file_path": "/etc/passwd"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_analizar_sin_ruta_da_400(self):
        self.assertEqual(
            self.client.post("/api/mkv/analyze", json={}).status_code, 400)

    def test_un_mkv_que_no_existe_da_400(self):
        r = self.client.post("/api/mkv/analyze",
                             json={"file_path": str(self.output_dir / "no.mkv")})
        self.assertEqual(r.status_code, 400, r.text)


class TestMkvVarios(ApiTestCase):

    def test_lista_los_mkv_del_output(self):
        (self.output_dir / "Uno.mkv").write_bytes(b"x")
        (self.output_dir / "no_soy_mkv.txt").write_bytes(b"x")
        d = self.client.get("/api/mkv/files").json()
        self.assertIn("Uno.mkv", d["files"])
        self.assertNotIn("no_soy_mkv.txt", d["files"])

    def test_el_progreso_del_apply_esta_inactivo(self):
        d = self.client.get("/api/mkv/apply/progress").json()
        self.assertFalse(d["active"])

    def test_cache_info_sin_ruta_da_400(self):
        self.assertEqual(self.client.get("/api/mkv/cache-info").status_code, 400)

    def test_cache_info_de_un_mkv_sin_cache(self):
        mkv = self.output_dir / "Sin cache.mkv"
        mkv.write_bytes(b"x" * 2048)
        r = self.client.get("/api/mkv/cache-info", params={"file_path": str(mkv)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json().get("cached"))


class TestSettings(ApiTestCase):

    def test_nunca_devuelve_el_secreto_en_crudo(self):
        """Solo `{configured, source, last4}`. Un endpoint que devolviera la
        key la expondría a cualquiera con acceso a la LAN."""
        self.client.post("/api/settings", json={"tmdb_api_key": "supersecreta12345"})
        crudo = self.client.get("/api/settings").text
        self.assertNotIn("supersecreta12345", crudo)
        d = self.client.get("/api/settings").json()
        self.assertTrue(d["tmdb"]["configured"])
        self.assertIn("last4", d["tmdb"])

    def test_persiste_y_se_puede_borrar(self):
        self.client.post("/api/settings", json={"tmdb_api_key": "abcd1234"})
        self.assertTrue(self.client.get("/api/settings").json()["tmdb"]["configured"])
        # "" = borrar / restaurar al valor del entorno.
        self.client.post("/api/settings", json={"tmdb_api_key": ""})
        d = self.client.get("/api/settings").json()
        self.assertIn(d["tmdb"]["source"], ("env", None, "", "none"))


class TestLimpieza(ApiTestCase):
    """El endpoint con un `rmtree` detrás."""

    def test_el_scan_no_borra_nada(self):
        r = self.client.get("/api/cleanup/scan")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("items", r.json())

    def test_no_se_puede_borrar_fuera_de_los_roots(self):
        victima = self.library_dir / "Peliculas"
        victima.mkdir()
        (victima / "no_me_borres.mkv").write_bytes(b"peli")
        # El `..` sale de una base REAL del whitelist (`work_base` es la que
        # el harness pone como CMV40_WORK_BASE): con una base inventada el
        # payload se rechaza por no encajar en ningún prefijo y el test pasaría
        # sin ejercitar la normalización. Visto por mutación.
        for intento in (str(victima),
                        str(self.work_base / ".." / "library" / "Peliculas"),
                        str(self.work_base),          # el propio root, tampoco
                        "/etc",
                        str(self.output_dir / "Peli.mkv")):   # solo .mkv.tmp
            with self.subTest(intento=intento):
                r = self.client.post("/api/cleanup/execute", json={"paths": [intento]})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["deleted"], [], f"borró {intento}")
                self.assertTrue(r.json()["failed"])
        self.assertTrue((victima / "no_me_borres.mkv").exists(),
                        "¡se ha borrado la biblioteca!")

    def test_borra_lo_que_si_es_basura(self):
        tmp_mkv = self.output_dir / "Peli (2024).mkv.tmp"
        tmp_mkv.write_bytes(b"x" * 100)
        r = self.client.post("/api/cleanup/execute", json={"paths": [str(tmp_mkv)]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["deleted"]), 1, r.json())
        self.assertFalse(tmp_mkv.exists())


class TestOrigenes(ApiTestCase):

    def test_lista_los_origenes(self):
        (self.isos_dir / "Peli (2024).iso").write_bytes(b"x" * 1024)
        r = self.client.get("/api/sources")
        self.assertEqual(r.status_code, 200, r.text)
        nombres = [s["name"] for s in r.json().get("sources", [])]
        self.assertIn("Peli (2024).iso", nombres)

    def test_el_alias_legacy_sigue_respondiendo(self):
        (self.isos_dir / "Peli (2024).iso").write_bytes(b"x" * 1024)
        d = self.client.get("/api/isos").json()
        self.assertIn("Peli (2024).iso", d["isos"])

    def test_analizar_una_ruta_de_fuera_da_400(self):
        """`safe_source_path` bloquea cualquier `..` o ruta absoluta ajena."""
        for intento in ("../../etc", "/etc/passwd"):
            with self.subTest(intento=intento):
                r = self.client.post("/api/analyze", json={"iso_path": intento})
                self.assertEqual(r.status_code, 400, f"{intento} → {r.status_code}")


class TestLaFichaDeLaPelicula(ApiTestCase):
    """Buscar o fijar la ficha TMDb de un proyecto ya creado.

    `_hydrate_session_tmdb` corre **solo al crear** y es best-effort, así que
    una sesión creada antes de que hubiera API key se quedaba sin ficha para
    siempre: nada lo reintentaba. Medido sobre el NAS, 9 de 44 sesiones sin
    ficha y **8 de las 9 con match perfecto al volver a preguntar** — de ahí
    que ahora exista este endpoint (Tab 3 lo tenía desde siempre; Tab 1, no) y
    que abrir el proyecto lo intente solo.

    Nada de esto sale a la red: se parchea `services.tmdb`.
    """

    class _Ficha:
        def __init__(self, tmdb_id, title):
            self.tmdb_id, self.title, self.year = tmdb_id, title, 2024
        def model_dump(self):
            return {"tmdb_id": self.tmdb_id, "title": self.title,
                    "poster_url": f"https://image.tmdb.org/t/p/w342/{self.tmdb_id}.jpg"}

    def _parchear(self, *, encontrado=True, configurado=True):
        import services.tmdb as tmdb
        fichas = {550: self._Ficha(550, "El club de la lucha"),
                  99: self._Ficha(99, "Peli")}

        async def _buscar(titulo, anio=None, limit=1):
            return [fichas[99]] if encontrado else []

        async def _detalles(tid):
            return fichas.get(int(tid))

        for nombre, valor in (("is_configured", lambda: configurado),
                              ("search_movies", _buscar),
                              ("fetch_details", _detalles)):
            original = getattr(tmdb, nombre)
            setattr(tmdb, nombre, valor)
            self.addCleanup(setattr, tmdb, nombre, original)

    def test_sin_api_key_lo_dice_y_no_toca_la_sesion(self):
        self._parchear(configurado=False)
        sid = self.crear_sesion_tab1()
        d = self.client.post(f"/api/sessions/{sid}/tmdb-refresh").json()
        self.assertEqual(d, {"tmdb_configured": False, "updated": False})
        self.assertIsNone(self.leer_sesion_tab1(sid).tmdb_info)

    def test_busca_por_el_nombre_y_la_guarda(self):
        self._parchear()
        sid = self.crear_sesion_tab1()
        d = self.client.post(f"/api/sessions/{sid}/tmdb-refresh").json()
        self.assertTrue(d["updated"])
        self.assertEqual(self.leer_sesion_tab1(sid).tmdb_info["title"], "Peli")

    def test_un_tmdb_id_manda_sobre_la_busqueda(self):
        """La salida para el caso que no se arregla solo: un nombre que TMDb
        no reconoce, o un match a otra película del mismo título."""
        self._parchear()
        sid = self.crear_sesion_tab1()
        d = self.client.post(f"/api/sessions/{sid}/tmdb-refresh",
                             json={"tmdb_id": 550}).json()
        self.assertEqual(d["details"]["title"], "El club de la lucha")
        self.assertEqual(self.leer_sesion_tab1(sid).tmdb_info["tmdb_id"], 550)

    def test_sin_match_NO_borra_la_ficha_que_ya_hubiera(self):
        """Pulsar el botón sobre un proyecto con ficha buena no puede dejarlo
        sin ninguna. El de Tab 3 escribía `None` y hacía justo eso."""
        self._parchear()
        sid = self.crear_sesion_tab1()
        self.client.post(f"/api/sessions/{sid}/tmdb-refresh", json={"tmdb_id": 550})
        self._parchear(encontrado=False)
        d = self.client.post(f"/api/sessions/{sid}/tmdb-refresh").json()
        self.assertFalse(d["updated"])
        self.assertEqual(self.leer_sesion_tab1(sid).tmdb_info["tmdb_id"], 550)

    def test_una_sesion_que_no_existe_da_404(self):
        self.assertEqual(
            self.client.post("/api/sessions/no_existe/tmdb-refresh").status_code, 404)

    def test_abrir_un_proyecto_sin_ficha_la_busca_una_vez(self):
        """Los 8 de 9 que solo necesitaban que alguien preguntara se arreglan
        sin pulsar nada. Y **una sola vez por proceso**: sin ese guard, cada
        apertura volvería a preguntar por una película que TMDb no conoce."""
        self._parchear()
        from routers import tab1
        tab1._tmdb_intentadas.clear()
        self.addCleanup(tab1._tmdb_intentadas.clear)
        sid = self.crear_sesion_tab1()
        self.client.get(f"/api/sessions/{sid}")
        self.assertIn(sid, tab1._tmdb_intentadas)
        # La segunda apertura no vuelve a intentarlo.
        antes = set(tab1._tmdb_intentadas)
        self.client.get(f"/api/sessions/{sid}")
        self.assertEqual(set(tab1._tmdb_intentadas), antes)


if __name__ == "__main__":
    unittest.main()
