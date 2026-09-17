"""Extrae el castellano que la app produce hoy. Base del golden de i18n.

No es un test: es la herramienta que captura el invariante «el castellano
actual no se toca». Se ejecuta una vez sobre el estado pre-i18n y su salida se
congela en `golden_castellano.json`.

El filtro busca **frases**, no cadenas: exige acento o palabra función
castellana y dos palabras como mínimo, y descarta lo que es código (marcado,
CSS, rutas, interpolaciones). Así el golden no exige que sobreviva un
`display:grid` —que nadie va a traducir— y sí que sobreviva cada frase.
"""
from __future__ import annotations

import ast
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import rutas  # noqa: E402

ACENTO = re.compile(r"[ÁÉÍÓÚÑÜáéíóúñü¿¡]")
FUNCION = re.compile(
    r"\b(el|la|los|las|un|una|unos|unas|de|del|al|y|o|en|con|para|por|que|no|"
    r"se|su|sus|es|son|está|están|hay|ya|si|más|sin|sobre|como|cuando|desde|"
    r"hasta|este|esta|esto|esos|pero|todo|toda|todos|todas|cada|solo|ni|le|"
    r"lo|te|tu|tus|nada|algo|otra|otro|aquí|ahora|antes|después)\b", re.I)
CODIGO = re.compile(
    r"<[a-zA-Z/!]|\$\{|=>|/api/|\bfunction\b|===|!==|"
    r"[:;]\s*[\w.#-]+\s*[;{]|\b(px|rem|vh|vw)\b|"
    r"^[\w.#/-]+$|^https?:")

TRADUCIBLES = {"placeholder", "title", "data-tooltip", "aria-label", "alt"}


def es_frase(s: str) -> bool:
    """¿Es una frase en castellano que un usuario lee?"""
    s = " ".join(s.split())
    if len(s) < 6 or len(s) > 400 or CODIGO.search(s):
        return False
    if len(s.split()) < 2:
        return False
    letras = len(re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]", s))
    if letras < 5 or letras / len(s) < 0.5:
        return False
    return bool(ACENTO.search(s)) or bool(FUNCION.search(s))



def sin_comentarios(src: str) -> str:
    """El fuente con los comentarios sustituidos por espacios.

    Hay que quitarlos ANTES de emparejar los backticks de las plantillas, y no
    después: un backtick dentro de un comentario —`` `default` es la clave que
    trae la app ``— descuadra el emparejado, y a partir de ahí el regex toma
    por plantilla lo que no lo es. Medido sobre el frontend: **278 backticks
    viven en comentarios**, y en `settings.js` eso creaba tres regiones
    fantasma de hasta 5.243 caracteres en las que el barrido de cadenas
    sueltas estaba CIEGO — ahí sobrevivió toda la familia de badges y
    placeholders castellanos de ⚙︎ Configuración.

    Se sustituye por espacios en vez de recortar para que las posiciones no se
    muevan: los llamadores las usan para dar el número de línea.

    No vale un regex: un `//` dentro de `'https://…'` no es un comentario, así
    que hay que saber si estamos dentro de una cadena. Es un autómata mínimo,
    sin pretensión de parsear JavaScript.
    """
    fuera = []
    i, n = 0, len(src)
    comilla = None          # ' " ` cuando estamos dentro de una cadena
    while i < n:
        c = src[i]
        if comilla:
            fuera.append(c)
            if c == "\\" and i + 1 < n:
                fuera.append(src[i + 1]); i += 2; continue
            if c == comilla:
                comilla = None
            i += 1
            continue
        if c in "\"'`":
            comilla = c
            fuera.append(c); i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j == -1 else j
            fuera.append(" " * (j - i)); i = j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            # Los saltos de línea se conservan: si no, los números de línea
            # que el llamador calcula contando `\n` se irían al garete.
            fuera.append("".join("\n" if ch == "\n" else " " for ch in src[i:j]))
            i = j
            continue
        fuera.append(c); i += 1
    return "".join(fuera)



def regiones_de_plantilla(src: str) -> list[tuple[int, int, str]]:
    """`(inicio, fin, contenido)` de cada plantilla `` `…` `` de nivel superior.

    Un regex no sirve, y no por un detalle: **empareja los backticks planos**,
    así que un backtick dentro de un comentario o dentro de un `${…}` anidado
    desplaza todas las parejas siguientes y el resultado son regiones que no
    existen. Medido en `settings.js`: tres regiones fantasma de hasta 5.243
    caracteres, y dentro de ellas el barrido de cadenas sueltas estaba CIEGO —
    ahí sobrevivió toda la familia de badges y placeholders castellanos de ⚙︎
    Configuración, que se veían en una captura del modal en catalán.

    Este autómata lleva la cuenta de `${` y de las comillas, así que una
    plantilla anidada se queda DENTRO de la de fuera, que es lo que hace falta
    para excluir la región entera.
    """
    fuera: list[tuple[int, int, str]] = []
    i, n = 0, len(src)
    # Un `/` abre una expresión regular solo donde cabe una expresión; si no,
    # es una división. La heurística de siempre: se mira el último carácter
    # significativo. Sin esto, `s.replace(/`([^`]+)`/g, …)` —que existe en
    # `settings.js`— mete DOS backticks en juego y desplaza todas las parejas
    # siguientes.
    ANTES_DE_REGEX = set("(,=:[!&|?{};+-*%~^") | {"\n"}
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            # una cadena normal: se salta entera, un backtick de dentro no abre
            q, i = c, i + 1
            while i < n and src[i] != q:
                i += 2 if src[i] == "\\" else 1
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if c == "/":
            previo = src[:i].rstrip()
            if not previo or previo[-1] in ANTES_DE_REGEX or previo.endswith("return"):
                # expresión regular: se consume entera, con sus clases `[...]`
                j, clase = i + 1, False
                while j < n:
                    d = src[j]
                    if d == "\\":
                        j += 2
                        continue
                    if d == "[":
                        clase = True
                    elif d == "]":
                        clase = False
                    elif d == "/" and not clase:
                        break
                    elif d == "\n":
                        break      # una regex no cruza la línea: era división
                    j += 1
                i = j + 1
                continue
            i += 1
            continue
        if c != "`":
            i += 1
            continue
        # aquí abre una plantilla: hasta su backtick de cierre, contando los
        # `${…}` y las plantillas que vivan dentro
        ini, i, prof = i, i + 1, 0
        while i < n:
            d = src[i]
            if d == "\\":
                i += 2
                continue
            if prof == 0 and d == "`":
                break
            if d == "$" and i + 1 < n and src[i + 1] == "{":
                prof += 1
                i += 2
                continue
            if prof and d == "}":
                prof -= 1
                i += 1
                continue
            if prof and d == "`":
                # plantilla anidada: se consume entera
                i += 1
                while i < n and src[i] != "`":
                    i += 2 if src[i] == "\\" else 1
            i += 1
        fuera.append((ini, min(i + 1, n), src[ini + 1:i]))
        i += 1
    return fuera




def huecos_de(txt: str) -> list[tuple[int, int]]:
    """Los tramos `${…}` de una plantilla, contando llaves."""
    fuera, i, n = [], 0, len(txt)
    while i < n:
        if txt.startswith("${", i):
            nivel, j = 0, i + 1
            while j < n:
                if txt[j] == "{":
                    nivel += 1
                elif txt[j] == "}":
                    nivel -= 1
                    if nivel == 0:
                        break
                j += 1
            fuera.append((i, min(j + 1, n)))
            i = j + 1
            continue
        i += 1
    return fuera


def sin_huecos(txt: str) -> str:
    """El contenido de una plantilla con cada `${…}` cambiado por el centinela.

    Se cuentan las LLAVES, no se usa un regex: `\\$\\{[^}]*\\}` se corta en la
    primera llave, y dentro de un `${…}` hay ternarias con objetos y llamadas.
    Con el corte a medias, lo que queda detrás —`')" data-tooltip="…`— entra en
    el parser de HTML como si fuera texto y el golden acaba guardando frases
    que no existen, mientras pierde las de verdad.

    Es el MISMO error que emparejar backticks con un regex, y ya se había
    resuelto en `extraer_literales._regiones_interpoladas`; aquí faltaba.
    """
    fuera, i, n = [], 0, len(txt)
    while i < n:
        if txt.startswith("${", i):
            nivel, j = 0, i + 1
            while j < n:
                if txt[j] == "{":
                    nivel += 1
                elif txt[j] == "}":
                    nivel -= 1
                    if nivel == 0:
                        break
                j += 1
            fuera.append(" ⟦⟧ ")
            i = j + 1
            continue
        fuera.append(txt[i])
        i += 1
    return "".join(fuera)


class _Texto(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.encontrado: list[str] = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k in TRADUCIBLES and v:
                self.encontrado.append(v)

    def handle_data(self, d):
        self.encontrado.append(d)


def _del_html(txt: str) -> list[str]:
    p = _Texto()
    p.feed(txt)
    return [" ".join(x.split()) for x in p.encontrado]


def frases_del_frontend() -> set[str]:
    """Marcado estático, plantillas y cadenas sueltas de los ocho scripts."""
    fuera: set[str] = set()
    fuera.update(x for x in _del_html((APP_DIR / "static" / "index.html")
                                      .read_text(encoding="utf-8")) if es_frase(x))
    for r in rutas():
        src = Path(r).read_text(encoding="utf-8")
        # Las plantillas se tratan como HTML, y sus regiones se EXCLUYEN del
        # barrido de cadenas sueltas. Sin excluirlas, el patrón de cadenas JS
        # muerde dentro de la plantilla: un `<em>"CMv4.0 arregla el
        # grading"</em>` del manual salía dos veces —con comillas como nodo de
        # texto y sin ellas como si fuera una cadena de JavaScript— y la
        # segunda es un artefacto que no existe en ninguna parte.
        regiones = []

        def cosechar(contenido: str) -> None:
            """El HTML de una plantilla, y el de las que lleve dentro.

            Enmascarar el `${…}` se lleva por delante las plantillas ANIDADAS,
            y ahí vive texto de verdad:

                ${a.cancelable ? `<button … data-tooltip="Detener este
                trabajo">Cancelar</button>` : ''}

            Trece frases del golden salían de sitios así —los tooltips de la
            columna de trabajo, el de «Vuelve a esta fase», el de la posición
            original de la pista—. Con el emparejado roto se encontraban por
            accidente, como cadenas de JavaScript; ahora se buscan donde
            están.
            """
            fuera.update(x for x in _del_html(sin_huecos(contenido))
                         if es_frase(x))
            for _, _, dentro in regiones_de_plantilla(contenido):
                cosechar(dentro)
            # Y las CADENAS que viven dentro de un `${…}`: los tres tooltips
            # del auto-pipeline son `return 'Auto-ejecuta…'` dentro de una
            # función que la plantilla invoca, así que enmascarar el hueco se
            # los llevaba. Los valores de atributo que este barrido pilla de
            # paso no son frases y los descarta `es_frase`.
            # SOLO dentro de los `${…}`. Barrer la plantilla entera vuelve
            # a meter el artefacto que el comentario de arriba describe: un
            # `<em>"CMv4.0 arregla el grading"</em>` sale dos veces, con
            # comillas como nodo de texto y sin ellas como si fuera una
            # cadena de JavaScript, y la segunda no existe en ninguna parte.
            for a, b in huecos_de(contenido):
                for m in re.finditer(
                        r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"",
                        contenido[a:b]):
                    t = " ".join((m.group(1) or m.group(2) or "").split())
                    if "<" in t:
                        fuera.update(x for x in _del_html(sin_huecos(t))
                                     if es_frase(x))
                    elif es_frase(t):
                        fuera.add(t)

        for ini, fin, contenido in regiones_de_plantilla(src):
            regiones.append((ini, fin))
            cosechar(contenido)
        # Los comentarios también se excluyen: un `/** … "cambios sin
        # guardar" … */` se colaba como si fuera una cadena de JavaScript, y
        # el golden acababa exigiendo que sobreviviera una frase que solo
        # existía dentro de un comentario.
        comentarios = [(c.start(), c.end()) for c in
                       re.finditer(r"/\*.*?\*/|//[^\n]*", src, re.S)]
        for m in re.finditer(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"", src):
            if any(a <= m.start() < b for a, b in regiones + comentarios):
                continue
            s = " ".join((m.group(1) or m.group(2) or "").split())
            # HTML es HTML aunque vaya entre comillas normales. Cinco frases
            # del golden viven en cadenas así —el vacío del panel de
            # Limpieza, el aviso de «Reciente o potencialmente activo», los
            # tres tooltips del auto-pipeline— y `es_frase` las descarta por
            # llevar `<`. Se parsean igual que una plantilla.
            if "<" in s:
                fuera.update(x for x in _del_html(sin_huecos(s)) if es_frase(x))
            elif es_frase(s):
                fuera.add(s)
    return fuera


# Las posiciones del backend cuyo texto ACABA EN PANTALLA. Todo lo demás
# —empezando por los docstrings, que son prosa castellana igualmente— no lo ve
# ningún usuario: meterlos en el golden exigiría que la documentación interna
# no cambie nunca.
# ── El criterio que ve los RÓTULOS ────────────────────────────────────
#
# `es_frase` acaba en «un acento O una palabra función», y eso es correcto para
# prosa y **ciego para rótulos**: «Analizando candidato» son dos palabras sin
# acento y sin función, así que no la ve. Fue el agujero por el que el usuario
# leyó «Analizando candidato 6/18: 00055.mpls» con la app en inglés.
#
# El criterio que sí los caza no es otro umbral sino un dato: **una palabra de
# ≥4 letras que está en los valores castellanos del catálogo y NO está en los
# ingleses es castellano**. Se afina solo con cada frase que se traduce.
#
# `es_frase` NO se toca: hay un test que exige que cada entrada del golden la
# pase, y el golden se capturó con ella.
# Como `CODIGO` pero SIN la regla del token único, que se comía los rótulos
# de una palabra.
_CODIGO_ROTULO = re.compile(
    r"<[a-zA-Z/!]|\$\{|=>|/api/|\bfunction\b|===|!==|"
    r"[:;]\s*[\w.#-]+\s*[;{]|\b(px|rem|vh|vw)\b|^https?:")

_VOCABULARIO: set[str] | None = None


def vocabulario_solo_castellano() -> set[str]:
    """Palabras de ≥4 letras que el catálogo `es` usa y el `en` no."""
    global _VOCABULARIO
    if _VOCABULARIO is None:
        import json
        es: set[str] = set()
        en: set[str] = set()
        for d in (APP_DIR / "i18n", APP_DIR / "static" / "i18n"):
            for idioma, acc in (("es", es), ("en", en)):
                f = d / f"{idioma}.json"
                if not f.exists():
                    continue
                for v in json.loads(f.read_text(encoding="utf-8")).values():
                    acc |= set(re.findall(r"[a-záéíóúñü]{4,}", v.lower()))
        _VOCABULARIO = es - en
    return _VOCABULARIO


def es_rotulo(s: str) -> bool:
    """¿Es un rótulo castellano, aunque `es_frase` no lo vea?

    Deja fuera lo que tiene forma de identificador —`serie`,
    `copia_biblioteca`— porque el `detalle` de un trabajo es el
    discriminador de qué vista pintar, no texto. Un rótulo de verdad trae
    espacios o mayúscula.
    """
    t = " ".join(s.split())
    if len(t) < 4 or len(t) > 400 or _CODIGO_ROTULO.search(t):
        return False
    # Un identificador en minúsculas es un slug, no texto: `serie`,
    # `copia_biblioteca`. Un rótulo de verdad trae mayúscula o espacios, y por
    # eso «Completado» —que es una etiqueta de fase y se ve— sí pasa. Este es
    # el motivo de no reusar `CODIGO`: su regla `^[\w.#/-]+$` descarta
    # cualquier token único, y ahí se escondían los rótulos de una palabra.
    # Los puntos entran porque una CLAVE del catálogo tiene esa forma
    # (`cmv40_strategy.res_c_sin_artefactos`) y su slug está en castellano:
    # sin ellos, la clave que se le pasa a `tr()` se denuncia como el rótulo
    # que acaba de sustituir.
    if re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*", t):
        return False
    return bool(set(re.findall(r"[a-záéíóúñü]{4,}", t.lower()))
                & vocabulario_solo_castellano())






# Tablas `id → rótulo` que NO son interfaz, exentas por su función y no por
# su forma. `LANGUAGE_MAP` son los literales de pista de la spec
# (`spanish: 'Castellano'`) y acaban en el nombre de las pistas del MKV, no
# en la pantalla; que sigan el idioma de la app es una decisión distinta y va
# con el bloque de selección de pistas, que está pendiente.
_TABLAS_EXENTAS = {"LANGUAGE_MAP", "ISO639_TO_ENGLISH", "MKVMERGE_CODEC_TO_BDINFO"}



def _nodos_de_dev_mode(arbol: ast.AST) -> set[int]:
    """Los ids de los nodos que cuelgan de un `if DEV_MODE:`."""
    fuera: set[int] = set()
    for n in ast.walk(arbol):
        if not isinstance(n, ast.If):
            continue
        nombres = {x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)}
        if "DEV_MODE" not in nombres:
            continue
        for rama in n.body:
            for h in ast.walk(rama):
                fuera.add(id(h))
    return fuera


def _texto_de(nodo) -> str:
    """El literal de un `str` o la parte fija de un f-string, con centinela."""
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return nodo.value
    if isinstance(nodo, ast.JoinedStr):
        partes = []
        for v in nodo.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                partes.append(v.value)
            else:
                partes.append(" ⟦⟧ ")
        return "".join(partes)
    return ""




# Métodos de logging: lo que escriben lo lee quien mira `docker logs`, no un
# usuario de la app. Se eximen con TODOS sus argumentos.
_LOGGING = {"info", "warning", "error", "debug", "exception", "critical"}


def _exentos_del_modulo(arbol: ast.AST) -> set[int]:
    """Los ids de nodo que NO son texto de interfaz, por FUNCIÓN.

    Cinco clases, y cada una está aquí porque eximirla es correcto, no
    porque fuera incómoda:

      * **docstrings**, incluidos los de ATRIBUTO —un literal que es una
        sentencia entera—. Los de `models.py` llevan EJEMPLOS dentro
        («Ej: 'Descartada: idioma French…'»), así que sin esta regla el
        guard denuncia la documentación del esquema: 244 en un fichero.
      * **la clave que se le pasa a `tr()`**: su slug va en castellano y no
        es texto, es la clave que acabas de escribir.
      * **`logger.info/warning/…`**, que sale por `docker logs`.
      * **`Field(...)`** de Pydantic, que documenta el esquema.
      * **las tablas de traducción de la spec** (`LANGUAGE_MAP` y
        compañía): sus valores acaban en el NOMBRE de las pistas del MKV,
        no en la pantalla. Ojo, están ANOTADAS (`dict[str, str] = {...}`),
        así que hay que mirar `AnnAssign` además de `Assign`.
      * y lo que cuelga de un **`if DEV_MODE:`**, que es maqueta.
    """
    fuera: set[int] = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
            fuera.add(id(n.value))
        if isinstance(n, ast.Call):
            f = n.func
            nombre = (f.id if isinstance(f, ast.Name)
                      else f.attr if isinstance(f, ast.Attribute) else "")
            if nombre in ("tr", "t") and n.args:
                for h in ast.walk(n.args[0]):
                    fuera.add(id(h))
            if isinstance(f, ast.Attribute) and f.attr in _LOGGING:
                for h in ast.walk(n):
                    fuera.add(id(h))
            if isinstance(f, ast.Name) and f.id == "Field":
                for h in ast.walk(n):
                    fuera.add(id(h))
            # El patrón de un `re.compile` no es texto: se casa contra un
            # dato. Los del sheet de DoviTools están además en INGLÉS
            # (`no bd yet`, `shots are different`) porque la hoja lo está.
            if (isinstance(f, ast.Attribute) and f.attr == "compile"
                    and isinstance(f.value, ast.Name) and f.value.id == "re"):
                for h in ast.walk(n):
                    fuera.add(id(h))
            # La documentación de la API: el `summary=` de un decorador de
            # ruta y la `description=` de `FastAPI(...)` salen en la página
            # `/docs`, que la lee quien integra contra la API. No es interfaz
            # —y encima se evalúa al importar, así que traducirla congelaría
            # el idioma del arranque—.
            if _ES_DECORADOR_DE_RUTA(f) or (isinstance(f, ast.Name)
                                            and f.id == "FastAPI"):
                for k in n.keywords:
                    if k.arg in ("summary", "description",
                                 "response_description"):
                        for h in ast.walk(k.value):
                            fuera.add(id(h))
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            objetivos = (n.targets if isinstance(n, ast.Assign) else [n.target])
            if (n.value is not None
                    and any(isinstance(t, ast.Name) and t.id in _TABLAS_EXENTAS
                            for t in objetivos)):
                for h in ast.walk(n.value):
                    fuera.add(id(h))
    # Y los envoltorios LOCALES del logger. `models.py` define un
    # `def avisa(que, arreglo): _logger.warning(…)` para sus diez avisos de
    # invariante, y eximir `logger.*` no los alcanza porque los argumentos
    # los recibe `avisa`. Se derivan —una función cuyo cuerpo es una sola
    # llamada a un logger— en vez de escribir una lista de nombres, que es
    # el error que este subsistema ya ha cometido cuatro veces.
    for nombre in _envoltorios_de_logger(arbol):
        for n in ast.walk(arbol):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == nombre):
                for h in ast.walk(n):
                    fuera.add(id(h))
    fuera |= _nodos_de_dev_mode(arbol)
    return fuera


def _ES_DECORADOR_DE_RUTA(f) -> bool:
    """`@app.get(...)`, `@router.post(...)` y compañía."""
    return (isinstance(f, ast.Attribute)
            and f.attr in ("get", "post", "put", "patch", "delete", "head",
                           "options", "websocket", "api_route")
            and isinstance(f.value, ast.Name)
            and f.value.id in ("app", "router"))


def _envoltorios_de_logger(arbol: ast.AST) -> set[str]:
    """Funciones cuyo cuerpo es UNA sola llamada a un logger."""
    out: set[str] = set()
    for n in ast.walk(arbol):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        cuerpo = [x for x in n.body
                  if not (isinstance(x, ast.Expr)
                          and isinstance(x.value, ast.Constant))]
        if len(cuerpo) != 1 or not isinstance(cuerpo[0], ast.Expr):
            continue
        c = cuerpo[0].value
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr in _LOGGING):
            out.add(n.name)
    return out


def frases_del_backend() -> set[str]:
    """Lo que el usuario ve del servidor: log, errores y etiquetas de paso.

    **La polaridad es «castellano = fuga salvo exención»**, y ese es el
    punto. Durante cuatro auditorías fue la contraria —lo era solo si
    aparecía en un sitio de una lista blanca— y el recuento fue 0 → 51 →
    79 → 92 → 114 → 190 → 680: cada vez se amplió la lista por donde se
    acababa de romper y cada vez seguía sin ver el sitio que nadie había
    apuntado. El último fue el más caro: `_LOG` eran SEIS nombres escritos
    a mano, así que `_cmv40_log`, `_mkv_quality_log`, `_paso`, `_emit` y
    `log_cb` —el log que el usuario lee en el overlay de Tab 3— nunca
    estuvieron dentro.

    Así que ya no hay lista de sitios. Se recorre TODO literal del
    servidor y se juzga por su contenido; lo que no es interfaz se exime
    por FUNCIÓN en `_exentos_del_modulo`, que es una lista cerrada y
    revisable con el motivo de cada clase escrito.
    """
    fuera: set[str] = set()
    for f in sorted(APP_DIR.rglob("*.py")):
        if "tests" in f.parts or "__pycache__" in str(f):
            continue
        # El propio motor de traducción no es texto de usuario: su único
        # literal es un `ValueError` que se captura ahí dentro para poder
        # caer al castellano cuando un catálogo está roto.
        #
        # `dev_fixtures.py` son los datos falsos de `DEV_MODE=1` —pistas
        # «Inglés TrueHD Atmos 7.1», capítulos «Capítulo 03»— que existen
        # para maquetar la UI sin discos delante, y `tools/` son CLI
        # standalone de depuración: su `argparse` lo lee quien ejecuta el
        # script a mano.
        if f.name in ("i18n.py", "dev_fixtures.py") or "tools" in f.parts:
            continue
        try:
            arbol = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        exentos = _exentos_del_modulo(arbol)
        for n in ast.walk(arbol):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
                continue
            if id(n) in exentos:
                continue
            s = " ".join(n.value.split())
            if es_frase(s) or es_rotulo(s):
                fuera.add(s)
    return fuera


if __name__ == "__main__":
    import json
    front = frases_del_frontend()
    back = frases_del_backend()
    datos = {
        "frontend": sorted(front),
        "backend": sorted(back),
    }
    salida = APP_DIR / "tests" / "golden_castellano.json"
    salida.write_text(json.dumps(datos, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8")
    print(f"frontend: {len(front)} frases")
    print(f"backend : {len(back)} frases")
    print(f"→ {salida.relative_to(APP_DIR.parent)}")
