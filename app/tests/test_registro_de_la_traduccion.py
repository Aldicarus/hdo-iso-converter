"""Las reglas de `app/i18n/REGISTRO.md`, ejecutadas.

Una guía de estilo que nadie comprueba es una intención. Aquí se comprueba lo
que se puede comprobar de verdad —los términos que no se traducen, los markers
que son contrato, las claves que faltan, los parámetros que no cuadran— y se
deja fuera lo que no (que las tres lenguas *digan* lo mismo no lo puede saber
un test; eso lo sostiene la guía y quien traduce).

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_registro_de_la_traduccion -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

IDIOMAS = ("es", "en", "ca")
DIR_FRONT = APP_DIR / "static" / "i18n"
DIR_BACK = APP_DIR / "i18n"

# Términos que NO se traducen en ninguna lengua. Si aparecen en el castellano
# de una clave, tienen que aparecer igual en el inglés y en el catalán.
#
# Son de tres familias, y las tres por el mismo motivo: no son palabras, son
# nombres. Traducir `Season` rompe el scraper de Plex; traducir `TrueHD Atmos`
# inventa un codec; traducir `remux` cambia el registro de quien habla de vídeo
# al de un manual (la regla que pidió el usuario para el catalán).
GLOSARIO = (
    # convención de Plex/Jellyfin — traducirlo rompe el scraper
    "Season",
    # tags del nombre del fichero
    "[DV FEL]", "[Audio DCP]", "[CMv4 FULL]", "[CMv4 CORE+]", "[CMv4 CORE]",
    # nombres comerciales de codec y formato
    "TrueHD Atmos", "DD+ Atmos", "DTS-HD MA", "Dolby Vision", "Blu-ray",
    "HDR10", "PGS", "HEVC", "MKV", "ISO", "BDMV", "M2TS", "RPU",
    # herramientas
    "mkvmerge", "mkvpropedit", "mkvextract", "mediainfo", "ffmpeg", "dovi_tool",
    # jerga del oficio, invariable también en catalán
    "remux", "demux", "playlist", "drop-in", "merge", "bin",
    # el sufijo que además va siempre en la pista castellana (decisión 5)
    "(DCP 9.1.6)",
)

# Markers del log: son claves del parser del frontend y de
# `_CMV40_LOG_FORCE_PERSIST_MARKERS`. Se traduce lo que va DETRÁS, así que no
# pueden estar dentro de una cadena traducible.
MARKERS = ("━━━", "✓ Fase", "✗ Fase", "📋 Plan", "🎯 Resultado",
           "🛑 Cancelado", "§§PROGRESS§§", "Progress:")

_PARAM = re.compile(r"\{(\w+)\}")

# Claves cuyo inglés ES el castellano, y con motivo. Cada una está aquí porque
# traducirla sería un defecto, no una mejora.
#
# **Por clave y no ampliando `GLOSARIO`**, y la razón es concreta: el glosario
# exige el término en las TRES lenguas, y dos de estas (`Audio`, `Generated`) sí
# se traducen al catalán (`Àudio`, `Generat`). Añadirlas al glosario prohibiría
# esa traducción correcta. Y meter `Min`, `Path` o `Stream` como términos
# globales relajaría la comprobación en las otras 600 cadenas, donde esas
# palabras sí aparecen dentro de prosa traducible.
# Frases que llevan un NOMBRE dentro, así que su mayúscula no es Title Case.
# Se exime la clave, no el patrón: un nombre nuevo tiene que costar escribir el
# motivo. Lo cruza `TestElEstiloSeSostiene` contra las claves reales.
NOMBRE_PROPIO = {
    "phase_a.dolby_vision_detectado_profile_cm":
        "«Dolby Vision» y «Profile» son nombres: los tres únicos candidatos de "
        "la frase, así que la heurística la toma por Title Case",
    "ui.solo_consultar_editar_mkv":
        "los tres filtros de la columna nombran las tres pestañas, y «Inspect /"
        " Edit MKV» es el nombre de una de ellas",
}

IGUAL_EN_INGLES = {
    # Ya están en inglés en el original: son cabeceras de tabla y leyendas de
    # gráfico que el castellano nunca tradujo.
    "core.audio":            "cabecera de pista; en catalán sí se traduce",
    "tab2.stream":           "cabecera de la columna de MediaInfo",
    "tab2.frames":           "cabecera de tabla",
    "tab3.frames":           "etiqueta de dato",
    "tab3.path":             "etiqueta de dato",
    "tab3.generated":        "procedencia del bin; en catalán sí se traduce",
    "tab3.bd_source":        "cabecera; su pareja es `Bin (target)` y se leen en paralelo",
    "tab3.bin_target":       "cabecera; su pareja es `BD (source)`",
    # Nombres de campo del RPU, tal como los emite dovi_tool.
    "tab2.peak":             "nombre de la serie L1, como lo llama dovi_tool",
    "tab2.avg":              "nombre de la serie L1 media, como lo llama dovi_tool",
    "tab2.peak_max_pq":      "nombre del campo del RPU entre paréntesis",
    "tab2.avg_avg_pq":       "nombre del campo del RPU entre paréntesis",
    "tab2.min_min_pq":       "nombre del campo del RPU entre paréntesis",
    "tab2.scene_cuts":       "nombre del campo del RPU",
    "tab2.master_display":   "nombre del campo del RPU (L9), cabecera de la tarjeta; su pareja es `Container HEVC`",
    "comun.master_p1_nits":  "`Master` es el campo L9 y `nits` la unidad: la "
                             "frase entera es nomenclatura",
    "tab2.master_p1_n":      "igual, con la `n` abreviada del gráfico",
    "comun.error_p1":        "«Error» se escribe igual en las dos lenguas",
    # Rótulos de la radiografía DV+HDR que en castellano YA están en inglés o
    # en notación de la spec, así que no hay nada que traducir. Varios se
    # habían «traducido» expandiéndolos, y eso rompía la simetría con sus
    # hermanos de la misma tabla o del mismo gráfico.
    "tab2.active_area":     "el castellano ya usa el término inglés del bloque L5",
    "tab2.ratio":           "cabecera de columna estrecha; el bloque que la contiene ya dice de qué ratio habla, igual que en castellano",
    "tab2.offsets_px":      "notación de la tabla de active area; sus hermanas son «Offsets L / R» y «Offsets T / B»",
    "tab2.min":             "una de las tres series del sparkline, junto a «peak» y «avg»",
    "tab2.midtone_100_300n": "bucket de brillo con su umbral; su pareja es «Highlight ≥300n»",
    "tab2.percentiles_dv_l1_max_pq": "«Percentiles» más el nombre del campo del RPU",
    "tab2.dv_target_display_p1": "nombre del bloque L10 tal como lo emite dovi_tool",
    "tab2.rec_2020":        "nombre del estándar; el castellano alterna «Rec.2020» y «BT.2020» y aquí dice el primero",
    "tab1.log":             "«Log» se escribe igual en las dos lenguas",
    "tab2.sdr_like":        "nombre acuñado del bucket de escenas por debajo de 100 nits: «SDR-like», no «rango SDR»",
    # Unidades y estándares: no se traducen en ninguna lengua.
    "tab2.nits":             "unidad de luminancia",
    "tab2.highlight_300n":   "clasificación con su umbral en nits",
    "tab2.dci_p3":           "nombre del espacio de color",
    "tab2.rec_709":          "nombre del estándar",
    "tab3.sha_256":          "nombre del algoritmo",
    # Nombres propios y tokens literales.
    "tab3.imdb":             "nombre propio",
    # ── Del catálogo del backend ──────────────────────────────────────
    # Etiquetas que el log ya escribía en inglés, y un comando.
    "cmv40_pipeline.source":
        "etiqueta del log, ya en inglés; su pareja es `target` y se leen "
        "juntas en «Sampling source frames»",
    "cmv40_pipeline.target":
        "etiqueta del log, ya en inglés; pareja de `source`",
    "phase_a.dolby_vision_profile_cm":
        "no queda ni una palabra de lengua común: todo es glosario o nombre "
        "de campo de dovi_tool",
    "phase_a.ffprobe_packet_count_timeout_10_min":
        "la línea ya estaba entera en inglés en el original",
    "tab1.mount_t_udf_o_ro_loop":
        "es el comando que se ejecuta, no una frase",
    # ── Los rótulos que ya nacieron en inglés, y siguen igual en catalán
    #    porque son el registro técnico con el que se lee el log y la hoja de
    #    DoviTools. Traducirlos al catalán daría un término que nadie usa.
    "tab2.aspect_ratio":     "nomenclatura de vídeo; se dice igual en las tres",
    "tab2.cm_version":       "nombre del campo del RPU, como lo emite dovi_tool",
    "tab2.enhancement_layer": "nombre de la capa en la spec de Dolby Vision",
    "tab2.hdr10_metadata":   "nombre del estándar más la palabra que lo acompaña",
    "tab2.l11_content_type": "nombre del bloque del RPU y de su campo",
    "tab2.offsets_l_r":      "campo del L5 del RPU, con las iniciales del lado",
    "tab2.offsets_t_b":      "campo del L5 del RPU, con las iniciales del lado",
    "tab2.track_id_p1":      "`Track ID` es como lo llama mkvmerge en su salida",
    "tab3.timecode":         "unidad de tiempo del vídeo, igual en las tres",
    "tab3.gates":            "el nombre de los trust gates en la spec de la app; el log y la hoja de DoviTools los llaman así",
    "cmv40_pipeline.rpu_frames":
        "solo huecos más «RPU» y «frames», que son glosario: no hay nada que traducir",
    "tab1.audio":
        "solo huecos más «Audio», que se escribe igual en las dos lenguas",
    "tab3.auto_trusted":     "«Auto» es el rótulo del interruptor y «trusted» el estado del bin en la spec; ninguno de los dos se traduce",
    "tab3.lbl_incompatible": "«incompatible» se escribe igual en las dos lenguas",
    "tab3.trusted":          "el estado del bin en la spec de trust de la app; su pareja es `Sin trust automático`, que sí se traduce",
    "tab3.auto_on":          "el rótulo del interruptor del auto-pipeline",
    "tab3.auto_off":         "el rótulo del interruptor del auto-pipeline",
    "tab1.col_match":        "cabecera de la columna de confianza del match; el castellano ya usa la palabra inglesa",
    # ── Y tres que son la misma palabra en inglés.
    "tab3.total":            "«Total» se escribe igual en las dos lenguas",
    "tab1.def":              "el badge del flag default de una pista: son tres letras en un chip, y «Default» no cabe",
    "tab1.frc":              "el badge del flag forced: mismo caso que `tab1.def`",
    "tab1.poster":           "«poster» se escribe igual en inglés; en catalán sí lleva acento",
    "settings.paypal_rec_9999": "un nombre propio y un alias: no hay nada que traducir",
}


def _cargar(directorio: Path) -> dict[str, dict[str, str]]:
    return {i: json.loads((directorio / f"{i}.json").read_text(encoding="utf-8"))
            for i in IDIOMAS}


class CatalogoCase(unittest.TestCase):
    """Los dos catálogos —frontend y backend— pasan las mismas reglas."""

    def catalogos(self):
        yield "frontend", _cargar(DIR_FRONT)
        yield "backend", _cargar(DIR_BACK)


class TestLosTresCatalogosCuadran(CatalogoCase):

    def test_existen_los_tres_ficheros_en_los_dos_sitios(self):
        for d in (DIR_FRONT, DIR_BACK):
            for i in IDIOMAS:
                self.assertTrue((d / f"{i}.json").exists(), f"falta {d.name}/{i}.json")

    def test_ninguna_lengua_se_queda_sin_claves_del_castellano(self):
        """El castellano es el original: define el conjunto de claves."""
        for donde, cat in self.catalogos():
            for otra in ("en", "ca"):
                faltan = sorted(set(cat["es"]) - set(cat[otra]))
                self.assertEqual(faltan, [], (
                    f"[{donde}] {len(faltan)} clave(s) sin traducir a "
                    f"`{otra}`: {faltan[:10]}"))

    def test_ninguna_lengua_tiene_claves_inventadas(self):
        """Una clave que solo existe en inglés no la pide nadie: es basura."""
        for donde, cat in self.catalogos():
            for otra in ("en", "ca"):
                sobran = sorted(set(cat[otra]) - set(cat["es"]))
                self.assertEqual(sobran, [],
                                 f"[{donde}] claves en `{otra}` que no están "
                                 f"en castellano: {sobran[:10]}")

    def test_los_parametros_son_los_mismos_en_las_tres(self):
        """El orden puede cambiar; el conjunto no.

        Un `{max}` que se pierde al traducir deja el número fuera del mensaje,
        y un `{mx}` mal escrito lo deja crudo en pantalla. Ninguno de los dos
        falla en ninguna otra parte.
        """
        for donde, cat in self.catalogos():
            for clave, es in cat["es"].items():
                esperados = set(_PARAM.findall(es))
                for otra in ("en", "ca"):
                    if clave not in cat[otra]:
                        continue
                    hay = set(_PARAM.findall(cat[otra][clave]))
                    self.assertEqual(hay, esperados, (
                        f"[{donde}] `{clave}` en `{otra}` usa {sorted(hay)} y "
                        f"el castellano {sorted(esperados)}"))


class TestElGlosarioSeRespeta(CatalogoCase):

    def test_los_terminos_del_glosario_no_se_traducen(self):
        fallos = []
        for donde, cat in self.catalogos():
            for clave, es in cat["es"].items():
                for termino in GLOSARIO:
                    if termino not in es:
                        continue
                    for otra in ("en", "ca"):
                        if clave in cat[otra] and termino not in cat[otra][clave]:
                            fallos.append(f"[{donde}] `{clave}` ({otra}): "
                                          f"falta «{termino}»")
        self.assertEqual(fallos, [], "\n  · ".join([""] + fallos[:12]))

    def test_ningun_marker_del_log_vive_dentro_de_una_cadena_traducible(self):
        """Se traduce lo que va detrás del marker, no el marker.

        Si un marker entra en el catálogo, la traducción puede cambiarlo y
        entonces el parser del frontend deja de reconocer la fase y
        `_CMV40_LOG_FORCE_PERSIST_MARKERS` deja de persistir la línea. Ninguna
        de las dos cosas da un error: simplemente dejan de pasar.
        """
        fallos = []
        for donde, cat in self.catalogos():
            for idioma in IDIOMAS:
                for clave, txt in cat[idioma].items():
                    for m in MARKERS:
                        if m in txt:
                            fallos.append(f"[{donde}] `{clave}` ({idioma}) "
                                          f"contiene el marker «{m}»")
        self.assertEqual(fallos, [], "\n  · ".join([""] + fallos[:12]))


class TestElEstiloSeSostiene(CatalogoCase):

    def test_el_ingles_no_usa_title_case_en_frases(self):
        """Sentence case, no Title Case: lo dice la guía.

        Se mira solo en frases de 3+ palabras: «New Project» es Title Case,
        pero «Dolby Vision» o «English Full» son nombres propios y etiquetas.
        """
        sospechosas = []
        for donde, cat in self.catalogos():
            for clave, en in cat["en"].items():
                if clave in NOMBRE_PROPIO:
                    continue
                palabras = [p for p in en.split() if p.isalpha() and len(p) > 3]
                if len(palabras) < 3:
                    continue
                mayus = [p for p in palabras[1:] if p[0].isupper()]
                if len(mayus) >= len(palabras) - 1:
                    sospechosas.append(f"[{donde}] `{clave}`: {en[:60]}")
        self.assertEqual(sospechosas, [],
                         "posible Title Case:\n  · " + "\n  · ".join(sospechosas[:8]))

    def test_no_hay_cortesia_ornamental(self):
        """«por favor» / «please» / «lo sentimos»: la app no habla así."""
        prohibidas = ("por favor", "please", "lo sentimos", "we are sorry",
                      "disculpe", "kindly")
        fallos = []
        for donde, cat in self.catalogos():
            for idioma in IDIOMAS:
                for clave, txt in cat[idioma].items():
                    bajo = txt.lower()
                    for p in prohibidas:
                        if p in bajo:
                            fallos.append(f"[{donde}] `{clave}` ({idioma}): «{p}»")
        self.assertEqual(fallos, [], "\n  · ".join([""] + fallos[:10]))

    def test_la_lista_blanca_no_se_podre(self):
        """Una entrada que ya no aplica esconde el siguiente descuido.

        Si alguien traduce de verdad una de estas —o le cambia el castellano—
        la excepción deja de tener sentido y hay que quitarla. Igual que con
        las excepciones del golden del castellano.
        """
        sobran = []
        for donde, cat in self.catalogos():
            for clave in IGUAL_EN_INGLES:
                if clave not in cat["es"]:
                    continue
                if cat["en"].get(clave) != cat["es"][clave]:
                    sobran.append(f"[{donde}] `{clave}` ya está traducida")
        self.assertEqual(sobran, [], "\n  · ".join([""] + sobran))

    def test_cada_nombre_propio_existe_y_lleva_motivo(self):
        """Una exención que ya no apunta a ninguna clave parece cobertura."""
        claves = set()
        for _, cat in self.catalogos():
            claves |= set(cat["es"])
        fantasmas = sorted(k for k in NOMBRE_PROPIO if k not in claves)
        self.assertEqual(fantasmas, [], f"NOMBRE_PROPIO sin clave real: {fantasmas}")
        flojas = [k for k, v in NOMBRE_PROPIO.items() if len(v.strip()) < 12]
        self.assertEqual(flojas, [], f"sin explicar por qué: {flojas}")

    def test_cada_entrada_de_la_lista_blanca_existe(self):
        claves = set()
        for _, cat in self.catalogos():
            claves |= set(cat["es"])
        fantasmas = sorted(k for k in IGUAL_EN_INGLES if k not in claves)
        self.assertEqual(fantasmas, [],
                         f"entradas de IGUAL_EN_INGLES sin clave real: {fantasmas}")

    def test_cada_excepcion_lleva_su_motivo(self):
        flojas = [k for k, v in IGUAL_EN_INGLES.items() if len(v.strip()) < 12]
        self.assertEqual(flojas, [], f"sin explicar por qué no se traduce: {flojas}")

    def test_una_cita_a_un_rotulo_usa_el_rotulo_traducido(self):
        """«Pulsa "Abrir MKV"» tiene que citar el botón, no otra frase.

        Es el defecto que hace que una app parezca rota: el mensaje manda a
        pulsar algo que no se llama así. En castellano no puede pasar —el
        autor copia el rótulo— y al traducir sí, porque el rótulo y su cita
        viven en dos claves distintas y se traducen por separado. Eran cinco:
        «Follow the progress in the *Running jobs* panel» cuando el panel dice
        «Jobs in progress»; «Skip existing» cuando el botón dice «Skip the
        existing ones»; y en catalán «Prem "Obrir MKV"» cuando el botón dice
        «Obre MKV».

        El criterio es exacto, no heurístico: se buscan las citas cuyo texto
        castellano COINCIDE con el valor de otra clave —o sea, un rótulo real
        de la app— y se exige que la traducción contenga la traducción de ese
        rótulo.
        """
        CITA = re.compile(r'"([^"{}<>]{3,40})"|«([^»{}<>]{3,40})»'
                          r'|<strong>([^<{}]{3,40})</strong>')
        rotulos = {}
        for _, cat in self.catalogos():
            for k, v in cat["es"].items():
                v = v.strip()
                if 3 <= len(v) <= 40 and "{" not in v and "<" not in v:
                    rotulos.setdefault(v, (cat, k))
        malas = []
        for _, cat in self.catalogos():
            for k, es in cat["es"].items():
                for m in CITA.finditer(es):
                    cita = (m.group(1) or m.group(2) or m.group(3)).strip()
                    if cita not in rotulos:
                        continue
                    cat_r, k_r = rotulos[cita]
                    if k_r == k:
                        continue
                    for l in ("en", "ca"):
                        rotulo = cat_r[l].get(k_r, "").strip()
                        if rotulo and rotulo not in cat[l].get(k, ""):
                            malas.append(
                                f"[{l}] `{k}` cita «{cita}», cuyo rótulo "
                                f"traducido es «{rotulo}»")
        malas = sorted(set(malas))
        self.assertEqual(malas, [], (
            "\nestas citas no usan el rótulo traducido:\n  · "
            + "\n  · ".join(malas[:10])))

    def test_ninguna_traduccion_se_ha_quedado_en_castellano(self):
        """Copiar el castellano en `en.json` para «rellenar» pasaría los otros
        tests y dejaría la app a medio traducir sin que nada avise.

        Se exige que difiera, salvo cuando la frase es SOLO glosario —
        «Dolby Vision», «RPU» o una cifra son iguales en las tres lenguas.
        """
        iguales = []
        for donde, cat in self.catalogos():
            for clave, es in cat["es"].items():
                if clave not in cat["en"] or clave in IGUAL_EN_INGLES:
                    continue
                if cat["en"][clave] != es:
                    continue
                sin_glosario = es
                for g in GLOSARIO:
                    sin_glosario = sin_glosario.replace(g, " ")
                if re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{3}", sin_glosario):
                    iguales.append(f"[{donde}] `{clave}`: {es[:60]}")
        self.assertEqual(iguales, [],
                         "sin traducir al inglés:\n  · " + "\n  · ".join(iguales[:10]))


class TestElManualEstaCompleto(unittest.TestCase):
    """Las secciones del catálogo tienen que ser las que el nav ofrece.

    Esto nació de un fallo real: al sacar el manual del bundle, la extracción
    buscaba las claves con `^  (\w+):` y se dejó **`why-upgrade`**, que lleva
    guion y por tanto va entrecomillada en el objeto. El resultado habría sido
    un botón del manual que abre una sección vacía — sin ningún error, ni en
    consola ni en la suite.

    Contar secciones no basta: hay que cruzarlas con los `onclick` del nav,
    que es la lista de lo que un usuario puede pedir.
    """

    DIR = APP_DIR / "static" / "i18n" / "manual"

    @classmethod
    def setUpClass(cls):
        import sys as _s
        _s.path.insert(0, str(APP_DIR / "tests"))
        from frontend_sources import html
        cls.pedidas = set(re.findall(r"_cmv40HelpSwitch\('([^']+)'\)", html()))
        cls.cat = {i: json.loads((cls.DIR / f"{i}.json").read_text(encoding="utf-8"))
                   for i in IDIOMAS}

    def test_el_nav_pide_secciones_y_las_hay(self):
        self.assertGreaterEqual(len(self.pedidas), 7,
                                "el nav del manual ha perdido secciones")
        for idioma in IDIOMAS:
            faltan = sorted(self.pedidas - set(self.cat[idioma]))
            self.assertEqual(faltan, [], (
                f"secciones que el nav ofrece y `{idioma}.json` no tiene: "
                f"{faltan} — el botón abriría un panel vacío sin dar error"))

    def test_no_hay_secciones_que_nadie_pueda_abrir(self):
        for idioma in IDIOMAS:
            sobran = sorted(set(self.cat[idioma]) - self.pedidas)
            self.assertEqual(sobran, [],
                             f"secciones en `{idioma}.json` sin botón: {sobran}")

    def test_ninguna_seccion_esta_vacia(self):
        for idioma in IDIOMAS:
            vacias = [k for k, v in self.cat[idioma].items() if len(v) < 500]
            self.assertEqual(vacias, [],
                             f"secciones sospechosamente cortas en {idioma}: {vacias}")

    def test_las_tres_lenguas_tienen_la_misma_estructura_html(self):
        """Mismo árbol de etiquetas: la traducción cambia texto, no marcado.

        Un `<td>` perdido descuadra una tabla del manual, y eso no lo ve nadie
        hasta que alguien abre esa sección en ese idioma.
        """
        TAG = re.compile(r"<\s*(/?)([a-zA-Z][\w-]*)")
        for seccion, es in self.cat["es"].items():
            base = [m.group(1) + m.group(2).lower() for m in TAG.finditer(es)]
            for otra in ("en", "ca"):
                txt = self.cat[otra].get(seccion, "")
                if txt == es:
                    continue        # aún sin traducir: cae al castellano
                otros = [m.group(1) + m.group(2).lower() for m in TAG.finditer(txt)]
                self.assertEqual(otros, base, (
                    f"`{seccion}` en `{otra}` no tiene el mismo árbol de "
                    f"etiquetas que el castellano ({len(otros)} vs {len(base)})"))


if __name__ == "__main__":
    unittest.main()
