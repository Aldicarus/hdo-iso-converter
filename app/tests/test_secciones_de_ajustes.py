"""El modal de Configuración, partido en secciones.

Llegó a **ocho bloques en un solo scroll** de 860 px y ya no se encontraba
nada. La partición NO se hizo reordenando el marcado: cada
`.settings-section` declara su `data-bloque` y `SECCIONES_AJUSTES`
(settings.js) dice a qué sección va y en qué orden; al abrir el modal, los
nodos se mueven a su panel con `appendChild`.

Eso deja **un fallo mudo posible**: un bloque que no esté en ninguna sección
—o una sección que cite un bloque que no existe— desaparece de la pantalla
sin dar un error, y el modal sigue abriendo tan campante. `TestLaTablaYElMarcadoCuadran`
lo cruza en las DOS direcciones, y corre sin Chrome.

El resto se mide **abriendo el modal en Chrome**, porque el reparto ocurre
en tiempo de ejecución: leer el HTML no dice dónde acaba cada bloque.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_secciones_de_ajustes -v
"""
import html as _html
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
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import html, js_completo, stub_catalogo_es  # noqa: E402

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)


def _tabla_de_secciones() -> list[dict]:
    """`SECCIONES_AJUSTES` leída del fuente, sin ejecutar JS.

    Se parsea en vez de importarse porque este test tiene que correr también
    sin Chrome ni node: es el guard del fallo mudo y no puede depender de
    tenerlos instalados.
    """
    src = js_completo()
    i = src.find("const SECCIONES_AJUSTES = [")
    assert i != -1, "no está SECCIONES_AJUSTES"
    j = src.find("\n];", i)
    cuerpo = src[i:j]
    secciones = []
    for trozo in re.findall(r"\{(.*?)\n  \}", cuerpo, re.S):
        sid = re.search(r"id:\s*'([^']+)'", trozo)
        bl = re.search(r"bloques:\s*\[([^\]]*)\]", trozo, re.S)
        if not sid or not bl:
            continue
        secciones.append({
            "id": sid.group(1),
            "icono": (re.search(r"icono:\s*'([^']+)'", trozo) or [None, ""])[1],
            "bloques": re.findall(r"'([^']+)'", bl.group(1)),
        })
    return secciones


def _bloques_del_marcado() -> list[str]:
    return re.findall(r'class="settings-section[^"]*"\s+data-bloque="([^"]+)"', html())


class TestLaTablaYElMarcadoCuadran(unittest.TestCase):
    """Las dos direcciones, porque cada una esconde un fallo distinto."""

    @classmethod
    def setUpClass(cls):
        cls.tabla = _tabla_de_secciones()
        cls.marcado = _bloques_del_marcado()

    def test_hay_secciones_y_bloques(self):
        self.assertGreaterEqual(len(self.tabla), 2)
        self.assertGreaterEqual(len(self.marcado), 8)

    def test_ningun_bloque_del_marcado_se_queda_sin_seccion(self):
        """El fallo mudo: se deja de ver y nadie se entera."""
        colocados = {b for s in self.tabla for b in s["bloques"]}
        huerfanos = sorted(set(self.marcado) - colocados)
        self.assertEqual(huerfanos, [], (
            "estos bloques existen en index.html y no están en ninguna "
            "sección, así que NO se pintan: " + ", ".join(huerfanos)))

    def test_ninguna_seccion_cita_un_bloque_que_no_existe(self):
        """Al revés: una entrada de más parece cobertura y no cubre nada."""
        reales = set(self.marcado)
        fantasma = sorted({b for s in self.tabla for b in s["bloques"]} - reales)
        self.assertEqual(fantasma, [], (
            "SECCIONES_AJUSTES cita bloques que no están en el marcado: "
            + ", ".join(fantasma)))

    def test_ningun_bloque_esta_en_dos_secciones(self):
        """`appendChild` MUEVE: el segundo se llevaría el nodo del primero."""
        vistos, repes = set(), []
        for s in self.tabla:
            for b in s["bloques"]:
                if b in vistos:
                    repes.append(b)
                vistos.add(b)
        self.assertEqual(repes, [])

    def test_la_version_va_primero_en_general(self):
        """Lo que se viene a mirar al abrir esto sin una tarea concreta, y lo
        único que puede pedir una acción."""
        general = next(s for s in self.tabla if s["id"] == "general")
        self.assertEqual(general["bloques"][0], "version")

    def test_los_titulos_y_los_iconos_existen(self):
        """El rótulo se COMPONE del id (`ajustes.seccion.general`), igual que
        el backend con `tr(f'cmv40.fase_{fase}')`.

        Eso deja al guard genérico de claves sin poder resolverlas —solo ve
        `ajustes.seccion.${sec.id}`— así que la comprobación de que existen
        es responsabilidad de este test. Una clave que falte NO da error:
        `tr()` devuelve la clave y se pinta en crudo.
        """
        for lg in ("es", "en", "ca"):
            cat = json.loads((APP_DIR / "static" / "i18n" / f"{lg}.json")
                             .read_text(encoding="utf-8"))
            for s in self.tabla:
                for clave in (f"ajustes.seccion.{s['id']}",
                              f"ajustes.seccion.{s['id']}_desc"):
                    with self.subTest(idioma=lg, clave=clave):
                        self.assertIn(clave, cat)

    def test_los_iconos_de_las_secciones_existen(self):
        """Un glifo que no está en el catálogo deja el hueco vacío, sin error."""
        src = js_completo()
        for s in self.tabla:
            with self.subTest(seccion=s["id"]):
                self.assertRegex(src, r"\n  %s:" % re.escape(s["icono"]))


def _medir() -> dict:
    sonda = ("<style>*{transition:none!important;animation:none!important}</style>"
             "<script>window.__errores=[];"
             "window.addEventListener('error',e=>window.__errores.push("
             "(e.message||'')+' @ '+(e.filename||'').split('/').pop()"
             "+':'+e.lineno));</script>")
    cuerpo = """
<pre id="__out"></pre>
<script>
(function () {
  // Lo que `/api/settings` contesta: dos claves puestas por el usuario, una
  // que trae la app y los tres idiomas del servidor.
  // Copiado de la respuesta real del NAS: `sheet` trae `url` —y por eso su
  // campo se pre-pobla, que es la condición del falso aviso de «cambios sin
  // guardar»—. Un stub sin `url` deja el campo vacío y el test pasa sin
  // comprobar nada.
  const URL_SHEET = 'https://docs.google.com/spreadsheets/d/15i0a84uiBtWiHZ5CXZZ7wygLFXwYOd84/edit?gid=828864432';
  window.apiFetch = async () => ({
    tmdb:   {configured: true,  source: 'default', is_default: true},
    google: {configured: true,  source: 'settings', last4: 'ab12', is_default: false},
    sheet:  {configured: true,  source: 'default', url: URL_SHEET,
             default_url: URL_SHEET, sheet_id_last6: 'wYOd84',
             gid: '828864432', is_default: true},
    'drive-folder': {configured: true, source: 'default'},
    idioma: {activo: 'es', disponibles: ['es', 'en', 'ca'], por_defecto: 'es'},
  });
  window.checkForUpdates = async () => {};
  const visible = el => el && getComputedStyle(el).display !== 'none';
  const bloquesDe = sid => [...document.querySelectorAll(
      `#settings-panel .settings-seccion[data-seccion="${sid}"] .settings-section`)]
    .map(n => n.dataset.bloque);
  setTimeout(async () => {
    const out = {errores: window.__errores};
    try {
      await openSettingsModal();
      await new Promise(r => setTimeout(r, 120));
      out.nav = [...document.querySelectorAll('#settings-nav .settings-nav-item')]
        .map(b => ({id: b.dataset.seccion,
                    txt: b.querySelector('.settings-nav-titulo').textContent,
                    desc: b.querySelector('.settings-nav-desc').textContent,
                    activo: b.classList.contains('activo'),
                    svg: b.querySelectorAll('svg').length}));
      // De la TABLA, no de una lista escrita aquí: una sección nueva se
      // mide sola, que es lo que este guard existe para vigilar.
      for (const sec of SECCIONES_AJUSTES) out[sec.id] = bloquesDe(sec.id);
      out.visibles = [...document.querySelectorAll('#settings-panel .settings-seccion')]
        .filter(visible).map(c => c.dataset.seccion);
      // Ningún bloque puede haberse quedado fuera del panel.
      out.fuera = [...document.querySelectorAll('.settings-section[data-bloque]')]
        .filter(n => !n.closest('#settings-panel')).map(n => n.dataset.bloque);
      // Los idiomas, con su distintivo.
      out.idiomas = [...document.querySelectorAll('#settings-idiomas .settings-idioma')]
        .map(b => ({txt: b.textContent.trim(),
                    svg: b.querySelectorAll('svg.bandera').length,
                    codigo: b.querySelectorAll('.bandera-codigo').length}));
      // Un idioma sin bandera cae a las dos letras, que es lo que permite crecer.
      out.desconocido = distintivoDeIdioma('pt');
      out.conocido = (bandera('ca') || '').slice(0, 5);
      // ── El aviso de «cambios sin guardar» ──
      // Recién abierto y sin tocar nada NO puede haber cambios, aunque el
      // campo del sheet venga pre-poblado con la URL activa.
      out.sucioAlAbrir = _ajustesSinGuardar();
      const sheet = document.getElementById('settings-sheet-input');
      out.sheetPrepoblado = (sheet?.value || '') !== '';
      // Reescribir el sheet con LO MISMO tampoco es un cambio.
      sheet.value = sheet.value;
      out.sucioTrasReescribirIgual = _ajustesSinGuardar();
      // Tocar de verdad, sí.
      const tmdb = document.getElementById('settings-tmdb-input');
      tmdb.value = 'deadbeef';
      out.sucioTrasEscribir = _ajustesSinGuardar();
      // Y deshacerlo deja de serlo.
      tmdb.value = '';
      out.sucioTrasDeshacer = _ajustesSinGuardar();
      // Cambiar la URL del sheet también cuenta.
      sheet.value = 'https://docs.google.com/spreadsheets/d/OTRA';
      out.sucioTrasCambiarSheet = _ajustesSinGuardar();
      sheet.value = sheet.dataset.inicial;

      // Cambiar de sección.
      activarSeccionDeAjustes('integraciones');
      await new Promise(r => setTimeout(r, 60));
      out.visibles2 = [...document.querySelectorAll('#settings-panel .settings-seccion')]
        .filter(visible).map(c => c.dataset.seccion);
      out.activo2 = [...document.querySelectorAll('#settings-nav .settings-nav-item.activo')]
        .map(b => b.dataset.seccion);
    } catch (e) { out.error = String(e && e.stack || e); }
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 900);
})();
</script>
"""
    pagina = html().replace("</head>", sonda + stub_catalogo_es() + "</head>")
    pagina = pagina.replace("</body>", cuerpo + "</body>")
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
             "--window-size=1600,1000", "--virtual-time-budget=7000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    return json.loads(_html.unescape(m.group(1)))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestElAvisoDeCambiosSoloSaleSiLosHay(unittest.TestCase):
    """Cambiar de idioma recarga la página, así que se avisa de lo que se
    perdería. Pero salía SIEMPRE, sin haber tocado nada.

    La causa: tres campos se pintan vacíos y el del sheet **viene
    pre-poblado con la URL activa** —es pública y se enseña a propósito—,
    así que compararlos todos con la cadena vacía daba «hay cambios» de
    entrada. Un aviso que sale siempre se aprende a cerrar sin leerlo, que
    es peor que no tenerlo. Reportado por el usuario el 2026-09-18.

    Tampoco valía reusar el criterio de `saveSettings` («¿mandaría algo?»):
    ese compara el sheet con la URL por DEFECTO, así que a quien tenga una
    propia guardada le habría seguido saliendo.
    """

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_el_campo_del_sheet_viene_pre_poblado(self):
        """La premisa del bug: si esto deja de ser cierto, el test de abajo
        pasaría sin comprobar nada."""
        self.assertTrue(self.m["sheetPrepoblado"])

    def test_recien_abierto_no_hay_cambios(self):
        self.assertEqual(self.m["sucioAlAbrir"], [])

    def test_reescribir_lo_mismo_no_es_un_cambio(self):
        self.assertEqual(self.m["sucioTrasReescribirIgual"], [])

    def test_escribir_de_verdad_si_lo_es(self):
        self.assertEqual(self.m["sucioTrasEscribir"], ["settings-tmdb-input"])

    def test_y_deshacerlo_deja_de_serlo(self):
        self.assertEqual(self.m["sucioTrasDeshacer"], [])

    def test_cambiar_la_url_del_sheet_cuenta(self):
        self.assertEqual(self.m["sucioTrasCambiarSheet"], ["settings-sheet-input"])


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestElModalSeParteDeVerdad(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_abre_sin_errores(self):
        self.assertNotIn("error", self.m, self.m.get("error"))
        self.assertEqual(self.m["errores"], [])

    def test_la_navegacion_trae_las_tres_secciones_traducidas(self):
        nav = self.m["nav"]
        self.assertEqual([n["id"] for n in nav],
                         ["general", "aspecto", "integraciones"])
        self.assertEqual([n["txt"] for n in nav],
                         ["General", "Aspecto e idioma", "Integraciones"])
        for n in nav:
            with self.subTest(seccion=n["id"]):
                self.assertTrue(n["desc"], "la descripción no se pintó")
                self.assertEqual(n["svg"], 1, "falta el icono de la sección")

    def test_los_bloques_estan_donde_dice_la_tabla_y_en_ese_orden(self):
        self.assertEqual(self.m["general"],
                         ["version", "aviso", "mantenimiento"])
        self.assertEqual(self.m["aspecto"], ["tema", "idioma"])
        self.assertEqual(self.m["integraciones"],
                         ["tmdb", "google", "drive", "sheet"])

    def test_ningun_bloque_se_queda_fuera_del_panel(self):
        """El reparto mueve nodos: uno que no encuentre su sección se
        quedaría donde estaba, fuera del panel, invisible y sin error."""
        self.assertEqual(self.m["fuera"], [])

    def test_solo_se_ve_la_seccion_activa(self):
        self.assertEqual(self.m["visibles"], ["general"])
        self.assertTrue(self.m["nav"][0]["activo"])

    def test_cambiar_de_seccion_cambia_lo_que_se_ve(self):
        self.assertEqual(self.m["visibles2"], ["integraciones"])
        self.assertEqual(self.m["activo2"], ["integraciones"])

    def test_cada_idioma_lleva_su_bandera(self):
        idiomas = self.m["idiomas"]
        self.assertEqual(len(idiomas), 3, "los tres del servidor")
        for i in idiomas:
            with self.subTest(idioma=i["txt"]):
                self.assertEqual(i["svg"], 1, "sin bandera")
                self.assertEqual(i["codigo"], 0, "cayó al respaldo teniendo bandera")
        self.assertEqual([i["txt"] for i in idiomas],
                         ["Castellano", "English", "Català"])

    def test_un_idioma_sin_bandera_sale_con_su_codigo(self):
        """Lo que permite que la lista crezca sin tocar el catálogo de
        banderas: un hueco vacío sería peor que dos letras."""
        self.assertIn("bandera-codigo", self.m["desconocido"])
        self.assertIn("PT", self.m["desconocido"])
        self.assertNotIn("<svg", self.m["desconocido"])
        # Y el que sí la tiene no cae al respaldo.
        self.assertTrue(self.m["conocido"].startswith("<svg"))

    def test_ningun_svg_se_lee_como_texto(self):
        """`distintivoDeIdioma` devuelve HTML: si acabara en un `textContent`
        se vería el código fuente del SVG en pantalla."""
        for i in self.m["idiomas"]:
            with self.subTest(idioma=i["txt"]):
                self.assertNotIn("viewBox", i["txt"])


if __name__ == "__main__":
    unittest.main()
