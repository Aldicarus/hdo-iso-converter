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
             "_rgrfMasteringChain", "_l8NitsLabel", "_rgrfL8Svg", "_rgrfTablaDeNiveles",
             "_rgrfTitular",
             "escHtml", "_fmtBytes")

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


# Los dos MKV reales del NAS, con sus valores tal cual se midieron.
# Pulp Fiction es CMv2.9 con análisis extendido; Backrooms, CMv4.0 con L3
# de UN combo — el caso que las pills no sabían distinguir de uno de 830.
PULP_COMPLETO = {
    "cm_version": "v2.9", "has_l1": True, "has_l2": True, "has_l5": True,
    "has_l6": True, "has_l4": True, "has_l3": False, "has_l8": False,
    "has_l9": False, "has_l10": False, "has_l11": False,
    "l5_top": 0, "l5_bottom": 0, "l5_left": 0, "l5_right": 0,
    "l6_max_cll": 0, "quality_classification": "real",
    "quality_l2_unique_count": 1605, "quality_l2_target_pqs": [2081, 2851, 3079],
    "quality_l3_unique_count": 0, "quality_l8_unique_count": 0,
    "l1_stats": {"peak": 1001, "avg_of_max": 87},
    "l1_references": {"l6_master_max_nits": 1000}, "niveles_medidos": True,
}
BACKROOMS = {
    "cm_version": "v4.0", "has_l3": True, "has_l4": True, "has_l8": True,
    "has_l9": True, "has_l10": False, "has_l11": True,
    "l9_primaries": "Display P3", "l11_content_type": "Cinema",
    "l8_trim_nits": [100, 600], "quality_classification": "tone_mapping",
    "quality_l3_unique_count": 1, "quality_l2_unique_count": 8,
    "quality_l8_unique_count": 3, "quality_l8_max_delta": 12,
    "niveles_medidos": True,
}


class TestLaTablaDeNiveles(EnNode):
    """Las siete pills binarias eran un cajón, y dos no podían encenderse."""

    def _tabla(self, dv, hdr=None):
        return self.evaluar("_rgrfTablaDeNiveles(DV, HDR)",
                            f"const DV = {json.dumps(dv)};\n"
                            f"const HDR = {json.dumps(hdr or {})};")

    def test_l254_YA_SE_MIDE(self):
        """`--levels` no lo acepta, pero `info -f` trae el frame entero.

        Lo preguntó el usuario el 2026-09-24 —«¿por qué sale siempre no
        medido si tenemos el análisis extendido?»— y la respuesta era que
        sólo se había mirado una de las dos vías.
        """
        fila = [f for f in self._tabla(BACKROOMS).split("<tr") if ">L254<" in f]
        self.assertEqual(len(fila), 1, "falta la fila de L254")
        self.assertNotIn("no medido", fila[0])

    def test_un_nivel_medido_y_ausente_dice_ausente(self):
        """L10 se pide al export y sale vacío: su ausencia es un dato."""
        fila = [f for f in self._tabla(BACKROOMS).split("<tr") if ">L10<" in f]
        self.assertIn("ausente", fila[0])
        self.assertNotIn("no medido", fila[0])

    def test_pero_sin_export_NINGUNO_dice_ausente(self):
        """«Ausente» sólo se puede afirmar si se ha mirado.

        El enriquecimiento va en un `try` que no bloquea; al fallar, los
        flags se quedan en su `False` por defecto y la tabla estaría
        afirmando una ausencia comprobada sobre un análisis que no llegó
        a correr. Es el defecto que tenía L254, con otra causa.
        """
        sin = {k: v for k, v in BACKROOMS.items() if k != "niveles_medidos"}
        html = self._tabla(sin)
        # Sólo los que NO tienen el flag puesto: uno en True se midió por
        # definición, y ahí «presente» es correcto con export o sin él.
        for nivel in ("L10", "L254"):
            fila = [f for f in html.split("<tr") if f">{nivel}<" in f][0]
            self.assertIn("no medido", fila, f"{nivel} afirma una ausencia sin mirar")

    def test_l3_ya_dice_CUANTO(self):
        """Un combo y ochocientos eran la misma pill verde."""
        pocos = [f for f in self._tabla(BACKROOMS).split("<tr") if ">L3<" in f][0]
        self.assertIn("1", pocos)
        muchos = dict(BACKROOMS, quality_l3_unique_count=830)
        self.assertIn("830", [f for f in self._tabla(muchos).split("<tr") if ">L3<" in f][0])

    def test_la_tabla_se_pinta_TAMBIEN_en_cmv29(self):
        """Con `isV40` no existía en 8 de los 10 MKV del NAS, y con ella
        se iba L4 — que 5 de esos 8 tienen."""
        html = self._tabla(PULP_COMPLETO)
        for nivel in ("L1", "L2", "L3", "L4", "L5", "L6", "L8", "L9", "L10", "L11", "L254"):
            self.assertIn(f">{nivel}<", html, f"falta {nivel}")
        self.assertIn("1605", html)

    def test_el_alcance_va_por_fila(self):
        """L1 de la película y L9 de los primeros 30 s, en la misma tabla."""
        html = self._tabla(PULP_COMPLETO)
        l1 = [f for f in html.split("<tr") if ">L1<" in f][0]
        l9 = [f for f in html.split("<tr") if ">L9<" in f][0]
        self.assertIn("dv-alcance-film", l1)
        self.assertIn("dv-alcance-muestra", l9)
        self.assertIn("1001", l1)

    def test_las_pills_no_vuelven(self):
        cuerpo = _funcion("_renderMkvDvRadiography")
        self.assertNotIn("dv-pill-row", cuerpo)
        self.assertNotIn("blockCmv4", JS)

    def test_y_el_CALLER_no_la_condiciona(self):
        """El de arriba llama a la tabla directamente, así que no vería
        un `isV40 ?` en quien la monta — y esa era justamente la avería:
        el bloque no existía en 8 de los 10 MKV del NAS. El invariante
        es de forma, así que se comprueba en la forma.
        """
        cuerpo = _funcion("_renderMkvDvRadiography")
        linea = [l for l in cuerpo.splitlines() if "_rgrfTablaDeNiveles(" in l]
        self.assertEqual(len(linea), 1, "la tabla se monta en un solo sitio")
        self.assertNotIn("isV40", linea[0])
        self.assertNotIn("?", linea[0], "montada sin condición")

    def test_un_dv_vacio_no_revienta(self):
        """El análisis básico puede no traer nada de esto."""
        html = self._tabla({})
        self.assertIn(">L1<", html)
        self.assertIn("no medido", html)


class TestElTitular(EnNode):
    """Las tres frases sustituyen a la card del veredicto."""

    LECTURA = [
        {"rotulo": "Qué es", "texto": "Dolby Vision Profile 7 FEL sobre HDR10.",
         "conclusion": "La capa de mejora es completa."},
        {"rotulo": "El máster", "texto": "Masterizado en BT.2020 a 1000 nits.",
         "conclusion": "Master CMv2.9 nativo"},
        {"rotulo": "La luz", "texto": "Pico 1001 nits y mediana 437.",
         "conclusion": "El pico medido coincide con el máster."},
    ]

    def _titular(self, lectura, dv=None):
        return self.evaluar("_rgrfTitular(A, DV)",
                            f"const A = {json.dumps({'lectura': lectura})};\n"
                            f"const DV = {json.dumps(dv or {})};")

    def test_pinta_las_tres_con_su_conclusion(self):
        html = self._titular(self.LECTURA)
        for f in self.LECTURA:
            self.assertIn(f["rotulo"], html)
            self.assertIn(f["texto"], html)
            self.assertIn(f["conclusion"], html)

    def test_la_conclusion_se_distingue_del_dato(self):
        """Lo interpretado no puede leerse igual que lo medido."""
        self.assertIn("dv-titular-conclusion", self._titular(self.LECTURA))

    def test_conserva_el_punto_de_color_del_veredicto(self):
        """Es lo único de la card vieja que no era un número: dice de un
        vistazo si el máster es bueno."""
        html = self._titular(self.LECTURA, {"quality_verdict_color": "green",
                                            "quality_classification": "real"})
        self.assertIn("punto-conf alta", html)

    def test_sin_auditoria_ofrece_el_analisis_extendido(self):
        html = self._titular(self.LECTURA)
        self.assertIn("data-analisis-extendido", html)

    def test_y_con_ella_ofrece_re_analizar(self):
        html = self._titular(self.LECTURA, {"quality_classification": "real"})
        self.assertNotIn("data-analisis-extendido", html)
        self.assertIn("_rgrfAuditQuality", html)

    def test_sin_lectura_lo_dice_y_no_revienta(self):
        html = self._titular(None)
        self.assertIn("dv-titular-vacio", html)

    def test_la_card_vieja_ya_no_encabeza_la_radiografia(self):
        """El guard del caller: el test de arriba llama al titular
        directamente y no vería que quien monta el bloque volviera a la
        card — que es de lo que se trata."""
        cuerpo = _funcion("_renderMkvDvRadiography")
        linea = [l for l in cuerpo.splitlines() if "blockQuality =" in l]
        self.assertEqual(len(linea), 1)
        self.assertIn("_rgrfTitular", linea[0])


if __name__ == "__main__":
    unittest.main()
