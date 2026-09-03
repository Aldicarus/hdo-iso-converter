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
  // Restaurar la cabecera al estado base: una apertura previa con match TMDb
  // pudo sustituir el poster por <img> y el título por el nombre de la peli.
  const posterEl = document.getElementById('mkv-analyze-modal-poster');
  if (posterEl) posterEl.innerHTML = '<span id="mkv-analyze-modal-icon">✏️</span>';
  const titleEl = document.getElementById('mkv-analyze-modal-title');
  if (titleEl) titleEl.textContent = 'Analizando MKV';

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
  // El perfil de luminancia llega dentro del análisis cuando está cacheado:
  // sale del mismo análisis extendido que los campos quality_*, así que al
  // reabrir el MKV el gráfico aparece poblado sin volver a analizar nada.
  _mkvAplicarPerfilLuminancia(analysis && analysis.dovi);
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
  // Curva de comparación (otro MKV del mismo título). NO se exige que tenga la
  // misma longitud que `series`: el eje X está normalizado a 0-100 % del
  // metraje, así que dos montajes con distinto número de frames se superponen
  // igual — y ver esa diferencia es justamente para lo que sirve la pantalla.
  const cmpSeries = Array.isArray(opts.compareSeries) && opts.compareSeries.length > 1
    ? opts.compareSeries : null;
  const cmpLabel = opts.compareLabel || 'Comparación';
  const refs = (opts.refs && typeof opts.refs === 'object') ? opts.refs : {};
  // El eje Y tiene que abarcar las DOS curvas o la de comparación se sale del
  // chart sin decirlo.
  const peakV = Math.max(...series, ...(cmpSeries || [0]));
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
  // La comparación se dibuja con su PROPIO reparto del eje X: si trae otro
  // número de cubos, mapearla con `xOf` (que asume la longitud de `series`)
  // la comprimiría contra el margen izquierdo.
  const cmpPath = cmpSeries
    ? _smoothPath(cmpSeries.map((v, i) => [
        padL + (i / (cmpSeries.length - 1)) * usableW, yOf(v)]))
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
  if (cmpPath) legendParts.push(`<span class="dv-sl-leg-item dashed" style="--c:#e11d48">${cmpLabel}</span>`);
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
      ${cmpPath ? `<path d="${cmpPath}" fill="none" stroke="#e11d48" stroke-width="1.8"
            stroke-dasharray="6,3" opacity="0.9" stroke-linejoin="round" stroke-linecap="round" />` : ''}
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
  // Esto solo aparece cuando el usuario ha pulsado "Análisis extendido"
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
  const cmp = hasLightProfile ? _mkvComparacion : null;
  const sparkOpts = hasLightProfile ? {
    avgSeries: dv.per_scene_max_fall && dv.per_scene_max_fall.length === dv.per_scene_max_cll.length
      ? dv.per_scene_max_fall : null,
    minSeries: dv.per_scene_min && dv.per_scene_min.length === dv.per_scene_max_cll.length
      ? dv.per_scene_min : null,
    refs: sparkRefs,
    compareSeries: cmp ? cmp.serie : null,
    compareLabel: cmp ? cmp.etiqueta : '',
  } : {};
  // Mini-card de stats (percentiles + clasificacion por brillo)
  const statsCardHtml = hasLightProfile && dv.l1_stats
    ? _rgrfL1StatsCard(dv.l1_stats, a.hdr)
    : '';
  const sparklineArea = hasLightProfile
    ? `<div class="dv-chart-large">${_rgrfSparklineSvg(dv.per_scene_max_cll, Math.max(...dv.per_scene_max_cll) + ' nits', a.duration_seconds, sparkOpts)}</div>
       ${_mkvTablaComparacionHtml(dv, a)}
       ${statsCardHtml}
       <div class="dv-chart-large">${_rgrfDistributionSvg(dv.per_scene_max_cll)}</div>`
    : `<div class="dv-chart-empty">
         <div class="dv-chart-empty-icon">📊</div>
         <div class="dv-chart-empty-text">Análisis per-escena no generado</div>
         <div class="dv-chart-empty-hint">Sale del <b>Análisis extendido</b>, junto a la auditoría de calidad: extraer el RPU es el ~97 % del trabajo y se hace una sola vez para los dos. ~5-10 min en UHD.</div>
       </div>`;
  // Un solo botón: el perfil sale del mismo análisis extendido que la
  // auditoría de calidad, compartiendo la extracción del RPU.
  const btnComparar = hasLightProfile
    ? (_mkvComparacion
       ? `<button class="btn btn-ghost btn-sm dv-chart-action" onclick="quitarComparacionLuminancia()" data-tooltip="Volver a ver solo este MKV"><span>✕</span> Quitar comparación</button>`
       : `<button class="btn btn-ghost btn-sm dv-chart-action" onclick="abrirComparadorLuminancia()" data-tooltip="Superponer la curva de otro MKV del mismo título — típicamente el mismo antes y después del upgrade a CMv4.0"><span>⚖️</span> Comparar con…</button>`)
    : '';
  const actionBtn = (hasLightProfile
    ? `<button class="btn btn-ghost btn-sm dv-chart-action" onclick="_rgrfAuditQuality(event)" data-tooltip="Re-analizar si el MKV cambió o mejoró el clasificador"><span>↻</span> Re-analizar</button>`
    : `<button class="btn btn-primary btn-sm dv-chart-action" onclick="_rgrfAuditQuality(event)" data-tooltip="Análisis extendido: combos L8/L2 + perfil de luminancia, en una sola pasada"><span>🔬</span> Análisis extendido</button>`) + btnComparar;
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
 *  - Sin datos (quality_classification vacío): CTA "Análisis extendido"
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
          <div class="dv-quality-empty-title">Análisis extendido ${cmLabel}</div>
          <div class="dv-quality-empty-text">
            Extrae el RPU completo del MKV y saca de él <b>dos cosas de una vez</b>:
            los combos L8/L2 clasificados (FULL / CORE+ / CORE / sintético), que
            dicen si el master es de referencia o generado algorítmicamente, y el
            <b>perfil de luminancia L1</b> frame a frame.
          </div>
          <button class="btn btn-primary btn-sm dv-quality-cta"
                  onclick="_rgrfAuditQuality(event)">
            <span>🔬</span> Análisis extendido (~5-10 min)
          </button>
          <div class="dv-quality-empty-hint">
            Extraer el RPU es el ~97 % del trabajo y se hace una sola vez para los
            dos · el MKV no se modifica · los intermedios se borran al terminar
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
                data-tooltip="Re-analizar (5-10 min). Útil si el clasificador mejoró o el MKV cambió.">↻ Re-analizar</button>
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

async function _rgrfAuditQuality(evt) {
  if (!mkvProject) return;
  // Guard anti-solapamiento (mismo patrón que luminancia, commit 4f5d9a8):
  // el estado del audit es un singleton global en el backend; lanzar un 2º
  // mientras hay uno activo pisaría ese estado y dejaría pollers cruzados.
  let prevAuditId = null;
  try {
    const cur = await apiFetch('/api/mkv/quality-audit/progress', { silent: true });
    if (cur && cur.active) {
      showToast('Ya hay una auditoría de calidad en curso — espera a que termine o cancélala', 'info');
      return;
    }
    // audit_id del audit ANTERIOR ya terminado: el backend retiene su
    // result/done hasta que NUESTRO POST resetee. Sin esto, el poller leía ese
    // estado viejo (active=false + result), creía que "ya terminó", abortaba
    // nuestro POST y aplicaba el resultado del audit anterior (mostraba la peli
    // equivocada en ~1s). Lo ignoramos hasta ver un audit_id nuevo.
    prevAuditId = (cur && cur.audit_id) || null;
  } catch (_) { /* si /progress falla seguimos: el guard 409 del backend es la red de seguridad */ }
  // MKV objetivo capturado AHORA: si el usuario abre otro MKV mientras corre
  // la auditoría, el resultado no debe aplicarse al proyecto equivocado.
  const targetFilePath = mkvProject.analysis.file_path || mkvProject.filePath || mkvProject.analysis.file_name;
  // request_id estable por lanzamiento: si el navegador/proxy re-envía el POST
  // largo (al perder foco / caer la conexión), reusa el MISMO body → el backend
  // lo dedup y NO arranca un audit duplicado.
  const requestId = (self.crypto && self.crypto.randomUUID)
    ? self.crypto.randomUUID()
    : (Date.now() + '-' + Math.random().toString(36).slice(2));
  const fileEl = document.getElementById('mkv-quality-modal-file');
  if (fileEl) fileEl.textContent = mkvProject.analysis.file_name;
  // Cabecera: restaurar icono base (una apertura previa con match TMDb pudo
  // dejar la cartela) antes de re-intentar la hidratación.
  const qPoster = document.getElementById('mkv-quality-modal-poster');
  if (qPoster) qPoster.innerHTML = '<span id="mkv-quality-modal-icon">🔬</span>';
  const qTitle = document.getElementById('mkv-quality-modal-title');
  if (qTitle) qTitle.textContent = 'Auditando calidad CMv4.0 / CMv2.9';
  _mkvQualityResetSteps();
  _mkvQualitySetProgress(0);
  _mkvQualitySetElapsed(0);
  const logEl = document.getElementById('mkv-quality-log');
  if (logEl) logEl.innerHTML = '';
  openModal('mkv-quality-modal');
  // Cartela + título TMDb en la cabecera (best-effort, en paralelo) — misma
  // ficha que los demás modales de análisis, para consistencia entre flujos.
  _hydrateModalWithTmdb({
    name: mkvProject.analysis.file_name,
    modalId: 'mkv-quality-modal',
    posterId: 'mkv-quality-modal-poster',
    titleId: 'mkv-quality-modal-title',
    subId: 'mkv-quality-modal-file',
    subText: mkvProject.analysis.file_name,
  });

  let lastLogCount = 0;
  let polling = true;
  // Sesión con scope LOCAL. window._mkvQualitySession sigue existiendo para
  // que el botón Cancelar lea el audit_id, pero el poller/finally/abort de
  // ESTA invocación operan sobre `session` local — así un audit nuevo que
  // tome el relevo no es pisado por el teardown del viejo (bug cancel+relanzar).
  const session = { ctrl: null, polledResult: null, cancelledByUser: false, auditId: null };
  window._mkvQualitySession = session;

  async function _pollLoop() {
    while (polling) {
      // Si otra auditoría tomó el relevo (usuario relanzó), este poller es
      // obsoleto: autodetenerse para no volcar log cruzado ni abortar el POST
      // del audit nuevo.
      if (window._mkvQualitySession !== session) { polling = false; return; }
      try {
        const st = await apiFetch('/api/mkv/quality-audit/progress', { silent: true });
        if (!polling || window._mkvQualitySession !== session) { polling = false; return; }
        // Mientras el state siga mostrando el audit ANTERIOR (aún no reseteado
        // por nuestro POST), ignorarlo — no es nuestro result.
        if (st && !(prevAuditId && st.audit_id === prevAuditId)) {
          if (st.audit_id) session.auditId = st.audit_id;
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
            session.polledResult = st.result || null;
            try { session.ctrl?.abort(); } catch (_) {}
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
      session.ctrl = ctrl;
      const POST_TIMEOUT_MS = 3600000;  // 1h
      const timer = setTimeout(() => ctrl.abort(), POST_TIMEOUT_MS);
      try {
        const resp = await fetch('/api/mkv/quality-audit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file_path: targetFilePath, request_id: requestId }),
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
      const polled = session.polledResult;
      if (polled && polled.quality_classification) {
        data = polled;
      } else {
        for (let i = 0; i < 20; i++) {
          await new Promise(r => setTimeout(r, 1500));
          const st = await apiFetch('/api/mkv/quality-audit/progress', { silent: true });
          // Ignorar el estado obsoleto del audit anterior (mismo motivo que el poller).
          if (st && prevAuditId && st.audit_id === prevAuditId) continue;
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
    // Si el usuario abrió otro MKV mientras corría la auditoría, NO aplicar el
    // resultado al proyecto equivocado. El backend ya lo cacheó bajo el path
    // correcto, así que al reabrir aquel MKV aparecerá poblado.
    const curFilePath = mkvProject.analysis.file_path || mkvProject.filePath || mkvProject.analysis.file_name;
    if (curFilePath !== targetFilePath) {
      closeModal('mkv-quality-modal');
      showToast('Auditoría completada para el MKV anterior (guardada en caché)', 'info');
      return;
    }
    if (!mkvProject.analysis.dovi) mkvProject.analysis.dovi = {};
    Object.assign(mkvProject.analysis.dovi, data);
    // El mismo análisis trae el perfil de luminancia (comparte la extracción
    // del RPU, que es el ~97 % del coste). A los campos planos del render.
    const conPerfil = _mkvAplicarPerfilLuminancia(mkvProject.analysis.dovi);
    await new Promise(r => setTimeout(r, 500));
    closeModal('mkv-quality-modal');
    _renderMkvEditPanel();
    showToast(
      `Análisis extendido completado — ${data.quality_verdict_text}`
      + (conPerfil ? ` · perfil de luminancia: ${(data.light_profile?.total_frames || 0).toLocaleString()} frames` : ''),
      'success');
  } catch (e) {
    if (session.cancelledByUser) {
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
    session.ctrl = null;
    session.polledResult = null;
    session.cancelledByUser = false;
    // Solo soltar la referencia global si seguimos siendo el audit activo —
    // si un audit nuevo ya tomó el relevo NO la tocamos (era el clobber del bug).
    if (window._mkvQualitySession === session) window._mkvQualitySession = null;
  }
}

async function _mkvQualityCancel() {
  const btn = document.getElementById('mkv-quality-cancel-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Cancelando…'; }
  const session = window._mkvQualitySession;
  if (session) session.cancelledByUser = true;
  try {
    // Mandamos el audit_id que ESTE modal está siguiendo: el backend ignora
    // el cancel si ya no coincide con el audit activo (cancel obsoleto tras
    // relanzar). Sin esto, un cancel tardío de A mataba la auditoría nueva B.
    await apiFetch('/api/mkv/quality-audit/cancel', {
      method: 'POST', silent: true,
      body: JSON.stringify({ audit_id: session?.auditId || null }),
    });
  } catch (_) {}
  try { session?.ctrl?.abort(); } catch (_) {}
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

// `_rgrfAnalyzeLight` y los helpers `_dvLight*` vivían aquí: modal propio,
// polling propio, cancelación propia y teardown propio, ~370 líneas calcadas de
// la auditoría de calidad para un análisis que hacía EXACTAMENTE la misma
// extracción del RPU. Los dos botones se separaron porque cada uno era caro;
// ya no lo son por separado (la extracción es el ~97 % y ahora se comparte),
// así que hay un solo botón y un solo job: `_rgrfAuditQuality`.

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
    _mkvAplicarPerfilLuminancia(fresh && fresh.dovi);
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
//  COMPARADOR A/B DEL PERFIL DE LUMINANCIA
// ═══════════════════════════════════════════════════════════════════
//
// Superpone la curva L1 de otro MKV sobre la del que está abierto. El caso
// para el que existe: el mismo título antes y después del upgrade a CMv4.0,
// para responder «¿mereció la pena?» con el grading delante en vez de con la
// clasificación de `classify_l8`, que es un proxy.
//
// Dos decisiones que conviene no deshacer:
//
// * **No lanza análisis.** El endpoint (`/api/mkv/light-profile-cached`) sólo
//   devuelve lo que YA está en `/config/mkv_audits/`. Extraer el RPU de un UHD
//   son ~10 min y eso no puede dispararse por elegir un fichero en un
//   navegador; si falta, se dice cuál falta.
// * **El eje X está normalizado a 0-100 % del metraje**, así que dos montajes
//   con distinto número de frames se superponen igual. Eso es deseado —es cómo
//   se ve un desfase— pero hace falta AVISAR de la diferencia de duración, o
//   el usuario compara dos cosas que no están alineadas creyendo que sí.

/** {serie, etiqueta, stats, duracion, fichero} del MKV con el que se compara. */
let _mkvComparacion = null;

/** Diferencia de duración a partir de la cual las curvas ya no son
 *  comparables sin avisar. 2 % de una peli de 2 h son ~2,5 min: eso ya no es
 *  un logo de estudio, es otro montaje. */
const _CMP_TOLERANCIA_DURACION = 0.02;

function abrirComparadorLuminancia() {
  openFileBrowser({
    title: 'Comparar el perfil de luminancia con…',
    subtitle: 'Normalmente, el mismo título antes o después del upgrade a CMv4.0',
    roots: [
      { key: 'library', label: 'Biblioteca', icon: '📚' },
      { key: 'output',  label: 'Output',     icon: '📦' },
    ],
    onSelect: (absPath) => _cargarComparacionLuminancia(absPath),
  });
}

async function _cargarComparacionLuminancia(ruta) {
  if (!ruta) return;
  if (mkvProject && ruta === mkvProject.filePath) {
    showToast('Ese es el MKV que ya tienes abierto', 'info');
    return;
  }
  try {
    const r = await apiFetch(
      `/api/mkv/light-profile-cached?file_path=${encodeURIComponent(ruta)}`);
    if (!r || !r.cached) {
      showToast(
        `Sin perfil que comparar — ${r?.reason || 'no analizado'}. ` +
        'Ábrelo en esta pestaña y lánzale el 🔬 Análisis extendido.',
        'info', 8000);
      return;
    }
    const perfil = r.light_profile || {};
    const serie = perfil.per_scene_max_cll;
    if (!Array.isArray(serie) || serie.length < 2) {
      showToast('El análisis de ese MKV no trae curva de luminancia', 'info');
      return;
    }
    _mkvComparacion = {
      serie,
      etiqueta: r.file_name || 'Comparación',
      stats: perfil.stats || null,
      duracion: r.duration_seconds || 0,
      fichero: ruta,
    };
    _renderMkvEditPanel();
    showToast(`⚖️ Comparando con ${r.file_name}`, 'success');
  } catch (e) {
    showToast(`No se pudo cargar la comparación: ${e.message}`, 'error', 6000);
  }
}

function quitarComparacionLuminancia() {
  _mkvComparacion = null;
  _renderMkvEditPanel();
}

/** Tabla de deltas entre el MKV abierto y el de comparación. */
function _mkvTablaComparacionHtml(dv, a) {
  const cmp = _mkvComparacion;
  if (!cmp) return '';
  const propias = dv.l1_stats || {};
  const otras = cmp.stats || {};
  const filas = [
    ['Peak', 'peak'], ['p99', 'p99'], ['p95', 'p95'],
    ['Mediana', 'p50'], ['Media de los picos', 'avg_of_max'],
  ];
  const celdas = filas.map(([etiq, clave]) => {
    const mia = propias[clave];
    const suya = otras[clave];
    if (mia == null || suya == null) return '';
    const d = mia - suya;
    // El signo se lee «este MKV respecto al de comparación».
    const signo = d > 0 ? 'up' : (d < 0 ? 'down' : 'flat');
    const pct = suya ? ` (${d >= 0 ? '+' : ''}${(d / suya * 100).toFixed(1)}%)` : '';
    return `<tr>
      <td>${etiq}</td>
      <td class="cmp-num">${mia} n</td>
      <td class="cmp-num">${suya} n</td>
      <td class="cmp-num cmp-${signo}">${d >= 0 ? '+' : ''}${d} n${pct}</td>
    </tr>`;
  }).join('');

  // El aviso que evita comparar dos montajes distintos creyendo que son el
  // mismo: el eje X va normalizado, así que la diferencia no se ve sola.
  const dMia = a.duration_seconds || 0;
  const dSuya = cmp.duracion || 0;
  let aviso = '';
  if (dMia && dSuya) {
    const rel = Math.abs(dMia - dSuya) / Math.max(dMia, dSuya);
    if (rel > _CMP_TOLERANCIA_DURACION) {
      aviso = `<div class="cmp-aviso">⚠️ Duran distinto (${_rgrfFmtTime(dMia)} vs
        ${_rgrfFmtTime(dSuya)}, ${(rel * 100).toFixed(1)}%). El eje X va normalizado al
        metraje, así que las dos curvas ocupan todo el ancho igualmente: pueden ser
        montajes distintos y estar comparando escenas que no se corresponden.</div>`;
    }
  } else {
    aviso = `<div class="cmp-aviso">ℹ︎ No se conoce la duración de uno de los dos,
      así que no se puede confirmar que sean el mismo montaje.</div>`;
  }

  return `<div class="dv-cmp-card">
    <div class="dv-cmp-head">⚖️ Comparación con <b>${cmp.etiqueta}</b></div>
    ${celdas ? `<table class="dv-cmp-table">
      <thead><tr><th></th><th>Este MKV</th><th>${cmp.etiqueta}</th><th>Δ</th></tr></thead>
      <tbody>${celdas}</tbody></table>` : ''}
    ${aviso}
  </div>`;
}
