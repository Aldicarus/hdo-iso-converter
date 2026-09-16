'use strict';
/**
 * cmv40_modals.js — Los modales de CMv4.0 que NO son el pipeline.
 *
 * La consulta rápida (mirar si un título tiene RPU sin crear proyecto), el
 * manual/ayuda con sus sección y sus enlaces hidratados desde el sheet y el
 * Drive, y la limpieza masiva de artefactos. Van aparte de `tab3.js` porque no
 * tocan una sesión: son consultas y operaciones en lote.
 */

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
  if (body) body.innerHTML = '<div class="cmv40-cleanup-loading"><span data-icono="reloj"></span> Escaneando proyectos…</div>';
  if (foot) foot.style.display = 'none';

  const data = await apiFetch('/api/cmv40/cleanup/preview');
  if (!data) return;
  if (!data.items || !data.items.length) {
    if (body) body.innerHTML = '<div class="cmv40-cleanup-empty">' + tr('cmv40_modals.no_hay_proyectos_cmv40_todavia') + '</div>';
    return;
  }
  if (data.deletable_count === 0) {
    if (body) {
      body.innerHTML = `
        <div class="cmv40-cleanup-empty">
          ${tr('cmv40_modals.p1_nada_que_limpiar_los_total', {p1: icono('check'), total_count: data.total_count})}
        </div>`;
    }
    return;
  }

  // Tabla con filas: checkbox, título, fase/estado, tamaño, motivo
  const rows = data.items.map((it) => {
    // Estado visual
    let stateBadge = '';
    if (it.state === 'running') stateBadge = '<span class="cleanup-state-pill running"><span data-icono="reloj"></span> ' + tr('workbar.en_curso') + '</span>';
    else if (it.state === 'archived') stateBadge = '<span class="cleanup-state-pill archived"><span data-icono="archivador"></span> Archivado</span>';
    else if (it.state === 'done') stateBadge = '<span class="cleanup-state-pill done"><span data-icono="check"></span> Done</span>';
    else if (it.state === 'error') stateBadge = '<span class="cleanup-state-pill error"><span data-icono="aviso"></span> Error</span>';
    else stateBadge = `<span class="cleanup-state-pill in-progress">${icono('pausa')} ${escHtml(it.phase)}</span>`;

    const cb = it.safe_to_delete
      ? `<input type="checkbox" class="cmv40-cleanup-cb" data-id="${escHtml(it.id)}" data-size="${it.size_bytes}" checked>`
      : `<input type="checkbox" class="cmv40-cleanup-cb" data-id="${escHtml(it.id)}" data-size="${it.size_bytes}" disabled title="${escHtml(it.reason)}">`;

    const sizeStr = it.size_bytes > 0 ? _cleanupFmtBytes(it.size_bytes) : '—';
    const filesStr = it.files_count > 0 ? tr('comun.n_ficheros', {n: it.files_count, p2: it.files_count === 1 ? '' : 's'}) : '';

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
      <span data-i18n-html="cmv40_modals.atencion_esta_accion_es_irreversible"></span>
    </div>
    <table class="cmv40-cleanup-table">
      <thead>
        <tr>
          <th><input type="checkbox" id="cmv40-cleanup-select-all" data-i18n-tip="cmv40_modals.seleccionar_todo"></th>
          <th data-i18n="cmv40_modals.proyecto"></th>
          <th data-i18n="ui.tamano"></th>
          <th data-i18n="workbar.detalle"></th>
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
      : '<span style="color:var(--text-3)">' + tr('cmv40_modals.selecciona_al_menos_un_proyecto') + '</span>';
  }
  if (btn) {
    btn.disabled = count === 0;
  }
}

async function cmv40BulkCleanupExecute() {
  const cbs = Array.from(document.querySelectorAll('.cmv40-cleanup-cb:checked'));
  const ids = cbs.map(cb => cb.dataset.id).filter(Boolean);
  if (!ids.length) {
    showToast(tr('cmv40_modals.no_hay_nada_seleccionado'), 'info');
    return;
  }
  showConfirm(
    tr('cmv40_modals.borrar_artefactos_de_proyecto', {p1: ids.length, p2: ids.length === 1 ? '' : 's'}),
    tr('cmv40_modals.esta_accion_es_irreversible_los_proyectos'),
    async () => {
      const data = await apiFetch('/api/cmv40/cleanup/bulk', {
        method: 'POST',
        body: JSON.stringify({ session_ids: ids }),
      }, API_FETCH_TIMEOUT_LARGO);
      if (!data) return;
      const okCount = (data.deleted || []).length;
      const skipCount = (data.skipped || []).length;
      const koCount = (data.failed || []).length;
      const freed = _cleanupFmtBytes(data.total_freed_bytes || 0);
      let msg = tr('cmv40_modals.n_proyectos_archivados_liberados', {n: okCount, p2: okCount === 1 ? '' : 's', freed: freed});
      if (skipCount > 0) msg += tr('cmv40_modals.omitido_en_curso', {skipcount: skipCount, p2: skipCount === 1 ? '' : 's'});
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
    tr('tab2.borrar'),
  );
}

// ── El manual, cargado a demanda ───────────────────────────────────
//
// Las siete secciones vivían dentro de este fichero: 1.647 líneas y ~150 KB
// que el navegador se descargaba EN CADA CARGA, abriera el manual o no. Con
// tres idiomas serían ~450 KB, y eso ya no es un detalle.
//
// Y son tres documentos paralelos, no un catálogo de claves: el manual es
// prosa, y trocear prosa en claves la vuelve intraducible.
let _manualCache = null;

async function _cmv40ManualSecciones() {
  if (_manualCache) return _manualCache;
  try {
    const r = await fetch(`/static/i18n/manual/${idiomaActivo()}.json?v=${TOKEN_I18N}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    _manualCache = await r.json();
  } catch (e) {
    console.error('[manual] no se pudo cargar', e);
    _manualCache = {};
  }
  return _manualCache;
}

async function _cmv40HelpSwitch(section) {
  sessionStorage.setItem('cmv40HelpSection', section);
  document.querySelectorAll('.cmv40-help-nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.section === section);
  });
  const content = document.getElementById('cmv40-help-content');
  if (!content) return;
  // Mientras llega: un aviso, no un panel en blanco. La primera vez son
  // ~150 KB del servidor local; las siguientes, cero.
  if (!_manualCache) content.innerHTML = `<p>${escHtml(tr('manual.cargando'))}</p>`;
  const secciones = await _cmv40ManualSecciones();
  content.innerHTML = secciones[section] || `<p>${escHtml(tr('manual.sin_seccion'))}</p>`;
  content.scrollTop = 0;

  // Hidrataciones post-render (nodos que dependen de estado live)
  if (section === 'sheet') _cmv40HelpHydrateSheetLink();
  if (section === 'repo')  _cmv40HelpHydrateDriveLink();
}

/** Hidrata el enlace tr('cmv40_modals.hoja_en_uso') al abrir la sección Sheet del manual.
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
      anchor.textContent = tr('cmv40_modals.url_no_disponible');
      anchor.removeAttribute('href');
      return;
    }
    anchor.href = url;
    anchor.textContent = url;
    if (metaEl) {
      const srcLabel = sh.source === 'settings' ? tr('cmv40_modals.url_personalizada_configuracion')
        : sh.source === 'env'      ? tr('cmv40_modals.url_de_variable_de_entorno')
        : tr('cmv40_modals.url_por_defecto_de_la_comunidad');
      metaEl.textContent = sh.is_default
        ? tr('cmv40_modals.url_por_defecto_de_la_comunidad_2')
        : srcLabel;
    }
  } catch (_) {
    anchor.textContent = tr('cmv40_modals.no_se_ha_podido_cargar_la');
    anchor.removeAttribute('href');
  }
}

/** Hidrata el bloque tr('cmv40_modals.carpeta_drive_en_este_servidor') al abrir la sección Repo.
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
      statusEl.innerHTML = icono('check') + ` Configurada <span style="font-size:11px; font-weight:500; color:var(--text-3)">${tr('cmv40_modals.folder_p1', {p1: escHtml(df.folder_id_last6 || '??????')})}</span>`;
      statusEl.style.color = '#0e6b2a';
      const srcLabel = df.source === 'settings' ? tr('cmv40_modals.configurada_desde_configuracion')
        : df.source === 'env' ? tr('cmv40_modals.configurada_por_variable_de_entorno_del')
        : 'configurada';
      const apiKeyState = apiKey.configured ? tr('cmv40_modals.api_key_configurada') : tr('cmv40_modals.api_key_sin_configurar_imprescindible');
      if (metaEl) metaEl.textContent = `${srcLabel} · ${apiKeyState}`;
    } else {
      statusEl.innerHTML = icono('aviso') + ' ' + tr('cmv40_modals.no_configurada');
      statusEl.style.color = '#8a4a00';
      if (metaEl) metaEl.textContent = tr('cmv40_modals.sigue_los_pasos_de_abajo_para');
    }
  } catch (_) {
    statusEl.textContent = tr('cmv40_modals.no_se_ha_podido_consultar_el');
    if (metaEl) metaEl.textContent = '—';
  }
}

/**
 * Contenido de las secciones del manual. v1 — se irá iterando con el usuario.
 * Datos validados contra el código real del pipeline (inventario de audit).
 * Marcado con `help-unverified` lo que requiera research externa.
 */

async function cmv40LookupSearch() {
  const input = document.getElementById('cmv40-lookup-title');
  const yearInput = document.getElementById('cmv40-lookup-year');
  const btn = document.getElementById('cmv40-lookup-btn');
  const results = document.getElementById('cmv40-lookup-results');
  if (!input || !results) return;

  const title = (input.value || '').trim();
  if (!title) {
    results.innerHTML = '<div class="cmv40-lookup-empty">' + tr('cmv40_modals.introduce_un_titulo_para_consultar') + '</div>';
    input.focus();
    return;
  }
  const year = yearInput?.value ? parseInt(yearInput.value, 10) : null;

  if (btn) btn.disabled = true;
  results.innerHTML = `<div class="cmv40-lookup-loading">
    <span class="cmv40-rec-spinner-inline"></span>
    <span data-i18n="cmv40_modals.buscando_coincidencias_en_tmdb"></span>
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
      : `<div class="cmv40-lookup-pick-poster cmv40-lookup-pick-noposter"><span data-icono="claqueta"></span></div>`;
    const rating = c.vote_average > 0
      ? `<span class="cmv40-lookup-pick-rating">${c.vote_average.toFixed(1)}</span>`
      : '';
    const origHtml = (c.title_en && c.title_en !== c.title_es)
      ? `<div class="cmv40-lookup-pick-orig">${tr('cmv40_modals.original_title_en', {title_en: escHtml(c.title_en)})}</div>`
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
      <div class="cmv40-lookup-section-title"><span data-icono="claqueta"></span> ${tr('cmv40_modals.p1_coincidencias_en_tmdb_para_querytitle', {p1: candidates.length, querytitle: escHtml(queryTitle)})}</div>
      <div class="cmv40-lookup-section-desc" data-i18n="cmv40_modals.selecciona_la_pelicula_a_la_que"></div>
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
    <span data-i18n="cmv40_modals.consultando_hoja_dovitools_repositorio_drive_para"></span> <strong>${escHtml(title)}${year ? ` (${year})` : ''}</strong>…
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
    container.innerHTML = '<div class="cmv40-lookup-empty">' + tr('cmv40_modals.no_se_pudo_consultar_revisa_la_conexion') + '</div>';
    return;
  }

  let html = '';

  // ── 1. Ficha TMDb ─────────────────────────────────────────────
  const tmdbDetails = tmdb?.details || null;
  if (tmdbDetails) {
    html += renderTmdbCardHTML(tmdbDetails) || '';
  } else if (tmdb && !tmdb.tmdb_configured) {
    html += `<div class="cmv40-lookup-warn"><span data-i18n-html="cmv40_modals.tmdb_no_esta_disponible_pon_la_tuya"></span></div>`;
  } else if (tmdb) {
    html += `<div class="cmv40-lookup-warn"><span data-icono="info"></span> <span data-i18n="cmv40_modals.tmdb_no_encontro_la_pelicula_con"></span></div>`;
  }

  // ── 2. Sección "Hoja de DoviTools" con su banner de estado/notas ──
  // Reusa exactamente el mismo renderer del modal de Nuevo proyecto, con
  // sus códigos de color (verde/rojo/gris), chips (Fuente·Sync·Verif.),
  // motivo textual + links clicables al sheet original.
  html += `<div class="cmv40-lookup-section">
    <div class="cmv40-lookup-section-title"><span data-icono="portapapeles"></span> <span data-i18n="cmv40_modals.hoja_de_recomendaciones_dovitools"></span></div>
    <div class="cmv40-lookup-section-desc" data-i18n="cmv40_modals.lo_que_dice_la_comunidad_sobre"></div>
    <div id="cmv40-lookup-rec-banner" class="cmv40-rec-banner" style="display:none"></div>
  </div>`;

  // ── 3. Candidatos del repositorio con pipeline previsto ──────
  html += '<div class="cmv40-lookup-section">';
  html += '<div class="cmv40-lookup-section-title"><span data-icono="caja"></span> Repositorio DoviTools (bins <code>.bin</code>)</div>';
  html += '<div class="cmv40-lookup-section-desc">' + tr('cmv40_modals.ficheros_disponibles_para_descarga_automatica') + '</div>';
  if (!repo || !repo.drive_configured) {
    html += _cmv40RepoUnavailableBanner(repo);
  } else if (repo.error) {
    html += `<div class="cmv40-lookup-warn">${escHtml(repo.error)}</div>`;
  } else if (!repo.candidates || repo.candidates.length === 0) {
    const t = repo.title_en || repo.title_es || tr('cmv40_modals.titulo');
    html += `<div class="cmv40-lookup-empty">${tr('cmv40_modals.no_hay_bin_para_titulo', {titulo: escHtml(t)})}</div>`;
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
        ? '<span class="cmv40-lookup-tag tag-ok"><span data-icono="biblioteca"></span> Retail</span>'
        : prov === 'generated'
        ? '<span class="cmv40-lookup-tag tag-warn"><span data-icono="aviso"></span> Generated</span>'
        : '';
      return `
        <li class="cmv40-lookup-candidate ${isBest ? 'best' : ''}">
          <div class="cmv40-lookup-cand-head">
            <span class="cmv40-lookup-tag ${tagMeta.cls}">${icono(tagMeta.icon)} ${tagMeta.label}</span>
            ${provTag}
            ${isBest ? '<span class="cmv40-lookup-best"><span data-icono="diana"></span> mejor match</span>' : ''}
            <span class="cmv40-lookup-score">${tr('cmv40_modals.score_similitud', {score: score})}</span>
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
      slot.innerHTML = '<div class="cmv40-rec-body">' + tr('cmv40_modals.no_se_pudo_consultar_la_hoja_de_dovitools') + '</div>';
    }
  }
}

function _cmv40LookupTagMeta(pt) {
  if (pt === 'trusted_p7_fel_final') return { icon: 'diana', label: 'Bin P7 FEL', cls: 'tag-ok' };
  if (pt === 'trusted_p7_mel_final') return { icon: 'diana', label: 'Bin P7 MEL', cls: 'tag-ok' };
  // trusted_p8_source cubre tanto P8 retail nativo como P5→P8 transfer.
  // Etiqueta neutra para no asumir uno u otro.
  if (pt === 'trusted_p8_source')    return { icon: 'caja', label: 'Bin P8 retail', cls: 'tag-info' };
  return { icon: 'info', label: tr('tab3.tipo_desconocido'), cls: 'tag-warn' };
}

function _cmv40LookupPipelineSummary(pt, provenance) {
  const info = (typeof _CMV40_PIPELINE_PREVIEW !== 'undefined') ? _CMV40_PIPELINE_PREVIEW[pt] : null;
  if (!info) {
    return '<div class="cmv40-lookup-pp-desc">' + tr('cmv40_modals.pipeline_se_determinara_tras_descarga') + '</div>';
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
