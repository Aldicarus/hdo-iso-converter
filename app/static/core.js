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
 *   isoPath:string, ws:WebSocket|null, sortableAudio:any, sortableSubs:any,
 *   mkvNameWasManual:boolean}>}
 */
const openProjects = [];

/** Sub-tab activo: null, 'empty' o el id del proyecto. @type {string|null} */
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
/** Timestamp de inicio del trabajo en curso (ms). @type {number|null} */

/** Líneas de log acumuladas del trabajo en curso (para filtrado). @type {string[]} */
/** Filtro activo del log en vivo: 'all' | 'warn'. @type {string} */
/** Timestamps de inicio/fin de cada fase para calcular elapsed y ETA. */
/** Último porcentaje de progreso reportado por mkvmerge (Fase D). */
/** IDs de items del historial actualmente expandidos. @type {Set<string>} */
/** IDs de items de la cola actualmente expandidos. @type {Set<string>} */

// Tabs (principales)
/** @type {number} Tab activo (1, 2 o 3). */
let currentTab = 1;

// ── Helpers de proyecto ───────────────────────────────────────────

/** Devuelve el proyecto activo, o null si no hay ninguno. */
function getActiveProject() {
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
  if (activeSubTabId) {
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
  // Lo PRIMERO: el marcado estático declara sus iconos con `data-icono` y
  // hasta que esto corre están vacíos.
  pintarIconos();
  _observarIconos();
  TooltipManager.init();
  loadSessions();
  checkAppStatus();
  connectQueueWebSocket();
  switchSubTab(null);
  _installSubtabScrollBindings();
  _installVisibilityRecovery();
  _instalarVigilanciaDeFin();
  _initUpdateCheckHeader();
  // Polling global de "jobs activos" para los dots verdes en los tabs.
  // Cada 5s pregunta al backend qué tabs tienen actividad y enciende /
  // apaga los indicadores. Coste mínimo (3 endpoints livianos) pero da al
  // usuario visibilidad inmediata de cualquier job, esté o no en el tab
  // del que viene.
  arrancarWorkbar();
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
  // La columna de trabajo: su poller se salta las vueltas con la pestaña
  // oculta, así que al volver hay que refrescarla aquí o se queda como estaba
  // al ocultarse.
  refrescarWorkbar();

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
  // Tab 2 — el análisis extendido ya no deja ningún fetch abierto esperando:
  // se encola, y el resultado lo recoge `_mkvRecogerAnalisis` cuando la
  // columna dice que el trabajo terminó. El refresco de la columna que hay
  // más abajo es lo único que hace falta tras despertar el Mac.
  // Tab 2 — apply (copia desde Library): si hay job activo en backend,
  // el modal puede estar congelado en "esperando" — forzar tick.

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

  // Las tres pestañas tienen columna izquierda. Tab 2 la tuvo vacía mucho
  // tiempo y aquí se ocultaba el #sidebar entero para que ocupara todo el
  // ancho; desde que lista los MKVs analizados, ya no.
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
  // Tab 2: refrescar la columna de MKVs analizados. Se pide al entrar y no al
  // arrancar (igual que el sidebar de Tab 3): quien nunca abre esta pestaña no
  // paga la petición.
  //
  // Aquí había también un `_mkvCheckActiveApply` que reabría el modal de una
  // copia en curso al volver a la pestaña. Se retiró con los modales propios
  // de Tab 2: la columna de trabajo enseña esa copia esté uno donde esté, sin
  // que nadie tenga que reabrir nada.
  if (n === 2 && typeof refrescarMkvRecientes === 'function') {
    refrescarMkvRecientes();
  }
}

// ═══════════════════════════════════════════════════════════════════
//  SUB-TABS (proyectos dentro de Tab 1)
// ═══════════════════════════════════════════════════════════════════

/**
 * Cambia el sub-tab activo dentro de Tab 1.
 * @param {string} id - project.id, o 'empty' si no hay ninguno abierto
 */
function switchSubTab(id) {
  // Si no hay proyectos abiertos, estado vacío. La sub-pestaña de trabajos
  // se retiró: su contenido es hoy el modal de detalle de un rip, y el
  // centro de las tres pestañas es solo proyectos.
  if (!id && openProjects.length === 0) id = 'empty';
  activeSubTabId = id;
  document.querySelectorAll('.subtab-proj').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.pid === id);
  });
  // Mostrar el panel correcto en #subtab-main (Cola, proyecto o estado vacío)
  document.querySelectorAll('#subtab-main .subtab-panel').forEach(panel => {
    const active = id === 'empty'
      ? panel.id === 'panel-empty-projects'
      : panel.id === `panel-project-${id}`;
    panel.classList.toggle('active-panel', active);
  });
  const main = document.getElementById('subtab-main');
  if (main) main.scrollTop = 0;
  const project = getActiveProject();
  currentSession = project ? project.session : null;
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
    <span class="unsaved-dot" id="unsaved-dot-${project.id}" style="display:none" data-tooltip="Cambios sin guardar"></span>
    <span class="subtab-proj-icon" id="subtab-icon-${project.id}">${icon}</span>
    <span class="subtab-proj-name" data-tooltip="${escHtml(project.name)}">${escHtml(project.name.slice(0,24))}${project.name.length > 24 ? '…' : ''}</span>
    <button class="subtab-proj-close" onclick="closeProject('${project.id}',event)"
      data-tooltip="Cerrar proyecto">×</button>`;
  btn.onclick = (e) => { if (!e.target.closest('.subtab-proj-close')) switchSubTab(project.id); };
  container.appendChild(btn);
  _updateSubtabScrollState();
}

/** Config de los scrollers de pestañas de las TRES pestañas. Misma lógica, IDs distintos. */
const _SUBTAB_SCROLLERS = [
  { areaId: 'subtab-projects-area',       scrollId: 'subtab-projects',       leftId: 'subtab-scroll-left',       rightId: 'subtab-scroll-right'       },
  { areaId: 'cmv40-subtab-projects-area', scrollId: 'cmv40-subtab-projects', leftId: 'cmv40-subtab-scroll-left', rightId: 'cmv40-subtab-scroll-right' },
  { areaId: 'mkv-subtab-projects-area',   scrollId: 'mkv-subtab-projects',   leftId: 'mkv-subtab-scroll-left',   rightId: 'mkv-subtab-scroll-right'   },
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
function scrollMkvSubtabProjects(direction)   { _scrollSubtabContainer('mkv-subtab-projects', direction); }

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
  // Los mismos glifos que la tarjeta de proyecto: un estado se dibuja
  // igual en toda la aplicación.
  const map = { pending: 'disco', queued: 'pausa', done: 'check', error: 'cruz' };
  return icono(map[status] || 'disco');
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
    // `innerHTML` y no `textContent`: ahora es un SVG.
    iconEl.innerHTML = projectStatusIcon(status);
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
      <span class="banner-icon"><span data-icono="disco"></span></span>
      <div><strong id="${pid}-iso-missing-title">Origen no disponible.</strong>
        <span id="${pid}-iso-missing-text"></span>
        Puedes editar los parámetros, pero no podrás ejecutar hasta que el origen vuelva a estar accesible.
      </div>
    </div>

    <div id="${pid}-vo-warning-banner" class="banner warning" style="display:none">
      <span class="banner-icon"><span data-icono="aviso"></span></span>
      <div><strong>VO no determinada automáticamente.</strong>
        <span id="${pid}-vo-warning-text"></span>
        Revisa las pistas incluidas y ajusta los flags manualmente.
      </div>
    </div>

    <div class="project-phase-strip-row">
      <div class="project-phase-strip"
        data-tooltip="Análisis mkvmerge completado → Reglas automáticas aplicadas → En revisión">
        <span class="pps-step done"><span data-icono="lupa"></span> Análisis</span>
        <span class="pps-conn">→</span>
        <span class="pps-step done"><span data-icono="rayo"></span> Reglas</span>
        <span class="pps-conn">→</span>
        <span class="pps-step active"><span data-icono="portapapeles"></span> Revisión</span>
        <span class="pps-conn">→</span>
        <span class="pps-step muted">${icono('flechaAbajo')} mkvmerge</span>
      </div>
      <button class="btn btn-ghost btn-xs" onclick="showRawAnalysisData()"
        data-tooltip="Ver los datos de análisis originales del ISO (mkvmerge -J + capítulos + reglas)"><span data-icono="lupaOnda"></span> Datos ISO</button>
    </div>

    <div class="section-card globals-card">
      <div class="section-header">
        <span class="section-icon"><span data-icono="caja"></span></span>
        <div><div class="section-title">Nombre del MKV</div><div class="section-subtitle">Se recalcula automáticamente al cambiar los toggles</div></div>
      </div>
      <div class="globals-body">
        <div class="globals-mkv-row">
          <input type="text" id="${pid}-mkv-name-input" class="globals-mkv-input" oninput="onMkvNameInput()"
            data-tooltip="Nombre del MKV de salida. Se genera automáticamente.\nEdítalo manualmente si necesitas otro nombre.">
          <div id="${pid}-mkv-name-manual-notice" class="manual-notice" style="display:none">
            ${icono('lapiz')} Editado manualmente
            <button class="btn btn-xs btn-ghost" onclick="revertMkvName()"
              data-tooltip="Restaurar el nombre generado automáticamente.">Revertir</button>
          </div>
          <div id="${pid}-mkv-dcp-chip" class="globals-mkv-chip" style="display:none"
            data-tooltip="El nombre del ISO contiene el tag 'Audio DCP'.\nAñade el sufijo (DCP 9.1.6) a la pista TrueHD Atmos en Castellano.">
            ${icono('grafico')} Audio DCP — detectado en el nombre del ISO
          </div>
          <div id="${pid}-mkv-size-chip" class="globals-mkv-chip globals-mkv-chip--size" style="display:none"
            data-tooltip="Estimación del tamaño del MKV final.\nSale de restar al m2ts de origen las pistas de audio que se descartan y la sobrecarga del contenedor Blu-ray.\nEl pipeline copia los flujos sin recodificar, así que es contabilidad, no una predicción — pero el margen es de ±10%."></div>
        </div>
        <div class="globals-info-row">
          <div class="global-info-item" id="${pid}-dv-card">
            <span class="global-card-icon" id="${pid}-dv-icon"><span data-icono="claqueta"></span></span>
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
            <span class="global-card-icon"><span data-icono="tv"></span></span>
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
        <span class="section-icon"><span data-icono="grafico"></span></span>
        <div><div class="section-title">Audio</div><div class="section-subtitle">Arrastra para reordenar · pulsa la cruz para descartar</div></div>
        <span class="section-badge" id="${pid}-audio-count">0 pistas</span>
      </div>
      <div style="padding:0 16px 10px; display:flex; gap:6px; align-items:center; font-size:12px; flex-wrap:wrap">
        <span style="color:var(--text-3)">Modo:</span>
        <button class="btn btn-xs mode-toggle active" data-mode="filtered" data-track="audio"
          onclick="setTrackMode('audio','filtered')"
          data-tooltip="Solo Castellano + VO con selección por calidad"><span data-icono="diana"></span> Castellano + VO</button>
        <button class="btn btn-xs mode-toggle" data-mode="keep_all" data-track="audio"
          onclick="setTrackMode('audio','keep_all')"
          data-tooltip="Mantener todas las pistas con labels automáticos (sin reordenar ni descartar)"><span data-icono="portapapeles"></span> Mantener todas</button>
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
        <span class="section-icon"><span data-icono="etiqueta"></span></span>
        <div><div class="section-title">Subtítulos</div><div class="section-subtitle">Arrastra para reordenar · pulsa la cruz para descartar</div></div>
        <span class="section-badge" id="${pid}-sub-count">0 pistas</span>
      </div>
      <div style="padding:0 16px 10px; display:flex; gap:6px; align-items:center; font-size:12px; flex-wrap:wrap">
        <span style="color:var(--text-3)">Modo:</span>
        <button class="btn btn-xs mode-toggle active" data-mode="filtered" data-track="subtitle"
          onclick="setTrackMode('subtitle','filtered')"
          data-tooltip="Solo Castellano + VO + Inglés. Detecta forzados por tamaño relativo (completo/forzado ≥3×) y descarta pistas en otros idiomas."><span data-icono="diana"></span> Castellano + VO + Inglés</button>
        <button class="btn btn-xs mode-toggle" data-mode="keep_all" data-track="subtitle"
          onclick="setTrackMode('subtitle','keep_all')"
          data-tooltip="Mantener todos los subtítulos con labels automáticos (sin reordenar ni descartar)"><span data-icono="portapapeles"></span> Mantener todos</button>
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
        <span class="section-icon"><span data-icono="libro"></span></span>
        <div><div class="section-title">Capítulos</div><div class="section-subtitle">Clic en la barra para añadir · arrastra para ajustar · la cruz elimina</div></div>
      </div>
      <div class="section-body">
        <div id="${pid}-chapters-auto-banner" class="banner info" style="display:none">
          <span class="banner-icon" id="${pid}-chapters-auto-icon"><span data-icono="aviso"></span></span>
          <span id="${pid}-chapters-auto-text"></span>
          <button class="btn btn-xs" id="${pid}-chapters-generic-btn" style="display:none; margin-left:auto"
            onclick="setGenericChapterNames()"
            data-tooltip="Reemplaza todos los nombres por Capítulo 01, Capítulo 02… (mantiene timestamps)"><span data-icono="etiqueta"></span> Nombres genéricos</button>
          <button class="btn btn-xs" id="${pid}-chapters-reset-btn" style="display:none"
            onclick="resetChaptersFromDisc()"
            data-tooltip="Extrae los capítulos originales del disco (MPLS) y reemplaza los actuales (automáticos o editados)."><span data-icono="refrescar"></span> Restaurar del disco</button>
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
        <span class="section-icon"><span data-icono="grafico"></span></span>
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
                <th data-tooltip="Montar ISO via loop mount"><span data-icono="disco"></span> Montar</th>
                <th data-tooltip="mkvmerge: MPLS → MKV"><span data-icono="flechaAbajo"></span> mkvmerge</th>
                <th data-tooltip="Desmontar ISO (umount)"><span data-icono="candadoAbierto"></span> Desmontar</th>
                <th data-tooltip="mkvpropedit in-place (solo ruta sin reordenación, — en ruta directa)"><span data-icono="lapiz"></span> Propedit</th>
                <th data-tooltip="Duración total de la ejecución"><span data-icono="reloj"></span> Total</th>
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
        data-tooltip="Guardar los cambios sin ejecutar"><span data-icono="caja"></span> Guardar</button>
      <button class="btn btn-success btn-lg" id="${pid}-execute-btn" onclick="executeSession()"
        data-tooltip="Confirmar y añadir a la cola de ejecución">
        <span data-icono="play"></span> Confirmar y ejecutar
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
    saveCloseBtn.innerHTML = icono('caja') + ' Guardar y cerrar';
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

/**
 * Aviso al cerrar la pestaña de un rip que está corriendo o encolado.
 *
 * NO es una confirmación, a propósito: cerrar la pestaña no toca el trabajo.
 * La cola vive en el backend y el log se persiste en la sesión, así que el rip
 * sigue y al reabrir el proyecto se ve entero. Lo único que se pierde es la
 * consola en vivo, y eso el usuario no tiene por qué saberlo — de ahí el toast
 * en vez del modal, que solo añadiría un clic para decir "sí, ciérralo".
 */
function _avisarSiCerramosUnRipEnMarcha(project) {
  const estado = project.session?.status;
  if (estado !== 'running' && estado !== 'queued') return;
  showToast(
    estado === 'running'
      ? `"${project.name}" sigue ejecutándose — míralo en la Cola`
      : `"${project.name}" sigue en la cola — míralo en la Cola`,
    'info');
}

/** Elimina el proyecto del array y limpia el DOM. */
function _doCloseProject(pid) {
  const idx = openProjects.findIndex(p => p.id === pid);
  if (idx === -1) return;

  const project = openProjects[idx];
  _avisarSiCerramosUnRipEnMarcha(project);
  if (project.ws) { project.ws.close(); project.ws = null; }
  // Los dos Sortable del panel. Antes se destruía `project.sortable`, que Tab 1
  // no asigna nunca, y los dos que sí existen se quedaban vivos con sus
  // listeners sobre un DOM que estamos a punto de borrar.
  for (const k of ['sortableAudio', 'sortableSubs']) {
    if (project[k]) { project[k].destroy(); project[k] = null; }
  }

  document.getElementById(`panel-project-${pid}`)?.remove();
  document.querySelector(`.subtab-proj[data-pid="${pid}"]`)?.remove();
  openProjects.splice(idx, 1);
  _updateSubtabScrollState();

  // Activar el sub-tab más cercano
  if (activeSubTabId === pid) {
    const next = openProjects[idx] || openProjects[idx - 1];
    switchSubTab(next ? next.id : 'empty');
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
  // El icono lo pone el TIPO. Los mensajes traían además el suyo delante, así
  // que salía dos veces: «✅ ✅ Pipeline completado».
  const icons = { success: 'check', error: 'cruz', warning: 'aviso', info: 'info' };
  const container = document.getElementById('toast-container');
  const t = document.createElement('div');
  const id = `toast-${++_toastIdCounter}`;
  t.id = id;
  t.className = `toast ${type}`;
  t.innerHTML = `<span class="toast-icon">${icono(icons[type] || 'info', 'ico-md')}</span>
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

// ═══════════════════════════════════════════════════════════════════
//  AVISO AL TERMINAR UN TRABAJO LARGO
// ═══════════════════════════════════════════════════════════════════
//
// Un rip de Tab 1 son 20-40 min y una fase de Tab 3 puede pasar de la hora.
// El usuario se va a hacer otra cosa y tiene que volver a mirar cada rato
// para saber si acabó. Esto se lo dice.
//
// Dos decisiones que condicionan el diseño:
//
// 1. **El NAS va por HTTP, así que no hay contexto seguro** y la Notification
//    API del navegador NO está disponible ahí (sí en `localhost`, o sea en
//    desarrollo). El aviso que SIEMPRE funciona es el título parpadeante de la
//    pestaña, que es el que se usa de base; la notificación de escritorio y el
//    pitido son extras cuando el navegador los permite.
//
// 2. **No se añade tráfico cuando no hace falta.** El poller de los puntos
//    verdes se salta las vueltas con `document.hidden` a propósito (eran dos
//    peticiones cada 5 s durante horas contra un NAS que está procesando
//    vídeo), y eso no se toca. En su lugar, al ocultarse la pestaña se toma
//    una foto de qué tabs tenían trabajo; si no había ninguno **no se pollea
//    nada**, y si lo había se pregunta cada 20 s hasta que termine. O sea: 3
//    peticiones por minuto sólo mientras estás fuera y con algo corriendo.

const _AVISO_INTERVALO_MS = 20000;
const _AVISO_PREF = 'hdo_avisar_fin_trabajo';
const _AVISO_SONIDO_PREF = 'hdo_avisar_con_sonido';
const _AVISO_NOMBRES = { 1: 'Blu-Ray ISO → MKV', 2: 'Editar MKV', 3: 'Upgrade CMv4.0' };

let _avisoTrabajosPrevios = null;   // {1,2,3} → bool, foto al ocultarse
let _avisoTimer = null;
let _avisoTituloOriginal = null;
let _avisoParpadeoTimer = null;

/** ¿Está activado el aviso? Por defecto sí — es informativo y no invasivo. */
function avisoFinActivado() {
  return localStorage.getItem(_AVISO_PREF) !== '0';
}

function setAvisoFinActivado(on) {
  localStorage.setItem(_AVISO_PREF, on ? '1' : '0');
  if (!on) _pararParpadeo();
}

function avisoSonidoActivado() {
  return localStorage.getItem(_AVISO_SONIDO_PREF) === '1';
}

function setAvisoSonidoActivado(on) {
  localStorage.setItem(_AVISO_SONIDO_PREF, on ? '1' : '0');
}

/**
 * ¿Se puede usar la Notification API? Requiere contexto seguro (HTTPS o
 * localhost), así que en el NAS por HTTP la respuesta es NO y el usuario
 * se queda con el título parpadeante.
 */
function avisoNotificacionDisponible() {
  return typeof Notification !== 'undefined' && window.isSecureContext;
}

/** Pide permiso de notificación. Devuelve el permiso resultante. */
async function pedirPermisoNotificaciones() {
  if (!avisoNotificacionDisponible()) return 'unsupported';
  if (Notification.permission !== 'default') return Notification.permission;
  try { return await Notification.requestPermission(); }
  catch (_) { return 'denied'; }
}

/**
 * Lee el estado de trabajo de los tres tabs. Silencioso: es background.
 *
 * De `/api/trabajos`, la misma fuente que la columna de trabajo — que es lo
 * que hay que mirar cuando la pestaña está oculta y la columna no pollea.
 * Antes esto preguntaba a dos endpoints y a Tab 2 solo por la copia desde
 * biblioteca, así que **un análisis extendido de diez minutos terminaba sin
 * avisar**, justo el caso para el que existe.
 *
 * `queueState` se sigue consultando para Tab 1 porque llega por WebSocket y
 * ya está en memoria: no cuesta nada y cubre el instante entre encolar y que
 * el servidor lo refleje.
 */
async function _leerTrabajosActivos() {
  const estado = { 1: false, 2: false, 3: false };
  estado[1] = !!(queueState && (queueState.running ||
                                (queueState.queue && queueState.queue.length)));
  const st = await apiFetch('/api/trabajos', { silent: true }).catch(() => null);
  if (!st) return estado;
  const tabs = new Set([
    ...(st.activo ? [st.activo.tab] : []),
    ...(st.cola || []).map(j => j.tab),
    ...(st.interactivo || []).map(t => t.tab),
  ]);
  estado[1] = estado[1] || tabs.has('rip');
  estado[2] = tabs.has('mkv');
  estado[3] = tabs.has('cmv40');
  return estado;
}

/** Un pitido corto con WebAudio — sin fichero de audio que servir. */
function _pitido() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    const ctx = new AC();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.type = 'sine';
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.45);
    osc.start(); osc.stop(ctx.currentTime + 0.5);
    setTimeout(() => { try { ctx.close(); } catch (_) {} }, 800);
  } catch (_) { /* el audio nunca debe romper nada */ }
}

function _pararParpadeo() {
  if (_avisoParpadeoTimer) { clearInterval(_avisoParpadeoTimer); _avisoParpadeoTimer = null; }
  if (_avisoTituloOriginal !== null) { document.title = _avisoTituloOriginal; _avisoTituloOriginal = null; }
}

/** Alterna el título de la pestaña hasta que el usuario vuelva. */
function _arrancarParpadeo(texto) {
  if (_avisoParpadeoTimer) clearInterval(_avisoParpadeoTimer);
  if (_avisoTituloOriginal === null) _avisoTituloOriginal = document.title;
  let alterno = false;
  document.title = texto;
  _avisoParpadeoTimer = setInterval(() => {
    alterno = !alterno;
    document.title = alterno ? _avisoTituloOriginal : texto;
  }, 1200);
}

/**
 * Avisa de que un trabajo terminó. `tab` es el número interno (1/2/3).
 * No comprueba `document.hidden`: quien llama ya sabe que el usuario no está
 * mirando — avisar de algo que se ve en pantalla sería ruido.
 */
function avisarFinDeTrabajo(tab) {
  if (!avisoFinActivado()) return;
  const nombre = _AVISO_NOMBRES[tab] || 'Trabajo';
  _arrancarParpadeo(`${nombre} — terminado`);
  if (avisoSonidoActivado()) _pitido();
  if (avisoNotificacionDisponible() && Notification.permission === 'granted') {
    try {
      const n = new Notification('HDO Blu-ray Toolkit', {
        body: `${nombre}: el trabajo ha terminado.`,
        tag: `hdo-fin-${tab}`,       // sustituye al anterior del mismo tab
      });
      n.onclick = () => { window.focus(); try { n.close(); } catch (_) {} };
    } catch (_) { /* el título parpadeante ya cumple */ }
  }
}

async function _avisoTick() {
  let ahora;
  try { ahora = await _leerTrabajosActivos(); }
  catch (_) { return; }   // red caída: se reintenta en el siguiente tick
  let quedaAlguno = false;
  for (const tab of [1, 2, 3]) {
    if (_avisoTrabajosPrevios && _avisoTrabajosPrevios[tab] && !ahora[tab]) {
      avisarFinDeTrabajo(tab);
    } else if (ahora[tab]) {
      quedaAlguno = true;
    }
  }
  _avisoTrabajosPrevios = ahora;
  if (!quedaAlguno) _pararVigilancia();
}

function _pararVigilancia() {
  if (_avisoTimer) { clearInterval(_avisoTimer); _avisoTimer = null; }
  _avisoTrabajosPrevios = null;
}

/**
 * Al ocultarse la pestaña: si había algo corriendo, vigilar hasta que acabe.
 * Si no había nada, no se pollea — la foto vacía evita el tráfico inútil.
 */
async function _instalarVigilanciaDeFin() {
  document.addEventListener('visibilitychange', async () => {
    if (!document.hidden) { _pararParpadeo(); _pararVigilancia(); return; }
    if (!avisoFinActivado() || _avisoTimer) return;
    try {
      const foto = await _leerTrabajosActivos();
      if (![1, 2, 3].some(t => foto[t])) return;   // nada que vigilar
      _avisoTrabajosPrevios = foto;
      _avisoTimer = setInterval(_avisoTick, _AVISO_INTERVALO_MS);
    } catch (_) { /* si falla, simplemente no se vigila esta vez */ }
  });
  window.addEventListener('focus', _pararParpadeo);
}


// ── La tarjeta de un proyecto ───────────────────────────────────────────────
//
// UNA para las tres columnas, con el lenguaje que ya usa la columna de
// trabajo: carátula, título, una línea de subtítulo, etiquetas y la meta a la
// derecha. Antes cada pestaña escribía su propio HTML —el mismo, copiado tres
// veces— y en las tres se dedicaba **dos filas enteras a fechas rotuladas**
// («Modif.», «Ejecuc.», «Analiz.») mientras lo que distingue un proyecto de
// otro no se veía en ninguna.
//
// Tres decisiones que no son cosméticas:
//
//  · **Los tags del nombre salen del título y pasan a etiquetas.** Los tres
//    nombres los llevan (`Peli (2026) [DV FEL] [Audio DCP].mkv`) y el título
//    se corta por la derecha con `ellipsis`, o sea que lo primero que se
//    perdía era justo lo que distingue una versión de otra.
//  · **El acento lateral lleva el ESTADO, no la pestaña.** En la columna de
//    trabajo el color dice de qué pestaña viene un trabajo, y eso la hace
//    escaneable; dentro de un sidebar todas las tarjetas son de la misma
//    pestaña, así que ese color sería constante y no diría nada. Aquí lo que
//    cambia de una fila a otra es en qué punto está.
//  · **Los rótulos de la meta se van.** Una fecha relativa en la esquina no
//    necesita que le pongan «Modif.» delante; la fecha completa y de qué es
//    siguen en el tooltip.
//
// `o` = { titulo, tituloTooltip, sub, subTooltip, chips[], estado,
//         estadoTooltip, estadoHtml, poster, icono, meta, metaIso,
//         metaTooltip, pips{hechas,total,tooltip}, insignia, abierto,
//         acciones }

/** El póster al ancho que hace falta. TMDb sirve cada tamaño como una imagen
 *  distinta y la ficha guarda la de 342 px: en una lista de 117 proyectos eso
 *  son 117 imágenes de un tamaño que no se ve. `w92` cubre de sobra los 36 px
 *  de la miniatura en pantalla de retina. */
function miniaturaDe(url, ancho = 'w92') {
  return (url || '').replace(/(\/t\/p\/)w\d+(\/)/, `$1${ancho}$2`);
}

/** Parte `Peli (2026) [DV FEL] [Audio DCP].mkv` en título y etiquetas. */
function nombreYTags(nombre) {
  const limpio = (nombre || '').replace(/\.mkv$/i, '');
  const tags = [...limpio.matchAll(/\[([^\]]+)\]/g)].map(m => m[1].trim());
  return { titulo: limpio.replace(/\s*\[[^\]]+\]/g, '').trim() || limpio, tags };
}

// A partir de aquí el chip se recorta (132 px ≈ 22 caracteres), así que el
// texto completo tiene que poder leerse en el tooltip. Por debajo NO se pone
// ninguno: un tooltip que repite «DV FEL» sobre la etiqueta «DV FEL» es ruido.
const _PROJ_CHIP_LARGO = 22;

function _projChipsHTML(chips) {
  const c = (chips || []).filter(Boolean);
  if (!c.length) return '';
  return `<div class="proj-chips">${c.map(ch => {
    const txt = String(ch.txt || '');
    const tip = ch.tooltip || (txt.length > _PROJ_CHIP_LARGO ? txt : '');
    return `<span class="proj-chip`
      + `${ch.tono ? ' tono-' + ch.tono : ''}${ch.apagado ? ' apagado' : ''}"`
      + `${tip ? ` data-tooltip="${escHtml(tip)}"` : ''}>`
      + `${escHtml(txt)}</span>`;
  }).join('')}</div>`;
}

/** Un punto por fase: por dónde va el proyecto, sin gastar una línea.
 *
 *  Es el mismo recurso que la columna de trabajo usa para el trabajo en
 *  curso, y aquí responde la pregunta de Tab 3 —¿en qué punto está?— que
 *  antes había que leer en un texto («Fase: Extraído») sin saber si eso es
 *  el principio o el final.
 */
function _projPipsHTML(p) {
  if (!p || !p.total || p.total < 2) return '';
  const puntos = [];
  for (let i = 1; i <= p.total; i++) {
    puntos.push(`<span class="proj-pip${i <= p.hechas ? ' hecha' : ''}"></span>`);
  }
  return `<div class="proj-pips"${p.tooltip ? ` data-tooltip="${escHtml(p.tooltip)}"` : ''}>`
       + `${puntos.join('')}</div>`;
}

function tarjetaDeProyecto(o) {
  // El icono va DEBAJO de la carátula, no en su lugar: si la imagen no carga,
  // el `onerror` la retira y el icono sigue ahí. Es lo que hace la columna de
  // trabajo, y evita el hueco gris que no dice de qué es la fila.
  const mini = `<div class="proj-mini">${o.icono || ''}${o.poster
    ? `<img src="${escHtml(miniaturaDe(o.poster))}" alt="" loading="lazy"
           onerror="this.remove()">` : ''}</div>`;
  const estado = o.estadoHtml
    || (o.estado && typeof iconoDeEstado === 'function'
        ? iconoDeEstado(o.estado, 'icono-chip-sm') : '');
  return `
    <div class="session-card-row">
      ${mini}
      <div class="session-card-body">
        <div class="session-card-title"${o.tituloTooltip
            ? ` data-tooltip="${escHtml(o.tituloTooltip)}"` : ''}>${escHtml(o.titulo || '')}</div>
        ${o.sub ? `<div class="proj-sub"${o.subTooltip
            ? ` data-tooltip="${escHtml(o.subTooltip)}"` : ''}>${escHtml(o.sub)}</div>` : ''}
        <div class="proj-pie">
          ${_projChipsHTML(o.chips)}
          ${o.meta ? `<span class="proj-fecha relative-date" data-iso="${escHtml(o.metaIso || '')}"
              ${o.metaTooltip ? `data-tooltip="${escHtml(o.metaTooltip)}"` : ''}>${escHtml(o.meta)}</span>` : ''}
        </div>
      </div>
      <div class="proj-der">
        ${estado ? `<span class="proj-estado"${o.estadoTooltip
            ? ` data-tooltip="${escHtml(o.estadoTooltip)}"` : ''}>${estado}</span>` : ''}
        ${o.insignia || ''}
        ${o.abierto ? '<span class="session-item-badge">abierto</span>' : ''}
      </div>
    </div>
    ${_projPipsHTML(o.pips)}
    ${o.acciones ? `<div class="session-card-actions">${o.acciones}</div>` : ''}`;
}


// ── Elegir la ficha de una película a mano ──────────────────────────────────
//
// `tmdb_info` se rellenaba SOLO al crear el proyecto y best-effort, así que
// una sesión creada antes de que hubiera API key —o cuando TMDb no contestó—
// se quedaba sin ficha **para siempre**: nada lo reintentaba. Medido sobre el
// NAS, 9 de 44 sesiones de Tab 1 no tenían, y **8 de las 9 dan match perfecto
// con solo volver a preguntar**; eso lo hace ya el backend al abrir el
// proyecto, sin que nadie pulse nada.
//
// Esto es para el noveno —`THE_MANDALORIAN_AND_GROGU_UHD`, sin año y con
// guiones bajos, donde no hay heurística que valga— y para corregir un match
// que apuntó a otra película del mismo título.

let _fichaDestino = null;   // { tipo: 'rip'|'cmv40', id, nombre }
let _fichaCandidatos = [];

/** Abre el selector, con el título del proyecto ya escrito. */
function abrirSelectorDeFicha(tipo, id, nombre) {
  _fichaDestino = { tipo, id, nombre: nombre || '' };
  _fichaCandidatos = [];
  // El nombre viene con sus tags y su extensión; el mismo troceo que usa la
  // tarjeta sirve para dejar el campo listo para buscar.
  const { titulo } = nombreYTags(nombre || '');
  const m = titulo.match(/^(.*?)\s*\((\d{4})\)\s*$/);
  const campoT = document.getElementById('ficha-titulo');
  const campoA = document.getElementById('ficha-anio');
  if (campoT) campoT.value = (m ? m[1] : titulo).trim();
  if (campoA) campoA.value = m ? m[2] : '';
  const sub = document.getElementById('ficha-modal-sub');
  if (sub) sub.textContent = nombre || '';
  const res = document.getElementById('ficha-resultados');
  if (res) res.innerHTML = '';
  openModal('ficha-modal');
  if (campoT) campoT.focus();
  // Con el título ya puesto, la primera búsqueda se hace sola: en el caso
  // normal el usuario solo tiene que elegir.
  buscarCandidatosDeFicha();
}

async function buscarCandidatosDeFicha() {
  const res = document.getElementById('ficha-resultados');
  const titulo = (document.getElementById('ficha-titulo')?.value || '').trim();
  if (!res) return;
  if (!titulo) {
    res.innerHTML = '<div class="cmv40-lookup-empty">Escribe un título para buscar.</div>';
    return;
  }
  const anioTxt = (document.getElementById('ficha-anio')?.value || '').trim();
  res.innerHTML = `<div class="cmv40-lookup-loading">
    <span class="cmv40-rec-spinner-inline"></span> Buscando en TMDb…</div>`;
  const r = await apiFetch('/api/cmv40/tmdb-search', {
    method: 'POST',
    body: JSON.stringify({ title: titulo, year: anioTxt || null }),
  });
  if (!r) { res.innerHTML = '<div class="cmv40-lookup-empty">No se pudo consultar TMDb.</div>'; return; }
  if (!r.tmdb_configured) {
    res.innerHTML = '<div class="cmv40-lookup-empty">Falta la API key de TMDb '
                  + '(Configuración).</div>';
    return;
  }
  _fichaCandidatos = r.candidates || [];
  if (!_fichaCandidatos.length) {
    res.innerHTML = '<div class="cmv40-lookup-empty">Sin coincidencias. '
                  + 'Prueba con el título original o quita el año.</div>';
    return;
  }
  res.innerHTML = `<div class="cmv40-lookup-picks">${
    _fichaCandidatos.map((c, i) => {
      const poster = c.poster_url
        ? `<img class="cmv40-lookup-pick-poster" src="${escHtml(c.poster_url)}" alt="" loading="lazy">`
        : '<div class="cmv40-lookup-pick-poster cmv40-lookup-pick-noposter"></div>';
      const nota = c.vote_average > 0
        ? `<span class="cmv40-lookup-pick-rating">${c.vote_average.toFixed(1)}</span>` : '';
      const orig = (c.title_en && c.title_en !== c.title_es)
        ? `<div class="cmv40-lookup-pick-orig">Original: ${escHtml(c.title_en)}</div>` : '';
      return `
        <button class="cmv40-lookup-pick" type="button" onclick="elegirFicha(${i})">
          ${poster}
          <div class="cmv40-lookup-pick-info">
            <div class="cmv40-lookup-pick-title">
              ${escHtml(c.title_es || c.title_en || '—')}
              ${c.year ? `<span class="cmv40-lookup-pick-year">(${c.year})</span>` : ''}
              ${nota}
            </div>
            ${orig}
            ${c.overview ? `<div class="cmv40-lookup-pick-overview">${escHtml(c.overview)}</div>` : ''}
          </div>
        </button>`;
    }).join('')}</div>`;
}

/** Fija la película elegida en el proyecto y refresca lo que la enseña. */
async function elegirFicha(i) {
  const c = _fichaCandidatos[i];
  if (!c || !_fichaDestino) return;
  const { tipo, id } = _fichaDestino;
  const url = tipo === 'cmv40'
    ? `/api/cmv40/${id}/tmdb-refresh`
    : `/api/sessions/${id}/tmdb-refresh`;
  const r = await apiFetch(url, {
    method: 'POST', body: JSON.stringify({ tmdb_id: c.tmdb_id }),
  });
  if (!r || !r.updated) { showToast('No se pudo guardar la ficha', 'error'); return; }
  closeModal('ficha-modal');
  showToast(`Ficha de «${c.title_es || c.title_en}» guardada`, 'success');
  // Repintar donde se ve: la cabecera del proyecto abierto y la columna.
  if (tipo === 'cmv40') {
    if (typeof refreshCMv40Sidebar === 'function') refreshCMv40Sidebar();
    if (typeof _refreshCMv40Session === 'function') _refreshCMv40Session(id);
  } else {
    if (typeof loadSessions === 'function') loadSessions();
    if (typeof refreshOpenProjectState === 'function') refreshOpenProjectState(id);
  }
}

/** El botón que abre el selector, para la cabecera del proyecto. */
function botonDeFicha(ctx, conFicha) {
  if (!ctx || !ctx.id) return '';
  const arg = `'${escHtml(ctx.tipo)}','${escHtml(ctx.id)}',` 
            + `'${escHtml(String(ctx.nombre || '').replace(/'/g, ''))}'`;
  return conFicha
    ? `<a class="tmdb-cambiar" role="button" tabindex="0"
         onclick="abrirSelectorDeFicha(${arg})"
         data-tooltip="Elegir otra película si esta no es la correcta">Cambiar película</a>`
    : `<div class="tmdb-sin-ficha">
         <span>Sin ficha de TMDb — no hay carátula ni sinopsis.</span>
         <button class="btn btn-primary btn-xs" onclick="abrirSelectorDeFicha(${arg})"
           data-tooltip="Buscar la película en TMDb y guardarla en el proyecto">Buscar película</button>
       </div>`;
}


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

// ── El catálogo de iconos de la aplicación ──────────────────────────────────
//
// Un emoji lo dibuja el sistema operativo: cambia de forma y de color entre
// máquinas, no hereda la paleta y a un ⏳ o un 📂 no hay manera de quitarles
// el aire de conversación de chat. Un `<svg>` con `currentColor` hereda el
// color del sitio donde se pone, se anima con CSS y pesa lo mismo.
//
// Todo comparte rejilla de 24, trazo 1.6 y extremos redondeados — el criterio
// de Material Symbols en su variante `outlined`—, que es lo que hace que se
// lean como una familia y no como una colección de dibujos.
//
// **Los glifos se declaran UNA vez.** Los de tipo de trabajo y de estado
// (`_GLIFOS_TRABAJO`, `_ICONOS_ESTADO`, más abajo) salen de aquí: un objeto
// que se pinta en dos sitios no puede tener dos dibujos.

const GLIFOS = {
  // ── Objetos del dominio ────────────────────────────────────────────────
  // Disco: dos círculos concéntricos, como el `album` de Material.
  disco: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="2.5"/>',
  // Pantalla con antena: una serie.
  tv: '<rect x="3" y="7.5" width="18" height="12.5" rx="2"/><path d="m8 3.5 4 4 4-4"/>',
  // Rollo de película con sus perforaciones: un fichero de vídeo suelto.
  cinta: '<rect x="3" y="5" width="18" height="14" rx="2"/>'
       + '<path d="M7 5v14M17 5v14M3 12h18"/>',
  // Claqueta: la película como obra, no como fichero.
  claqueta: '<path d="M3.5 9.5h17V19a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 19z"/>'
          + '<path d="m3.8 9.5 1-4.2 15.5 1.5-.5 2.7"/><path d="m9 5.6 1.4 3.6M14.4 6.1l1.4 3.5"/>',
  // Carpeta cerrada y carpeta que se abre.
  carpeta: '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.2l2 2.4H18a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2z"/>',
  abrir: '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.2l2 2.4H18a2 2 0 0 1 2 2v1.3"/>'
       + '<path d="M3.5 10.5h17.2l-1.9 8A2 2 0 0 1 16.9 20H5.5a2 2 0 0 1-2-2z"/>',
  // Libros en fila: la biblioteca.
  biblioteca: '<rect x="4" y="6" width="4" height="13" rx="1"/>'
            + '<rect x="9.8" y="6" width="4" height="13" rx="1"/>'
            + '<path d="m16.2 7.4 3.4 1-2.8 11.2-3.4-1z"/>',
  // Caja: el directorio de salida.
  caja: '<path d="M3.8 8.2 12 4.5l8.2 3.7v7.6L12 19.5 3.8 15.8z"/>'
      + '<path d="M3.8 8.2 12 12l8.2-3.8M12 12v7.5"/>',
  // Flecha entrando en una bandeja: lo descargado, y la copia hacia Output.
  bandeja: '<path d="M4 14.5V18a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3.5"/>'
         + '<path d="M12 3.5v10m0 0 3.5-3.5M12 13.5 8.5 10"/>',
  // Escudo con visto: una validación.
  escudo: '<path d="M12 3.2 5.5 6v6c0 4 2.8 7 6.5 8.8 3.7-1.8 6.5-4.8 6.5-8.8V6z"/>'
        + '<path d="m9.2 12.1 2 2 3.6-4"/>',
  // Algo que entra en un contenedor: inyectar el RPU en la capa.
  inyectar: '<rect x="12.5" y="4.5" width="7" height="15" rx="1.5"/>'
          + '<path d="M3.5 12h6.5m0 0L7 8.8M10 12l-3 3.2"/>',
  // La curva de tone-mapping: es literalmente lo que hace un upgrade a
  // CMv4.0 —metadata de tone-mapping dinámico—, y no «magia».
  //
  // Antes eran dos destellos. A los 15 px de la pestaña se convertían en unos
  // puntitos difusos y decían «efecto mágico», que es justo lo que esta
  // pestaña NO hace: no toca la imagen. Se probaron cuatro alternativas a
  // tamaño real: la curva dentro de un panel se convierte en un cuadradito
  // con una raya, el punto de anclaje sobre la curva se emborrona, y el
  // clásico círculo mitad relleno («contraste») se lee perfecto pero es
  // genérico —parece un conmutador de tema— y su relleno rompe con los otros
  // 43. Los ejes con la curva conservan la forma a 15 px.
  curva: '<path d="M4.5 3.8v15.7h15.7"/>'
       + '<path d="M6.6 17c5.4 0 4.6-11.2 12.6-11.2"/>',
  // Altavoz: una pista de audio. Sin las ondas, que a 14 px se emborronan.
  altavoz: '<path d="M4.5 9.5h3.2L12.5 5.5v13L7.7 14.5H4.5z"/>'
         + '<path d="M16.2 9.6a3.6 3.6 0 0 1 0 4.8"/>',
  // El rectángulo con dos renglones: un subtítulo, como el `subtitles` de
  // Material. Un bocadillo diría «un comentario».
  subtitulos: '<rect x="3.5" y="5.5" width="17" height="13" rx="2"/>'
            + '<path d="M6.8 11.5h4M13.2 11.5h4M6.8 15h7M16 15h1.2"/>',
  // El chevron de desplegar/plegar. Era ▸/▾, que el sistema dibuja con su
  // propio grosor y no casa con el trazo de los demás.
  chevron: '<path d="m9 5.5 7 6.5-7 6.5"/>',
  // La flecha que sale de la caja: el enlace que abre fuera de la app.
  enlaceExterno: '<path d="M13.5 4.5H19.5V10.5"/><path d="M19.5 4.5 11 13"/>'
               + '<path d="M18 14.5v4a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6h4"/>',
  // Un salto por encima: la fase que no hace falta ejecutar.
  omitida: '<path d="M5 7.5c3.5 0 4.5 9 8 9s4.5-9 8-9"/><path d="M18 4.6 21 7.5l-3 2.9"/>',
  // Crear algo. Dos de las «estrellitas» no eran de CMv4.0 sino del botón de
  // crear los proyectos de una serie, y ahí lo que se quiere decir es esto.
  mas: '<circle cx="12" cy="12" r="8.5"/><path d="M12 8.2v7.6M8.2 12h7.6"/>',

  // ── Acciones ───────────────────────────────────────────────────────────
  lapiz: '<path d="M4.5 19.5h3.2L19 8.2a1.7 1.7 0 0 0 0-2.4l-.8-.8a1.7 1.7 0 0 0-2.4 0L4.5 16.3z"/>'
       + '<path d="m14.8 6.6 2.6 2.6"/>',
  lupa: '<circle cx="10.8" cy="10.8" r="6.3"/><path d="m19.5 19.5-4.2-4.2"/>',
  // Lupa sobre una onda: analizar la señal, no «buscar un fichero».
  lupaOnda: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m20 20-4.6-4.6"/>'
          + '<path d="M8 10v1.5M10.5 8v5M13 9.5v2.5"/>',
  papelera: '<path d="M4.5 7h15M9.5 7V5.2a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V7"/>'
          + '<path d="M6.5 7v11.3a1.7 1.7 0 0 0 1.7 1.7h7.6a1.7 1.7 0 0 0 1.7-1.7V7"/>'
          + '<path d="M10.5 11v5.5M13.5 11v5.5"/>',
  ajustes: '<path d="M4 8.5h9M17 8.5h3M4 15.5h3M11 15.5h9"/>'
         + '<circle cx="15" cy="8.5" r="2.2"/><circle cx="7" cy="15.5" r="2.2"/>',
  libro: '<path d="M4 5.2A1.7 1.7 0 0 1 5.7 3.5H11v17H5.7A1.7 1.7 0 0 1 4 18.8z"/>'
       + '<path d="M20 5.2a1.7 1.7 0 0 0-1.7-1.7H13v17h5.3A1.7 1.7 0 0 0 20 18.8z"/>',
  campana: '<path d="M7 10a5 5 0 0 1 10 0c0 4 1.3 5.5 1.8 6H5.2C5.7 15.5 7 14 7 10z"/>'
         + '<path d="M10.2 19.2a2 2 0 0 0 3.6 0"/>',
  etiqueta: '<path d="M11.3 3.8H19a1.2 1.2 0 0 1 1.2 1.2v7.7a1 1 0 0 1-.3.7l-7.6 7.6a1 1 0 0 1-1.4 0'
          + 'l-6.4-6.4a1 1 0 0 1 0-1.4l6.9-6.9a1 1 0 0 1 .9-.5z"/>'
          + '<circle cx="15.8" cy="8.2" r="1.3"/>',
  grafico: '<path d="M4.5 19.5h15"/><path d="M7.5 16.5v-5M12 16.5v-9M16.5 16.5v-6.5"/>',
  diana: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/>'
       + '<circle cx="12" cy="12" r="1"/>',
  portapapeles: '<path d="M9 4.5H7.5a1.5 1.5 0 0 0-1.5 1.5v13A1.5 1.5 0 0 0 7.5 20.5h9a1.5 1.5 0 0 0 1.5-1.5V6a1.5 1.5 0 0 0-1.5-1.5H15"/>'
              + '<rect x="9" y="3" width="6" height="3.2" rx="1"/>',
  tijeras: '<circle cx="6.5" cy="17.5" r="2.3"/><circle cx="6.5" cy="6.5" r="2.3"/>'
         + '<path d="M8.4 8.1 19 18.5M19 5.5 8.4 15.9"/>',
  rayo: '<path d="M13.2 3.5 6 13.2h4.4L9.8 20.5 17 10.8h-4.4z"/>',
  refrescar: '<path d="M19.5 12a7.5 7.5 0 1 1-2.4-5.5"/><path d="M19.8 4.5v3.8h-3.8"/>',
  deshacer: '<path d="M4.5 12a7.5 7.5 0 1 0 2.4-5.5"/><path d="M4.2 4.5v3.8H8"/>',
  candado: '<rect x="4.8" y="10.8" width="14.4" height="9.2" rx="2"/>'
         + '<path d="M8.6 10.8V7.9a3.4 3.4 0 0 1 6.8 0v2.9"/>',
  candadoAbierto: '<rect x="4.8" y="10.8" width="14.4" height="9.2" rx="2"/>'
                + '<path d="M8.6 10.8V7.4a3.4 3.4 0 0 1 6.8 0"/>'
                + '<path d="M15.4 7.4V4.6"/>',
  bombilla: '<path d="M9.2 17.5a6 6 0 1 1 5.6 0z"/><path d="M9.8 20.5h4.4"/>',
  ojo: '<path d="M2.8 12S6 6.5 12 6.5 21.2 12 21.2 12 18 17.5 12 17.5 2.8 12 2.8 12z"/>'
     + '<circle cx="12" cy="12" r="2.8"/>',
  archivador: '<rect x="3.5" y="4.5" width="17" height="4" rx="1"/>'
            + '<path d="M5.5 8.5h13V18a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2z"/>'
            + '<path d="M10.5 12h3"/>',

  // ── Señales ────────────────────────────────────────────────────────────
  // Sin círculo: la marca a secas, para un texto. El estado «hecho» lleva
  // el suyo dentro de un círculo — son dos cosas distintas.
  check: '<path d="m5 12.8 4.2 4.2L19 6.5"/>',
  cruz: '<path d="M6.2 6.2l11.6 11.6M17.8 6.2 6.2 17.8"/>',
  aviso: '<path d="M12 4.2 21 19.5H3z"/><path d="M12 9.8v4.4"/><path d="M12 17h.01"/>',
  reloj: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 1.8"/>',
  pausa: '<path d="M9.5 6.5v11M14.5 6.5v11"/>',
  play: '<path d="M8.5 5.8 18 12l-9.5 6.2z"/>',
  // El paso que aún no ha empezado: un hueco, no un cuadrado relleno.
  pendiente: '<circle cx="12" cy="12" r="7.5" stroke-dasharray="3 3.2"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.2"/><path d="M12 7.9h.01"/>',
  flechaAbajo: '<path d="M12 4.8v14.4M12 19.2 6.5 13.7M12 19.2l5.5-5.5"/>',
  flechaArriba: '<path d="M12 19.2V4.8M12 4.8 6.5 10.3M12 4.8l5.5 5.5"/>',
  subirNivel: '<path d="M12 19.5V7.2M12 7.2 7 12.2M12 7.2l5 5"/><path d="M5.5 4.5h13"/>',
};

/** Un icono suelto, del tamaño del texto que lo rodea.
 *
 *  Hereda el color con `currentColor`, así que dentro de un botón primario
 *  sale blanco y en un título, del color del título. Sin chip: los chips
 *  (`_chipIcono`) son para cuando el icono ES el contenido, como en la
 *  columna de trabajo.
 */
function icono(nombre, clase = '') {
  const g = GLIFOS[nombre];
  if (!g) return '';
  return _svg(g, ` class="ico ${clase}"`);
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
  // Salen del catálogo: el objeto es el mismo y no puede tener dos dibujos.
  rip:                _svg(GLIFOS.disco),
  crear_serie:        _svg(GLIFOS.tv),
  analisis_extendido: _svg(GLIFOS.lupaOnda),
  copia_biblioteca:   _svg(GLIFOS.bandeja),
  preflight:          _svg(GLIFOS.escudo),
  fase_cmv40:         _svg(GLIFOS.curva),
};

/** Por ESTADO: dice en qué punto está. */
const _ICONOS_ESTADO = {
  // Arco abierto que gira. Sustituye al ⏳: un reloj de arena sugiere que hay
  // que esperar sin hacer nada, y esto sugiere que algo se mueve.
  corriendo: ['verde', _svg('<circle cx="12" cy="12" r="8.5" stroke-dasharray="40 14"/>',
                            ' class="icono-girando"')],
  // Reloj, no reloj de arena: es "le toca a las y cuarto", no "aguanta".
  en_cola: ['gris', _svg(GLIFOS.reloj)],
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
  // Los dos que pedían las columnas de proyecto. Van en el MISMO catálogo:
  // un estado que se pinta en dos sitios no puede tener dos dibujos.
  //
  // Configurado y sin ejecutar. El triángulo no invita a pulsar —los chips no
  // son botones y el de abrir está abajo—, dice que está todo listo y falta
  // arrancar, que es justo el estado de un proyecto de Tab 1 recién creado.
  listo: ['gris', _svg('<circle cx="12" cy="12" r="8.5"/>'
                     + '<path d="M10.4 9.3l4.4 2.7-4.4 2.7z"/>')],
  // Caja cerrada: el proyecto existe y se puede consultar, pero ya no se
  // trabaja sobre él. En gris porque no pide nada.
  archivado: ['gris', _svg(GLIFOS.archivador)],
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

/** Rellena los iconos del marcado estático de `index.html`.
 *
 *  El HTML no puede llamar a `icono()`, y pegar 43 SVG a mano ahí dejaría los
 *  dibujos en dos sitios. Se declara `data-icono="disco"` y esto los pinta al
 *  arrancar: el marcado queda legible y el catálogo sigue siendo el único
 *  lugar donde vive cada forma. El HTML que genera el JS usa `icono()`
 *  directamente.
 */
function pintarIconos(raiz = document) {
  const uno = el => {
    if (el.dataset.icoPuesto) return;
    const svg = icono(el.dataset.icono, el.dataset.iconoClase || '');
    if (!svg) return;
    el.innerHTML = svg;
    // El atributo se conserva —dice qué icono es, y eso se lee al depurar— y
    // la marca evita repintarlo en cada vuelta del observador.
    el.dataset.icoPuesto = '1';
  };
  if (raiz.nodeType === 1 && raiz.dataset && raiz.dataset.icono) uno(raiz);
  raiz.querySelectorAll('[data-icono]:not([data-ico-puesto])').forEach(uno);
}

/** Y lo mismo para el HTML que genera el JS.
 *
 *  La alternativa era interpolar `${icono('abrir')}` en cada plantilla, y hay
 *  más de cien: en las que NO son template literals el `${...}` se vería
 *  crudo en pantalla, y el fallo no da ningún error. Con `data-icono` la
 *  plantilla es la misma cadena en los dos casos y esto lo pinta en cuanto
 *  entra en el documento.
 *
 *  El trabajo por mutación está acotado —un `querySelectorAll` sobre el nodo
 *  que acaba de añadirse— y no se puede realimentar: pintar un icono marca su
 *  `data-ico-puesto`, así que la mutación que provoca no produce trabajo.
 */
function _observarIconos() {
  if (!window.MutationObserver || !document.body) return;
  new MutationObserver(muts => {
    for (const m of muts) {
      for (const n of m.addedNodes) {
        if (n.nodeType === 1) pintarIconos(n);
      }
    }
  }).observe(document.body, { childList: true, subtree: true });
}


/** El estado de un paso en un modal de progreso.
 *
 *  Era un PREFIJO DE TEXTO que se cambiaba con expresiones regulares
 *  (un `textContent.replace` con la regex de los tres prefijos), lo que ataba
 *  el estado del paso a su redacción: escribir el texto de otra forma —o
 *  traducirlo— dejaba
 *  el paso sin icono, y no había manera de darle color. Ahora el icono vive en
 *  su propio `<span>` y esto le cambia el contenido y la clase.
 *
 *  `estado`: `pendiente` · `curso` · `hecho`.
 */
const _PASO_GLIFO = { pendiente: 'pendiente', curso: 'reloj', hecho: 'check' };

function marcarPasoDeModal(el, estado) {
  if (!el) return;
  // El icono puede estar en el propio nodo o en su label (el paso de los PGS
  // lleva además barra y estadísticas).
  const ico = el.querySelector('.paso-ico')
           || (el.parentElement && el.parentElement.querySelector('.paso-ico'));
  if (ico) {
    ico.className = `paso-ico paso-${estado}`;
    ico.innerHTML = icono(_PASO_GLIFO[estado] || 'pendiente');
  }
  const caja = el.closest('.analyze-step') || el;
  if (caja && caja.style) caja.style.opacity = estado === 'pendiente' ? '.4' : '1';
}
