"""La pestaña «Multiplexar MKV» (Tab 4), ejecutada en Chrome sobre el `index.html` real.

Lo que se prueba es lo que hace la interfaz, no su fuente: se abre un proyecto
con los datos que el BACKEND produce (`mux_payloads`, sobre las pistas reales
del caso de Toy Story), se pulsa lo que el usuario pulsaría y se mira qué
peticiones salen y qué queda en pantalla. `apiFetch` lo sustituye un grabador
que contesta lo que cada caso decide, con retrasos donde el caso trata del
orden en que vuelven las respuestas.

Los flujos que fijan las reglas del módulo:

- **una edición manda el campo ENTERO** y con el esquema: la lista de pistas
  completa, el vídeo delante y el audio antes que los subtítulos — no un
  parche, y nunca sin `esquema` (un JS viejo borraría el vídeo);
- **el vídeo sale de un solo MKV y entero**: elegir el de B quita el de A;
- **los patrones los resuelve el servidor sobre lo guardado**: se espera a lo
  que está en vuelo y, mientras el patrón vuela, no se toca la selección;
- **antes de multiplexar se avisa de lo que se quedó vacío**: el proyecto
  nace sin nada elegido y se va sección a sección;
- **una respuesta vieja no pisa una nueva**, y **un cambio rechazado se
  deshace**;
- **lo que se dice sale de la respuesta**: cancelar algo que ya terminó no
  anuncia una cancelación;
- **el gráfico no dibuja una recta que no describe nada**;
- **lo que se está escribiendo sobrevive a un repintado** del poll.

Se salta sin Chrome. Ejecutar desde la raíz del repo:
    python3 app/tests/correr_suite.py test_tab4_multiplexar
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
  // Una fila de «En el MKV final» por su clave, y un interruptor de una
  // columna por lo que llama al cambiar.
  const fila = (pid, clave) => document.querySelector(
    `#mux-cuerpo-${pid} li[data-clave="${clave}"]`);
  const interruptor = (pid, llamada) => [...document.querySelectorAll(
    `#mux-cuerpo-${pid} .mux-interruptor`)].find(i => (i.getAttribute('onchange') || '').startsWith(llamada));
  const pulsar = (el) => { el.checked = !el.checked; el.dispatchEvent(new Event('change')); };
  const puts = () => window.__pet.filter(x => x.m === 'PUT');
  const toasts = () => [...document.querySelectorAll('#toast-container .toast, #toast-container > div')]
    .map(t => t.textContent.trim());
  const limpiarToasts = () => { const c = document.getElementById('toast-container'); if (c) c.innerHTML = ''; };
  const tipos = (d) => Object.fromEntries(d.fuentes.flatMap((f, i) => f.pistas.map(p => [`${i}:${p.id}`, p.tipo])));
  // Un servidor con memoria: lo que un PUT o un POST cambia es lo que el
  // siguiente GET devuelve. Contestar siempre el proyecto de partida hacía
  // que la relectura que dispara la columna de trabajo deshiciera el cambio
  // —un servidor de verdad no lo haría—, y el caso medía el arnés.
  const servidor = (d, extra = {}) => {
    let actual = copia(d);
    return (m, url, body) => {
      if (m === 'PUT') { const b = copia(body); delete b.esquema; Object.assign(actual, b); }
      if (m === 'POST' && url.endsWith('/ejecutar')) actual.status = 'queued';
      if (extra[m]) return extra[m](url, body, actual);
      return copia(actual);
    };
  };
  const tarde = (ms, v) => new Promise(r => setTimeout(
    () => r(typeof v === 'function' ? v() : v), ms));

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
    ms_por_hora: _muxMsPorHora(-40), lista: _muxEnumerar(['Subtítulos', 'Capítulos', 'Otros']),
  }));

  // ── Editar el nombre de una pista manda la lista entera ──────────────
  await caso('nombre_de_pista', async () => {
    const d = copia(D.listo); d.id = 'mx1';
    manejador = servidor(d);
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
    manejador = servidor(d);
    await abrirMux(d.id, d);
    fila('mx2', '1:2').querySelector('.flag-pill').click();
    await espera(30);
    return {antes: d.pistas, puts: puts().map(p => p.body)};
  });

  await caso('quitar_pista', async () => {
    const d = copia(D.listo); d.id = 'mx3';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    fila('mx3', '1:2').querySelector('.track-actions button').click();
    await espera(30);
    return {puts: puts().map(p => p.body), queda: !!fila('mx3', '1:2')};
  });

  // ── Los interruptores de las columnas ────────────────────────────────
  await caso('alternar_audio', async () => {
    const d = copia(D.listo); d.id = 'mx4';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    const sw = interruptor('mx4', `muxAlternar("mx4", 0, 1,`);
    if (!sw) return {fallo: 'sin interruptor para 0:1'};
    const antes = sw.checked;
    pulsar(sw);
    await espera(30);
    const despues = interruptor('mx4', `muxAlternar("mx4", 0, 1,`).checked;
    return {antes, despues, puts: puts().map(p => p.body), tipos: tipos(d),
            origen: d.fuentes[0].pistas.find(p => p.id === 1).nombre};
  });

  await caso('alternar_video', async () => {
    const d = copia(D.listo); d.id = 'mx5';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    pulsar(interruptor('mx5', `muxAlternarVideo("mx5", 1,`));
    await espera(30);
    const elegidoA = interruptor('mx5', `muxAlternarVideo("mx5", 0,`).checked;
    const elegidoB = interruptor('mx5', `muxAlternarVideo("mx5", 1,`).checked;
    // Y quitarlo desde «En el MKV final» lo quita entero.
    fila('mx5', '1:0').querySelector('.track-actions button').click();
    await espera(30);
    return {puts: puts().map(p => p.body), elegidoA, elegidoB, tipos: tipos(d)};
  });

  await caso('quitar_video_entero', async () => {
    // Una BL y su EL en pistas aparte: quitar una desde «En el MKV final»
    // quita las dos (el vídeo va entero).
    const d = copia(D.listo); d.id = 'mx5b';
    d.fuentes[0].pistas.splice(1, 0, Object.assign(copia(d.fuentes[0].pistas[0]), {id: 99}));
    d.pistas.splice(1, 0, {fuente: 0, id: 99, nombre: 'EL', por_defecto: false, forzada: false});
    manejador = servidor(d);
    await abrirMux(d.id, d);
    fila('mx5b', '0:99').querySelector('.track-actions button').click();
    await espera(30);
    return {puts: puts().map(p => p.body), tipos: tipos(d)};
  });

  await caso('alternar_capitulo', async () => {
    const d = copia(D.listo); d.id = 'mx6';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    pulsar(interruptor('mx6', `muxAlternarCapitulo("mx6", 0, 0, 2,`));
    await espera(30);
    pulsar(interruptor('mx6', `muxAlternarCapitulo("mx6", 1, 0, 0,`));
    await espera(30);
    return {antes: d.capitulos, puts: puts().map(p => p.body)};
  });

  await caso('alternar_adjunto', async () => {
    const d = copia(D.listo); d.id = 'mx6b';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    const sw = () => interruptor('mx6b', `muxAlternarAdjunto("mx6b", 0, 1,`);
    if (!sw()) return {fallo: 'sin adjuntos que marcar'};
    const antes = sw().checked;
    pulsar(sw()); await espera(30);
    pulsar(sw()); await espera(30);
    return {antes, puts: puts().map(p => p.body)};
  });

  // ── Los patrones ─────────────────────────────────────────────────────
  await caso('patron', async () => {
    const d = copia(D.listo); d.id = 'mx7';
    manejador = servidor(d, {POST: (url, body, actual) =>
      (url.endsWith('/patron') ? Object.assign(copia(actual), {nombre_salida: 'After pattern.mkv'}) : copia(actual))});
    await abrirMux(d.id, d);
    const boton = [...document.querySelectorAll('#mux-sec-audio-mx7 .mux-columna-b .mux-columna-acciones button')]
      .find(b => (b.getAttribute('onclick') || '').includes("'todo_b'"));
    if (!boton) return {fallo: 'sin «Todo» en la columna B del audio'};
    boton.click();
    await espera(40);
    const posts = window.__pet.filter(p => p.m === 'POST').map(p => ({url: p.url, body: p.body}));
    const mis = [...document.querySelectorAll('#mux-sec-audio-mx7 .mux-patrones button')]
      .map(b => b.getAttribute('onclick'));
    return {posts, nombre: document.getElementById('mux-mx7-nombre').value, mis};
  });

  await caso('patron_espera', async () => {
    const d = copia(D.listo); d.id = 'mx8';
    const ev = [];
    const srv = servidor(d);
    manejador = (m, url, body) => {
      ev.push(m + (url.endsWith('/patron') ? ' patron' : ''));
      const r = srv(m, url, body);
      if (m === 'PUT') return tarde(120, () => { ev.push('fin PUT'); return r; });
      return r;
    };
    await abrirMux(d.id, d);
    ev.length = 0;
    fila('mx8', '1:2').querySelector('.track-actions button').click();   // PUT en vuelo
    await muxPatron('mx8', 'subtitulos', 'mis_idiomas');
    await espera(30);
    return {ev};
  });

  await caso('patron_bloquea', async () => {
    const d = copia(D.listo); d.id = 'mx9';
    let soltar = null;
    manejador = (m, url) => {
      if (m === 'POST' && url.endsWith('/patron')) return new Promise(r => { soltar = () => r(copia(d)); });
      return copia(d);
    };
    await abrirMux(d.id, d);
    const enCurso = muxPatron('mx9', 'audio', 'todo_a');
    await espera(30);
    const durante = [...document.querySelectorAll('#mux-cuerpo-mx9 .mux-interruptor')].map(i => i.disabled);
    // Un cambio que llegara ahora (un clic sobre un repintado viejo) no se
    // aplica: pisaría el resultado del patrón al guardarse detrás.
    const sw = document.createElement('input'); sw.type = 'checkbox'; sw.checked = true;
    muxAlternar('mx9', 0, 1, sw);
    const putsDurante = puts().length;
    soltar(); await enCurso; await espera(20);
    const despues = [...document.querySelectorAll('#mux-cuerpo-mx9 .mux-interruptor')].map(i => i.disabled);
    return {durante, despues, putsDurante, revertido: sw.checked === false};
  });

  // ── Multiplexar ──────────────────────────────────────────────────────
  await caso('multiplexar_con_vacias', async () => {
    const d = copia(D.listo); d.id = 'mxM';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    const btn = document.getElementById('mux-multiplexar-resumen-mxM');
    const habilitado = btn && !btn.disabled;
    // Lo que el resumen dice ANTES de pulsar (al encolar deja de decirlo).
    const nota = (document.querySelector('#mux-cuerpo-mxM .mux-vacias') || {}).textContent || '';
    btn.click();
    await espera(30);
    const abierto = document.getElementById('confirm-modal').classList.contains('open');
    const mensaje = document.getElementById('confirm-message').textContent;
    const boton_ok = document.getElementById('confirm-ok-btn').textContent;
    const antes = window.__pet.filter(p => p.m === 'POST').length;
    document.getElementById('confirm-ok-btn').click();
    await espera(40);
    const despues = document.querySelector('#mux-cuerpo-mxM .mux-resumen .project-action-bar button');
    const r = {habilitado, abierto, mensaje, boton_ok, antes,
               posts: window.__pet.filter(p => p.m === 'POST').map(p => p.url), nota,
               despues_deshabilitado: !!(despues && despues.disabled)};
    cerrarMux('mxM');                     // en cola se seguiría leyendo cada 2 s
    return r;
  });

  // Con el diálogo abierto se puede seguir tocando: confirmar tiene que
  // esperar a ese guardado, no encolar si lo rechazaron y no pedir lo que el
  // plan nuevo ya no deja.
  const conDialogo = async (id, alGuardar) => {
    const d = copia(D.listo); d.id = id;
    const ev = [];
    const srv = servidor(d);
    manejador = (m, url, body) => {
      ev.push(m + (url.endsWith('/ejecutar') ? ' ejecutar' : ''));
      if (m === 'PUT') return tarde(120, () => { ev.push('fin PUT'); return alGuardar(srv(m, url, body)); });
      return srv(m, url, body);
    };
    await abrirMux(d.id, d);
    await muxMultiplexar(id);                           // abre el diálogo
    const abierto = document.getElementById('confirm-modal').classList.contains('open');
    ev.length = 0;
    const t = document.getElementById(`mux-${id}-titulo`);
    t.value = 'Title while asking';
    muxTituloContenedor(id, t);                         // PUT en vuelo
    document.getElementById('confirm-ok-btn').click();
    await espera(220);
    cerrarMux(id);
    return {abierto, ev};
  };
  await caso('confirmar_espera', () => conDialogo('mxCF1', (r) => r));
  await caso('confirmar_tras_rechazo', () => conDialogo('mxCF2', () => null));
  await caso('confirmar_con_plan_nuevo', () => conDialogo('mxCF3', (r) =>
    Object.assign(r, {plan: Object.assign(copia(r.plan), {puede_multiplexar: false})})));

  // Las secciones vacías se miran sobre lo GUARDADO: elegir el único adjunto
  // y pulsar enseguida no puede preguntar por «Otros».
  await caso('multiplexar_avisos_frescos', async () => {
    const d = copia(D.listo); d.id = 'mxAF';
    const srv = servidor(d, {PUT: (url, body, actual) =>
      tarde(120, () => Object.assign(copia(actual), {plan: Object.assign(copia(actual.plan), {avisos: []})}))});
    manejador = srv;
    await abrirMux(d.id, d);
    muxAlternarAdjunto('mxAF', 0, 1);                   // PUT en vuelo
    await muxMultiplexar('mxAF');
    await espera(30);
    const r = {abierto: document.getElementById('confirm-modal').classList.contains('open'),
               posts: window.__pet.filter(p => p.m === 'POST').map(p => p.url)};
    cerrarMux('mxAF');
    return r;
  });

  await caso('multiplexar_sin_vacias', async () => {
    const d = copia(D.listo); d.id = 'mxN';
    d.plan.avisos = [];
    manejador = servidor(d);
    await abrirMux(d.id, d);
    document.getElementById('mux-multiplexar-pie-mxN').click();
    await espera(40);
    const r = {abierto: document.getElementById('confirm-modal').classList.contains('open'),
               posts: window.__pet.filter(p => p.m === 'POST').map(p => p.url)};
    cerrarMux('mxN');
    return r;
  });

  await caso('aceptar_el_riesgo', async () => {
    const d = copia(D.decision); d.id = 'mx10';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    const btn = document.querySelector('#mux-cuerpo-mx10 .mux-motivos-pie button');
    if (!btn) return {fallo: 'sin botón de aceptar el riesgo'};
    btn.click();
    const abierto = document.getElementById('confirm-modal').classList.contains('open');
    const antes = puts().length;
    document.getElementById('confirm-ok-btn').click();
    await espera(30);
    return {abierto, antes, puts: puts().map(p => p.body)};
  });

  // ── Una respuesta vieja no pisa la nueva ─────────────────────────────
  await caso('respuesta_vieja', async () => {
    const d = copia(D.listo); d.id = 'mx11';
    await abrirMux(d.id, d);
    const p = _muxProyecto('mx11');
    manejador = (m, url, body) => {
      if (m === 'GET') return tarde(200, () => Object.assign(copia(d), {nombre_salida: 'Old name.mkv'}));
      return Object.assign(copia(d), {nombre_salida: body.nombre_salida, nombre_manual: true});
    };
    const lectura = _muxLeer(p);              // sale ANTES y vuelve DESPUÉS
    await espera(10);
    const el = document.getElementById('mux-mx11-nombre');
    el.value = 'New name.mkv';
    await muxNombreSalida('mx11', el);
    await lectura;
    await espera(30);
    return {datos: p.datos.nombre_salida,
            pantalla: document.getElementById('mux-mx11-nombre').value,
            metodos: window.__pet.map(x => x.m)};
  });

  // ── Un cambio rechazado se deshace ───────────────────────────────────
  await caso('cambio_rechazado', async () => {
    const d = copia(D.listo); d.id = 'mx12';
    manejador = (m) => (m === 'PUT' ? null : copia(d));
    await abrirMux(d.id, d);
    fila('mx12', '1:2').querySelector('.track-actions button').click();
    await espera(60);
    return {metodos: window.__pet.map(x => x.m), vuelve: !!fila('mx12', '1:2'),
            pistas: _muxProyecto('mx12').datos.pistas.length, original: d.pistas.length};
  });

  // ── Lo que se dice al cancelar sale de la respuesta ──────────────────
  await caso('cancelar', async () => {
    const r = {};
    limpiarToasts();
    manejador = (m, url) => (url.endsWith('/cancelar') ? {ok: false} : null);
    await cancelarMultiplexadoMux('mx13');
    r.nada = toasts();
    limpiarToasts();
    manejador = (m, url) => (url.endsWith('/cancelar') ? {ok: true, descartada: true} : null);
    await cancelarMultiplexadoMux('mx13');
    r.descartada = toasts();
    limpiarToasts();
    manejador = (m, url) => (url.endsWith('/cancelar') ? {ok: true} : null);
    await cancelarMultiplexadoMux('mx13');
    r.cancelada = toasts();
    // La columna de trabajo: cada trabajo de esta pestaña por su endpoint.
    window.__pet = [];
    manejador = (m, url) => (/\/cancelar$/.test(url) ? {ok: true} : null);
    cancelarTrabajoActivo({tab: 'mux', detalle: 'sincronia_mux', id: 'mux_sincronia:mx13', que: 'x'});
    document.getElementById('confirm-ok-btn').click();
    await espera(30);
    cancelarTrabajoActivo({tab: 'mux', detalle: 'combinar', id: 'mx13', que: 'x'});
    document.getElementById('confirm-ok-btn').click();
    await espera(30);
    r.posts = window.__pet.filter(p => p.m === 'POST').map(p => p.url);
    return r;
  });

  // ── El modal de creación ─────────────────────────────────────────────
  await caso('crear_mismo_fichero', async () => {
    abrirNuevoMultiplexado();
    _muxNueva.fuentes = [{ruta: '/mnt/library/a.mkv', nombre: 'a.mkv'},
                         {ruta: '/mnt/library/a.mkv', nombre: 'a.mkv'}];
    _muxPintarHuecos();
    const err = document.getElementById('mux-nueva-error');
    const r = {deshabilitado: document.getElementById('mux-nueva-crear').disabled,
               error_visible: err.style.display !== 'none',
               error: document.getElementById('mux-nueva-error-texto').textContent,
               roles: [...document.querySelectorAll('#mux-nueva-huecos .mux-fuente-rol')].map(x => x.textContent)};
    intercambiarFuentesMux();
    _muxNueva.fuentes[1] = {ruta: '/mnt/isos/b.mkv', nombre: 'b.mkv'};
    _muxPintarHuecos();
    r.distintos_habilitado = !document.getElementById('mux-nueva-crear').disabled;
    r.distintos_sin_error = document.getElementById('mux-nueva-error').style.display === 'none';
    closeModal('mux-nueva-modal');
    return r;
  });

  await caso('crear_bien', async () => {
    const d = copia(D.vacio); d.id = 'mx14';
    abrirNuevoMultiplexado();
    _muxNueva.fuentes = [{ruta: '/mnt/library/a.mkv', nombre: 'a.mkv'},
                         {ruta: '/mnt/isos/b.mkv', nombre: 'b.mkv'}];
    _muxPintarHuecos();
    manejador = (m, url) => (m === 'POST' && url.startsWith('/api/mux/create')) ? copia(d)
                          : url.startsWith('/api/mux/create/progress') ? {activo: true}
                          : copia(d);
    await crearMultiplexado();
    const post = window.__pet.find(p => p.m === 'POST');
    return {url: post && post.url, body: post && post.body, timeout: post && post.timeout,
            largo: API_FETCH_TIMEOUT_LARGO,
            cerrado: !document.getElementById('mux-nueva-modal').classList.contains('open'),
            panel: !!document.getElementById('mux-panel-mx14'),
            elegidos: [...document.querySelectorAll('#mux-cuerpo-mx14 .mux-interruptor')].filter(i => i.checked).length,
            boton_deshabilitado: document.getElementById('mux-multiplexar-resumen-mx14').disabled};
  });

  await caso('crear_mal', async () => {
    abrirNuevoMultiplexado();
    _muxNueva.fuentes = [{ruta: '/mnt/library/a.mkv', nombre: 'a.mkv'},
                         {ruta: '/mnt/isos/b.mkv', nombre: 'b.mkv'}];
    _muxPintarHuecos();
    let job = '';
    manejador = (m, url) => {
      if (m === 'POST') { job = new URL(url, 'http://x').searchParams.get('job'); return null; }
      if (url.startsWith('/api/mux/create/progress')) return {activo: false, job, error: 'mkvmerge -J failed on b.mkv'};
      return null;
    };
    await crearMultiplexado();
    const r = {abierto: document.getElementById('mux-nueva-modal').classList.contains('open'),
               error: document.getElementById('mux-nueva-error-texto').textContent,
               huecos: document.getElementById('mux-nueva-huecos').style.display !== 'none'};
    // Y el error de OTRA creación (otro sello) no se toma por el suyo.
    manejador = (m, url) => (url.startsWith('/api/mux/create/progress')
      ? {activo: false, job: 'otro', error: 'not mine'} : null);
    await crearMultiplexado();
    r.ajeno = document.getElementById('mux-nueva-error-texto').textContent;
    closeModal('mux-nueva-modal');
    return r;
  });

  // ── Lo que dicen el veredicto y el plan ──────────────────────────────
  await caso('veredicto_y_plan', async () => {
    const texto = (f, d) => { const c = document.createElement('div'); c.innerHTML = f(d); return c.textContent; };
    return {retraso: texto(_muxVeredictoHTML, D.marcha), velocidades: texto(_muxVeredictoHTML, D.listo),
            velocidades_reves: texto(_muxVeredictoHTML, D.reves),
            velocidades_vacio: texto(_muxVeredictoHTML, D.vacio),
            plan_retraso: texto(_muxPlanHTML, D.marcha), plan_listo: texto(_muxPlanHTML, D.listo),
            plan_reves: texto(_muxPlanHTML, D.reves), plan_montaje: texto(_muxPlanHTML, D.cola),
            plan_vacio: texto(_muxPlanHTML, D.vacio)};
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
    return {velocidades: medir(D.listo), montaje: medir(D.cola), dudosa: medir(D.decision)};
  });

  // ── Lo que se ve en cada sección ─────────────────────────────────────
  await caso('columna_plegada', async () => {
    const d = copia(D.listo); d.id = 'mx15';
    // El castellano de A, elegido: no es de «mis idiomas» (la app va en
    // inglés en los datos) y tiene que verse igual con la columna plegada.
    d.pistas.splice(1, 0, {fuente: 0, id: 3, nombre: 'GRP DD 5.1', por_defecto: false, forzada: false});
    await abrirMux(d.id, d);
    const col = () => document.querySelector('#mux-sec-audio-mx15 .mux-columna-a');
    const filas = () => col().querySelectorAll('.mux-fila').length;
    const plegada = filas();
    const boton = col().querySelector('.mux-ver-todas');
    const rotulo = boton ? boton.textContent : '';
    if (boton) boton.click();
    await espera(20);                     // el rótulo lo pinta el observador de i18n
    const abierta = filas();
    const otra = col().querySelector('.mux-ver-todas');
    return {plegada, abierta, rotulo, rotulo_abierta: otra ? otra.textContent : '',
            total: d.fuentes[0].pistas.filter(p => p.tipo === 'audio').length,
            capitulos_cajas: document.querySelectorAll('#mux-sec-capitulos-mx15 .mux-filas-caja').length,
            final_capitulos: document.querySelectorAll('#mux-sec-capitulos-mx15 .mux-cap-final').length,
            fuera: document.querySelectorAll('#mux-sec-capitulos-mx15 .mux-cap-final.fuera').length};
  });

  await caso('no_hace_falta', async () => {
    const d = copia(D.listo); d.id = 'mx16';
    d.plan.necesita_sincronia = false;
    await abrirMux(d.id, d);
    const conBanner = (document.getElementById('mux-sincronia-mx16') || {}).textContent || '';
    const d2 = copia(D.listo); d2.id = 'mx17';
    await abrirMux(d2.id, d2);
    const sinBanner = (document.getElementById('mux-sincronia-mx17') || {}).textContent || '';
    return {conBanner, sinBanner};
  });

  await caso('vacio', async () => {
    const d = copia(D.vacio); d.id = 'mx18';
    await abrirMux(d.id, d);
    const c = document.getElementById('mux-cuerpo-mx18');
    return {aporta: [...c.querySelectorAll('.mux-aporta')].map(x => x.textContent),
            motivos: [...c.querySelectorAll('.mux-motivos li')].map(x => x.textContent),
            boton: document.getElementById('mux-multiplexar-resumen-mx18').disabled,
            plan: !!c.querySelector('.mux-plan'),
            finales: [...c.querySelectorAll('.mux-final .track-empty')].map(x => x.textContent)};
  });

  // ── Reordenar con Sortable ───────────────────────────────────────────
  await caso('reordenar', async () => {
    const d = copia(D.listo); d.id = 'mx19';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    const lista = document.querySelector('#mux-cuerpo-mx19 .mux-orden[data-tipo="audio"]');
    lista.insertBefore(lista.lastElementChild, lista.firstElementChild);
    const p = _muxProyecto('mx19');
    const s = p._sortables.find(x => x.el === lista);
    s.options.onStart();
    s.options.onEnd({});
    await espera(30);
    return {antes: d.pistas, puts: puts().map(x => x.body), tipos: tipos(d),
            listas: p._sortables.map(x => x.el.dataset.tipo)};
  });

  // ── Lo que se está escribiendo sobrevive a un repintado ──────────────
  await caso('anclaje', async () => {
    const d = copia(D.listo); d.id = 'mx20';
    await abrirMux(d.id, d);
    const el = document.getElementById('mux-mx20-pista-1-2');
    el.value = 'half typed'; marcarTocado(el); el.focus();
    _muxAplicar(_muxProyecto('mx20'), Object.assign(copia(d), {nombre_salida: 'Server renamed.mkv'}));
    return {escrito: document.getElementById('mux-mx20-pista-1-2').value,
            foco: document.activeElement && document.activeElement.id,
            del_servidor: document.getElementById('mux-mx20-nombre').value};
  });

  await caso('arrastre', async () => {
    const d = copia(D.listo); d.id = 'mx21';
    await abrirMux(d.id, d);
    const p = _muxProyecto('mx21');
    p._sortables[0].options.onStart();
    _muxAplicar(p, Object.assign(copia(d), {nombre_salida: 'During drag.mkv'}));
    const durante = document.getElementById('mux-mx21-nombre').value;
    p._sortables[0].options.onEnd({});
    const despues = document.getElementById('mux-mx21-nombre').value;
    return {durante, despues};
  });

  // ── Las dos vistas del modal de trabajo ──────────────────────────────
  await caso('detalle_multiplexado', async () => {
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

  // ═══ El orden de las peticiones (revisión de Codex del 2026-10-25) ═══

  // «Multiplexar» espera a que lo último que se tocó esté guardado.
  await caso('multiplexar_espera', async () => {
    const d = copia(D.listo); d.id = 'mxA1';
    d.plan.avisos = [];
    const ev = [];
    const srv = servidor(d);
    manejador = (m, url, body) => {
      ev.push(m + (url.endsWith('/ejecutar') ? ' ejecutar' : ''));
      const r = srv(m, url, body);
      if (m === 'PUT') return tarde(120, () => { ev.push('fin PUT'); return r; });
      return r;
    };
    await abrirMux(d.id, d);
    ev.length = 0;
    fila('mxA1', '1:2').querySelector('.track-actions button').click();   // PUT en vuelo
    const t = document.getElementById('mux-mxA1-titulo');
    t.value = 'Title A';
    muxTituloContenedor('mxA1', t);                                         // espera detrás
    await muxMultiplexar('mxA1');
    await espera(30);
    const p = _muxProyecto('mxA1');
    if (p && p._timer) { clearTimeout(p._timer); p._timer = null; }
    return {ev};
  });

  // Lo guardado puede cambiar el plan: entonces no se pide lo que el servidor rechazaría.
  await caso('multiplexar_con_plan_nuevo', async () => {
    // Con secciones vacías: si no esperara al plan nuevo, preguntaría por
    // ellas antes de saber que no se puede multiplexar.
    const d = copia(D.listo); d.id = 'mxA3';
    manejador = servidor(d, {PUT: (url, body, actual) => {
      actual.plan = Object.assign(copia(actual.plan), {puede_multiplexar: false});
      return tarde(60, () => copia(actual));
    }});
    await abrirMux(d.id, d);
    fila('mxA3', '1:2').querySelector('.track-actions button').click();
    await muxMultiplexar('mxA3');
    await espera(30);
    const boton = document.getElementById('mux-multiplexar-resumen-mxA3');
    return {pedidas: window.__pet.map(x => `${x.m} ${x.url}`),
            abierto: document.getElementById('confirm-modal').classList.contains('open'),
            boton_deshabilitado: !!(boton && boton.disabled)};
  });

  // Borrar espera a lo que se estaba guardando.
  await caso('borrar_espera_guardado', async () => {
    const d = copia(D.listo); d.id = 'mxA4';
    const ev = [];
    manejador = (m, url, body) => {
      ev.push(m);
      if (m === 'PUT') return tarde(120, () => { ev.push('fin PUT'); return Object.assign(copia(d), body); });
      if (m === 'DELETE') return {ok: true};
      if (url === '/api/mux') return {proyectos: []};
      return copia(d);
    };
    await abrirMux(d.id, d);
    _muxLista = [copia(d)];
    ev.length = 0;
    const t = document.getElementById('mux-mxA4-titulo');
    t.value = 'Title Z';
    muxTituloContenedor('mxA4', t);
    borrarMux('mxA4');
    document.getElementById('confirm-ok-btn').click();
    await espera(300);
    return {ev: ev.filter(x => x !== 'GET'), abierto: !!_muxProyecto('mxA4')};
  });

  await caso('multiplexar_tras_rechazo', async () => {
    const d = copia(D.listo); d.id = 'mxA2';
    d.plan.avisos = [];
    manejador = (m) => (m === 'PUT' ? tarde(80, null) : copia(d));
    await abrirMux(d.id, d);
    fila('mxA2', '1:2').querySelector('.track-actions button').click();
    await muxMultiplexar('mxA2');
    await espera(30);
    return {pedidas: window.__pet.map(x => `${x.m} ${x.url}`)};
  });

  await caso('dos_lecturas', async () => {
    const d = copia(D.listo); d.id = 'mxB0';
    await abrirMux(d.id, d);
    const p = _muxProyecto('mxB0');
    let n = 0;
    manejador = (m) => {
      if (m !== 'GET') return null;
      n += 1;
      return n === 1 ? tarde(200, () => Object.assign(copia(d), {nombre_salida: 'Older.mkv'}))
                     : tarde(20, () => Object.assign(copia(d), {nombre_salida: 'Newer.mkv'}));
    };
    const primera = _muxLeer(p);
    await espera(10);
    await _muxLeer(p);
    await primera; await espera(20);
    return {datos: p.datos.nombre_salida};
  });

  await caso('get_cruzado', async () => {
    const d = copia(D.listo); d.id = 'mxB1';
    await abrirMux(d.id, d);
    const p = _muxProyecto('mxB1');
    manejador = (m, url, body) => {
      if (m === 'PUT') return tarde(100, () => Object.assign(copia(d),
        {nombre_salida: body.nombre_salida, nombre_manual: true}));
      if (m === 'GET') return tarde(250, () => Object.assign(copia(d), {nombre_salida: 'Old name.mkv'}));
      return null;
    };
    const el = document.getElementById('mux-mxB1-nombre');
    el.value = 'New name.mkv';
    const guardado = muxNombreSalida('mxB1', el);
    await espera(20);
    const lectura = _muxLeer(p);
    await guardado; await lectura; await espera(20);
    return {datos: p.datos.nombre_salida, pantalla: document.getElementById('mux-mxB1-nombre').value};
  });

  await caso('get_durante_put', async () => {
    const d = copia(D.listo); d.id = 'mxB2';
    await abrirMux(d.id, d);
    const p = _muxProyecto('mxB2');
    manejador = (m, url, body) => {
      if (m === 'PUT') return tarde(300, () => Object.assign(copia(d),
        {nombre_salida: body.nombre_salida, nombre_manual: true}));
      if (m === 'GET') return tarde(60, () => Object.assign(copia(d), {nombre_salida: 'Old name.mkv'}));
      return null;
    };
    const el = document.getElementById('mux-mxB2-nombre');
    el.value = 'New name.mkv';
    const guardado = muxNombreSalida('mxB2', el);
    await espera(10);
    _muxLeer(p);
    await espera(150);                           // el GET ya volvió; el PUT, no
    const durante = document.getElementById('mux-mxB2-nombre').value;
    await guardado; await espera(20);
    return {durante, despues: document.getElementById('mux-mxB2-nombre').value};
  });

  // Un GET que se cruza con un PATRÓN tampoco lo deshace.
  await caso('get_cruzado_con_patron', async () => {
    const d = copia(D.listo); d.id = 'mxB3';
    await abrirMux(d.id, d);
    const p = _muxProyecto('mxB3');
    manejador = (m, url) => {
      if (m === 'POST' && url.endsWith('/patron'))
        return tarde(100, () => Object.assign(copia(d), {nombre_salida: 'After pattern.mkv'}));
      if (m === 'GET') return tarde(250, () => Object.assign(copia(d), {nombre_salida: 'Old name.mkv'}));
      return null;
    };
    const patron = muxPatron('mxB3', 'audio', 'todo_b');
    await espera(20);
    const lectura = _muxLeer(p);
    await patron; await lectura; await espera(20);
    return {datos: p.datos.nombre_salida};
  });

  await caso('get_durante_patron', async () => {
    const d = copia(D.listo); d.id = 'mxB4';
    await abrirMux(d.id, d);
    const p = _muxProyecto('mxB4');
    manejador = (m, url) => {
      if (m === 'POST' && url.endsWith('/patron'))
        return tarde(300, () => Object.assign(copia(d), {nombre_salida: 'After pattern.mkv'}));
      if (m === 'GET') return tarde(60, () => Object.assign(copia(d), {nombre_salida: 'Old name.mkv'}));
      return null;
    };
    const patron = muxPatron('mxB4', 'audio', 'todo_b');
    await espera(10);
    _muxLeer(p);
    await espera(150);                           // el GET ya volvió; el patrón, no
    const durante = document.getElementById('mux-mxB4-nombre').value;
    await patron; await espera(20);
    return {durante, despues: document.getElementById('mux-mxB4-nombre').value};
  });

  await caso('post_y_get_viejo', async () => {
    const d = copia(D.listo); d.id = 'mxC1';
    d.plan.avisos = [];
    await abrirMux(d.id, d);
    const p = _muxProyecto('mxC1');
    manejador = (m, url) => {
      if (m === 'GET') return tarde(200, () => copia(d));                // lee `pending`
      if (m === 'POST' && url.endsWith('/ejecutar')) return Object.assign(copia(d), {status: 'queued'});
      return null;
    };
    const lectura = _muxLeer(p);
    await espera(20);
    await muxMultiplexar('mxC1');
    await lectura; await espera(20);
    const sigue = !!p._timer;
    if (p._timer) { clearTimeout(p._timer); p._timer = null; }
    const boton = document.querySelector('#mux-cuerpo-mxC1 .mux-resumen .project-action-bar button');
    return {status: p.datos.status, sigue_leyendo: sigue, boton_deshabilitado: !!(boton && boton.disabled)};
  });

  await caso('ficha_y_get_viejo', async () => {
    const d = copia(D.listo); d.id = 'mxQ1';
    await abrirMux(d.id, d);
    const p = _muxProyecto('mxQ1');
    manejador = (m, url) => (url === '/api/mux' ? {proyectos: []}
                             : m === 'GET' ? tarde(150, () => copia(d)) : null);
    const lectura = _muxLeer(p);
    await espera(20);
    const poster = 'https://image.tmdb.org/t/p/w342/poster.jpg';
    muxTrasCambiarFicha('mxQ1', Object.assign(copia(d),
      {tmdb_info: Object.assign(copia(d.tmdb_info), {poster_url: poster})}));
    await lectura; await espera(20);
    return {poster: (p.datos.tmdb_info || {}).poster_url, esperado: poster};
  });

  // Cerrar la pestaña no tira lo que esperaba detrás de un guardado.
  await caso('cerrar_con_pendiente', async () => {
    const d = copia(D.listo); d.id = 'mxD1';
    manejador = (m, url, body) => (m === 'PUT' ? tarde(100, () => Object.assign(copia(d), body)) : copia(d));
    await abrirMux(d.id, d);
    const el = document.getElementById('mux-mxD1-titulo');
    el.value = 'Title D';
    muxTituloContenedor('mxD1', el);              // PUT en vuelo
    muxAlternarAdjunto('mxD1', 0, 1);             // espera detrás
    cerrarMux('mxD1');
    await espera(300);
    return {puts: puts().map(p => p.body)};
  });

  await caso('reabrir_tras_cerrar', async () => {
    const d = copia(D.listo); d.id = 'mxD2';
    const ev = [];
    manejador = (m, url, body) => {
      ev.push(m);
      if (m === 'PUT') return tarde(120, () => { ev.push('fin PUT'); return Object.assign(copia(d), body); });
      return Object.assign(copia(d), {titulo: 'Title E'});
    };
    await abrirMux(d.id, d);
    ev.length = 0;
    const el = document.getElementById('mux-mxD2-titulo');
    el.value = 'Title E';
    muxTituloContenedor('mxD2', el);
    cerrarMux('mxD2');
    await abrirMux('mxD2');
    await espera(20);
    const r = {ev, titulo: document.getElementById('mux-mxD2-titulo').value};
    cerrarMux('mxD2');
    return r;
  });

  await caso('clic_tras_editar', async () => {
    const d = copia(D.listo); d.id = 'mxF1';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    const input = document.getElementById('mux-mxF1-pista-1-2');
    const boton = fila('mxF1', '1:2').querySelector('.flag-pill');
    const antes = !!d.pistas.find(p => p.fuente === 1 && p.id === 2).por_defecto;
    input.value = 'Renamed while clicking'; marcarTocado(input);
    boton.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, composed: true}));
    input.dispatchEvent(new Event('change'));
    boton.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, composed: true}));
    // El `click` lo genera el navegador DESPUÉS del `pointerup`, y solo si el
    // botón sigue en la página: por eso se mira aquí y no antes.
    const conectado = boton.isConnected;
    if (conectado) boton.click();
    await espera(80);
    const ultimo = puts().slice(-1)[0];
    const pista = ultimo && ultimo.body.pistas.find(p => p.fuente === 1 && p.id === 2);
    return {conectado, antes, pista: pista || null,
            pantalla: document.getElementById('mux-mxF1-pista-1-2').value};
  });

  await caso('medir_mismas_pistas', async () => {
    const d = copia(D.listo); d.id = 'mxG1';
    d.pareja = [d.sincronia.pista_a, d.sincronia.pista_b];
    manejador = (m, url) => (url.endsWith('/medir') ? {started: true} : copia(d));
    await abrirMux(d.id, d);
    await muxCambiarPareja('mxG1');
    const misma = window.__pet.map(x => `${x.m} ${x.url}`);
    window.__pet = [];
    const sel = document.getElementById('mux-mxG1-pareja-b');
    const otra = [...sel.options].map(o => o.value).find(v => Number(v) !== d.pareja[1]);
    sel.value = otra;
    await muxCambiarPareja('mxG1');
    return {misma, otra: window.__pet.map(x => ({m: x.m, url: x.url, body: x.body})),
            pareja_otra: [d.pareja[0], Number(otra)]};
  });

  await caso('lista_vieja', async () => {
    const d = copia(D.listo); d.id = 'mxH1';
    const otro = copia(D.hecho); otro.id = 'mxH2';
    let estado = {mxH1: Object.assign(copia(d), {nombre_salida: 'Old list.mkv'}), mxH2: copia(otro)};
    _muxLista = Object.values(estado).map(copia);
    _muxListaCargada = true;
    await abrirMux(d.id, estado.mxH1);
    await abrirMux(otro.id, otro);
    manejador = (m, url, body) => {
      if (m === 'GET' && url === '/api/mux') {
        const foto = Object.values(estado).map(copia);       // lo que hay AL PEDIR
        return tarde(200, {proyectos: foto});
      }
      if (m === 'PUT') {
        estado.mxH1 = Object.assign(copia(estado.mxH1), {nombre_salida: body.nombre_salida, nombre_manual: true});
        return copia(estado.mxH1);
      }
      return null;
    };
    const lista = refrescarMuxLista();             // sale ANTES y vuelve DESPUÉS
    await espera(10);
    const el = document.getElementById('mux-mxH1-nombre');
    el.value = 'New list.mkv';
    await muxNombreSalida('mxH1', el);
    delete estado.mxH2;
    _muxProyectoBorrado(_muxProyecto('mxH2'));     // el 404 de un GET
    await lista;
    await espera(20);
    const tarjeta = document.getElementById('mux-sidebar-list').textContent;
    const r = {nombres: Object.fromEntries(_muxLista.map(x => [x.id, x.nombre_salida])),
               tarjeta_nueva: tarjeta.includes('New list'), tarjeta_vieja: tarjeta.includes('Old list')};
    cerrarMux('mxH1');
    return r;
  });

  await caso('nombre_vacio', async () => {
    const d = copia(D.listo); d.id = 'mxI1';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    const el = document.getElementById('mux-mxI1-nombre');
    el.value = '   '; marcarTocado(el);
    await muxNombreSalida('mxI1', el);
    return {puts: puts().length, valor: el.value, original: d.nombre_salida};
  });

  await caso('dos_ediciones', async () => {
    const d = copia(D.listo); d.id = 'mxJ1';
    let enVuelo = 0, max = 0;
    manejador = (m, url, body) => {
      if (m !== 'PUT') return copia(d);
      enVuelo += 1; max = Math.max(max, enVuelo);
      return tarde(80, () => { enVuelo -= 1; return Object.assign(copia(d), body); });
    };
    await abrirMux(d.id, d);
    const t = document.getElementById('mux-mxJ1-titulo');
    t.value = 'T1'; muxTituloContenedor('mxJ1', t);
    muxAlternarAdjunto('mxJ1', 0, 1);
    const t2 = document.getElementById('mux-mxJ1-titulo');
    t2.value = 'T2'; muxTituloContenedor('mxJ1', t2);
    await _muxEsperarGuardado(_muxProyecto('mxJ1'));
    await espera(20);
    return {max, puts: puts().map(p => p.body)};
  });

  await caso('lee_mientras_mide', async () => {
    const d = copia(D.midiendo); d.id = 'mxK1';
    manejador = () => Object.assign(copia(d),
      {sincronia: Object.assign(copia(d.sincronia), {progreso: [5, 7]})});
    await abrirMux(d.id, d);
    await espera(_MUX_POLL_MS + 400);
    const gets = window.__pet.filter(x => x.m === 'GET' && x.url === '/api/mux/mxK1').length;
    cerrarMux('mxK1');
    return {gets};
  });

  await caso('aceptar_riesgo_habilita', async () => {
    const d = copia(D.decision); d.id = 'mxL1';
    manejador = servidor(d, {PUT: (url, body, actual) => {
      // El plan lo recalcula el servidor: aceptar el riesgo deja multiplexar.
      if (body.acepta_riesgo) actual.plan = Object.assign(copia(actual.plan), {puede_multiplexar: true});
      return copia(actual);
    }});
    await abrirMux(d.id, d);
    const antes = document.getElementById('mux-multiplexar-resumen-mxL1').disabled;
    document.querySelector('#mux-cuerpo-mxL1 .mux-motivos-pie button').click();
    document.getElementById('confirm-ok-btn').click();
    await espera(40);
    return {antes, despues: document.getElementById('mux-multiplexar-resumen-mxL1').disabled};
  });

  await caso('en_cola_bloquea', async () => {
    const d = copia(D.cola); d.id = 'mxM1';
    await abrirMux(d.id, d);
    const c = document.getElementById('mux-cuerpo-mxM1');
    const p = _muxProyecto('mxM1');
    const r = {campos: [...c.querySelectorAll('input.track-label-input, #mux-mxM1-nombre, #mux-mxM1-titulo')]
                 .map(i => i.disabled),
               marcas: [...c.querySelectorAll('.flag-pill')].map(b => b.disabled),
               quitar: [...c.querySelectorAll('.track-actions button')].map(b => b.disabled),
               interruptores: [...c.querySelectorAll('.mux-interruptor')].map(b => b.disabled),
               patrones: [...c.querySelectorAll('.mux-columna-acciones button, .mux-patrones button')]
                 .map(b => b.disabled),
               sortables: (p._sortables || []).length};
    if (p._timer) { clearTimeout(p._timer); p._timer = null; }
    cerrarMux('mxM1');
    return r;
  });

  await caso('retraso_manual', async () => {
    const d = copia(D.decision); d.id = 'mxN1';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    const campo = () => document.getElementById('mux-mxN1-retraso');
    campo().value = '+250'; await muxRetrasoManual('mxN1', campo());
    campo().value = '-80 ms'; await muxRetrasoManual('mxN1', campo());
    const antes = puts().length;
    campo().value = 'abc'; await muxRetrasoManual('mxN1', campo());
    return {puts: puts().map(p => p.body), sin_guardar: puts().length === antes,
            restaurado: campo().value};
  });

  await caso('progreso_ajeno', async () => {
    abrirNuevoMultiplexado();
    _muxNueva.fuentes = [{ruta: '/mnt/library/a.mkv', nombre: 'a.mkv'},
                         {ruta: '/mnt/isos/b.mkv', nombre: 'b.mkv'}];
    _muxPintarHuecos();
    manejador = (m, url) => {
      if (m === 'POST') return new Promise(() => {});        // la lectura no acaba en el caso
      if (url.startsWith('/api/mux/create/progress'))
        return {activo: true, job: 'otra-pestana', fuente: 1, paso: 'mediainfo'};
      return null;
    };
    crearMultiplexado();
    await espera(900);
    const hechos = document.querySelectorAll('#mux-nueva-pasos .mux-paso-hecho').length;
    const pedidos = window.__pet.filter(p => p.url.startsWith('/api/mux/create/progress')).length;
    const pasos = document.querySelectorAll('#mux-nueva-pasos .mux-paso').length;
    _muxNueva.leyendo = false;
    if (_muxNueva.timer) { clearTimeout(_muxNueva.timer); _muxNueva.timer = null; }
    closeModal('mux-nueva-modal');
    return {hechos, pedidos, pasos};
  });

  await caso('creacion_vieja', async () => {
    abrirNuevoMultiplexado();
    _muxNueva.fuentes = [{ruta: '/mnt/library/a.mkv', nombre: 'a.mkv'},
                         {ruta: '/mnt/isos/b.mkv', nombre: 'b.mkv'}];
    _muxPintarHuecos();
    let posts = 0, job1 = '', progresos = 0;
    manejador = (m, url) => {
      if (m === 'POST') {
        posts += 1;
        if (posts === 1) {
          job1 = new URL(url, 'http://x').searchParams.get('job');
          return tarde(450, null);                          // la primera falla
        }
        return new Promise(() => {});                       // la segunda sigue leyendo
      }
      if (url.startsWith('/api/mux/create/progress')) {
        progresos += 1;
        if (progresos === 1) return tarde(900, () => ({activo: true, job: job1, fuente: 1, paso: 'mediainfo'}));
        if (progresos === 2) return tarde(600, () => ({activo: false, job: job1, error: 'stale error'}));
        return new Promise(() => {});
      }
      return null;
    };
    const primera = crearMultiplexado();
    await espera(500);                                      // la primera ya falló
    crearMultiplexado();                                    // empieza la segunda
    await espera(800);                                      // vuelven las respuestas de la primera
    const r = {posts,
               leyendo: _muxNueva.leyendo,
               pasos_visibles: document.getElementById('mux-nueva-pasos').style.display !== 'none',
               huecos_visibles: document.getElementById('mux-nueva-huecos').style.display !== 'none',
               hechos: document.querySelectorAll('#mux-nueva-pasos .mux-paso-hecho').length,
               error: document.getElementById('mux-nueva-error-texto').textContent};
    await primera;
    _muxNueva.leyendo = false;
    if (_muxNueva.timer) { clearTimeout(_muxNueva.timer); _muxNueva.timer = null; }
    closeModal('mux-nueva-modal');
    return r;
  });

  // La comprobación final dice contra qué pista se midió cada audio —y de
  // CUÁL de los dos MKV—, y por qué uno no se pudo medir.
  await caso('comprobacion_sincronia', async () => {
    const leer = (d) => {
      const c = document.createElement('div');
      c.innerHTML = _muxComprobacionHTML(d);
      return [...c.querySelectorAll('.mux-check')].map(li => ({
        clase: li.className, titulo: li.querySelector('.mux-check-titulo').textContent,
        texto: li.querySelector('.mux-check-texto').textContent,
        detalle: [...li.querySelectorAll('.mux-check-detalle li')].map(x => x.textContent)}));
    };
    const d = copia(D.hecho);
    // Al revés: la referencia es una pista de B, y una validación de antes del
    // rediseño (sin `contra_fuente`) se lee contra A.
    const reves = copia(D.hecho);
    reves.validacion.checks = reves.validacion.checks.map(c => (c.id === 'sincronia'
      ? Object.assign(c, {contra: 4, contra_fuente: 1}) : c));
    const vieja = copia(D.hecho);
    vieja.validacion.checks = vieja.validacion.checks.map(c => { const x = copia(c); delete x.contra_fuente; return x; });
    return {filas: leer(d), reves: leer(reves), vieja: leer(vieja),
            contra_a: _muxDescripcionCorta(_muxOrigen(d, 0, 1)),
            contra_b: _muxDescripcionCorta(_muxOrigen(d, 1, 4))};
  });

  await caso('comillas', async () => {
    const d = copia(D.listo); d.id = 'mxP1';
    const pista = d.pistas.find(p => p.fuente === 1 && p.id === 2);
    pista.nombre = 'Doblaje "DCP" <7.1> & más';
    await abrirMux(d.id, d);
    return {valor: document.getElementById('mux-mxP1-pista-1-2').value, esperado: pista.nombre};
  });

  // ── La revisión de Codex del 2026-10-27 ──────────────────────────────
  // Mientras un patrón vuela no se toca NADA de lo que guarda: ni la
  // selección ni el nombre, el título, las pistas comparadas o el botón. Un
  // título guardado mientras tanto volvía después del patrón y su respuesta
  // deshacía en pantalla lo que el patrón había dejado en el servidor.
  await caso('patron_congela_todo', async () => {
    const d = copia(D.listo); d.id = 'mxQ1';
    let soltar = null;
    manejador = (m, url) => {
      if (m === 'POST' && url.endsWith('/patron')) return new Promise(r => { soltar = () => r(copia(d)); });
      return copia(d);
    };
    await abrirMux(d.id, d);
    const estado = () => ({
      campos: ['nombre', 'titulo', 'pareja-a', 'pareja-b'].map(x => {
        const el = document.getElementById(`mux-mxQ1-${x}`); return el ? el.disabled : null; }),
      botones: ['resumen', 'pie'].map(w => {
        const b = document.getElementById(`mux-multiplexar-${w}-mxQ1`); return b ? b.disabled : null; }),
    });
    const antes = estado();
    const enCurso = muxPatron('mxQ1', 'audio', 'todo_a');
    await espera(30);
    const durante = estado();
    soltar(); await enCurso; await espera(20);
    return {antes, durante, despues: estado()};
  });

  // «Multiplexar» espera también al patrón: el servidor ya tiene la selección
  // nueva y la pantalla todavía la de antes.
  await caso('multiplexar_espera_al_patron', async () => {
    const d = copia(D.listo); d.id = 'mxQ2';
    d.plan.avisos = [];
    const ev = [];
    let soltar = null;
    const srv = servidor(d);
    manejador = (m, url, body) => {
      const que = m + (url.endsWith('/patron') ? ' patron' : url.endsWith('/ejecutar') ? ' ejecutar' : '');
      ev.push(que);
      if (que === 'POST patron') {
        return new Promise(r => { soltar = () => { ev.push('fin patron'); r(srv(m, url, body)); }; });
      }
      return srv(m, url, body);
    };
    await abrirMux(d.id, d);
    ev.length = 0;
    const patron = muxPatron('mxQ2', 'audio', 'todo_b');
    await espera(20);
    const multiplexar = muxMultiplexar('mxQ2');
    await espera(60);
    const antes_de_volver = ev.slice();
    soltar(); await patron; await multiplexar; await espera(30);
    const p = _muxProyecto('mxQ2');
    if (p && p._timer) { clearTimeout(p._timer); p._timer = null; }
    cerrarMux('mxQ2');
    return {antes_de_volver, ev: ev.filter(x => x !== 'GET')};
  });

  // Tras un PUT rechazado, la pantalla enseña el cambio hasta que vuelve la
  // relectura: encolar en ese hueco mandaba la selección guardada, que no es
  // la que se ve.
  await caso('multiplexar_durante_la_relectura', async () => {
    const d = copia(D.listo); d.id = 'mxQ3';
    d.plan.avisos = [];
    const ev = [];
    manejador = (m, url) => {
      ev.push(m + (url.endsWith('/ejecutar') ? ' ejecutar' : ''));
      if (m === 'PUT') return null;
      if (m === 'GET') return tarde(150, () => { ev.push('fin GET'); return copia(d); });
      return copia(d);
    };
    await abrirMux(d.id, d);
    ev.length = 0;
    fila('mxQ3', '1:2').querySelector('.track-actions button').click();
    await espera(30);
    await muxMultiplexar('mxQ3');
    await espera(200);
    cerrarMux('mxQ3');
    return {ev};
  });

  // Cerrar con un patrón en vuelo y reabrir: la lectura espera a que vuelva.
  await caso('reabrir_con_patron', async () => {
    const d = copia(D.listo); d.id = 'mxQ4';
    const ev = [];
    let soltar = null;
    const srv = servidor(d);
    manejador = (m, url, body) => {
      const que = m + (url.endsWith('/patron') ? ' patron' : '');
      ev.push(que);
      if (que === 'POST patron') {
        return new Promise(r => { soltar = () => { ev.push('fin patron'); r(srv(m, url, body)); }; });
      }
      return srv(m, url, body);
    };
    await abrirMux(d.id, d);
    ev.length = 0;
    const patron = muxPatron('mxQ4', 'audio', 'todo_b');
    await espera(20);
    cerrarMux('mxQ4');
    const reabrir = abrirMux('mxQ4');
    await espera(60);
    const antes_de_volver = ev.slice();
    soltar(); await patron; await reabrir; await espera(20);
    cerrarMux('mxQ4');
    return {antes_de_volver, ev};
  });

  // Con `/ejecutar` en vuelo la selección está congelada: lo que se tocara
  // rebotaría con un 409 y el MKV saldría con la de antes.
  await caso('encolando_no_se_edita', async () => {
    const d = copia(D.listo); d.id = 'mxQ5';
    d.plan.avisos = [];
    let soltar = null;
    const srv = servidor(d);
    manejador = (m, url, body) => {
      if (m === 'POST' && url.endsWith('/ejecutar')) return new Promise(r => { soltar = () => r(srv(m, url, body)); });
      return srv(m, url, body);
    };
    await abrirMux(d.id, d);
    const enCurso = muxMultiplexar('mxQ5');
    await espera(30);
    const interruptores = [...document.querySelectorAll('#mux-cuerpo-mxQ5 .mux-interruptor')].map(i => i.disabled);
    const titulo = document.getElementById('mux-mxQ5-titulo').disabled;
    const sw = document.createElement('input'); sw.type = 'checkbox'; sw.checked = true;
    muxAlternar('mxQ5', 0, 1, sw);
    const puts_durante = puts().length;
    soltar(); await enCurso; await espera(20);
    const p = _muxProyecto('mxQ5');
    if (p && p._timer) { clearTimeout(p._timer); p._timer = null; }
    cerrarMux('mxQ5');
    return {interruptores, titulo, puts_durante, revertido: sw.checked === false};
  });

  // El diálogo preguntó por una lista; si lo guardado mientras estaba
  // abierto deja vacía otra sección, el «sí» era a otra pregunta.
  await caso('confirmar_con_vacias_nuevas', async () => {
    const d = copia(D.listo); d.id = 'mxQ6';
    manejador = servidor(d, {PUT: (url, body, actual) => tarde(60, () => {
      actual.plan = Object.assign(copia(actual.plan), {avisos: [...(actual.plan.avisos || []),
        {id: 'seccion_vacia', params: {seccion: 'subtitulos'}}]});
      return copia(actual);
    })});
    await abrirMux(d.id, d);
    await muxMultiplexar('mxQ6');
    const primero = document.getElementById('confirm-message').textContent;
    muxAlternar('mxQ6', 1, 5);                        // quita el subtítulo: PUT en vuelo
    document.getElementById('confirm-ok-btn').click();
    await espera(160);
    const abierto = document.getElementById('confirm-modal').classList.contains('open');
    const segundo = document.getElementById('confirm-message').textContent;
    const ejecutar_antes = window.__pet.filter(x => x.m === 'POST' && x.url.endsWith('/ejecutar')).length;
    document.getElementById('confirm-ok-btn').click();
    await espera(60);
    const ejecutar_despues = window.__pet.filter(x => x.m === 'POST' && x.url.endsWith('/ejecutar')).length;
    const p = _muxProyecto('mxQ6');
    if (p && p._timer) { clearTimeout(p._timer); p._timer = null; }
    cerrarMux('mxQ6');
    return {primero, segundo, abierto, ejecutar_antes, ejecutar_despues};
  });

  // Volver al valor de antes con el PUT en vuelo también se guarda: se
  // compara con lo PEDIDO, no con lo recibido.
  await caso('volver_al_valor', async () => {
    const d = copia(D.listo); d.id = 'mxQ7';
    const srv = servidor(d);
    manejador = (m, url, body) => (m === 'PUT' ? tarde(120, () => srv(m, url, body)) : srv(m, url, body));
    await abrirMux(d.id, d);
    const original = d.nombre_salida;
    const el = document.getElementById('mux-mxQ7-nombre');
    el.value = 'Nuevo.mkv'; muxNombreSalida('mxQ7', el);
    await espera(20);
    el.value = original; muxNombreSalida('mxQ7', el);
    await espera(300);
    const t = document.getElementById('mux-mxQ7-titulo');
    t.value = 'Otro título'; muxTituloContenedor('mxQ7', t);
    await espera(20);
    t.value = ''; muxTituloContenedor('mxQ7', t);
    await espera(300);
    const datos = _muxProyecto('mxQ7').datos;
    return {original, nombres: puts().filter(x => 'nombre_salida' in x.body).map(x => x.body.nombre_salida),
            titulos: puts().filter(x => 'titulo' in x.body).map(x => x.body.titulo),
            nombre_final: datos.nombre_salida, titulo_final: datos.titulo};
  });

  // Un vídeo con la BL y la EL en pistas aparte se elige entero.
  await caso('elegir_video_bl_el', async () => {
    const d = copia(D.listo); d.id = 'mxQ8';
    d.fuentes[1].pistas.splice(1, 0, Object.assign(copia(d.fuentes[1].pistas[0]), {id: 98}));
    manejador = servidor(d);
    await abrirMux(d.id, d);
    pulsar(interruptor('mxQ8', `muxAlternarVideo("mxQ8", 1,`));
    await espera(30);
    return {puts: puts().map(x => x.body), tipos: tipos(d)};
  });

  await caso('patron_nada', async () => {
    const d = copia(D.listo); d.id = 'mxQ9';
    manejador = servidor(d);
    await abrirMux(d.id, d);
    const boton = [...document.querySelectorAll('#mux-sec-audio-mxQ9 .mux-columna-b .mux-columna-acciones button')]
      .find(b => b.getAttribute('data-i18n') === 'tab4.nada');
    if (!boton) return {fallo: 'sin «Nada» en la columna B del audio'};
    boton.click();
    await espera(40);
    return {posts: window.__pet.filter(x => x.m === 'POST').map(x => x.body)};
  });

  // A y B tienen una pista con el mismo id: renombrar la de B no toca la de A.
  await caso('nombre_mismo_id', async () => {
    const d = copia(D.listo); d.id = 'mxQ10';
    d.pistas.splice(1, 0, {fuente: 0, id: 1, nombre: 'A TrueHD', por_defecto: false, forzada: false});
    manejador = servidor(d);
    await abrirMux(d.id, d);
    const el = document.getElementById('mux-mxQ10-pista-1-1');
    el.value = 'Renamed B'; el.dispatchEvent(new Event('change'));
    await espera(30);
    return {puts: puts().map(x => x.body)};
  });

  await caso('reordenar_subtitulos', async () => {
    const d = copia(D.listo); d.id = 'mxQ11';
    d.pistas.push({fuente: 1, id: 6, nombre: 'English Full (PGS)', por_defecto: false, forzada: false});
    manejador = servidor(d);
    await abrirMux(d.id, d);
    const lista = document.querySelector('#mux-cuerpo-mxQ11 .mux-orden[data-tipo="subtitles"]');
    lista.insertBefore(lista.lastElementChild, lista.firstElementChild);
    const p = _muxProyecto('mxQ11');
    const s = p._sortables.find(x => x.el === lista);
    s.options.onStart();
    s.options.onEnd({});
    await espera(30);
    return {puts: puts().map(x => x.body), tipos: tipos(d)};
  });

  await caso('tiempos_de_capitulos', async () => {
    const d = copia(D.listo); d.id = 'mxQ12';
    await abrirMux(d.id, d);
    return {tiempos: [...document.querySelectorAll('#mux-sec-capitulos-mxQ12 .mux-cap-final .mux-cap-tiempo')]
              .map(x => x.textContent),
            lista: ((d.plan.capitulos || {}).lista) || []};
  });

  out.errores = window.__errores || [];
  document.getElementById('__out').textContent = JSON.stringify(out);
}, 60));
</script>
"""


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestLaPestanaMultiplexarEnChrome(unittest.TestCase):

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

    def _tr(self, clave, **p):
        v = self.cat[clave]
        for k, x in p.items():
            v = v.replace("{" + k + "}", str(x))
        return v

    def _sin_esquema(self, bodies):
        """Los cuerpos de los PUT sin `esquema`, después de comprobar que cada
        uno lo lleva: un PUT sin él lo rechaza el servidor (`recarga la
        página`), y uno que lo mandara mal borraría el vídeo."""
        out = []
        for b in bodies:
            self.assertEqual(b.get("esquema"), 2, f"un PUT sin el esquema: {b}")
            out.append({k: v for k, v in b.items() if k != "esquema"})
        return out

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
        # 40 ppm son 144 ms por hora (1 ppm = 3,6 ms/h).
        self.assertEqual(c["ms_por_hora"], "144,0")
        self.assertEqual(c["lista"], "Subtítulos, Capítulos y Otros")

    # ── Lo que manda cada edición ────────────────────────────────────────

    def test_editar_el_nombre_manda_la_lista_entera_con_el_video(self):
        c = self.caso("nombre_de_pista")
        self.assertEqual(len(c["puts"]), 1, c["puts"])
        self.assertEqual(c["puts"][0]["url"], "/api/mux/mx1")
        [body] = self._sin_esquema([c["puts"][0]["body"]])
        self.assertEqual(body["pistas"], self._esperadas(
            c["antes"], cambio=((1, 1), {"nombre": "Spanish TrueHD Atmos 7.1 DCP"})))
        self.assertEqual((body["pistas"][0]["fuente"], body["pistas"][0]["id"]), (0, 0),
                         "el vídeo va el primero de la lista")
        self.assertEqual(c["valor"], "Spanish TrueHD Atmos 7.1 DCP")

    def test_la_marca_por_defecto_cambia_solo_en_su_pista(self):
        c = self.caso("marca_por_defecto")
        [body] = self._sin_esquema(c["puts"])
        antes = next(p for p in c["antes"] if (p["fuente"], p["id"]) == (1, 2))
        self.assertEqual(body["pistas"], self._esperadas(
            c["antes"], cambio=((1, 2), {"por_defecto": not antes["por_defecto"]})))

    def test_quitar_una_pista(self):
        c = self.caso("quitar_pista")
        [body] = self._sin_esquema(c["puts"])
        claves = [(p["fuente"], p["id"]) for p in body["pistas"]]
        self.assertNotIn((1, 2), claves)
        self.assertIn((0, 0), claves, "quitar un audio no se lleva el vídeo")
        self.assertFalse(c["queda"])

    def test_un_interruptor_anade_al_final_de_su_tipo_y_sin_robar_la_marca(self):
        c = self.caso("alternar_audio")
        self.assertFalse(c["antes"])
        self.assertTrue(c["despues"])
        [body] = self._sin_esquema(c["puts"])
        pistas = body["pistas"]
        tipos = [c["tipos"][f'{p["fuente"]}:{p["id"]}'] for p in pistas]
        orden = {"video": 0, "audio": 1, "subtitles": 2}
        self.assertEqual(tipos, sorted(tipos, key=orden.get), "vídeo, audio y subtítulos, en ese orden")
        ultima_de_audio = [p for p, t in zip(pistas, tipos) if t == "audio"][-1]
        self.assertEqual((ultima_de_audio["fuente"], ultima_de_audio["id"]), (0, 1))
        # Entra con el nombre que trae en su MKV.
        self.assertTrue(c["origen"])
        self.assertEqual(ultima_de_audio["nombre"], c["origen"])
        # Ya había un audio por defecto: la añadida no lo es aunque en su MKV sí.
        self.assertFalse(ultima_de_audio["por_defecto"])
        self.assertEqual(sum(p["por_defecto"] for p, t in zip(pistas, tipos) if t == "audio"), 1)

    def test_el_video_sale_de_un_solo_mkv_y_se_quita_entero(self):
        c = self.caso("alternar_video")
        bodies = self._sin_esquema(c["puts"])
        self.assertEqual(len(bodies), 2, bodies)
        con_b = [(p["fuente"], p["id"]) for p in bodies[0]["pistas"]
                 if c["tipos"][f'{p["fuente"]}:{p["id"]}'] == "video"]
        self.assertEqual(con_b, [(1, 0)], "elegir el vídeo de B quita el de A")
        self.assertEqual((bodies[0]["pistas"][0]["fuente"], bodies[0]["pistas"][0]["id"]), (1, 0))
        self.assertFalse(c["elegidoA"])
        self.assertTrue(c["elegidoB"])
        sin = [p for p in bodies[1]["pistas"] if c["tipos"][f'{p["fuente"]}:{p["id"]}'] == "video"]
        self.assertEqual(sin, [], "quitarlo desde «En el MKV final» lo quita entero")

    def test_quitar_una_pista_del_video_lo_quita_entero(self):
        c = self.caso("quitar_video_entero")
        [body] = self._sin_esquema(c["puts"])
        videos = [(p["fuente"], p["id"]) for p in body["pistas"]
                  if c["tipos"].get(f'{p["fuente"]}:{p["id"]}') == "video"]
        self.assertEqual(videos, [], "quedó media pista de vídeo")

    def test_los_capitulos_se_eligen_uno_a_uno_de_los_dos(self):
        c = self.caso("alternar_capitulo")
        bodies = self._sin_esquema(c["puts"])
        antes = [(x["fuente"], x["edicion"], x["indice"]) for x in c["antes"]]
        primero = [(x["fuente"], x["edicion"], x["indice"]) for x in bodies[0]["capitulos"]]
        self.assertEqual(primero, antes + [(0, 0, 2)], "un capítulo de A se suma a los de B")
        segundo = [(x["fuente"], x["edicion"], x["indice"]) for x in bodies[1]["capitulos"]]
        self.assertEqual(segundo, [k for k in primero if k != (1, 0, 0)])

    def test_un_adjunto_se_marca_y_se_desmarca(self):
        c = self.caso("alternar_adjunto")
        self.assertFalse(c["antes"])
        self.assertEqual(self._sin_esquema(c["puts"]),
                         [{"adjuntos": [{"fuente": 0, "id": 1}]}, {"adjuntos": []}])

    # ── Los patrones ─────────────────────────────────────────────────────

    def test_un_patron_lo_resuelve_el_servidor(self):
        c = self.caso("patron")
        self.assertEqual(c["posts"], [{"url": "/api/mux/mx7/patron",
                                       "body": {"esquema": 2, "seccion": "audio", "patron": "todo_b"}}])
        self.assertEqual(c["nombre"], "After pattern.mkv", "la respuesta del patrón no se aplicó")
        self.assertEqual(len(c["mis"]), 1)
        self.assertIn("'mis_idiomas'", c["mis"][0].replace('"', "'"))

    def test_un_patron_espera_a_lo_que_se_esta_guardando(self):
        """Un patrón calculado con un cambio aún en vuelo mezclaría los dos
        (revisión de Codex del plan, 2026-10-27)."""
        self.assertEqual(self.caso("patron_espera")["ev"], ["PUT", "fin PUT", "POST patron"])

    def test_con_un_patron_en_vuelo_no_se_toca_la_seleccion(self):
        c = self.caso("patron_bloquea")
        self.assertTrue(c["durante"] and all(c["durante"]), "interruptores tocables con el patrón en vuelo")
        self.assertEqual(c["putsDurante"], 0, "un cambio durante el patrón se guardó")
        self.assertTrue(c["revertido"], "el interruptor ignorado se quedó cambiado en pantalla")
        self.assertFalse(any(c["despues"]), "al volver el patrón, la selección sigue bloqueada")

    # ── Multiplexar ──────────────────────────────────────────────────────

    def test_antes_de_multiplexar_avisa_de_lo_que_se_quedo_vacio(self):
        """El proyecto nace sin nada elegido y se va sección a sección: lo
        que se quedó vacío se pregunta, para no repetir veinte minutos por un
        descuido (decisión del usuario, 2026-10-27)."""
        c = self.caso("multiplexar_con_vacias")
        self.assertTrue(c["habilitado"])
        self.assertTrue(c["abierto"], "no preguntó por las secciones vacías")
        self.assertEqual(c["antes"], 0, "encoló antes de confirmar")
        self.assertEqual(c["mensaje"], self._tr("tab4.hay_secciones_sin_nada_elegido_texto",
                                                secciones=self.cat["tab4.seccion_otros"]))
        self.assertEqual(c["boton_ok"], self.cat["tab4.multiplexar_sin_ellas"])
        self.assertEqual(c["posts"], ["/api/mux/mxM/ejecutar"])
        self.assertTrue(c["despues_deshabilitado"])
        self.assertIn(self.cat["tab4.seccion_otros"], c["nota"], "el resumen no lo decía antes de pulsar")

    def test_confirmar_en_el_dialogo_espera_a_lo_que_se_esta_guardando(self):
        c = self.caso("confirmar_espera")
        self.assertTrue(c["abierto"])
        self.assertEqual(c["ev"], ["PUT", "fin PUT", "POST ejecutar"])

    def test_confirmar_no_encola_si_rechazaron_lo_ultimo(self):
        c = self.caso("confirmar_tras_rechazo")
        self.assertTrue(c["abierto"])
        self.assertNotIn("POST ejecutar", c["ev"])

    def test_confirmar_no_pide_lo_que_el_plan_nuevo_no_deja(self):
        c = self.caso("confirmar_con_plan_nuevo")
        self.assertTrue(c["abierto"])
        self.assertNotIn("POST ejecutar", c["ev"])

    def test_las_secciones_vacias_se_miran_sobre_lo_guardado(self):
        c = self.caso("multiplexar_avisos_frescos")
        self.assertFalse(c["abierto"], "preguntó por una sección que ya no estaba vacía")
        self.assertEqual(c["posts"], ["/api/mux/mxAF/ejecutar"])

    def test_sin_secciones_vacias_multiplexa_sin_preguntar(self):
        c = self.caso("multiplexar_sin_vacias")
        self.assertFalse(c["abierto"])
        self.assertEqual(c["posts"], ["/api/mux/mxN/ejecutar"])

    def test_aceptar_el_riesgo_pregunta_antes(self):
        c = self.caso("aceptar_el_riesgo")
        self.assertTrue(c["abierto"], "el diálogo de confirmación no se abrió")
        self.assertEqual(c["antes"], 0, "guardó antes de confirmar")
        self.assertEqual(self._sin_esquema(c["puts"]), [{"acepta_riesgo": True}])

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
        self.assertNotIn(self.cat["tab4.multiplexado_cancelado"], " ".join(c["nada"]))
        self.assertIn(self.cat["tab4.quitado_de_la_cola"], " ".join(c["descartada"]))
        self.assertIn(self.cat["tab4.multiplexado_cancelado"], " ".join(c["cancelada"]))
        self.assertEqual(c["posts"], ["/api/mux/mx13/medir/cancelar", "/api/mux/mx13/cancelar"])

    def test_dos_veces_el_mismo_fichero_no_se_puede_crear(self):
        c = self.caso("crear_mismo_fichero")
        self.assertTrue(c["deshabilitado"])
        self.assertTrue(c["error_visible"])
        self.assertEqual(c["error"], self.cat["tab4.los_dos_son_el_mismo_fichero"])
        self.assertEqual(c["roles"], [self.cat["tab4.mkv_a"], self.cat["tab4.mkv_b"]],
                         "los huecos no tienen papeles: A y B")
        self.assertTrue(c["distintos_habilitado"])
        self.assertTrue(c["distintos_sin_error"])

    def test_crear_lee_a_y_b_con_el_timeout_largo_y_abre_el_proyecto_vacio(self):
        c = self.caso("crear_bien")
        self.assertTrue(c["url"].startswith("/api/mux/create?job="), c["url"])
        self.assertEqual(c["body"], {"a": "/mnt/library/a.mkv", "b": "/mnt/isos/b.mkv"})
        self.assertEqual(c["timeout"], c["largo"])
        self.assertTrue(c["cerrado"])
        self.assertTrue(c["panel"])
        self.assertEqual(c["elegidos"], 0, "el proyecto nace sin nada elegido")
        self.assertTrue(c["boton_deshabilitado"])

    def test_un_fallo_al_crear_lo_dice_en_el_modal_y_solo_el_suyo(self):
        c = self.caso("crear_mal")
        self.assertTrue(c["abierto"])
        self.assertTrue(c["huecos"], "tras el fallo se vuelve a la selección")
        self.assertEqual(c["error"], "mkvmerge -J failed on b.mkv")
        self.assertEqual(c["ajeno"], self.cat["tab4.no_se_pudieron_leer"])

    def test_el_veredicto_y_el_plan_en_las_dos_direcciones(self):
        c = self.caso("veredicto_y_plan")
        # El sentido se dice una vez: «llega 2,35 s más tarde», no «+2,35 s».
        self.assertIn(self._tr("tab4.v_retraso_b_tarde", ms="2,35 s"), c["retraso"])
        self.assertIn(self._tr("tab4.v_retraso", ms="+2,35 s"), c["retraso"])
        self.assertIn(self._tr("tab4.plan_movidas", letra="B", ms="−2,35 s", n=3), c["plan_retraso"])
        # Velocidades distintas: con el vídeo de A, A pasa a la de B…
        self.assertIn(self._tr("tab4.v_velocidades_distintas", a="23,976", b="24"), c["velocidades"])
        self.assertIn(self._tr("tab4.v_velocidades_ajuste", letra="A", otra="B", fps="24"), c["velocidades"])
        self.assertIn(self._tr("tab4.plan_velocidad_rapido", letra="A", otra="B", de="23,976", a="24",
                               pct="0,10"), c["plan_listo"])
        # …y con el de B, B pasa a la de A: el mismo caso, al revés.
        self.assertIn(self._tr("tab4.v_velocidades_ajuste", letra="B", otra="A", fps="23,976"),
                      c["velocidades_reves"])
        self.assertIn(self._tr("tab4.plan_velocidad_lento", letra="B", otra="A", de="24", a="23,976",
                               pct="0,10"), c["plan_reves"])
        self.assertIn(self._tr("tab4.plan_movidas", letra="A", ms="+1,50 s", n=3), c["plan_reves"])
        self.assertIn(self.cat["tab4.plan_sin_ajuste_montaje"], c["plan_montaje"])
        # Sin nada elegido no hay plan que contar, y el veredicto no inventa.
        self.assertEqual(c["plan_vacio"], "")
        self.assertIn(self.cat["tab4.v_velocidades_sin_elegir"], c["velocidades_vacio"])

    def test_el_grafico_solo_dibuja_la_recta_cuando_describe_algo(self):
        c = self.caso("grafico")
        o, m, n = c["velocidades"], c["montaje"], c["dudosa"]
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

    def test_una_columna_larga_ensena_lo_elegido_y_lo_de_tus_idiomas(self):
        c = self.caso("columna_plegada")
        self.assertEqual(c["total"], 18)
        # Los tres en inglés de A (los de «mis idiomas» con la app en inglés)
        # y el castellano elegido.
        self.assertEqual(c["plegada"], 4)
        self.assertEqual(c["rotulo"], self._tr("tab4.ver_las_n_restantes", n=14))
        self.assertEqual(c["abierta"], 18)
        self.assertEqual(c["rotulo_abierta"], self.cat["tab4.ver_menos"])
        # Los capítulos no se pliegan: van en su caja con scroll.
        self.assertEqual(c["capitulos_cajas"], 2)
        # «En el MKV final»: los doce de B, uno fuera del vídeo.
        self.assertEqual((c["final_capitulos"], c["fuera"]), (12, 1))

    def test_si_la_medida_no_hace_falta_se_dice(self):
        c = self.caso("no_hace_falta")
        self.assertIn(self.cat["tab4.con_lo_elegido_no_hace_falta"], c["conBanner"])
        self.assertNotIn(self.cat["tab4.con_lo_elegido_no_hace_falta"], c["sinBanner"])

    def test_un_proyecto_vacio_dice_que_falta(self):
        c = self.caso("vacio")
        self.assertEqual(c["aporta"], [self.cat["tab4.no_aporta_nada"]] * 2)
        self.assertEqual(c["motivos"], [self.cat["tab4.motivo_sin_video"], self.cat["tab4.motivo_sin_audio"]])
        self.assertTrue(c["boton"])
        self.assertFalse(c["plan"], "sin nada elegido no hay «Al multiplexar»")
        self.assertIn(self.cat["tab4.final_vacio_video"], c["finales"])

    def test_reordenar_manda_el_orden_nuevo(self):
        c = self.caso("reordenar")
        self.assertEqual(c["listas"], ["audio", "subtitles"], "el vídeo no se arrastra")
        [body] = self._sin_esquema(c["puts"])
        nuevo = [(p["fuente"], p["id"]) for p in body["pistas"]]
        antes = [(p["fuente"], p["id"]) for p in c["antes"]]
        de = lambda t: [k for k in antes if c["tipos"][f"{k[0]}:{k[1]}"] == t]
        video, audio = de("video"), de("audio")
        self.assertGreaterEqual(len(audio), 2)
        self.assertEqual(nuevo[:len(video)], video, "el vídeo sigue delante")
        self.assertEqual(nuevo[len(video):len(video) + len(audio)], [audio[-1]] + audio[:-1])
        # Y la lista ENTERA: los subtítulos siguen detrás, todos y en su orden.
        self.assertEqual(len(nuevo), len(antes))
        self.assertEqual(nuevo[len(video) + len(audio):], antes[len(video) + len(audio):])

    def test_lo_que_se_escribe_sobrevive_al_repintado(self):
        c = self.caso("anclaje")
        self.assertEqual(c["escrito"], "half typed")
        self.assertEqual(c["foco"], "mux-mx20-pista-1-2")
        self.assertEqual(c["del_servidor"], "Server renamed.mkv")

    def test_un_arrastre_aplaza_el_repintado(self):
        c = self.caso("arrastre")
        self.assertNotEqual(c["durante"], "During drag.mkv")
        self.assertEqual(c["despues"], "During drag.mkv")

    def test_el_detalle_de_una_ejecucion_es_el_de_esa_ejecucion(self):
        c = self.caso("detalle_multiplexado")
        self.assertIn("run one line", c["cuerpo"])
        self.assertNotIn("run two line", c["cuerpo"])
        self.assertNotIn("live line", c["cuerpo"])
        self.assertEqual(c["lateral_fases"], 3)
        self.assertEqual(c["hechas"], 3)
        self.assertEqual(c["caido"], {"sinDatos": True})

    # ── El orden de las peticiones ───────────────────────────────────────

    def test_multiplexar_espera_a_que_lo_tocado_este_guardado(self):
        c = self.caso("multiplexar_espera")
        self.assertEqual(c["ev"], ["PUT", "fin PUT", "PUT", "fin PUT", "POST ejecutar"])

    def test_multiplexar_no_pide_lo_que_el_plan_nuevo_no_deja(self):
        c = self.caso("multiplexar_con_plan_nuevo")
        self.assertFalse(any(p.endswith("/ejecutar") for p in c["pedidas"]), c["pedidas"])
        self.assertFalse(c["abierto"], "preguntó por las secciones vacías sin poder multiplexar")
        self.assertTrue(c["boton_deshabilitado"])

    def test_borrar_espera_a_lo_que_se_estaba_guardando(self):
        c = self.caso("borrar_espera_guardado")
        self.assertEqual(c["ev"], ["PUT", "fin PUT", "DELETE"])
        self.assertFalse(c["abierto"])

    def test_multiplexar_no_encola_si_el_servidor_rechazo_un_cambio(self):
        c = self.caso("multiplexar_tras_rechazo")
        self.assertEqual(c["pedidas"], ["PUT /api/mux/mxA2", "GET /api/mux/mxA2"])

    def test_de_dos_lecturas_gana_la_que_salio_despues(self):
        self.assertEqual(self.caso("dos_lecturas")["datos"], "Newer.mkv")

    def test_un_get_que_se_cruza_con_un_put_no_lo_deshace(self):
        c = self.caso("get_cruzado")
        self.assertEqual(c["datos"], "New name.mkv")
        self.assertEqual(c["pantalla"], "New name.mkv")

    def test_un_get_que_vuelve_con_el_put_en_vuelo_no_se_aplica(self):
        c = self.caso("get_durante_put")
        self.assertEqual(c["durante"], "New name.mkv")
        self.assertEqual(c["despues"], "New name.mkv")

    def test_un_get_que_vuelve_con_el_patron_en_vuelo_no_se_aplica(self):
        c = self.caso("get_durante_patron")
        self.assertNotEqual(c["durante"], "Old name.mkv")
        self.assertEqual(c["despues"], "After pattern.mkv")

    def test_un_get_que_se_cruza_con_un_patron_no_lo_deshace(self):
        self.assertEqual(self.caso("get_cruzado_con_patron")["datos"], "After pattern.mkv")

    def test_la_respuesta_de_multiplexar_gana_a_un_get_que_ya_volaba(self):
        c = self.caso("post_y_get_viejo")
        self.assertEqual(c["status"], "queued")
        self.assertTrue(c["sigue_leyendo"], "el poll se paró con el proyecto en cola")
        self.assertTrue(c["boton_deshabilitado"])

    def test_la_ficha_nueva_gana_a_un_get_que_ya_volaba(self):
        c = self.caso("ficha_y_get_viejo")
        self.assertEqual(c["poster"], c["esperado"])

    def test_cerrar_la_pestana_no_tira_lo_que_esperaba_guardarse(self):
        c = self.caso("cerrar_con_pendiente")
        self.assertEqual(self._sin_esquema(c["puts"]),
                         [{"titulo": "Title D"}, {"adjuntos": [{"fuente": 0, "id": 1}]}])

    def test_reabrirla_espera_al_guardado_antes_de_leer(self):
        c = self.caso("reabrir_tras_cerrar")
        self.assertEqual(c["ev"], ["PUT", "fin PUT", "GET"])
        self.assertEqual(c["titulo"], "Title E")

    def test_un_clic_tras_editar_un_nombre_no_se_pierde(self):
        """El `change` del nombre salta en el `mousedown` del botón: si ese
        repintado lo sustituye, el navegador no genera el `click`."""
        c = self.caso("clic_tras_editar")
        self.assertTrue(c["conectado"], "el repintado sustituyó el botón a mitad del clic")
        self.assertIsNotNone(c["pista"])
        self.assertEqual(c["pista"]["nombre"], "Renamed while clicking")
        self.assertEqual(c["pista"]["por_defecto"], not c["antes"])
        self.assertEqual(c["pantalla"], "Renamed while clicking")

    def test_medir_con_las_pistas_guardadas_mide(self):
        c = self.caso("medir_mismas_pistas")
        self.assertEqual(c["misma"], ["POST /api/mux/mxG1/medir", "GET /api/mux/mxG1"])
        self.assertEqual([(p["m"], p["body"]) for p in c["otra"]],
                         [("PUT", {"esquema": 2, "pareja": c["pareja_otra"]})])

    def test_la_lista_que_vuela_no_pisa_lo_nuevo_ni_resucita_lo_borrado(self):
        c = self.caso("lista_vieja")
        self.assertEqual(c["nombres"].get("mxH1"), "New list.mkv")
        self.assertNotIn("mxH2", c["nombres"])
        self.assertTrue(c["tarjeta_nueva"])
        self.assertFalse(c["tarjeta_vieja"])

    def test_un_nombre_vacio_no_se_guarda_ni_se_queda_a_la_vista(self):
        c = self.caso("nombre_vacio")
        self.assertEqual(c["puts"], 0)
        self.assertEqual(c["valor"], c["original"])

    def test_un_guardado_a_la_vez_y_lo_que_espera_se_junta(self):
        c = self.caso("dos_ediciones")
        self.assertEqual(c["max"], 1, "dos PUT a la vez pueden guardarse en orden inverso")
        self.assertEqual(self._sin_esquema(c["puts"]),
                         [{"titulo": "T1"}, {"adjuntos": [{"fuente": 0, "id": 1}], "titulo": "T2"}])

    def test_mientras_mide_se_sigue_leyendo(self):
        self.assertGreaterEqual(self.caso("lee_mientras_mide")["gets"], 1)

    def test_aceptar_el_riesgo_habilita_multiplexar_con_el_plan_del_servidor(self):
        c = self.caso("aceptar_riesgo_habilita")
        self.assertTrue(c["antes"])
        self.assertFalse(c["despues"], "el botón siguió con el plan de antes de aceptar")

    def test_en_cola_no_se_edita_nada(self):
        c = self.caso("en_cola_bloquea")
        for clave in ("campos", "marcas", "quitar", "interruptores", "patrones"):
            self.assertTrue(c[clave], f"no hay {clave} que mirar")
            self.assertTrue(all(c[clave]), f"{clave} editables con el proyecto en cola: {c[clave]}")
        self.assertEqual(c["sortables"], 0)

    def test_el_retraso_a_mano_conserva_el_signo(self):
        c = self.caso("retraso_manual")
        self.assertEqual(self._sin_esquema(c["puts"]),
                         [{"retraso_manual_ms": 250}, {"retraso_manual_ms": -80}])
        self.assertTrue(c["sin_guardar"])
        self.assertEqual(c["restaurado"], "-80")

    def test_el_progreso_de_otra_creacion_no_se_pinta(self):
        c = self.caso("progreso_ajeno")
        self.assertGreaterEqual(c["pedidos"], 1)
        self.assertEqual(c["hechos"], 0)
        # Los dos MKV llevan su muestra de Dolby Vision: de cualquiera puede
        # salir el vídeo.
        self.assertEqual(c["pasos"], 8)

    def test_una_creacion_vieja_no_pinta_sobre_la_nueva(self):
        c = self.caso("creacion_vieja")
        self.assertEqual(c["posts"], 2)
        self.assertTrue(c["leyendo"])
        self.assertTrue(c["pasos_visibles"], "el error de la primera devolvió el modal a la selección")
        self.assertFalse(c["huecos_visibles"])
        self.assertEqual(c["hechos"], 0, "se pintaron los pasos de la primera creación")
        self.assertEqual(c["error"], "")

    def test_la_comprobacion_dice_contra_que_pista_y_de_que_mkv(self):
        c = self.caso("comprobacion_sincronia")
        filas = {f["titulo"]: f for f in c["filas"]}
        ref = filas[self._tr("tab4.check_sincronia", n=2)]
        self.assertEqual(ref["detalle"], [self._tr("tab4.check_sincronia_contra", pista=c["contra_a"], letra="A")])
        sin = filas[self._tr("tab4.check_sincronia_pista", n=3)]
        self.assertEqual(sin["texto"], self._tr("tab4.check_sincronia_sin_doblaje", letra="A"))
        self.assertIn("mux-check-aviso", sin["clase"])
        self.assertEqual(sin["detalle"], [])
        # Con el vídeo de B, la referencia es una pista de B.
        ref_b = {f["titulo"]: f for f in c["reves"]}[self._tr("tab4.check_sincronia", n=2)]
        self.assertEqual(ref_b["detalle"], [self._tr("tab4.check_sincronia_contra", pista=c["contra_b"], letra="B")])
        # Una validación de antes del rediseño, sin `contra_fuente`, era contra A.
        ref_v = {f["titulo"]: f for f in c["vieja"]}[self._tr("tab4.check_sincronia", n=2)]
        self.assertEqual(ref_v["detalle"], ref["detalle"])

    def test_un_nombre_con_comillas_llega_entero_al_campo(self):
        c = self.caso("comillas")
        self.assertEqual(c["valor"], c["esperado"])

    def test_el_detalle_de_la_medida(self):
        c = self.caso("detalle_medida")
        self.assertIn(self.cat["workbar.n_de_total"].replace("{n}", "3").replace("{total}", "7"),
                      c["viva_cuerpo"])
        self.assertEqual(c["vieja_sin_detalle"], "efimero")
        self.assertEqual(c["vieja_cuerpo"], "")
        self.assertTrue(all(u.startswith("/api/mux/mux_Blade") for u in c["pedidas"]), c["pedidas"])


    # ── La revisión de Codex del 2026-10-27 ──────────────────────────────

    def test_un_patron_en_vuelo_congela_todo_lo_que_guarda(self):
        c = self.caso("patron_congela_todo")
        self.assertEqual(c["antes"], {"campos": [False] * 4, "botones": [False, False]}, c)
        self.assertEqual(c["durante"], {"campos": [True] * 4, "botones": [True, True]},
                         "con el patrón en vuelo se podía guardar otra cosa")
        self.assertEqual(c["despues"], c["antes"])

    def test_multiplexar_espera_al_patron(self):
        c = self.caso("multiplexar_espera_al_patron")
        self.assertEqual(c["antes_de_volver"], ["POST patron"],
                         "se encoló con el patrón sin volver")
        self.assertEqual(c["ev"], ["POST patron", "fin patron", "POST ejecutar"])

    def test_no_se_encola_durante_la_relectura_de_un_rechazo(self):
        ev = self.caso("multiplexar_durante_la_relectura")["ev"]
        self.assertNotIn("POST ejecutar", ev,
                         "encoló la selección guardada mientras la pantalla enseñaba otra")
        self.assertEqual(ev, ["PUT", "GET", "fin GET"])

    def test_reabrir_espera_al_patron(self):
        c = self.caso("reabrir_con_patron")
        self.assertEqual(c["antes_de_volver"], ["POST patron"],
                         "leyó la selección de antes con el patrón en vuelo")
        self.assertEqual(c["ev"], ["POST patron", "fin patron", "GET"])

    def test_encolando_la_seleccion_esta_congelada(self):
        c = self.caso("encolando_no_se_edita")
        self.assertTrue(c["interruptores"] and all(c["interruptores"]), c)
        self.assertTrue(c["titulo"])
        self.assertEqual(c["puts_durante"], 0)
        self.assertTrue(c["revertido"], "el interruptor se quedó cambiado sin aplicarse")

    def test_confirmar_vuelve_a_preguntar_si_cambio_lo_vacio(self):
        c = self.caso("confirmar_con_vacias_nuevas")
        self.assertTrue(c["abierto"], "encoló sin preguntar por la sección vaciada")
        self.assertEqual(c["ejecutar_antes"], 0)
        self.assertIn(self._tr("tab4.seccion_subtitulos"), c["segundo"])
        self.assertNotIn(self._tr("tab4.seccion_subtitulos"), c["primero"])
        self.assertEqual(c["ejecutar_despues"], 1)

    def test_volver_al_valor_de_antes_con_el_put_en_vuelo_se_guarda(self):
        c = self.caso("volver_al_valor")
        self.assertEqual(c["nombres"], ["Nuevo.mkv", c["original"]])
        self.assertEqual(c["nombre_final"], c["original"])
        self.assertEqual(c["titulos"], ["Otro título", ""])
        self.assertEqual(c["titulo_final"], "")

    def test_un_video_con_bl_y_el_se_elige_entero(self):
        c = self.caso("elegir_video_bl_el")
        [body] = self._sin_esquema(c["puts"])
        videos = [(p["fuente"], p["id"]) for p in body["pistas"]
                  if c["tipos"].get(f'{p["fuente"]}:{p["id"]}') == "video"]
        self.assertEqual(videos, [(1, 0), (1, 98)], "medio vídeo")

    def test_nada_pide_nada(self):
        self.assertEqual(self.caso("patron_nada")["posts"],
                         [{"esquema": 2, "seccion": "audio", "patron": "nada_b"}])

    def test_renombrar_no_toca_la_pista_del_otro_mkv_con_el_mismo_id(self):
        [body] = self._sin_esquema(self.caso("nombre_mismo_id")["puts"])
        nombres = {(p["fuente"], p["id"]): p["nombre"] for p in body["pistas"]}
        self.assertEqual(nombres[(0, 1)], "A TrueHD")
        self.assertEqual(nombres[(1, 1)], "Renamed B")

    def test_reordenar_los_subtitulos_manda_su_orden(self):
        c = self.caso("reordenar_subtitulos")
        [body] = self._sin_esquema(c["puts"])
        subs = [(p["fuente"], p["id"]) for p in body["pistas"]
                if c["tipos"][f'{p["fuente"]}:{p["id"]}'] == "subtitles"]
        self.assertEqual(subs, [(1, 6), (1, 5)])

    def test_los_capitulos_finales_ensenan_su_tiempo(self):
        import math
        c = self.caso("tiempos_de_capitulos")

        def reloj(seg):
            s = max(0, math.floor(seg + 0.5))
            h, m, x = s // 3600, (s % 3600) // 60, s % 60
            return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"
        esperado = ["—" if x["queda"] == "fuera" else reloj(x["inicio_ns"] / 1e9) for x in c["lista"]]
        self.assertGreater(len(esperado), 3)
        self.assertEqual(c["tiempos"], esperado)


if __name__ == "__main__":
    unittest.main()
