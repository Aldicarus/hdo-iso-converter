"""El token de cache-bust de index.html tiene que ser coherente.

`index.html` carga `app.js` y `style.css` con `?v=<token>`. Si el token no sube
al tocar esos ficheros, el navegador sirve el frontend viejo cacheado contra el
backend nuevo — y el síntoma es un "bug raro" que no aparece en el código.
Caso real (2026-06-06): el token llevaba semanas congelado y el usuario estaba
ejecutando JS de un mes antes.

Un test no puede saber si el token se subió lo suficiente, pero sí puede cazar
el error típico: tocar una de las dos referencias y olvidar la otra, que deja
el CSS y el JS en versiones distintas.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_frontend_cache_bust -v
"""
import re
import sys
import unittest
from pathlib import Path

# `frontend_sources` se importa DENTRO de un test, y sin esto solo
# funciona por accidente: en `discover` otro módulo ya ha metido este
# directorio en `sys.path`, pero ejecutando este módulo solo revienta.
sys.path.insert(0, str(Path(__file__).resolve().parent))

STATIC = Path(__file__).resolve().parents[1] / "static"
INDEX = STATIC / "index.html"

_REF = re.compile(r'/static/([\w.-]+)\?v=([\w.-]+)')


class TestCacheBust(unittest.TestCase):

    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")
        self.refs = _REF.findall(self.html)

    def test_todas_las_piezas_del_js_y_el_css_llevan_token(self):
        """Con el token en siete de las ocho piezas, el navegador sirve una
        vieja contra siete nuevas: peor que no tener token, porque el desajuste
        es parcial.

        La lista se fija a mano a propósito: añadir una pieza tiene que obligar
        a mirar este test, que es donde está escrito por qué el token es uno
        solo.
        """
        ficheros = {f for f, _ in self.refs}
        self.assertIn("style.css", ficheros, "style.css debe cargarse con ?v=")
        js = sorted(f for f in ficheros if f.endswith(".js"))
        self.assertEqual(js, ["browser.js", "cmv40_modals.js", "core.js",
                              "i18n.js", "settings.js", "tab1.js", "tab2.js",
                              "tab3.js", "workbar.js"],
                         "faltan piezas del JS en index.html (o hay de más)")

    def test_el_catalogo_del_idioma_hereda_el_token(self):
        """`i18n/{es,en,ca}.json` se piden con `?v=`, y el token NO se pasa a
        mano desde el HTML: `i18n.js` lo lee de su propio `src`.

        Escribirlo a mano sería una décima referencia al token que mantener
        sincronizada, y el desajuste PARCIAL es peor que no tener token —el
        navegador serviría un catálogo viejo contra un JS nuevo, o sea claves
        crudas en pantalla.
        """
        # Por `js_completo()` y no leyendo `i18n.js` por su ruta: la regla del
        # proyecto es que ningún test enumere las piezas a mano, porque la
        # lista se desincroniza del HTML en el primer cambio.
        from frontend_sources import js_completo
        fuente = js_completo()
        self.assertIn("document.currentScript", fuente)
        self.assertIn("searchParams.get('v')", fuente)
        self.assertIn("?v=${token", fuente,
                      "el fetch del catálogo no lleva el token")

    def test_no_queda_rastro_del_app_js_monolitico(self):
        self.assertFalse((STATIC / "app.js").exists(),
                         "app.js se partió en siete; un fichero suelto con ese "
                         "nombre es una copia que nadie carga")
        # Sobre las referencias, no sobre el texto: el HTML menciona el
        # `app.js` original en el comentario que explica el corte.
        self.assertNotIn("app.js", {f for f, _ in self.refs})

    def test_todas_las_referencias_comparten_el_mismo_token(self):
        tokens = {t for _, t in self.refs}
        self.assertEqual(len(tokens), 1,
                         f"tokens de cache-bust distintos entre assets: "
                         f"{dict((f, t) for f, t in self.refs)}")

    def test_los_ficheros_referenciados_existen(self):
        for fichero, _ in self.refs:
            self.assertTrue((STATIC / fichero).is_file(),
                            f"index.html carga /static/{fichero}, que no existe")

    def test_no_quedan_referencias_sin_token(self):
        """Un asset propio sin `?v=` se queda cacheado para siempre."""
        sin_token = re.findall(r'/static/([\w.-]+\.(?:js|css))(?!\?v=)', self.html)
        self.assertEqual(sin_token, [],
                         f"assets locales sin cache-bust: {sin_token}")


if __name__ == "__main__":
    unittest.main()
