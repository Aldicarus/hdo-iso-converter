"""El modal de trabajo tiene que verse EXACTAMENTE como el overlay que sustituye.

El overlay de ejecución de CMv4.0 era la vista más trabajada de la aplicación
—cartela, timeline con tiempos, bloque de progreso e isla de log a sangre— y al
unificarlo en un modal común se fue perdiendo por goteo: el marco blanco
alrededor del log, el aro que gira convertido en un chip estático, la cabecera
con dos nombres de fichero, los radios y las sombras de otra caja. Cada
diferencia salió de que un humano la viera y la contara; ninguna la habría
cazado un test que lea el CSS, porque las reglas se leen bien en los dos casos.

`fixtures_modal/golden_overlay_cmv40.json` son las propiedades computadas del
overlay REAL, medidas en Chrome sobre el commit anterior a su retirada
(`2b0c283^`). Este test renderiza el modal de hoy y compara elemento a
elemento. **Es un golden, no una transcripción**: nadie escribió esos valores a
mano.

Lo que NO se compara es la geometría: depende del tamaño de la ventana y de los
textos del fixture (el overlay se midió con el ETA vacío). Lo que importa —
fondos, tipografías, paddings, radios, sombras, quién scrolla y qué se anima—
son propiedades.

Si un cambio de diseño es deliberado, hay que regenerar el golden Y explicar en
el commit por qué el modal deja de parecerse al overlay.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_modal_calca_al_overlay -v
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

from frontend_sources import html  # noqa: E402

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)

GOLDEN = Path(__file__).parent / "fixtures_modal" / "golden_overlay_cmv40.json"

# Los mismos que se midieron sobre el overlay.
PROPS = ["backgroundColor", "color", "fontSize", "fontWeight", "padding",
         "border", "borderRadius", "margin", "overflow", "overflowY",
         "display", "flex", "minHeight", "boxShadow", "animationName",
         "letterSpacing", "lineHeight", "gap", "alignItems"]

# Dónde vive cada pieza en el modal de hoy. El nombre de la izquierda es el que
# usa el golden; el de la derecha, el selector actual.
SELECTORES = {
    "caja": ".trabajo-modal-caja",
    "columna": ".trabajo-modal-lateral",
    "cartel": ".trabajo-cartel",
    "poster": ".trabajo-cartel-poster",
    "cartelTitulo": ".trabajo-cartel-titulo",
    "cartelMeta": ".trabajo-cartel-meta",
    "timeline": "#trabajo-modal-timeline",
    "principal": ".trabajo-modal-principal",
    "cabecera": ".modal-header",
    "spinner": "#trabajo-modal-icono",
    "titulo": "#trabajo-modal-titulo",
    "sub": "#trabajo-modal-sub",
    "progreso": ".trabajo-progreso",
    "paso": "#trabajo-modal-paso",
    "eta": "#trabajo-modal-eta",
    "pct": "#trabajo-modal-pct",
    "track": ".trabajo-progreso-track",
    "log": ".cmv40-log",
    "pasos": ".cmv40-tl-steps",
}

_LOG = [f"[20:1{i % 10}:0{i % 10}] línea {i}" for i in range(300)]

_SESION = {
    "id": "p1", "output_mkv_name": "Predator (2026) [DV FEL CMv4].mkv",
    "source_mkv_name": "Predator (2026) UHD BluRay.mkv",
    "phase": "target_provided", "running_phase": "extract",
    "output_log": _LOG,
    "phase_history": [
        {"phase": "analyze_source", "status": "done", "elapsed_seconds": 862,
         "started_at": "2026-09-08T20:00:00+00:00",
         "finished_at": "2026-09-08T20:14:22+00:00"},
        {"phase": "extract", "status": "running",
         "started_at": "2026-09-08T20:14:30+00:00"}],
    "source_workflow": "p7_fel", "target_type": "trusted_p7_fel_final",
    "source_frame_count": 225177, "target_frame_count": 225177,
    "auto_pipeline": True, "target_trust_ok": True,
    "target_preflight_ok": True, "source_preflight_ok": True,
    "tmdb_info": {"title": "Predator: Tierra de Ojos", "year": 2026,
                  "runtime_minutes": 107, "genres": ["Acción"],
                  "poster_url": ""},
    "last_progress": {"pct": 41, "eta_s": 1020, "label": "Demuxing BL/EL"},
}

_ACTIVO = {
    "id": "p1", "sobre": "p1", "tab": "cmv40", "tipo": "fase_cmv40",
    "que": "Fase C de Predator", "fase": "extract",
    "fase_label": "Fase C — Extrayendo BL/EL y datos per-frame",
    "paso": "Demuxing BL/EL", "fase_n": 3, "fases_total": 7, "pct": 41,
    "pct_medido": True, "segundos": 742, "eta_s": 1020,
    "eta_fuente": "medido", "cancelable": True, "detalle": "cmv40",
}


def _medir_modal_actual() -> dict:
    sonda = ("<script>window.__errores=[];"
             "window.addEventListener('error',e=>window.__errores.push("
             "(e.message||'')+' @ '+(e.filename||'').split('/').pop()"
             f"+':'+e.lineno));window.__S={json.dumps(_SESION)};</script>")
    cuerpo = """
<pre id="__out"></pre>
<script>
const PROPS = __PROPS__, SEL = __SEL__;
(function () {
  const ACT = __ACT__;
  window.apiFetch = async (url) => {
    if (url.startsWith('/api/trabajos'))
      return {activo: ACT, cola: [], interactivo: [], recientes: []};
    if (url.startsWith('/api/cmv40/')) return window.__S;
    return null;
  };
  setTimeout(async () => {
    workbarEstado = {activo: ACT, cola: [], interactivo: [], recientes: []};
    _workbarRender(workbarEstado);
    abrirDetalleDeTrabajo();
    for (let i = 0; i < 80 && !document.querySelector('#trabajo-modal .cmv40-log'); i++)
      await new Promise(r => setTimeout(r, 50));
    await new Promise(r => setTimeout(r, 150));
    const raiz = document.getElementById('trabajo-modal');
    const out = {errores: window.__errores};
    for (const [k, s] of Object.entries(SEL)) {
      const el = raiz.querySelector(s);
      if (!el) { out[k] = null; continue; }
      const cs = getComputedStyle(el);
      out[k] = {};
      for (const p of PROPS) out[k][p] = cs[p];
    }
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 1400);
})();
</script>
"""
    cuerpo = (cuerpo.replace("__PROPS__", json.dumps(PROPS))
                    .replace("__SEL__", json.dumps(SELECTORES))
                    .replace("__ACT__", json.dumps(_ACTIVO)))
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
             "--window-size=1600,1000", "--virtual-time-budget=7000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    import html as _h
    return json.loads(_h.unescape(m.group(1)))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
@unittest.skipUnless(GOLDEN.exists(), "sin golden del overlay")
class TestElModalSeVeComoElOverlay(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        cls.actual = _medir_modal_actual()

    def test_se_abre_sin_errores(self):
        self.assertEqual(self.actual["errores"], [])

    def test_no_falta_ninguna_pieza(self):
        faltan = [k for k in self.golden if self.actual.get(k) is None]
        self.assertEqual(faltan, [], "el modal ya no tiene estas piezas")

    def test_cada_pieza_calca_al_overlay(self):
        for nombre, esperado in self.golden.items():
            real = self.actual.get(nombre)
            if real is None:
                continue        # lo cubre el test de arriba
            with self.subTest(pieza=nombre):
                difs = {p: (esperado[p], real[p])
                        for p in PROPS if esperado[p] != real[p]}
                self.assertEqual(
                    difs, {},
                    f"«{nombre}» ya no se ve como en el overlay. Si el cambio "
                    f"es deliberado, regenera el golden y explica en el commit "
                    f"por qué el modal deja de parecerse.")

    def test_lo_que_de_verdad_se_perdio_y_volvio(self):
        """Los cuatro que el usuario tuvo que reportar uno a uno. Redundante
        con el anterior a propósito: si el golden se regenera mal, estos
        siguen diciendo qué se rompió."""
        a = self.actual
        # El log a sangre, sin marco blanco alrededor.
        self.assertEqual(a["log"]["borderRadius"], "0px")
        self.assertEqual(a["log"]["padding"], "12px 16px")
        self.assertEqual(a["principal"]["padding"], "0px")
        # El aro que gira, no un chip quieto.
        self.assertEqual(a["spinner"]["animationName"], "cmv40-spin")
        self.assertEqual(a["spinner"]["borderRadius"], "50%")
        # El bloque de progreso pegado arriba y abajo.
        self.assertEqual(a["progreso"]["borderRadius"], "0px")
        self.assertEqual(a["progreso"]["margin"], "0px")
        # Quién scrolla: la lista de fases, no el hueco que la contiene.
        self.assertEqual(a["timeline"]["overflow"], "hidden")


if __name__ == "__main__":
    unittest.main()
