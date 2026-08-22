'use strict';
/**
 * settings.js — El engranaje: configuración, versión y mantenimiento.
 *
 * El modal ⚙︎ Configuración (las API keys, con su validación live), el pill de
 * nueva versión con `check-updates`, y el panel de Limpieza que escanea y borra
 * huérfanos. Es el equivalente en el frontend de lo que quedó en `main.py`:
 * cosas de la aplicación, no de una pestaña.
 */

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
  // Versión + chequeo de updates (no force, usa cache 1h)
  _renderVersionInfo();
  checkForUpdates(false);
  openModal('settings-modal');
  setTimeout(() => document.getElementById('settings-tmdb-input')?.focus(), 50);
}

/** Comprobacion silenciosa de updates al arrancar la app. Sin force (usa
 *  cache 1h) para no machacar la API de GitHub. Si hay update: pinta el
 *  pill ambar en el header. La comprobacion respeta la version simulada
 *  para que el modo dev test sea coherente con el header. */
async function _initUpdateCheckHeader() {
  // Esperamos un tick para que el modal Settings/UI ya esté inicializado
  // y para no competir con cargas críticas de arranque.
  await new Promise(r => setTimeout(r, 1500));
  await _refreshHeaderUpdatePill();
}

async function _refreshHeaderUpdatePill() {
  const pill = document.getElementById('header-update-pill');
  if (!pill) return;
  const params = new URLSearchParams();
  const sim = _getSimulatedVersion();
  if (sim) params.set('simulate_current', sim);
  const url = '/api/version/check-updates' + (params.toString() ? '?' + params.toString() : '');
  const data = await apiFetch(url, { silent: true });
  const txtEl = document.getElementById('header-update-pill-text');
  if (!data || !data.update_available || !data.latest) {
    pill.style.display = 'none';
    return;
  }
  pill.style.display = 'inline-flex';
  if (txtEl) txtEl.textContent = `Nueva versión: ${data.latest}`;
}

async function _renderVersionInfo() {
  const data = await apiFetch('/api/version', { silent: true });
  if (!data) return;
  const cur = document.getElementById('settings-version-current');
  const pill = document.getElementById('settings-version-pill');
  if (!cur || !pill) return;
  const versionLabel = data.version || 'desconocida';
  let pillCls = 'dev', pillTxt = '⚠ desarrollo';
  if (data.is_tagged) {
    pillCls = 'tagged'; pillTxt = '✓ release';
  } else if (data.commit) {
    pillCls = 'dev'; pillTxt = '⚙ desarrollo';
  } else {
    pillCls = 'unknown'; pillTxt = '? desconocida';
  }
  const commitTxt = data.commit ? ` · ${data.commit}` : '';
  const dirtyTxt  = data.is_dirty ? ' · dirty' : '';
  cur.innerHTML = `
    <strong>${escHtml(versionLabel)}</strong><span style="color:var(--text-3); font-size:11.5px">${escHtml(commitTxt + dirtyTxt)}</span>`;
  pill.className = 'settings-version-pill ' + pillCls;
  pill.textContent = pillTxt;
  // Mostrar input de simulación SOLO con DEV_MODE=1 en runtime (no basta
  // con que la version sea post-tag tipo v2.1.6-1-gXXXX — eso pasa en
  // builds de produccion en NAS si rebuilds despues del ultimo tag).
  const simBox = document.getElementById('settings-version-simulate');
  if (simBox) {
    simBox.style.display = data.is_dev_mode ? 'flex' : 'none';
    const simInput = document.getElementById('settings-version-simulate-input');
    if (simInput) simInput.value = localStorage.getItem('hdo_simulate_version') || '';
  }
}

function _getSimulatedVersion() {
  return (localStorage.getItem('hdo_simulate_version') || '').trim();
}

function applySimulatedVersion() {
  const inp = document.getElementById('settings-version-simulate-input');
  const v = (inp?.value || '').trim();
  if (v) {
    localStorage.setItem('hdo_simulate_version', v);
    showToast(`🧪 Simulando versión actual: ${v}`, 'info');
  } else {
    localStorage.removeItem('hdo_simulate_version');
    showToast('🧪 Simulación desactivada', 'info');
  }
  checkForUpdates(true);
}

function clearSimulatedVersion() {
  localStorage.removeItem('hdo_simulate_version');
  const inp = document.getElementById('settings-version-simulate-input');
  if (inp) inp.value = '';
  showToast('🧪 Simulación desactivada', 'info');
  checkForUpdates(true);
}

async function checkForUpdates(force) {
  const banner = document.getElementById('settings-update-banner');
  const btn = document.getElementById('settings-version-check-btn');
  if (!banner) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = '🔄 Consultando…';
  }
  const params = new URLSearchParams();
  if (force) params.set('force', 'true');
  const sim = _getSimulatedVersion();
  if (sim) params.set('simulate_current', sim);
  const url = '/api/version/check-updates' + (params.toString() ? '?' + params.toString() : '');
  const data = await apiFetch(url, { silent: true });
  // Sync el pill del header con el resultado actual (ej. tras ignorar
  // version o cambiar simulacion, el header refleja el cambio sin esperar
  // a otro tick automatico).
  const pill = document.getElementById('header-update-pill');
  const pillTxt = document.getElementById('header-update-pill-text');
  if (pill) {
    if (data && data.update_available && data.latest) {
      pill.style.display = 'inline-flex';
      if (pillTxt) pillTxt.textContent = `Nueva versión: ${data.latest}`;
    } else {
      pill.style.display = 'none';
    }
  }
  if (btn) {
    btn.disabled = false;
    btn.textContent = '🔄 Comprobar actualizaciones';
  }
  if (!data) {
    banner.style.display = 'block';
    banner.className = 'settings-update-banner err';
    banner.innerHTML = `<div class="settings-update-msg">⚠ No se pudo consultar la API de GitHub. Reintenta en unos minutos.</div>`;
    return;
  }
  if (!data.update_available) {
    banner.style.display = 'block';
    if (!data.latest) {
      // No conseguimos resolver la version remota — no es 'al dia',
      // es 'no se pudo comprobar'. Banner gris/error informativo.
      banner.className = 'settings-update-banner err';
      banner.innerHTML = `<div class="settings-update-msg">⚠ No se pudo determinar la última versión publicada. Comprueba que el repo tenga al menos un tag <code>vX.Y.Z</code> o un Release publicado.</div>`;
      return;
    }
    banner.className = 'settings-update-banner ok';
    const simBadge = data.simulated ? ` <span class="settings-update-sim-badge">🧪 simulado</span>` : '';
    const latestPart = ` · última publicada: <strong>${escHtml(data.latest)}</strong>${simBadge}`;
    const ignored = data.ignored_version
      ? `<div class="settings-update-msg-sub">Ignorando avisos de la versión ${escHtml(data.ignored_version)}. <button class="btn btn-ghost btn-xs" onclick="ignoreUpdate('')">Reactivar avisos</button></div>`
      : '';
    banner.innerHTML = `<div class="settings-update-msg">✓ Estás al día (current: <strong>${escHtml(data.current)}</strong>)${latestPart}.</div>${ignored}`;
    return;
  }
  // Hay update — banner ámbar con notas (todas las pendientes) + botones
  banner.style.display = 'block';
  banner.className = 'settings-update-banner warn';
  const cmds = `docker compose pull\ndocker compose up -d`;
  const simBadge = data.simulated ? `<span class="settings-update-sim-badge">🧪 simulado</span>` : '';

  // Lista de releases pendientes (todas entre current y latest, newest first).
  // Si solo viene release_notes (fallback antiguo), construye un pseudo-release
  // con la latest para mantener el formato uniforme.
  let pending = Array.isArray(data.pending_releases) ? data.pending_releases.slice() : [];
  if (!pending.length && data.release_notes) {
    pending = [{
      tag: data.latest,
      body: data.release_notes,
      url: data.release_url || '',
      published_at: data.published_at || '',
    }];
  }

  let notesHtml = '';
  if (pending.length) {
    const sectionsHtml = pending.map(rel => {
      const dateStr = rel.published_at
        ? new Date(rel.published_at).toLocaleDateString('es-ES', { day: '2-digit', month: 'short', year: 'numeric' })
        : '';
      const linkBtn = rel.url
        ? `<a class="settings-update-rel-link" href="${escHtml(rel.url)}" target="_blank" rel="noreferrer">↗</a>`
        : '';
      const body = (rel.body || '').trim() || '_(release sin notas)_';
      return `
        <div class="settings-update-rel">
          <div class="settings-update-rel-head">
            <strong>${escHtml(rel.tag)}</strong>
            ${dateStr ? `<span class="settings-update-rel-date">· ${escHtml(dateStr)}</span>` : ''}
            ${linkBtn}
          </div>
          <div class="settings-update-rel-body">${_renderReleaseMarkdown(body)}</div>
        </div>`;
    }).join('');
    const summaryTxt = pending.length === 1
      ? `📋 Ver notas de versión (1 release pendiente)`
      : `📋 Ver notas de versión (${pending.length} releases pendientes)`;
    // Cerrado por defecto — el triángulo nativo es poco intuitivo;
    // usamos un botón visible con icono + texto explícito.
    notesHtml = `<details class="settings-update-notes"><summary class="settings-update-notes-toggle">${summaryTxt}</summary>${sectionsHtml}</details>`;
  }

  banner.innerHTML = `
    <div class="settings-update-head">
      🔔 Nueva versión disponible: <strong>${escHtml(data.current)}</strong> → <strong>${escHtml(data.latest)}</strong> ${simBadge}
    </div>
    ${notesHtml}
    <div class="settings-update-cmd">
      <pre id="settings-update-cmd-pre">${escHtml(cmds)}</pre>
    </div>
    <div class="settings-update-actions">
      <button class="btn btn-primary btn-sm" onclick="copyUpdateCommands()">📋 Copiar comandos</button>
      ${data.release_url ? `<a class="btn btn-secondary btn-sm" href="${escHtml(data.release_url)}" target="_blank" rel="noreferrer">↗ Release en GitHub</a>` : ''}
      <button class="btn btn-ghost btn-sm" onclick="ignoreUpdate('${escHtml(data.latest)}')">Ignorar esta versión</button>
    </div>`;
}

/** Renderiza markdown ligero (headings ##, ###, bullets, **bold**, `code`)
 *  a HTML. Suficiente para las release notes que generamos con plantilla
 *  fija. NO es un parser markdown completo — no hace falta. */
function _renderReleaseMarkdown(md) {
  const lines = md.split('\n');
  const out = [];
  let inList = false;
  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) { closeList(); continue; }
    // Heading H2 (## Título)
    let m = line.match(/^##\s+(.+)$/);
    if (m) { closeList(); out.push(`<h4 class="settings-update-rel-h">${_inlineMd(m[1])}</h4>`); continue; }
    // Heading H3
    m = line.match(/^###\s+(.+)$/);
    if (m) { closeList(); out.push(`<h5 class="settings-update-rel-h">${_inlineMd(m[1])}</h5>`); continue; }
    // Bullet (- texto)
    m = line.match(/^\s*[-*]\s+(.+)$/);
    if (m) {
      if (!inList) { out.push('<ul class="settings-update-rel-list">'); inList = true; }
      out.push(`<li>${_inlineMd(m[1])}</li>`);
      continue;
    }
    // Texto suelto = párrafo
    closeList();
    out.push(`<p>${_inlineMd(line)}</p>`);
  }
  closeList();
  return out.join('');
}

/** Formato inline básico: **bold**, `code`, escape de < > & */
function _inlineMd(text) {
  let s = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  return s;
}

async function copyUpdateCommands() {
  const pre = document.getElementById('settings-update-cmd-pre');
  if (!pre) return;
  const txt = pre.textContent || '';
  const ok = await _copyTextToClipboardWithFallback(txt);
  showToast(ok ? '📋 Comandos copiados al portapapeles' : 'No se pudo copiar al portapapeles', ok ? 'success' : 'error');
}

async function ignoreUpdate(version) {
  await apiFetch('/api/version/ignore-update', {
    method: 'POST',
    body: JSON.stringify({ version }),
  });
  showToast(version
    ? `⏭ Aviso de ${version} silenciado`
    : '🔔 Avisos de actualización reactivados', 'info');
  checkForUpdates(false);
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

// ── Mantenimiento: scan + cleanup de huerfanos ──────────────────────
// Flujo: escanear → tabla con checkboxes → confirmar → toast con resumen.
// Solo paths bajo prefixes whitelisted (validacion adicional en backend).

function _cleanupFmtBytes(bytes) {
  if (!bytes || bytes < 1024) return `${bytes || 0} B`;
  const KB = 1024, MB = KB * 1024, GB = MB * 1024;
  if (bytes < MB) return `${(bytes / KB).toFixed(1)} KB`;
  if (bytes < GB) return `${(bytes / MB).toFixed(1)} MB`;
  return `${(bytes / GB).toFixed(2)} GB`;
}

function _cleanupFmtAge(secs) {
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

async function cleanupScanAndShow() {
  const btn = document.getElementById('settings-cleanup-scan-btn');
  const resultEl = document.getElementById('settings-cleanup-result');
  if (!btn || !resultEl) return;

  btn.disabled = true;
  btn.innerHTML = '⏳ Escaneando…';
  resultEl.innerHTML = '';

  const data = await apiFetch('/api/cleanup/scan');
  btn.disabled = false;
  btn.innerHTML = '🔍 Escanear huérfanos';

  if (!data) return;
  if (!data.items || !data.items.length) {
    resultEl.innerHTML = '<div class="settings-cleanup-empty">✓ No se encontraron huérfanos. Todo limpio.</div>';
    return;
  }

  // Render tabla con checkboxes (default: marcado solo si safe=true)
  const rows = data.items.map((it, i) => {
    const checked = it.safe ? 'checked' : '';
    const warnIcon = it.safe ? '' : '<span class="cleanup-warn" data-tooltip="Reciente o potencialmente activo — revisa antes de borrar">⚠️</span>';
    return `
      <tr class="cleanup-row${it.safe ? '' : ' cleanup-row-warn'}">
        <td><input type="checkbox" class="cleanup-cb" data-path="${escHtml(it.path)}" ${checked}></td>
        <td>${warnIcon}${escHtml(it.label)}</td>
        <td class="cleanup-path" title="${escHtml(it.path)}">${escHtml(it.path)}</td>
        <td class="cleanup-size">${_cleanupFmtBytes(it.size_bytes)}</td>
        <td class="cleanup-age">${_cleanupFmtAge(it.age_seconds)}</td>
        <td class="cleanup-reason">${escHtml(it.reason)}</td>
      </tr>`;
  }).join('');

  resultEl.innerHTML = `
    <div class="cleanup-summary">
      <strong>${data.total_count}</strong> elementos · liberables ${_cleanupFmtBytes(data.total_bytes)}
      ${data.safe_count < data.total_count
        ? ` · <span class="cleanup-warn-text">${data.total_count - data.safe_count} requieren revisión</span>`
        : ''}
    </div>
    <table class="cleanup-table">
      <thead>
        <tr>
          <th><input type="checkbox" id="cleanup-select-all" title="Seleccionar todo"></th>
          <th>Tipo</th>
          <th>Ruta</th>
          <th>Tamaño</th>
          <th>Edad</th>
          <th>Motivo</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="cleanup-actions">
      <button class="btn btn-ghost btn-sm" onclick="document.getElementById('settings-cleanup-result').innerHTML=''">Cancelar</button>
      <button class="btn btn-danger btn-sm" onclick="cleanupExecuteSelected()">🗑 Borrar seleccionados</button>
    </div>
  `;

  // Wire select-all
  const selectAll = document.getElementById('cleanup-select-all');
  if (selectAll) {
    selectAll.addEventListener('change', (e) => {
      const checked = e.target.checked;
      resultEl.querySelectorAll('.cleanup-cb').forEach(cb => { cb.checked = checked; });
    });
  }

  // Asegurar que el resultado es visible — la seccion puede quedar abajo del
  // body del modal y el usuario no verla si no hace scroll manualmente.
  resultEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function cleanupExecuteSelected() {
  const resultEl = document.getElementById('settings-cleanup-result');
  if (!resultEl) return;
  const checked = Array.from(resultEl.querySelectorAll('.cleanup-cb:checked'));
  const paths = checked.map(cb => cb.dataset.path).filter(Boolean);
  if (!paths.length) {
    showToast('No hay nada seleccionado', 'info');
    return;
  }
  // Confirmacion via modal nativo del proyecto
  showConfirm(
    `¿Borrar ${paths.length} elemento(s)?`,
    'Esta operación es irreversible. Asegúrate de no tener jobs activos sobre estos paths.',
    async () => {
      const data = await apiFetch('/api/cleanup/execute', {
        method: 'POST',
        body: JSON.stringify({ paths }),
      });
      if (!data) return;
      const okCount = (data.deleted || []).length;
      const koCount = (data.failed || []).length;
      const freed = _cleanupFmtBytes(data.total_freed_bytes || 0);
      if (koCount === 0) {
        showToast(`✓ Borrados ${okCount} elementos · liberados ${freed}`, 'success');
      } else {
        showToast(`Borrados ${okCount} · ${koCount} fallaron · liberados ${freed}`, 'warning');
      }
      // Re-escanear para refrescar el listado
      cleanupScanAndShow();
    },
    'Borrar',
  );
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
