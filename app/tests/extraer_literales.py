"""Saca las cadenas de interfaz del marcado y las deja en el catálogo.

No es un test: es la herramienta del refactor. Se ejecuta una vez por bloque,
su salida se revisa con `git diff`, y lo que la valida es
`test_castellano_intacto.py` —ninguna frase castellana puede desaparecer— más
la carga en Chrome.

## Dos formas de reescribir, y por qué no una sola

Un texto que es TODO el contenido de su elemento se resuelve poniéndole el
atributo al elemento (`<button data-i18n="k"></button>`): no añade nodos y el
diff es de una línea. Uno que comparte sitio con marcado —el caso típico es un
icono delante— hay que envolverlo (`<span data-i18n="k"></span>`), porque el
atributo del padre sobrescribiría al hermano.

Envolver siempre sería más simple pero mete un elemento donde no hacía falta en
167 de 278 casos, y en `<option>` o `<title>` un `<span>` dentro no es HTML
válido: ahí el atributo del padre es la única vía.

## Las claves

`<area>.<slug del texto>`, derivada del propio texto y no de un contador: un
catálogo con `ui.147` no lo puede mantener nadie, y menos quien traduce. Que la
clave dependa del texto tiene además la propiedad correcta — si el castellano
cambia, la clave cambia, y la traducción vieja deja de aplicarse en vez de
quedarse mintiendo.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

# Atributos cuyo valor lee un humano, con el sufijo de `data-i18n-*` que les
# toca. El resto de atributos no se traduce.
ATRIBUTOS = {
    "placeholder": "ph",
    "data-tooltip": "tip",
    "aria-label": "aria",
    "title": "tip",          # `title` nativo: mismo destino que el tooltip
    "alt": "aria",
}

# Dentro de estos un `<span>` no es HTML válido: el atributo del padre es la
# única opción.
SIN_HIJOS = {"option", "title", "textarea"}

# Lo que no es texto de interfaz aunque esté en un nodo de texto.
_NO_ES_TEXTO = re.compile(r"^[\s\d.,:;•·—–\-→←↔/|()\[\]{}%+*=~×·…]*$")


_EXT = (r"(hevc|json|bin|mkv|iso|m2ts|mpls|log|jsonl|idx|progress|tmp|py|js|"
        r"css|html|txt|xlsx|csv)")
_IDENT = re.compile(
    rf"(https?://\S+|/[\w./…-]{{2,}}|[\w./-]*\.{_EXT}\b|\bv?[Xx]\.[Yy]\.[Zz]\b|"
    rf"([\w-]+\.)+(com|org|net|io|dev)[\w./?=&…-]*)")


def _es_identificador(v: str) -> bool:
    """¿Es una ruta, una URL o un nombre de fichero, y nada más?

    Un identificador no se traduce en ninguna lengua, así que meterlo en el
    catálogo produce una entrada con el mismo valor en las tres.

    **Exige que HAYA un identificador**, no solo que no quede prosa: sin esa
    condición, «o», «y» y «no» pasaban por identificadores y se quedaban sin
    traducir, dejando la frase mezclando idiomas.
    """
    t = v.strip()
    if not _IDENT.search(t):
        return False
    resto = re.sub(r"[\s,;·:/()\[\]{}…—–+*=~%]+", " ", _IDENT.sub(" ", t))
    return not re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{3}", resto)


def _solo_glosario(t: str) -> bool:
    """¿Es únicamente un nombre que no se traduce en ninguna lengua?

    `UHD`, `Blu-ray Toolkit`, `mkvmerge -J`, `DTS-HD MA`… Extraer estos daría
    una clave con el mismo valor en las tres lenguas, o sea una entrada de
    catálogo que no traduce nada y que además haría fallar el test que impide
    «rellenar» una traducción copiando el castellano. Se quedan en el marcado.
    """
    from test_registro_de_la_traduccion import GLOSARIO
    resto = t
    for g in sorted(GLOSARIO, key=len, reverse=True):
        resto = resto.replace(g, " ")
    for extra in ("UHD", "Toolkit", "CMv4.0", "CMv2.9", "DV", "HDR",
                  "FEL", "MEL", "L1", "L8", "P7", "P8", "TMDb", "DoviTools",
                  "Atmos", "PQ", "BL", "EL", "SDR", "AC-3", "DD+", "DTS",
                  "TrueHD", "PCM", "FLAC", "SDH", "AD", "ffprobe", "Plex",
                  "Jellyfin", "Drive", "Sheets", "PayPal", "GitHub", "Docker"):
        resto = resto.replace(extra, " ")
    return not re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñüÜ]{2}", resto)


def _es_traducible(txt: str) -> bool:
    """¿Lo lee un humano y hay algo que traducir? «Cancelar» sí; «UHD» no."""
    t = " ".join(txt.split())
    if len(t) < 2 or _NO_ES_TEXTO.match(t):
        return False
    if len(re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñüÜ]", t)) < 2:
        return False
    return not _solo_glosario(t) and not _es_identificador(t)


def slug(txt: str, largo: int = 6) -> str:
    """Clave legible a partir del texto: sin acentos, minúsculas, con `_`."""
    t = unicodedata.normalize("NFKD", txt)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"<[^>]*>", " ", t)
    t = re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_").lower()
    palabras = [p for p in t.split("_") if p][:largo]
    return "_".join(palabras) or "x"


class _Recolector(HTMLParser):
    """Recorre el marcado guardando POSICIONES, no contenido.

    Trabaja sobre el texto original y las sustituciones se aplican de atrás
    hacia adelante: reconstruir el HTML desde el árbol reformatearía el fichero
    entero y el diff sería inservible para revisarlo.
    """

    def __init__(self, fuente: str):
        super().__init__(convert_charrefs=False)
        self.fuente = fuente
        self.lineas = [0]
        for linea in fuente.splitlines(keepends=True):
            self.lineas.append(self.lineas[-1] + len(linea))
        # (inicio, fin, texto, tag_padre, fin_del_tag_de_apertura, es_unico)
        self.textos: list[dict] = []
        self.atributos: list[dict] = []
        self.pila: list[dict] = []

    def _off(self) -> int:
        fila, col = self.getpos()
        return self.lineas[fila - 1] + col

    def handle_starttag(self, tag, attrs):
        ini = self._off()
        fin = self.fuente.index(">", ini) + 1
        for k, v in attrs:
            if k in ATRIBUTOS and v and _es_traducible(v):
                # La posición del VALOR, para sustituir solo eso.
                # Se guarda el atributo COMPLETO, con su espacio previo: se
                # borra entero y no solo el valor. Un `data-tooltip=""` haría
                # que el gestor de tooltips pinte una caja vacía, y un
                # `aria-label=""` es peor que no tenerlo para un lector de
                # pantalla.
                m = re.search(r'\s*' + re.escape(k) + r'\s*=\s*"[^"]*"',
                              self.fuente[ini:fin])
                if m:
                    self.atributos.append({
                        "ini": ini + m.start(), "fin": ini + m.end(),
                        "texto": v, "attr": k, "tag_ini": ini, "tag_fin": fin,
                    })
        if tag not in ("br", "img", "input", "hr", "meta", "link", "source"):
            self.pila.append({"tag": tag, "tag_fin": fin, "textos": [],
                              "marcado": False})

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.pila and self.pila[-1]["tag"] == tag:
            self.pila.pop()
        if self.pila:
            self.pila[-1]["marcado"] = True

    def handle_endtag(self, tag):
        while self.pila:
            el = self.pila.pop()
            for t in el["textos"]:
                t["tag_padre"] = el["tag"]
                t["tag_fin"] = el["tag_fin"]
                # Único = es todo el contenido del elemento: ni marcado
                # hermano ni otro texto al lado.
                t["unico"] = (not el["marcado"]) and len(el["textos"]) == 1
                self.textos.append(t)
            if el["tag"] == tag:
                break
        if self.pila:
            self.pila[-1]["marcado"] = True

    def handle_data(self, data):
        if not self.pila:
            return
        if self.pila[-1]["tag"] in ("script", "style"):
            return
        if _es_traducible(data):
            ini = self._off()
            self.pila[-1]["textos"].append(
                {"ini": ini, "fin": ini + len(data), "texto": data})
        elif data.strip():
            # Texto que no se traduce (un número, una flecha) pero que ocupa:
            # el elemento deja de ser «solo esta frase».
            self.pila[-1]["marcado"] = True

    def handle_comment(self, data):
        pass

    def close(self):
        """Vuelca lo que quede abierto.

        Los textos solo se registran al CERRAR su elemento, y una plantilla de
        JS es un fragmento: a menudo abre etiquetas que se cierran en otra
        plantilla o en una concatenación. Sin este volcado, un fragmento como
        `<div class="x">Sin resultados` no aporta ni una cadena — que es lo
        que pasó con `browser.js`, 0 de 6.
        """
        super().close()
        while self.pila:
            el = self.pila.pop()
            for t in el["textos"]:
                t["tag_padre"] = el["tag"]
                t["tag_fin"] = el["tag_fin"]
                t["unico"] = (not el["marcado"]) and len(el["textos"]) == 1
                self.textos.append(t)


def extraer(ruta: Path, area: str) -> tuple[str, dict[str, str], dict]:
    """Devuelve (marcado reescrito, claves nuevas, recuento)."""
    fuente = ruta.read_text(encoding="utf-8")
    r = _Recolector(fuente)
    r.feed(fuente)
    r.close()

    catalogo: dict[str, str] = {}
    usadas: dict[str, str] = {}      # slug → texto, para detectar colisiones
    cambios: list[tuple[int, int, str]] = []
    cuenta = {"atributo": 0, "padre": 0, "envuelto": 0}

    def clave_para(txt: str) -> str:
        limpio = " ".join(txt.split())
        base = f"{area}.{slug(limpio)}"
        k = base
        n = 2
        # Misma frase = misma clave (se traduce una vez). Frases distintas con
        # el mismo slug se numeran.
        while k in usadas and usadas[k] != limpio:
            k = f"{base}_{n}"
            n += 1
        usadas[k] = limpio
        catalogo[k] = limpio
        return k

    for a in r.atributos:
        k = clave_para(a["texto"])
        sufijo = ATRIBUTOS[a["attr"]]
        # El valor se vacía y el atributo `data-i18n-*` se añade al tag.
        cambios.append((a["ini"], a["fin"], ""))
        cambios.append((a["tag_fin"] - 1, a["tag_fin"] - 1,
                        f' data-i18n-{sufijo}="{k}"'))
        cuenta["atributo"] += 1

    for t in r.textos:
        k = clave_para(t["texto"])
        # `data-i18n` escribe `textContent`: si el elemento contiene una
        # interpolación, ponerlo en el padre la BORRARÍA.
        if ((t["unico"] or t["tag_padre"] in SIN_HIJOS)
                and "${" not in fuente[t["tag_fin"]:t["fin"] + 40]):
            # El atributo va al padre y el texto se borra: un nodo menos.
            cambios.append((t["ini"], t["fin"], ""))
            cambios.append((t["tag_fin"] - 1, t["tag_fin"] - 1,
                            f' data-i18n="{k}"'))
            cuenta["padre"] += 1
        else:
            # Comparte sitio con marcado: hay que envolverlo. Se conserva el
            # espaciado de alrededor, que en un flex es lo que separa el icono
            # del texto.
            crudo = t["texto"]
            izq = crudo[:len(crudo) - len(crudo.lstrip())]
            der = crudo[len(crudo.rstrip()):]
            cambios.append((t["ini"], t["fin"],
                            f'{izq}<span data-i18n="{k}"></span>{der}'))
            cuenta["envuelto"] += 1

    # De atrás hacia adelante: así ningún reemplazo mueve los offsets de los
    # que quedan por aplicar.
    salida = fuente
    for ini, fin, nuevo in sorted(cambios, key=lambda c: (-c[0], -c[1])):
        salida = salida[:ini] + nuevo + salida[fin:]
    return salida, catalogo, cuenta


# ── El JS: plantillas con HTML dentro, y cadenas sueltas ──────────────
#
# Una plantilla (`` `...` ``) es marcado, así que se le aplica el MISMO
# tratamiento que a `index.html`: `data-i18n` en el elemento, o un `<span>`
# si el texto comparte sitio. El observador lo pinta cuando entra en el DOM,
# igual que los iconos.
#
# Las cadenas sueltas que van a `showToast`, `showConfirm` o a un
# `textContent` no son marcado: ahí hay que sustituir el literal por una
# llamada a `t()`.

def extraer_de_plantillas(ruta: Path, area: str,
                          catalogo: dict[str, str] | None = None
                          ) -> tuple[str, dict[str, str], dict]:
    """Reescribe el HTML de las plantillas de un fichero JS."""
    fuente = ruta.read_text(encoding="utf-8")
    catalogo = {} if catalogo is None else catalogo
    usadas = {v: k for k, v in catalogo.items()}
    cuenta = {"atributo": 0, "padre": 0, "envuelto": 0, "plantillas": 0}
    trozos: list[tuple[int, int, str]] = []

    for m in re.finditer(r"`((?:[^`\\]|\\.)*)`", fuente, re.S):
        plantilla = m.group(1)
        # Sin marcado dentro no es HTML: es una cadena con comillas invertidas
        # y la trata el otro camino.
        if "<" not in plantilla:
            continue
        nuevo, nuevas, c = _reescribir_marcado(plantilla, area, catalogo, usadas)
        if nuevo != plantilla:
            trozos.append((m.start(1), m.end(1), nuevo))
            cuenta["plantillas"] += 1
            for k, v in c.items():
                cuenta[k] = cuenta.get(k, 0) + v

    salida = fuente
    for ini, fin, nuevo in sorted(trozos, key=lambda x: -x[0]):
        salida = salida[:ini] + nuevo + salida[fin:]
    return salida, catalogo, cuenta


def _regiones_interpoladas(txt: str) -> list[tuple[int, int]]:
    r"""Los tramos `${...}` de una plantilla, con llaves anidadas.

    Un regex `\$\{[^}]*\}` no sirve: dentro hay ternarias con objetos y
    llamadas, y se corta en la primera llave. Se cuentan las llaves.
    """
    regiones = []
    i = 0
    while True:
        i = txt.find("${", i)
        if i < 0:
            return regiones
        nivel, j = 0, i + 1
        while j < len(txt):
            if txt[j] == "{":
                nivel += 1
            elif txt[j] == "}":
                nivel -= 1
                if nivel == 0:
                    break
            j += 1
        regiones.append((i, min(j + 1, len(txt))))
        i = j + 1


def _reescribir_marcado(html: str, area: str, catalogo: dict[str, str],
                        usadas: dict[str, str]) -> tuple[str, dict, dict]:
    """El motor común: sobre un trozo de marcado, devuelve el reescrito.

    **Las expresiones `${...}` se enmascaran antes de parsear.** Dentro hay
    JavaScript —ternarias con cadenas, llamadas, fragmentos de HTML— y el
    parser de HTML lo tomaba por texto: reescribirlo metía un `<span>` dentro
    de una expresión y rompía el fichero. Pasó en `tab2.js` y `tab3.js`, y lo
    cazó `node --check`.
    """
    prohibidas = _regiones_interpoladas(html)
    # Relleno del mismo largo para que los offsets sigan valiendo, y con un
    # carácter que el parser trate como texto cualquiera.
    enmascarado = list(html)
    for a, b in prohibidas:
        for k in range(a, b):
            enmascarado[k] = "\x01"
    html_m = "".join(enmascarado)
    r = _Recolector(html_m)
    try:
        r.feed(html)
        r.close()
    except Exception:
        # Una plantilla con marcado a medias (se abre en una y se cierra en
        # otra) no se toca: reescribirla a ciegas produciría HTML roto.
        return html, {}, {}
    cambios: list[tuple[int, int, str]] = []
    cuenta = {"atributo": 0, "padre": 0, "envuelto": 0}

    def fuera_de_expresion(ini: int, fin: int) -> bool:
        return not any(a < fin and ini < b for a, b in prohibidas)

    def clave_para(txt: str) -> str:
        limpio = " ".join(txt.split())
        if limpio in usadas:
            return usadas[limpio]
        base = f"{area}.{slug(limpio)}"
        k, n = base, 2
        while k in catalogo:
            k = f"{base}_{n}"
            n += 1
        catalogo[k] = limpio
        usadas[limpio] = k
        return k

    for a in r.atributos:
        if not fuera_de_expresion(a["tag_ini"], a["tag_fin"]):
            continue
        k = clave_para(a["texto"])
        cambios.append((a["ini"], a["fin"], ""))
        cambios.append((a["tag_fin"] - 1, a["tag_fin"] - 1,
                        f' data-i18n-{ATRIBUTOS[a["attr"]]}="{k}"'))
        cuenta["atributo"] += 1
    for t in r.textos:
        # El texto se lee del ORIGINAL, no del enmascarado.
        t["texto"] = html[t["ini"]:t["fin"]]
        # Un texto con interpolación dentro NO se toca aquí: es un mensaje con
        # parámetros y va por `t()` con nombres, no por `data-i18n`.
        if "${" in t["texto"] or "\x01" in t["texto"]:
            continue
        if not (fuera_de_expresion(t["ini"], t["fin"])
                and fuera_de_expresion(t["tag_fin"] - 1, t["tag_fin"])):
            continue
        k = clave_para(t["texto"])
        # Igual que arriba, y aquí es donde hizo daño: el extractor puso
        # `data-i18n` en el `<div class="log-line">${escHtml(l)}</div>` del
        # visor, que al pintarse se habría quedado en blanco.
        contenido = html[t["tag_fin"]:t["fin"] + 40]
        if ((t["unico"] or t["tag_padre"] in SIN_HIJOS)
                and "${" not in contenido and "\x01" not in contenido):
            cambios.append((t["ini"], t["fin"], ""))
            cambios.append((t["tag_fin"] - 1, t["tag_fin"] - 1,
                            f' data-i18n="{k}"'))
            cuenta["padre"] += 1
        else:
            crudo = t["texto"]
            izq = crudo[:len(crudo) - len(crudo.lstrip())]
            der = crudo[len(crudo.rstrip()):]
            cambios.append((t["ini"], t["fin"],
                            f'{izq}<span data-i18n="{k}"></span>{der}'))
            cuenta["envuelto"] += 1

    salida = html
    for ini, fin, nuevo in sorted(cambios, key=lambda c: (-c[0], -c[1])):
        salida = salida[:ini] + nuevo + salida[fin:]
    return salida, catalogo, cuenta


JS = ("browser.js", "workbar.js", "settings.js", "core.js",
      "cmv40_modals.js", "tab1.js", "tab2.js", "tab3.js")


def main() -> None:
    aplicar = "--aplicar" in sys.argv
    solo_js = "--js" in sys.argv
    cat_path = APP_DIR / "static" / "i18n" / "es.json"
    catalogo = json.loads(cat_path.read_text(encoding="utf-8"))
    antes = len(catalogo)

    if not solo_js:
        destino = APP_DIR / "static" / "index.html"
        salida, nuevas, cuenta = extraer(destino, "ui")
        catalogo.update(nuevas)
        if aplicar:
            destino.write_text(salida, encoding="utf-8")
        print(f"  index.html         +{len(nuevas):>3} claves  "
              f"(padre {cuenta['padre']}, envuelto {cuenta['envuelto']}, "
              f"attr {cuenta['atributo']})")

    for nombre in JS:
        ruta = APP_DIR / "static" / nombre
        a = len(catalogo)
        salida, catalogo, _ = extraer_de_plantillas(
            ruta, nombre.replace(".js", ""), catalogo)
        if aplicar and len(catalogo) != a:
            ruta.write_text(salida, encoding="utf-8")
        print(f"  {nombre:<18} +{len(catalogo) - a:>3} claves")

    if aplicar:
        cat_path.write_text(
            json.dumps(catalogo, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
            encoding="utf-8")
        print("APLICADO")
    else:
        print("(simulación — pasa --aplicar para escribir)")
    print(f"TOTAL claves nuevas: {len(catalogo) - antes}")


if __name__ == "__main__":
    main()


# ── Bloque 4: los mensajes con parámetros ─────────────────────────────
#
# Lo que quedó fuera de los bloques 2 y 3: la prosa que lleva un `${...}`
# DENTRO. No es una etiqueta, es un mensaje, y `data-i18n` no puede con él
# porque escribe `textContent` y borraría el valor interpolado.
#
# La regla del proyecto es que no se traduce por trozos: partir
# «Máximo ${MAX} proyectos abiertos» en «Máximo » y « proyectos abiertos»
# obliga a que el orden de las palabras sea el del castellano en las tres
# lenguas. Así que cada mensaje pasa a ser UNA cadena con parámetros CON
# NOMBRE: `t('core.max_proyectos', {max: MAX})`.

_NOMBRE_DE_EXPR = re.compile(r"([A-Za-z_$][\w$]*)\s*$")


def _nombre_de_parametro(expr: str, usados: set[str], n: int) -> str:
    """Un nombre legible para el hueco, sacado de la expresión.

    `MAX_PROJECTS` → `max_projects`; `s.nombre` → `nombre`;
    `escHtml(p.titulo)` → `titulo`. Si no se puede sacar nada —una ternaria,
    una plantilla anidada— se cae a `p1`, `p2`…
    
    Importa porque quien traduce ve la frase con el hueco dentro: con
    `{titulo}` sabe qué va ahí y puede moverlo; con `{p2}` no.
    """
    m = _NOMBRE_DE_EXPR.search(re.sub(r"[)\]\s]+$", "", expr))
    base = ""
    if m and len(expr) < 60 and not re.search(r"[?:]", expr):
        base = re.sub(r"[^a-z0-9_]", "", m.group(1).lower())
    if not base or base in ("escHtml", "eschtml", "length"):
        base = f"p{n}"
    k, i = base, 2
    while k in usados:
        k = f"{base}{i}"
        i += 1
    usados.add(k)
    return k


def mensajes_con_parametros(ruta: Path, area: str, catalogo: dict[str, str]
                            ) -> tuple[str, dict[str, str], int]:
    """Convierte la prosa interpolada de un fichero JS en llamadas a `t()`."""
    fuente = ruta.read_text(encoding="utf-8")
    usadas = {v: k for k, v in catalogo.items()}
    trozos: list[tuple[int, int, str]] = []
    n_total = 0

    for m in re.finditer(r"`((?:[^`\\]|\\.)*)`", fuente, re.S):
        tpl = m.group(1)
        if "<" not in tpl or "${" not in tpl:
            continue
        prohibidas = _regiones_interpoladas(tpl)
        enmasc = "".join("\x01" if any(a <= i < b for a, b in prohibidas) else c
                         for i, c in enumerate(tpl))
        rec = _Recolector(enmasc)
        try:
            rec.feed(enmasc)
            rec.close()
        except Exception:
            continue
        cambios: list[tuple[int, int, str]] = []
        for t in rec.textos:
            trozo = enmasc[t["ini"]:t["fin"]]
            if "\x01" not in trozo:
                continue
            if len(re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]", trozo)) < 4:
                continue
            crudo = tpl[t["ini"]:t["fin"]]
            # Los huecos de ESTE trozo, en orden.
            huecos = [(a, b) for a, b in prohibidas
                      if t["ini"] <= a and b <= t["fin"]]
            if not huecos:
                continue
            # Una expresión con PLANTILLA ANIDADA dentro (`${x ? `<b>..</b>` :
            # ''}`) se deja como está. El contador de llaves no la delimita
            # bien —el backtick interior abre su propio mundo— y reescribirla
            # a ciegas produce JS roto; lo cazó `node --check` en tab2 y tab3.
            # Son pocas y no merece un parser de JavaScript entero.
            if any("`" in tpl[a + 2:b - 1] for a, b in huecos):
                continue
            usados: set[str] = set()
            plantilla, args, cursor = "", [], t["ini"]
            for i, (a, b) in enumerate(huecos, 1):
                plantilla += tpl[cursor:a]
                expr = tpl[a + 2:b - 1]
                nombre = _nombre_de_parametro(expr, usados, i)
                plantilla += "{" + nombre + "}"
                args.append(f"{nombre}: {expr}")
                cursor = b
            plantilla += tpl[cursor:t["fin"]]
            limpio = " ".join(plantilla.split())
            if limpio in usadas:
                clave = usadas[limpio]
            else:
                base = f"{area}.{slug(re.sub(r'\\{\\w+\\}', ' ', limpio))}"
                clave, j = base, 2
                while clave in catalogo:
                    clave = f"{base}_{j}"
                    j += 1
                catalogo[clave] = limpio
                usadas[limpio] = clave
            izq = crudo[:len(crudo) - len(crudo.lstrip())]
            der = crudo[len(crudo.rstrip()):]
            llamada = (f"{izq}${{t('{clave}', {{{', '.join(args)}}})}}{der}")
            cambios.append((t["ini"], t["fin"], llamada))
            n_total += 1
        if cambios:
            nuevo_tpl = tpl
            for a, b, txt in sorted(cambios, key=lambda c: -c[0]):
                nuevo_tpl = nuevo_tpl[:a] + txt + nuevo_tpl[b:]
            trozos.append((m.start(1), m.end(1), nuevo_tpl))

    salida = fuente
    for a, b, txt in sorted(trozos, key=lambda x: -x[0]):
        salida = salida[:a] + txt + salida[b:]
    return salida, catalogo, n_total


ULTIMA_CLAVE = ""


def mensajes_con_parametros_uno(ruta: Path, area: str, catalogo: dict,
                                fuente: str, vetados: set) -> tuple[str, dict, int]:
    """Como `mensajes_con_parametros`, pero aplica SOLO EL PRIMERO que quede.

    Existe para poder validar cada cambio con `node --check` y revertir el que
    rompa el fichero. Hace falta porque un regex no delimita una plantilla que
    contiene otra plantilla —se corta en el primer backtick anidado— y escribir
    un parser de JavaScript para cincuenta y cinco sitios no se sostiene.
    """
    global ULTIMA_CLAVE
    usadas = {v: k for k, v in catalogo.items()}
    for m in re.finditer(r"`((?:[^`\\]|\\.)*)`", fuente, re.S):
        tpl = m.group(1)
        if "<" not in tpl or "${" not in tpl:
            continue
        prohibidas = _regiones_interpoladas(tpl)
        enmasc = "".join("\x01" if any(a <= i < b for a, b in prohibidas) else c
                         for i, c in enumerate(tpl))
        rec = _Recolector(enmasc)
        try:
            rec.feed(enmasc)
            rec.close()
        except Exception:
            continue
        for t in sorted(rec.textos, key=lambda x: x["ini"]):
            trozo = enmasc[t["ini"]:t["fin"]]
            if "\x01" not in trozo:
                continue
            if len(re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]", trozo)) < 4:
                continue
            huecos = [(a, b) for a, b in prohibidas
                      if t["ini"] <= a and b <= t["fin"]]
            if not huecos:
                continue
            crudo = tpl[t["ini"]:t["fin"]]
            usados: set[str] = set()
            plantilla, args, cursor = "", [], t["ini"]
            for i, (a, b) in enumerate(huecos, 1):
                plantilla += tpl[cursor:a]
                expr = tpl[a + 2:b - 1]
                nombre = _nombre_de_parametro(expr, usados, i)
                plantilla += "{" + nombre + "}"
                args.append(f"{nombre}: {expr}")
                cursor = b
            plantilla += tpl[cursor:t["fin"]]
            limpio = " ".join(plantilla.split())
            if limpio in usadas:
                clave = usadas[limpio]
            else:
                base = f"{area}.{slug(re.sub(r'[{]\\w+[}]', ' ', limpio))}"
                clave, j = base, 2
                while clave in catalogo:
                    clave = f"{base}_{j}"
                    j += 1
            if clave in vetados:
                continue
            catalogo[clave] = limpio
            ULTIMA_CLAVE = clave
            izq = crudo[:len(crudo) - len(crudo.lstrip())]
            der = crudo[len(crudo.rstrip()):]
            llamada = f"{izq}${{t('{clave}', {{{', '.join(args)}}})}}{der}"
            nuevo_tpl = tpl[:t["ini"]] + llamada + tpl[t["fin"]:]
            salida = fuente[:m.start(1)] + nuevo_tpl + fuente[m.end(1):]
            return salida, catalogo, 1
    return fuente, catalogo, 0



# ── Bloque 7: las cadenas sueltas del JS ──────────────────────────────
#
# Lo que no es marcado ni mensaje con parámetros: el texto que va a
# `showToast`, a `showConfirm`, a un `textContent` o a una etiqueta calculada.
# Los bloques 2 y 3 solo miraron plantillas con HTML dentro, así que estas 524
# se quedaron incrustadas — y traducido todo lo demás, son las que delatan que
# la app no está entera.
#
# Lo que NO se toca:
#   · lo que va a `console.*`, que es para quien depura, no para el usuario
#     (mismo criterio que los docstrings del backend);
#   · los comentarios;
#   · identificadores y cadenas que son solo glosario.

_LINEA_DE_CONSOLA = re.compile(r"console\.\w+\s*\(")


def cadenas_sueltas(ruta: Path, area: str, catalogo: dict[str, str],
                    es_frase) -> tuple[str, dict[str, str], int]:
    """Sustituye por `tr('clave')` las cadenas de texto sueltas de un JS."""
    fuente = ruta.read_text(encoding="utf-8")
    usadas = {v: k for k, v in catalogo.items()}
    # Las plantillas ya las trataron los bloques 2, 3 y 4.
    plantillas = [(m.start(), m.end())
                  for m in re.finditer(r"`((?:[^`\\]|\\.)*)`", fuente, re.S)]
    lineas_ini = [0]
    for l in fuente.splitlines(keepends=True):
        lineas_ini.append(lineas_ini[-1] + len(l))

    def linea_de(pos: int) -> str:
        import bisect
        i = bisect.bisect_right(lineas_ini, pos) - 1
        return fuente[lineas_ini[i]:lineas_ini[i + 1] if i + 1 < len(lineas_ini)
                      else len(fuente)]

    cambios, n = [], 0
    for m in re.finditer(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"", fuente):
        if any(a <= m.start() < b for a, b in plantillas):
            continue
        crudo = m.group(1) if m.group(1) is not None else m.group(2)
        texto = " ".join(crudo.split())
        if not es_frase(texto) or _solo_glosario(texto) or _es_identificador(texto):
            continue
        linea = linea_de(m.start())
        if _LINEA_DE_CONSOLA.search(linea) or linea.lstrip().startswith(("//", "*")):
            continue
        # Un escape dentro de la cadena (`\'`, `\n`) se deja: reconstruirlo en
        # el catálogo pediría decidir qué es literal y qué es formato.
        if "\\" in crudo:
            continue
        if texto in usadas:
            clave = usadas[texto]
        else:
            base = f"{area}.{slug(texto)}"
            clave, i = base, 2
            while clave in catalogo:
                clave, i = f"{base}_{i}", i + 1
            catalogo[clave] = texto
            usadas[texto] = clave
        cambios.append((m.start(), m.end(), f"tr('{clave}')"))
        n += 1

    salida = fuente
    for a, b, txt in sorted(cambios, key=lambda c: -c[0]):
        salida = salida[:a] + txt + salida[b:]
    return salida, catalogo, n


def mensajes_sin_marcado(ruta: Path, area: str, catalogo: dict[str, str],
                         es_frase) -> tuple[str, dict[str, str], int]:
    """Las plantillas que son SOLO un mensaje, sin marcado dentro.

    El bloque 4 exigía `<` en la plantilla —trataba las que son HTML— y dejó
    fuera 153 que son mensajes puros: `` `Máximo ${MAX} proyectos abiertos.` ``.
    Aquí la plantilla ENTERA pasa a ser un valor del catálogo y la expresión se
    sustituye por una sola llamada, que es lo que hay que hacer con un mensaje:
    partirlo obligaría al inglés y al catalán a seguir el orden del castellano.
    """
    fuente = ruta.read_text(encoding="utf-8")
    usadas = {v: k for k, v in catalogo.items()}
    cambios, n = [], 0
    for m in re.finditer(r"`((?:[^`\\]|\\.)*)`", fuente, re.S):
        tpl = m.group(1)
        if "<" in tpl or "`" in tpl:
            continue
        regiones = _regiones_interpoladas(tpl)
        # Una plantilla con otra plantilla dentro de una expresión no se toca:
        # el contador de llaves no la delimita (ver el caso de la Fase D).
        if any("`" in tpl[a + 2:b - 1] for a, b in regiones):
            continue
        usados: set[str] = set()
        plantilla, args, cursor = "", [], 0
        for i, (a, b) in enumerate(regiones, 1):
            plantilla += tpl[cursor:a]
            expr = tpl[a + 2:b - 1]
            if "\n" in expr:
                plantilla = None
                break
            nombre = _nombre_de_parametro(expr, usados, i)
            plantilla += "{" + nombre + "}"
            args.append(f"{nombre}: {expr}")
            cursor = b
        if plantilla is None:
            continue
        plantilla += tpl[cursor:]
        limpio = " ".join(plantilla.split())
        if not es_frase(re.sub(r"\{\w+\}", " ⟦⟧ ", limpio)):
            continue
        if _solo_glosario(limpio) or _es_identificador(limpio):
            continue
        if limpio in usadas:
            clave = usadas[limpio]
        else:
            base = f"{area}.{slug(re.sub(r'[{]\w+[}]', ' ', limpio))}"
            clave, j = base, 2
            while clave in catalogo:
                clave, j = f"{base}_{j}", j + 1
            catalogo[clave] = limpio
            usadas[limpio] = clave
        args_txt = (", {" + ", ".join(args) + "}") if args else ""
        # Se sustituye la plantilla ENTERA, backticks incluidos.
        cambios.append((m.start(), m.end(), f"tr('{clave}'{args_txt})"))
        n += 1
    salida = fuente
    for a, b, txt in sorted(cambios, key=lambda c: -c[0]):
        salida = salida[:a] + txt + salida[b:]
    return salida, catalogo, n
