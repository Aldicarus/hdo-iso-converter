"""Ninguna función del frontend llama a algo que no existe.

El 2026-09-08 un commit quitó `let polling` y `_esperarCopiaEncolada` de Tab 2
y se dejó sus dos usos. En un script `'use strict'` las dos cosas LANZAN —una
llamada a una función inexistente y una asignación a una variable sin
declarar—, pero solo al ejecutar esa línea: «Aplicar cambios» murió en cada
pulsación durante cinco semanas y la suite siguió en verde, porque el test de
carga en Chrome (`test_frontend_troceado`) solo ve lo que corre AL CARGAR.

Este guard lo mira en el fuente, que es donde se ve sin ejecutar nada:

  · toda llamada a una función por su nombre (`_x(…)`, `foo(…)`) tiene que
    tener su declaración en alguna pieza del frontend, o ser del navegador;
  · toda asignación a un nombre suelto (`x = …`, `x += …`) tiene que ser a
    algo declarado con `let`/`var`, un parámetro o un global del navegador.

No es un analizador de ámbitos: una declaración en CUALQUIER sitio del
frontend vale para cualquier uso. Es deliberadamente generoso —un falso
positivo aquí sería ruido en cada commit— y aun así caza la clase entera de
«borré la declaración y me dejé el uso», que es la que pasó.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_referencias_del_frontend -v
"""
import re
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from frontend_sources import rutas  # noqa: E402

IDENT = r"[A-Za-z_$][\w$]*"

#: Lo que el navegador (o el servidor, en un script aparte) pone en el ámbito
#: global y el frontend llama por su nombre.
DEL_NAVEGADOR = {
    "setTimeout", "clearTimeout", "setInterval", "clearInterval", "fetch",
    "requestAnimationFrame", "cancelAnimationFrame", "queueMicrotask",
    "structuredClone", "getComputedStyle", "matchMedia", "parseInt",
    "parseFloat", "isNaN", "isFinite", "encodeURIComponent", "decodeURIComponent",
    "encodeURI", "decodeURI", "atob", "btoa", "Number", "String", "Boolean",
    "Array", "Object", "Date", "Math", "JSON", "Promise", "Set", "Map",
    "WeakMap", "RegExp", "Error", "TypeError", "Symbol", "Intl", "URL",
    "URLSearchParams", "AbortController", "Blob", "FileReader", "Image",
    "WebSocket", "Notification", "AudioContext", "MutationObserver",
    "ResizeObserver", "IntersectionObserver", "CustomEvent", "Event",
    "DOMParser", "TextEncoder", "TextDecoder", "Sortable", "BigInt",
    "alert", "confirm", "prompt", "escape", "unescape",
    # `/api/tema.js` (main.py): el script de tema que se sirve aparte.
    "aplicarTema",
}
PALABRAS = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "function",
    "await", "new", "super", "import", "async", "void", "delete", "in", "of",
    "instanceof", "do", "else", "try", "throw", "case", "yield", "with",
}
GLOBALES_ASIGNABLES = {"window", "document", "location", "globalThis", "self"}


def solo_codigo(src: str) -> str:
    """El fuente con comentarios, cadenas y expresiones regulares en blanco,
    y las plantillas reducidas a sus `${…}`, que SÍ son código. Las
    posiciones se conservan (todo lo quitado pasa a espacios)."""
    out = list(src)
    n = len(src)

    def blanco(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    ANTES_DE_REGEX = set("(,=:[!&|?{};+-*%~^") | {"\n"}

    def codigo(i, fin, cierre_llave=False):
        """Recorre código desde `i`; con `cierre_llave`, para en la `}` que
        cierra el `${` de una plantilla. Devuelve la posición de parada."""
        prof = 0
        while i < fin:
            c = src[i]
            if c in "'\"":
                j = i + 1
                while j < fin and src[j] != c and src[j] != "\n":
                    j += 2 if src[j] == "\\" else 1
                blanco(i + 1, j)
                i = j + 1
                continue
            if c == "/" and i + 1 < fin and src[i + 1] == "/":
                j = src.find("\n", i)
                j = fin if j == -1 else j
                blanco(i, j)
                i = j
                continue
            if c == "/" and i + 1 < fin and src[i + 1] == "*":
                j = src.find("*/", i + 2)
                j = fin if j == -1 else j + 2
                blanco(i, j)
                i = j
                continue
            if c == "/":
                previo = "".join(src[max(0, i - 8):i]).rstrip()
                if not previo or previo[-1] in ANTES_DE_REGEX or previo.endswith("return"):
                    j, clase = i + 1, False
                    while j < fin:
                        d = src[j]
                        if d == "\\":
                            j += 2
                            continue
                        if d == "[":
                            clase = True
                        elif d == "]":
                            clase = False
                        elif (d == "/" and not clase) or d == "\n":
                            break
                        j += 1
                    if j < fin and src[j] == "/":
                        blanco(i + 1, j)
                        i = j + 1
                        continue
                i += 1
                continue
            if c == "`":
                i = plantilla(i + 1, fin)
                continue
            if cierre_llave:
                if c == "{":
                    prof += 1
                elif c == "}":
                    if prof == 0:
                        return i
                    prof -= 1
            i += 1
        return i

    def plantilla(i, fin):
        ini = i
        while i < fin:
            c = src[i]
            if c == "\\":
                i += 2
                continue
            if c == "`":
                blanco(ini, i)
                return i + 1
            if c == "$" and i + 1 < fin and src[i + 1] == "{":
                blanco(ini, i)
                i = codigo(i + 2, fin, cierre_llave=True) + 1
                ini = i
                continue
            i += 1
        blanco(ini, fin)
        return fin

    codigo(0, n)
    return "".join(out)


def _declarados(codigo: str) -> set[str]:
    d = set(re.findall(rf"\bfunction\s*\*?\s*({IDENT})", codigo))
    d |= set(re.findall(rf"\bclass\s+({IDENT})", codigo))
    # `let a = 1, b, [c, d] = …`: cada nombre de la lista, a profundidad cero.
    for m in re.finditer(r"\b(?:const|let|var)\b", codigo):
        i, prof, esperando = m.end(), 0, True
        while i < len(codigo):
            c = codigo[i]
            if c in "([{":
                if prof == 0 and esperando and c in "[{":
                    j = i
                    p2 = 0
                    while j < len(codigo):
                        if codigo[j] in "([{":
                            p2 += 1
                        elif codigo[j] in ")]}":
                            p2 -= 1
                            if p2 == 0:
                                break
                        j += 1
                    d |= set(re.findall(IDENT, codigo[i:j]))
                    esperando = False
                    i = j + 1
                    continue
                prof += 1
            elif c in ")]}":
                if prof == 0:
                    break
                prof -= 1
            elif prof == 0 and c in ";\n" and not esperando:
                # `let a = 1,\n  b = 2` sigue la lista tras el salto
                resto = codigo[i:i + 200].lstrip()
                if not resto.startswith(","):
                    break
            elif prof == 0 and c == ",":
                esperando = True
            elif esperando and re.match(IDENT, codigo[i:]):
                nombre = re.match(IDENT, codigo[i:]).group(0)
                if nombre not in PALABRAS:
                    d.add(nombre)
                esperando = False
                i += len(nombre)
                continue
            i += 1
    # parámetros: todo identificador de la lista (generoso a propósito)
    for params in re.findall(r"function\s*\*?\s*[\w$]*\s*\(([^)]*)\)", codigo):
        d |= set(re.findall(IDENT, params))
    for params in re.findall(r"\(([^()]*)\)\s*=>", codigo):
        d |= set(re.findall(IDENT, params))
    d |= set(re.findall(rf"({IDENT})\s*=>", codigo))
    d |= set(re.findall(rf"\bcatch\s*\(\s*({IDENT})", codigo))
    # métodos abreviados de objetos y clases: `nombre(args) {`
    d |= set(re.findall(rf"(?m)^\s*(?:async\s+)?({IDENT})\s*\([^)]*\)\s*\{{", codigo))
    return d


def _frontend() -> dict[str, str]:
    return {p.name: solo_codigo(p.read_text(encoding="utf-8")) for p in rutas()}


class TestElTokenizador(unittest.TestCase):

    def test_quita_cadenas_comentarios_y_regex_y_deja_los_huecos(self):
        src = ("a('x(y)'); // b(z)\n/* c(w) */ d(`t ${e(1)} ${`${f(2)}`}`)"
               " ; g = s.replace(/h(\\d)/g, '')\n")
        cod = solo_codigo(src)
        self.assertEqual(len(cod), len(src))
        for fuera in ("x(y)", "b(z)", "c(w)", "h(\\d)"):
            self.assertNotIn(fuera, cod)
        for dentro in ("a(", "d(", "e(1)", "f(2)", "g ="):
            self.assertIn(dentro, cod)

    def test_declaraciones_multiples_y_destructuring(self):
        d = _declarados(solo_codigo(
            "let a = 1, b,\n  c = 2;\nconst {x, y: z} = o;\nlet [p, q] = r;\n"
            "function f(m, {n}, k = 3) {}\nconst g = (u, v) => u;\nlet w;\n"))
        self.assertLessEqual({"a", "b", "c", "x", "z", "p", "q", "f", "m", "n", "k",
                              "g", "u", "v", "w"}, d)


class TestNadieLlamaAAlgoQueNoExiste(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.piezas = _frontend()
        cls.declarados = set().union(*(_declarados(c) for c in cls.piezas.values()))

    def test_mira_de_verdad(self):
        """Trinquete: si el tokenizador se rompiera y dejara el código en
        blanco, los otros dos tests pasarían en verde sin mirar nada. Medido
        el 2026-10-17: 6.916 llamadas y 492 asignaciones en nueve piezas."""
        self.assertGreaterEqual(len(self.piezas), 9)
        llamadas = sum(len(re.findall(rf"(?<![\w$.])({IDENT})\s*\(", c))
                       for c in self.piezas.values())
        asignaciones = sum(len(re.findall(rf"(?m)^\s*({IDENT})\s*=(?![=>])", c))
                           for c in self.piezas.values())
        self.assertGreater(llamadas, 5000)
        self.assertGreater(asignaciones, 300)

    def test_cada_funcion_llamada_por_su_nombre_existe(self):
        faltan = []
        for nombre, cod in self.piezas.items():
            for m in re.finditer(rf"(?<![\w$.])({IDENT})\s*\(", cod):
                f = m.group(1)
                if f in PALABRAS or f in DEL_NAVEGADOR or f in self.declarados:
                    continue
                antes = cod[max(0, m.start() - 12):m.start()]
                if re.search(r"\bfunction\s*\*?\s*$", antes):
                    continue
                linea = cod.count("\n", 0, m.start()) + 1
                faltan.append(f"{nombre}:{linea} {f}(")
        self.assertEqual(faltan, [], "se llama a funciones que no se declaran en ninguna pieza")

    def test_cada_asignacion_suelta_es_a_algo_declarado(self):
        faltan = []
        patron = re.compile(rf"(?m)(?:^|[;{{}}(]|\belse\b|=>)\s*({IDENT})\s*"
                            r"(?:=(?![=>])|\+=|-=|\*=|\|\|=|&&=|\?\?=)")
        for nombre, cod in self.piezas.items():
            for m in patron.finditer(cod):
                v = m.group(1)
                if v in self.declarados or v in GLOBALES_ASIGNABLES or v in PALABRAS:
                    continue
                linea = cod.count("\n", 0, m.start(1)) + 1
                faltan.append(f"{nombre}:{linea} {v} =")
        self.assertEqual(faltan, [],
                         "asignaciones a nombres sin declarar: en 'use strict' LANZAN")


class TestElGuardCazaLoQuePaso(unittest.TestCase):
    """Los dos usos que se quedaron colgando el 2026-09-08, sobre una copia
    del fuente de Tab 2: el guard tiene que verlos."""

    def test_el_polling_sin_declarar(self):
        cod = solo_codigo("async function f() {\n  try { await g(); } finally {\n"
                          "    polling = false;\n  }\n}\nfunction g() {}\n")
        decl = _declarados(cod)
        self.assertNotIn("polling", decl)
        self.assertIn("polling", re.findall(rf"(?m)^\s*({IDENT})\s*=", cod))

    def test_la_funcion_que_ya_no_existe(self):
        cod = solo_codigo("async function f() {\n  r = await _esperar();\n}\nlet r;\n")
        self.assertNotIn("_esperar", _declarados(cod))


if __name__ == "__main__":
    unittest.main()
