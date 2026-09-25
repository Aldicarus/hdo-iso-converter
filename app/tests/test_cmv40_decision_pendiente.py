"""Con una decisión pendiente hay UN camino, y va donde se explica.

El proyecto de Drive quedó parado pidiendo confirmación por divergencias
—`awaiting_critical_ack`, con L6 a 611 nits y L1 al 50,7 %— y el panel
ofrecía **dos caminos a la vez**: el banner de «Cambiar target / Continuar
igualmente» y, más abajo, la Fase C con su botón de extraer BL/EL. La Fase C
depende de esa decisión: lo que haga —o si llega a hacerse— cambia según lo
que el usuario responda. Reportado el 2026-09-25.

Y el banner salía **arriba del todo**, con el argumento de que un pause-point
tiene que verse. Pero los números que lo justifican viven en la card 🛡️
Validaciones, mucho más abajo: había que decidir arriba leyendo abajo. Hoy va
entre esa card y la Fase C. Sigue fuera de una card colapsable —va ENTRE
cards, no dentro—, que era la otra mitad de aquel argumento y esa sí se
mantiene.

Lo que este fichero fija:

- con la decisión pendiente, la fase que tocaría sale **bloqueada** y sin
  botón de arrancar, y las posteriores también;
- lo ya hecho sigue marcado como hecho: eso no lo cambia ninguna decisión;
- el banner va **después** de la card de validaciones y **antes** de la
  Fase C, en ese orden exacto;
- y sin decisión pendiente no cambia nada: la fase que toca vuelve a estar
  activa y el banner no aparece.

Los datos son los del proyecto real, incluidos los dos gates que fallaron.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_decision_pendiente -v
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

# El proyecto de Drive, tal como quedó.
SESION = {
    "id": "cmv40_Drive_2011_1790316997",
    "source_mkv_path": "/mnt/output/Drive (2011).mkv",
    "source_mkv_name": "Drive (2011).mkv",
    "output_mkv_name": "Drive (2011) [DV FEL].mkv",
    "phase": "target_provided",
    "running_phase": None,
    "archived": False,
    "error_message": "",
    "target_type": "trusted_p7_fel_final",
    "target_trust_ok": False,
    "source_workflow": "p7_fel",
    "awaiting_critical_ack": True,
    "critical_gate_failures": [
        {"gate": "l6_div", "severity": "ack_required", "nits_diff": 611,
         "why": "MaxCLL estático diverge 611 nits (umbral 200)."},
        {"gate": "l1_div", "severity": "ack_required",
         "why": "Brillo medio diverge 50.7% (umbral 20%)."},
    ],
    "target_trust_gates": {
        "frames": {"ok": True}, "cm_version": {"ok": True},
        "has_l8": {"ok": True}, "l5_div": {"ok": True},
        "l6_div": {"ok": False, "severity": "ack_required"},
        "l1_div": {"ok": False, "severity": "ack_required"},
    },
}

_SONDA = ("<script>window.__errores=[];"
          "window.addEventListener('error',e=>window.__errores.push("
          "(e.message||'')+' @ '+(e.filename||'').split('/').pop()+':'+e.lineno));"
          "window.fetch=()=>new Promise(()=>{});"
          "window.WebSocket=function(){this.close=()=>{};};</script>")

_CUERPO = """
<pre id="__out"></pre>
<script>
(function () {
  // Los bloques del panel en el orden en que se pintan, leídos del DOM.
  // Las clases son las reales: `cmv40-fase-{done|active|pending}` para una
  // fase, `cmv40-gate-card` para las validaciones y `cmv40-card-ack-required`
  // para el banner de la decisión.
  const leer = pid => {
    const cont = document.getElementById(`cmv40-active-phase-${pid}`);
    return [...cont.children].map(d => ({
      cls: d.className,
      ack: d.classList.contains('cmv40-card-ack-required'),
      gate: d.classList.contains('cmv40-gate-card'),
      fase: d.classList.contains('cmv40-fase-card'),
      estado: (d.className.match(/cmv40-fase-(done|active|pending)/) || [])[1] || '',
      titulo: (d.textContent || '').trim().split('\\n')[0],
      // Los botones de acción que la card ofrece, sin filtrar por nombre:
      // lo que importa es que con la decisión pendiente no haya ninguno.
      acciones: [...d.querySelectorAll('button[onclick]')]
        .map(b => b.getAttribute('onclick')),
    }));
  };

  setTimeout(() => {
    const out = {errores: []};
    try {
      const abrir = (s) => {
        // Sin limpiar, el segundo caso deja DOS paneles con el mismo id y
        // `getElementById` devuelve el viejo: el test mediría el anterior.
        document.querySelectorAll('#cmv40-subtab-content > .cmv40-panel')
          .forEach(n => n.remove());
        openCMv40Projects.length = 0;
        const p = {id: 'p1', session: JSON.parse(JSON.stringify(s)),
                   expandedPhases: {}, ws: null};
        openCMv40Projects.push(p);
        activeCMv40SubTabId = 'p1';
        _createCMv40Panel(p);
        _renderCMv40ActivePhase(p);
        return leer('p1');
      };

      out.conAck = abrir(window.__S);
      const libre = JSON.parse(JSON.stringify(window.__S));
      libre.awaiting_critical_ack = false;
      libre.critical_gate_failures = [];
      out.sinAck = abrir(libre);

      // El OTRO pause-point: el pre-flight pidiendo mantener o inyectar.
      // Aquí el proyecto está en `created` y la que sobraba era la Fase A.
      const pf = JSON.parse(JSON.stringify(window.__S));
      pf.phase = 'created';
      pf.awaiting_critical_ack = false;
      pf.critical_gate_failures = [];
      pf.preflight_decision = 'ask_tone_mapping';
      pf.recommended_action = 'keep';
      pf.relato = {situacion: 'esperando_decision'};
      out.conPreflight = abrir(pf);
      // SÓLO el relato, sin los campos crudos: es lo que llega del
      // servidor y lo que el frontend tiene que leer.
      const soloRelato = JSON.parse(JSON.stringify(pf));
      delete soloRelato.preflight_decision;
      delete soloRelato.awaiting_critical_ack;
      out.soloRelato = abrir(soloRelato);
      // SÓLO los campos crudos, sin relato: el respaldo, para una sesión
      // cacheada de antes o el summary del sidebar.
      const soloCrudo = JSON.parse(JSON.stringify(pf));
      delete soloCrudo.relato;
      out.soloCrudo = abrir(soloCrudo);
      // Y el mismo, ya resuelto.
      const resuelto = JSON.parse(JSON.stringify(pf));
      resuelto.preflight_decision = 'ok';
      resuelto.relato = {situacion: 'preparando'};
      out.preflightResuelto = abrir(resuelto);
    } catch (e) { out.errores.push('EXCEPCIÓN: ' + e.message); }
    out.errores = out.errores.concat(window.__errores);
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 700);
})();
</script>
"""


def _medir() -> dict:
    pagina = html().replace("</head>", _SONDA + stub_catalogo_es() + "</head>")
    datos = f"<script>window.__S={json.dumps(SESION)};</script>"
    pagina = pagina.replace("</body>", datos + _CUERPO + "</body>")
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
             "--window-size=1280,1400", "--virtual-time-budget=7000", tmp.name],
            capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    return json.loads(m.group(1))


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class DecisionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _medir()

    def test_cero_errores_de_js(self):
        self.assertEqual(self.m["errores"], [])

    def bloques(self, clave="conAck"):
        return self.m[clave]


class TestSoloHayUnCamino(DecisionCase):

    def test_el_panel_pinta_el_banner_de_decision(self):
        self.assertEqual(sum(1 for b in self.bloques() if b["ack"]), 1,
                         "debe haber exactamente un banner de decisión")

    def test_y_ninguna_fase_ofrece_arrancar(self):
        # Era el defecto: el banner y, más abajo, la Fase C con su botón.
        ofrecen = [b["titulo"] for b in self.bloques()
                   if b["fase"] and b["acciones"]]
        self.assertEqual(ofrecen, [], f"fases accionables con la decisión "
                                      f"pendiente: {ofrecen}")

    def test_ninguna_card_queda_activa(self):
        activas = [b["titulo"] for b in self.bloques()
                   if b["estado"] == "active"]
        self.assertEqual(activas, [])

    def test_pero_lo_ya_hecho_sigue_hecho(self):
        # Ninguna decisión cambia lo que ya corrió: A y B siguen completas.
        self.assertEqual(sum(1 for b in self.bloques()
                             if b["estado"] == "done"), 2)


class TestLaDecisionVaDondeSeExplica(DecisionCase):

    def _indices(self):
        bl = self.bloques()
        ack = next(i for i, b in enumerate(bl) if b["ack"])
        # La card de validaciones es la que va justo antes del banner; se
        # localiza por su clase de gate, no por su título traducido.
        gate = max((i for i, b in enumerate(bl)
                    if b["gate"] and i < ack), default=-1)
        return bl, ack, gate

    def test_el_banner_va_despues_de_las_validaciones(self):
        _, ack, gate = self._indices()
        self.assertNotEqual(gate, -1, "no se encontró la card de validaciones")
        self.assertEqual(ack, gate + 1,
                         "el banner tiene que ir pegado a los números que lo "
                         "justifican, no arriba del todo")

    def test_y_ya_no_es_el_primer_bloque_del_panel(self):
        _, ack, _ = self._indices()
        self.assertGreater(ack, 0)


class TestElPausePointDelPreflight(DecisionCase):
    """El mismo defecto en otro sitio: las opciones de «mantener el MKV o
    inyectar RPU» convivían con el botón de analizar de la Fase A.

    La condición la resuelve el SERVIDOR —`relato.situacion` ya valía
    `esperando_decision` para este caso— y el frontend no la miraba: tenía
    su propio predicado, que sólo conocía el ACK. Reportado el 2026-09-25.
    """

    def test_ninguna_fase_ofrece_arrancar(self):
        ofrecen = [b["titulo"] for b in self.bloques("conPreflight")
                   if b["fase"] and b["acciones"]]
        self.assertEqual(ofrecen, [], f"con la decisión del pre-flight "
                                      f"pendiente se ofrece: {ofrecen}")

    def test_ni_la_fase_a(self):
        # La que el usuario vio: `created` deja la Fase A activa.
        activas = [b["titulo"] for b in self.bloques("conPreflight")
                   if b["estado"] == "active"]
        self.assertEqual(activas, [])

    def test_basta_con_el_relato(self):
        """Sin los campos crudos: es lo que manda, y es lo que no se leía."""
        ofrecen = [b["titulo"] for b in self.bloques("soloRelato")
                   if b["fase"] and b["acciones"]]
        self.assertEqual(ofrecen, [])

    def test_y_el_respaldo_cubre_cuando_el_relato_no_llega(self):
        """Sin relato —una sesión cacheada de antes, el summary—, los campos
        crudos tienen que dar la misma respuesta."""
        ofrecen = [b["titulo"] for b in self.bloques("soloCrudo")
                   if b["fase"] and b["acciones"]]
        self.assertEqual(ofrecen, [])

    def test_y_al_resolverla_vuelve_a_ofrecerse(self):
        activas = [b for b in self.bloques("preflightResuelto")
                   if b["estado"] == "active"]
        self.assertEqual(len(activas), 1)
        self.assertTrue(activas[0]["acciones"])


class TestSinDecisionNoCambiaNada(DecisionCase):

    def test_no_hay_banner(self):
        self.assertEqual(sum(1 for b in self.bloques("sinAck") if b["ack"]), 0)

    def test_y_la_fase_que_toca_vuelve_a_estar_activa(self):
        activas = [b for b in self.bloques("sinAck") if b["estado"] == "active"]
        self.assertEqual(len(activas), 1, "debe haber una y sólo una fase activa")

    def test_y_ofrece_arrancar(self):
        activa = next(b for b in self.bloques("sinAck")
                      if b["estado"] == "active")
        self.assertTrue(activa["acciones"],
                        "la fase activa tiene que poder lanzarse")


class TestElServidorResuelveLasDosEsperas(unittest.TestCase):
    """`_situacion` de `cmv40_relato`, que es de donde sale la condición.

    El ACK faltaba: un proyecto parado esperando que el usuario confirmara
    una degradación se contaba como «preparando», así que ni la columna de
    trabajo ni la tarjeta decían que le tocaba a él.
    """

    def _situacion(self, **campos):
        from models import CMv40Session
        from phases.cmv40_relato import _situacion
        base = dict(id="p1", source_mkv_path="/mnt/output/x.mkv",
                    source_mkv_name="x.mkv", phase="created")
        return _situacion(CMv40Session(**{**base, **campos}), en_cola=None)

    def test_la_decision_del_preflight(self):
        self.assertEqual(self._situacion(preflight_decision="ask_tone_mapping"),
                         "esperando_decision")

    def test_y_la_confirmacion_de_una_degradacion(self):
        # Con sus gates: el modelo tiene un invariante que levanta el
        # bloqueo si no hay ninguno —«no habría banner con el que salir»—,
        # así que un fixture sin ellos no reproduce nada.
        self.assertEqual(self._situacion(
            awaiting_critical_ack=True,
            critical_gate_failures=[{"gate": "l1_div",
                                     "severity": "ack_required"}]),
            "esperando_decision")

    def test_un_preflight_resuelto_no_espera_nada(self):
        self.assertNotEqual(self._situacion(preflight_decision="ok"),
                            "esperando_decision")

    def test_ni_un_proyecto_recien_creado(self):
        self.assertNotEqual(self._situacion(), "esperando_decision")

    def test_lo_que_CORRE_manda_sobre_la_espera(self):
        # El orden de `_situacion` es la decisión: si algo se mueve, eso es
        # lo que el usuario necesita saber.
        self.assertEqual(self._situacion(
            awaiting_critical_ack=True, running_phase="extract",
            critical_gate_failures=[{"gate": "l1_div",
                                     "severity": "ack_required"}]),
            "en_marcha")


if __name__ == "__main__":
    unittest.main()
