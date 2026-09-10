"""Qué número va en cada sitio: el del trabajo y el de la fase no se mezclan.

Una conversión CMv4.0 tiene DOS progresos y los dos hacen falta. El del
**trabajo** contesta «¿cuánto queda de la conversión?» y el de la **fase**,
«¿cuánto le queda al demux que estoy leyendo?». Durante veinte minutos de
Fase C el primero apenas se mueve y el segundo va del 0 al 100.

Se han confundido en las dos direcciones. Primero la tarjeta de la columna
enseñaba el de la fase, así que decía 90 % con el trabajo por el 20 %; al
arreglarlo, el bloque de encima del log —que es de la fase— pasó a enseñar el
total, y quedó clavado durante toda la fase.

El reparto, que es lo que este fichero fija:

| dónde                                   | qué |
|-----------------------------------------|-----|
| tarjeta de la columna de trabajo        | el del TRABAJO |
| modal · columna izquierda, bajo la cartela | el del TRABAJO |
| modal · bloque pegado al log            | el de la FASE  |

Se mide **en Chrome**, con dos juegos de números que no se pueden confundir
(24 % del trabajo contra 90 % de la fase): leer el código no lo demostraría,
porque los dos se leen igual de bien estén cruzados o no.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_dos_niveles_de_progreso -v
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

from frontend_sources import html  # noqa: E402

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)

# Números que no se pueden confundir: si un sitio enseña el del otro, se ve.
TRABAJO = {"pct": 24, "segundos": 2400, "eta_s": 7200}     # 24 % · 40:00 · 2 h
FASE    = {"pct": 90, "segundos": 300,  "eta_s": 120}      # 90 % ·  5:00 · 2 min

_ACTIVO = {
    "id": "p1", "sobre": "p1", "tab": "cmv40", "tipo": "fase_cmv40",
    "que": "Upgrade CMv4.0 · Predator (2026)",
    "titulo": "Predator (2026)", "poster": "",
    "fase": "extract", "fase_label": "Fase C — Extrayendo BL/EL",
    "paso": "Demuxing BL/EL", "fase_n": 3, "fases_total": 7,
    "pct": TRABAJO["pct"], "pct_medido": True,
    "segundos": TRABAJO["segundos"], "eta_s": TRABAJO["eta_s"],
    "eta_fuente": "modelo",
    "fase_progreso": {"pct": FASE["pct"], "pct_medido": True,
                      "segundos": FASE["segundos"], "eta_s": FASE["eta_s"],
                      "eta_fuente": "medido"},
    "chips": [], "cancelable": True, "detalle": "cmv40",
}

_SESION = {
    "id": "p1", "output_mkv_name": "Predator (2026) [DV FEL CMv4].mkv",
    "source_mkv_name": "Predator (2026) UHD BluRay.mkv",
    "phase": "target_provided", "running_phase": "extract",
    "output_log": ["[20:14:30] ━━━ Fase C ━━━"],
    "phase_history": [
        {"phase": "analyze_source", "status": "done", "elapsed_seconds": 862,
         "started_at": "2026-09-11T20:00:00+00:00",
         "finished_at": "2026-09-11T20:14:22+00:00"},
        {"phase": "extract", "status": "running",
         "started_at": "2026-09-11T20:14:30+00:00"}],
    "source_workflow": "p7_fel", "target_type": "trusted_p7_fel_final",
    "source_frame_count": 225177, "target_frame_count": 225177,
    "auto_pipeline": True, "target_trust_ok": True,
    "target_preflight_ok": True, "source_preflight_ok": True,
    "tmdb_info": {}, "last_progress": {"pct": FASE["pct"], "job_pct": TRABAJO["pct"]},
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
  window.apiFetch = async (url) => {
    if (url.startsWith('/api/trabajos')) return EST;
    if (url.startsWith('/api/cmv40/')) return window.__S;
    return null;
  };
  const txt = s => (document.querySelector(s) || {}).textContent || '';
  setTimeout(async () => {
    workbarEstado = EST;
    _workbarRender(workbarEstado);
    await new Promise(r => setTimeout(r, 60));
    const tarjeta = document.getElementById('workbar-body').textContent;
    abrirDetalleDeTrabajo();
    for (let i = 0; i < 80 && !document.querySelector('#trabajo-modal .cmv40-tl-steps'); i++)
      await new Promise(r => setTimeout(r, 50));
    await new Promise(r => setTimeout(r, 200));
    document.getElementById('__out').textContent = JSON.stringify({
      errores: window.__errores,
      tarjeta,
      // El bloque pegado al log.
      log_pct:   txt('#trabajo-modal-pct'),
      log_eta:   txt('#trabajo-modal-eta'),
      log_reloj: txt('#trabajo-modal-tiempos'),
      log_barra: (document.getElementById('trabajo-modal-barra') || {}).style?.width,
      // La columna izquierda, bajo la cartela.
      izq_pct:   txt('#trabajo-modal .cmv40-tl-progress-pct'),
      izq_resta: txt('#trabajo-modal .cmv40-tl-timer-remaining'),
    });
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
class TestCadaNumeroEnSuSitio(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_se_pinta_sin_errores(self):
        self.assertEqual(self.m["errores"], [])

    def test_la_tarjeta_de_la_columna_lleva_el_del_TRABAJO(self):
        self.assertIn("24 %", self.m["tarjeta"])
        self.assertNotIn("90 %", self.m["tarjeta"])

    def test_el_bloque_del_log_lleva_el_de_la_FASE(self):
        self.assertEqual(self.m["log_pct"], "90%")
        self.assertEqual(self.m["log_barra"], "90%")

    def test_y_su_restante_y_su_reloj_tambien(self):
        """2 min y 5:00 son de la fase; 2 h y 40:00, del trabajo."""
        self.assertIn("2 min", self.m["log_eta"])
        self.assertNotIn("2 h", self.m["log_eta"])
        self.assertIn("5 min", self.m["log_reloj"])
        self.assertNotIn("40 min", self.m["log_reloj"])

    def test_el_restante_de_la_fase_va_sin_aproximar(self):
        """Sale del ritmo real de la fase, no de un reparto por pesos: el
        «(aprox.)» es del total."""
        self.assertNotIn("aprox", self.m["log_eta"])

    def test_la_columna_izquierda_lleva_el_del_TRABAJO(self):
        self.assertIn("24%", self.m["izq_pct"])
        self.assertNotIn("90%", self.m["izq_pct"])

    def test_y_los_dos_sitios_del_total_dicen_LO_MISMO(self):
        """Es la queja de la que sale todo esto: tres cifras distintas para la
        misma pregunta. El de la izquierda salía del escalonado por fases
        (2 de 7 = 29 %) mientras la tarjeta enseñaba el `job_pct`."""
        self.assertIn("24%", self.m["izq_pct"])
        self.assertIn("24 %", self.m["tarjeta"])
        # El mismo `eta_s` (7200 s), aunque cada sitio lo escriba a su manera:
        # «Restante 2 h 0 min» en la tarjeta y «~02:00:00 restantes» aquí.
        self.assertIn("02:00:00", self.m["izq_resta"])


if __name__ == "__main__":
    unittest.main()
