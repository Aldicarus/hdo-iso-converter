"""La pestaña «Combinar MKV» (Tab 4), ejecutada en Chrome sobre el `index.html` real.

Lo que se prueba es lo que hace la interfaz, no su fuente: se abre un proyecto
con los datos que el BACKEND produce (`mux_payloads`, sobre las pistas reales
del caso de Toy Story), se pulsa lo que el usuario pulsaría y se mira qué
peticiones salen y qué queda en pantalla. `apiFetch` lo sustituye un grabador
que contesta lo que cada caso decide, con retrasos donde el caso trata del
orden en que vuelven las respuestas.

Los flujos que fijan las reglas del módulo:

- **una edición manda el campo ENTERO**: la lista de pistas completa y en su
  orden (audio antes que subtítulos), no un parche;
- **una respuesta vieja no pisa una nueva**: un GET que salió antes de un PUT
  y vuelve después se descarta;
- **un cambio rechazado se deshace**: si el servidor no acepta el PUT, se
  vuelve a leer el proyecto y la pantalla deja de enseñar lo que no existe;
- **lo que se dice sale de la respuesta**: cancelar algo que ya terminó no
  anuncia una cancelación;
- **el gráfico no dibuja una recta que no describe nada** (montaje distinto,
  medida no concluyente);
- **lo que se está escribiendo sobrevive a un repintado** del poll.

Se salta sin Chrome. Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_tab4_combinar -v
"""
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import catalogo_es, chrome, pagina_en_chrome  # noqa: E402

CHROME = chrome()

_DRIVER = r"""
<script>
window.addEventListener('load', () => setTimeout(async function () {
  const D = %(datos)s;
  const out = {casos: {}};
  const espera = (ms) => new Promise(r => setTimeout(r, ms));
  const copia = (x) => JSON.parse(JSON.stringify(x));
  let manejador = null;
  window.__pet = [];
  // Sortable llega por CDN y sin red no carga: lo justo para que la pestaña
  // instale su orden y el caso pueda llamar a sus manejadores.
  if (typeof Sortable === 'undefined') {
    window.Sortable = {create: (el, options) => ({el, options, destroy() {}})};
  }
  // El grabador: cada petición queda anotada con su método, su URL, su cuerpo
  // y el timeout con el que se pidió; la respuesta la decide el caso.
  apiFetch = async (url, opts = {}, timeout) => {
    const m = (opts.method || 'GET').toUpperCase();
    let body = null;
    try { body = opts.body ? JSON.parse(opts.body) : null; } catch (_) {}
    // Solo lo de esta pestaña: la columna de trabajo y el resto de la app
    // también preguntan, y contestarles un proyecto los rompería.
    if (!String(url).startsWith('/api/mux')) return null;
    window.__pet.push({m, url: String(url), body, timeout: timeout || null});
    return manejador ? manejador(m, String(url), body) : null;
  };
  const caso = async (nombre, fn) => {
    window.__pet = [];
    manejador = null;
    try { out.casos[nombre] = await fn(); }
    catch (e) { out.casos[nombre] = {fallo: String(e && e.stack || e)}; }
  };
  const fila = (pid, clave) => document.querySelector(
    `#mux-cuerpo-${pid} li[data-clave="${clave}"]`);
  const puts = () => window.__pet.filter(x => x.m === 'PUT');
  const toasts = () => [...document.querySelectorAll('#toast-container .toast, #toast-container > div')]
    .map(t => t.textContent.trim());
  const limpiarToasts = () => { const c = document.getElementById('toast-container'); if (c) c.innerHTML = ''; };
  // Un servidor con memoria: lo que un PUT o un POST cambia es lo que el
  // siguiente GET devuelve. Contestar siempre el proyecto de partida hacía
  // que la relectura que dispara la columna de trabajo deshiciera el cambio
  // —un servidor de verdad no lo haría—, y el caso medía el arnés.
  const servidor = (d, extra = {}) => {
    let actual = copia(d);
    return (m, url, body) => {
      if (m === 'PUT') Object.assign(actual, body);
      if (m === 'POST' && url.endsWith('/ejecutar')) actual.status = 'queued';
      if (extra[m]) return extra[m](url, body, actual);
      return copia(actual);
    };
  };
  const devolverPut = (d) => servidor(d);

  // ── Los formatos ─────────────────────────────────────────────────────
  await caso('formatos', async () => ({
    fps_cine: _muxFps([24000, 1001]), fps_24: _muxFps([24, 1]),
    fps_medida: _muxFps([2400013, 100000], true), fps_medida_corta: _muxFps([2400013, 100000]),
    desfase_neg: _muxDesfase(-5760), desfase_pos: _muxDesfase(120), desfase_cero: _muxDesfase(0),
    magnitud: _muxMagnitud(-2350), canales8: _muxCanales(8), canales5: _muxCanales(5),
    cifra: _muxCifra('≤ 40 ms · 6.2 ms'),
    idioma_latino: _muxIdioma({idioma: 'spa', idioma_ietf: 'es-419'}),
    idioma_cat: _muxIdioma({idioma: 'cat', idioma_ietf: 'ca'}),
    tamano_sub: _muxTamano(43000), tamano_audio: _muxTamano(489000000),
    tamano_gb: _muxTamano(3400000000),
  }));

  // ── Editar el nombre de una pista manda la lista entera ──────────────
  await caso('nombre_de_pista', async () => {
    const d = copia(D.listo); d.id = 'mx1';
    manejador = devolverPut(d);
    await abrirMux(d.id, d);
    const el = document.getElementById('mux-mx1-pista-1-1');
    el.value = 'Spanish TrueHD Atmos 7.1 DCP'; marcarTocado(el);
    el.dispatchEvent(new Event('change'));
    await espera(30);
    return {antes: d.pistas, puts: puts().map(p => ({url: p.url, body: p.body})),
            valor: document.getElementById('mux-mx1-pista-1-1').value};
  });

  await caso('marca_por_defecto', async () => {
    const d = copia(D.listo); d.id = 'mx2';
    manejador = devolverPut(d);
    await abrirMux(d.id, d);
    fila('mx2', '1:2').querySelector('.flag-pill').click();
    await espera(30);
    return {antes: d.pistas, puts: puts().map(p => p.body)};
  });

  await caso('quitar_pista', async () => {
    const d = copia(D.listo); d.id = 'mx3';
    manejador = devolverPut(d);
    await abrirMux(d.id, d);
    fila('mx3', '1:3').querySelector('.track-actions button').click();
    await espera(30);
    return {puts: puts().map(p => p.body), queda: !!fila('mx3', '1:3')};
  });

  await caso('anadir_pista', async () => {
    const d = copia(D.listo); d.id = 'mx4';
    manejador = devolverPut(d);
    await abrirMux(d.id, d);
    const btn = [...document.querySelectorAll('#mux-cuerpo-mx4 .mux-disponible button')]
      .find(b => /,\s*0,\s*1\)$/.test(b.getAttribute('onclick') || ''));
    if (!btn) return {fallo: 'no hay botón para añadir 0:1'};
    btn.click();
    await espera(30);
    return {puts: puts().map(p => p.body), tipos: Object.fromEntries(
      d.fuentes.flatMap((f, i) => f.pistas.map(p => [`${i}:${p.id}`, p.tipo])))};
  });

  await caso('capitulos_de_a', async () => {
    const d = copia(D.listo); d.id = 'mx5';
    manejador = devolverPut(d);
    await abrirMux(d.id, d);
    const btn = [...document.querySelectorAll('#mux-cuerpo-mx5 .mux-modos button')]
      .find(b => (b.getAttribute('onclick') || '').includes('"video"'));
    btn.click();
    await espera(30);
    return {puts: puts().map(p => p.body)};
  });

  await caso('aceptar_el_riesgo', async () => {
    const d = copia(D.decision); d.id = 'mx6';
    manejador = devolverPut(d);
    await abrirMux(d.id, d);
    const btn = document.querySelector('#mux-cuerpo-mx6 .mux-motivos-pie button');
    if (!btn) return {fallo: 'sin botón de aceptar el riesgo'};
    btn.click();
    const abierto = document.getElementById('confirm-modal').classList.contains('open');
    const antes = puts().length;
    document.getElementById('confirm-ok-btn').click();
    await espera(30);
    return {abierto, antes, puts: puts().map(p => p.body)};
  });

  await caso('combinar', async () => {
    const d = copia(D.listo); d.id = 'mx7';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    const btn = document.getElementById('mux-combinar-mx7');
    const habilitado = btn && !btn.disabled;
    btn.click();
    await espera(30);
    const despues = document.querySelector('#mux-cuerpo-mx7 .project-action-bar button');
    return {habilitado, posts: window.__pet.filter(p => p.m === 'POST').map(p => p.url),
            despues_deshabilitado: !!(despues && despues.disabled)};
  });

  // ── Una respuesta vieja no pisa la nueva ─────────────────────────────
  await caso('respuesta_vieja', async () => {
    const d = copia(D.listo); d.id = 'mx8';
    await abrirMux(d.id, d);
    const p = _muxProyecto('mx8');
    manejador = (m, url, body) => {
      if (m === 'GET') return new Promise(r => setTimeout(() =>
        r(Object.assign(copia(d), {nombre_salida: 'Old name.mkv'})), 200));
      return Object.assign(copia(d), {nombre_salida: body.nombre_salida, nombre_manual: true});
    };
    const lectura = _muxLeer(p);              // sale ANTES y vuelve DESPUÉS
    await espera(10);
    const el = document.getElementById('mux-mx8-nombre');
    el.value = 'New name.mkv';
    await muxNombreSalida('mx8', el);
    await lectura;
    await espera(30);
    return {datos: p.datos.nombre_salida,
            pantalla: document.getElementById('mux-mx8-nombre').value,
            metodos: window.__pet.map(x => x.m)};
  });

  // ── Un cambio rechazado se deshace ───────────────────────────────────
  await caso('cambio_rechazado', async () => {
    const d = copia(D.listo); d.id = 'mx9';
    manejador = (m) => (m === 'PUT' ? null : copia(d));
    await abrirMux(d.id, d);
    fila('mx9', '1:3').querySelector('.track-actions button').click();
    await espera(60);
    return {metodos: window.__pet.map(x => x.m), vuelve: !!fila('mx9', '1:3'),
            pistas: _muxProyecto('mx9').datos.pistas.length, original: d.pistas.length};
  });

  // ── Lo que se dice al cancelar sale de la respuesta ──────────────────
  await caso('cancelar', async () => {
    const r = {};
    limpiarToasts();
    manejador = (m, url) => (url.endsWith('/cancelar') ? {ok: false} : null);
    await cancelarCombinacionMux('mx10');
    r.nada = toasts();
    limpiarToasts();
    manejador = (m, url) => (url.endsWith('/cancelar') ? {ok: true, descartada: true} : null);
    await cancelarCombinacionMux('mx10');
    r.descartada = toasts();
    limpiarToasts();
    manejador = (m, url) => (url.endsWith('/cancelar') ? {ok: true} : null);
    await cancelarCombinacionMux('mx10');
    r.cancelada = toasts();
    // La columna de trabajo: cada trabajo de esta pestaña por su endpoint.
    window.__pet = [];
    manejador = (m, url) => (/\/cancelar$/.test(url) ? {ok: true} : null);
    cancelarTrabajoActivo({tab: 'mux', detalle: 'sincronia_mux', id: 'mux_sincronia:mx10', que: 'x'});
    document.getElementById('confirm-ok-btn').click();
    await espera(30);
    cancelarTrabajoActivo({tab: 'mux', detalle: 'combinar', id: 'mx10', que: 'x'});
    document.getElementById('confirm-ok-btn').click();
    await espera(30);
    r.posts = window.__pet.filter(p => p.m === 'POST').map(p => p.url);
    return r;
  });

  // ── El modal de creación ─────────────────────────────────────────────
  await caso('crear_mismo_fichero', async () => {
    abrirNuevaCombinacion();
    _muxNueva.fuentes = [{ruta: '/mnt/library/a.mkv', nombre: 'a.mkv'},
                         {ruta: '/mnt/library/a.mkv', nombre: 'a.mkv'}];
    _muxPintarHuecos();
    const err = document.getElementById('mux-nueva-error');
    const r = {deshabilitado: document.getElementById('mux-nueva-crear').disabled,
               error_visible: err.style.display !== 'none',
               error: document.getElementById('mux-nueva-error-texto').textContent};
    intercambiarFuentesMux();
    _muxNueva.fuentes[1] = {ruta: '/mnt/isos/b.mkv', nombre: 'b.mkv'};
    _muxPintarHuecos();
    r.distintos_habilitado = !document.getElementById('mux-nueva-crear').disabled;
    r.distintos_sin_error = document.getElementById('mux-nueva-error').style.display === 'none';
    closeModal('mux-nueva-modal');
    return r;
  });

  await caso('crear_bien', async () => {
    const d = copia(D.listo); d.id = 'mx11';
    abrirNuevaCombinacion();
    _muxNueva.fuentes = [{ruta: '/mnt/library/a.mkv', nombre: 'a.mkv'},
                         {ruta: '/mnt/isos/b.mkv', nombre: 'b.mkv'}];
    _muxPintarHuecos();
    manejador = (m, url) => (m === 'POST' && url.startsWith('/api/mux/create')) ? copia(d)
                          : url.startsWith('/api/mux/create/progress') ? {activo: true}
                          : copia(d);
    await crearCombinacion();
    const post = window.__pet.find(p => p.m === 'POST');
    return {url: post && post.url, body: post && post.body, timeout: post && post.timeout,
            largo: API_FETCH_TIMEOUT_LARGO,
            cerrado: !document.getElementById('mux-nueva-modal').classList.contains('open'),
            panel: !!document.getElementById('mux-panel-mx11')};
  });

  await caso('crear_mal', async () => {
    abrirNuevaCombinacion();
    _muxNueva.fuentes = [{ruta: '/mnt/library/a.mkv', nombre: 'a.mkv'},
                         {ruta: '/mnt/isos/b.mkv', nombre: 'b.mkv'}];
    _muxPintarHuecos();
    let job = '';
    manejador = (m, url) => {
      if (m === 'POST') { job = new URL(url, 'http://x').searchParams.get('job'); return null; }
      if (url.startsWith('/api/mux/create/progress')) return {activo: false, job, error: 'mkvmerge -J failed on b.mkv'};
      return null;
    };
    await crearCombinacion();
    const r = {abierto: document.getElementById('mux-nueva-modal').classList.contains('open'),
               error: document.getElementById('mux-nueva-error-texto').textContent,
               huecos: document.getElementById('mux-nueva-huecos').style.display !== 'none'};
    // Y el error de OTRA creación (otro sello) no se toma por el suyo.
    manejador = (m, url) => (url.startsWith('/api/mux/create/progress')
      ? {activo: false, job: 'otro', error: 'not mine'} : null);
    await crearCombinacion();
    r.ajeno = document.getElementById('mux-nueva-error-texto').textContent;
    closeModal('mux-nueva-modal');
    return r;
  });

  // ── Lo que dice el veredicto ────────────────────────────────────────
  await caso('veredicto', async () => {
    const texto = (d) => { const c = document.createElement('div');
                           c.innerHTML = _muxVeredictoHTML(d); return c.textContent; };
    const plan = (d) => { const c = document.createElement('div');
                          c.innerHTML = _muxPlanHTML(d); return c.textContent; };
    return {retraso: texto(D.marcha), otro_ritmo: texto(D.listo),
            plan_retraso: plan(D.marcha), plan_otro: plan(D.listo), plan_montaje: plan(D.cola)};
  });

  // ── El gráfico ───────────────────────────────────────────────────────
  await caso('grafico', async () => {
    const medir = (d) => {
      const c = document.createElement('div');
      c.innerHTML = _muxPanelHTML({id: d.id, datos: d});
      const zonas = [...c.querySelectorAll('.mux-graf-zona')];
      return {recta: c.querySelectorAll('.mux-graf-ajuste').length,
              leyenda_recta: c.querySelectorAll('.mux-leyenda-recta').length,
              puntos: c.querySelectorAll('.mux-graf-punto').length,
              descartados: c.querySelectorAll('.mux-graf-descartado').length,
              leyenda_descartado: c.querySelectorAll('.mux-leyenda-descartado').length,
              zonas: zonas.length,
              radio_min: Math.min(...zonas.map(z => Number(z.getAttribute('r')))),
              sin_tooltip: zonas.filter(z => !z.getAttribute('data-tooltip')).length,
              filas_tabla: c.querySelectorAll('.mux-tabla tbody tr').length};
    };
    return {otro_ritmo: medir(D.listo), montaje: medir(D.cola), no_concluyente: medir(D.decision)};
  });

  // ── Reordenar con Sortable ───────────────────────────────────────────
  await caso('reordenar', async () => {
    const d = copia(D.listo); d.id = 'mx13';
    manejador = devolverPut(d);
    await abrirMux(d.id, d);
    const lista = document.querySelector('#mux-cuerpo-mx13 .mux-orden[data-tipo="audio"]');
    lista.insertBefore(lista.lastElementChild, lista.firstElementChild);
    const p = _muxProyecto('mx13');
    p._sortables[0].options.onStart();
    p._sortables[0].options.onEnd({});
    await espera(30);
    return {antes: d.pistas, puts: puts().map(x => x.body)};
  });

  // ── Lo que se está escribiendo sobrevive a un repintado ──────────────
  await caso('anclaje', async () => {
    const d = copia(D.listo); d.id = 'mx14';
    await abrirMux(d.id, d);
    const el = document.getElementById('mux-mx14-pista-1-2');
    el.value = 'half typed'; marcarTocado(el); el.focus();
    _muxAplicar(_muxProyecto('mx14'), Object.assign(copia(d), {nombre_salida: 'Server renamed.mkv'}));
    return {escrito: document.getElementById('mux-mx14-pista-1-2').value,
            foco: document.activeElement && document.activeElement.id,
            del_servidor: document.getElementById('mux-mx14-nombre').value};
  });

  // ── Un repintado a mitad de un arrastre espera a que se suelte ───────
  await caso('arrastre', async () => {
    const d = copia(D.listo); d.id = 'mx15';
    await abrirMux(d.id, d);
    const p = _muxProyecto('mx15');
    p._sortables[0].options.onStart();
    _muxAplicar(p, Object.assign(copia(d), {nombre_salida: 'During drag.mkv'}));
    const durante = document.getElementById('mux-mx15-nombre').value;
    p._sortables[0].options.onEnd({});
    const despues = document.getElementById('mux-mx15-nombre').value;
    return {durante, despues};
  });

  // ── Las dos vistas del modal de trabajo ──────────────────────────────
  await caso('detalle_combinar', async () => {
    const d = copia(D.hecho); d.id = 'mxD';
    d.historial = [Object.assign(copia(d.historial[0]), {run_number: 1, output_log: ['run one line']}),
                   Object.assign(copia(d.historial[0]), {run_number: 2, output_log: ['run two line']})];
    d.output_log = ['live line'];
    manejador = () => copia(d);
    const vista = await _workbarDetalles['combinar']({
      id: 'mxD', sobre: 'mxD', terminal: true, fase_n: 0,
      historial: {ref_log: 'mux:mxD#1', estado: 'done'}});
    manejador = () => null;
    const caido = await _workbarDetalles['combinar']({id: 'mxD', sobre: 'mxD', fase_n: 2});
    return {cuerpo: vista.cuerpo, lateral_fases: (vista.lateral.match(/cmv40-tl-step /g) || []).length,
            hechas: (vista.lateral.match(/cmv40-tl-done/g) || []).length, caido};
  });

  await caso('detalle_medida', async () => {
    const d = copia(D.midiendo);
    manejador = () => copia(d);
    const viva = await _workbarDetalles['sincronia_mux']({id: 'mux_sincronia:' + d.id, terminal: false});
    const vieja = await _workbarDetalles['sincronia_mux']({id: 'mux_sincronia:' + d.id, terminal: true});
    const pedidas = window.__pet.map(p => p.url);
    return {viva_cuerpo: viva.cuerpo, vieja_sin_detalle: vieja.sinDetalle, vieja_cuerpo: vieja.cuerpo,
            pedidas};
  });

  out.errores = window.__errores || [];
  document.getElementById('__out').textContent = JSON.stringify(out);
}, 60));
</script>
"""


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestLaPestanaCombinarEnChrome(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from mux_payloads import proyectos_mux
        cls.D = proyectos_mux()
        cls.cat = catalogo_es()
        r = pagina_en_chrome(_DRIVER % {"datos": json.dumps(cls.D, ensure_ascii=False)},
                             presupuesto_ms=60000)
        cls.casos = r["casos"]
        cls.errores = r.get("errores") or []

    def caso(self, nombre):
        c = self.casos.get(nombre)
        self.assertIsNotNone(c, f"el caso {nombre} no llegó a correr")
        self.assertNotIn("fallo", c, c.get("fallo") if isinstance(c, dict) else c)
        return c

    def test_la_pagina_no_lanza(self):
        self.assertEqual(self.errores, [])

    def test_los_formatos(self):
        c = self.caso("formatos")
        self.assertEqual(c["fps_cine"], "23,976")
        self.assertEqual(c["fps_24"], "24")
        self.assertEqual(c["fps_medida"], "24,00013")
        self.assertEqual(c["fps_medida_corta"], "24")
        self.assertEqual(c["desfase_neg"], "−5,76 s")
        self.assertEqual(c["desfase_pos"], "+120 ms")
        self.assertEqual(c["desfase_cero"], "0 ms")
        self.assertEqual(c["magnitud"], "2,35 s")
        self.assertEqual(c["canales8"], "7.1")
        self.assertEqual(c["canales5"], "5 ch")
        self.assertEqual(c["cifra"], "≤ 40 ms · 6,2 ms")
        self.assertEqual(c["idioma_latino"], "Castellano (es-419)")
        self.assertEqual(c["idioma_cat"], "Catalán")
        self.assertEqual((c["tamano_sub"], c["tamano_audio"], c["tamano_gb"]),
                         ("43 KB", "489 MB", "3,4 GB"))

    def _esperadas(self, pistas, cambio=None, sin=None):
        out = []
        for p in pistas:
            q = {"fuente": p["fuente"], "id": p["id"], "nombre": p["nombre"],
                 "por_defecto": bool(p["por_defecto"]), "forzada": bool(p["forzada"])}
            if sin and (q["fuente"], q["id"]) == sin:
                continue
            if cambio and (q["fuente"], q["id"]) == cambio[0]:
                q.update(cambio[1])
            out.append(q)
        return out

    def test_editar_el_nombre_manda_la_lista_entera(self):
        c = self.caso("nombre_de_pista")
        self.assertEqual(len(c["puts"]), 1, c["puts"])
        self.assertEqual(c["puts"][0]["url"], "/api/mux/mx1")
        self.assertEqual(c["puts"][0]["body"]["pistas"], self._esperadas(
            c["antes"], cambio=((1, 1), {"nombre": "Spanish TrueHD Atmos 7.1 DCP"})))
        self.assertEqual(c["valor"], "Spanish TrueHD Atmos 7.1 DCP")

    def test_la_marca_por_defecto_cambia_solo_en_su_pista(self):
        c = self.caso("marca_por_defecto")
        self.assertEqual(len(c["puts"]), 1)
        antes = next(p for p in c["antes"] if (p["fuente"], p["id"]) == (1, 2))
        self.assertEqual(c["puts"][0]["pistas"], self._esperadas(
            c["antes"], cambio=((1, 2), {"por_defecto": not antes["por_defecto"]})))

    def test_quitar_una_pista(self):
        c = self.caso("quitar_pista")
        self.assertEqual(len(c["puts"]), 1)
        claves = [(p["fuente"], p["id"]) for p in c["puts"][0]["pistas"]]
        self.assertNotIn((1, 3), claves)
        self.assertFalse(c["queda"])

    def test_anadir_va_al_final_de_su_tipo_y_sin_robar_la_marca(self):
        c = self.caso("anadir_pista")
        self.assertEqual(len(c["puts"]), 1)
        pistas = c["puts"][0]["pistas"]
        tipos = [c["tipos"][f'{p["fuente"]}:{p["id"]}'] for p in pistas]
        # Todo el audio antes que los subtítulos, y la nueva la última del audio.
        self.assertEqual(tipos, sorted(tipos, key=lambda t: t != "audio"))
        ultima_de_audio = [p for p, t in zip(pistas, tipos) if t == "audio"][-1]
        self.assertEqual((ultima_de_audio["fuente"], ultima_de_audio["id"]), (0, 1))
        # Ya había un audio por defecto: la añadida no lo es aunque en su MKV sí.
        self.assertFalse(ultima_de_audio["por_defecto"])
        self.assertEqual(sum(p["por_defecto"] for p, t in zip(pistas, tipos) if t == "audio"), 1)

    def test_los_capitulos_de_otro_mkv(self):
        c = self.caso("capitulos_de_a")
        self.assertEqual(c["puts"], [{"capitulos_de": "video"}])

    def test_aceptar_el_riesgo_pregunta_antes(self):
        c = self.caso("aceptar_el_riesgo")
        self.assertTrue(c["abierto"], "el diálogo de confirmación no se abrió")
        self.assertEqual(c["antes"], 0, "guardó antes de confirmar")
        self.assertEqual(c["puts"], [{"acepta_riesgo": True}])

    def test_combinar_encola_y_bloquea_el_boton(self):
        c = self.caso("combinar")
        self.assertTrue(c["habilitado"])
        self.assertEqual(c["posts"], ["/api/mux/mx7/ejecutar"])
        self.assertTrue(c["despues_deshabilitado"])

    def test_una_respuesta_vieja_no_pisa_la_nueva(self):
        c = self.caso("respuesta_vieja")
        self.assertEqual(c["metodos"], ["GET", "PUT"])
        self.assertEqual(c["datos"], "New name.mkv")
        self.assertEqual(c["pantalla"], "New name.mkv")

    def test_un_cambio_rechazado_se_deshace(self):
        c = self.caso("cambio_rechazado")
        self.assertEqual(c["metodos"], ["PUT", "GET"])
        self.assertTrue(c["vuelve"], "la pista quitada no volvió a la pantalla")
        self.assertEqual(c["pistas"], c["original"])

    def test_lo_que_se_dice_al_cancelar_sale_de_la_respuesta(self):
        c = self.caso("cancelar")
        self.assertIn(self.cat["tab4.no_habia_nada_que_cancelar"], " ".join(c["nada"]))
        self.assertNotIn(self.cat["tab4.combinacion_cancelada"], " ".join(c["nada"]))
        self.assertIn(self.cat["tab4.quitada_de_la_cola"], " ".join(c["descartada"]))
        self.assertIn(self.cat["tab4.combinacion_cancelada"], " ".join(c["cancelada"]))
        self.assertEqual(c["posts"], ["/api/mux/mx10/medir/cancelar", "/api/mux/mx10/cancelar"])

    def test_dos_veces_el_mismo_fichero_no_se_puede_crear(self):
        c = self.caso("crear_mismo_fichero")
        self.assertTrue(c["deshabilitado"])
        self.assertTrue(c["error_visible"])
        self.assertEqual(c["error"], self.cat["tab4.los_dos_son_el_mismo_fichero"])
        self.assertTrue(c["distintos_habilitado"])
        self.assertTrue(c["distintos_sin_error"])

    def test_crear_lee_los_dos_con_el_timeout_largo_y_abre_el_proyecto(self):
        c = self.caso("crear_bien")
        self.assertTrue(c["url"].startswith("/api/mux/create?job="), c["url"])
        self.assertEqual(c["body"], {"video": "/mnt/library/a.mkv", "resto": "/mnt/isos/b.mkv"})
        self.assertEqual(c["timeout"], c["largo"])
        self.assertTrue(c["cerrado"])
        self.assertTrue(c["panel"])

    def test_un_fallo_al_crear_lo_dice_en_el_modal_y_solo_el_suyo(self):
        c = self.caso("crear_mal")
        self.assertTrue(c["abierto"])
        self.assertTrue(c["huecos"], "tras el fallo se vuelve a la selección")
        self.assertEqual(c["error"], "mkvmerge -J failed on b.mkv")
        self.assertEqual(c["ajeno"], self.cat["tab4.no_se_pudieron_leer"])

    def _tr(self, clave, **p):
        v = self.cat[clave]
        for k, x in p.items():
            v = v.replace("{" + k + "}", str(x))
        return v

    def test_el_veredicto_dice_el_sentido_una_vez(self):
        """«Llega 2,35 s más tarde» y no «llega +2,35 s más tarde»: la frase
        ya dice el sentido, y el signo lo decía dos veces."""
        c = self.caso("veredicto")
        self.assertIn(self._tr("tab4.v_retraso_b_tarde", ms="2,35 s"), c["retraso"])
        self.assertIn(self._tr("tab4.v_retraso", ms="+2,35 s"), c["retraso"])
        self.assertIn(self._tr("tab4.plan_b_movidas", ms="−2,35 s", n=6), c["plan_retraso"])
        # Otro ritmo: el vídeo se retima a la cadencia medida, escrita entera.
        self.assertIn(self._tr("tab4.v_otro_ritmo", a="23,976", b="24"), c["otro_ritmo"])
        self.assertIn(self._tr("tab4.plan_retimar", de="23,976", a="24"), c["plan_otro"])
        self.assertIn(self.cat["tab4.plan_sin_ajuste_montaje"], c["plan_montaje"])

    def test_el_grafico_solo_dibuja_la_recta_cuando_describe_algo(self):
        c = self.caso("grafico")
        o, m, n = c["otro_ritmo"], c["montaje"], c["no_concluyente"]
        self.assertEqual((o["recta"], o["leyenda_recta"]), (1, 1))
        self.assertEqual((m["recta"], m["leyenda_recta"]), (0, 0))
        self.assertEqual((n["recta"], n["leyenda_recta"]), (0, 0))
        self.assertEqual((o["puntos"], o["descartados"], o["leyenda_descartado"]), (7, 0, 0))
        self.assertEqual((n["puntos"], n["descartados"], n["leyenda_descartado"]), (3, 4, 1))
        for v in (o, m, n):
            self.assertEqual(v["zonas"], 7)
            self.assertGreaterEqual(v["radio_min"], 12, "la zona de cada punto mide menos de 24 px")
            self.assertEqual(v["sin_tooltip"], 0)
            self.assertEqual(v["filas_tabla"], 7, "la tabla gemela no trae los siete puntos")

    def test_reordenar_manda_el_orden_nuevo(self):
        c = self.caso("reordenar")
        self.assertEqual(len(c["puts"]), 1)
        nuevo = [(p["fuente"], p["id"]) for p in c["puts"][0]["pistas"]]
        audio = [(p["fuente"], p["id"]) for p in c["antes"]][:4]
        self.assertEqual(nuevo[:4], [audio[-1]] + audio[:-1])

    def test_lo_que_se_escribe_sobrevive_al_repintado(self):
        c = self.caso("anclaje")
        self.assertEqual(c["escrito"], "half typed")
        self.assertEqual(c["foco"], "mux-mx14-pista-1-2")
        self.assertEqual(c["del_servidor"], "Server renamed.mkv")

    def test_un_arrastre_aplaza_el_repintado(self):
        c = self.caso("arrastre")
        self.assertNotEqual(c["durante"], "During drag.mkv")
        self.assertEqual(c["despues"], "During drag.mkv")

    def test_el_detalle_de_una_ejecucion_es_el_de_esa_ejecucion(self):
        c = self.caso("detalle_combinar")
        self.assertIn("run one line", c["cuerpo"])
        self.assertNotIn("run two line", c["cuerpo"])
        self.assertNotIn("live line", c["cuerpo"])
        self.assertEqual(c["lateral_fases"], 3)
        self.assertEqual(c["hechas"], 3)
        # Un timeout no es «se borró»: el armazón conserva la vista buena.
        self.assertEqual(c["caido"], {"sinDatos": True})

    def test_el_detalle_de_la_medida(self):
        c = self.caso("detalle_medida")
        self.assertIn(self.cat["workbar.n_de_total"].replace("{n}", "3").replace("{total}", "7"),
                      c["viva_cuerpo"])
        self.assertEqual(c["vieja_sin_detalle"], "efimero")
        self.assertEqual(c["vieja_cuerpo"], "")
        self.assertTrue(all(u.startswith("/api/mux/mux_Blade") for u in c["pedidas"]), c["pedidas"])


if __name__ == "__main__":
    unittest.main()
