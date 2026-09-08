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
  if (!_workbarDetalles[a.detalle]) {
    showToast('Este trabajo todavía no tiene vista de detalle', 'info');
    return;
  }
  _trabajoModalAbrir(a);
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
  // El análisis extendido NO se cancela con un POST a pelo: su función manda
  // el `audit_id` que se está siguiendo y marca la sesión local como cancelada
  // por el usuario. Sin eso, un cancel tardío del audit A mataba el B recién
  // lanzado, y el error salía como fallo en vez de como cancelación — dos bugs
  // que costaron su tarde.
  const acciones = {
    rip:   () => apiFetch(`/api/sessions/${a.id}/cancel`, { method: 'POST' }),
    cmv40: () => apiFetch(`/api/cmv40/${a.id}/cancel`, { method: 'POST' }),
    mkv:   () => (a.detalle === 'analisis_extendido'
                    ? _mkvQualityCancel() : cancelMkvApply()),
  };
  const accion = acciones[a.tab];
  if (!accion) return;
  showConfirm(
    'Cancelar el trabajo',
    `Se detendrá «${a.que}». Lo que ya esté hecho se conserva.`,
    async () => { await accion(); refrescarWorkbar(); },
    'Cancelar el trabajo',
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

/** Pinta la tira de fases a partir de los campos comunes. */
function _trabajoPasosHTML(a, pasos) {
  if (!pasos || !pasos.length) return '';
  return pasos.map((p, i) => {
    const n = i + 1;
    const clase = a.fase_n && n < a.fase_n ? 'hecha'
                : a.fase_n === n ? 'activa' : '';
    const icono = clase === 'hecha' ? '✓' : clase === 'activa' ? '⏳' : '⬜';
    return `<div class="trabajo-paso ${clase}">${icono} ${escHtml(p)}</div>`;
  }).join('');
}

function _trabajoModalPinta(a, vista) {
  const set = (id, txt) => {
    const el = document.getElementById(id);
    if (el) el.textContent = txt;
  };
  set('trabajo-modal-icono', vista.icono || '⚙︎');
  set('trabajo-modal-titulo', vista.titulo || a.que || 'Trabajo');
  set('trabajo-modal-sub', vista.sub || '');
  const pasos = document.getElementById('trabajo-modal-pasos');
  if (pasos) pasos.innerHTML = _trabajoPasosHTML(a, vista.pasos);

  // La barra sigue la misma regla que en la columna: sin porcentaje medido no
  // se pinta una que avanza.
  const wrap = document.getElementById('trabajo-modal-barra-wrap');
  const fill = document.getElementById('trabajo-modal-barra');
  if (wrap && fill) {
    wrap.classList.toggle('indeterminada', !a.pct_medido);
    fill.style.width = a.pct_medido ? `${a.pct}%` : '';
  }
  set('trabajo-modal-tiempos', '');
  const t = document.getElementById('trabajo-modal-tiempos');
  if (t) {
    t.innerHTML = `<span>${a.pct_medido ? a.pct + '%' : 'sin medir'}</span>`
      + `<span>lleva ${escHtml(_workbarTiempo(a.segundos))}`
      + (a.eta_s != null
          ? ` · quedan ${escHtml(_workbarTiempo(a.eta_s))}`
            + (a.eta_fuente === 'modelo' ? ' (aprox.)' : '')
          : '')
      + '</span>';
  }

  const cuerpo = document.getElementById('trabajo-modal-cuerpo');
  if (cuerpo) cuerpo.innerHTML = vista.cuerpo || '';
  // El botón de copiar solo tiene sentido con log delante.
  const copiar = document.getElementById('trabajo-modal-copiar');
  if (copiar) copiar.style.display = vista.conLog ? '' : 'none';
  const cancelar = document.getElementById('trabajo-modal-cancelar');
  if (cancelar) cancelar.style.display = a.cancelable ? '' : 'none';
}

/** Un log con la paleta semántica de la app (marcadores ━━━ / $ / ✓ / ✗). */
function _trabajoLogHTML(lineas) {
  if (!lineas || !lineas.length) {
    return '<div class="trabajo-detalle-vacio">Todavía no hay líneas de log</div>';
  }
  return `<div class="cmv40-log" id="trabajo-modal-log">`
    + lineas.slice(-400).map(l => `<div>${escHtml(String(l))}</div>`).join('')
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
  closeModal('trabajo-modal');
}

/** Abre el modal para el trabajo activo y lo mantiene al día. */
async function _trabajoModalAbrir(a) {
  const fn = _workbarDetalles[a.detalle];
  if (!fn) return;
  _trabajoModalTipo = a.detalle;
  openModal('trabajo-modal');
  const refrescar = async () => {
    const act = workbarEstado.activo;
    // El trabajo terminó o lo relevó otro: el modal deja de tener sujeto.
    if (!act || act.detalle !== _trabajoModalTipo) {
      _trabajoModalPinta(a, { titulo: a.que, sub: 'Terminado',
                              pasos: [], cuerpo: '' });
      if (_trabajoModalTimer) { clearInterval(_trabajoModalTimer); _trabajoModalTimer = null; }
      return;
    }
    _trabajoModalPinta(act, await fn(act));
  };
  await refrescar();
  if (_trabajoModalTimer) clearInterval(_trabajoModalTimer);
  _trabajoModalTimer = setInterval(refrescar, 1500);
}
