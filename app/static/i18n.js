'use strict';
// ═══════════════════════════════════════════════════════════════════
//  i18n — castellano, inglés y catalán
// ═══════════════════════════════════════════════════════════════════
//
// Dos mecanismos, y hacen falta los dos:
//
//   · `data-i18n="clave"` en el marcado, que lo resuelve `pintarTextos()` más
//     un MutationObserver. Es el patrón de `data-icono`, ya en producción:
//     el marcado DECLARA y el pintor resuelve, así que funciona igual en
//     `index.html` y en el HTML que genera el JS. Sirve para las etiquetas
//     estáticas, que son casi la mitad.
//   · `t('clave', {param: valor})` llamado desde el JS, para todo lo que se
//     construye con datos dentro. `data-i18n` no puede con esto: un
//     `<div>Máximo ${MAX} proyectos</div>` no es una etiqueta, es un mensaje.
//
// El idioma se elige una vez y **la página se recarga**. Podría re-renderizarse
// en vivo, pero la app tiene miles de nodos ya pintados y media docena de
// pollers en marcha: recargar es una línea, es instantáneo y no deja mitades
// en el idioma viejo. Cambiar de idioma no es algo que se haga cada minuto.

/** Las tres. El orden es el del selector. */
const IDIOMAS = [
  { codigo: 'es', nombre: 'Castellano' },
  { codigo: 'en', nombre: 'English' },
  { codigo: 'ca', nombre: 'Català' },
];

const IDIOMA_POR_DEFECTO = 'es';
const IDIOMA_PREF = 'hdo_idioma';

// El token de caché sale del `src` de ESTE script. Pasarlo a mano desde
// `index.html` sería una novena referencia al token que mantener sincronizada,
// y el desajuste parcial es peor que no tenerlo (ya pasó con los ocho assets).
const TOKEN_I18N = (() => {
  try {
    const src = (document.currentScript && document.currentScript.src) || '';
    return new URL(src, location.href).searchParams.get('v') || '';
  } catch (e) { return ''; }
})();

let _catalogo = {};
let _idioma = IDIOMA_POR_DEFECTO;
// Claves pedidas que no existen. Se acumulan en vez de avisar una por una: en
// una vuelta de render se piden cientos, y un toast por cada una tapa la app.
const _ausentes = new Set();

/** El idioma activo (`'es'` | `'en'` | `'ca'`). */
function idiomaActivo() { return _idioma; }

/** Las claves que se han pedido y no existen. Lo usa el guard de la suite. */
function clavesAusentes() { return [..._ausentes]; }

/**
 * El idioma que el navegador debe usar en este arranque.
 *
 * Sale de `localStorage` y NO del servidor, a propósito: el ajuste del
 * servidor es la fuente de verdad —y es quien traduce sus propios mensajes—
 * pero preguntárselo antes del primer render añadiría una petición en el
 * camino crítico para pintar la página en el idioma correcto. El selector
 * escribe los dos a la vez, así que solo divergen si alguien edita
 * `app_settings.json` a mano; `reconciliarIdioma()` lo arregla al vuelo.
 */
function idiomaGuardado() {
  try {
    const v = localStorage.getItem(IDIOMA_PREF);
    if (v && IDIOMAS.some(i => i.codigo === v)) return v;
  } catch (e) { /* localStorage puede estar bloqueado */ }
  return IDIOMA_POR_DEFECTO;
}

/**
 * Carga el catálogo del idioma. Hay que esperarla ANTES del primer render.
 *
 * Si falla —fichero que falta, JSON roto, disco del NAS ocupado— se cae al
 * castellano y se sigue: una app en el idioma equivocado se usa, una app en
 * blanco no. El `?v=` es el mismo token de caché que el resto de los assets.
 */
async function cargarIdioma(codigo, token = TOKEN_I18N) {
  const pedido = IDIOMAS.some(i => i.codigo === codigo) ? codigo : IDIOMA_POR_DEFECTO;
  try {
    const r = await fetch(`/static/i18n/${pedido}.json?v=${token || ''}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    _catalogo = await r.json();
    _idioma = pedido;
  } catch (e) {
    console.error('[i18n] no se pudo cargar el catálogo', pedido, e);
    if (pedido !== IDIOMA_POR_DEFECTO) return cargarIdioma(IDIOMA_POR_DEFECTO, token);
    _catalogo = {};
    _idioma = IDIOMA_POR_DEFECTO;
  }
  return _idioma;
}

/**
 * El texto de una clave, con sus parámetros sustituidos.
 *
 * Los parámetros van **con nombre** (`{max}`) y nunca por posición: el orden
 * de las palabras cambia entre lenguas, y una plantilla posicional obliga a
 * que no cambie.
 *
 * Una clave que no existe devuelve la propia clave. Es deliberado: se ve en
 * pantalla, se ve en `clavesAusentes()` y la caza un test. Devolver cadena
 * vacía dejaría huecos silenciosos, que es el fallo que no se reporta.
 */
function t(clave, params) {
  let txt = _catalogo[clave];
  if (typeof txt !== 'string') {
    _ausentes.add(clave);
    return clave;
  }
  if (params) {
    txt = txt.replace(/\{(\w+)\}/g, (crudo, nombre) =>
      Object.prototype.hasOwnProperty.call(params, nombre)
        ? String(params[nombre]) : crudo);
  }
  return txt;
}

/** ¿Existe la clave? Para ramas que quieren decidir sin ensuciar `_ausentes`. */
function hayTexto(clave) { return typeof _catalogo[clave] === 'string'; }

// ── El marcado declara: data-i18n ──────────────────────────────────
//
// Cada atributo dice DÓNDE va el texto. Son cinco y no uno genérico
// (`data-i18n-attr="placeholder:clave"`) porque el genérico hay que parsearlo
// en cada nodo y se equivoca en silencio si el formato no cuadra.
const _DESTINOS = [
  ['i18n',     (el, s) => { el.textContent = s; }],
  ['i18nHtml', (el, s) => { el.innerHTML = s; }],
  ['i18nPh',   (el, s) => { el.placeholder = s; }],
  ['i18nTip',  (el, s) => { el.setAttribute('data-tooltip', s); }],
  ['i18nAria', (el, s) => { el.setAttribute('aria-label', s); }],
];

/**
 * Resuelve los `data-i18n*` de un árbol.
 *
 * Marca `data-i18n-puesto` para no repintar en cada vuelta del observador, y
 * eso es también lo que impide que el trabajo se realimente: pintar provoca
 * una mutación que ya no produce trabajo. Mismo razonamiento que
 * `pintarIconos`.
 */
function pintarTextos(raiz = document) {
  const uno = el => {
    if (el.dataset.i18nPuesto) return;
    let alguno = false;
    for (const [prop, poner] of _DESTINOS) {
      const clave = el.dataset[prop];
      if (clave) { poner(el, t(clave)); alguno = true; }
    }
    if (alguno) el.dataset.i18nPuesto = '1';
  };
  const SEL = '[data-i18n],[data-i18n-html],[data-i18n-ph],[data-i18n-tip],[data-i18n-aria]';
  if (raiz.nodeType === 1 && raiz.dataset) uno(raiz);
  if (raiz.querySelectorAll) {
    raiz.querySelectorAll(SEL).forEach(el => {
      if (!el.dataset.i18nPuesto) uno(el);
    });
  }
}

function _observarTextos() {
  if (typeof MutationObserver !== 'function') return;
  new MutationObserver(muts => {
    for (const m of muts) {
      m.addedNodes.forEach(n => {
        if (n.nodeType === 1) pintarTextos(n);
      });
    }
  }).observe(document.documentElement, { childList: true, subtree: true });
}

/**
 * El catálogo, pedido en cuanto se parsea este script.
 *
 * No en el `DOMContentLoaded` y con `await`, que es lo primero que se intentó:
 * un `await` como primera línea del arranque convierte en asíncrono TODO lo
 * que va detrás —los iconos, los tooltips, los pollers— y el navegador puede
 * pintar antes de que nada de eso haya corrido. Lo cazó
 * `test_ningun_svg_se_lee`: 12 iconos pintados en vez de 60.
 *
 * Así la petición sale antes incluso de que el DOM esté listo, así que cuando
 * el arranque la espera ya está resuelta, y el arranque sigue siendo síncrono.
 */
const catalogoListo = cargarIdioma(idiomaGuardado());

/**
 * Cambia el idioma: lo guarda en el navegador y en el servidor, y recarga.
 *
 * El POST se espera antes de recargar. Si no se esperara, la recarga podría
 * cancelarlo y el servidor se quedaría traduciendo sus mensajes al idioma
 * viejo mientras la interfaz ya está en el nuevo — una divergencia que el
 * usuario vería como «el log sale en otro idioma» y que no sabría explicar.
 */
async function cambiarIdioma(codigo) {
  if (!IDIOMAS.some(i => i.codigo === codigo)) return;
  try { localStorage.setItem(IDIOMA_PREF, codigo); } catch (e) { /* bloqueado */ }
  try {
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ idioma: codigo }),
    });
  } catch (e) {
    console.error('[i18n] el servidor no pudo guardar el idioma', e);
  }
  location.reload();
}

/**
 * Reconcilia con el servidor tras el arranque, sin bloquear el render.
 *
 * Solo hace algo si de verdad divergen, y entonces recarga una vez. El caso
 * que cubre es el `app_settings.json` editado a mano o copiado de otra
 * máquina: sin esto, el servidor hablaría un idioma y la interfaz otro.
 */
async function reconciliarIdioma(idiomaDelServidor) {
  if (!idiomaDelServidor || idiomaDelServidor === _idioma) return;
  if (!IDIOMAS.some(i => i.codigo === idiomaDelServidor)) return;
  try { localStorage.setItem(IDIOMA_PREF, idiomaDelServidor); } catch (e) { /**/ }
  location.reload();
}
