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

/* ══════════════════════════════════════════════════════════════════════
 *  Las secciones del modal — una tabla, no marcado
 *
 *  El modal llegó a OCHO bloques en un solo scroll y ya no se encontraba
 *  nada. La partición NO se hizo reordenando el HTML: cada
 *  `.settings-section` declara su `data-bloque` y esta tabla dice a qué
 *  sección va y en qué orden; al abrir, `_montarSeccionesDeAjustes` mueve
 *  los nodos a su panel con `appendChild`, que MUEVE en vez de copiar.
 *
 *  Lo que eso compra, y es el motivo de hacerlo así:
 *
 *  - reordenar —«la versión primero»— es mover una cadena de sitio;
 *  - añadir una sección es una fila aquí más un `data-bloque` en el bloque
 *    nuevo, sin tocar layout ni CSS;
 *  - **ningún id del DOM cambia**, así que las ~30 referencias por id de
 *    este fichero y `test_ids_del_dom` siguen valiendo tal cual;
 *  - el `MutationObserver` de iconos e i18n no se realimenta: los nodos que
 *    se mueven ya están pintados y llevan su `data-ico-puesto`.
 *
 *  **Un bloque que no esté en ninguna sección desaparecería de la pantalla
 *  sin dar un error**, que es la clase de fallo mudo que este repo persigue.
 *  Lo cruza `test_secciones_de_ajustes` en las dos direcciones.
 * ══════════════════════════════════════════════════════════════════════ */

// El rótulo y la descripción NO van aquí: se componen del `id`
// (`ajustes.seccion.general` y `..._desc`), que es el patrón del backend con
// `tr(f'cmv40.fase_{fase}')`. Guardar el texto —o su clave— en un campo es lo
// que los guards de i18n persiguen, y con razón: no hay forma de comprobar
// estáticamente que un string guardado en una propiedad acabe en un
// `data-i18n`. Que las dos claves de cada sección existan y se pinten lo
// cruza `test_secciones_de_ajustes`.
const SECCIONES_AJUSTES = [
  {
    id: 'general',
    icono: 'ajustes',
    // La versión va PRIMERO: es lo que se viene a mirar cuando se abre esto
    // sin una tarea concreta, y lo único que puede pedir una acción (actualizar).
    bloques: ['version', 'aviso', 'mantenimiento'],
  },
  {
    id: 'aspecto',
    icono: 'contraste',
    // Tema e idioma son la misma pregunta —cómo se ve y en qué lengua— y
    // vivían separados: el idioma perdido entre la versión y el mantenimiento.
    bloques: ['tema', 'idioma'],
  },
  {
    id: 'integraciones',
    icono: 'enlaceExterno',
    // El botón rojo del pie borra claves y URLs, que viven aquí. Con las
    // secciones dejó de tener sentido enseñarlo desde General: un botón de
    // borrar en rojo sobre una pantalla donde no se ve ni una clave.
    tieneClaves: true,
    bloques: ['tmdb', 'google', 'drive', 'sheet'],
  },
];

// Qué sección se está viendo. En memoria y no en `localStorage` a propósito:
// al volver a abrir el modal se empieza por arriba, que es lo predecible.
let _seccionDeAjustes = SECCIONES_AJUSTES[0].id;

/** Reparte los bloques en sus paneles y pinta la navegación.
 *
 *  Idempotente: se puede llamar en cada apertura. Los `appendChild` sobre un
 *  nodo que ya está en su sitio no hacen nada visible.
 */
function _montarSeccionesDeAjustes() {
  const panel = document.getElementById('settings-panel');
  const nav = document.getElementById('settings-nav');
  if (!panel || !nav) return;

  for (const sec of SECCIONES_AJUSTES) {
    let caja = panel.querySelector(`.settings-seccion[data-seccion="${sec.id}"]`);
    if (!caja) {
      caja = document.createElement('div');
      caja.className = 'settings-seccion';
      caja.dataset.seccion = sec.id;
      panel.appendChild(caja);
    }
    // El orden de `bloques` manda: `appendChild` mueve el nodo al final, así
    // que recorrer la lista en orden los deja en ese orden.
    for (const b of sec.bloques) {
      const nodo = document.querySelector(`.settings-section[data-bloque="${b}"]`);
      if (nodo) caja.appendChild(nodo);
    }
  }

  nav.innerHTML = SECCIONES_AJUSTES.map(sec => `
    <button type="button" class="settings-nav-item" data-seccion="${sec.id}"
            onclick="activarSeccionDeAjustes('${sec.id}')">
      <span class="settings-nav-ico">${icono(sec.icono)}</span>
      <span class="settings-nav-txt">
        <span class="settings-nav-titulo" data-i18n="ajustes.seccion.${sec.id}"></span>
        <span class="settings-nav-desc" data-i18n="ajustes.seccion.${sec.id}_desc"></span>
      </span>
    </button>`).join('');

  activarSeccionDeAjustes(_seccionDeAjustes);
}

/** Muestra una sección y marca su fila. */
function activarSeccionDeAjustes(id) {
  if (!SECCIONES_AJUSTES.some(s => s.id === id)) id = SECCIONES_AJUSTES[0].id;
  _seccionDeAjustes = id;
  document.querySelectorAll('#settings-panel .settings-seccion').forEach(c => {
    c.style.display = c.dataset.seccion === id ? '' : 'none';
  });
  document.querySelectorAll('#settings-nav .settings-nav-item').forEach(b => {
    const activo = b.dataset.seccion === id;
    b.classList.toggle('activo', activo);
    b.setAttribute('aria-current', activo ? 'true' : 'false');
  });
  _actualizarBotonBorrar();
  // El scroll es del panel, y al cambiar de sección se vuelve arriba: dejarlo
  // a media altura de la sección anterior desorienta.
  const panel = document.getElementById('settings-panel');
  if (panel) panel.scrollTop = 0;
}

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
  _montarSeccionesDeAjustes();
  await _loadSettings();
  // Versión + chequeo de updates (no force, usa cache 1h)
  _renderVersionInfo();
  checkForUpdates(false);
  renderAvisoFinSettings();
  openModal('settings-modal');
  // El foco NO va al campo de TMDb: desde que hay secciones vive en la otra,
  // y enfocarlo la abriría sola o —peor— escribirías a ciegas en un input
  // que no se ve. Va a la navegación, que es desde donde se elige.
  setTimeout(() => document.querySelector('#settings-nav .settings-nav-item.activo')?.focus(), 50);
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
  if (txtEl) txtEl.textContent = tr('settings.nueva_version', {latest: data.latest});
}

async function _renderVersionInfo() {
  const data = await apiFetch('/api/version', { silent: true });
  if (!data) return;
  const cur = document.getElementById('settings-version-current');
  const pill = document.getElementById('settings-version-pill');
  if (!cur || !pill) return;
  const versionLabel = data.version || 'desconocida';
  let pillCls = 'dev', pillTxt = 'desarrollo';
  if (data.is_tagged) {
    pillCls = 'tagged'; pillTxt = 'release';
  } else if (data.commit) {
    pillCls = 'dev'; pillTxt = 'desarrollo';
  } else {
    pillCls = 'unknown'; pillTxt = tr('settings.desconocida');
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
    showToast(tr('settings.simulando_version_actual', {v: v}), 'info');
  } else {
    localStorage.removeItem('hdo_simulate_version');
    showToast(tr('settings.simulacion_desactivada'), 'info');
  }
  checkForUpdates(true);
}

function clearSimulatedVersion() {
  localStorage.removeItem('hdo_simulate_version');
  const inp = document.getElementById('settings-version-simulate-input');
  if (inp) inp.value = '';
  showToast(tr('settings.simulacion_desactivada'), 'info');
  checkForUpdates(true);
}

async function checkForUpdates(force) {
  const banner = document.getElementById('settings-update-banner');
  const btn = document.getElementById('settings-version-check-btn');
  if (!banner) return;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = icono('refrescar') + ' ' + tr('settings.consultando');
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
      if (pillTxt) pillTxt.textContent = tr('settings.nueva_version', {latest: data.latest});
    } else {
      pill.style.display = 'none';
    }
  }
  if (btn) {
    btn.disabled = false;
    btn.innerHTML = icono('refrescar') + ' ' + tr('ui.comprobar_actualizaciones');
  }
  if (!data) {
    banner.style.display = 'block';
    banner.className = 'settings-update-banner err';
    banner.innerHTML = `<div class="settings-update-msg"><span data-icono="aviso"></span> <span data-i18n="settings.no_se_pudo_consultar_la_api"></span></div>`;
    return;
  }
  if (!data.update_available) {
    banner.style.display = 'block';
    if (!data.latest) {
      // No conseguimos resolver la version remota — no es 'al dia',
      // es 'no se pudo comprobar'. Banner gris/error informativo.
      banner.className = 'settings-update-banner err';
      banner.innerHTML = `<div class="settings-update-msg"><span data-i18n-html="settings.no_se_pudo_determinar_la_version"></span></div>`;
      return;
    }
    banner.className = 'settings-update-banner ok';
    const simBadge = data.simulated ? ` <span class="settings-update-sim-badge"><span data-icono="lupaOnda"></span> <span data-i18n="settings.simulado"></span></span>` : '';
    const latestPart = ' ' + tr('settings.ultima_publicada_p1', {p1: `<strong>${escHtml(data.latest)}</strong>`}) + `${simBadge}`;
    const ignored = data.ignored_version
      ? `<div class="settings-update-msg-sub">${tr('settings.ignorando_avisos_de_la_version_ignored', {ignored_version: escHtml(data.ignored_version)})} <button class="btn btn-ghost btn-xs" onclick="ignoreUpdate('')" data-i18n="settings.reactivar_avisos"></button></div>`
      : '';
    banner.innerHTML = `<div class="settings-update-msg"><span data-icono="check"></span> <span data-i18n="settings.estas_al_dia_current"></span> <strong>${escHtml(data.current)}</strong>)${latestPart}.</div>${ignored}`;
    return;
  }
  // Hay update — banner ámbar con notas (todas las pendientes) + botones
  banner.style.display = 'block';
  banner.className = 'settings-update-banner warn';
  const cmds = `docker compose pull\ndocker compose up -d`;
  const simBadge = data.simulated ? `<span class="settings-update-sim-badge"><span data-icono="lupaOnda"></span> <span data-i18n="settings.simulado"></span></span>` : '';

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
        ? new Date(rel.published_at).toLocaleDateString(localeActual(), { day: '2-digit', month: 'short', year: 'numeric' })
        : '';
      const linkBtn = rel.url
        ? `<a class="settings-update-rel-link" href="${escHtml(rel.url)}" target="_blank" rel="noreferrer"><span data-icono="enlaceExterno"></span></a>`
        : '';
      const body = (rel.body || '').trim() || tr('settings.release_sin_notas');
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
      ? icono('portapapeles') + ' ' + tr('settings.ver_notas_de_version_1_release')
      : icono('portapapeles') + tr('settings.ver_notas_de_version_releases_pendientes', {p1: pending.length});
    // Cerrado por defecto — el triángulo nativo es poco intuitivo;
    // usamos un botón visible con icono + texto explícito.
    notesHtml = `<details class="settings-update-notes"><summary class="settings-update-notes-toggle">${summaryTxt}</summary>${sectionsHtml}</details>`;
  }

  banner.innerHTML = `
    <div class="settings-update-head">
      ${tr('settings.p1_nueva_version_disponible', {p1: icono('campana')})} <strong>${escHtml(data.current)}</strong> → <strong>${escHtml(data.latest)}</strong> ${simBadge}
    </div>
    ${notesHtml}
    <div class="settings-update-cmd">
      <pre id="settings-update-cmd-pre">${escHtml(cmds)}</pre>
    </div>
    <div class="settings-update-actions">
      <button class="btn btn-primary btn-sm" onclick="copyUpdateCommands()"><span data-icono="portapapeles"></span> <span data-i18n="settings.copiar_comandos"></span></button>
      ${data.release_url ? `<a class="btn btn-secondary btn-sm" href="${escHtml(data.release_url)}" target="_blank" rel="noreferrer"><span data-icono="enlaceExterno"></span> <span data-i18n="settings.release_en_github"></span></a>` : ''}
      <button class="btn btn-ghost btn-sm" onclick="ignoreUpdate('${escHtml(data.latest)}')" data-i18n="settings.ignorar_esta_version"></button>
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
  showToast(ok ? tr('settings.comandos_copiados_al_portapapeles') : tr('tab1.no_se_pudo_copiar_al_portapapeles'), ok ? 'success' : 'error');
}

async function ignoreUpdate(version) {
  await apiFetch('/api/version/ignore-update', {
    method: 'POST',
    body: JSON.stringify({ version }),
  });
  showToast(version
    ? icono('omitida') + ' ' + tr('settings.aviso_de_p1_silenciado', {p1: version})
    : tr('settings.avisos_de_actualizacion_reactivados'), 'info');
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
    // `default` es la clave que trae la app: cuenta como configurada, pero no
    // la ha puesto el usuario — de ahí que no lleve el visto (no hay nada que
    // confirmar) ni la cola de 4 caracteres (el backend no la manda).
    const srcLabel = st.source === 'env'     ? tr('settings.desde_env')
                   : st.source === 'default' ? tr('settings.clave_de_la_app')
                   : tr('settings.personalizada');
    const cls = st.source === 'env'     ? 'env'
              : st.source === 'default' ? 'default'
              : 'ok';
    badge.className = 'settings-status ' + cls;
    const tail = st.last4 ? ' · …' + escHtml(st.last4) : '';
    badge.innerHTML = (st.source === 'default' ? '' : icono('check') + ' ')
                    + escHtml(srcLabel) + tail;
    if (inp) {
      inp.placeholder = st.source === 'default'
        ? tr('ui.opcional_pega_la_tuya_solo_si')
        : tr('settings.ya_configurada_escribe_para_reemplazar', {last4: st.last4 || ''});
    }
    return st.source === 'settings';
  }
  badge.className = 'settings-status warn';
  badge.textContent = tr('cmv40_modals.no_configurada');
  if (inp) {
    inp.placeholder = key === 'tmdb'
      ? tr('settings.pega_aqui_tu_clave_de_la_api')
      : tr('ui.pega_aqui_tu_clave_de_la');
  }
  return false;
}

function _renderSettingsDriveFolder(data) {
  const badge = document.getElementById('settings-drive-folder-status');
  const inp = document.getElementById('settings-drive-folder-input');
  if (!badge) return false;
  const st = data.drive_folder || {};
  if (st.configured) {
    // `default` es el repo que trae la app. Igual que con TMDb: cuenta como
    // configurado, pero sin visto verde — no lo ha puesto el usuario.
    const srcLabel = st.source === 'env'     ? tr('settings.desde_env')
                   : st.source === 'default' ? tr('settings.repo_de_la_app')
                   : tr('settings.personalizado');
    const cls = st.source === 'env'     ? 'env'
              : st.source === 'default' ? 'default'
              : 'ok';
    badge.className = 'settings-status ' + cls;
    const idTail = st.folder_id_last6 ? ` · ID …${st.folder_id_last6}` : '';
    badge.innerHTML = (st.source === 'default' ? '' : icono('check') + ' ')
                    + escHtml(srcLabel) + escHtml(idTail);
    if (inp) {
      inp.placeholder = st.source === 'default'
        ? tr('ui.opcional_pega_tu_enlace_si_has')
        : tr('settings.ya_configurado_escribe_una_url');
    }
    return st.source === 'settings';
  }
  badge.className = 'settings-status warn';
  badge.innerHTML = icono('aviso') + ' ' + tr('settings.sin_url_repo_bloqueado');
  if (inp) inp.placeholder = 'https://drive.google.com/drive/folders/…';
  return false;
}

function _renderSettingsSheet(data) {
  const badge = document.getElementById('settings-sheet-status');
  const inp = document.getElementById('settings-sheet-input');
  const resetBtn = document.getElementById('settings-sheet-reset');
  if (!badge) return false;
  const st = data.sheet || {};
  const srcLabel = st.source === 'env' ? tr('settings.desde_env')
                 : st.source === 'settings' ? tr('settings.personalizado')
                 : tr('settings.default_publico');
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

/**
 * Pinta los tres botones de idioma.
 *
 * Cada uno lleva su nombre EN SU PROPIA LENGUA («English», «Català»), no
 * traducido al idioma activo: quien abre los ajustes porque la app está en un
 * idioma que no entiende tiene que poder reconocer el suyo.
 */
/** Los idiomas que ofrecer, SEGÚN EL SERVIDOR.
 *
 *  `IDIOMAS` (i18n.js) es una constante del frontend y el servidor manda
 *  además `idioma.disponibles`: son DOS listas de lo mismo. Añadir un
 *  catálogo en el servidor sin tocar la constante no pintaba el botón, y al
 *  revés pintaba uno que no funciona — y ninguna de las dos cosas da un
 *  error. Manda el servidor, que es quien tiene los catálogos; `IDIOMAS`
 *  queda como la tabla de NOMBRES (cada idioma escrito en su propia lengua,
 *  que es lo que permite encontrarlo sin entender el idioma actual) y como
 *  respaldo si la respuesta no trae la lista.
 *
 *  Un idioma que el servidor ofrezca y la tabla no conozca sale con su
 *  código en mayúsculas: se puede elegir, que es lo que importa.
 */
function _idiomasOfrecidos(data) {
  const nombres = new Map(IDIOMAS.map(i => [i.codigo, i.nombre]));
  const codigos = (data && data.idioma && Array.isArray(data.idioma.disponibles)
                   && data.idioma.disponibles.length)
    ? data.idioma.disponibles
    : IDIOMAS.map(i => i.codigo);
  return codigos.map(c => ({ codigo: c, nombre: nombres.get(c) || c.toUpperCase() }));
}

function _renderSettingsIdioma(data) {
  const caja = document.getElementById('settings-idiomas');
  if (!caja) return;
  const activo = (data.idioma && data.idioma.activo) || idiomaActivo();
  caja.innerHTML = _idiomasOfrecidos(data).map(i => `
    <button class="btn btn-sm settings-idioma${i.codigo === activo ? ' activo' : ''}"
            onclick="pedirCambioDeIdioma('${i.codigo}')"
            ${i.codigo === activo ? 'disabled' : ''}
            ><span class="settings-idioma-bandera">${distintivoDeIdioma(i.codigo)}</span
            ><span>${escHtml(i.nombre)}</span></button>
  `).join('');
}

// Los cuatro campos del formulario de Configuración.
const _CAMPOS_DE_AJUSTES = [
  'settings-tmdb-input', 'settings-google-input',
  'settings-drive-folder-input', 'settings-sheet-input',
];

/** Deja constancia de con qué valor se pintó cada campo.
 *
 *  Es lo que permite saber después si el usuario ha tocado algo. Se llama
 *  cuando el formulario queda sincronizado con el servidor: al pintarlo y
 *  al terminar de guardar.
 */
function _marcarAjustesComoGuardados() {
  for (const id of _CAMPOS_DE_AJUSTES) {
    const inp = document.getElementById(id);
    if (inp) inp.dataset.inicial = inp.value || '';
  }
}

/** Los campos que el usuario ha tocado y no ha guardado.
 *
 *  **No basta con mirar si están vacíos.** Tres se pintan en blanco —de una
 *  clave configurada solo se enseña el `last4` en el placeholder— pero el
 *  del sheet **viene pre-poblado con la URL activa**, que es pública y se
 *  enseña a propósito. Compararlo con la cadena vacía daba «tienes cambios
 *  sin guardar» SIEMPRE, sin haber tocado nada, que es justo lo que el aviso
 *  no debe hacer: un diálogo que sale siempre se aprende a cerrar sin leer.
 *
 *  Tampoco vale reusar el criterio de `saveSettings` —«¿mandaría algo?»—,
 *  porque ese compara el sheet con la URL POR DEFECTO: a quien tenga una
 *  propia guardada le saldría el aviso igual sin tocar nada.
 *
 *  Lo que se compara es contra el valor CON EL QUE SE PINTÓ el campo. Es
 *  exacto, no depende de la semántica de ninguno, y un campo nuevo que
 *  nadie marque cuenta como «cambiado» en cuanto tenga texto, que es el
 *  default conservador.
 */
function _ajustesSinGuardar() {
  return _CAMPOS_DE_AJUSTES.filter(id => {
    const inp = document.getElementById(id);
    if (!inp) return false;
    return (inp.value || '').trim() !== (inp.dataset.inicial || '').trim();
  });
}

/** Cambiar de idioma recarga la página; antes, preguntar si hay que perder algo.
 *
 *  `cambiarIdioma` hace `location.reload()`, así que lo escrito y no guardado
 *  se va. Ya pasaba, pero con las claves en OTRA sección deja de ser evidente:
 *  al pulsar el idioma no las tienes delante. Mismo patrón que Tab 2 al cerrar
 *  un MKV con cambios pendientes.
 */
async function pedirCambioDeIdioma(codigo) {
  if (!_ajustesSinGuardar().length) return cambiarIdioma(codigo);
  const que = await _confirmarCambioDeIdioma();
  if (que === 'cancel') return;
  // Si el guardado falla no se recarga: el usuario se quedaría sin el error
  // y sin la clave.
  if (que === 'guardar' && !(await saveSettings())) return;
  return cambiarIdioma(codigo);
}

/** El diálogo de tres salidas, con el patrón de `_seriesConfirmConflicts`:
 *  `showConfirm` es de callback y el tercer botón se inserta a mano. */
function _confirmarCambioDeIdioma() {
  return new Promise(resolve => {
    showConfirm(
      tr('ajustes.idioma_cambios_titulo'),
      tr('ajustes.idioma_cambios_texto'),
      () => resolve('guardar'),
      tr('ajustes.idioma_guardar_y_cambiar'),
    );
    const sinGuardar = document.createElement('button');
    sinGuardar.className = 'btn btn-secondary btn-sm confirm-extra-btn';
    sinGuardar.textContent = tr('ajustes.idioma_cambiar_sin_guardar');
    sinGuardar.onclick = () => {
      closeModal('confirm-modal');
      resolve('descartar');
    };
    const ok = document.getElementById('confirm-ok-btn');
    if (ok) ok.parentNode.insertBefore(sinGuardar, ok);
    const cancelar = document.querySelector('#confirm-modal .btn-ghost');
    if (cancelar) {
      const alCancelar = () => {
        cancelar.removeEventListener('click', alCancelar);
        resolve('cancel');
      };
      cancelar.addEventListener('click', alCancelar);
    }
  });
}

/** Los tres botones del tema. Mismo patrón que el de idioma. */
function _renderSettingsTema(data) {
  const caja = document.getElementById('settings-temas');
  if (!caja) return;
  // El ajuste es la PREFERENCIA (`sistema` incluido), no el color resuelto:
  // marcar «Oscuro» porque el Mac está en oscuro sería mentir sobre lo que
  // hay guardado, y dejaría al usuario sin saber que sigue al sistema.
  const activo = (data && data.tema && data.tema.activo)
                 || document.documentElement.dataset.temaPref || 'claro';
  const OPCIONES = [
    {codigo: 'claro',   ico: 'sol'},
    {codigo: 'oscuro',  ico: 'luna'},
    {codigo: 'sistema', ico: 'pantalla'},
  ];
  caja.innerHTML = OPCIONES.map(o => `
    <button class="btn btn-sm settings-tema${o.codigo === activo ? ' activo' : ''}"
            onclick="cambiarTema('${o.codigo}')"
            ${o.codigo === activo ? 'disabled' : ''}
            ><span class="settings-tema-ico">${icono(o.ico)}</span
            ><span data-i18n="ajustes.tema.${o.codigo}"></span></button>
  `).join('');
  pintarTextos(caja);
}

/** Cambia el tema y lo persiste.
 *
 *  A diferencia del idioma, esto **no recarga**: el tema es sólo CSS y el
 *  cambio se ve en el acto, así que no hay nada escrito en el formulario que
 *  se pueda perder ni un catálogo que volver a pedir.
 */
async function cambiarTema(codigo) {
  aplicarTema(codigo);
  window.__TEMA_PREF = codigo;
  try { localStorage.setItem('tema', codigo); } catch (e) { /* modo privado */ }
  try {
    const data = await apiFetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tema: codigo}),
    });
    _renderSettingsTema(data);
  } catch (e) {
    // Se queda aplicado en esta pestaña aunque no se haya podido guardar:
    // deshacerlo delante del usuario sería peor que avisar.
    showToast(tr('ajustes.tema_no_guardado'), 'warning');
  }
}

function _renderSettings(data) {
  _renderSettingsTema(data);
  _renderSettingsIdioma(data);
  const tmdbUserSet   = _renderSettingsSection('tmdb', data);
  const googleUserSet = _renderSettingsSection('google', data);
  const driveUserSet  = _renderSettingsDriveFolder(data);
  const sheetUserSet  = _renderSettingsSheet(data);
  _hayClavesDelUsuario = tmdbUserSet || googleUserSet || driveUserSet || sheetUserSet;
  _actualizarBotonBorrar();
  // Aquí es donde el sheet se pre-pobla, así que la foto se toma después.
  _marcarAjustesComoGuardados();
}

// ¿Hay alguna clave o URL puesta POR EL USUARIO? Lo decide `_renderSettings`
// al leer la respuesta; el botón lo consulta cada vez que cambia la sección.
let _hayClavesDelUsuario = false;

/** El botón rojo del pie se ve si hay algo que borrar y estás donde vive. */
function _actualizarBotonBorrar() {
  const btn = document.getElementById('settings-clear-btn');
  if (!btn) return;
  const sec = SECCIONES_AJUSTES.find(s => s.id === _seccionDeAjustes);
  btn.style.display = (_hayClavesDelUsuario && sec && sec.tieneClaves) ? '' : 'none';
}

async function _testKeyGeneric(key, fieldKey, endpoint, payloadKey) {
  const inp = document.getElementById(`settings-${fieldKey}-input`);
  const fb  = document.getElementById(`settings-${fieldKey}-feedback`);
  const btn = document.getElementById(`settings-${fieldKey}-test`);
  const value = (inp?.value || '').trim();
  if (!fb || !btn) return;
  // TMDb con el campo vacío prueba la clave ACTIVA —la de la app, si el
  // usuario no ha puesto la suya—, que es la pregunta que trae aquí a nadie:
  // «¿sigue viva?». Las otras tres necesitan un valor sí o sí: no hay
  // ninguna por defecto que probar.
  if (!value && key !== 'tmdb') {
    fb.textContent = key === 'drive-folder'
      ? tr('settings.pega_la_url_del_folder_drive')
      : key === 'sheet'
      ? tr('settings.pega_la_url_del_sheet_para')
      : tr('settings.introduce_una_clave_de_la');
    fb.className = 'settings-feedback info';
    return;
  }
  btn.disabled = true;
  fb.textContent = tr('settings.probando');
  fb.className = 'settings-feedback info';
  const body = {};
  if (value) body[payloadKey] = value;
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
    if (fb) { fb.textContent = tr('settings.url_restaurada_al_default_publico'); fb.className = 'settings-feedback ok'; }
    showToast(tr('settings.url_del_sheet_restaurada'), 'success');
  });
}

/** Guarda las claves del formulario. Devuelve `true` si se guardó.
 *
 *  El retorno lo estrenó `pedirCambioDeIdioma`: si el guardado falla, NO se
 *  puede recargar la página encima — el usuario se quedaría sin el error y
 *  sin la clave. Antes no devolvía nada y no había forma de distinguirlo.
 */
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
    return true;
  }
  btn.disabled = true;
  const data = await apiFetch('/api/settings', {
    method: 'POST', body: JSON.stringify(payload),
  });
  btn.disabled = false;
  if (!data) return false;
  _settingsCache = data;
  _renderSettings(data);
  if (tk && tmdbInp)        { tmdbInp.value = '';        if (fbTmdb)   { fbTmdb.textContent = tr('settings.guardada');   fbTmdb.className = 'settings-feedback ok'; } }
  if (gk && googleInp)      { googleInp.value = '';      if (fbGoogle) { fbGoogle.textContent = tr('settings.guardada'); fbGoogle.className = 'settings-feedback ok'; } }
  if (du && driveFolderInp) { driveFolderInp.value = ''; if (fbDrive)  { fbDrive.textContent = tr('settings.guardada');  fbDrive.className = 'settings-feedback ok'; } }
  if (payload.cmv40_sheet_url && fbSheet) { fbSheet.textContent = tr('settings.guardada'); fbSheet.className = 'settings-feedback ok'; }
  showToast(tr('settings.configuracion_guardada'), 'success');
  _marcarAjustesComoGuardados();
  return true;
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
  showToast(tr('settings.claves_y_urls_borradas'), 'info');
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
  btn.innerHTML = icono('reloj') + ' ' + tr('settings.escaneando');
  resultEl.innerHTML = '';

  const data = await apiFetch('/api/cleanup/scan');
  btn.disabled = false;
  btn.innerHTML = icono('lupa') + ' ' + tr('ui.escanear_huerfanos');

  if (!data) return;
  if (!data.items || !data.items.length) {
    resultEl.innerHTML = '<div class="settings-cleanup-empty"><span data-icono="check"></span> <span data-i18n="settings.no_se_encontraron_huerfanos_todo_limpio"></span></div>';
    return;
  }

  // Render tabla con checkboxes (default: marcado solo si safe=true)
  const rows = data.items.map((it, i) => {
    const checked = it.safe ? 'checked' : '';
    const warnIcon = it.safe ? '' : '<span class="cleanup-warn" data-i18n-tip="settings.reciente_o_potencialmente_activo_revisa_antes"><span data-icono="aviso"></span></span>';
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
      <strong>${data.total_count}</strong> ${tr('settings.elementos_liberables_total_bytes_p2', {total_bytes: _cleanupFmtBytes(data.total_bytes), p2: data.safe_count < data.total_count
        ? ` · <span class="cleanup-warn-text">${tr('settings.safe_count_requieren_revision', {safe_count: data.total_count - data.safe_count})}</span>`
        : ''})}
    </div>
    <table class="cleanup-table">
      <thead>
        <tr>
          <th><input type="checkbox" id="cleanup-select-all" title="${tr('cmv40_modals.seleccionar_todo')}"></th>
          <th data-i18n="settings.limpieza_tipo"></th>
          <th data-i18n="settings.limpieza_ruta"></th>
          <th data-i18n="ui.tamano"></th>
          <th data-i18n="settings.limpieza_edad"></th>
          <th data-i18n="tab3.motivo"></th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="cleanup-actions">
      <button class="btn btn-ghost btn-sm" onclick="document.getElementById('settings-cleanup-result').innerHTML=''" data-i18n="ui.cancelar"></button>
      <button class="btn btn-danger btn-sm" onclick="cleanupExecuteSelected()"><span data-icono="papelera"></span> <span data-i18n="ui.borrar_seleccionados"></span></button>
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
    showToast(tr('cmv40_modals.no_hay_nada_seleccionado'), 'info');
    return;
  }
  // Confirmacion via modal nativo del proyecto
  showConfirm(
    tr('settings.borrar_n_elementos', {n: paths.length}),
    tr('settings.esta_operacion_es_irreversible_asegurate'),
    async () => {
      const data = await apiFetch('/api/cleanup/execute', {
        method: 'POST',
        body: JSON.stringify({ paths }),
      }, API_FETCH_TIMEOUT_LARGO);
      if (!data) return;
      const okCount = (data.deleted || []).length;
      const koCount = (data.failed || []).length;
      const freed = _cleanupFmtBytes(data.total_freed_bytes || 0);
      if (koCount === 0) {
        showToast(tr('settings.borrados_n_elementos_liberados', {n: okCount, freed: freed}), 'success');
      } else {
        showToast(tr('settings.borrados_n_fallaron_liberados', {n: okCount, ko: koCount, freed: freed}), 'warning');
      }
      // Re-escanear para refrescar el listado
      cleanupScanAndShow();
    },
    tr('tab2.borrar'),
  );
}

// Banner explicativo cuando el Repo DoviTools no está accesible. Cubre 3
// casos: falta folder URL (paywall), falta Google API key, o ambos. El
// primero es el más importante — el acceso al repo es privado (donación al
// autor) y merece explicación clara + link al PayPal.
function _cmv40RepoUnavailableBanner(repo) {
  const folderOk = !!(repo && repo.drive_folder_configured);
  const keyOk    = !!(repo && repo.google_key_configured);
  const openCfg = `<a href="#" onclick="openSettingsModal();return false"><span data-icono="ajustes"></span> <span data-i18n="ui.configuracion"></span></a>`;
  const donate  = `<a href="https://www.paypal.com/donate/?hosted_button_id=6ML5KUZG9XGB6" target="_blank" rel="noreferrer" data-i18n="settings.paypal_rec_9999"></a>`;
  if (!folderOk && !keyOk) {
    return `<div class="cmv40-repo-locked">
      <div class="cmv40-repo-locked-title"><span data-icono="candado"></span> <span data-i18n="settings.repositorio_dovitools_bloqueado"></span></div>
      <div class="cmv40-repo-locked-body">
        ${tr('settings.faltan_dos_cosas')}
        <ol>
          <li>${tr('settings.li_url_del_folder_es_privado', {donate: donate})}</li>
          <li>${tr('settings.li_google_api_key_con_apis')}</li>
        </ol>
        ${tr('settings.configura_ambas_en_opencfg', {opencfg: openCfg})}
      </div>
    </div>`;
  }
  if (!folderOk) {
    return `<div class="cmv40-repo-locked">
      <div class="cmv40-repo-locked-title"><span data-icono="candado"></span> <span data-i18n="settings.repositorio_dovitools_bloqueado"></span></div>
      <div class="cmv40-repo-locked-body">
        ${tr('settings.la_url_apunta_a_un_repo_privado', {donate: donate})}
        <br><br>${tr('settings.una_vez_tengas_el_link_pegalo', {opencfg: openCfg})} <em data-i18n="ui.url_del_repositorio_dovitools"></em>.
      </div>
    </div>`;
  }
  if (!keyOk) {
    return `<div class="cmv40-repo-locked">
      <div class="cmv40-repo-locked-title"><span data-icono="aviso"></span> <span data-i18n="settings.google_api_key_no_configurada"></span></div>
      <div class="cmv40-repo-locked-body">
        ${tr('settings.la_url_del_repo_esta_ok', {opencfg: openCfg})}
      </div>
    </div>`;
  }
  return `<div class="cmv40-repo-locked">
    <div class="cmv40-repo-locked-title"><span data-icono="aviso"></span> <span data-i18n="settings.repo_dovitools_no_accesible"></span></div>
    <div class="cmv40-repo-locked-body">
      ${escHtml(repo?.error || tr('comun.error_desconocido'))}
    </div>
  </div>`;
}

// ═══════════════════════════════════════════════════════════════════
//  AVISO AL TERMINAR UN TRABAJO — controles de ⚙︎ Configuración
// ═══════════════════════════════════════════════════════════════════
//
// La lógica vive en core.js (`avisarFinDeTrabajo` y la vigilancia); aquí
// sólo están los controles y el texto de estado, que tiene que explicar por
// qué las notificaciones del escritorio pueden no estar disponibles: sobre el
// NAS la app se sirve por HTTP y la Notification API exige contexto seguro.

/** Refresca los checks y el estado. Lo llama el render del modal. */
function renderAvisoFinSettings() {
  const check = document.getElementById('settings-aviso-check');
  const sonido = document.getElementById('settings-aviso-sonido-check');
  const btn = document.getElementById('settings-aviso-permiso-btn');
  const status = document.getElementById('settings-aviso-status');
  if (!check) return;

  check.checked = avisoFinActivado();
  if (sonido) sonido.checked = avisoSonidoActivado();

  let texto, clase;
  if (!avisoFinActivado()) {
    texto = tr('settings.desactivado'); clase = 'settings-status';
  } else if (!avisoNotificacionDisponible()) {
    texto = tr('settings.titulo_de_la_pestana_sin_https');
    clase = 'settings-status ok';
  } else if (Notification.permission === 'granted') {
    texto = tr('settings.titulo_notificacion_del_escritorio'); clase = 'settings-status ok';
  } else if (Notification.permission === 'denied') {
    texto = tr('settings.titulo_de_la_pestana_notificaciones_bloqueadas');
    clase = 'settings-status ok';
  } else {
    texto = tr('settings.titulo_de_la_pestana'); clase = 'settings-status ok';
  }
  if (status) { status.textContent = texto; status.className = clase; }

  // El botón de permiso sólo tiene sentido si se puede pedir.
  if (btn) {
    const puede = avisoFinActivado() && avisoNotificacionDisponible()
                  && Notification.permission === 'default';
    btn.style.display = puede ? '' : 'none';
  }
}

function onToggleAvisoFin(on) {
  setAvisoFinActivado(on);
  renderAvisoFinSettings();
}

function onToggleAvisoSonido(on) {
  setAvisoSonidoActivado(on);
  if (on) _pitido();          // que se oiga lo que se acaba de activar
  renderAvisoFinSettings();
}

async function onPedirPermisoNotificaciones() {
  const res = await pedirPermisoNotificaciones();
  renderAvisoFinSettings();
  if (res === 'granted') showToast(tr('settings.notificaciones_activadas'), 'success');
  else if (res === 'denied') showToast(tr('settings.notificaciones_bloqueadas_en_el_navegador'), 'info');
}

/** Botón tr('ui.probar'): dispara el aviso completo sin esperar a un job real. */
function probarAvisoFin() {
  if (!avisoFinActivado()) { showToast(tr('settings.activalo_primero_para_probarlo'), 'info'); return; }
  avisarFinDeTrabajo(1);
  showToast(tr('settings.mira_el_titulo_de_la_pestana'), 'info', 5000);
}
