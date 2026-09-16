"""El idioma se detecta la PRIMERA vez, y el ajuste manual gana siempre.

Decidido con el usuario el 2026-09-16, en dos preguntas:

  1. se detecta en el primer arranque **y también al actualizar desde una
     versión anterior** — de ahí que la señal sea la ausencia de la clave y no
     un número de versión;
  2. el resto es **inglés**: una cabecera que pide italiano o alemán es una
     petición de verdad, y lo que esa persona no lee es castellano.

Lo que estos tests protegen de verdad es la SEÑAL. «Nadie lo ha elegido» es
que `app_settings.json` no tenga la clave `idioma`, y eso solo se sostiene
mientras **nadie más la escriba**: hoy `saveSettings()` compone su payload con
las cuatro claves/URLs y el único POST que manda `idioma` es el del botón. El
día que otro sitio lo arrastre, la detección dejaría de dispararse para todo
el parque que actualiza — en silencio y sin un error. De ahí el último test.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402


class TestElParserDeAcceptLanguage(unittest.TestCase):
    """Función pura: ni lee ni escribe nada."""

    def _d(self, cabecera):
        from services.settings_store import detectar_idioma
        return detectar_idioma(cabecera)

    def test_los_tres_idiomas_por_su_prefijo(self):
        """`es-MX` y `ca-valencia` caen donde tienen que caer."""
        for cabecera, esperado in (
                ("es-ES,es;q=0.9,en;q=0.8", "es"),
                ("es-419", "es"),
                ("es-MX,es;q=0.9", "es"),
                ("ca-ES,ca;q=0.9,es;q=0.8", "ca"),
                ("ca-valencia", "ca"),
                ("en-GB,en;q=0.9", "en"),
                ("en-US", "en"),
        ):
            with self.subTest(cabecera=cabecera):
                self.assertEqual(self._d(cabecera), esperado)

    def test_el_resto_es_ingles(self):
        """La decisión del usuario: quien pide alemán no lee castellano."""
        for cabecera in ("de-DE", "it-IT,it;q=0.9", "fr-FR,fr;q=0.9",
                         "zh-CN,zh;q=0.9", "pt-BR", "ja"):
            with self.subTest(cabecera=cabecera):
                self.assertEqual(self._d(cabecera), "en")

    def test_se_respeta_la_q_no_el_orden(self):
        """`fr;q=0.9, es;q=0.8` pide francés antes que castellano, pero de los
        tres que tenemos el preferido es el castellano — y gana ese, no el
        primero de la lista."""
        self.assertEqual(self._d("fr;q=0.9,es;q=0.8,en;q=0.7"), "es")
        self.assertEqual(self._d("en;q=0.1,es;q=0.9"), "es")
        self.assertEqual(self._d("es;q=0.1,en;q=0.9"), "en")

    def test_una_q_a_cero_no_pide_ese_idioma(self):
        """`q=0` es un rechazo explícito, no una preferencia baja."""
        self.assertEqual(self._d("es;q=0,en;q=0.5"), "en")

    def test_sin_cabecera_no_dice_nada(self):
        """Y `None` NO es lo mismo que castellano: un healthcheck o un `curl`
        sin cabecera no deben dejar el idioma fijado para siempre."""
        for cabecera in (None, "", "   ", "*", "*;q=0.5"):
            with self.subTest(cabecera=cabecera):
                self.assertIsNone(self._d(cabecera))

    def test_una_cabecera_rota_no_lanza(self):
        """Esto lo consume el script BLOQUEANTE del catálogo."""
        for cabecera in ("xx-YY;q=zzz", ";;;", "q=1", "es;;q", ",,,"):
            with self.subTest(cabecera=cabecera):
                self.assertIn(self._d(cabecera), (None, "es", "en", "ca"))


class DetectaCase(unittest.TestCase):
    """Cada test con su `/config` limpio: la señal vive en un fichero."""

    def setUp(self):
        import shutil
        import tempfile
        from services import settings_store as st
        self.st = st
        self.tmp = Path(tempfile.mkdtemp(prefix="idioma_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # `CONFIG_DIR` y `SETTINGS_PATH` se resuelven en el import, así que se
        # parchean los YA resueltos — el mismo motivo por el que `api_harness`
        # no usa variables de entorno. Y `CONFIG_DIR` hace falta además de la
        # ruta porque `_save` monta el directorio antes de escribir.
        self.addCleanup(setattr, st, "SETTINGS_PATH", st.SETTINGS_PATH)
        self.addCleanup(setattr, st, "CONFIG_DIR", st.CONFIG_DIR)
        st.CONFIG_DIR = self.tmp
        st.SETTINGS_PATH = self.tmp / "app_settings.json"
        # El módulo cachea el JSON en una global: sin limpiarla, el primer
        # test deja su contenido al siguiente.
        self.addCleanup(setattr, st, "_cache", None)
        st._cache = None
        parche = mock.patch.dict(os.environ, {"HDO_IDIOMA": ""})
        parche.start()
        self.addCleanup(parche.stop)

    def _escribir(self, datos: dict):
        self.st.SETTINGS_PATH.write_text(json.dumps(datos), encoding="utf-8")
        self.st._cache = None


class TestCuandoSeDetecta(DetectaCase):

    def test_instalacion_nueva_sin_fichero(self):
        self.assertEqual(self.st.fijar_idioma_detectado("en-GB,en;q=0.9"), "en")
        self.assertEqual(self.st.get_idioma(), "en")

    def test_actualizacion_desde_una_version_anterior(self):
        """El caso que pidió el usuario: el `app_settings.json` ya existe con
        las claves de siempre, pero **sin `idioma`** porque esa clave no
        existía antes de que la app hablara tres idiomas."""
        self._escribir({"tmdb_api_key": "0" * 32,
                        "cmv40_sheet_url": "https://example.test/x"})
        self.assertEqual(self.st.fijar_idioma_detectado("ca-ES,ca;q=0.9"), "ca")
        self.assertEqual(self.st.get_idioma(), "ca")
        # Y no se lleva por delante lo que ya había.
        guardado = json.loads(self.st.SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(guardado["tmdb_api_key"], "0" * 32)

    def test_el_ajuste_manual_gana_y_no_se_toca(self):
        self._escribir({"idioma": "es"})
        self.assertIsNone(self.st.fijar_idioma_detectado("de-DE"))
        self.assertEqual(self.st.get_idioma(), "es")

    def test_hdo_idioma_del_env_tambien_gana(self):
        """Ponerlo en el `.env` es tan deliberado como pulsar el botón."""
        with mock.patch.dict(os.environ, {"HDO_IDIOMA": "ca"}):
            self.assertIsNone(self.st.fijar_idioma_detectado("en-GB"))
            self.assertEqual(self.st.get_idioma(), "ca")
        # Y sigue sin elegirse: quitar la variable vuelve al default.
        self.assertFalse(self.st.idioma_elegido())

    def test_sin_cabecera_no_se_fija_nada(self):
        """Si el primer GET es un healthcheck, la instalación NO se queda
        marcada: el siguiente navegador todavía puede detectarse."""
        self.assertIsNone(self.st.fijar_idioma_detectado(None))
        self.assertFalse(self.st.idioma_elegido())
        self.assertEqual(self.st.get_idioma(), "es")
        self.assertEqual(self.st.fijar_idioma_detectado("it-IT"), "en")

    def test_solo_se_detecta_una_vez(self):
        self.assertEqual(self.st.fijar_idioma_detectado("en-GB"), "en")
        self.assertIsNone(self.st.fijar_idioma_detectado("ca-ES"))
        self.assertEqual(self.st.get_idioma(), "en")


class TestNadieMasEscribeLaClave(unittest.TestCase):
    """La señal es la ausencia de `idioma`, así que quien la escriba de más
    deja sin detección a todo el parque que actualiza — y en silencio.

    `update_idioma` sí la escribe, y es su trabajo: lo llama el endpoint de
    ajustes con lo que manda el botón. Lo que se vigila es que el POST de
    ⚙︎ **no la arrastre** cuando el usuario solo venía a guardar una API key.
    """

    def test_el_guardado_de_ajustes_no_manda_idioma(self):
        from frontend_sources import rutas
        fuente = ""
        for r in rutas():
            if Path(r).name == "settings.js":
                fuente = Path(r).read_text(encoding="utf-8")
        self.assertTrue(fuente, "no se encontró settings.js")
        i = fuente.index("async function saveSettings(")
        j = fuente.index("\nasync function ", i + 10)
        cuerpo = fuente[i:j if j > 0 else len(fuente)]
        self.assertNotIn("idioma", cuerpo, (
            "`saveSettings()` manda `idioma` en el payload: con eso, guardar "
            "una API key\nescribe la clave y la detección no vuelve a "
            "dispararse — ni al actualizar."))

    def test_el_unico_post_de_idioma_es_el_del_boton(self):
        from frontend_sources import rutas
        sitios = []
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            for i, linea in enumerate(src.splitlines(), 1):
                if "idioma:" in linea and "JSON.stringify" in linea:
                    sitios.append(f"{Path(r).name}:{i}")
        self.assertEqual(len(sitios), 1, (
            f"se esperaba UN solo POST con `idioma` (el de `cambiarIdioma`), "
            f"hay {len(sitios)}: {sitios}"))


class TestElCatalogoDisparaLaDeteccion(ApiTestCase):
    """El cableado, por HTTP. Los tests de arriba prueban la FUNCIÓN; este
    prueba que el endpoint la llama y que el catálogo que sirve ya viene en el
    idioma detectado — que es el punto de hacerlo en el servidor: sin esto
    haría falta una recarga.
    """

    def setUp(self):
        super().setUp()
        from services import settings_store as st
        self.st = st
        st._cache = None
        self.addCleanup(setattr, st, "_cache", None)

    def _catalogo(self, cabecera: str | None):
        h = {"Accept-Language": cabecera} if cabecera is not None else {}
        return self.client.get("/api/i18n/catalogo.js", headers=h)

    def test_el_primer_get_detecta_y_sirve_ese_catalogo(self):
        r = self._catalogo("en-GB,en;q=0.9")
        self.assertEqual(r.status_code, 200)
        self.assertIn('window.__I18N = {idioma: "en"', r.text)
        self.assertEqual(self.st.get_idioma(), "en")

    def test_el_segundo_get_ya_no_cambia_nada(self):
        self._catalogo("en-GB")
        r = self._catalogo("ca-ES,ca;q=0.9")
        self.assertIn('window.__I18N = {idioma: "en"', r.text)

    def test_un_get_sin_cabecera_no_marca_la_instalacion(self):
        """El healthcheck y el `curl` no deben fijar el idioma."""
        r = self._catalogo(None)
        self.assertIn('window.__I18N = {idioma: "es"', r.text)
        self.assertFalse(self.st.idioma_elegido())

    def test_una_cabecera_rota_no_tumba_el_script_bloqueante(self):
        """Si esto devolviera un 500, la app no cargaría."""
        r = self._catalogo("esto;;no;;es=una;;cabecera,,,q=")
        self.assertEqual(r.status_code, 200)
        self.assertIn("window.__I18N", r.text)


if __name__ == "__main__":
    unittest.main()
