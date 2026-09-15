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
            plantillas = [(m.start(), m.end())
                          for m in re.finditer(r"`((?:[^`\\]|\\.)*)`", src, re.S)]
            lineas = src.splitlines(keepends=True)
            base = [0]
            for l in lineas:
                base.append(base[-1] + len(l))
            for m in re.finditer(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"", src):
                if any(a <= m.start() < b for a, b in plantillas):
                    continue
                s = " ".join((m.group(1) or m.group(2) or "").split())
                if not captura.es_frase(s) or s in self.cat:
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
