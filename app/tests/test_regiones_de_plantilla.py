"""El autómata que delimita las plantillas del JS. (Lo usan los guards de i18n.)

Es una pieza pequeña y crítica: de ella depende **qué partes del frontend
mira** el barrido de castellano suelto. Cuando emparejaba los backticks con un
regex, tres construcciones lo descuadraban y a partir del descuadre se tomaba
por plantilla lo que no lo era — con el efecto de que el guard pasaba en verde
vigilando el vacío.

Así se colaron **26 literales castellanos en `settings.js`**: los badges de
procedencia de cada API key, los placeholders de sus campos y los mensajes de
resultado. Se vieron en una captura del panel en catalán, no en un test.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_regiones_de_plantilla -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import captura_castellano as captura  # noqa: E402
from frontend_sources import rutas  # noqa: E402


class TestLasTresConstruccionesQueLoDescuadraban(unittest.TestCase):

    def _contenidos(self, src: str) -> list[str]:
        return [c for _, _, c in captura.regiones_de_plantilla(src)]

    def test_un_backtick_en_un_comentario_no_abre_plantilla(self):
        """Era el caso de `settings.js`: `// \\`default\\` es la clave…`."""
        src = "// `default` es la clave que trae la app\nconst x = 'clave de la app';\n"
        self.assertEqual(self._contenidos(src), [])

    def test_un_backtick_en_una_regex_no_abre_plantilla(self):
        """`s.replace(/\\`([^\\`]+)\\`/g, …)` mete DOS backticks en juego.

        Es el que de verdad rompía `settings.js`: con él, las parejas
        siguientes se desplazan y quedan tres regiones fantasma de hasta 5.243
        caracteres. Y no basta con quitar los comentarios antes.
        """
        src = ("s = s.replace(/`([^`]+)`/g, '<code>$1</code>');\n"
               "const label = 'desde .env';\n")
        self.assertEqual(self._contenidos(src), [])

    def test_una_plantilla_anidada_se_queda_dentro_de_la_de_fuera(self):
        src = "const h = `a ${cond ? `b ${x}` : ''} c`;\nconst y = 'suelta';\n"
        self.assertEqual(self._contenidos(src), ["a ${cond ? `b ${x}` : ''} c"])

    def test_un_backtick_en_una_cadena_normal_tampoco_abre(self):
        src = "const t = 'usa `comillas` invertidas';\nconst u = 'otra cosa';\n"
        self.assertEqual(self._contenidos(src), [])

    def test_una_division_no_se_toma_por_regex(self):
        """Si se tomara, se comería el resto de la línea y la plantilla."""
        src = "const r = ancho / alto;\nconst h = `mide ${r}`;\n"
        self.assertEqual(self._contenidos(src), ["mide ${r}"])

    def test_lo_normal_sigue_funcionando(self):
        src = "const a = `hola ${nombre}`;\nconst b = `adiós`;\n"
        self.assertEqual(self._contenidos(src), ["hola ${nombre}", "adiós"])


class TestSobreElFrontendDeVerdad(unittest.TestCase):
    """Sin un ancla sobre el código real, el autómata puede pasar los casos de
    laboratorio y seguir descuadrando en un fichero de 6.000 líneas."""

    def test_ninguna_region_abre_justo_despues_de_una_barra(self):
        """Una plantilla nunca sigue a un `/` pelado: eso es una regex.

        Es la firma exacta del fallo que rompía `settings.js`:
        `s.replace(/\`([^\`]+)\`/g, …)` — el emparejado tomaba el backtick de
        DENTRO de la expresión regular por el inicio de una plantilla, y a
        partir de ahí todas las parejas quedaban corridas.

        El umbral por tamaño de región no sirve como ancla:
        `buildProjectPanelHTML` es una plantilla legítima de 200 líneas y
        13.271 caracteres. Lo que hay que comprobar es DÓNDE abre cada una.
        """
        malas = []
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            for ini, _, _ in captura.regiones_de_plantilla(src):
                previo = src[:ini].rstrip()
                if previo.endswith("/"):
                    malas.append(f"{Path(r).name}:{src[:ini].count(chr(10)) + 1}")
        self.assertEqual(malas, [], (
            "\nestas regiones abren justo detrás de un `/`, así que el "
            "backtick era de una\nexpresión regular:\n  · "
            + "\n  · ".join(malas[:12])))

    def test_ninguna_region_empieza_dentro_de_un_comentario(self):
        """La otra mitad de la misma propiedad, dicha directamente."""
        malas = []
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            limpio = captura.sin_comentarios(src)
            for ini, _, _ in captura.regiones_de_plantilla(src):
                if limpio[ini] != "`":
                    malas.append(f"{Path(r).name}:{src[:ini].count(chr(10)) + 1}")
        self.assertEqual(malas, [], (
            "\nestas regiones abren en un backtick que vive en un "
            "comentario:\n  · " + "\n  · ".join(malas[:12])))

    def test_las_regiones_no_se_solapan_y_van_en_orden(self):
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            previo = -1
            for ini, fin, _ in captura.regiones_de_plantilla(src):
                self.assertGreater(ini, previo, f"{Path(r).name}: se solapan")
                self.assertGreater(fin, ini, f"{Path(r).name}: región vacía")
                previo = fin


if __name__ == "__main__":
    unittest.main()
