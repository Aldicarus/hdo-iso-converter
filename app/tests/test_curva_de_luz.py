# -*- coding: utf-8 -*-
"""La curva de luz: escala, conceptos y el caso de la serie plana.

El usuario lo describió con tres capturas (2026-09-24): «se ve pequeño,
las líneas se solapan, no hay explicación clara, la interpretación es
imposible porque juntan 5 o 6 conceptos en el mismo sitio».

Las tres cosas, medidas sobre sus MKV:

  · la mediana de Watchmen (119 nits sobre un pico de 2354) caía al
    **4,4 %** de la altura en escala lineal — media película en nueve
    píxeles. En logarítmica sube al 60,5 %;
  · **once** conceptos a la vez: tres curvas, tres referencias
    dibujadas y cinco chips de «fuera del chart»;
  · MaxCLL 1000 y L2 1001 se rotulaban uno encima del otro.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_curva_de_luz -v
"""
import json
import math
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import (argv_node, js_completo, motor_i18n,  # noqa: E402
                              pintar_en, sistema_de_iconos)

NODE = shutil.which("node")
JS = js_completo()


def _funcion(nombre: str) -> str:
    i = JS.find(f"\nfunction {nombre}(")
    if i < 0:
        raise AssertionError(f"no está: {nombre}")
    ini = i + 1
    par, j = 0, JS.index("(", ini)
    while True:
        if JS[j] == "(":
            par += 1
        elif JS[j] == ")":
            par -= 1
            if par == 0:
                break
        j += 1
    prof, abierto = 0, False
    for k in range(j, len(JS)):
        if JS[k] == "{":
            prof += 1
            abierto = True
        elif JS[k] == "}":
            prof -= 1
            if abierto and prof == 0:
                return JS[ini:k + 1]
    raise AssertionError(nombre)


FUNCIONES = ("_rgrfFmtTime", "_rgrfSparklineSvg", "escHtml")

#: Watchmen, el caso que peor se veía: pico 2354, mediana 119.
WATCHMEN = [120, 119, 2354, 130, 118, 125, 700, 119, 121, 119]
#: Drive: 240 puntos y un solo valor.
PLANA = [236] * 20


@unittest.skipUnless(NODE, "node no disponible")
class EnNode(unittest.TestCase):

    def svg(self, serie, opts=None, dur=6000):
        guion = "\n".join([
            motor_i18n(), "globalThis.window = globalThis;",
            sistema_de_iconos(), *(_funcion(n) for n in FUNCIONES),
            f"const S = {json.dumps(serie)};",
            f"const O = {json.dumps(opts or {})};",
            f"process.stdout.write(JSON.stringify("
            f"_rgrfSparklineSvg(S, '', {dur}, O)));"])
        r = subprocess.run(argv_node(guion), capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1200:]}")
        return pintar_en(json.loads(r.stdout))


class TestLaEscalaEsLogaritmica(EnNode):

    def test_el_eje_va_por_decadas(self):
        """1 · 10 · 100 · 1000, que es como se lee una gráfica de brillo."""
        svg = self.svg(WATCHMEN)
        ejes = re.findall(r'class="dv-luz-eje">([\dk]+)<', svg)
        valores = [int(e[:-1]) * 1000 if e.endswith("k") else int(e)
                   for e in ejes if e.strip()]
        decadas = [v for v in valores if v and math.log10(v) % 1 == 0]
        self.assertGreaterEqual(len(decadas), 3, f"ejes: {valores}")

    def test_la_mediana_deja_de_estar_aplastada(self):
        """El defecto medido: en lineal caía al 4,4 % de la altura.

        Se comprueba sobre la geometría real del path: el punto MÁS BAJO
        de la curva de Watchmen (119 nits) tiene que quedar por encima
        de la mitad del área útil. En lineal caía a nueve píxeles del
        eje; con el suelo del eje en 1 nit sube al 52 %.

        **El suelo tiene que ser 1 nit y no la década del mínimo**: con
        el eje arrancando en 100 el rango se estrecha y la curva vuelve
        abajo. Se pisó al escribirlo.
        """
        svg = self.svg(WATCHMEN)
        d = re.search(r'<path d="(M [^"]+)" fill="none"', svg).group(1)
        # Las `y` son el segundo número de cada par «x,y».
        ys = [float(y) for _x, y in re.findall(r"(\d+\.\d),(\d+\.\d)", d)]
        padT, alto_util = 16, 320 - 16 - 34
        desde_arriba = (max(ys) - padT) / alto_util
        self.assertLess(desde_arriba, 0.60,
                        f"la curva baja al {desde_arriba:.0%} del área: sigue aplastada")


class TestUnaLineaYUnaBanda(EnNode):

    OPTS = {"avgSeries": [9] * 10, "minSeries": [0] * 10,
            "refs": {"l2_trim_targets_nits": [100]}}

    def test_el_pico_es_la_unica_linea(self):
        svg = self.svg(WATCHMEN, self.OPTS)
        lineas = re.findall(r'<path d="[^"]+" fill="none" stroke="([^"]+)"', svg)
        self.assertEqual(len(lineas), 1, f"hay {len(lineas)} curvas compitiendo")

    def test_y_el_minimo_y_el_medio_son_una_banda(self):
        svg = self.svg(WATCHMEN, self.OPTS)
        self.assertIn('fill="var(--dv-accent-bg-2)"', svg)

    def test_sin_las_otras_dos_series_no_hay_banda(self):
        self.assertNotIn('fill="var(--dv-accent-bg-2)"', self.svg(WATCHMEN))


class TestLasReferenciasNoSeRotulanEnElLienzo(EnNode):
    """MaxCLL 1000 y L2 1001 se dibujaban uno encima del otro."""

    OPTS = {"refs": {"hdr10_max_cll": 1000, "l2_trim_targets_nits": [100, 1001],
                     "l6_master_max_nits": 4000, "hdr10_max_fall": 462}}

    def test_ninguna_referencia_escribe_su_nombre_sobre_el_grafico(self):
        svg = self.svg(WATCHMEN, self.OPTS)
        dentro = svg[svg.index("<svg"):svg.index("</svg>")]
        for nombre in ("MaxCLL", "MaxFALL", "L2 ", "master"):
            self.assertNotIn(nombre, dentro, f"«{nombre}» sigue rotulado en el lienzo")

    def test_pero_se_dibujan_sus_lineas(self):
        svg = self.svg(WATCHMEN, self.OPTS)
        dentro = svg[svg.index("<svg"):svg.index("</svg>")]
        self.assertGreaterEqual(dentro.count('stroke-dasharray="6,5"'), 3)

    def test_y_la_leyenda_las_agrupa_por_familia(self):
        """«Lo que el disco declara» y «objetivos de trim» son cosas
        distintas, y en una fila de once chips no se distinguían."""
        svg = self.svg(WATCHMEN, self.OPTS)
        self.assertIn("El disco declara", svg)
        self.assertIn("Objetivos de trim", svg)
        self.assertIn("Medido", svg)

    def test_una_referencia_fuera_de_escala_se_marca(self):
        """El máster de 4000 sobre una peli que llega a 2354: no se
        puede dibujar, pero decirlo es distinto de callarlo."""
        svg = self.svg([100, 200, 300], {"refs": {"l6_master_max_nits": 40000}})
        self.assertIn("dv-luz-chip-fuera", svg)


class TestLaSeriePlanaNoDibujaUnaCurva(EnNode):
    """Le pasa a 3 de los 7 MKV medidos: el relleno azul de lado a lado
    parecía un dato y no lo era."""

    def test_no_hay_svg(self):
        html = self.svg(PLANA)
        self.assertNotIn("<svg", html)
        self.assertIn("dv-luz-plana", html)

    def test_y_se_explica_por_que(self):
        self.assertIn("valor único", self.svg(PLANA))


class TestSeExplicaQueSeEstaViendo(EnNode):

    def test_hay_una_linea_de_ayuda(self):
        """No existía ninguna."""
        self.assertIn("dv-luz-ayuda", self.svg(WATCHMEN))
        self.assertIn("logarítmica", self.svg(WATCHMEN))


class TestOcupaElAnchoDelPanel(unittest.TestCase):

    def test_el_svg_escala_en_vez_de_ir_a_720_fijos(self):
        cuerpo = _funcion("_rgrfSparklineSvg")
        self.assertIn("viewBox", cuerpo)
        self.assertNotIn("svgW = 720", cuerpo)

    def test_y_el_margen_derecho_deja_de_ser_de_118px(self):
        """Eran 102 px de ancho perdidos para rótulos que ya no están."""
        cuerpo = _funcion("_rgrfSparklineSvg")
        m = re.search(r"padR = (\d+)", cuerpo)
        self.assertLess(int(m.group(1)), 40)


if __name__ == "__main__":
    unittest.main()
