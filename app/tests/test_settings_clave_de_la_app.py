"""Cómo se ve en ⚙︎ Configuración que la clave de TMDb la pone la app.

El badge de una sección de Configuración tiene ahora cuatro estados y solo
uno es nuevo, pero es el que ve todo el mundo sin haber tocado nada. Lo que
este test fija no es el texto por el texto, son tres decisiones:

  · **la clave de la app NO lleva visto verde** — funciona, pero no es un
    logro del usuario, y un visto donde no has hecho nada confunde;
  · **no enseña cola de 4 caracteres**, porque el backend no la manda: una
    cola invita a confundirla con la tuya;
  · **el botón «Vaciar todo» sigue saliendo solo si TÚ configuraste algo**,
    que es lo que devuelve `_renderSettingsSection`. Con la clave de la app
    contando como «configurada», el `if (st.configured)` de la primera línea
    invita a devolver `true` y dejar el botón encendido para siempre.

Se evalúa la función REAL sobre un DOM mínimo, como el resto de tests de
frontend en node.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_settings_clave_de_la_app -v
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

NODE = shutil.which("node")
from frontend_sources import argv_node, js_completo, motor_i18n, pintar_en  # noqa: E402

JS = js_completo()


def _extraer_funcion(nombre: str) -> str:
    for marca in (f"async function {nombre}(", f"function {nombre}("):
        i = JS.find(marca)
        if i != -1:
            return JS[i:JS.index("\n}\n", i) + 3]
    raise AssertionError(f"no encuentro {nombre}() en el JS del frontend")


ENTORNO = r"""
// DOM mínimo: el badge y el input de una sección de Configuración.
function _el(id) {
  return {
    id, className: '', _text: '', _html: '', placeholder: '',
    set textContent(v) { this._text = v; this._html = v; },
    get textContent() { return this._text; },
    set innerHTML(v) { this._html = v; this._text = v.replace(/<[^>]*>/g, ''); },
    get innerHTML() { return this._html; },
  };
}
const _dom = {};
globalThis.document = {
  getElementById: id => (_dom[id] = _dom[id] || _el(id)),
};
// `icono()` devuelve HTML; aquí basta con una marca reconocible.
globalThis.icono = n => `<svg data-i="${n}"></svg>`;
"""

SALIDA = r"""
const entrada = JSON.parse(process.argv[2]);
const userSet = _renderSettingsSection(entrada.key, entrada.data);
const badge = document.getElementById(`settings-${entrada.key}-status`);
const inp   = document.getElementById(`settings-${entrada.key}-input`);
console.log(JSON.stringify({
  userSet,
  clase: badge.className,
  html: badge.innerHTML,
  texto: badge.textContent,
  placeholder: inp.placeholder,
}));
"""


@unittest.skipIf(NODE is None, "node no está instalado")
class RenderCase(unittest.TestCase):

    def render(self, key: str, estado: dict) -> dict:
        # El motor PRIMERO: `_renderSettingsSection` escribe los badges y los
        # placeholders con `tr()` desde que se extrajeron —salían en
        # castellano con la app en otro idioma— y sin él el arnés muere con
        # «tr is not defined».
        script = "\n".join([
            motor_i18n(),
            ENTORNO,
            _extraer_funcion("escHtml"),
            _extraer_funcion("_renderSettingsSection"),
            SALIDA,
        ])
        proc = subprocess.run(
            argv_node(script, json.dumps({"key": key, "data": {key: estado}})),
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return pintar_en(json.loads(proc.stdout))


class TestLaClaveDeLaApp(RenderCase):

    ESTADO = {"configured": True, "source": "default", "last4": "",
              "is_default": True}

    def test_se_anuncia_como_clave_de_la_app(self):
        r = self.render("tmdb", self.ESTADO)
        self.assertIn("clave de la app", r["texto"])

    def test_no_lleva_visto_verde(self):
        """El visto es para lo que TÚ has configurado y ha validado."""
        r = self.render("tmdb", self.ESTADO)
        self.assertNotIn("<svg", r["html"], "la clave de la app lleva icono")
        self.assertIn("default", r["clase"])
        self.assertNotIn("ok", r["clase"].split())

    def test_no_ensena_cola_de_cuatro_caracteres(self):
        r = self.render("tmdb", self.ESTADO)
        self.assertNotIn("…", r["texto"])

    def test_el_placeholder_dice_que_es_opcional(self):
        r = self.render("tmdb", self.ESTADO)
        self.assertIn("opcional", r["placeholder"].lower())

    def test_no_enciende_el_boton_de_vaciar_todo(self):
        """`Vaciar todo` borra lo que el usuario puso; con la clave de la app
        no hay nada que vaciar."""
        self.assertFalse(self.render("tmdb", self.ESTADO)["userSet"])


class TestLosOtrosTresEstados(RenderCase):

    def test_la_propia_lleva_visto_cola_y_enciende_vaciar_todo(self):
        r = self.render("tmdb", {"configured": True, "source": "settings",
                                 "last4": "wxyz", "is_default": False})
        self.assertIn("<svg", r["html"])
        self.assertIn("ok", r["clase"].split())
        self.assertIn("wxyz", r["texto"])
        self.assertTrue(r["userSet"])

    def test_la_del_entorno_se_distingue_y_no_enciende_vaciar_todo(self):
        """`Vaciar todo` no puede borrar una variable de entorno."""
        r = self.render("tmdb", {"configured": True, "source": "env",
                                 "last4": "1234", "is_default": False})
        self.assertIn(".env", r["texto"])
        self.assertIn("env", r["clase"].split())
        self.assertFalse(r["userSet"])

    def test_sin_ninguna_clave_sigue_avisando_como_siempre(self):
        """Un build sin clave de la app ve la UI de antes, sin cambios."""
        r = self.render("tmdb", {"configured": False, "source": "none",
                                 "last4": "", "is_default": False})
        self.assertIn("No configurada", r["texto"])
        self.assertIn("warn", r["clase"].split())
        self.assertFalse(r["userSet"])

    def test_google_sin_clave_no_habla_de_ninguna_clave_de_la_app(self):
        r = self.render("google", {"configured": False, "source": "none",
                                   "last4": "", "is_default": False})
        self.assertNotIn("app", r["texto"].lower())
        self.assertIn("Google", r["placeholder"])


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElBadgeDelRepoDoviTools(unittest.TestCase):
    """El repo tiene su propio renderizador, así que su estado `default` no
    sale gratis por arreglar el de las claves."""

    def render(self, estado: dict) -> dict:
        script = "\n".join([
            motor_i18n(),
            ENTORNO.replace("settings-${entrada.key}", "settings-drive-folder"),
            _extraer_funcion("escHtml"),
            _extraer_funcion("_renderSettingsDriveFolder"),
            r"""
const entrada = JSON.parse(process.argv[2]);
const userSet = _renderSettingsDriveFolder({drive_folder: entrada});
const badge = document.getElementById('settings-drive-folder-status');
const inp   = document.getElementById('settings-drive-folder-input');
console.log(JSON.stringify({
  userSet, clase: badge.className, html: badge.innerHTML,
  texto: badge.textContent, placeholder: inp.placeholder,
}));
""",
        ])
        proc = subprocess.run(argv_node(script, json.dumps(estado)),
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return pintar_en(json.loads(proc.stdout))

    def test_el_de_la_app_se_anuncia_y_no_enciende_vaciar_todo(self):
        r = self.render({"configured": True, "source": "default", "last4": "",
                         "folder_id_last6": "5lmPgN", "is_default": True})
        self.assertIn("repo de la app", r["texto"])
        self.assertIn("default", r["clase"])
        self.assertNotIn("<svg", r["html"], "lleva visto verde")
        self.assertFalse(r["userSet"])

    def test_el_id_si_se_ensena_porque_no_es_un_secreto(self):
        """Ayuda a reconocer que es la carpeta de siempre."""
        r = self.render({"configured": True, "source": "default", "last4": "",
                         "folder_id_last6": "5lmPgN", "is_default": True})
        self.assertIn("5lmPgN", r["texto"])

    def test_el_propio_lleva_visto_y_enciende_vaciar_todo(self):
        r = self.render({"configured": True, "source": "settings", "last4": "aBcD",
                         "folder_id_last6": "abc123", "is_default": False})
        self.assertIn("<svg", r["html"])
        self.assertIn("ok", r["clase"].split())
        self.assertTrue(r["userSet"])

    def test_sin_ninguno_sigue_avisando_de_que_el_repo_esta_bloqueado(self):
        r = self.render({"configured": False, "source": "none", "last4": "",
                         "folder_id_last6": "", "is_default": False})
        self.assertIn("warn", r["clase"].split())
        self.assertFalse(r["userSet"])


class TestElEstiloExiste(unittest.TestCase):

    def test_la_clase_default_esta_definida_en_el_css(self):
        """La usaba ya el sheet y NO existía: caía al estilo base y se veía
        como texto suelto. Misma trampa que una `var()` inexistente."""
        css = (APP_DIR / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".settings-status.default", css)


if __name__ == "__main__":
    unittest.main()
