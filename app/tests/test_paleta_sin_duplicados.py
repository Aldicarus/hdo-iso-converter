"""Ningún color crudo puede repetir un token que ya existe.

`:root` define la paleta entera —cada color con su `-dim`, su `-border` y,
desde hoy, su `-text`— pero el CSS no siempre la usaba. Lo que eso produce no
es desorden abstracto: son **colores que no son el color**.

Medido el 2026-09-21 sobre 10.500 líneas:

- el verde del punto de actividad era **`#1ed760`, el de Spotify**, no
  `--green` (`#34C759`). A ojo no se distinguen; juntos en la misma pantalla,
  sí. Entró porque `rgba()` no admite una variable de color dentro y hacía
  falta un halo — de ahí `--green-rgb`;
- el texto sobre fondo teñido estaba escrito a mano **46 veces**
  (`#005fb8`, `#0e6b2a`, `#8a4a00`), con el emparejamiento ya correcto: lo
  que faltaba era nombrarlo;
- y entre los respaldos de `var(--token, …)` —que nunca se aplican, porque el
  token existe— había **un tercer naranja** (`#ff9f0a`) que nadie habría
  visto jamás.

**Lo que este guard NO pide** es que no haya colores crudos. Los hay a
propósito: la paleta del log CMv4.0 está elegida para fondo oscuro y fuera
del tema, los `rgba()` de sombras y velos llevan su alfa, y la radiografía
DV+HDR tiene su propia escala *slate* declarada en `.dv-detail`. Prohibirlos
todos daría una lista de excepciones más larga que la regla. Lo que se
prohíbe es **duplicar**: escribir a mano un valor que ya tiene nombre.

**Y lo que este guard NO PUEDE hacer, medido.** El verde de Spotify no
duplicaba ningún token: era un color PARECIDO, que es peor. Se intentaron dos
detectores y los dos se descartaron con los números delante:

- **por distancia perceptual**: `#1ed760` está a Δ47 de `--green`, pero hay
  colores crudos perfectamente legítimos a Δ0-15 (blancos rotos, negros casi
  puros, variantes de superficie). Un umbral que cazara el 47 marcaría
  docenas de buenos;
- **por tono**: «saturado y con el tono de un color de la paleta» marca **49
  de los 98** colores opacos crudos, y casi todos son legítimos — la paleta
  del log CMv4.0 (verdes y rojos para fondo oscuro), los stops de los
  degradados, los textos sobre isla oscura.

Lo que distinguía al verde de Spotify no estaba en el CSS: era que cumplía
*el mismo papel* que `--green`. Eso no se deduce de un valor. Así que el
verde se encontró **midiendo y leyendo**, no con una regla, y lo que impide
que vuelva es que ya no existe más este guard, que sí caza la copia exacta.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_paleta_sin_duplicados -v
"""
import re
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "tests"))

CSS = (APP_DIR / "static" / "style.css").read_text(encoding="utf-8")

#: token → por qué su valor puede aparecer crudo en algún sitio
CRUDO_ACEPTADO: dict[str, str] = {
    # `--velo-sutil` es un fondo: en oscuro baja a .045 para que «elevar»
    # siga siendo «aclarar» sin blanquear la superficie. Los dos sitios que
    # escriben ese mismo valor a mano son un COLOR DE LETRA y un BORDE sobre
    # isla oscura, donde usar el token los volvería invisibles al cambiar de
    # tema. Mismo número, papel distinto.
    "--velo-sutil": "es un velo de fondo; los crudos son una letra y un borde sobre oscuro",
}


def _sin_comentarios(css: str) -> str:
    """Los comentarios fuera, conservando los saltos de línea.

    Sin esto el guard lee los colores que un comentario CITA —el bloque del
    tema oscuro documenta las seis parejas de Apple HIG— y los denuncia como
    si fueran declaraciones. Los `\n` se conservan para que el número de
    línea que reporta siga señalando al sitio correcto.
    """
    return re.sub(r"/\*.*?\*/",
                  lambda m: "\n" * m.group(0).count("\n"), css, flags=re.S)


def _root() -> tuple[str, str]:
    """El bloque `:root` y el resto del fichero, por separado."""
    css = _sin_comentarios(CSS)
    i = css.index(":root")
    j = css.index("\n}", i)
    return css[i:j], css[:i] + css[j:]


def _norm(v: str) -> str:
    """Normaliza un color para comparar: minúsculas y sin espacios."""
    v = v.strip().lower().rstrip(";")
    v = re.sub(r"\s+", "", v)
    # `#fff` y `#ffffff` son el mismo color.
    m = re.fullmatch(r"#([0-9a-f])([0-9a-f])([0-9a-f])", v)
    return f"#{m[1]*2}{m[2]*2}{m[3]*2}" if m else v


class TestNingunColorCrudoRepiteUnToken(unittest.TestCase):

    def setUp(self):
        root, self.resto = _root()
        self.tokens = {}
        for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", root):
            valor = _norm(m.group(2))
            if valor.startswith("#") or valor.startswith("rgb"):
                self.tokens.setdefault(valor, m.group(1))

    def test_el_guard_conoce_la_paleta(self):
        """Con los tokens mal leídos pasaría en verde sin mirar nada."""
        self.assertGreater(len(self.tokens), 15)
        self.assertIn("#34c759", self.tokens)

    def test_ningun_hex_duplica_un_token(self):
        malos = []
        for m in re.finditer(r"(#[0-9a-fA-F]{3,8})\b", self.resto):
            tok = self.tokens.get(_norm(m.group(1)))
            if tok and tok not in CRUDO_ACEPTADO:
                linea = self.resto[:m.start()].count("\n") + 1
                malos.append(f"{m.group(1)} → var({tok})  (~línea {linea})")
        self.assertEqual(sorted(set(malos)), [],
                         "\n  · ".join(["colores escritos a mano que ya tienen nombre:"]
                                       + sorted(set(malos))))

    def test_ningun_rgba_duplica_un_token(self):
        malos = []
        for m in re.finditer(r"(rgba?\([^)]*\))", self.resto):
            tok = self.tokens.get(_norm(m.group(1)))
            if tok and tok not in CRUDO_ACEPTADO:
                linea = self.resto[:m.start()].count("\n") + 1
                malos.append(f"{m.group(1)} → var({tok})  (~línea {linea})")
        self.assertEqual(sorted(set(malos)), [],
                         "\n  · ".join(["colores escritos a mano que ya tienen nombre:"]
                                       + sorted(set(malos))))


class TestUnRespaldoDeVarNoEsUnColorEscondido(unittest.TestCase):
    """`var(--token, #hex)` con el token definido no se aplica NUNCA.

    Es código muerto que además sugiere que la variable podría faltar — y
    donde puede esconderse un color distinto del token sin que se vea: así
    vivía un tercer naranja. Se permite solo con propiedades que NO se
    declaran en el CSS porque las fija el JS en línea (`--c`, `--chip-c`);
    ahí el respaldo sí es el valor por defecto de verdad.
    """

    def test_solo_las_que_fija_el_js_llevan_respaldo(self):
        root, resto = _root()
        definidos = set(re.findall(r"(--[\w-]+)\s*:", root))
        con_respaldo = {m.group(1) for m in
                        re.finditer(r"var\((--[\w-]+)\s*,", resto)}
        muertos = sorted(con_respaldo & definidos)
        self.assertEqual(muertos, [],
                         "\n  · ".join(["respaldos que no se aplican nunca:"] + muertos))

    def test_las_que_quedan_las_fija_el_js(self):
        """Y si una deja de fijarse desde el JS, el respaldo pasa a ser el
        único valor: eso ya no es un respaldo, es el color."""
        root, resto = _root()
        definidos = set(re.findall(r"(--[\w-]+)\s*:", root))
        sueltas = {m.group(1) for m in re.finditer(r"var\((--[\w-]+)\s*,", resto)} - definidos
        js = "\n".join(p.read_text(encoding="utf-8")
                       for p in (APP_DIR / "static").glob("*.js"))
        for v in sorted(sueltas):
            self.assertIn(f"{v}:", js,
                          f"{v} no se define ni en el CSS ni en el JS")


if __name__ == "__main__":
    unittest.main()
