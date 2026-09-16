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


def pieza_de(funcion: str) -> tuple[str, str]:
    """(nombre, fuente) del script que DECLARA esa función.

    Para las pocas aserciones que son por pieza y no sobre el todo: comprobar
    que un patrón no aparece en Tab 1 cuando en Tab 3 es legítimo (`p.subTabId`
    es un campo del proyecto de Tab 3). Leer `tab1.js` por su ruta valdría hoy
    y pasaría en verde vacío el día que la función se mueva a otra pieza —que
    es justo lo que vigila `TestNadieLeeUnaPiezaSuelta`—, así que el ancla es
    la función: si se mueve, el helper la sigue; si desaparece o se duplica,
    falla.
    """
    marca = f"function {funcion}("
    encontradas = [(nombre, ruta.read_text(encoding="utf-8"))
                   for nombre, ruta in zip([n for n, _ in piezas()], rutas())
                   if marca in ruta.read_text(encoding="utf-8")]
    if len(encontradas) != 1:
        raise AssertionError(
            f"`{funcion}` se declara en {len(encontradas)} piezas "
            f"({[n for n, _ in encontradas]}); se esperaba exactamente una")
    return encontradas[0]


def sistema_de_iconos() -> str:
    """El sistema de iconos completo, para los arneses de node.

    Cada arnés listaba a mano las piezas que necesitaba
    (`_fn('_svg')` + `_bloque('const _GLIFOS_TRABAJO = {')` + …), así que cada
    vez que el sistema gana una pieza —el catálogo `GLIFOS`, la función
    `icono`— se rompían todos a la vez con un `ReferenceError`. Pasó cuatro
    veces en la misma sesión.

    Aquí se pide el conjunto y ya está. Si el sistema crece, crece en un sitio.
    """
    js = js_completo()

    def fn(nombre: str) -> str:
        for marca in (f"\nfunction {nombre}(", f"\nasync function {nombre}("):
            i = js.find(marca)
            if i != -1:
                return js[i + 1:js.index("\n}\n", i + 1) + 3]
        raise AssertionError(f"no se encuentra `{nombre}`")

    def const(marca: str) -> str:
        i = js.index(marca)
        fin = (js.index("\n", i) + 1 if marca.rstrip().endswith("=")
               else js.index("\n};\n", i) + 4)
        return js[i:fin]

    return "\n".join([
        fn("_svg"),
        const("const GLIFOS = {"),
        fn("icono"),
        const("const _TONO_POR_TAB = "),
        const("const _GLIFOS_TRABAJO = {"),
        const("const _ICONOS_ESTADO = {"),
        fn("_chipIcono"),
        fn("iconoDeTrabajo"),
        fn("iconoDeEstado"),
    ])


# ── i18n para los arneses ──────────────────────────────────────────────
#
# Mismo problema que resolvió `sistema_de_iconos()`, y la misma solución: cada
# arnés tendría que montarse su propio andamio para las traducciones, y cada
# vez que el sistema cambie se rompen todos a la vez.
#
# Hay DOS formas de arnés y necesitan cosas distintas:
#
#   · los de node renderizan una plantilla a una CADENA y afirman sobre ella.
#     Ahí no hay DOM, así que se resuelven los `data-i18n` en Python con
#     `pintar_textos_es()` — que además comprueba de paso que la clave existe.
#   · los de Chrome cargan `index.html` por `file://`, donde el `fetch` del
#     catálogo falla y `t()` devolvería las claves. `stub_catalogo_es()` da un
#     `<script>` que hay que inyectar ANTES de `i18n.js` para que el fetch
#     conteste lo que contestaría el servidor.

def catalogo_es() -> dict:
    """El catálogo castellano, tal cual lo sirve la app."""
    import json
    ruta = STATIC / "i18n" / "es.json"
    if not ruta.exists():
        return {}
    return json.loads(ruta.read_text(encoding="utf-8"))


def pintar_textos_es(html: str) -> str:
    """Resuelve los `data-i18n*` de una cadena, como haría `pintarTextos()`.

    Una clave que no exista se deja como `⟦clave⟧`, bien visible: así un test
    que afirme sobre el texto falla con el motivo delante en vez de por una
    comparación que no dice nada.
    """
    import re
    cat = catalogo_es()

    def txt(clave: str) -> str:
        return cat.get(clave, f"⟦{clave}⟧")

    # Elemento vacío con `data-i18n`: el texto va dentro.
    html = re.sub(
        r'(<(\w+)([^<>]*?))\s*data-i18n="([^"]+)"([^<>]*?>)\s*(</\2>)',
        lambda m: f"{m.group(1)}{m.group(5)}{txt(m.group(4))}{m.group(6)}", html)
    # Y el que no cierra en la misma cadena (fragmento de plantilla).
    html = re.sub(r'\s*data-i18n="([^"]+)"([^<>]*?)>',
                  lambda m: f"{m.group(2)}>{txt(m.group(1))}", html)
    for attr, destino in (("ph", "placeholder"), ("tip", "data-tooltip"),
                          ("aria", "aria-label"), ("html", None)):
        if destino is None:
            html = re.sub(r'\s*data-i18n-html="([^"]+)"([^<>]*?)>',
                          lambda m: f"{m.group(2)}>{txt(m.group(1))}", html)
        else:
            html = re.sub(rf'data-i18n-{attr}="([^"]+)"',
                          lambda m: f'{destino}="{txt(m.group(1))}"', html)
    return html


def stub_catalogo_es() -> str:
    """`<script>` que hace que el fetch del catálogo funcione en `file://`.

    Se inyecta antes de `i18n.js`. Sin esto, en un Chrome headless sobre
    `file://` el fetch falla, el catálogo queda vacío y toda la interfaz
    muestra claves en vez de texto — un fallo del arnés que parece un fallo de
    la app.
    """
    import json
    cat = json.dumps(catalogo_es(), ensure_ascii=False)
    manual = STATIC / "i18n" / "manual" / "es.json"
    man = manual.read_text(encoding="utf-8") if manual.exists() else "{}"
    return ("<script>(function(){\n"
            f"const _cat = {cat};\nconst _man = {man};\n"
            "const _real = window.fetch;\n"
            "window.fetch = function (u, o) {\n"
            "  const s = String(u);\n"
            "  if (s.includes('/i18n/manual/'))\n"
            "    return Promise.resolve({ok: true, json: () => Promise.resolve(_man)});\n"
            "  if (s.includes('/i18n/'))\n"
            "    return Promise.resolve({ok: true, json: () => Promise.resolve(_cat)});\n"
            "  return _real ? _real.apply(this, arguments)\n"
            "               : Promise.reject(new Error('sin red en el arnés'));\n"
            "};})();</script>\n")


def semilla_catalogo(idioma: str = "es") -> str:
    """`<script>` que siembra `window.__I18N`, igual que hace el servidor.

    En producción el catálogo llega por `/api/i18n/catalogo.js`, un script
    BLOQUEANTE que se carga antes de `i18n.js` — es lo que impide que un
    `tr()` en una constante de módulo se evalúe con el catálogo vacío y
    congele la clave. El arnés tiene que reproducir ESO y no el fetch, porque
    si no está midiendo otro arranque que el real.

    A diferencia de `stub_catalogo_es`, sirve cualquiera de las tres lenguas,
    que es lo que permite mirar la misma pantalla en inglés y en catalán.
    """
    import json
    cat = (STATIC / "i18n" / f"{idioma}.json").read_text(encoding="utf-8")
    manual = STATIC / "i18n" / "manual" / f"{idioma}.json"
    man = manual.read_text(encoding="utf-8") if manual.exists() else "{}"
    return ("<script>window.__I18N = {idioma: %s, catalogo: %s};\n"
            "(function(){const _man = %s; const _real = window.fetch;\n"
            "window.fetch = function (u) {\n"
            "  if (String(u).includes('/i18n/manual/'))\n"
            "    return Promise.resolve({ok: true, json: () => Promise.resolve(_man)});\n"
            "  return _real ? _real.apply(this, arguments) : new Promise(()=>{});\n"
            "};})();</script>\n" % (json.dumps(idioma), cat, man))


def pintar_en(obj):
    """`pintar_textos_es` recursivo, para arneses que devuelven JSON.

    Varios arneses no devuelven HTML suelto sino un objeto con campos (el
    texto de una tarjeta, la lista de chips, el `textContent` de un nodo).
    Pintar solo el primer nivel dejaría a medias justo los que miran dentro.
    """
    if isinstance(obj, str):
        return pintar_textos_es(obj)
    if isinstance(obj, list):
        return [pintar_en(x) for x in obj]
    if isinstance(obj, dict):
        return {k: pintar_en(v) for k, v in obj.items()}
    return obj


def motor_i18n() -> str:
    """El motor de traducción con el catálogo castellano dentro, para node.

    Los arneses que renderizan una plantilla ahora encuentran llamadas a
    `tr('clave', {param})` dentro: son los mensajes con parámetros, que no se
    pueden resolver con `data-i18n` porque llevan datos. Sin esto el script de
    node muere con «tr is not defined».

    Va aquí y no en cada arnés por lo mismo que `sistema_de_iconos()`: si el
    motor cambia, cambia en un sitio.
    """
    import json
    js = js_completo()

    def _trozo(marca: str, cierre: str) -> str:
        i = js.index(marca)
        return js[i:js.index(cierre, i) + len(cierre)]

    # `localeActual()` hace falta para todo lo que pinta una fecha o un número
    # (`toLocaleDateString(localeActual())`): había 16 `'es-ES'` cableados y al
    # centralizarlos los arneses se quedaron con un `ReferenceError`. Arrastra
    # `idiomaGuardado` y sus tres constantes; sin `localStorage` cae al
    # castellano por su propio try/catch, que es lo que quiere un test.
    locale = "\n".join([
        _trozo("const IDIOMAS = [", "];\n"),
        _trozo("const IDIOMA_POR_DEFECTO", "\n"),
        _trozo("const IDIOMA_PREF", "\n"),
        _trozo("const LOCALES = {", "};\n"),
        _trozo("function idiomaGuardado() {", "\n}\n"),
        _trozo("function localeActual() {", "\n}\n"),
    ])

    i = js.index("function tr(clave, params) {")
    fin = js.index("\n}\n", i) + 3
    # `'use strict'` PRIMERO: al prepender esto, el script del arnés dejaba de
    # ser estricto y una asignación a una propiedad sin setter fallaba en
    # silencio en vez de lanzar. Todas las piezas de la app son estrictas, así
    # que el arnés tiene que serlo también.
    return ("'use strict';\n"
            "const _catalogo = " + json.dumps(catalogo_es(), ensure_ascii=False)
            + ";\nconst _ausentes = new Set();\n" + locale + "\n" + js[i:fin])

