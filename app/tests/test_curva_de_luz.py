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


class TestElPunteroLeeLosValores(EnNode):
    """«Es lo más parecido a tener zoom sin tenerlo», dijo el usuario.

    El hover se perdió al reescribir el gráfico: la función seguía en el
    fichero pero buscaba las clases viejas, así que era código muerto.
    Con 240 puntos en el ancho del panel, leer el valor exacto de un
    instante sustituye a encuadrar un tramo.
    """

    def test_el_svg_lleva_cursor_punto_y_tooltip(self):
        svg = self.svg(WATCHMEN, {"avgSeries": [9] * 10, "minSeries": [0] * 10})
        for pieza in ("dv-luz-cursor", "dv-luz-dot", "dv-luz-tooltip", "dv-luz-host"):
            self.assertIn(pieza, svg, f"falta {pieza}")

    def test_y_los_datos_que_el_hover_necesita(self):
        svg = self.svg(WATCHMEN, {"avgSeries": [9] * 10, "minSeries": [0] * 10})
        self.assertIn("data-series=", svg)
        self.assertIn("data-geo=", svg)
        self.assertIn("data-avg=", svg)

    def test_la_geometria_incluye_la_escala_logaritmica(self):
        """`data-geo` lleva L0 y L1: sin ellos el hover tendría que
        recalcular la escala por su cuenta y el punto se despegaría de
        la curva en cuanto una de las dos fórmulas cambiara."""
        svg = self.svg(WATCHMEN)
        geo = re.search(r'data-geo="([^"]+)"', svg).group(1).split(",")
        self.assertEqual(len(geo), 8)
        self.assertEqual(geo[-2:], ["0", "4"])   # L0=1 nit · L1=10.000


    def test_en_la_serie_plana_no_hay_nada_que_enganchar(self):
        self.assertNotIn("dv-luz-host", self.svg(PLANA))


_CHROME = next((c for c in (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "", shutil.which("chromium") or "")
    if c and Path(c).exists()), None)

_HOVER = """
<pre id="__out"></pre>
<script>
setTimeout(() => {
  const out = {errores: [], casos: []};
  try {
    const S = %(serie)s;
    const host = document.createElement('div');
    host.style.width = '900px';
    host.innerHTML = _rgrfSparklineSvg(S, '', 6000,
      {avgSeries: S.map(() => 9), minSeries: S.map(() => 0)});
    document.body.appendChild(host);
    _attachSparklineHover();
    const svg = host.querySelector('.dv-luz-svg');
    const [padL, padR, padT, padB, W, H] = svg.dataset.geo.split(',').map(Number);
    const rect = svg.getBoundingClientRect();
    // Los anclajes del trazado del pico: el M y el último par de cada C.
    // El del PICO: de los trazados con curvas, el que más alturas distintas
    // tiene (el medio y el mínimo son planos en esta serie).
    const alturas = d => new Set(d.replace(/[MC]/g, ' ').trim().split(/ +/)
      .map(par => par.split(',')[1])).size;
    const pico = [...svg.querySelectorAll('path')].map(p => p.getAttribute('d'))
      .filter(d => d && d.includes('C')).sort((a, b) => alturas(b) - alturas(a))[0];
    const nums = pico.replace(/[MC]/g, ' ').trim().split(/ +/)
      .map(par => par.split(',').map(Number));
    const anclas = [nums[0]];
    for (let k = 3; k < nums.length; k += 3) anclas.push(nums[k]);
    for (const i of %(indices)s) {
      const sx = padL + (i / (S.length - 1)) * (W - padL - padR);
      svg.dispatchEvent(new MouseEvent('mousemove', {
        clientX: rect.left + (sx / W) * rect.width, clientY: rect.top + 10, bubbles: true}));
      const dot = svg.querySelector('.dv-luz-dot');
      const tip = host.querySelector('.dv-luz-tooltip');
      out.casos.push({i, ancla: anclas[i], cx: +dot.getAttribute('cx'),
                      cy: +dot.getAttribute('cy'), visible: dot.style.display !== 'none',
                      tip: tip.textContent});
    }
  } catch (e) { out.errores.push('EXCEPCIÓN: ' + e.message); }
  document.getElementById('__out').textContent = JSON.stringify(out);
}, 600);
</script>
"""


@unittest.skipUnless(_CHROME, "Chrome/Chromium no disponible")
class TestElPunteroCaeSobreLaCurva(unittest.TestCase):
    """El hover y el render tienen que usar la MISMA escala: si no, el punto
    se despega de la curva. Se comprobaba buscando `Math.log10` y las clases
    en el fuente del hover (revisión de Codex de la suite: el texto puede
    seguir ahí con la fórmula cambiada). Aquí se pinta la curva de Watchmen
    en Chrome, se mueve el puntero sobre tres muestras —el pico de 2.354
    nits incluido— y el punto tiene que caer en el anclaje que el render
    dibujó para esa muestra."""

    @classmethod
    def setUpClass(cls):
        import os, tempfile
        from frontend_sources import html, stub_catalogo_es
        cuerpo = _HOVER % {"serie": json.dumps(WATCHMEN), "indices": json.dumps([2, 4, 6])}
        pagina = html().replace("</head>", stub_catalogo_es() + "</head>")
        pagina = pagina.replace("</body>", cuerpo + "</body>")
        pagina = pagina.replace('src="/static/', 'src="').replace('href="/static/', 'href="')
        f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8",
                                        dir=str(APP_DIR / "static"))
        f.write(pagina)
        f.close()
        try:
            dom = subprocess.run(
                [_CHROME, "--headless", "--disable-gpu", "--allow-file-access-from-files",
                 "--dump-dom", "--window-size=1280,900", "--virtual-time-budget=6000", f.name],
                capture_output=True, text=True, timeout=180).stdout
        finally:
            os.unlink(f.name)
        m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
        if not m or not m.group(1).strip():
            raise unittest.SkipTest("Chrome no devolvió el volcado")
        cls.m = json.loads(m.group(1))

    def test_sin_errores(self):
        self.assertEqual(self.m["errores"], [])
        self.assertEqual(len(self.m["casos"]), 3)

    def test_el_punto_cae_en_el_anclaje_de_la_curva(self):
        for c in self.m["casos"]:
            with self.subTest(muestra=c["i"]):
                self.assertTrue(c["visible"])
                self.assertAlmostEqual(c["cx"], c["ancla"][0], delta=0.6)
                self.assertAlmostEqual(c["cy"], c["ancla"][1], delta=0.6)

    def test_y_el_tooltip_dice_el_valor_de_esa_muestra(self):
        pico = next(c for c in self.m["casos"] if c["i"] == 2)
        self.assertIn("354", pico["tip"])        # 2.354 / 2,354 según el locale


if __name__ == "__main__":
    unittest.main()
