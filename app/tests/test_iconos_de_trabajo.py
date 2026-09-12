"""Los iconos de la columna de trabajo, y las variables CSS que existen.

Los emoji los dibuja el sistema operativo: cambian de forma y de color entre
máquinas, no heredan la paleta de la aplicación y a un ⏳ o un ⬜ no hay manera
de quitarles el aire de conversación de chat. Un `<svg>` con `currentColor`
hereda el color, se anima con CSS y pesa lo mismo que un carácter.

Lo que este fichero fija:

- **Un icono por tipo de trabajo y otro por estado**, para que el mismo trabajo
  se vea igual en la columna, en la cola, en el historial y en el modal. Si el
  modal derivara el suyo de un campo aparte, podrían acabar discrepando.
- **La paleta es la de la app.** El aspecto pastel sale de usar las variantes
  `-dim` como fondo y las sólidas como trazo, no de colores nuevos.
- **Ninguna variable CSS se usa sin definirse.** Una `var()` que no existe
  invalida la declaración entera y el estilo cae al heredado, que casi siempre
  se parece — por eso pasan desapercibidas. Había cuatro sin definir, una de
  ellas recién metida por mí en los bordes de la columna nueva, que
  sencillamente no se pintaban.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_iconos_de_trabajo -v
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
STATIC = APP_DIR / "static"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import sistema_de_iconos, html, js_completo, pieza_de, rutas  # noqa: E402

SISTEMA_ICONOS = sistema_de_iconos()

NODE = shutil.which("node")
JS = js_completo()
CSS = (STATIC / "style.css").read_text(encoding="utf-8")

# tipo → pestaña de la que viene. El color sale de la SEGUNDA columna: el
# glifo dice qué se hace y el tono de dónde viene, que es lo que hace la
# columna escaneable sin leer.
TIPOS = {"rip": "rip", "crear_serie": "rip",
         "analisis_extendido": "mkv", "copia_biblioteca": "mkv",
         "fase_cmv40": "cmv40", "preflight": "cmv40"}
ESTADOS = ["corriendo", "en_cola", "hecho", "error", "cancelado", "esperando"]
TONO_DE_TAB = {"rip": "azul", "mkv": "turquesa", "cmv40": "naranja"}


def _fn(nombre: str) -> str:
    i = JS.index(f"function {nombre}(")
    return JS[JS.rindex("\n", 0, i) + 1:JS.index("\n}\n", i) + 3]


def _bloque(marca: str) -> str:
    i = JS.index(marca)
    return JS[i:JS.index("\n};\n", i) + 4]


def _linea(marca: str) -> str:
    i = JS.index(marca)
    return JS[i:JS.index("\n", i) + 1]


@unittest.skipIf(NODE is None, "node no está instalado")
class TestHayIconoParaTodo(unittest.TestCase):
    """Un tipo sin icono deja un hueco donde el resto tiene una pista."""

    def _render(self, fn, clave, tab="") -> str:
        args = (f"{json.dumps(clave)}, {json.dumps(tab)}"
                if fn == "iconoDeTrabajo" else json.dumps(clave))
        guion = f"""
{SISTEMA_ICONOS}
console.log(JSON.stringify({fn}({args})));
"""
        r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True,
                           timeout=30)
        if r.returncode != 0:
            raise AssertionError(r.stderr[:600])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_los_seis_tipos_tienen_el_suyo(self):
        for t, tab in TIPOS.items():
            with self.subTest(tipo=t):
                h = self._render("iconoDeTrabajo", t, tab)
                self.assertIn("<svg", h)
                self.assertIn("icono-chip", h)

    def test_el_tono_lo_da_la_PESTANA_no_el_tipo(self):
        """Decidido con el usuario: el glifo dice qué se hace y el color de
        dónde viene. Antes no había regla —rip azul y serie morada siendo las
        dos de Tab 1— y mirando la columna no se sabía el origen."""
        for t, tab in TIPOS.items():
            with self.subTest(tipo=t):
                self.assertIn(f"icono-{TONO_DE_TAB[tab]}",
                              self._render("iconoDeTrabajo", t, tab))

    def test_los_seis_estados_tambien(self):
        for e in ESTADOS:
            with self.subTest(estado=e):
                self.assertIn("<svg", self._render("iconoDeEstado", e))

    def test_un_tipo_desconocido_no_pinta_basura(self):
        """Mejor un hueco que un cuadro vacío o un `undefined` en el HTML."""
        self.assertEqual(
            self._render("iconoDeTrabajo", "tipo_que_no_existe", "rip"), "")

    def test_una_pestana_desconocida_no_deja_el_icono_sin_color(self):
        """Una entrada de una cola persistida de antes puede no traer `tab`;
        el icono tiene que salir igual, en gris."""
        self.assertIn("icono-gris", self._render("iconoDeTrabajo", "rip", ""))

    def test_solo_el_de_en_curso_se_anima(self):
        """El movimiento tiene que significar algo. Si se animaran todos, no
        distinguiría nada."""
        girando = [e for e in ESTADOS
                   if "icono-girando" in self._render("iconoDeEstado", e)]
        self.assertEqual(girando, ["corriendo"])

    def test_el_tamano_va_en_el_chip_no_en_el_svg(self):
        """Así el mismo icono sirve en la columna y en la cabecera del modal
        sin tocar el marcado."""
        h = self._render("iconoDeTrabajo", "rip", "rip")
        self.assertNotIn("width=", h.split("</span>")[0].split("<svg")[0])
        self.assertIn('viewBox="0 0 24 24"', h)


class TestElAspectoSaleDeLaPaletaDeLaApp(unittest.TestCase):

    def test_los_tonos_usados_estan_definidos_en_el_css(self):
        tonos = set(re.findall(r"\.icono-([a-z]+)\s*\{", CSS))
        usados = set(re.findall(r"\['([a-z]+)', _svg", JS))
        self.assertEqual(usados - tonos, set(),
                         "hay un tono de icono sin regla CSS: el chip saldría "
                         "sin color")

    def test_el_pastel_sale_de_las_variantes_dim(self):
        """No se inventan colores: el fondo es la variante translúcida que la
        app ya define y el trazo, la sólida."""
        bloque = CSS[CSS.index(".icono-azul"):CSS.index(".icono-girando")]
        self.assertIn("var(--blue-dim)", bloque)
        self.assertIn("var(--blue)", bloque)
        self.assertIn("var(--green-dim)", bloque)

    def test_se_respeta_prefers_reduced_motion(self):
        self.assertIn("prefers-reduced-motion", CSS)
        i = CSS.index("prefers-reduced-motion")
        self.assertIn("icono-girando", CSS[i:i + 400])


class TestNoQuedanEmojiEnLaColumnaNiEnElModal(unittest.TestCase):
    """Las superficies nuevas van con SVG. En el resto de la app los emoji
    siguen, y está bien: ahí son contenido (💿 = un disco), no iconografía de
    estado."""

    def _sin_comentarios(self, src: str) -> str:
        return re.sub(r"//.*|/\*(?:.|\n)*?\*/", "", src)

    def test_la_columna_no_pinta_emoji_de_estado(self):
        # La pieza de la COLUMNA, por una función que solo vive ahí. Antes se
        # buscaba por `iconoDeTrabajo`, que se fue al catálogo común de
        # `core.js` — y entonces esto medía otro fichero.
        src = self._sin_comentarios(pieza_de("_workbarTarjeta")[1])
        for emoji in ("⏳", "⬜", "✓ ", "⚙︎"):
            self.assertNotIn(emoji, src, f"queda {emoji!r} en la columna")

    def test_las_cinco_vistas_no_traen_icono_propio_DEL_TRABAJO(self):
        """El icono del TRABAJO lo deriva el modal del tipo; si cada vista
        trajera el suyo, la columna y el modal podrían enseñar distintos.

        Los de cada FASE sí son de la vista —igual que `st.icon` en la
        timeline de CMv4.0—: describen el paso, no el trabajo. Por eso se
        mira solo la clave de primer nivel del objeto que devuelve."""
        for i in re.finditer(r"registrarDetalleDeTrabajo\('[a-z_]+'", JS):
            bloque = JS[i.start():JS.index("});", i.start())]
            self.assertNotIn("\n    icono:", bloque, bloque[:60])

    def test_el_html_de_la_columna_y_el_modal_no_lleva_emoji(self):
        h = html()
        for marca in ('id="workbar-title"', 'id="trabajo-modal-icono"'):
            pass
        i = h.index('<aside id="workbar"')
        self.assertNotIn("⚙︎", h[i:i + 900])


class TestElHistorialSeVeSiempre(unittest.TestCase):
    """Saber qué acaba de pasar es la mitad de la pregunta que esta columna
    responde. Se pintaba solo con la casa libre, o sea casi nunca cuando de
    verdad interesa."""

    def test_se_pinta_en_las_DOS_ramas_del_render(self):
        """La de «no hay nada en ejecución» y la de con trabajo. Vive en su
        propio contenedor desde que dejó de viajar con el poll, así que lo que
        hay que comprobar es que las dos salidas lo pintan."""
        src = pieza_de("_workbarRender")[1]
        i = src.index("function _workbarRender(")
        cuerpo = src[i:src.index("\n}\n", i)]
        self.assertEqual(cuerpo.count("_workbarRenderHistorial()"), 2,
                         "alguna rama del render deja el historial sin pintar")
        # Y la de la casa libre lo hace ANTES de salirse.
        vacio = cuerpo.index("workbar-vacio")
        self.assertLess(vacio, cuerpo.index("_workbarRenderHistorial()", vacio))


class TestNingunaVariableCssSeUsaSinDefinirse(unittest.TestCase):
    """Una `var()` que no existe invalida la declaración ENTERA.

    No falla ruidosamente: el estilo cae al heredado, que casi siempre se
    parece lo bastante. Había cuatro sin definir —`--text`, `--accent`,
    `--text-secondary`, `--font-stack`— y una quinta recién metida por mí,
    `--border`, en los bordes de la columna de trabajo: sencillamente no se
    pintaban.
    """

    def test_todas_las_var_tienen_su_declaracion(self):
        definidas = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", CSS, re.M))
        usadas = set()
        for f in sorted(STATIC.glob("*")):
            if f.suffix in (".css", ".js", ".html"):
                # Solo `var(--x)` cerrado: `var(--x, fallback)` y las
                # construidas al vuelo (`var(--chip-${c})`) no son comprobables.
                usadas |= set(re.findall(r"var\((--[a-z0-9-]+)\)",
                                         f.read_text(encoding="utf-8")))
        self.assertEqual(
            sorted(usadas - definidas), [],
            "variables CSS usadas y nunca definidas: la declaración se "
            "invalida y el estilo cae al heredado sin un solo error")


class TestElCatalogoEsUnoYEstaCompleto(unittest.TestCase):
    """43 glifos SVG en `core.js`, y nadie puede pedir uno que no exista.

    Un nombre mal escrito —`data-icono="engranaje"` cuando el glifo se llama
    `ajustes`— deja el hueco VACÍO y no da ningún error: es el modo de fallo
    de esta familia de cambios, y el único que un test puede cazar antes de
    que lo vea el usuario.
    """

    @classmethod
    def setUpClass(cls):
        i = JS.index("const GLIFOS = {")
        j = JS.index("\n};", i)
        cls.nombres = set(re.findall(r"^\s{2}([a-zA-Z]\w*):",
                                     JS[i:j], re.M))

    def test_hay_catalogo_y_no_esta_vacio(self):
        self.assertGreater(len(self.nombres), 30, self.nombres)

    def test_ningun_data_icono_del_html_apunta_a_un_glifo_inexistente(self):
        pedidos = set(re.findall(r'data-icono="([a-zA-Z]\w*)"', html()))
        self.assertTrue(pedidos, "el marcado ya no declara ningún icono")
        self.assertEqual(pedidos - self.nombres, set(),
                         "iconos que el catálogo no tiene: el hueco se queda "
                         "vacío y no salta ningún error")

    def test_ni_los_del_html_que_genera_el_js(self):
        pedidos = set(re.findall(r'data-icono=\\?"([a-zA-Z]\w*)\\?"', JS))
        self.assertEqual(pedidos - self.nombres, set())

    def test_ni_las_llamadas_con_nombre_literal(self):
        pedidos = set(re.findall(r"\bicono\(\s*'([a-zA-Z]\w*)'", JS))
        self.assertTrue(pedidos)
        self.assertEqual(pedidos - self.nombres, set())

    def test_ni_las_referencias_directas_al_catalogo(self):
        """`_svg(GLIFOS.destellos)` con el glifo renombrado devuelve
        `undefined` y el icono sale VACÍO. Es como el tipo de trabajo de
        CMv4.0 se quedó sin dibujo al cambiar los destellos por la curva."""
        pedidos = set(re.findall(r"\bGLIFOS\.(\w+)", JS))
        self.assertTrue(pedidos)
        self.assertEqual(pedidos - self.nombres, set())

    def test_ni_los_valores_de_los_mapas_de_icono(self):
        """`icon: 'lupaOnda'` se pinta con `icono(x.icon)`: si el nombre no
        está en el catálogo, la fila sale sin icono."""
        pedidos = set(re.findall(r"icon:\s*'([a-zA-Z]\w*)'", JS))
        self.assertTrue(pedidos)
        self.assertEqual(pedidos - self.nombres, set())

    def test_todos_los_glifos_son_svg_de_verdad(self):
        i = JS.index("const GLIFOS = {")
        cuerpo = JS[i:JS.index("\n};", i)]
        # Cada valor tiene que empezar por una etiqueta SVG, no por texto.
        for linea in cuerpo.splitlines():
            m = re.match(r"\s{2}([a-zA-Z]\w*):\s*'(.)", linea)
            if m:
                self.assertEqual(m.group(2), "<",
                                 f"{m.group(1)} no empieza por una etiqueta")

    def test_un_nombre_desconocido_no_pinta_basura(self):
        guion = (_fn('_svg') + _bloque('const GLIFOS = {') + _fn('icono')
                 + "console.log(JSON.stringify([icono('noExiste'), icono('')]));")
        r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[:400])
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]), ["", ""])

    def test_el_icono_lleva_su_clase_para_que_el_css_lo_dimensione(self):
        guion = (_fn('_svg') + _bloque('const GLIFOS = {') + _fn('icono')
                 + "console.log(JSON.stringify(icono('disco', 'ico-lg')));")
        r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True,
                           timeout=30)
        h = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertIn('class="ico ico-lg"', h)
        self.assertIn('stroke="currentColor"', h,
                      "sin `currentColor` el icono no hereda el color del sitio")


class TestElEmojiSeFueDeLaInterfaz(unittest.TestCase):
    """Lo que se ve en pantalla ya no lo dibuja el sistema operativo.

    No se persigue el emoji en los comentarios ni en los markers del log de
    CMv4.0 (`🎯 Resultado`, `📋 Plan`…), que son tokens de persistencia y del
    parser del frontend: ahí cambiarlos rompe cosas.
    """

    _EMOJI = re.compile('[\U0001F300-\U0001FAFF☀-➿⬀-⯿✓✔✗✘▶⏳⏸⚠]')

    def test_el_marcado_estatico_no_pinta_emoji(self):
        h = html()
        # El favicon es un `data:` con un emoji dentro y dos frases lo usan
        # como palabra («marcada con 📀»); lo que se comprueba es el marcado.
        sin_favicon = re.sub(r'<link rel="icon"[^>]*>', '', h)
        sin_com = re.sub(r"<!--.*?-->", "", sin_favicon, flags=re.S)
        sueltos = [l.strip()[:70] for l in sin_com.splitlines()
                   if self._EMOJI.search(l)]
        self.assertEqual(sueltos, [], f"quedan {len(sueltos)} en index.html")

    def test_ningun_toast_trae_su_propio_icono(self):
        """El tipo del toast ya pone uno: el del mensaje salía duplicado."""
        for pieza, src in [(p, s) for p, s in map(pieza_de, ('showToast',))]:
            pass
        sobra = re.findall(rf"showToast\(\s*[`'\"]\s*{self._EMOJI.pattern}", JS)
        self.assertEqual(sobra, [])


class TestNoQuedaNingunEmojiSinJustificar(unittest.TestCase):
    """Lista blanca: **cualquier** emoji en el frontend falla salvo los de aquí.

    Perseguir patrones no sirvió. La primera pasada convirtió `>💿 Texto`, que
    es como se escribe en el HTML, y se dejó todo lo que vive en una TERNARIA
    (`state === 'done' ? '✅' : '🔒'`, los iconos de fase), en un VALOR de
    objeto, en un ARGUMENTO (`showConfirm('🗑️ Eliminar', …)`) o al principio
    de una línea dentro de una plantilla multilínea. Eran ciento veinte, y
    salieron a la cara del usuario.

    Con una lista blanca no hay patrón que se escape: si aparece un emoji
    nuevo, o se convierte o hay que venir aquí a escribir por qué no.
    """

    # El rango AMPLIADO. La primera versión solo miraba los emoji «de color»
    # y las flechas quedaron fuera —se excluyeron porque los 268 `→` de los
    # comentarios daban ruido—, así que un `↩️ Deshacer cambios` pasaba el
    # guard sin despeinarse. Lo mismo el ⏱ del cronómetro, el ⏭ de «saltar»,
    # los ▾▸ de los chevrones y el ● de «cambios sin guardar».
    _EMOJI = re.compile(
        '[\U0001F300-\U0001FAFF'      # pictogramas
        '☀-➿'                          # símbolos varios y dingbats
        '←-⇿'                          # FLECHAS: ↩ ↺ ↻ ↗ ⇄ …
        '⌀-⏿'                          # técnicos: ⏱ ⏭ ⏳ ⏸
        '■-◿'                          # geométricos: ● ▸ ▾ ⬜
        '⬀-⯿✓✔✗✘▶⏳⏸⚠]')

    # Lo que SÍ puede llevar un emoji, y el motivo.
    _PERMITIDO = (
        # 1 · Contrato con el backend: el log de las fases llega con estos
        #     símbolos y el frontend los busca para colorear y para filtrar.
        #     Cambiarlos aquí sin cambiar el Python rompe el coloreado.
        "line.includes(", "low.includes(", "msg.includes(", "l.includes(",
        "ev.data", ".test(ev.data)",
        # 2 · Texto que se copia al portapapeles: es un informe en Markdown,
        #     no interfaz. Ahí un ✓ es el contenido.
        "- L3: ",
    )

    # Y las flechas que son TIPOGRAFÍA, no iconos: «ISO → MKV», «P3 ↑ BT.2020»,
    # «MPLS ↔ episodio». Van dentro de una frase y se leen como un signo de
    # puntuación; sustituirlas por un SVG partiría el renglón.
    _TIPOGRAFICOS = "→←↔↑"

    def _solo_tipograficos(self, linea: str) -> bool:
        return all(c in self._TIPOGRAFICOS for c in self._EMOJI.findall(linea))

    def test_cero_emoji_fuera_de_la_lista(self):
        malas = []
        for ruta in rutas() + [STATIC / "index.html"]:
            en_bloque = False
            en_html = False
            for n, linea in enumerate(
                    ruta.read_text(encoding="utf-8").splitlines(), 1):
                t = linea.strip()
                if "/*" in t:
                    en_bloque = True
                # Los comentarios HTML también abarcan varias líneas, y el
                # `index.html` tiene unos cuantos explicando las plantillas.
                if "<!--" in t:
                    en_html = True
                if "-->" in t:
                    if en_html and not t.startswith("<!--"):
                        en_html = False
                        continue
                    en_html = False
                cierra = "*/" in t
                comentario = (t.startswith("//") or t.startswith("*")
                              or t.startswith("<!--") or t.startswith("/**")
                              or en_html or (en_bloque and not cierra))
                if cierra:
                    en_bloque = False
                if comentario or not self._EMOJI.search(linea):
                    continue
                if any(p in linea for p in self._PERMITIDO):
                    continue
                if self._solo_tipograficos(linea):
                    continue
                malas.append(f"{ruta.name}:{n}: {t[:76]}")
        self.assertEqual(malas, [], "\n  ".join(
            ["", "emoji sin convertir ni justificar:"] + malas))


class TestNadieInterpolaUnNombreDeGlifoCrudo(unittest.TestCase):
    """`${paso.icono}` con el valor ya convertido escribe «claqueta».

    Mientras los iconos eran emoji, interpolar el valor pintaba el carácter y
    todo funcionaba. Al pasar a nombres del catálogo, la misma interpolación
    escribe el NOMBRE en pantalla — texto crudo, sin ningún error. Le pasó a
    la tira de fases de la conversión, que recibe sus pasos de cinco sitios
    distintos; se arregló resolviéndolo en el consumidor (`_glifoDePaso`).
    """

    def test_toda_interpolacion_de_un_icono_pasa_por_el_catalogo(self):
        malas = []
        for ruta in rutas():
            for n, linea in enumerate(
                    ruta.read_text(encoding="utf-8").splitlines(), 1):
                t = linea.strip()
                if t.startswith("//") or t.startswith("*"):
                    continue
                for m in re.finditer(r"\$\{([^}]*\.icono?)\}", linea):
                    if "icono(" not in m.group(1) and "cartel" not in m.group(1):
                        malas.append(f"{ruta.name}:{n}: {m.group(0)}")
        self.assertEqual(malas, [], "\n  ".join(
            ["", "se pintaría el nombre del glifo como texto:"] + malas))


class TestNadieVuelveAPintarUnEmojiDesdeElJs(unittest.TestCase):
    """`el.textContent = '💿'` es como el icono de la pestaña 1 volvió al emoji.

    La conversión cubrió el HTML —`>💿 Texto`— y los mapas, pero no las
    asignaciones desde el JS, que no empiezan por `>`. Y una de ellas era
    `updateSubtabQueuePill`, que **corre en cada vuelta del poll de la cola**:
    restauraba el icono de la pestaña poniendo el emoji con `textContent`, así
    que machacaba el SVG nada más cargar la página, sin que hubiera corrido
    ningún trabajo.

    Se mira solo lo que va a un ELEMENTO. El Markdown que se copia al
    portapapeles y los markers del log llevan sus símbolos a propósito.
    """

    _EMOJI = '[\U0001F300-\U0001FAFF☀-➿⬀-⯿✓✔✗✘▶⏳⏸⚠]'

    def test_ninguna_asignacion_a_textContent_o_innerHTML_lleva_emoji(self):
        malas = []
        for ruta in rutas():
            nombre = ruta.name
            for n, linea in enumerate(
                    ruta.read_text(encoding="utf-8").splitlines(), 1):
                t = linea.strip()
                if t.startswith("//") or t.startswith("*"):
                    continue
                if re.search(rf"(textContent|innerHTML)\s*=\s*[`'\"][^`'\"]*"
                             rf"{self._EMOJI}", linea):
                    malas.append(f"{nombre}:{n}: {t[:72]}")
        self.assertEqual(malas, [], "\n  ".join([""] + malas))


if __name__ == "__main__":
    unittest.main()
