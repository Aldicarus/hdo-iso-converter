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

    <h2 id="s-structure">📋 Estructura del sheet — tres bloques de columnas</h2>
    <p>La hoja tiene tres bloques de columnas, y un mismo título puede aparecer en <strong>varios a la vez</strong>: no es una contradicción, cada bloque documenta una <em>ruta distinta</em>.</p>
    <table>
      <tr><th>Bloque</th><th>Qué evalúa realmente</th><th>Qué hace la app</th></tr>
      <tr><td><strong>Izquierda — "no factible"</strong></td><td>Si el disco se puede <strong>convertir a P8.1 single-layer</strong>, que es el objetivo mayoritario de la comunidad (fichero más ligero, reproducible en cualquier dispositivo DV). El motivo dominante — <em>"can only be played on a FEL device and can't be converted to P8 without baking FEL into BL"</em>, dos tercios de las filas del bloque — significa que el FEL lleva información de imagen real y aplanarlo exigiría re-codificar el vídeo.</td><td>Banner azul informativo <strong>"No convertible a P8.1"</strong> con el chip <em>no aplica a este flujo</em>. Esta app nunca aplana a P8: el workflow <code>p7_fel</code> preserva la capa de mejora, así que ese impedimento no afecta al resultado. No desaconseja nada.</td></tr>
      <tr><td><strong>Derecha — "factible"</strong></td><td>Si el <strong>bloque CMv4.0 se puede restaurar sobre el RPU P7</strong> del disco (la nota típica es <em>"cmv4.0 bloc can be restored to the P7 RPU (workflow 2-3)"</em>). Esto es exactamente lo que hace esta app.</td><td>Banner verde <strong>Factible</strong>: ruta verificada por la comunidad, con desfase de frames conocido y comparaciones HDR.</td></tr>
      <tr><td><strong>Derecha extra — "Not Sure!"</strong></td><td>Bin disponible pero <em>sin verificación completa</em>, o reportes contradictorios.</td><td>Banner ámbar <strong>Probablemente OK</strong>: conviene revisar la sincronización a mano aunque los <em>trust gates</em> pasen.</td></tr>
    </table>
    <div class="help-callout help-callout-info">
      <strong>Por qué importa la distinción:</strong> si un título está en la izquierda y en la derecha, la app se queda con la lectura de la derecha y muestra <em>todas</em> las filas, cada una con su bloque de origen. Antes colapsaba las dos en un único veredicto y ganaba siempre la de la izquierda, así que salía un ❌ rojo aunque la hoja documentara la ruta de restore. Los motivos que sí bajan el semáforo son los que afectan al resultado: <code>static dv</code> (metadata plana en la fuente), <code>mdl mismatch</code> / <code>different grade</code> (el master de referencia tiene otro grading) y <code>no bd yet</code>.
    </div>

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
      <strong>Cómo interpretar el desfase:</strong> si el sheet dice <code>+48</code>, significa que el bin viene con 48 frames extra al inicio (normalmente logos de estudio que el Blu-ray no tiene). En la Fase D la app <strong>contrasta ese dato con el desfase que mide ella misma</strong> por cross-correlation: si coinciden (±2 frames) aparece una confirmación verde — dos medidas independientes de acuerdo es la mejor señal de que el bin es el correcto; si divergen, un aviso ámbar te pide revisar el gráfico antes de inyectar, porque suele indicar un bin de otra edición o de otro corte. La corrección la sigues aplicando tú desde la Fase D.
    </div>

    <h2 id="s-hyperlinks">🔗 Enlaces del sheet</h2>
    <p>Muchas celdas llevan enlaces incrustados a recursos externos: el bin en Google Drive, imágenes comparativas, hilos de foro con pruebas, tutoriales específicos. La app los preserva y te los muestra con un botón "Abrir ↗" en:</p>
    <ul>
      <li>El <strong>banner de recomendación</strong> que aparece al seleccionar un Blu-ray en "Nuevo proyecto".</li>
      <li>La card <strong>"📋 Hoja de DoviTools"</strong> del panel del proyecto, que conserva el veredicto durante todo el pipeline.</li>
      <li>La <strong>consulta rápida <code>🔎</code></strong> del header — para revisar un título sin crear proyecto.</li>
    </ul>

    <h2 id="s-app">⚙️ Cómo lo usa esta app</h2>
    <ol>
      <li>Al seleccionar el Blu-ray origen en "Nuevo proyecto", la app extrae el título y año del nombre del fichero.</li>
      <li>Si has configurado una API key de TMDb en <strong>⚙︎ Configuración</strong>, la app contrasta el título con TMDb — así desambigua cine no-ASCII (cine asiático, títulos en otros idiomas) y confirma el año.</li>
      <li>Te muestra el veredicto <strong>traducido a lo que hace esta app</strong> (que preserva el FEL): verde <em>Factible</em>, ámbar <em>Viable con avisos</em> / <em>Probablemente OK</em>, azul <em>No convertible a P8.1</em> (informativo) o rojo <em>No recomendado</em>. Si el título aparece en varios bloques del sheet, se listan todos con su bloque de origen.</li>
      <li>Al crear el proyecto el veredicto <strong>se guarda con él</strong>, así que los avisos, el desfase documentado y los enlaces siguen a mano en la Fase D — que es donde hacen falta. El botón "↻ Actualizar" de la card lo vuelve a consultar.</li>
      <li>En la <strong>Fase D</strong> el desfase del sheet se compara con el que mide la app; coincidencia = confirmación, divergencia = aviso.</li>
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
      <div class="help-pipeline-diagram-sub">Hay bin P7 MEL CMv4.0 retail en el repo (poco común). El MEL original del Blu-ray no añade precisión de color respecto al bin target, así que la app se queda con el bin y descarta el EL. Como el stream resultante ya no tiene capa de mejora, el RPU se convierte a <strong>Profile 8.1</strong> (<code>dovi_tool editor</code>, mode 2) para que la señalización case con el contenido. Resultado: P8.1 CMv4.0 single-layer, compatible con cualquier reproductor DV.</div>
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
