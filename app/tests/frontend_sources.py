"""El JS del frontend, leído como lo lee el navegador. (No es un test.)

`app.js` eran 18.687 líneas en un fichero y ahora son siete scripts clásicos.
Seis módulos de test leían `app.js` entero —para extraer una función por
nombre y evaluarla en node, o para buscar un id en las plantillas— y todos
tendrían que saber los nombres y el orden de las piezas.

**El orden sale de `index.html`, no de una lista aquí.** Son scripts
clásicos: se concatenan en el orden en que el HTML los declara, y ese orden es
el que decide qué ve cada uno (una sentencia top-level que use algo declarado
en un script POSTERIOR ve `undefined`, porque el hoisting es por script). Una
lista hardcodeada en el test se desincronizaría del HTML en el primer cambio y
el test seguiría en verde midiendo otra cosa.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
INDEX = STATIC / "index.html"

# Solo los locales: el `src` de Sortable.js apunta a un CDN.
_SCRIPT_RE = re.compile(r'<script\s+src="/static/([A-Za-z0-9_]+\.js)\?v=([^"]+)"')


def piezas() -> list[tuple[str, str]]:
    """[(nombre, token de cache-bust)] en el orden en que las carga el HTML."""
    return _SCRIPT_RE.findall(INDEX.read_text(encoding="utf-8"))


def rutas() -> list[Path]:
    return [STATIC / nombre for nombre, _ in piezas()]


def js_completo() -> str:
    """Las siete piezas concatenadas: lo que el navegador acaba ejecutando.

    Con un separador marcado, para que un `src.index("function X")` de un test
    no pueda cruzar accidentalmente el borde entre dos piezas.
    """
    return "\n\n// ─── siguiente script ───\n\n".join(
        p.read_text(encoding="utf-8") for p in rutas())


def html() -> str:
    return INDEX.read_text(encoding="utf-8")
