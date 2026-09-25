"""El icono de la sub-pestaña gira mientras su proyecto trabaja. Y para.

Tres defectos que son el mismo, reportados el 2026-09-25:

- en **Tab 1** el spinner seguía girando con el rip ya terminado.
  `updateProjectTabIcon` buscaba el botón por `subtab-btn-{id}`, un id que no
  pone nadie, así que salía por su `return` y no actualizaba nunca: el icono
  se pintaba al crear la sub-pestaña y ahí se quedaba;
- **Tab 2** y **Tab 3** no tenían spinner, aunque su trabajo dura más que un
  rip —diez minutos un análisis extendido, más de una hora una fase CMv4.0—.

El arreglo no es repintar en más sitios sino **derivarlo del estado real**:
una función recorre las sub-pestañas en cada vuelta de la columna y decide.
Si no hay trabajo no hay spinner, se mire cuando se mire.

La referencia es `sobre`, que es como `/api/trabajos` dice de QUÉ va un
trabajo: la ruta del MKV en Tab 2 y el id de la sesión en Tab 1 y Tab 3. Se
lee de lo que la columna ya pollea; preguntarlo aparte sería una segunda idea
de qué está pasando.

Se mide en Chrome sobre el `index.html` real, creando las sub-pestañas con la
función real de cada pestaña: lo que falló era justamente un selector, y eso
no se ve leyendo el fuente.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_spinner_de_subpestana -v
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

from frontend_sources import html, stub_catalogo_es  # noqa: E402

_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "", shutil.which("chromium") or "",
]
CHROME = next((c for c in _CANDIDATOS if c and Path(c).exists()), None)

_SONDA = ("<script>window.__errores=[];"
          "window.addEventListener('error',e=>window.__errores.push("
          "(e.message||'')+' @ '+(e.filename||'').split('/').pop()+':'+e.lineno));"
          "window.fetch=()=>new Promise(()=>{});"
          "window.WebSocket=function(){this.close=()=>{};};</script>")

_CUERPO = """
<pre id="__out"></pre>
<script>
(function () {
  // Qué icono tiene cada sub-pestaña ahora mismo: 'gira' o el nombre del
  // glifo. Se lee del DOM pintado, no de lo que la función dijo.
  const foto = () => {
    const o = {};
    document.querySelectorAll('.subtab-proj[data-sobre]').forEach(b => {
      const el = b.querySelector('.subtab-proj-icon');
      o[b.dataset.sobre] = el && el.querySelector('.spinner-inline')
        ? 'gira'
        : (el && el.querySelector('[data-icono]')
             ? el.querySelector('[data-icono]').dataset.icono
             : (el && el.querySelector('svg') ? 'svg' : '?'));
    });
    return o;
  };
  // Lo que la columna habría recibido del servidor.
  const trabajando = (sobre, tab) => {
    workbarEstado = {activo: sobre
      ? {id: 'k', sobre, tab, tipo: 't', que: 'x'} : null, interactivo: []};
    refrescarIconosDeSubPestana();
    return foto();
  };

  setTimeout(() => {
    const out = {errores: []};
    try {
      // ── Una sub-pestaña por pestaña, con su función REAL ───────────
      const rip = {id: 'p1', sessionId: 'ses_1', name: 'Peli',
                   session: {id: 'ses_1', status: 'pending'}};
      openProjects.push(rip);
      renderProjectSubTabButton(rip);

      openMkvProjects.push({id: 'm1', fileName: 'X.mkv',
                            filePath: '/mnt/output/X.mkv'});
      _mkvCreateSubTab(openMkvProjects[0]);

      const up = {id: 'c1', session: {id: 'cmv_1',
                  source_mkv_name: 'Y.mkv'}};
      openCMv40Projects.push(up);
      _createCMv40SubTab(up);

      out.declarado = [...document.querySelectorAll('.subtab-proj[data-sobre]')]
        .map(b => [b.dataset.sobre, b.dataset.glifo]);

      // ── En reposo: ninguna gira ────────────────────────────────────
      out.reposo = trabajando(null);
      // ── Cada una, por separado ─────────────────────────────────────
      out.conRip = trabajando('ses_1', 'rip');
      out.conMkv = trabajando('/mnt/output/X.mkv', 'mkv');
      out.conUpgrade = trabajando('cmv_1', 'cmv40');
      // ── Y al terminar, para ────────────────────────────────────────
      out.trasTerminar = trabajando(null);

      // ── Tab 1: el estado cambia y el glifo de reposo le sigue ──────
      rip.session.status = 'done';
      updateProjectTabIcon(rip);
      out.trasCompletar = foto();

      // ── Un trabajo que va en paralelo también cuenta ───────────────
      workbarEstado = {activo: null,
        interactivo: [{id: 'k', sobre: '/mnt/output/X.mkv'}]};
      refrescarIconosDeSubPestana();
      out.enParalelo = foto();
    } catch (e) { out.errores.push('EXCEPCIÓN: ' + e.message); }
    out.errores = out.errores.concat(window.__errores);
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 700);
})();
</script>
"""


def _medir() -> dict:
    pagina = html().replace("</head>", _SONDA + stub_catalogo_es() + "</head>")
    pagina = pagina.replace("</body>", _CUERPO + "</body>")
    pagina = (pagina.replace('src="/static/', 'src="')
                    .replace('href="/static/', 'href="'))
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                      encoding="utf-8", dir=str(APP_DIR / "static"))
    tmp.write(pagina)
    tmp.close()
    try:
        dom = subprocess.run(
            [CHROME, "--headless", "--disable-gpu",
             "--allow-file-access-from-files", "--dump-dom",
             "--window-size=1280,1000", "--virtual-time-budget=7000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    return json.loads(m.group(1))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class SpinnerCase(unittest.TestCase):
    RIP, MKV, UP = "ses_1", "/mnt/output/X.mkv", "cmv_1"

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()


class TestLasTresDeclaranAQueMirar(SpinnerCase):

    def test_cero_errores_de_js(self):
        self.assertEqual(self.m["errores"], [])

    def test_cada_pestana_declara_su_referencia_y_su_glifo(self):
        # Sin `data-sobre` la sub-pestaña no entra en el recorrido y se queda
        # fuera en silencio, que es como estaban Tab 2 y Tab 3.
        self.assertEqual(dict(self.m["declarado"]), {
            self.RIP: "disco", self.MKV: "lapiz", self.UP: "curva"})


class TestGiraSoloLaQueTrabaja(SpinnerCase):

    def test_en_reposo_no_gira_ninguna(self):
        self.assertEqual(set(self.m["reposo"].values()), {"disco", "lapiz", "curva"})

    def test_el_rip(self):
        self.assertEqual(self.m["conRip"][self.RIP], "gira")
        self.assertNotEqual(self.m["conRip"][self.MKV], "gira")
        self.assertNotEqual(self.m["conRip"][self.UP], "gira")

    def test_el_analisis_de_un_mkv(self):
        self.assertEqual(self.m["conMkv"][self.MKV], "gira")
        self.assertNotEqual(self.m["conMkv"][self.RIP], "gira")

    def test_la_fase_de_un_upgrade(self):
        self.assertEqual(self.m["conUpgrade"][self.UP], "gira")
        self.assertNotEqual(self.m["conUpgrade"][self.MKV], "gira")

    def test_un_trabajo_en_paralelo_tambien_cuenta(self):
        # Los de Tab 2 no ocupan el turno: van en «En segundo plano».
        self.assertEqual(self.m["enParalelo"][self.MKV], "gira")


class TestYParaAlTerminar(SpinnerCase):
    """El defecto reportado: el spinner se quedaba girando."""

    def test_ninguna_sigue_girando(self):
        self.assertNotIn("gira", set(self.m["trasTerminar"].values()),
                         self.m["trasTerminar"])

    def test_y_vuelve_el_icono_que_le_toca(self):
        self.assertEqual(self.m["trasTerminar"][self.RIP], "disco")

    def test_el_glifo_de_reposo_sigue_al_estado_de_la_sesion(self):
        # `updateProjectTabIcon` buscaba un id que nadie pone: no hacía nada.
        self.assertEqual(self.m["trasCompletar"][self.RIP], "check")


if __name__ == "__main__":
    unittest.main()
