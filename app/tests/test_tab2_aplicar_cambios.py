"""«Aplicar cambios» de Tab 2, ejecutado de verdad en Chrome.

El 2026-09-08 (`6750d4d`, al quitar el modal propio de Tab 2) se fueron
`let polling` y `_esperarCopiaEncolada`, y se quedaron sus dos usos. Como
`tab2.js` es `'use strict'`, el `polling = false` del `finally` LANZABA en
cada pulsación: el servidor aplicaba mkvpropedit y la interfaz moría sin el
aviso de éxito ni el re-análisis; con copia desde biblioteca, ni siquiera
esperaba al trabajo para pasar la pestaña a la copia. La cazó la auditoría de
la suite (2026-10-17) buscando funciones que se llaman y no existen: ningún
test ejecutaba `_doApplyMkvEdits`, y el guard de carga en Chrome no puede
verlo — un `ReferenceError` en una función que nadie llama al cargar no es un
error de carga.

Aquí se ejecuta en sus cuatro desenlaces con `apiFetch` sustituido: sin
copia, con copia, la copia hecha y mkvpropedit fallando, y cancelada.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_tab2_aplicar_cambios -v
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
          "window.addEventListener('unhandledrejection',e=>window.__errores.push("
          "'promesa: '+((e.reason&&e.reason.message)||e.reason)));"
          "window.fetch=()=>new Promise(()=>{});"
          "window.WebSocket=function(){this.close=()=>{};};</script>")

_CUERPO = """
<pre id="__out"></pre>
<script>
(function () {
  const out = {errores: [], casos: {}};
  const toasts = [];
  const llamadas = [];
  let responder = () => null;
  // Lo que sale a la red, sustituido; el resto (el panel, la sub-pestaña,
  // el modal común) es el código real.
  window.showToast = (m, tipo) => toasts.push(tipo);
  window.apiFetch = async (url, opts) => { llamadas.push(url); return responder(url, opts); };
  window.refrescarWorkbar = async () => {};
  window.refrescarMkvRecientes = () => {};

  const analisis = (nombre, ruta) => ({
    file_name: nombre, file_path: ruta, file_size_bytes: 42e9, duration_seconds: 7200,
    tracks: [{id: 1, type: 'audio', name: 'Castellano TrueHD Atmos 7.1',
              flag_default: true, flag_forced: false}],
    chapters: [],
  });

  async function caso(nombre, ruta, copia, aplicar, progreso) {
    openMkvProject(analisis(nombre, ruta));
    const p = mkvProject;
    // Después de abrir: lo que cuenta es lo que dice el flujo de aplicar.
    toasts.length = 0; llamadas.length = 0;
    const pasos = (progreso || []).slice();
    responder = (url) => {
      if (url === '/api/mkv/apply') return aplicar;
      if (url === '/api/mkv/apply/progress') return pasos.length > 1 ? pasos.shift() : pasos[0];
      if (url === '/api/mkv/analyze') return analisis(p.fileName, p.filePath);
      return null;
    };
    let excepcion = null;
    try { await _doApplyMkvEdits(copia); } catch (e) { excepcion = e.message; }
    return {excepcion, filePath: p.filePath, toasts: toasts.slice(),
            reanalizo: llamadas.includes('/api/mkv/analyze'),
            esperoAlJob: llamadas.includes('/api/mkv/apply/progress')};
  }

  setTimeout(async () => {
    try {
      out.casos.sinCopia = await caso('Dune.mkv', '/mnt/output/Dune.mkv', false,
        {ok: true, new_path: '/mnt/output/Dune.mkv', output: ''});
      out.casos.copia = await caso('Arrival.mkv', '/mnt/library/Arrival.mkv', true,
        {ok: true, queued: true},
        [{active: true, step: 'en_cola'}, {active: true, step: 'copying', pct: 40},
         {active: true, step: 'done', result: {ok: true, new_path: '/mnt/output/Arrival.mkv',
           new_file_path: '/mnt/output/Arrival.mkv', copied_from_library: true}}]);
      out.casos.falloTrasCopiar = await caso('Her.mkv', '/mnt/library/Her.mkv', true,
        {ok: true, queued: true},
        [{active: true, step: 'copying', pct: 99},
         {active: true, step: 'error', error: 'mkvpropedit falló (código 2)',
          result: {ok: false, error: 'mkvpropedit falló (código 2)',
                   new_file_path: '/mnt/output/Her.mkv', copied_from_library: true}}]);
      out.casos.falloCopiando = await caso('Heat.mkv', '/mnt/library/Heat.mkv', true,
        {ok: true, queued: true},
        [{active: true, step: 'error', error: 'disco lleno', result: null}]);
      out.casos.cancelada = await caso('Alien.mkv', '/mnt/library/Alien.mkv', true,
        {ok: true, queued: true},
        [{active: true, step: 'copying', pct: 10}, {active: true, step: 'cancelled'}]);
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
             "--window-size=1280,1000", "--virtual-time-budget=40000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m or not m.group(1).strip():
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    return json.loads(m.group(1))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestAplicarCambios(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()
        cls.c = cls.m["casos"]

    def test_cero_errores_de_js(self):
        self.assertEqual(self.m["errores"], [])

    def test_ningun_desenlace_lanza(self):
        for nombre, c in self.c.items():
            with self.subTest(nombre):
                self.assertIsNone(c["excepcion"])

    def test_sin_copia_avisa_y_reanaliza(self):
        c = self.c["sinCopia"]
        self.assertEqual(c["toasts"], ["success"])
        self.assertTrue(c["reanalizo"])
        self.assertFalse(c["esperoAlJob"], "una edición sin copia no espera a ninguna cola")

    def test_con_copia_espera_al_trabajo_y_pasa_la_pestana_a_la_copia(self):
        c = self.c["copia"]
        self.assertTrue(c["esperoAlJob"])
        self.assertEqual(c["filePath"], "/mnt/output/Arrival.mkv")
        self.assertTrue(c["reanalizo"])
        self.assertEqual(c["toasts"][-1], "success")

    def test_si_falla_mkvpropedit_tras_copiar_la_pestana_va_a_la_copia(self):
        """Quedándose en la biblioteca, reintentar daba 409 «ya existe»."""
        c = self.c["falloTrasCopiar"]
        self.assertEqual(c["filePath"], "/mnt/output/Her.mkv")
        self.assertEqual(c["toasts"][-1], "error")
        self.assertFalse(c["reanalizo"])

    def test_si_falla_la_copia_la_pestana_sigue_en_la_biblioteca(self):
        c = self.c["falloCopiando"]
        self.assertEqual(c["filePath"], "/mnt/library/Heat.mkv")
        self.assertEqual(c["toasts"][-1], "error")

    def test_cancelada_avisa_y_no_toca_la_pestana(self):
        c = self.c["cancelada"]
        self.assertEqual(c["filePath"], "/mnt/library/Alien.mkv")
        self.assertEqual(c["toasts"][-1], "warning")
        self.assertFalse(c["reanalizo"])


if __name__ == "__main__":
    unittest.main()
