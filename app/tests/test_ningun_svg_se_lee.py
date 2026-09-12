"""Ningún icono se lee como texto, y ninguno queda descolocado.

`icono()` devuelve HTML. Hay **cuatro** maneras de que ese HTML acabe escrito
en vez de pintado, y las cuatro han ocurrido:

1. `escHtml(variable)` con el icono concatenado al texto — el badge de
   «Análisis y recomendación».
2. El icono en un CAMPO de objeto que el render escapa — la columna «¿=?» de
   la tabla de comparación.
3. `textContent =` — el «Esta carpeta no contiene MKVs» del file browser.
4. El icono puesto en una función y escapado en OTRA — `gateBCLabel`, que se
   arma en `_cmv40BuildPhaseSteps` y se pinta en la timeline. Este es el que
   ningún guard estático puede ver sin seguir el flujo entre funciones.

Los guards de `test_iconos_de_trabajo` cubren los tres primeros leyendo el
fuente. Este mira **el resultado**: carga la aplicación de verdad en Chrome,
pinta lo que se puede pintar sin servidor y busca el síntoma común —el texto
`viewBox`—, que no aparece jamás cuando un SVG está bien puesto.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_ningun_svg_se_lee -v
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

from frontend_sources import html  # noqa: E402

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)

# Una sesión CMv4.0 con lo justo para que la timeline pase por todas sus
# ramas: gates resueltos, una fase corriendo y otra ya hecha.
SESION = {
    "id": "c1", "source_mkv_name": "Supergirl (2026) [Audio DCP].mkv",
    "output_mkv_name": "Supergirl (2026) [CMv4 CORE].mkv",
    "phase": "extracted", "running_phase": "inject", "archived": False,
    "error_message": "", "source_workflow": "p7_fel",
    "target_type": "trusted_p7_fel_final", "target_trust_ok": True,
    "auto_pipeline": True, "phase_history": [],
    "updated_at": "2026-09-12T08:00:00Z", "created_at": "2026-09-11T08:00:00Z",
    "tmdb_info": {"title": "Supergirl", "year": 2026, "poster_url": ""},
}


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestNingunSvgSeLee(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        sonda = ("<script>window.fetch=()=>new Promise(()=>{});"
                 "window.WebSocket=function(){this.close=()=>{};};"
                 "window.__errores=[];window.addEventListener('error',"
                 "e=>window.__errores.push(e.message||''));"
                 f"window.__S={json.dumps(SESION)};</script>")
        cuerpo = """
<pre id="__out"></pre>
<div id="__extra"></div>
<script>
setTimeout(() => {
  const fuera = [];
  const pinta = (nombre, fn) => {
    try {
      const h = fn();
      if (h) document.getElementById('__extra').insertAdjacentHTML('beforeend', h);
    } catch (e) { fuera.push(nombre + ': ' + e.message); }
  };
  // Todo lo que se puede pintar sin servidor.
  document.querySelectorAll('.modal-overlay').forEach(m => m.classList.add('open'));
  pinta('timeline', () => _cmv40RenderTimeline(window.__S, {}));
  pinta('gates', () => _cmv40RenderGateCardBC('p1', window.__S, true));
  pinta('ficha', () => renderTmdbCardHTML(
      {title: 'Supergirl', year: 2026, vote_average: 7.1, vote_count: 900},
      {tipo: 'cmv40', id: 'c1', nombre: 'Supergirl (2026).mkv'}));
  pinta('recomendacion', () => _renderCMv40RecommendationCard(window.__S, 'p1'));
  setTimeout(() => {
    // Con las TRES pestañas visibles: un panel oculto mide 0 y el barrido lo
    // saltaría — que es justo lo que dejó pasar el botón de la franja navy.
    // Los paneles se ocultan con `style="display:none"` INLINE y por id, no
    // con una clase: hay que ir por los ids reales o el barrido mide 0 y se
    // salta justo lo que hay que mirar.
    ['tab-panel-1', 'tab-panel-2', 'tab-panel-3',
     'sidebar-tab-1', 'sidebar-tab-2', 'sidebar-tab-3',
     'subtab-bar', 'cmv40-subtab-bar', 'mkv-action-bar'].forEach(id => {
      const e = document.getElementById(id);
      if (e) { e.style.display = 'block'; e.style.visibility = 'visible'; }
    });
    // Y la alineación: el icono tiene que estar centrado con SU texto.
    // «Limpiar artefactos» de la franja navy se reportó TRES veces, y las dos
    // primeras se midió un botón suelto en vez del de verdad — que llevaba
    // encima una compensación óptica puesta para el emoji.
    const desalineados = [];
    document.querySelectorAll('button, .banner').forEach(b => {
      const svg = b.querySelector('svg.ico');
      const texto = [...b.childNodes].find(
        n => n.nodeType === 3 && n.textContent.trim().length > 2);
      if (!svg || !texto) return;
      const r = document.createRange();
      r.selectNodeContents(texto);
      const a = svg.getBoundingClientRect(), t = r.getBoundingClientRect();
      if (!a.height || !t.height) return;
      const d = (a.top + a.height / 2) - (t.top + t.height / 2);
      // 1,5 px y no 2: la compensación óptica que descolocaba el botón de la
      // franja navy era `translateY(2px)` justo, y con el umbral en 2 pasaba.
      if (Math.abs(d) > 1.5) {
        desalineados.push((b.className || b.tagName) + ' → ' + d.toFixed(1) + 'px');
      }
    });
    const txt = document.body.innerText || '';
    // El síntoma: un SVG bien puesto NUNCA deja `viewBox` en el texto.
    const i = txt.indexOf('viewBox');
    document.getElementById('__out').textContent = JSON.stringify({
      fuera,
      errores: window.__errores,
      svgLeido: i === -1 ? '' : txt.slice(Math.max(0, i - 90), i + 40),
      desalineados,
      iconos: document.querySelectorAll('svg.ico').length,
    });
  }, 250);
}, 700);
</script>
"""
        pagina = html().replace("</head>", sonda + "</head>")
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
                 "--window-size=1500,1000", "--virtual-time-budget=8000",
                 tmp.name],
                capture_output=True, text=True, timeout=180).stdout
        finally:
            os.unlink(tmp.name)
        m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
        if not m:
            raise unittest.SkipTest("Chrome no devolvió el volcado")
        cls.m = json.loads(_html.unescape(m.group(1)))

    def test_ningun_icono_se_lee_como_texto(self):
        self.assertEqual(self.m["svgLeido"], "",
                         "hay un SVG escrito en pantalla en vez de pintado")

    def test_y_las_vistas_se_pintan_sin_reventar(self):
        """Si una lanza, su HTML no entra y el barrido no la habría mirado."""
        self.assertEqual(self.m["fuera"], [])
        self.assertEqual(self.m["errores"], [])

    def test_ningun_icono_queda_descolocado_respecto_a_su_texto(self):
        """Tolerancia de 1,5 px sobre el centro óptico. Lo que descolocaba el de
        la franja navy era una regla escrita para compensar que **los emoji se
        sientan altos en su caja**: con un SVG, que sí está centrado, esa
        compensación hace el daño que evitaba. El CSS puesto para corregir un
        defecto del emoji hay que retirarlo al pasar a SVG."""
        self.assertEqual(self.m["desalineados"], [])

    def test_y_hay_iconos_de_verdad(self):
        """Un barrido que no encuentra ningún icono no está comprobando nada."""
        self.assertGreater(self.m["iconos"], 50)


if __name__ == "__main__":
    unittest.main()
