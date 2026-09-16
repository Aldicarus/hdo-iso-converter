"""La traducción no solo tiene que estar: tiene que ser buena.

Los doce guards de `test_i18n_completo` y `test_plantillas_del_js` validan
**presencia** —que no quede castellano, que no salga una clave, que las tres
lenguas cuadren— y con todos en verde el usuario leyó la cabecera en inglés y
encontró en dos segundos «DV+HDR X-ray», calco de «radiografía», que en
inglés es la placa del hospital.

La causa es estructural: el catálogo se tradujo **clave por clave y en orden
alfabético**, y una cadena aislada no se puede juzgar. Lo que arregla eso es
una revisión por pantalla (`revision.py` la hace posible); lo que **evita que
vuelva** es este fichero, con los defectos que sí se pueden detectar.

Lo que NO está aquí, a propósito: un detector de «el mismo término castellano
traducido de dos formas» por n-gramas. Se escribió y se midió — **252
candidatos con ~5 reales**: marca como disidente toda reestructuración
legítima («Series name» por «Nombre de la serie» es MEJOR inglés, no un
defecto). Un guard con esa precisión se desactiva a la semana.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_calidad_de_la_traduccion -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))


def _catalogos():
    for donde, base in (("frontend", APP_DIR / "static" / "i18n"),
                        ("backend", APP_DIR / "i18n")):
        yield donde, {l: json.loads((base / f"{l}.json").read_text(encoding="utf-8"))
                      for l in ("es", "en", "ca")}


# Calcos y registro. Cada entrada con el motivo, porque el motivo es el guard:
# sin él, el siguiente que lea esto no sabe si borrar la línea.
TRAMPAS = {
    r"\bX-ray\b":
        "calco de «radiografía», que en inglés es la placa del hospital; "
        "aquí es figurado y se dice «deep dive»",
    r"\bplease\b|\bwe are sorry\b|\bsorry\b":
        "cortesía ornamental — el REGISTRO la prohíbe en las tres lenguas",
    r"\bsuccessfully\b":
        "cortesía ornamental: «completed successfully» dice lo mismo que "
        "«complete» y suena a asistente",
    r"\w+ included\)":
        "«(X included)» se lee como «pilas incluidas» — que X viene con la app",
    # El tramo entre los dos «of the» no puede llevar dígitos ni paréntesis:
    # `max(40% of the median, 25% of the maximum)` es una FÓRMULA, y ahí el
    # encadenado es correcto en inglés. Sin ese matiz el guard señalaba a la
    # única cadena del catálogo que no podía escribirse de otra forma.
    r"\bof the\b[^.;()\d]{1,40}\bof the\b":
        "encadenado castellano («de la … del …»): en inglés el posesivo va "
        "delante y la frase se acorta",
    r"\bactualy\b|\brealize\b|\beventually\b|\bassist\b":
        "falsos amigos de «actualmente», «realizar», «eventualmente», «asistir»",
    r"\bintroduce (your|the) \w*(key|url|title)\b":
        "calco de «introducir» (teclear): en inglés es «enter»",
    r"\d\s%":
        "en inglés no va espacio antes del «%» (en castellano y catalán sí)",
}


class TestElInglesNoLlevaCalcos(unittest.TestCase):

    def test_ninguna_trampa_conocida(self):
        malas = []
        for donde, cat in _catalogos():
            for k, v in cat["en"].items():
                for rx, motivo in TRAMPAS.items():
                    if re.search(rx, v, re.I):
                        malas.append(f"[{donde}] `{k}`: {v[:60]}\n      → {motivo}")
        self.assertEqual(sorted(malas), [], (
            f"\n{len(malas)} calco(s) o fallo(s) de registro en inglés:\n  · "
            + "\n  · ".join(sorted(malas)[:10])))

    def test_las_comillas_angulares_no_viajan_al_ingles(self):
        """`«»` son de castellano y catalán; el inglés usa `""`.

        Eran **15**, y no es cosmético: en una frase inglesa el guillemet se
        lee como una cita de otro idioma.
        """
        malas = [f"[{donde}] `{k}`" for donde, cat in _catalogos()
                 for k, v in cat["en"].items() if "«" in v or "»" in v]
        self.assertEqual(malas, [], "\n  · ".join([""] + malas))


class TestLaOrtografiaInglesaEsUnaSola(unittest.TestCase):
    """`analyze` 58 vs `analyse` 16, `canceled` 3 vs `cancelled` 4.

    La app está en **inglés de EE. UU.**: es la mayoría medida y el
    vocabulario del dominio —`colorist`, `color primaries`, la documentación
    de Dolby— es americano. El `en-GB` de `localeActual()` es solo para las
    FECHAS (día antes del mes, como en las otras dos lenguas) y es otro eje.
    """

    # `analysis`/`analyses` son el SUSTANTIVO y se escriben igual en las dos
    # ortografías: el guard solo mira el verbo. Normalizar sin ese matiz dejó
    # «The analyzes are still saved», que es una falta.
    PARES = [("analyse/analyze", r"\banalys(e|ed|ing)\b", r"\banalyz(e|ed|ing)\b"),
             ("cancel(l)ed",     r"\bcancell(ed|ing)\b",      r"\bcancel(ed|ing)\b"),
             ("behaviour",       r"\bbehaviour\b",            r"\bbehavior\b"),
             ("artefact",        r"\bartefact(s?)\b",         r"\bartifact(s?)\b"),
             ("normalise",       r"\bnormalis\w*\b",          r"\bnormaliz\w*\b")]

    def test_no_se_mezcla_us_con_uk(self):
        malas = []
        for etiqueta, rx_uk, _ in self.PARES:
            for donde, cat in _catalogos():
                for k, v in cat["en"].items():
                    if re.search(rx_uk, v, re.I):
                        malas.append(f"[{donde}] `{k}`: forma británica de {etiqueta}")
        self.assertEqual(sorted(malas), [], (
            "\nla app está en inglés de EE. UU.:\n  · " + "\n  · ".join(sorted(malas)[:10])))


class TestElCatalanNoUsaElGerundioPelado(unittest.TestCase):
    """«Carregant…» es el castellanismo más común del software en catalán.

    Un gerundio no puede encabezar una oración independiente; un rótulo de
    progreso se dice «S'està carregant…» (o «S'estan …» concordando con el
    objeto). Eran **42 de 54**; los otros 12 ya lo hacían bien.
    """

    # El auxiliar concuerda con el objeto, así que no se puede autocorregir:
    # lo que el guard exige es que NO quede un gerundio pelado.
    GERUNDIO = re.compile(r"^[A-ZÀÈÉÍÒÓÚ]?[a-zàèéíòóúïüç·']*(ant|ent)\b")

    def test_ningun_rotulo_de_progreso_empieza_por_gerundio(self):
        malas = []
        for donde, cat in _catalogos():
            for k, es in cat["es"].items():
                if "…" not in es or not re.match(r"^[A-ZÁÉÍÓÚ]?\w*(ando|endo)\b", es):
                    continue
                ca = cat["ca"].get(k, "")
                if self.GERUNDIO.match(ca):
                    malas.append(f"[{donde}] `{k}`: {ca[:56]}")
        self.assertEqual(sorted(malas), [], (
            f"\n{len(malas)} rótulo(s) de progreso en catalán con gerundio "
            f"pelado.\nUsa «S'està/S'estan + gerundi», concordando con el "
            f"objeto:\n  · " + "\n  · ".join(sorted(malas)[:10])))

    def test_el_articulo_de_rpu_se_apostrofa(self):
        """En catalán «el RPU» es «l'RPU»: la sigla empieza por vocal. Eran 104."""
        malas = [f"[{donde}] `{k}`" for donde, cat in _catalogos()
                 for k, v in cat["ca"].items()
                 if re.search(r"\b[Ee]l RPU\b|\bde el RPU\b|\bal RPU\b", v)]
        self.assertEqual(malas, [], "\n  · ".join([""] + malas[:10]))


if __name__ == "__main__":
    unittest.main()
