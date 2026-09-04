"""Tests de `rpu_analyze.pistas_de_procedencia` — el L5 declarado en el nombre.

Las fixtures son **nombres reales de bins del corpus del NAS** (99 proyectos con
nombre de bin y perfil L5 conocido). Si un test de aquí falla, hay un bin de
verdad que se clasificaría distinto.

Medido sobre ese corpus::

    n=99   TP=6   FP=0   FN=7   TN=86   →   precisión 100%, recall 46%

De ahí las dos familias de tests que importan: los que declaran (y siempre
aciertan) y los que **callan aunque el bin sea variable** — el silencio no
concluye nada, y eso hay que dejarlo fijado para que nadie use esta señal
para relajar un veredicto.

Ejecutar desde la raíz del repo:
    .venv/bin/python -m unittest app.tests.test_cmv40_procedencia -v
"""
import sys
import unittest
from pathlib import Path

# Permite ejecutar el test sin instalar el paquete
sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.rpu_analyze import pistas_de_procedencia  # noqa: E402


# ── Corpus real ──────────────────────────────────────────────────────────────

# Los que SÍ declaran el L5 variable en el nombre (todos con L5 realmente
# variable). Sinners aparece dos veces en el corpus; con una vez basta.
NOMBRES_QUE_DECLARAN = [
    ("How to Train Your Dragon 2025 IMAX BD_P7 FEL_(cmv4.0 restored).bin",
     "imax"),
    ("Bring.Her.Back.2025.UHDBD_P7 FEL_(cmv4.0 restored) REPACK Variable L5.bin",
     "variable l5"),
    ("Sinners.2025.BD P7 MEL Variable L5 (retail cmv4.0 restored).bin",
     "variable l5"),
    ("Project.Hail.Mary.2026.UHD-BD_P7 MEL_variable_L5_(retail cmv4.0 restored).bin",
     "variable l5"),
    ("The Mandalorian and Grogu 2026 UHD BD_P7 FEL REPACK (retail cmv4.0 restored) VARIABLE L5.bin",
     "variable l5"),
]

# Los que NO lo declaran aunque el bin SÍ sea variable (los falsos negativos
# del corpus). Fijan que el silencio no concluye nada.
NOMBRES_QUE_CALLAN_SIENDO_VARIABLES = [
    "GOAT.2026.UHDBD_P7 MEL (retail cmv4.0 restored).bin",
    "The.Phoenician.Scheme.2025.BD_FEL_P7 Retail (cmv4.0 restored).bin",
    "Marty.Supreme.2025.UHD-BD_P7 FEL (retail cmv4.0 restored).bin",
    "NO.COUNTRY.FOR.OLD.MEN.2007.P7.FEL_RPU_Original_L1_CMV4.0_Flag.bin",
    "Trainspotting.Criterion.Collection.2160p.UHD.Blu-ray.P7.FEL_Original_L1_CMV4.0_Flag.bin",
]

# Nombres corrientes del repo: ninguno puede disparar la señal.
NOMBRES_NORMALES = [
    "Superman.2025.UHDBD_P7 FEL (cmv4.0 restored).bin",
    "Sinners.2025.BD P7 MEL (retail cmv4.0 restored).bin",
    "Dune.Part.Two.2024.UHD-BD_P7 FEL_(cmv4.0 restored).bin",
    "The.Batman.2022.UHDBD_P7 MEL (retail cmv4.0 restored).bin",
]


class TestNombresQueDeclaran(unittest.TestCase):
    """Los 5 nombres del corpus que declaran el L5 variable."""

    def test_todos_declaran_con_su_token(self):
        for nombre, token_esperado in NOMBRES_QUE_DECLARAN:
            with self.subTest(nombre=nombre):
                r = pistas_de_procedencia(nombre)
                self.assertTrue(
                    r["declara_l5_variable"],
                    f"debería declarar L5 variable: {nombre}")
                self.assertIn(token_esperado, r["tokens"])

    def test_variable_l5_con_guiones_bajos_se_detecta(self):
        """Project Hail Mary: `_variable_L5_` — el que se escapa sin normalizar.

        Sin convertir `_` en espacio, la subcadena buscada nunca aparece y el
        bin pasa por "no declara" siendo un caso real de L5 variable.
        """
        r = pistas_de_procedencia(
            "Project.Hail.Mary.2026.UHD-BD_P7 MEL_variable_L5_(retail cmv4.0 restored).bin")
        self.assertTrue(r["declara_l5_variable"])
        self.assertEqual(r["tokens"], ["variable l5"])

    def test_token_imax_se_detecta(self):
        """How to Train Your Dragon: el IMAX del nombre es la única pista."""
        r = pistas_de_procedencia(
            "How to Train Your Dragon 2025 IMAX BD_P7 FEL_(cmv4.0 restored).bin")
        self.assertTrue(r["declara_l5_variable"])
        self.assertEqual(r["tokens"], ["imax"])

    def test_mayusculas_y_minusculas_dan_igual(self):
        for variante in ("VARIABLE L5", "Variable L5", "variable l5"):
            with self.subTest(variante=variante):
                r = pistas_de_procedencia(f"Peli.2025.BD_P7 FEL {variante}.bin")
                self.assertEqual(r["tokens"], ["variable l5"])

    def test_los_tokens_van_en_orden_de_tabla_y_sin_repetir(self):
        # `variable l5` va antes que `imax` en la tabla, aunque en el nombre
        # aparezca después. Y `Variable L5` dos veces sigue siendo un token.
        r = pistas_de_procedencia(
            "Peli.2025.IMAX.BD_P7 FEL Variable L5 REPACK variable_l5.bin")
        self.assertEqual(r["tokens"], ["variable l5", "imax"])


class TestElSilencioNoConcluyeNada(unittest.TestCase):
    """Los 5 nombres del corpus con L5 variable que NO lo declaran.

    Son los falsos negativos que dejan el recall en 46%. La función tiene que
    decir `False` — y quien la consuma no puede leer ese `False` como "el L5
    es constante".
    """

    def test_no_declaran_y_no_traen_tokens(self):
        for nombre in NOMBRES_QUE_CALLAN_SIENDO_VARIABLES:
            with self.subTest(nombre=nombre):
                r = pistas_de_procedencia(nombre)
                self.assertFalse(r["declara_l5_variable"], nombre)
                self.assertEqual(r["tokens"], [])


class TestSinFalsosPositivos(unittest.TestCase):
    """Cero falsos positivos: es lo que hace utilizable la señal."""

    def test_nombres_corrientes_no_disparan(self):
        for nombre in NOMBRES_NORMALES:
            with self.subTest(nombre=nombre):
                r = pistas_de_procedencia(nombre)
                self.assertFalse(r["declara_l5_variable"], nombre)
                self.assertEqual(r["tokens"], [])

    def test_imax_dentro_de_otra_palabra_no_cuenta(self):
        """`Climax` contiene `imax` — sintético, no está en el corpus.

        Un token suelto es señal; la misma subcadena dentro de un título no lo
        es. Por eso la búsqueda va con frontera de palabra.
        """
        r = pistas_de_procedencia("Climax.2018.UHDBD_P7 FEL (cmv4.0 restored).bin")
        self.assertFalse(r["declara_l5_variable"])
        self.assertEqual(r["tokens"], [])


class TestEntradasVacias(unittest.TestCase):
    """Tolerante a None y "" en los dos argumentos."""

    def test_nombre_none_no_revienta(self):
        r = pistas_de_procedencia(None)
        self.assertFalse(r["declara_l5_variable"])
        self.assertEqual(r["tokens"], [])
        self.assertIsNone(r["nota_sheet_l5"])

    def test_nombre_vacio_no_revienta(self):
        r = pistas_de_procedencia("")
        self.assertFalse(r["declara_l5_variable"])
        self.assertEqual(r["tokens"], [])
        self.assertIsNone(r["nota_sheet_l5"])

    def test_notas_none_y_vacias_no_revientan(self):
        for notas in (None, "", "   "):
            with self.subTest(notas=notas):
                r = pistas_de_procedencia("Peli.2025.bin", notas)
                self.assertIsNone(r["nota_sheet_l5"])


class TestNotaDeLaHoja(unittest.TestCase):
    """La nota es CONTEXTO para el usuario, no un detector.

    En el corpus solo 1 de los 24 proyectos con notas menciona L5, así que
    nunca puede tocar `declara_l5_variable`.
    """

    def test_nota_con_l5_se_recoge_sin_declarar_nada(self):
        r = pistas_de_procedencia(
            "GOAT.2026.UHDBD_P7 MEL (retail cmv4.0 restored).bin",
            "L5 edit: https://justpaste.it/hecft",
        )
        self.assertEqual(r["nota_sheet_l5"], "L5 edit: https://justpaste.it/hecft")
        self.assertFalse(r["declara_l5_variable"])
        self.assertEqual(r["tokens"], [])

    def test_nota_sin_l5_no_se_recoge(self):
        r = pistas_de_procedencia(
            "GOAT.2026.UHDBD_P7 MEL (retail cmv4.0 restored).bin",
            "cmv4.0 bloc can be restored to the P7 RPU (workflow 2-3)",
        )
        self.assertIsNone(r["nota_sheet_l5"])

    def test_la_nota_no_puede_encender_la_senal(self):
        # Aunque la nota diga literalmente "variable L5", el veredicto sale
        # solo del NOMBRE.
        r = pistas_de_procedencia(
            "GOAT.2026.UHDBD_P7 MEL (retail cmv4.0 restored).bin",
            "this one has variable L5 across the movie",
        )
        self.assertFalse(r["declara_l5_variable"])
        self.assertEqual(r["tokens"], [])
        self.assertIsNotNone(r["nota_sheet_l5"])

    def test_nota_muy_larga_se_recorta(self):
        larga = "L5 " + ("x" * 400)
        r = pistas_de_procedencia("Peli.2025.bin", larga)
        self.assertLessEqual(len(r["nota_sheet_l5"]), 201)  # 200 + el elipsis
        self.assertTrue(r["nota_sheet_l5"].endswith("…"))


class TestFormaDelResultado(unittest.TestCase):

    def test_devuelve_las_tres_claves_siempre(self):
        for nombre in ["", "Peli.bin", NOMBRES_QUE_DECLARAN[0][0]]:
            with self.subTest(nombre=nombre):
                r = pistas_de_procedencia(nombre)
                self.assertEqual(
                    set(r.keys()),
                    {"declara_l5_variable", "tokens", "nota_sheet_l5"})
                self.assertIsInstance(r["declara_l5_variable"], bool)
                self.assertIsInstance(r["tokens"], list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
