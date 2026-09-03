'use strict';
/**
 * tab1.js — Tab 1: Blu-Ray ISO → MKV.
 *
 * El modal de nuevo proyecto con sus tres tipos de origen, el modo serie, el
 * sidebar de sesiones, el render de la sesión (pistas incluidas y descartadas,
 * capítulos, tarjetas de DV/HDR, nombre del MKV), guardar/ejecutar, el
 * WebSocket del pipeline con su consola y el panel de la cola.
 */

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

/** Lanza el análisis (Fase A+B) de un origen (ISO / carpeta BDMV / m2ts)
 *  enviando su source_type al endpoint /api/analyze. Configura el modal de
 *  progreso según el tipo y, si hay match TMDb, hidrata la cartela. */
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
            line += ` · Restante ${em}:${es}`;
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
/** Lookup TMDb best-effort para un modal de análisis con cabecera tipo poster
 *  (poster + título + sub). Si hay match, sustituye el icono por la cartela y
 *  el título genérico por el nombre + año de la película. Se ejecuta en
 *  paralelo al análisis: si tarda, falla o el usuario cierra el modal, no pasa
 *  nada. Compartida por Tab 1 (analyze-modal) y Tab 2 (mkv-analyze-modal) para
 *  que las cabeceras sean equivalentes en todos los flujos. */
async function _hydrateModalWithTmdb({ name, modalId, posterId, titleId, subId, subText }) {
  if (!name) return;
  try {
    const data = await apiFetch('/api/cmv40/tmdb-lookup', {
      method: 'POST',
      body: JSON.stringify({ source_mkv_name: name }),
      silent: true,
    }, 10000);
    if (!data || !data.details) return;
    const t = data.details;
    // El usuario pudo cerrar el modal (cancelar / error). Si ya no está
    // abierto, descartamos la hidratación silenciosamente.
    const modal = document.getElementById(modalId);
    if (!modal || !modal.classList.contains('open')) return;

    const posterEl = document.getElementById(posterId);
    if (posterEl && t.poster_url) {
      posterEl.innerHTML = `<img src="${escHtml(t.poster_url)}" alt="${escHtml(t.title || '')}" loading="lazy">`;
    }
    const titleEl = document.getElementById(titleId);
    if (titleEl && t.title) {
      const yr = t.year ? ` (${t.year})` : '';
      titleEl.textContent = `${t.title}${yr}`;
    }
    // Sub: contexto (acción + nombre del fichero) para no perderlo al
    // sustituir el título por el match TMDb.
    const subEl = document.getElementById(subId);
    if (subEl && subText) subEl.textContent = subText;
  } catch (_) { /* TMDb no configurado / sin red / etc. — silencioso */ }
}

/** Hidratación TMDb del analyze-modal de Tab 1 (delega en la genérica). */
async function _hydrateAnalyzeModalTmdb(sourceName, sourceType) {
  const action = sourceType === 'iso' ? 'Analizando disco'
    : sourceType === 'bdmv_folder' ? 'Analizando carpeta BDMV'
    : 'Analizando fichero M2TS';
  return _hydrateModalWithTmdb({
    name: sourceName,
    modalId: 'analyze-modal',
    posterId: 'analyze-modal-poster',
    titleId: 'analyze-modal-title',
    subId: 'analyze-modal-iso',
    subText: sourceName ? `${action} · ${sourceName}` : '',
  });
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

  // Tarjetas informativas: estado Dolby Vision del disco + resumen vídeo/HDR
  _renderDvStatusCard(session);
  _renderVideoHdrCard(session);
  const dcpChip = E('mkv-dcp-chip');
  if (dcpChip) dcpChip.style.display = session.audio_dcp ? '' : 'none';
  _renderTamanoEstimado(session);

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

  // Botón restaurar: visible si la fuente tiene MPLS (iso/bdmv). Se ofrece
  // tanto si el usuario editó capítulos del disco como si los actuales son
  // auto-generados — el disco puede tener capítulos reales que no se pudieron
  // extraer antes (discos UHD multi-segmento que hacían fallar a mkvmerge).
  // Oculto para m2ts (sin MPLS → no hay capítulos que restaurar).
  const _resetProject = openProjects.find(p => p.subTabId === activeSubTabId);
  const _resetHasMpls = (_resetProject?.session?.source_type || 'iso') !== 'm2ts';
  const modified = _chaptersModified.get(activeSubTabId) || false;
  if (resetBtn) resetBtn.style.display = (_resetHasMpls && (autoGenerated || modified)) ? '' : 'none';

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
  if (resetBtn && (project?.session?.source_type || 'iso') !== 'm2ts') {
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
  const auto = currentSession.chapters_auto_generated;

  showConfirm(
    auto ? '¿Extraer capítulos reales del disco?' : '¿Restaurar capítulos del disco?',
    auto
      ? 'Se extraerán los capítulos originales del disco (MPLS) y reemplazarán a los automáticos cada 10 minutos. Algunos discos UHD multi-segmento solo se pueden leer así.'
      : 'Se descartarán todas las ediciones manuales (nombres, posiciones, capítulos añadidos/eliminados) y se volverán a extraer los capítulos originales del ISO.',
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
//  TARJETAS INFORMATIVAS DEL DISCO (Dolby Vision / Vídeo·HDR)
//
//  Ambas son de SOLO LECTURA: describen lo que el análisis de Fase A
//  encontró en el disco. El estado FEL/MEL no es editable — antes había
//  un toggle que solo cambiaba el tag del nombre, lo que permitía
//  renombrar un disco MEL como FEL sin que el contenido cambiase.
//  Si la detección falla, el escape sigue siendo editar el nombre del MKV.
// ═══════════════════════════════════════════════════════════════════

/**
 * Clasifica el estado Dolby Vision del disco a partir del análisis.
 *
 * La fuente fiable es dovi_tool (`dovi` en la pista BL): da profile y
 * el_type definitivos. Es un paso OPCIONAL de Fase A — si falla, solo
 * queda la heurística estructural de `_detect_fel`, que asume FEL en
 * cuanto ve una Enhancement Layer. Ese caso se marca como "sin
 * confirmar" en vez de afirmar FEL.
 *
 * @returns {{label: string, icon: string, cls: string, detail: string,
 *            note: string, unconfirmed: boolean}}
 */
function _classifyDvStatus(session) {
  const bd       = session.bdinfo_result || {};
  const tracks   = bd.video_tracks || [];
  const mainVid  = tracks.find(t => !t.is_el) || null;
  const dv       = mainVid?.dovi || null;
  const hasEl    = tracks.some(t => t.is_el);

  // Detalle técnico del RPU (lo que antes vivía en #dovi-detail).
  // El master display va en la tarjeta de Vídeo·HDR — aquí sería duplicado.
  let detail = '';
  if (dv) {
    const parts = [`Perfil ${dv.profile}`];
    if (dv.el_type) parts.push(dv.el_type);
    if (dv.cm_version) parts.push(`CM ${dv.cm_version}`);
    const lvls = [];
    if (dv.has_l1) lvls.push('L1');
    if (dv.has_l2) lvls.push('L2');
    if (dv.has_l5) lvls.push('L5');
    if (dv.has_l6) lvls.push('L6');
    if (dv.has_l8) lvls.push('L8');
    if (lvls.length) parts.push(lvls.join(' '));
    if (dv.scene_count) parts.push(`${dv.scene_count.toLocaleString('es-ES')} escenas`);
    detail = parts.join(' · ');
  }

  if (dv && dv.profile === 7 && dv.el_type === 'FEL') {
    return { label: 'Dolby Vision FEL', icon: '🎬', cls: 'dv-fel', detail,
             note: '', unconfirmed: false };
  }
  if (dv && dv.profile === 7 && dv.el_type === 'MEL') {
    return { label: 'Dolby Vision MEL', icon: '🎬', cls: 'dv-mel', detail,
             note: 'Capa de mejora mínima — sin residuals de color. El MKV no lleva tag [DV FEL].',
             unconfirmed: false };
  }
  if (dv) {
    return { label: `Dolby Vision (Perfil ${dv.profile})`, icon: '🎬', cls: 'dv-other',
             detail, note: '', unconfirmed: false };
  }
  if (hasEl || session.has_fel) {
    // Del reason solo la primera frase (el "cómo se detectó"); el resto
    // ya lo cuenta la nota. El texto íntegro sigue en 🔬 Datos ISO.
    const how = (bd.fel_reason || '').split('. ')[0];
    return {
      label: 'Dolby Vision dual-layer', icon: '🎬', cls: 'dv-unconfirmed',
      detail: how || 'Enhancement Layer presente en el disco',
      note: 'dovi_tool no pudo confirmar si la capa es FEL o MEL — se asume FEL (mira 🔬 Datos ISO).',
      unconfirmed: true,
    };
  }
  // Sin Dolby Vision: describir el HDR que sí trae el disco.
  const hdrFmt = mainVid?.hdr?.hdr_format || '';
  return {
    label: 'Sin Dolby Vision', icon: '📼', cls: 'dv-none',
    detail: hdrFmt ? `El disco es ${hdrFmt} sin capa Dolby Vision` : (bd.fel_reason || ''),
    note: '', unconfirmed: false,
  };
}

/**
 * Chip con el tamaño estimado del MKV final.
 *
 * Lo calcula el backend (`estimate_output_size_bytes`, en phase_b) y llega en
 * `estimated_size_bytes` — CALCULADO al servir la sesión, no persistido,
 * porque cambia en cuanto el usuario toca la selección de pistas.
 *
 * Si vale `null` el chip se OCULTA en vez de enseñar un "?" o un cero: la
 * función devuelve null justamente cuando el dato de origen no es fiable (el
 * caso real son dos episodios de Juego de Tronos en los que MediaInfo midió
 * otro m2ts y la cuenta salía al doble). Preferimos un hueco a una cifra
 * inventada con pinta de dato.
 */
function _renderTamanoEstimado(session) {
  const chip = E('mkv-size-chip');
  if (!chip) return;
  const bytes = session.estimated_size_bytes;
  if (!bytes || bytes <= 0) { chip.style.display = 'none'; return; }
  const gb = bytes / 1e9;
  const texto = gb >= 10 ? gb.toFixed(0) : gb.toFixed(1);
  chip.textContent = `💾 Ocupará ~${texto} GB`;
  chip.style.display = '';
}

/** Pinta la tarjeta de estado Dolby Vision (solo lectura). */
function _renderDvStatusCard(session) {
  const st = _classifyDvStatus(session);
  const card = E('dv-card');
  if (card) card.className = `global-info-item ${st.cls}`;
  setText('dv-icon', st.icon);
  setText('dv-state', st.label);
  setText('dv-detail', st.detail);

  const note = E('dv-note');
  if (note) {
    note.textContent = st.note;
    note.style.display = st.note ? '' : 'none';
  }

  // Chip con el tag literal que se añadirá al nombre del MKV.
  const tag = E('dv-tag');
  if (tag) {
    if (session.has_fel) {
      tag.textContent = st.unconfirmed ? '[DV FEL] · sin confirmar' : '[DV FEL]';
      tag.style.display = '';
    } else {
      tag.style.display = 'none';
    }
  }
}

/**
 * Pinta el resumen de vídeo/HDR del disco: codec, bitrate real, HDR10 y
 * espacio de color. Los datos vienen de MediaInfo sobre el m2ts principal
 * (paso opcional de Fase A) — sin él solo hay codec y resolución.
 */
function _renderVideoHdrCard(session) {
  const mainVid = (session.bdinfo_result?.video_tracks || []).find(t => !t.is_el) || null;
  const hdr     = mainVid?.hdr || null;

  // "MPEG-H HEVC Video" → "HEVC" (el literal estilo BDInfo es ruido aquí)
  const codec = (mainVid?.codec || '').replace(/^MPEG-H\s+/i, '').replace(/\s+Video$/i, '');
  const codecParts = [codec || '—'];
  if (mainVid?.description) codecParts.push(mainVid.description);
  if (mainVid?.bitrate_kbps) codecParts.push(`${(mainVid.bitrate_kbps / 1000).toFixed(1)} Mbps`);
  if (hdr?.bit_depth) codecParts.push(`${hdr.bit_depth} bit`);
  setText('vhdr-codec', codecParts.join(' · '));

  const hdrParts = [];
  if (hdr?.hdr_format) hdrParts.push(hdr.hdr_format);
  if (hdr?.max_cll)  hdrParts.push(`MaxCLL ${hdr.max_cll.toLocaleString('es-ES')} nits`);
  if (hdr?.max_fall) hdrParts.push(`MaxFALL ${hdr.max_fall.toLocaleString('es-ES')} nits`);
  setText('vhdr-hdr', hdrParts.join(' · '));

  const colorParts = [];
  if (hdr?.color_primaries) colorParts.push(hdr.color_primaries);
  if (hdr?.transfer_characteristics) colorParts.push(hdr.transfer_characteristics);
  const master = _formatMasteringLuminance(hdr?.mastering_display_luminance);
  if (master) colorParts.push(master);
  setText('vhdr-color', colorParts.join(' · ') || (hdr ? '' : 'Sin datos de MediaInfo'));
}

/**
 * "min: 0.0001 cd/m2, max: 1000 cd/m2" → "Master 1000 nits".
 * MediaInfo devuelve la luminancia del display de masterizado en crudo;
 * el pico es lo único accionable. El valor completo sigue en 🔬 Datos ISO.
 */
function _formatMasteringLuminance(raw) {
  if (!raw) return '';
  const m = String(raw).match(/max\s*:\s*([\d.]+)/i);
  if (!m) return String(raw);
  const nits = Math.round(parseFloat(m[1]));
  return Number.isFinite(nits) ? `Master ${nits.toLocaleString('es-ES')} nits` : String(raw);
}


// ═══════════════════════════════════════════════════════════════════
//  NOMBRE DEL MKV
// ═══════════════════════════════════════════════════════════════════

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

// NOTA: la construcción del nombre del MKV vive SOLO en el backend
// (phase_b._build_mkv_name + endpoint /recalculate-name). La réplica local
// que había aquí existía para reaccionar a los toggles FEL/DCP; al
// eliminarlos desaparece también esa duplicación de reglas de naming.

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
  queueWs.onclose = (ev) => {
    // Cierre intencional (p. ej. recovery tras Mac sleep) → NO reconectar
    // aquí: quien cerró ya llamará a connectQueueWebSocket(). Sin este guard
    // se abrían DOS conexiones (la del recovery + la del setTimeout). audit #25.
    if (ev && ev.target && ev.target._closedByUser) return;
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
        if (etaEl) etaEl.textContent = `Restante ${fmtSecs(remaining)}`;
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
        const session = _sessionsCache.find(s => s.id === sid);
        // Mismo criterio que el job en curso: manda el mkv_name (nombre
        // formateado de la peli); proj.name solo si la pestaña está abierta;
        // sid como último recurso. Antes solo miraba proj.name, así que los
        // jobs encolados sin pestaña abierta mostraban el id técnico.
        const name = (session?.mkv_name || proj?.name || sid).replace(/\.mkv$/i, '');
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
