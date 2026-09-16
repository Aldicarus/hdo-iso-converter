"""El catálogo tiene que estar ANTES de que se parseen los scripts.

`tr()` dentro de una constante de MÓDULO se evalúa al parsear el script. El
catálogo llegaba por un `fetch` asíncrono que se resuelve después, así que
`tr()` devolvía la clave y la dejaba **congelada para siempre** en la
constante. Medido: **43 claves crudas en 11 constantes** —
`_CMV40_PIPELINE_PREVIEW`, `CMV40_PHASE_LABELS`, `ESTADO_TEXTO`… — y en
pantalla se leía `tab3.fase_h`.

Ningún guard lo veía, y conviene entender por qué: `clavesAusentes()` está
vacío —las claves **existen**— y `pintarTextos` pinta sus cientos de nodos,
así que `TestLaAppCargaEnLosTresIdiomas` pasaba en verde mientras media
interfaz mostraba nombres de clave. Lo que fallaba no era QUÉ se pedía, sino
CUÁNDO.

El arreglo es `/api/i18n/catalogo.js`: un `<script src>` clásico es síncrono y
bloqueante, así que va antes de los ocho y `tr()` funciona desde la primera
línea. Este test carga los ocho scripts en node con y sin esa siembra: el
contraste es lo que demuestra que la siembra hace algo.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_catalogo_al_parsear -v
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

from frontend_sources import catalogo_es, html, js_completo, piezas  # noqa: E402

NODE = shutil.which("node")

# Las constantes de módulo que resuelven texto. La lista se comprueba: una
# entrada que ya no exista hace fallar un test de aquí abajo.
CONSTANTES = [
    "CMV40_PHASE_LABELS", "CMV40_RUNNING_LABELS", "_CMV40_PIPELINE_PREVIEW",
    "ESTADO_TEXTO", "CMV40_CHIP_META", "CMV40_VERDICT_STYLE",
    "CMV40_FASES_DEF", "_CMV40_TARGET_TYPE_LABELS", "_CMV40_FIN",
    "_MOTIVO_SIN_LOG", "CMV40_SHEET_SECTION_LABEL", "ROOTS_MKV",
]

_CLAVE = re.compile(
    r'"((?:ui|core|tab1|tab2|tab3|settings|workbar|browser|cmv40_modals|comun)'
    r'\.[a-z0-9_.]{3,})"')

_ENTORNO = """globalThis.window = globalThis;
globalThis.document = {addEventListener(){}, getElementById:()=>null,
  querySelector:()=>null, querySelectorAll:()=>[], documentElement:{},
  createElement:()=>({style:{},classList:{add(){},remove(){}},appendChild(){},
  setAttribute(){}}), head:{appendChild(){}}, body:{appendChild(){}}};
globalThis.localStorage = {getItem:()=>null,setItem(){}};
globalThis.location = {href:'http://x/', search:''};
globalThis.fetch = () => new Promise(()=>{});
globalThis.WebSocket = function(){this.close=()=>{};};
globalThis.MutationObserver = function(){this.observe=()=>{};};
globalThis.setInterval = ()=>0; globalThis.setTimeout = ()=>0;
globalThis.Sortable = undefined;
globalThis.navigator = {clipboard:{}};
"""


@unittest.skipIf(NODE is None, "node no está instalado")
class ConstantesCase(unittest.TestCase):

    @classmethod
    def _correr(cls, sembrar: bool) -> dict:
        cat = json.dumps(catalogo_es(), ensure_ascii=False)
        pre = _ENTORNO + (
            f"window.__I18N = {{idioma:'es', catalogo:{cat}}};\n" if sembrar else "")
        post = "\nconst _o = {};\n" + "".join(
            f"try{{_o[{json.dumps(c)}] = JSON.stringify({c});}}"
            f"catch(e){{_o[{json.dumps(c)}] = null;}}\n" for c in CONSTANTES
        ) + "console.log(JSON.stringify(_o));\n"
        # A fichero y no por `-e`: con el catálogo dentro, el argv se pasa del
        # límite del sistema (`Argument list too long`).
        f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                        encoding="utf-8")
        f.write(pre + js_completo() + post)
        f.close()
        try:
            r = subprocess.run([NODE, f.name], capture_output=True, text=True,
                               timeout=120)
        finally:
            os.unlink(f.name)
        if r.returncode != 0:
            raise AssertionError(f"node falló:\n{r.stderr[:900]}")
        return json.loads(r.stdout.strip().splitlines()[-1])

    @staticmethod
    def _crudas(volcado: dict) -> dict[str, list[str]]:
        fuera = {}
        for c, v in volcado.items():
            ks = sorted(set(_CLAVE.findall(v or "")))
            if ks:
                fuera[c] = ks
        return fuera


class TestConElCatalogoSembradoNoQuedaNingunaClave(ConstantesCase):

    def test_las_constantes_guardan_texto_y_no_nombres_de_clave(self):
        crudas = self._crudas(self._correr(sembrar=True))
        detalle = "\n  · ".join(f"{c}: {ks[:4]}" for c, ks in crudas.items())
        self.assertEqual(crudas, {}, (
            f"\n{sum(len(v) for v in crudas.values())} clave(s) congeladas en "
            f"una constante de módulo.\n`tr()` ahí se evalúa al PARSEAR: o el "
            f"catálogo llega antes, o el valor\nse resuelve en el consumidor."
            f"\n  · {detalle}"))


class TestElContrasteQueJustificaLaSiembra(ConstantesCase):
    """Sin esto, el test de arriba podría pasar por otro motivo."""

    def test_sin_sembrar_las_constantes_SI_guardan_claves(self):
        crudas = self._crudas(self._correr(sembrar=False))
        self.assertGreater(sum(len(v) for v in crudas.values()), 20, (
            "sin el catálogo síncrono debería haber decenas de claves "
            "congeladas; si no las hay, el arnés ya no mide lo que cree"))


class TestElCableadoDeLaSiembra(unittest.TestCase):
    """Tres puntas que se pueden caer en silencio."""

    def test_el_html_pide_el_catalogo_ANTES_de_i18n_js(self):
        h = html()
        i = h.find('/api/i18n/catalogo.js')
        j = h.find('/static/i18n.js')
        self.assertNotEqual(i, -1, "`index.html` no pide `/api/i18n/catalogo.js`")
        self.assertLess(i, j, (
            "el catálogo se pide DESPUÉS de `i18n.js`: así no está cuando se "
            "parsean las constantes"))

    def test_el_script_del_catalogo_lleva_el_mismo_token_de_cache(self):
        h = html()
        tokens = set(re.findall(r'\?v=([0-9a-z]+)', h))
        self.assertEqual(len(tokens), 1, (
            f"hay {len(tokens)} tokens distintos en `index.html`: {tokens}"))

    def test_el_motor_siembra_desde_el_global(self):
        # Por `js_completo()` y no por la ruta del fichero: lo exige
        # `test_frontend_troceado::TestNadieLeeUnaPiezaSuelta`, y con razón —
        # una ruta a mano se desincroniza del `index.html`.
        self.assertIn("__I18N", js_completo(), (
            "el motor no lee el catálogo que deja el servidor"))

    def test_el_endpoint_existe_en_el_servidor(self):
        src = (APP_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn('"/api/i18n/catalogo.js"', src)

    def test_cada_constante_de_la_lista_existe(self):
        js = js_completo()
        faltan = [c for c in CONSTANTES
                  if not re.search(rf"\b(?:const|let|var)\s+{re.escape(c)}\b", js)]
        self.assertEqual(faltan, [], (
            f"\nestas constantes de la lista ya no existen: {faltan}"))


if __name__ == "__main__":
    unittest.main()
