"""El motor de traducción, ejecutado en Python y en node.

Dos mecanismos que hay que probar por separado porque fallan distinto:
`t()` —que construye mensajes con datos— y `data-i18n` —que resuelve las
etiquetas del marcado, incluido el que genera el JS—.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_i18n_motor -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

NODE = shutil.which("node")
from frontend_sources import js_completo  # noqa: E402


# ════════════════════════════════════════════════════════════════════
#  Backend
# ════════════════════════════════════════════════════════════════════

class MotorPythonCase(unittest.TestCase):

    def setUp(self):
        import i18n
        self.i18n = i18n
        self.tmp = Path(tempfile.mkdtemp(prefix="i18n_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        orig = i18n.CATALOGOS_DIR
        i18n.CATALOGOS_DIR = self.tmp
        self.addCleanup(lambda: setattr(i18n, "CATALOGOS_DIR", orig))
        i18n.limpiar_cache()
        self.addCleanup(i18n.limpiar_cache)

    def catalogo(self, idioma, datos):
        (self.tmp / f"{idioma}.json").write_text(
            json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        self.i18n.limpiar_cache()

    def con_idioma(self, codigo):
        p = mock.patch.object(self.i18n, "idioma_activo", lambda: codigo)
        p.start()
        self.addCleanup(p.stop)


class TestElTextoYSusParametros(MotorPythonCase):

    def test_devuelve_el_texto_del_idioma_activo(self):
        self.catalogo("es", {"saludo": "Hola"})
        self.catalogo("en", {"saludo": "Hello"})
        self.con_idioma("en")
        self.assertEqual(self.i18n.t("saludo"), "Hello")

    def test_los_parametros_van_por_NOMBRE_y_no_por_posicion(self):
        """El orden de las palabras cambia entre lenguas; el de los
        argumentos no puede depender de eso."""
        self.catalogo("es", {"x": "Quedan {n} de {total}"})
        self.catalogo("en", {"x": "{total} total, {n} left"})
        self.con_idioma("en")
        self.assertEqual(self.i18n.t("x", n=3, total=9), "9 total, 3 left")

    def test_un_parametro_que_no_se_pasa_se_queda_visible(self):
        """Mejor `{total}` en pantalla que una frase a la que le falta un
        dato sin que se note."""
        self.catalogo("es", {"x": "Quedan {n} de {total}"})
        self.con_idioma("es")
        self.assertEqual(self.i18n.t("x", n=3), "Quedan 3 de {total}")


class TestLoQueFaltaSeVE(MotorPythonCase):

    def test_una_clave_que_no_existe_devuelve_la_clave(self):
        self.catalogo("es", {})
        self.con_idioma("es")
        self.assertEqual(self.i18n.t("no.existe"), "no.existe")
        self.assertIn("no.existe", self.i18n.claves_ausentes())

    def test_si_falta_en_el_idioma_activo_cae_al_castellano(self):
        """Media traducción es mejor que un hueco."""
        self.catalogo("es", {"x": "Cancelar"})
        self.catalogo("en", {})
        self.con_idioma("en")
        self.assertEqual(self.i18n.t("x"), "Cancelar")
        self.assertEqual(self.i18n.claves_ausentes(), [])

    def test_un_catalogo_ilegible_no_tumba_nada(self):
        (self.tmp / "es.json").write_text("{esto no es json", encoding="utf-8")
        self.i18n.limpiar_cache()
        self.con_idioma("es")
        self.assertEqual(self.i18n.t("x"), "x")


class TestElIdiomaEsUnAjusteGlobal(unittest.TestCase):
    """Sin usuarios ni `Accept-Language`: sale de `app_settings.json`."""

    def setUp(self):
        from services import settings_store as st
        import i18n
        self.st, self.i18n = st, i18n
        self.tmp = Path(tempfile.mkdtemp(prefix="idioma_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        orig = (st.CONFIG_DIR, st.SETTINGS_PATH, st._cache)
        st.CONFIG_DIR = self.tmp
        st.SETTINGS_PATH = self.tmp / "app_settings.json"
        st._cache = None
        self.addCleanup(lambda: setattr(st, "_cache", orig[2]))
        self.addCleanup(lambda: setattr(st, "SETTINGS_PATH", orig[1]))
        self.addCleanup(lambda: setattr(st, "CONFIG_DIR", orig[0]))
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start(); self.addCleanup(env.stop)
        os.environ.pop("HDO_IDIOMA", None)
        i18n.limpiar_cache(); self.addCleanup(i18n.limpiar_cache)

    def test_por_defecto_castellano(self):
        self.assertEqual(self.st.get_idioma(), "es")
        self.assertEqual(self.i18n.idioma_activo(), "es")

    def test_se_guarda_y_el_motor_lo_ve(self):
        self.st.update_idioma("ca")
        self.assertEqual(self.st.get_idioma(), "ca")
        self.assertEqual(self.i18n.idioma_activo(), "ca")

    def test_un_idioma_inventado_se_ignora_sin_lanzar(self):
        """Va en el mismo POST que las cuatro claves de API: un valor raro no
        puede tumbar el guardado de las otras."""
        self.st.update_idioma("es")
        self.st.update_idioma("klingon")
        self.assertEqual(self.st.get_idioma(), "es")

    def test_sale_en_el_estado_publico_y_en_crudo(self):
        """No es un secreto, al contrario que las claves."""
        self.st.update_idioma("en")
        d = self.st.get_public_settings()["idioma"]
        self.assertEqual(d["activo"], "en")
        self.assertEqual(d["disponibles"], ["es", "en", "ca"])


# ════════════════════════════════════════════════════════════════════
#  Frontend, en node
# ════════════════════════════════════════════════════════════════════

_ENTORNO = r"""
// DOM mínimo: elementos con dataset, y un árbol que se puede recorrer.
function _el(tag) {
  const el = {
    tagName: tag, dataset: {}, _text: '', _html: '', placeholder: '',
    _attrs: {}, hijos: [], nodeType: 1,
    set textContent(v) { this._text = v; },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    setAttribute(k, v) { this._attrs[k] = v; },
    getAttribute(k) { return this._attrs[k]; },
    querySelectorAll(sel) {
      const props = ['i18n','i18nHtml','i18nPh','i18nTip','i18nAria'];
      return this.hijos.filter(h => props.some(p => h.dataset[p]));
    },
  };
  return el;
}
const _raiz = _el('div');
globalThis.document = {
  documentElement: _raiz,
  currentScript: { src: 'http://x/static/i18n.js?v=TOKEN' },
  querySelectorAll: sel => _raiz.querySelectorAll(sel),
  nodeType: 9,
};
globalThis.location = { href: 'http://x/', reload: () => { globalThis.__recargado = true; } };
globalThis.localStorage = {
  _d: {},
  getItem(k) { return k in this._d ? this._d[k] : null; },
  setItem(k, v) { this._d[k] = String(v); },
};
globalThis.MutationObserver = function (cb) {
  this.observe = () => { globalThis.__observando = true; };
};
// Solo se silencian error/warn (el motor avisa por ahí cuando un
// catálogo falla); `log` es por donde el test saca su resultado.
const _log = console.log.bind(console);
globalThis.console = { error: () => {}, warn: () => {}, log: _log };
globalThis.__el = _el;
globalThis.__raiz = _raiz;
"""


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElMotorDelNavegador(unittest.TestCase):
    """Se evalúa `i18n.js` REAL, sacado del JS que carga la página."""

    @classmethod
    def setUpClass(cls):
        js = js_completo()
        ini = js.index("const IDIOMAS = [")
        fin = js.index("async function reconciliarIdioma")
        fin = js.index("\n}\n", fin) + 3
        cls.motor = js[ini:fin]

    def corre(self, cuerpo: str) -> dict:
        script = "\n".join([_ENTORNO, self.motor, cuerpo])
        p = subprocess.run([NODE, "-e", script], capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_el_token_de_cache_sale_de_su_propio_src(self):
        r = self.corre("console.log(JSON.stringify({token: TOKEN_I18N}));")
        self.assertEqual(r["token"], "TOKEN")

    def test_tr_sustituye_parametros_por_nombre(self):
        r = self.corre("""
          _catalogo = {x: '{total} total, {n} left'};
          console.log(JSON.stringify({v: tr('x', {n: 3, total: 9})}));
        """)
        self.assertEqual(r["v"], "9 total, 3 left")

    def test_una_clave_ausente_devuelve_la_clave_y_se_apunta(self):
        r = self.corre("""
          _catalogo = {};
          const v = tr('falta.esta');
          console.log(JSON.stringify({v, ausentes: clavesAusentes()}));
        """)
        self.assertEqual(r["v"], "falta.esta")
        self.assertEqual(r["ausentes"], ["falta.esta"])

    def test_pintarTextos_llena_los_cinco_destinos(self):
        r = self.corre("""
          _catalogo = {a: 'Texto', b: '<b>HTML</b>', c: 'Pega aquí',
                       d: 'Ayuda', e: 'Etiqueta'};
          const n = __el('div');
          n.dataset.i18n = 'a'; n.dataset.i18nPh = 'c';
          n.dataset.i18nTip = 'd'; n.dataset.i18nAria = 'e';
          const h = __el('span'); h.dataset.i18nHtml = 'b';
          __raiz.hijos.push(n, h);
          pintarTextos(__raiz);
          console.log(JSON.stringify({
            texto: n.textContent, ph: n.placeholder,
            tip: n.getAttribute('data-tooltip'),
            aria: n.getAttribute('aria-label'), html: h.innerHTML,
          }));
        """)
        self.assertEqual(r, {"texto": "Texto", "ph": "Pega aquí",
                             "tip": "Ayuda", "aria": "Etiqueta",
                             "html": "<b>HTML</b>"})

    def test_no_repinta_lo_ya_pintado(self):
        """La marca es lo que impide que el observador se realimente: pintar
        provoca una mutación, y esa mutación no debe producir trabajo."""
        r = self.corre("""
          _catalogo = {a: 'Primero'};
          const n = __el('div'); n.dataset.i18n = 'a';
          __raiz.hijos.push(n);
          pintarTextos(__raiz);
          _catalogo = {a: 'Segundo'};
          pintarTextos(__raiz);
          console.log(JSON.stringify({texto: n.textContent,
                                      marca: n.dataset.i18nPuesto}));
        """)
        self.assertEqual(r["texto"], "Primero")
        self.assertEqual(r["marca"], "1")

    def test_el_idioma_guardado_solo_acepta_los_tres(self):
        r = self.corre("""
          const fuera = {};
          localStorage.setItem('hdo_idioma', 'ca');
          fuera.valido = idiomaGuardado();
          localStorage.setItem('hdo_idioma', 'klingon');
          fuera.invalido = idiomaGuardado();
          console.log(JSON.stringify(fuera));
        """)
        self.assertEqual(r, {"valido": "ca", "invalido": "es"})

    def test_si_el_catalogo_no_carga_se_cae_al_castellano(self):
        r = self.corre("""
          let pedidos = [];
          globalThis.fetch = async (u) => {
            pedidos.push(u);
            if (u.includes('/ca.json')) return {ok: false, status: 404};
            return {ok: true, json: async () => ({x: 'Hola'})};
          };
          cargarIdioma('ca').then(cod => {
            console.log(JSON.stringify({cod, pedidos, v: tr('x')}));
          });
        """)
        self.assertEqual(r["cod"], "es", "un catálogo que falla debe caer a es")
        self.assertEqual(r["v"], "Hola")
        self.assertEqual(len(r["pedidos"]), 2)

    def test_cambiar_de_idioma_guarda_en_los_dos_sitios_y_recarga(self):
        """Y el POST se ESPERA antes de recargar: si la recarga lo cancelara,
        el servidor seguiría escribiendo en el idioma viejo."""
        r = self.corre("""
          let post = null;
          globalThis.fetch = async (u, o) => { post = {u, body: o && o.body}; return {ok: true}; };
          cambiarIdioma('en').then(() => {
            console.log(JSON.stringify({
              local: localStorage.getItem('hdo_idioma'),
              post, recargado: !!globalThis.__recargado,
            }));
          });
        """)
        self.assertEqual(r["local"], "en")
        self.assertEqual(r["post"]["u"], "/api/settings")
        self.assertIn('"idioma":"en"', r["post"]["body"])
        self.assertTrue(r["recargado"])

    def test_reconciliar_no_recarga_si_coinciden(self):
        r = self.corre("""
          _idioma = 'es';
          reconciliarIdioma('es').then(() => {
            reconciliarIdioma('klingon').then(() => {
              console.log(JSON.stringify({recargado: !!globalThis.__recargado}));
            });
          });
        """)
        self.assertFalse(r["recargado"])


class TestElArranqueSigueSiendoSincrono(unittest.TestCase):
    """El `DOMContentLoaded` no puede esperar al catálogo.

    Se intentó al revés —`await cargarIdioma()` como primera línea— y convierte
    en asíncrono TODO lo que va detrás: iconos, tooltips, pollers. El navegador
    puede volcar el DOM antes de que nada de eso haya corrido, y lo cazó
    `test_ningun_svg_se_lee` con 12 iconos pintados en vez de 60.

    La forma correcta: la petición sale al parsear `i18n.js`, el observador se
    instala en el arranque y el primer barrido de textos se engancha al
    `.then()`. Cuando el arranque llega ahí, la promesa ya está resuelta.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = js_completo()
        i = cls.js.index("document.addEventListener('DOMContentLoaded'")
        cls.arranque = cls.js[i:cls.js.index("\n});", i)]

    def test_el_handler_del_arranque_no_es_async(self):
        self.assertNotIn("async", self.arranque.split("=>")[0],
                         "el arranque volvió a ser asíncrono")

    def test_no_hay_ningun_await_en_el_arranque(self):
        self.assertNotIn("await ", self.arranque,
                         "un await aquí retrasa los iconos y los pollers")

    def test_los_iconos_se_pintan_antes_que_los_textos(self):
        """Los iconos no dependen del catálogo: no deben esperarlo."""
        self.assertLess(self.arranque.index("pintarIconos()"),
                        self.arranque.index("catalogoListo"))

    def test_la_carga_arranca_al_parsear_el_script(self):
        self.assertIn("const catalogoListo = cargarIdioma(idiomaGuardado())",
                      self.js)


if __name__ == "__main__":
    unittest.main()
