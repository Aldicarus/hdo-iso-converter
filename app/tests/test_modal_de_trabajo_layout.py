"""El modal de detalle, medido en un navegador de verdad.

Los cuatro fallos que el usuario reportó al probarlo en el NAS eran TODOS de
geometría, y ninguno se veía leyendo el código:

- la caja usaba `class="modal"`, que **no existe** en esta aplicación (es
  `.modal-box`): sin fondo, sin bordes y sin centrar;
- sin tope de altura, las dos columnas crecían con su contenido —siete fases,
  dos mil líneas de log— y el pie con los botones quedaba bajo el borde
  inferior de la ventana. Eso es lo que se describió como «contenido
  descentrado y el log cortado por arriba y por abajo»;
- `.modal-header` y `.modal-icon` tampoco existen, así que el icono caía
  ENCIMA del título en vez de a su lado;
- `.cmv40-log` trae `max-height: 300px` de su uso en el panel de Tab 3, así
  que el log se quedaba en 300 px con medio modal en blanco debajo.

Un test que lea el fuente no distingue ninguno de los cuatro: el markup y el
CSS se leen bien en todos. Este abre `index.html` en Chrome con un trabajo
falso, abre el modal y **mide** el resultado.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_modal_de_trabajo_layout -v
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

VENTANA = (1600, 1000)

_LOG = [f"[20:1{i % 10}:0{i % 10}] línea de log número {i}" for i in range(400)]

_ACTIVOS = {
    "cmv40": {
        "id": "predator_2026_1", "sobre": "predator_2026_1", "tab": "cmv40",
        "tipo": "fase_cmv40", "que": "Fase C de Predator (2026)",
        "fase": "extract", "fase_label": "Extrayendo BL/EL", "fase_n": 3,
        "fases_total": 7, "pct": 41, "pct_medido": True, "segundos": 742,
        "eta_s": 1060, "eta_fuente": "medido", "cancelable": True,
        "detalle": "cmv40",
    },
    "rip": {
        "id": "dune_2024_1", "sobre": "dune_2024_1", "tab": "rip",
        "tipo": "rip", "que": "rip de Dune (2024)", "fase": "extract",
        "fase_label": "Extrayendo pistas", "fase_n": 2, "fases_total": 4,
        "pct": 63, "pct_medido": True, "segundos": 905, "eta_s": 531,
        "eta_fuente": "medido", "cancelable": True, "detalle": "rip",
    },
    "analisis_extendido": {
        "id": "aud-7f3", "sobre": "/mnt/output/Dune.mkv", "tab": "mkv",
        "tipo": "analisis_extendido", "que": "análisis extendido de Dune.mkv",
        "fase": "extract", "fase_label": "Extrayendo el RPU", "fase_n": 1,
        "fases_total": 3, "pct": 22, "pct_medido": True, "segundos": 180,
        "eta_s": 640, "eta_fuente": "medido", "cancelable": True,
        "detalle": "analisis_extendido",
    },
}

_SESION_CMV40 = {
    "id": "predator_2026_1",
    "output_mkv_name": "Predator (2026) [DV FEL CMv4].mkv",
    "source_mkv_name": "Predator (2026) UHD BluRay.mkv",
    "phase": "target_provided", "running_phase": "extract",
    "output_log": _LOG,
    "phase_history": [
        {"phase": "analyze_source", "status": "done", "elapsed_seconds": 862},
        {"phase": "extract", "status": "running"},
    ],
    "source_workflow": "p7_fel", "target_type": "trusted_p7_fel_final",
    "source_frame_count": 225177, "target_frame_count": 225177,
    "auto_pipeline": True, "target_trust_ok": True,
}

_SESION_RIP = {
    "id": "dune_2024_1", "mkv_name": "Dune (2024) [DV FEL].mkv",
    "iso_path": "/mnt/isos/Dune 2024 UHD.iso", "status": "running",
    "output_log": _LOG, "source_type": "iso",
    "execution_history": [{"phase_elapsed": {"mount": 9, "extract": 905}}],
}


def _medir(tipo: str) -> dict:
    """Abre el modal de ese tipo en Chrome y devuelve la geometría."""
    activo = _ACTIVOS[tipo]
    respuestas = {
        "trabajos": {"activo": activo, "cola": [
            {"id": "obsession_1", "sobre": "obsession_1", "tab": "cmv40",
             "tipo": "fase_cmv40", "que": "Fase F de Obsession", "posicion": 1}],
            "interactivo": [], "recientes": []},
        "cmv40": _SESION_CMV40,
        "rip": _SESION_RIP,
        "audit": {"active": True, "audit_id": "aud-7f3", "step": 1, "pct": 22,
                  "log_lines": _LOG, "steps_total": 3,
                  "file_name": "Dune (2024).mkv"},
    }
    sonda = ("<script>window.__errores=[];"
             "window.addEventListener('error',e=>window.__errores.push("
             "(e.message||'')+' @ '+(e.filename||'').split('/').pop()"
             "+':'+e.lineno));"
             f"window.__RESP={json.dumps(respuestas)};</script>")
    arranque = """
<pre id="__out"></pre>
<script>
(function () {
  window.apiFetch = async function (url) {
    const R = window.__RESP;
    if (url.startsWith('/api/trabajos')) return R.trabajos;
    if (url.startsWith('/api/cmv40/')) return R.cmv40;
    if (url.startsWith('/api/sessions/')) return R.rip;
    if (url.includes('quality-audit/progress')) return R.audit;
    return null;
  };
  const r = el => { const b = el && el.getBoundingClientRect();
    return b ? {t: b.top, l: b.left, w: b.width, h: b.height,
                b: b.bottom, r: b.right} : null; };
  setTimeout(async () => {
    workbarEstado = window.__RESP.trabajos;
    _workbarRender(workbarEstado);
    // `abrirDetalleDeTrabajo` NO es async: dispara la vista y vuelve. Sin
    // esperar a que la vista pinte, se mediría un modal vacío —y el test
    // saldría intermitente, que es peor que rojo.
    abrirDetalleDeTrabajo();
    for (let i = 0; i < 60 && !document.querySelector(
           '#trabajo-modal .cmv40-log, #trabajo-modal .trabajo-detalle-vacio'); i++) {
      await new Promise(r => setTimeout(r, 50));
    }
    await new Promise(r => setTimeout(r, 60));
    const caja = document.querySelector('#trabajo-modal .trabajo-modal-caja');
    const log = document.querySelector('#trabajo-modal .cmv40-log');
    const lateral = document.getElementById('trabajo-modal-lateral');
    document.getElementById('__out').textContent = JSON.stringify({
      errores: window.__errores,
      abierto: document.getElementById('trabajo-modal').classList.contains('open'),
      ventana: {w: window.innerWidth, h: window.innerHeight},
      caja: r(caja),
      fondo: caja ? getComputedStyle(caja).backgroundColor : null,
      lateral: r(lateral),
      lateralTexto: (lateral && lateral.textContent.trim().length) || 0,
      icono: r(document.getElementById('trabajo-modal-icono')),
      titulo: r(document.getElementById('trabajo-modal-titulo')),
      pie: r(document.querySelector('#trabajo-modal .modal-footer')),
      log: r(log),
      logScroll: log ? {alto: log.scrollHeight, visible: log.clientHeight,
                        pos: log.scrollTop} : null,
    });
  }, 1200);
})();
</script>
"""
    pagina = html().replace("</head>", sonda + "</head>")
    pagina = pagina.replace("</body>", arranque + "</body>")
    pagina = (pagina.replace('src="/static/', 'src="')
                    .replace('href="/static/', 'href="'))
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                      encoding="utf-8",
                                      dir=str(APP_DIR / "static"))
    tmp.write(pagina)
    tmp.close()
    try:
        salida = subprocess.run(
            [CHROME, "--headless", "--disable-gpu",
             "--allow-file-access-from-files", "--dump-dom",
             f"--window-size={VENTANA[0]},{VENTANA[1]}",
             "--virtual-time-budget=6000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', salida, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    import html as _h
    return json.loads(_h.unescape(m.group(1)))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestElModalCabeEnLaVentana(unittest.TestCase):
    """El fallo que se vio en el NAS: el pie quedaba fuera de la pantalla."""

    @classmethod
    def setUpClass(cls):
        cls.m = {t: _medir(t) for t in ("cmv40", "rip", "analisis_extendido")}

    def test_se_abre_sin_un_solo_error(self):
        for tipo, d in self.m.items():
            self.assertEqual(d["errores"], [], tipo)
            self.assertTrue(d["abierto"], f"{tipo}: el modal no se abrió")

    def test_la_caja_tiene_fondo(self):
        """`class="modal"` no existe: la caja salía transparente."""
        for tipo, d in self.m.items():
            fondo = d["fondo"] or ""
            self.assertNotIn("rgba(0, 0, 0, 0)", fondo,
                             f"{tipo}: la caja del modal es transparente")

    def test_la_caja_entera_cabe_en_la_ventana(self):
        for tipo, d in self.m.items():
            c, v = d["caja"], d["ventana"]
            self.assertIsNotNone(c, tipo)
            self.assertGreaterEqual(round(c["t"]), 0, f"{tipo}: se sale por arriba")
            self.assertLessEqual(round(c["b"]), v["h"],
                                 f"{tipo}: se sale por abajo {c['b']} > {v['h']}")
            self.assertLessEqual(round(c["r"]), v["w"], f"{tipo}: se sale por la derecha")

    def test_el_pie_con_los_botones_se_ve(self):
        """Cancelar y Cerrar viven ahí: fuera de la caja son inalcanzables."""
        for tipo, d in self.m.items():
            pie, c = d["pie"], d["caja"]
            self.assertIsNotNone(pie, tipo)
            self.assertLessEqual(round(pie["b"]), round(c["b"]) + 1,
                                 f"{tipo}: el pie cae por debajo de la caja")
            self.assertGreater(pie["h"], 0, f"{tipo}: el pie no ocupa nada")

    def test_el_icono_va_al_lado_del_titulo_no_encima(self):
        """`.modal-header` tampoco existía en el CSS."""
        for tipo, d in self.m.items():
            ic, ti = d["icono"], d["titulo"]
            self.assertLessEqual(round(ic["r"]), round(ti["l"]) + 1,
                                 f"{tipo}: el icono pisa el título")
            centro_ic = ic["t"] + ic["h"] / 2
            centro_ti = ti["t"] + ti["h"] / 2
            self.assertLess(abs(centro_ic - centro_ti), 14,
                            f"{tipo}: el icono y el título no están en la misma fila")


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestElLogSeVeEntero(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = {t: _medir(t) for t in ("cmv40", "rip", "analisis_extendido")}

    def test_el_log_no_se_queda_en_los_300px_de_tab3(self):
        """`.cmv40-log` trae `max-height: 300px` de su uso en el panel; aquí
        manda el alto de la caja o queda medio modal en blanco."""
        for tipo, d in self.m.items():
            self.assertGreater(d["log"]["h"], 320,
                               f"{tipo}: el log se quedó en el tope de Tab 3")

    def test_el_log_cabe_dentro_de_la_caja(self):
        """Lo que se veía cortado por arriba y por abajo."""
        for tipo, d in self.m.items():
            log, c = d["log"], d["caja"]
            self.assertGreaterEqual(round(log["t"]), round(c["t"]) - 1, tipo)
            self.assertLessEqual(round(log["b"]), round(c["b"]) + 1, tipo)

    def test_hace_scroll_por_dentro_y_arranca_por_el_final(self):
        """Es un directo: interesa la última línea, no la primera."""
        for tipo, d in self.m.items():
            s = d["logScroll"]
            self.assertGreater(s["alto"], s["visible"],
                               f"{tipo}: el log no tiene nada que desplazar")
            self.assertGreaterEqual(s["pos"], s["alto"] - s["visible"] - 24,
                                    f"{tipo}: el log no está al final")


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestLaColumnaLateral(unittest.TestCase):
    """El detalle de una fase CMv4.0 tenía la vista más completa de la
    aplicación —la timeline con las fases y sus tiempos— y el primer intento de
    unificar la bajó a un genérico de cabecera + barra + log. El plan decía lo
    contrario: que esa fuera el modelo."""

    @classmethod
    def setUpClass(cls):
        cls.m = {t: _medir(t) for t in ("cmv40", "rip", "analisis_extendido")}

    def test_cmv40_y_rip_traen_su_timeline(self):
        for tipo in ("cmv40", "rip"):
            d = self.m[tipo]
            self.assertGreater(d["lateralTexto"], 40,
                               f"{tipo}: el lateral está vacío")
            self.assertGreater(d["lateral"]["w"], 200,
                               f"{tipo}: el lateral no ocupa nada")

    def test_sin_timeline_el_modal_se_queda_a_una_columna(self):
        """Un trabajo sin fases no necesita el ancho de dos: dejarlo igual
        dejaba media pantalla en blanco."""
        d = self.m["analisis_extendido"]
        self.assertEqual(d["lateralTexto"], 0)
        self.assertLess(d["caja"]["w"], self.m["cmv40"]["caja"]["w"])


if __name__ == "__main__":
    unittest.main()
