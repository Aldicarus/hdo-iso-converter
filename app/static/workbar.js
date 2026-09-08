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
let workbarEstado = { activo: null, cola: [], interactivo: [], recientes: [] };

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
  if (btn) btn.textContent = abierta ? '›' : '‹';
}

/** "6 min" · "45 s" · "1 h 12 min" — la misma escala en toda la columna. */
function _workbarTiempo(segundos) {
  const s = Math.max(0, Math.round(segundos || 0));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

/** La tarjeta del trabajo que corre ahora. */
function _workbarActivoHTML(a) {
  // `pct_medido` distingue una barra real de un hueco. Sin evidencia se pinta
  // una barra indeterminada en vez de un número: es la regla del proyecto —
  // una cifra inventada con pinta de dato es peor que no decir nada.
  const barra = a.pct_medido
    ? `<div class="workbar-barra"><div class="workbar-barra-fill" style="width:${a.pct}%"></div></div>`
    : `<div class="workbar-barra indeterminada"><div class="workbar-barra-fill"></div></div>`;
  const izq = a.pct_medido ? `${a.pct}%` : 'sin medir';
  // El ETA se marca cuando es una extrapolación y no una medida, para que el
  // usuario sepa cuánto fiarse.
  const der = a.eta_s != null
    ? `quedan ${_workbarTiempo(a.eta_s)}${a.eta_fuente === 'modelo' ? ' (aprox.)' : ''}`
    : `${_workbarTiempo(a.segundos)}`;
  const fase = a.fases_total
    ? `${a.fase_label || a.fase} · ${a.fase_n || '–'}/${a.fases_total}`
    : (a.fase_label || a.fase || '');
  return `
    <div class="workbar-seccion">
      <div class="workbar-seccion-titulo">En curso</div>
      <div class="workbar-activo">
        <div class="workbar-activo-que">${escHtml(a.que || '')}</div>
        <div class="workbar-activo-fase">${escHtml(fase)}</div>
        ${barra}
        <div class="workbar-tiempos">
          <span>${escHtml(izq)}</span>
          <span>${escHtml(a.eta_s != null ? der : 'lleva ' + der)}</span>
        </div>
        <div class="workbar-acciones">
          <button class="btn btn-ghost btn-xs" onclick="abrirDetalleDeTrabajo()"
            data-tooltip="Ver el log y el detalle de la ejecución">Detalle</button>
          ${a.cancelable ? `<button class="btn btn-ghost btn-xs" onclick="cancelarTrabajoActivo()"
            data-tooltip="Detener este trabajo">Cancelar</button>` : ''}
        </div>
      </div>
    </div>`;
}

function _workbarListaHTML(titulo, items, render) {
  if (!items.length) return '';
  return `
    <div class="workbar-seccion">
      <div class="workbar-seccion-titulo">${escHtml(titulo)}</div>
      ${items.map(render).join('')}
    </div>`;
}

function _workbarRender(st) {
  const body = document.getElementById('workbar-body');
  const cuenta = document.getElementById('workbar-count');
  if (!body) return;
  const total = (st.activo ? 1 : 0) + st.cola.length + st.interactivo.length;
  if (cuenta) cuenta.textContent = String(total);
  // La tira plegada: el contador va en el propio botón, para que cerrar la
  // columna no te deje sin saber que hay algo en marcha.
  const btn = document.getElementById('workbar-toggle');
  if (btn) {
    btn.classList.toggle('con-trabajo', total > 0);
    btn.dataset.tooltip = total
      ? `${total} trabajo${total === 1 ? '' : 's'} — abrir la columna`
      : 'Mostrar u ocultar la columna de trabajo';
  }

  if (!total) {
    body.innerHTML = '<div class="workbar-vacio">No hay nada en marcha</div>'
      + _workbarListaHTML('Últimos trabajos', st.recientes.slice(0, 5), r => `
        <div class="workbar-item">
          <span class="workbar-item-que">${escHtml(r.que || '')}</span>
          <span class="workbar-item-meta">${escHtml(_workbarTiempo(r.segundos))}</span>
        </div>`);
    return;
  }

  body.innerHTML =
    (st.activo ? _workbarActivoHTML(st.activo) : '')
    + _workbarListaHTML('Esperando turno', st.cola, j => `
        <div class="workbar-item">
          <span class="workbar-item-pos">${j.posicion}</span>
          <span class="workbar-item-que">${escHtml(j.que || '')}</span>
        </div>`)
    // Lo interactivo no tiene fases ni barra: corre en paralelo porque el
    // usuario está delante. Se lista para que se entienda por qué el NAS va
    // cargado, sin darle la prominencia del trabajo diferido.
    + _workbarListaHTML('En paralelo', st.interactivo, t => `
        <div class="workbar-item">
          <span class="workbar-item-que">${escHtml(t.que || '')}</span>
          <span class="workbar-item-meta">${escHtml(_workbarTiempo(t.segundos))}</span>
        </div>`);
}

async function refrescarWorkbar() {
  const st = await apiFetch('/api/trabajos', { silent: true }).catch(() => null);
  // Un fallo de red NO se interpreta como "no hay nada": se conserva lo
  // último bueno. Vaciar la columna haría creer que el trabajo terminó.
  if (st && Array.isArray(st.cola)) workbarEstado = st;
  _workbarRender(workbarEstado);
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

function abrirDetalleDeTrabajo() {
  const a = workbarEstado.activo;
  if (!a) return;
  const fn = _workbarDetalles[a.detalle];
  if (!fn) {
    showToast('Este trabajo todavía no tiene vista de detalle', 'info');
    return;
  }
  fn(a);
}

/** Cancela el trabajo activo, sea del tipo que sea.
 *
 *  Cada pestaña tiene su endpoint de cancelación y los tres hacen ya las dos
 *  cosas —matar el proceso y sacarlo de la cola—, así que aquí basta con
 *  elegir cuál. Se pregunta antes: cancelar un remux de 10 minutos por un clic
 *  de más duele.
 */
function cancelarTrabajoActivo() {
  const a = workbarEstado.activo;
  if (!a) return;
  const rutas = {
    rip:   `/api/sessions/${a.id}/cancel`,
    cmv40: `/api/cmv40/${a.id}/cancel`,
    mkv:   a.detalle === 'analisis_extendido'
             ? '/api/mkv/quality-audit/cancel' : '/api/mkv/apply/cancel',
  };
  const url = rutas[a.tab];
  if (!url) return;
  showConfirm(
    'Cancelar el trabajo',
    `Se detendrá «${a.que}». Lo que ya esté hecho se conserva.`,
    async () => {
      await apiFetch(url, { method: 'POST', body: JSON.stringify({}) });
      refrescarWorkbar();
    },
    'Cancelar el trabajo',
  );
}
