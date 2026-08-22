"""El corte de `app.js` en siete scripts, y lo que podía romper sin avisar.

`app.js` eran 18.687 líneas. Ahora son siete scripts **clásicos** (no
módulos), cargados en orden por `index.html`. Se eligió así, y no
`type="module"`, por un motivo medible: **154 nombres de función se referencian
desde atributos inline** (`onclick="openProject(…)"`) en `index.html` y en las
plantillas que el propio JS emite con `innerHTML`. En un script clásico las
declaraciones `function` van a `globalThis` y siguen funcionando; con
`type="module"` quedan encerradas en el ámbito del módulo y haría falta un
bloque de 154 `window.foo = foo` que hay que mantener a mano — cada botón nuevo
moriría en silencio hasta que alguien lo pulse.

Concatenar scripts clásicos en orden es equivalente al fichero único, con tres
salvedades, y las tres tienen su test aquí:

  1. **`'use strict'` es por script.** El original lo tenía en la línea 1; si
     una pieza se queda sin él, esa parte pasa a modo sloppy — los globals
     accidentales dejan de lanzar y `this` cambia en las funciones sueltas. Un
     cambio de semántica que no da ningún error.
  2. **El hoisting es por script.** Una sentencia top-level que llame a algo
     declarado en una pieza POSTERIOR ve `undefined` en vez de la función. En
     un fichero único funcionaba.
  3. **Una declaración duplicada** que antes se sombreaba en silencio ahora
     puede caer en piezas distintas. (Había una: `_doFilterSidebarSessions`
     estaba declarada dos veces y la primera —sin filtro de estado ni
     ordenación— era código muerto. Se quitó.)

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_frontend_troceado -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import html, js_completo, piezas, rutas  # noqa: E402

NODE = shutil.which("node")
_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)

# `onclick="foo(`, `onchange='bar(`, y las mismas dentro de plantillas del JS
# (donde las comillas van escapadas).
_HANDLER_RE = re.compile(r"""on[a-z]+=\\?["']\s*([A-Za-z_$][\w$]*)\s*\(""")
_DECL_RE = re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", re.M)
_TOP_DECL_RE = re.compile(
    r"^(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)", re.M)


def _handlers() -> set[str]:
    nombres = set(_HANDLER_RE.findall(html())) | set(_HANDLER_RE.findall(js_completo()))
    return {n for n in nombres if n not in {"if", "return", "typeof", "new"}}


class TestLosHandlersInlineSiguenSiendoGlobales(unittest.TestCase):

    def test_hay_muchos_y_por_eso_no_son_modulos(self):
        """Si algún día bajan a un puñado, `type="module"` volvería a estar
        sobre la mesa. Con 150+, no."""
        self.assertGreater(len(_handlers()), 100,
                           "¿se quitaron los handlers inline? revisar la "
                           "decisión de no usar módulos")

    def test_cada_handler_inline_esta_declarado_en_alguna_pieza(self):
        declaradas = set(_DECL_RE.findall(js_completo()))
        huerfanos = sorted(_handlers() - declaradas)
        self.assertEqual(huerfanos, [],
                         f"handlers inline sin función que los respalde: "
                         f"{huerfanos}")

    def test_ninguna_pieza_usa_import_ni_export(self):
        """Un `import`/`export` en un script clásico es un SyntaxError que se
        lleva por delante el fichero entero."""
        for ruta in rutas():
            src = ruta.read_text(encoding="utf-8")
            for i, linea in enumerate(src.splitlines(), 1):
                limpia = linea.strip()
                if limpia.startswith(("import ", "export ", "export{", "import{")):
                    self.fail(f"{ruta.name}:{i} — {limpia[:70]}")


class TestLasTresSalvedadesDeConcatenar(unittest.TestCase):

    def test_todas_las_piezas_declaran_use_strict(self):
        for ruta in rutas():
            primera = ruta.read_text(encoding="utf-8").lstrip().splitlines()[0]
            self.assertIn("use strict", primera,
                          f"{ruta.name} arrancaría en modo sloppy")

    def test_no_hay_declaraciones_top_level_duplicadas(self):
        vistas, dups = {}, []
        for ruta in rutas():
            for nombre in _TOP_DECL_RE.findall(ruta.read_text(encoding="utf-8")):
                if nombre in vistas:
                    dups.append(f"{nombre} ({vistas[nombre]} y {ruta.name})")
                else:
                    vistas[nombre] = ruta.name
        self.assertEqual(dups, [], f"declaraciones repetidas: {dups}")

    def test_ninguna_sentencia_top_level_usa_algo_de_una_pieza_posterior(self):
        """LA salvedad que de verdad muerde: el hoisting es por script.

        Se recorren las piezas en orden de carga acumulando lo declarado, y por
        cada sentencia ejecutable a nivel top se comprueba que los nombres que
        menciona ya estén disponibles.
        """
        declaradas: set[str] = set()
        problemas = []
        for ruta in rutas():
            src = ruta.read_text(encoding="utf-8")
            propias = set(_TOP_DECL_RE.findall(src))
            for i, linea in enumerate(src.splitlines(), 1):
                if not linea or linea[0].isspace() or linea.startswith(("//", "/*", "*", "}", ")", "]")):
                    continue
                if _TOP_DECL_RE.match(linea):
                    continue
                # sentencia ejecutable: qué identificadores menciona
                for nombre in re.findall(r"\b([A-Za-z_$][\w$]*)\s*[,)]", linea):
                    if nombre in declaradas or nombre in propias:
                        continue
                    # solo interesan los que ALGUNA pieza posterior declara
                    if any(nombre in set(_TOP_DECL_RE.findall(
                            r.read_text(encoding="utf-8")))
                            for r in rutas()[rutas().index(ruta) + 1:]):
                        problemas.append(f"{ruta.name}:{i} usa {nombre}, "
                                         f"declarado más tarde")
        self.assertEqual(problemas, [], "; ".join(problemas))


class TestNadieLeeUnaPiezaSuelta(unittest.TestCase):
    """Los tests del frontend van por `frontend_sources`, no por la ruta.

    Nueve módulos leían `static/app.js` directamente. Al partirlo, siete
    fallaron en el acto (fichero inexistente) pero **dos se me escaparon al
    buscarlos con grep** porque escribían la ruta de otra forma. El problema de
    fondo es peor que un fichero que no existe: un test que lea SOLO `tab3.js`
    pasa en verde el día que la función que vigila se mueve a otra pieza, y
    nadie se enterará.
    """

    def test_ningun_test_lee_un_js_del_frontend_por_su_ruta(self):
        nombres = {n for n, _ in piezas()} | {"app.js"}
        culpables = []
        aqui = Path(__file__).name
        for ruta in sorted(Path(__file__).parent.glob("*.py")):
            if ruta.name in (aqui, "frontend_sources.py"):
                continue
            src = ruta.read_text(encoding="utf-8")
            for i, linea in enumerate(src.splitlines(), 1):
                if "read_text" not in linea and "readFileSync" not in linea:
                    continue
                for nombre in nombres:
                    if f'"{nombre}"' in linea or f"'{nombre}'" in linea:
                        culpables.append(f"{ruta.name}:{i} lee {nombre}")
        self.assertEqual(culpables, [],
                         "usar `frontend_sources.js_completo()`: " + "; ".join(culpables))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestLaPaginaRealCarga(unittest.TestCase):
    """La prueba de fuego: abrir `index.html` en Chrome y mirar el resultado.

    Es el único test que ejecuta las siete piezas juntas en un navegador de
    verdad, con el orden y el `?v=` que dice el HTML. Un error de carga en una
    pieza (SyntaxError, TDZ, referencia a algo de una pieza posterior) sale
    aquí y no sale en ningún otro sitio.
    """

    @classmethod
    def setUpClass(cls):
        # Las funciones se comprueban sobre `window`; los errores, sobre lo que
        # el navegador haya registrado durante la carga.
        sonda = """
<script>
window.__errores = [];
window.addEventListener('error', e => window.__errores.push(
  (e.message || '') + ' @ ' + (e.filename || '').split('/').pop() + ':' + e.lineno));
</script>
"""
        pagina = html().replace("</head>", sonda + "</head>")
        # El volcado va después de que los siete scripts hayan corrido.
        nombres = json.dumps(sorted(_handlers()))
        volcado = f"""
<pre id="__out"></pre>
<script>
(function () {{
  const faltan = {nombres}.filter(n => typeof window[n] !== 'function');
  document.getElementById('__out').textContent = JSON.stringify(
    {{errores: window.__errores, faltan: faltan,
      piezas: {json.dumps([n for n, _ in piezas()])}}});
}})();
</script>
"""
        pagina = pagina.replace("</body>", volcado + "</body>")
        # En el mismo directorio que los scripts: los `src` son absolutos
        # (`/static/...`), así que se reescriben a relativos para file://.
        pagina = pagina.replace('src="/static/', 'src="').replace(
            'href="/static/', 'href="')
        cls._tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".html", delete=False, encoding="utf-8",
            dir=str(APP_DIR / "static"))
        cls._tmp.write(pagina)
        cls._tmp.close()
        salida = subprocess.run(
            [CHROME, "--headless", "--disable-gpu",
             "--allow-file-access-from-files", "--dump-dom",
             "--virtual-time-budget=4000", cls._tmp.name],
            capture_output=True, text=True, timeout=120).stdout
        m = re.search(r'<pre id="__out">(.*?)</pre>', salida, re.S)
        if not m:
            raise unittest.SkipTest("Chrome no devolvió el volcado")
        import html as _h
        cls.datos = json.loads(_h.unescape(m.group(1)))

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_tmp", None):
            os.unlink(cls._tmp.name)

    def test_las_siete_piezas_cargan_sin_un_solo_error(self):
        self.assertEqual(self.datos["errores"], [],
                         "errores de JS durante la carga de la página")

    def test_los_154_handlers_estan_en_window(self):
        self.assertEqual(self.datos["faltan"], [],
                         "handlers inline que NO son funciones globales: el "
                         "botón se ve y no hace nada")

    def test_el_html_carga_las_siete(self):
        self.assertEqual(len(self.datos["piezas"]), 7)


if __name__ == "__main__":
    unittest.main()
