"""El HTML que el JS genera también es interfaz.

Había un guard para las cadenas sueltas, otro para `index.html` y otro para el
servidor — y **ninguno para el HTML de dentro de las plantillas**, que es
donde vive la mayor parte del texto de la app. Ahí sobrevivieron **136
sitios**: los tooltips de cada botón, los rótulos de las tarjetas de fase, los
mensajes de estado. El golden los CAPTURABA, pero el golden solo exige que la
frase exista en algún sitio y el código cuenta como sitio.

Tres cosas que este módulo fija, y las tres salieron de una prueba real, no de
un test:

1. **Texto y atributos traducibles dentro de una plantilla.** Se recorre a
   cualquier profundidad: la mayor parte del HTML condicional vive en una
   plantilla ANIDADA dentro de un `${…}`, y ahí estaban 94 de los 136.
2. **La función de traducción del frontend se llama `tr`, nunca `t`.** Había
   **8 llamadas a `t('clave')`** en `settings.js` y `tab3.js`; `t` no existe
   en el navegador, así que esas funciones lanzaban `ReferenceError` y su
   sección **no se pintaba**. Se renombró a `tr` por la colisión con los 49
   locales llamados `t`, y estas ocho se quedaron atrás.
3. **Ninguna clave se escribe como si fuera el texto.** `${paso.sub}` con
   `sub: 'tab3.fase_h'` imprime la clave; es la misma trampa que
   `_glifoDePaso` documenta para los iconos.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_plantillas_del_js -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import captura_castellano as captura  # noqa: E402
from frontend_sources import rutas  # noqa: E402

ATRIBUTOS = ("title", "data-tooltip", "placeholder", "aria-label", "alt")

# Texto que se queda en el marcado de una plantilla, con su motivo.
ACEPTADO = {
    "source.hevc, BL.hevc, EL.hevc":
        "lista de nombres de fichero del workdir, no prosa",
}


def _catalogo() -> set[str]:
    es = json.loads((APP_DIR / "static" / "i18n" / "es.json")
                    .read_text(encoding="utf-8"))
    fuera = {" ".join(v.split()) for v in es.values()}
    fuera |= {" ".join(re.sub(r"\{\w+\}", " ⟦⟧ ", v).split()) for v in es.values()}
    # Un valor con marcado dentro aporta también sus nodos de texto.
    for v in es.values():
        if "<" in v:
            fuera |= {" ".join(x.split()) for x in captura._del_html(v) if x.strip()}
    return fuera


class TestNoQuedaCastellanoEnLasPlantillas(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.cat = _catalogo()

    def _sueltas(self, cont: str, fichero: str, fuera: list) -> None:
        for m in re.finditer(
                rf'\b({"|".join(ATRIBUTOS)})="([^"]+)"', cont):
            v = " ".join(m.group(2).split())
            if v and captura.es_frase(v) and v not in ACEPTADO:
                fuera.append(f"{fichero}: [{m.group(1)}] {v[:60]}")
        for t in captura._del_html(captura.sin_huecos(cont)):
            n = " ".join(t.split())
            if n and captura.es_frase(n) and n not in ACEPTADO:
                fuera.append(f"{fichero}: [texto] {n[:60]}")
        for _, _, dentro in captura.regiones_de_plantilla(cont):
            self._sueltas(dentro, fichero, fuera)
        # Y el marcado que viaja dentro de una CADENA metida en un `${…}`.
        #
        # Dentro de una plantilla las comillas son texto, no delimitadores, así
        # que `TestNoQuedaCastellanoSuelto` se salta a propósito todo lo que
        # cae en una región de plantilla; y esta cadena lleva un `<`, así que
        # el guard de cadenas en huecos también la descarta. Resultado: un
        # `'<div class="banner info">…<span>Verifica en el chart de Fase D que
        # las curvas coinciden antes de inyectar.</span></div>'` entero se
        # quedó en castellano y no lo veía NINGUNO de los tres. Aquí es
        # marcado, y se mira como tal.
        for ini, fin in captura.huecos_de(cont):
            expr = cont[ini:fin]
            for m in re.finditer(
                    r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"", expr):
                dentro = m.group(1) or m.group(2) or ""
                if "<" in dentro:
                    self._sueltas(dentro, fichero, fuera)

    def test_ni_texto_ni_atributos_castellanos_en_el_html_generado(self):
        fuera = []
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            plantillas = [(a, b) for a, b, _ in captura.regiones_de_plantilla(src)]
            for _, _, cont in captura.regiones_de_plantilla(src):
                self._sueltas(cont, Path(r).name, fuera)
            # Una cadena entrecomillada con marcado dentro es marcado.
            #
            # `TestNoQuedaCastellanoSuelto` la mira COMO CADENA, y `es_frase`
            # de un bloque HTML entero es False —la prosa es una parte
            # pequeña de la cadena—, así que un
            # `const banner = '<div class="banner info">…<span>Verifica en el
            # chart de Fase D…</span></div>'` pasaba en verde. La regla es la
            # misma que para una plantilla: lo que se juzga son sus nodos de
            # texto y sus atributos, no la cadena.
            for m in re.finditer(
                    r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"", src):
                if any(a <= m.start() < b for a, b in plantillas):
                    continue     # dentro de una plantilla ya lo ve la recursión
                dentro = m.group(1) or m.group(2) or ""
                if "<" in dentro and ">" in dentro:
                    self._sueltas(dentro, Path(r).name, fuera)
        fuera = sorted(set(fuera))
        self.assertEqual(fuera, [], (
            f"\n{len(fuera)} sitio(s) con castellano en el HTML que genera el "
            f"JS.\nPasa el texto por `data-i18n` y los atributos por "
            f"`data-i18n-tip|ph|aria`:\n  · " + "\n  · ".join(fuera[:15])))

    def test_la_lista_de_aceptados_no_se_queda_vieja(self):
        vivas = []
        todo = "\n".join(Path(r).read_text(encoding="utf-8") for r in rutas())
        muertas = [k for k in ACEPTADO if k not in todo]
        self.assertEqual(muertas, [], (
            f"\nestas entradas de ACEPTADO ya no están en el código: {muertas}"))


class TestLaFuncionDeTraduccionSeLlamaTr(unittest.TestCase):
    """`t` no existe en el frontend: se renombró por la colisión con los 49
    locales llamados `t` (`for (const t of tracks)`). Una llamada a `t('k')`
    lanza `ReferenceError` y **la sección entera deja de pintarse** — no es un
    texto mal traducido, es una tarjeta en blanco."""

    def test_nadie_llama_a_t_con_una_clave(self):
        malas = []
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            for m in re.finditer(
                    r"(?<![A-Za-z0-9_.$])t\('([a-z][a-z0-9_]*\.[a-z0-9_.]+)'", src):
                linea = src[:m.start()].count("\n") + 1
                malas.append(f"{Path(r).name}:{linea}: t('{m.group(1)}')")
        self.assertEqual(malas, [], (
            f"\n{len(malas)} llamada(s) a `t()` en el frontend. Aquí la "
            f"función se llama `tr`;\n`t` no existe y el render entero "
            f"lanza:\n  · " + "\n  · ".join(malas[:12])))

    def test_tr_existe_y_t_no(self):
        from frontend_sources import js_completo
        js = js_completo()
        self.assertIn("function tr(clave", js)
        self.assertNotRegex(js, r"^function t\(", )


class TestNingunaClaveSeEscribeComoTexto(unittest.TestCase):
    """`${paso.sub}` con `sub: 'tab3.fase_h'` imprime la clave."""

    @classmethod
    def setUpClass(cls):
        cls.claves = set(json.loads(
            (APP_DIR / "static" / "i18n" / "es.json").read_text(encoding="utf-8")))

    def test_ninguna_clave_viaja_como_valor_de_dato(self):
        malas = []
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            ok = {m.start(1) for m in
                  re.finditer(r'data-i18n(?:-\w+)?="([^"]+)"', src)}
            for m in re.finditer(r"""['"]([a-z][a-z0-9_]*\.[a-z0-9_.]+)['"]""", src):
                if m.start(1) in ok or m.group(1) not in self.claves:
                    continue
                atras = src[max(0, m.start() - 130):m.start()]
                # el primer argumento de `tr(`, o la clave de un ternario que
                # `tr(` va a resolver
                if re.search(r"\btr\(\s*$", atras) or re.search(r"\btr\([^()]{0,120}$", atras):
                    continue
                linea = src[:m.start()].count("\n") + 1
                malas.append(f"{Path(r).name}:{linea}: '{m.group(1)}'")
        self.assertEqual(malas, [], (
            f"\n{len(malas)} clave(s) escritas como si fueran el texto. "
            f"Resuélvelas con `tr()`\nen el consumidor:\n  · "
            + "\n  · ".join(malas[:12])))


if __name__ == "__main__":
    unittest.main()


class TestNingunAtributoLlevaUnTrSinInterpolar(unittest.TestCase):
    """`data-tooltip=tr('clave')` escribe `tr('clave')` en la pantalla.

    Dentro de una plantilla, `tr(...)` solo se evalúa si va en un `${…}`.
    Sin él es TEXTO, y como además va sin comillas el navegador se queda con
    `tr('workbar.detener_este_trabajo')` como valor del atributo: el tooltip
    dice el nombre de la función. **Había diez**, y ninguno daba un error —
    `node --check` pasa, el HTML es válido y el guard de plantillas ve un
    atributo que no es castellano.

    Salieron de sustituir a máquina `data-tooltip="…"` por una llamada sin
    reponer la interpolación. La forma correcta en esos diez sitios no es
    `${tr(...)}` sino `data-i18n-tip="clave"`: es declarativa, la resuelve el
    observador igual que el resto del marcado y no hay nada que interpolar.
    """

    def test_ningun_atributo_vale_una_llamada_a_tr(self):
        malas = []
        for r in rutas():
            src = Path(r).read_text(encoding="utf-8")
            for m in re.finditer(r"([a-zA-Z-]+)=\s*(tr|icono|escHtml)\(", src):
                linea = src[:m.start()].count("\n") + 1
                malas.append(f"{Path(r).name}:{linea}: "
                             f"{m.group(1)}={m.group(2)}(…)")
        self.assertEqual(malas, [], (
            f"\n{len(malas)} atributo(s) con una llamada SIN `${{…}}`: el "
            f"nombre de la función\nacaba en la pantalla. Usa "
            f"`data-i18n-tip|ph|aria=\"clave\"`, que es declarativo:\n  · "
            + "\n  · ".join(malas[:12])))


class TestNingunDataI18nParteUnAtributo(unittest.TestCase):
    """Un `<span data-i18n>` dentro de un atributo destroza el marcado.

    Caso real, y estuvo roto desde la migración:

        <div class="dv-<span data-i18n="tab2.sparkline_tooltip_s"></span>
             tyle="display:none"></div>

    El original era `<div class="dv-sparkline-tooltip" style="display:none">`.
    La sustitución a máquina cogió el trozo `sparkline-tooltip" s` —que para
    un regex parece texto— y lo reemplazó **dentro del valor del atributo**,
    partiendo el `class` y comiéndose la `s` de `style`. El HTML resultante es
    válido, así que el navegador no dice nada; lo que pasa es que
    `host.querySelector('.dv-sparkline-tooltip')` ya no encuentra nada y el
    tooltip del gráfico de luminancia **no aparece nunca**.

    Es el mismo error que documenta el proyecto sobre los regex: no se puede
    delimitar un constructo anidado con uno, y reescribir dentro de un hueco
    no falla — contesta otra cosa.
    """

    def test_ningun_valor_de_atributo_contiene_una_etiqueta(self):
        malas = []
        fuentes = list(rutas()) + [APP_DIR / "static" / "index.html"]
        for r in fuentes:
            src = Path(r).read_text(encoding="utf-8")
            # Un `data:` URI lleva el SVG del favicon dentro y NO es
            # marcado que el navegador parsee como HTML.
            for m in re.finditer(r'[a-zA-Z-]+="(?!data:)[^"]*<[a-zA-Z/]', src):
                linea = src[:m.start()].count("\n") + 1
                malas.append(f"{Path(r).name}:{linea}: {m.group(0)}")
        self.assertEqual(malas, [], (
            f"\n{len(malas)} atributo(s) con una etiqueta dentro del valor: "
            f"la sustitución\nse metió en medio del marcado:\n  · "
            + "\n  · ".join(malas[:12])))
