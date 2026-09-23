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
import shutil
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
STATIC = APP / "static"
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


def maquinaria_del_modal_de_trabajo() -> str:
    """El estado y TODAS las funciones `_trabajoModal*`, para los arneses.

    Misma historia que `sistema_de_iconos()`: los arneses las enumeraban a
    mano (`_fn('_trabajoModalAbrir')` + `_fn('_trabajoModalRefrescar')` + …)
    y el día que el armazón ganó dos —`_trabajoModalProgramar` y
    `_trabajoModalParar`, al pasar el poller de `setInterval` a encadenado—
    diez tests murieron a la vez con un `ReferenceError`, sin decir nada
    sobre el comportamiento que medían.

    Se DERIVA del fuente: cualquier `function _trabajoModal…` entra sola.
    Un arnés que quiera espiar una la redeclara después, que es lo que ya
    hacían — la última declaración gana.
    """
    js = js_completo()
    estado = "".join(f"let {n} = null;\n" for n in
                     re.findall(r"^let (_trabajoModal\w+) = .*$", js, re.M))
    assert estado, "no se encuentran las variables de estado del modal"
    # Por prefijo `_trabajo` y no `_trabajoModal`: el resumen llama a
    # `_trabajoKvHTML` y la cartela a `_trabajoCartelPinta`. Inyectar la
    # función sin sus ayudantes cambia el `ReferenceError` de sitio.
    trozos = []
    for m in re.finditer(r"^(?:async )?function (_trabajo\w+)\(", js, re.M):
        i = js.rindex("\n", 0, m.start()) + 1
        trozos.append(js[i:js.index("\n}\n", m.start()) + 3])
    assert trozos, "no se encuentra ninguna función del modal de trabajo"
    # Y las tablas que esas funciones leen. Inyectar la función sin su tabla
    # solo mueve el `ReferenceError` una línea más abajo, que es exactamente
    # lo que este helper existe para no tener que ir descubriendo de una en
    # una: `_CMV40_FIN` lo lee el resumen y `_MOTIVO_SIN_LOG` el cuerpo.
    #
    # Van como propiedad de `globalThis` y NO como `const`: un `const`
    # duplicado es un SyntaxError —al revés que una `function`, que
    # simplemente se redeclara— y varios arneses ya declaran el suyo. Así el
    # que lo tenga lo sombrea y el que no, lo hereda.
    for nombre in ("_CMV40_FIN", "_MOTIVO_SIN_LOG"):
        i = js.index(f"const {nombre} = {{")
        cuerpo = js[i + len(f"const {nombre} = "):js.index("\n};\n", i) + 3]
        trozos.insert(0, f"globalThis.{nombre} = {cuerpo};")
    return estado + "\n" + "\n".join(trozos) + "\n"


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
        # Cómo se PINTA una situación del relato. Va aquí y no en cada arnés
        # por lo que dice el docstring: es la misma pieza del sistema visual
        # —usa los mismos nombres de `_ICONOS_ESTADO`— y las tres columnas la
        # llaman, así que enumerarla a mano se rompería en las tres a la vez.
        const("const ICONO_DE_SITUACION = {"),
        const("const ACENTO_DE_SITUACION = {"),
        fn("situacionDe"),
        fn("pinturaDeSituacion"),
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


def catalogo_servidor_es() -> dict:
    """El catálogo castellano del SERVIDOR (`app/i18n/es.json`).

    Vive aquí, junto al del frontend, porque los dos contestan la misma
    pregunta —«¿qué castellano tiene esta clave?»— y cuatro módulos de test
    necesitan el segundo desde que el relato lo resuelve en el servidor. Con
    un cargador por fichero, el día que cambie la ruta se rompen los cuatro.

    **Un test de comportamiento se ancla en la CLAVE**: una frase copiada en
    el test deja de contar nada en cuanto alguien la reescribe, y eso ya ha
    puesto en rojo tests de código que funcionaba.
    """
    import json
    ruta = APP / "i18n" / "es.json"
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
    """Siembra el catálogo castellano igual que lo hace el servidor.

    Dos piezas, y las dos hacen falta:

    · **`window.__I18N` antes de `i18n.js`**, que es el script BLOQUEANTE de
      `/api/i18n/catalogo.js` en producción. Sin él, un `tr()` dentro de una
      CONSTANTE de módulo se evalúa con el catálogo vacío y congela la clave:
      el arnés ve `ui.biblioteca` donde la app ve «Biblioteca», o sea que
      falla señalando a código que funciona.
    · **el stub de `fetch`**, porque en `file://` la petición del catálogo y
      la del manual fallan y toda la interfaz se quedaría en claves — un
      fallo del arnés con pinta de fallo de la app.
    """
    import json
    cat = json.dumps(catalogo_es(), ensure_ascii=False)
    manual = STATIC / "i18n" / "manual" / "es.json"
    man = manual.read_text(encoding="utf-8") if manual.exists() else "{}"
    return (f"<script>window.__I18N = {{idioma: 'es', catalogo: {cat}}};</script>\n"
            "<script>(function(){\n"
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


_TEMPORALES: list = []


def _registrar_temporal(ruta: str) -> None:
    """Apunta un temporal para borrarlo al terminar el proceso de test."""
    import atexit
    if not _TEMPORALES:
        atexit.register(_limpiar_temporales)
    _TEMPORALES.append(ruta)


_JS_EN_DISCO: list = []


def js_en_disco() -> str:
    """Ruta a un temporal con `js_completo()` dentro, para los drivers de node.

    Se escribe UNA vez por proceso: son ~700 KB y el JS no cambia durante la
    suite. Escribirlo por llamada dejaba cientos de megas de temporales sin
    borrar, porque el helper que lo hacía no limpiaba nada (medido: **328 MB**
    en `$TMPDIR`). Va aquí y no en el arnés para que la limpieza sea la misma
    que la de `argv_node`.
    """
    if not _JS_EN_DISCO:
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                        encoding="utf-8")
        f.write(js_completo())
        f.close()
        _registrar_temporal(f.name)
        _JS_EN_DISCO.append(f.name)
    return _JS_EN_DISCO[0]


_MOTOR_EN_DISCO: list = []


def motor_en_disco() -> str:
    """Ruta a un temporal con `motor_i18n()` dentro.

    **No pasarlo por variable de entorno.** El tope de `MAX_ARG_STRLEN`
    (128 KiB) que en Linux limita un argumento limita igual cada cadena del
    entorno, y el motor —que lleva el catálogo castellano dentro— son ya
    **130.819 bytes**: quedaban **242 bytes** de margen, o sea tres o cuatro
    claves nuevas antes de volver a romper CI con el mismo `Argument list too
    long` por el otro canal. Un fichero no tiene ese tope.
    """
    if not _MOTOR_EN_DISCO:
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                        encoding="utf-8")
        f.write(motor_i18n())
        f.close()
        _registrar_temporal(f.name)
        _MOTOR_EN_DISCO.append(f.name)
    return _MOTOR_EN_DISCO[0]


def argv_node(guion: str, *extra: str) -> list:
    """`[node, fichero.js, *extra]` — el guion va en un FICHERO, no en `-e`.

    En Linux un solo argumento no puede pasar de `MAX_ARG_STRLEN`, que son
    **128 KiB**, y `motor_i18n()` ya son 129 KB: cualquier arnés que lo
    prependa a su driver se pasa de largo y node muere con
    `OSError: [Errno 7] Argument list too long`. En macOS el límite es otro,
    así que **el Mac pasa y CI no** — es la asimetría que CLAUDE.md documenta,
    y aquí le tocó a dieciséis módulos a la vez.

    **Un fichero NO se evalúa en el ámbito global.** node envuelve un módulo
    en el wrapper de CommonJS, así que un `function X(){}` del guion es local
    del módulo y `globalThis.X = stub` escrito después **ya no lo tapa**: las
    otras funciones insertadas siguen viendo la real. Con `-e` sí quedaba
    tapada, porque ahí todo es global. Un arnés que inserte una función y la
    stubee acto seguido tiene que dejar de insertarla.

    **Los `extra` empiezan en `process.argv[2]`**, no en el 1: con `-e` node no
    inserta ninguna ruta y el primer dato caía en el 1, pero aquí ese hueco lo
    ocupa el fichero del guion. Lo guarda
    `test_frontend_troceado::TestNingunArnesPasaElGuionPorLaLineaDeComandos`.

    El fichero se borra al terminar el proceso de test, no en cada llamada:
    node ya lo ha leído, pero borrarlo antes de que arranque sería una carrera.
    """
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                    encoding="utf-8")
    f.write(guion)
    f.close()
    _registrar_temporal(f.name)
    return [shutil.which("node") or "node", f.name, *extra]


def _limpiar_temporales() -> None:
    import os
    for ruta in _TEMPORALES:
        try:
            os.unlink(ruta)
        except OSError:
            pass


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
    #
    # Y arrastra `idiomaActivo` desde que `localeActual` lee el idioma SEMBRADO
    # en vez de `localStorage`: sin esto el arnés muere con
    # `idiomaActivo is not defined`, que es la cicatriz de siempre — cuando el
    # sistema gana una pieza se rompen todos a la vez.
    locale = "\n".join([
        _trozo("const IDIOMAS = [", "];\n"),
        _trozo("const IDIOMA_POR_DEFECTO", "\n"),
        _trozo("const IDIOMA_PREF", "\n"),
        _trozo("const LOCALES = {", "};\n"),
        "let _idioma = IDIOMA_POR_DEFECTO;",
        _trozo("function idiomaActivo() {", "\n"),
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

