'use strict';
/* ═══════════════════════════════════════════════════════════════════════════
 *  TAB 4 — MULTIPLEXAR MKV
 *
 *  Dos MKV, A y B, y de cada uno lo que se elija —el vídeo, el audio, los
 *  subtítulos, los capítulos y los adjuntos— en un MKV final. El caso que la
 *  motivó es el de siempre: el remux del Blu-ray trae la imagen buena —Dolby
 *  Vision FEL— y un WEB-DL trae el doblaje que el disco no tiene. Juntarlos
 *  «a mano» en MKVToolNix sale desfasado en cuanto los dos van a velocidades
 *  distintas (23,976 y 24 fps): el audio se adelanta un 0,1 % y a la hora y
 *  media son seis segundos.
 *
 *  A y B no tienen papeles (rediseño del 2026-10-27). El proyecto nace SIN
 *  NADA ELEGIDO y se va sección a sección —Vídeo, Audio, Subtítulos,
 *  Capítulos y Otros—, cada una con dos columnas (A | B) y un interruptor por
 *  elemento; lo que se quede vacío se avisa antes de multiplexar, para no
 *  repetir veinte minutos por un descuido. Son decisiones del usuario.
 *
 *  Contrato con el servidor (`routers/mux.py`):
 *
 *   · el proyecto llega ENTERO por `GET /api/mux/{id}`, con `plan`, `relato`,
 *     `patrones` y `mis_idiomas` calculados al servir. La interfaz los LEE: no
 *     re-deriva ni la línea de tiempo, ni qué hace falta medir, ni qué se
 *     puede multiplexar (la regla de `session.plan` en CMv4.0);
 *   · las ediciones se guardan solas (`PUT`, siempre con `esquema`), y la
 *     respuesta trae el plan nuevo: cambiar de dónde sale el vídeo cambia qué
 *     se mueve, qué se estira y el nombre del MKV;
 *   · los patrones («Todo», «Nada», «Mis idiomas»…) los resuelve el servidor
 *     (`POST /patron`) sobre lo guardado, después de esperar a que lo tocado
 *     esté guardado;
 *   · lo que corre (la medida, el multiplexado) no tiene WebSocket: se lee
 *     por REST cada 2 s mientras dura, y la columna de trabajo avisa al acabar.
 *
 *  Tres reglas heredadas de la tanda 3 del frontend:
 *
 *   · cada lectura lleva su NÚMERO al salir, y una respuesta más vieja que la
 *     ya aplicada se descarta — si no, un GET lento pisaría el PUT de después;
 *   · lo que se escribe a mano se ancla (`pintarSiCambia` conserva valores,
 *     foco y desplegables), y un campo se desmarca al guardarse para que el
 *     servidor pueda volver a pintarlo;
 *   · los argumentos de un handler inline van con `jsArg`, nunca entre
 *     comillas a mano: un nombre con apóstrofo mataría el botón.
 * ═══════════════════════════════════════════════════════════════════════════ */

const openMuxProjects = [];
let activeMuxSubTabId = null;
let _muxLista = [];
let _muxListaCargada = false;
let _muxSeleccionado = null;
let _muxSortDir = 'desc';
let _muxFiltro = 'all';

/** Cada cuánto se lee un proyecto mientras algo corre sobre él. */
const _MUX_POLL_MS = 2000;

/** El formato de proyecto que habla ESTA interfaz. Va en cada escritura: un
 *  servidor que espere otro contesta 409 «recarga la página» en vez de
 *  guardar una selección escrita con otras reglas — el JS de antes del
 *  rediseño no mandaba el vídeo en la lista de pistas, y guardar lo que
 *  mandara lo borraría (`models.ESQUEMA_MUX`). */
const _MUX_ESQUEMA = 2;

// Los veredictos de la medida, con los ids de `phases/mux_sync.py`.
const MUX_EN_SINCRONIA = 'en_sincronia';
const MUX_RETRASO = 'retraso';
const MUX_OTRO_RITMO = 'otro_ritmo';
const MUX_DERIVA = 'deriva';
const MUX_MONTAJE_DISTINTO = 'montaje_distinto';
const MUX_NO_CONCLUYENTE = 'no_concluyente';

/** Las cadencias que se escriben con su nombre (y no con 6 decimales). */
const _MUX_CADENCIAS = [[24000, 1001], [24, 1], [25, 1], [30000, 1001], [30, 1],
                        [50, 1], [60000, 1001], [60, 1]];

/** Las tres fases del multiplexado, con los ids de `mux_pipeline.FASES`
 *  (`combinar` es el id de siempre: los historiales ya guardados lo usan). */
const _MUX_FASES = ['comprobar_origenes', 'combinar', 'comprobar_y_guardar'];

/** El orden de los tipos en el MKV final, el de `mux_plan.normalizar_pistas`:
 *  un reproductor que enumera por posición no los mezcla. */
const _MUX_ORDEN_DE_TIPO = { video: 0, audio: 1, subtitles: 2 };

/** Una columna de audio o de subtítulos con más elementos que esto enseña lo
 *  elegido y lo de tus idiomas, y lo demás detrás de «Ver las N restantes»:
 *  un remux trae cuarenta subtítulos y casi nunca se quieren más de dos. */
const _MUX_FILAS_A_LA_VISTA = 10;

// ═══════════════════════════════════════════════════════════════════════════
//  Formatos
// ═══════════════════════════════════════════════════════════════════════════

function _muxNum(v, dec = 0) {
  return Number(v || 0).toLocaleString(localeActual(),
    { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

function _muxGB(bytes) {
  return bytes ? `${_muxNum(bytes / 1e9, 1)} GB` : '—';
}

/** Un tamaño a la escala que le toca: un subtítulo son KB, no «0,0 GB». */
function _muxTamano(bytes) {
  if (!bytes) return '';
  if (bytes >= 1e9) return `${_muxNum(bytes / 1e9, 1)} GB`;
  if (bytes >= 1e6) return `${_muxNum(bytes / 1e6)} MB`;
  return `${_muxNum(Math.max(1, bytes / 1e3))} KB`;
}

/** «1:42:05» o «48:12», como se lee una posición en una película. */
function _muxReloj(segundos) {
  const s = Math.max(0, Math.round(Number(segundos) || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  const dos = (n) => String(n).padStart(2, '0');
  return h ? `${h}:${dos(m)}:${dos(x)}` : `${m}:${dos(x)}`;
}

/** Una cadencia `[num, den]` como la diría alguien: «23,976», «24».
 *  `precisa` enseña los decimales de una cadencia medida, que no es ninguna
 *  de las estándar (24,00013 se leería «24» y diría otra cosa). */
function _muxFps(par, precisa = false) {
  if (!par || !par[0] || !par[1]) return '?';
  const v = par[0] / par[1];
  const estandar = _MUX_CADENCIAS.some(([n, d]) => n * par[1] === d * par[0]);
  const dec = (precisa && !estandar) ? 6 : (v % 1 ? 3 : 0);
  return Number(v.toFixed(dec)).toLocaleString(localeActual(),
    { maximumFractionDigits: dec });
}

/** Un desfase con su signo: «+120 ms», «−5,76 s». */
function _muxDesfase(ms) {
  if (ms === null || ms === undefined || isNaN(ms)) return '—';
  const signo = ms > 0 ? '+' : ms < 0 ? '−' : '';
  return signo + _muxMagnitud(ms);
}

/** Lo mismo sin signo, para una frase que ya dice el sentido («llega 2,35 s
 *  más tarde»): con el signo se leía «+2,35 s más tarde». */
function _muxMagnitud(ms) {
  if (ms === null || ms === undefined || isNaN(ms)) return '—';
  const a = Math.abs(ms);
  return a >= 1000 ? `${_muxNum(a / 1000, 2)} s` : `${_muxNum(a)} ms`;
}

/** Cuánto se separan dos MKV en una hora, a partir de la pendiente en ppm:
 *  «40 ppm» no lo entiende nadie, «144 ms por hora» sí (1 ppm = 3,6 ms/h). */
function _muxMsPorHora(ppm) {
  return _muxNum(Math.abs(Number(ppm) || 0) * 3.6, 1);
}

/** El idioma de una pista en el idioma de la app, con la misma tabla que
 *  Tab 2 (`ISO639_MAP` + `langLiteral`). La etiqueta IETF va detrás solo si
 *  dice algo que el código no: «es-419» es el latino, y sin ella se leería
 *  «Castellano» en una pista que no lo es. */
function _muxIdioma(p) {
  const nombre = langLiteral(ISO639_MAP[p.idioma] || p.idioma || '') || p.idioma || '';
  const ietf = String(p.idioma_ietf || '');
  return ietf.includes('-') ? `${nombre} (${ietf})` : nombre;
}

/** Los canales como se escriben en una pista: 7.1, 5.1, 2.0. */
function _muxCanales(n) {
  const TABLA = { 1: '1.0', 2: '2.0', 3: '2.1', 6: '5.1', 7: '6.1', 8: '7.1' };
  return n ? (TABLA[n] || `${n} ch`) : '';
}

/** El códec con lo que mkvmerge no dice: MediaInfo sabe si un E-AC-3 o un
 *  TrueHD lleva Atmos, o un DTS-HD MA lleva DTS:X. */
function _muxCodec(p) {
  const fc = String(p.formato_comercial || '');
  const extra = /atmos/i.test(fc) && !/atmos/i.test(p.codec) ? ' Atmos'
              : /dts:x/i.test(fc) && !/dts:x/i.test(p.codec) ? ' DTS:X' : '';
  return [`${p.codec || ''}${extra}`, _muxCanales(p.canales)].filter(Boolean).join(' ');
}

/** Lo que identifica una pista en una línea: idioma y códec. */
function _muxDescripcionCorta(p) {
  return [_muxIdioma(p), _muxCodec(p)].filter(Boolean).join(' · ');
}

/** Las marcas de una pista de origen que dicen qué es: forzados,
 *  comentarios, para sordos. */
function _muxMarcasDeOrigen(p) {
  return [p.forzada && p.tipo === 'subtitles' ? tr('tab4.forzados') : '',
          p.comentario ? tr('tab4.comentarios') : '',
          p.sordos ? tr('tab4.para_sordos') : ''].filter(Boolean).join(', ');
}

/** La línea técnica de una pista: idioma · códec · bitrate · tamaño · marcas. */
function _muxLineaDePista(p) {
  // El bitrate de un subtítulo de texto son cientos de bit/s: «0 kbps» no
  // dice nada.
  return [_muxDescripcionCorta(p),
          p.bps >= 1000 ? `${_muxNum(p.bps / 1000)} kbps` : '',
          _muxTamano(p.bytes), _muxMarcasDeOrigen(p)].filter(Boolean).join(' · ');
}

/** El vídeo de una fuente en una línea: códec · resolución · fps · DV/HDR. */
function _muxVideoDe(v) {
  const dv = v.dv_profile
    ? tr('tab4.dv_perfil', { p: `${v.dv_profile}${v.dv_el ? ' ' + v.dv_el : ''}` })
      + (v.dv_cm ? ` · CM ${v.dv_cm}` : '')
    : (v.hdr ? v.hdr : tr('tab4.sin_dolby_vision'));
  return [v.codec, v.resolucion,
          v.cadencia_num ? `${_muxFps([v.cadencia_num, v.cadencia_den])} fps` : '',
          dv].filter(Boolean).join(' · ');
}

function _muxEsc(s) { return escHtml(s == null ? '' : String(s)); }

/** Las cifras de un texto que escribió el servidor («6.2 ms», «≤ 40 ms»),
 *  con el separador decimal del idioma de la interfaz. */
function _muxCifra(texto) {
  return String(texto == null ? '' : texto).replace(/-?\d+(?:\.\d+)?/g, (n) => {
    const dec = (n.split('.')[1] || '').length;
    return _muxNum(Number(n), dec);
  });
}

/** Una lista en prosa, con la conjunción del idioma: «A, B y C». */
function _muxEnumerar(cosas) {
  try {
    return new Intl.ListFormat(localeActual(), { type: 'conjunction' }).format(cosas);
  } catch (_) {
    return cosas.join(', ');
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  La columna izquierda
// ═══════════════════════════════════════════════════════════════════════════

/** Pide la lista. Un fallo NO la vacía: se conserva la última buena y se
 *  reintenta, como en CMv4.0 (un timeout con el NAS cargado no es «no tienes
 *  proyectos»). */
async function refrescarMuxLista() {
  const f = refrescarMuxLista;
  const pedido = f.pedido = (f.pedido || 0) + 1;
  const desde = f.marca = (f.marca || 0) + 1;
  const data = await apiFetch('/api/mux', { silent: true });
  if (pedido !== f.pedido) return;          // ya hay una más nueva en camino
  if (!data) {
    if (_muxListaCargada) renderMuxLista(); else _muxPintarErrorDeLista();
    if (!f.reintento) {
      f.reintento = setTimeout(() => { f.reintento = null; refrescarMuxLista(); }, 4000);
    }
    return;
  }
  if (f.reintento) { clearTimeout(f.reintento); f.reintento = null; }
  _muxLista = _muxConLoTocado(Array.isArray(data.proyectos) ? data.proyectos : [], desde);
  _muxListaCargada = true;
  renderMuxLista();
}

/** Lo que cambió AQUÍ mientras la lista volaba es más nuevo que ella.
 *
 *  Un guardado que vuelve pone el proyecto nuevo en su tarjeta
 *  (`_muxActualizarEnLista`); si la lista se pidió antes, trae el resumen
 *  de antes y lo pisaba —el nombre viejo en la tarjeta con el nuevo en el
 *  panel— hasta el siguiente refresco. Y un proyecto recién borrado
 *  reaparecía (revisión de Codex del 2026-10-25). Las marcas cuelgan de
 *  `refrescarMuxLista` por lo de siempre: los arneses cargan funciones por
 *  nombre. */
function _muxConLoTocado(lista, desde) {
  const f = refrescarMuxLista;
  const tocados = f.tocados || (f.tocados = new Map());
  const local = new Map(_muxLista.map(x => [x.id, x]));
  let out = lista;
  for (const [id, t] of tocados) {
    if (t.marca <= desde) { tocados.delete(id); continue; }   // la lista ya lo trae
    if (t.borrado) { out = out.filter(x => x.id !== id); continue; }
    const mio = local.get(id);
    if (!mio) continue;
    out = out.some(x => x.id === id) ? out.map(x => (x.id === id ? mio : x)) : [mio, ...out];
  }
  return out;
}

function _muxTocarLista(id, borrado = false) {
  const f = refrescarMuxLista;
  f.marca = (f.marca || 0) + 1;
  (f.tocados || (f.tocados = new Map())).set(id, { marca: f.marca, borrado });
}

function _muxPintarErrorDeLista() {
  const list = document.getElementById('mux-sidebar-list');
  if (!list) return;
  const count = document.getElementById('mux-count');
  if (count) count.textContent = '—';
  list.innerHTML = `
    <div class="empty-state" style="padding:24px 12px">
      <div class="empty-state-icon" data-icono="caja"></div>
      <div data-i18n="tab4.no_se_ha_podido_cargar_la_lista"></div>
      <div class="empty-state-desc" style="margin-top:6px" data-i18n="tab4.tus_proyectos_siguen_guardados"></div>
      <button class="btn btn-ghost btn-xs" style="margin-top:10px" onclick="refrescarMuxLista()">
        <span data-icono="refrescar"></span> <span data-i18n="tab3.reintentar_ahora"></span></button>
    </div>`;
}

function _muxNorm(s) {
  return String(s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
}

/** El filtro de los pills, sobre la situación que resolvió el servidor. */
function _muxPasaFiltro(p, filtro) {
  const sit = situacionDe(p);
  if (filtro === 'pending') return sit === 'preparando' || sit === 'cancelado';
  if (filtro === 'decision') return sit === 'esperando_decision';
  if (filtro === 'done') return sit === 'terminado';
  if (filtro === 'error') return sit === 'detenido_por_error';
  return true;
}

/** El chip de la sincronía: lo que separa un proyecto de otro en la lista.
 *  «Vídeo a 24 fps» y «Montaje distinto» son decisiones muy distintas y sin
 *  él había que abrir cada proyecto para saberlo. */
function _muxChipDeSincronia(p) {
  const s = p.sincronia || null;
  if (p.midiendo || (s && s.estado === 'midiendo')) {
    return { txt: tr('tab4.chip_midiendo'), tono: 'azul' };
  }
  if (!s || s.estado !== 'medida') {
    return { txt: tr('tab4.chip_sin_medir'), apagado: true };
  }
  const plan = p.plan || {};
  switch (s.veredicto) {
    case MUX_EN_SINCRONIA:
      return { txt: tr('tab4.chip_en_sincronia'), tono: 'verde' };
    case MUX_RETRASO:
      return { txt: tr('tab4.chip_retraso', { ms: _muxDesfase(s.beta_ms) }), tono: 'azul' };
    case MUX_OTRO_RITMO:
      return { txt: plan.velocidad_video
                 ? tr('tab4.chip_velocidad', { fps: _muxFps(plan.velocidad_video.a) })
                 : tr('tab4.chip_velocidades_distintas'), tono: 'naranja' };
    case MUX_DERIVA:
      return { txt: tr('tab4.chip_se_van_separando'), tono: 'naranja' };
    case MUX_MONTAJE_DISTINTO:
      return { txt: tr('tab4.chip_montaje_distinto'), tono: 'naranja' };
    default:
      return { txt: tr('tab4.chip_medida_dudosa'), apagado: true };
  }
}

function renderMuxLista() {
  const list = document.getElementById('mux-sidebar-list');
  if (!list) return;
  const count = document.getElementById('mux-count');
  const q = _muxNorm(document.getElementById('mux-sidebar-search')?.value || '').trim();
  const sortKey = document.getElementById('mux-sidebar-sort')?.value || 'modified';
  const dir = _muxSortDir === 'asc' ? 1 : -1;
  let items = _muxLista.filter(p => _muxPasaFiltro(p, _muxFiltro));
  if (q) {
    items = items.filter(p => _muxNorm([p.nombre_salida,
      ...(p.fuentes || []).map(f => f.nombre)].join(' ')).includes(q));
  }
  const ORDEN = ['en_marcha', 'esperando_turno', 'esperando_decision',
                 'detenido_por_error', 'cancelado', 'preparando', 'terminado'];
  items.sort((a, b) => {
    let av, bv;
    if (sortKey === 'name') {
      av = (a.nombre_salida || '').toLowerCase(); bv = (b.nombre_salida || '').toLowerCase();
    } else if (sortKey === 'status') {
      av = ORDEN.indexOf(situacionDe(a)); bv = ORDEN.indexOf(situacionDe(b));
    } else {
      av = new Date(a.updated_at || a.created_at || 0).getTime();
      bv = new Date(b.updated_at || b.created_at || 0).getTime();
    }
    return av < bv ? -dir : av > bv ? dir : 0;
  });
  if (count) count.textContent = items.length;
  if (!items.length) {
    const filtrando = q || _muxFiltro !== 'all';
    list.innerHTML = `
      <div class="empty-state" style="padding:24px 12px">
        <div class="empty-state-icon" data-icono="combinar"></div>
        <div>${escHtml(tr(filtrando ? 'tab3.sin_resultados' : 'tab4.crea_un_multiplexado'))}</div>
      </div>`;
    return;
  }
  list.innerHTML = '';
  for (const p of items) list.appendChild(_muxTarjeta(p));
}

function _muxTarjeta(p) {
  const pint = pinturaDeSituacion(p);
  const { titulo, tags } = nombreYTags(p.nombre_salida);
  const chips = [_muxChipDeSincronia(p)];
  for (const t of tags) chips.push({ txt: t, tono: /^CMv4/i.test(t) ? 'morado' : 'teal' });
  const r = p.relato || {};
  const etapa = r.etapa || {};
  const fecha = p.updated_at || p.created_at || '';
  const completa = new Date(fecha).toLocaleString(localeActual(), {
    day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' });
  // Los puntitos solo en lo que está a medias: en un proyecto terminado
  // están todos llenos y no dicen nada (la regla de las otras dos columnas).
  const aMedias = !['terminado', 'detenido_por_error'].includes(pint.situacion);
  const card = document.createElement('div');
  card.className = `session-card${_muxSeleccionado === p.id ? ' selected' : ''}`
                 + (pint.acento ? ' ' + pint.acento : '');
  card.dataset.sid = p.id;
  card.innerHTML = tarjetaDeProyecto({
    titulo,
    tituloTooltip: p.nombre_salida,
    sub: pint.rotulo,
    subTooltip: pint.porque,
    chips,
    estado: pint.chip,
    estadoTooltip: pint.rotulo,
    poster: (p.tmdb_info || {}).poster_url || '',
    icono: iconoDeTrabajo('combinar_mkv', 'mux'),
    meta: fecha ? hace(fecha) : '',
    metaIso: fecha,
    metaTooltip: tr('tab3.modificado') + ' ' + completa,
    pips: aMedias && etapa.total
      ? { hechas: etapa.indice || 0, total: etapa.total,
          tooltip: tr('workbar.n_de_total', { n: etapa.indice || 0, total: etapa.total })
                   + ` · ${etapa.rotulo || ''}` }
      : null,
    insignia: insigniaDeTrabajo(p.id),
    abierto: !!openMuxProjects.find(x => x.id === p.id),
    acciones: `
      <button class="btn btn-primary btn-sm" onclick="event.stopPropagation();abrirMux(${jsArg(p.id)})" data-i18n="tab1.abrir" data-i18n-tip="tab4.abrir_este_proyecto"></button>
      <button class="btn btn-danger btn-sm" onclick="event.stopPropagation();borrarMux(${jsArg(p.id)})" data-i18n="tab1.eliminar" data-i18n-tip="tab4.borrar_el_proyecto_no_el_mkv"></button>`,
  });
  const row = card.querySelector('.session-card-row');
  row.onclick = () => _muxSeleccionarEnLista(p.id);
  row.ondblclick = () => abrirMux(p.id);
  return card;
}

function _muxSeleccionarEnLista(id) {
  _muxSeleccionado = _muxSeleccionado === id ? null : id;
  document.querySelectorAll('#mux-sidebar-list .session-card').forEach(c => {
    c.classList.toggle('selected', c.dataset.sid === _muxSeleccionado);
  });
}

function toggleMuxSortDir() {
  _muxSortDir = _muxSortDir === 'asc' ? 'desc' : 'asc';
  const btn = document.getElementById('mux-sort-dir');
  if (btn) btn.innerHTML = icono(_muxSortDir === 'asc' ? 'flechaArriba' : 'flechaAbajo');
  renderMuxLista();
}

function onMuxFiltro(btn) {
  document.querySelectorAll('#sidebar-tab-4 .sb-filter-pill').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _muxFiltro = btn.dataset.filter || 'all';
  renderMuxLista();
}

/** Mete en la lista lo que acaba de llegar de un proyecto, sin pedirla
 *  entera: es lo que hace que la tarjeta cambie al mismo tiempo que el
 *  panel. */
function _muxActualizarEnLista(datos) {
  if (!datos || !datos.id) return;
  _muxTocarLista(datos.id);
  const i = _muxLista.findIndex(x => x.id === datos.id);
  const antes = i >= 0 ? _muxFirmaDeTarjeta(_muxLista[i]) : '';
  if (i >= 0) _muxLista[i] = datos; else _muxLista.unshift(datos);
  // Un proyecto en marcha se lee cada 2 s, y repintar la lista entera cada
  // vez volvería a decodificar todas las carátulas. Solo si cambia algo de
  // lo que la tarjeta enseña.
  if (_muxFirmaDeTarjeta(datos) !== antes) renderMuxLista();
}

/** Lo que una tarjeta de la lista enseña de un proyecto, en una cadena. */
function _muxFirmaDeTarjeta(p) {
  const s = p.sincronia || {};
  const r = p.relato || {};
  return [p.nombre_salida, p.status, situacionDe(p), (r.etapa || {}).indice, s.estado,
          s.veredicto, s.beta_ms, p.midiendo ? 1 : 0, (p.tmdb_info || {}).poster_url,
          JSON.stringify((p.plan || {}).velocidad_video || null), p.updated_at].join('|');
}

function borrarMux(id) {
  const p = _muxLista.find(x => x.id === id)
         || (openMuxProjects.find(x => x.id === id) || {}).datos;
  if (!p) return;
  showConfirm(
    tr('tab4.borrar_el_proyecto'),
    tr('tab4.se_borrara_el_proyecto', { nombre: p.nombre_salida || id }),
    async () => {
      // Lo que se estaba guardando termina antes: un PUT que llegara detrás
      // del DELETE contestaría 404 con su aviso de error.
      const abierto = _muxProyecto(id);
      if (abierto) await _muxEsperarGuardado(abierto);
      const r = await apiFetch(`/api/mux/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (!r) return;
      const ahora = _muxProyecto(id);
      if (ahora) { ahora._borrado = true; cerrarMux(id); }
      if (_muxSeleccionado === id) _muxSeleccionado = null;
      _muxTocarLista(id, true);
      _muxLista = _muxLista.filter(x => x.id !== id);
      renderMuxLista();
      refrescarMuxLista();
    },
    tr('tab1.eliminar'),
  );
}

// ═══════════════════════════════════════════════════════════════════════════
//  Proyectos abiertos: sub-pestañas, lectura y guardado
// ═══════════════════════════════════════════════════════════════════════════

/** Abre (o trae al frente) un proyecto. */
async function abrirMux(id, datos = null) {
  if (typeof currentTab !== 'undefined' && currentTab !== 4) switchTab(4);
  let project = openMuxProjects.find(p => p.id === id);
  if (!project) {
    project = { id, datos: null, _closed: false };
    openMuxProjects.push(project);
    _muxCrearSubTab(project);
    _muxCrearPanel(project);
    renderMuxLista();
  }
  switchMuxSubTab(id);
  // Cerrada con un guardado a medias, el guardado sigue (`cerrarMux`); leer
  // antes de que termine enseñaría el proyecto de antes de ese cambio.
  const enVuelo = cerrarMux.enVuelo && cerrarMux.enVuelo.get(id);
  if (enVuelo) await enVuelo;
  if (datos) _muxAplicar(project, datos, true);
  else await _muxLeer(project);
  return project;
}

function _muxCrearSubTab(project) {
  const cont = document.getElementById('mux-subtab-projects');
  if (!cont) return;
  const btn = document.createElement('button');
  btn.className = 'subtab-proj';
  btn.id = `mux-stab-${project.id}`;
  btn.dataset.pid = project.id;
  // El trabajo de un multiplexado se encola con el id del proyecto: con eso
  // el icono gira mientras corre (`refrescarIconosDeSubPestana`).
  btn.dataset.sobre = project.id;
  btn.dataset.glifo = 'combinar';
  btn.innerHTML = `
    <span class="subtab-proj-icon"><span data-icono="combinar"></span></span>
    <span class="subtab-proj-name"></span>
    <button class="subtab-proj-close" onclick="cerrarMux(${jsArg(project.id)});event.stopPropagation()" data-i18n-tip="core.cerrar_proyecto">×</button>`;
  btn.onclick = (e) => { if (!e.target.closest('.subtab-proj-close')) switchMuxSubTab(project.id); };
  cont.appendChild(btn);
  _muxNombreDeSubTab(project);
  refrescarIconosDeSubPestana();
  _updateSubtabScrollState();
}

function _muxNombreDeSubTab(project) {
  const el = document.querySelector(`#mux-stab-${CSS.escape(project.id)} .subtab-proj-name`);
  if (!el) return;
  const nombre = (project.datos?.nombre_salida || project.id).replace(/\.mkv$/i, '');
  const corto = nombre.length > 24 ? nombre.slice(0, 24) + '…' : nombre;
  if (el.textContent !== corto) el.textContent = corto;
  el.dataset.tooltip = project.datos?.nombre_salida || project.id;
}

function _muxCrearPanel(project) {
  const cont = document.getElementById('mux-subtab-content');
  if (!cont) return;
  const panel = document.createElement('div');
  panel.className = 'mux-panel subtab-panel';
  panel.id = `mux-panel-${project.id}`;
  panel.style.display = 'none';
  panel.innerHTML = `<div class="project-panel-inner" id="mux-cuerpo-${project.id}">
      <div class="mux-cargando"><span class="cmv40-rec-spinner-inline"></span>
        <span data-i18n="ui.cargando"></span></div>
    </div>`;
  cont.appendChild(panel);
}

function switchMuxSubTab(pid) {
  const cont = document.getElementById('mux-subtab-content');
  guardarScrollDePanel(cont, document.getElementById(`mux-panel-${activeMuxSubTabId}`));
  activeMuxSubTabId = pid;
  document.querySelectorAll('#mux-subtab-content > .mux-panel').forEach(el => {
    el.style.display = 'none';
  });
  const activo = document.getElementById(`mux-panel-${pid}`);
  if (activo) activo.style.display = 'block';
  const vacio = document.getElementById('mux-empty-state');
  if (vacio) vacio.style.display = openMuxProjects.find(p => p.id === pid) ? 'none' : '';
  document.querySelectorAll('#mux-subtab-projects .subtab-proj').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.pid === pid);
  });
  restaurarScrollDePanel(cont, activo);
}

function cerrarMux(pid) {
  const i = openMuxProjects.findIndex(p => p.id === pid);
  if (i === -1) return;
  const project = openMuxProjects[i];
  project._closed = true;
  // Cerrar deja de leer y de pintar, pero NO tira lo que el guardado
  // automático ya aceptó: el que está en vuelo y lo que espera detrás se
  // mandan igual (revisión de Codex del 2026-10-25). Reabrirla espera a que
  // terminen.
  // El patrón cuenta igual que un PUT: reabrir antes de que vuelva leería
  // la selección de antes y la pestaña nueva no sabría que hay otra en
  // camino (revisión de Codex del 2026-10-27).
  const enVuelo = [project._guardado, project._patronEnVuelo].filter(Boolean);
  if (enVuelo.length) {
    const m = cerrarMux.enVuelo || (cerrarMux.enVuelo = new Map());
    const p = Promise.all(enVuelo);
    m.set(pid, p);
    p.then(() => { if (m.get(pid) === p) m.delete(pid); });
  }
  if (project._timer) { clearTimeout(project._timer); project._timer = null; }
  if (project._sortables) project._sortables.forEach(s => { try { s.destroy(); } catch (_) {} });
  document.getElementById(`mux-stab-${pid}`)?.remove();
  document.getElementById(`mux-panel-${pid}`)?.remove();
  openMuxProjects.splice(i, 1);
  _updateSubtabScrollState();
  if (activeMuxSubTabId === pid) {
    if (openMuxProjects.length) switchMuxSubTab(openMuxProjects[openMuxProjects.length - 1].id);
    else {
      activeMuxSubTabId = null;
      const vacio = document.getElementById('mux-empty-state');
      if (vacio) vacio.style.display = '';
    }
  }
  renderMuxLista();
}

function _muxProyecto(pid) {
  return openMuxProjects.find(p => p.id === pid) || null;
}

/** ¿Sigue habiendo algo que leer de este proyecto? */
function _muxHayQueSeguir(d) {
  if (!d) return false;
  return ['queued', 'running'].includes(d.status) || !!d.midiendo
      || (d.sincronia && d.sincronia.estado === 'midiendo');
}

/** Programa la siguiente lectura si algo corre. Encadenada, no `setInterval`:
 *  con el NAS cargado una lectura puede tardar más que el intervalo y se
 *  apilarían (la lección de la columna de trabajo). */
function _muxProgramar(project) {
  if (!project || project._closed) return;
  if (project._timer) { clearTimeout(project._timer); project._timer = null; }
  if (!_muxHayQueSeguir(project.datos)) return;
  const ms = document.hidden ? _MUX_POLL_MS * 3 : _MUX_POLL_MS;
  project._timer = setTimeout(() => { project._timer = null; _muxLeer(project); }, ms);
}

/** Lee el proyecto. La respuesta se aplica solo si es la más nueva. */
async function _muxLeer(project) {
  if (!project || project._closed) return;
  const n = project._pedidos = (project._pedidos || 0) + 1;
  const version = project._version || 0;
  const estado = {};
  const datos = await apiFetch(`/api/mux/${encodeURIComponent(project.id)}`,
                               { silent: true, estado });
  if (project._closed) return;
  if (!datos) {
    if (estado.status === 404) { _muxProyectoBorrado(project); return; }
    // Un timeout no es «se borró»: se vuelve a intentar. Si es la primera
    // lectura, el panel lo dice en vez de quedarse cargando.
    if (!project.datos) {
      const cuerpo = document.getElementById(`mux-cuerpo-${project.id}`);
      if (cuerpo) {
        cuerpo.innerHTML = `<div class="mux-cargando"><span data-icono="aviso"></span>
          <span data-i18n="tab4.no_se_pudo_leer_reintentando"></span></div>`;
      }
    }
    if (project._timer) clearTimeout(project._timer);
    project._timer = setTimeout(() => { project._timer = null; _muxLeer(project); }, _MUX_POLL_MS * 2);
    return;
  }
  // Una lectura vieja no se aplica: ni la que adelantó otra más nueva, ni la
  // que se CRUZÓ con una escritura. Esa salió antes de que el PUT se
  // guardara, o mientras volaba, y puede traer el proyecto de antes: mirar
  // solo si hay un PUT en vuelo al VOLVER la dejaba pasar cuando el PUT ya
  // había terminado, y deshacía en pantalla lo recién guardado (revisión de
  // Codex del 2026-10-25). Cada escritura sube `_version` al salir y al
  // volver, y trae su propio proyecto.
  if (n < (project._aplicado || 0) || project._escribiendo || project._pendiente
      || (project._version || 0) !== version) {
    _muxProgramar(project);
    return;
  }
  project._aplicado = n;
  _muxAplicar(project, datos);
}

function _muxProyectoBorrado(project) {
  showToast(tr('tab4.el_proyecto_ya_no_existe'), 'warning');
  project._borrado = true;
  cerrarMux(project.id);
  _muxTocarLista(project.id, true);
  _muxLista = _muxLista.filter(x => x.id !== project.id);
  renderMuxLista();
}

/** Lo que llega del servidor pasa a ser el proyecto. Solo se acepta un
 *  proyecto: un acuse (`{ok}`, `{started}`) asignado aquí dejaría el panel
 *  sin fuentes (el bug de «Aplicar corrección» en CMv4.0). */
function _muxAplicar(project, datos, forzar = false) {
  if (!project || project._closed) return;
  if (!datos || datos.id !== project.id || !Array.isArray(datos.fuentes)) return;
  // Lo que llega forzado es más nuevo que cualquier lectura en vuelo: subir
  // la versión las deja viejas a todas. Igualar el contador de lecturas no
  // bastaba —una que ya había salido tenía ESE número y pasaba—.
  if (forzar) project._version = (project._version || 0) + 1;
  project.datos = datos;
  _muxPintarPanel(project);
  _muxNombreDeSubTab(project);
  _muxActualizarEnLista(datos);
  _muxProgramar(project);
}

/** Una escritura —un PUT o un patrón—: mientras vuela, y en cuanto vuelve,
 *  las lecturas que se cruzaron con ella son viejas (ver `_muxLeer`). */
async function _muxEscribiendo(project, enviar) {
  project._escribiendo = (project._escribiendo || 0) + 1;
  project._version = (project._version || 0) + 1;
  try {
    return await enviar();
  } finally {
    project._escribiendo -= 1;
    project._version += 1;
  }
}

/** Guarda cambios: uno en vuelo a la vez, y lo que llegue mientras tanto se
 *  junta en el siguiente. Cada cambio manda su campo entero (la lista de
 *  pistas entera, no un parche), así que juntar es quedarse con el último.
 *
 *  Devuelve la promesa del envío en curso, que se resuelve cuando lo
 *  pendiente está en el servidor: `true` si lo aceptó todo, `false` si
 *  rechazó algo. Es lo que esperan «Multiplexar» y los patrones. */
function _muxGuardar(project, cambios) {
  if (!project || project._borrado) return Promise.resolve(false);
  project._pendiente = Object.assign(project._pendiente || {}, cambios);
  // Lo último que se ha PEDIDO de cada campo, mientras no vuelva: comparar
  // con lo recibido se tragaba volver al valor de antes con el PUT en vuelo
  // (`_muxValorPedido`).
  project._pedido = Object.assign(project._pedido || {}, cambios);
  if (!project._guardando) {
    const enviando = _muxEnviarPendiente(project);
    project._guardado = enviando;
    // La promesa vive hasta que el envío TERMINA, relectura tras un rechazo
    // incluida: es lo que esperan «Multiplexar» y los patrones.
    enviando.then(() => { if (project._guardado === enviando) project._guardado = null; });
  }
  return project._guardado;
}

/** El valor de un campo tal y como el usuario lo dejó: lo último pedido si
 *  todavía no ha vuelto, o lo guardado. */
function _muxValorPedido(project, campo) {
  const pedido = project._pedido || {};
  return campo in pedido ? pedido[campo] : (project.datos || {})[campo];
}

async function _muxEnviarPendiente(project) {
  project._guardando = true;
  let rechazado = false;
  try {
    // Sin mirar `_closed`: cerrar la pestaña con un cambio esperando detrás
    // de otro lo tiraba sin mandarlo (revisión de Codex del 2026-10-25).
    while (project._pendiente && !project._borrado) {
      const cuerpo = project._pendiente;
      project._pendiente = null;
      const r = await _muxEscribiendo(project, () => apiFetch(
        `/api/mux/${encodeURIComponent(project.id)}`,
        { method: 'PUT', body: JSON.stringify({ esquema: _MUX_ESQUEMA, ...cuerpo }) }));
      if (!r) { rechazado = true; project._pendiente = null; break; }
      // El último guardado es el estado más nuevo que existe; si hay otro
      // detrás, se espera a su respuesta.
      if (!project._pendiente) _muxAplicar(project, r);
    }
  } finally {
    // Se suelta ANTES de volver a leer: un cambio que llegue durante esa
    // lectura abre su propio envío en vez de esperar a uno que ya terminó.
    project._guardando = false;
    project._pedido = null;
  }
  // El servidor no lo aceptó (apiFetch ya ha dicho por qué): se vuelve a lo
  // que hay de verdad, o la pantalla enseñaría un cambio que no existe.
  if (rechazado) await _muxLeer(project);
  return !rechazado;
}

/** Espera a que lo que se ha tocado esté guardado: los PUT —con la relectura
 *  que deshace uno rechazado— y, con `conPatron`, el patrón en vuelo.
 *  `false` si el servidor rechazó algo: la pantalla ya ha vuelto a lo que
 *  hay de verdad.
 *
 *  Mirar solo `_guardando` dejaba pasar dos ventanas (revisión de Codex del
 *  2026-10-27): la relectura tras un rechazo —con la pantalla enseñando aún
 *  el cambio rechazado— y un patrón cuya respuesta no había vuelto. En las
 *  dos se encolaba una selección que no era la que se veía. */
async function _muxEsperarGuardado(project, conPatron = true) {
  for (;;) {
    const enviando = project._guardado;
    if (enviando) {
      if (!await enviando) return false;
      if (project._guardado === enviando) project._guardado = null;
      continue;
    }
    const patron = conPatron && project._patronEnVuelo;
    if (patron) {
      if (!await patron) return false;
      continue;
    }
    return true;
  }
}

/** Tras el recovery (Mac dormido, pestaña oculta): volver a leer todo. */
function refrescarMuxAbiertos() {
  for (const p of openMuxProjects) if (!p._closed) _muxLeer(p);
  if (typeof currentTab !== 'undefined' && currentTab === 4) refrescarMuxLista();
}

/** El selector de ficha devuelve el proyecto entero ya con la carátula. */
function muxTrasCambiarFicha(id, datos) {
  const project = _muxProyecto(id);
  if (project && datos && datos.id) _muxAplicar(project, datos, true);
  refrescarMuxLista();
}

// ═══════════════════════════════════════════════════════════════════════════
//  El panel
// ═══════════════════════════════════════════════════════════════════════════

function _muxBloqueado(d) {
  return ['queued', 'running'].includes(d.status);
}

/** ¿Se puede tocar la selección ahora? No con el proyecto en marcha, ni con
 *  un patrón en vuelo —un cambio hecho sobre la selección de antes del patrón
 *  pisaría su resultado al guardarse detrás—, ni mientras se encola: lo que se
 *  tocara con `/ejecutar` en vuelo rebotaría con un 409 y el MKV saldría con
 *  la selección de antes (revisión de Codex del 2026-10-27). Vale para TODO
 *  lo que guarda: el nombre, el título, el retraso y las pistas comparadas
 *  también. */
function _muxEditable(project) {
  return !!(project && project.datos && !project._patron && !project._multiplexando
            && !project._borrado && !_muxBloqueado(project.datos));
}

function _muxPintarPanel(project) {
  const cuerpo = document.getElementById(`mux-cuerpo-${project.id}`);
  if (!cuerpo || !project.datos) return;
  // Repintar a mitad de un arrastre deja a Sortable con nodos que ya no
  // existen: el orden se perdería al soltar.
  if (project._arrastrando) { project._repintarAlSoltar = true; return; }
  // Ni a mitad de un clic. Al pulsar un botón con un nombre de pista a
  // medio editar, el `change` del campo salta en el `mousedown` (el foco se
  // va) y el repintado sustituía el botón antes del `mouseup`: el navegador
  // no genera el `click` y se guardaba el nombre pero no la marca (revisión
  // de Codex del 2026-10-25). Es la guarda de la columna de trabajo: el
  // repintado espera al final del gesto.
  const f = _muxPintarPanel;
  if (f.gesto && Date.now() - f.gesto < _MUX_GESTO_MAX_MS) {
    (f.pendientes || (f.pendientes = new Set())).add(project);
    return;
  }
  const html = _muxPanelHTML(project);
  if (pintarSiCambia(cuerpo, html)) _muxInstalarOrden(project);
}

// Un gesto que no termina —el puntero se suelta fuera de la ventana y no
// llega el `pointerup`— no puede dejar el panel sin repintarse para siempre.
const _MUX_GESTO_MAX_MS = 3000;

document.addEventListener('pointerdown', (e) => {
  if (e.target && e.target.closest && e.target.closest('#tab-panel-4')) {
    _muxPintarPanel.gesto = Date.now();
  }
}, true);

function _muxFinDeGesto() {
  const f = _muxPintarPanel;
  if (!f.gesto) return;
  // En la tarea SIGUIENTE: `pointerup` llega antes que `mouseup` y `click`,
  // y repintar aquí sustituiría el botón justo antes de que se generara.
  setTimeout(() => {
    f.gesto = 0;
    const pendientes = f.pendientes;
    f.pendientes = null;
    if (pendientes) pendientes.forEach(p => { if (!p._closed) _muxPintarPanel(p); });
  }, 0);
}
document.addEventListener('pointerup', _muxFinDeGesto, true);
document.addEventListener('pointercancel', _muxFinDeGesto, true);

/** Las secciones de la selección, en su orden. Una FUNCIÓN y no una tabla
 *  del módulo: sus rótulos llevan `tr()`, que en el ámbito del módulo se
 *  evaluaría al cargar y congelaría el idioma. Los ids son los de
 *  `mux_plan.SECCIONES`. */
function _muxSecciones() {
  return [
    { id: 'video', tipo: 'video', glifo: 'claqueta',
      titulo: tr('tab4.seccion_video'), sub: tr('tab4.seccion_video_sub') },
    { id: 'audio', tipo: 'audio', glifo: 'altavoz',
      titulo: tr('tab4.seccion_audio'), sub: tr('tab4.seccion_audio_sub') },
    { id: 'subtitulos', tipo: 'subtitles', glifo: 'subtitulos',
      titulo: tr('tab4.seccion_subtitulos'), sub: tr('tab4.seccion_subtitulos_sub') },
    { id: 'capitulos', glifo: 'libro',
      titulo: tr('tab4.seccion_capitulos'), sub: tr('tab4.seccion_capitulos_sub') },
    { id: 'otros', glifo: 'etiqueta',
      titulo: tr('tab4.seccion_otros'), sub: tr('tab4.seccion_otros_sub') },
  ];
}

function _muxPanelHTML(project) {
  const d = project.datos;
  const ctx = { d, project, pid: d.id, bloq: !_muxEditable(project) };
  return [
    renderTmdbCardHTML(d.tmdb_info, { tipo: 'mux', id: d.id, nombre: d.nombre_salida }),
    _muxBannerHTML(d),
    _muxResumenHTML(ctx),
    _muxSincroniaHTML(d, ctx.bloq),
    ..._muxSecciones().map(s => _muxSeccionHTML(ctx, s)),
    _muxComprobacionHTML(d),
    _muxHistorialHTML(d),
    _muxAccionesHTML(d, 'pie', ctx.bloq),
  ].join('');
}

// ── El banner de arriba: qué está pasando ──────────────────────────────────

function _muxBannerHTML(d) {
  const pint = pinturaDeSituacion(d);
  const pid = jsArg(d.id);
  const t = trabajoSobre(d.id);
  if (pint.situacion === 'en_marcha') {
    const tb = (t && t.trabajo) || {};
    const fase = tb.fase_label || pint.rotulo;
    const pct = tb.pct_medido && tb.pct != null ? ` · ${tr('tab4.pct', { n: _muxNum(tb.pct) })}` : '';
    return `<div class="banner info mux-banner">
      <span class="banner-icon"><span class="spinner-inline"></span></span>
      <div class="exec-result-body">
        <div style="font-weight:600">${escHtml(tr('tab4.multiplexando'))} · ${escHtml(fase)}${escHtml(pct)}</div>
        <div class="exec-result-detail">${escHtml(pint.porque)}</div>
      </div>
      <div class="exec-result-actions">
        <button class="btn btn-ghost btn-xs" onclick="abrirDetalleDeTrabajo(${pid})" data-i18n="tab1.ver_progreso"></button>
        <button class="btn btn-danger btn-xs" onclick="muxPedirCancelar(${pid})" data-i18n="ui.cancelar"></button>
      </div></div>`;
  }
  if (pint.situacion === 'esperando_turno') {
    const pos = d.posicion_en_cola || (t && t.posicion) || null;
    return `<div class="banner info mux-banner">
      <span class="banner-icon"><span data-icono="reloj"></span></span>
      <div class="exec-result-body">
        <div style="font-weight:600">${escHtml(pos ? tr('tab4.en_cola_puesto', { n: pos }) : pint.rotulo)}</div>
        <div class="exec-result-detail" data-i18n="tab4.la_cola_es_una_para_toda_la_app"></div>
      </div>
      <div class="exec-result-actions">
        <button class="btn btn-ghost btn-xs" onclick="cancelarMultiplexadoMux(${pid})" data-i18n="workbar.quitar_de_la_cola"></button>
      </div></div>`;
  }
  if (pint.situacion === 'terminado') {
    const v = d.validacion || {};
    const avisos = (v.checks || []).filter(c => c.aviso).length;
    const ruta = d.output_mkv_path || '';
    return `<div class="banner success mux-banner">
      <span class="banner-icon"><span data-icono="check"></span></span>
      <div class="exec-result-body">
        <div style="font-weight:600">${escHtml(tr('tab4.multiplexado_nombre', { nombre: d.nombre_salida }))}</div>
        <div class="exec-result-detail">${escHtml(ruta)}${ruta ? ' · ' : ''}${escHtml(avisos
          ? tr('tab4.comprobacion_con_avisos', { n: avisos }) : tr('tab4.comprobacion_limpia'))}</div>
      </div>
      <div class="exec-result-actions">
        <button class="btn btn-ghost btn-xs" onclick="muxVerLog(${pid})" data-i18n="tab4.ver_log"></button>
      </div></div>`;
  }
  if (pint.situacion === 'detenido_por_error') {
    return `<div class="banner error mux-banner">
      <span class="banner-icon"><span data-icono="aviso"></span></span>
      <div class="exec-result-body">
        <div style="font-weight:600" data-i18n="tab4.el_multiplexado_fallo"></div>
        <div class="exec-result-detail">${escHtml(d.error_message || pint.porque)}</div>
      </div>
      <div class="exec-result-actions">
        <button class="btn btn-ghost btn-xs" onclick="muxVerLog(${pid})" data-i18n="tab4.ver_log"></button>
      </div></div>`;
  }
  if (pint.situacion === 'cancelado') {
    return `<div class="banner warning mux-banner">
      <span class="banner-icon"><span data-icono="cruz"></span></span>
      <div class="exec-result-body">
        <div style="font-weight:600">${escHtml(pint.rotulo)}</div>
        <div class="exec-result-detail">${escHtml(pint.porque)}</div>
      </div></div>`;
  }
  if (pint.situacion === 'esperando_decision') {
    return `<div class="banner warning mux-banner">
      <span class="banner-icon"><span data-icono="aviso"></span></span>
      <div class="exec-result-body">
        <div style="font-weight:600">${escHtml(pint.rotulo)}</div>
        <div class="exec-result-detail">${escHtml(pint.porque)}</div>
      </div>
      <div class="exec-result-actions">
        <button class="btn btn-ghost btn-xs" onclick="muxIrA(${pid}, 'sincronia')" data-i18n="tab4.ver_la_sincronia"></button>
      </div></div>`;
  }
  return '';
}

/** Lleva la vista a una tarjeta del panel. */
function muxIrA(pid, que) {
  const el = document.getElementById(`mux-${que}-${pid}`);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ── El resumen: las dos fuentes, el plan, la salida y el botón ─────────────

/** La letra de una fuente: A o B. Es como se las nombra en toda la pestaña,
 *  igual que MKVToolNix numera sus ficheros. */
function _muxLetra(i, nombre) {
  const letra = i === 0 ? 'A' : 'B';
  return `<span class="mux-letra mux-letra-${letra.toLowerCase()}"`
       + `${nombre ? ` data-tooltip="${_muxEsc(nombre)}"` : ''}>${letra}</span>`;
}

function _muxOrigen(d, f, id) {
  return ((d.fuentes[f] || {}).pistas || []).find(p => p.id === id) || null;
}

function _muxTipo(d, p) {
  return (_muxOrigen(d, p.fuente, p.id) || {}).tipo;
}

/** Lo que el MKV final saca de una fuente, en piezas: «el vídeo», «audio: 2»… */
function _muxAporta(d, i) {
  const elegidas = (d.pistas || []).filter(p => p.fuente === i);
  const de = (tipo) => elegidas.filter(p => _muxTipo(d, p) === tipo).length;
  const caps = (d.capitulos || []).filter(c => c.fuente === i).length;
  const adj = (d.adjuntos || []).filter(a => a.fuente === i).length;
  return [de('video') ? tr('tab4.aporta_video') : '',
          de('audio') ? tr('tab4.aporta_audio', { n: de('audio') }) : '',
          de('subtitles') ? tr('tab4.aporta_subtitulos', { n: de('subtitles') }) : '',
          caps ? tr('tab4.aporta_capitulos', { n: caps }) : '',
          adj ? tr('tab4.aporta_adjuntos', { n: adj }) : ''].filter(Boolean);
}

function _muxFuenteHTML(d, i) {
  const f = d.fuentes[i] || {};
  const pistas = f.pistas || [];
  const n = (tipo) => pistas.filter(p => p.tipo === tipo).length;
  const datos = [_muxGB(f.tamano), f.duracion_s ? _muxReloj(f.duracion_s) : ''].filter(Boolean);
  const video = n('video') ? _muxVideoDe(f.video || {}) : tr('tab4.sin_video');
  const contenido = [tr('tab4.n_audios', { n: n('audio') }), tr('tab4.n_subtitulos', { n: n('subtitles') }),
                     tr('tab4.n_capitulos', { n: (f.capitulos || []).length }),
                     (f.adjuntos || []).length ? tr('tab4.n_adjuntos', { n: f.adjuntos.length }) : '']
    .filter(Boolean).join(' · ');
  const aporta = _muxAporta(d, i);
  return `
    <div class="mux-fuente${aporta.length ? ' aporta' : ''}">
      <div class="mux-fuente-cab">
        ${_muxLetra(i, f.nombre)}
        <span class="mux-fuente-rol">${escHtml(tr(i === 0 ? 'tab4.mkv_a' : 'tab4.mkv_b'))}</span>
      </div>
      <div class="mux-fuente-nombre" data-tooltip="${_muxEsc(f.ruta)}">${_muxEsc(f.nombre)}</div>
      <div class="mux-fuente-dato">${_muxEsc(datos.join(' · '))}</div>
      <div class="mux-fuente-dato">${_muxEsc(video)}</div>
      <div class="mux-fuente-dato">${_muxEsc(contenido)}</div>
      <div class="mux-aporta">${escHtml(aporta.length
        ? tr('tab4.aporta', { lista: aporta.join(' · ') }) : tr('tab4.no_aporta_nada'))}</div>
    </div>`;
}

/** Lo que se hará al multiplexar, en palabras. Sale del plan del servidor:
 *  aquí solo se cuenta. */
function _muxPlanLineas(d) {
  const plan = d.plan || {};
  if (plan.video_de == null) return [];
  const L = (i) => (i === 0 ? 'A' : 'B');
  const filas = [];
  const vel = plan.velocidad_video;
  if (vel && vel.de && vel.a) {
    const de = vel.de[0] / vel.de[1], a = vel.a[0] / vel.a[1];
    const dif = Math.abs(a / de - 1) * 100;
    filas.push(tr(a > de ? 'tab4.plan_velocidad_rapido' : 'tab4.plan_velocidad_lento', {
      letra: L(plan.video_de), otra: L(1 - plan.video_de),
      de: _muxFps(vel.de), a: _muxFps(vel.a, true), pct: _muxNum(dif, dif < 1 ? 2 : 1) }));
  }
  const movidas = Object.entries(plan.sync || {});
  for (const f of [0, 1]) {
    const deF = movidas.filter(([k]) => k.startsWith(`${f}:`)).map(([, s]) => s);
    const estiradas = deF.filter(s => s.factor);
    const soloMovidas = deF.filter(s => !s.factor);
    if (estiradas.length) filas.push(tr('tab4.plan_estiradas', { letra: L(f), n: estiradas.length }));
    if (soloMovidas.length) {
      filas.push(tr('tab4.plan_movidas', { letra: L(f), n: soloMovidas.length,
                                           ms: _muxDesfase(soloMovidas[0].retraso_ms) }));
    }
  }
  const caps = plan.capitulos || {};
  const lista = caps.lista || [];
  for (const f of [0, 1]) {
    if (!lista.some(x => x.fuente === f)) continue;
    const m = (caps.mapeos || {})[String(f)];
    if (m && m.factor) filas.push(tr('tab4.plan_capitulos_velocidad', { letra: L(f) }));
    else if (m && m.retraso_ms) filas.push(tr('tab4.plan_capitulos_movidos', { letra: L(f), ms: _muxDesfase(m.retraso_ms) }));
  }
  const fuera = lista.filter(x => x.queda === 'fuera').length;
  if (fuera) filas.push(tr('tab4.plan_capitulos_fuera', { n: fuera }));
  const alCero = lista.find(x => x.queda === 'al_cero');
  if (alCero) filas.push(tr('tab4.plan_capitulo_al_principio', { nombre: alCero.nombre || '—' }));
  // Con un montaje distinto, o una medida dudosa sin retraso a mano, lo que
  // viene del otro MKV va sin ajustar: se dice primero, aunque haya otras
  // líneas (unos capítulos que quedan fuera no explican eso).
  const v = plan.necesita_sincronia ? (d.sincronia || {}).veredicto : '';
  if (v === MUX_MONTAJE_DISTINTO) filas.unshift(tr('tab4.plan_sin_ajuste_montaje'));
  else if (v === MUX_NO_CONCLUYENTE && !d.retraso_manual_ms) {
    filas.unshift(tr('tab4.plan_sin_ajuste_medida_dudosa'));
  }
  if (!filas.length) filas.push(tr('tab4.plan_nada_que_ajustar'));
  return filas;
}

function _muxPlanHTML(d) {
  const filas = _muxPlanLineas(d);
  if (!filas.length) return '';
  return `
    <div class="mux-plan">
      <div class="mux-plan-titulo" data-i18n="tab4.al_multiplexar"></div>
      <ul>${filas.map(f => `<li>${escHtml(f)}</li>`).join('')}</ul>
    </div>`;
}

function _muxResumenHTML(ctx) {
  const { d, pid, bloq } = ctx;
  const tam = d.tamano_estimado;
  return `
    <div class="section-card mux-resumen" id="mux-resumen-${_muxEsc(pid)}">
      <div class="section-header">
        <span class="section-icon"><span data-icono="combinar"></span></span>
        <div><div class="section-title" data-i18n="tab4.resumen"></div>
          <div class="section-subtitle" data-i18n="tab4.que_sale_de_cada_mkv"></div></div>
      </div>
      <div class="section-body">
        <div class="mux-fuentes">
          ${_muxFuenteHTML(d, 0)}
          ${_muxFuenteHTML(d, 1)}
        </div>
        ${_muxPlanHTML(d)}
        <div class="mux-salida">
          <label class="mux-campo-rotulo" for="mux-${_muxEsc(pid)}-nombre" data-i18n="tab4.mkv_de_salida"></label>
          <input type="text" class="globals-mkv-input" id="mux-${_muxEsc(pid)}-nombre"
            value="${_muxEsc(d.nombre_salida)}" ${bloq ? 'disabled' : ''}
            oninput="marcarTocado(this)" onchange="muxNombreSalida(${jsArg(pid)}, this)"
            onkeydown="if(event.key==='Enter'){this.blur()}"
            data-i18n-tip="tab4.nombre_del_mkv_en_output">
          <div class="mux-salida-chips">
            ${tam ? `<span class="globals-mkv-chip globals-mkv-chip--size" data-i18n-tip="tab4.la_suma_de_lo_que_se_copia">${escHtml(tr('tab1.ocupara_gb', { texto: _muxNum(tam / 1e9, 1) }))}</span>` : ''}
            ${d.nombre_manual ? `<span class="manual-notice">${icono('lapiz')} <span data-i18n="tab4.nombre_editado_a_mano"></span></span>` : ''}
          </div>
          <label class="mux-campo-rotulo" for="mux-${_muxEsc(pid)}-titulo" data-i18n="tab4.titulo_del_contenedor"></label>
          <input type="text" class="globals-mkv-input mux-titulo" id="mux-${_muxEsc(pid)}-titulo"
            value="${_muxEsc(d.titulo)}" ${bloq ? 'disabled' : ''}
            placeholder="${_muxEsc((d.nombre_salida || '').replace(/\.mkv$/i, ''))}"
            oninput="marcarTocado(this)" onchange="muxTituloContenedor(${jsArg(pid)}, this)"
            onkeydown="if(event.key==='Enter'){this.blur()}"
            data-i18n-tip="tab4.el_que_ensenan_los_reproductores">
        </div>
        ${_muxAccionesHTML(d, 'resumen', bloq)}
      </div>
    </div>`;
}

async function muxNombreSalida(pid, el) {
  const project = _muxProyecto(pid);
  if (!project || !el) return;
  delete el.dataset.tocado;
  const valor = el.value.trim();
  // Vacío no se guarda, y tampoco se deja a la vista: un campo en blanco
  // diría que el MKV no tiene nombre mientras «Multiplexar» usa el de antes.
  if (!valor) { el.value = _muxValorPedido(project, 'nombre_salida') || ''; return; }
  // Contra lo PEDIDO y no contra lo recibido: con un PUT en vuelo, volver al
  // nombre de antes se tomaba por «no ha cambiado» y se quedaba el del PUT
  // (revisión de Codex del 2026-10-27).
  if (valor === _muxValorPedido(project, 'nombre_salida')) return;
  await _muxGuardar(project, { nombre_salida: valor });
}

async function muxTituloContenedor(pid, el) {
  const project = _muxProyecto(pid);
  if (!project || !el) return;
  delete el.dataset.tocado;
  if (el.value.trim() === (_muxValorPedido(project, 'titulo') || '')) return;
  await _muxGuardar(project, { titulo: el.value.trim() });
}

// ── La sincronía ───────────────────────────────────────────────────────────

/** Los motivos del plan que no son «hay algo en marcha». */
function _muxMotivos(d) {
  return ((d.plan || {}).motivos || []).filter(m => m.id !== 'en_marcha');
}

function _muxTextoDeMotivo(m) {
  const p = m.params || {};
  const extra = {};
  // Las cadencias llegan escritas por el servidor con punto («23.976»); aquí
  // se escriben como el resto de números de la interfaz.
  for (const k of ['a', 'b']) {
    if (p[k] != null && !isNaN(Number(p[k]))) extra[k] = _muxNum(Number(p[k]), Number(p[k]) % 1 ? 3 : 0);
  }
  if (p.desde != null) extra.desde = _muxReloj(p.desde);
  if (p.hasta != null) extra.hasta = _muxReloj(p.hasta);
  if (p.ppm != null) extra.ms = _muxMsPorHora(p.ppm);
  return tr(`tab4.motivo_${m.id}`, { ...p, ...extra });
}

function _muxSincroniaHTML(d, bloqSeleccion = _muxBloqueado(d)) {
  const pid = d.id;
  const s = d.sincronia || null;
  const plan = d.plan || {};
  const midiendo = !!d.midiendo || (s && s.estado === 'midiendo');
  const bloq = _muxBloqueado(d);
  let cuerpo;
  if (midiendo) {
    const [hechos, total] = (s && s.progreso) || [0, 7];
    const pct = total ? Math.round((hechos / total) * 100) : 0;
    cuerpo = `
      <div class="mux-midiendo">
        <div class="mux-midiendo-texto"><span class="spinner-inline"></span>
          ${escHtml(tr('tab4.midiendo_punto_n_de', { n: Math.min(hechos + 1, total), total }))}</div>
        <div class="mux-barra"><div class="mux-barra-fill" style="width:${pct}%"></div></div>
        <div class="mux-nota" data-i18n="tab4.la_medida_compara_el_sonido"></div>
        <button class="btn btn-ghost btn-xs" onclick="cancelarMedidaMux(${jsArg(pid)})" data-i18n="tab4.parar_la_medida"></button>
      </div>`;
  } else if (!s || s.estado !== 'medida') {
    const motivo = !s ? tr('tab4.todavia_no_se_ha_medido')
      : s.estado === 'cancelada' ? tr('tab4.la_medida_se_cancelo')
      : tr('tab4.la_medida_fallo', { error: s.error || '—' });
    cuerpo = `
      <div class="mux-veredicto mux-v-neutro">
        <span class="mux-veredicto-icono">${icono('sincronia')}</span>
        <div><div class="mux-veredicto-titulo">${escHtml(motivo)}</div>
          <div class="mux-veredicto-texto" data-i18n="tab4.sin_medida_no_se_sabe"></div></div>
      </div>
      ${_muxRetrasoManualHTML(d, bloqSeleccion)}`;
  } else {
    cuerpo = _muxVeredictoHTML(d) + _muxGraficoHTML(d)
           + _muxRetrasoManualHTML(d, bloqSeleccion) + _muxPuntosHTML(d)
           + _muxParejaHTML(d, bloqSeleccion);
  }
  const vieja = _muxMotivos(d).some(m => m.id === 'medida_vieja');
  // Con el vídeo elegido y todo lo que va en el tiempo del mismo MKV, la
  // medida no se usa: se dice, para que nadie espere a una medida que no
  // decide nada (revisión de Codex del plan: el 409 «esperando la medida»).
  const noHaceFalta = plan.video_de != null && !plan.necesita_sincronia;
  return `
    <div class="section-card" id="mux-sincronia-${_muxEsc(pid)}">
      <div class="section-header">
        <span class="section-icon"><span data-icono="sincronia"></span></span>
        <div><div class="section-title" data-i18n="tab4.sincronia"></div>
          <div class="section-subtitle" data-i18n="tab4.donde_cae_el_mismo_sonido"></div></div>
        <button class="btn btn-ghost btn-xs" onclick="muxMedir(${jsArg(pid)})"
          ${midiendo || bloq ? 'disabled' : ''} data-i18n-tip="tab4.vuelve_a_medir_con_las_pistas">
          <span data-icono="refrescar"></span> <span data-i18n="tab4.volver_a_medir"></span></button>
      </div>
      <div class="section-body">
        ${noHaceFalta ? `<div class="banner info"><span class="banner-icon"><span data-icono="info"></span></span>
          <span data-i18n="tab4.con_lo_elegido_no_hace_falta"></span></div>` : ''}
        ${vieja ? `<div class="banner warning"><span class="banner-icon"><span data-icono="aviso"></span></span>
          <span data-i18n="tab4.los_ficheros_cambiaron_desde_la_medida"></span></div>` : ''}
        ${cuerpo}
      </div>
    </div>`;
}

/** El veredicto, con lo que significa para ESTOS dos ficheros. */
function _muxVeredictoHTML(d) {
  const s = d.sincronia;
  const plan = d.plan || {};
  const cads = plan.cadencias || [];
  let clase = 'mux-v-ok', glifo = 'check', titulo = '', texto = '';
  const disp = s.dispersion_ms != null ? _muxNum(s.dispersion_ms, 1) : '—';
  switch (s.veredicto) {
    case MUX_EN_SINCRONIA:
      titulo = tr('tab4.v_en_sincronia');
      texto = tr('tab4.v_en_sincronia_texto', { disp, n: s.n_validos, total: s.n_puntos });
      break;
    case MUX_RETRASO: {
      const ms = _muxMagnitud(s.beta_ms);
      titulo = tr('tab4.v_retraso', { ms: _muxDesfase(s.beta_ms) });
      texto = tr(s.beta_ms > 0 ? 'tab4.v_retraso_b_tarde' : 'tab4.v_retraso_b_pronto', { ms });
      clase = 'mux-v-info'; glifo = 'reloj';
      break;
    }
    case MUX_OTRO_RITMO: {
      titulo = tr('tab4.v_velocidades_distintas', { a: _muxFps(cads[0]), b: _muxFps(cads[1]) });
      const v = plan.video_de;
      const vel = plan.velocidad_video;
      texto = vel
        ? tr('tab4.v_velocidades_ajuste', { letra: v === 0 ? 'A' : 'B', otra: v === 0 ? 'B' : 'A',
                                            fps: _muxFps(vel.a, true) })
        : (plan.audio_de || []).length === 2 ? tr('tab4.v_velocidades_dos_audios')
        // «Ninguna pista de audio procede del otro»: solo cuando es verdad.
        // Con el audio del otro y un vídeo que no admite el ajuste (cadencia
        // variable) no hay velocidad en el plan, y decirlo sería falso.
        : v != null && plan.necesita_sincronia && !(plan.audio_de || []).includes(1 - v)
          ? tr('tab4.v_velocidades_estira', { otra: v === 0 ? 'B' : 'A' })
        : tr('tab4.v_velocidades_sin_elegir');
      clase = 'mux-v-info'; glifo = 'sincronia';
      break;
    }
    case MUX_DERIVA:
      titulo = tr('tab4.v_se_van_separando', { ms: _muxMsPorHora(s.pendiente_ppm) });
      texto = tr('tab4.v_se_van_separando_texto');
      clase = 'mux-v-aviso'; glifo = 'aviso';
      break;
    case MUX_MONTAJE_DISTINTO: {
      const [a, b] = s.salto_entre || [null, null];
      titulo = tr('tab4.v_montaje_distinto');
      texto = tr('tab4.v_montaje_distinto_texto', { desde: _muxReloj(a), hasta: _muxReloj(b) });
      clase = 'mux-v-fallo'; glifo = 'cruz';
      break;
    }
    default:
      titulo = tr('tab4.v_medida_dudosa', { n: s.n_validos, total: s.n_puntos });
      texto = tr('tab4.v_medida_dudosa_texto');
      clase = 'mux-v-aviso'; glifo = 'aviso';
  }
  return `
    <div class="mux-veredicto ${clase}">
      <span class="mux-veredicto-icono">${icono(glifo)}</span>
      <div><div class="mux-veredicto-titulo">${escHtml(titulo)}</div>
        <div class="mux-veredicto-texto">${escHtml(texto)}</div></div>
    </div>`;
}

/** Los ejes «bonitos»: 1, 2 o 5 por potencia de diez. */
function _muxPaso(rango, n = 4) {
  const bruto = rango / n;
  const pot = Math.pow(10, Math.floor(Math.log10(bruto || 1)));
  for (const k of [1, 2, 2.5, 5, 10]) if (bruto <= k * pot) return k * pot;
  return 10 * pot;
}

/** El gráfico de la medida: dónde cae cada punto y la recta del ajuste.
 *
 *  Es la forma de ENSEÑAR el veredicto: una recta plana es un retraso, una
 *  inclinada son velocidades distintas, y un escalón es un montaje distinto.
 *  Un solo tono para los puntos medidos y gris para la recta y los
 *  descartados (son contexto); la tabla de abajo es su gemela, así que el
 *  tooltip no es la única forma de leer un valor. */
function _muxGraficoHTML(d) {
  const s = d.sincronia;
  const puntos = (s.puntos || []).filter(p => p.t_a != null);
  const conDato = puntos.filter(p => p.desfase_ms != null);
  if (!conDato.length) return '';
  // Con un montaje distinto o una medida dudosa no hay UNA recta que
  // describa la película: dibujarla —plana en cero— diría que cuadra.
  const conRecta = ![MUX_MONTAJE_DISTINTO, MUX_NO_CONCLUYENTE].includes(s.veredicto);
  const W = 680, H = 200, mi = 62, md = 16, ms = 14, mb = 34;
  const anchoP = W - mi - md, altoP = H - ms - mb;
  const durA = (d.fuentes[0] || {}).duracion_s || 0;
  const xMax = Math.max(durA, ...puntos.map(p => p.t_a + 30)) || 1;
  const alfa = (s.alfa_num || 1) / (s.alfa_den || 1);
  const ajuste = (t) => (alfa - 1) * t * 1000 + (s.beta_ms || 0);
  const ys = conDato.map(p => p.desfase_ms).concat(conRecta ? [ajuste(0), ajuste(xMax), 0] : [0]);
  let yMin = Math.min(...ys), yMax = Math.max(...ys);
  if (yMax - yMin < 40) { const c = (yMax + yMin) / 2; yMin = c - 20; yMax = c + 20; }
  const pad = (yMax - yMin) * 0.12;
  yMin -= pad; yMax += pad;
  const enSeg = Math.max(Math.abs(yMin), Math.abs(yMax)) >= 1500;
  const X = (t) => mi + (t / xMax) * anchoP;
  const Y = (v) => ms + (1 - (v - yMin) / (yMax - yMin)) * altoP;
  const paso = _muxPaso(yMax - yMin);
  const marcasY = [];
  for (let v = Math.ceil(yMin / paso) * paso; v <= yMax; v += paso) marcasY.push(v);
  const pasoX = xMax > 5400 ? 1800 : xMax > 1800 ? 900 : 300;
  const marcasX = [];
  for (let t = 0; t <= xMax; t += pasoX) marcasX.push(t);
  const rotY = (v) => enSeg ? `${_muxNum(v / 1000, 1)} s` : `${_muxNum(v)} ms`;
  const rejilla = marcasY.map(v => `
      <line class="mux-graf-rejilla${v === 0 ? ' cero' : ''}" x1="${mi}" x2="${W - md}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}"/>
      <text class="mux-graf-eje" x="${mi - 8}" y="${(Y(v) + 3.5).toFixed(1)}" text-anchor="end">${escHtml(rotY(v))}</text>`).join('');
  const ejeX = marcasX.map(t => `
      <text class="mux-graf-eje" x="${X(t).toFixed(1)}" y="${H - mb + 16}" text-anchor="middle">${escHtml(_muxReloj(t))}</text>`).join('');
  const recta = !conRecta ? '' : `<line class="mux-graf-ajuste" x1="${X(0).toFixed(1)}" y1="${Y(ajuste(0)).toFixed(1)}"
      x2="${X(xMax).toFixed(1)}" y2="${Y(ajuste(xMax)).toFixed(1)}"/>`;
  const marcas = conDato.map((p, i) => {
    const tip = [tr('tab4.punto_n', { n: i + 1 }) + ' · ' + _muxReloj(p.t_a),
                 tr('tab4.desfase_p', { p: _muxDesfase(p.desfase_ms) }),
                 `r ${_muxNum(p.r, 2)} · ${tr('tab4.contraste_p', { p: _muxNum(p.contraste, 1) })}`,
                 p.valido ? tr('tab4.punto_valido') : tr('tab4.punto_descartado')].join('\n');
    const cx = X(p.t_a).toFixed(1), cy = Y(p.desfase_ms).toFixed(1);
    return `<g class="mux-graf-marca">
        <circle class="${p.valido ? 'mux-graf-punto' : 'mux-graf-descartado'}" cx="${cx}" cy="${cy}" r="${p.valido ? 5 : 4.5}"/>
        <circle class="mux-graf-zona" cx="${cx}" cy="${cy}" r="13" data-tooltip="${_muxEsc(tip)}"/>
      </g>`;
  }).join('');
  return `
    <div class="mux-grafico">
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${_muxEsc(tr('tab4.grafico_de_la_medida'))}">
        ${rejilla}${ejeX}${recta}${marcas}
      </svg>
      <div class="mux-graf-leyenda">
        <span><span class="mux-leyenda-punto"></span> <span data-i18n="tab4.leyenda_medido"></span></span>
        ${conDato.some(p => !p.valido) ? `<span><span class="mux-leyenda-descartado"></span> <span data-i18n="tab4.leyenda_descartado"></span></span>` : ''}
        ${conRecta ? `<span><span class="mux-leyenda-recta"></span> <span data-i18n="tab4.leyenda_ajuste"></span></span>` : ''}
        <span class="mux-graf-eje-nota" data-i18n="tab4.eje_desfase_de_b"></span>
      </div>
    </div>`;
}

/** El retraso a mano, para cuando la medida no da un resultado de fiar. Es
 *  una relación entre los dos ficheros —B respecto a A—, no «lo que se
 *  mueve»: con el vídeo de B lo que se mueve es lo de A, al revés. */
function _muxRetrasoManualHTML(d, bloq = _muxBloqueado(d)) {
  const s = d.sincronia;
  const aplica = !s || s.estado !== 'medida' || s.veredicto === MUX_NO_CONCLUYENTE;
  if (!aplica || d.status === 'running') return '';
  const pid = d.id;
  return `
    <div class="mux-retraso">
      <label class="mux-campo-rotulo" for="mux-${_muxEsc(pid)}-retraso" data-i18n="tab4.retraso_de_b_respecto_a_a"></label>
      <div class="mux-retraso-fila">
        <input type="text" inputmode="decimal" class="globals-mkv-input" id="mux-${_muxEsc(pid)}-retraso"
          value="${d.retraso_manual_ms != null ? _muxEsc(d.retraso_manual_ms) : ''}" placeholder="0"
          ${bloq ? 'disabled' : ''}
          oninput="marcarTocado(this)" onchange="muxRetrasoManual(${jsArg(pid)}, this)">
        <span class="mux-unidad">ms</span>
      </div>
      <div class="mux-nota" data-i18n="tab4.positivo_lo_de_b_suena_mas_tarde"></div>
    </div>`;
}

/** La tabla gemela del gráfico. */
function _muxPuntosHTML(d) {
  const s = d.sincronia;
  const puntos = s.puntos || [];
  if (!puntos.length) return '';
  const filas = puntos.map((p, i) => `
      <tr class="${p.valido ? '' : 'mux-punto-no'}">
        <td>${i + 1}</td>
        <td>${escHtml(_muxReloj(p.t_a))}</td>
        <td>${escHtml(_muxDesfase(p.desfase_ms))}</td>
        <td>${p.r != null ? escHtml(_muxNum(p.r, 2)) : '—'}</td>
        <td>${p.contraste != null ? escHtml(_muxNum(p.contraste, 1)) : '—'}</td>
        <td>${escHtml(p.valido ? tr('tab4.punto_valido')
                                : (p.error || tr('tab4.punto_descartado')))}</td>
      </tr>`).join('');
  return `
    <details class="mux-detalles" data-detalle="mux-puntos">
      <summary data-i18n="tab4.ver_los_puntos_medidos"></summary>
      <table class="exec-history-table mux-tabla">
        <thead><tr><th>#</th><th data-i18n="tab4.posicion_en_a"></th><th data-i18n="tab4.desfase"></th>
          <th data-i18n-tip="tab4.correlacion_entre_las_dos_senales">r</th>
          <th data-i18n="tab4.contraste"></th><th data-i18n="ui.estado"></th></tr></thead>
        <tbody>${filas}</tbody>
      </table>
      <div class="mux-nota">${escHtml(tr('tab4.variacion_y_separacion', {
        disp: s.dispersion_ms != null ? _muxNum(s.dispersion_ms, 1) : '—',
        ms: s.pendiente_ppm != null ? _muxMsPorHora(s.pendiente_ppm) : '—' }))}</div>
    </details>`;
}

function _muxOpcionDeAudio(p) {
  return [_muxDescripcionCorta(p), p.nombre].filter(Boolean).join(' · ');
}

/** Con qué dos pistas se mide. La de referencia la elige el servidor (mismo
 *  idioma en los dos lados si lo hay); cambiarla vuelve a medir sola. */
function _muxParejaHTML(d, bloqSeleccion = _muxBloqueado(d)) {
  const pid = d.id;
  const s = d.sincronia || {};
  const audios = (i) => (d.fuentes[i].pistas || []).filter(p => p.tipo === 'audio');
  const a = d.pareja ? d.pareja[0] : s.pista_a;
  const b = d.pareja ? d.pareja[1] : s.pista_b;
  const opciones = (i, sel) => audios(i).map(p => `<option value="${p.id}"${p.id === sel ? ' selected' : ''}>`
    + `${_muxEsc(_muxOpcionDeAudio(p))}</option>`).join('');
  const bloq = bloqSeleccion || !!d.midiendo;
  const actual = (i, id) => audios(i).find(p => p.id === id);
  const pa = actual(0, s.pista_a), pb = actual(1, s.pista_b);
  return `
    <details class="mux-detalles" data-detalle="mux-pareja">
      <summary>${escHtml(pa && pb ? tr('tab4.medido_con', { a: _muxDescripcionCorta(pa), b: _muxDescripcionCorta(pb) })
                                  : tr('tab4.pistas_comparadas'))}</summary>
      <div class="mux-pareja">
        <div class="mux-pareja-fila">${_muxLetra(0)}
          <select id="mux-${_muxEsc(pid)}-pareja-a" class="sidebar-sort-select" ${bloq ? 'disabled' : ''}
            onchange="marcarTocado(this)">${opciones(0, a)}</select></div>
        <div class="mux-pareja-fila">${_muxLetra(1)}
          <select id="mux-${_muxEsc(pid)}-pareja-b" class="sidebar-sort-select" ${bloq ? 'disabled' : ''}
            onchange="marcarTocado(this)">${opciones(1, b)}</select></div>
        <button class="btn btn-ghost btn-xs" ${bloq ? 'disabled' : ''} onclick="muxCambiarPareja(${jsArg(pid)})"
          data-i18n="tab4.medir_con_estas_pistas"></button>
        <div class="mux-nota" data-i18n="tab4.la_medida_es_mas_fiable_mismo_idioma"></div>
      </div>
    </details>`;
}

async function muxMedir(pid) {
  const project = _muxProyecto(pid);
  if (!project) return;
  const r = await apiFetch(`/api/mux/${encodeURIComponent(pid)}/medir`, { method: 'POST' });
  if (!r) return;
  if (!r.started) showToast(tr('tab4.ya_se_esta_midiendo'), 'info');
  await _muxLeer(project);
}

async function cancelarMedidaMux(pid) {
  const r = await apiFetch(`/api/mux/${encodeURIComponent(pid)}/medir/cancelar`, { method: 'POST' });
  // Solo se dice que se paró si el servidor lo aceptó: con `ok: false` no
  // había medida que parar (ya había terminado).
  if (r && r.ok) showToast(tr('tab4.medida_parada'), 'info');
  else if (r) showToast(tr('tab4.no_habia_medida_en_marcha'), 'info');
  const project = _muxProyecto(pid);
  if (project) await _muxLeer(project);
  refrescarWorkbar();
}

async function muxCambiarPareja(pid) {
  const project = _muxProyecto(pid);
  if (!project) return;
  const a = document.getElementById(`mux-${pid}-pareja-a`);
  const b = document.getElementById(`mux-${pid}-pareja-b`);
  if (!a || !b) return;
  delete a.dataset.tocado;
  delete b.dataset.tocado;
  const pareja = [Number(a.value), Number(b.value)];
  // El servidor vuelve a medir solo si la pareja CAMBIA, así que con la que
  // ya estaba guardada el botón no hacía nada (revisión de Codex del
  // 2026-10-25). Ahí se pide la medida directamente.
  const actual = project.datos && project.datos.pareja;
  if (actual && actual[0] === pareja[0] && actual[1] === pareja[1]) {
    await muxMedir(pid);
    return;
  }
  await _muxGuardar(project, { pareja });
}

async function muxRetrasoManual(pid, el) {
  const project = _muxProyecto(pid);
  if (!project || !el) return;
  delete el.dataset.tocado;
  // Texto y no `type="number"`: ahí «+250» —lo natural, la nota dice que
  // el positivo retrasa— no es un número válido para el navegador, el
  // campo valía '' y se guardaba 0 sin decir nada. Se acepta el signo, la
  // coma decimal y un «ms» detrás; lo que no es un número no se guarda y el
  // campo vuelve a lo que había.
  const texto = el.value.trim().replace(/\s*ms$/i, '').replace(',', '.');
  const v = texto === '' ? 0 : Math.round(Number(texto));
  if (!Number.isFinite(v)) {
    const antes = project.datos && project.datos.retraso_manual_ms;
    el.value = antes != null ? String(antes) : '';
    return;
  }
  await _muxGuardar(project, { retraso_manual_ms: v });
}

function muxAceptarRiesgo(pid) {
  const project = _muxProyecto(pid);
  if (!project) return;
  const motivos = _muxMotivos(project.datos).map(_muxTextoDeMotivo);
  showConfirm(
    tr('tab4.multiplexar_igualmente'),
    tr('tab4.multiplexar_igualmente_texto', { motivos: motivos.join(' · ') }),
    () => _muxGuardar(project, { acepta_riesgo: true }),
    tr('tab4.aceptar_y_seguir'),
  );
}

function muxRetirarRiesgo(pid) {
  const project = _muxProyecto(pid);
  if (project) _muxGuardar(project, { acepta_riesgo: false });
}

// ── La selección: las cinco secciones ──────────────────────────────────────

/** Las pistas elegidas, como las manda el PUT. */
function _muxPistasDe(d) {
  return (d.pistas || []).map(p => ({ fuente: p.fuente, id: p.id, nombre: p.nombre || '',
                                      por_defecto: !!p.por_defecto, forzada: !!p.forzada }));
}

/** El vídeo delante, después el audio y después los subtítulos, sin cambiar
 *  el orden dentro de cada tipo. Es lo que llega al `--track-order`: una pista
 *  añadida al final de la lista entera acabaría DETRÁS de los subtítulos (lo
 *  normaliza también el servidor; esto es para que la pantalla no salte). */
function _muxAgrupar(d, pistas) {
  const orden = (p) => {
    const t = _muxTipo(d, p);
    return t in _MUX_ORDEN_DE_TIPO ? _MUX_ORDEN_DE_TIPO[t] : 3;
  };
  return pistas.map((p, i) => [p, i])
    .sort((x, y) => (orden(x[0]) - orden(y[0])) || (x[1] - y[1]))
    .map(([p]) => p);
}

/** La lista de pistas en el orden del MKV final, con el audio y los
 *  subtítulos en el orden que digan sus listas (al soltar un arrastre). */
function _muxComponerPistas(d, ordenAudio, ordenSubs) {
  const actuales = _muxPistasDe(d);
  const por = new Map(actuales.map(p => [`${p.fuente}:${p.id}`, p]));
  const de = (tipo) => actuales.filter(p => _muxTipo(d, p) === tipo).map(p => `${p.fuente}:${p.id}`);
  return [...de('video'), ...(ordenAudio || de('audio')), ...(ordenSubs || de('subtitles'))]
    .map(k => por.get(k)).filter(Boolean);
}

/** Cambia las pistas en local, repinta y guarda. */
function _muxCambiarPistas(project, pistas) {
  pistas = _muxAgrupar(project.datos, pistas);
  project.datos = { ...project.datos, pistas };
  _muxPintarPanel(project);
  _muxGuardar(project, { pistas });
}

/** Un interruptor que no se aplicó (proyecto en marcha, patrón en vuelo)
 *  vuelve a donde estaba: el repintado no lo haría, porque el HTML no
 *  cambia y `pintarSiCambia` no toca nada. */
function _muxDeshacerInterruptor(el) {
  if (el && el.type === 'checkbox') el.checked = !el.checked;
}

/** Elige o quita una pista de audio o de subtítulos. */
function muxAlternar(pid, f, id, el) {
  const project = _muxProyecto(pid);
  if (!_muxEditable(project)) { _muxDeshacerInterruptor(el); return; }
  const d = project.datos;
  const o = _muxOrigen(d, f, id);
  if (!o) return;
  const actuales = _muxPistasDe(d);
  if (actuales.some(p => p.fuente === f && p.id === id)) {
    _muxCambiarPistas(project, actuales.filter(p => !(p.fuente === f && p.id === id)));
    return;
  }
  // Entra con el nombre y las marcas que traía en su MKV. La marca de por
  // defecto no se hereda si ya hay otra de su tipo: dos «por defecto» y el
  // reproductor elige la primera, que no tiene por qué ser la que añades.
  const yaHayDefecto = actuales.some(p => p.por_defecto && _muxTipo(d, p) === o.tipo);
  _muxCambiarPistas(project, [...actuales, {
    fuente: f, id, nombre: o.nombre || '',
    por_defecto: !!o.por_defecto && !yaHayDefecto, forzada: !!o.forzada }]);
}

/** Elige el vídeo de una fuente, o lo quita. Va entero —una BL y su EL en
 *  pistas aparte van juntas— y de un solo MKV: elegir el de B quita el de A. */
function muxAlternarVideo(pid, f, el) {
  const project = _muxProyecto(pid);
  if (!_muxEditable(project)) { _muxDeshacerInterruptor(el); return; }
  const d = project.datos;
  const actuales = _muxPistasDe(d);
  const sinVideo = actuales.filter(p => _muxTipo(d, p) !== 'video');
  const tenia = actuales.some(p => p.fuente === f && _muxTipo(d, p) === 'video');
  const videos = tenia ? [] : ((d.fuentes[f] || {}).pistas || [])
    .filter(o => o.tipo === 'video')
    .map(o => ({ fuente: f, id: o.id, nombre: o.nombre || '',
                 por_defecto: !!o.por_defecto, forzada: !!o.forzada }));
  _muxCambiarPistas(project, [...videos, ...sinVideo]);
}

function muxAlternarCapitulo(pid, f, edicion, indice, el) {
  const project = _muxProyecto(pid);
  if (!_muxEditable(project)) { _muxDeshacerInterruptor(el); return; }
  const d = project.datos;
  const caps = (d.capitulos || []).map(c => ({ fuente: c.fuente, edicion: c.edicion || 0, indice: c.indice }));
  const es = (c) => c.fuente === f && c.edicion === edicion && c.indice === indice;
  const capitulos = caps.some(es) ? caps.filter(c => !es(c)) : [...caps, { fuente: f, edicion, indice }];
  project.datos = { ...d, capitulos };
  _muxPintarPanel(project);
  _muxGuardar(project, { capitulos });
}

function muxAlternarAdjunto(pid, f, id, el) {
  const project = _muxProyecto(pid);
  if (!_muxEditable(project)) { _muxDeshacerInterruptor(el); return; }
  const d = project.datos;
  const actuales = (d.adjuntos || []).map(a => ({ fuente: a.fuente, id: a.id }));
  const es = (a) => a.fuente === f && a.id === id;
  const adjuntos = actuales.some(es) ? actuales.filter(a => !es(a)) : [...actuales, { fuente: f, id }];
  project.datos = { ...d, adjuntos };
  _muxPintarPanel(project);
  _muxGuardar(project, { adjuntos });
}

/** Un patrón de la sección («Todo», «Nada», «Mis idiomas»…). Lo resuelve el
 *  servidor sobre lo GUARDADO, así que primero se espera a que lo tocado lo
 *  esté: un patrón calculado con un cambio aún en vuelo mezclaría los dos
 *  (revisión de Codex del plan, 2026-10-27). */
async function muxPatron(pid, seccion, patron) {
  const project = _muxProyecto(pid);
  if (!_muxEditable(project)) return;
  project._patron = true;
  _muxPintarPanel(project);
  // Lo que «Multiplexar», cerrar y borrar esperan además de los PUT: hasta que
  // vuelva, el servidor ya tiene la selección nueva y la pantalla la de antes.
  let acabar;
  project._patronEnVuelo = new Promise(r => { acabar = r; });
  let ok = false;
  try {
    // Solo los PUT: el patrón esperándose a sí mismo no terminaría.
    if (!await _muxEsperarGuardado(project, false)) return;
    const r = await _muxEscribiendo(project, () => apiFetch(
      `/api/mux/${encodeURIComponent(pid)}/patron`,
      { method: 'POST', body: JSON.stringify({ esquema: _MUX_ESQUEMA, seccion, patron }) }));
    project._patron = false;
    if (r) { ok = true; _muxAplicar(project, r, true); }
    else await _muxLeer(project);
  } finally {
    project._patronEnVuelo = null;
    acabar(ok);
    if (project._patron) {
      project._patron = false;
      _muxPintarPanel(project);
    }
  }
}

/** Despliega (o pliega) una columna larga. Es estado de la pantalla: no se
 *  guarda ni viaja al servidor. */
function muxVerTodas(pid, seccion, f) {
  const project = _muxProyecto(pid);
  if (!project) return;
  const v = project._verTodas || (project._verTodas = {});
  const k = `${seccion}:${f}`;
  v[k] = !v[k];
  _muxPintarPanel(project);
}

/** ¿Es de tus idiomas? Los da el servidor (`mis_idiomas`, los mismos que usa
 *  el patrón): aquí solo se mira si la pista habla uno de ellos, para decidir
 *  qué se enseña con la columna plegada. */
function _muxEsMio(d, o) {
  const mios = new Set((d.mis_idiomas || []).map(x => String(x).toLowerCase()));
  const codigo = String(o.idioma || '').toLowerCase();
  const ietf = String(o.idioma_ietf || '').toLowerCase().split('-')[0];
  return mios.has(codigo) || (!!ietf && mios.has(ietf));
}

/** Los elementos de una columna: `{elegido, titulo, dato, nota, mio, accion}`. */
function _muxElementos(d, s, f) {
  const fuente = d.fuentes[f] || {};
  const pid = jsArg(d.id);
  if (s.tipo) {
    const elegidas = new Set((d.pistas || []).map(p => `${p.fuente}:${p.id}`));
    const origen = (fuente.pistas || []).filter(o => o.tipo === s.tipo);
    if (s.tipo === 'video') {
      return origen.map((o, i) => ({
        elegido: elegidas.has(`${f}:${o.id}`),
        titulo: i === 0 ? _muxVideoDe(fuente.video || {}) : tr('tab4.otra_pista_de_video', { n: i + 1 }),
        dato: [o.nombre, o.resolucion && i ? o.resolucion : '', _muxTamano(o.bytes)].filter(Boolean).join(' · '),
        accion: `muxAlternarVideo(${pid}, ${f}, this)`,
      }));
    }
    const sync = (d.plan || {}).sync || {};
    return origen.map(o => {
      const k = `${f}:${o.id}`;
      const ajuste = elegidas.has(k) ? sync[k] : null;
      return {
        elegido: elegidas.has(k),
        mio: _muxEsMio(d, o),
        titulo: _muxDescripcionCorta(o) || o.codec,
        dato: [o.nombre, o.bps >= 1000 ? `${_muxNum(o.bps / 1000)} kbps` : '', _muxTamano(o.bytes),
               o.por_defecto ? tr('tab4.por_defecto_en_su_mkv') : '', _muxMarcasDeOrigen(o)]
          .filter(Boolean).join(' · '),
        nota: ajuste ? (ajuste.factor ? tr('tab4.chip_velocidad_ajustada') : _muxDesfase(ajuste.retraso_ms)) : '',
        accion: `muxAlternar(${pid}, ${f}, ${o.id}, this)`,
      };
    });
  }
  if (s.id === 'capitulos') {
    const elegidos = new Set((d.capitulos || []).map(c => `${c.fuente}:${c.edicion || 0}:${c.indice}`));
    const finales = new Map((((d.plan || {}).capitulos || {}).lista || [])
      .map(x => [`${x.fuente}:${x.edicion}:${x.indice}`, x]));
    const ediciones = new Set((fuente.capitulos || []).map(c => c.edicion || 0)).size;
    return (fuente.capitulos || []).map(c => {
      const k = `${f}:${c.edicion || 0}:${c.indice}`;
      const fin = elegidos.has(k) ? finales.get(k) : null;
      const movido = fin && fin.queda === 'ok' && Math.abs(fin.inicio_ns - c.inicio_ns) >= 500_000_000;
      return {
        elegido: elegidos.has(k),
        titulo: `${_muxReloj(c.inicio_ns / 1e9)} · ${c.nombre || tr('tab4.sin_nombre')}`,
        dato: [ediciones > 1 ? tr('tab4.edicion_n', { n: (c.edicion || 0) + 1 }) : '',
               c.hijos ? tr('tab4.capitulos_dentro', { n: c.hijos }) : '',
               c.oculto ? tr('tab4.oculto') : ''].filter(Boolean).join(' · '),
        nota: !fin ? '' : fin.queda === 'fuera' ? tr('tab4.queda_fuera')
            : fin.queda === 'al_cero' ? tr('tab4.al_principio')
            : movido ? `→ ${_muxReloj(fin.inicio_ns / 1e9)}` : '',
        fuera: !!(fin && fin.queda === 'fuera'),
        accion: `muxAlternarCapitulo(${pid}, ${f}, ${c.edicion || 0}, ${c.indice}, this)`,
      };
    });
  }
  // Otros: los adjuntos.
  const elegidos = new Set((d.adjuntos || []).map(a => `${a.fuente}:${a.id}`));
  return (fuente.adjuntos || []).map(a => ({
    elegido: elegidos.has(`${f}:${a.id}`),
    titulo: a.nombre || `#${a.id}`,
    dato: [a.mime, _muxTamano(a.tamano)].filter(Boolean).join(' · '),
    accion: `muxAlternarAdjunto(${pid}, ${f}, ${a.id}, this)`,
  }));
}

function _muxColumnaHTML(ctx, s, f) {
  const { d, project, pid, bloq } = ctx;
  const fuente = d.fuentes[f] || {};
  const elementos = _muxElementos(d, s, f);
  const letra = f === 0 ? 'a' : 'b';
  // Plegada solo la de audio y la de subtítulos, que tienen idioma con el
  // que decidir qué enseñar; los capítulos van en una caja con scroll.
  const plegable = s.id === 'audio' || s.id === 'subtitulos';
  const expandida = !!((project._verTodas || {})[`${s.id}:${f}`]);
  let visibles = elementos, ocultos = 0;
  if (plegable && !expandida && elementos.length > _MUX_FILAS_A_LA_VISTA) {
    visibles = elementos.filter(e => e.elegido || e.mio);
    ocultos = elementos.length - visibles.length;
    if (!ocultos) visibles = elementos;
  }
  const filas = visibles.map(e => `
        <li class="mux-fila${e.elegido ? ' elegida' : ''}${e.fuera ? ' fuera' : ''}">
          <input type="checkbox" class="mux-interruptor" ${e.elegido ? 'checked' : ''} ${bloq ? 'disabled' : ''}
            aria-label="${_muxEsc(e.titulo)}" onchange="${e.accion}">
          <div class="mux-fila-texto">
            <div class="mux-fila-titulo">${_muxEsc(e.titulo)}</div>
            ${e.dato ? `<div class="mux-fila-dato">${_muxEsc(e.dato)}</div>` : ''}
          </div>
          ${e.nota ? `<span class="mux-fila-nota">${_muxEsc(e.nota)}</span>` : ''}
        </li>`).join('');
  const verTodas = ocultos
    ? `<button class="mux-ver-todas" onclick="muxVerTodas(${jsArg(pid)}, ${jsArg(s.id)}, ${f})">${escHtml(
        ocultos === 1 ? tr('tab4.ver_la_restante') : tr('tab4.ver_las_n_restantes', { n: ocultos }))}</button>`
    : (plegable && expandida && elementos.length > _MUX_FILAS_A_LA_VISTA
      ? `<button class="mux-ver-todas" onclick="muxVerTodas(${jsArg(pid)}, ${jsArg(s.id)}, ${f})" data-i18n="tab4.ver_menos"></button>` : '');
  return `
      <div class="mux-columna mux-columna-${letra}">
        <div class="mux-columna-cab">
          ${_muxLetra(f, fuente.nombre)}
          <span class="mux-columna-nombre" data-tooltip="${_muxEsc(fuente.nombre)}">${_muxEsc(fuente.nombre)}</span>
          ${elementos.length ? `<span class="mux-columna-acciones">
            <button class="btn btn-ghost btn-xs" ${bloq ? 'disabled' : ''} onclick="muxPatron(${jsArg(pid)}, ${jsArg(s.id)}, 'todo_${letra}')" data-i18n="tab4.todo" data-i18n-tip="tab4.todo_tip"></button>
            <button class="btn btn-ghost btn-xs" ${bloq ? 'disabled' : ''} onclick="muxPatron(${jsArg(pid)}, ${jsArg(s.id)}, 'nada_${letra}')" data-i18n="tab4.nada" data-i18n-tip="tab4.nada_tip"></button>
          </span>` : ''}
        </div>
        ${elementos.length
          ? `<ul class="mux-filas${s.id === 'capitulos' ? ' mux-filas-caja' : ''}">${filas}</ul>${verTodas}`
          : `<div class="mux-columna-vacia">${escHtml(tr(`tab4.vacio_${s.id}`))}</div>`}
      </div>`;
}

/** Cuántos elementos de la sección van en el MKV final. */
function _muxElegidosEn(d, s) {
  if (s.tipo) return (d.pistas || []).filter(p => _muxTipo(d, p) === s.tipo).length;
  if (s.id === 'capitulos') return (d.capitulos || []).length;
  return (d.adjuntos || []).length;
}

function _muxSeccionHTML(ctx, s) {
  const { d, pid, bloq } = ctx;
  // Los de una columna llevan su letra al final y van en su cabecera; aquí,
  // los de la sección entera.
  const patrones = ((d.patrones || {})[s.id] || []).filter(p => !/_[ab]$/.test(p));
  return `
    <div class="section-card mux-seccion" id="mux-sec-${s.id}-${_muxEsc(pid)}">
      <div class="section-header">
        <span class="section-icon"><span data-icono="${s.glifo}"></span></span>
        <div><div class="section-title">${escHtml(s.titulo)}</div>
          <div class="section-subtitle">${escHtml(s.sub)}</div></div>
        ${patrones.length ? `<div class="mux-patrones">${patrones.map(p => `
          <button class="btn btn-ghost btn-xs" ${bloq ? 'disabled' : ''} onclick="muxPatron(${jsArg(pid)}, ${jsArg(s.id)}, ${jsArg(p)})"
            data-i18n-tip="tab4.patron_${p}_tip"><span data-i18n="tab4.patron_${p}"></span></button>`).join('')}</div>` : ''}
        <span class="section-badge">${escHtml(tr('tab4.n_elegidos', { n: _muxElegidosEn(d, s) }))}</span>
      </div>
      <div class="section-body">
        <div class="mux-columnas">
          ${_muxColumnaHTML(ctx, s, 0)}
          ${_muxColumnaHTML(ctx, s, 1)}
        </div>
        ${_muxFinalHTML(ctx, s)}
      </div>
    </div>`;
}

// ── «En el MKV final», bajo cada sección ───────────────────────────────────

function _muxFinalHTML(ctx, s) {
  if (s.tipo) return _muxFinalDePistasHTML(ctx, s);
  if (s.id === 'capitulos') return _muxFinalDeCapitulosHTML(ctx);
  return '';
}

/** Lo elegido de un tipo, en su orden, con su nombre y sus marcas. El audio y
 *  los subtítulos se ordenan arrastrando; el vídeo no (sale entero de un
 *  MKV, en su orden). */
function _muxFinalDePistasHTML(ctx, s) {
  const { d, pid, bloq } = ctx;
  const incluidas = (d.pistas || []).filter(p => _muxTipo(d, p) === s.tipo);
  const esVideo = s.tipo === 'video';
  const glifo = esVideo ? 'claqueta' : s.tipo === 'audio' ? 'altavoz' : 'subtitulos';
  const filas = incluidas.map(p => {
    const o = _muxOrigen(d, p.fuente, p.id) || {};
    const sync = esVideo ? null : ((d.plan || {}).sync || {})[`${p.fuente}:${p.id}`];
    const chipSync = sync
      ? `<span class="mux-chip-sync" data-i18n-tip="tab4.esta_pista_se_ajusta">${escHtml(
          sync.factor ? tr('tab4.chip_velocidad_ajustada') : _muxDesfase(sync.retraso_ms))}</span>` : '';
    const arg = `${jsArg(pid)}, ${p.fuente}, ${p.id}`;
    const linea = esVideo ? _muxVideoDe((d.fuentes[p.fuente] || {}).video || {}) : _muxLineaDePista(o);
    return `
      <li class="track-item mux-pista${esVideo ? ' mux-pista-video' : ''}" data-clave="${p.fuente}:${p.id}">
        <span class="track-drag"${esVideo ? '' : ' data-i18n-tip="tab1.arrastra_para_reordenar"'}>${esVideo ? '' : '⠿'}</span>
        ${_muxLetra(p.fuente, (d.fuentes[p.fuente] || {}).nombre)}
        <span class="track-type-icon" data-tooltip="${_muxEsc(linea)}">${icono(glifo)}</span>
        <div class="track-main">
          <input class="track-label-input" type="text" id="mux-${_muxEsc(pid)}-pista-${p.fuente}-${p.id}"
            value="${_muxEsc(p.nombre)}" ${bloq ? 'disabled' : ''}
            oninput="marcarTocado(this)" onchange="muxNombrePista(${arg}, this)"
            data-i18n-tip="tab2.nombre_de_la_pista_en_el">
          <span class="track-raw">${_muxEsc(linea)}</span>
        </div>
        <div class="track-flags">
          ${chipSync}
          <button class="flag-pill${p.por_defecto ? ' active-default' : ''}" ${bloq ? 'disabled' : ''}
            onclick="muxMarca(${arg}, 'por_defecto')" data-i18n="tab1.def" data-i18n-tip="tab4.marca_por_defecto"></button>
          ${s.tipo === 'subtitles' ? `<button class="flag-pill${p.forzada ? ' active-forced' : ''}" ${bloq ? 'disabled' : ''}
            onclick="muxMarca(${arg}, 'forzada')" data-i18n="tab1.frc" data-i18n-tip="tab4.marca_de_forzado"></button>` : ''}
        </div>
        <div class="track-actions">
          <button class="btn btn-icon" ${bloq ? 'disabled' : ''} onclick="muxQuitarPista(${arg})"
            data-i18n-tip="tab4.quitar_del_mkv_final"><span data-icono="cruz"></span></button>
        </div>
      </li>`;
  }).join('');
  return `
    <div class="mux-final">
      <div class="tracks-group-label">${escHtml(tr(esVideo ? 'tab4.en_el_mkv_final' : 'tab4.en_el_mkv_final_arrastra'))}</div>
      <ul class="track-list${esVideo ? '' : ' mux-orden'}" data-tipo="${s.tipo}">
        ${filas || `<li class="track-empty">${escHtml(tr(`tab4.final_vacio_${s.id}`))}</li>`}
      </ul>
    </div>`;
}

/** Los capítulos del MKV final, en el orden y con los tiempos que tendrán:
 *  los del plan del servidor, con lo que queda fuera y lo que pasa al cero
 *  marcado. No se arrastran: el orden es el de la película. */
function _muxFinalDeCapitulosHTML(ctx) {
  const { d } = ctx;
  const lista = (((d.plan || {}).capitulos || {}).lista) || [];
  const filas = lista.map(x => `
      <li class="mux-cap-final${x.queda === 'fuera' ? ' fuera' : ''}">
        <span class="mux-cap-tiempo">${escHtml(x.queda === 'fuera' ? '—' : _muxReloj(x.inicio_ns / 1e9))}</span>
        ${_muxLetra(x.fuente, (d.fuentes[x.fuente] || {}).nombre)}
        <span class="mux-cap-nombre">${_muxEsc(x.nombre || tr('tab4.sin_nombre'))}</span>
        ${x.queda === 'fuera' ? `<span class="mux-fila-nota">${escHtml(tr('tab4.queda_fuera'))}</span>`
          : x.queda === 'al_cero' ? `<span class="mux-fila-nota">${escHtml(tr('tab4.al_principio'))}</span>` : ''}
      </li>`).join('');
  return `
    <div class="mux-final">
      <div class="tracks-group-label" data-i18n="tab4.en_el_mkv_final_en_orden"></div>
      ${lista.length ? `<ol class="mux-capitulos-final">${filas}</ol>`
        : `<div class="track-empty">${escHtml(tr('tab4.final_vacio_capitulos'))}</div>`}
    </div>`;
}

function muxNombrePista(pid, f, id, el) {
  const project = _muxProyecto(pid);
  if (!project || !el || !project.datos) return;
  delete el.dataset.tocado;
  const pistas = _muxComponerPistas(project.datos).map(p =>
    (p.fuente === f && p.id === id) ? { ...p, nombre: el.value.trim() } : p);
  _muxCambiarPistas(project, pistas);
}

function muxMarca(pid, f, id, campo) {
  const project = _muxProyecto(pid);
  if (!_muxEditable(project)) return;
  const pistas = _muxComponerPistas(project.datos).map(p =>
    (p.fuente === f && p.id === id) ? { ...p, [campo]: !p[campo] } : p);
  _muxCambiarPistas(project, pistas);
}

function muxQuitarPista(pid, f, id) {
  const project = _muxProyecto(pid);
  if (!_muxEditable(project)) return;
  const d = project.datos;
  const o = _muxOrigen(d, f, id);
  // El vídeo sale entero: quitar una de sus pistas lo quita todo.
  if (o && o.tipo === 'video') { muxAlternarVideo(pid, f); return; }
  _muxCambiarPistas(project, _muxComponerPistas(d).filter(p => !(p.fuente === f && p.id === id)));
}

/** Sortable en las dos listas de pistas. Se recrea tras cada repintado: el
 *  HTML nuevo trae listas nuevas. */
function _muxInstalarOrden(project) {
  if (project._sortables) project._sortables.forEach(s => { try { s.destroy(); } catch (_) {} });
  project._sortables = [];
  if (typeof Sortable === 'undefined' || !_muxEditable(project)) return;
  const cuerpo = document.getElementById(`mux-cuerpo-${project.id}`);
  if (!cuerpo) return;
  cuerpo.querySelectorAll('.mux-orden').forEach(lista => {
    project._sortables.push(Sortable.create(lista, {
      handle: '.track-drag', animation: 180,
      ghostClass: 'sortable-ghost', chosenClass: 'sortable-chosen',
      onStart: () => { project._arrastrando = true; },
      onEnd: () => {
        project._arrastrando = false;
        const orden = (tipo) => [...cuerpo.querySelectorAll(`.mux-orden[data-tipo="${tipo}"] [data-clave]`)]
          .map(li => li.dataset.clave);
        const pistas = _muxComponerPistas(project.datos, orden('audio'), orden('subtitles'));
        const antes = (project.datos.pistas || []).map(p => `${p.fuente}:${p.id}`).join(',');
        const ahora = pistas.map(p => `${p.fuente}:${p.id}`).join(',');
        if (antes !== ahora) _muxCambiarPistas(project, pistas);
        else if (project._repintarAlSoltar) _muxPintarPanel(project);
        project._repintarAlSoltar = false;
      },
    }));
  });
}

// ── La comprobación del MKV multiplexado ───────────────────────────────────

/** Las comprobaciones de la Fase C, agrupadas como se leen: las de cada
 *  pista juntas (son decenas y casi siempre cuadran) y las globales una por
 *  línea. Lo que NO cuadra se enseña siempre; lo que cuadra, resumido. */
function _muxComprobacionHTML(d) {
  const v = d.validacion;
  if (!v || !Array.isArray(v.checks) || !v.checks.length) return '';
  const DE_PISTA = new Set(['tipo', 'codec', 'idioma', 'nombre', 'por_defecto', 'forzada',
                            'cadencia', 'frames']);
  const deVideo = new Set(['cadencia', 'frames', 'duracion_del_video']);
  const checks = v.checks;
  const estado = (c) => c.ok === false ? 'fallo' : (c.aviso || c.ok == null) ? 'aviso' : 'ok';
  const filas = [];
  const numero = checks.find(c => c.id === 'numero_de_pistas');
  const pistas = checks.filter(c => DE_PISTA.has(c.id) && !deVideo.has(c.id));
  const malPista = pistas.filter(c => estado(c) !== 'ok');
  if (numero || pistas.length) {
    filas.push({
      estado: (numero && numero.ok === false) || malPista.length ? 'fallo' : 'ok',
      titulo: tr('tab4.check_pistas'),
      texto: numero && numero.ok === false
        ? tr('tab4.check_numero_mal', { esperado: numero.esperado, real: numero.real })
        : malPista.length ? tr('tab4.check_pistas_mal', { n: malPista.length })
        : tr('tab4.check_pistas_bien', { n: numero ? numero.real : '' }),
      detalle: malPista.map(c => tr('tab4.check_discrepancia', {
        pista: c.pista, aspecto: tr(`tab4.aspecto_${c.id}`), esperado: String(c.esperado),
        real: String(c.real) })),
    });
  }
  const video = checks.filter(c => deVideo.has(c.id));
  if (video.length) {
    const mal = video.filter(c => estado(c) !== 'ok');
    const dur = video.find(c => c.id === 'duracion_del_video');
    filas.push({
      estado: mal.length ? 'fallo' : 'ok', titulo: tr('tab4.check_video'),
      texto: mal.length ? tr('tab4.check_video_mal')
           : dur ? tr('tab4.check_video_bien', { s: _muxReloj(dur.real) }) : tr('tab4.check_video_bien_corto'),
      detalle: mal.map(c => tr('tab4.check_discrepancia', {
        pista: c.pista || 1, aspecto: tr(`tab4.aspecto_${c.id}`),
        esperado: String(c.esperado), real: String(c.real) })),
    });
  }
  for (const c of checks) {
    if (c.id === 'capitulos') {
      filas.push({ estado: estado(c), titulo: tr('core.capitulos'),
        texto: c.real == null ? tr('tab4.check_capitulos_sin_leer')
             : c.ok === false && c.real !== c.esperado
             ? tr('tab4.check_numero_mal', { esperado: c.esperado, real: c.real })
             : tr('tab4.check_capitulos_bien', { n: c.real, ms: _muxNum(c.desvio_ms || 0, 1) }) });
    } else if (c.id === 'adjuntos') {
      filas.push({ estado: estado(c), titulo: tr('tab4.adjuntos'),
        texto: tr(c.ok ? 'tab4.check_adjuntos_bien' : 'tab4.check_numero_mal',
                  { n: c.real, esperado: c.esperado, real: c.real }) });
    } else if (c.id === 'dolby_vision') {
      filas.push({ estado: estado(c), titulo: 'Dolby Vision',
        texto: c.ok ? tr('tab4.check_dv_bien', { dv: c.real })
                    : tr('tab4.check_dv_mal', { esperado: c.esperado, real: c.real || '—' }) });
    } else if (c.id === 'sincronia' || c.id === 'sincronia_pista') {
      // Contra qué pista del MKV del vídeo se midió. Sin eso, «5,9 ms» no
      // dice si se comparó el castellano con el castellano o con el inglés.
      // `contra_fuente` dice de cuál de los dos es (la validación de antes del
      // rediseño no la traía, y entonces era siempre A).
      const de = c.contra_fuente != null ? c.contra_fuente : 0;
      const contra = c.contra != null && c.real ? _muxOrigen(d, de, c.contra) : null;
      filas.push({ estado: estado(c),
        titulo: tr(c.id === 'sincronia' ? 'tab4.check_sincronia' : 'tab4.check_sincronia_pista',
                   { n: c.pista }),
        texto: c.real ? tr('tab4.check_sincronia_texto', { real: _muxCifra(c.real), esperado: _muxCifra(c.esperado) })
             : c.motivo === 'sin_mismo_doblaje' ? tr('tab4.check_sincronia_sin_doblaje', { letra: de === 0 ? 'A' : 'B' })
             : tr('tab4.check_sincronia_sin_medir'),
        detalle: contra ? [tr('tab4.check_sincronia_contra', {
          pista: _muxDescripcionCorta(contra), letra: de === 0 ? 'A' : 'B' })] : [] });
    }
  }
  const ICONO = { ok: 'check', aviso: 'aviso', fallo: 'cruz' };
  return `
    <div class="section-card">
      <div class="section-header">
        <span class="section-icon"><span data-icono="escudo"></span></span>
        <div><div class="section-title" data-i18n="tab4.comprobacion_del_mkv_multiplexado"></div>
          <div class="section-subtitle" data-i18n="tab4.lo_que_se_leyo_del_fichero"></div></div>
      </div>
      <div class="section-body">
        <ul class="mux-checks">${filas.map(f => `
          <li class="mux-check mux-check-${f.estado}">
            <span class="mux-check-icono">${icono(ICONO[f.estado])}</span>
            <div><div class="mux-check-titulo">${escHtml(f.titulo)}</div>
              <div class="mux-check-texto">${escHtml(f.texto)}</div>
              ${(f.detalle || []).length ? `<ul class="mux-check-detalle">${f.detalle.map(x =>
                `<li>${escHtml(x)}</li>`).join('')}</ul>` : ''}</div>
          </li>`).join('')}</ul>
      </div>
    </div>`;
}

// ── El historial ───────────────────────────────────────────────────────────

function _muxHistorialHTML(d) {
  const h = d.historial || [];
  if (!h.length) return '';
  const pid = d.id;
  const ESTADO = { done: 'hecho', error: 'error', cancelled: 'cancelado' };
  const filas = h.slice().reverse().map(e => {
    const fecha = new Date(e.started_at).toLocaleString(localeActual(), {
      day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' });
    const seg = (k) => (e.fases || {})[k];
    const total = e.finished_at ? (new Date(e.finished_at) - new Date(e.started_at)) / 1000 : null;
    return `<tr class="${e.status === 'error' ? 'exec-row-error' : ''}">
      <td class="exec-h-num">${e.run_number}</td>
      <td class="exec-h-date">${escHtml(fecha)}</td>
      <td class="exec-h-status">${iconoDeEstado(ESTADO[e.status] || 'error', 'icono-chip-sm')}</td>
      ${_MUX_FASES.map(k => `<td>${seg(k) != null ? `<span class="exec-ph-val">${escHtml(_workbarTiempo(seg(k)))}</span>`
                                               : '<span class="exec-ph-na">—</span>'}</td>`).join('')}
      <td class="exec-h-total">${total != null ? escHtml(_workbarTiempo(total)) : '—'}</td>
      <td class="exec-h-actions"><button class="btn btn-ghost btn-xs" onclick="muxVerLog(${jsArg(pid)}, ${e.run_number})"
        data-i18n="tab4.ver_log"></button></td>
    </tr>`;
  }).join('');
  return `
    <div class="section-card">
      <div class="section-header">
        <span class="section-icon"><span data-icono="grafico"></span></span>
        <div><div class="section-title" data-i18n="core.historial_de_ejecuciones"></div>
          <div class="section-subtitle" data-i18n="tab4.cada_multiplexado_con_sus_tiempos"></div></div>
        <span class="section-badge">${h.length}</span>
      </div>
      <div class="section-body">
        <table class="exec-history-table">
          <thead><tr><th>#</th><th data-i18n="core.fecha"></th><th data-i18n="ui.estado"></th>
            ${_MUX_FASES.map(k => `<th data-i18n="tab4.fase_corta_${k}"></th>`).join('')}
            <th data-i18n="core.total"></th><th data-i18n="core.acciones"></th></tr></thead>
          <tbody>${filas}</tbody>
        </table>
      </div>
    </div>`;
}

/** El log de una ejecución, en el modal común de trabajos. */
function muxVerLog(pid, run = null) {
  const project = _muxProyecto(pid);
  const d = project && project.datos;
  if (!d) return;
  const e = run != null ? (d.historial || []).find(x => x.run_number === run)
                        : (d.historial || []).slice(-1)[0];
  if (!e) { showToast(tr('tab4.este_proyecto_no_tiene_log'), 'info'); return; }
  _trabajoModalAbrir({
    id: pid, sobre: pid, tab: 'mux', tipo: 'combinar_mkv', detalle: 'combinar',
    que: tr('tab4.multiplexado_de', { nombre: d.nombre_salida }),
    fase: '', fase_label: '', fase_n: 0, fases_total: _MUX_FASES.length,
    pct: null, pct_medido: false, eta_s: null, cancelable: false, terminal: true,
    segundos: e.finished_at ? Math.round((new Date(e.finished_at) - new Date(e.started_at)) / 1000) : 0,
    historial: { ref_log: `mux:${pid}#${e.run_number}`, estado: e.status,
                 inicio: e.started_at, fin: e.finished_at, error: e.error_message },
    // El desenlace, como lo escribe la columna para una línea del historial.
    paso: _CMV40_FIN[e.status] || tr('workbar.terminado'),
  });
}

// ── Las acciones ───────────────────────────────────────────────────────────

/** El botón «Multiplexar», con lo que lo impide. Va dos veces: en el resumen
 *  —con los motivos enteros y lo que se ha quedado vacío— y al pie, donde se
 *  acaba de recorrer la última sección, con el motivo en una línea. */
function _muxAccionesHTML(d, donde, bloq = _muxBloqueado(d)) {
  const pid = jsArg(d.id);
  const plan = d.plan || {};
  const motivos = _muxMotivos(d);
  const saltables = motivos.length && motivos.every(m => m.saltable);
  const enMarcha = ['queued', 'running'].includes(d.status);
  let aviso = '';
  if (donde === 'resumen' && motivos.length && !enMarcha) {
    const lista = motivos.map(m => `<li>${escHtml(_muxTextoDeMotivo(m))}</li>`).join('');
    const salida = d.acepta_riesgo && saltables
      ? `<div class="mux-motivos-pie"><span data-i18n="tab4.riesgo_aceptado"></span>
           <button class="btn btn-ghost btn-xs" ${bloq ? 'disabled' : ''} onclick="muxRetirarRiesgo(${pid})" data-i18n="tab4.retirar"></button></div>`
      : saltables
      ? `<div class="mux-motivos-pie"><span data-i18n="tab4.se_puede_multiplexar_igualmente"></span>
           <button class="btn btn-ghost btn-xs" ${bloq ? 'disabled' : ''} onclick="muxAceptarRiesgo(${pid})" data-i18n="tab4.multiplexar_igualmente"></button></div>`
      : (motivos.some(m => ['midiendo', 'sin_video', 'sin_audio', 'video_de_las_dos'].includes(m.id)) ? ''
         : `<div class="mux-motivos-pie"><span data-i18n="tab4.esto_no_se_arregla_aceptando"></span></div>`);
    aviso = `
      <div class="mux-motivos${d.acepta_riesgo && saltables ? ' aceptado' : ''}">
        <div class="mux-motivos-titulo">${escHtml(tr(d.acepta_riesgo && saltables
          ? 'tab4.se_multiplexara_aunque' : 'tab4.todavia_no_se_puede_multiplexar'))}</div>
        <ul>${lista}</ul>${salida}
      </div>`;
  }
  // Lo que se ha quedado sin nada elegido: no impide multiplexar, pero se ve
  // antes de pulsar (y el botón lo vuelve a preguntar).
  const vacias = _muxSeccionesVacias(d);
  const nota = donde === 'resumen' && vacias.length && !enMarcha
    ? `<div class="mux-vacias">${icono('info')} <span>${escHtml(tr('tab4.sin_nada_elegido_en', { secciones: _muxEnumerar(vacias) }))}</span></div>`
    : '';
  let boton;
  if (d.status === 'running') {
    boton = `<button class="btn btn-success btn-lg" disabled><span class="spinner-inline"></span> <span data-i18n="tab4.multiplexando"></span></button>`;
  } else if (d.status === 'queued') {
    boton = `<button class="btn btn-success btn-lg" disabled><span data-icono="reloj"></span> <span data-i18n="tab1.en_cola"></span></button>`;
  } else {
    const rotulo = tr(d.status === 'done' ? 'tab4.volver_a_multiplexar'
                    : d.status === 'error' ? 'tab2.reintentar' : 'tab4.multiplexar');
    boton = `<button class="btn btn-success btn-lg" id="mux-multiplexar-${donde}-${_muxEsc(d.id)}" ${plan.puede_multiplexar && !bloq ? '' : 'disabled'}
      onclick="muxMultiplexar(${pid})" data-i18n-tip="tab4.anade_el_multiplexado_a_la_cola">
      <span data-icono="play"></span> <span>${escHtml(rotulo)}</span></button>`;
  }
  const porque = donde === 'pie' && !enMarcha && !plan.puede_multiplexar && motivos.length
    ? `<span class="mux-pie-nota">${escHtml(_muxTextoDeMotivo(motivos[0]))}</span>` : '';
  return `${aviso}${nota}<div class="project-action-bar">${porque}${boton}</div>`;
}

/** Los nombres de las secciones con algo que elegir y nada elegido, del plan
 *  del servidor (`plan.avisos`). */
function _muxSeccionesVacias(d) {
  return (((d.plan || {}).avisos) || [])
    .filter(a => a.id === 'seccion_vacia' && a.params && a.params.seccion)
    .map(a => tr(`tab4.seccion_${a.params.seccion}`));
}

/** «Multiplexar». Antes, lo tocado tiene que estar guardado y, si hay
 *  secciones sin nada elegido, se pregunta: el proyecto nace vacío y se va
 *  sección a sección, y repetir un multiplexado de veinte minutos por un
 *  descuido es justo lo que el usuario pidió evitar (2026-10-27). */
async function muxMultiplexar(pid) {
  const project = _muxProyecto(pid);
  if (!project || project._multiplexando) return;
  if (!await _muxEsperarGuardado(project)) return;
  const d = project.datos || {};
  if (!(d.plan || {}).puede_multiplexar) return;
  const vacias = _muxSeccionesVacias(d);
  if (vacias.length) {
    showConfirm(
      tr('tab4.hay_secciones_sin_nada_elegido'),
      tr('tab4.hay_secciones_sin_nada_elegido_texto', { secciones: _muxEnumerar(vacias) }),
      () => _muxEncolar(project, vacias),
      tr('tab4.multiplexar_sin_ellas'),
    );
    return;
  }
  await _muxEncolar(project, []);
}

/** Encola. `avisadas` son las secciones vacías por las que ya se preguntó: si
 *  lo guardado entretanto deja vacía otra, el «sí» era a otra lista y se
 *  vuelve a preguntar (revisión de Codex del 2026-10-27). Mientras dura, la
 *  selección no se toca (`_muxEditable`). */
async function _muxEncolar(project, avisadas = []) {
  if (!project || project._multiplexando) return;
  project._multiplexando = true;
  _muxPintarPanel(project);
  let preguntarOtraVez = false;
  try {
    // Lo último que se tocó tiene que estar en el servidor ANTES de encolar.
    // Quitar una pista y pulsar mandaba el POST con el PUT aún en vuelo —o
    // sin salir, esperando detrás de otro—: el servidor encolaba la
    // selección de antes y el PUT de después rebotaba con un 409, así que el
    // MKV salía con la pista que se acababa de quitar (revisión de Codex del
    // 2026-10-25).
    if (!await _muxEsperarGuardado(project)) return;
    // Y lo guardado puede haber cambiado el plan (un audio de cada MKV con
    // las velocidades distintas): el botón ya lo dice repintado, y no se
    // pide algo que el servidor va a rechazar.
    if (!((project.datos || {}).plan || {}).puede_multiplexar) return;
    if (_muxSeccionesVacias(project.datos).some(s => !avisadas.includes(s))) {
      preguntarOtraVez = true;
      return;
    }
    const r = await apiFetch(`/api/mux/${encodeURIComponent(project.id)}/ejecutar`, { method: 'POST' });
    if (!r) return;
    // Forzado: un GET que ya volaba traería el proyecto de antes de encolar
    // —editable, con el botón encendido— y pararía el poll.
    _muxAplicar(project, r, true);
    showToast(tr('tab4.multiplexado_en_cola'), 'success');
    refrescarWorkbar();
  } finally {
    project._multiplexando = false;
    _muxPintarPanel(project);
    if (preguntarOtraVez) muxMultiplexar(project.id);
  }
}

/** Cancelar a mitad cuesta lo ya hecho —un mux de diez minutos—, así que se
 *  pregunta antes, como en la columna de trabajo. */
function muxPedirCancelar(pid) {
  const d = (_muxProyecto(pid) || {}).datos || {};
  showConfirm(
    tr('workbar.detener_el_trabajo'),
    tr('workbar.se_detendra_lo_que_ya_este', { que: tr('tab4.multiplexado_de', { nombre: d.nombre_salida || pid }) }),
    () => cancelarMultiplexadoMux(pid),
    tr('workbar.si_detenerlo'),
  );
}

/** Cancela el multiplexado, o lo saca de la cola si aún espera turno. */
async function cancelarMultiplexadoMux(pid) {
  const r = await apiFetch(`/api/mux/${encodeURIComponent(pid)}/cancelar`, { method: 'POST' });
  // Lo que se dice sale de la respuesta, no de la intención: con `ok: false`
  // no había nada que cancelar (ya había terminado).
  if (r && r.descartada) showToast(tr('tab4.quitado_de_la_cola'), 'info');
  else if (r && r.ok) showToast(tr('tab4.multiplexado_cancelado'), 'info');
  else if (r) showToast(tr('tab4.no_habia_nada_que_cancelar'), 'info');
  const project = _muxProyecto(pid);
  if (project) await _muxLeer(project);
  refrescarWorkbar();
}

// ═══════════════════════════════════════════════════════════════════════════
//  El modal «Nuevo multiplexado»
// ═══════════════════════════════════════════════════════════════════════════

const _muxNueva = { fuentes: [null, null], leyendo: false, job: '', timer: null, error: '' };

function abrirNuevoMultiplexado() {
  if (_muxNueva.leyendo) { openModal('mux-nueva-modal'); return; }
  _muxNueva.fuentes = [null, null];
  _muxNueva.error = '';
  _muxNuevaVista('elegir');
  _muxPintarHuecos();
  openModal('mux-nueva-modal');
}

function cerrarNuevoMultiplexado() {
  // Con la lectura en marcha no se cierra: la petición sigue y el proyecto
  // se abriría solo sobre lo que el usuario estuviera haciendo.
  if (_muxNueva.leyendo) return;
  closeModal('mux-nueva-modal');
}

function _muxNuevaVista(vista, error = '') {
  const huecos = document.getElementById('mux-nueva-huecos');
  const pasos = document.getElementById('mux-nueva-pasos');
  const err = document.getElementById('mux-nueva-error');
  const cancelar = document.getElementById('mux-nueva-cancelar');
  const crear = document.getElementById('mux-nueva-crear');
  if (huecos) huecos.style.display = vista === 'elegir' ? '' : 'none';
  if (pasos) pasos.style.display = vista === 'leyendo' ? '' : 'none';
  if (err) {
    err.style.display = error ? '' : 'none';
    const t = document.getElementById('mux-nueva-error-texto');
    if (t) t.textContent = error;
  }
  if (cancelar) cancelar.disabled = vista === 'leyendo';
  if (crear) crear.style.display = vista === 'leyendo' ? 'none' : '';
}

function _muxPintarHuecos() {
  const cont = document.getElementById('mux-nueva-huecos');
  if (!cont) return;
  const hueco = (i) => {
    const f = _muxNueva.fuentes[i];
    const carpeta = f ? f.ruta.replace(/\/[^/]+$/, '') : '';
    return `
      <div class="mux-hueco${f ? ' lleno' : ''}">
        <div class="mux-hueco-cab">${_muxLetra(i)} <span class="mux-fuente-rol">${escHtml(tr(i === 0 ? 'tab4.mkv_a' : 'tab4.mkv_b'))}</span></div>
        ${f ? `<div class="mux-hueco-nombre" data-tooltip="${_muxEsc(f.ruta)}">${_muxEsc(f.nombre)}</div>
               <div class="mux-hueco-ruta">${_muxEsc(carpeta)}</div>`
            : `<div class="mux-hueco-ayuda">${escHtml(tr('tab4.hueco_ayuda'))}</div>`}
        <div class="mux-hueco-acciones">
          <button class="btn btn-${f ? 'ghost' : 'primary'} btn-xs" onclick="muxElegirFuente(${i})">
            <span data-icono="abrir"></span> <span>${escHtml(tr(f ? 'tab4.cambiar' : 'tab4.elegir_mkv'))}</span></button>
          ${f ? `<button class="btn btn-ghost btn-xs" onclick="muxQuitarFuente(${i})" data-i18n="workbar.quitar"></button>` : ''}
        </div>
      </div>`;
  };
  cont.innerHTML = `${hueco(0)}
    <button class="mux-intercambiar" onclick="intercambiarFuentesMux()"
      ${_muxNueva.fuentes[0] || _muxNueva.fuentes[1] ? '' : 'disabled'}
      data-i18n-tip="tab4.intercambiar_los_dos_mkv"><span data-icono="intercambiar"></span></button>
    ${hueco(1)}`;
  const crear = document.getElementById('mux-nueva-crear');
  const [a, b] = _muxNueva.fuentes;
  const mismo = !!(a && b && a.ruta === b.ruta);
  if (crear) crear.disabled = !(a && b) || mismo;
  _muxNuevaVista('elegir', mismo ? tr('tab4.los_dos_son_el_mismo_fichero') : _muxNueva.error);
}

function muxElegirFuente(i) {
  openFileBrowser({
    title: tr(i === 0 ? 'tab4.elige_el_mkv_a' : 'tab4.elige_el_mkv_b'),
    subtitle: tr('tab4.hueco_ayuda'),
    roots: ROOTS_MKV,
    onSelect: (ruta, nombre) => {
      _muxNueva.fuentes[i] = { ruta, nombre: nombre || ruta.split('/').pop() };
      _muxNueva.error = '';
      _muxPintarHuecos();
    },
  });
}

function muxQuitarFuente(i) {
  _muxNueva.fuentes[i] = null;
  _muxNueva.error = '';
  _muxPintarHuecos();
}

function intercambiarFuentesMux() {
  _muxNueva.fuentes = [_muxNueva.fuentes[1], _muxNueva.fuentes[0]];
  _muxNueva.error = '';
  _muxPintarHuecos();
}

/** Los pasos de la lectura, los mismos en los dos: de cualquiera puede salir
 *  el vídeo, y por eso a los dos se les saca la muestra de Dolby Vision
 *  (el servidor se la salta en un MKV sin HEVC). */
const _MUX_PASOS_DE_LECTURA = [[0, 'pistas'], [0, 'capitulos'], [0, 'mediainfo'], [0, 'dolby_vision'],
                               [1, 'pistas'], [1, 'capitulos'], [1, 'mediainfo'], [1, 'dolby_vision']];

function _muxPintarPasos(prog) {
  const cont = document.getElementById('mux-nueva-pasos');
  if (!cont) return;
  const actual = prog && prog.paso
    ? _MUX_PASOS_DE_LECTURA.findIndex(([f, p]) => f === prog.fuente && p === prog.paso) : 0;
  cont.innerHTML = _MUX_PASOS_DE_LECTURA.map(([f, paso], i) => {
    const estado = i < actual ? 'hecho' : i === actual ? 'curso' : 'pendiente';
    const glifo = { hecho: 'check', curso: 'reloj', pendiente: 'pendiente' }[estado];
    return `<div class="mux-paso mux-paso-${estado}">
        <span class="paso-ico paso-${estado}">${icono(glifo)}</span>
        ${_muxLetra(f)} <span data-i18n="tab4.paso_lectura_${paso}"></span>
      </div>`;
  }).join('');
}

async function crearMultiplexado() {
  const [a, b] = _muxNueva.fuentes;
  if (!a || !b || a.ruta === b.ruta || _muxNueva.leyendo) return;
  _muxNueva.leyendo = true;
  const job = _muxNueva.job = Math.random().toString(36).slice(2, 14);
  _muxNuevaVista('leyendo');
  _muxPintarPasos(null);
  const sub = document.getElementById('mux-nueva-sub');
  if (sub) sub.textContent = tr('tab4.leyendo_los_dos_mkv');
  // El paso real, del servidor: el sello `job` evita leer el de otra
  // creación (dos pestañas del navegador a la vez).
  const tick = async () => {
    if (!_muxNueva.leyendo || _muxNueva.job !== job) return;
    const p = await apiFetch('/api/mux/create/progress', { silent: true }).catch(() => null);
    // El sello de la respuesta Y el de la creación que se está leyendo AHORA:
    // un tick que volviera tarde, con otra creación ya en marcha, pintaría
    // los pasos de la anterior.
    if (p && p.job === job && _muxNueva.leyendo && _muxNueva.job === job) _muxPintarPasos(p);
    if (_muxNueva.leyendo && _muxNueva.job === job) _muxNueva.timer = setTimeout(tick, 500);
  };
  _muxNueva.timer = setTimeout(tick, 300);
  let r = null;
  try {
    r = await apiFetch(`/api/mux/create?job=${encodeURIComponent(job)}`, {
      method: 'POST', body: JSON.stringify({ a: a.ruta, b: b.ruta }),
    }, API_FETCH_TIMEOUT_LARGO);
  } finally {
    _muxNueva.leyendo = false;
    if (_muxNueva.timer) { clearTimeout(_muxNueva.timer); _muxNueva.timer = null; }
  }
  if (sub) sub.textContent = tr('ui.elige_los_dos_mkv');
  if (!r || !r.id) {
    // `apiFetch` ya ha dicho por qué en un toast; aquí se deja el motivo a
    // la vista y se vuelve a la selección, que sigue puesta.
    const p = await apiFetch('/api/mux/create/progress', { silent: true }).catch(() => null);
    // Si mientras tanto empezó otra creación, el modal es de ella.
    if (_muxNueva.leyendo || _muxNueva.job !== job) return;
    _muxNueva.error = (p && p.job === job && p.error) || tr('tab4.no_se_pudieron_leer');
    _muxPintarHuecos();
    return;
  }
  closeModal('mux-nueva-modal');
  await abrirMux(r.id, r);
  refrescarMuxLista();
  refrescarWorkbar();
}

// ═══════════════════════════════════════════════════════════════════════════
//  La columna de trabajo: los dos detalles de esta pestaña
// ═══════════════════════════════════════════════════════════════════════════

/** La ejecución a la que apunta una línea del historial (`mux:{id}#{n}`). */
function _muxEjecucionDe(a, d) {
  const m = /#(\d+)$/.exec((a && a.historial && a.historial.ref_log) || '');
  if (!m || !d) return null;
  return (d.historial || []).find(e => e.run_number === Number(m[1])) || null;
}

function _muxTimelineHTML(a, d, ejec) {
  // Dentro de la función y con `tr()` en cada fila: una tabla de rótulos en
  // el ámbito del módulo se evaluaría al cargar y congelaría el idioma.
  const FASES = [
    ['comprobar_origenes', 'escudo', tr('tab4.fase_a_comprobar_origenes'), tr('tab4.fase_a_que')],
    ['combinar', 'combinar', tr('tab4.fase_b_multiplexar'), tr('tab4.fase_b_que')],
    ['comprobar_y_guardar', 'check', tr('tab4.fase_c_comprobar_y_guardar'), tr('tab4.fase_c_que')],
  ];
  const tiempos = (ejec || {}).fases || {};
  const pasos = FASES.map(([id, glifo, titulo, sub], i) => {
    const secs = tiempos[id];
    const yaPaso = a.terminal ? secs != null : (a.fase_n && i + 1 < a.fase_n);
    return {
      titulo, sub, icono: glifo,
      estado: a.terminal ? (secs != null ? 'done' : 'pending') : undefined,
      nota: yaPaso && secs != null ? tr('comun.completado_p1', { p1: _workbarTiempo(secs) }) : '',
    };
  });
  return timelineDeTrabajo(pasos, a, tr('tab4.fases_del_multiplexado'));
}

registrarDetalleDeTrabajo('combinar', async (a) => {
  const pid = (a && (a.sobre || a.id)) || '';
  const est = {};
  const d = await apiFetch(`/api/mux/${encodeURIComponent(pid)}`, { silent: true, estado: est })
    .catch(() => null);
  // Un timeout no es «se borró» (la regla del adaptador de CMv4.0).
  if (!d && est.status !== 404) return { sinDatos: true };
  const ejec = _muxEjecucionDe(a, d);
  return {
    sinDetalle: d ? '' : 'borrado',
    titulo: (d && d.nombre_salida) || a.que,
    sub: d ? `${d.fuentes[0].nombre} + ${d.fuentes[1].nombre}` : '',
    cartel: cartelDeTmdb(d && d.tmdb_info, d && d.nombre_salida, icono('combinar', 'ico-xl')),
    lateral: _muxTimelineHTML(a, d, ejec),
    pasos: [],
    conLog: true,
    cuerpo: _trabajoLogHTML(ejec ? ejec.output_log : (d && d.output_log)),
  };
});

registrarDetalleDeTrabajo('sincronia_mux', async (a) => {
  const pid = String((a && (a.sobre || a.id)) || '').replace(/^mux_sincronia:/, '');
  const est = {};
  const d = await apiFetch(`/api/mux/${encodeURIComponent(pid)}`, { silent: true, estado: est })
    .catch(() => null);
  if (!d && est.status !== 404) return { sinDatos: true };
  const s = (d && d.sincronia) || {};
  const [hechos, total] = s.progreso || [s.n_puntos || 0, s.n_puntos || 0];
  const vista = {
    // Una línea vieja del historial no tiene su medida guardada: el
    // proyecto conserva la ÚLTIMA. Para esa, el resumen del historial.
    sinDetalle: d ? (a.terminal ? 'efimero' : '') : 'borrado',
    titulo: tr('tab4.medida_de_la_sincronia'),
    sub: (d && d.nombre_salida) || a.que,
    cartel: cartelDeTmdb(d && d.tmdb_info, d && d.nombre_salida, icono('sincronia', 'ico-xl')),
    pasosTitulo: tr('tab4.como_se_mide'),
    pasos: [
      { icono: 'altavoz', titulo: tr('tab4.paso_medida_decodificar'), sub: tr('tab4.paso_medida_decodificar_que') },
      { icono: 'sincronia', titulo: tr('tab4.paso_medida_comparar'), sub: tr('tab4.paso_medida_comparar_que') },
      { icono: 'grafico', titulo: tr('tab4.paso_medida_calcular'), sub: tr('tab4.paso_medida_calcular_que') },
    ],
    conLog: false,
    cuerpo: a.terminal ? '' : _trabajoKvHTML([
      [tr('tab4.puntos_medidos'), tr('workbar.n_de_total', { n: hechos, total })],
      [tr('tab4.pistas_comparadas'), s.pista_a != null ? `A ${s.pista_a} · B ${s.pista_b}` : '—'],
    ]),
  };
  return vista;
});

// Cuando el trabajo cambia —un multiplexado que acaba, una medida que
// termina—, los proyectos abiertos se vuelven a leer y la lista se repinta:
// es lo que hace que el banner pase a «Multiplexado» sin esperar al poll.
alCambiarTrabajos(() => {
  for (const p of openMuxProjects) if (!p._closed && !p._guardando) _muxLeer(p);
  if (document.getElementById('mux-sidebar-list')) {
    if (typeof currentTab !== 'undefined' && currentTab === 4) refrescarMuxLista();
    else renderMuxLista();
  }
});
