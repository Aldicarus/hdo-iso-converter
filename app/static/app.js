'use strict';

/**
 * @fileoverview HDO ISO Converter — Frontend SPA (Fase C de la pipeline)
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

/** Máximo de proyectos abiertos simultáneamente. */
const MAX_PROJECTS = 5;

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
});

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

  // Refrescar sidebar Tab 3 al entrar
  if (n === 3 && typeof refreshCMv40Sidebar === 'function') {
    refreshCMv40Sidebar();
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
  const name = session.iso_path
    ? session.iso_path.replace(/\\/g, '/').split('/').pop().replace(/\.iso$/i, '')
    : session.id;

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
      <div><strong>ISO no disponible.</strong>
        <span id="${pid}-iso-missing-text"></span>
        Puedes editar los parámetros, pero no podrás ejecutar hasta que el ISO vuelva a estar accesible.
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
          data-tooltip="Solo Castellano + VO + Inglés, con detección de audiodescripción">🎯 Castellano + VO + Inglés</button>
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
  openModal('settings-modal');
  setTimeout(() => document.getElementById('settings-tmdb-input')?.focus(), 50);
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
      <li><strong>Merge con P7 clásico</strong>: el bin es P8.x retail (WEB-DL) pero tu BD es P7 FEL. La app transfiere los trims L8-L11 del bin al RPU P7 preservando todo el EL del Blu-ray. Resultado: P7 FEL CMv4.0 completo.</li>
      <li><strong>P7 MEL → P8.1</strong>: tu BD es MEL, que aporta poco. La app descarta el EL y usa solo la BL + RPU CMv4.0. El resultado es un MKV P8.1 single-layer, más ligero y sin pérdida visual real.</li>
      <li><strong>P8 directo</strong>: source y target son P8 — inyección limpia, single-layer.</li>
    </ul>

    <h3>Fase G — Ensamblar el MKV final</h3>
    <p>El vídeo con el RPU CMv4.0 se junta con el audio, subtítulos y capítulos del Blu-ray original. El MKV resultante se escribe con una barra de progreso real (no estimada). Se escribe con sufijo temporal y se renombra atómicamente al nombre final al acabar — si la app se corta a mitad, nunca queda un MKV a medias con el nombre definitivo.</p>

    <h3>🛡️ Validación final — antes de Fase H</h3>
    <p>Igual que en el punto B→C, aquí hay otro <em>gate</em> entre G y H: la app verifica que el HEVC ensamblado contiene efectivamente CMv4.0, que el número de frames coincide con el BD original, y que la estructura del fichero Matroska es correcta. Si algo falla, el MKV se rechaza y el proyecto se marca con error (se puede rehacer desde la fase que quieras).</p>
    <div class="help-callout help-callout-info">
      <strong>Detalle de implementación histórico:</strong> la validación lee el RPU del HEVC en el directorio de trabajo, no del MKV final. Esto evitaba un bug conocido de <code>dovi_tool</code> 2.1.x que fallaba al leer RPUs desde algunos MKVs por cómo se almacenan los PPS en <code>CodecPrivate</code>. El contenedor ya usa 2.3.2, donde el bug está corregido y la lectura directa del MKV es estable; el workaround se mantiene como defensa adicional hasta validar la retirada en un proyecto real.
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
      <div class="help-pipeline-diagram-sub"><span class="help-pill help-pill-retail">Retail</span> El bin es un P8.1 retail típico (por ejemplo un WEB-DL reciente de la película). La app transfiere los trims L8-L11 del bin al RPU P7 del Blu-ray preservando el EL completo. Resultado final: P7 FEL CMv4.0 con toda la calidad del disco + los niveles nuevos.</div>
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
      <div class="help-pipeline-diagram-sub"><span class="help-pill help-pill-retail">Retail</span> Hay bin P8.1 de otra edición (sin los marcadores del repo tipo 'trusted_p8_source') pero con CMv4.0 válido. Mismo flujo que los anteriores: descartar EL, inyectar target en BL → P8.1 CMv4.0.</div>
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
      <div class="help-pipeline-diagram-sub"><span class="help-pill help-pill-retail">Retail</span> Variante del anterior con un bin P8.x de otra edición. Mismo flujo: reemplazo del RPU in-place sobre el HEVC existente → P8.1 CMv4.0 refinado.</div>
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
          <li><em>Application name</em>: <strong>HDO ISO Converter</strong> (o el nombre que quieras)</li>
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
          <li><em>Nombre</em>: <strong>HDO ISO Converter</strong> (o lo que quieras)</li>
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
    document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
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

/** ISO seleccionado en el picker. @type {string|null} */
let pickerSelectedIso = null;

/** Abre el modal de nuevo proyecto y carga la lista de ISOs en el select. */
async function openNewProjectModal() {
  if (openProjects.length >= MAX_PROJECTS) {
    showToast(`Máximo ${MAX_PROJECTS} proyectos abiertos. Cierra uno antes de crear otro.`, 'warning');
    return;
  }
  pickerSelectedIso = null;
  document.getElementById('iso-picker-select').value = '';
  document.getElementById('new-project-analyze-btn').disabled = true;
  openModal('new-project-modal');
  await loadIsoPickerList();
}

/** Almacena el último listado de ISOs cargado. @type {string[]} */
let _isoPickerCache = [];

/** Carga los ISOs disponibles vía /api/isos y rellena el select + status. */
async function loadIsoPickerList() {
  const sel    = document.getElementById('iso-picker-select');
  const status = document.getElementById('iso-picker-status');
  sel.innerHTML = '<option value="">Cargando…</option>';
  sel.disabled  = true;
  status.className = 'iso-picker-status';
  status.textContent = '⏳ Consultando /mnt/isos…';

  const data = await apiFetch('/api/isos');

  if (!data) {
    sel.innerHTML = '<option value="">Error al cargar</option>';
    status.className = 'iso-picker-status error';
    status.textContent = '❌ No se pudo conectar con el servidor. Comprueba que el backend está en marcha.';
    sel.disabled = false;
    return;
  }

  _isoPickerCache = data.isos;
  sel.disabled = false;

  if (!data.isos.length) {
    sel.innerHTML = '<option value="">— No hay ISOs disponibles —</option>';
    status.className = 'iso-picker-status warn';
    status.textContent = '⚠️ No se encontraron ficheros .iso en /mnt/isos. Comprueba el volumen Docker.';
    document.getElementById('new-project-analyze-btn').disabled = true;
    return;
  }

  sel.innerHTML = '<option value="">— Seleccionar ISO —</option>';
  data.isos.forEach(iso => {
    const name = iso.replace(/\\/g, '/').split('/').pop().replace(/\.iso$/i, '');
    const opt  = document.createElement('option');
    opt.value  = iso;
    opt.textContent = name;
    sel.appendChild(opt);
  });

  status.className = 'iso-picker-status ok';
  status.textContent = `✅ ${data.isos.length} ISO${data.isos.length !== 1 ? 's' : ''} encontrado${data.isos.length !== 1 ? 's' : ''} en /mnt/isos`;

  // Restaurar selección previa si sigue disponible
  if (pickerSelectedIso && data.isos.includes(pickerSelectedIso)) {
    sel.value = pickerSelectedIso;
    document.getElementById('new-project-analyze-btn').disabled = false;
  }
}

/** Actualiza pickerSelectedIso al cambiar el select nativo. */
function onIsoPickerChange() {
  const sel = document.getElementById('iso-picker-select');
  pickerSelectedIso = sel.value || null;
  document.getElementById('new-project-analyze-btn').disabled = !pickerSelectedIso;
}

/**
 * Analiza el ISO seleccionado en el picker.
 * Cierra el modal, dispara Fase A+B y abre el proyecto resultante.
 */
async function analyzeSelectedISO() {
  if (!pickerSelectedIso) return;
  if (openProjects.length >= MAX_PROJECTS) {
    showToast(`Máximo ${MAX_PROJECTS} proyectos abiertos.`, 'warning');
    return;
  }

  const isoPath = pickerSelectedIso;
  const isoName = isoPath.split('/').pop();

  // Deshabilitar botón dentro del modal mientras comprobamos
  const btn = document.getElementById('new-project-analyze-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '⏳ Comprobando…'; }

  // Comprobar si ya existe un proyecto para este ISO (por huella, no por nombre/ruta)
  const check = await apiFetch('/api/check-duplicate', {
    method: 'POST',
    body: JSON.stringify({ iso_path: isoPath }),
  });

  // Restaurar botón por si se cancela
  if (btn) { btn.disabled = false; btn.innerHTML = '💿 Analizar ISO'; }

  if (check?.duplicate && check.session) {
    closeModal('new-project-modal');
    const existingName = check.session.mkv_name || isoName;
    // Ofrecer abrir existente o re-analizar
    const okBtn = document.getElementById('confirm-ok-btn');
    showConfirm(
      'Este disco ya tiene un proyecto',
      `Se ha detectado el mismo disco en "${existingName}". Puedes abrir el proyecto existente o re-analizar el disco (se perderán las ediciones actuales).`,
      () => _doAnalyzeISO(isoPath, isoName),
      'Re-analizar',
    );
    // Añadir botón extra "Abrir existente"
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
  await _doAnalyzeISO(isoPath, isoName);
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
        for (let i = prevIdx; i < newIdx; i++) {
          _advanceAnalyzeStep(steps[i], steps[i + 1]);
        }
        lastStep = prog.step;
        stepStartTs = Date.now();
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
        if (labelEl) labelEl.textContent = '⏳ Contando paquetes PGS por subtítulo…';
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
    showToast(`Error al analizar ${escHtml(isoName)}. Comprueba que el ISO es válido y el contenedor tiene privileged: true.`, 'error');
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

/** Devuelve el nodo de texto visible del paso (label directo o anidado para pgs). */
function _analyzeStepLabelNode(stepKey) {
  // El paso pgs tiene estructura compleja (label + bar + stats)
  if (stepKey === 'pgs') return document.getElementById('analyze-step-pgs-label');
  return document.getElementById(`analyze-step-${stepKey}`);
}

/** Resetea todos los pasos del modal de análisis al estado inicial. */
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
  const data = await apiFetch('/api/sessions');
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

function _doFilterSidebarSessions() {
  const query = normalizeSearch(document.getElementById('sidebar-search')?.value || '');
  if (!query) {
    renderSidebarSessions(_sessionsCache);
    return;
  }
  const filtered = _sessionsCache.filter(s => {
    const name = s.mkv_name
      ? s.mkv_name.replace(/\.mkv$/i, '')
      : s.id.replace(/_\d+$/, '').replace(/_/g, ' ');
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
      const name = s.mkv_name
        ? s.mkv_name.replace(/\.mkv$/i, '')
        : s.id.replace(/_\d+$/, '').replace(/_/g, ' ');
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

    const name = s.mkv_name
      ? s.mkv_name.replace(/\.mkv$/i, '')
      : s.id.replace(/_\d+$/, '').replace(/_/g, ' ');

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
  // Parseamos el título del mkv_name (o del basename del ISO si aún no
  // hay mkv_name). Cache global evita re-fetches entre cambios.
  const filenameForTmdb = session.mkv_name
    || (session.iso_path ? session.iso_path.split('/').pop() : '');
  hydrateTmdbCard(`${project.id}-tmdb-card`, filenameForTmdb);

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
    const isoName = (data.iso_path || '').replace(/\\/g, '/').split('/').pop();
    setText('iso-missing-text', ` El fichero "${isoName}" ya no se encuentra en /mnt/isos.`);
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
      // Orden: descripción (canales + kHz) + bitrate siempre visible (aunque caiga ellipsis).
      // Omitimos raw.codec porque ya aparece en el label (DD+, TrueHD Atmos, etc.).
      const rawLine = [raw.description, raw.bitrate_kbps ? `${raw.bitrate_kbps.toLocaleString()} kbps` : null].filter(Boolean).join(' · ');
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
        <div class="track-reason"><span>ℹ️</span><span>${escHtml(track.selection_reason || '')}</span></div>`;
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
        <div class="track-reason"><span>ℹ️</span><span>${escHtml(track.selection_reason || '')}</span></div>`;
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
        codecInfo = [langLit, raw.codec, raw.description,
          raw.bitrate_kbps ? `${raw.bitrate_kbps.toLocaleString()} kbps` : null
        ].filter(Boolean).join(' · ');
      } else {
        const packets = raw.packet_count || 0;
        const pktTag = packets > 0 ? `${packets.toLocaleString()} paq.` : '';
        codecInfo = [langLit, 'PGS', pktTag].filter(Boolean).join(' · ');
      }

      const icon = isAudio ? '🔊' : '💬';
      const div = document.createElement('div');
      div.className = 'discarded-item';
      div.innerHTML = `
        ${origLabel ? `<span class="track-orig-pos" data-tooltip="Posición original de la pista en el ISO">${origLabel}</span>` : ''}
        <span class="track-type-icon" data-tooltip="${escHtml(tooltip)}">${icon}</span>
        <div class="discarded-body">
          <div class="discarded-codec">${escHtml(codecInfo || 'Pista desconocida')}</div>
          <div class="discarded-reason">${escHtml(track.discard_reason || '')}</div>
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

  const text = lines.join('\n');
  document.getElementById('raw-analysis-content').textContent = text;
  openModal('raw-analysis-modal');
}

/** Copia los datos de análisis al portapapeles. */
function _copyRawAnalysis() {
  const pre = document.getElementById('raw-analysis-content');
  if (!pre) return;
  navigator.clipboard.writeText(pre.textContent).then(() => {
    showToast('Datos copiados al portapapeles.', 'success');
  });
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

  let codecLit, fullLabel;
  if (isAudio) {
    codecLit = _buildAudioCodecLiteral(raw, !!currentSession.audio_dcp);
    fullLabel = `${langLit} ${codecLit}`.trim() || 'Pista recuperada';
  } else {
    // Subtítulos: formato "{Idioma} Completos (PGS)" (asumimos completo al recuperar)
    codecLit = 'Completos (PGS)';
    fullLabel = `${langLit} ${codecLit}`.trim() || 'Pista recuperada';
  }

  const recovered = {
    track_type: track.track_type,
    position: 0,  // se renumera abajo
    raw: track.raw,
    label: fullLabel,
    flag_default: false,
    flag_forced: false,
    selection_reason: 'Recuperada manualmente por el usuario',
    language_literal: langLit,
    codec_literal: codecLit,
    subtitle_type: 'complete',
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

  // Verificar disponibilidad del ISO
  const check = await apiFetch(`/api/sessions/${currentSession.id}/check-iso`);
  if (!check) return; // error de red ya manejado por apiFetch
  if (!check.available) {
    const isoName = (check.iso_path || '').replace(/\\/g, '/').split('/').pop();
    showToast(`ISO no disponible: "${isoName}" no está en /mnt/isos. No se puede ejecutar.`, 'error');
    // Actualizar banner por si no estaba visible
    project.isoAvailable = false;
    const prevSubTab = activeSubTabId;
    activeSubTabId = project.id;
    setText('iso-missing-text', ` El fichero "${isoName}" ya no se encuentra en /mnt/isos.`);
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
  if (executionWs) executionWs.close();
  _colaLogLines = [];  // Limpiar log del trabajo anterior
  document.getElementById('csb-log-viewer') && (document.getElementById('csb-log-viewer').innerHTML = '');
  document.getElementById('pc-log-viewer')  && (document.getElementById('pc-log-viewer').innerHTML  = '');
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  executionWs = new WebSocket(`${proto}://${location.host}/ws/${sessionId}`);
  executionWs.onmessage = (e) => handleExecutionWsMessage(e.data, sessionId);
  executionWs.onclose = () => { executionWs = null; };
}

/**
 * Procesa mensajes del WebSocket de ejecución.
 * Solo alimenta el panel Cola — el panel de proyecto nunca muestra estado de ejecución.
 * @param {string} msg
 * @param {string} sessionId
 */
function handleExecutionWsMessage(msg) {
  if (msg === '__DONE__') {
    const finishedId = queueState.running;
    if (executionWs) { executionWs.close(); executionWs = null; }
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
    if (executionWs) { executionWs.close(); executionWs = null; }
    for (const ph of ['mount', 'extract', 'unmount']) updateColaMiniPipeline(ph, 'pending');
    updateSubtabQueuePill();
    showToast('Ejecución cancelada. Temporales limpiados.', 'info');
    loadSessions();
    if (cancelledId) refreshOpenProjectState(cancelledId);
    return;
  }

  if (msg.startsWith('__ERROR__')) {
    const failedId = queueState.running;
    if (executionWs) { executionWs.close(); executionWs = null; }
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

  // Detectar cambios de fase por marcadores en el log
  if (msg.includes('[Montando ISO]')) {
    updateColaMiniPipeline('mount', 'active');
    const el = document.getElementById('csb-phase-label');
    if (el) el.textContent = 'Montando ISO…';
    // Actualizar subtítulo de la fase extract según la ruta detectada
    const subEl = document.getElementById('pc-sub-extract');
    if (subEl) subEl.textContent = 'MPLS → MKV';
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
      if (subEl) subEl.textContent = 'MPLS → MKV final (ruta directa)';
    } else if (msg.includes('intermedio') || msg.includes('propedit')) {
      if (subEl) subEl.textContent = 'MPLS → intermedio → propedit → final';
    }
    updateSubtabQueuePill();
  } else if (msg.includes('[Desmontando ISO]')) {
    updateColaMiniPipeline('extract', 'done');
    updateColaMiniPipeline('unmount', 'active');
    const el = document.getElementById('csb-phase-label');
    if (el) el.textContent = 'Desmontando ISO…';
    updateSubtabQueuePill();
  }
}

/**
 * Actualiza la barra de progreso determinada con un porcentaje concreto.
 * @param {number} pct   - Porcentaje de 0 a 100.
 * @param {string} [label] - Texto descriptivo de la operación actual.
 */
function updateProgress(pct, label) {
  const bar   = document.getElementById('progress-bar');
  const pctEl = document.getElementById('progress-pct');
  bar.classList.remove('indeterminate');
  bar.style.width = `${pct}%`;
  if (pctEl) pctEl.textContent = `${pct}%`;
  if (label) setText('progress-label', label);
}

/**
 * Cambia el icono y label de la barra de progreso y vuelve a modo indeterminate.
 * @param {string} icon  - Emoji de la fase.
 * @param {string} label - Texto descriptivo.
 */
function setProgressLabel(icon, label) {
  setText('progress-icon', icon);
  setText('progress-label', label);
  // Volver a indeterminate cuando cambia de fase
  const bar = document.getElementById('progress-bar');
  bar.style.width = '35%';
  bar.classList.add('indeterminate');
  document.getElementById('progress-pct').textContent = '';
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

/** Vacía el contenido de la consola de output. */
function clearConsole() {
  const el = E('console-wrap');
  if (el) el.innerHTML = '';
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
  } else {
    // Resetear indicadores al quedar sin trabajo
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
function updateColaMiniPipeline(phase, state) {
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
  const csbPhaseEl  = document.getElementById(`csb-pipe-${phase}`);
  const csbCircleEl = document.getElementById(`csb-pipe-circle-${phase}`);
  if (csbPhaseEl)  csbPhaseEl.className    = `csb-pipe-phase ${state}`;
  if (csbCircleEl) csbCircleEl.textContent = icon;
  if (CONN[phase]) {
    const csbConn = document.getElementById(`csb-pipe-conn-${CONN[phase]}`);
    if (csbConn) csbConn.className = `csb-pipe-conn${state === 'done' ? ' done' : state === 'active' ? ' active' : ''}`;
  }

  // — Panel de control —
  const stepEl   = document.getElementById(`pc-step-${phase}`);
  const circleEl = document.getElementById(`pc-circle-${phase}`);
  const progEl   = document.getElementById(`pc-prog-${phase}`);
  if (stepEl)   stepEl.className      = `pc-step ${state}`;
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
    showToast('Cancelando ejecución… El ISO se desmontará y los temporales se limpiarán.', 'info');
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
 * @param {RequestInit} [opts={}]   - Opciones de fetch.
 * @param {number} [timeoutMs]      - Timeout en ms (default: API_FETCH_TIMEOUT).
 * @returns {Promise<Object|null>}  - JSON parseado, o null si hubo error.
 */
async function apiFetch(url, opts = {}, timeoutMs = API_FETCH_TIMEOUT) {
  opts.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  opts.signal = controller.signal;
  try {
    const resp = await fetch(url, opts);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      showToast(`Error: ${err.detail || resp.statusText}`, 'error');
      appendConsole(`[Error API] ${url}: ${err.detail || resp.statusText}`);
      return null;
    }
    return await resp.json();
  } catch (e) {
    const msg = e.name === 'AbortError'
      ? `Timeout: el servidor no respondió en ${timeoutMs / 1000}s`
      : `Error de red: ${e.message}`;
    showToast(msg, 'error');
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

async function _doAnalyzeMkvFromPickerPath(absPath, fileName) {

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
        for (let i = prevIdx; i < newIdx; i++) {
          _advanceMkvAnalyzeStep(steps[i], steps[i + 1]);
        }
        lastStep = prog.step;
        stepStartTs = Date.now();
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
        if (labelEl) labelEl.textContent = '⏳ Contando paquetes PGS por subtítulo…';
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
  const data = await apiFetch('/api/mkv/analyze', {
    method: 'POST',
    body: JSON.stringify({ file_path: absPath }),
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
function _rgrfL8Svg(nits) {
  if (!Array.isArray(nits) || !nits.length) return '';
  const svgW = 500, svgH = 68, padL = 32, padR = 32, axisY = 46;
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
  // Dots con halo (shadow SVG)
  nits.forEach(n => {
    const x = xOf(n);
    html += `<circle cx="${x}" cy="${axisY}" r="10" fill="#007AFF" fill-opacity="0.12" />`;
    html += `<circle cx="${x}" cy="${axisY}" r="6.5" fill="url(#${gid})" stroke="#ffffff" stroke-width="2" />`;
    html += `<text x="${x}" y="${axisY - 14}" fill="#003e8a" font-size="12"
               font-family="SF Mono,monospace" text-anchor="middle" font-weight="700">${n}</text>`;
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

  // Trim chips ordenados ASC. Si no se ha corrido el light profile no
  // tenemos l2Trims aun — mostramos placeholder apuntando a "analizar".
  const trimChips = (Array.isArray(l2Trims) && l2Trims.length > 0)
    ? l2Trims.map(n => `<span class="dv-mc-trim-chip">${n}n</span>`).join('')
    : '<span class="dv-mc-empty">analiza el perfil de luminancia para extraer los trim targets</span>';

  // HDR10 metadata footer
  const hdr10Cll  = hdr?.max_cll  != null ? `MaxCLL ${hdr.max_cll} nits` : '';
  const hdr10Fall = hdr?.max_fall != null ? `MaxFALL ${hdr.max_fall} nits` : '';
  const hdr10Line = [hdr10Cll, hdr10Fall].filter(Boolean).join(' · ');

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
  const fps = a.duration_seconds && dv.frame_count
    ? (dv.frame_count / a.duration_seconds).toFixed(3)
    : (a.fps ? a.fps.toFixed(3) : '23.976');

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
  const framesTotal = mainVideo?.frame_count || dv.frame_count || 0;
  const durationStr = a.duration_seconds ? _fmtDuration(a.duration_seconds) : '—';
  const avgShot = dv.scene_avg_length_frames
    ? `${dv.scene_avg_length_frames}f · ${(dv.scene_avg_length_frames / parseFloat(fps || 24)).toFixed(1)}s`
    : '—';
  const rpuSize = dv.rpu_size_bytes
    ? `${_fmtBytes(dv.rpu_size_bytes)} · ${Math.round(dv.rpu_size_bytes / Math.max(framesTotal, 1))} B/f`
    : '—';
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
  // BLOQUE 1 · Stream (profile + timing + structure)
  // ═══════════════════════════════════════════════════════════════
  const blockStream = `
    <section class="dv-block">
      <h5 class="dv-block-title">Stream</h5>
      <div class="dv-grid-3">
        ${cell('Profile', profile)}
        ${cell('CM version', cmLabel)}
        ${cell('Frames', framesTotal ? framesTotal.toLocaleString() : '—')}
        ${cell('Duración', durationStr)}
        ${cell('FPS', fps)}
        ${cell('Escenas', dv.scene_count ? `${dv.scene_count.toLocaleString()} · avg ${avgShot}` : '—')}
        ${cell('Bit depth', mainVideo?.bit_depth ? `${mainVideo.bit_depth}-bit` : '—')}
        ${cell('Codec', mainVideo?.codec || '—')}
        ${cell('RPU', rpuSize)}
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
  const blockActiveArea = `
    <section class="dv-block">
      <h5 class="dv-block-title">Active area <span class="dv-block-sub">L5</span></h5>
      <div class="dv-split">
        <div class="dv-grid-2">
          ${cell('Offsets T / B', `${dv.l5_top || 0} / ${dv.l5_bottom || 0} px`)}
          ${cell('Offsets L / R', `${dv.l5_left || 0} / ${dv.l5_right || 0} px`)}
          ${cell('Área activa', `${activeW} × ${activeH}`)}
          ${cell('Aspect ratio', aspectLabel)}
          ${cell('Simetría vertical', symV ? 'T = B' : `Δ ${Math.abs((dv.l5_top || 0) - (dv.l5_bottom || 0))} px`, { status: symV ? 'ok' : 'warn' })}
          ${cell('Simetría horizontal', symH ? 'L = R' : `Δ ${Math.abs((dv.l5_left || 0) - (dv.l5_right || 0))} px`, { status: symH ? 'ok' : 'warn' })}
        </div>
        <div class="dv-viz-side">${_rgrfL5Svg(dv, frameW, frameH)}</div>
      </div>
    </section>`;

  // ═══════════════════════════════════════════════════════════════
  // BLOQUE 4 · CMv4.0 levels (solo si v4.0).
  // Slim: solo presencia de los levels — los datos concretos (L9/L10
  // primaries, L11 content type) ya estan en la cadena de mastering.
  // L8 trim targets en nits se mantienen aqui con su visualizacion
  // logarítmica porque es un grafico especifico del L8.
  // ═══════════════════════════════════════════════════════════════
  let blockCmv4 = '';
  if (isV40) {
    const nitsLabel = (dv.l8_trim_nits && dv.l8_trim_nits.length)
      ? dv.l8_trim_nits.join(' · ') + ' nits'
      : (dv.l8_trim_count ? `${dv.l8_trim_count} trims` : '');
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
        ${dv.l8_trim_nits && dv.l8_trim_nits.length ? `
          <div class="dv-viz-inline">
            <div class="dv-viz-caption">L8 target displays · escala logarítmica de nits</div>
            ${_rgrfL8Svg(dv.l8_trim_nits)}
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
      ${blockStream}
      ${blockMastering}
      ${blockActiveArea}
      ${blockCmv4}
      ${blockLight}
    </div>`;
}

/** Copia la radiografía como Markdown al portapapeles. */
function _rgrfCopyToClipboard(evt) {
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

  const md = [
    `# Radiografía DV+HDR — ${a.file_name}`,
    ``,
    `**Tamaño:** ${_fmtBytes(a.file_size_bytes)} · **Duración:** ${_fmtDuration(a.duration_seconds)}`,
    ``,
    `## 1. Identidad`,
    `- Profile: **${fmt(dv.profile)}${el}**`,
    `- CM version: **${fmt(dv.cm_version)}**`,
    `- Frames: ${fmt(dv.frame_count?.toLocaleString())}`,
    `- Escenas: ${fmt(dv.scene_count?.toLocaleString())}`,
    `- Long. media escena: ${fmt(dv.scene_avg_length_frames, ' frames')}`,
    `- Bit depth: ${fmt(a.tracks?.find(t=>t.type==='video')?.bit_depth, '-bit')}`,
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

  navigator.clipboard.writeText(md).then(() => {
    showToast('✓ Radiografía copiada como Markdown', 'success');
  }).catch(() => {
    showToast('No se pudo copiar al portapapeles', 'error');
  });
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
  const pollTicker = { _stop: () => { polling = false; } };
  async function _pollLoop() {
    while (polling) {
      try {
        const st = await apiFetch('/api/mkv/light-profile/progress');
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
    // terminar igualmente y el resultado vive en state.result. Polling
    // unas veces para darle tiempo a finalizar.
    if (!data?.per_scene_max_cll) {
      for (let i = 0; i < 20; i++) {
        await new Promise(r => setTimeout(r, 1500));
        const st = await apiFetch('/api/mkv/light-profile/progress');
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
  }
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
    const counts = [];
    if (dv.scene_count) counts.push(`${dv.scene_count.toLocaleString()} escenas`);
    if (dv.frame_count) counts.push(`${dv.frame_count.toLocaleString()} frames`);
    dvCountsLine = counts.join(' · ');

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
        <div class="section-header"><div><div class="section-title">📦 Fichero MKV</div></div></div>
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

    // Clasificación Forzados / Completos con señal de fallback:
    //   1. Si el flag del MKV está puesto → es la verdad.
    //   2. Si no: packet_count < 500 → forzados (proxy fiable para PGS).
    //   3. Si no hay packet_count: bitrate < 3 kbps → forzados (señal menos fiable).
    //   4. En otro caso: completos.
    const packets = t.packet_count || 0;
    let derivedForced = t.flag_forced;
    let forcedSource = t.flag_forced ? 'flag del MKV' : '';
    if (!t.flag_forced) {
      if (packets > 0 && packets < 500) {
        derivedForced = true;
        forcedSource = `${packets} paquetes < 500`;
      } else if (packets === 0 && t.bitrate_kbps > 0 && t.bitrate_kbps < 3) {
        derivedForced = true;
        forcedSource = `bitrate ${t.bitrate_kbps} kbps < 3`;
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

  if (a.chapters.length > 0) {
    if (banner) { banner.style.display = 'flex'; banner.className = 'banner info'; }
    if (text) text.textContent = `${a.chapters.length} capítulos`;
  } else {
    if (banner) { banner.style.display = 'flex'; banner.className = 'banner warning'; }
    if (text) text.textContent = 'Sin capítulos en este MKV';
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

async function applyMkvEdits() {
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
  };

  // Mostrar modal de progreso
  const modal = document.getElementById('mkv-apply-modal');
  const titleEl = document.getElementById('mkv-apply-modal-title');
  const subEl = document.getElementById('mkv-apply-modal-sub');
  const logEl = document.getElementById('mkv-apply-modal-log');
  const statusEl = document.getElementById('mkv-apply-modal-status');
  const closeBtn = document.getElementById('mkv-apply-modal-close-btn');

  titleEl.textContent = 'Aplicando cambios…';
  subEl.textContent = `${audioEdits.length} pistas de audio · ${subEdits.length} pistas de subtítulos · ${a.chapters.length} capítulos`;
  logEl.style.display = 'none';
  logEl.textContent = '';
  statusEl.innerHTML = '<span class="spinner-inline"></span> Ejecutando mkvpropedit…';
  closeBtn.style.display = 'none';
  openModal('mkv-apply-modal');

  const result = await apiFetch('/api/mkv/apply', {
    method: 'POST',
    body: JSON.stringify(body),
  });

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

  statusEl.innerHTML = '<span style="color:var(--green)">✓ Cambios aplicados correctamente</span>';

  // Re-analizar para refrescar estado — usamos la ruta ABSOLUTA del MKV
  // (filePath), no el filename. El backend valida que cae bajo un root
  // permitido (Library / Output).
  const fresh = await apiFetch('/api/mkv/analyze', {
    method: 'POST',
    body: JSON.stringify({ file_path: mkvProject.filePath || mkvProject.fileName }),
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
  // Fase H: extract-rpu del HEVC pre-mux (~r_extract_rpu × anchor) + mkvmerge -J (~2s).
  // Antes era constante 15s pero tras el fix de extract-rpu sobre pre-mux
  // (para evitar "Invalid PPS index" en el MKV) tarda mucho más. En el NAS real:
  // 152s para 155k frames sobre el HEVC de 34 GB.
  const etaH = _cmv40EstimateSecs(s, CMV40_ETA.r_extract_rpu, CMV40_ETA.fps_extract) + 5;

  const steps = [];
  steps.push({
    key: 'A', icon: '🔍', title: 'Fase A · Analizar MKV origen',
    what: 'ffmpeg copia el HEVC + dovi_tool extract-rpu + info',
    etaSecs: etaA,
  });
  const bWhat = s.target_rpu_source === 'drive' ? 'Descarga del repo DoviTools + dovi_tool info + gates'
              : s.target_rpu_source === 'mkv' ? 'ffmpeg + extract-rpu del MKV target + gates'
              : 'Copia local + dovi_tool info + gates';
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
    what: trust ? 'Omitida — gates validaron frame count + L5/L6/L8' : 'Revisión visual manual del usuario',
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
        : 'Por determinar tras revisión de sync en Fase D');
  steps.push({
    key: 'E', icon: '🔧', title: 'Fase E · Corrección de sync',
    what: eWhat,
    etaSecs: hasSyncCfg ? 20 : 0,
    forcedStatus: eStatus,
    customLabel: eLabel,
  });
  const fWhat = dropIn ? 'Drop-in — inyecta bin directo en EL (sin merge)'
              : wf === 'p7_fel' ? 'Merge CMv4.0 + inject en EL (preserva FEL)'
              : wf === 'p7_mel' ? 'Inject RPU target en BL (descarta EL MEL)'
              : 'Inject RPU target en source HEVC';
  steps.push({
    key: 'F', icon: '💉', title: 'Fase F · Inyectar RPU',
    what: fWhat, etaSecs: etaF,
  });
  const gWhat = (wf === 'p7_fel') ? 'dovi_tool mux BL + EL_injected + mkvmerge con audio/subs/caps'
              : 'Sin mux (single-layer) — mkvmerge directo con audio/subs/caps';
  steps.push({
    key: 'G', icon: '📦', title: 'Fase G · Remux MKV final',
    what: gWhat, etaSecs: etaG,
  });

  // Gate G→H: decisión instantanea sobre el resultado de Fase H (profile + CM
  // v4.0 + frames). La etaSecs es 0 — es solo la trazabilidad del veredicto;
  // el trabajo real de validacion vive dentro de Fase H. Antes etaSecs=etaH
  // duplicaba el tiempo mostrado en el timeline.
  const validatedIdx = CMV40_PHASES_ORDER.indexOf('validated');
  const gateGHStatus = (curIdxForGate < validatedIdx) ? 'pending' : 'done';
  const gateGHLabel = (curIdxForGate < validatedIdx)
    ? (s.running_phase === 'validate' ? 'en curso…' : 'pendiente')
    : 'validación OK';
  steps.push({
    key: 'GATE_GH', icon: '🛡️', title: 'Validación final pre-finalizar',
    what: 'dovi_tool info + mkvmerge -J verifican profile + CM v4.0 + frame count del MKV resultante',
    etaSecs: 0,
    forcedStatus: gateGHStatus, customLabel: gateGHLabel,
    isGate: true,
  });

  // Fase H = validar + finalizar. El backend unifica en running_phase='validate'
  // tanto el extract-rpu del HEVC pre-mux (~150s en UHD BD) como el mkvmerge -J
  // (~2s) y el rename atomico (instantaneo). El ETA visible debe reflejar todo
  // el trabajo, no solo el rename — antes etaSecs=2 hacia que el timeline dijera
  // "ETA 2s" cuando quedaban 2min 30s reales.
  steps.push({
    key: 'H', icon: '✅', title: 'Fase H · Validar + finalizar',
    what: 'Validación DV completa (extract-rpu full-stream + dovi_tool info + mkvmerge -J) → rename atómico → cleanup',
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

  // Timer — arranque del pipeline. Tres fuentes en orden de preferencia:
  //   1. phase_history[0].started_at (autoridad si llega del backend)
  //   2. project._pipelineStartMs (cacheado la primera vez que vimos fase activa)
  //   3. Date.now() como último recurso (solo si running activo)
  const hist = s.phase_history || [];
  const firstWithTime = hist.find(h => h.started_at);
  let startedMs = firstWithTime ? Date.parse(firstWithTime.started_at) : 0;
  if (!startedMs && project) {
    if (!project._pipelineStartMs && (s.running_phase || (project.autoContinue && !s.error_message && s.phase !== 'done'))) {
      project._pipelineStartMs = Date.now();
    }
    if (project._pipelineStartMs) startedMs = project._pipelineStartMs;
  }

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
    return `<li class="cmv40-tl-step cmv40-tl-${status}${gateCls}">
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
          <div class="cmv40-tl-title-main">Pipeline automático</div>
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
  // Solo visible cuando la tab es 'repo' y hay un candidato seleccionado
  if (_cmv40NewTargetTab !== 'repo' || !_cmv40NewTargetSelected
      || _cmv40NewTargetSelected.kind !== 'repo') {
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
  const data = await apiFetch('/api/cmv40/create', {
    method: 'POST',
    body: JSON.stringify({ source_mkv_path: sourcePath }),
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

  // Arrancar cadena auto: el polling + _cmv40MaybeAutoAdvance se encarga del
  // pipeline completo. Para que el primer paso (preflight) arranque sin esperar
  // al siguiente tick, llamamos directamente a la lógica del auto-advance —
  // que decide internamente: si pendingTarget && !target_preflight_ok → preflight,
  // si target_preflight_ok || !pendingTarget → Fase A.
  if (autoOn) {
    if (project) project._autoChaining = true;
    _cmv40MaybeAutoAdvance(project);
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
  project.session = Object.assign({}, data, preserved);
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
  // Si abrimos un proyecto que YA está ejecutando una fase y no es terminal,
  // asumimos que estaba en modo auto (si no, el overlay parpadearía entre
  // fases al perder autoContinue tras recargar la página).
  const resumeAuto = !!session.running_phase
    && session.phase !== 'done'
    && !session.error_message;
  const project = {
    id: pid,
    subTabId: pid,
    session: session,
    ws: null,
    syncData: null,
    autoContinue: resumeAuto,  // off por defecto; createCMv40Project lo activa explícitamente
    pendingTarget: null,       // { kind: 'path'|'mkv', value: string }
  };
  openCMv40Projects.push(project);
  _createCMv40SubTab(project);
  _createCMv40Panel(project);
  switchCMv40SubTab(pid);
  _connectCMv40WebSocket(project);
  // Validar artefactos en disco — detecta ficheros borrados manualmente
  // y retrocede la fase automáticamente si hace falta.
  _cmv40VerifyArtifacts(project);
  return project;
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
  const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${wsProto}//${location.host}/ws/cmv40/${project.id}`);
  ws.onmessage = (ev) => {
    _appendCMv40Log(project, ev.data);
    // Refrescar sesión periódicamente
    if (ev.data.includes('━━━') || ev.data.includes('✓') || ev.data.includes('✗')) {
      _refreshCMv40Session(project.id);
    }
  };
  ws.onerror = () => {};
  project.ws = ws;
}

// Copia al portapapeles el texto plano de un elemento que contiene líneas de
// log (div.log-line). Muestra un toast de confirmación; fallback a
// document.execCommand para contextos inseguros (file://, http en IP).
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

function _appendCMv40Log(project, line) {
  const pid = project.id;
  // Marcador de progreso: no se añade al log visual, solo actualiza la barra
  const prog = _cmv40ParseProgress(line);
  if (prog) { _cmv40UpdateProgressUI(pid, prog); return; }
  _appendLogLine(document.getElementById(`cmv40-log-${pid}`), line);
  _appendLogLine(document.getElementById(`cmv40-running-log-${pid}`), line);
}

async function _refreshCMv40Session(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}`);
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
  const terminalPhase = (s.phase === 'done' || s.phase === 'error' || !!s.error_message);
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
          <div class="cmv40-running-timeline-wrap" id="cmv40-running-timeline-${pid}"></div>
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
    return;
  }

  // Recalcular métricas
  const steps = _cmv40PlanAutoSteps(s);
  const stepStatuses = steps.map(st => _cmv40StepStatus(st, s));
  const doneCount = stepStatuses.filter(st => st === 'done' || st === 'skipped').length;
  const totalCount = steps.length;
  const progressPct = totalCount > 0 ? Math.round((doneCount / totalCount) * 100) : 0;

  // Timer: elapsed / remaining (mismos cálculos que en _cmv40RenderTimeline)
  const hist = s.phase_history || [];
  const firstWithTime = hist.find(h => h.started_at);
  let startedMs = firstWithTime ? Date.parse(firstWithTime.started_at) : 0;
  if (!startedMs && project) {
    if (!project._pipelineStartMs && (s.running_phase || (project.autoContinue && !s.error_message && s.phase !== 'done'))) {
      project._pipelineStartMs = Date.now();
    }
    if (project._pipelineStartMs) startedMs = project._pipelineStartMs;
  }
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
    return `<li class="cmv40-tl-step cmv40-tl-${status}">
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

function _cmv40BindRunningLog(project) {
  // Replica TODO el log backend acumulado (no solo -200: entre fases puede
  // acumular miles de líneas y queremos ver el histórico completo si el
  // usuario scrolla arriba). Actualiza la barra con el último progreso.
  const pid = project.id;
  const logEl = document.getElementById(`cmv40-running-log-${pid}`);
  if (!logEl) return;
  logEl.innerHTML = '';
  let lastProg = null;
  (project.session.output_log || []).forEach(line => {
    const prog = _cmv40ParseProgress(line);
    if (prog) { lastProg = prog; return; }  // no añadir al log
    const div = document.createElement('div');
    div.className = 'log-line';
    if (line.includes('✓')) div.classList.add('log-success');
    if (line.includes('✗') || line.toLowerCase().includes('error')) div.classList.add('log-error');
    if (line.includes('━━━')) div.classList.add('log-phase');
    div.textContent = line;
    logEl.appendChild(div);
  });
  if (lastProg) _cmv40UpdateProgressUI(pid, lastProg);
  // Primera hidratación: al fondo (el usuario aún no ha scrolleado)
  logEl.scrollTop = logEl.scrollHeight;
}

async function cmv40CancelRunning(pid) {
  if (!confirm('¿Cancelar la ejecución en curso?')) return;
  await apiFetch(`/api/cmv40/${pid}/cancel`, { method: 'POST' });
  // Cancelar también desactiva el auto-pipeline (evita que re-arranque la siguiente)
  const project = openCMv40Projects.find(p => p.id === pid);
  if (project) {
    project._lastAutoFiredFor = null;  // resetea debounce para futuros arranques
    project._lastAutoFiredAt = 0;
    project._autoChaining = false;     // intervencion manual: apaga bridging
    if (project.autoContinue) {
      project.autoContinue = false;
      showToast('Cancelado — auto-avance desactivado', 'info');
    } else {
      showToast('Cancelando…', 'info');
    }
  }
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
          data-tooltip="Auto-ejecuta cada fase tras la anterior. Pausa obligatoria en Fase D para revisión visual del chart.">
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
    </div>`;

  // Si aún no tenemos tmdb_info, intentamos hidratarlo (puede haber fallado la
  // tarea background). Best-effort, sin bloquear UI.
  if (!s.tmdb_info && !project?._tmdbLookupTried) {
    if (project) project._tmdbLookupTried = true;
    _cmv40HydrateTmdbClient(pid);
  }
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
  CMV40_FASES_DEF.forEach(fase => {
    const state = _cmv40PhaseState(s.phase, fase.produces, fase.startsFrom);
    const isExpanded = project.expandedPhases[fase.key] !== undefined
      ? project.expandedPhases[fase.key]
      : (state === 'active');
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
    errorHtml = `
      <div class="section-card cmv40-card-error" style="margin-top:12px">
        <div class="section-body" style="display:flex; align-items:center; gap:12px">
          <span style="font-size:20px">⚠️</span>
          <div style="flex:1">
            <div style="font-weight:600; color:var(--red); margin-bottom:2px">Error en la última acción</div>
            <div style="font-size:12px; color:var(--text-2)">${escHtml(s.error_message)}</div>
          </div>
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

  container.innerHTML = errorHtml + archivedHtml + doneHtml + cards.join('');

  // Lanzar cargas asíncronas donde aplique
  if (_cmv40PhaseState(s.phase, 'target_provided', 'source_analyzed') === 'active') {
    _cmv40LoadRpus(pid);
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
  const titleSuffix = isSkipped
    ? ' <span style="color:var(--text-3); font-weight:400; font-size:11px">(omitida)</span>'
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
      // L5 divergence
      if (g.l5_div) {
        const px = g.l5_div.px_max || 0;
        const st = px <= 5 ? 'ok' : (px <= 30 ? 'warn' : 'ko');
        const result = px <= 5 ? `div ≤ 5 px` : (px <= 30 ? `div ${px} px · warn` : `div ${px} px · aborta`);
        const explanation = st === 'ok'
          ? 'Los offsets de letterbox del target están a ≤5 px de los del BD — misma edición o recorte equivalente.'
          : st === 'warn'
          ? 'Divergencia moderada del active area (5-30 px). Puede ser la misma edición con recorte ligeramente distinto. La app avanza pero conviene revisar visualmente.'
          : 'Divergencia crítica de active area (>30 px). Target es un master con corte radicalmente distinto — rechazado automáticamente.';
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
    pushGate('l5_div',
      `L5 div ${gates.l5_div.px_max}px`,
      `L5 div ${gates.l5_div.px_max}px (>30)`,
      `Divergencia L5 (active area). ≤5 ok · 5-30 warn · >30 aborta — posible edición distinta del disco`
    );
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
        <button class="cmv40-tab-btn active" id="cmv40-tab-btn-path-${pid}"
          onclick="_cmv40SwitchTargetTab('${pid}','path')">📂 Desde carpeta NAS</button>
        <button class="cmv40-tab-btn" id="cmv40-tab-btn-mkv-${pid}"
          onclick="_cmv40SwitchTargetTab('${pid}','mkv')">🎬 Extraer de otro MKV</button>
      </div>

      <div id="cmv40-target-path-${pid}" class="cmv40-target-tab">
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
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Separa el Base Layer del Enhancement Layer y extrae datos de brillo por frame. Tarda 5-15 min.</div>
      ${s.sync_delta !== 0 ? `<div class="banner warning" style="margin-bottom:10px"><span class="banner-icon">⚠️</span><span>Ya se detecta diferencia de frames (Δ = ${s.sync_delta > 0 ? '+' : ''}${s.sync_delta}). Lo revisarás visualmente en la siguiente fase.</span></div>` : ''}
      <button class="btn btn-primary btn-md" onclick="cmv40DoExtract('${pid}')">✂️ Extraer BL/EL + per-frame data</button>
    </div>`;
}

function _cmv40FaseDBody(pid, s) {
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Gráfico de MaxPQ (L1 del RPU Dolby Vision) por frame. Rojo = origen, Azul = target. Deben coincidir en forma. Si hay offset, aplicar corrección.</div>
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
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Inyecta el RPU sincronizado en el Enhancement Layer.</div>
      <div class="banner info" style="margin-bottom:10px"><span class="banner-icon">ℹ️</span><span>Verifica en el gráfico de la Fase D que los dos trazos coinciden antes de inyectar.</span></div>
      <button class="btn btn-primary btn-md" onclick="cmv40DoInject('${pid}')">💉 Inyectar RPU</button>
    </div>`;
}

function _cmv40FaseGBody(pid, s) {
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Combina BL + EL inyectado + audio/subs/capítulos del origen. Genera el MKV final.</div>
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
    const data = await apiFetch(`/api/cmv40/${pid}`);
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
  const pid = project.id;
  // Dedup key: phase + estado de target_preflight_ok. Necesitamos sensibilidad
  // al flag de preflight porque para la fase 'created' hay dos acciones
  // distintas: si !target_preflight_ok → disparar preflight; si OK → Fase A.
  // Sin esto, _lastAutoFiredFor === 'created' nos bloquearía la transición
  // preflight → Fase A.
  const stateKey = s.phase + ':pf=' + (s.target_preflight_ok ? '1' : '0');
  if (project._lastAutoFiredFor === stateKey) return;
  project._lastAutoFiredFor = stateKey;
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
      const s = project.session;
      const trustedAuto = s.target_trust_ok === true
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
      // Terminal: toast único de éxito cuando la pipeline completa el full run.
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
  if (project.autoContinue) {
    showToast('🤖 Auto-avance activado · la siguiente fase se lanzara al terminar la actual', 'success');
  } else {
    showToast('Auto-avance desactivado · tendras que lanzar cada fase manualmente', 'info');
  }
}

function _cmv40SwitchTargetTab(pid, tab) {
  document.getElementById(`cmv40-target-path-${pid}`).style.display = (tab === 'path') ? '' : 'none';
  document.getElementById(`cmv40-target-mkv-${pid}`).style.display = (tab === 'mkv') ? '' : 'none';
  const btnPath = document.getElementById(`cmv40-tab-btn-path-${pid}`);
  const btnMkv  = document.getElementById(`cmv40-tab-btn-mkv-${pid}`);
  if (btnPath) btnPath.classList.toggle('active', tab === 'path');
  if (btnMkv)  btnMkv.classList.toggle('active',  tab === 'mkv');
  if (tab === 'path') _cmv40LoadRpus(pid);
  else _cmv40LoadTargetMkvs(pid);
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
  const data = await apiFetch('/api/cmv40');
  _cmv40SidebarList = data?.sessions || [];
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
    const phaseLabel = s.archived ? 'Archivado' : (CMV40_PHASE_LABELS[s.phase] || s.phase);
    const phaseIcon  = s.archived ? '🗃️' : (s.error_message ? '⚠️' : (CMV40_PHASE_ICONS[s.phase] || '🎨'));
    const isOpen = openCMv40Projects.find(p => p.id === s.id);
    const isSelected = _cmv40SelectedSidebarId === s.id;
    const name = s.source_mkv_name.replace(/\.mkv$/i, '');

    const modDate = formatRelativeDate(s.updated_at || s.created_at);
    const modFull = new Date(s.updated_at || s.created_at).toLocaleString('es-ES', {
      day: '2-digit', month: '2-digit', year: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });

    const card = document.createElement('div');
    card.className = `session-card${isSelected ? ' selected' : ''}`;
    card.dataset.sid = s.id;
    card.innerHTML = `
      <div class="session-card-row">
        <div class="session-card-status-badge" data-tooltip="${escHtml(phaseLabel)}">${phaseIcon}</div>
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
  library: 'Biblioteca',
  output:  'Output',
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

