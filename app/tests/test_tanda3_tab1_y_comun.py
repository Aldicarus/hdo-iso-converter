"""Tab 1 y las piezas comunes, ejecutados en Chrome: el proyecto atado tras un
`await`, los fallos leídos como fallos y el escapado según contexto.

La auditoría con Codex del 2026-10-17 (tanda 3, frontend) encontró una
familia de fallos que ningún test podía ver porque todos los que había leían
funciones sueltas: **lo que pasa ENTRE dos piezas cuando una respuesta
vuelve tarde**. Guardar un proyecto y cambiar a otro mientras el PUT vuela;
comprobar el origen de un episodio con otro ya activo; un terminal del WS de
un rip que llega con el siguiente ya en marcha. Aquí se ejecuta la página
entera —el `index.html` real con sus siete piezas— con `apiFetch` sustituido
por uno que deja cada respuesta en la mano del test, y se mira el estado que
queda.

Cada caso dice el hallazgo (S4, F03…) del informe de la auditoría.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_tanda3_tab1_y_comun -v
"""
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import chrome, pagina_en_chrome  # noqa: E402

# Una sesión de Tab 1 con lo justo para que el panel se pinte entero.
def _sesion(sid, nombre):
    return {
        "id": sid, "mkv_name": nombre, "mkv_name_manual": False,
        "status": "pending", "media_type": "movie", "source_type": "iso",
        "iso_path": f"{sid}.iso", "source_path": f"{sid}.iso",
        "included_tracks": [
            {"track_type": "audio", "position": 0, "label": "Castellano TrueHD Atmos 7.1",
             "flag_default": True, "flag_forced": False,
             "raw": {"codec": "TrueHD Atmos", "language": "Spanish",
                     "description": "7.1 / 48 kHz"}},
            {"track_type": "subtitle", "position": 1, "label": "Castellano Forzados (PGS)",
             "flag_default": True, "flag_forced": True, "subtitle_type": "forced",
             "raw": {"codec": "PGS", "language": "Spanish", "packet_count": 248,
                     "bitrate_kbps": 1.0}}],
        "discarded_tracks": [],
        "chapters": [{"number": 1, "name": "Capítulo 01", "timestamp": "00:00:00.000",
                      "name_custom": False}],
        "execution_history": [], "updated_at": "2026-10-17T08:00:00Z",
        "bdinfo_result": {"main_mpls": "00800.mpls", "duration_seconds": 7200,
                          "video_tracks": [], "audio_tracks": [], "subtitle_tracks": []},
    }


_GUION = r"""
<script>
(function () {
  const out = {casos: {}, errores: []};
  const espera = (ms = 0) => new Promise(r => setTimeout(r, ms));
  const llamadas = [];
  const toasts = [];
  // `apiFetch` en manos del test: `responder(url, opts)` decide qué vuelve,
  // y puede devolver una promesa que el caso resuelve cuando le conviene.
  let responder = () => null;
  const apiFetchReal = window.apiFetch;
  window.apiFetch = async (url, opts) => { llamadas.push([url, (opts && opts.method) || 'GET']); return responder(url, opts); };
  window.showToast = (m, tipo) => { toasts.push([tipo, String(m)]); return 't'; };
  window.refrescarWorkbar = async () => {};
  window.loadSessions = async () => {};
  window.hydrateTmdbCard = () => {};
  const diferida = () => { let r; const p = new Promise(x => r = x); p.resolver = r; return p; };
  const S = (sid, nombre) => JSON.parse(JSON.stringify(window.__SES(sid, nombre)));
  const limpiar = () => {
    for (const p of openProjects.slice()) _doCloseProject(p.id);
    llamadas.length = 0; toasts.length = 0; responder = () => null;
  };

  async function caso(nombre, fn) {
    try { out.casos[nombre] = await fn(); }
    catch (e) { out.casos[nombre] = {excepcion: String(e && e.message || e)}; }
  }

  setTimeout(async () => {
    switchTab(1);

    // S4 — «Guardar y cerrar» un proyecto que NO es el activo.
    await caso('guardarYCerrarInactivo', async () => {
      limpiar();
      responder = (url, o) => (o && o.method === 'PUT') ? {id: 'a', updated_at: 'x'} : {available: true};
      const A = openProject(S('a', 'A.mkv'));
      const B = openProject(S('b', 'B.mkv'));        // B queda activo
      A.dirty = true;
      closeProject(A.id);
      document.querySelector('#confirm-modal .confirm-extra-btn').click();
      await espera(50);
      return {
        cerrado: !openProjects.includes(A),
        activoEsB: getActiveProject() === B,
        sesionActualEsB: currentSession === B.session,
        put: llamadas.filter(l => l[1] === 'PUT').map(l => l[0]),
      };
    });

    // F03 — la comprobación del origen de A vuelve con B ya activo.
    await caso('checkIsoNoCambiaElActivo', async () => {
      limpiar();
      const checkA = diferida();
      responder = (url) => url === '/api/sessions/a/check-iso' ? checkA : {available: true};
      const A = openProject(S('a', 'A.mkv'));
      const B = openProject(S('b', 'B.mkv'));
      await espera(10);
      checkA.resolver({available: false, iso_path: 'a.iso', source_label: 'ISO'});
      await espera(10);
      // El usuario escribe el nombre en SU pestaña, la de B.
      document.getElementById(`${B.id}-mkv-name-input`).value = 'nuevo-B.mkv';
      onMkvNameInput();
      return {
        activoEsB: activeSubTabId === B.id,
        nombreB: B.session.mkv_name,
        nombreA: A.session.mkv_name,
        bannerA: document.getElementById(`${A.id}-iso-missing-banner`)?.style.display !== 'none',
        bannerB: document.getElementById(`${B.id}-iso-missing-banner`)?.style.display !== 'none',
      };
    });

    // F05 — si el PUT falla, no se ejecuta.
    await caso('ejecutarConElGuardadoFallido', async () => {
      limpiar();
      responder = (url, o) => (o && o.method === 'PUT') ? null : {available: true};
      const A = openProject(S('a', 'A.mkv'));
      A.dirty = true;
      llamadas.length = 0;
      await executeSession();
      return {
        comproboOrigen: llamadas.some(l => l[0].endsWith('/check-iso')),
        confirmAbierto: document.getElementById('confirm-modal').classList.contains('open'),
        sigueSucio: !!A.dirty,
      };
    });

    // F04 — revertir el nombre de A y cambiar a B mientras vuelve.
    await caso('revertirNombreTrasCambiar', async () => {
      limpiar();
      const recalc = diferida();
      responder = (url) => url.endsWith('/recalculate-name') ? recalc : {available: true};
      const A = openProject(S('a', 'A.mkv'));
      const B = openProject(S('b', 'B.mkv'));
      switchSubTab(A.id);
      const p = revertMkvName();
      switchSubTab(B.id);
      recalc.resolver({mkv_name: 'A-recalculado.mkv'});
      await p;
      return {
        nombreA: A.session.mkv_name,
        inputA: document.getElementById(`${A.id}-mkv-name-input`).value,
        inputB: document.getElementById(`${B.id}-mkv-name-input`).value,
        nombreB: B.session.mkv_name,
      };
    });

    // F04 — y si un refresco sustituye la sesión mientras vuelve, el nombre
    // va a la sesión NUEVA, que es la que guarda `saveSession`.
    await caso('revertirNombreConRefresco', async () => {
      limpiar();
      const recalc = diferida();
      responder = (url) => url.endsWith('/recalculate-name') ? recalc : {available: true};
      const A = openProject(S('a', 'A.mkv'));
      switchSubTab(A.id);
      const p = revertMkvName();
      A.session = JSON.parse(JSON.stringify(A.session));
      recalc.resolver({mkv_name: 'A-recalculado.mkv'});
      await p;
      return {
        nombre: A.session.mkv_name,
        manual: A.session.mkv_name_manual,
        input: document.getElementById(`${A.id}-mkv-name-input`).value,
      };
    });

    // F11 — un refresco de estado con ediciones sin guardar.
    await caso('refrescoConservaLasEdiciones', async () => {
      limpiar();
      const servidor = S('a', 'A.mkv');
      servidor.status = 'done';
      responder = (url) => url === '/api/sessions/a' ? servidor : {available: true};
      const A = openProject(S('a', 'A.mkv'));
      A.session.included_tracks[0].label = 'Editado a mano';
      A.session.mkv_name = 'Editado.mkv';
      A.dirty = true;
      await refreshOpenProjectState('a');
      return {etiqueta: A.session.included_tracks[0].label, nombre: A.session.mkv_name,
              estado: A.session.status};
    });

    // S4/F03 de rebote: pintar un proyecto que no es el activo no deja
    // `currentSession` en él.
    await caso('pintarOtroNoMueveLaSesion', async () => {
      limpiar();
      responder = () => ({available: true});
      const A = openProject(S('a', 'A.mkv'));
      const B = openProject(S('b', 'B.mkv'));
      renderProjectPanel(A);
      return {sesionActualEsB: currentSession === B.session};
    });

    // F02 — un apóstrofo en un handler inline.
    await caso('apostrofoEnUnHandler', async () => {
      limpiar();
      const ruta = "/mnt/isos/Grey's Anatomy \"S01\" </b>/00800.m2ts";
      _seriesState = {
        probe: {episode_candidates: [{mpls_path: ruta, mpls_name: '00800.m2ts',
                                       duration_minutes: 42}]},
        mode: 'manual', selectedSeries: {tmdb_id: 1, name: 'x'},
        selectedSeason: {season_number: 1}, seasonEpisodes: [], mplsMatches: [],
        candidates: {}, existingByMpls: new Map(), existingByEp: new Map(),
        mapping: {[ruta]: {include: true, episode_number: 1, episode_title: ''}},
      };
      _renderSeriesEpisodesTable();
      const cb = document.querySelector('#series-episodes-table input[type=checkbox]')
              || document.querySelector('.series-ep-row input[type=checkbox]');
      cb.checked = false;
      cb.dispatchEvent(new Event('change', {bubbles: true}));
      const incluido = _seriesState.mapping[ruta].include;
      // Y la pieza en sí: lo que llega al handler es la cadena exacta.
      const caja = document.createElement('div');
      const valor = "O'Brien \"x\" & <\/script><img>";
      caja.innerHTML = `<button onclick="window.__r = ${jsArg(valor)}">x</button>`;
      caja.querySelector('button').click();
      return {incluido, exacto: window.__r === valor};
    });

    // F06 — otra serie invalida la temporada y el mapping de la anterior.
    await caso('otraSerieVaciaLoDeLaAnterior', async () => {
      limpiar();
      const detalles2 = diferida();
      responder = (url) => url === '/api/tv-details/2' ? detalles2
        : url === '/api/tv-details/3' ? {details: {seasons: [{season_number: 1, name: 'Temporada TRES', episode_count: 8}]}}
        : null;
      _seriesState = {
        probe: {episode_candidates: []}, mode: 'tmdb',
        selectedSeries: {tmdb_id: 1, name: 'A'}, selectedSeason: {season_number: 1},
        seasonEpisodes: [{episode_number: 1}], mplsMatches: [{}],
        candidates: {2: {tmdb_id: 2, name: 'B'}, 3: {tmdb_id: 3, name: 'C'}},
        existingByMpls: new Map(), existingByEp: new Map(),
        mapping: {'/x.mpls': {include: true, episode_number: 1}},
      };
      const p2 = seriesSelectCandidate(2);
      const justoDespues = {
        mapping: Object.keys(_seriesState.mapping).length,
        temporada: _seriesState.selectedSeason,
        crearDeshabilitado: document.getElementById('series-create-btn').disabled,
      };
      await seriesSelectCandidate(3);
      detalles2.resolver({details: {seasons: [{season_number: 1, name: 'Temporada DOS', episode_count: 9}]}});
      await p2;
      return {...justoDespues,
              opciones: document.getElementById('series-season-select').textContent};
    });

    // F10 — el terminal de A con B ya corriendo.
    await caso('terminalDelWsEsDeSuSesion', async () => {
      limpiar();
      const refrescadas = [];
      const orig = window.refreshOpenProjectState;
      window.refreshOpenProjectState = (sid) => refrescadas.push(sid);
      queueState = {running: 'b', queue: []};
      _resetTerminalToastDedup();
      handleExecutionWsMessage('__ERROR__ boom', 'a');
      window.refreshOpenProjectState = orig;
      return {refrescadas, dedup: _lastTerminalToastSessionId};
    });

    // F13 — dos discos de la misma temporada: el singleton ya es del otro.
    async function crearSerie(progreso, cola) {
      responder = (url) => url === '/api/create-series-sessions' ? {queued: true, job: 'j1'}
        : url === '/api/series-create-progress' ? progreso : null;
      workbarEstado = {activo: null, cola: cola || [], interactivo: [], recientes: []};
      const ruta = '/mnt/isos/Serie/00800.mpls';
      _seriesState = {
        probe: {episode_candidates: [{mpls_path: ruta, mpls_name: '00800.mpls', duration_minutes: 42}],
                source_type: 'iso', source_path: 'Serie.iso', iso_path: 'Serie.iso'},
        mode: 'tmdb', selectedSeries: {tmdb_id: 1, name: 'Serie', year: 2020},
        selectedSeason: {season_number: 1}, seasonEpisodes: [], mplsMatches: [],
        candidates: {}, existingByMpls: new Map(), existingByEp: new Map(),
        mapping: {[ruta]: {include: true, episode_number: 1, episode_title: 'Uno'}},
      };
      const abiertos = [];
      const orig = window.openProject;
      window.openProject = (s) => abiertos.push(s.id);
      const fin = await Promise.race([
        seriesCreateSessions().then(() => 'terminó'),
        espera(30000).then(() => 'colgado'),
      ]);
      window.openProject = orig;
      return {fin, abiertos, toasts: toasts.map(t => t[0])};
    }
    await caso('serieConResultadoGuardado', async () => {
      limpiar();
      return crearSerie({job: 'j2', running: true,
                         resultados_por_job: {j1: {created: [{id: 'e1'}], failed: [],
                                                   skipped_existing: [], replaced_ids: []}}});
    });
    await caso('serieSinRastro', async () => {
      limpiar();
      return crearSerie({job: 'j2', running: true, resultados_por_job: {}});
    });
    await caso('serieQueEsperaTurnoNoSeAbandona', async () => {
      limpiar();
      const r = await crearSerie({job: 'j2', running: true, resultados_por_job: {}},
                                 [{id: 'j1', posicion: 1}]);
      return r;
    });

    // F28 — descartar un forzado y recuperarlo.
    await caso('recuperarUnForzado', async () => {
      limpiar();
      responder = () => ({available: true});
      const A = openProject(S('a', 'A.mkv'));
      const i = A.session.included_tracks.findIndex(t => t.track_type === 'subtitle');
      discardTrack(i);
      recoverTrack(A.session.discarded_tracks.length - 1);
      const sub = A.session.included_tracks.find(t => t.track_type === 'subtitle');
      return {tipo: sub.subtitle_type, forzado: sub.flag_forced};
    });

    // F23 — «Ver progreso» abre el trabajo de ESTE proyecto.
    await caso('verProgresoConSuReferencia', async () => {
      limpiar();
      responder = () => ({available: true});
      const s = S('a', 'A.mkv'); s.status = 'queued';
      openProject(s);
      const abiertos = [];
      const orig = window.abrirDetalleDeTrabajo;
      window.abrirDetalleDeTrabajo = (ref) => abiertos.push(ref === undefined ? 'SIN-REF' : ref);
      [...document.querySelectorAll('.btn')].find(b => (b.getAttribute('onclick') || '').includes('abrirDetalleDeTrabajo')).click();
      window.abrirDetalleDeTrabajo = orig;
      return {abiertos};
    });

    // F17 — el detalle de una ejecución vieja enseña SU log.
    await caso('detalleDeUnaEjecucionVieja', async () => {
      limpiar();
      responder = () => ({id: 's1', mkv_name: 'A.mkv', output_log: ['de la segunda'],
        execution_history: [
          {run_number: 1, output_log: ['de la primera'], phase_elapsed: {extract: 10}},
          {run_number: 2, output_log: ['de la segunda'], phase_elapsed: {extract: 99}}]});
      const v = await _workbarDetalles.rip({id: 's1', terminal: true, fase_n: 0,
        historial: {ref_log: 'sesion:s1#1', estado: 'done'}});
      return {primera: v.cuerpo.includes('de la primera'),
              segunda: v.cuerpo.includes('de la segunda')};
    });

    // F18 — una lectura fallida del detalle de una serie no la sustituye.
    await caso('detalleDeSerieSinRespuesta', async () => {
      limpiar();
      responder = () => null;
      return await _workbarDetalles.serie({id: 'j1', que: 'x'});
    });

    // F01/H1 — el detalle de un error HTTP, escapado y legible.
    await caso('detalleDeErrorEscapado', async () => {
      limpiar();
      const fetchOrig = window.fetch;
      window.fetch = async () => ({ok: false, status: 422, statusText: 'x', headers: new Headers(),
        json: async () => ({detail: [{loc: ['body', 'title'], msg: 'field <b>required</b>'}]})});
      await apiFetchReal('/api/algo', {method: 'POST'});
      window.fetch = fetchOrig;
      const toast = toasts[0] ? toasts[0][1] : '';
      const caja = document.createElement('div'); caja.innerHTML = toast;
      return {texto: caja.textContent, sinNegrita: !caja.querySelector('b'),
              sinObjeto: !toast.includes('[object Object]')};
    });

    // Extra — Escape pasa por el cierre de cada modal.
    await caso('escapeEsLaMismaSalida', async () => {
      limpiar();
      const decision = _seriesConfirmConflicts(1, 'S01E01');
      cerrarConEscape();
      const r = await Promise.race([decision, espera(2000).then(() => 'COLGADA')]);
      _trabajoModalRef = 'x';
      _trabajoModalUltimo = {id: 'x'};
      openModal('trabajo-modal');
      cerrarConEscape();
      return {decision: r, refDelModal: _trabajoModalRef,
              trabajoAbierto: document.getElementById('trabajo-modal').classList.contains('open')};
    });

    out.errores = window.__errores.slice();
    document.getElementById('__out').textContent = JSON.stringify(out);
  }, 700);
})();
</script>
"""


def _medir() -> dict:
    sesion = json.dumps(_sesion("__SID__", "__NOMBRE__"))
    fabrica = ("<script>window.__SES = (sid, nombre) => JSON.parse("
               + json.dumps(sesion) + ".split('__SID__').join(sid)"
               ".split('__NOMBRE__').join(nombre));</script>\n")
    return pagina_en_chrome(fabrica + _GUION, presupuesto_ms=120000)


@unittest.skipUnless(chrome(), "Chrome/Chromium no disponible")
class TestTab1YComun(unittest.TestCase):

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

    def test_S4_guardar_y_cerrar_uno_inactivo_deja_la_sesion_del_activo(self):
        """El bug: `currentSession` se quedaba en el cerrado y lo siguiente
        que se editaba en el activo iba a parar a la sesión cerrada."""
        c = self._caso("guardarYCerrarInactivo")
        self.assertTrue(c["cerrado"])
        self.assertTrue(c["activoEsB"])
        self.assertTrue(c["sesionActualEsB"], "currentSession apunta al proyecto cerrado")
        self.assertEqual(c["put"], ["/api/sessions/a"])

    def test_F03_la_comprobacion_del_origen_no_cambia_el_activo(self):
        c = self._caso("checkIsoNoCambiaElActivo")
        self.assertTrue(c["activoEsB"], "al volver la comprobación de A, el activo pasaba a A")
        self.assertEqual(c["nombreB"], "nuevo-B.mkv", "lo escrito en B no llegó a B")
        self.assertEqual(c["nombreA"], "A.mkv", "lo escrito en B fue a parar a A")
        self.assertTrue(c["bannerA"], "el aviso de origen no disponible va en el panel de A")
        self.assertFalse(c["bannerB"])

    def test_F05_si_el_guardado_falla_no_se_ejecuta(self):
        c = self._caso("ejecutarConElGuardadoFallido")
        self.assertFalse(c["comproboOrigen"])
        self.assertFalse(c["confirmAbierto"])
        self.assertTrue(c["sigueSucio"], "un guardado fallido no deja el proyecto limpio")

    def test_F04_revertir_el_nombre_va_a_su_proyecto(self):
        c = self._caso("revertirNombreTrasCambiar")
        self.assertEqual(c["nombreA"], "A-recalculado.mkv")
        self.assertEqual(c["inputA"], "A-recalculado.mkv")
        self.assertEqual(c["inputB"], "B.mkv", "el nombre de A se escribió en el panel de B")
        self.assertEqual(c["nombreB"], "B.mkv")

    def test_F04_revertir_el_nombre_va_a_la_sesion_viva(self):
        c = self._caso("revertirNombreConRefresco")
        self.assertEqual(c["input"], "A-recalculado.mkv")
        self.assertEqual(c["nombre"], "A-recalculado.mkv",
                         "el nombre nuevo fue a la sesión sustituida y se guardaría el viejo")
        self.assertFalse(c["manual"])

    def test_F11_un_refresco_conserva_lo_editado(self):
        c = self._caso("refrescoConservaLasEdiciones")
        self.assertEqual(c["etiqueta"], "Editado a mano")
        self.assertEqual(c["nombre"], "Editado.mkv")
        self.assertEqual(c["estado"], "done", "el estado sí es del servidor")

    def test_pintar_otro_proyecto_no_mueve_la_sesion_actual(self):
        self.assertTrue(self._caso("pintarOtroNoMueveLaSesion")["sesionActualEsB"])

    def test_F02_un_apostrofo_no_mata_el_handler(self):
        c = self._caso("apostrofoEnUnHandler")
        self.assertFalse(c["incluido"], "el onchange con la ruta con apóstrofo no llegó")
        self.assertTrue(c["exacto"], "jsArg no entrega la cadena exacta")

    def test_F06_otra_serie_vacia_temporada_y_mapping(self):
        c = self._caso("otraSerieVaciaLoDeLaAnterior")
        self.assertEqual(c["mapping"], 0)
        self.assertIsNone(c["temporada"])
        self.assertTrue(c["crearDeshabilitado"])
        self.assertIn("Temporada TRES", c["opciones"])
        self.assertNotIn("Temporada DOS", c["opciones"],
                         "la respuesta vieja de otra serie pintó sus temporadas")

    def test_F10_el_terminal_es_de_su_sesion(self):
        c = self._caso("terminalDelWsEsDeSuSesion")
        self.assertEqual(c["refrescadas"], ["a"])
        self.assertEqual(c["dedup"], "a")

    def test_F13_la_serie_recoge_su_resultado_guardado(self):
        c = self._caso("serieConResultadoGuardado")
        self.assertEqual(c["fin"], "terminó", "el bucle preguntaba cada 700 ms para siempre")
        self.assertIn("e1", c["abiertos"])
        self.assertIn("success", c["toasts"])

    def test_F13_sin_rastro_se_abandona_y_se_dice(self):
        c = self._caso("serieSinRastro")
        self.assertEqual(c["fin"], "terminó")
        self.assertIn("warning", c["toasts"])

    def test_F13_esperando_turno_no_se_abandona(self):
        """Esperar detrás de un rip de 40 minutos es legítimo."""
        self.assertEqual(self._caso("serieQueEsperaTurnoNoSeAbandona")["fin"], "colgado")

    def test_F28_un_forzado_recuperado_sigue_siendo_forzado(self):
        c = self._caso("recuperarUnForzado")
        self.assertEqual(c["tipo"], "forced")
        self.assertTrue(c["forzado"])

    def test_F23_ver_progreso_lleva_su_referencia(self):
        self.assertEqual(self._caso("verProgresoConSuReferencia")["abiertos"], ["a"])

    def test_F17_una_ejecucion_vieja_enseña_su_log(self):
        c = self._caso("detalleDeUnaEjecucionVieja")
        self.assertTrue(c["primera"])
        self.assertFalse(c["segunda"], "enseñaba el log de la última ejecución")

    def test_F18_una_lectura_fallida_no_sustituye_la_vista(self):
        self.assertEqual(self._caso("detalleDeSerieSinRespuesta"), {"sinDatos": True})

    def test_F01_el_detalle_de_un_error_se_escapa_y_se_lee(self):
        c = self._caso("detalleDeErrorEscapado")
        self.assertTrue(c["sinNegrita"], "el detalle del servidor se interpretó como HTML")
        self.assertTrue(c["sinObjeto"], "un 422 salía como [object Object]")
        self.assertIn("body.title", c["texto"])
        self.assertIn("<b>required</b>", c["texto"])

    def test_escape_pasa_por_el_cierre_de_cada_modal(self):
        c = self._caso("escapeEsLaMismaSalida")
        self.assertEqual(c["decision"], "cancel", "la promesa del diálogo se quedaba colgada")
        self.assertIsNone(c["refDelModal"], "el modal de trabajo se ocultaba sin cerrarse")
        self.assertFalse(c["trabajoAbierto"])


if __name__ == "__main__":
    unittest.main()
