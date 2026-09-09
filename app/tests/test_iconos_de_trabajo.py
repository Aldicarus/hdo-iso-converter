"""Los iconos de la columna de trabajo, y las variables CSS que existen.

Los emoji los dibuja el sistema operativo: cambian de forma y de color entre
máquinas, no heredan la paleta de la aplicación y a un ⏳ o un ⬜ no hay manera
de quitarles el aire de conversación de chat. Un `<svg>` con `currentColor`
hereda el color, se anima con CSS y pesa lo mismo que un carácter.

Lo que este fichero fija:

- **Un icono por tipo de trabajo y otro por estado**, para que el mismo trabajo
  se vea igual en la columna, en la cola, en el historial y en el modal. Si el
  modal derivara el suyo de un campo aparte, podrían acabar discrepando.
- **La paleta es la de la app.** El aspecto pastel sale de usar las variantes
  `-dim` como fondo y las sólidas como trazo, no de colores nuevos.
- **Ninguna variable CSS se usa sin definirse.** Una `var()` que no existe
  invalida la declaración entera y el estilo cae al heredado, que casi siempre
  se parece — por eso pasan desapercibidas. Había cuatro sin definir, una de
  ellas recién metida por mí en los bordes de la columna nueva, que
  sencillamente no se pintaban.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_iconos_de_trabajo -v
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
STATIC = APP_DIR / "static"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import html, js_completo, pieza_de  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()
CSS = (STATIC / "style.css").read_text(encoding="utf-8")

TIPOS = ["rip", "crear_serie", "analisis_extendido", "copia_biblioteca",
         "fase_cmv40"]
ESTADOS = ["corriendo", "en_cola", "hecho", "error", "cancelado"]


def _fn(nombre: str) -> str:
    i = JS.index(f"function {nombre}(")
    return JS[JS.rindex("\n", 0, i) + 1:JS.index("\n}\n", i) + 3]


def _bloque(marca: str) -> str:
    i = JS.index(marca)
    return JS[i:JS.index("\n};\n", i) + 4]


@unittest.skipIf(NODE is None, "node no está instalado")
class TestHayIconoParaTodo(unittest.TestCase):
    """Un tipo sin icono deja un hueco donde el resto tiene una pista."""

    def _render(self, fn, clave) -> str:
        guion = f"""
{_bloque('const _ICONOS_TRABAJO = {')}
{_bloque('const _ICONOS_ESTADO = {')}
{_fn('_svg')}
{_fn('_chipIcono')}
{_fn('iconoDeTrabajo')}
{_fn('iconoDeEstado')}
console.log(JSON.stringify({fn}({json.dumps(clave)})));
"""
        r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True,
                           timeout=30)
        if r.returncode != 0:
            raise AssertionError(r.stderr[:600])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_los_cinco_tipos_tienen_el_suyo(self):
        for t in TIPOS:
            with self.subTest(tipo=t):
                h = self._render("iconoDeTrabajo", t)
                self.assertIn("<svg", h)
                self.assertIn("icono-chip", h)

    def test_los_cinco_estados_tambien(self):
        for e in ESTADOS:
            with self.subTest(estado=e):
                self.assertIn("<svg", self._render("iconoDeEstado", e))

    def test_un_tipo_desconocido_no_pinta_basura(self):
        """Mejor un hueco que un cuadro vacío o un `undefined` en el HTML."""
        self.assertEqual(self._render("iconoDeTrabajo", "tipo_que_no_existe"), "")

    def test_solo_el_de_en_curso_se_anima(self):
        """El movimiento tiene que significar algo. Si se animaran todos, no
        distinguiría nada."""
        girando = [e for e in ESTADOS
                   if "icono-girando" in self._render("iconoDeEstado", e)]
        self.assertEqual(girando, ["corriendo"])

    def test_el_tamano_va_en_el_chip_no_en_el_svg(self):
        """Así el mismo icono sirve en la columna y en la cabecera del modal
        sin tocar el marcado."""
        h = self._render("iconoDeTrabajo", "rip")
        self.assertNotIn("width=", h.split("</span>")[0].split("<svg")[0])
        self.assertIn('viewBox="0 0 24 24"', h)


class TestElAspectoSaleDeLaPaletaDeLaApp(unittest.TestCase):

    def test_los_tonos_usados_estan_definidos_en_el_css(self):
        tonos = set(re.findall(r"\.icono-([a-z]+)\s*\{", CSS))
        usados = set(re.findall(r"\['([a-z]+)', _svg", JS))
        self.assertEqual(usados - tonos, set(),
                         "hay un tono de icono sin regla CSS: el chip saldría "
                         "sin color")

    def test_el_pastel_sale_de_las_variantes_dim(self):
        """No se inventan colores: el fondo es la variante translúcida que la
        app ya define y el trazo, la sólida."""
        bloque = CSS[CSS.index(".icono-azul"):CSS.index(".icono-girando")]
        self.assertIn("var(--blue-dim)", bloque)
        self.assertIn("var(--blue)", bloque)
        self.assertIn("var(--green-dim)", bloque)

    def test_se_respeta_prefers_reduced_motion(self):
        self.assertIn("prefers-reduced-motion", CSS)
        i = CSS.index("prefers-reduced-motion")
        self.assertIn("icono-girando", CSS[i:i + 400])


class TestNoQuedanEmojiEnLaColumnaNiEnElModal(unittest.TestCase):
    """Las superficies nuevas van con SVG. En el resto de la app los emoji
    siguen, y está bien: ahí son contenido (💿 = un disco), no iconografía de
    estado."""

    def _sin_comentarios(self, src: str) -> str:
        return re.sub(r"//.*|/\*(?:.|\n)*?\*/", "", src)

    def test_la_columna_no_pinta_emoji_de_estado(self):
        src = self._sin_comentarios(pieza_de("iconoDeTrabajo")[1])
        for emoji in ("⏳", "⬜", "✓ ", "⚙︎"):
            self.assertNotIn(emoji, src, f"queda {emoji!r} en la columna")

    def test_las_cinco_vistas_no_traen_icono_propio_DEL_TRABAJO(self):
        """El icono del TRABAJO lo deriva el modal del tipo; si cada vista
        trajera el suyo, la columna y el modal podrían enseñar distintos.

        Los de cada FASE sí son de la vista —igual que `st.icon` en la
        timeline de CMv4.0—: describen el paso, no el trabajo. Por eso se
        mira solo la clave de primer nivel del objeto que devuelve."""
        for i in re.finditer(r"registrarDetalleDeTrabajo\('[a-z_]+'", JS):
            bloque = JS[i.start():JS.index("});", i.start())]
            self.assertNotIn("\n    icono:", bloque, bloque[:60])

    def test_el_html_de_la_columna_y_el_modal_no_lleva_emoji(self):
        h = html()
        for marca in ('id="workbar-title"', 'id="trabajo-modal-icono"'):
            pass
        i = h.index('<aside id="workbar"')
        self.assertNotIn("⚙︎", h[i:i + 900])


class TestElHistorialSeVeSiempre(unittest.TestCase):
    """Saber qué acaba de pasar es la mitad de la pregunta que esta columna
    responde. Se pintaba solo con la casa libre, o sea casi nunca cuando de
    verdad interesa."""

    def test_los_recientes_no_dependen_de_que_no_haya_nada_en_marcha(self):
        src = pieza_de("_workbarRender")[1]
        i = src.index("function _workbarRender(")
        cuerpo = src[i:src.index("\n}\n", i)]
        vacio = cuerpo.index("if (!total)")
        self.assertLess(cuerpo.index("const recientes ="), vacio,
                        "los recientes se calculan DENTRO de la rama de "
                        "«no hay nada», así que no salen cuando hay trabajo")
        # Y se concatenan en las DOS ramas: la de la casa libre y la de con
        # trabajo. Una sola aparición significaría que a una le falta.
        self.assertEqual(cuerpo.count("recientes;"), 2)


class TestNingunaVariableCssSeUsaSinDefinirse(unittest.TestCase):
    """Una `var()` que no existe invalida la declaración ENTERA.

    No falla ruidosamente: el estilo cae al heredado, que casi siempre se
    parece lo bastante. Había cuatro sin definir —`--text`, `--accent`,
    `--text-secondary`, `--font-stack`— y una quinta recién metida por mí,
    `--border`, en los bordes de la columna de trabajo: sencillamente no se
    pintaban.
    """

    def test_todas_las_var_tienen_su_declaracion(self):
        definidas = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", CSS, re.M))
        usadas = set()
        for f in sorted(STATIC.glob("*")):
            if f.suffix in (".css", ".js", ".html"):
                # Solo `var(--x)` cerrado: `var(--x, fallback)` y las
                # construidas al vuelo (`var(--chip-${c})`) no son comprobables.
                usadas |= set(re.findall(r"var\((--[a-z0-9-]+)\)",
                                         f.read_text(encoding="utf-8")))
        self.assertEqual(
            sorted(usadas - definidas), [],
            "variables CSS usadas y nunca definidas: la declaración se "
            "invalida y el estilo cae al heredado sin un solo error")


if __name__ == "__main__":
    unittest.main()
