"""Saca a catálogo el texto que el SERVIDOR le escribe al usuario.

No es un test: es la herramienta del bloque 5 del refactor de i18n.

## Qué entra y qué no

Solo las posiciones cuyo texto acaba en pantalla: las líneas del log, los
`detail` de las `HTTPException`, los `RuntimeError` que llegan a un banner,
las etiquetas de `_emit_progress` y el `que` con el que un trabajo se anuncia.
Los docstrings y los comentarios NO: son ~2.000 frases castellanas que no ve
ningún usuario.

## Los markers se quedan fuera de la cadena traducible

`━━━`, `✓ Fase`, `📋 Plan`, `🎯 Resultado`, `§§PROGRESS§§`, `[Fase C]`,
`[Pipeline]`… son claves del parser del frontend y de
`_CMV40_LOG_FORCE_PERSIST_MARKERS`. Si entran en el catálogo, una traducción
puede cambiarlos y entonces el parser deja de reconocer la fase y el log deja
de persistirse — **sin dar ningún error**. Así que el prefijo se queda literal
en la f-string y solo se traduce lo que va detrás:

    await _log(log, f"[Fase C] {t('cmv40.fase_c.demux', bl=bl)}")

## Por qué esto es más seguro de lo que parece

El castellano no se toca, así que con el idioma en `es` esa f-string renderiza
**exactamente** la cadena de antes. Las ~690 afirmaciones de la suite sobre
texto español siguen pasando sin tocarlas: son la red que valida el bloque, no
su víctima. Una clave que falte devuelve la clave y rompe la afirmación con el
motivo delante.
"""
from __future__ import annotations

import ast
import re
import sys
import unicodedata
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

# Llamadas cuyo texto ve el usuario, y en qué argumentos buscarlo.
LOG = {"log", "_log", "log_callback", "_emit_progress", "emit"}
EXC = {"HTTPException", "RuntimeError", "ValueError", "MkvmergePlaylistError"}
KW = {"detail", "que", "label", "message", "mensaje", "step_label"}

# Lo que NO puede entrar en el catálogo: se queda literal en la f-string.
MARKERS = (
    "§§PROGRESS§§", "━━━", "✓ Fase", "✗ Fase", "📋 Plan", "🎯 Resultado",
    "🛑 Cancelado", "ℹ️ Auto", "ℹ️ Forward", "Progress:",
)
_PREFIJO = re.compile(
    r"^(\s*)"                       # espacio inicial
    r"((?:\[[^\]]{1,26}\]\s*)?)"     # [Fase C], [Pipeline], [Pre-flight]…
    r"((?:(?:" + "|".join(re.escape(m) for m in MARKERS) + r")\s*)*)"
    r"((?:[┌└├│─]+\s*)?)"            # dibujo de árbol del log
    r"(.*)$", re.S)


def slug(txt: str, largo: int = 6) -> str:
    t = unicodedata.normalize("NFKD", txt)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\{[^}]*\}", " ", t)
    t = re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_").lower()
    return "_".join([p for p in t.split("_") if p][:largo]) or "x"


def _es_prosa(txt: str) -> bool:
    """¿Hay algo que traducir? Cuatro letras y alguna palabra de verdad."""
    t = " ".join(txt.split())
    if len(re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]", t)) < 4:
        return False
    # Una ruta, un comando o un nombre de fichero no se traduce.
    resto = re.sub(r"(https?://\S+|/[\w./-]{2,}|[\w./-]+\.\w{2,5}\b|\$ .*)", " ", t)
    return bool(re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{3}", resto))


def _nombre_param(expr: str, usados: set[str], n: int) -> str:
    m = re.search(r"([A-Za-z_][\w]*)\s*$", re.sub(r"[)\]\s]+$", "", expr))
    base = ""
    if m and len(expr) < 70 and not re.search(r"[?]|\bif\b", expr):
        base = re.sub(r"[^a-z0-9_]", "", m.group(1).lower())
    if not base or base in ("name", "str", "len", "int", "float"):
        base = f"p{n}"
    k, i = base, 2
    while k in usados:
        k, i = f"{base}{i}", i + 1
    usados.add(k)
    return k


def _texto_y_huecos(nodo: ast.AST, fuente: str) -> tuple[str, list[str]] | None:
    """(plantilla con `{nombre}`, lista de `nombre: expr`) o None."""
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return nodo.value, []
    if not isinstance(nodo, ast.JoinedStr):
        return None
    usados: set[str] = set()
    partes, args, n = [], [], 0
    for v in nodo.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            partes.append(v.value)
        elif isinstance(v, ast.FormattedValue):
            n += 1
            expr = ast.get_source_segment(fuente, v.value) or ""
            if not expr or "\n" in expr:
                return None      # multilínea: se deja en paz
            if v.format_spec is not None or v.conversion not in (-1, None):
                # `{x:.1f}` o `{x!r}`: el formato viaja con la expresión.
                spec = ""
                if v.format_spec is not None:
                    spec = ast.get_source_segment(fuente, v.format_spec) or ""
                    spec = ":" + spec.strip("'\"")
                conv = "!r" if v.conversion == 114 else ""
                expr = f"format({expr}, '{spec.lstrip(':')}')" if spec else expr
                if conv:
                    expr = f"repr({expr})"
            nombre = _nombre_param(expr, usados, n)
            partes.append("{" + nombre + "}")
            args.append(f"{nombre}={expr}")
    return "".join(partes), args


def candidatos(ruta: Path) -> list[dict]:
    """Los sitios de un fichero que hay que reescribir, con sus posiciones."""
    fuente = ruta.read_text(encoding="utf-8")
    try:
        arbol = ast.parse(fuente)
    except SyntaxError:
        return []
    fuera = []
    for n in ast.walk(arbol):
        if not isinstance(n, ast.Call):
            continue
        nombre = (n.func.id if isinstance(n.func, ast.Name)
                  else n.func.attr if isinstance(n.func, ast.Attribute) else "")
        objetivos = []
        if nombre in LOG:
            objetivos = list(n.args[:3])
        elif nombre in EXC:
            objetivos = list(n.args[:1])
        objetivos += [k.value for k in n.keywords if k.arg in KW]
        for arg in objetivos:
            r = _texto_y_huecos(arg, fuente)
            if r is None:
                continue
            crudo, args = r
            m = _PREFIJO.match(crudo)
            prefijo = m.group(1) + m.group(2) + m.group(3) + m.group(4)
            prosa = m.group(5)
            if not _es_prosa(prosa):
                continue
            seg = ast.get_source_segment(fuente, arg)
            if seg is None:
                continue
            fuera.append({
                "prefijo": prefijo, "prosa": prosa, "args": args,
                "lineno": arg.lineno, "col": arg.col_offset,
                "end_lineno": arg.end_lineno, "end_col": arg.end_col_offset,
                "segmento": seg,
            })
    return fuera


def _expresion(prefijo: str, clave: str, args: list[str]) -> str:
    """El código que sustituye al literal.

    El prefijo se queda LITERAL en la f-string; solo la prosa pasa por `t()`.
    Las llaves del prefijo se doblan porque va dentro de una f-string.
    """
    llamada = f"t({clave!r}" + ("".join(", " + a for a in args)) + ")"
    if not prefijo:
        return llamada
    # CONCATENACIÓN y no f-string: en Python 3.10 —la del contenedor— no se
    # puede reusar el mismo tipo de comilla dentro de la expresión de una
    # f-string, y aquí las expresiones vienen del código de la app, con
    # comillas de los dos tipos. Con `+` no hay restricción ninguna, y el
    # prefijo se escribe con `repr()`, que se encarga del entrecomillado.
    return f"{prefijo!r} + {llamada}"


def aplicar(ruta: Path, area: str, catalogo: dict[str, str],
            vetados: set[str] | None = None) -> tuple[str, dict, int, list[str]]:
    """Reescribe un fichero. Devuelve (fuente, catálogo, nº, claves usadas)."""
    vetados = vetados or set()
    fuente = ruta.read_text(encoding="utf-8")
    usadas = {v: k for k, v in catalogo.items()}
    sitios = candidatos(ruta)
    if not sitios:
        return fuente, catalogo, 0, []

    # **En BYTES, no en caracteres.** `col_offset` del AST es un
    # desplazamiento en bytes UTF-8, y este fuente está lleno de acentos y
    # emoji: usándolo como índice de caracteres las sustituciones se pisan
    # unas a otras y el fichero sale con trozos de código dentro de una
    # cadena. Costó un rato de diagnóstico y lo delató un «invalid character
    # '📋'».
    crudo = fuente.encode("utf-8")
    base = [0]
    for l in crudo.splitlines(keepends=True):
        base.append(base[-1] + len(l))

    def off(lineno: int, col: int) -> int:
        return base[lineno - 1] + col

    cambios, puestas = [], []
    for s in sitios:
        limpio = " ".join(s["prosa"].split())
        if limpio in usadas:
            clave = usadas[limpio]
        else:
            raiz = f"{area}.{slug(limpio)}"
            clave, i = raiz, 2
            while clave in catalogo:
                clave, i = f"{raiz}_{i}", i + 1
        if clave in vetados:
            continue
        catalogo[clave] = limpio
        usadas[limpio] = clave
        puestas.append(clave)
        cambios.append((off(s["lineno"], s["col"]),
                        off(s["end_lineno"], s["end_col"]),
                        _expresion(s["prefijo"], clave, s["args"])))

    salida_b = crudo
    for a, b, txt in sorted(cambios, key=lambda c: -c[0]):
        salida_b = salida_b[:a] + txt.encode("utf-8") + salida_b[b:]
    salida = salida_b.decode("utf-8")
    # `t` tiene que estar importado. Va al lado de los otros imports de nivel
    # superior; el import es perezoso por dentro (`i18n` no importa nada de la
    # app al cargarse), así que no crea ciclos.
    if "from i18n import t" not in salida and puestas:
        # DESPUÉS de `from __future__`, que tiene que ser la primera sentencia
        # del fichero. `ast.parse` no comprueba esa regla —solo `compile()`—,
        # así que colarse delante pasaba la validación en seco y reventaba al
        # importar: 133 errores en la suite y dos módulos que no cargaban.
        fut = re.search(r"^from __future__ import [^\n]+\n", salida, re.M)
        if fut:
            salida = salida[:fut.end()] + "\nfrom i18n import t\n" + salida[fut.end():]
        else:
            m = re.search(r"^(import |from )", salida, re.M)
            if m:
                salida = salida[:m.start()] + "from i18n import t\n" + salida[m.start():]
    return salida, catalogo, len(puestas), puestas
