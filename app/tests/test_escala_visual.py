"""Los radios y las duraciones salen de la escala, no del teclado.

`:root` ya definía `--r-xs…xl` y `--t-fast/med/slow`. El CSS no los usaba: 244
`border-radius` con **24 valores distintos** (2·3·4·5·6·7·8·9·10·12·16·17·20)
y **14 duraciones** de transición (0,1 · 0,12 · 0,14 · 0,15 · 0,18 · 0,2 ·
0,25 · 0,3 · 0,35 · 0,4 · 0,5). No es desorden abstracto: es lo que da el aire
de «hecho a trozos» cuando dos elementos vecinos se redondean distinto y dos
animaciones que deberían ir juntas no duran lo mismo.

Al adoptarlos, los empates se rompieron **hacia arriba** —«radios generosos»
es lo que este proyecto documenta sobre su propio lenguaje visual— y 148 de
los 222 no cambiaron ni un píxel porque ya coincidían con un token.

Dos duraciones se quedan crudas **a propósito**, y por eso están escritas
aquí con su motivo: no son transiciones de interfaz.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_escala_visual -v
"""
import re
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
CSS = (APP_DIR / "static" / "style.css").read_text(encoding="utf-8")

#: duración cruda → por qué no es una transición de interfaz
FUERA_DE_LA_ESCALA = {
    "2s": "la barra de progreso: su duración la marca el dato que anima, no "
          "el lenguaje de la interfaz",
    "9999s": "el truco contra el autorrelleno de Chrome — nunca llega a "
             "ocurrir, es una forma de congelar el color de fondo",
    "0s": "el retardo de ese mismo truco, no una duración: `transition: "
          "background-color 9999s ease-in-out 0s`",
}


def _fuera_de_root() -> str:
    i = CSS.index(":root")
    return CSS[:i] + CSS[CSS.index("\n}", i):]


class TestLosRadiosSalenDeLaEscala(unittest.TestCase):

    def test_ninguno_se_escribe_en_pixeles(self):
        crudos = []
        for m in re.finditer(r"border-radius:\s*([^;{}]+);", _fuera_de_root()):
            if re.search(r"\d+px", m.group(1)):
                crudos.append(m.group(1).strip())
        self.assertEqual(sorted(set(crudos)), [],
                         "\n  · ".join(["radios a mano:"] + sorted(set(crudos))))

    def test_el_porcentaje_se_queda(self):
        """`50%` NO es un radio de la escala: es la mitad de la caja, así que
        en una caja no cuadrada da una elipse. Es otra cosa y se escribe
        distinto a propósito."""
        self.assertIn("border-radius: 50%", CSS)

    def test_la_escala_existe(self):
        for t in ("--r-xxs", "--r-xs", "--r-sm", "--r-md", "--r-lg", "--r-xl",
                  "--r-full"):
            self.assertRegex(CSS, rf"{t}\s*:", f"falta {t}")


class TestLasOpacidadesSalenDeLaEscala(unittest.TestCase):
    """«Apagado» se escribía con 17 valores distintos entre .3 y .95.

    Cuatro peldaños, no dos: agrupar en dos —como proponía la medición de
    septiembre— movería algunos 0,25, y eso no es unificar, es repintar. Con
    cuatro el salto máximo fue de 0,07, salvo un .3 que sube a .4.
    """

    #: opacidad cruda → por qué no es un peldaño de «apagado»
    FUERA = {
        "0": "encendido/apagado, no un grado de atenuación",
        "1": "lo mismo, al otro extremo",
        "0.1": "el velo del backdrop ambiente de la ficha: es un efecto con "
               "su valor, no un elemento atenuado",
        "0.95": "un casi-opaco puntual, por la misma razón",
    }

    def test_ninguna_se_escribe_a_mano(self):
        # `0.10` y `0.1` son el mismo número escrito de dos formas.
        fuera = {float(v) for v in self.FUERA}
        crudas = []
        for m in re.finditer(r"opacity:\s*([\d.]+)\s*[;}]", _fuera_de_root()):
            if float(m.group(1)) not in fuera:
                crudas.append(m.group(1))
        self.assertEqual(sorted(set(crudas)), [],
                         "\n  · ".join(["opacidades a mano:"] + sorted(set(crudas))))

    def test_la_escala_existe(self):
        for t in ("--op-fuerte", "--op-medio", "--op-tenue", "--op-muy-tenue"):
            self.assertRegex(CSS, rf"{t}\s*:", f"falta {t}")

    def test_cada_excepcion_sigue_existiendo(self):
        vivas = {float(m) for m in
                 re.findall(r"opacity:\s*([\d.]+)\s*[;}]", CSS)}
        for v in self.FUERA:
            self.assertIn(float(v), vivas, f"la excepción {v} ya no existe")


class TestLasDuracionesSalenDeLaEscala(unittest.TestCase):

    def _crudas(self) -> list[str]:
        fuera = []
        for m in re.finditer(r"transition:([^;{}]+);", _fuera_de_root()):
            for d in re.findall(r"(?<![\d.])(\.?\d+(?:\.\d+)?s)\b", m.group(1)):
                if d not in FUERA_DE_LA_ESCALA:
                    fuera.append(d)
        return fuera

    def test_ninguna_se_escribe_en_segundos(self):
        self.assertEqual(sorted(set(self._crudas())), [],
                         "\n  · ".join(["duraciones a mano:"]
                                       + sorted(set(self._crudas()))))

    def test_cada_excepcion_sigue_existiendo(self):
        """Una excepción que ya no corresponde a código real parece cobertura
        y no cubre nada."""
        for d in FUERA_DE_LA_ESCALA:
            self.assertRegex(CSS, rf"transition:[^;{{}}]*\b{re.escape(d)}\b",
                             f"la excepción {d} ya no existe")

    def test_cada_excepcion_lleva_su_motivo(self):
        for d, motivo in FUERA_DE_LA_ESCALA.items():
            self.assertGreater(len(motivo), 30, d)


if __name__ == "__main__":
    unittest.main()
