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
EXCEPCIONES: dict[str, str] = {
    # ── Los tres plurales por sufijo de una letra, partidos en dos claves.
    #
    # `{p2}` = 's'/'' y `{p3}` = 'n'/'' pluralizan en castellano por pura
    # coincidencia ortográfica: el inglés no tiene ninguna palabra que se
    # pluralice añadiendo una `n` («not foundn») y el catalán tampoco cuando
    # el plural es irregular («dia» → «dies»). El castellano RENDERIZADO no
    # cambia — sale «1 saltado (ya existía)» y «3 saltados (ya existían)»
    # igual que antes—, lo que cambia es que son dos claves en vez de una con
    # un hueco.
    "⟦⟧ saltado ⟦⟧ (ya existía ⟦⟧ )":
        "partida en `tab1.saltado_ya_existia_uno` / `_varios`",
    "hace ⟦⟧ día ⟦⟧":
        "partida en `tab1.hace_dia_uno` / `_varios`",
    "⟦⟧ no existe ⟦⟧ — ejecuta Fase F primero (workflow ⟦⟧ )":
        "partida en `cmv40_pipeline.no_existe_ejecuta_fase_f_primero_uno` / `_varios`",

    # ── Un valor castellano que se colaba por el hueco de un parámetro.
    #
    # `Corrección {p1}` con `p1` ∈ {'adicional', 'manual'}: el adjetivo es un
    # literal del JS, así que en inglés y en catalán saldría en castellano
    # dentro de la frase. Son dos claves completas.
    "Corrección ⟦⟧":
        "partida en `tab3.correccion_adicional` / `tab3.correccion_manual`",

    # ── El mensaje con una plantilla DENTRO de un `${…}`.
    #
    # El extractor no puede delimitar eso con un regex —se corta en el primer
    # backtick anidado— así que este mensaje se quedó a medio extraer: un
    # trozo con `data-i18n` y el resto en castellano, con la sintaxis de la
    # ternaria incluida en lo que capturó el golden. Reescrito sacando la
    # ternaria a un `const`, es UNA clave con tres parámetros.
    "Se detecta un desfase de":
        "absorbida en `tab3.se_detecta_un_desfase_que_la_hoja_no_explica`",
    ": 'no consta ningún desfase'}). Revisa el chart antes de inyectar.":
        "era la cola de la ternaria anidada; hoy es la misma clave de arriba",
}


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
    # Los dos catálogos: el del frontend y el del BACKEND (`app/i18n/`), que
    # es donde han ido las 483 frases del log y de los errores HTTP.
    rutas = [APP_DIR / "static" / "i18n" / "es.json",
             APP_DIR / "i18n" / "es.json"]
    if not any(r.exists() for r in rutas):
        return fuera
    def hojas(nodo):
        """Los valores TAL CUAL, sin normalizar el espacio.

        Normalizar aquí dentro se llevaba por delante los saltos de línea
        antes de que nadie pudiera convertirlos a la forma del golden, que es
        justo la última equivalencia de abajo.
        """
        if isinstance(nodo, str):
            yield nodo
        elif isinstance(nodo, dict):
            for v in nodo.values():
                yield from hojas(v)
        elif isinstance(nodo, list):
            for v in nodo:
                yield from hojas(v)
    crudos: set[str] = set()
    for r in rutas:
        if r.exists():
            crudos |= set(hojas(json.loads(r.read_text(encoding="utf-8"))))
    valores = {" ".join(v.split()) for v in crudos}
    fuera |= valores
    # Y la misma frase con los huecos normalizados al centinela del golden.
    #
    # Las frases interpoladas se capturaron con `${…}` sustituido por `⟦⟧`, y
    # al convertirlas en mensajes con parámetros pasaron a llevar `{max}`,
    # `{total}`… Es el MISMO hueco escrito de otra forma, no un cambio del
    # castellano: las palabras de alrededor tienen que seguir coincidiendo
    # byte a byte, y eso es lo que se comprueba. Listar cincuenta excepciones
    # habría escondido justo lo que el guard existe para ver.
    fuera |= {" ".join(re.sub(r"\{\w+\}", " ⟦⟧ ", v).split()) for v in valores}
    # Y la misma frase con el salto de línea escrito como en el fuente.
    #
    # En la plantilla, `\n` son DOS caracteres que el motor de JS resuelve al
    # ejecutar, y el golden capturó el fuente: los guarda tal cual. En el
    # catálogo tienen que ser un salto de verdad —si se guardan como texto, el
    # modal imprime `\n` en pantalla, que es un bug que hubo y está
    # arreglado—. Es el MISMO salto escrito de dos formas, así que se
    # normaliza en vez de listar seis excepciones que esconderían un cambio
    # real en esas frases.
    # Las dos normalizaciones se combinan, porque hay frases que llevan las
    # dos cosas (`{name}\n{timestamp}\nArrastra para mover…`).
    for v in crudos:
        escapado = v.replace("\n", "\\n")
        fuera.add(" ".join(escapado.split()))
        fuera.add(" ".join(re.sub(r"\{\w+\}", " ⟦⟧ ", escapado).split()))
    return fuera


class TestElCastellanoSigueSiendoElMismo(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        cls.vivas = (captura.frases_del_frontend()
                     | captura.frases_del_backend()
                     | _catalogo_es())

    @staticmethod
    def _sin_prefijo(frase: str) -> str:
        """La frase sin su `[Fase C]`, su marker y su dibujo de árbol.

        El bloque 5 dejó los prefijos LITERALES en el código —son claves del
        parser del frontend y de la persistencia del log— y mandó al catálogo
        solo la prosa. Así que el golden guarda «[Audit] L2: …» y el catálogo
        «L2: …»: es la misma frase, partida donde tocaba.

        Se usa el MISMO regex que el extractor, no una copia: si el criterio
        de qué es un prefijo cambia, cambia en un sitio.
        """
        import extraer_backend as eb
        m = eb._PREFIJO.match(frase)
        prosa = m.group(5) if m else frase
        # Y el marker de CIERRE: hay líneas de fase que van entre dos `━━━`.
        # El de apertura ya lo quitaba el regex del prefijo; el de cierre se
        # quedaba dentro y esas cuatro frases parecían perdidas.
        prosa = re.sub(r"\s*━+\s*$", "", prosa)
        return " ".join(prosa.split())

    def _comprobar(self, clave: str):
        esperadas = set(self.golden[clave])
        faltan = sorted(f for f in esperadas - self.vivas
                        if f not in EXCEPCIONES
                        and self._sin_prefijo(f) not in self.vivas)
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
