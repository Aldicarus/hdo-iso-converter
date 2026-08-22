'use strict';
/**
 * browser.js — El file browser modal, reusable.
 *
 * Lo comparten Tab 2 ("Abrir MKV") y Tab 3 ("MKV origen"); cada uno pide los
 * roots que le interesan. Se carga al final porque nadie lo necesita en el
 * arranque.
 */

// ══════════════════════════════════════════════════════════════════════
//  FILE BROWSER — modal reusable para navegar /mnt/library
//  Usado en Tab 3 para seleccionar el MKV origen del proceso CMv4.0.
//  El backend (`/api/library/browse`) sirve subdirs + ficheros .mkv.
// ══════════════════════════════════════════════════════════════════════

const _fileBrowser = {
  // Roots disponibles. 1 root = sin selector visible. 2+ = pills arriba.
  roots: [],            // [{key, label, icon}]
  rootKey: 'library',   // key del root activo
  base: '/mnt/library', // ruta absoluta del root activo (devuelta por backend)
  currentPath: '',      // relativa al root activo
  parent: null,
  entries: [],
  filter: '',
  onSelect: null,
  // Selección actual — null hasta que el usuario haga click en una fila
  // de fichero. El boton "Seleccionar" del footer queda disabled hasta
  // entonces; click sobre otra fila reemplaza la seleccion. Doble-click
  // sobre una fila confirma directamente (atajo power-user).
  selectedRel: null,
  selectedName: null,
};

const _DEFAULT_FB_ROOTS = [
  { key: 'library', label: 'Biblioteca', icon: '📚' },
];
const _FB_ROOT_LABELS = {
  library:    'Biblioteca',
  output:     'Output',
  downloaded: 'Downloaded',
};

/** Abre el modal del file browser.
 *  opts: { title, subtitle, roots, onSelect }
 *    - roots: array de {key, label, icon}. Default = [Biblioteca].
 *      Si hay 2+ roots, se muestra un selector de pills arriba.
 *      El primer root del array es el que se carga al abrir.
 *  CRUCIAL: el modal se abre ANTES de hacer el fetch para evitar gaps
 *  visuales (la app de fondo no debe ser interactuable). La lista
 *  muestra "⏳ Cargando…" hasta que el fetch termina. */
async function openFileBrowser({ title, subtitle, roots, onSelect } = {}) {
  _fileBrowser.onSelect = onSelect || null;
  _fileBrowser.filter = '';
  _fileBrowser.selectedRel = null;
  _fileBrowser.selectedName = null;
  _fileBrowser.roots = (roots && roots.length) ? roots : _DEFAULT_FB_ROOTS;
  _fileBrowser.rootKey = _fileBrowser.roots[0].key;

  const titleEl = document.getElementById('file-browser-title');
  const subEl = document.getElementById('file-browser-sub');
  const searchEl = document.getElementById('file-browser-search');
  const listEl = document.getElementById('file-browser-list');
  const bcEl = document.getElementById('file-browser-breadcrumb');
  const baseEl = document.getElementById('file-browser-base');
  const statsEl = document.getElementById('file-browser-stats');
  if (titleEl) titleEl.textContent = title || 'Seleccionar MKV';
  if (subEl) subEl.textContent = subtitle || 'Navega tu biblioteca y elige el fichero';
  if (searchEl) searchEl.value = '';
  // Limpiar restos de aperturas anteriores ANTES de mostrar para no flashear datos viejos
  if (listEl) listEl.innerHTML = '<div class="file-browser-loading">⏳ Cargando…</div>';
  if (bcEl) bcEl.innerHTML = '';
  if (baseEl) baseEl.textContent = '';
  if (statsEl) statsEl.textContent = '';
  _renderFileBrowserRoots();
  // Modal arriba YA — antes de cualquier await. Mantiene cobertura modal
  // sin gaps cuando se invoca durante una transicion (close→open de otro modal).
  openModal('file-browser-modal');
  setTimeout(() => searchEl?.focus(), 80);
  await fileBrowserNavigate('');
}

/** Pinta el selector de roots (pills arriba del breadcrumb). Solo visible
 *  cuando hay 2+ roots configurados; con 1 solo se oculta. */
function _renderFileBrowserRoots() {
  const el = document.getElementById('file-browser-roots');
  if (!el) return;
  if (!_fileBrowser.roots || _fileBrowser.roots.length < 2) {
    el.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  el.style.display = 'flex';
  el.innerHTML = '';
  _fileBrowser.roots.forEach(r => {
    const btn = document.createElement('button');
    btn.className = `fb-root-btn ${r.key === _fileBrowser.rootKey ? 'active' : ''}`;
    btn.innerHTML = `<span class="fb-root-icon">${r.icon || '📁'}</span><span class="fb-root-label">${escHtml(r.label)}</span>`;
    btn.addEventListener('click', () => {
      if (r.key === _fileBrowser.rootKey) return;
      _fileBrowser.rootKey = r.key;
      _renderFileBrowserRoots();
      fileBrowserNavigate('');
    });
    el.appendChild(btn);
  });
}

/** Carga el contenido de `relPath` (relativo al root activo) y re-renderiza.
 *  Limpia la seleccion previa: cambiar de directorio implica reset, igual
 *  que hace Finder/Explorer. */
async function fileBrowserNavigate(relPath) {
  _fileBrowser.selectedRel = null;
  _fileBrowser.selectedName = null;
  _updateFileBrowserConfirmBtn();
  const listEl = document.getElementById('file-browser-list');
  if (listEl) listEl.innerHTML = '<div class="file-browser-loading">⏳ Cargando…</div>';
  try {
    const url = `/api/library/browse?root=${encodeURIComponent(_fileBrowser.rootKey)}&path=${encodeURIComponent(relPath || '')}`;
    const data = await apiFetch(url);
    if (!data) throw new Error('Sin respuesta');
    if (data.error) {
      if (listEl) listEl.innerHTML = `<div class="file-browser-empty">${escHtml(data.error)}</div>`;
      return;
    }
    _fileBrowser.base = data.base || '';
    _fileBrowser.currentPath = data.path || '';
    _fileBrowser.parent = data.parent;
    _fileBrowser.entries = data.entries || [];
    _renderFileBrowser();
  } catch (e) {
    if (listEl) listEl.innerHTML = `<div class="file-browser-empty">⚠ Error: ${escHtml(e.message || String(e))}</div>`;
  }
}

/** Sube un nivel en el árbol (si no estás en la raíz). */
function fileBrowserUp() {
  if (_fileBrowser.parent === null || _fileBrowser.parent === undefined) return;
  fileBrowserNavigate(_fileBrowser.parent);
}

/** Filtra la lista por substring (in-memory, no recarga del servidor). */
function fileBrowserFilter(value) {
  _fileBrowser.filter = (value || '').toLowerCase().trim();
  _renderFileBrowser();
}

function _renderFileBrowser() {
  const listEl = document.getElementById('file-browser-list');
  const bcEl = document.getElementById('file-browser-breadcrumb');
  const baseEl = document.getElementById('file-browser-base');
  const upBtn = document.getElementById('file-browser-up-btn');
  const statsEl = document.getElementById('file-browser-stats');
  if (!listEl || !bcEl) return;

  // ── Breadcrumb (DOM, no strings con encoding) ───────────────────
  const path = _fileBrowser.currentPath || '';
  const parts = path.split('/').filter(Boolean);
  bcEl.innerHTML = '';
  const rootLink = document.createElement('a');
  // Etiqueta del root activo en el inicio del breadcrumb. Buscar en
  // los roots configurados; fallback al mapping de defaults.
  const activeRoot = (_fileBrowser.roots || []).find(r => r.key === _fileBrowser.rootKey);
  const rootIcon = activeRoot?.icon || '📁';
  const rootLabel = activeRoot?.label || _FB_ROOT_LABELS[_fileBrowser.rootKey] || _fileBrowser.rootKey;
  rootLink.textContent = `${rootIcon} ${rootLabel}`;
  rootLink.addEventListener('click', () => fileBrowserNavigate(''));
  bcEl.appendChild(rootLink);
  parts.forEach((part, i) => {
    const subPath = parts.slice(0, i + 1).join('/');
    const isLast = i === parts.length - 1;
    const sep = document.createElement('span');
    sep.className = 'fb-bc-sep';
    sep.textContent = '›';
    bcEl.appendChild(sep);
    if (isLast) {
      const cur = document.createElement('span');
      cur.className = 'fb-bc-current';
      cur.textContent = part;
      bcEl.appendChild(cur);
    } else {
      const link = document.createElement('a');
      link.textContent = part;
      link.addEventListener('click', () => fileBrowserNavigate(subPath));
      bcEl.appendChild(link);
    }
  });
  if (baseEl) baseEl.textContent = _fileBrowser.base + (path ? '/' + path : '');
  if (upBtn) upBtn.disabled = _fileBrowser.parent === null || _fileBrowser.parent === undefined;

  // ── Filtro in-memory ────────────────────────────────────────────
  const filter = _fileBrowser.filter;
  const filtered = filter
    ? _fileBrowser.entries.filter(e => e.name.toLowerCase().includes(filter))
    : _fileBrowser.entries;

  // ── Lista (DOM, no innerHTML con paths encoded) ─────────────────
  listEl.innerHTML = '';
  if (!filtered.length) {
    const empty = document.createElement('div');
    empty.className = 'file-browser-empty';
    empty.textContent = filter
      ? `Sin coincidencias para "${filter}"`
      : '📭 Esta carpeta no contiene MKVs ni subcarpetas.';
    listEl.appendChild(empty);
    if (statsEl) statsEl.textContent = '';
    return;
  }

  const dirs = filtered.filter(e => e.type === 'dir').length;
  const files = filtered.filter(e => e.type === 'file').length;
  if (statsEl) {
    statsEl.textContent = (dirs ? `${dirs} ${dirs === 1 ? 'carpeta' : 'carpetas'}` : '')
                        + (dirs && files ? ' · ' : '')
                        + (files ? `${files} ${files === 1 ? 'MKV' : 'MKVs'}` : '');
  }

  filtered.forEach(e => {
    // childRel: ruta RELATIVA al base, sin encoding (lo encoda
    // fileBrowserNavigate en su fetch — antes había doble encoding
    // que rompía paths con espacios o tildes)
    const childRel = path ? `${path}/${e.name}` : e.name;
    const row = document.createElement('div');
    row.className = `file-browser-row ${e.type}`;
    if (e.type === 'file' && _fileBrowser.selectedRel === childRel) {
      row.classList.add('selected');
    }
    row.tabIndex = 0;
    row.innerHTML = `
      <span class="fb-icon">${e.type === 'dir' ? '📁' : '🎬'}</span>
      <span class="fb-name">${escHtml(e.name)}</span>
      ${e.type === 'file' ? `<span class="fb-size">${_fmtBytes(e.size_bytes)}</span>` : ''}
    `;
    if (e.type === 'dir') {
      // Carpetas: click navega (entrar es la unica accion posible)
      row.addEventListener('click', () => fileBrowserNavigate(childRel));
    } else {
      // Ficheros: click SELECCIONA (visual highlight + boton "Seleccionar"
      // habilitado). Confirmar requiere clicar el boton del footer o
      // doble-click sobre la fila (atajo power-user).
      row.addEventListener('click', () => _fileBrowserSelectRow(childRel, e.name));
      row.addEventListener('dblclick', () => _fileBrowserConfirmSelection());
    }
    // Soporte teclado: Enter activa la fila (mismo flujo que click)
    row.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        row.click();
      }
    });
    listEl.appendChild(row);
  });
  _updateFileBrowserConfirmBtn();
}

/** Marca una fila de fichero como seleccionada (sin confirmar). */
function _fileBrowserSelectRow(rel, name) {
  _fileBrowser.selectedRel = rel;
  _fileBrowser.selectedName = name;
  // Re-render para refrescar el highlight visual + estado del boton
  _renderFileBrowser();
}

/** Confirma la seleccion actual: llama a onSelect y cierra el modal. */
function _fileBrowserConfirmSelection() {
  if (!_fileBrowser.selectedRel) return;
  _fileBrowserSelect(_fileBrowser.selectedRel, _fileBrowser.selectedName);
}

/** Habilita/deshabilita el boton "Seleccionar" segun haya seleccion. */
function _updateFileBrowserConfirmBtn() {
  const btn = document.getElementById('file-browser-confirm-btn');
  if (!btn) return;
  btn.disabled = !_fileBrowser.selectedRel;
}

/** Confirma selección de un fichero. Espera a que onSelect termine (puede
 *  abrir otro modal con su propio fetch) ANTES de cerrar el browser → asi
 *  el usuario nunca ve la app de fondo: el browser cubre la transicion
 *  hasta que el siguiente modal este renderizado. Mientras espera, el
 *  browser se "congela" deshabilitando pointer events para que el usuario
 *  no pueda lanzar otro click sobre filas. */
async function _fileBrowserSelect(relPath, name) {
  const absPath = `${_fileBrowser.base.replace(/\/$/, '')}/${relPath}`;
  const modal = document.getElementById('file-browser-modal');
  if (modal) modal.style.pointerEvents = 'none';
  try {
    if (typeof _fileBrowser.onSelect === 'function') {
      await _fileBrowser.onSelect(absPath, name);
    }
  } catch (e) {
    console.error('FileBrowser onSelect error:', e);
  } finally {
    if (modal) modal.style.pointerEvents = '';
    closeModal('file-browser-modal');
  }
}
