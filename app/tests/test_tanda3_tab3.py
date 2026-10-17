"""Tab 3 ejecutado en Chrome: el acuse que no es una sesión, lo que se
anuncia sin que el servidor lo acepte, las respuestas que vuelven tarde y lo
que un repintado se llevaba por delante.

Los hallazgos de la auditoría con Codex del 2026-10-17 (tanda 3, frontend)
que viven en `tab3.js`. Varios estaban reproducidos —«Aplicar corrección»
dejando siete cards en pendiente, el bucle de reconexión del WS al despertar
el Mac, las casillas del sync a cero al expandir otra card— y aquí se
ejecutan con la página real y `apiFetch` en manos del test. Cada caso dice
el hallazgo (H2, H6…) del informe.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_tanda3_tab3 -v
"""
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import catalogo_es, chrome, pagina_en_chrome  # noqa: E402
import test_las_tres_lenguas_en_pantalla as P  # noqa: E402  (fixture de CMv4.0)

_GUION = r"""
<script>
(function () {
  const out = {casos: {}, errores: []};
  const espera = (ms = 0) => new Promise(r => setTimeout(r, ms));
  const llamadas = [];
  const toasts = [];
  let responder = () => null;
  window.apiFetch = async (url, opts) => {
    llamadas.push([url, (opts && opts.method) || 'GET']);
    return responder(url, opts);
  };
  window.showToast = (m, tipo) => { toasts.push([tipo, String(m)]); return 't'; };
  window.refrescarWorkbar = async () => {};
  window.refreshCMv40Sidebar = async () => {};
  window.hydrateTmdbCard = () => {};
  const diferida = () => { let r; const p = new Promise(x => r = x); p.resolver = r; return p; };
  const SES = () => JSON.parse(JSON.stringify(window.__SES));
  // Un proyecto abierto en la memoria de la pestaña, sin pintar el panel
  // entero: la mayoría de los casos miran el modelo, no el DOM.
  const proyecto = (cambios = {}) => {
    const s = Object.assign(SES(), cambios);
    const p = {id: s.id, subTabId: s.id, session: s, autoContinue: !!s.auto_pipeline,
               expandedPhases: {}, ws: null};
    openCMv40Projects.length = 0;
    openCMv40Projects.push(p);
    return p;
  };
  const limpiar = () => {
    llamadas.length = 0; toasts.length = 0; responder = () => null;
    window._cmv40PollActive = {};
  };
  async function caso(nombre, fn) {
    try { out.casos[nombre] = await fn(); }
    catch (e) { out.casos[nombre] = {excepcion: String(e && e.message || e)}; }
  }
  const input = (id, valor) => {
    let el = document.getElementById(id);
    if (!el) { el = document.createElement('input'); el.id = id; document.body.appendChild(el); }
    el.value = valor; return el;
  };

  setTimeout(async () => {
    // H2 — «Aplicar corrección» contesta `{ok, started}`, no la sesión.
    await caso('aplicarCorreccionConAcuse', async () => {
      limpiar();
      const p = proyecto({phase: 'extracted'});
      input(`cmv40-remove-${p.id}`, '12').dataset.tocado = '1';
      input(`cmv40-remove-fin-${p.id}`, '0');
      input(`cmv40-duplicate-${p.id}`, '0');
      input(`cmv40-duplicate-fin-${p.id}`, '0');
      responder = (url) => url.endsWith('/apply-sync') ? {ok: true, started: true} : null;
      await cmv40DoApplySync(p.id);
      return {id: p.session.id, fase: p.session.phase,
              origen: p.session.source_mkv_name,
              vigilada: !!window._cmv40PollActive[p.id],
              casilla: document.getElementById(`cmv40-remove-${p.id}`).value};
    });

    // H6 — el toggle del auto espera al servidor.
    await caso('toggleConElPostFallido', async () => {
      limpiar();
      const p = proyecto({auto_pipeline: true});
      responder = (url) => url.endsWith('/auto-pipeline') ? null : {files: []};
      await cmv40ToggleAuto(p.id);
      return {auto: p.autoContinue, toasts: toasts.map(t => t[0])};
    });
    await caso('toggleAceptado', async () => {
      limpiar();
      const p = proyecto({auto_pipeline: true});
      responder = (url) => url.endsWith('/auto-pipeline')
        ? Object.assign(SES(), {auto_pipeline: false}) : {files: []};
      await cmv40ToggleAuto(p.id);
      return {auto: p.autoContinue};
    });
    // H6 — cancelar sin respuesta no apaga nada; con ella, sí.
    await caso('cancelarSinRespuesta', async () => {
      limpiar();
      const p = proyecto({auto_pipeline: true, running_phase: 'extract'});
      responder = () => null;
      cmv40CancelRunning(p.id);
      document.getElementById('confirm-ok-btn').click();
      await espera(20);
      const sinRespuesta = p.autoContinue;
      responder = () => ({ok: true});
      cmv40CancelRunning(p.id);
      document.getElementById('confirm-ok-btn').click();
      await espera(20);
      return {sinRespuesta, conRespuesta: p.autoContinue};
    });
    // H6 — un lanzador cuyo POST falla ni anuncia ni vigila.
    await caso('lanzadorFallido', async () => {
      limpiar();
      const p = proyecto({phase: 'target_provided'});
      responder = () => null;
      await cmv40DoExtract(p.id);
      return {vigilada: !!window._cmv40PollActive[p.id], toasts: toasts.length};
    });

    // H7 — un GET que vuelve tarde no pisa uno posterior.
    await caso('getViejoNoPisa', async () => {
      limpiar();
      const p = proyecto({phase: 'injected', running_phase: 'inject'});
      const viejo = diferida();
      let n = 0;
      responder = () => (++n === 1) ? viejo
        : Object.assign(SES(), {phase: 'remuxed', running_phase: 'remux'});
      const a = _refreshCMv40Session(p.id);
      await _refreshCMv40Session(p.id);
      viejo.resolver(Object.assign(SES(), {phase: 'injected', running_phase: 'inject'}));
      await a;
      return {fase: p.session.phase, corriendo: p.session.running_phase};
    });

    // H13 — el `onclose` de un socket sustituido no reconecta.
    await caso('wsViejoNoReconecta', async () => {
      limpiar();
      const sockets = [];
      const WsOrig = window.WebSocket;
      window.WebSocket = function () { sockets.push(this); this.readyState = 1; this.close = () => {}; };
      window.WebSocket.OPEN = 1;
      const p = proyecto({running_phase: 'extract'});
      _connectCMv40WebSocket(p);
      _connectCMv40WebSocket(p);          // reconexión: el primero queda viejo
      sockets[0].onclose();                // y su cierre llega después
      const timer = !!p._wsReconnectTimer;
      if (p._wsReconnectTimer) clearTimeout(p._wsReconnectTimer);
      window.WebSocket = WsOrig;
      return {sockets: sockets.length, vigenteEsElNuevo: p.ws === sockets[1],
              reconexionProgramada: timer};
    });

    // H17 — el poller se va cuando la fase deja de estar ocupada.
    await caso('pollerTrasCancelar', async () => {
      limpiar();
      // Su propio id: los pollers de casos anteriores siguen vivos y
      // preguntan por el suyo.
      const p = proyecto({id: 'c17', phase: 'sync_verified', running_phase: 'inject'});
      let n = 0;
      // Solo cuentan las vueltas de ESTE poller; el repintado pide otras cosas.
      responder = (url) => !url.startsWith('/api/cmv40/c17?include_log=false') ? null
        : (++n === 1)
        ? Object.assign(SES(), {phase: 'sync_verified', running_phase: 'inject'})
        : Object.assign(SES(), {phase: 'sync_verified', running_phase: null});
      const fin = await Promise.race([
        _cmv40PollPhase(p.id, 'injected').then(() => 'salió'),
        espera(15000).then(() => 'siguió'),
      ]);
      return {fin, gets: n, libre: !window._cmv40PollActive[p.id]};
    });

    // H5 — cambiar de origen suelta el candidato del repo anterior.
    await caso('cambiarOrigenSueltaElRepo', async () => {
      limpiar();
      responder = () => new Promise(() => {});
      _cmv40SourceSelected = '/mnt/output/A.mkv';
      _cmv40NewTargetTab = 'repo';
      _cmv40NewTargetSelected = {kind: 'repo', value: {file_id: 'f1', file_name: 'A.bin'}};
      onCMv40SourceChange('/mnt/output/B.mkv', 'B.mkv');
      const repo = _cmv40NewTargetSelected;
      _cmv40NewTargetTab = 'path';
      _cmv40NewTargetSelected = {kind: 'path', value: '/mnt/cmv40_rpus/x.bin'};
      onCMv40SourceChange('/mnt/output/C.mkv', 'C.mkv');
      return {repo, path: _cmv40NewTargetSelected && _cmv40NewTargetSelected.kind,
              crearHabilitado: !document.getElementById('cmv40-create-btn').disabled};
    });

    // H10 — lo escrito en una casilla que el repintado del padre recrea.
    await caso('casillaSobreviveAlPadre', async () => {
      const padre = document.createElement('div');
      document.body.appendChild(padre);
      pintarSiCambia(padre, '<div id="t3-hijo"><input id="t3-casilla" value="0"></div>');
      const c = document.getElementById('t3-casilla');
      c.value = '24'; marcarTocado(c);
      // El padre se repinta y el hijo nace VACÍO: la casilla la crea otro.
      pintarSiCambia(padre, '<div id="t3-hijo"></div><p>otra card</p>');
      pintarSiCambia(document.getElementById('t3-hijo'), '<input id="t3-casilla" value="0">');
      const recuperado = document.getElementById('t3-casilla').value;
      // Y lo aplicado se olvida.
      olvidarAnclaje(['t3-casilla']);
      document.getElementById('t3-casilla').dataset.tocado = '';
      pintarSiCambia(padre, '<div id="t3-hijo"></div><p>tercera</p>');
      pintarSiCambia(document.getElementById('t3-hijo'), '<input id="t3-casilla" value="0">');
      const olvidado = document.getElementById('t3-casilla').value;
      padre.remove();
      return {recuperado, olvidado};
    });

    // H8 — la lista del repo de un elemento recreado.
    await caso('listaDelRepoRecreada', async () => {
      limpiar();
      const p = proyecto({phase: 'source_analyzed',
                          source_mkv_path: '/mnt/output/Drive (2011).mkv'});
      const caja = document.createElement('div');
      caja.innerHTML = `<div id="cmv40-repo-list-${p.id}"></div><div id="cmv40-repo-info-${p.id}"></div>`;
      document.body.appendChild(caja);
      responder = (url) => url.startsWith('/api/cmv40/repo-rpus')
        ? {drive_configured: true, candidates: [{score: 0.9, predicted_type: 'unknown',
             provenance: '', file: {id: 'f1', name: 'Drive.bin', path: 'Drive/Drive.bin', size_bytes: 1000}}]}
        : null;
      await _cmv40LoadRepoForPanel(p.id);
      // Colapsar y volver a expandir: el elemento se recrea vacío.
      caja.innerHTML = `<div id="cmv40-repo-list-${p.id}"></div><div id="cmv40-repo-info-${p.id}"></div>`;
      await _cmv40LoadRepoForPanel(p.id);
      const tarjetas = caja.querySelectorAll('.cmv40-repo-card').length;
      const peticiones = llamadas.filter(l => l[0].startsWith('/api/cmv40/repo-rpus')).length;
      caja.remove();
      return {tarjetas, peticiones, seleccion: p._panelSelectedRepo && p._panelSelectedRepo.file_id};
    });

    // H11 — el aviso de «sin datos» no se lleva el canvas.
    await caso('canvasSobreviveAlFallo', async () => {
      limpiar();
      const p = proyecto({phase: 'extracted'});
      p.syncData = null;
      const caja = document.createElement('div');
      caja.innerHTML = `<div id="cmv40-chart-wrap-${p.id}"><canvas id="cmv40-chart-${p.id}"></canvas></div>`;
      document.body.appendChild(caja);
      responder = () => null;
      await _loadCMv40SyncChart(p);
      const tras = {canvas: !!document.getElementById(`cmv40-chart-${p.id}`),
                    aviso: !!caja.querySelector('[data-sin-datos]')};
      _cmv40ChartConDatos(p.id);
      const conDatos = {aviso: !!caja.querySelector('[data-sin-datos]'),
                        visible: document.getElementById(`cmv40-chart-${p.id}`).style.display !== 'none'};
      caja.remove();
      return {tras, conDatos};
    });

    // H12 — pedir B mientras carga A acaba pidiendo B.
    await caso('zoomPideElUltimoRango', async () => {
      limpiar();
      const p = proyecto({phase: 'extracted'});
      p.syncData = {data: [], source_frames: 1000, target_frames: 1000};
      p.chartRange = {start: 0, end: 1000};
      const primera = diferida();
      responder = (url) => url.includes('desde=0&hasta=100') ? primera : {data: [], source_frames: 1000};
      const a = _cmv40SetRange(p.id, 0, 100);
      _cmv40SetRange(p.id, 50, 60);
      primera.resolver({data: [], source_frames: 1000});
      await a;
      return {pedidas: llamadas.map(l => l[0]).filter(u => u.includes('sync-data'))};
    });

    // H9 — abrir el pre-flight de B con la lectura de A en vuelo.
    await caso('preflightDeOtroProyecto', async () => {
      limpiar();
      const deA = diferida();
      responder = (url) => url === '/api/cmv40/A' ? deA : new Promise(() => {});
      const pa = abrirPreflightCMv40('A');
      cerrarPreflightCMv40();
      abrirPreflightCMv40('B');
      await espera(10);
      deA.resolver(null);
      await pa;
      await espera(10);
      const pedidoB = llamadas.some(l => l[0] === '/api/cmv40/B');
      cerrarPreflightCMv40();
      return {pedidoB};
    });

    // H23 — dónde quedó el trabajo, sin inventar.
    await caso('dondeQuedoSinAuto', async () => {
      limpiar();
      const p = proyecto({auto_pipeline: false});
      p.autoContinue = false;
      responder = () => ({activo: null, cola: []});
      const sinAuto = await _cmv40PfDondeQuedo(p.id);
      p.autoContinue = true;
      const conAuto = await _cmv40PfDondeQuedo(p.id);
      responder = () => ({activo: {id: p.id, sobre: p.id, fase: 'inject', fase_label: 'Fase F'}, cola: []});
      const otra = await _cmv40PfDondeQuedo(p.id);
      return {sinAuto, conAuto, otra};
    });

    // H16 — una previsualización fallida no dice «nada que borrar».
    await caso('rehacerSinPrevisualizacion', async () => {
      limpiar();
      proyecto();
      responder = () => null;
      await _cmv40Redo('c1', 'extracted', 'F');
      const cuerpo = document.getElementById('cmv40-confirm-body').textContent;
      closeModal('cmv40-confirm-modal');
      return {cuerpo};
    });

    // H24 / H25 / S1 / H19 — textos y estados que salen de lo que pasa.
    await caso('pintadosVarios', async () => {
      const s = SES();
      const gh = _cmv40RenderGateCardGH('c1', Object.assign({}, s, {phase: 'remuxed', running_phase: null}), true);
      const ghCorre = _cmv40RenderGateCardGH('c1', Object.assign({}, s, {phase: 'remuxed', running_phase: 'validate'}), true);
      const pasos = _cmv40RenderTimelineStepsHTML(
        [{key: 'B', icon: 'caja', title: 'B', what: '', etaSecs: 0}], ['error'], s);
      const ack = Object.assign({}, s, {target_trust_ok: false, user_acknowledged_degradation: true,
        plan: Object.assign({}, s.plan, {skip_sync_review: true, trust_effective: false})});
      const d = _cmv40PlanAutoSteps(ack, null).find(x => x.key === 'D');
      return {
        ghEnCurso: gh.includes(tr('tab3.validacion_en_curso')),
        ghCorreEnCurso: ghCorre.includes(tr('tab3.validacion_en_curso')),
        pasoIndefinido: pasos.includes('undefined'),
        e: CMV40_RUNNING_TO_STEP['correct_sync'],
        rotuloE: typeof CMV40_RUNNING_LABELS['correct_sync'] === 'string',
        dConAck: d && d.forcedStatus,
      };
    });

    // H4 — con la revisión forzada, el gráfico se pide aunque los gates pasen.
    await caso('revisionForzadaCargaElGrafico', async () => {
      limpiar();
      responder = () => new Promise(() => {});
      switchTab(3);
      const s = Object.assign(SES(), {id: 'c9', phase: 'extracted', running_phase: null,
        target_trust_ok: true, trust_override: 'force_interactive', awaiting_critical_ack: false,
        plan: Object.assign({}, SES().plan, {skip_sync_review: false, trust_effective: false})});
      openCMv40Project(s);
      await espera(300);
      const pidio = llamadas.some(l => l[0].startsWith('/api/cmv40/c9/sync-data'));
      closeCMv40Project('c9');
      return {pidio};
    });

    // H21 / H22 — el log recortado se dice, y «Limpiar» no se deshace.
    await caso('logRecortadoYLimpio', async () => {
      limpiar();
      const p = proyecto();
      p.session.output_log = Array.from({length: 1300}, (_, i) => `línea ${i}`);
      p._renderedLogCount = 0;
      const caja = document.createElement('div');
      caja.innerHTML = `<div id="cmv40-log-${p.id}"></div>`;
      document.body.appendChild(caja);
      _cmv40SyncPermanentLog(p);
      const log = document.getElementById(`cmv40-log-${p.id}`);
      const lineas = log.querySelectorAll('.log-line').length;
      const aviso = !!log.querySelector('.cmv40-log-recortado');
      let copiado = null;
      Object.defineProperty(navigator, 'clipboard', {configurable: true,
        value: {writeText: async t => { copiado = t; }}});
      const execOrig = document.execCommand;
      document.execCommand = () => { copiado = document.querySelector('textarea')?.value; return true; };
      await copyLogToClipboard(`cmv40-log-${p.id}`);
      document.execCommand = execOrig;
      _clearCMv40Log(p.id);
      _cmv40SyncPermanentLog(p);
      const trasLimpiar = log.querySelectorAll('.log-line').length;
      caja.remove();
      return {lineas, aviso, copiadoEntero: !!copiado && copiado.includes('línea 0')
                                              && copiado.includes('línea 1299'), trasLimpiar};
    });

    out.errores = window.__errores.slice();
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 700);
})();
</script>
"""


def _medir() -> dict:
    semilla = "<script>window.__SES = " + json.dumps(P.SESION_CMV40) + ";</script>\n"
    return pagina_en_chrome(semilla + _GUION, presupuesto_ms=90000)


@unittest.skipUnless(chrome(), "Chrome/Chromium no disponible")
class TestTab3(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = _medir()
        cls.c = cls.m["casos"]

    def _caso(self, nombre):
        c = self.c[nombre]
        self.assertNotIn("excepcion", c, c)
        return c

    def test_cero_errores_de_js(self):
        self.assertEqual(self.m["errores"], [])

    def test_H2_el_acuse_no_se_asigna_como_sesion(self):
        c = self._caso("aplicarCorreccionConAcuse")
        self.assertEqual(c["id"], "c1", "la sesión se quedó sin id: era el acuse")
        self.assertEqual(c["fase"], "extracted")
        self.assertTrue(c["origen"])
        self.assertTrue(c["vigilada"], "con el WS caído nadie traía la sesión al acabar")
        self.assertEqual(c["casilla"], "0", "tras aplicar, las casillas vuelven a cero")

    def test_H6_el_toggle_no_cambia_si_el_servidor_no_acepta(self):
        c = self._caso("toggleConElPostFallido")
        self.assertTrue(c["auto"], "decía «auto desactivado» con el servidor encadenando")
        self.assertNotIn("info", c["toasts"])

    def test_H6_el_toggle_aceptado_cambia(self):
        self.assertFalse(self._caso("toggleAceptado")["auto"])

    def test_H6_cancelar_sin_respuesta_no_apaga_el_auto(self):
        c = self._caso("cancelarSinRespuesta")
        self.assertTrue(c["sinRespuesta"])
        self.assertFalse(c["conRespuesta"])

    def test_H6_un_lanzador_fallido_ni_anuncia_ni_vigila(self):
        c = self._caso("lanzadorFallido")
        self.assertFalse(c["vigilada"])
        self.assertEqual(c["toasts"], 0)

    def test_H7_un_get_viejo_no_pisa_uno_nuevo(self):
        c = self._caso("getViejoNoPisa")
        self.assertEqual(c["fase"], "remuxed", "la interfaz volvió a la fase anterior")
        self.assertEqual(c["corriendo"], "remux")

    def test_H13_el_socket_viejo_no_reconecta(self):
        c = self._caso("wsViejoNoReconecta")
        self.assertTrue(c["vigenteEsElNuevo"])
        self.assertFalse(c["reconexionProgramada"], "bucle de reconexión cada 2 s")

    def test_H17_el_poller_se_va_tras_cancelar(self):
        c = self._caso("pollerTrasCancelar")
        self.assertEqual(c["fin"], "salió", "seguía hasta 600 vueltas")
        self.assertLessEqual(c["gets"], 3)
        self.assertTrue(c["libre"])

    def test_H5_otro_origen_suelta_el_candidato_del_repo(self):
        c = self._caso("cambiarOrigenSueltaElRepo")
        self.assertIsNone(c["repo"], "se creaba con el RPU del origen anterior")
        self.assertEqual(c["path"], "path", "un RPU de carpeta no depende del origen")

    def test_H10_la_casilla_sobrevive_al_repintado_del_padre(self):
        c = self._caso("casillaSobreviveAlPadre")
        self.assertEqual(c["recuperado"], "24", "lo escrito se perdía al expandir otra card")
        self.assertEqual(c["olvidado"], "0")

    def test_H8_la_lista_recreada_se_repinta_sin_volver_a_pedir(self):
        c = self._caso("listaDelRepoRecreada")
        self.assertEqual(c["tarjetas"], 1, "la lista se quedaba en blanco")
        self.assertEqual(c["peticiones"], 1)
        self.assertEqual(c["seleccion"], "f1")

    def test_H11_el_aviso_no_se_lleva_el_canvas(self):
        c = self._caso("canvasSobreviveAlFallo")
        self.assertTrue(c["tras"]["canvas"], "el fallo eliminaba el canvas")
        self.assertTrue(c["tras"]["aviso"])
        self.assertFalse(c["conDatos"]["aviso"])
        self.assertTrue(c["conDatos"]["visible"])

    def test_H12_el_zoom_acaba_pidiendo_el_ultimo_rango(self):
        pedidas = self._caso("zoomPideElUltimoRango")["pedidas"]
        self.assertTrue(any("desde=50&hasta=60" in u for u in pedidas),
                        f"nadie pedía el rango nuevo: {pedidas}")

    def test_H9_el_preflight_de_otro_proyecto_se_vigila(self):
        self.assertTrue(self._caso("preflightDeOtroProyecto")["pedidoB"],
                        "el modal de B se quedaba en «Iniciando»")

    def test_H23_donde_quedo_no_inventa(self):
        c = self._caso("dondeQuedoSinAuto")
        self.assertEqual(c["sinAuto"], catalogo_es()["tab3.pf_lanza_la_fase_a"],
                         "decía «continúa en segundo plano» sin nada encolado")
        self.assertEqual(c["conAuto"], "")
        self.assertIn("Fase F", c["otra"])

    def test_H16_sin_previsualizacion_no_dice_que_no_hay_nada(self):
        c = self._caso("rehacerSinPrevisualizacion")
        self.assertNotIn("No hay artefactos", c["cuerpo"])
        self.assertIn("No se pudo consultar", c["cuerpo"])

    def test_H24_la_card_GH_no_afirma_que_valida_sin_validar(self):
        c = self._caso("pintadosVarios")
        self.assertFalse(c["ghEnCurso"])
        self.assertTrue(c["ghCorreEnCurso"])

    def test_H25_un_paso_con_error_no_pinta_undefined(self):
        self.assertFalse(self._caso("pintadosVarios")["pasoIndefinido"])

    def test_S1_la_fase_E_se_llama_como_en_el_backend(self):
        c = self._caso("pintadosVarios")
        self.assertEqual(c["e"], "E")
        self.assertTrue(c["rotuloE"])

    def test_H19_con_el_ack_la_fase_D_sale_omitida(self):
        self.assertEqual(self._caso("pintadosVarios")["dConAck"], "skipped")

    def test_H4_con_la_revision_forzada_se_pide_el_grafico(self):
        self.assertTrue(self._caso("revisionForzadaCargaElGrafico")["pidio"],
                        "la Fase D salía sin gráfico ni controles")

    def test_H21_el_recorte_se_dice_y_copiar_copia_todo(self):
        c = self._caso("logRecortadoYLimpio")
        self.assertEqual(c["lineas"], 1200)
        self.assertTrue(c["aviso"])
        self.assertTrue(c["copiadoEntero"], "«Copiar» copiaba solo lo que quedaba en el DOM")

    def test_H22_limpiar_no_se_deshace_en_el_siguiente_refresco(self):
        self.assertEqual(self._caso("logRecortadoYLimpio")["trasLimpiar"], 0)


if __name__ == "__main__":
    unittest.main()
