'use strict';
/**
 * core.js — Estado de la aplicación, arranque y las primitivas de UI.
 *
 * Lo que usan las tres pestañas: la tabla de idiomas, el estado global
 * (`_sessionsCache`, los proyectos abiertos…), los helpers de proyecto, el
 * `DOMContentLoaded` que arranca todo, el gestor de tooltips, el conmutador de
 * tabs y sub-tabs, los toasts, el diálogo de confirmación y los helpers de
 * modal. Se carga PRIMERO: el resto declara funciones, pero esto declara las
 * constantes que los demás leen.
 */

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
  // Modelo de ETA medido del histórico. Se refresca cada 10 min: cada job
  // que termina lo afina, así que sigue a los cambios del pipeline solo.
  _cmv40LoadEtaModel();
  setInterval(_cmv40LoadEtaModel, 600000);
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
  // Con la pestaña oculta no hay puntos que pintar, y esto son dos peticiones
  // cada 5 s durante horas contra un NAS que además está procesando vídeo. Al
  // volver a primer plano, `visibilitychange` dispara la recuperación y con
  // ella el refresco.
  if (document.hidden) return;
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
  // Tab 3: cualquier sesión con running_phase. Endpoint dedicado — pedir
  // /api/cmv40 entero para esto costaba 569 KB y 193 ms cada 5s (~10% de un
  // core del NAS) solo para decidir si se pinta un punto.
  try {
    const data = await apiFetch('/api/cmv40-active', { silent: true });
    setDot(3, !!data?.active);
  } catch (_) { setDot(3, false); }
}

/**
 * Tras Mac sleep / cambio de pestaña / suspend de red, los WebSockets
 * mueren y los timers de polling pueden quedarse sin actualizar la UI.
 * Cuando el documento vuelve a ser visible, fuerza un refresh del estado
 * vía API y reconecta los WS si las sesiones siguen corriendo. Cubre
 * los tres tabs:
 *   - Tab 1 (ISO→MKV): sessions list + queue WS + executionWs activo
 *   - Tab 2 (Editar MKV): el análisis extendido ya es resiliente
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
  // Los puntos verdes de los tabs: su poller se salta las vueltas con la
  // pestaña oculta, así que al volver hay que refrescarlos aquí o se quedan
  // como estaban al ocultarse.
  _refreshTabRunningDots();

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
  // Queue WS — reconnect agresivo (no chequea readyState). Marcamos
  // _closedByUser para que el onclose no programe SU PROPIO reconnect (sería
  // por duplicado con el connectQueueWebSocket() de abajo). audit #25.
  if (typeof queueWs !== 'undefined' && queueWs) {
    try { queueWs._closedByUser = true; queueWs.close(); } catch (_) {}
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
  // Tab 2 — el análisis extendido: si el job ya terminó en el backend, el
  // modal puede haberse quedado esperando un POST que murió con el sleep del
  // Mac. Se aborta el fetch y se recoge el resultado del state.
  if (window._mkvQualitySession?.ctrl) {
    apiFetch('/api/mkv/quality-audit/progress', { silent: true }).then(st => {
      if (st && st.active === false && (st.result || st.error)) {
        window._mkvQualitySession.polledResult = st.result || null;
        try { window._mkvQualitySession.ctrl?.abort(); } catch (_) {}
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
  // Pelis usan el mkv_name (ya saneado por el backend: sin tags del release);
  // fallback al basename del ISO solo si aún no hay mkv_name.
  let name;
  if (session.media_type === 'series') {
    const sn = String(session.season_number || 0).padStart(2, '0');
    const en = String(session.episode_number || 0).padStart(2, '0');
    const yearPart = session.series_year ? ` (${session.series_year})` : '';
    const base = `${session.series_name || 'Serie'}${yearPart} - S${sn}E${en}`;
    name = session.episode_title ? `${base} - ${session.episode_title}` : base;
  } else if (session.mkv_name) {
    name = session.mkv_name.replace(/\.mkv$/i, '');
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
          <div id="${pid}-mkv-dcp-chip" class="globals-mkv-chip" style="display:none"
            data-tooltip="El nombre del ISO contiene el tag 'Audio DCP'.\nAñade el sufijo (DCP 9.1.6) a la pista TrueHD Atmos en Castellano.">
            🎵 Audio DCP — detectado en el nombre del ISO
          </div>
        </div>
        <div class="globals-info-row">
          <div class="global-info-item" id="${pid}-dv-card">
            <span class="global-card-icon" id="${pid}-dv-icon">🎬</span>
            <div class="global-info-body">
              <div class="global-info-head">
                <span class="global-card-label" id="${pid}-dv-state">—</span>
                <span class="global-info-chip" id="${pid}-dv-tag" style="display:none"
                  data-tooltip="Tag que se añade automáticamente al nombre del MKV."></span>
              </div>
              <div class="global-info-line" id="${pid}-dv-detail"></div>
              <div class="global-info-line global-info-line--note" id="${pid}-dv-note" style="display:none"></div>
            </div>
          </div>
          <div class="global-info-item" id="${pid}-vhdr-card">
            <span class="global-card-icon">📺</span>
            <div class="global-info-body">
              <div class="global-info-head">
                <span class="global-card-label">Vídeo · HDR</span>
              </div>
              <div class="global-info-line" id="${pid}-vhdr-codec"></div>
              <div class="global-info-line" id="${pid}-vhdr-hdr"></div>
              <div class="global-info-line" id="${pid}-vhdr-color"></div>
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
            data-tooltip="Extrae los capítulos originales del disco (MPLS) y reemplaza los actuales (automáticos o editados).">🔄 Restaurar del disco</button>
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
