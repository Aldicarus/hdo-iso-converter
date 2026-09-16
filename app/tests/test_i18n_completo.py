"""¿Está la app entera traducida? Y ¿carga en los tres idiomas?

Los seis bloques anteriores movieron ~1.600 frases al catálogo. Lo que este
módulo vigila es lo que viene DESPUÉS: que nadie añada texto castellano suelto
sin pasar por `tr()`, que no se pida una clave que no existe, y que la interfaz
no se desborde en inglés ni en catalán —que son más largos que el castellano en
casi todo—.

Sin esto, la traducción se degrada sola: el primer botón nuevo con su literal
en castellano no rompe nada, no da ningún error, y aparece en español dentro de
una pantalla inglesa.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_i18n_completo -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import captura_castellano as captura  # noqa: E402
from frontend_sources import html, piezas, rutas  # noqa: E402

NODE = shutil.which("node")
_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)

IDIOMAS = ("es", "en", "ca")

# Texto castellano que se queda en el código A PROPÓSITO, con su motivo.
# Ampliarla es una decisión: si una frase nueva aparece sin entrada aquí, el
# test falla, y hace bien.
FUERA_DEL_CATALOGO = {
    # Lo que va a la consola del navegador es para quien depura, igual que un
    # docstring. No lo ve ningún usuario.
    "console": "mensajes de `console.error`/`warn`, para depurar",
    # Las claves de parser del log: contratos, no texto.
    "markers": "`§§PROGRESS§§`, `━━━`, `📋 Plan`… son claves de parser",
}


def _catalogos() -> set[str]:
    """Todos los valores castellanos, con los huecos normalizados."""
    fuera: set[str] = set()
    for ruta in (APP_DIR / "static" / "i18n" / "es.json",
                 APP_DIR / "i18n" / "es.json"):
        if ruta.exists():
            fuera |= set(json.loads(ruta.read_text(encoding="utf-8")).values())
    manual = APP_DIR / "static" / "i18n" / "manual" / "es.json"
    if manual.exists():
        for h in json.loads(manual.read_text(encoding="utf-8")).values():
            fuera |= set(captura._del_html(h))
    # `{max}` y `⟦⟧` son el mismo hueco escrito de dos formas.
    fuera |= {" ".join(re.sub(r"\{\w+\}", " ⟦⟧ ", v).split()) for v in fuera}
    return {" ".join(v.split()) for v in fuera}


_CONSOLA = re.compile(r"console\.\w+\s*\(")


class TestNoQuedaCastellanoSuelto(unittest.TestCase):
    """La prueba de que la traducción está completa, y sigue estándolo."""

    @classmethod
    def setUpClass(cls):
        cls.cat = _catalogos()

    def test_ninguna_cadena_del_js_se_ha_quedado_fuera(self):
        sueltas = []
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            # Los backticks se emparejan sobre el fuente SIN comentarios: uno
            # dentro de un comentario descuadra el emparejado y a partir de
            # ahí se toma por plantilla lo que no lo es. En `settings.js` eso
            # dejaba tres regiones ciegas de hasta 5.243 caracteres, y ahí
            # sobrevivió toda la familia de badges y placeholders castellanos
            # de ⚙︎ Configuración.
            plantillas = [(a, b) for a, b, _ in captura.regiones_de_plantilla(src)]
            lineas = src.splitlines(keepends=True)
            base = [0]
            for l in lineas:
                base.append(base[-1] + len(l))
            for m in re.finditer(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"", src):
                if any(a <= m.start() < b for a, b in plantillas):
                    continue
                s = " ".join((m.group(1) or m.group(2) or "").split())
                if not captura.es_frase(s):
                    continue
                # Estar en el catálogo NO es pasar por `tr()`. El guard daba
                # por buena la cadena si su texto coincidía con el valor de
                # alguna clave, y eso dejó pasar los badges y placeholders de
                # ⚙︎ Configuración: la frase estaba en el catálogo —extraída
                # del marcado— y el JS seguía escribiendo el literal. Lo que
                # se exime es la CLAVE que se le pasa a `tr(`, no el texto.
                if src[max(0, m.start() - 6):m.start()].endswith("tr("):
                    continue
                import bisect
                i = bisect.bisect_right(base, m.start()) - 1
                linea = lineas[i] if i < len(lineas) else ""
                if _CONSOLA.search(linea) or linea.lstrip().startswith(("//", "*")):
                    continue
                sueltas.append(f"{Path(r).name}:{i + 1}: {s[:66]}")
        self.assertEqual(sueltas, [], (
            f"\n{len(sueltas)} frase(s) castellanas sueltas en el JS. Pasa por "
            f"`tr('clave')` o, si de verdad no es texto de usuario, di por qué "
            f"en FUERA_DEL_CATALOGO:\n  · " + "\n  · ".join(sueltas[:15])))

    def test_ningun_texto_del_marcado_se_ha_quedado_fuera(self):
        fuera = [x for x in captura._del_html(html())
                 if captura.es_frase(x) and " ".join(x.split()) not in self.cat]
        self.assertEqual(fuera, [], (
            f"\ntexto castellano en `index.html` sin `data-i18n`:\n  · "
            + "\n  · ".join(fuera[:12])))

    def test_ninguna_linea_del_backend_se_ha_quedado_fuera(self):
        fuera = sorted(
            f for f in captura.frases_del_backend()
            if " ".join(f.split()) not in self.cat
            and self._sin_prefijo(f) not in self.cat
            # Una línea que se queda SIN PROSA al quitarle el prefijo y el
            # hueco no tiene nada que traducir: es `f"[Validación] {msg}"`,
            # donde el texto lo aporta el parámetro.
            and re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{3}",
                          re.sub(r"⟦⟧", " ", self._sin_prefijo(f))))
        self.assertEqual(fuera, [], (
            f"\n{len(fuera)} línea(s) del servidor sin pasar por `tr()`:\n  · "
            + "\n  · ".join(fuera[:12])))

    @staticmethod
    def _sin_prefijo(frase: str) -> str:
        import extraer_backend as eb
        m = eb._PREFIJO.match(frase)
        prosa = m.group(5) if m else frase
        return " ".join(re.sub(r"\s*━+\s*$", "", prosa).split())


def _llamadas_a_tr(src: str) -> list[tuple[str, int, int]]:
    """`(clave, inicio, fin)` de cada `tr('clave'…)`, con el paréntesis CERRADO.

    Hace falta emparejar el paréntesis y no cortar en el primero: un mensaje
    con parámetros es `tr('k', {a: f(x)})`, y un regex que pare en `')'` deja
    fuera justo a los que llevan datos —que son la mitad del catálogo—. Con el
    corte ingenuo, la comprobación de abajo no veía ninguna juntura en la que
    uno de los dos trozos tuviera un hueco, y cuatro de los seis casos reales
    eran de esos.
    """
    fuera = []
    for m in re.finditer(r"\btr\('([\w.]+)'", src):
        i = src.index("(", m.start())
        prof, j, comilla = 0, i, None
        while j < len(src):
            c = src[j]
            if comilla:
                if c == "\\":
                    j += 2
                    continue
                if c == comilla:
                    comilla = None
            elif c in "\"'`":
                comilla = c
            elif c in "([{":
                prof += 1
            elif c in ")]}":
                prof -= 1
                if prof == 0:
                    break
            j += 1
        fuera.append((m.group(1), m.start(), j + 1))
    return fuera


# Las tres funciones que producen un VOLCADO de diagnóstico, no interfaz: el
# modal «🔬 Datos ISO», su equivalente de Tab 2 y el texto de Validaciones que
# se copia al portapapeles. Son etiquetas para depurar —`raw: lang=`,
# `── Pistas descartadas ──`— y se leen igual en cualquier idioma, como los
# markers del log. Traducirlas añadiría ~38 claves que nadie mira salvo cuando
# algo va mal, y cambiaría el texto que un usuario pega en un informe.
VOLCADOS_DE_DIAGNOSTICO = {
    "showRawAnalysisData":         "el modal 🔬 Datos ISO de Tab 1",
    "showRawMkvData":              "su equivalente en Tab 2",
    "_rgrfCopyToClipboard":        "el Markdown de la radiografía DV+HDR",
    "_cmv40GateDiagnosticoTexto":  "el texto de Validaciones que se copia",
    # Los cinco bloques de la card 🛡️ Validaciones y su cabecera. Son el
    # detalle técnico de los trust gates —`cuerpo 97,4%`, `· sync +16`,
    # `source ok`, `VARIABLE · 0,0/0,0`— que se lee contra el log y contra la
    # hoja de DoviTools, las dos en inglés. Traducirlos no ayudaría a nadie a
    # entender un gate y cambiaría el texto que se pega en un informe.
    "_cmv40GateBloque2": "detalle técnico de la card de Validaciones",
    "_cmv40GateBloque3": "detalle técnico de la card de Validaciones",
    "_cmv40GateBloque4": "detalle técnico de la card de Validaciones",
    "_cmv40GateBloque5": "detalle técnico de la card de Validaciones",
    "_cmv40RenderGateCardBC": "cabecera de la card de Validaciones",
}

# Rótulos cortos que se quedan en castellano por otro motivo, con el suyo.
CORTOS_ACEPTADOS = {
    "cargarIdioma": "la URL del catálogo y el código HTTP de un fallo",
    "_cmv40ManualSecciones": "la URL del manual y el código HTTP",
    # `TrueHD Atmos 7.1`, `DD+ Atmos 5.1`: el nombre del codec y los canales.
    # CLAUDE.md los fija —«los literales de pistas siguen exactamente la
    # spec»— y esta función REPLICA `phase_b._codec_literal` para las pistas
    # que se recuperan a mano. Traducir aquí las dejaría distintas de las que
    # escribe el backend, que es el bug que la función existe para no tener.
    "_buildAudioCodecLiteral": "el literal de codec de una pista, fijado por la spec",
}

_HUECO = re.compile(r"\$\{[^}]*\}")
# El contexto de la línea delata que la cadena es un id, una URL, CSS o una
# clase, no texto: ahí una palabra castellana es el nombre de algo.
_NO_ES_TEXTO = re.compile(
    r"(getElementById|querySelector|apiFetch|classList|className|\.style|url\("
    r"|setAttribute|on\w+=|data-\w+=|\.id ?=|href|console\.|localStorage"
    r"|\bclass=|style=|fetch\(|\.log\.txt)")
# Términos que no son castellano aunque lo parezcan.
_TECNICO = re.compile(
    r"(kbps|nits|hevc|mkvmerge|mediainfo|ffprobe|ffmpeg|maxcll|maxfall|bitrate"
    r"|codec|profile|frames?|combos?|trims?|gates?|workflow|playlist|remux"
    r"|demux|hdr|pipeline|log|json|html|mpls|m2ts|bdmv)", re.I)


class TestNoQuedaNingunFragmentoCortoSuelto(unittest.TestCase):
    """Un rótulo de dos palabras pegado a un hueco también es texto.

    `captura.es_frase` exige seis caracteres, DOS palabras y un acento o una
    palabra función, y eso deja fuera justo los rótulos cortos: `hace ⟦⟧ min`,
    `⟦⟧ escenas`, `Crear ⟦⟧ proyecto⟦⟧`, `Temporada ⟦⟧`, `Movido a: ⟦⟧`. Eran
    **38 claves** que salían en castellano con la app en inglés, y no las veía
    ningún guard — el de castellano suelto porque el umbral las descarta, y el
    del golden porque el golden se capturó con el mismo umbral.

    El criterio de aquí es otro: una cadena con un hueco cuya prosa contiene
    una palabra que YA está traducida en otra clave del catálogo. Si la
    palabra es nuestra y está traducida en otro sitio, aquí también tiene que
    estarlo.
    """

    @classmethod
    def setUpClass(cls):
        es = json.loads(
            (APP_DIR / "static" / "i18n" / "es.json").read_text(encoding="utf-8"))
        cls.vocabulario = set()
        for v in es.values():
            cls.vocabulario |= {
                w.lower() for w in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{4,}", v)}

    @staticmethod
    def _funcion_de(src: str, pos: int) -> str:
        m = list(re.finditer(r"^(?:async )?function (\w+)\(", src[:pos], re.M))
        return m[-1].group(1) if m else ""

    def test_ninguna_cadena_corta_con_hueco_se_queda_en_castellano(self):
        import bisect
        sueltas = []
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            lineas = src.splitlines(keepends=True)
            base = [0]
            for l in lineas:
                base.append(base[-1] + len(l))
            trozos = [(a, cont) for a, _, cont in captura.regiones_de_plantilla(src)]
            trozos += [(m.start(), m.group(1) or m.group(2) or "") for m in
                       re.finditer(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"", src)]
            for pos, t in trozos:
                # Una barra descarta la cadena porque suele ser una ruta
                # (`/api/...`, `cmv40/${id}`) — pero `Canales / frecuencia:`
                # también lleva una, y ahí es puntuación de un rótulo. Lo que
                # las separa es el espacio a los dos lados: una ruta no lo
                # tiene. Sin este matiz se escapaban los dos rótulos del
                # tooltip de pista, que es texto de interfaz en tres sitios.
                barra_de_ruta = re.search(r"[^ ]/|/[^ ]", t)
                if ("<" in t or "data-i18n" in t or len(t) > 180
                        or barra_de_ruta or "#" in t):
                    continue
                norm = " ".join(_HUECO.sub("⟦⟧", t).split())
                if not norm or "⟦⟧" not in norm or captura.es_frase(norm):
                    continue     # lo largo ya lo cubre el otro guard
                # Un id de elemento no lleva espacios (`panel-project-⟦⟧`,
                # `⟦⟧-tmdb-card`, `cola:⟦⟧`), y la prosa siempre lleva al
                # menos uno. Es lo que separa un nombre de un texto sin
                # mantener una lista de ids.
                if " " not in norm or '="' in norm:
                    continue     # sin espacios es un id; con `="`, marcado
                # Si después de sustituir sigue habiendo un `${`, el regex de
                # plantillas cortó a mitad de una PLANTILLA ANIDADA y lo que
                # tenemos delante no es la cadena completa. No se puede
                # juzgar; el otro guard mira esos mensajes por su clave.
                if "${" in norm:
                    continue
                palabras = [w.lower() for w in
                            re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{4,}", norm)
                            if not _TECNICO.fullmatch(w)]
                if not any(w in self.vocabulario for w in palabras):
                    continue
                i = bisect.bisect_right(base, pos) - 1
                if _NO_ES_TEXTO.search(lineas[i] if i < len(lineas) else ""):
                    continue
                fn = self._funcion_de(src, pos)
                if fn in VOLCADOS_DE_DIAGNOSTICO or fn in CORTOS_ACEPTADOS:
                    continue
                sueltas.append(f"{Path(r).name}:{i + 1} ({fn}): {norm[:56]}")
        self.assertEqual(sueltas, [], (
            f"\n{len(sueltas)} rótulo(s) cortos en castellano junto a un hueco. "
            f"Pásalos por `tr()`,\no —si de verdad son un volcado de "
            f"diagnóstico— di en qué función y por qué en\n"
            f"VOLCADOS_DE_DIAGNOSTICO:\n  · " + "\n  · ".join(sueltas[:15])))

    def test_las_exenciones_siguen_correspondiendo_a_codigo_real(self):
        """Una exención que ya no apunta a nada parece cobertura."""
        fuentes = "\n".join(Path(r).read_text(encoding="utf-8") for r in rutas())
        muertas = [f for f in (*VOLCADOS_DE_DIAGNOSTICO, *CORTOS_ACEPTADOS)
                   if f"function {f}(" not in fuentes]
        self.assertEqual(muertas, [], (
            f"\nestas funciones exentas ya no existen: {muertas}"))


class TestLosFragmentosSeCosenBien(unittest.TestCase):
    """Dos `tr()` pegados tienen que llevar un ESPACIO entre las palabras.

    El espacio del borde de una cadena es significativo cuando la cadena se
    CONCATENA, y `" ".join(txt.split())` se lo come: `'… Un reproductor '` +
    `'compatible con…'` acabó rindiendo «reproductorcompatible». El golden del
    castellano **no lo ve**, porque su comparación también normaliza los
    espacios — así que hace falta este guard aparte.

    Se arreglaron 45 casos sacando el espacio FUERA de la llamada, y después
    otros 6 que este guard no veía por dos motivos, los dos corregidos aquí:

    - **un `tr()` con parámetros no se reconocía** (ver `_llamadas_a_tr`);
    - **se daba por bueno cualquier signo en el borde.** Es falso: el punto
      final de una frase no separa nada si detrás no hay espacio, y lo que
      salía era «…queda intacta.Esto puede tardar…». Lo único que vale es un
      carácter de espacio —a un lado o al otro—, salvo en las junturas que son
      tirantes a propósito (un paréntesis, una coma, unas comillas de cierre).
    """

    TIRANTES_A_LA_IZQUIERDA = "«([{/-—:"
    TIRANTES_A_LA_DERECHA = "»)]},.;:!?%/-—"

    @classmethod
    def setUpClass(cls):
        cls.es = json.loads(
            (APP_DIR / "static" / "i18n" / "es.json").read_text(encoding="utf-8"))

    def test_ningun_par_de_claves_se_pega_sin_separador(self):
        pegados = []
        junta = re.compile(r"^\s*\+\s*$")
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            llamadas = _llamadas_a_tr(src)
            for (ka, _, fin), (kb, ini, _) in zip(llamadas, llamadas[1:]):
                if not junta.match(src[fin:ini]):
                    continue              # hay algo en medio: `+ ' ' +`, …
                a, b = self.es.get(ka, ""), self.es.get(kb, "")
                if not a or not b:
                    continue
                if a[-1:].isspace() or b[:1].isspace():
                    continue
                if a[-1:] in self.TIRANTES_A_LA_IZQUIERDA:
                    continue
                if b[:1] in self.TIRANTES_A_LA_DERECHA:
                    continue
                linea = src[:ini].count("\n") + 1
                pegados.append(
                    f"{Path(r).name}:{linea}: «…{a[-20:]}» + «{b[:20]}…»")
        self.assertEqual(pegados, [], (
            "\ndos claves concatenadas sin espacio: el texto sale con las "
            "palabras pegadas.\nSaca el espacio FUERA del `tr()`:\n  · "
            + "\n  · ".join(pegados[:10])))


# Claves cuyo valor NO puede encabezar una frase y que, aun así, se quedan
# sueltas a propósito. Cada una dice por qué; ampliar la lista es una
# decisión, no un arreglo.
FRAGMENTOS_ACEPTADOS = {
    # La leyenda de confianza del modal de series intercala PUNTOS DE COLOR
    # con su propio tooltip entre los trozos de prosa
    # (`<span class="punto-conf alta" data-i18n-tip="…">`). Fusionarla metería
    # clases de presentación Y otras claves de i18n dentro de una cadena
    # traducible, que es peor que el fragmento: el traductor tendría que no
    # tocar un `data-i18n-tip` incrustado.
    "ui.una_estimacion": "leyenda con puntos de color intercalados",
    "ui.basada_en_la_duracion_de_cada": "leyenda con puntos de color intercalados",
    "ui.coincidencia_alta": "rótulo de un punto de la leyenda",
    "ui.coincidencia_baja": "rótulo de un punto de la leyenda",
    "ui.sin_coincidencia": "rótulo de un punto de la leyenda",
    "ui.que_cada_mpls_corresponda_al_episodio": "leyenda con puntos intercalados",
    # No es una frase partida: es un TÍTULO más una etiqueta. «TMDb — Clave de
    # la API» y el chip «opcional» son dos cosas distintas, y el chip se
    # reutiliza en dos secciones.
    "ui.opcional_2": "chip junto a un título, no la cola de una frase",
    # Rótulos de eje del gráfico de luminancia: van en minúscula porque son
    # nombres de campo del RPU, no prosa.
    "tab2.peak": "rótulo del gráfico; es el nombre del campo del RPU",
    "tab2.avg": "rótulo del gráfico; es el nombre del campo del RPU",
    "tab2.nits": "unidad, va detrás de una cifra interpolada",
}


class TestNingunaFraseSePartePorMarcado(unittest.TestCase):
    """Una frase con un `<strong>` en medio es UNA clave, no tres.

    El extractor ve tres nodos de texto y saca tres claves, y eso traduce por
    fragmentos por la puerta de atrás: quien traduce uno sin ver los otros no
    puede mover el orden de las palabras —que en inglés cambia justo alrededor
    del énfasis— y quien edita el castellano de uno deja los demás
    descolgados. `REGISTRO.md` lo prohíbe de frente.

    El arreglo es `data-i18n-html` en el bloque, con el marcado en línea
    DENTRO del valor traducible. Se hizo en 24 bloques. Los que quedan están
    en `FRAGMENTOS_ACEPTADOS`, cada uno con su motivo.

    La señal de que un trozo es la cola de otro: su valor no puede ENCABEZAR
    una frase — empieza en minúscula o en signo de puntuación.
    """

    BLOQUE = re.compile(
        r"<(p|div|span|li|label|small)\b[^>]*>((?:(?!</?\1\b).)*?)</\1>", re.S)

    @classmethod
    def setUpClass(cls):
        cls.es = json.loads(
            (APP_DIR / "static" / "i18n" / "es.json").read_text(encoding="utf-8"))

    @staticmethod
    def _es_cola(v: str) -> bool:
        return bool(v) and (v[0] in ".,:;)»" or v[0].islower())

    def test_ningun_bloque_parte_una_frase_en_varias_claves(self):
        partidos = []
        for nombre, src in [("index.html", html())] + [
                (Path(r).name, Path(r).read_text(encoding="utf-8")) for r in rutas()]:
            for m in self.BLOQUE.finditer(src):
                claves = re.findall(r'data-i18n="([^"]+)"', m.group(2))
                if len(claves) < 2:
                    continue
                colas = [k for k in claves
                         if self._es_cola(self.es.get(k, ""))
                         and k not in FRAGMENTOS_ACEPTADOS]
                if colas:
                    linea = src[:m.start()].count("\n") + 1
                    partidos.append(f"{nombre}:{linea}: {colas}")
        self.assertEqual(partidos, [], (
            "\nestos bloques parten una frase en varias claves. Pásalos a UNA "
            "clave con\n`data-i18n-html` y el marcado dentro del valor, o di "
            "por qué no en\nFRAGMENTOS_ACEPTADOS:\n  · " + "\n  · ".join(partidos[:10])))

    def test_la_lista_de_aceptados_no_se_queda_vieja(self):
        """Una entrada que ya no existe parece cobertura y no cubre nada."""
        muertas = [k for k in FRAGMENTOS_ACEPTADOS if k not in self.es]
        self.assertEqual(muertas, [], (
            f"\nestas claves de FRAGMENTOS_ACEPTADOS ya no están en el "
            f"catálogo: {muertas}"))

    def test_el_marcado_de_un_valor_traducible_no_lleva_otra_clave(self):
        """Un `data-i18n` DENTRO de un valor traducible se realimenta.

        `pintarTextos` escribe el `innerHTML`, el observador ve el nodo nuevo,
        vuelve a pintar… y además obligaría al traductor a no tocar una clave
        incrustada. Los iconos sí pueden ir (`data-icono` es declarativo y lo
        pinta su propio observador, que marca lo ya pintado).
        """
        malos = []
        for donde in ("static/i18n",):
            for idioma in IDIOMAS:
                cat = json.loads((APP_DIR / donde / f"{idioma}.json")
                                 .read_text(encoding="utf-8"))
                for k, v in cat.items():
                    if "data-i18n" in v:
                        malos.append(f"{idioma}.json → {k}")
        self.assertEqual(malos, [], (
            "\nestos valores llevan otra clave de i18n dentro:\n  · "
            + "\n  · ".join(malos[:10])))


class TestElSaltoDeLineaEsUnSaltoDeLinea(unittest.TestCase):
    """Un `\\n` del JS es un salto; en JSON tiene que serlo también.

    En la plantilla original `\\n` era un escape que el motor resolvía, y al
    pasar el texto a JSON se guardó como **dos caracteres**, así que el modal
    escribía `\\n` en pantalla donde antes partía el párrafo. Eran 6 claves de
    `tab1.js` (confirmar la cola, abrir un proyecto, borrarlo…).

    El golden del castellano tampoco lo ve: compara el texto del fuente, donde
    los dos caracteres son justo lo que estaba escrito.
    """

    def test_ningun_catalogo_guarda_el_escape_en_crudo(self):
        malas = []
        for donde in ("static/i18n", "i18n", "static/i18n/manual"):
            for idioma in IDIOMAS:
                ruta = APP_DIR / donde / f"{idioma}.json"
                if not ruta.exists():
                    continue
                for k, v in json.loads(ruta.read_text(encoding="utf-8")).items():
                    if "\\n" in str(v):
                        malas.append(f"{donde}/{idioma}.json → {k}")
        self.assertEqual(malas, [], (
            "\nestas claves guardan «\\n» como texto, así que se imprime tal "
            "cual:\n  · " + "\n  · ".join(malas[:10])))


class TestLosTresCatalogosEstanCompletos(unittest.TestCase):

    def test_las_tres_lenguas_tienen_todas_las_claves(self):
        for donde in ("static/i18n", "i18n"):
            d = {i: json.loads((APP_DIR / donde / f"{i}.json").read_text(encoding="utf-8"))
                 for i in IDIOMAS}
            for otra in ("en", "ca"):
                faltan = sorted(set(d["es"]) - set(d[otra]))
                self.assertEqual(faltan, [],
                                 f"[{donde}] sin traducir a `{otra}`: {faltan[:8]}")

    def test_el_volumen_es_el_que_se_midio(self):
        """Un catálogo que se vacía pasaría los demás tests sin vigilar nada."""
        ui = json.loads((APP_DIR / "static" / "i18n" / "es.json").read_text(encoding="utf-8"))
        back = json.loads((APP_DIR / "i18n" / "es.json").read_text(encoding="utf-8"))
        self.assertGreater(len(ui), 1000)
        self.assertGreater(len(back), 400)


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestLaAppCargaEnLosTresIdiomas(unittest.TestCase):
    """La prueba de fuego: abrir `index.html` en cada idioma.

    Es el único sitio donde se ven los fallos que no da ningún test de fuente:
    una clave que no existe (sale la clave en pantalla), un error de JS al
    pintar, o un catálogo que no carga.
    """

    @classmethod
    def setUpClass(cls):
        cls.datos = {i: cls._cargar(i) for i in IDIOMAS}

    @staticmethod
    def _cargar(idioma: str) -> dict:
        from frontend_sources import catalogo_es
        cat_dir = APP_DIR / "static" / "i18n"
        cat = json.loads((cat_dir / f"{idioma}.json").read_text(encoding="utf-8"))
        man = (cat_dir / "manual" / f"{idioma}.json").read_text(encoding="utf-8")
        # El fetch por `file://` no funciona: se sustituye por el catálogo del
        # idioma que toca, que es lo que contestaría el servidor.
        stub = ("<script>(function(){\n"
                f"const _c = {json.dumps(cat, ensure_ascii=False)};\n"
                f"const _m = {man};\n"
                "try { localStorage.setItem('hdo_idioma', "
                f"{json.dumps(idioma)}); }} catch (e) {{}}\n"
                "window.__errores = [];\n"
                "window.addEventListener('error', e => window.__errores.push("
                "(e.message||'') + ' @ ' + (e.filename||'').split('/').pop()"
                " + ':' + e.lineno));\n"
                "window.fetch = function (u) {\n"
                "  const s = String(u);\n"
                "  if (s.includes('/i18n/manual/'))\n"
                "    return Promise.resolve({ok: true, json: () => Promise.resolve(_m)});\n"
                "  if (s.includes('/i18n/'))\n"
                "    return Promise.resolve({ok: true, json: () => Promise.resolve(_c)});\n"
                "  return Promise.reject(new Error('sin red'));\n"
                "};})();</script>\n")
        volcado = """
<pre id="__out"></pre>
<script>
setTimeout(function () {
  const crudas = [...document.querySelectorAll('[data-i18n],[data-i18n-html]')]
    .map(e => (e.textContent || '').trim())
    .filter(x => /^[a-z0-9_]+\\.[a-z0-9_]+$/.test(x));
  document.getElementById('__out').textContent = JSON.stringify({
    errores: window.__errores,
    ausentes: (typeof clavesAusentes === 'function' ? clavesAusentes() : ['sin motor']),
    crudas: crudas.slice(0, 12),
    idioma: (typeof idiomaActivo === 'function' ? idiomaActivo() : '?'),
    pintados: document.querySelectorAll('[data-i18n-puesto]').length,
  });
}, 500);
</script>
"""
        pagina = html().replace("</head>", stub + "</head>")
        pagina = pagina.replace("</body>", volcado + "</body>")
        pagina = (pagina.replace('src="/static/', 'src="')
                        .replace('href="/static/', 'href="'))
        tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                          encoding="utf-8",
                                          dir=str(APP_DIR / "static"))
        tmp.write(pagina)
        tmp.close()
        try:
            dom = subprocess.run(
                [CHROME, "--headless", "--disable-gpu",
                 "--allow-file-access-from-files", "--dump-dom",
                 "--virtual-time-budget=6000", tmp.name],
                capture_output=True, text=True, timeout=180).stdout
        finally:
            os.unlink(tmp.name)
        m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
        if not m:
            raise unittest.SkipTest("Chrome no devolvió el volcado")
        import html as _h
        return json.loads(_h.unescape(m.group(1)))

    def test_carga_sin_un_solo_error_de_js(self):
        for idioma in IDIOMAS:
            with self.subTest(idioma=idioma):
                self.assertEqual(self.datos[idioma]["errores"], [],
                                 f"errores de JS con la app en `{idioma}`")

    def test_el_idioma_activo_es_el_que_se_pidio(self):
        for idioma in IDIOMAS:
            with self.subTest(idioma=idioma):
                self.assertEqual(self.datos[idioma]["idioma"], idioma)

    def test_no_se_pide_ninguna_clave_que_no_existe(self):
        """Una clave ausente se ve en pantalla: `tr()` devuelve la clave."""
        for idioma in IDIOMAS:
            with self.subTest(idioma=idioma):
                self.assertEqual(self.datos[idioma]["ausentes"], [],
                                 f"claves pedidas y no encontradas en `{idioma}`")

    def test_no_queda_ninguna_clave_cruda_en_pantalla(self):
        """Por si `tr()` devolvió la clave y nadie miró `clavesAusentes()`."""
        for idioma in IDIOMAS:
            with self.subTest(idioma=idioma):
                self.assertEqual(self.datos[idioma]["crudas"], [],
                                 f"claves sin resolver visibles en `{idioma}`")

    def test_se_pintan_los_textos_del_marcado(self):
        """Un `pintarTextos` que no corra dejaría la pantalla vacía y los
        otros tests pasarían: no hay clave cruda porque no hay nada."""
        for idioma in IDIOMAS:
            with self.subTest(idioma=idioma):
                self.assertGreater(self.datos[idioma]["pintados"], 200)


if __name__ == "__main__":
    unittest.main()


# Un valor de CSS tiene la misma pinta que un rótulo para cualquier heurística
# de palabras: `font-size:11px; color:var(--text-3)` lleva `color`, que está en
# el catálogo. Lo que los separa es la sintaxis, no el vocabulario.
_ES_CSS = re.compile(r"var\(--|[;{}]|^[a-z-]+:\s*\S+$|^[a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)+$")


class TestNingunaCadenaCastellanaSeCuelaPorUnHueco(unittest.TestCase):
    """Una cadena entrecomillada dentro de un `${…}` acaba en el HTML.

    El guard de plantillas mira los nodos de texto y los atributos del
    marcado; una ternaria DENTRO de un hueco no es ninguna de las dos cosas y
    se le escapaba:

        <div class="dv-viz-caption">${tr('tab2.l8_escala')}${
            tieneLuz ? ' · validado film completo' : ' · sample 30s'}</div>

    El rótulo está traducido y el sufijo no, así que con la app en inglés
    salía media frase en cada idioma. Tampoco lo veían los otros dos: `es_frase`
    pide un acento o una palabra función —`validado film completo` no tiene
    ninguna de las dos— y el guard de fragmentos cortos exige un hueco, y aquí
    la cadena ES el contenido del hueco.

    El criterio es el de los cortos: una palabra de cuatro letras que YA está
    traducida en otra clave. Si la palabra es nuestra y está traducida en otro
    sitio, aquí también.
    """

    @classmethod
    def setUpClass(cls):
        es = json.loads(
            (APP_DIR / "static" / "i18n" / "es.json").read_text(encoding="utf-8"))
        cls.vocabulario = {w.lower() for v in es.values()
                           for w in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{4,}", v)}
        cls.valores = {" ".join(v.split()) for v in es.values()}
        cls.valores |= {" ".join(_HUECO.sub(" ⟦⟧ ", v).split()) for v in es.values()}

    @staticmethod
    def _cadenas_de_huecos(cont: str):
        """Las cadenas entrecomilladas de cada `${…}`, a cualquier hondura."""
        for ini, fin in captura.huecos_de(cont):
            expr = cont[ini:fin]
            for m in re.finditer(
                    r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"", expr):
                yield (m.group(1) or m.group(2) or "")
            for _, _, dentro in captura.regiones_de_plantilla(expr):
                yield from TestNingunaCadenaCastellanaSeCuelaPorUnHueco \
                    ._cadenas_de_huecos(dentro)

    def test_ninguna_cadena_de_un_hueco_se_queda_en_castellano(self):
        fuera = []
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            for ini, _, cont in captura.regiones_de_plantilla(src):
                fn = TestNoQuedaNingunFragmentoCortoSuelto._funcion_de(src, ini)
                if fn in VOLCADOS_DE_DIAGNOSTICO or fn in CORTOS_ACEPTADOS:
                    continue
                for s in self._cadenas_de_huecos(cont):
                    norm = " ".join(s.split())
                    if (not norm or " " not in norm or len(norm) > 180
                            or "<" in norm or "${" in norm
                            or norm in self.valores or _ES_CSS.search(norm)
                            or _NO_ES_TEXTO.search(norm)):
                        continue
                    palabras = [w.lower() for w in
                                re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{4,}", norm)
                                if not _TECNICO.fullmatch(w)]
                    if not any(w in self.vocabulario for w in palabras):
                        continue
                    linea = src[:ini].count("\n") + 1
                    fuera.append(f"{Path(r).name}:{linea} ({fn}): {norm[:62]}")
        fuera = sorted(set(fuera))
        self.assertEqual(fuera, [], (
            f"\n{len(fuera)} cadena(s) castellanas dentro de un `${{…}}` de "
            f"plantilla.\nPásalas por `tr()` —o une la ternaria en dos claves "
            f"completas—:\n  · " + "\n  · ".join(fuera[:20])))
