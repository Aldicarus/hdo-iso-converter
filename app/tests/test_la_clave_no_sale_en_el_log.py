"""La API key de Google no acaba en `docker logs`, y un bin a medias no se queda.

Las APIs de Google llevan la clave como parámetro de la query y httpx la
incrusta en el `str()` de un `HTTPStatusError`. El listado de Drive registraba
la excepción cruda, así que cualquier 429/404/5xx dejaba la clave en el log
—que se pega en informes— y Google desactiva las claves que encuentra
publicadas. TMDb ya lo evitaba (`_safe_err`); Drive y la hoja, no (auditoría
con Codex del 2026-10-17, reproducido con un 429).

Y la descarga de un bin no tenía tope de inactividad ni borraba lo escrito si
el stream se cortaba.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_la_clave_no_sale_en_el_log -v
"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

CLAVE = "clave-de-prueba-que-no-tiene-que-salir-en-el-log"


def _cliente_con(manejador):
    real = httpx.AsyncClient

    def fabrica(*a, **k):
        k.pop("transport", None)
        return real(*a, transport=httpx.MockTransport(manejador), **k)
    return fabrica


class TestElListadoDeDrive(unittest.TestCase):

    def test_un_429_no_escribe_la_clave(self):
        from services import rec999_drive as drive
        with mock.patch.object(drive, "get_google_api_key", return_value=CLAVE), \
                mock.patch.object(drive, "get_cmv40_drive_folder_id", return_value="carpeta"), \
                mock.patch.object(drive, "_load_cache", return_value=None), \
                mock.patch.object(drive.httpx, "AsyncClient",
                                  _cliente_con(lambda r: httpx.Response(429, request=r))), \
                self.assertLogs(drive._logger, level="WARNING") as registro:
            asyncio.run(drive.list_bin_files(force_refresh=True))
        texto = "\n".join(registro.output)
        self.assertIn("429", texto, "el aviso tiene que seguir diciendo qué pasó")
        self.assertNotIn(CLAVE, texto)


class TestElRedactor(unittest.TestCase):

    def test_tapa_key_y_api_key_y_deja_lo_demas(self):
        from services.settings_store import redactar_claves
        texto = ("Client error '429' for url 'https://x/files?q=a&key=" + CLAVE
                 + "&pageSize=1000' · https://api.themoviedb.org/3/x?api_key=abc123")
        limpio = redactar_claves(texto)
        self.assertNotIn(CLAVE, limpio)
        self.assertNotIn("abc123", limpio)
        self.assertIn("pageSize=1000", limpio)
        self.assertIn("429", limpio)


class TestLaDescargaDeUnBin(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="descarga_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _descargar(self, manejador):
        from services import rec999_drive as drive
        destino = self.tmp / "RPU_target.bin"
        with mock.patch.object(drive, "get_google_api_key", return_value=CLAVE), \
                mock.patch.object(drive, "registrar_descarga_de_bin"), \
                mock.patch.object(drive.httpx, "AsyncClient", _cliente_con(manejador)):
            asyncio.run(drive.download_file("id", destino))
        return destino

    def test_un_stream_que_se_corta_no_deja_el_bin_a_medias(self):
        def corto(r):
            return httpx.Response(200, headers={"content-length": "1000"}, content=b"x" * 400,
                                  request=r)
        with self.assertRaises(Exception):
            self._descargar(corto)
        self.assertFalse((self.tmp / "RPU_target.bin").exists(), "queda un bin truncado")

    def test_una_descarga_entera_se_queda(self):
        destino = self._descargar(lambda r: httpx.Response(
            200, headers={"content-length": "1000"}, content=b"x" * 1000, request=r))
        self.assertEqual(destino.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
