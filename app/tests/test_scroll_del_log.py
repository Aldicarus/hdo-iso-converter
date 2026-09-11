"""Repintar un log no puede llevarte al principio.

El modal de trabajo se repinta cada 1,5 s y reconstruye su cuerpo entero, así
que el `.cmv40-log` se recrea con el scroll a cero. Había medio arreglo: si
estabas pegado al fondo, se te devolvía al fondo. Pero si habías subido a leer
—que es lo que se hace con un log de dos mil líneas— cada vuelta te mandaba
**arriba del todo**, y volver a donde estabas era casi imposible porque a los
1,5 s pasaba otra vez.

Es la misma trampa que la columna de trabajo con su historial: mientras el
contenido se sustituye la caja se queda vacía, el navegador recorta el
`scrollTop` al nuevo máximo —cero— y ya no lo devuelve.

Dos sitios lo hacían: el modal de trabajo (`workbar.js`) y el registro del
modal de pre-flight (`tab3.js`). Los que van añadiendo líneas —el log del
panel de Tab 3 y la consola de Tab 1— nunca lo tuvieron: `appendChild` no
toca el scroll.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_scroll_del_log -v
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
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import html, js_completo  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)


def _fn(nombre: str) -> str:
    i = JS.index(f"function {nombre}(")
    return JS[JS.rindex("\n", 0, i) + 1:JS.index("\n}\n", i) + 3]


def _node(guion: str) -> dict:
    r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True,
                       timeout=30)
    if r.returncode != 0:
        raise AssertionError(f"node falló:\n{r.stderr[:900]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElAnclaje(unittest.TestCase):
    """Las dos mitades del arreglo, que son dos comportamientos distintos."""

    def _restaurar(self, antes, despues_alto):
        guion = f"""
{_fn('anclajeDeLog')}
{_fn('restaurarAnclajeDeLog')}
const viejo = {json.dumps(antes)};
const nuevo = {{ scrollTop: 0, clientHeight: {antes['clientHeight']},
                 scrollHeight: {despues_alto} }};
restaurarAnclajeDeLog(nuevo, anclajeDeLog(viejo));
console.log(JSON.stringify({{ y: nuevo.scrollTop }}));
"""
        return _node(guion)["y"]

    def test_pegado_al_fondo_sigue_pegado(self):
        """El log es un directo: si lo estabas siguiendo, se sigue solo."""
        y = self._restaurar({"scrollTop": 900, "clientHeight": 300,
                             "scrollHeight": 1200}, 1500)
        self.assertEqual(y, 1500)

    def test_y_leyendo_por_la_mitad_te_quedas_donde_estabas(self):
        """Lo que faltaba: aquí se iba a cero en cada repintado."""
        y = self._restaurar({"scrollTop": 400, "clientHeight": 300,
                             "scrollHeight": 1200}, 1500)
        self.assertEqual(y, 400)

    def test_la_tolerancia_perdona_los_ultimos_pixeles(self):
        """Con líneas llegando rápido, «al fondo» nunca es exacto."""
        y = self._restaurar({"scrollTop": 890, "clientHeight": 300,
                             "scrollHeight": 1200}, 1500)
        self.assertEqual(y, 1500)

    def test_sin_log_previo_arranca_pegado(self):
        guion = (_fn('anclajeDeLog')
                 + "console.log(JSON.stringify(anclajeDeLog(null)));")
        self.assertTrue(_node(guion)["abajo"])


_LOG = [f"[20:1{i % 10}:0{i % 10}] línea número {i} del registro" for i in range(400)]

_ACTIVO = {
    "id": "p1", "sobre": "p1", "tab": "cmv40", "tipo": "fase_cmv40",
    "que": "Upgrade CMv4.0 · Predator (2026)", "titulo": "Predator (2026)",
    "poster": "", "fase": "extract", "fase_label": "Fase C",
    "paso": "Demuxing", "fase_n": 3, "fases_total": 8, "pct": 24,
    "pct_medido": True, "segundos": 2400, "eta_s": 7200, "eta_fuente": "modelo",
    "chips": [], "cancelable": True, "detalle": "cmv40",
}
_SESION = {
    "id": "p1", "output_mkv_name": "Predator (2026) [CMv4].mkv",
    "source_mkv_name": "Predator (2026).mkv", "phase": "target_provided",
    "running_phase": "extract", "output_log": _LOG,
    "phase_history": [{"phase": "extract", "status": "running",
                       "started_at": "2026-09-11T20:14:30+00:00"}],
    "auto_pipeline": True, "tmdb_info": {},
}


def _medir() -> dict:
    sonda = ("<style>*{transition:none!important;animation:none!important}</style>"
             "<script>window.__errores=[];"
             "window.addEventListener('error',e=>window.__errores.push("
             "(e.message||'')+' @ '+(e.filename||'').split('/').pop()"
             f"+':'+e.lineno));window.__T={json.dumps(_ACTIVO)};"
             f"window.__S={json.dumps(_SESION)};</script>")
    cuerpo = """
<pre id="__out"></pre>
<script>
(function () {
  const EST = {activo: window.__T, cola: [], interactivo: [], recientes: []};
  let extra = 0;
  window.apiFetch = async (url) => {
    if (url.startsWith('/api/trabajos')) return EST;
    if (url.startsWith('/api/cmv40/')) {
      // Cada vuelta trae líneas nuevas, como un log vivo.
      extra += 5;
      return {...window.__S, output_log: window.__S.output_log.concat(
        Array.from({length: extra}, (_, i) => `[20:20:0${i % 10}] nueva ${i}`))};
    }
    return null;
  };
  const log = () => document.querySelector('#trabajo-modal .cmv40-log');
  setTimeout(async () => {
    workbarEstado = EST;
    _workbarRender(workbarEstado);
    abrirDetalleDeTrabajo();
    for (let i = 0; i < 80 && !log(); i++) await new Promise(r => setTimeout(r, 50));
    await new Promise(r => setTimeout(r, 150));
    const out = {errores: window.__errores};
    // 1) Sube a leer por la mitad y deja que se repinte dos veces.
    log().scrollTop = 500;
    out.antes = log().scrollTop;
    await _trabajoModalRefrescar();
    await new Promise(r => setTimeout(r, 60));
    await _trabajoModalRefrescar();
    await new Promise(r => setTimeout(r, 60));
    out.leyendo = log().scrollTop;
    // 2) Bájate del todo: a partir de ahí tiene que seguir el directo.
    log().scrollTop = log().scrollHeight;
    await _trabajoModalRefrescar();
    await new Promise(r => setTimeout(r, 60));
    out.pegado = log().scrollTop;
    out.fondo = log().scrollHeight - log().clientHeight;
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 900);
})();
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
             "--window-size=1600,1000", "--virtual-time-budget=8000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    import html as _h
    return json.loads(_h.unescape(m.group(1)))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestElLogDelModalNoSalta(unittest.TestCase):
    """Con el modal de verdad, repintándose con líneas nuevas."""

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_se_abre_sin_errores(self):
        self.assertEqual(self.m["errores"], [])

    def test_leyendo_por_la_mitad_no_te_manda_arriba(self):
        self.assertEqual(self.m["antes"], 500, "no se pudo colocar el scroll")
        self.assertEqual(self.m["leyendo"], 500,
                         "cada repintado te devolvía al principio del log")

    def test_y_si_estabas_al_final_sigue_el_directo(self):
        self.assertGreaterEqual(self.m["pegado"], self.m["fondo"] - 2)


if __name__ == "__main__":
    unittest.main()
