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

  document.querySelectorAll('.tab').forEach((btn, idx) => {
    btn.classList.toggle('active', idx + 1 === n);
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
    const parts = [`Profile ${d.profile} (${d.el_type})`, `CM ${d.cm_version}`];
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

function _findOriginalTrackIndex(raw, type) {
  const bd = currentSession?.bdinfo_result;
  if (!bd) return -1;

  if (type === 'audio') {
    return bd.audio_tracks.findIndex(t =>
      t.language === raw.language && t.codec === raw.codec && t.description === raw.description
    );
  }
  // Subtítulos: si tenemos packet_count (heurístico nuevo), usarlo como
  // discriminador principal — el bitrate sintético es idéntico para todos.
  if (raw.packet_count && raw.packet_count > 0) {
    return bd.subtitle_tracks.findIndex(t =>
      t.language === raw.language && t.packet_count === raw.packet_count
    );
  }
  return bd.subtitle_tracks.findIndex(t =>
    t.language === raw.language && t.bitrate_kbps === raw.bitrate_kbps
  );
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
      const origIdx = _findOriginalTrackIndex(raw, 'audio');
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
      const origIdx = _findOriginalTrackIndex(raw, 'subtitle');
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
  tracks.forEach((t, i) => { t.position = i; });
  currentSession.included_tracks = tracks;
  renderIncludedTracks(tracks);
  markProjectDirty();
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

  const byType = { audio: [], subtitle: [] };
  tracks.forEach((track, idx) => {
    const type = track.track_type === 'audio' ? 'audio' : 'subtitle';
    byType[type].push({ track, idx });
  });

  updateTrackCounts();

  const renderGroup = (container, items, isAudio) => {
    if (!items.length) {
      container.innerHTML = `<div class="discarded-empty">${isAudio ? 'Ninguna descartada' : 'Ninguna descartada'}</div>`;
      return;
    }
    items.forEach(({ track, idx }) => {
      const raw = track.raw || {};
      const origIdx = _findOriginalTrackIndex(raw, isAudio ? 'audio' : 'subtitle');
      const origLabel = origIdx >= 0 ? `#${origIdx + 1}` : '';
      const codecInfo = isAudio
        ? [raw.codec, raw.description, raw.bitrate_kbps ? `${raw.bitrate_kbps.toLocaleString()} kbps` : null].filter(Boolean).join(' · ')
        : `PGS · ${langLiteral(raw.language)}`;
      const div = document.createElement('div');
      div.className = 'discarded-item';
      div.innerHTML = `
        ${origLabel ? `<span class="track-orig-pos" data-tooltip="Posición original de la pista en el ISO">${origLabel}</span>` : ''}
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
    lines.push(`Profile: ${d.profile} (${d.el_type})`);
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


function recoverTrack(idx) {
  const track = currentSession.discarded_tracks.splice(idx, 1)[0];
  const raw   = track.raw || {};
  const recovered = {
    track_type: track.track_type,
    position: currentSession.included_tracks.length,
    raw: track.raw,
    label: `${langLiteral(raw.language) || ''} ${raw.codec || ''}`.trim() || 'Pista recuperada',
    flag_default: false,
    flag_forced: false,
    selection_reason: 'Recuperada manualmente por el usuario',
    language_literal: raw.language || '',
    codec_literal: raw.codec || '',
    subtitle_type: 'complete',
  };
  currentSession.included_tracks.push(recovered);
  renderIncludedTracks(currentSession.included_tracks);
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
    ? `Añadido a la cola en posición ${queuePos}. Sigue el progreso en "Demux Jobs".`
    : 'Iniciando extracción… Sigue el progreso en "Demux Jobs".', 'success');

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
    detail.innerHTML = 'Monitoriza el progreso en el panel <strong>Demux Jobs</strong>.';
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
  const line = document.createElement('div');
  const low  = text.toLowerCase();
  if (low.startsWith('[fase') || low.startsWith('[pipeline')) line.className = 'log-phase';
  else if (low.includes('error') || low.includes('fallo'))   line.className = 'log-error';
  else if (low.includes('aviso') || low.includes('warning')) line.className = 'log-warn';
  else if (low.startsWith('prgv:'))                          line.className = 'log-prog';
  line.textContent = text;
  c.appendChild(line);
  c.scrollTop = c.scrollHeight;
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

  // Renderiza en un elemento dado
  const fill = (c) => {
    if (!c) return;
    c.innerHTML = '';
    lines.forEach(text => {
      const div = document.createElement('div');
      const low = text.toLowerCase();
      if (low.includes('error') || low.includes('fallo')) {
        div.className = 'cola-log-error';
      } else if (low.includes('aviso') || low.includes('warning')) {
        div.className = 'cola-log-warn';
      } else if (/^\[(?:Fase|Montando|Desmontando|Pipeline)\b/i.test(text)) {
        div.className = 'log-phase';
      } else if (/^Progress:\s*\d+%/i.test(text)) {
        div.className = 'log-progress';
      }
      div.textContent = text;
      c.appendChild(div);
    });
    c.scrollTop = c.scrollHeight;
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

/** No-op: el sub-tab "Demux Jobs" ya no muestra contador ni icono dinámico. */
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
 * Cancela el trabajo en ejecución desde el panel Cola (Demux Jobs).
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

  // Renderizar log con coloreado semántico
  const content = document.getElementById('log-viewer-content');
  content.innerHTML = '';
  const lines = rec.output_log || [];
  for (const line of lines) {
    const div = document.createElement('div');
    const low = line.toLowerCase();
    if (low.includes('error') || low.includes('fallo'))       div.className = 'log-line-error';
    else if (low.includes('aviso') || low.includes('warning')) div.className = 'log-line-warning';
    else if (line.includes('[Pipeline]') || line.includes('Completado')) div.className = 'log-line-done';
    else if (line.includes('[Fase ') || line.includes('[Montando') || line.includes('[Desmontando')) div.className = 'log-line-phase';
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

// ── MKV Picker Modal ─────────────────────────────────────────────

async function openMkvPickerModal() {
  _mkvPickerSelected = null;
  const btn = document.getElementById('mkv-picker-analyze-btn');
  if (btn) btn.disabled = true;
  await loadMkvPickerList();
  openModal('mkv-picker-modal');
}

async function loadMkvPickerList() {
  const select = document.getElementById('mkv-picker-select');
  if (!select) return;
  select.innerHTML = '<option value="">— Cargando… —</option>';
  const data = await apiFetch('/api/mkv/files');
  select.innerHTML = '<option value="">— Seleccionar MKV —</option>';
  if (data?.files) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f;
      opt.textContent = f;
      select.appendChild(opt);
    });
  }
}

function onMkvPickerChange(val) {
  _mkvPickerSelected = val || null;
  const btn = document.getElementById('mkv-picker-analyze-btn');
  if (btn) btn.disabled = !val;
}

async function analyzeMkvFromPicker() {
  if (!_mkvPickerSelected) return;

  // Si hay un MKV abierto con cambios pendientes, confirmar
  if (mkvProject?.dirty) {
    showConfirm(
      'Cambios sin guardar',
      'Hay cambios sin guardar en el MKV actual. ¿Descartar y abrir otro?',
      () => _doAnalyzeMkvFromPicker(),
      'Descartar y abrir',
    );
    return;
  }
  await _doAnalyzeMkvFromPicker();
}

async function _doAnalyzeMkvFromPicker() {
  const fileName = _mkvPickerSelected;
  const btn = document.getElementById('mkv-picker-analyze-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Analizando…'; }

  const data = await apiFetch('/api/mkv/analyze', {
    method: 'POST',
    body: JSON.stringify({ file_path: fileName }),
  });

  if (btn) { btn.disabled = false; btn.textContent = '🔍 Abrir y analizar'; }
  closeModal('mkv-picker-modal');

  if (!data) {
    showToast('Error al analizar el MKV.', 'error');
    return;
  }

  openMkvProject(data);
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

// ── Render del panel de edición ──────────────────────────────────

function _renderMkvEditPanel() {
  if (!mkvProject) return;
  const a = mkvProject.analysis;
  const videoTracks = a.tracks.filter(t => t.type === 'video');
  const audioTracks = a.tracks.filter(t => t.type === 'audio');
  const subTracks   = a.tracks.filter(t => t.type === 'subtitles');

  // Resumen de vídeo (solo pista principal)
  const mainVideo = videoTracks.find(v => (v.pixel_dimensions || '').startsWith('3840') || (v.pixel_dimensions || '').startsWith('4096')) || videoTracks[0];
  const videoInfo = mainVideo ? `${mainVideo.codec} ${mainVideo.pixel_dimensions}` : '';
  const videoBitrate = mainVideo?.bitrate_kbps ? `${mainVideo.bitrate_kbps.toLocaleString()} kbps` : '';
  // HDR info
  const hdrInfo = a.hdr ? [a.hdr.hdr_format, a.hdr.color_primaries, a.hdr.transfer_characteristics, a.hdr.bit_depth ? `${a.hdr.bit_depth}-bit` : ''].filter(Boolean).join(' · ') : (mainVideo?.hdr_format || '');
  const hdrMaxCll = a.hdr?.max_cll ? `MaxCLL: ${a.hdr.max_cll}` : '';
  const hdrMaxFall = a.hdr?.max_fall ? `MaxFALL: ${a.hdr.max_fall}` : '';
  // Dolby Vision
  const hasDV = a.dovi != null || videoTracks.filter(v => v.codec.includes('HEVC') || v.codec.includes('H.265')).length > 1;
  const dvInfo = a.dovi ? `DV P${a.dovi.profile} ${a.dovi.el_type}, CM ${a.dovi.cm_version}` : '';

  const panel = document.getElementById('mkv-edit-panel');
  panel.innerHTML = `
    <div class="project-panel-inner" style="max-width:900px; margin:0 auto; padding:24px 20px">

      <!-- Info del fichero (solo lectura) -->
      <div class="section-card">
        <div class="section-header"><div><div class="section-title">📦 Fichero MKV</div></div></div>
        <div class="section-body">
          <div style="font-weight:600; font-size:14px; margin-bottom:6px">${escHtml(a.file_name)}</div>
          <div style="font-size:12px; color:var(--text-2); display:flex; flex-wrap:wrap; gap:6px 16px; line-height:1.6">
            <span>${_fmtBytes(a.file_size_bytes)}</span>
            <span>${_fmtDuration(a.duration_seconds)}</span>
            <span>${escHtml(videoInfo)}${videoBitrate ? ` · ${videoBitrate}` : ''}</span>
            ${hdrInfo ? `<span style="color:var(--orange); font-weight:500">${escHtml(hdrInfo)}</span>` : ''}
            ${hdrMaxCll || hdrMaxFall ? `<span style="color:var(--text-3)">${[hdrMaxCll, hdrMaxFall].filter(Boolean).join(' · ')}</span>` : ''}
            ${hasDV ? `<span style="color:var(--teal); font-weight:600">${dvInfo || 'Dolby Vision'}</span>` : ''}
            <span>${audioTracks.length} audio · ${subTracks.length} subs</span>
          </div>
        </div>
      </div>

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

  _renderMkvTracks();
  _renderMkvChapters();
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
    const channels = t.channels ? `${t.channels >= 7 ? '7.1' : t.channels >= 5 ? '5.1' : t.channels >= 2 ? '2.0' : '1.0'}` : '';
    const desc = [t.codec, channels, t.sample_rate ? `${t.sample_rate/1000}kHz` : '', t.bitrate_kbps ? `${t.bitrate_kbps.toLocaleString()} kbps` : ''].filter(Boolean).join(' · ');
    const def = t.flag_default ? ' active-default' : '';
    const tooltip = [
      `Codec: ${t.codec}`,
      t.format_commercial ? `Formato: ${t.format_commercial}` : null,
      `Idioma: ${t.language} → ${langName}`,
      channels ? `Canales: ${channels}` : null,
      t.channel_layout ? `Layout: ${t.channel_layout}` : null,
      t.sample_rate ? `Sample rate: ${t.sample_rate/1000}kHz` : null,
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
          placeholder="${escHtml(langName + ' ' + t.codec)}"
          onchange="onMkvTrackEdit(${t.id}, 'name', this.value)"
          data-tooltip="Nombre de la pista en el MKV">
        <span class="track-raw">${escHtml(langName)} · ${escHtml(desc)}</span>
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
    const def = t.flag_default ? ' active-default' : '';
    const frc = t.flag_forced  ? ' active-forced'  : '';
    const forcedLabel = t.flag_forced ? 'Forzados' : 'Completos';
    const tooltip = [
      `Codec: ${t.codec || 'PGS'}`,
      `Idioma: ${t.language} → ${langName}`,
      `Tipo: ${forcedLabel}`,
      t.bitrate_kbps ? `Bitrate: ${t.bitrate_kbps} kbps` : null,
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
          placeholder="${escHtml(langName + ' ' + forcedLabel + ' (PGS)')}"
          onchange="onMkvTrackEdit(${t.id}, 'name', this.value)"
          data-tooltip="Nombre de la pista en el MKV">
        <span class="track-raw">${escHtml(langName)} · PGS · ${escHtml(forcedLabel)}</span>
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

  // Re-analizar para refrescar estado
  const fresh = await apiFetch('/api/mkv/analyze', {
    method: 'POST',
    body: JSON.stringify({ file_path: mkvProject.fileName }),
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
  'extract':         'Fase C — Extrayendo BL/EL y datos per-frame',
  'inject':          'Fase F — Inyectando RPU en EL',
  'remux':           'Fase G — Remuxando MKV final',
};

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

let _cmv40NewTargetTab = 'path';  // 'path' | 'mkv'
let _cmv40NewTargetSelected = null;  // { kind: 'path'|'mkv', value: string }

async function openNewCMv40Modal() {
  _cmv40SourceSelected = null;
  _cmv40NewTargetTab = 'path';
  _cmv40NewTargetSelected = null;
  const btn = document.getElementById('cmv40-create-btn');
  if (btn) btn.disabled = true;
  const autoCb = document.getElementById('cmv40-new-auto');
  if (autoCb) autoCb.checked = true;
  _cmv40NewSwitchTargetTab('path');
  await Promise.all([
    loadCMv40SourceList(),
    _cmv40NewLoadRpus(),
  ]);
  openModal('cmv40-new-modal');
}

async function loadCMv40SourceList() {
  const select = document.getElementById('cmv40-source-select');
  select.innerHTML = '<option value="">— Cargando… —</option>';
  const data = await apiFetch('/api/mkv/files');
  select.innerHTML = '<option value="">— Seleccionar MKV origen —</option>';
  if (data?.files) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f;
      opt.textContent = f;
      select.appendChild(opt);
    });
  }
}

function onCMv40SourceChange(val) {
  _cmv40SourceSelected = val || null;
  _cmv40NewUpdateCreateBtn();
}

function _cmv40NewSwitchTargetTab(tab) {
  _cmv40NewTargetTab = tab;
  document.getElementById('cmv40-new-target-path').style.display = tab === 'path' ? '' : 'none';
  document.getElementById('cmv40-new-target-mkv').style.display  = tab === 'mkv'  ? '' : 'none';
  document.getElementById('cmv40-new-tab-btn-path').classList.toggle('active', tab === 'path');
  document.getElementById('cmv40-new-tab-btn-mkv').classList.toggle('active',  tab === 'mkv');
  _cmv40NewTargetSelected = null;
  _cmv40NewUpdateCreateBtn();
  if (tab === 'mkv') _cmv40NewLoadTargetMkvs();
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
  const id = _cmv40NewTargetTab === 'path' ? 'cmv40-new-rpu-select' : 'cmv40-new-target-mkv-select';
  const val = document.getElementById(id).value;
  _cmv40NewTargetSelected = val ? { kind: _cmv40NewTargetTab, value: val } : null;
  _cmv40NewUpdateCreateBtn();
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

  const data = await apiFetch('/api/cmv40/create', {
    method: 'POST',
    body: JSON.stringify({
      source_mkv_path: '/mnt/output/' + _cmv40SourceSelected,
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

  // Arrancar cadena auto: A primero (backend). Cuando A termine,
  // _cmv40MaybeAutoAdvance detectará pendingTarget y disparará B automáticamente.
  if (autoOn) {
    cmv40DoAnalyzeSource(data.id);
  }
}

// ── Proyecto CMv4.0 ──────────────────────────────────────────────

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
  const project = {
    id: pid,
    subTabId: pid,
    session: session,
    ws: null,
    syncData: null,
    autoContinue: false,      // por defecto off; createCMv40Project lo activa
    pendingTarget: null,      // { kind: 'path'|'mkv', value: string }
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

function _appendCMv40Log(project, line) {
  const pid = project.id;
  // Marcador de progreso: no se añade al log visual, solo actualiza la barra
  const prog = _cmv40ParseProgress(line);
  if (prog) { _cmv40UpdateProgressUI(pid, prog); return; }
  // Append al log persistente
  const logEl = document.getElementById(`cmv40-log-${pid}`);
  if (logEl) {
    const div = document.createElement('div');
    div.className = 'log-line';
    if (line.includes('✓')) div.classList.add('log-success');
    if (line.includes('✗') || line.toLowerCase().includes('error')) div.classList.add('log-error');
    if (line.includes('━━━')) div.classList.add('log-phase');
    div.textContent = line;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
  }
  // También al overlay si está abierto
  const runningLogEl = document.getElementById(`cmv40-running-log-${pid}`);
  if (runningLogEl) {
    const div = document.createElement('div');
    div.className = 'log-line';
    if (line.includes('✓')) div.classList.add('log-success');
    if (line.includes('✗') || line.toLowerCase().includes('error')) div.classList.add('log-error');
    if (line.includes('━━━')) div.classList.add('log-phase');
    div.textContent = line;
    runningLogEl.appendChild(div);
    runningLogEl.scrollTop = runningLogEl.scrollHeight;
  }
}

async function _refreshCMv40Session(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}`);
  if (!data) return;
  const project = openCMv40Projects.find(p => p.id === pid);
  if (project) {
    project.session = data;
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
          <button class="btn btn-ghost btn-xs" onclick="_clearCMv40Log('${pid}')">🗑️ Limpiar</button>
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

  if (s.running_phase) {
    // Crear o actualizar overlay
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.className = 'cmv40-running-overlay';
      overlay.innerHTML = `
        <div class="cmv40-running-box">
          <div class="cmv40-running-header">
            <div class="cmv40-running-spinner"></div>
            <div style="flex:1">
              <div class="cmv40-running-title" id="cmv40-running-title-${pid}"></div>
              <div class="cmv40-running-subtitle">El proyecto está bloqueado mientras se ejecuta la tarea</div>
            </div>
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
              <div class="cmv40-progress-bar indeterminate" id="cmv40-progress-bar-${pid}"
                style="height:100%; background:linear-gradient(90deg,#1e6fe6 0%,#3b8fff 50%,#5ab3ff 100%); width:0%; min-width:2px; transition:width 0.4s ease; border-radius:7px; position:relative; overflow:hidden; box-shadow:0 0 10px rgba(59,143,255,0.55), inset 0 1px 0 rgba(255,255,255,0.25)"></div>
            </div>
          </div>
          <div class="cmv40-running-log" id="cmv40-running-log-${pid}"></div>
        </div>`;
      panel.appendChild(overlay);
      // Suscribe al WebSocket para actualizar el log en tiempo real
      _cmv40BindRunningLog(project);
    }
    // Actualizar título
    const titleEl = document.getElementById(`cmv40-running-title-${pid}`);
    if (titleEl) {
      const autoTag = project.autoContinue ? '🤖 Auto · ' : '';
      titleEl.textContent = autoTag + (CMV40_RUNNING_LABELS[s.running_phase] || `Ejecutando: ${s.running_phase}`);
    }
  } else if (overlay) {
    // Quitar overlay con animación
    overlay.classList.add('closing');
    setTimeout(() => overlay.remove(), 200);
  }
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
  // Replica los últimos logs de la sesión al div de log y actualiza progreso
  const pid = project.id;
  const logEl = document.getElementById(`cmv40-running-log-${pid}`);
  if (!logEl) return;
  logEl.innerHTML = '';
  let lastProg = null;
  (project.session.output_log || []).slice(-200).forEach(line => {
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
  logEl.scrollTop = logEl.scrollHeight;
}

async function cmv40CancelRunning(pid) {
  if (!confirm('¿Cancelar la ejecución en curso?')) return;
  await apiFetch(`/api/cmv40/${pid}/cancel`, { method: 'POST' });
  // Cancelar también desactiva el auto-pipeline (evita que re-arranque la siguiente)
  const project = openCMv40Projects.find(p => p.id === pid);
  if (project && project.autoContinue) {
    project.autoContinue = false;
    showToast('Cancelado — auto-avance desactivado', 'info');
  } else {
    showToast('Cancelando…', 'info');
  }
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
  container.innerHTML = `
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
              ${srcDv ? `Profile ${srcDv.profile} (${srcDv.el_type}) · CM ${srcDv.cm_version} · ${s.source_frame_count.toLocaleString()} frames` : 'Sin analizar'}
            </div>
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
              ${tgtDv ? `RPU target: Profile ${tgtDv.profile} (${tgtDv.el_type}) · CM ${tgtDv.cm_version} · ${s.target_frame_count.toLocaleString()} frames` : ''}
              ${s.sync_delta ? ` · <span style="color:var(--orange)">Δ ${s.sync_delta > 0 ? '+' : ''}${s.sync_delta} frames</span>` : ''}
            </div>
          </div>
        </div>
      </div>
    </div>`;
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
    project.session = data;
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
  { key: 'D', title: 'Fase D — Verificar sincronización',  produces: 'sync_verified',   startsFrom: 'extracted',       reset_to: 'extracted' },
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

  // Renderizar todas las fases como cards
  const cards = CMV40_FASES_DEF.map(fase => {
    const state = _cmv40PhaseState(s.phase, fase.produces, fase.startsFrom);
    // Active siempre expandida. Done colapsada por defecto. Pending colapsada.
    const isExpanded = project.expandedPhases[fase.key] !== undefined
      ? project.expandedPhases[fase.key]
      : (state === 'active');
    return _cmv40RenderFaseCard(pid, s, fase, state, isExpanded);
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
  // Chart: cargar si Fase D activa o completada y está expandida
  const faseDState = _cmv40PhaseState(s.phase, 'sync_verified', 'extracted');
  const dExpanded = project.expandedPhases['D'] !== undefined
    ? project.expandedPhases['D']
    : (faseDState === 'active');
  if ((faseDState === 'active' || faseDState === 'done') && dExpanded) {
    _loadCMv40SyncChart(project);
  }
}

function _cmv40RenderFaseCard(pid, s, fase, state, isExpanded) {
  const stateIcon = state === 'done' ? '✅' : state === 'active' ? '▶️' : '🔒';
  const stateLabel = state === 'done' ? 'Completado' : state === 'active' ? 'En curso' : 'Pendiente';

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

  return `
    <div class="section-card cmv40-fase-card cmv40-fase-${state}" style="margin-top:12px" data-fase-key="${fase.key}">
      <div class="section-header cmv40-fase-header" onclick="_cmv40TogglePhase('${pid}','${fase.key}')">
        <div class="cmv40-fase-state-icon">${stateIcon}</div>
        <div style="flex:1">
          <div class="section-title">${escHtml(fase.title)}</div>
          ${summary ? `<div class="section-subtitle">${summary}</div>` : `<div class="section-subtitle">${stateLabel}</div>`}
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
  const fase = CMV40_FASES_DEF.find(f => f.key === key);
  const state = _cmv40PhaseState(project.session.phase, fase.produces, fase.startsFrom);
  const current = project.expandedPhases[key] !== undefined
    ? project.expandedPhases[key]
    : (state === 'active');
  project.expandedPhases[key] = !current;
  _updateCMv40Panel(project);
}

function _cmv40FaseSummary(key, s) {
  const arts = s.artifacts || {};
  if (key === 'A' && s.source_dv_info) {
    const d = s.source_dv_info;
    return `Profile ${d.profile} (${d.el_type}) · CM ${d.cm_version} · ${s.source_frame_count.toLocaleString()} frames`;
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
  if (key === 'D') return s.sync_config ? `Corrección aplicada (Δ = ${s.sync_delta})` : 'Sincronización verificada (Δ = 0)';
  if (key === 'F') {
    const sz = arts['EL_injected.hevc'];
    return sz ? `EL_injected.hevc generado (${_fmtBytes(sz)})` : 'EL_injected.hevc generado';
  }
  if (key === 'G') {
    const sz = arts['output.mkv'];
    return sz ? `output.mkv generado (${_fmtBytes(sz)})` : 'output.mkv generado';
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
        <div><span style="color:var(--text-3)">Profile:</span> ${d.profile} (${d.el_type})</div>
        <div><span style="color:var(--text-3)">CM version:</span> ${d.cm_version}</div>
        <div><span style="color:var(--text-3)">Frames:</span> ${s.source_frame_count.toLocaleString()}</div>
        ${d.has_l1 ? '<div><span style="color:var(--text-3)">Metadata:</span> L1 L2 L5 L6</div>' : ''}
      </div>`;
  }
  if (key === 'B' && s.target_dv_info) {
    const d = s.target_dv_info;
    const srcType = s.target_rpu_source === 'path' ? 'Carpeta NAS' : 'Extraído de otro MKV';
    return `
      <div style="font-size:12px; line-height:1.8">
        <div><span style="color:var(--text-3)">Fuente:</span> ${srcType}</div>
        <div><span style="color:var(--text-3)">Path:</span> <code>${escHtml(s.target_rpu_path || '—')}</code></div>
        <div><span style="color:var(--text-3)">CM version:</span> ${d.cm_version}</div>
        <div><span style="color:var(--text-3)">Frames:</span> ${s.target_frame_count.toLocaleString()}</div>
        <div><span style="color:var(--text-3)">Δ vs origen:</span> <b style="color:${s.sync_delta === 0 ? 'var(--green)' : 'var(--orange)'}">${s.sync_delta > 0 ? '+' : ''}${s.sync_delta} frames</b></div>
      </div>`;
  }
  // Fase D completada: mostrar stats + chart (modo revisión, sin controles)
  if (key === 'D') {
    const syncConfigHtml = s.sync_config
      ? `<div style="margin-bottom:10px; font-size:12px">
          <span style="color:var(--text-3)">Corrección aplicada:</span>
          <pre style="margin-top:6px; font-size:11px; background:var(--surface-2); padding:8px; border-radius:4px">${escHtml(JSON.stringify(s.sync_config, null, 2))}</pre>
        </div>`
      : '<div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Sincronización confirmada sin corrección.</div>';
    return `
      ${syncConfigHtml}
      <div id="cmv40-sync-stats-${pid}" class="cmv40-sync-stats"></div>
      <div id="cmv40-chart-wrap-${pid}" class="cmv40-chart-wrap">
        <canvas id="cmv40-chart-${pid}" width="1000" height="280"></canvas>
        <div class="cmv40-chart-tooltip" id="cmv40-chart-tooltip-${pid}" style="display:none"></div>
      </div>`;
  }
  if (key === 'H' && s.output_mkv_path) {
    return `<div style="font-size:12px"><span style="color:var(--text-3)">MKV final:</span> <code>${escHtml(s.output_mkv_path)}</code></div>`;
  }
  // Fase C: mostrar artefactos generados (BL.hevc, EL.hevc, per_frame_data.json)
  if (key === 'C') {
    return _cmv40ArtifactsBody(s, ['BL.hevc', 'EL.hevc', 'per_frame_data.json']);
  }
  // Fase F: EL_injected.hevc
  if (key === 'F') {
    return _cmv40ArtifactsBody(s, ['EL_injected.hevc']);
  }
  // Fase G: output.mkv (antes de mover en Fase H)
  if (key === 'G') {
    return _cmv40ArtifactsBody(s, ['output.mkv']);
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
      project.session = data;
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
        project.session = data;
        if (!project.expandedPhases) project.expandedPhases = {};
        project.expandedPhases[faseKey] = true;
        project.syncData = null;
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

async function cmv40DoAnalyzeSource(pid) {
  await apiFetch(`/api/cmv40/${pid}/analyze-source`, { method: 'POST' });
  showToast('Analizando origen…', 'info');
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
      project.session = data;
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
  switch (s.phase) {
    case 'created':
      showToast('🤖 Auto: analizando origen', 'info');
      cmv40DoAnalyzeSource(pid);
      break;
    case 'source_analyzed':
      // Si el usuario preseleccionó el target en el modal, aplicarlo automático
      if (project.pendingTarget) {
        const t = project.pendingTarget;
        project.pendingTarget = null;
        if (t.kind === 'path') {
          showToast('🤖 Auto: cargando RPU target', 'info');
          _cmv40AutoTargetPath(pid, t.value);
        } else {
          showToast('🤖 Auto: extrayendo RPU del MKV target', 'info');
          _cmv40AutoTargetMkv(pid, t.value);
        }
      }
      // Si no hay pendingTarget, usuario debe provisionar manual (no auto)
      break;
    case 'target_provided':
      showToast('🤖 Auto: extrayendo BL/EL + per-frame', 'info');
      cmv40DoExtract(pid);
      break;
    case 'extracted':
      // Parada obligatoria: revisión visual del chart
      showToast('🤖 Auto: pausa en Fase D — revisa el chart y confirma sync', 'info');
      break;
    case 'sync_verified':
      showToast('🤖 Auto: inyectando RPU', 'info');
      _cmv40AutoInject(pid);
      break;
    case 'injected':
      showToast('🤖 Auto: remuxando MKV final', 'info');
      cmv40DoRemux(pid);
      break;
    case 'remuxed':
      showToast('🤖 Auto: validando', 'info');
      cmv40DoValidate(pid);
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
  _updateCMv40Panel(project);
  if (project.autoContinue) {
    showToast('🤖 Auto-avance activado', 'success');
    _cmv40MaybeAutoAdvance(project);
  } else {
    showToast('Auto-avance desactivado', 'info');
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
      project.session = data;
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
  showToast('Extrayendo RPU del MKV…', 'info');
  _cmv40PollPhase(pid, 'target_provided');
}

async function cmv40DoExtract(pid) {
  await apiFetch(`/api/cmv40/${pid}/extract`, { method: 'POST' });
  showToast('Extrayendo BL/EL y datos per-frame…', 'info');
  _cmv40PollPhase(pid, 'extracted');
}

async function cmv40DoInject(pid) {
  showConfirm(
    '¿Inyectar RPU?',
    'Esto creará EL_injected.hevc. ¿Has verificado que la sincronización es correcta?',
    async () => {
      await apiFetch(`/api/cmv40/${pid}/inject`, { method: 'POST' });
      showToast('Inyectando RPU…', 'info');
      _cmv40PollPhase(pid, 'injected');
    },
    'Inyectar',
  );
}

async function cmv40DoRemux(pid) {
  await apiFetch(`/api/cmv40/${pid}/remux`, { method: 'POST' });
  showToast('Remuxando a MKV final…', 'info');
  _cmv40PollPhase(pid, 'remuxed');
}

async function cmv40DoValidate(pid) {
  await apiFetch(`/api/cmv40/${pid}/validate`, { method: 'POST' });
  showToast('Validando MKV final…', 'info');
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
  if (!project.syncData) {
    const data = await apiFetch(`/api/cmv40/${pid}/sync-data`);
    if (!data) return;
    project.syncData = data;
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

  container.innerHTML = `
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
    </div>

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
          project.session = data;
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
      project.session = data;
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
      project.session = data;
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
