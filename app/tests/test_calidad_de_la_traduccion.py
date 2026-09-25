"""La traducción no solo tiene que estar: tiene que ser buena.

Los doce guards de `test_i18n_completo` y `test_plantillas_del_js` validan
**presencia** —que no quede castellano, que no salga una clave, que las tres
lenguas cuadren— y con todos en verde el usuario leyó la cabecera en inglés y
encontró en dos segundos «DV+HDR X-ray», calco de «radiografía», que en
inglés es la placa del hospital.

La causa es estructural: el catálogo se tradujo **clave por clave y en orden
alfabético**, y una cadena aislada no se puede juzgar. Lo que arregla eso es
una revisión por pantalla (`revision.py` la hace posible); lo que **evita que
vuelva** es este fichero, con los defectos que sí se pueden detectar.

Lo que NO está aquí, a propósito: un detector de «el mismo término castellano
traducido de dos formas» por n-gramas. Se escribió y se midió — **252
candidatos con ~5 reales**: marca como disidente toda reestructuración
legítima («Series name» por «Nombre de la serie» es MEJOR inglés, no un
defecto). Un guard con esa precisión se desactiva a la semana.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_calidad_de_la_traduccion -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))


def _catalogos():
    for donde, base in (("frontend", APP_DIR / "static" / "i18n"),
                        ("backend", APP_DIR / "i18n")):
        yield donde, {l: json.loads((base / f"{l}.json").read_text(encoding="utf-8"))
                      for l in ("es", "en", "ca")}


# Calcos y registro. Cada entrada con el motivo, porque el motivo es el guard:
# sin él, el siguiente que lea esto no sabe si borrar la línea.
TRAMPAS = {
    r"\bX-ray\b":
        "calco de «radiografía», que en inglés es la placa del hospital; "
        "aquí es figurado y se dice «deep dive»",
    r"\bplease\b|\bwe are sorry\b|\bsorry\b":
        "cortesía ornamental — el REGISTRO la prohíbe en las tres lenguas",
    r"\bsuccessfully\b":
        "cortesía ornamental: «completed successfully» dice lo mismo que "
        "«complete» y suena a asistente",
    r"\w+ included\)":
        "«(X included)» se lee como «pilas incluidas» — que X viene con la app",
    # El tramo entre los dos «of the» no puede llevar dígitos ni paréntesis:
    # `max(40% of the median, 25% of the maximum)` es una FÓRMULA, y ahí el
    # encadenado es correcto en inglés. Sin ese matiz el guard señalaba a la
    # única cadena del catálogo que no podía escribirse de otra forma.
    r"\bof the\b[^.;()\d]{1,40}\bof the\b":
        "encadenado castellano («de la … del …»): en inglés el posesivo va "
        "delante y la frase se acorta",
    r"\bactualy\b|\brealize\b|\beventually\b|\bassist\b":
        "falsos amigos de «actualmente», «realizar», «eventualmente», «asistir»",
    r"\bintroduce (your|the) \w*(key|url|title)\b":
        "calco de «introducir» (teclear): en inglés es «enter»",
    r"\d\s%":
        "en inglés no va espacio antes del «%» (en castellano y catalán sí)",
}


class TestLaMismaFraseSeTraduceIgual(unittest.TestCase):
    """Dos claves con el MISMO castellano tienen la misma traducción.

    Es la versión sin heurística del detector de consistencia: los cruces
    de término que la lectura por pantalla destapó —«cercar» contra
    «buscar», «memòria cau» contra «cache»— son todos este caso, y aquí no
    hay que adivinar nada.

    **Los huecos se normalizan antes de comparar**, y ahí está el caso que
    lo motivó: `cmv40.validacion_previa` y `cmv40.validacion_previa_de`
    dicen los dos «Validación previa · …» y en inglés decían «Pre-flight
    check» y «Pre-check». Con el nombre del hueco dentro
    (`{id}` vs `{que}`) el castellano no coincide byte a byte y la
    comparación exacta no los veía — que es justo el agujero por el que
    este guard no existía todavía aunque CLAUDE.md lo diera por escrito.
    """

    @staticmethod
    def catalogos():
        return _catalogos()

    @staticmethod
    def _sin_huecos(v: str) -> str:
        return " ".join(re.sub(r"\{\w+\}", "⟦⟧", v).split())

    def test_el_mismo_castellano_no_tiene_dos_traducciones(self):
        # Se acumula y se afirma UNA vez: con el `assertEqual` dentro del
        # bucle, el primer par (frontend, en) abortaba y los cruces del
        # backend no se veían — y ahí estaba justo el caso que motivó el
        # guard. Un test que para en el primer hallazgo esconde los demás.
        cruces = []
        for donde, cat in self.catalogos():
            for otra in ("en", "ca"):
                por_es: dict[str, list[tuple[str, str]]] = {}
                for k, es in cat["es"].items():
                    if k not in cat[otra]:
                        continue
                    por_es.setdefault(self._sin_huecos(es), []).append(
                        (k, self._sin_huecos(cat[otra][k])))
                for es, xs in por_es.items():
                    distintas = {t for _, t in xs}
                    if len(distintas) > 1:
                        cruces.append(
                            f"[{donde}/{otra}] «{es[:46]}» → "
                            f"{sorted(distintas)[:2]} "
                            f"({', '.join(k for k, _ in xs[:3])})")
        self.assertEqual(sorted(cruces), [], (
            f"\n{len(cruces)} frase(s) castellanas con DOS traducciones:"
            f"\n  · " + "\n  · ".join(sorted(cruces)[:14])))


class TestElInglesNoLlevaCalcos(unittest.TestCase):

    def test_ninguna_trampa_conocida(self):
        malas = []
        for donde, cat in _catalogos():
            for k, v in cat["en"].items():
                for rx, motivo in TRAMPAS.items():
                    if re.search(rx, v, re.I):
                        malas.append(f"[{donde}] `{k}`: {v[:60]}\n      → {motivo}")
        self.assertEqual(sorted(malas), [], (
            f"\n{len(malas)} calco(s) o fallo(s) de registro en inglés:\n  · "
            + "\n  · ".join(sorted(malas)[:10])))

    def test_las_comillas_angulares_no_viajan_al_ingles(self):
        """`«»` son de castellano y catalán; el inglés usa `""`.

        Eran **15**, y no es cosmético: en una frase inglesa el guillemet se
        lee como una cita de otro idioma.
        """
        malas = [f"[{donde}] `{k}`" for donde, cat in _catalogos()
                 for k, v in cat["en"].items() if "«" in v or "»" in v]
        self.assertEqual(malas, [], "\n  · ".join([""] + malas))


class TestLaOrtografiaInglesaEsUnaSola(unittest.TestCase):
    """`analyze` 58 vs `analyse` 16, `canceled` 3 vs `cancelled` 4.

    La app está en **inglés de EE. UU.**: es la mayoría medida y el
    vocabulario del dominio —`colorist`, `color primaries`, la documentación
    de Dolby— es americano. El `en-GB` de `localeActual()` es solo para las
    FECHAS (día antes del mes, como en las otras dos lenguas) y es otro eje.
    """

    # `analysis`/`analyses` son el SUSTANTIVO y se escriben igual en las dos
    # ortografías: el guard solo mira el verbo. Normalizar sin ese matiz dejó
    # «The analyzes are still saved», que es una falta.
    PARES = [("analyse/analyze", r"\banalys(e|ed|ing)\b", r"\banalyz(e|ed|ing)\b"),
             ("cancel(l)ed",     r"\bcancell(ed|ing)\b",      r"\bcancel(ed|ing)\b"),
             ("behaviour",       r"\bbehaviour\b",            r"\bbehavior\b"),
             ("artefact",        r"\bartefact(s?)\b",         r"\bartifact(s?)\b"),
             ("normalise",       r"\bnormalis\w*\b",          r"\bnormaliz\w*\b")]

    def test_no_se_mezcla_us_con_uk(self):
        malas = []
        for etiqueta, rx_uk, _ in self.PARES:
            for donde, cat in _catalogos():
                for k, v in cat["en"].items():
                    if re.search(rx_uk, v, re.I):
                        malas.append(f"[{donde}] `{k}`: forma británica de {etiqueta}")
        self.assertEqual(sorted(malas), [], (
            "\nla app está en inglés de EE. UU.:\n  · " + "\n  · ".join(sorted(malas)[:10])))


class TestElCatalanNoUsaElGerundioPelado(unittest.TestCase):
    """«Carregant…» es el castellanismo más común del software en catalán.

    Un gerundio no puede encabezar una oración independiente; un rótulo de
    progreso se dice «S'està carregant…» (o «S'estan …» concordando con el
    objeto). Eran **42 de 54**; los otros 12 ya lo hacían bien.
    """

    # El auxiliar concuerda con el objeto, así que no se puede autocorregir:
    # lo que el guard exige es que NO quede un gerundio pelado.
    #
    # El ancla admite lo que va DELANTE del gerundio —una raya de la cortinilla
    # («— Cargando… —»), un marcador del log («📋 Extrayendo…»)— porque con `^`
    # a secas se escapaban **14**, seis de ellos en el log del servidor. Y no se
    # exige `…`: «En cola» es un rótulo de estado sin puntos y el
    # castellanismo es el mismo.
    # `-re` hace falta en `INF_CA` para los infinitivos de esa conjugación
    # («treure», «prendre»), pero deja pasar tres imperativos que acaban
    # igual: «Obre» por «Obrir» era el rótulo del botón «Abrir MKV» y el
    # guard lo daba por bueno. La lista es explícita porque por patrón no se
    # pueden separar — «obre» y «treure» acaban las dos en `re`.
    IMPERATIVO_CA = re.compile(r"^(?:Obre|Obri|Omple|Emple|Cobre)$")

    # Las TRES conjugaciones: `-ant` (cantant), `-ent` (perdent) y **`-int`**
    # (obtenint, escrivint). Sin la tercera se escapaban tres, y no por poco:
    # el guard llevaba desde que se escribió sin mirar una conjugación entera.
    #
    # Y el gerundio no tiene por qué abrir la cadena: va detrás de un prefijo
    # cerrado por raya, punto medio o dos puntos —«Fase A — Analizando…»,
    # «Paso 1/4: Identificando…», «mkvmerge: extrayendo…»—. Con el ancla
    # solo en `^` se escapaban **44**, o sea más que los que el guard había
    # cazado nunca: los doce rótulos de fase de Tab 3 y treinta y dos líneas
    # del log del servidor.
    # Delante del gerundio puede haber un prefijo («Fase A — », «Paso 1/4: »)
    # o simplemente la frase anterior: «Tus proyectos siguen guardados.
    # Reintentando cada 4 s…». Con solo `^` se escapaban 44; sin el punto,
    # dos más.
    ANTES = r"(?:^|[—·:.;!?]\s+)"
    GERUNDIO = re.compile(
        ANTES + r"[^A-Za-zÀÈÉÍÒÓÚ]*[A-ZÀÈÉÍÒÓÚ]?[a-zàèéíòóúïüç·'-]*(ant|ent|int)\b")

    def test_ningun_rotulo_de_progreso_empieza_por_gerundio(self):
        malas = []
        for donde, cat in _catalogos():
            for k, es in cat["es"].items():
                if not re.search(self.ANTES + r"[^A-Za-zÁÉÍÓÚ]*[A-ZÁÉÍÓÚ]?"
                                 r"[a-záéíóúñü-]*(ando|endo)\b", es):
                    continue
                ca = cat["ca"].get(k, "")
                if self.GERUNDIO.search(ca):
                    malas.append(f"[{donde}] `{k}`: {ca[:56]}")
        self.assertEqual(sorted(malas), [], (
            f"\n{len(malas)} rótulo(s) de progreso en catalán con gerundio "
            f"pelado.\nUsa «S'està/S'estan + gerundi», concordando con el "
            f"objeto:\n  · " + "\n  · ".join(sorted(malas)[:10])))

    def test_donde_el_castellano_va_en_infinitivo_el_catalan_tambien(self):
        """Decisión del usuario (2026-09-16): el catalán **sigue al castellano**
        en la forma verbal de los rótulos de acción.

        Softcatalà prescribe el imperativo para los botones y el castellano usa
        el infinitivo, así que las dos convenciones son defendibles — y por eso
        el catálogo tenía las dos: de los 189 rótulos cuyo castellano empieza
        por infinitivo, **101 iban en infinitivo y 88 en imperativo**, sin
        mayoría a la que normalizar. Peor que la forma eran los cruces que
        producía: `core.buscar_pelicula` decía «Cerca la pel·lícula» al lado de
        `core.buscar_la_pelicula_en_tmdb_y` con «Buscar», o sea el mismo verbo
        con dos lexemas.

        El sub-criterio es **espejar también el artículo**: «Limpiar
        artefactos» → «Netejar artefactes», no «Netejar els artefactes». Sin
        eso la decisión no cierra nada, porque el imperativo pedía artículo
        para sonar natural y el infinitivo no.

        Ningún detector podía cazar esto: los 88 imperativos eran catalán
        correcto. Solo se ve leyendo las dos lenguas juntas.
        """
        # El enclítico va aparte: «Seleccionar-ho tot» es infinitivo.
        ENCLITIC = r"(?:-(?:ho|lo|la|los|les|li|ne|hi|me|te|se|nos|vos)|'[nl]|-s)*"
        # El castellano también admite enclítico («Avisarme cuando termine»).
        # La lista deja fuera `-te` y `-os` a propósito: con ellos, «Convierte»
        # y «Ficheros» pasan por infinitivos y el guard señala seis rótulos que
        # no tienen nada que ver.
        INF_ES = re.compile(r"^[A-ZÁÉÍÓÚ][a-záéíóúñü]+(?:ar|er|ir)"
                            r"(?:me|se|lo|la|los|las|le|les|nos)?\b")
        INF_CA = re.compile(r"^[A-ZÀÈÉÍÒÓÚ][a-zàèéíòóúïüç·]*(?:ar|er|ir|re)"
                            + ENCLITIC + r"$")
        # `-re` hace falta para los infinitivos de esa conjugación («treure»,
        # «prendre»), pero deja pasar tres imperativos que acaban igual:
        # «Obre» por «Obrir» era el rótulo del botón «Abrir MKV», y el guard
        # lo daba por bueno. La lista es explícita porque separarlos por
        # patrón no se puede — «obre» y «treure» terminan las dos en `re`.
        assert self.IMPERATIVO_CA
        malas = []
        for donde, cat in _catalogos():
            for k, es in cat["es"].items():
                # Una PREGUNTA no es un rótulo de acción: los doce títulos de
                # confirmación dicen «Vols …?», y el castellano los escribe
                # con infinitivo («¿Borrar {n} elementos?»). El «¿» de
                # apertura no sirve para distinguirlos porque a uno le falta;
                # lo que sí, el cierre.
                if not INF_ES.match(es) or es.rstrip().endswith("?"):
                    continue
                ca = cat["ca"].get(k, "")
                if not ca:
                    continue
                primera = ca.split()[0].rstrip(":,.…—")
                if (not INF_CA.match(primera)
                        or self.IMPERATIVO_CA.match(primera)):
                    malas.append(f"[{donde}] `{k}`: {es[:34]} → {ca[:34]}")
        self.assertEqual(sorted(malas), [], (
            f"\n{len(malas)} rótulo(s) con el castellano en infinitivo y el "
            f"catalán en imperativo;\nel catalán sigue al castellano (ver "
            f"REGISTRO.md):\n  · " + "\n  · ".join(sorted(malas)[:10])))

    def test_el_articulo_de_rpu_se_apostrofa(self):
        """En catalán «el RPU» es «l'RPU»: la sigla empieza por vocal. Eran 104.

        Las contracciones cuentan igual y no estaban: ante vocal la
        preposición NO se contrae con el artículo, así que es «de l'RPU» y no
        «del RPU» — eran **17** más, once en la interfaz. Misma regla para
        «pel» → «per l'».
        """
        malas = [f"[{donde}] `{k}`" for donde, cat in _catalogos()
                 for k, v in cat["ca"].items()
                 if re.search(r"\b[Ee]l RPU\b|\bde el RPU\b|\bal RPU\b"
                              r"|\bdel RPU\b|\bpel RPU\b", v)]
        self.assertEqual(malas, [], "\n  · ".join([""] + malas[:10]))


if __name__ == "__main__":
    unittest.main()


class TestLaPreposicionYElArticuloContraen(unittest.TestCase):
    """«extraídos de el MPLS del episodio», que el usuario leyó en la ficha
    de un proyecto antiguo el 2026-09-17.

    No era un defecto del i18n: el fuente de `pre-i18n` ya componía
    `f"{n} capítulos extraídos de {ep_origin_label}"` con
    `ep_origin_label = "el MPLS del episodio"`. La causa es que **el
    fragmento lleva el artículo y la plantilla lleva la preposición**, así
    que se encuentran sin contraer — y no se arregla moviendo la
    preposición al fragmento, porque el mismo fragmento se usaba con `de` y
    con `en`/`a`, que contraen distinto. La salida es una clave completa por
    (mensaje × origen), igual que con los plurales irregulares.

    El guard mide las composiciones REALES: recorre el AST del servidor
    buscando un `tr()` cuyo parámetro con nombre sea otro `tr()`, renderiza
    el par y busca la contracción perdida. No es una lista de sitios, así
    que un mensaje nuevo con la misma forma lo caza igual.

    Solo cuentan las que la lengua obliga a contraer:
      · castellano — `de el` → del · `a el` → al
      · catalán    — `de el` → del · `a el` → al · `de els`/`a els` ·
                     `per el` → pel
    `de los` **no contrae** en castellano, y darlo por error es lo que hacía
    que el detector señalara «merge selectivo de los levels», que está bien.
    """

    _MAL = {"es": r"\b(?:de|a)\s+el\b",
            "ca": r"\b(?:de|a|per)\s+(?:el|els)\b"}

    @staticmethod
    def _claves_de(n) -> list[str]:
        """Las claves que un nodo puede resolver: un `tr()` o una ternaria."""
        import ast
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "tr" and n.args
                and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)):
            return [n.args[0].value]
        if isinstance(n, ast.IfExp):
            return (TestLaPreposicionYElArticuloContraen._claves_de(n.body)
                    + TestLaPreposicionYElArticuloContraen._claves_de(n.orelse))
        return []

    def _composiciones(self) -> set[tuple[str, str, str, str]]:
        """`(clave externa, parámetro, clave interna, dónde)` del servidor.

        Solo composiciones DIRECTAS: el valor del keyword ES un `tr()`, o una
        ternaria de `tr()`, o una variable a la que se asignó una de las dos.
        Bajar por todo el subárbol cruzaría claves que no se juntan nunca.
        """
        import ast
        pares: set[tuple[str, str, str, str]] = set()
        for f in sorted(APP_DIR.rglob("*.py")):
            if "tests" in f.parts or "__pycache__" in str(f):
                continue
            try:
                arbol = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            var: dict[str, list[str]] = {}
            for n in ast.walk(arbol):
                if (isinstance(n, ast.Assign) and len(n.targets) == 1
                        and isinstance(n.targets[0], ast.Name)):
                    if ks := self._claves_de(n.value):
                        var.setdefault(n.targets[0].id, []).extend(ks)
            for n in ast.walk(arbol):
                if not isinstance(n, ast.Call):
                    continue
                for ext in self._claves_de(n):
                    for kw in n.keywords:
                        dentro = (var.get(kw.value.id, [])
                                  if isinstance(kw.value, ast.Name)
                                  else self._claves_de(kw.value))
                        for ic in dentro:
                            pares.add((ext, str(kw.arg), ic,
                                       f"{f.name}:{n.lineno}"))
        return pares

    def test_ninguna_composicion_deja_la_contraccion_sin_hacer(self):
        cat = {l: {} for l in ("es", "ca")}
        for _, porl in _catalogos():
            for l in cat:
                cat[l].update(porl[l])
        pares = self._composiciones()
        self.assertGreater(len(pares), 20, "el AST no encontró composiciones")
        fuera = []
        for ext, param, ic, donde in sorted(pares):
            for l in ("es", "ca"):
                pl, fr = cat[l].get(ext), cat[l].get(ic)
                hueco = "{" + param + "}"
                if not (pl and fr) or hueco not in pl:
                    continue
                r = pl.replace(hueco, fr)
                if re.search(self._MAL[l], r, re.I):
                    fuera.append(f"{donde} [{l}] {ext} ← {ic}: {r[:70]}")
        self.assertEqual(fuera, [], (
            f"\n{len(fuera)} composición(es) con la contracción sin hacer. El "
            f"hueco no puede llevar dentro el artículo: parte el mensaje en "
            f"una clave por origen.\n  · " + "\n  · ".join(fuera[:12])))
