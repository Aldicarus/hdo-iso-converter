"""`GET /api/mkv/recientes` — lo que llena la columna izquierda de Tab 2.

Tab 2 no persiste proyectos, así que su columna no puede listar «proyectos»
como Tab 1 y Tab 3. Lista los **MKVs ya analizados**, que salen de la caché de
`/config/mkv_audits/` — un fichero por MKV abierto alguna vez. Cero
persistencia nueva.

Lo que estos tests protegen, y por qué cada cosa:

* **Una entrada rota no puede tumbar la lista.** Corrupta, con un JSON que no
  es un objeto, sin `original_file_path` o sin fecha: la columna entera se
  quedaría en blanco por un fichero, y el usuario no tiene forma de saber cuál.
* **Un bloque con la versión caducada NO cuenta como análisis hecho.**
  `read_mkv_cache` no lo sirve, así que abrir ese MKV vuelve a analizarlo:
  anunciar «🔬 extendido» sería prometer algo que la app va a recalcular. Vale
  igual para el perfil de luminancia, que vive DENTRO del bloque `quality`.
* **El MKV movido se MARCA, no se oculta.** La caché va por fingerprint y la
  ruta es una pista: el análisis sigue siendo válido y se reaprovecha si el
  fichero reaparece. Esconderlo daría a entender que hay que rehacerlo.
* **El recorte va DESPUÉS de ordenar.** Al revés, `?limite=N` devolvería N
  entradas cualesquiera y las llamaría «las más recientes».
* **El recorrido del directorio corre fuera del bucle de eventos.** Es un
  `glob` + un `json.loads` por MKV en el mismo proceso que lee el pipe de los
  `ffmpeg` en marcha. Se comprueba preguntando si hay un bucle corriendo, no
  cronometrando: un test atado al reloj falla bajo carga y acusa al código
  bueno.

Ejecutar desde la raíz del repo:
    .venv/bin/python3 -m unittest app.tests.test_tab2_recientes -v
"""
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402


class RecientesTestCase(ApiTestCase):
    """Escribe ficheros de caché a mano en el /config aislado del arnés."""

    def setUp(self):
        super().setUp()
        import storage
        from phases.mkv_analyze import CACHE_VERSION_BASIC, CACHE_VERSION_QUALITY
        self.v_basic = CACHE_VERSION_BASIC
        self.v_quality = CACHE_VERSION_QUALITY
        self.audits = storage.MKV_AUDIT_DIR
        self.audits.mkdir(parents=True, exist_ok=True)
        self.mkvs = self.tmp / "biblioteca"
        self.mkvs.mkdir(parents=True, exist_ok=True)

    # ── construcción de estado ───────────────────────────────────────

    def escribir_mkv(self, nombre: str) -> str:
        """Un fichero que existe de verdad, para el `stat` de `existe`."""
        p = self.mkvs / nombre
        p.write_bytes(b"no es un mkv, pero existe")
        return str(p)

    def escribir_cache(self, sha, ruta, *, cached_at,
                       basic=True, quality=True, luz=True,
                       v_basic=None, v_quality=None,
                       tamano=42_000_000_000, duracion=7200.0,
                       crudo=None):
        """Un fichero de `/config/mkv_audits/` con la forma que escribe
        `storage._write_mkv_cache_full`. `crudo` lo sustituye entero."""
        destino = self.audits / f"{sha}.json"
        if crudo is not None:
            destino.write_text(crudo, encoding="utf-8")
            return destino
        versions = {}
        if basic:
            versions["basic"] = self.v_basic if v_basic is None else v_basic
        if quality:
            versions["quality"] = self.v_quality if v_quality is None else v_quality
        bloque_quality = None
        if quality:
            bloque_quality = {
                "quality_total_frames_rpu": 190_021,
                "quality_classification": "real",
            }
            if luz:
                bloque_quality["light_profile"] = {"total_frames": 190_021}
        destino.write_text(json.dumps({
            "schema_version": 1,
            "fingerprint": {"sha256_1mb": sha, "size_bytes": tamano,
                            "mtime_ns": 1},
            "versions": versions,
            "cached_at": cached_at,
            "basic": ({"file_path": ruta, "file_name": Path(ruta).name,
                       "duration_seconds": duracion} if basic else None),
            "quality": bloque_quality,
            "original_file_path": ruta,
        }, ensure_ascii=False), encoding="utf-8")
        return destino

    # ── acceso ───────────────────────────────────────────────────────

    def pedir(self, **params):
        r = self.client.get("/api/mkv/recientes", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def por_nombre(self, cuerpo):
        return {t["nombre"]: t for t in cuerpo["recientes"]}


class TestLoQueDevuelve(RecientesTestCase):

    def test_sin_caché_la_lista_está_vacía(self):
        cuerpo = self.pedir()
        self.assertEqual(cuerpo, {"recientes": [], "total": 0})

    def test_una_tarjeta_trae_todo_lo_que_la_columna_pinta(self):
        ruta = self.escribir_mkv("Dune (2021).mkv")
        self.escribir_cache("aa", ruta, cached_at="2026-09-01T10:00:00+00:00",
                            tamano=79_000_000_000, duracion=9155.0)
        t = self.pedir()["recientes"][0]
        self.assertEqual(t["ruta"], ruta)
        self.assertEqual(t["nombre"], "Dune (2021).mkv")
        self.assertEqual(t["tamano_bytes"], 79_000_000_000)
        self.assertEqual(t["duracion_segundos"], 9155.0)
        self.assertEqual(t["analizado_en"], "2026-09-01T10:00:00+00:00")
        self.assertTrue(t["existe"])
        self.assertTrue(t["tiene_basico"])
        self.assertTrue(t["tiene_extendido"])
        self.assertTrue(t["tiene_luminancia"])

    def test_el_tamaño_es_el_del_mkv_no_el_del_fichero_de_caché(self):
        """`list_mkv_audit_entries` ya devolvía un `size_bytes` que es el del
        JSON de la caché (unos KB). Confundirlos pintaría «12 KB» en la ficha
        de un UHD de 79 GB."""
        ruta = self.escribir_mkv("Alien.mkv")
        fichero = self.escribir_cache("bb", ruta, cached_at="2026-09-01T10:00:00+00:00",
                                      tamano=79_000_000_000)
        t = self.pedir()["recientes"][0]
        self.assertEqual(t["tamano_bytes"], 79_000_000_000)
        self.assertNotEqual(t["tamano_bytes"], fichero.stat().st_size)

    def test_vienen_del_más_reciente_al_más_antiguo(self):
        for sha, nombre, cuando in (
            ("a1", "vieja.mkv",   "2026-01-01T00:00:00+00:00"),
            ("a2", "nueva.mkv",   "2026-09-08T00:00:00+00:00"),
            ("a3", "de enmedio.mkv", "2026-05-05T00:00:00+00:00"),
        ):
            self.escribir_cache(sha, self.escribir_mkv(nombre), cached_at=cuando)
        cuerpo = self.pedir()
        self.assertEqual([t["nombre"] for t in cuerpo["recientes"]],
                         ["nueva.mkv", "de enmedio.mkv", "vieja.mkv"])
        self.assertEqual(cuerpo["total"], 3)


class TestUnaEntradaRotaNoTumbaLaLista(RecientesTestCase):

    def _con_una_buena(self):
        self.escribir_cache("ok", self.escribir_mkv("Buena.mkv"),
                            cached_at="2026-09-01T10:00:00+00:00")

    def test_un_json_corrupto_se_salta(self):
        self._con_una_buena()
        self.escribir_cache("rota", "", cached_at="", crudo="{esto no es json")
        cuerpo = self.pedir()
        self.assertEqual([t["nombre"] for t in cuerpo["recientes"]], ["Buena.mkv"])

    def test_un_json_válido_que_no_es_un_objeto_se_salta(self):
        """Es JSON legal, así que `json.loads` no lanza — lanza el `.get` de
        después, con un AttributeError que se lleva la petición entera."""
        self._con_una_buena()
        self.escribir_cache("lista", "", cached_at="", crudo="[1, 2, 3]")
        self.escribir_cache("cadena", "", cached_at="", crudo='"hola"')
        cuerpo = self.pedir()
        self.assertEqual([t["nombre"] for t in cuerpo["recientes"]], ["Buena.mkv"])

    def test_una_entrada_sin_ruta_de_origen_se_salta(self):
        """Las caches escritas antes de que existiera `original_file_path`. Sin
        ruta no hay tarjeta que pintar ni MKV que abrir."""
        self._con_una_buena()
        self.escribir_cache("sinruta", "", cached_at="2026-09-02T00:00:00+00:00",
                            crudo=json.dumps({
                                "fingerprint": {"sha256_1mb": "sinruta"},
                                "versions": {"basic": self.v_basic},
                                "cached_at": "2026-09-02T00:00:00+00:00",
                                "basic": {"file_name": "x.mkv"},
                            }))
        cuerpo = self.pedir()
        self.assertEqual([t["nombre"] for t in cuerpo["recientes"]], ["Buena.mkv"])

    def test_una_ruta_que_no_es_texto_se_salta(self):
        """Un JSON con la forma correcta pero un campo del tipo que no es. No
        lo puede escribir la app, pero `Path(42)` lanza TypeError y eso vacía
        la columna entera."""
        self._con_una_buena()
        self.escribir_cache("rara", "", cached_at="", crudo=json.dumps({
            "fingerprint": {"sha256_1mb": "rara"},
            "versions": {"basic": self.v_basic},
            "cached_at": "2026-09-03T00:00:00+00:00",
            "basic": {"duration_seconds": 1},
            "original_file_path": 42,
        }))
        cuerpo = self.pedir()
        self.assertEqual([t["nombre"] for t in cuerpo["recientes"]], ["Buena.mkv"])

    def test_una_entrada_sin_fecha_se_salta(self):
        """La fecha es la clave de ordenación: sin ella la tarjeta no sabe
        decir cuándo se analizó y encima desordena el resto."""
        self._con_una_buena()
        self.escribir_cache("sinfecha", self.escribir_mkv("Sin fecha.mkv"),
                            cached_at=None)
        cuerpo = self.pedir()
        self.assertEqual([t["nombre"] for t in cuerpo["recientes"]], ["Buena.mkv"])


class TestQuéAnálisisTieneHecho(RecientesTestCase):

    def test_sin_bloque_quality_es_solo_básico(self):
        self.escribir_cache("a", self.escribir_mkv("Solo basico.mkv"),
                            cached_at="2026-09-01T10:00:00+00:00", quality=False)
        t = self.pedir()["recientes"][0]
        self.assertTrue(t["tiene_basico"])
        self.assertFalse(t["tiene_extendido"])
        self.assertFalse(t["tiene_luminancia"])

    def test_con_extendido_pero_sin_perfil_de_luminancia(self):
        """El análisis extendido es anterior al perfil de luminancia en varias
        caches del NAS: tiene combos L8/L2 y no tiene curva L1."""
        self.escribir_cache("a", self.escribir_mkv("Sin luz.mkv"),
                            cached_at="2026-09-01T10:00:00+00:00", luz=False)
        t = self.pedir()["recientes"][0]
        self.assertTrue(t["tiene_extendido"])
        self.assertFalse(t["tiene_luminancia"])

    def test_un_bloque_basic_caducado_no_cuenta_como_analizado(self):
        self.escribir_cache("a", self.escribir_mkv("Basic viejo.mkv"),
                            cached_at="2026-09-01T10:00:00+00:00",
                            v_basic=self.v_basic - 1)
        t = self.pedir()["recientes"][0]
        self.assertFalse(t["tiene_basico"])

    def test_un_bloque_quality_caducado_arrastra_al_perfil_de_luminancia(self):
        """El perfil vive DENTRO del bloque `quality`, así que caduca con él:
        `read_mkv_cache` no lo sirve y el comparador A/B no lo encontraría.
        Anunciar 💡 sería ofrecer algo que no está."""
        self.escribir_cache("a", self.escribir_mkv("Quality viejo.mkv"),
                            cached_at="2026-09-01T10:00:00+00:00",
                            v_quality=self.v_quality - 1, luz=True)
        t = self.pedir()["recientes"][0]
        self.assertTrue(t["tiene_basico"])
        self.assertFalse(t["tiene_extendido"])
        self.assertFalse(t["tiene_luminancia"])

    def test_una_caché_entera_caducada_sigue_saliendo_en_la_lista(self):
        """Es un MKV que SE analizó: la tarjeta cuenta la verdad (ningún
        análisis vigente) en vez de desaparecer sin explicación."""
        self.escribir_cache("a", self.escribir_mkv("Todo viejo.mkv"),
                            cached_at="2026-09-01T10:00:00+00:00",
                            v_basic=self.v_basic - 1, v_quality=self.v_quality - 1)
        t = self.pedir()["recientes"][0]
        self.assertEqual(t["nombre"], "Todo viejo.mkv")
        self.assertFalse(t["tiene_basico"])
        self.assertFalse(t["tiene_extendido"])


class TestElMkvQueYaNoEstá(RecientesTestCase):

    def test_se_marca_pero_no_se_oculta(self):
        self.escribir_cache("a", str(self.mkvs / "Se lo llevaron.mkv"),
                            cached_at="2026-09-01T10:00:00+00:00")
        cuerpo = self.pedir()
        self.assertEqual(len(cuerpo["recientes"]), 1,
                         "una entrada cuyo MKV no está NO se oculta: el "
                         "análisis sigue valiendo si el fichero reaparece")
        t = cuerpo["recientes"][0]
        self.assertFalse(t["existe"])
        self.assertEqual(t["nombre"], "Se lo llevaron.mkv")
        # Y lo que sí se sabe se sigue sabiendo: lo midió el fingerprint.
        self.assertEqual(t["tamano_bytes"], 42_000_000_000)

    def test_convive_con_las_que_sí_están(self):
        self.escribir_cache("a", self.escribir_mkv("Aqui.mkv"),
                            cached_at="2026-09-02T00:00:00+00:00")
        self.escribir_cache("b", str(self.mkvs / "Ya no.mkv"),
                            cached_at="2026-09-01T00:00:00+00:00")
        t = self.por_nombre(self.pedir())
        self.assertTrue(t["Aqui.mkv"]["existe"])
        self.assertFalse(t["Ya no.mkv"]["existe"])


class TestElRecorte(RecientesTestCase):

    def _cinco(self):
        # Escritas de la más vieja a la más nueva, para que el orden del `glob`
        # no coincida por casualidad con el que se espera.
        for i in range(5):
            self.escribir_cache(
                f"s{i}", self.escribir_mkv(f"peli{i}.mkv"),
                cached_at=f"2026-09-0{i + 1}T00:00:00+00:00")

    def test_recorta_por_arriba_y_total_no_miente(self):
        self._cinco()
        cuerpo = self.pedir(limite=2)
        self.assertEqual(len(cuerpo["recientes"]), 2)
        self.assertEqual(cuerpo["total"], 5, "`total` cuenta lo que hay, no lo "
                                             "que se devuelve")

    def test_lo_que_sobrevive_al_recorte_son_las_MÁS_RECIENTES(self):
        """El recorte va después de ordenar. Al revés serían dos cualesquiera
        —el orden del `glob` es el del directorio— presentadas como las
        últimas."""
        self._cinco()
        cuerpo = self.pedir(limite=2)
        self.assertEqual([t["nombre"] for t in cuerpo["recientes"]],
                         ["peli4.mkv", "peli3.mkv"])


class TestElDirectorioSeRecorreFueraDelBucle(RecientesTestCase):
    """Un `glob` + un `stat` + un `json.loads` por MKV analizado, en el mismo
    proceso que lee el pipe de los ffmpeg en marcha.

    No se cronometra —un test atado al reloj falla bajo carga y señala al
    código bueno— y tampoco vale mirar si es el hilo principal: bajo
    `TestClient` el bucle de eventos ya vive en un hilo aparte. Lo que se
    pregunta es lo que importa: si hay un bucle CORRIENDO en este hilo,
    entonces esto se está ejecutando dentro de él."""

    def test_el_recorrido_no_ve_ningún_bucle_de_eventos(self):
        import asyncio
        import storage
        dentro = []
        original = storage.list_mkv_audit_entries

        def _espia():
            try:
                asyncio.get_running_loop()
                dentro.append(True)
            except RuntimeError:
                dentro.append(False)
            return original()

        storage.list_mkv_audit_entries = _espia
        self.addCleanup(setattr, storage, "list_mkv_audit_entries", original)
        self.escribir_cache("a", self.escribir_mkv("X.mkv"),
                            cached_at="2026-09-01T10:00:00+00:00")
        self.pedir()
        self.assertEqual(dentro, [False],
                         "el recorrido de /config/mkv_audits/ tiene que ir en "
                         "`asyncio.to_thread`, no en el bucle de eventos")


class TestEnDevModeHayAlgoQuePintar(RecientesTestCase):
    """Sin discos reales la caché está vacía y la columna saldría siempre en su
    estado vacío, que es justo lo que no se puede desarrollar."""

    def test_devuelve_fixtures_sin_tocar_el_disco(self):
        from routers import tab2 as t2
        original = t2.DEV_MODE
        t2.DEV_MODE = True
        self.addCleanup(setattr, t2, "DEV_MODE", original)
        cuerpo = self.pedir()
        self.assertGreater(len(cuerpo["recientes"]), 0)
        t = cuerpo["recientes"][0]
        for campo in ("ruta", "nombre", "tamano_bytes", "analizado_en",
                      "existe", "tiene_basico", "tiene_extendido",
                      "tiene_luminancia"):
            self.assertIn(campo, t)
        self.assertTrue(any(not x["existe"] for x in cuerpo["recientes"]),
                        "hace falta uno movido para poder ver el aviso")


if __name__ == "__main__":
    unittest.main()
