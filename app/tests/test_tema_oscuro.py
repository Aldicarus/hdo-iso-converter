"""El tema se elige, se guarda y se aplica antes del primer pintado.

Tres piezas que se pueden romper por separado y ninguna avisa al romperse:

* **el ajuste** (`settings_store`), que es la fuente de verdad;
* **el script bloqueante** `/api/tema.js`, que lo aplica ANTES de que se
  pinte nada — si llegara tarde, cada carga enseñaría la app en claro
  durante unos fotogramas, que es exactamente el defecto que el modo oscuro
  viene a evitar;
* y **`aplicarTema`**, que resuelve `sistema` contra `prefers-color-scheme`.

Lo de `sistema` se prueba **ejecutando el JS que el endpoint emite**, no una
copia: las dos funciones viven en `main.py` como texto, así que un test que
reescribiera su lógica aquí estaría comprobando la copia. Se saca el cuerpo
real por HTTP y se evalúa en node con un `document` y un `matchMedia` de
mentira.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_tema_oscuro -v
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase                          # noqa: E402
import frontend_sources                                      # noqa: E402

NODE = shutil.which("node")


class TestElAjusteDelTema(ApiTestCase):

    def _leer(self):
        r = self.client.get("/api/settings")
        self.assertEqual(r.status_code, 200)
        return r.json()["tema"]

    def test_arranca_siguiendo_al_sistema(self):
        """Decisión del usuario (2026-09-23), que cambió la del 21.

        Nació en `claro` para que actualizar no le cambiara el aspecto a
        nadie. Ese argumento se agota solo: dos días después el parque ya
        conoce el oscuro y lo que queda es que quien tiene el ordenador en
        oscuro abra la app en blanco y tenga que ir a ⚙︎ a decirlo.
        """
        t = self._leer()
        self.assertEqual(t["activo"], "sistema")
        self.assertEqual(t["por_defecto"], "sistema")
        self.assertEqual(t["disponibles"], ["claro", "oscuro", "sistema"])

    def test_quien_ya_eligio_no_se_mueve(self):
        """El cambio de default solo alcanza a quien nunca tocó el ajuste.

        Es lo que hace que cambiarlo sea barato: `app_settings.json` gana
        sobre el default, así que una instalación con el tema elegido sigue
        exactamente igual tras actualizar.
        """
        self.client.post("/api/settings", json={"tema": "claro"})
        self.assertEqual(self._leer()["activo"], "claro")
        self.assertEqual(self._leer()["por_defecto"], "sistema")

    def test_se_guarda_y_se_devuelve(self):
        r = self.client.post("/api/settings", json={"tema": "oscuro"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tema"]["activo"], "oscuro")
        self.assertEqual(self._leer()["activo"], "oscuro")

    def test_un_tema_inventado_se_ignora_sin_tumbar_el_resto_del_POST(self):
        """Mismo criterio que el idioma: esto recibe lo que mande el
        cliente, y un valor raro no puede llevarse por delante el guardado
        de las cuatro claves que viajan en el mismo cuerpo."""
        self.client.post("/api/settings", json={"tema": "oscuro"})
        r = self.client.post("/api/settings",
                             json={"tema": "sepia", "cmv40_sheet_url": ""})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tema"]["activo"], "oscuro")

    def test_el_script_lleva_el_tema_guardado(self):
        self.client.post("/api/settings", json={"tema": "claro"})
        r = self.client.get("/api/tema.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn("javascript", r.headers["content-type"])
        # `no-store`: cambia en cuanto se toca el ajuste.
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        self.assertIn('window.__TEMA_PREF = "claro"', r.text)

    def test_el_script_no_falla_si_el_ajuste_no_se_puede_leer(self):
        """Es el script BLOQUEANTE: caer al default es un inconveniente,
        una pantalla en blanco no."""
        self.client.post("/api/settings", json={"tema": "oscuro"})
        from unittest import mock
        with mock.patch("services.settings_store.get_tema",
                        side_effect=RuntimeError("disco")):
            r = self.client.get("/api/tema.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn('window.__TEMA_PREF = "sistema"', r.text)


@unittest.skipUnless(NODE, "sin node")
class TestAplicarTemaResuelveSistema(ApiTestCase):
    """El JS REAL del endpoint, ejecutado."""

    def _correr(self, pref, sistema_oscuro):
        self.client.post("/api/settings", json={"tema": pref})
        cuerpo = self.client.get("/api/tema.js").text
        guion = """
        var _cambios = [];
        global.document = {documentElement: {dataset: {}}};
        global.window = global;
        global.matchMedia = function (q) {
          return {matches: %s, addEventListener: function (_, f) { _cambios.push(f); }};
        };
        %s
        console.log(JSON.stringify({
          tema: document.documentElement.dataset.tema,
          pref: document.documentElement.dataset.temaPref,
          escucha: _cambios.length,
        }));
        """ % ("true" if sistema_oscuro else "false", cuerpo)
        out = subprocess.run(frontend_sources.argv_node(guion),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_elegir_un_tema_manda_sobre_el_sistema(self):
        for pref in ("claro", "oscuro"):
            for sistema in (True, False):
                with self.subTest(pref=pref, sistema_oscuro=sistema):
                    r = self._correr(pref, sistema)
                    self.assertEqual(r["tema"], pref)
                    self.assertEqual(r["pref"], pref)

    def test_sistema_se_resuelve_contra_prefers_color_scheme(self):
        self.assertEqual(self._correr("sistema", True)["tema"], "oscuro")
        self.assertEqual(self._correr("sistema", False)["tema"], "claro")

    def test_con_sistema_puesto_se_escucha_el_cambio_del_sistema_operativo(self):
        """Es lo que distingue «seguir al sistema» de haber elegido: cambiar
        la apariencia del Mac tiene que cambiar la de la app sin recargar."""
        self.assertEqual(self._correr("sistema", False)["escucha"], 1)

    def test_la_preferencia_guardada_es_la_PREFERENCIA_no_el_color(self):
        """Si se guardara el color resuelto, poner el Mac en claro dejaría la
        app en claro para siempre aunque el ajuste dijera «sistema»."""
        r = self._correr("sistema", True)
        self.assertEqual(r["pref"], "sistema")
        self.assertEqual(r["tema"], "oscuro")


class TestElMarcadoCablea(unittest.TestCase):
    """Lo que hace que todo lo anterior sirva de algo.

    Se busca la ETIQUETA `<script src=…>`, no la cadena `/api/tema.js`. Con
    la cadena, el guard casaba con el comentario HTML que hay justo encima
    —que cita la ruta para explicar por qué va ahí— y **pasaba en verde con
    el script borrado**: lo destapó una mutación. Una mención no es una
    referencia.
    """

    ETIQUETA = re.compile(r'<script\b[^>]*\bsrc="/api/tema\.js[^"]*"[^>]*>')

    @classmethod
    def setUpClass(cls):
        cls.html = frontend_sources.html()
        cls.tags = cls.ETIQUETA.findall(cls.html)

    def test_el_script_esta_y_una_sola_vez(self):
        self.assertEqual(len(self.tags), 1,
                         f"etiquetas <script> de /api/tema.js: {len(self.tags)}")

    def test_va_antes_que_la_hoja_de_estilos(self):
        """Si fuera después habría un fotograma con la app en claro."""
        i = self.ETIQUETA.search(self.html).start()
        self.assertLess(i, self.html.index("style.css"),
                        "el tema se aplicaría después de pintar")

    def test_va_dentro_del_head(self):
        self.assertLess(self.ETIQUETA.search(self.html).start(),
                        self.html.index("</head>"))

    def test_no_es_async_ni_defer(self):
        """Los dos lo sacarían del camino crítico, que es justo lo que este
        script NO puede permitirse."""
        self.assertNotIn("async", self.tags[0])
        self.assertNotIn("defer", self.tags[0])

    def test_lleva_el_mismo_token_de_cache_que_el_resto(self):
        """Un asset con el token viejo y los demás con el nuevo es peor que
        no tener token: el desajuste es parcial."""
        tokens = set(re.findall(r"\?v=([0-9a-z]+)", self.html))
        self.assertEqual(len(tokens), 1, f"tokens distintos en index.html: {tokens}")


if __name__ == "__main__":
    unittest.main()
