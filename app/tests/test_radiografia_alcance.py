# -*- coding: utf-8 -*-
"""La radiografía no puede contradecirse a sí misma.

Tres defectos que el usuario reportó el 2026-09-24 como «me miro un MKV y
soy incapaz de sacar conclusiones», y los tres eran eso literalmente: la
pantalla decía una cosa arriba y la contraria abajo.

1. El banner de divergencia comparaba el pico L1 del **sniff de 30 s**
   contra el MaxCLL del SEI, que es de todo el metraje. Medido sobre los
   MKV del NAS falla en 2 de 4 y en las DOS direcciones: Pulp Fiction
   sacaba el banner rojo de «máster conservador» (395 del sniff contra
   1000) cuando su pico real es 1001 —o sea, coincidencia perfecta— y
   Apocalypse Now se callaba teniendo 4082 contra 1000. Y el pico bueno
   estaba en la misma pantalla, en el gráfico de luminancia.
2. Los cinco stats de la card eran fijos, así que un RPU CMv2.9 enseñaba
   tres ceros grandes y el número que sostiene su veredicto iba en
   segunda posición sin distinguirse.
3. El alcance de cada dato se decía con una redacción distinta en cada
   bloque, o no se decía.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_radiografia_alcance -v
"""
import json
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
    """El fuente de una función top-level.

    Se cuenta desde la llave que abre el CUERPO, no desde el nombre: la
    primera `{` de `_rgrfRow(label, value, { tooltip = '' } = {})` es la
    del destructuring de la firma, y contando desde ahí la función se
    corta en la propia cabecera. El resultado no es un error legible —es
    un `SyntaxError` en la función SIGUIENTE, porque la anterior se quedó
    sin cerrar—, que es de las peores pistas posibles.
    """
    i = JS.find(f"\nfunction {nombre}(")
    if i < 0:
        raise AssertionError(f"función no encontrada: {nombre}")
    ini = i + 1
    # El `)` que cierra la lista de parámetros.
    par = 0
    j = JS.index("(", ini)
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
    raise AssertionError(f"función sin cerrar: {nombre}")


FUNCIONES = ("_rgrfAlcance", "_rgrfRow", "_rgrfQualityAuditCard",
             "_rgrfMasteringChain", "escHtml", "_fmtBytes")

DOM = """
globalThis.window = globalThis;
globalThis.document = { createElement: () => ({ set innerHTML(v) {}, }) };
"""


@unittest.skipUnless(NODE, "node no disponible")
class EnNode(unittest.TestCase):

    def evaluar(self, expr: str, preludio: str = "") -> str:
        guion = "\n".join([motor_i18n(), DOM, sistema_de_iconos(),
                           *(_funcion(n) for n in FUNCIONES), preludio,
                           f"process.stdout.write(JSON.stringify({expr}));"])
        r = subprocess.run(argv_node(guion), capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        return pintar_en(json.loads(r.stdout))


# Los dos casos reales del NAS, con los números tal cual se midieron.
PULP = {"l1_max_cll": 395.0, "l1_stats": {"peak": 1001}}      # SEI 1000
APOC = {"l1_max_cll": 1435.0, "l1_stats": {"peak": 4082}}     # SEI 1000


class TestElBannerUsaElPicoDeLaPelicula(EnNode):

    def _cadena(self, dv, sei):
        return self.evaluar("_rgrfMasteringChain(DV, HDR, {})",
                            f"const DV = {json.dumps(dv)};\n"
                            f"const HDR = {json.dumps({'max_cll': sei})};")

    def test_pulp_fiction_ya_no_saca_el_banner_rojo(self):
        """395 era el sniff; el pico real es 1001 contra un SEI de 1000."""
        self.assertNotIn("dv-mc-div-low", self._cadena(PULP, 1000))

    def test_apocalypse_now_si_lo_saca(self):
        """4082 contra 1000: el RPU es mucho más generoso que la etiqueta,
        y con el dato del sniff (1435) el aviso no salía."""
        self.assertIn("dv-mc-div-high", self._cadena(APOC, 1000))

    def test_sin_analisis_extendido_no_se_opina(self):
        """No hay pico de película, así que no hay comparación posible.
        Un aviso falso es peor que ninguno."""
        html = self._cadena({"l1_max_cll": 395.0}, 1000)
        self.assertNotIn("dv-mc-div-low", html)
        self.assertNotIn("dv-mc-div-high", html)

    def test_y_el_sniff_ya_no_alimenta_la_comparacion(self):
        """El guard de la mutación: con `l1_max_cll` de vuelta, Pulp
        Fiction vuelve a sacar el rojo."""
        self.assertIn("l1_stats?.peak", _funcion("_rgrfMasteringChain"))
        self.assertNotIn("const l1Peak  = dv?.l1_max_cll",
                         _funcion("_rgrfMasteringChain"))


class TestLaCardEnsenaLoQueAPLICA(EnNode):

    CMV29 = {"quality_classification": "real", "quality_verdict_text": "x",
             "quality_verdict_color": "green", "quality_l2_unique_count": 1605,
             "quality_l2_target_pqs": [2081, 2851, 3079], "quality_scene_cuts": 1177,
             "quality_total_frames_rpu": 222274, "quality_l8_unique_count": 0,
             "quality_frames_with_cmv40": 0, "quality_l3_unique_count": 0}
    CMV40 = {"quality_classification": "real", "quality_verdict_text": "x",
             "quality_verdict_color": "green", "quality_l8_unique_count": 210,
             "quality_l8_max_delta": 606, "quality_l2_unique_count": 44,
             "quality_scene_cuts": 1500, "quality_total_frames_rpu": 150000,
             "quality_frames_with_cmv40": 150000, "quality_l3_unique_count": 830}

    def _card(self, dv, v40):
        return self.evaluar("_rgrfQualityAuditCard(DV, V40)",
                            f"const DV = {json.dumps(dv)};\nconst V40 = {json.dumps(v40)};")

    def test_un_cmv29_no_ensena_tres_ceros(self):
        """Era el caso de Pulp Fiction: L8, L3 y «cobertura CMv4.0» a cero,
        con el mismo tamaño que el 1.605 que decide su veredicto."""
        html = self._card(self.CMV29, False)
        self.assertNotIn("cobertura", html.lower())
        self.assertIn("1605", html)   # es-ES no separa los millares de 4 dígitos
        # Ni un solo valor a cero entre los stats.
        valores = re.findall(r'class="dv-quality-stat-value">([^<]+)<', html)
        self.assertTrue(valores, "la card no pintó ningún stat")
        self.assertNotIn("0", [v.strip() for v in valores])

    def test_y_marca_cuál_decide(self):
        self.assertIn("dv-quality-stat-clave", self._card(self.CMV29, False))
        self.assertIn("dv-quality-stat-clave", self._card(self.CMV40, True))

    def test_un_cmv40_si_ensena_la_cobertura_y_el_maxdelta(self):
        html = self._card(self.CMV40, True)
        self.assertIn("100%", html)
        self.assertIn("606", html)

    def test_l3_informa_pero_no_es_un_stat(self):
        """Sobre 40 bins acierta el 57 % —azar— porque lo genera el
        análisis de Dolby. Al lado de los que deciden pesaba igual."""
        html = self._card(self.CMV40, True)
        self.assertIn("dv-quality-nota", html)
        self.assertIn("830", html)
        stats = html.split('dv-quality-nota')[0]
        self.assertNotIn("830", stats, "L3 sigue entre los stats que deciden")


class TestElAlcanceSeDiceUnaSolaVez(EnNode):

    def test_la_muestra_se_marca_y_la_pelicula_tambien(self):
        self.assertIn("30 s", self.evaluar("_rgrfAlcance(true)"))
        self.assertIn("dv-alcance-muestra", self.evaluar("_rgrfAlcance(true)"))
        self.assertIn("dv-alcance-film", self.evaluar("_rgrfAlcance(false)"))

    def test_los_tres_bloques_usan_el_MISMO_helper(self):
        """L5, L8 y el pico L1 tenían cada uno su redacción. Con tres
        textos distintos, el cuarto bloque que llegue inventará un
        cuarto."""
        cuerpo = JS[JS.index("function _renderMkvDvRadiography("):]
        cuerpo = cuerpo[:cuerpo.index("\nfunction _rgrfQualityAuditCard")]
        self.assertGreaterEqual(cuerpo.count("_rgrfAlcance("), 2)
        for vieja in ("tab2.l5_sample_30s_corre_el_perfil",
                      "tab2.l5_validado_en_todo_el_film",
                      "tab2.l8_escala_sample_30s",
                      "tab2.l8_escala_validado_film_completo"):
            self.assertNotIn(vieja, JS, f"quedó la redacción suelta {vieja}")


if __name__ == "__main__":
    unittest.main()
