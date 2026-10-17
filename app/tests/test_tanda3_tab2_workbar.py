"""Tab 2, la columna de trabajo, ⚙︎ y los modales de CMv4.0, ejecutados en
Chrome.

Los hallazgos de la auditoría con Codex del 2026-10-17 (tanda 3, frontend)
que viven fuera de Tab 1 y Tab 3: cancelar una copia que el servidor no
canceló, el resultado de un análisis extendido cuyo singleton ya es de otro,
una lectura del modal que pinta sobre el de al lado, respuestas viejas de la
columna y del browser, y ⚙︎ marcando como guardado lo que no se guardó. Cada
caso dice su hallazgo (F09, F13…).

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_tanda3_tab2_workbar -v
"""
import copy
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import catalogo_es, chrome, pagina_en_chrome  # noqa: E402
import test_las_tres_lenguas_en_pantalla as P  # noqa: E402  (fixture de Tab 2)

_GUION = r"""
<script>
(function () {
  const out = {casos: {}, errores: []};
  const espera = (ms = 0) => new Promise(r => setTimeout(r, ms));
  const llamadas = [];
  const toasts = [];
  let responder = () => null;
  window.apiFetch = async (url, opts) => {
    llamadas.push([url, (opts && opts.method) || 'GET', opts && opts.body]);
    return responder(url, opts);
  };
  window.showToast = (m, tipo) => { toasts.push([tipo, String(m)]); return 't'; };
  window.refrescarMkvRecientes = () => {};
  const diferida = () => { let r; const p = new Promise(x => r = x); p.resolver = r; return p; };
  const limpiar = () => { llamadas.length = 0; toasts.length = 0; responder = () => null; };
  async function caso(nombre, fn) {
    try { out.casos[nombre] = await fn(); }
    catch (e) { out.casos[nombre] = {excepcion: String(e && e.message || e)}; }
  }
  // El análisis de un MKV SIN el extendido hecho (es cuando se ofrece).
  const analisis = (nombre, ruta) => Object.assign(JSON.parse(JSON.stringify(window.__MKV)), {
    file_name: nombre, file_path: ruta,
    tracks: [{id: 1, type: 'audio', name: 'Castellano TrueHD Atmos 7.1',
              flag_default: true, flag_forced: false}].concat(
      window.__MKV.tracks.filter(t => t.type === 'video')),
    chapters: [],
  });
  const abrirMkv = (nombre, ruta) => {
    openMkvProject(analisis(nombre, ruta));
    return openMkvProjects.find(p => p.filePath === ruta);
  };
  const cerrarMkvs = () => { for (const q of openMkvProjects.slice()) _doCloseMkvProject(q.id); };

  setTimeout(async () => {
    // F09 — cancelar una copia EN COLA: el servidor la saca de la cola.
    await caso('cancelarCopiaEnCola', async () => {
      limpiar(); cerrarMkvs();
      const p = abrirMkv('Alien.mkv', '/mnt/library/Alien.mkv');
      let pasos = [{active: true, step: 'en_cola'}];
      responder = (url) => url === '/api/mkv/apply' ? {ok: true, queued: true}
        : url === '/api/mkv/apply/cancel' ? {ok: true, descartada: true}
        : url === '/api/mkv/apply/progress' ? pasos[0] : null;
      const flujo = _doApplyMkvEdits(true);
      await espera(1200);
      const cancelado = await cancelMkvApply();
      pasos = [{active: false, step: 'cancelled'}];
      await flujo;
      return {cancelado, ultimo: toasts[toasts.length - 1], ruta: p.filePath};
    });
    // F09 — el servidor dice que no había nada que cancelar y la copia sale bien.
    await caso('cancelarSinNadaQueCancelar', async () => {
      limpiar(); cerrarMkvs();
      const p = abrirMkv('Heat.mkv', '/mnt/library/Heat.mkv');
      let pasos = [{active: true, step: 'copying', pct: 99}];
      responder = (url) => url === '/api/mkv/apply' ? {ok: true, queued: true}
        : url === '/api/mkv/apply/cancel' ? {ok: false, reason: 'no_active_job'}
        : url === '/api/mkv/apply/progress' ? pasos[0]
        : url === '/api/mkv/analyze' ? analisis('Heat.mkv', '/mnt/output/Heat.mkv') : null;
      const flujo = _doApplyMkvEdits(true);
      await espera(1200);
      const cancelado = await cancelMkvApply();
      pasos = [{active: true, step: 'done', result: {ok: true, new_file_path: '/mnt/output/Heat.mkv',
                                                     copied_from_library: true}}];
      await flujo;
      return {cancelado, tipos: toasts.map(t => t[0]), ruta: p.filePath};
    });

    // F09 — la cancelación aceptada llega tarde: la copia ya había acabado.
    await caso('cancelarTarde', async () => {
      limpiar(); cerrarMkvs();
      const p = abrirMkv('Her.mkv', '/mnt/library/Her.mkv');
      let pasos = [{active: true, step: 'copying', pct: 99}];
      responder = (url) => url === '/api/mkv/apply' ? {ok: true, queued: true}
        : url === '/api/mkv/apply/cancel' ? {ok: true}
        : url === '/api/mkv/apply/progress' ? pasos[0]
        : url === '/api/mkv/analyze' ? analisis('Her.mkv', '/mnt/output/Her.mkv') : null;
      const flujo = _doApplyMkvEdits(true);
      await espera(1200);
      const cancelado = await cancelMkvApply();
      pasos = [{active: true, step: 'done', result: {ok: true, new_file_path: '/mnt/output/Her.mkv',
                                                     copied_from_library: true}}];
      await flujo;
      return {cancelado, tipos: toasts.map(t => t[0]), ruta: p.filePath};
    });

    // F12 — editar mientras se aplica no se pierde.
    await caso('editarDuranteElApply', async () => {
      limpiar(); cerrarMkvs();
      const p = abrirMkv('Dune.mkv', '/mnt/output/Dune.mkv');
      const apply = diferida();
      responder = (url) => url === '/api/mkv/apply' ? apply
        : url === '/api/mkv/analyze' ? analisis('Dune.mkv', '/mnt/output/Dune.mkv') : null;
      p.analysis.tracks[0].name = 'Primera edición';
      _mkvMarkDirty(p);
      const flujo = _doApplyMkvEdits(false);
      await espera(10);
      p.analysis.tracks[0].name = 'Segunda edición';
      _mkvMarkDirty(p);
      apply.resolver({ok: true, new_path: '/mnt/output/Dune.mkv'});
      await flujo;
      return {nombre: p.analysis.tracks[0].name, sucio: !!p.dirty};
    });

    // F12 — elegir en el browser un MKV abierto con cambios pregunta.
    await caso('reabrirConCambios', async () => {
      limpiar(); cerrarMkvs();
      const p = abrirMkv('Dune.mkv', '/mnt/output/Dune.mkv');
      p.dirty = true;
      _mkvAbrirDesdeElBrowser('/mnt/output/Dune.mkv', 'Dune.mkv');
      await espera(20);
      const r = {pregunta: document.getElementById('confirm-modal').classList.contains('open'),
                 analizo: llamadas.some(l => l[0] === '/api/mkv/analyze')};
      closeModal('confirm-modal');
      closeModal('mkv-analyze-modal');
      return r;
    });

    // F34 — sin FPS ni frames medidos, un guion.
    await caso('radiografiaSinFps', async () => {
      const a = analisis('X.mkv', '/mnt/output/X.mkv');
      const video = JSON.parse(JSON.stringify(window.__MKV.tracks.find(t => t.type === 'video')));
      delete video.fps; delete video.frame_count;
      delete a.fps;
      const html = _renderMkvDvRadiography(a, a.dovi, video, null);
      const caja = document.createElement('div'); caja.innerHTML = html;
      const valor = (rotulo) => {
        const c = [...caja.querySelectorAll('.dv-cell')]
          .find(x => x.querySelector('.dv-cell-label')?.textContent.trim() === rotulo);
        return c ? c.querySelector('.dv-cell-value').textContent.trim() : null;
      };
      return {fps: valor('FPS'), frames: valor('Frames')};
    });

    // F19 — un trabajo que acabó dice cómo, según el historial.
    await caso('modalComoAcabo', async () => {
      _trabajoModalUltimo = {id: 'K'};
      workbarEstado = {activo: null, cola: [], interactivo: [],
                       recientes: [{id: 'K', estado: 'error'}]};
      const conLinea = _trabajoModalComoAcabo();
      workbarEstado.recientes = [];
      const sinLinea = _trabajoModalComoAcabo();
      _trabajoModalUltimo = null;
      workbarEstado = {activo: null, cola: [], interactivo: [], recientes: []};
      return {conLinea, sinLinea};
    });

    // F13/F14 — el resultado de un análisis cuyo singleton ya es de otro.
    await caso('analisisDeOtroSingleton', async () => {
      limpiar(); cerrarMkvs();
      const a = abrirMkv('A.mkv', '/mnt/output/A.mkv');
      const b = abrirMkv('B.mkv', '/mnt/output/B.mkv');   // B queda activo
      const resultado = {quality_classification: 'real', quality_verdict_text: 'Master FULL'};
      responder = (url) => url === '/api/mkv/quality-audit/progress'
        ? {audit_id: 'b2', active: true,
           resultados_por_audit: {a1: {step: 'done', result: resultado, error: null}}}
        : null;
      const pintados = [];
      const pintarOrig = window._renderMkvEditPanel;
      window._renderMkvEditPanel = (p) => { pintados.push(p.id); return pintarOrig(p); };
      await _mkvAplicarAnalisisTerminado('a1', '/mnt/output/A.mkv');
      window._renderMkvEditPanel = pintarOrig;
      return {aplicadoEnA: a.analysis.dovi.quality_classification,
              enElOriginal: a.originalAnalysis.dovi.quality_classification,
              enB: b.analysis.dovi.quality_classification || null,
              panelDeAConElDato: pintados.includes(a.id) && a.id !== mkvProject.id};
    });
    // F13 — si la lectura falla, se reintenta.
    await caso('analisisConLecturaFallida', async () => {
      limpiar(); cerrarMkvs();
      const a = abrirMkv('A.mkv', '/mnt/output/A.mkv');
      let n = 0;
      responder = (url) => url !== '/api/mkv/quality-audit/progress' ? null
        : (++n === 1) ? null
        : {audit_id: 'a1', active: false, result: {quality_classification: 'real',
                                                  quality_verdict_text: 'x'}};
      await _mkvAplicarAnalisisTerminado('a1', '/mnt/output/A.mkv');
      await espera(6000);
      return {lecturas: n, aplicado: (a.analysis.dovi || {}).quality_classification || null};
    });

    // F16 — cada botón con el estado de SU MKV.
    await caso('botonesPorMkv', async () => {
      limpiar(); cerrarMkvs();
      const a = abrirMkv('A.mkv', '/mnt/output/A.mkv');
      const b = abrirMkv('B.mkv', '/mnt/output/B.mkv');
      workbarEstado = {activo: {id: 'a1', sobre: '/mnt/output/A.mkv', tab: 'mkv'},
                       cola: [], interactivo: [], recientes: []};
      _mkvPintarEstadoDeAnalisis();
      const ocupado = id => [...document.querySelectorAll(`#mkv-panel-${id} [data-analisis-extendido]`)]
        .some(x => x.classList.contains('ocupado'));
      const r = {a: ocupado(a.id), b: ocupado(b.id)};
      workbarEstado = {activo: null, cola: [], interactivo: [], recientes: []};
      return r;
    });

    // F15 — la ruta de la pestaña manda sobre la del análisis.
    await caso('rutaDeLaPestana', async () => {
      return _mkvRutaAnalisis({filePath: '/mnt/output/X.mkv',
                               analysis: {file_path: '/mnt/library/X.mkv'}});
    });

    // F29 — lo de menos de 10 nits cuenta.
    await caso('histograma', async () => {
      const svg = _rgrfDistributionSvg([1, 5, 20, 200]);
      return {setenta: svg.includes('75'), cincuenta: svg.includes('>50%') || svg.includes('50%<')};
    });

    // F35 — sin porcentaje medido no se inventa uno.
    await caso('timelineSinPorcentaje', async () => {
      const html = timelineDeTrabajo(
        [{titulo: 'A'}, {titulo: 'B'}, {titulo: 'C'}, {titulo: 'D'}],
        {fase_n: 2, pct_medido: false, segundos: 10, terminal: false}, 'Fases');
      const caja = document.createElement('div'); caja.innerHTML = html;
      return {indeterminada: !!caja.querySelector('.cmv40-tl-progress-track.indeterminada'),
              pct: caja.querySelector('.cmv40-tl-progress-pct').textContent};
    });

    // F08 — la lectura de A no pinta sobre el modal de B.
    await caso('modalDeOtroTrabajo', async () => {
      limpiar();
      const deA = diferida();
      registrarDetalleDeTrabajo('t3a', () => deA);
      registrarDetalleDeTrabajo('t3b', async () => ({titulo: 'Vista de B', cuerpo: 'B'}));
      const ta = {id: 'A', sobre: 'A', tipo: 'x', detalle: 't3a', que: 'Trabajo A', fases_total: 0};
      const tb = {id: 'B', sobre: 'B', tipo: 'x', detalle: 't3b', que: 'Trabajo B', fases_total: 0};
      const pa = _trabajoModalAbrir(ta);
      cerrarModalDeTrabajo();
      await _trabajoModalAbrir(tb);
      deA.resolver({titulo: 'Vista de A', cuerpo: 'A'});
      await pa;
      const r = {ref: _trabajoModalRef, sub: document.getElementById('trabajo-modal-cuerpo').textContent};
      cerrarModalDeTrabajo();
      return r;
    });

    // F19 — esperando turno no cuenta como «sin sujeto».
    await caso('modalEsperandoTurno', async () => {
      registrarDetalleDeTrabajo('t3c', async () => ({titulo: 'C', cuerpo: 'c'}));
      workbarEstado = {activo: null, cola: [{id: 'C', sobre: 'C', posicion: 1}],
                       interactivo: [], recientes: []};
      _trabajoModalTipo = 't3c'; _trabajoModalRef = 'C';
      _trabajoModalUltimo = {id: 'C', sobre: 'C', tipo: 'x', detalle: 't3c'};
      _trabajoModalSinActivo = 0; _trabajoModalVivo = true;
      for (let i = 0; i < 30; i++) await _trabajoModalRefrescar();
      const r = {sinActivo: _trabajoModalSinActivo, vivo: _trabajoModalVivo};
      cerrarModalDeTrabajo();
      workbarEstado = {activo: null, cola: [], interactivo: [], recientes: []};
      return r;
    });

    // F20 — cerrar el modal de OTRO trabajo no se hace.
    await caso('cerrarElModalDeOtro', async () => {
      _trabajoModalRef = 'Y'; _trabajoModalUltimo = {id: 'Y', sobre: 'Y'};
      openModal('trabajo-modal');
      const cerro = cerrarModalDeTrabajoDe('X');
      const abierto = document.getElementById('trabajo-modal').classList.contains('open');
      cerrarModalDeTrabajo();
      return {cerro, abierto};
    });

    // F22 — una lectura vieja de la columna no pisa una nueva.
    await caso('columnaSinRespuestasViejas', async () => {
      limpiar();
      const vieja = diferida();
      let n = 0;
      responder = (url) => !url.startsWith('/api/trabajos') ? null
        : (++n === 1) ? vieja
        : {activo: null, cola: [], interactivo: [], recientes: [], historial_rev: workbarEstado.historial_rev};
      const a = refrescarWorkbar();
      await refrescarWorkbar();
      vieja.resolver({activo: {id: 'viejo', sobre: 'viejo', tab: 'rip', que: 'viejo'}, cola: [],
                      interactivo: [], recientes: []});
      await a;
      return {activo: workbarEstado.activo};
    });

    // Sortable — la instancia vieja se destruye.
    await caso('sortableSinFuga', async () => {
      const creadas = [], destruidas = [];
      const SortOrig = window.Sortable;
      window.Sortable = {create: (el, o) => { const i = {el, destroy() { destruidas.push(i); }};
                                             creadas.push(i); return i; }};
      const st = {activo: null, interactivo: [], recientes: [],
                  cola: [{id: 'q1', sobre: 'q1', tab: 'rip', tipo: 'rip', que: 'uno', posicion: 1}]};
      _workbarRender(st);
      st.cola = [{id: 'q2', sobre: 'q2', tab: 'rip', tipo: 'rip', que: 'dos', posicion: 1}];
      _workbarRender(st);
      window.Sortable = SortOrig;
      return {creadas: creadas.length, destruidas: destruidas.length};
    });

    // F07 — una navegación vieja del browser no cambia la base.
    await caso('browserSinRespuestasViejas', async () => {
      limpiar();
      const vieja = diferida();
      responder = (url) => url.includes('root=library') ? vieja
        : {base: '/mnt/output', path: '', parent: null, entries: []};
      _fileBrowser.rootKey = 'library';
      const a = fileBrowserNavigate('');
      _fileBrowser.rootKey = 'output';
      await fileBrowserNavigate('');
      vieja.resolver({base: '/mnt/library', path: '', parent: null, entries: []});
      await a;
      return {base: _fileBrowser.base};
    });

    // F24/F25 y borrar claves — ⚙︎.
    await caso('ajustes', async () => {
      limpiar();
      const propia = 'https://docs.google.com/spreadsheets/d/PROPIA';
      const deflt = 'https://docs.google.com/spreadsheets/d/DEFAULT';
      const conPropia = {sheet: {url: propia, default_url: deflt, source: 'settings', is_default: false},
                         tmdb: {configured: true, source: 'default'}, google: {configured: false},
                         drive_folder: {configured: false}, idioma: {activo: 'es', disponibles: ['es', 'en', 'ca']}};
      const sinPropia = JSON.parse(JSON.stringify(conPropia));
      sinPropia.sheet = {url: deflt, default_url: deflt, source: 'default', is_default: true};
      _settingsCache = conPropia;
      _renderSettings(conPropia);
      const inp = document.getElementById('settings-sheet-input');
      // Borrar claves pregunta antes.
      clearAllKeys();
      const pregunta = document.getElementById('confirm-modal').classList.contains('open');
      const postAntes = llamadas.filter(l => l[1] === 'POST').length;
      responder = (url, o) => (o && o.method === 'POST') ? sinPropia : null;
      document.getElementById('confirm-ok-btn').click();
      await espera(20);
      const trasBorrar = inp.value;
      // F25: con una propia guardada, vaciar el campo es pedir quitarla.
      _settingsCache = conPropia; _renderSettings(conPropia);
      llamadas.length = 0;
      inp.value = '';
      await saveSettings();
      const cuerpo = (llamadas.find(l => l[1] === 'POST') || [])[2] || null;
      closeModal('settings-modal');
      return {pregunta, postAntes, trasBorrar, cuerpo, deflt};
    });

    // F30/F32/F33 — los modales de CMv4.0.
    await caso('modalesCmv40', async () => {
      limpiar();
      responder = () => null;
      await openCMv40CleanupModal();
      const limpieza = document.getElementById('cmv40-cleanup-body').textContent;
      closeModal('cmv40-cleanup-modal');
      const banner = _cmv40RepoUnavailableBanner(null);
      // El manual: un fallo no se cachea.
      _manualCache = null;
      const fetchOrig = window.fetch;
      let n = 0;
      window.fetch = async () => (++n === 1) ? {ok: false, status: 503}
        : {ok: true, json: async () => ({general: '<p>manual</p>'})};
      const primera = await _cmv40ManualSecciones();
      const segunda = await _cmv40ManualSecciones();
      window.fetch = fetchOrig;
      return {limpieza, banner, primera: Object.keys(primera).length, segunda: segunda.general || null};
    });

    // F31 — una consulta vieja no pinta encima de la nueva.
    await caso('consultaRapidaSinRespuestasViejas', async () => {
      limpiar();
      const vieja = diferida();
      responder = (url) => url.includes('title=Viejo') ? vieja
        : url.startsWith('/api/cmv40/recommend') ? {status: 'unknown', title: 'Nuevo'} : null;
      const caja = document.createElement('div');
      document.body.appendChild(caja);
      const a = _cmv40LookupFullFetch(caja, 'Viejo', null);
      await _cmv40LookupFullFetch(caja, 'Nuevo', null);
      const tras = caja.innerHTML;
      vieja.resolver({status: 'unknown', title: 'Viejo'});
      await a;
      const final = caja.innerHTML;
      caja.remove();
      return {igual: tras === final};
    });

    out.errores = window.__errores.slice();
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 700);
})();
</script>
"""


@unittest.skipUnless(chrome(), "Chrome/Chromium no disponible")
class TestTab2YColumna(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        mkv = copy.deepcopy(P.ANALISIS_MKV)
        mkv["dovi"] = {k: v for k, v in P.DOVI_TAB2.items()
                       if not k.startswith("quality_") and k != "light_profile"}
        semilla = "<script>window.__MKV = " + json.dumps(mkv) + ";</script>\n"
        cls.m = pagina_en_chrome(semilla + _GUION, presupuesto_ms=90000)
        cls.c = cls.m["casos"]
        cls.cat = catalogo_es()

    def _caso(self, nombre):
        c = self.c[nombre]
        if isinstance(c, dict):
            self.assertNotIn("excepcion", c, c)
        return c

    def test_cero_errores_de_js(self):
        self.assertEqual(self.m["errores"], [])

    def test_F09_una_copia_en_cola_retirada_lo_dice_asi(self):
        c = self._caso("cancelarCopiaEnCola")
        self.assertTrue(c["cancelado"])
        self.assertEqual(c["ultimo"][0], "warning")
        self.assertIn(self.cat["tab2.copia_retirada_de_la_cola"], c["ultimo"][1])
        self.assertNotIn(self.cat["tab2.parcial_se_borro"], c["ultimo"][1],
                         "anunciaba un parcial borrado de una copia que no empezó")
        self.assertEqual(c["ruta"], "/mnt/library/Alien.mkv")

    def test_F09_sin_nada_que_cancelar_la_copia_buena_sigue_siendo_buena(self):
        c = self._caso("cancelarSinNadaQueCancelar")
        self.assertFalse(c["cancelado"])
        self.assertEqual(c["tipos"][-1], "success",
                         "una copia terminada se anunciaba como cancelada")
        self.assertEqual(c["ruta"], "/mnt/output/Heat.mkv")

    def test_F09_una_cancelacion_que_llega_tarde_no_tapa_la_copia_buena(self):
        """El servidor aceptó el cancel, pero la copia ya había terminado
        bien: se anunciaba «cancelada, parcial borrado» y la pestaña no pasaba
        a la copia."""
        c = self._caso("cancelarTarde")
        self.assertTrue(c["cancelado"])
        self.assertEqual(c["tipos"][-1], "success")
        self.assertEqual(c["ruta"], "/mnt/output/Her.mkv")

    def test_F12_lo_editado_durante_el_apply_se_conserva(self):
        c = self._caso("editarDuranteElApply")
        self.assertEqual(c["nombre"], "Segunda edición")
        self.assertTrue(c["sucio"])

    def test_F12_reabrir_un_mkv_con_cambios_pregunta(self):
        c = self._caso("reabrirConCambios")
        self.assertTrue(c["pregunta"])
        self.assertFalse(c["analizo"], "el re-análisis tiraba los cambios sin preguntar")

    def test_F34_sin_fps_ni_frames_medidos_un_guion(self):
        c = self._caso("radiografiaSinFps")
        self.assertEqual(c["fps"], "—", "se suponía 23,976")
        self.assertEqual(c["frames"], "—", "los 720 frames de la muestra como total")

    def test_F19_un_trabajo_acabado_dice_como(self):
        c = self._caso("modalComoAcabo")
        self.assertEqual(c["conLinea"], self.cat["workbar.terminado_con_error"])
        self.assertEqual(c["sinLinea"], self.cat["workbar.ya_no_esta_en_curso"])

    def test_F13_el_resultado_se_recoge_aunque_el_singleton_sea_de_otro(self):
        c = self._caso("analisisDeOtroSingleton")
        self.assertEqual(c["aplicadoEnA"], "real")
        self.assertEqual(c["enElOriginal"], "real", "«Deshacer» se llevaba el extendido")
        self.assertIsNone(c["enB"])
        self.assertTrue(c["panelDeAConElDato"], "el panel oculto no se repintaba")

    def test_F13_una_lectura_fallida_se_reintenta(self):
        c = self._caso("analisisConLecturaFallida")
        self.assertGreaterEqual(c["lecturas"], 2)
        self.assertEqual(c["aplicado"], "real")

    def test_F16_cada_boton_con_el_estado_de_su_mkv(self):
        c = self._caso("botonesPorMkv")
        self.assertTrue(c["a"])
        self.assertFalse(c["b"], "el MKV de al lado decía «Analizando…»")

    def test_F15_la_ruta_de_la_pestana_manda(self):
        self.assertEqual(self._caso("rutaDeLaPestana"), "/mnt/output/X.mkv")

    def test_F29_lo_de_menos_de_10_nits_cuenta(self):
        self.assertTrue(self._caso("histograma")["setenta"])

    def test_F35_sin_porcentaje_medido_no_se_inventa(self):
        c = self._caso("timelineSinPorcentaje")
        self.assertTrue(c["indeterminada"])
        self.assertNotIn("%", c["pct"])

    def test_F08_la_lectura_de_A_no_pinta_sobre_B(self):
        c = self._caso("modalDeOtroTrabajo")
        self.assertEqual(c["ref"], "B")
        self.assertNotIn("A", c["sub"].replace("B", ""), c["sub"])

    def test_F19_esperar_turno_no_cuenta_como_sin_sujeto(self):
        c = self._caso("modalEsperandoTurno")
        self.assertEqual(c["sinActivo"], 0)
        self.assertTrue(c["vivo"], "a los 32 s dejaba de seguir un trabajo en cola")

    def test_F20_no_se_cierra_el_modal_de_otro(self):
        c = self._caso("cerrarElModalDeOtro")
        self.assertFalse(c["cerro"])
        self.assertTrue(c["abierto"])

    def test_F22_una_lectura_vieja_de_la_columna_no_pisa(self):
        self.assertIsNone(self._caso("columnaSinRespuestasViejas")["activo"])

    def test_sortable_no_deja_instancias_vivas(self):
        c = self._caso("sortableSinFuga")
        self.assertEqual(c["creadas"], 2)
        self.assertEqual(c["destruidas"], 1, "la instancia vieja no se destruía")

    def test_F07_el_browser_no_cambia_de_base_con_una_respuesta_vieja(self):
        self.assertEqual(self._caso("browserSinRespuestasViejas")["base"], "/mnt/output")

    def test_borrar_claves_pregunta_antes(self):
        c = self._caso("ajustes")
        self.assertTrue(c["pregunta"])
        self.assertEqual(c["postAntes"], 0, "borraba las cuatro claves sin preguntar")

    def test_F24_tras_borrar_claves_el_sheet_es_el_default(self):
        c = self._caso("ajustes")
        self.assertEqual(c["trasBorrar"], c["deflt"],
                         "el campo seguía con la URL propia y el siguiente «Guardar» la reinstalaba")

    def test_F25_vaciar_el_sheet_propio_lo_quita(self):
        c = self._caso("ajustes")
        self.assertIsNotNone(c["cuerpo"], "no se mandaba nada")
        self.assertEqual(json.loads(c["cuerpo"]).get("cmv40_sheet_url"), "")

    def test_F33_un_preview_fallido_no_deja_el_spinner(self):
        c = self._caso("modalesCmv40")
        self.assertIn(self.cat["cmv40_modals.no_se_pudo_escanear_los_proyectos"], c["limpieza"])

    def test_F32_sin_respuesta_no_se_afirma_que_falte_configuracion(self):
        c = self._caso("modalesCmv40")
        self.assertNotIn("settings.faltan_dos_cosas", c["banner"])
        self.assertIn(self.cat["settings.repo_no_se_pudo_consultar"], c["banner"])

    def test_F30_un_fallo_del_manual_no_se_cachea(self):
        c = self._caso("modalesCmv40")
        self.assertEqual(c["primera"], 0)
        self.assertEqual(c["segunda"], "<p>manual</p>")

    def test_F31_una_consulta_vieja_no_pinta_encima(self):
        self.assertTrue(self._caso("consultaRapidaSinRespuestasViejas")["igual"])


if __name__ == "__main__":
    unittest.main()
