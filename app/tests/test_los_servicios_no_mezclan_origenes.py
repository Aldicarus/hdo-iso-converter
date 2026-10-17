"""Lo que viene de fuera —la hoja, Drive, TMDb, GitHub— no se mezcla ni se pierde.

Hallazgos del paquete P4 de la auditoría con Codex del 2026-10-17, todos sin
error visible:

  · la recomendación juntaba secuelas como si fueran dos secciones del mismo
    título (Night/Night II, Kill Bill 1/2, Matrix Reloaded/Revolutions): el
    rechazo de una salía con el veredicto y el offset de la otra;
  · una temporada catalana con los episodios SIN nombre daba la petición por
    satisfecha, y el nombre del episodio va dentro del nombre del MKV;
  · el listado de Drive en vuelo se guardaba etiquetado con la carpeta
    configurada AL TERMINAR, y la caché en memoria no sabía de qué carpeta era;
  · la «última copia buena» de la hoja solo la escribía el CSV, antes de
    parsearlo y sin saber de qué hoja era;
  · `CMV40_DRIVE_FOLDER_ID` (legacy) se anunciaba en ⚙︎ y se listaba la
    carpeta de la app;
  · «Probar Google» sin carpeta aprobaba una clave que Sheets rechazaba;
  · un 403 de GitHub reescribía la caché de actualizaciones con nada.

Todo con transportes HTTP de mentira: nada sale a la red.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_los_servicios_no_mezclan_origenes -v
"""
import asyncio
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402


def _cliente_con(manejador):
    real = httpx.AsyncClient

    def fabrica(*a, **k):
        k.pop("transport", None)
        return real(*a, transport=httpx.MockTransport(manejador), **k)
    return fabrica


class ConConfigAislada(unittest.TestCase):
    """`/config` en un tmpdir para settings, TMDb, Drive y la hoja."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="servicios_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        from services import settings_store as st, tmdb, rec999_drive as drive
        from services import rec999_sheet as sheet
        import i18n
        self.st, self.tmdb, self.drive, self.sheet = st, tmdb, drive, sheet
        for mod, attr, valor in (
                (st, "CONFIG_DIR", self.tmp), (st, "SETTINGS_PATH", self.tmp / "app_settings.json"),
                (st, "_cache", None),
                (tmdb, "CONFIG_DIR", self.tmp), (tmdb, "CACHE_PATH", self.tmp / "tmdb_cache.json"),
                (tmdb, "_cache", None),
                (drive, "CONFIG_DIR", self.tmp), (drive, "CACHE_PATH", self.tmp / "drive.json"),
                (drive, "_cache_files", None), (drive, "_cache_fetched_at", 0.0),
                (drive, "_cache_folder_id", ""),
                (sheet, "CONFIG_DIR", self.tmp), (sheet, "CACHE_PATH", self.tmp / "sheet.json"),
                (sheet, "_cache_rows", None), (sheet, "_cache_fetched_at", 0.0),
                (sheet, "_cache_sheet", ("", ""))):
            p = mock.patch.object(mod, attr, valor)
            p.start()
            self.addCleanup(p.stop)
        entorno = mock.patch.dict(os.environ, {}, clear=False)
        entorno.start()
        self.addCleanup(entorno.stop)
        for v in ("HDO_IDIOMA", "CMV40_DRIVE_FOLDER_URL", "CMV40_DRIVE_FOLDER_ID",
                  "GOOGLE_API_KEY", "CMV40_SHEET_URL"):
            os.environ.pop(v, None)
        i18n.limpiar_cache()
        self.addCleanup(i18n.limpiar_cache)


# ── #14 · la recomendación ─────────────────────────────────────────────

class TestLaRecomendacionNoMezclaSecuelas(unittest.TestCase):

    def _recomendar(self, titulo, anio, filas):
        from services import cmv40_recommend as cr
        from services.rec999_sheet import RecommendationRow

        def fila(t, y, factible, notas="", offset=""):
            return RecommendationRow(
                feasible=factible, section="feasible" if factible else "infeasible",
                title_raw=t, title_normalized=cr._normalize_title(t), year=y,
                notes=notas, sync_offset=offset)

        async def _filas():
            return [fila(*f) for f in filas]
        with mock.patch.object(cr, "get_recommendations", _filas), \
                mock.patch.object(cr, "get_cache_status", lambda: {"source": "csv"}), \
                mock.patch.object(cr, "tmdb_is_configured", lambda: False):
            return asyncio.run(cr.recommend(titulo, anio))

    def test_casos_reproducidos(self):
        casos = [
            ("Night", 2024, [("Night", 2024, False, "static DV, not worth it"),
                             ("Night II", 2025, True, "", "(+24)")]),
            ("Kill Bill Vol. 1", 2003, [("Kill Bill Vol. 1", 2003, False, "grading is different"),
                                        ("Kill Bill Vol. 2", 2004, True, "", "(+24)")]),
            ("The Matrix Reloaded", 2003, [("The Matrix Reloaded", 2003, False, "static DV"),
                                           ("The Matrix Revolutions", 2003, True, "", "(+12)")]),
        ]
        for titulo, anio, filas in casos:
            with self.subTest(titulo):
                r = self._recomendar(titulo, anio, filas)
                self.assertEqual(r.match_title, filas[0][0], "el veredicto es de otra película")
                self.assertEqual(r.status, "not_feasible")
                self.assertEqual(r.sync_offset, "")
                self.assertEqual(r.feasible_row_count, 0)

    def test_el_mismo_titulo_en_dos_secciones_sigue_juntandose(self):
        r = self._recomendar("The Matrix Reloaded", 2003, [
            ("The Matrix Reloaded", 2003, False, "static DV"),
            ("Matrix Reloaded", 2003, True, "", "(+12)")])
        self.assertEqual((r.feasible_row_count, r.infeasible_row_count), (1, 1))


# ── #15 · la temporada ─────────────────────────────────────────────────

class TestUnaTemporadaMudaSeCompletaEnCastellano(ConConfigAislada):

    def _temporada(self, manejador):
        (self.tmp / "app_settings.json").write_text(json.dumps({"idioma": "ca"}))
        self.st._cache = None
        import i18n
        i18n.limpiar_cache()
        self.st.update_tmdb_api_key("clave_de_prueba")
        with mock.patch.object(self.tmdb.httpx, "AsyncClient", _cliente_con(manejador)):
            return asyncio.run(self.tmdb.fetch_tv_season(1399, 3))

    def test_los_nombres_vacios_salen_del_castellano(self):
        idiomas = []

        def manejador(req):
            lang = req.url.params.get("language")
            idiomas.append(lang)
            if lang == "es-ES":
                eps = [{"episode_number": 1, "name": "Valar Dohaeris", "overview": "Sinopsis 1"},
                       {"episode_number": 2, "name": "Alas negras", "overview": "Sinopsis 2"}]
            else:
                eps = [{"episode_number": 1, "name": "", "overview": ""},
                       {"episode_number": 2, "name": "Ales negres", "overview": ""}]
            return httpx.Response(200, json={"episodes": eps})

        eps = self._temporada(manejador)
        self.assertEqual([e.name for e in eps], ["Valar Dohaeris", "Ales negres"],
                         "un episodio sin nombre acaba en un MKV sin título")
        self.assertEqual(eps[1].overview, "Sinopsis 2")
        self.assertEqual(idiomas, ["ca-ES", "es-ES"])

    def test_con_todos_traducidos_no_se_repite(self):
        idiomas = []

        def manejador(req):
            idiomas.append(req.url.params.get("language"))
            return httpx.Response(200, json={"episodes": [
                {"episode_number": 1, "name": "Un", "overview": "x"}]})

        self._temporada(manejador)
        self.assertEqual(idiomas, ["ca-ES"])


# ── #16 · Drive ────────────────────────────────────────────────────────

class TestElListadoDeDriveEsDeLaCarpetaQueSeListo(ConConfigAislada):

    def test_cambiar_la_carpeta_a_mitad_no_cruza_los_listados(self):
        carpeta = {"id": "carpetaAAAAAA"}

        def manejador(req):
            q = req.url.params.get("q", "")
            if "carpetaAAAAAA" in q:
                carpeta["id"] = "carpetaBBBBBB"     # alguien la cambia en ⚙︎
                nombre = "De_A.bin"
            else:
                nombre = "De_B.bin"
            return httpx.Response(200, json={"files": [
                {"id": nombre, "name": nombre, "mimeType": "application/octet-stream",
                 "size": "10"}]})

        with mock.patch.object(self.drive, "get_google_api_key", return_value="k"), \
                mock.patch.object(self.drive, "get_cmv40_drive_folder_id",
                                  lambda: carpeta["id"]), \
                mock.patch.object(self.drive.httpx, "AsyncClient", _cliente_con(manejador)):
            primero = asyncio.run(self.drive.list_bin_files(force_refresh=True))
            self.assertEqual([f.name for f in primero], ["De_A.bin"])
            guardado = json.loads(self.drive.CACHE_PATH.read_text())
            self.assertEqual(guardado["folder_id"], "carpetaAAAAAA",
                             "el listado de A quedó etiquetado como de B")
            segundo = asyncio.run(self.drive.list_bin_files())
            self.assertEqual([f.name for f in segundo], ["De_B.bin"],
                             "la caché en memoria sirvió el listado de la carpeta anterior")


# ── #16 y #17 · la hoja ────────────────────────────────────────────────

_CSV = (
    "h0,h1,h2,h3,h4,h5,h6,h7,h8,h9,h10,h11,h12,h13,h14,h15,h16,h17,h18\n"
    '"Obsession 2026","BD FEL","HDR COMP","sample","static dv",'
    '"","Obsession 2026","(+24)","MA","HDR COMP","plot","restorable",'
    '"","","","","","",""\n'
)


def _como_api(texto):
    return [[{"text": c, "link": ""} for c in fila]
            for fila in csv.reader(io.StringIO(texto))]


class TestLaUltimaCopiaDeLaHoja(ConConfigAislada):

    def _leer(self, hoja=("hojaA", "0"), xlsx=None, csv_texto=None):
        async def _sin_api(*a):
            return None

        async def _xlsx():
            return xlsx

        async def _csv():
            if csv_texto is None:
                raise httpx.ConnectError("sin red")
            return csv_texto
        with mock.patch.object(self.sheet, "get_google_api_key", return_value=""), \
                mock.patch.object(self.sheet, "get_cmv40_sheet_id_gid", return_value=hoja), \
                mock.patch.object(self.sheet, "_fetch_sheet_api", _sin_api), \
                mock.patch.object(self.sheet, "_fetch_sheet_xlsx", _xlsx), \
                mock.patch.object(self.sheet, "_fetch_sheet_html", _sin_api), \
                mock.patch.object(self.sheet, "_fetch_csv", _csv):
            return asyncio.run(self.sheet.get_recommendations(force_refresh=True))

    def _reinicio(self):
        self.sheet._cache_rows = None
        self.sheet._cache_sheet = ("", "")

    def test_lo_leido_por_xlsx_sobrevive_a_una_caida_de_la_red(self):
        self.assertTrue(self._leer(xlsx=_como_api(_CSV)))
        self._reinicio()
        filas = self._leer()
        self.assertTrue(filas, "sin copia: la instalación que lee por XLSX se queda sin hoja")
        self.assertEqual(self.sheet.get_cache_status()["source"], "disk")

    def test_un_csv_que_no_es_la_hoja_no_sustituye_la_copia_buena(self):
        self._leer(xlsx=_como_api(_CSV))
        self._reinicio()
        filas = self._leer(csv_texto="<html><body>Sign in</body></html>")
        self.assertTrue(filas, "una página de error dejó la hoja vacía")
        self._reinicio()
        self.assertTrue(self._leer(), "la copia buena se sustituyó por la página de error")

    def test_la_copia_de_otra_hoja_no_se_sirve(self):
        self._leer(hoja=("hojaA", "0"), xlsx=_como_api(_CSV))
        self._reinicio()
        self.assertEqual(self._leer(hoja=("hojaB", "7")), [],
                         "con la red caída se sirvieron las recomendaciones de la hoja anterior")

    def test_la_memoria_tampoco_cruza_hojas(self):
        self._leer(hoja=("hojaA", "0"), xlsx=_como_api(_CSV))
        # Sin reinicio: la copia en memoria es de A y está dentro del TTL.
        with mock.patch.object(self.sheet, "get_cmv40_sheet_id_gid",
                               return_value=("hojaB", "7")), \
                mock.patch.object(self.sheet, "_leer_copia", return_value=[]):
            async def _nada(*a):
                return None

            async def _csv_caido():
                raise httpx.ConnectError("sin red")
            with mock.patch.object(self.sheet, "get_google_api_key", return_value=""), \
                    mock.patch.object(self.sheet, "_fetch_sheet_xlsx", _nada), \
                    mock.patch.object(self.sheet, "_fetch_sheet_html", _nada), \
                    mock.patch.object(self.sheet, "_fetch_csv", _csv_caido):
                self.assertEqual(asyncio.run(self.sheet.get_recommendations()), [])


# ── #18 · la carpeta de Drive configurada ─────────────────────────────

class TestElOverrideLegacyDeDrive(ConConfigAislada):

    def test_el_legacy_gana_al_enlace_de_la_app_y_se_anuncia_el_mismo(self):
        legacy = "1LegacyFolderIdDelUsuario"
        os.environ["CMV40_DRIVE_FOLDER_ID"] = legacy
        with mock.patch.object(self.st, "carpeta_drive_de_la_app",
                               return_value="https://drive.google.com/drive/folders/1DeLaAppXXXXXXXX"):
            self.assertEqual(self.st.get_cmv40_drive_folder_id(), legacy,
                             "se lista la carpeta de la app y no la configurada")
            publico = self.st.get_public_settings()["drive_folder"]
            self.assertEqual(publico["source"], "env")
            self.assertEqual(publico["folder_id_last6"], legacy[-6:])
            self.assertFalse(self.st.usando_el_repo_de_la_app())


# ── #19 · «Probar Google» ──────────────────────────────────────────────

class TestProbarGoogleSinCarpeta(ConConfigAislada):

    def _probar(self, sheets_status):
        def manejador(req):
            if "sheets.googleapis.com" in str(req.url):
                return httpx.Response(sheets_status, json={"error": {"message": "API key not valid"}})
            return httpx.Response(200, json={"files": []})
        with mock.patch.object(self.drive, "get_cmv40_drive_folder_id", return_value=""), \
                mock.patch.object(self.drive.httpx, "AsyncClient", _cliente_con(manejador)):
            return asyncio.run(self.drive.test_api_key("una-clave-cualquiera"))

    def test_una_clave_que_sheets_rechaza_no_se_aprueba(self):
        ok, _ = self._probar(400)
        self.assertFalse(ok, "sin carpeta, «no comprobado» contó como aprobado")

    def test_una_clave_que_sheets_acepta_si(self):
        ok, _ = self._probar(200)
        self.assertTrue(ok)


# ── #22 · las actualizaciones ──────────────────────────────────────────

class TestUnLimiteDeGithubNoBorraLoQueSeSabia(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.cache = self.main._UPDATE_CHECK_CACHE_PATH
        self.cache.write_text(json.dumps({
            "fetched_at": time.time() - 7200,           # caducada
            "latest": "v99.0.0", "release_url": "https://x/v99.0.0",
            "release_notes": "notas", "pending_releases": [
                {"tag": "v99.0.0", "body": "notas", "url": "https://x", "published_at": ""}],
            "published_at": "", "checked_at": "2026-10-17T00:00:00+00:00"}))

    def _consultar(self, estado):
        def manejador(req):
            return httpx.Response(estado, json={"message": "API rate limit exceeded"})
        with mock.patch.object(httpx, "AsyncClient", _cliente_con(manejador)):
            r = self.client.get("/api/version/check-updates?force=true")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_un_403_conserva_el_aviso_y_la_cache(self):
        for estado in (403, 429):
            with self.subTest(estado=estado):
                datos = self._consultar(estado)
                self.assertEqual(datos["latest"], "v99.0.0")
                self.assertTrue(datos["update_available"],
                                "el aviso de una versión conocida se apagó")
                self.assertTrue(datos.get("error"), "el fallo de la consulta no se dice")
                self.assertEqual(json.loads(self.cache.read_text())["latest"], "v99.0.0",
                                 "la caché se reescribió con nada")


if __name__ == "__main__":
    unittest.main()
