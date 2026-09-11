'use strict';
/**
 * workbar.js — La columna de trabajo: qué se está ejecutando, en toda la app.
 *
 * Vive FUERA de los tres tab-panels a propósito. Hay una sola cola para las
 * tres pestañas, así que el sitio donde se consulta qué está pasando tiene que
 * ser el mismo estés donde estés: si un rip está bloqueando tu fase CMv4.0, se
 * ve sin cambiar de pestaña.
 *
 * **Solo consulta.** Fase, porcentaje, transcurrido y ETA, y nada más. El
 * detalle —log, comandos, tiempos por fase— vive en un modal que se abre a
 * petición (`abrirDetalleDeTrabajo`). Antes de esto, el único trabajo con
 * vista de detalle era una fase CMv4.0, y encima con un overlay que se abría
 * SOLO y tapaba el panel entero.
 *
 * Todo lo que pinta sale de `GET /api/trabajos`, que devuelve la misma forma
 * para los cinco tipos de trabajo (ver `app/trabajos.py`). Esta pieza no sabe
 * qué es un rip ni una fase CMv4.0, y ese es justo el punto.
 */

const _WORKBAR_INTERVALO_MS = 2000;
const _WORKBAR_PREF = 'hdo_workbar_abierta';

let _workbarTimer = null;
/** Lo último que dijo el servidor. Lo lee el modal de detalle al abrirse. */
let workbarEstado = { activo: null, cola: [], interactivo: [],
                      consultas: { n: 0, nombres: [] }, recientes: [] };

function workbarAbierta() {
  return localStorage.getItem(_WORKBAR_PREF) !== '0';
}

function toggleWorkbar() {
  const abierta = !workbarAbierta();
  localStorage.setItem(_WORKBAR_PREF, abierta ? '1' : '0');
  _aplicarEstadoWorkbar();
  if (abierta) refrescarWorkbar();
}

function _aplicarEstadoWorkbar() {
  const el = document.getElementById('workbar');
  const btn = document.getElementById('workbar-toggle');
  if (!el) return;
  const abierta = workbarAbierta();
  el.classList.toggle('collapsed', !abierta);
  // La pestaña de la columna vive en #tab-bar y la columna en #app-body, así
  // que el estado no cuelga de ningún ancestro común más cercano que <body>.
  // Un solo escritor —esta línea— y dos lectores en el CSS.
  document.body.classList.toggle('workbar-plegada', !abierta);
  if (btn) {
    btn.textContent = abierta ? '›' : '‹';
    btn.setAttribute('aria-expanded', abierta ? 'true' : 'false');
  }
}


// ── El filtro de la columna ─────────────────────────────────────────────────
// Buscador y pills viven en el HTML, FUERA de #workbar-body: el cuerpo se
// repinta entero con cada poll (2 s), así que un input ahí dentro perdería el
// foco y el cursor mientras se escribe. Por lo mismo el valor se lee del DOM
// en vez de guardarse en una variable: el input ES el estado, y así no hay dos
// copias que puedan discrepar.

let _workbarFiltroTab = 'all';

function onWorkbarPill(el) {
  document.querySelectorAll('#workbar .wb-pill')
    .forEach(b => b.classList.toggle('active', b === el));
  _workbarFiltroTab = el.dataset.tab || 'all';
  _workbarRender(workbarEstado);
}

function filtrarWorkbar() { _workbarRender(workbarEstado); }

function _workbarBusqueda() {
  const v = document.getElementById('workbar-search')?.value || '';
  return normalizeSearch(v);
}

function _workbarFiltrando() {
  return _workbarFiltroTab !== 'all' || !!_workbarBusqueda();
}

/** ¿Pasa este trabajo el pill de pestaña y el buscador? */
function _workbarPasaFiltro(t) {
  if (!t) return false;
  if (_workbarFiltroTab !== 'all' && (t.tab || '') !== _workbarFiltroTab) return false;
  const q = _workbarBusqueda();
  return !q || normalizeSearch(t.que || '').includes(q);
}

/** "6 min" · "45 s" · "1 h 12 min" — la misma escala en toda la columna. */
/** Un reloj que corre en el NAVEGADOR, anclado al último dato del servidor.
 *
 *  El transcurrido venía del contrato y solo se refrescaba con el poll —cada
 *  2 s en la columna, 1,5 s en el modal—, así que por debajo del minuto se
 *  veía saltar de dos en dos segundos. La timeline de CMv4.0 no tenía ese
 *  problema porque su reloj lo lleva un tick local de 1 s; esto es lo mismo
 *  para el resto.
 *
 *  El ancla se recalcula en cada render, así que el navegador interpola pero
 *  el servidor sigue mandando: no puede desviarse.
 */
function _relojHTML(segundos, prefijo = '', sufijo = '', clase = '') {
  const desde = Date.now() - Math.max(0, segundos || 0) * 1000;
  return `<span class="workbar-reloj ${clase}" data-desde="${desde}"`
       + ` data-pre="${escHtml(prefijo)}" data-post="${escHtml(sufijo)}">`
       + `${escHtml(prefijo + _workbarTiempo(segundos) + sufijo)}</span>`;
}

function _arrancarRelojes() {
  if (window._workbarRelojTick) return;
  window._workbarRelojTick = setInterval(() => {
    document.querySelectorAll('.workbar-reloj[data-desde]').forEach(el => {
      const desde = parseInt(el.dataset.desde, 10);
      if (!desde) return;
      el.textContent = (el.dataset.pre || '')
        + _workbarTiempo((Date.now() - desde) / 1000)
        + (el.dataset.post || '');
    });
  }, 1000);
}

function _workbarTiempo(segundos) {
  const s = Math.max(0, Math.round(segundos || 0));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

// ── La tarjeta ──────────────────────────────────────────────────────────────
//
// UNA para las cuatro secciones. Antes había tres modelos de interacción en la
// misma columna —el activo con sus botones siempre puestos, «En paralelo»
// igual, y solo los recientes seleccionables, y encima con las acciones en un
// bloque HERMANO que empujaba la lista— así que nada se parecía a nada.
//
// El modelo es el de la `session-card` de los tres sidebars, que es el que
// funciona: se selecciona con un clic y las acciones aparecen DENTRO, tras un
// separador. La única excepción es el trabajo en curso, que sale desplegado
// mientras no se seleccione otra cosa: es lo que se mira, y esconder su
// «Cancelar» detrás de un clic sería peor que la uniformidad que gana.

/** La miniatura: la carátula si la hay, y si no el icono del tipo.
 *
 *  Los dos se pintan siempre, uno encima del otro: si la imagen no carga —una
 *  URL de TMDb caducada, el NAS sin salida a internet— el `onerror` la quita y
 *  debajo sigue estando el icono. Un hueco gris no diría de qué es la fila.
 */
function _workbarMini(t) {
  return `<div class="wb-mini">`
    + iconoDeTrabajo(t.tipo, t.tab, 'icono-chip-sm')
    + (t.poster ? `<img src="${escHtml(t.poster)}" alt=""
        onerror="this.remove()">` : '')
    + `</div>`;
}

/** Qué se le está haciendo, sin repetir el nombre de la película.
 *
 *  `que` es la línea entera («Conversión a MKV · Drive (2011)») y `titulo` la
 *  película. La tarjeta las enseña en dos renglones, así que aquí se quita la
 *  cola para no decir lo mismo dos veces.
 */
function _workbarDescripcion(t) {
  const que = t.que || '';
  const cola = ` · ${t.titulo || ''}`;
  return (t.titulo && que.endsWith(cola)) ? que.slice(0, -cola.length) : que;
}

/** Un punto por fase: dónde va el conjunto, de un vistazo.
 *
 *  La barra es del PROCESO completo (un turno de cola es el proyecto entero),
 *  así que sin esto no se veía por qué fase iba: los puntos lo dicen sin
 *  ocupar una línea de texto. Sirven para cualquier trabajo con fases — el
 *  rip tiene cuatro y una fase CMv4.0 siete.
 */
function _workbarPips(a) {
  if (!a.fases_total || a.fases_total < 2) return '';
  const p = [];
  for (let i = 1; i <= a.fases_total; i++) {
    p.push(`<span class="wb-pip${i < a.fase_n ? ' hecha'
                                : i === a.fase_n ? ' ahora' : ''}"></span>`);
  }
  return `<div class="wb-pips" data-tooltip="Fase ${a.fase_n || '–'} de `
       + `${a.fases_total}">${p.join('')}</div>`;
}

/** Lo que distingue a este trabajo de los otros cinco, en dos palabras.
 *
 *  Lo dice el ADAPTADOR de cada tipo, no esta función: la columna no sabe qué
 *  es un rip ni una conversión, y ese es justo el punto. Aquí solo se pintan.
 */
function _workbarChips(t) {
  const c = t.chips || [];
  if (!c.length) return '';
  return `<div class="wb-chips">`
       + c.map(x => `<span class="wb-chip">${escHtml(x)}</span>`).join('')
       + `</div>`;
}

/** El armazón común. `o` decide qué secciones del cuerpo salen. */
function _workbarTarjeta(t, o) {
  const sel = _workbarSeleccion === o.ref
              || (o.pordefecto && _workbarSeleccion === null);
  // Sin `titulo` —una entrada de una cola persistida de antes, o un trabajo
  // sin película que reconocer— el renglón de arriba ya lleva la línea
  // entera, así que el de abajo diría exactamente lo mismo.
  const titulo = t.titulo || t.que || '';
  const sub = (o.sub === titulo) ? '' : (o.sub || '');
  const acciones = (sel && o.acciones)
    ? `<div class="wb-card-acciones">${o.acciones}</div>` : '';
  return `
    <div class="wb-card wb-tab-${escHtml(t.tab || '')}${sel ? ' selected' : ''}`
      + `${o.clase ? ' ' + o.clase : ''}" data-ref="${escHtml(o.ref)}"
         data-clave="${escHtml(t.id || '')}"
         onclick="seleccionarTrabajo('${escHtml(o.ref)}')">
      <div class="wb-card-fila">
        ${_workbarMini(t)}
        <div class="wb-card-txt">
          <div class="wb-card-titulo">${escHtml(titulo)}</div>
          ${sub ? `<div class="wb-card-sub">${escHtml(sub)}</div>` : ''}
          ${o.paso ? `<div class="wb-card-paso">${escHtml(o.paso)}</div>` : ''}
          ${o.aviso ? `<div class="wb-card-error">${escHtml(o.aviso)}</div>` : ''}
          ${_workbarChips(t)}
        </div>
        <div class="wb-card-der">
          ${o.estado || ''}
          ${o.meta ? `<span class="wb-card-meta">${o.meta}</span>` : ''}
          ${o.meta2 ? `<span class="wb-card-meta tenue">${o.meta2}</span>` : ''}
        </div>
      </div>
      ${o.cuerpo || ''}
      ${acciones}
    </div>`;
}

/** La tarjeta del trabajo que corre ahora: la única con barra y ETA. */
function _workbarActivoHTML(a) {
  // `pct_medido` distingue una barra real de un hueco. Sin evidencia se pinta
  // una barra indeterminada en vez de un número: es la regla del proyecto —
  // una cifra inventada con pinta de dato es peor que no decir nada.
  const barra = a.pct_medido
    ? `<div class="workbar-barra"><div class="workbar-barra-fill" style="width:${a.pct}%"></div></div>`
    : `<div class="workbar-barra indeterminada"><div class="workbar-barra-fill"></div></div>`;
  const izq = a.pct_medido ? `${a.pct} %` : 'Progreso no medible';
  // El ETA se marca cuando es una extrapolación y no una medida, para que el
  // usuario sepa cuánto fiarse.
  const der = a.eta_s != null
    ? _relojHTML(a.segundos, '', ` · Restante ${_workbarTiempo(a.eta_s)}`
        + (a.eta_fuente === 'modelo' ? ' (aprox.)' : ''))
    : _relojHTML(a.segundos, 'Lleva ');
  const fase = a.fases_total
    ? `${a.fase_label || a.fase} · ${a.fase_n || '–'} de ${a.fases_total}`
    : (a.fase_label || a.fase || _workbarDescripcion(a));
  return _workbarTarjeta(a, {
    ref: 'act', clase: 'wb-activa', pordefecto: true,
    sub: fase, paso: a.paso,
    estado: iconoDeEstado('corriendo', 'icono-chip-sm'),
    cuerpo: _workbarPips(a) + barra + `
        <div class="workbar-tiempos">
          <span>${escHtml(izq)}</span>
          <span>${der}</span>
        </div>`,
    acciones: `
      <button class="btn btn-ghost btn-xs" onclick="event.stopPropagation();abrirDetalleDeTrabajo()"
        data-tooltip="Ver el detalle y el registro de la ejecución">Detalle</button>
      ${a.cancelable ? `<button class="btn btn-ghost btn-xs"
        onclick="event.stopPropagation();cancelarTrabajoActivo()"
        data-tooltip="Detener este trabajo">Cancelar</button>` : ''}`,
  });
}

// Qué tarjeta está seleccionada, de cualquiera de las cuatro secciones. La
// referencia lleva el prefijo de la sección porque un trabajo puede estar a la
// vez en el historial y en la cola —una sesión re-ejecutada— y son dos
// tarjetas distintas. La de un reciente añade `inicio`: la misma clave con la
// que se borra, porque una sesión re-ejecutada deja varias líneas con el mismo
// id y es lo único que las distingue.
//
// `null` significa «ninguna», y entonces sale desplegada la del trabajo en
// curso: es lo que se está mirando.
let _workbarSeleccion = null;

// Cómo acabó, dicho para el usuario.
const _CMV40_FIN = {
  done: 'Terminado', cancelled: 'Cancelado', error: 'Terminado con error',
  esperando: 'Requiere una decisión',
};

function _workbarRefReciente(r) {
  return `rec:${r.id || ''}|${r.inicio || ''}`;
}

function _workbarRecientePor(ref) {
  return (workbarEstado.recientes || [])
    .find(r => _workbarRefReciente(r) === ref) || null;
}

/** Despliega o repliega las acciones de una tarjeta. */
function seleccionarTrabajo(ref) {
  _workbarSeleccion = (_workbarSeleccion === ref) ? null : ref;
  _workbarRender(workbarEstado);
}

/** Abre el MISMO modal de detalle que mientras se ejecutaba.
 *
 *  El `detalle` no viene en la línea del historial —eso lo resuelve el
 *  adaptador de la cola, y aquí no hay cola— así que se deriva del tipo. Y las
 *  vistas leen su propia sesión, no el contrato de progreso, que es lo que
 *  hace que sigan teniendo algo que enseñar cuando ya no corre nada.
 */
function abrirDetalleDeReciente(ref) {
  const r = _workbarRecientePor(ref);
  if (!r) { showToast('Ese trabajo ya no está en la lista', 'info'); return; }
  const detalle = _DETALLE_POR_TIPO[r.tipo] || r.tipo;
  if (!_workbarDetalles[detalle]) {
    showToast('Este trabajo no conserva ningún detalle', 'info');
    return;
  }
  _trabajoModalAbrir({
    id: r.id, sobre: r.id, tab: r.tab, tipo: r.tipo, que: r.que,
    detalle, fase: '', fase_label: '',
    fase_n: 0, fases_total: 0, pct: null, pct_medido: false,
    segundos: Math.round(r.segundos || 0), eta_s: null, cancelable: false,
    // Lo que distingue mirar un trabajo TERMINADO de uno en curso: no hay
    // nada que seguir, así que ni se poltea ni se anima ni se habla de fases
    // que vengan. Lo que se enseña es la última foto.
    // Uno que espera decisión NO se abre en modo «última foto»: su vista
    // tiene que ofrecer las salidas, no un resumen de lo que pasó.
    terminal: r.estado !== 'esperando',
    historial: r, paso: _CMV40_FIN[r.estado] || 'Terminado',
  });
}

/** Quita la entrada de la lista. NO toca el proyecto ni el MKV. */
function borrarReciente(ref) {
  const r = _workbarRecientePor(ref);
  if (!r) return;
  showConfirm(
    '¿Quitar del historial?',
    `Se borra la línea de «${r.que}». El proyecto y el MKV no se tocan, y `
    + 'el historial del propio proyecto se conserva.',
    async () => {
      const q = `id=${encodeURIComponent(r.id || '')}`
              + `&inicio=${encodeURIComponent(r.inicio || '')}`;
      const ok = await apiFetch(`/api/historial?${q}`, { method: 'DELETE' });
      if (ok) showToast('Quitado del historial', 'info');
      _workbarSeleccion = null;
      _workbarCargarHistorial();
    },
    'Sí, quitarla');
}

function _workbarListaHTML(titulo, items, render, clase = '', pie = '') {
  if (!items.length && !pie) return '';
  return `
    <div class="workbar-seccion">
      <div class="workbar-seccion-titulo">${escHtml(titulo)}</div>
      <div class="${clase}">${items.map(render).join('')}</div>
      ${pie}
    </div>`;
}

/** Las consultas: contadas, no listadas.
 *
 *  Lo que dura lo que la petición —abrir un MKV, analizar un disco, un
 *  borrado— no lleva tarjeta: el usuario lo tiene delante en su modal con la
 *  barra, así que pintarlo se lo repite, y los de 0-3 s parpadean contra un
 *  poll de 2 s. Lo que sí hace falta decir es CUÁNTAS hay, porque son la
 *  respuesta a «¿por qué va tan lento esto?» — una consulta le cuesta a un
 *  trabajo largo un +15 % medido. Los nombres van en el tooltip.
 */
function _workbarConsultasHTML(c) {
  const n = (c && c.n) || 0;
  if (!n) return '';
  const nombres = (c.nombres || []).join(' · ');
  return `<div class="workbar-consultas">`
       + `<span data-tooltip="${escHtml(nombres)}">`
       + `+ ${n} consulta${n === 1 ? '' : 's'} en curso</span></div>`;
}

function _workbarRender(st) {
  const body = document.getElementById('workbar-body');
  const cuenta = document.getElementById('workbar-count');
  if (!body) return;
  // El contador cuenta TODO, nunca lo filtrado: es el indicador de «hay
  // trabajo» y es lo que lleva la tira plegada. Si el filtro lo apagara,
  // buscar una película haría desaparecer el aviso de que algo está corriendo.
  const total = (st.activo ? 1 : 0) + st.cola.length + st.interactivo.length;
  const activo = _workbarPasaFiltro(st.activo) ? st.activo : null;
  const cola = (st.cola || []).filter(_workbarPasaFiltro);
  const paralelo = (st.interactivo || []).filter(_workbarPasaFiltro);
  if (cuenta) cuenta.textContent = String(total);
  // La tira plegada: el contador va en el propio botón, para que cerrar la
  // columna no te deje sin saber que hay algo en marcha.
  const btn = document.getElementById('workbar-toggle');
  if (btn) {
    btn.classList.toggle('con-trabajo', total > 0);
    btn.dataset.tooltip = total
      ? `${total} trabajo${total === 1 ? '' : 's'} en curso — abrir la columna`
      : 'Mostrar u ocultar la columna de trabajo';
  }

  const enPantalla = (activo ? 1 : 0) + cola.length + paralelo.length;
  // Con la búsqueda puesta tampoco: no es un resultado, y el usuario está
  // mirando otra cosa.
  const pieConsultas = (enPantalla && !_workbarFiltrando())
    ? _workbarConsultasHTML(st.consultas) : '';
  if (!enPantalla) {
    _workbarConservandoElScroll(body, `<div class="workbar-vacio">${
      _workbarFiltrando() ? 'Nada en ejecución coincide con el filtro'
                          : 'No hay nada en ejecución'}</div>`);
    _workbarRenderHistorial();
    return;
  }

  _workbarConservandoElScroll(body,
    // Envuelta en su sección como las otras tres: eso le da el título «En
    // curso» y los 14 px de aire a los lados. Sin el envoltorio la tarjeta
    // caía pegada al borde de la ventana y al de la columna.
    _workbarListaHTML('En curso', activo ? [activo] : [], _workbarActivoHTML)
    // Lo interactivo no tiene fases ni barra: corre en paralelo porque el
    // usuario está delante. Se lista para que se entienda por qué el NAS va
    // cargado, sin darle la prominencia del trabajo diferido.
    // Contexto de la carga, no un trabajo que seguir: se dice cuando hay algo
    // a lo que pueda estar ralentizando. Sola no aporta —el usuario tiene su
    // modal delante— y encendería la columna por abrir un MKV. Por eso
    // tampoco entra en el contador de la tira plegada.
    + _workbarListaHTML('En segundo plano', paralelo, t => _workbarTarjeta(t, {
        ref: `par:${t.id}`,
        sub: _workbarDescripcion(t),
        estado: iconoDeEstado('corriendo', 'icono-chip-sm'),
        meta: _relojHTML(t.segundos),
        acciones: (t.detalle || t.cancelable) ? `
          ${t.detalle ? `<button class="btn btn-ghost btn-xs"
            onclick="event.stopPropagation();abrirDetalleDeTrabajo('${escHtml(t.id)}')"
            data-tooltip="Ver el detalle de este trabajo">Detalle</button>` : ''}
          ${t.cancelable ? `<button class="btn btn-ghost btn-xs"
            onclick="event.stopPropagation();cancelarTrabajoInteractivo('${escHtml(t.id)}')"
            data-tooltip="Detener este trabajo">Cancelar</button>` : ''}` : '',
      }), '', pieConsultas)
    + _workbarListaHTML('Esperando turno', cola, j => _workbarTarjeta(j, {
        ref: `cola:${j.id}`,
        sub: _workbarDescripcion(j),
        estado: iconoDeEstado('en_cola', 'icono-chip-sm'),
        meta: `<span class="workbar-item-pos">${j.posicion}</span>`,
        acciones: `
          <button class="btn btn-ghost btn-xs"
            onclick="event.stopPropagation();quitarDeLaCola('${escHtml(j.id)}')"
            data-tooltip="Sacarlo de la cola. El proyecto no se toca.">Quitar de la cola</button>`,
      }), 'workbar-seccion-cola')
  );
  _instalarReordenDeCola();
  _workbarRenderHistorial();
}


// ── El historial ────────────────────────────────────────────────────────────
//
// Vive en su propio contenedor y se carga por su cuenta, no con el poll. Los
// dos motivos son de comportamiento, no de estética:
//
//  · **Se repinta a otro ritmo.** Con el resto, cada 2 s: bajar por él era
//    imposible —volvía al principio en la vuelta siguiente— y las carátulas
//    se volvían a decodificar en cada una. El scroll, en cambio, es el de la
//    columna entera: las cuatro secciones van una detrás de otra.
//  · **Se puede consultar entero.** Antes eran cinco entradas fijas y el
//    endpoint solo servía ocho; `GET /api/historial` da hasta mil.
//
// Se recarga cuando cambia lo que está en marcha —ahí es cuando aparece una
// línea nueva— y cuando el usuario pide más.

const _WORKBAR_HISTORIAL_PASO = 25;
let _workbarTopeHistorial = _WORKBAR_HISTORIAL_PASO;
let _workbarHayMasHistorial = false;

async function _workbarCargarHistorial(rev) {
  const r = await apiFetch(`/api/historial?limite=${_workbarTopeHistorial}`,
                           { silent: true }).catch(() => null);
  if (!r || !Array.isArray(r.trabajos)) return;   // se conserva lo anterior
  // La revisión se apunta al CARGAR, no al pedir: si la petición falla, la
  // vuelta siguiente lo reintenta en vez de quedarse con el historial viejo
  // hasta el próximo cambio.
  if (rev !== undefined) _workbarUltimaRevHistorial = rev;
  // Si vino justo el tope pedido, es que puede haber más.
  _workbarHayMasHistorial = r.trabajos.length >= _workbarTopeHistorial;
  workbarEstado.recientes = r.trabajos;
  _workbarRenderHistorial();
}

function verMasHistorial() {
  _workbarTopeHistorial += _WORKBAR_HISTORIAL_PASO;
  _workbarCargarHistorial();
}

/** «Hoy» · «Ayer» · «8 sep» — de qué día es una línea. */
function _workbarDia(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || isNaN(d)) return '';
  const hoy = new Date();
  const soloDia = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const dias = Math.round((soloDia(hoy) - soloDia(d)) / 86400000);
  if (dias === 0) return 'Hoy';
  if (dias === 1) return 'Ayer';
  return d.toLocaleDateString('es-ES', { day: 'numeric', month: 'short' });
}

/** «hace 12 min». Cuándo pasó, que la duración no lo dice. */
function _workbarHace(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || isNaN(d)) return '';
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 90) return 'hace un momento';
  return `hace ${_workbarTiempo(s)}`;
}

function _workbarRenderHistorial() {
  const caja = document.getElementById('workbar-historial');
  if (!caja) return;
  const items = (workbarEstado.recientes || []).filter(_workbarPasaFiltro);
  if (!items.length) {
    caja.innerHTML = _workbarFiltrando()
      ? '<div class="workbar-vacio">Nada terminado coincide con el filtro</div>'
      : '';
    return;
  }
  // Con la clase, sin estilo inline: es la misma cabecera que las otras tres
  // y tiene que pegarse igual al bajar. El margen negativo NO se le aplica
  // —no vive dentro de una `.workbar-seccion`— y por eso su padding lateral
  // sale ya de la clase.
  let html = '<div class="workbar-seccion-titulo">Recientes</div>';
  let dia = null;
  for (const r of items) {
    const d = _workbarDia(r.inicio);
    if (d !== dia) {
      dia = d;
      html += `<div class="wb-dia">${escHtml(d)}</div>`;
    }
    html += `<div class="workbar-seccion">${_workbarTarjetaReciente(r)}</div>`;
  }
  if (_workbarHayMasHistorial) {
    html += `<button class="wb-vermas" onclick="verMasHistorial()"
      data-tooltip="Carga ${_WORKBAR_HISTORIAL_PASO} más">Ver más</button>`;
  }
  _workbarConservandoElScroll(caja, html);
}

/** Cambia el contenido de una zona sin mover el scroll de la columna.
 *
 *  El scroll es del contenedor padre, así que reemplazar el HTML de una zona
 *  lo arrastra: mientras la zona está vacía el navegador recorta el
 *  `scrollTop` al nuevo máximo y ya no vuelve. Al usuario le saltaba al
 *  principio cada vez que un trabajo cambiaba de fase — el cuerpo se repinta
 *  cada 2 s.
 */
function _workbarConservandoElScroll(caja, html) {
  const scroller = document.getElementById('workbar-scroll');
  const y = scroller ? scroller.scrollTop : 0;
  caja.innerHTML = html;
  if (scroller && scroller.scrollTop !== y) scroller.scrollTop = y;
}

function _workbarTarjetaReciente(r) {
  const ref = _workbarRefReciente(r);
  const espera = r.estado === 'esperando';
  const cuando = _workbarHace(r.fin || r.inicio);
  return _workbarTarjeta(r, {
    ref, clase: espera ? 'wb-espera' : '',
    sub: _workbarDescripcion(r),
    // El motivo estaba guardado y no se enseñaba en ninguna parte: había que
    // abrir el detalle para saber por qué había fallado algo. Y una
    // cancelación cuenta el suyo igual que un fallo — es el mismo final
    // abrupto, y de los cinco tipos solo el análisis extendido lo decía.
    aviso: (r.estado === 'error' || r.estado === 'cancelled')
             ? (r.error || '').split('\n')[0] : '',
    estado: iconoDeEstado({ done: 'hecho', cancelled: 'cancelado',
                            esperando: 'esperando' }[r.estado] || 'error',
                          'icono-chip-sm'),
    // Cuánto duró y cuándo fue, en dos renglones: en uno solo el texto se
    // come el ancho del título, que es lo primero que hay que poder leer.
    meta: espera ? '<span class="workbar-espera">Requiere decisión</span>'
                 : escHtml(_workbarTiempo(r.segundos)),
    meta2: espera ? '' : escHtml(cuando),
    acciones: `
      <button class="btn ${espera ? 'btn-primary' : 'btn-ghost'} btn-xs"
        onclick="event.stopPropagation();abrirDetalleDeReciente('${escHtml(ref)}')"
        data-tooltip="${espera
          ? 'Abrir para decidir qué hacer con este proyecto'
          : 'Ver el detalle y el registro de esta ejecución'}">${
        espera ? 'Decidir' : 'Detalle'}</button>
      <button class="btn btn-ghost btn-xs"
        onclick="event.stopPropagation();borrarReciente('${escHtml(ref)}')"
        data-tooltip="Quitarlo de la lista. NO borra el proyecto ni el MKV.">Quitar</button>`,
  });
}


/** Arrastrar para reordenar la cola.
 *
 *  Lo hacía el panel «Trabajos en Curso» y se perdió al retirarlo. Sortable ya
 *  está cargado (lo usa el mismo Tab 1 para las pistas), así que es enganchar
 *  la lista y mandar el orden.
 *
 *  Se reordena la cola ENTERA, no solo los rips: `reorder` mueve por clave y
 *  conserva al final lo que no se mencione, así que arrastrar aquí no puede
 *  perder nada.
 */
function _instalarReordenDeCola() {
  const lista = document.querySelector('#workbar-body .workbar-seccion-cola');
  if (!lista || typeof Sortable === 'undefined') return;
  if (lista._sortable) lista._sortable.destroy();
  lista._sortable = Sortable.create(lista, {
    animation: 140,
    onEnd: async () => {
      const orden = [...lista.querySelectorAll('[data-clave]')]
        .map(el => el.dataset.clave);
      await apiFetch('/api/queue/reorder', {
        method: 'POST', body: JSON.stringify({ ordered_ids: orden }),
      });
      refrescarWorkbar();
    },
  });
}

// Quién quiere enterarse de que el trabajo cambió. Lo usan las listas de las
// tres pestañas para repintar sus insignias, y SOLO cuando hay algo que
// repintar: el tick es cada 2 s y volver a montar tres listas en cada vuelta
// costaría más que la petición.
const _workbarOyentes = [];

function alCambiarTrabajos(fn) { _workbarOyentes.push(fn); }

function _workbarFirma(st) {
  const a = st.activo;
  return [a ? `${a.sobre || a.id}:${a.fase || ''}` : '',
          ...(st.cola || []).map(j => j.sobre || j.id)].join('|');
}

let _workbarUltimaFirma = null;
// La revisión del historial con la que se pintó la lista de recientes.
// `null` fuerza la primera carga.
let _workbarUltimaRevHistorial = null;

async function refrescarWorkbar() {
  // `recientes=0`: el historial no viaja con el poll. Se carga aparte y solo
  // cuando cambia, que es lo que dice `historial_rev` — ver más abajo.
  const st = await apiFetch('/api/trabajos?recientes=0', { silent: true })
    .catch(() => null);
  // Un fallo de red NO se interpreta como "no hay nada": se conserva lo
  // último bueno. Vaciar la columna haría creer que el trabajo terminó.
  if (st && Array.isArray(st.cola)) {
    // El historial NO viene en la respuesta: se conserva el ya cargado. Se
    // compone un objeto NUEVO en vez de escribir dentro de `st`, que es lo
    // que acaba de llegar del servidor — mutarlo es un acoplamiento que no
    // hace falta y que en el arnés de las capturas se veía: el fake devolvía
    // el mismo objeto en cada petición y la app le vaciaba el historial.
    workbarEstado = { ...st, recientes: workbarEstado.recientes || [] };
  }
  // El historial se recarga cuando el historial cambia, y eso lo dice el
  // servidor con un contador.
  //
  // La señal anterior era «cambió lo que está en marcha», que solo acierta
  // con las líneas NUEVAS. Una ya escrita que se resuelve no mueve nada: al
  // contestar «mantener el MKV» el pre-flight pasa a terminado, pero si lo
  // que corría seguía corriendo, la tarjeta se quedaba pidiendo una decisión
  // ya tomada — y ofreciendo el botón de decidirla.
  if (st && typeof st.historial_rev === 'number'
      && st.historial_rev !== _workbarUltimaRevHistorial) {
    _workbarCargarHistorial(st.historial_rev);
  }
  _workbarRender(workbarEstado);
  const firma = _workbarFirma(workbarEstado);
  if (firma !== _workbarUltimaFirma) {
    _workbarUltimaFirma = firma;
    for (const fn of _workbarOyentes) {
      try { fn(workbarEstado); } catch (e) { console.error(e); }
    }
  }
}

/** Qué trabajo hay sobre un recurso: `null`, o `{estado, posicion, trabajo}`.
 *
 *  La columna dice lo que pasa en toda la app, pero cada pestaña necesita
 *  marcarlo en SU lista: sin esto, un MKV con el análisis extendido corriendo
 *  se veía igual que uno parado y se podía volver a pedir —el backend lo
 *  rechazaba, que es la peor forma de enterarse.
 *
 *  `sobre` es el identificador con el que la pestaña conoce el recurso: el
 *  session id de un rip o de un proyecto CMv4.0, la ruta del MKV en un
 *  análisis extendido. NO es la clave del trabajo (la de un análisis es su
 *  `audit_id`, que la pestaña no conoce).
 */
function trabajoSobre(sobre) {
  if (!sobre) return null;
  const a = workbarEstado.activo;
  if (a && (a.sobre || a.id) === sobre) {
    return { estado: 'corriendo', posicion: 0, trabajo: a };
  }
  const enCola = (workbarEstado.cola || [])
    .find(j => (j.sobre || j.id) === sobre);
  if (enCola) {
    return { estado: 'en_cola', posicion: enCola.posicion, trabajo: enCola };
  }
  return null;
}

/** El distintivo de la lista de una pestaña: rueda girando o puesto en cola. */
function insigniaDeTrabajo(sobre) {
  const t = trabajoSobre(sobre);
  if (!t) return '';
  if (t.estado === 'corriendo') {
    return `<span class="insignia-trabajo corriendo"
      data-tooltip="${escHtml(t.trabajo.que || 'En ejecución')}">`
      + iconoDeEstado('corriendo', 'icono-chip-sm') + 'En curso</span>';
  }
  return `<span class="insignia-trabajo en-cola"
    data-tooltip="${escHtml(t.trabajo.que || 'Esperando turno')}">`
    + iconoDeEstado('en_cola', 'icono-chip-sm') + `Cola · ${t.posicion}ª</span>`;
}

function _workbarTick() {
  // Con la pestaña oculta no hay nada que pintar, y esto es tráfico cada 2 s
  // contra un NAS que además está procesando vídeo. Al volver,
  // `visibilitychange` dispara la recuperación.
  //
  // Plegada SÍ se pollea: la tira estrecha lleva el contador, y es lo que
  // permite retirar los puntos verdes de las pestañas — si la columna dejara
  // de saber nada al plegarse, cerrarla te dejaría sin ninguna señal de que
  // hay trabajo. La petición se responde desde memoria.
  if (document.hidden) return;
  refrescarWorkbar();
}

function arrancarWorkbar() {
  _aplicarEstadoWorkbar();
  _arrancarRelojes();
  if (_workbarTimer) clearInterval(_workbarTimer);
  _workbarTimer = setInterval(_workbarTick, _WORKBAR_INTERVALO_MS);
  refrescarWorkbar();
}

// ── El detalle ───────────────────────────────────────────────────────────────
// La columna no sabe qué es un rip ni una fase CMv4.0, así que tampoco sabe
// abrir su detalle: cada pestaña registra el suyo. Es el mismo patrón que los
// adaptadores del backend, y por el mismo motivo — añadir un tipo de trabajo
// no puede obligar a tocar esta pieza.
const _workbarDetalles = {};

/** Registra quién sabe enseñar el detalle de un tipo de trabajo. */
function registrarDetalleDeTrabajo(clave, fn) {
  _workbarDetalles[clave] = fn;
}

/** Abre el detalle de un trabajo.
 *
 *  `ref` dice CUÁL: la cadena `sobre` del recurso, o `{tipo}` cuando solo hay
 *  uno de esa clase. Sin `ref` se abre el activo, que es lo que quiere el
 *  botón de la columna.
 *
 *  Los cuatro sitios que lo llaman tras lanzar algo SÍ pasan el suyo. Antes no,
 *  y como lo recién lanzado está en la COLA y no activo, el modal se abría
 *  sobre el trabajo que estuviera corriendo — el de otro. Visto en el NAS: al
 *  cancelar un job saltaba solo el modal del siguiente.
 */
function abrirDetalleDeTrabajo(ref) {
  const coincide = (j) => {
    if (!j) return false;
    if (typeof ref === 'string') return (j.sobre || j.id) === ref;
    return j.tipo === ref.tipo;
  };
  let a = workbarEstado.activo;
  if (ref) {
    a = coincide(workbarEstado.activo) ? workbarEstado.activo
      : (workbarEstado.cola || []).find(coincide)
      || (workbarEstado.interactivo || []).find(coincide) || null;
    // Sin encontrarlo NO se abre otro: enseñar el trabajo de al lado es peor
    // que no enseñar ninguno.
    if (!a) {
      showToast('Ese trabajo ya no está en ejecución', 'info');
      return;
    }
  }
  if (!a) {
    showToast('No hay ningún trabajo en ejecución', 'info');
    return;
  }
  // Una entrada de la cola no trae los campos de progreso: se completan con
  // los del contrato vacío para que el armazón no tenga que comprobarlos.
  const trabajo = a.detalle ? a : {
    ...a, pct: null, pct_medido: false, segundos: 0, eta_s: null,
    fase_n: 0, fases_total: 0, cancelable: true,
    detalle: _DETALLE_POR_TIPO[a.tipo] || a.tipo,
    paso: a.posicion ? `Esperando turno · ${a.posicion}º de la cola` : '',
  };
  if (!_workbarDetalles[trabajo.detalle]) {
    showToast('Este trabajo no tiene una vista de detalle', 'info');
    return;
  }
  _trabajoModalAbrir(trabajo);
}

// Una entrada de la cola solo trae `tipo`; el `detalle` lo pone el adaptador,
// que aún no ha corrido porque el trabajo no ha empezado.
const _DETALLE_POR_TIPO = {
  rip: 'rip', crear_serie: 'serie', fase_cmv40: 'cmv40',
  analisis_extendido: 'analisis_extendido', copia_biblioteca: 'copia_biblioteca',
};

/** Cancela el trabajo activo, sea del tipo que sea.
 *
 *  Cada pestaña tiene su endpoint de cancelación y los tres hacen ya las dos
 *  cosas —matar el proceso y sacarlo de la cola—, así que aquí basta con
 *  elegir cuál. Se pregunta antes: cancelar un remux de 10 minutos por un clic
 *  de más duele.
 */
/** Cancela un trabajo de la lista «En paralelo».
 *
 *  Va por el mismo camino que el activo —cada pestaña tiene su endpoint— pero
 *  hay que buscarlo ahí: `workbarEstado.activo` es lo DIFERIDO que corre, y lo
 *  interactivo por definición no lo es.
 */
function cancelarTrabajoInteractivo(ref) {
  const t = (workbarEstado.interactivo || [])
    .find(x => (x.sobre || x.id) === ref);
  if (!t) { showToast('Ese trabajo ya no está en ejecución', 'info'); return; }
  cancelarTrabajoActivo(t);
}

function cancelarTrabajoActivo(trabajo) {
  // El trabajo llega por parámetro cuando se pulsa desde el modal, que sabe a
  // cuál está mirando. Antes SIEMPRE se leía el activo del último poll: si
  // ese hueco caía entre dos fases, la función se iba de vacío con un `return`
  // mudo — sin petición, sin toast, sin nada en el log del servidor. Es lo que
  // se veía como «no me deja cancelar».
  const a = trabajo || _trabajoModalUltimo || workbarEstado.activo;
  if (!a) {
    showToast('No hay ningún trabajo que cancelar', 'info');
    return;
  }
  // El análisis extendido NO se cancela con un POST a pelo: su función manda
  // el `audit_id` que se está siguiendo y marca la sesión local como cancelada
  // por el usuario. Sin eso, un cancel tardío del audit A mataba el B recién
  // lanzado, y el error salía como fallo en vez de como cancelación — dos bugs
  // que costaron su tarde.
  const acciones = {
    rip:   () => apiFetch(`/api/sessions/${a.id}/cancel`, { method: 'POST' }),
    cmv40: async () => {
      await apiFetch(`/api/cmv40/${a.id}/cancel`, { method: 'POST' });
      // Sin esto el poller del auto-pipeline vuelve a arrancar la cadena a los
      // pocos segundos — visto en el NAS: cancelas la Fase A y salta el
      // pre-flight otra vez.
      if (typeof cmv40TrasCancelar === 'function') cmv40TrasCancelar(a.id);
    },
    mkv:   () => (a.detalle === 'analisis_extendido'
                    ? _mkvQualityCancel(a.id) : cancelMkvApply()),
  };
  const accion = acciones[a.tab];
  if (!accion) {
    showToast(`No hay acción de cancelación para un trabajo de «${a.tab || '?'}»`, 'error');
    return;
  }
  showConfirm(
    '¿Detener el trabajo?',
    `Se detendrá «${a.que}». Lo que ya esté hecho se conserva.`,
    async () => {
      await accion();
      // El overlay desaparecía al llegar la fase a terminal; cancelar es
      // terminal. Dejarlo abierto «en modo cancelado» obliga a cerrarlo a
      // mano para ver el proyecto que hay debajo.
      cerrarModalDeTrabajo();
      refrescarWorkbar();
    },
    'Sí, detenerlo',
  );
}

// ── El armazón del modal ─────────────────────────────────────────────────────
// Uno para los cinco tipos. Cada uno registra una función que devuelve qué
// poner: icono, subtítulo, la tira de fases y el cuerpo. El armazón no sabe de
// ninguno, que es lo que permite añadir un tipo sin tocarlo.
//
// El detalle NO es siempre un log: el rip, la fase CMv4.0 y el análisis
// extendido producen uno, pero la copia y la creación de una serie no — ahí el
// detalle son bytes y episodios. Un log vacío sería peor que decirlo.

let _trabajoModalTimer = null;
let _trabajoModalTipo = null;
let _trabajoModalRef = null;      // `sobre` del trabajo que se está mirando
let _trabajoModalUltimo = null;   // su último progreso conocido
let _trabajoModalSinActivo = 0;   // refrescos seguidos sin sujeto
let _trabajoModalVista = null;    // la última vista con contenido


/** Una cartela a partir del `tmdb_info` que ya tiene la pestaña.
 *
 *  Las tres pestañas guardan el mismo dict (ver `services/tmdb.py`), así que
 *  la traducción a cartela se hace UNA vez. Sin match de TMDb sigue habiendo
 *  cartela: el nombre del fichero limpio dice bastante más que nada.
 */
function cartelDeTmdb(tmdb, nombreFallback, icono) {
  const t = tmdb || null;
  let titulo = t?.title || '';
  if (!titulo && nombreFallback) {
    titulo = String(nombreFallback).replace(/\.mkv$/i, '').replace(/[._]+/g, ' ');
  }
  if (!titulo) return null;
  const partes = [];
  if (t?.year) partes.push(String(t.year));
  if (t?.runtime_minutes) {
    partes.push(`${Math.floor(t.runtime_minutes / 60)}h ${t.runtime_minutes % 60}min`);
  }
  if (t?.genres?.length) partes.push(t.genres.slice(0, 2).join(' · '));
  return { url: t?.poster_url || '', titulo, meta: partes.join(' · '),
           icono: icono || '🎬' };
}

/** La cartela: póster, título largo y ficha (año · duración · géneros).
 *
 *  La trae la vista de cada tipo, no el armazón: sacarla del backend obligaría
 *  a meter TMDb en el contrato de progreso, y quien tiene el `tmdb_info`
 *  delante es la pestaña, que ya lo pide para su panel.
 */
function _trabajoCartelPinta(cartel) {
  const caja = document.getElementById('trabajo-modal-cartel');
  if (!caja) return;
  caja.style.display = cartel ? '' : 'none';
  if (!cartel) return;
  const poster = document.getElementById('trabajo-modal-cartel-poster');
  if (poster) {
    poster.innerHTML = cartel.url
      ? `<img src="${escHtml(cartel.url)}" alt="" loading="lazy">`
      : (cartel.icono || '🎬');
  }
  const t = document.getElementById('trabajo-modal-cartel-titulo');
  if (t) {
    t.textContent = cartel.titulo || '';
    t.dataset.tooltip = cartel.titulo || '';
  }
  const m = document.getElementById('trabajo-modal-cartel-meta');
  if (m) m.textContent = cartel.meta || '';
}

/** La timeline de fases, con el MISMO marcado que la de CMv4.0.
 *
 *  No se parece a ella: **es** ella. Usa sus clases (`cmv40-tl-*`), así que
 *  hereda el raíl que conecta las fases, el resalte de la activa, el punto que
 *  late y los colores de completada / omitida / pendiente. Una versión propia
 *  se veía distinta en la misma aplicación, que es justo lo que este modal
 *  vino a arreglar.
 *
 *  Cada paso puede ser una cadena o `{titulo, sub, icono, nota}`.
 */
function timelineDeTrabajo(pasos, a, titulo) {
  if (!pasos || !pasos.length) return '';
  const total = pasos.length;
  // Un trabajo TERMINADO no tiene fase en curso: o pasaron todas, o se paró
  // donde se parara. `fase_n` describe el presente y en una línea del
  // historial vale 0, así que sin esta rama la columna salía entera en gris,
  // como si el trabajo no hubiera hecho nada.
  const term = !!a.terminal;
  const todoHecho = term && (a.historial || {}).estado === 'done';
  const filas = pasos.map((p, i) => {
    const n = i + 1;
    const paso = typeof p === 'string' ? { titulo: p } : (p || {});
    // La fila puede fijar su estado: el rip sabe por su historial de ejecución
    // cuáles corrieron, incluso en una cancelada a medias.
    const estado = paso.estado ? paso.estado
                 : todoHecho ? 'done'
                 : a.fase_n && n < a.fase_n ? 'done'
                 : (!term && a.fase_n === n) ? 'running'
                 : 'pending';
    const nota = paso.nota || (estado === 'done' ? 'completado'
                             : estado === 'running' ? 'en curso…'
                             : term ? 'no llegó a ejecutarse' : '');
    const icono = {
      done:    '<span class="cmv40-tl-status-icon done">✓</span>',
      running: '<span class="cmv40-tl-status-icon running"></span>',
      pending: '<span class="cmv40-tl-status-icon pending"></span>',
    }[estado];
    const html = `<li class="cmv40-tl-step cmv40-tl-${estado}" data-step-key="p${n}">
      <div class="cmv40-tl-rail">${icono}</div>
      <div class="cmv40-tl-body">
        <div class="cmv40-tl-title">
          ${paso.icono ? `<span class="cmv40-tl-phase-icon">${paso.icono}</span>` : ''}
          <span>${escHtml(paso.titulo || '')}</span>
        </div>
        ${paso.sub ? `<div class="cmv40-tl-what">${escHtml(paso.sub)}</div>` : ''}
        ${nota ? `<span class="cmv40-tl-eta ${estado}">${escHtml(nota)}</span>` : ''}
      </div>
    </li>`;
    return { html, hecha: estado === 'done' };
  });
  // El contador sale de las filas, no de `fase_n`: así cuenta igual en un
  // trabajo vivo y en uno terminado, sin dos maneras de calcular lo mismo.
  const hechas = filas.filter(f => f.hecha).length;
  const pct = a.pct_medido ? a.pct
            : total ? Math.round((hechas / total) * 100) : 0;
  const restante = term ? '' : (a.eta_s != null
    ? `Restante ${_workbarTiempo(a.eta_s)}`
      + (a.eta_fuente === 'modelo' ? ' (aprox.)' : '')
    : '');
  return `
    <aside class="cmv40-running-timeline">
      <div class="cmv40-tl-header">
        <div class="cmv40-tl-header-top">
          <span class="cmv40-tl-trust-badge pending">${escHtml(titulo || 'Fases')}</span>
        </div>
        <div class="cmv40-tl-progress">
          <div class="cmv40-tl-progress-meta">
            <span class="cmv40-tl-timer">
              <span class="cmv40-tl-timer-icon">⏱</span>
              ${term
                ? `<span class="cmv40-tl-timer-elapsed">${escHtml(_workbarTiempo(a.segundos))}</span>`
                : _relojHTML(a.segundos, '', '', 'cmv40-tl-timer-elapsed')}
            </span>
            <span class="cmv40-tl-progress-pct">${hechas}/${total} · ${pct}%</span>
            <span class="cmv40-tl-timer-remaining">${escHtml(restante)}</span>
          </div>
          <div class="cmv40-tl-progress-track">
            <div class="cmv40-tl-progress-fill" style="width:${pct}%"></div>
          </div>
        </div>
      </div>
      <ol class="cmv40-tl-steps">${filas.map(f => f.html).join('')}</ol>
    </aside>`;
}

function _trabajoModalPinta(a, vista) {
  const set = (id, txt) => {
    const el = document.getElementById(id);
    if (el) el.textContent = txt;
  };
  const iconoEl = document.getElementById('trabajo-modal-icono');
  if (iconoEl) {
    // Mientras hay trabajo, el aro que gira del overlay (`cmv40-running-spinner`,
    // 28 px, `cmv40-spin` a 0,8 s). El chip del tipo es estático y ese
    // movimiento es la señal de que la cosa sigue viva. Parado, el chip.
    const enMarcha = !a.terminal && a.cancelable !== false;
    iconoEl.className = enMarcha ? 'cmv40-running-spinner' : 'modal-icon';
    iconoEl.innerHTML = enMarcha ? ''
      : a.terminal
      ? iconoDeEstado({ done: 'hecho', cancelled: 'cancelado' }[
          (a.historial || {}).estado] || 'error', 'icono-chip-lg')
      : iconoDeTrabajo(a.tipo, a.tab, 'icono-chip-lg');
  }
  // La cabecera dice QUÉ está pasando. El nombre del fichero no va aquí: lo
  // enseña la cartela de la columna, y repetirlo dejaba tres líneas con el
  // mismo título (cartela + nombre de salida + nombre de origen).
  set('trabajo-modal-titulo',
      (vista.autoTag || '') + (a.fase_label || vista.titulo || a.que || 'Trabajo'));
  set('trabajo-modal-sub', vista.sub || '');
  _trabajoCartelPinta(vista.cartel);
  // La tira horizontal se retiró: las fases van SIEMPRE en la columna. Los
  // tipos que solo aportan una lista de nombres se la fabrica el armazón.
  const lateral = vista.lateral
    || timelineDeTrabajo(vista.pasos, a, vista.pasosTitulo);

  // **El bloque de progreso de aquí abajo —barra, paso, %, restante— es el de
  // la FASE, no el del trabajo.** El total va arriba a la izquierda, bajo la
  // cartela, y en la tarjeta de la columna; aquí, pegado al log, lo que se
  // quiere saber es cuánto le queda a lo que se está leyendo: durante veinte
  // minutos de demux el porcentaje del proyecto entero apenas se mueve.
  //
  // `fase_progreso` solo lo llena quien tiene dos niveles de verdad —CMv4.0,
  // cuyo turno de cola son las siete fases—. Los demás caen a los campos de
  // arriba, que para ellos son la misma cosa.
  const fase = a.fase_progreso || a;
  // La barra sigue la misma regla que en la columna: sin porcentaje medido no
  // se pinta una que avanza.
  const wrap = document.getElementById('trabajo-modal-barra-wrap');
  const fill = document.getElementById('trabajo-modal-barra');
  if (wrap && fill) {
    // La barra pertenece a este bloque, así que también es la de la fase.
    wrap.classList.toggle('indeterminada', !a.terminal && !fase.pct_medido);
    fill.style.width = a.terminal ? '100%' : (fase.pct_medido ? `${fase.pct}%` : '');
    wrap.classList.toggle('terminada', !!a.terminal);
  }
  // El PASO dentro de la fase. Sin él la barra dice cuánto queda pero no de
  // qué: diez minutos de demux se ven igual que diez de merge.
  set('trabajo-modal-paso', a.paso || vista.paso || a.fase_label || 'Preparando…');
  set('trabajo-modal-pct', a.terminal ? '' : (fase.pct_medido ? `${fase.pct}%` : '—'));
  set('trabajo-modal-eta', a.terminal ? '' : (fase.eta_s != null
    ? `Restante ${_workbarTiempo(fase.eta_s)}`
      + (fase.eta_fuente === 'modelo' ? ' (aprox.)' : '')
    : ''));
  const tiemposEl = document.getElementById('trabajo-modal-tiempos');
  if (tiemposEl) {
    // Terminado el reloj se para: es un dato, no un contador. Y al terminar
    // lo que interesa es lo que costó el TRABAJO, no la última fase.
    tiemposEl.innerHTML = a.terminal
      ? (a.segundos ? `Duró ${escHtml(_workbarTiempo(a.segundos))}` : '')
      : (fase.segundos ? _relojHTML(fase.segundos, 'Lleva ') : '');
  }

  const cuerpo = document.getElementById('trabajo-modal-cuerpo');
  if (cuerpo) {
    // El log es un directo: interesa el final. Pero solo se baja si el usuario
    // YA estaba abajo — si ha subido a leer algo, el refresco cada dos
    // segundos no puede arrastrarlo de vuelta.
    const ancla = anclajeDeLog(cuerpo.querySelector('.cmv40-log'));
    cuerpo.innerHTML = vista.cuerpo || '';
    restaurarAnclajeDeLog(cuerpo.querySelector('.cmv40-log'), ancla);
  }
  // La columna izquierda la rellena el tipo. Vacía, el CSS la esconde y el
  // modal se queda a una columna — no todos los trabajos tienen una timeline
  // que enseñar.
  const timeline = document.getElementById('trabajo-modal-timeline');
  if (timeline) {
    // **NO se reescribe si no ha cambiado.** Reemplazar el innerHTML cada 1,5 s
    // (a) devuelve el scroll al principio en cuanto el usuario lo mueve, y
    // (b) reinicia la animación del icono de la fase en curso, que por eso se
    // veía parado. Un tipo con timeline propia —CMv4.0— pasa una FUNCIÓN y la
    // actualiza en sitio, que es lo que ya hacía su overlay.
    if (typeof lateral === 'function') {
      lateral(timeline);
    } else if (timeline.dataset.pintado !== lateral) {
      timeline.innerHTML = lateral;
      timeline.dataset.pintado = lateral;
    }
  }
  document.querySelector('.trabajo-modal-caja')
    ?.classList.toggle('sin-lateral', !lateral && !vista.cartel);
  // El botón de copiar solo tiene sentido con log delante.
  const copiar = document.getElementById('trabajo-modal-copiar');
  if (copiar) copiar.style.display = vista.conLog ? '' : 'none';
  const cancelar = document.getElementById('trabajo-modal-cancelar');
  if (cancelar) cancelar.style.display = a.cancelable ? '' : 'none';
}

// Por qué no hay registro que enseñar. Son casos distintos y decirlos como
// uno solo es contarle al usuario algo que no ha pasado: un proyecto borrado
// no es «un estado que se sustituye».
const _MOTIVO_SIN_LOG = {
  borrado: 'El proyecto ya no existe: su registro se borró con él. La línea '
         + 'del historial es lo que queda.',
  efimero: 'El registro de esta ejecución no se conserva: su estado es de un '
         + 'solo trabajo a la vez y lo sustituye el siguiente.',
  desconocido: 'No hay registro guardado de esta ejecución.',
};

/** Completa la vista de un trabajo terminado con lo que el historial sabe.
 *
 *  Las cinco vistas leen su propia sesión, y tres de ellas la conservan (el
 *  rip, la fase CMv4.0). Las dos de Tab 2 no: su estado es un singleton que se
 *  resetea con el siguiente trabajo, así que al abrir una ejecución vieja
 *  devolvían un cuerpo vacío. Antes que un modal en blanco, lo que sí consta.
 */
function _trabajoModalConResumen(a, vista) {
  const h = a.historial || {};
  if (vista.cuerpo && !/detalle-vacio/.test(vista.cuerpo)) return vista;
  const fecha = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d) ? '—' : d.toLocaleString('es-ES',
      { day: '2-digit', month: '2-digit', year: '2-digit',
        hour: '2-digit', minute: '2-digit' });
  };
  return {
    ...vista,
    conLog: false,
    cuerpo: _trabajoKvHTML([
      ['Resultado', _CMV40_FIN[h.estado] || h.estado || '—'],
      ['Empezó', fecha(h.inicio)],
      ['Terminó', fecha(h.fin)],
      ['Duración', _workbarTiempo(h.segundos || a.segundos)],
      ['Error', h.error || '—'],
    ]) + `<div class="trabajo-detalle-nota">${escHtml(_MOTIVO_SIN_LOG[
      vista.sinDetalle] || _MOTIVO_SIN_LOG.desconocido)}</div>`,
  };
}

/** Dónde estaba mirando el usuario un log, para poder devolverlo ahí.
 *
 *  Repintar un log lo manda al PRINCIPIO: mientras el contenido se sustituye
 *  la caja se queda vacía, el navegador recorta el `scrollTop` al nuevo máximo
 *  —cero— y ya no vuelve. Como el modal se repinta cada 1,5 s, leer una línea
 *  de la mitad era imposible: en cuanto soltabas, saltaba arriba.
 *
 *  Se guardan las dos cosas porque son dos comportamientos distintos: si
 *  estaba pegado al fondo hay que MANTENERLO pegado (el log es un directo y
 *  llegan líneas nuevas), y si no, dejarlo donde estaba. Las líneas se añaden
 *  al final, así que la posición desde arriba sigue apuntando a lo mismo.
 */
function anclajeDeLog(el) {
  if (!el) return { y: 0, abajo: true };
  return {
    y: el.scrollTop,
    abajo: el.scrollTop + el.clientHeight >= el.scrollHeight - 24,
  };
}

/** Devuelve el log a donde estaba. `el` puede ser otro elemento: el modal
 *  reemplaza el cuerpo entero y el log se recrea. */
function restaurarAnclajeDeLog(el, ancla) {
  if (!el || !ancla) return;
  el.scrollTop = ancla.abajo ? el.scrollHeight : ancla.y;
}

/** Un log con la paleta semántica de la app (marcadores ━━━ / $ / ✓ / ✗). */
function _trabajoLogHTML(lineas) {
  if (!lineas || !lineas.length) {
    return '<div class="trabajo-detalle-vacio">Todavía no hay líneas de log</div>';
  }
  // Con la paleta semántica de siempre (`log-phase`, `log-success`, `log-error`
  // …). Se clasifica con la MISMA función que el panel de Tab 3: pintarlo en
  // gris plano hacía ilegible un log de dos mil líneas donde lo único que se
  // busca es el ✗ o el separador de fase.
  const clase = typeof _classifyLogLine === 'function'
    ? _classifyLogLine : () => '';
  return `<div class="cmv40-log" id="trabajo-modal-log">`
    + lineas.slice(-400).map(l => {
        const t = String(l);
        return `<div class="log-line ${clase(t)}">${escHtml(t)}</div>`;
      }).join('')
    + '</div>';
}

/** Para los trabajos que no producen log: pares clave/valor. */
function _trabajoKvHTML(pares) {
  return '<dl class="trabajo-kv">'
    + pares.filter(([, v]) => v !== undefined && v !== null && v !== '')
           .map(([k, v]) => `<dt>${escHtml(k)}</dt><dd>${escHtml(String(v))}</dd>`)
           .join('')
    + '</dl>';
}

function cerrarModalDeTrabajo() {
  if (_trabajoModalTimer) { clearInterval(_trabajoModalTimer); _trabajoModalTimer = null; }
  _trabajoModalTipo = null;
  _trabajoModalRef = null;
  _trabajoModalUltimo = null;
  _trabajoModalVista = null;
  const tl = document.getElementById('trabajo-modal-timeline');
  if (tl) { tl.innerHTML = ''; delete tl.dataset.pintado; }
  closeModal('trabajo-modal');
}

/** Abre el modal para el trabajo activo y lo mantiene al día. */
/** Un ciclo de refresco del modal abierto.
 *
 *  Sale del `_trabajoModalAbrir` para poder ejecutarla en un test: el bug que
 *  arregla —el modal vaciándose en el hueco entre dos fases— solo se ve
 *  encadenando varios refrescos, y desde dentro de una closure con su
 *  `setInterval` no hay forma de provocarlo.
 */
async function _trabajoModalRefrescar() {
  const fn = _workbarDetalles[_trabajoModalTipo];
  if (!fn || !_trabajoModalUltimo) return;
  // Un trabajo terminado no se sigue: se pinta su última foto y se para el
  // reloj. Sin esto entraba en la rama de «cambiando de fase» —pensada para
  // el hueco entre dos fases de un job vivo— y salía animado, con el botón de
  // cancelar puesto y polleando veinte veces algo que ya no cambia.
  if (_trabajoModalUltimo.terminal) {
    const base = _trabajoModalUltimo;
    try {
      const vista = (await fn(base)) || {};
      _trabajoModalPinta(base, _trabajoModalConResumen(base, vista));
    } catch (e) { console.error('[trabajo-modal]', e); }
    if (_trabajoModalTimer) { clearInterval(_trabajoModalTimer); _trabajoModalTimer = null; }
    return;
  }
  const act = workbarEstado.activo;
  const esElMismo = act && (act.sobre || act.id) === _trabajoModalRef;
  if (esElMismo) { _trabajoModalUltimo = act; _trabajoModalSinActivo = 0; }
  else _trabajoModalSinActivo += 1;
  // Aunque el trabajo ya no esté activo se SIGUE pintando su vista: la
  // timeline, el estado de las fases y el log los lee cada pestaña de su
  // propia sesión, no del contrato de progreso. Lo único que deja de tener
  // sentido es la barra.
  // Entre dos fases de un mismo proyecto la cola se queda sin `running` un
  // instante, así que «no hay activo» NO significa «terminó»: significa eso
  // durante unos segundos, o mientras el trabajo siga esperando turno.
  const enCola = (workbarEstado.cola || [])
    .some(j => (j.sobre || j.id) === _trabajoModalRef);
  const enTransito = enCola || _trabajoModalSinActivo <= 8;
  const base = esElMismo ? act : {
    ..._trabajoModalUltimo,
    pct: null, pct_medido: false, eta_s: null, cancelable: enTransito,
    paso: enTransito ? 'Cambiando de fase…' : 'Terminado',
  };
  try {
    const vista = await fn(base);
    // Una vista vacía NO sustituye a la anterior. Las cinco piden su estado al
    // backend y se lo tragan con `.catch(() => null)`, así que un GET lento
    // durante una fase pesada devolvía todo en blanco: se veía cómo el modal
    // perdía la columna, la cartela y el log durante un minuto y luego volvía.
    const hayAlgo = vista && (vista.lateral || vista.cuerpo || vista.cartel);
    if (hayAlgo) _trabajoModalVista = vista;
    _trabajoModalPinta(base, hayAlgo ? vista : (_trabajoModalVista || vista || {}));
  } catch (e) {
    console.error('[trabajo-modal]', e);
  }
  // Se deja de pollear cuando lleva un rato sin sujeto, pero el contenido se
  // queda: cerrarlo es del usuario.
  if (_trabajoModalSinActivo > 20 && _trabajoModalTimer) {
    clearInterval(_trabajoModalTimer);
    _trabajoModalTimer = null;
  }
}

async function _trabajoModalAbrir(a) {
  const fn = _workbarDetalles[a.detalle];
  if (!fn) return;
  // Un tipo con modal PROPIO —el pre-flight— lo abre él y devuelve null; el
  // armazón no monta el suyo encima. Es un caso, no una familia: montar un
  // segundo registro para él sería abstracción para un solo uso.
  if (await fn(a) === null) return;
  _trabajoModalTipo = a.detalle;
  // El modal se ancla al TRABAJO, no al tipo. Un proyecto CMv4.0 encadena
  // siete fases y entre una y la siguiente el contrato deja de traer activo un
  // instante; comparando el tipo, el modal se daba por terminado, se sustituía
  // por un armazón vacío —sin la columna de fases y con «Todavía no hay líneas
  // de log»— y APAGABA su propio timer, así que no se recuperaba nunca.
  _trabajoModalRef = a.sobre || a.id;
  _trabajoModalUltimo = a;
  _trabajoModalSinActivo = 0;
  openModal('trabajo-modal');
  await _trabajoModalRefrescar();
  if (_trabajoModalTimer) clearInterval(_trabajoModalTimer);
  _trabajoModalTimer = setInterval(_trabajoModalRefrescar, 1500);
}

// ── Iconos ───────────────────────────────────────────────────────────────────
// SVG en línea, no emoji. Los emoji los dibuja el sistema operativo: cambian de
// forma y de color entre máquinas, no heredan la paleta y a un ⏳ o un ⬜ no hay
// manera de quitarles el aire de chat. Un `<svg>` con `currentColor` sí hereda,
// se anima con CSS y pesa lo mismo que un carácter.
//
// El trazo es de 1.6 con extremos redondeados sobre una rejilla de 24, que es
// lo que hace que se lean como una familia — el mismo criterio de Material
// Symbols en su variante `outlined`. El color va en un chip: fondo con la
// variante `-dim` de la paleta y trazo con la sólida, que es de donde sale el
// aspecto pastel sin inventar colores nuevos.

/** Envuelve un `path` en el `<svg>` común. Todo comparte rejilla y trazo. */
function _svg(cuerpo, extra = '') {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
    aria-hidden="true"${extra}>${cuerpo}</svg>`;
}

/** El TONO lo da la PESTAÑA, no el tipo.
 *
 *  Antes cada tipo tenía el suyo y no seguía ninguna regla: el rip azul y la
 *  serie morada siendo las dos de Tab 1, el análisis y la copia turquesas por
 *  casualidad. Mirando la columna no se sabía de dónde venía cada cosa.
 *
 *  No hay tabla que mantener: todo trabajo lleva ya su `tab` en el contrato,
 *  en la cola y en el historial, así que el color sale de ahí y no se puede
 *  desincronizar de nada.
 */
const _TONO_POR_TAB = { rip: 'azul', mkv: 'turquesa', cmv40: 'naranja' };

/** Por TIPO de trabajo: el glifo dice QUÉ se está haciendo. */
const _GLIFOS_TRABAJO = {
  // Disco: dos círculos concéntricos, como el `album` de Material.
  rip: _svg('<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="2.5"/>'),
  // Pantalla con antena: una serie de televisión.
  crear_serie: _svg('<rect x="3" y="7.5" width="18" height="12.5" rx="2"/>'
                  + '<path d="m8 3.5 4 4 4-4"/>'),
  // Lupa sobre una onda: analizar la señal, no "buscar un fichero".
  analisis_extendido: _svg('<circle cx="10.5" cy="10.5" r="6.5"/>'
                         + '<path d="m20 20-4.6-4.6"/>'
                         + '<path d="M8 10v1.5M10.5 8v5M13 9.5v2.5"/>'),
  // Flecha entrando en una bandeja: copiar hacia Output.
  copia_biblioteca: _svg('<path d="M4 14.5V18a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3.5"/>'
                       + '<path d="M12 3.5v10m0 0 3.5-3.5M12 13.5 8.5 10"/>'),
  // Escudo con visto: la validación previa, que decide SI va a haber trabajo.
  preflight: _svg('<path d="M12 3.2 5.5 6v6c0 4 2.8 7 6.5 8.8'
                + ' 3.7-1.8 6.5-4.8 6.5-8.8V6z"/>'
                + '<path d="m9.2 12.1 2 2 3.6-4"/>'),
  // Destellos: el upgrade de metadata, sin tocar la imagen.
  fase_cmv40: _svg('<path d="m11 3.5 1.7 4.3 4.3 1.7-4.3 1.7L11 15.5 9.3 11.2 5 9.5l4.3-1.7z"/>'
                 + '<path d="m18 15 .8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>'),
};

/** Por ESTADO: dice en qué punto está. */
const _ICONOS_ESTADO = {
  // Arco abierto que gira. Sustituye al ⏳: un reloj de arena sugiere que hay
  // que esperar sin hacer nada, y esto sugiere que algo se mueve.
  corriendo: ['verde', _svg('<circle cx="12" cy="12" r="8.5" stroke-dasharray="40 14"/>',
                            ' class="icono-girando"')],
  // Reloj, no reloj de arena: es "le toca a las y cuarto", no "aguanta".
  en_cola: ['gris', _svg('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 1.8"/>')],
  hecho:   ['verde', _svg('<circle cx="12" cy="12" r="8.5"/><path d="m8.2 12.2 2.6 2.6 5-5.6"/>')],
  error:   ['rojo',  _svg('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.8v4.6"/>'
                        + '<path d="M12 16.1h.01"/>')],
  // En rojo, como el error: pararlo a medias es un final abrupto y así se
  // lee de un vistazo. En gris se confundía con «en cola» y no contaba nada;
  // el motivo va debajo, igual que el de un fallo.
  cancelado: ['rojo', _svg('<circle cx="12" cy="12" r="8.5"/><path d="M8.2 8.2l7.6 7.6"/>')],
  // Ni hecho ni fallido: terminó su parte y espera una decisión. En ámbar
  // porque hay algo que hacer, con la interrogación que lo dice sin texto.
  esperando: ['naranja', _svg('<circle cx="12" cy="12" r="8.5"/>'
                            + '<path d="M9.9 9.8a2.2 2.2 0 1 1 2.5 2.7v1.1"/>'
                            + '<path d="M12.3 16.4h.01"/>')],
};

/** El chip con su icono. `clase` añade tamaño (`icono-chip-sm`). */
function _chipIcono(par, clase = '') {
  if (!par) return '';
  const [tono, svg] = par;
  return `<span class="icono-chip icono-${tono} ${clase}">${svg}</span>`;
}

function iconoDeTrabajo(tipo, tab = '', clase = '') {
  const glifo = _GLIFOS_TRABAJO[tipo];
  if (!glifo) return '';
  return _chipIcono([_TONO_POR_TAB[tab] || 'gris', glifo], clase);
}

function iconoDeEstado(estado, clase = '') {
  return _chipIcono(_ICONOS_ESTADO[estado], clase);
}


/** Saca un trabajo de la cola antes de que empiece.
 *
 *  Al retirar el panel «Trabajos en Curso» de Tab 1 se fue con él la única
 *  forma de hacer esto, que es una regresión: encolar tres cosas y no poder
 *  quitar la de en medio. `DELETE /api/queue/{clave}` sirve para cualquier
 *  tipo — la cola borra por clave, no por sesión de Tab 1.
 */
async function quitarDeLaCola(clave) {
  const r = await apiFetch(`/api/queue/${encodeURIComponent(clave)}`,
                           { method: 'DELETE' });
  if (r !== null) showToast('Retirado de la cola', 'info');
  refrescarWorkbar();
  if (typeof loadSessions === 'function') loadSessions();
}
