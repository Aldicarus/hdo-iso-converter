'use strict';

/**
 * @fileoverview HDO Blu-ray Toolkit — Frontend SPA (Fase C de la pipeline)
 *
 * Arquitectura:
 *   - Vanilla JS sin framework ni bundler. Todo el estado vive en `currentSession`.
 *   - La UI tiene tres tabs principales: Crear MKV, Editar MKV, CMv4.0 BD.
 *   - Tab 1 contiene dos pantallas: welcome (sin sesión activa) y
 *     review-screen (Fase C: revisión y edición de la sesión).
 *   - Comunicación con el backend via REST (apiFetch) + WebSocket para streaming
 *     de output en tiempo real durante la ejecución (Fases D y E).
 *
 * Módulos principales:
 *   TooltipManager  — Tooltips flotantes con posicionamiento automático.
 *   PipelineBar     — Barra de pipeline inferior con las 5 fases (A→E).
 *   showToast       — Notificaciones temporales tipo toast (éxito / error / aviso).
 *   showConfirm     — Diálogo de confirmación reutilizable.
 *   apiFetch        — Wrapper de fetch con manejo de errores y Content-Type JSON.
 *   renderSession   — Renderiza la pantalla de revisión completa a partir de una sesión.
 *   connectWebSocketForProject — Conecta al WS del backend para streaming de output.
 *   switchTab       — Gestiona los tres tabs del header.
 */

// ── Tabla de idiomas (inglés → literal en español) ───────────────
const LANGUAGE_MAP = {
  spanish: 'Castellano', english: 'Inglés', french: 'Francés',
  german: 'Alemán', italian: 'Italiano', japanese: 'Japonés',
  portuguese: 'Portugués', chinese: 'Chino', korean: 'Coreano',
  dutch: 'Holandés', russian: 'Ruso', polish: 'Polaco',
  czech: 'Checo', hungarian: 'Húngaro', swedish: 'Sueco',
  norwegian: 'Noruego', danish: 'Danés', finnish: 'Finlandés',
  turkish: 'Turco', arabic: 'Árabe', hebrew: 'Hebreo',
  thai: 'Tailandés', greek: 'Griego', romanian: 'Rumano',
  croatian: 'Croata', slovak: 'Eslovaco', ukrainian: 'Ucraniano',
};

/** Convierte un idioma en inglés (cualquier capitalización) al literal en español. */
function langLiteral(bdInfoLang) {
  if (!bdInfoLang) return '';
  return LANGUAGE_MAP[bdInfoLang.toLowerCase()] || bdInfoLang;
}

// ── Estado global ─────────────────────────────────────────────────

/** Máximo de proyectos abiertos simultáneamente en Tab 1.
 *  Sin límite desde v2.5.0+ — con el soporte de series, un disco puede
 *  producir 10-15 episodios y queremos abrirlos todos como pestañas
 *  consecutivas. El valor Infinity mantiene la estructura del código
 *  (los checks siguen llamándose pero nunca disparan). Tab 3 mantiene
 *  su tope de 5 (MAX_CMV40_PROJECTS) — un job CMv4.0 es mucho más
 *  pesado y no tiene caso de uso multi-episodio. */
const MAX_PROJECTS = Infinity;

/**
 * Proyectos abiertos (sub-tabs de proyecto en Tab 1).
 * @type {Array<{id:string, sessionId:string, session:Object|null, name:string,
 *   isoPath:string, ws:WebSocket|null, sortable:any, sortableAudio:any, sortableSubs:any,
 *   mkvNameWasManual:boolean, activePhaseE:boolean,
 *   executionStartTime:number|null, executionTimer:number|null}>}
 */
const openProjects = [];

/** Sub-tab activo: null (ninguno), 'cola', o el id del proyecto. @type {string|null} */
let activeSubTabId = null;

/** Sesión activa (siempre apunta a activeProject.session). @type {Object|null} */
let currentSession = null;

/** Estado de la cola (actualizado por WS de cola). @type {{running:string|null, queue:string[]}} */
let queueState = { running: null, queue: [] };

/** WebSocket de cola. @type {WebSocket|null} */
let queueWs = null;

/** WebSocket único para la ejecución en curso — alimenta solo el panel Cola. @type {WebSocket|null} */
let executionWs = null;

/** Temporizador standalone del trabajo en curso en la Cola. @type {number|null} */
let _colaExecTimer = null;
/** Timestamp de inicio del trabajo en curso (ms). @type {number|null} */
let _colaExecStart = null;

/** Líneas de log acumuladas del trabajo en curso (para filtrado). @type {string[]} */
let _colaLogLines = [];
/** Filtro activo del log en vivo: 'all' | 'warn'. @type {string} */
let _colaLogFilter = 'all';
/** Timestamps de inicio/fin de cada fase para calcular elapsed y ETA. */
let _pcPhaseStart  = { mount: null, extract: null, unmount: null };
let _pcPhaseEnd    = { mount: null, extract: null, unmount: null };
/** Último porcentaje de progreso reportado por mkvmerge (Fase D). */
let _pcLastPct = 0;
/** IDs de items del historial actualmente expandidos. @type {Set<string>} */
/** IDs de items de la cola actualmente expandidos. @type {Set<string>} */
const _colaQueueExpanded = new Set();

// Tabs (principales)
/** @type {number} Tab activo (1, 2 o 3). */
let currentTab = 1;

// ── Helpers de proyecto ───────────────────────────────────────────

/** Devuelve el proyecto activo o null si el sub-tab activo es 'cola'. */
function getActiveProject() {
  if (activeSubTabId === 'cola') return null;
  return openProjects.find(p => p.id === activeSubTabId) || null;
}

/**
 * Busca un elemento primero en el panel del proyecto activo (prefijo id),
 * luego en el DOM global. Esto permite usar los mismos nombres de ID
 * en funciones compartidas sin romper el aislamiento por proyecto.
 * @param {string} id
 * @returns {HTMLElement|null}
 */
function E(id) {
  if (activeSubTabId && activeSubTabId !== 'cola') {
    const el = document.getElementById(`${activeSubTabId}-${id}`);
    if (el) return el;
  }
  return document.getElementById(id);
}

/** Genera un ID corto único para un proyecto. */
function genProjectId() {
  return Math.random().toString(36).slice(2, 10);
}

// ── Inicialización ────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  TooltipManager.init();
  loadSessions();
  checkAppStatus();
  connectQueueWebSocket();
  switchSubTab(null);
  _installSubtabScrollBindings();
  _installVisibilityRecovery();
  _initUpdateCheckHeader();
  // Auto-detect de operaciones de Tab 2 en curso en el backend tras un
  // refresh de pestaña (caso: usuario cierra navegador con copia activa,
  // reabre y debería ver el modal con el progreso). El check es silencioso
  // — si no hay nada activo, no abre nada.
  setTimeout(() => {
    if (typeof _mkvCheckActiveApply === 'function') _mkvCheckActiveApply();
  }, 500);
  // Polling global de "jobs activos" para los dots verdes en los tabs.
  // Cada 5s pregunta al backend qué tabs tienen actividad y enciende /
  // apaga los indicadores. Coste mínimo (3 endpoints livianos) pero da al
  // usuario visibilidad inmediata de cualquier job, esté o no en el tab
  // del que viene.
  _refreshTabRunningDots();
  setInterval(_refreshTabRunningDots, 5000);
});

/**
 * Refresca los indicadores "punto verde animado" de cada tab principal
 * según el estado real del backend:
 *   - Tab 1: alguna sesión con status='running' o cola con jobs queued.
 *   - Tab 2: _mkv_apply_state.active=true (copia/edición desde Library).
 *   - Tab 3: alguna sesión CMv4.0 con running_phase != null.
 *
 * Silent: estos checks corren en background, sin toasts si fallan. La UI
 * tiene fuentes de verdad redundantes para el estado de cada tab; este
 * indicador es solo un "atajo visual" — no hay riesgo de mostrar info
 * incorrecta y borrarla en el siguiente tick.
 */
async function _refreshTabRunningDots() {
  const setDot = (n, on) => {
    const el = document.getElementById(`tab-running-dot-${n}`);
    if (el) el.style.display = on ? '' : 'none';
  };
  // Tab 1: queueState (ya en memoria, lleno por queueWs) + sesiones
  const t1 = !!(queueState && (queueState.running || (queueState.queue && queueState.queue.length)));
  setDot(1, t1);
  // Tab 2: apply progress
  try {
    const st = await apiFetch('/api/mkv/apply/progress', { silent: true });
    setDot(2, !!(st && st.active));
  } catch (_) { setDot(2, false); }
  // Tab 3: cualquier sesión con running_phase
  try {
    const data = await apiFetch('/api/cmv40', { silent: true });
    const t3 = !!(data?.sessions || []).some(s => s.running_phase);
    setDot(3, t3);
  } catch (_) { setDot(3, false); }
}

/**
 * Tras Mac sleep / cambio de pestaña / suspend de red, los WebSockets
 * mueren y los timers de polling pueden quedarse sin actualizar la UI.
 * Cuando el documento vuelve a ser visible, fuerza un refresh del estado
 * vía API y reconecta los WS si las sesiones siguen corriendo. Cubre
 * los tres tabs:
 *   - Tab 1 (ISO→MKV): sessions list + queue WS + executionWs activo
 *   - Tab 2 (Editar MKV): light-profile chained-await ya es resiliente
 *   - Tab 3 (CMv4.0): proyectos abiertos + WS log por proyecto
 *
 * Sin esto, tras el wake los logs se quedan congelados aunque el job
 * en backend haya terminado correctamente.
 */
function _installVisibilityRecovery() {
  // El recovery se dispara desde 3 fuentes:
  //  1. visibilitychange → visible (cambio de pestaña, foco)
  //  2. focus de la ventana (alt-tab, click en el navegador)
  //  3. online (red vuelve tras pérdida temporal)
  //  4. pageshow con persisted=true (bfcache restore en mobile/desktop)
  //
  // Macos cerrar tapa por <60s NO siempre dispara visibilitychange —
  // depende de la versión de macOS, Chrome y la app. Por eso necesitamos
  // múltiples triggers. Tras 1 dispare, no spamear: dedup con throttle 1s.
  let _lastRecoveryAt = 0;
  const _doRecovery = () => {
    const now = Date.now();
    if (now - _lastRecoveryAt < 1000) return;  // throttle 1s
    _lastRecoveryAt = now;
    if (document.hidden) return;
    _runRecoveryTasks();
  };
  document.addEventListener('visibilitychange', _doRecovery);
  window.addEventListener('focus', _doRecovery);
  window.addEventListener('online', _doRecovery);
  window.addEventListener('pageshow', (e) => { if (e.persisted) _doRecovery(); });
}

/**
 * Ejecuta las tareas de recovery (refresh + reconnect WS) de los 3 tabs.
 * Estrategia AGRESIVA: cierra y reconecta todos los WS sin chequear
 * readyState — un WS zombie tras Mac sleep puede reportar OPEN aunque los
 * datos ya no fluyan, y la red TCP no se entera hasta que un keepalive
 * falla (puede tardar 60-120s). Reconectar es barato (handshake <100ms),
 * preferimos garantizar datos al ahorrar conexión.
 */
function _runRecoveryTasks() {
  // ── Tab 3 — proyectos CMv4.0 abiertos ─────────────────────────
  if (Array.isArray(openCMv40Projects)) {
    for (const project of openCMv40Projects) {
      if (!project || project._closed) continue;
      _refreshCMv40Session(project.id);
      const s = project.session || {};
      // Reconnect AGRESIVO: cierra WS actual sin importar readyState y
      // abre uno nuevo. Si project.ws era zombie tras Mac sleep, esto es
      // lo que destraba el log. Solo reconectamos si la sesión tiene
      // running_phase (sino no hay nada que streamar).
      if (s.running_phase) {
        try { project.ws?.close(); } catch (_) {}
        if (project._wsReconnectTimer) {
          clearTimeout(project._wsReconnectTimer);
          project._wsReconnectTimer = null;
        }
        // Pequeño delay para dejar al ws.onclose handler ejecutarse y
        // limpiar referencias antes de abrir el nuevo.
        setTimeout(() => {
          if (!project._closed) _connectCMv40WebSocket(project);
        }, 50);
      }
    }
  }

  // ── Tab 1 — sessions list + queue + executionWs ──────────────
  if (typeof loadSessions === 'function') {
    try { loadSessions(); } catch (_) {}
  }
  // Queue WS — reconnect agresivo (no chequea readyState).
  if (typeof queueWs !== 'undefined' && queueWs) {
    try { queueWs.close(); } catch (_) {}
  }
  if (typeof connectQueueWebSocket === 'function') {
    try { connectQueueWebSocket(); } catch (_) {}
  }
  // ExecutionWs — si hay un job running en la cola, reconectar siempre.
  if (typeof queueState !== 'undefined' && queueState
      && queueState.running
      && typeof connectExecutionWebSocket === 'function') {
    if (typeof executionWs !== 'undefined' && executionWs) {
      try { executionWs._closedByUser = true; executionWs.close(); } catch (_) {}
    }
    setTimeout(() => connectExecutionWebSocket(queueState.running), 50);
  }
  // Tab 2 — light-profile resilience (sin cambios)
  if (window._dvLightSession?.ctrl) {
    apiFetch('/api/mkv/light-profile/progress', { silent: true }).then(st => {
      if (st && st.active === false && (st.result || st.error)) {
        window._dvLightSession.polledResult = st.result || null;
        try { window._dvLightSession.ctrl?.abort(); } catch (_) {}
      }
    }).catch(() => {});
  }
  // Tab 2 — apply (copia desde Library): si hay job activo en backend,
  // el modal puede estar congelado en "esperando" — forzar tick.
  if (typeof _mkvCheckActiveApply === 'function') {
    _mkvCheckActiveApply();
  }

  // BURST refresh: la red Wi-Fi puede tardar varios segundos en
  // estabilizarse tras un wake del Mac. Un solo refresh inmediato puede
  // caer en una ventana donde la conexión aún está reconectando y el
  // siguiente safety poll está a 4s. Disparamos 3 refreshes adicionales
  // espaciados a 0.5s, 2s y 4s para acelerar el catchup hasta ~3-5s en
  // el peor caso (vs 20s observado sin burst).
  for (const delayMs of [500, 2000, 4000]) {
    setTimeout(() => {
      if (document.hidden) return;  // si se cierra otra vez, abortar
      if (Array.isArray(openCMv40Projects)) {
        for (const project of openCMv40Projects) {
          if (project && !project._closed && project.session?.running_phase) {
            _refreshCMv40Session(project.id);
          }
        }
      }
      if (typeof loadSessions === 'function') {
        try { loadSessions(); } catch (_) {}
      }
    }, delayMs);
  }
}

// ═══════════════════════════════════════════════════════════════════
//  TOOLTIP MANAGER
// ═══════════════════════════════════════════════════════════════════

/**
 * Gestor de tooltips flotantes basado en el atributo `data-tooltip`.
 *
 * Cualquier elemento con `data-tooltip="texto"` muestra automáticamente
 * un tooltip al hacer hover. El posicionamiento se calcula para que el
 * tooltip nunca salga del viewport. Se oculta al hacer scroll o al salir
 * del elemento, con un pequeño debounce de 80 ms para evitar parpadeos.
 *
 * @namespace TooltipManager
 */
const TooltipManager = (() => {
  let el, hideTimer;

  /**
   * Inicializa el gestor. Debe llamarse una vez en DOMContentLoaded.
   * También se llama tras actualizaciones de innerHTML para re-enlazar listeners.
   */
  function init() {
    el = document.getElementById('tooltip');
    document.addEventListener('mouseover', onOver);
    document.addEventListener('mouseout',  onOut);
    document.addEventListener('scroll',    hide, true);
  }

  /**
   * Muestra el tooltip al hacer mouseover sobre un elemento con data-tooltip.
   * @param {MouseEvent} e
   */
  function onOver(e) {
    const target = e.target.closest('[data-tooltip]');
    if (!target) return;
    clearTimeout(hideTimer);
    const text = target.dataset.tooltip;
    if (!text) return;
    el.textContent = text;

    // Posicionar fuera del viewport para medir sin parpadeo
    el.style.top  = '-9999px';
    el.style.left = '-9999px';

    requestAnimationFrame(() => {
      const rect = target.getBoundingClientRect();
      const tw = el.offsetWidth;
      const th = el.offsetHeight;
      const vw = window.innerWidth;
      const vh = window.innerHeight;

      // Flip hacia arriba si no cabe debajo (útil para elementos cerca del borde inferior como la pipeline bar)
      const top = (rect.bottom + 6 + th > vh - 8)
        ? rect.top - th - 6
        : rect.bottom + 6;

      // Corrección para evitar salir del viewport por la derecha
      let left = rect.left;
      if (left + tw > vw - 8) left = Math.max(8, vw - tw - 8);

      el.style.top  = `${top}px`;
      el.style.left = `${left}px`;
      el.classList.add('visible');
    });
  }

  /**
   * Oculta el tooltip con debounce al salir del elemento.
   * @param {MouseEvent} e
   */
  function onOut(e) {
    if (!e.target.closest('[data-tooltip]')) return;
    hideTimer = setTimeout(hide, 80);
  }

  /** Oculta el tooltip inmediatamente. */
  function hide() { el?.classList.remove('visible'); }

  return { init, hide };
})();

// ═══════════════════════════════════════════════════════════════════
//  TAB SWITCHING
// ═══════════════════════════════════════════════════════════════════

/**
 * Cambia el tab activo del header y actualiza sidebar + panel principal.
 * @param {number} n - Número de tab (1, 2 o 3).
 */
function switchTab(n) {
  currentTab = n;

  // Activar por ID, no por posición: el orden visual de los tabs no coincide
  // con su numeración interna (Tab 3 está visualmente en la posición 2).
  [1, 2, 3].forEach(i => {
    const btn = document.getElementById(`tab-btn-${i}`);
    if (btn) btn.classList.toggle('active', i === n);
  });

  // Tab 2 no tiene sidebar — ocultar sidebar y usar ancho completo
  const sidebar = document.getElementById('sidebar');
  if (sidebar) sidebar.style.display = (n === 2) ? 'none' : '';

  [1, 2, 3].forEach(i => {
    const el = document.getElementById(`sidebar-tab-${i}`);
    if (el) el.style.display = i === n ? '' : 'none';
  });
  [1, 2, 3].forEach(i => {
    const el = document.getElementById(`tab-panel-${i}`);
    if (!el) return;
    if (i !== n) { el.style.display = 'none'; return; }
    el.style.display = (i === 1) ? 'flex' : (i === 2) ? 'flex' : (i === 3) ? 'flex' : 'block';
  });

  // Refrescar sidebar Tab 3 al entrar. Reset del flag de auto-resume:
  // cada vez que el usuario entra al Tab 3, volvemos a evaluar si hay un
  // proyecto running para abrirlo automáticamente (1-shot por entrada,
  // no spam si refrescamos varias veces el sidebar).
  if (n === 3 && typeof refreshCMv40Sidebar === 'function') {
    _cmv40AutoResumeAttempted = false;
    refreshCMv40Sidebar();
  }
  // Tab 2: detectar si hay una operación de apply (copia + edición) en
  // curso desde otra sesión del navegador o un refresh de pestaña — si la
  // hay, reabrir el modal de progreso para que el usuario pueda seguirla.
  if (n === 2 && typeof _mkvCheckActiveApply === 'function') {
    _mkvCheckActiveApply();
  }
}

// ═══════════════════════════════════════════════════════════════════
//  SUB-TABS (proyectos dentro de Tab 1)
// ═══════════════════════════════════════════════════════════════════

/**
 * Cambia el sub-tab activo dentro de Tab 1.
 * @param {string} id - 'cola' o project.id
 */
function switchSubTab(id) {
  // Si no hay proyectos abiertos y no se pide Cola, mostrar estado vacío
  if (!id && openProjects.length === 0) id = 'empty';
  activeSubTabId = id;
  document.getElementById('subtab-btn-cola')?.classList.toggle('active', id === 'cola');
  document.querySelectorAll('.subtab-proj').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.pid === id);
  });
  // Mostrar el panel correcto en #subtab-main (Cola, proyecto o estado vacío)
  document.querySelectorAll('#subtab-main .subtab-panel').forEach(panel => {
    let active;
    if (id === 'cola') active = panel.id === 'panel-cola';
    else if (id === 'empty') active = panel.id === 'panel-empty-projects';
    else active = panel.id === `panel-project-${id}`;
    panel.classList.toggle('active-panel', active);
  });
  // Actualizar cortinilla: icono + posición (clase cola-panel-open)
  const expandTab = document.getElementById('cola-expand-tab');
  const icon = document.getElementById('cola-expand-icon');
  if (expandTab) expandTab.classList.toggle('cola-panel-open', id === 'cola');
  document.getElementById('cola-sidebar')?.classList.toggle('cola-panel-open', id === 'cola');
  if (icon) icon.textContent = id === 'cola' ? '▶' : '◀';
  // Scrollbar izquierda cuando Cola está activo
  const main = document.getElementById('subtab-main');
  if (main) {
    main.classList.toggle('cola-scroll-rtl', id === 'cola');
    main.scrollTop = 0;
  }
  if (id === 'cola') renderColaDetailPanel();
  const project = getActiveProject();
  currentSession = project ? project.session : null;
}

/** Toggle cortinilla: muestra/oculta el panel Cola en el área principal. */
function toggleColaSidebar() {
  if (activeSubTabId === 'cola') {
    const lastProject = openProjects[openProjects.length - 1];
    switchSubTab(lastProject ? lastProject.id : null);
  } else {
    switchSubTab('cola');
  }
}

/**
 * Abre o reutiliza un proyecto para una sesión dada.
 * Si el sessionId ya está abierto, activa ese sub-tab.
 * Si no, crea un nuevo sub-tab (máx. 5).
 * @param {Object} session - Objeto sesión completo del backend.
 * @returns {Object} El proyecto (nuevo o existente).
 */
function openProject(session) {
  // ¿Ya está abierto?
  const existing = openProjects.find(p => p.sessionId === session.id);
  if (existing) {
    existing.session = session;
    switchSubTab(existing.id);
    renderProjectPanel(existing);
    return existing;
  }

  if (openProjects.length >= MAX_PROJECTS) {
    showToast(`Máximo ${MAX_PROJECTS} proyectos abiertos. Cierra uno antes de abrir otro.`, 'warning');
    return null;
  }

  const pid  = genProjectId();
  // Para series TV el nombre del tab debe identificar el episodio
  // concreto (todas las sesiones de la misma temporada comparten ISO).
  // Formato Plex/Jellyfin-style: "Serie (Año) - SNNeNN - Título".
  // Pelis siguen con basename del ISO sin extensión.
  let name;
  if (session.media_type === 'series') {
    const sn = String(session.season_number || 0).padStart(2, '0');
    const en = String(session.episode_number || 0).padStart(2, '0');
    const yearPart = session.series_year ? ` (${session.series_year})` : '';
    const base = `${session.series_name || 'Serie'}${yearPart} - S${sn}E${en}`;
    name = session.episode_title ? `${base} - ${session.episode_title}` : base;
  } else if (session.iso_path) {
    name = session.iso_path.replace(/\\/g, '/').split('/').pop().replace(/\.iso$/i, '');
  } else {
    name = session.id;
  }

  const project = {
    id: pid,
    sessionId: session.id,
    session,
    name,
    isoPath: session.iso_path || '',
    ws: null,
    sortableAudio: null,
    sortableSubs: null,
    mkvNameWasManual: session.mkv_name_manual || false,
    activePhaseE: false,
    executionStartTime: null,
    executionTimer: null,
  };

  openProjects.push(project);
  renderProjectSubTabButton(project);
  createProjectPanel(project);
  switchSubTab(pid);
  renderProjectPanel(project);
  _doFilterSidebarSessions();

  return project;
}

/** Renderiza el botón de sub-tab para un proyecto. */
function renderProjectSubTabButton(project) {
  const container = document.getElementById('subtab-projects');
  const existing  = container.querySelector(`[data-pid="${project.id}"]`);
  if (existing) {
    existing.querySelector('.subtab-proj-name').textContent = project.name.slice(0, 24) + (project.name.length > 24 ? '…' : '');
    return;
  }
  const icon = projectStatusIcon(project.session?.status);
  const btn  = document.createElement('button');
  btn.className  = 'subtab-proj';
  btn.dataset.pid = project.id;
  btn.innerHTML  = `
    <span class="unsaved-dot" id="unsaved-dot-${project.id}" style="display:none" data-tooltip="Cambios sin guardar">●</span>
    <span class="subtab-proj-icon" id="subtab-icon-${project.id}">${icon}</span>
    <span class="subtab-proj-name" data-tooltip="${escHtml(project.name)}">${escHtml(project.name.slice(0,24))}${project.name.length > 24 ? '…' : ''}</span>
    <button class="subtab-proj-close" onclick="closeProject('${project.id}',event)"
      data-tooltip="Cerrar proyecto">×</button>`;
  btn.onclick = (e) => { if (!e.target.closest('.subtab-proj-close')) switchSubTab(project.id); };
  container.appendChild(btn);
  _updateSubtabScrollState();
}

/** Config de los dos scrollers de pestañas (Tab 1 y Tab 3). Misma lógica, IDs distintos. */
const _SUBTAB_SCROLLERS = [
  { areaId: 'subtab-projects-area',       scrollId: 'subtab-projects',       leftId: 'subtab-scroll-left',       rightId: 'subtab-scroll-right'       },
  { areaId: 'cmv40-subtab-projects-area', scrollId: 'cmv40-subtab-projects', leftId: 'cmv40-subtab-scroll-left', rightId: 'cmv40-subtab-scroll-right' },
];

/** Comprueba overflow horizontal de un scroller y activa/desactiva sus chevrones. */
function _updateOneSubtabScrollState(cfg) {
  const area   = document.getElementById(cfg.areaId);
  const scroll = document.getElementById(cfg.scrollId);
  if (!area || !scroll) return;
  const hasOverflow = scroll.scrollWidth > scroll.clientWidth + 1;
  area.classList.toggle('has-overflow', hasOverflow);
  if (!hasOverflow) return;
  const left  = document.getElementById(cfg.leftId);
  const right = document.getElementById(cfg.rightId);
  if (left)  left.disabled  = scroll.scrollLeft <= 0;
  if (right) right.disabled = scroll.scrollLeft + scroll.clientWidth >= scroll.scrollWidth - 1;
}

/** Actualiza el estado de scroll de todos los scrollers de pestañas. */
function _updateSubtabScrollState() {
  _SUBTAB_SCROLLERS.forEach(_updateOneSubtabScrollState);
}

/** Scrolla el contenedor ~70% de su ancho en la dirección dada. */
function _scrollSubtabContainer(scrollId, direction) {
  const scroll = document.getElementById(scrollId);
  if (!scroll) return;
  const step = Math.max(150, scroll.clientWidth * 0.7);
  scroll.scrollBy({ left: direction === 'left' ? -step : step, behavior: 'smooth' });
}

/** Handlers invocados desde los chevrones (HTML onclick). */
function scrollSubtabProjects(direction)      { _scrollSubtabContainer('subtab-projects', direction); }
function scrollCmv40SubtabProjects(direction) { _scrollSubtabContainer('cmv40-subtab-projects', direction); }

/** Instala wheel→horizontal + listeners de scroll/resize en todos los scrollers. Idempotente. */
function _installSubtabScrollBindings() {
  _SUBTAB_SCROLLERS.forEach(cfg => {
    const scroll = document.getElementById(cfg.scrollId);
    if (!scroll || scroll.dataset.scrollBound === '1') return;
    scroll.dataset.scrollBound = '1';
    scroll.addEventListener('wheel', (e) => {
      if (e.deltaY === 0 || e.shiftKey) return;
      scroll.scrollBy({ left: e.deltaY, behavior: 'auto' });
      e.preventDefault();
    }, { passive: false });
    scroll.addEventListener('scroll', () => _updateOneSubtabScrollState(cfg), { passive: true });
  });
  window.addEventListener('resize', _updateSubtabScrollState, { passive: true });
}

/** Marca el proyecto activo como modificado y muestra el punto naranja en su sub-tab. */
function markProjectDirty() {
  const project = getActiveProject();
  if (!project) return;
  project.dirty = true;
  const dot = document.getElementById(`unsaved-dot-${project.id}`);
  if (dot) dot.style.display = 'inline';
}

/** Limpia el indicador de cambios sin guardar de un proyecto. */
function clearProjectDirty(pid) {
  const project = openProjects.find(p => p.id === pid);
  if (!project) return;
  project.dirty = false;
  const dot = document.getElementById(`unsaved-dot-${pid}`);
  if (dot) dot.style.display = 'none';
}

/**
 * Devuelve el emoji de estado para el icono del sub-tab según el estado de la sesión.
 * @param {string} [status] — estado de la sesión
 */
function projectStatusIcon(status) {
  if (status === 'running') return '<span class="spinner-inline"></span>';
  const map = { pending: '💿', queued: '⏸', done: '✅', error: '❌' };
  return map[status] || '💿';
}

/** Actualiza el icono del sub-tab de un proyecto. */
/**
 * Actualiza el icono del sub-tab del proyecto según el estado de ejecución.
 * @param {Object} [project] — proyecto activo (si se omite, usa getActiveProject)
 */
function updateProjectTabIcon(project) {
  project = project || getActiveProject();
  if (!project) return;
  const btn = document.getElementById(`subtab-btn-${project.id}`);
  if (!btn) return;
  const iconEl = btn.querySelector('.subtab-proj-icon');
  if (!iconEl) return;
  const status = project.session?.status;
  if (status === 'running') {
    iconEl.textContent = '';
    if (!iconEl.querySelector('.spinner-inline')) {
      iconEl.innerHTML = '<span class="spinner-inline"></span>';
    }
  } else {
    const statusIcons = { pending: '💿', queued: '⏸', done: '✅', error: '❌' };
    iconEl.textContent = statusIcons[status] || '💿';
  }
}

/** Crea el panel DOM del proyecto (vacío, se rellena con renderProjectPanel). */
function createProjectPanel(project) {
  const content = document.getElementById('subtab-main');
  const div     = document.createElement('div');
  div.id        = `panel-project-${project.id}`;
  div.className = 'subtab-panel panel-project';
  div.tabIndex  = 0;
  div.innerHTML = buildProjectPanelHTML(project.id);
  content.appendChild(div);
}

/** Genera el HTML interno del panel de revisión de un proyecto (IDs prefijados con pid). */
function buildProjectPanelHTML(pid) {
  return `
    <div id="${pid}-tmdb-card" class="tmdb-card-slot"></div>

    <div id="${pid}-exec-result-banner" class="banner" style="display:none">
      <span class="banner-icon" id="${pid}-exec-result-icon"></span>
      <div class="exec-result-body">
        <div id="${pid}-exec-result-title" style="font-weight:600"></div>
        <div id="${pid}-exec-result-detail" class="exec-result-detail"></div>
      </div>
      <div class="exec-result-actions" id="${pid}-exec-result-actions"></div>
    </div>

    <div id="${pid}-iso-missing-banner" class="banner error" style="display:none">
      <span class="banner-icon">💿</span>
      <div><strong id="${pid}-iso-missing-title">Origen no disponible.</strong>
        <span id="${pid}-iso-missing-text"></span>
        Puedes editar los parámetros, pero no podrás ejecutar hasta que el origen vuelva a estar accesible.
      </div>
    </div>

    <div id="${pid}-vo-warning-banner" class="banner warning" style="display:none">
      <span class="banner-icon">⚠️</span>
      <div><strong>VO no determinada automáticamente.</strong>
        <span id="${pid}-vo-warning-text"></span>
        Revisa las pistas incluidas y ajusta los flags manualmente.
      </div>
    </div>

    <div class="project-phase-strip-row">
      <div class="project-phase-strip"
        data-tooltip="Análisis mkvmerge completado → Reglas automáticas aplicadas → En revisión">
        <span class="pps-step done">🔍 Análisis</span>
        <span class="pps-conn">→</span>
        <span class="pps-step done">⚡ Reglas</span>
        <span class="pps-conn">→</span>
        <span class="pps-step active">📋 Revisión</span>
        <span class="pps-conn">→</span>
        <span class="pps-step muted">⬇️ mkvmerge</span>
      </div>
      <button class="btn btn-ghost btn-xs" onclick="showRawAnalysisData()"
        data-tooltip="Ver los datos de análisis originales del ISO (mkvmerge -J + capítulos + reglas)">🔬 Datos ISO</button>
    </div>

    <div class="section-card globals-card">
      <div class="section-header">
        <span class="section-icon">📦</span>
        <div><div class="section-title">Nombre del MKV</div><div class="section-subtitle">Se recalcula automáticamente al cambiar los toggles</div></div>
      </div>
      <div class="globals-body">
        <div class="globals-mkv-row">
          <input type="text" id="${pid}-mkv-name-input" class="globals-mkv-input" oninput="onMkvNameInput()"
            data-tooltip="Nombre del MKV de salida. Se genera automáticamente.\nEdítalo manualmente si necesitas otro nombre.">
          <div id="${pid}-mkv-name-manual-notice" class="manual-notice" style="display:none">
            ✏️ Editado manualmente
            <button class="btn btn-xs btn-ghost" onclick="revertMkvName()"
              data-tooltip="Restaurar el nombre generado automáticamente.">Revertir</button>
          </div>
        </div>
        <div class="globals-toggles-row">
          <div class="global-toggle-item" id="${pid}-global-fel">
            <div class="global-toggle-left">
              <span class="global-card-icon">🎬</span>
              <div>
                <div class="global-card-label">Dolby Vision FEL</div>
                <div class="global-card-reason"><span>ℹ️</span><span id="${pid}-fel-reason-text"></span></div>
                <div id="${pid}-dovi-detail" class="global-card-reason" style="display:none; margin-top:2px; font-size:10px; color:var(--text-3)"></div>
              </div>
            </div>
            <div class="global-toggle-right">
              <span id="${pid}-fel-value" class="toggle-value">—</span>
              <label class="ios-toggle" data-tooltip="FEL (Full Enhancement Layer) de Dolby Vision.\nAfecta al nombre del MKV.">
                <input type="checkbox" id="${pid}-toggle-fel" onchange="onFelChange()">
                <span class="ios-track"></span><span class="ios-thumb"></span>
              </label>
            </div>
          </div>
          <div class="global-toggle-item" id="${pid}-global-dcp">
            <div class="global-toggle-left">
              <span class="global-card-icon">🎵</span>
              <div>
                <div class="global-card-label">Audio DCP</div>
                <div class="global-card-reason"><span>ℹ️</span><span id="${pid}-dcp-reason-text"></span></div>
              </div>
            </div>
            <div class="global-toggle-right">
              <span id="${pid}-dcp-value" class="toggle-value">—</span>
              <label class="ios-toggle" data-tooltip="Tag 'Audio DCP' en el nombre del ISO.\nAñade sufijo (DCP 9.1.6) a pistas TrueHD Atmos.">
                <input type="checkbox" id="${pid}-toggle-dcp" onchange="onDcpChange()">
                <span class="ios-track"></span><span class="ios-thumb"></span>
              </label>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="section-card">
      <div class="section-header">
        <span class="section-icon">🔊</span>
        <div><div class="section-title">Audio</div><div class="section-subtitle">Arrastra para reordenar · pulsa ✕ para descartar</div></div>
        <span class="section-badge" id="${pid}-audio-count">0 pistas</span>
      </div>
      <div style="padding:0 16px 10px; display:flex; gap:6px; align-items:center; font-size:12px; flex-wrap:wrap">
        <span style="color:var(--text-3)">Modo:</span>
        <button class="btn btn-xs mode-toggle active" data-mode="filtered" data-track="audio"
          onclick="setTrackMode('audio','filtered')"
          data-tooltip="Solo Castellano + VO con selección por calidad">🎯 Castellano + VO</button>
        <button class="btn btn-xs mode-toggle" data-mode="keep_all" data-track="audio"
          onclick="setTrackMode('audio','keep_all')"
          data-tooltip="Mantener todas las pistas con labels automáticos (sin reordenar ni descartar)">📋 Mantener todas</button>
      </div>
      <div class="section-body tracks-type-body">
        <div class="tracks-included-group">
          <div class="tracks-group-label">Incluidas</div>
          <ul id="${pid}-included-audio-tracks" class="track-list"></ul>
        </div>
        <div class="tracks-discarded-group" id="${pid}-discarded-audio-group">
          <div class="tracks-group-label tracks-group-label--discarded">Descartadas</div>
          <div id="${pid}-discarded-audio-tracks"></div>
        </div>
      </div>
    </div>

    <div class="section-card">
      <div class="section-header">
        <span class="section-icon">💬</span>
        <div><div class="section-title">Subtítulos</div><div class="section-subtitle">Arrastra para reordenar · pulsa ✕ para descartar</div></div>
        <span class="section-badge" id="${pid}-sub-count">0 pistas</span>
      </div>
      <div style="padding:0 16px 10px; display:flex; gap:6px; align-items:center; font-size:12px; flex-wrap:wrap">
        <span style="color:var(--text-3)">Modo:</span>
        <button class="btn btn-xs mode-toggle active" data-mode="filtered" data-track="subtitle"
          onclick="setTrackMode('subtitle','filtered')"
          data-tooltip="Solo Castellano + VO + Inglés. Detecta forzados por tamaño relativo (completo/forzado ≥3×) y descarta pistas en otros idiomas.">🎯 Castellano + VO + Inglés</button>
        <button class="btn btn-xs mode-toggle" data-mode="keep_all" data-track="subtitle"
          onclick="setTrackMode('subtitle','keep_all')"
          data-tooltip="Mantener todos los subtítulos con labels automáticos (sin reordenar ni descartar)">📋 Mantener todos</button>
      </div>
      <div class="section-body tracks-type-body">
        <div class="tracks-included-group">
          <div class="tracks-group-label">Incluidas</div>
          <ul id="${pid}-included-sub-tracks" class="track-list"></ul>
        </div>
        <div class="tracks-discarded-group" id="${pid}-discarded-sub-group">
          <div class="tracks-group-label tracks-group-label--discarded">Descartadas</div>
          <div id="${pid}-discarded-sub-tracks"></div>
        </div>
      </div>
    </div>

    <div class="section-card">
      <div class="section-header">
        <span class="section-icon">📖</span>
        <div><div class="section-title">Capítulos</div><div class="section-subtitle">Clic en la barra para añadir · arrastra para ajustar · ✕ para eliminar</div></div>
      </div>
      <div class="section-body">
        <div id="${pid}-chapters-auto-banner" class="banner info" style="display:none">
          <span class="banner-icon" id="${pid}-chapters-auto-icon">⚠️</span>
          <span id="${pid}-chapters-auto-text"></span>
          <button class="btn btn-xs" id="${pid}-chapters-generic-btn" style="display:none; margin-left:auto"
            onclick="setGenericChapterNames()"
            data-tooltip="Reemplaza todos los nombres por Capítulo 01, Capítulo 02… (mantiene timestamps)">🏷️ Nombres genéricos</button>
          <button class="btn btn-xs" id="${pid}-chapters-reset-btn" style="display:none"
            onclick="resetChaptersFromDisc()"
            data-tooltip="Vuelve a extraer los capítulos originales del disco (MPLS). Descarta las ediciones manuales.">🔄 Restaurar del disco</button>
        </div>
        <div id="${pid}-chapter-timeline-wrap" class="chapter-timeline-wrap"
          onclick="onTimelineClick(event)"
          onmousemove="onTimelineHover(event)"
          onmouseleave="onTimelineLeave()">
          <div id="${pid}-chapter-timeline-track" class="chapter-timeline-track"></div>
          <div id="${pid}-timeline-marks" class="timeline-marks"></div>
          <div id="${pid}-timeline-cursor" class="timeline-cursor"></div>
        </div>
        <div id="${pid}-chapters-list" class="chapter-list"></div>
      </div>
    </div>

    <div class="section-card" id="${pid}-exec-history-card">
      <div class="section-header">
        <span class="section-icon">📊</span>
        <div><div class="section-title">Historial de ejecuciones</div><div class="section-subtitle">Resultados, tiempos por fase y logs de cada ejecución</div></div>
        <span class="section-badge" id="${pid}-exec-history-count">0</span>
      </div>
      <div class="section-body">
        <div id="${pid}-exec-history-empty" class="exec-history-empty">Sin ejecuciones todavía</div>
        <div id="${pid}-exec-history-table-wrap" style="display:none">
          <table class="exec-history-table" id="${pid}-exec-history-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Fecha</th>
                <th>Estado</th>
                <th data-tooltip="Montar ISO via loop mount">💿 Montar</th>
                <th data-tooltip="mkvmerge: MPLS → MKV">⬇️ mkvmerge</th>
                <th data-tooltip="Desmontar ISO (umount)">🔓 Desmontar</th>
                <th data-tooltip="mkvpropedit in-place (solo ruta sin reordenación, — en ruta directa)">✍️ Propedit</th>
                <th data-tooltip="Duración total de la ejecución">⏱ Total</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody id="${pid}-exec-history-tbody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="project-action-bar">
      <button class="btn btn-ghost btn-md" onclick="saveSession()"
        data-tooltip="Guardar los cambios sin ejecutar">💾 Guardar</button>
      <button class="btn btn-success btn-lg" id="${pid}-execute-btn" onclick="executeSession()"
        data-tooltip="Confirmar y añadir a la cola de ejecución">
        ▶️ Confirmar y ejecutar
      </button>
    </div>`;
}

/**
 * Cierra un proyecto con confirmación.
 * @param {string} pid - ID del proyecto.
 * @param {Event}  e   - Evento del botón (para stopPropagation).
 */
function closeProject(pid, e) {
  e?.stopPropagation();
  const project = openProjects.find(p => p.id === pid);
  if (!project) return;

  if (project.dirty) {
    showConfirm(
      'Cerrar proyecto',
      `"${project.name}" tiene cambios sin ejecutar.`,
      () => _doCloseProject(pid),
      'Cerrar sin guardar',
    );
    // Botón guardar y cerrar — limpiar cualquier botón extra previo antes de insertar
    const okBtn = document.getElementById('confirm-ok-btn');
    okBtn.parentNode.querySelectorAll('.confirm-extra-btn').forEach(b => b.remove());
    const saveCloseBtn = document.createElement('button');
    saveCloseBtn.className = 'btn btn-primary btn-sm confirm-extra-btn';
    saveCloseBtn.textContent = '💾 Guardar y cerrar';
    saveCloseBtn.onclick = async () => {
      closeModal('confirm-modal');
      const activeBackup = activeSubTabId;
      activeSubTabId = pid;
      currentSession = project.session;
      await saveSession();
      activeSubTabId = activeBackup;
      _doCloseProject(pid);
    };
    okBtn.parentNode.insertBefore(saveCloseBtn, okBtn);
  } else {
    _doCloseProject(pid);
  }
}

/** Elimina el proyecto del array y limpia el DOM. */
function _doCloseProject(pid) {
  const idx = openProjects.findIndex(p => p.id === pid);
  if (idx === -1) return;

  const project = openProjects[idx];
  if (project.ws) { project.ws.close(); project.ws = null; }
  if (project.sortable) { project.sortable.destroy(); }
  clearInterval(project.executionTimer);

  document.getElementById(`panel-project-${pid}`)?.remove();
  document.querySelector(`.subtab-proj[data-pid="${pid}"]`)?.remove();
  openProjects.splice(idx, 1);
  _updateSubtabScrollState();

  // Activar el sub-tab más cercano
  if (activeSubTabId === pid) {
    const next = openProjects[idx] || openProjects[idx - 1];
    switchSubTab(next ? next.id : (openProjects.length === 0 ? 'empty' : 'cola'));
  }
  _doFilterSidebarSessions();
}



// ═══════════════════════════════════════════════════════════════════
//  TOAST NOTIFICATIONS
// ═══════════════════════════════════════════════════════════════════

/**
 * Muestra una notificación toast temporal en la esquina inferior derecha.
 *
 * @param {string} msg      - Texto del mensaje (se escapa antes de insertar en el DOM).
 * @param {'info'|'success'|'warning'|'error'} [type='info'] - Tipo visual.
 * @param {number} [duration=3500] - Milisegundos hasta el inicio de la animación de salida.
 */
/** Contador global para IDs únicos de toast. */
let _toastIdCounter = 0;

/**
 * Muestra un toast de notificación temporal.
 * @param {string} msg      — Texto del mensaje (ya escapado si contiene HTML).
 * @param {string} type     — 'success' | 'error' | 'warning' | 'info'
 * @param {number} duration — ms hasta auto-eliminar. 0 = persistente (eliminar con removeToast).
 * @returns {string} ID del toast para poder eliminarlo con removeToast().
 */
function showToast(msg, type = 'info', duration = 3500) {
  const icons = { success:'✅', error:'❌', warning:'⚠️', info:'ℹ️' };
  const container = document.getElementById('toast-container');
  const t = document.createElement('div');
  const id = `toast-${++_toastIdCounter}`;
  t.id = id;
  t.className = `toast ${type}`;
  t.innerHTML = `<span class="toast-icon">${icons[type] || 'ℹ️'}</span>
                 <span class="toast-msg">${msg}</span>`;
  container.appendChild(t);
  if (duration > 0) {
    setTimeout(() => {
      t.classList.add('removing');
      t.addEventListener('animationend', () => t.remove());
    }, duration);
  }
  return id;
}

/** Elimina un toast persistente por su ID. */
function removeToast(toastId) {
  const t = document.getElementById(toastId);
  if (!t) return;
  t.classList.add('removing');
  t.addEventListener('animationend', () => t.remove());
}

// ═══════════════════════════════════════════════════════════════════
//  CUSTOM CONFIRM DIALOG
// ═══════════════════════════════════════════════════════════════════

/**
 * Muestra un diálogo de confirmación modal reutilizable.
 *
 * @param {string}   title        - Título del diálogo.
 * @param {string}   message      - Texto del cuerpo del diálogo.
 * @param {Function} onConfirm    - Callback a ejecutar si el usuario confirma.
 * @param {string}   [confirmLabel='Confirmar'] - Texto del botón de confirmación.
 */
function showConfirm(title, message, onConfirm, confirmLabel = 'Confirmar') {
  document.getElementById('confirm-title').textContent   = title;
  document.getElementById('confirm-message').textContent = message;
  const okBtn = document.getElementById('confirm-ok-btn');
  // Limpiar botones extra de usos anteriores
  okBtn.parentNode.querySelectorAll('.confirm-extra-btn').forEach(b => b.remove());
  okBtn.textContent = confirmLabel;
  const newBtn = okBtn.cloneNode(true);  // elimina listeners previos
  okBtn.parentNode.replaceChild(newBtn, okBtn);
  newBtn.addEventListener('click', () => {
    closeModal('confirm-modal');
    onConfirm();
  });
  openModal('confirm-modal');
}

// ═══════════════════════════════════════════════════════════════════
//  MODAL HELPERS
// ═══════════════════════════════════════════════════════════════════

/** Abre un modal añadiendo la clase 'open' al overlay. @param {string} id */
function openModal(id)  { document.getElementById(id).classList.add('open'); }
/** Cierra un modal eliminando la clase 'open' del overlay. @param {string} id */
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

// ── Modal de Configuración (API keys, integraciones) ─────────────
// Cache de la respuesta /api/settings — se usa para saber si las secciones
// del repo DoviTools están configuradas sin tener que llamar cada vez.
let _settingsCache = null;

async function openSettingsModal() {
  ['settings-tmdb-feedback', 'settings-google-feedback',
   'settings-drive-folder-feedback', 'settings-sheet-feedback'].forEach(id => {
    const fb = document.getElementById(id);
    if (fb) { fb.textContent = ''; fb.className = 'settings-feedback'; }
  });
  ['settings-tmdb-input', 'settings-google-input',
   'settings-drive-folder-input'].forEach(id => {
    const inp = document.getElementById(id);
    if (inp) inp.value = '';
  });
  // El sheet NO se borra — pre-populamos con la URL actual para que el
  // usuario vea qué está usando y pueda editarlo directamente.
  await _loadSettings();
  // Versión + chequeo de updates (no force, usa cache 1h)
  _renderVersionInfo();
  checkForUpdates(false);
  openModal('settings-modal');
  setTimeout(() => document.getElementById('settings-tmdb-input')?.focus(), 50);
}

/** Comprobacion silenciosa de updates al arrancar la app. Sin force (usa
 *  cache 1h) para no machacar la API de GitHub. Si hay update: pinta el
 *  pill ambar en el header. La comprobacion respeta la version simulada
 *  para que el modo dev test sea coherente con el header. */
async function _initUpdateCheckHeader() {
  // Esperamos un tick para que el modal Settings/UI ya esté inicializado
  // y para no competir con cargas críticas de arranque.
  await new Promise(r => setTimeout(r, 1500));
  await _refreshHeaderUpdatePill();
}

async function _refreshHeaderUpdatePill() {
  const pill = document.getElementById('header-update-pill');
  if (!pill) return;
  const params = new URLSearchParams();
  const sim = _getSimulatedVersion();
  if (sim) params.set('simulate_current', sim);
  const url = '/api/version/check-updates' + (params.toString() ? '?' + params.toString() : '');
  const data = await apiFetch(url, { silent: true });
  const txtEl = document.getElementById('header-update-pill-text');
  if (!data || !data.update_available || !data.latest) {
    pill.style.display = 'none';
    return;
  }
  pill.style.display = 'inline-flex';
  if (txtEl) txtEl.textContent = `Nueva versión: ${data.latest}`;
}

async function _renderVersionInfo() {
  const data = await apiFetch('/api/version', { silent: true });
  if (!data) return;
  const cur = document.getElementById('settings-version-current');
  const pill = document.getElementById('settings-version-pill');
  if (!cur || !pill) return;
  const versionLabel = data.version || 'desconocida';
  let pillCls = 'dev', pillTxt = '⚠ desarrollo';
  if (data.is_tagged) {
    pillCls = 'tagged'; pillTxt = '✓ release';
  } else if (data.commit) {
    pillCls = 'dev'; pillTxt = '⚙ desarrollo';
  } else {
    pillCls = 'unknown'; pillTxt = '? desconocida';
  }
  const commitTxt = data.commit ? ` · ${data.commit}` : '';
  const dirtyTxt  = data.is_dirty ? ' · dirty' : '';
  cur.innerHTML = `
    <strong>${escHtml(versionLabel)}</strong><span style="color:var(--text-3); font-size:11.5px">${escHtml(commitTxt + dirtyTxt)}</span>`;
  pill.className = 'settings-version-pill ' + pillCls;
  pill.textContent = pillTxt;
  // Mostrar input de simulación SOLO con DEV_MODE=1 en runtime (no basta
  // con que la version sea post-tag tipo v2.1.6-1-gXXXX — eso pasa en
  // builds de produccion en NAS si rebuilds despues del ultimo tag).
  const simBox = document.getElementById('settings-version-simulate');
  if (simBox) {
    simBox.style.display = data.is_dev_mode ? 'flex' : 'none';
    const simInput = document.getElementById('settings-version-simulate-input');
    if (simInput) simInput.value = localStorage.getItem('hdo_simulate_version') || '';
  }
}

function _getSimulatedVersion() {
  return (localStorage.getItem('hdo_simulate_version') || '').trim();
}

function applySimulatedVersion() {
  const inp = document.getElementById('settings-version-simulate-input');
  const v = (inp?.value || '').trim();
  if (v) {
    localStorage.setItem('hdo_simulate_version', v);
    showToast(`🧪 Simulando versión actual: ${v}`, 'info');
  } else {
    localStorage.removeItem('hdo_simulate_version');
    showToast('🧪 Simulación desactivada', 'info');
  }
  checkForUpdates(true);
}

function clearSimulatedVersion() {
  localStorage.removeItem('hdo_simulate_version');
  const inp = document.getElementById('settings-version-simulate-input');
  if (inp) inp.value = '';
  showToast('🧪 Simulación desactivada', 'info');
  checkForUpdates(true);
}

async function checkForUpdates(force) {
  const banner = document.getElementById('settings-update-banner');
  const btn = document.getElementById('settings-version-check-btn');
  if (!banner) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = '🔄 Consultando…';
  }
  const params = new URLSearchParams();
  if (force) params.set('force', 'true');
  const sim = _getSimulatedVersion();
  if (sim) params.set('simulate_current', sim);
  const url = '/api/version/check-updates' + (params.toString() ? '?' + params.toString() : '');
  const data = await apiFetch(url, { silent: true });
  // Sync el pill del header con el resultado actual (ej. tras ignorar
  // version o cambiar simulacion, el header refleja el cambio sin esperar
  // a otro tick automatico).
  const pill = document.getElementById('header-update-pill');
  const pillTxt = document.getElementById('header-update-pill-text');
  if (pill) {
    if (data && data.update_available && data.latest) {
      pill.style.display = 'inline-flex';
      if (pillTxt) pillTxt.textContent = `Nueva versión: ${data.latest}`;
    } else {
      pill.style.display = 'none';
    }
  }
  if (btn) {
    btn.disabled = false;
    btn.textContent = '🔄 Comprobar actualizaciones';
  }
  if (!data) {
    banner.style.display = 'block';
    banner.className = 'settings-update-banner err';
    banner.innerHTML = `<div class="settings-update-msg">⚠ No se pudo consultar la API de GitHub. Reintenta en unos minutos.</div>`;
    return;
  }
  if (!data.update_available) {
    banner.style.display = 'block';
    if (!data.latest) {
      // No conseguimos resolver la version remota — no es 'al dia',
      // es 'no se pudo comprobar'. Banner gris/error informativo.
      banner.className = 'settings-update-banner err';
      banner.innerHTML = `<div class="settings-update-msg">⚠ No se pudo determinar la última versión publicada. Comprueba que el repo tenga al menos un tag <code>vX.Y.Z</code> o un Release publicado.</div>`;
      return;
    }
    banner.className = 'settings-update-banner ok';
    const simBadge = data.simulated ? ` <span class="settings-update-sim-badge">🧪 simulado</span>` : '';
    const latestPart = ` · última publicada: <strong>${escHtml(data.latest)}</strong>${simBadge}`;
    const ignored = data.ignored_version
      ? `<div class="settings-update-msg-sub">Ignorando avisos de la versión ${escHtml(data.ignored_version)}. <button class="btn btn-ghost btn-xs" onclick="ignoreUpdate('')">Reactivar avisos</button></div>`
      : '';
    banner.innerHTML = `<div class="settings-update-msg">✓ Estás al día (current: <strong>${escHtml(data.current)}</strong>)${latestPart}.</div>${ignored}`;
    return;
  }
  // Hay update — banner ámbar con notas (todas las pendientes) + botones
  banner.style.display = 'block';
  banner.className = 'settings-update-banner warn';
  const cmds = `docker compose pull\ndocker compose up -d`;
  const simBadge = data.simulated ? `<span class="settings-update-sim-badge">🧪 simulado</span>` : '';

  // Lista de releases pendientes (todas entre current y latest, newest first).
  // Si solo viene release_notes (fallback antiguo), construye un pseudo-release
  // con la latest para mantener el formato uniforme.
  let pending = Array.isArray(data.pending_releases) ? data.pending_releases.slice() : [];
  if (!pending.length && data.release_notes) {
    pending = [{
      tag: data.latest,
      body: data.release_notes,
      url: data.release_url || '',
      published_at: data.published_at || '',
    }];
  }

  let notesHtml = '';
  if (pending.length) {
    const sectionsHtml = pending.map(rel => {
      const dateStr = rel.published_at
        ? new Date(rel.published_at).toLocaleDateString('es-ES', { day: '2-digit', month: 'short', year: 'numeric' })
        : '';
      const linkBtn = rel.url
        ? `<a class="settings-update-rel-link" href="${escHtml(rel.url)}" target="_blank" rel="noreferrer">↗</a>`
        : '';
      const body = (rel.body || '').trim() || '_(release sin notas)_';
      return `
        <div class="settings-update-rel">
          <div class="settings-update-rel-head">
            <strong>${escHtml(rel.tag)}</strong>
            ${dateStr ? `<span class="settings-update-rel-date">· ${escHtml(dateStr)}</span>` : ''}
            ${linkBtn}
          </div>
          <div class="settings-update-rel-body">${_renderReleaseMarkdown(body)}</div>
        </div>`;
    }).join('');
    const summaryTxt = pending.length === 1
      ? `📋 Ver notas de versión (1 release pendiente)`
      : `📋 Ver notas de versión (${pending.length} releases pendientes)`;
    // Cerrado por defecto — el triángulo nativo es poco intuitivo;
    // usamos un botón visible con icono + texto explícito.
    notesHtml = `<details class="settings-update-notes"><summary class="settings-update-notes-toggle">${summaryTxt}</summary>${sectionsHtml}</details>`;
  }

  banner.innerHTML = `
    <div class="settings-update-head">
      🔔 Nueva versión disponible: <strong>${escHtml(data.current)}</strong> → <strong>${escHtml(data.latest)}</strong> ${simBadge}
    </div>
    ${notesHtml}
    <div class="settings-update-cmd">
      <pre id="settings-update-cmd-pre">${escHtml(cmds)}</pre>
    </div>
    <div class="settings-update-actions">
      <button class="btn btn-primary btn-sm" onclick="copyUpdateCommands()">📋 Copiar comandos</button>
      ${data.release_url ? `<a class="btn btn-secondary btn-sm" href="${escHtml(data.release_url)}" target="_blank" rel="noreferrer">↗ Release en GitHub</a>` : ''}
      <button class="btn btn-ghost btn-sm" onclick="ignoreUpdate('${escHtml(data.latest)}')">Ignorar esta versión</button>
    </div>`;
}

/** Renderiza markdown ligero (headings ##, ###, bullets, **bold**, `code`)
 *  a HTML. Suficiente para las release notes que generamos con plantilla
 *  fija. NO es un parser markdown completo — no hace falta. */
function _renderReleaseMarkdown(md) {
  const lines = md.split('\n');
  const out = [];
  let inList = false;
  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) { closeList(); continue; }
    // Heading H2 (## Título)
    let m = line.match(/^##\s+(.+)$/);
    if (m) { closeList(); out.push(`<h4 class="settings-update-rel-h">${_inlineMd(m[1])}</h4>`); continue; }
    // Heading H3
    m = line.match(/^###\s+(.+)$/);
    if (m) { closeList(); out.push(`<h5 class="settings-update-rel-h">${_inlineMd(m[1])}</h5>`); continue; }
    // Bullet (- texto)
    m = line.match(/^\s*[-*]\s+(.+)$/);
    if (m) {
      if (!inList) { out.push('<ul class="settings-update-rel-list">'); inList = true; }
      out.push(`<li>${_inlineMd(m[1])}</li>`);
      continue;
    }
    // Texto suelto = párrafo
    closeList();
    out.push(`<p>${_inlineMd(line)}</p>`);
  }
  closeList();
  return out.join('');
}

/** Formato inline básico: **bold**, `code`, escape de < > & */
function _inlineMd(text) {
  let s = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  return s;
}

async function copyUpdateCommands() {
  const pre = document.getElementById('settings-update-cmd-pre');
  if (!pre) return;
  const txt = pre.textContent || '';
  const ok = await _copyTextToClipboardWithFallback(txt);
  showToast(ok ? '📋 Comandos copiados al portapapeles' : 'No se pudo copiar al portapapeles', ok ? 'success' : 'error');
}

async function ignoreUpdate(version) {
  await apiFetch('/api/version/ignore-update', {
    method: 'POST',
    body: JSON.stringify({ version }),
  });
  showToast(version
    ? `⏭ Aviso de ${version} silenciado`
    : '🔔 Avisos de actualización reactivados', 'info');
  checkForUpdates(false);
}

async function _loadSettings() {
  const data = await apiFetch('/api/settings');
  if (!data) return;
  _settingsCache = data;
  _renderSettings(data);
}

function _renderSettingsSection(key, data) {
  const badge = document.getElementById(`settings-${key}-status`);
  const inp = document.getElementById(`settings-${key}-input`);
  if (!badge) return false;
  const st = data[key] || {};
  if (st.configured) {
    const srcLabel = st.source === 'env' ? 'desde .env' : 'guardada';
    const cls = st.source === 'env' ? 'env' : 'ok';
    badge.className = 'settings-status ' + cls;
    badge.textContent = `✓ ${srcLabel}${st.last4 ? ' · …' + st.last4 : ''}`;
    if (inp) inp.placeholder = `Ya configurada (…${st.last4 || ''}). Escribe para reemplazar.`;
    return st.source === 'settings';
  }
  badge.className = 'settings-status warn';
  badge.textContent = 'No configurada';
  if (inp) {
    inp.placeholder = key === 'tmdb'
      ? 'Pega aquí tu Clave de la API…'
      : 'Pega aquí tu Clave de la API de Google…';
  }
  return false;
}

function _renderSettingsDriveFolder(data) {
  const badge = document.getElementById('settings-drive-folder-status');
  const inp = document.getElementById('settings-drive-folder-input');
  if (!badge) return false;
  const st = data.drive_folder || {};
  if (st.configured) {
    const srcLabel = st.source === 'env' ? 'desde .env' : 'guardada';
    const cls = st.source === 'env' ? 'env' : 'ok';
    badge.className = 'settings-status ' + cls;
    const idTail = st.folder_id_last6 ? ` · ID …${st.folder_id_last6}` : '';
    badge.textContent = `✓ ${srcLabel}${idTail}`;
    if (inp) inp.placeholder = `Ya configurado. Escribe una URL para reemplazar.`;
    return st.source === 'settings';
  }
  badge.className = 'settings-status warn';
  badge.textContent = '⛔ Sin URL — repo bloqueado';
  if (inp) inp.placeholder = 'https://drive.google.com/drive/folders/…';
  return false;
}

function _renderSettingsSheet(data) {
  const badge = document.getElementById('settings-sheet-status');
  const inp = document.getElementById('settings-sheet-input');
  const resetBtn = document.getElementById('settings-sheet-reset');
  if (!badge) return false;
  const st = data.sheet || {};
  const srcLabel = st.source === 'env' ? 'desde .env'
                 : st.source === 'settings' ? 'personalizado'
                 : 'default público';
  const cls = st.source === 'settings' ? 'ok' : (st.source === 'env' ? 'env' : 'default');
  badge.className = 'settings-status ' + cls;
  const idTail = st.sheet_id_last6 ? ` · …${st.sheet_id_last6}·gid${st.gid || '0'}` : '';
  badge.textContent = `${srcLabel}${idTail}`;
  // Pre-popular con la URL activa (es pública, no es secret)
  if (inp && !inp.value) inp.value = st.url || '';
  // Botón reset visible solo si NO es el default
  if (resetBtn) resetBtn.style.display = st.is_default ? 'none' : '';
  return st.source === 'settings';
}

function _renderSettings(data) {
  const tmdbUserSet   = _renderSettingsSection('tmdb', data);
  const googleUserSet = _renderSettingsSection('google', data);
  const driveUserSet  = _renderSettingsDriveFolder(data);
  const sheetUserSet  = _renderSettingsSheet(data);
  const clearBtn = document.getElementById('settings-clear-btn');
  if (clearBtn) {
    const anyUserSet = tmdbUserSet || googleUserSet || driveUserSet || sheetUserSet;
    clearBtn.style.display = anyUserSet ? '' : 'none';
  }
}

async function _testKeyGeneric(key, fieldKey, endpoint, payloadKey) {
  const inp = document.getElementById(`settings-${fieldKey}-input`);
  const fb  = document.getElementById(`settings-${fieldKey}-feedback`);
  const btn = document.getElementById(`settings-${fieldKey}-test`);
  const value = (inp?.value || '').trim();
  if (!fb || !btn) return;
  if (!value) {
    fb.textContent = key === 'drive-folder'
      ? 'Pega la URL del folder Drive para probar'
      : key === 'sheet'
      ? 'Pega la URL del sheet para probar'
      : 'Introduce una Clave de la API para probar';
    fb.className = 'settings-feedback info';
    return;
  }
  btn.disabled = true;
  fb.textContent = 'Probando…';
  fb.className = 'settings-feedback info';
  const body = {};
  body[payloadKey] = value;
  const data = await apiFetch(endpoint, {
    method: 'POST', body: JSON.stringify(body),
  });
  btn.disabled = false;
  if (!data) return;
  fb.textContent = data.message || (data.ok ? 'OK' : 'Error');
  fb.className = 'settings-feedback ' + (data.ok ? 'ok' : 'error');
}

async function testTmdbKey()        { return _testKeyGeneric('tmdb',         'tmdb',         '/api/settings/test-tmdb',         'tmdb_api_key'); }
async function testGoogleKey()      { return _testKeyGeneric('google',       'google',       '/api/settings/test-google',       'google_api_key'); }
async function testDriveFolderUrl() { return _testKeyGeneric('drive-folder', 'drive-folder', '/api/settings/test-drive-folder', 'cmv40_drive_folder_url'); }
async function testSheetUrl()       { return _testKeyGeneric('sheet',        'sheet',        '/api/settings/test-sheet',        'cmv40_sheet_url'); }

function resetSheetUrlToDefault() {
  // Envía cadena vacía → borra el override → vuelve al default público
  const inp = document.getElementById('settings-sheet-input');
  if (inp) inp.value = '';
  apiFetch('/api/settings', {
    method: 'POST',
    body: JSON.stringify({ cmv40_sheet_url: '' }),
  }).then(data => {
    if (!data) return;
    _settingsCache = data;
    _renderSettings(data);
    const fb = document.getElementById('settings-sheet-feedback');
    if (fb) { fb.textContent = 'URL restaurada al default público ✓'; fb.className = 'settings-feedback ok'; }
    showToast('URL del sheet restaurada', 'success');
  });
}

async function saveSettings() {
  const tmdbInp        = document.getElementById('settings-tmdb-input');
  const googleInp      = document.getElementById('settings-google-input');
  const driveFolderInp = document.getElementById('settings-drive-folder-input');
  const sheetInp       = document.getElementById('settings-sheet-input');
  const btn = document.getElementById('settings-save-btn');
  const fbTmdb   = document.getElementById('settings-tmdb-feedback');
  const fbGoogle = document.getElementById('settings-google-feedback');
  const fbDrive  = document.getElementById('settings-drive-folder-feedback');
  const fbSheet  = document.getElementById('settings-sheet-feedback');
  if (!btn) return;
  const payload = {};
  const tk = (tmdbInp?.value || '').trim();
  const gk = (googleInp?.value || '').trim();
  const du = (driveFolderInp?.value || '').trim();
  const su = (sheetInp?.value || '').trim();
  if (tk) payload.tmdb_api_key = tk;
  if (gk) payload.google_api_key = gk;
  if (du) payload.cmv40_drive_folder_url = du;
  // Para el sheet, si la URL está vacía o coincide con el default, no la guardamos
  // (dejamos que caiga al default automático). Si es distinta, la guardamos.
  if (su && su !== (_settingsCache?.sheet?.default_url || '')) {
    payload.cmv40_sheet_url = su;
  }
  if (!Object.keys(payload).length) {
    closeModal('settings-modal');
    return;
  }
  btn.disabled = true;
  const data = await apiFetch('/api/settings', {
    method: 'POST', body: JSON.stringify(payload),
  });
  btn.disabled = false;
  if (!data) return;
  _settingsCache = data;
  _renderSettings(data);
  if (tk && tmdbInp)        { tmdbInp.value = '';        if (fbTmdb)   { fbTmdb.textContent = 'Guardada ✓';   fbTmdb.className = 'settings-feedback ok'; } }
  if (gk && googleInp)      { googleInp.value = '';      if (fbGoogle) { fbGoogle.textContent = 'Guardada ✓'; fbGoogle.className = 'settings-feedback ok'; } }
  if (du && driveFolderInp) { driveFolderInp.value = ''; if (fbDrive)  { fbDrive.textContent = 'Guardada ✓';  fbDrive.className = 'settings-feedback ok'; } }
  if (payload.cmv40_sheet_url && fbSheet) { fbSheet.textContent = 'Guardada ✓'; fbSheet.className = 'settings-feedback ok'; }
  showToast('Configuración guardada', 'success');
}

async function clearAllKeys() {
  const data = await apiFetch('/api/settings', {
    method: 'POST',
    body: JSON.stringify({
      tmdb_api_key: '',
      google_api_key: '',
      cmv40_drive_folder_url: '',
      cmv40_sheet_url: '',
    }),
  });
  if (!data) return;
  _settingsCache = data;
  _renderSettings(data);
  showToast('Claves y URLs borradas', 'info');
}

// ── Mantenimiento: scan + cleanup de huerfanos ──────────────────────
// Flujo: escanear → tabla con checkboxes → confirmar → toast con resumen.
// Solo paths bajo prefixes whitelisted (validacion adicional en backend).

function _cleanupFmtBytes(bytes) {
  if (!bytes || bytes < 1024) return `${bytes || 0} B`;
  const KB = 1024, MB = KB * 1024, GB = MB * 1024;
  if (bytes < MB) return `${(bytes / KB).toFixed(1)} KB`;
  if (bytes < GB) return `${(bytes / MB).toFixed(1)} MB`;
  return `${(bytes / GB).toFixed(2)} GB`;
}

function _cleanupFmtAge(secs) {
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

async function cleanupScanAndShow() {
  const btn = document.getElementById('settings-cleanup-scan-btn');
  const resultEl = document.getElementById('settings-cleanup-result');
  if (!btn || !resultEl) return;

  btn.disabled = true;
  btn.innerHTML = '⏳ Escaneando…';
  resultEl.innerHTML = '';

  const data = await apiFetch('/api/cleanup/scan');
  btn.disabled = false;
  btn.innerHTML = '🔍 Escanear huérfanos';

  if (!data) return;
  if (!data.items || !data.items.length) {
    resultEl.innerHTML = '<div class="settings-cleanup-empty">✓ No se encontraron huérfanos. Todo limpio.</div>';
    return;
  }

  // Render tabla con checkboxes (default: marcado solo si safe=true)
  const rows = data.items.map((it, i) => {
    const checked = it.safe ? 'checked' : '';
    const warnIcon = it.safe ? '' : '<span class="cleanup-warn" data-tooltip="Reciente o potencialmente activo — revisa antes de borrar">⚠️</span>';
    return `
      <tr class="cleanup-row${it.safe ? '' : ' cleanup-row-warn'}">
        <td><input type="checkbox" class="cleanup-cb" data-path="${escHtml(it.path)}" ${checked}></td>
        <td>${warnIcon}${escHtml(it.label)}</td>
        <td class="cleanup-path" title="${escHtml(it.path)}">${escHtml(it.path)}</td>
        <td class="cleanup-size">${_cleanupFmtBytes(it.size_bytes)}</td>
        <td class="cleanup-age">${_cleanupFmtAge(it.age_seconds)}</td>
        <td class="cleanup-reason">${escHtml(it.reason)}</td>
      </tr>`;
  }).join('');

  resultEl.innerHTML = `
    <div class="cleanup-summary">
      <strong>${data.total_count}</strong> elementos · liberables ${_cleanupFmtBytes(data.total_bytes)}
      ${data.safe_count < data.total_count
        ? ` · <span class="cleanup-warn-text">${data.total_count - data.safe_count} requieren revisión</span>`
        : ''}
    </div>
    <table class="cleanup-table">
      <thead>
        <tr>
          <th><input type="checkbox" id="cleanup-select-all" title="Seleccionar todo"></th>
          <th>Tipo</th>
          <th>Ruta</th>
          <th>Tamaño</th>
          <th>Edad</th>
          <th>Motivo</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="cleanup-actions">
      <button class="btn btn-ghost btn-sm" onclick="document.getElementById('settings-cleanup-result').innerHTML=''">Cancelar</button>
      <button class="btn btn-danger btn-sm" onclick="cleanupExecuteSelected()">🗑 Borrar seleccionados</button>
    </div>
  `;

  // Wire select-all
  const selectAll = document.getElementById('cleanup-select-all');
  if (selectAll) {
    selectAll.addEventListener('change', (e) => {
      const checked = e.target.checked;
      resultEl.querySelectorAll('.cleanup-cb').forEach(cb => { cb.checked = checked; });
    });
  }

  // Asegurar que el resultado es visible — la seccion puede quedar abajo del
  // body del modal y el usuario no verla si no hace scroll manualmente.
  resultEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function cleanupExecuteSelected() {
  const resultEl = document.getElementById('settings-cleanup-result');
  if (!resultEl) return;
  const checked = Array.from(resultEl.querySelectorAll('.cleanup-cb:checked'));
  const paths = checked.map(cb => cb.dataset.path).filter(Boolean);
  if (!paths.length) {
    showToast('No hay nada seleccionado', 'info');
    return;
  }
  // Confirmacion via modal nativo del proyecto
  showConfirm(
    `¿Borrar ${paths.length} elemento(s)?`,
    'Esta operación es irreversible. Asegúrate de no tener jobs activos sobre estos paths.',
    async () => {
      const data = await apiFetch('/api/cleanup/execute', {
        method: 'POST',
        body: JSON.stringify({ paths }),
      });
      if (!data) return;
      const okCount = (data.deleted || []).length;
      const koCount = (data.failed || []).length;
      const freed = _cleanupFmtBytes(data.total_freed_bytes || 0);
      if (koCount === 0) {
        showToast(`✓ Borrados ${okCount} elementos · liberados ${freed}`, 'success');
      } else {
        showToast(`Borrados ${okCount} · ${koCount} fallaron · liberados ${freed}`, 'warning');
      }
      // Re-escanear para refrescar el listado
      cleanupScanAndShow();
    },
    'Borrar',
  );
}

// Banner explicativo cuando el Repo DoviTools no está accesible. Cubre 3
// casos: falta folder URL (paywall), falta Google API key, o ambos. El
// primero es el más importante — el acceso al repo es privado (donación al
// autor) y merece explicación clara + link al PayPal.
function _cmv40RepoUnavailableBanner(repo) {
  const folderOk = !!(repo && repo.drive_folder_configured);
  const keyOk    = !!(repo && repo.google_key_configured);
  const openCfg = `<a href="#" onclick="openSettingsModal();return false">⚙︎ Configuración</a>`;
  const donate  = `<a href="https://www.paypal.com/donate/?hosted_button_id=6ML5KUZG9XGB6" target="_blank" rel="noreferrer">PayPal · REC_9999</a>`;
  if (!folderOk && !keyOk) {
    return `<div class="cmv40-repo-locked">
      <div class="cmv40-repo-locked-title">🔒 Repositorio DoviTools bloqueado</div>
      <div class="cmv40-repo-locked-body">
        Faltan <strong>dos cosas</strong>:
        <ol>
          <li><strong>URL del folder Drive del repo</strong> — es privado, requiere donación (15 CAD) en ${donate} indicando tu correo y pidiendo acceso al repositorio de RPUs. Recibirás el link por email.</li>
          <li><strong>Google API key</strong> con Drive API y Sheets API habilitadas.</li>
        </ol>
        Configura ambas en ${openCfg}.
      </div>
    </div>`;
  }
  if (!folderOk) {
    return `<div class="cmv40-repo-locked">
      <div class="cmv40-repo-locked-title">🔒 Repositorio DoviTools bloqueado</div>
      <div class="cmv40-repo-locked-body">
        La URL del folder Drive del repo de REC_9999 no está configurada. Es un repositorio <strong>privado</strong>: el acceso se obtiene donando 15 CAD en ${donate}, indicando tu correo y pidiendo acceso al repositorio de RPUs. Recibirás el link por email.
        <br><br>Una vez tengas el link, pégalo en ${openCfg} → sección <em>URL del repositorio DoviTools</em>.
      </div>
    </div>`;
  }
  if (!keyOk) {
    return `<div class="cmv40-repo-locked">
      <div class="cmv40-repo-locked-title">⚠️ Google API key no configurada</div>
      <div class="cmv40-repo-locked-body">
        La URL del repo está OK, pero falta la Google API key para consultar Drive. Configúrala en ${openCfg}.
      </div>
    </div>`;
  }
  return `<div class="cmv40-repo-locked">
    <div class="cmv40-repo-locked-title">⚠️ Repo DoviTools no accesible</div>
    <div class="cmv40-repo-locked-body">
      ${escHtml(repo?.error || 'Error desconocido')}
    </div>
  </div>`;
}

// ── Modal de Consulta rápida CMv4.0 (read-only, sin crear proyecto) ─

function openCMv40LookupModal() {
  const input = document.getElementById('cmv40-lookup-title');
  const yearInput = document.getElementById('cmv40-lookup-year');
  const results = document.getElementById('cmv40-lookup-results');
  if (input) input.value = '';
  if (yearInput) yearInput.value = '';
  if (results) results.innerHTML = '';
  openModal('cmv40-lookup-modal');
  setTimeout(() => input?.focus(), 60);
}

// ══════════════════════════════════════════════════════════════════
//  Modal de Ayuda / Manual CMv4.0
// ══════════════════════════════════════════════════════════════════

function openCMv40HelpModal() {
  openModal('cmv40-help-modal');
  // Recordar última sección abierta (o abrir "general" la primera vez)
  const last = sessionStorage.getItem('cmv40HelpSection') || 'general';
  _cmv40HelpSwitch(last);
}

// ── Limpieza masiva de artefactos CMv4.0 ────────────────────────────
// Modal accesible desde el header del tab CMv4.0 (boton al lado de
// Manual). Lista todos los proyectos con tamaño de workdir + estado y
// permite borrar varios a la vez. Tras el borrado los proyectos quedan
// archived (modo solo lectura) — no desaparecen del listado.

async function openCMv40CleanupModal() {
  openModal('cmv40-cleanup-modal');
  const body = document.getElementById('cmv40-cleanup-body');
  const foot = document.getElementById('cmv40-cleanup-foot');
  if (body) body.innerHTML = '<div class="cmv40-cleanup-loading">⏳ Escaneando proyectos…</div>';
  if (foot) foot.style.display = 'none';

  const data = await apiFetch('/api/cmv40/cleanup/preview');
  if (!data) return;
  if (!data.items || !data.items.length) {
    if (body) body.innerHTML = '<div class="cmv40-cleanup-empty">No hay proyectos CMv4.0 todavía.</div>';
    return;
  }
  if (data.deletable_count === 0) {
    if (body) {
      body.innerHTML = `
        <div class="cmv40-cleanup-empty">
          ✓ Nada que limpiar — los ${data.total_count} proyectos ya están archivados o tienen una fase en curso.
        </div>`;
    }
    return;
  }

  // Tabla con filas: checkbox, título, fase/estado, tamaño, motivo
  const rows = data.items.map((it) => {
    // Estado visual
    let stateBadge = '';
    if (it.state === 'running') stateBadge = '<span class="cleanup-state-pill running">⏳ En curso</span>';
    else if (it.state === 'archived') stateBadge = '<span class="cleanup-state-pill archived">🗃️ Archivado</span>';
    else if (it.state === 'done') stateBadge = '<span class="cleanup-state-pill done">✓ Done</span>';
    else if (it.state === 'error') stateBadge = '<span class="cleanup-state-pill error">⚠ Error</span>';
    else stateBadge = `<span class="cleanup-state-pill in-progress">⏸ ${escHtml(it.phase)}</span>`;

    const cb = it.safe_to_delete
      ? `<input type="checkbox" class="cmv40-cleanup-cb" data-id="${escHtml(it.id)}" data-size="${it.size_bytes}" checked>`
      : `<input type="checkbox" class="cmv40-cleanup-cb" data-id="${escHtml(it.id)}" data-size="${it.size_bytes}" disabled title="${escHtml(it.reason)}">`;

    const sizeStr = it.size_bytes > 0 ? _cleanupFmtBytes(it.size_bytes) : '—';
    const filesStr = it.files_count > 0 ? `${it.files_count} fichero${it.files_count === 1 ? '' : 's'}` : '';

    return `
      <tr class="cmv40-cleanup-row${it.safe_to_delete ? '' : ' cmv40-cleanup-row-disabled'}">
        <td>${cb}</td>
        <td class="cmv40-cleanup-title-cell">
          <div class="cmv40-cleanup-title">${escHtml(it.title)}</div>
          <div class="cmv40-cleanup-subline">${stateBadge}</div>
        </td>
        <td class="cmv40-cleanup-size-cell">
          <div class="cleanup-size">${sizeStr}</div>
          ${filesStr ? `<div class="cmv40-cleanup-files">${filesStr}</div>` : ''}
        </td>
        <td class="cmv40-cleanup-reason">${escHtml(it.reason)}</td>
      </tr>`;
  }).join('');

  body.innerHTML = `
    <div class="cmv40-cleanup-warn">
      <strong>⚠️ Atención:</strong> esta acción es <strong>irreversible</strong>. Tras borrar los artefactos, los proyectos quedan en modo <strong>solo lectura</strong> — no se podrán rehacer fases ni reanudar pipelines abiertos. El JSON de la sesión y el log se preservan; solo se borran los HEVC/RPU/MKV.tmp del workdir.
    </div>
    <table class="cmv40-cleanup-table">
      <thead>
        <tr>
          <th><input type="checkbox" id="cmv40-cleanup-select-all" title="Seleccionar todo"></th>
          <th>Proyecto</th>
          <th>Tamaño</th>
          <th>Detalle</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;

  // Wire select-all (solo afecta a checkboxes habilitados)
  const selectAll = document.getElementById('cmv40-cleanup-select-all');
  if (selectAll) {
    // Estado inicial: marcado si todos los habilitados están marcados
    const enabledCbs = body.querySelectorAll('.cmv40-cleanup-cb:not(:disabled)');
    selectAll.checked = enabledCbs.length > 0 &&
      Array.from(enabledCbs).every(cb => cb.checked);
    selectAll.addEventListener('change', (e) => {
      body.querySelectorAll('.cmv40-cleanup-cb:not(:disabled)').forEach(cb => {
        cb.checked = e.target.checked;
      });
      _cmv40CleanupUpdateSummary();
    });
  }
  // Refresh summary cuando cambia cualquier checkbox
  body.querySelectorAll('.cmv40-cleanup-cb').forEach(cb => {
    cb.addEventListener('change', _cmv40CleanupUpdateSummary);
  });

  foot.style.display = '';
  _cmv40CleanupUpdateSummary();
}

function _cmv40CleanupUpdateSummary() {
  const cbs = document.querySelectorAll('.cmv40-cleanup-cb:checked');
  const count = cbs.length;
  let totalBytes = 0;
  cbs.forEach(cb => { totalBytes += parseInt(cb.dataset.size || '0', 10) || 0; });
  const summaryEl = document.getElementById('cmv40-cleanup-summary');
  const btn = document.getElementById('cmv40-cleanup-execute-btn');
  if (summaryEl) {
    summaryEl.innerHTML = count > 0
      ? `<strong>${count}</strong> proyecto${count === 1 ? '' : 's'} · liberables <strong>${_cleanupFmtBytes(totalBytes)}</strong>`
      : '<span style="color:var(--text-3)">Selecciona al menos un proyecto</span>';
  }
  if (btn) {
    btn.disabled = count === 0;
  }
}

async function cmv40BulkCleanupExecute() {
  const cbs = Array.from(document.querySelectorAll('.cmv40-cleanup-cb:checked'));
  const ids = cbs.map(cb => cb.dataset.id).filter(Boolean);
  if (!ids.length) {
    showToast('No hay nada seleccionado', 'info');
    return;
  }
  showConfirm(
    `Borrar artefactos de ${ids.length} proyecto${ids.length === 1 ? '' : 's'}?`,
    'Esta acción es irreversible. Los proyectos pasarán a modo SOLO LECTURA — no se podrán rehacer fases ni reanudar pipelines abiertos. El JSON de la sesión y el log se conservan; solo se borra el workdir intermedio (HEVC, RPU.bin, .mkv.tmp).',
    async () => {
      const data = await apiFetch('/api/cmv40/cleanup/bulk', {
        method: 'POST',
        body: JSON.stringify({ session_ids: ids }),
      });
      if (!data) return;
      const okCount = (data.deleted || []).length;
      const skipCount = (data.skipped || []).length;
      const koCount = (data.failed || []).length;
      const freed = _cleanupFmtBytes(data.total_freed_bytes || 0);
      let msg = `🗃️ ${okCount} proyecto${okCount === 1 ? '' : 's'} archivado${okCount === 1 ? '' : 's'} · liberados ${freed}`;
      if (skipCount > 0) msg += ` · ${skipCount} omitido${skipCount === 1 ? '' : 's'} (en curso)`;
      if (koCount > 0)   msg += ` · ${koCount} fallido${koCount === 1 ? '' : 's'}`;
      showToast(msg, koCount === 0 ? 'success' : 'warning');
      // Refrescar el sidebar y los proyectos abiertos para reflejar el nuevo
      // estado archived (banner solo-lectura, etc).
      try { refreshCMv40Sidebar(); } catch (_) {}
      for (const id of (data.deleted || []).map(d => d.id)) {
        try { _refreshCMv40Session(id); } catch (_) {}
      }
      // Re-escanear preview para refrescar la tabla del modal
      openCMv40CleanupModal();
    },
    'Borrar',
  );
}

function _cmv40HelpSwitch(section) {
  sessionStorage.setItem('cmv40HelpSection', section);
  document.querySelectorAll('.cmv40-help-nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.section === section);
  });
  const content = document.getElementById('cmv40-help-content');
  if (!content) return;
  const html = _CMV40_HELP_SECTIONS[section] || '<p>Sección no encontrada.</p>';
  content.innerHTML = html;
  content.scrollTop = 0;

  // Hidrataciones post-render (nodos que dependen de estado live)
  if (section === 'sheet') _cmv40HelpHydrateSheetLink();
  if (section === 'repo')  _cmv40HelpHydrateDriveLink();
}

/** Hidrata el enlace "Hoja en uso" al abrir la sección Sheet del manual.
 *  Lee /api/settings y rellena el <a> con la URL efectiva (configurada o
 *  default). Añade un meta línea con la procedencia (settings/env/default). */
async function _cmv40HelpHydrateSheetLink() {
  const anchor = document.getElementById('help-sheet-link-anchor');
  const metaEl = document.getElementById('help-sheet-link-meta');
  if (!anchor) return;
  try {
    const s = await apiFetch('/api/settings');
    const sh = s?.sheet || {};
    const url = sh.url || sh.default_url || '';
    if (!url) {
      anchor.textContent = 'URL no disponible';
      anchor.removeAttribute('href');
      return;
    }
    anchor.href = url;
    anchor.textContent = url;
    if (metaEl) {
      const srcLabel = sh.source === 'settings' ? 'URL personalizada (Configuración)'
        : sh.source === 'env'      ? 'URL de variable de entorno'
        : 'URL por defecto de la comunidad DoviTools';
      metaEl.textContent = sh.is_default
        ? 'URL por defecto de la comunidad DoviTools — la puedes cambiar en ⚙︎ Configuración'
        : srcLabel;
    }
  } catch (_) {
    anchor.textContent = 'No se ha podido cargar la URL';
    anchor.removeAttribute('href');
  }
}

/** Hidrata el bloque "Carpeta Drive en este servidor" al abrir la sección Repo.
 *  Lee /api/settings y muestra si el folder está configurado, su origen
 *  (settings / env / ninguno) y el sufijo del folder_id como confirmación. */
async function _cmv40HelpHydrateDriveLink() {
  const statusEl = document.getElementById('help-drive-link-status');
  const metaEl   = document.getElementById('help-drive-link-meta');
  if (!statusEl) return;
  try {
    const s = await apiFetch('/api/settings');
    const df = s?.drive_folder || {};
    const apiKey = s?.google || {};
    if (df.configured) {
      statusEl.innerHTML = `✓ Configurada <span style="font-size:11px; font-weight:500; color:var(--text-3)">(folder …${escHtml(df.folder_id_last6 || '??????')})</span>`;
      statusEl.style.color = '#0e6b2a';
      const srcLabel = df.source === 'settings' ? 'configurada desde ⚙︎ Configuración'
        : df.source === 'env' ? 'configurada por variable de entorno del contenedor'
        : 'configurada';
      const apiKeyState = apiKey.configured ? 'API key ✓' : 'API key ✗ sin configurar — imprescindible';
      if (metaEl) metaEl.textContent = `${srcLabel} · ${apiKeyState}`;
    } else {
      statusEl.innerHTML = `⚠️ No configurada`;
      statusEl.style.color = '#8a4a00';
      if (metaEl) metaEl.textContent = 'Sigue los pasos de abajo para habilitar el acceso al repo DoviTools';
    }
  } catch (_) {
    statusEl.textContent = 'No se ha podido consultar el estado';
    if (metaEl) metaEl.textContent = '—';
  }
}

/**
 * Contenido de las secciones del manual. v1 — se irá iterando con el usuario.
 * Datos validados contra el código real del pipeline (inventario de audit).
 * Marcado con `help-unverified` lo que requiera research externa.
 */
const _CMV40_HELP_SECTIONS = {

  // ═══════════════════════════════════════════════════════════════
  // GENERAL — Conceptos clave
  // ═══════════════════════════════════════════════════════════════
  general: `
    <h1>🧠 Conceptos clave de Dolby Vision</h1>
    <p class="cmv40-help-lead">Qué son BL, EL, los profiles DV, las versiones CM y los niveles. Todos los datos contrastados con fuentes primarias (dovi_tool, Dolby Professional, Netflix Partner docs, Wikipedia).</p>

    <div class="help-subtoc">
      <b>En esta sección</b>
      <a href="#g-layers">Capas BL + EL + RPU</a>
      <a href="#g-felmel">FEL vs MEL</a>
      <a href="#g-profiles">Profiles</a>
      <a href="#g-cm">CMv2.9 vs CMv4.0</a>
      <a href="#g-levels">Niveles L0-L11</a>
    </div>

    <h2 id="g-layers">🎞️ Las tres piezas de Dolby Vision: BL, EL y RPU</h2>
    <p>Un stream Dolby Vision tiene siempre <strong>un vídeo HEVC base</strong> + <strong>metadata de tone-mapping</strong>. Algunos profiles añaden una capa extra.</p>
    <table>
      <tr><th>Pieza</th><th>Qué es</th><th>Tamaño típico</th></tr>
      <tr><td><strong>BL</strong> · Base Layer</td><td>Vídeo HEVC 10-bit. En profiles 7/8 es HDR10 <em>válido</em> (se ve bien en cualquier TV HDR). En profile 5 es IPT/ICtCp propietario — solo se ve bien con aparato DV.</td><td>30-50 GB (2h UHD)</td></tr>
      <tr><td><strong>EL</strong> · Enhancement Layer</td><td>Solo en profiles dual-layer (4/7). Contiene <em>residuals de color y luma</em> que al combinarse con el BL reconstruyen un grading interno de hasta <strong>12-bit 4:2:0</strong>.</td><td>MEL: despreciable · FEL: 10-15% del BL (típicamente 1-6 Mbps)</td></tr>
      <tr><td><strong>RPU</strong> · Reference Processing Unit</td><td>Metadata de tone-mapping dinámico por escena (L0-L11). Se interleva como NAL units en el stream HEVC. Es el "cerebro" DV.</td><td>&lt; 10 MB típicamente</td></tr>
    </table>
    <div class="help-callout help-callout-info">
      <strong>Corrección importante:</strong> la leyenda de que "FEL alcanza 4000 nits y MEL solo 1000" es un <em>malentendido comunitario</em> — ambos transportan el mismo rango PQ 0-10.000 nits en L1. La diferencia real de FEL es <strong>precisión de color y gradientes</strong> (bits efectivos, no techo de brillo).
    </div>

    <h2 id="g-felmel">🔍 FEL vs MEL — la diferencia real</h2>
    <table>
      <tr><th>Variante</th><th>Contenido del EL</th><th>Aporta</th></tr>
      <tr><td><span class="help-pill help-pill-fel">FEL</span> · Full EL</td><td>Residuals reales de luma y croma, punto por punto frente al BL.</td><td>Reconstrucción 12-bit 4:2:0. Gradientes más finos, menos banding, mejor croma en escenas saturadas.</td></tr>
      <tr><td><span class="help-pill help-pill-mel">MEL</span> · Minimal EL</td><td>EL "vacío" — solo metadatos estructurales con offsets cero.</td><td>Nada perceptible. Funcionalmente equivalente a un Profile 8.1 con overhead de container.</td></tr>
    </table>
    <div class="help-callout help-callout-warning">
      <strong>Por qué la reproducción de FEL es un tema delicado:</strong>
      <p style="margin:6px 0 0">Procesar FEL de verdad significa <em>combinar</em> BL + EL frame a frame y aplicar el RPU con tone-mapping dinámico en tiempo real. Es computacionalmente más costoso que HDR10 o DV single-layer, y <strong>requiere licencia Dolby</strong> (Dolby no libera el decoder — cada fabricante integra una SDK cerrada). Eso explica por qué la mayoría del ecosistema streaming (Apple TV 4K, NVIDIA Shield, FireTV) <em>ni siquiera acepta Profile 7</em>: Dolby no licencia P7 para apps genéricas — lo reserva a reproductores Blu-ray certificados y a algunos hardware dedicados.</p>
    </div>

    <div class="help-callout help-callout-success">
      <strong>Quién reproduce FEL correctamente en la práctica:</strong>
      <table style="margin-top:8px; width:100%">
        <tr><th>Categoría</th><th>Ejemplos</th><th>Notas</th></tr>
        <tr><td><strong>Reproductores UHD Blu-ray oficiales</strong></td><td>Panasonic UB820/9000, Sony UBP-X800M2/X1100ES, Pioneer UDP-LX800</td><td>La vía original — Dolby los certifica específicamente para P7 FEL desde disco.</td></tr>
        <tr><td><strong>Reproductores Chinos "Chinoppo"</strong></td><td>Reavon UBR-X100/X110/X200, Magnetar UDP800/900, Pioneer LX500 (chip Mediatek)</td><td>Clones de la plataforma OPPO UDP-205 descontinuada. Reproducen FEL desde ISO o disco físico sin problema. <strong>Requieren ISO completa</strong> — no rippeo MKV.</td></tr>
        <tr><td><strong>Amlogic + CoreELEC (FEL-aware)</strong></td><td>Ugoos AM6B+, AM6B Plus, Homatics Box R 4K Plus; SoCs S905X4/S922X/S922X-J/Z licenciados por Dolby</td><td>La vía más flexible para MKV: CoreELEC NG 20.5+/21+ procesa FEL real frame a frame sobre MKVs P7. Sin licencia Dolby en SoC no funciona — por eso NO todos los boxes Amlogic valen.</td></tr>
        <tr><td><strong>Hardware dedicado premium</strong></td><td>Kaleidescape Strato V / Terra / Alto</td><td>Servidor multiroom profesional con licencia Dolby completa.</td></tr>
        <tr><td><strong>Algunos TVs OLED directamente</strong></td><td>Panasonic GZ2000 (2019) y OLED Panasonic posteriores (JZ, LZ, MZ, Z95)</td><td>Panasonic fue el primer fabricante en incluir decoder FEL real en un TV de consumo. LG y Sony <strong>no</strong> procesan FEL en el TV — dependen del reproductor.</td></tr>
      </table>
    </div>

    <div class="help-callout help-callout-info">
      <strong>Nota importante sobre los boxes que "aceptan P7 pero descartan el EL":</strong> esto sigue siendo cierto para Zidoo/Dune y para <em>Amlogic sin CoreELEC-NG reciente</em>. Reproducen BL + RPU (equivalente a P8.1), lo cual se ve bien pero pierde la precisión de color del EL. La diferencia con los boxes FEL-aware es exactamente esa: procesan el EL o no. Si tienes una Ugoos AM6B+ con CoreELEC NG actualizado, estás en el grupo que sí procesa.
    </div>

    <h2 id="g-profiles">🎯 Profiles Dolby Vision — matriz completa</h2>
    <p>Un <strong>Profile</strong> en Dolby Vision es el <em>"formato de empaquetado"</em> del stream: describe cómo están organizadas las capas (single vs dual-layer), qué codec se usa (HEVC o AV1), qué color space tiene la Base Layer (HDR10, HLG, SDR, IPT propietario), y cómo viaja el RPU. No es una "calidad" — un profile no es mejor que otro en abstracto. Lo que cambia es el <em>caso de uso</em>: cada ecosistema (UHD Blu-ray, streaming, broadcast, móvil) adopta los profiles que encajan con sus restricciones de ancho de banda, compatibilidad y licencia.</p>
    <p>Conocer el profile de un fichero determina tres cosas prácticas: <strong>(1)</strong> si tu reproductor lo puede entender; <strong>(2)</strong> si se ve correctamente en un display no-DV (solo los profiles con BL válida HDR10/SDR/HLG son retro-compatibles); <strong>(3)</strong> qué pipeline de upgrade CMv4.0 tiene sentido (ej. P7 FEL se puede upgradear conservando el EL; P5 no tiene sentido upgradear porque la BL es propietaria).</p>
    <table>
      <tr><th>Profile</th><th>Tipo</th><th>Retrocompatibilidad</th><th>Uso típico</th></tr>
      <tr><td><span class="help-pill help-pill-p5">5</span></td><td>Single-layer HEVC 10-bit (IPT/ICtCp)</td><td><strong>Ninguna</strong> — fuera de aparato DV se ve verdoso</td><td>Netflix, iTunes, Disney+ (antiguo)</td></tr>
      <tr><td><span class="help-pill help-pill-p7">7 FEL</span></td><td>Dual-layer (BL + FEL + RPU)</td><td>BL es HDR10 válido</td><td><strong>UHD Blu-ray FEL</strong> (Paramount, Universal, Lionsgate, etc.)</td></tr>
      <tr><td><span class="help-pill help-pill-p7">7 MEL</span></td><td>Dual-layer (BL + MEL + RPU)</td><td>BL es HDR10 válido</td><td>UHD Blu-ray sin FEL real (la mayoría 2016-2020)</td></tr>
      <tr><td><span class="help-pill help-pill-p8">8.1</span></td><td>Single-layer HEVC + RPU in-band</td><td>BL es HDR10 válido</td><td>Streaming moderno (Apple TV+, Disney+, Netflix reciente)</td></tr>
      <tr><td><strong>8.2</strong></td><td>Single-layer HEVC + RPU</td><td>BL es SDR BT.709</td><td>Flujos profesionales — raro en consumo</td></tr>
      <tr><td><strong>8.4</strong></td><td>Single-layer HEVC + RPU</td><td>BL es HLG</td><td>iPhone 12+ grabando DV, broadcast HLG</td></tr>
      <tr><td><strong>10 / 10.1 / 10.4</strong></td><td>Single-layer <strong>AV1</strong> + RPU</td><td>none / HDR10 / HLG respectivamente</td><td>Apple HLS (2024+). Empezando a aparecer en streaming AV1.</td></tr>
      <tr><td>4 (legacy)</td><td>Dual-layer HEVC con BL SDR BT.709</td><td>Sí a SDR</td><td>Legacy de mastering. No se ve en consumo actual.</td></tr>
    </table>
    <div class="help-callout help-callout-info">
      <strong>Nota técnica:</strong> Profile 7 <em>no es oficialmente válido</em> en containers .mkv ni .mp4 según Dolby — el contenedor nativo es UHD Blu-ray (.m2ts). mkvmerge lo acepta por <strong>convención de la comunidad</strong>, y todo el ecosistema open-source (dovi_tool, MadVR, Jellyfin) ha adoptado esa convención.
    </div>

    <h2 id="g-cm">📐 CM Versions — v2.9 y v4.0</h2>
    <p>El <strong>Content Mapping</strong> es el algoritmo que traduce el master HDR al rango dinámico del TV final. Es la metadata que le dice al TV cómo comprimir 4000+ nits del master a los ~700-2000 nits que maneja.</p>
    <table>
      <tr><th>Versión</th><th>Introducida</th><th>Niveles incluidos</th><th>Cambios clave</th></tr>
      <tr><td><span class="help-pill help-pill-cm29">CMv2.9</span></td><td>Inicial (2014-2015)</td><td>L0-L6</td><td>Tone-mapping "clásico" por target 100/300/600/1000 nits vía L2</td></tr>
      <tr><td><span class="help-pill help-pill-cm40">CMv4.0</span></td><td>Otoño 2018 (docs oficiales Dic 2019)</td><td>L0-L6 + <strong>L3, L8, L9, L10, L11</strong></td><td>L8 reemplaza funcionalmente a L2 con 8 parámetros finos. L3 añade offsets dinámicos a L1. L11 añade "Content Type" (Movie/Game/Sport/UGC) → Dolby Vision IQ.</td></tr>
    </table>
    <div class="help-callout help-callout-success">
      <strong>CMv4.0 es superset de CMv2.9, no reemplazo:</strong> un RPU CMv4.0 sigue conteniendo L0-L6 completos. Los TVs sin engine CMv4.0 <em>ignoran silenciosamente L8-L11</em> y usan L1+L2 como siempre — no hay fallo, solo no aprovechan el refinamiento.
    </div>
    <div class="help-callout help-callout-warning">
      <strong>Adopción en discos:</strong> la mayoría de UHD BDs <em>pre-2020</em> son CMv2.9. Estudios recientes varían — incluso en 2024-2025 se siguen publicando BDs CMv2.9. Ahí es donde el <strong>upgrade CMv4.0</strong> tiene sentido: el BD FEL se mantiene y solo se sustituye el RPU.
    </div>

    <h2 id="g-levels">📊 Niveles L0-L11 — especificación verificada</h2>
    <table>
      <tr><th>Nivel</th><th>Nombre</th><th>CM</th><th>Obligatorio</th><th>Función</th></tr>
      <tr><td><strong>L0</strong></td><td>Mastering & Target Display Characteristics</td><td>v2.9 + v4.0</td><td>Sí</td><td>Estático. Info del mastering display, aspect ratio, frame rate, algoritmo/trim version.</td></tr>
      <tr><td><strong>L1</strong></td><td>Image Character Analysis (Min/Mid/Max)</td><td>v2.9 + v4.0</td><td><strong>Sí (todos los shots)</strong></td><td>Dinámico. Tres valores por shot (min, mid, max) en espacio LMS. Es la base del tone-mapping.</td></tr>
      <tr><td><strong>L2</strong></td><td>Trims retrocompatibles por target display</td><td>v2.9 + v4.0</td><td>Opcional</td><td>Dinámico. Trims por shot (Lift, Gain, Gamma, Saturation, Chroma Weight) para displays 100/300/600/1000/2000/4000 nits.</td></tr>
      <tr><td><strong>L3</strong></td><td>L1 offsets</td><td><strong>v4.0</strong></td><td>Opcional</td><td>Dinámico. Offsets Min/Mid/Max que se suman a L1.</td></tr>
      <tr><td><strong>L4</strong></td><td>Smoothing filters</td><td>v2.9 + v4.0</td><td>Opcional</td><td>Dinámico. Suavizado entre shots. Poco usado por coloristas en la práctica.</td></tr>
      <tr><td><strong>L5</strong></td><td>Aspect Ratio / Active Area</td><td>v2.9 + v4.0</td><td>Opcional</td><td>Dinámico. Canvas + offsets left/right/top/bottom. <strong>Crítico en CinemaScope para que el TV no clipee luminancia en barras negras.</strong></td></tr>
      <tr><td><strong>L6</strong></td><td>MaxCLL / MaxFALL (ST.2086)</td><td>v2.9 + v4.0</td><td>Opcional (recomendado para HDR10 fallback)</td><td>Estático. Los mismos valores que HDR10 embebe.</td></tr>
      <tr><td><strong>L7</strong></td><td><em>No existe en documentación pública</em></td><td>—</td><td>—</td><td>Ni Wikipedia, Netflix, Dolby Pro ni dovi_tool lo enumeran. Probable reservado/no usado.</td></tr>
      <tr><td><strong>L8</strong></td><td>Advanced Trims (reemplaza L2 en v4.0)</td><td><strong>v4.0</strong></td><td>Opcional</td><td>Dinámico. 8 parámetros: <code>slope, offset, power, chroma, saturation, ms</code> (mid-contrast), <code>mid, clip</code>. Mucho más rico que L2.</td></tr>
      <tr><td><strong>L9</strong></td><td>Source Content Mastering Display Primaries</td><td><strong>v4.0</strong></td><td>Opcional</td><td>Dinámico. Primarias + white point del mastering display por shot.</td></tr>
      <tr><td><strong>L10</strong></td><td>Target Display Mastering Primaries</td><td><strong>v4.0</strong></td><td>Opcional</td><td>Dinámico. Contrapartida a L9 para target display. <em>Documentación pública escasa</em> — dovi_tool lo preserva pero Dolby no ha publicado el spec completo.</td></tr>
      <tr><td><strong>L11</strong></td><td>Content Type (Dolby Vision IQ)</td><td><strong>v4.0</strong></td><td>Opcional</td><td>Dinámico. Valores: 0=Default, 1=Movies, 2=Game, 3=Sport, 4=UGC. Activa perfiles de post-procesado en TV (<em>Filmmaker Mode</em>, <em>Game Mode</em>, etc.).</td></tr>
    </table>
    <div class="help-callout help-callout-info">
      <strong>Qué revisa la app de estos niveles:</strong> antes de empezar el upgrade, la app compara automáticamente los niveles <strong>L1, L5 y L6</strong> entre tu Blu-ray y el bin target. Si coinciden lo suficiente, el upgrade se hace en modo automático. Si no, te lleva a revisión visual (lo verás con detalle en la sección <em>Pipelines</em>).
    </div>

    <div class="help-callout help-callout-warning">
      <strong>L1 max_pq ≠ luminancia que verás en pantalla.</strong> El gráfico "Perfil de luminancia DV L1" del tab <em>Consultar / Editar MKV</em> muestra el <code>max_pq</code> codificado por el colorista en la metadata DV — es lo que el RPU dice que tiene la escena, no lo que efectivamente se reproduce. Algunos discos están etiquetados muy conservadoramente (Blade Runner 2049 reporta peak L1 ~176 nits aunque medidas reales en pantalla muestren ~600 nits). El TV aplica tone-mapping y los trims L2/L8 antes de mostrar cada frame. Por eso la cifra del gráfico puede parecer baja para un máster HDR — está reflejando fielmente la metadata, no es un bug.
    </div>

    <div class="help-sources">
      <b>Fuentes</b>
      <a href="https://en.wikipedia.org/wiki/Dolby_Vision" target="_blank" rel="noreferrer">Wikipedia: Dolby Vision</a> ·
      <a href="https://github.com/quietvoid/dovi_tool/blob/main/README.md" target="_blank" rel="noreferrer">quietvoid/dovi_tool README</a> ·
      <a href="https://github.com/quietvoid/dovi_tool/blob/main/docs/editor.md" target="_blank" rel="noreferrer">dovi_tool editor.md</a> ·
      <a href="https://professional.dolby.com/siteassets/pdfs/dolby_vision_best-practices_colorgrading_v4.pdf" target="_blank" rel="noreferrer">Dolby Best Practices v4.0 (PDF)</a> ·
      <a href="https://partnerhelp.netflixstudios.com/hc/en-us/articles/360058735254-Dolby-Vision-Metadata-Overview" target="_blank" rel="noreferrer">Netflix Partner Help — DV Metadata</a> ·
      <a href="https://www.veneratech.com/hdr-dolby-vision-meta-data-parameters-to-validate-content" target="_blank" rel="noreferrer">Venera Tech HDR Insights #4</a> ·
      <a href="https://professionalsupport.dolby.com/s/article/Dolby-Vision-IQ-Content-Type-Metadata-L11" target="_blank" rel="noreferrer">Dolby — L11 Content Type</a> ·
      <a href="https://avdisco.com/t/demystifying-dolby-vision-profile-levels-dolby-vision-levels-mel-fel/95" target="_blank" rel="noreferrer">avdisco — Demystifying DV Profiles/Levels</a>
    </div>
  `,

  // ═══════════════════════════════════════════════════════════════
  // POR QUÉ UPGRADE
  // ═══════════════════════════════════════════════════════════════
  'why-upgrade': `
    <h1>💡 Por qué hacer upgrade a CMv4.0</h1>
    <p class="cmv40-help-lead">El objetivo: combinar el <strong>vídeo del UHD Blu-ray</strong> (la mejor calidad de imagen disponible) con el <strong>tone-mapping CMv4.0</strong> (típicamente extraído de versiones streaming con remaster reciente). Hay que entender qué se gana, qué no, y en qué TVs merece la pena antes de invertir horas.</p>

    <div class="help-subtoc">
      <b>En esta sección</b>
      <a href="#w-gain">Qué se gana exactamente</a>
      <a href="#w-levels">Los niveles (L) que marcan la diferencia</a>
      <a href="#w-static-vs-runtime">Upgrade estático vs conversión en tiempo real</a>
      <a href="#w-tvs">TVs que realmente lo aprovechan</a>
      <a href="#w-lldv">El caso LLDV (proyectores, Shield, HDFury)</a>
      <a href="#w-decide">Árbol de decisión</a>
    </div>

    <h2 id="w-gain">🎯 Qué se gana exactamente (y qué no)</h2>
    <p>El Blu-ray UHD es la mejor fuente de vídeo que puedes tener hoy en casa. Lo que no siempre es lo mejor es el <em>conjunto de instrucciones</em> que lo acompaña (el RPU) para decirle a tu TV cómo adaptar la imagen a sus capacidades. Muchos Blu-ray se masterizaron antes de 2018 con CMv2.9 — un estándar menor, con menos precisión y con bugs conocidos. CMv4.0 es la evolución: misma imagen base, mejores instrucciones de tone-mapping. El upgrade sustituye <em>solo</em> esas instrucciones.</p>
    <ul>
      <li><strong>Tone-mapping adaptativo más fino</strong> en TVs CMv4.0-aware: el nivel L8 amplía a L2 con 8 parámetros (slope, offset, power, chroma weight, saturation, mid-contrast, mid, clip) — mejor precisión en mid-tones y clipping controlado de highlights.</li>
      <li><strong>Corrección de bugs específicos de CMv2.9</strong>: CMv4.0 arregla el bug de sobrebrillo con EDID 1000-nit (TVs de brillo moderado perdían detalle en highlights) y el bug de Chroma Weight en trims.</li>
      <li><strong>Metadata de tipo de contenido (L11)</strong>: Dolby Vision IQ — el TV puede activar perfiles de post-procesado automáticamente según el tipo de material (Filmmaker Mode para cine, Game Mode, etc.).</li>
      <li><strong>Se preserva el vídeo del Blu-ray intacto</strong>: ni el HEVC ni la Enhancement Layer se re-encodan. Solo se sustituye el RPU (la metadata) — cero pérdida de calidad de imagen base.</li>
    </ul>
    <div class="help-callout help-callout-warning">
      <strong>Qué NO es cierto (aunque se repite):</strong>
      <br>· <em>"CMv4.0 añade brillo"</em> → no. El rango PQ 0-10.000 nits está en L1 igual en ambas versiones. Lo que cambia es cómo se mapea, no el rango.
      <br>· <em>"Mejora dramática visible"</em> → no en todos los TVs. En TVs pre-2019 es indistinguible (el engine ignora L8-L11). En OLED tope de gama 2023+ la diferencia existe, pero es sutil-notable, no obvia.
      <br>· <em>"CMv4.0 arregla el grading"</em> → tampoco. Si el master original tenía un problema de color, CMv4.0 no lo corrige. Corrige la <em>adaptación</em> al display.
    </div>

    <h2 id="w-levels">📐 Los niveles (L) que marcan la diferencia</h2>
    <p>Un RPU contiene instrucciones organizadas en niveles numerados (L0, L1, L2…). Cada nivel describe un aspecto distinto del tone-mapping. CMv2.9 tiene L0, L1, L2, L4, L5, L6. CMv4.0 es <strong>superset</strong>: mantiene todos los de v2.9 y añade L3, L8, L9, L10 y (más tarde) L11. Estos son los que importan para el upgrade:</p>
    <table>
      <tr><th>Nivel</th><th>Qué hace</th><th>Por qué mejora con v4.0</th></tr>
      <tr><td><strong>L1</strong> <span class="help-pill help-pill-mel">común v2.9 + v4.0</span></td><td>MaxCLL/MaxFALL dinámico por escena. Guía principal del tone-mapping.</td><td>Mismo en ambas versiones. No es donde está la ganancia.</td></tr>
      <tr><td><strong>L2</strong> <span class="help-pill help-pill-mel">común v2.9 + v4.0</span></td><td>Trim por target display: slope/offset/power (lift/gamma/gain) en hasta 9 niveles de pico de brillo distintos (100, 600, 1000 nits, etc.).</td><td>Sigue existiendo en v4.0 como <em>fallback</em> para TVs sin engine v4.0. En engine v4.0 se deriva automáticamente de L8 (y por eso los MKVs CMv4.0 en TVs viejas siguen funcionando).</td></tr>
      <tr><td><strong>L3</strong> <span class="help-pill help-pill-cm40">nuevo en v4.0</span></td><td>Ajuste local de L1 por escena específica.</td><td>Permite al colorista afinar escenas concretas sin tocar el resto.</td></tr>
      <tr><td><strong>L5</strong> <span class="help-pill help-pill-mel">común v2.9 + v4.0</span></td><td>Área activa (letterbox) — indica al TV la zona real de imagen.</td><td>Clave para los trust gates: si el bin tiene otro L5, es otro corte.</td></tr>
      <tr><td><strong>L6</strong> <span class="help-pill help-pill-mel">común v2.9 + v4.0</span></td><td>MaxCLL/MaxFALL estáticos (HDR10 fallback).</td><td>Mismo en ambas versiones.</td></tr>
      <tr><td><strong>L8</strong> <span class="help-pill help-pill-cm40">nuevo en v4.0 — el grande</span></td><td>Trim ampliado con 8 parámetros: slope, offset, power, <strong>chroma weight</strong>, <strong>saturation</strong>, <strong>mid-contrast</strong>, <strong>mid point</strong>, <strong>clip</strong>.</td><td>Permite trims con <em>mucho</em> más control que L2. El chroma weight corrige el bug de saturación que arrastraba v2.9 en trims agresivos. Es <strong>la razón principal</strong> del upgrade.</td></tr>
      <tr><td><strong>L9</strong> <span class="help-pill help-pill-cm40">nuevo en v4.0</span></td><td>Gamut source primaries (qué espacio de color usa el master: Rec.709, P3, Rec.2020).</td><td>En v2.9 el TV tenía que asumir. Con L9, el TV sabe con certeza el gamut origen y adapta mejor.</td></tr>
      <tr><td><strong>L10</strong> <span class="help-pill help-pill-cm40">nuevo en v4.0</span></td><td>Target display primaries (qué espacio reproduce el target).</td><td>Permite mapeo más preciso cuando el TV tiene gamut limitado.</td></tr>
      <tr><td><strong>L11</strong> <span class="help-pill help-pill-cm40">añadido en v4.0 (2020+)</span></td><td>Content Type — señaliza "película", "deporte", "animación", "HDR game", etc.</td><td>Activa Dolby Vision IQ: el TV ajusta post-procesado (motion, sharpening) automáticamente.</td></tr>
    </table>
    <div class="help-callout help-callout-success">
      <strong>El nivel decisivo es L8.</strong> Si un bin se etiqueta como CMv4.0 pero no contiene L8 (pasa con bins "CMv4.0 vacíos" que solo renombran niveles), la app lo rechaza en Fase B — no aporta sobre el Blu-ray original. Esto es exactamente uno de los trust gates críticos.
    </div>
    <div class="help-callout help-callout-info">
      <strong>Un detalle técnico bonito:</strong> CMv4.0 es <em>hacia atrás compatible</em>. Un MKV CMv4.0 se reproduce sin fallos en una TV CMv2.9 — el engine antiguo ignora los niveles que no entiende (L3, L8-L11) y usa L1+L2 como siempre. Por eso el upgrade nunca "rompe" nada aunque tu TV sea vieja. Simplemente no aprovecha lo nuevo.
    </div>

    <h2 id="w-static-vs-runtime">⚡ Upgrade estático (esta app) vs conversión procedural en tiempo real (CoreELEC)</h2>
    <p>Existen dos caminos para pasar un Blu-ray CMv2.9 a CMv4.0, y son <strong>radicalmente distintos</strong> en qué hacen y qué consiguen. Conviene entenderlos bien antes de elegir.</p>

    <h3>🎯 Upgrade estático con transferencia — lo que hace esta app</h3>
    <p>Esta app reemplaza permanentemente el RPU del MKV por uno CMv4.0 <strong>auténtico</strong>, transferido desde una fuente externa firmada por colorista (WEB-DL retail, bin del repo DoviTools). Los niveles L3/L8-L11 que acaban en el MKV son reales — con valores artísticos, trims por escena y primaries de colorimetría que un colorista de Dolby decidió. El fichero resultante se reproduce igual en <strong>cualquier</strong> cadena DV — tu TV, un Shield, un Apple TV, un proyector con LLDV, otro reproductor Amlogic, un PC. El upgrade viaja con el fichero.</p>

    <h3>🔄 Conversión procedural en tiempo real — "CMv4.0 on-the-fly append" en CoreELEC</h3>
    <p>Builds de desarrollador de CoreELEC como <strong>avdvplus</strong>, <strong>panni/pannal</strong> o <strong>cpm</strong> —disponibles en reproductores Amlogic con SoC licenciado por Dolby (Ugoos AM6B+, AM6B Plus, Homatics R 4K Plus)— tienen un toggle <em>"DV CMv4.0 on-the-fly append"</em> que hace una operación muy concreta: al reproducir un RPU CMv2.9, lo <strong>promociona estructuralmente</strong> a CMv4.0 en memoria, sin tocar el fichero.</p>

    <div class="help-callout help-callout-warning">
      <strong>Importante — no hay fuente externa:</strong> esta conversión <em>no</em> descarga ni consulta un bin CMv4.0 retail. Es puramente procedural: se añade el marker CMv4.0 (bloque L254) y se rellenan los niveles L3/L9/L11 con <strong>valores por defecto neutros/identidad</strong> (L9=DCI-P3, L11=Cinema, L3 en cero, L8 derivado de L2 cuando existe). La metadata original CMv2.9 se respeta tal cual; el "upgrade" es solo el envoltorio estructural.
    </div>

    <h3>¿Por qué mejora si no añade información real?</h3>
    <p>El beneficio es <strong>indirecto pero medible</strong>: al recibir un stream etiquetado como CMv4.0, la TV conmuta del decoder DV viejo al decoder DV nuevo — y ese decoder nuevo corrige varios bugs conocidos del pipeline CMv2.9:</p>
    <ul>
      <li><strong>Bug de sobrebrillo con EDID 1000-nit</strong>: TVs de brillo moderado aplicaban un tone-mapping agresivo de más en CMv2.9; el pipeline CMv4.0 lo modera.</li>
      <li><strong>Bug de Chroma Weight en trims L2</strong>: error matemático histórico en la aplicación de saturation offsets que CMv4.0 arregla.</li>
      <li><strong>Bug "base config data" Display-Led DV-STD</strong>: early tone-mapping visible sobre todo en masters 4000-nit, corregido en v4.0.</li>
    </ul>
    <p>Es decir, la conversión procedural no inventa L8-L11 ni aporta grading nuevo, pero obliga al display a ejecutar <em>código más reciente y depurado</em> sobre la misma metadata base. De ahí que el "Auto" mode solo active el append cuando se cumple una de dos condiciones: (1) el source no tiene L2 (no hay trims reales que "perder" al re-etiquetar), o (2) el display es más brillante que el MDL del master (la TV iba a ignorar los trims de todos modos). En esos casos es riesgo cero.</p>

    <h3>¿Se puede "inventar" L8-L11 con cálculos desde L1/L2?</h3>
    <div class="help-callout help-callout-info">
      <strong>No, oficialmente.</strong> Dolby es claro en su documentación: la conversión real CMv2.9 → CMv4.0 requiere <em>re-autoría</em> por un colorista en una Content Mapping Unit. No existe una fórmula pública que derive trims L8-L11 artísticamente correctos desde L1/L2.
      <br><br>
      Lo que hace esta app es <em>transferir</em> niveles L8-L11 desde una fuente que sí los tiene auténticos (un master CMv4.0 retail de la misma edición). Lo que hace avdvplus es <em>estructural</em> — rellena los huecos con identidad para que la TV use el pipeline nuevo. Son operaciones distintas con objetivos distintos; ninguna inventa metadata artística.
    </div>

    <h3>Comparativa directa</h3>
    <table>
      <tr><th>Aspecto</th><th>Upgrade estático con transferencia (esta app)</th><th>Conversión procedural on-the-fly (avdvplus/panni)</th></tr>
      <tr><td><strong>Origen de L3/L8-L11 en el resultado</strong></td><td>RPU CMv4.0 <em>real</em> (colorista) de una fuente externa retail — trims y primaries auténticos</td><td>Valores <em>identidad/neutros</em> generados procedimentalmente (L9=DCI-P3, L11=Cinema, L3=0, L8=L2 o neutro)</td></tr>
      <tr><td><strong>Qué gana la TV</strong></td><td>Trims artísticos reales + pipeline CMv4.0 (lo mejor de ambos)</td><td>Solo el pipeline CMv4.0 (los bugs v2.9 se corrigen, pero sin trims nuevos)</td></tr>
      <tr><td><strong>Dónde funciona</strong></td><td>En cualquier reproductor con DV (Apple TV, Shield, TVs, proyectores, otros Amlogic)</td><td>Solo en la caja Amlogic con firmware avdvplus/panni — no es portable</td></tr>
      <tr><td><strong>Portabilidad del fichero</strong></td><td>El MKV resultante es portable — el upgrade viaja con él</td><td>El MKV original no se modifica — el "upgrade" vive en la caja</td></tr>
      <tr><td><strong>Validación</strong></td><td>Auditable: Fase D muestra Δ frames y correlación Pearson</td><td>Heurística en el reproductor (Off/Always/Auto); sin chequeo humano</td></tr>
      <tr><td><strong>Qué necesitas aportar</strong></td><td>Un bin CMv4.0 retail compatible (repo DoviTools o MKV propio)</td><td>Nada — la caja lo hace sola con lo que hay en el fichero</td></tr>
      <tr><td><strong>Coste</strong></td><td>Una vez, al crear el proyecto (20-60 min). Reproducción normal después.</td><td>Cada reproducción hace la promoción — coste mínimo pero siempre presente</td></tr>
      <tr><td><strong>Reversibilidad</strong></td><td>Conservas el MKV original aparte si quieres deshacer</td><td>Toggle off y vuelve a v2.9 al instante</td></tr>
      <tr><td><strong>Compatibilidad futura</strong></td><td>Archivo estándar — sobrevive a actualizaciones y cambios de reproductor</td><td>Depende de que el developer siga manteniendo el build</td></tr>
    </table>

    <div class="help-callout help-callout-success">
      <strong>Cuándo interesa cada uno:</strong>
      <br>· <em>Si tu caja es Ugoos AM6B+ (o similar con CoreELEC-avdvplus) y solo reproduces ahí</em>: el append procedural es cómodo, gratis y suficiente para corregir los bugs del pipeline v2.9 sin hacer nada. No necesitas esta app.
      <br>· <em>Si quieres un archivo portable con trims reales de colorista, que se reproduzca igual en cualquier cadena DV</em>: el upgrade estático con transferencia es el camino. Ganas además los L3/L8-L11 auténticos, no solo el cambio de pipeline.
      <br>· <em>Enfoque combinado</em>: muchos usuarios avanzados mantienen el MKV estático como "master" portable y usan el append procedural como conveniencia para películas sin bin retail disponible.
    </div>

    <h2 id="w-tvs">📺 Matriz de TVs que realmente aprovechan CMv4.0</h2>
    <p>Los TVs sin engine CMv4.0 <em>ignoran silenciosamente L8-L11</em> y usan L1+L2 como siempre. No hay fallo, simplemente no aprovechan los trims nuevos. <strong>Regla general consolidada:</strong> TVs <strong>2020+</strong> de marcas que soportan DV suelen tener engine CMv4.0. Detalles por marca:</p>
    <table>
      <tr><th>Marca</th><th>CMv4.0 confirmado en</th><th>Notas</th></tr>
      <tr><td><strong>LG OLED</strong></td><td>CX/BX (2020) y posteriores. C1/G1 (2021), C2/G2 (2022), C3/G3 (2023), C4/G4 (2024) — todos.</td><td>La referencia del ecosistema DV. webOS engine DV es maduro.</td></tr>
      <tr><td><strong>Sony Bravia XR</strong></td><td>A95K (2022 QD-OLED) y posteriores. A95L, A80L/K, Bravia XR 2023+.</td><td>⚠️ Foros reportan bugs en "base config data" DV TV-led en modelos no-A95 — el beneficio práctico de CMv4.0 puede ser menor.</td></tr>
      <tr><td><strong>Panasonic OLED</strong></td><td>JZ1500/2000 (2021) y posteriores. LZ/MZ (2022-2023), Z95/Z90 (2024).</td><td>Panasonic fue el primero con procesamiento FEL real en consumo (GZ2000, 2019).</td></tr>
      <tr><td><strong>TCL Mini-LED</strong></td><td>Q-class, C series, X series 2023+ (C735, C845, X955).</td><td>Brillos altos — aprovechan bien el tone-mapping CMv4.0.</td></tr>
      <tr><td><strong>Hisense</strong></td><td>U8K/U9K (2023), U8N/U9N (2024), ULED X.</td><td>Similar a TCL — tope de gamas Mini-LED 2023+.</td></tr>
      <tr><td><strong>Philips OLED (EU)</strong></td><td>OLED807/907 (2022), OLED908/818 (2023).</td><td>—</td></tr>
      <tr><td><strong>Samsung</strong></td><td><span class="help-pill help-pill-samsung">No soporta DV</span> en ningún modelo</td><td>Política corporativa por royalties. Promueve HDR10+. Upgrade CMv4.0 es <strong>irrelevante</strong> en Samsung.</td></tr>
    </table>
    <div class="help-callout help-callout-info">
      <strong>Aviso honesto:</strong> ninguna marca publica "CMv4.0 engine on/off" en las notas de versión de firmware. La clasificación arriba proviene del consenso de foros (AVSForum, Firecore, makemkv) y debe interpretarse como "tendencia mayoritaria", no garantía absoluta por firmware específico.
    </div>

    <h2 id="w-lldv">🚨 El caso LLDV (Low Latency Dolby Vision)</h2>
    <p><strong>LLDV / "player-led DV" / "block 5 DV"</strong> es el modo donde el reproductor origen (Apple TV, Shield, HDFury Vertex/Vrroom) hace el tone-mapping DV internamente y envía una señal HDR estándar ya mapeada al dispositivo receptor. El TV no ve el RPU real — solo recibe HDR con la imagen ya tone-mapeada.</p>
    <p><strong>Dónde se usa:</strong> principalmente <em>proyectores</em> (no existen proyectores con DV TV-led real) y displays HDR10-only que quieren aprovechar el grading DV.</p>
    <div class="help-callout help-callout-danger">
      <strong>LLDV + CMv4.0 = depende del firmware del reproductor:</strong>
      <br>· <strong>Apple TV 4K (2022+) tvOS 17+</strong>: aplica CMv4 en LLDV correctamente.
      <br>· <strong>CoreELEC y similares</strong>: <em>atascados en engine CMv2.9</em> para LLDV. L8-L11 se pierden.
      <br>· <strong>NVIDIA Shield</strong>: depende del "Force LLDV" (developer option post 9.1.1).
      <br>Si tu cadena de reproducción pasa por LLDV en un reproductor sin soporte CMv4, el upgrade no aporta. <strong>Verifica el firmware de tu reproductor antes de invertir horas.</strong>
    </div>

    <h2 id="w-decide">✅ Árbol de decisión — ¿vale la pena en mi caso?</h2>
    <ol>
      <li><strong>¿Tu TV es Samsung?</strong> → no aprovechas DV en ningún modelo (política corporativa). Detente aquí.</li>
      <li><strong>¿Tu TV es anterior a 2020?</strong> → probablemente engine CMv2.9. El upgrade no aporta mejora visible porque el TV ignora L8-L11. Quédate con el Blu-ray original.</li>
      <li><strong>¿Tu TV es 2020-2022?</strong> → aprovecha CMv4.0 en cierta medida según el panel y el procesador. Merece la pena con bin retail; marginal con generated.</li>
      <li><strong>¿Tu TV es 2023+ de tope de gama</strong> (LG G3/G4/C3/C4, Sony A95L/A80L+, Panasonic MZ/Z95, TCL X955+, Hisense U9N+)? → beneficio claro del upgrade cuando haya bin retail. Es donde más se nota.</li>
      <li><strong>¿Reproduces vía LLDV</strong> (proyector, Shield, HDFury)? → verifica firmware. Apple TV tvOS 17+ OK; CoreELEC stock y varios reproductores antiguos pueden perder el upgrade en el camino.</li>
      <li><strong>¿Tu Blu-ray es MEL (no FEL)?</strong> → considera el camino "descartar MEL → P8.1 CMv4.0 single-layer". Mismo resultado visual, archivo más ligero.</li>
      <li><strong>¿Reproduces exclusivamente desde una Ugoos con CoreELEC-avdvplus?</strong> → tienes append automático en el reproductor. Puedes saltarte el upgrade estático o hacerlo solo para películas que quieras archivar portables.</li>
      <li><strong>¿Hay bin retail para tu peli en el repo DoviTools?</strong> (consulta rápida 🔎 desde la app) → si sí, adelante. Si solo hay generated, decide según tu tolerancia a aproximaciones algorítmicas.</li>
    </ol>

    <div class="help-sources">
      <b>Fuentes</b>
      <a href="https://community.firecore.com/t/what-advantages-does-dolbyvision-cmv4-0-have-compared-to-cmv2-9/57517" target="_blank" rel="noreferrer">Firecore community — CMv4 vs CMv2.9 advantages</a> ·
      <a href="https://professionalsupport.dolby.com/s/article/When-should-I-use-CM-v2-9-or-CM-v4-0-and-can-I-convert-between-them" target="_blank" rel="noreferrer">Dolby oficial — CMv2.9 vs CMv4.0 y conversión</a> ·
      <a href="https://professionalsupport.dolby.com/s/article/Dolby-Vision-IQ-Content-Type-Metadata-L11" target="_blank" rel="noreferrer">Dolby oficial — L11 Content Type</a> ·
      <a href="https://avdisco.com/t/demystifying-dolby-vision-profile-levels-dolby-vision-levels-mel-fel/95" target="_blank" rel="noreferrer">avdisco — Demystifying DV Levels</a> ·
      <a href="https://www.veneratech.com/hdr-dolby-vision-meta-data-parameters-to-validate-content" target="_blank" rel="noreferrer">Venera Tech — DV metadata parameters</a> ·
      <a href="http://videoprocessor.org/lldv" target="_blank" rel="noreferrer">VideoProcessor.org — LLDV explicado</a> ·
      <a href="https://www.avsforum.com/threads/ugoos-am6b-coreelec-and-dv-profile-7-fel-playback.3294526/" target="_blank" rel="noreferrer">AVSForum — Ugoos AM6B+ CoreELEC + DV P7 FEL</a> ·
      <a href="https://discourse.coreelec.org/t/ce-ng-dolby-vision-fel-for-dv-licensed-socs-s905x2-s922x-z-s905x4/50953" target="_blank" rel="noreferrer">CoreELEC forum — CE-NG DV (+FEL)</a> ·
      <a href="https://github.com/avdvplus/Builds/releases" target="_blank" rel="noreferrer">avdvplus/Builds — releases del fork CMv4.0 append</a> ·
      <a href="https://www.kodinerds.net/thread/80579-coreelec-entwickler-builds-cpm-avdvplus-pannal-p3i/" target="_blank" rel="noreferrer">Kodinerds — builds avdvplus / pannal P3i</a> ·
      <a href="https://www.samsung.com/us/support/answer/ANS00078565/" target="_blank" rel="noreferrer">Samsung — no Dolby Vision support</a> ·
      <a href="https://forum.makemkv.com/forum/viewtopic.php?t=18602" target="_blank" rel="noreferrer">makemkv forum — hilo DV master</a>
    </div>
  `,

  // ═══════════════════════════════════════════════════════════════
  // SHEET DOVITOOLS
  // ═══════════════════════════════════════════════════════════════
  sheet: `
    <h1>📊 Hoja de DoviTools (R3S3t9999)</h1>
    <p class="cmv40-help-lead">Investigación comunitaria que documenta qué películas aceptan upgrade CMv4.0 sobre el BD original. Es el primer chequeo antes de gastar horas en un proyecto.</p>

    <!-- Enlace directo a la hoja en uso (configurada o por defecto).
         Se hidrata al abrir la sección — ver _cmv40HelpHydrateSheetLink(). -->
    <div id="help-sheet-link-slot" style="margin:10px 0 18px; padding:12px 14px; border:1px solid var(--sep); border-radius:8px; background:var(--surface-2); display:flex; align-items:center; gap:10px; flex-wrap:wrap">
      <span style="font-size:18px">🔗</span>
      <div style="flex:1; min-width:0">
        <div style="font-size:11px; color:var(--text-3); text-transform:uppercase; letter-spacing:0.5px; font-weight:600; margin-bottom:2px">Hoja en uso ahora mismo</div>
        <a id="help-sheet-link-anchor" href="#" target="_blank" rel="noreferrer"
           style="font-size:13px; color:var(--blue); font-weight:600; text-decoration:none; word-break:break-all"
           data-tooltip="Abre la hoja en una pestaña nueva">
          Cargando…
        </a>
        <div id="help-sheet-link-meta" style="font-size:11px; color:var(--text-3); margin-top:2px; font-style:italic">—</div>
      </div>
    </div>

    <div class="help-subtoc">
      <b>En esta sección</b>
      <a href="#s-who">Quién es R3S3t9999 / REC_9999</a>
      <a href="#s-structure">Estructura del sheet</a>
      <a href="#s-columns">Columnas y cómo leerlas</a>
      <a href="#s-hyperlinks">Enlaces del sheet</a>
      <a href="#s-app">Cómo lo usa la app</a>
    </div>

    <h2 id="s-who">👤 De dónde sale la información</h2>
    <p><strong>R3S3t9999</strong> (alias en GitHub; también conocido como <em>REC_9999</em> o <em>Salty01</em> en foros) mantiene la referencia de facto del ecosistema Dolby Vision abierto:</p>
    <ul>
      <li>Un conjunto de <strong>scripts open-source</strong> (<em>DoVi_Scripts</em>) para generar y editar RPUs de Dolby Vision.</li>
      <li>Una <strong>hoja pública</strong> que documenta, película por película, si el upgrade CMv4.0 es viable y qué precauciones tomar.</li>
    </ul>
    <p>Su taxonomía <em>retail / restored / generated</em> es el vocabulario estándar que verás en AVSForum, el foro de makemkv y Reddit r/4kbluray. La hoja se actualiza en comunidad: cualquiera puede aportar datos de pruebas.</p>
    <div class="help-callout help-callout-info">
      <strong>Tamaño aproximado:</strong> varios cientos de títulos catalogados distribuidos en las 3 secciones (ver abajo). Crece en tiempo real.
    </div>

    <h2 id="s-structure">📋 Estructura del sheet — tres clasificaciones</h2>
    <p>Cada película se clasifica en <strong>una de tres categorías</strong> según el estado de viabilidad del upgrade:</p>
    <table>
      <tr><th>Categoría</th><th>Qué significa</th><th>Qué hace la app al detectarla</th></tr>
      <tr><td><strong>No factible</strong></td><td>El upgrade <em>no funciona</em> limpiamente para esta película: no existe bin CMv4.0 público, o el master de referencia tiene un corte incompatible con el Blu-ray, o hay problemas técnicos documentados.</td><td>Banner rojo. La app desaconseja crear el proyecto.</td></tr>
      <tr><td><strong>Factible</strong></td><td>Upgrade <em>verificado</em> por la comunidad: bin disponible y probado, desfase de frames conocido, comparaciones HDR OK.</td><td>Banner verde. Adelante con el proyecto, normalmente en modo automático.</td></tr>
      <tr><td><strong>Probably OK / Not Sure</strong></td><td>Caso <em>con incertidumbre</em>: bin disponible pero sin verificación completa, o hay reportes contradictorios en la comunidad.</td><td>Banner ámbar. La app recomienda revisión visual manual aunque los gates automáticos pasen.</td></tr>
    </table>

    <h2 id="s-columns">🗂️ Cómo leer cada columna</h2>
    <p>La app te muestra estos campos cuando el sheet tiene información de tu película:</p>
    <table>
      <tr><th>Campo</th><th>Qué significa</th><th>Ejemplo real</th></tr>
      <tr><td><strong>Título</strong></td><td>Nombre de la película (generalmente inglés).</td><td>Zootopia 2 (2024)</td></tr>
      <tr><td><strong>DV source</strong></td><td>De dónde se extrajo el bin CMv4.0 que usa el upgrade.</td><td><code>BD FEL</code>, <code>iTunes</code>, <code>DSNP</code> (Disney+), <code>MA</code> (Movies Anywhere / Vudu), <code>Netflix</code>, <code>AMZN</code>, <code>WEB</code></td></tr>
      <tr><td><strong>Desfase (sync_offset)</strong></td><td>Cuántos frames difiere el bin respecto al Blu-ray. Positivo = el bin tiene frames de más al inicio. Negativo = le faltan.</td><td><code>+48</code>, <code>-24</code>, <code>0</code></td></tr>
      <tr><td><strong>Comparaciones</strong></td><td>Validaciones cruzadas que alguien de la comunidad ya hizo. Confirman que el bin encaja con el Blu-ray más allá del número de frames.</td><td><code>HDR COMP</code> (comparación de imágenes HDR), <code>plot</code> (curvas L1 graficadas), <code>nits</code> (brillo pico verificado), <code>sample</code> (escena concreta revisada), <code>shots</code> (límites de escenas comparados)</td></tr>
      <tr><td><strong>Notas</strong></td><td>Observaciones libres del autor: avisos, consejos, detalles críticos.</td><td>"Use iTunes rip", "BD has extra logos", "FEL preserved OK"</td></tr>
    </table>
    <div class="help-callout help-callout-warning">
      <strong>Cómo interpretar el desfase:</strong> si el sheet dice <code>+48</code>, significa que el bin viene con 48 frames extra al inicio (normalmente logos de estudio que el Blu-ray no tiene). La app lo arregla automáticamente — o te avisa en la revisión visual para confirmes el ajuste.
    </div>

    <h2 id="s-hyperlinks">🔗 Enlaces del sheet</h2>
    <p>Muchas celdas llevan enlaces incrustados a recursos externos: el bin en Google Drive, imágenes comparativas, hilos de foro con pruebas, tutoriales específicos. La app los preserva y te los muestra con un botón "Abrir ↗" en:</p>
    <ul>
      <li>El <strong>banner de recomendación</strong> que aparece al seleccionar un Blu-ray en "Nuevo proyecto".</li>
      <li>La <strong>consulta rápida <code>🔎</code></strong> del header — para revisar un título sin crear proyecto.</li>
    </ul>

    <h2 id="s-app">⚙️ Cómo lo usa esta app</h2>
    <ol>
      <li>Al seleccionar el Blu-ray origen en "Nuevo proyecto", la app extrae el título y año del nombre del fichero.</li>
      <li>Si has configurado una API key de TMDb en <strong>⚙︎ Configuración</strong>, la app contrasta el título con TMDb — así desambigua cine no-ASCII (cine asiático, títulos en otros idiomas) y confirma el año.</li>
      <li>Te muestra el banner de recomendación: verde / ámbar / rojo según la categoría, con los detalles de la columna correspondiente y los enlaces a los recursos.</li>
    </ol>

    <h3>Cómo ajusta la sensibilidad del match</h3>
    <p>La exigencia de similitud se adapta al contexto:</p>
    <table>
      <tr><th>Escenario</th><th>Exigencia de similitud</th><th>Por qué</th></tr>
      <tr><td>Año exacto disponible</td><td><strong>72%</strong></td><td>Más permisiva — el año ya descarta falsos positivos.</td></tr>
      <tr><td>Año ±1 (remaster / re-edición)</td><td><strong>82%</strong></td><td>Margen para re-ediciones. Ej: <em>Blade Runner 2007</em> vs <em>2006</em>.</td></tr>
      <tr><td>Sin año conocido</td><td><strong>88%</strong></td><td>Más estricta — evita que "Rocky" case con cualquier otra peli de boxeo.</td></tr>
    </table>

    <h3>Caché de consultas</h3>
    <p>El sheet se descarga la primera vez y se guarda localmente durante una hora. Para forzar una relectura (por ejemplo, cuando se ha actualizado con nuevos casos), pulsa el botón "Recargar" del modal.</p>

    <div class="help-sources">
      <b>Fuentes</b>
      <a href="https://github.com/R3S3t9999/DoVi_Scripts" target="_blank" rel="noreferrer">R3S3t9999/DoVi_Scripts (GitHub)</a> ·
      <a href="https://forum.makemkv.com/forum/viewtopic.php?t=18602" target="_blank" rel="noreferrer">makemkv forum — Dolby Vision master hilo</a>
    </div>
  `,

  // ═══════════════════════════════════════════════════════════════
  // REPO DRIVE
  // ═══════════════════════════════════════════════════════════════
  repo: `
    <h1>📦 Repositorio DoviTools (Google Drive)</h1>
    <p class="cmv40-help-lead">Carpeta pública de Google Drive con los <code>.bin</code> RPU pre-validados por la comunidad. Cada tipo de bin activa una rama específica del pipeline.</p>

    <div class="help-subtoc">
      <b>En esta sección</b>
      <a href="#r-access">Acceso y API key</a>
      <a href="#r-structure">Estructura del repo</a>
      <a href="#r-philosophy">Retail vs Restored vs Generated</a>
      <a href="#r-taxonomy">Taxonomía de bins</a>
      <a href="#r-pipelines">Qué pipeline activa cada tipo</a>
      <a href="#r-match">Matching con el MKV origen</a>
      <a href="#r-download">Descarga y caching</a>
    </div>

    <h2 id="r-access">🔑 Cómo conseguir acceso al repo</h2>
    <p>Hay una diferencia importante que conviene entender desde el principio: la hoja pública de recomendaciones (la que consulta el tab <strong>📊 Hoja</strong> del manual) es <strong>abierta y anónima</strong>, no requiere nada. Los <strong>bins en sí</strong> (los <code>.bin</code> del Google Drive) están en una carpeta <strong>gated</strong> mantenida personalmente por REC_9999 — no es un enlace público.</p>

    <h3>El modelo de acceso de la comunidad DoviTools</h3>
    <p>El repositorio lo mantiene y paga REC_9999 de su propio bolsillo (coste de Drive, ancho de banda, tiempo de curación). Para sostenerlo, el acceso se concede a los usuarios que <strong>apoyan económicamente el proyecto</strong>. El proceso es muy directo:</p>
    <ol style="font-size:13px">
      <li>Abre el enlace oficial de donación de DoVi_Scripts en PayPal: <a href="https://www.paypal.com/donate/?hosted_button_id=6ML5KUZG9XGB6" target="_blank" rel="noreferrer">paypal.com/donate — DoVi_Scripts</a> (el mismo que aparece en el README de <a href="https://github.com/R3S3t9999/DoVi_Scripts#readme" target="_blank" rel="noreferrer">R3S3t9999/DoVi_Scripts</a> en GitHub).</li>
      <li>Donas <strong>15 CAD</strong> (la cifra de referencia para obtener acceso — dólares canadienses, la moneda por defecto del mantenedor).</li>
      <li>En el campo de <strong>comentarios / mensaje</strong> del formulario de PayPal escribe tu <strong>correo de Google</strong> y una petición breve del tipo <em>"acceso al repositorio de RPUs"</em>. Todo en el mismo paso — no hace falta escribir después por forum ni Discord.</li>
      <li>REC_9999 recibe el correo y comparte manualmente la carpeta de Google Drive contigo usando el correo que has indicado. A partir de ahí tu cuenta de Google tiene visibilidad sobre la carpeta como "compartida conmigo".</li>
      <li>Copia la URL de la carpeta desde tu Google Drive y configúrala en la app (ver el paso 3 de la sección <strong>🔐 Claves y APIs</strong>).</li>
    </ol>

    <div class="help-callout help-callout-info">
      <strong>Por qué el modelo es gated:</strong> almacenar cientos de bins .bin (algunos de varios MB cada uno) con docenas de películas implica coste de espacio y tráfico en Google Drive, además del tiempo de curación. El modelo de donación hace sostenible el proyecto sin anuncios ni comercialización — es la forma habitual en proyectos de comunidad A/V cuando un solo mantenedor lleva la infraestructura.
    </div>

    <h3>¿Qué es exactamente lo que obtienes con el acceso?</h3>
    <ul style="font-size:13px">
      <li>Lectura completa de la carpeta de Google Drive con todos los bins validados</li>
      <li>Puedes filtrar, listar y descargar desde la propia interfaz web de Drive</li>
      <li>Desde esta app: la pestaña <strong>📦 Repo DoviTools</strong> del modal "Nuevo proyecto" lista el inventario y descarga al workdir sin clics manuales</li>
      <li>Acceso a nuevas ediciones según el mantenedor añade bins (sin tener que volver a donar)</li>
    </ul>

    <h3>Sin donar — qué puedes hacer igualmente</h3>
    <ul style="font-size:13px">
      <li>La <strong>hoja pública de recomendaciones</strong> (tab <strong>📊 Hoja</strong> del manual) funciona sin credenciales — es una hoja de Google Sheets pública, cualquiera la puede leer</li>
      <li>Puedes ver <em>qué películas</em> tienen upgrade disponible y de qué tipo (retail/restored/generated) — te hace el diagnóstico previo igual</li>
      <li>Si solo tienes curiosidad o pocas películas, puedes construir tus propios RPUs con <a href="https://github.com/R3S3t9999/DoVi_Scripts" target="_blank" rel="noreferrer">DoVi_Scripts</a> directamente: el código es open-source, lo que se paga es la infraestructura de distribución y la curación comunitaria</li>
      <li>Algunos usuarios comparten puntualmente bins sueltos en los foros públicos — búsqueda caso a caso</li>
    </ul>

    <div class="help-callout help-callout-warning">
      <strong>Aviso:</strong> la cifra de 15 CAD y el formato del proceso son la referencia actual de la comunidad, pero pueden cambiar con el tiempo. Si al donar no recibes respuesta en unos días, los <a href="https://www.avsforum.com/threads/ugoos-am6b-coreelec-and-dv-profile-7-fel-playback.3294526/" target="_blank" rel="noreferrer">hilos de AVSForum</a> y <a href="https://forum.doom9.org/showthread.php?t=185317" target="_blank" rel="noreferrer">Doom9</a> son los sitios donde consultar el procedimiento vigente.
    </div>

    <!-- Estado actual del folder Drive configurado en este servidor -->
    <div id="help-drive-link-slot" style="margin:18px 0 18px; padding:12px 14px; border:1px solid var(--sep); border-radius:8px; background:var(--surface-2); display:flex; align-items:center; gap:10px; flex-wrap:wrap">
      <span style="font-size:18px">📁</span>
      <div style="flex:1; min-width:0">
        <div style="font-size:11px; color:var(--text-3); text-transform:uppercase; letter-spacing:0.5px; font-weight:600; margin-bottom:2px">Carpeta Drive en este servidor</div>
        <div id="help-drive-link-status" style="font-size:13px; font-weight:600">Cargando…</div>
        <div id="help-drive-link-meta" style="font-size:11px; color:var(--text-3); margin-top:2px; font-style:italic">—</div>
      </div>
    </div>

    <p style="font-size:12px; color:var(--text-3); font-style:italic">Para la configuración técnica (cómo crear la Google API key que la app usa para leer el Drive, cómo pegarlo todo en ⚙︎ Configuración, errores frecuentes), ve a la sección <strong>🔐 Claves y APIs</strong> al final del manual.</p>

    <h2 id="r-structure">📁 Estructura del repo</h2>
    <p>La carpeta se organiza jerárquicamente por película + versión + tipo de bin. La app escanea hasta <strong>5 niveles de profundidad</strong> buscando <code>.bin</code>. Ejemplos de estructura típica:</p>
    <ul>
      <li><code>Zootopia 2 (2024) UHD-BD/</code>
        <ul>
          <li><code>Zootopia 2 UHD-BD_P7 FEL (retail cmv4.0 restored).bin</code> ← preferida</li>
          <li><code>Zootopia 2 UHD-BD_P5 to P8_(Variable L5).bin</code></li>
          <li><code>Zootopia 2 iMAX_Generated (variable L5) V3.bin</code> ← última alternativa</li>
        </ul>
      </li>
    </ul>
    <div class="help-callout help-callout-info">
      <strong>Inventario total:</strong> el repo crece constantemente (cientos de películas indexadas). La app lo consulta en tiempo real cuando seleccionas un Blu-ray, filtrando solo los bins que potencialmente encajan con tu película.
    </div>

    <h2 id="r-philosophy">🏷️ Retail vs Restored vs Generated — la taxonomía de la comunidad</h2>
    <p>Antes de profundizar en los nombres de los ficheros conviene entender la <em>clasificación conceptual</em> que usa la comunidad DoviTools para hablar de RPUs. No todos los bins CMv4.0 son iguales: dependiendo de cómo se haya creado el RPU, la calidad del resultado final cambia sustancialmente. Estas son las tres categorías consolidadas en AVSForum, MakeMKV y el propio repo:</p>
    <table>
      <tr><th>Categoría</th><th>Qué es</th><th>Cuándo aparece</th><th>Calidad esperable</th></tr>
      <tr><td><span class="help-pill help-pill-retail">Retail</span></td><td>RPU extraído sin modificar de un stream o remux con Dolby Vision <strong>oficial</strong>: un WEB-DL con CMv4.0, un Blu-ray CMv4.0, o similar. Los trims los ha firmado un colorista de Dolby o del estudio.</td><td>Cuando existe una versión streaming o disco con CMv4.0 de la misma edición que el Blu-ray que quieres upgradear.</td><td><strong>Máxima.</strong> Es lo que pretendes cuando usas esta app.</td></tr>
      <tr><td><span class="help-pill help-pill-retail">Restored CMv4.0 retail</span></td><td>Lo mismo que Retail, pero cuando el Blu-ray original es P7 FEL CMv2.9 y el streaming es P5 o P8 CMv4.0. El bin "restaura" los trims CMv4.0 al formato P7 FEL del disco. <strong>Es el caso más frecuente del upgrade con esta app.</strong></td><td>Upgrade clásico: Blu-ray FEL + bin CMv4.0 de un WEB-DL moderno.</td><td>Máxima práctica. Indistinguible de retail puro en reproducción.</td></tr>
      <tr><td><span class="help-pill help-pill-gen">Generated</span></td><td>RPU <strong>sintético</strong>, creado por algoritmos de la comunidad (scripts del propio R3S3t9999, la opción <em>generate</em> de dovi_tool) a partir del HDR10/HDR10+/HLG del Blu-ray. <strong>No hay colorista detrás</strong> — los trims los calcula una heurística.</td><td>Blockbusters sin versión streaming CMv4.0 — la única forma de obtener "algún" CMv4.0 para el Blu-ray es generarlo.</td><td>Aceptable. Mejor que el CMv2.9 original en TVs CMv4.0-aware, pero un escalón por debajo de retail en precisión de trims.</td></tr>
    </table>
    <div class="help-callout help-callout-success">
      <strong>Consenso consolidado en la comunidad:</strong> <em>si existe retail (o restored retail) CMv4.0 de la edición exacta de tu Blu-ray, usar retail siempre</em>. Generated es la opción "mejor que nada" cuando no hay alternativa real. Por eso el modal de nuevo proyecto avisa en ámbar si eliges un generated habiendo retail disponible.
    </div>

    <h2 id="r-taxonomy">🏷️ Cómo se nombran en el repo</h2>
    <p>Esta sección pasa de lo conceptual a lo concreto: cómo identificar qué tipo es cada fichero <em>a partir de su nombre</em> sin necesidad de descargarlo. Los nombres siguen convenciones consolidadas por R3S3t9999 y adoptadas ampliamente en AVSForum y MakeMKV. La app detecta estos patrones automáticamente:</p>
    <table>
      <tr><th>Patrón en filename</th><th>Significado</th><th>Provenance</th></tr>
      <tr><td><code>P7 FEL</code> + <code>retail cmv4.0 restored</code></td><td>RPU retail extraído de WEB CMv4.0 re-adaptado al stream P7 FEL del BD. <strong>Formato estrella</strong> — drop-in directo.</td><td><span class="help-pill help-pill-retail">Retail</span></td></tr>
      <tr><td><code>P7 MEL</code> + <code>retail cmv4.0</code></td><td>Retail equivalente para BDs MEL. Al inyectar se descarta la MEL (no aporta calidad) → sale P8.1.</td><td><span class="help-pill help-pill-retail">Retail</span></td></tr>
      <tr><td><code>P5 to P8</code> + <code>(Variable L5)</code></td><td>Bin extraído de un stream P5 (iTunes, Netflix antiguo) y convertido a P8.1 reusando el BL HDR10 del BD. Preserva FEL en el merge final.</td><td><span class="help-pill help-pill-retail">Retail</span></td></tr>
      <tr><td><code>P8</code> + <code>(Variable L5)</code></td><td>Retail directo de una fuente P8.1 (WEB-DL moderno). Merge del CMv4.0 sobre el P7 del BD.</td><td><span class="help-pill help-pill-retail">Retail</span></td></tr>
      <tr><td><code>Generated</code> / <code>V3</code> / <code>tcfs</code> / <code>synthetic</code></td><td>RPU sintético generado algorítmicamente desde HDR10/HDR10+/HLG del propio BD. No es "trim real" de colorista.</td><td><span class="help-pill help-pill-gen">Generated</span></td></tr>
      <tr><td><code>iMAX_Generated</code></td><td>Variante generated específica para ratio IMAX (1.90:1) del BD.</td><td><span class="help-pill help-pill-gen">Generated</span></td></tr>
    </table>
    <div class="help-callout help-callout-success">
      <strong>Preferencia consolidada de la comunidad:</strong> Retail CMv4.0 WEB &gt; Retail P5→P8 &gt; Retail MEL &gt; Generated CMv4.0. Si existe retail, usar retail siempre.
    </div>

    <h2 id="r-pipelines">🔀 Qué pipeline activa cada tipo de bin</h2>
    <p>Según el bin que elijas, la app toma automáticamente una ruta distinta del pipeline — algunas fases se optimizan o se saltan para ahorrar tiempo. En la sección <em>Pipelines</em> verás los diagramas visuales de cada ruta; aquí el resumen por tipo:</p>
    <table>
      <tr><th>Tipo de bin</th><th>Cuándo aplica</th><th>Qué hace la app</th><th>Revisión manual</th></tr>
      <tr><td><strong>Drop-in P7 FEL retail</strong></td><td>Tu Blu-ray es P7 FEL y el bin es P7 FEL CMv4.0 retail (mismo formato del BD, solo cambia el tone-mapping).</td><td>Ruta más rápida: inyecta el RPU directamente sobre el vídeo del Blu-ray sin separar capas. ~20 min totales.</td><td>No (se salta)</td></tr>
      <tr><td><strong>Drop-in P7 MEL retail</strong></td><td>Tu Blu-ray es P7 MEL y el bin es P7 MEL CMv4.0 retail.</td><td>Descarta el EL (no aporta calidad), inyecta sobre el BL y la salida es P8.1 single-layer. Archivo más ligero que el origen.</td><td>No (se salta)</td></tr>
      <tr><td><strong>P8 source retail</strong></td><td>El bin viene de un master P8.1 (streaming moderno) con L8 incluido, y tu Blu-ray es P7 FEL.</td><td>Transfiere los niveles CMv4.0 al RPU P7 del Blu-ray <em>preservando todo el EL</em>. Mantiene la calidad máxima.</td><td>No (se salta)</td></tr>
      <tr><td><strong>Generated</strong></td><td>El bin es sintético (creado algorítmicamente desde HDR10), no hay retail disponible.</td><td>Ruta completa con revisión visual obligatoria — los trims sintéticos conviene verificarlos antes de inyectar.</td><td><strong>Sí</strong></td></tr>
      <tr><td><strong>Extraído de otro MKV</strong></td><td>Aportas un MKV propio con CMv4.0 como target (no es del repo público).</td><td>Extrae el RPU del MKV que le das y ejecuta la ruta completa con revisión visual.</td><td><strong>Sí</strong></td></tr>
      <tr><td><strong>Incompatible</strong></td><td>El bin no es CMv4.0, o el corte del master es radicalmente distinto al Blu-ray.</td><td>Aborta el proyecto con un mensaje explicativo. Busca otro bin o pasa en esta peli.</td><td>—</td></tr>
    </table>

    <h2 id="r-match">🔍 Cómo encuentra la app el bin correcto</h2>
    <p>Cuando seleccionas el Blu-ray origen en el modal "Nuevo proyecto", la app:</p>
    <ol>
      <li>Lee el nombre del fichero y extrae el título y el año, ignorando las etiquetas técnicas típicas (<em>UHD.BluRay.x265</em>, <em>[DV FEL]</em>, <em>REMUX</em>, etc.).</li>
      <li>Si tienes TMDb configurado, obtiene hasta 5 títulos alternativos — útil sobre todo para cine asiático y otros idiomas no latinos.</li>
      <li>Compara cada bin del repo con la película usando <strong>matching por similitud</strong> tolerante a acentos, puntuación y variantes (<em>II → 2</em>, <em>The / El / La / de</em>, etc.).</li>
      <li>Distingue películas distintas con el mismo título usando el año — p.ej. <em>El Rey León 1994</em> vs <em>El Rey León 2019</em>.</li>
      <li>Te presenta los mejores candidatos ordenados. El de mayor afinidad se selecciona solo, pero puedes cambiar a cualquiera de la lista.</li>
    </ol>
    <div class="help-callout help-callout-info">
      <strong>Aviso de procedencia:</strong> si eliges un bin <em>Generated</em> pero en el repo existe un equivalente <em>Retail</em> para la misma película, el modal muestra un aviso ámbar con el nombre del bin retail disponible — para que reconsideres antes de crear el proyecto.
    </div>

    <h2 id="r-download">📥 Qué pasa cuando creas el proyecto</h2>
    <ol>
      <li>La app descarga el bin elegido (5-50 MB típicamente, es inmediato con buena conexión).</li>
      <li>Calcula su huella SHA-256 abreviada — útil si luego compartes resultados en foros.</li>
      <li>Lee la metadata del bin y comprueba que efectivamente es CMv4.0 con los niveles necesarios.</li>
      <li>Ejecuta las comparaciones automáticas (trust gates) contra tu Blu-ray — frames, L5, L6, L1.</li>
      <li>Según los resultados, toma la ruta automática o la ruta con revisión manual (ver <em>Pipelines</em>).</li>
    </ol>
    <p><strong>Caché del inventario:</strong> la lista de todos los bins del repo se descarga la primera vez y se guarda localmente durante 24 horas. Para forzar relectura, pulsa el botón ↻ del modal.</p>

    <h3>Alternativa local (legacy)</h3>
    <p>Si has descargado manualmente bins <code>.bin</code> desde un ordenador externo, puedes dejarlos en la carpeta local que definas en el arranque Docker (variable <code>CMV40_RPU_PATH</code>). La tab "Carpeta local" del modal los listará. Es una opción residual — la forma recomendada y más cómoda es usar el repositorio Drive, que siempre está actualizado.</p>

    <div class="help-sources">
      <b>Fuentes</b>
      <a href="https://github.com/R3S3t9999/DoVi_Scripts" target="_blank" rel="noreferrer">R3S3t9999/DoVi_Scripts</a> ·
      <a href="https://github.com/R3S3t9999/DoVi_Scripts/discussions/89" target="_blank" rel="noreferrer">DoVi_Scripts — Generated vs Retail hilo</a> ·
      <a href="https://forum.makemkv.com/forum/viewtopic.php?t=18602&start=7230" target="_blank" rel="noreferrer">makemkv — taxonomía retail / generated / restored</a>
    </div>
  `,

  // ═══════════════════════════════════════════════════════════════
  // HERRAMIENTAS
  // ═══════════════════════════════════════════════════════════════
  tools: `
    <h1>🔧 Qué herramientas usa la app por debajo</h1>
    <p class="cmv40-help-lead">No vas a ejecutar ningún comando manualmente — la app orquesta todo. Pero conocer las piezas te ayuda a entender qué hace en cada fase, por qué tarda lo que tarda, y qué está detrás de cada resultado. Todas son open-source y vienen empaquetadas en el contenedor Docker de la app.</p>

    <div class="help-subtoc">
      <b>En esta sección</b>
      <a href="#t-ffmpeg">ffmpeg</a>
      <a href="#t-dovi">dovi_tool (el core)</a>
      <a href="#t-mkvmerge">mkvmerge</a>
      <a href="#t-mkvpropedit">mkvpropedit</a>
      <a href="#t-mediainfo">mediainfo</a>
    </div>

    <h2 id="t-ffmpeg">🎬 ffmpeg — la navaja suiza del vídeo</h2>
    <p><strong>Qué es:</strong> el estándar de facto para procesamiento de vídeo/audio. Universal, open-source, en casi todo lo que reproduce vídeo en software.</p>
    <p><strong>Para qué la usa la app:</strong> solo para <em>extraer</em> el stream de vídeo del MKV sin re-encodarlo (copia pura byte a byte). Es la primera operación del pipeline.</p>
    <div class="help-callout help-callout-success">
      <strong>Descubrimiento interesante:</strong> aunque <code>dovi_tool</code> (la siguiente herramienta) sabe leer MKVs directamente en teoría, en la práctica falla con ciertos Blu-rays porque la metadata HEVC se almacena de forma peculiar dentro del MKV. Por eso la app siempre extrae el HEVC primero a un fichero intermedio — es más lento pero 100% fiable.
    </div>

    <h2 id="t-dovi">🎯 dovi_tool — el cerebro del upgrade</h2>
    <p><strong>Qué es:</strong> la herramienta de referencia del ecosistema Dolby Vision open-source. La mantiene <strong>quietvoid</strong> en GitHub (escrita en Rust). Todo el software de la comunidad la usa — es la referencia técnica de facto.</p>
    <p><strong>Para qué la usa la app:</strong> prácticamente todo lo que tiene que ver con el RPU (la metadata Dolby Vision) — leerlo, analizarlo, modificarlo e inyectarlo. Se usa en todas las fases del pipeline excepto las puramente de fichero.</p>

    <h3>Qué hace en cada fase</h3>
    <table>
      <tr><th>Fase</th><th>Acción de dovi_tool</th><th>Duración aproximada</th></tr>
      <tr><td><strong>A (Analizar)</strong></td><td>Lee el RPU del vídeo del Blu-ray y extrae su metadata (perfil, FEL/MEL, CMv2.9/v4.0, número de frames, L1/L5/L6 principales).</td><td>2-3 min para un UHD de 155.000 frames</td></tr>
      <tr><td><strong>B (Target)</strong></td><td>Lo mismo sobre el bin target — para clasificarlo y compararlo con el Blu-ray.</td><td>&lt; 5 s</td></tr>
      <tr><td><strong>C (Demux)</strong></td><td>Separa el vídeo HEVC en BL (capa base) y EL (capa de mejora) cuando la ruta lo requiere.</td><td>~3 min</td></tr>
      <tr><td><strong>E (Corrección)</strong></td><td>Aplica sobre el RPU target las operaciones de <em>eliminar</em> y <em>duplicar</em> frames que hayas confirmado en la revisión visual.</td><td>&lt; 10 s</td></tr>
      <tr><td><strong>F (Inyectar)</strong></td><td><strong>El paso clave del upgrade.</strong> Reescribe el vídeo entero con el nuevo RPU CMv4.0 intercalado frame a frame. No re-encoda — solo sustituye la metadata.</td><td>5-7 min (es el paso más pesado)</td></tr>
      <tr><td><strong>G (Remux)</strong></td><td>Combina BL + EL en un stream dual-layer cuando la ruta no es drop-in.</td><td>~2 min</td></tr>
      <tr><td><strong>H (Validar)</strong></td><td>Re-lee el RPU del resultado final para confirmar que todo cuadra: CM v4.0, número de frames correcto, perfil esperado.</td><td>2-3 min</td></tr>
    </table>

    <div class="help-callout help-callout-info">
      <strong>Versión en uso:</strong> el contenedor incluye <strong>dovi_tool 2.3.2</strong>. Mejoras clave que aporta respecto a versiones 2.1.x: <em>inject-rpu</em> coloca el RPU como último NALU del access unit (corrige playback en reproductores basados en FFmpeg); <em>mux</em> maneja EOS/EOB NALUs por defecto sin flags manuales; <em>extract-rpu</em> acepta Matroska (MKV) como entrada directa — esto permite que el análisis de DV en las pestañas <strong>Blu-Ray ISO → MKV</strong> y <strong>Consultar / Editar MKV</strong> se haga sin pre-extraer el HEVC con ffmpeg; <em>editor</em> soporta oficialmente <code>allow_cmv4_transfer</code> para transferir trims L3/L8-L11 de un RPU CMv4.0 a uno CMv2.9 (lo usamos en Fase F para la rama de merge sobre P7 FEL); <em>info --summary</em> incluye estructuradamente offsets L5, trims L8 y primaries L9.
    </div>

    <h2 id="t-mkvmerge">📦 mkvmerge — el ensamblador final</h2>
    <p><strong>Qué es:</strong> el ensamblador de ficheros Matroska (MKV) profesional. Parte de MKVToolNix, la suite estándar para trabajar con este formato.</p>
    <p><strong>Para qué la usa la app:</strong> en la fase final, toma el vídeo ya con el RPU CMv4.0 inyectado y lo ensambla con el audio, subtítulos y capítulos del Blu-ray original. El resultado es el MKV final que te queda en la carpeta de salida. Opera sin copiar datos innecesariamente — la barra de progreso que ves en el modal de ejecución viene directamente de ahí.</p>

    <h2 id="t-mkvpropedit">🏷️ mkvpropedit — edición instantánea</h2>
    <p><strong>Qué es:</strong> la herramienta compañera de mkvmerge para editar propiedades de un MKV sin tener que reescribirlo (operación instantánea).</p>
    <p>El pipeline CMv4.0 <strong>no la usa</strong> directamente — mkvmerge ya escribe con los nombres y flags correctos desde el principio (título del vídeo, pistas, etc.). La app <em>sí la usa</em> intensamente en la pestaña <strong>Editar Propiedades MKV</strong> para modificar nombres de pistas, flags por defecto/forzados y capítulos sin duplicar el fichero.</p>

    <h2 id="t-mediainfo">🔍 MediaInfo — el detector experto</h2>
    <p><strong>Qué es:</strong> lector de metadata multimedia más completo que existe. Extrae toda la información técnica de un fichero: codec, bitrate real, canales, HDR10, formato comercial del audio…</p>
    <p><strong>Para qué la usa la app:</strong> principalmente en la pestaña <strong>Blu-Ray ISO → MKV</strong>, para detectar con precisión si una pista de audio es Atmos, DTS:X o variante; determinar el bitrate real; leer la metadata HDR10 del vídeo. En el pipeline CMv4.0 apenas interviene — ahí manda dovi_tool para todo lo que concierne al Dolby Vision.</p>
    <div class="help-callout help-callout-warning">
      <strong>Fiabilidad:</strong> la detección de Dolby Atmos (sobre TrueHD o Dolby Digital+) es determinista porque Dolby publica las especificaciones. La de DTS:X depende de ingeniería inversa — ocasionalmente falla con falsos negativos, especialmente con variantes IMAX Enhanced. Es una limitación conocida del ecosistema DTS.
    </div>

    <div class="help-sources">
      <b>Fuentes</b>
      <a href="https://github.com/quietvoid/dovi_tool" target="_blank" rel="noreferrer">quietvoid/dovi_tool (GitHub)</a> ·
      <a href="https://github.com/quietvoid/dovi_tool/releases/tag/2.3.2" target="_blank" rel="noreferrer">dovi_tool 2.3.2 — notas de versión</a> ·
      <a href="https://mkvtoolnix.download/" target="_blank" rel="noreferrer">MKVToolNix — sitio oficial</a> ·
      <a href="https://mediaarea.net/en/MediaInfo" target="_blank" rel="noreferrer">MediaInfo — sitio oficial</a> ·
      <a href="https://ffmpeg.org/" target="_blank" rel="noreferrer">FFmpeg — sitio oficial</a>
    </div>
  `,

  // ═══════════════════════════════════════════════════════════════
  // PIPELINES
  // ═══════════════════════════════════════════════════════════════
  pipelines: `
    <h1>🔀 Pipelines CMv4.0 — qué pasa tras pulsar "Crear"</h1>
    <p class="cmv40-help-lead">Cuando arrancas un proyecto, la app ejecuta un proceso de 8 fases (A-H). Según cómo sea tu Blu-ray (P7 FEL, P7 MEL o P8) y qué tipo de bin CMv4.0 uses como target, el recorrido cambia: hay fases que se saltan, otras que se reducen y alguna donde tú tomas el control. Esta sección explica qué hace cada fase, qué ves en pantalla, y por qué para ciertos bins el pipeline termina en 20 minutos mientras que para otros te pide revisión visual.</p>

    <div class="help-subtoc">
      <b>En esta sección</b>
      <a href="#p-overview">Flujo general</a>
      <a href="#p-recommendation">Mantener MKV vs Inyectar RPU (recomendación automática)</a>
      <a href="#p-phases">Qué hace cada fase (y qué ves tú)</a>
      <a href="#p-gates">Cómo decide la app entre automático y manual</a>
      <a href="#p-casos">Casuísticas completas por tipo de source</a>
      <a href="#p-sync">El ajustador visual (Fase D) al detalle</a>
      <a href="#p-problems">Problemas típicos y qué hacer</a>
    </div>

    <h2 id="p-overview">🔁 Flujo general</h2>
    <p>Este es el recorrido cuando el target <em>no</em> está pre-validado por la comunidad (bin generated, MKV custom o divergencias con el BD). Es el caso que requiere más intervención tuya: la fase D exige que valides visualmente que las curvas están alineadas antes de inyectar.</p>
    <p style="font-size:12px; color:var(--text-3); margin:-4px 0 10px">Las <em>fases</em> (letras A-H) son trabajo que ejecuta la app. Las <em>🛡️ validaciones</em> son puntos de decisión que viven entre fases: la app compara datos de la Fase A con los del bin target, y según el resultado, el pipeline puede saltar fases enteras. Por eso aparecen en los diagramas con otro color y sin letra.</p>
    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Pipeline por defecto — target no pre-validado</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar BD</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Preparar target</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Validaciones</span><span class="cmv40-ph-mod">gates no OK</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Separar capas</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Revisión visual</span><span class="cmv40-ph-mod">manual</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corregir sync</span><span class="cmv40-ph-mod">si Δ≠0</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inyectar RPU</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Ensamblar MKV</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Validación final</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Finalizar</span></div>
      </div>
    </div>

    <h2 id="p-recommendation">🎯 Mantener MKV vs Inyectar RPU — recomendación automática</h2>
    <p>Antes de gastar 25 minutos procesando, la app analiza si el bin del repo realmente aporta calidad sobre el MKV original. Si tu reproductor compatible con CMv4.0 (p3i T4 / Sony / LG modernos) puede hacer la conversión al vuelo en runtime con el mismo resultado visible, la app te lo dice y puedes cerrar el proyecto sin procesar nada. Esta decisión la toma un modelo que mira los datos del bin (no su nombre ni su tag).</p>

    <h3>Calidad del bin — clasificación CORE / CORE+ / FULL</h3>
    <p>Cuando descargas un bin del repo DoviTools, la app lo abre y analiza la <strong>riqueza real</strong> de su contenido CMv4.0 — no se fía del nombre del fichero ni del tag de la hoja. Mira cuántos trims únicos lleva, qué porcentaje de frames tienen trabajo del colorista y si usa los campos exclusivos de CMv4.0. Hay cuatro niveles:</p>
    <table>
      <tr><th>Calidad</th><th>Etiqueta del MKV</th><th>Qué significa</th></tr>
      <tr><td><strong>FULL</strong></td><td><code>[CMv4 FULL]</code></td><td>Master CMv4.0 trabajado a fondo — el colorista usó el toolkit completo (target_mid_contrast, clip_trim). Calidad máxima posible. Típico de BDs UHD recientes de WB y estudios pulidos.</td></tr>
      <tr><td><strong>CORE+</strong></td><td><code>[CMv4 CORE+]</code></td><td>Master con grading dinámico shot-a-shot intenso (combos altos relativos al número de escenas). Sin los campos extras de CMv4.0 pero con mucha intervención del colorista. Ej: 28 años después (2025).</td></tr>
      <tr><td><strong>CORE</strong></td><td><code>[CMv4 CORE]</code></td><td>Master CMv4.0 estándar de streaming (Apple TV+, Disney+, Netflix). Trabajado pero con cambios poco frecuentes. Mejor que CMv2.9 puro pero sin los campos extras.</td></tr>
      <tr><td><strong>Sintético</strong></td><td>— (no aplica)</td><td>El bin solo tiene el wrapper estructural CMv4.0 sin trims reales. Equivale a la conversión al vuelo que hace tu reproductor. Procesarlo no aporta visible. La app recomienda mantener el MKV actual.</td></tr>
    </table>
    <p>La etiqueta se aplica automáticamente al MKV de salida — por ejemplo <code>Predator Badlands (2025) [CMv4 FULL].mkv</code>. Si el bin no aporta sobre la conversión al vuelo, no se procesa nada y el nombre original se conserva.</p>

    <h3>El árbol de decisión del modelo</h3>
    <p>Tras descargar el bin, la app responde a 3 preguntas en orden:</p>
    <ol>
      <li><strong>¿El bin aporta L8 trabajado real?</strong> Si todos los trims L8 son neutros (sintético), recomendación: <strong>Mantener MKV actual</strong>. Sin discusión — procesarlo no daría diferencia visible.</li>
      <li><strong>¿El perfil del bin coincide con el del MKV original?</strong> Source y bin del mismo profile/el_type (P7 FEL↔P7 FEL, P7 MEL↔P7 MEL, P8↔P8) → opción rápida de drop-in disponible. Mismatch (ej. BD P7 FEL + bin P7 MEL) → requiere merge frame-a-frame.</li>
      <li><strong>¿El L2 del bin es idéntico al L2 del MKV original?</strong> Comparación byte-a-byte de todos los valores L2. Si idéntico → drop-in seguro (sustituir RPU del bin íntegro, ~30s). Si difiere → merge selectivo preservando el L2 del original (regla: nunca degradar metadata existente).</li>
    </ol>

    <h3>Las cuatro acciones posibles</h3>
    <table>
      <tr><th>Acción</th><th>Cuándo</th><th>Qué hace</th></tr>
      <tr><td><strong>Mantener MKV actual</strong></td><td>Bin sintético, sin bin, o el bin no aporta sobre la conversión al vuelo</td><td>Cierra el proyecto sin tocar el MKV original. Tu reproductor compatible con CMv4.0 hace la conversión en runtime con el mismo resultado.</td></tr>
      <tr><td><strong>Inyectar RPU CMv4.0 (rápido)</strong></td><td>Perfil coincide + L2 idéntico</td><td>Sustituye el RPU del MKV por el del bin, completo. Operación de ~30 segundos sin tocar el HEVC. Internamente: drop-in.</td></tr>
      <tr><td><strong>Inyectar RPU CMv4.0 (preserva L2)</strong></td><td>Perfil distinto O L2 diferente</td><td>Inyecta solo los niveles CMv4.0 [3,8,9,11,254] del bin manteniendo intacto el L2 del MKV original (para compatibilidad con reproductores CMv2.9-only). Operación más lenta (~15-20 min).</td></tr>
      <tr><td><strong>Forzar inyección</strong> <em>(override del usuario)</em></td><td>El usuario decide procesar aunque el modelo recomiende mantener</td><td>Útil para archivar la versión CMv4.0 "completa" por compatibilidad con otros equipos, aunque visualmente equivale a la conversión al vuelo. Botón "Inyectar RPU igualmente" cuando la recomendación es mantener.</td></tr>
    </table>

    <div class="help-callout help-callout-info">
      <strong>Setup multi-reproductor:</strong> el modelo está pensado para que el resultado sea correcto en cualquier cadena (chip CMv4.0-aware, LLDV CMv2.9-only, etc.). Por eso "Inyectar RPU (preserva L2)" no transfiere el L2 del bin aunque el bin lo tenga — preservar el L2 del MKV original garantiza que las cadenas CMv2.9-only siguen viendo lo correcto. Resultado: ambas cadenas ven lo mejor disponible para su versión.
    </div>

    <h2 id="p-phases">📋 Qué hace cada fase (y qué ves tú)</h2>

    <h3>Pre-flight — validación rápida del bin antes de empezar</h3>
    <p>Cuando arrancas un proyecto con un bin pre-seleccionado y modo auto activado, hay un <strong>pre-check del bin que se ejecuta antes de Fase A</strong>. Su objetivo es simple: si el bin no aporta CMv4.0, abortar inmediatamente con mensaje claro <em>antes</em> de gastar los ~12 min que tarda Fase A en extraer el HEVC del Blu-ray.</p>
    <ul>
      <li><strong>Drive (repo DoviTools)</strong>: descarga el .bin (~5s, son 30-50 MB típicos) y corre <code>dovi_tool info --summary</code> sobre él.</li>
      <li><strong>MKV (extraer de otro MKV)</strong>: extrae el RPU del MKV que indiques con ffmpeg + dovi_tool extract-rpu (~30s-2min según tamaño).</li>
      <li><strong>Carpeta local</strong>: copia el .bin al workdir y lo analiza.</li>
    </ul>
    <p>Si el bin <strong>no es CMv4.0</strong> (caso típico: bins "P5 to P8 transfer" del repo, que solo cambian profile sin upgrade de CM) → aborta antes de Fase A con mensaje en el log explicando exactamente por qué y qué buscar como alternativa. Si <strong>pasa</strong> → Fase A arranca y Fase B reutiliza el bin del workdir sin re-descargar.</p>
    <div class="help-callout help-callout-info">
      <strong>Bloqueante por diseño</strong>: durante el pre-flight la sesión está en <code>running_phase="preflight"</code>, lo que impide que el auto-pipeline lance Fase A en paralelo. Cancelable como cualquier otra fase. Si se aborta, no se gasta nada del análisis pesado.
    </div>
    <div class="help-callout help-callout-success">
      <strong>Resiliente al cliente</strong>: el pre-flight (y todas las fases automáticas que vienen después) se ejecutan en el servidor de forma independiente del navegador. Aunque cierres la pestaña, se duerma el Mac o falle la red, el job continúa hasta el final. Al volver a la app verás el estado actualizado y el log completo en el panel del proyecto.
    </div>

    <h3>Fase A — Analizar el Blu-ray de origen</h3>
    <p>Fase A hace más de lo que su nombre sugiere: no es solo "detectar qué tienes", es también <strong>extraer el material que servirá de referencia para todas las validaciones posteriores</strong>. En concreto:</p>
    <ul>
      <li><strong>Detecta el profile DV</strong>: P7 FEL (el 99% de los Blu-ray UHD), P7 MEL (primeros BDs DV 2017-2018) o P8 (streaming). Esto determina toda la ruta del pipeline.</li>
      <li><strong>Extrae el RPU completo del Blu-ray</strong> a un fichero <code>.bin</code> temporal. Es el paso más pesado — tarda un buen rato, especialmente en películas largas, porque requiere leer el stream HEVC entero y procesar los NAL units con <code>dovi_tool</code>. Este RPU es la <em>línea base</em> contra la que se comparará el bin target en las validaciones.</li>
      <li><strong>Cuenta los frames exactos</strong> de la película. Esta cifra es crítica: la validación crítica de frames en gates solo pasa si el bin target tiene exactamente el mismo número (tolerancia cero).</li>
      <li><strong>Captura metadata L1/L5/L6</strong>: MaxCLL/MaxFALL dinámico, offsets de letterbox, MaxCLL estático. Todo esto se usará en las validaciones soft/críticas para decidir si el target encaja con este master.</li>
      <li><strong>Detecta CM version actual</strong>: v2.9 vs v4.0 del disco, para saber si el upgrade es aplicable.</li>
    </ul>
    <div class="help-callout help-callout-info">
      <strong>Lo que ves:</strong> spinner y log con los pasos de extracción. El tiempo depende del tamaño del MKV — desde ~30 segundos en una película corta hasta varios minutos en películas de más de 2 horas con bitrates altos. Al terminar, la app salta a Fase B con el RPU source ya guardado en el workdir del proyecto.
    </div>

    <h3>Fase B — Preparar el RPU target</h3>
    <p>Eliges de dónde viene el bin CMv4.0 que vas a transferir a tu MKV. Tres opciones en el modal:</p>
    <ol>
      <li><strong>📦 Repo DoviTools</strong> <em>(recomendado)</em>: descarga directa desde el repositorio compartido. Un clic, sin backups locales. La app lo descarga en segundo plano.</li>
      <li><strong>🎬 Extraer de MKV</strong>: si ya tienes en casa un MKV con CMv4.0 (por ejemplo un WEB-DL reciente), la app extrae el RPU de ese fichero. Útil para casos que no están en el repo.</li>
      <li><strong>📁 Carpeta local</strong> <em>(residual)</em>: para .bin que ya tenías descargados previamente.</li>
    </ol>
    <p>En cuanto el bin está en el workdir, la app lee su metadata con <code>dovi_tool info --summary</code>: profile, CM version, niveles presentes (L1, L2, L5, L6, L8, L9…), scene/frame count. Con esta metadata lista, se cierra Fase B y se ejecuta el siguiente bloque: las validaciones.</p>

    <h3>🛡️ Validaciones (trust gates) — el punto de decisión</h3>
    <p>Entre Fase B y Fase C, la app <strong>compara la metadata del bin target con la que Fase A extrajo del Blu-ray</strong>. Esto no es una fase (no hace trabajo nuevo de procesado), es una decisión basada en la comparación. No aparece como letra en los diagramas pero sí como marcador 🛡️, porque es donde el pipeline elige entre ruta auto o ruta manual.</p>
    <p>Lo que se compara:</p>
    <ul>
      <li><strong>Número de frames</strong> — tolerancia cero. Si difieren, el bin es para otra edición.</li>
      <li><strong>CM version</strong> — debe ser v4.0 en el target; si no, no hay upgrade posible y se aborta.</li>
      <li><strong>Presencia de L8</strong> — el nivel que hace útil el CMv4.0.</li>
      <li><strong>L5 offsets</strong> — si el letterbox difiere mucho, los cortes no coinciden.</li>
      <li><strong>L1 / L6 divergencias</strong> — validaciones soft: divergencia no aborta, solo avisa.</li>
    </ul>
    <p>La app pinta un resumen de las validaciones en el log y toma una decisión:</p>
    <ul>
      <li><strong>Todas las críticas pasan</strong> → <em>trusted</em>. El pipeline marca el bin como pre-validado y <strong>salta Fase D (revisión visual)</strong> y — en algunos casos — también Fase C (no hace falta medir luminancia si no va a haber chart).</li>
      <li><strong>Alguna crítica falla</strong> → <em>not trusted</em>. El pipeline ejecuta la ruta completa: separar capas, generar el chart de luminancia, pedir tu revisión visual en Fase D.</li>
      <li><strong>Alguna crítica aborta</strong> (CM no es v4.0, sin L8, L5 muy divergente…) → error claro: el bin no sirve para este disco.</li>
    </ul>
    <div class="help-callout help-callout-info">
      <strong>Detalle importante:</strong> estas validaciones son posibles <em>porque Fase A ya había extraído el RPU source</em>. Si Fase A se saltara, no habría referencia contra la que comparar. Esa es la razón de por qué Fase A dedica tanto tiempo a extraer el RPU aunque aparentemente solo quieras "detectar el profile" — ese trabajo se reutiliza aquí.
    </div>

    <h3>Fase C — Separar las capas del vídeo</h3>
    <p>Los Blu-ray DV Profile 7 tienen la imagen partida en dos capas dentro del mismo fichero: la <strong>Base Layer</strong> (BL) es el HDR10 que vería una TV sin Dolby Vision, y la <strong>Enhancement Layer</strong> (EL) es la corrección fina que le suma DV. Para poder sustituir el RPU hay que separarlas. La app hace ese split automáticamente y mide los niveles de luminancia frame a frame para dibujar el chart de Fase D.</p>
    <div class="help-callout help-callout-success">
      <strong>Se puede saltar:</strong> si tu bin es un drop-in (ver casuísticas), no hace falta separar nada. Si los trust gates pasan, tampoco hace falta medir luminancia porque no vas a pasar por la revisión visual. En esos casos esta fase se omite y ganas minutos.
    </div>

    <h3>Fase D — Revisión visual (el corazón del pipeline)</h3>
    <p>Aquí es donde la app te pide que tomes el control. Te muestra un chart con dos curvas superpuestas: la roja es la luminancia escena a escena del BD original, la azul es la del bin target. Si ambas tienen la misma forma, están alineadas. Si hay offset horizontal entre ellas, hay desfase de frames que hay que corregir antes de inyectar — si no, el resultado final tendría escenas oscuras cuando deberían ser brillantes y viceversa.</p>
    <ul>
      <li>Presets de <strong>zoom</strong> (30s / 1min / 5min / 30min / Todo) para inspeccionar el inicio, donde suelen estar los desfases por logos de estudio.</li>
      <li>Botón <strong>"Detectar offset"</strong> que sugiere automáticamente cuántos frames eliminar o duplicar.</li>
      <li>Un <strong>medidor de confianza</strong> de 0 a 100%: mide la similitud de las dos curvas. El botón "Confirmar sync" solo se activa cuando Δ frames = 0 y la confianza supera el 85%.</li>
    </ul>
    <div class="help-callout help-callout-success">
      <strong>Se puede saltar:</strong> si los trust gates pasaron, la app considera que el bin ya está validado por la comunidad y no necesitas revisión visual. Salta directa a Fase F. Si quieres auditar el resultado aunque sea auto-validado, hay un toggle para forzar la revisión completa.
    </div>

    <h3>Fase E — Aplicar corrección (solo si hace falta)</h3>
    <p>Si en Fase D detectas que las curvas están desalineadas, esta es la fase que corrige. Pulsas <strong>"Aplicar"</strong> y la app elimina o duplica frames al inicio del bin según indiques. Las correcciones se <strong>acumulan</strong>: si aplicas -3 y luego +1, el resultado neto es -2. Si te equivocas, el botón <strong>"Resetear al original"</strong> devuelve el bin a cómo vino.</p>
    <p>Esta fase no avanza el pipeline — es una herramienta que usas dentro de Fase D. Solo cuando pulsas "Confirmar sync" en D pasas a la siguiente.</p>

    <h3>Fase F — Inyectar el RPU CMv4.0</h3>
    <p>Aquí la app sustituye el RPU v2.9 original del Blu-ray por el CMv4.0 del target. La operación concreta varía según la combinación source/target:</p>
    <ul>
      <li><strong>Drop-in FEL</strong>: el bin es un RPU P7 FEL CMv4.0 compatible byte a byte — se inyecta directo sin separar capas. Caso más limpio.</li>
      <li><strong>Merge con source P7 FEL</strong>: el RPU del Blu-ray tiene una capa de mejora real (BL+EL). La app transfiere los niveles <code>[1, 2, 3, 6, 8, 9, 10, 11, 254]</code> del bin al RPU FEL — incluye los niveles "comunes" L1/L2/L6 porque en FEL el grading WEB restaurado suele ser más afinado que el L1 legacy del disco. Preserva el corte/aspect ratio (L5) del BD. Resultado: P7 FEL CMv4.0 completo.</li>
      <li><strong>Merge con source P7 MEL o P8</strong>: el RPU del Blu-ray ya describe los píxeles finales del disco (sin capa de mejora). La app transfiere SOLO los niveles exclusivos de CMv4.0 <code>[3, 8, 9, 11, 254]</code> — los brillos por escena (L1), trims por display (L2), corte (L5) y peak/max (L6) se quedan del BD porque describen tus píxeles, no los del WEB target. Resultado: P8.1 CMv4.0 sin alterar el carácter del disco.</li>
      <li><strong>P7 MEL → P8.1 directo</strong>: si el bin coincide con la BL del MEL (drop-in P8), inyección limpia sin merge. Se descarta el EL del MEL (no aporta sobre un CMv4.0 moderno) y queda un single-layer ligero.</li>
      <li><strong>P8 directo</strong>: source y target son P8 con CMv4.0 — inyección limpia, single-layer.</li>
    </ul>
    <div class="help-callout help-callout-info">
      <strong>Por qué dos listas de niveles distintas</strong>: en P7 FEL el contenido BL+EL combinado a veces diverge ligeramente del L1 "estático" del disco, así que el L1 restaurado del WEB ofrece tone-mapping más afinado escena a escena. En P7 MEL y P8 el L1 del BD <em>es</em> la verdad del contenido — sobreescribirlo con datos calibrados para otro master daría brillos incorrectos en TVs HDR. Las dos listas coinciden exactamente con la implementación de referencia <a href="https://github.com/bbeny123/remuxer" target="_blank" rel="noreferrer">bbeny123/remuxer</a> y con las recomendaciones de la docs oficial de <a href="https://github.com/quietvoid/dovi_tool/blob/main/docs/editor.md" target="_blank" rel="noreferrer">dovi_tool</a>.
    </div>

    <h3>Fase G — Ensamblar el MKV final</h3>
    <p>El vídeo con el RPU CMv4.0 se junta con el audio, subtítulos y capítulos del Blu-ray original. El MKV resultante se escribe con una barra de progreso real (no estimada). Se escribe con sufijo temporal y se renombra atómicamente al nombre final al acabar — si la app se corta a mitad, nunca queda un MKV a medias con el nombre definitivo.</p>

    <h3>🛡️ Validación final — antes de Fase H</h3>
    <p>Igual que en el punto B→C, aquí hay otro <em>gate</em> entre G y H: la app verifica que el MKV final tiene el número de frames esperado y que la estructura del fichero Matroska es correcta. Si algo falla, el MKV se rechaza y el proyecto se marca con error (se puede rehacer desde la fase que quieras).</p>
    <div class="help-callout help-callout-info">
      <strong>Dos rutas de validación según el modo:</strong>
      <ul style="margin:6px 0 0 0; padding-left:18px">
        <li><strong>Drop-in FEL puro</strong> (caso típico con bins de DoviTools): la cadena upstream ya garantiza que el output es Profile 7 FEL CMv4.0 — el bin pasó pre-flight como CMv4.0, los <em>trust gates</em> de Fase B dieron OK, y <code>inject-rpu</code> es una operación determinista que copia el bin íntegro al stream HEVC. Por eso la Fase H se reduce a <code>ffprobe</code> (frame count) + <code>mkvmerge -J</code> (integridad del Matroska). Tarda segundos.</li>
        <li><strong>Merge CMv4.0</strong> (cuando el bin necesita transferir levels al RPU del Blu-ray): el RPU final viene de un merge frame-a-frame, así que se valida con la máxima exigencia. La app extrae el <strong>RPU completo del HEVC pre-mux</strong> con <code>dovi_tool extract-rpu</code> y verifica frame a frame que: (1) el frame count del RPU coincide exactamente con el esperado (tolerancia ±2), (2) la metadata reporta <strong>CM v4.0</strong>, (3) el <em>el_type</em> es el esperado según el source workflow, (4) hay bloques <strong>L8</strong> presentes (los trims que hacen útil el upgrade). Si cualquiera falla se aborta antes del rename — el MKV temporal queda en disco con sufijo <code>.tmp</code> para inspección. Tarda 3-8 min según peli, comparable al tiempo de extract original.</li>
      </ul>
    </div>

    <h3>Fase H — Finalizar</h3>
    <p>Si la validación final pasa, la app mueve el MKV a <code>/mnt/output/</code>, limpia los ficheros temporales del workdir y marca el proyecto como completo. Es el único paso en el que el fichero aparece en su ubicación final — antes de eso vive con sufijo <code>.tmp</code> para evitar que quede un MKV a medias si algo se corta.</p>

    <h2 id="p-gates">🛡️ Cómo decide la app entre automático y manual</h2>
    <p>Tras preparar el bin en Fase B, la app lo compara automáticamente contra el RPU original del Blu-ray. A esta comparación la llamamos <strong>trust gates</strong> (puertas de confianza). Si el bin pasa todos los críticos, la app lo marca como "pre-validado por la comunidad" y <strong>salta las fases manuales</strong> (D y a veces C). Así un pipeline que de otro modo duraría ~1 hora se completa en ~20-25 minutos.</p>

    <h3>Gates críticos (tienen que pasar todos)</h3>
    <table>
      <tr><th>Criterio</th><th>Qué se comprueba</th><th>Qué pasa si falla</th></tr>
      <tr><td><strong>Número de frames</strong></td><td>El bin tiene exactamente los mismos frames que el Blu-ray — sin tolerancia</td><td>El bin es para una edición distinta (theatrical vs extended) o se creó mal. La app abre Fase D para que alinees manualmente.</td></tr>
      <tr><td><strong>CM version</strong></td><td>El bin tiene que ser CMv4.0 (no v2.9)</td><td>Sin CMv4.0 no hay upgrade posible — la app aborta y te pide elegir otro bin.</td></tr>
      <tr><td><strong>Presencia de L8</strong></td><td>El bin contiene los trims L8 (los que hacen útil el upgrade)</td><td>Bin "CMv4.0 vacío" que solo renombra niveles sin añadir información nueva. No aporta sobre el original — se rechaza.</td></tr>
      <tr><td><strong>L5 (letterbox)</strong></td><td>Los offsets de recorte del bin coinciden con los del BD en ≤ 5 píxeles</td><td>5-30 px = aviso (edición similar, puede valer). <strong>&gt; 30 px aborta</strong> — el master tiene un corte/aspecto radicalmente distinto.</td></tr>
    </table>

    <h3>Gates informativos (no bloquean, solo alertan)</h3>
    <table>
      <tr><th>Criterio</th><th>Qué se compara</th><th>Qué significa una divergencia grande</th></tr>
      <tr><td><strong>L6 (metadata estática)</strong></td><td>MaxCLL/MaxFALL del contenedor HDR10</td><td>El master tiene el brillo global recalibrado — normalmente significa otro grading.</td></tr>
      <tr><td><strong>L1 (metadata dinámica)</strong></td><td>MaxCLL promedio por escenas</td><td>Color grading distinto escena a escena. El upgrade seguirá funcionando pero el carácter de la imagen puede cambiar.</td></tr>
    </table>

    <div class="help-callout help-callout-info">
      <strong>Modo "auditar antes de confiar":</strong> aunque el bin pase todos los gates, puedes pedir a la app que te enseñe Fase D igualmente para comprobar las curvas con tus propios ojos antes de inyectar. Es el toggle "forzar revisión interactiva" del modal de nuevo proyecto.
    </div>

    <h2 id="p-casos">🌳 Casuísticas completas por tipo de source</h2>
    <p>Cada casuística combina el <strong>tipo de Blu-ray de origen</strong> con el <strong>tipo de bin CMv4.0 disponible</strong>. La app soporta las tres fuentes habituales (P7 FEL, P7 MEL, P8.1) cruzadas con los cuatro tipos de target, y elige automáticamente la ruta que tiene sentido en cada caso. Los pasos en color son los que se ejecutan; los grises son los que se saltan.</p>
    <p style="font-size:12px; color:var(--text-3); margin:-4px 0 14px">Organización: <strong>(1)</strong> source P7 FEL — el caso más frecuente, 7 variantes; <strong>(2)</strong> source P7 MEL — BDs DV 2017-2018, 4 variantes que siempre producen P8.1 single-layer; <strong>(3)</strong> source P8.1 — MKVs ya single-layer (WEB-DL o MEL ya convertido), 4 variantes de refinamiento a P8.1 mejorado.</p>

    <h3 style="margin-top:14px; color:var(--blue); font-size:15px">① Source <code>P7 FEL</code> — Blu-ray UHD con capa de mejora completa</h3>
    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P7 FEL + target Retail P7 FEL CMv4.0 (drop-in)</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Descargar bin</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">trusted ✓</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux</span><span class="cmv40-ph-mod">no hace falta</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">gates trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección</span><span class="cmv40-ph-mod">Δ=0 gates</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inyectar</span><span class="cmv40-ph-mod">sin merge</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux</span><span class="cmv40-ph-mod">sin mux dual</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Validar</span></div>
      </div>
      <div class="help-pipeline-diagram-sub">Caso más rápido y limpio (~20 min en NAS). El bin descargado se inyecta directo sin tocar el vídeo; la validación final comprueba que el RPU del MKV resultante es byte-idéntico al que has descargado. Ideal cuando el repo tiene el bin exacto para tu edición.</div>
    </div>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P7 FEL + target Retail P7 MEL CMv4.0</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Descargar bin</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">trusted ✓</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux BL</span><span class="cmv40-ph-mod">EL descartado</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">gates trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección</span><span class="cmv40-ph-mod">Δ=0 gates</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inyectar en BL</span><span class="cmv40-ph-mod">sin merge</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux</span><span class="cmv40-ph-mod">single-layer</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Validar</span></div>
      </div>
      <div class="help-pipeline-diagram-sub">Hay bin P7 MEL CMv4.0 retail en el repo (poco común). El MEL original del Blu-ray no añade precisión de color respecto al bin target, así que la app se queda con el bin y descarta el EL. Resultado: P7 MEL CMv4.0, compatible con reproductores DV de gama media.</div>
    </div>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P7 FEL + target Retail P5→P8 (transfer CMv4.0)</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Descargar bin</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">trusted ✓</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux BL+EL</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">gates trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección</span><span class="cmv40-ph-mod">Δ=0 gates</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Merge + inyectar</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Validar</span></div>
      </div>
      <div class="help-pipeline-diagram-sub">El bin es un P5/P8 con trims CMv4.0. La app transfiere esos trims al RPU P7 del Blu-ray preservando el FEL original. Mantienes toda la precisión de color del UHD disc y ganas los niveles nuevos de CMv4.0 — el mejor de los dos mundos.</div>
    </div>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P7 FEL + target P8.x retail (merge CMv4.0 → P7 conservando FEL)</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Descargar P8.x</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">trusted ✓</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux BL+EL</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">si gates OK</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Merge + inject</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux dual-layer</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Validar</span></div>
      </div>
      <div class="help-pipeline-diagram-sub"><span class="help-pill help-pill-retail">Retail</span> El bin es un P8.1 retail típico (por ejemplo un WEB-DL reciente). En FEL la app transfiere los niveles <code>[1,2,3,6,8,9,10,11,254]</code> al RPU P7 del Blu-ray — incluye L1/L2/L6 deliberadamente porque el grading WEB restaurado es más afinado que el legacy del disco. El EL del BD se preserva intacto. Resultado: P7 FEL CMv4.0 con toda la calidad del disco + los niveles refinados.</div>
    </div>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P7 FEL + target extraído de otro MKV</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Extract-rpu del MKV</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">NO trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux + per-frame</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">obligatoria</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección si Δ≠0</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inject</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Validar</span></div>
      </div>
      <div class="help-pill help-pill-retail"></div>
      <div class="help-pipeline-diagram-sub">Cuando tienes en casa un MKV con CMv4.0 (por ejemplo un WEB-DL reciente que quieres portar al master del Blu-ray) y el repo no tiene el bin exacto. La app extrae el RPU de ese MKV y lo usa como target. <strong>Siempre pasa por Fase D</strong> porque no hay pre-validación comunitaria — tú eres quien garantiza que está alineado.</div>
    </div>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P7 FEL + target Generated (sin retail disponible)</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Descargar bin gen.</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">NO trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux + per-frame</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">obligatoria</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Merge + inject</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux dual-layer</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Validar</span></div>
      </div>
      <div class="help-pill help-pill-gen"></div>
      <div class="help-pipeline-diagram-sub">RPU sintético creado algorítmicamente cuando no existe un master CMv4.0 oficial de la película. <strong>Rama completa obligatoria</strong> — los trims los calcula un script a partir del BD, no los ha aprobado un colorista, así que siempre revisas visualmente aunque el número de frames coincida. Calidad: mejor que el v2.9 original en TV CMv4.0-aware, pero un escalón por debajo de un bin retail.</div>
    </div>

    <h3 style="margin-top:20px; color:var(--blue); font-size:15px">② Source <code>P7 MEL</code> — Blu-ray UHD con Minimal EL (típico 2017-2018)</h3>
    <p style="font-size:12px; color:var(--text-3); margin:-4px 0 10px">El MEL no aporta precisión de color real respecto a un target CMv4.0 moderno. En las 4 variantes siguientes la app <strong>descarta el EL</strong> del disco y se queda solo con la Base Layer + el RPU CMv4.0 del target. Resultado: un MKV <strong>P8.1 CMv4.0 single-layer</strong>, más ligero que el origen y visualmente equivalente (o mejor) al BD original en TVs CMv4.0-aware.</p>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P7 MEL → descarte EL → P8.1 CMv4.0 (con bin P8.1 retail)</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar (MEL)</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Descargar bin P8.1</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">trusted ✓</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux solo BL</span><span class="cmv40-ph-mod">EL descartado</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">gates OK</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inject en BL</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux single-layer</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Validar</span></div>
      </div>
      <div class="help-pill help-pill-retail"></div>
      <div class="help-pipeline-diagram-sub">El caso más limpio para BDs MEL: hay bin P8.1 retail firmado por colorista en el repo. Resultado single-layer con calidad máxima disponible para este disco.</div>
    </div>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P7 MEL + target Retail P5→P8 (transfer CMv4.0)</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar (MEL)</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Descargar bin</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">trusted ✓</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux solo BL</span><span class="cmv40-ph-mod">EL descartado</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">gates trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inject en BL</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux single-layer</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Finalizar</span></div>
      </div>
      <div class="help-pipeline-diagram-sub"><span class="help-pill help-pill-retail">Retail</span> El bin viene de un stream P5 o P8 con trims CMv4.0. La app inyecta el RPU directamente en la BL del Blu-ray (descartando el MEL). Resultado: P8.1 CMv4.0 con los trims de la edición streaming pero sobre la BL del disco UHD.</div>
    </div>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P7 MEL + target P8.x retail genérico</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar (MEL)</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Descargar P8.x</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">trusted ✓</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux solo BL</span><span class="cmv40-ph-mod">EL descartado</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">gates trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inject en BL</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux single-layer</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Finalizar</span></div>
      </div>
      <div class="help-pipeline-diagram-sub"><span class="help-pill help-pill-retail">Retail</span> Hay bin P8.1 retail (o P7 MEL/FEL del repo con CMv4.0) pero clasificado como "merge" porque no es match exacto de profile para drop-in directo. La app descarta el EL del MEL y mergea los niveles exclusivos de CMv4.0 <code>[3,8,9,11,254]</code> del bin en el RPU del source — los niveles que describen tus píxeles (L1/L2/L5/L6) se quedan del BD. Resultado: P8.1 CMv4.0 sin alterar el carácter del disco.</div>
    </div>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P7 MEL + target extraído de otro MKV</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar (MEL)</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Extract-rpu del MKV</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">NO trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux BL + per-frame</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">obligatoria</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección si Δ≠0</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inject en BL</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux single-layer</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Finalizar</span></div>
      </div>
      <div class="help-pipeline-diagram-sub">Tienes un MKV propio con CMv4.0 (p.ej. WEB-DL que quieres portar al master del Blu-ray MEL) y el repo no tiene el bin exacto. La app extrae el RPU del MKV y lo usa. <strong>Siempre pasa por Fase D</strong> porque no hay pre-validación — tú garantizas la alineación frame a frame. Salida: P8.1 CMv4.0 single-layer.</div>
    </div>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P7 MEL + target Generated (sin retail disponible)</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar (MEL)</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Descargar bin gen.</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">NO trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux BL + per-frame</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">obligatoria</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inject en BL</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux single-layer</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Finalizar</span></div>
      </div>
      <div class="help-pipeline-diagram-sub"><span class="help-pill help-pill-gen">Generated</span> No existe master CMv4.0 oficial de esta película. RPU sintético algorítmico. Rama completa obligatoria — trims no firmados por colorista, revisión visual siempre. Salida: P8.1 CMv4.0 single-layer. Mejor que v2.9 en TVs aware.</div>
    </div>

    <h3 style="margin-top:20px; color:var(--blue); font-size:15px">③ Source <code>P8.1</code> — MKV ya single-layer (WEB-DL o MEL ya convertido)</h3>
    <p style="font-size:12px; color:var(--text-3); margin:-4px 0 10px">Cuando el source es ya P8.1 (por ejemplo un MKV WEB-DL que guardas, o un Blu-ray MEL que ya habías convertido antes), no hay capas que separar — Fase C prácticamente no hace nada. La app simplemente <strong>reemplaza el RPU del MKV por uno mejor</strong>. Estas 4 variantes tienen como objetivo tomar un P8.1 ya funcional y "mejorarlo" con un RPU CMv4.0 más afinado. Resultado: <strong>P8.1 CMv4.0 mejorado</strong>, mismo formato base pero con metadata más precisa.</p>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P8.1 + target Retail P5→P8 (transfer CMv4.0)</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar (P8.1)</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Descargar bin</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">trusted ✓</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux</span><span class="cmv40-ph-mod">single-layer ya</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">gates trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inject en HEVC</span><span class="cmv40-ph-mod">in-place</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux single-layer</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Finalizar</span></div>
      </div>
      <div class="help-pipeline-diagram-sub"><span class="help-pill help-pill-retail">Retail</span> MKV P8.1 (WEB-DL, conversión previa de MEL, etc.) al que quieres sustituir el RPU por uno CMv4.0 retail de mejor calidad. Caso casi instantáneo — no hay demux ni remux complejo, solo reemplazar el RPU en el HEVC.</div>
    </div>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P8.1 + target P8.x retail genérico</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar (P8.1)</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Descargar P8.x</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">trusted ✓</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Demux</span><span class="cmv40-ph-mod">single-layer ya</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">gates trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-skip"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inject en HEVC</span><span class="cmv40-ph-mod">in-place</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux single-layer</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Finalizar</span></div>
      </div>
      <div class="help-pipeline-diagram-sub"><span class="help-pill help-pill-retail">Retail</span> Bin P8.x retail de otra edición (o un P7 MEL/FEL clasificado como merge). La app transfiere los niveles exclusivos de CMv4.0 <code>[3,8,9,11,254]</code> al RPU del source P8 — el L1/L2/L5/L6 del MKV original se queda intacto. Reemplazo del RPU in-place sin tocar el HEVC. Resultado: P8.1 CMv4.0 refinado.</div>
    </div>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P8.1 + target extraído de otro MKV</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar (P8.1)</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Extract-rpu del MKV</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">NO trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Per-frame solo</span><span class="cmv40-ph-mod">sin demux</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">obligatoria</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección si Δ≠0</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inject en HEVC</span><span class="cmv40-ph-mod">in-place</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux single-layer</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Finalizar</span></div>
      </div>
      <div class="help-pipeline-diagram-sub">Tu source ya es P8.1 y tienes otro MKV con CMv4.0 retail para la misma película. La app extrae el RPU del MKV secundario, pasa por Fase D obligatoria (sin pre-validación), y reemplaza el RPU del source. Salida: P8.1 CMv4.0 con el grading del secundario sobre la imagen del primero.</div>
    </div>

    <div class="help-pipeline-diagram">
      <div class="help-pipeline-diagram-title">Source P8.1 + target Generated (sin retail disponible)</div>
      <div class="cmv40-pp-flow">
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">A</span><span class="cmv40-ph-label">Analizar (P8.1)</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">B</span><span class="cmv40-ph-label">Descargar bin gen.</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-gate"><span class="cmv40-ph-letter">🛡️</span><span class="cmv40-ph-label">Gates</span><span class="cmv40-ph-mod">NO trusted</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">C</span><span class="cmv40-ph-label">Per-frame solo</span><span class="cmv40-ph-mod">sin demux</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">D</span><span class="cmv40-ph-label">Verif. visual</span><span class="cmv40-ph-mod">obligatoria</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">E</span><span class="cmv40-ph-label">Corrección</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">F</span><span class="cmv40-ph-label">Inject en HEVC</span><span class="cmv40-ph-mod">in-place</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">G</span><span class="cmv40-ph-label">Remux single-layer</span></div>
        <span class="cmv40-ph-arrow">→</span>
        <div class="cmv40-ph-pill cmv40-ph-run"><span class="cmv40-ph-letter">H</span><span class="cmv40-ph-label">Finalizar</span></div>
      </div>
      <div class="help-pipeline-diagram-sub"><span class="help-pill help-pill-gen">Generated</span> Source P8.1 sin retail disponible para mejorar el grading. Se usa un bin generated que reemplaza el RPU existente. Calidad intermedia — mejor que un P8.1 sin trims pero sin la precisión de un master nativo.</div>
    </div>

    <div class="help-callout help-callout-success">
      <strong>Compatibilidad source × target — validación automática:</strong> la app rechaza al cerrar Fase B las combinaciones estructuralmente imposibles. En concreto: si tu source es <code>P8.1</code> o <code>P7 MEL</code> (cualquier caso donde el material resultante es single-layer) y eliges un bin target de tipo <em>drop-in P7 FEL</em> o <em>drop-in P7 MEL</em>, el pipeline <strong>aborta con un mensaje explicativo</strong> — no se llega a inyectar, no se pierden los minutos de Fase C ni se produce un MKV inválido. En esos casos elige en su lugar targets P8.x retail, P5→P8 transfer, o generated, que sí son compatibles con sources single-layer.
    </div>

    <h2 id="p-sync">🎛️ El ajustador visual (Fase D) al detalle</h2>
    <p>La Fase D es la pieza más interactiva del pipeline y la que más tiempo puede consumir si te toca usarla. Solo aparece cuando el bin no está pre-validado por la comunidad (o cuando has pedido expresamente revisar aunque lo esté). Su objetivo es que confirmes con tus propios ojos que el bin está alineado frame a frame con el Blu-ray antes de inyectar — porque si hay desfase, el resultado final tendría escenas con los trims aplicados al frame equivocado.</p>

    <h3>Qué representa el chart</h3>
    <ul>
      <li><strong>Eje horizontal:</strong> número de frame de la película (del 0 al total — para una peli de 2 horas a 24 fps son ~170.000).</li>
      <li><strong>Eje vertical:</strong> luminancia máxima por frame (a cuánto llega el pico de brillo en esa escena).</li>
      <li><strong>Curva roja:</strong> las escenas del Blu-ray original.</li>
      <li><strong>Curva azul:</strong> las escenas del bin target.</li>
      <li><strong>Objetivo visual:</strong> que ambas curvas tengan la <strong>misma forma</strong> y estén <strong>perfectamente superpuestas</strong>. Cualquier desplazamiento horizontal entre ellas indica desfase de frames.</li>
    </ul>

    <h3>Controles de la interfaz</h3>
    <table>
      <tr><th>Control</th><th>Para qué sirve</th></tr>
      <tr><td>Presets de zoom (30s / 1min / 5min / 30min / Todo)</td><td>Acceso rápido a rangos típicos. El zoom de 30s es el más útil: cubre los logos de estudio del inicio, que es donde casi siempre está el desfase.</td></tr>
      <tr><td>Inputs "Desde frame" / "Hasta frame"</td><td>Zoom arbitrario a cualquier zona de la película. Útil para cambios de escena con flash de brillo alto que son muy fáciles de alinear a ojo.</td></tr>
      <tr><td>Detectar offset</td><td>La app calcula automáticamente cuántos frames hay que eliminar o duplicar al inicio del bin para alinearlo. Normalmente acierta a la primera.</td></tr>
      <tr><td>Aplicar corrección</td><td>Ejecuta la corrección sugerida. Las correcciones son <strong>acumulativas</strong>: si aplicas −3 y luego +1, el neto es −2. Si vas por pasos puedes converger a la alineación perfecta.</td></tr>
      <tr><td>Resetear al original</td><td>Descarta todas las correcciones y devuelve el bin a cómo llegó. Útil si te equivocas y prefieres empezar de cero.</td></tr>
      <tr><td>Confirmar sync</td><td>Marca la alineación como OK y desbloquea la siguiente fase. Solo se activa cuando Δ=0 y la confianza llega al 85%.</td></tr>
    </table>

    <h3>El medidor de confianza</h3>
    <p>Debajo del chart hay un indicador de 0 a 100%. Mide la similitud entre las dos curvas con un método estadístico que tiene una propiedad importante: solo le interesa la <strong>forma</strong> de las curvas, no sus valores absolutos. Esto es fundamental porque:</p>
    <ul>
      <li>Un bin CMv4.0 puede tener valores de luminancia distintos al v2.9 original (otro grading, otro mastering). Si el medidor se fijara en los valores absolutos, marcaría "distintos" cuando en realidad están perfectamente alineados en el tiempo.</li>
      <li>Como solo se fija en la forma, detecta con precisión los desfases temporales: si hay offset de 5 frames, la confianza se desploma.</li>
      <li>A partir de <strong>85%</strong> la app considera que la alineación es plausible y te deja confirmar.</li>
    </ul>
    <div class="help-callout help-callout-info">
      <strong>Dos condiciones para avanzar:</strong> Δ frames exactamente 0 <em>y</em> confianza ≥ 85%. Si solo tienes una de las dos, el botón "Confirmar" sigue desactivado y te dice cuál falla.
    </div>

    <h2 id="p-problems">❓ Problemas típicos y qué hacer</h2>
    <table>
      <tr><th>Qué ves</th><th>Por qué pasa</th><th>Cómo resolverlo</th></tr>
      <tr><td>"El MKV final no existe" al abrir un proyecto que ya habías completado</td><td>Has borrado o movido el MKV de la carpeta de salida desde fuera de la app</td><td>La app rebobina automáticamente el proyecto al estado "RPU inyectado" y te permite volver a ensamblar el MKV en un clic, sin tener que rehacer las fases caras.</td></tr>
      <tr><td>Error "Invalid PPS index" durante la validación</td><td>Bug histórico de dovi_tool 2.1.x (corregido en 2.3.x, que es la que lleva el contenedor). Si aparece, probablemente es un fichero HEVC parcial o corrupto.</td><td>La app esquiva el bug leyendo desde el HEVC pre-mux y no del MKV final. Si aparece igualmente, relanza la fase — suele ser transitorio por I/O.</td></tr>
      <tr><td>Los trust gates pasan pero en Fase D detectas desfase de frames</td><td>El bin se generó a partir de una edición distinta (theatrical vs extended) o versión streaming recortada</td><td>Busca en la hoja DoviTools el bin de la edición exacta de tu disco. Si no hay, alinea manualmente en Fase D o reporta a la comunidad.</td></tr>
      <tr><td>Aviso "Divergencia L5 &gt; 30 píxeles"</td><td>El master del bin tiene otro aspect ratio o letterbox que tu Blu-ray (típico IMAX vs scope, o cortes específicos de streaming)</td><td>Busca en el repo un bin con la anotación IMAX/Generated que corresponda al ratio que quieres. Si el corte es el mismo pero el aviso aparece, puedes aceptarlo y continuar.</td></tr>
      <tr><td>La inyección se queda colgada o tarda demasiado</td><td>El NAS está saturado con otras tareas de I/O en paralelo</td><td>Cancela, espera a que terminen las otras tareas y relanza. Los proyectos guardan progreso — no pierdes nada.</td></tr>
      <tr><td>El MKV está upgradeado a CMv4.0 pero en tu TV se ve igual que antes</td><td>Tu TV o tu cadena de reproducción no entiende CMv4.0</td><td>Revisa la sección "Por qué upgrade" de este manual: la matriz de TV / firmware detalla qué modelos y reproductores muestran realmente los trims nuevos. No es un problema del MKV — es una limitación del display.</td></tr>
      <tr><td>El pipeline se detiene en Fase B con "CM version ≠ v4.0"</td><td>El bin que has elegido es CMv2.9 — no sirve para upgrade (sería sustituir lo mismo por lo mismo)</td><td>Elige otro bin marcado como CMv4.0. Si el repo solo tiene v2.9 para esta película, el upgrade no es posible por ahora.</td></tr>
      <tr><td>Cerraste la tapa del Mac / la pestaña a mitad de un job y al volver no ves progreso al instante</td><td>El servidor seguía trabajando todo el tiempo. Cuando recarga la web, la app vuelve a engancharse al WebSocket de log y re-hidrata el panel del proyecto desde disco. El gap suele ser de 1-3 segundos.</td><td>Espera unos segundos a que el panel se actualice solo. El log es persistente — verás toda la actividad mientras estabas fuera. Si el job terminó por completo, lo verás como "done" con el MKV en <code>/mnt/output</code>.</td></tr>
      <tr><td>Validación final aborta con "RPU del MKV final NO contiene bloques L8"</td><td>El merge produjo un RPU marcado como CMv4.0 pero sin los trims L8 que dan utilidad real al upgrade. Posible bug puntual de <code>dovi_tool editor</code> en ese título concreto.</td><td>El MKV temporal queda preservado con sufijo <code>.tmp</code> para que lo puedas inspeccionar manualmente. Relanza Fase F+G+H — si vuelve a pasar, el bin target puede estar corrupto: prueba otra fuente del repo.</td></tr>
    </table>

    <h2>🔄 Modo automático vs manual</h2>
    <p>La app tiene un modo "pipeline automático" que encadena todas las fases sin pedirte nada más que crear el proyecto. Activado por defecto cuando el target está pre-validado. Esta tabla resume qué hace cada fase en uno u otro modo:</p>
    <table>
      <tr><th>Fase</th><th>En modo auto</th><th>En modo manual</th></tr>
      <tr><td>A (Analizar BD)</td><td>Se ejecuta al crear el proyecto. Sin intervención.</td><td>—</td></tr>
      <tr><td>B (Preparar target)</td><td>Descarga o extracción automática según tu elección en el modal.</td><td>—</td></tr>
      <tr><td>C (Separar capas)</td><td>Se salta si no hace falta (bin drop-in). Si hace falta, se ejecuta sola.</td><td>—</td></tr>
      <tr><td>D (Revisión visual)</td><td>Se salta si los trust gates han pasado. Caso típico con bin retail del repo.</td><td><strong>Obligatoria</strong> si el bin no está pre-validado (generated, otro master, MKV custom).</td></tr>
      <tr><td>E (Corregir sync)</td><td>No se ejecuta si Δ=0. Si hay desfase pequeño, puede aplicar la corrección sugerida automáticamente.</td><td>Iteras con "Aplicar" y "Detectar offset" hasta alinear.</td></tr>
      <tr><td>F (Inyectar)</td><td>Se encadena tras D (o directamente tras B si el bin es pre-validado).</td><td>Tienes que pulsar "Inyectar RPU" a mano.</td></tr>
      <tr><td>G, H (Ensamblar + Validar)</td><td>Se encadenan solas hasta tener el MKV en la carpeta de salida.</td><td>Pulsaciones manuales en cada paso.</td></tr>
    </table>

    <div class="help-callout help-callout-success">
      <strong>Modo auto-pipeline:</strong> toggle en el modal "Nuevo proyecto" (activado por defecto cuando el target es pre-validado). Con auto on y un bin pre-validado el pipeline completo dura ~20-25 minutos sin que tengas que tocar nada. Con un bin no pre-validado (generated o MKV custom) el pipeline se detiene en Fase D y espera tu revisión — es lo esperado y correcto.
    </div>

    <div class="help-callout help-callout-success">
      <strong>Resiliente al estado del cliente:</strong> el auto-pipeline encadena las fases en el servidor (no en el navegador). Eso significa que el job sigue avanzando aunque cierres la pestaña, cierres la tapa del Mac, el navegador se cuelgue o se vaya el WiFi. Al volver a la app verás el log entero y el estado actualizado del proyecto. La única forma de parar un job en curso es pulsar "Cancelar" en el modal de la fase activa o reiniciar el contenedor — un cierre accidental del cliente no afecta.
    </div>

    <div class="help-sources">
      <b>Fuentes para profundizar</b>
      <a href="https://github.com/quietvoid/dovi_tool" target="_blank" rel="noreferrer">dovi_tool — motor de procesado de RPU</a> ·
      <a href="https://github.com/R3S3t9999/DoVi_Scripts" target="_blank" rel="noreferrer">DoVi_Scripts — scripts de la comunidad DoviTools</a> ·
      <a href="https://forum.makemkv.com/forum/viewtopic.php?t=18602" target="_blank" rel="noreferrer">MakeMKV forum — hilo de referencia sobre DV master</a> ·
      <a href="https://www.avsforum.com/threads/dolby-vision-profile-7-fel-with-full-lossless-audio-truehd-atmos-and-dts-x.3339774/" target="_blank" rel="noreferrer">AVSForum — hilo técnico sobre DV P7 FEL</a>
    </div>
  `,

  // ═══════════════════════════════════════════════════════════════
  // CLAVES Y APIS — guía de configuración centralizada
  // ═══════════════════════════════════════════════════════════════
  keys: `
    <h1>🔐 Claves y APIs — configuración paso a paso</h1>
    <p class="cmv40-help-lead">La app usa dos servicios externos opcionales para enriquecer la experiencia. Ambos tienen <strong>cuota gratuita</strong> y se configuran una sola vez en <strong>⚙︎ Configuración</strong>. Ninguna es obligatoria, pero la app es mucho más útil con ellas.</p>

    <div class="help-subtoc">
      <b>En esta sección</b>
      <a href="#k-overview">Qué necesita cada servicio</a>
      <a href="#k-tmdb">TMDb — paso a paso</a>
      <a href="#k-google">Google API (Drive) — paso a paso</a>
      <a href="#k-configure">Pegarlas en la app</a>
      <a href="#k-troubleshoot">Problemas frecuentes</a>
      <a href="#k-privacy">Privacidad y seguridad</a>
    </div>

    <h2 id="k-overview">📋 Qué necesita cada servicio</h2>
    <table>
      <tr><th>Servicio</th><th>Para qué lo usa la app</th><th>Qué pasa si no lo configuras</th></tr>
      <tr>
        <td><strong>TMDb</strong><br><span style="font-size:11px; color:var(--text-3)">(The Movie Database)</span></td>
        <td>Traducción de títulos ES→EN, ficha extendida (póster, sinopsis, géneros, rating) en la cabecera de cada proyecto. Ayuda también a desambiguar cine no-ASCII (cine asiático).</td>
        <td>Los proyectos se crean igual, pero sin ficha visual y con menor precisión en la búsqueda contra el repo/sheet para títulos con variantes de nombre.</td>
      </tr>
      <tr>
        <td><strong>Google API</strong><br><span style="font-size:11px; color:var(--text-3)">(Drive v3)</span></td>
        <td>Listar y descargar bins <code>.bin</code> del repositorio público DoviTools en Google Drive. También permite lectura del sheet vía API oficial.</td>
        <td>La pestaña "📦 Repo DoviTools" del modal de nuevo proyecto queda vacía. Sigues pudiendo usar el repo descargando bins a mano a una carpeta local, pero pierdes la comodidad del flujo integrado.</td>
      </tr>
    </table>

    <div class="help-callout help-callout-info">
      <strong>No necesitas tarjeta de crédito para ninguna.</strong> Ambas funcionan con cuentas personales gratuitas sin métodos de pago asociados. La app está diseñada para uso doméstico — las cuotas gratuitas del free tier de Google + el acceso TMDb gratuito cubren cualquier uso razonable sin pisar los límites.
    </div>

    <h2 id="k-tmdb">🎬 TMDb — paso a paso</h2>
    <p><strong>The Movie Database</strong> es una base de datos comunitaria de películas con API pública gratuita. No necesita pago ni aprobación comercial — cualquier cuenta personal puede solicitar una API key para uso privado.</p>

    <h3>Conseguir la API key</h3>
    <ol style="font-size:13px">
      <li>Abre <a href="https://www.themoviedb.org/signup" target="_blank" rel="noreferrer">themoviedb.org/signup</a> y crea una cuenta (email + contraseña). Si ya tienes cuenta, entra en <a href="https://www.themoviedb.org/login" target="_blank" rel="noreferrer">themoviedb.org/login</a>.</li>
      <li>Ve a tu perfil → <strong>Settings</strong> (Ajustes) → <strong>API</strong> en el menú lateral izquierdo. Enlace directo: <a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noreferrer">themoviedb.org/settings/api</a>.</li>
      <li>En "Request an API Key" selecciona <strong>"Developer"</strong>. No necesitas "Commercial" — este es gratis.</li>
      <li>Acepta los términos de uso. Rellena el formulario con datos reales:
        <ul style="margin-top:4px">
          <li><em>Application name</em>: <strong>HDO Blu-ray Toolkit</strong> (o el nombre que quieras)</li>
          <li><em>Application URL</em>: cualquier URL válida (por ejemplo <code>http://localhost</code> si no tienes dominio — vale)</li>
          <li><em>Application summary</em>: <em>Uso doméstico para enriquecer metadata de películas en biblioteca personal</em></li>
          <li>Tipo: <em>Personal</em> / <em>Non-commercial</em></li>
        </ul>
      </li>
      <li>Envía. La aprobación es <strong>instantánea</strong> — recarga la página y verás tu key en la misma sección. Hay dos valores:
        <ul style="margin-top:4px">
          <li><strong>API Key (v3 auth)</strong> — una cadena corta tipo <code>1a2b3c4d5e6f...</code> → <em>esta es la que necesitas</em>.</li>
          <li><strong>API Read Access Token (v4 auth)</strong> — una cadena larga JWT — <em>esta NO la uses</em>, la app usa v3.</li>
        </ul>
      </li>
      <li>Cópiala al portapapeles. La configurarás en la app en la sección <a href="#k-configure">"Pegarlas en la app"</a>.</li>
    </ol>

    <h3>Cuota TMDb</h3>
    <p>Sin límite explícito para uso personal. TMDb pide no hacer más de 50 peticiones por segundo (imposible alcanzarlo con uso normal). No hay cuota diaria.</p>

    <h2 id="k-google">🔑 Google API (Drive) — paso a paso</h2>
    <p>Google Cloud te da una API key gratuita con cuotas generosas. Es el mismo mecanismo que usan aplicaciones profesionales — el setup parece intimidante la primera vez, pero se hace en ~10 minutos.</p>

    <h3>Crear un proyecto en Google Cloud</h3>
    <ol style="font-size:13px">
      <li>Abre <a href="https://console.cloud.google.com/" target="_blank" rel="noreferrer">console.cloud.google.com</a> con tu cuenta de Google (cualquier Gmail vale — no hace falta cuenta de pago, solo una cuenta Google normal).</li>
      <li>Si es tu primera vez, Google te pedirá aceptar los términos de Cloud Console. Acepta. No te pedirá tarjeta; el free tier funciona sin ella.</li>
      <li>Arriba a la izquierda, justo al lado del logo de Google Cloud, hay un selector de proyecto. Pulsa sobre él.</li>
      <li>En la ventana que se abre, arriba a la derecha, pulsa <strong>"Nuevo proyecto"</strong>.</li>
      <li>Rellena:
        <ul style="margin-top:4px">
          <li><em>Nombre</em>: <strong>HDO Blu-ray Toolkit</strong> (o lo que quieras)</li>
          <li><em>Organización</em>: deja "Sin organización" si no perteneces a una</li>
          <li><em>Ubicación</em>: "Sin organización"</li>
        </ul>
      </li>
      <li>Pulsa <strong>Crear</strong>. Google tardará unos segundos en aprovisionarlo; verás una notificación cuando esté listo. Asegúrate de que el selector de proyecto arriba muestra tu proyecto nuevo (no otro que tuvieras antes).</li>
    </ol>

    <h3>Habilitar la Google Drive API</h3>
    <div class="help-callout help-callout-warning">
      <strong>Este paso es crítico.</strong> Sin habilitar la API, la key no funciona aunque la generes correctamente. Es el error más común al configurar.
    </div>
    <ol style="font-size:13px">
      <li>Con tu proyecto seleccionado arriba, abre el menú lateral (☰ arriba a la izquierda) → <strong>APIs y servicios</strong> → <strong>Biblioteca</strong>. Enlace directo: <a href="https://console.cloud.google.com/apis/library" target="_blank" rel="noreferrer">console.cloud.google.com/apis/library</a>.</li>
      <li>En el buscador escribe <strong>"Google Drive API"</strong>. Pulsa en la tarjeta del resultado.</li>
      <li>Pulsa el botón azul <strong>"Habilitar"</strong> (Enable). Espera unos segundos. Cuando termine verás una pantalla con métricas de uso (inicialmente a cero).</li>
    </ol>

    <h3>Crear la API key</h3>
    <ol style="font-size:13px">
      <li>Menú lateral → <strong>APIs y servicios</strong> → <strong>Credenciales</strong>. Enlace directo: <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">console.cloud.google.com/apis/credentials</a>.</li>
      <li>Arriba pulsa <strong>"+ Crear credenciales"</strong> → <strong>"Clave de API"</strong>.</li>
      <li>Google genera una cadena larga (formato <code>AIzaSy...</code> — 39 caracteres). Cópiala al portapapeles.</li>
      <li><em>Opcional pero recomendado</em>: en el popup de "Clave de API creada" pulsa <strong>"Editar clave de API"</strong> (o luego desde la lista de credenciales). En la sección <strong>"Restricciones de API"</strong> selecciona <strong>"Restringir clave"</strong> → marca solo <strong>"Google Drive API"</strong>. Guarda.<br>
        <em>Por qué</em>: si la key se filtrara, el atacante solo podría hacer peticiones a Drive, no a otras APIs de Google. Es una buena práctica de seguridad.</li>
    </ol>

    <h3>Cuota Google Drive API</h3>
    <p>Free tier generoso para uso personal:</p>
    <ul style="font-size:13px">
      <li><strong>1.000 peticiones por 100 segundos</strong> por usuario (~10 req/s sostenido)</li>
      <li><strong>20.000 peticiones/día</strong> para lecturas</li>
    </ul>
    <p>Uso típico de la app (abrir el modal de nuevo proyecto una docena de veces al día, descargar algunos bins) está <em>muy</em> por debajo. No verás límites.</p>

    <h2 id="k-configure">📝 Pegarlas en la app</h2>
    <ol style="font-size:13px">
      <li>En la app, pulsa el icono <strong>⚙︎</strong> arriba a la derecha para abrir el modal de Configuración.</li>
      <li>En <strong>"TMDb API key"</strong> pega la cadena corta (v3 auth) del paso TMDb. Pulsa <strong>"Probar"</strong>. Si todo va bien verás ✓ verde y un título de prueba.</li>
      <li>En <strong>"Google API key"</strong> pega la cadena <code>AIzaSy...</code>. Pulsa <strong>"Probar"</strong>.</li>
      <li>En <strong>"Carpeta Drive DoviTools"</strong> pega la URL de la carpeta compartida por la comunidad (busca el enlace vigente en los hilos listados en la sección <strong>📦 Repositorio DoviTools</strong> de este manual). Pulsa <strong>"Probar"</strong>.</li>
      <li>Pulsa <strong>Guardar</strong>. La configuración queda en el servidor; no hay que reintroducirla al reabrir el navegador.</li>
    </ol>

    <h2 id="k-troubleshoot">❓ Problemas frecuentes</h2>
    <table>
      <tr><th>Síntoma</th><th>Causa</th><th>Solución</th></tr>
      <tr>
        <td>"Probar" en Google API key devuelve <strong>403</strong></td>
        <td>La Google Drive API no está habilitada en tu proyecto de Cloud Console</td>
        <td>Vuelve al paso "Habilitar la Google Drive API" — es el más olvidado.</td>
      </tr>
      <tr>
        <td>"Probar" en Google API key devuelve <strong>400 Bad Request</strong></td>
        <td>La clave es sintácticamente inválida (faltó un carácter al copiar)</td>
        <td>Vuelve a copiar desde la consola de Google. Debe tener 39 caracteres y empezar por <code>AIzaSy</code>.</td>
      </tr>
      <tr>
        <td>"Probar" en Google API key devuelve <strong>referer not allowed</strong></td>
        <td>Has restringido la key por HTTP referrer en lugar de por API</td>
        <td>Edita la key en Cloud Console y cambia la restricción de "Restricciones de aplicación" a <strong>None</strong>. Usa solo "Restricciones de API" para acotarla a Drive.</td>
      </tr>
      <tr>
        <td>"Probar" en carpeta Drive devuelve <strong>404</strong></td>
        <td>La URL de la carpeta es incorrecta o la carpeta ha cambiado de propietario</td>
        <td>Busca la URL vigente en los hilos de AVSForum / MakeMKV / Discord DoviTools listados en la sección <strong>📦 Repositorio DoviTools</strong>.</td>
      </tr>
      <tr>
        <td>"Probar" en TMDb key devuelve <strong>401 Unauthorized</strong></td>
        <td>Has pegado el "Read Access Token v4" en lugar de la "API Key v3"</td>
        <td>Vuelve a themoviedb.org/settings/api y copia el campo <strong>"API Key (v3 auth)"</strong> — el corto, no el JWT largo.</td>
      </tr>
      <tr>
        <td>TMDb funciona pero no encuentra la película</td>
        <td>Título demasiado ofuscado por tags del filename</td>
        <td>Usa el botón <strong>🔎 Consulta</strong> del tab CMv4.0 y busca manualmente por título + año. La ficha aparecerá con el título canónico.</td>
      </tr>
    </table>

    <h2 id="k-privacy">🔒 Privacidad y seguridad</h2>
    <ul>
      <li><strong>Dónde se guardan</strong>: ambas keys se persisten en <code>/config/app_settings.json</code> dentro del volumen Docker del servidor, con permisos restrictivos de fichero. Nunca salen de tu NAS / servidor local.</li>
      <li><strong>Qué ve el navegador</strong>: nada. El servidor nunca envía los valores crudos al frontend — solo los últimos 4 caracteres como confirmación de que están configuradas.</li>
      <li><strong>Compartir el fichero</strong>: si haces backup del volumen <code>/config</code>, estás copiando tus keys. Trátalas como credenciales personales.</li>
      <li><strong>Rotación</strong>: si sospechas que una key se ha filtrado, genera una nueva en Google Cloud / TMDb, pégala en la app y borra la anterior desde la consola de origen.</li>
      <li><strong>Variables de entorno</strong>: alternativa a configurar en la UI — puedes pasar <code>TMDB_API_KEY</code> y <code>GOOGLE_API_KEY</code> como env vars al contenedor. La UI tendrá prioridad si están ambas fuentes.</li>
    </ul>

    <div class="help-callout help-callout-info">
      <strong>Resumen:</strong> TMDb es casi instantáneo (cuenta + formulario de aprobación automática). Google es más laborioso porque requiere crear un proyecto en Cloud Console y habilitar la Drive API — ~10 minutos la primera vez. Con ambas configuradas la app alcanza su potencial completo: fichas con póster, sinopsis y géneros; acceso directo a cientos de bins pre-validados; búsqueda robusta en idiomas no latinos.
    </div>

    <div class="help-sources">
      <b>Enlaces útiles</b>
      <a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noreferrer">TMDb — API keys</a> ·
      <a href="https://developer.themoviedb.org/docs/getting-started" target="_blank" rel="noreferrer">TMDb — Docs oficiales</a> ·
      <a href="https://console.cloud.google.com/" target="_blank" rel="noreferrer">Google Cloud Console</a> ·
      <a href="https://console.cloud.google.com/apis/library/drive.googleapis.com" target="_blank" rel="noreferrer">Habilitar Google Drive API</a> ·
      <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">Google Cloud — Credenciales</a> ·
      <a href="https://developers.google.com/drive/api/guides/about-sdk" target="_blank" rel="noreferrer">Google Drive API v3 — Docs</a>
    </div>
  `
};

async function cmv40LookupSearch() {
  const input = document.getElementById('cmv40-lookup-title');
  const yearInput = document.getElementById('cmv40-lookup-year');
  const btn = document.getElementById('cmv40-lookup-btn');
  const results = document.getElementById('cmv40-lookup-results');
  if (!input || !results) return;

  const title = (input.value || '').trim();
  if (!title) {
    results.innerHTML = '<div class="cmv40-lookup-empty">Introduce un título para consultar.</div>';
    input.focus();
    return;
  }
  const year = yearInput?.value ? parseInt(yearInput.value, 10) : null;

  if (btn) btn.disabled = true;
  results.innerHTML = `<div class="cmv40-lookup-loading">
    <span class="cmv40-rec-spinner-inline"></span>
    Buscando coincidencias en TMDb…
  </div>`;

  // Paso 1 — buscar candidatos TMDb. Si hay varios, mostrar selector.
  const search = await apiFetch('/api/cmv40/tmdb-search', {
    method: 'POST',
    body: JSON.stringify({ title, year }),
  });

  if (!search || !search.tmdb_configured) {
    // Sin TMDb: vamos directos con el texto crudo (matching peor pero funcional)
    if (btn) btn.disabled = false;
    await _cmv40LookupFullFetch(results, title, year);
    return;
  }

  const candidates = search.candidates || [];

  if (candidates.length === 0) {
    if (btn) btn.disabled = false;
    // No hay match en TMDb — aún así intentamos contra la hoja/repo por si acaso
    await _cmv40LookupFullFetch(results, title, year);
    return;
  }

  if (candidates.length === 1 || (year && candidates.filter(c => c.year === year).length === 1)) {
    // Una sola coincidencia → consulta directa
    const picked = (year ? candidates.find(c => c.year === year) : null) || candidates[0];
    if (btn) btn.disabled = false;
    await _cmv40LookupFullFetch(results, picked.title_en || picked.title_es || title, picked.year || year);
    return;
  }

  // Más de una — mostrar selector visual
  if (btn) btn.disabled = false;
  _cmv40LookupRenderSelector(results, candidates, title);
}

function _cmv40LookupRenderSelector(container, candidates, queryTitle) {
  const items = candidates.map((c, i) => {
    const poster = c.poster_url
      ? `<img class="cmv40-lookup-pick-poster" src="${escHtml(c.poster_url)}" alt="" loading="lazy">`
      : `<div class="cmv40-lookup-pick-poster cmv40-lookup-pick-noposter">🎬</div>`;
    const rating = c.vote_average > 0
      ? `<span class="cmv40-lookup-pick-rating">★ ${c.vote_average.toFixed(1)}</span>`
      : '';
    const origHtml = (c.title_en && c.title_en !== c.title_es)
      ? `<div class="cmv40-lookup-pick-orig">Original: ${escHtml(c.title_en)}</div>`
      : '';
    const overview = c.overview
      ? `<div class="cmv40-lookup-pick-overview">${escHtml(c.overview)}</div>`
      : '';
    return `
      <button class="cmv40-lookup-pick" type="button"
        onclick="_cmv40LookupPick(${i})"
        data-tmdb-title="${escHtml(c.title_en || c.title_es || queryTitle)}"
        data-tmdb-year="${c.year || ''}">
        ${poster}
        <div class="cmv40-lookup-pick-info">
          <div class="cmv40-lookup-pick-title">
            ${escHtml(c.title_es || c.title_en || '—')}
            ${c.year ? `<span class="cmv40-lookup-pick-year">(${c.year})</span>` : ''}
            ${rating}
          </div>
          ${origHtml}
          ${overview}
        </div>
      </button>`;
  }).join('');

  container.innerHTML = `
    <div class="cmv40-lookup-section">
      <div class="cmv40-lookup-section-title">🎬 ${candidates.length} coincidencias en TMDb para "${escHtml(queryTitle)}"</div>
      <div class="cmv40-lookup-section-desc">Selecciona la película a la que te refieres — la consulta del sheet + repositorio se ejecutará sobre ella.</div>
      <div class="cmv40-lookup-picks">${items}</div>
    </div>`;

  // Guardar candidates en memoria para el handler del click
  _cmv40LookupCandidates = candidates;
}

let _cmv40LookupCandidates = [];

function _cmv40LookupClearYear() {
  const yearInput = document.getElementById('cmv40-lookup-year');
  if (yearInput) {
    yearInput.value = '';
    yearInput.focus();
  }
}

function _cmv40LookupClearTitle() {
  const titleInput = document.getElementById('cmv40-lookup-title');
  const yearInput = document.getElementById('cmv40-lookup-year');
  const results = document.getElementById('cmv40-lookup-results');
  if (titleInput) { titleInput.value = ''; titleInput.focus(); }
  if (yearInput) yearInput.value = '';
  if (results) results.innerHTML = '';
  _cmv40LookupCandidates = [];
}

async function _cmv40LookupPick(idx) {
  const picked = _cmv40LookupCandidates[idx];
  if (!picked) return;
  const results = document.getElementById('cmv40-lookup-results');
  // NO tocamos los inputs del formulario — quedan como el usuario los
  // escribió. Así el año que vea en la casilla siempre refleja SU input,
  // no un valor auto-pegado que pueda envenenar la siguiente búsqueda.
  await _cmv40LookupFullFetch(results, picked.title_en || picked.title_es, picked.year);
}

async function _cmv40LookupFullFetch(container, title, year) {
  container.innerHTML = `<div class="cmv40-lookup-loading">
    <span class="cmv40-rec-spinner-inline"></span>
    Consultando hoja DoviTools + repositorio Drive para <strong>${escHtml(title)}${year ? ` (${year})` : ''}</strong>…
  </div>`;
  const qs = new URLSearchParams({ title });
  if (year) qs.set('year', String(year));
  const qsStr = '?' + qs.toString();
  const [recResp, repoResp, tmdbResp] = await Promise.all([
    apiFetch('/api/cmv40/recommend' + qsStr).catch(() => null),
    apiFetch('/api/cmv40/repo-rpus' + qsStr).catch(() => null),
    apiFetch('/api/cmv40/tmdb-lookup', {
      method: 'POST',
      body: JSON.stringify({ source_mkv_name: title + (year ? ` (${year})` : '') }),
    }).catch(() => null),
  ]);
  _cmv40LookupRenderResults(container, recResp, repoResp, tmdbResp);
}

function _cmv40LookupRenderResults(container, rec, repo, tmdb) {
  if (!rec && !repo && !tmdb) {
    container.innerHTML = '<div class="cmv40-lookup-empty">No se pudo consultar. Revisa la conexión o las API keys.</div>';
    return;
  }

  let html = '';

  // ── 1. Ficha TMDb ─────────────────────────────────────────────
  const tmdbDetails = tmdb?.details || null;
  if (tmdbDetails) {
    html += renderTmdbCardHTML(tmdbDetails) || '';
  } else if (tmdb && !tmdb.tmdb_configured) {
    html += `<div class="cmv40-lookup-warn">⚠️ TMDb API key no configurada — la búsqueda usará solo el texto introducido. Añade la key en <a href="#" onclick="openSettingsModal();return false">⚙︎ Configuración</a> para mejorar el matching ES→EN.</div>`;
  } else if (tmdb) {
    html += `<div class="cmv40-lookup-warn">ℹ️ TMDb no encontró la película con ese título/año. La consulta continúa con el texto crudo.</div>`;
  }

  // ── 2. Sección "Hoja de DoviTools" con su banner de estado/notas ──
  // Reusa exactamente el mismo renderer del modal de Nuevo proyecto, con
  // sus códigos de color (verde/rojo/gris), chips (Fuente·Sync·Verif.),
  // motivo textual + links clicables al sheet original.
  html += `<div class="cmv40-lookup-section">
    <div class="cmv40-lookup-section-title">📋 Hoja de recomendaciones DoviTools</div>
    <div class="cmv40-lookup-section-desc">Lo que dice la comunidad sobre la viabilidad de la conversión — con comentarios, métricas de sync y enlaces a comparativas HDR/plots cuando existen.</div>
    <div id="cmv40-lookup-rec-banner" class="cmv40-rec-banner" style="display:none"></div>
  </div>`;

  // ── 3. Candidatos del repositorio con pipeline previsto ──────
  html += '<div class="cmv40-lookup-section">';
  html += '<div class="cmv40-lookup-section-title">📦 Repositorio DoviTools (bins <code>.bin</code>)</div>';
  html += '<div class="cmv40-lookup-section-desc">Ficheros disponibles para descarga automática. El tag indica qué pipeline se aplicaría.</div>';
  if (!repo || !repo.drive_configured) {
    html += _cmv40RepoUnavailableBanner(repo);
  } else if (repo.error) {
    html += `<div class="cmv40-lookup-warn">${escHtml(repo.error)}</div>`;
  } else if (!repo.candidates || repo.candidates.length === 0) {
    const t = repo.title_en || repo.title_es || '(título)';
    html += `<div class="cmv40-lookup-empty">No hay <code>.bin</code> para <strong>${escHtml(t)}</strong> en el repositorio. Si quieres convertir esta película tendrás que obtener el RPU por otra vía (extraer de otro MKV, bin local, etc.).</div>`;
  } else {
    // Lista plana ordenada por score. El backend ya aplicó bonus retail +0.03
    // — el orden viene correcto. Sin agrupación para no confundir (un
    // P5→P8 source sin provenance marker puede ser mejor que un Generated).
    const bestFilename = repo.candidates[0]?.file?.name || '';
    const renderCand = (c) => {
      const pt = c.predicted_type || 'unknown';
      const prov = c.provenance || '';
      const tagMeta = _cmv40LookupTagMeta(pt);
      const sizeMb = (c.file.size_bytes / 1024 / 1024).toFixed(1);
      const score = Math.round(c.score * 100);
      const isBest = c.file.name === bestFilename;
      const provTag = prov === 'retail'
        ? '<span class="cmv40-lookup-tag tag-ok">🏛 Retail</span>'
        : prov === 'generated'
        ? '<span class="cmv40-lookup-tag tag-warn">⚠️ Generated</span>'
        : '';
      return `
        <li class="cmv40-lookup-candidate ${isBest ? 'best' : ''}">
          <div class="cmv40-lookup-cand-head">
            <span class="cmv40-lookup-tag ${tagMeta.cls}">${tagMeta.icon} ${tagMeta.label}</span>
            ${provTag}
            ${isBest ? '<span class="cmv40-lookup-best">🏆 mejor match</span>' : ''}
            <span class="cmv40-lookup-score">${score}% similitud</span>
            <span class="cmv40-lookup-size">${sizeMb} MB</span>
          </div>
          <div class="cmv40-lookup-cand-path">${escHtml(c.file.path)}</div>
          <div class="cmv40-lookup-cand-pipeline">${_cmv40LookupPipelineSummary(pt, prov)}</div>
        </li>`;
    };
    html += `<ul class="cmv40-lookup-candidates">${repo.candidates.map(renderCand).join('')}</ul>`;
  }
  html += '</div>';

  container.innerHTML = html;

  // Tras inyectar el HTML, renderiza el banner de recomendación en su slot
  // — reusa el mismo renderer de Tab 3 con todos los chips/notas/links.
  if (rec) {
    _cmv40RenderRecommendation(rec, 'cmv40-lookup-rec-banner');
  } else {
    // Fallback raro: si rec no llegó, ocultamos la sección del sheet
    const slot = document.getElementById('cmv40-lookup-rec-banner');
    if (slot) {
      slot.style.display = 'block';
      slot.className = 'cmv40-rec-banner unknown';
      slot.innerHTML = '<div class="cmv40-rec-body">No se pudo consultar la hoja de DoviTools.</div>';
    }
  }
}

function _cmv40LookupTagMeta(pt) {
  if (pt === 'trusted_p7_fel_final') return { icon: '🎯', label: 'Bin P7 FEL', cls: 'tag-ok' };
  if (pt === 'trusted_p7_mel_final') return { icon: '🎯', label: 'Bin P7 MEL', cls: 'tag-ok' };
  // trusted_p8_source cubre tanto P8 retail nativo como P5→P8 transfer.
  // Etiqueta neutra para no asumir uno u otro.
  if (pt === 'trusted_p8_source')    return { icon: '📦', label: 'Bin P8 retail', cls: 'tag-info' };
  return { icon: '❓', label: 'Tipo desconocido', cls: 'tag-warn' };
}

function _cmv40LookupPipelineSummary(pt, provenance) {
  const info = (typeof _CMV40_PIPELINE_PREVIEW !== 'undefined') ? _CMV40_PIPELINE_PREVIEW[pt] : null;
  if (!info) {
    return '<div class="cmv40-lookup-pp-desc">Pipeline se determinará tras descarga y análisis con dovi_tool.</div>';
  }
  return _cmv40PipelinePreviewHTML(info, provenance, null, pt);
}
/**
 * Cierra el modal si el click fue directamente sobre el overlay (no en el contenido).
 * @param {MouseEvent} e
 * @param {string}     id - ID del overlay.
 */
function onModalOverlayClick(e, id) { if (e.target === document.getElementById(id)) closeModal(id); }

// Cerrar con Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay.open:not([data-no-escape])').forEach(m => m.classList.remove('open'));
    TooltipManager.hide();
  }
});

// ═══════════════════════════════════════════════════════════════════
//  APP STATUS
// ═══════════════════════════════════════════════════════════════════

/**
 * Consulta GET /api/status al cargar la app para mostrar el badge de la clave
 * MakeMKV y el banner de aviso si no está configurada.
 */
async function checkAppStatus() {
  const data = await apiFetch('/api/status');
  if (!data) return;
  // Mostrar sección Dev Tools en el sidebar Cola solo si el servidor corre en DEV_MODE
  if (data.dev_mode) {
    document.getElementById('csb-dev-section')?.style &&
      (document.getElementById('csb-dev-section').style.display = '');
  }
}

/**
 * ⚠️ DEV MODE — Encola sesiones fake y simula el pipeline completo.
 * Solo disponible cuando el servidor responde dev_mode: true.
 */
async function devSimulate() {
  const btn = document.querySelector('#csb-dev-section button');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Encolando…'; }
  const data = await apiFetch('/api/dev/simulate', { method: 'POST' });
  if (btn) { btn.disabled = false; btn.textContent = '▶ Simular ejecución'; }
  if (!data) return;
  if (!data.ok) { showToast(data.detail || 'Sin sesiones disponibles', 'warning'); return; }
  showToast(`${data.enqueued?.length ?? 0} sesiones encoladas para simulación`, 'success');
  await loadSessions();
}

// ═══════════════════════════════════════════════════════════════════
//  MODAL NUEVO PROYECTO — ISO picker
// ═══════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════
//  Source selection (v2.6+) — 3 tipos: ISO, carpeta BDMV, ficheros M2TS
//
//  El modal tiene 3 tabs. Cada uno mantiene su propia selección.
//  El estado global `_sourceTab` indica el tab activo; `pickerSelectedIso`
//  (legacy) + `bdmvSelectedPath` + `m2tsSelectedPaths` los valores.
// ═══════════════════════════════════════════════════════════════════

/** Tab activo en el modal "Nuevo proyecto". */
let _sourceTab = 'iso';

/** ISO seleccionado en el picker. @type {string|null} */
let pickerSelectedIso = null;

/** Carpeta BDMV seleccionada. @type {string|null} */
let bdmvSelectedPath = null;

/** Lista de m2ts seleccionados (multi). @type {string[]} */
let m2tsSelectedPaths = [];

/** Tipo de contenido elegido por el usuario en el modal Nuevo proyecto.
 *  null (sin elegir aún, bloquea el resto del modal) / 'movie' / 'series'.
 *  Determina:
 *    - Permite 1 o varios m2ts en el tab M2TS.
 *    - Se envía como media_type_hint a /api/disc-probe — backend respeta
 *      la elección sin auto-detect.
 *  @type {null | 'movie' | 'series'} */
let _contentType = null;

/** Cambia el tipo de contenido (Película / Serie). Desbloquea la zona
 *  de selección de origen la primera vez que se elige. Re-renderiza el
 *  browser activo si afecta a la selección (típicamente m2ts: cambia
 *  entre radio y checkbox). En movie con varios m2ts ya marcados,
 *  reduce la selección al primero. */
function onContentTypeChange(type) {
  if (type !== 'movie' && type !== 'series') return;
  const wasLocked = _contentType === null;
  _contentType = type;
  document.getElementById('ctt-btn-movie')?.classList.toggle('active', type === 'movie');
  document.getElementById('ctt-btn-series')?.classList.toggle('active', type === 'series');
  // Desbloquear la zona de selección de origen + ocultar el banner
  // instructivo "Selecciona arriba si el contenido es Película o Serie".
  document.getElementById('new-project-source-area')?.classList.remove('locked');
  document.getElementById('new-project-locked-banner')?.classList.add('hidden');
  // Actualizar el subtítulo del modal para indicar el paso 2.
  const subEl = document.getElementById('new-project-sub');
  if (subEl) {
    subEl.textContent = type === 'movie'
      ? 'Paso 2: elige el origen (un fichero) y púlsa Analizar.'
      : 'Paso 2: elige el origen (varios episodios) y púlsa Analizar.';
  }
  // En movie no permitimos múltiples m2ts — recortamos al primero
  if (type === 'movie' && m2tsSelectedPaths.length > 1) {
    m2tsSelectedPaths = [m2tsSelectedPaths[0]];
  }
  // Texto del status del browser m2ts según modo
  const m2tsStatusEl = document.getElementById('src-fb-m2ts-status');
  if (m2tsStatusEl) {
    m2tsStatusEl.textContent = type === 'movie'
      ? 'Marca un fichero .m2ts (modo película — solo un MKV de salida)'
      : 'Marca varios ficheros .m2ts — uno por episodio (modo serie)';
  }
  // Re-render del browser m2ts para alternar radio/checkbox visualmente
  if (_srcFb && _srcFb.m2ts && _srcFb.m2ts.entries) {
    _renderSrcFb('m2ts');
  }
  // Cargar el listado de fuentes si es la primera vez que el usuario
  // elige un tipo (la zona estaba bloqueada → no se había cargado).
  if (wasLocked) {
    loadSourcesList();
  }
  _updateAnalyzeButtonState();
}

/** Cache del último listado completo de /api/sources. */
let _sourcesCache = { iso: [], bdmv_folder: [], m2ts: [] };

/** Abre el modal de nuevo proyecto y carga las fuentes disponibles. */
async function openNewProjectModal() {
  if (openProjects.length >= MAX_PROJECTS) {
    showToast(`Máximo ${MAX_PROJECTS} proyectos abiertos. Cierra uno antes de crear otro.`, 'warning');
    return;
  }
  pickerSelectedIso = null;
  bdmvSelectedPath = null;
  m2tsSelectedPaths = [];
  _sourceTab = 'iso';
  // Reset del tipo de contenido a "ningún elegido" en cada apertura.
  // El usuario debe elegir explícitamente Película o Serie antes de
  // poder navegar el browser de origen — esto evita el caso del
  // usuario olvidándose y dejándolo en Película por defecto.
  _contentType = null;
  document.getElementById('ctt-btn-movie')?.classList.remove('active');
  document.getElementById('ctt-btn-series')?.classList.remove('active');
  document.getElementById('new-project-source-area')?.classList.add('locked');
  document.getElementById('new-project-locked-banner')?.classList.remove('hidden');
  const subEl = document.getElementById('new-project-sub');
  if (subEl) subEl.textContent = 'Paso 1: elige el tipo de contenido para empezar.';
  // Reset del botón Analizar. El tab ISO se activa por defecto vía
  // onSourceTabSwitch — NO se referencia ningún select legacy (los
  // antiguos iso-picker-select / bdmv-picker-select / m2ts-picker-list
  // se reemplazaron por file browsers embebidos en el commit UX 1/3).
  const btn = document.getElementById('new-project-analyze-btn');
  if (btn) btn.disabled = true;
  onSourceTabSwitch('iso');  // muestra panel ISO por defecto
  openModal('new-project-modal');
  // No cargamos las fuentes aquí: la zona de origen está bloqueada
  // hasta que el usuario elija Película o Serie. La carga la dispara
  // onContentTypeChange la primera vez que se selecciona un tipo.
}

/** Cambia entre tabs ISO / Carpeta BDMV / Ficheros M2TS. */
function onSourceTabSwitch(tab) {
  _sourceTab = tab;
  ['iso', 'bdmv_folder', 'm2ts'].forEach(t => {
    // El id del botón usa 'bdmv' como abreviatura del tipo bdmv_folder
    const btnId = t === 'bdmv_folder' ? 'source-tab-btn-bdmv' : `source-tab-btn-${t}`;
    const panelId = t === 'bdmv_folder' ? 'source-panel-bdmv' : `source-panel-${t}`;
    const btn = document.getElementById(btnId);
    const panel = document.getElementById(panelId);
    if (btn) btn.classList.toggle('active', tab === t);
    if (panel) panel.style.display = tab === t ? 'block' : 'none';
  });
  _updateAnalyzeButtonState();
}

/** Habilita/deshabilita el botón Analizar según el tipo de contenido,
 *  el tab activo y la selección. Sin tipo elegido (Película/Serie) el
 *  botón queda siempre deshabilitado — la zona de origen también está
 *  bloqueada visualmente. */
function _updateAnalyzeButtonState() {
  const btn = document.getElementById('new-project-analyze-btn');
  if (!btn) return;
  if (_contentType === null) {
    btn.disabled = true;
    return;
  }
  let enabled = false;
  if (_sourceTab === 'iso') enabled = !!pickerSelectedIso;
  else if (_sourceTab === 'bdmv_folder') enabled = !!bdmvSelectedPath;
  else if (_sourceTab === 'm2ts') enabled = m2tsSelectedPaths.length > 0;
  btn.disabled = !enabled;
}

/** Backwards-compat alias — algunos callers antiguos invocan loadIsoPickerList. */
async function loadIsoPickerList() {
  await loadSourcesList();
}

/** Carga inicial de los 3 file browsers embebidos en el modal Nuevo Proyecto.
 *  Cada uno navega /mnt/isos con un filtro de extensión distinto. */
async function loadSourcesList() {
  // Estado de navegación por filtro. Mantenemos el último `sort` elegido
  // por el usuario entre aperturas (es estado de preferencia UI, no de
  // contenido — no lo persistimos en disco pero sí dentro de la SPA).
  const isoSort = _srcFb.iso?.sort || 'name_asc';
  const bdmvSort = _srcFb.bdmv?.sort || 'name_asc';
  const m2tsSort = _srcFb.m2ts?.sort || 'size_desc';
  _srcFb.iso  = { path: '', entries: [], sort: isoSort };
  _srcFb.bdmv = { path: '', entries: [], sort: bdmvSort };
  _srcFb.m2ts = { path: '', entries: [], sort: m2tsSort };
  // Sincronizar el valor del <select> con el sort activo
  const setSelect = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.value = v;
  };
  setSelect('src-fb-iso-sort', isoSort);
  setSelect('src-fb-bdmv-sort', bdmvSort);
  setSelect('src-fb-m2ts-sort', m2tsSort);
  await Promise.all([
    srcFbNavigate('iso', ''),
    srcFbNavigate('bdmv', ''),
    srcFbNavigate('m2ts', ''),
  ]);
}

/** Estado por-filtro del file browser embebido. */
const _srcFb = {
  // sort: ordenación visible en el listado.
  //   - iso/bdmv default: name_asc (orden alfabético, intuitivo en carpetas)
  //   - m2ts default: size_desc (los episodios/películas son los m2ts más
  //     grandes; los pequeños son extras/menús/intros — sort por tamaño
  //     descendente pone los útiles arriba)
  iso:  { path: '', entries: [], sort: 'name_asc' },
  bdmv: { path: '', entries: [], sort: 'name_asc' },
  m2ts: { path: '', entries: [], sort: 'size_desc' },
};

/** Cambia el orden visible de un browser y re-renderiza sin re-fetch. */
function srcFbSetSort(filter, sortMode) {
  if (!_srcFb[filter]) return;
  _srcFb[filter].sort = sortMode;
  _renderSrcFb(filter);
}

/** Ordena las entries de un browser según el modo activo. Carpetas
 *  siempre van arriba (orden alfabético independiente del modo de
 *  fichero para que el árbol siga siendo navegable). */
function _sortSrcFbEntries(entries, sort) {
  const dirs = entries.filter(e => e.type === 'dir');
  const files = entries.filter(e => e.type !== 'dir');
  dirs.sort((a, b) => a.name.localeCompare(b.name, 'es', { numeric: true }));
  files.sort((a, b) => {
    if (sort === 'size_desc') return (b.size_bytes || 0) - (a.size_bytes || 0);
    if (sort === 'size_asc')  return (a.size_bytes || 0) - (b.size_bytes || 0);
    if (sort === 'name_desc') return b.name.localeCompare(a.name, 'es', { numeric: true });
    return a.name.localeCompare(b.name, 'es', { numeric: true });  // name_asc default
  });
  return [...dirs, ...files];
}

/** Navega a una ruta relativa dentro del browser de un filtro concreto.
 *  filter: 'iso' | 'bdmv' | 'm2ts' (los IDs del DOM usan estos prefijos).
 *  relPath: ruta relativa a /mnt/isos (vacío = raíz). */
async function srcFbNavigate(filter, relPath) {
  const listEl = document.getElementById(`src-fb-${filter}-list`);
  const bcEl = document.getElementById(`src-fb-${filter}-breadcrumb`);
  if (listEl) listEl.innerHTML = '<div class="src-fb-loading">⏳ Cargando…</div>';
  if (bcEl) bcEl.textContent = relPath ? `📂 /mnt/isos / ${relPath}` : '📂 /mnt/isos';

  const url = `/api/library/browse?root=downloaded&path=${encodeURIComponent(relPath || '')}&filter=${filter}`;
  const data = await apiFetch(url);
  if (!data) {
    if (listEl) listEl.innerHTML = '<div class="src-fb-empty">⚠ No se pudo leer la carpeta</div>';
    return;
  }
  if (data.error) {
    if (listEl) listEl.innerHTML = `<div class="src-fb-empty">${escHtml(data.error)}</div>`;
    return;
  }

  // Preservar el sort actual al navegar — sin esto cada navegación
  // (entrar a una carpeta) resetearía el orden a undefined.
  _srcFb[filter] = {
    path: data.path || '',
    parent: data.parent,
    entries: data.entries || [],
    sort: _srcFb[filter]?.sort || (filter === 'm2ts' ? 'size_desc' : 'name_asc'),
  };

  _renderSrcFb(filter);
}

/** Renderiza el contenido del browser para un filtro dado. */
function _renderSrcFb(filter) {
  const st = _srcFb[filter];
  const listEl = document.getElementById(`src-fb-${filter}-list`);
  if (!listEl) return;

  // Usamos data-attributes + un único event listener delegado en el
  // contenedor (registrado bajo demanda). Sin onclick inline porque
  // los paths pueden contener comillas/acentos/espacios que rompían
  // el atributo HTML (bug v2.6 — file browser no era navegable con
  // ciertos nombres de carpeta).
  const rows = [];

  // Fila "..": subir un nivel si no estamos en la raíz
  if (st.parent !== null && st.parent !== undefined) {
    rows.push(`
      <div class="src-fb-row" data-action="navigate" data-path="${escHtml(st.parent)}">
        <span class="src-fb-icon">⬆</span>
        <span class="src-fb-name">.. (subir)</span>
      </div>
    `);
  }

  if (!st.entries.length) {
    if (rows.length === 0) {
      // Mensaje específico por filtro — "filtro iso/bdmv/m2ts" no es
      // legible en castellano. Cada tipo dice qué busca exactamente.
      const filterDesc = filter === 'iso' ? 'ficheros .iso'
        : filter === 'bdmv' ? 'carpetas con estructura BDMV'
        : 'ficheros .m2ts';
      listEl.innerHTML = `<div class="src-fb-empty">— Sin ${filterDesc} en esta carpeta —</div>`;
      _attachSrcFbDelegation(filter);
      return;
    }
    listEl.innerHTML = rows.join('');
    _attachSrcFbDelegation(filter);
    return;
  }

  const sortedEntries = _sortSrcFbEntries(st.entries, st.sort || 'name_asc');
  sortedEntries.forEach(e => {
    const path = st.path ? `${st.path}/${e.name}` : e.name;
    const pathAttr = escHtml(path);
    if (e.type === 'dir') {
      if (filter === 'bdmv' && e.is_bdmv) {
        // Carpeta seleccionable como BDMV root
        const selected = bdmvSelectedPath === path ? ' selected' : '';
        rows.push(`
          <div class="src-fb-row bdmv-folder${selected}" data-action="select-bdmv" data-path="${pathAttr}">
            <span class="src-fb-icon">📀</span>
            <span class="src-fb-name">${escHtml(e.name)}</span>
            <span class="src-fb-badge">BDMV</span>
          </div>
        `);
      } else {
        // Carpeta navegable normal
        rows.push(`
          <div class="src-fb-row" data-action="navigate" data-path="${pathAttr}">
            <span class="src-fb-icon">📁</span>
            <span class="src-fb-name">${escHtml(e.name)}</span>
            <span class="src-fb-meta">→</span>
          </div>
        `);
      }
    } else {
      const sizeGb = e.size_bytes > 0 ? `${(e.size_bytes / 1e9).toFixed(2)} GB` : '';
      if (filter === 'iso') {
        const selected = pickerSelectedIso === path ? ' selected' : '';
        rows.push(`
          <div class="src-fb-row${selected}" data-action="select-iso" data-path="${pathAttr}">
            <span class="src-fb-icon">💿</span>
            <span class="src-fb-name">${escHtml(e.name)}</span>
            <span class="src-fb-meta">${sizeGb}</span>
          </div>
        `);
      } else if (filter === 'm2ts') {
        const checked = m2tsSelectedPaths.includes(path) ? 'checked' : '';
        const selected = checked ? ' selected' : '';
        // En modo película → radio (selección única). En modo serie →
        // checkbox (multi-selección). El handler delegado distingue por
        // _contentType, no por el tipo de input — pero el visual lo
        // alineamos para que el usuario sepa qué puede elegir.
        const inputType = _contentType === 'movie' ? 'radio' : 'checkbox';
        rows.push(`
          <label class="src-fb-row${selected}" data-action="toggle-m2ts" data-path="${pathAttr}">
            <input type="${inputType}" name="src-fb-m2ts-sel" class="src-fb-check" ${checked}>
            <span class="src-fb-icon">🎞️</span>
            <span class="src-fb-name">${escHtml(e.name)}</span>
            <span class="src-fb-meta">${sizeGb}</span>
          </label>
        `);
      }
    }
  });

  listEl.innerHTML = rows.join('');
  _attachSrcFbDelegation(filter);
}

/** Registra (una sola vez) el listener delegado en el contenedor del
 *  browser de un filtro. Lee data-action y data-path de la fila clicada
 *  para decidir qué hacer — sin onclick inline. */
function _attachSrcFbDelegation(filter) {
  const listEl = document.getElementById(`src-fb-${filter}-list`);
  if (!listEl || listEl.dataset.delegated === '1') return;
  listEl.dataset.delegated = '1';
  listEl.addEventListener('click', (ev) => {
    const row = ev.target.closest('.src-fb-row');
    if (!row) return;
    const action = row.dataset.action;
    const path = row.dataset.path || '';
    if (action === 'navigate') {
      srcFbNavigate(filter, path);
    } else if (action === 'select-iso') {
      srcFbSelectIso(path);
    } else if (action === 'select-bdmv') {
      srcFbSelectBdmv(path);
    } else if (action === 'toggle-m2ts') {
      // El <label> ya hace toggle del checkbox/radio por defecto del
      // browser; capturamos para sincronizar nuestro estado tras el
      // cambio. Microtask para que el input tenga el nuevo `checked`.
      setTimeout(() => {
        // En modo película el input es radio (sin tipo en el selector
        // CSS general; aceptamos ambos). Si no encuentra ninguno,
        // fallback usa el path como nuevo seleccionado.
        const cb = row.querySelector('input[type=checkbox], input[type=radio]');
        if (cb) srcFbToggleM2ts(cb, path);
      }, 0);
    }
  });
}

/** Selección de ISO (single). */
function srcFbSelectIso(path) {
  pickerSelectedIso = path;
  const status = document.getElementById('src-fb-iso-status');
  if (status) status.textContent = `✅ Seleccionado: ${path}`;
  _renderSrcFb('iso');  // re-render para marcar la fila activa
  _updateAnalyzeButtonState();
}

/** Selección de carpeta BDMV (single). */
function srcFbSelectBdmv(path) {
  bdmvSelectedPath = path;
  const status = document.getElementById('src-fb-bdmv-status');
  if (status) status.textContent = `✅ Seleccionada carpeta BDMV: ${path}`;
  _renderSrcFb('bdmv');
  _updateAnalyzeButtonState();
}

/** Toggle de un M2TS en el multi-select. */
function srcFbToggleM2ts(checkbox, path) {
  if (_contentType === 'movie') {
    // Selección única en modo película — reemplaza siempre.
    m2tsSelectedPaths = checkbox.checked ? [path] : [];
  } else {
    // Multi-selección en modo serie.
    if (checkbox.checked) {
      if (!m2tsSelectedPaths.includes(path)) m2tsSelectedPaths.push(path);
    } else {
      m2tsSelectedPaths = m2tsSelectedPaths.filter(p => p !== path);
    }
  }
  _updateM2tsStatusText();
  // Refresca solo la fila para que el highlight se actualice
  _renderSrcFb('m2ts');
  _updateAnalyzeButtonState();
}

/** Reset de las selecciones M2TS. */
function srcFbM2tsClear() {
  m2tsSelectedPaths = [];
  _updateM2tsStatusText();
  _renderSrcFb('m2ts');
  _updateAnalyzeButtonState();
}

/** Refresca el texto del status del browser m2ts según selección y
 *  modo activo (película/serie). Centralizado para no duplicar las
 *  cuerdas en los handlers de toggle/clear. */
function _updateM2tsStatusText() {
  const status = document.getElementById('src-fb-m2ts-status');
  if (!status) return;
  if (m2tsSelectedPaths.length === 0) {
    status.textContent = _contentType === 'movie'
      ? 'Marca un fichero .m2ts (modo película — solo un MKV de salida)'
      : 'Marca varios ficheros .m2ts — uno por episodio (modo serie)';
    return;
  }
  if (_contentType === 'movie') {
    status.textContent = `✅ 1 fichero seleccionado → modo película`;
  } else {
    status.textContent =
      `✅ ${m2tsSelectedPaths.length} fichero${m2tsSelectedPaths.length !== 1 ? 's' : ''} → ${m2tsSelectedPaths.length} episodio${m2tsSelectedPaths.length !== 1 ? 's' : ''} (modo serie)`;
  }
}

/** Legacy stubs (algunos callers antiguos los llamaban). */
function onIsoPickerChange() { /* obsoleto — el browser nuevo usa srcFbSelectIso */ }
function onBdmvPickerChange() { /* obsoleto — usa srcFbSelectBdmv */ }
function onM2tsToggle() { /* obsoleto — usa srcFbToggleM2ts */ }


// ═══════════════════════════════════════════════════════════════════
//  Modal de progreso genérico (v2.6+) — operaciones que congelan UX
//
//  showProgressModal({title, sub, icon}) abre el overlay con spinner.
//  updateProgressModal({current, pct, addStep}) actualiza el contenido
//  durante la operación.
//  closeProgressModal() lo cierra.
//
//  Pensado para disc-probe (~10-30s) y create-series-sessions (~1-3 min)
//  donde el usuario veía el modal de origen congelado sin feedback.
// ═══════════════════════════════════════════════════════════════════

function showProgressModal({ title, sub, icon, posterUrl } = {}) {
  // Poster: si hay URL, muestra imagen; si no, fallback al emoji.
  const posterEl = document.getElementById('progress-modal-poster');
  if (posterEl) {
    if (posterUrl) {
      posterEl.innerHTML = `<img src="${escHtml(posterUrl)}" alt="poster">`;
    } else {
      posterEl.innerHTML = `<span id="progress-modal-icon">${icon || '⏳'}</span>`;
    }
  }
  document.getElementById('progress-modal-title').textContent = title || 'Procesando…';
  document.getElementById('progress-modal-sub').textContent = sub || '';
  document.getElementById('progress-modal-current').textContent = 'Iniciando…';
  const barEl = document.getElementById('progress-modal-bar');
  if (barEl) { barEl.style.width = '0%'; barEl.classList.remove('done'); }
  document.getElementById('progress-modal-pct').textContent = '';
  const stepsEl = document.getElementById('progress-modal-steps');
  if (stepsEl) {
    stepsEl.innerHTML = '';
    stepsEl.style.display = 'none';
    stepsEl.classList.remove('checklist');
  }
  const footnoteEl = document.getElementById('progress-modal-footnote');
  if (footnoteEl) { footnoteEl.textContent = ''; footnoteEl.style.display = 'none'; }
  const footerEl = document.getElementById('progress-modal-footer');
  if (footerEl) footerEl.classList.remove('done');
  openModal('progress-modal');
}

function updateProgressModal({ current, pct, addStep, checklist, footnote, done } = {}) {
  if (current !== undefined) {
    document.getElementById('progress-modal-current').textContent = current;
  }
  if (pct !== undefined && pct !== null) {
    const v = Math.max(0, Math.min(100, pct));
    const barEl = document.getElementById('progress-modal-bar');
    if (barEl) barEl.style.width = v + '%';
    document.getElementById('progress-modal-pct').textContent = v.toFixed(0) + '%';
  }
  if (addStep) {
    const el = document.getElementById('progress-modal-steps');
    if (el) {
      el.style.display = 'block';
      const div = document.createElement('div');
      div.textContent = `✓ ${addStep}`;
      el.appendChild(div);
      el.scrollTop = el.scrollHeight;
    }
  }
  // `checklist`: array de {key, label, status: 'done'|'active'|'pending', detail}.
  // Re-render completo de la lista en cada call (no append). Pensado para
  // mostrar los sub-pasos del episodio en curso durante create-series.
  if (checklist) {
    const el = document.getElementById('progress-modal-steps');
    if (el) {
      el.style.display = 'block';
      el.classList.add('checklist');
      el.innerHTML = checklist.map(item => {
        const icon = item.status === 'done' ? '✅'
          : item.status === 'active' ? '⏳'
          : '⬜';
        const cls = item.status === 'active' ? 'checklist-row active'
          : item.status === 'done' ? 'checklist-row done'
          : 'checklist-row';
        const detail = item.detail ? `<span class="checklist-detail">${escHtml(item.detail)}</span>` : '';
        return `<div class="${cls}"><span class="checklist-icon">${icon}</span><span class="checklist-label">${escHtml(item.label)}</span>${detail}</div>`;
      }).join('');
    }
  }
  // `footnote`: texto pequeño al pie (ej. resumen de episodios anteriores).
  if (footnote !== undefined) {
    let el = document.getElementById('progress-modal-footnote');
    if (!el) {
      el = document.createElement('div');
      el.id = 'progress-modal-footnote';
      el.className = 'progress-modal-footnote';
      const steps = document.getElementById('progress-modal-steps');
      if (steps && steps.parentNode) {
        steps.parentNode.insertBefore(el, steps.nextSibling);
      }
    }
    el.textContent = footnote;
    el.style.display = footnote ? 'block' : 'none';
  }
  if (done) {
    // Estado completado: barra verde + sin spinner + tick verde
    const barEl = document.getElementById('progress-modal-bar');
    if (barEl) { barEl.style.width = '100%'; barEl.classList.add('done'); }
    document.getElementById('progress-modal-pct').textContent = '100%';
    const footerEl = document.getElementById('progress-modal-footer');
    if (footerEl) footerEl.classList.add('done');
  }
}

function closeProgressModal() {
  closeModal('progress-modal');
}

/**
 * Analiza el ISO seleccionado en el picker.
 * Cierra el modal, dispara Fase A+B y abre el proyecto resultante.
 */
async function analyzeSelectedISO() {
  if (openProjects.length >= MAX_PROJECTS) {
    showToast(`Máximo ${MAX_PROJECTS} proyectos abiertos.`, 'warning');
    return;
  }

  // Construir payload según el tab activo
  let sourceType, sourcePath, sourceName, payloadProbe;
  if (_sourceTab === 'iso') {
    if (!pickerSelectedIso) return;
    sourceType = 'iso';
    sourcePath = pickerSelectedIso;
    sourceName = pickerSelectedIso.split('/').pop();
    payloadProbe = { source_type: 'iso', source_path: pickerSelectedIso };
  } else if (_sourceTab === 'bdmv_folder') {
    if (!bdmvSelectedPath) return;
    sourceType = 'bdmv_folder';
    sourcePath = bdmvSelectedPath;
    sourceName = bdmvSelectedPath.split('/').pop();
    payloadProbe = { source_type: 'bdmv_folder', source_path: bdmvSelectedPath };
  } else if (_sourceTab === 'm2ts') {
    if (!m2tsSelectedPaths.length) return;
    // Doble validación frontend: en modo película no debería ser posible
    // tener >1 m2ts (la UI usa radio), pero blindamos por si el usuario
    // ha cambiado de modo después de marcar varios.
    if (_contentType === 'movie' && m2tsSelectedPaths.length > 1) {
      showToast(
        'Modo película solo admite un fichero M2TS. Cambia a modo serie o desmarca los demás.',
        'warning',
      );
      return;
    }
    sourceType = 'm2ts';
    sourcePath = m2tsSelectedPaths[0];
    sourceName = m2tsSelectedPaths.length === 1
      ? m2tsSelectedPaths[0].split('/').pop()
      : `${m2tsSelectedPaths.length} ficheros M2TS`;
    payloadProbe = {
      source_type: 'm2ts',
      source_path: m2tsSelectedPaths[0],
      m2ts_paths: m2tsSelectedPaths,
    };
  } else {
    return;
  }

  // Hint del tipo de contenido elegido por el usuario en el toggle del
  // modal. El backend lo respeta y omite el auto-detect.
  payloadProbe.media_type_hint = _contentType;

  // Deshabilitar botón dentro del modal mientras comprobamos
  const btn = document.getElementById('new-project-analyze-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Comprobando…'; }

  // Check duplicate (compatible con los 3 tipos vía /api/check-duplicate)
  const checkPayload = sourceType === 'm2ts'
    ? { source_type: 'm2ts', source_path: m2tsSelectedPaths[0] }
    : { source_type: sourceType, source_path: sourcePath };
  const check = await apiFetch('/api/check-duplicate', {
    method: 'POST',
    body: JSON.stringify(checkPayload),
  });

  if (btn) { btn.disabled = false; btn.innerHTML = '🔍 Analizar'; }

  // El check-duplicate ahora devuelve `sessions[]` (todas las que
  // comparten fingerprint). Para BDMV/ISO de serie con N episodios
  // procesados, eso son N — no queremos mostrar un diálogo "Ya
  // existe un proyecto" como si solo hubiera uno. Filtramos según
  // modo elegido por el usuario.
  //
  // La detección de "es una sesión de serie" usa la presencia de
  // season_number + episode_number (no el campo media_type) porque
  // sesiones legacy pueden tener media_type cargado como 'movie' por
  // el default de Pydantic aunque hayan sido creadas como serie. Los
  // campos season/episode son el signal real.
  const existingSessions = (check?.sessions || []).filter(Boolean);
  const existingSeriesSessions = existingSessions.filter(
    s => s.season_number && s.episode_number,
  );

  if (_contentType === 'series' && existingSeriesSessions.length > 0) {
    // Modo serie con episodios previos: NO mostramos diálogo de duplicado.
    // Pasamos directamente al disc-probe + series-modal, llevando el set
    // de existentes para que cada candidato muestre badge "✓ Existe"
    // (desmarcado por defecto — el usuario marca solo lo que quiere
    // añadir o rehacer).
    closeModal('new-project-modal');
    await _probeAndRouteSource(
      sourceType, sourcePath, sourceName, payloadProbe,
      { existingSeriesSessions },
    );
    return;
  }

  if (check?.duplicate && check.session) {
    closeModal('new-project-modal');
    const existingName = check.session.mkv_name || sourceName;
    showConfirm(
      'Ya existe un proyecto para este origen',
      `Hay un proyecto previo asociado a este contenido: "${existingName}". Puedes abrirlo tal cual está o reanalizar el origen (se perderán las ediciones actuales).`,
      // Reanalizar pasa por el flujo COMPLETO de probe + routing — sin
      // esto, un disco de serie reabierto en modo película saltaba
      // directo a _doAnalyzeSource sin pasar por la detección de
      // multi-episodios y fallaba en mitad del análisis.
      () => _probeAndRouteSource(sourceType, sourcePath, sourceName, payloadProbe),
      'Reanalizar',
    );
    const openBtn = document.createElement('button');
    openBtn.className = 'btn btn-primary btn-sm confirm-extra-btn';
    openBtn.textContent = '📂 Abrir existente';
    openBtn.onclick = () => {
      closeModal('confirm-modal');
      openProject(check.session);
    };
    const confirmOk = document.getElementById('confirm-ok-btn');
    if (confirmOk) confirmOk.parentNode.insertBefore(openBtn, confirmOk);
    return;
  }

  closeModal('new-project-modal');
  await _probeAndRouteSource(sourceType, sourcePath, sourceName, payloadProbe);
}

/** Ejecuta /api/disc-probe y enruta el resultado a la siguiente pantalla:
 *
 *  - series / ambiguous → abre el series-modal (selector de episodios).
 *  - movie con movie_warning → showConfirm antes de proceder
 *    (caso BDMV/ISO de serie reabierto en modo película).
 *  - movie sin warning → _doAnalyzeSource directo.
 *
 *  Se llama desde dos puntos: el flujo normal (sin duplicado) y el handler
 *  de "Reanalizar" tras el aviso de duplicado. Antes el handler de
 *  Reanalizar saltaba directo a _doAnalyzeSource sin disc-probe, por lo
 *  que el movie_warning nunca se mostraba al reabrir un origen previo. */
async function _probeAndRouteSource(sourceType, sourcePath, sourceName, payloadProbe, opts = {}) {
  // Modal de progreso — disc-probe puede tardar 10-30s (montaje del
  // ISO + búsqueda de episodios candidatos en discos grandes).
  const probeIcon = sourceType === 'iso' ? '💿' : sourceType === 'bdmv_folder' ? '📁' : '🎞️';
  const probeSub = sourceType === 'iso'
    ? 'Montando ISO y buscando episodios candidatos en el disco'
    : sourceType === 'bdmv_folder'
    ? 'Buscando episodios candidatos en la carpeta BDMV'
    : `Analizando ${m2tsSelectedPaths.length} fichero${m2tsSelectedPaths.length !== 1 ? 's' : ''} M2TS`;
  showProgressModal({
    title: `Detectando contenido — ${sourceName}`,
    sub: probeSub,
    icon: probeIcon,
  });
  // Estado inicial — el polling de /api/disc-probe/progress lo sustituye
  // en cuanto el backend empieza a reportar el paso real (montaje, scan
  // por candidato, clasificación). Sin polling el modal se quedaba con
  // "Conectando con el servidor…" durante 10-30s sin barra avanzando.
  updateProgressModal({ current: '⏳ Iniciando…', pct: 0 });

  // Polling del progreso real. Pollea cada 400ms hasta que el POST
  // termine. Si el backend reporta `running:false` lo respetamos (el
  // POST suele acabar a la vez o un tick antes que el polling lo vea).
  const pollId = setInterval(async () => {
    try {
      const prog = await apiFetch('/api/disc-probe/progress', { silent: true });
      if (prog && prog.current_label) {
        updateProgressModal({ current: prog.current_label, pct: prog.pct || 0 });
      }
    } catch (_) { /* silenciar errores de polling */ }
  }, 400);

  const probe = await apiFetch('/api/disc-probe', {
    method: 'POST',
    body: JSON.stringify(payloadProbe),
  });

  clearInterval(pollId);
  closeProgressModal();

  if (!probe) {
    showToast('No se pudo inspeccionar el origen. Revisa el log del servidor.', 'error');
    return;
  }

  if (probe.media_type === 'series' || probe.media_type === 'ambiguous') {
    // Series modal usa los m2ts_paths para identificar el origen al crear
    // sesiones — los persistimos en el probe object.
    if (sourceType === 'm2ts') {
      probe.m2ts_paths = m2tsSelectedPaths;
    }
    probe.source_type = sourceType;
    probe.source_path = sourcePath;
    // Sesiones de serie ya existentes para este origen (mismo fingerprint).
    // El series-modal las usa para marcar candidatos con badge "✓ Existe"
    // y desmarcarlos por defecto (evita reprocesar sin querer).
    probe.existing_series_sessions = opts.existingSeriesSessions || [];
    openSeriesModal(probe);
    return;
  }

  // Movie path. Si el backend devolvió movie_warning (origen tiene
  // pinta de serie pero el usuario eligió película), confirmamos
  // antes de proceder — la operación es larga y el resultado puede
  // no ser el esperado si era una serie disfrazada.
  if (probe.movie_warning) {
    showConfirm(
      '⚠️ Origen con varios episodios',
      probe.movie_warning,
      () => _doAnalyzeSource(sourceType, sourcePath, sourceName, payloadProbe),
      'Sí, procesar como película',
    );
    return;
  }
  await _doAnalyzeSource(sourceType, sourcePath, sourceName, payloadProbe);
}

/** Wrapper de _doAnalyzeISO que envía el source_type al endpoint /api/analyze.
 *  Reemplaza al wrapper antiguo solo-ISO. Mantiene la signature legacy
 *  como alias para que cualquier caller antiguo siga funcionando. */
async function _doAnalyzeSource(sourceType, sourcePath, sourceName, _payloadProbe) {
  // ── Modal de progreso ──────────────────────────────────────────
  // Configura título, icono y label del primer paso según el tipo de
  // fuente — sin esto el modal hablaba siempre de "Montando ISO" y
  // "MPLS" aunque el origen fuera una carpeta BDMV o un m2ts directo.
  _configureAnalyzeModalForSource(sourceType);
  const isoEl = document.getElementById('analyze-modal-iso');
  if (isoEl) isoEl.textContent = sourceName;
  _resetAnalyzeSteps();
  openModal('analyze-modal');

  // Hidratación TMDb en paralelo — sin bloquear el análisis. Si hay
  // match, sustituye el icono 💿 por la cartela y el título genérico
  // por el nombre real de la película. Best-effort; si falla, el
  // modal sigue con el aspecto sin cartela.
  _hydrateAnalyzeModalTmdb(sourceName, sourceType);

  // Para m2ts el paso "mount" no aplica — se salta visualmente
  // marcándolo como ✅ antes de empezar (mount es no-op para m2ts).
  if (sourceType === 'm2ts' || sourceType === 'bdmv_folder') {
    _advanceAnalyzeStep('mount', 'identify');
  }

  const steps = ['mount', 'identify', 'chapters', 'mediainfo', 'pgs', 'dovi', 'rules'];
  let lastStep = 'mount';
  let stepStartTs = Date.now();
  const pollId = setInterval(async () => {
    try {
      const prog = await apiFetch('/api/analyze/progress');
      if (prog?.step && prog.step !== lastStep && steps.includes(prog.step)) {
        const prevIdx = steps.indexOf(lastStep);
        const newIdx = steps.indexOf(prog.step);
        if (newIdx > prevIdx) {
          for (let i = prevIdx; i < newIdx; i++) {
            _advanceAnalyzeStep(steps[i], steps[i + 1]);
          }
          lastStep = prog.step;
          stepStartTs = Date.now();
        }
      }
      if (lastStep === 'pgs') {
        const labelEl = document.getElementById('analyze-step-pgs-label');
        const barWrap = document.getElementById('analyze-step-pgs-bar');
        const barFill = document.getElementById('analyze-step-pgs-bar-fill');
        const statsEl = document.getElementById('analyze-step-pgs-stats');
        const elapsed = Math.floor((Date.now() - stepStartTs) / 1000);
        const mm = Math.floor(elapsed / 60);
        const ss = (elapsed % 60).toString().padStart(2, '0');
        const pct = prog?.pct;
        const eta = prog?.eta_s;
        if (labelEl) labelEl.textContent = '⏳ Analizando subtítulos del origen…';
        if (barWrap) barWrap.style.display = 'block';
        if (statsEl) statsEl.style.display = 'block';
        if (pct != null && barFill) {
          barFill.style.width = pct + '%';
        }
        if (statsEl) {
          let line = `${mm}:${ss} transcurridos`;
          if (pct != null) line += ` · ${pct.toFixed(1)}% leído`;
          if (eta && eta > 0) {
            const em = Math.floor(eta / 60);
            const es = (eta % 60).toString().padStart(2, '0');
            line += ` · ETA ${em}:${es}`;
          }
          statsEl.textContent = line;
        }
      }
    } catch (_) { /* silenciar errores de polling */ }
  }, 500);

  const payload = {
    source_type: sourceType,
    source_path: sourcePath,
  };
  // Compat con endpoint antiguo: si es iso, mandar también iso_path
  if (sourceType === 'iso') payload.iso_path = sourcePath;

  const session = await apiFetch('/api/analyze', {
    method: 'POST',
    body: JSON.stringify(payload),
  }, 900000);

  clearInterval(pollId);
  steps.forEach((s, i) => {
    if (i < steps.length - 1) _advanceAnalyzeStep(s, steps[i + 1]);
  });
  await new Promise(r => setTimeout(r, 400));
  closeModal('analyze-modal');

  if (!session) {
    showToast(`No se pudo analizar ${escHtml(sourceName)}. Verifica que el origen sigue disponible y es válido.`, 'error');
    return;
  }

  // Si ya existía la sesión, re-renderizar pestaña; si no, abrir.
  const existingProject = openProjects.find(p => p.subTab && p.session.id === session.id);
  if (existingProject) {
    existingProject.session = session;
    showToast(`Proyecto re-analizado: ${session.mkv_name || sourceName}`, 'success');
  } else {
    showToast(`Proyecto creado: ${session.mkv_name || sourceName}`, 'success');
    openProject(session);
  }

  await loadSessions();
}

/**
 * Ejecuta el análisis de un ISO (Fase A+B). Extraída para reutilizar
 * tanto en creación nueva como en re-análisis de un proyecto existente.
 */
async function _doAnalyzeISO(isoPath, isoName) {
  // ── Modal de progreso ──────────────────────────────────────────
  const isoEl = document.getElementById('analyze-modal-iso');
  if (isoEl) isoEl.textContent = isoName;
  _resetAnalyzeSteps();
  openModal('analyze-modal');

  // Polling de progreso real del backend
  const steps = ['mount', 'identify', 'chapters', 'mediainfo', 'pgs', 'dovi', 'rules'];
  let lastStep = 'mount';
  let stepStartTs = Date.now();
  const pollId = setInterval(async () => {
    try {
      const prog = await apiFetch('/api/analyze/progress');
      if (prog?.step && prog.step !== lastStep && steps.includes(prog.step)) {
        const prevIdx = steps.indexOf(lastStep);
        const newIdx = steps.indexOf(prog.step);
        // Solo avanzar — ignorar backward transitions (pueden ocurrir si un
        // log tardio contiene una keyword que matchea un step anterior).
        if (newIdx > prevIdx) {
          for (let i = prevIdx; i < newIdx; i++) {
            _advanceAnalyzeStep(steps[i], steps[i + 1]);
          }
          lastStep = prog.step;
          stepStartTs = Date.now();
        }
      }
      // Paso PGS: barra de progreso real basada en bytes leídos por ffprobe
      if (lastStep === 'pgs') {
        const labelEl = document.getElementById('analyze-step-pgs-label');
        const barWrap = document.getElementById('analyze-step-pgs-bar');
        const barFill = document.getElementById('analyze-step-pgs-bar-fill');
        const statsEl = document.getElementById('analyze-step-pgs-stats');
        const elapsed = Math.floor((Date.now() - stepStartTs) / 1000);
        const mm = Math.floor(elapsed / 60);
        const ss = (elapsed % 60).toString().padStart(2, '0');
        const pct = prog?.pct;
        const eta = prog?.eta_s;
        if (labelEl) labelEl.textContent = '⏳ Analizando subtítulos del origen…';
        if (barWrap) barWrap.style.display = 'block';
        if (statsEl) statsEl.style.display = 'block';
        if (pct != null && barFill) {
          barFill.style.width = pct + '%';
        }
        if (statsEl) {
          let line = `${mm}:${ss} transcurridos`;
          if (pct != null) line += ` · ${pct.toFixed(1)}% leído`;
          if (eta && eta > 0) {
            const em = Math.floor(eta / 60);
            const es = (eta % 60).toString().padStart(2, '0');
            line += ` · ETA ${em}:${es}`;
          }
          statsEl.textContent = line;
        }
      }
    } catch (_) { /* silenciar errores de polling */ }
  }, 500);

  // 15 min timeout: el paso de packet count puede tardar hasta 3 min en m2ts
  // de 60GB, más el resto del análisis (ffmpeg DV, MediaInfo, mkvmerge).
  const session = await apiFetch('/api/analyze', {
    method: 'POST',
    body: JSON.stringify({ iso_path: isoPath }),
  }, 900000);

  clearInterval(pollId);
  // Marcar todos los pasos restantes como completados
  steps.forEach((s, i) => {
    if (i < steps.length - 1) _advanceAnalyzeStep(s, steps[i + 1]);
  });
  // Pequeña pausa para que se vea el último ✅ antes de cerrar
  await new Promise(r => setTimeout(r, 400));
  closeModal('analyze-modal');

  if (!session) {
    showToast(`No se pudo analizar ${escHtml(isoName)}. Verifica que el ISO es válido y que el contenedor Docker arranca en modo privilegiado.`, 'error');
    return;
  }

  // Si el proyecto ya estaba abierto, actualizar su sesión
  const existingProject = openProjects.find(p => p.sessionId === session.id);
  if (existingProject) {
    existingProject.session = session;
    currentSession = session;
    _chaptersModified.delete(existingProject.subTabId);
    renderChapters(session.chapters, session.chapters_auto_generated, session.chapters_auto_reason);
    showToast(`Proyecto re-analizado: ${session.mkv_name || isoName}`, 'success');
  } else {
    showToast(`Proyecto creado: ${session.mkv_name || isoName}`, 'success');
    openProject(session);
  }

  await loadSessions();
}


// ══════════════════════════════════════════════════════════════════════
//  MODO SERIE — selección de episodios y creación de N sesiones (v2.5+)
//
//  Flujo:
//    1. /api/disc-probe ya devolvió media_type='series'|'ambiguous'.
//    2. openSeriesModal() guarda el probe en _seriesState y prellena
//       la búsqueda TMDb con suggested_title.
//    3. Usuario busca → seriesTmdbSearch() → /api/tv-search.
//    4. Usuario elige candidata → seriesSelectCandidate() → /api/tv-details.
//    5. Usuario elige temporada → seriesLoadSeason() → /api/tv-season
//       (con mpls_durations para que el backend dé el match auto).
//    6. Renderiza tabla MPLS↔episodio editable con confianza 🟢🟡.
//    7. Usuario marca checkboxes + edita mapping si quiere.
//    8. seriesCreateSessions() → /api/create-series-sessions → N sesiones.
// ══════════════════════════════════════════════════════════════════════

// Estado del modal de series. Se vacía al cerrar.
let _seriesState = null;

/** Busca la sesión existente que corresponde a un candidato (MPLS/m2ts).
 *  Prioriza el lookup por mpls_basename (identifica físicamente el
 *  fichero del disco); fallback por (season, episode_number) si el match
 *  por path falla (sesión muy antigua sin mpls_path persistido).
 *
 *  Devuelve la Session o null si no hay match. */
function _findExistingForCandidate(candidate, season, episodeNumber) {
  if (!_seriesState) return null;
  // 1) Lookup primario: por basename del MPLS/m2ts. Robusto frente a
  //    cambios de numeración de episodio entre runs (smart-match TMDb).
  const byMpls = _seriesState.existingByMpls;
  if (byMpls && candidate && candidate.mpls_name) {
    const hit = byMpls.get(candidate.mpls_name) || byMpls.get(candidate.mpls_path);
    if (hit) return hit;
  }
  // 2) Fallback: por (season, episode_number).
  const byEp = _seriesState.existingByEp;
  if (byEp && season && episodeNumber) {
    return byEp.get(`${season}.${episodeNumber}`) || null;
  }
  return null;
}

function openSeriesModal(probe) {
  // Sesiones existentes para este origen (mismo fingerprint). Dos índices
  // para lookup O(1) en el render del table:
  //
  //   - existingByMpls (PRIMARIO): key = basename del MPLS/M2TS persistido.
  //     Es el identificador físico de qué fichero del disco representa la
  //     sesión. Independiente de qué número de episodio le hayamos asignado.
  //   - existingByEp (SECUNDARIO): key = "season.episode". Útil si en modo
  //     manual el usuario edita el episode_number — pero el primario gana
  //     siempre que coincide.
  //
  // Antes solo usábamos existingByEp y fallaba cuando el smart-match de
  // TMDb asignaba números distintos a los que tenían las sesiones
  // existentes (caso del usuario: badges no aparecían y los checkboxes
  // quedaban invertidos).
  const existing = probe.existing_series_sessions || [];
  const existingByMpls = new Map();
  const existingByEp = new Map();
  for (const s of existing) {
    if (s.mpls_path) {
      // Para iso/bdmv: basename del MPLS (ej. "00800.mpls").
      // Para m2ts: el path absoluto entero o el basename. Cubrimos ambos.
      const basename = s.mpls_path.split('/').pop();
      existingByMpls.set(basename, s);
      // También por path completo (por si se compara con absoluto)
      if (basename !== s.mpls_path) existingByMpls.set(s.mpls_path, s);
    }
    if (s.season_number && s.episode_number) {
      existingByEp.set(`${s.season_number}.${s.episode_number}`, s);
    }
  }

  _seriesState = {
    probe,                       // {iso_path, episode_candidates[], suggested_*}
    mode: 'tmdb',                // 'tmdb' | 'manual' — afecta a render + payload
    selectedSeries: null,        // {tmdb_id, name, year, ...}; en modo manual lo seteamos a mano
    selectedSeason: null,        // {season_number, episode_count, ...}
    seasonEpisodes: [],          // [TvEpisode] — vacío en modo manual
    mplsMatches: [],             // matches runtime; vacío en modo manual
    candidates: {},              // cache de candidatos TMDb por id
    mapping: {},                 // {mpls_path: {include, episode_number, episode_title, runtime_minutes}}
    existingByMpls,              // Map<mpls_basename, session> — primario
    existingByEp,                // Map<"season.ep", session> — secundario
  };

  // Título + subtítulo source-aware. Para ISO/BDMV los candidatos son
  // playlists (MPLS); para M2TS son los ficheros sueltos que eligió el
  // usuario. El término "episodio candidato" funciona para los 3 casos
  // sin exponer jerga de Blu-ray al usuario que viene de Plex/Jellyfin.
  const stype = probe.source_type || 'iso';
  const titleEl = document.getElementById('series-modal-title');
  if (titleEl) {
    titleEl.textContent = stype === 'iso' ? '📺 Disco de serie detectado'
      : stype === 'bdmv_folder' ? '📺 Carpeta BDMV de serie detectada'
      : '📺 Episodios de serie detectados';
  }
  const sub = document.getElementById('series-modal-sub');
  if (sub) {
    const n = probe.episode_candidates.length;
    // Conteo de existentes para la nota informativa. Usamos el mapa
    // primario (por mpls); fallback al secundario por si solo hay
    // datos season/episode (sesión muy antigua sin mpls_path).
    const existingCount = existingByMpls.size > 0 ? existingByMpls.size : existingByEp.size;
    const sourceLabel = stype === 'iso' ? 'el disco'
      : stype === 'bdmv_folder' ? 'la carpeta BDMV'
      : 'los ficheros M2TS';
    const verdict = probe.media_type === 'series'
      ? `Detectados <strong>${n} episodios candidatos</strong> en ${sourceLabel} con duración similar.`
      : `Detectados <strong>${n} candidatos</strong> en ${sourceLabel} con duración compatible (clasificación ambigua — confirma manualmente).`;
    // Aviso adicional cuando ya hay episodios procesados de este origen
    // — el usuario sabe por qué algunas filas vienen desmarcadas.
    const existingNote = existingCount > 0
      ? ` <strong>${existingCount} episodio${existingCount === 1 ? '' : 's'} ya procesado${existingCount === 1 ? '' : 's'}</strong> aparece${existingCount === 1 ? '' : 'n'} desmarcado${existingCount === 1 ? '' : 's'} con badge <span class="series-badge-exists">✓ Existe</span> — marca solo los que quieras añadir o rehacer.`
      : '';
    sub.innerHTML = `${verdict} Identifica la serie (TMDb o manual) y asigna cada candidato a su número de episodio.${existingNote}`;
  }

  // Prellenar inputs con título/año sugerido
  const q = document.getElementById('series-tmdb-query');
  const y = document.getElementById('series-tmdb-year');
  if (q) q.value = probe.suggested_title || '';
  if (y) y.value = probe.suggested_year || '';
  const mn = document.getElementById('series-manual-name');
  const my = document.getElementById('series-manual-year');
  if (mn) mn.value = probe.suggested_title || '';
  if (my) my.value = probe.suggested_year || '';
  const ms = document.getElementById('series-manual-season');
  if (ms) ms.value = 1;

  // Reset secciones inferiores + stepper + modo TMDb por defecto
  document.getElementById('series-tmdb-results').innerHTML = '';
  document.getElementById('series-season-section').style.display = 'none';
  document.getElementById('series-episodes-section').style.display = 'none';
  seriesSetMode('tmdb');
  _seriesUpdateStepper(1);
  _seriesUpdateCreateButton();

  openModal('series-modal');

  // Lanza búsqueda inicial si tenemos query
  if (probe.suggested_title) {
    seriesTmdbSearch();
  }
}

/** Cambia entre los dos modos de identificación: TMDb (con búsqueda y
 *  match runtime) o manual (entrada libre — útil cuando TMDb no la
 *  encuentra o el usuario prefiere nombres propios). */
function seriesSetMode(mode) {
  if (!_seriesState) return;
  _seriesState.mode = mode;
  // Reset de selección al cambiar de modo
  _seriesState.selectedSeries = null;
  _seriesState.selectedSeason = null;
  _seriesState.seasonEpisodes = [];
  _seriesState.mplsMatches = [];
  _seriesState.mapping = {};
  document.getElementById('series-mode-btn-tmdb').classList.toggle('active', mode === 'tmdb');
  document.getElementById('series-mode-btn-manual').classList.toggle('active', mode === 'manual');
  document.getElementById('series-mode-tmdb-panel').style.display = mode === 'tmdb' ? 'block' : 'none';
  document.getElementById('series-mode-manual-panel').style.display = mode === 'manual' ? 'block' : 'none';
  // Oculta secciones siguientes — el usuario tiene que re-confirmar
  document.getElementById('series-season-section').style.display = 'none';
  document.getElementById('series-episodes-section').style.display = 'none';
  _seriesUpdateStepper(1);
  _seriesUpdateCreateButton();
}

/** Confirma los datos manuales y salta directo al paso 3 (mapeo).
 *  En modo manual no hay step 2 (temporada) — la pedimos arriba. */
function seriesConfirmManual() {
  if (!_seriesState) return;
  const name = (document.getElementById('series-manual-name').value || '').trim();
  const yearStr = (document.getElementById('series-manual-year').value || '').trim();
  const seasonStr = (document.getElementById('series-manual-season').value || '').trim();
  if (!name) {
    showToast('Introduce el nombre de la serie', 'warning');
    return;
  }
  const season = parseInt(seasonStr, 10);
  if (isNaN(season) || season < 1) {
    showToast('La temporada debe ser un número >= 1', 'warning');
    return;
  }
  _seriesState.selectedSeries = {
    tmdb_id: null,
    name,
    year: yearStr ? parseInt(yearStr, 10) : null,
    manual: true,
  };
  _seriesState.selectedSeason = { season_number: season };
  _seriesState.seasonEpisodes = [];  // sin TMDb → sin episodios pre-cargados
  _seriesState.mplsMatches = [];

  // Mapping inicial: asignación secuencial 1-based; sin título de episodio
  // (el usuario puede teclear uno por fila si quiere). Si ya existe una
  // sesión para este MPLS o (season, episode_number), por defecto la
  // fila queda DESMARCADA — el usuario marca solo lo que quiera añadir
  // o rehacer.
  const mapping = {};
  _seriesState.probe.episode_candidates.forEach((mpls, idx) => {
    const epNum = idx + 1;
    const existing = _findExistingForCandidate(mpls, season, epNum);
    mapping[mpls.mpls_path] = {
      include: !existing,
      episode_number: epNum,
      episode_title: '',
      runtime_minutes: 0,
    };
  });
  _seriesState.mapping = mapping;

  // Salta direto a paso 3 (sin step 2)
  _seriesUpdateStepper(3);
  document.getElementById('series-season-section').style.display = 'none';
  document.getElementById('series-episodes-section').style.display = 'block';
  // Actualizar ayuda contextual
  const help = document.getElementById('series-episodes-help');
  if (help) {
    // Modo manual: usamos innerHTML para que el <strong> de la advertencia
    // de validación se renderice como en el modo TMDb (mismo patrón
    // visual entre ambos paneles).
    help.innerHTML = 'Modo manual: la asignación inicial es secuencial (E01, E02…). Edita el nº de episodio y, opcionalmente, su título en cada fila. <strong>Valida manualmente</strong> que cada MPLS corresponda al episodio correcto antes de crear.';
  }
  _renderSeriesEpisodesTable();
  _seriesUpdateCreateButton();
}

async function seriesTmdbSearch() {
  const query = (document.getElementById('series-tmdb-query').value || '').trim();
  const yearStr = (document.getElementById('series-tmdb-year').value || '').trim();
  const year = yearStr ? parseInt(yearStr, 10) : null;
  if (!query) {
    showToast('Introduce el nombre de la serie', 'warning');
    return;
  }
  const resultsBox = document.getElementById('series-tmdb-results');
  resultsBox.innerHTML = '<div style="font-size:12px; color:var(--text-3); padding:8px">⏳ Buscando en TMDb…</div>';

  const qs = new URLSearchParams({ query });
  if (year && !isNaN(year)) qs.set('year', String(year));
  const data = await apiFetch(`/api/tv-search?${qs.toString()}`);

  if (!data || !data.tmdb_configured) {
    resultsBox.innerHTML = '<div style="font-size:12px; color:var(--orange); padding:8px">⚠️ TMDb no configurado. Configura la API key en ⚙️ Ajustes para buscar series.</div>';
    return;
  }
  if (!data.results || data.results.length === 0) {
    resultsBox.innerHTML = '<div style="font-size:12px; color:var(--text-3); padding:8px">— Sin resultados. Prueba con otro título o año. —</div>';
    return;
  }

  // Cacheamos los candidatos por tmdb_id para no tener que serializar todo
  // el objeto en el onclick (evita problemas de escape).
  _seriesState.candidates = {};
  resultsBox.innerHTML = data.results.map(r => {
    _seriesState.candidates[r.tmdb_id] = r;
    const isSelected = _seriesState.selectedSeries && _seriesState.selectedSeries.tmdb_id === r.tmdb_id;
    const yr = r.year ? `<span class="yr">(${r.year})</span>` : '';
    const meta = [
      r.original_name && r.original_name !== r.name ? escHtml(r.original_name) : '',
      r.vote_average ? `★ ${r.vote_average.toFixed(1)}` : '',
    ].filter(Boolean).join(' · ');
    const poster = r.poster_url
      ? `<img src="${escHtml(r.poster_url)}" alt="${escHtml(r.name)}" loading="lazy">`
      : '📺';
    return `
      <div class="series-candidate${isSelected ? ' selected' : ''}"
           onclick="seriesSelectCandidate(${r.tmdb_id})">
        <div class="series-candidate-poster">${poster}</div>
        <div class="series-candidate-info">
          <div class="series-candidate-title">${escHtml(r.name)} ${yr}</div>
          ${meta ? `<div class="series-candidate-meta">${meta}</div>` : ''}
          ${r.overview ? `<div class="series-candidate-overview">${escHtml(r.overview)}</div>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

async function seriesSelectCandidate(tmdbId) {
  // Recupera el candidato cacheado por tmdb_id (no recibimos el objeto
  // entero por onclick — más limpio y sin issues de escape de quotes).
  const candidate = _seriesState.candidates && _seriesState.candidates[tmdbId];
  if (!candidate) return;
  _seriesState.selectedSeries = candidate;

  // Update stepper visual: 1=done, 2=active
  _seriesUpdateStepper(2);

  // Re-render para marcar la card seleccionada (refresh con CSS .selected)
  const resultsBox = document.getElementById('series-tmdb-results');
  resultsBox.querySelectorAll('.series-candidate').forEach(el => {
    el.classList.toggle('selected',
      el.getAttribute('onclick') === `seriesSelectCandidate(${tmdbId})`);
  });

  // Cargar detalles de la serie para poblar combo de temporadas
  const data = await apiFetch(`/api/tv-details/${tmdbId}`);
  if (!data || !data.details) {
    showToast('No se pudieron cargar los detalles de la serie', 'error');
    return;
  }
  const seasons = data.details.seasons || [];
  const select = document.getElementById('series-season-select');
  select.innerHTML = '<option value="">— Elige temporada —</option>' + seasons.map(s =>
    `<option value="${s.season_number}">${escHtml(s.name)} (${s.episode_count} episodios)</option>`
  ).join('');
  document.getElementById('series-season-section').style.display = 'block';
  document.getElementById('series-episodes-section').style.display = 'none';
  _seriesUpdateCreateButton();
}

/** Update visual stepper. activeStep: 1, 2 o 3. Pasos anteriores quedan
 *  marcados como done. */
function _seriesUpdateStepper(activeStep) {
  document.querySelectorAll('#series-stepper .series-step').forEach(el => {
    const n = parseInt(el.getAttribute('data-step'), 10);
    el.classList.remove('active', 'done');
    if (n < activeStep) el.classList.add('done');
    else if (n === activeStep) el.classList.add('active');
  });
}

async function seriesLoadSeason() {
  const select = document.getElementById('series-season-select');
  const seasonNumber = parseInt(select.value, 10);
  if (isNaN(seasonNumber)) {
    document.getElementById('series-episodes-section').style.display = 'none';
    _seriesState.selectedSeason = null;
    _seriesUpdateCreateButton();
    return;
  }
  _seriesState.selectedSeason = { season_number: seasonNumber };

  const tmdbId = _seriesState.selectedSeries.tmdb_id;
  const durations = _seriesState.probe.episode_candidates
    .map(c => c.duration_minutes)
    .join(',');
  const qs = new URLSearchParams({ mpls_durations: durations });
  const data = await apiFetch(`/api/tv-season/${tmdbId}/${seasonNumber}?${qs.toString()}`);
  if (!data) return;
  _seriesState.seasonEpisodes = data.episodes || [];
  _seriesState.mplsMatches = data.mpls_matches || [];

  // Construir mapping inicial: pre-rellenar con suggested_episode_number
  // y marcar todos como include=true por defecto (el usuario desmarca lo
  // que no quiera). Capturamos también overview + still_url para que la
  // cabecera de la pestaña del proyecto muestre la info concreta del
  // episodio (no la genérica de la serie) tras la creación.
  const mapping = {};
  _seriesState.probe.episode_candidates.forEach((mpls, idx) => {
    const match = _seriesState.mplsMatches[idx] || {};
    const matched = match.matched_episode || {};
    const epNum = match.suggested_episode_number || (idx + 1);
    // Episodios ya procesados de este origen → desmarcados por defecto.
    // Sin esto, al reabrir un disco con N episodios el usuario tendría
    // que desmarcar uno a uno (o reprocesar todos sin querer). El helper
    // mira primero por mpls_basename (identificador físico) y solo
    // cae a (season, ep_number) si lo primero falla.
    const existing = _findExistingForCandidate(mpls, seasonNumber, epNum);
    mapping[mpls.mpls_path] = {
      include: !existing,
      episode_number: epNum,
      episode_title: matched.name || '',
      runtime_minutes: matched.runtime_minutes || 0,
      episode_overview: matched.overview || '',
      episode_still_url: matched.still_url || '',
    };
  });
  _seriesState.mapping = mapping;

  _renderSeriesEpisodesTable();
  document.getElementById('series-episodes-section').style.display = 'block';
  _seriesUpdateCreateButton();
}

/** Calcula el indicador de confianza para una fila MPLS↔episodio
 *  basándose en el episodio ACTUAL del mapping (no el sugerido por el
 *  backend). Se recomputa en cada render para que el indicador refleje
 *  la elección actual del usuario.
 *
 *  Devuelve {emoji, title} para pintar en la celda y su tooltip:
 *    🟢 high  · |Δ runtime| ≤ 1 min  (match perfecto)
 *    🟡 low   · |Δ runtime| > 1 min  (runtime no coincide)
 *    ⚪ unknown · sin episodio o TMDb sin runtime
 *    ✏️ manual · modo manual (sin TMDb)
 */
function _computeMatchConfidence(mplsDurationMin, episodeNumber, isManual) {
  if (isManual) {
    return { emoji: '✏️', title: 'Modo manual · sin match runtime' };
  }
  if (!episodeNumber) {
    return {
      emoji: '⚪',
      title: 'Sin episodio asignado — elige uno del desplegable',
    };
  }
  const ep = (_seriesState.seasonEpisodes || []).find(e => e.episode_number === episodeNumber);
  if (!ep) {
    return {
      emoji: '⚪',
      title: `Episodio E${String(episodeNumber).padStart(2,'0')} no está en la lista de TMDb`,
    };
  }
  if (!ep.runtime_minutes) {
    return {
      emoji: '🟡',
      title: `MPLS ${mplsDurationMin.toFixed(1)} min · TMDb sin runtime para E${String(episodeNumber).padStart(2,'0')}`,
    };
  }
  const delta = Math.abs(mplsDurationMin - ep.runtime_minutes);
  const epLabel = `E${String(episodeNumber).padStart(2,'0')} ${ep.runtime_minutes} min`;
  if (delta <= 1) {
    return {
      emoji: '🟢',
      title: `Match alto · MPLS ${mplsDurationMin.toFixed(1)} min · ${epLabel} (Δ=${delta.toFixed(1)} min)`,
    };
  }
  return {
    emoji: '🟡',
    title: `Match bajo · MPLS ${mplsDurationMin.toFixed(1)} min · ${epLabel} (Δ=${delta.toFixed(1)} min)`,
  };
}

function _renderSeriesEpisodesTable() {
  _seriesUpdateStepper(3);
  const cands = _seriesState.probe.episode_candidates;
  const mapping = _seriesState.mapping;
  const episodes = _seriesState.seasonEpisodes;
  const isManual = _seriesState.mode === 'manual';

  // En modo TMDb construimos un <select> con los episodios de la
  // temporada. En modo manual usamos <input type="number"> para el
  // episode_number + <input type="text"> para el título opcional.
  const buildSelect = (selectedNum, mplsPath) => {
    const opts = [`<option value="">—</option>`].concat(episodes.map(e => {
      const isSel = e.episode_number === selectedNum ? ' selected' : '';
      const label = `E${String(e.episode_number).padStart(2, '0')} · ${escHtml(e.name)}${e.runtime_minutes ? ` (${e.runtime_minutes}m)` : ''}`;
      return `<option value="${e.episode_number}"${isSel}>${label}</option>`;
    }));
    return `<select onchange="seriesChangeEpisode('${escHtml(mplsPath)}', this.value)">${opts.join('')}</select>`;
  };

  const buildManualInputs = (epNum, epTitle, mplsPath) => `
    <div class="series-manual-ep-inputs">
      <input type="number" class="series-manual-ep-num" min="1" value="${epNum || ''}"
             placeholder="Nº"
             onchange="seriesChangeEpisode('${escHtml(mplsPath)}', this.value)">
      <input type="text" class="series-manual-ep-title" value="${escHtml(epTitle || '')}"
             placeholder="Título del episodio (opcional)"
             onchange="seriesChangeEpisodeTitle('${escHtml(mplsPath)}', this.value)">
    </div>
  `;

  const season = _seriesState.selectedSeason ? _seriesState.selectedSeason.season_number : null;

  const rows = cands.map((c, idx) => {
    const map = mapping[c.mpls_path] || {};
    // Confianza dinámica basada en el episodio actualmente seleccionado
    // (no en el match inicial del backend). Sin esto, cuando el usuario
    // corregía manualmente la asignación el indicador se quedaba en
    // amarillo aunque las duraciones coincidieran perfectamente.
    const conf = _computeMatchConfidence(c.duration_minutes, map.episode_number, isManual);
    const dur = c.duration_minutes >= 60
      ? `${Math.floor(c.duration_minutes / 60)}h ${Math.round(c.duration_minutes % 60)}m`
      : `${c.duration_minutes.toFixed(1)} min`;
    // Badge "Existe" si este MPLS/m2ts (o el (season, ep_number) actual)
    // ya tiene sesión persistida. Si la marcas igualmente, se entra en
    // flujo de reemplazo y se pedirá confirmación al pulsar "Crear".
    // Se renderiza en la columna del episodio (col-episode, 1fr) para
    // que no quede recortado — la columna MPLS es estrecha (130px) y
    // tiene overflow:hidden, lo que ocultaba el badge si lo poníamos
    // junto al nombre del fichero.
    const existingSession = _findExistingForCandidate(c, season, map.episode_number);
    const existsBadge = existingSession
      ? `<span class="series-badge-exists" title="${escHtml('Ya existe: ' + (existingSession.mkv_name || existingSession.id))}">✓ Existe</span>`
      : '';
    return `
      <div class="series-ep-row${map.include ? '' : ' unchecked'}${existingSession ? ' has-existing' : ''}">
        <div class="col-cb">
          <input type="checkbox" ${map.include ? 'checked' : ''}
                 onchange="seriesToggleEpisode('${escHtml(c.mpls_path)}', this.checked)">
        </div>
        <div class="col-mpls" title="${escHtml(c.mpls_path)}">${escHtml(c.mpls_name)}</div>
        <div class="col-dur">${dur}</div>
        <div class="col-match" title="${escHtml(conf.title)}">${conf.emoji}</div>
        <div class="col-episode">${isManual ? buildManualInputs(map.episode_number, map.episode_title, c.mpls_path) : buildSelect(map.episode_number, c.mpls_path)}${existsBadge}</div>
      </div>
    `;
  }).join('');

  document.getElementById('series-episodes-table').innerHTML = `
    <div class="series-ep-header">
      <div></div>
      <div>MPLS / fichero</div>
      <div>Duración</div>
      <div title="${isManual ? 'Modo manual' : 'Confianza del match runtime MPLS ↔ TMDb'}">${isManual ? 'Modo' : 'Match'}</div>
      <div>${isManual ? 'Nº episodio + título' : 'Episodio TMDb'}</div>
    </div>
    ${rows}
  `;
}

/** En modo manual el usuario puede editar el título del episodio. */
function seriesChangeEpisodeTitle(mplsPath, title) {
  if (!_seriesState || !_seriesState.mapping[mplsPath]) return;
  _seriesState.mapping[mplsPath].episode_title = (title || '').trim();
  _seriesUpdateCreateButton();
}

function seriesToggleEpisode(mplsPath, checked) {
  if (!_seriesState.mapping[mplsPath]) return;
  _seriesState.mapping[mplsPath].include = checked;
  _seriesUpdateCreateButton();
}

function seriesChangeEpisode(mplsPath, episodeNumberStr) {
  if (!_seriesState.mapping[mplsPath]) return;
  const isManual = _seriesState.mode === 'manual';
  const epNum = parseInt(episodeNumberStr, 10);
  const map = _seriesState.mapping[mplsPath];
  if (isNaN(epNum)) {
    map.episode_number = null;
    // En modo manual no borramos el título — el usuario lo edita aparte.
    if (!isManual) {
      map.episode_title = '';
      map.runtime_minutes = 0;
      map.episode_overview = '';
      map.episode_still_url = '';
    }
  } else {
    map.episode_number = epNum;
    if (isManual) {
      // Modo manual: NO sobreescribimos el título ni runtime — el usuario
      // los teclea por separado en su input dedicado.
    } else {
      const ep = _seriesState.seasonEpisodes.find(e => e.episode_number === epNum);
      map.episode_title = ep ? ep.name : '';
      map.runtime_minutes = ep ? ep.runtime_minutes : 0;
      map.episode_overview = ep ? (ep.overview || '') : '';
      map.episode_still_url = ep ? (ep.still_url || '') : '';
    }
  }
  _seriesUpdateCreateButton();
  // Re-render del table para que el indicador de match (🟢/🟡/⚪) refleje
  // la nueva selección. Sin esto se quedaba en el valor inicial del
  // backend aunque el usuario corrigiera la asignación al episodio
  // correcto. En modo TMDb el re-render conserva el foco del <select>
  // (el navegador lo restablece tras el re-render porque el value sigue
  // siendo el mismo); el coste es despreciable (<5ms).
  _renderSeriesEpisodesTable();
}

function _seriesUpdateCreateButton() {
  const btn = document.getElementById('series-create-btn');
  if (!btn) return;
  const m = _seriesState?.mapping || {};
  const selected = Object.values(m).filter(x => x.include && x.episode_number).length;
  btn.innerHTML = `➕ Crear ${selected} proyecto${selected === 1 ? '' : 's'}`;
  // Solo habilitar si hay serie + temporada + al menos un episodio marcado
  btn.disabled = !(_seriesState?.selectedSeries && _seriesState?.selectedSeason && selected > 0);
}

/** Sub-pasos del análisis por episodio. Las "weights" son acumulativas
 *  (0-100) y representan el % completado al ARRANCAR cada paso. La barra
 *  avanza gradualmente entre `weight[i]` y `weight[i+1]` mientras el paso
 *  está activo; el caso pgs usa pgs_pct para interpolación fina dado que
 *  es el paso más largo y phase_a ya emite progreso (bytes leídos).
 *
 *  Las etiquetas son las que se ven en la checklist del modal.
 */
const _SERIES_EP_SUBSTEPS = [
  { key: 'identify',  label: 'Identificando pistas del episodio',      weight: 0  },
  { key: 'chapters',  label: 'Extrayendo capítulos',                   weight: 8  },
  { key: 'mediainfo', label: 'Analizando metadatos (codecs, HDR)',     weight: 18 },
  { key: 'pgs',       label: 'Analizando subtítulos del episodio',     weight: 30 },
  { key: 'dovi',      label: 'Analizando Dolby Vision',                weight: 75 },
  { key: 'rules',     label: 'Aplicando reglas automáticas',           weight: 90 },
  { key: 'save',      label: 'Guardando proyecto',                     weight: 97 },
];

/** Construye el payload de updateProgressModal a partir del estado del
 *  backend (progress) y la lista local de episodios elegidos. Calcula
 *  la barra gradual, etiqueta legible y checklist del episodio en curso. */
function _buildSeriesProgressUpdate(prog, episodes) {
  const total = prog.total || 1;
  const epIdx = Math.max(1, prog.current_index || 1);  // 1-based
  const epLabel = prog.current_episode_title || '';
  const step = prog.current_episode_step || 'identify';

  // Posición del step actual y siguiente en _SERIES_EP_SUBSTEPS.
  const stepIdx = _SERIES_EP_SUBSTEPS.findIndex(s => s.key === step);
  const currentWeight = stepIdx >= 0 ? _SERIES_EP_SUBSTEPS[stepIdx].weight : 0;
  const nextWeight = stepIdx >= 0 && stepIdx < _SERIES_EP_SUBSTEPS.length - 1
    ? _SERIES_EP_SUBSTEPS[stepIdx + 1].weight
    : 100;

  // Interpolación dentro del paso. Solo PGS reporta % granular; el resto
  // queda al inicio de su slot (avance discreto cada vez que cambia el step).
  // Caso especial step='done': episodio cerrado → 100% del slot del episodio
  // (el backend ya saltó al siguiente o terminó).
  let inEpisodePct;
  if (step === 'done') {
    inEpisodePct = 100;
  } else {
    let withinPct = 0;
    if (step === 'pgs' && prog.pgs_pct) {
      withinPct = Math.min(1, Math.max(0, prog.pgs_pct / 100));
    }
    inEpisodePct = currentWeight + (nextWeight - currentWeight) * withinPct;
  }
  const totalPct = ((epIdx - 1) + inEpisodePct / 100) / total * 100;

  // Checklist: marca como done los pasos anteriores, active el actual,
  // pending los siguientes. Si stepIdx<0 (estado inicial / desconocido),
  // todo queda pendiente excepto el primero como activo. Caso especial
  // step='done' (episodio terminado): todos completados (transición a
  // siguiente episodio o cierre del job).
  const allDone = step === 'done';
  const checklist = _SERIES_EP_SUBSTEPS.map((s, i) => {
    let status;
    if (allDone) status = 'done';
    else if (stepIdx < 0) status = i === 0 ? 'active' : 'pending';
    else if (i < stepIdx) status = 'done';
    else if (i === stepIdx) status = 'active';
    else status = 'pending';
    let detail = '';
    if (i === stepIdx && step === 'pgs' && prog.pgs_pct) {
      const eta = prog.pgs_eta_s
        ? `· ETA ${Math.floor(prog.pgs_eta_s / 60)}:${String(prog.pgs_eta_s % 60).padStart(2, '0')}`
        : '';
      detail = `${prog.pgs_pct.toFixed(0)}% ${eta}`.trim();
    }
    return { key: s.key, label: s.label, status, detail };
  });

  // Footnote: resumen compacto de episodios completados/pendientes.
  const epsBefore = epIdx - 1;
  const epsAfter = total - epIdx;
  const parts = [];
  if (epsBefore > 0) parts.push(`✓ ${epsBefore} completado${epsBefore === 1 ? '' : 's'}`);
  parts.push(`⏳ E${String(epIdx).padStart(2, '0')}`);
  if (epsAfter > 0) parts.push(`⏸ ${epsAfter} pendiente${epsAfter === 1 ? '' : 's'}`);
  const footnote = parts.join(' · ');

  const current = epLabel
    ? `Episodio ${epIdx}/${total}: ${epLabel}`
    : (prog.current_label || `Episodio ${epIdx}/${total}`);

  return { current, pct: totalPct, checklist, footnote };
}

/** Pregunta al usuario qué hacer con N episodios marcados que ya tienen
 *  sesión previa. Devuelve una promesa que resuelve a:
 *    'replace'        → el usuario quiere sobrescribir (perderá edits)
 *    'skip_existing'  → omitir los conflictos, crear solo los nuevos
 *    'cancel'         → volver al modal sin hacer nada
 *
 *  Implementado con showConfirm + un botón extra (igual que el modal
 *  de "Ya existe un proyecto" del flujo Película). */
function _seriesConfirmConflicts(count, listText) {
  return new Promise(resolve => {
    showConfirm(
      `${count} episodio${count === 1 ? '' : 's'} ya existen para este origen`,
      `Las siguientes sesiones ya están creadas:\n\n${listText}\n\n` +
      `· "Reemplazar" borra las existentes y crea unas nuevas — perderás ediciones, historial de ejecución y el output MKV si aún no se ha movido.\n` +
      `· "Saltar existentes" mantiene las actuales y procesa solo los episodios nuevos marcados.`,
      () => resolve('replace'),
      '🗑️ Reemplazar',
    );
    // Botón secundario "Saltar existentes" — clonado del patrón de
    // "Abrir existente" del flujo Película (single duplicate).
    const skipBtn = document.createElement('button');
    skipBtn.className = 'btn btn-primary btn-sm confirm-extra-btn';
    skipBtn.textContent = '⏭ Saltar existentes';
    skipBtn.onclick = () => {
      closeModal('confirm-modal');
      resolve('skip_existing');
    };
    const confirmOk = document.getElementById('confirm-ok-btn');
    if (confirmOk) confirmOk.parentNode.insertBefore(skipBtn, confirmOk);
    // Si el usuario cierra el modal por Cancelar o fuera del modal,
    // tratamos como cancel. Hookeamos al cancel-btn del modal estándar.
    const cancelBtn = document.querySelector('#confirm-modal .btn-ghost');
    if (cancelBtn) {
      const onCancel = () => {
        cancelBtn.removeEventListener('click', onCancel);
        resolve('cancel');
      };
      cancelBtn.addEventListener('click', onCancel);
    }
  });
}

async function seriesCreateSessions() {
  if (!_seriesState) return;
  const s = _seriesState;
  const episodes = Object.entries(s.mapping)
    .filter(([_, v]) => v.include && v.episode_number)
    .map(([mplsPath, v]) => ({
      mpls_path: mplsPath,
      episode_number: v.episode_number,
      episode_title: v.episode_title || '',
      runtime_minutes: v.runtime_minutes || 0,
      episode_overview: v.episode_overview || '',
      episode_still_url: v.episode_still_url || '',
    }))
    .sort((a, b) => a.episode_number - b.episode_number);

  if (!episodes.length) {
    showToast('Selecciona al menos un episodio', 'warning');
    return;
  }

  // ── Detección frontend de conflictos ─────────────────────────────
  // El usuario puede haber marcado un episodio que YA existe (badge
  // ✓ Existe). Antes de lanzar la creación, le pedimos confirmación:
  //   - Reemplazar: borra las existentes y crea las nuevas (perdemos
  //     edits + historial de ejecución).
  //   - Saltar existentes: crea solo los nuevos.
  //   - Cancelar: vuelve al modal sin hacer nada.
  //
  // Sin esto, el backend caía en mode='add_only' y devolvía 409, pero
  // el frontend solo veía null y mostraba un toast genérico. Usamos el
  // helper _findExistingForCandidate para consistencia con el render
  // (que mira primero por mpls_basename, fallback por season+episode).
  const seasonNum = s.selectedSeason.season_number;
  const candsByPath = new Map();
  for (const c of s.probe.episode_candidates) {
    candsByPath.set(c.mpls_path, c);
  }
  const conflicts = episodes
    .map(ep => {
      const cand = candsByPath.get(ep.mpls_path);
      const existing = _findExistingForCandidate(cand, seasonNum, ep.episode_number);
      return existing ? {
        season_number: seasonNum,
        episode_number: ep.episode_number,
        episode_title: ep.episode_title,
        existing,
      } : null;
    })
    .filter(Boolean);

  // Modo a enviar al backend. add_only para casos sin conflictos
  // (defensa contra race conditions), replace si el usuario confirma
  // sobrescribir, skip_existing si decide saltarlos.
  let createMode = 'add_only';
  if (conflicts.length > 0) {
    const list = conflicts.map(c => {
      const sn = String(c.season_number).padStart(2, '0');
      const en = String(c.episode_number).padStart(2, '0');
      const tStr = c.existing?.updated_at
        ? ` · actualizado ${new Date(c.existing.updated_at).toLocaleDateString('es-ES')}`
        : '';
      return `S${sn}E${en}${c.episode_title ? ' — ' + c.episode_title : ''}${tStr}`;
    }).join('\n');
    const decision = await _seriesConfirmConflicts(conflicts.length, list);
    if (decision === 'cancel') return;
    createMode = decision;  // 'replace' | 'skip_existing'
  }

  const btn = document.getElementById('series-create-btn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `⏳ Creando ${episodes.length} proyecto${episodes.length === 1 ? '' : 's'}…`;
  }

  // Cerramos el series-modal y abrimos el progress-modal para que el
  // usuario vea feedback continuo (sin esto, el modal de series se
  // congela durante 1-3 minutos sin actividad visible).
  // El subtítulo lleva el nombre de la serie/temporada para que el
  // usuario sepa de qué se está analizando los capítulos.
  closeModal('series-modal');
  const seriesTitle = s.selectedSeries.name || '—';
  const seriesYear = s.selectedSeries.year ? ` (${s.selectedSeries.year})` : '';
  const seasonLabel = `Temporada ${s.selectedSeason.season_number}`;
  showProgressModal({
    title: `${seriesTitle}${seriesYear}`,
    sub: `${seasonLabel} · Analizando ${episodes.length} episodio${episodes.length === 1 ? '' : 's'} y creando proyecto${episodes.length === 1 ? '' : 's'}`,
    icon: '📺',
    posterUrl: s.selectedSeries.poster_url || '',
  });

  // Polling del progreso. /api/series-create-progress devuelve current_index,
  // current_episode_step + pgs_pct para construir gradual progress + checklist.
  // Sin esto la barra saltaba 33%/66%/100% sin detalle del trabajo interno.
  const pollId = setInterval(async () => {
    try {
      const prog = await apiFetch('/api/series-create-progress');
      if (prog && prog.running && prog.total > 0) {
        const update = _buildSeriesProgressUpdate(prog, episodes);
        updateProgressModal(update);
      }
    } catch (_) { /* silenciar errores de polling */ }
  }, 500);

  // El backend procesa cada MPLS/M2TS y crea las sesiones.
  // Coste: ~30s (mount ISO) + N × 15-30s. Para 4 episodios típicos: ~2 min.
  // Payload generalizado a los 3 tipos (v2.6+): source_type + source_path
  // (+ m2ts_paths si aplica). iso_path se mantiene como alias compat.
  // Incluimos los metadatos TMDb de la serie a nivel root para que el
  // backend pueda construir un tmdb_info por episodio en cada Session.
  const payload = {
    series_tmdb_id: s.selectedSeries.tmdb_id,
    series_name: s.selectedSeries.name,
    series_year: s.selectedSeries.year,
    series_poster_url: s.selectedSeries.poster_url || '',
    series_backdrop_url: s.selectedSeries.backdrop_url || '',
    series_overview: s.selectedSeries.overview || '',
    series_genres: s.selectedSeries.genres || [],
    series_vote_average: s.selectedSeries.vote_average || 0,
    season_number: s.selectedSeason.season_number,
    episodes,
    mode: createMode,
  };
  if (s.probe.source_type) {
    payload.source_type = s.probe.source_type;
    payload.source_path = s.probe.source_path;
    if (s.probe.source_type === 'm2ts' && s.probe.m2ts_paths) {
      payload.m2ts_paths = s.probe.m2ts_paths;
    }
  }
  // Compat con backend antiguo
  payload.iso_path = s.probe.iso_path || s.probe.source_path;
  const data = await apiFetch('/api/create-series-sessions', {
    method: 'POST',
    body: JSON.stringify(payload),
  }, 600000);  // timeout 10 min

  clearInterval(pollId);

  if (!data) {
    closeProgressModal();
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `➕ Crear ${episodes.length} proyecto${episodes.length === 1 ? '' : 's'}`;
    }
    showToast('No se pudieron crear los proyectos. Revisa el log del servidor.', 'error');
    return;
  }

  const created = data.created || [];
  const failed = data.failed || [];
  _seriesState = null;

  // Marcamos el modal como done (barra verde 100% + checkmark, sin
  // spinner gris): da cierre visual antes de pasar a abrir las pestañas.
  // Pequeño delay para que el usuario lo perciba (300ms es suficiente).
  updateProgressModal({
    current: `✓ ${created.length} proyecto${created.length === 1 ? '' : 's'} creado${created.length === 1 ? '' : 's'}`,
    done: true,
  });
  await new Promise(r => setTimeout(r, 350));
  closeProgressModal();

  const skippedExisting = data.skipped_existing || [];
  const replacedIds = data.replaced_ids || [];
  const okWord = created.length === 1 ? 'proyecto creado' : 'proyectos creados';
  // Mensaje de toast adaptado a las distintas combinaciones (creados,
  // fallidos, saltados, reemplazados). Sin esto el usuario veía solo el
  // count de creados aunque hubiera saltado o reemplazado N.
  const extras = [];
  if (replacedIds.length) extras.push(`${replacedIds.length} reemplazado${replacedIds.length === 1 ? '' : 's'}`);
  if (skippedExisting.length) extras.push(`${skippedExisting.length} saltado${skippedExisting.length === 1 ? '' : 's'} (ya existía${skippedExisting.length === 1 ? '' : 'n'})`);
  const extrasStr = extras.length ? ` · ${extras.join(' · ')}` : '';
  if (failed.length) {
    const failWord = failed.length === 1 ? 'falló' : 'fallaron';
    showToast(`${created.length} ${okWord} · ${failed.length} ${failWord}${extrasStr}. Revisa el log del servidor.`, 'warning');
  } else if (created.length === 0 && skippedExisting.length > 0) {
    showToast(`Sin novedades: los ${skippedExisting.length} episodios ya existían`, 'info');
  } else {
    showToast(`${created.length} ${okWord}${extrasStr}`, 'success');
  }

  // Refrescar el sidebar y abrir TODAS las pestañas de episodios creados.
  // Las sesiones llegan ya ordenadas por episode_number desde el backend
  // (que itera el payload.episodes que enviamos sorted). openProject()
  // respeta MAX_PROJECTS y rechaza si el slot está lleno — silenciamos
  // los toasts por episodio y mostramos uno único al final si hubo skip.
  await loadSessions();
  if (created.length > 0) {
    const availableSlots = Math.max(0, MAX_PROJECTS - openProjects.length);
    const toOpen = created.slice(0, availableSlots);
    const skipped = created.length - toOpen.length;
    // Abrimos en orden de izquierda a derecha (E01 → ENN). El último
    // openProject() deja ese tab como activo; lo "anclamos" al final
    // re-activando el primer episodio para que sea el visible.
    for (const sess of toOpen) {
      openProject(sess);
    }
    if (toOpen.length > 0) {
      openProject(toOpen[0]);
    }
    if (skipped > 0) {
      showToast(
        `${skipped} episodio${skipped === 1 ? '' : 's'} creado${skipped === 1 ? '' : 's'} pero no abierto${skipped === 1 ? '' : 's'} (límite ${MAX_PROJECTS} pestañas). Ábrelos desde el sidebar.`,
        'info',
      );
    }
  }
}


/** Devuelve el nodo de texto visible del paso (label directo o anidado para pgs). */
function _analyzeStepLabelNode(stepKey) {
  // El paso pgs tiene estructura compleja (label + bar + stats)
  if (stepKey === 'pgs') return document.getElementById('analyze-step-pgs-label');
  return document.getElementById(`analyze-step-${stepKey}`);
}

/** Resetea todos los pasos del modal de análisis al estado inicial. */
/** Lookup TMDb best-effort para el analyze-modal (Tab 1 movies). Si hay
 *  match, sustituye el icono emoji por la cartela y el título genérico
 *  ("Analizando disco" etc.) por el nombre + año de la película. La
 *  acción se baja al sub junto al filename. Se ejecuta en paralelo al
 *  análisis — si tarda o falla, el modal sigue funcionando. */
async function _hydrateAnalyzeModalTmdb(sourceName, sourceType) {
  if (!sourceName) return;
  try {
    const data = await apiFetch('/api/cmv40/tmdb-lookup', {
      method: 'POST',
      body: JSON.stringify({ source_mkv_name: sourceName }),
      silent: true,
    }, 10000);
    if (!data || !data.details) return;
    const t = data.details;
    // El usuario pudo cerrar el modal (cancelar análisis o error). Si
    // ya no está abierto, descartamos la hidratación silenciosamente.
    const modal = document.getElementById('analyze-modal');
    if (!modal || !modal.classList.contains('open')) return;

    const posterEl = document.getElementById('analyze-modal-poster');
    if (posterEl && t.poster_url) {
      posterEl.innerHTML = `<img src="${escHtml(t.poster_url)}" alt="${escHtml(t.title || '')}" loading="lazy">`;
    }
    const titleEl = document.getElementById('analyze-modal-title');
    if (titleEl && t.title) {
      const yr = t.year ? ` (${t.year})` : '';
      titleEl.textContent = `${t.title}${yr}`;
    }
    // Sub: combina acción + nombre del fichero para no perder ese
    // contexto al sustituir el título por el match TMDb.
    const subEl = document.getElementById('analyze-modal-iso');
    if (subEl) {
      const action = sourceType === 'iso' ? 'Analizando disco'
        : sourceType === 'bdmv_folder' ? 'Analizando carpeta BDMV'
        : 'Analizando fichero M2TS';
      subEl.textContent = `${action} · ${sourceName}`;
    }
  } catch (_) { /* TMDb no configurado / sin red / etc. — silencioso */ }
}

/** Configura iconos y labels del analyze-modal según el tipo de fuente.
 *  Se llama antes de _resetAnalyzeSteps. Sin esto, el modal mostraba
 *  siempre "Analizando disco / Montando ISO / Extrayendo capítulos del
 *  MPLS" — engañoso para BDMV folder y m2ts directo. */
function _configureAnalyzeModalForSource(sourceType) {
  // El poster se sustituye por <img> si TMDb da match. En aperturas
  // posteriores hay que restaurar el span del icono (lo perdió el
  // innerHTML del lookup anterior).
  const posterEl = document.getElementById('analyze-modal-poster');
  if (posterEl) {
    posterEl.innerHTML = '<span id="analyze-modal-icon"></span>';
  }
  const iconEl = document.getElementById('analyze-modal-icon');
  const titleEl = document.getElementById('analyze-modal-title');
  const mountEl = document.getElementById('analyze-step-mount');
  const chaptersEl = document.getElementById('analyze-step-chapters');
  const identifyEl = document.getElementById('analyze-step-identify');
  if (sourceType === 'iso') {
    if (iconEl) iconEl.textContent = '💿';
    if (titleEl) titleEl.textContent = 'Analizando disco';
    if (mountEl) mountEl.textContent = '⏳ Montando el ISO…';
    if (identifyEl) identifyEl.textContent = '⬜ Identificando pistas del disco…';
    if (chaptersEl) chaptersEl.textContent = '⬜ Extrayendo capítulos…';
  } else if (sourceType === 'bdmv_folder') {
    if (iconEl) iconEl.textContent = '📁';
    if (titleEl) titleEl.textContent = 'Analizando carpeta BDMV';
    if (mountEl) mountEl.textContent = '✅ Carpeta directa — no requiere montaje';
    if (identifyEl) identifyEl.textContent = '⬜ Identificando pistas del playlist principal…';
    if (chaptersEl) chaptersEl.textContent = '⬜ Extrayendo capítulos del playlist…';
  } else if (sourceType === 'm2ts') {
    if (iconEl) iconEl.textContent = '🎞️';
    if (titleEl) titleEl.textContent = 'Analizando fichero M2TS';
    if (mountEl) mountEl.textContent = '✅ Fichero directo — no requiere montaje';
    if (identifyEl) identifyEl.textContent = '⬜ Identificando pistas del fichero…';
    if (chaptersEl) chaptersEl.textContent = '⬜ Generando capítulos automáticos cada 10 min…';
  }
}

function _resetAnalyzeSteps() {
  const steps = ['mount', 'identify', 'chapters', 'mediainfo', 'pgs', 'dovi', 'rules'];
  steps.forEach((s, i) => {
    const container = document.getElementById(`analyze-step-${s}`);
    if (container) container.style.opacity = i === 0 ? '1' : '.4';
    const labelEl = _analyzeStepLabelNode(s);
    if (!labelEl) return;
    labelEl.textContent = labelEl.textContent.replace(/^[✅⏳⬜]\s*/, i === 0 ? '⏳ ' : '⬜ ');
  });
  // Reset bar/stats del step pgs
  const barWrap = document.getElementById('analyze-step-pgs-bar');
  const statsEl = document.getElementById('analyze-step-pgs-stats');
  const barFill = document.getElementById('analyze-step-pgs-bar-fill');
  if (barWrap) barWrap.style.display = 'none';
  if (statsEl) statsEl.style.display = 'none';
  if (barFill) barFill.style.width = '0%';
}

/** Marca un paso como completado y activa el siguiente. */
function _advanceAnalyzeStep(doneStep, nextStep) {
  const doneContainer = document.getElementById(`analyze-step-${doneStep}`);
  if (doneContainer) doneContainer.style.opacity = '1';
  const doneLabel = _analyzeStepLabelNode(doneStep);
  if (doneLabel) {
    doneLabel.textContent = doneLabel.textContent.replace(/^[⏳⬜]\s*/, '✅ ');
  }
  // Ocultar la barra del pgs al completarse
  if (doneStep === 'pgs') {
    const barWrap = document.getElementById('analyze-step-pgs-bar');
    const statsEl = document.getElementById('analyze-step-pgs-stats');
    if (barWrap) barWrap.style.display = 'none';
    if (statsEl) statsEl.style.display = 'none';
  }
  const nextContainer = document.getElementById(`analyze-step-${nextStep}`);
  if (nextContainer) nextContainer.style.opacity = '1';
  const nextLabel = _analyzeStepLabelNode(nextStep);
  if (nextLabel) {
    nextLabel.textContent = nextLabel.textContent.replace(/^[⬜]\s*/, '⏳ ');
  }
}

// ═══════════════════════════════════════════════════════════════════
//  SESIONES SIDEBAR
// ═══════════════════════════════════════════════════════════════════

/** ID del proyecto seleccionado en el sidebar (sin abrir). @type {string|null} */
let selectedSidebarSessionId = null;

/** Caché de todas las sesiones para poder re-filtrar sin nueva petición. @type {Object[]} */
let _sessionsCache = [];

/** Carga todas las sesiones desde GET /api/sessions y las renderiza en el sidebar. */
async function loadSessions() {
  // silent: refresh background invocado desde WS callbacks, visibilitychange,
  // tras acciones, etc. Bajo VPN/red flaky un timeout transitorio no es
  // accionable — el siguiente refresh automatico lo corrige.
  const data = await apiFetch('/api/sessions', { silent: true });
  if (!data) return;
  _sessionsCache = [...data.sessions];
  // Siempre aplica sort + filter + búsqueda activa
  _doFilterSidebarSessions();
  renderColaSidebar();
  // Actualizar spinner en el proyecto en ejecución (tras re-render del sidebar)
  _updateSidebarRunningIcon();
}

/**
 * Normaliza un string para búsqueda: minúsculas, sin tildes, sin puntuación.
 * @param {string} s
 * @returns {string}
 */
function normalizeSearch(s) {
  return s
    .toLowerCase()
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')  // quitar tildes
    .replace(/[^a-z0-9\s]/g, ' ')                       // quitar puntuación
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Re-filtra la lista del sidebar usando el valor actual del input de búsqueda.
 * Se llama con debounce (150ms) desde el oninput del campo para evitar
 * reconstruir el DOM en cada keystroke.
 */
let _filterDebounceTimer = null;
function filterSidebarSessions() {
  clearTimeout(_filterDebounceTimer);
  _filterDebounceTimer = setTimeout(_doFilterSidebarSessions, 150);
}

/** Nombre de display para una sesión en el sidebar y filtros. Para
 *  series TV, mkv_name es Plex-style ("Serie (Año)/Season NN/Serie...
 *  - SNNeNN - Título.mkv") — el path completo es ilegible en una card,
 *  así que extraemos solo el basename. Para pelis se queda igual. */
function _sessionDisplayName(s) {
  if (!s.mkv_name) {
    return s.id.replace(/_\d+$/, '').replace(/_/g, ' ');
  }
  const baseName = s.mkv_name.includes('/')
    ? s.mkv_name.split('/').pop()
    : s.mkv_name;
  return baseName.replace(/\.mkv$/i, '');
}

function _doFilterSidebarSessions() {
  const query = normalizeSearch(document.getElementById('sidebar-search')?.value || '');
  if (!query) {
    renderSidebarSessions(_sessionsCache);
    return;
  }
  const filtered = _sessionsCache.filter(s => {
    const name = _sessionDisplayName(s);
    return normalizeSearch(name).includes(query);
  });
  renderSidebarSessions(filtered, query);
}

/**
 * Formatea una fecha como "hace X" (relativo) para fechas recientes,
 * o como fecha corta para fechas más antiguas.
 * @param {string} isoDate
 * @returns {string}
 */
function formatRelativeDate(isoDate) {
  if (!isoDate) return '—';
  const d    = new Date(isoDate);
  const now  = Date.now();
  const diff = now - d.getTime();
  const mins  = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  const days  = Math.floor(diff / 86400000);
  if (mins < 1)    return 'ahora mismo';
  if (mins < 60)   return `hace ${mins} min`;
  if (hours < 24)  return `hace ${hours} h`;
  if (days < 7)    return `hace ${days} día${days !== 1 ? 's' : ''}`;
  return d.toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit', year: '2-digit' });
}

/**
 * Actualiza todos los elementos con clase .relative-date en la página.
 * Recalcula el texto relativo ("hace 5 min", "ahora mismo") a partir
 * del atributo data-iso sin re-renderizar toda la lista.
 */
function _refreshRelativeDates() {
  document.querySelectorAll('.relative-date').forEach(el => {
    const iso = el.dataset.iso;
    if (iso) el.textContent = formatRelativeDate(iso);
  });
}

// Actualizar fechas relativas cada 30 segundos
setInterval(_refreshRelativeDates, 30_000);

/** Estado actual de ordenación y filtro del sidebar. */
let _sidebarSort    = 'modified';
let _sidebarSortAsc = false; // false = descendente (más reciente primero por defecto)
let _sidebarFilter  = 'all';

/** Callback del select de ordenación. */
function onSidebarSortChange() {
  _sidebarSort = document.getElementById('sidebar-sort')?.value || 'modified';
  // Nombre es natural asc, fechas/estado naturalmente desc
  _sidebarSortAsc = (_sidebarSort === 'name');
  _updateSortDirBtn();
  _doFilterSidebarSessions();
}

/** Alterna la dirección de ordenación asc/desc. */
function toggleSidebarSortDir() {
  _sidebarSortAsc = !_sidebarSortAsc;
  _updateSortDirBtn();
  _doFilterSidebarSessions();
}

function _updateSortDirBtn() {
  const btn = document.getElementById('sidebar-sort-dir');
  if (btn) btn.textContent = _sidebarSortAsc ? '↑' : '↓';
}

/** Callback de los pills de filtro por estado. */
function onSidebarFilterClick(btn) {
  _sidebarFilter = btn.dataset.filter || 'all';
  document.querySelectorAll('.sb-filter-pill').forEach(p =>
    p.classList.toggle('active', p.dataset.filter === _sidebarFilter));
  _doFilterSidebarSessions();
}

/**
 * Determina el estado de ejecución efectivo de una sesión para filtros y badge.
 * Usa la última entrada de execution_history si existe, o el status directo.
 */
function _sessionExecStatus(s) {
  if (s.status === 'running' || s.status === 'queued') return s.status;
  const hist = s.execution_history || [];
  if (hist.length) return hist[hist.length - 1].status; // 'done' | 'error'
  return 'pending'; // nunca ejecutado
}

/**
 * Aplica ordenación, filtro de texto y filtro de estado sobre _sessionsCache.
 * Llamada desde el debounce de búsqueda, el select de sort y los pills de filtro.
 */
function _doFilterSidebarSessions() {
  const query = normalizeSearch(document.getElementById('sidebar-search')?.value || '');
  let list = [..._sessionsCache];

  // Filtro de texto
  if (query) {
    list = list.filter(s => {
      const name = _sessionDisplayName(s);
      return normalizeSearch(name).includes(query);
    });
  }

  // Filtro de estado
  if (_sidebarFilter !== 'all') {
    list = list.filter(s => _sessionExecStatus(s) === _sidebarFilter);
  }

  // Ordenación (dir: _sidebarSortAsc invierte el resultado)
  const dir = _sidebarSortAsc ? 1 : -1;
  list.sort((a, b) => {
    let cmp = 0;
    switch (_sidebarSort) {
      case 'name': {
        const na = (a.mkv_name || a.id).toLowerCase();
        const nb = (b.mkv_name || b.id).toLowerCase();
        cmp = na.localeCompare(nb);
        break;
      }
      case 'executed': {
        const ea = a.last_executed ? new Date(a.last_executed).getTime() : 0;
        const eb = b.last_executed ? new Date(b.last_executed).getTime() : 0;
        cmp = ea - eb; // natural asc; dir lo invierte si desc
        break;
      }
      case 'status': {
        const order = { running: 0, queued: 1, error: 2, pending: 3, done: 4 };
        cmp = (order[_sessionExecStatus(a)] ?? 5) - (order[_sessionExecStatus(b)] ?? 5);
        break;
      }
      default: { // modified
        const ta = new Date(a.updated_at || a.created_at).getTime();
        const tb = new Date(b.updated_at || b.created_at).getTime();
        cmp = ta - tb; // natural asc; dir lo invierte si desc
        break;
      }
    }
    return cmp * dir;
  });

  renderSidebarSessions(list, query || (_sidebarFilter !== 'all' ? _sidebarFilter : ''));
}

/**
 * Renderiza las tarjetas de proyecto en el sidebar.
 * @param {Object[]} sessions - Sesiones ya ordenadas y filtradas.
 * @param {string}   [query]  - Término de filtro activo (para el contador).
 */
function renderSidebarSessions(sessions, query = '') {
  const container = document.getElementById('sessions-list');
  const countEl   = document.getElementById('sessions-count');
  if (!container || !countEl) return;

  countEl.textContent = query
    ? `${sessions.length} / ${_sessionsCache.length}`
    : sessions.length;

  if (!_sessionsCache.length) {
    selectedSidebarSessionId = null;
    container.innerHTML = `<div class="empty-state">
      <div class="empty-state-icon">🗂️</div>
      <div>Sin proyectos todavía</div>
      <div style="font-size:11px;color:var(--text-3);margin-top:4px">Pulsa "Nuevo proyecto" para empezar</div>
    </div>`;
    return;
  }

  if (!sessions.length) {
    container.innerHTML = `<div class="empty-state">
      <div class="empty-state-icon">🔎</div>
      <div>Sin resultados</div>
      <div style="font-size:11px;color:var(--text-3);margin-top:4px">Prueba con otro término o filtro</div>
    </div>`;
    return;
  }

  const statusIcons = { pending: '💿', queued: '⏸', running: '⏳', done: '✅', error: '❌' };
  const statusLabels = { pending: 'Sin ejecutar', queued: 'En cola', running: 'En curso', done: 'Completado', error: 'Error' };

  container.innerHTML = '';
  sessions.forEach(s => {
    const isSelected = selectedSidebarSessionId === s.id;
    const execStatus = _sessionExecStatus(s);
    const statusIcon = statusIcons[execStatus] || '💿';

    const name = _sessionDisplayName(s);

    const modDate = formatRelativeDate(s.updated_at || s.created_at);
    const modFull = new Date(s.updated_at || s.created_at).toLocaleString('es-ES', {
      day: '2-digit', month: '2-digit', year: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });

    const execDate = s.last_executed ? formatRelativeDate(s.last_executed) : '—';
    const execFull = s.last_executed
      ? new Date(s.last_executed).toLocaleString('es-ES')
      : 'Nunca ejecutado';

    const card = document.createElement('div');
    card.className = `session-card${isSelected ? ' selected' : ''}`;
    card.dataset.sid = s.id;
    const isOpen = !!openProjects.find(p => p.sessionId === s.id);
    card.innerHTML = `
      <div class="session-card-row">
        <div class="session-card-status-badge" data-tooltip="${escHtml(statusLabels[execStatus] || '')}">${statusIcon}</div>
        <div class="session-card-body">
          <div class="session-card-title" data-tooltip="${escHtml(name)}">${escHtml(name)}</div>
          <div class="session-card-meta">
            <div class="session-card-meta-row">
              <span class="meta-label">Modif.</span>
              <span class="relative-date" data-iso="${s.updated_at || s.created_at || ''}"
                data-tooltip="${escHtml('Modificado: ' + modFull)}">${escHtml(modDate)}</span>
            </div>
            <div class="session-card-meta-row">
              <span class="meta-label">Ejecuc.</span>
              <span class="relative-date" data-iso="${s.last_executed || ''}"
                data-tooltip="${escHtml(execFull)}">${escHtml(execDate)}</span>
            </div>
          </div>
        </div>
        ${isOpen ? '<span class="session-item-badge">abierto</span>' : ''}
      </div>
      <div class="session-card-actions">
        <button class="btn btn-primary btn-sm" onclick="confirmOpenSession('${s.id}','${escHtml(name)}')"
          data-tooltip="Abrir este proyecto en una sub-pestaña de revisión">📂 Abrir</button>
        <button class="btn btn-danger btn-sm" onclick="confirmDeleteSession('${s.id}','${escHtml(name)}')"
          data-tooltip="Eliminar permanentemente este proyecto">🗑️ Eliminar</button>
      </div>`;
    const row = card.querySelector('.session-card-row');
    row.onclick = () => toggleSidebarSelection(s.id);
    row.ondblclick = () => confirmOpenSession(s.id, name);
    container.appendChild(card);
  });
}

/**
 * Alterna la selección de un proyecto en el sidebar.
 * Si ya estaba seleccionado, lo deselecciona.
 * @param {string} sessionId
 */
function toggleSidebarSelection(sessionId) {
  selectedSidebarSessionId = (selectedSidebarSessionId === sessionId) ? null : sessionId;
  document.querySelectorAll('.session-card').forEach(card => {
    card.classList.toggle('selected', card.dataset.sid === selectedSidebarSessionId);
  });
}

/**
 * Abre el diálogo de confirmación antes de abrir un proyecto guardado.
 * @param {string} sessionId
 * @param {string} name - Nombre legible del proyecto.
 */
function confirmOpenSession(sessionId, name) {
  showConfirm(
    '📂 Abrir proyecto',
    `¿Abrir el proyecto "${name}"?\n\nSe cargará en una nueva sub-pestaña de revisión.`,
    () => loadSession(sessionId),
    '📂 Abrir'
  );
}

/**
 * Abre el diálogo de confirmación antes de eliminar un proyecto.
 * @param {string} sessionId
 * @param {string} name - Nombre legible del proyecto.
 */
function confirmDeleteSession(sessionId, name) {
  showConfirm(
    '🗑️ Eliminar proyecto',
    `¿Eliminar permanentemente el proyecto "${name}"?\n\nEsta acción no se puede deshacer. El MKV de salida (si existe) no se borrará.`,
    () => deleteSession(sessionId),
    '🗑️ Eliminar'
  );
}

/**
 * Elimina una sesión vía DELETE /api/sessions/{id} y refresca el sidebar.
 * @param {string} sessionId
 */
async function deleteSession(sessionId) {
  const resp = await apiFetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
  if (resp === null) return;  // error ya manejado por apiFetch
  // Cerrar el proyecto si estaba abierto
  const proj = openProjects.find(p => p.sessionId === sessionId);
  if (proj) _doCloseProject(proj.id);
  if (selectedSidebarSessionId === sessionId) selectedSidebarSessionId = null;
  showToast('Proyecto eliminado.', 'success');
  await loadSessions();
}

/**
 * Carga una sesión por ID desde el backend y la abre como proyecto.
 * Si ya existe el proyecto abierto, lo activa.
 * @param {string} sessionId
 */
async function loadSession(sessionId) {
  const session = await apiFetch(`/api/sessions/${sessionId}`);
  if (!session) return;
  openProject(session);
}

// ═══════════════════════════════════════════════════════════════════
//  RENDER SESIÓN (Fase C)
// ═══════════════════════════════════════════════════════════════════

/**
 * Renderiza la pantalla de revisión completa para una sesión.
 *
 * Actualiza: pipeline bar, variables globales (FEL/DCP/nombre MKV),
 * pistas incluidas y descartadas, capítulos, área de ejecución y consola.
 * Si la sesión está en estado 'running', reconecta el WebSocket.
 *
 * @param {Object} session - Objeto sesión completo devuelto por el backend.
 */
/**
 * Rellena el panel de revisión de un proyecto con los datos de su sesión.
 * Requiere que activeSubTabId apunte al project.id.
 * @param {Object} project
 */
function renderProjectPanel(project) {
  const session = project.session;
  if (!session) return;

  currentSession = session;

  // Heal silencioso: sesiones guardadas con versiones anteriores de
  // recoverTrack podrían tener audios después de subs en included_tracks.
  // Normaliza sin marcar dirty (es una corrección cosmética al cargar).
  if (session.included_tracks && session.included_tracks.length) {
    const before = session.included_tracks.map(t => t.track_type).join(',');
    _enforceTrackGrouping(session.included_tracks);
    const after = session.included_tracks.map(t => t.track_type).join(',');
    if (before !== after) {
      session.included_tracks.forEach((t, i) => { t.position = i; });
    }
  }

  // Reinicia el tracking de posiciones originales — una sola pasada
  // coherente para todas las listas del panel (incluidas + descartadas,
  // audio + subs). Evita colisiones con pistas duplicadas.
  _resetOrigIndexTracking();

  // Ficha TMDb en la cabecera (mismo look que Tab 3). Best-effort.
  // Para series TV (v2.5+) la sesión ya trae `tmdb_info` poblado por
  // create_series_sessions con la info del episodio concreto (título,
  // sinopsis, still). Pintamos directo sin hacer otra búsqueda — el
  // lookup por nombre buscaría la serie y traería metadata genérica.
  // Para pelis seguimos con el flujo lookup-por-filename de siempre.
  const tmdbCardId = `${project.id}-tmdb-card`;
  if (session.tmdb_info && session.media_type === 'series') {
    const tmdbEl = document.getElementById(tmdbCardId);
    if (tmdbEl) tmdbEl.innerHTML = renderTmdbCardHTML(session.tmdb_info) || '';
  } else {
    // Parseamos el título del mkv_name (o del basename del ISO si aún no
    // hay mkv_name). Cache global evita re-fetches entre cambios.
    const filenameForTmdb = session.mkv_name
      || (session.iso_path ? session.iso_path.split('/').pop() : '');
    hydrateTmdbCard(tmdbCardId, filenameForTmdb);
  }

  // Asegurar que el sub-tab activo es este proyecto
  const prevSubTab = activeSubTabId;
  activeSubTabId = project.id;

  // Estado activo de los toggles de modo audio/subs
  _updateModeToggles(project.id, session.audio_mode || 'filtered', session.subtitle_mode || 'filtered');

  // Variables globales
  setToggle('toggle-fel', session.has_fel);
  setText('fel-value', session.has_fel ? 'FEL' : 'MEL');
  // No mostrar fel_reason si hay dovi detail (evitar duplicado)
  const hasDovi = session.bdinfo_result?.video_tracks?.find(t => !t.is_el)?.dovi;
  setText('fel-reason-text', hasDovi ? '' : (session.bdinfo_result?.fel_reason || ''));
  E('global-fel').className = `global-toggle-item${session.has_fel ? ' active-fel' : ''}`;

  // Info extendida de Dolby Vision (dovi_tool)
  const mainVid = session.bdinfo_result?.video_tracks?.find(t => !t.is_el);
  const doviDetail = E('dovi-detail');
  if (doviDetail && mainVid?.dovi) {
    const d = mainVid.dovi;
    const parts = [`Profile ${d.profile}${d.el_type ? ` (${d.el_type})` : ''}`, `CM ${d.cm_version}`];
    if (d.has_l1) parts.push('L1');
    if (d.has_l2) parts.push('L2');
    if (d.has_l5) parts.push('L5');
    if (d.has_l6) parts.push('L6');
    if (d.scene_count) parts.push(`${d.scene_count} escenas`);
    if (mainVid.hdr?.mastering_display_luminance) parts.push(mainVid.hdr.mastering_display_luminance);
    doviDetail.textContent = parts.join(' · ');
    doviDetail.style.display = '';
  } else if (doviDetail) {
    doviDetail.style.display = 'none';
  }

  setToggle('toggle-dcp', session.audio_dcp);
  setText('dcp-value', session.audio_dcp ? 'Activo' : 'No detectado');
  setText('dcp-reason-text', session.audio_dcp
    ? "Tag 'Audio DCP' encontrado en nombre del ISO"
    : "Tag no encontrado en nombre del ISO");
  E('global-dcp').className = `global-toggle-item${session.audio_dcp ? ' active-dcp' : ''}`;

  const mkvInput = E('mkv-name-input');
  if (mkvInput) mkvInput.value = session.mkv_name || '';
  const manualNotice = E('mkv-name-manual-notice');
  if (manualNotice) manualNotice.style.display = project.mkvNameWasManual ? '' : 'none';

  renderIncludedTracks(session.included_tracks || []);
  renderDiscardedTracks(session.discarded_tracks || []);
  renderChapters(session.chapters || [], session.chapters_auto_generated, session.chapters_auto_reason);
  renderExecuteArea();
  renderExecResultBanner(session);
  renderPhaseStrip(session);
  renderExecutionHistory(session);

  // Banner VO warning
  const voWarning = session.vo_warning || '';
  if (voWarning) {
    setText('vo-warning-text', ' ' + voWarning);
    show('vo-warning-banner');
  } else {
    hide('vo-warning-banner');
  }

  activeSubTabId = prevSubTab;

  // Comprobar disponibilidad del ISO en background (no bloquea el render)
  _checkIsoAvailability(project);

  updateProjectTabIcon(project);
}

/** Alias legacy para compatibilidad con código anterior. */
function renderSession(session) {
  const project = openProject(session);
  if (project) renderProjectPanel(project);
}

/**
 * Comprueba en background si el ISO de un proyecto sigue disponible.
 * Muestra u oculta el banner de ISO no disponible según el resultado.
 * @param {Object} project
 */
async function _checkIsoAvailability(project) {
  const pid = project.id;
  const prevSubTab = activeSubTabId;
  activeSubTabId = pid;

  const data = await apiFetch(`/api/sessions/${project.sessionId}/check-iso`);
  project.isoAvailable = data ? data.available : null;  // null = error de red

  if (data && !data.available) {
    const name = (data.iso_path || '').replace(/\\/g, '/').split('/').pop();
    // Mensaje dinámico según source_type — antes era siempre "El fichero
    // ... ya no se encuentra en /mnt/isos" (asumía ISO), ahora respeta
    // el tipo real del origen (carpeta BDMV / fichero M2TS / ISO).
    const label = data.source_label || 'ISO';
    const verb = data.source_type === 'bdmv_folder' ? 'La carpeta' : 'El fichero';
    setText('iso-missing-title', `${label} no disponible.`);
    setText('iso-missing-text', ` ${verb} "${name}" ya no se encuentra en /mnt/isos.`);
    show('iso-missing-banner');
  } else {
    hide('iso-missing-banner');
  }

  activeSubTabId = prevSubTab;
}

// ═══════════════════════════════════════════════════════════════════
//  PISTAS INCLUIDAS / DESCARTADAS
// ═══════════════════════════════════════════════════════════════════

/** Actualiza los badges de conteo de audio y subtítulos leyendo el estado actual de la sesión. */
/**
 * Busca la posición original de una pista raw en el bdinfo_result.
 * Compara por idioma + codec (audio) o idioma + bitrate (subtítulos).
 * @param {Object} raw — datos raw de la pista incluida
 * @param {'audio'|'subtitle'} type
 * @returns {number} índice 0-based en el array original, o -1 si no se encuentra
 */
/** Actualiza el estado visual de los toggles de modo audio/subtítulos. */
function _updateModeToggles(pid, audioMode, subMode) {
  const prefix = `panel-project-${pid}`;
  const panel = document.getElementById(prefix);
  const root = panel || document;
  root.querySelectorAll('.mode-toggle').forEach(btn => {
    const track = btn.dataset.track;
    const mode = btn.dataset.mode;
    const current = track === 'audio' ? audioMode : subMode;
    btn.classList.toggle('active', mode === current);
  });
}

/** Cambia el modo de selección de audio/subs y re-aplica reglas en backend. */
async function setTrackMode(trackKind, mode) {
  const project = getActiveProject();
  if (!project) return;
  const sid = project.sessionId;
  const body = {};
  if (trackKind === 'audio') body.audio_mode = mode;
  else body.subtitle_mode = mode;
  const updated = await apiFetch(`/api/sessions/${sid}/reapply-rules`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
  if (updated) {
    project.session = updated;
    currentSession = updated;
    renderProjectPanel(project);
    const label = trackKind === 'audio' ? 'Audio' : 'Subtítulos';
    const modeLabel = mode === 'keep_all' ? 'Mantener todas' : 'Filtrado';
    showToast(`${label}: modo «${modeLabel}» aplicado`, 'success');
  }
}

// Índices ya asignados en el render actual — evita colisiones cuando hay
// pistas duplicadas (mismo idioma+codec+descr. ej. dos DD+ francesas, dos
// DD 2.0 inglés con bitrate distinto, dos subs "forced" mismo lang...).
let _usedAudioOrigIdx = new Set();
let _usedSubOrigIdx = new Set();

function _resetOrigIndexTracking() {
  _usedAudioOrigIdx = new Set();
  _usedSubOrigIdx = new Set();
}

// Pre-computa `_orig_index` en todas las pistas (incluidas + descartadas)
// de la sesión actual. Se ejecuta al inicio de cada render — evita que
// render sucesivos (p.ej. tras recoverTrack) pierdan el índice porque el
// set de "usadas" ya estaba lleno de la pasada anterior.
function _precomputeOrigIndices() {
  _resetOrigIndexTracking();
  if (!currentSession) return;
  // Orden: incluidas primero, luego descartadas. Dentro de cada lista,
  // orden actual del array (que el usuario puede haber reordenado).
  const all = [
    ...(currentSession.included_tracks || []),
    ...(currentSession.discarded_tracks || []),
  ];
  for (const t of all) {
    const raw = t.raw || {};
    const type = t.track_type === 'audio' ? 'audio' : 'subtitle';
    t._orig_index = _findOriginalTrackIndex(raw, type);
  }
}

function _findOriginalTrackIndex(raw, type) {
  const bd = currentSession?.bdinfo_result;
  if (!bd) return -1;

  if (type === 'audio') {
    const list = bd.audio_tracks || [];
    // Pasada 1: match estricto (lang + codec + desc + bitrate) — si los
    // bitrates coinciden y la pista no está asignada, la tomamos
    for (let i = 0; i < list.length; i++) {
      if (_usedAudioOrigIdx.has(i)) continue;
      const t = list[i];
      if (t.language === raw.language && t.codec === raw.codec
          && t.description === raw.description
          && t.bitrate_kbps === raw.bitrate_kbps) {
        _usedAudioOrigIdx.add(i);
        return i;
      }
    }
    // Pasada 2: match laxo (lang + codec + desc) — cuando duplicados
    // tienen bitrates idénticos, elegimos cualquiera libre
    for (let i = 0; i < list.length; i++) {
      if (_usedAudioOrigIdx.has(i)) continue;
      const t = list[i];
      if (t.language === raw.language && t.codec === raw.codec
          && t.description === raw.description) {
        _usedAudioOrigIdx.add(i);
        return i;
      }
    }
    return -1;
  }

  // Subtítulos
  const subList = bd.subtitle_tracks || [];
  if (raw.packet_count && raw.packet_count > 0) {
    // Con packet_count el match es definitivo — cada pista tiene un count único
    for (let i = 0; i < subList.length; i++) {
      if (_usedSubOrigIdx.has(i)) continue;
      const t = subList[i];
      if (t.language === raw.language && t.packet_count === raw.packet_count) {
        _usedSubOrigIdx.add(i);
        return i;
      }
    }
  }
  // Fallback: lang + bitrate, saltando usados
  for (let i = 0; i < subList.length; i++) {
    if (_usedSubOrigIdx.has(i)) continue;
    const t = subList[i];
    if (t.language === raw.language && t.bitrate_kbps === raw.bitrate_kbps) {
      _usedSubOrigIdx.add(i);
      return i;
    }
  }
  // Último recurso: lang solo
  for (let i = 0; i < subList.length; i++) {
    if (_usedSubOrigIdx.has(i)) continue;
    if (subList[i].language === raw.language) {
      _usedSubOrigIdx.add(i);
      return i;
    }
  }
  return -1;
}

function updateTrackCounts() {
  if (!currentSession) return;
  const inc  = currentSession.included_tracks  || [];
  const disc = currentSession.discarded_tracks || [];
  const incAudio  = inc.filter(t => t.track_type === 'audio').length;
  const incSub    = inc.filter(t => t.track_type !== 'audio').length;
  const discAudio = disc.filter(t => t.track_type === 'audio').length;
  const discSub   = disc.filter(t => t.track_type !== 'audio').length;
  const audioEl = E('audio-count');
  const subEl   = E('sub-count');
  if (audioEl) audioEl.textContent = `${incAudio} incluidas · ${discAudio} descartadas`;
  if (subEl)   subEl.textContent   = `${incSub} incluidas · ${discSub} descartadas`;
}

/**
 * Devuelve el texto de aviso de ambigüedad efectivo para una pista.
 *
 * Lógica en dos pasos:
 *   1) Si el track tiene su propio `ambiguity_warning` (texto rico
 *      asignado por phase_b al hacer el análisis), devolverlo tal cual.
 *   2) Si NO lo tiene pero la lengua de la pista está en la lista
 *      session.ambiguous_{audio,subtitle}_langs, devolver un fallback
 *      genérico. Esto cubre el caso de swap manual: cuando el usuario
 *      descarta la incluida y recupera otra, los objetos nuevos no
 *      llevan ambiguity_warning, pero la lista a nivel de sesión nos
 *      dice que la lengua era ambigua → seguimos pintando el banner.
 *
 * Devuelve "" si no hay aviso aplicable.
 */
function getTrackAmbiguityWarning(track) {
  if (track && track.ambiguity_warning) return track.ambiguity_warning;
  if (!currentSession || !track || !track.raw || !track.raw.language) return '';
  const isAudio = track.track_type === 'audio';
  const list = isAudio
    ? (currentSession.ambiguous_audio_langs || [])
    : (currentSession.ambiguous_subtitle_langs || []);
  const lang = String(track.raw.language || '').toLowerCase();
  if (!list.includes(lang)) return '';
  const langLit = langLiteral(lang) || lang;
  return (
    `Pertenece a un grupo de pistas ${langLit} marcado como ambiguo en el análisis. ` +
    `Revisa manualmente que la elegida sea la versión que querías.`
  );
}

/**
 * Renderiza la lista de pistas incluidas con controles de edición.
 *
 * @param {Object[]} tracks - Array de IncludedAudioTrack | IncludedSubtitleTrack.
 */
function renderIncludedTracks(tracks) {
  const audioList = E('included-audio-tracks');
  const subList   = E('included-sub-tracks');
  audioList.innerHTML = '';
  subList.innerHTML   = '';

  // Pre-computa el índice original de cada pista (incluidas + descartadas)
  // para que los badges #N sean coherentes y únicos, incluso tras recover.
  _precomputeOrigIndices();

  const byType = { audio: [], subtitle: [] };
  tracks.forEach((track, flatIdx) => {
    const type = track.track_type === 'audio' ? 'audio' : 'subtitle';
    byType[type].push({ track, flatIdx });
  });

  updateTrackCounts();

  // ── Audio ──
  if (!byType.audio.length) {
    audioList.innerHTML = `<li class="track-empty">Sin pistas de audio</li>`;
  } else {
    byType.audio.forEach(({ track, flatIdx }) => {
      const raw  = track.raw || {};
      const def  = track.flag_default ? ' active-default' : '';
      const tooltip = [
        `Codec: ${raw.codec || '—'}`,
        raw.format_commercial ? `Formato: ${raw.format_commercial}` : null,
        `Idioma: ${raw.language || '—'} → ${langLiteral(raw.language) || '—'}`,
        raw.description ? `Canales / frecuencia: ${raw.description}` : null,
        raw.channel_layout ? `Layout: ${raw.channel_layout}` : null,
        raw.bitrate_kbps ? `Bitrate: ${raw.bitrate_kbps.toLocaleString()} kbps` : null,
        raw.compression_mode ? `Compresión: ${raw.compression_mode}` : null,
        `Posición en MKV: #${flatIdx + 1}`,
        '',
        `Razón: ${track.selection_reason || '—'}`,
      ].filter(Boolean).join('\n');
      // Línea informativa idéntica a la de descartadas: idioma · codec ·
      // descripción · bitrate. Antes omitíamos idioma+codec asumiendo que el
      // label editable los cubría, pero el usuario puede renombrarlo y
      // perder esa información — dejarla siempre visible es más util.
      // Bitrate: SIEMPRE el de MediaInfo (raw.bitrate_kbps) — es el real
      // medido (bytes/duración). El que viene en raw.description es el
      // nominal de mkvmerge y en VBR (TrueHD) discrepa hasta ~15% del real.
      // Limpiamos del description el tramo de kbps para no duplicarlo.
      const langLit = langLiteral(raw.language) || raw.language || '';
      const desc = (raw.description || '').replace(/\s*\/\s*\d[\d,.]*\s*kbps\s*/i, ' / ').replace(/\/\s*\//g, '/').replace(/^\s*\/|\/\s*$/g, '').trim();
      const rawLine = [
        langLit,
        raw.codec,
        desc,
        raw.bitrate_kbps ? `${raw.bitrate_kbps.toLocaleString()} kbps` : null,
      ].filter(Boolean).join(' · ');
      const origIdx = (typeof track._orig_index === 'number') ? track._orig_index : -1;
      const origLabel = origIdx >= 0 ? `#${origIdx + 1}` : '';
      const li = document.createElement('li');
      li.className = 'track-item';
      li.dataset.flatIdx = flatIdx;
      li.innerHTML = `
        <span class="track-drag" data-tooltip="Arrastra para reordenar">⠿</span>
        ${origLabel ? `<span class="track-orig-pos" data-tooltip="Posición original de la pista en el ISO">${origLabel}</span>` : ''}
        <span class="track-type-icon" data-tooltip="${escHtml(tooltip)}">🔊</span>
        <div class="track-main">
          <input class="track-label-input" type="text"
            value="${escHtml(track.label || '')}"
            onchange="onTrackLabelChange(${flatIdx}, this.value)"
            data-tooltip="Nombre de la pista en el MKV">
          <span class="track-raw">${escHtml(rawLine)}</span>
        </div>
        <div class="track-flags">
          <button class="flag-pill${def}" onclick="toggleFlag(${flatIdx},'default')"
            data-tooltip="flag default: pista de audio seleccionada por defecto en el reproductor">DEF</button>
        </div>
        <div class="track-actions">
          <button class="btn btn-icon" onclick="discardTrack(${flatIdx})"
            data-tooltip="Descartar esta pista">✕</button>
        </div>
        <div class="track-reason"><span>ℹ️</span><span>${escHtml(track.selection_reason || '')}</span></div>
        ${(() => { const w = getTrackAmbiguityWarning(track); return w ? `<div class="track-ambiguity"><span class="ta-icon">⚠️</span><span class="ta-text">${escHtml(w)}</span></div>` : ''; })()}`;
      audioList.appendChild(li);
    });
  }

  // ── Subtítulos ──
  if (!byType.subtitle.length) {
    subList.innerHTML = `<li class="track-empty">Sin pistas de subtítulos</li>`;
  } else {
    byType.subtitle.forEach(({ track, flatIdx }) => {
      const raw  = track.raw || {};
      const def  = track.flag_default ? ' active-default' : '';
      const frc  = track.flag_forced  ? ' active-forced'  : '';
      const subTypeLabel = track.subtitle_type === 'forced' ? 'Forzados' : 'Completos';
      const packets = raw.packet_count || 0;
      const tooltip = [
        `Codec: PGS (Presentation Graphics)`,
        `Idioma: ${raw.language || '—'} → ${langLiteral(raw.language) || '—'}`,
        `Tipo: ${subTypeLabel}`,
        raw.resolution ? `Resolución: ${raw.resolution}` : null,
        packets > 0 ? `Paquetes PES: ${packets.toLocaleString()} (ffprobe)` : null,
        raw.bitrate_kbps ? `Bitrate sintético: ${raw.bitrate_kbps} kbps` : null,
        `Posición en MKV: #${flatIdx + 1}`,
        '',
        `Razón: ${track.selection_reason || '—'}`,
      ].filter(Boolean).join('\n');
      const pktTag = packets > 0 ? ` · ${packets.toLocaleString()} paq.` : '';
      const rawLine = `PGS · ${langLiteral(raw.language)} · ${subTypeLabel}${pktTag}`;
      const origIdx = (typeof track._orig_index === 'number') ? track._orig_index : -1;
      const origLabel = origIdx >= 0 ? `#${origIdx + 1}` : '';
      const li = document.createElement('li');
      li.className = 'track-item';
      li.dataset.flatIdx = flatIdx;
      li.innerHTML = `
        <span class="track-drag" data-tooltip="Arrastra para reordenar">⠿</span>
        ${origLabel ? `<span class="track-orig-pos" data-tooltip="Posición original de la pista en el ISO">${origLabel}</span>` : ''}
        <span class="track-type-icon" data-tooltip="${escHtml(tooltip)}">💬</span>
        <div class="track-main">
          <input class="track-label-input" type="text"
            value="${escHtml(track.label || '')}"
            onchange="onTrackLabelChange(${flatIdx}, this.value)"
            data-tooltip="Nombre de la pista en el MKV">
          <span class="track-raw">${escHtml(rawLine)}</span>
        </div>
        <div class="track-flags">
          <button class="flag-pill${def}" onclick="toggleFlag(${flatIdx},'default')"
            data-tooltip="flag default: subtítulo seleccionado por defecto">DEF</button>
          <button class="flag-pill${frc}" onclick="toggleFlag(${flatIdx},'forced')"
            data-tooltip="flag forced: subtítulos forzados para diálogos en idioma extranjero">FRC</button>
        </div>
        <div class="track-actions">
          <button class="btn btn-icon" onclick="discardTrack(${flatIdx})"
            data-tooltip="Descartar esta pista">✕</button>
        </div>
        <div class="track-reason"><span>ℹ️</span><span>${escHtml(track.selection_reason || '')}</span></div>
        ${(() => { const w = getTrackAmbiguityWarning(track); return w ? `<div class="track-ambiguity"><span class="ta-icon">⚠️</span><span class="ta-text">${escHtml(w)}</span></div>` : ''; })()}`;
      subList.appendChild(li);
    });
  }

  // Sortable independiente por tipo
  const project = getActiveProject();
  if (project) {
    if (project.sortableAudio) project.sortableAudio.destroy();
    if (project.sortableSubs)  project.sortableSubs.destroy();
    project.sortableAudio = Sortable.create(audioList, {
      handle: '.track-drag', animation: 180,
      ghostClass: 'sortable-ghost', chosenClass: 'sortable-chosen',
      onEnd: (evt) => onTrackReorder(evt, 'audio'),
    });
    project.sortableSubs = Sortable.create(subList, {
      handle: '.track-drag', animation: 180,
      ghostClass: 'sortable-ghost', chosenClass: 'sortable-chosen',
      onEnd: (evt) => onTrackReorder(evt, 'subtitle'),
    });
  }
}

/**
 * Callback de Sortable.js al finalizar un drag & drop.
 * Reordena solo las pistas del tipo arrastrado dentro del array plano.
 * @param {{ oldIndex: number, newIndex: number }} evt
 * @param {'audio'|'subtitle'} type
 */
function onTrackReorder(_evt, type) {
  const tracks = currentSession.included_tracks;
  const listEl = type === 'audio' ? E('included-audio-tracks') : E('included-sub-tracks');
  // Nuevo orden de flat-indices según el DOM post-drag
  const newFlatOrder = Array.from(listEl.querySelectorAll('[data-flat-idx]'))
    .map(el => parseInt(el.dataset.flatIdx));
  // Snapshot de las pistas en su nuevo orden (antes de mutar)
  const reordered = newFlatOrder.map(i => tracks[i]);
  // Índices en el array plano que pertenecen a este tipo
  const typeIndices = tracks
    .map((t, i) => t.track_type === (type === 'audio' ? 'audio' : 'subtitle') ? i : -1)
    .filter(i => i >= 0);
  // Escribe el nuevo orden en el array plano
  typeIndices.forEach((flatIdx, subIdx) => { tracks[flatIdx] = reordered[subIdx]; });
  // Red de seguridad: garantiza que en el MKV final el orden sea
  // [todos los audios…][todos los subs…], por si quedara alguna pista
  // fuera de sitio (p.ej. por estado heredado de versiones anteriores).
  _enforceTrackGrouping(tracks);
  tracks.forEach((t, i) => { t.position = i; });
  currentSession.included_tracks = tracks;
  renderIncludedTracks(tracks);
  markProjectDirty();
}

// Ordena in-place el array para que TODOS los audios vayan antes que
// TODOS los subs (stable sort — preserva el orden relativo dentro de
// cada tipo). Útil como red de seguridad en reorder y recover.
function _enforceTrackGrouping(tracks) {
  // Array.prototype.sort es stable desde ES2019 (Chrome 70+, Safari 10+).
  tracks.sort((a, b) => {
    const ta = a.track_type === 'audio' ? 0 : 1;
    const tb = b.track_type === 'audio' ? 0 : 1;
    return ta - tb;
  });
}

/**
 * Actualiza el label de una pista incluida al editar el input de texto.
 * @param {number} idx   - Índice de la pista en included_tracks.
 * @param {string} value - Nuevo valor del label.
 */
function onTrackLabelChange(idx, value) {
  currentSession.included_tracks[idx].label = value;
  markProjectDirty();
}

/**
 * Alterna el flag default o forced de una pista incluida y re-renderiza.
 * @param {number} idx  - Índice de la pista en included_tracks.
 * @param {'default'|'forced'} flag - Flag a alternar.
 */
function toggleFlag(idx, flag) {
  const track = currentSession.included_tracks[idx];
  if (flag === 'default') track.flag_default = !track.flag_default;
  if (flag === 'forced')  track.flag_forced  = !track.flag_forced;
  renderIncludedTracks(currentSession.included_tracks);
  markProjectDirty();
}

/**
 * Mueve una pista de la lista de incluidas a la de descartadas.
 * @param {number} idx - Índice de la pista a descartar en included_tracks.
 */
function discardTrack(idx) {
  const track = currentSession.included_tracks.splice(idx, 1)[0];
  currentSession.discarded_tracks.push({
    track_type: track.track_type,
    raw: track.raw,
    discard_reason: 'Descartada manualmente por el usuario',
  });
  currentSession.included_tracks.forEach((t, i) => { t.position = i; });
  renderIncludedTracks(currentSession.included_tracks);
  renderDiscardedTracks(currentSession.discarded_tracks);
  markProjectDirty();
}

// ═══════════════════════════════════════════════════════════════════
//  PISTAS DESCARTADAS
// ═══════════════════════════════════════════════════════════════════

/**
 * Renderiza la lista de pistas descartadas con su razón y botón de recuperación.
 * @param {Object[]} tracks - Array de DiscardedTrack.
 */
function renderDiscardedTracks(tracks) {
  const audioContainer = E('discarded-audio-tracks');
  const subContainer   = E('discarded-sub-tracks');
  audioContainer.innerHTML = '';
  subContainer.innerHTML   = '';

  // Mismo precompute que renderIncludedTracks — idempotente,
  // garantiza que los badges son coherentes entre listas.
  _precomputeOrigIndices();

  const byType = { audio: [], subtitle: [] };
  tracks.forEach((track, idx) => {
    const type = track.track_type === 'audio' ? 'audio' : 'subtitle';
    byType[type].push({ track, idx });
  });

  // Orden visual SIEMPRE por posición original del disco (#N), tanto
  // en audio como en subs. El array discarded_tracks puede llegar en
  // cualquier orden — backend lo guarda ordenado tras Fase B, pero
  // ediciones manuales (discardTrack hace push al final) lo desordenan.
  // Sortamos a nivel de render para que el orden visual sea estable
  // sin tener que mantener el array sortado en cada mutación. Stable
  // sort por _orig_index, conservando idx original (apunta al array)
  // para que el botón "Recuperar" siga funcionando.
  const _sortByDiscOrder = (a, b) => {
    const ai = (typeof a.track._orig_index === 'number') ? a.track._orig_index : 99999;
    const bi = (typeof b.track._orig_index === 'number') ? b.track._orig_index : 99999;
    return ai - bi;
  };
  byType.audio.sort(_sortByDiscOrder);
  byType.subtitle.sort(_sortByDiscOrder);

  updateTrackCounts();

  const renderGroup = (container, items, isAudio) => {
    if (!items.length) {
      container.innerHTML = `<div class="discarded-empty">Ninguna descartada</div>`;
      return;
    }
    items.forEach(({ track, idx }) => {
      const raw = track.raw || {};
      const origIdx = (typeof track._orig_index === 'number') ? track._orig_index : -1;
      const origLabel = origIdx >= 0 ? `#${origIdx + 1}` : '';

      // Tooltip con todo el detalle (mismo contenido que en incluidas)
      let tooltip;
      if (isAudio) {
        tooltip = [
          `Codec: ${raw.codec || '—'}`,
          raw.format_commercial ? `Formato: ${raw.format_commercial}` : null,
          `Idioma: ${raw.language || '—'} → ${langLiteral(raw.language) || '—'}`,
          raw.description ? `Canales / frecuencia: ${raw.description}` : null,
          raw.channel_layout ? `Layout: ${raw.channel_layout}` : null,
          raw.bitrate_kbps ? `Bitrate: ${raw.bitrate_kbps.toLocaleString()} kbps` : null,
          raw.compression_mode ? `Compresión: ${raw.compression_mode}` : null,
          '',
          `Razón del descarte: ${track.discard_reason || '—'}`,
        ].filter(s => s !== null).join('\n');
      } else {
        const packets = raw.packet_count || 0;
        tooltip = [
          `Codec: PGS (Presentation Graphics)`,
          `Idioma: ${raw.language || '—'} → ${langLiteral(raw.language) || '—'}`,
          raw.resolution ? `Resolución: ${raw.resolution}` : null,
          packets > 0 ? `Paquetes PES: ${packets.toLocaleString()} (ffprobe)` : null,
          raw.bitrate_kbps ? `Bitrate sintético: ${raw.bitrate_kbps} kbps` : null,
          '',
          `Razón del descarte: ${track.discard_reason || '—'}`,
        ].filter(s => s !== null).join('\n');
      }

      // Label compacto — idioma siempre primero para identificación rápida
      let codecInfo;
      const langLit = langLiteral(raw.language) || raw.language || '';
      if (isAudio) {
        // Mismo criterio que en included: SOLO bitrate de MediaInfo (real
        // medido). Limpiamos el kbps nominal de mkvmerge dentro de
        // raw.description para no duplicar.
        const desc = (raw.description || '').replace(/\s*\/\s*\d[\d,.]*\s*kbps\s*/i, ' / ').replace(/\/\s*\//g, '/').replace(/^\s*\/|\/\s*$/g, '').trim();
        codecInfo = [langLit, raw.codec, desc,
          raw.bitrate_kbps ? `${raw.bitrate_kbps.toLocaleString()} kbps` : null
        ].filter(Boolean).join(' · ');
      } else {
        const packets = raw.packet_count || 0;
        const pktTag = packets > 0 ? `${packets.toLocaleString()} paq.` : '';
        codecInfo = [langLit, 'PGS', pktTag].filter(Boolean).join(' · ');
      }

      const icon = isAudio ? '🔊' : '💬';
      const ambigWarn = getTrackAmbiguityWarning(track);
      const div = document.createElement('div');
      div.className = 'discarded-item' + (ambigWarn ? ' has-ambiguity' : '');
      div.innerHTML = `
        ${origLabel ? `<span class="track-orig-pos" data-tooltip="Posición original de la pista en el ISO">${origLabel}</span>` : ''}
        <span class="track-type-icon" data-tooltip="${escHtml(tooltip)}">${icon}</span>
        <div class="discarded-body">
          <div class="discarded-codec">${escHtml(codecInfo || 'Pista desconocida')}</div>
          <div class="discarded-reason">${escHtml(track.discard_reason || '')}</div>
          ${ambigWarn ? `<div class="track-ambiguity inline"><span class="ta-icon">⚠️</span><span class="ta-text">${escHtml(ambigWarn)}</span></div>` : ''}
        </div>
        <button class="btn btn-ghost btn-xs" onclick="recoverTrack(${idx})"
          data-tooltip="Recuperar esta pista y añadirla a las incluidas">↩ Recuperar</button>`;
      container.appendChild(div);
    });
  };

  renderGroup(audioContainer, byType.audio,    true);
  renderGroup(subContainer,   byType.subtitle, false);
}

/**
 * Mueve una pista de descartadas a incluidas, creando un IncludedTrack mínimo.
 * @param {number} idx - Índice de la pista a recuperar en discarded_tracks.
 */
/**
 * Muestra un modal con los datos de análisis originales del ISO.
 * Incluye: pistas del bdinfo_result (vídeo, audio, subtítulos con posición),
 * capítulos, pistas incluidas/descartadas por las reglas, y flags.
 */
function showRawAnalysisData() {
  if (!currentSession) return;
  const s = currentSession;
  const bd = s.bdinfo_result;
  const lines = [];

  lines.push(`═══════════════════════════════════════════════`);
  lines.push(`  DATOS DE ANÁLISIS DEL ISO`);
  lines.push(`═══════════════════════════════════════════════`);
  lines.push(`Sesión: ${s.id}`);
  lines.push(`ISO: ${s.iso_path}`);
  lines.push(`MKV: ${s.mkv_name}`);
  lines.push(`FEL: ${s.has_fel} | Audio DCP: ${s.audio_dcp}`);
  lines.push('');

  // ── SECCIÓN 1: Datos RAW de mkvmerge -J (sin heurísticas) ──
  if (bd?.mkvmerge_raw) {
    const raw = bd.mkvmerge_raw;
    const rawTracks = raw.tracks || [];
    const container = raw.container?.properties || {};

    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`  MKVMERGE -J RAW (sin heurísticas)`);
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`MPLS: ${raw.file_name || '—'}`);
    lines.push(`Duración raw: ${container.playlist_duration || 0} (${(container.playlist_duration / 1e9)?.toFixed(1) || '?'}s)`);
    lines.push(`Tamaño playlist: ${container.playlist_size || 0} bytes`);
    lines.push(`Capítulos raw: ${container.playlist_chapters || 0}`);
    lines.push('');

    rawTracks.forEach((t, i) => {
      const p = t.properties || {};
      const parts = [`id=${t.id}`, `type=${t.type}`, `codec="${t.codec}"`];
      if (p.language) parts.push(`lang=${p.language}`);
      if (p.pixel_dimensions) parts.push(`res=${p.pixel_dimensions}`);
      if (p.audio_channels) parts.push(`ch=${p.audio_channels}`);
      if (p.audio_sampling_frequency) parts.push(`freq=${p.audio_sampling_frequency}`);
      if (p.track_name) parts.push(`name="${p.track_name}"`);
      if (p.default_track) parts.push(`default=true`);
      if (p.forced_track) parts.push(`forced=true`);
      if (p.multiplexed_tracks) parts.push(`mux=[${p.multiplexed_tracks}]`);
      lines.push(`  ${i+1}. ${parts.join(' | ')}`);
    });
    lines.push('');
  }

  // ── SECCIÓN 2: Post-heurística ──
  if (bd) {
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`  POST-HEURÍSTICA (resultado del análisis)`);
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`Duración: ${bd.duration_seconds?.toFixed(1)}s | VO: ${bd.vo_language} | MPLS: ${bd.main_mpls}`);
    lines.push(`FEL: ${bd.has_fel} | Razón: ${bd.fel_reason}`);
    lines.push('');

    lines.push(`── Vídeo (${bd.video_tracks?.length || 0} pistas) ──`);
    (bd.video_tracks || []).forEach((t, i) => {
      lines.push(`  #${i+1} codec="${t.codec}" | desc="${t.description}" | EL=${t.is_el} | bitrate=${t.bitrate_kbps}`);
    });
    lines.push('');

    lines.push(`── Audio adaptado (${bd.audio_tracks?.length || 0} pistas) ──`);
    (bd.audio_tracks || []).forEach((t, i) => {
      const parts = [`codec="${t.codec}"`, `lang="${t.language}"`, `desc="${t.description}"`];
      if (t.bitrate_kbps) parts.push(`bitrate=${t.bitrate_kbps.toLocaleString()} kbps`);
      if (t.format_commercial) parts.push(`format="${t.format_commercial}"`);
      if (t.compression_mode) parts.push(`${t.compression_mode}`);
      lines.push(`  #${i+1} ${parts.join(' | ')}`);
    });
    lines.push('');

    lines.push(`── Subtítulos adaptado (${bd.subtitle_tracks?.length || 0} pistas) ──`);
    (bd.subtitle_tracks || []).forEach((t, i) => {
      const pkts = t.packet_count || 0;
      let tipo, extra;
      if (pkts > 0) {
        tipo = pkts < 500 ? 'FORZADO' : 'COMPLETO';
        extra = `packets=${pkts}`;
      } else {
        tipo = t.bitrate_kbps < 3 ? 'FORZADO (patrón)' : 'COMPLETO (patrón)';
        extra = `bitrate_sintético=${t.bitrate_kbps}`;
      }
      lines.push(`  #${i+1} lang="${t.language}" | ${extra} → ${tipo}`);
    });
    lines.push('');
  }

  // ── SECCIÓN 2b: MediaInfo (datos extendidos) ──
  if (bd?.mediainfo_result) {
    const mi = bd.mediainfo_result;
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`  MEDIAINFO (${mi.source_path || bd.main_m2ts || '—'})`);
    lines.push(`═══════════════════════════════════════════════`);
    if (mi.source_size_bytes) lines.push(`Tamaño m2ts: ${_fmtBytes(mi.source_size_bytes)}`);
    (mi.tracks || []).forEach((t, i) => {
      const parts = [`type=${t.track_type}`];
      if (t.bitrate_kbps) parts.push(`bitrate=${t.bitrate_kbps.toLocaleString()} kbps`);
      if (t.format_commercial) parts.push(`"${t.format_commercial}"`);
      if (t.channel_layout) parts.push(`layout="${t.channel_layout}"`);
      if (t.compression_mode) parts.push(`${t.compression_mode}`);
      if (t.bit_depth) parts.push(`${t.bit_depth}-bit`);
      if (t.color_primaries) parts.push(`${t.color_primaries}`);
      if (t.transfer_characteristics) parts.push(`${t.transfer_characteristics}`);
      if (t.resolution) parts.push(`res=${t.resolution}`);
      lines.push(`  ${i+1}. ${parts.join(' | ')}`);
    });
    lines.push('');
  }

  // ── SECCIÓN 2c: Dolby Vision (dovi_tool) ──
  const mainV = bd?.video_tracks?.find(t => !t.is_el);
  if (mainV?.dovi) {
    const d = mainV.dovi;
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`  DOLBY VISION (dovi_tool RPU analysis)`);
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`Profile: ${d.profile}${d.el_type ? ` (${d.el_type})` : ''}`);
    lines.push(`CM version: ${d.cm_version}`);
    lines.push(`Metadata: L1=${d.has_l1} L2=${d.has_l2} L5=${d.has_l5} L6=${d.has_l6}`);
    lines.push(`Scenes: ${d.scene_count} | Frames: ${d.frame_count}`);
    if (d.raw_summary) {
      lines.push('');
      lines.push(d.raw_summary.trim());
    }
    lines.push('');
  }

  // ── HDR10 metadata ──
  if (mainV?.hdr) {
    const h = mainV.hdr;
    if (h.hdr_format || h.max_cll || h.mastering_display_luminance) {
      lines.push(`═══════════════════════════════════════════════`);
      lines.push(`  HDR METADATA`);
      lines.push(`═══════════════════════════════════════════════`);
      if (h.hdr_format) lines.push(`Formato: ${h.hdr_format}`);
      if (h.color_primaries) lines.push(`Color primaries: ${h.color_primaries}`);
      if (h.transfer_characteristics) lines.push(`Transfer: ${h.transfer_characteristics}`);
      if (h.bit_depth) lines.push(`Bit depth: ${h.bit_depth}`);
      if (h.max_cll != null) lines.push(`MaxCLL: ${h.max_cll} cd/m²`);
      if (h.max_fall != null) lines.push(`MaxFALL: ${h.max_fall} cd/m²`);
      if (h.mastering_display_luminance) lines.push(`Mastering display: ${h.mastering_display_luminance}`);
      lines.push('');
    }
  }

  // ── SECCIÓN 3: Resultado de reglas (Fase B) ──
  lines.push(`═══════════════════════════════════════════════`);
  lines.push(`  RESULTADO DE REGLAS (Fase B)`);
  lines.push(`═══════════════════════════════════════════════`);

  lines.push(`── Pistas incluidas (${s.included_tracks?.length || 0}) ──`);
  (s.included_tracks || []).forEach((t, i) => {
    const raw = t.raw || {};
    if (t.track_type === 'audio') {
      lines.push(`  ${i+1}. [AUDIO] label="${t.label}" | default=${t.flag_default} | raw: lang="${raw.language}" codec="${raw.codec}" desc="${raw.description}"`);
      lines.push(`         razón: ${t.selection_reason || '—'}`);
    } else {
      const pktInfo = raw.packet_count ? ` packets=${raw.packet_count}` : ` bitrate=${raw.bitrate_kbps}`;
      lines.push(`  ${i+1}. [SUB] label="${t.label}" | tipo=${t.subtitle_type} | default=${t.flag_default} | forced=${t.flag_forced} | raw: lang="${raw.language}"${pktInfo}`);
      lines.push(`         razón: ${t.selection_reason || '—'}`);
    }
  });
  lines.push('');

  lines.push(`── Pistas descartadas (${s.discarded_tracks?.length || 0}) ──`);
  (s.discarded_tracks || []).forEach((t, i) => {
    const raw = t.raw || {};
    if (t.track_type === 'audio') {
      const br = raw.bitrate_kbps ? ` | bitrate=${raw.bitrate_kbps.toLocaleString()} kbps` : '';
      const fc = raw.format_commercial ? ` | format="${raw.format_commercial}"` : '';
      lines.push(`  ${i+1}. [AUDIO] lang="${raw.language}" codec="${raw.codec}" desc="${raw.description}"${br}${fc}`);
    } else {
      const pktInfo = raw.packet_count ? `packets=${raw.packet_count}` : `bitrate=${raw.bitrate_kbps}`;
      lines.push(`  ${i+1}. [SUB] lang="${raw.language}" ${pktInfo}`);
    }
    lines.push(`         razón: ${t.discard_reason}`);
  });
  lines.push('');

  lines.push(`── Capítulos (${s.chapters?.length || 0}) ──`);
  (s.chapters || []).forEach(ch => {
    lines.push(`  ${ch.number}. ${ch.timestamp} — "${ch.name}"${ch.name_custom ? ' (editado)' : ''}`);
  });

  // ── SECCIÓN 4: Log del modal de análisis (capturado al crear) ──
  if (s.analysis_log && s.analysis_log.length) {
    lines.push('');
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`  LOG DE ANÁLISIS (Fase A — capturado al crear)`);
    lines.push(`═══════════════════════════════════════════════`);
    s.analysis_log.forEach(l => lines.push(l));
  }

  const text = lines.join('\n');
  document.getElementById('raw-analysis-content').textContent = text;
  openModal('raw-analysis-modal');
}

/** Copia los datos de análisis al portapapeles. */
async function _copyRawAnalysis() {
  const pre = document.getElementById('raw-analysis-content');
  if (!pre) return;
  const ok = await _copyTextToClipboardWithFallback(pre.textContent);
  showToast(ok ? 'Datos copiados al portapapeles.' : 'No se pudo copiar al portapapeles', ok ? 'success' : 'error');
}

/**
 * Tab 2: vista de diagnóstico del análisis del MKV — paridad con "Datos ISO".
 * Muestra el log capturado durante el análisis + tracks crudos de mkvmerge -J +
 * resumen de MediaInfo + DV. Reusa el modal raw-analysis-modal.
 */
function showRawMkvData() {
  if (!mkvProject || !mkvProject.analysis) return;
  const a = mkvProject.analysis;
  const lines = [];

  lines.push(`═══════════════════════════════════════════════`);
  lines.push(`  DATOS DE ANÁLISIS DEL MKV`);
  lines.push(`═══════════════════════════════════════════════`);
  lines.push(`Fichero: ${a.file_name || '—'}`);
  lines.push(`Ruta: ${a.file_path || '—'}`);
  lines.push(`Tamaño: ${_fmtBytes(a.file_size_bytes || 0)}`);
  lines.push(`Duración: ${_fmtDuration(a.duration_seconds || 0)}`);
  if (a.title) lines.push(`Título contenedor: ${a.title}`);
  lines.push(`FEL: ${!!a.has_fel}`);
  lines.push('');

  // ── PISTAS (mkvmerge -J → MkvTrackInfo) ──
  lines.push(`═══════════════════════════════════════════════`);
  lines.push(`  PISTAS (${a.tracks?.length || 0})`);
  lines.push(`═══════════════════════════════════════════════`);
  (a.tracks || []).forEach(t => {
    const parts = [`id=${t.id}`, `type=${t.type}`, `codec="${t.codec}"`];
    if (t.language) parts.push(`lang=${t.language}`);
    if (t.name) parts.push(`name="${t.name}"`);
    if (t.pixel_dimensions) parts.push(`res=${t.pixel_dimensions}`);
    if (t.channels) parts.push(`ch=${t.channels}`);
    if (t.bitrate_kbps) parts.push(`bitrate=${t.bitrate_kbps}kbps`);
    if (t.format_commercial) parts.push(`fmt="${t.format_commercial}"`);
    if (t.flag_default) parts.push('default=true');
    if (t.flag_forced) parts.push('forced=true');
    if (t.packet_count) parts.push(`packets=${t.packet_count}`);
    lines.push(`  ${parts.join(' | ')}`);
  });
  lines.push('');

  // ── HDR ──
  if (a.hdr) {
    const h = a.hdr;
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`  HDR METADATA`);
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`Formato: ${h.hdr_format || '—'}`);
    if (h.color_primaries) lines.push(`Color primaries: ${h.color_primaries}`);
    if (h.transfer_characteristics) lines.push(`Transfer: ${h.transfer_characteristics}`);
    if (h.bit_depth) lines.push(`Bit depth: ${h.bit_depth}`);
    if (h.max_cll != null) lines.push(`MaxCLL: ${h.max_cll} cd/m²`);
    if (h.max_fall != null) lines.push(`MaxFALL: ${h.max_fall} cd/m²`);
    if (h.mastering_display_luminance) lines.push(`Mastering: ${h.mastering_display_luminance}`);
    lines.push('');
  }

  // ── DOLBY VISION ──
  if (a.dovi) {
    const d = a.dovi;
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`  DOLBY VISION`);
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`Profile: ${d.profile}${d.el_type ? ` (${d.el_type})` : ''}`);
    lines.push(`CM version: ${d.cm_version || '—'}`);
    if (d.frame_count) lines.push(`Frames: ${d.frame_count}`);
    if (d.scene_count) lines.push(`Scenes: ${d.scene_count}`);
    const lvls = [];
    ['l1', 'l2', 'l3', 'l4', 'l5', 'l6', 'l8', 'l9', 'l10', 'l11', 'l254'].forEach(k => {
      if (d[`has_${k}`]) lvls.push(k.toUpperCase());
    });
    if (lvls.length) lines.push(`Niveles: ${lvls.join(' · ')}`);
    if (d.raw_summary) {
      lines.push('');
      lines.push(d.raw_summary.trim());
    }
    lines.push('');
  }

  // ── CAPÍTULOS ──
  if (a.chapters?.length) {
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`  CAPÍTULOS (${a.chapters.length})`);
    lines.push(`═══════════════════════════════════════════════`);
    a.chapters.forEach(ch => {
      lines.push(`  ${ch.number}. ${ch.timestamp} — "${ch.name}"${ch.name_custom ? ' (editado)' : ''}`);
    });
    lines.push('');
  }

  // ── LOG DE ANÁLISIS (paralelo al de Tab 1) ──
  if (a.analysis_log && a.analysis_log.length) {
    lines.push(`═══════════════════════════════════════════════`);
    lines.push(`  LOG DE ANÁLISIS (capturado al abrir MKV)`);
    lines.push(`═══════════════════════════════════════════════`);
    a.analysis_log.forEach(l => lines.push(l));
  }

  const text = lines.join('\n');
  document.getElementById('raw-analysis-content').textContent = text;
  openModal('raw-analysis-modal');
}


// Extrae canales (7.1 / 5.1 / 2.0…) del primer campo de description.
function _extractAudioChannels(description) {
  const m = (description || '').match(/(\d+\.\d+)/);
  return m ? m[1] : '';
}

// Identifica el codec normalizado (mismo mapeo que phase_b._codec_key).
// Devuelve 'truehd_atmos', 'truehd', 'ddplus_atmos', 'ddplus',
// 'dts_hd_ma', 'dts', 'dd' o ''.
function _codecKeyFromRaw(raw) {
  const codec = (raw.codec || '').toLowerCase();
  const desc  = (raw.description || '').toLowerCase();
  const fc    = (raw.format_commercial || '').toLowerCase();
  const hasAtmos = fc.includes('atmos') || codec.includes('atmos') || desc.includes('atmos');

  if (fc) {
    if (fc.includes('truehd')) return hasAtmos ? 'truehd_atmos' : 'truehd';
    if (fc.includes('digital plus') || fc.includes('e-ac-3'))
      return hasAtmos ? 'ddplus_atmos' : 'ddplus';
    if (fc.includes('dts-hd master') || fc.includes('dts-hd ma')) return 'dts_hd_ma';
    if (fc.includes('dts')) return 'dts';
    if (fc.includes('dolby digital') && !fc.includes('plus')) return 'dd';
  }
  if (codec.includes('truehd')) return hasAtmos ? 'truehd_atmos' : 'truehd';
  if (codec.includes('digital plus')) return hasAtmos ? 'ddplus_atmos' : 'ddplus';
  if (codec.includes('dts-hd master') ||
      (codec.includes('dts') && codec.includes('hd') && codec.includes('master')))
    return 'dts_hd_ma';
  if (codec.includes('dts') && !codec.includes('hd')) return 'dts';
  if (codec.includes('dolby digital') && !codec.includes('plus')) return 'dd';
  return '';
}

// Construye el codec literal (ej. "DD+ Atmos 7.1", "TrueHD Atmos 7.1 (DCP 9.1.6)")
// Replica phase_b._codec_literal para pistas recuperadas manualmente.
function _buildAudioCodecLiteral(raw, audioDcp) {
  const channels = _extractAudioChannels(raw.description);
  const key = _codecKeyFromRaw(raw);
  const map = {
    truehd_atmos: `TrueHD Atmos ${channels}`.trim(),
    truehd:       `TrueHD ${channels}`.trim(),
    ddplus_atmos: `DD+ Atmos ${channels}`.trim(),
    ddplus:       `DD+ ${channels}`.trim(),
    dts_hd_ma:    `DTS-HD MA ${channels}`.trim(),
    dts:          `DTS ${channels}`.trim(),
    dd:           `DD ${channels}`.trim(),
  };
  let lit = map[key] || `${raw.codec || ''} ${channels}`.trim();
  // Sufijo DCP solo en TrueHD Atmos Castellano
  if (audioDcp && key === 'truehd_atmos' && raw.language === 'Spanish') {
    lit += ' (DCP 9.1.6)';
  }
  return lit;
}

function recoverTrack(idx) {
  const track = currentSession.discarded_tracks.splice(idx, 1)[0];
  const raw   = track.raw || {};
  const isAudio = track.track_type === 'audio';
  const langLit = langLiteral(raw.language) || '';

  // Para subs: usar el tipo inferido por Fase B (forced/complete). Antes
  // siempre se asumía 'complete', así que forzados de idiomas no-target
  // (Tailandés Forzados, Checo Forzados, etc.) se etiquetaban erróneamente
  // como Completos al recuperarlos. Ahora preservamos la clasificación.
  // Fallback a 'complete' si el campo no está (sesiones legacy o tracks
  // sin packet_count fiable).
  const inferredSubType = track.inferred_subtitle_type || 'complete';
  const isForcedSub = !isAudio && inferredSubType === 'forced';
  // flag_forced de Matroska solo a Castellano (spec §5.2). El resto de
  // forzados (VO, Inglés, idiomas no-target recuperados) llevan
  // subtitle_type='forced' + label "X Forzados (PGS)" para reflejar
  // el contenido, pero flag_forced=false para mantener una sola pista
  // con flag forced=yes en el MKV final. Sin esto el reproductor podía
  // solapar varios forzados al cambiar de audio.
  const isCastellanoSub = !isAudio && (raw.language || '').toLowerCase() === 'spanish';
  const setForcedFlag = isForcedSub && isCastellanoSub;

  let codecLit, fullLabel;
  if (isAudio) {
    codecLit = _buildAudioCodecLiteral(raw, !!currentSession.audio_dcp);
    fullLabel = `${langLit} ${codecLit}`.trim() || 'Pista recuperada';
  } else {
    codecLit = isForcedSub ? 'Forzados (PGS)' : 'Completos (PGS)';
    fullLabel = `${langLit} ${codecLit}`.trim() || 'Pista recuperada';
  }

  const recovered = {
    track_type: track.track_type,
    position: 0,  // se renumera abajo
    raw: track.raw,
    label: fullLabel,
    flag_default: false,
    flag_forced: setForcedFlag,
    selection_reason: 'Recuperada manualmente por el usuario'
      + (!isAudio ? ` (tipo inferido por Fase B: ${inferredSubType}${isForcedSub && !setForcedFlag ? ' — sin flag forced de Matroska porque no es Castellano' : ''})` : ''),
    language_literal: langLit,
    codec_literal: codecLit,
    subtitle_type: isForcedSub ? 'forced' : 'complete',
  };

  // Insertar agrupando por tipo: audio recuperado va tras el último audio;
  // subtítulo recuperado va al final. Así el orden del MKV final es
  // [audio…][subs…] coherente, sin subs intercalados entre audios.
  const inc = currentSession.included_tracks;
  let insertAt;
  if (isAudio) {
    // Último índice donde hay audio; si no hay ninguno, va al principio (0)
    let lastAudioIdx = -1;
    for (let i = 0; i < inc.length; i++) {
      if (inc[i].track_type === 'audio') lastAudioIdx = i;
    }
    insertAt = lastAudioIdx + 1;  // tras el último audio (o 0 si no hay)
  } else {
    insertAt = inc.length;  // al final del todo (tras subs existentes)
  }
  inc.splice(insertAt, 0, recovered);

  // Red de seguridad: normaliza [audios…][subs…] por si el array venía
  // desordenado (p.ej. de sesiones guardadas con la versión antigua que
  // hacía push al final sin agrupar).
  _enforceTrackGrouping(inc);

  // Renumerar posiciones tras la inserción
  inc.forEach((t, i) => { t.position = i; });

  renderIncludedTracks(inc);
  renderDiscardedTracks(currentSession.discarded_tracks);
  markProjectDirty();
}

// ═══════════════════════════════════════════════════════════════════
//  CAPÍTULOS
// ═══════════════════════════════════════════════════════════════════

/**
 * Renderiza la sección completa de capítulos: banner auto-generados,
 * marcas en la timeline y la tabla de lista editable.
 *
 * @param {Object[]} chapters      - Array de Chapter con number, timestamp y name.
 * @param {boolean}  autoGenerated - True si los capítulos fueron auto-generados en Fase B.
 * @param {string}   autoReason    - Razón del auto-generado para mostrar en el banner.
 */
/** Flag por proyecto: true cuando el usuario ha modificado capítulos desde el último render/reset. */
const _chaptersModified = new Map();

function renderChapters(chapters, autoGenerated, autoReason) {
  const banner   = E('chapters-auto-banner');
  const text     = E('chapters-auto-text');
  const icon     = E('chapters-auto-icon');
  const resetBtn = E('chapters-reset-btn');

  if (autoReason) {
    if (text) text.textContent = autoReason;
    if (icon) icon.textContent = autoGenerated ? '⚠️' : '💿';
    if (banner) {
      banner.className = autoGenerated ? 'banner warning' : 'banner info';
      banner.style.display = 'flex';
    }
  } else {
    if (banner) banner.style.display = 'none';
  }

  // Botón restaurar: visible solo con capítulos del disco + editados por el usuario
  const modified = _chaptersModified.get(activeSubTabId) || false;
  if (resetBtn) resetBtn.style.display = (!autoGenerated && modified) ? '' : 'none';

  // Botón nombres genéricos: visible solo si algún capítulo tiene nombre custom
  const genericBtn = E('chapters-generic-btn');
  const hasCustomNames = chapters.some(ch => ch.name_custom);
  if (genericBtn) genericBtn.style.display = hasCustomNames ? '' : 'none';

  renderChapterMarks(chapters);
  renderChapterList(chapters);
}

/** Marca que los capítulos del proyecto activo han sido editados. */
function _markChaptersModified() {
  _chaptersModified.set(activeSubTabId, true);
  const resetBtn = E('chapters-reset-btn');
  const project = openProjects.find(p => p.subTabId === activeSubTabId);
  if (resetBtn && project && !project.session?.chapters_auto_generated) {
    resetBtn.style.display = '';
  }
}

/**
 * Dibuja ticks de escala temporal sobre el timeline.
 * Elige el intervalo de tick más adecuado según la duración total.
 * @param {HTMLElement} container - El elemento .timeline-marks
 * @param {number} duration - Duración total en segundos
 */
function renderTimelineTicks(container, duration) {
  // Elegir intervalo de tick: cada 5, 10, 15, 20 o 30 min según duración
  const candidates = [5, 10, 15, 20, 30].map(m => m * 60);
  const targetTicks = 8;
  const interval = candidates.find(i => (duration / i) <= targetTicks) || candidates[candidates.length - 1];

  for (let t = interval; t < duration; t += interval) {
    const pct = (t / duration) * 100;
    const mins = Math.round(t / 60);
    const label = mins >= 60 ? `${Math.floor(mins/60)}h${mins%60 > 0 ? String(mins%60).padStart(2,'0')+'m' : ''}` : `${mins}m`;

    const tick = document.createElement('div');
    tick.className = 'timeline-tick';
    tick.style.left = `${pct}%`;
    container.appendChild(tick);

    const lbl = document.createElement('div');
    lbl.className = 'timeline-tick-label';
    lbl.style.left = `${pct}%`;
    lbl.textContent = label;
    container.appendChild(lbl);
  }
}

/**
 * Dibuja las marcas de capítulo sobre la barra de timeline proporcional.
 * @param {Object[]} chapters
 */
function renderChapterMarks(chapters) {
  const marks    = E('timeline-marks');
  const duration = currentSession?.bdinfo_result?.duration_seconds || 0;
  if (!marks) return;
  marks.innerHTML = '';
  if (!duration) return;

  renderTimelineTicks(marks, duration);

  chapters.forEach((ch, idx) => {
    const secs  = tsToSecs(ch.timestamp);
    const pct   = (secs / duration) * 100;
    const mark  = document.createElement('div');
    mark.className = 'chapter-mark';
    mark.style.left = `${pct}%`;
    mark.dataset.tooltip = `${ch.name}\n${ch.timestamp}\nArrastra para mover · clic para seleccionar`;
    mark.onclick    = (e) => { e.stopPropagation(); highlightChapter(idx); };
    mark.onmousedown = (e) => { e.preventDefault(); e.stopPropagation(); startChapterDrag(e, mark, idx); };
    marks.appendChild(mark);
  });
}

/**
 * Inicia el arrastre de una marca de capítulo a lo largo del timeline.
 * Actualiza la posición visual en tiempo real y confirma el timestamp al soltar.
 * @param {MouseEvent}  e       - Evento mousedown original.
 * @param {HTMLElement} markEl  - El elemento .chapter-mark que se arrastra.
 * @param {number}      idx     - Índice del capítulo en currentSession.chapters.
 */
function startChapterDrag(_e, markEl, idx) {
  const duration = currentSession?.bdinfo_result?.duration_seconds || 0;
  if (!duration) return;
  const wrap = E('chapter-timeline-wrap');
  let dragged = false;

  markEl.classList.add('selected');
  document.body.style.cursor = 'grabbing';

  // Tooltip dedicado al drag — se crea dentro de .timeline-marks (mismo sistema de coords que el mark)
  const marksEl = E('timeline-marks');
  const dragTip = document.createElement('div');
  dragTip.className = 'chapter-drag-tip';
  dragTip.style.display = 'none';
  marksEl?.appendChild(dragTip);

  const onMove = (ev) => {
    dragged = true;
    const rect = wrap.getBoundingClientRect();
    const pct  = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
    const secs = pct * duration;
    const ts   = secsToTs(secs);
    markEl.style.left = `${pct * 100}%`;
    dragTip.style.left = `${pct * 100}%`;
    dragTip.style.display = '';
    dragTip.textContent = ts;
    currentSession.chapters[idx].timestamp = ts;
  };

  const onUp = () => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    document.body.style.cursor = '';
    dragTip.remove();
    if (dragged) {
      const chapters = currentSession.chapters;
      renumberChapters(chapters);
      _markChaptersModified();
      renderChapters(currentSession.chapters, currentSession.chapters_auto_generated, currentSession.chapters_auto_reason);
      markProjectDirty();
    } else {
      markEl.classList.remove('selected');
    }
  };

  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

/**
 * Renderiza la tabla editable de capítulos (número, timestamp, nombre, borrar).
 * @param {Object[]} chapters
 */
function renderChapterList(chapters) {
  const container = E('chapters-list');
  container.innerHTML = '';
  chapters.forEach((ch, idx) => {
    const row = document.createElement('div');
    row.className = 'chapter-row';
    row.id = `ch-row-${idx}`;
    row.innerHTML = `
      <span class="chapter-num">${String(ch.number).padStart(2,'0')}</span>
      <input type="text" value="${escHtml(ch.timestamp)}" style="font-family:'SF Mono','Menlo',monospace;font-size:11px"
        onchange="onChapterTimestampChange(${idx}, this.value)"
        data-tooltip="Timestamp de inicio del capítulo.\nFormato HH:MM:SS.mmm">
      <input type="text" value="${escHtml(ch.name)}"
        onchange="onChapterNameChange(${idx}, this.value)"
        data-tooltip="Nombre del capítulo tal como aparecerá en el reproductor.">
      <button class="btn btn-icon" onclick="deleteChapter(${idx})"
        data-tooltip="Eliminar este capítulo.">✕</button>`;
    container.appendChild(row);
  });
}

/**
 * Resalta la marca del capítulo en la timeline y hace scroll al row correspondiente.
 * @param {number} idx - Índice del capítulo en el array chapters.
 */
function highlightChapter(idx) {
  document.querySelectorAll('.chapter-mark').forEach((m, i) => {
    m.classList.toggle('selected', i === idx);
  });
  document.getElementById(`ch-row-${idx}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/**
 * Añade un capítulo en la posición del click sobre la timeline.
 * @param {MouseEvent} e
 */
function onTimelineClick(e) {
  const duration = currentSession?.bdinfo_result?.duration_seconds || 0;
  if (!duration) return;
  const wrap = E('chapter-timeline-wrap');
  const rect = wrap.getBoundingClientRect();
  const pct  = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const secs = pct * duration;
  const chapters = currentSession.chapters || [];
  chapters.push({ number: 0, timestamp: secsToTs(secs), name: '', name_custom: false });
  renumberChapters(chapters);
  currentSession.chapters = chapters;
  _markChaptersModified();
  renderChapters(currentSession.chapters, currentSession.chapters_auto_generated, currentSession.chapters_auto_reason);
  markProjectDirty();
}

/**
 * Muestra el cursor flotante con el timestamp bajo el puntero en la timeline.
 * @param {MouseEvent} e
 */
function onTimelineHover(e) {
  const duration = currentSession?.bdinfo_result?.duration_seconds || 0;
  if (!duration) return;
  const wrap  = E('chapter-timeline-wrap');
  if (!wrap) return;
  const rect  = wrap.getBoundingClientRect();
  const pct   = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const secs  = pct * duration;
  const label = E('timeline-cursor');
  label.style.display = '';
  label.style.left    = `${e.clientX - rect.left}px`;
  label.textContent   = secsToTs(secs);
}

function onTimelineLeave() {
  const el = E('timeline-cursor');
  if (el) el.style.display = 'none';
}

function deleteChapter(idx) {
  currentSession.chapters.splice(idx, 1);
  renumberChapters(currentSession.chapters);
  _markChaptersModified();
  renderChapters(currentSession.chapters, currentSession.chapters_auto_generated, currentSession.chapters_auto_reason);
  markProjectDirty();
}

function onChapterTimestampChange(idx, value) {
  currentSession.chapters[idx].timestamp = value;
  renumberChapters(currentSession.chapters);
  _markChaptersModified();
  renderChapters(currentSession.chapters, currentSession.chapters_auto_generated, currentSession.chapters_auto_reason);
  markProjectDirty();
}


function onChapterNameChange(idx, value) {
  const ch = currentSession.chapters[idx];
  ch.name = value;
  ch.name_custom = value.trim() !== '';
  _markChaptersModified();
  // Actualizar tooltip del mark inmediatamente
  const markEls = document.querySelectorAll('.chapter-mark');
  if (markEls[idx]) {
    markEls[idx].dataset.tooltip = `${ch.name}\n${ch.timestamp}\nArrastra para mover · clic para seleccionar`;
  }
  // Re-evaluar botones del banner (nombres genéricos, restaurar)
  const resetBtn = E('chapters-reset-btn');
  const genericBtn = E('chapters-generic-btn');
  if (resetBtn && !currentSession.chapters_auto_generated) resetBtn.style.display = '';
  if (genericBtn) genericBtn.style.display = currentSession.chapters.some(c => c.name_custom) ? '' : 'none';
  markProjectDirty();
}

/**
 * Reordena los capítulos cronológicamente, reasigna números correlativos
 * y actualiza los nombres auto-generados (respetando los editados manualmente).
 * @param {Object[]} chapters - Array de Chapter a reordenar in-place.
 */
function renumberChapters(chapters) {
  chapters.sort((a, b) => tsToSecs(a.timestamp) - tsToSecs(b.timestamp));
  chapters.forEach((ch, i) => {
    ch.number = i + 1;
    if (!ch.name_custom) {
      ch.name = `Capítulo ${String(ch.number).padStart(2, '0')}`;
    }
  });
}

/**
 * Restaura los capítulos originales del disco re-extrayéndolos del MPLS.
 * Descarta cualquier edición manual del usuario.
 */
/**
 * Reemplaza todos los nombres de capítulo por genéricos en español.
 * Mantiene timestamps y posiciones intactos.
 */
function setGenericChapterNames() {
  if (!currentSession?.chapters) return;

  currentSession.chapters.forEach((ch, i) => {
    ch.name = `Capítulo ${String(i + 1).padStart(2, '0')}`;
    ch.name_custom = false;
  });

  _markChaptersModified();
  renderChapters(currentSession.chapters, currentSession.chapters_auto_generated, currentSession.chapters_auto_reason);
  markProjectDirty();
  showToast('Nombres de capítulo reemplazados por genéricos.', 'info');
}


async function resetChaptersFromDisc() {
  if (!currentSession) return;
  const sessionId = currentSession.id;

  showConfirm(
    '¿Restaurar capítulos del disco?',
    'Se descartarán todas las ediciones manuales (nombres, posiciones, capítulos añadidos/eliminados) y se volverán a extraer los capítulos originales del ISO.',
    async () => {
      const toastId = showToast('⏳ Montando ISO y extrayendo capítulos…', 'info', 0);
      const data = await apiFetch(`/api/sessions/${sessionId}/reset-chapters`, { method: 'POST' });
      removeToast(toastId);
      if (!data) return;

      // Actualizar sesión en proyecto abierto y en currentSession
      const project = openProjects.find(p => p.sessionId === sessionId);
      if (project) project.session = data;
      currentSession = data;

      _chaptersModified.set(activeSubTabId, false);
      renderChapters(data.chapters, data.chapters_auto_generated, data.chapters_auto_reason);
      showToast(`${data.chapters.length} capítulos restaurados del disco.`, 'success');
    },
  );
}


// ═══════════════════════════════════════════════════════════════════
//  VARIABLES GLOBALES (FEL / DCP / Nombre MKV)
// ═══════════════════════════════════════════════════════════════════

/**
 * Maneja el cambio del toggle FEL. Actualiza la sesión en memoria y
 * regenera el nombre del MKV si no fue editado manualmente.
 */
function onFelChange() {
  const project = getActiveProject();
  markProjectDirty();
  const val = E('toggle-fel')?.checked;
  if (!currentSession) return;
  currentSession.has_fel = val;
  setText('fel-value', val ? 'FEL' : 'MEL');
  E('global-fel').className = `global-toggle-item${val ? ' active-fel' : ''}`;
  if (!project?.mkvNameWasManual) recalcMkvNameLocal();
}

function onDcpChange() {
  const project = getActiveProject();
  markProjectDirty();
  const val = E('toggle-dcp')?.checked;
  if (!currentSession) return;
  currentSession.audio_dcp = val;
  setText('dcp-value', val ? 'Activo' : 'No detectado');
  E('global-dcp').className = `global-toggle-item${val ? ' active-dcp' : ''}`;
  recalcAudioLabelsForDcp(val);
  if (!project?.mkvNameWasManual) recalcMkvNameLocal();
}

/**
 * Añade o quita el sufijo "(DCP 9.1.6)" en los labels de pistas TrueHD Atmos
 * incluidas, según el estado del toggle DCP (spec §5.1.4).
 * Solo afecta a pistas cuyo codec raw contiene "TrueHD" y "Atmos".
 */
function recalcAudioLabelsForDcp(enabled) {
  if (!currentSession) return;
  let changed = false;
  currentSession.included_tracks.forEach(t => {
    if (t.track_type !== 'audio') return;
    const raw = t.raw || {};
    if ((raw.language || '').toLowerCase() !== 'spanish') return;
    const codec = (raw.codec || '').toLowerCase();
    if (!codec.includes('truehd') || !codec.includes('atmos')) return;
    const base = t.label.replace(/ \(DCP 9\.1\.6\)$/, '');
    t.label = enabled ? `${base} (DCP 9.1.6)` : base;
    changed = true;
  });
  if (changed) renderIncludedTracks(currentSession.included_tracks);
}

function onMkvNameInput() {
  const project = getActiveProject();
  if (!currentSession || !project) return;
  project.mkvNameWasManual = true;
  currentSession.mkv_name = E('mkv-name-input')?.value || '';
  currentSession.mkv_name_manual = true;
  show('mkv-name-manual-notice');
  markProjectDirty();
}

/**
 * Revierte el nombre del MKV al valor calculado automáticamente por el backend.
 */
async function revertMkvName() {
  const project = getActiveProject();
  if (!currentSession || !project) return;
  project.mkvNameWasManual = false;
  currentSession.mkv_name_manual = false;
  const data = await apiFetch(`/api/sessions/${currentSession.id}/recalculate-name`, { method: 'POST' });
  if (data) {
    currentSession.mkv_name = data.mkv_name;
    const inp = E('mkv-name-input');
    if (inp) inp.value = data.mkv_name;
    hide('mkv-name-manual-notice');
  }
}

/**
 * Recalcula el nombre del MKV localmente (sin llamar al backend) cuando
 * cambia el toggle FEL o DCP y el nombre no fue editado manualmente.
 */
function recalcMkvNameLocal() {
  const project = getActiveProject();
  const iso  = currentSession.iso_path || '';
  const stem = iso.replace(/\\/g, '/').split('/').pop().replace(/\.iso$/i, '');
  const m    = stem.match(/^(.+?)\s*\((\d{4})\)/);
  const title = m ? m[1].trim() : stem;
  const year  = m ? m[2] : '0000';
  let name = `${title} (${year})`;
  if (currentSession.has_fel)   name += ' [DV FEL]';
  if (currentSession.audio_dcp) name += ' [Audio DCP]';
  name += '.mkv';
  currentSession.mkv_name = name;
  const inp = E('mkv-name-input');
  if (inp) inp.value = name;
  // Actualizar también el título del subtab
  if (project) {
    project.name = name.replace(/\.mkv$/i, '');
    renderProjectSubTabButton(project);
  }
}

// ═══════════════════════════════════════════════════════════════════
//  GUARDAR / EJECUTAR
// ═══════════════════════════════════════════════════════════════════

/**
 * Persiste el estado actual de la sesión via PUT /api/sessions/{id}.
 */
async function saveSession() {
  if (!currentSession) return;
  const data = await apiFetch(`/api/sessions/${currentSession.id}`, {
    method: 'PUT',
    body: JSON.stringify({
      has_fel: currentSession.has_fel,
      audio_dcp: currentSession.audio_dcp,
      mkv_name: currentSession.mkv_name,
      mkv_name_manual: currentSession.mkv_name_manual || false,
      included_tracks: currentSession.included_tracks,
      discarded_tracks: currentSession.discarded_tracks,
      chapters: currentSession.chapters,
    }),
  });
  if (data) {
    showToast('Sesión guardada.', 'success');
    const project = getActiveProject();
    if (project) clearProjectDirty(project.id);
    // Actualizar cache local y re-renderizar sidebar con sort+filter
    const cached = _sessionsCache.find(s => s.id === currentSession.id);
    if (cached) cached.updated_at = data.updated_at;
    _doFilterSidebarSessions();
  }
}

/**
 * Comprueba el ISO y muestra el diálogo de confirmación antes de ejecutar.
 */
async function executeSession() {
  const project = getActiveProject();
  if (!currentSession || !project) return;

  // Guardar antes de cualquier comprobación para no perder cambios
  await saveSession();
  clearProjectDirty(project.id);

  // Verificar disponibilidad del origen (los 3 tipos soportados)
  const check = await apiFetch(`/api/sessions/${currentSession.id}/check-iso`);
  if (!check) return; // error de red ya manejado por apiFetch
  if (!check.available) {
    const name = (check.iso_path || '').replace(/\\/g, '/').split('/').pop();
    const label = check.source_label || 'ISO';
    const verb = check.source_type === 'bdmv_folder' ? 'La carpeta' : 'El fichero';
    showToast(`${label} no disponible: "${name}" no está en /mnt/isos. No se puede ejecutar.`, 'error');
    // Actualizar banner por si no estaba visible
    project.isoAvailable = false;
    const prevSubTab = activeSubTabId;
    activeSubTabId = project.id;
    setText('iso-missing-title', `${label} no disponible.`);
    setText('iso-missing-text', ` ${verb} "${name}" ya no se encuentra en /mnt/isos.`);
    show('iso-missing-banner');
    activeSubTabId = prevSubTab;
    return;
  }

  showConfirm(
    '▶️ Ejecutar proyecto',
    `Se añadirá a la cola de ejecución:\n\n"${currentSession.mkv_name || 'MKV'}"\n\nSi hay otros trabajos en espera, se ejecutará cuando les toque.`,
    _doExecute,
    '▶️ Ejecutar'
  );
}

async function _doExecute() {
  const project = getActiveProject();
  if (!currentSession || !project) return;

  const sid = currentSession.id;
  const data = await apiFetch(`/api/sessions/${sid}/execute`, { method: 'POST' });
  if (!data) return;

  const queuePos = data.queue?.length || 0;
  showToast(queuePos > 0
    ? `Añadido a la cola en posición ${queuePos}. Sigue el progreso en "Trabajos en Curso".`
    : 'Iniciando extracción… Sigue el progreso en "Trabajos en Curso".', 'success');

  // Actualizar proyecto abierto: ahora está queued/running
  refreshOpenProjectState(sid);
  switchSubTab('cola');
}

/**
 * Renderiza el banner de resultado post-ejecución en el panel de proyecto.
 * Muestra info de éxito (ruta, duración) o error (mensaje + botón reintentar).
 * Solo visible cuando status === 'done' o 'error'.
 * @param {Object} session — sesión del proyecto
 */
function renderExecResultBanner(session) {
  const banner  = E('exec-result-banner');
  const icon    = E('exec-result-icon');
  const title   = E('exec-result-title');
  const detail  = E('exec-result-detail');
  const actions = E('exec-result-actions');
  if (!banner) return;

  // El banner SOLO se muestra cuando hay ejecución activa (running/queued).
  // Los resultados de ejecuciones pasadas (done/error) se muestran en la
  // tabla de historial de ejecuciones (§6.10).
  if (session.status === 'running' || session.status === 'queued') {
    banner.style.display = '';
    banner.className = 'banner info';
    icon.textContent = session.status === 'running' ? '⏳' : '⏸';
    title.textContent = session.status === 'running' ? 'Ejecución en curso…' : 'En cola de ejecución';
    detail.innerHTML = 'Monitoriza el progreso en el panel <strong>Trabajos en Curso</strong>.';
    const cancelBtn = session.status === 'running'
      ? ` <button class="btn btn-danger btn-xs" onclick="cancelRunningSession('${escHtml(session.id)}')"
          data-tooltip="Cancela el proceso en curso, desmonta el ISO y limpia temporales">🛑 Cancelar</button>`
      : '';
    actions.innerHTML = `
      <button class="btn btn-primary btn-xs" onclick="switchSubTab('cola')"
        data-tooltip="Ver el progreso en tiempo real">📺 Ver progreso</button>${cancelBtn}`;
  } else {
    banner.style.display = 'none';
  }
}

/**
 * Refresca el estado de un proyecto abierto tras un cambio de ejecución.
 * Recarga la sesión desde el backend y actualiza banner, tabla, phase strip,
 * botón e icono de tab — sin re-renderizar todo el panel (preserva ediciones).
 * @param {string} sessionId — ID de la sesión a refrescar
 */
async function refreshOpenProjectState(sessionId) {
  const project = openProjects.find(p => p.sessionId === sessionId);
  if (!project) return;

  const data = await apiFetch(`/api/sessions/${sessionId}`);
  if (!data) return;

  // Actualizar sesión en el proyecto abierto
  project.session = data;

  // Actualizar en cache del sidebar también
  const cached = _sessionsCache.find(s => s.id === sessionId);
  if (cached) Object.assign(cached, data);

  // Re-renderizar solo las partes dinámicas (scoped al proyecto)
  const prevSubTab = activeSubTabId;
  activeSubTabId = project.id;
  currentSession = data;

  renderExecResultBanner(data);
  renderPhaseStrip(data);
  renderExecuteArea();
  renderExecutionHistory(data);
  updateProjectTabIcon(project);

  activeSubTabId = prevSubTab;
  // Restaurar currentSession al proyecto activo real
  const active = getActiveProject();
  currentSession = active ? active.session : null;
}

/**
 * Renderiza la tabla de historial de ejecuciones en el panel de proyecto.
 * Cada fila muestra: número, fecha, estado, elapsed por fase, total, acciones (ver log).
 * @param {Object} session
 */
function renderExecutionHistory(session) {
  const history = session.execution_history || [];
  const countEl = E('exec-history-count');
  const emptyEl = E('exec-history-empty');
  const wrapEl  = E('exec-history-table-wrap');
  const tbodyEl = E('exec-history-tbody');

  if (countEl) countEl.textContent = history.length;

  if (!history.length) {
    if (emptyEl) emptyEl.style.display = '';
    if (wrapEl)  wrapEl.style.display = 'none';
    return;
  }

  if (emptyEl) emptyEl.style.display = 'none';
  if (wrapEl)  wrapEl.style.display = '';
  if (!tbodyEl) return;

  // Renderizar filas en orden inverso (más reciente primero)
  tbodyEl.innerHTML = '';
  const reversed = [...history].reverse();
  for (const rec of reversed) {
    const isDone  = rec.status === 'done';
    const icon    = isDone ? '✅' : '❌';
    const dateStr = rec.started_at ? formatRelativeDate(rec.started_at) : '—';

    // Elapsed por fase
    const ph = rec.phase_elapsed || {};
    const fmtPh = (key) => {
      const v = ph[key];
      if (v === null || v === undefined) return '<span class="exec-ph-na">—</span>';
      return `<span class="exec-ph-val">${fmtSecs(Math.round(v))}</span>`;
    };

    // Total
    let totalSecs = 0;
    if (rec.started_at && rec.finished_at) {
      totalSecs = Math.round((new Date(rec.finished_at) - new Date(rec.started_at)) / 1000);
    }

    // Error snippet
    const errTitle = !isDone && rec.error_message
      ? ` data-tooltip="${escHtml(rec.error_message)}"`
      : '';

    const tr = document.createElement('tr');
    tr.className = isDone ? '' : 'exec-row-error';
    tr.innerHTML = `
      <td class="exec-h-num">${rec.run_number}</td>
      <td class="exec-h-date" data-tooltip="${rec.started_at ? escHtml(new Date(rec.started_at).toLocaleString()) : ''}">${escHtml(dateStr)}</td>
      <td class="exec-h-status"${errTitle}>${icon}</td>
      <td>${fmtPh('mount')}</td>
      <td>${fmtPh('extract')}</td>
      <td>${fmtPh('unmount')}</td>
      <td>${fmtPh('write')}</td>
      <td class="exec-h-total">${totalSecs > 0 ? fmtSecs(totalSecs) : '—'}</td>
      <td class="exec-h-actions">
        <button class="btn btn-ghost btn-xs" onclick="showLogModal(${rec.run_number - 1})"
          data-tooltip="Ver el log completo de esta ejecución">📄 Log</button>
        <button class="btn btn-ghost btn-xs" onclick="downloadExecLog(${rec.run_number - 1})"
          data-tooltip="Descargar el log como fichero .txt">⬇</button>
      </td>`;
    tbodyEl.appendChild(tr);
  }
}

/**
 * Actualiza la phase strip del proyecto según el estado de la sesión.
 * Refleja si la ejecución está pendiente, en curso, completada o con error.
 * @param {Object} session
 */
function renderPhaseStrip(session) {
  const strip = E('exec-result-banner')?.parentElement?.querySelector('.project-phase-strip');
  if (!strip) return;

  // 4 pasos: Análisis → Reglas → Revisión → mkvmerge
  const states = {
    pending:  { a:'done', b:'done', c:'active', d:'muted' },
    queued:   { a:'done', b:'done', c:'done',   d:'muted' },
    running:  { a:'done', b:'done', c:'done',   d:'active' },
    done:     { a:'done', b:'done', c:'done',   d:'done' },
    error:    { a:'done', b:'done', c:'done',   d:'error' },
  };
  const s = states[session.status] || states.pending;

  const steps = strip.querySelectorAll('.pps-step');
  const keys = ['a', 'b', 'c', 'd'];
  steps.forEach((step, i) => {
    if (keys[i]) step.className = `pps-step ${s[keys[i]]}`;
  });
}

/**
 * Muestra el botón de ejecución con texto adaptado al estado de la sesión.
 */
function renderExecuteArea() {
  const btn = E('execute-btn');
  if (!btn) return;

  const session = currentSession;
  if (session?.status === 'done') {
    btn.disabled = false;
    btn.innerHTML = '↻ Re-ejecutar';
  } else if (session?.status === 'running' || session?.status === 'queued') {
    btn.disabled = true;
    btn.innerHTML = '⏳ En ejecución…';
  } else {
    btn.disabled = false;
    btn.innerHTML = '▶️ Confirmar y ejecutar';
  }
}

// ═══════════════════════════════════════════════════════════════════
//  WEBSOCKET + PROGRESO
// ═══════════════════════════════════════════════════════════════════

/**
 * Conecta el WebSocket de log para un proyecto específico.
 * @param {Object} project
 * @param {string} sessionId
 */
function connectWebSocketForProject(project, sessionId) {
  if (project.ws) project.ws.close();
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  project.ws = new WebSocket(`${proto}://${location.host}/ws/${sessionId}`);
  project.ws.onmessage = (e) => handleExecutionWsMessage(e.data);
  project.ws.onclose   = () => { project.ws = null; };
}

/** Delay de reconexión con backoff exponencial (3s → 6s → 12s → 30s max). */
let _queueWsReconnectDelay = 3000;
const _QUEUE_WS_MAX_DELAY  = 30000;

/** Conecta el WebSocket global de cola. */
function connectQueueWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  queueWs = new WebSocket(`${proto}://${location.host}/ws/queue`);
  queueWs.onopen = () => { _queueWsReconnectDelay = 3000; }; // reset on success
  queueWs.onmessage = (e) => {
    const prevRunning = queueState.running;
    try { queueState = JSON.parse(e.data); } catch { return; }
    renderColaSidebar();
    renderColaDetailPanel();
    updateSubtabQueuePill();
    if (queueState.running && queueState.running !== prevRunning) {
      // Nuevo job arrancando — limpiar dedup de toasts terminales para
      // que el __DONE__/__ERROR__ del job nuevo sí dispare el toast aunque
      // por casualidad tenga el mismo sessionId que el anterior (no debería
      // pasar con timestamps, pero defensivo).
      _resetTerminalToastDedup();
      connectExecutionWebSocket(queueState.running);
      startColaExecTimer();
      // Actualizar proyecto abierto y sidebar: ahora está "running"
      refreshOpenProjectState(queueState.running);
      loadSessions();
    } else if (!queueState.running && prevRunning) {
      stopColaExecTimer();
      loadSessions();
    }
    // Actualizar proyecto anterior que dejó de ejecutarse
    if (prevRunning && prevRunning !== queueState.running) {
      refreshOpenProjectState(prevRunning);
    }
  };
  queueWs.onclose = () => {
    setTimeout(connectQueueWebSocket, _queueWsReconnectDelay);
    _queueWsReconnectDelay = Math.min(_queueWsReconnectDelay * 2, _QUEUE_WS_MAX_DELAY);
  };
}

/**
 * Conecta el WebSocket de la sesión en ejecución para alimentar el panel Cola.
 * @param {string} sessionId
 */
function connectExecutionWebSocket(sessionId) {
  if (executionWs) {
    executionWs._closedByUser = true;  // evita reconnect en el onclose siguiente
    executionWs.close();
  }
  _colaLogLines = [];  // Limpiar log del trabajo anterior
  document.getElementById('csb-log-viewer') && (document.getElementById('csb-log-viewer').innerHTML = '');
  document.getElementById('pc-log-viewer')  && (document.getElementById('pc-log-viewer').innerHTML  = '');

  // Hidratación REST del log antes de conectar el WS — clave para el caso
  // "Mac dormido / pestaña recargada con job en curso". Sin esto, el panel
  // Cola arrancaba vacío y solo se llenaba con líneas nuevas tras reconectar
  // el WS — el histórico se perdía visualmente aunque seguía en
  // session.output_log del backend. Hacemos fetch sin bloquear: si tarda,
  // el WS streaming ya empieza a llenar lineas mientras tanto y el dedupe
  // del watermark evita duplicación cuando el fetch termina.
  let _hydratedCount = 0;
  apiFetch(`/api/sessions/${sessionId}`, { silent: true }).then(sess => {
    if (!sess || !sess.output_log) return;
    // Si el WS ya añadió líneas mientras esperábamos el fetch, mantenemos
    // las que ya están al final y prefijamos las históricas. _colaLogLines
    // tiene el ringbuffer de 500; si el histórico es enorme, mostramos solo
    // últimas 500-N donde N son las que ya entraron via WS.
    const wsLines = _colaLogLines.slice();
    const historic = sess.output_log;
    // Combinamos sin duplicar: el WS pudo haber traído alguna de las
    // líneas finales del histórico si llegó simultáneo. Detectamos por
    // contenido — si las últimas K líneas de historic coinciden con las
    // primeras K de wsLines, no las repetimos.
    let overlap = 0;
    for (let k = Math.min(historic.length, wsLines.length, 50); k > 0; k--) {
      const tail = historic.slice(historic.length - k).join('\n');
      const head = wsLines.slice(0, k).join('\n');
      if (tail === head) { overlap = k; break; }
    }
    const merged = historic.concat(wsLines.slice(overlap));
    // Trim a últimas 500 (el ringbuffer) preservando el final
    _colaLogLines = merged.length > 500 ? merged.slice(merged.length - 500) : merged;
    _hydratedCount = _colaLogLines.length;
    _renderCsbLog();
  }).catch(() => { /* fetch silencioso, no rompe el flujo si falla */ });

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  executionWs = new WebSocket(`${proto}://${location.host}/ws/${sessionId}`);
  executionWs.onmessage = (e) => handleExecutionWsMessage(e.data, sessionId);
  // Auto-reconnect tras Mac sleep / pestaña suspendida / red caida — solo si
  // la sesion sigue running segun el queueState. Los mensajes __DONE__/
  // __CANCELLED__/__ERROR__ ya marcan _closedByUser=true antes de cerrar
  // manualmente, asi que no se reconectan tras finalizacion legitima.
  executionWs.onclose = (ev) => {
    const wasIntentional = ev.target && ev.target._closedByUser;
    executionWs = null;
    if (wasIntentional) return;
    setTimeout(() => {
      if (queueState && queueState.running === sessionId && !executionWs) {
        connectExecutionWebSocket(sessionId);
      }
    }, 2000);
  };
}

/**
 * Procesa mensajes del WebSocket de ejecución.
 * Solo alimenta el panel Cola — el panel de proyecto nunca muestra estado de ejecución.
 * @param {string} msg
 * @param {string} sessionId
 */
// Dedup de toasts terminales (__DONE__/__CANCELLED__/__ERROR__).
// El backend mantiene una lista de WS por session_id en _ws_connections
// y envía el señalizador final a TODOS los conectados. Si la pestaña se
// reenfoca durante un job, el frontend hace reconnect (line 270-278) y
// brevemente coexisten dos conexiones backend (la vieja en CLOSING, la
// nueva en OPEN) — ambas reciben __DONE__ → handleExecutionWsMessage se
// dispara 2× → 2 toasts duplicados. Trackeamos el último sessionId
// procesado para terminal events y descartamos los duplicados. Se
// resetea al arrancar un job nuevo (queueWs detecta running !== prev).
let _lastTerminalToastSessionId = null;

function _resetTerminalToastDedup() {
  _lastTerminalToastSessionId = null;
}

function handleExecutionWsMessage(msg) {
  if (msg === '__DONE__') {
    const finishedId = queueState.running;
    if (finishedId && finishedId === _lastTerminalToastSessionId) {
      // Ya procesado por una conexión WS gemela — descartar.
      return;
    }
    _lastTerminalToastSessionId = finishedId;
    if (executionWs) { executionWs._closedByUser = true; executionWs.close(); executionWs = null; }
    for (const ph of ['mount', 'extract', 'unmount']) updateColaMiniPipeline(ph, 'done');
    updateSubtabQueuePill();
    showToast('Ejecución completada.', 'success');
    loadSessions();
    // Actualizar proyecto abierto en tiempo real
    if (finishedId) refreshOpenProjectState(finishedId);
    return;
  }

  if (msg === '__CANCELLED__') {
    const cancelledId = queueState.running;
    if (cancelledId && cancelledId === _lastTerminalToastSessionId) {
      return;
    }
    _lastTerminalToastSessionId = cancelledId;
    if (executionWs) { executionWs._closedByUser = true; executionWs.close(); executionWs = null; }
    for (const ph of ['mount', 'extract', 'unmount']) updateColaMiniPipeline(ph, 'pending');
    updateSubtabQueuePill();
    showToast('Ejecución cancelada. Temporales limpiados.', 'info');
    loadSessions();
    if (cancelledId) refreshOpenProjectState(cancelledId);
    return;
  }

  if (msg.startsWith('__ERROR__')) {
    const failedId = queueState.running;
    if (failedId && failedId === _lastTerminalToastSessionId) {
      return;
    }
    _lastTerminalToastSessionId = failedId;
    if (executionWs) { executionWs._closedByUser = true; executionWs.close(); executionWs = null; }
    for (const ph of ['mount', 'extract', 'unmount']) updateColaMiniPipeline(ph, 'error');
    updateSubtabQueuePill();
    showToast('Error en la ejecución. Revisa el historial del proyecto.', 'error');
    loadSessions();
    // Actualizar proyecto abierto en tiempo real
    if (failedId) refreshOpenProjectState(failedId);
    return;
  }

  // Alimentar log en vivo
  appendColaLog(msg);

  // Progreso mkvmerge durante la extracción: "Progress: XX%"
  const prgMatch = msg.match(/Progress:\s*(\d+)%/i);
  if (prgMatch) {
    const pct = parseInt(prgMatch[1], 10);
    _pcLastPct = pct;
    const csbBar = document.getElementById('csb-prog-bar');
    if (csbBar) { csbBar.classList.remove('indeterminate'); csbBar.style.width = `${pct}%`; }
    const csbPhaseEl = document.getElementById('csb-phase-label');
    if (csbPhaseEl) csbPhaseEl.textContent = `${pct}%`;
    const pcBar = document.getElementById('pc-bar-extract');
    if (pcBar) { pcBar.classList.remove('indeterminate'); pcBar.style.width = `${pct}%`; }
    const pcPct = document.getElementById('pc-pct-extract');
    if (pcPct) pcPct.textContent = `${pct}%`;
    updateColaMiniPipeline('extract', 'active');
    return;
  }

  // Detectar cambios de fase por marcadores en el log. Los marcadores
  // `[Origen]` (v2.7+) reemplazaron a `[Montando ISO]` / `[Desmontando
  // ISO]` cuando el pipeline pasó a soportar 3 tipos de origen (iso /
  // bdmv_folder / m2ts). Mantenemos los antiguos por compat con sesiones
  // legacy cuyo log se renderiza al reabrir.
  const isMountMarker = msg.includes('[Origen] ┌─') || msg.includes('[Montando ISO]');
  const isUnmountMarker = (
    msg.includes('[Origen] ✓ ISO desmontado')
    || msg.includes('[Origen] ✓ Origen cerrado')
    // Backward-compat con sesiones legacy persistidas en disco
    || msg.includes('[Origen] ISO desmontado')
    || msg.includes('[Origen] Carpeta BDMV liberada')
    || msg.includes('[Origen] Fichero M2TS liberado')
    || msg.includes('[Desmontando ISO]')
  );
  if (isMountMarker) {
    updateColaMiniPipeline('mount', 'active');
    const el = document.getElementById('csb-phase-label');
    if (el) {
      // Label según tipo de origen detectado en el propio mensaje
      el.textContent = msg.includes('Montando el ISO') || msg.includes('Montando ISO') ? 'Montando ISO…'
        : msg.includes('carpeta BDMV') ? 'Preparando carpeta BDMV…'
        : msg.includes('M2TS') ? 'Preparando M2TS…'
        : 'Preparando origen…';
    }
    // Subtítulo de la fase extract: usaremos "origen → MKV" porque
    // puede ser MPLS o m2ts directo según el caso.
    const subEl = document.getElementById('pc-sub-extract');
    if (subEl) subEl.textContent = 'Origen → MKV';
    updateSubtabQueuePill();
  } else if (msg.includes('[Fase D]') || msg.includes('[Fase E]')) {
    updateColaMiniPipeline('mount', 'done');
    updateColaMiniPipeline('extract', 'active');
    const csbBar = document.getElementById('csb-prog-bar');
    if (csbBar) { csbBar.classList.add('indeterminate'); csbBar.style.width = ''; }
    const el = document.getElementById('csb-phase-label');
    if (el) el.textContent = 'mkvmerge…';
    // Detectar ruta por el contenido del mensaje
    const subEl = document.getElementById('pc-sub-extract');
    if (msg.includes('directo') || msg.includes('direct')) {
      if (subEl) subEl.textContent = 'Origen → MKV final (ruta directa)';
    } else if (msg.includes('intermedio') || msg.includes('propedit')) {
      if (subEl) subEl.textContent = 'Origen → intermedio → propedit → final';
    }
    updateSubtabQueuePill();
  } else if (isUnmountMarker) {
    updateColaMiniPipeline('extract', 'done');
    updateColaMiniPipeline('unmount', 'active');
    const el = document.getElementById('csb-phase-label');
    if (el) {
      el.textContent = msg.includes('ISO desmontado') ? 'Desmontando ISO…'
        : msg.includes('carpeta BDMV') ? 'Cerrando carpeta…'
        : msg.includes('fichero M2TS') ? 'Cerrando fichero…'
        // Legacy fallbacks
        : msg.includes('Carpeta BDMV liberada') ? 'Cerrando carpeta…'
        : msg.includes('M2TS liberado') ? 'Cerrando fichero…'
        : 'Cerrando origen…';
    }
    updateSubtabQueuePill();
  }
}

/**
 * Inicia el temporizador de ejecución de un proyecto específico.
 * Actualiza el elapsed del panel del proyecto (si está activo) y del Cola panel.
 * @param {Object} project
 */
/**
 * Inicia el timer standalone del trabajo en curso en la Cola.
 * No necesita un proyecto abierto — funciona con cualquier session_id en ejecución.
 */
function startColaExecTimer() {
  stopColaExecTimer();
  _pcPhaseStart = { mount: null, extract: null, unmount: null };
  _pcPhaseEnd   = { mount: null, extract: null, unmount: null };
  _pcLastPct    = 0;
  _colaExecStart = Date.now();
  // Resetear visual de las 4 fases al arrancar un nuevo job
  for (const ph of ['mount', 'extract', 'unmount']) {
    updateColaMiniPipeline(ph, 'pending');
    const elEl = document.getElementById(`pc-elapsed-${ph}`);
    if (elEl) elEl.textContent = '—';
  }
  // Resetear barra de progreso del sidebar y etiqueta de fase
  const csbBar = document.getElementById('csb-prog-bar');
  if (csbBar) { csbBar.classList.add('indeterminate'); csbBar.style.width = ''; }
  const csbPhase = document.getElementById('csb-phase-label');
  if (csbPhase) csbPhase.textContent = 'Iniciando…';
  document.getElementById('pc-total-elapsed') && (document.getElementById('pc-total-elapsed').textContent = '00:00');
  document.getElementById('csb-elapsed')      && (document.getElementById('csb-elapsed').textContent      = '');

  _colaExecTimer = setInterval(() => {
    const now   = Date.now();
    const total = Math.floor((now - _colaExecStart) / 1000);
    const ts    = fmtSecs(total);

    document.getElementById('csb-elapsed')      && (document.getElementById('csb-elapsed').textContent      = ts);
    document.getElementById('pc-total-elapsed') && (document.getElementById('pc-total-elapsed').textContent = ts);

    // Elapsed por fase
    for (const ph of ['mount', 'extract', 'unmount']) {
      if (_pcPhaseStart[ph] === null) continue;
      const end  = _pcPhaseEnd[ph] ?? now;
      const secs = Math.floor((end - _pcPhaseStart[ph]) / 1000);
      const el   = document.getElementById(`pc-elapsed-${ph}`);
      if (el) el.textContent = fmtSecs(secs);
      // ETA solo para extract (fase con progreso de mkvmerge)
      if (ph === 'extract' && _pcPhaseEnd.extract === null && _pcLastPct > 0 && _pcLastPct < 100) {
        const remaining = Math.round(secs * (100 - _pcLastPct) / _pcLastPct);
        const etaEl = document.getElementById('pc-eta-extract');
        if (etaEl) etaEl.textContent = `ETA ${fmtSecs(remaining)}`;
      }
    }
  }, 1000);
}

/** Detiene el timer standalone de la Cola. */
function stopColaExecTimer() {
  clearInterval(_colaExecTimer);
  _colaExecTimer = null;
  _colaExecStart = null;
}

// ═══════════════════════════════════════════════════════════════════
//  CONSOLA
// ═══════════════════════════════════════════════════════════════════

/**
 * Añade una línea de texto a la consola de output con coloreado semántico.
 *
 * @param {string} text - Línea de texto a añadir.
 */
function appendConsole(text) {
  const c = E('console-wrap');
  if (!c) return;
  // Smart scroll: solo auto-scroll al final si el usuario ya estaba en el
  // fondo. Si ha scrolleado arriba para leer, no le arrastramos.
  const wasAtBottom = _isScrolledNearBottom(c);
  const line = document.createElement('div');
  const low  = text.toLowerCase();
  if (low.startsWith('[fase') || low.startsWith('[pipeline')) line.className = 'log-phase';
  else if (low.includes('error') || low.includes('fallo'))   line.className = 'log-error';
  else if (low.includes('aviso') || low.includes('warning')) line.className = 'log-warn';
  else if (low.startsWith('prgv:'))                          line.className = 'log-prog';
  line.textContent = text;
  c.appendChild(line);
  if (wasAtBottom) c.scrollTop = c.scrollHeight;
}

/**
 * Añade una línea al log en vivo de la Cola y lo re-renderiza según el filtro activo.
 * @param {string} text
 */
function appendColaLog(text) {
  _colaLogLines.push(text);
  if (_colaLogLines.length > 500) _colaLogLines.shift();
  _renderCsbLog();
}

/** Re-renderiza el log en vivo en el sidebar y en el panel detallado. */
function _renderCsbLog() {
  const lines = _colaLogFilter === 'warn'
    ? _colaLogLines.filter(l => {
        const low = l.toLowerCase();
        return low.includes('error') || low.includes('fallo') || low.includes('aviso') || low.includes('warning');
      })
    : _colaLogLines;

  // Renderiza en un elemento dado — misma paleta rica que Tab 3
  const fill = (c) => {
    if (!c) return;
    // Smart scroll: capturar si el usuario estaba en el fondo ANTES de borrar.
    // Si scrolleo arriba para leer lineas previas, respetamos su posicion.
    const wasAtBottom = _isScrolledNearBottom(c);
    const prevScrollTop = c.scrollTop;
    c.innerHTML = '';
    lines.forEach(text => {
      const div = document.createElement('div');
      // Clase base 'log-line' + clase semantica via classifier compartido
      const semCls = _classifyLogLine(text);
      // Caso especial: "Progress: X%" no lo captura el classifier — mantener
      // clase dedicada para que no distraiga con color de fase.
      const progressMatch = /^Progress:\s*\d+%/i.test(text) || /\] Progress:\s*\d+%/.test(text);
      div.className = 'log-line ' + (progressMatch ? 'log-progress' : semCls);
      div.textContent = text;
      c.appendChild(div);
    });
    if (wasAtBottom) {
      c.scrollTop = c.scrollHeight;
    } else {
      // Restaurar aproximadamente la posicion previa. Al re-render con
      // innerHTML=""  el scrollTop se resetea a 0, asi que lo reponemos.
      c.scrollTop = prevScrollTop;
    }
  };

  fill(document.getElementById('csb-log-viewer'));  // sidebar compacto
  fill(document.getElementById('pc-log-viewer'));    // panel de control
}

/**
 * Cambia el filtro del log en vivo del sidebar y re-renderiza.
 * @param {'all'|'warn'} mode
 */
function setCsbLogFilter(mode) {
  _colaLogFilter = mode;
  document.getElementById('csb-filter-all')?.classList.toggle('active', mode === 'all');
  document.getElementById('csb-filter-warn')?.classList.toggle('active', mode === 'warn');
  document.getElementById('pc-filter-all')?.classList.toggle('active', mode === 'all');
  document.getElementById('pc-filter-warn')?.classList.toggle('active', mode === 'warn');
  _renderCsbLog();
}

/** Cambia el filtro del log desde el panel de control (alias sincronizado). */
function setPcLogFilter(mode) { setCsbLogFilter(mode); }

/** Toggle expand/collapse del detalle de log del trabajo en curso. */
function toggleColaJobDetail() {
  const detailEl = document.getElementById('csb-job-detail');
  const btnEl    = document.getElementById('csb-detail-btn');
  if (!detailEl) return;
  const showing = detailEl.style.display !== 'none';
  detailEl.style.display = showing ? 'none' : '';
  if (btnEl) btnEl.classList.toggle('open', !showing);
  if (!showing) {
    _renderCsbLog();
  }
}

// ═══════════════════════════════════════════════════════════════════
//  COLA PANEL
// ═══════════════════════════════════════════════════════════════════

/** Actualiza el sidebar Cola unificado (En curso + Pendiente de inicio + Historial). */
function renderColaSidebar() {
  const running = !!queueState.running;
  const runningProject = queueState.running
    ? openProjects.find(p => p.sessionId === queueState.running) : null;
  const runningSession = queueState.running
    ? _sessionsCache.find(s => s.id === queueState.running) : null;

  // — En curso —
  const runIconEl  = document.getElementById('csb-running-icon');
  if (runIconEl) {
    runIconEl.innerHTML = running ? '<span class="spinner-inline"></span>' : '⏳';
  }
  const runCountEl = document.getElementById('csb-running-count');
  const emptyEl    = document.getElementById('csb-empty');
  const cardEl     = document.getElementById('csb-running-card');
  if (runCountEl) runCountEl.textContent = running ? 1 : 0;
  if (emptyEl) emptyEl.style.display  = running ? 'none' : '';
  if (cardEl)  cardEl.style.display   = running ? '' : 'none';
  if (running) {
    const nameEl = document.getElementById('csb-job-name');
    if (nameEl) {
      const rawName = runningSession?.mkv_name || runningProject?.name || queueState.running || '';
      nameEl.textContent = rawName.replace(/\.mkv$/i, '');
    }
    // Reconfigurar el strip de fases según el tipo de origen — solo si
    // cambió respecto a la última sesión, para evitar reset visual en
    // cada poll. Bdmv/m2ts marcan mount/unmount como skipped (⊘).
    const currentType = runningSession?.source_type || 'iso';
    if (currentType !== _lastConfiguredSourceType) {
      _configurePhaseStripForSource(currentType);
      _lastConfiguredSourceType = currentType;
    }
  } else {
    // Resetear indicadores al quedar sin trabajo
    // Volver a la configuración por defecto (iso) para que la próxima
    // sesión arranque con labels correctos antes de saber su tipo.
    if (_lastConfiguredSourceType !== 'iso') {
      _configurePhaseStripForSource('iso');
      _lastConfiguredSourceType = 'iso';
    }
    for (const ph of ['mount', 'extract', 'unmount']) updateColaMiniPipeline(ph, 'pending');
    const csbBar = document.getElementById('csb-prog-bar');
    if (csbBar) { csbBar.style.width = ''; csbBar.classList.add('indeterminate'); }
    const csbPhaseEl = document.getElementById('csb-phase-label');
    if (csbPhaseEl) csbPhaseEl.textContent = 'Iniciando…';
    const csbElEl = document.getElementById('csb-elapsed');
    if (csbElEl) csbElEl.textContent = '';
  }

  // — Pendiente de inicio —
  const qCountEl = document.getElementById('csb-queue-count');
  const qListEl  = document.getElementById('csb-queue-list');
  const qLen = queueState.queue.length;
  if (qCountEl) qCountEl.textContent = qLen;
  if (qListEl) {
    if (!qLen) {
      qListEl.innerHTML = '<div class="csb-empty-inline">Sin trabajos en espera</div>';
    } else {
      qListEl.innerHTML = '';
      queueState.queue.forEach((sid, idx) => {
        const proj = openProjects.find(p => p.sessionId === sid);
        const name = (proj?.name || sid).replace(/\.mkv$/i, '');
        const session = _sessionsCache.find(s => s.id === sid);
        const dateStr = session ? formatRelativeDate(session.updated_at || session.created_at) : '';
        const isExp = _colaQueueExpanded.has(sid);
        const item = document.createElement('div');
        item.className = 'csb-history-item' + (isExp ? ' expanded' : '');
        item.dataset.sid = sid;
        item.innerHTML = `
          <div class="csb-history-row">
            <span class="csb-queue-drag" data-tooltip="Arrastra para reordenar">⠿</span>
            <span class="csb-history-status">⏳</span>
            <div class="csb-history-body">
              <div class="csb-history-name" data-tooltip="${escHtml(name)}">${escHtml(name)}</div>
              <div class="csb-history-date">🕐 ${escHtml(dateStr)} · #${idx + 1} en cola</div>
            </div>
          </div>
          <div class="csb-history-actions">
            <div class="csb-history-actions-row">
              <button class="btn btn-primary btn-sm" onclick="confirmOpenSession('${escHtml(sid)}','${escHtml(name)}');event.stopPropagation()"
                data-tooltip="Abrir este proyecto en una sub-pestaña de revisión">📂 Abrir</button>
              <button class="btn btn-danger btn-sm" onclick="cancelQueueItem('${escHtml(sid)}');event.stopPropagation()"
                data-tooltip="Quitar de la cola sin ejecutar">✕ Eliminar</button>
            </div>
          </div>`;
        item.querySelector('.csb-history-row').onclick = () => toggleQueueItem(sid);
        qListEl.appendChild(item);
      });
      // Drag & drop para reordenar cola
      _initQueueSortable(qListEl);
    }
  }

}

/**
 * Actualiza el panel de control de ejecución (#panel-cola).
 * Solo muestra el estado del trabajo activo; el historial/cola vive en el sidebar.
 */
function renderColaDetailPanel() {
  const running = !!queueState.running;
  // Buscar sesión directamente en la caché (funciona aunque el proyecto no esté abierto)
  const session = queueState.running
    ? _sessionsCache.find(s => s.id === queueState.running) : null;
  const runningProject = queueState.running
    ? openProjects.find(p => p.sessionId === queueState.running) : null;

  document.getElementById('pc-empty')  ?.style &&
    (document.getElementById('pc-empty').style.display   = running ? 'none' : '');
  document.getElementById('pc-running')?.style &&
    (document.getElementById('pc-running').style.display = running ? '' : 'none');

  if (!running) return;

  // Nombre del trabajo: preferir mkv_name de la sesión, luego nombre del proyecto abierto
  const rawName = session?.mkv_name || runningProject?.name || queueState.running || '';
  const nameEl = document.getElementById('pc-job-name');
  if (nameEl) nameEl.textContent = rawName.replace(/\.mkv$/i, '');

  // Rutas iso → mkv
  const pathsEl = document.getElementById('pc-job-paths');
  if (pathsEl) {
    const iso = session?.iso_path?.split('/').pop() || '—';
    const mkv = session?.mkv_name || '—';
    pathsEl.textContent = `${iso} → ${mkv}`;
  }

  _renderCsbLog();
}

/** Cambia el filtro del log en el panel de control y en el sidebar. */
function setColaLogFilter(mode) {
  _colaLogFilter = mode;
  document.getElementById('csb-filter-all')?.classList.toggle('active', mode === 'all');
  document.getElementById('csb-filter-warn')?.classList.toggle('active', mode === 'warn');
  document.getElementById('pc-filter-all')?.classList.toggle('active', mode === 'all');
  document.getElementById('pc-filter-warn')?.classList.toggle('active', mode === 'warn');
  _renderCsbLog();
}

/** No-op: el sub-tab "Trabajos en Curso" ya no muestra contador ni icono dinámico. */
/** Actualiza indicadores de ejecución: tab principal + sidebar proyectos. */
function updateSubtabQueuePill() {
  const running = !!queueState.running;

  // Tab principal "Crear MKV" — spinner junto al nombre
  const tabBtn = document.getElementById('tab-btn-1');
  if (tabBtn) {
    const existingSpinner = tabBtn.querySelector('.spinner-inline');
    if (running && !existingSpinner) {
      tabBtn.querySelector('.tab-icon').innerHTML = '<span class="spinner-inline"></span>';
    } else if (!running) {
      tabBtn.querySelector('.tab-icon').textContent = '💿';
    }
  }

  // Sidebar: spinner en el proyecto que se está ejecutando
  _updateSidebarRunningIcon();
}

/** Actualiza el icono del sidebar de proyectos para el que está en ejecución. */
function _updateSidebarRunningIcon() {
  const runningId = queueState.running;
  document.querySelectorAll('#sessions-list .session-card').forEach(card => {
    const badge = card.querySelector('.session-card-status-badge');
    if (!badge) return;
    const sid = card.dataset.sid;
    if (sid === runningId) {
      if (!badge.querySelector('.spinner-inline')) {
        badge.innerHTML = '<span class="spinner-inline"></span>';
      }
    } else if (badge.querySelector('.spinner-inline')) {
      // Restaurar icono normal — buscar el estado real en caché
      const session = _sessionsCache.find(s => s.id === sid);
      const statusIcons = { pending: '💿', queued: '⏸', done: '✅', error: '❌' };
      badge.textContent = statusIcons[session?.status] || '💿';
    }
  });
}

/**
 * Actualiza el estado de una fase en el mini pipeline del sidebar Cola.
 * @param {'d'|'e'} phase - Letra de fase.
 * @param {'pending'|'active'|'done'|'error'} state - Nuevo estado.
 * @param {string} [meta] - No usado (mantenido para compatibilidad de llamadas).
 */
/** Fases del pipeline que NO aplican al tipo de origen actual.
 *  Para bdmv_folder y m2ts no hay montaje/desmontaje real — las
 *  marcamos como 'skipped' visualmente para que el panel no sugiera
 *  que se montó algo. Se popula desde _configurePhaseStripForSource. */
let _pipelineSkippedPhases = new Set();

/** Último source_type configurado en el strip (para evitar reconfigurar
 *  en cada render si no cambió). */
let _lastConfiguredSourceType = null;

/** Adapta los títulos, subtítulos y estado visual del pipeline (panel
 *  cola + sidebar mini) al tipo de origen de la sesión en ejecución.
 *  Para iso muestra las 3 fases reales (montar → mkvmerge → desmontar).
 *  Para bdmv_folder y m2ts marca mount/unmount como 'skipped' (atenuado
 *  con ⊘) y cambia los textos para que no mientan al usuario. */
function _configurePhaseStripForSource(sourceType) {
  const isIso = sourceType === 'iso';
  const sourceLabel = sourceType === 'bdmv_folder' ? 'Carpeta BDMV'
    : sourceType === 'm2ts' ? 'Fichero M2TS'
    : 'Origen';

  _pipelineSkippedPhases = isIso ? new Set() : new Set(['mount', 'unmount']);

  // Cola panel — títulos/subtítulos de los pasos
  const set = (sel, text) => {
    const el = document.querySelector(sel);
    if (el) el.textContent = text;
  };
  if (isIso) {
    set('#pc-step-mount .pc-step-title', 'Montar ISO');
    set('#pc-step-mount .pc-step-sub', 'loop mount UDF → /mnt/bd/');
    set('#pc-step-unmount .pc-step-title', 'Desmontar ISO');
    set('#pc-step-unmount .pc-step-sub', 'umount del loop device');
  } else {
    set('#pc-step-mount .pc-step-title', 'Origen directo');
    set('#pc-step-mount .pc-step-sub', `${sourceLabel} — no requiere montaje`);
    set('#pc-step-unmount .pc-step-title', 'Cierre del origen');
    set('#pc-step-unmount .pc-step-sub', `${sourceLabel} — sin operación de limpieza`);
  }

  // Marcado 'skipped' en cola panel + sidebar mini-pipe. Usamos
  // classList.toggle para preservar otras clases (active/done/error)
  // si las hubiera.
  for (const ph of ['mount', 'unmount']) {
    const stepEl = document.getElementById(`pc-step-${ph}`);
    if (stepEl) stepEl.classList.toggle('skipped', !isIso);
    const csbEl = document.getElementById(`csb-pipe-${ph}`);
    if (csbEl) csbEl.classList.toggle('skipped', !isIso);
    // Icono ⊘ en lugar de 💿/🔓 para fases que no aplican
    const circleEl = document.getElementById(`pc-circle-${ph}`);
    if (circleEl && !isIso) circleEl.textContent = '⊘';
    const csbCircleEl = document.getElementById(`csb-pipe-circle-${ph}`);
    if (csbCircleEl && !isIso) csbCircleEl.textContent = '⊘';
  }
}

function updateColaMiniPipeline(phase, state) {
  // No transitar el estado visual ni el icono de fases marcadas como
  // 'skipped'. BDMV/M2TS no tienen mount/unmount real (aunque el
  // backend emita el evento por compat con el ctx-manager Source).
  // El icono ⊘ y la clase 'skipped' las pone _configurePhaseStripForSource
  // al inicio; aquí nos limitamos a no tocarlas.
  if (_pipelineSkippedPhases.has(phase)) {
    return;
  }
  const ICONS = { mount: '💿', extract: '⬇️', unmount: '🔓' };
  // Conector que sigue a cada fase (en sidebar y en panel)
  const CONN = { mount: 'me', extract: 'eu', unmount: null };
  const icon = state === 'done' ? '✓' : state === 'error' ? '✗' : ICONS[phase] || phase;

  // — Timestamps de fase —
  const now = Date.now();
  if (state === 'active' && _pcPhaseStart[phase] === null) {
    _pcPhaseStart[phase] = now;
  }
  if ((state === 'done' || state === 'error') && _pcPhaseEnd[phase] === null && _pcPhaseStart[phase] !== null) {
    _pcPhaseEnd[phase] = now;
    const elapsed = Math.floor((now - _pcPhaseStart[phase]) / 1000);
    const elEl = document.getElementById(`pc-elapsed-${phase}`);
    if (elEl) elEl.textContent = fmtSecs(elapsed);
    const progEl = document.getElementById(`pc-prog-${phase}`);
    if (progEl) progEl.style.display = 'none';
  }

  // — Sidebar compacto —
  // Preservamos clase 'skipped' si está presente — no sobrescribimos
  // todo el className para que mount/unmount en BDMV/M2TS no se
  // resetee al estado por defecto (esos return-earlys arriba ya lo
  // protegen pero el reset a pending pasa por aquí).
  const csbPhaseEl  = document.getElementById(`csb-pipe-${phase}`);
  const csbCircleEl = document.getElementById(`csb-pipe-circle-${phase}`);
  if (csbPhaseEl) {
    const wasSkipped = csbPhaseEl.classList.contains('skipped');
    csbPhaseEl.className = `csb-pipe-phase ${state}${wasSkipped ? ' skipped' : ''}`;
  }
  if (csbCircleEl) csbCircleEl.textContent = icon;
  if (CONN[phase]) {
    const csbConn = document.getElementById(`csb-pipe-conn-${CONN[phase]}`);
    if (csbConn) csbConn.className = `csb-pipe-conn${state === 'done' ? ' done' : state === 'active' ? ' active' : ''}`;
  }

  // — Panel de control —
  const stepEl   = document.getElementById(`pc-step-${phase}`);
  const circleEl = document.getElementById(`pc-circle-${phase}`);
  const progEl   = document.getElementById(`pc-prog-${phase}`);
  if (stepEl) {
    const wasSkipped = stepEl.classList.contains('skipped');
    stepEl.className = `pc-step ${state}${wasSkipped ? ' skipped' : ''}`;
  }
  if (circleEl) circleEl.textContent  = icon;
  if (progEl)   progEl.style.display  = state === 'active' ? '' : 'none';
  const cancelEl = document.getElementById(`pc-cancel-${phase}`);
  if (cancelEl) cancelEl.style.display = state === 'active' ? '' : 'none';
  if (CONN[phase]) {
    const connEl = document.getElementById(`pc-conn-${CONN[phase]}`);
    if (connEl) connEl.className = `pc-step-conn${state === 'done' ? ' done' : state === 'active' ? ' active' : ''}`;
  }
  if (state === 'active') {
    const barEl = document.getElementById(`pc-bar-${phase}`);
    // Solo volver a indeterminate si no hay progreso real aún
    if (barEl && !barEl.style.width) {
      barEl.classList.add('indeterminate');
    }
  }
}

/**
 * Quita una sesión de la cola de espera via DELETE /api/queue/{id}.
 * @param {string} sessionId
 */
async function cancelQueueItem(sessionId) {
  const data = await apiFetch(`/api/queue/${sessionId}`, { method: 'DELETE' });
  if (data !== null) {
    _colaQueueExpanded.delete(sessionId);
    showToast('Trabajo eliminado de la cola.', 'info');
    // Refrescar proyecto abierto y sidebar
    refreshOpenProjectState(sessionId);
    loadSessions();
  }
}

/**
 * Cancela la ejecución activa de una sesión via POST /api/sessions/{id}/cancel.
 * Mata el proceso en curso, desmonta el ISO y limpia temporales.
 * @param {string} sessionId
 */
async function cancelRunningSession(sessionId) {
  const data = await apiFetch(`/api/sessions/${sessionId}/cancel`, { method: 'POST' });
  if (data && data.ok) {
    showToast('Cancelando ejecución… Se cerrará el origen y se limpiarán los temporales.', 'info');
  }
}

/**
 * Cancela el trabajo en ejecución desde el panel Cola (Trabajos en Curso).
 * Lee el session_id del trabajo en curso desde el estado de cola.
 */
function cancelRunningFromCola() {
  const sid = queueState?.running;
  if (sid) cancelRunningSession(sid);
}

/** Instancia Sortable para la cola (se recrea en cada render). */
let _queueSortableInstance = null;

/**
 * Inicializa drag & drop en la lista de cola de ejecución.
 * Al soltar, envía el nuevo orden al backend via POST /api/queue/reorder.
 * @param {HTMLElement} listEl — contenedor de los items de cola
 */
function _initQueueSortable(listEl) {
  if (_queueSortableInstance) _queueSortableInstance.destroy();
  if (!listEl || listEl.children.length < 2) { _queueSortableInstance = null; return; }
  _queueSortableInstance = Sortable.create(listEl, {
    animation: 150,
    ghostClass: 'sortable-ghost',
    chosenClass: 'sortable-chosen',
    handle: '.csb-queue-drag',
    onEnd: async () => {
      const ordered = [...listEl.querySelectorAll('.csb-history-item')]
        .map(el => el.dataset.sid)
        .filter(Boolean);
      await apiFetch('/api/queue/reorder', {
        method: 'POST',
        body: JSON.stringify({ ordered_ids: ordered }),
      });
    },
  });
}

// ── Historial y estadísticas ──────────────────────────────────────


/**
 * Toggle expand/collapse de un item de la cola en la vista compacta.
 * @param {string} sessionId
 */
function toggleQueueItem(sessionId) {
  if (_colaQueueExpanded.has(sessionId)) {
    _colaQueueExpanded.delete(sessionId);
  } else {
    _colaQueueExpanded.add(sessionId);
  }
  const item = document.querySelector(`#csb-queue-list .csb-history-item[data-sid="${CSS.escape(sessionId)}"]`);
  if (item) item.classList.toggle('expanded', _colaQueueExpanded.has(sessionId));
}


/**
 * Descarga el log de una sesión como fichero .txt (log activo, no historial).
 * @param {string} sessionId
 */
function downloadSessionLog(sessionId) {
  const session = _sessionsCache.find(s => s.id === sessionId);
  if (!session) return;
  const text = session.output_log?.length ? session.output_log.join('\n') : '(sin log)';
  const name = (session.mkv_name || sessionId).replace(/\.mkv$/i, '');
  _downloadText(text, `${name}.log.txt`);
}

/**
 * Obtiene el ExecutionRecord del proyecto activo por su índice (0-based).
 * @param {number} idx — índice en execution_history
 * @returns {Object|null}
 */
function _getExecRecord(idx) {
  if (!currentSession?.execution_history) return null;
  return currentSession.execution_history[idx] || null;
}

/**
 * Abre el modal visor de log para una ejecución específica del proyecto activo.
 * @param {number} idx — índice en execution_history (0-based)
 */
function showLogModal(idx) {
  const rec = _getExecRecord(idx);
  if (!rec) return;

  const isDone  = rec.status === 'done';
  const dateStr = rec.started_at ? new Date(rec.started_at).toLocaleString() : '—';
  const status  = isDone ? '✅ Completada' : '❌ Error';

  document.getElementById('log-viewer-title').textContent = `📄 Log — Ejecución #${rec.run_number}`;
  document.getElementById('log-viewer-sub').textContent   = `${status} · ${dateStr}`;

  // Renderizar log con coloreado semántico (misma paleta rica que Tab 3)
  const content = document.getElementById('log-viewer-content');
  content.innerHTML = '';
  const lines = rec.output_log || [];
  for (const line of lines) {
    const div = document.createElement('div');
    div.className = 'log-line ' + _classifyLogLine(line);
    div.textContent = line;
    content.appendChild(div);
  }

  // Botón descargar
  const dlBtn = document.getElementById('log-viewer-download-btn');
  const newBtn = dlBtn.cloneNode(true);
  dlBtn.parentNode.replaceChild(newBtn, dlBtn);
  newBtn.addEventListener('click', () => downloadExecLog(idx));

  document.getElementById('log-viewer-modal').classList.add('open');
  // Scroll al final del log
  content.scrollTop = content.scrollHeight;
}

/**
 * Descarga el log de una ejecución específica como fichero .txt.
 * @param {number} idx — índice en execution_history (0-based)
 */
function downloadExecLog(idx) {
  const rec = _getExecRecord(idx);
  if (!rec) return;
  const text = rec.output_log?.length ? rec.output_log.join('\n') : '(sin log)';
  const name = (currentSession?.mkv_name || 'session').replace(/\.mkv$/i, '');
  _downloadText(text, `${name}_run${rec.run_number}.log.txt`);
}

/** Helper: descarga texto como fichero. */
function _downloadText(text, filename) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ═══════════════════════════════════════════════════════════════════
//  UTILIDADES
// ═══════════════════════════════════════════════════════════════════

/**
 * Convierte segundos (float) al formato de timestamp Matroska HH:MM:SS.mmm.
 * @param {number} secs
 * @returns {string}
 */
function secsToTs(secs) {
  const h  = Math.floor(secs / 3600);
  const m  = Math.floor((secs % 3600) / 60);
  const s  = secs % 60;
  const ms = Math.floor((s - Math.floor(s)) * 1000);
  return `${p2(h)}:${p2(m)}:${p2(Math.floor(s))}.${String(ms).padStart(3,'0')}`;
}

/**
 * Convierte un timestamp HH:MM:SS.mmm a segundos (float).
 * @param {string} ts
 * @returns {number}
 */
function tsToSecs(ts) {
  if (!ts) return 0;
  const parts = ts.split(':');
  if (parts.length === 3)
    return parseInt(parts[0]) * 3600 + parseInt(parts[1]) * 60 + parseFloat(parts[2]);
  return 0;
}

/** @param {number} n @returns {string} Número formateado con al menos 2 dígitos. */
function p2(n) { return String(n).padStart(2,'0'); }
/** @param {number} secs @returns {string} Segundos formateados como MM:SS o HH:MM:SS. */
function fmtSecs(secs) {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  return h > 0 ? `${p2(h)}:${p2(m)}:${p2(s)}` : `${p2(m)}:${p2(s)}`;
}

/**
 * Escapa caracteres especiales HTML para inserción segura en el DOM.
 * @param {*} s
 * @returns {string}
 */
function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

/**
 * Muestra un elemento por ID (busca primero en el proyecto activo con E()).
 * @param {string} id
 * @param {string} [displayValue=''] - Valor CSS display.
 */
function show(id, displayValue = '') {
  const el = E(id);
  if (el) el.style.display = displayValue || '';
}
/** Oculta un elemento por ID (busca con E()). @param {string} id */
function hide(id) {
  const el = E(id);
  if (el) el.style.display = 'none';
}
/** Establece el textContent de un elemento buscado con E(). @param {string} id @param {string} text */
function setText(id, text) {
  const el = E(id);
  if (el) el.textContent = text;
}
/** Establece el estado checked de un checkbox buscado con E(). @param {string} id @param {boolean} checked */
function setToggle(id, checked) {
  const el = E(id);
  if (el) el.checked = checked;
}

/** Timeout por defecto para llamadas API (30s). */
const API_FETCH_TIMEOUT = 30000;

/**
 * Wrapper de fetch con Content-Type JSON, timeout y manejo centralizado de errores.
 *
 * @param {string} url              - URL relativa del endpoint.
 * @param {RequestInit} [opts={}]   - Opciones de fetch. opts.silent=true suprime el toast
 *                                     de timeout/error (util para polling rutinario donde
 *                                     timeouts transitorios bajo carga I/O son normales y
 *                                     el siguiente tick los resuelve).
 * @param {number} [timeoutMs]      - Timeout en ms (default: API_FETCH_TIMEOUT).
 * @returns {Promise<Object|null>}  - JSON parseado, o null si hubo error.
 */
async function apiFetch(url, opts = {}, timeoutMs = API_FETCH_TIMEOUT) {
  const silent = !!opts.silent;
  delete opts.silent;
  opts.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  opts.signal = controller.signal;
  try {
    const resp = await fetch(url, opts);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      if (!silent) showToast(`Error: ${err.detail || resp.statusText}`, 'error');
      appendConsole(`[Error API] ${url}: ${err.detail || resp.statusText}`);
      return null;
    }
    return await resp.json();
  } catch (e) {
    const msg = e.name === 'AbortError'
      ? `Timeout: el servidor no respondió en ${timeoutMs / 1000}s`
      : `Error de red: ${e.message}`;
    if (!silent) showToast(msg, 'error');
    appendConsole(`[Error red] ${url}: ${msg}`);
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// Añadir spin animation al CSS dinámicamente
const spinStyle = document.createElement('style');
spinStyle.textContent = '@keyframes spin { to { transform: rotate(360deg) } }';
document.head.appendChild(spinStyle);


// ═══════════════════════════════════════════════════════════════════
//  TAB 2 — EDITAR MKV
// ═══════════════════════════════════════════════════════════════════

/** MKV abierto en Tab 2. null = sin MKV cargado. */
let mkvProject = null;  // {fileName, filePath, analysis, originalAnalysis, dirty}
let _mkvPickerSelected = null;

// ── MKV Picker — usa el file browser con roots Library + Output ───
// El antiguo modal #mkv-picker-modal con <select> queda como fallback
// pero ya no se invoca desde la UI. El flujo nuevo es:
//   1. openMkvPickerModal() → openFileBrowser con roots [biblioteca, output]
//   2. Al seleccionar MKV, _doAnalyzeMkvFromPickerPath(absPath, name) lanza
//      /api/mkv/analyze con la ruta absoluta. El backend valida que la
//      ruta cae bajo un root permitido.

async function openMkvPickerModal() {
  // Si hay MKV abierto con cambios pendientes, confirmar antes de abrir
  if (mkvProject?.dirty) {
    showConfirm(
      'Cambios sin guardar',
      'Hay cambios sin guardar en el MKV actual. ¿Descartar y abrir otro?',
      () => _openMkvBrowserNow(),
      'Descartar y abrir',
    );
    return;
  }
  _openMkvBrowserNow();
}

function _openMkvBrowserNow() {
  openFileBrowser({
    title: 'Abrir MKV para inspeccionar / editar',
    subtitle: 'Selecciona el MKV en tu biblioteca o en el output del converter',
    roots: [
      { key: 'library', label: 'Biblioteca', icon: '📚' },
      { key: 'output',  label: 'Output',     icon: '📦' },
    ],
    onSelect: async (absPath, name) => {
      // 1) Sync: setup + abrir modal analisis (queda BAJO el browser por z-index).
      const fileEl = document.getElementById('mkv-analyze-modal-file');
      if (fileEl) fileEl.textContent = name;
      _resetMkvAnalyzeSteps();
      openModal('mkv-analyze-modal');
      // 2) Async (NO await): el fetch de analisis tarda 1-3 min. Lo lanzamos en
      //    background para que onSelect resuelva inmediatamente y _fileBrowserSelect
      //    cierre el browser → quedando solo el modal de analisis visible.
      _doAnalyzeMkvFromPickerPath(absPath, name).catch(e => {
        console.error('analyze MKV error:', e);
        showToast(`Error en analisis: ${e.message || e}`, 'error');
      });
    },
  });
}

async function _doAnalyzeMkvFromPickerPath(absPath, fileName, forceRefresh = false) {

  // Polling de progreso real del backend — reusa /api/analyze/progress
  const steps = ['identify', 'mediainfo', 'pgs', 'dovi'];
  let lastStep = 'identify';
  let stepStartTs = Date.now();
  const pollId = setInterval(async () => {
    try {
      const prog = await apiFetch('/api/analyze/progress');
      if (prog?.step && prog.step !== lastStep && steps.includes(prog.step)) {
        const prevIdx = steps.indexOf(lastStep);
        const newIdx = steps.indexOf(prog.step);
        // Solo avanzar — ignorar backward transitions (defense-in-depth,
        // mismo guard que en Tab 1's _doAnalyzeISO).
        if (newIdx > prevIdx) {
          for (let i = prevIdx; i < newIdx; i++) {
            _advanceMkvAnalyzeStep(steps[i], steps[i + 1]);
          }
          lastStep = prog.step;
          stepStartTs = Date.now();
        }
      }
      // En el paso PGS mostrar barra de progreso real basada en bytes leídos
      // por ffprobe (vía /proc/{pid}/io, emitido desde phase_a.run_pgs_packet_counts).
      if (lastStep === 'pgs') {
        const labelEl = document.getElementById('mkv-analyze-step-pgs-label');
        const barWrap = document.getElementById('mkv-analyze-step-pgs-bar');
        const barFill = document.getElementById('mkv-analyze-step-pgs-bar-fill');
        const statsEl = document.getElementById('mkv-analyze-step-pgs-stats');
        const elapsed = Math.floor((Date.now() - stepStartTs) / 1000);
        const mm = Math.floor(elapsed / 60);
        const ss = (elapsed % 60).toString().padStart(2, '0');
        const pct = prog?.pct;
        const eta = prog?.eta_s;
        if (labelEl) labelEl.textContent = '⏳ Analizando subtítulos del origen…';
        if (barWrap) barWrap.style.display = 'block';
        if (statsEl) statsEl.style.display = 'block';
        if (pct != null && barFill) {
          barFill.style.width = pct + '%';
        }
        if (statsEl) {
          let line = `${mm}:${ss} transcurridos`;
          if (pct != null) line += ` · ${pct.toFixed(1)}% leído`;
          if (eta && eta > 0) {
            const em = Math.floor(eta / 60);
            const es = (eta % 60).toString().padStart(2, '0');
            line += ` · ETA ${em}:${es}`;
          }
          statsEl.textContent = line;
        }
      }
    } catch (_) { /* silenciar errores de polling */ }
  }, 500);

  // Enviamos absPath (ruta absoluta resuelta por el file browser). El
  // backend valida que cae bajo un root permitido (Library / Output) y
  // ya no asume /mnt/output como prefijo automatico.
  // force_refresh: si true, invalida el cache antes de re-analizar (botón
  // "↻ Re-analizar" del panel). Si false (default), permite cache HIT
  // instantáneo para MKVs ya analizados previamente.
  const data = await apiFetch('/api/mkv/analyze', {
    method: 'POST',
    body: JSON.stringify({ file_path: absPath, force_refresh: forceRefresh }),
  }, 600000);  // 10 min timeout — el PGS puede tardar 1-3 min

  clearInterval(pollId);
  // Marcar todos los pasos restantes como completados
  steps.forEach((s, i) => {
    if (i < steps.length - 1) _advanceMkvAnalyzeStep(s, steps[i + 1]);
  });
  await new Promise(r => setTimeout(r, 300));
  closeModal('mkv-analyze-modal');

  if (!data) {
    showToast('Error al analizar el MKV.', 'error');
    return;
  }

  openMkvProject(data);
}

/** Resetea los pasos del modal de análisis de MKV. */
function _resetMkvAnalyzeSteps() {
  const steps = ['identify', 'mediainfo', 'pgs', 'dovi'];
  steps.forEach((s, i) => {
    const container = document.getElementById(`mkv-analyze-step-${s}`);
    if (container) container.style.opacity = i === 0 ? '1' : '.4';
    const labelEl = s === 'pgs'
      ? document.getElementById('mkv-analyze-step-pgs-label')
      : container;
    if (labelEl) {
      labelEl.textContent = labelEl.textContent.replace(/^[✅⏳⬜]\s*/, i === 0 ? '⏳ ' : '⬜ ');
    }
  });
  const statsEl = document.getElementById('mkv-analyze-step-pgs-stats');
  if (statsEl) { statsEl.style.display = 'none'; statsEl.textContent = ''; }
  const barWrap = document.getElementById('mkv-analyze-step-pgs-bar');
  const barFill = document.getElementById('mkv-analyze-step-pgs-bar-fill');
  if (barWrap) barWrap.style.display = 'none';
  if (barFill) barFill.style.width = '0%';
}

/** Avanza del paso fromStep (que se marca ✅) al nextStep (que se marca ⏳). */
function _advanceMkvAnalyzeStep(fromStep, nextStep) {
  const fromLabel = fromStep === 'pgs'
    ? document.getElementById('mkv-analyze-step-pgs-label')
    : document.getElementById(`mkv-analyze-step-${fromStep}`);
  if (fromLabel) fromLabel.textContent = fromLabel.textContent.replace(/^[⏳⬜✅]\s*/, '✅ ');
  const fromContainer = document.getElementById(`mkv-analyze-step-${fromStep}`);
  if (fromContainer) fromContainer.style.opacity = '1';

  if (nextStep) {
    const nextContainer = document.getElementById(`mkv-analyze-step-${nextStep}`);
    if (nextContainer) nextContainer.style.opacity = '1';
    const nextLabel = nextStep === 'pgs'
      ? document.getElementById('mkv-analyze-step-pgs-label')
      : nextContainer;
    if (nextLabel) nextLabel.textContent = nextLabel.textContent.replace(/^[⏳⬜✅]\s*/, '⏳ ');
  }
}

// ── Proyecto MKV ─────────────────────────────────────────────────

function openMkvProject(analysis) {
  mkvProject = {
    fileName: analysis.file_name,
    filePath: analysis.file_path,
    analysis: analysis,
    originalAnalysis: structuredClone(analysis),
    dirty: false,
  };
  document.getElementById('mkv-empty-state').style.display = 'none';
  const panel = document.getElementById('mkv-edit-panel');
  panel.style.display = '';
  _renderMkvEditPanel();
  showToast(`MKV abierto: ${analysis.file_name}`, 'success');
}

function closeMkvEditor() {
  if (!mkvProject) return;
  if (mkvProject.dirty) {
    showConfirm(
      'Cambios sin guardar',
      'Hay cambios sin guardar. ¿Cerrar de todas formas?',
      () => _doCloseMkvEditor(),
      'Cerrar sin guardar',
    );
    return;
  }
  _doCloseMkvEditor();
}

function _doCloseMkvEditor() {
  mkvProject = null;
  document.getElementById('mkv-edit-panel').style.display = 'none';
  document.getElementById('mkv-edit-panel').innerHTML = '';
  document.getElementById('mkv-empty-state').style.display = '';
}

/**
 * Re-analiza el MKV actualmente abierto invalidando el cache. Útil cuando
 * el fichero ha cambiado externamente (no via Tab 2 — esos cambios ya
 * invalidan automáticamente el cache) o cuando se quiere forzar un fresh
 * tras un bump de versión del clasificador.
 *
 * Si hay cambios pendientes en el panel, pide confirmación. Al terminar,
 * el resultado fresh sobrescribe el cache para futuras aperturas.
 */
async function reanalyzeMkv() {
  if (!mkvProject) return;
  const absPath = mkvProject.filePath || mkvProject.analysis?.file_path;
  const fileName = mkvProject.fileName || mkvProject.analysis?.file_name || '';
  if (!absPath) {
    showToast('No se conoce la ruta del MKV', 'error');
    return;
  }
  const doRun = () => {
    // Abrir el modal de análisis (mismo del open inicial) y disparar el
    // fetch con force_refresh:true. El backend invalida el cache antes de
    // re-ejecutar el pipeline completo (1-3 min en MKVs grandes).
    const fileEl = document.getElementById('mkv-analyze-modal-file');
    if (fileEl) fileEl.textContent = fileName;
    _resetMkvAnalyzeSteps();
    openModal('mkv-analyze-modal');
    _doAnalyzeMkvFromPickerPath(absPath, fileName, true).catch(e => {
      console.error('reanalyze MKV error:', e);
      showToast(`Error en re-análisis: ${e.message || e}`, 'error');
    });
  };
  if (mkvProject.dirty) {
    showConfirm(
      'Cambios sin guardar',
      'Hay cambios sin guardar en el MKV actual. Re-analizar los descartará. ¿Continuar?',
      doRun,
      'Descartar y re-analizar',
    );
    return;
  }
  doRun();
}

function undoMkvEdits() {
  if (!mkvProject) return;
  mkvProject.analysis = structuredClone(mkvProject.originalAnalysis);
  mkvProject.dirty = false;
  _renderMkvEditPanel();
  showToast('Cambios revertidos', 'info');
}

// ══════════════════════════════════════════════════════════════════
//  RADIOGRAFÍA DV+HDR — Tab 2 "Consultar / Editar MKV"
//  Sustituye a los badges heurísticos de procedencia (nativo/retail/…).
//  8 secciones con datos factuales + visualizadores.
//  Datos provienen de `a.dovi` (DoviInfo via dovi_tool info) y `a.hdr`
//  (HdrMetadata via MediaInfo).
// ══════════════════════════════════════════════════════════════════

/** Fila factual de la tabla: label + valor + tooltip opcional.
 *  `status`: 'ok' (verde), 'warn' (ámbar), 'absent' (gris tenue), 'neutral' */
function _rgrfRow(label, value, { tooltip = '', status = 'neutral' } = {}) {
  if (value == null || value === '' || value === undefined) {
    value = '<span style="color:var(--text-3); font-style:italic">—</span>';
  }
  const colorMap = {
    ok:      '#0e6b2a',
    warn:    '#8a4a00',
    absent:  'var(--text-3)',
    neutral: 'var(--text-1)',
  };
  const valColor = colorMap[status] || colorMap.neutral;
  const tipAttr = tooltip ? ` data-tooltip="${escHtml(tooltip)}"` : '';
  return `
    <div class="rgrf-row"${tipAttr}>
      <span class="rgrf-label">${label}</span>
      <span class="rgrf-value" style="color:${valColor}">${value}</span>
    </div>`;
}

/** Icono ✓/✗ según presencia, con tooltip explicativo opcional. */
function _rgrfPresence(present, label, { tooltip = '' } = {}) {
  const icon  = present ? '✓' : '✗';
  const color = present ? '#0e6b2a' : 'var(--text-3)';
  const bg    = present ? 'rgba(52,199,89,0.10)' : 'transparent';
  const tip   = tooltip ? ` data-tooltip="${escHtml(tooltip)}"` : '';
  return `<span class="rgrf-pill" style="color:${color}; background:${bg}"${tip}><span class="rgrf-pill-icon">${icon}</span> ${escHtml(label)}</span>`;
}

/** Visualizador L5: frame con active area resaltada.
 *  Metafora de pantalla: fondo negro (barras letterbox), área activa teal
 *  con gradient sutil + borde brillante. Texto blanco centrado. */
function _rgrfL5Svg(dv, frameW = 3840, frameH = 2160) {
  const t = dv.l5_top || 0, b = dv.l5_bottom || 0;
  const l = dv.l5_left || 0, r = dv.l5_right || 0;
  const targetW = 240;
  const ratio = targetW / frameW;
  const svgW = Math.round(frameW * ratio);
  const svgH = Math.round(frameH * ratio);
  const activeX = Math.round(l * ratio);
  const activeY = Math.round(t * ratio);
  const activeW = Math.round((frameW - l - r) * ratio);
  const activeH = Math.round((frameH - t - b) * ratio);
  const gid = `l5g-${Math.random().toString(36).slice(2, 7)}`;
  return `
    <svg viewBox="0 0 ${svgW} ${svgH}" width="${svgW}" height="${svgH}"
         style="display:block; border-radius:8px; overflow:hidden; box-shadow:0 2px 8px rgba(15,23,42,0.15)"
         xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"   stop-color="#66b0ff" stop-opacity="0.38" />
          <stop offset="100%" stop-color="#007AFF" stop-opacity="0.28" />
        </linearGradient>
      </defs>
      <rect width="${svgW}" height="${svgH}" fill="#0a0a0c" />
      <rect x="${activeX + 0.5}" y="${activeY + 0.5}"
            width="${activeW - 1}" height="${activeH - 1}"
            fill="url(#${gid})" stroke="#5eead4" stroke-width="1.5"
            rx="2" />
      <text x="${svgW/2}" y="${svgH/2 + 5}" fill="#ccfbf1" font-size="13"
            font-family="SF Mono,monospace" text-anchor="middle" font-weight="600"
            style="letter-spacing:0.3px">${frameW - l - r} × ${frameH - t - b}</text>
    </svg>`;
}

/** Aspect ratio inferido a partir de active area de L5. */
function _rgrfAspectLabel(dv, frameW = 3840, frameH = 2160) {
  const activeW = frameW - (dv.l5_left || 0) - (dv.l5_right || 0);
  const activeH = frameH - (dv.l5_top || 0) - (dv.l5_bottom || 0);
  if (activeH === 0) return '—';
  const ratio = activeW / activeH;
  const candidates = [
    { val: 2.39, label: '2.39 : 1 (CinemaScope)' },
    { val: 2.35, label: '2.35 : 1' },
    { val: 2.20, label: '2.20 : 1 (Todd-AO)' },
    { val: 1.85, label: '1.85 : 1 (Widescreen)' },
    { val: 1.78, label: '1.78 : 1 (16:9)' },
    { val: 1.66, label: '1.66 : 1' },
    { val: 1.33, label: '1.33 : 1 (4:3)' },
  ];
  const match = candidates.reduce((best, c) =>
    Math.abs(c.val - ratio) < Math.abs(best.val - ratio) ? c : best
  );
  const close = Math.abs(match.val - ratio) < 0.03;
  return close ? match.label : `${ratio.toFixed(2)} : 1`;
}

/** Visualizador L8: trims en escala log — dots con halo radial + labels encima. */
/**
 * Etiqueta semántica de un trim target L8 según los nits del display
 * destino. Mapping de la spec de Dolby Vision (subset común). Los valores
 * intermedios (ej. 1500 nits visto en algún master raro) se etiquetan
 * solo con sus nits sin texto extra.
 */
function _l8NitsLabel(n) {
  const map = {
    100:  'SDR target',
    350:  'HDR low',
    600:  'HDR mid',
    1000: 'HDR consumer',
    2000: 'HDR high-end',
    4000: 'Pulsar reference',
  };
  return map[n] || '';
}

function _rgrfL8Svg(nits) {
  if (!Array.isArray(nits) || !nits.length) return '';
  // svgH aumentado de 68 → 86 para alojar la fila de labels semánticos
  // debajo de la fila de nits (eje X). axisY se queda igual.
  const svgW = 500, svgH = 86, padL = 32, padR = 32, axisY = 46;
  const usableW = svgW - padL - padR;
  const logMin = Math.log10(10), logMax = Math.log10(10000);
  const xOf = (n) => padL + ((Math.log10(Math.max(n, 1)) - logMin) / (logMax - logMin)) * usableW;
  const ticks = [10, 100, 1000, 10000];
  const gid = `l8g-${Math.random().toString(36).slice(2, 7)}`;
  let html = `<svg viewBox="0 0 ${svgW} ${svgH}" width="${svgW}" height="${svgH}"
    style="display:block; max-width:100%" xmlns="http://www.w3.org/2000/svg">`;
  html += `<defs>
    <radialGradient id="${gid}" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#5eead4"/>
      <stop offset="100%" stop-color="#007AFF"/>
    </radialGradient>
  </defs>`;
  // Eje horizontal con grosor sutil
  html += `<line x1="${padL}" y1="${axisY}" x2="${svgW - padR}" y2="${axisY}"
             stroke="rgba(15,23,42,0.15)" stroke-width="1" />`;
  ticks.forEach(t => {
    const x = xOf(t);
    html += `<line x1="${x}" y1="${axisY - 3}" x2="${x}" y2="${axisY + 3}"
               stroke="rgba(15,23,42,0.28)" stroke-width="1.2" />`;
    html += `<text x="${x}" y="${axisY + 18}" fill="#64748b" font-size="11.5"
               font-family="SF Mono,monospace" text-anchor="middle" font-weight="500">${t}</text>`;
  });
  // Dots con halo + label semántica debajo (si conocida)
  nits.forEach(n => {
    const x = xOf(n);
    html += `<circle cx="${x}" cy="${axisY}" r="10" fill="#007AFF" fill-opacity="0.12" />`;
    html += `<circle cx="${x}" cy="${axisY}" r="6.5" fill="url(#${gid})" stroke="#ffffff" stroke-width="2" />`;
    html += `<text x="${x}" y="${axisY - 14}" fill="#003e8a" font-size="12"
               font-family="SF Mono,monospace" text-anchor="middle" font-weight="700">${n}</text>`;
    const label = _l8NitsLabel(n);
    if (label) {
      html += `<text x="${x}" y="${axisY + 33}" fill="#1e40af" font-size="9.5"
                 font-family="-apple-system,Inter,sans-serif" text-anchor="middle"
                 font-weight="500" opacity="0.75">${label}</text>`;
    }
  });
  html += `</svg>`;
  return html;
}

/** Visualizador CIE 1931: triángulos gamut con legenda glassmorphism. */
function _rgrfGamutSvg(l9Primaries, l10Primaries) {
  const svgSize = 280, pad = 24;
  const cieToSvg = (x, y) => {
    const sx = pad + x * (svgSize - 2 * pad) / 0.8;
    const sy = svgSize - pad - y * (svgSize - 2 * pad) / 0.9;
    return [sx, sy];
  };
  const triangle = (pts, color, highlight = false) => {
    const d = pts.map(([x, y]) => cieToSvg(x, y).join(',')).join(' ');
    const op = highlight ? 0.22 : 0.08;
    const sw = highlight ? 2.5 : 1.5;
    return `<polygon points="${d}" stroke="${color}" fill="${color}" stroke-width="${sw}" fill-opacity="${op}" />`;
  };
  const rec709  = [[0.640, 0.330], [0.300, 0.600], [0.150, 0.060]];
  const dciP3   = [[0.680, 0.320], [0.265, 0.690], [0.150, 0.060]];
  const rec2020 = [[0.708, 0.292], [0.170, 0.797], [0.131, 0.046]];
  const d65     = [0.3127, 0.3290];
  const [d65x, d65y] = cieToSvg(d65[0], d65[1]);

  const gamutMatch = (s) => {
    const low = (s || '').toLowerCase();
    if (low.includes('2020')) return 'rec2020';
    if (low.includes('p3'))   return 'p3';
    if (low.includes('709'))  return 'rec709';
    return null;
  };
  const l9Match = gamutMatch(l9Primaries);

  // Paleta para light mode — más saturada, alto contraste
  const cRec2020 = '#007AFF';   // app blue (--blue)
  const cP3      = '#f59e0b';   // amber-500
  const cRec709  = '#e11d48';   // rose-600

  return `
    <svg viewBox="0 0 ${svgSize} ${svgSize}" width="${svgSize}" height="${svgSize}"
         style="display:block; background:#fafbfc; border-radius:8px; border:1px solid rgba(15,23,42,0.05)"
         xmlns="http://www.w3.org/2000/svg">
      <!-- grid sutil -->
      <g stroke="rgba(15,23,42,0.05)" stroke-width="1">
        ${[0.2, 0.4, 0.6].map(v => {
          const [, y] = cieToSvg(0, v);
          return `<line x1="${pad}" y1="${y}" x2="${svgSize - pad}" y2="${y}" />`;
        }).join('')}
        ${[0.2, 0.4, 0.6].map(v => {
          const [x,] = cieToSvg(v, 0);
          return `<line x1="${x}" y1="${pad}" x2="${x}" y2="${svgSize - pad}" />`;
        }).join('')}
      </g>
      <!-- ejes -->
      <line x1="${pad}" y1="${svgSize - pad}" x2="${svgSize - pad}" y2="${svgSize - pad}" stroke="rgba(15,23,42,0.3)" stroke-width="1.3" />
      <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${svgSize - pad}" stroke="rgba(15,23,42,0.3)" stroke-width="1.3" />
      <!-- triangulos gamut (de mayor a menor para que queden bien stacked) -->
      ${triangle(rec2020, cRec2020, l9Match === 'rec2020')}
      ${triangle(dciP3,   cP3,      l9Match === 'p3')}
      ${triangle(rec709,  cRec709,  l9Match === 'rec709')}
      <!-- D65 white point con halo -->
      <circle cx="${d65x}" cy="${d65y}" r="8" fill="rgba(15,23,42,0.08)" />
      <circle cx="${d65x}" cy="${d65y}" r="4" fill="#ffffff" stroke="#0f172a" stroke-width="1.5" />
      <text x="${d65x + 9}" y="${d65y + 4}" fill="#0f172a" font-size="11" font-family="SF Mono,monospace" font-weight="700">D65</text>
      <!-- Leyenda glassmorphism -->
      <g font-size="11" font-family="SF Mono,monospace">
        <rect x="${svgSize - 94}" y="${pad - 4}" width="84" height="62" rx="6"
              fill="rgba(255,255,255,0.92)" stroke="rgba(15,23,42,0.08)" stroke-width="1" />
        <circle cx="${svgSize - 85}" cy="${pad + 8}" r="4" fill="${cRec2020}"/>
        <text x="${svgSize - 77}" y="${pad + 12}" fill="#003e8a" font-weight="700">Rec.2020</text>
        <circle cx="${svgSize - 85}" cy="${pad + 26}" r="4" fill="${cP3}"/>
        <text x="${svgSize - 77}" y="${pad + 30}" fill="#92400e" font-weight="700">DCI-P3</text>
        <circle cx="${svgSize - 85}" cy="${pad + 44}" r="4" fill="${cRec709}"/>
        <text x="${svgSize - 77}" y="${pad + 48}" fill="#9f1239" font-weight="700">Rec.709</text>
      </g>
    </svg>`;
}

/** Formatea segundos a "MM:SS" (< 1h) o "H:MM:SS" (>= 1h). */
function _rgrfFmtTime(secs) {
  secs = Math.max(0, Math.round(secs || 0));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** Sparkline MaxCLL — smooth curve con gradient fill + shadow filter + grid +
 *  EJE DE TIEMPO con 5 ticks + marcador del pico con su timestamp. */
function _rgrfSparklineSvg(series, labelMax, durationSeconds, opts = {}) {
  if (!Array.isArray(series) || series.length < 2) return '';
  const svgW = 720, svgH = 200, padL = 56, padR = 118, padT = 18, padB = 44;
  // Curvas opcionales (mismo length que series) + referencias en nits.
  const avgSeries = Array.isArray(opts.avgSeries) && opts.avgSeries.length === series.length
    ? opts.avgSeries : null;
  const minSeries = Array.isArray(opts.minSeries) && opts.minSeries.length === series.length
    ? opts.minSeries : null;
  const refs = (opts.refs && typeof opts.refs === 'object') ? opts.refs : {};
  const peakV = Math.max(...series);
  // Y-axis: peak con 10% headroom. Las referencias que caigan dentro se
  // pintan como lineas; las que excedan se listan como chips a la derecha.
  const yMax = Math.max(1, Math.ceil(peakV * 1.15 / 10) * 10);
  const usableW = svgW - padL - padR;
  const usableH = svgH - padT - padB;
  const xOf = (i) => padL + (i / (series.length - 1)) * usableW;
  const yOf = (v) => padT + usableH - Math.max(0, Math.min(1, v / yMax)) * usableH;
  // Mapa index-del-bucket → segundo del movie (proporcional a duracion)
  const tOf = (i) => (durationSeconds && durationSeconds > 0)
    ? durationSeconds * (i / (series.length - 1))
    : null;

  // Helper: genera path Catmull-Rom suavizado para una serie de [x, y] points
  const _smoothPath = (pts) => {
    if (pts.length < 2) return '';
    let p = `M ${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[Math.max(0, i - 1)];
      const p1 = pts[i];
      const p2 = pts[i + 1];
      const p3 = pts[Math.min(pts.length - 1, i + 2)];
      const cp1x = p1[0] + (p2[0] - p0[0]) / 6;
      const cp1y = p1[1] + (p2[1] - p0[1]) / 6;
      const cp2x = p2[0] - (p3[0] - p1[0]) / 6;
      const cp2y = p2[1] - (p3[1] - p1[1]) / 6;
      p += ` C ${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
    }
    return p;
  };

  const peakPts = series.map((v, i) => [xOf(i), yOf(v)]);
  const linePath = _smoothPath(peakPts);
  const areaPath = `${linePath} L ${peakPts[peakPts.length-1][0].toFixed(1)},${padT + usableH} L ${peakPts[0][0].toFixed(1)},${padT + usableH} Z`;
  const avgPath = avgSeries
    ? _smoothPath(avgSeries.map((v, i) => [xOf(i), yOf(v)]))
    : '';
  const minPath = minSeries
    ? _smoothPath(minSeries.map((v, i) => [xOf(i), yOf(v)]))
    : '';

  // Grid en 0/25/50/75/100% del yMax
  const gridLines = [0, 0.25, 0.5, 0.75, 1.0].map(pct => {
    const y = padT + usableH - pct * usableH;
    const val = Math.round(yMax * pct);
    return `<line x1="${padL}" y1="${y}" x2="${svgW - padR}" y2="${y}" stroke="rgba(15,23,42,0.06)" stroke-dasharray="3,4" />
            <text x="${padL - 8}" y="${y + 4}" fill="#64748b" font-size="11" font-family="SF Mono,monospace" text-anchor="end" font-weight="500">${val}</text>`;
  }).join('');

  const gid = `sp-${Math.random().toString(36).slice(2, 7)}`;

  // Eje X con 5 ticks de tiempo (0%, 25%, 50%, 75%, 100%) + linea base
  const TICK_FRACS = [0, 0.25, 0.5, 0.75, 1.0];
  const axisY = padT + usableH;
  let timeTicks = `<line x1="${padL}" y1="${axisY}" x2="${svgW - padR}" y2="${axisY}"
                         stroke="rgba(15,23,42,0.15)" stroke-width="1" />`;
  TICK_FRACS.forEach(frac => {
    const x = padL + frac * usableW;
    const t = durationSeconds ? durationSeconds * frac : null;
    const label = t !== null ? _rgrfFmtTime(t) : (frac === 0 ? 'inicio' : (frac === 1 ? 'final' : ''));
    timeTicks += `<line x1="${x}" y1="${axisY - 3}" x2="${x}" y2="${axisY + 3}"
                         stroke="rgba(15,23,42,0.3)" stroke-width="1.2" />`;
    if (label) {
      const anchor = frac === 0 ? 'start' : (frac === 1 ? 'end' : 'middle');
      timeTicks += `<text x="${x}" y="${axisY + 18}" fill="#475569" font-size="11"
                          font-family="SF Mono,monospace" text-anchor="${anchor}" font-weight="500">${label}</text>`;
    }
  });

  // Marcador del pico: busca el índice del valor máximo y dibuja círculo + línea + label
  const peakIdx = series.indexOf(peakV);
  const peakX = xOf(peakIdx);
  const peakY = yOf(peakV);
  const peakTime = tOf(peakIdx);
  const peakLabelText = peakTime !== null ? `${peakV} nits @ ${_rgrfFmtTime(peakTime)}` : `pico ${labelMax}`;
  // Decidir lado del label (izq si el pico está en la mitad derecha, para no salirse)
  const peakOnRight = peakIdx / series.length > 0.5;
  const peakLabelX = peakOnRight ? peakX - 8 : peakX + 8;
  const peakLabelAnchor = peakOnRight ? 'end' : 'start';
  const peakMarker = `
    <line x1="${peakX}" y1="${peakY}" x2="${peakX}" y2="${axisY}"
          stroke="#007AFF" stroke-width="1" stroke-dasharray="2,3" opacity="0.45" />
    <circle cx="${peakX}" cy="${peakY}" r="9" fill="#007AFF" fill-opacity="0.15" />
    <circle cx="${peakX}" cy="${peakY}" r="4.5" fill="#007AFF" stroke="#ffffff" stroke-width="2" />
    <text x="${peakLabelX}" y="${peakY + 4}" fill="#003e8a" font-size="11.5"
          font-family="SF Mono,monospace" text-anchor="${peakLabelAnchor}" font-weight="700">${peakLabelText}</text>`;

  // ── Líneas de referencia (L2 trims, HDR10 MaxCLL, L6 master) ─────
  // Las que caben dentro del yMax se dibujan como líneas dasheadas con
  // label a la derecha. Las que exceden se listan abajo como chips.
  const refsToDraw = [];
  const refsOutOfRange = [];
  const _addRef = (val, label, color) => {
    if (!val || val <= 0) return;
    if (val <= yMax) refsToDraw.push({ val, label, color });
    else refsOutOfRange.push({ val, label, color });
  };
  if (Array.isArray(refs.l2_trim_targets_nits)) {
    refs.l2_trim_targets_nits.forEach(n =>
      _addRef(n, `Trim ${n}n`, '#f59e0b')); // amber
  }
  _addRef(refs.hdr10_max_cll, `MaxCLL ${refs.hdr10_max_cll}n`, '#ec4899'); // pink
  _addRef(refs.hdr10_max_fall, `MaxFALL ${refs.hdr10_max_fall}n`, '#a855f7'); // purple
  _addRef(refs.l6_master_max_nits, `Master ${refs.l6_master_max_nits}n`, '#64748b'); // slate
  _addRef(refs.l6_max_cll, `L6 CLL ${refs.l6_max_cll}n`, '#dc2626'); // red

  const refLines = refsToDraw.map(r => {
    const y = yOf(r.val);
    return `<line x1="${padL}" y1="${y}" x2="${svgW - padR}" y2="${y}"
                  stroke="${r.color}" stroke-width="1" stroke-dasharray="4,3" opacity="0.55" />
            <text x="${svgW - padR + 4}" y="${y + 4}" fill="${r.color}"
                  font-size="10" font-family="SF Mono,monospace" font-weight="600"
                  text-anchor="start">${r.label}</text>`;
  }).join('');
  // Chips para refs fuera de rango — se renderizan abajo del SVG
  const outOfRangeChips = refsOutOfRange.length > 0
    ? `<div class="dv-sparkline-out-chips">
         <span class="dv-sparkline-out-label">Fuera del chart:</span>
         ${refsOutOfRange.map(r =>
            `<span class="dv-sparkline-out-chip" style="--chip-c:${r.color}">${r.label}</span>`
         ).join('')}
       </div>`
    : '';

  // Leyenda compacta: peak / avg / min cuando aplica + refs (max 3)
  const legendParts = [
    `<span class="dv-sl-leg-item" style="--c:#007AFF">Peak (max_pq)</span>`,
  ];
  if (avgPath) legendParts.push(`<span class="dv-sl-leg-item" style="--c:#22c55e">Avg (avg_pq)</span>`);
  if (minPath) legendParts.push(`<span class="dv-sl-leg-item" style="--c:#94a3b8">Min (min_pq)</span>`);
  refsToDraw.slice(0, 4).forEach(r =>
    legendParts.push(`<span class="dv-sl-leg-item dashed" style="--c:${r.color}">${r.label}</span>`));
  const legendHtml = `<div class="dv-sparkline-legend">${legendParts.join('')}</div>`;

  // Crosshair y dot del hover — ocultos hasta que el usuario mueva el mouse
  // sobre el chart. La hidratación se hace en _attachSparklineHover().
  // Usamos vector-effect="non-scaling-stroke" para que el grosor se
  // mantenga aunque el SVG estire en X (preserveAspectRatio="none").
  const hoverCursor = `
    <line class="dv-sparkline-cursor" x1="0" y1="${padT}" x2="0" y2="${axisY}"
          stroke="#007AFF" stroke-width="1.2" stroke-dasharray="3,3" opacity="0.7"
          style="display:none" vector-effect="non-scaling-stroke" />
    <circle class="dv-sparkline-dot" cx="0" cy="0" r="4.5" fill="#007AFF"
            stroke="#ffffff" stroke-width="2" style="display:none" />`;

  // Datos serializados para el handler de mouse (no se renderizan visualmente).
  const seriesAttr = JSON.stringify(series).replace(/"/g, '&quot;');
  const avgAttr = avgSeries ? JSON.stringify(avgSeries).replace(/"/g, '&quot;') : '';
  const minAttr = minSeries ? JSON.stringify(minSeries).replace(/"/g, '&quot;') : '';
  const dur = durationSeconds || 0;

  return `
    <div class="dv-sparkline-host" style="position:relative">
    <svg class="dv-sparkline-svg" viewBox="0 0 ${svgW} ${svgH}" width="100%" height="${svgH}" preserveAspectRatio="none"
         data-series="${seriesAttr}" data-avg-series="${avgAttr}" data-min-series="${minAttr}"
         data-duration="${dur}" data-y-max="${yMax}"
         data-pad-l="${padL}" data-pad-r="${padR}" data-pad-t="${padT}" data-pad-b="${padB}"
         data-svg-w="${svgW}" data-svg-h="${svgH}"
         style="display:block; max-width:100%" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="${gid}-area" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"  stop-color="#66b0ff" stop-opacity="0.40"/>
          <stop offset="60%" stop-color="#66b0ff" stop-opacity="0.16"/>
          <stop offset="100%" stop-color="#66b0ff" stop-opacity="0.00"/>
        </linearGradient>
        <linearGradient id="${gid}-line" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%"   stop-color="#007AFF"/>
          <stop offset="100%" stop-color="#3395ff"/>
        </linearGradient>
        <filter id="${gid}-shadow" x="-2%" y="-10%" width="104%" height="120%">
          <feGaussianBlur in="SourceAlpha" stdDeviation="1.5"/>
          <feOffset dy="1.5"/>
          <feComponentTransfer><feFuncA type="linear" slope="0.22"/></feComponentTransfer>
          <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>
      ${gridLines}
      ${refLines}
      <path d="${areaPath}" fill="url(#${gid}-area)" />
      ${minPath ? `<path d="${minPath}" fill="none" stroke="#94a3b8" stroke-width="1.2"
            stroke-dasharray="4,3" opacity="0.7" stroke-linejoin="round" stroke-linecap="round" />` : ''}
      ${avgPath ? `<path d="${avgPath}" fill="none" stroke="#22c55e" stroke-width="1.6"
            stroke-linejoin="round" stroke-linecap="round" opacity="0.85" />` : ''}
      <path d="${linePath}" fill="none" stroke="url(#${gid}-line)" stroke-width="2.2"
            stroke-linejoin="round" stroke-linecap="round" filter="url(#${gid}-shadow)" />
      ${timeTicks}
      ${peakMarker}
      ${hoverCursor}
    </svg>
    <div class="dv-sparkline-tooltip" style="display:none"></div>
    ${legendHtml}
    ${outOfRangeChips}
    </div>`;
}

/** Mini-card con percentiles + clasificacion de escenas por rango de brillo.
 *  stats: { peak, p99, p95, p50, avg_of_max, bucket_dim, bucket_mid, bucket_high, total }
 *  hdr:   info HDR10 del container (a.hdr) para mostrar comparativa MaxCLL/MaxFALL.
 */
function _rgrfL1StatsCard(stats, hdr) {
  if (!stats || !stats.total) return '';
  const pct = (n) => stats.total > 0 ? (n / stats.total) * 100 : 0;
  const p1 = pct(stats.bucket_dim).toFixed(1);
  const p2 = pct(stats.bucket_mid).toFixed(1);
  const p3 = pct(stats.bucket_high).toFixed(1);
  const hdr10 = hdr ? [
    hdr.max_cll  ? `MaxCLL ${hdr.max_cll} nits`   : '',
    hdr.max_fall ? `MaxFALL ${hdr.max_fall} nits` : '',
  ].filter(Boolean).join(' · ') : '';

  return `
    <div class="dv-l1-stats">
      <div class="dv-l1-stats-row">
        <div class="dv-l1-stats-block">
          <div class="dv-l1-stats-block-title">Percentiles · DV L1 max_pq</div>
          <div class="dv-l1-stats-grid">
            <div><span class="lbl">peak</span><span class="val">${stats.peak}<span class="u">nits</span></span></div>
            <div><span class="lbl">p99</span><span class="val">${stats.p99}<span class="u">nits</span></span></div>
            <div><span class="lbl">p95</span><span class="val">${stats.p95}<span class="u">nits</span></span></div>
            <div><span class="lbl">p50</span><span class="val">${stats.p50}<span class="u">nits</span></span></div>
            <div><span class="lbl">avg</span><span class="val">${stats.avg_of_max}<span class="u">nits</span></span></div>
          </div>
        </div>
        <div class="dv-l1-stats-block">
          <div class="dv-l1-stats-block-title">Distribución por brillo de escena</div>
          <div class="dv-l1-bars">
            <div class="dv-l1-bar-row">
              <span class="dv-l1-bar-label">SDR-like &lt;100n</span>
              <div class="dv-l1-bar-track"><div class="dv-l1-bar-fill" style="width:${p1}%; background:#94a3b8"></div></div>
              <span class="dv-l1-bar-pct">${p1}%</span>
              <span class="dv-l1-bar-count">(${stats.bucket_dim.toLocaleString()})</span>
            </div>
            <div class="dv-l1-bar-row">
              <span class="dv-l1-bar-label">Midtone 100–300n</span>
              <div class="dv-l1-bar-track"><div class="dv-l1-bar-fill" style="width:${p2}%; background:#3395ff"></div></div>
              <span class="dv-l1-bar-pct">${p2}%</span>
              <span class="dv-l1-bar-count">(${stats.bucket_mid.toLocaleString()})</span>
            </div>
            <div class="dv-l1-bar-row">
              <span class="dv-l1-bar-label">Highlight ≥300n</span>
              <div class="dv-l1-bar-track"><div class="dv-l1-bar-fill" style="width:${p3}%; background:#f59e0b"></div></div>
              <span class="dv-l1-bar-pct">${p3}%</span>
              <span class="dv-l1-bar-count">(${stats.bucket_high.toLocaleString()})</span>
            </div>
          </div>
        </div>
      </div>
      ${hdr10 ? `<div class="dv-l1-stats-foot">HDR10 container: ${hdr10}<span class="dv-l1-stats-foot-note">— métrica estática del SEI, distinta de DV L1 (puede diferir ampliamente del peak L1)</span></div>` : ''}
    </div>`;
}

/** Cadena de mastering — sustituye al bloque "Gamut CIE 1931" + parte
 *  del bloque "Luminancia". Muestra textualmente con chips toda la
 *  ficha del color/master del MKV, que es donde realmente varia entre
 *  discos UHD (el container BT.2020 es constante asi que el diagrama
 *  CIE no aportaba info). Distingue 3 etapas: master donde se grade,
 *  container del stream, target del DV.
 *
 *  dv         — analysis.dovi (puede ser null)
 *  hdr        — analysis.hdr (HdrMetadata)
 *  mainVideo  — pista video principal (para bit_depth)
 */
function _rgrfMasteringChain(dv, hdr, mainVideo) {
  const masterPrim = (hdr?.mastering_display_primaries || '').trim();
  const masterLum  = (hdr?.mastering_display_luminance || '').trim();
  const cont = {
    primaries: hdr?.color_primaries || mainVideo?.color_primaries || '',
    transfer:  hdr?.transfer_characteristics || mainVideo?.transfer_characteristics || '',
    bitDepth:  hdr?.bit_depth || mainVideo?.bit_depth || 0,
  };
  const l9     = dv?.l9_primaries  || '';   // source primaries (donde se grade)
  const l10    = dv?.l10_primaries || '';   // target display primaries
  const l11Type = dv?.l11_content_type || '';
  const l11App  = dv?.l11_intended_application || '';
  // L2 trim targets (refs ya extraidas durante el light profile, si se corrio)
  const l2Trims = dv?.l1_references?.l2_trim_targets_nits;
  // L6 master peak — del light profile O parseando hdr.mastering_display_luminance
  const l6MasterMax = dv?.l1_references?.l6_master_max_nits || 0;

  // El master "real" donde se hizo el grade: prioridad L9 (si DV lo declara)
  // luego mastering_display_primaries del HDR10 SEI.
  const masterPrimResolved = l9 || masterPrim || '—';
  const masterSource = l9 ? 'desde L9' : (masterPrim ? 'desde HDR10 SEI' : '');

  // Master peak/min: si tenemos L6 numerico lo usamos; si no parseamos el
  // string del HDR10 (formato 'min: X cd/m2, max: Y cd/m2').
  let masterPeakStr = '—';
  let masterMinStr = '—';
  if (l6MasterMax > 0) {
    masterPeakStr = `${l6MasterMax} nits`;
    const mn = dv?.l1_references?.l6_master_min_nits;
    if (mn != null && mn > 0) masterMinStr = `${mn.toFixed(3)} nits`;
  } else if (masterLum) {
    // 'min: 0.0050 cd/m2, max: 4000.0000 cd/m2'
    const mxM = masterLum.match(/max:\s*([\d.]+)\s*cd/i);
    const mnM = masterLum.match(/min:\s*([\d.]+)\s*cd/i);
    if (mxM) masterPeakStr = `${Math.round(parseFloat(mxM[1]))} nits`;
    if (mnM) masterMinStr = `${parseFloat(mnM[1]).toFixed(3)} nits`;
  }

  // Diferencia gamut master vs container — si master es P3 y container BT.2020
  // es un grading P3 expandido a BT.2020 container (caso muy comun).
  const isP3Master = /p3|dci/i.test(masterPrimResolved);
  const is2020Container = /2020/i.test(cont.primaries);
  const showExpansionChip = isP3Master && is2020Container;

  // Trim chips ordenados ASC. Distinguimos 3 estados:
  //   1. Hay L2 trims → mostrar chips
  //   2. Light profile YA corrido pero sin L2 trims → RPU CMv4.0 (usa L8)
  //   3. Light profile NO corrido → invitacion a analizar
  const lightProfileRun = !!dv?.l1_references;
  const hasL8Trims = Array.isArray(dv?.l8_trim_nits) && dv.l8_trim_nits.length > 0;
  let trimChips;
  if (Array.isArray(l2Trims) && l2Trims.length > 0) {
    trimChips = l2Trims.map(n => `<span class="dv-mc-trim-chip">${n}n</span>`).join('');
  } else if (lightProfileRun) {
    // Light profile corrido pero sin L2 trims — caso normal en RPUs CMv4.0
    // que solo tienen L8. No es un error, solo informativo.
    trimChips = hasL8Trims
      ? '<span class="dv-mc-empty">sin L2 trims · este RPU usa L8 (ver fila inferior)</span>'
      : '<span class="dv-mc-empty">sin L2 trims declarados en el RPU</span>';
  } else {
    trimChips = '<span class="dv-mc-empty">analiza el perfil de luminancia para extraer los trim targets</span>';
  }

  // HDR10 metadata footer
  const hdr10Cll  = hdr?.max_cll  != null ? `MaxCLL ${hdr.max_cll} nits` : '';
  const hdr10Fall = hdr?.max_fall != null ? `MaxFALL ${hdr.max_fall} nits` : '';
  const hdr10Line = [hdr10Cll, hdr10Fall].filter(Boolean).join(' · ');

  // L1 vs HDR10 divergence: comparar el peak L1 RPU (de dovi_tool info
  // sample 30s, ya disponible en dv.l1_max_cll) vs el MaxCLL del SEI
  // estático. Si difieren >1.8×, suele indicar master con tone-mapping
  // agresivo etiquetado conservadoramente (caso BR2049: L1=176, SEI=1000).
  // Si L1 > SEI, lo contrario: SEI conservador, RPU más generoso.
  let divergenceBanner = '';
  const l1Peak  = dv?.l1_max_cll || 0;
  const seiCll  = hdr?.max_cll || 0;
  if (l1Peak > 10 && seiCll > 10) {
    const ratio = l1Peak / seiCll;
    if (ratio < 0.5) {
      divergenceBanner = `
        <div class="dv-mc-divergence dv-mc-div-low">
          <span class="dv-mc-div-icon">⚠️</span>
          <span><strong>Master conservador con tone-mapping agresivo</strong> —
            L1 RPU peak ${l1Peak.toFixed(0)} nits vs HDR10 SEI MaxCLL ${seiCll} nits
            (ratio ${ratio.toFixed(2)}×). El colorista etiquetó la metadata DV
            por debajo del peak HDR10 — la imagen real tras display mapping
            puede mostrar valores mayores que los anunciados por el L1.
          </span>
        </div>`;
    } else if (ratio > 2.0) {
      divergenceBanner = `
        <div class="dv-mc-divergence dv-mc-div-high">
          <span class="dv-mc-div-icon">ℹ️</span>
          <span><strong>L1 RPU más generoso que HDR10 SEI</strong> —
            L1 peak ${l1Peak.toFixed(0)} nits vs SEI MaxCLL ${seiCll} nits
            (ratio ${ratio.toFixed(2)}×). El SEI HDR10 está etiquetado conservadoramente
            respecto al grado DV real.
          </span>
        </div>`;
    }
  }

  return `
    <section class="dv-block">
      <h5 class="dv-block-title">Cadena de mastering
        <span class="dv-block-sub">grade source → container → DV targets</span>
      </h5>
      <div class="dv-mc-grid">
        <div class="dv-mc-card">
          <div class="dv-mc-card-title">Master display
            ${masterSource ? `<span class="dv-mc-card-src">· ${masterSource}</span>` : ''}
          </div>
          <div class="dv-mc-card-primary">${escHtml(masterPrimResolved)}</div>
          <div class="dv-mc-card-meta">peak <strong>${masterPeakStr}</strong> · min ${masterMinStr}</div>
        </div>
        <div class="dv-mc-card">
          <div class="dv-mc-card-title">Container HEVC</div>
          <div class="dv-mc-card-primary">${escHtml(cont.primaries || '—')}</div>
          <div class="dv-mc-card-meta">
            ${cont.transfer ? `<strong>${escHtml(cont.transfer)}</strong>` : '—'}
            ${cont.bitDepth ? ` · ${cont.bitDepth}-bit` : ''}
          </div>
          ${showExpansionChip ? `<div class="dv-mc-flow-hint">P3 ↑ BT.2020 (gamut expandido al container)</div>` : ''}
        </div>
        <div class="dv-mc-card">
          <div class="dv-mc-card-title">DV target display
            ${l10 ? '<span class="dv-mc-card-src">· L10</span>' : ''}
          </div>
          <div class="dv-mc-card-primary">${l10 ? escHtml(l10) : '—'}</div>
          <div class="dv-mc-card-meta">
            ${l10
              ? 'gamut objetivo del grade DV'
              : '<span class="dv-mc-empty">L10 no presente — DV targeting genérico</span>'}
          </div>
        </div>
      </div>
      <div class="dv-mc-row-trims">
        <div class="dv-mc-row-label">DV trim targets <span class="dv-mc-row-sub">L2 target_max_pq</span></div>
        <div class="dv-mc-row-content">${trimChips}</div>
      </div>
      ${hdr10Line ? `
        <div class="dv-mc-row-hdr10">
          <div class="dv-mc-row-label">HDR10 metadata <span class="dv-mc-row-sub">SEI estática</span></div>
          <div class="dv-mc-row-content"><span class="dv-mc-hdr10-val">${hdr10Line}</span></div>
        </div>` : ''}
      ${divergenceBanner}
      ${(l11Type || l11App) ? `
        <div class="dv-mc-row-l11">
          <div class="dv-mc-row-label">L11 content type</div>
          <div class="dv-mc-row-content">${escHtml(l11Type)}${l11App ? ` <span class="dv-mc-row-sub">(${escHtml(l11App)})</span>` : ''}</div>
        </div>` : ''}
    </section>`;
}

/** Histograma distribución luminancia — barras con gradient vertical + ticks. */
function _rgrfDistributionSvg(series) {
  if (!Array.isArray(series) || series.length < 1) return '';
  const svgW = 720, svgH = 200, padL = 52, padR = 18, padT = 16, padB = 48;
  const usableW = svgW - padL - padR;
  const usableH = svgH - padT - padB;
  const bins = [10, 30, 100, 300, 1000, 3000, 10000];
  const binLabels = ['10', '30', '100', '300', '1K', '3K', '10K'];
  const counts = new Array(bins.length).fill(0);
  series.forEach(v => {
    for (let i = bins.length - 1; i >= 0; i--) {
      if (v >= bins[i]) { counts[i]++; break; }
    }
  });
  const total = Math.max(counts.reduce((a, b) => a + b, 0), 1);
  const maxPct = Math.max(...counts.map(c => c / total * 100), 1);
  const barW = usableW / bins.length;

  // Paleta cold → warm (light-mode friendly, contrastes WCAG AA)
  const colors = [
    ['#2563eb', '#3b82f6'],   // blue
    ['#0891b2', '#06b6d4'],   // cyan
    ['#059669', '#10b981'],   // emerald
    ['#65a30d', '#84cc16'],   // lime
    ['#d97706', '#f59e0b'],   // amber
    ['#ea580c', '#f97316'],   // orange
    ['#dc2626', '#ef4444'],   // red
  ];

  let defs = '<defs>';
  colors.forEach((c, i) => {
    defs += `<linearGradient id="hist-${i}" x1="0" y1="0" x2="0" y2="1">
               <stop offset="0%" stop-color="${c[1]}" stop-opacity="0.95"/>
               <stop offset="100%" stop-color="${c[0]}" stop-opacity="0.80"/>
             </linearGradient>`;
  });
  defs += '</defs>';

  let grid = '';
  [0, 0.25, 0.5, 0.75, 1.0].forEach(r => {
    const y = padT + usableH - r * usableH;
    const lbl = Math.round(maxPct * r);
    grid += `<line x1="${padL}" y1="${y}" x2="${svgW - padR}" y2="${y}" stroke="rgba(15,23,42,0.06)" stroke-dasharray="3,4" />`;
    grid += `<text x="${padL - 8}" y="${y + 4}" fill="#64748b" font-size="11" font-family="SF Mono,monospace" text-anchor="end" font-weight="500">${lbl}%</text>`;
  });

  let bars = '';
  counts.forEach((c, i) => {
    const pct = (c / total) * 100;
    const h = (pct / maxPct) * usableH;
    const x = padL + i * barW;
    const y = padT + usableH - h;
    // Barra con radius top + shadow sutil
    bars += `<rect x="${x + 8}" y="${y}" width="${barW - 16}" height="${Math.max(h, 1)}"
               fill="url(#hist-${i})" rx="3" />`;
    bars += `<text x="${x + barW/2}" y="${padT + usableH + 18}" fill="#475569" font-size="12"
               font-family="SF Mono,monospace" text-anchor="middle" font-weight="600">${binLabels[i]}</text>`;
    if (c > 0) {
      bars += `<text x="${x + barW/2}" y="${y - 6}" fill="#0f172a" font-size="12"
                 font-family="SF Mono,monospace" text-anchor="middle" font-weight="700">${Math.round(pct)}%</text>`;
    }
  });

  return `
    <svg viewBox="0 0 ${svgW} ${svgH}" width="100%" height="${svgH}" preserveAspectRatio="none"
         style="display:block; max-width:100%" xmlns="http://www.w3.org/2000/svg">
      ${defs}
      ${grid}
      ${bars}
      <line x1="${padL}" y1="${padT + usableH}" x2="${svgW - padR}" y2="${padT + usableH}"
            stroke="rgba(15,23,42,0.25)" stroke-width="1" />
      <text x="${padL + usableW/2}" y="${svgH - 10}" fill="#64748b" font-size="11"
            font-family="SF Mono,monospace" text-anchor="middle" font-weight="500">
        pico de luz por escena · nits (escala logarítmica)
      </text>
    </svg>`;
}

/** Render del bloque "Información detallada HDR / Dolby Vision".
 *  Diseño compacto, profesional — se inserta DENTRO del card de Vídeo.
 *  Agrupa todos los parámetros DV+HDR en bloques temáticos densos con
 *  visualizadores inline. */
function _renderMkvDvRadiography(a, dv, mainVideo, elVideo) {
  const hdr = a.hdr || {};
  // FPS real desde el track de vídeo (mkvmerge default_duration → fps).
  // NO computamos fps = dv.frame_count / duration porque dv.frame_count
  // viene del extract-rpu --limit 720 (sample de ~30s, NO el total).
  const fpsNum = mainVideo?.fps || a.fps || 23.976;
  const fps = fpsNum.toFixed(3);

  // Helper inline: celda label+valor compacta
  const cell = (label, value, opts = {}) => {
    const tip = opts.tooltip ? ` data-tooltip="${escHtml(opts.tooltip)}"` : '';
    const cls = opts.status ? ` dv-cell-${opts.status}` : '';
    const v = (value == null || value === '') ? '—' : value;
    return `<div class="dv-cell${cls}"${tip}><span class="dv-cell-label">${label}</span><strong class="dv-cell-value">${v}</strong></div>`;
  };
  const pill = (present, label, value) => {
    const st = present ? 'ok' : 'off';
    const content = value && present
      ? `<span class="dv-pill-name">${label}</span><span class="dv-pill-val">${value}</span>`
      : `<span class="dv-pill-name">${label}</span>`;
    return `<div class="dv-pill dv-pill-${st}">${content}</div>`;
  };

  // ── DATA
  const el = dv.el_type ? ` ${dv.el_type}` : '';
  const profile = dv.profile ? `P${dv.profile}${el}${dv.profile_compatibility_id ? ` · compat ${dv.profile_compatibility_id}` : ''}` : '—';
  // framesTotal: prioridad al track de vídeo (real, derivado de duration×fps).
  // dv.frame_count es sample-scoped (--limit 720) — solo lo usamos como
  // último recurso si no tenemos ni duración ni fps.
  const framesTotal = mainVideo?.frame_count
    || (a.duration_seconds && fpsNum ? Math.round(a.duration_seconds * fpsNum) : 0)
    || dv.frame_count
    || 0;
  const durationStr = a.duration_seconds ? _fmtDuration(a.duration_seconds) : '—';
  // RPU: el bytes/frame del sample es estable y aplica al film completo.
  // Extrapolamos el tamano TOTAL como B/f × frames_total para tener un
  // valor representativo del MKV entero, no del sample de 30s.
  const rpuBytesPerFrame = dv.rpu_size_bytes && dv.frame_count
    ? Math.round(dv.rpu_size_bytes / dv.frame_count)
    : 0;
  const rpuSize = rpuBytesPerFrame && framesTotal
    ? `~${_fmtBytes(rpuBytesPerFrame * framesTotal)} · ${rpuBytesPerFrame} B/f`
    : (rpuBytesPerFrame ? `${rpuBytesPerFrame} B/f` : '—');
  const cmLabel = dv.cm_version ? dv.cm_version.toUpperCase() : '—';

  const hasLightProfile = Array.isArray(dv.per_scene_max_cll) && dv.per_scene_max_cll.length > 0;

  // L5 (active area)
  const frameW = mainVideo?.pixel_dimensions ? parseInt(mainVideo.pixel_dimensions.split('x')[0]) || 3840 : 3840;
  const frameH = mainVideo?.pixel_dimensions ? parseInt(mainVideo.pixel_dimensions.split('x')[1]) || 2160 : 2160;
  const activeW = frameW - (dv.l5_left || 0) - (dv.l5_right || 0);
  const activeH = frameH - (dv.l5_top || 0) - (dv.l5_bottom || 0);
  const aspectLabel = _rgrfAspectLabel(dv, frameW, frameH);

  // CMv4.0
  const cm = (dv.cm_version || '').toLowerCase();
  const isV40 = cm.includes('4.0') || cm.includes('v4');

  // ═══════════════════════════════════════════════════════════════
  // BLOQUE 0 · Auditoría de calidad (quality_*) — encabeza la radiografía
  // ═══════════════════════════════════════════════════════════════
  const blockQuality = _rgrfQualityAuditCard(dv, isV40);

  // ═══════════════════════════════════════════════════════════════
  // BLOQUE 1 · Stream (profile + timing + structure)
  // ═══════════════════════════════════════════════════════════════
  // Scene cuts + density si la auditoría profunda lo ha calculado.
  // Esto solo aparece cuando el usuario ha pulsado "Auditar calidad"
  // (los datos vienen del quality audit, no del análisis básico).
  const sceneCutsCell = (dv?.quality_scene_cuts || 0) > 0
    ? cell(
        'Scene cuts',
        `${dv.quality_scene_cuts.toLocaleString()} (~${
          (a.duration_seconds / dv.quality_scene_cuts).toFixed(1)
        }s/escena)`,
        { tooltip: 'Nº de frames con scene_refresh_flag en el RPU (cambios de plano detectados por el colorista). Aportado por la auditoría profunda.' }
      )
    : '';

  const blockStream = `
    <section class="dv-block">
      <h5 class="dv-block-title">Stream</h5>
      <div class="dv-grid-3">
        ${cell('Profile', profile)}
        ${cell('CM version', cmLabel)}
        ${cell('Frames', framesTotal ? framesTotal.toLocaleString() : '—', { tooltip: 'Total de frames del MKV (duración × FPS)' })}
        ${cell('Duración', durationStr)}
        ${cell('FPS', fps, { tooltip: 'FPS del track de vídeo (de mkvmerge default_duration)' })}
        ${cell('Bit depth', mainVideo?.bit_depth ? `${mainVideo.bit_depth}-bit` : '—')}
        ${cell('Codec', mainVideo?.codec || '—')}
        ${cell('RPU', rpuSize, { tooltip: 'Tamaño total estimado del RPU del MKV completo (bytes/frame medido en sample × frames totales). El bytes/frame es estable entre sample y total.' })}
        ${sceneCutsCell}
        ${elVideo ? cell('Enhancement Layer', `${escHtml(elVideo.codec || 'HEVC')} · ${escHtml(elVideo.pixel_dimensions || '')}`) : ''}
      </div>
    </section>`;

  // ═══════════════════════════════════════════════════════════════
  // BLOQUE 2 · Cadena de mastering (sustituye al antiguo bloque
  // "Luminancia" + bloque "Gamut CIE 1931"). Toda la info de primaries,
  // mastering display, container HEVC, DV L9/L10 y trim targets en una
  // sola ficha escaneable. La luminancia DV L1 dinámica vive en el
  // bloque del sparkline donde hay graficos + stats card; la HDR10
  // estatica se muestra aqui como dato del SEI.
  // ═══════════════════════════════════════════════════════════════
  const blockMastering = _rgrfMasteringChain(dv, hdr, mainVideo);

  // ═══════════════════════════════════════════════════════════════
  // BLOQUE 3 · Active area (L5) con visualizador lateral
  // ═══════════════════════════════════════════════════════════════
  const symV = (dv.l5_top || 0) === (dv.l5_bottom || 0);
  const symH = (dv.l5_left || 0) === (dv.l5_right || 0);
  // L5 zones del light profile: lista de zonas detectadas a lo largo del
  // film. Si hay >1 zona, el film tiene active area dinamica (letterbox
  // cambiante por escena, ej. partes IMAX en 1.43:1 vs 2.40:1 cinema).
  // Si solo hay 1 zona o no hay light profile, mostramos el L5 estatico
  // del sample como antes.
  const l5Zones = dv?.l1_references?.l5_zones || [];
  const hasZonedL5 = l5Zones.length > 1;

  let blockActiveArea;
  if (hasZonedL5) {
    // Render multi-zona: tabla con cada zona y su % de frames
    const zonesHtml = l5Zones.map((z, i) => {
      const aw = frameW - (z.left || 0) - (z.right || 0);
      const ah = frameH - (z.top || 0) - (z.bottom || 0);
      const ratio = ah > 0 ? (aw / ah).toFixed(2) : '—';
      return `
        <tr>
          <td>${i + 1}</td>
          <td><code>T${z.top}/B${z.bottom}/L${z.left}/R${z.right}</code></td>
          <td>${aw} × ${ah}</td>
          <td>${ratio}:1</td>
          <td>${z.frames.toLocaleString()}</td>
          <td><strong>${z.pct}%</strong></td>
        </tr>`;
    }).join('');
    blockActiveArea = `
      <section class="dv-block">
        <h5 class="dv-block-title">Active area
          <span class="dv-block-sub">L5 · ${l5Zones.length} zonas detectadas (letterbox dinámico)</span>
        </h5>
        <table class="dv-l5-zones-table">
          <thead><tr><th>#</th><th>Offsets (px)</th><th>Área activa</th><th>Ratio</th><th>Frames</th><th>%</th></tr></thead>
          <tbody>${zonesHtml}</tbody>
        </table>
      </section>`;
  } else {
    // Caso clasico: una sola zona (uniform letterbox). Si tenemos light
    // profile, usamos los offsets de la zona dominante; si no, los del
    // sample del extract-rpu.
    const z0 = l5Zones[0];
    const lTop = z0 ? z0.top : (dv.l5_top || 0);
    const lBot = z0 ? z0.bottom : (dv.l5_bottom || 0);
    const lLft = z0 ? z0.left : (dv.l5_left || 0);
    const lRgt = z0 ? z0.right : (dv.l5_right || 0);
    const aWi = frameW - lLft - lRgt;
    const aHi = frameH - lTop - lBot;
    const sV = lTop === lBot;
    const sH = lLft === lRgt;
    const subLabel = z0 ? 'L5 · validado en todo el film' : 'L5 · sample 30s (corre el perfil de luminancia para validar)';
    blockActiveArea = `
      <section class="dv-block">
        <h5 class="dv-block-title">Active area <span class="dv-block-sub">${subLabel}</span></h5>
        <div class="dv-split">
          <div class="dv-grid-2">
            ${cell('Offsets T / B', `${lTop} / ${lBot} px`)}
            ${cell('Offsets L / R', `${lLft} / ${lRgt} px`)}
            ${cell('Área activa', `${aWi} × ${aHi}`)}
            ${cell('Aspect ratio', aspectLabel)}
            ${cell('Simetría vertical', sV ? 'T = B' : `Δ ${Math.abs(lTop - lBot)} px`, { status: sV ? 'ok' : 'warn' })}
            ${cell('Simetría horizontal', sH ? 'L = R' : `Δ ${Math.abs(lLft - lRgt)} px`, { status: sH ? 'ok' : 'warn' })}
          </div>
          <div class="dv-viz-side">${_rgrfL5Svg(dv, frameW, frameH)}</div>
        </div>
      </section>`;
  }

  // ═══════════════════════════════════════════════════════════════
  // BLOQUE 4 · CMv4.0 levels (solo si v4.0).
  // Slim: solo presencia de los levels — los datos concretos (L9/L10
  // primaries, L11 content type) ya estan en la cadena de mastering.
  // L8 trim targets en nits se mantienen aqui con su visualizacion
  // logarítmica porque es un grafico especifico del L8.
  // ═══════════════════════════════════════════════════════════════
  let blockCmv4 = '';
  if (isV40) {
    // Preferir L8 trims del light profile (full movie) sobre los del
    // sample. Capta target_display_index distintos que solo aparecen
    // en frames mid/late del film.
    const l8FromLightProfile = dv?.l1_references?.l8_trim_nits_full;
    const l8Effective = (Array.isArray(l8FromLightProfile) && l8FromLightProfile.length)
      ? l8FromLightProfile
      : (dv.l8_trim_nits || []);
    const nitsLabel = (l8Effective && l8Effective.length)
      ? l8Effective.join(' · ') + ' nits'
      : (dv.l8_trim_count ? `${dv.l8_trim_count} trims` : '');
    // Mini-tabla cuantitativa si hay quality audit. Sustituye al "info
    // binaria solamente" de las pills con datos concretos del L8/L2.
    const hasQuality = !!dv?.quality_classification;
    const cmv4StatsTable = hasQuality ? `
      <div class="dv-cmv4-stats-table">
        <div class="dv-cmv4-stats-row">
          <div class="dv-cmv4-stats-key">L8</div>
          <div class="dv-cmv4-stats-val">
            <strong>${(dv.quality_l8_unique_count || 0).toLocaleString()}</strong> combos únicos
            <span class="dv-cmv4-stats-sub">
              ${dv.quality_scene_cuts > 0
                ? `· ${(dv.quality_l8_unique_count / dv.quality_scene_cuts).toFixed(2)} combos/shot`
                : ''}
              ${dv.quality_l8_neutral_pct != null
                ? ` · ${Math.round(dv.quality_l8_neutral_pct * 100)}% frames neutros`
                : ''}
              ${dv.quality_l8_has_mid_contrast ? ' · <code>mid_contrast</code>' : ''}
              ${dv.quality_l8_has_clip_trim ? ' · <code>clip_trim</code>' : ''}
            </span>
          </div>
        </div>
        <div class="dv-cmv4-stats-row">
          <div class="dv-cmv4-stats-key">L2</div>
          <div class="dv-cmv4-stats-val">
            <strong>${(dv.quality_l2_unique_count || 0).toLocaleString()}</strong> combos únicos
            ${(dv.quality_l2_target_pqs?.length || 0) > 0
              ? `<span class="dv-cmv4-stats-sub">· ${dv.quality_l2_target_pqs.length} target_pqs</span>`
              : ''}
          </div>
        </div>
      </div>` : '';

    blockCmv4 = `
      <section class="dv-block">
        <h5 class="dv-block-title">CMv4.0 levels extendidos
          <span class="dv-block-sub">presencia · L9/L10/L11 detallados en cadena de mastering</span>
        </h5>
        <div class="dv-pill-row">
          ${pill(dv.has_l3,  'L3',  'local scene trim')}
          ${pill(dv.has_l4,  'L4',  'legacy compat trim')}
          ${pill(dv.has_l8,  'L8',  nitsLabel)}
          ${pill(dv.has_l9,  'L9',  'source primaries')}
          ${pill(dv.has_l10, 'L10', 'target primaries')}
          ${pill(dv.has_l11, 'L11', 'content type')}
          ${pill(dv.has_l254,'L254', 'CMv4.0 marker')}
        </div>
        ${cmv4StatsTable}
        ${l8Effective && l8Effective.length ? `
          <div class="dv-viz-inline">
            <div class="dv-viz-caption">L8 target displays · escala logarítmica de nits${l8FromLightProfile && l8FromLightProfile.length ? ' · validado film completo' : ' · sample 30s'}</div>
            ${_rgrfL8Svg(l8Effective)}
          </div>` : ''}
      </section>`;
  }

  // BLOQUE 5 ELIMINADO — la antigua "Gamut CIE 1931" se sustituyo por la
  // cadena de mastering (BLOQUE 2) que muestra textualmente toda la info de
  // primaries y trim targets. El diagrama CIE no aportaba info nueva en UHD
  // BD donde casi siempre coincide BT.2020 container + P3/2020 master.

  // ═══════════════════════════════════════════════════════════════
  // BLOQUE 6 · Perfil de luminancia (sparkline + distribución) + botón
  // ═══════════════════════════════════════════════════════════════
  const lightMeta = hasLightProfile
    ? `${dv.per_scene_max_cll.length} buckets · max ${Math.max(...dv.per_scene_max_cll)} nits`
    : '';
  // Referencias del RPU + HDR10 del container para overlay
  const sparkRefs = hasLightProfile ? {
    ...((dv.l1_references || {})),
    hdr10_max_cll:  a.hdr?.max_cll  || 0,
    hdr10_max_fall: a.hdr?.max_fall || 0,
  } : {};
  const sparkOpts = hasLightProfile ? {
    avgSeries: dv.per_scene_max_fall && dv.per_scene_max_fall.length === dv.per_scene_max_cll.length
      ? dv.per_scene_max_fall : null,
    minSeries: dv.per_scene_min && dv.per_scene_min.length === dv.per_scene_max_cll.length
      ? dv.per_scene_min : null,
    refs: sparkRefs,
  } : {};
  // Mini-card de stats (percentiles + clasificacion por brillo)
  const statsCardHtml = hasLightProfile && dv.l1_stats
    ? _rgrfL1StatsCard(dv.l1_stats, a.hdr)
    : '';
  const sparklineArea = hasLightProfile
    ? `<div class="dv-chart-large">${_rgrfSparklineSvg(dv.per_scene_max_cll, Math.max(...dv.per_scene_max_cll) + ' nits', a.duration_seconds, sparkOpts)}</div>
       ${statsCardHtml}
       <div class="dv-chart-large">${_rgrfDistributionSvg(dv.per_scene_max_cll)}</div>`
    : `<div class="dv-chart-empty">
         <div class="dv-chart-empty-icon">📊</div>
         <div class="dv-chart-empty-text">Análisis per-escena no generado</div>
         <div class="dv-chart-empty-hint">Extrae MaxCLL y MaxFALL frame-a-frame del movie completo para visualizar la curva de luminancia y su distribución. ~3-5 min en UHD BDs.</div>
       </div>`;
  const actionBtn = hasLightProfile
    ? `<button class="btn btn-ghost btn-sm dv-chart-action" onclick="_rgrfAnalyzeLight(event)" data-tooltip="Re-analizar si el MKV cambió"><span>↻</span> Re-analizar</button>`
    : `<button class="btn btn-primary btn-sm dv-chart-action" onclick="_rgrfAnalyzeLight(event)"><span>▶</span> Analizar ahora</button>`;
  // Tooltip explicando que estos valores son metadata DV L1 (no medidas
  // reales en pantalla). Para BR2049 nuestro peak es ~176 nits aunque
  // medidas reales tras tone-mapping sean 500-600 nits — porque el
  // colorista etiqueto conservadoramente. Confirmado: dovi_tool info
  // --summary reporta el mismo MaxCLL.
  const lightHint = hasLightProfile
    ? `<span class="dv-block-hint" data-tooltip="Valores extraídos del bloque L1 del RPU Dolby Vision (peak/avg de PQ por escena, según etiquetó el colorista). No son medidas reales en pantalla — un disco conservadoramente mastered (BR2049, p.ej.) puede mostrar peaks de metadata bajos aunque la imagen real alcance valores mayores tras tone-mapping. Coincide exactamente con dovi_tool info --summary.">ℹ︎</span>`
    : '';
  const blockLight = `
    <section class="dv-block">
      <div class="dv-block-head">
        <h5 class="dv-block-title">Perfil de luminancia DV L1 por escena ${lightHint} <span class="dv-block-sub">metadata max_pq · no luminancia real en pantalla</span></h5>
        <div class="dv-block-action">
          ${lightMeta ? `<span class="dv-block-meta">${lightMeta}</span>` : ''}
          ${actionBtn}
        </div>
      </div>
      ${sparklineArea}
    </section>`;

  // ═══════════════════════════════════════════════════════════════
  //  Ensamblaje con toolbar superior compacta
  // ═══════════════════════════════════════════════════════════════
  return `
    <div class="dv-detail">
      <div class="dv-detail-header">
        <h4 class="dv-detail-title">Información detallada HDR / Dolby Vision</h4>
        <button class="btn btn-ghost btn-sm" onclick="_rgrfCopyToClipboard(event)"
                data-tooltip="Copia toda la información como Markdown">📋 Copiar</button>
      </div>
      ${blockQuality}
      ${blockStream}
      ${blockMastering}
      ${blockActiveArea}
      ${blockCmv4}
      ${blockLight}
    </div>`;
}

/**
 * Card de auditoría de calidad del RPU. Dos estados:
 *  - Sin datos (quality_classification vacío): CTA "Auditar calidad"
 *  - Con datos: badge color + verdict + 4 mini-stats + descripción técnica
 *
 * El usuario pulsa la CTA → pipeline backend de 5-10 min → la card se
 * re-renderiza poblada. El resultado se persiste en el cache MKV, así
 * que re-abrir el MKV muestra la card directamente.
 */
function _rgrfQualityAuditCard(dv, isV40) {
  const cls = dv?.quality_classification || '';
  const hasAudit = !!cls;

  if (!hasAudit) {
    // Estado "no auditado" — CTA
    const cmLabel = isV40 ? 'CMv4.0' : (dv?.cm_version ? dv.cm_version.toUpperCase() : 'CMv2.9');
    return `
      <section class="dv-block dv-quality-card dv-quality-empty">
        <div class="dv-quality-empty-icon">🔬</div>
        <div class="dv-quality-empty-body">
          <div class="dv-quality-empty-title">Auditoría de calidad ${cmLabel}</div>
          <div class="dv-quality-empty-text">
            Extrae todos los combos L8/L2 del RPU completo del MKV y los clasifica
            (FULL / CORE+ / CORE / sintético). Identifica si el master que tienes
            es de calidad de referencia o si fue generado algorítmicamente.
          </div>
          <button class="btn btn-primary btn-sm dv-quality-cta"
                  onclick="_rgrfAuditQuality(event)">
            <span>🔬</span> Auditar calidad (~5-10 min)
          </button>
          <div class="dv-quality-empty-hint">
            El MKV no se modifica · ficheros intermedios borrados al terminar
          </div>
        </div>
      </section>`;
  }

  // Estado poblado
  const colorMap = {
    green:  { badge: '🟢', cls: 'dv-q-green' },
    yellow: { badge: '🟡', cls: 'dv-q-yellow' },
    red:    { badge: '🔴', cls: 'dv-q-red' },
    gray:   { badge: '⚪', cls: 'dv-q-gray' },
  };
  const color = colorMap[dv.quality_verdict_color] || colorMap.gray;
  const verdict = dv.quality_verdict_text || '—';
  const tierLabel = dv.quality_tier_label || '';
  const reason = dv.quality_reason || dv.quality_tier_description || '';

  // 4 mini-stats
  const l8Count = dv.quality_l8_unique_count || 0;
  const l2Count = dv.quality_l2_unique_count || 0;
  const scenes = dv.quality_scene_cuts || 0;
  const totalFrames = dv.quality_total_frames_rpu || 0;
  const cmv40Frames = dv.quality_frames_with_cmv40 || 0;
  const cmv40Pct = totalFrames > 0 ? Math.round(cmv40Frames * 100 / totalFrames) : 0;
  const combosPerShot = scenes > 0 ? (l8Count / scenes).toFixed(2) : '—';

  return `
    <section class="dv-block dv-quality-card ${color.cls}">
      <div class="dv-quality-header">
        <div class="dv-quality-badge">${color.badge}</div>
        <div class="dv-quality-head-body">
          <div class="dv-quality-verdict">${escHtml(verdict)}</div>
          ${tierLabel ? `<div class="dv-quality-tier">${escHtml(tierLabel)}</div>` : ''}
        </div>
        <button class="btn btn-ghost btn-xs dv-quality-reaudit"
                onclick="_rgrfAuditQuality(event)"
                data-tooltip="Re-auditar (5-10 min). Útil si el clasificador mejoró.">↻ Re-auditar</button>
      </div>
      <div class="dv-quality-stats">
        <div class="dv-quality-stat">
          <div class="dv-quality-stat-value">${l8Count.toLocaleString()}</div>
          <div class="dv-quality-stat-label">combos L8 únicos</div>
        </div>
        <div class="dv-quality-stat">
          <div class="dv-quality-stat-value">${l2Count.toLocaleString()}</div>
          <div class="dv-quality-stat-label">combos L2 únicos</div>
        </div>
        <div class="dv-quality-stat">
          <div class="dv-quality-stat-value">${scenes.toLocaleString()}</div>
          <div class="dv-quality-stat-label">scene cuts <span style="opacity:.6">(~${combosPerShot} L8/shot)</span></div>
        </div>
        <div class="dv-quality-stat">
          <div class="dv-quality-stat-value">${cmv40Pct}%</div>
          <div class="dv-quality-stat-label">cobertura CMv4.0</div>
        </div>
      </div>
      ${(dv.quality_provenance_hints?.length || 0) > 0 ? `
        <div class="dv-quality-hints">
          <div class="dv-quality-hints-label">Procedencia probable</div>
          <ul class="dv-quality-hints-list">
            ${dv.quality_provenance_hints.map(h => `<li>${escHtml(h)}</li>`).join('')}
          </ul>
        </div>` : ''}
      ${reason ? `<details class="dv-quality-details">
        <summary>Detalle técnico</summary>
        <div class="dv-quality-reason">${escHtml(reason)}</div>
      </details>` : ''}
    </section>`;
}

/**
 * Dispara la auditoría de calidad del MKV abierto. Igual patrón que
 * _rgrfAnalyzeLight: modal de progreso con steps + polling.
 */
async function _rgrfAuditQuality(evt) {
  if (!mkvProject) return;
  const fileEl = document.getElementById('mkv-quality-modal-file');
  if (fileEl) fileEl.textContent = mkvProject.analysis.file_name;
  _mkvQualityResetSteps();
  _mkvQualitySetProgress(0);
  _mkvQualitySetElapsed(0);
  const logEl = document.getElementById('mkv-quality-log');
  if (logEl) logEl.innerHTML = '';
  openModal('mkv-quality-modal');

  let lastLogCount = 0;
  let polling = true;
  window._mkvQualitySession = { ctrl: null, polledResult: null, cancelledByUser: false };

  async function _pollLoop() {
    while (polling) {
      try {
        const st = await apiFetch('/api/mkv/quality-audit/progress', { silent: true });
        if (!polling) return;
        if (st) {
          _mkvQualitySetStep(st.step);
          _mkvQualitySetProgress(st.global_pct || 0);
          _mkvQualitySetElapsed(st.elapsed_s || 0);
          const lines = Array.isArray(st.log_lines) ? st.log_lines : [];
          if (lines.length > lastLogCount && logEl) {
            const newLines = lines.slice(lastLogCount);
            // Reusa _appendLogLine que ya implementa scroll inteligente
            // (_isScrolledNearBottom) y aplica la paleta semántica de
            // .cmv40-log (clasifica por markers ━━━ / $ / 📋 / 🎯 / ✓ / ✗).
            // Misma UX que el log del overlay de CMv4.0 — paridad visual.
            for (const line of newLines) _appendLogLine(logEl, line);
            lastLogCount = lines.length;
          }
          if (st.active === false && (st.result || st.error)) {
            window._mkvQualitySession.polledResult = st.result || null;
            try { window._mkvQualitySession.ctrl?.abort(); } catch (_) {}
            polling = false;
            return;
          }
        }
      } catch (_) { /* silencioso */ }
      await new Promise(r => setTimeout(r, 1500));
    }
  }
  _pollLoop();

  try {
    let data = null;
    let postError = null;
    {
      const ctrl = new AbortController();
      window._mkvQualitySession.ctrl = ctrl;
      const POST_TIMEOUT_MS = 3600000;  // 1h
      const timer = setTimeout(() => ctrl.abort(), POST_TIMEOUT_MS);
      try {
        const resp = await fetch('/api/mkv/quality-audit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            file_path: mkvProject.analysis.file_path || mkvProject.filePath || mkvProject.analysis.file_name,
          }),
          signal: ctrl.signal,
        });
        if (resp.ok) {
          data = await resp.json();
        } else {
          const err = await resp.json().catch(() => ({ detail: resp.statusText }));
          postError = err.detail || resp.statusText;
        }
      } catch (e) {
        postError = e.name === 'AbortError'
          ? `Timeout tras ${POST_TIMEOUT_MS / 1000}s`
          : (e.message || String(e));
      } finally {
        clearTimeout(timer);
      }
    }
    // Fallback via polling
    if (!data?.quality_classification) {
      const polled = window._mkvQualitySession?.polledResult;
      if (polled && polled.quality_classification) {
        data = polled;
      } else {
        for (let i = 0; i < 20; i++) {
          await new Promise(r => setTimeout(r, 1500));
          const st = await apiFetch('/api/mkv/quality-audit/progress', { silent: true });
          if (st && st.result && st.result.quality_classification) {
            data = st.result;
            break;
          }
          if (st && !st.active && st.error) throw new Error(st.error);
          if (st && !st.active && st.step === 'done') break;
        }
      }
    }
    if (!data?.quality_classification) {
      throw new Error(postError || 'respuesta vacía del servidor');
    }

    _mkvQualitySetProgress(100);
    if (!mkvProject || !mkvProject.analysis) {
      throw new Error('El MKV se cerró durante la auditoría — vuelve a abrirlo');
    }
    if (!mkvProject.analysis.dovi) mkvProject.analysis.dovi = {};
    Object.assign(mkvProject.analysis.dovi, data);
    await new Promise(r => setTimeout(r, 500));
    closeModal('mkv-quality-modal');
    _renderMkvEditPanel();
    showToast(`Auditoría completada — ${data.quality_verdict_text}`, 'success');
  } catch (e) {
    if (window._mkvQualitySession?.cancelledByUser) {
      closeModal('mkv-quality-modal');
      showToast('🛑 Auditoría cancelada', 'info');
      return;
    }
    const errMsg = e?.message || String(e);
    if (logEl) {
      // _appendLogLine clasifica automáticamente por marcador "✗" como log-error
      _appendLogLine(logEl, `✗ Error: ${errMsg}`);
    }
    showToast(`Error auditoría: ${errMsg}`, 'error', 8000);
    // Inyectar botón "Cerrar" para que el usuario lea el error sin presión
    if (!document.getElementById('mkv-quality-error-close-btn')) {
      const footer = document.querySelector('#mkv-quality-modal .modal-footer');
      if (footer) {
        const btn = document.createElement('button');
        btn.id = 'mkv-quality-error-close-btn';
        btn.className = 'btn btn-ghost btn-sm';
        btn.textContent = 'Cerrar';
        btn.onclick = () => { closeModal('mkv-quality-modal'); btn.remove(); };
        footer.appendChild(btn);
      }
    }
    // Deshabilita el botón cancelar (ya no aplica)
    const cancelBtn = document.getElementById('mkv-quality-cancel-btn');
    if (cancelBtn) cancelBtn.style.display = 'none';
  } finally {
    polling = false;
    if (window._mkvQualitySession) {
      window._mkvQualitySession.ctrl = null;
      window._mkvQualitySession.polledResult = null;
      window._mkvQualitySession.cancelledByUser = false;
    }
  }
}

async function _mkvQualityCancel() {
  const btn = document.getElementById('mkv-quality-cancel-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Cancelando…'; }
  if (window._mkvQualitySession) window._mkvQualitySession.cancelledByUser = true;
  try {
    await apiFetch('/api/mkv/quality-audit/cancel', { method: 'POST', silent: true });
  } catch (_) {}
  try { window._mkvQualitySession?.ctrl?.abort(); } catch (_) {}
}

function _mkvQualityResetSteps() {
  ['ffmpeg', 'extract_rpu', 'combos'].forEach((s, i) => {
    const el = document.getElementById(`mkv-quality-step-${s}`);
    if (!el) return;
    el.style.opacity = i === 0 ? '1' : '.4';
    el.textContent = el.textContent.replace(/^[✅⏳⬜✗]\s*/, i === 0 ? '⏳ ' : '⬜ ');
  });
  // Resetear el guard monotónico de step para que una re-auditoría tras
  // error vuelva a reconocer "ffmpeg" como step inicial (sin esto, si la
  // sesión previa quedó en step "combos"/"error", la nueva llegaba con
  // "ffmpeg" y se ignoraba como duplicado).
  _mkvQualityLastStep = '';
  const cancelBtn = document.getElementById('mkv-quality-cancel-btn');
  if (cancelBtn) { cancelBtn.disabled = false; cancelBtn.textContent = '🛑 Cancelar'; cancelBtn.style.display = ''; }
  const closeBtn = document.getElementById('mkv-quality-error-close-btn');
  if (closeBtn) closeBtn.remove();
}

let _mkvQualityLastStep = '';
function _mkvQualitySetStep(step) {
  if (!step || step === _mkvQualityLastStep) return;
  const order = ['ffmpeg', 'extract_rpu', 'combos', 'done', 'error'];
  const idx = order.indexOf(step);
  if (idx < 0) return;
  _mkvQualityLastStep = step;
  ['ffmpeg', 'extract_rpu', 'combos'].forEach((s, i) => {
    const el = document.getElementById(`mkv-quality-step-${s}`);
    if (!el) return;
    if (step === 'done' || i < idx) {
      el.style.opacity = '1';
      el.textContent = el.textContent.replace(/^[⏳⬜✅✗]\s*/, '✅ ');
    } else if (i === idx) {
      el.style.opacity = '1';
      el.textContent = el.textContent.replace(/^[⏳⬜✅✗]\s*/, '⏳ ');
    } else {
      el.style.opacity = '.4';
      el.textContent = el.textContent.replace(/^[⏳⬜✅✗]\s*/, '⬜ ');
    }
  });
}

function _mkvQualitySetProgress(pct) {
  const clamped = Math.max(0, Math.min(100, pct));
  const bar = document.getElementById('mkv-quality-progress-bar');
  const txt = document.getElementById('mkv-quality-pct');
  if (bar) bar.style.width = `${clamped}%`;
  if (txt) txt.textContent = `${Math.round(clamped)}%`;
}

function _mkvQualitySetElapsed(secs) {
  const el = document.getElementById('mkv-quality-elapsed');
  if (!el) return;
  const s = Math.max(0, Math.floor(secs));
  el.textContent = `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

/** Copia la radiografía como Markdown al portapapeles. */
async function _rgrfCopyToClipboard(evt) {
  if (!mkvProject) return;
  const a = mkvProject.analysis;
  const dv = a.dovi;
  const hdr = a.hdr || {};
  if (!dv) return;

  const fmt = (v, suf = '') => (v != null && v !== '') ? `${v}${suf}` : '—';
  const el = dv.el_type ? ` ${dv.el_type}` : '';
  const levels = [];
  [['L1', dv.has_l1], ['L2', dv.has_l2], ['L3', dv.has_l3], ['L4', dv.has_l4],
   ['L5', dv.has_l5], ['L6', dv.has_l6], ['L8', dv.has_l8], ['L9', dv.has_l9],
   ['L10', dv.has_l10], ['L11', dv.has_l11], ['L254', dv.has_l254]]
    .forEach(([k, v]) => { if (v) levels.push(k); });

  // Frames y FPS reales del track de video (no del sample de 30s del dovi)
  const mainV = a.tracks?.find(t => t.type === 'video');
  const realFrames = mainV?.frame_count;
  const realFps = mainV?.fps;

  const md = [
    `# Radiografía DV+HDR — ${a.file_name}`,
    ``,
    `**Tamaño:** ${_fmtBytes(a.file_size_bytes)} · **Duración:** ${_fmtDuration(a.duration_seconds)}`,
    ``,
    `## 1. Identidad`,
    `- Profile: **${fmt(dv.profile)}${el}**`,
    `- CM version: **${fmt(dv.cm_version)}**`,
    `- Frames totales: ${fmt(realFrames?.toLocaleString())}`,
    `- FPS: ${fmt(realFps?.toFixed(3))}`,
    `- Bit depth: ${fmt(mainV?.bit_depth, '-bit')}`,
    `- Niveles detectados: ${levels.join(' · ')}`,
    ``,
    `## 2. HDR10 base`,
    `- Formato: ${fmt(hdr.hdr_format)}`,
    `- Primaries: ${fmt(hdr.color_primaries)}`,
    `- Transfer: ${fmt(hdr.transfer_characteristics)}`,
    `- MaxCLL / MaxFALL: ${fmt(hdr.max_cll, ' nits')} / ${fmt(hdr.max_fall, ' nits')}`,
    `- Mastering: ${fmt(hdr.mastering_display_luminance)}`,
    ``,
    `## 3. L1 dinámico`,
    `- MaxCLL avg: ${fmt(dv.l1_max_cll?.toFixed(2), ' nits')}`,
    `- MaxFALL avg: ${fmt(dv.l1_max_fall?.toFixed(2), ' nits')}`,
    ``,
    `## 4. L5 Active area`,
    `- Offsets: top ${dv.l5_top||0} · bottom ${dv.l5_bottom||0} · left ${dv.l5_left||0} · right ${dv.l5_right||0} px`,
    `- Aspect: ${_rgrfAspectLabel(dv)}`,
    ``,
    `## 5. L6 Mastering`,
    `- MaxCLL / MaxFALL: ${fmt(dv.l6_max_cll, ' nits')} / ${fmt(dv.l6_max_fall, ' nits')}`,
    ``,
    `## 6. CMv4.0 levels`,
    `- L3: ${dv.has_l3 ? '✓' : '✗'} · L4: ${dv.has_l4 ? '✓' : '✗'} · L8: ${dv.has_l8 ? '✓' : '✗'} · L9: ${dv.has_l9 ? '✓' : '✗'} · L10: ${dv.has_l10 ? '✓' : '✗'} · L11: ${dv.has_l11 ? '✓' : '✗'} · L254: ${dv.has_l254 ? '✓' : '✗'}`,
    `- L8 trims: ${dv.l8_trim_nits?.length ? dv.l8_trim_nits.join(' · ') + ' nits' : (dv.l8_trim_count || '—')}`,
    `- L9 primaries: ${fmt(dv.l9_primaries)}`,
    `- L10 primaries: ${fmt(dv.l10_primaries)}`,
    `- L11 content: ${fmt(dv.l11_content_type)}${dv.l11_intended_application ? ` (${dv.l11_intended_application})` : ''}`,
    ``,
  ].join('\n');

  const ok = await _copyTextToClipboardWithFallback(md);
  showToast(ok ? '✓ Radiografía copiada como Markdown' : 'No se pudo copiar al portapapeles', ok ? 'success' : 'error');
}

/** Lanza el análisis del perfil de luminancia del movie completo.
 *  Polling del endpoint /progress cada 1.5s para barra + mini log en vivo. */
async function _rgrfAnalyzeLight(evt) {
  if (!mkvProject) return;

  // Inicializa UI del modal
  const fileEl = document.getElementById('dv-light-modal-file');
  if (fileEl) fileEl.textContent = mkvProject.analysis.file_name;
  _dvLightLastStep = 0;       // monotonic step guard — reset por sesión
  _dvLightLastPct = 0;        // idem para progreso global
  _dvLightSetStep(1);
  _dvLightSetProgress(0);
  _dvLightSetElapsed(0);
  const logEl = document.getElementById('dv-light-log');
  if (logEl) logEl.innerHTML = '';
  openModal('dv-light-modal');

  // Polling del backend para el estado real. Usamos chained await (no
  // setInterval) para evitar que se solapen peticiones en vuelo: bajo
  // carga del NAS una petición lenta podía responder DESPUÉS de una más
  // reciente, y la respuesta vieja con step=2 pisaba la actual con step=3.
  // Resultado: el modal se quedaba "Extrayendo RPU" mientras el log
  // mostraba ya "Exportando JSON". Con chained await solo hay 1 fetch
  // en vuelo a la vez → orden monotónico garantizado.
  let lastLogCount = 0;
  let polling = true;
  // AbortController del POST se expone via _dvLightSession para que el
  // handler de cancel y el polling lo aborten cuando el backend ya
  // termino (recuperacion tras Mac sleep — el POST pendiente puede
  // quedar colgado pero el polling ve el resultado).
  window._dvLightSession = { ctrl: null, polledResult: null };
  const pollTicker = { _stop: () => { polling = false; } };
  async function _pollLoop() {
    while (polling) {
      try {
        const st = await apiFetch('/api/mkv/light-profile/progress', { silent: true });
        if (!polling) return;
        if (st) {
          if (st.step >= 1 && st.step <= 4) _dvLightSetStep(st.step);
          _dvLightSetProgress(st.global_pct || 0);
          _dvLightSetElapsed(st.elapsed_s || 0);
          const lines = Array.isArray(st.log_lines) ? st.log_lines : [];
          if (lines.length > lastLogCount && logEl) {
            const newLines = lines.slice(lastLogCount);
            const wasAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 12;
            for (const line of newLines) {
              const div = document.createElement('div');
              div.className = 'dv-light-log-line';
              if (/Paso \d\/3/.test(line)) div.classList.add('step');
              if (line.includes('✓ Listo') || line.includes('✓ export') || line.includes('✓ ffmpeg') || line.includes('✓ RPU')) div.classList.add('done');
              if (st.error && line.includes(st.error)) div.classList.add('error');
              div.textContent = line;
              logEl.appendChild(div);
            }
            if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;
            lastLogCount = lines.length;
          }
          // Recuperacion tras sleep: si el backend ya termino (active=false
          // con result o error) pero el POST sigue colgado, abortamos el
          // POST para desbloquear el await — el catch path usa el resultado
          // del polling como fallback. Esto cubre el caso "Mac sleep durante
          // analisis → wake → POST nunca resuelve aunque backend ya acabo".
          if (st.active === false && (st.result || st.error)) {
            window._dvLightSession.polledResult = st.result || null;
            try { window._dvLightSession.ctrl?.abort(); } catch (_) {}
            polling = false;
            return;
          }
        }
      } catch (_) { /* silencioso */ }
      await new Promise(r => setTimeout(r, 1500));
    }
  }
  _pollLoop();

  try {
    // Timeout 60 min — antes era 25 min y un MKV de 63 GB con 238k frames
    // puede tardar ~25 min (ffmpeg ~6 min + extract-rpu ~6 min + dovi_tool
    // export ~13 min). El abort llegaba segundos antes de que el backend
    // respondiera. Si el caso real supera 60 min, el polling puede recoger
    // el resultado del state.result via fallback (ver catch).
    // Enviamos la ruta ABSOLUTA (file_path), no el filename. Antes el
    // backend prefijaba /mnt/output asumiendo que solo se inspeccionaban
    // ficheros del converter; con el browser de Library/Output ahora la
    // ruta completa la determina el frontend en analyze_mkv.
    //
    // Usamos fetch() crudo (no apiFetch) porque apiFetch dispara un toast
    // automatico si la respuesta falla. Como tenemos fallback via polling,
    // el toast prematuro confunde al usuario ("Error de red" mientras el
    // perfil se renderiza correctamente). Manejamos el error in-line.
    const POST_TIMEOUT_MS = 3600000;
    let data = null;
    let postError = null;
    {
      const ctrl = new AbortController();
      // Expone el ctrl al polling para que pueda abortar cuando vea el
      // backend completado (Mac sleep recovery) y al handler de cancel.
      window._dvLightSession.ctrl = ctrl;
      const timer = setTimeout(() => ctrl.abort(), POST_TIMEOUT_MS);
      try {
        const resp = await fetch('/api/mkv/light-profile', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file_path: mkvProject.analysis.file_path || mkvProject.filePath || mkvProject.analysis.file_name }),
          signal: ctrl.signal,
        });
        if (resp.ok) {
          data = await resp.json();
        } else {
          const err = await resp.json().catch(() => ({ detail: resp.statusText }));
          postError = err.detail || resp.statusText;
        }
      } catch (e) {
        // Aborto, fallo de red, etc. Guardamos el motivo pero no toasteamos
        // todavia — vamos a intentar recuperar via polling antes.
        postError = e.name === 'AbortError'
          ? `Timeout tras ${POST_TIMEOUT_MS / 1000}s`
          : (e.message || String(e));
      } finally {
        clearTimeout(timer);
      }
    }
    // Fallback: si el POST fallo o devolvio sin datos, el backend pudo
    // terminar igualmente y el resultado vive en state.result.
    // 1) Si el polling ya capturo el resultado (recuperacion sleep), usalo.
    // 2) Si no, sondea unas veces mas para darle tiempo a finalizar.
    if (!data?.per_scene_max_cll) {
      const polled = window._dvLightSession?.polledResult;
      if (polled && polled.per_scene_max_cll) {
        data = polled;
      } else {
        for (let i = 0; i < 20; i++) {
          await new Promise(r => setTimeout(r, 1500));
          const st = await apiFetch('/api/mkv/light-profile/progress', { silent: true });
          if (st && st.result && st.result.per_scene_max_cll) {
            data = st.result;
            break;
          }
          if (st && !st.active && st.error) {
            throw new Error(st.error);
          }
          if (st && !st.active && st.step === 4) {
            // Backend marca terminado pero no hay result — caso raro
            break;
          }
        }
      }
    }
    // Si tras el fallback seguimos sin datos, ahora SI fallamos con el
    // motivo del POST original.
    if (!data?.per_scene_max_cll) {
      throw new Error(postError || 'respuesta vacía del servidor');
    }

    _dvLightSetStep(4);
    _dvLightSetProgress(100);

    // Guard: si el analyze inicial detecto dovi=null (raro pero posible si
    // _run_dovi_on_mkv fallo silenciosamente), inicializamos el dict para no
    // crashear al asignar. Antes este TypeError se tragaba en el catch que
    // cerraba el modal sin error visible — el usuario veia "parado sin
    // resultado" cuando en realidad el backend habia devuelto 200 OK.
    if (!mkvProject || !mkvProject.analysis) {
      throw new Error('El MKV se cerró durante el análisis — vuelve a abrirlo');
    }
    if (!mkvProject.analysis.dovi) mkvProject.analysis.dovi = {};
    mkvProject.analysis.dovi.per_scene_max_cll = data.per_scene_max_cll;
    mkvProject.analysis.dovi.per_scene_max_fall = data.per_scene_max_fall || [];
    mkvProject.analysis.dovi.per_scene_min     = data.per_scene_min || [];
    mkvProject.analysis.dovi.l1_stats          = data.stats || null;
    mkvProject.analysis.dovi.l1_references     = data.references || null;

    await new Promise(r => setTimeout(r, 700));
    closeModal('dv-light-modal');
    _renderMkvEditPanel();
    showToast(
      `Perfil extraído — ${data.total_frames?.toLocaleString() || data.per_scene_max_cll.length} frames · ${data.per_scene_max_cll.length} buckets`,
      'success'
    );
  } catch (e) {
    // Cancelación explícita por el usuario — cierre directo sin tratar
    // como error. Toast informativo + closeModal. NO inyectamos el botón
    // "Cerrar" (sería redundante: el usuario ya cliqueó Cancelar).
    if (window._dvLightSession?.cancelledByUser) {
      closeModal('dv-light-modal');
      showToast('🛑 Análisis cancelado', 'info');
      return;
    }
    // No cerramos el modal automaticamente — antes lo haciamos a los 2.2s y
    // si el usuario no estaba mirando perdia el error y veia "parado sin
    // resultado". Ahora lo dejamos abierto con el error inyectado al log
    // del modal y un boton "Cerrar" explicito para descartar.
    const activeStep = document.querySelector('.dv-light-step.active');
    if (activeStep) {
      activeStep.classList.remove('active');
      activeStep.classList.add('error');
      const marker = activeStep.querySelector('.dv-light-step-marker');
      if (marker) marker.textContent = '✗';
    }
    const errMsg = e?.message || String(e);
    if (logEl) {
      const div = document.createElement('div');
      div.className = 'dv-light-log-line error';
      div.textContent = `✗ Error: ${errMsg}`;
      logEl.appendChild(div);
      logEl.scrollTop = logEl.scrollHeight;
    }
    showToast(`Error perfil luminancia: ${errMsg}`, 'error', 8000);
    // Sustituimos el progreso por un boton de cerrar para que el usuario
    // pueda leer el error con calma. Idempotente: si ya existe, no lo
    // duplicamos.
    if (!document.getElementById('dv-light-error-close-btn')) {
      const footer = document.querySelector('#dv-light-modal .modal-foot, #dv-light-modal .dv-light-foot');
      const host = footer || document.querySelector('#dv-light-modal .modal-box') || document.getElementById('dv-light-modal');
      if (host) {
        const btn = document.createElement('button');
        btn.id = 'dv-light-error-close-btn';
        btn.className = 'btn btn-ghost btn-sm';
        btn.textContent = 'Cerrar';
        btn.style.marginTop = '12px';
        btn.onclick = () => {
          closeModal('dv-light-modal');
          btn.remove();
        };
        host.appendChild(btn);
      }
    }
  } finally {
    pollTicker._stop();
    // Limpia la session global por higiene; futuros analisis crearan una nueva.
    if (window._dvLightSession) {
      window._dvLightSession.ctrl = null;
      window._dvLightSession.polledResult = null;
      window._dvLightSession.cancelledByUser = false;
    }
  }
}

/** Cancela el análisis de perfil de luminancia activo. Llamado desde el
 *  botón "🛑 Cancelar análisis" del modal. Marca la sesión como cancelada
 *  por el usuario (para que el catch del POST cierre el modal sin
 *  mostrarlo como error), POST al backend para matar el subprocess y
 *  aborta el fetch local. */
async function _dvLightCancel() {
  const btn = document.getElementById('dv-light-cancel-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Cancelando…'; }
  // Flag para que el catch path NO muestre error ni boton Cerrar — fue
  // intencional, cierre directo + toast informativo.
  if (window._dvLightSession) window._dvLightSession.cancelledByUser = true;
  try {
    await apiFetch('/api/mkv/light-profile/cancel', { method: 'POST', silent: true });
  } catch (_) { /* el aborto local + state del backend bastan */ }
  try { window._dvLightSession?.ctrl?.abort(); } catch (_) {}
}

// Guard monotónico: ignora actualizaciones que llegan con un step inferior
// al actual (orden de mensajes garantizado por el chained await del polling,
// pero esto ofrece doble red por si el codigo manda updates desordenados
// desde otros sitios — p.ej. el success path llama _dvLightSetStep(4) y
// luego una respuesta vieja en vuelo querría volver a step=3).
let _dvLightLastStep = 0;
let _dvLightLastPct = 0;

function _dvLightSetStep(activeStep) {
  if (activeStep < _dvLightLastStep) return;
  _dvLightLastStep = activeStep;
  for (let i = 1; i <= 3; i++) {
    const el = document.getElementById(`dv-light-step-${i}`);
    if (!el) continue;
    el.classList.remove('active', 'pending', 'done', 'error');
    const marker = el.querySelector('.dv-light-step-marker');
    if (activeStep >= 4 || i < activeStep) {
      el.classList.add('done');
      if (marker) marker.textContent = '✓';
    } else if (i === activeStep) {
      el.classList.add('active');
      if (marker) marker.textContent = '⟳';
    } else {
      el.classList.add('pending');
      if (marker) marker.textContent = '○';
    }
  }
}
function _dvLightSetProgress(pct) {
  // Mismo guard monotonico que _dvLightSetStep
  const clamped = Math.max(0, Math.min(100, pct));
  if (clamped < _dvLightLastPct) return;
  _dvLightLastPct = clamped;
  const bar = document.getElementById('dv-light-progress-bar');
  const txt = document.getElementById('dv-light-pct');
  if (bar) bar.style.width = `${clamped}%`;
  if (txt) txt.textContent = `${Math.round(clamped)}%`;
}
function _dvLightSetElapsed(secs) {
  const el = document.getElementById('dv-light-elapsed');
  if (!el) return;
  const s = Math.max(0, Math.floor(secs));
  el.textContent = `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;
}

// ── Render del panel de edición ──────────────────────────────────

function _renderMkvEditPanel() {
  if (!mkvProject) return;
  const a = mkvProject.analysis;
  const videoTracks = a.tracks.filter(t => t.type === 'video');
  const audioTracks = a.tracks.filter(t => t.type === 'audio');
  const subTracks   = a.tracks.filter(t => t.type === 'subtitles');

  // Pista principal de vídeo (Base Layer — NO el EL si existe)
  const mainVideo = videoTracks.find(v => (v.pixel_dimensions || '').startsWith('3840') || (v.pixel_dimensions || '').startsWith('4096')) || videoTracks[0];
  const elVideo   = videoTracks.find(v => v !== mainVideo && (v.pixel_dimensions || '').startsWith('1920'));

  // Línea de codec + resolución + bitrate
  const videoCodecLine = mainVideo ? [
    mainVideo.codec || 'HEVC',
    mainVideo.pixel_dimensions || '',
    mainVideo.bit_depth ? `${mainVideo.bit_depth}-bit` : '',
    mainVideo.bitrate_kbps ? `${mainVideo.bitrate_kbps.toLocaleString()} kbps` : '',
  ].filter(Boolean).join(' · ') : '';

  // HDR10 / color space
  const hdrBadge = a.hdr?.hdr_format ? escHtml(a.hdr.hdr_format)
    : (mainVideo?.hdr_format ? escHtml(mainVideo.hdr_format) : '');
  const hdrSpace = [
    a.hdr?.color_primaries || mainVideo?.color_primaries,
    a.hdr?.transfer_characteristics,
  ].filter(Boolean).join(' · ');
  const hdrLuminance = a.hdr?.mastering_display_luminance || '';
  const hdrMaxCll  = a.hdr?.max_cll  ? `MaxCLL ${a.hdr.max_cll} nits`  : '';
  const hdrMaxFall = a.hdr?.max_fall ? `MaxFALL ${a.hdr.max_fall} nits` : '';

  // Dolby Vision — bloque enriquecido (reusa lógica de Tab 1)
  const hasElByCount = videoTracks.filter(v => (v.codec || '').toUpperCase().includes('HEVC') || (v.codec || '').toUpperCase().includes('H.265')).length > 1;
  const dv = a.dovi;
  const dvDetected = !!dv || a.has_fel || hasElByCount;
  let dvProfileLine = '';
  let dvLevelsLine  = '';
  let dvCountsLine  = '';
  let cmBadgeHtml   = '';
  let cmHintHtml    = '';
  if (dv) {
    const elType = dv.el_type || (a.has_fel ? 'FEL' : (hasElByCount ? 'MEL' : ''));
    dvProfileLine = `Profile ${dv.profile}${elType ? ` (${elType})` : ''}`;
    const lvls = [];
    if (dv.has_l1) lvls.push('L1');
    if (dv.has_l2) lvls.push('L2');
    if (dv.has_l3) lvls.push('L3');
    if (dv.has_l5) lvls.push('L5');
    if (dv.has_l6) lvls.push('L6');
    if (dv.has_l8) lvls.push(`L8${dv.l8_trim_count ? '×' + dv.l8_trim_count : ''}`);
    if (dv.has_l9)  lvls.push('L9');
    if (dv.has_l10) lvls.push('L10');
    if (dv.has_l11) lvls.push('L11');
    dvLevelsLine = lvls.length ? `Niveles: ${lvls.join(' · ')}` : '';
    // dv.scene_count y dv.frame_count vienen del sample de 30s
    // (extract-rpu --limit 720), no del film completo. Los omitimos para
    // no engañar al usuario; los frames totales reales se muestran en el
    // bloque "Stream" de la radiografía via duration × fps.
    dvCountsLine = '';

    // Badge CM version — v2.9 naranja (upgradeable), v4.0 verde (ya CMv4.0)
    const cm = (dv.cm_version || '').toLowerCase();
    const isV40 = cm.includes('4.0') || cm.includes('v4');
    const isV29 = cm.includes('2.9') || cm.includes('v2');
    if (isV40) {
      cmBadgeHtml = `<span style="display:inline-flex; align-items:center; gap:4px; padding:2px 9px; border-radius:10px; background:rgba(52,199,89,0.18); color:#0e6b2a; font-size:11px; font-weight:700; letter-spacing:0.2px" data-tooltip="Este MKV ya tiene CMv4.0 (incluye L8-L11 — tone-mapping de última generación)">✓ CMv4.0</span>`;
      // Los badges heuristicos de procedencia (nativo/retail/generado/incierto)
      // se reemplazaron por la tabla detallada "Radiografia DV+HDR" que muestra
      // los datos factuales sin interpretacion.
    } else if (isV29) {
      cmBadgeHtml = `<span style="display:inline-flex; align-items:center; gap:4px; padding:2px 9px; border-radius:10px; background:rgba(255,149,0,0.18); color:#8a4a00; font-size:11px; font-weight:700; letter-spacing:0.2px" data-tooltip="Este MKV está en CMv2.9 — se puede upgradear a CMv4.0 desde Tab 3 para ganar L8-L11">⚡ CMv2.9</span>`;
      cmHintHtml = `<span style="color:#8a4a00; font-size:11px; font-weight:500">→ Upgradeable a CMv4.0 (pestaña "Upgrade Dolby Vision CMv4.0")</span>`;
    } else if (dv.cm_version) {
      cmBadgeHtml = `<span style="display:inline-flex; align-items:center; gap:4px; padding:2px 9px; border-radius:10px; background:rgba(142,142,147,0.20); color:var(--text-2); font-size:11px; font-weight:700">CM ${escHtml(dv.cm_version)}</span>`;
    }
  } else if (dvDetected) {
    // Se detecta DV por número de HEVC pero dovi_tool no corrió / falló
    dvProfileLine = a.has_fel ? 'P7 FEL (detectado por estructura)' : (hasElByCount ? 'P7 MEL (detectado por estructura)' : 'Dolby Vision detectado');
  }

  const panel = document.getElementById('mkv-edit-panel');
  panel.innerHTML = `
    <div class="project-panel-inner" style="max-width:900px; margin:0 auto; padding:24px 20px">

      <!-- Ficha TMDb (hidratada en async) -->
      <div id="mkv-edit-tmdb-card" class="tmdb-card-slot"></div>

      <!-- Info del fichero (solo lectura) -->
      <div class="section-card">
        <div class="section-header">
          <div><div class="section-title">📦 Fichero MKV</div></div>
          <button class="btn btn-ghost btn-xs" onclick="reanalyzeMkv()"
                  data-tooltip="Invalida el cache y re-ejecuta el análisis completo (1-3 min). Útil si el fichero cambió externamente o tras una mejora del clasificador."
                  style="margin-left:auto; color:var(--text-2)">↻ Re-analizar</button>
        </div>
        <div class="section-body">
          <div style="font-weight:600; font-size:14px; margin-bottom:4px">${escHtml(a.file_name)}</div>
          <div style="font-size:12px; color:var(--text-2); display:flex; flex-wrap:wrap; gap:4px 14px; line-height:1.55">
            <span>${_fmtBytes(a.file_size_bytes)}</span>
            <span>${_fmtDuration(a.duration_seconds)}</span>
            <span>${audioTracks.length} audio · ${subTracks.length} subs · ${a.chapters?.length || 0} capítulos</span>
          </div>
        </div>
      </div>

      <!-- Vídeo: resumen compacto + bloque detallado HDR/DV inline -->
      ${mainVideo ? `
      <div class="section-card">
        <div class="section-header">
          <div style="flex:1">
            <div class="section-title">🎞️ Vídeo</div>
          </div>
          <div class="video-header-badges">
            ${hdrBadge ? `<span class="video-badge video-badge-hdr">${hdrBadge}</span>` : ''}
            ${dvDetected && dvProfileLine ? `<span class="video-badge video-badge-dv">✨ DV ${escHtml(dvProfileLine.replace('Profile ', 'P'))}</span>` : ''}
            ${cmBadgeHtml}
            ${cmHintHtml ? `<span class="video-hint">${cmHintHtml}</span>` : ''}
          </div>
        </div>
        <div class="section-body">
          <div class="video-summary-line">
            <strong>${escHtml(videoCodecLine)}</strong>
            ${elVideo ? `<span class="video-el">+EL ${escHtml(elVideo.codec || 'HEVC')} ${escHtml(elVideo.pixel_dimensions || '')}${elVideo.bitrate_kbps ? ' · ' + elVideo.bitrate_kbps.toLocaleString() + ' kbps' : ''}</span>` : ''}
          </div>
          ${dvDetected && dv ? _renderMkvDvRadiography(a, dv, mainVideo, elVideo) : (dvDetected && !dv ? `<div style="font-size:11px; color:var(--text-3); font-style:italic; margin-top:6px">RPU no analizado en detalle (dovi_tool no disponible o falló)</div>` : '')}
        </div>
      </div>` : ''}

      <!-- Pistas de Audio -->
      <div class="section-card">
        <div class="section-header">
          <div><div class="section-title">🔊 Pistas de audio <span style="font-weight:400; color:var(--text-3); font-size:11px">(${audioTracks.length})</span></div>
          <div class="section-subtitle">Edita nombres y flag default</div></div>
        </div>
        <div class="section-body">
          <ul class="track-list" id="mkv-audio-list"></ul>
        </div>
      </div>

      <!-- Pistas de Subtítulos -->
      <div class="section-card">
        <div class="section-header">
          <div><div class="section-title">💬 Pistas de subtítulos <span style="font-weight:400; color:var(--text-3); font-size:11px">(${subTracks.length})</span></div>
          <div class="section-subtitle">Edita nombres, flags default y forzado</div></div>
        </div>
        <div class="section-body">
          <ul class="track-list" id="mkv-sub-list"></ul>
        </div>
      </div>

      <!-- Capítulos -->
      <div class="section-card">
        <div class="section-header">
          <div><div class="section-title">📖 Capítulos</div>
          <div class="section-subtitle">Clic en la barra para añadir · arrastra marcas para ajustar</div></div>
          <button class="btn btn-xs" id="mkv-chapters-generic-btn" style="display:none; margin-left:auto"
            onclick="setMkvGenericChapterNames()"
            data-tooltip="Reemplaza todos los nombres por Capítulo 01, Capítulo 02… (mantiene timestamps)">🏷️ Nombres genéricos</button>
        </div>
        <div class="section-body">
          <div id="mkv-chapters-banner" class="banner info" style="display:none">
            <span class="banner-icon" id="mkv-chapters-icon">💿</span>
            <span id="mkv-chapters-text"></span>
            <button class="btn btn-xs" id="mkv-chapters-autogen-btn" style="display:none; margin-left:auto"
              onclick="generateMkvAutoChapters()"
              data-tooltip="Genera Capítulo 01, 02, 03… cada 10 minutos desde el minuto 10 (igual que en Crear MKV cuando el disco no trae capítulos)">📑 Generar cada 10 min</button>
          </div>
          <div id="mkv-chapter-timeline-wrap" class="chapter-timeline-wrap"
            onclick="onMkvTimelineClick(event)"
            onmousemove="onMkvTimelineHover(event)"
            onmouseleave="onMkvTimelineLeave()">
            <div class="chapter-timeline-track"></div>
            <div class="timeline-marks" id="mkv-timeline-marks"></div>
            <div class="timeline-cursor" id="mkv-timeline-cursor" style="display:none"></div>
          </div>
          <div id="mkv-chapters-list" class="chapter-list"></div>
        </div>
      </div>

      <!-- Barra de botones -->
      <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:20px; padding-bottom:12px">
        <button class="btn btn-ghost btn-md" onclick="showRawMkvData()"
          data-tooltip="Ver datos crudos del análisis (mkvmerge -J + MediaInfo + log)"
          style="color:var(--text-2); margin-right:auto">🔬 Datos MKV</button>
        <button class="btn btn-ghost btn-md" onclick="undoMkvEdits()"
          data-tooltip="Revertir todos los cambios al estado original"
          style="color:var(--text-2)">↩️ Deshacer cambios</button>
        <button class="btn btn-ghost btn-md" onclick="closeMkvEditor()"
          data-tooltip="Cerrar el editor"
          style="color:var(--red)">✕ Cerrar</button>
        <button class="btn btn-primary btn-md" onclick="applyMkvEdits()"
          data-tooltip="Aplica todos los cambios al MKV">✅ Aplicar cambios</button>
      </div>
    </div>`;

  // Hidratar ficha TMDb (async, no bloquea el render de pistas)
  hydrateTmdbCard('mkv-edit-tmdb-card', mkvProject.fileName || a.file_name);

  _renderMkvTracks();
  _renderMkvChapters();
  _attachSparklineHover();
}

/**
 * Attach de mousemove al sparkline de luminancia: muestra crosshair vertical
 * + dot en la curva + tooltip con valor (nits) y timestamp en hh:mm:ss.
 * Idempotente — recorre todos los .dv-sparkline-host del documento (en
 * principio solo hay uno en Tab 2 a la vez). Los datos se leen via
 * data-series del SVG → no necesita acceso a mkvProject.
 */
function _attachSparklineHover() {
  document.querySelectorAll('.dv-sparkline-host').forEach(host => {
    const svg = host.querySelector('.dv-sparkline-svg');
    if (!svg || svg._hoverWired) return;
    svg._hoverWired = true;

    let series, avgSer = null, minSer = null;
    try { series = JSON.parse(svg.dataset.series); }
    catch (_) { return; }
    if (!Array.isArray(series) || series.length < 2) return;
    try { if (svg.dataset.avgSeries) avgSer = JSON.parse(svg.dataset.avgSeries); }
    catch (_) { avgSer = null; }
    try { if (svg.dataset.minSeries) minSer = JSON.parse(svg.dataset.minSeries); }
    catch (_) { minSer = null; }

    const dur   = parseFloat(svg.dataset.duration) || 0;
    const padL  = parseFloat(svg.dataset.padL);
    const padR  = parseFloat(svg.dataset.padR);
    const padT  = parseFloat(svg.dataset.padT);
    const padB  = parseFloat(svg.dataset.padB);
    const svgW  = parseFloat(svg.dataset.svgW);
    const svgH  = parseFloat(svg.dataset.svgH);
    const usableW = svgW - padL - padR;
    const usableH = svgH - padT - padB;
    // yMax = escala efectiva del chart (peak con headroom). Lo lee el SVG
    // del data-attribute para que coincida con el render.
    const yMax = parseFloat(svg.dataset.yMax) || Math.max(...series) || 1;

    const cursor  = svg.querySelector('.dv-sparkline-cursor');
    const dot     = svg.querySelector('.dv-sparkline-dot');
    const tooltip = host.querySelector('.dv-sparkline-tooltip');
    if (!cursor || !dot || !tooltip) return;

    svg.addEventListener('mousemove', (e) => {
      const rect = svg.getBoundingClientRect();
      if (rect.width <= 0) return;
      const px = e.clientX - rect.left;        // pixel X relativo al SVG
      const sx = (px / rect.width) * svgW;     // viewBox X
      // Solo mostrar tooltip cuando el mouse esta dentro del area de chart
      if (sx < padL || sx > svgW - padR) {
        cursor.style.display = 'none';
        dot.style.display = 'none';
        tooltip.style.display = 'none';
        return;
      }
      const i = Math.max(0, Math.min(series.length - 1,
        Math.round(((sx - padL) / usableW) * (series.length - 1))));
      const v = series[i];
      const av = avgSer ? avgSer[i] : null;
      const mn = minSer ? minSer[i] : null;
      const t = dur * (i / (series.length - 1));
      const x = padL + (i / (series.length - 1)) * usableW;
      const y = padT + usableH - Math.max(0, Math.min(1, v / yMax)) * usableH;

      cursor.setAttribute('x1', x);
      cursor.setAttribute('x2', x);
      cursor.style.display = '';
      dot.setAttribute('cx', x);
      dot.setAttribute('cy', y);
      dot.style.display = '';

      // Tooltip: peak / avg / min en filas con codigo de color matching las curvas.
      const lines = [];
      lines.push(`<span style="color:#7cc4ff">peak</span> ${v.toLocaleString()} nits`);
      if (av != null) lines.push(`<span style="color:#86efac">avg</span> ${av.toLocaleString()} nits`);
      if (mn != null) lines.push(`<span style="color:#cbd5e1">min</span> ${mn.toLocaleString()} nits`);
      if (dur > 0) lines.push(`<span style="color:#94a3b8">@</span> ${_rgrfFmtTime(t)}`);
      tooltip.innerHTML = lines.join('<br>');
      tooltip.style.display = '';
      // Posiciona el tooltip cerca del cursor; si está en la mitad derecha
      // del chart, mostrar a la izquierda para no salirse.
      const tipPxX = (x / svgW) * rect.width;
      const tipPxY = (y / svgH) * rect.height;
      const onRight = px > rect.width / 2;
      tooltip.style.left  = onRight ? '' : `${tipPxX + 14}px`;
      tooltip.style.right = onRight ? `${rect.width - tipPxX + 14}px` : '';
      tooltip.style.top   = `${Math.max(0, tipPxY - 56)}px`;
    });

    svg.addEventListener('mouseleave', () => {
      cursor.style.display = 'none';
      dot.style.display = 'none';
      tooltip.style.display = 'none';
    });
  });
}

// ── Render helpers ───────────────────────────────────────────────

function _renderMkvTracks() {
  if (!mkvProject) return;
  const a = mkvProject.analysis;
  const audioList = document.getElementById('mkv-audio-list');
  const subList   = document.getElementById('mkv-sub-list');

  // Audio
  const audioTracks = a.tracks.filter(t => t.type === 'audio');
  audioList.innerHTML = '';
  audioTracks.forEach(t => {
    const langName = langLiteral(ISO639_MAP[t.language] || t.language || 'und');
    // Conteo de canales: usa layout explícito de MediaInfo si disponible (más preciso que el contador bruto)
    const chCount = t.channels || 0;
    const channelsPretty = chCount ? (chCount >= 8 ? '7.1' : chCount >= 6 ? '5.1' : chCount >= 2 ? '2.0' : '1.0') : '';
    // Codec comercial (Atmos, DTS:X, TrueHD…) prevalece sobre el técnico
    const codecPretty = t.format_commercial || t.codec || '';
    const compressionPill = t.compression_mode
      ? `<span style="font-size:10px; padding:1px 6px; border-radius:8px; background:${t.compression_mode.toLowerCase().includes('lossless') ? 'rgba(52,199,89,0.15)' : 'rgba(142,142,147,0.18)'}; color:${t.compression_mode.toLowerCase().includes('lossless') ? '#0e6b2a' : 'var(--text-2)'}; font-weight:600; margin-left:4px">${escHtml(t.compression_mode)}</span>`
      : '';
    // Info visible (no solo tooltip) — todo lo que aporta
    const desc = [
      codecPretty,
      channelsPretty,
      t.channel_layout ? escHtml(t.channel_layout) : '',
      t.sample_rate ? `${t.sample_rate/1000} kHz` : '',
      t.bitrate_kbps ? `${t.bitrate_kbps.toLocaleString()} kbps` : '',
    ].filter(Boolean).join(' · ');
    const def = t.flag_default ? ' active-default' : '';
    const tooltip = [
      `Codec técnico: ${t.codec}`,
      t.format_commercial ? `Codec comercial: ${t.format_commercial}` : null,
      `Idioma: ${t.language || '—'} → ${langName}`,
      chCount ? `Canales: ${chCount} (${channelsPretty})` : null,
      t.channel_layout ? `Layout: ${t.channel_layout}` : null,
      t.sample_rate ? `Sample rate: ${t.sample_rate/1000} kHz` : null,
      t.bitrate_kbps ? `Bitrate: ${t.bitrate_kbps.toLocaleString()} kbps` : null,
      t.compression_mode ? `Compresión: ${t.compression_mode}` : null,
      `Track ID: ${t.id}`,
    ].filter(Boolean).join('\n');
    const li = document.createElement('li');
    li.className = 'track-item';
    li.dataset.trackId = t.id;
    li.innerHTML = `
      <span class="track-type-icon" data-tooltip="${escHtml(tooltip)}">🔊</span>
      <div class="track-main">
        <span class="track-edit-icon">✏️</span>
        <input class="track-label-input" type="text"
          value="${escHtml(t.name || '')}"
          placeholder="${escHtml(langName + ' ' + codecPretty)}"
          onchange="onMkvTrackEdit(${t.id}, 'name', this.value)"
          data-tooltip="Nombre de la pista en el MKV">
        <span class="track-raw">${escHtml(langName)} · ${desc}${compressionPill}</span>
      </div>
      <div class="track-flags">
        <button class="flag-pill${def}" onclick="onMkvTrackFlag(${t.id}, 'default', 'audio')"
          data-tooltip="flag default: pista seleccionada por defecto">DEF</button>
      </div>`;
    audioList.appendChild(li);
  });

  // Subtítulos
  const subTracksArr = a.tracks.filter(t => t.type === 'subtitles');
  subList.innerHTML = '';
  subTracksArr.forEach(t => {
    const langName = langLiteral(ISO639_MAP[t.language] || t.language || 'und');
    // Codec real desde mkvmerge (ej: "HDMV PGS", "SubRip/SRT", "VobSub", "TrueType SSA/ASS")
    const codecRaw = (t.codec || '').trim();
    const codecPretty = codecRaw
      ? (codecRaw.toUpperCase().includes('PGS') ? 'PGS'
        : codecRaw.toUpperCase().includes('SRT') || codecRaw.toUpperCase().includes('SUBRIP') ? 'SRT'
        : codecRaw.toUpperCase().includes('VOBSUB') ? 'VobSub'
        : codecRaw.toUpperCase().includes('ASS') || codecRaw.toUpperCase().includes('SSA') ? 'ASS'
        : codecRaw)
      : 'PGS';

    // Clasificación Forzados / Completos con señal de fallback. En Tab 2
    // estamos inspeccionando UN MKV ya construido y clasificamos cada
    // pista de forma independiente — sin acceso barato al ratio
    // completo/forzado por idioma que sí usa la heurística de Fase B
    // sobre el origen. Por eso aquí el fallback se queda en el umbral
    // absoluto (<500 paq.). Cuando el flag forced del MKV está bien
    // puesto (caso típico de los MKVs generados por la propia app), se
    // ignora el fallback y se usa la verdad del contenedor.
    //   1. flag forced del MKV → fuente de verdad.
    //   2. <500 paquetes → forzado (muy ligero, casi siempre forzado).
    //   3. bitrate <3 kbps (sin packet_count) → forzado, señal histórica
    //      antes de tener PGS packet counting.
    //   4. resto → completos.
    const packets = t.packet_count || 0;
    let derivedForced = t.flag_forced;
    let forcedSource = t.flag_forced ? 'flag del MKV' : '';
    if (!t.flag_forced) {
      if (packets > 0 && packets < 500) {
        derivedForced = true;
        forcedSource = `${packets} paquetes (forzado típico <500)`;
      } else if (packets === 0 && t.bitrate_kbps > 0 && t.bitrate_kbps < 3) {
        derivedForced = true;
        forcedSource = `bitrate ${t.bitrate_kbps} kbps (forzado típico <3)`;
      }
    }
    const flagForcedLit = t.flag_forced;
    const def = t.flag_default ? ' active-default' : '';
    const frc = flagForcedLit ? ' active-forced' : '';
    const forcedLabel = derivedForced ? 'Forzados' : 'Completos';
    // Anotación cuando la clasificación viene inferida del volumen, no del flag
    const inferredMark = (derivedForced && !flagForcedLit) ? ' <span style="color:var(--orange); font-size:10px; font-weight:600" data-tooltip="Clasificación inferida por volumen (el flag forced del MKV no está puesto)">↯ inferido</span>' : '';

    // Info visible: codec + resolución + paq. + bitrate + tipo
    const pktTag = packets > 0 ? `${packets.toLocaleString()} paq.` : '';
    const desc = [
      codecPretty,
      t.pixel_dimensions ? escHtml(t.pixel_dimensions) : '',
      pktTag,
      t.bitrate_kbps ? `${t.bitrate_kbps.toLocaleString()} kbps` : '',
      forcedLabel,
    ].filter(Boolean).join(' · ');
    const tooltip = [
      `Codec: ${codecRaw || 'PGS'}`,
      `Idioma: ${t.language || '—'} → ${langName}`,
      `Tipo: ${forcedLabel}${forcedSource ? ` (${forcedSource})` : ''}`,
      t.pixel_dimensions ? `Resolución bitmap: ${t.pixel_dimensions}` : null,
      packets > 0 ? `Paquetes PES: ${packets.toLocaleString()} (ffprobe)` : null,
      t.bitrate_kbps ? `Bitrate: ${t.bitrate_kbps.toLocaleString()} kbps` : null,
      `Track ID: ${t.id}`,
    ].filter(Boolean).join('\n');
    const li = document.createElement('li');
    li.className = 'track-item';
    li.dataset.trackId = t.id;
    li.innerHTML = `
      <span class="track-type-icon" data-tooltip="${escHtml(tooltip)}">💬</span>
      <div class="track-main">
        <span class="track-edit-icon">✏️</span>
        <input class="track-label-input" type="text"
          value="${escHtml(t.name || '')}"
          placeholder="${escHtml(langName + ' ' + forcedLabel + ' (' + codecPretty + ')')}"
          onchange="onMkvTrackEdit(${t.id}, 'name', this.value)"
          data-tooltip="Nombre de la pista en el MKV">
        <span class="track-raw">${escHtml(langName)} · ${desc}${inferredMark}</span>
      </div>
      <div class="track-flags">
        <button class="flag-pill${def}" onclick="onMkvTrackFlag(${t.id}, 'default', 'subtitles')"
          data-tooltip="flag default: subtítulo seleccionado por defecto">DEF</button>
        <button class="flag-pill${frc}" onclick="onMkvTrackFlag(${t.id}, 'forced', 'subtitles')"
          data-tooltip="flag forced: subtítulos forzados para diálogos en idioma extranjero">FRC</button>
      </div>`;
    subList.appendChild(li);
  });
}

/** Mapa ISO 639-2 → nombre en inglés (para langLiteral) */
const ISO639_MAP = {
  spa:'Spanish', eng:'English', fre:'French', fra:'French', ger:'German', deu:'German',
  ita:'Italian', jpn:'Japanese', por:'Portuguese', chi:'Chinese', zho:'Chinese',
  kor:'Korean', dut:'Dutch', nld:'Dutch', rus:'Russian', pol:'Polish', cze:'Czech',
  ces:'Czech', hun:'Hungarian', swe:'Swedish', nor:'Norwegian', dan:'Danish',
  fin:'Finnish', tur:'Turkish', tha:'Thai', ara:'Arabic', heb:'Hebrew', hin:'Hindi',
  und:'Undetermined',
};

function _renderMkvChapters() {
  if (!mkvProject) return;
  const a = mkvProject.analysis;
  const banner = document.getElementById('mkv-chapters-banner');
  const text   = document.getElementById('mkv-chapters-text');
  const autogenBtn = document.getElementById('mkv-chapters-autogen-btn');

  if (a.chapters.length > 0) {
    if (banner) { banner.style.display = 'flex'; banner.className = 'banner info'; }
    if (text) text.textContent = `${a.chapters.length} capítulos`;
    if (autogenBtn) autogenBtn.style.display = 'none';
  } else {
    if (banner) { banner.style.display = 'flex'; banner.className = 'banner warning'; }
    if (text) text.textContent = 'Sin capítulos en este MKV';
    // Botón "Generar cada 10 min" visible sólo si la duración permite al menos
    // un capítulo (necesita > 10 min de duración total).
    if (autogenBtn) {
      const dur = a.duration_seconds || 0;
      autogenBtn.style.display = (dur > 600) ? '' : 'none';
    }
  }

  // Botón nombres genéricos: visible solo si algún capítulo tiene nombre custom
  const genericBtn = document.getElementById('mkv-chapters-generic-btn');
  if (genericBtn) {
    const hasCustomNames = a.chapters.some(ch => ch.name_custom);
    genericBtn.style.display = hasCustomNames ? '' : 'none';
  }

  _renderMkvChapterMarks();
  _renderMkvChapterList();
}

/**
 * Genera capítulos automáticos cada 10 min desde el minuto 10. Mismo
 * algoritmo que `generate_auto_chapters` del backend (phases/phase_b.py)
 * que se usa en Tab 1 cuando el disco no trae capítulos. Marca el proyecto
 * dirty para que aparezca el botón "Aplicar cambios" (el backend escribe
 * los capítulos via mkvpropedit con --chapters).
 */
function generateMkvAutoChapters() {
  if (!mkvProject) return;
  const a = mkvProject.analysis;
  const dur = a.duration_seconds || 0;
  if (dur <= 600) {
    showToast('La duración del MKV es menor de 10 min — no hay donde poner capítulos', 'warning');
    return;
  }
  const interval = 600;
  const chapters = [];
  let t = interval;     // empieza en 00:10:00 (no en 00:00:00)
  let num = 1;
  while (t < dur) {
    chapters.push({
      number: num,
      timestamp: secsToTs(t),
      name: `Capítulo ${String(num).padStart(2, '0')}`,
      name_custom: false,
    });
    t += interval;
    num += 1;
  }
  a.chapters = chapters;
  mkvProject.dirty = true;
  _renderMkvChapters();
  showToast(`✓ ${chapters.length} capítulos generados — pulsa "Aplicar cambios" para escribirlos al MKV`, 'success');
}

function _renderMkvChapterMarks() {
  if (!mkvProject) return;
  const a = mkvProject.analysis;
  const container = document.getElementById('mkv-timeline-marks');
  if (!container) return;
  container.innerHTML = '';

  const duration = a.duration_seconds;
  if (!duration) return;

  renderTimelineTicks(container, duration);

  a.chapters.forEach((ch, idx) => {
    const secs = tsToSecs(ch.timestamp);
    const pct = (secs / duration) * 100;
    const mark = document.createElement('div');
    mark.className = 'chapter-mark';
    mark.style.left = `${pct}%`;
    mark.dataset.tooltip = `${ch.name}\n${ch.timestamp}`;
    mark.onmousedown = (e) => startMkvChapterDrag(e, mark, idx);
    container.appendChild(mark);
  });
}

function _renderMkvChapterList() {
  if (!mkvProject) return;
  const a = mkvProject.analysis;
  const container = document.getElementById('mkv-chapters-list');
  if (!container) return;
  container.innerHTML = '';

  a.chapters.forEach((ch, idx) => {
    const row = document.createElement('div');
    row.className = 'chapter-row';
    row.innerHTML = `
      <span class="chapter-num">${ch.number}</span>
      <input type="text" class="chapter-ts" value="${escHtml(ch.timestamp)}"
        onchange="onMkvChapterTsChange(${idx}, this.value)">
      <input type="text" class="chapter-name" value="${escHtml(ch.name)}"
        onchange="onMkvChapterNameChange(${idx}, this.value)">
      <button class="btn btn-icon" onclick="deleteMkvChapter(${idx})"
        data-tooltip="Eliminar capítulo">✕</button>`;
    container.appendChild(row);
  });
}

// ── Track editing ────────────────────────────────────────────────

function onMkvTrackEdit(trackId, field, value) {
  if (!mkvProject) return;
  const track = mkvProject.analysis.tracks.find(t => t.id === trackId);
  if (track) track[field] = value;
  mkvProject.dirty = true;
}

function onMkvTrackFlag(trackId, flag, trackType) {
  if (!mkvProject) return;

  const tracks = mkvProject.analysis.tracks.filter(t => t.type === trackType);

  if (flag === 'default') {
    tracks.forEach(t => { t.flag_default = t.id === trackId ? !t.flag_default : false; });
  } else {
    const track = tracks.find(t => t.id === trackId);
    if (track) track.flag_forced = !track.flag_forced;
  }

  mkvProject.dirty = true;
  _renderMkvTracks();
}

// ── Chapter editing ──────────────────────────────────────────────

function onMkvTimelineClick(e) {
  if (!mkvProject) return;
  const duration = mkvProject.analysis.duration_seconds;
  if (!duration) return;

  const wrap = document.getElementById('mkv-chapter-timeline-wrap');
  const rect = wrap.getBoundingClientRect();
  const pct  = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const secs = pct * duration;

  mkvProject.analysis.chapters.push({
    number: 0, timestamp: secsToTs(secs), name: '', name_custom: false,
  });
  _renumberMkvChapters();
  _renderMkvChapters();
  mkvProject.dirty = true;
}

function onMkvTimelineHover(e) {
  if (!mkvProject) return;
  const duration = mkvProject.analysis.duration_seconds;
  if (!duration) return;
  const wrap  = document.getElementById('mkv-chapter-timeline-wrap');
  const rect  = wrap.getBoundingClientRect();
  const pct   = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const label = document.getElementById('mkv-timeline-cursor');
  if (label) {
    label.style.display = '';
    label.style.left = `${e.clientX - rect.left}px`;
    label.textContent = secsToTs(pct * duration);
  }
}

function onMkvTimelineLeave() {
  const el = document.getElementById('mkv-timeline-cursor');
  if (el) el.style.display = 'none';
}

function deleteMkvChapter(idx) {
  if (!mkvProject) return;
  mkvProject.analysis.chapters.splice(idx, 1);
  _renumberMkvChapters();
  _renderMkvChapters();
  mkvProject.dirty = true;
}

function onMkvChapterTsChange(idx, value) {
  if (!mkvProject) return;
  mkvProject.analysis.chapters[idx].timestamp = value;
  _renumberMkvChapters();
  _renderMkvChapters();
  mkvProject.dirty = true;
}

function onMkvChapterNameChange(idx, value) {
  if (!mkvProject) return;
  mkvProject.analysis.chapters[idx].name = value;
  mkvProject.analysis.chapters[idx].name_custom = value.trim() !== '';
  mkvProject.dirty = true;
  // Actualizar visibilidad del botón "Nombres genéricos"
  const genericBtn = document.getElementById('mkv-chapters-generic-btn');
  if (genericBtn) {
    const hasCustom = mkvProject.analysis.chapters.some(ch => ch.name_custom);
    genericBtn.style.display = hasCustom ? '' : 'none';
  }
}

function startMkvChapterDrag(_e, markEl, idx) {
  if (!mkvProject) return;
  const duration = mkvProject.analysis.duration_seconds;
  if (!duration) return;
  const wrap = document.getElementById('mkv-chapter-timeline-wrap');
  let dragged = false;

  markEl.classList.add('selected');
  document.body.style.cursor = 'grabbing';

  const marksEl = document.getElementById('mkv-timeline-marks');
  const dragTip = document.createElement('div');
  dragTip.className = 'chapter-drag-tip';
  dragTip.style.display = 'none';
  marksEl?.appendChild(dragTip);

  const onMove = (ev) => {
    dragged = true;
    const rect = wrap.getBoundingClientRect();
    const pct  = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
    const ts   = secsToTs(pct * duration);
    markEl.style.left = `${pct * 100}%`;
    dragTip.style.left = `${pct * 100}%`;
    dragTip.style.display = '';
    dragTip.textContent = ts;
    mkvProject.analysis.chapters[idx].timestamp = ts;
  };

  const onUp = () => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    document.body.style.cursor = '';
    dragTip.remove();
    if (dragged) {
      _renumberMkvChapters();
      _renderMkvChapters();
      mkvProject.dirty = true;
    } else {
      markEl.classList.remove('selected');
    }
  };

  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

function _renumberMkvChapters() {
  if (!mkvProject) return;
  const chs = mkvProject.analysis.chapters;
  chs.sort((a, b) => tsToSecs(a.timestamp) - tsToSecs(b.timestamp));
  chs.forEach((ch, i) => {
    ch.number = i + 1;
    if (!ch.name_custom) ch.name = `Capítulo ${String(ch.number).padStart(2, '0')}`;
  });
}

function setMkvGenericChapterNames() {
  if (!mkvProject?.analysis?.chapters) return;
  mkvProject.analysis.chapters.forEach((ch, i) => {
    ch.name = `Capítulo ${String(i + 1).padStart(2, '0')}`;
    ch.name_custom = false;
  });
  mkvProject.dirty = true;
  _renderMkvChapters();
  showToast('Nombres de capítulo reemplazados por genéricos.', 'info');
}

// ── Aplicar cambios ──────────────────────────────────────────────

/**
 * Detecta si el path está bajo /mnt/library (read-only) y por tanto requiere
 * que el backend lo copie a /mnt/output antes de editar. Lógica replicada
 * del helper backend `_mkv_needs_copy_to_output`.
 */
function _mkvFileIsInLibrary(filePath) {
  if (!filePath) return false;
  return filePath.startsWith('/mnt/library/');
}

async function applyMkvEdits() {
  if (!mkvProject) return;
  const a = mkvProject.analysis;
  const filePath = mkvProject.filePath;

  // Si el MKV está en Biblioteca read-only → confirmación previa.
  // Tras "Aceptar", el backend copia a /mnt/output y luego edita.
  if (_mkvFileIsInLibrary(filePath)) {
    const sizeGb = (a.file_size_bytes || 0) / 1e9;
    showConfirm(
      'MKV en Biblioteca (read-only)',
      `Este MKV está en la biblioteca read-only y no se puede modificar in-place. ` +
      `La app copiará el fichero (${sizeGb.toFixed(1)} GB) a /mnt/output y aplicará ` +
      `los cambios sobre la copia. La biblioteca queda intacta. ` +
      `Esto puede tardar varios minutos para MKVs grandes.`,
      () => _doApplyMkvEdits(true),
      'Copiar y aplicar',
    );
    return;
  }
  _doApplyMkvEdits(false);
}

// Tiempo máximo para el POST de apply cuando hay copia (4 h). El polling
// es la fuente de verdad del progreso; el fetch solo se quedaría abierto
// más tiempo si la copia tarda muchísimo. 30s default era inviable porque
// abortaba el modal aunque la copia siguiera en background.
const MKV_APPLY_LONG_TIMEOUT_MS = 4 * 60 * 60 * 1000;

// Estado del job actual de apply. Permite que el botón "Cancelar copia"
// llame al endpoint del backend y que el flujo principal sepa que el
// usuario inició la cancelación (para mostrar mensaje correcto en lugar
// de "error genérico" cuando el POST devuelve 499).
let _mkvApplyUserCancelled = false;

async function _doApplyMkvEdits(copyToOutput) {
  if (!mkvProject) return;
  const a = mkvProject.analysis;

  const audioEdits = a.tracks.filter(t => t.type === 'audio').map(t => ({
    id: t.id, name: t.name || '', flag_default: t.flag_default, flag_forced: t.flag_forced,
  }));
  const subEdits = a.tracks.filter(t => t.type === 'subtitles').map(t => ({
    id: t.id, name: t.name || '', flag_default: t.flag_default, flag_forced: t.flag_forced,
  }));

  const body = {
    file_path: mkvProject.filePath,
    title: null,
    audio_tracks: audioEdits,
    subtitle_tracks: subEdits,
    chapters: a.chapters,
    copy_to_output: copyToOutput,
  };

  // Mostrar modal de progreso
  const titleEl = document.getElementById('mkv-apply-modal-title');
  const subEl = document.getElementById('mkv-apply-modal-sub');
  const logEl = document.getElementById('mkv-apply-modal-log');
  const statusEl = document.getElementById('mkv-apply-modal-status');
  const closeBtn = document.getElementById('mkv-apply-modal-close-btn');
  const cancelBtn = document.getElementById('mkv-apply-modal-cancel-btn');

  titleEl.textContent = copyToOutput ? 'Copiando MKV a Output…' : 'Aplicando cambios…';
  subEl.textContent = `${audioEdits.length} pistas de audio · ${subEdits.length} pistas de subtítulos · ${a.chapters.length} capítulos`;
  logEl.style.display = 'none';
  logEl.textContent = '';
  statusEl.innerHTML = copyToOutput
    ? '<div style="text-align:left"><div class="progress-bar-wrap"><div id="mkv-apply-progress-fill" class="progress-bar-fill" style="width:0%"></div></div><div id="mkv-apply-progress-text" style="font-size:12px; color:var(--text-3)">Iniciando copia…</div></div>'
    : '<span class="spinner-inline"></span> Ejecutando mkvpropedit…';
  closeBtn.style.display = 'none';
  if (cancelBtn) cancelBtn.style.display = copyToOutput ? '' : 'none';
  _mkvApplyUserCancelled = false;
  openModal('mkv-apply-modal');

  // Polling de progreso (solo cuando hay copia que monitorizar). El POST
  // sigue corriendo en paralelo — el polling solo lee estado, no espera al
  // POST. Cuando el POST resuelve, paramos el polling.
  let pollHandle = null;
  let polling = copyToOutput;
  if (copyToOutput) {
    const tick = async () => {
      if (!polling) return;
      try {
        const st = await apiFetch('/api/mkv/apply/progress', { silent: true });
        if (st && polling) {
          _renderMkvApplyProgress(st);
          // Cuando entra en step="applying", la copia ha terminado y
          // mkvpropedit está corriendo (instantáneo). Ocultamos el botón
          // de cancelar — ya no aplica, y la copia ya no es lo que va a
          // tardar.
          if (st.step === 'applying' && cancelBtn) cancelBtn.style.display = 'none';
        }
      } catch (_) { /* ignora errores de poll */ }
      if (polling) pollHandle = setTimeout(tick, 1000);
    };
    pollHandle = setTimeout(tick, 500);
  }

  let result;
  try {
    // Para copia: silent + timeout largo. El polling cuenta el progreso
    // visualmente; el modal sabe interpretar 499 (cancelado) y otros
    // errores via el último estado del polling, sin necesidad del toast
    // genérico de apiFetch.
    const opts = { method: 'POST', body: JSON.stringify(body) };
    if (copyToOutput) opts.silent = true;
    result = await apiFetch('/api/mkv/apply', opts, copyToOutput ? MKV_APPLY_LONG_TIMEOUT_MS : API_FETCH_TIMEOUT);
  } finally {
    polling = false;
    if (pollHandle) clearTimeout(pollHandle);
    if (cancelBtn) cancelBtn.style.display = 'none';
  }

  // Cancelación por el usuario: prima sobre cualquier otro estado.
  if (_mkvApplyUserCancelled) {
    titleEl.textContent = 'Cancelado';
    statusEl.innerHTML = '<span style="color:var(--orange)">⚠ Copia cancelada — la biblioteca queda intacta y el destino parcial se borró</span>';
    closeBtn.style.display = '';
    return;
  }

  if (!result?.ok) {
    titleEl.textContent = 'Error';
    statusEl.innerHTML = '<span style="color:var(--red)">Error al aplicar cambios</span>';
    closeBtn.style.display = '';
    return;
  }

  // Mostrar output de mkvpropedit
  if (result.output) {
    logEl.textContent = result.output;
    logEl.style.display = '';
  }

  // Si se copió a /mnt/output, actualizar el estado del proyecto al nuevo
  // path para que ediciones posteriores trabajen sobre la copia editable.
  let newFilePath = mkvProject.filePath;
  if (result.copied_from_library && result.new_file_path) {
    newFilePath = result.new_file_path;
    mkvProject.filePath = newFilePath;
    showToast(`✓ MKV copiado a Output con tus cambios: ${newFilePath.split('/').pop()}`, 'success');
  }

  statusEl.innerHTML = '<span style="color:var(--green)">✓ Cambios aplicados correctamente</span>';

  // Re-analizar para refrescar estado — usamos el path ABSOLUTO del MKV
  // (potencialmente actualizado tras copia). El backend valida que cae
  // bajo un root permitido (Library / Output).
  const fresh = await apiFetch('/api/mkv/analyze', {
    method: 'POST',
    body: JSON.stringify({ file_path: newFilePath || mkvProject.fileName }),
  });

  if (fresh) {
    mkvProject.analysis = fresh;
    mkvProject.originalAnalysis = structuredClone(fresh);
    mkvProject.dirty = false;
    _renderMkvEditPanel();
  }

  titleEl.textContent = 'Cambios aplicados';
  closeBtn.style.display = '';
}

/**
 * Botón "🛑 Cancelar copia": llama al backend para abortar la copia
 * cooperativamente. El thread de copia detecta el flag al inicio del
 * siguiente chunk (<1s) y borra el destino parcial. El POST de apply
 * eventualmente devuelve 499 — el flujo principal lo trata como
 * cancelación del usuario y muestra el mensaje correcto.
 */
/**
 * Auto-detect: al entrar a Tab 2 (o al recargar la pestaña con Tab 2 ya
 * activo), comprobar si hay una operación de apply activa en el backend
 * (caso típico: el usuario lanzó una copia de MKV de Library, cerró la
 * pestaña sin esperar, y ahora reabre). Si hay job activo, abrimos el
 * modal con el progreso al que va el backend, polling normal y botón
 * cancelar funcional. Si está terminado/error/cancelled, también lo
 * mostramos brevemente para que el usuario sepa el resultado.
 */
let _mkvApplyResumePolling = false;  // evita loops de polling concurrentes (audit #6)
async function _mkvCheckActiveApply() {
  if (_mkvApplyResumePolling) return;  // ya hay un loop de reanudación activo
  const st = await apiFetch('/api/mkv/apply/progress', { silent: true });
  if (!st || !st.active) {
    // Si hay un step terminal pero active=false, no abrimos modal
    // (probablemente el usuario ya vio el resultado en una sesión previa).
    return;
  }
  // Hay un job activo. Reabrir el modal en estado coherente con el
  // backend, sin volver a lanzar el apply (ya está corriendo).
  const titleEl = document.getElementById('mkv-apply-modal-title');
  const subEl = document.getElementById('mkv-apply-modal-sub');
  const logEl = document.getElementById('mkv-apply-modal-log');
  const statusEl = document.getElementById('mkv-apply-modal-status');
  const closeBtn = document.getElementById('mkv-apply-modal-close-btn');
  const cancelBtn = document.getElementById('mkv-apply-modal-cancel-btn');
  if (!titleEl) return;  // DOM no listo aún; el siguiente entrar a Tab 2 reintentará.
  titleEl.textContent = 'Reanudando seguimiento del apply…';
  const fileLabel = st.file_name ? ` · ${st.file_name}` : '';
  subEl.textContent = `Operación en curso${fileLabel} (lanzada en otra sesión)`;
  logEl.style.display = 'none';
  logEl.textContent = '';
  statusEl.innerHTML = (st.step === 'copying')
    ? '<div style="text-align:left"><div class="progress-bar-wrap"><div id="mkv-apply-progress-fill" class="progress-bar-fill" style="width:0%"></div></div><div id="mkv-apply-progress-text" style="font-size:12px; color:var(--text-3)">Sincronizando con el progreso…</div></div>'
    : '<span class="spinner-inline"></span> Sincronizando con el backend…';
  if (cancelBtn) cancelBtn.style.display = (st.step === 'copying') ? '' : 'none';
  closeBtn.style.display = 'none';
  _mkvApplyUserCancelled = false;
  openModal('mkv-apply-modal');
  showToast('🔄 Hay una copia/edición de MKV en curso desde otra sesión — reanudando seguimiento', 'info');
  // Pinta el primer estado inmediatamente
  _renderMkvApplyProgress(st);
  // Lanzar polling local que sigue hasta que step sea terminal. Sin POST
  // que esperar — el backend ya está procesando.
  _mkvApplyResumePolling = true;
  let polling = true;
  let lastStep = st.step;
  const tick = async () => {
    if (!polling) return;
    try {
      const stNew = await apiFetch('/api/mkv/apply/progress', { silent: true });
      if (stNew && polling) {
        _renderMkvApplyProgress(stNew);
        if (stNew.step === 'applying' && cancelBtn) cancelBtn.style.display = 'none';
        if (stNew.step === 'done' || stNew.step === 'error' || stNew.step === 'cancelled') {
          polling = false;
          if (cancelBtn) cancelBtn.style.display = 'none';
          if (stNew.step === 'done') {
            titleEl.textContent = 'Cambios aplicados';
            statusEl.innerHTML = '<span style="color:var(--green)">✓ Operación completada</span>';
          } else if (stNew.step === 'cancelled') {
            titleEl.textContent = 'Cancelado';
            statusEl.innerHTML = '<span style="color:var(--orange)">⚠ Copia cancelada — destino parcial borrado</span>';
          } else {
            titleEl.textContent = 'Error';
            statusEl.innerHTML = `<span style="color:var(--red)">⚠ ${escHtml(stNew.error || 'Error desconocido')}</span>`;
          }
          closeBtn.style.display = '';
        }
        lastStep = stNew.step;
      }
    } catch (_) { /* ignora errores de poll */ }
    if (polling) setTimeout(tick, 1000);
    else _mkvApplyResumePolling = false;
  };
  setTimeout(tick, 500);
}

async function cancelMkvApply() {
  _mkvApplyUserCancelled = true;
  // Feedback inmediato: deshabilita el botón mientras esperamos al backend
  const cancelBtn = document.getElementById('mkv-apply-modal-cancel-btn');
  if (cancelBtn) {
    cancelBtn.disabled = true;
    cancelBtn.textContent = 'Cancelando…';
  }
  await apiFetch('/api/mkv/apply/cancel', { method: 'POST', silent: true });
  // Restauramos el botón al estado base por si el flujo se reabre después
  // — el `display:none` lo gestiona el flujo principal en el finally.
  if (cancelBtn) {
    cancelBtn.disabled = false;
    cancelBtn.textContent = '🛑 Cancelar copia';
  }
}

/** Pinta la barra de progreso de la copia + texto con bytes/ETA. */
function _renderMkvApplyProgress(st) {
  const fill = document.getElementById('mkv-apply-progress-fill');
  const text = document.getElementById('mkv-apply-progress-text');
  const titleEl = document.getElementById('mkv-apply-modal-title');
  if (!fill || !text) return;
  if (st.step === 'copying') {
    if (titleEl) titleEl.textContent = 'Copiando MKV a Output…';
    fill.style.width = `${st.pct || 0}%`;
    const copiedGb = (st.bytes_copied || 0) / 1e9;
    const totalGb  = (st.total_bytes  || 0) / 1e9;
    const eta = st.eta_s > 0 ? ` · ETA ${_fmtSecs(st.eta_s)}` : '';
    text.textContent = `${copiedGb.toFixed(1)} / ${totalGb.toFixed(1)} GB (${st.pct || 0}%)${eta}`;
  } else if (st.step === 'applying') {
    if (titleEl) titleEl.textContent = 'Aplicando cambios…';
    fill.style.width = '100%';
    text.textContent = '✓ Copia completada — ejecutando mkvpropedit…';
  } else if (st.step === 'done') {
    fill.style.width = '100%';
    text.textContent = '✓ Cambios aplicados';
  } else if (st.step === 'cancelled') {
    text.textContent = '⚠ Copia cancelada — destino parcial borrado';
  } else if (st.step === 'error') {
    text.textContent = `⚠ ${st.error || 'Error desconocido'}`;
  }
}

/** Formatea segundos como "Xh Ym" o "Ym Ks" o "Ks". */
function _fmtSecs(s) {
  if (!s || s < 0) return '0s';
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const ss = Math.round(s % 60);
  if (m < 60) return `${m}m ${ss}s`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return `${h}h ${mm}m`;
}

// ── Utility ──────────────────────────────────────────────────────

function _fmtBytes(bytes) {
  if (bytes >= 1e9) return (bytes / 1e9).toFixed(1) + ' GB';
  if (bytes >= 1e6) return (bytes / 1e6).toFixed(1) + ' MB';
  return (bytes / 1e3).toFixed(0) + ' KB';
}

function _fmtDuration(seconds) {
  if (!seconds) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}min` : `${m}min`;
}

// ═══════════════════════════════════════════════════════════════════
//  TAB 3 — CMv4.0 BD (inyección de RPU Dolby Vision CMv4.0)
// ═══════════════════════════════════════════════════════════════════

/** Proyectos CMv4.0 abiertos. Cada entrada: {id, subTabId, session, ws, syncData} */
const openCMv40Projects = [];
let activeCMv40SubTabId = null;
let _cmv40SourceSelected = null;
let _cmv40SidebarList = [];
let _cmv40SelectedSidebarId = null;
let _cmv40SortKey = 'modified';
let _cmv40SortDir = 'desc';
// Flag de "auto-resume hecho" — solo intentamos abrir automáticamente el
// proyecto running una vez por sesión de Tab 3 (al primer load). Si el
// usuario cierra el proyecto manualmente, NO reabrimos. Se reset al cambiar
// de tab principal (ver switchTab) para que la siguiente entrada al Tab 3
// vuelva a evaluarlo.
let _cmv40AutoResumeAttempted = false;
let _cmv40Filter = 'all';

// Icono por fase (para el badge del sidebar)
const CMV40_PHASE_ICONS = {
  'created':         '🎨',
  'source_analyzed': '🔍',
  'target_provided': '🎯',
  'extracted':       '✂️',
  'sync_verified':   '📊',
  'sync_corrected':  '📊',
  'injected':        '💉',
  'remuxed':         '📦',
  'validated':       '✅',
  'done':            '✅',
  'error':           '❌',
  'cancelled':       '⏹',
};

const MAX_CMV40_PROJECTS = 5;

// Label humano por nombre de fase (running_phase)
const CMV40_RUNNING_LABELS = {
  'analyze_source':  'Fase A — Analizando MKV origen',
  'target_rpu_mkv':  'Fase B — Extrayendo RPU target',
  'target_rpu_drive':'Fase B — Descargando RPU del repositorio DoviTools',
  'target_rpu_path': 'Fase B — Cargando RPU de carpeta local',
  'extract':         'Fase C — Extrayendo BL/EL y datos per-frame',
  'sync_correct':    'Fase E — Aplicando corrección de sincronización',
  'inject':          'Fase F — Inyectando RPU en EL',
  'remux':           'Fase G — Remuxando MKV final',
  'validate':        'Fase H — Validando MKV final',
};

// Ratios empíricos calibrados contra logs reales del NAS ZFS con Dolby Vision
// P7 FEL drop-in (Zootrópolis 2, 155001 frames, 34 GB HEVC).
// Recalibrados tras aislar per_frame_data regeneration del Fase F real:
// antes el inject aparecia ~407s porque tenia export dovi_tool concurrente
// de 2 min encima. Sin contaminacion el inject real es ~283s → fps ~548.
// Si se cambia de hardware (SSD local vs ZFS sobre HDD), revisitar.
const CMV40_ETA = {
  // ratio respecto a wall time de ffmpeg (fase A) — observados:
  r_extract_rpu: 0.84,   // 157/186 observado (antes 0.92)
  r_demux:       1.30,   // (sin medir en drop-in, valor legacy)
  r_export:      0.19,   // (sin medir en drop-in, valor legacy)
  r_inject:      2.15,   // 388/180 observado en drop-in FEL (2 runs: 387s, 388s). Antes 1.55 era subestimacion
  r_mux:         2.00,   // 373/186 observado (antes 2.15)
  // FPS de cada tool (fallback cuando no hay anchor)
  fps_extract:   1550,   // 155001/100 ≈ 1550
  fps_demux:     1100,
  fps_export:    7000,
  fps_inject:    400,    // 155001/388 observado en drop-in (antes 545 era subestimacion)
  fps_mux:       415,    // 155001/373 ≈ 415 (antes 711)
  // Fallback inicial cuando aun no tenemos ffmpeg_wall_seconds ni tamaño
  // del fichero (sesiones legacy sin parser de tracks). Los usuarios típicos de UHD BD manejan
  // 60-70 GB — ffmpeg extract ~280-330s en NAS ZFS a ~220 MB/s. Antes era 180
  // (caso 35-42 GB) que subestimaba. Con tamaño en session usamos scaling en
  // _cmv40FallbackAnchor; este valor solo aplica a sesiones viejas sin size.
  ffmpeg_wall_fallback_s: 260,
};

/** Deduce el label de la siguiente fase que el auto-pipeline disparara,
 *  a partir del phase actual (sin running_phase). Usado en el subtitulo del
 *  overlay durante el "puente" entre fases para mostrar algo util en vez
 *  del antiguo "Preparando siguiente fase..." vago. */
function _cmv40GuessNextPhase(s) {
  const trust = !!s.target_trust_ok && s.trust_override !== 'force_interactive';
  switch (s.phase) {
    case 'created':          return 'Fase A — Analizando MKV origen';
    case 'source_analyzed':  return 'Fase B — Preparando RPU target';
    case 'target_provided':  return 'Fase C — Separando capas';
    case 'extracted':        return trust ? 'Fase F — Inyectando RPU (drop-in)' : 'Fase D — Revisión visual';
    case 'sync_verified':    return 'Fase F — Inyectando RPU';
    case 'sync_corrected':   return 'Fase F — Inyectando RPU';
    case 'injected':         return 'Fase G — Ensamblando MKV';
    case 'remuxed':          return 'Fase H — Validando resultado';
    case 'validated':        return 'Fase H — Finalizando';
    default:                 return '';
  }
}

// Mapeo de running_phase backend → step key del timeline
const CMV40_RUNNING_TO_STEP = {
  'preflight':        'PREFLIGHT',
  'analyze_source':   'A',
  'target_rpu_path':  'B',
  'target_rpu_mkv':   'B',
  'target_rpu_drive': 'B',
  'extract':          'C',
  'sync_correct':     'E',
  'inject':           'F',
  'remux':            'G',
  'validate':         'H',
};

/** Duración (segs) real de un step completado, leída de session.phase_history.
 *  Devuelve null si no hay entrada o falta alguna marca de tiempo. */
function _cmv40StepElapsedSecs(stepKey, s) {
  const hist = s && s.phase_history;
  if (!Array.isArray(hist) || !hist.length) return null;
  // Recorremos en orden y acumulamos duración de TODAS las entradas cuyo
  // running_phase mapea al stepKey (Fase B puede tener varios intentos).
  let total = 0, found = false;
  for (const h of hist) {
    if (!h || !h.phase) continue;
    if (CMV40_RUNNING_TO_STEP[h.phase] !== stepKey) continue;
    if (!h.started_at || !h.finished_at) continue;
    const a = Date.parse(h.started_at);
    const b = Date.parse(h.finished_at);
    if (!isFinite(a) || !isFinite(b) || b < a) continue;
    total += (b - a) / 1000;
    found = true;
  }
  return found ? total : null;
}

/** Calcula el anchor fallback (segundos estimados de ffmpeg extract) a partir
 *  del tamaño del MKV origen. El usuario tipico de UHD BD maneja 40-70 GB;
 *  usar 180s constante subestimaba cuando el MKV era grande.
 *
 *  Calibracion: NAS ZFS observado ~239 MB/s en ffmpeg -c copy. Tomamos 220
 *  MB/s como margen conservador (mejor sobreestimar el ETA que quedarse corto).
 *  Para 42 GB sale ~195s (observado 182s, +7%), para 65 GB ~302s, para 70 GB ~326s.
 */
function _cmv40FallbackAnchor(s) {
  const size = s && s.source_file_size_bytes;
  if (size && size > 0) {
    const mbps = 220;                       // MB/s — margen conservador
    const secs = size / (mbps * 1024 * 1024);
    return Math.max(60, Math.min(900, secs));   // clamp [60s, 15min]
  }
  return CMV40_ETA.ffmpeg_wall_fallback_s;   // 180s por defecto si no hay size
}

/** Estima segundos de una sub-tarea usando ffmpeg wall time (anchor) o
 *  frame_count × fps como fallback. */
function _cmv40EstimateSecs(s, ratio, fps) {
  if (s.ffmpeg_wall_seconds && s.ffmpeg_wall_seconds > 0) {
    return Math.max(5, s.ffmpeg_wall_seconds * ratio);
  }
  if (s.source_frame_count && s.source_frame_count > 0) {
    return Math.max(5, s.source_frame_count / fps);
  }
  // Fallback escalado al tamaño del fichero origen (si disponible), evitando
  // el salto de "5 min → 25 min" cuando llega el anchor real de Fase A.
  return Math.max(5, _cmv40FallbackAnchor(s) * ratio);
}

/** Formatea segundos como "Xm Ys" o "~Xm". */
function _cmv40FmtEta(secs) {
  if (!secs || secs <= 0) return '—';
  const s = Math.round(secs);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 10 && rem > 0) return `${m}m ${rem}s`;
  return `${m}m`;
}

/** Plan de pasos del auto-pipeline según workflow + trust del proyecto.
 *  Devuelve array ordenado de objetos con {key, icon, title, what, etaSecs}. */
function _cmv40PlanAutoSteps(s) {
  const wf = s.source_workflow || 'p7_fel';
  const trust = !!s.target_trust_ok && s.trust_override !== 'force_interactive';
  const dropIn = trust && s.target_type === 'trusted_p7_fel_final' && wf === 'p7_fel';
  const skipped = s.phases_skipped || [];

  // ETAs estimados
  const anchor = s.ffmpeg_wall_seconds || 0;
  const etaA = anchor > 0 ? anchor * (1 + CMV40_ETA.r_extract_rpu) : _cmv40EstimateSecs(s, 1.0 + CMV40_ETA.r_extract_rpu, CMV40_ETA.fps_extract);
  const etaB = s.target_rpu_source === 'drive' ? 30
             : s.target_rpu_source === 'mkv'   ? _cmv40EstimateSecs(s, 1.0 + CMV40_ETA.r_extract_rpu, CMV40_ETA.fps_extract)
             : 10;  // path: copia local
  // Drop-in trusted: Fase C no hace nada — cero demux, cero per_frame.
  // Esta es la parte que mas desajustaba el ETA antes: el etaC calculado
  // (~300-400s en un UHD BD) inflaba el total inicial y luego se evaporaba
  // al detectar trust, causando el salto visible de 25 → 15 min.
  const etaDemux = (wf === 'p8' || dropIn) ? 0 : _cmv40EstimateSecs(s, CMV40_ETA.r_demux, CMV40_ETA.fps_demux);
  const etaExport = _cmv40EstimateSecs(s, CMV40_ETA.r_export * 2, CMV40_ETA.fps_export);  // ×2 por ambos RPUs
  const etaC = etaDemux + ((trust || dropIn) ? 0 : etaExport);
  const etaF = _cmv40EstimateSecs(s, CMV40_ETA.r_inject, CMV40_ETA.fps_inject);
  const etaG = (wf === 'p7_fel') ? _cmv40EstimateSecs(s, CMV40_ETA.r_mux, CMV40_ETA.fps_mux) : 30;
  // Fase H: depende del modo. Calibrado con runs reales en NAS UHD BD:
  // - Drop-in FEL (caso típico): ffprobe + mkvmerge -J + rename atómico.
  //   ffprobe sobre MKV 71 GB → ~1s; mkvmerge -J → ~1s; rename mismo
  //   filesystem instantáneo; cleanup unlinks <1s. Total real ~3-5s; 5s
  //   con margen.
  // - Path clásico (merge CMv4.0): extract-rpu COMPLETO del HEVC pre-mux
  //   + dovi_tool info + mkvmerge -J. El extract-rpu sobre el HEVC entero
  //   (~60-80 GB en UHD) toma ~5-8 min (heurística backend: hevc_gb/30*3
  //   a hevc_gb/30*5 min). Sin ancla ffmpeg_wall_seconds usamos 240s
  //   (4 min) como fallback razonable; si tenemos ancla, el extract-rpu
  //   ronda 0.92× ffmpeg wall time (mismo ratio que Fase A).
  const etaH = dropIn ? 5 : (anchor > 0 ? Math.round(anchor * 0.92) : 240);

  const steps = [];

  // Pre-flight: sniff DV del origen + dovi_tool info del bin target.
  // Backend: running_phase='preflight'. Phase backend NO cambia (sigue
  // 'created'); el progreso se trackea via source_preflight_ok flag.
  // Edge cases manejados en _cmv40StepStatus mapping de PREFLIGHT.
  const phasePastSource = CMV40_PHASES_ORDER.indexOf(s.phase) >= CMV40_PHASES_ORDER.indexOf('source_analyzed');
  let preflightStatus;
  if (s.running_phase === 'preflight') {
    preflightStatus = 'running';
  } else if (s.source_preflight_ok === true || phasePastSource) {
    preflightStatus = 'done';
  } else if (s.running_phase === 'analyze_source') {
    // Sesion legacy o pre-flight saltado: Fase A corriendo sin flag de
    // preflight. Lo marcamos como skipped para no confundir.
    preflightStatus = 'skipped';
  } else {
    preflightStatus = 'pending';
  }
  steps.push({
    key: 'PREFLIGHT', icon: '🔬', title: 'Pre-flight · Validación rápida',
    what: 'Sniff DV del MKV origen + descarga + dovi_tool info del bin + análisis combos L2/L8 + clasificación de calidad (CMv4 CORE/CORE+/FULL) + recomendación Mantener vs Inyectar RPU (~30-60s, aborta o recomienda Mantener antes de gastar Fase A)',
    etaSecs: 45,
    forcedStatus: preflightStatus,
  });

  steps.push({
    key: 'A', icon: '🔍', title: 'Fase A · Analizar MKV origen',
    what: 'ffmpeg copia el HEVC + dovi_tool extract-rpu + info + análisis combos L2 del source + comparación L2 source vs target → recomendación final del modelo (drop-in / merge / mantener)',
    etaSecs: etaA,
  });
  // Fase B: si el pre-flight ya descargó/copió/extrajo el bin, aquí se reusa
  // del workdir y solo se re-evalúan los trust gates con los datos del source
  // recién extraído en Fase A. Texto refleja ese rol real.
  const bWhat = s.target_rpu_source === 'drive' ? 'Reusa el bin del workdir (descargado en pre-flight) + re-evalúa trust gates con datos del source: frames, L5/L6 zoneados, compatibilidad estructural'
              : s.target_rpu_source === 'mkv' ? 'Reusa el RPU del workdir (extraído en pre-flight) + re-evalúa trust gates con datos del source: frames, L5/L6, compatibilidad'
              : 'Reusa el bin del workdir (copiado en pre-flight) + re-evalúa trust gates con datos del source: frames, L5/L6, compatibilidad';
  steps.push({
    key: 'B', icon: '🎯', title: 'Fase B · Preparar RPU target',
    what: bWhat, etaSecs: etaB,
  });

  // Gate B→C: validaciones estructurales + trust gates del target
  // Se evalúa al cerrar Fase B. No gasta tiempo (es una comprobación in-memory).
  // Visible siempre en el timeline para dar trazabilidad de la decisión.
  const curIdxForGate = CMV40_PHASES_ORDER.indexOf(s.phase);
  const targetProvidedIdx = CMV40_PHASES_ORDER.indexOf('target_provided');
  const gateBCStatus = s.compat_warning ? 'error'
                    : (curIdxForGate < targetProvidedIdx) ? 'pending'
                    : 'done';
  const failingGates = Object.entries(s.target_trust_gates || {})
    .filter(([k, v]) => typeof v === 'object' && v && v.ok === false)
    .map(([k]) => k);
  let gateBCLabel;
  if (s.compat_warning) {
    gateBCLabel = 'incompatible · abortada';
  } else if (curIdxForGate < targetProvidedIdx) {
    gateBCLabel = 'pendiente';
  } else if (s.target_trust_ok) {
    gateBCLabel = 'trusted ✓';
  } else if (failingGates.length) {
    gateBCLabel = `${failingGates.length} gate${failingGates.length > 1 ? 's' : ''} ⚠ revisión manual`;
  } else {
    gateBCLabel = 'flujo manual';
  }
  const gateBCWhat = s.compat_warning
    ? s.compat_warning.slice(0, 140) + (s.compat_warning.length > 140 ? '…' : '')
    : 'Comparación target vs source RPU: frames · CM version · L8 · L5/L6/L1 · compatibilidad estructural';
  steps.push({
    key: 'GATE_BC', icon: '🛡️', title: 'Validaciones — trust gates + compatibilidad',
    what: gateBCWhat, etaSecs: 0,
    forcedStatus: gateBCStatus, customLabel: gateBCLabel,
    isGate: true,
  });

  // Fase C: si el backend marco tanto demux_dual_layer como per_frame_data_skipped,
  // la fase no hizo trabajo real (caso drop-in trusted) — mostrar como 'skipped'
  // en el timeline con label descriptivo en vez de 'done · 00:00'.
  const demuxSkipped = skipped.includes('demux_dual_layer');
  const pfdSkipped   = skipped.includes('per_frame_data_skipped');
  const cFullySkipped = demuxSkipped && (pfdSkipped || (wf === 'p8' && skipped.length));
  let cWhat, cForcedStatus = null, cLabel = null;
  if (cFullySkipped) {
    cWhat = dropIn
      ? 'Omitida — drop-in FEL: sin demux ni per-frame (inject directo sobre source.hevc)'
      : 'Omitida — target trusted, no se necesitan capas separadas ni chart';
    cForcedStatus = 'skipped';
    cLabel = 'omitida · drop-in';
  } else {
    cWhat = (wf === 'p8') ? 'Workflow P8 — sin demux' + (trust ? ' (per-frame omitido)' : ', genera per-frame data')
                          : 'dovi_tool demux → BL' + (wf === 'p7_fel' ? ' + EL' : '') + (trust ? ' (per-frame omitido)' : ' + per-frame data');
  }
  steps.push({
    key: 'C', icon: '✂️', title: 'Fase C · Demux + per-frame',
    what: cWhat, etaSecs: cFullySkipped ? 0 : etaC,
    forcedStatus: cForcedStatus, customLabel: cLabel,
  });
  steps.push({
    key: 'D', icon: '📊', title: 'Fase D · Verificar sincronización',
    what: trust
      ? 'Omitida — gates validaron frame count + L5/L6/L8'
      : 'Chart interactivo de sincronización: alinear las curvas MaxCLL de source y target (correlación Pearson ≥ 85% + Δ frames = 0) antes de inyectar',
    etaSecs: trust ? 0 : null,   // null = desconocido (interactivo)
    forcedStatus: trust ? 'skipped' : null,
  });
  // Fase E — corrección de sync (dovi_tool editor remove/duplicate).
  // Estado depende de la combinación (trust, hasSyncCfg, fase actual):
  //   · trusted+auto                       → omitida por gates
  //   · no-trusted + fase < sync_verified  → PENDING (aún no sabemos si hará falta)
  //   · no-trusted + fase ≥ sync_verified + sin sync_config → omitida (Δ=0)
  //   · con sync_config                    → aplicada (se ejecutó Fase E)
  //   · running_phase == 'sync_correct'    → running (cubierto por el mapping)
  const hasSyncCfg = !!(s.sync_config && Object.keys(s.sync_config).length);
  const curIdx = CMV40_PHASES_ORDER.indexOf(s.phase);
  const syncVerIdx = CMV40_PHASES_ORDER.indexOf('sync_verified');
  const pastSyncVerified = curIdx >= syncVerIdx;
  let eStatus = null, eLabel = null;
  if (trust) {
    eStatus = 'skipped';
    eLabel = 'omitida · gates Δ=0';
  } else if (hasSyncCfg) {
    // Corrección aplicada; _cmv40StepStatus decide done/running/pending según
    // la fase actual. El customLabel se usa cuando esté done.
    eLabel = 'aplicada';
    eStatus = null;
  } else if (pastSyncVerified) {
    // Usuario confirmó sync sin corrección — Δ era 0 tras revisión.
    eStatus = 'skipped';
    eLabel = 'omitida · Δ=0 confirmado';
  }
  // (caso restante: no-trusted + sin sync_config + pre-sync_verified →
  //  eStatus/eLabel null → _cmv40StepStatus decide 'pending'.)
  const eWhat = hasSyncCfg
    ? 'dovi_tool editor — remove/duplicate frames según config'
    : (trust || pastSyncVerified
        ? 'No requerida — el RPU target alinea con el source'
        : 'Solo si Fase D detecta desfase de frames');
  steps.push({
    key: 'E', icon: '🔧', title: 'Fase E · Corrección de sync',
    what: eWhat,
    etaSecs: hasSyncCfg ? 20 : 0,
    forcedStatus: eStatus,
    customLabel: eLabel,
  });
  // Fase F: la ruta concreta depende del workflow y del target_type.
  // - drop-in FEL: inyecta el bin sobre source.hevc (BL+EL juntos), sin merge ni mux.
  // - p7_fel non-drop-in: merge CMv4.0 sobre RPU P7 + inyecta en EL.hevc.
  // - p7_mel y p8: merge solo cuando target ∈ {p7_fel_final, p7_mel_final, generic};
  //   con target P8 retail (trusted_p8_source) es inject directo sin merge.
  //   Alineado con _do_merge() y target_needs_merge en cmv40_pipeline.py.
  const targetNeedsMerge = ['trusted_p7_fel_final', 'trusted_p7_mel_final', 'generic'].includes(s.target_type);
  let fWhat;
  if (dropIn) {
    fWhat = 'Drop-in — inyecta el RPU del bin sobre source.hevc (BL+EL juntos, sin merge ni mux posterior)';
  } else if (wf === 'p7_fel') {
    fWhat = 'Merge CMv4.0 sobre RPU P7 del source + inyecta el RPU merged en EL.hevc (preserva FEL)';
  } else if (wf === 'p7_mel') {
    fWhat = targetNeedsMerge
      ? 'Merge CMv4.0 sobre RPU P7 MEL del source + inyecta el RPU merged en BL.hevc (descarta EL MEL → P8.1)'
      : 'Inyecta el RPU target directamente en BL.hevc (target P8 retail, sin merge — descarta EL MEL → P8.1)';
  } else {  // p8
    fWhat = targetNeedsMerge
      ? 'Merge CMv4.0 sobre RPU P8 del source + inyecta el RPU merged en source.hevc'
      : 'Inyecta el RPU target directamente en source.hevc (target P8 retail, sin merge)';
  }
  steps.push({
    key: 'F', icon: '💉', title: 'Fase F · Inyectar RPU',
    what: fWhat, etaSecs: etaF,
  });
  // Fase G: tres rutas distintas según workflow/modo.
  // - drop-in FEL: source_injected.hevc ya es BL+EL dual-layer → mkvmerge directo.
  // - p7_fel non-drop-in: dovi_tool mux combina BL + EL_injected → mkvmerge.
  // - p7_mel / p8: BL_injected.hevc single-layer → mkvmerge directo.
  let gWhat;
  if (dropIn) {
    gWhat = 'mkvmerge directo sobre source_injected.hevc (BL+EL dual-layer ya combinado en Fase F) con audio/subs/capítulos del MKV origen';
  } else if (wf === 'p7_fel') {
    gWhat = 'dovi_tool mux combina BL.hevc + EL_injected.hevc en un HEVC dual-layer + mkvmerge añade audio/subs/capítulos del MKV origen';
  } else {  // p7_mel / p8
    gWhat = 'Sin mux dual-layer (single-layer) — mkvmerge directo sobre BL_injected.hevc con audio/subs/capítulos del MKV origen';
  }
  steps.push({
    key: 'G', icon: '📦', title: 'Fase G · Remux MKV final',
    what: gWhat, etaSecs: etaG,
  });

  // Fase H = validar + finalizar. El backend unifica en running_phase='validate'
  // dos rutas según modo:
  // - Drop-in FEL: ffprobe (frame count) + mkvmerge -J. Sin extract-rpu
  //   porque la cadena upstream ya garantiza Profile 7 FEL CMv4.0. ~5-10s.
  // - Path clásico (merge CMv4.0): extract-rpu COMPLETO del HEVC pre-mux
  //   (BL_injected/EL_injected/source_injected según workflow) + dovi_tool
  //   info → valida frame count del RPU vs expected (±2), cm_version=v4.0,
  //   el_type correcto, L8 presente. Después mkvmerge -J. ~5-8 min en UHD.
  //   NO usa muestreo HEAD+TAIL: aunque más lento, garantiza frame count
  //   total del RPU (un bug que cortara el RPU a la mitad pasaría
  //   desapercibido con muestreo).
  // En ambos: rename atómico .tmp → .mkv + cleanup pre-mux.
  steps.push({
    key: 'H', icon: '✅', title: 'Fase H · Validar + finalizar',
    what: dropIn
      ? 'Validación rápida (ffprobe frame count + mkvmerge -J — el RPU es bit-a-bit el bin pre-validado) → rename atómico → cleanup'
      : 'Validación rigurosa: extract-rpu completo del HEVC pre-mux + dovi_tool info → confirma frame count, CMv4.0, el_type, L8 presente. Más mkvmerge -J. → rename atómico → cleanup',
    etaSecs: etaH,
  });

  return steps;
}

/** Estado de cada step según session.phase + running_phase + phases_skipped. */
function _cmv40StepStatus(step, s) {
  if (step.forcedStatus) return step.forcedStatus;
  const PROD = {
    A: 'source_analyzed', B: 'target_provided', C: 'extracted',
    D: 'sync_verified',   E: 'sync_verified',   F: 'injected',
    G: 'remuxed',         H: 'done',
  };
  const order = CMV40_PHASES_ORDER;
  const produces = PROD[step.key];
  const curIdx = order.indexOf(s.phase);
  const prodIdx = order.indexOf(produces);
  const runStep = CMV40_RUNNING_TO_STEP[s.running_phase];

  if (runStep === step.key) return 'running';
  if (prodIdx >= 0 && curIdx >= prodIdx) return 'done';
  return 'pending';
}

// Formatea segundos → "MM:SS" (o "HH:MM:SS" si pasa de 1h)
function _cmv40FmtClock(totalSecs) {
  totalSecs = Math.max(0, Math.floor(totalSecs || 0));
  const h = Math.floor(totalSecs / 3600);
  const m = Math.floor((totalSecs % 3600) / 60);
  const s = totalSecs % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** Devuelve el started_at del proyecto en ms (epoch). Cacheado en
 *  project._resolvedStartedMs para que los tres lugares que computan el
 *  elapsed (full render, incremental update, tick por segundo) usen
 *  EXACTAMENTE el mismo valor.
 *
 *  Sin este cache el render puede usar server-time
 *  (phase_history[0].started_at) mientras el tick lee data-started-at
 *  cacheado en cliente — al alternar uno y otro el contador "salta 3
 *  segundos" y luego "resta 2" porque server clock != client clock por
 *  la latencia de la API. Bug visible al usuario como timer no lineal.
 *
 *  Prioridad para el primer cache:
 *    1. phase_history[0].started_at (autoritativo, server time)
 *    2. Date.now() (solo si hay running o auto activo)
 *  Una vez cacheado, NO se recalcula — la fuente queda fija. */
function _cmv40ResolveStartedMs(s, project) {
  if (project && project._resolvedStartedMs) return project._resolvedStartedMs;
  const hist = (s && s.phase_history) || [];
  const firstWithTime = hist.find(h => h.started_at);
  let startedMs = firstWithTime ? Date.parse(firstWithTime.started_at) : 0;
  if (!startedMs && project) {
    if (s.running_phase || (project.autoContinue && !s.error_message && s.phase !== 'done')) {
      startedMs = Date.now();
    }
  }
  if (startedMs && project) {
    project._resolvedStartedMs = startedMs;
  }
  return startedMs || 0;
}

// Ticker único global que actualiza todos los timers vivos cada segundo.
// Re-calcula elapsed y remaining cada segundo. Elapsed = now - started_at.
// Remaining = baseRemaining (snapshot en render) - (now - baseAt). Así
// decrementa suavemente segundo a segundo entre renders, y solo "salta" al
// recomputar cuando llega una actualización de sesión (transición de fase).
function _cmv40EnsureTimerTick() {
  if (window._cmv40TimerTick) return;
  window._cmv40TimerTick = setInterval(() => {
    document.querySelectorAll('.cmv40-tl-timer-elapsed[data-started-at]').forEach(el => {
      const started = parseInt(el.dataset.startedAt, 10);
      if (!started) return;
      const elapsed = (Date.now() - started) / 1000;
      el.textContent = _cmv40FmtClock(elapsed);
      // Remaining: decrementa desde la snapshot del último render.
      const remainEl = el.parentElement?.parentElement?.querySelector('.cmv40-tl-timer-remaining');
      const baseRem = parseFloat(el.dataset.baseRemaining || 'NaN');
      const baseAt  = parseFloat(el.dataset.baseAt || 'NaN');
      if (remainEl && isFinite(baseRem) && isFinite(baseAt)) {
        const delta = (Date.now() - baseAt) / 1000;
        const remaining = Math.max(0, baseRem - delta);
        remainEl.textContent = remaining > 0 ? `~${_cmv40FmtClock(remaining)} restantes (auto)` : 'casi listo…';
      }
    });
  }, 1000);
}

/** Segundos restantes estimados para las fases AUTO pendientes de ejecución.
 *  Suma los etaSecs de pasos no-done/no-skipped, descontando el tiempo que
 *  lleva ejecutándose el paso en curso. Fase D manual (etaSecs=null) no cuenta. */
function _cmv40ComputeRemainingSecs(s, steps, stepStatuses, hist) {
  let remaining = 0;
  for (let i = 0; i < steps.length; i++) {
    const status = stepStatuses[i];
    if (status === 'done' || status === 'skipped') continue;
    const eta = steps[i].etaSecs || 0;   // null (manual) → 0
    remaining += eta;
  }
  // Descontar el tiempo que lleva ejecutándose la fase actual (si existe)
  if (s.running_phase && Array.isArray(hist)) {
    const curEntry = [...hist].reverse().find(h => h.phase === s.running_phase);
    if (curEntry && curEntry.started_at && !curEntry.finished_at) {
      const startMs = Date.parse(curEntry.started_at);
      if (isFinite(startMs)) {
        const runningSecs = (Date.now() - startMs) / 1000;
        remaining = Math.max(0, remaining - runningSecs);
      }
    }
  }
  return Math.max(0, Math.round(remaining));
}

/** Renderiza el timeline lateral del auto-pipeline (HTML). */
function _cmv40RenderTimeline(s, project) {
  const steps = _cmv40PlanAutoSteps(s);
  // Progreso por #pasos completados (done + skipped) sobre total.
  const stepStatuses = steps.map(st => _cmv40StepStatus(st, s));
  const doneCount = stepStatuses.filter(st => st === 'done' || st === 'skipped').length;
  const totalCount = steps.length;
  const progressPct = totalCount > 0 ? Math.round((doneCount / totalCount) * 100) : 0;

  // Timer — arranque del pipeline cacheado por proyecto via _cmv40ResolveStartedMs.
  // Imprescindible que sea el MISMO valor en full render, incremental update y
  // tick por segundo: si difieren (p.ej. cache cliente vs server timestamp) el
  // contador alterna entre dos valores → "salta 3 / resta 2" visible al usuario.
  const startedMs = _cmv40ResolveStartedMs(s, project);
  const hist = s.phase_history || [];

  const isTerminal = (s.phase === 'done' || !!s.error_message);
  let elapsedLabel  = '—';
  let remainingText = '';
  let timerAttrs    = '';
  if (startedMs) {
    let elapsedSecs;
    if (isTerminal) {
      const lastWithEnd = [...hist].reverse().find(h => h.finished_at);
      const endMs = lastWithEnd ? Date.parse(lastWithEnd.finished_at) : Date.now();
      elapsedSecs = (endMs - startedMs) / 1000;
      remainingText = s.phase === 'done' ? 'finalizado' : (s.error_message ? 'con error' : '');
    } else {
      elapsedSecs = (Date.now() - startedMs) / 1000;
      // Tiempo restante de fases AUTO pendientes. Excluye fases manuales
      // (etaSecs null = interactiva, p.ej. Fase D no-trusted). Descontamos
      // el tiempo que lleva ejecutándose la fase actual para que el contador
      // baje suavemente durante ella.
      const remaining = _cmv40ComputeRemainingSecs(s, steps, stepStatuses, hist);
      remainingText = remaining > 0 ? `~${_cmv40FmtClock(remaining)} restantes (auto)` : 'casi listo…';
      // data-base-remaining + data-base-at permiten que el tick de 1s
      // decremente suavemente sin recalcular la suma (evita fluctuaciones
      // por cambios de steps.etaSecs entre renders).
      timerAttrs = ` data-started-at="${startedMs}" data-base-remaining="${remaining}" data-base-at="${Date.now()}"`;
      _cmv40EnsureTimerTick();
    }
    elapsedLabel = _cmv40FmtClock(elapsedSecs);
  }

  const itemsHtml = steps.map((st, i) => {
    const status = stepStatuses[i];
    const iconMap = {
      done:    '<span class="cmv40-tl-status-icon done">✓</span>',
      running: '<span class="cmv40-tl-status-icon running"></span>',
      skipped: '<span class="cmv40-tl-status-icon skipped">⏭</span>',
      pending: '<span class="cmv40-tl-status-icon pending"></span>',
      error:   '<span class="cmv40-tl-status-icon error">✗</span>',
    };
    // Tiempo real de ejecución (solo disponible si la fase se ejecutó en backend)
    const elapsed = status === 'done' ? _cmv40StepElapsedSecs(st.key, s) : null;
    // Label por defecto según status, o customLabel si el step lo especifica.
    // Para done, añadimos el tiempo real ej. "completado · 05:29" si lo hay.
    const doneLabel = elapsed != null
      ? `completado · ${_cmv40FmtClock(elapsed)}`
      : 'completado';
    const defaultLabel = status === 'done'    ? doneLabel
                       : status === 'skipped' ? 'omitida'
                       : status === 'running' ? 'en curso…'
                       : status === 'error'   ? 'incompatible'
                       : `ETA ${_cmv40FmtEta(st.etaSecs)}`;
    const label = st.customLabel || defaultLabel;
    const etaHtml = `<span class="cmv40-tl-eta ${status}">${escHtml(label)}</span>`;
    const gateCls = st.isGate ? ' cmv40-tl-is-gate' : '';
    return `<li class="cmv40-tl-step cmv40-tl-${status}${gateCls}" data-step-key="${escHtml(st.key)}">
      <div class="cmv40-tl-rail">${iconMap[status] || iconMap.pending}</div>
      <div class="cmv40-tl-body">
        <div class="cmv40-tl-title">
          <span class="cmv40-tl-phase-icon">${st.icon}</span>
          <span>${escHtml(st.title)}</span>
        </div>
        <div class="cmv40-tl-what">${escHtml(st.what)}</div>
        ${etaHtml}
      </div>
    </li>`;
  }).join('');

  // Badge 3-estado del modo de ejecucion:
  //   1. Automatico · pendiente de validaciones — antes de Fase B (aun no
  //      se sabe si trusted) o durante Fase B (evaluando gates)
  //   2. Automatico · trusted — gates OK, el pipeline encadena sin revision
  //      manual (drop-in FEL, retail P8, etc)
  //   3. Manual · revision visual — gates no pasan o usuario forzo force_interactive
  //      (Fase D requiere revision en el chart)
  const gatesEvaluated = !!(s.target_trust_gates && Object.keys(s.target_trust_gates).length);
  const targetProvidedIdx = CMV40_PHASES_ORDER.indexOf('target_provided');
  const curPhaseIdx = CMV40_PHASES_ORDER.indexOf(s.phase);
  const beforeGates = curPhaseIdx < targetProvidedIdx || !gatesEvaluated;
  let trustBadge;
  if (beforeGates) {
    trustBadge = '<span class="cmv40-tl-trust-badge pending">⏳ Auto · pendiente validaciones</span>';
  } else if (s.target_trust_ok && s.trust_override !== 'force_interactive') {
    trustBadge = '<span class="cmv40-tl-trust-badge trusted">🚀 Auto · trusted</span>';
  } else {
    trustBadge = '<span class="cmv40-tl-trust-badge manual">🔬 Manual · revisión visual</span>';
  }

  const progressCls = isTerminal && !s.error_message ? 'cmv40-tl-progress-done'
                    : s.error_message ? 'cmv40-tl-progress-error'
                    : '';

  return `
    <aside class="cmv40-running-timeline">
      <div class="cmv40-tl-header">
        <div class="cmv40-tl-header-top">
          ${trustBadge}
        </div>
        <div class="cmv40-tl-progress ${progressCls}">
          <div class="cmv40-tl-progress-meta">
            <span class="cmv40-tl-timer-block">
              <span class="cmv40-tl-timer">
                <span class="cmv40-tl-timer-icon">⏱</span>
                <span class="cmv40-tl-timer-elapsed"${timerAttrs}>${elapsedLabel}</span>
              </span>
              <span class="cmv40-tl-timer-remaining">${escHtml(remainingText)}</span>
            </span>
            <span class="cmv40-tl-progress-pct">${doneCount}/${totalCount} · ${progressPct}%</span>
          </div>
          <div class="cmv40-tl-progress-track">
            <div class="cmv40-tl-progress-fill" style="width:${progressPct}%"></div>
          </div>
        </div>
      </div>
      <ol class="cmv40-tl-steps">${itemsHtml}</ol>
    </aside>`;
}

// Fases ordenadas secuencialmente
const CMV40_PHASES_ORDER = [
  'created', 'source_analyzed', 'target_provided', 'extracted',
  'sync_verified', 'sync_corrected', 'injected', 'remuxed', 'validated', 'done',
];

// Pretty names por fase
const CMV40_PHASE_LABELS = {
  'created':         'Proyecto creado',
  'source_analyzed': 'Origen analizado',
  'target_provided': 'RPU target listo',
  'extracted':       'BL/EL extraídos',
  'sync_verified':   'Sync verificado',
  'sync_corrected':  'Sync corregido',
  'injected':        'RPU inyectado',
  'remuxed':         'MKV remuxado',
  'validated':       'Validado',
  'done':            'Completado',
  'error':           'Error',
  'cancelled':       'Cancelado',
};

// ── Modal "Nuevo proyecto CMv4.0" ────────────────────────────────

let _cmv40NewTargetTab = 'repo';  // 'repo' | 'path' | 'mkv'
let _cmv40NewTargetSelected = null;  // { kind, value }

/** Punto de entrada al wizard "Nuevo proyecto CMv4.0".
 *  Flujo: file browser primero (paso obligatorio) → al seleccionar MKV se
 *  abre el modal con todo lo demás (target RPU, opciones de auto-pipeline).
 *  Si el usuario cancela el browser sin elegir nada, no se abre nada más. */
async function openNewCMv40Modal() {
  _cmv40SourceSelected = null;
  _cmv40SourceFilename = null;
  _cmv40NewTargetTab = 'repo';
  _cmv40NewTargetSelected = null;
  // Paso 1: file browser. Es la única forma de elegir source MKV ahora.
  openFileBrowser({
    title: 'Nuevo proyecto CMv4.0 · paso 1 de 2',
    subtitle: 'Selecciona el MKV origen (CMv2.9) que quieres procesar',
    roots: [
      { key: 'library',    label: 'Biblioteca', icon: '📚' },
      { key: 'downloaded', label: 'Downloaded', icon: '📥' },
    ],
    onSelect: async (absPath, name) => {
      _cmv40SourceSelected = absPath;
      _cmv40SourceFilename = name;
      // Paso 2: abre el wizard con MKV preseleccionado
      await _showCMv40NewProjectWizard();
    }
  });
}

/** Abre el modal del wizard CMv4.0 ya con el MKV seleccionado.
 *  Llamado desde openNewCMv40Modal (paso 2) o desde "Cambiar MKV"
 *  cuando el usuario quiere reabrir el browser desde dentro del wizard. */
async function _showCMv40NewProjectWizard() {
  const btn = document.getElementById('cmv40-create-btn');
  if (btn) btn.disabled = true;
  const autoCb = document.getElementById('cmv40-new-auto');
  if (autoCb) autoCb.checked = true;
  // Pinta el nombre del MKV seleccionado en el botón de la fila "MKV origen"
  const labelEl = document.getElementById('cmv40-source-btn-label');
  if (labelEl) {
    if (_cmv40SourceFilename) {
      labelEl.textContent = _cmv40SourceFilename;
      labelEl.classList.remove('placeholder');
    } else {
      labelEl.textContent = 'Selecciona MKV…';
      labelEl.classList.add('placeholder');
    }
  }
  // Reset visual de la sección del repo: preview del pipeline + info de
  // candidatos. Sin esto, al reabrir el modal se queda el match anterior.
  const pp = document.getElementById('cmv40-new-pipeline-preview');
  if (pp) { pp.innerHTML = ''; pp.style.display = 'none'; }
  const repoInfo = document.getElementById('cmv40-new-repo-info');
  if (repoInfo) {
    repoInfo.textContent =
      'Se descargará desde la carpeta pública del repositorio DoviTools en Google Drive.';
  }
  // Label del auto-pipeline al estado neutro (sin fases conocidas todavía)
  _cmv40NewUpdateAutoLabel(null);
  _cmv40NewSwitchTargetTab('repo');
  // Si ya hay MKV origen, dispara el lookup de recomendación + repo
  if (_cmv40SourceFilename) {
    _cmv40LoadRecommendation(_cmv40SourceFilename);
    _cmv40NewLoadRepoCandidates();
    _cmv40NewUpdateCreateBtn();
  } else {
    _cmv40LoadRecommendation('');
    _cmv40NewResetRepoList('— Selecciona primero el MKV origen —');
  }
  await _cmv40NewLoadRpus();
  openModal('cmv40-new-modal');
}

// Variables ligadas al picker de MKV origen.
//  _cmv40SourceSelected guarda la RUTA ABSOLUTA del MKV elegido (no solo el
//  filename como antes) — necesario porque el browser navega un árbol con
//  subdirectorios bajo /mnt/library en vez de listar /mnt/output plano.
//  _cmv40SourceFilename guarda solo el nombre, usado para recommendation
//  y match contra el sheet de DoviTools (que matchea por nombre, no path).
let _cmv40SourceFilename = null;

/** Reabre el file browser desde dentro del wizard CMv4.0 (boton "Cambiar MKV").
 *  El browser tiene z-index 220 (vs wizard 200), por lo que se monta ENCIMA
 *  cubriendo el wizard sin necesidad de cerrarlo. Si el usuario selecciona,
 *  actualizamos el state del wizard in-place; si cancela, el browser se
 *  cierra y el wizard re-emerge tal cual estaba. Sin gaps de cobertura modal. */
function openCMv40SourceBrowser() {
  openFileBrowser({
    title: 'Cambiar MKV origen',
    subtitle: 'Selecciona otro MKV para reemplazar el actual',
    roots: [
      { key: 'library',    label: 'Biblioteca', icon: '📚' },
      { key: 'downloaded', label: 'Downloaded', icon: '📥' },
    ],
    onSelect: async (absPath, name) => {
      _cmv40SourceSelected = absPath;
      _cmv40SourceFilename = name;
      const labelEl = document.getElementById('cmv40-source-btn-label');
      if (labelEl) {
        labelEl.textContent = name;
        labelEl.classList.remove('placeholder');
      }
      onCMv40SourceChange(absPath, name);
    }
  });
}

/** Mantenida por compatibilidad (botón ↺ en HTML lo invoca).
 *  Ya no carga /mnt/output — solo limpia el botón para volver a elegir. */
async function loadCMv40SourceList() {
  _cmv40SourceSelected = null;
  _cmv40SourceFilename = null;
  const labelEl = document.getElementById('cmv40-source-btn-label');
  if (labelEl) {
    labelEl.textContent = 'Selecciona MKV…';
    labelEl.classList.add('placeholder');
  }
  _cmv40NewUpdateCreateBtn();
  _cmv40LoadRecommendation('');
}

function onCMv40SourceChange(absPathOrLegacyVal, name) {
  // Compat: si el caller pasa solo un string sin name, asume que era el
  // viejo flujo (filename desde un select). Lo tratamos como filename.
  if (name === undefined) {
    _cmv40SourceFilename = absPathOrLegacyVal || null;
    _cmv40SourceSelected = absPathOrLegacyVal ? '/mnt/output/' + absPathOrLegacyVal : null;
  } else {
    _cmv40SourceSelected = absPathOrLegacyVal || null;
    _cmv40SourceFilename = name || null;
  }
  _cmv40NewUpdateCreateBtn();
  // Recomendación + repo matching usan el FILENAME (por convención del sheet)
  _cmv40LoadRecommendation(_cmv40SourceFilename);
  if (_cmv40NewTargetTab === 'repo') _cmv40NewLoadRepoCandidates();
  else _cmv40NewResetRepoList('— Selecciona primero el MKV origen —');
}

// Token para anular peticiones obsoletas si el usuario cambia de MKV rápido
let _cmv40RecReqId = 0;

async function _cmv40LoadRecommendation(filename) {
  const banner = document.getElementById('cmv40-recommendation-banner');
  if (!banner) return;
  if (!filename) {
    banner.style.display = 'none';
    banner.innerHTML = '';
    banner.className = 'cmv40-rec-banner';
    return;
  }
  const reqId = ++_cmv40RecReqId;
  banner.style.display = 'block';
  banner.className = 'cmv40-rec-banner loading';
  banner.innerHTML = `<div class="cmv40-rec-header">
    <span class="cmv40-rec-spinner-inline"></span>
    <span>Consultando hoja de DoviTools…</span>
  </div>`;
  const qs = '?filename=' + encodeURIComponent(filename);
  const data = await apiFetch('/api/cmv40/recommend-from-filename' + qs);
  if (reqId !== _cmv40RecReqId) return;  // petición obsoleta
  if (!data) {
    banner.style.display = 'none';
    return;
  }
  _cmv40RenderRecommendation(data);
}

// Metadata por columna: icono, label corta, tooltip explicativo
const CMV40_CHIP_META = {
  dv_source:     { icon: '🎬', label: 'Fuente',   help: 'Plataforma de origen del RPU CMv4.0 (iTunes, Disney+, MA, MAX, Fandango, BD-FEL…)' },
  sync:          { icon: '⏱', label: 'Sync',     help: 'Offset de frames entre WEB-DL y Blu-ray + comprobación de L5 (active area / letterbox)' },
  comparisons:   { icon: '🔬', label: 'Verif.',   help: 'Primera sub-columna de Comparisons: tipo de verificación (HDR COMP, plot, nits, sample, shots…)' },
  comparisons_2: { icon: '📊', label: 'Verif. 2', help: 'Segunda sub-columna de Comparisons (suele ser plot, L1, nits…)' },
  notes:         { icon: '📝', label: 'Notas',    help: 'Notas / workflow. Factible suele ser "workflow 2-3"; si no, explica el motivo' },
};

// Fila de tabla key-value — icono + label (columna fija) + valor (flex) + link opcional.
// Uniforme para todos los campos de factibilidad: Fuente, Sync, Verif, Notas.
function _cmv40TableRow(key, value, link, opts = {}) {
  if (!value && !link) return '';
  const m = CMV40_CHIP_META[key] || { icon: '·', label: key, help: '' };
  const valueClass = opts.mono ? 'cmv40-rec-row-value mono' : 'cmv40-rec-row-value';
  const linkHtml = link
    ? `<a class="cmv40-rec-row-link" href="${escHtml(link)}" target="_blank" rel="noreferrer noopener"
         data-tooltip="Abrir: ${escHtml(link)}">Abrir ↗</a>`
    : '';
  return `
    <div class="cmv40-rec-row">
      <div class="cmv40-rec-row-label" data-tooltip="${escHtml(m.help)}">
        <span class="cmv40-rec-row-icon">${m.icon}</span>
        <span>${escHtml(m.label)}</span>
      </div>
      <div class="${valueClass}">${escHtml(value || '—')}</div>
      ${linkHtml}
    </div>`;
}

function _cmv40RenderRecommendation(data, containerId) {
  const banner = document.getElementById(containerId || 'cmv40-recommendation-banner');
  if (!banner) return;
  banner.style.display = 'block';
  const status = data.status || 'unknown';
  const cls = status === 'recommended' ? 'ok'
            : status === 'not_feasible' ? 'ko'
            : 'unknown';
  const icon = status === 'recommended' ? '✅'
             : status === 'not_feasible' ? '❌'
             : '❓';
  const statusLabel = status === 'recommended' ? 'Factible'
                    : status === 'not_feasible' ? 'No factible'
                    : 'Sin datos';
  banner.className = 'cmv40-rec-banner ' + cls;

  const matchTitleHtml = data.match_title
    ? (data.title_link
        ? `<a class="cmv40-rec-match-title linked" href="${escHtml(data.title_link)}" target="_blank" rel="noreferrer noopener" data-tooltip="Abrir: ${escHtml(data.title_link)}">${escHtml(data.match_title)} <span class="chip-arrow">↗</span></a>`
        : `<span class="cmv40-rec-match-title">${escHtml(data.match_title)}</span>`)
    : '';

  // Meta compacta (match% · vía TMDb) empotrada en el header para no añadir otra fila
  let metaHtml = '';
  if (data.match_confidence && data.match_confidence > 0) {
    const pct = Math.round(data.match_confidence * 100);
    const viaLabel = data.match_source === 'tmdb' ? 'TMDb' : data.match_source;
    metaHtml = `<div class="cmv40-rec-meta">
      <span class="cmv40-rec-meta-tag" data-tooltip="Similitud entre el título del fichero y la fila de DoviTools">${pct}% match</span>
      <span class="cmv40-rec-meta-tag" data-tooltip="Fuente del matching: TMDb traduce ES→EN">vía ${escHtml(viaLabel)}</span>
    </div>`;
  }

  // Header compacto en una sola línea: icono + estado + separador + match title + meta
  let html = `
    <div class="cmv40-rec-top">
      <span class="cmv40-rec-status-badge ${cls}">
        <span class="cmv40-rec-icon">${icon}</span>
        <span class="cmv40-rec-status-label">${statusLabel}</span>
      </span>
      ${matchTitleHtml ? `<span class="cmv40-rec-match-sep">·</span>${matchTitleHtml}` : ''}
      ${metaHtml}
    </div>`;

  if (status === 'recommended' || status === 'not_feasible') {
    const notesKey = status === 'not_feasible' ? 'notes_motivo' : 'notes';
    // Etiqueta dinámica para "motivo" en caso no_feasible — reusa meta de 'notes'
    if (!CMV40_CHIP_META.notes_motivo) {
      CMV40_CHIP_META.notes_motivo = { ...CMV40_CHIP_META.notes, label: 'Motivo' };
    }
    const rows = [
      _cmv40TableRow('dv_source',     data.dv_source,     data.dv_source_link),
      _cmv40TableRow('sync',          data.sync_offset,   data.sync_link),
      _cmv40TableRow('comparisons',   data.comparisons,   data.comparisons_link),
      _cmv40TableRow('comparisons_2', data.comparisons_2, data.comparisons_2_link),
      _cmv40TableRow(notesKey,        data.notes,         data.notes_link),
    ].filter(Boolean);
    if (rows.length) {
      html += `<div class="cmv40-rec-table">${rows.join('')}</div>`;
    }
  } else {
    html += `<div class="cmv40-rec-body">
      El título <strong>${escHtml(data.input_title || '')}</strong>${data.input_year ? ' (' + data.input_year + ')' : ''}`;
    if (data.title_en && data.title_en !== data.input_title) {
      html += ` (TMDb: <em>${escHtml(data.title_en)}</em>)`;
    }
    html += ` no aparece en la hoja de DoviTools (${data.sheet_rows_loaded || 0} títulos revisados). Puedes continuar bajo tu propio criterio.`;
    html += `</div>`;
    if (!data.tmdb_configured) {
      html += `<div class="cmv40-rec-footer">⚠️ Clave de la API de TMDb no configurada — el matching ES→EN es más limitado. Añádela en ⚙︎ Configuración.</div>`;
    }
  }

  // Warning: cuando no tenemos hyperlinks (fuentes xlsx/api/html → ok; csv/disk → sin links)
  const linksOk = ['xlsx', 'api', 'html'].includes(data.sheet_source);
  if (!linksOk && data.sheet_source && data.sheet_source !== 'none') {
    const reason = data.sheets_api_error ||
      'no se pudo leer el sheet vía HTML ni Sheets API';
    html += `<div class="cmv40-rec-warn">
      ⚠️ Los enlaces incrustados en el sheet no están disponibles (fuente actual: <code>${escHtml(data.sheet_source)}</code>).<br>
      <span class="cmv40-rec-warn-detail">${escHtml(reason)}</span>
    </div>`;
  }

  banner.innerHTML = html;
}

function _cmv40NewSwitchTargetTab(tab) {
  _cmv40NewTargetTab = tab;
  ['repo', 'path', 'mkv'].forEach(t => {
    const pane = document.getElementById(`cmv40-new-target-${t}`);
    const btn  = document.getElementById(`cmv40-new-tab-btn-${t}`);
    if (pane) pane.style.display = tab === t ? '' : 'none';
    if (btn)  btn.classList.toggle('active', tab === t);
  });
  _cmv40NewTargetSelected = null;
  _cmv40NewUpdateCreateBtn();
  // Reset del preview al cambiar de tab — sin esto, el HTML del tab previo
  // (p.ej. el preview "Trusted CMv4.0" de un candidato de repo) queda
  // visible al pasar a path/mkv hasta que el usuario haga otra acción.
  _cmv40NewUpdatePipelinePreview();
  if (tab === 'mkv')  _cmv40NewLoadTargetMkvs();
  if (tab === 'path') _cmv40NewLoadRpus();
  if (tab === 'repo' && _cmv40SourceSelected) _cmv40NewLoadRepoCandidates();
}

async function _cmv40NewLoadRpus() {
  const select = document.getElementById('cmv40-new-rpu-select');
  select.innerHTML = '<option value="">— Cargando… —</option>';
  const data = await apiFetch('/api/cmv40/rpu-files');
  select.innerHTML = '<option value="">— Seleccionar RPU —</option>';
  if (data?.files?.length) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = `${f.name} (${_fmtBytes(f.size_bytes)})`;
      select.appendChild(opt);
    });
  } else {
    select.innerHTML = '<option value="">— No hay RPUs en /mnt/cmv40_rpus —</option>';
  }
}

async function _cmv40NewLoadTargetMkvs() {
  const select = document.getElementById('cmv40-new-target-mkv-select');
  select.innerHTML = '<option value="">— Cargando… —</option>';
  const data = await apiFetch('/api/mkv/files-in-isos');
  select.innerHTML = '<option value="">— Seleccionar MKV con CMv4.0 —</option>';
  if (data?.files?.length) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = f.name;
      select.appendChild(opt);
    });
  } else {
    select.innerHTML = '<option value="">— No hay MKVs en el directorio de ISOs —</option>';
  }
}

function onCMv40TargetChange() {
  // Repo: _cmv40NewTargetSelected se mantiene gracias al card-picker
  // (_cmv40NewSelectRepoCandidate). path y mkv siguen usando <select>.
  if (_cmv40NewTargetTab === 'repo') {
    // No hacemos nada aquí; el picker ya llamó a _cmv40NewSelectRepoCandidate.
    _cmv40NewUpdateCreateBtn();
    _cmv40NewUpdatePipelinePreview();
    return;
  }
  const idMap = {
    path: 'cmv40-new-rpu-select',
    mkv:  'cmv40-new-target-mkv-select',
  };
  const id = idMap[_cmv40NewTargetTab];
  const select = document.getElementById(id);
  const val = select ? select.value : '';
  if (!val) {
    _cmv40NewTargetSelected = null;
  } else {
    _cmv40NewTargetSelected = { kind: _cmv40NewTargetTab, value: val };
  }
  _cmv40NewUpdateCreateBtn();
  _cmv40NewUpdatePipelinePreview();
}

/** Calcula el ETA total del pipeline para un tipo de target dado, usando las
 *  constantes calibradas de CMV40_ETA. Se deriva dinámicamente para que
 *  cualquier recalibración de ratios se refleje automáticamente en el modal
 *  sin tocar strings hardcoded. */
function _cmv40ComputeTargetTypeETA(targetType) {
  const anchor = CMV40_ETA.ffmpeg_wall_fallback_s;  // 180s típico UHD BD
  // Partes comunes
  const etaA = anchor + anchor * CMV40_ETA.r_extract_rpu;   // ffmpeg + extract-rpu
  const etaB = 30;                                           // drive download
  const etaH = anchor * CMV40_ETA.r_extract_rpu + 5;         // extract-rpu pre-mux + info
  let etaC, etaF, etaG, etaDE;
  etaDE = 0;  // drop-in trusted salta D y E
  switch (targetType) {
    case 'trusted_p7_fel_final':
      etaC = 0;                                  // sin demux, sin per-frame
      etaF = anchor * CMV40_ETA.r_inject;        // inject sobre source.hevc
      etaG = anchor * CMV40_ETA.r_mux;           // mkvmerge 42 GB dual-layer
      break;
    case 'trusted_p7_mel_final':
      etaC = anchor * CMV40_ETA.r_demux;         // demux solo BL
      etaF = anchor * CMV40_ETA.r_inject;        // inject en BL
      etaG = 30;                                 // mkvmerge single-layer rápido
      break;
    case 'trusted_p8_source':
      etaC = anchor * CMV40_ETA.r_demux;         // demux BL+EL
      etaF = anchor * CMV40_ETA.r_inject;        // merge + inject
      etaG = anchor * CMV40_ETA.r_mux;           // mkvmerge dual-layer
      break;
    default:
      return { tiempo: 'Variable · depende de revisión manual', totalSecs: null };
  }
  const total = etaA + etaB + etaC + etaDE + etaF + etaG + etaH;
  const mins = total / 60;
  const lo = Math.max(1, Math.floor(mins * 0.9));
  const hi = Math.ceil(mins * 1.15);
  return { tiempo: `~${lo}-${hi} min`, totalSecs: total };
}

// Panel explicativo del pipeline que se ejecutará según el tipo de target
// Estructura: cada fase es un pill en el flujo visual. state: 'run' | 'skip'.
// mod: etiqueta opcional bajo el pill (ej. "sin demux"). autoEndsAt: fase tras
// la cual el auto-pipeline se detiene (null = corre hasta H).
// El campo `tiempo` se calcula dinámicamente — ver _cmv40PipelinePreviewHTML.
// IMPORTANTE: el preview se muestra ANTES de Fase A (no conocemos aun el
// profile del source — puede ser P7 FEL, P7 MEL, o P8.1 venido de un MEL
// convertido). Los blurbs cubren las 3 posibilidades para no ser engañosos.
// Las phases pills muestran el caso "trusted-fast-path": cuando los gates
// pasan y el source coincide en estructura con el bin, el flujo es el
// optimo descrito; en las otras combinaciones Fase F hace merge en lugar
// de drop-in (ver matriz completa en cmv40_pipeline.py _execute_fase_f).
const _CMV40_PIPELINE_PREVIEW = {
  trusted_p7_fel_final: {
    icon: '🎯',
    title: 'Bin P7 FEL · CMv4.0 ya cocinado',
    blurb: 'Bin con BL+EL+RPU CMv4.0 listo para drop-in. ' +
           'Comportamiento según tu BD: ' +
           '· P7 FEL → drop-in directo (sin demux, sin merge — máxima velocidad, preserva BL+EL). ' +
           '· P7 MEL → merge de los levels CMv4.0 en el RPU del source, descarta EL del source → P8.1 CMv4.0. ' +
           '· P8.1 (MEL convertido) → merge de los levels CMv4.0 en el RPU P8 del source → P8.1 CMv4.0.',
    cls: 'ok',
    autoEndsAt: null,
    phases: [
      { k: 'A', label: 'Analizar BD',    state: 'run' },
      { k: 'B', label: 'Descargar bin',  state: 'run' },
      { k: 'C', label: 'Demux',          state: 'skip', mod: 'si BD es FEL' },
      { k: 'D', label: 'Verif. visual',  state: 'skip', mod: 'gates trusted' },
      { k: 'E', label: 'Corrección sync', state: 'skip', mod: 'Δ=0 por gates' },
      { k: 'F', label: 'Inyectar',       state: 'run',  mod: 'drop-in o merge' },
      { k: 'G', label: 'Remux MKV',      state: 'run' },
      { k: 'H', label: 'Validar',        state: 'run' },
    ],
  },
  trusted_p7_mel_final: {
    icon: '🎯',
    title: 'Bin P7 MEL · CMv4.0 ya cocinado',
    blurb: 'Bin con BL+EL(MEL)+RPU CMv4.0 listo. El EL del bin (MEL) no aporta calidad, ' +
           'siempre se descarta. Comportamiento según tu BD: ' +
           '· P7 MEL → inyección directa del RPU del bin sobre la BL del source → P8.1 CMv4.0. ' +
           '· P7 FEL → merge de los levels CMv4.0 en el RPU del source, preservando FEL → P7 FEL CMv4.0. ' +
           '· P8.1 (MEL convertido) → merge en el RPU P8 del source → P8.1 CMv4.0.',
    cls: 'ok',
    autoEndsAt: null,
    phases: [
      { k: 'A', label: 'Analizar BD',    state: 'run' },
      { k: 'B', label: 'Descargar bin',  state: 'run' },
      { k: 'C', label: 'Demux',          state: 'run', mod: 'según BD' },
      { k: 'D', label: 'Verif. visual',  state: 'skip', mod: 'gates trusted' },
      { k: 'E', label: 'Corrección sync', state: 'skip', mod: 'Δ=0 por gates' },
      { k: 'F', label: 'Inyectar',       state: 'run',  mod: 'directo o merge' },
      { k: 'G', label: 'Remux MKV',      state: 'run' },
      { k: 'H', label: 'Validar',        state: 'run' },
    ],
  },
  trusted_p8_source: {
    icon: '📦',
    title: 'Bin P8 retail · CMv4.0 completo',
    blurb: 'Bin P8 con CMv4.0 completo (L8 trims + L9/L10/L11). Sirve como donante ' +
           'de metadata CMv4.0 vía dovi_tool editor (allow_cmv4_transfer). ' +
           'Comportamiento según tu BD: ' +
           '· P7 FEL → merge de los levels CMv4.0 en el RPU del source preservando FEL → P7 FEL CMv4.0. ' +
           '· P7 MEL → descarta EL e inyecta el RPU del bin directamente en BL → P8.1 CMv4.0. ' +
           '· P8.1 (MEL convertido) → inyección directa (mismo profile, sin merge) → P8.1 CMv4.0 refinado.',
    cls: 'info',
    autoEndsAt: null,
    phases: [
      { k: 'A', label: 'Analizar BD',    state: 'run' },
      { k: 'B', label: 'Descargar bin',  state: 'run' },
      { k: 'C', label: 'Demux',          state: 'run', mod: 'según BD' },
      { k: 'D', label: 'Verif. visual',  state: 'skip', mod: 'gates trusted' },
      { k: 'E', label: 'Corrección sync', state: 'skip', mod: 'Δ=0 por gates' },
      { k: 'F', label: 'Merge + inyectar', state: 'run' },
      { k: 'G', label: 'Remux MKV',      state: 'run' },
      { k: 'H', label: 'Validar',        state: 'run' },
    ],
  },
  unknown: {
    icon: '❓',
    title: 'Tipo por clasificar',
    blurb: 'La clasificación real se hará en Fase B tras descargar el bin. Si los trust gates ' +
           '(frames + L5 + CM v4.0 + has_l8) pasan → flujo automático trusted. Si no → pausa en ' +
           'Fase D para revisión visual de la sincronización antes de inyectar.',
    cls: 'warn',
    autoEndsAt: 'D',
    phases: [
      { k: 'A', label: 'Analizar BD',    state: 'run' },
      { k: 'B', label: 'Clasificar bin', state: 'run' },
      { k: 'C', label: 'Demux',          state: 'run', mod: 'probable' },
      { k: 'D', label: 'Verif. visual',  state: 'run', mod: 'si no trusted' },
      { k: 'E', label: 'Corrección sync', state: 'run', mod: 'si Δ≠0' },
      { k: 'F', label: 'Inyectar',       state: 'run' },
      { k: 'G', label: 'Remux MKV',      state: 'run' },
      { k: 'H', label: 'Validar',        state: 'run' },
    ],
  },
};

// Renderer compartido del preview del pipeline — se usa en el modal "Nuevo
// proyecto" y también en cada candidato de la consulta rápida (🔎).
// `provenance`: 'retail' | 'generated' | '' — añade aviso UX.
// `retailAlternative`: si provenance=generated, nombre del bin retail
// disponible en la misma lista (refuerza el aviso).
function _cmv40PipelinePreviewHTML(info, provenance, retailAlternative, targetType) {
  if (!info) return '';
  const cls = info.cls || 'warn';
  const flow = info.phases.map((p, i) => {
    const modHtml = p.mod ? `<span class="cmv40-ph-mod">${escHtml(p.mod)}</span>` : '';
    const arrow = (i < info.phases.length - 1)
      ? `<span class="cmv40-ph-arrow" aria-hidden="true">→</span>`
      : '';
    return `
      <div class="cmv40-ph-pill cmv40-ph-${p.state}" data-tooltip="Fase ${p.k}: ${escHtml(p.label)}${p.mod ? ' · ' + escHtml(p.mod) : ''}">
        <span class="cmv40-ph-letter">${p.k}</span>
        <span class="cmv40-ph-label">${escHtml(p.label)}</span>
        ${modHtml}
      </div>${arrow}`;
  }).join('');
  // ETA dinámico: se calcula a partir de CMV40_ETA constants (calibradas con
  // mediciones reales). Se actualiza automáticamente cuando se recalibran
  // los ratios sin tocar strings hardcoded.
  const tiempo = targetType
    ? _cmv40ComputeTargetTypeETA(targetType).tiempo
    : (info.tiempo || 'Variable');
  const provHtml = _cmv40ProvenanceNoteHTML(provenance, retailAlternative);
  return `
    <div class="cmv40-pipeline-preview ${cls}">
      <div class="cmv40-pp-header">
        <span class="cmv40-pp-icon">${info.icon}</span>
        <span class="cmv40-pp-title">${escHtml(info.title)}</span>
        <span class="cmv40-pp-time" data-tooltip="Estimación basada en tiempos medidos en NAS ZFS — se recalibra con cada ejecución real">⏱ ${escHtml(tiempo)}</span>
      </div>
      <div class="cmv40-pp-flow">${flow}</div>
      <div class="cmv40-pp-blurb">${escHtml(info.blurb)}</div>
      ${provHtml}
    </div>`;
}

// Nota de procedencia del CMv4.0. Verde para retail, ámbar para generated;
// si además hay alternativa retail para el mismo título, el aviso se
// refuerza con el nombre del bin retail disponible.
function _cmv40ProvenanceNoteHTML(prov, retailAlternative) {
  if (prov === 'retail') {
    return `
      <div class="cmv40-pp-prov cmv40-pp-prov-retail">
        <span class="cmv40-pp-prov-icon">🏛</span>
        <span class="cmv40-pp-prov-label">Retail</span>
        <span class="cmv40-pp-prov-body">RPU extraído de master streaming oficial — creative intent del colorista.</span>
      </div>`;
  }
  if (prov === 'generated') {
    const altHtml = retailAlternative
      ? `<div class="cmv40-pp-prov-alt">
           <strong>Alternativa retail disponible en este repo:</strong><br>
           <code>${escHtml(retailAlternative)}</code><br>
           <em>Cámbiala en el desplegable de arriba para usar CMv4.0 auténtico.</em>
         </div>`
      : '';
    return `
      <div class="cmv40-pp-prov cmv40-pp-prov-gen">
        <span class="cmv40-pp-prov-icon">⚠️</span>
        <span class="cmv40-pp-prov-label">Generated</span>
        <span class="cmv40-pp-prov-body">CMv4.0 <strong>sintético</strong> desde HDR10 (algorítmico). La calidad depende del tuning (T1/T3…). Si existe un bin <code>(cmv4.0 restored/added)</code> o <code>(P5 to P8)</code> para este título, es preferible.</span>
        ${altHtml}
      </div>`;
  }
  return '';
}

function _cmv40NewUpdatePipelinePreview() {
  const container = document.getElementById('cmv40-new-pipeline-preview');
  if (!container) return;

  // Sin selección de target → vacío para los 3 tabs.
  if (!_cmv40NewTargetSelected) {
    container.innerHTML = '';
    container.style.display = 'none';
    _cmv40NewUpdateAutoLabel(null);
    return;
  }

  const tab = _cmv40NewTargetTab;

  // Tabs 'path' y 'mkv': sin sheet de recomendación no podemos predecir el
  // provenance/predicted_type antes de descargar/extraer el bin. Mostramos
  // un placeholder informativo para que el usuario sepa que la validación
  // completa pasa por el pre-flight (idéntica a la del tab 'repo').
  if (tab === 'path' || tab === 'mkv') {
    const sourceLabel = tab === 'path' ? 'el bin local' : 'el MKV target';
    container.style.display = 'block';
    container.innerHTML = `
      <div class="cmv40-pp-card" style="background:var(--blue-dim); border:1px solid var(--blue-border); border-radius:8px; padding:10px 12px">
        <div style="font-size:12px; color:var(--text-1); line-height:1.5">
          <strong style="color:var(--blue)">ℹ El tipo del bin se detectará en el pre-flight.</strong>
          Sin el sheet de recomendación de DoviTools no podemos predecir
          la calidad de ${sourceLabel} antes de procesarlo. El pre-flight (5-30s
          para .bin local, 30-90s para extraer de MKV) clasifica el L8
          (real / sintético / ambiguo), calcula el tier de calidad
          (CMv4 CORE/CORE+/FULL) y decide la recomendación
          Mantener vs Inyectar — igual que con un bin del repo.
          Verás el veredicto en la card "🎯 Análisis y recomendación"
          del proyecto.
        </div>
      </div>`;
    // Label del auto-pipeline neutro: no sabemos si será trusted/generic
    // hasta que el pre-flight clasifique. El usuario verá el detalle real
    // tras crear el proyecto.
    _cmv40NewUpdateAutoLabel(null);
    return;
  }

  // Tab 'repo': preview clásico con datos del sheet.
  if (tab !== 'repo' || _cmv40NewTargetSelected.kind !== 'repo') {
    container.innerHTML = '';
    container.style.display = 'none';
    _cmv40NewUpdateAutoLabel(null);
    return;
  }
  const pt = _cmv40NewTargetSelected.predicted_type || 'unknown';
  const prov = _cmv40NewTargetSelected.provenance || '';
  const info = _CMV40_PIPELINE_PREVIEW[pt] || _CMV40_PIPELINE_PREVIEW.unknown;

  // Si el usuario eligió un Generated, comprobamos si en la lista cargada
  // hay al menos una opción Retail (CMv4.0 auténtico). En ese caso, el warning
  // se refuerza — hay alternativa preferible accesible en la misma vista.
  let retailAlternative = '';
  if (prov === 'generated' && Array.isArray(_cmv40NewRepoCands)) {
    const alt = _cmv40NewRepoCands.find(c => c.provenance === 'retail');
    if (alt) retailAlternative = alt.file?.name || '(retail disponible)';
  }

  container.style.display = 'block';
  container.innerHTML = _cmv40PipelinePreviewHTML(info, prov, retailAlternative, pt);
  _cmv40NewUpdateAutoLabel(info);
}

// Actualiza el texto del toggle "Auto-pipeline" según el preview activo.
// - Trusted: corre todo A→H automáticamente.
// - Unknown/generic: se detiene en D si los gates no pasan.
function _cmv40NewUpdateAutoLabel(info) {
  const span = document.querySelector('.cmv40-new-auto-toggle span');
  const wrap = document.querySelector('.cmv40-new-auto-toggle');
  if (!span) return;
  if (!info) {
    span.textContent = '🤖 Auto-pipeline';
    if (wrap) wrap.setAttribute('data-tooltip',
      'Encadena las fases disponibles sin interacción manual.');
    return;
  }
  const runPhases = info.phases.filter(p => p.state === 'run').map(p => p.k);
  const endsAt = info.autoEndsAt;
  if (endsAt) {
    span.textContent = `🤖 Auto-pipeline hasta Fase ${endsAt} (pausa si no trusted)`;
    if (wrap) wrap.setAttribute('data-tooltip',
      `Corre hasta la Fase ${endsAt}. Si los gates no pasan en B, espera revisión manual.`);
  } else {
    span.textContent = `🤖 Auto-pipeline completo (${runPhases.join('→')})`;
    if (wrap) wrap.setAttribute('data-tooltip',
      `Ejecuta ${runPhases.length} fases automáticamente. Estimado: ${info.tiempo}.`);
  }
}

// Cache de los candidatos cargados (para que _cmv40NewSelectRepoCandidate
// pueda recuperar el objeto completo por file_id al hacer click en una card).
let _cmv40NewRepoCands = [];

function _cmv40NewResetRepoList(placeholder, isError = false) {
  const list = document.getElementById('cmv40-new-repo-list');
  if (!list) return;
  _cmv40NewRepoCands = [];
  list.innerHTML = `<div class="cmv40-repo-empty ${isError ? 'error' : ''}">${placeholder}</div>`;
  // Al resetear también limpia la selección del target
  if (_cmv40NewTargetSelected?.kind === 'repo') {
    _cmv40NewTargetSelected = null;
    _cmv40NewUpdateCreateBtn();
    _cmv40NewUpdatePipelinePreview();
  }
}

// Token anti-race: si el usuario cambia de tab o de source durante el await,
// la respuesta vieja NO debe auto-seleccionar un repo candidate (lo que
// sobreescribe el target que el usuario haya elegido mientras tanto).
let _cmv40RepoReqId = 0;

async function _cmv40NewLoadRepoCandidates(forceRefresh = false) {
  const list = document.getElementById('cmv40-new-repo-list');
  const info = document.getElementById('cmv40-new-repo-info');
  if (!list) return;
  if (!_cmv40SourceSelected) {
    _cmv40NewResetRepoList('— Selecciona primero el MKV origen —');
    if (info) info.textContent = 'Selecciona primero un MKV origen.';
    return;
  }
  list.innerHTML = '<div class="cmv40-repo-empty">⏳ Buscando en Drive…</div>';
  if (info) info.innerHTML = '<span class="cmv40-rec-spinner-inline"></span> Consultando repositorio de DoviTools…';
  // El sheet de DoviTools matchea por NOMBRE de fichero (no path), asi que
  // pasamos el filename, no la ruta absoluta.
  const matchKey = _cmv40SourceFilename || _cmv40SourceSelected;
  const qs = '?filename=' + encodeURIComponent(matchKey);
  const myReqId = ++_cmv40RepoReqId;
  const mySource = _cmv40SourceSelected;
  const data = await apiFetch('/api/cmv40/repo-rpus' + qs);
  // Stale response: el usuario ya lanzó otra carga, cambió de tab o de
  // source — descartamos silenciosamente para no sobreescribir su selección.
  if (myReqId !== _cmv40RepoReqId || mySource !== _cmv40SourceSelected
      || _cmv40NewTargetTab !== 'repo') {
    return;
  }
  if (!data) {
    _cmv40NewResetRepoList('Error consultando el repositorio', true);
    return;
  }
  if (!data.drive_configured) {
    _cmv40NewRepoCands = [];
    list.innerHTML = `<div class="cmv40-repo-banner-wrap">${_cmv40RepoUnavailableBanner(data)}</div>`;
    if (info) {
      info.textContent = !data.drive_folder_configured
        ? 'Repo bloqueado — configura la URL.'
        : 'Google API key no configurada.';
    }
    return;
  }
  if (data.error) {
    _cmv40NewResetRepoList(data.error, true);
    if (info) info.textContent = data.error;
    return;
  }
  const cands = data.candidates || [];
  if (!cands.length) {
    const t = data.title_en || data.title_es || '?';
    _cmv40NewResetRepoList(`Sin coincidencias para "${escHtml(t)}"`);
    if (info) {
      info.innerHTML = `No hay <code>.bin</code> para <strong>${escHtml(t)}</strong> en el repositorio. Prueba otra pestaña.`;
    }
    return;
  }

  // Lista plana ordenada por score (el backend ya aplicó +0.03 a retail,
  // así que el orden viene correcto). Quitamos la agrupación visual
  // porque confunde: un P5→P8 source (provenance='') puede ser mejor
  // que un Generated FEL aunque "Sin marca" suena peor que "Generated".
  _cmv40NewRepoCands = cands;
  const topFilename = cands[0]?.file?.name || '';

  const renderCard = (c) => {
    const sizeMb = (c.file.size_bytes / 1024 / 1024).toFixed(1);
    const pt = c.predicted_type || 'unknown';
    const prov = c.provenance || '';
    const tagMeta = pt === 'trusted_p7_fel_final' ? { icon: '🎯', label: 'bin P7 FEL',  cls: 'tag-ok' }
                  : pt === 'trusted_p7_mel_final' ? { icon: '🎯', label: 'bin P7 MEL',  cls: 'tag-ok' }
                  : pt === 'trusted_p8_source'    ? { icon: '📦', label: 'bin P8 retail', cls: 'tag-info' }
                  : { icon: '❓', label: 'tipo desconocido', cls: 'tag-warn' };
    const provTag = prov === 'retail'
      ? '<span class="cmv40-repo-card-tag tag-ok">🏛 Retail</span>'
      : prov === 'generated'
      ? '<span class="cmv40-repo-card-tag tag-warn">⚠ Generated</span>'
      : '';
    const isBest = c.file.name === topFilename;
    return `
      <div class="cmv40-repo-card" data-file-id="${escHtml(c.file.id)}"
           role="button" tabindex="0"
           onclick="_cmv40NewSelectRepoCandidate('${escHtml(c.file.id)}')"
           onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();_cmv40NewSelectRepoCandidate('${escHtml(c.file.id)}')}">
        <div class="cmv40-repo-card-head">
          <span class="cmv40-repo-card-tag ${tagMeta.cls}">${tagMeta.icon} ${tagMeta.label}</span>
          ${provTag}
          ${isBest ? '<span class="cmv40-repo-card-best">🏆 mejor match</span>' : ''}
          <span class="cmv40-repo-card-score">${Math.round(c.score * 100)}%</span>
          <span class="cmv40-repo-card-size">${sizeMb} MB</span>
        </div>
        <div class="cmv40-repo-card-path">${escHtml(c.file.path)}</div>
      </div>`;
  };

  list.innerHTML = cands.map(renderCard).join('');

  // Auto-seleccionar el top-score global
  if (topFilename) {
    const top = cands.find(c => c.file.name === topFilename);
    if (top) _cmv40NewSelectRepoCandidate(top.file.id);
  }

  if (info) {
    info.innerHTML = `<strong>${cands.length}</strong> candidato${cands.length !== 1 ? 's' : ''} · top score: <strong>${Math.round(cands[0].score * 100)}%</strong>. Haz click para seleccionar. Se descargará al crear el proyecto.`;
  }
}

// Marca visualmente una card como seleccionada y actualiza el estado global.
function _cmv40NewSelectRepoCandidate(fileId) {
  // Guard anti-race: si el usuario cambió de tab, no pisamos su target.
  // (La carga async de repo candidates podía completar tras un cambio de
  //  tab y reemplazar _cmv40NewTargetSelected con el top-candidate.)
  if (_cmv40NewTargetTab !== 'repo') return;
  const list = document.getElementById('cmv40-new-repo-list');
  if (!list) return;
  const card = list.querySelector(`.cmv40-repo-card[data-file-id="${fileId}"]`);
  if (!card) return;
  // Quita selected de todas las cards, marca la actual
  list.querySelectorAll('.cmv40-repo-card.selected').forEach(el => el.classList.remove('selected'));
  card.classList.add('selected');
  // Scroll dentro del contenedor para que la card elegida sea visible
  try {
    card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  } catch (e) { /* ignore */ }

  const cand = _cmv40NewRepoCands.find(c => c.file.id === fileId);
  if (!cand) return;
  _cmv40NewTargetSelected = {
    kind: 'repo',
    value: { file_id: cand.file.id, file_name: cand.file.name },
    predicted_type: cand.predicted_type || 'unknown',
    provenance: cand.provenance || '',
  };
  _cmv40NewUpdateCreateBtn();
  _cmv40NewUpdatePipelinePreview();
}

function _cmv40EscHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s || '');
  return d.innerHTML;
}

function _cmv40NewUpdateCreateBtn() {
  const btn = document.getElementById('cmv40-create-btn');
  if (!btn) return;
  btn.disabled = !_cmv40SourceSelected || !_cmv40NewTargetSelected;
}

async function createCMv40Project() {
  if (!_cmv40SourceSelected || !_cmv40NewTargetSelected) return;
  const autoOn = !!document.getElementById('cmv40-new-auto')?.checked;
  const target = _cmv40NewTargetSelected;

  // _cmv40SourceSelected es ya la ruta absoluta tras el browser (puede venir
  // de /mnt/library/Movies/...). Si por compat fuera solo un filename (caso
  // legacy si alguien lo seteara directo), prepend /mnt/output como antes.
  const sourcePath = _cmv40SourceSelected.startsWith('/')
    ? _cmv40SourceSelected
    : '/mnt/output/' + _cmv40SourceSelected;

  // Construir pending_target para que el backend lo persista. Crítico para
  // que el orquestador pueda disparar preflight + Fase B aunque el cliente
  // desaparezca tras Fase A (Mac sleep, pestaña cerrada, etc).
  const pendingTargetPayload = { kind: target.kind };
  if (target.kind === 'repo') {
    pendingTargetPayload.file_id = target.value?.file_id || '';
    pendingTargetPayload.file_name = target.value?.file_name || '';
  } else if (target.kind === 'path') {
    pendingTargetPayload.rpu_path = target.value || '';
  } else if (target.kind === 'mkv') {
    pendingTargetPayload.source_mkv_path = target.value || '';
  }

  const data = await apiFetch('/api/cmv40/create', {
    method: 'POST',
    body: JSON.stringify({
      source_mkv_path: sourcePath,
      // CRÍTICO: auto_pipeline le dice al backend que encadene fases
      // automáticamente sin esperar al frontend. Hace el job resiliente
      // a Mac sleep, pestaña cerrada, navegador crashado, etc.
      auto_pipeline: autoOn,
      pending_target: pendingTargetPayload,
    }),
  });

  closeModal('cmv40-new-modal');
  if (!data) {
    showToast('Error al crear el proyecto', 'error');
    return;
  }

  // Abrir el proyecto y preconfigurar auto + target pendiente
  const project = openCMv40Project(data);
  if (project) {
    project.autoContinue = autoOn;
    project.pendingTarget = target;  // se aplicará cuando A termine
    _updateCMv40Panel(project);
  }
  await refreshCMv40Sidebar();

  // Disparar preflight INMEDIATAMENTE si hay target seleccionado, sin importar
  // si auto mode esta on. Sin auto, esto evita que el usuario gaste 12 min de
  // Fase A si el bin target no aporta CMv4.0 — el preflight tarda <5s y aborta
  // con mensaje claro. Con auto, _cmv40MaybeAutoAdvance se encarga del flujo
  // completo (preflight → Fase A → ...).
  if (target && project) {
    if (autoOn) {
      project._autoChaining = true;
      _cmv40MaybeAutoAdvance(project);
    } else {
      // Auto OFF: solo el preflight, no encadena Fase A. El usuario lanza
      // Fase A manualmente cuando vea preflight OK.
      _cmv40FirePreflight(project.id, target);
    }
  }
}

/**
 * Dispara el pre-flight del bin target en background. Backend responde
 * inmediatamente con {started:true} y setea running_phase="preflight". El
 * polling se encarga del resto: si OK → target_preflight_ok=True y el
 * próximo tick dispara Fase A. Si KO → error_message se setea y el
 * pipeline se detiene (el motivo queda en el log de la sesión via WS,
 * sin toast).
 */
async function _cmv40FirePreflight(pid, target) {
  const body = { kind: target.kind === 'repo' ? 'drive' : target.kind };
  if (target.kind === 'repo') {
    body.file_id = target.value.file_id;
    body.file_name = target.value.file_name || '';
  } else if (target.kind === 'path') {
    body.rpu_path = target.value;
  } else if (target.kind === 'mkv') {
    body.source_mkv_path = target.value;
  }
  await apiFetch(`/api/cmv40/${pid}/preflight-target`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ── Proyecto CMv4.0 ──────────────────────────────────────────────

// Asigna una sesión nueva al proyecto preservando campos que el backend
// puede no haber hidratado aún (típicamente `tmdb_info`). Evita que la
// ficha TMDb desaparezca/flickee cuando hay saves concurrentes (p.ej.
// durante una cancelación de fase que clobberea campos async).
function _cmv40AssignSession(project, data) {
  if (!project || !data) return;
  const preserved = {};
  const PRESERVE_FIELDS = ['tmdb_info'];
  for (const f of PRESERVE_FIELDS) {
    if (project.session && project.session[f] && !data[f]) {
      preserved[f] = project.session[f];
    }
  }
  // running_phase: preservar el optimistic local si hay race condition
  // con el GET. Caso concreto del bug: cuando "✓ Fase X completada" llega
  // por WS, el frontend dispara GET /api/cmv40/{id}. Bajo carga I/O del
  // NAS ese GET puede tardar 30s en responder. Mientras tanto:
  //   T=0:    backend save running_phase=null (fin de fase X)
  //   T=0.01: backend dispatch a siguiente fase Y → save running_phase=Y
  //   T=0.02: WS broadcasta "━━━ Inicio fase: Y ━━━" → optimistic local Y
  //   T=30:   GET de T=0 responde — pero load_cmv40_session leyó el JSON
  //           en la ventana de ~10ms donde running_phase=null y devuelve
  //           ese snapshot obsoleto. Sin este guard, _cmv40AssignSession
  //           pisaba el optimistic Y con un null antiguo → spinner del
  //           timeline desaparecía de la fase nueva hasta el SIGUIENTE
  //           GET (el del "━━━ Inicio fase: Y" propio).
  // Si el local tiene running_phase reciente y data lo trae null pero la
  // sesión no es terminal, conservamos el local.
  if (project.session && project.session.running_phase
      && !data.running_phase
      && data.phase !== 'done'
      && !data.archived
      && !data.error_message) {
    const optimisticAt = project._optimisticRunningPhaseAt || 0;
    if (Date.now() - optimisticAt < 60000) {
      preserved.running_phase = project.session.running_phase;
    }
  }
  project.session = Object.assign({}, data, preserved);
  _cmv40RehydratePendingTarget(project);
}

// Reconstruye `project.pendingTarget` desde `session.pending_target_*` (que
// el backend persiste al crear el proyecto). Crítico para que el frontend
// no se confunda tras un reload (Mac sleep, pestaña cerrada): sin esto, el
// case 'created' de _cmv40MaybeAutoAdvance saltaba el preflight y disparaba
// Fase A directo, y el case 'source_analyzed' dejaba el flujo "pausado"
// cuando el backend ya estaba corriendo Fase B.
//
// Solo hidratamos cuando phase ∈ {created, source_analyzed}: a partir de
// target_provided el target ya está consumido en backend (target_rpu_source).
function _cmv40RehydratePendingTarget(project) {
  const s = project?.session;
  if (!s) return;
  const phase = s.phase;
  if (phase !== 'created' && phase !== 'source_analyzed') {
    project.pendingTarget = null;
    return;
  }
  const kind = s.pending_target_kind;
  if (!kind) return;  // nunca hubo target preseleccionado
  // Idempotente: si ya está hidratado al mismo kind, preservar metadata
  // adicional que el frontend pueda haber añadido (predicted_type, etc).
  if (project.pendingTarget?.kind === kind) return;
  let value = null;
  if (kind === 'path') {
    value = s.pending_target_rpu_path || '';
  } else if (kind === 'mkv') {
    value = s.pending_target_source_mkv_path || '';
  } else if (kind === 'drive' || kind === 'repo') {
    value = {
      file_id: s.pending_target_file_id || '',
      file_name: s.pending_target_file_name || '',
    };
  }
  if (!value) return;
  project.pendingTarget = { kind, value };
}

function openCMv40Project(session) {
  // Si ya está abierto, activar su subtab
  const existing = openCMv40Projects.find(p => p.id === session.id);
  if (existing) {
    switchCMv40SubTab(existing.subTabId);
    return existing;
  }
  if (openCMv40Projects.length >= MAX_CMV40_PROJECTS) {
    showToast(`Máximo ${MAX_CMV40_PROJECTS} proyectos abiertos`, 'warning');
    return null;
  }

  const pid = session.id;
  // resumeAuto: refleja el estado persistente `session.auto_pipeline` del
  // backend. Esta es ahora la FUENTE DE VERDAD del modo auto. El backend
  // encadena fases automáticamente sin depender del frontend (resiliente
  // a Mac sleep / pestaña cerrada / navegador crashado). Frontend solo
  // necesita reflejar el flag para mostrar la UI correcta.
  // Fallback heurístico para sesiones legacy (creadas antes del campo
  // auto_pipeline): si está en mid-pipeline con target trusted o con
  // running_phase, asumimos que estaba en auto.
  const isMidPipeline = session.phase
    && session.phase !== 'done'
    && session.phase !== 'created'
    && !session.error_message
    && !session.archived;
  const wasInAutoFlowLegacy = !!session.running_phase
    || (session.target_trust_ok === true);
  const resumeAuto = (session.auto_pipeline === true)
    || (isMidPipeline && wasInAutoFlowLegacy);
  const project = {
    id: pid,
    subTabId: pid,
    session: session,
    ws: null,
    syncData: null,
    autoContinue: resumeAuto,  // off por defecto; createCMv40Project lo activa explícitamente
    pendingTarget: null,       // { kind: 'path'|'mkv'|'repo', value }
  };
  // Hidrata pendingTarget desde session.pending_target_* si aplica. Necesario
  // para reanudar el auto-pipeline tras reload del cliente sin disparar Fase
  // A sin preflight ni quedarse "pausado" en source_analyzed.
  _cmv40RehydratePendingTarget(project);
  openCMv40Projects.push(project);
  _createCMv40SubTab(project);
  _createCMv40Panel(project);
  switchCMv40SubTab(pid);
  _connectCMv40WebSocket(project);
  // Polling REST de seguridad: independiente del WS, refresca la sesión
  // cada 4s mientras haya running_phase. Garantiza que el log avanza
  // aunque el WS quede zombie tras Mac sleep (caso real visto: tras
  // cerrar tapa del Mac >1min y reabrir, el WS reportaba OPEN pero los
  // datos ya no fluían — el polling los trae via REST y la hidratación
  // con watermark añade las líneas nuevas al DOM sin duplicar).
  _cmv40StartSafetyPoller(project);
  // Validar artefactos en disco — detecta ficheros borrados manualmente
  // y retrocede la fase automáticamente si hace falta.
  _cmv40VerifyArtifacts(project);
  // Si reanudamos auto-pipeline y la sesión NO tiene fase corriendo (modo
  // "puente" tras una fase atascada), disparar _cmv40MaybeAutoAdvance
  // INMEDIATAMENTE en lugar de esperar al primer tick del safety poller
  // (4s). Caso real: el usuario reabre un proyecto que se quedó atascado
  // en phase='extracted' tras perder foco — queremos arrancar la
  // transición a sync_verified en cuanto el panel termine de pintar.
  if (resumeAuto && !session.running_phase) {
    setTimeout(() => {
      if (!project._closed) _cmv40MaybeAutoAdvance(project);
    }, 100);
  }
  return project;
}

/**
 * Polling REST de seguridad cada 4s mientras haya running_phase. Llama
 * _refreshCMv40Session, que actualiza session + panel + log permanente
 * (via watermark, sin duplicados con líneas que llegaron por WS).
 *
 * Watchdog del WS: si el último mensaje WS llegó hace más de 30s pero
 * sigue habiendo running_phase, asumimos zombie y forzamos reconexión.
 * Sin esto, un usuario con la tapa cerrada >5min y la pestaña visible
 * podría tardar 60-120s en ver actualizaciones (timeout del TCP).
 *
 * Se autoapaga cuando running_phase=null o cuando el proyecto se cierra.
 */
function _cmv40StartSafetyPoller(project) {
  if (project._safetyPoller) clearInterval(project._safetyPoller);
  project._lastWsMessageAt = Date.now();
  project._safetyPoller = setInterval(() => {
    if (!project || project._closed) {
      clearInterval(project._safetyPoller);
      project._safetyPoller = null;
      return;
    }
    const s = project.session || {};
    // Condición de salida: el job terminó completamente o entró en error.
    // Antes salíamos cuando running_phase=null, pero eso paraba el poller
    // durante el "puente" entre fases del auto-pipeline (running_phase=null
    // mientras el frontend dispara la siguiente fase). Si el dispatch falla
    // en background tab (Chrome throttle de setTimeout afecta el polling
    // interno), nadie reintenta y la cadena se cuelga.
    // Ahora seguimos activos mientras autoContinue=true y phase no es
    // terminal, para vigilar el modo puente.
    const isTerminal = (s.phase === 'done' || s.archived || !!s.error_message);
    const isActive = !!s.running_phase || project.autoContinue;
    if (isTerminal || !isActive) {
      clearInterval(project._safetyPoller);
      project._safetyPoller = null;
      return;
    }
    // Refresh REST: actualiza session.output_log (entre otros). Internamente
    // dispara _cmv40MaybeAutoAdvance si autoContinue=true y phase no
    // running — con el retry de 5s del flag, esto destraba cadenas
    // atascadas en modo puente.
    if (!document.hidden) {
      _refreshCMv40Session(project.id);
    }
    // Watchdog: detectar zombie WS por silencio prolongado.
    const silentMs = Date.now() - (project._lastWsMessageAt || 0);
    const ws = project.ws;
    const looksOpen = ws && ws.readyState === WebSocket.OPEN;
    if (silentMs > 30000 && looksOpen && !document.hidden && s.running_phase) {
      // Más de 30s sin mensaje pero el WS dice OPEN → zombie probable.
      // Solo aplica si hay running_phase (sino no hay líneas que esperar).
      try { project.ws?.close(); } catch (_) {}
      setTimeout(() => {
        if (!project._closed) _connectCMv40WebSocket(project);
      }, 50);
    }
  }, 4000);
}

async function _cmv40VerifyArtifacts(project) {
  // No validar proyectos recién creados (sin artefactos aún esperados)
  if (project.session.phase === 'created') return;
  const data = await apiFetch(`/api/cmv40/${project.id}/verify-artifacts`, { method: 'POST' });
  if (!data) return;
  if (data.changed) {
    project.session = data.session;
    _updateCMv40Panel(project);
    refreshCMv40Sidebar();
    if (data.all_missing) {
      showToast(`⛔ ${data.message}`, 'error');
      // Con todo borrado, auto-avance queda neutralizado (comprueba error_message)
    } else {
      showToast(`⚠ ${data.message}`, 'warning');
    }
  }
}

function closeCMv40Project(pid) {
  const idx = openCMv40Projects.findIndex(p => p.id === pid);
  if (idx === -1) return;
  const project = openCMv40Projects[idx];
  // Marca para que onclose del WS NO intente reconectar.
  project._closed = true;
  if (project._wsReconnectTimer) {
    clearTimeout(project._wsReconnectTimer);
    project._wsReconnectTimer = null;
  }
  if (project._safetyPoller) {
    clearInterval(project._safetyPoller);
    project._safetyPoller = null;
  }
  try { project.ws?.close(); } catch (_) {}
  document.getElementById(`cmv40-stab-${pid}`)?.remove();
  document.getElementById(`cmv40-panel-${pid}`)?.remove();
  openCMv40Projects.splice(idx, 1);
  _updateSubtabScrollState();

  if (activeCMv40SubTabId === pid) {
    if (openCMv40Projects.length > 0) {
      switchCMv40SubTab(openCMv40Projects[openCMv40Projects.length - 1].subTabId);
    } else {
      activeCMv40SubTabId = null;
      document.getElementById('cmv40-empty-state').style.display = '';
    }
  }
  // Refrescar sidebar para actualizar el badge "abierto"
  _renderCMv40Sidebar();
}

function switchCMv40SubTab(pid) {
  activeCMv40SubTabId = pid;
  document.querySelectorAll('#cmv40-subtab-content > .cmv40-panel').forEach(el => {
    el.style.display = 'none';
  });
  const active = document.getElementById(`cmv40-panel-${pid}`);
  if (active) active.style.display = 'block';
  const empty = document.getElementById('cmv40-empty-state');
  if (empty) empty.style.display = openCMv40Projects.find(p => p.id === pid) ? 'none' : '';
  document.querySelectorAll('#cmv40-subtab-projects .subtab-proj').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.pid === pid);
  });
}

function _createCMv40SubTab(project) {
  const container = document.getElementById('cmv40-subtab-projects');
  const btn = document.createElement('button');
  btn.className = 'subtab-proj active';
  btn.id = `cmv40-stab-${project.id}`;
  btn.dataset.pid = project.id;
  const name = project.session.source_mkv_name.replace(/\.mkv$/i, '');
  btn.innerHTML = `
    <span class="subtab-proj-icon">🎨</span>
    <span class="subtab-proj-name" data-tooltip="${escHtml(project.session.source_mkv_name)}">${escHtml(name.slice(0, 24))}${name.length > 24 ? '…' : ''}</span>
    <button class="subtab-proj-close" onclick="closeCMv40Project('${project.id}');event.stopPropagation()"
      data-tooltip="Cerrar proyecto">×</button>`;
  btn.onclick = (e) => { if (!e.target.closest('.subtab-proj-close')) switchCMv40SubTab(project.id); };
  container.appendChild(btn);
  _updateSubtabScrollState();
}

function _connectCMv40WebSocket(project) {
  try { project.ws?.close(); } catch (_) {}
  // Limpia timer de reconnect previo si lo hubiera (defensivo).
  if (project._wsReconnectTimer) {
    clearTimeout(project._wsReconnectTimer);
    project._wsReconnectTimer = null;
  }
  const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${wsProto}//${location.host}/ws/cmv40/${project.id}`);
  // Refresh REST inmediato al conectar el WS — sin esperar a que el backend
  // emita la primera línea (que puede tardar segundos si la fase actual está
  // en un paso silencioso de ffmpeg/dovi_tool). Garantiza que tras un wake
  // del Mac, el log catchea hasta el momento actual en cuanto el WS se abre.
  // También marca _lastWsMessageAt para que el watchdog cuente desde aquí.
  ws.onopen = () => {
    project._lastWsMessageAt = Date.now();
    _refreshCMv40Session(project.id);
  };
  ws.onmessage = (ev) => {
    _appendCMv40Log(project, ev.data);
    // Marca timestamp del último mensaje recibido — el watchdog usa esto
    // para detectar conexiones zombie (WS reporta OPEN pero no llega data).
    project._lastWsMessageAt = Date.now();
    // ── Optimistic update del timeline ────────────────────────────────
    // El GET /api/cmv40/{id} bajo carga I/O del NAS (extract-rpu en
    // paralelo) puede tardar 30-60s en responder. Sin esto, el spinner
    // del timeline lateral tardaba minutos en aparecer en la fase nueva
    // (visto en Fase H tras un remux pesado). Aquí extraemos el nombre
    // de la fase desde el marcador del log y actualizamos
    // project.session.running_phase de inmediato — el timeline pinta el
    // spinner correcto al instante. El GET posterior trae datos
    // autoritativos y rectifica si hay discrepancia.
    if (project.session) {
      const startMatch = ev.data.match(/━━━ Inicio fase:\s*([a-z_]+)\s*━━━/i);
      if (startMatch) {
        project.session.running_phase = startMatch[1];
        // Timestamp para que _cmv40AssignSession sepa que este valor es
        // reciente y NO debe pisarlo si un GET tardío trae null obsoleto.
        project._optimisticRunningPhaseAt = Date.now();
        _updateCMv40Panel(project);
      } else if (/✓ Fase \w+ completada en/.test(ev.data) ||
                 /✗ Fase \w+ FALLÓ/.test(ev.data)) {
        // Fase terminó: limpia running_phase localmente para que el
        // spinner desaparezca de la fase anterior mientras llega el GET
        // que dirá la fase nueva (si la hay) o el done definitivo.
        project.session.running_phase = null;
        project._optimisticRunningPhaseAt = 0;
        _updateCMv40Panel(project);
      }
    }
    // Refrescar sesión vía REST para tener phase/phase_history/etc al día
    if (ev.data.includes('━━━') || ev.data.includes('✓') || ev.data.includes('✗')) {
      _refreshCMv40Session(project.id);
    }
  };
  ws.onerror = () => {};
  // Reconnect automatico con backoff cuando el WS se cierra (Mac sleep,
  // pestaña en background con sleep agresivo, perdida temporal de red...).
  // SIN esto, tras el wake del Mac el log se queda congelado y la UI no se
  // actualiza aunque el job haya terminado en backend.
  ws.onclose = () => {
    if (project._closed) return;
    if (project._wsReconnectTimer) clearTimeout(project._wsReconnectTimer);
    project._wsReconnectTimer = setTimeout(() => {
      project._wsReconnectTimer = null;
      // Refrescar sesion ANTES de reconectar — si el job ya termino en
      // backend mientras dormiamos, esto pone la UI al dia inmediatamente.
      _refreshCMv40Session(project.id);
      // Solo reconectar si el proyecto sigue abierto y la sesion esta
      // viva (running_phase != null o estado no terminal). Si el job
      // termino, no hace falta WS — el refresh ya pinto el estado final.
      const stillOpen = openCMv40Projects.find(p => p.id === project.id);
      if (stillOpen && !stillOpen._closed) {
        const s = stillOpen.session || {};
        if (s.running_phase) {
          _connectCMv40WebSocket(stillOpen);
        }
      }
    }, 2000);
  };
  project.ws = ws;
}

// Copia al portapapeles el texto plano de un elemento que contiene líneas de
// log (div.log-line). Muestra un toast de confirmación; fallback a
// document.execCommand para contextos inseguros (file://, http en IP).
/** Copia texto al portapapeles con fallback a execCommand para HTTP (no
 *  secure context). Devuelve true/false; el caller muestra toasts. */
async function _copyTextToClipboardWithFallback(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* cae al fallback */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch { return false; }
}

async function copyLogToClipboard(containerId, btn) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const text = Array.from(el.querySelectorAll('.log-line, div'))
    .map(d => d.textContent || '')
    .filter(Boolean)
    .join('\n') || (el.textContent || '');
  if (!text.trim()) {
    showToast('No hay log que copiar', 'info');
    return;
  }
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    }
  } catch { ok = false; }
  if (ok) {
    showToast(`Log copiado (${text.length.toLocaleString()} caracteres)`, 'success');
    // Feedback visual breve en el botón si se pasó
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = '✓ Copiado';
      btn.disabled = true;
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1200);
    }
  } else {
    showToast('No se pudo copiar al portapapeles', 'error');
  }
}

// Auto-scroll "sticky": solo scrolla al fondo si el usuario YA estaba ahí.
// Tolerancia de 30px para no perder el pegado cuando llegan líneas rápidas.
// Si el usuario hace scroll arriba para leer, se respeta — el foco no vuelve
// al final en cada nueva línea.
function _isScrolledNearBottom(el, tolerance = 30) {
  return (el.scrollHeight - el.scrollTop - el.clientHeight) <= tolerance;
}

function _appendLogLine(containerEl, line) {
  if (!containerEl) return;
  const wasAtBottom = _isScrolledNearBottom(containerEl);
  const div = document.createElement('div');
  div.className = 'log-line ' + _classifyLogLine(line);
  div.textContent = line;
  containerEl.appendChild(div);
  if (wasAtBottom) containerEl.scrollTop = containerEl.scrollHeight;
}

/** Clasifica una linea de log por patrones textuales para aplicar color.
 *  Paleta rica (user-friendly) — todas las clases se definen en style.css
 *  con buena legibilidad sobre fondo oscuro del log-viewer.
 *
 *  Principio: distinguir claramente 2 tipos de linea:
 *    · Feedback de la APP (semantico, colorido): marcadores como
 *      [Fase X], 🎯 Resultado, 📋 Plan, ├─ sub-pasos, ✓ ok, ✗ error
 *    · Output crudo de las HERRAMIENTAS (muted): ffmpeg frame=X,
 *      mkvmerge Progress, dovi_tool Parsing RPU, Input #0/Stream #0,
 *      banners de version, stderr ruidoso. Todo lo que no empieza con
 *      [ o ━━━ o $ y no tiene marcadores semanticos se considera output
 *      crudo de tool y se renderiza muted + indentado.
 *
 *  Orden de prioridad importa: la primera regla que matchea gana.
 *  Errores > warnings > markers de fase > sub-pasos > resultado > plan >
 *  success > skip > command > tool-output (fallback).
 */
function _classifyLogLine(line) {
  const low = line.toLowerCase();
  // Errores explicitos (fallo duro)
  if (line.includes('✗') || line.includes('⛔') || line.includes('❌')
      || low.includes('error') || low.includes('fallo') || low.includes('aborta')) {
    return 'log-error';
  }
  // Warnings (soft alerts)
  if (line.includes('⚠') || low.includes('warning') || low.includes('aviso')) {
    return 'log-warning';
  }
  // Separadores entre fases
  if (line.includes('━━━')) {
    return 'log-phase';
  }
  // Sub-pasos con box-drawing chars: ├─ ┌─ └─
  if (/[├┌└]─/.test(line)) {
    return 'log-step';
  }
  // Plan (intencion antes de actuar): "📋 Plan:" o "Voy a ..."
  if (line.includes('📋 Plan:') || /\[Fase [A-H]\] Voy a /.test(line)) {
    return 'log-plan';
  }
  // Resultado / conclusion con implicacion para siguientes fases
  if (line.includes('🎯 Resultado:') || line.includes('🎯 Result:')) {
    return 'log-result';
  }
  // Success checkmark
  if (line.includes('✓')) {
    return 'log-success';
  }
  // Skipped steps
  if (line.includes('⏭')) {
    return 'log-skip';
  }
  // Drop-in special case (exito destacado)
  if (line.includes('🚀')) {
    return 'log-highlight';
  }
  // Comando shell ejecutado (transparencia)
  if (/^\s*\$ /.test(line) || /\] \$ /.test(line)) {
    return 'log-command';
  }
  // Fallback: si la linea NO empieza con [Algo] (prefijo de nuestro feedback)
  // y no tiene marcadores semanticos, es output crudo de una herramienta
  // externa (ffmpeg, mkvmerge, dovi_tool, ffprobe) — rendereizar muted.
  // El regex permite prefijo opcional de timestamp "[HH:MM:SS] " que mete
  // _cmv40_log antes del contenido.
  const hasAppPrefix = /^\[\d{2}:\d{2}:\d{2}\]\s*\[(?:Fase|Pipeline|Montando|Desmontando|Preflight|Validaci|sync-data)/i.test(line)
                       || /^\[(?:Fase|Pipeline|Montando|Desmontando|Preflight|Validaci|sync-data)/i.test(line);
  if (!hasAppPrefix) {
    return 'log-tool-output';
  }
  return '';
}

// ── Sistema de hidratación de logs CMv4.0 con watermark anti-duplicados ──
//
// ARQUITECTURA: cada proyecto trackea cuántas líneas de session.output_log
// ya están pintadas en cada uno de sus contenedores DOM (log permanente
// "📜 Log" + log running del overlay). Esto permite:
//
//  1. Hidratar el log permanente al cargar el proyecto (incluso si no hay
//     WS conectado porque running_phase=null) — fix del bug "log incompleto
//     al volver del Mac dormido".
//  2. Actualización incremental al refrescar la sesión: pinta solo las
//     líneas nuevas desde el último watermark.
//  3. WS streaming en vivo: cada línea recibida hace append + watermark++.
//     Cuando luego llega el refresh con la sesión completa, el watermark
//     evita duplicar las líneas que ya entraron por WS.
//
// El estado se guarda en `project._renderedLogCount` (permanente) y
// `project._renderedRunningLogCount` (overlay running, se reinicia al
// crear el overlay). Funciona porque session.output_log es append-only en
// el backend — nunca se borran líneas en mid-flight.

/**
 * Helper: detecta desincronización entre el log permanente del DOM y el
 * `session.output_log` del backend.
 *
 * Caso típico de desincronización (visto en producción durante I/O
 * intensivo del NAS):
 *   - Backend tiene output_log RAM=[L1..L1000], JSON=[L1..L900] (throttle
 *     retrasó los últimos saves).
 *   - Frontend hace fetch → recibe 900 líneas → watermark sube a 900.
 *   - WS entrega L1001 que se acababa de generar → frontend appendea +
 *     watermark sube a 901, PERO esa línea es realmente la 1001ª, no la 901ª.
 *   - Siguiente fetch trae 1001 líneas. _sync slice(901)=L902..L1001.
 *     L1001 ya está en DOM (vino por WS), se pintaría duplicada, y
 *     L901..L1000 quedan SIN pintar nunca → gap visible al usuario.
 *
 * Este helper compara la última línea del DOM con `output_log[watermark-1]`.
 * Si no coinciden → desincronización detectada → caller resetea y repinta.
 *
 * Devuelve true si DOM y backend están sincronizados, false si hubo desync.
 */
function _cmv40LogIsConsistent(containerEl, logArr, watermark) {
  if (watermark === 0) return true;            // nada pintado: trivialmente consistente
  if (watermark > logArr.length) {
    // Backend devolvió MENOS líneas que las pintadas. Caso común:
    //   - El WS entregó L1001 al frontend mientras un fetch REST estaba
    //     en vuelo. El backend respondió ese fetch con un snapshot que
    //     aún no incluía L1001 (throttle del save retrasó el JSON).
    //   - El frontend ya tiene L1001 pintada legítimamente.
    // NO es inconsistencia estructural: solo la session.output_log que
    // recibimos está atrasada. Si reseteáramos el DOM, perderíamos las
    // líneas WS legítimas. Devolvemos true → caller no resetea, y como
    // watermark >= logArr.length el sync simplemente no añade líneas.
    // El próximo fetch (4s) traerá más líneas y se reconciliará.
    return true;
  }
  // Buscar el último elemento .log-line del DOM (puede haber otros nodes).
  const lastEl = containerEl.lastElementChild;
  if (!lastEl) return false;  // DOM vacío pero watermark > 0 → desync
  // Comparamos con la línea backend que correspondería: output_log[watermark-1],
  // saltando las §§PROGRESS§§ que NO se renderizan al DOM.
  let backendIdx = watermark - 1;
  while (backendIdx >= 0) {
    const candidate = logArr[backendIdx];
    if (!_cmv40ParseProgress(candidate)) {
      return lastEl.textContent === candidate;
    }
    backendIdx--;
  }
  // Solo había progress markers — pintamos nada. El DOM debería estar vacío.
  return !lastEl;
}

function _cmv40SyncPermanentLog(project) {
  if (!project || !project.session) return;
  const pid = project.id;
  const containerEl = document.getElementById(`cmv40-log-${pid}`);
  if (!containerEl) return;
  const logArr = project.session.output_log || [];
  const watermark = project._renderedLogCount || 0;
  // Defensa contra desincronización (caso WS-vs-REST race durante I/O
  // intensivo del NAS, ver doc en _cmv40LogIsConsistent). Si detectamos
  // que la última línea del DOM no coincide con output_log[watermark-1],
  // asumimos que el watermark está corrupto y reseteamos: borramos el
  // DOM y re-pintamos desde cero. Coste: O(N) líneas, ms en miles.
  // Beneficio: garantía de orden y completitud absolutas.
  if (!_cmv40LogIsConsistent(containerEl, logArr, watermark)) {
    containerEl.innerHTML = '';
    project._renderedLogCount = 0;
  }
  if ((project._renderedLogCount || 0) >= logArr.length) return;
  const newLines = logArr.slice(project._renderedLogCount || 0);
  for (const line of newLines) {
    // Filtrar marcadores §§PROGRESS§§ (no se muestran como log, solo
    // alimentan la barra de progreso del overlay).
    const prog = _cmv40ParseProgress(line);
    if (prog) continue;
    _appendLogLine(containerEl, line);
  }
  project._renderedLogCount = logArr.length;
}

function _cmv40SyncRunningLog(project) {
  if (!project || !project.session) return;
  const pid = project.id;
  const containerEl = document.getElementById(`cmv40-running-log-${pid}`);
  if (!containerEl) return;
  const logArr = project.session.output_log || [];
  const watermark = project._renderedRunningLogCount || 0;
  if (!_cmv40LogIsConsistent(containerEl, logArr, watermark)) {
    containerEl.innerHTML = '';
    project._renderedRunningLogCount = 0;
  }
  if ((project._renderedRunningLogCount || 0) >= logArr.length) return;
  let lastProg = null;
  const newLines = logArr.slice(project._renderedRunningLogCount || 0);
  for (const line of newLines) {
    const prog = _cmv40ParseProgress(line);
    if (prog) { lastProg = prog; continue; }
    _appendLogLine(containerEl, line);
  }
  project._renderedRunningLogCount = logArr.length;
  if (lastProg) _cmv40UpdateProgressUI(pid, lastProg);
}

function _appendCMv40Log(project, line) {
  if (!project || !project.session) return;
  const pid = project.id;
  // Marcador de progreso: no se añade al log visual, solo actualiza la barra.
  // Nota: la línea se persiste en session.output_log también, así que el
  // watermark debe contarla aunque no se renderice — sino el próximo sync
  // intentaría re-pintarla.
  const prog = _cmv40ParseProgress(line);
  if (prog) {
    _cmv40UpdateProgressUI(pid, prog);
  } else {
    _appendLogLine(document.getElementById(`cmv40-log-${pid}`), line);
    _appendLogLine(document.getElementById(`cmv40-running-log-${pid}`), line);
  }
  // Watermark++: el WS acaba de entregar una línea que también está
  // (o estará en milisegundos) en session.output_log. Sin este incremento,
  // un refresh posterior intentaría pintarla de nuevo.
  project._renderedLogCount = (project._renderedLogCount || 0) + 1;
  if (document.getElementById(`cmv40-running-log-${pid}`)) {
    project._renderedRunningLogCount = (project._renderedRunningLogCount || 0) + 1;
  }
}

async function _refreshCMv40Session(pid) {
  // silent: timeouts transitorios bajo carga I/O pesada (Fase C/E/F escribiendo
  // 40+ GB) no son utiles al usuario — el siguiente tick los resuelve y el WS
  // sigue trayendo el log. Sin silent, el toast 'el servidor no respondio en 30s'
  // aparecia repetidamente durante extract/inject/remux pesados.
  const data = await apiFetch(`/api/cmv40/${pid}`, { silent: true });
  if (!data) return;
  const project = openCMv40Projects.find(p => p.id === pid);
  if (project) {
    _cmv40AssignSession(project, data);
    _updateCMv40Panel(project);
    if (project.autoContinue && !data.running_phase && !data.error_message) {
      _cmv40MaybeAutoAdvance(project);
    }
  }
  refreshCMv40Sidebar();
}

// ── Render del panel ─────────────────────────────────────────────

function _createCMv40Panel(project) {
  const s = project.session;
  const pid = project.id;
  const panel = document.createElement('div');
  panel.className = 'cmv40-panel subtab-panel';
  panel.id = `cmv40-panel-${pid}`;
  panel.style.display = 'none';
  panel.innerHTML = `
    <div class="project-panel-inner" style="max-width:1100px; margin:0 auto; padding:24px 20px">
      <div id="cmv40-info-${pid}"></div>
      <div id="cmv40-phase-strip-${pid}" class="cmv40-phase-strip"></div>
      <div id="cmv40-active-phase-${pid}"></div>

      <!-- Log de ejecución -->
      <div class="section-card" style="margin-top:16px">
        <div class="section-header">
          <div><div class="section-title">📜 Log</div></div>
          <div style="display:flex; gap:6px">
            <button class="btn btn-ghost btn-xs"
              onclick="copyLogToClipboard('cmv40-log-${pid}', this)"
              data-tooltip="Copiar todo el log al portapapeles">📋 Copiar</button>
            <button class="btn btn-ghost btn-xs" onclick="_clearCMv40Log('${pid}')">🗑️ Limpiar</button>
          </div>
        </div>
        <div class="section-body" style="padding:0">
          <div id="cmv40-log-${pid}" class="cmv40-log"></div>
        </div>
      </div>
    </div>`;
  document.getElementById('cmv40-subtab-content').appendChild(panel);
  _updateCMv40Panel(project);
}

function _clearCMv40Log(pid) {
  const el = document.getElementById(`cmv40-log-${pid}`);
  if (el) el.innerHTML = '';
}

function _updateCMv40Panel(project) {
  const s = project.session;
  const pid = project.id;
  _renderCMv40Info(s, pid);
  _renderCMv40PhaseStrip(s, pid);
  _renderCMv40ActivePhase(project);
  _renderCMv40RunningOverlay(project);
  // Hidrata el log permanente (card "📜 Log" del panel del proyecto) con
  // las líneas que aún no estén pintadas. CRÍTICO para el caso "Mac dormido
  // toda la noche": el job sigue en backend, output_log crece a miles de
  // líneas, y al volver el frontend tiene que ver el log completo aunque
  // running_phase=null y no haya WS activo. Sin esta llamada el container
  // solo recibía líneas via WS streaming y se quedaba vacío post-mortem.
  _cmv40SyncPermanentLog(project);
}

function _renderCMv40RunningOverlay(project) {
  const s = project.session;
  const pid = project.id;
  const panel = document.getElementById(`cmv40-panel-${pid}`);
  if (!panel) return;
  let overlay = panel.querySelector('.cmv40-running-overlay');

  // Auto-pipeline "puente": entre una fase y la siguiente el backend pone
  // running_phase=null brevemente. Dos heurísticas para mantener el overlay
  // sin parpadeo durante esa ventana:
  //   (a) project._autoChaining — flag que se enciende al disparar una fase
  //       desde _cmv40MaybeAutoAdvance o desde el arranque inicial de Fase A.
  //   (b) "recent running" — hace menos de 15s vimos running_phase no-null.
  //       Actúa como red de seguridad si _autoChaining no se seteo a tiempo
  //       (ej. polling tarda en captar el cambio).
  // Se apaga al llegar a terminal o al intervenir manualmente.
  //
  // preflight_decision != "ok" (y no vacío) es estado terminal: el pre-flight
  // decidió "Keep recomendado" y el pipeline no avanza más sin acción del
  // usuario. Sin esto, el modal se quedaba colgado en "bridge auto" después
  // de un keep_l8_default porque autoContinue=true seguía verdadero.
  const isPreflightHalted = !!(s.preflight_decision && s.preflight_decision !== 'ok');
  const terminalPhase = (s.phase === 'done' || s.phase === 'error'
                          || !!s.error_message || isPreflightHalted);
  if (terminalPhase) project._autoChaining = false;
  if (s.running_phase) project._lastRunningPhaseAt = Date.now();
  const recentRunning = (Date.now() - (project._lastRunningPhaseAt || 0)) < 15000;
  const bridgingAuto  = !s.running_phase && project.autoContinue && !terminalPhase
                        && (!!project._autoChaining || recentRunning);
  const shouldShow    = !!s.running_phase || bridgingAuto;

  if (shouldShow) {
    // Crear o actualizar overlay
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.className = 'cmv40-running-overlay';
      overlay.innerHTML = `
        <div class="cmv40-running-box">
          <div class="cmv40-running-timeline-wrap">
            <!-- Mini cabecera con la pelicula que se esta procesando — vive en
                 el TOP de la columna izquierda, encima del timeline. Solo ocupa
                 el ancho de la columna (330px), no el de todo el modal. -->
            <div class="cmv40-running-movie" id="cmv40-running-movie-${pid}">
              <div class="cmv40-running-movie-poster" id="cmv40-running-movie-poster-${pid}">🎬</div>
              <div class="cmv40-running-movie-info">
                <div class="cmv40-running-movie-title" id="cmv40-running-movie-title-${pid}"></div>
                <div class="cmv40-running-movie-meta" id="cmv40-running-movie-meta-${pid}"></div>
              </div>
            </div>
            <div class="cmv40-running-timeline-inner" id="cmv40-running-timeline-${pid}"></div>
          </div>
          <div class="cmv40-running-main">
            <div class="cmv40-running-header">
              <div class="cmv40-running-spinner"></div>
              <div style="flex:1">
                <div class="cmv40-running-title" id="cmv40-running-title-${pid}"></div>
                <div class="cmv40-running-subtitle" id="cmv40-running-subtitle-${pid}">El proyecto está bloqueado mientras se ejecuta la tarea</div>
              </div>
              <button class="btn btn-ghost btn-sm"
                onclick="copyLogToClipboard('cmv40-running-log-${pid}', this)"
                data-tooltip="Copiar el log actual al portapapeles">📋 Copiar log</button>
              <button class="btn btn-danger btn-sm" onclick="cmv40CancelRunning('${pid}')">🛑 Cancelar</button>
            </div>
            <div class="cmv40-progress" id="cmv40-progress-${pid}"
              style="padding:14px 18px; background:#1a1e2a; border-bottom:1px solid #2a2f3d; display:flex; flex-direction:column; gap:10px">
              <div class="cmv40-progress-meta"
                style="display:flex; align-items:baseline; justify-content:space-between; gap:12px; font-size:12px">
                <span class="cmv40-progress-label" id="cmv40-progress-label-${pid}"
                  style="font-weight:600; color:#e8ecf4; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; flex:1">Preparando…</span>
                <span class="cmv40-progress-right"
                  style="display:flex; align-items:baseline; gap:12px; flex-shrink:0; font-variant-numeric:tabular-nums">
                  <span class="cmv40-progress-eta" id="cmv40-progress-eta-${pid}"
                    style="color:#9aa3b2; font-size:11px"></span>
                  <span class="cmv40-progress-pct" id="cmv40-progress-pct-${pid}"
                    style="color:#4da3ff; font-weight:700; font-size:15px; min-width:54px; text-align:right">—</span>
                </span>
              </div>
              <div class="cmv40-progress-track"
                style="height:14px; background:#0b0e17; border:1px solid #2a2f3d; border-radius:8px; overflow:hidden; position:relative; box-shadow:inset 0 1px 3px rgba(0,0,0,0.5)">
                <div class="cmv40-progress-bar indeterminate" id="cmv40-progress-bar-${pid}"></div>
              </div>
            </div>
            <div class="cmv40-running-log" id="cmv40-running-log-${pid}"></div>
          </div>
        </div>`;
      panel.appendChild(overlay);
      // Suscribe al WebSocket para actualizar el log en tiempo real
      _cmv40BindRunningLog(project);
    }
    // Hidratar la mini cabecera de pelicula (poster + titulo). Si la sesion
    // ya tiene tmdb_info cacheado, usarlo directo; si no, usar source_mkv_name
    // como fallback de texto y disparar lookup TMDb async.
    _cmv40HydrateRunningMovieHeader(project);
    // Actualizar título + subtítulo según estemos en una fase o "puente"
    const titleEl    = document.getElementById(`cmv40-running-title-${pid}`);
    const subtitleEl = document.getElementById(`cmv40-running-subtitle-${pid}`);
    if (titleEl) {
      if (s.running_phase) {
        const autoTag = project.autoContinue ? '🤖 Auto · ' : '';
        titleEl.textContent = autoTag + (CMV40_RUNNING_LABELS[s.running_phase] || `Ejecutando: ${s.running_phase}`);
        if (subtitleEl) subtitleEl.textContent = 'El proyecto está bloqueado mientras se ejecuta la tarea';
      } else {
        // Modo puente: fase X completada, siguiente a punto de arrancar.
        // En vez de mostrar "Preparando siguiente fase" (redundante y vago),
        // mostramos el titulo de la proxima fase deducida del estado actual.
        const nextPhase = _cmv40GuessNextPhase(s);
        const autoTag = project.autoContinue ? '🤖 Auto · ' : '';
        if (nextPhase) {
          titleEl.textContent = autoTag + nextPhase;
          if (subtitleEl) subtitleEl.textContent = 'Transición entre fases — arrancando en un instante';
        } else {
          titleEl.textContent = autoTag + 'Encadenando fases…';
          if (subtitleEl) subtitleEl.textContent = '';
        }
      }
    }
    // Actualizar timeline en cada tick — incremental, NO innerHTML wholesale.
    // Antes reemplazabamos todo el HTML, lo que (a) reiniciaba la animación
    // del spinner de la fase en curso (elemento destruido/recreado cada tick),
    // (b) rompía el CSS transition de la barra de progreso total (cada vez
    // un elemento nuevo con width inicial 0% → sin transición), y (c) saltaba
    // el scroll de .cmv40-tl-steps a 0 (nuevo DOM).
    const tlWrap = document.getElementById(`cmv40-running-timeline-${pid}`);
    if (tlWrap) _cmv40UpdateTimelineIncremental(tlWrap, s, project);
    // CRÍTICO: sincronizar el running log desde session.output_log en cada
    // tick (no solo cuando se crea el overlay). Antes el running log solo
    // se actualizaba via WS messages — si el cliente WS estaba zombie tras
    // Mac sleep, las líneas que llegaban via REST refresh NO aparecían en
    // el running overlay (solo en el log permanente). El watermark +
    // consistency check del helper evita duplicados con las líneas que SÍ
    // llegaron por WS.
    _cmv40SyncRunningLog(project);
  } else if (overlay) {
    // Quitar overlay con animación — solo cuando SEGURO que no hay más fases
    overlay.classList.add('closing');
    setTimeout(() => overlay.remove(), 200);
  }
}

/** Update incremental del timeline — actualiza solo los campos que cambian
 *  sin reemplazar el DOM (preserva animación del spinner, CSS transition de
 *  la barra de progreso total, y scrollTop de la lista de pasos). */
function _cmv40UpdateTimelineIncremental(tlWrap, s, project) {
  // Si el timeline aun no existe (primera vez), render completo.
  if (!tlWrap.querySelector('.cmv40-tl-steps')) {
    tlWrap.innerHTML = _cmv40RenderTimeline(s, project);
    // Posiciona la fase activa centrada al abrir el modal — sin animación
    // (estamos en frame 0, smooth se vería como un salto).
    const stepsElInit = tlWrap.querySelector('.cmv40-tl-steps');
    const stepsInit = _cmv40PlanAutoSteps(s);
    const statusesInit = stepsInit.map(st => _cmv40StepStatus(st, s));
    const activeIdxInit = statusesInit.findIndex(st => st === 'running');
    const activeKeyInit = activeIdxInit >= 0
      ? stepsInit[activeIdxInit].key
      : (stepsInit[stepsInit.length - 1] && stepsInit[stepsInit.length - 1].key);
    if (stepsElInit && activeKeyInit) {
      _cmv40ScrollActiveStepIntoView(stepsElInit, activeKeyInit, 'auto');
      tlWrap.dataset.activeStepKey = activeKeyInit;
    }
    return;
  }

  // Recalcular métricas
  const steps = _cmv40PlanAutoSteps(s);
  const stepStatuses = steps.map(st => _cmv40StepStatus(st, s));
  const doneCount = stepStatuses.filter(st => st === 'done' || st === 'skipped').length;
  const totalCount = steps.length;
  const progressPct = totalCount > 0 ? Math.round((doneCount / totalCount) * 100) : 0;

  // Timer: elapsed / remaining (mismo helper que el full render — garantiza
  // que ambos rendered + tick usan el MISMO startedMs cacheado, sin saltos
  // entre fuentes server-time vs client-cached).
  const startedMs = _cmv40ResolveStartedMs(s, project);
  const hist = s.phase_history || [];
  const isTerminal = (s.phase === 'done' || !!s.error_message);
  let elapsedLabel  = '—';
  let remainingText = '';
  let newBaseRemaining = null;   // null = no actualizar data-base-remaining
  if (startedMs) {
    let elapsedSecs;
    if (isTerminal) {
      const lastWithEnd = [...hist].reverse().find(h => h.finished_at);
      const endMs = lastWithEnd ? Date.parse(lastWithEnd.finished_at) : Date.now();
      elapsedSecs = (endMs - startedMs) / 1000;
      remainingText = s.phase === 'done' ? 'finalizado' : (s.error_message ? 'con error' : '');
    } else {
      elapsedSecs = (Date.now() - startedMs) / 1000;
      newBaseRemaining = _cmv40ComputeRemainingSecs(s, steps, stepStatuses, hist);
      remainingText = newBaseRemaining > 0
        ? `~${_cmv40FmtClock(newBaseRemaining)} restantes (auto)`
        : 'casi listo…';
    }
    elapsedLabel = _cmv40FmtClock(elapsedSecs);
  }

  // Update header fields (mismos elementos, solo text/style — transiciones OK)
  const elapsedEl   = tlWrap.querySelector('.cmv40-tl-timer-elapsed');
  const remainingEl = tlWrap.querySelector('.cmv40-tl-timer-remaining');
  const pctEl       = tlWrap.querySelector('.cmv40-tl-progress-pct');
  const fillEl      = tlWrap.querySelector('.cmv40-tl-progress-fill');
  const progressBox = tlWrap.querySelector('.cmv40-tl-progress');
  // Sincroniza data-started-at del DOM con el cache canónico — el tick lee
  // de ahí, y debe coincidir con el startedMs que usa este render. Sin
  // esto el contador alterna entre dos valores cuando la fuente cambia.
  if (elapsedEl && startedMs && elapsedEl.dataset.startedAt !== String(startedMs)) {
    elapsedEl.dataset.startedAt = String(startedMs);
  }
  if (elapsedEl   && elapsedEl.textContent   !== elapsedLabel)   elapsedEl.textContent   = elapsedLabel;
  if (remainingEl && remainingEl.textContent !== remainingText)  remainingEl.textContent = remainingText;
  // Refrescar snapshot del remaining — el tick de 1s decrementa desde aquí.
  if (elapsedEl && newBaseRemaining !== null) {
    elapsedEl.dataset.baseRemaining = String(newBaseRemaining);
    elapsedEl.dataset.baseAt = String(Date.now());
  }
  const pctText = `${doneCount}/${totalCount} · ${progressPct}%`;
  if (pctEl       && pctEl.textContent       !== pctText)        pctEl.textContent       = pctText;
  if (fillEl) {
    const newW = progressPct + '%';
    if (fillEl.style.width !== newW) fillEl.style.width = newW;
  }
  if (progressBox) {
    const cls = isTerminal && !s.error_message ? 'cmv40-tl-progress-done'
              : s.error_message ? 'cmv40-tl-progress-error'
              : '';
    progressBox.classList.toggle('cmv40-tl-progress-done',  cls === 'cmv40-tl-progress-done');
    progressBox.classList.toggle('cmv40-tl-progress-error', cls === 'cmv40-tl-progress-error');
  }

  // Update trust badge: el badge se calcula dinamicamente a partir del
  // estado (gates evaluados / trust_ok / trust_override) y debe refrescarse
  // cuando cambia la fase. Sin esto, el badge se queda en "pendiente
  // validaciones" aun despues de que Fase B haya clasificado el target.
  const trustBadgeEl = tlWrap.querySelector('.cmv40-tl-trust-badge');
  if (trustBadgeEl) {
    const gatesEvaluated2 = !!(s.target_trust_gates && Object.keys(s.target_trust_gates).length);
    const targetProvidedIdx2 = CMV40_PHASES_ORDER.indexOf('target_provided');
    const curPhaseIdx2 = CMV40_PHASES_ORDER.indexOf(s.phase);
    const beforeGates2 = curPhaseIdx2 < targetProvidedIdx2 || !gatesEvaluated2;
    let cls2, txt2;
    if (beforeGates2) {
      cls2 = 'pending'; txt2 = '⏳ Auto · pendiente validaciones';
    } else if (s.target_trust_ok && s.trust_override !== 'force_interactive') {
      cls2 = 'trusted'; txt2 = '🚀 Auto · trusted';
    } else {
      cls2 = 'manual'; txt2 = '🔬 Manual · revisión visual';
    }
    if (trustBadgeEl.textContent !== txt2) {
      trustBadgeEl.textContent = txt2;
    }
    // Asegurar que solo tiene la clase correcta de las tres
    trustBadgeEl.classList.toggle('pending', cls2 === 'pending');
    trustBadgeEl.classList.toggle('trusted', cls2 === 'trusted');
    trustBadgeEl.classList.toggle('manual',  cls2 === 'manual');
  }

  // Update de steps: solo reemplaza el HTML de la lista si el HASH de
  // estados+labels cambió (evita spinner restart cuando no cambia nada).
  const newStepsHash = stepStatuses.map((st, i) =>
    `${steps[i].key}:${st}:${steps[i].customLabel || ''}`
  ).join('|');
  const stepsEl = tlWrap.querySelector('.cmv40-tl-steps');
  if (stepsEl && stepsEl.dataset.hash !== newStepsHash) {
    const savedScroll = stepsEl.scrollTop;
    // Re-genera solo los <li> de steps, no toca el <ol> wrapper (mantiene
    // scrollTop implícitamente si no tocamos el contenedor... pero innerHTML
    // sí reemplaza hijos → guardamos scrollTop y lo restauramos).
    stepsEl.innerHTML = _cmv40RenderTimelineStepsHTML(steps, stepStatuses, s);
    stepsEl.scrollTop = savedScroll;
    stepsEl.dataset.hash = newStepsHash;
  }

  // Auto-scroll dinámico: cuando avanza la fase en curso, traer la nueva
  // fase activa al centro del panel lateral con scroll suave. Solo se
  // dispara cuando cambia la KEY (no en cada tick) — así el usuario puede
  // hacer scroll manual dentro de una fase sin que el timeline rebote.
  // Si ya no hay fase running (terminal: done o error), apuntamos a la
  // última fase con estado distinto de pending para mantener el contexto.
  const activeIdx = stepStatuses.findIndex(st => st === 'running');
  let activeKey = activeIdx >= 0 ? steps[activeIdx].key : null;
  if (!activeKey) {
    for (let i = stepStatuses.length - 1; i >= 0; i--) {
      if (stepStatuses[i] !== 'pending') { activeKey = steps[i].key; break; }
    }
  }
  if (stepsEl && activeKey && tlWrap.dataset.activeStepKey !== activeKey) {
    _cmv40ScrollActiveStepIntoView(stepsEl, activeKey, 'smooth');
    tlWrap.dataset.activeStepKey = activeKey;
  }
}

/** Genera solo el contenido interno (<li>...</li>) de la lista de steps.
 *  Extraído de _cmv40RenderTimeline para reuso desde el update incremental. */
function _cmv40RenderTimelineStepsHTML(steps, stepStatuses, s) {
  return steps.map((st, i) => {
    const status = stepStatuses[i];
    const iconMap = {
      done:    '<span class="cmv40-tl-status-icon done">✓</span>',
      running: '<span class="cmv40-tl-status-icon running"></span>',
      skipped: '<span class="cmv40-tl-status-icon skipped">⏭</span>',
      pending: '<span class="cmv40-tl-status-icon pending"></span>',
    };
    const elapsed = status === 'done' ? _cmv40StepElapsedSecs(st.key, s) : null;
    const doneLabel = elapsed != null
      ? `completado · ${_cmv40FmtClock(elapsed)}`
      : 'completado';
    const defaultLabel = status === 'done'    ? doneLabel
                       : status === 'skipped' ? 'omitida'
                       : status === 'running' ? 'en curso…'
                       : `ETA ${_cmv40FmtEta(st.etaSecs)}`;
    const label = st.customLabel || defaultLabel;
    const etaHtml = `<span class="cmv40-tl-eta ${status}">${escHtml(label)}</span>`;
    return `<li class="cmv40-tl-step cmv40-tl-${status}" data-step-key="${escHtml(st.key)}">
      <div class="cmv40-tl-rail">${iconMap[status]}</div>
      <div class="cmv40-tl-body">
        <div class="cmv40-tl-title">
          <span class="cmv40-tl-phase-icon">${st.icon}</span>
          <span>${escHtml(st.title)}</span>
        </div>
        <div class="cmv40-tl-what">${escHtml(st.what)}</div>
        ${etaHtml}
      </div>
    </li>`;
  }).join('');
}

/** Auto-scroll del timeline lateral para mantener visible la fase activa.
 *  Se ejecuta solo cuando cambia la fase running (no en cada tick) — así no
 *  pelea contra el scroll manual del usuario dentro de una misma fase.
 *  Si no hay fase running (todo done), se hace scroll a la última fase
 *  completada para que el usuario vea el final del recorrido. */
function _cmv40ScrollActiveStepIntoView(stepsEl, activeKey, behavior = 'smooth') {
  if (!stepsEl || !activeKey) return;
  const li = stepsEl.querySelector(`li.cmv40-tl-step[data-step-key="${activeKey}"]`);
  if (!li) return;
  const containerH = stepsEl.clientHeight;
  const liTop      = li.offsetTop;
  const liH        = li.offsetHeight;
  // Centra el step activo verticalmente dentro del contenedor scrollable.
  const target = liTop - (containerH / 2) + (liH / 2);
  const max = stepsEl.scrollHeight - containerH;
  stepsEl.scrollTo({ top: Math.max(0, Math.min(max, target)), behavior });
}

function _cmv40ParseProgress(line) {
  // Detecta marcadores §§PROGRESS§§{json} (con o sin timestamp [HH:MM:SS] delante)
  const m = line.match(/§§PROGRESS§§(\{.*\})/);
  if (!m) return null;
  try { return JSON.parse(m[1]); } catch { return null; }
}

function _cmv40UpdateProgressUI(pid, prog) {
  const bar = document.getElementById(`cmv40-progress-bar-${pid}`);
  const pct = document.getElementById(`cmv40-progress-pct-${pid}`);
  const lab = document.getElementById(`cmv40-progress-label-${pid}`);
  const eta = document.getElementById(`cmv40-progress-eta-${pid}`);
  if (!bar || !pct || !lab) return;
  const p = Math.max(0, Math.min(100, prog.pct ?? 0));
  bar.classList.remove('indeterminate');
  bar.style.width = p + '%';
  pct.textContent = p.toFixed(1) + '%';
  lab.textContent = prog.label || '';
  if (eta) {
    if (prog.eta_s != null && prog.eta_s > 0) {
      const m = Math.floor(prog.eta_s / 60);
      const s = prog.eta_s % 60;
      eta.textContent = `ETA ${m}:${String(s).padStart(2, '0')}`;
    } else {
      eta.textContent = '';
    }
  }
}

/** Hidrata la mini cabecera del overlay con la pelicula que se procesa.
 *  Pinta poster (de tmdb_info si existe, fallback emoji) + titulo + año.
 *  Si la sesion no tiene tmdb_info, dispara hydrateTmdbCard logica
 *  tras un pequeño defer (no bloquea el render del overlay). */
function _cmv40HydrateRunningMovieHeader(project) {
  const pid = project.id;
  const posterEl = document.getElementById(`cmv40-running-movie-poster-${pid}`);
  const titleEl  = document.getElementById(`cmv40-running-movie-title-${pid}`);
  const metaEl   = document.getElementById(`cmv40-running-movie-meta-${pid}`);
  if (!posterEl || !titleEl || !metaEl) return;

  const s = project.session || {};
  const t = s.tmdb_info || null;

  // Poster: usa tmdb si tenemos URL, sino emoji 🎬 placeholder
  if (t && t.poster_url) {
    posterEl.innerHTML = `<img src="${escHtml(t.poster_url)}" alt="${escHtml(t.title || '')}" loading="lazy">`;
  } else {
    posterEl.textContent = '🎬';
  }

  // Titulo: tmdb.title > source_mkv_name limpio > id
  let titleText = '';
  if (t && t.title) titleText = t.title;
  else if (s.source_mkv_name) {
    // Limpia .mkv y reemplaza separadores comunes por espacios para
    // hacer el filename mas legible mientras llega tmdb_info.
    titleText = s.source_mkv_name.replace(/\.mkv$/i, '').replace(/[\._]+/g, ' ');
  }
  else titleText = s.id || '—';
  titleEl.textContent = titleText;

  // Meta: año + runtime + génenros (si tmdb), si no nada
  if (t) {
    const parts = [];
    if (t.year) parts.push(String(t.year));
    if (t.runtime_minutes) parts.push(`${Math.floor(t.runtime_minutes/60)}h ${t.runtime_minutes%60}min`);
    if (t.genres && t.genres.length) parts.push(t.genres.slice(0, 2).join(' · '));
    metaEl.textContent = parts.join(' · ');
  } else {
    metaEl.textContent = '';
  }
}


function _cmv40BindRunningLog(project) {
  // El overlay running se acaba de crear → reset del watermark del running
  // log y delega la hidratación al helper centralizado, que pinta toda la
  // sesión y trackea el contador para futuras incrementales.
  const pid = project.id;
  const logEl = document.getElementById(`cmv40-running-log-${pid}`);
  if (!logEl) return;
  logEl.innerHTML = '';
  project._renderedRunningLogCount = 0;
  _cmv40SyncRunningLog(project);
  // Primera hidratación: al fondo (el usuario aún no ha scrolleado)
  logEl.scrollTop = logEl.scrollHeight;
}

async function cmv40CancelRunning(pid) {
  const project = openCMv40Projects.find(p => p.id === pid);
  const phaseLabel = project && project.session && project.session.running_phase
    ? (CMV40_RUNNING_LABELS[project.session.running_phase] || project.session.running_phase)
    : 'la fase actual';
  const isAuto = project && project.autoContinue;
  // Mensaje contextual: explica que cancela el subprocess en curso y, si
  // el auto-pipeline esta activo, que tambien se desactiva el auto-avance
  // (no lanza la siguiente fase).
  const message = isAuto
    ? `Se matará el subprocess de "${phaseLabel}", se limpiarán los temporales generados y se desactivará el auto-avance del pipeline. Las fases ya completadas se conservan; podrás relanzar manualmente la fase cuando quieras.`
    : `Se matará el subprocess de "${phaseLabel}" y se limpiarán los temporales generados. Las fases ya completadas se conservan; podrás relanzar manualmente la fase cuando quieras.`;
  showConfirm(
    '¿Cancelar la ejecución en curso?',
    message,
    async () => {
      await apiFetch(`/api/cmv40/${pid}/cancel`, { method: 'POST' });
      const proj = openCMv40Projects.find(p => p.id === pid);
      if (proj) {
        proj._lastAutoFiredFor = null;
        proj._lastAutoFiredAt = 0;
        proj._autoChaining = false;
        if (proj.autoContinue) {
          proj.autoContinue = false;
          showToast('Cancelado — auto-avance desactivado', 'info');
        } else {
          showToast('Cancelando…', 'info');
        }
      }
    },
    'Cancelar fase',
  );
}

// Hidrata una ficha TMDb en el DOM (container dado) a partir de un
// filename. Cache por clave en `_tmdbCardCache` para evitar re-fetches.
// Uso: desde Tab 1, Tab 2 y Tab 3, pasar el id del contenedor + filename.
const _tmdbCardCache = new Map();  // clave = filename -> details|null

async function hydrateTmdbCard(containerId, filename) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!filename) { el.innerHTML = ''; return; }

  // Cache hit inmediato
  if (_tmdbCardCache.has(filename)) {
    el.innerHTML = renderTmdbCardHTML(_tmdbCardCache.get(filename)) || '';
    return;
  }
  // Skeleton mínimo mientras llega la respuesta
  el.innerHTML = '<div class="tmdb-card-loading"></div>';

  try {
    const data = await apiFetch('/api/cmv40/tmdb-lookup', {
      method: 'POST',
      body: JSON.stringify({ source_mkv_name: filename }),
    });
    const details = (data && data.details) ? data.details : null;
    _tmdbCardCache.set(filename, details);
    el.innerHTML = renderTmdbCardHTML(details) || '';
  } catch {
    el.innerHTML = '';
  }
}

// Genérico — reutilizable para Tab 1, Tab 2 y Tab 3.
function renderTmdbCardHTML(t) {
  if (!t) return '';
  const metaParts = [];
  if (t.year) metaParts.push(String(t.year));
  if (t.runtime_minutes)
    metaParts.push(`${Math.floor(t.runtime_minutes/60)}h ${t.runtime_minutes%60}min`);
  if (t.genres && t.genres.length) metaParts.push(t.genres.join(' · '));

  const ratingHtml = (t.vote_count > 0)
    ? `<span class="cmv40-tmdb-rating" data-tooltip="${t.vote_count.toLocaleString()} votos en TMDb">★ ${t.vote_average.toFixed(1)}</span>`
    : '';
  const origHtml = (t.original_title && t.original_title !== t.title)
    ? `<span class="cmv40-tmdb-orig">· ${escHtml(t.original_title)}</span>`
    : '';
  const taglineHtml = t.tagline
    ? `<div class="cmv40-tmdb-tagline">“${escHtml(t.tagline)}”</div>`
    : '';
  const overviewHtml = t.overview
    ? `<div class="cmv40-tmdb-overview">${escHtml(t.overview)}</div>`
    : '';

  const links = [];
  if (t.tmdb_url) links.push(`<a href="${escHtml(t.tmdb_url)}" target="_blank" rel="noreferrer noopener">TMDb</a>`);
  if (t.imdb_id)   links.push(`<a href="https://www.imdb.com/title/${escHtml(t.imdb_id)}/" target="_blank" rel="noreferrer noopener">IMDb</a>`);
  if (t.homepage)  links.push(`<a href="${escHtml(t.homepage)}" target="_blank" rel="noreferrer noopener">Web oficial</a>`);
  const linksHtml = links.length ? `<div class="cmv40-tmdb-links">${links.join(' · ')}</div>` : '';

  const posterHtml = t.poster_url
    ? `<img class="cmv40-tmdb-poster" src="${escHtml(t.poster_url)}" alt="${escHtml(t.title)}" loading="lazy">`
    : `<div class="cmv40-tmdb-poster cmv40-tmdb-poster-placeholder">🎬</div>`;
  const backdropHtml = t.backdrop_url
    ? `<div class="cmv40-tmdb-backdrop" style="background-image: url('${escHtml(t.backdrop_url)}');"></div>`
    : '';

  return `
    <div class="cmv40-tmdb-card">
      ${backdropHtml}
      ${posterHtml}
      <div class="cmv40-tmdb-info">
        <div class="cmv40-tmdb-titlerow">
          <span class="cmv40-tmdb-title">${escHtml(t.title || t.original_title || '—')}</span>
          ${origHtml}
          ${ratingHtml}
        </div>
        ${metaParts.length ? `<div class="cmv40-tmdb-meta">${escHtml(metaParts.join(' · '))}</div>` : ''}
        ${taglineHtml}
        ${overviewHtml}
        ${linksHtml}
      </div>
    </div>`;
}

function _renderCMv40Info(s, pid) {
  const container = document.getElementById(`cmv40-info-${pid}`);
  if (!container) return;
  const srcDv = s.source_dv_info;
  const tgtDv = s.target_dv_info;
  const canEditName = s.phase !== 'done' && !s.archived;
  const project = openCMv40Projects.find(p => p.id === pid);
  const autoOn = !!(project && project.autoContinue);
  const canAuto = s.phase !== 'done' && !s.archived;
  const tmdbCardHtml = renderTmdbCardHTML(s.tmdb_info);
  container.innerHTML = `
    ${tmdbCardHtml}
    <div class="section-card">
      <div class="section-header" style="display:flex; align-items:flex-start; justify-content:space-between; gap:12px">
        <div><div class="section-title">🎬 Proyecto CMv4.0</div>
        <div class="section-subtitle">💾 Los cambios se guardan automáticamente tras cada acción. Cerrar la pestaña no pierde nada.</div></div>
        ${canAuto ? `
        <button class="btn btn-${autoOn ? 'primary' : 'ghost'} btn-sm" onclick="cmv40ToggleAuto('${pid}')"
          data-tooltip="${(() => {
            const trust = !!s.target_trust_ok && s.trust_override !== 'force_interactive';
            if (trust) return 'Auto-ejecuta el pipeline completo A→H sin pausas. Los trust gates ya aprobaron alineación, Fase D se omite automáticamente.';
            if (s.target_type) return 'Auto-ejecuta cada fase tras la anterior. Si los trust gates no aprueban, pausa en Fase D para revisión manual del chart.';
            return 'Auto-ejecuta cada fase tras la anterior. La pausa en Fase D depende del target — sin gates trusted requiere revisión manual del chart.';
          })()}">
          ${autoOn ? '🤖 Auto ON' : '🤖 Auto OFF'}
        </button>` : ''}
      </div>
      <div class="section-body">
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px">
          <div>
            <div style="font-size:11px; color:var(--text-3); margin-bottom:2px">MKV origen</div>
            <div style="font-weight:600">${escHtml(s.source_mkv_name)}</div>
            <div style="font-size:11px; color:var(--text-3); margin-top:4px">
              ${srcDv ? `Profile ${srcDv.profile}${srcDv.el_type ? ` (${srcDv.el_type})` : ''} · CM ${srcDv.cm_version} · ${s.source_frame_count.toLocaleString()} frames` : 'Sin analizar'}
            </div>
            ${s.source_workflow ? `<div style="font-size:10px; margin-top:4px">
              <span class="cmv40-workflow-badge cmv40-workflow-${s.source_workflow}">${_cmv40WorkflowLabel(s.source_workflow)}</span>
            </div>` : ''}
          </div>
          <div>
            <div style="font-size:11px; color:var(--text-3); margin-bottom:2px">MKV salida ${canEditName ? '<span style="color:var(--text-3)">· editable</span>' : ''}</div>
            ${canEditName
              ? `<input type="text" id="cmv40-output-name-${pid}" class="cmv40-output-name-input"
                    value="${escHtml(s.output_mkv_name)}"
                    onblur="_cmv40SaveOutputName('${pid}', this.value)"
                    onkeydown="if(event.key==='Enter'){this.blur()}">`
              : `<div style="font-weight:600">${escHtml(s.output_mkv_name)}</div>`}
            <div style="font-size:11px; color:var(--text-3); margin-top:4px">
              ${tgtDv ? `RPU target: Profile ${tgtDv.profile}${tgtDv.el_type ? ` (${tgtDv.el_type})` : ''} · CM ${tgtDv.cm_version} · ${s.target_frame_count.toLocaleString()} frames` : ''}
              ${s.sync_delta ? ` · <span style="color:var(--orange)">Δ ${s.sync_delta > 0 ? '+' : ''}${s.sync_delta} frames</span>` : ''}
            </div>
          </div>
        </div>
      </div>
    </div>
    ${_renderCMv40RecommendationCard(s, pid)}`;

  // Si aún no tenemos tmdb_info, intentamos hidratarlo (puede haber fallado la
  // tarea background). Best-effort, sin bloquear UI.
  if (!s.tmdb_info && !project?._tmdbLookupTried) {
    if (project) project._tmdbLookupTried = true;
    _cmv40HydrateTmdbClient(pid);
  }
}

/**
 * Renderiza la card "🎯 Análisis y recomendación" del modelo Keep/Restore.
 * Solo aparece si tenemos datos del análisis del bin (target_l8_classification
 * != ''). Muestra:
 *   - Calidad del bin (CMv4 CORE / CORE+ / FULL / DEFAULT)
 *   - Comparación L2 source vs target (cuando Fase A ya corrió)
 *   - Recomendación final con badge grande
 *   - Botones de acción cuando recommended_action="keep"
 */
function _renderCMv40RecommendationCard(s, pid) {
  // No mostrar la card si no hay análisis del bin todavía (típico antes de
  // que termine el pre-flight, o sesiones legacy sin estos campos).
  if (!s.target_l8_classification) return '';

  const action = s.recommended_action || '';
  const isKeep = action === 'keep';
  const isDropIn = action === 'drop_in';
  const isMerge = action === 'merge';
  const isUnknown = action === 'unknown' || action === '';
  const projectDone = (s.phase === 'done' || s.archived);

  // Badge alineado a la paleta de la app (light mode, variables CSS).
  // Patrón estándar: dim background + border + color del nivel semántico.
  const badgeStyle = isKeep
    ? 'background:var(--blue-dim); color:var(--blue); border:1px solid var(--blue-border)'
    : isDropIn
    ? 'background:var(--green-dim); color:var(--green); border:1px solid var(--green-border)'
    : isMerge
    ? 'background:var(--orange-dim); color:var(--orange); border:1px solid var(--orange-border)'
    : 'background:var(--surface-2); color:var(--text-2); border:1px solid var(--sep)';

  const label = s.recommended_action_label || (isUnknown ? '⏳ Esperando análisis' : '—');
  const reason = s.recommended_action_reason || '';

  // Tag de calidad del bin (la que va al filename)
  const qualityTag = s.target_l8_quality_label || (
    s.target_l8_classification === 'default' ? 'CMv4 sintético' :
    s.target_l8_classification === 'real' ? 'CMv4 (real)' :
    s.target_l8_classification === 'indeterminate' ? 'CMv4 (ambiguo)' :
    'CMv4 ?'
  );

  // Chip comparación L2 (color semántico, paleta de la app)
  const l2Comp = s.l2_comparison || '';
  const l2Chip = l2Comp === 'identical'
    ? `<span style="background:var(--green-dim); color:var(--green); border:1px solid var(--green-border); padding:3px 9px; border-radius:10px; font-size:11px; font-weight:600">L2 idéntico al MKV original</span>`
    : l2Comp === 'different'
    ? `<span style="background:var(--orange-dim); color:var(--orange); border:1px solid var(--orange-border); padding:3px 9px; border-radius:10px; font-size:11px; font-weight:600">L2 distinto del MKV original</span>`
    : '';

  // Datos técnicos en formato lista (grid 2-col label/value) — más legible
  // que tabla HTML y sin riesgo de solape. Padding fijo + line-height claro.
  const techRows = [];
  if (s.target_l8_unique_count) {
    techRows.push({ label: 'Combos L8', value: String(s.target_l8_unique_count) });
  }
  if (s.target_l8_neutral_frames_pct != null && s.target_frames_analyzed) {
    const worked = (1.0 - s.target_l8_neutral_frames_pct) * 100;
    techRows.push({ label: 'Frames con trim', value: `${worked.toFixed(0)}%` });
  }
  if (s.target_l8_has_mid_contrast || s.target_l8_has_clip_trim) {
    const extras = [];
    if (s.target_l8_has_mid_contrast) extras.push('target_mid_contrast');
    if (s.target_l8_has_clip_trim) extras.push('clip_trim');
    techRows.push({ label: 'CMv4.0 extras', value: extras.join(' · ') });
  }
  if (s.target_l2_unique_count) {
    techRows.push({ label: 'Combos L2 (bin)', value: String(s.target_l2_unique_count) });
  }
  if (s.source_l2_unique_count) {
    techRows.push({ label: 'Combos L2 (MKV original)', value: String(s.source_l2_unique_count) });
  }
  const techGrid = techRows.length ? `
    <div style="margin-top:14px; display:grid; grid-template-columns:auto 1fr; gap:6px 16px; align-items:baseline">
      ${techRows.map(r => `
        <div style="font-size:11px; color:var(--text-3); font-weight:500">${escHtml(r.label)}</div>
        <div style="font-size:12px; color:var(--text-1); font-family:ui-monospace,SFMono-Regular,Menlo,monospace">${escHtml(r.value)}</div>
      `).join('')}
    </div>` : '';

  // Botones de acción cuando la recomendación es KEEP y el proyecto no está
  // cerrado todavía. Si el proyecto ya está done/archived, no se muestran.
  let actionButtons = '';
  if (isKeep && !projectDone) {
    actionButtons = `
      <div style="display:flex; gap:8px; margin-top:14px; flex-wrap:wrap">
        <button class="btn btn-primary btn-sm" onclick="cmv40AcceptKeep('${pid}')"
          data-tooltip="Cierra el proyecto sin tocar el MKV original. Un reproductor compatible con CMv4.0 (p3i T4 / Sony / LG modernos) hará la conversión al vuelo en runtime.">
          ✓ Mantener MKV actual
        </button>
        <button class="btn btn-ghost btn-sm" onclick="cmv40OverrideRecommendation('${pid}')"
          data-tooltip="Procesa el MKV inyectando el RPU CMv4.0 aunque el bin sea sintético. Resultado equivalente a la conversión al vuelo del reproductor pero quedará archivado como MKV CMv4.0 completo.">
          🔬 Inyectar RPU igualmente
        </button>
      </div>`;
  }

  // Banner verde cuando el proyecto está cerrado — distinguimos por
  // output_workflow para que el usuario sepa qué pasó realmente.
  // Para restore_merge, los niveles transferidos dependen del source_workflow
  // (P7 FEL → [1,2,3,6,8,9,10,11,254]; MEL/P8 → [3,8,9,11,254]).
  let doneBanner = '';
  if (s.output_workflow === 'keep_cmv29') {
    doneBanner = `
      <div style="margin-top:12px; padding:10px 12px; background:var(--green-dim); border:1px solid var(--green-border); border-radius:var(--r-sm); color:var(--text-1); font-size:12px; line-height:1.4">
        <span style="color:var(--green); font-weight:600">✓ Proyecto cerrado — MKV actual mantenido</span>
        — el fichero original quedó intacto. Tu reproductor (p3i T4 / Sony /
        LG modernos) hace la conversión CMv4.0 al vuelo en runtime.
      </div>`;
  } else if (s.output_workflow === 'restore_dropin') {
    doneBanner = `
      <div style="margin-top:12px; padding:10px 12px; background:var(--green-dim); border:1px solid var(--green-border); border-radius:var(--r-sm); color:var(--text-1); font-size:12px; line-height:1.4">
        <span style="color:var(--green); font-weight:600">✓ MKV procesado — RPU CMv4.0 inyectado (rápido)</span>
        — el bin se inyectó directo sobre el MKV original, sin merge
        frame-a-frame. Calidad: ${escHtml(qualityTag)}.
      </div>`;
  } else if (s.output_workflow === 'restore_merge') {
    const mergeLevels = s.source_workflow === 'p7_fel'
      ? '[1, 2, 3, 6, 8, 9, 10, 11, 254]'
      : '[3, 8, 9, 11, 254]';
    const l2Note = s.source_workflow === 'p7_fel'
      ? 'L1/L2/L6 del bin sobrescriben al del source (refinan stats legacy del BD)'
      : 'L1/L2/L5/L6 del MKV original preservados';
    doneBanner = `
      <div style="margin-top:12px; padding:10px 12px; background:var(--green-dim); border:1px solid var(--green-border); border-radius:var(--r-sm); color:var(--text-1); font-size:12px; line-height:1.4">
        <span style="color:var(--green); font-weight:600">✓ MKV procesado — RPU CMv4.0 inyectado (merge selectivo)</span>
        — niveles CMv4.0 ${mergeLevels} transferidos del bin al MKV; ${l2Note}.
        Calidad: ${escHtml(qualityTag)}.
      </div>`;
  } else if (projectDone) {
    // Proyecto done sin output_workflow conocido (sesiones legacy procesadas
    // antes del Bloque 4). Banner genérico.
    doneBanner = `
      <div style="margin-top:12px; padding:10px 12px; background:var(--green-dim); border:1px solid var(--green-border); border-radius:var(--r-sm); color:var(--text-1); font-size:12px; line-height:1.4">
        <span style="color:var(--green); font-weight:600">✓ Proyecto completado</span>
      </div>`;
  }

  return `
    <div class="section-card">
      <div class="section-header">
        <div>
          <div class="section-title">🎯 Análisis y recomendación</div>
          <div class="section-subtitle">Decisión Mantener vs Inyectar (rápido / preserva L2) basada en el análisis del bin: clasificación L8, tier de calidad CMv4 y comparación L2 source vs target</div>
        </div>
      </div>
      <div class="section-body">
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap">
          <span style="padding:6px 12px; border-radius:var(--r-sm); font-weight:700; font-size:13px; ${badgeStyle}">${escHtml(label)}</span>
          <span style="background:var(--surface-2); color:var(--text-2); border:1px solid var(--sep); padding:4px 10px; border-radius:10px; font-size:11px; font-weight:600; font-family:ui-monospace,SFMono-Regular,Menlo,monospace">${escHtml(qualityTag)}</span>
          ${l2Chip}
        </div>
        ${reason ? `<div style="margin-top:12px; color:var(--text-2); font-size:12px; line-height:1.5">${escHtml(reason)}</div>` : ''}
        ${techGrid}
        ${actionButtons}
        ${doneBanner}
      </div>
    </div>`;
}

function cmv40AcceptKeep(pid) {
  showConfirm(
    'Mantener el MKV actual y cerrar el proyecto',
    'El proyecto se cierra como completado sin tocar el MKV original. '
      + 'Tu reproductor (p3i T4 / Sony / LG modernos compatibles con CMv4.0) '
      + 'hará la conversión al vuelo en runtime — el resultado visible es '
      + 'equivalente al de inyectar el RPU, pero sin gastar ~25 min de '
      + 'procesado ni ~50 GB de disco temporal.',
    async () => {
      const data = await apiFetch(`/api/cmv40/${pid}/accept-keep`, { method: 'POST' });
      if (!data) {
        showToast('Error al cerrar el proyecto', 'error');
        return;
      }
      const project = openCMv40Projects.find(p => p.id === pid);
      if (project) {
        _cmv40AssignSession(project, data);
        _updateCMv40Panel(project);
      }
      refreshCMv40Sidebar();
      showToast('✓ Proyecto cerrado — MKV actual mantenido', 'success');
    },
    'Mantener MKV actual',
  );
}

function cmv40OverrideRecommendation(pid) {
  showConfirm(
    'Inyectar RPU CMv4.0 aunque el bin sea sintético',
    'El pipeline va a procesar el MKV (~25 min de Fase A + extracción + remux) '
      + 'aunque el bin del repo no aporte un L8 trabajado real. '
      + 'El resultado visible es equivalente a la conversión al vuelo del '
      + 'reproductor, pero el MKV queda archivado como CMv4.0 "completo" '
      + 'para compatibilidad con otros equipos.',
    async () => {
      const data = await apiFetch(`/api/cmv40/${pid}/override-recommendation`, { method: 'POST' });
      if (!data) {
        showToast('Error al continuar el procesado', 'error');
        return;
      }
      const project = openCMv40Projects.find(p => p.id === pid);
      if (project) {
        _cmv40AssignSession(project, data);
        _updateCMv40Panel(project);
      }
      refreshCMv40Sidebar();
      showToast('🔬 Inyección forzada — el pipeline continuará', 'info');
    },
    'Inyectar RPU CMv4.0',
  );
}

async function _cmv40HydrateTmdbClient(pid) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project || project.session.tmdb_info) return;
  const data = await apiFetch('/api/cmv40/tmdb-lookup', {
    method: 'POST',
    body: JSON.stringify({ source_mkv_name: project.session.source_mkv_name }),
  });
  if (!data || !data.details) return;
  project.session.tmdb_info = data.details;
  _updateCMv40Panel(project);
}

function _cmv40WorkflowLabel(wf) {
  return {
    p7_fel: '🎯 P7 FEL · merge CMv4.0 preservando dual-layer',
    p7_mel: '📀 P7 MEL · descarta EL → P8.1 CMv4.0',
    p8:     '🎬 P8.1 · inject directo → P8.1 CMv4.0',
  }[wf] || wf;
}

async function _cmv40SaveOutputName(pid, newName) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  const trimmed = (newName || '').trim();
  if (!trimmed || trimmed === project.session.output_mkv_name) return;
  const data = await apiFetch(`/api/cmv40/${pid}/rename-output`, {
    method: 'POST',
    body: JSON.stringify({ output_mkv_name: trimmed }),
  });
  if (data) {
    _cmv40AssignSession(project, data);
    showToast('Nombre actualizado', 'success');
  }
}

function _renderCMv40PhaseStrip(s, pid) {
  const container = document.getElementById(`cmv40-phase-strip-${pid}`);
  if (!container) return;
  const phases = [
    { key: 'source_analyzed', icon: '🔍', label: 'Analizar origen' },
    { key: 'target_provided', icon: '🎯', label: 'RPU target' },
    { key: 'extracted',       icon: '✂️', label: 'Extraer BL/EL' },
    { key: 'sync_verified',   icon: '📊', label: 'Verificar sync' },
    { key: 'injected',        icon: '💉', label: 'Inyectar' },
    { key: 'remuxed',         icon: '📦', label: 'Remux' },
    { key: 'validated',       icon: '✅', label: 'Validar' },
  ];
  const currentIdx = CMV40_PHASES_ORDER.indexOf(s.phase);
  const isError = s.phase === 'error';
  container.innerHTML = phases.map((ph, i) => {
    const phaseIdx = CMV40_PHASES_ORDER.indexOf(ph.key);
    let state = 'pending';
    if (phaseIdx < currentIdx) state = 'done';
    else if (phaseIdx === currentIdx) state = isError ? 'error' : 'active';
    return `
      <div class="cmv40-phase-step ${state}">
        <div class="cmv40-phase-circle">${ph.icon}</div>
        <div class="cmv40-phase-label">${ph.label}</div>
      </div>
      ${i < phases.length - 1 ? '<div class="cmv40-phase-conn"></div>' : ''}
    `;
  }).join('');
}

// Definición de todas las fases: inicio + fin
// Una fase está "done" si la phase actual es >= el estado que esa fase PRODUCE
const CMV40_FASES_DEF = [
  { key: 'A', title: 'Fase A — Analizar MKV origen',       produces: 'source_analyzed', startsFrom: 'created',         reset_to: 'created' },
  { key: 'B', title: 'Fase B — Proporcionar RPU target',   produces: 'target_provided', startsFrom: 'source_analyzed', reset_to: 'source_analyzed' },
  { key: 'C', title: 'Fase C — Extraer BL/EL',             produces: 'extracted',       startsFrom: 'target_provided', reset_to: 'target_provided' },
  { key: 'D', title: 'Fase D + E — Verificar y corregir sincronización',  produces: 'sync_verified',   startsFrom: 'extracted',       reset_to: 'extracted' },
  { key: 'F', title: 'Fase F — Inyectar RPU',              produces: 'injected',        startsFrom: 'sync_verified',   reset_to: 'sync_verified' },
  { key: 'G', title: 'Fase G — Remux final',               produces: 'remuxed',         startsFrom: 'injected',        reset_to: 'injected' },
  { key: 'H', title: 'Fase H — Validación final',          produces: 'validated',       startsFrom: 'remuxed',         reset_to: 'remuxed' },
];

function _cmv40PhaseState(sessionPhase, produces, startsFrom) {
  const currentIdx  = CMV40_PHASES_ORDER.indexOf(sessionPhase);
  const producesIdx = CMV40_PHASES_ORDER.indexOf(produces);
  const startsIdx   = CMV40_PHASES_ORDER.indexOf(startsFrom);
  if (currentIdx >= producesIdx) return 'done';
  if (currentIdx >= startsIdx)   return 'active';
  return 'pending';
}

/** Banner ámbar que aparece encima del proyecto cuando Fase B detectó
 *  gates con degradación previsible y pide ACK explícita al usuario.
 *  Contiene la lista de gates fallados + botones "Cambiar target" /
 *  "Continuar igualmente". */
function _cmv40RenderCriticalAckBanner(pid, s) {
  if (!s.awaiting_critical_ack) return '';
  const failures = s.critical_gate_failures || [];
  if (!failures.length) return '';
  const itemsHtml = failures.map(f => {
    const label = ({
      l5_div: 'L5 — letterbox / active area',
      l6_div: 'L6 — MaxCLL/MaxFALL estático',
      l1_div: 'L1 — brillo medio dinámico',
    })[f.gate] || f.gate;
    return `
      <li class="cmv40-ack-item">
        <span class="cmv40-ack-item-name">${escHtml(label)}</span>
        <span class="cmv40-ack-item-why">${escHtml(f.why || '')}</span>
      </li>`;
  }).join('');
  return `
    <div class="section-card cmv40-card-ack-required" style="margin-top:12px">
      <div class="section-body cmv40-ack-body">
        <div class="cmv40-ack-head">
          <span class="cmv40-ack-icon">⚠️</span>
          <div class="cmv40-ack-title-block">
            <div class="cmv40-ack-title">Divergencias detectadas — confirma cómo continuar</div>
            <div class="cmv40-ack-sub">
              El bin pasa los gates estructurales (CMv4.0, L8, frames) pero hay divergencias
              que <strong>Fase D no puede corregir</strong>. Si continúas, el resultado puede
              tener artefactos visibles. Decide cómo seguir:
            </div>
          </div>
        </div>
        <ul class="cmv40-ack-list">${itemsHtml}</ul>
        <div class="cmv40-ack-actions">
          <button class="btn btn-ghost btn-md"
            onclick="_cmv40ChangeTarget('${pid}')"
            data-tooltip="Vuelve a Fase B para escoger otro bin (del repo DoviTools, de carpeta local o extraído de otro MKV propio)">
            ↩ Cambiar target
          </button>
          <button class="btn btn-warning btn-md"
            onclick="_cmv40AcknowledgeCriticalGates('${pid}')"
            data-tooltip="Reconoces que el resultado puede ser degradado y autorizas continuar — Fase D se saltará automáticamente">
            ⚠ Continuar igualmente (resultado degradado)
          </button>
        </div>
      </div>
    </div>`;
}

/** Handler del botón "Continuar igualmente" — POST al endpoint de ack y
 *  refresca el panel para que el auto-pipeline pueda avanzar. */
async function _cmv40AcknowledgeCriticalGates(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}/acknowledge-critical-gates`, { method: 'POST' });
  if (!data) return;
  const project = openCMv40Projects.find(p => p.id === pid);
  if (project) {
    _cmv40AssignSession(project, data);
    // Reset del dedup del orquestador para que en el siguiente tick auto
    // detecte el cambio de estado (awaiting_critical_ack: true → false).
    project._lastAutoFiredFor = null;
    _updateCMv40Panel(project);
  }
  showToast('⚠️ Degradación reconocida — pipeline continúa, Fase D omitida', 'info');
}

/** Handler del botón "Cambiar target" — reset a 'source_analyzed' para
 *  que el usuario seleccione otro bin. Reusa el endpoint reset-to. */
async function _cmv40ChangeTarget(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}/reset-to/source_analyzed`, { method: 'POST' });
  if (!data) return;
  const project = openCMv40Projects.find(p => p.id === pid);
  if (project) {
    _cmv40AssignSession(project, data);
    project._lastAutoFiredFor = null;
    project._autoChaining = false;
    _updateCMv40Panel(project);
  }
  showToast('Listo para escoger otro target — abre la card de Fase B', 'info');
}

function _renderCMv40ActivePhase(project) {
  const s = project.session;
  const pid = project.id;
  const container = document.getElementById(`cmv40-active-phase-${pid}`);
  if (!container) return;

  // Ensure expandedPhases map exists
  if (!project.expandedPhases) {
    project.expandedPhases = {};  // key: fase key, value: true/false
  }

  // Renderizar todas las fases como cards — intercalando los gates entre
  // Fase B y Fase C (trust gates) y entre Fase G y Fase H (validación final).
  const cards = [];
  // Si hay error_message poblado, forzamos que la fase active se renderize
  // siempre expandida — aunque el usuario hubiera colapsado la card antes.
  // Sin esto, el botón "Reintentar" puede quedar oculto bajo el chevrón ▸ y
  // el usuario solo ve el banner rojo + la card "done" de la fase anterior.
  const forceExpandActiveOnError = !!s.error_message;
  CMV40_FASES_DEF.forEach(fase => {
    const state = _cmv40PhaseState(s.phase, fase.produces, fase.startsFrom);
    let isExpanded = project.expandedPhases[fase.key] !== undefined
      ? project.expandedPhases[fase.key]
      : (state === 'active');
    if (forceExpandActiveOnError && state === 'active') isExpanded = true;
    cards.push(_cmv40RenderFaseCard(pid, s, fase, state, isExpanded));
    // Inyectar gate card tras Fase B — trust gates + compatibilidad
    if (fase.key === 'B') {
      const gateBCExpanded = project.expandedPhases['GATE_BC'] !== undefined
        ? project.expandedPhases['GATE_BC']
        : true;  // por defecto expandida — la info es la que el usuario necesita revisar
      cards.push(_cmv40RenderGateCardBC(pid, s, gateBCExpanded));
    }
    // Inyectar gate card tras Fase G — validación final pre-finalizar
    if (fase.key === 'G') {
      const gateGHExpanded = project.expandedPhases['GATE_GH'] !== undefined
        ? project.expandedPhases['GATE_GH']
        : false;
      cards.push(_cmv40RenderGateCardGH(pid, s, gateGHExpanded));
    }
  });

  // Banner de error de la última acción intentada (no bloquea el flujo)
  let errorHtml = '';
  if (s.error_message) {
    // Detectar la fase active actual para ofrecer "Reintentar" directo desde
    // el banner. Sin esto, el usuario tenía que ir a buscar la card de la
    // fase (que podría estar colapsada) para encontrar el botón equivalente.
    const activeFase = CMV40_FASES_DEF.find(f =>
      _cmv40PhaseState(s.phase, f.produces, f.startsFrom) === 'active'
    );
    const retryBtn = activeFase
      ? `<button class="btn btn-warning btn-sm" onclick="_cmv40RetryActivePhase('${pid}','${activeFase.key}')"
            data-tooltip="Vuelve a ejecutar ${escHtml(activeFase.title)}">🔄 Reintentar</button>`
      : '';
    errorHtml = `
      <div class="section-card cmv40-card-error" style="margin-top:12px">
        <div class="section-body" style="display:flex; align-items:center; gap:12px">
          <span style="font-size:20px">⚠️</span>
          <div style="flex:1">
            <div style="font-weight:600; color:var(--red); margin-bottom:2px">Error en la última acción</div>
            <div style="font-size:12px; color:var(--text-2)">${escHtml(s.error_message)}</div>
          </div>
          ${retryBtn}
          <button class="btn btn-ghost btn-sm" onclick="_cmv40ClearError('${pid}')"
            data-tooltip="Descartar este mensaje">✕</button>
        </div>
      </div>`;
  }

  // Si done, card de celebración arriba
  let doneHtml = '';
  if (s.phase === 'done' && !s.archived) {
    doneHtml = `
      <div class="section-card" style="margin-top:16px; background:var(--green-dim); border:1px solid var(--green)">
        <div class="section-body" style="text-align:center; padding:20px">
          <div style="font-size:32px">🎉</div>
          <div style="font-size:15px; font-weight:700; margin-top:4px">MKV CMv4.0 completado</div>
          <div style="font-size:11px; color:var(--text-3); margin-top:4px">${escHtml(s.output_mkv_path || s.output_mkv_name)}</div>
          <div style="margin-top:12px; display:flex; gap:8px; justify-content:center">
            <button class="btn btn-ghost btn-sm" onclick="cmv40Cleanup('${pid}')">🗑️ Limpiar artefactos</button>
          </div>
          <div style="margin-top:8px; font-size:10px; color:var(--text-3)">
            ⚠️ Al limpiar artefactos no podrás rehacer fases (el proyecto pasará a modo solo lectura)
          </div>
        </div>
      </div>`;
  }

  // Si archived, banner de solo lectura
  let archivedHtml = '';
  if (s.archived) {
    archivedHtml = `
      <div class="section-card" style="margin-top:16px; background:var(--surface-2); border:1px solid var(--sep-strong)">
        <div class="section-body" style="display:flex; align-items:center; gap:12px">
          <span style="font-size:22px">🗃️</span>
          <div style="flex:1">
            <div style="font-weight:600">Proyecto archivado — solo lectura</div>
            <div style="font-size:11px; color:var(--text-3); margin-top:2px">
              Los artefactos intermedios se borraron. No se pueden rehacer fases.
              Para iterar de nuevo, crea un proyecto CMv4.0 nuevo desde el mismo MKV origen.
            </div>
          </div>
        </div>
      </div>`;
  }

  // Footer de acciones: botón "Limpiar artefactos" siempre visible mientras
  // el proyecto NO esté archived ni tenga running_phase activo. Permite
  // limpiar tras un fallo de fase sin tener que esperar a que el pipeline
  // termine entero.
  let actionsFooterHtml = '';
  if (!s.archived && !s.running_phase) {
    actionsFooterHtml = `
      <div class="section-card cmv40-actions-footer" style="margin-top:16px">
        <div class="section-body" style="display:flex; align-items:center; gap:12px">
          <span style="font-size:18px; opacity:0.7">🗑️</span>
          <div style="flex:1; min-width:0">
            <div style="font-size:12.5px; font-weight:600">Limpiar artefactos del workdir</div>
            <div style="font-size:11px; color:var(--text-3); margin-top:2px">
              Libera el espacio del workdir intermedio (HEVC, RPU, .mkv.tmp). Disponible siempre que no haya una fase en curso — útil tras un fallo para liberar espacio sin esperar al final del pipeline. <strong>Pasa el proyecto a modo solo lectura.</strong>
            </div>
          </div>
          <button class="btn btn-ghost btn-sm" onclick="cmv40Cleanup('${pid}')"
            style="flex-shrink:0">Limpiar artefactos</button>
        </div>
      </div>`;
  }

  // Banner ACK (gates críticos pendientes) por encima de todo lo demás —
  // pause-point bloqueante: hasta que el usuario decida, el auto-pipeline
  // no avanza. Ver _cmv40MaybeAutoAdvance.
  const ackBannerHtml = _cmv40RenderCriticalAckBanner(pid, s);
  container.innerHTML = ackBannerHtml + errorHtml + archivedHtml + doneHtml + cards.join('') + actionsFooterHtml;

  // Lanzar cargas asíncronas donde aplique. En Fase B el tab default es
  // "Repo DoviTools" — disparamos su loader; los otros tabs (path / MKV)
  // se cargan lazy al hacer click via _cmv40SwitchTargetTab.
  if (_cmv40PhaseState(s.phase, 'target_provided', 'source_analyzed') === 'active') {
    _cmv40LoadRepoForPanel(pid);
  }
  // Chart: cargar si Fase D activa o completada y está expandida.
  // Guards para NO disparar per_frame_data.json on-demand durante auto:
  //   1. Si hay otra fase running — no lanzar otro dovi_tool export pesado
  //      sobre el mismo workdir (race con Fase F inject)
  //   2. Si target_trust_ok — drop-in trusted, nunca se va a usar el chart
  //      (Fase D ya se saltó por gates). Regenerarlo on-demand desperdicia
  //      ~2 min de CPU superponiendose a Fase F.
  const faseDState = _cmv40PhaseState(s.phase, 'sync_verified', 'extracted');
  const dExpanded = project.expandedPhases['D'] !== undefined
    ? project.expandedPhases['D']
    : (faseDState === 'active');
  const shouldLoadChart = (faseDState === 'active' || faseDState === 'done')
                          && dExpanded
                          && !s.running_phase
                          && !s.target_trust_ok;
  if (shouldLoadChart) {
    _loadCMv40SyncChart(project);
  }
}

function _cmv40RenderFaseCard(pid, s, fase, state, isExpanded) {
  // Detectar fases omitidas o modificadas por modo trusted/drop-in
  const skipped = s.phases_skipped || [];
  // Fase C: omitida completamente cuando drop-in + trusted (ambos
  // demux_dual_layer y per_frame_data_skipped marcados).
  const isSkippedC = fase.key === 'C'
                      && skipped.includes('demux_dual_layer')
                      && (skipped.includes('per_frame_data_skipped') || skipped.includes('mux_dual_layer'))
                      && state === 'done';
  // Fase D: omitida cuando el target es trusted y no hay override manual.
  // Usamos la misma condicion que el body (_cmv40FaseDoneBody key==='D') —
  // asi es robusta a reload del proyecto (phases_skipped no se persiste
  // desde el frontend y solo estaria disponible mid-sesion).
  const trustedSkippedD = !!s.target_trust_ok
                           && (s.trust_override || 'auto') !== 'force_interactive';
  const isSkippedD = fase.key === 'D'
                     && (skipped.includes('sync_verification_pause') || trustedSkippedD)
                     && state === 'done';
  // Fase F: en drop-in se salta SOLO el merge, pero el inject SI se ejecuta.
  // NO marcamos la fase como omitida (seria engañoso) — se anotara el "sin
  // merge" en el summary pero el stateIcon sigue siendo ✅ Completado.
  const isDropInF = fase.key === 'F' && skipped.includes('merge_cmv40_transfer') && state === 'done';
  const isSkipped = isSkippedC || isSkippedD;   // solo C y D son "totalmente omitidas"

  const stateIcon = isSkipped ? '⏭️'
                  : state === 'done' ? '✅'
                  : state === 'active' ? '▶️' : '🔒';
  const stateLabel = isSkippedC ? 'Omitida — drop-in: no hace falta demux ni per-frame data'
                   : isSkippedD ? 'Omitida — target trusted: sync validado por gates'
                   : isDropInF  ? 'Ejecutada en modo drop-in (inject directo sin merge previo)'
                   : state === 'done' ? 'Completado'
                   : state === 'active' ? 'En curso' : 'Pendiente';

  // Resumen cuando está done
  let summary = '';
  if (state === 'done') {
    summary = _cmv40FaseSummary(fase.key, s);
  }

  // Body según estado
  let body = '';
  if (isExpanded) {
    if (state === 'active') {
      body = _cmv40FaseBody(fase.key, pid, s);
    } else if (state === 'done') {
      body = `
        <div class="section-body">
          ${_cmv40FaseDoneBody(fase.key, pid, s)}
          ${s.archived ? '' : `
          <div style="margin-top:12px; padding-top:12px; border-top:1px solid var(--sep)">
            <button class="btn btn-danger btn-sm" onclick="_cmv40Redo('${pid}','${fase.reset_to}','${fase.key}')"
              data-tooltip="Vuelve a esta fase. Las fases posteriores se invalidarán.">🔄 Rehacer esta fase</button>
          </div>`}
        </div>`;
    } else {
      body = `<div class="section-body"><div style="font-size:12px; color:var(--text-3)">🔒 Completa las fases anteriores para activar esta.</div></div>`;
    }
  }

  const extraCls = isSkipped ? ' cmv40-fase-skipped' : '';
  // Subtitulo: cuando la fase se omite o se ejecuta en drop-in preferimos
  // el stateLabel explicito (es mas claro que el summary auto-generado que
  // puede sugerir trabajo que realmente no se hizo).
  const preferStateLabel = isSkipped || isDropInF;
  const subtitle = (!preferStateLabel && summary) ? summary : stateLabel;
  // Sufijo diferenciado por razón de omisión — antes era "(omitida)" genérico
  // para Fase C y D sin diferenciar el porqué (C por drop-in, D por trust).
  const skippedSuffix = isSkippedC
    ? '(omitida · drop-in)'
    : isSkippedD
    ? (s.user_acknowledged_degradation
        ? '(omitida · usuario reconoció degradación)'
        : '(omitida · trust gates OK)')
    : '(omitida)';
  const titleSuffix = isSkipped
    ? ` <span style="color:var(--text-3); font-weight:400; font-size:11px">${skippedSuffix}</span>`
    : isDropInF
    ? ' <span style="color:#8a4a00; font-weight:500; font-size:11px">(drop-in)</span>'
    : '';
  return `
    <div class="section-card cmv40-fase-card cmv40-fase-${state}${extraCls}" style="margin-top:12px" data-fase-key="${fase.key}">
      <div class="section-header cmv40-fase-header" onclick="_cmv40TogglePhase('${pid}','${fase.key}')">
        <div class="cmv40-fase-state-icon">${stateIcon}</div>
        <div style="flex:1">
          <div class="section-title">${escHtml(fase.title)}${titleSuffix}</div>
          <div class="section-subtitle">${subtitle}</div>
        </div>
        <div class="cmv40-fase-chevron">${isExpanded ? '▾' : '▸'}</div>
      </div>
      ${body}
    </div>`;
}

/* ───────── Gate cards (pseudo-fases) ──────────────────────────────────
 * No son fases ejecutables: son puntos de decisión que la app evalúa
 * automáticamente a partir de datos ya capturados. Por eso no tienen
 * botón "rehacer" — se recalculan al re-ejecutar la fase que las alimenta
 * (Fase B para el gate de trust, Fase G para la validación final).
 * Visualmente usan el esquema azul-dashed igual que los pills del manual.
 */

/** Genera el HTML de una fila de gate con estado coloreado + explicación. */
function _cmv40GateRowHtml(status, title, result, explanation) {
  // status: 'ok' | 'warn' | 'ko' | 'pending'
  const icon = { ok: '✓', warn: '⚠', ko: '✗', pending: '○' }[status] || '·';
  const color = { ok: '#0e6b2a', warn: '#8a4a00', ko: '#b10b0b', pending: 'var(--text-3)' }[status] || 'var(--text-3)';
  const bg    = { ok: 'rgba(52,199,89,0.10)', warn: 'rgba(255,149,0,0.10)', ko: 'rgba(255,59,48,0.10)', pending: 'rgba(0,0,0,0.03)' }[status] || 'transparent';
  return `
    <div style="display:grid; grid-template-columns:24px 1fr; gap:10px; padding:10px 12px; background:${bg}; border-radius:6px; margin-bottom:6px">
      <div style="font-size:16px; font-weight:700; color:${color}; text-align:center">${icon}</div>
      <div>
        <div style="display:flex; gap:8px; align-items:baseline; flex-wrap:wrap">
          <span style="font-size:12px; font-weight:700; color:var(--text-1)">${escHtml(title)}</span>
          <span style="font-size:11px; color:${color}; font-weight:600">${escHtml(result)}</span>
        </div>
        <div style="font-size:11px; color:var(--text-2); line-height:1.5; margin-top:2px">${escHtml(explanation)}</div>
      </div>
    </div>`;
}

function _cmv40RenderGateCardBC(pid, s, isExpanded) {
  const curIdx   = CMV40_PHASES_ORDER.indexOf(s.phase);
  const bIdx     = CMV40_PHASES_ORDER.indexOf('target_provided');
  const hasData  = curIdx >= bIdx && (s.target_dv_info || s.target_trust_gates);
  const compatErr= !!s.compat_warning;
  const trustOk  = s.target_trust_ok === true;

  let overallIcon, overallLabel;
  if (compatErr) { overallIcon = '⛔'; overallLabel = 'Abortada · combinación incompatible'; }
  else if (!hasData) { overallIcon = '🔒'; overallLabel = 'Pendiente — se evalúa al cerrar Fase B'; }
  else if (trustOk) { overallIcon = '✅'; overallLabel = 'Trusted · todos los críticos pasan'; }
  else { overallIcon = '⚠️'; overallLabel = 'Sin trust automático · flujo completo manual'; }

  // Resumen en el header
  let summary;
  if (compatErr) summary = `Combinación ${s.source_workflow || '?'} + ${s.target_type || '?'} incompatible`;
  else if (!hasData) summary = 'Se evalúan al tener target — comparación con el RPU del Blu-ray';
  else if (trustOk) summary = 'Bin pre-validado: se saltan fases manuales (D/E)';
  else summary = 'Se ejecuta el flujo completo con revisión visual en Fase D';

  let body = '';
  if (isExpanded) {
    const rows = [];
    if (compatErr) {
      rows.push(_cmv40GateRowHtml('ko', 'Compatibilidad estructural',
        'abortada',
        s.compat_warning || 'Source y target estructuralmente incompatibles — la inyección produciría un MKV inválido.'));
    } else if (hasData) {
      const g = s.target_trust_gates || {};
      // Frames
      if (g.frames) {
        const ok = g.frames.ok;
        rows.push(_cmv40GateRowHtml(ok ? 'ok' : 'ko',
          'Número de frames',
          ok ? `coinciden · ${(g.frames.bd || 0).toLocaleString()} frames`
             : `${(g.frames.bd || 0).toLocaleString()} ≠ ${(g.frames.target || 0).toLocaleString()}`,
          ok
            ? 'Source y target tienen exactamente el mismo número de frames — condición crítica para que el RPU target se inyecte alineado escena a escena.'
            : 'Diferencia de frames ≠ 0. Suele indicar que el bin target es para otra edición (theatrical vs extended, streaming recortado). Requiere sync manual en Fase D/E o buscar el bin correcto.'));
      }
      // CM version
      if (g.cm_version) {
        const ok = g.cm_version.ok;
        rows.push(_cmv40GateRowHtml(ok ? 'ok' : 'ko',
          'CM version del target',
          ok ? `CM ${g.cm_version.value}` : `CM ${g.cm_version.value || '?'}`,
          ok
            ? 'El target está firmado como CMv4.0 — tiene los niveles nuevos (L3/L8-L11) que justifican el upgrade.'
            : 'El target no es CMv4.0. Sin CMv4.0 no hay upgrade posible — elige otro bin.'));
      }
      // L8
      if (g.has_l8) {
        const ok = g.has_l8.ok;
        rows.push(_cmv40GateRowHtml(ok ? 'ok' : 'ko',
          'Presencia de L8',
          ok ? 'L8 detectado' : 'L8 ausente',
          ok
            ? 'El bin contiene trims L8 auténticos — el nivel que aporta el tone-mapping fino de CMv4.0.'
            : 'Bin "CMv4.0 vacío" sin L8. No añade valor sobre el v2.9 original — rechazado.'));
      }
      // L5 divergence — usa el muestreo zoneado si se ejecutó (Fase B
      // refina con dovi_tool export cuando el static check supera el umbral).
      // En ese caso el `why` dinámico ya describe matches/mismatches por
      // zona; el resultado visual refleja el veredicto final (warn/ack/ok).
      if (g.l5_div) {
        const l5 = g.l5_div;
        const px = l5.px_max || 0;
        const sampled = l5.sampled_method === 'per_frame_zoned_24';
        let st, result, explanation;
        if (sampled) {
          const total = l5.sampled_total || 0;
          const matches = l5.sampled_matches || 0;
          const bodyCov = Math.round((l5.sampled_body_coverage || 0) * 100);
          const zm = l5.sampled_zone_mismatches || {};
          const sev = l5.severity || (l5.ok ? 'warn' : 'ack_required');
          // Estado visual: ok si todos coinciden, warn si pasa con muestreo,
          // ko si el muestreo confirmó divergencia legítima (ack_required).
          if (sev === 'ack_required') st = 'ko';
          else if (matches === total) st = 'ok';
          else st = 'warn';
          // Etiqueta corta: usa la métrica del muestreo en vez del Δpx
          // estático (que en variable L5 confunde).
          if (matches === total) {
            result = `${total}/${total} muestras coinciden`;
          } else if ((zm.body || 0) === 0) {
            result = `${matches}/${total} · cuerpo OK`;
          } else if (sev === 'ack_required') {
            result = `cuerpo solo ${bodyCov}% · ack`;
          } else {
            result = `cuerpo ${bodyCov}% · ${matches}/${total}`;
          }
          // El `why` ya trae el desglose completo intro/body/outro generado
          // en backend (_refine_l5_gate_with_sampling).
          explanation = l5.why || `Muestreo per-frame: ${matches}/${total} coinciden, cuerpo principal ${bodyCov}%.`;
        } else {
          // Sin muestreo: gate estático puro (≤30 px). Solo aplica cuando
          // el static check no necesitó refinamiento, así que st nunca es ko aquí.
          st = px <= 5 ? 'ok' : 'warn';
          result = px <= 5 ? `div ≤ 5 px` : `div ${px} px · warn`;
          explanation = st === 'ok'
            ? 'Los offsets de letterbox del target están a ≤5 px de los del BD — misma edición o recorte equivalente.'
            : 'Divergencia moderada del active area (5-30 px). Puede ser la misma edición con recorte ligeramente distinto. La app avanza pero conviene revisar visualmente.';
        }
        rows.push(_cmv40GateRowHtml(st, 'L5 — letterbox (active area)', result, explanation));
      }
      // L6 divergence
      if (g.l6_div) {
        const nits = Math.abs(g.l6_div.nits_diff || 0);
        const st = nits <= 50 ? 'ok' : 'warn';
        rows.push(_cmv40GateRowHtml(st,
          'L6 — MaxCLL/MaxFALL estático',
          `Δ ${g.l6_div.nits_diff} nits`,
          st === 'ok'
            ? 'La metadata HDR estática del target coincide (≤50 nits de diferencia) con la del BD.'
            : `Diferencia > 50 nits en L6. Sugiere que el target viene de un mastering con brillo global distinto. No bloquea pero el carácter de la imagen puede cambiar.`));
      }
      // L1 divergence
      if (g.l1_div) {
        const pct = Math.abs(g.l1_div.pct_diff || 0);
        const st = pct <= 5 ? 'ok' : 'warn';
        rows.push(_cmv40GateRowHtml(st,
          'L1 — MaxCLL dinámico por escena',
          `Δ ${g.l1_div.pct_diff}%`,
          st === 'ok'
            ? 'El promedio de brillo escena-a-escena coincide (≤5% de diferencia) — el grading es comparable.'
            : 'Diferencia > 5% en el promedio de brillo por escena. Sugiere color grading distinto entre el target y el BD. Avanza pero el resultado puede verse diferente al original.'));
      }
    }
    body = `
      <div class="section-body">
        <div style="font-size:12px; color:var(--text-2); line-height:1.5; margin-bottom:10px">
          <strong>Qué se valida aquí:</strong> al cerrar Fase B, la app compara automáticamente el RPU target con el RPU del Blu-ray para decidir si puede saltar las fases manuales (D/E) o si hace falta revisión visual. Las validaciones críticas además aseguran que la combinación source × target no producirá un MKV roto.
        </div>
        ${rows.join('') || '<div style="font-size:12px; color:var(--text-3); font-style:italic">Aún sin datos — completa Fase B primero.</div>'}
      </div>`;
  }

  return `
    <div class="section-card cmv40-gate-card" style="margin-top:12px; border-left:3px solid rgba(0,122,255,0.55)">
      <div class="section-header cmv40-fase-header" onclick="_cmv40TogglePhase('${pid}','GATE_BC')" style="cursor:pointer">
        <div class="cmv40-fase-state-icon" style="font-size:20px">${overallIcon}</div>
        <div style="flex:1">
          <div class="section-title" style="color:#0a5cab">🛡️ Validaciones — trust gates + compatibilidad</div>
          <div class="section-subtitle">${escHtml(overallLabel)} · ${escHtml(summary)}</div>
        </div>
        <div class="cmv40-fase-chevron">${isExpanded ? '▾' : '▸'}</div>
      </div>
      ${body}
    </div>`;
}

function _cmv40RenderGateCardGH(pid, s, isExpanded) {
  const curIdx = CMV40_PHASES_ORDER.indexOf(s.phase);
  const remuxedIdx = CMV40_PHASES_ORDER.indexOf('remuxed');
  const validatedIdx = CMV40_PHASES_ORDER.indexOf('validated');
  const state = curIdx < remuxedIdx ? 'pending'
             : curIdx === remuxedIdx ? 'running'
             : curIdx >= validatedIdx ? 'done'
             : 'pending';

  let overallIcon, overallLabel, summary;
  if (state === 'done') {
    overallIcon = '✅';
    overallLabel = 'Validación final OK';
    summary = 'El MKV contiene CMv4.0, el profile es correcto y el frame count coincide';
  } else if (state === 'running') {
    overallIcon = '⏳';
    overallLabel = 'Validación en curso…';
    summary = 'Verificando profile + CM v4.0 + frame count del HEVC pre-mux';
  } else {
    overallIcon = '🔒';
    overallLabel = 'Pendiente';
    summary = 'Se ejecuta tras completar Fase G (remux)';
  }

  let body = '';
  if (isExpanded) {
    const rows = [];
    // Profile
    const targetProfile = s.source_dv_info?.profile || '?';
    rows.push(_cmv40GateRowHtml(state === 'done' ? 'ok' : 'pending',
      'Profile del HEVC resultante',
      state === 'done' ? `Profile ${targetProfile}` : '—',
      state === 'done'
        ? 'El MKV final tiene el profile DV esperado según el workflow elegido (P7 FEL si source era FEL, P8.1 single-layer si source era MEL/P8).'
        : 'Se verifica que el profile coincide con el esperado al completar Fase G.'));
    // CM version
    rows.push(_cmv40GateRowHtml(state === 'done' ? 'ok' : 'pending',
      'CM version del MKV',
      state === 'done' ? 'CM v4.0 confirmado' : '—',
      state === 'done'
        ? 'dovi_tool extract-rpu + info sobre el HEVC pre-mux confirma CMv4.0 en el RPU del MKV resultante.'
        : 'Se verifica que el RPU del MKV final reporta CMv4.0.'));
    // Frame count
    rows.push(_cmv40GateRowHtml(state === 'done' ? 'ok' : 'pending',
      'Frame count',
      state === 'done' ? `${(s.source_frame_count || 0).toLocaleString()} frames` : '—',
      state === 'done'
        ? 'El número de frames del MKV resultante coincide con el del Blu-ray origen — sin inserciones ni recortes accidentales.'
        : 'Se compara frame count del resultado contra el del source.'));
    // Estructura MKV
    rows.push(_cmv40GateRowHtml(state === 'done' ? 'ok' : 'pending',
      'Estructura Matroska',
      state === 'done' ? 'MKV válido · mkvmerge -J OK' : '—',
      state === 'done'
        ? 'mkvmerge -J lee el fichero sin errores: audio, subs, capítulos y pista de vídeo con RPU NAL units correctamente ensamblados.'
        : 'Se verifica que el contenedor MKV es estructuralmente correcto.'));

    body = `
      <div class="section-body">
        <div style="font-size:12px; color:var(--text-2); line-height:1.5; margin-bottom:10px">
          <strong>Qué se valida aquí:</strong> antes de mover el MKV al directorio de salida, la app verifica que el resultado es estructuralmente correcto y que el upgrade a CMv4.0 se ha materializado en el fichero. Si algo falla, el proyecto queda en error y puedes rehacer desde la fase que prefieras.
        </div>
        ${rows.join('')}
      </div>`;
  }

  return `
    <div class="section-card cmv40-gate-card" style="margin-top:12px; border-left:3px solid rgba(0,122,255,0.55)">
      <div class="section-header cmv40-fase-header" onclick="_cmv40TogglePhase('${pid}','GATE_GH')" style="cursor:pointer">
        <div class="cmv40-fase-state-icon" style="font-size:20px">${overallIcon}</div>
        <div style="flex:1">
          <div class="section-title" style="color:#0a5cab">🛡️ Validación final pre-finalizar</div>
          <div class="section-subtitle">${escHtml(overallLabel)} · ${escHtml(summary)}</div>
        </div>
        <div class="cmv40-fase-chevron">${isExpanded ? '▾' : '▸'}</div>
      </div>
      ${body}
    </div>`;
}

function _cmv40TogglePhase(pid, key) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  if (!project.expandedPhases) project.expandedPhases = {};
  // Gates son pseudo-fases — toggle directo sin consultar CMV40_FASES_DEF
  if (key === 'GATE_BC' || key === 'GATE_GH') {
    const current = project.expandedPhases[key] !== undefined
      ? project.expandedPhases[key]
      : (key === 'GATE_BC');   // BC abierto por defecto, GH cerrado
    project.expandedPhases[key] = !current;
    _updateCMv40Panel(project);
    return;
  }
  const fase = CMV40_FASES_DEF.find(f => f.key === key);
  const state = _cmv40PhaseState(project.session.phase, fase.produces, fase.startsFrom);
  const current = project.expandedPhases[key] !== undefined
    ? project.expandedPhases[key]
    : (state === 'active');
  project.expandedPhases[key] = !current;
  _updateCMv40Panel(project);
}

// Label amigable del target_type + panel de gates con resultado visual
const _CMV40_TARGET_TYPE_LABELS = {
  'generic':               { icon: '🔧', label: 'Target genérico',             desc: 'Flujo completo: merge CMv4.0 + revisión visual en Fase D' },
  'trusted_p8_source':     { icon: '📦', label: 'Target P8 + CMv4.0 (trusted)', desc: 'Bin pre-validado (rama B): skip Fase D si gates OK' },
  'trusted_p7_fel_final':  { icon: '🎯', label: 'Target P7 FEL CMv4.0 final',   desc: 'Drop-in: skip merge en Fase F + skip Fase D si gates OK' },
  'trusted_p7_mel_final':  { icon: '🎯', label: 'Target P7 MEL CMv4.0 final',   desc: 'Drop-in MEL: skip Fase D si gates OK' },
  'incompatible':          { icon: '❌', label: 'Target incompatible',          desc: 'Sin CMv4.0 — no sirve como fuente de transfer' },
};

function _cmv40RenderTrustPanel(s) {
  const tt = s.target_type || 'generic';
  const meta = _CMV40_TARGET_TYPE_LABELS[tt] || _CMV40_TARGET_TYPE_LABELS.generic;
  const gates = s.target_trust_gates || {};
  const trustOk = s.target_trust_ok === true;
  const isTrusted = tt !== 'generic' && tt !== 'incompatible';
  const isIncompat = tt === 'incompatible';

  let cls = 'generic';
  if (isIncompat) cls = 'ko';
  else if (isTrusted && trustOk) cls = 'ok';
  else if (isTrusted && !trustOk) cls = 'warn';

  // Renderizar cada gate con status visual
  const gateItems = [];
  const pushGate = (key, okText, failText, detail) => {
    const g = gates[key];
    if (!g) return;
    const gClass = g.ok ? 'pass' : (g.critical ? 'fail' : 'soft');
    const txt = g.ok ? okText : failText;
    gateItems.push(`<span class="cmv40-trust-gate ${gClass}" data-tooltip="${escHtml(detail)}">
      ${g.ok ? '✓' : '✗'} ${escHtml(txt)}
    </span>`);
  };
  if (gates.frames) {
    pushGate('frames',
      `frames ${gates.frames.bd?.toLocaleString() || '?'}`,
      `frames ${gates.frames.bd?.toLocaleString()} ≠ ${gates.frames.target?.toLocaleString()}`,
      'Frame count del BD vs target — crítico'
    );
  }
  if (gates.cm_version) {
    pushGate('cm_version',
      `CM ${gates.cm_version.value}`,
      `CM ${gates.cm_version.value || '?'}`,
      'Debe ser v4.0 para ser fuente de transfer — crítico'
    );
  }
  if (gates.has_l8) {
    pushGate('has_l8', 'L8 presente', 'sin L8',
      'L8 = trims CMv4.0 — sin L8 no hay transfer útil'
    );
  }
  if (gates.l5_div) {
    const l5 = gates.l5_div;
    // Si el muestreo per-frame con zonas se ejecutó, prioriza esa narrativa
    // (matches por zona) sobre el Δpx estático del summary — en pelis con
    // L5 variable este último engaña.
    const sampled = l5.sampled_method === 'per_frame_zoned_24';
    let okText, failText, tip;
    if (sampled) {
      const total = l5.sampled_total || 0;
      const matches = l5.sampled_matches || 0;
      const bodyCov = Math.round((l5.sampled_body_coverage || 0) * 100);
      const zm = l5.sampled_zone_mismatches || {};
      // Etiqueta corta con la métrica clave: matches del cuerpo principal
      if (matches === total) {
        okText = `L5 ${matches}/${total} idéntico`;
      } else if ((zm.body || 0) === 0) {
        okText = `L5 cuerpo OK · ${matches}/${total}`;
      } else if (l5.ok) {
        okText = `L5 cuerpo ${bodyCov}% · ${matches}/${total}`;
      } else {
        okText = `L5 cuerpo solo ${bodyCov}%`;
      }
      failText = okText;
      tip = l5.why || `Muestreo per-frame con zonas (intro/body/outro).`;
    } else {
      okText = `L5 div ${l5.px_max}px`;
      failText = `L5 div ${l5.px_max}px (>30)`;
      tip = l5.why || `Divergencia L5 (active area). ≤5 ok · 5-30 warn · >30 aborta — posible edición distinta del disco`;
    }
    pushGate('l5_div', okText, failText, tip);
  }
  if (gates.l6_div) {
    pushGate('l6_div',
      `L6 Δ${gates.l6_div.nits_diff}n`,
      `L6 Δ${gates.l6_div.nits_diff}n (>50)`,
      'Divergencia L6 MaxCLL — soft warn si >50 nits'
    );
  }
  if (gates.l1_div) {
    pushGate('l1_div',
      `L1 Δ${gates.l1_div.pct_diff}%`,
      `L1 Δ${gates.l1_div.pct_diff}% (>5%)`,
      'Divergencia L1 MaxCLL en % — soft warn si >5%'
    );
  }

  const statusTxt = isIncompat
    ? 'Incompatible — no sirve como target'
    : isTrusted && trustOk ? 'TRUSTED — se saltarán pasos manuales'
    : isTrusted && !trustOk ? 'Trust NO aprobado — flujo completo con revisión manual'
    : 'Flujo estándar con merge + revisión';

  return `
    <div class="cmv40-trust-panel ${cls}" style="margin-top:10px">
      <div class="cmv40-trust-header">
        <span>${meta.icon}</span>
        <span>${escHtml(meta.label)}</span>
        <span class="cmv40-trust-status">${escHtml(statusTxt)}</span>
      </div>
      <div class="cmv40-trust-desc">${escHtml(meta.desc)}</div>
      ${gateItems.length ? `<div class="cmv40-trust-gates">${gateItems.join('')}</div>` : ''}
    </div>`;
}

function _cmv40FaseSummary(key, s) {
  const arts = s.artifacts || {};
  if (key === 'A' && s.source_dv_info) {
    const d = s.source_dv_info;
    return `Profile ${d.profile}${d.el_type ? ` (${d.el_type})` : ''} · CM ${d.cm_version} · ${s.source_frame_count.toLocaleString()} frames`;
  }
  if (key === 'B' && s.target_dv_info) {
    const d = s.target_dv_info;
    return `CM ${d.cm_version} · ${s.target_frame_count.toLocaleString()} frames (Δ ${s.sync_delta > 0 ? '+' : ''}${s.sync_delta})`;
  }
  if (key === 'C') {
    const sizes = ['BL.hevc', 'EL.hevc', 'per_frame_data.json'].map(n => arts[n] || 0);
    const total = sizes.reduce((a, b) => a + b, 0);
    return total > 0 ? `BL.hevc, EL.hevc y per_frame_data (${_fmtBytes(total)} total)` : 'BL.hevc, EL.hevc y datos per-frame generados';
  }
  if (key === 'D') {
    const trustedSkipped = !!s.target_trust_ok
                            && (s.trust_override || 'auto') !== 'force_interactive';
    if (trustedSkipped) return 'Omitida — target trusted: sync validado por gates';
    return s.sync_config ? `Corrección aplicada (Δ = ${s.sync_delta})` : 'Sincronización verificada (Δ = 0)';
  }
  if (key === 'F') {
    // En drop-in FEL el artefacto es source_injected.hevc (BL+EL intactos);
    // en merge clasico es EL_injected.hevc (solo EL). Preferimos el que exista.
    const dropIn = arts['source_injected.hevc'];
    const merge  = arts['EL_injected.hevc'];
    if (dropIn) return `source_injected.hevc generado (${_fmtBytes(dropIn)}, drop-in)`;
    if (merge)  return `EL_injected.hevc generado (${_fmtBytes(merge)})`;
    return 'HEVC con RPU inyectado generado';
  }
  if (key === 'G') {
    // El MKV se escribe en /mnt/output (fuera del workdir) por lo que no sale
    // del scan de artifacts. Mostramos el nombre directo del session.
    const name = s.output_mkv_name || '';
    return name ? `MKV remuxado: ${name} (pre-validación)` : 'MKV remuxado (pre-validación)';
  }
  if (key === 'H') return s.output_mkv_path ? `Movido a: ${s.output_mkv_path}` : 'Validado';
  return '';
}

function _cmv40FaseBody(key, pid, s) {
  if (key === 'A') return _cmv40FaseABody(pid, s);
  if (key === 'B') return _cmv40FaseBBody(pid, s);
  if (key === 'C') return _cmv40FaseCBody(pid, s);
  if (key === 'D') return _cmv40FaseDBody(pid, s);
  if (key === 'F') return _cmv40FaseFBody(pid, s);
  if (key === 'G') return _cmv40FaseGBody(pid, s);
  if (key === 'H') return _cmv40FaseHBody(pid, s);
  return '';
}

function _cmv40FaseDoneBody(key, pid, s) {
  // Contenido "modo lectura" cuando la fase está completada
  if (key === 'A' && s.source_dv_info) {
    const d = s.source_dv_info;
    return `
      <div style="font-size:12px; line-height:1.8">
        <div><span style="color:var(--text-3)">Profile:</span> ${d.profile}${d.el_type ? ` (${d.el_type})` : ''}</div>
        <div><span style="color:var(--text-3)">CM version:</span> ${d.cm_version}</div>
        <div><span style="color:var(--text-3)">Frames:</span> ${s.source_frame_count.toLocaleString()}</div>
        ${d.has_l1 ? '<div><span style="color:var(--text-3)">Metadata:</span> L1 L2 L5 L6</div>' : ''}
      </div>`;
  }
  if (key === 'B' && s.target_dv_info) {
    const d = s.target_dv_info;
    const srcType = s.target_rpu_source === 'drive' ? 'Repo DoviTools'
                   : s.target_rpu_source === 'mkv' ? 'Extraído de otro MKV'
                   : 'Carpeta NAS';
    const shortHash = s.target_rpu_sha256 ? s.target_rpu_sha256.slice(0, 12) : '';
    const hashLine = shortHash
      ? `<div><span style="color:var(--text-3)">SHA-256:</span> <code title="${escHtml(s.target_rpu_sha256)}" style="font-size:11px">${shortHash}…</code></div>`
      : '';
    // NO incluimos _cmv40RenderTrustPanel aqui — los gates tienen su propia
    // tarjeta dedicada (🛡️ Validaciones) que aparece justo debajo de Fase B.
    // Mostrarlo aqui ademas duplicaba la informacion.
    return `
      <div style="font-size:12px; line-height:1.8">
        <div><span style="color:var(--text-3)">Fuente:</span> ${srcType}</div>
        <div><span style="color:var(--text-3)">Path:</span> <code>${escHtml(s.target_rpu_path || '—')}</code></div>
        ${hashLine}
        <div><span style="color:var(--text-3)">CM version:</span> ${d.cm_version}</div>
        <div><span style="color:var(--text-3)">Frames:</span> ${s.target_frame_count.toLocaleString()}</div>
        <div><span style="color:var(--text-3)">Δ vs origen:</span> <b style="color:${s.sync_delta === 0 ? 'var(--green)' : 'var(--orange)'}">${s.sync_delta > 0 ? '+' : ''}${s.sync_delta} frames</b></div>
        <div style="margin-top:8px; font-size:11px; color:var(--text-3); font-style:italic">💡 Los resultados de los trust gates se muestran en la tarjeta 🛡️ Validaciones de abajo.</div>
      </div>`;
  }
  // Fase D completada — dos casuísticas:
  //   (1) target trusted + auto → NUNCA se generó per_frame_data.json →
  //       mostrar banner "omitida" en vez de canvas vacío (que se veía negro).
  //   (2) revisión visual real (non-trusted, o trust_override=force_interactive)
  //       → el plot existe; mostrar chart + stats + controles de navegación
  //       (zoom + frame range) en modo read-only.
  if (key === 'D') {
    const trustedSkipped = s.target_trust_ok
      && (s.trust_override || 'auto') !== 'force_interactive';
    if (trustedSkipped) {
      // Sin trust panel aqui — la tarjeta 🛡️ Validaciones arriba ya lo muestra.
      return `
        <div class="banner success" style="margin-bottom:10px">
          <span class="banner-icon">✓</span>
          <span>Fase D omitida — el bin target pasó los trust gates (frames, L5, L6, L8) y no se generó <code>per_frame_data.json</code>. Sin revisión visual necesaria en el auto-pipeline.</span>
        </div>
        <div style="font-size:11px; color:var(--text-3); font-style:italic; margin-top:6px">💡 Los resultados de los gates están en la tarjeta 🛡️ Validaciones justo tras Fase B.</div>`;
    }
    const syncConfigHtml = s.sync_config
      ? `<div style="margin-bottom:10px; font-size:12px">
          <span style="color:var(--text-3)">Corrección aplicada:</span>
          <pre style="margin-top:6px; font-size:11px; background:var(--surface-2); padding:8px; border-radius:4px">${escHtml(JSON.stringify(s.sync_config, null, 2))}</pre>
        </div>`
      : '<div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Sincronización confirmada sin corrección.</div>';
    return `
      ${syncConfigHtml}
      <div style="font-size:11px; color:var(--text-3); margin-bottom:8px">
        Navegación por el gráfico en solo lectura — la corrección ya está aplicada.
      </div>
      <div id="cmv40-sync-stats-${pid}" class="cmv40-sync-stats"></div>
      <div id="cmv40-chart-wrap-${pid}" class="cmv40-chart-wrap">
        <canvas id="cmv40-chart-${pid}" width="1000" height="280"></canvas>
        <div class="cmv40-chart-tooltip" id="cmv40-chart-tooltip-${pid}" style="display:none"></div>
      </div>
      <div class="cmv40-sync-controls" id="cmv40-sync-controls-${pid}"></div>
      <div id="cmv40-confidence-${pid}"></div>`;
  }
  if (key === 'H' && s.output_mkv_path) {
    return `<div style="font-size:12px"><span style="color:var(--text-3)">MKV final:</span> <code>${escHtml(s.output_mkv_path)}</code></div>`;
  }
  // Fase C: mostrar artefactos generados (BL.hevc, EL.hevc, per_frame_data.json)
  if (key === 'C') {
    return _cmv40ArtifactsBody(s, ['BL.hevc', 'EL.hevc', 'per_frame_data.json']);
  }
  // Fase F: drop-in FEL genera source_injected.hevc (BL+EL intactos);
  // merge clasico genera EL_injected.hevc (solo EL). Tras validacion exitosa
  // (Fase H) el pipeline borra ambos ficheros — ya no son necesarios, el MKV
  // final los contiene. Distinguimos 3 casos:
  //   (a) artifact existe: mostramos size
  //   (b) artifact no existe Y pipeline ya termino: mensaje de cleanup ok
  //   (c) artifact no existe y pipeline a medias: "no encontrado" (bug)
  if (key === 'F') {
    const arts = s.artifacts || {};
    const hasDropIn = arts['source_injected.hevc'] !== undefined;
    const hasMerge  = arts['EL_injected.hevc']     !== undefined;
    if (hasDropIn) return _cmv40ArtifactsBody(s, ['source_injected.hevc']);
    if (hasMerge)  return _cmv40ArtifactsBody(s, ['EL_injected.hevc']);
    // Nada encontrado: decidimos segun fase global
    const cleaned = ['validated', 'done'].includes(s.phase) || s.archived;
    const wf = (s.workflow || s.source_workflow || '').toLowerCase();
    const phSkipped = s.phases_skipped || [];
    const isDropIn = phSkipped.includes('merge_cmv40_transfer') || wf === 'p7_fel';
    const name = isDropIn ? 'source_injected.hevc' : 'EL_injected.hevc';
    if (cleaned) {
      return `
        <div style="font-size:12px">
          <div style="color:var(--text-3); margin-bottom:6px">Artefactos generados:</div>
          <div style="display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px dashed var(--sep); opacity:0.7">
            <code style="font-size:11px">${escHtml(name)}</code>
            <span style="font-size:11px; color:var(--text-3)">consumido tras validación</span>
          </div>
          <div style="font-size:11px; color:var(--text-3); margin-top:6px; line-height:1.4">
            El HEVC intermedio se borra automáticamente en Fase H una vez validado el MKV final — el resultado vive ahora en <code>/mnt/output</code>.
          </div>
        </div>`;
    }
    return _cmv40ArtifactsBody(s, [name]);
  }
  // Fase G: el MKV final se escribe en /mnt/output/{nombre}.mkv.tmp (fuera del
  // workdir), por eso no aparece en artifacts. Mostramos directamente el path.
  if (key === 'G') {
    const path = s.output_mkv_path || '';
    const name = s.output_mkv_name || (path ? path.split('/').pop() : '');
    if (!name) return '<div style="font-size:11px; color:var(--text-3)">—</div>';
    return `
      <div style="font-size:12px">
        <div style="color:var(--text-3); margin-bottom:6px">MKV remuxado (pre-validación Fase H):</div>
        <div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px dashed var(--sep); gap:8px">
          <code style="font-size:11px; word-break:break-all">${escHtml(name)}</code>
          <span style="font-size:11px; color:var(--text-3); white-space:nowrap">escrito en /mnt/output</span>
        </div>
        <div style="font-size:11px; color:var(--text-3); margin-top:6px; line-height:1.4">
          Sufijo <code>.mkv.tmp</code> mientras Fase H no valide. Tras validar se hace rename atómico al nombre final.
        </div>
      </div>`;
  }
  return '<div style="font-size:11px; color:var(--text-3)">—</div>';
}

function _cmv40ArtifactsBody(s, fileNames) {
  const arts = s.artifacts || {};
  const rows = fileNames.map(name => {
    const size = arts[name];
    if (size !== undefined) {
      return `<div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px dashed var(--sep)">
        <code style="font-size:11px">${escHtml(name)}</code>
        <span style="font-size:11px; color:var(--text-3)">${_fmtBytes(size)}</span>
      </div>`;
    }
    return `<div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px dashed var(--sep); opacity:0.5">
      <code style="font-size:11px">${escHtml(name)}</code>
      <span style="font-size:11px; color:var(--text-3)">no encontrado</span>
    </div>`;
  }).join('');
  const total = fileNames.reduce((acc, n) => acc + (arts[n] || 0), 0);
  return `
    <div style="font-size:12px">
      <div style="color:var(--text-3); margin-bottom:6px">Artefactos generados:</div>
      ${rows}
      ${total > 0 ? `<div style="margin-top:6px; font-size:11px; color:var(--text-3); text-align:right">Total: <b>${_fmtBytes(total)}</b></div>` : ''}
    </div>`;
}

async function _cmv40ClearError(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}/clear-error`, { method: 'POST' });
  if (data) {
    const project = openCMv40Projects.find(p => p.id === pid);
    if (project) {
      _cmv40AssignSession(project, data);
      _updateCMv40Panel(project);
    }
  }
}

/** Botón "🔄 Reintentar" del banner de error: descarta el error_message y
 *  dispara la fase active actual. Mapeado por key (A..H) → función do*. */
async function _cmv40RetryActivePhase(pid, faseKey) {
  await apiFetch(`/api/cmv40/${pid}/clear-error`, { method: 'POST' });
  const launcher = {
    A: () => cmv40DoAnalyzeSource(pid),
    F: () => cmv40DoInject(pid),
    G: () => cmv40DoRemux(pid),
    H: () => cmv40DoValidate(pid),
  }[faseKey];
  if (launcher) {
    launcher();
  } else {
    // Las fases B (target), C (extract) y D (sync) tienen flujos manuales —
    // refrescamos el panel para que el usuario vea la card active expandida.
    const data = await apiFetch(`/api/cmv40/${pid}`, { silent: true });
    if (data) {
      const project = openCMv40Projects.find(p => p.id === pid);
      if (project) {
        _cmv40AssignSession(project, data);
        _updateCMv40Panel(project);
      }
    }
  }
}

async function _cmv40Redo(pid, targetPhase, faseKey) {
  // Consultar qué artefactos se borrarán
  const preview = await apiFetch(`/api/cmv40/${pid}/reset-preview/${targetPhase}`);

  let artifactsList = '';
  if (preview?.files?.length) {
    const rows = preview.files.map(f =>
      `<li style="font-family:monospace; font-size:11px">${escHtml(f.name)} <span style="color:var(--text-3)">(${_fmtBytes(f.size_bytes)})</span></li>`
    ).join('');
    artifactsList = `
      <div style="margin-top:10px; padding:10px; background:var(--surface-2); border-radius:var(--r-sm); max-height:180px; overflow-y:auto">
        <div style="font-size:11px; color:var(--text-2); margin-bottom:6px">
          <b>Se borrarán ${preview.files.length} artefacto(s)</b> — ${_fmtBytes(preview.total_bytes)} liberados:
        </div>
        <ul style="margin:0; padding-left:18px">${rows}</ul>
      </div>`;
  } else {
    artifactsList = '<div style="font-size:11px; color:var(--text-3); margin-top:8px">No hay artefactos posteriores que borrar.</div>';
  }

  // Uso el modal cmv40-confirm-modal que acepta HTML en el body
  document.getElementById('cmv40-confirm-title').textContent = '¿Rehacer esta fase?';
  document.getElementById('cmv40-confirm-sub').textContent = 'La sesión volverá al estado previo. Las fases posteriores se invalidarán y sus artefactos se borrarán del disco.';
  document.getElementById('cmv40-confirm-body').innerHTML = artifactsList;
  const confirmBtn = document.getElementById('cmv40-confirm-btn');
  confirmBtn.textContent = 'Rehacer y borrar artefactos';
  confirmBtn.className = 'btn btn-danger btn-sm';
  const newBtn = confirmBtn.cloneNode(true);
  confirmBtn.parentNode.replaceChild(newBtn, confirmBtn);
  newBtn.addEventListener('click', async () => {
    closeModal('cmv40-confirm-modal');
    const data = await apiFetch(`/api/cmv40/${pid}/reset-to/${targetPhase}`, { method: 'POST' });
    if (data) {
      const project = openCMv40Projects.find(p => p.id === pid);
      if (project) {
        _cmv40AssignSession(project, data);
        if (!project.expandedPhases) project.expandedPhases = {};
        project.expandedPhases[faseKey] = true;
        project.syncData = null;
        // Tras reset invalidamos el dedup del orquestador, el timer del
        // overlay y el flag de bridging. El reset NO dispara ninguna fase
        // automaticamente — el lanzamiento es siempre manual. Si el usuario
        // tiene auto=ON y lanza la fase manualmente, las siguientes se
        // encadenaran al terminar esa.
        project._lastAutoFiredFor = null;
        project._lastAutoFiredAt = 0;
        project._pipelineStartMs = null;
        project._resolvedStartedMs = null;
        project._autoChaining = false;
        _updateCMv40Panel(project);
      }
      refreshCMv40Sidebar();
      showToast(`Fase ${faseKey} lista para rehacer`, 'info');
    }
  });
  openModal('cmv40-confirm-modal');
}

// ── Tarjetas por fase ────────────────────────────────────────────

function _cmv40FaseABody(pid, s) {
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Extrae el stream HEVC y el RPU del MKV origen. Tarda 2-5 minutos.</div>
      <button class="btn btn-primary btn-md" onclick="cmv40DoAnalyzeSource('${pid}')">🔍 Analizar origen</button>
    </div>`;
}

function _cmv40FaseBBody(pid, s) {
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Elige una fuente del RPU CMv4.0 a inyectar.</div>
      <div class="cmv40-tab-switcher">
        <button class="cmv40-tab-btn active" id="cmv40-tab-btn-repo-${pid}"
          onclick="_cmv40SwitchTargetTab('${pid}','repo')">📦 Repo DoviTools</button>
        <button class="cmv40-tab-btn" id="cmv40-tab-btn-mkv-${pid}"
          onclick="_cmv40SwitchTargetTab('${pid}','mkv')">🎬 Extraer de otro MKV</button>
        <button class="cmv40-tab-btn" id="cmv40-tab-btn-path-${pid}"
          onclick="_cmv40SwitchTargetTab('${pid}','path')">📂 Carpeta NAS</button>
      </div>

      <div id="cmv40-target-repo-${pid}" class="cmv40-target-tab">
        <div id="cmv40-repo-info-${pid}" style="font-size:12px;color:var(--text-3);margin-bottom:8px">— Cargando candidatos del repositorio… —</div>
        <div id="cmv40-repo-list-${pid}" class="cmv40-repo-list" style="max-height:280px;overflow-y:auto"></div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:12px">
          <button class="btn btn-primary btn-md" onclick="cmv40DoTargetFromDrive('${pid}')">⬇ Descargar y usar</button>
          <button class="btn btn-secondary btn-sm" onclick="_cmv40LoadRepoForPanel('${pid}')">↺ Refrescar</button>
        </div>
      </div>

      <div id="cmv40-target-path-${pid}" class="cmv40-target-tab" style="display:none">
        <label class="modal-field-label">RPU disponible en /mnt/cmv40_rpus/</label>
        <div class="iso-select-row">
          <select id="cmv40-rpu-select-${pid}" class="iso-select">
            <option value="">— Cargando… —</option>
          </select>
          <button class="btn btn-secondary btn-sm" onclick="_cmv40LoadRpus('${pid}')">↺</button>
        </div>
        <button class="btn btn-primary btn-md" style="margin-top:12px" onclick="cmv40DoTargetFromPath('${pid}')">✓ Usar este RPU</button>
      </div>

      <div id="cmv40-target-mkv-${pid}" class="cmv40-target-tab" style="display:none">
        <label class="modal-field-label">MKV que ya tiene CMv4.0</label>
        <div class="iso-select-row">
          <select id="cmv40-target-mkv-select-${pid}" class="iso-select">
            <option value="">— Cargando… —</option>
          </select>
          <button class="btn btn-secondary btn-sm" onclick="_cmv40LoadTargetMkvs('${pid}')">↺</button>
        </div>
        <button class="btn btn-primary btn-md" style="margin-top:12px" onclick="cmv40DoTargetFromMkv('${pid}')">✂️ Extraer RPU del MKV</button>
      </div>
    </div>`;
}

function _cmv40FaseCBody(pid, s) {
  // El warning del Δ frames debe matizar que Fase D puede omitirse si los
  // trust gates aprobaron alineación (no siempre habrá "revisión visual").
  const trust = !!s.target_trust_ok && s.trust_override !== 'force_interactive';
  const deltaNote = trust
    ? 'Los trust gates ya validaron la alineación; la diferencia se considera tolerable y Fase D se omitirá.'
    : 'Se evaluará en Fase D (chart de sincronización) — podrás aplicar corrección si hace falta.';
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Separa el HEVC en BL (Capa Base) + EL (Capa de Mejora) y extrae datos de luminancia por frame para el chart de sincronización. Tarda 5-15 min.</div>
      ${s.sync_delta !== 0 ? `<div class="banner warning" style="margin-bottom:10px"><span class="banner-icon">⚠️</span><span>Diferencia de frames detectada (Δ = ${s.sync_delta > 0 ? '+' : ''}${s.sync_delta}). ${deltaNote}</span></div>` : ''}
      <button class="btn btn-primary btn-md" onclick="cmv40DoExtract('${pid}')">✂️ Extraer BL/EL + per-frame data</button>
    </div>`;
}

function _cmv40FaseDBody(pid, s) {
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Chart de MaxPQ (L1 del RPU Dolby Vision) por frame. Rojo = origen, Azul = target. Las curvas deben coincidir en forma; si hay offset detectable, se aplica corrección con dovi_tool editor.</div>
      <div id="cmv40-sync-stats-${pid}" class="cmv40-sync-stats"></div>
      <div id="cmv40-chart-wrap-${pid}" class="cmv40-chart-wrap">
        <canvas id="cmv40-chart-${pid}" width="1000" height="320"></canvas>
        <div class="cmv40-chart-tooltip" id="cmv40-chart-tooltip-${pid}" style="display:none"></div>
      </div>
      <div class="cmv40-sync-controls" id="cmv40-sync-controls-${pid}"></div>
      <div id="cmv40-confidence-${pid}"></div>
    </div>`;
}

function _cmv40FaseFBody(pid, s) {
  // Texto dinámico según workflow y target_type (igual estrategia que el
  // sidebar de la timeline). El banner "verifica el gráfico" solo aplica si
  // Fase D fue ejecutada visualmente — con trust_ok o ack se omite.
  const trust = !!s.target_trust_ok && s.trust_override !== 'force_interactive';
  const wf = s.source_workflow || 'p7_fel';
  const dropIn = trust && s.target_type === 'trusted_p7_fel_final' && wf === 'p7_fel';
  const targetNeedsMerge = ['trusted_p7_fel_final', 'trusted_p7_mel_final', 'generic'].includes(s.target_type);
  const userAcked = !!s.user_acknowledged_degradation;
  const faseDExecutedVisually = !trust && !userAcked;
  let desc;
  if (dropIn) {
    desc = 'Inyecta el RPU del bin directamente sobre source.hevc (BL+EL juntos, sin merge ni mux posterior). Vía más rápida — el byte-identical del RPU queda garantizado.';
  } else if (wf === 'p7_fel') {
    desc = 'Merge CMv4.0 sobre el RPU P7 del source + inyecta el RPU merged en EL.hevc preservando la FEL.';
  } else if (wf === 'p7_mel') {
    desc = targetNeedsMerge
      ? 'Merge CMv4.0 sobre el RPU P7 MEL del source + inyecta el RPU merged en BL.hevc (descarta el EL MEL → P8.1 CMv4.0).'
      : 'Inyecta el RPU target directamente en BL.hevc (target P8 retail, sin merge — descarta el EL MEL → P8.1).';
  } else {  // p8
    desc = targetNeedsMerge
      ? 'Merge CMv4.0 sobre el RPU P8 del source + inyecta el RPU merged en source.hevc.'
      : 'Inyecta el RPU target directamente en source.hevc (target P8 retail, sin merge — reemplaza el RPU CMv2.9 existente).';
  }
  const reviewBanner = faseDExecutedVisually
    ? '<div class="banner info" style="margin-bottom:10px"><span class="banner-icon">ℹ️</span><span>Verifica en el chart de Fase D que las curvas coinciden antes de inyectar.</span></div>'
    : '';
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">${escHtml(desc)}</div>
      ${reviewBanner}
      <button class="btn btn-primary btn-md" onclick="cmv40DoInject('${pid}')">💉 Inyectar RPU</button>
    </div>`;
}

function _cmv40FaseGBody(pid, s) {
  // Texto dinámico según workflow + drop-in. Misma lógica que el sidebar.
  const trust = !!s.target_trust_ok && s.trust_override !== 'force_interactive';
  const wf = s.source_workflow || 'p7_fel';
  const dropIn = trust && s.target_type === 'trusted_p7_fel_final' && wf === 'p7_fel';
  let desc;
  if (dropIn) {
    desc = 'mkvmerge directo sobre source_injected.hevc (BL+EL dual-layer ya combinado en Fase F) con audio/subs/capítulos del MKV origen.';
  } else if (wf === 'p7_fel') {
    desc = 'dovi_tool mux combina BL.hevc + EL_injected.hevc en un HEVC dual-layer + mkvmerge añade audio/subs/capítulos del MKV origen.';
  } else {  // p7_mel / p8: single-layer
    desc = 'Sin mux dual-layer (single-layer) — mkvmerge directo sobre BL_injected.hevc con audio/subs/capítulos del MKV origen.';
  }
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">${escHtml(desc)}</div>
      <button class="btn btn-primary btn-md" onclick="cmv40DoRemux('${pid}')">📦 Remux MKV final</button>
    </div>`;
}

function _cmv40FaseHBody(pid, s) {
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Verifica que el MKV resultante tiene CMv4.0 y mueve a /mnt/output.</div>
      <button class="btn btn-primary btn-md" onclick="cmv40DoValidate('${pid}')">✅ Validar y finalizar</button>
    </div>`;
}

// ── Acciones de fases ────────────────────────────────────────────

/** Toast de inicio de fase. Silenciado cuando el auto-pipeline está activo
 *  — el timeline lateral ya muestra fase en curso + progreso en vivo y los
 *  toasts intermedios saturan la UI. Con auto-off (usuario dispara fase
 *  manualmente con el botón), sí aparece para confirmar que se oyó el click. */
function _cmv40PhaseToast(pid, msg) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (project?.autoContinue) return;
  showToast(msg, 'info');
}

async function cmv40DoAnalyzeSource(pid) {
  await apiFetch(`/api/cmv40/${pid}/analyze-source`, { method: 'POST' });
  _cmv40PhaseToast(pid, 'Analizando origen…');
  // Polling hasta que termine la fase
  _cmv40PollPhase(pid, 'source_analyzed', 'error');
}

/**
 * Polling hasta que la sesión alcance una fase objetivo (o error).
 * Refresca la UI cada 500ms durante 5 min máximo.
 *
 * Si el proyecto tiene project.autoContinue === true y terminó la fase con
 * éxito, dispara la siguiente fase automáticamente (sin atravesar Fase D).
 */
async function _cmv40PollPhase(pid, targetPhase, errorPhase = 'error', maxTries = 600) {
  for (let i = 0; i < maxTries; i++) {
    await new Promise(r => setTimeout(r, 500));
    // silent: ver _refreshCMv40Session — polling rutinario suprime toasts
    // de timeout transitorio bajo carga I/O.
    const data = await apiFetch(`/api/cmv40/${pid}`, { silent: true });
    if (!data) continue;
    const project = openCMv40Projects.find(p => p.id === pid);
    if (project) {
      _cmv40AssignSession(project, data);
      _updateCMv40Panel(project);
    }
    // Termina cuando: no hay fase corriendo, alcanzó objetivo, hay error, o done
    if (!data.running_phase && (data.phase === targetPhase || data.phase === 'done' || data.error_message)) {
      refreshCMv40Sidebar();
      // Auto-avanzar si el flag está activo y no hay error
      if (project && project.autoContinue && !data.error_message && data.phase !== 'done') {
        _cmv40MaybeAutoAdvance(project);
      }
      return;
    }
  }
}

/**
 * Orquesta el auto-pipeline: dispara la siguiente fase según la actual.
 * Fase D (extracted → sync_verified) es MANUAL por diseño — revisión visual.
 */
function _cmv40MaybeAutoAdvance(project) {
  if (!project.autoContinue) return;
  const s = project.session;
  if (s.running_phase || s.error_message || s.archived) return;
  // Pause point por gates críticos pendientes de ACK del usuario. Banner
  // ámbar en el panel pide confirmación; sin ack no se progresa. Apagamos
  // _autoChaining para que el overlay se oculte y se vea el banner.
  if (s.awaiting_critical_ack) {
    project._autoChaining = false;
    return;
  }
  // Pause point por decisión del pre-flight: si el bin del repo fue
  // clasificado como sintético (keep_l8_default) o sin CMv4.0, el backend
  // detuvo el pipeline esperando que el usuario acepte Keep o fuerce
  // Restore desde la UI. Sin este guard, el frontend re-disparaba el
  // endpoint /preflight-target cada 4s (aunque el backend ya lo rechaza
  // con 'started:false', generaba tráfico HTTP inútil).
  if (s.preflight_decision && s.preflight_decision !== 'ok') {
    project._autoChaining = false;
    return;
  }
  const pid = project.id;
  // Dedup key: phase + estado de target_preflight_ok. Necesitamos sensibilidad
  // al flag de preflight porque para la fase 'created' hay dos acciones
  // distintas: si !target_preflight_ok → disparar preflight; si OK → Fase A.
  // Sin esto, _lastAutoFiredFor === 'created' nos bloquearía la transición
  // preflight → Fase A.
  //
  // RETRY ROBUSTO: el flag ahora trackea el timestamp del último trigger.
  // Si el mismo stateKey lleva >5s sin haber avanzado (caso real: pestaña
  // sin foco → setTimeout throttled → polling interno se cuelga → la
  // siguiente fase nunca se dispara), volvemos a intentar. Sin esto, una
  // sola falla silenciosa atasca el auto-pipeline indefinidamente.
  const stateKey = s.phase + ':pf=' + (s.target_preflight_ok ? '1' : '0');
  const now = Date.now();
  const lastFired = project._lastAutoFiredFor;
  // Dedup: para fases NO terminales, retry tras 5s (recupera transiciones
  // perdidas por throttling de background tab). Para fases TERMINALES
  // (done), dedup ESTRICTO una sola vez — sino el toast "Pipeline
  // completado" se re-disparaba cada 5s al volver el foco a la pestaña
  // (visto: con un proyecto done abierto, el toast aparecía recurrente
  // porque cada burst refresh / safety check llamaba a esta función).
  const isTerminalPhase = (s.phase === 'done');
  if (lastFired && lastFired.state === stateKey) {
    if (isTerminalPhase || (now - lastFired.at) < 5000) {
      return;
    }
  }
  project._lastAutoFiredFor = { state: stateKey, at: now };
  // Marca que la cadena auto está encadenando en este momento — usado por
  // el overlay para mostrarse durante el "puente" entre dos fases. Se limpia
  // al alcanzar un estado terminal o al intervenir manualmente (toggle,
  // reset, cancel). No es lo mismo que autoContinue: el flag refleja
  // actividad, la variable refleja configuración.
  project._autoChaining = true;
  // Los toasts intermedios ("🤖 Auto: analizando", "🤖 Auto: inyectando"…) eran
  // redundantes con el timeline lateral que ya muestra fase en curso + progreso.
  // Aquí solo disparamos las acciones del pipeline; el toast de inicio está en
  // cmv40ToggleAuto y el de fin (done) lo emitimos al final del switch.
  switch (s.phase) {
    case 'created':
      // Pre-flight bloqueante: si hay pendingTarget y aun no se ha validado,
      // disparamos preflight PRIMERO. Setea running_phase="preflight" y bloquea
      // el resto del pipeline. Solo cuando target_preflight_ok=true, el
      // siguiente tick de auto-advance dispara Fase A.
      if (project.pendingTarget && !s.target_preflight_ok) {
        _cmv40FirePreflight(pid, project.pendingTarget);
      } else {
        cmv40DoAnalyzeSource(pid);
      }
      break;
    case 'source_analyzed':
      // Si el usuario preseleccionó el target en el modal, aplicarlo automático
      if (project.pendingTarget) {
        const t = project.pendingTarget;
        project.pendingTarget = null;
        if (t.kind === 'path') {
          _cmv40AutoTargetPath(pid, t.value);
        } else if (t.kind === 'repo') {
          _cmv40AutoTargetDrive(pid, t.value);
        } else {
          _cmv40AutoTargetMkv(pid, t.value);
        }
      } else {
        // Pause point: sin pendingTarget, el usuario debe provisionar manual.
        // Apagamos _autoChaining para que el overlay se oculte y la UI vuelva
        // al proyecto. autoContinue se mantiene ON para retomar si el usuario
        // lanza Fase B manualmente.
        project._autoChaining = false;
      }
      break;
    case 'target_provided':
      cmv40DoExtract(pid);
      break;
    case 'extracted': {
      // Trusted target: los gates automáticos ya validaron frame count,
      // CM v4.0, L5/L6 — saltar la revisión visual manual.
      // user_acknowledged_degradation: el usuario reconocio que algun gate
      // critico (L5 grande, L6/L1 muy grandes) genera resultado degradado
      // pero aceptó continuar — Fase D no puede arreglar nada en ese caso,
      // saltamos directamente a inject/remux/validate.
      const s = project.session;
      const trustedAuto = (s.target_trust_ok === true || s.user_acknowledged_degradation === true)
        && s.trust_override !== 'force_interactive';
      if (trustedAuto) {
        if (!s.phases_skipped) s.phases_skipped = [];
        if (!s.phases_skipped.includes('sync_verification_pause')) {
          s.phases_skipped.push('sync_verification_pause');
        }
        _cmv40AutoMarkSynced(pid);
      } else {
        // Pause point: target no pasó los trust gates (caso MKV custom o bin
        // generated). El flujo se detiene aqui a la espera de revisión visual
        // manual en Fase D. Apagamos _autoChaining para que el overlay se oculte
        // y el usuario pueda interactuar con el chart. autoContinue se mantiene
        // ON para que al pulsar "Confirmar sync" (o aplicar correccion) la
        // cadena retome automaticamente hacia Fase F.
        project._autoChaining = false;
        showToast('⏸️ Auto pausado en Fase D — los gates requieren revisión manual del sync', 'info');
      }
      break;
    }
    case 'sync_verified':
      _cmv40AutoInject(pid);
      break;
    case 'injected':
      cmv40DoRemux(pid);
      break;
    case 'remuxed':
      cmv40DoValidate(pid);
      break;
    case 'done':
      // Terminal: toast único de éxito cuando la pipeline completa el full
      // run. El dedup estricto por isTerminalPhase (línea ~13755) evita que
      // bursts post-wake / safety poller / ws.onopen re-disparen el toast.
      // NO apagamos `autoContinue` aquí: el backend mantiene
      // `session.auto_pipeline=true` post-done y desincronizar el frontend
      // confundiría futuros refreshes (resumeAuto leería true del backend
      // y revertiría la flag local a true).
      showToast('✅ Pipeline CMv4.0 completado — MKV listo en /mnt/output', 'success');
      break;
  }
}

async function _cmv40AutoTargetPath(pid, rpuPath) {
  await apiFetch(`/api/cmv40/${pid}/target-rpu-path`, {
    method: 'POST',
    body: JSON.stringify({ rpu_path: rpuPath }),
  });
  _cmv40PollPhase(pid, 'target_provided');
}

async function _cmv40AutoTargetDrive(pid, driveSel) {
  await apiFetch(`/api/cmv40/${pid}/target-rpu-from-drive`, {
    method: 'POST',
    body: JSON.stringify({ file_id: driveSel.file_id, file_name: driveSel.file_name }),
  });
  _cmv40PollPhase(pid, 'target_provided');
}

async function _cmv40AutoTargetMkv(pid, mkvPath) {
  await apiFetch(`/api/cmv40/${pid}/target-rpu-from-mkv`, {
    method: 'POST',
    body: JSON.stringify({ source_mkv_path: mkvPath }),
  });
  _cmv40PollPhase(pid, 'target_provided');
}

async function _cmv40AutoInject(pid) {
  await apiFetch(`/api/cmv40/${pid}/inject`, { method: 'POST' });
  _cmv40PollPhase(pid, 'injected');
}

// Para target trusted: confirma sync OK sin intervención manual y avanza
// automáticamente a Fase F (inject).
async function _cmv40AutoMarkSynced(pid) {
  await apiFetch(`/api/cmv40/${pid}/mark-synced`, { method: 'POST' });
  _cmv40PollPhase(pid, 'sync_verified');
}

/** Toggle del auto-pipeline para un proyecto. */
async function cmv40ToggleAuto(pid) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  // Si activamos, validar colisión de nombre en /mnt/output
  if (!project.autoContinue) {
    const existing = await apiFetch('/api/mkv/files');
    const name = project.session.output_mkv_name;
    if (existing?.files?.includes(name)) {
      showToast(`⚠️ Ya existe un MKV con el nombre "${name}" en /mnt/output. Renómbralo antes de activar auto.`, 'warning');
      return;
    }
  }
  project.autoContinue = !project.autoContinue;
  // El switch solo marca el modo de trabajo — NO dispara fases por si mismo.
  // Al acabar la fase que el usuario lance manualmente, si auto=ON la siguiente
  // se encadena automaticamente. Lanzar con el toggle seria sorprendente para
  // el usuario (ej. si tocan el toggle sin recordar que estado tiene el proyecto).
  // Toggling tambien apaga _autoChaining — limpia el estado de bridging.
  project._autoChaining = false;
  _updateCMv40Panel(project);
  // Sincroniza con el backend (auto_pipeline persistente). Si lo activamos
  // y la sesión está en una fase intermedia, el backend retoma la cadena
  // inmediatamente — el job avanza solo aunque cierres el navegador.
  apiFetch(`/api/cmv40/${pid}/auto-pipeline`, {
    method: 'POST',
    body: JSON.stringify({ enabled: project.autoContinue }),
    silent: true,
  }).catch(() => {});
  if (project.autoContinue) {
    showToast('🤖 Auto-pipeline activado · el backend encadenará las fases sin depender del cliente', 'success');
  } else {
    showToast('Auto-pipeline desactivado · tendrás que lanzar cada fase manualmente', 'info');
  }
}

function _cmv40SwitchTargetTab(pid, tab) {
  const repoEl = document.getElementById(`cmv40-target-repo-${pid}`);
  const pathEl = document.getElementById(`cmv40-target-path-${pid}`);
  const mkvEl  = document.getElementById(`cmv40-target-mkv-${pid}`);
  if (repoEl) repoEl.style.display = (tab === 'repo') ? '' : 'none';
  if (pathEl) pathEl.style.display = (tab === 'path') ? '' : 'none';
  if (mkvEl)  mkvEl.style.display  = (tab === 'mkv')  ? '' : 'none';
  const btnRepo = document.getElementById(`cmv40-tab-btn-repo-${pid}`);
  const btnPath = document.getElementById(`cmv40-tab-btn-path-${pid}`);
  const btnMkv  = document.getElementById(`cmv40-tab-btn-mkv-${pid}`);
  if (btnRepo) btnRepo.classList.toggle('active', tab === 'repo');
  if (btnPath) btnPath.classList.toggle('active', tab === 'path');
  if (btnMkv)  btnMkv.classList.toggle('active',  tab === 'mkv');
  if (tab === 'repo')      _cmv40LoadRepoForPanel(pid);
  else if (tab === 'path') _cmv40LoadRpus(pid);
  else                     _cmv40LoadTargetMkvs(pid);
}

// Token anti-race por proyecto (igual que en el modal pero scopeado al pid)
const _cmv40PanelRepoReqIds = {};

/**
 * Carga candidatos de bin del repo DoviTools para un proyecto en Fase B.
 * Usa el filename del MKV origen del proyecto (no estado del modal).
 */
async function _cmv40LoadRepoForPanel(pid) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  const list = document.getElementById(`cmv40-repo-list-${pid}`);
  const info = document.getElementById(`cmv40-repo-info-${pid}`);
  if (!list) return;
  const sourcePath = project.session?.source_mkv_path || '';
  const filename = sourcePath.split('/').pop() || '';
  if (!filename) {
    list.innerHTML = '<div class="cmv40-repo-empty">Sin MKV origen — imposible matchear.</div>';
    if (info) info.textContent = '';
    return;
  }
  list.innerHTML = '<div class="cmv40-repo-empty">⏳ Buscando en Drive…</div>';
  if (info) info.innerHTML = '<span class="cmv40-rec-spinner-inline"></span> Consultando repositorio de DoviTools…';
  const reqId = (_cmv40PanelRepoReqIds[pid] || 0) + 1;
  _cmv40PanelRepoReqIds[pid] = reqId;
  const qs = '?filename=' + encodeURIComponent(filename);
  const data = await apiFetch('/api/cmv40/repo-rpus' + qs);
  if (_cmv40PanelRepoReqIds[pid] !== reqId) return;
  if (!data) {
    list.innerHTML = '<div class="cmv40-repo-empty">Error consultando el repositorio.</div>';
    if (info) info.textContent = '';
    return;
  }
  if (!data.drive_configured) {
    list.innerHTML = '<div class="cmv40-repo-empty">Repositorio DoviTools no configurado — abre ⚙︎ Configuración para añadir Google API key + URL del repo.</div>';
    if (info) info.textContent = '';
    return;
  }
  const cands = data.candidates || [];
  if (!cands.length) {
    const t = data.title_en || data.title_es || filename;
    list.innerHTML = `<div class="cmv40-repo-empty">Sin coincidencias para "${escHtml(t)}". Prueba otra pestaña.</div>`;
    if (info) info.textContent = '';
    return;
  }
  project._panelRepoCands = cands;
  const topId = cands[0]?.file?.id || '';
  const renderCard = (c) => {
    const sizeMb = (c.file.size_bytes / 1024 / 1024).toFixed(1);
    const pt = c.predicted_type || 'unknown';
    const prov = c.provenance || '';
    const tagMeta = pt === 'trusted_p7_fel_final' ? { icon: '🎯', label: 'bin P7 FEL',  cls: 'tag-ok' }
                  : pt === 'trusted_p7_mel_final' ? { icon: '🎯', label: 'bin P7 MEL',  cls: 'tag-ok' }
                  : pt === 'trusted_p8_source'    ? { icon: '📦', label: 'bin P8 retail', cls: 'tag-info' }
                  : { icon: '❓', label: 'tipo desconocido', cls: 'tag-warn' };
    const provTag = prov === 'retail'
      ? '<span class="cmv40-repo-card-tag tag-ok">🏛 Retail</span>'
      : prov === 'generated'
      ? '<span class="cmv40-repo-card-tag tag-warn">⚠ Generated</span>'
      : '';
    const isBest = c.file.id === topId;
    return `
      <div class="cmv40-repo-card" data-file-id="${escHtml(c.file.id)}"
           role="button" tabindex="0"
           onclick="_cmv40SelectRepoForPanel('${escHtml(pid)}','${escHtml(c.file.id)}')"
           onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();_cmv40SelectRepoForPanel('${escHtml(pid)}','${escHtml(c.file.id)}')}">
        <div class="cmv40-repo-card-head">
          <span class="cmv40-repo-card-tag ${tagMeta.cls}">${tagMeta.icon} ${tagMeta.label}</span>
          ${provTag}
          ${isBest ? '<span class="cmv40-repo-card-best">🏆 mejor match</span>' : ''}
          <span class="cmv40-repo-card-score">${Math.round(c.score * 100)}%</span>
          <span class="cmv40-repo-card-size">${sizeMb} MB</span>
        </div>
        <div class="cmv40-repo-card-path">${escHtml(c.file.path)}</div>
      </div>`;
  };
  list.innerHTML = cands.map(renderCard).join('');
  if (info) {
    info.innerHTML = `<strong>${cands.length}</strong> candidato${cands.length !== 1 ? 's' : ''} · top score: <strong>${Math.round(cands[0].score * 100)}%</strong>. Click para seleccionar.`;
  }
  if (topId) _cmv40SelectRepoForPanel(pid, topId);
}

function _cmv40SelectRepoForPanel(pid, fileId) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  const list = document.getElementById(`cmv40-repo-list-${pid}`);
  if (!list) return;
  const card = list.querySelector(`.cmv40-repo-card[data-file-id="${fileId}"]`);
  if (!card) return;
  list.querySelectorAll('.cmv40-repo-card.selected').forEach(el => el.classList.remove('selected'));
  card.classList.add('selected');
  const cand = (project._panelRepoCands || []).find(c => c.file.id === fileId);
  if (!cand) return;
  project._panelSelectedRepo = { file_id: cand.file.id, file_name: cand.file.name };
}

async function cmv40DoTargetFromDrive(pid) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  const sel = project._panelSelectedRepo;
  if (!sel || !sel.file_id) {
    showToast('Selecciona un candidato del repositorio', 'warning');
    return;
  }
  await apiFetch(`/api/cmv40/${pid}/target-rpu-from-drive`, {
    method: 'POST',
    body: JSON.stringify({ file_id: sel.file_id, file_name: sel.file_name || '' }),
  });
  _cmv40PhaseToast(pid, 'Descargando RPU del repositorio…');
  _cmv40PollPhase(pid, 'target_provided');
}

async function _cmv40LoadRpus(pid) {
  const select = document.getElementById(`cmv40-rpu-select-${pid}`);
  const data = await apiFetch('/api/cmv40/rpu-files');
  select.innerHTML = '<option value="">— Seleccionar RPU —</option>';
  if (data?.files?.length) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = `${f.name} (${_fmtBytes(f.size_bytes)})`;
      select.appendChild(opt);
    });
  } else {
    select.innerHTML = '<option value="">— No hay RPUs en /mnt/cmv40_rpus —</option>';
  }
}

async function _cmv40LoadTargetMkvs(pid) {
  const select = document.getElementById(`cmv40-target-mkv-select-${pid}`);
  const data = await apiFetch('/api/mkv/files-in-isos');
  select.innerHTML = '<option value="">— Seleccionar MKV con CMv4.0 —</option>';
  if (data?.files && data.files.length) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = f.name;
      select.appendChild(opt);
    });
  } else {
    select.innerHTML = '<option value="">— No hay MKVs en el directorio de ISOs —</option>';
  }
}

async function cmv40DoTargetFromPath(pid) {
  const select = document.getElementById(`cmv40-rpu-select-${pid}`);
  const rpuPath = select.value;
  if (!rpuPath) {
    showToast('Selecciona un RPU', 'warning');
    return;
  }
  const data = await apiFetch(`/api/cmv40/${pid}/target-rpu-path`, {
    method: 'POST',
    body: JSON.stringify({ rpu_path: rpuPath }),
  });
  if (data) {
    showToast('RPU target cargado', 'success');
    const project = openCMv40Projects.find(p => p.id === pid);
    if (project) {
      _cmv40AssignSession(project, data);
      _updateCMv40Panel(project);
      refreshCMv40Sidebar();
      if (project.autoContinue) _cmv40MaybeAutoAdvance(project);
    } else {
      _refreshCMv40Session(pid);
    }
  }
}

async function cmv40DoTargetFromMkv(pid) {
  const select = document.getElementById(`cmv40-target-mkv-select-${pid}`);
  const mkvPath = select.value;
  if (!mkvPath) {
    showToast('Selecciona un MKV', 'warning');
    return;
  }
  await apiFetch(`/api/cmv40/${pid}/target-rpu-from-mkv`, {
    method: 'POST',
    body: JSON.stringify({ source_mkv_path: mkvPath }),
  });
  _cmv40PhaseToast(pid, 'Extrayendo RPU del MKV…');
  _cmv40PollPhase(pid, 'target_provided');
}

async function cmv40DoExtract(pid) {
  await apiFetch(`/api/cmv40/${pid}/extract`, { method: 'POST' });
  _cmv40PhaseToast(pid, 'Extrayendo BL/EL y datos per-frame…');
  _cmv40PollPhase(pid, 'extracted');
}

async function cmv40DoInject(pid) {
  showConfirm(
    '¿Inyectar RPU?',
    'Esto creará EL_injected.hevc. ¿Has verificado que la sincronización es correcta?',
    async () => {
      await apiFetch(`/api/cmv40/${pid}/inject`, { method: 'POST' });
      _cmv40PhaseToast(pid, 'Inyectando RPU…');
      _cmv40PollPhase(pid, 'injected');
    },
    'Inyectar',
  );
}

async function cmv40DoRemux(pid) {
  await apiFetch(`/api/cmv40/${pid}/remux`, { method: 'POST' });
  _cmv40PhaseToast(pid, 'Remuxando a MKV final…');
  _cmv40PollPhase(pid, 'remuxed');
}

async function cmv40DoValidate(pid) {
  await apiFetch(`/api/cmv40/${pid}/validate`, { method: 'POST' });
  _cmv40PhaseToast(pid, 'Validando MKV final…');
  // Polling — Fase H dura varios minutos (move 42 GB), no se puede hacer síncrono
  _cmv40PollPhase(pid, 'done');
}

async function cmv40Cleanup(pid) {
  const bodyHtml = `
    <div style="line-height:1.6">
      <p style="margin:0 0 10px 0"><b>Qué se borrará:</b></p>
      <ul style="margin:0 0 12px 18px; padding:0; font-family:'SF Mono',monospace; font-size:11px">
        <li>source.hevc, BL.hevc, EL.hevc</li>
        <li>RPU_source.bin, RPU_target.bin, RPU_synced.bin</li>
        <li>EL_injected.hevc</li>
        <li>per_frame_data.json, editor_config.json</li>
      </ul>
      <p style="margin:0 0 10px 0"><b>Qué se preserva:</b></p>
      <ul style="margin:0 0 12px 18px; padding:0; font-size:12px">
        <li>El MKV final en <code>/mnt/output</code></li>
        <li>Los metadatos del proyecto (log, sync_config, info DV)</li>
      </ul>
      <div class="banner warning" style="margin-top:12px">
        <span class="banner-icon">⚠️</span>
        <span><b>Esta acción archiva el proyecto</b>. No podrás rehacer fases porque los artefactos de entrada ya no existen. Para iterar de nuevo tendrás que crear un proyecto nuevo desde el MKV origen.</span>
      </div>
    </div>`;

  document.getElementById('cmv40-confirm-title').textContent = '¿Limpiar artefactos?';
  document.getElementById('cmv40-confirm-sub').textContent = 'Esta acción libera espacio en disco pero deja el proyecto en modo solo lectura.';
  document.getElementById('cmv40-confirm-body').innerHTML = bodyHtml;

  const btn = document.getElementById('cmv40-confirm-btn');
  btn.textContent = 'Limpiar y archivar';
  btn.className = 'btn btn-danger btn-sm';
  const newBtn = btn.cloneNode(true);
  btn.parentNode.replaceChild(newBtn, btn);
  newBtn.addEventListener('click', async () => {
    closeModal('cmv40-confirm-modal');
    const data = await apiFetch(`/api/cmv40/${pid}/cleanup`, { method: 'POST' });
    if (data) {
      showToast(`Liberado ${_fmtBytes(data.freed_bytes)} · proyecto archivado`, 'success');
      _refreshCMv40Session(pid);
    }
  });
  openModal('cmv40-confirm-modal');
}

// ── Sidebar Tab 3 ────────────────────────────────────────────────

async function refreshCMv40Sidebar() {
  // silent: invocado desde WS handlers, _refreshCMv40Session, _cmv40PollPhase
  // y tras cada accion de fase. Bajo VPN un timeout transitorio no es util —
  // el siguiente tick (otro mensaje WS o accion del usuario) lo corrige.
  const data = await apiFetch('/api/cmv40', { silent: true });
  _cmv40SidebarList = data?.sessions || [];

  // Auto-resume del overlay: si hay un proyecto con running_phase != null,
  // no archivado, y NO hay nada abierto en Tab 3, abrirlo automáticamente
  // para que el usuario vea el modal de ejecución y el log en vivo. Cubre
  // el caso "Mac dormido toda la noche, hoy abro pestaña" — sin esto el
  // usuario tendría que recordar qué proyecto estaba corriendo y abrirlo
  // desde el sidebar manualmente.
  if (!_cmv40AutoResumeAttempted && openCMv40Projects.length === 0) {
    const running = _cmv40SidebarList.find(
      s => s.running_phase && !s.archived
    );
    if (running) {
      _cmv40AutoResumeAttempted = true;
      openCMv40Project(running);
      const niceName = running.source_mkv_name || running.id;
      const phaseLabel = (typeof CMV40_RUNNING_LABELS === 'object' && CMV40_RUNNING_LABELS)
        ? (CMV40_RUNNING_LABELS[running.running_phase] || running.running_phase)
        : running.running_phase;
      showToast(`🤖 Reanudando seguimiento: ${niceName} · ${phaseLabel}`, 'info');
    } else {
      // No hay nada que reanudar — marcamos el intento hecho para no
      // reevaluar en cada refresh del sidebar (es 1-shot por entrada al tab).
      _cmv40AutoResumeAttempted = true;
    }
  }
  // Capturar cambio del select de ordenación
  const sortSel = document.getElementById('cmv40-sidebar-sort');
  if (sortSel) {
    _cmv40SortKey = sortSel.value;
    if (!sortSel.dataset.bound) {
      sortSel.addEventListener('change', () => {
        _cmv40SortKey = sortSel.value;
        _renderCMv40Sidebar();
      });
      sortSel.dataset.bound = '1';
    }
  }
  _renderCMv40Sidebar();
}

function _renderCMv40Sidebar() {
  const list = document.getElementById('cmv40-sidebar-list');
  const count = document.getElementById('cmv40-count');
  if (!list) return;

  // Filtro de búsqueda
  const searchEl = document.getElementById('cmv40-sidebar-search');
  const searchTerm = (searchEl?.value || '').toLowerCase().trim();
  const norm = (s) => (s || '').toLowerCase().replace(/[^\w\s]/g, '');

  // Filtro de fase
  let filtered = _cmv40SidebarList.slice();
  if (_cmv40Filter === 'done') {
    filtered = filtered.filter(s => s.phase === 'done' || s.phase === 'validated');
  } else if (_cmv40Filter === 'error') {
    filtered = filtered.filter(s => !!s.error_message);
  } else if (_cmv40Filter === 'in_progress') {
    filtered = filtered.filter(s => !['done', 'validated', 'cancelled'].includes(s.phase) && !s.error_message);
  }
  if (searchTerm) {
    filtered = filtered.filter(s => {
      const hay = norm(s.source_mkv_name + ' ' + (CMV40_PHASE_LABELS[s.phase] || s.phase));
      return hay.includes(norm(searchTerm));
    });
  }

  // Ordenación
  const sortKey = _cmv40SortKey;
  const dir = _cmv40SortDir === 'asc' ? 1 : -1;
  filtered.sort((a, b) => {
    let av, bv;
    if (sortKey === 'name') {
      av = (a.source_mkv_name || '').toLowerCase();
      bv = (b.source_mkv_name || '').toLowerCase();
    } else if (sortKey === 'phase') {
      av = CMV40_PHASES_ORDER.indexOf(a.phase);
      bv = CMV40_PHASES_ORDER.indexOf(b.phase);
    } else {
      av = new Date(a.updated_at || 0).getTime();
      bv = new Date(b.updated_at || 0).getTime();
    }
    if (av < bv) return -dir;
    if (av > bv) return dir;
    return 0;
  });

  if (count) count.textContent = filtered.length;
  list.innerHTML = '';

  if (filtered.length === 0) {
    list.innerHTML = `
      <div class="empty-state" style="padding:24px 12px">
        <div class="empty-state-icon">🎨</div>
        <div>${searchTerm || _cmv40Filter !== 'all' ? 'Sin resultados' : 'Crea un proyecto para inyectar CMv4.0'}</div>
      </div>`;
    return;
  }

  filtered.forEach(s => {
    const isRunning  = !!s.running_phase;
    const phaseLabel = s.archived ? 'Archivado' : (CMV40_PHASE_LABELS[s.phase] || s.phase);
    const runningLabel = isRunning
      ? (CMV40_RUNNING_LABELS[s.running_phase] || s.running_phase)
      : null;
    const phaseIcon = s.archived
      ? '🗃️'
      : (s.error_message ? '⚠️' : (CMV40_PHASE_ICONS[s.phase] || '🎨'));
    const isOpen = openCMv40Projects.find(p => p.id === s.id);
    const isSelected = _cmv40SelectedSidebarId === s.id;
    const name = s.source_mkv_name.replace(/\.mkv$/i, '');

    const modDate = formatRelativeDate(s.updated_at || s.created_at);
    const modFull = new Date(s.updated_at || s.created_at).toLocaleString('es-ES', {
      day: '2-digit', month: '2-digit', year: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });

    const card = document.createElement('div');
    card.className = `session-card${isSelected ? ' selected' : ''}${isRunning ? ' is-running' : ''}`;
    card.dataset.sid = s.id;
    // Tooltip distinto cuando running_phase: indica claramente la fase
    // activa, no la última fase completada (que es lo que da phaseLabel).
    const badgeTooltip = isRunning ? `${runningLabel}` : phaseLabel;
    // Cuando está running, sustituimos el icono estático por un spinner
    // animado para que sea visualmente obvio en la lista que algo está
    // corriendo, sin necesidad de pasar el ratón.
    const badgeContent = isRunning
      ? `<span class="cmv40-card-spinner" aria-label="${escHtml(runningLabel)}"></span>`
      : phaseIcon;
    card.innerHTML = `
      <div class="session-card-row">
        <div class="session-card-status-badge${isRunning ? ' running' : ''}" data-tooltip="${escHtml(badgeTooltip)}">${badgeContent}</div>
        <div class="session-card-body">
          <div class="session-card-title" data-tooltip="${escHtml(name)}">${escHtml(name)}</div>
          <div class="session-card-meta">
            <div class="session-card-meta-row">
              <span class="meta-label">Fase</span>
              <span>${escHtml(phaseLabel)}</span>
            </div>
            <div class="session-card-meta-row">
              <span class="meta-label">Modif.</span>
              <span class="relative-date" data-iso="${s.updated_at || s.created_at || ''}"
                data-tooltip="${escHtml('Modificado: ' + modFull)}">${escHtml(modDate)}</span>
            </div>
          </div>
        </div>
        ${isOpen ? '<span class="session-item-badge">abierto</span>' : ''}
      </div>
      <div class="session-card-actions">
        <button class="btn btn-primary btn-sm" onclick="event.stopPropagation();_cmv40OpenSelected('${s.id}')"
          data-tooltip="Abrir este proyecto">📂 Abrir</button>
        <button class="btn btn-danger btn-sm" onclick="event.stopPropagation();_cmv40DeleteFromSidebar('${s.id}')"
          data-tooltip="Eliminar permanentemente">🗑️ Eliminar</button>
      </div>`;
    const row = card.querySelector('.session-card-row');
    row.onclick = () => _cmv40ToggleSidebarSelection(s.id);
    row.ondblclick = () => _cmv40OpenSelected(s.id);
    list.appendChild(card);
  });
}

function _cmv40ToggleSortDir() {
  _cmv40SortDir = _cmv40SortDir === 'asc' ? 'desc' : 'asc';
  const btn = document.getElementById('cmv40-sort-dir');
  if (btn) btn.textContent = _cmv40SortDir === 'asc' ? '↑' : '↓';
  _renderCMv40Sidebar();
}

function _cmv40FilterClick(btn) {
  document.querySelectorAll('#sidebar-tab-3 .sb-filter-pill').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _cmv40Filter = btn.dataset.filter;
  _renderCMv40Sidebar();
}

function _cmv40ToggleSidebarSelection(sid) {
  _cmv40SelectedSidebarId = (_cmv40SelectedSidebarId === sid) ? null : sid;
  document.querySelectorAll('#cmv40-sidebar-list .session-card').forEach(card => {
    card.classList.toggle('selected', card.dataset.sid === _cmv40SelectedSidebarId);
  });
}

function _cmv40OpenSelected(sid) {
  const s = _cmv40SidebarList.find(x => x.id === sid);
  if (s) openCMv40Project(s);
}

async function _cmv40DeleteFromSidebar(sid) {
  const s = _cmv40SidebarList.find(x => x.id === sid);
  if (!s) return;
  showConfirm(
    '¿Eliminar proyecto?',
    `Se eliminará "${s.source_mkv_name}" y sus artefactos intermedios. Esta acción no se puede deshacer.`,
    async () => {
      await apiFetch(`/api/cmv40/${sid}?clean_artifacts=true`, { method: 'DELETE' });
      // Cerrar subtab si estaba abierto
      const open = openCMv40Projects.find(p => p.id === sid);
      if (open) closeCMv40Project(sid);
      if (_cmv40SelectedSidebarId === sid) _cmv40SelectedSidebarId = null;
      refreshCMv40Sidebar();
    },
    'Eliminar',
  );
}

// ── Chart interactivo de sincronización (Fase D) ─────────────────

async function _loadCMv40SyncChart(project) {
  const pid = project.id;
  // Skip defensivo: si el canvas del chart no existe en el DOM (p.ej. Fase D
  // omitida por trusted → body muestra banner sin canvas), no hay donde
  // renderizar y el fetch solo provocaría regeneración innecesaria del
  // per_frame_data.json en backend.
  if (!document.getElementById(`cmv40-chart-wrap-${pid}`)) return;
  // Guard anti-thundering-herd: cada re-render de la phase card llamaba aquí.
  // Sin flag, N renders antes de que resuelva la promesa lanzaban N fetches
  // paralelos → N `dovi_tool export` concurrentes en backend → I/O thrash.
  if (project._syncDataLoading) return;
  if (!project.syncData) {
    project._syncDataLoading = true;
    try {
      const data = await apiFetch(`/api/cmv40/${pid}/sync-data`);
      if (!data) return;
      project.syncData = data;
    } finally {
      project._syncDataLoading = false;
    }
  }
  _renderCMv40Chart(project);
  _renderCMv40SyncStats(project);
  _renderCMv40SyncControls(project);
  _renderCMv40Confidence(project);
}

function _renderCMv40SyncStats(project) {
  const d = project.syncData;
  const s = project.session;
  const pid = project.id;
  const container = document.getElementById(`cmv40-sync-stats-${pid}`);
  if (!container) return;
  // Frame counts autoritativos de la sesión (reflejan correcciones ya aplicadas).
  const srcFrames = (s && s.source_frame_count) || d.source_frames;
  const tgtFrames = (s && s.target_frame_count) || d.target_frames;
  const delta = (s && s.sync_delta != null) ? s.sync_delta : (tgtFrames - srcFrames);
  const suggested = d.suggested_offset || {};

  container.innerHTML = `
    <div class="cmv40-sync-row">
      <div><span class="sync-label">Frames origen:</span> <b>${srcFrames.toLocaleString()}</b></div>
      <div><span class="sync-label">Frames target:</span> <b>${tgtFrames.toLocaleString()}</b></div>
      <div><span class="sync-label">Diferencia:</span> <b style="color:${delta===0?'var(--green)':'var(--orange)'}">${delta > 0 ? '+' : ''}${delta}</b></div>
    </div>
    ${suggested.offset !== undefined && suggested.offset !== 0 ? `
      <div class="banner info" style="margin-top:10px">
        <span class="banner-icon">🔍</span>
        <span>Offset detectado automáticamente: <b>${suggested.offset > 0 ? '+' : ''}${suggested.offset} frames</b></span>
      </div>` : ''}
  `;
}

function _renderCMv40Confidence(project) {
  const d = project.syncData;
  const pid = project.id;
  const container = document.getElementById(`cmv40-confidence-${pid}`);
  if (!container) return;
  const conf = d.confidence || {};
  const pct = conf.confidence_pct || 0;
  const rating = conf.rating || 'insufficient_data';
  const ratingColor = {
    'excellent': 'var(--green)',
    'good':      'var(--green)',
    'moderate':  'var(--orange)',
    'poor':      'var(--red)',
    'insufficient_data': 'var(--text-3)',
    'no_variance':       'var(--text-3)',
  }[rating];
  const ratingLabel = {
    'excellent': 'Excelente',
    'good':      'Buena',
    'moderate':  'Moderada',
    'poor':      'Baja',
    'insufficient_data': 'Datos insuficientes',
    'no_variance':       'Sin variación',
  }[rating];
  container.innerHTML = `
    <div class="cmv40-confidence-panel" style="border-color:${ratingColor}; margin-top:16px">
      <div class="cmv40-confidence-header">
        <span class="cmv40-confidence-label">Confianza de sincronización</span>
        <span class="cmv40-confidence-value" style="color:${ratingColor}">${pct}%</span>
        <span class="cmv40-confidence-rating" style="color:${ratingColor}">${ratingLabel}</span>
      </div>
      <div class="cmv40-confidence-bar">
        <div class="cmv40-confidence-fill" style="width:${pct}%; background:${ratingColor}"></div>
        <div class="cmv40-confidence-threshold" style="left:85%" data-tooltip="Umbral mínimo 85%">·</div>
      </div>
      <div class="cmv40-confidence-reason">${escHtml(conf.reason || '')}</div>
      <div style="font-size:10px; color:var(--text-3); margin-top:4px">
        Mide la correlación de forma entre MaxCLL origen y target. Insensible a diferencias de valor absoluto — las curvas pueden no coincidir exactamente pero sí seguir el mismo patrón temporal.
      </div>
    </div>
  `;
}

function _renderCMv40SyncControls(project) {
  const pid = project.id;
  const s = project.session;
  const d = project.syncData;
  const container = document.getElementById(`cmv40-sync-controls-${pid}`);
  if (!container) return;
  // Read-only mode: la sesión ya pasó Fase D (phase index > sync_verified).
  // Mostramos solo controles de zoom + inputs de rango para navegar el plot,
  // nada de form de corrección ni botones de apply/confirmar.
  const phaseIdx  = CMV40_PHASES_ORDER.indexOf(s.phase);
  const dDoneIdx  = CMV40_PHASES_ORDER.indexOf('sync_verified');
  const readOnly  = phaseIdx > dDoneIdx;
  const delta = (s && s.sync_delta != null) ? s.sync_delta : (d.target_frames - d.source_frames);
  const suggested = d.suggested_offset || {};
  const hasSyncConfig = !!s.sync_config;
  // Confianza y criterio para habilitar "Confirmar"
  const conf = d.confidence || {};
  const confPct = conf.confidence_pct || 0;
  const confOk  = !!conf.threshold_ok;
  const canConfirm = delta === 0 && confOk;
  const confirmReason = delta !== 0
    ? 'Hay diferencia de frames, debes corregir primero'
    : !confOk
      ? `Confianza ${confPct}% inferior al umbral 85% — revisa el gráfico o verifica compatibilidad del RPU`
      : '';
  // Framerate real del vídeo origen (fallback 23.976)
  const FPS = s.source_fps || 23.976;
  const totalFrames = d.source_frames || d.target_frames || 0;
  if (!project.chartRange) {
    // Default: primeros 30s — la zona típica donde hay logos y desfases
    project.chartRange = { start: 0, end: Math.min(Math.round(30 * FPS), totalFrames) };
  }
  const currentRange = project.chartRange;

  // Detectar qué preset está activo (si el rango coincide exactamente)
  const presets = [
    { key: '30s',   start: 0, end: Math.min(Math.round(30 * FPS), totalFrames),       label: '30 s' },
    { key: '1min',  start: 0, end: Math.min(Math.round(60 * FPS), totalFrames),       label: '1 min' },
    { key: '5min',  start: 0, end: Math.min(Math.round(5 * 60 * FPS), totalFrames),   label: '5 min' },
    { key: '30min', start: 0, end: Math.min(Math.round(30 * 60 * FPS), totalFrames),  label: '30 min' },
    { key: 'all',   start: 0, end: totalFrames,                                        label: 'Todo' },
  ];
  const activeKey = presets.find(p => p.start === currentRange.start && p.end === currentRange.end)?.key;

  const presetBtns = presets.map(p => `
    <button class="btn btn-ghost btn-xs cmv40-zoom-preset${activeKey === p.key ? ' active' : ''}"
      onclick="_cmv40SetRange('${pid}', ${p.start}, ${p.end})">${p.label}</button>
  `).join('');

  const zoomRowHtml = `
    <div class="cmv40-zoom-row">
      <span class="section-subtitle">Zoom</span>
      ${presetBtns}
      <span class="cmv40-range-inputs">
        <label>Desde frame:
          <input type="number" id="cmv40-range-start-${pid}" value="${currentRange.start}" min="0" max="${totalFrames}"
            onchange="_cmv40ApplyRangeFromInputs('${pid}')">
        </label>
        <label>Hasta frame:
          <input type="number" id="cmv40-range-end-${pid}" value="${currentRange.end}" min="0" max="${totalFrames}"
            onchange="_cmv40ApplyRangeFromInputs('${pid}')">
        </label>
      </span>
    </div>`;

  // Read-only: solo zoom/rango, sin form de corrección.
  if (readOnly) {
    container.innerHTML = `
      ${zoomRowHtml}
      <div style="margin-top:10px; padding:8px 12px; background:var(--surface-2); border-radius:6px; font-size:11px; color:var(--text-3)">
        ${hasSyncConfig
          ? `Corrección aplicada en su día — el gráfico se muestra en modo solo lectura.`
          : 'Sincronización confirmada sin corrección (Δ era 0).'}
      </div>`;
    return;
  }

  container.innerHTML = `
    ${zoomRowHtml}

    <div class="section-subtitle" style="margin-top:16px; margin-bottom:4px">Corrección ${hasSyncConfig ? 'adicional' : 'manual'}</div>
    <div style="font-size:11px; color:var(--text-3); margin-bottom:8px">
      ${hasSyncConfig
        ? 'Estos valores se <b>sumarán</b> a la corrección ya aplicada. El Δ actual del gráfico indica cuánto falta por alinear.'
        : 'Los valores se aplican desde el target original.'}
    </div>
    <div class="cmv40-sync-form">
      <label>Eliminar N frames al inicio del target:
        <input type="number" id="cmv40-remove-${pid}" value="${delta > 0 ? delta : 0}" min="0" style="width:80px"
          oninput="_cmv40UpdateExpectedDelta('${pid}', ${delta})">
      </label>
      <label>Duplicar primer frame N veces:
        <input type="number" id="cmv40-duplicate-${pid}" value="${delta < 0 ? Math.abs(delta) : 0}" min="0" style="width:80px"
          oninput="_cmv40UpdateExpectedDelta('${pid}', ${delta})">
      </label>
    </div>
    <div style="margin-top:10px; padding:10px 12px; background:var(--surface-2); border-radius:6px; font-size:12px">
      <span style="color:var(--text-3)">Δ después de aplicar:</span>
      <b id="cmv40-expected-delta-${pid}" style="margin-left:6px">—</b>
      <span style="color:var(--text-3); margin-left:12px; font-size:11px">
        (remove ${delta > 0 ? delta : 0} · dup ${delta < 0 ? Math.abs(delta) : 0} dejaría Δ=0)
      </span>
    </div>
    <div style="display:flex; gap:10px; margin-top:16px; flex-wrap:wrap">
      <button class="btn btn-ghost btn-md" onclick="cmv40DoApplySync('${pid}')">✏️ Aplicar corrección</button>
      ${hasSyncConfig ? `<button class="btn btn-danger btn-md" onclick="cmv40DoResetSync('${pid}')"
          data-tooltip="Descartar corrección y volver al target original">↩️ Resetear al original</button>` : ''}
      <button class="btn btn-primary btn-md" onclick="cmv40DoSkipSync('${pid}')"
        ${canConfirm ? '' : 'disabled data-tooltip="' + confirmReason + '"'}>✓ Confirmar sync y continuar</button>
    </div>
    <div style="margin-top:8px; font-size:11px; color:var(--text-3)">
      Δ actual: <b style="color:${delta===0?'var(--green)':'var(--orange)'}">${delta > 0 ? '+' : ''}${delta} frames</b>
      · Confianza: <b style="color:${confOk ? 'var(--green)' : 'var(--orange)'}">${confPct}%</b>
      ${canConfirm ? ' — <b style="color:var(--green)">listo para continuar</b>' : ' — <b style="color:var(--orange)">' + confirmReason + '</b>'}
    </div>
  `;
  // Inicializar preview del Δ esperado
  _cmv40UpdateExpectedDelta(pid, delta);
}

function _cmv40UpdateExpectedDelta(pid, currentDelta) {
  const r = parseInt(document.getElementById(`cmv40-remove-${pid}`)?.value) || 0;
  const d = parseInt(document.getElementById(`cmv40-duplicate-${pid}`)?.value) || 0;
  // Aplicar remove reduce delta; duplicate lo aumenta
  const expected = currentDelta - r + d;
  const el = document.getElementById(`cmv40-expected-delta-${pid}`);
  if (!el) return;
  const sign = expected > 0 ? '+' : '';
  const color = expected === 0 ? 'var(--green)' : 'var(--orange)';
  el.innerHTML = `<span style="color:${color}">${sign}${expected} frames</span>`;
}

function _cmv40SetRange(pid, start, end) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  project.chartRange = { start, end };
  _renderCMv40Chart(project);
  _renderCMv40SyncControls(project);
}

function _cmv40ApplyRangeFromInputs(pid) {
  const start = parseInt(document.getElementById(`cmv40-range-start-${pid}`).value) || 0;
  const end = parseInt(document.getElementById(`cmv40-range-end-${pid}`).value) || 0;
  if (end <= start) {
    showToast('El frame final debe ser mayor que el inicial', 'warning');
    return;
  }
  _cmv40SetRange(pid, start, end);
}

async function cmv40DoResetSync(pid) {
  showConfirm(
    '¿Descartar corrección?',
    'Se borrará la corrección aplicada y el RPU target volverá a su estado original. El gráfico mostrará de nuevo el desfase inicial para que puedas empezar de cero.',
    async () => {
      const data = await apiFetch(`/api/cmv40/${pid}/reset-sync`, { method: 'POST' });
      if (data) {
        const project = openCMv40Projects.find(p => p.id === pid);
        if (project) {
          project.syncData = null;
          _cmv40AssignSession(project, data);
          project.chartRange = null;  // volver al zoom por defecto
          _updateCMv40Panel(project);
        }
        showToast('Corrección descartada', 'info');
      }
    },
    'Descartar corrección',
  );
}

async function cmv40DoApplySync(pid) {
  const remove = parseInt(document.getElementById(`cmv40-remove-${pid}`).value) || 0;
  const dup = parseInt(document.getElementById(`cmv40-duplicate-${pid}`).value) || 0;
  if (remove === 0 && dup === 0) {
    showToast('Indica un valor para eliminar o duplicar', 'warning');
    return;
  }
  const config = {};
  if (remove > 0) config.remove = [`0-${remove - 1}`];
  if (dup > 0) config.duplicate = [{ source: 0, offset: 0, length: dup }];
  const data = await apiFetch(`/api/cmv40/${pid}/apply-sync`, {
    method: 'POST',
    body: JSON.stringify({ editor_config: config }),
  });
  if (data) {
    showToast(`Corrección aplicada. Nuevo Δ = ${data.sync_delta > 0 ? '+' : ''}${data.sync_delta}`, 'success');
    const project = openCMv40Projects.find(p => p.id === pid);
    if (project) {
      project.syncData = null;  // forzar recarga
      _cmv40AssignSession(project, data);
      if (!project.expandedPhases) project.expandedPhases = {};
      project.expandedPhases['D'] = true;  // mantener la fase D visible
      _updateCMv40Panel(project);
      // Los inputs se re-renderizan pre-rellenados con el nuevo delta
      // (evita aplicar dos veces el mismo valor por despiste)
    }
  }
}

async function cmv40DoSkipSync(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}/mark-synced`, { method: 'POST' });
  if (data) {
    showToast('Sync confirmado', 'success');
    const project = openCMv40Projects.find(p => p.id === pid);
    if (project) {
      _cmv40AssignSession(project, data);
      _updateCMv40Panel(project);
      refreshCMv40Sidebar();
      // Si auto está activo, disparar el siguiente tramo (inject → remux → validate)
      if (project.autoContinue) {
        _cmv40MaybeAutoAdvance(project);
      }
    }
  }
}

// ── Chart Canvas (custom, sin librerías) ─────────────────────────

function _renderCMv40Chart(project) {
  const pid = project.id;
  const canvas = document.getElementById(`cmv40-chart-${pid}`);
  if (!canvas) return;
  const allData = project.syncData?.data || [];
  if (allData.length === 0) return;

  // Framerate real del vídeo origen
  const FPS = project.session.source_fps || 23.976;
  // totalFrames real de la película (NO es allData.length por muestreo)
  // No usar Math.max(...array): el spread supera el límite de argumentos (~65k)
  // y lanza "Maximum call stack size exceeded" con arrays grandes (155k frames).
  const totalFrames = project.syncData.source_frames
    || (allData.reduce((m, p) => Math.max(m, p.frame || 0), 0) + 1);
  if (!project.chartRange) {
    project.chartRange = { start: 0, end: Math.min(Math.round(30 * FPS), totalFrames) };
  }
  const { start, end } = project.chartRange;
  // Filtrar por número de frame real (no por índice del array)
  const data = allData.filter(p => p.frame >= start && p.frame < end);
  if (data.length === 0) return;

  const ctx = canvas.getContext('2d');
  const W = canvas.width;
  const H = canvas.height;
  const padding = { top: 20, right: 20, bottom: 40, left: 60 };
  const plotW = W - padding.left - padding.right;
  const plotH = H - padding.top - padding.bottom;

  // Reduce en vez de spread — evita "Max call stack" con arrays > ~65k
  let srcMax = 0, tgtMax = 0;
  for (let i = 0; i < data.length; i++) {
    const s = data[i].src_maxcll || 0;
    const t = data[i].tgt_maxcll || 0;
    if (s > srcMax) srcMax = s;
    if (t > tgtMax) tgtMax = t;
  }
  const yMax = Math.max(srcMax, tgtMax, 100) * 1.1;
  // Ancho en frames del rango visible (para mapeo X)
  const rangeSpan = end - start;

  // Fondo
  ctx.fillStyle = '#1a1a1a';
  ctx.fillRect(0, 0, W, H);

  // Grid horizontal
  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.lineWidth = 1;
  ctx.font = '10px sans-serif';
  ctx.fillStyle = 'rgba(255,255,255,0.5)';
  for (let i = 0; i <= 5; i++) {
    const y = padding.top + (plotH * i / 5);
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(padding.left + plotW, y);
    ctx.stroke();
    const val = (yMax * (1 - i / 5)).toFixed(0);
    ctx.fillText(`${val} PQ`, 4, y + 3);
  }
  // Eje X (frames + tiempo) — 6 labels bien espaciados
  const NUM_X_LABELS = 6;
  ctx.textAlign = 'center';
  for (let i = 0; i <= NUM_X_LABELS; i++) {
    const x = padding.left + (plotW * i / NUM_X_LABELS);
    const frame = Math.round(start + (rangeSpan * i / NUM_X_LABELS));
    const mm = Math.floor(frame / FPS / 60);
    const ss = Math.floor((frame / FPS) % 60).toString().padStart(2, '0');
    // Marca del tick
    ctx.strokeStyle = 'rgba(255,255,255,0.2)';
    ctx.beginPath();
    ctx.moveTo(x, padding.top + plotH);
    ctx.lineTo(x, padding.top + plotH + 4);
    ctx.stroke();
    // Labels
    ctx.fillStyle = 'rgba(255,255,255,0.7)';
    ctx.fillText(`${mm}:${ss}`, x, H - 22);
    ctx.fillStyle = 'rgba(255,255,255,0.4)';
    ctx.font = '9px sans-serif';
    ctx.fillText(`f ${frame.toLocaleString()}`, x, H - 8);
    ctx.font = '10px sans-serif';
  }
  ctx.textAlign = 'left';

  // Helper: frame absoluto → posición X en el canvas
  const frameToX = (frame) => padding.left + (plotW * (frame - start) / rangeSpan);

  // Curva target (azul) — se dibuja primero, más gruesa y con cierta transparencia
  ctx.strokeStyle = 'rgba(59, 130, 246, 0.85)';
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  data.forEach((d, i) => {
    const x = frameToX(d.frame);
    const y = padding.top + plotH - (plotH * (d.tgt_maxcll || 0) / yMax);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Curva source (rojo) — encima, más fina y punteada para que se vea cuando coincide
  ctx.strokeStyle = '#ef4444';
  ctx.lineWidth = 1.2;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  data.forEach((d, i) => {
    const x = frameToX(d.frame);
    const y = padding.top + plotH - (plotH * (d.src_maxcll || 0) / yMax);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.setLineDash([]);

  // Leyenda — origen con guiones (reflejando cómo se dibuja)
  ctx.fillStyle = '#3b82f6';
  ctx.fillRect(padding.left + 10, 7, 14, 3);
  ctx.fillStyle = 'rgba(255,255,255,0.8)';
  ctx.fillText('RPU target (CMv4.0)', padding.left + 30, 12);
  ctx.strokeStyle = '#ef4444';
  ctx.lineWidth = 1.5;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  ctx.moveTo(padding.left + 180, 8);
  ctx.lineTo(padding.left + 196, 8);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = 'rgba(255,255,255,0.8)';
  ctx.fillText('MKV origen (CMv2.9)', padding.left + 202, 12);
  // Info de rango prominente (arriba a la derecha)
  const startSec = start / FPS, endSec = end / FPS;
  const fmtTime = (s) => {
    const mm = Math.floor(s / 60), ss = Math.floor(s % 60).toString().padStart(2, '0');
    return `${mm}:${ss}`;
  };
  ctx.textAlign = 'right';
  ctx.fillStyle = 'rgba(255,255,255,0.9)';
  ctx.font = '11px sans-serif';
  ctx.fillText(`Rango: ${fmtTime(startSec)} — ${fmtTime(endSec)}`, W - padding.right, 14);
  ctx.fillStyle = 'rgba(255,255,255,0.5)';
  ctx.font = '10px sans-serif';
  ctx.fillText(`(${(end - start).toLocaleString()} de ${totalFrames.toLocaleString()} frames · ${FPS.toFixed(2)} fps)`, W - padding.right, 28);
  ctx.textAlign = 'left';

  // Hover handler
  canvas.onmousemove = (e) => {
    const rect = canvas.getBoundingClientRect();
    const scaleX = W / rect.width;
    const mx = (e.clientX - rect.left) * scaleX;
    if (mx < padding.left || mx > padding.left + plotW) return;
    // Posición X → frame absoluto
    const absFrame = Math.round(start + ((mx - padding.left) / plotW) * rangeSpan);
    // Buscar el datapoint más cercano al frame
    const d = data.reduce((closest, p) =>
      Math.abs(p.frame - absFrame) < Math.abs(closest.frame - absFrame) ? p : closest,
      data[0]
    );
    if (!d) return;
    const tooltip = document.getElementById(`cmv40-chart-tooltip-${project.id}`);
    if (tooltip) {
      tooltip.style.display = '';
      tooltip.style.left = `${e.clientX - rect.left + 10}px`;
      tooltip.style.top  = `${e.clientY - rect.top - 30}px`;
      const mm = Math.floor(absFrame / FPS / 60);
      const ss = Math.floor((absFrame / FPS) % 60).toString().padStart(2, '0');
      tooltip.innerHTML = `Frame ${absFrame.toLocaleString()} (${mm}:${ss})<br>
        <span style="color:#ef4444">Origen: ${(d.src_maxcll || 0).toFixed(0)} PQ</span><br>
        <span style="color:#3b82f6">Target: ${(d.tgt_maxcll || 0).toFixed(0)} PQ</span>`;
    }
  };
  canvas.onmouseleave = () => {
    const tooltip = document.getElementById(`cmv40-chart-tooltip-${project.id}`);
    if (tooltip) tooltip.style.display = 'none';
  };
}

// ══════════════════════════════════════════════════════════════════════
//  FILE BROWSER — modal reusable para navegar /mnt/library
//  Usado en Tab 3 para seleccionar el MKV origen del proceso CMv4.0.
//  El backend (`/api/library/browse`) sirve subdirs + ficheros .mkv.
// ══════════════════════════════════════════════════════════════════════

const _fileBrowser = {
  // Roots disponibles. 1 root = sin selector visible. 2+ = pills arriba.
  roots: [],            // [{key, label, icon}]
  rootKey: 'library',   // key del root activo
  base: '/mnt/library', // ruta absoluta del root activo (devuelta por backend)
  currentPath: '',      // relativa al root activo
  parent: null,
  entries: [],
  filter: '',
  onSelect: null,
  // Selección actual — null hasta que el usuario haga click en una fila
  // de fichero. El boton "Seleccionar" del footer queda disabled hasta
  // entonces; click sobre otra fila reemplaza la seleccion. Doble-click
  // sobre una fila confirma directamente (atajo power-user).
  selectedRel: null,
  selectedName: null,
};

const _DEFAULT_FB_ROOTS = [
  { key: 'library', label: 'Biblioteca', icon: '📚' },
];
const _FB_ROOT_LABELS = {
  library:    'Biblioteca',
  output:     'Output',
  downloaded: 'Downloaded',
};

/** Abre el modal del file browser.
 *  opts: { title, subtitle, roots, onSelect }
 *    - roots: array de {key, label, icon}. Default = [Biblioteca].
 *      Si hay 2+ roots, se muestra un selector de pills arriba.
 *      El primer root del array es el que se carga al abrir.
 *  CRUCIAL: el modal se abre ANTES de hacer el fetch para evitar gaps
 *  visuales (la app de fondo no debe ser interactuable). La lista
 *  muestra "⏳ Cargando…" hasta que el fetch termina. */
async function openFileBrowser({ title, subtitle, roots, onSelect } = {}) {
  _fileBrowser.onSelect = onSelect || null;
  _fileBrowser.filter = '';
  _fileBrowser.selectedRel = null;
  _fileBrowser.selectedName = null;
  _fileBrowser.roots = (roots && roots.length) ? roots : _DEFAULT_FB_ROOTS;
  _fileBrowser.rootKey = _fileBrowser.roots[0].key;

  const titleEl = document.getElementById('file-browser-title');
  const subEl = document.getElementById('file-browser-sub');
  const searchEl = document.getElementById('file-browser-search');
  const listEl = document.getElementById('file-browser-list');
  const bcEl = document.getElementById('file-browser-breadcrumb');
  const baseEl = document.getElementById('file-browser-base');
  const statsEl = document.getElementById('file-browser-stats');
  if (titleEl) titleEl.textContent = title || 'Seleccionar MKV';
  if (subEl) subEl.textContent = subtitle || 'Navega tu biblioteca y elige el fichero';
  if (searchEl) searchEl.value = '';
  // Limpiar restos de aperturas anteriores ANTES de mostrar para no flashear datos viejos
  if (listEl) listEl.innerHTML = '<div class="file-browser-loading">⏳ Cargando…</div>';
  if (bcEl) bcEl.innerHTML = '';
  if (baseEl) baseEl.textContent = '';
  if (statsEl) statsEl.textContent = '';
  _renderFileBrowserRoots();
  // Modal arriba YA — antes de cualquier await. Mantiene cobertura modal
  // sin gaps cuando se invoca durante una transicion (close→open de otro modal).
  openModal('file-browser-modal');
  setTimeout(() => searchEl?.focus(), 80);
  await fileBrowserNavigate('');
}

/** Pinta el selector de roots (pills arriba del breadcrumb). Solo visible
 *  cuando hay 2+ roots configurados; con 1 solo se oculta. */
function _renderFileBrowserRoots() {
  const el = document.getElementById('file-browser-roots');
  if (!el) return;
  if (!_fileBrowser.roots || _fileBrowser.roots.length < 2) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = 'flex';
  el.innerHTML = '';
  _fileBrowser.roots.forEach(r => {
    const btn = document.createElement('button');
    btn.className = `fb-root-btn ${r.key === _fileBrowser.rootKey ? 'active' : ''}`;
    btn.innerHTML = `<span class="fb-root-icon">${r.icon || '📁'}</span><span class="fb-root-label">${escHtml(r.label)}</span>`;
    btn.addEventListener('click', () => {
      if (r.key === _fileBrowser.rootKey) return;
      _fileBrowser.rootKey = r.key;
      _renderFileBrowserRoots();
      fileBrowserNavigate('');
    });
    el.appendChild(btn);
  });
}

/** Carga el contenido de `relPath` (relativo al root activo) y re-renderiza.
 *  Limpia la seleccion previa: cambiar de directorio implica reset, igual
 *  que hace Finder/Explorer. */
async function fileBrowserNavigate(relPath) {
  _fileBrowser.selectedRel = null;
  _fileBrowser.selectedName = null;
  _updateFileBrowserConfirmBtn();
  const listEl = document.getElementById('file-browser-list');
  if (listEl) listEl.innerHTML = '<div class="file-browser-loading">⏳ Cargando…</div>';
  try {
    const url = `/api/library/browse?root=${encodeURIComponent(_fileBrowser.rootKey)}&path=${encodeURIComponent(relPath || '')}`;
    const data = await apiFetch(url);
    if (!data) throw new Error('Sin respuesta');
    if (data.error) {
      if (listEl) listEl.innerHTML = `<div class="file-browser-empty">${escHtml(data.error)}</div>`;
      return;
    }
    _fileBrowser.base = data.base || '';
    _fileBrowser.currentPath = data.path || '';
    _fileBrowser.parent = data.parent;
    _fileBrowser.entries = data.entries || [];
    _renderFileBrowser();
  } catch (e) {
    if (listEl) listEl.innerHTML = `<div class="file-browser-empty">⚠ Error: ${escHtml(e.message || String(e))}</div>`;
  }
}

/** Sube un nivel en el árbol (si no estás en la raíz). */
function fileBrowserUp() {
  if (_fileBrowser.parent === null || _fileBrowser.parent === undefined) return;
  fileBrowserNavigate(_fileBrowser.parent);
}

/** Filtra la lista por substring (in-memory, no recarga del servidor). */
function fileBrowserFilter(value) {
  _fileBrowser.filter = (value || '').toLowerCase().trim();
  _renderFileBrowser();
}

function _renderFileBrowser() {
  const listEl = document.getElementById('file-browser-list');
  const bcEl = document.getElementById('file-browser-breadcrumb');
  const baseEl = document.getElementById('file-browser-base');
  const upBtn = document.getElementById('file-browser-up-btn');
  const statsEl = document.getElementById('file-browser-stats');
  if (!listEl || !bcEl) return;

  // ── Breadcrumb (DOM, no strings con encoding) ───────────────────
  const path = _fileBrowser.currentPath || '';
  const parts = path.split('/').filter(Boolean);
  bcEl.innerHTML = '';
  const rootLink = document.createElement('a');
  // Etiqueta del root activo en el inicio del breadcrumb. Buscar en
  // los roots configurados; fallback al mapping de defaults.
  const activeRoot = (_fileBrowser.roots || []).find(r => r.key === _fileBrowser.rootKey);
  const rootIcon = activeRoot?.icon || '📁';
  const rootLabel = activeRoot?.label || _FB_ROOT_LABELS[_fileBrowser.rootKey] || _fileBrowser.rootKey;
  rootLink.textContent = `${rootIcon} ${rootLabel}`;
  rootLink.addEventListener('click', () => fileBrowserNavigate(''));
  bcEl.appendChild(rootLink);
  parts.forEach((part, i) => {
    const subPath = parts.slice(0, i + 1).join('/');
    const isLast = i === parts.length - 1;
    const sep = document.createElement('span');
    sep.className = 'fb-bc-sep';
    sep.textContent = '›';
    bcEl.appendChild(sep);
    if (isLast) {
      const cur = document.createElement('span');
      cur.className = 'fb-bc-current';
      cur.textContent = part;
      bcEl.appendChild(cur);
    } else {
      const link = document.createElement('a');
      link.textContent = part;
      link.addEventListener('click', () => fileBrowserNavigate(subPath));
      bcEl.appendChild(link);
    }
  });
  if (baseEl) baseEl.textContent = _fileBrowser.base + (path ? '/' + path : '');
  if (upBtn) upBtn.disabled = _fileBrowser.parent === null || _fileBrowser.parent === undefined;

  // ── Filtro in-memory ────────────────────────────────────────────
  const filter = _fileBrowser.filter;
  const filtered = filter
    ? _fileBrowser.entries.filter(e => e.name.toLowerCase().includes(filter))
    : _fileBrowser.entries;

  // ── Lista (DOM, no innerHTML con paths encoded) ─────────────────
  listEl.innerHTML = '';
  if (!filtered.length) {
    const empty = document.createElement('div');
    empty.className = 'file-browser-empty';
    empty.textContent = filter
      ? `Sin coincidencias para "${filter}"`
      : '📭 Esta carpeta no contiene MKVs ni subcarpetas.';
    listEl.appendChild(empty);
    if (statsEl) statsEl.textContent = '';
    return;
  }

  const dirs = filtered.filter(e => e.type === 'dir').length;
  const files = filtered.filter(e => e.type === 'file').length;
  if (statsEl) {
    statsEl.textContent = (dirs ? `${dirs} ${dirs === 1 ? 'carpeta' : 'carpetas'}` : '')
                        + (dirs && files ? ' · ' : '')
                        + (files ? `${files} ${files === 1 ? 'MKV' : 'MKVs'}` : '');
  }

  filtered.forEach(e => {
    // childRel: ruta RELATIVA al base, sin encoding (lo encoda
    // fileBrowserNavigate en su fetch — antes había doble encoding
    // que rompía paths con espacios o tildes)
    const childRel = path ? `${path}/${e.name}` : e.name;
    const row = document.createElement('div');
    row.className = `file-browser-row ${e.type}`;
    if (e.type === 'file' && _fileBrowser.selectedRel === childRel) {
      row.classList.add('selected');
    }
    row.tabIndex = 0;
    row.innerHTML = `
      <span class="fb-icon">${e.type === 'dir' ? '📁' : '🎬'}</span>
      <span class="fb-name">${escHtml(e.name)}</span>
      ${e.type === 'file' ? `<span class="fb-size">${_fmtBytes(e.size_bytes)}</span>` : ''}
    `;
    if (e.type === 'dir') {
      // Carpetas: click navega (entrar es la unica accion posible)
      row.addEventListener('click', () => fileBrowserNavigate(childRel));
    } else {
      // Ficheros: click SELECCIONA (visual highlight + boton "Seleccionar"
      // habilitado). Confirmar requiere clicar el boton del footer o
      // doble-click sobre la fila (atajo power-user).
      row.addEventListener('click', () => _fileBrowserSelectRow(childRel, e.name));
      row.addEventListener('dblclick', () => _fileBrowserConfirmSelection());
    }
    // Soporte teclado: Enter activa la fila (mismo flujo que click)
    row.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        row.click();
      }
    });
    listEl.appendChild(row);
  });
  _updateFileBrowserConfirmBtn();
}

/** Marca una fila de fichero como seleccionada (sin confirmar). */
function _fileBrowserSelectRow(rel, name) {
  _fileBrowser.selectedRel = rel;
  _fileBrowser.selectedName = name;
  // Re-render para refrescar el highlight visual + estado del boton
  _renderFileBrowser();
}

/** Confirma la seleccion actual: llama a onSelect y cierra el modal. */
function _fileBrowserConfirmSelection() {
  if (!_fileBrowser.selectedRel) return;
  _fileBrowserSelect(_fileBrowser.selectedRel, _fileBrowser.selectedName);
}

/** Habilita/deshabilita el boton "Seleccionar" segun haya seleccion. */
function _updateFileBrowserConfirmBtn() {
  const btn = document.getElementById('file-browser-confirm-btn');
  if (!btn) return;
  btn.disabled = !_fileBrowser.selectedRel;
}

/** Confirma selección de un fichero. Espera a que onSelect termine (puede
 *  abrir otro modal con su propio fetch) ANTES de cerrar el browser → asi
 *  el usuario nunca ve la app de fondo: el browser cubre la transicion
 *  hasta que el siguiente modal este renderizado. Mientras espera, el
 *  browser se "congela" deshabilitando pointer events para que el usuario
 *  no pueda lanzar otro click sobre filas. */
async function _fileBrowserSelect(relPath, name) {
  const absPath = `${_fileBrowser.base.replace(/\/$/, '')}/${relPath}`;
  const modal = document.getElementById('file-browser-modal');
  if (modal) modal.style.pointerEvents = 'none';
  try {
    if (typeof _fileBrowser.onSelect === 'function') {
      await _fileBrowser.onSelect(absPath, name);
    }
  } catch (e) {
    console.error('FileBrowser onSelect error:', e);
  } finally {
    if (modal) modal.style.pointerEvents = '';
    closeModal('file-browser-modal');
  }
}

