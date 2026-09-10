"""En la columna se lee la PELÍCULA, no el fichero.

Cada punto componía su texto por su cuenta y salían cuatro estilos distintos
para la misma pregunta: el nombre del MKV con sus tags
(`Drive.2011.UHD.BluRay [DV FEL].mkv`), la ruta interna del destino (`copia de
X a /mnt/output`), el nombre de la serie a secas y —cuando faltaba el nombre—
el **session id crudo**, que es un identificador interno en pantalla.

`trabajos.nombre_de_trabajo` es el único sitio donde se decide, y lo llaman los
tres routers. El fallback reusa `parse_mkv_filename`, el parser de la
recomendación CMv4.0: dos parsers de nombres de película acabarían divergiendo,
que es la regla que este repo ya aprendió con el JSON de `dovi_tool`.

La carátula viaja con el trabajo desde que se encoló —donde la sesión estaba en
la mano— y no se resuelve en cada poll: la columna se refresca cada 2 s y eso
no puede costar una lectura de disco por vuelta. **La columna no dispara ni una
consulta de red.**

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_nombre_de_los_trabajos -v
"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import trabajos  # noqa: E402
from api_harness import ApiTestCase  # noqa: E402


class TestComoSeLlamaUnTrabajo(unittest.TestCase):

    def test_lo_que_dijo_tmdb_manda(self):
        self.assertEqual(
            trabajos.nombre_de_trabajo({"title": "Drive", "year": 2011},
                                       "Drive.2011.UHD [DV FEL].mkv"),
            "Drive (2011)")

    def test_sin_match_se_limpia_el_fichero(self):
        """Ni extensión, ni tags, ni puntos por espacios."""
        for fichero, esperado in [
            ("Zootropolis.2.2025.UHD.BluRay.2160p.x265.mkv", "Zootropolis 2 (2025)"),
            ("El padrino (1972) [DV FEL] [Audio DCP].mkv", "El padrino (1972)"),
            ("Predator [CMv4 CORE].mkv", "Predator"),
        ]:
            with self.subTest(fichero=fichero):
                self.assertEqual(trabajos.nombre_de_trabajo(fichero=fichero),
                                 esperado)

    def test_una_serie_dice_de_QUE_serie(self):
        """En una sesión de serie el `title` de TMDb es el del EPISODIO, así
        que enseñarlo solo dejaría «Pilot (2011)» sin decir de qué."""
        self.assertEqual(
            trabajos.nombre_de_trabajo(
                {"title": "Pilot", "year": 2011},
                serie={"nombre": "The Mandalorian", "anio": 2019,
                       "temporada": 1, "episodio": 3}),
            "The Mandalorian (2019) · S01E03")

    def test_sin_nada_devuelve_vacio_y_no_inventa(self):
        self.assertEqual(trabajos.nombre_de_trabajo(), "")
        self.assertEqual(trabajos.nombre_de_trabajo({}, ""), "")

    def test_un_titulo_sin_ano_no_arrastra_parentesis_vacios(self):
        self.assertEqual(trabajos.nombre_de_trabajo({"title": "Akira"}),
                         "Akira")


class TestLaMiniatura(unittest.TestCase):

    def test_se_pide_al_tamano_de_una_miniatura(self):
        """TMDb da la URL a w342: ~30 KB por fila para pintar 40 px. El tamaño
        va en la propia ruta, así que se reescribe."""
        self.assertEqual(
            trabajos.poster_de({"poster_url":
                                "https://image.tmdb.org/t/p/w342/abc.jpg"}),
            "https://image.tmdb.org/t/p/w92/abc.jpg")

    def test_una_url_con_otra_forma_se_deja_como_esta(self):
        """Mejor una miniatura pesada que ninguna."""
        u = "https://ejemplo/imagen.jpg"
        self.assertEqual(trabajos.poster_de({"poster_url": u}), u)

    def test_sin_poster_no_hay_nada_que_pintar(self):
        self.assertEqual(trabajos.poster_de(None), "")
        self.assertEqual(trabajos.poster_de({}), "")


class TestElContratoLosLleva(unittest.TestCase):

    def test_titulo_y_poster_viajan_en_la_entrada_de_la_cola(self):
        import queue_manager as qm
        t = qm.TrabajoEnCola(tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave="p1",
                             que="Upgrade CMv4.0 · Drive (2011)",
                             titulo="Drive (2011)", poster="http://x/w92/a.jpg")
        p = trabajos.progreso_de(t)
        self.assertEqual(p["titulo"], "Drive (2011)")
        self.assertEqual(p["poster"], "http://x/w92/a.jpg")

    def test_una_cola_persistida_de_antes_sigue_cargando(self):
        """Las entradas escritas sin estos campos no pueden reventar el
        arranque: se pintan con su icono y ya."""
        import queue_manager as qm
        t = qm.TrabajoEnCola.de_json(
            {"tab": "rip", "tipo": qm.TIPO_RIP, "clave": "s1", "que": "x"})
        self.assertEqual((t.titulo, t.poster), ("", ""))
        self.assertEqual(trabajos.progreso_de(t)["titulo"], "")


class TestElHistorialLosGuarda(unittest.TestCase):

    def test_la_linea_lleva_pelicula_y_caratula(self):
        import historial
        import paths
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            previo, paths.CONFIG_DIR = paths.CONFIG_DIR, Path(d)
            self.addCleanup(setattr, paths, "CONFIG_DIR", previo)
            historial.anotar(id="s1", tab="rip", tipo="rip",
                             que="Conversión a MKV · Drive (2011)",
                             titulo="Drive (2011)", poster="http://x/w92/a.jpg",
                             inicio=datetime.now(timezone.utc))
            r = historial.leer()[0]
            self.assertEqual(r["titulo"], "Drive (2011)")
            self.assertEqual(r["poster"], "http://x/w92/a.jpg")


class TestNadieCompleteElNombreASuManera(unittest.TestCase):
    """El barrido: ningún `que=` de los routers puede seguir enseñando el
    nombre de un fichero con extensión ni una ruta interna."""

    _FUENTES = ("routers/tab1.py", "routers/tab2.py", "routers/cmv40.py",
                "queue_manager.py")

    def test_ningun_que_arma_su_texto_con_un_nombre_de_fichero(self):
        import re
        malos = []
        for rel in self._FUENTES:
            texto = (APP_DIR / rel).read_text(encoding="utf-8")
            for linea in texto.splitlines():
                m = re.search(r'que\s*=\s*\(?f?"([^"]*)"', linea)
                if not m:
                    continue
                t = m.group(1)
                if "/mnt/" in t:
                    malos.append(f"{rel}: {t}")
                    continue
                # Un `{x or fichero.name}` es un RESPALDO y es legítimo: si el
                # parser no saca nada del nombre, enseñar el fichero es mejor
                # que dejar el hueco. Lo que no puede es ser la fuente
                # principal, así que se mira cada hueco por separado.
                for hueco in re.findall(r"\{([^}]*)\}", t):
                    if " or " in hueco:
                        continue
                    for prohibido in ("mkv_name", ".name", "session.id"):
                        if prohibido in hueco:
                            malos.append(f"{rel}: {t}")
        self.assertEqual(malos, [], "un `que=` compone el nombre por su cuenta")

    def test_el_atajo_que_ensenaba_el_session_id_ya_no_existe(self):
        """`enqueue(session_id)` componía `que` con el id crudo. Su único
        llamador pasó a `encolar` con nombre y carátula."""
        import queue_manager as qm
        self.assertFalse(hasattr(qm.QueueManager, "enqueue"))


class TestLosEndpointsLoPonen(ApiTestCase):

    def test_encolar_un_rip_lleva_la_pelicula_y_su_caratula(self):
        import storage
        (self.isos_dir / "Peli (2024).iso").write_bytes(b"x" * 4096)
        sid = self.crear_sesion_tab1(sid="Drive_2011_1")
        s = storage.load_session(sid)
        s.mkv_name = "Drive (2011) [DV FEL].mkv"
        s.tmdb_info = {"title": "Drive", "year": 2011,
                       "poster_url": "https://image.tmdb.org/t/p/w342/d.jpg"}
        storage.save_session(s)
        r = self.client.post(f"/api/sessions/{sid}/execute")
        self.assertEqual(r.status_code, 200, r.text)
        t = [x for x in self.encolados_enteros if x.clave == sid]
        self.assertEqual(len(t), 1, self.encolados_enteros)
        # Lo que el usuario LEE: la película, sin los tags que la propia app
        # le pone al nombre del fichero.
        self.assertEqual(t[0].titulo, "Drive (2011)")
        self.assertEqual(t[0].que, "Conversión a MKV · Drive (2011)")
        self.assertNotIn("[DV FEL]", t[0].que)
        self.assertTrue(t[0].poster.endswith("/w92/d.jpg"), t[0].poster)

    def test_una_fase_cmv40_encolada_tambien(self):
        import storage
        sid = self.crear_sesion(sid="cmv40_drive", phase="created")
        s = storage.load_cmv40_session(sid)
        s.output_mkv_name = "Drive (2011) [CMv4 CORE].mkv"
        s.tmdb_info = {"title": "Drive", "year": 2011,
                       "poster_url": "https://image.tmdb.org/t/p/w342/d.jpg"}
        storage.save_cmv40_session(s)
        import asyncio
        from routers import cmv40
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            cmv40._cmv40_encolar_fase(storage.load_cmv40_session(sid),
                                      "analyze_source"))
        t = self.encolados_enteros[-1]
        self.assertEqual(t.titulo, "Drive (2011)")
        self.assertNotIn("[CMv4", t.que)
        self.assertTrue(t.poster.endswith("/w92/d.jpg"), t.poster)

    def test_lo_interactivo_dice_sobre_que_trabaja(self):
        """La marca de la ruta solo sabe «Apertura de un MKV»: es el endpoint
        quien le pone nombre cuando resuelve el fichero. Se comprueba de
        verdad porque el mecanismo —un ContextVar puesto por la dependencia—
        depende de que el contexto llegue al endpoint."""
        import workload
        visto = {}
        original = workload.detallar_actual

        def _espia(**kw):
            visto.update(kw)
            original(**kw)

        workload.detallar_actual = _espia
        self.addCleanup(setattr, workload, "detallar_actual", original)
        (self.output_dir / "Drive (2011) [DV FEL].mkv").write_bytes(b"x" * 32)
        self.client.post("/api/mkv/analyze",
                         json={"file_path": "Drive (2011) [DV FEL].mkv"})
        self.assertEqual(visto.get("titulo"), "Drive (2011)")

    def test_y_el_contexto_llega_de_verdad_hasta_workload(self):
        """El eslabón que el espía de arriba no cubre: que la clave del
        ContextVar sea la del trabajo que la petición registró."""
        import workload
        workload.limpiar()
        vistos = []
        original = workload.detallar

        def _espia(clave, **kw):
            vistos.append((clave, kw.get("titulo")))
            original(clave, **kw)

        workload.detallar = _espia
        self.addCleanup(setattr, workload, "detallar", original)
        (self.output_dir / "Drive (2011).mkv").write_bytes(b"x" * 32)
        self.client.post("/api/mkv/analyze",
                         json={"file_path": "Drive (2011).mkv"})
        self.assertTrue(vistos, "el ContextVar no llegó al endpoint")
        # La clave que genera `marca` es «<etiqueta de pestaña>#<n>».
        self.assertTrue(vistos[0][0].startswith(workload.TAB_MKV + "#"), vistos)
        self.assertEqual(vistos[0][1], "Drive (2011)")


if __name__ == "__main__":
    unittest.main()
