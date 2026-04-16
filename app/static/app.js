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
    el.style.display = (i === 1) ? 'flex' : (i === 2) ? 'flex' : 'block';
  });

}

// ═══════════════════════════════════════════════════════════════════
//  SUB-TABS (proyectos dentro de Tab 1)
// ═══════════════════════════════════════════════════════════════════

/**
 * Cambia el sub-tab activo dentro de Tab 1.
 * @param {string} id - 'cola' o project.id
 */
function switchSubTab(id) {
  activeSubTabId = id;
  document.getElementById('subtab-btn-cola')?.classList.toggle('active', id === 'cola');
  document.querySelectorAll('.subtab-proj').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.pid === id);
  });
  // Mostrar el panel correcto en #subtab-main (Cola o proyecto)
  document.querySelectorAll('#subtab-main .subtab-panel').forEach(panel => {
    const active = id === 'cola'
      ? panel.id === 'panel-cola'
      : panel.id === `panel-project-${id}`;
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
    switchSubTab(next ? next.id : 'cola');
  }
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
  const steps = ['mount', 'identify', 'chapters', 'mediainfo', 'dovi', 'rules'];
  let lastStep = 'mount';
  const pollId = setInterval(async () => {
    try {
      const prog = await apiFetch('/api/analyze/progress');
      if (prog?.step && prog.step !== lastStep && steps.includes(prog.step)) {
        const prevIdx = steps.indexOf(lastStep);
        const newIdx = steps.indexOf(prog.step);
        // Marcar como completados todos los pasos intermedios
        for (let i = prevIdx; i < newIdx; i++) {
          _advanceAnalyzeStep(steps[i], steps[i + 1]);
        }
        lastStep = prog.step;
      }
    } catch (_) { /* silenciar errores de polling */ }
  }, 500);

  const session = await apiFetch('/api/analyze', {
    method: 'POST',
    body: JSON.stringify({ iso_path: isoPath }),
  }, 120000);

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

/** Resetea todos los pasos del modal de análisis al estado inicial. */
function _resetAnalyzeSteps() {
  const steps = ['mount', 'identify', 'chapters', 'mediainfo', 'dovi', 'rules'];
  steps.forEach((s, i) => {
    const el = document.getElementById(`analyze-step-${s}`);
    if (!el) return;
    el.style.opacity = i === 0 ? '1' : '.4';
    el.textContent = el.textContent.replace(/^[✅⏳⬜]\s*/, i === 0 ? '⏳ ' : '⬜ ');
  });
}

/** Marca un paso como completado y activa el siguiente. */
function _advanceAnalyzeStep(doneStep, nextStep) {
  const doneEl = document.getElementById(`analyze-step-${doneStep}`);
  if (doneEl) {
    doneEl.style.opacity = '1';
    doneEl.textContent = doneEl.textContent.replace(/^[⏳⬜]\s*/, '✅ ');
  }
  const nextEl = document.getElementById(`analyze-step-${nextStep}`);
  if (nextEl) {
    nextEl.style.opacity = '1';
    nextEl.textContent = nextEl.textContent.replace(/^[⬜]\s*/, '⏳ ');
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
function _findOriginalTrackIndex(raw, type) {
  const bd = currentSession?.bdinfo_result;
  if (!bd) return -1;

  if (type === 'audio') {
    return bd.audio_tracks.findIndex(t =>
      t.language === raw.language && t.codec === raw.codec && t.description === raw.description
    );
  }
  // Subtítulos: comparar por idioma + bitrate (único combo)
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
      const rawLine = [raw.codec, raw.description, raw.bitrate_kbps ? `${raw.bitrate_kbps.toLocaleString()} kbps` : null].filter(Boolean).join(' · ');
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
      const tooltip = [
        `Codec: PGS (Presentation Graphics)`,
        `Idioma: ${raw.language || '—'} → ${langLiteral(raw.language) || '—'}`,
        `Tipo: ${subTypeLabel}`,
        raw.resolution ? `Resolución: ${raw.resolution}` : null,
        raw.bitrate_kbps ? `Bitrate: ${raw.bitrate_kbps} kbps` : null,
        `Posición en MKV: #${flatIdx + 1}`,
        '',
        `Razón: ${track.selection_reason || '—'}`,
      ].filter(Boolean).join('\n');
      const rawLine = `PGS · ${langLiteral(raw.language)} · ${subTypeLabel}`;
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
      const tipo = t.bitrate_kbps < 3 ? 'FORZADO (heurística)' : 'COMPLETO (heurística)';
      lines.push(`  #${i+1} lang="${t.language}" | bitrate_sintético=${t.bitrate_kbps} → ${tipo}`);
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
      lines.push(`  ${i+1}. [SUB] label="${t.label}" | tipo=${t.subtitle_type} | default=${t.flag_default} | forced=${t.flag_forced} | raw: lang="${raw.language}" bitrate=${raw.bitrate_kbps}`);
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
      lines.push(`  ${i+1}. [SUB] lang="${raw.language}" bitrate=${raw.bitrate_kbps}`);
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
