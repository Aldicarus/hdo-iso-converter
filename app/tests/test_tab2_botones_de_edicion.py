"""«Aplicar» y «Deshacer» sólo se encienden cuando hay algo que guardar.

Al abrir un MKV los dos salían activos sin haber tocado nada: aplicar no
habría hecho más que reescribir las mismas cabeceras y deshacer no tenía qué
deshacer. Reportado el 2026-09-25, comparándolo con Tab 1, donde el botón de
ejecutar sí dice en qué estado está.

Van con el MISMO estado que el punto de la pestaña y se pintan en la misma
función: si se pintaran por separado podrían decir cosas distintas del mismo
proyecto.

Y el pie pierde el botón «Cerrar», heredado de cuando Tab 2 no tenía
pestañas. Hoy se cierra por la ✕ de la sub-pestaña, igual que en las otras
dos; un segundo botón para lo mismo, en rojo y al lado del primario, invitaba
a pulsarlo por error. Con él se fue `closeMkvEditor`, que se quedaba sin
llamador.

Se mide en Chrome y no con el DOM falso del arnés de node: ese devuelve un
objeto para CUALQUIER id, así que el test pasaría con los botones
inexistentes — comprobado antes de escribir esto.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_tab2_botones_de_edicion -v
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
  const analisis = (nombre, ruta) => ({
    file_name: nombre, file_path: ruta, file_size_bytes: 42e9,
    duration_seconds: 7200, tracks: [], chapters: [],
  });
  const foto = pid => {
    const b = id => document.getElementById(`${id}-${pid}`);
    return {
      apply: b('mkv-apply-btn') ? b('mkv-apply-btn').disabled : null,
      undo: b('mkv-undo-btn') ? b('mkv-undo-btn').disabled : null,
      punto: b('mkv-unsaved-dot')
        ? b('mkv-unsaved-dot').style.display : null,
    };
  };

  setTimeout(() => {
    const out = {errores: []};
    try {
      openMkvProject(analisis('Dune.mkv', '/mnt/output/Dune.mkv'));
      const p = openMkvProjects[0];
      out.alAbrir = foto(p.id);

      _mkvMarkDirty(p);
      out.trasEditar = foto(p.id);

      _mkvClearDirty(p);
      out.trasAplicar = foto(p.id);

      // Reabrir el mismo fichero lo deja limpio: es el camino por el que
      // `openMkvProject` pasa cuando la pestaña ya existe.
      _mkvMarkDirty(p);
      openMkvProject(analisis('Dune.mkv', '/mnt/output/Dune.mkv'));
      out.alReabrir = foto(p.id);

      // El pie: qué botones quedan.
      const pie = document.querySelector(`#mkv-panel-${p.id} .mkv-edit-panel-inner`)
        || document.getElementById(`mkv-panel-${p.id}`);
      out.acciones = [...pie.querySelectorAll('button[onclick]')]
        .map(b => b.getAttribute('onclick'))
        .filter(o => /showRawMkvData|undoMkvEdits|applyMkvEdits|closeMkvEditor/.test(o));
      out.hayCloseMkvEditor = typeof closeMkvEditor;
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
class BotonesCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_cero_errores_de_js(self):
        self.assertEqual(self.m["errores"], [])


class TestSoloSeEncendenConCambios(BotonesCase):

    def test_al_abrir_estan_apagados(self):
        # Los dos existen —si `getElementById` fallara serían `null` y el
        # resto del fichero no probaría nada— y los dos están apagados.
        self.assertEqual(self.m["alAbrir"], {"apply": True, "undo": True,
                                             "punto": "none"})

    def test_una_edicion_los_enciende(self):
        self.assertEqual(self.m["trasEditar"], {"apply": False, "undo": False,
                                                "punto": "inline"})

    def test_y_aplicar_los_vuelve_a_apagar(self):
        self.assertEqual(self.m["trasAplicar"], {"apply": True, "undo": True,
                                                 "punto": "none"})

    def test_reabrir_el_mismo_mkv_tambien(self):
        self.assertEqual(self.m["alReabrir"]["apply"], True)

    def test_el_punto_y_los_botones_no_pueden_discrepar(self):
        # Se pintan en la misma función justamente por esto.
        for foto in ("alAbrir", "trasEditar", "trasAplicar", "alReabrir"):
            with self.subTest(foto):
                f = self.m[foto]
                self.assertEqual(f["apply"], f["undo"])
                self.assertEqual(f["apply"], f["punto"] == "none")


class TestElPieYaNoTieneCerrar(BotonesCase):

    def test_quedan_tres_acciones_y_ninguna_es_cerrar(self):
        self.assertEqual(self.m["acciones"],
                         ["showRawMkvData()", "undoMkvEdits()",
                          "applyMkvEdits()"])

    def test_y_la_funcion_se_fue_con_el_boton(self):
        # Una función sin llamador es código muerto, y ésta además dejaba
        # una segunda vía de cerrar que no se parecía a las otras pestañas.
        self.assertEqual(self.m["hayCloseMkvEditor"], "undefined")


if __name__ == "__main__":
    unittest.main()
