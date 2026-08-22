'use strict';
/**
 * tab3.js — Tab 3: Upgrade Dolby Vision CMv4.0.
 *
 * El sidebar de proyectos, las cards por fase, el overlay de ejecución con su
 * log en vivo, el gráfico de sincronización de la Fase D y el modal de creación
 * con la recomendación del sheet y los bins del repo DoviTools.
 */

// ═══════════════════════════════════════════════════════════════════
//  TAB 3 — CMv4.0 BD (inyección de RPU Dolby Vision CMv4.0)
// ═══════════════════════════════════════════════════════════════════

/** Proyectos CMv4.0 abiertos. Cada entrada: {id, subTabId, session, ws, syncData} */
const openCMv40Projects = [];
let activeCMv40SubTabId = null;
let _cmv40SourceSelected = null;
let _cmv40SidebarList = [];
let _cmv40SelectedSidebarId = null;
let _cmv40SortKey = 'modified';
let _cmv40SortDir = 'desc';
// Flag de "auto-resume hecho" — solo intentamos abrir automáticamente el
// proyecto running una vez por sesión de Tab 3 (al primer load). Si el
// usuario cierra el proyecto manualmente, NO reabrimos. Se reset al cambiar
// de tab principal (ver switchTab) para que la siguiente entrada al Tab 3
// vuelva a evaluarlo.
let _cmv40AutoResumeAttempted = false;
let _cmv40Filter = 'all';

// Icono por fase (para el badge del sidebar)
const CMV40_PHASE_ICONS = {
  'created':         '🎨',
  'source_analyzed': '🔍',
  'target_provided': '🎯',
  'extracted':       '✂️',
  'sync_verified':   '📊',
  'sync_corrected':  '📊',
  'injected':        '💉',
  'remuxed':         '📦',
  'validated':       '✅',
  'done':            '✅',
  'error':           '❌',
  'cancelled':       '⏹',
};

const MAX_CMV40_PROJECTS = 5;

// Label humano por nombre de fase (running_phase)
const CMV40_RUNNING_LABELS = {
  'analyze_source':  'Fase A — Analizando MKV origen',
  'target_rpu_mkv':  'Fase B — Extrayendo RPU target',
  'target_rpu_drive':'Fase B — Descargando RPU del repositorio DoviTools',
  'target_rpu_path': 'Fase B — Cargando RPU de carpeta local',
  'extract':         'Fase C — Extrayendo BL/EL y datos per-frame',
  'sync_correct':    'Fase E — Aplicando corrección de sincronización',
  'inject':          'Fase F — Inyectando RPU en EL',
  'remux':           'Fase G — Remuxando MKV final',
  'validate':        'Fase H — Validando MKV final',
};

// Ratios empíricos calibrados contra logs reales del NAS ZFS con Dolby Vision
// P7 FEL drop-in (Zootrópolis 2, 155001 frames, 34 GB HEVC).
// Recalibrados tras aislar per_frame_data regeneration del Fase F real:
// antes el inject aparecia ~407s porque tenia export dovi_tool concurrente
// de 2 min encima. Sin contaminacion el inject real es ~283s → fps ~548.
// Si se cambia de hardware (SSD local vs ZFS sobre HDD), revisitar.
const CMV40_ETA = {
  // ratio respecto a wall time de ffmpeg (fase A) — observados:
  r_extract_rpu: 0.84,   // 157/186 observado (antes 0.92)
  r_demux:       1.30,   // (sin medir en drop-in, valor legacy)
  r_export:      0.19,   // (sin medir en drop-in, valor legacy)
  r_inject:      2.15,   // 388/180 observado en drop-in FEL (2 runs: 387s, 388s). Antes 1.55 era subestimacion
  r_mux:         2.00,   // 373/186 observado (antes 2.15)
  // FPS de cada tool (fallback cuando no hay anchor)
  fps_extract:   1550,   // 155001/100 ≈ 1550
  fps_demux:     1100,
  fps_export:    7000,
  fps_inject:    400,    // 155001/388 observado en drop-in (antes 545 era subestimacion)
  fps_mux:       415,    // 155001/373 ≈ 415 (antes 711)
  // Fallback inicial cuando aun no tenemos ffmpeg_wall_seconds ni tamaño
  // del fichero (sesiones legacy sin parser de tracks). Los usuarios típicos de UHD BD manejan
  // 60-70 GB — ffmpeg extract ~280-330s en NAS ZFS a ~220 MB/s. Antes era 180
  // (caso 35-42 GB) que subestimaba. Con tamaño en session usamos scaling en
  // _cmv40FallbackAnchor; este valor solo aplica a sesiones viejas sin size.
  ffmpeg_wall_fallback_s: 260,
};

/* ── El plan de la matriz de workflows ────────────────────────────────
 *
 * Estas reglas viven en `phases/cmv40_strategy.py` y llegan resueltas en
 * `session.plan`. Aquí solo se leen.
 *
 * Antes se calculaban a mano: el trust efectivo aparecía ONCE veces y en
 * dos variantes sintácticas distintas (`s.trust_override !==
 * 'force_interactive'` y `(s.trust_override || 'auto') !== ...`), señal de
 * que se habían ido copiando. Una réplica de una regla del backend se
 * desincroniza en silencio, y de esa familia era el bug del overlay: la UI
 * decidiendo por su cuenta sobre estado que el servidor ya sabía.
 *
 * El fallback local existe porque el plan no está en tres situaciones:
 * sesiones cacheadas de antes de este cambio, el summary del sidebar (que
 * vacía campos pesados) y el modal de creación (donde aún no hay sesión).
 * Es UNA implementación, no once.
 */
function _cmv40Plan(s) {
  return (s && s.plan) || null;
}

/** Trust que el pipeline honra: gates OK y sin revisión manual forzada. */
function _cmv40Trust(s) {
  const plan = _cmv40Plan(s);
  if (plan) return !!plan.trust_effective;
  return !!s.target_trust_ok && (s.trust_override || 'auto') !== 'force_interactive';
}

/** ¿Se puede saltar la revisión visual de Fase D?
 *
 *  Dos vías: gates OK, o el usuario aceptó una degradación que Fase D no
 *  puede arreglar. Ninguna sobrevive a `force_interactive`.
 *
 *  Esta regla estaba escrita distinta en las dos partes: el backend hacía
 *  `trusted_auto or user_acked` (con el ACK dado se saltaba Fase D aunque el
 *  usuario hubiera pedido revisión manual) y aquí se exigía además que no
 *  hubiera `force_interactive`. Ahora manda la tabla del backend, que adoptó
 *  esta lectura: pedir revisar el sync a mano y aceptar que el grading
 *  diverge son decisiones distintas.
 */
function _cmv40SkipSyncReview(s) {
  const plan = _cmv40Plan(s);
  if (plan && plan.skip_sync_review !== undefined) return !!plan.skip_sync_review;
  if ((s.trust_override || 'auto') === 'force_interactive') return false;
  return !!s.target_trust_ok || !!s.user_acknowledged_degradation;
}

/** Bin P7 FEL CMv4.0 ya cocinado sobre source P7 FEL con gates OK: se
 *  inyecta sobre BL+EL sin demux ni mux. */
function _cmv40DropIn(s) {
  const plan = _cmv40Plan(s);
  if (plan) return !!plan.drop_in;
  return _cmv40Trust(s)
    && s.target_type === 'trusted_p7_fel_final'
    && (s.source_workflow || 'p7_fel') === 'p7_fel';
}

/** El bin no encaja como reemplazo directo del RPU del source: hay que
 *  transferirle los levels CMv4.0. */
function _cmv40TargetNeedsMerge(s) {
  const plan = _cmv40Plan(s);
  if (plan) return !!plan.target_needs_merge;
  return ['trusted_p7_fel_final', 'trusted_p7_mel_final', 'generic']
    .includes(s.target_type);
}

/** Deduce el label de la siguiente fase que el auto-pipeline disparara,
 *  a partir del phase actual (sin running_phase). Usado en el subtitulo del
 *  overlay durante el "puente" entre fases para mostrar algo util en vez
 *  del antiguo "Preparando siguiente fase..." vago. */
function _cmv40GuessNextPhase(s) {
  const trust = _cmv40Trust(s);
  switch (s.phase) {
    case 'created':          return 'Fase A — Analizando MKV origen';
    case 'source_analyzed':  return 'Fase B — Preparando RPU target';
    case 'target_provided':  return 'Fase C — Separando capas';
    case 'extracted':        return trust ? 'Fase F — Inyectando RPU (drop-in)' : 'Fase D — Revisión visual';
    case 'sync_verified':    return 'Fase F — Inyectando RPU';
    case 'sync_corrected':   return 'Fase F — Inyectando RPU';
    case 'injected':         return 'Fase G — Ensamblando MKV';
    case 'remuxed':          return 'Fase H — Validando resultado';
    case 'validated':        return 'Fase H — Finalizando';
    default:                 return '';
  }
}

// Mapeo de running_phase backend → step key del timeline
const CMV40_RUNNING_TO_STEP = {
  'preflight':        'PREFLIGHT',
  'analyze_source':   'A',
  'target_rpu_path':  'B',
  'target_rpu_mkv':   'B',
  'target_rpu_drive': 'B',
  'extract':          'C',
  'sync_correct':     'E',
  'inject':           'F',
  'remux':            'G',
  'validate':         'H',
};

/** Duración (segs) real de un step completado, leída de session.phase_history.
 *  Devuelve null si no hay entrada o falta alguna marca de tiempo. */
function _cmv40StepElapsedSecs(stepKey, s) {
  const hist = s && s.phase_history;
  if (!Array.isArray(hist) || !hist.length) return null;
  // Recorremos en orden y acumulamos duración de TODAS las entradas cuyo
  // running_phase mapea al stepKey (Fase B puede tener varios intentos).
  let total = 0, found = false;
  for (const h of hist) {
    if (!h || !h.phase) continue;
    if (CMV40_RUNNING_TO_STEP[h.phase] !== stepKey) continue;
    if (!h.started_at || !h.finished_at) continue;
    const a = Date.parse(h.started_at);
    const b = Date.parse(h.finished_at);
    if (!isFinite(a) || !isFinite(b) || b < a) continue;
    total += (b - a) / 1000;
    found = true;
  }
  return found ? total : null;
}

/** Calcula el anchor fallback (segundos estimados de ffmpeg extract) a partir
 *  del tamaño del MKV origen. El usuario tipico de UHD BD maneja 40-70 GB;
 *  usar 180s constante subestimaba cuando el MKV era grande.
 *
 *  Calibracion: NAS ZFS observado ~239 MB/s en ffmpeg -c copy. Tomamos 220
 *  MB/s como margen conservador (mejor sobreestimar el ETA que quedarse corto).
 *  Para 42 GB sale ~195s (observado 182s, +7%), para 65 GB ~302s, para 70 GB ~326s.
 */
function _cmv40FallbackAnchor(s) {
  const size = s && s.source_file_size_bytes;
  if (size && size > 0) {
    const mbps = 220;                       // MB/s — margen conservador
    const secs = size / (mbps * 1024 * 1024);
    return Math.max(60, Math.min(900, secs));   // clamp [60s, 15min]
  }
  return CMV40_ETA.ffmpeg_wall_fallback_s;   // 180s por defecto si no hay size
}

/** Estima segundos de una sub-tarea usando ffmpeg wall time (anchor) o
 *  frame_count × fps como fallback. */
function _cmv40EstimateSecs(s, ratio, fps, anchorOverride) {
  // anchorOverride: ancla ya normalizada a "ffmpeg puro" por el llamador.
  // Necesario desde que ffmpeg_wall_seconds puede medir ffmpeg+extract-rpu
  // solapados (pipe de Fase A): usarla tal cual inflaría todo lo demás.
  const base = (anchorOverride != null && anchorOverride > 0)
    ? anchorOverride
    : (s.ffmpeg_wall_seconds || 0);
  if (base > 0) {
    return Math.max(5, base * ratio);
  }
  if (s.source_frame_count && s.source_frame_count > 0) {
    return Math.max(5, s.source_frame_count / fps);
  }
  // Fallback escalado al tamaño del fichero origen (si disponible), evitando
  // el salto de "5 min → 25 min" cuando llega el anchor real de Fase A.
  return Math.max(5, _cmv40FallbackAnchor(s) * ratio);
}

/** Formatea segundos como "Xm Ys" o "~Xm". */
function _cmv40FmtEta(secs) {
  if (!secs || secs <= 0) return '—';
  const s = Math.round(secs);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 10 && rem > 0) return `${m}m ${rem}s`;
  return `${m}m`;
}

// Modelo de ETA medido del histórico de esta instalación (GET
// /api/cmv40/eta-model). Sustituye a los ratios de CMV40_ETA para las fases
// que aún no han empezado. Vacío hasta que se carga; entonces se usan las
// constantes, que es el comportamiento de siempre.
let CMV40_ETA_MODEL = null;

async function _cmv40LoadEtaModel() {
  try {
    const d = await apiFetch('/api/cmv40/eta-model', { silent: true });
    if (d && (d.dropin || d.merge)) CMV40_ETA_MODEL = d;
  } catch (_) { /* nos quedamos con las constantes */ }
}

/** Ratio de una fase respecto a la Fase A: medido si hay muestras
 *  suficientes, constante calibrada a mano si no. */
function _cmv40RatioFase(fase, dropIn, porDefecto, rutaDesconocida) {
  const mod = CMV40_ETA_MODEL;
  if (!mod) return porDefecto;
  // Los primeros segundos de un job no se sabe la ruta: el pre-flight aún no
  // ha clasificado el bin. Antes se asumía merge (la cara) y salían 48 min
  // para jobs de 35. Con la mezcla real de esta instalación se acierta más.
  if (rutaDesconocida && typeof mod.share_dropin === 'number') {
    const d = mod.dropin && mod.dropin[fase];
    const g = mod.merge && mod.merge[fase];
    if (typeof d === 'number' && typeof g === 'number') {
      return d * mod.share_dropin + g * (1 - mod.share_dropin);
    }
    return (typeof d === 'number' ? d : (typeof g === 'number' ? g : porDefecto));
  }
  const m = mod[dropIn ? 'dropin' : 'merge'];
  const v = m && m[fase];
  return (typeof v === 'number' && v > 0) ? v : porDefecto;
}

/** Segundos que lleva ejecutándose una fase, según su registro abierto en
 *  phase_history. null si no está corriendo. */
function _cmv40PhaseStartedSecs(s, phase) {
  const hist = s.phase_history || [];
  const cur = [...hist].reverse().find(h => h.phase === phase && !h.finished_at);
  if (!cur || !cur.started_at) return null;
  const ms = Date.parse(cur.started_at);
  if (!isFinite(ms)) return null;
  return Math.max(0, (Date.now() - ms) / 1000);
}

/** Plan de pasos del auto-pipeline según workflow + trust del proyecto.
 *  Devuelve array ordenado de objetos con {key, icon, title, what, etaSecs}. */
function _cmv40PlanAutoSteps(s, project) {
  const wf = s.source_workflow || 'p7_fel';
  const trust = _cmv40Trust(s);
  // `target_trust_ok` no se evalúa hasta Fase B, pero el pre-flight ya dejó
  // clasificado el bin. Sin anticiparlo, durante toda la Fase A el plan
  // asume ruta merge y suma un demux, un export y una validación completa
  // que no van a ejecutarse: ~13 min de fantasma en un UHD BD (reportado
  // con M3GAN 2.0: 49 min estimados para un job de ~26).
  // NO es `_cmv40DropIn`: esto es una PREDICCIÓN válida solo antes de que
  // Fase B evalúe los gates (no mira target_trust_ok, que aún no existe).
  // El drop-in real lo dice el plan del backend.
  const dropInProbable = s.target_type === 'trusted_p7_fel_final'
                       && s.trust_override !== 'force_interactive'
                       && (wf === 'p7_fel')
                       && !s.error_message;
  // La predicción solo vale ANTES de que Fase B evalúe los gates. Después
  // manda el dato real: un bin trusted_p7_fel_final que no pase los gates
  // va por merge, y el plan tiene que reflejarlo.
  const gatesHechos = CMV40_PHASES_ORDER.indexOf(s.phase)
                    >= CMV40_PHASES_ORDER.indexOf('target_provided');
  const dropIn = gatesHechos
    ? (trust && s.target_type === 'trusted_p7_fel_final' && wf === 'p7_fel')
    : dropInProbable;
  // Ni siquiera hay predicción posible mientras el pre-flight no haya
  // clasificado el bin (los primeros ~10s de un job).
  const rutaDesconocida = !gatesHechos && !_cmv40BinClasificado(s);
  const skipped = s.phases_skipped || [];

  // ETAs estimados.
  //
  // El ancla es el tiempo de la extracción de Fase A. Desde que ffmpeg y
  // extract-rpu van por un pipe, ese tiempo YA incluye los dos (lo marca
  // ffmpeg_wall_includes_rpu), así que multiplicarlo por (1+r_extract_rpu)
  // contaría el extract dos veces.
  let anchor = s.ffmpeg_wall_seconds || 0;
  let anchorEsFaseA = !!s.ffmpeg_wall_includes_rpu;
  // Mejor todavía: si la Fase A está corriendo AHORA, su ETA medida (ritmo
  // real de este job) proyecta un ancla mucho más fiable que el teórico de
  // tamaño ÷ 220 MB/s. Lo proyectado es la fase A ENTERA, no el ffmpeg solo.
  if (!anchor && project && project._phaseEtaSecs != null
      && s.running_phase === 'analyze_source') {
    const empezado = _cmv40PhaseStartedSecs(s, 'analyze_source');
    if (empezado != null) {
      anchor = empezado + project._phaseEtaSecs;
      anchorEsFaseA = true;
    }
  }
  // Todo lo de abajo se estima sobre el ffmpeg "puro", que es contra lo que
  // están calibrados los ratios. Si el ancla mide la Fase A completa
  // (ffmpeg + extract-rpu por el pipe), se descuenta la parte del extract.
  const anchorFfmpeg = anchorEsFaseA
    ? anchor / (1 + CMV40_ETA.r_extract_rpu)
    : anchor;
  const etaA = anchor > 0
    ? (anchorEsFaseA ? anchor : anchor * (1 + CMV40_ETA.r_extract_rpu))
    : _cmv40EstimateSecs(s, 1.0 + CMV40_ETA.r_extract_rpu, CMV40_ETA.fps_extract);
  const etaB = s.target_rpu_source === 'drive' ? 30
             : s.target_rpu_source === 'mkv'   ? _cmv40EstimateSecs(s, 1.0 + CMV40_ETA.r_extract_rpu, CMV40_ETA.fps_extract)
             : 10;  // path: copia local
  // Drop-in trusted: Fase C no hace nada — cero demux, cero per_frame.
  // Esta es la parte que mas desajustaba el ETA antes: el etaC calculado
  // (~300-400s en un UHD BD) inflaba el total inicial y luego se evaporaba
  // al detectar trust, causando el salto visible de 25 → 15 min.
  const pesoMerge = rutaDesconocida && CMV40_ETA_MODEL
    ? (1 - (CMV40_ETA_MODEL.share_dropin ?? 0.5)) : 1;
  const etaDemux = (wf === 'p8' || dropIn)
    ? 0
    : _cmv40EstimateSecs(s, CMV40_ETA.r_demux, CMV40_ETA.fps_demux, anchorFfmpeg) * pesoMerge;
  const etaExport = _cmv40EstimateSecs(s, CMV40_ETA.r_export * 2, CMV40_ETA.fps_export, anchorFfmpeg);  // ×2 por ambos RPUs
  // El export per-frame también desaparece en drop-in (Fase C entera se
  // salta), así que mientras la ruta esté por saber pesa igual que el demux.
  // Sin esto la mitad de la Fase C se contaba a precio de merge y el total
  // inicial salía inflado.
  const etaC = etaDemux + ((trust || dropIn) ? 0 : etaExport * pesoMerge);
  // Si el histórico da un ratio medido para esta ruta, se usa contra la
  // duración de la Fase A (que es su referencia). Si no, la estimación de
  // siempre sobre el ffmpeg puro.
  const rInject = _cmv40RatioFase('inject', dropIn, null, rutaDesconocida);
  const etaF = (rInject && etaA > 0)
    ? Math.max(5, etaA * rInject)
    : _cmv40EstimateSecs(s, CMV40_ETA.r_inject, CMV40_ETA.fps_inject, anchorFfmpeg);
  const rRemux = _cmv40RatioFase('remux', dropIn, null, rutaDesconocida);
  const etaG = (rRemux && etaA > 0)
    ? Math.max(5, etaA * rRemux)
    : ((wf === 'p7_fel') ? _cmv40EstimateSecs(s, CMV40_ETA.r_mux, CMV40_ETA.fps_mux, anchorFfmpeg) : 30);
  // Fase H: depende del modo. Calibrado con runs reales en NAS UHD BD:
  // - Drop-in FEL (caso típico): ffprobe + mkvmerge -J + rename atómico.
  //   ffprobe sobre MKV 71 GB → ~1s; mkvmerge -J → ~1s; rename mismo
  //   filesystem instantáneo; cleanup unlinks <1s. Total real ~3-5s; 5s
  //   con margen.
  // - Path clásico (merge CMv4.0): extract-rpu COMPLETO del HEVC pre-mux
  //   + dovi_tool info + mkvmerge -J. El extract-rpu sobre el HEVC entero
  //   (~60-80 GB en UHD) toma ~5-8 min (heurística backend: hevc_gb/30*3
  //   a hevc_gb/30*5 min). Sin ancla ffmpeg_wall_seconds usamos 240s
  //   (4 min) como fallback razonable; si tenemos ancla, el extract-rpu
  //   ronda 0.92× ffmpeg wall time (mismo ratio que Fase A).
  const etaH = dropIn
    ? 5
    : Math.round((anchorFfmpeg > 0 ? anchorFfmpeg * 0.92 : 240) * pesoMerge);

  const steps = [];

  // Pre-flight: sniff DV del origen + dovi_tool info del bin target.
  // Backend: running_phase='preflight'. Phase backend NO cambia (sigue
  // 'created'); el progreso se trackea via source_preflight_ok flag.
  // Edge cases manejados en _cmv40StepStatus mapping de PREFLIGHT.
  const phasePastSource = CMV40_PHASES_ORDER.indexOf(s.phase) >= CMV40_PHASES_ORDER.indexOf('source_analyzed');
  let preflightStatus;
  if (s.running_phase === 'preflight') {
    preflightStatus = 'running';
  } else if (s.source_preflight_ok === true || phasePastSource) {
    preflightStatus = 'done';
  } else if (s.running_phase === 'analyze_source') {
    // Sesion legacy o pre-flight saltado: Fase A corriendo sin flag de
    // preflight. Lo marcamos como skipped para no confundir.
    preflightStatus = 'skipped';
  } else {
    preflightStatus = 'pending';
  }
  steps.push({
    key: 'PREFLIGHT', icon: '🔬', title: 'Pre-flight · Validación rápida',
    what: 'Sniff DV del MKV origen + descarga + dovi_tool info del bin + análisis combos L2/L8 + clasificación de calidad (CMv4 CORE/CORE+/FULL) + recomendación Mantener vs Inyectar RPU (~30-60s, aborta o recomienda Mantener antes de gastar Fase A)',
    etaSecs: 45,
    forcedStatus: preflightStatus,
  });

  steps.push({
    key: 'A', icon: '🔍', title: 'Fase A · Analizar MKV origen',
    what: 'ffmpeg copia el HEVC + dovi_tool extract-rpu + info + análisis combos L2 del source + comparación L2 source vs target → recomendación final del modelo (drop-in / merge / mantener)',
    etaSecs: etaA,
  });
  // Fase B: si el pre-flight ya descargó/copió/extrajo el bin, aquí se reusa
  // del workdir y solo se re-evalúan los trust gates con los datos del source
  // recién extraído en Fase A. Texto refleja ese rol real.
  const bWhat = s.target_rpu_source === 'drive' ? 'Reusa el bin del workdir (descargado en pre-flight) + re-evalúa trust gates con datos del source: frames, L5/L6 zoneados, compatibilidad estructural'
              : s.target_rpu_source === 'mkv' ? 'Reusa el RPU del workdir (extraído en pre-flight) + re-evalúa trust gates con datos del source: frames, L5/L6, compatibilidad'
              : 'Reusa el bin del workdir (copiado en pre-flight) + re-evalúa trust gates con datos del source: frames, L5/L6, compatibilidad';
  steps.push({
    key: 'B', icon: '🎯', title: 'Fase B · Preparar RPU target',
    what: bWhat, etaSecs: etaB,
  });

  // Gate B→C: validaciones estructurales + trust gates del target
  // Se evalúa al cerrar Fase B. No gasta tiempo (es una comprobación in-memory).
  // Visible siempre en el timeline para dar trazabilidad de la decisión.
  const curIdxForGate = CMV40_PHASES_ORDER.indexOf(s.phase);
  const targetProvidedIdx = CMV40_PHASES_ORDER.indexOf('target_provided');
  const gateBCStatus = s.compat_warning ? 'error'
                    : (curIdxForGate < targetProvidedIdx) ? 'pending'
                    : 'done';
  const failingGates = Object.entries(s.target_trust_gates || {})
    .filter(([k, v]) => typeof v === 'object' && v && v.ok === false)
    .map(([k]) => k);
  let gateBCLabel;
  if (s.compat_warning) {
    gateBCLabel = 'incompatible · abortada';
  } else if (curIdxForGate < targetProvidedIdx) {
    gateBCLabel = 'pendiente';
  } else if (s.target_trust_ok) {
    gateBCLabel = 'trusted ✓';
  } else if (failingGates.length) {
    gateBCLabel = `${failingGates.length} gate${failingGates.length > 1 ? 's' : ''} ⚠ revisión manual`;
  } else {
    gateBCLabel = 'flujo manual';
  }
  const gateBCWhat = s.compat_warning
    ? s.compat_warning.slice(0, 140) + (s.compat_warning.length > 140 ? '…' : '')
    : 'Comparación target vs source RPU: frames · CM version · L8 · L5/L6/L1 · compatibilidad estructural';
  steps.push({
    key: 'GATE_BC', icon: '🛡️', title: 'Validaciones — trust gates + compatibilidad',
    what: gateBCWhat, etaSecs: 0,
    forcedStatus: gateBCStatus, customLabel: gateBCLabel,
    isGate: true,
  });

  // Fase C: si el backend marco tanto demux_dual_layer como per_frame_data_skipped,
  // la fase no hizo trabajo real (caso drop-in trusted) — mostrar como 'skipped'
  // en el timeline con label descriptivo en vez de 'done · 00:00'.
  const demuxSkipped = skipped.includes('demux_dual_layer');
  const pfdSkipped   = skipped.includes('per_frame_data_skipped');
  const cFullySkipped = demuxSkipped && (pfdSkipped || (wf === 'p8' && skipped.length));
  let cWhat, cForcedStatus = null, cLabel = null;
  if (cFullySkipped) {
    cWhat = dropIn
      ? 'Omitida — drop-in FEL: sin demux ni per-frame (inject directo sobre source.hevc)'
      : 'Omitida — target trusted, no se necesitan capas separadas ni chart';
    cForcedStatus = 'skipped';
    cLabel = 'omitida · drop-in';
  } else {
    cWhat = (wf === 'p8') ? 'Workflow P8 — sin demux' + (trust ? ' (per-frame omitido)' : ', genera per-frame data')
                          : 'dovi_tool demux → BL' + (wf === 'p7_fel' ? ' + EL' : '') + (trust ? ' (per-frame omitido)' : ' + per-frame data');
  }
  steps.push({
    key: 'C', icon: '✂️', title: 'Fase C · Demux + per-frame',
    what: cWhat, etaSecs: cFullySkipped ? 0 : etaC,
    forcedStatus: cForcedStatus, customLabel: cLabel,
  });
  steps.push({
    key: 'D', icon: '📊', title: 'Fase D · Verificar sincronización',
    what: trust
      ? 'Omitida — gates validaron frame count + L5/L6/L8'
      : 'Chart interactivo de sincronización: alinear las curvas MaxCLL de source y target (correlación Pearson ≥ 85% + Δ frames = 0) antes de inyectar',
    etaSecs: trust ? 0 : null,   // null = desconocido (interactivo)
    forcedStatus: trust ? 'skipped' : null,
  });
  // Fase E — corrección de sync (dovi_tool editor remove/duplicate).
  // Estado depende de la combinación (trust, hasSyncCfg, fase actual):
  //   · trusted+auto                       → omitida por gates
  //   · no-trusted + fase < sync_verified  → PENDING (aún no sabemos si hará falta)
  //   · no-trusted + fase ≥ sync_verified + sin sync_config → omitida (Δ=0)
  //   · con sync_config                    → aplicada (se ejecutó Fase E)
  //   · running_phase == 'sync_correct'    → running (cubierto por el mapping)
  const hasSyncCfg = !!(s.sync_config && Object.keys(s.sync_config).length);
  const curIdx = CMV40_PHASES_ORDER.indexOf(s.phase);
  const syncVerIdx = CMV40_PHASES_ORDER.indexOf('sync_verified');
  const pastSyncVerified = curIdx >= syncVerIdx;
  let eStatus = null, eLabel = null;
  if (trust) {
    eStatus = 'skipped';
    eLabel = 'omitida · gates Δ=0';
  } else if (hasSyncCfg) {
    // Corrección aplicada; _cmv40StepStatus decide done/running/pending según
    // la fase actual. El customLabel se usa cuando esté done.
    eLabel = 'aplicada';
    eStatus = null;
  } else if (pastSyncVerified) {
    // Usuario confirmó sync sin corrección — Δ era 0 tras revisión.
    eStatus = 'skipped';
    eLabel = 'omitida · Δ=0 confirmado';
  }
  // (caso restante: no-trusted + sin sync_config + pre-sync_verified →
  //  eStatus/eLabel null → _cmv40StepStatus decide 'pending'.)
  const eWhat = hasSyncCfg
    ? 'dovi_tool editor — remove/duplicate frames según config'
    : (trust || pastSyncVerified
        ? 'No requerida — el RPU target alinea con el source'
        : 'Solo si Fase D detecta desfase de frames');
  steps.push({
    key: 'E', icon: '🔧', title: 'Fase E · Corrección de sync',
    what: eWhat,
    etaSecs: hasSyncCfg ? 20 : 0,
    forcedStatus: eStatus,
    customLabel: eLabel,
  });
  // Fase F: la ruta concreta depende del workflow y del target_type.
  // - drop-in FEL: inyecta el bin sobre source.hevc (BL+EL juntos), sin merge ni mux.
  // - p7_fel non-drop-in: merge CMv4.0 sobre RPU P7 + inyecta en EL.hevc.
  // - p7_mel y p8: merge solo cuando target ∈ {p7_fel_final, p7_mel_final, generic};
  //   con target P8 retail (trusted_p8_source) es inject directo sin merge.
  //   Alineado con _do_merge() y target_needs_merge en cmv40_pipeline.py.
  const targetNeedsMerge = _cmv40TargetNeedsMerge(s);
  let fWhat;
  if (dropIn) {
    fWhat = 'Drop-in — inyecta el RPU del bin sobre source.hevc (BL+EL juntos, sin merge ni mux posterior)';
  } else if (wf === 'p7_fel') {
    fWhat = 'Merge CMv4.0 sobre RPU P7 del source + inyecta el RPU merged en EL.hevc (preserva FEL)';
  } else if (wf === 'p7_mel') {
    fWhat = targetNeedsMerge
      ? 'Merge CMv4.0 sobre RPU P7 MEL del source + inyecta el RPU merged en BL.hevc (descarta EL MEL → P8.1)'
      : 'Inyecta el RPU target directamente en BL.hevc (target P8 retail, sin merge — descarta EL MEL → P8.1)';
  } else {  // p8
    fWhat = targetNeedsMerge
      ? 'Merge CMv4.0 sobre RPU P8 del source + inyecta el RPU merged en source.hevc'
      : 'Inyecta el RPU target directamente en source.hevc (target P8 retail, sin merge)';
  }
  steps.push({
    key: 'F', icon: '💉', title: 'Fase F · Inyectar RPU',
    what: fWhat, etaSecs: etaF,
  });
  // Fase G: tres rutas distintas según workflow/modo.
  // - drop-in FEL: source_injected.hevc ya es BL+EL dual-layer → mkvmerge directo.
  // - p7_fel non-drop-in: dovi_tool mux combina BL + EL_injected → mkvmerge.
  // - p7_mel / p8: BL_injected.hevc single-layer → mkvmerge directo.
  let gWhat;
  if (dropIn) {
    gWhat = 'mkvmerge directo sobre source_injected.hevc (BL+EL dual-layer ya combinado en Fase F) con audio/subs/capítulos del MKV origen';
  } else if (wf === 'p7_fel') {
    gWhat = 'dovi_tool mux combina BL.hevc + EL_injected.hevc en un HEVC dual-layer + mkvmerge añade audio/subs/capítulos del MKV origen';
  } else {  // p7_mel / p8
    gWhat = 'Sin mux dual-layer (single-layer) — mkvmerge directo sobre BL_injected.hevc con audio/subs/capítulos del MKV origen';
  }
  steps.push({
    key: 'G', icon: '📦', title: 'Fase G · Remux MKV final',
    what: gWhat, etaSecs: etaG,
  });

  // Fase H = validar + finalizar. El backend unifica en running_phase='validate'
  // dos rutas según modo:
  // - Drop-in FEL: ffprobe (frame count) + mkvmerge -J. Sin extract-rpu
  //   porque la cadena upstream ya garantiza Profile 7 FEL CMv4.0. ~5-10s.
  // - Path clásico (merge CMv4.0): extract-rpu COMPLETO del HEVC pre-mux
  //   (BL_injected/EL_injected/source_injected según workflow) + dovi_tool
  //   info → valida frame count del RPU vs expected (±2), cm_version=v4.0,
  //   el_type correcto, L8 presente. Después mkvmerge -J. ~5-8 min en UHD.
  //   NO usa muestreo HEAD+TAIL: aunque más lento, garantiza frame count
  //   total del RPU (un bug que cortara el RPU a la mitad pasaría
  //   desapercibido con muestreo).
  // En ambos: rename atómico .tmp → .mkv + cleanup pre-mux.
  steps.push({
    key: 'H', icon: '✅', title: 'Fase H · Validar + finalizar',
    what: dropIn
      ? 'Validación rápida (ffprobe frame count + mkvmerge -J — el RPU es bit-a-bit el bin pre-validado) → rename atómico → cleanup'
      : 'Validación rigurosa: extract-rpu completo del HEVC pre-mux + dovi_tool info → confirma frame count, CMv4.0, el_type, L8 presente. Más mkvmerge -J. → rename atómico → cleanup',
    etaSecs: etaH,
  });

  return steps;
}

/** Estado de cada step según session.phase + running_phase + phases_skipped. */
function _cmv40StepStatus(step, s) {
  if (step.forcedStatus) return step.forcedStatus;
  const PROD = {
    A: 'source_analyzed', B: 'target_provided', C: 'extracted',
    D: 'sync_verified',   E: 'sync_verified',   F: 'injected',
    G: 'remuxed',         H: 'done',
  };
  const order = CMV40_PHASES_ORDER;
  const produces = PROD[step.key];
  const curIdx = order.indexOf(s.phase);
  const prodIdx = order.indexOf(produces);
  const runStep = CMV40_RUNNING_TO_STEP[s.running_phase];

  if (runStep === step.key) return 'running';
  if (prodIdx >= 0 && curIdx >= prodIdx) return 'done';
  return 'pending';
}

// Formatea segundos → "MM:SS" (o "HH:MM:SS" si pasa de 1h)
function _cmv40FmtClock(totalSecs) {
  totalSecs = Math.max(0, Math.floor(totalSecs || 0));
  const h = Math.floor(totalSecs / 3600);
  const m = Math.floor((totalSecs % 3600) / 60);
  const s = totalSecs % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** Devuelve el started_at del proyecto en ms (epoch). Cacheado en
 *  project._resolvedStartedMs para que los tres lugares que computan el
 *  elapsed (full render, incremental update, tick por segundo) usen
 *  EXACTAMENTE el mismo valor.
 *
 *  Sin este cache el render puede usar server-time
 *  (phase_history[0].started_at) mientras el tick lee data-started-at
 *  cacheado en cliente — al alternar uno y otro el contador "salta 3
 *  segundos" y luego "resta 2" porque server clock != client clock por
 *  la latencia de la API. Bug visible al usuario como timer no lineal.
 *
 *  Prioridad para el primer cache:
 *    1. phase_history[0].started_at (autoritativo, server time)
 *    2. Date.now() (solo si hay running o auto activo)
 *  Una vez cacheado, NO se recalcula — la fuente queda fija. */
function _cmv40ResolveStartedMs(s, project) {
  if (project && project._resolvedStartedMs) return project._resolvedStartedMs;
  const hist = (s && s.phase_history) || [];
  const firstWithTime = hist.find(h => h.started_at);
  let startedMs = firstWithTime ? Date.parse(firstWithTime.started_at) : 0;
  if (!startedMs && project) {
    if (s.running_phase || (project.autoContinue && !s.error_message && s.phase !== 'done')) {
      startedMs = Date.now();
    }
  }
  if (startedMs && project) {
    project._resolvedStartedMs = startedMs;
  }
  return startedMs || 0;
}

// Ticker único global que actualiza todos los timers vivos cada segundo.
// Re-calcula elapsed y remaining cada segundo. Elapsed = now - started_at.
// Remaining = baseRemaining (snapshot en render) - (now - baseAt). Así
// decrementa suavemente segundo a segundo entre renders, y solo "salta" al
// recomputar cuando llega una actualización de sesión (transición de fase).
function _cmv40EnsureTimerTick() {
  if (window._cmv40TimerTick) return;
  window._cmv40TimerTick = setInterval(() => {
    document.querySelectorAll('.cmv40-tl-timer-elapsed[data-started-at]').forEach(el => {
      const started = parseInt(el.dataset.startedAt, 10);
      if (!started) return;
      const elapsed = (Date.now() - started) / 1000;
      el.textContent = _cmv40FmtClock(elapsed);
      // Remaining: decrementa desde la snapshot del último render.
      const remainEl = el.closest('.cmv40-tl-progress-meta')
                         ?.querySelector('.cmv40-tl-timer-remaining');
      const baseRem = parseFloat(el.dataset.baseRemaining || 'NaN');
      const baseAt  = parseFloat(el.dataset.baseAt || 'NaN');
      if (remainEl && isFinite(baseRem) && isFinite(baseAt)) {
        const delta = (Date.now() - baseAt) / 1000;
        const remaining = Math.max(0, baseRem - delta);
        // El sufijo lo decide el render (ver _cmv40TextoRestante) y viaja en
        // un data-attribute: si no, este tick de 1s lo pisaba con "(auto)"
        // y el aviso de estimación provisional no llegaba a verse nunca.
        const sufijo = el.dataset.etaSufijo || '(auto)';
        remainEl.textContent = remaining > 0
          ? `~${_cmv40FmtClock(remaining)} restantes ${sufijo}`
          : 'casi listo…';
      }
    });
  }, 1000);
}

/** Texto del tiempo restante. Mientras el pre-flight no ha clasificado el
 *  bin no se sabe la ruta (drop-in o merge cambian el total en ~15 min), así
 *  que el número se marca como provisional en vez de darlo por bueno. */
/** ¿El pre-flight ya clasificó el bin target?
 *
 *  OJO con el sentinel: `target_type` NO sirve para esto — el modelo lo
 *  inicializa a 'generic', así que `!s.target_type` es SIEMPRE falso y los
 *  dos sitios que lo usaban quedaron muertos (ni el reparto por share_dropin
 *  ni el aviso de estimación provisional llegaron a activarse nunca). El
 *  campo que sí nace vacío es `target_dv_info`, que el pre-flight rellena en
 *  la misma línea que target_type. */
function _cmv40BinClasificado(s) {
  return !!s.target_dv_info;
}

function _cmv40SufijoEta(s) {
  const rutaPorSaber = !_cmv40BinClasificado(s)
    && CMV40_PHASES_ORDER.indexOf(s.phase) < CMV40_PHASES_ORDER.indexOf('target_provided');
  return rutaPorSaber ? '(estimado inicial)' : '(auto)';
}

function _cmv40TextoRestante(secs, s) {
  if (secs <= 0) return 'casi listo…';
  return `~${_cmv40FmtClock(secs)} restantes ${_cmv40SufijoEta(s)}`;
}

/** ¿El job está en un estado terminal? Con done/error el porcentaje no debe
 *  salir del último job_pct recibido, que se quedó a medias. */
function isTerminal0(s) {
  return s.phase === 'done' || !!s.error_message || !!s.archived;
}

/** Segundos restantes estimados para las fases AUTO pendientes de ejecución.
 *  Suma los etaSecs de pasos no-done/no-skipped, descontando el tiempo que
 *  lleva ejecutándose el paso en curso. Fase D manual (etaSecs=null) no cuenta. */
function _cmv40ComputeRemainingSecs(s, steps, stepStatuses, hist, project) {
  // ETA MEDIDA de la fase en curso: el backend la calcula con el ritmo real
  // de este job (_ReadProgress.eta), no con una constante. Si la tenemos,
  // sustituye a la estimación de esa fase; las pendientes siguen estimadas
  // porque todavía no han empezado y no hay nada que medir.
  const medida = project && project._phaseEtaSecs;
  let remaining = 0;
  for (let i = 0; i < steps.length; i++) {
    const status = stepStatuses[i];
    if (status === 'done' || status === 'skipped') continue;
    if (status === 'running' && medida != null) {
      remaining += medida;
      continue;
    }
    const eta = steps[i].etaSecs || 0;   // null (manual) → 0
    remaining += eta;
  }
  // Descontar el tiempo que lleva ejecutándose la fase actual (si existe).
  // Con ETA medida no aplica: ya cuenta lo que falta, no el total.
  if (medida == null && s.running_phase && Array.isArray(hist)) {
    const curEntry = [...hist].reverse().find(h => h.phase === s.running_phase);
    if (curEntry && curEntry.started_at && !curEntry.finished_at) {
      const startMs = Date.parse(curEntry.started_at);
      if (isFinite(startMs)) {
        const runningSecs = (Date.now() - startMs) / 1000;
        remaining = Math.max(0, remaining - runningSecs);
      }
    }
  }
  return Math.max(0, Math.round(remaining));
}

/** Renderiza el timeline lateral del auto-pipeline (HTML). */
function _cmv40RenderTimeline(s, project) {
  const steps = _cmv40PlanAutoSteps(s, project);
  // Progreso por #pasos completados (done + skipped) sobre total.
  const stepStatuses = steps.map(st => _cmv40StepStatus(st, s));
  const doneCount = stepStatuses.filter(st => st === 'done' || st === 'skipped').length;
  const totalCount = steps.length;
  // El porcentaje por fases completadas es escalonado: se queda clavado los
  // minutos que dura cada fase. Si el backend manda `job_pct` (ponderado por
  // lo que pesa cada fase y con el avance real de la que corre — ver
  // _cmv40_job_pct), ese manda. El escalonado queda de respaldo.
  const progressPct = (project && project._jobPct != null && !isTerminal0(s))
    ? Math.round(project._jobPct)
    : (totalCount > 0 ? Math.round((doneCount / totalCount) * 100) : 0);

  // Timer — arranque del pipeline cacheado por proyecto via _cmv40ResolveStartedMs.
  // Imprescindible que sea el MISMO valor en full render, incremental update y
  // tick por segundo: si difieren (p.ej. cache cliente vs server timestamp) el
  // contador alterna entre dos valores → "salta 3 / resta 2" visible al usuario.
  const startedMs = _cmv40ResolveStartedMs(s, project);
  const hist = s.phase_history || [];

  const isTerminal = (s.phase === 'done' || !!s.error_message);
  let elapsedLabel  = '—';
  let remainingText = '';
  let timerAttrs    = '';
  if (startedMs) {
    let elapsedSecs;
    if (isTerminal) {
      const lastWithEnd = [...hist].reverse().find(h => h.finished_at);
      const endMs = lastWithEnd ? Date.parse(lastWithEnd.finished_at) : Date.now();
      elapsedSecs = (endMs - startedMs) / 1000;
      remainingText = s.phase === 'done' ? 'finalizado' : (s.error_message ? 'con error' : '');
    } else {
      elapsedSecs = (Date.now() - startedMs) / 1000;
      // Tiempo restante de fases AUTO pendientes. Excluye fases manuales
      // (etaSecs null = interactiva, p.ej. Fase D no-trusted). Descontamos
      // el tiempo que lleva ejecutándose la fase actual para que el contador
      // baje suavemente durante ella.
      const remaining = _cmv40ComputeRemainingSecs(s, steps, stepStatuses, hist, project);
      remainingText = _cmv40TextoRestante(remaining, s);
      // data-base-remaining + data-base-at permiten que el tick de 1s
      // decremente suavemente sin recalcular la suma (evita fluctuaciones
      // por cambios de steps.etaSecs entre renders).
      timerAttrs = ` data-started-at="${startedMs}" data-base-remaining="${remaining}"`
                 + ` data-base-at="${Date.now()}" data-eta-sufijo="${escHtml(_cmv40SufijoEta(s))}"`;
      _cmv40EnsureTimerTick();
    }
    elapsedLabel = _cmv40FmtClock(elapsedSecs);
  }

  const itemsHtml = steps.map((st, i) => {
    const status = stepStatuses[i];
    const iconMap = {
      done:    '<span class="cmv40-tl-status-icon done">✓</span>',
      running: '<span class="cmv40-tl-status-icon running"></span>',
      skipped: '<span class="cmv40-tl-status-icon skipped">⏭</span>',
      pending: '<span class="cmv40-tl-status-icon pending"></span>',
      error:   '<span class="cmv40-tl-status-icon error">✗</span>',
    };
    // Tiempo real de ejecución (solo disponible si la fase se ejecutó en backend)
    const elapsed = status === 'done' ? _cmv40StepElapsedSecs(st.key, s) : null;
    // Label por defecto según status, o customLabel si el step lo especifica.
    // Para done, añadimos el tiempo real ej. "completado · 05:29" si lo hay.
    const doneLabel = elapsed != null
      ? `completado · ${_cmv40FmtClock(elapsed)}`
      : 'completado';
    const defaultLabel = status === 'done'    ? doneLabel
                       : status === 'skipped' ? 'omitida'
                       : status === 'running' ? 'en curso…'
                       : status === 'error'   ? 'incompatible'
                       : `Restante ${_cmv40FmtEta(st.etaSecs)}`;
    const label = st.customLabel || defaultLabel;
    const etaHtml = `<span class="cmv40-tl-eta ${status}">${escHtml(label)}</span>`;
    const gateCls = st.isGate ? ' cmv40-tl-is-gate' : '';
    return `<li class="cmv40-tl-step cmv40-tl-${status}${gateCls}" data-step-key="${escHtml(st.key)}">
      <div class="cmv40-tl-rail">${iconMap[status] || iconMap.pending}</div>
      <div class="cmv40-tl-body">
        <div class="cmv40-tl-title">
          <span class="cmv40-tl-phase-icon">${st.icon}</span>
          <span>${escHtml(st.title)}</span>
        </div>
        <div class="cmv40-tl-what">${escHtml(st.what)}</div>
        ${etaHtml}
      </div>
    </li>`;
  }).join('');

  // Badge 3-estado del modo de ejecucion:
  //   1. Automatico · pendiente de validaciones — antes de Fase B (aun no
  //      se sabe si trusted) o durante Fase B (evaluando gates)
  //   2. Automatico · trusted — gates OK, el pipeline encadena sin revision
  //      manual (drop-in FEL, retail P8, etc)
  //   3. Manual · revision visual — gates no pasan o usuario forzo force_interactive
  //      (Fase D requiere revision en el chart)
  const gatesEvaluated = !!(s.target_trust_gates && Object.keys(s.target_trust_gates).length);
  const targetProvidedIdx = CMV40_PHASES_ORDER.indexOf('target_provided');
  const curPhaseIdx = CMV40_PHASES_ORDER.indexOf(s.phase);
  const beforeGates = curPhaseIdx < targetProvidedIdx || !gatesEvaluated;
  let trustBadge;
  if (beforeGates) {
    trustBadge = '<span class="cmv40-tl-trust-badge pending">⏳ Auto · pendiente validaciones</span>';
  } else if (_cmv40Trust(s)) {
    trustBadge = '<span class="cmv40-tl-trust-badge trusted">🚀 Auto · trusted</span>';
  } else {
    trustBadge = '<span class="cmv40-tl-trust-badge manual">🔬 Manual · revisión visual</span>';
  }

  const progressCls = isTerminal && !s.error_message ? 'cmv40-tl-progress-done'
                    : s.error_message ? 'cmv40-tl-progress-error'
                    : '';

  return `
    <aside class="cmv40-running-timeline">
      <div class="cmv40-tl-header">
        <div class="cmv40-tl-header-top">
          ${trustBadge}
        </div>
        <div class="cmv40-tl-progress ${progressCls}">
          <div class="cmv40-tl-progress-meta">
            <span class="cmv40-tl-timer">
              <span class="cmv40-tl-timer-icon">⏱</span>
              <span class="cmv40-tl-timer-elapsed"${timerAttrs}>${elapsedLabel}</span>
            </span>
            <span class="cmv40-tl-progress-pct">${doneCount}/${totalCount} · ${progressPct}%</span>
            <!-- Fuera del bloque del timer a propósito: así ocupa una línea
                 entera de la fila (ver .cmv40-tl-progress-meta) en vez de
                 competir por el ancho con el chip del porcentaje. -->
            <span class="cmv40-tl-timer-remaining">${escHtml(remainingText)}</span>
          </div>
          <div class="cmv40-tl-progress-track">
            <div class="cmv40-tl-progress-fill" style="width:${progressPct}%"></div>
          </div>
        </div>
      </div>
      <ol class="cmv40-tl-steps">${itemsHtml}</ol>
    </aside>`;
}

// Fases ordenadas secuencialmente
const CMV40_PHASES_ORDER = [
  'created', 'source_analyzed', 'target_provided', 'extracted',
  'sync_verified', 'sync_corrected', 'injected', 'remuxed', 'validated', 'done',
];

// Pretty names por fase
const CMV40_PHASE_LABELS = {
  'created':         'Proyecto creado',
  'source_analyzed': 'Origen analizado',
  'target_provided': 'RPU target listo',
  'extracted':       'BL/EL extraídos',
  'sync_verified':   'Sync verificado',
  'sync_corrected':  'Sync corregido',
  'injected':        'RPU inyectado',
  'remuxed':         'MKV remuxado',
  'validated':       'Validado',
  'done':            'Completado',
  'error':           'Error',
  'cancelled':       'Cancelado',
};

// ── Modal "Nuevo proyecto CMv4.0" ────────────────────────────────

let _cmv40NewTargetTab = 'repo';  // 'repo' | 'path' | 'mkv'
let _cmv40NewTargetSelected = null;  // { kind, value }

/** Punto de entrada al wizard "Nuevo proyecto CMv4.0".
 *  Flujo: file browser primero (paso obligatorio) → al seleccionar MKV se
 *  abre el modal con todo lo demás (target RPU, opciones de auto-pipeline).
 *  Si el usuario cancela el browser sin elegir nada, no se abre nada más. */
async function openNewCMv40Modal() {
  _cmv40SourceSelected = null;
  _cmv40SourceFilename = null;
  _cmv40NewTargetTab = 'repo';
  _cmv40NewTargetSelected = null;
  // Paso 1: file browser. Es la única forma de elegir source MKV ahora.
  openFileBrowser({
    title: 'Nuevo proyecto CMv4.0 · paso 1 de 2',
    subtitle: 'Selecciona el MKV origen (CMv2.9) que quieres procesar',
    roots: [
      { key: 'library',    label: 'Biblioteca', icon: '📚' },
      { key: 'downloaded', label: 'Downloaded', icon: '📥' },
    ],
    onSelect: async (absPath, name) => {
      _cmv40SourceSelected = absPath;
      _cmv40SourceFilename = name;
      // Paso 2: abre el wizard con MKV preseleccionado
      await _showCMv40NewProjectWizard();
    }
  });
}

/** Abre el modal del wizard CMv4.0 ya con el MKV seleccionado.
 *  Llamado desde openNewCMv40Modal (paso 2) o desde "Cambiar MKV"
 *  cuando el usuario quiere reabrir el browser desde dentro del wizard. */
async function _showCMv40NewProjectWizard() {
  const btn = document.getElementById('cmv40-create-btn');
  if (btn) btn.disabled = true;
  const autoCb = document.getElementById('cmv40-new-auto');
  if (autoCb) autoCb.checked = true;
  // Pinta el nombre del MKV seleccionado en el botón de la fila "MKV origen"
  const labelEl = document.getElementById('cmv40-source-btn-label');
  if (labelEl) {
    if (_cmv40SourceFilename) {
      labelEl.textContent = _cmv40SourceFilename;
      labelEl.classList.remove('placeholder');
    } else {
      labelEl.textContent = 'Selecciona MKV…';
      labelEl.classList.add('placeholder');
    }
  }
  // Reset visual de la sección del repo: preview del pipeline + info de
  // candidatos. Sin esto, al reabrir el modal se queda el match anterior.
  const pp = document.getElementById('cmv40-new-pipeline-preview');
  if (pp) { pp.innerHTML = ''; pp.style.display = 'none'; }
  const repoInfo = document.getElementById('cmv40-new-repo-info');
  if (repoInfo) {
    repoInfo.textContent =
      'Se descargará desde la carpeta pública del repositorio DoviTools en Google Drive.';
  }
  // Label del auto-pipeline al estado neutro (sin fases conocidas todavía)
  _cmv40NewUpdateAutoLabel(null);
  _cmv40NewSwitchTargetTab('repo');
  // Si ya hay MKV origen, dispara el lookup de recomendación + repo
  if (_cmv40SourceFilename) {
    _cmv40LoadRecommendation(_cmv40SourceFilename);
    _cmv40NewLoadRepoCandidates();
    _cmv40NewUpdateCreateBtn();
  } else {
    _cmv40LoadRecommendation('');
    _cmv40NewResetRepoList('— Selecciona primero el MKV origen —');
  }
  await _cmv40NewLoadRpus();
  openModal('cmv40-new-modal');
}

// Variables ligadas al picker de MKV origen.
//  _cmv40SourceSelected guarda la RUTA ABSOLUTA del MKV elegido (no solo el
//  filename como antes) — necesario porque el browser navega un árbol con
//  subdirectorios bajo /mnt/library en vez de listar /mnt/output plano.
//  _cmv40SourceFilename guarda solo el nombre, usado para recommendation
//  y match contra el sheet de DoviTools (que matchea por nombre, no path).
let _cmv40SourceFilename = null;

/** Reabre el file browser desde dentro del wizard CMv4.0 (boton "Cambiar MKV").
 *  El browser tiene z-index 220 (vs wizard 200), por lo que se monta ENCIMA
 *  cubriendo el wizard sin necesidad de cerrarlo. Si el usuario selecciona,
 *  actualizamos el state del wizard in-place; si cancela, el browser se
 *  cierra y el wizard re-emerge tal cual estaba. Sin gaps de cobertura modal. */
function openCMv40SourceBrowser() {
  openFileBrowser({
    title: 'Cambiar MKV origen',
    subtitle: 'Selecciona otro MKV para reemplazar el actual',
    roots: [
      { key: 'library',    label: 'Biblioteca', icon: '📚' },
      { key: 'downloaded', label: 'Downloaded', icon: '📥' },
    ],
    onSelect: async (absPath, name) => {
      _cmv40SourceSelected = absPath;
      _cmv40SourceFilename = name;
      const labelEl = document.getElementById('cmv40-source-btn-label');
      if (labelEl) {
        labelEl.textContent = name;
        labelEl.classList.remove('placeholder');
      }
      onCMv40SourceChange(absPath, name);
    }
  });
}

/** Mantenida por compatibilidad (botón ↺ en HTML lo invoca).
 *  Ya no carga /mnt/output — solo limpia el botón para volver a elegir. */
async function loadCMv40SourceList() {
  _cmv40SourceSelected = null;
  _cmv40SourceFilename = null;
  const labelEl = document.getElementById('cmv40-source-btn-label');
  if (labelEl) {
    labelEl.textContent = 'Selecciona MKV…';
    labelEl.classList.add('placeholder');
  }
  _cmv40NewUpdateCreateBtn();
  _cmv40LoadRecommendation('');
}

function onCMv40SourceChange(absPathOrLegacyVal, name) {
  // Compat: si el caller pasa solo un string sin name, asume que era el
  // viejo flujo (filename desde un select). Lo tratamos como filename.
  if (name === undefined) {
    _cmv40SourceFilename = absPathOrLegacyVal || null;
    _cmv40SourceSelected = absPathOrLegacyVal ? '/mnt/output/' + absPathOrLegacyVal : null;
  } else {
    _cmv40SourceSelected = absPathOrLegacyVal || null;
    _cmv40SourceFilename = name || null;
  }
  _cmv40NewUpdateCreateBtn();
  // Recomendación + repo matching usan el FILENAME (por convención del sheet)
  _cmv40LoadRecommendation(_cmv40SourceFilename);
  if (_cmv40NewTargetTab === 'repo') _cmv40NewLoadRepoCandidates();
  else _cmv40NewResetRepoList('— Selecciona primero el MKV origen —');
}

// Token para anular peticiones obsoletas si el usuario cambia de MKV rápido
let _cmv40RecReqId = 0;

async function _cmv40LoadRecommendation(filename) {
  const banner = document.getElementById('cmv40-recommendation-banner');
  if (!banner) return;
  if (!filename) {
    banner.style.display = 'none';
    banner.innerHTML = '';
    banner.className = 'cmv40-rec-banner';
    return;
  }
  const reqId = ++_cmv40RecReqId;
  banner.style.display = 'block';
  banner.className = 'cmv40-rec-banner loading';
  banner.innerHTML = `<div class="cmv40-rec-header">
    <span class="cmv40-rec-spinner-inline"></span>
    <span>Consultando hoja de DoviTools…</span>
  </div>`;
  const qs = '?filename=' + encodeURIComponent(filename);
  const data = await apiFetch('/api/cmv40/recommend-from-filename' + qs);
  if (reqId !== _cmv40RecReqId) return;  // petición obsoleta
  if (!data) {
    banner.style.display = 'none';
    return;
  }
  _cmv40RenderRecommendation(data);
}

// Metadata por columna: icono, label corta, tooltip explicativo
const CMV40_CHIP_META = {
  dv_source:     { icon: '🎬', label: 'Fuente',   help: 'Plataforma de origen del RPU CMv4.0 (iTunes, Disney+, MA, MAX, Fandango, BD-FEL…)' },
  sync:          { icon: '⏱', label: 'Sync',     help: 'Offset de frames entre WEB-DL y Blu-ray + comprobación de L5 (active area / letterbox)' },
  comparisons:   { icon: '🔬', label: 'Verif.',   help: 'Primera sub-columna de Comparisons: tipo de verificación (HDR COMP, plot, nits, sample, shots…)' },
  comparisons_2: { icon: '📊', label: 'Verif. 2', help: 'Segunda sub-columna de Comparisons (suele ser plot, L1, nits…)' },
  notes:         { icon: '📝', label: 'Notas',    help: 'Notas / workflow. Factible suele ser "workflow 2-3"; si no, explica el motivo' },
};

// Fila de tabla key-value — icono + label (columna fija) + valor (flex) + link opcional.
// Uniforme para todos los campos de factibilidad: Fuente, Sync, Verif, Notas.
function _cmv40TableRow(key, value, link, opts = {}) {
  if (!value && !link) return '';
  const m = CMV40_CHIP_META[key] || { icon: '·', label: key, help: '' };
  const valueClass = opts.mono ? 'cmv40-rec-row-value mono' : 'cmv40-rec-row-value';
  const linkHtml = link
    ? `<a class="cmv40-rec-row-link" href="${escHtml(link)}" target="_blank" rel="noreferrer noopener"
         data-tooltip="Abrir: ${escHtml(link)}">Abrir ↗</a>`
    : '';
  return `
    <div class="cmv40-rec-row">
      <div class="cmv40-rec-row-label" data-tooltip="${escHtml(m.help)}">
        <span class="cmv40-rec-row-icon">${m.icon}</span>
        <span>${escHtml(m.label)}</span>
      </div>
      <div class="${valueClass}">${escHtml(value || '—')}</div>
      ${linkHtml}
    </div>`;
}

// Etiqueta de cada bloque de columnas del sheet. La izquierda ("infeasible")
// NO significa "no se puede añadir CMv4.0": evalúa la conversión a P8.1
// single-layer, que es el objetivo de la comunidad pero no el de esta app.
const CMV40_SHEET_SECTION_LABEL = {
  feasible:    { icon: '✅', text: 'Ruta verificada — restore del bloque CMv4.0 sobre el RPU' },
  probably_ok: { icon: '⚠️', text: 'Sección "Not Sure!" — viable pero sin verificación completa' },
  infeasible:  { icon: 'ℹ️', text: 'Ruta de conversión a P8.1 single-layer' },
};

/** Tabla de campos de una fila del sheet (fuente · sync · verif. · notas). */
function _cmv40SheetRowTable(row) {
  const notesKey = row.feasible === false ? 'notes_motivo' : 'notes';
  const cells = [
    _cmv40TableRow('dv_source',     row.dv_source,     row.dv_source_link),
    _cmv40TableRow('sync',          row.sync_offset,   row.sync_link),
    _cmv40TableRow('comparisons',   row.comparisons,   row.comparisons_link),
    _cmv40TableRow('comparisons_2', row.comparisons_2, row.comparisons_2_link),
    _cmv40TableRow(notesKey,        row.notes,         row.notes_link),
  ].filter(Boolean);
  return cells.length ? `<div class="cmv40-rec-table">${cells.join('')}</div>` : '';
}

/**
 * Bloque de una fila cuando el título aparece en varias secciones: cabecera
 * con la sección de origen + tabla. Las filas cuyo motivo no aplica a este
 * flujo (el caso P8) se atenúan y llevan chip explicativo, en vez de
 * presentarse como un rechazo.
 */
function _cmv40RenderSheetRowBlock(row) {
  const meta = CMV40_SHEET_SECTION_LABEL[row.section]
            || CMV40_SHEET_SECTION_LABEL.feasible;
  const notApplicable = row.feasible === false && row.applies_to_our_workflow === false;
  const chip = notApplicable
    ? `<span class="cmv40-rec-na-chip" data-tooltip="Esta app preserva el FEL del disco: nunca aplana a P8.1, así que este impedimento no afecta al resultado.">no aplica a este flujo</span>`
    : '';
  const labels = (row.blocker_labels || [])
    .filter(l => !notApplicable || (row.blocker_labels || []).length > 1)
    .map(l => `<div class="cmv40-rec-blocker">· ${escHtml(l)}</div>`).join('');
  return `
    <div class="cmv40-rec-section${notApplicable ? ' na' : ''}">
      <div class="cmv40-rec-section-head">
        <span>${meta.icon}</span><span>${escHtml(meta.text)}</span>${chip}
      </div>
      ${labels}
      ${_cmv40SheetRowTable(row)}
    </div>`;
}

// Estados del veredicto del sheet, traducidos al flujo de ESTA app (que
// preserva el FEL del disco). El backend los calcula en
// cmv40_recommend._build_verdict; aquí solo se pintan.
//
//   recommended  ✅ verde  — el sheet documenta la ruta de restore CMv4.0
//   caveats      ⚠️ ámbar  — viable, pero con avisos que sí nos afectan
//                            (o fila de la sección "Not Sure!")
//   p8_only_note ℹ️ azul   — lo único que impide el sheet es aplanar a P8.1,
//                            que esta app no hace: NO es un rechazo
//   not_feasible ❌ rojo   — motivos que comprometen el resultado
//   unknown      ❓ gris   — el título no está en la hoja
const CMV40_VERDICT_STYLE = {
  recommended:  { cls: 'ok',       icon: '✅', label: 'Factible' },
  caveats:      { cls: 'caveats',  icon: '⚠️', label: 'Viable con avisos' },
  p8_only_note: { cls: 'p8only',   icon: 'ℹ️', label: 'No convertible a P8.1' },
  not_feasible: { cls: 'ko',       icon: '❌', label: 'No recomendado' },
  unknown:      { cls: 'unknown',  icon: '❓', label: 'Sin datos' },
};

function _cmv40RenderRecommendation(data, containerId) {
  const banner = document.getElementById(containerId || 'cmv40-recommendation-banner');
  if (!banner) return;
  banner.style.display = 'block';
  const status = data.status || 'unknown';
  const style = CMV40_VERDICT_STYLE[status] || CMV40_VERDICT_STYLE.unknown;
  const cls = style.cls;
  const icon = style.icon;
  // El backend manda la etiqueta ya redactada (verdict_label); el mapa local
  // es el fallback para respuestas viejas cacheadas.
  const statusLabel = data.verdict_label || style.label;
  banner.className = 'cmv40-rec-banner ' + cls;

  const matchTitleHtml = data.match_title
    ? (data.title_link
        ? `<a class="cmv40-rec-match-title linked" href="${escHtml(data.title_link)}" target="_blank" rel="noreferrer noopener" data-tooltip="Abrir: ${escHtml(data.title_link)}">${escHtml(data.match_title)} <span class="chip-arrow">↗</span></a>`
        : `<span class="cmv40-rec-match-title">${escHtml(data.match_title)}</span>`)
    : '';

  // Meta compacta (match% · vía TMDb) empotrada en el header para no añadir otra fila
  let metaHtml = '';
  if (data.match_confidence && data.match_confidence > 0) {
    const pct = Math.round(data.match_confidence * 100);
    const viaLabel = data.match_source === 'tmdb' ? 'TMDb' : data.match_source;
    metaHtml = `<div class="cmv40-rec-meta">
      <span class="cmv40-rec-meta-tag" data-tooltip="Similitud entre el título del fichero y la fila de DoviTools">${pct}% match</span>
      <span class="cmv40-rec-meta-tag" data-tooltip="Fuente del matching: TMDb traduce ES→EN">vía ${escHtml(viaLabel)}</span>
    </div>`;
  }

  // Header compacto en una sola línea: icono + estado + separador + match title + meta
  let html = `
    <div class="cmv40-rec-top">
      <span class="cmv40-rec-status-badge ${cls}">
        <span class="cmv40-rec-icon">${icon}</span>
        <span class="cmv40-rec-status-label">${statusLabel}</span>
      </span>
      ${matchTitleHtml ? `<span class="cmv40-rec-match-sep">·</span>${matchTitleHtml}` : ''}
      ${metaHtml}
    </div>`;

  // Explicación del veredicto en una línea (la redacta el backend según la
  // combinación de filas encontradas).
  if (data.verdict_detail) {
    html += `<div class="cmv40-rec-verdict-detail">${escHtml(data.verdict_detail)}</div>`;
  }

  if (status !== 'unknown') {
    // Etiqueta dinámica para "motivo" en filas no factibles — reusa meta de 'notes'
    if (!CMV40_CHIP_META.notes_motivo) {
      CMV40_CHIP_META.notes_motivo = { ...CMV40_CHIP_META.notes, label: 'Motivo' };
    }
    const sheetRows = Array.isArray(data.rows) ? data.rows : [];
    if (sheetRows.length > 1) {
      // El mismo título catalogado en varias secciones: se muestran TODAS.
      // Antes se colapsaban en una y ganaba sistemáticamente la de "no
      // factible", que suele hablar solo de la conversión a P8.1.
      html += sheetRows.map(_cmv40RenderSheetRowBlock).join('');
    } else {
      const row = sheetRows[0] || null;
      html += _cmv40SheetRowTable(row || {
        feasible: data.feasible,
        dv_source: data.dv_source, dv_source_link: data.dv_source_link,
        sync_offset: data.sync_offset, sync_link: data.sync_link,
        comparisons: data.comparisons, comparisons_link: data.comparisons_link,
        comparisons_2: data.comparisons_2, comparisons_2_link: data.comparisons_2_link,
        notes: data.notes, notes_link: data.notes_link,
      });
    }
  } else {
    html += `<div class="cmv40-rec-body">
      El título <strong>${escHtml(data.input_title || '')}</strong>${data.input_year ? ' (' + data.input_year + ')' : ''}`;
    if (data.title_en && data.title_en !== data.input_title) {
      html += ` (TMDb: <em>${escHtml(data.title_en)}</em>)`;
    }
    html += ` no aparece en la hoja de DoviTools (${data.sheet_rows_loaded || 0} títulos revisados). Puedes continuar bajo tu propio criterio.`;
    html += `</div>`;
    if (!data.tmdb_configured) {
      html += `<div class="cmv40-rec-footer">⚠️ Clave de la API de TMDb no configurada — el matching ES→EN es más limitado. Añádela en ⚙︎ Configuración.</div>`;
    }
  }

  // Warning: cuando no tenemos hyperlinks (fuentes xlsx/api/html → ok; csv/disk → sin links)
  const linksOk = ['xlsx', 'api', 'html'].includes(data.sheet_source);
  if (!linksOk && data.sheet_source && data.sheet_source !== 'none') {
    const reason = data.sheets_api_error ||
      'no se pudo leer el sheet vía HTML ni Sheets API';
    html += `<div class="cmv40-rec-warn">
      ⚠️ Los enlaces incrustados en el sheet no están disponibles (fuente actual: <code>${escHtml(data.sheet_source)}</code>).<br>
      <span class="cmv40-rec-warn-detail">${escHtml(reason)}</span>
    </div>`;
  }

  banner.innerHTML = html;
}

function _cmv40NewSwitchTargetTab(tab) {
  _cmv40NewTargetTab = tab;
  ['repo', 'path', 'mkv'].forEach(t => {
    const pane = document.getElementById(`cmv40-new-target-${t}`);
    const btn  = document.getElementById(`cmv40-new-tab-btn-${t}`);
    if (pane) pane.style.display = tab === t ? '' : 'none';
    if (btn)  btn.classList.toggle('active', tab === t);
  });
  _cmv40NewTargetSelected = null;
  _cmv40NewUpdateCreateBtn();
  // Reset del preview al cambiar de tab — sin esto, el HTML del tab previo
  // (p.ej. el preview "Trusted CMv4.0" de un candidato de repo) queda
  // visible al pasar a path/mkv hasta que el usuario haga otra acción.
  _cmv40NewUpdatePipelinePreview();
  if (tab === 'mkv')  _cmv40NewLoadTargetMkvs();
  if (tab === 'path') _cmv40NewLoadRpus();
  if (tab === 'repo' && _cmv40SourceSelected) _cmv40NewLoadRepoCandidates();
}

async function _cmv40NewLoadRpus() {
  const select = document.getElementById('cmv40-new-rpu-select');
  select.innerHTML = '<option value="">— Cargando… —</option>';
  const data = await apiFetch('/api/cmv40/rpu-files');
  select.innerHTML = '<option value="">— Seleccionar RPU —</option>';
  if (data?.files?.length) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = `${f.name} (${_fmtBytes(f.size_bytes)})`;
      select.appendChild(opt);
    });
  } else {
    select.innerHTML = '<option value="">— No hay RPUs en /mnt/cmv40_rpus —</option>';
  }
}

async function _cmv40NewLoadTargetMkvs() {
  const select = document.getElementById('cmv40-new-target-mkv-select');
  select.innerHTML = '<option value="">— Cargando… —</option>';
  const data = await apiFetch('/api/mkv/files-in-isos');
  select.innerHTML = '<option value="">— Seleccionar MKV con CMv4.0 —</option>';
  if (data?.files?.length) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = f.name;
      select.appendChild(opt);
    });
  } else {
    select.innerHTML = '<option value="">— No hay MKVs en el directorio de ISOs —</option>';
  }
}

function onCMv40TargetChange() {
  // Repo: _cmv40NewTargetSelected se mantiene gracias al card-picker
  // (_cmv40NewSelectRepoCandidate). path y mkv siguen usando <select>.
  if (_cmv40NewTargetTab === 'repo') {
    // No hacemos nada aquí; el picker ya llamó a _cmv40NewSelectRepoCandidate.
    _cmv40NewUpdateCreateBtn();
    _cmv40NewUpdatePipelinePreview();
    return;
  }
  const idMap = {
    path: 'cmv40-new-rpu-select',
    mkv:  'cmv40-new-target-mkv-select',
  };
  const id = idMap[_cmv40NewTargetTab];
  const select = document.getElementById(id);
  const val = select ? select.value : '';
  if (!val) {
    _cmv40NewTargetSelected = null;
  } else {
    _cmv40NewTargetSelected = { kind: _cmv40NewTargetTab, value: val };
  }
  _cmv40NewUpdateCreateBtn();
  _cmv40NewUpdatePipelinePreview();
}

/** Calcula el ETA total del pipeline para un tipo de target dado, usando las
 *  constantes calibradas de CMV40_ETA. Se deriva dinámicamente para que
 *  cualquier recalibración de ratios se refleje automáticamente en el modal
 *  sin tocar strings hardcoded. */
function _cmv40ComputeTargetTypeETA(targetType) {
  const anchor = CMV40_ETA.ffmpeg_wall_fallback_s;  // 180s típico UHD BD
  // Partes comunes
  const etaA = anchor + anchor * CMV40_ETA.r_extract_rpu;   // ffmpeg + extract-rpu
  const etaB = 30;                                           // drive download
  const etaH = anchor * CMV40_ETA.r_extract_rpu + 5;         // extract-rpu pre-mux + info
  let etaC, etaF, etaG, etaDE;
  etaDE = 0;  // drop-in trusted salta D y E
  switch (targetType) {
    case 'trusted_p7_fel_final':
      etaC = 0;                                  // sin demux, sin per-frame
      etaF = anchor * CMV40_ETA.r_inject;        // inject sobre source.hevc
      etaG = anchor * CMV40_ETA.r_mux;           // mkvmerge 42 GB dual-layer
      break;
    case 'trusted_p7_mel_final':
      etaC = anchor * CMV40_ETA.r_demux;         // demux solo BL
      etaF = anchor * CMV40_ETA.r_inject;        // inject en BL
      etaG = 30;                                 // mkvmerge single-layer rápido
      break;
    case 'trusted_p8_source':
      etaC = anchor * CMV40_ETA.r_demux;         // demux BL+EL
      etaF = anchor * CMV40_ETA.r_inject;        // merge + inject
      etaG = anchor * CMV40_ETA.r_mux;           // mkvmerge dual-layer
      break;
    default:
      return { tiempo: 'Variable · depende de revisión manual', totalSecs: null };
  }
  const total = etaA + etaB + etaC + etaDE + etaF + etaG + etaH;
  const mins = total / 60;
  const lo = Math.max(1, Math.floor(mins * 0.9));
  const hi = Math.ceil(mins * 1.15);
  return { tiempo: `~${lo}-${hi} min`, totalSecs: total };
}

// Panel explicativo del pipeline que se ejecutará según el tipo de target
// Estructura: cada fase es un pill en el flujo visual. state: 'run' | 'skip'.
// mod: etiqueta opcional bajo el pill (ej. "sin demux"). autoEndsAt: fase tras
// la cual el auto-pipeline se detiene (null = corre hasta H).
// El campo `tiempo` se calcula dinámicamente — ver _cmv40PipelinePreviewHTML.
// IMPORTANTE: el preview se muestra ANTES de Fase A (no conocemos aun el
// profile del source — puede ser P7 FEL, P7 MEL, o P8.1 venido de un MEL
// convertido). Los blurbs cubren las 3 posibilidades para no ser engañosos.
// Las phases pills muestran el caso "trusted-fast-path": cuando los gates
// pasan y el source coincide en estructura con el bin, el flujo es el
// optimo descrito; en las otras combinaciones Fase F hace merge en lugar
// de drop-in (ver matriz completa en cmv40_pipeline.py _execute_fase_f).
const _CMV40_PIPELINE_PREVIEW = {
  trusted_p7_fel_final: {
    icon: '🎯',
    title: 'Bin P7 FEL · CMv4.0 ya cocinado',
    blurb: 'Bin con BL+EL+RPU CMv4.0 listo para drop-in. ' +
           'Comportamiento según tu BD: ' +
           '· P7 FEL → drop-in directo (sin demux, sin merge — máxima velocidad, preserva BL+EL). ' +
           '· P7 MEL → merge de los levels CMv4.0 en el RPU del source, descarta EL del source → P8.1 CMv4.0. ' +
           '· P8.1 (MEL convertido) → merge de los levels CMv4.0 en el RPU P8 del source → P8.1 CMv4.0.',
    cls: 'ok',
    autoEndsAt: null,
    phases: [
      { k: 'A', label: 'Analizar BD',    state: 'run' },
      { k: 'B', label: 'Descargar bin',  state: 'run' },
      { k: 'C', label: 'Demux',          state: 'skip', mod: 'si BD es FEL' },
      { k: 'D', label: 'Verif. visual',  state: 'skip', mod: 'gates trusted' },
      { k: 'E', label: 'Corrección sync', state: 'skip', mod: 'Δ=0 por gates' },
      { k: 'F', label: 'Inyectar',       state: 'run',  mod: 'drop-in o merge' },
      { k: 'G', label: 'Remux MKV',      state: 'run' },
      { k: 'H', label: 'Validar',        state: 'run' },
    ],
  },
  trusted_p7_mel_final: {
    icon: '🎯',
    title: 'Bin P7 MEL · CMv4.0 ya cocinado',
    blurb: 'Bin con BL+EL(MEL)+RPU CMv4.0 listo. El EL del bin (MEL) no aporta calidad, ' +
           'siempre se descarta. Comportamiento según tu BD: ' +
           '· P7 MEL → inyección directa del RPU del bin sobre la BL del source → P8.1 CMv4.0. ' +
           '· P7 FEL → merge de los levels CMv4.0 en el RPU del source, preservando FEL → P7 FEL CMv4.0. ' +
           '· P8.1 (MEL convertido) → merge en el RPU P8 del source → P8.1 CMv4.0.',
    cls: 'ok',
    autoEndsAt: null,
    phases: [
      { k: 'A', label: 'Analizar BD',    state: 'run' },
      { k: 'B', label: 'Descargar bin',  state: 'run' },
      { k: 'C', label: 'Demux',          state: 'run', mod: 'según BD' },
      { k: 'D', label: 'Verif. visual',  state: 'skip', mod: 'gates trusted' },
      { k: 'E', label: 'Corrección sync', state: 'skip', mod: 'Δ=0 por gates' },
      { k: 'F', label: 'Inyectar',       state: 'run',  mod: 'directo o merge' },
      { k: 'G', label: 'Remux MKV',      state: 'run' },
      { k: 'H', label: 'Validar',        state: 'run' },
    ],
  },
  trusted_p8_source: {
    icon: '📦',
    title: 'Bin P8 retail · CMv4.0 completo',
    blurb: 'Bin P8 con CMv4.0 completo (L8 trims + L9/L10/L11). Sirve como donante ' +
           'de metadata CMv4.0 vía dovi_tool editor (allow_cmv4_transfer). ' +
           'Comportamiento según tu BD: ' +
           '· P7 FEL → merge de los levels CMv4.0 en el RPU del source preservando FEL → P7 FEL CMv4.0. ' +
           '· P7 MEL → descarta EL e inyecta el RPU del bin directamente en BL → P8.1 CMv4.0. ' +
           '· P8.1 (MEL convertido) → inyección directa (mismo profile, sin merge) → P8.1 CMv4.0 refinado.',
    cls: 'info',
    autoEndsAt: null,
    phases: [
      { k: 'A', label: 'Analizar BD',    state: 'run' },
      { k: 'B', label: 'Descargar bin',  state: 'run' },
      { k: 'C', label: 'Demux',          state: 'run', mod: 'según BD' },
      { k: 'D', label: 'Verif. visual',  state: 'skip', mod: 'gates trusted' },
      { k: 'E', label: 'Corrección sync', state: 'skip', mod: 'Δ=0 por gates' },
      { k: 'F', label: 'Merge + inyectar', state: 'run' },
      { k: 'G', label: 'Remux MKV',      state: 'run' },
      { k: 'H', label: 'Validar',        state: 'run' },
    ],
  },
  unknown: {
    icon: '❓',
    title: 'Tipo por clasificar',
    blurb: 'La clasificación real se hará en Fase B tras descargar el bin. Si los trust gates ' +
           '(frames + L5 + CM v4.0 + has_l8) pasan → flujo automático trusted. Si no → pausa en ' +
           'Fase D para revisión visual de la sincronización antes de inyectar.',
    cls: 'warn',
    autoEndsAt: 'D',
    phases: [
      { k: 'A', label: 'Analizar BD',    state: 'run' },
      { k: 'B', label: 'Clasificar bin', state: 'run' },
      { k: 'C', label: 'Demux',          state: 'run', mod: 'probable' },
      { k: 'D', label: 'Verif. visual',  state: 'run', mod: 'si no trusted' },
      { k: 'E', label: 'Corrección sync', state: 'run', mod: 'si Δ≠0' },
      { k: 'F', label: 'Inyectar',       state: 'run' },
      { k: 'G', label: 'Remux MKV',      state: 'run' },
      { k: 'H', label: 'Validar',        state: 'run' },
    ],
  },
};

// Renderer compartido del preview del pipeline — se usa en el modal "Nuevo
// proyecto" y también en cada candidato de la consulta rápida (🔎).
// `provenance`: 'retail' | 'generated' | '' — añade aviso UX.
// `retailAlternative`: si provenance=generated, nombre del bin retail
// disponible en la misma lista (refuerza el aviso).
function _cmv40PipelinePreviewHTML(info, provenance, retailAlternative, targetType) {
  if (!info) return '';
  const cls = info.cls || 'warn';
  const flow = info.phases.map((p, i) => {
    const modHtml = p.mod ? `<span class="cmv40-ph-mod">${escHtml(p.mod)}</span>` : '';
    const arrow = (i < info.phases.length - 1)
      ? `<span class="cmv40-ph-arrow" aria-hidden="true">→</span>`
      : '';
    return `
      <div class="cmv40-ph-pill cmv40-ph-${p.state}" data-tooltip="Fase ${p.k}: ${escHtml(p.label)}${p.mod ? ' · ' + escHtml(p.mod) : ''}">
        <span class="cmv40-ph-letter">${p.k}</span>
        <span class="cmv40-ph-label">${escHtml(p.label)}</span>
        ${modHtml}
      </div>${arrow}`;
  }).join('');
  // ETA dinámico: se calcula a partir de CMV40_ETA constants (calibradas con
  // mediciones reales). Se actualiza automáticamente cuando se recalibran
  // los ratios sin tocar strings hardcoded.
  const tiempo = targetType
    ? _cmv40ComputeTargetTypeETA(targetType).tiempo
    : (info.tiempo || 'Variable');
  const provHtml = _cmv40ProvenanceNoteHTML(provenance, retailAlternative);
  return `
    <div class="cmv40-pipeline-preview ${cls}">
      <div class="cmv40-pp-header">
        <span class="cmv40-pp-icon">${info.icon}</span>
        <span class="cmv40-pp-title">${escHtml(info.title)}</span>
        <span class="cmv40-pp-time" data-tooltip="Estimación basada en tiempos medidos en NAS ZFS — se recalibra con cada ejecución real">⏱ ${escHtml(tiempo)}</span>
      </div>
      <div class="cmv40-pp-flow">${flow}</div>
      <div class="cmv40-pp-blurb">${escHtml(info.blurb)}</div>
      ${provHtml}
    </div>`;
}

// Nota de procedencia del CMv4.0. Verde para retail, ámbar para generated;
// si además hay alternativa retail para el mismo título, el aviso se
// refuerza con el nombre del bin retail disponible.
function _cmv40ProvenanceNoteHTML(prov, retailAlternative) {
  if (prov === 'retail') {
    return `
      <div class="cmv40-pp-prov cmv40-pp-prov-retail">
        <span class="cmv40-pp-prov-icon">🏛</span>
        <span class="cmv40-pp-prov-label">Retail</span>
        <span class="cmv40-pp-prov-body">RPU extraído de master streaming oficial — creative intent del colorista.</span>
      </div>`;
  }
  if (prov === 'generated') {
    const altHtml = retailAlternative
      ? `<div class="cmv40-pp-prov-alt">
           <strong>Alternativa retail disponible en este repo:</strong><br>
           <code>${escHtml(retailAlternative)}</code><br>
           <em>Cámbiala en el desplegable de arriba para usar CMv4.0 auténtico.</em>
         </div>`
      : '';
    return `
      <div class="cmv40-pp-prov cmv40-pp-prov-gen">
        <span class="cmv40-pp-prov-icon">⚠️</span>
        <span class="cmv40-pp-prov-label">Generated</span>
        <span class="cmv40-pp-prov-body">CMv4.0 <strong>sintético</strong> desde HDR10 (algorítmico). La calidad depende del tuning (T1/T3…). Si existe un bin <code>(cmv4.0 restored/added)</code> o <code>(P5 to P8)</code> para este título, es preferible.</span>
        ${altHtml}
      </div>`;
  }
  return '';
}

function _cmv40NewUpdatePipelinePreview() {
  const container = document.getElementById('cmv40-new-pipeline-preview');
  if (!container) return;

  // Sin selección de target → vacío para los 3 tabs.
  if (!_cmv40NewTargetSelected) {
    container.innerHTML = '';
    container.style.display = 'none';
    _cmv40NewUpdateAutoLabel(null);
    return;
  }

  const tab = _cmv40NewTargetTab;

  // Tabs 'path' y 'mkv': sin sheet de recomendación no podemos predecir el
  // provenance/predicted_type antes de descargar/extraer el bin. Mostramos
  // un placeholder informativo para que el usuario sepa que la validación
  // completa pasa por el pre-flight (idéntica a la del tab 'repo').
  if (tab === 'path' || tab === 'mkv') {
    const sourceLabel = tab === 'path' ? 'el bin local' : 'el MKV target';
    container.style.display = 'block';
    container.innerHTML = `
      <div class="cmv40-pp-card" style="background:var(--blue-dim); border:1px solid var(--blue-border); border-radius:8px; padding:10px 12px">
        <div style="font-size:12px; color:var(--text-1); line-height:1.5">
          <strong style="color:var(--blue)">ℹ El tipo del bin se detectará en el pre-flight.</strong>
          Sin el sheet de recomendación de DoviTools no podemos predecir
          la calidad de ${sourceLabel} antes de procesarlo. El pre-flight (5-30s
          para .bin local, 30-90s para extraer de MKV) clasifica el L8
          (real / sintético / ambiguo), calcula el tier de calidad
          (CMv4 CORE/CORE+/FULL) y decide la recomendación
          Mantener vs Inyectar — igual que con un bin del repo.
          Verás el veredicto en la card "🎯 Análisis y recomendación"
          del proyecto.
        </div>
      </div>`;
    // Label del auto-pipeline neutro: no sabemos si será trusted/generic
    // hasta que el pre-flight clasifique. El usuario verá el detalle real
    // tras crear el proyecto.
    _cmv40NewUpdateAutoLabel(null);
    return;
  }

  // Tab 'repo': preview clásico con datos del sheet.
  if (tab !== 'repo' || _cmv40NewTargetSelected.kind !== 'repo') {
    container.innerHTML = '';
    container.style.display = 'none';
    _cmv40NewUpdateAutoLabel(null);
    return;
  }
  const pt = _cmv40NewTargetSelected.predicted_type || 'unknown';
  const prov = _cmv40NewTargetSelected.provenance || '';
  const info = _CMV40_PIPELINE_PREVIEW[pt] || _CMV40_PIPELINE_PREVIEW.unknown;

  // Si el usuario eligió un Generated, comprobamos si en la lista cargada
  // hay al menos una opción Retail (CMv4.0 auténtico). En ese caso, el warning
  // se refuerza — hay alternativa preferible accesible en la misma vista.
  let retailAlternative = '';
  if (prov === 'generated' && Array.isArray(_cmv40NewRepoCands)) {
    const alt = _cmv40NewRepoCands.find(c => c.provenance === 'retail');
    if (alt) retailAlternative = alt.file?.name || '(retail disponible)';
  }

  container.style.display = 'block';
  container.innerHTML = _cmv40PipelinePreviewHTML(info, prov, retailAlternative, pt);
  _cmv40NewUpdateAutoLabel(info);
}

// Actualiza el texto del toggle "Auto-pipeline" según el preview activo.
// - Trusted: corre todo A→H automáticamente.
// - Unknown/generic: se detiene en D si los gates no pasan.
function _cmv40NewUpdateAutoLabel(info) {
  const span = document.querySelector('.cmv40-new-auto-toggle span');
  const wrap = document.querySelector('.cmv40-new-auto-toggle');
  if (!span) return;
  if (!info) {
    span.textContent = '🤖 Auto-pipeline';
    if (wrap) wrap.setAttribute('data-tooltip',
      'Encadena las fases disponibles sin interacción manual.');
    return;
  }
  const runPhases = info.phases.filter(p => p.state === 'run').map(p => p.k);
  const endsAt = info.autoEndsAt;
  if (endsAt) {
    span.textContent = `🤖 Auto-pipeline hasta Fase ${endsAt} (pausa si no trusted)`;
    if (wrap) wrap.setAttribute('data-tooltip',
      `Corre hasta la Fase ${endsAt}. Si los gates no pasan en B, espera revisión manual.`);
  } else {
    span.textContent = `🤖 Auto-pipeline completo (${runPhases.join('→')})`;
    if (wrap) wrap.setAttribute('data-tooltip',
      `Ejecuta ${runPhases.length} fases automáticamente. Estimado: ${info.tiempo}.`);
  }
}

// Cache de los candidatos cargados (para que _cmv40NewSelectRepoCandidate
// pueda recuperar el objeto completo por file_id al hacer click en una card).
let _cmv40NewRepoCands = [];

function _cmv40NewResetRepoList(placeholder, isError = false) {
  const list = document.getElementById('cmv40-new-repo-list');
  if (!list) return;
  _cmv40NewRepoCands = [];
  list.innerHTML = `<div class="cmv40-repo-empty ${isError ? 'error' : ''}">${placeholder}</div>`;
  // Al resetear también limpia la selección del target
  if (_cmv40NewTargetSelected?.kind === 'repo') {
    _cmv40NewTargetSelected = null;
    _cmv40NewUpdateCreateBtn();
    _cmv40NewUpdatePipelinePreview();
  }
}

// Token anti-race: si el usuario cambia de tab o de source durante el await,
// la respuesta vieja NO debe auto-seleccionar un repo candidate (lo que
// sobreescribe el target que el usuario haya elegido mientras tanto).
let _cmv40RepoReqId = 0;

async function _cmv40NewLoadRepoCandidates(forceRefresh = false) {
  const list = document.getElementById('cmv40-new-repo-list');
  const info = document.getElementById('cmv40-new-repo-info');
  if (!list) return;
  if (!_cmv40SourceSelected) {
    _cmv40NewResetRepoList('— Selecciona primero el MKV origen —');
    if (info) info.textContent = 'Selecciona primero un MKV origen.';
    return;
  }
  list.innerHTML = '<div class="cmv40-repo-empty">⏳ Buscando en Drive…</div>';
  if (info) info.innerHTML = '<span class="cmv40-rec-spinner-inline"></span> Consultando repositorio de DoviTools…';
  // El sheet de DoviTools matchea por NOMBRE de fichero (no path), asi que
  // pasamos el filename, no la ruta absoluta.
  const matchKey = _cmv40SourceFilename || _cmv40SourceSelected;
  const qs = '?filename=' + encodeURIComponent(matchKey);
  const myReqId = ++_cmv40RepoReqId;
  const mySource = _cmv40SourceSelected;
  const data = await apiFetch('/api/cmv40/repo-rpus' + qs);
  // Stale response: el usuario ya lanzó otra carga, cambió de tab o de
  // source — descartamos silenciosamente para no sobreescribir su selección.
  if (myReqId !== _cmv40RepoReqId || mySource !== _cmv40SourceSelected
      || _cmv40NewTargetTab !== 'repo') {
    return;
  }
  if (!data) {
    _cmv40NewResetRepoList('Error consultando el repositorio', true);
    return;
  }
  if (!data.drive_configured) {
    _cmv40NewRepoCands = [];
    list.innerHTML = `<div class="cmv40-repo-banner-wrap">${_cmv40RepoUnavailableBanner(data)}</div>`;
    if (info) {
      info.textContent = !data.drive_folder_configured
        ? 'Repo bloqueado — configura la URL.'
        : 'Google API key no configurada.';
    }
    return;
  }
  if (data.error) {
    _cmv40NewResetRepoList(data.error, true);
    if (info) info.textContent = data.error;
    return;
  }
  const cands = data.candidates || [];
  if (!cands.length) {
    const t = data.title_en || data.title_es || '?';
    _cmv40NewResetRepoList(`Sin coincidencias para "${escHtml(t)}"`);
    if (info) {
      info.innerHTML = `No hay <code>.bin</code> para <strong>${escHtml(t)}</strong> en el repositorio. Prueba otra pestaña.`;
    }
    return;
  }

  // Lista plana ordenada por score (el backend ya aplicó +0.03 a retail,
  // así que el orden viene correcto). Quitamos la agrupación visual
  // porque confunde: un P5→P8 source (provenance='') puede ser mejor
  // que un Generated FEL aunque "Sin marca" suena peor que "Generated".
  _cmv40NewRepoCands = cands;
  const topFilename = cands[0]?.file?.name || '';

  const renderCard = (c) => {
    const sizeMb = (c.file.size_bytes / 1024 / 1024).toFixed(1);
    const pt = c.predicted_type || 'unknown';
    const prov = c.provenance || '';
    const tagMeta = pt === 'trusted_p7_fel_final' ? { icon: '🎯', label: 'bin P7 FEL',  cls: 'tag-ok' }
                  : pt === 'trusted_p7_mel_final' ? { icon: '🎯', label: 'bin P7 MEL',  cls: 'tag-ok' }
                  : pt === 'trusted_p8_source'    ? { icon: '📦', label: 'bin P8 retail', cls: 'tag-info' }
                  : { icon: '❓', label: 'tipo desconocido', cls: 'tag-warn' };
    const provTag = prov === 'retail'
      ? '<span class="cmv40-repo-card-tag tag-ok">🏛 Retail</span>'
      : prov === 'generated'
      ? '<span class="cmv40-repo-card-tag tag-warn">⚠ Generated</span>'
      : '';
    const isBest = c.file.name === topFilename;
    return `
      <div class="cmv40-repo-card" data-file-id="${escHtml(c.file.id)}"
           role="button" tabindex="0"
           onclick="_cmv40NewSelectRepoCandidate('${escHtml(c.file.id)}')"
           onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();_cmv40NewSelectRepoCandidate('${escHtml(c.file.id)}')}">
        <div class="cmv40-repo-card-head">
          <span class="cmv40-repo-card-tag ${tagMeta.cls}">${tagMeta.icon} ${tagMeta.label}</span>
          ${provTag}
          ${isBest ? '<span class="cmv40-repo-card-best">🏆 mejor match</span>' : ''}
          <span class="cmv40-repo-card-score">${Math.round(c.score * 100)}%</span>
          <span class="cmv40-repo-card-size">${sizeMb} MB</span>
        </div>
        <div class="cmv40-repo-card-path">${escHtml(c.file.path)}</div>
      </div>`;
  };

  list.innerHTML = cands.map(renderCard).join('');

  // Auto-seleccionar el top-score global
  if (topFilename) {
    const top = cands.find(c => c.file.name === topFilename);
    if (top) _cmv40NewSelectRepoCandidate(top.file.id);
  }

  if (info) {
    info.innerHTML = `<strong>${cands.length}</strong> candidato${cands.length !== 1 ? 's' : ''} · top score: <strong>${Math.round(cands[0].score * 100)}%</strong>. Haz click para seleccionar. Se descargará al crear el proyecto.`;
  }
}

// Marca visualmente una card como seleccionada y actualiza el estado global.
function _cmv40NewSelectRepoCandidate(fileId) {
  // Guard anti-race: si el usuario cambió de tab, no pisamos su target.
  // (La carga async de repo candidates podía completar tras un cambio de
  //  tab y reemplazar _cmv40NewTargetSelected con el top-candidate.)
  if (_cmv40NewTargetTab !== 'repo') return;
  const list = document.getElementById('cmv40-new-repo-list');
  if (!list) return;
  const card = list.querySelector(`.cmv40-repo-card[data-file-id="${fileId}"]`);
  if (!card) return;
  // Quita selected de todas las cards, marca la actual
  list.querySelectorAll('.cmv40-repo-card.selected').forEach(el => el.classList.remove('selected'));
  card.classList.add('selected');
  // Scroll dentro del contenedor para que la card elegida sea visible
  try {
    card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  } catch (e) { /* ignore */ }

  const cand = _cmv40NewRepoCands.find(c => c.file.id === fileId);
  if (!cand) return;
  _cmv40NewTargetSelected = {
    kind: 'repo',
    value: { file_id: cand.file.id, file_name: cand.file.name },
    predicted_type: cand.predicted_type || 'unknown',
    provenance: cand.provenance || '',
  };
  _cmv40NewUpdateCreateBtn();
  _cmv40NewUpdatePipelinePreview();
}

function _cmv40EscHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s || '');
  return d.innerHTML;
}

function _cmv40NewUpdateCreateBtn() {
  const btn = document.getElementById('cmv40-create-btn');
  if (!btn) return;
  btn.disabled = !_cmv40SourceSelected || !_cmv40NewTargetSelected;
}

async function createCMv40Project() {
  if (!_cmv40SourceSelected || !_cmv40NewTargetSelected) return;
  const autoOn = !!document.getElementById('cmv40-new-auto')?.checked;
  const target = _cmv40NewTargetSelected;

  // _cmv40SourceSelected es ya la ruta absoluta tras el browser (puede venir
  // de /mnt/library/Movies/...). Si por compat fuera solo un filename (caso
  // legacy si alguien lo seteara directo), prepend /mnt/output como antes.
  const sourcePath = _cmv40SourceSelected.startsWith('/')
    ? _cmv40SourceSelected
    : '/mnt/output/' + _cmv40SourceSelected;

  // Construir pending_target para que el backend lo persista. Crítico para
  // que el orquestador pueda disparar preflight + Fase B aunque el cliente
  // desaparezca tras Fase A (Mac sleep, pestaña cerrada, etc).
  const pendingTargetPayload = { kind: target.kind };
  if (target.kind === 'repo') {
    pendingTargetPayload.file_id = target.value?.file_id || '';
    pendingTargetPayload.file_name = target.value?.file_name || '';
  } else if (target.kind === 'path') {
    pendingTargetPayload.rpu_path = target.value || '';
  } else if (target.kind === 'mkv') {
    pendingTargetPayload.source_mkv_path = target.value || '';
  }

  const data = await apiFetch('/api/cmv40/create', {
    method: 'POST',
    body: JSON.stringify({
      source_mkv_path: sourcePath,
      // CRÍTICO: auto_pipeline le dice al backend que encadene fases
      // automáticamente sin esperar al frontend. Hace el job resiliente
      // a Mac sleep, pestaña cerrada, navegador crashado, etc.
      auto_pipeline: autoOn,
      pending_target: pendingTargetPayload,
    }),
  });

  closeModal('cmv40-new-modal');
  if (!data) {
    showToast('Error al crear el proyecto', 'error');
    return;
  }

  // Abrir el proyecto y preconfigurar auto + target pendiente
  const project = openCMv40Project(data);
  if (project) {
    project.autoContinue = autoOn;
    project.pendingTarget = target;  // se aplicará cuando A termine
    _updateCMv40Panel(project);
  }
  await refreshCMv40Sidebar();

  // Disparar preflight INMEDIATAMENTE si hay target seleccionado, sin importar
  // si auto mode esta on. Sin auto, esto evita que el usuario gaste 12 min de
  // Fase A si el bin target no aporta CMv4.0 — el preflight tarda <5s y aborta
  // con mensaje claro. Con auto, _cmv40MaybeAutoAdvance se encarga del flujo
  // completo (preflight → Fase A → ...).
  if (target && project) {
    if (autoOn) {
      project._autoChaining = true;
      _cmv40MaybeAutoAdvance(project);
    } else {
      // Auto OFF: solo el preflight, no encadena Fase A. El usuario lanza
      // Fase A manualmente cuando vea preflight OK.
      _cmv40FirePreflight(project.id, target);
    }
  }
}

/**
 * Dispara el pre-flight del bin target en background. Backend responde
 * inmediatamente con {started:true} y setea running_phase="preflight". El
 * polling se encarga del resto: si OK → target_preflight_ok=True y el
 * próximo tick dispara Fase A. Si KO → error_message se setea y el
 * pipeline se detiene (el motivo queda en el log de la sesión via WS,
 * sin toast).
 */
async function _cmv40FirePreflight(pid, target) {
  const body = { kind: target.kind === 'repo' ? 'drive' : target.kind };
  if (target.kind === 'repo') {
    body.file_id = target.value.file_id;
    body.file_name = target.value.file_name || '';
  } else if (target.kind === 'path') {
    body.rpu_path = target.value;
  } else if (target.kind === 'mkv') {
    body.source_mkv_path = target.value;
  }
  await apiFetch(`/api/cmv40/${pid}/preflight-target`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ── Proyecto CMv4.0 ──────────────────────────────────────────────

// Asigna una sesión nueva al proyecto preservando campos que el backend
// puede no haber hidratado aún (típicamente `tmdb_info`). Evita que la
// ficha TMDb desaparezca/flickee cuando hay saves concurrentes (p.ej.
// durante una cancelación de fase que clobberea campos async).
// Silencio del WS a partir del cual la barra se reconstruye desde el estado
// persistido (session.last_progress) en vez de esperar al siguiente mensaje.
const CMV40_WS_SILENCE_FOR_REST_PROGRESS_MS = 8000;

function _cmv40AssignSession(project, data) {
  if (!project || !data) return;
  // Respuesta sin log (GET ?include_log=false del safety poller): el backend
  // no reenvía output_log porque el WS ya lo está entregando en vivo.
  // Restauramos la copia local para que el resto del flujo (watermark,
  // _cmv40SyncPermanentLog) siga viendo el array completo de siempre.
  if (data.output_log_omitted) {
    data.output_log = (project.session && project.session.output_log) || [];
  }
  // Barra de progreso desde el estado persistido. Los pasos silenciosos
  // (extract-rpu, export, demux) tardan minutos sin escribir una sola línea:
  // si el WS se cae, la barra era la única señal y se quedaba congelada.
  // Solo entra en juego cuando el WS lleva rato callado — mientras entrega,
  // él manda porque va más al día que el JSON persistido.
  if (data.last_progress && data.running_phase) {
    const wsSilentMs = Date.now() - (project._lastWsMessageAt || 0);
    if (wsSilentMs > CMV40_WS_SILENCE_FOR_REST_PROGRESS_MS) {
      _cmv40UpdateProgressUI(project.id, data.last_progress);
    }
  }
  const preserved = {};
  const PRESERVE_FIELDS = ['tmdb_info'];
  for (const f of PRESERVE_FIELDS) {
    if (project.session && project.session[f] && !data[f]) {
      preserved[f] = project.session[f];
    }
  }
  // running_phase: preservar el optimistic local si hay race condition
  // con el GET. Caso concreto del bug: cuando "✓ Fase X completada" llega
  // por WS, el frontend dispara GET /api/cmv40/{id}. Bajo carga I/O del
  // NAS ese GET puede tardar 30s en responder. Mientras tanto:
  //   T=0:    backend save running_phase=null (fin de fase X)
  //   T=0.01: backend dispatch a siguiente fase Y → save running_phase=Y
  //   T=0.02: WS broadcasta "━━━ Inicio fase: Y ━━━" → optimistic local Y
  //   T=30:   GET de T=0 responde — pero load_cmv40_session leyó el JSON
  //           en la ventana de ~10ms donde running_phase=null y devuelve
  //           ese snapshot obsoleto. Sin este guard, _cmv40AssignSession
  //           pisaba el optimistic Y con un null antiguo → spinner del
  //           timeline desaparecía de la fase nueva hasta el SIGUIENTE
  //           GET (el del "━━━ Inicio fase: Y" propio).
  // Si el local tiene running_phase reciente y data lo trae null pero la
  // sesión no es terminal, conservamos el local.
  if (project.session && project.session.running_phase
      && !data.running_phase
      && data.phase !== 'done'
      && !data.archived
      && !data.error_message) {
    const optimisticAt = project._optimisticRunningPhaseAt || 0;
    if (Date.now() - optimisticAt < 60000) {
      preserved.running_phase = project.session.running_phase;
    }
  }
  project.session = Object.assign({}, data, preserved);
  _cmv40RehydratePendingTarget(project);
}

// Reconstruye `project.pendingTarget` desde `session.pending_target_*` (que
// el backend persiste al crear el proyecto). Crítico para que el frontend
// no se confunda tras un reload (Mac sleep, pestaña cerrada): sin esto, el
// case 'created' de _cmv40MaybeAutoAdvance saltaba el preflight y disparaba
// Fase A directo, y el case 'source_analyzed' dejaba el flujo "pausado"
// cuando el backend ya estaba corriendo Fase B.
//
// Solo hidratamos cuando phase ∈ {created, source_analyzed}: a partir de
// target_provided el target ya está consumido en backend (target_rpu_source).
function _cmv40RehydratePendingTarget(project) {
  const s = project?.session;
  if (!s) return;
  const phase = s.phase;
  if (phase !== 'created' && phase !== 'source_analyzed') {
    project.pendingTarget = null;
    return;
  }
  const kind = s.pending_target_kind;
  if (!kind) return;  // nunca hubo target preseleccionado
  // Idempotente: si ya está hidratado al mismo kind, preservar metadata
  // adicional que el frontend pueda haber añadido (predicted_type, etc).
  if (project.pendingTarget?.kind === kind) return;
  let value = null;
  if (kind === 'path') {
    value = s.pending_target_rpu_path || '';
  } else if (kind === 'mkv') {
    value = s.pending_target_source_mkv_path || '';
  } else if (kind === 'drive' || kind === 'repo') {
    value = {
      file_id: s.pending_target_file_id || '',
      file_name: s.pending_target_file_name || '',
    };
  }
  if (!value) return;
  project.pendingTarget = { kind, value };
}

function openCMv40Project(session) {
  // Si ya está abierto, activar su subtab
  const existing = openCMv40Projects.find(p => p.id === session.id);
  if (existing) {
    switchCMv40SubTab(existing.subTabId);
    return existing;
  }
  if (openCMv40Projects.length >= MAX_CMV40_PROJECTS) {
    showToast(`Máximo ${MAX_CMV40_PROJECTS} proyectos abiertos`, 'warning');
    return null;
  }

  const pid = session.id;
  // resumeAuto: refleja el estado persistente `session.auto_pipeline` del
  // backend. Esta es ahora la FUENTE DE VERDAD del modo auto. El backend
  // encadena fases automáticamente sin depender del frontend (resiliente
  // a Mac sleep / pestaña cerrada / navegador crashado). Frontend solo
  // necesita reflejar el flag para mostrar la UI correcta.
  // Fallback heurístico para sesiones legacy (creadas antes del campo
  // auto_pipeline): si está en mid-pipeline con target trusted o con
  // running_phase, asumimos que estaba en auto.
  const isMidPipeline = session.phase
    && session.phase !== 'done'
    && session.phase !== 'created'
    && !session.error_message
    && !session.archived;
  const wasInAutoFlowLegacy = !!session.running_phase
    || (session.target_trust_ok === true);
  const resumeAuto = (session.auto_pipeline === true)
    || (isMidPipeline && wasInAutoFlowLegacy);
  const project = {
    id: pid,
    subTabId: pid,
    session: session,
    ws: null,
    syncData: null,
    autoContinue: resumeAuto,  // off por defecto; createCMv40Project lo activa explícitamente
    pendingTarget: null,       // { kind: 'path'|'mkv'|'repo', value }
  };
  // Hidrata pendingTarget desde session.pending_target_* si aplica. Necesario
  // para reanudar el auto-pipeline tras reload del cliente sin disparar Fase
  // A sin preflight ni quedarse "pausado" en source_analyzed.
  _cmv40RehydratePendingTarget(project);
  openCMv40Projects.push(project);
  _createCMv40SubTab(project);
  _createCMv40Panel(project);
  switchCMv40SubTab(pid);
  _connectCMv40WebSocket(project);
  // Polling REST de seguridad: independiente del WS, refresca la sesión
  // cada 4s mientras haya running_phase. Garantiza que el log avanza
  // aunque el WS quede zombie tras Mac sleep (caso real visto: tras
  // cerrar tapa del Mac >1min y reabrir, el WS reportaba OPEN pero los
  // datos ya no fluían — el polling los trae via REST y la hidratación
  // con watermark añade las líneas nuevas al DOM sin duplicar).
  _cmv40StartSafetyPoller(project);
  // Validar artefactos en disco — detecta ficheros borrados manualmente
  // y retrocede la fase automáticamente si hace falta.
  _cmv40VerifyArtifacts(project);
  // Si reanudamos auto-pipeline y la sesión NO tiene fase corriendo (modo
  // "puente" tras una fase atascada), disparar _cmv40MaybeAutoAdvance
  // INMEDIATAMENTE en lugar de esperar al primer tick del safety poller
  // (4s). Caso real: el usuario reabre un proyecto que se quedó atascado
  // en phase='extracted' tras perder foco — queremos arrancar la
  // transición a sync_verified en cuanto el panel termine de pintar.
  if (resumeAuto && !session.running_phase) {
    setTimeout(() => {
      if (!project._closed) _cmv40MaybeAutoAdvance(project);
    }, 100);
  }
  return project;
}

/**
 * Polling REST de seguridad cada 4s mientras haya running_phase. Llama
 * _refreshCMv40Session, que actualiza session + panel + log permanente
 * (via watermark, sin duplicados con líneas que llegaron por WS).
 *
 * Watchdog del WS: si el último mensaje WS llegó hace más de 30s pero
 * sigue habiendo running_phase, asumimos zombie y forzamos reconexión.
 * Sin esto, un usuario con la tapa cerrada >5min y la pestaña visible
 * podría tardar 60-120s en ver actualizaciones (timeout del TCP).
 *
 * Se autoapaga cuando running_phase=null o cuando el proyecto se cierra.
 */
function _cmv40StartSafetyPoller(project) {
  if (project._safetyPoller) clearInterval(project._safetyPoller);
  project._lastWsMessageAt = Date.now();
  project._safetyPoller = setInterval(() => {
    if (!project || project._closed) {
      clearInterval(project._safetyPoller);
      project._safetyPoller = null;
      return;
    }
    const s = project.session || {};
    // Condición de salida: el job terminó completamente o entró en error.
    // Antes salíamos cuando running_phase=null, pero eso paraba el poller
    // durante el "puente" entre fases del auto-pipeline (running_phase=null
    // mientras el frontend dispara la siguiente fase). Si el dispatch falla
    // en background tab (Chrome throttle de setTimeout afecta el polling
    // interno), nadie reintenta y la cadena se cuelga.
    // Ahora seguimos activos mientras autoContinue=true y phase no es
    // terminal, para vigilar el modo puente.
    const isTerminal = (s.phase === 'done' || s.archived || !!s.error_message);
    const isActive = !!s.running_phase || project.autoContinue;
    if (isTerminal || !isActive) {
      clearInterval(project._safetyPoller);
      project._safetyPoller = null;
      return;
    }
    // Refresh REST: actualiza session.output_log (entre otros). Internamente
    // dispara _cmv40MaybeAutoAdvance si autoContinue=true y phase no
    // running — con el retry de 5s del flag, esto destraba cadenas
    // atascadas en modo puente.
    // No solapar refreshes: GET /api/cmv40/{id} trae el output_log completo y
    // bajo carga del NAS puede tardar >4s (el intervalo del poller). Sin este
    // guard se acumulaban GETs pesados en vuelo (audit #10).
    if (!document.hidden && !project._safetyRefreshInFlight) {
      project._safetyRefreshInFlight = true;
      // El log solo se pide cuando el WS NO lo está entregando. Con el WS
      // sano este tick solo necesita el estado, y pedir el log completo
      // costaba 1,57 MB / 437 ms de servidor cada 4 s (medido en un job
      // real). Si el WS lleva >10s callado, volvemos a pedirlo entero para
      // recuperar lo que se haya perdido.
      const wsSilentMs = Date.now() - (project._lastWsMessageAt || 0);
      const wsAlive = project.ws && project.ws.readyState === WebSocket.OPEN
                      && wsSilentMs < 10000;
      Promise.resolve(_refreshCMv40Session(project.id, { includeLog: !wsAlive }))
        .finally(() => { project._safetyRefreshInFlight = false; });
    }
    // Watchdog: detectar zombie WS por silencio prolongado.
    const silentMs = Date.now() - (project._lastWsMessageAt || 0);
    const ws = project.ws;
    const looksOpen = ws && ws.readyState === WebSocket.OPEN;
    if (silentMs > 30000 && looksOpen && !document.hidden && s.running_phase) {
      // Más de 30s sin mensaje pero el WS dice OPEN → zombie probable.
      // Solo aplica si hay running_phase (sino no hay líneas que esperar).
      try { project.ws?.close(); } catch (_) {}
      setTimeout(() => {
        if (!project._closed) _connectCMv40WebSocket(project);
      }, 50);
    }
  }, 4000);
}

async function _cmv40VerifyArtifacts(project) {
  // No validar proyectos recién creados (sin artefactos aún esperados)
  if (project.session.phase === 'created') return;
  const data = await apiFetch(`/api/cmv40/${project.id}/verify-artifacts`, { method: 'POST' });
  if (!data) return;
  if (data.changed) {
    project.session = data.session;
    _updateCMv40Panel(project);
    refreshCMv40Sidebar();
    if (data.all_missing) {
      showToast(`⛔ ${data.message}`, 'error');
      // Con todo borrado, auto-avance queda neutralizado (comprueba error_message)
    } else {
      showToast(`⚠ ${data.message}`, 'warning');
    }
  }
}

function closeCMv40Project(pid) {
  const idx = openCMv40Projects.findIndex(p => p.id === pid);
  if (idx === -1) return;
  const project = openCMv40Projects[idx];
  // Marca para que onclose del WS NO intente reconectar.
  project._closed = true;
  if (project._wsReconnectTimer) {
    clearTimeout(project._wsReconnectTimer);
    project._wsReconnectTimer = null;
  }
  if (project._safetyPoller) {
    clearInterval(project._safetyPoller);
    project._safetyPoller = null;
  }
  try { project.ws?.close(); } catch (_) {}
  document.getElementById(`cmv40-stab-${pid}`)?.remove();
  document.getElementById(`cmv40-panel-${pid}`)?.remove();
  openCMv40Projects.splice(idx, 1);
  _updateSubtabScrollState();

  if (activeCMv40SubTabId === pid) {
    if (openCMv40Projects.length > 0) {
      switchCMv40SubTab(openCMv40Projects[openCMv40Projects.length - 1].subTabId);
    } else {
      activeCMv40SubTabId = null;
      document.getElementById('cmv40-empty-state').style.display = '';
    }
  }
  // Refrescar sidebar para actualizar el badge "abierto"
  _renderCMv40Sidebar();
}

function switchCMv40SubTab(pid) {
  activeCMv40SubTabId = pid;
  document.querySelectorAll('#cmv40-subtab-content > .cmv40-panel').forEach(el => {
    el.style.display = 'none';
  });
  const active = document.getElementById(`cmv40-panel-${pid}`);
  if (active) active.style.display = 'block';
  const empty = document.getElementById('cmv40-empty-state');
  if (empty) empty.style.display = openCMv40Projects.find(p => p.id === pid) ? 'none' : '';
  document.querySelectorAll('#cmv40-subtab-projects .subtab-proj').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.pid === pid);
  });
}

function _createCMv40SubTab(project) {
  const container = document.getElementById('cmv40-subtab-projects');
  const btn = document.createElement('button');
  btn.className = 'subtab-proj active';
  btn.id = `cmv40-stab-${project.id}`;
  btn.dataset.pid = project.id;
  const name = project.session.source_mkv_name.replace(/\.mkv$/i, '');
  btn.innerHTML = `
    <span class="subtab-proj-icon">🎨</span>
    <span class="subtab-proj-name" data-tooltip="${escHtml(project.session.source_mkv_name)}">${escHtml(name.slice(0, 24))}${name.length > 24 ? '…' : ''}</span>
    <button class="subtab-proj-close" onclick="closeCMv40Project('${project.id}');event.stopPropagation()"
      data-tooltip="Cerrar proyecto">×</button>`;
  btn.onclick = (e) => { if (!e.target.closest('.subtab-proj-close')) switchCMv40SubTab(project.id); };
  container.appendChild(btn);
  _updateSubtabScrollState();
}

function _connectCMv40WebSocket(project) {
  try { project.ws?.close(); } catch (_) {}
  // Limpia timer de reconnect previo si lo hubiera (defensivo).
  if (project._wsReconnectTimer) {
    clearTimeout(project._wsReconnectTimer);
    project._wsReconnectTimer = null;
  }
  const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${wsProto}//${location.host}/ws/cmv40/${project.id}`);
  // Refresh REST inmediato al conectar el WS — sin esperar a que el backend
  // emita la primera línea (que puede tardar segundos si la fase actual está
  // en un paso silencioso de ffmpeg/dovi_tool). Garantiza que tras un wake
  // del Mac, el log catchea hasta el momento actual en cuanto el WS se abre.
  // También marca _lastWsMessageAt para que el watchdog cuente desde aquí.
  ws.onopen = () => {
    project._lastWsMessageAt = Date.now();
    _refreshCMv40Session(project.id);
  };
  ws.onmessage = (ev) => {
    _appendCMv40Log(project, ev.data);
    // Marca timestamp del último mensaje recibido — el watchdog usa esto
    // para detectar conexiones zombie (WS reporta OPEN pero no llega data).
    project._lastWsMessageAt = Date.now();
    // ── Optimistic update del timeline ────────────────────────────────
    // El GET /api/cmv40/{id} bajo carga I/O del NAS (extract-rpu en
    // paralelo) puede tardar 30-60s en responder. Sin esto, el spinner
    // del timeline lateral tardaba minutos en aparecer en la fase nueva
    // (visto en Fase H tras un remux pesado). Aquí extraemos el nombre
    // de la fase desde el marcador del log y actualizamos
    // project.session.running_phase de inmediato — el timeline pinta el
    // spinner correcto al instante. El GET posterior trae datos
    // autoritativos y rectifica si hay discrepancia.
    if (project.session) {
      const startMatch = ev.data.match(/━━━ Inicio fase:\s*([a-z_]+)\s*━━━/i);
      if (startMatch) {
        project.session.running_phase = startMatch[1];
        // Timestamp para que _cmv40AssignSession sepa que este valor es
        // reciente y NO debe pisarlo si un GET tardío trae null obsoleto.
        project._optimisticRunningPhaseAt = Date.now();
        _updateCMv40Panel(project);
      } else if (/✓ Fase \w+ completada en/.test(ev.data) ||
                 /✗ Fase \w+ FALLÓ/.test(ev.data)) {
        // Fase terminó: limpia running_phase localmente para que el
        // spinner desaparezca de la fase anterior mientras llega el GET
        // que dirá la fase nueva (si la hay) o el done definitivo.
        project.session.running_phase = null;
        project._optimisticRunningPhaseAt = 0;
        _updateCMv40Panel(project);
      }
    }
    // Refrescar sesión vía REST para tener phase/phase_history/etc al día
    if (ev.data.includes('━━━') || ev.data.includes('✓') || ev.data.includes('✗')) {
      _refreshCMv40Session(project.id);
    }
  };
  ws.onerror = () => {};
  // Reconnect automatico con backoff cuando el WS se cierra (Mac sleep,
  // pestaña en background con sleep agresivo, perdida temporal de red...).
  // SIN esto, tras el wake del Mac el log se queda congelado y la UI no se
  // actualiza aunque el job haya terminado en backend.
  ws.onclose = () => {
    if (project._closed) return;
    if (project._wsReconnectTimer) clearTimeout(project._wsReconnectTimer);
    project._wsReconnectTimer = setTimeout(() => {
      project._wsReconnectTimer = null;
      // Refrescar sesion ANTES de reconectar — si el job ya termino en
      // backend mientras dormiamos, esto pone la UI al dia inmediatamente.
      _refreshCMv40Session(project.id);
      // Solo reconectar si el proyecto sigue abierto y la sesion esta
      // viva (running_phase != null o estado no terminal). Si el job
      // termino, no hace falta WS — el refresh ya pinto el estado final.
      const stillOpen = openCMv40Projects.find(p => p.id === project.id);
      if (stillOpen && !stillOpen._closed) {
        const s = stillOpen.session || {};
        if (s.running_phase) {
          _connectCMv40WebSocket(stillOpen);
        }
      }
    }, 2000);
  };
  project.ws = ws;
}

// Copia al portapapeles el texto plano de un elemento que contiene líneas de
// log (div.log-line). Muestra un toast de confirmación; fallback a
// document.execCommand para contextos inseguros (file://, http en IP).
/** Copia texto al portapapeles con fallback a execCommand para HTTP (no
 *  secure context). Devuelve true/false; el caller muestra toasts. */
async function _copyTextToClipboardWithFallback(text) {
  if (!text) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* cae al fallback */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch { return false; }
}

async function copyLogToClipboard(containerId, btn) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const text = Array.from(el.querySelectorAll('.log-line, div'))
    .map(d => d.textContent || '')
    .filter(Boolean)
    .join('\n') || (el.textContent || '');
  if (!text.trim()) {
    showToast('No hay log que copiar', 'info');
    return;
  }
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    }
  } catch { ok = false; }
  if (ok) {
    showToast(`Log copiado (${text.length.toLocaleString()} caracteres)`, 'success');
    // Feedback visual breve en el botón si se pasó
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = '✓ Copiado';
      btn.disabled = true;
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1200);
    }
  } else {
    showToast('No se pudo copiar al portapapeles', 'error');
  }
}

// Auto-scroll "sticky": solo scrolla al fondo si el usuario YA estaba ahí.
// Tolerancia de 30px para no perder el pegado cuando llegan líneas rápidas.
// Si el usuario hace scroll arriba para leer, se respeta — el foco no vuelve
// al final en cada nueva línea.
function _isScrolledNearBottom(el, tolerance = 30) {
  return (el.scrollHeight - el.scrollTop - el.clientHeight) <= tolerance;
}

function _appendLogLine(containerEl, line) {
  if (!containerEl) return;
  const wasAtBottom = _isScrolledNearBottom(containerEl);
  const div = document.createElement('div');
  div.className = 'log-line ' + _classifyLogLine(line);
  div.textContent = line;
  containerEl.appendChild(div);
  if (wasAtBottom) containerEl.scrollTop = containerEl.scrollHeight;
}

/** Clasifica una linea de log por patrones textuales para aplicar color.
 *  Paleta rica (user-friendly) — todas las clases se definen en style.css
 *  con buena legibilidad sobre fondo oscuro del log-viewer.
 *
 *  Principio: distinguir claramente 2 tipos de linea:
 *    · Feedback de la APP (semantico, colorido): marcadores como
 *      [Fase X], 🎯 Resultado, 📋 Plan, ├─ sub-pasos, ✓ ok, ✗ error
 *    · Output crudo de las HERRAMIENTAS (muted): ffmpeg frame=X,
 *      mkvmerge Progress, dovi_tool Parsing RPU, Input #0/Stream #0,
 *      banners de version, stderr ruidoso. Todo lo que no empieza con
 *      [ o ━━━ o $ y no tiene marcadores semanticos se considera output
 *      crudo de tool y se renderiza muted + indentado.
 *
 *  Orden de prioridad importa: la primera regla que matchea gana.
 *  Errores > warnings > markers de fase > sub-pasos > resultado > plan >
 *  success > skip > command > tool-output (fallback).
 */
function _classifyLogLine(line) {
  const low = line.toLowerCase();
  // Errores explicitos (fallo duro)
  if (line.includes('✗') || line.includes('⛔') || line.includes('❌')
      || low.includes('error') || low.includes('fallo') || low.includes('aborta')) {
    return 'log-error';
  }
  // Warnings (soft alerts)
  if (line.includes('⚠') || low.includes('warning') || low.includes('aviso')) {
    return 'log-warning';
  }
  // Separadores entre fases
  if (line.includes('━━━')) {
    return 'log-phase';
  }
  // Sub-pasos con box-drawing chars: ├─ ┌─ └─
  if (/[├┌└]─/.test(line)) {
    return 'log-step';
  }
  // Plan (intencion antes de actuar): "📋 Plan:" o "Voy a ..."
  if (line.includes('📋 Plan:') || /\[Fase [A-H]\] Voy a /.test(line)) {
    return 'log-plan';
  }
  // Resultado / conclusion con implicacion para siguientes fases
  if (line.includes('🎯 Resultado:') || line.includes('🎯 Result:')) {
    return 'log-result';
  }
  // Success checkmark
  if (line.includes('✓')) {
    return 'log-success';
  }
  // Skipped steps
  if (line.includes('⏭')) {
    return 'log-skip';
  }
  // Drop-in special case (exito destacado)
  if (line.includes('🚀')) {
    return 'log-highlight';
  }
  // Comando shell ejecutado (transparencia)
  if (/^\s*\$ /.test(line) || /\] \$ /.test(line)) {
    return 'log-command';
  }
  // Fallback: si la linea NO empieza con [Algo] (prefijo de nuestro feedback)
  // y no tiene marcadores semanticos, es output crudo de una herramienta
  // externa (ffmpeg, mkvmerge, dovi_tool, ffprobe) — rendereizar muted.
  // El regex permite prefijo opcional de timestamp "[HH:MM:SS] " que mete
  // _cmv40_log antes del contenido.
  const hasAppPrefix = /^\[\d{2}:\d{2}:\d{2}\]\s*\[(?:Fase|Pipeline|Montando|Desmontando|Preflight|Validaci|sync-data)/i.test(line)
                       || /^\[(?:Fase|Pipeline|Montando|Desmontando|Preflight|Validaci|sync-data)/i.test(line);
  if (!hasAppPrefix) {
    return 'log-tool-output';
  }
  return '';
}

// ── Sistema de hidratación de logs CMv4.0 con watermark anti-duplicados ──
//
// ARQUITECTURA: cada proyecto trackea cuántas líneas de session.output_log
// ya están pintadas en cada uno de sus contenedores DOM (log permanente
// "📜 Log" + log running del overlay). Esto permite:
//
//  1. Hidratar el log permanente al cargar el proyecto (incluso si no hay
//     WS conectado porque running_phase=null) — fix del bug "log incompleto
//     al volver del Mac dormido".
//  2. Actualización incremental al refrescar la sesión: pinta solo las
//     líneas nuevas desde el último watermark.
//  3. WS streaming en vivo: cada línea recibida hace append + watermark++.
//     Cuando luego llega el refresh con la sesión completa, el watermark
//     evita duplicar las líneas que ya entraron por WS.
//
// El estado se guarda en `project._renderedLogCount` (permanente) y
// `project._renderedRunningLogCount` (overlay running, se reinicia al
// crear el overlay). Funciona porque session.output_log es append-only en
// el backend — nunca se borran líneas en mid-flight.

/**
 * Helper: detecta desincronización entre el log permanente del DOM y el
 * `session.output_log` del backend.
 *
 * Caso típico de desincronización (visto en producción durante I/O
 * intensivo del NAS):
 *   - Backend tiene output_log RAM=[L1..L1000], JSON=[L1..L900] (throttle
 *     retrasó los últimos saves).
 *   - Frontend hace fetch → recibe 900 líneas → watermark sube a 900.
 *   - WS entrega L1001 que se acababa de generar → frontend appendea +
 *     watermark sube a 901, PERO esa línea es realmente la 1001ª, no la 901ª.
 *   - Siguiente fetch trae 1001 líneas. _sync slice(901)=L902..L1001.
 *     L1001 ya está en DOM (vino por WS), se pintaría duplicada, y
 *     L901..L1000 quedan SIN pintar nunca → gap visible al usuario.
 *
 * Este helper compara la última línea del DOM con `output_log[watermark-1]`.
 * Si no coinciden → desincronización detectada → caller resetea y repinta.
 *
 * Devuelve true si DOM y backend están sincronizados, false si hubo desync.
 */
function _cmv40LogIsConsistent(containerEl, logArr, watermark) {
  if (watermark === 0) return true;            // nada pintado: trivialmente consistente
  if (watermark > logArr.length) {
    // Backend devolvió MENOS líneas que las pintadas. Caso común:
    //   - El WS entregó L1001 al frontend mientras un fetch REST estaba
    //     en vuelo. El backend respondió ese fetch con un snapshot que
    //     aún no incluía L1001 (throttle del save retrasó el JSON).
    //   - El frontend ya tiene L1001 pintada legítimamente.
    // NO es inconsistencia estructural: solo la session.output_log que
    // recibimos está atrasada. Si reseteáramos el DOM, perderíamos las
    // líneas WS legítimas. Devolvemos true → caller no resetea, y como
    // watermark >= logArr.length el sync simplemente no añade líneas.
    // El próximo fetch (4s) traerá más líneas y se reconciliará.
    return true;
  }
  // Buscar el último elemento .log-line del DOM (puede haber otros nodes).
  const lastEl = containerEl.lastElementChild;
  if (!lastEl) return false;  // DOM vacío pero watermark > 0 → desync
  // Comparamos con la línea backend que correspondería: output_log[watermark-1],
  // saltando las §§PROGRESS§§ que NO se renderizan al DOM.
  let backendIdx = watermark - 1;
  while (backendIdx >= 0) {
    const candidate = logArr[backendIdx];
    if (!_cmv40ParseProgress(candidate)) {
      return lastEl.textContent === candidate;
    }
    backendIdx--;
  }
  // Solo había progress markers — pintamos nada. El DOM debería estar vacío.
  return !lastEl;
}

function _cmv40SyncPermanentLog(project) {
  if (!project || !project.session) return;
  const pid = project.id;
  const containerEl = document.getElementById(`cmv40-log-${pid}`);
  if (!containerEl) return;
  const logArr = project.session.output_log || [];
  const watermark = project._renderedLogCount || 0;
  // Defensa contra desincronización (caso WS-vs-REST race durante I/O
  // intensivo del NAS, ver doc en _cmv40LogIsConsistent). Si detectamos
  // que la última línea del DOM no coincide con output_log[watermark-1],
  // asumimos que el watermark está corrupto y reseteamos: borramos el
  // DOM y re-pintamos desde cero. Coste: O(N) líneas, ms en miles.
  // Beneficio: garantía de orden y completitud absolutas.
  if (!_cmv40LogIsConsistent(containerEl, logArr, watermark)) {
    containerEl.innerHTML = '';
    project._renderedLogCount = 0;
  }
  if ((project._renderedLogCount || 0) >= logArr.length) return;
  const newLines = logArr.slice(project._renderedLogCount || 0);
  for (const line of newLines) {
    // Filtrar marcadores §§PROGRESS§§ (no se muestran como log, solo
    // alimentan la barra de progreso del overlay).
    const prog = _cmv40ParseProgress(line);
    if (prog) continue;
    _appendLogLine(containerEl, line);
  }
  project._renderedLogCount = logArr.length;
  // Cap del DOM: un job CMv4.0 largo (ffmpeg/dovi_tool) genera miles de líneas.
  // La fuente de verdad es session.output_log (re-hidratado por watermark), así
  // que podar los nodos más antiguos no pierde nada y evita inflar el DOM
  // (Tab 1 ya acota con el ring buffer _colaLogLines). audit #11.
  while (containerEl.childNodes.length > 1200) {
    containerEl.removeChild(containerEl.firstChild);
  }
}

function _cmv40SyncRunningLog(project) {
  if (!project || !project.session) return;
  const pid = project.id;
  const containerEl = document.getElementById(`cmv40-running-log-${pid}`);
  if (!containerEl) return;
  const logArr = project.session.output_log || [];
  const watermark = project._renderedRunningLogCount || 0;
  if (!_cmv40LogIsConsistent(containerEl, logArr, watermark)) {
    containerEl.innerHTML = '';
    project._renderedRunningLogCount = 0;
  }
  if ((project._renderedRunningLogCount || 0) >= logArr.length) return;
  let lastProg = null;
  const newLines = logArr.slice(project._renderedRunningLogCount || 0);
  for (const line of newLines) {
    const prog = _cmv40ParseProgress(line);
    if (prog) { lastProg = prog; continue; }
    _appendLogLine(containerEl, line);
  }
  project._renderedRunningLogCount = logArr.length;
  if (lastProg) _cmv40UpdateProgressUI(pid, lastProg);
}

function _appendCMv40Log(project, line) {
  if (!project || !project.session) return;
  const pid = project.id;
  // Marcador de progreso: no se añade al log visual, solo actualiza la barra.
  // Tampoco toca el watermark — desde que el backend dejó de persistirlos
  // (ver _cmv40_progress_should_emit), estas líneas viajan SOLO por WS y no
  // existen en session.output_log. Contarlas desincronizaría el watermark
  // por encima de output_log.length y el siguiente sync se saltaría líneas
  // reales.
  const prog = _cmv40ParseProgress(line);
  if (prog) {
    _cmv40UpdateProgressUI(pid, prog);
    return;
  }
  _appendLogLine(document.getElementById(`cmv40-log-${pid}`), line);
  _appendLogLine(document.getElementById(`cmv40-running-log-${pid}`), line);
  // Watermark++: el WS acaba de entregar una línea que también está
  // (o estará en milisegundos) en session.output_log. Sin este incremento,
  // un refresh posterior intentaría pintarla de nuevo.
  project._renderedLogCount = (project._renderedLogCount || 0) + 1;
  if (document.getElementById(`cmv40-running-log-${pid}`)) {
    project._renderedRunningLogCount = (project._renderedRunningLogCount || 0) + 1;
  }
}

async function _refreshCMv40Session(pid, { includeLog = true } = {}) {
  // silent: timeouts transitorios bajo carga I/O pesada (Fase C/E/F escribiendo
  // 40+ GB) no son utiles al usuario — el siguiente tick los resuelve y el WS
  // sigue trayendo el log. Sin silent, el toast 'el servidor no respondio en 30s'
  // aparecia repetidamente durante extract/inject/remux pesados.
  //
  // includeLog=false: el llamador sabe que el WS está entregando el log y
  // solo quiere el estado. Ahorra ~1,5 MB de payload por tick.
  const qs = includeLog ? '' : '?include_log=false';
  const data = await apiFetch(`/api/cmv40/${pid}${qs}`, { silent: true });
  if (!data) return;
  const project = openCMv40Projects.find(p => p.id === pid);
  if (project) {
    _cmv40AssignSession(project, data);
    _updateCMv40Panel(project);
    if (project.autoContinue && !data.running_phase && !data.error_message) {
      _cmv40MaybeAutoAdvance(project);
    }
  }
  // El sidebar solo si cambió algo que se ve en él. Esta función corre en
  // cada tick del safety poller (4s) y refrescaba la lista SIEMPRE: 569 KB
  // y 193 ms de servidor por tick, ~30 veces por minuto entre unos
  // llamadores y otros, para repintar exactamente lo mismo.
  if (_cmv40SidebarStateChanged(pid, data)) refreshCMv40Sidebar();
}

// Huella de lo que el sidebar muestra de un proyecto. Si no cambia, no hay
// nada que repintar.
const _cmv40SidebarKeys = new Map();

function _cmv40SidebarStateChanged(pid, data) {
  const key = [
    data.phase, data.running_phase || '', data.error_message ? 'e' : '',
    data.archived ? 'a' : '', data.output_mkv_name || '',
  ].join('|');
  if (_cmv40SidebarKeys.get(pid) === key) return false;
  _cmv40SidebarKeys.set(pid, key);
  return true;
}

// ── Render del panel ─────────────────────────────────────────────

function _createCMv40Panel(project) {
  const s = project.session;
  const pid = project.id;
  const panel = document.createElement('div');
  panel.className = 'cmv40-panel subtab-panel';
  panel.id = `cmv40-panel-${pid}`;
  panel.style.display = 'none';
  panel.innerHTML = `
    <div class="project-panel-inner" style="max-width:1100px; margin:0 auto; padding:24px 20px">
      <div id="cmv40-info-${pid}"></div>
      <div id="cmv40-phase-strip-${pid}" class="cmv40-phase-strip"></div>
      <div id="cmv40-active-phase-${pid}"></div>

      <!-- Log de ejecución -->
      <div class="section-card" style="margin-top:16px">
        <div class="section-header">
          <div><div class="section-title">📜 Log</div></div>
          <div style="display:flex; gap:6px">
            <button class="btn btn-ghost btn-xs"
              onclick="copyLogToClipboard('cmv40-log-${pid}', this)"
              data-tooltip="Copiar todo el log al portapapeles">📋 Copiar</button>
            <button class="btn btn-ghost btn-xs" onclick="_clearCMv40Log('${pid}')">🗑️ Limpiar</button>
          </div>
        </div>
        <div class="section-body" style="padding:0">
          <div id="cmv40-log-${pid}" class="cmv40-log"></div>
        </div>
      </div>
    </div>`;
  document.getElementById('cmv40-subtab-content').appendChild(panel);
  _updateCMv40Panel(project);
}

function _clearCMv40Log(pid) {
  const el = document.getElementById(`cmv40-log-${pid}`);
  if (el) el.innerHTML = '';
}

function _updateCMv40Panel(project) {
  const s = project.session;
  const pid = project.id;
  _renderCMv40Info(s, pid);
  _renderCMv40PhaseStrip(s, pid);
  _renderCMv40ActivePhase(project);
  _renderCMv40RunningOverlay(project);
  // Hidrata el log permanente (card "📜 Log" del panel del proyecto) con
  // las líneas que aún no estén pintadas. CRÍTICO para el caso "Mac dormido
  // toda la noche": el job sigue en backend, output_log crece a miles de
  // líneas, y al volver el frontend tiene que ver el log completo aunque
  // running_phase=null y no haya WS activo. Sin esta llamada el container
  // solo recibía líneas via WS streaming y se quedaba vacío post-mortem.
  _cmv40SyncPermanentLog(project);
}

/** ¿Debe cubrirse el panel con el overlay modal de ejecución?
 *
 *  Sale aparte para poder probarla: el overlay es `position:fixed; inset:0`
 *  con z-index 2000, así que mientras esté puesto **se come cualquier clic**
 *  sobre el panel. Mostrarlo cuando el pipeline en realidad está esperando al
 *  usuario no es un defecto cosmético: deja botones que se ven pero no se
 *  pueden pulsar.
 *
 *  Caso real (2026-08-19, The Mandalorian and Grogu): al acabar Fase B con
 *  gates pendientes de ACK, `recentRunning` seguía activo y el overlay tapaba
 *  el banner ámbar. El usuario pulsó "Continuar igualmente", el clic se lo
 *  quedó el overlay, y como no hubo POST tampoco hubo toast de error: el
 *  pipeline se quedó parado y hubo que lanzar cada fase a mano.
 */
function _cmv40PipelineHalted(s) {
  // Estados en los que el pipeline NO va a avanzar solo: o terminó, o está
  // esperando una decisión del usuario. En ambos casos el panel tiene que
  // ser operable.
  return (
    s.phase === 'done'
    || s.phase === 'error'
    || !!s.error_message
    // El pre-flight decidió "Keep recomendado" y espera aceptar o forzar.
    || !!(s.preflight_decision && s.preflight_decision !== 'ok')
    // Gates degradados pendientes de confirmación: el banner ámbar tiene los
    // botones "Cambiar target" y "Continuar igualmente", y hay que poder
    // pulsarlos.
    || !!s.awaiting_critical_ack
  );
}

function _cmv40ShouldShowOverlay(s, project) {
  if (s.running_phase) return true;
  if (_cmv40PipelineHalted(s)) return false;
  if (!project.autoContinue) return false;
  // Puente del auto-pipeline: entre una fase y la siguiente el backend deja
  // running_phase=null un instante. Sin esto el overlay parpadearía.
  //   (a) autoChaining — se enciende al disparar una fase.
  //   (b) recentRunning — hace menos de 15 s había una fase corriendo; red de
  //       seguridad si (a) no se seteó a tiempo.
  const recentRunning = (Date.now() - (project.lastRunningPhaseAt || 0)) < 15000;
  return !!project.autoChaining || recentRunning;
}

function _renderCMv40RunningOverlay(project) {
  const s = project.session;
  const pid = project.id;
  const panel = document.getElementById(`cmv40-panel-${pid}`);
  if (!panel) return;
  let overlay = panel.querySelector('.cmv40-running-overlay');
  const terminalPhase = _cmv40PipelineHalted(s);

  // Auto-pipeline "puente": entre una fase y la siguiente el backend pone
  // running_phase=null brevemente. Dos heurísticas para mantener el overlay
  // sin parpadeo durante esa ventana:
  //   (a) project._autoChaining — flag que se enciende al disparar una fase
  //       desde _cmv40MaybeAutoAdvance o desde el arranque inicial de Fase A.
  //   (b) "recent running" — hace menos de 15s vimos running_phase no-null.
  //       Actúa como red de seguridad si _autoChaining no se seteo a tiempo
  //       (ej. polling tarda en captar el cambio).
  // Se apaga al llegar a terminal o al intervenir manualmente.
  //
  if (terminalPhase) project._autoChaining = false;
  if (s.running_phase) project._lastRunningPhaseAt = Date.now();
  const shouldShow = _cmv40ShouldShowOverlay(s, {
    autoContinue: project.autoContinue,
    autoChaining: project._autoChaining,
    lastRunningPhaseAt: project._lastRunningPhaseAt,
  });

  if (shouldShow) {
    // Crear o actualizar overlay
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.className = 'cmv40-running-overlay';
      overlay.innerHTML = `
        <div class="cmv40-running-box">
          <div class="cmv40-running-timeline-wrap">
            <!-- Mini cabecera con la pelicula que se esta procesando — vive en
                 el TOP de la columna izquierda, encima del timeline. Solo ocupa
                 el ancho de la columna (330px), no el de todo el modal. -->
            <div class="cmv40-running-movie" id="cmv40-running-movie-${pid}">
              <div class="cmv40-running-movie-poster" id="cmv40-running-movie-poster-${pid}">🎬</div>
              <div class="cmv40-running-movie-info">
                <div class="cmv40-running-movie-title" id="cmv40-running-movie-title-${pid}"></div>
                <div class="cmv40-running-movie-meta" id="cmv40-running-movie-meta-${pid}"></div>
              </div>
            </div>
            <div class="cmv40-running-timeline-inner" id="cmv40-running-timeline-${pid}"></div>
          </div>
          <div class="cmv40-running-main">
            <div class="cmv40-running-header">
              <div class="cmv40-running-spinner"></div>
              <div style="flex:1">
                <div class="cmv40-running-title" id="cmv40-running-title-${pid}"></div>
                <div class="cmv40-running-subtitle" id="cmv40-running-subtitle-${pid}">El proyecto está bloqueado mientras se ejecuta la tarea</div>
              </div>
              <button class="btn btn-ghost btn-sm"
                onclick="copyLogToClipboard('cmv40-running-log-${pid}', this)"
                data-tooltip="Copiar el log actual al portapapeles">📋 Copiar log</button>
              <button class="btn btn-danger btn-sm" onclick="cmv40CancelRunning('${pid}')">🛑 Cancelar</button>
            </div>
            <div class="cmv40-progress" id="cmv40-progress-${pid}"
              style="padding:14px 18px; background:#1a1e2a; border-bottom:1px solid #2a2f3d; display:flex; flex-direction:column; gap:10px">
              <div class="cmv40-progress-meta"
                style="display:flex; align-items:baseline; justify-content:space-between; gap:12px; font-size:12px">
                <span class="cmv40-progress-label" id="cmv40-progress-label-${pid}"
                  style="font-weight:600; color:#e8ecf4; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; flex:1">Preparando…</span>
                <span class="cmv40-progress-right"
                  style="display:flex; align-items:baseline; gap:12px; flex-shrink:0; font-variant-numeric:tabular-nums">
                  <span class="cmv40-progress-eta" id="cmv40-progress-eta-${pid}"
                    style="color:#9aa3b2; font-size:11px"></span>
                  <span class="cmv40-progress-pct" id="cmv40-progress-pct-${pid}"
                    style="color:#4da3ff; font-weight:700; font-size:15px; min-width:54px; text-align:right">—</span>
                </span>
              </div>
              <div class="cmv40-progress-track"
                style="height:14px; background:#0b0e17; border:1px solid #2a2f3d; border-radius:8px; overflow:hidden; position:relative; box-shadow:inset 0 1px 3px rgba(0,0,0,0.5)">
                <div class="cmv40-progress-bar indeterminate" id="cmv40-progress-bar-${pid}"></div>
              </div>
            </div>
            <div class="cmv40-running-log" id="cmv40-running-log-${pid}"></div>
          </div>
        </div>`;
      panel.appendChild(overlay);
      // Suscribe al WebSocket para actualizar el log en tiempo real
      _cmv40BindRunningLog(project);
    }
    // Hidratar la mini cabecera de pelicula (poster + titulo). Si la sesion
    // ya tiene tmdb_info cacheado, usarlo directo; si no, usar source_mkv_name
    // como fallback de texto y disparar lookup TMDb async.
    _cmv40HydrateRunningMovieHeader(project);
    // Actualizar título + subtítulo según estemos en una fase o "puente"
    const titleEl    = document.getElementById(`cmv40-running-title-${pid}`);
    const subtitleEl = document.getElementById(`cmv40-running-subtitle-${pid}`);
    if (titleEl) {
      if (s.running_phase) {
        const autoTag = project.autoContinue ? '🤖 Auto · ' : '';
        titleEl.textContent = autoTag + (CMV40_RUNNING_LABELS[s.running_phase] || `Ejecutando: ${s.running_phase}`);
        if (subtitleEl) subtitleEl.textContent = 'El proyecto está bloqueado mientras se ejecuta la tarea';
      } else {
        // Modo puente: fase X completada, siguiente a punto de arrancar.
        // En vez de mostrar "Preparando siguiente fase" (redundante y vago),
        // mostramos el titulo de la proxima fase deducida del estado actual.
        const nextPhase = _cmv40GuessNextPhase(s);
        const autoTag = project.autoContinue ? '🤖 Auto · ' : '';
        if (nextPhase) {
          titleEl.textContent = autoTag + nextPhase;
          if (subtitleEl) subtitleEl.textContent = 'Transición entre fases — arrancando en un instante';
        } else {
          titleEl.textContent = autoTag + 'Encadenando fases…';
          if (subtitleEl) subtitleEl.textContent = '';
        }
      }
    }
    // Actualizar timeline en cada tick — incremental, NO innerHTML wholesale.
    // Antes reemplazabamos todo el HTML, lo que (a) reiniciaba la animación
    // del spinner de la fase en curso (elemento destruido/recreado cada tick),
    // (b) rompía el CSS transition de la barra de progreso total (cada vez
    // un elemento nuevo con width inicial 0% → sin transición), y (c) saltaba
    // el scroll de .cmv40-tl-steps a 0 (nuevo DOM).
    const tlWrap = document.getElementById(`cmv40-running-timeline-${pid}`);
    if (tlWrap) _cmv40UpdateTimelineIncremental(tlWrap, s, project);
    // CRÍTICO: sincronizar el running log desde session.output_log en cada
    // tick (no solo cuando se crea el overlay). Antes el running log solo
    // se actualizaba via WS messages — si el cliente WS estaba zombie tras
    // Mac sleep, las líneas que llegaban via REST refresh NO aparecían en
    // el running overlay (solo en el log permanente). El watermark +
    // consistency check del helper evita duplicados con las líneas que SÍ
    // llegaron por WS.
    _cmv40SyncRunningLog(project);
  } else if (overlay) {
    // Quitar overlay con animación — solo cuando SEGURO que no hay más fases
    overlay.classList.add('closing');
    setTimeout(() => overlay.remove(), 200);
  }
}

/** Update incremental del timeline — actualiza solo los campos que cambian
 *  sin reemplazar el DOM (preserva animación del spinner, CSS transition de
 *  la barra de progreso total, y scrollTop de la lista de pasos). */
function _cmv40UpdateTimelineIncremental(tlWrap, s, project) {
  // Si el timeline aun no existe (primera vez), render completo.
  if (!tlWrap.querySelector('.cmv40-tl-steps')) {
    tlWrap.innerHTML = _cmv40RenderTimeline(s, project);
    // Posiciona la fase activa centrada al abrir el modal — sin animación
    // (estamos en frame 0, smooth se vería como un salto).
    const stepsElInit = tlWrap.querySelector('.cmv40-tl-steps');
    const stepsInit = _cmv40PlanAutoSteps(s);
    const statusesInit = stepsInit.map(st => _cmv40StepStatus(st, s));
    const activeIdxInit = statusesInit.findIndex(st => st === 'running');
    const activeKeyInit = activeIdxInit >= 0
      ? stepsInit[activeIdxInit].key
      : (stepsInit[stepsInit.length - 1] && stepsInit[stepsInit.length - 1].key);
    if (stepsElInit && activeKeyInit) {
      _cmv40ScrollActiveStepIntoView(stepsElInit, activeKeyInit, 'auto');
      tlWrap.dataset.activeStepKey = activeKeyInit;
    }
    return;
  }

  // Recalcular métricas
  const steps = _cmv40PlanAutoSteps(s, project);
  const stepStatuses = steps.map(st => _cmv40StepStatus(st, s));
  const doneCount = stepStatuses.filter(st => st === 'done' || st === 'skipped').length;
  const totalCount = steps.length;
  // El porcentaje por fases completadas es escalonado: se queda clavado los
  // minutos que dura cada fase. Si el backend manda `job_pct` (ponderado por
  // lo que pesa cada fase y con el avance real de la que corre — ver
  // _cmv40_job_pct), ese manda. El escalonado queda de respaldo.
  const progressPct = (project && project._jobPct != null && !isTerminal0(s))
    ? Math.round(project._jobPct)
    : (totalCount > 0 ? Math.round((doneCount / totalCount) * 100) : 0);

  // Timer: elapsed / remaining (mismo helper que el full render — garantiza
  // que ambos rendered + tick usan el MISMO startedMs cacheado, sin saltos
  // entre fuentes server-time vs client-cached).
  const startedMs = _cmv40ResolveStartedMs(s, project);
  const hist = s.phase_history || [];
  const isTerminal = (s.phase === 'done' || !!s.error_message);
  let elapsedLabel  = '—';
  let remainingText = '';
  let newBaseRemaining = null;   // null = no actualizar data-base-remaining
  if (startedMs) {
    let elapsedSecs;
    if (isTerminal) {
      const lastWithEnd = [...hist].reverse().find(h => h.finished_at);
      const endMs = lastWithEnd ? Date.parse(lastWithEnd.finished_at) : Date.now();
      elapsedSecs = (endMs - startedMs) / 1000;
      remainingText = s.phase === 'done' ? 'finalizado' : (s.error_message ? 'con error' : '');
    } else {
      elapsedSecs = (Date.now() - startedMs) / 1000;
      newBaseRemaining = _cmv40ComputeRemainingSecs(s, steps, stepStatuses, hist, project);
      remainingText = _cmv40TextoRestante(newBaseRemaining, s);
    }
    elapsedLabel = _cmv40FmtClock(elapsedSecs);
  }

  // Update header fields (mismos elementos, solo text/style — transiciones OK)
  const elapsedEl   = tlWrap.querySelector('.cmv40-tl-timer-elapsed');
  const remainingEl = tlWrap.querySelector('.cmv40-tl-timer-remaining');
  const pctEl       = tlWrap.querySelector('.cmv40-tl-progress-pct');
  const fillEl      = tlWrap.querySelector('.cmv40-tl-progress-fill');
  const progressBox = tlWrap.querySelector('.cmv40-tl-progress');
  // Sincroniza data-started-at del DOM con el cache canónico — el tick lee
  // de ahí, y debe coincidir con el startedMs que usa este render. Sin
  // esto el contador alterna entre dos valores cuando la fuente cambia.
  if (elapsedEl && startedMs && elapsedEl.dataset.startedAt !== String(startedMs)) {
    elapsedEl.dataset.startedAt = String(startedMs);
  }
  if (elapsedEl   && elapsedEl.textContent   !== elapsedLabel)   elapsedEl.textContent   = elapsedLabel;
  if (remainingEl && remainingEl.textContent !== remainingText)  remainingEl.textContent = remainingText;
  // Refrescar snapshot del remaining — el tick de 1s decrementa desde aquí.
  if (elapsedEl && newBaseRemaining !== null) {
    elapsedEl.dataset.baseRemaining = String(newBaseRemaining);
    elapsedEl.dataset.etaSufijo = _cmv40SufijoEta(s);
    elapsedEl.dataset.baseAt = String(Date.now());
  }
  const pctText = `${doneCount}/${totalCount} · ${progressPct}%`;
  if (pctEl       && pctEl.textContent       !== pctText)        pctEl.textContent       = pctText;
  if (fillEl) {
    const newW = progressPct + '%';
    if (fillEl.style.width !== newW) fillEl.style.width = newW;
  }
  if (progressBox) {
    const cls = isTerminal && !s.error_message ? 'cmv40-tl-progress-done'
              : s.error_message ? 'cmv40-tl-progress-error'
              : '';
    progressBox.classList.toggle('cmv40-tl-progress-done',  cls === 'cmv40-tl-progress-done');
    progressBox.classList.toggle('cmv40-tl-progress-error', cls === 'cmv40-tl-progress-error');
  }

  // Update trust badge: el badge se calcula dinamicamente a partir del
  // estado (gates evaluados / trust_ok / trust_override) y debe refrescarse
  // cuando cambia la fase. Sin esto, el badge se queda en "pendiente
  // validaciones" aun despues de que Fase B haya clasificado el target.
  const trustBadgeEl = tlWrap.querySelector('.cmv40-tl-trust-badge');
  if (trustBadgeEl) {
    const gatesEvaluated2 = !!(s.target_trust_gates && Object.keys(s.target_trust_gates).length);
    const targetProvidedIdx2 = CMV40_PHASES_ORDER.indexOf('target_provided');
    const curPhaseIdx2 = CMV40_PHASES_ORDER.indexOf(s.phase);
    const beforeGates2 = curPhaseIdx2 < targetProvidedIdx2 || !gatesEvaluated2;
    let cls2, txt2;
    if (beforeGates2) {
      cls2 = 'pending'; txt2 = '⏳ Auto · pendiente validaciones';
    } else if (_cmv40Trust(s)) {
      cls2 = 'trusted'; txt2 = '🚀 Auto · trusted';
    } else {
      cls2 = 'manual'; txt2 = '🔬 Manual · revisión visual';
    }
    if (trustBadgeEl.textContent !== txt2) {
      trustBadgeEl.textContent = txt2;
    }
    // Asegurar que solo tiene la clase correcta de las tres
    trustBadgeEl.classList.toggle('pending', cls2 === 'pending');
    trustBadgeEl.classList.toggle('trusted', cls2 === 'trusted');
    trustBadgeEl.classList.toggle('manual',  cls2 === 'manual');
  }

  // Update de steps: solo reemplaza el HTML de la lista si el HASH de
  // estados+labels cambió (evita spinner restart cuando no cambia nada).
  const newStepsHash = stepStatuses.map((st, i) =>
    `${steps[i].key}:${st}:${steps[i].customLabel || ''}`
  ).join('|');
  const stepsEl = tlWrap.querySelector('.cmv40-tl-steps');
  if (stepsEl && stepsEl.dataset.hash !== newStepsHash) {
    const savedScroll = stepsEl.scrollTop;
    // Re-genera solo los <li> de steps, no toca el <ol> wrapper (mantiene
    // scrollTop implícitamente si no tocamos el contenedor... pero innerHTML
    // sí reemplaza hijos → guardamos scrollTop y lo restauramos).
    stepsEl.innerHTML = _cmv40RenderTimelineStepsHTML(steps, stepStatuses, s);
    stepsEl.scrollTop = savedScroll;
    stepsEl.dataset.hash = newStepsHash;
  }

  // Auto-scroll dinámico: cuando avanza la fase en curso, traer la nueva
  // fase activa al centro del panel lateral con scroll suave. Solo se
  // dispara cuando cambia la KEY (no en cada tick) — así el usuario puede
  // hacer scroll manual dentro de una fase sin que el timeline rebote.
  // Si ya no hay fase running (terminal: done o error), apuntamos a la
  // última fase con estado distinto de pending para mantener el contexto.
  const activeIdx = stepStatuses.findIndex(st => st === 'running');
  let activeKey = activeIdx >= 0 ? steps[activeIdx].key : null;
  if (!activeKey) {
    for (let i = stepStatuses.length - 1; i >= 0; i--) {
      if (stepStatuses[i] !== 'pending') { activeKey = steps[i].key; break; }
    }
  }
  if (stepsEl && activeKey && tlWrap.dataset.activeStepKey !== activeKey) {
    _cmv40ScrollActiveStepIntoView(stepsEl, activeKey, 'smooth');
    tlWrap.dataset.activeStepKey = activeKey;
  }
}

/** Genera solo el contenido interno (<li>...</li>) de la lista de steps.
 *  Extraído de _cmv40RenderTimeline para reuso desde el update incremental. */
function _cmv40RenderTimelineStepsHTML(steps, stepStatuses, s) {
  return steps.map((st, i) => {
    const status = stepStatuses[i];
    const iconMap = {
      done:    '<span class="cmv40-tl-status-icon done">✓</span>',
      running: '<span class="cmv40-tl-status-icon running"></span>',
      skipped: '<span class="cmv40-tl-status-icon skipped">⏭</span>',
      pending: '<span class="cmv40-tl-status-icon pending"></span>',
    };
    const elapsed = status === 'done' ? _cmv40StepElapsedSecs(st.key, s) : null;
    const doneLabel = elapsed != null
      ? `completado · ${_cmv40FmtClock(elapsed)}`
      : 'completado';
    const defaultLabel = status === 'done'    ? doneLabel
                       : status === 'skipped' ? 'omitida'
                       : status === 'running' ? 'en curso…'
                       : `Restante ${_cmv40FmtEta(st.etaSecs)}`;
    const label = st.customLabel || defaultLabel;
    const etaHtml = `<span class="cmv40-tl-eta ${status}">${escHtml(label)}</span>`;
    return `<li class="cmv40-tl-step cmv40-tl-${status}" data-step-key="${escHtml(st.key)}">
      <div class="cmv40-tl-rail">${iconMap[status]}</div>
      <div class="cmv40-tl-body">
        <div class="cmv40-tl-title">
          <span class="cmv40-tl-phase-icon">${st.icon}</span>
          <span>${escHtml(st.title)}</span>
        </div>
        <div class="cmv40-tl-what">${escHtml(st.what)}</div>
        ${etaHtml}
      </div>
    </li>`;
  }).join('');
}

/** Auto-scroll del timeline lateral para mantener visible la fase activa.
 *  Se ejecuta solo cuando cambia la fase running (no en cada tick) — así no
 *  pelea contra el scroll manual del usuario dentro de una misma fase.
 *  Si no hay fase running (todo done), se hace scroll a la última fase
 *  completada para que el usuario vea el final del recorrido. */
function _cmv40ScrollActiveStepIntoView(stepsEl, activeKey, behavior = 'smooth') {
  if (!stepsEl || !activeKey) return;
  const li = stepsEl.querySelector(`li.cmv40-tl-step[data-step-key="${activeKey}"]`);
  if (!li) return;
  const containerH = stepsEl.clientHeight;
  const liTop      = li.offsetTop;
  const liH        = li.offsetHeight;
  // Centra el step activo verticalmente dentro del contenedor scrollable.
  const target = liTop - (containerH / 2) + (liH / 2);
  const max = stepsEl.scrollHeight - containerH;
  stepsEl.scrollTo({ top: Math.max(0, Math.min(max, target)), behavior });
}

function _cmv40ParseProgress(line) {
  // Detecta marcadores §§PROGRESS§§{json} (con o sin timestamp [HH:MM:SS] delante)
  const m = line.match(/§§PROGRESS§§(\{.*\})/);
  if (!m) return null;
  try { return JSON.parse(m[1]); } catch { return null; }
}

function _cmv40UpdateProgressUI(pid, prog) {
  const bar = document.getElementById(`cmv40-progress-bar-${pid}`);
  const pct = document.getElementById(`cmv40-progress-pct-${pid}`);
  const lab = document.getElementById(`cmv40-progress-label-${pid}`);
  const eta = document.getElementById(`cmv40-progress-eta-${pid}`);
  if (!bar || !pct || !lab) return;
  const p = Math.max(0, Math.min(100, prog.pct ?? 0));
  bar.classList.remove('indeterminate');
  bar.style.width = p + '%';
  pct.textContent = p.toFixed(1) + '%';
  lab.textContent = prog.label || '';
  if (eta) {
    if (prog.eta_s != null && prog.eta_s > 0) {
      const m = Math.floor(prog.eta_s / 60);
      const s = prog.eta_s % 60;
      eta.textContent = `Restante ${m}:${String(s).padStart(2, '0')}`;
    } else {
      eta.textContent = '';
    }
  }
  // El progreso del JOB y la ETA de la fase en curso NO se pintan aquí: van
  // a la barra del timeline lateral, que es la que ya cumplía esa función.
  // Se guardan en el proyecto para que el próximo render del timeline los
  // use en lugar de sus estimaciones.
  const project = openCMv40Projects.find(p => p.id === pid);
  if (project) {
    if (prog.job_pct != null) project._jobPct = prog.job_pct;
    project._phaseEtaSecs = (prog.eta_s != null && prog.eta_s > 0) ? prog.eta_s : null;
  }
}

/** Hidrata la mini cabecera del overlay con la pelicula que se procesa.
 *  Pinta poster (de tmdb_info si existe, fallback emoji) + titulo + año.
 *  Si la sesion no tiene tmdb_info, dispara hydrateTmdbCard logica
 *  tras un pequeño defer (no bloquea el render del overlay). */
function _cmv40HydrateRunningMovieHeader(project) {
  const pid = project.id;
  const posterEl = document.getElementById(`cmv40-running-movie-poster-${pid}`);
  const titleEl  = document.getElementById(`cmv40-running-movie-title-${pid}`);
  const metaEl   = document.getElementById(`cmv40-running-movie-meta-${pid}`);
  if (!posterEl || !titleEl || !metaEl) return;

  const s = project.session || {};
  const t = s.tmdb_info || null;

  // Poster: usa tmdb si tenemos URL, sino emoji 🎬 placeholder
  if (t && t.poster_url) {
    posterEl.innerHTML = `<img src="${escHtml(t.poster_url)}" alt="${escHtml(t.title || '')}" loading="lazy">`;
  } else {
    posterEl.textContent = '🎬';
  }

  // Titulo: tmdb.title > source_mkv_name limpio > id
  let titleText = '';
  if (t && t.title) titleText = t.title;
  else if (s.source_mkv_name) {
    // Limpia .mkv y reemplaza separadores comunes por espacios para
    // hacer el filename mas legible mientras llega tmdb_info.
    titleText = s.source_mkv_name.replace(/\.mkv$/i, '').replace(/[\._]+/g, ' ');
  }
  else titleText = s.id || '—';
  titleEl.textContent = titleText;

  // Meta: año + runtime + génenros (si tmdb), si no nada
  if (t) {
    const parts = [];
    if (t.year) parts.push(String(t.year));
    if (t.runtime_minutes) parts.push(`${Math.floor(t.runtime_minutes/60)}h ${t.runtime_minutes%60}min`);
    if (t.genres && t.genres.length) parts.push(t.genres.slice(0, 2).join(' · '));
    metaEl.textContent = parts.join(' · ');
  } else {
    metaEl.textContent = '';
  }
}


function _cmv40BindRunningLog(project) {
  // El overlay running se acaba de crear → reset del watermark del running
  // log y delega la hidratación al helper centralizado, que pinta toda la
  // sesión y trackea el contador para futuras incrementales.
  const pid = project.id;
  const logEl = document.getElementById(`cmv40-running-log-${pid}`);
  if (!logEl) return;
  logEl.innerHTML = '';
  project._renderedRunningLogCount = 0;
  _cmv40SyncRunningLog(project);
  // Primera hidratación: al fondo (el usuario aún no ha scrolleado)
  logEl.scrollTop = logEl.scrollHeight;
}

async function cmv40CancelRunning(pid) {
  const project = openCMv40Projects.find(p => p.id === pid);
  const phaseLabel = project && project.session && project.session.running_phase
    ? (CMV40_RUNNING_LABELS[project.session.running_phase] || project.session.running_phase)
    : 'la fase actual';
  const isAuto = project && project.autoContinue;
  // Mensaje contextual: explica que cancela el subprocess en curso y, si
  // el auto-pipeline esta activo, que tambien se desactiva el auto-avance
  // (no lanza la siguiente fase).
  const message = isAuto
    ? `Se matará el subprocess de "${phaseLabel}", se limpiarán los temporales generados y se desactivará el auto-avance del pipeline. Las fases ya completadas se conservan; podrás relanzar manualmente la fase cuando quieras.`
    : `Se matará el subprocess de "${phaseLabel}" y se limpiarán los temporales generados. Las fases ya completadas se conservan; podrás relanzar manualmente la fase cuando quieras.`;
  showConfirm(
    '¿Cancelar la ejecución en curso?',
    message,
    async () => {
      await apiFetch(`/api/cmv40/${pid}/cancel`, { method: 'POST' });
      const proj = openCMv40Projects.find(p => p.id === pid);
      if (proj) {
        proj._lastAutoFiredFor = null;
        proj._lastAutoFiredAt = 0;
        proj._autoChaining = false;
        if (proj.autoContinue) {
          proj.autoContinue = false;
          showToast('Cancelado — auto-avance desactivado', 'info');
        } else {
          showToast('Cancelando…', 'info');
        }
      }
    },
    'Cancelar fase',
  );
}

// Hidrata una ficha TMDb en el DOM (container dado) a partir de un
// filename. Cache por clave en `_tmdbCardCache` para evitar re-fetches.
// Uso: desde Tab 1, Tab 2 y Tab 3, pasar el id del contenedor + filename.
const _tmdbCardCache = new Map();  // clave = filename -> details|null

async function hydrateTmdbCard(containerId, filename) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!filename) { el.innerHTML = ''; return; }

  // Cache hit inmediato
  if (_tmdbCardCache.has(filename)) {
    el.innerHTML = renderTmdbCardHTML(_tmdbCardCache.get(filename)) || '';
    return;
  }
  // Skeleton mínimo mientras llega la respuesta
  el.innerHTML = '<div class="tmdb-card-loading"></div>';

  try {
    const data = await apiFetch('/api/cmv40/tmdb-lookup', {
      method: 'POST',
      body: JSON.stringify({ source_mkv_name: filename }),
    });
    const details = (data && data.details) ? data.details : null;
    _tmdbCardCache.set(filename, details);
    el.innerHTML = renderTmdbCardHTML(details) || '';
  } catch {
    el.innerHTML = '';
  }
}

// Genérico — reutilizable para Tab 1, Tab 2 y Tab 3.
function renderTmdbCardHTML(t) {
  if (!t) return '';
  const metaParts = [];
  if (t.year) metaParts.push(String(t.year));
  if (t.runtime_minutes)
    metaParts.push(`${Math.floor(t.runtime_minutes/60)}h ${t.runtime_minutes%60}min`);
  if (t.genres && t.genres.length) metaParts.push(t.genres.join(' · '));

  const ratingHtml = (t.vote_count > 0)
    ? `<span class="cmv40-tmdb-rating" data-tooltip="${t.vote_count.toLocaleString()} votos en TMDb">★ ${t.vote_average.toFixed(1)}</span>`
    : '';
  const origHtml = (t.original_title && t.original_title !== t.title)
    ? `<span class="cmv40-tmdb-orig">· ${escHtml(t.original_title)}</span>`
    : '';
  const taglineHtml = t.tagline
    ? `<div class="cmv40-tmdb-tagline">“${escHtml(t.tagline)}”</div>`
    : '';
  const overviewHtml = t.overview
    ? `<div class="cmv40-tmdb-overview">${escHtml(t.overview)}</div>`
    : '';

  const links = [];
  if (t.tmdb_url) links.push(`<a href="${escHtml(t.tmdb_url)}" target="_blank" rel="noreferrer noopener">TMDb</a>`);
  if (t.imdb_id)   links.push(`<a href="https://www.imdb.com/title/${escHtml(t.imdb_id)}/" target="_blank" rel="noreferrer noopener">IMDb</a>`);
  if (t.homepage)  links.push(`<a href="${escHtml(t.homepage)}" target="_blank" rel="noreferrer noopener">Web oficial</a>`);
  const linksHtml = links.length ? `<div class="cmv40-tmdb-links">${links.join(' · ')}</div>` : '';

  const posterHtml = t.poster_url
    ? `<img class="cmv40-tmdb-poster" src="${escHtml(t.poster_url)}" alt="${escHtml(t.title)}" loading="lazy">`
    : `<div class="cmv40-tmdb-poster cmv40-tmdb-poster-placeholder">🎬</div>`;
  const backdropHtml = t.backdrop_url
    ? `<div class="cmv40-tmdb-backdrop" style="background-image: url('${escHtml(t.backdrop_url)}');"></div>`
    : '';

  return `
    <div class="cmv40-tmdb-card">
      ${backdropHtml}
      ${posterHtml}
      <div class="cmv40-tmdb-info">
        <div class="cmv40-tmdb-titlerow">
          <span class="cmv40-tmdb-title">${escHtml(t.title || t.original_title || '—')}</span>
          ${origHtml}
          ${ratingHtml}
        </div>
        ${metaParts.length ? `<div class="cmv40-tmdb-meta">${escHtml(metaParts.join(' · '))}</div>` : ''}
        ${taglineHtml}
        ${overviewHtml}
        ${linksHtml}
      </div>
    </div>`;
}

function _renderCMv40Info(s, pid) {
  const container = document.getElementById(`cmv40-info-${pid}`);
  if (!container) return;
  const srcDv = s.source_dv_info;
  const tgtDv = s.target_dv_info;
  const canEditName = s.phase !== 'done' && !s.archived;
  const project = openCMv40Projects.find(p => p.id === pid);
  const autoOn = !!(project && project.autoContinue);
  const canAuto = s.phase !== 'done' && !s.archived;
  const tmdbCardHtml = renderTmdbCardHTML(s.tmdb_info);
  container.innerHTML = `
    ${tmdbCardHtml}
    <div class="section-card">
      <div class="section-header" style="display:flex; align-items:flex-start; justify-content:space-between; gap:12px">
        <div><div class="section-title">🎬 Proyecto CMv4.0</div>
        <div class="section-subtitle">💾 Los cambios se guardan automáticamente tras cada acción. Cerrar la pestaña no pierde nada.</div></div>
        ${canAuto ? `
        <button class="btn btn-${autoOn ? 'primary' : 'ghost'} btn-sm" onclick="cmv40ToggleAuto('${pid}')"
          data-tooltip="${(() => {
            const trust = _cmv40Trust(s);
            if (trust) return 'Auto-ejecuta el pipeline completo A→H sin pausas. Los trust gates ya aprobaron alineación, Fase D se omite automáticamente.';
            if (s.target_type) return 'Auto-ejecuta cada fase tras la anterior. Si los trust gates no aprueban, pausa en Fase D para revisión manual del chart.';
            return 'Auto-ejecuta cada fase tras la anterior. La pausa en Fase D depende del target — sin gates trusted requiere revisión manual del chart.';
          })()}">
          ${autoOn ? '🤖 Auto ON' : '🤖 Auto OFF'}
        </button>` : ''}
      </div>
      <div class="section-body">
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px">
          <div>
            <div style="font-size:11px; color:var(--text-3); margin-bottom:2px">MKV origen</div>
            <div style="font-weight:600">${escHtml(s.source_mkv_name)}</div>
            <div style="font-size:11px; color:var(--text-3); margin-top:4px">
              ${srcDv ? `Profile ${srcDv.profile}${srcDv.el_type ? ` (${srcDv.el_type})` : ''} · CM ${srcDv.cm_version} · ${s.source_frame_count.toLocaleString()} frames` : 'Sin analizar'}
            </div>
            ${s.source_workflow ? `<div style="font-size:10px; margin-top:4px">
              <span class="cmv40-workflow-badge cmv40-workflow-${s.source_workflow}">${_cmv40WorkflowLabel(s.source_workflow)}</span>
            </div>` : ''}
          </div>
          <div>
            <div style="font-size:11px; color:var(--text-3); margin-bottom:2px">MKV salida ${canEditName ? '<span style="color:var(--text-3)">· editable</span>' : ''}</div>
            ${canEditName
              ? `<input type="text" id="cmv40-output-name-${pid}" class="cmv40-output-name-input"
                    value="${escHtml(s.output_mkv_name)}"
                    onblur="_cmv40SaveOutputName('${pid}', this.value)"
                    onkeydown="if(event.key==='Enter'){this.blur()}">`
              : `<div style="font-weight:600">${escHtml(s.output_mkv_name)}</div>`}
            <div style="font-size:11px; color:var(--text-3); margin-top:4px">
              ${tgtDv ? `RPU target: Profile ${tgtDv.profile}${tgtDv.el_type ? ` (${tgtDv.el_type})` : ''} · CM ${tgtDv.cm_version} · ${s.target_frame_count.toLocaleString()} frames` : ''}
              ${s.sync_delta ? ` · <span style="color:var(--orange)">Δ ${s.sync_delta > 0 ? '+' : ''}${s.sync_delta} frames</span>` : ''}
            </div>
          </div>
        </div>
      </div>
    </div>
    ${_renderCMv40SheetCard(s, pid)}
    ${_renderCMv40RecommendationCard(s, pid)}`;

  // Si aún no tenemos tmdb_info, intentamos hidratarlo (puede haber fallado la
  // tarea background). Best-effort, sin bloquear UI.
  if (!s.tmdb_info && !project?._tmdbLookupTried) {
    if (project) project._tmdbLookupTried = true;
    _cmv40HydrateTmdbClient(pid);
  }

  // Veredicto del sheet: se pinta en el slot recién creado (el banner usa
  // innerHTML sobre un contenedor, no se puede devolver como string).
  if (s.sheet_recommendation) {
    _cmv40RenderRecommendation(s.sheet_recommendation, `cmv40-sheet-banner-${pid}`);
  } else if (!project?._sheetLookupTried) {
    // Proyectos creados antes de que el veredicto se persistiera: se pide
    // una vez por apertura y el polling lo recoge.
    if (project) project._sheetLookupTried = true;
    _cmv40HydrateSheetClient(pid);
  }
}

/**
 * Card "📋 Hoja de DoviTools" del panel del proyecto. Mantiene el veredicto,
 * los avisos y el offset conocido visibles durante todo el pipeline — antes
 * solo existían en el modal de creación y se perdían justo antes de Fase D,
 * que es donde hacen falta.
 */
function _renderCMv40SheetCard(s, pid) {
  if (!s.sheet_recommendation) return '';
  return `
    <div class="section-card" style="margin-top:12px">
      <div class="section-header">
        <span class="section-icon">📋</span>
        <div>
          <div class="section-title">Hoja de DoviTools</div>
          <div class="section-subtitle">Lo que la comunidad ha documentado sobre este título</div>
        </div>
        <button class="btn btn-ghost btn-xs" onclick="_cmv40HydrateSheetClient('${pid}')"
          data-tooltip="Vuelve a consultar la hoja (la caché dura 1 h)"
          style="margin-left:auto; color:var(--text-2)">↻ Actualizar</button>
      </div>
      <div style="padding:0 16px 14px">
        <div id="cmv40-sheet-banner-${pid}" class="cmv40-rec-banner"></div>
      </div>
    </div>`;
}

/** Pide al backend el veredicto del sheet para un proyecto ya creado. */
async function _cmv40HydrateSheetClient(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}/refresh-sheet`, { method: 'POST' });
  if (!data?.sheet_recommendation) return;
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project?.session) return;
  project.session.sheet_recommendation = data.sheet_recommendation;
  if (activeCMv40SubTabId === pid) _renderCMv40Info(project.session, pid);
}

/**
 * Renderiza la card "🎯 Análisis y recomendación" del modelo Keep/Restore.
 * Solo aparece si tenemos datos del análisis del bin (target_l8_classification
 * != ''). Muestra:
 *   - Calidad del bin (CMv4 CORE / CORE+ / FULL / DEFAULT)
 *   - Comparación L2 source vs target (cuando Fase A ya corrió)
 *   - Recomendación final con badge grande
 *   - Botones de acción cuando recommended_action="keep"
 */
function _renderCMv40RecommendationCard(s, pid) {
  // No mostrar la card si no hay análisis del bin todavía (típico antes de
  // que termine el pre-flight, o sesiones legacy sin estos campos).
  if (!s.target_l8_classification) return '';

  const action = s.recommended_action || '';
  const isKeep = action === 'keep';
  const isDropIn = action === 'drop_in';
  const isMerge = action === 'merge';
  const isUnknown = action === 'unknown' || action === '';
  const projectDone = (s.phase === 'done' || s.archived);

  // Badge alineado a la paleta de la app (light mode, variables CSS).
  // Patrón estándar: dim background + border + color del nivel semántico.
  const badgeStyle = isKeep
    ? 'background:var(--blue-dim); color:var(--blue); border:1px solid var(--blue-border)'
    : isDropIn
    ? 'background:var(--green-dim); color:var(--green); border:1px solid var(--green-border)'
    : isMerge
    ? 'background:var(--orange-dim); color:var(--orange); border:1px solid var(--orange-border)'
    : 'background:var(--surface-2); color:var(--text-2); border:1px solid var(--sep)';

  const label = s.recommended_action_label || (isUnknown ? '⏳ Esperando análisis' : '—');
  const reason = s.recommended_action_reason || '';

  // Tag de calidad del bin (la que va al filename)
  const qualityTag = s.target_l8_quality_label || (
    s.target_l8_classification === 'default' ? 'CMv4 sintético' :
    s.target_l8_classification === 'real' ? 'CMv4 (real)' :
    s.target_l8_classification === 'indeterminate' ? 'CMv4 (ambiguo)' :
    'CMv4 ?'
  );

  // Chip comparación L2 (color semántico, paleta de la app)
  const l2Comp = s.l2_comparison || '';
  const l2Chip = l2Comp === 'identical'
    ? `<span style="background:var(--green-dim); color:var(--green); border:1px solid var(--green-border); padding:3px 9px; border-radius:10px; font-size:11px; font-weight:600">L2 idéntico al MKV original</span>`
    : l2Comp === 'different'
    ? `<span style="background:var(--orange-dim); color:var(--orange); border:1px solid var(--orange-border); padding:3px 9px; border-radius:10px; font-size:11px; font-weight:600">L2 distinto del MKV original</span>`
    : '';

  // Datos técnicos en formato lista (grid 2-col label/value) — más legible
  // que tabla HTML y sin riesgo de solape. Padding fijo + line-height claro.
  const techRows = [];
  if (s.target_l8_unique_count) {
    techRows.push({ label: 'Combos L8', value: String(s.target_l8_unique_count) });
  }
  if (s.target_l8_neutral_frames_pct != null && s.target_frames_analyzed) {
    const worked = (1.0 - s.target_l8_neutral_frames_pct) * 100;
    techRows.push({ label: 'Frames con trim', value: `${worked.toFixed(0)}%` });
  }
  if (s.target_l8_has_mid_contrast || s.target_l8_has_clip_trim) {
    const extras = [];
    if (s.target_l8_has_mid_contrast) extras.push('target_mid_contrast');
    if (s.target_l8_has_clip_trim) extras.push('clip_trim');
    techRows.push({ label: 'CMv4.0 extras', value: extras.join(' · ') });
  }
  if (s.target_l2_unique_count) {
    techRows.push({ label: 'Combos L2 (bin)', value: String(s.target_l2_unique_count) });
  }
  if (s.source_l2_unique_count) {
    techRows.push({ label: 'Combos L2 (MKV original)', value: String(s.source_l2_unique_count) });
  }
  const techGrid = techRows.length ? `
    <div style="margin-top:14px; display:grid; grid-template-columns:auto 1fr; gap:6px 16px; align-items:baseline">
      ${techRows.map(r => `
        <div style="font-size:11px; color:var(--text-3); font-weight:500">${escHtml(r.label)}</div>
        <div style="font-size:12px; color:var(--text-1); font-family:ui-monospace,SFMono-Regular,Menlo,monospace">${escHtml(r.value)}</div>
      `).join('')}
    </div>` : '';

  // Botones de acción cuando la recomendación es KEEP y el proyecto no está
  // cerrado todavía. Si el proyecto ya está done/archived, no se muestran.
  let actionButtons = '';
  if (isKeep && !projectDone) {
    actionButtons = `
      <div style="display:flex; gap:8px; margin-top:14px; flex-wrap:wrap">
        <button class="btn btn-primary btn-sm" onclick="cmv40AcceptKeep('${pid}')"
          data-tooltip="Cierra el proyecto sin tocar el MKV original. Un reproductor compatible con CMv4.0 (p3i T4 / Sony / LG modernos) hará la conversión al vuelo en runtime.">
          ✓ Mantener MKV actual
        </button>
        <button class="btn btn-ghost btn-sm" onclick="cmv40OverrideRecommendation('${pid}')"
          data-tooltip="Procesa el MKV inyectando el RPU CMv4.0 aunque el bin sea sintético. Resultado equivalente a la conversión al vuelo del reproductor pero quedará archivado como MKV CMv4.0 completo.">
          🔬 Inyectar RPU igualmente
        </button>
      </div>`;
  }

  // Banner verde cuando el proyecto está cerrado — distinguimos por
  // output_workflow para que el usuario sepa qué pasó realmente.
  // Para restore_merge, los niveles transferidos dependen del source_workflow
  // (P7 FEL → [1,2,3,6,8,9,10,11,254]; MEL/P8 → [3,8,9,11,254]).
  let doneBanner = '';
  if (s.output_workflow === 'keep_cmv29') {
    doneBanner = `
      <div style="margin-top:12px; padding:10px 12px; background:var(--green-dim); border:1px solid var(--green-border); border-radius:var(--r-sm); color:var(--text-1); font-size:12px; line-height:1.4">
        <span style="color:var(--green); font-weight:600">✓ Proyecto cerrado — MKV actual mantenido</span>
        — el fichero original quedó intacto. Tu reproductor (p3i T4 / Sony /
        LG modernos) hace la conversión CMv4.0 al vuelo en runtime.
      </div>`;
  } else if (s.output_workflow === 'restore_dropin') {
    doneBanner = `
      <div style="margin-top:12px; padding:10px 12px; background:var(--green-dim); border:1px solid var(--green-border); border-radius:var(--r-sm); color:var(--text-1); font-size:12px; line-height:1.4">
        <span style="color:var(--green); font-weight:600">✓ MKV procesado — RPU CMv4.0 inyectado (rápido)</span>
        — el bin se inyectó directo sobre el MKV original, sin merge
        frame-a-frame. Calidad: ${escHtml(qualityTag)}.
      </div>`;
  } else if (s.output_workflow === 'restore_merge') {
    const mergeLevels = s.source_workflow === 'p7_fel'
      ? '[1, 2, 3, 6, 8, 9, 10, 11, 254]'
      : '[3, 8, 9, 11, 254]';
    const l2Note = s.source_workflow === 'p7_fel'
      ? 'L1/L2/L6 del bin sobrescriben al del source (refinan stats legacy del BD)'
      : 'L1/L2/L5/L6 del MKV original preservados';
    doneBanner = `
      <div style="margin-top:12px; padding:10px 12px; background:var(--green-dim); border:1px solid var(--green-border); border-radius:var(--r-sm); color:var(--text-1); font-size:12px; line-height:1.4">
        <span style="color:var(--green); font-weight:600">✓ MKV procesado — RPU CMv4.0 inyectado (merge selectivo)</span>
        — niveles CMv4.0 ${mergeLevels} transferidos del bin al MKV; ${l2Note}.
        Calidad: ${escHtml(qualityTag)}.
      </div>`;
  } else if (projectDone) {
    // Proyecto done sin output_workflow conocido (sesiones legacy procesadas
    // antes del Bloque 4). Banner genérico.
    doneBanner = `
      <div style="margin-top:12px; padding:10px 12px; background:var(--green-dim); border:1px solid var(--green-border); border-radius:var(--r-sm); color:var(--text-1); font-size:12px; line-height:1.4">
        <span style="color:var(--green); font-weight:600">✓ Proyecto completado</span>
      </div>`;
  }

  return `
    <div class="section-card">
      <div class="section-header">
        <div>
          <div class="section-title">🎯 Análisis y recomendación</div>
          <div class="section-subtitle">Decisión Mantener vs Inyectar (rápido / preserva L2) basada en el análisis del bin: clasificación L8, tier de calidad CMv4 y comparación L2 source vs target</div>
        </div>
      </div>
      <div class="section-body">
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap">
          <span style="padding:6px 12px; border-radius:var(--r-sm); font-weight:700; font-size:13px; ${badgeStyle}">${escHtml(label)}</span>
          <span style="background:var(--surface-2); color:var(--text-2); border:1px solid var(--sep); padding:4px 10px; border-radius:10px; font-size:11px; font-weight:600; font-family:ui-monospace,SFMono-Regular,Menlo,monospace">${escHtml(qualityTag)}</span>
          ${l2Chip}
        </div>
        ${reason ? `<div style="margin-top:12px; color:var(--text-2); font-size:12px; line-height:1.5">${escHtml(reason)}</div>` : ''}
        ${techGrid}
        ${actionButtons}
        ${doneBanner}
      </div>
    </div>`;
}

function cmv40AcceptKeep(pid) {
  showConfirm(
    'Mantener el MKV actual y cerrar el proyecto',
    'El proyecto se cierra como completado sin tocar el MKV original. '
      + 'Tu reproductor (p3i T4 / Sony / LG modernos compatibles con CMv4.0) '
      + 'hará la conversión al vuelo en runtime — el resultado visible es '
      + 'equivalente al de inyectar el RPU, pero sin gastar ~25 min de '
      + 'procesado ni ~50 GB de disco temporal.',
    async () => {
      const data = await apiFetch(`/api/cmv40/${pid}/accept-keep`, { method: 'POST' });
      if (!data) {
        showToast('Error al cerrar el proyecto', 'error');
        return;
      }
      const project = openCMv40Projects.find(p => p.id === pid);
      if (project) {
        _cmv40AssignSession(project, data);
        _updateCMv40Panel(project);
      }
      refreshCMv40Sidebar();
      showToast('✓ Proyecto cerrado — MKV actual mantenido', 'success');
    },
    'Mantener MKV actual',
  );
}

function cmv40OverrideRecommendation(pid) {
  showConfirm(
    'Inyectar RPU CMv4.0 aunque el bin sea sintético',
    'El pipeline va a procesar el MKV (~25 min de Fase A + extracción + remux) '
      + 'aunque el bin del repo no aporte un L8 trabajado real. '
      + 'El resultado visible es equivalente a la conversión al vuelo del '
      + 'reproductor, pero el MKV queda archivado como CMv4.0 "completo" '
      + 'para compatibilidad con otros equipos.',
    async () => {
      const data = await apiFetch(`/api/cmv40/${pid}/override-recommendation`, { method: 'POST' });
      if (!data) {
        showToast('Error al continuar el procesado', 'error');
        return;
      }
      const project = openCMv40Projects.find(p => p.id === pid);
      if (project) {
        _cmv40AssignSession(project, data);
        _updateCMv40Panel(project);
      }
      refreshCMv40Sidebar();
      showToast('🔬 Inyección forzada — el pipeline continuará', 'info');
    },
    'Inyectar RPU CMv4.0',
  );
}

async function _cmv40HydrateTmdbClient(pid) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project || project.session.tmdb_info) return;
  const data = await apiFetch('/api/cmv40/tmdb-lookup', {
    method: 'POST',
    body: JSON.stringify({ source_mkv_name: project.session.source_mkv_name }),
  });
  if (!data || !data.details) return;
  project.session.tmdb_info = data.details;
  _updateCMv40Panel(project);
}

function _cmv40WorkflowLabel(wf) {
  return {
    p7_fel: '🎯 P7 FEL · merge CMv4.0 preservando dual-layer',
    p7_mel: '📀 P7 MEL · descarta EL → P8.1 CMv4.0',
    p8:     '🎬 P8.1 · inject directo → P8.1 CMv4.0',
  }[wf] || wf;
}

async function _cmv40SaveOutputName(pid, newName) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  const trimmed = (newName || '').trim();
  if (!trimmed || trimmed === project.session.output_mkv_name) return;
  const data = await apiFetch(`/api/cmv40/${pid}/rename-output`, {
    method: 'POST',
    body: JSON.stringify({ output_mkv_name: trimmed }),
  });
  if (data) {
    _cmv40AssignSession(project, data);
    showToast('Nombre actualizado', 'success');
  }
}

function _renderCMv40PhaseStrip(s, pid) {
  const container = document.getElementById(`cmv40-phase-strip-${pid}`);
  if (!container) return;
  const phases = [
    { key: 'source_analyzed', icon: '🔍', label: 'Analizar origen' },
    { key: 'target_provided', icon: '🎯', label: 'RPU target' },
    { key: 'extracted',       icon: '✂️', label: 'Extraer BL/EL' },
    { key: 'sync_verified',   icon: '📊', label: 'Verificar sync' },
    { key: 'injected',        icon: '💉', label: 'Inyectar' },
    { key: 'remuxed',         icon: '📦', label: 'Remux' },
    { key: 'validated',       icon: '✅', label: 'Validar' },
  ];
  const currentIdx = CMV40_PHASES_ORDER.indexOf(s.phase);
  const isError = s.phase === 'error';
  container.innerHTML = phases.map((ph, i) => {
    const phaseIdx = CMV40_PHASES_ORDER.indexOf(ph.key);
    let state = 'pending';
    if (phaseIdx < currentIdx) state = 'done';
    else if (phaseIdx === currentIdx) state = isError ? 'error' : 'active';
    return `
      <div class="cmv40-phase-step ${state}">
        <div class="cmv40-phase-circle">${ph.icon}</div>
        <div class="cmv40-phase-label">${ph.label}</div>
      </div>
      ${i < phases.length - 1 ? '<div class="cmv40-phase-conn"></div>' : ''}
    `;
  }).join('');
}

// Definición de todas las fases: inicio + fin
// Una fase está "done" si la phase actual es >= el estado que esa fase PRODUCE
const CMV40_FASES_DEF = [
  { key: 'A', title: 'Fase A — Analizar MKV origen',       produces: 'source_analyzed', startsFrom: 'created',         reset_to: 'created' },
  { key: 'B', title: 'Fase B — Proporcionar RPU target',   produces: 'target_provided', startsFrom: 'source_analyzed', reset_to: 'source_analyzed' },
  { key: 'C', title: 'Fase C — Extraer BL/EL',             produces: 'extracted',       startsFrom: 'target_provided', reset_to: 'target_provided' },
  { key: 'D', title: 'Fase D + E — Verificar y corregir sincronización',  produces: 'sync_verified',   startsFrom: 'extracted',       reset_to: 'extracted' },
  { key: 'F', title: 'Fase F — Inyectar RPU',              produces: 'injected',        startsFrom: 'sync_verified',   reset_to: 'sync_verified' },
  { key: 'G', title: 'Fase G — Remux final',               produces: 'remuxed',         startsFrom: 'injected',        reset_to: 'injected' },
  { key: 'H', title: 'Fase H — Validación final',          produces: 'validated',       startsFrom: 'remuxed',         reset_to: 'remuxed' },
];

function _cmv40PhaseState(sessionPhase, produces, startsFrom) {
  const currentIdx  = CMV40_PHASES_ORDER.indexOf(sessionPhase);
  const producesIdx = CMV40_PHASES_ORDER.indexOf(produces);
  const startsIdx   = CMV40_PHASES_ORDER.indexOf(startsFrom);
  if (currentIdx >= producesIdx) return 'done';
  if (currentIdx >= startsIdx)   return 'active';
  return 'pending';
}

/** Banner ámbar que aparece encima del proyecto cuando Fase B detectó
 *  gates con degradación previsible y pide ACK explícita al usuario.
 *  Contiene la lista de gates fallados + botones "Cambiar target" /
 *  "Continuar igualmente". */
function _cmv40RenderCriticalAckBanner(pid, s) {
  if (!s.awaiting_critical_ack) return '';
  const failures = s.critical_gate_failures || [];
  if (!failures.length) return '';
  const itemsHtml = failures.map(f => {
    const label = ({
      l5_div: 'L5 — letterbox / active area',
      l6_div: 'L6 — MaxCLL/MaxFALL estático',
      l1_div: 'L1 — brillo medio dinámico',
    })[f.gate] || f.gate;
    return `
      <li class="cmv40-ack-item">
        <span class="cmv40-ack-item-name">${escHtml(label)}</span>
        <span class="cmv40-ack-item-why">${escHtml(f.why || '')}</span>
      </li>`;
  }).join('');
  return `
    <div class="section-card cmv40-card-ack-required" style="margin-top:12px">
      <div class="section-body cmv40-ack-body">
        <div class="cmv40-ack-head">
          <span class="cmv40-ack-icon">⚠️</span>
          <div class="cmv40-ack-title-block">
            <div class="cmv40-ack-title">Divergencias detectadas — confirma cómo continuar</div>
            <div class="cmv40-ack-sub">
              El bin pasa los gates estructurales (CMv4.0, L8, frames) pero hay divergencias
              que <strong>Fase D no puede corregir</strong>. Si continúas, el resultado puede
              tener artefactos visibles. Decide cómo seguir:
            </div>
          </div>
        </div>
        <ul class="cmv40-ack-list">${itemsHtml}</ul>
        <div class="cmv40-ack-actions">
          <button class="btn btn-ghost btn-md"
            onclick="_cmv40ChangeTarget('${pid}')"
            data-tooltip="Vuelve a Fase B para escoger otro bin (del repo DoviTools, de carpeta local o extraído de otro MKV propio)">
            ↩ Cambiar target
          </button>
          <button class="btn btn-warning btn-md"
            onclick="_cmv40AcknowledgeCriticalGates('${pid}')"
            data-tooltip="Reconoces que el resultado puede ser degradado y autorizas continuar — Fase D se saltará automáticamente">
            ⚠ Continuar igualmente (resultado degradado)
          </button>
        </div>
      </div>
    </div>`;
}

/** Handler del botón "Continuar igualmente" — POST al endpoint de ack y
 *  refresca el panel para que el auto-pipeline pueda avanzar. */
async function _cmv40AcknowledgeCriticalGates(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}/acknowledge-critical-gates`, { method: 'POST' });
  if (!data) return;
  const project = openCMv40Projects.find(p => p.id === pid);
  if (project) {
    _cmv40AssignSession(project, data);
    // Reset del dedup del orquestador para que en el siguiente tick auto
    // detecte el cambio de estado (awaiting_critical_ack: true → false).
    project._lastAutoFiredFor = null;
    _updateCMv40Panel(project);
  }
  showToast('⚠️ Degradación reconocida — pipeline continúa, Fase D omitida', 'info');
}

/** Handler del botón "Cambiar target" — reset a 'source_analyzed' para
 *  que el usuario seleccione otro bin. Reusa el endpoint reset-to. */
async function _cmv40ChangeTarget(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}/reset-to/source_analyzed`, { method: 'POST' });
  if (!data) return;
  const project = openCMv40Projects.find(p => p.id === pid);
  if (project) {
    _cmv40AssignSession(project, data);
    project._lastAutoFiredFor = null;
    project._autoChaining = false;
    _updateCMv40Panel(project);
  }
  showToast('Listo para escoger otro target — abre la card de Fase B', 'info');
}

function _renderCMv40ActivePhase(project) {
  const s = project.session;
  const pid = project.id;
  const container = document.getElementById(`cmv40-active-phase-${pid}`);
  if (!container) return;

  // Ensure expandedPhases map exists
  if (!project.expandedPhases) {
    project.expandedPhases = {};  // key: fase key, value: true/false
  }

  // Renderizar todas las fases como cards — intercalando los gates entre
  // Fase B y Fase C (trust gates) y entre Fase G y Fase H (validación final).
  const cards = [];
  // Si hay error_message poblado, forzamos que la fase active se renderize
  // siempre expandida — aunque el usuario hubiera colapsado la card antes.
  // Sin esto, el botón "Reintentar" puede quedar oculto bajo el chevrón ▸ y
  // el usuario solo ve el banner rojo + la card "done" de la fase anterior.
  const forceExpandActiveOnError = !!s.error_message;
  CMV40_FASES_DEF.forEach(fase => {
    const state = _cmv40PhaseState(s.phase, fase.produces, fase.startsFrom);
    let isExpanded = project.expandedPhases[fase.key] !== undefined
      ? project.expandedPhases[fase.key]
      : (state === 'active');
    if (forceExpandActiveOnError && state === 'active') isExpanded = true;
    cards.push(_cmv40RenderFaseCard(pid, s, fase, state, isExpanded));
    // Inyectar gate card tras Fase B — trust gates + compatibilidad
    if (fase.key === 'B') {
      const gateBCExpanded = project.expandedPhases['GATE_BC'] !== undefined
        ? project.expandedPhases['GATE_BC']
        : true;  // por defecto expandida — la info es la que el usuario necesita revisar
      cards.push(_cmv40RenderGateCardBC(pid, s, gateBCExpanded));
    }
    // Inyectar gate card tras Fase G — validación final pre-finalizar
    if (fase.key === 'G') {
      const gateGHExpanded = project.expandedPhases['GATE_GH'] !== undefined
        ? project.expandedPhases['GATE_GH']
        : false;
      cards.push(_cmv40RenderGateCardGH(pid, s, gateGHExpanded));
    }
  });

  // Banner de error de la última acción intentada (no bloquea el flujo)
  let errorHtml = '';
  if (s.error_message) {
    // Detectar la fase active actual para ofrecer "Reintentar" directo desde
    // el banner. Sin esto, el usuario tenía que ir a buscar la card de la
    // fase (que podría estar colapsada) para encontrar el botón equivalente.
    const activeFase = CMV40_FASES_DEF.find(f =>
      _cmv40PhaseState(s.phase, f.produces, f.startsFrom) === 'active'
    );
    const retryBtn = activeFase
      ? `<button class="btn btn-warning btn-sm" onclick="_cmv40RetryActivePhase('${pid}','${activeFase.key}')"
            data-tooltip="Vuelve a ejecutar ${escHtml(activeFase.title)}">🔄 Reintentar</button>`
      : '';
    errorHtml = `
      <div class="section-card cmv40-card-error" style="margin-top:12px">
        <div class="section-body" style="display:flex; align-items:center; gap:12px">
          <span style="font-size:20px">⚠️</span>
          <div style="flex:1">
            <div style="font-weight:600; color:var(--red); margin-bottom:2px">Error en la última acción</div>
            <div style="font-size:12px; color:var(--text-2)">${escHtml(s.error_message)}</div>
          </div>
          ${retryBtn}
          <button class="btn btn-ghost btn-sm" onclick="_cmv40ClearError('${pid}')"
            data-tooltip="Descartar este mensaje">✕</button>
        </div>
      </div>`;
  }

  // Si done, card de celebración arriba
  let doneHtml = '';
  if (s.phase === 'done' && !s.archived) {
    doneHtml = `
      <div class="section-card" style="margin-top:16px; background:var(--green-dim); border:1px solid var(--green)">
        <div class="section-body" style="text-align:center; padding:20px">
          <div style="font-size:32px">🎉</div>
          <div style="font-size:15px; font-weight:700; margin-top:4px">MKV CMv4.0 completado</div>
          <div style="font-size:11px; color:var(--text-3); margin-top:4px">${escHtml(s.output_mkv_path || s.output_mkv_name)}</div>
          <div style="margin-top:12px; display:flex; gap:8px; justify-content:center">
            <button class="btn btn-ghost btn-sm" onclick="cmv40Cleanup('${pid}')">🗑️ Limpiar artefactos</button>
          </div>
          <div style="margin-top:8px; font-size:10px; color:var(--text-3)">
            ⚠️ Al limpiar artefactos no podrás rehacer fases (el proyecto pasará a modo solo lectura)
          </div>
        </div>
      </div>`;
  }

  // Si archived, banner de solo lectura
  let archivedHtml = '';
  if (s.archived) {
    archivedHtml = `
      <div class="section-card" style="margin-top:16px; background:var(--surface-2); border:1px solid var(--sep-strong)">
        <div class="section-body" style="display:flex; align-items:center; gap:12px">
          <span style="font-size:22px">🗃️</span>
          <div style="flex:1">
            <div style="font-weight:600">Proyecto archivado — solo lectura</div>
            <div style="font-size:11px; color:var(--text-3); margin-top:2px">
              Los artefactos intermedios se borraron. No se pueden rehacer fases.
              Para iterar de nuevo, crea un proyecto CMv4.0 nuevo desde el mismo MKV origen.
            </div>
          </div>
        </div>
      </div>`;
  }

  // Footer de acciones: botón "Limpiar artefactos" siempre visible mientras
  // el proyecto NO esté archived ni tenga running_phase activo. Permite
  // limpiar tras un fallo de fase sin tener que esperar a que el pipeline
  // termine entero.
  let actionsFooterHtml = '';
  if (!s.archived && !s.running_phase) {
    actionsFooterHtml = `
      <div class="section-card cmv40-actions-footer" style="margin-top:16px">
        <div class="section-body" style="display:flex; align-items:center; gap:12px">
          <span style="font-size:18px; opacity:0.7">🗑️</span>
          <div style="flex:1; min-width:0">
            <div style="font-size:12.5px; font-weight:600">Limpiar artefactos del workdir</div>
            <div style="font-size:11px; color:var(--text-3); margin-top:2px">
              Libera el espacio del workdir intermedio (HEVC, RPU, .mkv.tmp). Disponible siempre que no haya una fase en curso — útil tras un fallo para liberar espacio sin esperar al final del pipeline. <strong>Pasa el proyecto a modo solo lectura.</strong>
            </div>
          </div>
          <button class="btn btn-ghost btn-sm" onclick="cmv40Cleanup('${pid}')"
            style="flex-shrink:0">Limpiar artefactos</button>
        </div>
      </div>`;
  }

  // Banner ACK (gates críticos pendientes) por encima de todo lo demás —
  // pause-point bloqueante: hasta que el usuario decida, el auto-pipeline
  // no avanza. Ver _cmv40MaybeAutoAdvance.
  const ackBannerHtml = _cmv40RenderCriticalAckBanner(pid, s);
  container.innerHTML = ackBannerHtml + errorHtml + archivedHtml + doneHtml + cards.join('') + actionsFooterHtml;

  // Lanzar cargas asíncronas donde aplique. En Fase B el tab default es
  // "Repo DoviTools" — disparamos su loader; los otros tabs (path / MKV)
  // se cargan lazy al hacer click via _cmv40SwitchTargetTab.
  if (_cmv40PhaseState(s.phase, 'target_provided', 'source_analyzed') === 'active') {
    _cmv40LoadRepoForPanel(pid);
  }
  // Chart: cargar si Fase D activa o completada y está expandida.
  // Guards para NO disparar per_frame_data.json on-demand durante auto:
  //   1. Si hay otra fase running — no lanzar otro dovi_tool export pesado
  //      sobre el mismo workdir (race con Fase F inject)
  //   2. Si target_trust_ok — drop-in trusted, nunca se va a usar el chart
  //      (Fase D ya se saltó por gates). Regenerarlo on-demand desperdicia
  //      ~2 min de CPU superponiendose a Fase F.
  const faseDState = _cmv40PhaseState(s.phase, 'sync_verified', 'extracted');
  const dExpanded = project.expandedPhases['D'] !== undefined
    ? project.expandedPhases['D']
    : (faseDState === 'active');
  const shouldLoadChart = (faseDState === 'active' || faseDState === 'done')
                          && dExpanded
                          && !s.running_phase
                          && !s.target_trust_ok;
  if (shouldLoadChart) {
    _loadCMv40SyncChart(project);
  }
}

function _cmv40RenderFaseCard(pid, s, fase, state, isExpanded) {
  // Detectar fases omitidas o modificadas por modo trusted/drop-in
  const skipped = s.phases_skipped || [];
  // Fase C: omitida completamente cuando drop-in + trusted (ambos
  // demux_dual_layer y per_frame_data_skipped marcados).
  const isSkippedC = fase.key === 'C'
                      && skipped.includes('demux_dual_layer')
                      && (skipped.includes('per_frame_data_skipped') || skipped.includes('mux_dual_layer'))
                      && state === 'done';
  // Fase D: omitida cuando el target es trusted y no hay override manual.
  // Usamos la misma condicion que el body (_cmv40FaseDoneBody key==='D') —
  // asi es robusta a reload del proyecto (phases_skipped no se persiste
  // desde el frontend y solo estaria disponible mid-sesion).
  const trustedSkippedD = _cmv40Trust(s);
  const isSkippedD = fase.key === 'D'
                     && (skipped.includes('sync_verification_pause') || trustedSkippedD)
                     && state === 'done';
  // Fase F: en drop-in se salta SOLO el merge, pero el inject SI se ejecuta.
  // NO marcamos la fase como omitida (seria engañoso) — se anotara el "sin
  // merge" en el summary pero el stateIcon sigue siendo ✅ Completado.
  const isDropInF = fase.key === 'F' && skipped.includes('merge_cmv40_transfer') && state === 'done';
  const isSkipped = isSkippedC || isSkippedD;   // solo C y D son "totalmente omitidas"

  const stateIcon = isSkipped ? '⏭️'
                  : state === 'done' ? '✅'
                  : state === 'active' ? '▶️' : '🔒';
  const stateLabel = isSkippedC ? 'Omitida — drop-in: no hace falta demux ni per-frame data'
                   : isSkippedD ? 'Omitida — target trusted: sync validado por gates'
                   : isDropInF  ? 'Ejecutada en modo drop-in (inject directo sin merge previo)'
                   : state === 'done' ? 'Completado'
                   : state === 'active' ? 'En curso' : 'Pendiente';

  // Resumen cuando está done
  let summary = '';
  if (state === 'done') {
    summary = _cmv40FaseSummary(fase.key, s);
  }

  // Body según estado
  let body = '';
  if (isExpanded) {
    if (state === 'active') {
      body = _cmv40FaseBody(fase.key, pid, s);
    } else if (state === 'done') {
      body = `
        <div class="section-body">
          ${_cmv40FaseDoneBody(fase.key, pid, s)}
          ${s.archived ? '' : `
          <div style="margin-top:12px; padding-top:12px; border-top:1px solid var(--sep)">
            <button class="btn btn-danger btn-sm" onclick="_cmv40Redo('${pid}','${fase.reset_to}','${fase.key}')"
              data-tooltip="Vuelve a esta fase. Las fases posteriores se invalidarán.">🔄 Rehacer esta fase</button>
          </div>`}
        </div>`;
    } else {
      body = `<div class="section-body"><div style="font-size:12px; color:var(--text-3)">🔒 Completa las fases anteriores para activar esta.</div></div>`;
    }
  }

  const extraCls = isSkipped ? ' cmv40-fase-skipped' : '';
  // Subtitulo: cuando la fase se omite o se ejecuta en drop-in preferimos
  // el stateLabel explicito (es mas claro que el summary auto-generado que
  // puede sugerir trabajo que realmente no se hizo).
  const preferStateLabel = isSkipped || isDropInF;
  const subtitle = (!preferStateLabel && summary) ? summary : stateLabel;
  // Sufijo diferenciado por razón de omisión — antes era "(omitida)" genérico
  // para Fase C y D sin diferenciar el porqué (C por drop-in, D por trust).
  const skippedSuffix = isSkippedC
    ? '(omitida · drop-in)'
    : isSkippedD
    ? (s.user_acknowledged_degradation
        ? '(omitida · usuario reconoció degradación)'
        : '(omitida · trust gates OK)')
    : '(omitida)';
  const titleSuffix = isSkipped
    ? ` <span style="color:var(--text-3); font-weight:400; font-size:11px">${skippedSuffix}</span>`
    : isDropInF
    ? ' <span style="color:#8a4a00; font-weight:500; font-size:11px">(drop-in)</span>'
    : '';
  return `
    <div class="section-card cmv40-fase-card cmv40-fase-${state}${extraCls}" style="margin-top:12px" data-fase-key="${fase.key}">
      <div class="section-header cmv40-fase-header" onclick="_cmv40TogglePhase('${pid}','${fase.key}')">
        <div class="cmv40-fase-state-icon">${stateIcon}</div>
        <div style="flex:1">
          <div class="section-title">${escHtml(fase.title)}${titleSuffix}</div>
          <div class="section-subtitle">${subtitle}</div>
        </div>
        <div class="cmv40-fase-chevron">${isExpanded ? '▾' : '▸'}</div>
      </div>
      ${body}
    </div>`;
}

/* ───────── Gate cards (pseudo-fases) ──────────────────────────────────
 * No son fases ejecutables: son puntos de decisión que la app evalúa
 * automáticamente a partir de datos ya capturados. Por eso no tienen
 * botón "rehacer" — se recalculan al re-ejecutar la fase que las alimenta
 * (Fase B para el gate de trust, Fase G para la validación final).
 * Visualmente usan el esquema azul-dashed igual que los pills del manual.
 */

/** Genera el HTML de una fila de gate con estado coloreado + explicación. */
function _cmv40GateRowHtml(status, title, result, explanation) {
  // status: 'ok' | 'warn' | 'ko' | 'pending'
  const icon = { ok: '✓', warn: '⚠', ko: '✗', pending: '○' }[status] || '·';
  const color = { ok: '#0e6b2a', warn: '#8a4a00', ko: '#b10b0b', pending: 'var(--text-3)' }[status] || 'var(--text-3)';
  const bg    = { ok: 'rgba(52,199,89,0.10)', warn: 'rgba(255,149,0,0.10)', ko: 'rgba(255,59,48,0.10)', pending: 'rgba(0,0,0,0.03)' }[status] || 'transparent';
  return `
    <div style="display:grid; grid-template-columns:24px 1fr; gap:10px; padding:10px 12px; background:${bg}; border-radius:6px; margin-bottom:6px">
      <div style="font-size:16px; font-weight:700; color:${color}; text-align:center">${icon}</div>
      <div>
        <div style="display:flex; gap:8px; align-items:baseline; flex-wrap:wrap">
          <span style="font-size:12px; font-weight:700; color:var(--text-1)">${escHtml(title)}</span>
          <span style="font-size:11px; color:${color}; font-weight:600">${escHtml(result)}</span>
        </div>
        <div style="font-size:11px; color:var(--text-2); line-height:1.5; margin-top:2px">${escHtml(explanation)}</div>
      </div>
    </div>`;
}

function _cmv40RenderGateCardBC(pid, s, isExpanded) {
  const curIdx   = CMV40_PHASES_ORDER.indexOf(s.phase);
  const bIdx     = CMV40_PHASES_ORDER.indexOf('target_provided');
  const hasData  = curIdx >= bIdx && (s.target_dv_info || s.target_trust_gates);
  const compatErr= !!s.compat_warning;
  const trustOk  = s.target_trust_ok === true;

  let overallIcon, overallLabel;
  if (compatErr) { overallIcon = '⛔'; overallLabel = 'Abortada · combinación incompatible'; }
  else if (!hasData) { overallIcon = '🔒'; overallLabel = 'Pendiente — se evalúa al cerrar Fase B'; }
  else if (trustOk) { overallIcon = '✅'; overallLabel = 'Trusted · todos los críticos pasan'; }
  else { overallIcon = '⚠️'; overallLabel = 'Sin trust automático · flujo completo manual'; }

  // Resumen en el header
  let summary;
  if (compatErr) summary = `Combinación ${s.source_workflow || '?'} + ${s.target_type || '?'} incompatible`;
  else if (!hasData) summary = 'Se evalúan al tener target — comparación con el RPU del Blu-ray';
  else if (trustOk) summary = 'Bin pre-validado: se saltan fases manuales (D/E)';
  else summary = 'Se ejecuta el flujo completo con revisión visual en Fase D';

  let body = '';
  if (isExpanded) {
    const rows = [];
    if (compatErr) {
      rows.push(_cmv40GateRowHtml('ko', 'Compatibilidad estructural',
        'abortada',
        s.compat_warning || 'Source y target estructuralmente incompatibles — la inyección produciría un MKV inválido.'));
    } else if (hasData) {
      const g = s.target_trust_gates || {};
      // Frames
      if (g.frames) {
        const ok = g.frames.ok;
        rows.push(_cmv40GateRowHtml(ok ? 'ok' : 'ko',
          'Número de frames',
          ok ? `coinciden · ${(g.frames.bd || 0).toLocaleString()} frames`
             : `${(g.frames.bd || 0).toLocaleString()} ≠ ${(g.frames.target || 0).toLocaleString()}`,
          ok
            ? 'Source y target tienen exactamente el mismo número de frames — condición crítica para que el RPU target se inyecte alineado escena a escena.'
            : 'Diferencia de frames ≠ 0. Suele indicar que el bin target es para otra edición (theatrical vs extended, streaming recortado). Requiere sync manual en Fase D/E o buscar el bin correcto.'));
      }
      // CM version
      if (g.cm_version) {
        const ok = g.cm_version.ok;
        rows.push(_cmv40GateRowHtml(ok ? 'ok' : 'ko',
          'CM version del target',
          ok ? `CM ${g.cm_version.value}` : `CM ${g.cm_version.value || '?'}`,
          ok
            ? 'El target está firmado como CMv4.0 — tiene los niveles nuevos (L3/L8-L11) que justifican el upgrade.'
            : 'El target no es CMv4.0. Sin CMv4.0 no hay upgrade posible — elige otro bin.'));
      }
      // L8
      if (g.has_l8) {
        const ok = g.has_l8.ok;
        rows.push(_cmv40GateRowHtml(ok ? 'ok' : 'ko',
          'Presencia de L8',
          ok ? 'L8 detectado' : 'L8 ausente',
          ok
            ? 'El bin contiene trims L8 auténticos — el nivel que aporta el tone-mapping fino de CMv4.0.'
            : 'Bin "CMv4.0 vacío" sin L8. No añade valor sobre el v2.9 original — rechazado.'));
      }
      // L5 divergence — usa el muestreo zoneado si se ejecutó (Fase B
      // refina con dovi_tool export cuando el static check supera el umbral).
      // En ese caso el `why` dinámico ya describe matches/mismatches por
      // zona; el resultado visual refleja el veredicto final (warn/ack/ok).
      if (g.l5_div) {
        const l5 = g.l5_div;
        const px = l5.px_max || 0;
        const sampled = l5.sampled_method === 'per_frame_zoned_24';
        let st, result, explanation;
        if (sampled) {
          const total = l5.sampled_total || 0;
          const matches = l5.sampled_matches || 0;
          const bodyCov = Math.round((l5.sampled_body_coverage || 0) * 100);
          const zm = l5.sampled_zone_mismatches || {};
          const sev = l5.severity || (l5.ok ? 'warn' : 'ack_required');
          // Estado visual: ok si todos coinciden, warn si pasa con muestreo,
          // ko si el muestreo confirmó divergencia legítima (ack_required).
          if (sev === 'ack_required') st = 'ko';
          else if (matches === total) st = 'ok';
          else st = 'warn';
          // Etiqueta corta: usa la métrica del muestreo en vez del Δpx
          // estático (que en variable L5 confunde).
          if (matches === total) {
            result = `${total}/${total} muestras coinciden`;
          } else if ((zm.body || 0) === 0) {
            result = `${matches}/${total} · cuerpo OK`;
          } else if (sev === 'ack_required') {
            result = `cuerpo solo ${bodyCov}% · ack`;
          } else {
            result = `cuerpo ${bodyCov}% · ${matches}/${total}`;
          }
          // El `why` ya trae el desglose completo intro/body/outro generado
          // en backend (_refine_l5_gate_with_sampling).
          explanation = l5.why || `Muestreo per-frame: ${matches}/${total} coinciden, cuerpo principal ${bodyCov}%.`;
        } else {
          // Sin muestreo: gate estático puro (≤30 px). Solo aplica cuando
          // el static check no necesitó refinamiento, así que st nunca es ko aquí.
          st = px <= 5 ? 'ok' : 'warn';
          result = px <= 5 ? `div ≤ 5 px` : `div ${px} px · warn`;
          explanation = st === 'ok'
            ? 'Los offsets de letterbox del target están a ≤5 px de los del BD — misma edición o recorte equivalente.'
            : 'Divergencia moderada del active area (5-30 px). Puede ser la misma edición con recorte ligeramente distinto. La app avanza pero conviene revisar visualmente.';
        }
        rows.push(_cmv40GateRowHtml(st, 'L5 — letterbox (active area)', result, explanation));
      }
      // L6 divergence
      if (g.l6_div) {
        const nits = Math.abs(g.l6_div.nits_diff || 0);
        const st = nits <= 50 ? 'ok' : 'warn';
        rows.push(_cmv40GateRowHtml(st,
          'L6 — MaxCLL/MaxFALL estático',
          `Δ ${g.l6_div.nits_diff} nits`,
          st === 'ok'
            ? 'La metadata HDR estática del target coincide (≤50 nits de diferencia) con la del BD.'
            : `Diferencia > 50 nits en L6. Sugiere que el target viene de un mastering con brillo global distinto. No bloquea pero el carácter de la imagen puede cambiar.`));
      }
      // L1 divergence
      if (g.l1_div) {
        const pct = Math.abs(g.l1_div.pct_diff || 0);
        const st = pct <= 5 ? 'ok' : 'warn';
        rows.push(_cmv40GateRowHtml(st,
          'L1 — MaxCLL dinámico por escena',
          `Δ ${g.l1_div.pct_diff}%`,
          st === 'ok'
            ? 'El promedio de brillo escena-a-escena coincide (≤5% de diferencia) — el grading es comparable.'
            : 'Diferencia > 5% en el promedio de brillo por escena. Sugiere color grading distinto entre el target y el BD. Avanza pero el resultado puede verse diferente al original.'));
      }
    }
    body = `
      <div class="section-body">
        <div style="font-size:12px; color:var(--text-2); line-height:1.5; margin-bottom:10px">
          <strong>Qué se valida aquí:</strong> al cerrar Fase B, la app compara automáticamente el RPU target con el RPU del Blu-ray para decidir si puede saltar las fases manuales (D/E) o si hace falta revisión visual. Las validaciones críticas además aseguran que la combinación source × target no producirá un MKV roto.
        </div>
        ${rows.join('') || '<div style="font-size:12px; color:var(--text-3); font-style:italic">Aún sin datos — completa Fase B primero.</div>'}
      </div>`;
  }

  return `
    <div class="section-card cmv40-gate-card" style="margin-top:12px; border-left:3px solid rgba(0,122,255,0.55)">
      <div class="section-header cmv40-fase-header" onclick="_cmv40TogglePhase('${pid}','GATE_BC')" style="cursor:pointer">
        <div class="cmv40-fase-state-icon" style="font-size:20px">${overallIcon}</div>
        <div style="flex:1">
          <div class="section-title" style="color:#0a5cab">🛡️ Validaciones — trust gates + compatibilidad</div>
          <div class="section-subtitle">${escHtml(overallLabel)} · ${escHtml(summary)}</div>
        </div>
        <div class="cmv40-fase-chevron">${isExpanded ? '▾' : '▸'}</div>
      </div>
      ${body}
    </div>`;
}

function _cmv40RenderGateCardGH(pid, s, isExpanded) {
  const curIdx = CMV40_PHASES_ORDER.indexOf(s.phase);
  const remuxedIdx = CMV40_PHASES_ORDER.indexOf('remuxed');
  const validatedIdx = CMV40_PHASES_ORDER.indexOf('validated');
  const state = curIdx < remuxedIdx ? 'pending'
             : curIdx === remuxedIdx ? 'running'
             : curIdx >= validatedIdx ? 'done'
             : 'pending';

  let overallIcon, overallLabel, summary;
  if (state === 'done') {
    overallIcon = '✅';
    overallLabel = 'Validación final OK';
    summary = 'El MKV contiene CMv4.0, el profile es correcto y el frame count coincide';
  } else if (state === 'running') {
    overallIcon = '⏳';
    overallLabel = 'Validación en curso…';
    summary = 'Verificando profile + CM v4.0 + frame count del HEVC pre-mux';
  } else {
    overallIcon = '🔒';
    overallLabel = 'Pendiente';
    summary = 'Se ejecuta tras completar Fase G (remux)';
  }

  let body = '';
  if (isExpanded) {
    const rows = [];
    // Profile
    const targetProfile = s.source_dv_info?.profile || '?';
    rows.push(_cmv40GateRowHtml(state === 'done' ? 'ok' : 'pending',
      'Profile del HEVC resultante',
      state === 'done' ? `Profile ${targetProfile}` : '—',
      state === 'done'
        ? 'El MKV final tiene el profile DV esperado según el workflow elegido (P7 FEL si source era FEL, P8.1 single-layer si source era MEL/P8).'
        : 'Se verifica que el profile coincide con el esperado al completar Fase G.'));
    // CM version
    rows.push(_cmv40GateRowHtml(state === 'done' ? 'ok' : 'pending',
      'CM version del MKV',
      state === 'done' ? 'CM v4.0 confirmado' : '—',
      state === 'done'
        ? 'dovi_tool extract-rpu + info sobre el HEVC pre-mux confirma CMv4.0 en el RPU del MKV resultante.'
        : 'Se verifica que el RPU del MKV final reporta CMv4.0.'));
    // Frame count
    rows.push(_cmv40GateRowHtml(state === 'done' ? 'ok' : 'pending',
      'Frame count',
      state === 'done' ? `${(s.source_frame_count || 0).toLocaleString()} frames` : '—',
      state === 'done'
        ? 'El número de frames del MKV resultante coincide con el del Blu-ray origen — sin inserciones ni recortes accidentales.'
        : 'Se compara frame count del resultado contra el del source.'));
    // Estructura MKV
    rows.push(_cmv40GateRowHtml(state === 'done' ? 'ok' : 'pending',
      'Estructura Matroska',
      state === 'done' ? 'MKV válido · mkvmerge -J OK' : '—',
      state === 'done'
        ? 'mkvmerge -J lee el fichero sin errores: audio, subs, capítulos y pista de vídeo con RPU NAL units correctamente ensamblados.'
        : 'Se verifica que el contenedor MKV es estructuralmente correcto.'));

    body = `
      <div class="section-body">
        <div style="font-size:12px; color:var(--text-2); line-height:1.5; margin-bottom:10px">
          <strong>Qué se valida aquí:</strong> antes de mover el MKV al directorio de salida, la app verifica que el resultado es estructuralmente correcto y que el upgrade a CMv4.0 se ha materializado en el fichero. Si algo falla, el proyecto queda en error y puedes rehacer desde la fase que prefieras.
        </div>
        ${rows.join('')}
      </div>`;
  }

  return `
    <div class="section-card cmv40-gate-card" style="margin-top:12px; border-left:3px solid rgba(0,122,255,0.55)">
      <div class="section-header cmv40-fase-header" onclick="_cmv40TogglePhase('${pid}','GATE_GH')" style="cursor:pointer">
        <div class="cmv40-fase-state-icon" style="font-size:20px">${overallIcon}</div>
        <div style="flex:1">
          <div class="section-title" style="color:#0a5cab">🛡️ Validación final pre-finalizar</div>
          <div class="section-subtitle">${escHtml(overallLabel)} · ${escHtml(summary)}</div>
        </div>
        <div class="cmv40-fase-chevron">${isExpanded ? '▾' : '▸'}</div>
      </div>
      ${body}
    </div>`;
}

function _cmv40TogglePhase(pid, key) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  if (!project.expandedPhases) project.expandedPhases = {};
  // Gates son pseudo-fases — toggle directo sin consultar CMV40_FASES_DEF
  if (key === 'GATE_BC' || key === 'GATE_GH') {
    const current = project.expandedPhases[key] !== undefined
      ? project.expandedPhases[key]
      : (key === 'GATE_BC');   // BC abierto por defecto, GH cerrado
    project.expandedPhases[key] = !current;
    _updateCMv40Panel(project);
    return;
  }
  const fase = CMV40_FASES_DEF.find(f => f.key === key);
  const state = _cmv40PhaseState(project.session.phase, fase.produces, fase.startsFrom);
  const current = project.expandedPhases[key] !== undefined
    ? project.expandedPhases[key]
    : (state === 'active');
  project.expandedPhases[key] = !current;
  _updateCMv40Panel(project);
}

// Label amigable del target_type + panel de gates con resultado visual
const _CMV40_TARGET_TYPE_LABELS = {
  'generic':               { icon: '🔧', label: 'Target genérico',             desc: 'Flujo completo: merge CMv4.0 + revisión visual en Fase D' },
  'trusted_p8_source':     { icon: '📦', label: 'Target P8 + CMv4.0 (trusted)', desc: 'Bin pre-validado (rama B): skip Fase D si gates OK' },
  'trusted_p7_fel_final':  { icon: '🎯', label: 'Target P7 FEL CMv4.0 final',   desc: 'Drop-in: skip merge en Fase F + skip Fase D si gates OK' },
  'trusted_p7_mel_final':  { icon: '🎯', label: 'Target P7 MEL CMv4.0 final',   desc: 'Drop-in MEL: skip Fase D si gates OK' },
  'incompatible':          { icon: '❌', label: 'Target incompatible',          desc: 'Sin CMv4.0 — no sirve como fuente de transfer' },
};

function _cmv40RenderTrustPanel(s) {
  const tt = s.target_type || 'generic';
  const meta = _CMV40_TARGET_TYPE_LABELS[tt] || _CMV40_TARGET_TYPE_LABELS.generic;
  const gates = s.target_trust_gates || {};
  const trustOk = s.target_trust_ok === true;
  const isTrusted = tt !== 'generic' && tt !== 'incompatible';
  const isIncompat = tt === 'incompatible';

  let cls = 'generic';
  if (isIncompat) cls = 'ko';
  else if (isTrusted && trustOk) cls = 'ok';
  else if (isTrusted && !trustOk) cls = 'warn';

  // Renderizar cada gate con status visual
  const gateItems = [];
  const pushGate = (key, okText, failText, detail) => {
    const g = gates[key];
    if (!g) return;
    const gClass = g.ok ? 'pass' : (g.critical ? 'fail' : 'soft');
    const txt = g.ok ? okText : failText;
    gateItems.push(`<span class="cmv40-trust-gate ${gClass}" data-tooltip="${escHtml(detail)}">
      ${g.ok ? '✓' : '✗'} ${escHtml(txt)}
    </span>`);
  };
  if (gates.frames) {
    pushGate('frames',
      `frames ${gates.frames.bd?.toLocaleString() || '?'}`,
      `frames ${gates.frames.bd?.toLocaleString()} ≠ ${gates.frames.target?.toLocaleString()}`,
      'Frame count del BD vs target — crítico'
    );
  }
  if (gates.cm_version) {
    pushGate('cm_version',
      `CM ${gates.cm_version.value}`,
      `CM ${gates.cm_version.value || '?'}`,
      'Debe ser v4.0 para ser fuente de transfer — crítico'
    );
  }
  if (gates.has_l8) {
    pushGate('has_l8', 'L8 presente', 'sin L8',
      'L8 = trims CMv4.0 — sin L8 no hay transfer útil'
    );
  }
  if (gates.l5_div) {
    const l5 = gates.l5_div;
    // Si el muestreo per-frame con zonas se ejecutó, prioriza esa narrativa
    // (matches por zona) sobre el Δpx estático del summary — en pelis con
    // L5 variable este último engaña.
    const sampled = l5.sampled_method === 'per_frame_zoned_24';
    let okText, failText, tip;
    if (sampled) {
      const total = l5.sampled_total || 0;
      const matches = l5.sampled_matches || 0;
      const bodyCov = Math.round((l5.sampled_body_coverage || 0) * 100);
      const zm = l5.sampled_zone_mismatches || {};
      // Etiqueta corta con la métrica clave: matches del cuerpo principal
      if (matches === total) {
        okText = `L5 ${matches}/${total} idéntico`;
      } else if ((zm.body || 0) === 0) {
        okText = `L5 cuerpo OK · ${matches}/${total}`;
      } else if (l5.ok) {
        okText = `L5 cuerpo ${bodyCov}% · ${matches}/${total}`;
      } else {
        okText = `L5 cuerpo solo ${bodyCov}%`;
      }
      failText = okText;
      tip = l5.why || `Muestreo per-frame con zonas (intro/body/outro).`;
    } else {
      okText = `L5 div ${l5.px_max}px`;
      failText = `L5 div ${l5.px_max}px (>30)`;
      tip = l5.why || `Divergencia L5 (active area). ≤5 ok · 5-30 warn · >30 aborta — posible edición distinta del disco`;
    }
    pushGate('l5_div', okText, failText, tip);
  }
  if (gates.l6_div) {
    pushGate('l6_div',
      `L6 Δ${gates.l6_div.nits_diff}n`,
      `L6 Δ${gates.l6_div.nits_diff}n (>50)`,
      'Divergencia L6 MaxCLL — soft warn si >50 nits'
    );
  }
  if (gates.l1_div) {
    pushGate('l1_div',
      `L1 Δ${gates.l1_div.pct_diff}%`,
      `L1 Δ${gates.l1_div.pct_diff}% (>5%)`,
      'Divergencia L1 MaxCLL en % — soft warn si >5%'
    );
  }

  const statusTxt = isIncompat
    ? 'Incompatible — no sirve como target'
    : isTrusted && trustOk ? 'TRUSTED — se saltarán pasos manuales'
    : isTrusted && !trustOk ? 'Trust NO aprobado — flujo completo con revisión manual'
    : 'Flujo estándar con merge + revisión';

  return `
    <div class="cmv40-trust-panel ${cls}" style="margin-top:10px">
      <div class="cmv40-trust-header">
        <span>${meta.icon}</span>
        <span>${escHtml(meta.label)}</span>
        <span class="cmv40-trust-status">${escHtml(statusTxt)}</span>
      </div>
      <div class="cmv40-trust-desc">${escHtml(meta.desc)}</div>
      ${gateItems.length ? `<div class="cmv40-trust-gates">${gateItems.join('')}</div>` : ''}
    </div>`;
}

function _cmv40FaseSummary(key, s) {
  const arts = s.artifacts || {};
  if (key === 'A' && s.source_dv_info) {
    const d = s.source_dv_info;
    return `Profile ${d.profile}${d.el_type ? ` (${d.el_type})` : ''} · CM ${d.cm_version} · ${s.source_frame_count.toLocaleString()} frames`;
  }
  if (key === 'B' && s.target_dv_info) {
    const d = s.target_dv_info;
    return `CM ${d.cm_version} · ${s.target_frame_count.toLocaleString()} frames (Δ ${s.sync_delta > 0 ? '+' : ''}${s.sync_delta})`;
  }
  if (key === 'C') {
    const sizes = ['BL.hevc', 'EL.hevc', 'per_frame_data.json'].map(n => arts[n] || 0);
    const total = sizes.reduce((a, b) => a + b, 0);
    return total > 0 ? `BL.hevc, EL.hevc y per_frame_data (${_fmtBytes(total)} total)` : 'BL.hevc, EL.hevc y datos per-frame generados';
  }
  if (key === 'D') {
    const trustedSkipped = _cmv40Trust(s);
    if (trustedSkipped) return 'Omitida — target trusted: sync validado por gates';
    return s.sync_config ? `Corrección aplicada (Δ = ${s.sync_delta})` : 'Sincronización verificada (Δ = 0)';
  }
  if (key === 'F') {
    // En drop-in FEL el artefacto es source_injected.hevc (BL+EL intactos);
    // en merge clasico es EL_injected.hevc (solo EL). Preferimos el que exista.
    const dropIn = arts['source_injected.hevc'];
    const merge  = arts['EL_injected.hevc'];
    if (dropIn) return `source_injected.hevc generado (${_fmtBytes(dropIn)}, drop-in)`;
    if (merge)  return `EL_injected.hevc generado (${_fmtBytes(merge)})`;
    return 'HEVC con RPU inyectado generado';
  }
  if (key === 'G') {
    // El MKV se escribe en /mnt/output (fuera del workdir) por lo que no sale
    // del scan de artifacts. Mostramos el nombre directo del session.
    const name = s.output_mkv_name || '';
    return name ? `MKV remuxado: ${name} (pre-validación)` : 'MKV remuxado (pre-validación)';
  }
  if (key === 'H') return s.output_mkv_path ? `Movido a: ${s.output_mkv_path}` : 'Validado';
  return '';
}

function _cmv40FaseBody(key, pid, s) {
  if (key === 'A') return _cmv40FaseABody(pid, s);
  if (key === 'B') return _cmv40FaseBBody(pid, s);
  if (key === 'C') return _cmv40FaseCBody(pid, s);
  if (key === 'D') return _cmv40FaseDBody(pid, s);
  if (key === 'F') return _cmv40FaseFBody(pid, s);
  if (key === 'G') return _cmv40FaseGBody(pid, s);
  if (key === 'H') return _cmv40FaseHBody(pid, s);
  return '';
}

function _cmv40FaseDoneBody(key, pid, s) {
  // Contenido "modo lectura" cuando la fase está completada
  if (key === 'A' && s.source_dv_info) {
    const d = s.source_dv_info;
    return `
      <div style="font-size:12px; line-height:1.8">
        <div><span style="color:var(--text-3)">Profile:</span> ${d.profile}${d.el_type ? ` (${d.el_type})` : ''}</div>
        <div><span style="color:var(--text-3)">CM version:</span> ${d.cm_version}</div>
        <div><span style="color:var(--text-3)">Frames:</span> ${s.source_frame_count.toLocaleString()}</div>
        ${d.has_l1 ? '<div><span style="color:var(--text-3)">Metadata:</span> L1 L2 L5 L6</div>' : ''}
      </div>`;
  }
  if (key === 'B' && s.target_dv_info) {
    const d = s.target_dv_info;
    const srcType = s.target_rpu_source === 'drive' ? 'Repo DoviTools'
                   : s.target_rpu_source === 'mkv' ? 'Extraído de otro MKV'
                   : 'Carpeta NAS';
    const shortHash = s.target_rpu_sha256 ? s.target_rpu_sha256.slice(0, 12) : '';
    const hashLine = shortHash
      ? `<div><span style="color:var(--text-3)">SHA-256:</span> <code title="${escHtml(s.target_rpu_sha256)}" style="font-size:11px">${shortHash}…</code></div>`
      : '';
    // NO incluimos _cmv40RenderTrustPanel aqui — los gates tienen su propia
    // tarjeta dedicada (🛡️ Validaciones) que aparece justo debajo de Fase B.
    // Mostrarlo aqui ademas duplicaba la informacion.
    return `
      <div style="font-size:12px; line-height:1.8">
        <div><span style="color:var(--text-3)">Fuente:</span> ${srcType}</div>
        <div><span style="color:var(--text-3)">Path:</span> <code>${escHtml(s.target_rpu_path || '—')}</code></div>
        ${hashLine}
        <div><span style="color:var(--text-3)">CM version:</span> ${d.cm_version}</div>
        <div><span style="color:var(--text-3)">Frames:</span> ${s.target_frame_count.toLocaleString()}</div>
        <div><span style="color:var(--text-3)">Δ vs origen:</span> <b style="color:${s.sync_delta === 0 ? 'var(--green)' : 'var(--orange)'}">${s.sync_delta > 0 ? '+' : ''}${s.sync_delta} frames</b></div>
        <div style="margin-top:8px; font-size:11px; color:var(--text-3); font-style:italic">💡 Los resultados de los trust gates se muestran en la tarjeta 🛡️ Validaciones de abajo.</div>
      </div>`;
  }
  // Fase D completada — dos casuísticas:
  //   (1) target trusted + auto → NUNCA se generó per_frame_data.json →
  //       mostrar banner "omitida" en vez de canvas vacío (que se veía negro).
  //   (2) revisión visual real (non-trusted, o trust_override=force_interactive)
  //       → el plot existe; mostrar chart + stats + controles de navegación
  //       (zoom + frame range) en modo read-only.
  if (key === 'D') {
    const trustedSkipped = _cmv40Trust(s);
    if (trustedSkipped) {
      // Sin trust panel aqui — la tarjeta 🛡️ Validaciones arriba ya lo muestra.
      return `
        <div class="banner success" style="margin-bottom:10px">
          <span class="banner-icon">✓</span>
          <span>Fase D omitida — el bin target pasó los trust gates (frames, L5, L6, L8) y no se generó <code>per_frame_data.json</code>. Sin revisión visual necesaria en el auto-pipeline.</span>
        </div>
        <div style="font-size:11px; color:var(--text-3); font-style:italic; margin-top:6px">💡 Los resultados de los gates están en la tarjeta 🛡️ Validaciones justo tras Fase B.</div>`;
    }
    const syncConfigHtml = s.sync_config
      ? `<div style="margin-bottom:10px; font-size:12px">
          <span style="color:var(--text-3)">Corrección aplicada:</span>
          <pre style="margin-top:6px; font-size:11px; background:var(--surface-2); padding:8px; border-radius:4px">${escHtml(JSON.stringify(s.sync_config, null, 2))}</pre>
        </div>`
      : '<div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Sincronización confirmada sin corrección.</div>';
    return `
      ${syncConfigHtml}
      <div style="font-size:11px; color:var(--text-3); margin-bottom:8px">
        Navegación por el gráfico en solo lectura — la corrección ya está aplicada.
      </div>
      <div id="cmv40-sync-stats-${pid}" class="cmv40-sync-stats"></div>
      <div id="cmv40-chart-wrap-${pid}" class="cmv40-chart-wrap">
        <canvas id="cmv40-chart-${pid}" width="1000" height="280"></canvas>
        <div class="cmv40-chart-tooltip" id="cmv40-chart-tooltip-${pid}" style="display:none"></div>
      </div>
      <div class="cmv40-sync-controls" id="cmv40-sync-controls-${pid}"></div>
      <div id="cmv40-confidence-${pid}"></div>`;
  }
  if (key === 'H' && s.output_mkv_path) {
    return `<div style="font-size:12px"><span style="color:var(--text-3)">MKV final:</span> <code>${escHtml(s.output_mkv_path)}</code></div>`;
  }
  // Fase C: mostrar artefactos generados (BL.hevc, EL.hevc, per_frame_data.json)
  if (key === 'C') {
    return _cmv40ArtifactsBody(s, ['BL.hevc', 'EL.hevc', 'per_frame_data.json']);
  }
  // Fase F: drop-in FEL genera source_injected.hevc (BL+EL intactos);
  // merge clasico genera EL_injected.hevc (solo EL). Tras validacion exitosa
  // (Fase H) el pipeline borra ambos ficheros — ya no son necesarios, el MKV
  // final los contiene. Distinguimos 3 casos:
  //   (a) artifact existe: mostramos size
  //   (b) artifact no existe Y pipeline ya termino: mensaje de cleanup ok
  //   (c) artifact no existe y pipeline a medias: "no encontrado" (bug)
  if (key === 'F') {
    const arts = s.artifacts || {};
    const hasDropIn = arts['source_injected.hevc'] !== undefined;
    const hasMerge  = arts['EL_injected.hevc']     !== undefined;
    if (hasDropIn) return _cmv40ArtifactsBody(s, ['source_injected.hevc']);
    if (hasMerge)  return _cmv40ArtifactsBody(s, ['EL_injected.hevc']);
    // Nada encontrado: decidimos segun fase global
    const cleaned = ['validated', 'done'].includes(s.phase) || s.archived;
    const wf = (s.workflow || s.source_workflow || '').toLowerCase();
    const phSkipped = s.phases_skipped || [];
    const isDropIn = phSkipped.includes('merge_cmv40_transfer') || wf === 'p7_fel';
    const name = isDropIn ? 'source_injected.hevc' : 'EL_injected.hevc';
    if (cleaned) {
      return `
        <div style="font-size:12px">
          <div style="color:var(--text-3); margin-bottom:6px">Artefactos generados:</div>
          <div style="display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px dashed var(--sep); opacity:0.7">
            <code style="font-size:11px">${escHtml(name)}</code>
            <span style="font-size:11px; color:var(--text-3)">consumido tras validación</span>
          </div>
          <div style="font-size:11px; color:var(--text-3); margin-top:6px; line-height:1.4">
            El HEVC intermedio se borra automáticamente en Fase H una vez validado el MKV final — el resultado vive ahora en <code>/mnt/output</code>.
          </div>
        </div>`;
    }
    return _cmv40ArtifactsBody(s, [name]);
  }
  // Fase G: el MKV final se escribe en /mnt/output/{nombre}.mkv.tmp (fuera del
  // workdir), por eso no aparece en artifacts. Mostramos directamente el path.
  if (key === 'G') {
    const path = s.output_mkv_path || '';
    const name = s.output_mkv_name || (path ? path.split('/').pop() : '');
    if (!name) return '<div style="font-size:11px; color:var(--text-3)">—</div>';
    return `
      <div style="font-size:12px">
        <div style="color:var(--text-3); margin-bottom:6px">MKV remuxado (pre-validación Fase H):</div>
        <div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px dashed var(--sep); gap:8px">
          <code style="font-size:11px; word-break:break-all">${escHtml(name)}</code>
          <span style="font-size:11px; color:var(--text-3); white-space:nowrap">escrito en /mnt/output</span>
        </div>
        <div style="font-size:11px; color:var(--text-3); margin-top:6px; line-height:1.4">
          Sufijo <code>.mkv.tmp</code> mientras Fase H no valide. Tras validar se hace rename atómico al nombre final.
        </div>
      </div>`;
  }
  return '<div style="font-size:11px; color:var(--text-3)">—</div>';
}

function _cmv40ArtifactsBody(s, fileNames) {
  const arts = s.artifacts || {};
  const rows = fileNames.map(name => {
    const size = arts[name];
    if (size !== undefined) {
      return `<div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px dashed var(--sep)">
        <code style="font-size:11px">${escHtml(name)}</code>
        <span style="font-size:11px; color:var(--text-3)">${_fmtBytes(size)}</span>
      </div>`;
    }
    return `<div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px dashed var(--sep); opacity:0.5">
      <code style="font-size:11px">${escHtml(name)}</code>
      <span style="font-size:11px; color:var(--text-3)">no encontrado</span>
    </div>`;
  }).join('');
  const total = fileNames.reduce((acc, n) => acc + (arts[n] || 0), 0);
  return `
    <div style="font-size:12px">
      <div style="color:var(--text-3); margin-bottom:6px">Artefactos generados:</div>
      ${rows}
      ${total > 0 ? `<div style="margin-top:6px; font-size:11px; color:var(--text-3); text-align:right">Total: <b>${_fmtBytes(total)}</b></div>` : ''}
    </div>`;
}

async function _cmv40ClearError(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}/clear-error`, { method: 'POST' });
  if (data) {
    const project = openCMv40Projects.find(p => p.id === pid);
    if (project) {
      _cmv40AssignSession(project, data);
      _updateCMv40Panel(project);
    }
  }
}

/** Botón "🔄 Reintentar" del banner de error: descarta el error_message y
 *  dispara la fase active actual. Mapeado por key (A..H) → función do*. */
async function _cmv40RetryActivePhase(pid, faseKey) {
  await apiFetch(`/api/cmv40/${pid}/clear-error`, { method: 'POST' });
  const launcher = {
    A: () => cmv40DoAnalyzeSource(pid),
    F: () => cmv40DoInject(pid),
    G: () => cmv40DoRemux(pid),
    H: () => cmv40DoValidate(pid),
  }[faseKey];
  if (launcher) {
    launcher();
  } else {
    // Las fases B (target), C (extract) y D (sync) tienen flujos manuales —
    // refrescamos el panel para que el usuario vea la card active expandida.
    const data = await apiFetch(`/api/cmv40/${pid}`, { silent: true });
    if (data) {
      const project = openCMv40Projects.find(p => p.id === pid);
      if (project) {
        _cmv40AssignSession(project, data);
        _updateCMv40Panel(project);
      }
    }
  }
}

async function _cmv40Redo(pid, targetPhase, faseKey) {
  // Consultar qué artefactos se borrarán
  const preview = await apiFetch(`/api/cmv40/${pid}/reset-preview/${targetPhase}`);

  let artifactsList = '';
  if (preview?.files?.length) {
    const rows = preview.files.map(f =>
      `<li style="font-family:monospace; font-size:11px">${escHtml(f.name)} <span style="color:var(--text-3)">(${_fmtBytes(f.size_bytes)})</span></li>`
    ).join('');
    artifactsList = `
      <div style="margin-top:10px; padding:10px; background:var(--surface-2); border-radius:var(--r-sm); max-height:180px; overflow-y:auto">
        <div style="font-size:11px; color:var(--text-2); margin-bottom:6px">
          <b>Se borrarán ${preview.files.length} artefacto(s)</b> — ${_fmtBytes(preview.total_bytes)} liberados:
        </div>
        <ul style="margin:0; padding-left:18px">${rows}</ul>
      </div>`;
  } else {
    artifactsList = '<div style="font-size:11px; color:var(--text-3); margin-top:8px">No hay artefactos posteriores que borrar.</div>';
  }

  // Uso el modal cmv40-confirm-modal que acepta HTML en el body
  document.getElementById('cmv40-confirm-title').textContent = '¿Rehacer esta fase?';
  document.getElementById('cmv40-confirm-sub').textContent = 'La sesión volverá al estado previo. Las fases posteriores se invalidarán y sus artefactos se borrarán del disco.';
  document.getElementById('cmv40-confirm-body').innerHTML = artifactsList;
  const confirmBtn = document.getElementById('cmv40-confirm-btn');
  confirmBtn.textContent = 'Rehacer y borrar artefactos';
  confirmBtn.className = 'btn btn-danger btn-sm';
  const newBtn = confirmBtn.cloneNode(true);
  confirmBtn.parentNode.replaceChild(newBtn, confirmBtn);
  newBtn.addEventListener('click', async () => {
    closeModal('cmv40-confirm-modal');
    const data = await apiFetch(`/api/cmv40/${pid}/reset-to/${targetPhase}`, { method: 'POST' });
    if (data) {
      const project = openCMv40Projects.find(p => p.id === pid);
      if (project) {
        _cmv40AssignSession(project, data);
        if (!project.expandedPhases) project.expandedPhases = {};
        project.expandedPhases[faseKey] = true;
        project.syncData = null;
        // Tras reset invalidamos el dedup del orquestador, el timer del
        // overlay y el flag de bridging. El reset NO dispara ninguna fase
        // automaticamente — el lanzamiento es siempre manual. Si el usuario
        // tiene auto=ON y lanza la fase manualmente, las siguientes se
        // encadenaran al terminar esa.
        project._lastAutoFiredFor = null;
        project._lastAutoFiredAt = 0;
        project._pipelineStartMs = null;
        project._resolvedStartedMs = null;
        project._autoChaining = false;
        _updateCMv40Panel(project);
      }
      refreshCMv40Sidebar();
      showToast(`Fase ${faseKey} lista para rehacer`, 'info');
    }
  });
  openModal('cmv40-confirm-modal');
}

// ── Tarjetas por fase ────────────────────────────────────────────

function _cmv40FaseABody(pid, s) {
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Extrae el stream HEVC y el RPU del MKV origen. Tarda 2-5 minutos.</div>
      <button class="btn btn-primary btn-md" onclick="cmv40DoAnalyzeSource('${pid}')">🔍 Analizar origen</button>
    </div>`;
}

function _cmv40FaseBBody(pid, s) {
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Elige una fuente del RPU CMv4.0 a inyectar.</div>
      <div class="cmv40-tab-switcher">
        <button class="cmv40-tab-btn active" id="cmv40-tab-btn-repo-${pid}"
          onclick="_cmv40SwitchTargetTab('${pid}','repo')">📦 Repo DoviTools</button>
        <button class="cmv40-tab-btn" id="cmv40-tab-btn-mkv-${pid}"
          onclick="_cmv40SwitchTargetTab('${pid}','mkv')">🎬 Extraer de otro MKV</button>
        <button class="cmv40-tab-btn" id="cmv40-tab-btn-path-${pid}"
          onclick="_cmv40SwitchTargetTab('${pid}','path')">📂 Carpeta NAS</button>
      </div>

      <div id="cmv40-target-repo-${pid}" class="cmv40-target-tab">
        <div id="cmv40-repo-info-${pid}" style="font-size:12px;color:var(--text-3);margin-bottom:8px">— Cargando candidatos del repositorio… —</div>
        <div id="cmv40-repo-list-${pid}" class="cmv40-repo-list" style="max-height:280px;overflow-y:auto"></div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:12px">
          <button class="btn btn-primary btn-md" onclick="cmv40DoTargetFromDrive('${pid}')">⬇ Descargar y usar</button>
          <button class="btn btn-secondary btn-sm" onclick="_cmv40LoadRepoForPanel('${pid}')">↺ Refrescar</button>
        </div>
      </div>

      <div id="cmv40-target-path-${pid}" class="cmv40-target-tab" style="display:none">
        <label class="modal-field-label">RPU disponible en /mnt/cmv40_rpus/</label>
        <div class="iso-select-row">
          <select id="cmv40-rpu-select-${pid}" class="iso-select">
            <option value="">— Cargando… —</option>
          </select>
          <button class="btn btn-secondary btn-sm" onclick="_cmv40LoadRpus('${pid}')">↺</button>
        </div>
        <button class="btn btn-primary btn-md" style="margin-top:12px" onclick="cmv40DoTargetFromPath('${pid}')">✓ Usar este RPU</button>
      </div>

      <div id="cmv40-target-mkv-${pid}" class="cmv40-target-tab" style="display:none">
        <label class="modal-field-label">MKV que ya tiene CMv4.0</label>
        <div class="iso-select-row">
          <select id="cmv40-target-mkv-select-${pid}" class="iso-select">
            <option value="">— Cargando… —</option>
          </select>
          <button class="btn btn-secondary btn-sm" onclick="_cmv40LoadTargetMkvs('${pid}')">↺</button>
        </div>
        <button class="btn btn-primary btn-md" style="margin-top:12px" onclick="cmv40DoTargetFromMkv('${pid}')">✂️ Extraer RPU del MKV</button>
      </div>
    </div>`;
}

function _cmv40FaseCBody(pid, s) {
  // El warning del Δ frames debe matizar que Fase D puede omitirse si los
  // trust gates aprobaron alineación (no siempre habrá "revisión visual").
  const trust = _cmv40Trust(s);
  const deltaNote = trust
    ? 'Los trust gates ya validaron la alineación; la diferencia se considera tolerable y Fase D se omitirá.'
    : 'Se evaluará en Fase D (chart de sincronización) — podrás aplicar corrección si hace falta.';
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Separa el HEVC en BL (Capa Base) + EL (Capa de Mejora) y extrae datos de luminancia por frame para el chart de sincronización. Tarda 5-15 min.</div>
      ${s.sync_delta !== 0 ? `<div class="banner warning" style="margin-bottom:10px"><span class="banner-icon">⚠️</span><span>Diferencia de frames detectada (Δ = ${s.sync_delta > 0 ? '+' : ''}${s.sync_delta}). ${deltaNote}</span></div>` : ''}
      <button class="btn btn-primary btn-md" onclick="cmv40DoExtract('${pid}')">✂️ Extraer BL/EL + per-frame data</button>
    </div>`;
}

function _cmv40FaseDBody(pid, s) {
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Chart de MaxPQ (L1 del RPU Dolby Vision) por frame. Rojo = origen, Azul = target. Las curvas deben coincidir en forma; si hay offset detectable, se aplica corrección con dovi_tool editor.</div>
      <div id="cmv40-sync-stats-${pid}" class="cmv40-sync-stats"></div>
      <div id="cmv40-chart-wrap-${pid}" class="cmv40-chart-wrap">
        <canvas id="cmv40-chart-${pid}" width="1000" height="320"></canvas>
        <div class="cmv40-chart-tooltip" id="cmv40-chart-tooltip-${pid}" style="display:none"></div>
      </div>
      <div class="cmv40-sync-controls" id="cmv40-sync-controls-${pid}"></div>
      <div id="cmv40-confidence-${pid}"></div>
    </div>`;
}

function _cmv40FaseFBody(pid, s) {
  // Texto dinámico según workflow y target_type (igual estrategia que el
  // sidebar de la timeline). El banner "verifica el gráfico" solo aplica si
  // Fase D fue ejecutada visualmente — con trust_ok o ack se omite.
  const trust = _cmv40Trust(s);
  const wf = s.source_workflow || 'p7_fel';
  const dropIn = _cmv40DropIn(s);
  const targetNeedsMerge = _cmv40TargetNeedsMerge(s);
  const userAcked = !!s.user_acknowledged_degradation;
  const faseDExecutedVisually = !trust && !userAcked;
  let desc;
  if (dropIn) {
    desc = 'Inyecta el RPU del bin directamente sobre source.hevc (BL+EL juntos, sin merge ni mux posterior). Vía más rápida — el byte-identical del RPU queda garantizado.';
  } else if (wf === 'p7_fel') {
    desc = 'Merge CMv4.0 sobre el RPU P7 del source + inyecta el RPU merged en EL.hevc preservando la FEL.';
  } else if (wf === 'p7_mel') {
    desc = targetNeedsMerge
      ? 'Merge CMv4.0 sobre el RPU P7 MEL del source + inyecta el RPU merged en BL.hevc (descarta el EL MEL → P8.1 CMv4.0).'
      : 'Inyecta el RPU target directamente en BL.hevc (target P8 retail, sin merge — descarta el EL MEL → P8.1).';
  } else {  // p8
    desc = targetNeedsMerge
      ? 'Merge CMv4.0 sobre el RPU P8 del source + inyecta el RPU merged en source.hevc.'
      : 'Inyecta el RPU target directamente en source.hevc (target P8 retail, sin merge — reemplaza el RPU CMv2.9 existente).';
  }
  const reviewBanner = faseDExecutedVisually
    ? '<div class="banner info" style="margin-bottom:10px"><span class="banner-icon">ℹ️</span><span>Verifica en el chart de Fase D que las curvas coinciden antes de inyectar.</span></div>'
    : '';
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">${escHtml(desc)}</div>
      ${reviewBanner}
      <button class="btn btn-primary btn-md" onclick="cmv40DoInject('${pid}')">💉 Inyectar RPU</button>
    </div>`;
}

function _cmv40FaseGBody(pid, s) {
  // Texto dinámico según workflow + drop-in. Misma lógica que el sidebar.
  const trust = _cmv40Trust(s);
  const wf = s.source_workflow || 'p7_fel';
  const dropIn = _cmv40DropIn(s);
  let desc;
  if (dropIn) {
    desc = 'mkvmerge directo sobre source_injected.hevc (BL+EL dual-layer ya combinado en Fase F) con audio/subs/capítulos del MKV origen.';
  } else if (wf === 'p7_fel') {
    desc = 'dovi_tool mux combina BL.hevc + EL_injected.hevc en un HEVC dual-layer + mkvmerge añade audio/subs/capítulos del MKV origen.';
  } else {  // p7_mel / p8: single-layer
    desc = 'Sin mux dual-layer (single-layer) — mkvmerge directo sobre BL_injected.hevc con audio/subs/capítulos del MKV origen.';
  }
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">${escHtml(desc)}</div>
      <button class="btn btn-primary btn-md" onclick="cmv40DoRemux('${pid}')">📦 Remux MKV final</button>
    </div>`;
}

function _cmv40FaseHBody(pid, s) {
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px">Verifica que el MKV resultante tiene CMv4.0 y mueve a /mnt/output.</div>
      <button class="btn btn-primary btn-md" onclick="cmv40DoValidate('${pid}')">✅ Validar y finalizar</button>
    </div>`;
}

// ── Acciones de fases ────────────────────────────────────────────

/** Toast de inicio de fase. Silenciado cuando el auto-pipeline está activo
 *  — el timeline lateral ya muestra fase en curso + progreso en vivo y los
 *  toasts intermedios saturan la UI. Con auto-off (usuario dispara fase
 *  manualmente con el botón), sí aparece para confirmar que se oyó el click. */
function _cmv40PhaseToast(pid, msg) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (project?.autoContinue) return;
  showToast(msg, 'info');
}

async function cmv40DoAnalyzeSource(pid) {
  await apiFetch(`/api/cmv40/${pid}/analyze-source`, { method: 'POST' });
  _cmv40PhaseToast(pid, 'Analizando origen…');
  // Polling hasta que termine la fase
  _cmv40PollPhase(pid, 'source_analyzed', 'error');
}

// Cadencia del poller de fase. Era 500ms, pensado para que la UI reaccionara
// rápido al cambio de fase; pero el log en vivo ya llega por WS y el panel se
// repinta con él, así que lo único que aporta este tick es detectar el fin de
// fase. A 1,5s el encadenado sigue siendo instantáneo a ojo (las fases duran
// minutos) y el poller cubre 15 min en vez de 5 con los mismos maxTries.
const CMV40_POLL_PHASE_MS = 1500;

/**
 * Polling hasta que la sesión alcance una fase objetivo (o error).
 * Refresca la UI cada CMV40_POLL_PHASE_MS durante 15 min máximo.
 *
 * Si el proyecto tiene project.autoContinue === true y terminó la fase con
 * éxito, dispara la siguiente fase automáticamente (sin atravesar Fase D).
 */
async function _cmv40PollPhase(pid, targetPhase, errorPhase = 'error', maxTries = 600) {
  // Singleton por pid: se dispara desde varios sitios (analyze, target-provided,
  // inject…). Sin guard, dos llamadas para el mismo proyecto corrían bucles de
  // polling concurrentes → GETs pesados solapados a /api/cmv40/{id} (audit #10).
  if (!window._cmv40PollActive) window._cmv40PollActive = {};
  if (window._cmv40PollActive[pid]) return;
  window._cmv40PollActive[pid] = true;
  for (let i = 0; i < maxTries; i++) {
    await new Promise(r => setTimeout(r, CMV40_POLL_PHASE_MS));
    // silent: ver _refreshCMv40Session — polling rutinario suprime toasts
    // de timeout transitorio bajo carga I/O.
    // include_log=false: este poller solo mira phase/running_phase/error. El
    // log llega por WS. Pidiéndolo entero eran 1,57 MB y 437 ms de servidor
    // por tick, ~1.000 ticks en una fase de inject de 15 min.
    const data = await apiFetch(`/api/cmv40/${pid}?include_log=false`, { silent: true });
    if (!data) continue;
    const project = openCMv40Projects.find(p => p.id === pid);
    if (project) {
      _cmv40AssignSession(project, data);
      _updateCMv40Panel(project);
    }
    // Termina cuando: no hay fase corriendo, alcanzó objetivo, hay error, o done
    if (!data.running_phase && (data.phase === targetPhase || data.phase === 'done' || data.error_message)) {
      refreshCMv40Sidebar();
      // Liberar el singleton ANTES de encadenar. Si se libera después, la
      // llamada a _cmv40MaybeAutoAdvance de aquí abajo dispara la fase
      // siguiente y SU _cmv40PollPhase se encuentra el flag todavía en true
      // → retorna sin vigilar nada. Esa fase se quedaba cubierta solo por el
      // safety poller de 4s, que con un snapshot atrasado reintentaba el
      // disparo a los 5s: es el origen del "⏭ Fase validate omitida" que
      // aparecía justo 5s después de completarla (John Wick 4, FNAF 2).
      window._cmv40PollActive[pid] = false;
      // Auto-avanzar si el flag está activo y no hay error
      if (project && project.autoContinue && !data.error_message && data.phase !== 'done') {
        _cmv40MaybeAutoAdvance(project);
      }
      return;
    }
  }
  window._cmv40PollActive[pid] = false;
}

// Ventana del retry del dedup de auto-avance (ver más abajo). Era 5s, y una
// fase que el backend completa en menos que eso (Fase H en drop-in tarda 1-4s)
// entraba en carrera: el frontend aún no había visto el 'done' y reintentaba
// el mismo disparo al segundo 5. El backend lo rechaza —"⏭ Fase X omitida"—
// pero ensucia el log y confunde. 12s deja margen de sobra para que el estado
// llegue, sin renunciar a recuperar disparos realmente perdidos.
const AUTO_ADVANCE_RETRY_MS = 12000;

/**
 * Orquesta el auto-pipeline: dispara la siguiente fase según la actual.
 * Fase D (extracted → sync_verified) es MANUAL por diseño — revisión visual.
 */
function _cmv40MaybeAutoAdvance(project) {
  if (!project.autoContinue) return;
  const s = project.session;
  if (s.running_phase || s.error_message || s.archived) return;
  // Pause point por gates críticos pendientes de ACK del usuario. Banner
  // ámbar en el panel pide confirmación; sin ack no se progresa. Apagamos
  // _autoChaining para que el overlay se oculte y se vea el banner.
  if (s.awaiting_critical_ack) {
    project._autoChaining = false;
    return;
  }
  // Pause point por decisión del pre-flight: si el bin del repo fue
  // clasificado como sintético (keep_l8_default) o sin CMv4.0, el backend
  // detuvo el pipeline esperando que el usuario acepte Keep o fuerce
  // Restore desde la UI. Sin este guard, el frontend re-disparaba el
  // endpoint /preflight-target cada 4s (aunque el backend ya lo rechaza
  // con 'started:false', generaba tráfico HTTP inútil).
  if (s.preflight_decision && s.preflight_decision !== 'ok') {
    project._autoChaining = false;
    return;
  }
  const pid = project.id;
  // Dedup key: phase + estado de target_preflight_ok. Necesitamos sensibilidad
  // al flag de preflight porque para la fase 'created' hay dos acciones
  // distintas: si !target_preflight_ok → disparar preflight; si OK → Fase A.
  // Sin esto, _lastAutoFiredFor === 'created' nos bloquearía la transición
  // preflight → Fase A.
  //
  // RETRY ROBUSTO: el flag ahora trackea el timestamp del último trigger.
  // Si el mismo stateKey lleva >5s sin haber avanzado (caso real: pestaña
  // sin foco → setTimeout throttled → polling interno se cuelga → la
  // siguiente fase nunca se dispara), volvemos a intentar. Sin esto, una
  // sola falla silenciosa atasca el auto-pipeline indefinidamente.
  const stateKey = s.phase + ':pf=' + (s.target_preflight_ok ? '1' : '0');
  const now = Date.now();
  const lastFired = project._lastAutoFiredFor;
  // Dedup: para fases NO terminales, retry tras 5s (recupera transiciones
  // perdidas por throttling de background tab). Para fases TERMINALES
  // (done), dedup ESTRICTO una sola vez — sino el toast "Pipeline
  // completado" se re-disparaba cada 5s al volver el foco a la pestaña
  // (visto: con un proyecto done abierto, el toast aparecía recurrente
  // porque cada burst refresh / safety check llamaba a esta función).
  const isTerminalPhase = (s.phase === 'done');
  if (lastFired && lastFired.state === stateKey) {
    if (isTerminalPhase || (now - lastFired.at) < AUTO_ADVANCE_RETRY_MS) {
      return;
    }
  }
  project._lastAutoFiredFor = { state: stateKey, at: now };
  // Marca que la cadena auto está encadenando en este momento — usado por
  // el overlay para mostrarse durante el "puente" entre dos fases. Se limpia
  // al alcanzar un estado terminal o al intervenir manualmente (toggle,
  // reset, cancel). No es lo mismo que autoContinue: el flag refleja
  // actividad, la variable refleja configuración.
  project._autoChaining = true;
  // Los toasts intermedios ("🤖 Auto: analizando", "🤖 Auto: inyectando"…) eran
  // redundantes con el timeline lateral que ya muestra fase en curso + progreso.
  // Aquí solo disparamos las acciones del pipeline; el toast de inicio está en
  // cmv40ToggleAuto y el de fin (done) lo emitimos al final del switch.
  switch (s.phase) {
    case 'created':
      // Pre-flight bloqueante: si hay pendingTarget y aun no se ha validado,
      // disparamos preflight PRIMERO. Setea running_phase="preflight" y bloquea
      // el resto del pipeline. Solo cuando target_preflight_ok=true, el
      // siguiente tick de auto-advance dispara Fase A.
      if (project.pendingTarget && !s.target_preflight_ok) {
        _cmv40FirePreflight(pid, project.pendingTarget);
      } else {
        cmv40DoAnalyzeSource(pid);
      }
      break;
    case 'source_analyzed':
      // Si el usuario preseleccionó el target en el modal, aplicarlo automático
      if (project.pendingTarget) {
        const t = project.pendingTarget;
        project.pendingTarget = null;
        if (t.kind === 'path') {
          _cmv40AutoTargetPath(pid, t.value);
        } else if (t.kind === 'repo') {
          _cmv40AutoTargetDrive(pid, t.value);
        } else {
          _cmv40AutoTargetMkv(pid, t.value);
        }
      } else {
        // Pause point: sin pendingTarget, el usuario debe provisionar manual.
        // Apagamos _autoChaining para que el overlay se oculte y la UI vuelva
        // al proyecto. autoContinue se mantiene ON para retomar si el usuario
        // lanza Fase B manualmente.
        project._autoChaining = false;
      }
      break;
    case 'target_provided':
      cmv40DoExtract(pid);
      break;
    case 'extracted': {
      // Trusted target: los gates automáticos ya validaron frame count,
      // CM v4.0, L5/L6 — saltar la revisión visual manual.
      // user_acknowledged_degradation: el usuario reconocio que algun gate
      // critico (L5 grande, L6/L1 muy grandes) genera resultado degradado
      // pero aceptó continuar — Fase D no puede arreglar nada en ese caso,
      // saltamos directamente a inject/remux/validate.
      const s = project.session;
      // La regla vive en la tabla del backend y llega en el plan; el
      // backend la adoptó al unificar (antes hacía `trusted_auto or
      // user_acked`, que se saltaba Fase D con el ACK dado aunque el
      // usuario hubiera pedido revisión manual).
      const trustedAuto = _cmv40SkipSyncReview(s);
      if (trustedAuto) {
        if (!s.phases_skipped) s.phases_skipped = [];
        if (!s.phases_skipped.includes('sync_verification_pause')) {
          s.phases_skipped.push('sync_verification_pause');
        }
        _cmv40AutoMarkSynced(pid);
      } else {
        // Pause point: target no pasó los trust gates (caso MKV custom o bin
        // generated). El flujo se detiene aqui a la espera de revisión visual
        // manual en Fase D. Apagamos _autoChaining para que el overlay se oculte
        // y el usuario pueda interactuar con el chart. autoContinue se mantiene
        // ON para que al pulsar "Confirmar sync" (o aplicar correccion) la
        // cadena retome automaticamente hacia Fase F.
        project._autoChaining = false;
        showToast('⏸️ Auto pausado en Fase D — los gates requieren revisión manual del sync', 'info');
      }
      break;
    }
    case 'sync_verified':
      _cmv40AutoInject(pid);
      break;
    case 'injected':
      cmv40DoRemux(pid);
      break;
    case 'remuxed':
      cmv40DoValidate(pid);
      break;
    case 'done':
      // Terminal: toast único de éxito cuando la pipeline completa el full
      // run. El dedup estricto por isTerminalPhase (línea ~13755) evita que
      // bursts post-wake / safety poller / ws.onopen re-disparen el toast.
      // NO apagamos `autoContinue` aquí: el backend mantiene
      // `session.auto_pipeline=true` post-done y desincronizar el frontend
      // confundiría futuros refreshes (resumeAuto leería true del backend
      // y revertiría la flag local a true).
      showToast('✅ Pipeline CMv4.0 completado — MKV listo en /mnt/output', 'success');
      break;
  }
}

async function _cmv40AutoTargetPath(pid, rpuPath) {
  await apiFetch(`/api/cmv40/${pid}/target-rpu-path`, {
    method: 'POST',
    body: JSON.stringify({ rpu_path: rpuPath }),
  });
  _cmv40PollPhase(pid, 'target_provided');
}

async function _cmv40AutoTargetDrive(pid, driveSel) {
  await apiFetch(`/api/cmv40/${pid}/target-rpu-from-drive`, {
    method: 'POST',
    body: JSON.stringify({ file_id: driveSel.file_id, file_name: driveSel.file_name }),
  });
  _cmv40PollPhase(pid, 'target_provided');
}

async function _cmv40AutoTargetMkv(pid, mkvPath) {
  await apiFetch(`/api/cmv40/${pid}/target-rpu-from-mkv`, {
    method: 'POST',
    body: JSON.stringify({ source_mkv_path: mkvPath }),
  });
  _cmv40PollPhase(pid, 'target_provided');
}

async function _cmv40AutoInject(pid) {
  await apiFetch(`/api/cmv40/${pid}/inject`, { method: 'POST' });
  _cmv40PollPhase(pid, 'injected');
}

// Para target trusted: confirma sync OK sin intervención manual y avanza
// automáticamente a Fase F (inject).
/** Fallback del criterio de la Fase D cuando la respuesta no trae `sync_gate`
 *  (sesión cacheada de antes del cambio). La fuente de verdad es
 *  `evaluate_sync_gate` en el backend; esto solo evita una UI en blanco. */
function _cmv40SyncGateLocal(delta, confOk, confPct) {
  if (delta !== 0) {
    return { ok: false, reason: `Hay diferencia de frames (Δ = ${delta > 0 ? '+' : ''}${delta}); corrígela antes de confirmar` };
  }
  if (!confOk) {
    return { ok: false, reason: `Confianza ${confPct}% inferior al umbral 85% — revisa el gráfico o verifica que el RPU target corresponda a esta película` };
  }
  return { ok: true, reason: '' };
}

async function _cmv40AutoMarkSynced(pid) {
  await apiFetch(`/api/cmv40/${pid}/mark-synced`, { method: 'POST' });
  _cmv40PollPhase(pid, 'sync_verified');
}

/** Toggle del auto-pipeline para un proyecto. */
async function cmv40ToggleAuto(pid) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  // Si activamos, validar colisión de nombre en /mnt/output
  if (!project.autoContinue) {
    const existing = await apiFetch('/api/mkv/files');
    const name = project.session.output_mkv_name;
    if (existing?.files?.includes(name)) {
      showToast(`⚠️ Ya existe un MKV con el nombre "${name}" en /mnt/output. Renómbralo antes de activar auto.`, 'warning');
      return;
    }
  }
  project.autoContinue = !project.autoContinue;
  // El switch solo marca el modo de trabajo — NO dispara fases por si mismo.
  // Al acabar la fase que el usuario lance manualmente, si auto=ON la siguiente
  // se encadena automaticamente. Lanzar con el toggle seria sorprendente para
  // el usuario (ej. si tocan el toggle sin recordar que estado tiene el proyecto).
  // Toggling tambien apaga _autoChaining — limpia el estado de bridging.
  project._autoChaining = false;
  _updateCMv40Panel(project);
  // Sincroniza con el backend (auto_pipeline persistente). Si lo activamos
  // y la sesión está en una fase intermedia, el backend retoma la cadena
  // inmediatamente — el job avanza solo aunque cierres el navegador.
  apiFetch(`/api/cmv40/${pid}/auto-pipeline`, {
    method: 'POST',
    body: JSON.stringify({ enabled: project.autoContinue }),
    silent: true,
  }).catch(() => {});
  if (project.autoContinue) {
    showToast('🤖 Auto-pipeline activado · el backend encadenará las fases sin depender del cliente', 'success');
  } else {
    showToast('Auto-pipeline desactivado · tendrás que lanzar cada fase manualmente', 'info');
  }
}

function _cmv40SwitchTargetTab(pid, tab) {
  const repoEl = document.getElementById(`cmv40-target-repo-${pid}`);
  const pathEl = document.getElementById(`cmv40-target-path-${pid}`);
  const mkvEl  = document.getElementById(`cmv40-target-mkv-${pid}`);
  if (repoEl) repoEl.style.display = (tab === 'repo') ? '' : 'none';
  if (pathEl) pathEl.style.display = (tab === 'path') ? '' : 'none';
  if (mkvEl)  mkvEl.style.display  = (tab === 'mkv')  ? '' : 'none';
  const btnRepo = document.getElementById(`cmv40-tab-btn-repo-${pid}`);
  const btnPath = document.getElementById(`cmv40-tab-btn-path-${pid}`);
  const btnMkv  = document.getElementById(`cmv40-tab-btn-mkv-${pid}`);
  if (btnRepo) btnRepo.classList.toggle('active', tab === 'repo');
  if (btnPath) btnPath.classList.toggle('active', tab === 'path');
  if (btnMkv)  btnMkv.classList.toggle('active',  tab === 'mkv');
  if (tab === 'repo')      _cmv40LoadRepoForPanel(pid);
  else if (tab === 'path') _cmv40LoadRpus(pid);
  else                     _cmv40LoadTargetMkvs(pid);
}

// Token anti-race por proyecto (igual que en el modal pero scopeado al pid)
const _cmv40PanelRepoReqIds = {};

/**
 * Carga candidatos de bin del repo DoviTools para un proyecto en Fase B.
 * Usa el filename del MKV origen del proyecto (no estado del modal).
 */
async function _cmv40LoadRepoForPanel(pid) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  const list = document.getElementById(`cmv40-repo-list-${pid}`);
  const info = document.getElementById(`cmv40-repo-info-${pid}`);
  if (!list) return;
  const sourcePath = project.session?.source_mkv_path || '';
  const filename = sourcePath.split('/').pop() || '';
  if (!filename) {
    list.innerHTML = '<div class="cmv40-repo-empty">Sin MKV origen — imposible matchear.</div>';
    if (info) info.textContent = '';
    return;
  }
  list.innerHTML = '<div class="cmv40-repo-empty">⏳ Buscando en Drive…</div>';
  if (info) info.innerHTML = '<span class="cmv40-rec-spinner-inline"></span> Consultando repositorio de DoviTools…';
  const reqId = (_cmv40PanelRepoReqIds[pid] || 0) + 1;
  _cmv40PanelRepoReqIds[pid] = reqId;
  const qs = '?filename=' + encodeURIComponent(filename);
  const data = await apiFetch('/api/cmv40/repo-rpus' + qs);
  if (_cmv40PanelRepoReqIds[pid] !== reqId) return;
  if (!data) {
    list.innerHTML = '<div class="cmv40-repo-empty">Error consultando el repositorio.</div>';
    if (info) info.textContent = '';
    return;
  }
  if (!data.drive_configured) {
    list.innerHTML = '<div class="cmv40-repo-empty">Repositorio DoviTools no configurado — abre ⚙︎ Configuración para añadir Google API key + URL del repo.</div>';
    if (info) info.textContent = '';
    return;
  }
  const cands = data.candidates || [];
  if (!cands.length) {
    const t = data.title_en || data.title_es || filename;
    list.innerHTML = `<div class="cmv40-repo-empty">Sin coincidencias para "${escHtml(t)}". Prueba otra pestaña.</div>`;
    if (info) info.textContent = '';
    return;
  }
  project._panelRepoCands = cands;
  const topId = cands[0]?.file?.id || '';
  const renderCard = (c) => {
    const sizeMb = (c.file.size_bytes / 1024 / 1024).toFixed(1);
    const pt = c.predicted_type || 'unknown';
    const prov = c.provenance || '';
    const tagMeta = pt === 'trusted_p7_fel_final' ? { icon: '🎯', label: 'bin P7 FEL',  cls: 'tag-ok' }
                  : pt === 'trusted_p7_mel_final' ? { icon: '🎯', label: 'bin P7 MEL',  cls: 'tag-ok' }
                  : pt === 'trusted_p8_source'    ? { icon: '📦', label: 'bin P8 retail', cls: 'tag-info' }
                  : { icon: '❓', label: 'tipo desconocido', cls: 'tag-warn' };
    const provTag = prov === 'retail'
      ? '<span class="cmv40-repo-card-tag tag-ok">🏛 Retail</span>'
      : prov === 'generated'
      ? '<span class="cmv40-repo-card-tag tag-warn">⚠ Generated</span>'
      : '';
    const isBest = c.file.id === topId;
    return `
      <div class="cmv40-repo-card" data-file-id="${escHtml(c.file.id)}"
           role="button" tabindex="0"
           onclick="_cmv40SelectRepoForPanel('${escHtml(pid)}','${escHtml(c.file.id)}')"
           onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();_cmv40SelectRepoForPanel('${escHtml(pid)}','${escHtml(c.file.id)}')}">
        <div class="cmv40-repo-card-head">
          <span class="cmv40-repo-card-tag ${tagMeta.cls}">${tagMeta.icon} ${tagMeta.label}</span>
          ${provTag}
          ${isBest ? '<span class="cmv40-repo-card-best">🏆 mejor match</span>' : ''}
          <span class="cmv40-repo-card-score">${Math.round(c.score * 100)}%</span>
          <span class="cmv40-repo-card-size">${sizeMb} MB</span>
        </div>
        <div class="cmv40-repo-card-path">${escHtml(c.file.path)}</div>
      </div>`;
  };
  list.innerHTML = cands.map(renderCard).join('');
  if (info) {
    info.innerHTML = `<strong>${cands.length}</strong> candidato${cands.length !== 1 ? 's' : ''} · top score: <strong>${Math.round(cands[0].score * 100)}%</strong>. Click para seleccionar.`;
  }
  if (topId) _cmv40SelectRepoForPanel(pid, topId);
}

function _cmv40SelectRepoForPanel(pid, fileId) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  const list = document.getElementById(`cmv40-repo-list-${pid}`);
  if (!list) return;
  const card = list.querySelector(`.cmv40-repo-card[data-file-id="${fileId}"]`);
  if (!card) return;
  list.querySelectorAll('.cmv40-repo-card.selected').forEach(el => el.classList.remove('selected'));
  card.classList.add('selected');
  const cand = (project._panelRepoCands || []).find(c => c.file.id === fileId);
  if (!cand) return;
  project._panelSelectedRepo = { file_id: cand.file.id, file_name: cand.file.name };
}

async function cmv40DoTargetFromDrive(pid) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  const sel = project._panelSelectedRepo;
  if (!sel || !sel.file_id) {
    showToast('Selecciona un candidato del repositorio', 'warning');
    return;
  }
  await apiFetch(`/api/cmv40/${pid}/target-rpu-from-drive`, {
    method: 'POST',
    body: JSON.stringify({ file_id: sel.file_id, file_name: sel.file_name || '' }),
  });
  _cmv40PhaseToast(pid, 'Descargando RPU del repositorio…');
  _cmv40PollPhase(pid, 'target_provided');
}

async function _cmv40LoadRpus(pid) {
  const select = document.getElementById(`cmv40-rpu-select-${pid}`);
  const data = await apiFetch('/api/cmv40/rpu-files');
  select.innerHTML = '<option value="">— Seleccionar RPU —</option>';
  if (data?.files?.length) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = `${f.name} (${_fmtBytes(f.size_bytes)})`;
      select.appendChild(opt);
    });
  } else {
    select.innerHTML = '<option value="">— No hay RPUs en /mnt/cmv40_rpus —</option>';
  }
}

async function _cmv40LoadTargetMkvs(pid) {
  const select = document.getElementById(`cmv40-target-mkv-select-${pid}`);
  const data = await apiFetch('/api/mkv/files-in-isos');
  select.innerHTML = '<option value="">— Seleccionar MKV con CMv4.0 —</option>';
  if (data?.files && data.files.length) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = f.name;
      select.appendChild(opt);
    });
  } else {
    select.innerHTML = '<option value="">— No hay MKVs en el directorio de ISOs —</option>';
  }
}

async function cmv40DoTargetFromPath(pid) {
  const select = document.getElementById(`cmv40-rpu-select-${pid}`);
  const rpuPath = select.value;
  if (!rpuPath) {
    showToast('Selecciona un RPU', 'warning');
    return;
  }
  const data = await apiFetch(`/api/cmv40/${pid}/target-rpu-path`, {
    method: 'POST',
    body: JSON.stringify({ rpu_path: rpuPath }),
  });
  if (data) {
    showToast('RPU target cargado', 'success');
    const project = openCMv40Projects.find(p => p.id === pid);
    if (project) {
      _cmv40AssignSession(project, data);
      _updateCMv40Panel(project);
      refreshCMv40Sidebar();
      if (project.autoContinue) _cmv40MaybeAutoAdvance(project);
    } else {
      _refreshCMv40Session(pid);
    }
  }
}

async function cmv40DoTargetFromMkv(pid) {
  const select = document.getElementById(`cmv40-target-mkv-select-${pid}`);
  const mkvPath = select.value;
  if (!mkvPath) {
    showToast('Selecciona un MKV', 'warning');
    return;
  }
  await apiFetch(`/api/cmv40/${pid}/target-rpu-from-mkv`, {
    method: 'POST',
    body: JSON.stringify({ source_mkv_path: mkvPath }),
  });
  _cmv40PhaseToast(pid, 'Extrayendo RPU del MKV…');
  _cmv40PollPhase(pid, 'target_provided');
}

async function cmv40DoExtract(pid) {
  await apiFetch(`/api/cmv40/${pid}/extract`, { method: 'POST' });
  _cmv40PhaseToast(pid, 'Extrayendo BL/EL y datos per-frame…');
  _cmv40PollPhase(pid, 'extracted');
}

async function cmv40DoInject(pid) {
  showConfirm(
    '¿Inyectar RPU?',
    'Esto creará EL_injected.hevc. ¿Has verificado que la sincronización es correcta?',
    async () => {
      await apiFetch(`/api/cmv40/${pid}/inject`, { method: 'POST' });
      _cmv40PhaseToast(pid, 'Inyectando RPU…');
      _cmv40PollPhase(pid, 'injected');
    },
    'Inyectar',
  );
}

async function cmv40DoRemux(pid) {
  await apiFetch(`/api/cmv40/${pid}/remux`, { method: 'POST' });
  _cmv40PhaseToast(pid, 'Remuxando a MKV final…');
  _cmv40PollPhase(pid, 'remuxed');
}

async function cmv40DoValidate(pid) {
  await apiFetch(`/api/cmv40/${pid}/validate`, { method: 'POST' });
  _cmv40PhaseToast(pid, 'Validando MKV final…');
  // Polling — Fase H dura varios minutos (move 42 GB), no se puede hacer síncrono
  _cmv40PollPhase(pid, 'done');
}

async function cmv40Cleanup(pid) {
  const bodyHtml = `
    <div style="line-height:1.6">
      <p style="margin:0 0 10px 0"><b>Qué se borrará:</b></p>
      <ul style="margin:0 0 12px 18px; padding:0; font-family:'SF Mono',monospace; font-size:11px">
        <li>source.hevc, BL.hevc, EL.hevc</li>
        <li>RPU_source.bin, RPU_target.bin, RPU_synced.bin</li>
        <li>EL_injected.hevc</li>
        <li>per_frame_data.json, editor_config.json</li>
      </ul>
      <p style="margin:0 0 10px 0"><b>Qué se preserva:</b></p>
      <ul style="margin:0 0 12px 18px; padding:0; font-size:12px">
        <li>El MKV final en <code>/mnt/output</code></li>
        <li>Los metadatos del proyecto (log, sync_config, info DV)</li>
      </ul>
      <div class="banner warning" style="margin-top:12px">
        <span class="banner-icon">⚠️</span>
        <span><b>Esta acción archiva el proyecto</b>. No podrás rehacer fases porque los artefactos de entrada ya no existen. Para iterar de nuevo tendrás que crear un proyecto nuevo desde el MKV origen.</span>
      </div>
    </div>`;

  document.getElementById('cmv40-confirm-title').textContent = '¿Limpiar artefactos?';
  document.getElementById('cmv40-confirm-sub').textContent = 'Esta acción libera espacio en disco pero deja el proyecto en modo solo lectura.';
  document.getElementById('cmv40-confirm-body').innerHTML = bodyHtml;

  const btn = document.getElementById('cmv40-confirm-btn');
  btn.textContent = 'Limpiar y archivar';
  btn.className = 'btn btn-danger btn-sm';
  const newBtn = btn.cloneNode(true);
  btn.parentNode.replaceChild(newBtn, btn);
  newBtn.addEventListener('click', async () => {
    closeModal('cmv40-confirm-modal');
    const data = await apiFetch(`/api/cmv40/${pid}/cleanup`, { method: 'POST' });
    if (data) {
      showToast(`Liberado ${_fmtBytes(data.freed_bytes)} · proyecto archivado`, 'success');
      _refreshCMv40Session(pid);
    }
  });
  openModal('cmv40-confirm-modal');
}

// ── Sidebar Tab 3 ────────────────────────────────────────────────

async function refreshCMv40Sidebar() {
  // silent: invocado desde WS handlers, _refreshCMv40Session, _cmv40PollPhase
  // y tras cada accion de fase. Bajo VPN un timeout transitorio no es util —
  // el siguiente tick (otro mensaje WS o accion del usuario) lo corrige.
  const data = await apiFetch('/api/cmv40', { silent: true });
  if (!data) {
    // El fetch falló (timeout por el I/O alto del NAS al terminar un job con
    // mucha escritura, VPN caída, etc.). NO vaciar la lista: machacarla con
    // [] dejaba el sidebar en "0 jobs", y como al terminar el job el WS se
    // cierra puede no llegar ningún otro tick que lo recupere. Conservamos
    // lo último bueno, re-pintamos por si el DOM se limpió, y reintentamos
    // hasta que el I/O baje y el backend vuelva a responder.
    if (_cmv40SidebarList.length) _renderCMv40Sidebar();
    else _renderCMv40SidebarLoadError();
    if (!window._cmv40SidebarRetryTimer) {
      window._cmv40SidebarRetryTimer = setTimeout(() => {
        window._cmv40SidebarRetryTimer = null;
        refreshCMv40Sidebar();
      }, 4000);
    }
    return;
  }
  // Éxito: cancelar cualquier reintento pendiente de un fallo previo.
  if (window._cmv40SidebarRetryTimer) {
    clearTimeout(window._cmv40SidebarRetryTimer);
    window._cmv40SidebarRetryTimer = null;
  }
  _cmv40SidebarList = data.sessions || [];

  // Auto-resume del overlay: si hay un proyecto con running_phase != null,
  // no archivado, y NO hay nada abierto en Tab 3, abrirlo automáticamente
  // para que el usuario vea el modal de ejecución y el log en vivo. Cubre
  // el caso "Mac dormido toda la noche, hoy abro pestaña" — sin esto el
  // usuario tendría que recordar qué proyecto estaba corriendo y abrirlo
  // desde el sidebar manualmente.
  if (!_cmv40AutoResumeAttempted && openCMv40Projects.length === 0) {
    const running = _cmv40SidebarList.find(
      s => s.running_phase && !s.archived
    );
    if (running) {
      _cmv40AutoResumeAttempted = true;
      openCMv40Project(running);
      const niceName = running.source_mkv_name || running.id;
      const phaseLabel = (typeof CMV40_RUNNING_LABELS === 'object' && CMV40_RUNNING_LABELS)
        ? (CMV40_RUNNING_LABELS[running.running_phase] || running.running_phase)
        : running.running_phase;
      showToast(`🤖 Reanudando seguimiento: ${niceName} · ${phaseLabel}`, 'info');
    } else {
      // No hay nada que reanudar — marcamos el intento hecho para no
      // reevaluar en cada refresh del sidebar (es 1-shot por entrada al tab).
      _cmv40AutoResumeAttempted = true;
    }
  }
  // Capturar cambio del select de ordenación
  const sortSel = document.getElementById('cmv40-sidebar-sort');
  if (sortSel) {
    _cmv40SortKey = sortSel.value;
    if (!sortSel.dataset.bound) {
      sortSel.addEventListener('change', () => {
        _cmv40SortKey = sortSel.value;
        _renderCMv40Sidebar();
      });
      sortSel.dataset.bound = '1';
    }
  }
  _renderCMv40Sidebar();
}

/**
 * Estado del sidebar cuando el listado falla y NO hay nada cacheado que
 * mostrar. Sin esto el panel quedaba en blanco y era indistinguible de "no
 * tienes proyectos" — el usuario daba por perdidos proyectos que estaban
 * intactos en disco. Caso real: el payload de /api/cmv40 creció hasta pasarse
 * del timeout de 30 s del fetch y el sidebar aparecía vacío tras cada
 * reinicio del contenedor.
 */
function _renderCMv40SidebarLoadError() {
  const list = document.getElementById('cmv40-sidebar-list');
  if (!list) return;
  const count = document.getElementById('cmv40-count');
  if (count) count.textContent = '—';
  list.innerHTML = `
    <div class="empty-state" style="padding:24px 12px">
      <div class="empty-state-icon">🔌</div>
      <div>No se ha podido cargar la lista de proyectos</div>
      <div class="empty-state-desc" style="margin-top:6px">
        Tus proyectos siguen guardados. Reintentando cada 4 s…
      </div>
      <button class="btn btn-ghost btn-xs" style="margin-top:10px"
        onclick="refreshCMv40Sidebar()">↻ Reintentar ahora</button>
    </div>`;
}

function _renderCMv40Sidebar() {
  const list = document.getElementById('cmv40-sidebar-list');
  const count = document.getElementById('cmv40-count');
  if (!list) return;

  // Filtro de búsqueda
  const searchEl = document.getElementById('cmv40-sidebar-search');
  const searchTerm = (searchEl?.value || '').toLowerCase().trim();
  const norm = (s) => (s || '').toLowerCase().replace(/[^\w\s]/g, '');

  // Filtro de fase
  let filtered = _cmv40SidebarList.slice();
  if (_cmv40Filter === 'done') {
    filtered = filtered.filter(s => s.phase === 'done' || s.phase === 'validated');
  } else if (_cmv40Filter === 'error') {
    filtered = filtered.filter(s => !!s.error_message);
  } else if (_cmv40Filter === 'in_progress') {
    filtered = filtered.filter(s => !['done', 'validated', 'cancelled'].includes(s.phase) && !s.error_message);
  }
  if (searchTerm) {
    filtered = filtered.filter(s => {
      const hay = norm(s.source_mkv_name + ' ' + (CMV40_PHASE_LABELS[s.phase] || s.phase));
      return hay.includes(norm(searchTerm));
    });
  }

  // Ordenación
  const sortKey = _cmv40SortKey;
  const dir = _cmv40SortDir === 'asc' ? 1 : -1;
  filtered.sort((a, b) => {
    let av, bv;
    if (sortKey === 'name') {
      av = (a.source_mkv_name || '').toLowerCase();
      bv = (b.source_mkv_name || '').toLowerCase();
    } else if (sortKey === 'phase') {
      av = CMV40_PHASES_ORDER.indexOf(a.phase);
      bv = CMV40_PHASES_ORDER.indexOf(b.phase);
    } else {
      av = new Date(a.updated_at || 0).getTime();
      bv = new Date(b.updated_at || 0).getTime();
    }
    if (av < bv) return -dir;
    if (av > bv) return dir;
    return 0;
  });

  if (count) count.textContent = filtered.length;
  list.innerHTML = '';

  if (filtered.length === 0) {
    list.innerHTML = `
      <div class="empty-state" style="padding:24px 12px">
        <div class="empty-state-icon">🎨</div>
        <div>${searchTerm || _cmv40Filter !== 'all' ? 'Sin resultados' : 'Crea un proyecto para inyectar CMv4.0'}</div>
      </div>`;
    return;
  }

  filtered.forEach(s => {
    const isRunning  = !!s.running_phase;
    const phaseLabel = s.archived ? 'Archivado' : (CMV40_PHASE_LABELS[s.phase] || s.phase);
    const runningLabel = isRunning
      ? (CMV40_RUNNING_LABELS[s.running_phase] || s.running_phase)
      : null;
    const phaseIcon = s.archived
      ? '🗃️'
      : (s.error_message ? '⚠️' : (CMV40_PHASE_ICONS[s.phase] || '🎨'));
    const isOpen = openCMv40Projects.find(p => p.id === s.id);
    const isSelected = _cmv40SelectedSidebarId === s.id;
    const name = s.source_mkv_name.replace(/\.mkv$/i, '');

    const modDate = formatRelativeDate(s.updated_at || s.created_at);
    const modFull = new Date(s.updated_at || s.created_at).toLocaleString('es-ES', {
      day: '2-digit', month: '2-digit', year: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });

    const card = document.createElement('div');
    card.className = `session-card${isSelected ? ' selected' : ''}${isRunning ? ' is-running' : ''}`;
    card.dataset.sid = s.id;
    // Tooltip distinto cuando running_phase: indica claramente la fase
    // activa, no la última fase completada (que es lo que da phaseLabel).
    const badgeTooltip = isRunning ? `${runningLabel}` : phaseLabel;
    // Cuando está running, sustituimos el icono estático por un spinner
    // animado para que sea visualmente obvio en la lista que algo está
    // corriendo, sin necesidad de pasar el ratón.
    const badgeContent = isRunning
      ? `<span class="cmv40-card-spinner" aria-label="${escHtml(runningLabel)}"></span>`
      : phaseIcon;
    card.innerHTML = `
      <div class="session-card-row">
        <div class="session-card-status-badge${isRunning ? ' running' : ''}" data-tooltip="${escHtml(badgeTooltip)}">${badgeContent}</div>
        <div class="session-card-body">
          <div class="session-card-title" data-tooltip="${escHtml(name)}">${escHtml(name)}</div>
          <div class="session-card-meta">
            <div class="session-card-meta-row">
              <span class="meta-label">Fase</span>
              <span>${escHtml(phaseLabel)}</span>
            </div>
            <div class="session-card-meta-row">
              <span class="meta-label">Modif.</span>
              <span class="relative-date" data-iso="${s.updated_at || s.created_at || ''}"
                data-tooltip="${escHtml('Modificado: ' + modFull)}">${escHtml(modDate)}</span>
            </div>
          </div>
        </div>
        ${isOpen ? '<span class="session-item-badge">abierto</span>' : ''}
      </div>
      <div class="session-card-actions">
        <button class="btn btn-primary btn-sm" onclick="event.stopPropagation();_cmv40OpenSelected('${s.id}')"
          data-tooltip="Abrir este proyecto">📂 Abrir</button>
        <button class="btn btn-danger btn-sm" onclick="event.stopPropagation();_cmv40DeleteFromSidebar('${s.id}')"
          data-tooltip="Eliminar permanentemente">🗑️ Eliminar</button>
      </div>`;
    const row = card.querySelector('.session-card-row');
    row.onclick = () => _cmv40ToggleSidebarSelection(s.id);
    row.ondblclick = () => _cmv40OpenSelected(s.id);
    list.appendChild(card);
  });
}

function _cmv40ToggleSortDir() {
  _cmv40SortDir = _cmv40SortDir === 'asc' ? 'desc' : 'asc';
  const btn = document.getElementById('cmv40-sort-dir');
  if (btn) btn.textContent = _cmv40SortDir === 'asc' ? '↑' : '↓';
  _renderCMv40Sidebar();
}

function _cmv40FilterClick(btn) {
  document.querySelectorAll('#sidebar-tab-3 .sb-filter-pill').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _cmv40Filter = btn.dataset.filter;
  _renderCMv40Sidebar();
}

function _cmv40ToggleSidebarSelection(sid) {
  _cmv40SelectedSidebarId = (_cmv40SelectedSidebarId === sid) ? null : sid;
  document.querySelectorAll('#cmv40-sidebar-list .session-card').forEach(card => {
    card.classList.toggle('selected', card.dataset.sid === _cmv40SelectedSidebarId);
  });
}

function _cmv40OpenSelected(sid) {
  const s = _cmv40SidebarList.find(x => x.id === sid);
  if (s) openCMv40Project(s);
}

async function _cmv40DeleteFromSidebar(sid) {
  const s = _cmv40SidebarList.find(x => x.id === sid);
  if (!s) return;
  showConfirm(
    '¿Eliminar proyecto?',
    `Se eliminará "${s.source_mkv_name}" y sus artefactos intermedios. Esta acción no se puede deshacer.`,
    async () => {
      await apiFetch(`/api/cmv40/${sid}?clean_artifacts=true`, { method: 'DELETE' });
      // Cerrar subtab si estaba abierto
      const open = openCMv40Projects.find(p => p.id === sid);
      if (open) closeCMv40Project(sid);
      if (_cmv40SelectedSidebarId === sid) _cmv40SelectedSidebarId = null;
      refreshCMv40Sidebar();
    },
    'Eliminar',
  );
}

// ── Chart interactivo de sincronización (Fase D) ─────────────────

async function _loadCMv40SyncChart(project) {
  const pid = project.id;
  // Skip defensivo: si el canvas del chart no existe en el DOM (p.ej. Fase D
  // omitida por trusted → body muestra banner sin canvas), no hay donde
  // renderizar y el fetch solo provocaría regeneración innecesaria del
  // per_frame_data.json en backend.
  if (!document.getElementById(`cmv40-chart-wrap-${pid}`)) return;
  // Guard anti-thundering-herd: cada re-render de la phase card llamaba aquí.
  // Sin flag, N renders antes de que resuelva la promesa lanzaban N fetches
  // paralelos → N `dovi_tool export` concurrentes en backend → I/O thrash.
  if (project._syncDataLoading) return;
  if (!project.syncData) {
    project._syncDataLoading = true;
    try {
      // Sin rango: el backend devuelve la película entera reducida a cubos.
      const data = await apiFetch(`/api/cmv40/${pid}/sync-data`);
      if (!data) return;
      project.syncData = data;
    } finally {
      project._syncDataLoading = false;
    }
  }
  _renderCMv40Chart(project);
  _renderCMv40SyncStats(project);
  _renderCMv40SyncControls(project);
  _renderCMv40Confidence(project);
}

function _renderCMv40SyncStats(project) {
  const d = project.syncData;
  const s = project.session;
  const pid = project.id;
  const container = document.getElementById(`cmv40-sync-stats-${pid}`);
  if (!container) return;
  // Frame counts autoritativos de la sesión (reflejan correcciones ya aplicadas).
  const srcFrames = (s && s.source_frame_count) || d.source_frames;
  const tgtFrames = (s && s.target_frame_count) || d.target_frames;
  const delta = (s && s.sync_delta != null) ? s.sync_delta : (tgtFrames - srcFrames);
  const suggested = d.suggested_offset || {};

  container.innerHTML = `
    <div class="cmv40-sync-row">
      <div><span class="sync-label">Frames origen:</span> <b>${srcFrames.toLocaleString()}</b></div>
      <div><span class="sync-label">Frames target:</span> <b>${tgtFrames.toLocaleString()}</b></div>
      <div><span class="sync-label">Diferencia:</span> <b style="color:${delta===0?'var(--green)':'var(--orange)'}">${delta > 0 ? '+' : ''}${delta}</b></div>
    </div>
    ${suggested.offset !== undefined && suggested.offset !== 0 ? `
      <div class="banner info" style="margin-top:10px">
        <span class="banner-icon">🔍</span>
        <span>Offset detectado automáticamente: <b>${suggested.offset > 0 ? '+' : ''}${suggested.offset} frames</b></span>
      </div>` : ''}
    ${_cmv40SheetSyncBannerHTML(d.sheet_sync)}
  `;
}

/**
 * Contraste entre el offset documentado en la hoja de DoviTools y el que
 * acaba de medir la cross-correlation. Son dos medidas independientes del
 * mismo desfase: coincidir es confirmación fuerte de que el bin es el
 * correcto; divergir es la señal más temprana de que el bin corresponde a
 * otra edición o a otro corte.
 */
function _cmv40SheetSyncBannerHTML(sheetSync) {
  if (!sheetSync || sheetSync.sheet_offset === null
      || sheetSync.sheet_offset === undefined) return '';
  const sheetVal = sheetSync.sheet_offset;
  const sign = v => (v > 0 ? '+' : '') + v;
  const sheetTxt = sheetSync.sheet_offset_text
    ? `<b>${escHtml(sheetSync.sheet_offset_text)}</b>`
    : `<b>${sign(sheetVal)} frames</b>`;
  const src = sheetSync.match_title
    ? ` (fila «${escHtml(sheetSync.match_title)}»)` : '';

  if (sheetSync.agrees === true) {
    return `<div class="banner success" style="margin-top:8px">
      <span class="banner-icon">✅</span>
      <span>La hoja de DoviTools documenta el mismo desfase ${sheetTxt}${src} —
        confirmación independiente de que el bin está bien alineado.</span>
    </div>`;
  }
  if (sheetSync.agrees === false && sheetSync.sign_flipped) {
    return `<div class="banner info" style="margin-top:8px">
      <span class="banner-icon">↔️</span>
      <span>La hoja documenta ${sheetTxt}${src}: misma magnitud que el detectado
        (${sign(sheetSync.detected_offset)}) pero con el signo al revés. Suele ser
        la convención de la fila, no un desajuste — comprueba el chart.</span>
    </div>`;
  }
  if (sheetSync.agrees === false) {
    return `<div class="banner warning" style="margin-top:8px">
      <span class="banner-icon">⚠️</span>
      <span>La hoja documenta ${sheetTxt}${src} pero aquí se ha detectado
        <b>${sign(sheetSync.detected_offset)} frames</b> (Δ ${sign(sheetSync.delta)}).
        Revisa el gráfico antes de inyectar: puede ser un bin de otra edición.</span>
    </div>`;
  }
  // Sin offset detectado con el que comparar (aún no hay per-frame data).
  return `<div class="banner info" style="margin-top:8px">
    <span class="banner-icon">📋</span>
    <span>La hoja de DoviTools documenta un desfase de ${sheetTxt}${src} para este título.</span>
  </div>`;
}

function _renderCMv40Confidence(project) {
  const d = project.syncData;
  const pid = project.id;
  const container = document.getElementById(`cmv40-confidence-${pid}`);
  if (!container) return;
  const conf = d.confidence || {};
  const pct = conf.confidence_pct || 0;
  const rating = conf.rating || 'insufficient_data';
  const ratingColor = {
    'excellent': 'var(--green)',
    'good':      'var(--green)',
    'moderate':  'var(--orange)',
    'poor':      'var(--red)',
    'insufficient_data': 'var(--text-3)',
    'no_variance':       'var(--text-3)',
  }[rating];
  const ratingLabel = {
    'excellent': 'Excelente',
    'good':      'Buena',
    'moderate':  'Moderada',
    'poor':      'Baja',
    'insufficient_data': 'Datos insuficientes',
    'no_variance':       'Sin variación',
  }[rating];
  container.innerHTML = `
    <div class="cmv40-confidence-panel" style="border-color:${ratingColor}; margin-top:16px">
      <div class="cmv40-confidence-header">
        <span class="cmv40-confidence-label">Confianza de sincronización</span>
        <span class="cmv40-confidence-value" style="color:${ratingColor}">${pct}%</span>
        <span class="cmv40-confidence-rating" style="color:${ratingColor}">${ratingLabel}</span>
      </div>
      <div class="cmv40-confidence-bar">
        <div class="cmv40-confidence-fill" style="width:${pct}%; background:${ratingColor}"></div>
        <div class="cmv40-confidence-threshold" style="left:85%" data-tooltip="Umbral mínimo 85%">·</div>
      </div>
      <div class="cmv40-confidence-reason">${escHtml(conf.reason || '')}</div>
      <div style="font-size:10px; color:var(--text-3); margin-top:4px">
        Mide la correlación de forma entre MaxCLL origen y target. Insensible a diferencias de valor absoluto — las curvas pueden no coincidir exactamente pero sí seguir el mismo patrón temporal.
      </div>
    </div>
  `;
}

function _renderCMv40SyncControls(project) {
  const pid = project.id;
  const s = project.session;
  const d = project.syncData;
  const container = document.getElementById(`cmv40-sync-controls-${pid}`);
  if (!container) return;
  // Read-only mode: la sesión ya pasó Fase D (phase index > sync_verified).
  // Mostramos solo controles de zoom + inputs de rango para navegar el plot,
  // nada de form de corrección ni botones de apply/confirmar.
  const phaseIdx  = CMV40_PHASES_ORDER.indexOf(s.phase);
  const dDoneIdx  = CMV40_PHASES_ORDER.indexOf('sync_verified');
  const readOnly  = phaseIdx > dDoneIdx;
  const delta = (s && s.sync_delta != null) ? s.sync_delta : (d.target_frames - d.source_frames);
  const suggested = d.suggested_offset || {};
  const hasSyncConfig = !!s.sync_config;
  // Confianza y criterio para habilitar "Confirmar". El criterio se LEE del
  // backend (`sync_gate`), que es quien lo aplica en POST /mark-synced. Antes
  // se calculaba aquí y solo aquí: el endpoint aceptaba cualquier cosa, así
  // que un app.js viejo en caché se lo saltaba sin dejar rastro.
  const conf = d.confidence || {};
  const confPct = conf.confidence_pct || 0;
  const confOk  = !!conf.threshold_ok;
  // Fallback local para respuestas cacheadas de antes de `sync_gate` (mismo
  // patrón que los helpers del plan). Es UNA implementación, no una réplica:
  // `_cmv40SyncGateLocal` la comparte con quien la necesite.
  const gate = d.sync_gate || _cmv40SyncGateLocal(delta, confOk, confPct);
  const canConfirm = !!gate.ok;
  const confirmReason = gate.reason || '';
  // Framerate real del vídeo origen (fallback 23.976)
  const FPS = s.source_fps || 23.976;
  const totalFrames = d.source_frames || d.target_frames || 0;
  if (!project.chartRange) {
    // Default: primeros 30s — la zona típica donde hay logos y desfases
    project.chartRange = { start: 0, end: Math.min(Math.round(30 * FPS), totalFrames) };
  }
  const currentRange = project.chartRange;

  // Detectar qué preset está activo (si el rango coincide exactamente)
  const presets = [
    { key: '30s',   start: 0, end: Math.min(Math.round(30 * FPS), totalFrames),       label: '30 s' },
    { key: '1min',  start: 0, end: Math.min(Math.round(60 * FPS), totalFrames),       label: '1 min' },
    { key: '5min',  start: 0, end: Math.min(Math.round(5 * 60 * FPS), totalFrames),   label: '5 min' },
    { key: '30min', start: 0, end: Math.min(Math.round(30 * 60 * FPS), totalFrames),  label: '30 min' },
    { key: 'all',   start: 0, end: totalFrames,                                        label: 'Todo' },
  ];
  const activeKey = presets.find(p => p.start === currentRange.start && p.end === currentRange.end)?.key;

  const presetBtns = presets.map(p => `
    <button class="btn btn-ghost btn-xs cmv40-zoom-preset${activeKey === p.key ? ' active' : ''}"
      onclick="_cmv40SetRange('${pid}', ${p.start}, ${p.end})">${p.label}</button>
  `).join('');

  const zoomRowHtml = `
    <div class="cmv40-zoom-row">
      <span class="section-subtitle">Zoom</span>
      ${presetBtns}
      <span class="cmv40-range-inputs">
        <label>Desde frame:
          <input type="number" id="cmv40-range-start-${pid}" value="${currentRange.start}" min="0" max="${totalFrames}"
            onchange="_cmv40ApplyRangeFromInputs('${pid}')">
        </label>
        <label>Hasta frame:
          <input type="number" id="cmv40-range-end-${pid}" value="${currentRange.end}" min="0" max="${totalFrames}"
            onchange="_cmv40ApplyRangeFromInputs('${pid}')">
        </label>
      </span>
    </div>`;

  // Read-only: solo zoom/rango, sin form de corrección.
  if (readOnly) {
    container.innerHTML = `
      ${zoomRowHtml}
      <div style="margin-top:10px; padding:8px 12px; background:var(--surface-2); border-radius:6px; font-size:11px; color:var(--text-3)">
        ${hasSyncConfig
          ? `Corrección aplicada en su día — el gráfico se muestra en modo solo lectura.`
          : 'Sincronización confirmada sin corrección (Δ era 0).'}
      </div>`;
    return;
  }

  container.innerHTML = `
    ${zoomRowHtml}

    <div class="section-subtitle" style="margin-top:16px; margin-bottom:4px">Corrección ${hasSyncConfig ? 'adicional' : 'manual'}</div>
    <div style="font-size:11px; color:var(--text-3); margin-bottom:8px">
      ${hasSyncConfig
        ? 'Estos valores se <b>sumarán</b> a la corrección ya aplicada. El Δ actual del gráfico indica cuánto falta por alinear.'
        : 'Los valores se aplican desde el target original.'}
    </div>
    <div class="cmv40-sync-form">
      <label>Eliminar N frames al inicio del target:
        <input type="number" id="cmv40-remove-${pid}" value="${delta > 0 ? delta : 0}" min="0" style="width:80px"
          oninput="_cmv40UpdateExpectedDelta('${pid}', ${delta})">
      </label>
      <label>Duplicar primer frame N veces:
        <input type="number" id="cmv40-duplicate-${pid}" value="${delta < 0 ? Math.abs(delta) : 0}" min="0" style="width:80px"
          oninput="_cmv40UpdateExpectedDelta('${pid}', ${delta})">
      </label>
    </div>
    <div style="margin-top:10px; padding:10px 12px; background:var(--surface-2); border-radius:6px; font-size:12px">
      <span style="color:var(--text-3)">Δ después de aplicar:</span>
      <b id="cmv40-expected-delta-${pid}" style="margin-left:6px">—</b>
      <span style="color:var(--text-3); margin-left:12px; font-size:11px">
        (remove ${delta > 0 ? delta : 0} · dup ${delta < 0 ? Math.abs(delta) : 0} dejaría Δ=0)
      </span>
    </div>
    <div style="display:flex; gap:10px; margin-top:16px; flex-wrap:wrap">
      <button class="btn btn-ghost btn-md" onclick="cmv40DoApplySync('${pid}')">✏️ Aplicar corrección</button>
      ${hasSyncConfig ? `<button class="btn btn-danger btn-md" onclick="cmv40DoResetSync('${pid}')"
          data-tooltip="Descartar corrección y volver al target original">↩️ Resetear al original</button>` : ''}
      <button class="btn btn-primary btn-md" onclick="cmv40DoSkipSync('${pid}')"
        ${canConfirm ? '' : 'disabled data-tooltip="' + confirmReason + '"'}>✓ Confirmar sync y continuar</button>
    </div>
    <div style="margin-top:8px; font-size:11px; color:var(--text-3)">
      Δ actual: <b style="color:${delta===0?'var(--green)':'var(--orange)'}">${delta > 0 ? '+' : ''}${delta} frames</b>
      · Confianza: <b style="color:${confOk ? 'var(--green)' : 'var(--orange)'}">${confPct}%</b>
      ${canConfirm ? ' — <b style="color:var(--green)">listo para continuar</b>' : ' — <b style="color:var(--orange)">' + confirmReason + '</b>'}
    </div>
  `;
  // Inicializar preview del Δ esperado
  _cmv40UpdateExpectedDelta(pid, delta);
}

function _cmv40UpdateExpectedDelta(pid, currentDelta) {
  const r = parseInt(document.getElementById(`cmv40-remove-${pid}`)?.value) || 0;
  const d = parseInt(document.getElementById(`cmv40-duplicate-${pid}`)?.value) || 0;
  // Aplicar remove reduce delta; duplicate lo aumenta
  const expected = currentDelta - r + d;
  const el = document.getElementById(`cmv40-expected-delta-${pid}`);
  if (!el) return;
  const sign = expected > 0 ? '+' : '';
  const color = expected === 0 ? 'var(--green)' : 'var(--orange)';
  el.innerHTML = `<span style="color:${color}">${sign}${expected} frames</span>`;
}

/** Cambia el rango visible del chart y pide esa ventana al servidor.
 *
 *  Antes el frontend se traía la película entera —24 MB para un UHD— y
 *  filtraba en cliente. Ahora el backend reduce a cubos (min y max por cubo,
 *  no la media: un promedio se come los picos que delatan el desfase) y sirve
 *  la ventana pedida, así que un zoom fino recibe el dato EXACTO en vez de
 *  filtrar una muestra gruesa. */
async function _cmv40SetRange(pid, start, end) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project) return;
  project.chartRange = { start, end };
  // Pinta ya con lo que hay (respuesta instantánea al clic) y refina cuando
  // llegue la ventana.
  _renderCMv40Chart(project);
  _renderCMv40SyncControls(project);
  if (project._syncRangeLoading) return;
  project._syncRangeLoading = true;
  try {
    const data = await apiFetch(
      `/api/cmv40/${pid}/sync-data?desde=${start}&hasta=${end}`, { silent: true });
    if (!data) return;
    // El rango pudo cambiar mientras la petición volvía (clics rápidos entre
    // presets): si ya no es el que se pidió, se descarta.
    if (project.chartRange.start !== start || project.chartRange.end !== end) return;
    project.syncData = data;
    _renderCMv40Chart(project);
    _renderCMv40SyncControls(project);
  } finally {
    project._syncRangeLoading = false;
  }
}

function _cmv40ApplyRangeFromInputs(pid) {
  const start = parseInt(document.getElementById(`cmv40-range-start-${pid}`).value) || 0;
  const end = parseInt(document.getElementById(`cmv40-range-end-${pid}`).value) || 0;
  if (end <= start) {
    showToast('El frame final debe ser mayor que el inicial', 'warning');
    return;
  }
  _cmv40SetRange(pid, start, end);
}

async function cmv40DoResetSync(pid) {
  showConfirm(
    '¿Descartar corrección?',
    'Se borrará la corrección aplicada y el RPU target volverá a su estado original. El gráfico mostrará de nuevo el desfase inicial para que puedas empezar de cero.',
    async () => {
      const data = await apiFetch(`/api/cmv40/${pid}/reset-sync`, { method: 'POST' });
      if (data) {
        const project = openCMv40Projects.find(p => p.id === pid);
        if (project) {
          project.syncData = null;
          _cmv40AssignSession(project, data);
          project.chartRange = null;  // volver al zoom por defecto
          _updateCMv40Panel(project);
        }
        showToast('Corrección descartada', 'info');
      }
    },
    'Descartar corrección',
  );
}

async function cmv40DoApplySync(pid) {
  const remove = parseInt(document.getElementById(`cmv40-remove-${pid}`).value) || 0;
  const dup = parseInt(document.getElementById(`cmv40-duplicate-${pid}`).value) || 0;
  if (remove === 0 && dup === 0) {
    showToast('Indica un valor para eliminar o duplicar', 'warning');
    return;
  }
  const config = {};
  if (remove > 0) config.remove = [`0-${remove - 1}`];
  if (dup > 0) config.duplicate = [{ source: 0, offset: 0, length: dup }];
  const data = await apiFetch(`/api/cmv40/${pid}/apply-sync`, {
    method: 'POST',
    body: JSON.stringify({ editor_config: config }),
  });
  if (data) {
    showToast(`Corrección aplicada. Nuevo Δ = ${data.sync_delta > 0 ? '+' : ''}${data.sync_delta}`, 'success');
    const project = openCMv40Projects.find(p => p.id === pid);
    if (project) {
      project.syncData = null;  // forzar recarga
      _cmv40AssignSession(project, data);
      if (!project.expandedPhases) project.expandedPhases = {};
      project.expandedPhases['D'] = true;  // mantener la fase D visible
      _updateCMv40Panel(project);
      // Los inputs se re-renderizan pre-rellenados con el nuevo delta
      // (evita aplicar dos veces el mismo valor por despiste)
    }
  }
}

async function cmv40DoSkipSync(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}/mark-synced`, { method: 'POST' });
  if (data) {
    showToast('Sync confirmado', 'success');
    const project = openCMv40Projects.find(p => p.id === pid);
    if (project) {
      _cmv40AssignSession(project, data);
      _updateCMv40Panel(project);
      refreshCMv40Sidebar();
      // Si auto está activo, disparar el siguiente tramo (inject → remux → validate)
      if (project.autoContinue) {
        _cmv40MaybeAutoAdvance(project);
      }
    }
  }
}

// ── Chart Canvas (custom, sin librerías) ─────────────────────────

function _renderCMv40Chart(project) {
  const pid = project.id;
  const canvas = document.getElementById(`cmv40-chart-${pid}`);
  if (!canvas) return;
  const allData = project.syncData?.data || [];
  if (allData.length === 0) return;

  // Framerate real del vídeo origen
  const FPS = project.session.source_fps || 23.976;
  // totalFrames real de la película (NO es allData.length por muestreo)
  // No usar Math.max(...array): el spread supera el límite de argumentos (~65k)
  // y lanza "Maximum call stack size exceeded" con arrays grandes (155k frames).
  const totalFrames = project.syncData.source_frames
    || (allData.reduce((m, p) => Math.max(m, p.frame || 0), 0) + 1);
  if (!project.chartRange) {
    project.chartRange = { start: 0, end: Math.min(Math.round(30 * FPS), totalFrames) };
  }
  const { start, end } = project.chartRange;
  // Filtrar por número de frame real (no por índice del array)
  const data = allData.filter(p => p.frame >= start && p.frame < end);
  if (data.length === 0) return;

  const ctx = canvas.getContext('2d');
  const W = canvas.width;
  const H = canvas.height;
  const padding = { top: 20, right: 20, bottom: 40, left: 60 };
  const plotW = W - padding.left - padding.right;
  const plotH = H - padding.top - padding.bottom;

  // Reduce en vez de spread — evita "Max call stack" con arrays > ~65k
  let srcMax = 0, tgtMax = 0;
  for (let i = 0; i < data.length; i++) {
    const s = data[i].src_maxcll || 0;
    const t = data[i].tgt_maxcll || 0;
    if (s > srcMax) srcMax = s;
    if (t > tgtMax) tgtMax = t;
  }
  // El backend ya emite el MÁXIMO de cada cubo, así que el techo del eje no
  // cambia por la banda (su mínimo siempre queda por debajo).
  const yMax = Math.max(srcMax, tgtMax, 100) * 1.1;
  // Ancho en frames del rango visible (para mapeo X)
  const rangeSpan = end - start;

  // Fondo
  ctx.fillStyle = '#1a1a1a';
  ctx.fillRect(0, 0, W, H);

  // Grid horizontal
  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.lineWidth = 1;
  ctx.font = '10px sans-serif';
  ctx.fillStyle = 'rgba(255,255,255,0.5)';
  for (let i = 0; i <= 5; i++) {
    const y = padding.top + (plotH * i / 5);
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(padding.left + plotW, y);
    ctx.stroke();
    const val = (yMax * (1 - i / 5)).toFixed(0);
    ctx.fillText(`${val} PQ`, 4, y + 3);
  }
  // Eje X (frames + tiempo) — 6 labels bien espaciados
  const NUM_X_LABELS = 6;
  ctx.textAlign = 'center';
  for (let i = 0; i <= NUM_X_LABELS; i++) {
    const x = padding.left + (plotW * i / NUM_X_LABELS);
    const frame = Math.round(start + (rangeSpan * i / NUM_X_LABELS));
    const mm = Math.floor(frame / FPS / 60);
    const ss = Math.floor((frame / FPS) % 60).toString().padStart(2, '0');
    // Marca del tick
    ctx.strokeStyle = 'rgba(255,255,255,0.2)';
    ctx.beginPath();
    ctx.moveTo(x, padding.top + plotH);
    ctx.lineTo(x, padding.top + plotH + 4);
    ctx.stroke();
    // Labels
    ctx.fillStyle = 'rgba(255,255,255,0.7)';
    ctx.fillText(`${mm}:${ss}`, x, H - 22);
    ctx.fillStyle = 'rgba(255,255,255,0.4)';
    ctx.font = '9px sans-serif';
    ctx.fillText(`f ${frame.toLocaleString()}`, x, H - 8);
    ctx.font = '10px sans-serif';
  }
  ctx.textAlign = 'left';

  // Helper: frame absoluto → posición X en el canvas
  const frameToX = (frame) => padding.left + (plotW * (frame - start) / rangeSpan);

  // Banda min-max de cada cubo, cuando la ventana viene reducida por el
  // backend. Sin ella, con ~160 frames por punto las dos curvas quedan
  // convertidas en envolventes superiores parecidas y se puede PERDER la
  // desalineación que esta gráfica existe para ver. La banda enseña cuánto
  // recorrido hay dentro de cada cubo.
  const reducido = !!project.syncData.downsampled;
  if (reducido) {
    const banda = (clave, color) => {
      ctx.fillStyle = color;
      data.forEach((d) => {
        const lo = d[clave + '_min'];
        if (lo === undefined) return;
        const x = frameToX(d.frame);
        const yTop = padding.top + plotH - (plotH * (d[clave] || 0) / yMax);
        const yBot = padding.top + plotH - (plotH * lo / yMax);
        ctx.fillRect(x - 0.5, yTop, 1.5, Math.max(1, yBot - yTop));
      });
    };
    banda('tgt_maxcll', 'rgba(59, 130, 246, 0.28)');
    banda('src_maxcll', 'rgba(239, 68, 68, 0.28)');
  }

  // Curva target (azul) — se dibuja primero, más gruesa y con cierta transparencia
  ctx.strokeStyle = 'rgba(59, 130, 246, 0.85)';
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  data.forEach((d, i) => {
    const x = frameToX(d.frame);
    const y = padding.top + plotH - (plotH * (d.tgt_maxcll || 0) / yMax);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Curva source (rojo) — encima, más fina y punteada para que se vea cuando coincide
  ctx.strokeStyle = '#ef4444';
  ctx.lineWidth = 1.2;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  data.forEach((d, i) => {
    const x = frameToX(d.frame);
    const y = padding.top + plotH - (plotH * (d.src_maxcll || 0) / yMax);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.setLineDash([]);

  // Leyenda — origen con guiones (reflejando cómo se dibuja)
  ctx.fillStyle = '#3b82f6';
  ctx.fillRect(padding.left + 10, 7, 14, 3);
  ctx.fillStyle = 'rgba(255,255,255,0.8)';
  ctx.fillText('RPU target (CMv4.0)', padding.left + 30, 12);
  ctx.strokeStyle = '#ef4444';
  ctx.lineWidth = 1.5;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  ctx.moveTo(padding.left + 180, 8);
  ctx.lineTo(padding.left + 196, 8);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = 'rgba(255,255,255,0.8)';
  ctx.fillText('MKV origen (CMv2.9)', padding.left + 202, 12);
  // Info de rango prominente (arriba a la derecha)
  const startSec = start / FPS, endSec = end / FPS;
  const fmtTime = (s) => {
    const mm = Math.floor(s / 60), ss = Math.floor(s % 60).toString().padStart(2, '0');
    return `${mm}:${ss}`;
  };
  ctx.textAlign = 'right';
  ctx.fillStyle = 'rgba(255,255,255,0.9)';
  ctx.font = '11px sans-serif';
  ctx.fillText(`Rango: ${fmtTime(startSec)} — ${fmtTime(endSec)}`, W - padding.right, 14);
  ctx.fillStyle = 'rgba(255,255,255,0.5)';
  ctx.font = '10px sans-serif';
  ctx.fillText(`(${(end - start).toLocaleString()} de ${totalFrames.toLocaleString()} frames · ${FPS.toFixed(2)} fps)`, W - padding.right, 28);
  ctx.textAlign = 'left';

  // Hover handler
  canvas.onmousemove = (e) => {
    const rect = canvas.getBoundingClientRect();
    const scaleX = W / rect.width;
    const mx = (e.clientX - rect.left) * scaleX;
    if (mx < padding.left || mx > padding.left + plotW) return;
    // Posición X → frame absoluto
    const absFrame = Math.round(start + ((mx - padding.left) / plotW) * rangeSpan);
    // Buscar el datapoint más cercano al frame
    const d = data.reduce((closest, p) =>
      Math.abs(p.frame - absFrame) < Math.abs(closest.frame - absFrame) ? p : closest,
      data[0]
    );
    if (!d) return;
    const tooltip = document.getElementById(`cmv40-chart-tooltip-${project.id}`);
    if (tooltip) {
      tooltip.style.display = '';
      tooltip.style.left = `${e.clientX - rect.left + 10}px`;
      tooltip.style.top  = `${e.clientY - rect.top - 30}px`;
      const mm = Math.floor(absFrame / FPS / 60);
      const ss = Math.floor((absFrame / FPS) % 60).toString().padStart(2, '0');
      tooltip.innerHTML = `Frame ${absFrame.toLocaleString()} (${mm}:${ss})<br>
        <span style="color:#ef4444">Origen: ${(d.src_maxcll || 0).toFixed(0)} PQ</span><br>
        <span style="color:#3b82f6">Target: ${(d.tgt_maxcll || 0).toFixed(0)} PQ</span>`;
    }
  };
  canvas.onmouseleave = () => {
    const tooltip = document.getElementById(`cmv40-chart-tooltip-${project.id}`);
    if (tooltip) tooltip.style.display = 'none';
  };
}
