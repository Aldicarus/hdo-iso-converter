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
        for m in re.finditer(r"`((?:[^`\\]|\\.)*)`", src, re.S):
            regiones.append((m.start(), m.end()))
            limpio = re.sub(r"\$\{[^}]*\}", " ⟦⟧ ", m.group(1))
            fuera.update(x for x in _del_html(limpio) if es_frase(x))
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
            if es_frase(s):
                fuera.add(s)
    return fuera


# Las posiciones del backend cuyo texto ACABA EN PANTALLA. Todo lo demás
# —empezando por los docstrings, que son prosa castellana igualmente— no lo ve
# ningún usuario: meterlos en el golden exigiría que la documentación interna
# no cambie nunca.
_LOG = {"log", "_log", "log_callback", "_emit_progress", "emit", "anotar"}
_EXC = {"HTTPException", "RuntimeError", "ValueError", "MkvmergePlaylistError"}


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


def frases_del_backend() -> set[str]:
    """Lo que el usuario ve del servidor: log, errores y etiquetas de paso."""
    fuera: set[str] = set()
    for f in sorted(APP_DIR.rglob("*.py")):
        if "tests" in f.parts or "__pycache__" in str(f):
            continue
        # El propio motor de traducción no es texto de usuario: su único
        # literal es un `ValueError` que se captura ahí dentro para poder caer
        # al castellano cuando un catálogo está roto.
        if f.name == "i18n.py":
            continue
        try:
            arbol = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for n in ast.walk(arbol):
            if not isinstance(n, ast.Call):
                continue
            nombre = (n.func.id if isinstance(n.func, ast.Name)
                      else n.func.attr if isinstance(n.func, ast.Attribute) else "")
            candidatos = []
            if nombre in _LOG:
                candidatos = list(n.args[:3])
            elif nombre in _EXC:
                candidatos = list(n.args[:1]) + [k.value for k in n.keywords
                                                 if k.arg in ("detail", "msg")]
            else:
                # `que=`/`label=`/`message=` de cualquier llamada: es el texto
                # con el que un trabajo se anuncia en la columna y el historial.
                candidatos = [k.value for k in n.keywords
                              if k.arg in ("que", "label", "message", "mensaje",
                                           "step_label", "detalle")]
            for c in candidatos:
                s = " ".join(_texto_de(c).split())
                if es_frase(s):
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
