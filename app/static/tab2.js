'use strict';
/**
 * tab2.js — Tab 2: Consultar / Editar MKV.
 *
 * Abrir un MKV, editar nombres de pista, flags y capítulos, y la radiografía
 * DV+HDR con el perfil de luminancia y la cadena de mastering.
 */

// ═══════════════════════════════════════════════════════════════════
//  TAB 2 — EDITAR MKV
// ═══════════════════════════════════════════════════════════════════

/**
 * Proyectos MKV abiertos en Tab 2, uno por sub-pestaña. Tab 1 y Tab 3 ya
 * tenían sub-pestañas desde hace tiempo; esto iguala Tab 2.
 *
 * Cada entrada es `{id, fileName, filePath, analysis, originalAnalysis,
 * dirty}`. El `id` es un token corto generado aquí y se usa como
 * SUFIJO de los ids del DOM del panel (`mkv-audio-list-m1`), igual que hace
 * Tab 3 — así dos paneles abiertos a la vez nunca comparten un id.
 */
const openMkvProjects = [];

/** id del proyecto cuya pestaña está visible, o null si no hay ninguna. */
let activeMkvProjectId = null;

/** Tope de pestañas abiertas, el mismo que Tab 3. */
const MAX_MKV_PROJECTS = 5;

/** Contador para generar ids de proyecto. */
let _mkvProjectSeq = 0;

/**
 * Compat shim: `mkvProject` sigue significando "el MKV abierto" y devuelve el
 * proyecto ACTIVO. Es un getter y no una variable espejo justamente para que
 * no pueda desincronizarse. Lo lee `showRawMkvData` desde `tab1.js`, además de
 * casi todo este fichero.
 *
 * No tiene setter a propósito: una asignación (`mkvProject = …`) lanza en modo
 * estricto en vez de crear un segundo estado en la sombra.
 */
Object.defineProperty(window, 'mkvProject', {
  configurable: true,
  get() {
    return openMkvProjects.find(p => p.id === activeMkvProjectId) || null;
  },
});

/**
 * Resuelve un id de dentro del panel de un proyecto: los ids del panel llevan
 * el id del proyecto como sufijo.
 *
 * NO se usa el helper `E()` de `core.js`: ese resuelve contra `activeSubTabId`,
 * que es el sub-tab de TAB 1, no el de esta pestaña.
 */
function _mkvEl(id, pid) {
  const p = pid || activeMkvProjectId;
  return p ? document.getElementById(`${id}-${p}`) : null;
}

/** La ruta con la que se identifica un proyecto ya abierto. */
function _mkvRutaDe(p) {
  return p.filePath || p.analysis?.file_path || p.fileName;
}

/** Marca un proyecto como modificado y enciende el punto de su pestaña. */
function _mkvMarkDirty(project = mkvProject) {
  if (!project) return;
  project.dirty = true;
  const dot = document.getElementById(`mkv-unsaved-dot-${project.id}`);
  if (dot) dot.style.display = 'inline';
}

/** Apaga el punto de cambios sin guardar de un proyecto. */
function _mkvClearDirty(project) {
  if (!project) return;
  project.dirty = false;
  const dot = document.getElementById(`mkv-unsaved-dot-${project.id}`);
  if (dot) dot.style.display = 'none';
}

let _mkvPickerSelected = null;

// ── MKV Picker — usa el file browser con roots Library + Output ───
// El antiguo modal #mkv-picker-modal con <select> queda como fallback
// pero ya no se invoca desde la UI. El flujo nuevo es:
//   1. openMkvPickerModal() → openFileBrowser con roots [biblioteca, output]
//   2. Al seleccionar MKV, _doAnalyzeMkvFromPickerPath(absPath, name) lanza
//      /api/mkv/analyze con la ruta absoluta. El backend valida que la
//      ruta cae bajo un root permitido.

async function openMkvPickerModal() {
  // Con sub-pestañas, abrir otro MKV ya no descarta el actual: va a una
  // pestaña nueva. El tope se comprueba AQUÍ y no al terminar el análisis
  // porque ese análisis son 1-3 min y sería cruel avisar después.
  if (openMkvProjects.length >= MAX_MKV_PROJECTS) {
    showToast(tr('tab2.maximo_mkv_abiertos_cierra_alguno_antes', {max_mkv_projects: MAX_MKV_PROJECTS}), 'warning');
    return;
  }
  _openMkvBrowserNow();
}

function _openMkvBrowserNow() {
  openFileBrowser({
    title: tr('tab2.abrir_mkv_para_inspeccionar_editar'),
    subtitle: tr('tab2.selecciona_el_mkv_en_la_biblioteca'),
    roots: ROOTS_MKV,
    onSelect: async (absPath, name) => _mkvAbrirRuta(absPath, name),
  });
}

/**
 * Abre un MKV por su ruta: modal de análisis + petición en background.
 *
 * Lo llaman las DOS puertas de entrada — el file browser y las tarjetas de la
 * columna izquierda—, y por eso está aquí fuera: era el cuerpo del `onSelect`
 * del browser, y duplicarlo habría dejado dos sitios donde recordar el modal,
 * la cartela de TMDb y el `catch`.
 */
function _mkvAbrirRuta(absPath, name) {
  // 1) Sync: setup + abrir modal analisis (queda BAJO el browser por z-index).
  const fileEl = document.getElementById('mkv-analyze-modal-file');
  if (fileEl) fileEl.textContent = name;
  _resetMkvAnalyzeSteps();
  openModal('mkv-analyze-modal');
  // Cartela + título TMDb en la cabecera (best-effort, en paralelo) — misma
  // ficha que el modal de análisis de Tab 1, para consistencia entre flujos.
  _hydrateModalWithTmdb({
    name,
    modalId: 'mkv-analyze-modal',
    posterId: 'mkv-analyze-modal-poster',
    titleId: 'mkv-analyze-modal-title',
    subId: 'mkv-analyze-modal-file',
    subText: name,
  });
  // 2) Async (NO await): el fetch de analisis tarda 1-3 min. Lo lanzamos en
  //    background para que onSelect resuelva inmediatamente y _fileBrowserSelect
  //    cierre el browser → quedando solo el modal de analisis visible.
  _doAnalyzeMkvFromPickerPath(absPath, name).catch(e => {
    console.error('analyze MKV error:', e);
    showToast(tr('tab2.error_en_analisis', {e: e.message || e}), 'error');
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
        // mismo guard que en Tab 1's _doAnalyzeSource).
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
        if (labelEl) {
          // Solo el texto: reescribir el `textContent` entero se llevaba por
          // delante el `<span>` del icono del paso.
          const ico = labelEl.querySelector('.paso-ico');
          labelEl.textContent = ' ' + tr('tab2.analizando_subtitulos_del_origen');
          if (ico) labelEl.prepend(ico);
        }
        if (barWrap) barWrap.style.display = 'block';
        if (statsEl) statsEl.style.display = 'block';
        if (pct != null && barFill) {
          barFill.style.width = pct + '%';
        }
        if (statsEl) {
          let line = tr('ui.transcurridos_mm_ss', { mm, ss });
          if (pct != null) line += tr('ui.pct_leido', { pct: pct.toFixed(1) });
          if (eta && eta > 0) {
            const em = Math.floor(eta / 60);
            const es = (eta % 60).toString().padStart(2, '0');
            line += ' · ' + tr('comun.restante_p1', {p1: `${em}:${es}`});
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
  }, API_FETCH_TIMEOUT_LARGO);   // el conteo PGS puede tardar 1-3 min

  clearInterval(pollId);
  // Marcar todos los pasos restantes como completados
  steps.forEach((s, i) => {
    if (i < steps.length - 1) _advanceMkvAnalyzeStep(s, steps[i + 1]);
  });
  await new Promise(r => setTimeout(r, 300));
  closeModal('mkv-analyze-modal');

  if (!data) {
    showToast(tr('tab2.error_al_analizar_el_mkv'), 'error');
    return;
  }

  openMkvProject(data);
  // El análisis acaba de escribir (o refrescar) su entrada en la caché, que es
  // de donde sale la columna izquierda: sin esto, el MKV recién abierto no
  // aparece en la lista hasta cambiar de pestaña y volver.
  refrescarMkvRecientes();
}

/** Resetea los pasos del modal de análisis de MKV. */
function _resetMkvAnalyzeSteps() {
  // Restaurar la cabecera al estado base: una apertura previa con match TMDb
  // pudo sustituir el poster por <img> y el título por el nombre de la peli.
  const posterEl = document.getElementById('mkv-analyze-modal-poster');
  if (posterEl) posterEl.innerHTML = '<span id="mkv-analyze-modal-icon"><span data-icono="lapiz"></span></span>';
  const titleEl = document.getElementById('mkv-analyze-modal-title');
  if (titleEl) titleEl.textContent = tr('ui.analizando_mkv');

  const steps = ['identify', 'mediainfo', 'pgs', 'dovi'];
  steps.forEach((s, i) => {
    const container = document.getElementById(`mkv-analyze-step-${s}`);
    if (container) container.style.opacity = i === 0 ? '1' : '.4';
    const labelEl = s === 'pgs'
      ? document.getElementById('mkv-analyze-step-pgs-label')
      : container;
    marcarPasoDeModal(labelEl, i === 0 ? 'curso' : 'pendiente');
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
  marcarPasoDeModal(fromLabel, 'hecho');
  const fromContainer = document.getElementById(`mkv-analyze-step-${fromStep}`);
  if (fromContainer) fromContainer.style.opacity = '1';

  if (nextStep) {
    const nextContainer = document.getElementById(`mkv-analyze-step-${nextStep}`);
    if (nextContainer) nextContainer.style.opacity = '1';
    const nextLabel = nextStep === 'pgs'
      ? document.getElementById('mkv-analyze-step-pgs-label')
      : nextContainer;
    marcarPasoDeModal(nextLabel, 'curso');
  }
}

// ── Proyecto MKV ─────────────────────────────────────────────────

function openMkvProject(analysis) {
  // El perfil de luminancia llega dentro del análisis cuando está cacheado:
  // sale del mismo análisis extendido que los campos quality_*, así que al
  // reabrir el MKV el gráfico aparece poblado sin volver a analizar nada.
  _mkvAplicarPerfilLuminancia(analysis && analysis.dovi);

  const ruta = analysis.file_path || analysis.file_name;
  const existente = openMkvProjects.find(p => _mkvRutaDe(p) === ruta);
  if (existente) {
    // Re-análisis, o reapertura del mismo fichero desde el browser: refresca
    // su pestaña en vez de duplicarla.
    existente.fileName = analysis.file_name;
    existente.analysis = analysis;
    existente.originalAnalysis = structuredClone(analysis);
    _mkvClearDirty(existente);
    _mkvRefreshSubTab(existente);
    switchMkvSubTab(existente.id);
    _renderMkvEditPanel(existente);
    showToast(tr('tab2.mkv_actualizado_p1', {p1: analysis.file_name}), 'success');
    return existente;
  }

  if (openMkvProjects.length >= MAX_MKV_PROJECTS) {
    showToast(tr('tab2.maximo_mkv_abiertos_cierra_alguno_antes', {max_mkv_projects: MAX_MKV_PROJECTS}), 'warning');
    return null;
  }

  const project = {
    id: `m${++_mkvProjectSeq}`,
    fileName: analysis.file_name,
    filePath: analysis.file_path,
    analysis: analysis,
    originalAnalysis: structuredClone(analysis),
    dirty: false,
  };
  openMkvProjects.push(project);
  _mkvCreateSubTab(project);
  _mkvCreatePanel(project);
  switchMkvSubTab(project.id);
  _renderMkvEditPanel(project);
  showToast(tr('tab2.mkv_abierto_p1', {p1: analysis.file_name}), 'success');
  return project;
}

// ── Sub-pestañas de Tab 2 ────────────────────────────────────────
// Mismo markup y mismas clases que Tab 1/3 (`.subtab-proj`, `.subtab-projects`)
// para que el look sea idéntico sin tocar el CSS.

/** El HTML interior del botón de sub-pestaña. */
function _mkvSubTabInnerHtml(project) {
  const nombre = (project.fileName || '').replace(/\.mkv$/i, '');
  const corto = nombre.slice(0, 24) + (nombre.length > 24 ? '…' : '');
  return `
    <span class="unsaved-dot" id="mkv-unsaved-dot-${project.id}"
      style="display:${project.dirty ? 'inline' : 'none'}" data-i18n-tip="tab2.cambios_sin_guardar_2"></span>
    <span class="subtab-proj-icon"><span data-icono="lapiz"></span></span>
    <span class="subtab-proj-name" data-tooltip="${escHtml(project.fileName || '')}">${escHtml(corto)}</span>
    <button class="subtab-proj-close" onclick="closeMkvProject('${project.id}');event.stopPropagation()" data-i18n-tip="tab2.cerrar_este_mkv">×</button>`;
}

function _mkvCreateSubTab(project) {
  const container = document.getElementById('mkv-subtab-projects');
  if (!container) return;
  const btn = document.createElement('button');
  btn.className = 'subtab-proj';
  btn.id = `mkv-stab-${project.id}`;
  btn.dataset.pid = project.id;
  btn.innerHTML = _mkvSubTabInnerHtml(project);
  btn.onclick = (e) => {
    if (!e.target.closest('.subtab-proj-close')) switchMkvSubTab(project.id);
  };
  container.appendChild(btn);
  _installSubtabScrollBindings();
  _updateSubtabScrollState();
}

/** Repinta el botón de sub-pestaña (cambió el nombre del fichero). */
function _mkvRefreshSubTab(project) {
  const btn = document.getElementById(`mkv-stab-${project.id}`);
  if (btn) btn.innerHTML = _mkvSubTabInnerHtml(project);
}

/** Crea el panel vacío del proyecto dentro del contenedor con scroll. */
function _mkvCreatePanel(project) {
  const host = document.getElementById('mkv-edit-panel');
  if (!host) return;
  const panel = document.createElement('div');
  panel.className = 'mkv-panel subtab-panel';
  panel.id = `mkv-panel-${project.id}`;
  panel.style.display = 'none';
  host.appendChild(panel);
}

function switchMkvSubTab(pid) {
  activeMkvProjectId = pid;
  document.querySelectorAll('#mkv-edit-panel > .mkv-panel').forEach(el => {
    el.style.display = 'none';
  });
  const activo = document.getElementById(`mkv-panel-${pid}`);
  if (activo) activo.style.display = 'block';
  document.querySelectorAll('#mkv-subtab-projects .subtab-proj').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.pid === pid);
  });
  _mkvUpdateEmptyState();
}

/** Empty state visible sólo cuando no queda ninguna pestaña abierta. */
function _mkvUpdateEmptyState() {
  const hay = openMkvProjects.length > 0;
  const empty = document.getElementById('mkv-empty-state');
  const panel = document.getElementById('mkv-edit-panel');
  const area  = document.getElementById('mkv-subtab-projects-area');
  if (empty) empty.style.display = hay ? 'none' : '';
  if (panel) panel.style.display = hay ? '' : 'none';
  if (area)  area.style.display  = hay ? '' : 'none';
}

/** Cierra una pestaña. Con cambios pendientes, el mismo aviso de siempre. */
function closeMkvProject(pid) {
  const project = openMkvProjects.find(p => p.id === pid);
  if (!project) return;
  if (project.dirty) {
    showConfirm(
      tr('tab2.cambios_sin_guardar_2'),
      tr('tab2.hay_cambios_sin_guardar_en_cerrar', {filename: project.fileName}),
      () => _doCloseMkvProject(pid),
      tr('core.cerrar_sin_guardar'),
    );
    return;
  }
  _doCloseMkvProject(pid);
}

function _doCloseMkvProject(pid) {
  const idx = openMkvProjects.findIndex(p => p.id === pid);
  if (idx === -1) return;
  openMkvProjects.splice(idx, 1);
  document.getElementById(`mkv-stab-${pid}`)?.remove();
  document.getElementById(`mkv-panel-${pid}`)?.remove();
  if (activeMkvProjectId === pid) {
    activeMkvProjectId = null;
    const siguiente = openMkvProjects[openMkvProjects.length - 1];
    if (siguiente) switchMkvSubTab(siguiente.id);
  }
  _mkvUpdateEmptyState();
  _updateSubtabScrollState();
  // La columna marca con «abierto» los MKV que tienen pestaña, así que al
  // cerrar una hay que repintarla o el distintivo se queda puesto. Tab 1 y
  // Tab 3 ya lo hacían (`_doFilterSidebarSessions` y `_renderCMv40Sidebar`);
  // esta era la única de las tres que no.
  if (document.getElementById('mkv-recientes-list')) _renderMkvRecientes();
}

/** Cierra el MKV activo — el botón "✕ Cerrar" del pie del panel. */
function closeMkvEditor() {
  if (activeMkvProjectId) closeMkvProject(activeMkvProjectId);
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
    showToast(tr('tab2.no_se_conoce_la_ruta_del'), 'error');
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
    _hydrateModalWithTmdb({
      name: fileName,
      modalId: 'mkv-analyze-modal',
      posterId: 'mkv-analyze-modal-poster',
      titleId: 'mkv-analyze-modal-title',
      subId: 'mkv-analyze-modal-file',
      subText: fileName,
    });
    _doAnalyzeMkvFromPickerPath(absPath, fileName, true).catch(e => {
      console.error('reanalyze MKV error:', e);
      showToast(tr('tab2.error_en_re_analisis', {e: e.message || e}), 'error');
    });
  };
  if (mkvProject.dirty) {
    showConfirm(
      tr('tab2.cambios_sin_guardar_2'),
      tr('tab2.hay_cambios_sin_guardar_en_este'),
      doRun,
      tr('tab2.descartar_y_re_analizar'),
    );
    return;
  }
  doRun();
}

function undoMkvEdits() {
  const project = mkvProject;
  if (!project) return;
  project.analysis = structuredClone(project.originalAnalysis);
  _mkvClearDirty(project);
  _renderMkvEditPanel(project);
  showToast(tr('tab2.cambios_revertidos'), 'info');
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
/** De DÓNDE sale un número: de la película, o de los primeros 30 s.
 *
 *  `dovi_tool info --summary` corre sobre un sniff de **720 frames**, así
 *  que el L5, el L6 y el pico L1 que devuelve son de los primeros treinta
 *  segundos — que en una película suelen ser logos y créditos oscuros. El
 *  resto de la pantalla (MediaInfo, mkvmerge, el análisis extendido) mide
 *  el fichero entero.
 *
 *  Mezclarlos sin decirlo es lo que hacía que la pantalla se contradijera:
 *  el banner de divergencia comparaba el pico de 30 s contra el MaxCLL de
 *  todo el metraje y sacaba «máster conservador» en Pulp Fiction, cuyo
 *  pico real (1001 nits) coincide con el declarado (1000) — y el propio
 *  gráfico de luminancia, cinco centímetros más abajo, lo decía.
 *
 *  **Sólo se marca la muestra.** Un badge en cada celda sería ruido: lo
 *  que el lector necesita saber es cuál de los números NO describe la
 *  película. Los tres bloques que tenían su propia redacción para esto
 *  (L5, L8 y ahora el pico L1) pasan por aquí.
 */
function _rgrfAlcance(esMuestra) {
  if (!esMuestra) {
    return `<span class="dv-alcance dv-alcance-film" data-i18n="tab2.alcance_pelicula"
                  data-i18n-tip="tab2.alcance_pelicula_tip"></span>`;
  }
  return `<span class="dv-alcance dv-alcance-muestra" data-i18n="tab2.alcance_muestra"
                data-i18n-tip="tab2.alcance_muestra_tip"></span>`;
}

function _rgrfRow(label, value, { tooltip = '', status = 'neutral' } = {}) {
  if (value == null || value === '' || value === undefined) {
    value = '<span style="color:var(--text-3); font-style:italic">—</span>';
  }
  const colorMap = {
    ok:      'var(--green-text)',
    warn:    'var(--orange-text)',
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
  const icon  = icono(present ? 'check' : 'cruz');
  const color = present ? 'var(--green-text)' : 'var(--text-3)';
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
    html += `<text x="${x}" y="${axisY + 18}" fill="var(--dv-text-3)" font-size="11.5"
               font-family="SF Mono,monospace" text-anchor="middle" font-weight="500">${t}</text>`;
  });
  // Dots con halo + label semántica debajo (si conocida)
  nits.forEach(n => {
    const x = xOf(n);
    html += `<circle cx="${x}" cy="${axisY}" r="10" fill="#007AFF" fill-opacity="0.12" />`;
    html += `<circle cx="${x}" cy="${axisY}" r="6.5" fill="url(#${gid})" stroke="#ffffff" stroke-width="2" />`;
    html += `<text x="${x}" y="${axisY - 14}" fill="var(--dv-accent-text)" font-size="12"
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
         style="display:block; background:var(--dv-chart-bg-alt); border-radius:8px; border:1px solid var(--dv-chart-border)"
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
      <circle cx="${d65x}" cy="${d65y}" r="4" fill="#ffffff" stroke="var(--dv-text-1)" stroke-width="1.5" />
      <text x="${d65x + 9}" y="${d65y + 4}" fill="var(--dv-text-1)" font-size="11" font-family="SF Mono,monospace" font-weight="700">D65</text>
      <!-- Leyenda glassmorphism -->
      <g font-size="11" font-family="SF Mono,monospace">
        <rect x="${svgSize - 94}" y="${pad - 4}" width="84" height="62" rx="6"
              fill="rgba(255,255,255,0.92)" stroke="rgba(15,23,42,0.08)" stroke-width="1" />
        <circle cx="${svgSize - 85}" cy="${pad + 8}" r="4" fill="${cRec2020}"/>
        <text x="${svgSize - 77}" y="${pad + 12}" fill="var(--dv-accent-text)" font-weight="700"><span data-i18n="tab2.rec_2020"></span></text>
        <circle cx="${svgSize - 85}" cy="${pad + 26}" r="4" fill="${cP3}"/>
        <text x="${svgSize - 77}" y="${pad + 30}" fill="#92400e" font-weight="700"><span data-i18n="tab2.dci_p3"></span></text>
        <circle cx="${svgSize - 85}" cy="${pad + 44}" r="4" fill="${cRec709}"/>
        <text x="${svgSize - 77}" y="${pad + 48}" fill="#9f1239" font-weight="700" data-i18n="tab2.rec_709"></text>
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
/** La curva de luz de la película, en escala logarítmica.
 *
 *  Lo que había no se podía leer, y el usuario lo describió con tres
 *  capturas (2026-09-24): «se ve pequeño, las líneas se solapan, juntan
 *  5 o 6 conceptos en el mismo sitio». Las tres cosas medidas:
 *
 *  · **La escala lineal es la equivocada para HDR.** En Watchmen la
 *    mediana (119 nits sobre un pico de 2354) caía al **4,4 %** de la
 *    altura: la mitad de la película vivía en nueve píxeles. En
 *    logarítmica sube al 60,5 %. Apocalypse pasa del 10 % al 72,8 % y
 *    El padrino del 21,9 % al 82 %.
 *  · **Once conceptos a la vez**: tres curvas, tres referencias
 *    dibujadas y cinco chips de «fuera del chart».
 *  · **Las etiquetas se pisaban**: MaxCLL 1000 y L2 1001 se rotulaban
 *    uno encima del otro, literalmente ilegibles.
 *
 *  De ahí las tres decisiones, tomadas con el usuario:
 *
 *  1. **Una línea y una banda.** El pico manda; el mínimo y el medio
 *     van de sombra detrás. Las tres series siguen ahí, pero sólo una
 *     compite por la atención.
 *  2. **Las referencias no se rotulan en el lienzo.** Se dibujan finas
 *     y sus nombres van a una leyenda agrupada por FAMILIA —lo que el
 *     disco declara contra los objetivos de trim—, que es la distinción
 *     que antes no se veía.
 *  3. **La película entera al abrir.**
 *
 *  Y si la serie es constante no se dibuja nada: le pasa a 3 de los 7
 *  MKV medidos, donde el relleno azul parecía un dato y no lo era.
 */
function _rgrfSparklineSvg(series, labelMax, durationSeconds, opts = {}) {
  if (!Array.isArray(series) || series.length < 2) return '';
  const distintos = new Set(series).size;
  if (distintos <= 1) {
    return `<div class="dv-luz-plana" data-i18n-html="tab2.luz_plana_grafico"></div>`;
  }
  const avgSeries = Array.isArray(opts.avgSeries) && opts.avgSeries.length === series.length
    ? opts.avgSeries : null;
  const minSeries = Array.isArray(opts.minSeries) && opts.minSeries.length === series.length
    ? opts.minSeries : null;
  const refs = (opts.refs && typeof opts.refs === 'object') ? opts.refs : {};

  // ── Geometría ────────────────────────────────────────────────────
  // El SVG escala al ancho del contenedor (`viewBox` + `width:100%`) en
  // vez de ir a 720 px fijos, y el margen derecho baja de 118 a 16: esos
  // 102 px eran sólo para los rótulos que ahora están en la leyenda.
  const svgW = 1000, svgH = 320;
  const padL = 62, padR = 16, padT = 16, padB = 34;
  const usableW = svgW - padL - padR;
  const usableH = svgH - padT - padB;

  // ── Escala logarítmica ───────────────────────────────────────────
  // El suelo es la década por debajo del mínimo y el techo la de encima
  // del pico, así que el eje siempre cae en potencias de diez y las
  // marcas son 1 · 10 · 100 · 1000 · 10000 — como se lee una gráfica de
  // brillo en cualquier sitio.
  // **El suelo es 1 nit, no la década del mínimo.** Con el suelo pegado
  // al valor más bajo el eje se estrecha y la curva vuelve a aplastarse:
  // en Watchmen (mínimo 118, pico 2354) el rango quedaba en 100-10.000 y
  // la mediana caía otra vez al 4 %. Con el eje completo —1 a la década
  // del pico— sube al 52 %, que es la mejora que se midió. Y la parte
  // baja no se desperdicia: ahí vive la banda del mínimo, que en estas
  // películas baja de 10 nits.
  const vMax = Math.max(...series, 10);
  const L0 = 0;
  const L1 = Math.max(1, Math.ceil(Math.log10(vMax)));
  const yOf = (v) => {
    const l = Math.log10(Math.max(v, Math.pow(10, L0)));
    const t = (l - L0) / (L1 - L0);
    return padT + usableH - Math.max(0, Math.min(1, t)) * usableH;
  };
  const xOf = (i) => padL + (i / (series.length - 1)) * usableW;
  const tOf = (i) => (durationSeconds && durationSeconds > 0)
    ? (i / (series.length - 1)) * durationSeconds : 0;

  const _smoothPath = (pts) => {
    if (pts.length < 2) return '';
    let p = `M ${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[Math.max(0, i - 1)], p1 = pts[i];
      const p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
      const cp1x = p1[0] + (p2[0] - p0[0]) / 6, cp1y = p1[1] + (p2[1] - p0[1]) / 6;
      const cp2x = p2[0] - (p3[0] - p1[0]) / 6, cp2y = p2[1] - (p3[1] - p1[1]) / 6;
      p += ` C ${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
    }
    return p;
  };

  // ── La banda: entre el mínimo y el medio ─────────────────────────
  let banda = '';
  if (avgSeries && minSeries) {
    const arriba = avgSeries.map((v, i) => [xOf(i), yOf(v)]);
    const abajo = minSeries.map((v, i) => [xOf(i), yOf(v)]).reverse();
    banda = `<path d="${_smoothPath(arriba)} L ${abajo.map(pt => `${pt[0].toFixed(1)},${pt[1].toFixed(1)}`).join(' L ')} Z"
                   fill="var(--dv-accent-bg-2)" stroke="none" />`;
  }

  const peakPts = series.map((v, i) => [xOf(i), yOf(v)]);
  const linePath = _smoothPath(peakPts);

  // ── Rejilla: una línea por década ────────────────────────────────
  const rejilla = [];
  for (let d = L0; d <= L1; d++) {
    const v = Math.pow(10, d), y = yOf(v);
    rejilla.push(`<line x1="${padL}" y1="${y.toFixed(1)}" x2="${svgW - padR}" y2="${y.toFixed(1)}"
                        stroke="var(--dv-grid)" stroke-dasharray="3,4" />`);
    rejilla.push(`<text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end"
                        class="dv-luz-eje">${v >= 1000 ? (v / 1000) + 'k' : v}</text>`);
  }

  // ── Referencias: línea fina y SIN rótulo ─────────────────────────
  // El nombre va a la leyenda. Aquí un rótulo por cada una es lo que
  // producía «MaxCLL 1000n» encima de «L2 1001n».
  const familias = [
    { clave: 'declara', color: 'var(--dv-text-3)', items: [
        ['master', refs.l6_master_max_nits], ['MaxCLL', refs.hdr10_max_cll],
        ['MaxFALL', refs.hdr10_max_fall], ['L6 CLL', refs.l6_max_cll]] },
    // L8 va primero y L2 sólo aporta los que él no trae: un target que
    // está en los dos es UNA pantalla de destino, y pintarla dos veces
    // deja dos líneas en el mismo píxel — el problema de las etiquetas
    // encimadas, con otra cara.
    { clave: 'trim', color: 'var(--amber)', items: (() => {
        const l8 = refs.l8_trim_nits_full || [];
        const ya = new Set(l8);
        return [...l8.map(n => ['L8', n]),
                ...(refs.l2_trim_targets_nits || [])
                    .filter(n => !ya.has(n)).map(n => ['L2', n])];
      })() },
  ];
  const lineasRef = [];
  const leyenda = [];
  const techo = Math.pow(10, L1);
  for (const fam of familias) {
    const dentro = [], fuera = [];
    for (const [nombre, val] of fam.items) {
      if (!val || val <= 0) continue;
      (val <= techo ? dentro : fuera).push([nombre, val]);
      if (val <= techo) {
        lineasRef.push(`<line x1="${padL}" y1="${yOf(val).toFixed(1)}" x2="${svgW - padR}"
                              y2="${yOf(val).toFixed(1)}" stroke="${fam.color}"
                              stroke-width="1" stroke-dasharray="6,5" opacity="0.7" />`);
      }
    }
    if (dentro.length || fuera.length) {
      leyenda.push({ clave: fam.clave, color: fam.color, dentro, fuera });
    }
  }

  const ejeX = [0, 0.25, 0.5, 0.75, 1].map(p => {
    const i = Math.round(p * (series.length - 1));
    return `<text x="${xOf(i).toFixed(1)}" y="${svgH - 12}" text-anchor="${
      p === 0 ? 'start' : p === 1 ? 'end' : 'middle'}"
      class="dv-luz-eje">${_rgrfFmtTime(tOf(i))}</text>`;
  }).join('');

  const chips = (arr, color, fuera) => arr.map(([n, v]) =>
    `<span class="dv-luz-chip${fuera ? ' dv-luz-chip-fuera' : ''}" style="--chip-c:${color}">${
      escHtml(n)} ${v.toLocaleString(localeActual())}n</span>`).join('');
  const leyendaHtml = leyenda.map(f => `
      <div class="dv-luz-fam">
        <span class="dv-luz-fam-rotulo" data-i18n="tab2.luz_fam_${f.clave}"></span>
        ${chips(f.dentro, f.color, false)}${chips(f.fuera, f.color, true)}
      </div>`).join('');

  return `
    <div class="dv-luz">
      <div class="dv-luz-ayuda" data-i18n-html="tab2.luz_como_se_lee"></div>
      <svg class="dv-luz-svg" viewBox="0 0 ${svgW} ${svgH}" preserveAspectRatio="none"
           role="img" data-dur="${durationSeconds || 0}">
        ${rejilla.join('')}
        ${banda}
        ${lineasRef.join('')}
        <path d="${linePath}" fill="none" stroke="var(--dv-accent-soft)" stroke-width="2"
              stroke-linejoin="round" />
        ${ejeX}
      </svg>
      <div class="dv-luz-leyenda">
        <div class="dv-luz-fam">
          <span class="dv-luz-fam-rotulo" data-i18n="tab2.luz_fam_medido"></span>
          <span class="dv-luz-chip dv-luz-chip-linea" style="--chip-c:var(--dv-accent-soft)"
                data-i18n="tab2.luz_pico_frame"></span>
          <span class="dv-luz-chip dv-luz-chip-banda" style="--chip-c:var(--dv-accent-bg-2)"
                data-i18n="tab2.luz_banda"></span>
        </div>
        ${leyendaHtml}
      </div>
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
          <div class="dv-l1-stats-block-title" data-i18n="tab2.percentiles_dv_l1_max_pq"></div>
          <div class="dv-l1-stats-grid">
            <div><span class="lbl"><span data-i18n="tab2.peak"></span></span><span class="val">${stats.peak}<span class="u" data-i18n="tab2.nits"></span></span></div>
            <div><span class="lbl">p99</span><span class="val">${stats.p99}<span class="u" data-i18n="tab2.nits"></span></span></div>
            <div><span class="lbl">p95</span><span class="val">${stats.p95}<span class="u" data-i18n="tab2.nits"></span></span></div>
            <div><span class="lbl">p50</span><span class="val">${stats.p50}<span class="u" data-i18n="tab2.nits"></span></span></div>
            <div><span class="lbl"><span data-i18n="tab2.avg"></span></span><span class="val">${stats.avg_of_max}<span class="u" data-i18n="tab2.nits"></span></span></div>
          </div>
        </div>
        <div class="dv-l1-stats-block">
          <div class="dv-l1-stats-block-title" data-i18n="tab2.distribucion_de_frames_por_brillo"></div>
          <div class="dv-l1-bars">
            <div class="dv-l1-bar-row">
              <span class="dv-l1-bar-label"><span data-i18n="tab2.sdr_like"></span> &lt;100n</span>
              <div class="dv-l1-bar-track"><div class="dv-l1-bar-fill" style="width:${p1}%; background:#94a3b8"></div></div>
              <span class="dv-l1-bar-pct">${p1}%</span>
              <span class="dv-l1-bar-count">(${stats.bucket_dim.toLocaleString(localeActual())})</span>
            </div>
            <div class="dv-l1-bar-row">
              <span class="dv-l1-bar-label" data-i18n="tab2.midtone_100_300n"></span>
              <div class="dv-l1-bar-track"><div class="dv-l1-bar-fill" style="width:${p2}%; background:#3395ff"></div></div>
              <span class="dv-l1-bar-pct">${p2}%</span>
              <span class="dv-l1-bar-count">(${stats.bucket_mid.toLocaleString(localeActual())})</span>
            </div>
            <div class="dv-l1-bar-row">
              <span class="dv-l1-bar-label" data-i18n="tab2.highlight_300n"></span>
              <div class="dv-l1-bar-track"><div class="dv-l1-bar-fill" style="width:${p3}%; background:#f59e0b"></div></div>
              <span class="dv-l1-bar-pct">${p3}%</span>
              <span class="dv-l1-bar-count">(${stats.bucket_high.toLocaleString(localeActual())})</span>
            </div>
          </div>
        </div>
      </div>
      ${hdr10 ? `<div class="dv-l1-stats-foot">HDR10 container: ${hdr10}<span class="dv-l1-stats-foot-note" data-i18n="tab2.metrica_estatica_del_sei_distinta_de"></span></div>` : ''}
    </div>`;
}

/** Cadena de mastering — sustituye al bloque "Gamut CIE 1931" + parte
 *  del bloque tr('tab2.luminancia'). Muestra textualmente con chips toda la
 *  ficha del color/master del MKV, que es donde realmente varia entre
 *  discos UHD (el container BT.2020 es constante asi que el diagrama
 *  CIE no aportaba info). Distingue 3 etapas: master donde se grade,
 *  container del stream, target del DV.
 *
 *  dv         — analysis.dovi (puede ser null)
 *  hdr        — analysis.hdr (HdrMetadata)
 *  mainVideo  — pista video principal (para bit_depth)
 */
/** Lo que el CONTENEDOR declara del HDR, en chips.
 *
 *  `hdr_format` lo deriva la app de la curva de transferencia («PQ» →
 *  «HDR10»), así que un disco con Dolby Vision decía «HDR10» a secas: se
 *  perdían el perfil declarado, las capas y —lo más útil— con qué es
 *  compatible, que es la pregunta de «¿esto lo reproduce mi equipo?».
 *
 *  Y cuando el perfil declarado no coincide con el que `dovi_tool` lee
 *  del RPU, eso es justo lo que hay que enseñar: es la firma del bug de
 *  «Te van a matar», un MKV anunciado `dvhe.07` con el EL descartado.
 */
function _rgrfHdrDeclarado(dv, hdr) {
  const dvDeclarado = (hdr?.dv_profile_string || '').trim();
  const perfilMedido = dv?.profile ? `dvhe.0${dv.profile}` : '';
  const discrepa = !!(dvDeclarado && perfilMedido
                      && dvDeclarado.toLowerCase() !== perfilMedido.toLowerCase());
  return [
    hdr?.hdr_format_raw ? `<span class="dv-mc-dec-fmt">${escHtml(hdr.hdr_format_raw)}</span>` : '',
    dvDeclarado ? `<span class="dv-mc-trim-chip${discrepa ? ' dv-mc-chip-warn' : ''}"${
      discrepa ? ` data-i18n-tip="tab2.perfil_declarado_no_coincide"` : ''
      }>${escHtml(dvDeclarado)}${hdr.dv_level ? `.${escHtml(hdr.dv_level)}` : ''}</span>` : '',
    hdr?.dv_layers ? `<span class="dv-mc-trim-chip">${escHtml(hdr.dv_layers)}</span>` : '',
    hdr?.hdr_format_compatibility
      ? `<span class="dv-mc-dec-compat"><span data-i18n="tab2.compatible_con"></span> <strong>${escHtml(hdr.hdr_format_compatibility)}</strong></span>`
      : '',
  ].filter(Boolean).join(' ');
}

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
  const masterSource = l9 ? tr('tab2.desde_l9') : (masterPrim ? tr('tab2.desde_hdr10_sei') : '');

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

  // **La fila de «DV trim targets» se fue de aquí.** Enseñaba los
  // objetivos de L2 con el rótulo genérico de «trim targets», y en un
  // RPU CMv4.0 —que lleva L2 y L8— eso deja fuera justo los que mandan.
  // Peor: los de L8 se pintaban dos bloques más abajo con otra escala,
  // porque `luminance` los sacaba del `trim_slope`. Los dos viven ahora
  // en la tabla de niveles, cada uno en su fila y con la misma unidad.
  // HDR10 metadata footer
  const hdr10Cll  = hdr?.max_cll  != null ? `MaxCLL ${hdr.max_cll} nits` : '';
  const hdr10Fall = hdr?.max_fall != null ? `MaxFALL ${hdr.max_fall} nits` : '';
  const hdr10Line = [hdr10Cll, hdr10Fall].filter(Boolean).join(' · ');


  // **El aviso de divergencia se fue al titular.** Comparar el pico
  // medido con el máster declarado es una CONCLUSIÓN, y el titular
  // tiene una frase para eso («La luz»). Tenerlo en los dos sitios era
  // decir dos veces lo mismo con distinta redacción — y mientras usó el
  // sniff de 30 s, decirlo mal en uno de ellos.


  return `
    <section class="dv-block">
      <h5 class="dv-block-title"><span data-i18n="tab2.cadena_de_mastering"></span>
        <span class="dv-block-sub" data-i18n="tab2.grade_source_container_dv_targets"></span>
      </h5>
      <div class="dv-mc-grid">
        <div class="dv-mc-card">
          <div class="dv-mc-card-title"><span data-i18n="tab2.master_display"></span>
            ${masterSource ? `<span class="dv-mc-card-src">· ${masterSource}</span>` : ''}
          </div>
          <div class="dv-mc-card-primary">${escHtml(masterPrimResolved)}</div>
          <div class="dv-mc-card-meta"><span data-i18n="tab2.peak"></span> <strong>${masterPeakStr}</strong> · min ${masterMinStr}</div>
        </div>
        <div class="dv-mc-card">
          <div class="dv-mc-card-title" data-i18n="tab2.container_hevc"></div>
          <div class="dv-mc-card-primary">${escHtml(cont.primaries || '—')}</div>
          <div class="dv-mc-card-meta">
            ${cont.transfer ? `<strong>${escHtml(cont.transfer)}</strong>` : '—'}
            ${cont.bitDepth ? ` · ${cont.bitDepth}-bit` : ''}
          </div>
          ${showExpansionChip ? `<div class="dv-mc-flow-hint" data-i18n="tab2.p3_bt_2020_gamut_expandido_al"></div>` : ''}
        </div>
        <div class="dv-mc-card">
          <div class="dv-mc-card-title">${tr('tab2.dv_target_display_p1', {p1: l10 ? '<span class="dv-mc-card-src">· L10</span>' : ''})}
          </div>
          <div class="dv-mc-card-primary">${l10 ? escHtml(l10) : '—'}</div>
          <div class="dv-mc-card-meta">
            ${l10
              ? tr('tab2.gamut_objetivo_del_grade_dv')
              : '<span class="dv-mc-empty">' + tr('tab2.l10_no_presente_dv_targeting_generico') + '</span>'}
          </div>
        </div>
      </div>
      ${hdr10Line ? `
        <div class="dv-mc-row-hdr10">
          <div class="dv-mc-row-label"><span data-i18n="tab2.hdr10_metadata"></span> <span class="dv-mc-row-sub" data-i18n="tab2.sei_estatica"></span></div>
          <div class="dv-mc-row-content"><span class="dv-mc-hdr10-val">${hdr10Line}</span></div>
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

  // Sufijo aleatorio: con dos paneles abiertos, unos ids fijos harían que el
  // segundo SVG redefiniera los gradientes del primero (y los pintara mal).
  const gid = `hist-${Math.random().toString(36).slice(2, 7)}`;
  let defs = '<defs>';
  colors.forEach((c, i) => {
    defs += `<linearGradient id="${gid}-${i}" x1="0" y1="0" x2="0" y2="1">
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
    grid += `<text x="${padL - 8}" y="${y + 4}" fill="var(--dv-text-3)" font-size="11" font-family="SF Mono,monospace" text-anchor="end" font-weight="500">${lbl}%</text>`;
  });

  let bars = '';
  counts.forEach((c, i) => {
    const pct = (c / total) * 100;
    const h = (pct / maxPct) * usableH;
    const x = padL + i * barW;
    const y = padT + usableH - h;
    // Barra con radius top + shadow sutil
    bars += `<rect x="${x + 8}" y="${y}" width="${barW - 16}" height="${Math.max(h, 1)}"
               fill="url(#${gid}-${i})" rx="3" />`;
    bars += `<text x="${x + barW/2}" y="${padT + usableH + 18}" fill="var(--dv-text-2)" font-size="12"
               font-family="SF Mono,monospace" text-anchor="middle" font-weight="600">${binLabels[i]}</text>`;
    if (c > 0) {
      bars += `<text x="${x + barW/2}" y="${y - 6}" fill="var(--dv-text-1)" font-size="12"
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
      <text x="${padL + usableW/2}" y="${svgH - 10}" fill="var(--dv-text-3)" font-size="11"
            font-family="SF Mono,monospace" text-anchor="middle" font-weight="500" data-i18n="tab2.pico_de_luz_por_frame_nits"></text>
    </svg>`;
}

/** Render del bloque tr('tab2.informacion_detallada_hdr_dolby_vision').
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

  // La ficha técnica que MediaInfo ya daba y no se leía. `Main 10@L5.1`
  // con su tier es la forma en que se cita un stream HEVC en cualquier
  // análisis, y la terna croma/rango/matriz es la señal de color completa
  // —hasta ahora sólo se enseñaban los primarios—.
  const perfilHevc = [mainVideo?.format_profile,
                      mainVideo?.format_level ? `@L${mainVideo.format_level}` : '',
                      mainVideo?.format_tier].filter(Boolean).join(' ').trim() || '—';
  // Un VFR en un remux de disco no es normal: el disco es de tasa fija.
  const modoTasaEstado = !mainVideo?.framerate_mode ? 'neutral'
    : (mainVideo.framerate_mode.toUpperCase() === 'CFR' ? 'ok' : 'warn');
  const senalColor = [hdr?.chroma_subsampling, hdr?.colour_range,
                      hdr?.matrix_coefficients].filter(Boolean).join(' · ') || '—';

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
  // **El titular absorbe la card del veredicto.** Decidido con el usuario
  // el 2026-09-24: el veredicto pasa a ser la conclusión de «El máster» y
  // los cuatro stats bajan a la tabla de niveles, donde ya estaban casi
  // todos. Una cosa menos que leer para decir lo mismo.
  const blockQuality = _rgrfTitular(a, dv);

  // ═══════════════════════════════════════════════════════════════
  // BLOQUE 1 · Stream (profile + timing + structure)
  // ═══════════════════════════════════════════════════════════════
  // Scene cuts + density si la auditoría profunda lo ha calculado.
  // Esto solo aparece cuando el usuario ha pulsado tr('tab2.analisis_extendido')
  // (los datos vienen del quality audit, no del análisis básico).
  const sceneCutsCell = (dv?.quality_scene_cuts || 0) > 0
    ? cell(
        'Scene cuts',
        tr('tab2.scene_cuts_cada', {
          n: dv.quality_scene_cuts.toLocaleString(localeActual()),
          seg: (a.duration_seconds / dv.quality_scene_cuts).toFixed(1)}),
        { tooltip: tr('tab2.no_de_frames_con_scene_refresh') }
      )
    : '';

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
          <td>${z.frames.toLocaleString(localeActual())}</td>
          <td><strong>${z.pct}%</strong></td>
        </tr>`;
    }).join('');
    blockActiveArea = `
      <div class="dv-ficha-encuadre">
        <div class="dv-mc-row-label"><span data-i18n="tab2.active_area"></span>
          <span class="dv-mc-row-sub">${tr('tab2.l5_p1_zonas_detectadas_letterbox_dinamico', {p1: l5Zones.length})}</span>
        </div>
        <table class="dv-l5-zones-table">
          <thead><tr><th>#</th><th data-i18n="tab2.offsets_px"></th><th data-i18n="tab2.area_activa"></th><th data-i18n="tab2.ratio"></th><th data-i18n="tab2.frames"></th><th>%</th></tr></thead>
          <tbody>${zonesHtml}</tbody>
        </table>
      </div>`;
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
    const subLabel = _rgrfAlcance(!z0);
    blockActiveArea = `
      <div class="dv-ficha-encuadre">
        <div class="dv-mc-row-label"><span data-i18n="tab2.active_area"></span> ${subLabel}</div>
        <div class="dv-split">
          <div class="dv-grid-2">
            ${cell(tr('tab2.area_activa'), `${aWi} × ${aHi}`)}
            ${cell(tr('tab2.aspect_ratio'), aspectLabel)}
            ${(sV && sH) ? '' : cell(tr('tab2.simetria'),
                `${sV ? 'T = B' : `Δ ${Math.abs(lTop - lBot)} px`} · ${sH ? 'L = R' : `Δ ${Math.abs(lLft - lRgt)} px`}`,
                { status: 'warn', tooltip: tr('tab2.simetria_tip') })}
          </div>
          <div class="dv-viz-side">${_rgrfL5Svg(dv, frameW, frameH)}</div>
        </div>
      </div>`;
  }

  // **El HDR declarado y el contenedor viven AQUÍ.**
  //
  // El primero estuvo unos días en la cadena de mastering y no es su
  // sitio: describe lo que el fichero dice ser, igual que el códec y la
  // señal de color, mientras la cadena describe dónde se hizo el grade.
  // Y el contenedor era un bloque propio al final, con seis celdas —una
  // sección entera para decir con qué se muxeó.
  const declaradoHdr = _rgrfHdrDeclarado(dv, hdr);
  const c = a.container;
  const filaContenedor = !c ? '' : `
      <div class="dv-ficha-contenedor">
        <div class="dv-mc-row-label"><span data-i18n="tab2.contenedor"></span> <span class="dv-mc-row-sub" data-i18n="tab2.contenedor_sub"></span></div>
        <div class="dv-grid-3">
          ${cell(tr('tab2.formato'), [c.format, c.format_version].filter(Boolean).join(' v') || '—')}
          ${cell(tr('tab2.tasa_media'), c.overall_bitrate_kbps
              ? `${(c.overall_bitrate_kbps / 1000).toFixed(1)} Mbps${c.overall_bitrate_mode ? ` · ${escHtml(c.overall_bitrate_mode)}` : ''}`
              : '—', { tooltip: tr('tab2.tasa_media_tip') })}
          ${cell(tr('tab2.titulo_interno'), c.title ? escHtml(c.title) : '—', { tooltip: tr('tab2.titulo_interno_tip') })}
          ${cell(tr('tab2.muxeado_con'), c.encoded_application ? escHtml(c.encoded_application) : '—')}
          ${cell(tr('tab2.muxeado_el'), c.encoded_date ? escHtml(c.encoded_date) : '—')}
          ${cell(tr('tab2.ids_externos'), (c.imdb_id || c.tmdb_id)
              ? `${escHtml(c.imdb_id || '—')} · ${escHtml(c.tmdb_id || '—')}`
              : '—', { tooltip: tr('tab2.ids_del_contenedor_tip') })}
        </div>
      </div>`;

  const blockStream = `
    <section class="dv-block">
      <h5 class="dv-block-title"><span data-i18n="tab2.ficha_tecnica"></span>
        <span class="dv-block-sub" data-i18n="tab2.ficha_tecnica_sub"></span>
      </h5>
      <div class="dv-grid-3">
        ${cell('Profile', profile)}
        ${cell(tr('tab2.cm_version'), cmLabel)}
        ${cell('Frames', framesTotal ? framesTotal.toLocaleString(localeActual()) : '—', { tooltip: tr('tab2.total_de_frames_del_mkv') })}
        ${cell(tr('tab1.duracion'), durationStr)}
        ${cell('FPS', fps, { tooltip: tr('tab2.fps_del_track_de_video') })}
        ${cell('Bit depth', mainVideo?.bit_depth ? `${mainVideo.bit_depth}-bit` : '—')}
        ${cell('Codec', mainVideo?.codec || '—')}
        ${cell(tr('tab2.perfil_codec'), perfilHevc, { tooltip: tr('tab2.perfil_codec_tip') })}
        ${cell(tr('tab2.modo_de_tasa'), mainVideo?.framerate_mode || '—', { tooltip: tr('tab2.modo_de_tasa_tip'), status: modoTasaEstado })}
        ${cell(tr('tab2.muestreo_croma'), senalColor, { tooltip: tr('tab2.muestreo_croma_tip') })}
        ${cell('RPU', rpuSize, { tooltip: tr('tab2.tamano_total_estimado_del_rpu') })}
        ${sceneCutsCell}
        ${elVideo ? cell(tr('tab2.enhancement_layer'), `${escHtml(elVideo.codec || 'HEVC')} · ${escHtml(elVideo.pixel_dimensions || '')}`) : ''}
      </div>
      ${declaradoHdr ? `
        <div class="dv-mc-row-declarado">
          <div class="dv-mc-row-label"><span data-i18n="tab2.hdr_declarado"></span> <span class="dv-mc-row-sub" data-i18n="tab2.segun_el_contenedor"></span></div>
          <div class="dv-mc-row-content">${declaradoHdr}</div>
        </div>` : ''}
      ${blockActiveArea}
      ${filaContenedor}
    </section>`;

  // ═══════════════════════════════════════════════════════════════
  // BLOQUE 2 · Cadena de mastering (sustituye al antiguo bloque
  // tr('tab2.luminancia') + bloque "Gamut CIE 1931"). Toda la info de primaries,
  // mastering display, container HEVC, DV L9/L10 y trim targets en una
  // sola ficha escaneable. La luminancia DV L1 dinámica vive en el
  // bloque del sparkline donde hay graficos + stats card; la HDR10
  // estatica se muestra aqui como dato del SEI.
  // ═══════════════════════════════════════════════════════════════
  const blockMastering = _rgrfMasteringChain(dv, hdr, mainVideo);

  // ═══════════════════════════════════════════════════════════════
  // BLOQUE 4 · Los niveles del RPU, en una tabla
  // ═══════════════════════════════════════════════════════════════
  // Se pinta SIEMPRE: los niveles no son cosa de CMv4.0 —L1, L2, L5 y L6
  // son del Blu-ray— y con `isV40` el bloque no existía en 8 de los 10
  // MKV del NAS. El detalle está en `_rgrfTablaDeNiveles`.
  const blockNiveles = _rgrfTablaDeNiveles(dv, hdr);

  // BLOQUE 5 ELIMINADO — la antigua "Gamut CIE 1931" se sustituyo por la
  // cadena de mastering (BLOQUE 2) que muestra textualmente toda la info de
  // primaries y trim targets. El diagrama CIE no aportaba info nueva en UHD
  // BD donde casi siempre coincide BT.2020 container + P3/2020 master.

  // ═══════════════════════════════════════════════════════════════
  // BLOQUE 6 · Perfil de luminancia (sparkline + distribución) + botón
  // ═══════════════════════════════════════════════════════════════
  const lightMeta = hasLightProfile
    ? tr('tab2.n_buckets_max_nits', {n: dv.per_scene_max_cll.length,
                                     max: Math.max(...dv.per_scene_max_cll)})
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
         <div class="dv-chart-empty-icon"><span data-icono="grafico"></span></div>
         <div class="dv-chart-empty-text" data-i18n="tab2.perfil_de_luminancia_no_generado"></div>
         <div class="dv-chart-empty-hint"><span data-i18n-html="tab2.sale_del_analisis_rpu_luz"></span></div>
       </div>`;
  // **Con perfil ya calculado, esta card no tiene botones.** Tenía dos y los
  // dos se fueron el 2026-09-23: «Re-analizar», que repetía diez minutos de
  // `extract-rpu` para volver a leer un RPU que no ha cambiado —el MKV no se
  // toca desde aquí—, y el comparador A/B, que superponía la curva de otro
  // MKV. Ninguno se usaba, y entre los dos ocupaban la fila de acciones
  // entera encima del gráfico, que es lo que se ha venido a mirar.
  //
  // El que SÍ queda es el de la card sin perfil: ahí el botón es la única
  // forma de generarlo.
  const actionBtn = hasLightProfile ? ''
    : `<button class="btn btn-primary btn-sm dv-chart-action" data-analisis-extendido="1" onclick="_rgrfAuditQuality(event)" data-i18n-tip="tab2.analisis_extendido_combos_l8_l2_perfil"><span><span data-icono="lupaOnda"></span></span> <span data-i18n="tab2.analisis_extendido"></span></button>`;
  // Tooltip explicando que estos valores son metadata DV L1 (no medidas
  // reales en pantalla). Para BR2049 nuestro peak es ~176 nits aunque
  // medidas reales tras tone-mapping sean 500-600 nits — porque el
  // colorista etiqueto conservadoramente. Confirmado: dovi_tool info
  // --summary reporta el mismo MaxCLL.
  const lightHint = hasLightProfile
    ? `<span class="dv-block-hint" data-i18n-tip="tab2.valores_extraidos_del_bloque_l1_del">ℹ︎</span>`
    : '';
  const blockLight = `
    <section class="dv-block">
      <div class="dv-block-head">
        <h5 class="dv-block-title">${tr('tab2.perfil_de_luminancia_dv_l1_por', {lighthint: lightHint})} <span class="dv-block-sub" data-i18n="tab2.metadata_max_pq_no_luminancia_real"></span></h5>
        ${(lightMeta || actionBtn) ? `<div class="dv-block-action">
          ${lightMeta ? `<span class="dv-block-meta">${lightMeta}</span>` : ''}
          ${actionBtn}
        </div>` : ''}
      </div>
      ${sparklineArea}
    </section>`;

  // ═══════════════════════════════════════════════════════════════
  // ═══════════════════════════════════════════════════════════════
  //  Ensamblaje con toolbar superior compacta
  // ═══════════════════════════════════════════════════════════════
  return `
    <div class="dv-detail">
      <div class="dv-detail-header">
        <h4 class="dv-detail-title" data-i18n="tab2.informacion_detallada_hdr_dolby_vision"></h4>
        <button class="btn btn-ghost btn-sm" onclick="_rgrfCopyToClipboard(event)" data-i18n-tip="tab2.copia_toda_la_informacion_como_markdown"><span data-icono="portapapeles"></span> <span data-i18n="ui.copiar"></span></button>
      </div>
      ${blockQuality}
      ${blockStream}
      ${blockMastering}
      ${blockNiveles}
      ${blockLight}
    </div>`;
}

/**
 * Card de auditoría de calidad del RPU. Dos estados:
 *  - Sin datos (quality_classification vacío): CTA tr('tab2.analisis_extendido')
 *  - Con datos: badge color + verdict + 4 mini-stats + descripción técnica
 *
 * El usuario pulsa la CTA → pipeline backend de 5-10 min → la card se
 * re-renderiza poblada. El resultado se persiste en el cache MKV, así
 * que re-abrir el MKV muestra la card directamente.
 */
/** Los once niveles del RPU en una tabla: qué aporta, qué hay, y de dónde.
 *
 *  Sustituye a las siete pills de «CMv4.0 levels extendidos», que tenían
 *  tres problemas de fondo y ninguno era de forma:
 *
 *  · **No era un grupo, era un cajón.** Los niveles estaban repartidos por
 *    cinco bloques —L1 en el perfil de luminancia, L2/L6/L9/L10/L11 en la
 *    cadena de mastering, L5 en su bloque, L8 partido entre la card de
 *    calidad y aquí— y este juntaba «los que no caben en otro sitio», con
 *    solapes. Por eso no se entendía: no describía ninguna idea.
 *  · **Una pill es binaria y un nivel no lo es.** Backrooms tiene L3 con
 *    UN combo y un máster trabajado tiene cientos: la misma pill verde
 *    para «hay un bloque testimonial» y para «grading de tonos medios
 *    plano a plano».
 *  · **Dos pills no podían encenderse.** `L254` no se mide —`--levels` no
 *    lo acepta y sacarlo pediría el volcado de 682 MB— así que su pill
 *    apagada decía «no está» cuando la verdad es «no lo hemos mirado».
 *    Es el defecto del banner de divergencia otra vez: afirmar lo que no
 *    se sabe.
 *
 *  De ahí las tres decisiones: **tres estados y no dos** (presente ·
 *  ausente · no medido), **valores en vez de checks**, y el alcance por
 *  fila. Y se pinta SIEMPRE: con `isV40` no existía en 8 de los 10 MKV
 *  del NAS, y con él se iba L4, que 5 de esos 8 tienen.
 */
/** El titular: las tres frases que interpretan el análisis.
 *
 *  Sustituye a la card del veredicto —punto de color y cuatro cifras—,
 *  que respondía a medias una sola de las tres preguntas que trae a
 *  alguien a esta pantalla: qué es este fichero, cómo está masterizado y
 *  cómo se ve. Los números no desaparecen: bajan a la tabla de niveles,
 *  donde ya estaban casi todos.
 *
 *  **Las frases las compone el SERVIDOR** (`phases/mkv_lectura.py`), por
 *  lo mismo que el relato: son una interpretación, y una interpretación
 *  escrita dos veces se desincroniza. Aquí sólo se pintan.
 *
 *  El botón se queda: sin análisis extendido no hay frase de luz ni
 *  detalle de grading, y es la única forma de conseguirlos.
 */
function _rgrfTitular(a, dv) {
  const frases = Array.isArray(a?.lectura) ? a.lectura : [];
  const hayAudit = !!dv?.quality_classification;
  const boton = hayAudit
    ? `<button class="btn btn-ghost btn-xs dv-titular-accion"
              onclick="_rgrfAuditQuality(event)" data-i18n-tip="tab2.re_analizar_5_10_min_util"><span data-icono="refrescar"></span> <span data-i18n="tab2.re_analizar"></span></button>`
    : `<button class="btn btn-primary btn-sm dv-titular-accion" data-analisis-extendido="1"
              onclick="_rgrfAuditQuality(event)" data-i18n-tip="tab2.analisis_extendido_combos_l8_l2_perfil"><span><span data-icono="lupaOnda"></span></span> <span data-i18n="tab2.analisis_rpu_luz_5_10_min"></span></button>`;
  if (!frases.length) {
    return `
      <section class="dv-block dv-titular">
        <div class="dv-titular-vacio">
          <span data-i18n="tab2.titular_sin_datos"></span>
          ${boton}
        </div>
      </section>`;
  }
  // El punto de color del veredicto se conserva porque es lo único de la
  // card que no era un número: dice de un vistazo si el máster es bueno.
  const color = ({ green: 'alta', yellow: 'media', red: 'mala' })[dv?.quality_verdict_color] || '';
  const filas = frases.map(f => `
      <div class="dv-titular-fila">
        <div class="dv-titular-rotulo">${escHtml(f.rotulo)}</div>
        <div class="dv-titular-cuerpo">
          <div class="dv-titular-texto">${escHtml(f.texto)}</div>
          ${f.conclusion
            ? `<div class="dv-titular-conclusion">${escHtml(f.conclusion)}</div>` : ''}
        </div>
      </div>`).join('');
  return `
    <section class="dv-block dv-titular${color ? ' dv-titular-' + color : ''}">
      ${color ? `<span class="punto-conf ${color} dv-titular-punto"></span>` : ''}
      ${filas}
      <div class="dv-titular-pie">${boton}</div>
    </section>`;
}

function _rgrfTablaDeNiveles(dv, hdr) {
  const n = (x) => (x || 0).toLocaleString(localeActual());
  const refs = dv?.l1_references || null;
  const stats = dv?.l1_stats || null;
  // El export de niveles del análisis básico corre sobre el RPU del sniff
  // (`extract-rpu --limit 720`), así que L3, L4, L9, L10 y L11 describen
  // los primeros 30 s. Lo que viene del análisis extendido es de la
  // película entera.
  const M = true, F = false;   // muestra · película
  // **«Ausente» sólo se puede decir si se ha mirado.** Los niveles que
  // salen del export del RPU (L3, L4, L10 y L254) dependen de que ese
  // export corriera: va en un `try` que no bloquea, y al fallar los
  // flags se quedan en su `False` por defecto. Sin distinguirlo, la
  // tabla afirmaría una ausencia comprobada sobre un análisis que no
  // llegó a ejecutarse — el defecto que tenía L254 cuando de verdad no
  // se medía.
  const medidos = !!dv?.niveles_medidos;
  const delExport = (presente) => (presente ? 'si' : (medidos ? 'no' : 'nm'));

  // Cada fila: [nivel, qué aporta, valor, estado, alcance]
  //   estado: 'si' | 'no' | 'nm'  (nm = no medido)
  const filas = [];
  const fila = (lvl, aporta, valor, estado, muestra) =>
    filas.push({ lvl, aporta, valor, estado, muestra });

  // ── L1 · brillo por frame ──────────────────────────────────────────
  if (stats && stats.peak) {
    fila('L1', tr('tab2.niv_l1'),
         tr('tab2.niv_l1_val', {pico: n(stats.peak), media: n(stats.avg_of_max || 0)}),
         'si', F);
  } else if (dv?.l1_max_cll) {
    fila('L1', tr('tab2.niv_l1'),
         tr('tab2.niv_l1_val_muestra', {pico: n(Math.round(dv.l1_max_cll))}), 'si', M);
  } else {
    fila('L1', tr('tab2.niv_l1'), '', dv?.has_l1 ? 'si' : 'no', M);
  }

  // ── L2 · los trims del disco ───────────────────────────────────────
  const l2c = dv?.quality_l2_unique_count || 0;
  const l2nits = refs?.l2_trim_targets_nits || [];
  if (l2c) {
    fila('L2', tr('tab2.niv_l2'),
         tr('tab2.niv_combos_objetivos',
            {combos: n(l2c), objetivos: (dv.quality_l2_target_pqs || []).length})
         + (l2nits.length
            ? ' · ' + tr('tab2.niv_l8_targets', {nits: l2nits.join(' · ')}) : ''),
         'si', F);
  } else {
    fila('L2', tr('tab2.niv_l2'), '', dv?.has_l2 ? 'si' : 'no', M);
  }

  // ── L3 · el que no se veía ─────────────────────────────────────────
  // Con el análisis extendido hay conteo; sin él, sólo presencia en los
  // primeros 30 s. Y **no entra en el veredicto**: sobre 40 bins acierta
  // el 57 %, azar, porque lo genera el análisis de Dolby.
  const l3c = dv?.quality_l3_unique_count || 0;
  if (l3c) {
    fila('L3', tr('tab2.niv_l3'), tr('tab2.niv_combos', {combos: n(l3c)}), 'si', F);
  } else {
    fila('L3', tr('tab2.niv_l3'), '', delExport(dv?.has_l3), M);
  }

  fila('L4', tr('tab2.niv_l4'), '', delExport(dv?.has_l4), M);

  // ── L5 · el encuadre ───────────────────────────────────────────────
  const zonas = refs?.l5_zones || [];
  if (zonas.length > 1) {
    fila('L5', tr('tab2.niv_l5'), tr('tab2.niv_l5_zonas', {zonas: zonas.length}), 'si', F);
  } else {
    const z = zonas[0] || dv || {};
    const t = z.top ?? dv?.l5_top ?? 0, b = z.bottom ?? dv?.l5_bottom ?? 0;
    const l = z.left ?? dv?.l5_left ?? 0, r = z.right ?? dv?.l5_right ?? 0;
    const hayBarras = (t || b || l || r);
    fila('L5', tr('tab2.niv_l5'),
         hayBarras ? tr('tab2.niv_l5_barras', {t, b, l, r}) : tr('tab2.niv_l5_sin_barras'),
         dv?.has_l5 || hayBarras ? 'si' : 'no', zonas.length ? F : M);
  }

  // ── L6 · lo que el máster declara ──────────────────────────────────
  const l6max = refs?.l6_master_max_nits || dv?.l6_max_cll || 0;
  fila('L6', tr('tab2.niv_l6'),
       l6max ? tr('tab2.niv_l6_val', {max: n(l6max)}) : '',
       (l6max || dv?.has_l6) ? 'si' : 'no', refs?.l6_master_max_nits ? F : M);

  // ── L8 · el que decide en CMv4.0 ───────────────────────────────────
  const l8c = dv?.quality_l8_unique_count || 0;
  const l8nits = (refs?.l8_trim_nits_full?.length ? refs.l8_trim_nits_full
                                                  : (dv?.l8_trim_nits || []));
  // Los objetivos van CON los combos: «para qué pantallas hay ajuste»
  // y «cuánto ajuste hay» son la misma pregunta partida en dos, y
  // estaban en dos bloques distintos con dos escalas distintas.
  const objetivos = l8nits.length
    ? ' · ' + tr('tab2.niv_l8_targets', {nits: l8nits.join(' · ')}) : '';
  if (l8c || dv?.quality_classification) {
    fila('L8', tr('tab2.niv_l8'),
         l8c ? tr('tab2.niv_l8_val',
                  {combos: n(l8c), delta: n(dv.quality_l8_max_delta || 0)}) + objetivos
             : tr('tab2.niv_l8_sin_trims'),
         l8c ? 'si' : 'no', F);
  } else {
    fila('L8', tr('tab2.niv_l8'), objetivos.replace(' · ', ''),
         dv?.has_l8 ? 'si' : 'no', M);
  }

  fila('L9', tr('tab2.niv_l9'), escHtml(dv?.l9_primaries || ''),
       dv?.has_l9 ? 'si' : 'no', M);
  fila('L10', tr('tab2.niv_l10'), escHtml(dv?.l10_primaries || ''),
       delExport(dv?.has_l10), M);
  fila('L11', tr('tab2.niv_l11'), escHtml(dv?.l11_content_type || ''),
       dv?.has_l11 ? 'si' : 'no', M);
  // **Se mide con `info -f`, no con `--levels`.** Se daba por no medible
  // porque `--levels` llega hasta `level11`; lo que sí lo trae es un
  // frame suelto, y eso cuesta segundos. Lo preguntó el usuario el
  // 2026-09-24: «¿por qué L254 sale siempre no medido si tenemos el
  // análisis extendido?».
  fila('L254', tr('tab2.niv_l254'), '', delExport(dv?.has_l254), M);

  const cuerpo = filas.map(f => `
      <tr class="dv-niv-${f.estado}">
        <td class="dv-niv-lvl">${f.lvl}</td>
        <td class="dv-niv-aporta">${escHtml(f.aporta)}</td>
        <td class="dv-niv-val">${
          f.estado === 'nm' ? `<span class="dv-niv-nm-txt" data-i18n="tab2.niv_no_medido"></span>`
          : f.estado === 'no' ? `<span class="dv-niv-no-txt" data-i18n="tab2.niv_ausente"></span>`
          : (f.valor || '<span class="dv-niv-si-txt" data-i18n="tab2.niv_presente"></span>')}</td>
        <td class="dv-niv-alc">${f.muestra === null ? '' : _rgrfAlcance(f.muestra)}</td>
      </tr>`).join('');

  return `
    <section class="dv-block">
      <h5 class="dv-block-title"><span data-i18n="tab2.niveles_del_rpu"></span>
        <span class="dv-block-sub" data-i18n="tab2.niveles_del_rpu_sub"></span>
      </h5>
      <table class="dv-niveles-tabla">
        <thead><tr>
          <th data-i18n="tab2.niv_col_nivel"></th>
          <th data-i18n="tab2.niv_col_aporta"></th>
          <th data-i18n="tab2.niv_col_este"></th>
          <th data-i18n="tab2.niv_col_alcance"></th>
        </tr></thead>
        <tbody>${cuerpo}</tbody>
      </table>
      ${l8nits.length ? `
        <div class="dv-viz-inline">
          <div class="dv-viz-caption"><span data-i18n="tab2.l8_pantallas"></span> ${
            _rgrfAlcance(!(refs?.l8_trim_nits_full?.length))}</div>
          ${_rgrfL8Svg(l8nits)}
        </div>` : ''}
    </section>`;
}

function _rgrfQualityAuditCard(dv, isV40) {
  const cls = dv?.quality_classification || '';
  const hasAudit = !!cls;

  if (!hasAudit) {
    // Estado "no auditado" — CTA
    const cmLabel = isV40 ? 'CMv4.0' : (dv?.cm_version ? dv.cm_version.toUpperCase() : 'CMv2.9');
    return `
      <section class="dv-block dv-quality-card dv-quality-empty">
        <div class="dv-quality-empty-icon"><span data-icono="lupaOnda"></span></div>
        <div class="dv-quality-empty-body">
          <div class="dv-quality-empty-title">${tr('tab2.analisis_rpu_luz_cmlabel', {cmlabel: cmLabel})}</div>
          <div class="dv-quality-empty-text">
            <span data-i18n-html="tab2.extrae_el_rpu_y_saca_dos_cosas"></span>
          </div>
          <button class="btn btn-primary btn-sm dv-quality-cta"
                  data-analisis-extendido="1"
                  onclick="_rgrfAuditQuality(event)">
            <span><span data-icono="lupaOnda"></span></span> <span data-i18n="tab2.analisis_rpu_luz_5_10_min"></span>
          </button>
          <div class="dv-quality-empty-hint" data-i18n="tab2.extraer_el_rpu_es_el_97"></div>
        </div>
      </section>`;
  }

  // Estado poblado
  // El punto va con la paleta del tema; un emoji de color no la hereda y a
  // 12 px se ve como un borrón.
  const colorMap = {
    green:  { badge: '<span class="punto-conf alta"></span>',  cls: 'dv-q-green' },
    yellow: { badge: '<span class="punto-conf media"></span>', cls: 'dv-q-yellow' },
    red:    { badge: '<span class="punto-conf mala"></span>',  cls: 'dv-q-red' },
    gray:   { badge: '<span class="punto-conf"></span>',       cls: 'dv-q-gray' },
  };
  const color = colorMap[dv.quality_verdict_color] || colorMap.gray;
  const verdict = dv.quality_verdict_text || '—';
  const tierLabel = dv.quality_tier_label || '';
  const reason = dv.quality_reason || dv.quality_tier_description || '';

  // **Los stats son los que APLICAN, y el que decide va marcado.**
  //
  // Eran cinco fijos, así que un RPU CMv2.9 enseñaba tres ceros grandes
  // —L8, L3 y «cobertura CMv4.0»— y el número que sostiene su veredicto
  // (los combos L2) iba en segunda posición sin distinguirse de ellos.
  // Con tres de cada cinco cifras a cero no hay nada que concluir.
  //
  // El criterio de «esto es CMv2.9» es el MISMO que usa el backend para
  // elegir el veredicto (`is_cmv29_only`), y a propósito: si divergieran,
  // la card explicaría con unos números un veredicto decidido con otros.
  const l8Count = dv.quality_l8_unique_count || 0;
  const l2Count = dv.quality_l2_unique_count || 0;
  const l3Count = dv.quality_l3_unique_count || 0;
  // La magnitud es lo que decide el veredicto; los combos solo dicen cuántos hay.
  const l8Delta = dv.quality_l8_max_delta || 0;
  const scenes = dv.quality_scene_cuts || 0;
  const totalFrames = dv.quality_total_frames_rpu || 0;
  const cmv40Frames = dv.quality_frames_with_cmv40 || 0;
  const cmv40Pct = totalFrames > 0 ? Math.round(cmv40Frames * 100 / totalFrames) : 0;
  const combosPerShot = scenes > 0 ? (l8Count / scenes).toFixed(2) : '—';
  const esCmv29 = cmv40Frames === 0 && l8Count === 0;
  const nQty = (n) => n.toLocaleString(localeActual());
  // El tooltip llega RESUELTO, no como clave: una clave interpolada en un
  // `data-i18n-tip` es indistinguible de un dato y acaba pintada en crudo
  // si alguien la escribe mal — hay dos guards que lo prohíben por eso.
  const stat = (valor, etiqueta, { clave = false, extra = '', tip = '' } = {}) => `
        <div class="dv-quality-stat${clave ? ' dv-quality-stat-clave' : ''}"${
          tip ? ` data-tooltip="${escHtml(tip)}"` : ''}>
          <div class="dv-quality-stat-value">${valor}</div>
          <div class="dv-quality-stat-label">${etiqueta}${extra}</div>
        </div>`;
  const statsHtml = esCmv29
    ? [
        stat(nQty(l2Count), '<span data-i18n="tab2.combos_l2_unicos"></span>',
             { clave: true, tip: tr('tab2.l2_decide_en_cmv29') }),
        stat(nQty((dv.quality_l2_target_pqs || []).length),
             '<span data-i18n="tab2.objetivos_de_trim"></span>',
             { tip: tr('tab2.objetivos_de_trim_tip') }),
        stat(nQty(scenes), '<span data-i18n="tab2.scene_cuts"></span>'),
        stat(nQty(totalFrames), '<span data-i18n="tab2.frames_analizados"></span>'),
      ].join('')
    : [
        stat(nQty(l8Count), '<span data-i18n="tab2.combos_l8_unicos"></span>',
             { clave: true, tip: tr('tab2.l8_maxdelta_que_es'),
               extra: l8Delta ? ` <span style="opacity:.7">· maxΔ ${nQty(l8Delta)}</span>` : '' }),
        stat(nQty(l2Count), '<span data-i18n="tab2.combos_l2_unicos"></span>'),
        stat(nQty(scenes), '<span data-i18n="tab2.scene_cuts"></span>',
             { extra: ` <span style="opacity:.6">(~${combosPerShot} L8/shot)</span>` }),
        stat(`${cmv40Pct}%`, '<span data-i18n="tab2.cobertura_cmv4_0"></span>'),
      ].join('');
  // **L3 no entra en el veredicto y por eso no es un stat.** Sobre 40 bins
  // medidos acierta el 57 % —azar— porque lo produce el análisis de Dolby,
  // no el colorista. Al lado de los que sí deciden se leía como si pesara
  // lo mismo. Queda como renglón informativo, que es lo que es.
  const l3Html = l3Count > 0 ? `
      <div class="dv-quality-nota" data-i18n-tip="tab2.l3_que_es">${
        escHtml(tr('tab2.l3_nota', {n: nQty(l3Count)}))}</div>` : '';

  return `
    <section class="dv-block dv-quality-card ${color.cls}">
      <div class="dv-quality-header">
        <div class="dv-quality-badge">${color.badge}</div>
        <div class="dv-quality-head-body">
          <div class="dv-quality-verdict">${escHtml(verdict)}</div>
          ${tierLabel ? `<div class="dv-quality-tier">${escHtml(tierLabel)}</div>` : ''}
        </div>
        <button class="btn btn-ghost btn-xs dv-quality-reaudit"
                onclick="_rgrfAuditQuality(event)" data-i18n-tip="tab2.re_analizar_5_10_min_util"><span data-icono="refrescar"></span> <span data-i18n="tab2.re_analizar"></span></button>
      </div>
      <div class="dv-quality-stats">${statsHtml}</div>
      ${l3Html}
      ${(dv.quality_provenance_hints?.length || 0) > 0 ? `
        <div class="dv-quality-hints">
          <div class="dv-quality-hints-label" data-i18n="tab2.procedencia_probable"></div>
          <ul class="dv-quality-hints-list">
            ${dv.quality_provenance_hints.map(h => `<li>${escHtml(h)}</li>`).join('')}
          </ul>
        </div>` : ''}
      ${reason ? `<details class="dv-quality-details">
        <summary data-i18n="tab2.detalle_tecnico"></summary>
        <div class="dv-quality-reason">${escHtml(reason)}</div>
      </details>` : ''}
    </section>`;
}

/**
 * Dispara la auditoría de calidad del MKV abierto. Igual patrón que
 * _rgrfAuditQuality: modal de progreso con steps + polling. Produce los DOS
 * análisis (combos L8/L2 y perfil de luminancia) con una sola extracción.
 */
/** Copia el perfil de luminancia a los campos planos que lee el render.
 *
 *  El backend lo manda agrupado en `dovi.light_profile`, porque sale del MISMO
 *  análisis que los campos `quality_*` y se cachea con ellos. El render lo lee
 *  plano (`per_scene_max_cll`, `l1_stats`, `l1_references`) desde cuando eran
 *  dos análisis distintos con dos endpoints. Se mapea en UN sitio en vez de
 *  tocar las diez lecturas del render. */
function _mkvAplicarPerfilLuminancia(dv) {
  const lp = dv && dv.light_profile;
  if (!lp || !Array.isArray(lp.per_scene_max_cll) || !lp.per_scene_max_cll.length) return false;
  dv.per_scene_max_cll  = lp.per_scene_max_cll;
  dv.per_scene_max_fall = lp.per_scene_max_fall || [];
  dv.per_scene_min      = lp.per_scene_min || [];
  dv.l1_stats           = lp.stats || null;
  dv.l1_references      = lp.references || null;
  return true;
}

/** La ruta con la que el backend identifica el MKV de un proyecto abierto.
 *
 *  Es la misma que `_rgrfAuditQuality` manda al encolar (`sobre` del trabajo),
 *  así que las dos puntas comparan lo mismo.
 */
function _mkvRutaAnalisis(proyecto) {
  if (!proyecto) return '';
  return proyecto.analysis?.file_path || proyecto.filePath
      || proyecto.analysis?.file_name || '';
}

/** Repinta los botones de análisis extendido según lo que haya en la cola.
 *
 *  No re-monta el panel: el panel se re-monta al terminar el análisis, y entre
 *  medias lo único que cambia es el rótulo del botón. Se dispara desde
 *  `alCambiarTrabajos`, o sea solo cuando el trabajo cambia de verdad.
 */
function _mkvPintarEstadoDeAnalisis() {
  const t = typeof trabajoSobre === 'function'
    ? trabajoSobre(_mkvRutaAnalisis(mkvProject)) : null;
  document.querySelectorAll('[data-analisis-extendido]').forEach(btn => {
    if (!btn.dataset.rotuloOriginal) btn.dataset.rotuloOriginal = btn.innerHTML;
    if (!t) {
      btn.innerHTML = btn.dataset.rotuloOriginal;
      btn.classList.remove('ocupado');
      btn.disabled = false;
      return;
    }
    btn.classList.add('ocupado');
    btn.disabled = false;   // sigue pulsable: abre el detalle
    btn.innerHTML = t.estado === 'corriendo'
      ? iconoDeEstado('corriendo', 'icono-chip-sm') + ' ' + tr('tab2.analizando')
      : iconoDeEstado('en_cola', 'icono-chip-sm') + ' ' + tr('tab2.en_cola_posicion', {posicion: t.posicion});
  });
}

/** Los análisis extendidos que hemos pedido: `audit_id` → ruta del MKV.
 *
 *  Existe para saber a qué panel aplicar el resultado cuando el trabajo
 *  termine, que puede ser cuarenta minutos después. **No** hay una «sesión»
 *  por lanzamiento con su poller y su POST abierto: eso era una sola,
 *  global, así que pedir un segundo análisis mientras el primero esperaba
 *  turno pisaba la del primero y su resultado acababa en el panel
 *  equivocado. Aquí caben todos los que haya en la cola.
 */
const _mkvAnalisisPedidos = new Map();
// Los que la columna ha llegado a enseñar. Sin esta marca, un trabajo recién
// encolado —que aún no aparece en el poll— se daría por desaparecido.
const _mkvAnalisisVistos = new Set();

async function _rgrfAuditQuality(evt) {
  const proyecto = mkvProject;
  if (!proyecto) return;
  const ruta = _mkvRutaAnalisis(proyecto);
  // Un trabajo ESPERANDO TURNO no está «activo», así que el guard del backend
  // no lo veía y el usuario podía volver a pedirlo: el rechazo llegaba en un
  // 409, que es la peor forma de enterarse. Aquí se le enseña el que ya hay,
  // que es lo que quería ver.
  const yaHay = typeof trabajoSobre === 'function' ? trabajoSobre(ruta) : null;
  if (yaHay) {
    showToast(yaHay.estado === 'corriendo'
      ? tr('tab2.el_analisis_rpu_luz_de_este')
      : tr('tab2.el_analisis_rpu_luz_de_este_2', {posicion: yaHay.posicion}),
      'info');
    abrirDetalleDeTrabajo(ruta);
    return;
  }
  // El POST responde al instante: el trabajo son ~10 min y puede tener por
  // delante un rip de 40, así que se encola y el resultado se recoge cuando
  // la columna diga que ha terminado (`_mkvRecogerAnalisis`). Antes este
  // fetch se quedaba abierto hasta una hora esperándolo.
  const r = await apiFetch('/api/mkv/quality-audit', {
    method: 'POST',
    body: JSON.stringify({ file_path: ruta }),
  });
  if (!r?.audit_id) return;      // el error ya lo ha contado `apiFetch`
  _mkvAnalisisPedidos.set(r.audit_id, ruta);
  // NO se abre el modal: el usuario pulsa «analizar», no «mírame analizar»,
  // y taparle el panel con un log que aún no tiene líneas es interrumpirle
  // para nada. El acuse es el toast y la entrada en la columna.
  await refrescarWorkbar();
  showToast(tr('tab2.analisis_rpu_luz_en_marcha_el') + ' '
            + tr('tab2.columna_de_trabajo'), 'success');
}

/** Recoge los análisis que ya no están ni corriendo ni en la cola.
 *
 *  Lo llama `alCambiarTrabajos`, o sea solo cuando el trabajo cambia de
 *  verdad. Que un `audit_id` desaparezca de las dos listas es la señal de que
 *  terminó — o de que lo sacaron de la fila, que se distingue mirando de
 *  quién es el estado.
 */
function _mkvRecogerAnalisis(st) {
  if (!_mkvAnalisisPedidos.size) return;
  const vivos = new Set([
    ...(st.activo ? [st.activo.id] : []),
    ...(st.cola || []).map(j => j.id),
  ]);
  for (const [auditId, ruta] of [..._mkvAnalisisPedidos]) {
    if (vivos.has(auditId)) { _mkvAnalisisVistos.add(auditId); continue; }
    if (!_mkvAnalisisVistos.has(auditId)) continue;
    _mkvAnalisisPedidos.delete(auditId);
    _mkvAnalisisVistos.delete(auditId);
    _mkvAplicarAnalisisTerminado(auditId, ruta);
  }
}

async function _mkvAplicarAnalisisTerminado(auditId, ruta) {
  const st = await apiFetch('/api/mkv/quality-audit/progress', { silent: true })
    .catch(() => null);
  // El estado es un singleton: describe al análisis que tiene la máquina. Si
  // no es el nuestro, este se retiró de la cola antes de empezar y no hay
  // nada que recoger — ni resultado ni error que contar.
  if (!st || st.audit_id !== auditId) return;
  if (typeof _trabajoModalUltimo !== 'undefined'
      && _trabajoModalUltimo && _trabajoModalUltimo.id === auditId) {
    cerrarModalDeTrabajo();
  }
  if (st.error) {
    const cancelado = st.step === 'cancelled' || /cancelad/i.test(st.error);
    showToast(cancelado ? tr('tab2.auditoria_cancelada') : tr('tab2.error_auditoria', {error: st.error}),
              cancelado ? 'info' : 'error', cancelado ? 3500 : 8000);
    return;
  }
  const data = st.result;
  if (!data?.quality_classification) return;
  // El backend acaba de persistir el bloque `quality` (y con él el perfil de
  // luminancia) en la caché: la tarjeta de la columna izquierda pasa de 📋 a
  // 🔬. Se refresca pase lo que pase con la pestaña, que puede haberse
  // cerrado durante los diez minutos.
  refrescarMkvRecientes();
  const proyecto = (openMkvProjects || []).find(p => _mkvRutaAnalisis(p) === ruta);
  if (!proyecto || !proyecto.analysis) {
    showToast(tr('tab2.el_mkv_se_cerro_durante_el'),
              'info');
    return;
  }
  if (!proyecto.analysis.dovi) proyecto.analysis.dovi = {};
  Object.assign(proyecto.analysis.dovi, data);
  // El mismo análisis trae el perfil de luminancia (comparte la extracción
  // del RPU, que es el ~97 % del coste). A los campos planos del render.
  const conPerfil = _mkvAplicarPerfilLuminancia(proyecto.analysis.dovi);
  // Solo se repinta el que se está viendo; el de otra sub-pestaña ya lleva el
  // dato en `analysis` y se pinta al cambiar a ella.
  if (proyecto === mkvProject) _renderMkvEditPanel(proyecto);
  showToast(
    tr('tab2.analisis_rpu_luz_completado', {quality_verdict_text: data.quality_verdict_text})
    + (conPerfil ? tr('tab2.perfil_de_luminancia_frames', {p1: (data.light_profile?.total_frames || 0).toLocaleString(localeActual())}) : ''),
    'success');
}

async function _mkvQualityCancel(auditId) {
  try {
    // Se manda el audit_id que se está cancelando: el backend ignora el
    // cancel si ya no coincide con el análisis activo (cancel obsoleto tras
    // relanzar). Sin esto, un cancel tardío de A mataba la auditoría nueva B.
    await apiFetch('/api/mkv/quality-audit/cancel', {
      method: 'POST', silent: true,
      body: JSON.stringify({ audit_id: auditId || null }),
    });
  } catch (_) {}
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
    tr('tab2.radiografia_dv_hdr', {file_name: a.file_name}),
    ``,
    `**Tamaño:** ${_fmtBytes(a.file_size_bytes)} · **Duración:** ${_fmtDuration(a.duration_seconds)}`,
    ``,
    `## 1. Identidad`,
    `- Profile: **${fmt(dv.profile)}${el}**`,
    `- CM version: **${fmt(dv.cm_version)}**`,
    `- Frames totales: ${fmt(realFrames?.toLocaleString(localeActual()))}`,
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
    tr('tab2.3_l1_dinamico'),
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
  showToast(ok ? tr('tab2.radiografia_copiada_como_markdown') : tr('tab1.no_se_pudo_copiar_al_portapapeles'), ok ? 'success' : 'error');
}

// `_rgrfAnalyzeLight` y los helpers `_dvLight*` vivían aquí: modal propio,
// polling propio, cancelación propia y teardown propio, ~370 líneas calcadas de
// la auditoría de calidad para un análisis que hacía EXACTAMENTE la misma
// extracción del RPU. Los dos botones se separaron porque cada uno era caro;
// ya no lo son por separado (la extracción es el ~97 % y ahora se comparte),
// así que hay un solo botón y un solo job: `_rgrfAuditQuality`.

// ── Render del panel de edición ──────────────────────────────────

function _renderMkvEditPanel(project = mkvProject) {
  if (!project) return;
  const pid = project.id;
  const a = project.analysis;
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
    mainVideo.bitrate_kbps ? `${mainVideo.bitrate_kbps.toLocaleString(localeActual())} kbps` : '',
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
    dvLevelsLine = lvls.length ? tr('comun.niveles_p1', {p1: lvls.join(' · ')}) : '';
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
      cmBadgeHtml = `<span style="display:inline-flex; align-items:center; gap:4px; padding:2px 9px; border-radius:10px; background:rgba(52,199,89,0.18); color:var(--green-text); font-size:11px; font-weight:700; letter-spacing:0.2px" data-i18n-tip="tab2.este_mkv_ya_tiene_cmv4_0"><span data-icono="check"></span> CMv4.0</span>`;
      // Los badges heuristicos de procedencia (nativo/retail/generado/incierto)
      // se reemplazaron por la tabla detallada "Radiografia DV+HDR" que muestra
      // los datos factuales sin interpretacion.
    } else if (isV29) {
      cmBadgeHtml = `<span style="display:inline-flex; align-items:center; gap:4px; padding:2px 9px; border-radius:10px; background:rgba(255,149,0,0.18); color:var(--orange-text); font-size:11px; font-weight:700; letter-spacing:0.2px" data-i18n-tip="tab2.este_mkv_esta_en_cmv2_9"><span data-icono="rayo"></span> CMv2.9</span>`;
      cmHintHtml = `<span style="color:var(--orange-text); font-size:11px; font-weight:500" data-i18n="tab2.upgradeable_a_cmv4_0_pestana_upgrade"></span>`;
    } else if (dv.cm_version) {
      cmBadgeHtml = `<span style="display:inline-flex; align-items:center; gap:4px; padding:2px 9px; border-radius:10px; background:rgba(142,142,147,0.20); color:var(--text-2); font-size:11px; font-weight:700">CM ${escHtml(dv.cm_version)}</span>`;
    }
  } else if (dvDetected) {
    // Se detecta DV por número de HEVC pero dovi_tool no corrió / falló
    dvProfileLine = a.has_fel ? tr('tab2.p7_fel_detectado_por_estructura') : (hasElByCount ? tr('tab2.p7_mel_detectado_por_estructura') : tr('tab2.dolby_vision_detectado'));
  }

  const panel = document.getElementById(`mkv-panel-${pid}`);
  if (!panel) return;
  panel.innerHTML = `
    <div class="project-panel-inner" style="max-width:900px; margin:0 auto; padding:24px 20px">

      <!-- Ficha TMDb (hidratada en async) -->
      <div id="mkv-edit-tmdb-card-${pid}" class="tmdb-card-slot"></div>

      <!-- Info del fichero (solo lectura) -->
      <div class="section-card">
        <div class="section-header">
          <div><div class="section-title"><span data-icono="caja"></span> <span data-i18n="tab2.fichero_mkv"></span></div></div>
          <button class="btn btn-ghost btn-xs" onclick="reanalyzeMkv()"
                  style="margin-left:auto; color:var(--text-2)" data-i18n-tip="tab2.invalida_el_cache_y_re_ejecuta"><span data-icono="refrescar"></span> <span data-i18n="tab2.re_analizar"></span></button>
        </div>
        <div class="section-body">
          <div style="font-weight:600; font-size:14px; margin-bottom:4px">${escHtml(a.file_name)}</div>
          <div style="font-size:12px; color:var(--text-2); display:flex; flex-wrap:wrap; gap:4px 14px; line-height:1.55">
            <span>${_fmtBytes(a.file_size_bytes)}</span>
            <span>${_fmtDuration(a.duration_seconds)}</span>
            <span>${tr('tab2.p1_audio_p2_subs_p3_capitulos', {p1: audioTracks.length, p2: subTracks.length, p3: a.chapters?.length || 0})}</span>
          </div>
        </div>
      </div>

      <!-- Vídeo: resumen compacto + bloque detallado HDR/DV inline -->
      ${mainVideo ? `
      <div class="section-card">
        <div class="section-header">
          <div style="flex:1">
            <div class="section-title"><span data-icono="cinta"></span> <span data-i18n="tab2.video"></span></div>
          </div>
          <div class="video-header-badges">
            ${hdrBadge ? `<span class="video-badge video-badge-hdr">${hdrBadge}</span>` : ''}
            ${dvDetected && dvProfileLine ? `<span class="video-badge video-badge-dv"><span data-icono="curva"></span> DV ${escHtml(dvProfileLine.replace('Profile ', 'P'))}</span>` : ''}
            ${cmBadgeHtml}
            ${cmHintHtml ? `<span class="video-hint">${cmHintHtml}</span>` : ''}
          </div>
        </div>
        <div class="section-body">
          <div class="video-summary-line">
            <strong>${escHtml(videoCodecLine)}</strong>
            ${elVideo ? `<span class="video-el">+EL ${escHtml(elVideo.codec || 'HEVC')} ${escHtml(elVideo.pixel_dimensions || '')}${elVideo.bitrate_kbps ? ' · ' + elVideo.bitrate_kbps.toLocaleString(localeActual()) + ' kbps' : ''}</span>` : ''}
          </div>
          ${dvDetected && dv ? _renderMkvDvRadiography(a, dv, mainVideo, elVideo) : (dvDetected && !dv ? `<div style="font-size:11px; color:var(--text-3); font-style:italic; margin-top:6px" data-i18n="tab2.rpu_no_analizado_en_detalle_dovi"></div>` : '')}
        </div>
      </div>` : ''}

      <!-- Pistas de Audio -->
      <div class="section-card">
        <div class="section-header">
          <div><div class="section-title"><span data-icono="grafico"></span> <span data-i18n="tab2.pistas_de_audio"></span> <span style="font-weight:400; color:var(--text-3); font-size:11px">(${audioTracks.length})</span></div>
          <div class="section-subtitle" data-i18n="tab2.edita_nombres_y_flag_default"></div></div>
        </div>
        <div class="section-body">
          <ul class="track-list" id="mkv-audio-list-${pid}"></ul>
        </div>
      </div>

      <!-- Pistas de Subtítulos -->
      <div class="section-card">
        <div class="section-header">
          <div><div class="section-title"><span data-icono="etiqueta"></span> <span data-i18n="tab2.pistas_de_subtitulos"></span> <span style="font-weight:400; color:var(--text-3); font-size:11px">(${subTracks.length})</span></div>
          <div class="section-subtitle" data-i18n="tab2.edita_nombres_flags_default_y_forzado"></div></div>
        </div>
        <div class="section-body">
          <ul class="track-list" id="mkv-sub-list-${pid}"></ul>
        </div>
      </div>

      <!-- Capítulos -->
      <div class="section-card">
        <div class="section-header">
          <div><div class="section-title"><span data-icono="libro"></span> <span data-i18n="core.capitulos"></span></div>
          <div class="section-subtitle" data-i18n="tab2.clic_en_la_barra_para_anadir"></div></div>
          <button class="btn btn-xs" id="mkv-chapters-generic-btn-${pid}" style="display:none; margin-left:auto"
            onclick="setMkvGenericChapterNames()"
            data-i18n-tip="core.reemplaza_todos_los_nombres_por_capitulo"><span data-icono="etiqueta"></span> <span data-i18n="core.nombres_genericos"></span></button>
        </div>
        <div class="section-body">
          <div id="mkv-chapters-banner-${pid}" class="banner info" style="display:none">
            <span class="banner-icon" id="mkv-chapters-icon-${pid}"><span data-icono="disco"></span></span>
            <span id="mkv-chapters-text-${pid}"></span>
            <button class="btn btn-xs" id="mkv-chapters-autogen-btn-${pid}" style="display:none; margin-left:auto"
              onclick="generateMkvAutoChapters()"
              data-i18n-tip="tab2.genera_capitulo_01_02_03_cada"><span data-icono="portapapeles"></span> <span data-i18n="tab2.generar_cada_10_min"></span></button>
          </div>
          <div id="mkv-chapter-timeline-wrap-${pid}" class="chapter-timeline-wrap"
            onclick="onMkvTimelineClick(event)"
            onmousemove="onMkvTimelineHover(event)"
            onmouseleave="onMkvTimelineLeave()">
            <div class="chapter-timeline-track"></div>
            <div class="timeline-marks" id="mkv-timeline-marks-${pid}"></div>
            <div class="timeline-cursor" id="mkv-timeline-cursor-${pid}" style="display:none"></div>
          </div>
          <div id="mkv-chapters-list-${pid}" class="chapter-list"></div>
        </div>
      </div>

      <!-- Barra de botones -->
      <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:20px; padding-bottom:12px">
        <button class="btn btn-ghost btn-md" onclick="showRawMkvData()"
          style="color:var(--text-2); margin-right:auto" data-i18n-tip="tab2.ver_datos_crudos_del_analisis_mkvmerge"><span data-icono="lupaOnda"></span> <span data-i18n="tab2.datos_mkv"></span></button>
        <button class="btn btn-ghost btn-md" onclick="undoMkvEdits()"
          style="color:var(--text-2)" data-i18n-tip="tab2.revertir_todos_los_cambios_al_estado"><span data-icono="deshacer"></span> <span data-i18n="tab2.deshacer_cambios"></span></button>
        <button class="btn btn-ghost btn-md" onclick="closeMkvEditor()"
          style="color:var(--red)" data-i18n-tip="tab2.cerrar_el_editor"><span data-icono="cruz"></span> <span data-i18n="ui.cerrar"></span></button>
        <button class="btn btn-primary btn-md" onclick="applyMkvEdits()" data-i18n-tip="tab2.aplica_todos_los_cambios_al_mkv"><span data-icono="check"></span> <span data-i18n="tab2.aplicar_cambios"></span></button>
      </div>
    </div>`;

  // Hidratar ficha TMDb (async, no bloquea el render de pistas)
  hydrateTmdbCard(`mkv-edit-tmdb-card-${pid}`, project.fileName || a.file_name);

  _renderMkvTracks(project);
  _renderMkvChapters(project);
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
      lines.push(`<span style="color:#7cc4ff"><span data-i18n="tab2.peak"></span></span> ${v.toLocaleString(localeActual())} nits`);
      if (av != null) lines.push(`<span style="color:#86efac"><span data-i18n="tab2.avg"></span></span> ${av.toLocaleString(localeActual())} nits`);
      if (mn != null) lines.push(`<span style="color:#cbd5e1"><span data-i18n="tab2.min"></span></span> ${mn.toLocaleString(localeActual())} nits`);
      if (dur > 0) lines.push(`<span style="color:var(--dv-text-4)">@</span> ${_rgrfFmtTime(t)}`);
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

function _renderMkvTracks(project = mkvProject) {
  if (!project) return;
  const a = project.analysis;
  const audioList = _mkvEl('mkv-audio-list', project.id);
  const subList   = _mkvEl('mkv-sub-list', project.id);
  if (!audioList || !subList) return;

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
      ? `<span style="font-size:10px; padding:1px 6px; border-radius:8px; background:${t.compression_mode.toLowerCase().includes('lossless') ? 'rgba(52,199,89,0.15)' : 'rgba(142,142,147,0.18)'}; color:${t.compression_mode.toLowerCase().includes('lossless') ? 'var(--green-text)' : 'var(--text-2)'}; font-weight:600; margin-left:4px">${escHtml(t.compression_mode)}</span>`
      : '';
    // Info visible (no solo tooltip) — todo lo que aporta
    const desc = [
      codecPretty,
      channelsPretty,
      t.channel_layout ? escHtml(t.channel_layout) : '',
      t.sample_rate ? `${t.sample_rate/1000} kHz` : '',
      t.bitrate_kbps ? `${t.bitrate_kbps.toLocaleString(localeActual())} kbps` : '',
    ].filter(Boolean).join(' · ');
    const def = t.flag_default ? ' active-default' : '';
    const tooltip = [
      tr('tab2.codec_tecnico', {codec: t.codec}),
      t.format_commercial ? tr('tab2.codec_comercial_p1', {p1: t.format_commercial}) : null,
      tr('comun.idioma_p1_p2', {p1: t.language || '—', p2: langName}),
      chCount ? tr('tab2.canales_p1_p2', {p1: chCount, p2: channelsPretty}) : null,
      t.channel_layout ? `Layout: ${t.channel_layout}` : null,
      t.sample_rate ? `Sample rate: ${t.sample_rate/1000} kHz` : null,
      t.bitrate_kbps ? `Bitrate: ${t.bitrate_kbps.toLocaleString(localeActual())} kbps` : null,
      t.compression_mode ? tr('tab1.compresion', {compression_mode: t.compression_mode}) : null,
      tr('tab2.track_id_p1', {p1: t.id}),
    ].filter(Boolean).join('\n');
    const li = document.createElement('li');
    li.className = 'track-item';
    li.dataset.trackId = t.id;
    li.innerHTML = `
      <span class="track-type-icon" data-tooltip="${escHtml(tooltip)}"><span data-icono="grafico"></span></span>
      <div class="track-main">
        <span class="track-edit-icon"><span data-icono="lapiz"></span></span>
        <input class="track-label-input" type="text"
          value="${escHtml(t.name || '')}"
          placeholder="${escHtml(langName + ' ' + codecPretty)}"
          onchange="onMkvTrackEdit(${t.id}, 'name', this.value)" data-i18n-tip="tab2.nombre_de_la_pista_en_el">
        <span class="track-raw">${escHtml(langName)} · ${desc}${compressionPill}</span>
      </div>
      <div class="track-flags">
        <button class="flag-pill${def}" onclick="onMkvTrackFlag(${t.id}, 'default', 'audio')" data-i18n="tab1.def" data-i18n-tip="tab2.flag_default_pista_seleccionada_por_defecto"></button>
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
    let forcedSource = t.flag_forced ? tr('tab2.flag_del_mkv') : '';
    if (!t.flag_forced) {
      if (packets > 0 && packets < 500) {
        derivedForced = true;
        forcedSource = tr('tab2.n_paquetes_forzado_tipico', {n: packets});
      } else if (packets === 0 && t.bitrate_kbps > 0 && t.bitrate_kbps < 3) {
        derivedForced = true;
        forcedSource = tr('tab2.bitrate_n_kbps_forzado_tipico', {n: t.bitrate_kbps});
      }
    }
    const flagForcedLit = t.flag_forced;
    const def = t.flag_default ? ' active-default' : '';
    const frc = flagForcedLit ? ' active-forced' : '';
    const forcedLabel = derivedForced ? tr('tab2.forzados') : tr('tab2.completos');
    // Anotación cuando la clasificación viene inferida del volumen, no del flag
    const inferredMark = (derivedForced && !flagForcedLit) ? ' <span style="color:var(--orange); font-size:10px; font-weight:600" data-i18n-tip="tab2.clasificacion_inferida_por_volumen"><span data-icono="info"></span> inferido</span>' : '';

    // Info visible: codec + resolución + paq. + bitrate + tipo
    const pktTag = packets > 0 ? `${packets.toLocaleString(localeActual())} paq.` : '';
    const desc = [
      codecPretty,
      t.pixel_dimensions ? escHtml(t.pixel_dimensions) : '',
      pktTag,
      t.bitrate_kbps ? `${t.bitrate_kbps.toLocaleString(localeActual())} kbps` : '',
      forcedLabel,
    ].filter(Boolean).join(' · ');
    const tooltip = [
      `Codec: ${codecRaw || 'PGS'}`,
      tr('comun.idioma_p1_p2', {p1: t.language || '—', p2: langName}),
      tr('comun.tipo_p1', {p1: forcedLabel + (forcedSource ? ` (${forcedSource})` : '')}),
      t.pixel_dimensions ? tr('tab2.resolucion_bitmap', {pixel_dimensions: t.pixel_dimensions}) : null,
      packets > 0 ? tr('comun.paquetes_pes_p1_ffprobe', {p1: packets.toLocaleString(localeActual())}) : null,
      t.bitrate_kbps ? `Bitrate: ${t.bitrate_kbps.toLocaleString(localeActual())} kbps` : null,
      tr('tab2.track_id_p1', {p1: t.id}),
    ].filter(Boolean).join('\n');
    const li = document.createElement('li');
    li.className = 'track-item';
    li.dataset.trackId = t.id;
    li.innerHTML = `
      <span class="track-type-icon" data-tooltip="${escHtml(tooltip)}"><span data-icono="etiqueta"></span></span>
      <div class="track-main">
        <span class="track-edit-icon"><span data-icono="lapiz"></span></span>
        <input class="track-label-input" type="text"
          value="${escHtml(t.name || '')}"
          placeholder="${escHtml(langName + ' ' + forcedLabel + ' (' + codecPretty + ')')}"
          onchange="onMkvTrackEdit(${t.id}, 'name', this.value)" data-i18n-tip="tab2.nombre_de_la_pista_en_el">
        <span class="track-raw">${escHtml(langName)} · ${desc}${inferredMark}</span>
      </div>
      <div class="track-flags">
        <button class="flag-pill${def}" onclick="onMkvTrackFlag(${t.id}, 'default', 'subtitles')" data-i18n="tab1.def" data-i18n-tip="tab2.flag_default_subtitulo_seleccionado_por_defecto"></button>
        <button class="flag-pill${frc}" onclick="onMkvTrackFlag(${t.id}, 'forced', 'subtitles')" data-i18n="tab1.frc" data-i18n-tip="tab2.flag_forced_subtitulos_forzados_para_dialogos"></button>
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

function _renderMkvChapters(project = mkvProject) {
  if (!project) return;
  const a = project.analysis;
  const banner = _mkvEl('mkv-chapters-banner', project.id);
  const text   = _mkvEl('mkv-chapters-text', project.id);
  const autogenBtn = _mkvEl('mkv-chapters-autogen-btn', project.id);

  if (a.chapters.length > 0) {
    if (banner) { banner.style.display = 'flex'; banner.className = 'banner info'; }
    if (text) text.textContent = tr('tab2.capitulos', {p1: a.chapters.length});
    if (autogenBtn) autogenBtn.style.display = 'none';
  } else {
    if (banner) { banner.style.display = 'flex'; banner.className = 'banner warning'; }
    if (text) text.textContent = tr('tab2.sin_capitulos_en_este_mkv');
    // Botón tr('tab2.generar_cada_10_min') visible sólo si la duración permite al menos
    // un capítulo (necesita > 10 min de duración total).
    if (autogenBtn) {
      const dur = a.duration_seconds || 0;
      autogenBtn.style.display = (dur > 600) ? '' : 'none';
    }
  }

  // Botón nombres genéricos: visible solo si algún capítulo tiene nombre custom
  const genericBtn = _mkvEl('mkv-chapters-generic-btn', project.id);
  if (genericBtn) {
    const hasCustomNames = a.chapters.some(ch => ch.name_custom);
    genericBtn.style.display = hasCustomNames ? '' : 'none';
  }

  _renderMkvChapterMarks(project);
  _renderMkvChapterList(project);
}

/**
 * Genera capítulos automáticos cada 10 min desde el minuto 10. Mismo
 * algoritmo que `generate_auto_chapters` del backend (phases/phase_b.py)
 * que se usa en Tab 1 cuando el disco no trae capítulos. Marca el proyecto
 * dirty para que aparezca el botón tr('tab2.aplicar_cambios') (el backend escribe
 * los capítulos via mkvpropedit con --chapters).
 */
function generateMkvAutoChapters() {
  if (!mkvProject) return;
  const a = mkvProject.analysis;
  const dur = a.duration_seconds || 0;
  if (dur <= 600) {
    showToast(tr('tab2.la_duracion_del_mkv_es_menor'), 'warning');
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
      name: tr('tab1.capitulo', {p1: String(num).padStart(2, '0')}),
      name_custom: false,
    });
    t += interval;
    num += 1;
  }
  a.chapters = chapters;
  _mkvMarkDirty();
  _renderMkvChapters();
  showToast(tr('tab2.capitulos_generados_pulsa_aplicar_cambios_para', {p1: chapters.length}), 'success');
}

function _renderMkvChapterMarks(project = mkvProject) {
  if (!project) return;
  const a = project.analysis;
  const container = _mkvEl('mkv-timeline-marks', project.id);
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

function _renderMkvChapterList(project = mkvProject) {
  if (!project) return;
  const a = project.analysis;
  const container = _mkvEl('mkv-chapters-list', project.id);
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
      <button class="btn btn-icon" onclick="deleteMkvChapter(${idx})" data-i18n-tip="tab2.eliminar_capitulo"><span data-icono="cruz"></span></button>`;
    container.appendChild(row);
  });
}

// ── Track editing ────────────────────────────────────────────────

function onMkvTrackEdit(trackId, field, value) {
  if (!mkvProject) return;
  const track = mkvProject.analysis.tracks.find(t => t.id === trackId);
  if (track) track[field] = value;
  _mkvMarkDirty();
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

  _mkvMarkDirty();
  _renderMkvTracks();
}

// ── Chapter editing ──────────────────────────────────────────────

function onMkvTimelineClick(e) {
  if (!mkvProject) return;
  const duration = mkvProject.analysis.duration_seconds;
  if (!duration) return;

  const wrap = _mkvEl('mkv-chapter-timeline-wrap');
  if (!wrap) return;
  const rect = wrap.getBoundingClientRect();
  const pct  = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const secs = pct * duration;

  mkvProject.analysis.chapters.push({
    number: 0, timestamp: secsToTs(secs), name: '', name_custom: false,
  });
  _renumberMkvChapters();
  _renderMkvChapters();
  _mkvMarkDirty();
}

function onMkvTimelineHover(e) {
  if (!mkvProject) return;
  const duration = mkvProject.analysis.duration_seconds;
  if (!duration) return;
  const wrap  = _mkvEl('mkv-chapter-timeline-wrap');
  if (!wrap) return;
  const rect  = wrap.getBoundingClientRect();
  const pct   = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const label = _mkvEl('mkv-timeline-cursor');
  if (label) {
    label.style.display = '';
    label.style.left = `${e.clientX - rect.left}px`;
    label.textContent = secsToTs(pct * duration);
  }
}

function onMkvTimelineLeave() {
  const el = _mkvEl('mkv-timeline-cursor');
  if (el) el.style.display = 'none';
}

function deleteMkvChapter(idx) {
  if (!mkvProject) return;
  mkvProject.analysis.chapters.splice(idx, 1);
  _renumberMkvChapters();
  _renderMkvChapters();
  _mkvMarkDirty();
}

function onMkvChapterTsChange(idx, value) {
  if (!mkvProject) return;
  mkvProject.analysis.chapters[idx].timestamp = value;
  _renumberMkvChapters();
  _renderMkvChapters();
  _mkvMarkDirty();
}

function onMkvChapterNameChange(idx, value) {
  if (!mkvProject) return;
  mkvProject.analysis.chapters[idx].name = value;
  mkvProject.analysis.chapters[idx].name_custom = value.trim() !== '';
  _mkvMarkDirty();
  // Actualizar visibilidad del botón tr('core.nombres_genericos')
  const genericBtn = _mkvEl('mkv-chapters-generic-btn');
  if (genericBtn) {
    const hasCustom = mkvProject.analysis.chapters.some(ch => ch.name_custom);
    genericBtn.style.display = hasCustom ? '' : 'none';
  }
}

function startMkvChapterDrag(_e, markEl, idx) {
  if (!mkvProject) return;
  const duration = mkvProject.analysis.duration_seconds;
  if (!duration) return;
  const wrap = _mkvEl('mkv-chapter-timeline-wrap');
  if (!wrap) return;
  let dragged = false;

  markEl.classList.add('selected');
  document.body.style.cursor = 'grabbing';

  const marksEl = _mkvEl('mkv-timeline-marks');
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
      _mkvMarkDirty();
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
    if (!ch.name_custom) ch.name = tr('tab1.capitulo', {p1: String(ch.number).padStart(2, '0')});
  });
}

function setMkvGenericChapterNames() {
  if (!mkvProject?.analysis?.chapters) return;
  mkvProject.analysis.chapters.forEach((ch, i) => {
    ch.name = tr('tab1.capitulo', {p1: String(i + 1).padStart(2, '0')});
    ch.name_custom = false;
  });
  _mkvMarkDirty();
  _renderMkvChapters();
  showToast(tr('tab1.nombres_de_capitulo_reemplazados_por_genericos'), 'info');
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
      tr('tab2.mkv_en_biblioteca_read_only'),
      tr('tab2.este_mkv_esta_en_la_biblioteca') + ' ' +
      tr('tab2.la_app_copiara_el_fichero_gb', {p1: sizeGb.toFixed(1)}) + ' ' +
      tr('tab2.los_cambios_sobre_la_copia_la') + ' ' +
      tr('tab2.esto_puede_tardar_varios_minutos_para'),
      () => _doApplyMkvEdits(true),
      tr('tab2.copiar_y_aplicar'),
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

// Estado del job actual de apply. Permite que el botón tr('tab2.cancelar_copia')
// llame al endpoint del backend y que el flujo principal sepa que el
// usuario inició la cancelación (para mostrar mensaje correcto en lugar
// de tr('tab2.error_generico') cuando el POST devuelve 499).
let _mkvApplyUserCancelled = false;

async function _doApplyMkvEdits(copyToOutput) {
  const project = mkvProject;
  if (!project) return;
  const a = project.analysis;

  const audioEdits = a.tracks.filter(t => t.type === 'audio').map(t => ({
    id: t.id, name: t.name || '', flag_default: t.flag_default, flag_forced: t.flag_forced,
  }));
  const subEdits = a.tracks.filter(t => t.type === 'subtitles').map(t => ({
    id: t.id, name: t.name || '', flag_default: t.flag_default, flag_forced: t.flag_forced,
  }));

  const body = {
    file_path: project.filePath,
    title: null,
    audio_tracks: audioEdits,
    subtitle_tracks: subEdits,
    chapters: a.chapters,
    copy_to_output: copyToOutput,
  };

  // Mostrar modal de progreso
  // Este flujo tampoco monta su propio modal. Con copia, el progreso lo
  // enseña la columna de trabajo y el detalle es el modal común; sin copia,
  // `mkvpropedit` es O(1) y lo único que hace falta es contar el resultado.
  _mkvApplyUserCancelled = false;
  if (copyToOutput) {
    // Tampoco aquí: a la cola y de fondo. El acuse es la columna.
    await refrescarWorkbar();
    showToast(tr('tab2.copia_en_marcha_el_progreso_esta'),
              'success');
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
    // Con copia, la respuesta es solo el acuse de encolado; el resultado de
    // verdad llega por el estado del job.
    if (result?.queued) result = await _esperarCopiaEncolada();
  } finally {
    polling = false;
  }

  // Cancelación por el usuario: prima sobre cualquier otro estado.
  if (_mkvApplyUserCancelled) {
    cerrarModalDeTrabajo();
    showToast(tr('tab2.copia_cancelada_la_biblioteca_queda_intacta') + ' '
              + tr('tab2.parcial_se_borro'), 'warning', 8000);
    return;
  }

  if (!result?.ok) {
    // El detalle del fallo vive en el estado del job (lo enseña el modal
    // común) y en el historial; aquí basta con decirlo y no cerrar el modal,
    // para que se pueda leer.
    showToast(tr('tab2.error_al_aplicar_cambios', {p1: result?.error ? ': ' + result.error : ''}),
              'error', 8000);
    console.warn('[apply] salida de mkvpropedit:', result?.output);
    return;
  }

  // Si se copió a /mnt/output, actualizar el estado del proyecto al nuevo
  // path para que ediciones posteriores trabajen sobre la copia editable.
  let newFilePath = project.filePath;
  if (result.copied_from_library && result.new_file_path) {
    newFilePath = result.new_file_path;
    project.filePath = newFilePath;
    project.fileName = newFilePath.split('/').pop();
    _mkvRefreshSubTab(project);
    showToast(tr('tab2.mkv_copiado_a_output_con_tus', {filename: project.fileName}), 'success');
  }

  // Re-analizar para refrescar estado — usamos el path ABSOLUTO del MKV
  // (potencialmente actualizado tras copia). El backend valida que cae
  // bajo un root permitido (Library / Output).
  const fresh = await apiFetch('/api/mkv/analyze', {
    method: 'POST',
    body: JSON.stringify({ file_path: newFilePath || project.fileName }),
  }, API_FETCH_TIMEOUT_LARGO);

  if (fresh) {
    _mkvAplicarPerfilLuminancia(fresh && fresh.dovi);
    project.analysis = fresh;
    project.originalAnalysis = structuredClone(fresh);
    _mkvClearDirty(project);
    _renderMkvEditPanel(project);
  }

  // `apply` invalida la caché del MKV editado (mkvpropedit toca el primer MB,
  // así que el fingerprint cambia) y el re-análisis de arriba la reescribe —
  // con OTRA ruta si venía de la biblioteca. La columna izquierda sale de esa
  // caché, así que sin repintarla se queda con la entrada de antes.
  refrescarMkvRecientes();

  cerrarModalDeTrabajo();
  showToast(tr('tab2.cambios_aplicados_correctamente'), 'success');
}

/**
 * Botón "🛑 Cancelar copia": llama al backend para abortar la copia
 * cooperativamente. El thread de copia detecta el flag al inicio del
 * siguiente chunk (<1s) y borra el destino parcial. El POST de apply
 * eventualmente devuelve 499 — el flujo principal lo trata como
 * cancelación del usuario y muestra el mensaje correcto.
 */

async function cancelMkvApply() {
  // `_mkvApplyUserCancelled` es lo que hace que el flujo principal cuente
  // «cancelada» en vez de «error» al volver del poller.
  _mkvApplyUserCancelled = true;
  await apiFetch('/api/mkv/apply/cancel', { method: 'POST', silent: true });
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
//  COLUMNA IZQUIERDA — MKVs ANALIZADOS
// ═══════════════════════════════════════════════════════════════════
//
// Las tres pestañas comparten armazón: primario arriba, cabecera con
// contador, búsqueda, ordenación, filtros y una lista de tarjetas. Tab 2 lo
// tenía vacío porque **no persiste proyectos**: `openMkvProjects` vive en
// memoria y se va al recargar la página.
//
// Lo que sí sobrevive es la caché de análisis (`/config/mkv_audits/`), un
// fichero por MKV abierto alguna vez. Eso es lo que se lista, y por eso la
// columna no habla de «proyectos» sino de MKVs analizados: reabrir uno de
// ahí es instantáneo (cache hit), que es justo lo que la hace útil.
//
// Se reutilizan las clases de Tab 1 y Tab 3 tal cual (`.session-card`,
// `.sb-filter-pill`, `.sidebar-search-input`…). No hay helper común porque
// los tres renders comparten la FORMA pero no los datos —fases CMv4.0,
// estados de ejecución, análisis de MKV—, y el único trozo idéntico es el
// bucle que pinta la tarjeta: sacarlo pediría parametrizar icono, chips, meta
// y acciones, o sea reinventar una plantilla para tres usos.

/** Lo último que devolvió el endpoint. Se conserva ante un fallo de red. */
let _mkvRecientes = [];
/** Cuántos hay en la caché, que puede ser más de lo que el endpoint sirve. */
let _mkvRecientesTotal = 0;
let _mkvRecientesSort = 'analizado';
let _mkvRecientesSortAsc = false;   // lo natural en una lista de recientes
let _mkvRecientesFilter = 'all';
/** Ruta de la tarjeta desplegada (la que enseña sus acciones), o null. */
let _mkvRecienteSeleccion = null;
let _mkvRecientesDebounce = null;

/** Pide la lista y repinta. Silencioso: se llama al entrar en la pestaña y
 *  después de cada análisis, y un timeout puntual no es accionable. */
async function refrescarMkvRecientes() {
  const data = await apiFetch('/api/mkv/recientes', { silent: true });
  if (!data) {
    // NO vaciar la lista: machacarla con [] dejaría la columna en «0» y sería
    // indistinguible de «no has analizado nada», que es una conclusión mucho
    // peor que un dato viejo. Sin reintento automático a propósito — esto es
    // navegación, no un monitor: volver a entrar en la pestaña la repide.
    if (_mkvRecientes.length) _renderMkvRecientes();
    else _renderMkvRecientesErrorDeCarga();
    return;
  }
  _mkvRecientes = data.recientes || [];
  _mkvRecientesTotal = data.total || _mkvRecientes.length;
  _renderMkvRecientes();
}

function _renderMkvRecientesErrorDeCarga() {
  const lista = document.getElementById('mkv-recientes-list');
  if (!lista) return;
  const contador = document.getElementById('mkv-recientes-count');
  if (contador) contador.textContent = '—';
  lista.innerHTML = `
    <div class="empty-state" style="padding:24px 12px">
      <div class="empty-state-icon" data-icono="caja"></div>
      <div data-i18n="tab2.no_se_ha_podido_cargar_la"></div>
      <div class="empty-state-desc" style="margin-top:6px" data-i18n="tab2.los_analisis_siguen_guardados_abrir_un"></div>
      <button class="btn btn-ghost btn-xs" style="margin-top:10px"
        onclick="refrescarMkvRecientes()"><span data-icono="refrescar"></span> <span data-i18n="tab2.reintentar"></span></button>
    </div>`;
}

/** Búsqueda incremental con el mismo debounce que Tab 1 (150 ms): sin él se
 *  reconstruye el DOM en cada pulsación. */
function filtrarMkvRecientes() {
  clearTimeout(_mkvRecientesDebounce);
  _mkvRecientesDebounce = setTimeout(_renderMkvRecientes, 150);
}

function onMkvRecientesSortChange() {
  _mkvRecientesSort = document.getElementById('mkv-recientes-sort')?.value || 'analizado';
  // El nombre se lee de la A a la Z; la fecha y el tamaño, de mayor a menor.
  _mkvRecientesSortAsc = (_mkvRecientesSort === 'name');
  _actualizarBotonOrdenMkvRecientes();
  _renderMkvRecientes();
}

function toggleMkvRecientesSortDir() {
  _mkvRecientesSortAsc = !_mkvRecientesSortAsc;
  _actualizarBotonOrdenMkvRecientes();
  _renderMkvRecientes();
}

function _actualizarBotonOrdenMkvRecientes() {
  const btn = document.getElementById('mkv-recientes-sort-dir');
  if (btn) btn.innerHTML = icono(_mkvRecientesSortAsc ? 'flechaArriba' : 'flechaAbajo');
}

function onMkvRecientesFilterClick(btn) {
  _mkvRecientesFilter = btn.dataset.filter || 'all';
  _renderMkvRecientes();
}

/**
 * El estado de una entrada, en un solo sitio: decide el icono, el texto del
 * tooltip y la clave con la que filtran los pills.
 *
 * La caché caducada (ninguno de los dos bloques con la versión actual) cae en
 * `basico` a propósito: el pill 📋 dice «sin análisis extendido», que es
 * exactamente lo que es, y así ninguna tarjeta se queda sin pill que la
 * alcance.
 */
// Qué situaciones enseña cada pill. «Sin extendido» incluye la caché caducada
// a propósito: si exigiera tener el básico al día, esas tarjetas solo saldrían
// con «Todos» y quien filtra las daría por desaparecidas. Los tres pills más
// «Todos» cubren TODAS las tarjetas, que es la regla de esta columna.
const _MKV_PILL_SITUACIONES = {
  extendido: ['terminado'],
  basico:    ['preparando', 'caducado'],
  missing:   ['no_disponible'],
};

/** La situación de un MKV analizado, tal como la resolvió el servidor.
 *
 *  Se derivaba aquí, con palabras y colores propios: la misma idea que en las
 *  otras dos columnas —«esto ya está», «esto hay que rehacerlo»— se decía de
 *  otra forma según dónde la miraras. El respaldo mantiene lo de antes si
 *  alguna vez llega una tarjeta sin relato.
 */
function _situacionDeMkv(r) {
  return situacionDe(r)
      || (!r.existe ? 'no_disponible'
          : r.tiene_extendido ? 'terminado'
          : r.tiene_basico ? 'preparando' : 'caducado');
}

function _renderMkvRecientes() {
  const lista = document.getElementById('mkv-recientes-list');
  const contador = document.getElementById('mkv-recientes-count');
  if (!lista) return;

  // Los pills se re-marcan aquí y no solo en el click: `onSidebarFilterClick`
  // de Tab 1 quita `.active` a TODOS los `.sb-filter-pill` del documento, así
  // que tocar un filtro allí deja los de esta columna sin resaltar aunque el
  // filtro siga puesto. Repintar desde el estado lo corrige al entrar.
  document.querySelectorAll('#sidebar-tab-2 .sb-filter-pill').forEach(p =>
    p.classList.toggle('active', p.dataset.filter === _mkvRecientesFilter));

  const consulta = normalizeSearch(
    document.getElementById('mkv-recientes-search')?.value || '');
  let filtrada = _mkvRecientes.slice();
  if (consulta) {
    filtrada = filtrada.filter(r => normalizeSearch(r.nombre || '').includes(consulta));
  }
  if (_mkvRecientesFilter !== 'all') {
    const quiere = _MKV_PILL_SITUACIONES[_mkvRecientesFilter] || [];
    filtrada = filtrada.filter(r => quiere.includes(_situacionDeMkv(r)));
  }

  const dir = _mkvRecientesSortAsc ? 1 : -1;
  filtrada.sort((a, b) => {
    let cmp = 0;
    if (_mkvRecientesSort === 'name') {
      cmp = (a.nombre || '').localeCompare(b.nombre || '');
    } else if (_mkvRecientesSort === 'size') {
      cmp = (a.tamano_bytes || 0) - (b.tamano_bytes || 0);
    } else {
      cmp = new Date(a.analizado_en || 0).getTime()
          - new Date(b.analizado_en || 0).getTime();
    }
    return cmp * dir;
  });

  const filtrando = !!consulta || _mkvRecientesFilter !== 'all';
  if (contador) {
    contador.textContent = filtrando
      ? `${filtrada.length} / ${_mkvRecientes.length}`
      : filtrada.length;
  }

  if (!_mkvRecientes.length) {
    lista.innerHTML = `<div class="empty-state">
      <div class="empty-state-icon"><span data-icono="caja"></span></div>
      <div data-i18n="tab2.sin_mkvs_analizados"></div>
      <div style="font-size:11px;color:var(--text-3);margin-top:4px" data-i18n="tab2.pulsa_abrir_mkv_para_empezar"></div>
    </div>`;
    return;
  }
  if (!filtrada.length) {
    lista.innerHTML = `<div class="empty-state">
      <div class="empty-state-icon"><span data-icono="lupa"></span></div>
      <div data-i18n="tab1.sin_resultados"></div>
      <div style="font-size:11px;color:var(--text-3);margin-top:4px" data-i18n="tab1.prueba_con_otro_termino_o_filtro"></div>
    </div>`;
    return;
  }

  lista.innerHTML = '';
  filtrada.forEach(r => {
    const pintura = pinturaDeSituacion(r, _situacionDeMkv(r));
    const nombre = (r.nombre || '').replace(/\.mkv$/i, '');
    const abierto = !!openMkvProjects.find(p => _mkvRutaDe(p) === r.ruta);
    const seleccionada = _mkvRecienteSeleccion === r.ruta;

    const fecha = formatRelativeDate(r.analizado_en);
    const fechaLarga = r.analizado_en
      ? new Date(r.analizado_en).toLocaleString(localeActual(), {
          day: '2-digit', month: '2-digit', year: '2-digit',
          hour: '2-digit', minute: '2-digit' })
      : tr('tab2.fecha_desconocida');
    const tamano = r.tamano_bytes ? _fmtBytes(r.tamano_bytes) : '—';
    const duracion = r.duracion_segundos ? ` · ${_fmtDuration(r.duracion_segundos)}` : '';

    // Qué análisis tiene hecho. La etiqueta está siempre, apagada cuando no:
    // así la fila se lee igual esté como esté y la posición no se mueve.
    const chips = [
      { txt: 'RPU', tono: r.tiene_extendido ? 'verde' : '', apagado: !r.tiene_extendido,
        tooltip: r.tiene_extendido
          ? tr('tab2.combos_l8_l2_del_rpu_ya')
          : tr('tab2.sin_analisis_rpu_luz_el_boton') },
      { txt: 'Luz', tono: r.tiene_luminancia ? 'verde' : '', apagado: !r.tiene_luminancia,
        tooltip: r.tiene_luminancia
          ? tr('tab2.tiene_perfil_de_luminancia_sirve_para')
          : tr('tab2.sin_perfil_de_luminancia') },
    ];
    // Los tags del nombre dicen qué ES este MKV —si ya trae CMv4.0, de dónde
    // salió— y en esta pestaña eso es media pregunta contestada.
    nombreYTags(r.nombre).tags.forEach(t => chips.push({
      txt: t, tono: /CMv4|DV|FEL|MEL/i.test(t) ? 'morado' : 'teal' }));
    if (!r.existe) {
      chips.push({ txt: tr('tab2.no_encontrado'), tono: 'naranja', tooltip: r.ruta });
    }

    // Que un MKV tenga trabajo en marcha se ve AQUÍ, no solo en la columna:
    // sin esto una lista de veinte no dice cuál se está analizando.
    const insignia = typeof insigniaDeTrabajo === 'function'
      ? insigniaDeTrabajo(r.ruta) : '';

    const card = document.createElement('div');
    card.className = `session-card${seleccionada ? ' selected' : ''}`
                   + (r.existe ? '' : ' no-encontrado')
                   + (pintura.acento ? ' ' + pintura.acento : '');
    card.dataset.ruta = r.ruta;
    card.innerHTML = tarjetaDeProyecto({
      // El título de la ficha cuando se conoce; si no, el nombre sin tags.
      // El fichero completo, con su ruta, sigue en el tooltip.
      titulo: r.titulo || nombreYTags(r.nombre).titulo,
      tituloTooltip: r.ruta || nombre,
      sub: tamano + duracion,
      chips,
      estado: pintura.chip,
      // El «por qué» al tooltip, igual que en Tab 1: es lo que contesta «¿y
      // esto por qué está así?» sin tener que abrirlo.
      estadoTooltip: pintura.porque || pintura.rotulo,
      poster: r.poster || '',
      icono: typeof iconoDeTrabajo === 'function'
        ? iconoDeTrabajo('analisis_extendido', 'mkv') : '',
      meta: fecha,
      metaIso: r.analizado_en || '',
      metaTooltip: tr('tab2.analizado') + ' ' + fechaLarga,
      insignia,
      abierto,
      acciones: `
        ${r.existe
          ? `<button class="btn btn-primary btn-sm" data-abrir="1"
               data-i18n-tip="tab2.abrir_este_mkv_en_una_sub" data-i18n="tab1.abrir"></button>`
          : `<button class="btn btn-ghost btn-sm" disabled
               data-tooltip="${tr('tab2.no_esta_en_p1_el_analisis', {p1: escHtml(r.ruta)})}" data-i18n="tab2.fichero_no_encontrado"></button>`}
        <button class="btn btn-danger btn-sm" data-borrar="1" data-i18n="tab2.borrar" data-i18n-tip="tab2.quita_el_analisis_guardado_de_la"></button>`,
    });
    // Los handlers se cuelgan aquí y NO como `onclick="…('${r.ruta}')"` en la
    // plantilla: `escHtml` no escapa la comilla simple (no hace falta para un
    // atributo entre comillas dobles), así que un título como «Ocean's Eleven»
    // cerraría la cadena JS del atributo. El resultado sería un botón que no
    // hace nada, sin un solo error visible — el modo de fallo de siempre.
    // Tab 1 y Tab 3 sí interpolan, pero lo suyo es un id de sesión saneado.
    const fila = card.querySelector('.session-card-row');
    fila.onclick = () => _mkvToggleSeleccionReciente(r.ruta);
    fila.ondblclick = () => abrirMkvReciente(r.ruta);
    const abrir = card.querySelector('[data-abrir]');
    if (abrir) abrir.onclick = (ev) => { ev.stopPropagation(); abrirMkvReciente(r.ruta); };
    const borrar = card.querySelector('[data-borrar]');
    if (borrar) borrar.onclick = (ev) => { ev.stopPropagation(); _mkvBorrarReciente(r); };
    lista.appendChild(card);
  });

  // El endpoint recorta por arriba (ver TOPE_RECIENTES). Decirlo es más honesto
  // que dejar que el usuario deduzca que un MKV viejo tr('tab2.ya_no_esta_analizado').
  if (_mkvRecientesTotal > _mkvRecientes.length && !filtrando) {
    const pie = document.createElement('div');
    pie.className = 'empty-state-desc';
    pie.style.cssText = 'padding:8px 6px 0;text-align:center;font-size:10px';
    pie.textContent = tr('tab2.los_mas_recientes_de', {p1: _mkvRecientes.length, _mkvrecientestotal: _mkvRecientesTotal});
    lista.appendChild(pie);
  }
}

/** Despliega/repliega las acciones de una tarjeta, como en Tab 1 y Tab 3. */
function _mkvToggleSeleccionReciente(ruta) {
  _mkvRecienteSeleccion = (_mkvRecienteSeleccion === ruta) ? null : ruta;
  document.querySelectorAll('#mkv-recientes-list .session-card').forEach(card => {
    card.classList.toggle('selected', card.dataset.ruta === _mkvRecienteSeleccion);
  });
}

/**
 * Abre un MKV de la lista. Tres caminos, en este orden:
 *
 *   1. **Ya está abierto** → se activa su pestaña. Volver a analizarlo daría
 *      el mismo resultado (cache hit) pero abriendo el modal para nada.
 *   2. **El fichero no está** → se avisa y no se llama al backend, que
 *      respondería un 404 seco. El análisis se conserva igualmente.
 *   3. Si no, el flujo normal, el mismo que el file browser.
 */
function abrirMkvReciente(ruta) {
  const entrada = _mkvRecientes.find(r => r.ruta === ruta);
  const nombre = entrada?.nombre || (ruta || '').split('/').pop();

  const yaAbierto = openMkvProjects.find(p => _mkvRutaDe(p) === ruta);
  if (yaAbierto) {
    switchMkvSubTab(yaAbierto.id);
    return;
  }
  if (entrada && !entrada.existe) {
    showToast(tr('tab2.ya_no_esta_en_el_analisis', {nombre: nombre, ruta: ruta}), 'warning');
    return;
  }
  // El tope se comprueba ANTES de arrancar: el análisis puede tardar 1-3 min
  // y avisar al terminar sería cruel (mismo motivo que en openMkvPickerModal).
  if (openMkvProjects.length >= MAX_MKV_PROJECTS) {
    showToast(tr('tab2.maximo_mkv_abiertos_cierra_alguno_antes', {max_mkv_projects: MAX_MKV_PROJECTS}), 'warning');
    return;
  }
  _mkvAbrirRuta(ruta, nombre);
}

// ── Vistas de detalle para el modal de trabajo ───────────────────────────────

registrarDetalleDeTrabajo('analisis_extendido', async (a) => {
  let st = await apiFetch('/api/mkv/quality-audit/progress', { silent: true })
    .catch(() => null);
  // El estado es un singleton: describe al análisis que tiene la máquina. Con
  // varios esperando turno, el de la cola que se abra aquí enseñaría el log y
  // el fichero del que está corriendo — otra película.
  if (st && a && a.id && st.audit_id !== a.id) st = null;
  // La ficha ya la pidió el panel al abrir el MKV (`hydrateTmdbCard`), así
  // que aquí sale de su caché: no se vuelve a salir a la red por una cartela.
  // Sin estado vivo no hay nombre de fichero: la clave de este trabajo es su
  // `audit_id`, que no se le enseña a nadie. Antes que una cartela con
  // «aud-7f3» de título, ninguna — el subtítulo ya dice de qué MKV se habla.
  const nombre = st?.file_name || '';
  return {
    // El estado del análisis es un singleton: lo resetea el trabajo
    // siguiente, así que de una ejecución vieja no queda registro.
    sinDetalle: st ? '' : 'efimero',
    titulo: tr('tab2.analisis_rpu_luz_del_mkv'),
    sub: st?.file_name || a.que,
    cartel: nombre ? cartelDeTmdb(_tmdbCardCache?.get(nombre), nombre,
                                  icono('lupaOnda', 'ico-xl')) : null,
    // Dos pasos, no tres: ffmpeg y dovi_tool van conectados por un pipe, así
    // que extraer el HEVC y extraer el RPU son el mismo trabajo.
    pasosTitulo: tr('tab2.fases_del_analisis_rpu_luz'),
    pasos: [
      { icono: 'claqueta', titulo: tr('tab2.fase_a_extraccion_del_rpu'),
        sub: tr('tab2.ffmpeg_y_dovi_tool_encadenados_por') },
      { icono: 'grafico', titulo: tr('tab2.fase_b_combos_y_perfil_de'),
        sub: tr('tab2.export_por_niveles_combos_l8_l2') },
    ],
    conLog: true,
    cuerpo: _trabajoLogHTML(st?.log_lines),
  };
});

registrarDetalleDeTrabajo('copia_biblioteca', async (a) => {
  const st = await apiFetch('/api/mkv/apply/progress', { silent: true })
    .catch(() => null);
  // La copia no produce log: su detalle son los bytes.
  const gb = b => (b ? `${(b / 1e9).toFixed(1)} GB` : '—');
  const nombre = st?.file_name || '';
  return {
    sinDetalle: st ? '' : 'efimero',
    titulo: tr('tab2.copia_a_output'),
    sub: st?.file_name || a.que,
    cartel: nombre ? cartelDeTmdb(_tmdbCardCache?.get(nombre), nombre,
                                  icono('caja', 'ico-xl')) : null,
    pasosTitulo: tr('tab2.fases_de_la_copia'),
    pasos: [
      { icono: 'caja', titulo: tr('tab2.fase_a_copia_del_mkv'),
        sub: tr('tab2.de_la_biblioteca_solo_lectura_a') },
      { icono: 'etiqueta', titulo: tr('tab2.fase_b_escritura_de_metadatos'),
        sub: tr('tab2.mkvpropedit_sobre_la_copia_sin_remuxar') },
    ],
    conLog: false,
    cuerpo: _trabajoKvHTML([
      [tr('tab2.copiado'), `${gb(st?.bytes_copied)} de ${gb(st?.total_bytes)}`],
      [tr('tab2.fichero_de_origen'), st?.src_path || '—'],
      [tr('tab2.fichero_de_destino'), st?.dst_path || '—'],
      ['Error', st?.error || '—'],
    ]),
  };
});


/** Quita de la lista el análisis guardado de un MKV.
 *
 *  Borra la ENTRADA DE CACHÉ, no el fichero, y el diálogo lo dice: en las
 *  otras dos pestañas «Borrar» elimina un proyecto, y aquí no hay proyecto que
 *  borrar — lo que hay es un análisis que se puede rehacer abriendo el MKV
 *  otra vez.
 */
async function _mkvBorrarReciente(r) {
  showConfirm(
    tr('tab2.quitar_de_la_lista'),
    tr('tab2.se_borrara_el_analisis_guardado_de', {ruta: r.nombre || r.ruta})
    + ' ' + tr('tab2.no_se_toca_al_volver_a'),
    async () => {
      const resp = await apiFetch(
        `/api/mkv/cache-info?file_path=${encodeURIComponent(r.ruta)}`,
        { method: 'DELETE' });
      if (resp) showToast(tr('tab2.analisis_borrado_de_la_lista'), 'info');
      refrescarMkvRecientes();
    },
    tr('tab2.borrar_el_analisis'),
  );
}


// Cuando el trabajo cambia, esta pestaña repinta lo suyo: el rótulo del botón
// de análisis extendido y las insignias de la lista. Solo se dispara cuando el
// conjunto de trabajos cambia de verdad (ver `_workbarFirma`), no en cada tick.
alCambiarTrabajos((st) => {
  _mkvPintarEstadoDeAnalisis();
  _mkvRecogerAnalisis(st);
  if (document.getElementById('mkv-recientes-list')) _renderMkvRecientes();
});
