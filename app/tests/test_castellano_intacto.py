"""El castellano actual no se toca. Este test es lo que lo hace verificable.

La traducción a inglés y catalán mueve ~2.650 frases castellanas de estar
incrustadas en el código a estar en un catálogo. Ese movimiento **no debe
cambiar ni una palabra del castellano**, y una intención no basta: un cambio de
una coma en una frase que se mueve de sitio no lo nota nadie leyendo el diff de
un refactor de 3.000 líneas.

`golden_castellano.json` se capturó con `captura_castellano.py` sobre el estado
`pre-i18n` (etiqueta de git, punto de retorno). Cada frase de ahí tiene que
seguir existiendo, **byte a byte**, en alguno de estos dos sitios:

  · el código, si todavía no se ha extraído;
  · el catálogo `es`, si ya se extrajo.

Así el test acompaña la migración entera: pasa antes de empezar, pasa a mitad y
pasa al final, y solo falla si el castellano cambia.

## Las frases reescritas

Las 248 plantillas con interpolación **hay que reescribirlas** —un fragmento
suelto no se puede traducir— y ahí el literal castellano sí cambia de forma.
Para esas va `EXCEPCIONES`: cada una declara qué frase desapareció y por qué, y
**la lista no se puede ampliar sin escribir el motivo**. Lo que se conserva en
esos casos es el texto RENDERIZADO, y eso lo comprueba
`test_mensajes_con_parametros.py` llamando a `t()` con parámetros de ejemplo.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_castellano_intacto -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import captura_castellano as captura  # noqa: E402

GOLDEN = APP_DIR / "tests" / "golden_castellano.json"

# Frases que han cambiado de FORMA a propósito, con el motivo. Formato:
#   "frase exacta del golden": "por qué ya no existe tal cual"
# Ampliarla es una decisión, no un arreglo: si una frase desaparece sin entrada
# aquí, el test falla y hace bien.
EXCEPCIONES: dict[str, str] = {}


def _catalogo_es() -> set[str]:
    """Las frases castellanas ya extraídas: catálogo de UI y manual.

    El manual va aparte porque no es un catálogo de claves sino tres
    documentos paralelos de HTML, así que sus frases se sacan parseándolo
    igual que se sacaron del código.
    """
    fuera: set[str] = set()
    manual = APP_DIR / "static" / "i18n" / "manual" / "es.json"
    if manual.exists():
        for html in json.loads(manual.read_text(encoding="utf-8")).values():
            fuera.update(x for x in captura._del_html(
                re.sub(r"\$\{[^}]*\}", " ⟦⟧ ", html)) if captura.es_frase(x))
    ruta = APP_DIR / "static" / "i18n" / "es.json"
    if not ruta.exists():
        return fuera
    def hojas(nodo):
        if isinstance(nodo, str):
            yield " ".join(nodo.split())
        elif isinstance(nodo, dict):
            for v in nodo.values():
                yield from hojas(v)
        elif isinstance(nodo, list):
            for v in nodo:
                yield from hojas(v)
    fuera.update(hojas(json.loads(ruta.read_text(encoding="utf-8"))))
    return fuera


class TestElCastellanoSigueSiendoElMismo(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        cls.vivas = (captura.frases_del_frontend()
                     | captura.frases_del_backend()
                     | _catalogo_es())

    def _comprobar(self, clave: str):
        esperadas = set(self.golden[clave])
        faltan = sorted(f for f in esperadas - self.vivas
                        if f not in EXCEPCIONES)
        self.assertEqual(faltan, [], (
            f"\n{len(faltan)} frase(s) castellanas de `{clave}` han "
            f"desaparecido o cambiado.\nSi el cambio es deliberado, añádelas a "
            f"EXCEPCIONES con el motivo:\n  · "
            + "\n  · ".join(faltan[:15])))

    def test_las_frases_del_frontend_siguen_intactas(self):
        self._comprobar("frontend")

    def test_las_frases_del_backend_siguen_intactas(self):
        self._comprobar("backend")


class TestElGoldenEsUtil(unittest.TestCase):
    """Un golden vacío o con basura pasaría siempre: no vigilaría nada."""

    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_tiene_el_volumen_que_se_midio(self):
        self.assertGreater(len(self.golden["frontend"]), 2000)
        self.assertGreater(len(self.golden["backend"]), 300)

    def test_no_se_ha_colado_codigo(self):
        malas = [f for f in self.golden["frontend"] + self.golden["backend"]
                 if not captura.es_frase(f)]
        self.assertEqual(malas, [], f"entradas que no son frases: {malas[:5]}")

    def test_cada_excepcion_lleva_su_motivo(self):
        sin_motivo = [k for k, v in EXCEPCIONES.items() if len(v.strip()) < 15]
        self.assertEqual(sin_motivo, [],
                         "excepciones sin explicar por qué cambió la frase")

    def test_ninguna_excepcion_sobra(self):
        """Una excepción que ya no hace falta oculta un cambio futuro."""
        vivas = (captura.frases_del_frontend() | captura.frases_del_backend()
                 | _catalogo_es())
        sobran = sorted(k for k in EXCEPCIONES if k in vivas)
        self.assertEqual(sobran, [],
                         f"excepciones que ya no aplican: {sobran[:5]}")


if __name__ == "__main__":
    unittest.main()
