"""El título dudoso: el diálogo de seguir o abandonar, y el aviso del panel.

La Fase A deja en `bdinfo_result.avisos_titulo` lo que sabe del título
elegido y no puede resolver sola —un tráiler elegido por falta de algo mejor,
varias versiones de la película, playlists señuelo—. No corrige nada: lo
cuenta, y el usuario decide.

  · al terminar el análisis, un diálogo con dos salidas: **seguir** con el
    proyecto o **abandonarlo**, que lo borra;
  · en el panel del proyecto, un aviso fijo, porque el diálogo solo sale una
    vez y el proyecto puede ejecutarse días después.

Los avisos viajan como DATOS y las frases las escribe el frontend, así que se
mira en las tres lenguas: una clave cruda o un `{hueco}` sin rellenar es lo
que se colaría.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_titulo_dudoso_en_pantalla -v
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

from frontend_sources import html, semilla_catalogo  # noqa: E402

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)

# Los tres tipos, con la forma que escribe `phase_a._avisos_del_titulo`.
AVISOS = [
    {"tipo": "titulo_menor", "mpls": "00018.mpls", "segundos_titulo": 595,
     "ref": "00000.mpls", "segundos_ref": 8340},
    {"tipo": "varias_versiones", "mpls": "00018.mpls", "segundos": 595,
     "otras": [{"mpls": "00801.mpls", "segundos": 9240}], "mas": 0},
    {"tipo": "playlists_ofuscados", "mpls": "00018.mpls", "n": 37},
]

# Una sesión de Tab 1 con lo que `openProject` y el panel leen. En inglés,
# como las fixtures de `test_las_tres_lenguas_en_pantalla`: así cualquier
# castellano en la pantalla inglesa viene de la app y no del dato.
SESION = {
    "id": "s2", "mkv_name": "Divergent (2014).mkv", "status": "pending",
    "media_type": "movie", "source_type": "iso", "iso_path": "Divergent.iso",
    "source_path": "Divergent.iso", "included_tracks": [],
    "discarded_tracks": [], "chapters": [], "execution_history": [],
    "updated_at": "2026-10-15T08:00:00Z",
    "bdinfo_result": {"main_mpls": "00018.mpls", "duration_seconds": 594.7,
                      "video_tracks": [], "audio_tracks": [],
                      "subtitle_tracks": [], "avisos_titulo": AVISOS},
}

PALABRA_DE_CADA_LENGUA = {"es": "película", "en": "film", "ca": "pel·lícula"}

_CUERPO = """
<pre id="__out"></pre>
<script>
(async function () {
  await new Promise(r => setTimeout(r, 700));
  const AV = %s;
  const SES = %s;
  const out = {errores: window.__errores};
  const modal = document.getElementById('titulo-dudoso-modal');
  try {
    out.frases = frasesDeAvisosDelTitulo(AV);
    out.desconocido = frasesDeAvisosDelTitulo([{tipo: 'de_una_version_futura'}]);

    // ── El diálogo, en sus dos salidas.
    const p1 = preguntarPorElTitulo(SES);
    pintarTextos(modal);
    out.dialogo = {abierto: modal.classList.contains('open'),
                   texto: modal.innerText,
                   items: [...modal.querySelectorAll('li')].map(li => li.textContent)};
    document.getElementById('titulo-dudoso-abandonar').click();
    out.abandonar = await p1;
    out.cerrado_tras_responder = !modal.classList.contains('open');
    const p2 = preguntarPorElTitulo(SES);
    document.getElementById('titulo-dudoso-seguir').click();
    out.seguir = await p2;

    // ── El aviso fijo del panel.
    openProjects.push({id: 'p1', sessionId: 'otra', session: SES, name: SES.mkv_name});
    createProjectPanel({id: 'p1'});
    switchSubTab('p1');
    renderAvisosDelTitulo(SES);
    const ban = document.getElementById('p1-titulo-aviso-banner');
    pintarTextos(ban);
    out.banner = {visible: ban.style.display !== 'none', texto: ban.innerText};
    renderAvisosDelTitulo({bdinfo_result: {avisos_titulo: []}});
    out.banner_sin_avisos = ban.style.display;

    // ── El flujo entero: analizar y responder.
    const flujo = async (boton) => {
      const llamadas = [];
      window.apiFetch = async (url, opts) => {
        llamadas.push([(opts && opts.method) || 'GET', url]);
        if (url === '/api/analyze') return JSON.parse(JSON.stringify(SES));
        if (url === '/api/sessions') return {sessions: []};
        return {};
      };
      const en_curso = _doAnalyzeSource('iso', 'Divergent.iso', 'Divergent.iso', {});
      for (let i = 0; i < 60 && !modal.classList.contains('open'); i++)
        await new Promise(r => setTimeout(r, 100));
      const abierto = modal.classList.contains('open');
      document.getElementById(boton).click();
      await en_curso;
      return {abierto, llamadas};
    };
    out.flujo_abandonar = await flujo('titulo-dudoso-abandonar');
    out.flujo_seguir = await flujo('titulo-dudoso-seguir');
  } catch (e) { out.error = String(e && e.stack || e); }
  document.getElementById('__out').textContent = JSON.stringify(out);
})();
</script>
"""


def _medir(idioma: str) -> dict:
    sonda = ("<style>*{transition:none!important;animation:none!important}</style>"
             "<script>window.__errores=[];"
             "window.addEventListener('error',e=>window.__errores.push("
             "(e.message||'')+' @ '+(e.filename||'').split('/').pop()));</script>")
    pagina = html().replace("</head>", sonda + semilla_catalogo(idioma) + "</head>")
    pagina = pagina.replace("</body>", _CUERPO % (
        json.dumps(AVISOS), json.dumps(SESION)) + "</body>")
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
             "--window-size=1600,1000", "--virtual-time-budget=30000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    return json.loads(_html.unescape(m.group(1)))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestElTituloDudoso(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = {idioma: _medir(idioma) for idioma in ("es", "en", "ca")}

    def test_sin_errores(self):
        for idioma, m in self.m.items():
            with self.subTest(idioma=idioma):
                self.assertNotIn("error", m, m.get("error"))
                self.assertEqual(m["errores"], [])

    def test_una_frase_por_aviso_en_cada_lengua(self):
        for idioma, m in self.m.items():
            with self.subTest(idioma=idioma):
                self.assertEqual(len(m["frases"]), 3)
                texto = " ".join(m["frases"])
                for crudo in ("tab1.", "{", "}"):
                    self.assertNotIn(crudo, texto)
                for dato in ("00000.mpls", "00801.mpls", "37"):
                    self.assertIn(dato, texto)

    def test_un_tipo_desconocido_no_se_pinta(self):
        """Un aviso de una versión más nueva del servidor no puede salir
        como su id."""
        for idioma, m in self.m.items():
            with self.subTest(idioma=idioma):
                self.assertEqual(m["desconocido"], [])

    def test_el_dialogo_lista_los_avisos_en_su_lengua(self):
        for idioma, m in self.m.items():
            with self.subTest(idioma=idioma):
                d = m["dialogo"]
                self.assertTrue(d["abierto"])
                self.assertEqual(d["items"], m["frases"])
                self.assertIn("00018.mpls", d["texto"])
                self.assertIn(PALABRA_DE_CADA_LENGUA[idioma], d["texto"])
                self.assertNotIn("tab1.", d["texto"])

    def test_las_dos_salidas(self):
        for idioma, m in self.m.items():
            with self.subTest(idioma=idioma):
                self.assertIs(m["abandonar"], False)
                self.assertIs(m["seguir"], True)
                self.assertTrue(m["cerrado_tras_responder"])

    def test_el_aviso_del_panel(self):
        for idioma, m in self.m.items():
            with self.subTest(idioma=idioma):
                self.assertTrue(m["banner"]["visible"])
                for frase in m["frases"]:
                    self.assertIn(frase, m["banner"]["texto"])
                self.assertEqual(m["banner_sin_avisos"], "none")

    def test_abandonar_borra_el_proyecto_recien_analizado(self):
        f = self.m["es"]["flujo_abandonar"]
        self.assertTrue(f["abierto"], f)
        self.assertIn(["DELETE", "/api/sessions/s2"], f["llamadas"])

    def test_seguir_no_borra_nada(self):
        f = self.m["es"]["flujo_seguir"]
        self.assertTrue(f["abierto"], f)
        self.assertNotIn("DELETE", [metodo for metodo, _ in f["llamadas"]])


if __name__ == "__main__":
    unittest.main()
