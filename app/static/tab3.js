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
  'created':         'mas',
  'source_analyzed': 'lupa',
  'target_provided': 'diana',
  'extracted':       'tijeras',
  'sync_verified':   'grafico',
  'sync_corrected':  'grafico',
  'injected':        'inyectar',
  'remuxed':         'caja',
  'validated':       'check',
  'done':            'check',
  'error':           'cruz',
  'cancelled':       'pausa',
};

const MAX_CMV40_PROJECTS = 5;

// Label humano por nombre de fase (running_phase)
const CMV40_RUNNING_LABELS = {
  'analyze_source':  tr('tab3.fase_a_analizando_mkv_origen'),
  'target_rpu_mkv':  tr('tab3.fase_b_extrayendo_rpu_target'),
  'target_rpu_drive':tr('tab3.fase_b_descargando_rpu_del_repositorio'),
  'target_rpu_path': tr('tab3.fase_b_cargando_rpu_de_carpeta'),
  'extract':         tr('tab3.fase_c_extrayendo_bl_el_y'),
  'sync_correct':    tr('tab3.fase_e_aplicando_correccion_de_sincronizacion'),
  'inject':          tr('tab3.fase_f_inyectando_rpu_en_el'),
  'remux':           tr('tab3.fase_g_remuxando_mkv_final'),
  'validate':        tr('tab3.fase_h_validando_mkv_final'),
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
    case 'created':          return tr('tab3.fase_a_analizando_mkv_origen');
    case 'source_analyzed':  return tr('tab3.fase_b_preparando_rpu_target');
    case 'target_provided':  return tr('tab3.fase_c_separando_capas');
    case 'extracted':        return trust ? tr('tab3.fase_f_inyectando_rpu_drop_in') : tr('tab3.fase_d_revision_visual');
    case 'sync_verified':    return tr('tab3.fase_f_inyectando_rpu');
    case 'sync_corrected':   return tr('tab3.fase_f_inyectando_rpu');
    case 'injected':         return tr('tab3.fase_g_ensamblando_mkv');
    case 'remuxed':          return tr('tab3.fase_h_validando_resultado');
    case 'validated':        return tr('tab3.fase_h_guardando');
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
    key: 'PREFLIGHT', icon: 'lupaOnda', title: tr('tab3.pre_flight_validacion_rapida'),
    what: tr('tab3.sniff_dv_del_mkv_origen_descarga'),
    etaSecs: 45,
    forcedStatus: preflightStatus,
  });

  steps.push({
    key: 'A', icon: 'lupa', title: tr('tab3.fase_a_analizar_mkv_origen'),
    what: tr('tab3.ffmpeg_copia_el_hevc_dovi_tool'),
    etaSecs: etaA,
  });
  // Fase B: si el pre-flight ya descargó/copió/extrajo el bin, aquí se reusa
  // del workdir y solo se re-evalúan los trust gates con los datos del source
  // recién extraído en Fase A. Texto refleja ese rol real.
  const bWhat = s.target_rpu_source === 'drive' ? tr('tab3.reusa_el_bin_del_workdir_descargado')
              : s.target_rpu_source === 'mkv' ? tr('tab3.reusa_el_rpu_del_workdir_extraido')
              : tr('tab3.reusa_el_bin_del_workdir_copiado');
  steps.push({
    key: 'B', icon: 'diana', title: tr('tab3.fase_b_preparar_rpu_target'),
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
    gateBCLabel = tr('tab3.incompatible_abortada');
  } else if (curIdxForGate < targetProvidedIdx) {
    gateBCLabel = tr('tab3.lbl_pendiente');
  } else if (s.target_trust_ok) {
    // Sin icono: esto es el `customLabel` de un paso y la timeline lo pinta
    // con `escHtml` —hace bien, porque otros labels traen datos—, así que un
    // SVG aquí se lee como código. El estado ya lo dice el icono del paso.
    gateBCLabel = 'trusted';
  } else if (failingGates.length) {
    gateBCLabel = tr('tab3.gate_revision_manual', {p1: failingGates.length, p2: failingGates.length > 1 ? 's' : ''});
  } else {
    gateBCLabel = tr('tab3.flujo_manual');
  }
  const gateBCWhat = s.compat_warning
    ? s.compat_warning.slice(0, 140) + (s.compat_warning.length > 140 ? '…' : '')
    : tr('tab3.comparacion_target_vs_source_rpu_frames');
  steps.push({
    key: 'GATE_BC', icon: 'escudo', title: tr('tab3.validaciones_trust_gates_compatibilidad'),
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
      ? tr('tab3.omitida_drop_in_fel_sin_demux')
      : tr('tab3.omitida_target_trusted_no_se_necesitan');
    cForcedStatus = 'skipped';
    cLabel = tr('tab3.omitida_drop_in_2');
  } else {
    cWhat = (wf === 'p8') ? tr('tab3.workflow_p8_sin_demux') + (trust ? ' ' + tr('tab3.per_frame_omitido') : tr('tab3.genera_per_frame_data'))
                          : 'dovi_tool demux → BL' + (wf === 'p7_fel' ? ' + EL' : '') + (trust ? ' ' + tr('tab3.per_frame_omitido') : ' + per-frame data');
  }
  steps.push({
    key: 'C', icon: 'tijeras', title: tr('tab3.fase_c_demux_per_frame'),
    what: cWhat, etaSecs: cFullySkipped ? 0 : etaC,
    forcedStatus: cForcedStatus, customLabel: cLabel,
  });
  steps.push({
    key: 'D', icon: 'grafico', title: tr('tab3.fase_d_verificar_sincronizacion'),
    what: trust
      ? tr('tab3.omitida_gates_validaron_frame_count_l5_l6')
      : tr('tab3.chart_interactivo_de_sincronizacion_alinear_las'),
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
    eLabel = tr('tab3.omitida_gates_0');
  } else if (hasSyncCfg) {
    // Corrección aplicada; _cmv40StepStatus decide done/running/pending según
    // la fase actual. El customLabel se usa cuando esté done.
    eLabel = tr('tab3.lbl_aplicada');
    eStatus = null;
  } else if (pastSyncVerified) {
    // Usuario confirmó sync sin corrección — Δ era 0 tras revisión.
    eStatus = 'skipped';
    eLabel = tr('tab3.omitida_0_confirmado');
  }
  // (caso restante: no-trusted + sin sync_config + pre-sync_verified →
  //  eStatus/eLabel null → _cmv40StepStatus decide 'pending'.)
  const eWhat = hasSyncCfg
    ? tr('tab3.dovi_tool_editor_remove_duplicate_frames')
    : (trust || pastSyncVerified
        ? tr('tab3.no_requerida_el_rpu_target_alinea')
        : tr('tab3.solo_si_fase_d_detecta_desfase'));
  steps.push({
    key: 'E', icon: 'ajustes', title: tr('tab3.fase_e_correccion_de_sync'),
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
    fWhat = tr('tab3.drop_in_inyecta_el_rpu_del');
  } else if (wf === 'p7_fel') {
    fWhat = tr('tab3.merge_cmv4_0_sobre_rpu_p7');
  } else if (wf === 'p7_mel') {
    fWhat = targetNeedsMerge
      ? tr('tab3.merge_cmv4_0_sobre_rpu_p7_2')
      : tr('tab3.inyecta_el_rpu_target_directamente_en');
  } else {  // p8
    fWhat = targetNeedsMerge
      ? tr('tab3.merge_cmv4_0_sobre_rpu_p8')
      : tr('tab3.inyecta_el_rpu_target_directamente_en_2');
  }
  steps.push({
    key: 'F', icon: 'inyectar', title: tr('tab3.fase_f_inyectar_rpu'),
    what: fWhat, etaSecs: etaF,
  });
  // Fase G: tres rutas distintas según workflow/modo.
  // - drop-in FEL: source_injected.hevc ya es BL+EL dual-layer → mkvmerge directo.
  // - p7_fel non-drop-in: dovi_tool mux combina BL + EL_injected → mkvmerge.
  // - p7_mel / p8: BL_injected.hevc single-layer → mkvmerge directo.
  let gWhat;
  if (dropIn) {
    gWhat = tr('tab3.mkvmerge_directo_sobre_source_injected_hevc');
  } else if (wf === 'p7_fel') {
    gWhat = tr('tab3.dovi_tool_mux_combina_bl_hevc');
  } else {  // p7_mel / p8
    gWhat = tr('tab3.sin_mux_dual_layer_single_layer');
  }
  steps.push({
    key: 'G', icon: 'caja', title: tr('tab3.fase_g_remux_mkv_final'),
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
    key: 'H', icon: 'check', title: tr('tab3.fase_h_validar_finalizar'),
    what: dropIn
      ? tr('tab3.validacion_rapida_ffprobe_frame_count_mkvmerge')
      : tr('tab3.validacion_rigurosa_extract_rpu_completo_del'),
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
          ? tr('tab3.eta_restantes',
                 {reloj: _cmv40FmtClock(remaining), sufijo})
          : tr('tab3.casi_listo');
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
  return rutaPorSaber ? tr('tab3.estimado_inicial') : '(auto)';
}

function _cmv40TextoRestante(secs, s) {
  if (secs <= 0) return tr('tab3.casi_listo');
  return tr('tab3.eta_restantes',
            {reloj: _cmv40FmtClock(secs), sufijo: _cmv40SufijoEta(s)});
}

/** ¿El job está en un estado terminal? Con done/error el porcentaje no debe
 *  salir del último job_pct recibido, que se quedó a medias. */
/** ¿Este pipeline ya no avanza? Decide si el cronómetro corre o se para.
 *
 *  Tres casos, y hasta ahora se cubrían de uno en uno:
 *
 *  - `phase === 'done'` o hay `error_message`: lo de siempre.
 *  - **cancelada**: no cambia `phase` ni escribe `error_message` —cancelar no
 *    es un error, y así está a propósito— así que hay que mirar el último
 *    registro del `phase_history`.
 *  - **abierta desde el historial**: la fase terminó bien pero el proyecto no
 *    está en `done`, así que las dos condiciones de arriba son falsas y el
 *    reloj seguía contando desde el arranque del pipeline. Nueve horas, en el
 *    caso que lo destapó. Ahí no hay nada corriendo y quien lo sabe es el
 *    modal, que lo dice con `project.terminal`.
 *
 *  Vive aquí porque la condición estaba escrita DOS veces —el render completo
 *  y el incremental— y cada arreglo tenía que acordarse de las dos.
 */
function _cmv40Terminado(s, project) {
  const ultima = (s.phase_history || []).slice(-1)[0];
  const cancelado = !s.running_phase && !!ultima && ultima.status === 'cancelled';
  return {
    cancelado,
    terminal: s.phase === 'done' || !!s.error_message || cancelado
              || !!(project && project.terminal),
  };
}

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

/** El restante del JOB, del mismo sitio que la columna de trabajo.
 *
 *  Había tres cifras distintas para la misma pregunta: la de la fase, la que
 *  sumaba aquí las fases pendientes y la del backend. Manda **el backend**,
 *  que extrapola el `job_pct` calibrado con lo que ya ha costado, y así la
 *  barra y el restante no pueden contradecirse — salen del mismo número.
 *
 *  La suma local se queda de respaldo para cuando la columna no sabe de este
 *  proyecto: no es el trabajo activo (está en cola, o parado esperando), o el
 *  poll aún no ha traído nada.
 */
function _cmv40RestanteDelJob(s, steps, stepStatuses, hist, project) {
  const t = (typeof trabajoSobre === 'function') ? trabajoSobre(s.id) : null;
  if (t && t.estado === 'corriendo' && t.trabajo && t.trabajo.eta_s != null) {
    return t.trabajo.eta_s;
  }
  return _cmv40ComputeRemainingSecs(s, steps, stepStatuses, hist, project);
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

  const { terminal: isTerminal, cancelado } = _cmv40Terminado(s, project);
  let elapsedLabel  = '—';
  let remainingText = '';
  let timerAttrs    = '';
  if (startedMs) {
    let elapsedSecs;
    if (isTerminal) {
      const lastWithEnd = [...hist].reverse().find(h => h.finished_at);
      const endMs = lastWithEnd ? Date.parse(lastWithEnd.finished_at) : Date.now();
      elapsedSecs = (endMs - startedMs) / 1000;
      remainingText = s.phase === 'done' ? 'finalizado'
                    : s.error_message ? tr('tab3.con_error')
                    : cancelado ? 'cancelado' : '';
    } else {
      elapsedSecs = (Date.now() - startedMs) / 1000;
      // Tiempo restante de fases AUTO pendientes. Excluye fases manuales
      // (etaSecs null = interactiva, p.ej. Fase D no-trusted). Descontamos
      // el tiempo que lleva ejecutándose la fase actual para que el contador
      // baje suavemente durante ella.
      const remaining = _cmv40RestanteDelJob(s, steps, stepStatuses, hist, project);
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
      done:    '<span class="cmv40-tl-status-icon done"><span data-icono="check"></span></span>',
      running: '<span class="cmv40-tl-status-icon running"></span>',
      skipped: '<span class="cmv40-tl-status-icon skipped"><span data-icono="omitida"></span></span>',
      pending: '<span class="cmv40-tl-status-icon pending"></span>',
      error:   '<span class="cmv40-tl-status-icon error"><span data-icono="cruz"></span></span>',
    };
    // Tiempo real de ejecución (solo disponible si la fase se ejecutó en backend)
    const elapsed = status === 'done' ? _cmv40StepElapsedSecs(st.key, s) : null;
    // Label por defecto según status, o customLabel si el step lo especifica.
    // Para done, añadimos el tiempo real ej. "completado · 05:29" si lo hay.
    const doneLabel = elapsed != null
      ? tr('comun.completado_p1', {p1: _cmv40FmtClock(elapsed)})
      : tr('tab3.lbl_completado');
    const defaultLabel = status === 'done'    ? doneLabel
                       : status === 'skipped' ? tr('tab3.lbl_omitida')
                       : status === 'running' ? tr('workbar.en_curso_2')
                       : status === 'error'   ? tr('tab3.lbl_incompatible')
                       : tr('comun.restante_p1', {p1: _cmv40FmtEta(st.etaSecs)});
    const label = st.customLabel || defaultLabel;
    const etaHtml = `<span class="cmv40-tl-eta ${status}">${escHtml(label)}</span>`;
    const gateCls = st.isGate ? ' cmv40-tl-is-gate' : '';
    return `<li class="cmv40-tl-step cmv40-tl-${status}${gateCls}" data-step-key="${escHtml(st.key)}">
      <div class="cmv40-tl-rail">${iconMap[status] || iconMap.pending}</div>
      <div class="cmv40-tl-body">
        <div class="cmv40-tl-title">
          <span class="cmv40-tl-phase-icon">${icono(st.icon)}</span>
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
    trustBadge = '<span class="cmv40-tl-trust-badge pending"><span data-icono="reloj"></span> ' + tr('tab3.auto_pendiente_validaciones') + '</span>';
  } else if (_cmv40Trust(s)) {
    trustBadge = '<span class="cmv40-tl-trust-badge trusted"><span data-icono="rayo"></span> ' + tr('tab3.auto_trusted') + '</span>';
  } else {
    trustBadge = '<span class="cmv40-tl-trust-badge manual"><span data-icono="lupaOnda"></span> ' + tr('tab3.manual_revision_visual') + '</span>';
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
              <span class="cmv40-tl-timer-icon" data-icono="reloj"></span>
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
  'created':         tr('tab1.proyecto_creado'),
  'source_analyzed': tr('tab3.origen_analizado'),
  'target_provided': tr('tab3.rpu_target_listo'),
  'extracted':       tr('tab3.bl_el_extraidos'),
  'sync_verified':   tr('tab3.sync_verificado'),
  'sync_corrected':  tr('tab3.sync_corregido'),
  'injected':        tr('tab3.rpu_inyectado'),
  'remuxed':         tr('tab3.mkv_remuxado'),
  'validated':       tr('tab3.validado'),
  'done':            tr('tab3.completado'),
  'error':           'Error',
  'cancelled':       tr('tab3.cancelado'),
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
    title: tr('tab3.nuevo_proyecto_cmv4_0_paso_1'),
    subtitle: tr('tab3.selecciona_el_mkv_origen_cmv2_9'),
    roots: ROOTS_MKV,
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
      labelEl.textContent = tr('ui.selecciona_mkv');
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
      tr('tab3.se_descargara_desde_la_carpeta_publica');
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
    _cmv40NewResetRepoList(tr('ui.selecciona_primero_el_mkv_origen'));
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
    title: tr('tab3.cambiar_mkv_origen'),
    subtitle: tr('tab3.selecciona_otro_mkv_para_reemplazar_el'),
    roots: ROOTS_MKV,
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
    labelEl.textContent = tr('ui.selecciona_mkv');
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
  else _cmv40NewResetRepoList(tr('ui.selecciona_primero_el_mkv_origen'));
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
    <span data-i18n="tab3.consultando_hoja_de_dovitools"></span>
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
  dv_source:     { icon: 'claqueta', label: tr('tab3.chip_fuente'),   help: tr('tab3.plataforma_de_origen_del_rpu_cmv4') },
  sync:          { icon: 'reloj', label: 'Sync',     help: tr('tab3.offset_de_frames_entre_web_dl') },
  comparisons:   { icon: 'lupaOnda', label: tr('tab3.chip_verif'),   help: tr('tab3.primera_sub_columna_de_comparisons_tipo') },
  comparisons_2: { icon: 'grafico', label: tr('tab3.chip_verif_2'), help: tr('tab3.segunda_sub_columna_de_comparisons_suele') },
  notes:         { icon: 'portapapeles', label: tr('tab3.chip_notas'),    help: tr('tab3.notas_workflow_factible_suele_ser_workflow') },
};

// Fila de tabla key-value — icono + label (columna fija) + valor (flex) + link opcional.
// Uniforme para todos los campos de factibilidad: Fuente, Sync, Verif, Notas.
function _cmv40TableRow(key, value, link, opts = {}) {
  if (!value && !link) return '';
  const m = CMV40_CHIP_META[key] || { icon: '·', label: key, help: '' };
  const valueClass = opts.mono ? 'cmv40-rec-row-value mono' : 'cmv40-rec-row-value';
  const linkHtml = link
    ? `<a class="cmv40-rec-row-link" href="${escHtml(link)}" target="_blank" rel="noreferrer noopener"
         data-tooltip="${escHtml(tr('comun.abrir_p1', {p1: link}))}"><span data-i18n="tab1.abrir"></span> <span data-icono="enlaceExterno"></span></a>`
    : '';
  return `
    <div class="cmv40-rec-row">
      <div class="cmv40-rec-row-label" data-tooltip="${escHtml(m.help)}">
        <span class="cmv40-rec-row-icon">${icono(m.icon)}</span>
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
  feasible:    { icon: 'check', text: tr('tab3.ruta_verificada_restore_del_bloque_cmv4') },
  probably_ok: { icon: 'aviso', text: tr('tab3.seccion_not_sure_viable_pero_sin') },
  infeasible:  { icon: 'info', text: tr('tab3.ruta_de_conversion_a_p8_1') },
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
    ? `<span class="cmv40-rec-na-chip" data-i18n="tab3.no_aplica_a_este_flujo" data-i18n-tip="tab3.esta_app_preserva_el_fel_del"></span>`
    : '';
  const labels = (row.blocker_labels || [])
    .filter(l => !notApplicable || (row.blocker_labels || []).length > 1)
    .map(l => `<div class="cmv40-rec-blocker">· ${escHtml(l)}</div>`).join('');
  return `
    <div class="cmv40-rec-section${notApplicable ? ' na' : ''}">
      <div class="cmv40-rec-section-head">
        <span>${icono(meta.icon)}</span><span>${escHtml(meta.text)}</span>${chip}
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
  recommended:  { cls: 'ok',       icon: 'check', label: tr('tab3.factible') },
  caveats:      { cls: 'caveats',  icon: 'aviso', label: tr('tab3.viable_con_avisos') },
  p8_only_note: { cls: 'p8only',   icon: 'info', label: tr('tab3.no_convertible_a_p8_1') },
  not_feasible: { cls: 'ko',       icon: 'cruz', label: tr('tab3.no_recomendado') },
  unknown:      { cls: 'unknown',  icon: 'info', label: tr('tab3.sin_datos') },
};

function _cmv40RenderRecommendation(data, containerId) {
  const banner = document.getElementById(containerId || 'cmv40-recommendation-banner');
  if (!banner) return;
  banner.style.display = 'block';
  const status = data.status || 'unknown';
  const style = CMV40_VERDICT_STYLE[status] || CMV40_VERDICT_STYLE.unknown;
  const cls = style.cls;
  const icon = icono(style.icon);
  // El backend manda la etiqueta ya redactada (verdict_label); el mapa local
  // es el fallback para respuestas viejas cacheadas.
  const statusLabel = data.verdict_label || style.label;
  banner.className = 'cmv40-rec-banner ' + cls;

  const matchTitleHtml = data.match_title
    ? (data.title_link
        ? `<a class="cmv40-rec-match-title linked" href="${escHtml(data.title_link)}" target="_blank" rel="noreferrer noopener" data-tooltip="${escHtml(tr('comun.abrir_p1', {p1: data.title_link}))}">${escHtml(data.match_title)} <span class="chip-arrow" data-icono="enlaceExterno"></span></a>`
        : `<span class="cmv40-rec-match-title">${escHtml(data.match_title)}</span>`)
    : '';

  // Meta compacta (match% · vía TMDb) empotrada en el header para no añadir otra fila
  let metaHtml = '';
  if (data.match_confidence && data.match_confidence > 0) {
    const pct = Math.round(data.match_confidence * 100);
    const viaLabel = data.match_source === 'tmdb' ? 'TMDb' : data.match_source;
    metaHtml = `<div class="cmv40-rec-meta">
      <span class="cmv40-rec-meta-tag" data-i18n-tip="tab3.similitud_entre_el_titulo_del_fichero">${tr('tab3.pct_match', {pct: pct})}</span>
      <span class="cmv40-rec-meta-tag" data-i18n-tip="tab3.fuente_del_matching_tmdb_traduce_es">${tr('tab3.via_fuente', {fuente: escHtml(viaLabel)})}</span>
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
      CMV40_CHIP_META.notes_motivo = { ...CMV40_CHIP_META.notes, label: tr('tab3.motivo') };
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
      <span data-i18n="tab3.el_titulo"></span> <strong>${escHtml(data.input_title || '')}</strong>${data.input_year ? ' (' + data.input_year + ')' : ''}`;
    if (data.title_en && data.title_en !== data.input_title) {
      html += ` (TMDb: <em>${escHtml(data.title_en)}</em>)`;
    }
    html += tr('tab3.no_aparece_en_la_hoja_de', {p1: data.sheet_rows_loaded || 0});
    html += `</div>`;
    if (!data.tmdb_configured) {
      html += `<div class="cmv40-rec-footer"><span data-icono="aviso"></span> <span data-i18n="tab3.tmdb_no_esta_disponible_sin_clave"></span></div>`;
    }
  }

  // Warning: cuando no tenemos hyperlinks (fuentes xlsx/api/html → ok; csv/disk → sin links)
  const linksOk = ['xlsx', 'api', 'html'].includes(data.sheet_source);
  if (!linksOk && data.sheet_source && data.sheet_source !== 'none') {
    const reason = data.sheets_api_error ||
      tr('tab3.no_se_pudo_leer_el_sheet');
    html += `<div class="cmv40-rec-warn">
      <span data-icono="aviso"></span> <span data-i18n="tab3.los_enlaces_incrustados_en_el_sheet"></span> <code>${escHtml(data.sheet_source)}</code>).<br>
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
  select.innerHTML = '<option value="">' + tr('ui.cargando_2') + '</option>';
  const data = await apiFetch('/api/cmv40/rpu-files');
  select.innerHTML = '<option value="">' + tr('tab3.opt_seleccionar_rpu') + '</option>';
  if (data?.files?.length) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = `${f.name} (${_fmtBytes(f.size_bytes)})`;
      select.appendChild(opt);
    });
  } else {
    select.innerHTML = '<option value="">' + tr('tab3.no_hay_rpus_en_mnt_cmv40_rpus') + '</option>';
  }
}

async function _cmv40NewLoadTargetMkvs() {
  const select = document.getElementById('cmv40-new-target-mkv-select');
  select.innerHTML = '<option value="">' + tr('ui.cargando_2') + '</option>';
  const data = await apiFetch('/api/mkv/files-in-isos');
  select.innerHTML = '<option value="">' + tr('tab3.seleccionar_mkv_con_cmv40') + '</option>';
  if (data?.files?.length) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = f.name;
      select.appendChild(opt);
    });
  } else {
    select.innerHTML = '<option value="">' + tr('tab3.no_hay_mkvs_en_el_directorio_de_isos') + '</option>';
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
      return { tiempo: tr('tab3.variable_depende_de_revision_manual'), totalSecs: null };
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
    icon: 'diana',
    title: tr('tab3.bin_p7_fel_cmv4_0_ya'),
    blurb: tr('tab3.bin_con_bl_el_rpu_cmv4') + ' ' +
           tr('tab3.comportamiento_segun_tu_bd') + ' ' +
           tr('tab3.p7_fel_drop_in_directo_sin') + ' ' +
           tr('tab3.p7_mel_merge_de_los_levels') + ' ' +
           tr('tab3.p8_1_mel_convertido_merge_de'),
    cls: 'ok',
    autoEndsAt: null,
    phases: [
      { k: 'A', label: tr('tab3.paso_analizar_bd'),    state: 'run' },
      { k: 'B', label: tr('tab3.paso_descargar_bin'),  state: 'run' },
      { k: 'C', label: 'Demux',          state: 'skip', mod: tr('tab3.si_bd_es_fel') },
      { k: 'D', label: tr('tab3.paso_verif_visual'),  state: 'skip', mod: tr('tab3.mod_gates_trusted') },
      { k: 'E', label: tr('tab3.correccion_sync'), state: 'skip', mod: tr('tab3.0_por_gates') },
      { k: 'F', label: tr('tab3.paso_inyectar'),       state: 'run',  mod: tr('tab3.drop_in_o_merge') },
      { k: 'G', label: 'Remux MKV',      state: 'run' },
      { k: 'H', label: tr('tab3.paso_validar'),        state: 'run' },
    ],
  },
  trusted_p7_mel_final: {
    icon: 'diana',
    title: tr('tab3.bin_p7_mel_cmv4_0_ya'),
    blurb: tr('tab3.bin_con_bl_el_mel_rpu') + ' ' +
           tr('tab3.siempre_se_descarta_comportamiento_segun_tu') + ' ' +
           tr('tab3.p7_mel_inyeccion_directa_del_rpu') + ' ' +
           tr('tab3.p7_fel_merge_de_los_levels') + ' ' +
           tr('tab3.p8_1_mel_convertido_merge_en'),
    cls: 'ok',
    autoEndsAt: null,
    phases: [
      { k: 'A', label: tr('tab3.paso_analizar_bd'),    state: 'run' },
      { k: 'B', label: tr('tab3.paso_descargar_bin'),  state: 'run' },
      { k: 'C', label: 'Demux',          state: 'run', mod: tr('tab3.segun_bd') },
      { k: 'D', label: tr('tab3.paso_verif_visual'),  state: 'skip', mod: tr('tab3.mod_gates_trusted') },
      { k: 'E', label: tr('tab3.correccion_sync'), state: 'skip', mod: tr('tab3.0_por_gates') },
      { k: 'F', label: tr('tab3.paso_inyectar'),       state: 'run',  mod: tr('tab3.directo_o_merge') },
      { k: 'G', label: 'Remux MKV',      state: 'run' },
      { k: 'H', label: tr('tab3.paso_validar'),        state: 'run' },
    ],
  },
  trusted_p8_source: {
    icon: 'caja',
    title: tr('tab3.bin_p8_retail_cmv40_completo'),
    blurb: tr('tab3.bin_p8_con_cmv4_0_completo') + ' ' +
           tr('tab3.de_metadata_cmv4_0_via_dovi') + ' ' +
           tr('tab3.comportamiento_segun_tu_bd') + ' ' +
           tr('tab3.p7_fel_merge_de_los_levels_2') + ' ' +
           tr('tab3.p7_mel_descarta_el_e_inyecta') + ' ' +
           tr('tab3.p8_1_mel_convertido_inyeccion_directa'),
    cls: 'info',
    autoEndsAt: null,
    phases: [
      { k: 'A', label: tr('tab3.paso_analizar_bd'),    state: 'run' },
      { k: 'B', label: tr('tab3.paso_descargar_bin'),  state: 'run' },
      { k: 'C', label: 'Demux',          state: 'run', mod: tr('tab3.segun_bd') },
      { k: 'D', label: tr('tab3.paso_verif_visual'),  state: 'skip', mod: tr('tab3.mod_gates_trusted') },
      { k: 'E', label: tr('tab3.correccion_sync'), state: 'skip', mod: tr('tab3.0_por_gates') },
      { k: 'F', label: tr('tab3.paso_merge_inyectar'), state: 'run' },
      { k: 'G', label: 'Remux MKV',      state: 'run' },
      { k: 'H', label: tr('tab3.paso_validar'),        state: 'run' },
    ],
  },
  unknown: {
    icon: 'info',
    title: tr('tab3.tipo_por_clasificar'),
    blurb: tr('tab3.la_clasificacion_real_se_hara_en') + ' ' +
           tr('tab3.frames_l5_cm_v4_0_has') + ' ' +
           tr('tab3.fase_d_para_revision_visual_de'),
    cls: 'warn',
    autoEndsAt: 'D',
    phases: [
      { k: 'A', label: tr('tab3.paso_analizar_bd'),    state: 'run' },
      { k: 'B', label: tr('tab3.paso_clasificar_bin'), state: 'run' },
      { k: 'C', label: 'Demux',          state: 'run', mod: tr('tab3.mod_probable') },
      { k: 'D', label: tr('tab3.paso_verif_visual'),  state: 'run', mod: tr('tab3.si_no_trusted') },
      { k: 'E', label: tr('tab3.correccion_sync'), state: 'run', mod: 'si Δ≠0' },
      { k: 'F', label: tr('tab3.paso_inyectar'),       state: 'run' },
      { k: 'G', label: 'Remux MKV',      state: 'run' },
      { k: 'H', label: tr('tab3.paso_validar'),        state: 'run' },
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
      <div class="cmv40-ph-pill cmv40-ph-${p.state}" data-tooltip="${escHtml(tr('tab3.pill_fase_k_label', {k: p.k, label: p.label}))}${p.mod ? ' · ' + escHtml(p.mod) : ''}">
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
        <span class="cmv40-pp-icon">${icono(info.icon)}</span>
        <span class="cmv40-pp-title">${escHtml(info.title)}</span>
        <span class="cmv40-pp-time" data-i18n-tip="tab3.estimacion_basada_en_tiempos_medidos_en"><span data-icono="reloj"></span> ${escHtml(tiempo)}</span>
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
        <span class="cmv40-pp-prov-icon"><span data-icono="biblioteca"></span></span>
        <span class="cmv40-pp-prov-label" data-i18n="tab3.retail"></span>
        <span class="cmv40-pp-prov-body" data-i18n="tab3.rpu_extraido_de_master_streaming_oficial"></span>
      </div>`;
  }
  if (prov === 'generated') {
    const altHtml = retailAlternative
      ? `<div class="cmv40-pp-prov-alt">
           <strong><span data-i18n="tab3.alternativa_retail_disponible_en_este_repo"></span></strong><br>
           <code>${escHtml(retailAlternative)}</code><br>
           <em data-i18n="tab3.cambiala_en_el_desplegable_de_arriba"></em>
         </div>`
      : '';
    return `
      <div class="cmv40-pp-prov cmv40-pp-prov-gen">
        <span class="cmv40-pp-prov-icon"><span data-icono="aviso"></span></span>
        <span class="cmv40-pp-prov-label" data-i18n="tab3.generated"></span>
        <span class="cmv40-pp-prov-body"><span data-i18n-html="tab3.cmv4_0_sintetico_desde_hdr10"></span></span>
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
    const sourceLabel = tab === 'path' ? tr('tab3.el_bin_local') : tr('tab3.el_mkv_target');
    container.style.display = 'block';
    container.innerHTML = `
      <div class="cmv40-pp-card" style="background:var(--blue-dim); border:1px solid var(--blue-border); border-radius:8px; padding:10px 12px">
        <div style="font-size:12px; color:var(--text-1); line-height:1.5">
          <strong style="color:var(--blue)" data-i18n="tab3.i_el_tipo_del_bin_se"></strong>
          ${tr('tab3.sin_el_sheet_de_recomendacion_de', {sourcelabel: sourceLabel})}
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
    if (alt) retailAlternative = alt.file?.name || tr('tab3.retail_disponible');
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
    span.innerHTML = icono('rayo') + ' Auto-pipeline';
    if (wrap) wrap.setAttribute('data-tooltip',
      tr('ui.encadena_las_fases_disponibles_sin_interaccion'));
    return;
  }
  const runPhases = info.phases.filter(p => p.state === 'run').map(p => p.k);
  const endsAt = info.autoEndsAt;
  if (endsAt) {
    span.innerHTML = icono('rayo') + tr('tab3.auto_pipeline_hasta_fase_pausa_si', {endsat: escHtml(endsAt)});
    if (wrap) wrap.setAttribute('data-tooltip',
      tr('tab3.corre_hasta_la_fase_si_los', {endsat: endsAt}));
  } else {
    span.innerHTML = icono('rayo') + ' ' + tr('tab3.auto_pipeline_completo_p1', {p1: escHtml(runPhases.join('→'))});
    if (wrap) wrap.setAttribute('data-tooltip',
      tr('tab3.ejecuta_fases_automaticamente_estimado', {p1: runPhases.length, tiempo: info.tiempo}));
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
    _cmv40NewResetRepoList(tr('ui.selecciona_primero_el_mkv_origen'));
    if (info) info.textContent = tr('ui.selecciona_primero_un_mkv_origen');
    return;
  }
  list.innerHTML = '<div class="cmv40-repo-empty"><span data-icono="reloj"></span> ' + tr('tab3.buscando_en_drive') + '</div>';
  if (info) info.innerHTML = '<span class="cmv40-rec-spinner-inline"></span> ' + tr('tab3.consultando_repositorio_de_dovitools');
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
    _cmv40NewResetRepoList(tr('tab3.error_consultando_el_repositorio'), true);
    return;
  }
  if (!data.drive_configured) {
    _cmv40NewRepoCands = [];
    list.innerHTML = `<div class="cmv40-repo-banner-wrap">${_cmv40RepoUnavailableBanner(data)}</div>`;
    if (info) {
      info.textContent = !data.drive_folder_configured
        ? tr('tab3.repo_bloqueado_configura_la_url')
        : tr('tab3.google_api_key_no_configurada');
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
    _cmv40NewResetRepoList(tr('tab3.sin_coincidencias_para', {t: escHtml(t)}));
    if (info) {
      info.innerHTML = tr('tab3.no_hay_bin_para_prueba_otra_pestana', {titulo: escHtml(t)});
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
    const tagMeta = pt === 'trusted_p7_fel_final' ? { icon: 'diana', label: 'bin P7 FEL',  cls: 'tag-ok' }
                  : pt === 'trusted_p7_mel_final' ? { icon: 'diana', label: 'bin P7 MEL',  cls: 'tag-ok' }
                  : pt === 'trusted_p8_source'    ? { icon: 'caja', label: 'bin P8 retail', cls: 'tag-info' }
                  : { icon: 'info', label: tr('tab3.tipo_desconocido_min'), cls: 'tag-warn' };
    const provTag = prov === 'retail'
      ? '<span class="cmv40-repo-card-tag tag-ok"><span data-icono="biblioteca"></span> Retail</span>'
      : prov === 'generated'
      ? '<span class="cmv40-repo-card-tag tag-warn"><span data-icono="aviso"></span> Generated</span>'
      : '';
    const isBest = c.file.name === topFilename;
    return `
      <div class="cmv40-repo-card" data-file-id="${escHtml(c.file.id)}"
           role="button" tabindex="0"
           onclick="_cmv40NewSelectRepoCandidate('${escHtml(c.file.id)}')"
           onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();_cmv40NewSelectRepoCandidate('${escHtml(c.file.id)}')}">
        <div class="cmv40-repo-card-head">
          <span class="cmv40-repo-card-tag ${tagMeta.cls}">${icono(tagMeta.icon)} ${tagMeta.label}</span>
          ${provTag}
          ${isBest ? '<span class="cmv40-repo-card-best"><span data-icono="diana"></span> <span data-i18n="comun.mejor_match"></span></span>' : ''}
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
    info.innerHTML = tr('tab3.n_candidatos_top_score_al_crear', {n: `<strong>${cands.length}</strong>`, p2: cands.length !== 1 ? 's' : '', score: `<strong>${Math.round(cands[0].score * 100)}%</strong>`});
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
    showToast(tr('tab3.error_al_crear_el_proyecto'), 'error');
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
    // Y se mira mientras pasa. El pre-flight decide SI va a haber trabajo, y
    // su veredicto llegaba en diferido: se cerraba el asistente y el motivo
    // aparecía después como un banner en el panel, que hay que estar mirando.
    abrirPreflightCMv40(project.id);
  }
  // Sin target no hay nada que validar: el proyecto se crea y ya está.
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
  // Dos reglas distintas, y la diferencia importa:
  //
  //  · **Si viene VACÍO** — para lo que el modelo siempre trae y algún
  //    endpoint devuelve a null por el camino.
  //  · **Si la clave NO ESTÁ** — para lo que no es del modelo y solo añade
  //    `GET /api/cmv40/{id}`. `cola` es de estos: la respuesta de cualquier
  //    endpoint es un `model_dump()` y viene sin él, así que sin preservarlo
  //    el proyecto «olvida» que espera turno en cuanto se hace una acción, y
  //    el auto-avance vuelve a disparar la fase — que el guard rechaza con
  //    «este proyecto ya tiene una fase esperando turno», cada cuatro
  //    segundos.
  //
  //    Con la primera regla no valdría: el GET manda `cola: null` cuando el
  //    proyecto SALE de la cola, y conservarlo ahí lo dejaría encolado para
  //    siempre.
  const PRESERVE_SI_VACIO = ['tmdb_info'];
  const PRESERVE_SI_FALTA = ['cola'];
  for (const f of PRESERVE_SI_VACIO) {
    if (project.session && project.session[f] && !data[f]) {
      preserved[f] = project.session[f];
    }
  }
  for (const f of PRESERVE_SI_FALTA) {
    if (project.session && !(f in data)) {
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
  // **Al terminar la Fase E, el volcado del gráfico ya no vale.**
  //
  // `correct_sync` regenera `per_frame_data.json`, y de ese volcado salen la
  // confianza, el Δ y el `sync_gate` que habilita «Confirmar sync». La copia
  // en memoria se invalidaba solo al pulsar «Aplicar» —cuando la fase aún no
  // ha corrido— así que el gráfico y el gate seguían siendo los de ANTES de
  // la corrección hasta que algo más los tirara: medido por el usuario, más
  // de 20 s desde que el job queda en «necesita decisión» hasta que el botón
  // se enciende. Ahora se invalida en el flanco: la fase estaba corriendo y
  // ya no. Reportado el 2026-09-23.
  const faseAnterior = project.session && project.session.running_phase;
  if (faseAnterior === 'correct_sync' && !preserved.running_phase
      && !data.running_phase) {
    project.syncData = null;
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
    showToast(tr('tab3.maximo_proyectos_abiertos', {max_cmv40_projects: MAX_CMV40_PROJECTS}), 'warning');
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
      showToast(`${data.message}`, 'error');
      // Con todo borrado, auto-avance queda neutralizado (comprueba error_message)
    } else {
      showToast(`${data.message}`, 'warning');
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
    <span class="subtab-proj-icon"><span data-icono="curva"></span></span>
    <span class="subtab-proj-name" data-tooltip="${escHtml(project.session.source_mkv_name)}">${escHtml(name.slice(0, 24))}${name.length > 24 ? '…' : ''}</span>
    <button class="subtab-proj-close" onclick="closeCMv40Project('${project.id}');event.stopPropagation()" data-i18n-tip="core.cerrar_proyecto">×</button>`;
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
    showToast(tr('tab3.no_hay_log_que_copiar'), 'info');
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
    showToast(tr('tab3.log_copiado_p1_caracteres', {p1: text.length.toLocaleString(localeActual())}), 'success');
    // Feedback visual breve en el botón si se pasó
    if (btn) {
      const orig = btn.textContent;
      btn.innerHTML = icono('check') + ' ' + tr('tab3.copiado');
      btn.disabled = true;
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1200);
    }
  } else {
    showToast(tr('tab1.no_se_pudo_copiar_al_portapapeles'), 'error');
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
  // `🎯 Resultado` es un MARCADOR: el servidor lo concatena en el código,
  // fuera de la cadena traducible, así que llega igual en los tres idiomas.
  // El `'🎯 Result:'` que había al lado no lo emitía nadie — daba a entender
  // que el marcador se traduce, que es justo lo contrario de la regla.
  if (line.includes('🎯 Resultado:')) {
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
  // Watermark++: el WS acaba de entregar una línea que también está
  // (o estará en milisegundos) en session.output_log. Sin este incremento,
  // un refresh posterior intentaría pintarla de nuevo.
  project._renderedLogCount = (project._renderedLogCount || 0) + 1;
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
          <div><div class="section-title"><span data-icono="portapapeles"></span> <span data-i18n="tab1.log"></span></div></div>
          <div style="display:flex; gap:6px">
            <button class="btn btn-ghost btn-xs"
              onclick="copyLogToClipboard('cmv40-log-${pid}', this)" data-i18n-tip="ui.copiar_todo_el_log_al_portapapeles"><span data-icono="portapapeles"></span> <span data-i18n="ui.copiar"></span></button>
            <button class="btn btn-ghost btn-xs" onclick="_clearCMv40Log('${pid}')"><span data-icono="papelera"></span> <span data-i18n="ui.limpiar"></span></button>
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
  // El overlay de ejecución se retiró: se abría SOLO y tapaba el panel entero
  // —de ahí el bug de agosto en que el banner de ACK se veía y no se podía
  // pulsar— y lo que enseñaba de más (la cartela y la timeline) ya está en el
  // panel, que ahora se ve. El progreso vive en la columna de trabajo y el
  // log en el modal común, que se abre a petición.
  // Hidrata el log permanente (card "📜 Log" del panel del proyecto) con
  // las líneas que aún no estén pintadas. CRÍTICO para el caso "Mac dormido
  // toda la noche": el job sigue en backend, output_log crece a miles de
  // líneas, y al volver el frontend tiene que ver el log completo aunque
  // running_phase=null y no haya WS activo. Sin esta llamada el container
  // solo recibía líneas via WS streaming y se quedaba vacío post-mortem.
  _cmv40SyncPermanentLog(project);
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
  const { terminal: isTerminal, cancelado } = _cmv40Terminado(s, project);
  let elapsedLabel  = '—';
  let remainingText = '';
  let newBaseRemaining = null;   // null = no actualizar data-base-remaining
  if (startedMs) {
    let elapsedSecs;
    if (isTerminal) {
      const lastWithEnd = [...hist].reverse().find(h => h.finished_at);
      const endMs = lastWithEnd ? Date.parse(lastWithEnd.finished_at) : Date.now();
      elapsedSecs = (endMs - startedMs) / 1000;
      remainingText = s.phase === 'done' ? 'finalizado'
                    : s.error_message ? tr('tab3.con_error')
                    : cancelado ? 'cancelado' : '';
    } else {
      elapsedSecs = (Date.now() - startedMs) / 1000;
      newBaseRemaining = _cmv40RestanteDelJob(s, steps, stepStatuses, hist, project);
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
  if (isTerminal) {
    // Terminado, fuera el ancla: el tick de 1 s la busca por ese atributo, y
    // reponerla aquí resucitaba el cronómetro que el render acababa de parar.
    delete elapsedEl?.dataset.startedAt;
  } else if (elapsedEl && startedMs
             && elapsedEl.dataset.startedAt !== String(startedMs)) {
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
      cls2 = 'pending'; txt2 = icono('reloj') + ' ' + tr('tab3.auto_pendiente_validaciones');
    } else if (_cmv40Trust(s)) {
      cls2 = 'trusted'; txt2 = icono('rayo') + ' ' + tr('tab3.auto_trusted');
    } else {
      cls2 = 'manual'; txt2 = icono('lupaOnda') + ' ' + tr('tab3.manual_revision_visual');
    }
    // `innerHTML`, no `textContent`: txt2 LLEVA el SVG del icono dentro y
    // textContent lo escribiría como código — el badge se pintaba bien de
    // entrada y un segundo después, en la primera vuelta del refresco, se
    // convertía en «<svg viewBox="0 0 24 24" fill="none" stroke=…».
    // La comparación va por el ESTADO, no por el contenido: releer innerHTML
    // devuelve el HTML normalizado por el navegador, que no coincide nunca
    // con lo que se escribió, así que repintaría en cada vuelta.
    if (trustBadgeEl.dataset.estado !== cls2) {
      trustBadgeEl.dataset.estado = cls2;
      trustBadgeEl.innerHTML = txt2;
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
      done:    '<span class="cmv40-tl-status-icon done"><span data-icono="check"></span></span>',
      running: '<span class="cmv40-tl-status-icon running"></span>',
      skipped: '<span class="cmv40-tl-status-icon skipped"><span data-icono="omitida"></span></span>',
      pending: '<span class="cmv40-tl-status-icon pending"></span>',
    };
    const elapsed = status === 'done' ? _cmv40StepElapsedSecs(st.key, s) : null;
    const doneLabel = elapsed != null
      ? tr('comun.completado_p1', {p1: _cmv40FmtClock(elapsed)})
      : tr('tab3.lbl_completado');
    const defaultLabel = status === 'done'    ? doneLabel
                       : status === 'skipped' ? tr('tab3.lbl_omitida')
                       : status === 'running' ? tr('workbar.en_curso_2')
                       : tr('comun.restante_p1', {p1: _cmv40FmtEta(st.etaSecs)});
    const label = st.customLabel || defaultLabel;
    const etaHtml = `<span class="cmv40-tl-eta ${status}">${escHtml(label)}</span>`;
    return `<li class="cmv40-tl-step cmv40-tl-${status}" data-step-key="${escHtml(st.key)}">
      <div class="cmv40-tl-rail">${iconMap[status]}</div>
      <div class="cmv40-tl-body">
        <div class="cmv40-tl-title">
          <span class="cmv40-tl-phase-icon">${icono(st.icon)}</span>
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
      eta.textContent = tr('comun.restante_p1', {p1: `${m}:${String(s).padStart(2, '0')}`});
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




async function cmv40CancelRunning(pid) {
  const project = openCMv40Projects.find(p => p.id === pid);
  const phaseLabel = project && project.session && project.session.running_phase
    ? (CMV40_RUNNING_LABELS[project.session.running_phase] || project.session.running_phase)
    : tr('tab3.la_fase_actual');
  const isAuto = project && project.autoContinue;
  // Mensaje contextual: explica que cancela el subprocess en curso y, si
  // el auto-pipeline esta activo, que tambien se desactiva el auto-avance
  // (no lanza la siguiente fase).
  const message = isAuto
    ? tr('tab3.se_matara_el_subprocess_de_se', {phaselabel: phaseLabel})
    : tr('tab3.se_matara_el_subprocess_de_y', {phaselabel: phaseLabel});
  showConfirm(
    tr('tab3.cancelar_la_ejecucion_en_curso'),
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
          showToast(tr('tab3.cancelado_auto_avance_desactivado'), 'info');
        } else {
          showToast(tr('tab3.cancelando'), 'info');
        }
      }
    },
    tr('tab3.cancelar_fase'),
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
/** La ficha de la película. La comparten los tres tabs.
 *
 *  `ctx = {tipo, id, nombre}` la ata a un proyecto concreto y con eso se
 *  puede ofrecer elegir la película: **sin ficha el hueco quedaba vacío**, o
 *  sea que un proyecto sin carátula no daba ninguna pista de qué hacer. Sin
 *  `ctx` —el modal de creación, que aún no tiene proyecto— se comporta como
 *  antes.
 */
function renderTmdbCardHTML(t, ctx = null) {
  if (!t) {
    return (ctx && typeof botonDeFicha === 'function')
      ? botonDeFicha(ctx) : '';
  }
  const metaParts = [];
  // Un episodio se identifica por su sitio en la serie, y va PRIMERO: es lo
  // que distingue este fichero de los otros nueve de la misma temporada.
  if (t.es_serie) {
    const ep = `T${t.temporada} · E${t.episodio}`;
    metaParts.push(t.episodio_titulo ? `${ep} · ${t.episodio_titulo}` : ep);
  }
  if (t.year) metaParts.push(String(t.year));
  if (t.runtime_minutes)
    metaParts.push(`${Math.floor(t.runtime_minutes/60)}h ${t.runtime_minutes%60}min`);
  if (t.genres && t.genres.length) metaParts.push(t.genres.join(' · '));

  const ratingHtml = (t.vote_count > 0)
    ? `<span class="cmv40-tmdb-rating" data-tooltip="${tr('tab3.n_votos_en_tmdb', {n: t.vote_count.toLocaleString(localeActual())})}">${t.vote_average.toFixed(1)}</span>`
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
  if (t.imdb_id)   links.push(`<a href="https://www.imdb.com/title/${escHtml(t.imdb_id)}/" target="_blank" rel="noreferrer noopener" data-i18n="tab3.imdb"></a>`);
  if (t.homepage)  links.push(`<a href="${escHtml(t.homepage)}" target="_blank" rel="noreferrer noopener" data-i18n="tab3.web_oficial"></a>`);
  const linksHtml = links.length ? `<div class="cmv40-tmdb-links">${links.join(' · ')}</div>` : '';

  const posterHtml = t.poster_url
    ? `<img class="cmv40-tmdb-poster" src="${escHtml(t.poster_url)}" alt="${escHtml(t.title)}" loading="lazy">`
    : `<div class="cmv40-tmdb-poster cmv40-tmdb-poster-placeholder"><span data-icono="claqueta"></span></div>`;
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
  const tmdbCardHtml = renderTmdbCardHTML(s.tmdb_info,
    { tipo: 'cmv40', id: s.id, nombre: s.source_mkv_name || '' });
  // Un repintado no puede borrar lo que estás escribiendo, y aquí vive el
  // nombre del MKV de salida: se guarda al perder el foco, así que un
  // repintado a mitad de teclear se llevaba lo tecleado. Misma medida que
  // en el formulario del sync, y la restauración va al FINAL de la función
  // —después del último `innerHTML`— porque este render pinta varias zonas.
  const escritoInfo = anclajeDeFormulario(container);
  container.innerHTML = `
    ${tmdbCardHtml}
    <div class="section-card">
      <div class="section-header" style="display:flex; align-items:flex-start; justify-content:space-between; gap:12px">
        <div><div class="section-title"><span data-icono="curva"></span> <span data-i18n="tab3.proyecto_cmv4_0"></span></div>
        <div class="section-subtitle"><span data-icono="caja"></span> <span data-i18n="tab3.los_cambios_se_guardan_automaticamente_tras"></span></div></div>
        ${canAuto ? `
        <button class="btn btn-${autoOn ? 'primary' : 'ghost'} btn-sm" onclick="cmv40ToggleAuto('${pid}')"
          data-tooltip="${(() => {
            const trust = _cmv40Trust(s);
            if (trust) return tr('tab3.auto_ejecuta_el_pipeline_completo_a');
            if (s.target_type) return tr('tab3.auto_ejecuta_cada_fase_tras_la');
            return tr('tab3.auto_ejecuta_cada_fase_tras_la_2');
          })()}">
          ${icono('rayo')} ${tr(autoOn ? 'tab3.auto_on' : 'tab3.auto_off')}
        </button>` : ''}
      </div>
      <div class="section-body">
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px">
          <div>
            <div style="font-size:11px; color:var(--text-3); margin-bottom:2px" data-i18n="tab3.mkv_origen"></div>
            <div style="font-weight:600">${escHtml(s.source_mkv_name)}</div>
            <div style="font-size:11px; color:var(--text-3); margin-top:4px">
              ${srcDv ? `Profile ${srcDv.profile}${srcDv.el_type ? ` (${srcDv.el_type})` : ''} · CM ${srcDv.cm_version} · ${s.source_frame_count.toLocaleString(localeActual())} frames` : tr('tab3.sin_analizar')}
            </div>
            ${s.source_workflow ? `<div style="font-size:10px; margin-top:4px">
              <span class="cmv40-workflow-badge cmv40-workflow-${s.source_workflow}">${_cmv40WorkflowLabel(s.source_workflow)}</span>
            </div>` : ''}
          </div>
          <div>
            <div style="font-size:11px; color:var(--text-3); margin-bottom:2px">${tr('tab3.mkv_salida_p1', {p1: canEditName ? `<span style="color:var(--text-3)">${tr('tab3.editable')}</span>` : ''})}</div>
            ${canEditName
              ? `<input type="text" id="cmv40-output-name-${pid}" class="cmv40-output-name-input"
                    value="${escHtml(s.output_mkv_name)}"
                    oninput="marcarTocado(this)"
                    onblur="_cmv40SaveOutputName('${pid}', this.value, this)"
                    onkeydown="if(event.key==='Enter'){this.blur()}">`
              : `<div style="font-weight:600">${escHtml(s.output_mkv_name)}</div>`}
            <div style="font-size:11px; color:var(--text-3); margin-top:4px">
              ${tgtDv ? `RPU target: Profile ${tgtDv.profile}${tgtDv.el_type ? ` (${tgtDv.el_type})` : ''} · CM ${tgtDv.cm_version} · ${s.target_frame_count.toLocaleString(localeActual())} frames` : ''}
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
  restaurarAnclajeDeFormulario(container, escritoInfo);
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
        <span class="section-icon"><span data-icono="portapapeles"></span></span>
        <div>
          <div class="section-title" data-i18n="tab3.hoja_de_dovitools"></div>
          <div class="section-subtitle" data-i18n="tab3.lo_que_la_comunidad_ha_documentado"></div>
        </div>
        <button class="btn btn-ghost btn-xs" onclick="_cmv40HydrateSheetClient('${pid}')"
          style="margin-left:auto; color:var(--text-2)" data-i18n-tip="tab3.vuelve_a_consultar_la_hoja_la"><span data-icono="refrescar"></span> <span data-i18n="tab3.actualizar"></span></button>
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
  // El tercer veredicto: sin trims de colorista, pero con L3/L9/L11 reales
  // del análisis. No es «no aporta» y tampoco es autoría, así que va en
  // ámbar y con las dos salidas — es el único caso donde la app no decide
  // por ti, y a propósito: lo que gana el usuario depende de con qué
  // reproduce, y eso la app no lo sabe.
  const esToneMapping = s.target_l8_classification === 'tone_mapping';
  // `isKeep` ya no excluye el tercer veredicto. Lo excluía cuando el pipeline
  // encadenaba sin preguntar: entonces sacar los dos botones habría ofrecido
  // una decisión sobre algo que ya estaba corriendo. Hoy el pre-flight SE
  // DETIENE también con `tone_mapping` —lo pidió el usuario el 2026-09-19— y
  // `recommend_action` devuelve «keep» mientras no haya contestado, así que
  // este es exactamente el caso que tiene que ofrecer las dos salidas. Sin
  // esto, el único veredicto que existe para que decida el usuario era el
  // único sin ningún sitio donde decidirlo.
  const isKeep = action === 'keep';
  // Qué contestó, si contestó — del relato, que resuelve de una vez los tres
  // campos que antes leía cada superficie por su lado (y el respaldo de los
  // proyectos cerrados antes de que el campo existiera).
  const rel = s.relato || null;
  const decision = (rel?.decision?.estado === 'tomada' && rel.decision.elegida)
                 || '';
  const isDropIn = action === 'drop_in';
  const isMerge = action === 'merge';
  const isUnknown = action === 'unknown' || action === '';
  const projectDone = (s.phase === 'done' || s.archived);

  // Badge alineado a la paleta de la app (light mode, variables CSS).
  // Patrón estándar: dim background + border + color del nivel semántico.
  // Ámbar de la PALETA (`--amber-*`, en `:root`). Antes citaba las de la
  // radiografía (`--dv-amber-*`), que están declaradas dentro de `.dv-detail`
  // — o sea fuera del alcance de esta card, así que las tres declaraciones
  // caían y el badge se quedaba sin fondo, sin color y sin borde. Una `var()`
  // fuera de alcance no da error: se lleva la declaración entera, y el guard
  // de variables sin definir no lo ve porque definidas sí están.
  const badgeStyle = esToneMapping
    ? 'background:var(--amber-dim); color:var(--amber-text); border:1px solid var(--amber-border)'
    : isKeep
    ? 'background:var(--blue-dim); color:var(--blue); border:1px solid var(--blue-border)'
    : isDropIn
    ? 'background:var(--green-dim); color:var(--green); border:1px solid var(--green-border)'
    : isMerge
    ? 'background:var(--orange-dim); color:var(--orange); border:1px solid var(--orange-border)'
    : 'background:var(--surface-2); color:var(--text-2); border:1px solid var(--sep)';

  // El icono va SUELTO y no pegado al texto: `label` puede venir del
  // servidor y se pinta con `escHtml`, que convertiría el SVG en el código
  // fuente del SVG, visible en pantalla.
  const esperando = isUnknown && !s.recommended_action_label;
  // Con el tercer veredicto sin contestar el rótulo lo pone el servidor
  // («Requiere decisión — mejora automática, sin ajuste manual»), el mismo que
  // lee la fila «Recomendación» del modal del pre-flight: dos sitios que
  // hablan del mismo bin no pueden llamarlo de dos maneras. Una vez
  // contestado, manda la ruta — que es lo que interesa mientras corre.
  // El rótulo: si hay una decisión pendiente, la pregunta; si hay ruta, la
  // ruta; y si no hay ninguna de las dos, la SITUACIÓN. Antes caía siempre en
  // `recommended_action_label`, que vale «Análisis pendiente» hasta que la
  // Fase A puebla el L2 — y por eso dos proyectos en estados opuestos, uno
  // decidido por el usuario y otro pasado de largo, enseñaban lo mismo.
  // Cuando NO hay ruta todavía, el rótulo es la situación — no
  // `recommended_action_label`, que en ese caso vale «Análisis pendiente» y
  // era el mismo texto para dos proyectos opuestos. Ponerlo como respaldo
  // no bastaba: llega relleno, así que ganaba igual.
  const label = rel?.decision?.estado === 'pendiente'
    ? (rel.decision.titulo || '')
    : isUnknown
    ? (rel?.situacion_rotulo || tr('tab3.esperando_analisis'))
    : (s.recommended_action_label || '—');
  // `porque` del relato manda sobre el motivo de la recomendación: cuenta por
  // qué el trabajo está DONDE está (parado, cancelado, por la vía rápida),
  // que es la pregunta que el usuario tiene delante. El motivo de la ruta se
  // queda detrás como respaldo.
  const reason = rel?.porque || s.recommended_action_reason || '';

  // Tag de calidad del bin (la que va al filename)
  const qualityTag = s.target_l8_quality_label || (
    s.target_l8_classification === 'default' ? tr('tab3.cmv4_sintetico') :
    // El tercer veredicto no tenía entrada y caía al «CMv4 ?» del final: un
    // interrogante justo donde la app sabe exactamente qué es el bin —CMv4.0
    // de verdad, producido por el análisis y no por un colorista.
    s.target_l8_classification === 'tone_mapping' ? tr('tab3.cmv4_solo_analisis') :
    s.target_l8_classification === 'real' ? 'CMv4 (real)' :
    s.target_l8_classification === 'indeterminate' ? tr('tab3.cmv4_ambiguo') :
    'CMv4 ?'
  );

  // Chip comparación L2 (color semántico, paleta de la app)
  const l2Comp = s.l2_comparison || '';
  const l2Chip = l2Comp === 'identical'
    ? `<span style="background:var(--green-dim); color:var(--green); border:1px solid var(--green-border); padding:3px 9px; border-radius:10px; font-size:11px; font-weight:600" data-i18n="tab3.l2_identico_al_mkv_original"></span>`
    : l2Comp === 'different'
    ? `<span style="background:var(--orange-dim); color:var(--orange); border:1px solid var(--orange-border); padding:3px 9px; border-radius:10px; font-size:11px; font-weight:600" data-i18n="tab3.l2_distinto_del_mkv_original"></span>`
    : '';

  // ── Los niveles, con su PAPEL y con lo que ya tiene tu disco ──
  //
  // Antes esto era una lista de números sueltos («combos L8: 2») que no
  // decían si eso era bueno. Hoy cada fila dice de dónde sale el nivel,
  // porque es lo que permite entender el veredicto:
  //
  //   L8  lo pone el COLORISTA  -> es lo único que decide
  //   L3  lo pone el ANÁLISIS   -> lo produce cm_analyze sobre cualquier disco
  //   L2  lo pone el colorista  -> pero YA lo tienes en el Blu-ray
  //   L1  lo pone el análisis   -> ya lo tienes
  //
  // La columna «tu disco» es la clave didáctica: enseña de un vistazo que
  // lo único que el bin aporta de un humano es el L8.
  const delta = s.target_l8_max_delta || 0;
  const nivelRows = [];
  if (s.target_l8_unique_count || delta) {
    const trabajados = s.target_l8_neutral_frames_pct != null
      ? `${((1.0 - s.target_l8_neutral_frames_pct) * 100).toFixed(0)}%` : '—';
    const extras = [];
    if (s.target_l8_has_mid_contrast) extras.push('mid_contrast');
    if (s.target_l8_has_clip_trim) extras.push('clip_trim');
    nivelRows.push({
      nivel: 'L8', papel: tr('tab3.nivel_l8_papel'), decide: true,
      bin: tr('tab3.nivel_l8_valor', {
        combos: _cmv40Num(s.target_l8_unique_count || 0),
        delta: _cmv40Num(delta), pct: trabajados }),
      extra: extras.join(' · '),
      disco: '—',
    });
  }
  if (s.target_l2_unique_count || s.source_l2_unique_count) {
    nivelRows.push({
      nivel: 'L2', papel: tr('tab3.nivel_l2_papel'),
      bin: tr('tab3.n_combos', {n: _cmv40Num(s.target_l2_unique_count || 0)}),
      disco: s.source_l2_unique_count
        ? tr('tab3.n_combos', {n: _cmv40Num(s.source_l2_unique_count)}) : '—',
    });
  }
  if (s.target_l3_unique_count || s.target_l3_frames) {
    nivelRows.push({
      nivel: 'L3', papel: tr('tab3.nivel_l3_papel'),
      bin: tr('tab3.n_combos', {n: _cmv40Num(s.target_l3_unique_count || 0)}),
      disco: '—',
    });
  }
  const techGrid = nivelRows.length ? `
    <div class="cmv40-niveles" data-i18n-tip="tab3.niveles_quien_los_crea">
      <div class="cmv40-niveles-cab"></div>
      <div class="cmv40-niveles-cab" data-i18n="tab3.col_papel"></div>
      <div class="cmv40-niveles-cab" data-i18n="tab3.col_bin"></div>
      <div class="cmv40-niveles-cab" data-i18n="tab3.col_disco"></div>
      ${nivelRows.map(r => `
        <div class="cmv40-nivel-id${r.decide ? ' decide' : ''}">${escHtml(r.nivel)}</div>
        <div class="cmv40-nivel-papel">${escHtml(r.papel)}${
            r.decide ? ` <span class="cmv40-nivel-decide" data-i18n="tab3.decide"></span>` : ''}</div>
        <div class="cmv40-nivel-val">${escHtml(r.bin)}${
            r.extra ? `<br><span class="cmv40-nivel-extra">${escHtml(r.extra)}</span>` : ''}</div>
        <div class="cmv40-nivel-val cmv40-nivel-disco">${escHtml(r.disco)}</div>
      `).join('')}
    </div>` : '';

  // El seguimiento de la decisión: qué falta por contestar, o qué se
  // contestó y cuándo. Sin esto las fases pasaban por delante y no quedaba
  // en ninguna parte que hubiera habido algo que decidir — que es
  // literalmente lo que el usuario reportó el 2026-09-19.
  const pideDecision = isKeep && !projectDone && !decision;
  let decisionLinea = '';
  if (pideDecision) {
    decisionLinea = `
      <div class="cmv40-decision pendiente">
        <span data-icono="reloj"></span> <span data-i18n="tab3.esperando_tu_decision"></span>
      </div>`;
  } else if (decision) {
    const cuando = (typeof _cmv40PfCuando === 'function')
      ? _cmv40PfCuando(rel?.decision?.cuando) : '';
    const txt = decision === 'keep'
      ? tr('tab3.decidiste_mantener') : tr('tab3.decidiste_inyectar');
    decisionLinea = `
      <div class="cmv40-decision tomada">
        <span data-icono="check"></span> ${escHtml(txt + (cuando ? ` · ${cuando}` : ''))}
      </div>`;
  }

  // Botones de acción cuando la recomendación es KEEP y el proyecto no está
  // cerrado todavía. Si el proyecto ya está done/archived —o si el usuario ya
  // contestó— no se muestran: volver a preguntar lo ya decidido es lo que
  // hacía el modal antes de que existiera `preflight_user_choice`.
  let actionButtons = '';
  if (pideDecision) {
    actionButtons = `
      <div style="display:flex; gap:8px; margin-top:14px; flex-wrap:wrap">
        <button class="btn btn-primary btn-sm" onclick="cmv40AcceptKeep('${pid}')" data-i18n-tip="tab3.cierra_el_proyecto_sin_tocar_el">
          <span data-icono="check"></span> <span data-i18n="tab3.mantener_mkv_actual"></span>
        </button>
        <button class="btn btn-ghost btn-sm" onclick="cmv40OverrideRecommendation('${pid}')" data-i18n-tip="tab3.procesa_el_mkv_inyectando_el_rpu">
          <span data-icono="inyectar"></span> <span data-i18n="tab3.inyectar_rpu_igualmente"></span>
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
        <span style="color:var(--green); font-weight:600"><span data-icono="check"></span> <span data-i18n="tab3.proyecto_cerrado_mkv_actual_mantenido"></span></span>
        <span data-i18n="tab3.el_fichero_original_quedo_intacto_tu"></span>
      </div>`;
  } else if (s.output_workflow === 'restore_dropin') {
    doneBanner = `
      <div style="margin-top:12px; padding:10px 12px; background:var(--green-dim); border:1px solid var(--green-border); border-radius:var(--r-sm); color:var(--text-1); font-size:12px; line-height:1.4">
        <span style="color:var(--green); font-weight:600"><span data-icono="check"></span> <span data-i18n="tab3.mkv_procesado_rpu_cmv4_0_inyectado"></span></span>
        ${tr('tab3.el_bin_se_inyecto_directo_sobre', {qualitytag: escHtml(qualityTag)})}
      </div>`;
  } else if (s.output_workflow === 'restore_merge') {
    const mergeLevels = s.source_workflow === 'p7_fel'
      ? '[1, 2, 3, 6, 8, 9, 10, 11, 254]'
      : '[3, 8, 9, 11, 254]';
    const l2Note = s.source_workflow === 'p7_fel'
      ? tr('tab3.l1_l2_l6_del_bin_sobrescriben')
      : tr('tab3.l1_l2_l5_l6_del_mkv');
    doneBanner = `
      <div style="margin-top:12px; padding:10px 12px; background:var(--green-dim); border:1px solid var(--green-border); border-radius:var(--r-sm); color:var(--text-1); font-size:12px; line-height:1.4">
        <span style="color:var(--green); font-weight:600"><span data-icono="check"></span> <span data-i18n="tab3.mkv_procesado_rpu_cmv4_0_inyectado_2"></span></span>
        ${tr('tab3.niveles_cmv4_0_mergelevels_transferidos_del', {mergelevels: mergeLevels, l2note: l2Note, qualitytag: escHtml(qualityTag)})}
      </div>`;
  } else if (projectDone) {
    // Proyecto done sin output_workflow conocido (sesiones legacy procesadas
    // antes del Bloque 4). Banner genérico.
    doneBanner = `
      <div style="margin-top:12px; padding:10px 12px; background:var(--green-dim); border:1px solid var(--green-border); border-radius:var(--r-sm); color:var(--text-1); font-size:12px; line-height:1.4">
        <span style="color:var(--green); font-weight:600"><span data-icono="check"></span> <span data-i18n="tab3.proyecto_completado"></span></span>
      </div>`;
  }

  return `
    <div class="section-card">
      <div class="section-header">
        <div>
          <div class="section-title"><span data-icono="diana"></span> <span data-i18n="tab3.analisis_y_recomendacion"></span></div>
          <div class="section-subtitle" data-i18n="tab3.decision_mantener_vs_inyectar_rapido_preserva"></div>
        </div>
      </div>
      <div class="section-body">
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap">
          <span style="padding:6px 12px; border-radius:var(--r-sm); font-weight:700; font-size:13px; ${badgeStyle}">${esperando ? icono('reloj') + ' ' : ''}${escHtml(label)}</span>
          <span style="background:var(--surface-2); color:var(--text-2); border:1px solid var(--sep); padding:4px 10px; border-radius:10px; font-size:11px; font-weight:600; font-family:ui-monospace,SFMono-Regular,Menlo,monospace">${escHtml(qualityTag)}</span>
          ${l2Chip}
        </div>
        ${reason ? `<div style="margin-top:12px; color:var(--text-2); font-size:12px; line-height:1.5">${escHtml(reason)}</div>` : ''}
        ${decisionLinea}
        ${techGrid}
        ${actionButtons}
        ${doneBanner}
      </div>
    </div>`;
}

function cmv40AcceptKeep(pid) {
  showConfirm(
    tr('tab3.mantener_el_mkv_actual_y_cerrar'),
    tr('tab3.el_proyecto_se_cierra_como_completado') + ' '
      + tr('tab3.tu_reproductor_p3i_t4_sony_lg') + ' '
      + tr('tab3.hara_la_conversion_al_vuelo_en') + ' '
      + tr('tab3.equivalente_al_de_inyectar_el_rpu') + ' '
      + tr('tab3.procesado_ni_50_gb_de_disco'),
    async () => {
      const data = await apiFetch(`/api/cmv40/${pid}/accept-keep`, { method: 'POST' });
      if (!data) {
        showToast(tr('tab3.error_al_cerrar_el_proyecto'), 'error');
        return;
      }
      const project = openCMv40Projects.find(p => p.id === pid);
      if (project) {
        _cmv40AssignSession(project, data);
        _updateCMv40Panel(project);
      }
      refreshCMv40Sidebar();
      showToast(tr('tab3.proyecto_cerrado_mkv_actual_mantenido'), 'success');
    },
    tr('tab3.mantener_mkv_actual'),
  );
}

function cmv40OverrideRecommendation(pid) {
  showConfirm(
    tr('tab3.inyectar_rpu_cmv4_0_aunque_el'),
    tr('tab3.el_pipeline_va_a_procesar_el') + ' '
      + tr('tab3.aunque_el_bin_del_repo_no') + ' '
      + tr('tab3.el_resultado_visible_es_equivalente_a') + ' '
      + tr('tab3.reproductor_pero_el_mkv_queda_archivado') + ' '
      + tr('tab3.para_compatibilidad_con_otros_equipos'),
    async () => {
      const data = await apiFetch(`/api/cmv40/${pid}/override-recommendation`, { method: 'POST' });
      if (!data) {
        showToast(tr('tab3.error_al_continuar_el_procesado'), 'error');
        return;
      }
      const project = openCMv40Projects.find(p => p.id === pid);
      if (project) {
        _cmv40AssignSession(project, data);
        _updateCMv40Panel(project);
      }
      refreshCMv40Sidebar();
      showToast(tr('tab3.inyeccion_forzada_el_pipeline_continuara'), 'info');
    },
    tr('tab3.inyectar_rpu_cmv4_0'),
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
    p7_fel: tr('tab3.p7_fel_merge_cmv4_0_preservando_dual'),
    p7_mel: tr('tab3.p7_mel_descarta_el_p8_1'),
    p8:     tr('tab3.p8_1_inject_directo_p8_1_cmv4'),
  }[wf] || wf;
}

async function _cmv40SaveOutputName(pid, newName, campo) {
  // Guardado: lo que el usuario escribió ya es lo que hay, así que deja de
  // ser «pendiente de conservar» y el próximo repintado puede traer el
  // valor del servidor sin pelearse con él.
  if (campo) delete campo.dataset.tocado;
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
    showToast(tr('tab3.nombre_actualizado'), 'success');
  }
}

function _renderCMv40PhaseStrip(s, pid) {
  const container = document.getElementById(`cmv40-phase-strip-${pid}`);
  if (!container) return;
  // Icono y rótulo de cada paso. La LISTA sale de `CMV40_FASES_DEF`, que es
  // la misma que usan las cards: la tira tenía su propia copia con los siete
  // `produces` escritos otra vez, y es donde se coló el desfase de abajo.
  // Va dentro de la función porque lleva `tr()`: una tabla de rótulos en el
  // ámbito del módulo se resuelve al cargar y congela el idioma.
  const pinta = {
    A: ['lupa', tr('tab3.analizar_origen')],
    B: ['diana', 'RPU target'],
    C: ['tijeras', tr('tab3.extraer_bl_el')],
    D: ['grafico', tr('tab3.paso_verificar_sync')],
    F: ['inyectar', tr('tab3.paso_inyectar')],
    G: ['caja', 'Remux'],
    H: ['check', tr('tab3.paso_validar')],
  };
  const hayError = !!s.error_message;
  container.innerHTML = CMV40_FASES_DEF.map((fase, i) => {
    const [ico, rotulo] = pinta[fase.key] || ['pendiente', fase.key];
    // **El MISMO criterio que las cards** (`_cmv40PhaseState`).
    //
    // `s.phase` es la última fase COMPLETADA, no la que corre. La tira la
    // tomaba por la actual y comparaba contra la clave del paso, así que
    // con la Fase F en marcha —`phase` todavía en `sync_verified`— seguía
    // parpadeando «verificar sync», que ya había terminado, y la F salía
    // como pendiente. Un paso entero de desfase. Reportado el 2026-09-23.
    let state = _cmv40PhaseState(s.phase, fase.produces, fase.startsFrom);
    if (state === 'active' && hayError) state = 'error';
    return `
      <div class="cmv40-phase-step ${state}">
        <div class="cmv40-phase-circle">${icono(ico)}</div>
        <div class="cmv40-phase-label">${rotulo}</div>
      </div>
      ${i < CMV40_FASES_DEF.length - 1 ? '<div class="cmv40-phase-conn"></div>' : ''}
    `;
  }).join('');
}

// Definición de todas las fases: inicio + fin
// Una fase está "done" si la phase actual es >= el estado que esa fase PRODUCE
const CMV40_FASES_DEF = [
  { key: 'A', title: tr('tab3.fase_a_analizar_mkv_origen_guion'),       produces: 'source_analyzed', startsFrom: 'created',         reset_to: 'created' },
  { key: 'B', title: tr('tab3.fase_b_proporcionar_rpu_target_guion'),   produces: 'target_provided', startsFrom: 'source_analyzed', reset_to: 'source_analyzed' },
  { key: 'C', title: tr('tab3.fase_c_extraer_bl_el'),             produces: 'extracted',       startsFrom: 'target_provided', reset_to: 'target_provided' },
  { key: 'D', title: tr('tab3.fase_d_e_verificar_y_corregir'),  produces: 'sync_verified',   startsFrom: 'extracted',       reset_to: 'extracted' },
  { key: 'F', title: tr('tab3.fase_f_inyectar_rpu_guion'),              produces: 'injected',        startsFrom: 'sync_verified',   reset_to: 'sync_verified' },
  { key: 'G', title: tr('tab3.fase_g_remux_final_guion'),               produces: 'remuxed',         startsFrom: 'injected',        reset_to: 'injected' },
  { key: 'H', title: tr('tab3.fase_h_validacion_final'),          produces: 'validated',       startsFrom: 'remuxed',         reset_to: 'remuxed' },
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
      l6_div: tr('tab3.l6_maxcll_maxfall_estatico'),
      l1_div: tr('tab3.l1_brillo_medio_dinamico'),
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
          <span class="cmv40-ack-icon"><span data-icono="aviso"></span></span>
          <div class="cmv40-ack-title-block">
            <div class="cmv40-ack-title" data-i18n="tab3.divergencias_detectadas_confirma_como_continuar"></div>
            <div class="cmv40-ack-sub">
              <span data-i18n-html="tab3.el_bin_pasa_los_gates_pero_hay_divergencias"></span>
            </div>
          </div>
        </div>
        <ul class="cmv40-ack-list">${itemsHtml}</ul>
        <div class="cmv40-ack-actions">
          <button class="btn btn-ghost btn-md"
            onclick="_cmv40ChangeTarget('${pid}')" data-i18n-tip="tab3.vuelve_a_fase_b_para_escoger">
            <span data-icono="deshacer"></span> <span data-i18n="tab3.cambiar_target"></span>
          </button>
          <button class="btn btn-warning btn-md"
            onclick="_cmv40AcknowledgeCriticalGates('${pid}')" data-i18n-tip="tab3.reconoces_que_el_resultado_puede_ser">
            <span data-icono="aviso"></span> <span data-i18n="tab3.continuar_igualmente_resultado_degradado"></span>
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
  showToast(tr('tab3.degradacion_reconocida_pipeline_continua_fase_d'), 'info');
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
  showToast(tr('tab3.listo_para_escoger_otro_target_abre'), 'info');
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

  // Banner de "esperando turno". Sin esto, encolar una fase deja al usuario
  // mirando un botón que ya pulsó: el endpoint responde al instante pero la
  // fase puede tardar cuarenta minutos en arrancar si hay un rip por delante.
  let colaHtml = '';
  if (s.cola) {
    const c = s.cola;
    const posicion = c.total > 1
      ? tr('tab3.puesto_de', {posicion: c.posicion, total: c.total}) : tr('tab3.siguiente_en_la_cola');
    const delante = c.por_delante
      ? `<div style="font-size:12px; color:var(--text-2)">${tr('tab3.esperando_a_por_delante', {por_delante: escHtml(c.por_delante)})}</div>`
      : '';
    colaHtml = `
      <div class="section-card" style="margin-top:12px; border:1px solid var(--orange-border)">
        <div class="section-body" style="display:flex; align-items:center; gap:12px">
          ${iconoDeEstado('en_cola')}
          <div style="flex:1">
            <div style="font-weight:600; margin-bottom:2px">
              ${tr('tab3.fase_en_cola_posicion', {fase: escHtml(CMV40_RUNNING_LABELS[c.fase] || c.fase), posicion: posicion})}
            </div>
            ${delante}
          </div>
          <button class="btn btn-ghost btn-sm" onclick="cmv40CancelRunning('${pid}')" data-i18n="workbar.quitar_de_la_cola" data-i18n-tip="tab3.sacalo_de_la_cola_el_proyecto"></button>
        </div>
      </div>`;
  }

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
            data-tooltip="${escHtml(tr('tab3.vuelve_a_ejecutar_p1', {p1: activeFase.title}))}"><span data-icono="refrescar"></span> <span data-i18n="tab2.reintentar"></span></button>`
      : '';
    errorHtml = `
      <div class="section-card cmv40-card-error" style="margin-top:12px">
        <div class="section-body" style="display:flex; align-items:center; gap:12px">
          <span data-icono="aviso" data-icono-clase="ico-lg" style="display:inline-flex"></span>
          <div style="flex:1">
            <div style="font-weight:600; color:var(--red); margin-bottom:2px" data-i18n="tab3.error_en_la_ultima_accion"></div>
            <div style="font-size:12px; color:var(--text-2)">${escHtml(s.error_message)}</div>
          </div>
          ${retryBtn}
          <button class="btn btn-ghost btn-sm" onclick="_cmv40ClearError('${pid}')" data-i18n-tip="tab3.descartar_este_mensaje"><span data-icono="cruz"></span></button>
        </div>
      </div>`;
  }

  // Si done, card de celebración arriba
  let doneHtml = '';
  if (s.phase === 'done' && !s.archived) {
    doneHtml = `
      <div class="section-card" style="margin-top:16px; background:var(--green-dim); border:1px solid var(--green)">
        <div class="section-body" style="text-align:center; padding:20px">
          <div data-icono="curva" data-icono-clase="ico-2x" style="display:flex; justify-content:center"></div>
          <div style="font-size:15px; font-weight:700; margin-top:4px" data-i18n="tab3.mkv_cmv4_0_completado"></div>
          <div style="font-size:11px; color:var(--text-3); margin-top:4px">${escHtml(s.output_mkv_path || s.output_mkv_name)}</div>
          <div style="margin-top:12px; display:flex; gap:8px; justify-content:center">
            <button class="btn btn-ghost btn-sm" onclick="cmv40Cleanup('${pid}')"><span data-icono="papelera"></span> <span data-i18n="ui.limpiar_artefactos"></span></button>
          </div>
          <div style="margin-top:8px; font-size:10px; color:var(--text-3)">
            <span data-icono="aviso"></span> <span data-i18n="tab3.al_limpiar_artefactos_no_podras_rehacer"></span>
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
          <span data-icono="archivador" data-icono-clase="ico-lg" style="display:inline-flex"></span>
          <div style="flex:1">
            <div style="font-weight:600" data-i18n="tab3.proyecto_archivado_solo_lectura"></div>
            <div style="font-size:11px; color:var(--text-3); margin-top:2px" data-i18n="tab3.los_artefactos_intermedios_se_borraron_no"></div>
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
          <span data-icono="papelera" data-icono-clase="ico-lg" style="display:inline-flex; opacity:0.7"></span>
          <div style="flex:1; min-width:0">
            <div style="font-size:12.5px; font-weight:600" data-i18n="tab3.limpiar_artefactos_del_workdir"></div>
            <div style="font-size:11px; color:var(--text-3); margin-top:2px">
              <span data-i18n="tab3.libera_el_espacio_del_workdir_intermedio"></span> <strong data-i18n="tab3.pasa_el_proyecto_a_modo_solo"></strong>
            </div>
          </div>
          <button class="btn btn-ghost btn-sm" onclick="cmv40Cleanup('${pid}')"
            style="flex-shrink:0" data-i18n="ui.limpiar_artefactos"></button>
        </div>
      </div>`;
  }

  // Banner ACK (gates críticos pendientes) por encima de todo lo demás —
  // pause-point bloqueante: hasta que el usuario decida, el auto-pipeline
  // no avanza. Ver _cmv40MaybeAutoAdvance.
  const ackBannerHtml = _cmv40RenderCriticalAckBanner(pid, s);
  const html = ackBannerHtml + colaHtml + errorHtml + archivedHtml + doneHtml
             + cards.join('') + actionsFooterHtml;
  // **No se repinta si no ha cambiado.** Con un job en marcha esto corría
  // cada pocos segundos y reconstruía el panel entero para dejarlo igual;
  // de paso cerraba los `<details>` que el usuario tuviera abiertos y le
  // quitaba el foco a un input. Se compara contra la cadena que ESTE código
  // escribió y no contra `container.innerHTML`, que el navegador devuelve
  // normalizado y no coincide nunca — la trampa del `dataset.estado`.
  if (html !== project._panelHTML) {
    // Y cuando sí cambia, lo que el usuario había abierto se conserva: un
    // repintado no puede deshacer un clic suyo.
    const abiertos = anclajeDeDetalles(container);
    container.innerHTML = html;
    project._panelHTML = html;
    restaurarAnclajeDeDetalles(container, abiertos);
  }

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
  // **`!s.running_phase` se fue.** Con una fase en marcha el gráfico no se
  // cargaba nunca, así que la card de Fase D enseñaba un canvas negro
  // durante toda la Fase F y no decía por qué — reportado el 2026-09-23. Lo
  // caro no es leer el volcado (eso es un fichero que ya está y una
  // reducción a cubos): es REGENERARLO, y de eso se encarga el guard del
  // backend, que ahora rechaza la regeneración con cualquier fase corriendo.
  const shouldLoadChart = (faseDState === 'active' || faseDState === 'done')
                          && dExpanded
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

  const stateIcon = icono(isSkipped ? 'omitida'
                  : state === 'done' ? 'check'
                  : state === 'active' ? 'play' : 'candado', 'ico-lg');
  const stateLabel = isSkippedC ? tr('tab3.omitida_drop_in_no_hace_falta')
                   : isSkippedD ? tr('tab3.omitida_target_trusted_sync_validado_por')
                   : isDropInF  ? tr('tab3.ejecutada_en_modo_drop_in_inject')
                   : state === 'done' ? tr('tab3.completado')
                   : state === 'active' ? tr('workbar.en_curso') : tr('tab3.pendiente');

  // Resumen cuando está done
  let summary = '';
  if (state === 'done') {
    summary = _cmv40FaseSummary(fase.key, s);
  }

  // Body según estado
  let body = '';
  if (isExpanded) {
    if (state === 'active') {
      // La MISMA línea que el log escribe al arrancar la fase — no una
      // segunda redacción. Solo en la activa: en una pendiente sería una
      // promesa, y en una terminada, ruido.
      const suyo = _cmv40RotuloDeFase(s, fase.key, '') === (s.relato?.etapa?.rotulo || '')
        ? (s.relato?.etapa?.porque || '') : '';
      // La fase activa sigue explicando lo que va a hacer, pero mientras hay
      // trabajo en marcha NO ofrece su botón: `_cmv40PhaseState` decide
      // `active` mirando solo `s.phase`, así que la card de la fase que se
      // está ejecutando enseñaba su lanzador como si se pudiera pulsar. El
      // backend lo rechaza (`_cmv40_guard_no_duplicado`, 409), o sea que lo
      // único que producía era un toast rojo — un botón que solo sabe dar un
      // error es peor que uno que no está.
      body = (suyo ? `<div class="section-body fase-porque">${escHtml(suyo)}</div>` : '')
           + _cmv40FaseBodyBloqueable(fase.key, pid, s);
    } else if (state === 'done') {
      body = `
        <div class="section-body">
          ${_cmv40FaseDoneBody(fase.key, pid, s)}
          ${s.archived ? '' : `
          <div style="margin-top:12px; padding-top:12px; border-top:1px solid var(--sep)">
            ${_cmv40Ocupado(s)
              ? `<button class="btn btn-danger btn-sm" disabled
              data-i18n-tip="tab3.no_se_puede_rehacer_ocupado"><span data-icono="refrescar"></span> <span data-i18n="tab3.rehacer_esta_fase_2"></span></button>`
              : `<button class="btn btn-danger btn-sm" onclick="_cmv40Redo('${pid}','${fase.reset_to}','${fase.key}')"
              data-i18n-tip="tab3.vuelve_a_esta_fase_las_fases"><span data-icono="refrescar"></span> <span data-i18n="tab3.rehacer_esta_fase_2"></span></button>`}
          </div>`}
        </div>`;
    } else {
      body = `<div class="section-body"><div style="font-size:12px; color:var(--text-3)"><span data-icono="candado"></span> <span data-i18n="tab3.completa_las_fases_anteriores_para_activar"></span></div></div>`;
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
    ? tr('tab3.omitida_drop_in')
    : isSkippedD
    ? (s.user_acknowledged_degradation
        ? tr('tab3.omitida_usuario_reconocio_degradacion')
        : tr('tab3.omitida_trust_gates_ok'))
    : tr('tab3.omitida');
  const titleSuffix = isSkipped
    ? ` <span style="color:var(--text-3); font-weight:400; font-size:11px">${skippedSuffix}</span>`
    : isDropInF
    ? ' <span style="color:var(--orange-text); font-weight:500; font-size:11px">(drop-in)</span>'
    : '';
  return `
    <div class="section-card cmv40-fase-card cmv40-fase-${state}${extraCls}" style="margin-top:12px" data-fase-key="${fase.key}">
      <div class="section-header cmv40-fase-header" onclick="_cmv40TogglePhase('${pid}','${fase.key}')">
        <div class="cmv40-fase-state-icon">${stateIcon}</div>
        <div style="flex:1">
          <div class="section-title">${escHtml(_cmv40RotuloDeFase(s, fase.key, fase.title))}${titleSuffix}</div>
          <div class="section-subtitle">${subtitle}</div>
        </div>
        <div class="cmv40-fase-chevron">${icono('chevron', isExpanded ? 'chevron-abierto' : '')}</div>
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
  const icon = icono({ ok: 'check', warn: 'aviso', ko: 'cruz',
                       pending: 'pendiente' }[status] || 'pendiente');
  const color = { ok: 'var(--green-text)', warn: 'var(--orange-text)', ko: 'var(--red-text)', pending: 'var(--text-3)' }[status] || 'var(--text-3)';
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

/* ───────── Card «🛡️ Validaciones» — el volcado completo ───────────────
 *
 * Antes esta card enseñaba SEIS veredictos y ningún dato: de los ~40 campos
 * que la sesión ya tiene y que son evidencia de validación, usaba 6. En un
 * job correcto no se veía ni el profile, ni los niveles, ni los frames de
 * cada lado — no había forma de auditar la decisión, solo de leerla. Y el
 * caso que lo destapó (The Mandalorian and Grogu, 2026-09-04) decía
 * literalmente «1/1 muestras coinciden» sobre un veredicto que dependía de
 * comparar UN frame.
 *
 * Cinco bloques:
 *   ① veredicto y su CONSECUENCIA — qué fases se omiten y qué hará Fase F
 *   ② los dos RPU lado a lado, SIEMPRE, también cuando todo pasa
 *   ③ los gates con su umbral y su severidad literal
 *   ④ el desglose L5 con los tramos divergentes y sus timecodes
 *   ⑤ evidencia de apoyo (L2, L8, procedencia del bin, sheet, pre-flight)
 *
 * Los timecodes de ④ son el punto: «558 frames divergen» no es accionable y
 * «00:10:11» sí — el usuario abre el MKV ahí y lo mira.
 *
 * Todos los campos nuevos son OPCIONALES. Una sesión anterior a este cambio
 * trae `sampled_method: 'per_frame_zoned_24'` y ④ se pinta con lo que haya;
 * sin `sampled_method` no se pinta ④ y ya.
 */

/** frame → "hh:mm:ss" con el fps de la sesión (23.976 si no viene). */
function _cmv40Timecode(frame, fps) {
  const f = Number(fps) > 0 ? Number(fps) : 23.976;
  let seg = Math.floor((Number(frame) || 0) / f);
  const h = Math.floor(seg / 3600); seg -= h * 3600;
  const m = Math.floor(seg / 60);   seg -= m * 60;
  const p2 = n => String(n).padStart(2, '0');
  return `${p2(h)}:${p2(m)}:${p2(seg)}`;
}

/** Miles con punto. Manual y no `toLocaleString` para que el resultado no
 *  dependa del ICU del entorno (los tests evalúan esto en node). */
function _cmv40Num(n) {
  // El separador de miles lo decide el IDIOMA, no el código: cableado a `.`
  // salía «141.336» con la app en inglés, donde toca «141,336».
  //
  // `useGrouping: 'always'` **no es decorativo**: `es-ES` sigue la RAE y NO
  // agrupa los números de cuatro cifras, así que sin él «2.313 escenas»
  // pasaba a «2313» — un cambio del castellano que nadie pidió. Con él, el
  // castellano sale byte a byte como antes y el inglés se arregla.
  return Math.round(Number(n) || 0)
    .toLocaleString(localeActual(), {useGrouping: 'always'});
}

/** Segundos → "1min 23s" / "45s". */
function _cmv40Dur(segundos) {
  const s = Math.round(Number(segundos) || 0);
  return s >= 60 ? `${Math.floor(s / 60)}min ${s % 60}s` : `${s}s`;
}

/** Tupla de active area [top,bottom,left,right] → "275/275/0/0". */
function _cmv40L5Tupla(t) {
  return Array.isArray(t) ? t.map(v => Number(v) || 0).join('/') : '—';
}

/** Marcador de comparación entre los dos lados del bloque ②. */
function _cmv40CmpMarca(a, b) {
  if (a === null || a === undefined || a === '' || a === '—') {
    return (b === null || b === undefined || b === '' || b === '—')
      ? { txt: '', color: 'var(--text-3)' }
      : { txt: tr('tab3.rpu_l8_nuevo'), color: 'var(--blue-text)' };
  }
  if (b === null || b === undefined || b === '' || b === '—') {
    return { txt: tr('tab3.solo_bd'), color: 'var(--orange-text)' };
  }
  return String(a) === String(b)
    ? { html: icono('check'), color: 'var(--green-text)' }
    : { txt: '≠', color: 'var(--orange-text)' };
}

/** Fila de dos columnas del bloque ②. */
function _cmv40RpuFila(etiqueta, a, b, marcaOverride) {
  const m = marcaOverride || _cmv40CmpMarca(a, b);
  const val = v => (v === null || v === undefined || v === '') ? '—' : String(v);
  return `
    <div style="display:grid; grid-template-columns:120px 1fr 1fr 104px; gap:8px; padding:4px 8px; border-bottom:1px solid var(--sep); font-size:11.5px">
      <div style="color:var(--text-2); font-weight:600">${escHtml(etiqueta)}</div>
      <div style="color:var(--text-1)">${escHtml(val(a))}</div>
      <div style="color:var(--text-1)">${escHtml(val(b))}</div>
      <div style="color:${m.color}; font-weight:700; text-align:right">${
        m.html || escHtml(m.txt || '')}</div>
    </div>`;
}

/** Cabecera de bloque numerado. */
function _cmv40BloqueHead(num, titulo, extra) {
  return `
    <div style="display:flex; align-items:baseline; gap:8px; margin:14px 0 6px">
      <span style="font-size:12px; font-weight:800; color:var(--blue-text)">${escHtml(num)}</span>
      <span style="font-size:11.5px; font-weight:800; letter-spacing:.03em; text-transform:uppercase; color:var(--text-2)">${escHtml(titulo)}</span>
      ${extra ? `<span style="font-size:11px; color:var(--text-3)">${escHtml(extra)}</span>` : ''}
    </div>`;
}

/** Los marcadores de `phases_skipped`, en nombres que signifiquen algo. */
function _cmv40SkipLabel(marker) {
  return ({
    demux_dual_layer:       tr('tab3.omitido_demux_dual_layer'),
    mux_dual_layer:         tr('tab3.omitido_mux_dual_layer'),
    per_frame_data_skipped: tr('tab3.datos_per_frame_del_chart_fase'),
    sync_verification_pause:tr('tab3.revision_visual_de_sync_fases_d'),
    merge_cmv40_transfer:   tr('tab3.omitido_merge_cmv40'),
  })[marker] || marker;
}

// ── ① Veredicto y consecuencia ───────────────────────────────────────
function _cmv40GateBloque1(pid, s) {
  const trust   = _cmv40Trust(s);
  const dropIn  = _cmv40DropIn(s);
  const skipped = (s.phases_skipped || []).map(_cmv40SkipLabel);
  const wf      = s.output_workflow || (dropIn ? 'restore_dropin' : 'restore_merge');

  const queHaraF = dropIn
    ? tr('tab3.fase_f_inyectara_rpu_target_bin')
    : tr('tab3.fase_f_transferira_los_niveles_cmv4');

  let ackHtml = '';
  if (s.awaiting_critical_ack) {
    // Los BOTONES viven en el banner ámbar del panel (arriba del todo, fuera
    // de una card colapsable, que es donde tiene que estar un pause-point).
    // Aquí van los NÚMEROS, que es lo que el banner no tiene: sin ellos el
    // usuario acepta una degradación sin saber de cuánta película habla.
    const l5 = (s.target_trust_gates || {}).l5_div || {};
    const mt = l5.mayor_tramo || null;
    const detalles = [];
    if (typeof l5.body_coverage === 'number') {
      detalles.push(tr('tab3.el_cuerpo_de_la_pelicula_coincide', {p1: (l5.body_coverage * 100).toFixed(2)}));
    }
    if (mt && mt.frames) {
      detalles.push(tr('tab3.el_mayor_tramo_divergente_son_frames', {frames: _cmv40Num(mt.frames), segundos: _cmv40Dur(mt.segundos)}));
    }
    if (l5.divergentes && l5.comparados) {
      detalles.push(tr('tab3.de_frames_difieren', {divergentes: _cmv40Num(l5.divergentes), comparados: _cmv40Num(l5.comparados)}));
    }
    ackHtml = `
      <div style="margin-top:8px; padding:10px 12px; background:rgba(255,149,0,0.12); border:1px solid rgba(255,149,0,0.35); border-radius:6px">
        <div style="font-size:12px; font-weight:700; color:var(--orange-text)"><span data-icono="aviso"></span> <span data-i18n="tab3.esperando_tu_confirmacion"></span></div>
        <div style="font-size:11.5px; color:var(--text-2); line-height:1.5; margin-top:3px">
          ${tr('tab3.aviso_los_botones_estan_arriba', {
            motivo: detalles.length
              ? tr('tab3.si_continuas_aceptas_que_p1', {p1: escHtml(detalles.join(' · '))})
              : tr('tab3.hay_divergencias_que_la_fase_d'),
            p1: `<em>${tr('tab3.cambiar_target')}</em>`,
            p2: `<em>${tr('tab3.continuar_igualmente')}</em>`})}
        </div>
      </div>`;
  }

  return `
    ${_cmv40BloqueHead('①', tr('tab3.veredicto_y_consecuencia'))}
    <div style="font-size:12px; line-height:1.7; color:var(--text-1)">
      <div><strong>${tr(trust ? 'tab3.trusted' : 'tab3.sin_trust_automatico')}</strong>
        ${tr('tab3.p1_workflow', {p1: escHtml(s.trust_override || 'auto')})} <code>${escHtml(wf)}</code></div>
      <div style="color:var(--text-2)">${tr('tab3.se_omiten_p1', {p1: skipped.length ? escHtml(skipped.join(' · ')) : tr('tab3.ninguna_fase')})}</div>
      <div style="color:var(--text-2)">${escHtml(queHaraF)}</div>
    </div>
    ${ackHtml}`;
}

// ── ② Los dos RPU, lado a lado ───────────────────────────────────────
function _cmv40GateBloque2(s) {
  const sdv = s.source_dv_info || null;
  const tdv = s.target_dv_info || null;
  if (!sdv && !tdv) return '';
  const l5g = (s.target_trust_gates || {}).l5_div || {};

  const NIVELES = ['1','2','3','4','5','6','8','9','10','11'];
  const setNiveles = dv => new Set(
    dv ? NIVELES.filter(n => dv['has_l' + n]).map(n => 'L' + n) : []);
  const niveles = dv => {
    if (!dv) return '—';
    const ns = [...setNiveles(dv)];
    return ns.length ? ns.join(' ') : '—';
  };
  // Si «Niveles» dice que el bin trae L9, esta fila no puede decir «—». El
  // pipeline de CMv4.0 **no rellena** `l9_primaries` ni `l11_content_type`
  // —solo lo hacen Tab 1 y Tab 2—, así que el VALOR falta mientras el nivel
  // está, y las dos filas se contradecían. Mismo patrón que `l8txt`: se dice
  // «presente» en vez de fingir una ausencia. Con el flag a false, el guion
  // sí es lo correcto.
  const presente = (dv, valor, flag) =>
    !dv ? '—' : (valor || (dv[flag] ? tr('tab3.gate_valor_presente') : '—'));
  // Y si de un lado solo sabemos que el nivel ESTÁ, no hay comparación que
  // hacer: el comparador genérico pintaría `≠` en ámbar, afirmando una
  // discrepancia que nadie ha medido. Es la misma regla que en el L3 — no
  // concluir desde un hueco.
  const soloSiLosDosSeConocen = (a, b) =>
    (a === tr('tab3.gate_valor_presente') || b === tr('tab3.gate_valor_presente'))
      ? { txt: '', color: 'var(--text-3)' } : null;
  const l5txt = (dv, perfil) => {
    if (perfil && Array.isArray(perfil.valores) && perfil.valores.length) {
      const v = perfil.valores;
      if (!perfil.variable && v.length === 1) return `${_cmv40L5Tupla(v[0][0])} constante`;
      const tot = v.reduce((a, x) => a + (Number(x[1]) || 0), 0) + (Number(perfil.sin_bloque) || 0);
      const trozos = v.slice(0, 2).map(x =>
        `${_cmv40L5Tupla(x[0])} ${tot ? Math.round((Number(x[1]) || 0) / tot * 100) : 0}%`);
      if (Number(perfil.sin_bloque) > 0) {
        trozos.push(tr('tab3.sin_bloque', {p1: tot ? Math.round(Number(perfil.sin_bloque) / tot * 100) : 0}));
      }
      return `VARIABLE · ${trozos.join(' · ')}`;
    }
    if (!dv) return '—';
    return `${dv.l5_top || 0}/${dv.l5_bottom || 0}/${dv.l5_left || 0}/${dv.l5_right || 0}`;
  };
  const l8txt = dv => {
    if (!dv) return '—';
    const nits = Array.isArray(dv.l8_trim_nits) && dv.l8_trim_nits.length
      ? dv.l8_trim_nits.join(', ') + ' nits' : '';
    const idx = Array.isArray(dv.l8_target_indices) && dv.l8_target_indices.length
      ? ` · idx [${dv.l8_target_indices.join(', ')}]` : '';
    return nits ? nits + idx : (dv.has_l8 ? tr('tab3.gate_valor_presente') : '—');
  };
  const l1txt = dv => (!dv || (!dv.l1_max_cll && !dv.l1_max_fall)) ? '—'
    : `${dv.l1_max_cll || 0} / ${dv.l1_max_fall || 0} nits`;
  // El L3 distingue TRES cosas, y hasta hoy la tabla no enseñaba ninguna:
  // no medido (el summary de `dovi_tool info` no emite L3, así que ningún
  // RPU lo traía), medido y ausente, y medido con su cuenta.
  const l3txt = dv => {
    if (!dv || !dv.l3_medido) return tr('tab3.rpu_sin_medir');
    if (!dv.l3_unique_count) return tr('tab3.rpu_sin_l3');
    return tr('tab3.rpu_l3_ajustes', { n: _cmv40Num(dv.l3_unique_count) });
  };
  const l6txt = dv => !dv ? '—' : `${dv.l6_max_cll || 0} nits`;

  const perfS = l5g.perfil_source || null;
  const perfT = l5g.perfil_target || null;
  const cmS = sdv ? (sdv.cm_version || '—') : '—';
  const cmT = tdv ? (tdv.cm_version || '—') : '—';
  const marcaCm = (cmS !== '—' && cmT !== '—' && cmS !== cmT)
    ? { txt: '↑ upgrade', color: 'var(--blue-text)' } : null;
  const nivS = niveles(sdv), nivT = niveles(tdv);
  // Estaba clavado a `+L8`, así que un bin que además trae L3 y L9 —el caso
  // normal de un máster CMv4.0— se anunciaba como si solo trajera el L8. Se
  // resta el conjunto en LOS DOS sentidos: perder un nivel también hay que
  // verlo, y en ámbar.
  const nsS = setNiveles(sdv), nsT = setNiveles(tdv);
  const mas   = [...nsT].filter(x => !nsS.has(x));
  const menos = [...nsS].filter(x => !nsT.has(x));
  const marcaNiv = (mas.length || menos.length)
    ? { txt: [...mas.map(x => '+' + x), ...menos.map(x => '−' + x)].join(' '),
        color: menos.length ? 'var(--orange-text)' : 'var(--blue-text)' }
    : null;
  const l5S = l5txt(sdv, perfS), l5T = l5txt(tdv, perfT);
  // Lo que el bin APORTA, que es la pregunta de este bloque: el ajuste de
  // tonos medios lo calcula el análisis de Dolby y el Blu-ray no lo trae
  // —medido sobre un RPU P7 MEL CM v2.9, el export de `level3` sale vacío—
  // así que traerlo es una mejora real aunque el L8 esté plano.
  //
  // La CELDA dice lo que SABEMOS («sin medir» · «sin L3» · la cuenta) y la
  // MARCA lo que el bin APORTA. Antes la marca exigía que el disco estuviera
  // medido y, al no estarlo, caía al comparador genérico — que no pinta
  // nada: pinta `≠` en NARANJA. O sea que «no lo hemos mirado» acababa en
  // pantalla como «discrepan», que es peor que la conclusión que se quería
  // evitar. Reportado mirando la card el 2026-09-23.
  //
  // Y no sale de un hueco: medido sobre un RPU P7 MEL CM v2.9 real, el
  // export de `level3` sale VACÍO. El Blu-ray no trae L3, así que que el bin
  // lo traiga mejora el juego de metadata aunque su L8 esté plano — que es
  // justo lo que dice el tercer veredicto del criterio CMv4.0.
  const l9S  = presente(sdv, sdv && sdv.l9_primaries, 'has_l9');
  const l9T  = presente(tdv, tdv && tdv.l9_primaries, 'has_l9');
  const l11S = presente(sdv, sdv && sdv.l11_content_type, 'has_l11');
  const l11T = presente(tdv, tdv && tdv.l11_content_type, 'has_l11');
  const l3n = dv => (dv && dv.l3_unique_count) || 0;
  const l3medido = dv => !!(dv && dv.l3_medido);
  const marcaL3 =
      (l3n(tdv) && !l3n(sdv)) ? { txt: '↑ upgrade', color: 'var(--blue-text)' }
    : (l3n(sdv) && !l3n(tdv)) ? { txt: tr('tab3.solo_bd'), color: 'var(--orange-text)' }
    // Ninguno de los dos medido: no hay nada que comparar, y un ✓ verde
    // afirmaría que coinciden dos huecos.
    : (!l3medido(sdv) || !l3medido(tdv)) ? { txt: '', color: 'var(--text-3)' }
    : null;

  return `
    ${_cmv40BloqueHead('②', tr('tab3.los_dos_rpu_lado_a_lado'))}
    <div style="border:1px solid var(--sep); border-radius:6px; overflow:hidden">
      <div style="display:grid; grid-template-columns:120px 1fr 1fr 104px; gap:8px; padding:6px 8px; background:rgba(0,122,255,0.06); font-size:11px; font-weight:800; color:var(--text-2)">
        <div></div><div data-i18n="tab3.bd_source"></div><div data-i18n="tab3.bin_target"></div><div style="text-align:right" data-i18n="tab3.rpu_diferencias"></div>
      </div>
      ${_cmv40RpuFila('Profile', sdv ? `${sdv.profile}${sdv.el_type ? ' (' + sdv.el_type + ')' : ''}` : '—',
                                 tdv ? `${tdv.profile}${tdv.el_type ? ' (' + tdv.el_type + ')' : ''}` : '—')}
      ${_cmv40RpuFila('CM version', cmS, cmT, marcaCm)}
      ${_cmv40RpuFila('Frames', sdv ? _cmv40Num(sdv.frame_count) : '—', tdv ? _cmv40Num(tdv.frame_count) : '—')}
      ${_cmv40RpuFila(tr('tab3.rpu_escenas'), sdv ? _cmv40Num(sdv.scene_count) : '—', tdv ? _cmv40Num(tdv.scene_count) : '—')}
      ${_cmv40RpuFila(tr('tab3.rpu_niveles'), nivS, nivT, marcaNiv)}
      ${_cmv40RpuFila('L1 CLL/FALL', l1txt(sdv), l1txt(tdv))}
      ${_cmv40RpuFila(tr('tab3.rpu_l3_tonos_medios'), l3txt(sdv), l3txt(tdv), marcaL3)}
      ${_cmv40RpuFila('L5 active area', l5S, l5T)}
      ${_cmv40RpuFila('L6 MaxCLL', l6txt(sdv), l6txt(tdv))}
      ${_cmv40RpuFila('L8 trims', l8txt(sdv), l8txt(tdv))}
      ${_cmv40RpuFila('L9 primaries', l9S, l9T, soloSiLosDosSeConocen(l9S, l9T))}
      ${_cmv40RpuFila(tr('tab3.rpu_l11_contenido'), l11S, l11T,
                      soloSiLosDosSeConocen(l11S, l11T))}
    </div>`;
}

// ── ③ Gates: valor · umbral · severidad ──────────────────────────────
/** Fila de gate con el umbral y la severidad literal al lado del valor.
 *  `_cmv40GateRowHtml` se queda como está: lo usa también la card G/H. */
function _cmv40GateFilaHtml(status, titulo, valor, umbral, sev, critical, explicacion) {
  const icon  = icono({ ok: 'check', warn: 'aviso', ko: 'cruz',
                        pending: 'pendiente' }[status] || 'pendiente');
  const color = { ok: 'var(--green-text)', warn: 'var(--orange-text)', ko: 'var(--red-text)', pending: 'var(--text-3)' }[status] || 'var(--text-3)';
  const bg    = { ok: 'rgba(52,199,89,0.10)', warn: 'rgba(255,149,0,0.10)', ko: 'rgba(255,59,48,0.10)', pending: 'rgba(0,0,0,0.03)' }[status] || 'transparent';
  const chip = (txt, c) => `<span style="font-size:10px; font-weight:700; padding:1px 6px; border-radius:8px; background:rgba(15,23,42,0.06); color:${c}">${escHtml(txt)}</span>`;
  return `
    <div style="display:grid; grid-template-columns:24px 1fr; gap:10px; padding:9px 12px; background:${bg}; border-radius:6px; margin-bottom:6px">
      <div style="font-size:16px; font-weight:700; color:${color}; text-align:center">${icon}</div>
      <div>
        <div style="display:flex; gap:8px; align-items:baseline; flex-wrap:wrap">
          <span style="font-size:12px; font-weight:700; color:var(--text-1)">${escHtml(titulo)}</span>
          <span style="font-size:11px; color:${color}; font-weight:600">${escHtml(valor)}</span>
          ${umbral ? `<span style="font-size:10.5px; color:var(--text-3)">${escHtml(tr('tab3.gate_umbral', {umbral: umbral}))}</span>` : ''}
          ${sev ? chip(sev, color) : ''}
          ${critical ? chip(tr('tab3.gate_critico'), 'var(--text-2)') : ''}
        </div>
        <div style="font-size:11px; color:var(--text-2); line-height:1.5; margin-top:2px">${escHtml(explicacion)}</div>
      </div>
    </div>`;
}

function _cmv40GateBloque3(s) {
  const g = s.target_trust_gates || {};
  const rows = [];
  const estado = gate => {
    const sev = gate.severity || (gate.ok ? 'ok' : 'ack_required');
    if (sev === 'ok') return 'ok';
    if (sev === 'hard_abort' || sev === 'ack_required') return 'ko';
    return 'warn';
  };

  if (g.frames) {
    const ok = g.frames.ok;
    rows.push(_cmv40GateFilaHtml(estado(g.frames), tr('tab3.numero_de_frames'),
      ok ? `${_cmv40Num(g.frames.bd)} = ${_cmv40Num(g.frames.target)}`
         : `${_cmv40Num(g.frames.bd)} ≠ ${_cmv40Num(g.frames.target)}`,
      tr('tab3.gate_umbral_exacto'), g.frames.severity, g.frames.critical,
      ok ? tr('tab3.source_y_target_tienen_exactamente_el')
         : tr('tab3.diferencia_de_frames_0_suele_indicar')));
  }
  if (g.cm_version) {
    rows.push(_cmv40GateFilaHtml(estado(g.cm_version), tr('tab3.cm_version_del_target'),
      `CM ${g.cm_version.value || '?'}`, '= v4.0', g.cm_version.severity, g.cm_version.critical,
      g.cm_version.ok
        ? tr('tab3.el_target_esta_firmado_como_cmv4')
        : tr('tab3.el_target_no_es_cmv4_0')));
  }
  if (g.has_l8) {
    rows.push(_cmv40GateFilaHtml(estado(g.has_l8), tr('tab3.presencia_de_l8'),
      g.has_l8.ok ? tr('tab3.gate_valor_presente') : tr('tab3.gate_valor_ausente'),
      tr('tab3.gate_valor_presente'), g.has_l8.severity, g.has_l8.critical,
      g.has_l8.ok
        ? tr('tab3.el_bin_contiene_trims_l8_autenticos')
        : tr('tab3.bin_cmv4_0_vacio_sin_l8')));
  }
  if (g.l5_div) {
    const l5 = g.l5_div;
    const completo = l5.sampled_method === 'per_frame_completo';
    let valor, umbral;
    if (completo && typeof l5.body_coverage === 'number') {
      valor = tr('tab3.l5_cuerpo_pct', {pct: (l5.body_coverage * 100).toFixed(2)});
      umbral = '≥ 90%';
    } else if (l5.sampled_method === 'per_frame_zoned_24') {
      valor = tr('tab3.l5_muestras', {ok: l5.sampled_matches || 0,
                                      total: l5.sampled_total || 0});
      umbral = tr('tab3.l5_umbral_cuerpo');
    } else {
      valor = tr('tab3.l5_div_px', {px: l5.px_max || 0});
      umbral = `≤ ${l5.soft_px != null ? l5.soft_px : 5} px`;
    }
    rows.push(_cmv40GateFilaHtml(estado(l5), tr('tab3.l5_letterbox'),
      valor, umbral, l5.severity, l5.critical,
      l5.why || tr('tab3.compara_el_active_area_del_bin')));
  }
  if (g.l6_div) {
    rows.push(_cmv40GateFilaHtml(estado(g.l6_div), tr('tab3.l6_maxcll_maxfall_estatico'),
      `Δ ${g.l6_div.nits_diff} nits`,
      `≤ ${g.l6_div.threshold != null ? g.l6_div.threshold : 50} nits`,
      g.l6_div.severity, g.l6_div.critical,
      g.l6_div.why || tr('tab3.la_metadata_hdr_estatica_del_target')));
  }
  if (g.l1_div) {
    rows.push(_cmv40GateFilaHtml(estado(g.l1_div), tr('tab3.l1_maxcll_del_metadata_dinamico'),
      `Δ ${g.l1_div.pct_diff}%`,
      `≤ ${g.l1_div.threshold_pct != null ? g.l1_div.threshold_pct : 5}%`,
      g.l1_div.severity, g.l1_div.critical,
      g.l1_div.why || tr('tab3.pico_de_brillo_de_todo_el_metraje')));
  }
  if (!rows.length) return '';
  return _cmv40BloqueHead('③', tr('tab3.gates'), tr('tab3.valor_umbral_severidad')) + rows.join('');
}

// ── ④ Desglose L5 ────────────────────────────────────────────────────
function _cmv40GateBloque4(s) {
  const l5 = (s.target_trust_gates || {}).l5_div || {};
  const metodo = l5.sampled_method;
  if (!metodo) return '';   // el gate no se refinó: no hay nada que desglosar

  const fps = l5.fps || s.source_fps || 23.976;
  const lin = (k, v) => `
    <div style="display:grid; grid-template-columns:190px 1fr; gap:8px; font-size:11.5px; padding:2px 0">
      <div style="color:var(--text-2)">${escHtml(k)}</div><div style="color:var(--text-1)">${escHtml(v)}</div>
    </div>`;

  // Sesiones anteriores al cambio: solo tenían el muestreo de 24.
  if (metodo !== 'per_frame_completo') {
    const zc = l5.sampled_zone_counts || {};
    const zm = l5.sampled_zone_mismatches || {};
    return `
      ${_cmv40BloqueHead('④', tr('tab3.desglose_l5'), tr('tab3.muestreo_antiguo_24_frames'))}
      <div style="padding:8px 10px; background:rgba(0,0,0,0.02); border-radius:6px">
        ${lin(tr('tab3.l5_pares_comparados'), tr('tab3.n_coinciden_de_n', {n: l5.sampled_matches || 0, total: l5.sampled_total || 0}))}
        ${lin(tr('tab3.l5_por_zona_muestras'), tr('tab3.l5_zonas', {intro: `${zm.intro || 0}/${zc.intro || 0}`,
                              cuerpo: `${zm.body || 0}/${zc.body || 0}`,
                              outro: `${zm.outro || 0}/${zc.outro || 0}`}))}
        ${lin(tr('tab3.l5_cobertura_cuerpo'), `${Math.round((l5.sampled_body_coverage || 0) * 100)}%`)}
        <div style="font-size:11px; color:var(--text-3); font-style:italic; margin-top:6px">
          ${tr('tab3.proyecto_analizado_con_el_muestreo_antiguo', {p1: l5.sampled_total || 0})}
        </div>
      </div>`;
  }

  const pz = l5.por_zona || {};
  const zonaTxt = z => {
    const p = pz[z];
    return Array.isArray(p) ? `${_cmv40Num(p[0])}/${_cmv40Num(p[1])}` : '—';
  };
  const perfil = (etiqueta, p) => {
    if (!p) return lin(etiqueta, '—');
    const vals = (p.valores || []).slice(0, 4)
      .map(x => `${_cmv40L5Tupla(x[0])} ×${_cmv40Num(x[1])}`).join(' · ');
    const sin = Number(p.sin_bloque) > 0
      ? tr('tab3.sin_bloque_neutro', {sin_bloque: _cmv40Num(p.sin_bloque)}) : '';
    return lin(etiqueta, tr('tab3.l5_con_bloque', {n: _cmv40Num(p.frames_con_bloque)})
      + `${sin} — ${vals || '—'}${p.variable ? '  [VARIABLE]' : '  [constante]'}`);
  };

  const mt = l5.mayor_tramo || {};
  const umbralT = l5.umbral_tramo_segundos != null ? l5.umbral_tramo_segundos : 40;
  const tramos = (l5.tramos || []).map(t => {
    const [desde, hasta, n, zona] = t;
    return `
      <div style="display:grid; grid-template-columns:1fr 1fr 70px 70px; gap:8px; font-size:11px; padding:2px 8px; border-bottom:1px solid var(--sep)">
        <div style="color:var(--text-1)">${escHtml(_cmv40Num(desde))}–${escHtml(_cmv40Num(hasta))}</div>
        <div style="color:var(--text-2)">${escHtml(_cmv40Timecode(desde, fps))} – ${escHtml(_cmv40Timecode(hasta, fps))}</div>
        <div style="color:var(--text-1); text-align:right">${escHtml(_cmv40Num(n))} f</div>
        <div style="color:var(--text-3); text-align:right">${escHtml(zona || '')}</div>
      </div>`;
  }).join('');

  // La procedencia contradice a la medición: el nombre del bin declara L5
  // variable (señal con precisión 100% sobre los 99 proyectos del corpus,
  // cero falsos positivos) y aquí no la hemos detectado. Con esa precisión
  // el error es NUESTRO, así que el aviso va pegado a la medición, no a los
  // gates: no es un gate, la cuestiona.
  const proc = l5.procedencia || {};
  const avisoProc = proc.contradice ? `
    <div style="margin-top:8px; padding:9px 11px; background:rgba(255,149,0,0.12); border:1px solid rgba(255,149,0,0.35); border-radius:6px; font-size:11.5px; color:var(--orange-text); line-height:1.5">
      <span data-icono="aviso"></span> <span data-i18n="tab3.el_nombre_del_bin_declara"></span> <strong><span data-i18n="tab3.l5_variable"></span></strong>${tr('tab3.p1_pero_la_medicion_no_ha', {p1: (proc.tokens || []).length ? ` (${escHtml((proc.tokens || []).join(', '))})` : ''})}
    </div>` : '';

  return `
    ${_cmv40BloqueHead('④', tr('tab3.desglose_l5'), tr('tab3.comparacion_frame_a_frame'))}
    <div style="padding:8px 10px; background:rgba(0,0,0,0.02); border-radius:6px">
      ${perfil(tr('tab3.l5_perfil_bd'), l5.perfil_source)}
      ${perfil(tr('tab3.l5_perfil_bin'), l5.perfil_target)}
      ${lin(tr('tab3.l5_comparados'), tr('tab3.l5_frames_divergen', {frames: _cmv40Num(l5.comparados),
                                                     div: _cmv40Num(l5.divergentes)}) +
            (l5.comparados ? ` (${((l5.divergentes || 0) / l5.comparados * 100).toFixed(2)}%)` : ''))}
      ${lin(tr('tab3.l5_por_zona_div'), tr('tab3.l5_zonas', {intro: zonaTxt('intro'), cuerpo: zonaTxt('body'),
                              outro: zonaTxt('outro')}))}
      ${lin(tr('tab3.l5_cobertura_cuerpo'), tr('tab3.l5_cobertura_umbral', {pct: ((l5.body_coverage || 0) * 100).toFixed(2)}))}
      ${lin(tr('tab3.l5_mayor_tramo'), mt.frames
            ? `${_cmv40Num(mt.frames)} frames · ${_cmv40Dur(mt.segundos)} · ${mt.zona || '—'} · desde ${_cmv40Num(mt.desde)}   (umbral ${umbralT}s)`
            : '—')}
      ${tramos ? `
        <div style="margin-top:8px">
          <div style="display:grid; grid-template-columns:1fr 1fr 70px 70px; gap:8px; font-size:10.5px; font-weight:800; color:var(--text-2); padding:3px 8px; background:rgba(0,122,255,0.06); border-radius:4px 4px 0 0">
            <div data-i18n="tab2.frames"></div><div data-i18n="tab3.timecode"></div><div style="text-align:right" data-i18n="tab3.longitud"></div><div style="text-align:right"><span data-i18n="tab3.zona"></span></div>
          </div>
          ${tramos}
        </div>` : ''}
      ${avisoProc}
    </div>`;
}

// ── ⑤ Evidencia de apoyo ─────────────────────────────────────────────
function _cmv40GateBloque5(pid, s) {
  const lin = (k, v) => v ? `
    <div style="display:grid; grid-template-columns:110px 1fr; gap:8px; font-size:11.5px; padding:3px 0">
      <div style="color:var(--text-2); font-weight:600">${escHtml(k)}</div>
      <div style="color:var(--text-1)">${escHtml(v)}</div>
    </div>` : '';

  const l2 = s.l2_comparison
    ? tr('tab3.combos_bd_vs_del_bin', {l2_comparison: s.l2_comparison, source_l2_unique_count: _cmv40Num(s.source_l2_unique_count), target_l2_unique_count: _cmv40Num(s.target_l2_unique_count)})
      + (Array.isArray(s.target_l2_target_pqs) && s.target_l2_target_pqs.length
         ? ` · peaks ${s.target_l2_target_pqs.join('/')}` : '')
    : '';

  const l8 = s.target_l8_classification
    ? `${s.target_l8_classification} · ${s.target_l8_quality_label || '—'} · ${_cmv40Num(s.target_l8_unique_count)} combos`
      + (typeof s.target_l8_neutral_frames_pct === 'number'
         ? tr('tab3.l8_pct_neutro', {pct: (s.target_l8_neutral_frames_pct * 100).toFixed(1)}) : '')
      + (s.target_l8_has_mid_contrast ? ' ' + tr('tab3.mid_contrast_si') : '')
      + (s.target_l8_has_clip_trim ? ' ' + tr('tab3.clip_trim_si') : '')
    : '';

  const proc = ((s.target_trust_gates || {}).l5_div || {}).procedencia || {};
  const binName = s.target_rpu_source_label || s.target_rpu_path || s.pending_target_name || '';
  const binCorto = binName ? binName.split('/').pop() : '';
  const procTxt = binCorto
    ? binCorto + (proc.declara_l5_variable
        ? tr('tab3.declara_l5_variable', {tokens: (proc.tokens || []).join(', ')}) : '')
    : '';

  const sr = s.sheet_recommendation || null;
  const fila0 = sr && Array.isArray(sr.rows) && sr.rows.length ? sr.rows[0] : {};
  const sheet = sr
    ? `${sr.status || '—'}` + (fila0.dv_source ? tr('tab3.hoja_fuente', {fuente: fila0.dv_source}) : '')
      + (fila0.sync_offset != null ? ` · sync ${fila0.sync_offset}` : '')
      + (fila0.notes ? ` · «${fila0.notes}»` : '')
    : '';

  const pf = [
    s.source_preflight_ok != null ? `source ${s.source_preflight_ok ? 'ok' : 'ko'}` : '',
    s.target_preflight_ok != null ? `target ${s.target_preflight_ok ? 'ok' : 'ko'}` : '',
    s.preflight_decision ? tr('tab3.decision', {preflight_decision: s.preflight_decision}) : '',
  ].filter(Boolean).join(' · ');

  const filas = lin('L2', l2) + lin('L8', l8) + lin(tr('tab3.ev_procedencia'), procTxt)
              + lin('Sheet', sheet) + lin('Pre-flight', pf)
              + lin(tr('tab3.ev_recomendacion'), s.recommended_action_label || '');
  if (!filas) return '';

  return `
    ${_cmv40BloqueHead('⑤', tr('tab3.evidencia_de_apoyo'))}
    <div style="padding:8px 10px; background:rgba(0,0,0,0.02); border-radius:6px">
      ${filas}
      <div style="margin-top:8px; text-align:right">
        <button class="btn btn-ghost btn-xs" onclick="_cmv40CopiarDiagnostico('${pid}', this)" data-i18n-tip="tab3.vuelca_los_cinco_bloques_en_texto"><span data-icono="portapapeles"></span> <span data-i18n="tab3.copiar_diagnostico"></span></button>
      </div>
    </div>`;
}

/** El volcado en texto plano de los cinco bloques — para pegarlo en un
 *  informe o en una conversación sin tener que capturar la pantalla. */
function _cmv40GateDiagnosticoTexto(s) {
  const L = [];
  const g = s.target_trust_gates || {};
  const l5 = g.l5_div || {};
  const sdv = s.source_dv_info || {}, tdv = s.target_dv_info || {};
  L.push(`=== Validaciones — ${s.output_mkv_name || s.id || ''}`);
  L.push(`① ${_cmv40Trust(s) ? 'Trusted' : 'Sin trust'} (${s.trust_override || 'auto'}) · workflow ${s.output_workflow || '—'}`);
  L.push(`   fases omitidas: ${(s.phases_skipped || []).map(_cmv40SkipLabel).join(' · ') || 'ninguna'}`);
  L.push('② RPU                    BD (source)              Bin (target)');
  const par = (k, a, b) => L.push(`   ${String(k).padEnd(20)} ${String(a).padEnd(24)} ${b}`);
  par('Profile', `${sdv.profile || '—'} ${sdv.el_type || ''}`, `${tdv.profile || '—'} ${tdv.el_type || ''}`);
  par('CM version', sdv.cm_version || '—', tdv.cm_version || '—');
  par('Frames', _cmv40Num(sdv.frame_count), _cmv40Num(tdv.frame_count));
  par('Escenas', _cmv40Num(sdv.scene_count), _cmv40Num(tdv.scene_count));
  par('L1 CLL/FALL', `${sdv.l1_max_cll || 0}/${sdv.l1_max_fall || 0}`, `${tdv.l1_max_cll || 0}/${tdv.l1_max_fall || 0}`);
  L.push('③ Gates');
  Object.keys(g).forEach(k => {
    const gate = g[k] || {};
    L.push(`   ${String(k).padEnd(12)} sev=${gate.severity || '?'}  ok=${gate.ok}  ${gate.critical ? 'crítico' : ''}`);
    if (gate.why) L.push(`                ${gate.why}`);
  });
  if (l5.sampled_method === 'per_frame_completo') {
    const fps = l5.fps || s.source_fps || 23.976;
    L.push('④ Desglose L5');
    L.push(`   comparados ${_cmv40Num(l5.comparados)} · divergen ${_cmv40Num(l5.divergentes)}`);
    const pz = l5.por_zona || {};
    ['intro', 'body', 'outro'].forEach(z => {
      if (Array.isArray(pz[z])) L.push(`   ${z.padEnd(6)} ${_cmv40Num(pz[z][0])}/${_cmv40Num(pz[z][1])}`);
    });
    L.push(`   cobertura cuerpo ${((l5.body_coverage || 0) * 100).toFixed(2)}%`);
    (l5.tramos || []).forEach(t => L.push(
      `   tramo ${_cmv40Num(t[0])}–${_cmv40Num(t[1])}  ${_cmv40Timecode(t[0], fps)}–${_cmv40Timecode(t[1], fps)}  ${t[2]} f  ${t[3] || ''}`));
  }
  L.push('⑤ Evidencia');
  L.push(`   L2 ${s.l2_comparison || '—'} · ${_cmv40Num(s.source_l2_unique_count)} vs ${_cmv40Num(s.target_l2_unique_count)}`);
  L.push(`   L8 ${s.target_l8_classification || '—'} · ${s.target_l8_quality_label || '—'} · ${_cmv40Num(s.target_l8_unique_count)} combos`);
  L.push(`   bin ${(s.target_rpu_source_label || s.target_rpu_path || '').split('/').pop() || '—'}`);
  return L.join('\n');
}

/** Handler del botón «Copiar diagnóstico». */
async function _cmv40CopiarDiagnostico(pid, btn) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project || !project.session) return;
  const texto = _cmv40GateDiagnosticoTexto(project.session);
  const ok = await _copyTextToClipboardWithFallback(texto);
  showToast(ok ? tr('tab3.diagnostico_copiado_al_portapapeles') : tr('tab1.no_se_pudo_copiar_al_portapapeles'),
            ok ? 'success' : 'error');
  if (ok && btn) {
    const orig = btn.textContent;
    btn.innerHTML = icono('check') + ' ' + tr('tab3.copiado');
    setTimeout(() => { btn.textContent = orig; }, 1200);
  }
}

function _cmv40RenderGateCardBC(pid, s, isExpanded) {
  const curIdx   = CMV40_PHASES_ORDER.indexOf(s.phase);
  const bIdx     = CMV40_PHASES_ORDER.indexOf('target_provided');
  const hasData  = curIdx >= bIdx && (s.target_dv_info || s.target_trust_gates);
  const compatErr= !!s.compat_warning;
  const trustOk  = s.target_trust_ok === true;

  let overallIcon, overallLabel;
  if (compatErr) { overallIcon = icono('aviso', 'ico-lg'); overallLabel = tr('tab3.abortada_combinacion_incompatible'); }
  else if (!hasData) { overallIcon = icono('candado', 'ico-lg'); overallLabel = tr('tab3.pendiente_se_evalua_al_cerrar_fase'); }
  else if (s.awaiting_critical_ack) { overallIcon = icono('aviso', 'ico-lg'); overallLabel = tr('tab3.esperando_tu_confirmacion'); }
  else if (trustOk) { overallIcon = icono('check', 'ico-lg'); overallLabel = tr('tab3.trusted_todos_los_criticos_pasan'); }
  else { overallIcon = icono('aviso', 'ico-lg'); overallLabel = tr('tab3.sin_trust_automatico_flujo_completo_manual'); }

  // Resumen del header: cuántos gates y qué se omite, que es la consecuencia.
  let summary;
  if (compatErr) summary = tr('tab3.combinacion_incompatible', {p1: s.source_workflow || '?', p2: s.target_type || '?'});
  else if (!hasData) summary = tr('tab3.se_evaluan_al_tener_target_comparacion');
  else {
    const g = s.target_trust_gates || {};
    const claves = Object.keys(g);
    const pasan = claves.filter(k => (g[k] || {}).ok).length;
    const omitidas = (s.phases_skipped || []).length;
    summary = `${pasan}/${claves.length} gates`
      + (_cmv40DropIn(s) ? ' · drop-in' : '')
      + (omitidas ? tr('tab3.n_fases_omitidas', {n: omitidas}) : '');
  }

  let body = '';
  if (isExpanded) {
    if (compatErr) {
      body = `
        <div class="section-body">
          ${_cmv40GateRowHtml('ko', 'Compatibilidad estructural', 'abortada',
            s.compat_warning || 'Source y target estructuralmente incompatibles — la inyección produciría un MKV inválido.')}
        </div>`;
    } else if (!hasData) {
      body = `
        <div class="section-body">
          <div style="font-size:12px; color:var(--text-3); font-style:italic" data-i18n="tab3.aun_sin_datos_completa_fase_b"></div>
        </div>`;
    } else {
      body = `
        <div class="section-body">
          <div style="font-size:12px; color:var(--text-2); line-height:1.5">
            <span data-i18n-html="tab3.que_se_valida_aqui_fase_b"></span>
          </div>
          ${_cmv40GateBloque1(pid, s)}
          ${_cmv40GateBloque2(s)}
          ${_cmv40GateBloque3(s)}
          ${_cmv40GateBloque4(s)}
          ${_cmv40GateBloque5(pid, s)}
        </div>`;
    }
  }

  return `
    <div class="section-card cmv40-gate-card" style="margin-top:12px; border-left:3px solid rgba(0,122,255,0.55)">
      <div class="section-header cmv40-fase-header" onclick="_cmv40TogglePhase('${pid}','GATE_BC')" style="cursor:pointer">
        <div class="cmv40-fase-state-icon" style="font-size:20px">${overallIcon}</div>
        <div style="flex:1">
          <div class="section-title" style="color:var(--blue-text)"><span data-icono="escudo"></span> <span data-i18n="tab3.validaciones_trust_gates_compatibilidad"></span></div>
          <div class="section-subtitle">${escHtml(overallLabel)} · ${escHtml(summary)}</div>
        </div>
        <div class="cmv40-fase-chevron">${icono('chevron', isExpanded ? 'chevron-abierto' : '')}</div>
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
    overallIcon = icono('check', 'ico-lg');
    overallLabel = tr('tab3.validacion_final_ok');
    summary = tr('tab3.el_mkv_contiene_cmv4_0_el');
  } else if (state === 'running') {
    overallIcon = icono('reloj', 'ico-lg');
    overallLabel = tr('tab3.validacion_en_curso');
    summary = tr('tab3.verificando_profile_cm_v4_0_frame');
  } else {
    overallIcon = icono('candado', 'ico-lg');
    overallLabel = tr('tab3.pendiente');
    summary = tr('tab3.se_ejecuta_tras_completar_fase_g');
  }

  let body = '';
  if (isExpanded) {
    const rows = [];
    // Profile
    const targetProfile = s.source_dv_info?.profile || '?';
    rows.push(_cmv40GateRowHtml(state === 'done' ? 'ok' : 'pending',
      tr('tab3.profile_del_hevc_resultante'),
      state === 'done' ? `Profile ${targetProfile}` : '—',
      state === 'done'
        ? tr('tab3.el_mkv_final_tiene_el_profile')
        : tr('tab3.se_verifica_que_el_profile_coincide')));
    // CM version
    rows.push(_cmv40GateRowHtml(state === 'done' ? 'ok' : 'pending',
      tr('tab3.cm_version_del_mkv'),
      state === 'done' ? tr('tab3.cm_v4_0_confirmado') : '—',
      state === 'done'
        ? tr('tab3.dovi_tool_extract_rpu_info_sobre')
        : tr('tab3.se_verifica_que_el_rpu_del')));
    // Frame count
    rows.push(_cmv40GateRowHtml(state === 'done' ? 'ok' : 'pending',
      'Frame count',
      state === 'done' ? `${(s.source_frame_count || 0).toLocaleString(localeActual())} frames` : '—',
      state === 'done'
        ? tr('tab3.el_numero_de_frames_del_mkv')
        : tr('tab3.se_compara_frame_count_del_resultado')));
    // Estructura MKV
    rows.push(_cmv40GateRowHtml(state === 'done' ? 'ok' : 'pending',
      tr('tab3.estructura_matroska'),
      state === 'done' ? tr('tab3.mkv_valido_mkvmerge_j_ok') : '—',
      state === 'done'
        ? tr('tab3.mkvmerge_j_lee_el_fichero_sin')
        : tr('tab3.se_verifica_que_el_contenedor_mkv')));

    body = `
      <div class="section-body">
        <div style="font-size:12px; color:var(--text-2); line-height:1.5; margin-bottom:10px">
          <span data-i18n-html="tab3.que_se_valida_aqui_fase_h"></span>
        </div>
        ${rows.join('')}
      </div>`;
  }

  return `
    <div class="section-card cmv40-gate-card" style="margin-top:12px; border-left:3px solid rgba(0,122,255,0.55)">
      <div class="section-header cmv40-fase-header" onclick="_cmv40TogglePhase('${pid}','GATE_GH')" style="cursor:pointer">
        <div class="cmv40-fase-state-icon" style="font-size:20px">${overallIcon}</div>
        <div style="flex:1">
          <div class="section-title" style="color:var(--blue-text)"><span data-icono="escudo"></span> <span data-i18n="tab3.validacion_final_pre_finalizar"></span></div>
          <div class="section-subtitle">${escHtml(overallLabel)} · ${escHtml(summary)}</div>
        </div>
        <div class="cmv40-fase-chevron">${icono('chevron', isExpanded ? 'chevron-abierto' : '')}</div>
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
  'generic':               { icon: 'ajustes', label: tr('tab3.target_generico'),             desc: tr('tab3.flujo_completo_merge_cmv4_0_revision') },
  'trusted_p8_source':     { icon: 'caja', label: 'Target P8 + CMv4.0 (trusted)', desc: tr('tab3.bin_pre_validado_rama_b_skip') },
  'trusted_p7_fel_final':  { icon: 'diana', label: 'Target P7 FEL CMv4.0 final',   desc: tr('tab3.drop_in_skip_merge_en_fase') },
  'trusted_p7_mel_final':  { icon: 'diana', label: 'Target P7 MEL CMv4.0 final',   desc: tr('tab3.drop_in_mel_skip_fase_d') },
  'incompatible':          { icon: 'cruz', label: tr('tab3.target_incompatible'),          desc: tr('tab3.sin_cmv4_0_no_sirve_como') },
};

function _cmv40FaseSummary(key, s) {
  const arts = s.artifacts || {};
  if (key === 'A' && s.source_dv_info) {
    const d = s.source_dv_info;
    return `Profile ${d.profile}${d.el_type ? ` (${d.el_type})` : ''} · CM ${d.cm_version} · ${s.source_frame_count.toLocaleString(localeActual())} frames`;
  }
  if (key === 'B' && s.target_dv_info) {
    const d = s.target_dv_info;
    return `CM ${d.cm_version} · ${s.target_frame_count.toLocaleString(localeActual())} frames (Δ ${s.sync_delta > 0 ? '+' : ''}${s.sync_delta})`;
  }
  if (key === 'C') {
    const sizes = ['BL.hevc', 'EL.hevc', 'per_frame_data.json'].map(n => arts[n] || 0);
    const total = sizes.reduce((a, b) => a + b, 0);
    return total > 0 ? tr('tab3.bl_hevc_el_hevc_y_per', {total: _fmtBytes(total)}) : tr('tab3.bl_hevc_el_hevc_y_datos');
  }
  if (key === 'D') {
    const trustedSkipped = _cmv40Trust(s);
    if (trustedSkipped) return tr('tab3.omitida_target_trusted_sync_validado_por');
    return s.sync_config ? tr('tab3.correccion_aplicada_2', {sync_delta: s.sync_delta}) : tr('tab3.sincronizacion_verificada_0');
  }
  if (key === 'F') {
    // En drop-in FEL el artefacto es source_injected.hevc (BL+EL intactos);
    // en merge clasico es EL_injected.hevc (solo EL). Preferimos el que exista.
    const dropIn = arts['source_injected.hevc'];
    const merge  = arts['EL_injected.hevc'];
    if (dropIn) return tr('tab3.source_injected_generado_drop_in', {p1: _fmtBytes(dropIn)});
    if (merge)  return tr('tab3.el_injected_generado', {p1: _fmtBytes(merge)});
    return tr('tab3.hevc_con_rpu_inyectado_generado');
  }
  if (key === 'G') {
    // El MKV se escribe en /mnt/output (fuera del workdir) por lo que no sale
    // del scan de artifacts. Mostramos el nombre directo del session.
    const name = s.output_mkv_name || '';
    return name ? tr('tab3.mkv_remuxado_pre_validacion_2', {name: name}) : tr('tab3.mkv_remuxado_pre_validacion');
  }
  if (key === 'H') return s.output_mkv_path ? tr('tab3.movido_a_p1', {p1: s.output_mkv_path}) : tr('tab3.validado');
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
        <div><span style="color:var(--text-3)"><span data-i18n="tab3.profile"></span></span> ${d.profile}${d.el_type ? ` (${d.el_type})` : ''}</div>
        <div><span style="color:var(--text-3)"><span data-i18n="tab3.cm_version"></span></span> ${d.cm_version}</div>
        <div><span style="color:var(--text-3)"><span data-i18n="tab3.frames"></span></span> ${s.source_frame_count.toLocaleString(localeActual())}</div>
        ${d.has_l1 ? '<div><span style="color:var(--text-3)">Metadata:</span> L1 L2 L5 L6</div>' : ''}
      </div>`;
  }
  if (key === 'B' && s.target_dv_info) {
    const d = s.target_dv_info;
    const srcType = s.target_rpu_source === 'drive' ? 'Repo DoviTools'
                   : s.target_rpu_source === 'mkv' ? tr('tab3.extraido_de_otro_mkv')
                   : tr('tab3.carpeta_nas');
    const shortHash = s.target_rpu_sha256 ? s.target_rpu_sha256.slice(0, 12) : '';
    const hashLine = shortHash
      ? `<div><span style="color:var(--text-3)"><span data-i18n="tab3.sha_256"></span></span> <code title="${escHtml(s.target_rpu_sha256)}" style="font-size:11px">${shortHash}…</code></div>`
      : '';
    // Mostrarlo aqui ademas duplicaba la informacion.
    return `
      <div style="font-size:12px; line-height:1.8">
        <div><span style="color:var(--text-3)"><span data-i18n="tab3.fuente"></span></span> ${srcType}</div>
        <div><span style="color:var(--text-3)"><span data-i18n="tab3.path"></span></span> <code>${escHtml(s.target_rpu_path || '—')}</code></div>
        ${hashLine}
        <div><span style="color:var(--text-3)"><span data-i18n="tab3.cm_version"></span></span> ${d.cm_version}</div>
        <div><span style="color:var(--text-3)"><span data-i18n="tab3.frames"></span></span> ${s.target_frame_count.toLocaleString(localeActual())}</div>
        <div><span style="color:var(--text-3)"><span data-i18n="tab3.vs_origen"></span></span> <b style="color:${s.sync_delta === 0 ? 'var(--green)' : 'var(--orange)'}">${tr('tab3.p1_sync_delta_frames', {p1: s.sync_delta > 0 ? '+' : '', sync_delta: s.sync_delta})}</b></div>
        <div style="margin-top:8px; font-size:11px; color:var(--text-3); font-style:italic"><span data-icono="bombilla"></span> <span data-i18n="tab3.los_resultados_de_los_trust_gates"></span></div>
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
          <span class="banner-icon"><span data-icono="check"></span></span>
          <span><span data-i18n-html="tab3.fase_d_omitida_los_gates_pasaron"></span></span>
        </div>
        <div style="font-size:11px; color:var(--text-3); font-style:italic; margin-top:6px"><span data-icono="bombilla"></span> <span data-i18n="tab3.los_resultados_de_los_gates_estan"></span></div>`;
    }
    const syncConfigHtml = s.sync_config
      ? `<div style="margin-bottom:10px; font-size:12px">
          <span style="color:var(--text-3)" data-i18n="tab3.correccion_aplicada"></span>
          <div style="margin-top:4px">${escHtml(_cmv40ResumenDeCorreccion(s.sync_config))}</div>
          <details data-detalle="sync-json" style="margin-top:6px">
            <summary style="font-size:11px; color:var(--text-3); cursor:pointer" data-i18n="tab3.correccion_ver_json"></summary>
            <pre style="margin-top:6px; font-size:11px; background:var(--surface-2); padding:8px; border-radius:4px">${escHtml(JSON.stringify(s.sync_config, null, 2))}</pre>
          </details>
        </div>`
      : '<div style="font-size:12px; color:var(--text-3); margin-bottom:10px">' + tr('tab3.sincronizacion_confirmada_sin_correccion_2') + '</div>';
    return `
      ${syncConfigHtml}
      <div style="font-size:11px; color:var(--text-3); margin-bottom:8px" data-i18n="tab3.navegacion_por_el_grafico_en_solo"></div>
      <div id="cmv40-sync-stats-${pid}" class="cmv40-sync-stats"></div>
      <div id="cmv40-chart-wrap-${pid}" class="cmv40-chart-wrap">
        <canvas id="cmv40-chart-${pid}" width="1000" height="280"></canvas>
        <div class="cmv40-chart-tooltip" id="cmv40-chart-tooltip-${pid}" style="display:none"></div>
      </div>
      <div class="cmv40-sync-controls" id="cmv40-sync-controls-${pid}"></div>
      <div id="cmv40-confidence-${pid}"></div>`;
  }
  if (key === 'H' && s.output_mkv_path) {
    return `<div style="font-size:12px"><span style="color:var(--text-3)"><span data-i18n="tab3.mkv_final"></span></span> <code>${escHtml(s.output_mkv_path)}</code></div>`;
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
          <div style="color:var(--text-3); margin-bottom:6px" data-i18n="tab3.artefactos_generados"></div>
          <div style="display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px dashed var(--sep); opacity:0.7">
            <code style="font-size:11px">${escHtml(name)}</code>
            <span style="font-size:11px; color:var(--text-3)" data-i18n="tab3.consumido_tras_validacion"></span>
          </div>
          <div style="font-size:11px; color:var(--text-3); margin-top:6px; line-height:1.4">
            <span data-i18n="tab3.el_hevc_intermedio_se_borra_automaticamente"></span> <code>/mnt/output</code>.
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
        <div style="color:var(--text-3); margin-bottom:6px" data-i18n="tab3.mkv_remuxado_pre_validacion_fase_h"></div>
        <div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px dashed var(--sep); gap:8px">
          <code style="font-size:11px; word-break:break-all">${escHtml(name)}</code>
          <span style="font-size:11px; color:var(--text-3); white-space:nowrap" data-i18n="tab3.escrito_en_mnt_output"></span>
        </div>
        <div style="font-size:11px; color:var(--text-3); margin-top:6px; line-height:1.4">
          <span data-i18n-html="tab3.sufijo_mkv_tmp_mientras_fase_h"></span>
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
      <span style="font-size:11px; color:var(--text-3)" data-i18n="tab3.no_encontrado"></span>
    </div>`;
  }).join('');
  const total = fileNames.reduce((acc, n) => acc + (arts[n] || 0), 0);
  return `
    <div style="font-size:12px">
      <div style="color:var(--text-3); margin-bottom:6px"><span data-i18n="tab3.artefactos_generados"></span></div>
      ${rows}
      ${total > 0 ? `<div style="margin-top:6px; font-size:11px; color:var(--text-3); text-align:right"><span data-i18n="tab3.total"></span> <b>${_fmtBytes(total)}</b></div>` : ''}
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
          <b>${tr('tab3.se_borraran_p1_artefacto_s', {p1: preview.files.length})}</b> ${tr('tab3.total_bytes_liberados', {total_bytes: _fmtBytes(preview.total_bytes)})}
        </div>
        <ul style="margin:0; padding-left:18px">${rows}</ul>
      </div>`;
  } else {
    artifactsList = '<div style="font-size:11px; color:var(--text-3); margin-top:8px">' + tr('tab3.no_hay_artefactos_posteriores_que_borrar') + '</div>';
  }

  // Uso el modal cmv40-confirm-modal que acepta HTML en el body
  document.getElementById('cmv40-confirm-title').textContent = tr('tab3.rehacer_esta_fase');
  document.getElementById('cmv40-confirm-sub').textContent = tr('tab3.la_sesion_volvera_al_estado_previo');
  document.getElementById('cmv40-confirm-body').innerHTML = artifactsList;
  const confirmBtn = document.getElementById('cmv40-confirm-btn');
  confirmBtn.textContent = tr('tab3.rehacer_y_borrar_artefactos');
  confirmBtn.className = 'btn btn-danger btn-sm';
  const newBtn = confirmBtn.cloneNode(true);
  confirmBtn.parentNode.replaceChild(newBtn, confirmBtn);
  newBtn.addEventListener('click', async () => {
    closeModal('cmv40-confirm-modal');
    const data = await apiFetch(`/api/cmv40/${pid}/reset-to/${targetPhase}`,
                                { method: 'POST' }, API_FETCH_TIMEOUT_LARGO);
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
      showToast(tr('tab3.fase_lista_para_rehacer', {fasekey: faseKey}), 'info');
    }
  });
  openModal('cmv40-confirm-modal');
}

// ── Tarjetas por fase ────────────────────────────────────────────

/** El rótulo de una fase, del relato — no de la tabla local.
 *
 *  Había CUATRO variantes del nombre de la Fase A: `[Fase A]` en el log,
 *  «Fase A — Analizar MKV origen» en la card, «Fase A — Analizando el MKV
 *  origen» en la columna de trabajo y `analyze_source` por dentro. El
 *  usuario las veía todas. El relato tiene una, y el respaldo es el título
 *  local para que una sesión cacheada sin relato siga pintando algo.
 */
function _cmv40RotuloDeFase(s, key, porDefecto) {
  const e = (s?.relato?.etapas || []).find(x => x.letra === key);
  return e?.rotulo || porDefecto;
}

/** El cuerpo de una fase: qué le pasa a tu película, y debajo el detalle.
 *
 *  Las cards describían HERRAMIENTAS —«dovi_tool mux combina BL.hevc +
 *  EL_injected.hevc en un HEVC dual-layer»— que dice qué binario corre, no
 *  qué está pasando con tu fichero. El detalle no se tira: se pliega, porque
 *  es justo lo que hace falta cuando algo va mal.
 */
function _cmv40DosCapas(humano, tecnico) {
  return `
    <div class="fase-que-pasa">${humano}</div>
    ${tecnico ? `<details class="fase-detalle">
      <summary data-i18n="tab3.detalle_tecnico"></summary>
      <div class="fase-detalle-cuerpo">${tecnico}</div>
    </details>` : ''}`;
}

function _cmv40FaseABody(pid, s) {
  return `
    <div class="section-body">
      ${_cmv40DosCapas('<span data-i18n="tab3.que_pasa_fase_a"></span>',
                        '<span data-i18n="tab3.extrae_el_stream_hevc_y_el"></span>')}
      <button class="btn btn-primary btn-md" onclick="cmv40DoAnalyzeSource('${pid}')"><span data-icono="lupa"></span> <span data-i18n="tab3.analizar_origen"></span></button>
    </div>`;
}

function _cmv40FaseBBody(pid, s) {
  return `
    <div class="section-body">
      <div style="font-size:12px; color:var(--text-3); margin-bottom:10px" data-i18n="tab3.elige_una_fuente_del_rpu_cmv4"></div>
      <div class="cmv40-tab-switcher">
        <button class="cmv40-tab-btn active" id="cmv40-tab-btn-repo-${pid}"
          onclick="_cmv40SwitchTargetTab('${pid}','repo')"><span data-icono="caja"></span> <span data-i18n="ui.repo_dovitools"></span></button>
        <button class="cmv40-tab-btn" id="cmv40-tab-btn-mkv-${pid}"
          onclick="_cmv40SwitchTargetTab('${pid}','mkv')"><span data-icono="claqueta"></span> <span data-i18n="tab3.extraer_de_otro_mkv"></span></button>
        <button class="cmv40-tab-btn" id="cmv40-tab-btn-path-${pid}"
          onclick="_cmv40SwitchTargetTab('${pid}','path')"><span data-icono="abrir"></span> <span data-i18n="tab3.carpeta_nas"></span></button>
      </div>

      <div id="cmv40-target-repo-${pid}" class="cmv40-target-tab">
        <div id="cmv40-repo-info-${pid}" style="font-size:12px;color:var(--text-3);margin-bottom:8px" data-i18n="tab3.cargando_candidatos_del_repositorio"></div>
        <div id="cmv40-repo-list-${pid}" class="cmv40-repo-list" style="max-height:280px;overflow-y:auto"></div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:12px">
          <button class="btn btn-primary btn-md" onclick="cmv40DoTargetFromDrive('${pid}')"><span data-icono="flechaAbajo"></span> <span data-i18n="tab3.descargar_y_usar"></span></button>
          <button class="btn btn-secondary btn-sm" onclick="_cmv40LoadRepoForPanel('${pid}')"><span data-icono="deshacer"></span> <span data-i18n="tab3.refrescar"></span></button>
        </div>
      </div>

      <div id="cmv40-target-path-${pid}" class="cmv40-target-tab" style="display:none">
        <label class="modal-field-label" data-i18n="tab3.rpu_disponible_en_mnt_cmv40_rpus"></label>
        <div class="iso-select-row">
          <select id="cmv40-rpu-select-${pid}" class="iso-select">
            <option value="" data-i18n="ui.cargando_2"></option>
          </select>
          <button class="btn btn-secondary btn-sm" onclick="_cmv40LoadRpus('${pid}')"><span data-icono="deshacer"></span></button>
        </div>
        <button class="btn btn-primary btn-md" style="margin-top:12px" onclick="cmv40DoTargetFromPath('${pid}')"><span data-icono="check"></span> <span data-i18n="tab3.usar_este_rpu"></span></button>
      </div>

      <div id="cmv40-target-mkv-${pid}" class="cmv40-target-tab" style="display:none">
        <label class="modal-field-label" data-i18n="tab3.mkv_que_ya_tiene_cmv4_0"></label>
        <div class="iso-select-row">
          <select id="cmv40-target-mkv-select-${pid}" class="iso-select">
            <option value="" data-i18n="ui.cargando_2"></option>
          </select>
          <button class="btn btn-secondary btn-sm" onclick="_cmv40LoadTargetMkvs('${pid}')"><span data-icono="deshacer"></span></button>
        </div>
        <button class="btn btn-primary btn-md" style="margin-top:12px" onclick="cmv40DoTargetFromMkv('${pid}')"><span data-icono="tijeras"></span> <span data-i18n="tab3.extraer_rpu_del_mkv"></span></button>
      </div>
    </div>`;
}

function _cmv40FaseCBody(pid, s) {
  // El warning del Δ frames debe matizar que Fase D puede omitirse si los
  // trust gates aprobaron alineación (no siempre habrá "revisión visual").
  const trust = _cmv40Trust(s);
  const deltaNote = trust
    ? tr('tab3.los_trust_gates_ya_validaron_la')
    : tr('tab3.se_evaluara_en_fase_d_chart');
  return `
    <div class="section-body">
      ${_cmv40DosCapas('<span data-i18n="tab3.que_pasa_fase_c"></span>',
                        '<span data-i18n="tab3.separa_el_hevc_en_bl_capa"></span>')}
      ${s.sync_delta !== 0 ? `<div class="banner warning" style="margin-bottom:10px"><span class="banner-icon"><span data-icono="aviso"></span></span><span>${tr('tab3.diferencia_de_frames_detectada_delta', {delta: `${s.sync_delta > 0 ? '+' : ''}${s.sync_delta}`, nota: deltaNote})}</span></div>` : ''}
      <button class="btn btn-primary btn-md" onclick="cmv40DoExtract('${pid}')"><span data-icono="tijeras"></span> <span data-i18n="tab3.extraer_bl_el_per_frame_data"></span></button>
    </div>`;
}

function _cmv40FaseDBody(pid, s) {
  return `
    <div class="section-body">
      ${_cmv40DosCapas('<span data-i18n="tab3.que_pasa_fase_d"></span>',
                        '<span data-i18n="tab3.chart_de_maxpq_l1_del_rpu"></span>')}
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
    desc = tr('tab3.inyecta_el_rpu_del_bin_directamente');
  } else if (wf === 'p7_fel') {
    desc = tr('tab3.merge_cmv4_0_sobre_el_rpu');
  } else if (wf === 'p7_mel') {
    desc = targetNeedsMerge
      ? tr('tab3.merge_cmv4_0_sobre_el_rpu_2')
      : tr('tab3.inyecta_el_rpu_target_directamente_en_3');
  } else {  // p8
    desc = targetNeedsMerge
      ? tr('tab3.merge_cmv4_0_sobre_el_rpu_3')
      : tr('tab3.inyecta_el_rpu_target_directamente_en_4');
  }
  const reviewBanner = faseDExecutedVisually
    ? '<div class="banner info" style="margin-bottom:10px"><span class="banner-icon"><span data-icono="info"></span></span><span>' + tr('tab3.verifica_en_el_chart_de_fase_d') + '</span></div>'
    : '';
  return `
    <div class="section-body">
      ${_cmv40DosCapas('<span data-i18n="tab3.que_pasa_fase_f"></span>', escHtml(desc))}
      ${reviewBanner}
      <button class="btn btn-primary btn-md" onclick="cmv40DoInject('${pid}')"><span data-icono="inyectar"></span> <span data-i18n="tab3.inyectar_rpu"></span></button>
    </div>`;
}

function _cmv40FaseGBody(pid, s) {
  // Texto dinámico según workflow + drop-in. Misma lógica que el sidebar.
  const trust = _cmv40Trust(s);
  const wf = s.source_workflow || 'p7_fel';
  const dropIn = _cmv40DropIn(s);
  let desc;
  if (dropIn) {
    desc = tr('tab3.mkvmerge_directo_sobre_source_injected_hevc_2');
  } else if (wf === 'p7_fel') {
    desc = tr('tab3.dovi_tool_mux_combina_bl_hevc_2');
  } else {  // p7_mel / p8: single-layer
    desc = tr('tab3.sin_mux_dual_layer_single_layer_2');
  }
  return `
    <div class="section-body">
      ${_cmv40DosCapas('<span data-i18n="tab3.que_pasa_fase_g"></span>', escHtml(desc))}
      <button class="btn btn-primary btn-md" onclick="cmv40DoRemux('${pid}')"><span data-icono="caja"></span> <span data-i18n="tab3.remux_mkv_final"></span></button>
    </div>`;
}

/** El cuerpo de la fase activa, sin lanzadores si ya hay trabajo en marcha.
 *
 *  No se toca el HTML que devuelven los `_cmv40Fase?Body`: se envuelve. Una
 *  sustitución sobre la cadena generada es justo lo que partió un `class` en
 *  la migración de i18n y dejó un `querySelector` sin encontrar nada durante
 *  semanas. La clase apaga los `.btn-primary`, que en los cuerpos de fase son
 *  siempre los lanzadores (los controles del gráfico de la Fase D son
 *  `btn-ghost`).
 */
function _cmv40FaseBodyBloqueable(key, pid, s) {
  const cuerpo = _cmv40FaseBody(key, pid, s);
  if (!s.running_phase && !s.cola) return cuerpo;
  // Sin nombrar la fase: el título de la card y su chip «En curso» ya la
  // dicen, y repetirla daba «Fase G — Remux final … Fase G — Remuxando MKV
  // final está en curso».
  const aviso = s.running_phase
    ? tr('tab3.fase_en_curso_no_relanzar')
    : tr('tab3.fase_en_cola_no_relanzar');
  return `<div class="fase-bloqueada">
      <div class="fase-bloqueada-aviso"><span data-icono="reloj"></span> ${escHtml(aviso)}</div>
      ${cuerpo}
    </div>`;
}


function _cmv40FaseHBody(pid, s) {
  // Qué comprueba la Fase H depende de la ruta, y la diferencia es de dos
  // órdenes de magnitud: por drop-in son segundos (el RPU se copió entero
  // del bin) y por merge son dos `extract-rpu` completos. Con un texto único
  // la rama corta parecía no comprobar nada. Se lee del plan —`fast_path` es
  // lo que la fase ramifica— con el respaldo de siempre para las sesiones
  // que aún no lo traen.
  // `tr()` aquí y no un `data-i18n="${clave}"`: una clave metida en un
  // atributo por interpolación no la ve el guard del catálogo, y si no
  // existiera se pintaría en crudo.
  const rapido = s?.plan?.validate?.fast_path ?? _cmv40DropIn(s);
  const quePasa = rapido ? tr('tab3.que_pasa_fase_h_rapido')
                         : tr('tab3.que_pasa_fase_h_completo');
  return `
    <div class="section-body">
      ${_cmv40DosCapas(escHtml(quePasa),
                        '<span data-i18n="tab3.verifica_que_el_mkv_resultante_tiene"></span>')}
      <button class="btn btn-primary btn-md" onclick="cmv40DoValidate('${pid}')"><span data-icono="check"></span> <span data-i18n="tab3.validar_y_finalizar"></span></button>
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
  await _cmv40PostFase(`/api/cmv40/${pid}/analyze-source`);
  _cmv40PhaseToast(pid, tr('tab3.analizando_origen'));
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
/** Arranca una fase, tolerando el 409 de «ya la arrancó el otro disparador».
 *
 *  El auto-pipeline tiene DOS disparadores —el orquestador del backend y
 *  `_cmv40MaybeAutoAdvance`— y el servidor rechaza el segundo con un 409.
 *  Eso es el guard haciendo su trabajo, no un fallo: lo que el usuario veía
 *  era un toast ROJO de «ya hay una fase en curso» justo después de pulsar
 *  Continuar, y el job continuando igualmente. Reportado el 2026-09-23 en la
 *  Fase D y otra vez en la G.
 *
 *  **No se silencia el 409 entero**, que sería tapar dos avisos que sí
 *  importan (la sesión con un error sin resolver, el gate de sync sin pasar).
 *  Se mira la cabecera `X-Fase-Ya-En-Curso`, que el guard pone y el texto no
 *  puede sustituir: el detalle está traducido y comparar prosa del catálogo
 *  es lo que `test_el_paso_no_se_adivina` prohíbe.
 */
/** ¿Este proyecto tiene trabajo en marcha o esperando turno?
 *
 *  Rehacer una fase borra artefactos y rebobina el estado, así que con algo
 *  en vuelo el backend lo rechaza con un 409. El botón se veía activo
 *  igualmente y lo único que producía era un toast rojo — el mismo criterio
 *  que ya se aplica al lanzador de la fase activa unas líneas más arriba:
 *  un botón que solo sabe dar un error es peor que uno que no está.
 *  Reportado el 2026-09-23.
 *
 *  Cuenta también la COLA: una fase esperando turno correría después contra
 *  un estado rebobinado, que es la misma incoherencia con más retardo.
 */
function _cmv40Ocupado(s) {
  return !!(s && (s.running_phase || s.cola));
}

async function _cmv40PostFase(url, opts = {}) {
  const est = {};
  const r = await apiFetch(url, { ...opts, method: opts.method || 'POST',
                                  silent: true, estado: est });
  if (r) return r;
  if (est.status === 409
      && est.headers && est.headers.get('X-Fase-Ya-En-Curso') === '1') {
    return { ya_en_curso: true };        // benigno: alguien llegó antes
  }
  if (est.detalle) showToast(tr('comun.error_p1', {p1: est.detalle}), 'error');
  return null;
}

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
  // El aviso de pausa caduca al cambiar de fase. Va ANTES de los `return`
  // de abajo: si no, rehacer una fase y volver a pararse en el mismo punto
  // se quedaría sin aviso.
  if (project._pausaAvisadaEn && project._pausaAvisadaEn !== s.phase) {
    project._pausaAvisadaEn = null;
  }
  if (s.running_phase || s.error_message || s.archived) return;
  // YA ESPERA TURNO. Desde que un turno de cola es el proyecto entero, entre
  // «encolado» y «corriendo» hay un hueco en el que `running_phase` sigue a
  // null: el backend ya tiene el trabajo apuntado y aquí se veía como «no hay
  // nada en marcha, arranca la fase». Cada intento se lo comía el guard de
  // duplicados con un 409, y el usuario un toast rojo cada cuatro segundos.
  if (s.cola) return;
  // ABRIR UN PROYECTO NO ARRANCA TRABAJO.
  //
  // En `created` el auto-avance no *reanuda* nada: *empieza* el job (el
  // pre-flight o los 12 min de Fase A). Y esta función la llaman también la
  // apertura del proyecto (a los 100 ms) y el safety poller (cada 4 s), así
  // que con `auto_pipeline=true` persistido en el backend bastaba con hacer
  // clic en el proyecto del sidebar para lanzar Fase A sin pedirlo — y al
  // cancelar, el poller volvía a dispararla.
  //
  // Caso real (El día de la revelación, 2026-09-04): dos arranques a las
  // 10:45 y las 11:00 UTC sobre un proyecto cuyo MKV origen ya no existe.
  //
  // `_autoChaining` distingue las dos cosas: lo enciende quien SÍ pidió
  // arrancar —la creación del proyecto— y se mantiene mientras la cadena
  // avanza, así que el encadenado pre-flight → Fase A sigue funcionando. En
  // una apertura viene sin definir. Es el mismo razonamiento que ya llevaba
  // escrito el toggle de auto: «el switch solo marca el modo de trabajo, NO
  // dispara fases por sí mismo; lanzar con el toggle sería sorprendente».
  if (s.phase === 'created' && !project._autoChaining) return;
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
        // **Una vez por llegada a la pausa.** Este brazo no avanza de fase:
        // solo avisa. Y a `_cmv40MaybeAutoAdvance` la llaman el poller de
        // fase, el de seguridad (cada 4 s) y el WS, así que mientras el job
        // esperaba aquí el aviso salía una y otra vez — reportado el
        // 2026-09-23. Los demás brazos no lo tenían porque avanzan, y al
        // avanzar dejan de entrar.
        if (project._pausaAvisadaEn !== s.phase) {
          project._pausaAvisadaEn = s.phase;
          showToast(tr('tab3.auto_pausado_en_fase_d_los'), 'info');
        }
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
      showToast(tr('tab3.pipeline_cmv4_0_completado_mkv_listo'), 'success');
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
  await _cmv40PostFase(`/api/cmv40/${pid}/inject`);
  _cmv40PollPhase(pid, 'injected');
}

// Para target trusted: confirma sync OK sin intervención manual y avanza
// automáticamente a Fase F (inject).
/** Fallback del criterio de la Fase D cuando la respuesta no trae `sync_gate`
 *  (sesión cacheada de antes del cambio). La fuente de verdad es
 *  `evaluate_sync_gate` en el backend; esto solo evita una UI en blanco. */
function _cmv40SyncGateLocal(delta, confOk, confPct) {
  if (delta !== 0) {
    return { ok: false, reason: tr('tab3.hay_diferencia_de_frames_corrigela_antes', {p1: delta > 0 ? '+' : '', delta: delta}) };
  }
  if (!confOk) {
    return { ok: false, reason: tr('tab3.confianza_inferior_al_umbral_85_revisa', {confpct: confPct}) };
  }
  return { ok: true, reason: '' };
}

async function _cmv40AutoMarkSynced(pid) {
  await _cmv40PostFase(`/api/cmv40/${pid}/mark-synced`);
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
      showToast(tr('tab3.ya_existe_un_mkv_con_el', {name: name}), 'warning');
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
    showToast(tr('tab3.auto_pipeline_activado_el_backend_encadenara'), 'success');
  } else {
    showToast(tr('tab3.auto_pipeline_desactivado_tendras_que_lanzar'), 'info');
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
    list.innerHTML = '<div class="cmv40-repo-empty">' + tr('tab3.sin_mkv_origen_imposible_matchear') + '</div>';
    if (info) info.textContent = '';
    return;
  }
  list.innerHTML = '<div class="cmv40-repo-empty"><span data-icono="reloj"></span> ' + tr('tab3.buscando_en_drive') + '</div>';
  if (info) info.innerHTML = '<span class="cmv40-rec-spinner-inline"></span> ' + tr('tab3.consultando_repositorio_de_dovitools');
  const reqId = (_cmv40PanelRepoReqIds[pid] || 0) + 1;
  _cmv40PanelRepoReqIds[pid] = reqId;
  const qs = '?filename=' + encodeURIComponent(filename);
  const data = await apiFetch('/api/cmv40/repo-rpus' + qs);
  if (_cmv40PanelRepoReqIds[pid] !== reqId) return;
  if (!data) {
    list.innerHTML = '<div class="cmv40-repo-empty">' + tr('tab3.error_consultando_el_repositorio') + '</div>';
    if (info) info.textContent = '';
    return;
  }
  if (!data.drive_configured) {
    list.innerHTML = '<div class="cmv40-repo-empty">' + tr('tab3.repositorio_dovitools_no_configurado') + '</div>';
    if (info) info.textContent = '';
    return;
  }
  const cands = data.candidates || [];
  if (!cands.length) {
    const t = data.title_en || data.title_es || filename;
    list.innerHTML = `<div class="cmv40-repo-empty">${tr('tab3.sin_coincidencias_para_t_prueba_otra', {t: escHtml(t)})}</div>`;
    if (info) info.textContent = '';
    return;
  }
  project._panelRepoCands = cands;
  const topId = cands[0]?.file?.id || '';
  const renderCard = (c) => {
    const sizeMb = (c.file.size_bytes / 1024 / 1024).toFixed(1);
    const pt = c.predicted_type || 'unknown';
    const prov = c.provenance || '';
    const tagMeta = pt === 'trusted_p7_fel_final' ? { icon: 'diana', label: 'bin P7 FEL',  cls: 'tag-ok' }
                  : pt === 'trusted_p7_mel_final' ? { icon: 'diana', label: 'bin P7 MEL',  cls: 'tag-ok' }
                  : pt === 'trusted_p8_source'    ? { icon: 'caja', label: 'bin P8 retail', cls: 'tag-info' }
                  : { icon: 'info', label: tr('tab3.tipo_desconocido_min'), cls: 'tag-warn' };
    const provTag = prov === 'retail'
      ? '<span class="cmv40-repo-card-tag tag-ok"><span data-icono="biblioteca"></span> Retail</span>'
      : prov === 'generated'
      ? '<span class="cmv40-repo-card-tag tag-warn"><span data-icono="aviso"></span> Generated</span>'
      : '';
    const isBest = c.file.id === topId;
    return `
      <div class="cmv40-repo-card" data-file-id="${escHtml(c.file.id)}"
           role="button" tabindex="0"
           onclick="_cmv40SelectRepoForPanel('${escHtml(pid)}','${escHtml(c.file.id)}')"
           onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();_cmv40SelectRepoForPanel('${escHtml(pid)}','${escHtml(c.file.id)}')}">
        <div class="cmv40-repo-card-head">
          <span class="cmv40-repo-card-tag ${tagMeta.cls}">${icono(tagMeta.icon)} ${tagMeta.label}</span>
          ${provTag}
          ${isBest ? '<span class="cmv40-repo-card-best"><span data-icono="diana"></span> <span data-i18n="comun.mejor_match"></span></span>' : ''}
          <span class="cmv40-repo-card-score">${Math.round(c.score * 100)}%</span>
          <span class="cmv40-repo-card-size">${sizeMb} MB</span>
        </div>
        <div class="cmv40-repo-card-path">${escHtml(c.file.path)}</div>
      </div>`;
  };
  list.innerHTML = cands.map(renderCard).join('');
  if (info) {
    info.innerHTML = tr('tab3.n_candidatos_top_score', {n: `<strong>${cands.length}</strong>`, p2: cands.length !== 1 ? 's' : '', score: `<strong>${Math.round(cands[0].score * 100)}%</strong>`});
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
    showToast(tr('tab3.selecciona_un_candidato_del_repositorio'), 'warning');
    return;
  }
  await apiFetch(`/api/cmv40/${pid}/target-rpu-from-drive`, {
    method: 'POST',
    body: JSON.stringify({ file_id: sel.file_id, file_name: sel.file_name || '' }),
  });
  _cmv40PhaseToast(pid, tr('tab3.descargando_rpu_del_repositorio'));
  _cmv40PollPhase(pid, 'target_provided');
}

async function _cmv40LoadRpus(pid) {
  const select = document.getElementById(`cmv40-rpu-select-${pid}`);
  const data = await apiFetch('/api/cmv40/rpu-files');
  select.innerHTML = '<option value="">' + tr('tab3.opt_seleccionar_rpu') + '</option>';
  if (data?.files?.length) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = `${f.name} (${_fmtBytes(f.size_bytes)})`;
      select.appendChild(opt);
    });
  } else {
    select.innerHTML = '<option value="">' + tr('tab3.no_hay_rpus_en_mnt_cmv40_rpus') + '</option>';
  }
}

async function _cmv40LoadTargetMkvs(pid) {
  const select = document.getElementById(`cmv40-target-mkv-select-${pid}`);
  const data = await apiFetch('/api/mkv/files-in-isos');
  select.innerHTML = '<option value="">' + tr('tab3.seleccionar_mkv_con_cmv40') + '</option>';
  if (data?.files && data.files.length) {
    data.files.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.path;
      opt.textContent = f.name;
      select.appendChild(opt);
    });
  } else {
    select.innerHTML = '<option value="">' + tr('tab3.no_hay_mkvs_en_el_directorio_de_isos') + '</option>';
  }
}

async function cmv40DoTargetFromPath(pid) {
  const select = document.getElementById(`cmv40-rpu-select-${pid}`);
  const rpuPath = select.value;
  if (!rpuPath) {
    showToast(tr('tab3.selecciona_un_rpu'), 'warning');
    return;
  }
  const data = await apiFetch(`/api/cmv40/${pid}/target-rpu-path`, {
    method: 'POST',
    body: JSON.stringify({ rpu_path: rpuPath }),
  });
  if (data) {
    showToast(tr('tab3.rpu_target_cargado'), 'success');
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
    showToast(tr('tab3.selecciona_un_mkv'), 'warning');
    return;
  }
  await apiFetch(`/api/cmv40/${pid}/target-rpu-from-mkv`, {
    method: 'POST',
    body: JSON.stringify({ source_mkv_path: mkvPath }),
  });
  _cmv40PhaseToast(pid, tr('tab3.extrayendo_rpu_del_mkv'));
  _cmv40PollPhase(pid, 'target_provided');
}

async function cmv40DoExtract(pid) {
  await _cmv40PostFase(`/api/cmv40/${pid}/extract`);
  _cmv40PhaseToast(pid, tr('tab3.extrayendo_bl_el_y_datos_per'));
  _cmv40PollPhase(pid, 'extracted');
}

async function cmv40DoInject(pid) {
  showConfirm(
    tr('tab3.inyectar_rpu_2'),
    tr('tab3.esto_creara_el_injected_hevc_has'),
    async () => {
      await _cmv40PostFase(`/api/cmv40/${pid}/inject`);
      _cmv40PhaseToast(pid, tr('tab3.inyectando_rpu'));
      _cmv40PollPhase(pid, 'injected');
    },
    tr('tab3.paso_inyectar'),
  );
}

async function cmv40DoRemux(pid) {
  await _cmv40PostFase(`/api/cmv40/${pid}/remux`);
  _cmv40PhaseToast(pid, tr('tab3.toast_remuxando_a_mkv_final'));
  _cmv40PollPhase(pid, 'remuxed');
}

async function cmv40DoValidate(pid) {
  await _cmv40PostFase(`/api/cmv40/${pid}/validate`);
  _cmv40PhaseToast(pid, tr('tab3.toast_validando_mkv_final'));
  // Polling — Fase H dura varios minutos (move 42 GB), no se puede hacer síncrono
  _cmv40PollPhase(pid, 'done');
}

async function cmv40Cleanup(pid) {
  const bodyHtml = `
    <div style="line-height:1.6">
      <p style="margin:0 0 10px 0"><b data-i18n="tab3.que_se_borrara"></b></p>
      <ul style="margin:0 0 12px 18px; padding:0; font-family:'SF Mono',monospace; font-size:11px">
        <li>source.hevc, BL.hevc, EL.hevc</li>
        <li>RPU_source.bin, RPU_target.bin, RPU_synced.bin</li>
        <li>EL_injected.hevc</li>
        <li>per_frame_data.json, editor_config.json</li>
      </ul>
      <p style="margin:0 0 10px 0"><b data-i18n="tab3.que_se_preserva"></b></p>
      <ul style="margin:0 0 12px 18px; padding:0; font-size:12px">
        <li><span data-i18n="tab3.el_mkv_final_en"></span> <code>/mnt/output</code></li>
        <li data-i18n="tab3.los_metadatos_del_proyecto_log_sync"></li>
      </ul>
      <div class="banner warning" style="margin-top:12px">
        <span class="banner-icon"><span data-icono="aviso"></span></span>
        <span><span data-i18n-html="tab3.esta_accion_archiva_el_proyecto_y_no_podras"></span></span>
      </div>
    </div>`;

  document.getElementById('cmv40-confirm-title').textContent = tr('tab3.limpiar_artefactos');
  document.getElementById('cmv40-confirm-sub').textContent = tr('tab3.esta_accion_libera_espacio_en_disco');
  document.getElementById('cmv40-confirm-body').innerHTML = bodyHtml;

  const btn = document.getElementById('cmv40-confirm-btn');
  btn.textContent = tr('tab3.limpiar_y_archivar');
  btn.className = 'btn btn-danger btn-sm';
  const newBtn = btn.cloneNode(true);
  btn.parentNode.replaceChild(newBtn, btn);
  newBtn.addEventListener('click', async () => {
    closeModal('cmv40-confirm-modal');
    const data = await apiFetch(`/api/cmv40/${pid}/cleanup`,
                                { method: 'POST' }, API_FETCH_TIMEOUT_LARGO);
    if (data) {
      showToast(tr('tab3.liberado_p1_proyecto_archivado', {p1: _fmtBytes(data.freed_bytes)}), 'success');
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
      // Un pre-flight en curso se reanuda en SU modal, no en el de trabajo:
      // es lo que decide si va a haber trabajo, y su veredicto pide una
      // respuesta del usuario en dos de los tres desenlaces.
      if (running.running_phase === 'preflight') abrirPreflightCMv40(running.id);
      const niceName = running.source_mkv_name || running.id;
      const phaseLabel = (typeof CMV40_RUNNING_LABELS === 'object' && CMV40_RUNNING_LABELS)
        ? (CMV40_RUNNING_LABELS[running.running_phase] || running.running_phase)
        : running.running_phase;
      showToast(`Reanudando seguimiento: ${niceName} · ${phaseLabel}`, 'info');
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
      <div class="empty-state-icon" data-icono="caja"></div>
      <div data-i18n="tab3.no_se_ha_podido_cargar_la"></div>
      <div class="empty-state-desc" style="margin-top:6px" data-i18n="tab3.tus_proyectos_siguen_guardados_reintentando_cada"></div>
      <button class="btn btn-ghost btn-xs" style="margin-top:10px"
        onclick="refreshCMv40Sidebar()"><span data-icono="refrescar"></span> <span data-i18n="tab3.reintentar_ahora"></span></button>
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
        <div class="empty-state-icon" data-icono="curva"></div>
        <div>${tr(searchTerm || _cmv40Filter !== 'all' ? 'tab3.sin_resultados' : 'tab3.crea_un_proyecto_para_inyectar_cmv40')}</div>
      </div>`;
    return;
  }

  filtered.forEach(s => {
    const isRunning  = !!s.running_phase;
    // La fase REAL, también en un proyecto archivado: que está cerrado ya lo
    // dice su chip de estado, y repetirlo en el subtítulo costaba el único
    // dato que la fila tenía —en qué punto se quedó— en las seis de cada
    // siete tarjetas que están archivadas.
    const phaseLabel = CMV40_PHASE_LABELS[s.phase] || s.phase;
    const runningLabel = isRunning
      ? (CMV40_RUNNING_LABELS[s.running_phase] || s.running_phase)
      : null;
    const isOpen = openCMv40Projects.find(p => p.id === s.id);
    const isSelected = _cmv40SelectedSidebarId === s.id;
    const { titulo, tags } = nombreYTags(s.source_mkv_name);
    const name = s.source_mkv_name.replace(/\.mkv$/i, '');

    const modFull = new Date(s.updated_at || s.created_at).toLocaleString(localeActual(), {
      day: '2-digit', month: '2-digit', year: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });

    // El estado, en el orden en que manda: lo que corre ahora, un error sin
    // resolver, el proyecto cerrado, el terminado y el que está a medias.
    const estado = isRunning ? 'corriendo'
      : s.error_message ? 'error'
      : s.archived ? 'archivado'
      : (s.phase === 'done' || s.phase === 'validated') ? 'hecho'
      : s.phase === 'created' ? 'listo' : 'en_cola';
    const acento = isRunning ? 'estado-curso'
      : s.error_message ? 'estado-error'
      : s.archived ? ''
      : (s.phase === 'done' || s.phase === 'validated') ? 'estado-hecho' : '';

    // Qué clase de trabajo es. Es la pregunta que separa un proyecto de
    // treinta segundos de uno de hora y media, y no se veía en ninguna parte
    // de la lista: había que abrirlo para saberlo.
    const RUTA = { restore_dropin: ['Drop-in', 'verde'],
                   restore_merge:  ['Merge', 'azul'],
                   keep_cmv29:     [tr('tab3.se_mantiene'), 'naranja'] };
    const ruta = RUTA[s.output_workflow];
    const TIER = { full: 'CMv4 FULL', core_rich: 'CMv4 CORE+', core: 'CMv4 CORE' };
    const chips = [];
    if (ruta) chips.push({ txt: ruta[0], tono: ruta[1],
                           tooltip: tr('tab3.como_se_resolvio_el_upgrade') });
    if (TIER[s.target_l8_quality_tier]) {
      chips.push({ txt: TIER[s.target_l8_quality_tier], tono: 'morado',
                   tooltip: tr('tab3.riqueza_del_l8_del_rpu_target') });
    }
    tags.filter(t => !/^CMv4/i.test(t))
        .forEach(t => chips.push({ txt: t, tono: 'teal' }));

    const card = document.createElement('div');
    card.className = `session-card${isSelected ? ' selected' : ''}`
                   + (acento ? ' ' + acento : '');
    card.dataset.sid = s.id;
    // Los puntitos solo en lo que está a medias: en un proyecto terminado
    // están todos llenos y no contestan nada, y sobre una tarjeta sin acento
    // —un archivado— salen gris sobre gris. Es un accesorio de más.
    const terminado = s.archived || s.phase === 'done' || s.phase === 'validated';
    const idx = terminado ? -1 : CMV40_PHASES_ORDER.indexOf(s.phase);
    card.innerHTML = tarjetaDeProyecto({
      titulo,
      tituloTooltip: name,
      // Cuando algo corre, lo que interesa es QUÉ corre, no la última fase
      // que terminó.
      sub: isRunning ? runningLabel : phaseLabel,
      chips,
      estado,
      estadoTooltip: isRunning ? runningLabel : phaseLabel,
      poster: (s.tmdb_info || {}).poster_url || '',
      icono: typeof iconoDeTrabajo === 'function'
        ? iconoDeTrabajo('fase_cmv40', 'cmv40') : '',
      meta: formatRelativeDate(s.updated_at || s.created_at),
      metaIso: s.updated_at || s.created_at || '',
      metaTooltip: tr('tab3.modificado') + ' ' + modFull,
      // Diez puntos, uno por fase del pipeline. «Fase: BL/EL extraídos» no
      // dice si eso es el principio o el final; esto sí, y sin una línea.
      pips: idx >= 0 ? { hechas: idx + 1, total: CMV40_PHASES_ORDER.length,
                         tooltip: `${idx + 1} de ${CMV40_PHASES_ORDER.length} · ${phaseLabel}` }
                     : null,
      insignia: typeof insigniaDeTrabajo === 'function' ? insigniaDeTrabajo(s.id) : '',
      abierto: !!isOpen,
      acciones: `
        <button class="btn btn-primary btn-sm" onclick="event.stopPropagation();_cmv40OpenSelected('${s.id}')" data-i18n="tab1.abrir" data-i18n-tip="tab3.abrir_este_proyecto"></button>
        <button class="btn btn-danger btn-sm" onclick="event.stopPropagation();_cmv40DeleteFromSidebar('${s.id}')" data-i18n="tab1.eliminar" data-i18n-tip="tab3.eliminar_permanentemente"></button>`,
    });
    const row = card.querySelector('.session-card-row');
    row.onclick = () => _cmv40ToggleSidebarSelection(s.id);
    row.ondblclick = () => _cmv40OpenSelected(s.id);
    list.appendChild(card);
  });
}

function _cmv40ToggleSortDir() {
  _cmv40SortDir = _cmv40SortDir === 'asc' ? 'desc' : 'asc';
  const btn = document.getElementById('cmv40-sort-dir');
  if (btn) btn.innerHTML = icono(_cmv40SortDir === 'asc' ? 'flechaArriba' : 'flechaAbajo');
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
    tr('tab3.eliminar_proyecto'),
    tr('tab3.se_eliminara_y_sus_artefactos_intermedios', {source_mkv_name: s.source_mkv_name}),
    async () => {
      await apiFetch(`/api/cmv40/${sid}?clean_artifacts=true`,
                     { method: 'DELETE' }, API_FETCH_TIMEOUT_LARGO);
      // Cerrar subtab si estaba abierto
      const open = openCMv40Projects.find(p => p.id === sid);
      if (open) closeCMv40Project(sid);
      if (_cmv40SelectedSidebarId === sid) _cmv40SelectedSidebarId = null;
      refreshCMv40Sidebar();
    },
    tr('tab1.eliminar'),
  );
}

// ── Chart interactivo de sincronización (Fase D) ─────────────────

/** «Quitados 72 frames · duplicados 0 · en 1 paso», y el JSON debajo.
 *
 *  La card enseñaba `JSON.stringify(sync_config, null, 2)` en un `<pre>`, que
 *  es el dato de diagnóstico y no lo que alguien viene a leer. Se resume, y
 *  el JSON se queda plegado para quien lo necesite — como 🔬 Datos ISO.
 */
function _cmv40ResumenDeCorreccion(cfg) {
  if (!cfg) return '';
  const pasos = Array.isArray(cfg.steps) ? cfg.steps.length
              : (Object.keys(cfg).length ? 1 : 0);
  return tr('tab3.correccion_resumen', {
    quitados: _cmv40Num(cfg.total_removed || 0),
    duplicados: _cmv40Num(cfg.total_duplicated || 0),
    pasos: pasos === 1 ? tr('tab3.correccion_un_paso')
                       : tr('tab3.correccion_n_pasos', {n: pasos}),
  });
}

/** Un canvas sin datos se ve NEGRO y no dice nada.
 *
 *  `sync-data` lee `per_frame_data.json` del workdir, y ese volcado no
 *  siempre está: la ruta drop-in no lo crea (`per_frame_data_skipped`) y la
 *  limpieza de artefactos se lo lleva. El cargador hacía `if (!data) return`,
 *  así que al abrir la card de Fase D de un proyecto ya pasado quedaba un
 *  rectángulo negro con un «el gráfico se muestra en modo solo lectura» al
 *  lado — y no se podía ver ni navegar nada. Reportado el 2026-09-23.
 *
 *  Es la misma regla que el resto del proyecto: antes que un hueco con
 *  pinta de dato, decir qué pasa.
 */
function _cmv40ChartSinDatos(pid, est) {
  const wrap = document.getElementById(`cmv40-chart-wrap-${pid}`);
  if (!wrap) return;
  const motivo = (est && est.status === 404) ? tr('tab3.sync_sin_volcado')
    // 409: el volcado no está y hay una fase corriendo, así que el backend
    // se niega a regenerarlo. No es un fallo, es un «ahora no».
    : (est && est.status === 409) ? tr('tab3.sync_fase_en_marcha')
    : tr('tab3.sync_no_se_pudo_leer');
  wrap.innerHTML = `<div class="banner info"><span class="banner-icon">`
    + icono('info') + `</span><span>${escHtml(motivo)}</span></div>`;
}

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
      const est = {};
      const data = await apiFetch(`/api/cmv40/${pid}/sync-data`,
                                  { silent: true, estado: est });
      if (!data) { _cmv40ChartSinDatos(pid, est); return; }
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
      <div><span class="sync-label"><span data-i18n="tab3.frames_origen"></span></span> <b>${srcFrames.toLocaleString(localeActual())}</b></div>
      <div><span class="sync-label"><span data-i18n="tab3.frames_target"></span></span> <b>${tgtFrames.toLocaleString(localeActual())}</b></div>
      <div><span class="sync-label"><span data-i18n="tab3.diferencia"></span></span> <b style="color:${delta===0?'var(--green)':'var(--orange)'}">${delta > 0 ? '+' : ''}${delta}</b></div>
    </div>
    ${suggested.offset !== undefined && suggested.offset !== 0 ? `
      <div class="banner info" style="margin-top:10px">
        <span class="banner-icon"><span data-icono="lupa"></span></span>
        <span><span data-i18n="tab3.offset_detectado_automaticamente"></span> <b>${suggested.offset > 0 ? '+' : ''}${suggested.offset} frames</b></span>
      </div>` : ''}
    ${_cmv40SheetSyncBannerHTML(d.sheet_sync)}
  `;
}

/**
 * Qué dice la hoja de DoviTools frente a lo que acabamos de medir.
 *
 * El offset de la hoja NO es un desfase pendiente: es el que la comunidad
 * detectó y **ya corrigió** dentro del bin. Así que lo esperable es medir
 * CERO, y detectar justo el valor documentado sería la señal mala. Antes esto
 * estaba al revés y marcaba "algo no cuadra" sobre bins correctos — los 16
 * proyectos del corpus con offset documentado caían ahí.
 */
function _cmv40SheetSyncBannerHTML(sheetSync) {
  if (!sheetSync || sheetSync.sheet_offset === null
      || sheetSync.sheet_offset === undefined) return '';
  const sheetVal = sheetSync.sheet_offset;
  const sign = v => (v > 0 ? '+' : '') + v;
  const sheetTxt = sheetSync.sheet_offset_text
    ? `<b>${escHtml(sheetSync.sheet_offset_text)}</b>`
    : `<b>${tr('tab3.sheetval_frames', {sheetval: sign(sheetVal)})}</b>`;
  // El espacio va FUERA del `tr()`: dentro se lo come la normalización.
  const src = sheetSync.match_title
    ? ' ' + tr('tab3.fila_p1', {p1: escHtml(sheetSync.match_title)}) : '';
  const det = sheetSync.detected_offset;

  if (sheetSync.corregido === true) {
    if (!sheetVal) {
      return `<div class="banner success" style="margin-top:8px">
        <span class="banner-icon"><span data-icono="check"></span></span>
        <span>${tr('tab3.la_hoja_src_no_documenta_ningun', {src: src})}</span>
      </div>`;
    }
    return `<div class="banner success" style="margin-top:8px">
      <span class="banner-icon"><span data-icono="check"></span></span>
      <span>${tr('tab3.la_hoja_src_documenta_que_la', {src: src})} <b><span data-i18n="tab3.ya_corrigio"></span></b>
        ${tr('tab3.sheettxt_en_este_bin_y_aqui', {sheettxt: sheetTxt, det: sign(det)})}</span>
    </div>`;
  }
  if (sheetSync.parece_sin_corregir) {
    return `<div class="banner warning" style="margin-top:8px">
      <span class="banner-icon"><span data-icono="aviso"></span></span>
      <span>${tr('tab3.la_hoja_src_dice_que_la', {src: src, sheettxt: sheetTxt})} ${tr('tab3.det_la_misma_magnitud_no_es_el_corregido', {det: tr('tab3.det_frames', {det: sign(det)})})}</span>
    </div>`;
  }
  if (sheetSync.corregido === false) {
    // La ternaria fuera de la plantilla: una plantilla DENTRO de un `${…}`
    // no se puede delimitar con un regex, así que el extractor no podía
    // tocar este mensaje y se quedaba en castellano.
    const detTxt = `<b>${tr('tab3.det_frames', {det: sign(det)})}</b>`;
    const ahi = sheetVal
      ? tr('tab3.sheettxt_ya_venia_corregido', {sheettxt: sheetTxt})
      : tr('tab3.no_consta_ningun_desfase');
    return `<div class="banner warning" style="margin-top:8px">
      <span class="banner-icon"><span data-icono="aviso"></span></span>
      <span>${tr('tab3.se_detecta_un_desfase_que_la_hoja_no_explica',
                 {det: detTxt, src: src, ahi: ahi})}</span>
    </div>`;
  }
  return `<div class="banner info" style="margin-top:8px">
    <span class="banner-icon"><span data-icono="info"></span></span>
    <span>${tr('tab3.la_hoja_src_documenta_sheettxt_como', {src: src, sheettxt: sheetTxt})}</span>
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
    'excellent': tr('tab3.excelente'),
    'good':      tr('tab3.buena'),
    'moderate':  tr('tab3.moderada'),
    'poor':      tr('tab3.baja'),
    'insufficient_data': tr('tab3.datos_insuficientes'),
    'no_variance':       tr('tab3.sin_variacion'),
  }[rating];
  container.innerHTML = `
    <div class="cmv40-confidence-panel" style="border-color:${ratingColor}; margin-top:16px">
      <div class="cmv40-confidence-header">
        <span class="cmv40-confidence-label" data-i18n="tab3.confianza_de_sincronizacion"></span>
        <span class="cmv40-confidence-value" style="color:${ratingColor}">${pct}%</span>
        <span class="cmv40-confidence-rating" style="color:${ratingColor}">${ratingLabel}</span>
      </div>
      <div class="cmv40-confidence-bar">
        <div class="cmv40-confidence-fill" style="width:${pct}%; background:${ratingColor}"></div>
        <div class="cmv40-confidence-threshold" style="left:85%" data-i18n-tip="tab3.umbral_minimo_85">·</div>
      </div>
      <div class="cmv40-confidence-reason">${escHtml(conf.reason || '')}</div>
      <div style="font-size:10px; color:var(--text-3); margin-top:4px" data-i18n="tab3.mide_la_correlacion_de_forma_entre"></div>
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
  // **`>=`, no `>`.** `sync_verified` es lo que escribe `mark-synced`, o sea
  // exactamente «el usuario ya confirmó el sync»: con el `>` la fase seguía
  // contando como editable y el formulario de corrección —los dos campos de
  // frames, «Aplicar corrección», «Volver al original» y «Confirmar»— se
  // quedaba vivo mientras corría la Fase F. Pulsar cualquiera de ellos a esas
  // alturas no arregla nada: el RPU ya está inyectándose. Reportado el
  // 2026-09-23. Dentro de la Fase D la sesión está en `extracted`, así que la
  // edición sigue donde tiene que estar.
  const readOnly  = phaseIdx >= dDoneIdx;
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
  if (!project.chartRange) project.chartRange = _cmv40RangoPorDefecto(totalFrames);
  const currentRange = project.chartRange;

  // El preset activo es el que coincide en ANCHO, no en posición: desde que
  // los presets centran en la vista actual, «30 s» rara vez empieza en 0 y
  // comparar los dos extremos dejaba la fila sin ninguno marcado.
  const span = currentRange.end - currentRange.start;
  const presets = [
    { key: '1s',    seg: 1,       label: '1 s' },
    { key: '5s',    seg: 5,       label: '5 s' },
    { key: '30s',   seg: 30,      label: '30 s' },
    { key: '1min',  seg: 60,      label: '1 min' },
    { key: '5min',  seg: 5 * 60,  label: '5 min' },
    { key: '30min', seg: 30 * 60, label: '30 min' },
    { key: 'all',   seg: null,    label: tr('tab3.zoom_todo') },
  ];
  const activeKey = span >= totalFrames ? 'all'
    : presets.find(p => p.seg !== null
                        && Math.abs(Math.round(p.seg * FPS) - span) <= 1)?.key;

  const presetBtns = presets.map(p => `
    <button class="btn btn-ghost btn-xs cmv40-zoom-preset${activeKey === p.key ? ' active' : ''}"
      onclick="_cmv40ZoomPreset('${pid}', ${p.seg === null ? 'null' : p.seg})">${p.label}</button>
  `).join('');

  const zoomRowHtml = `
    <div class="cmv40-zoom-row">
      <span class="section-subtitle"><span data-i18n="tab3.zoom"></span></span>
      ${presetBtns}
      <button class="btn btn-ghost btn-xs cmv40-zoom-preset" onclick="_cmv40ZoomFuera('${pid}')"
        data-i18n-tip="tab3.zoom_alejar_tip"><span data-icono="lupaMenos"></span></button>
      <span class="cmv40-range-inputs">
        <label data-i18n="tab3.desde"><input type="text" inputmode="numeric" id="cmv40-range-start-${pid}"
            value="${_cmv40FrameATiempo(currentRange.start, FPS)}" size="7"
            onchange="_cmv40AplicarRangoDeTiempos('${pid}')">
        </label>
        <label data-i18n="tab3.hasta"><input type="text" inputmode="numeric" id="cmv40-range-end-${pid}"
            value="${_cmv40FrameATiempo(currentRange.end, FPS)}" size="7"
            onchange="_cmv40AplicarRangoDeTiempos('${pid}')">
        </label>
      </span>
      <span class="cmv40-zoom-pista" data-i18n="tab3.zoom_arrastra_para_encuadrar"></span>
    </div>`;

  // Read-only: solo zoom/rango, sin form de corrección.
  if (readOnly) {
    const soloLectura = `
      ${zoomRowHtml}
      <div style="margin-top:10px; padding:8px 12px; background:var(--surface-2); border-radius:6px; font-size:11px; color:var(--text-3)">
        ${hasSyncConfig
          ? tr('tab3.correccion_aplicada_en_su_dia')
          : tr('tab3.sincronizacion_confirmada_sin_correccion')}
      </div>`;
    if (soloLectura !== project._syncControlesHTML) {
      container.innerHTML = soloLectura;
      project._syncControlesHTML = soloLectura;
    }
    return;
  }

  const htmlControles = `
    ${zoomRowHtml}

    <div class="section-subtitle" style="margin-top:16px; margin-bottom:4px">${tr(hasSyncConfig ? 'tab3.correccion_adicional' : 'tab3.correccion_manual')}</div>
    <div style="font-size:11px; color:var(--text-3); margin-bottom:8px">
      ${hasSyncConfig
        ? tr('tab3.estos_valores_se_sumaran_a_la')
        : tr('tab3.los_valores_se_aplican_desde_el')}
    </div>
    ${delta === 0 ? '' : `<div class="cmv40-sync-aviso">
      <span data-icono="aviso"></span>
      <span>${escHtml(delta > 0
        ? tr('tab3.sync_sobran_frames', {n: delta})
        : tr('tab3.sync_faltan_frames', {n: Math.abs(delta)}))}</span>
    </div>`}
    <table class="cmv40-sync-matriz">
      <thead><tr><th></th>
        <th data-i18n="tab3.sync_al_inicio"></th>
        <th data-i18n="tab3.sync_al_final"></th></tr></thead>
      <tbody>
        <tr>
          <th data-i18n="tab3.sync_quitar"></th>
          ${['remove', 'remove-fin'].map(k => `<td><input type="number"
             id="cmv40-${k}-${pid}" value="0" min="0"
             oninput="marcarTocado(this); _cmv40UpdateExpectedDelta('${pid}', ${delta})"></td>`).join('')}
        </tr>
        <tr>
          <th data-i18n="tab3.sync_duplicar"></th>
          ${['duplicate', 'duplicate-fin'].map(k => `<td><input type="number"
             id="cmv40-${k}-${pid}" value="0" min="0"
             oninput="marcarTocado(this); _cmv40UpdateExpectedDelta('${pid}', ${delta})"></td>`).join('')}
        </tr>
      </tbody>
    </table>
    <div style="margin-top:10px; padding:10px 12px; background:var(--surface-2); border-radius:6px; font-size:12px">
      <span style="color:var(--text-3)" data-i18n="tab3.delta_despues_de_aplicar"></span>
      <b id="cmv40-expected-delta-${pid}" style="margin-left:6px">—</b>
    </div>
    <div style="display:flex; gap:10px; margin-top:16px; flex-wrap:wrap">
      <button class="btn btn-ghost btn-md" onclick="cmv40DoApplySync('${pid}')"><span data-icono="lapiz"></span> <span data-i18n="tab3.aplicar_correccion"></span></button>
      ${hasSyncConfig ? `<button class="btn btn-danger btn-md" onclick="cmv40DoResetSync('${pid}')"
          data-i18n-tip="tab3.descartar_correccion_y_volver_al_target"><span data-icono="deshacer"></span> <span data-i18n="tab3.resetear_al_original"></span></button>` : ''}
      <button class="btn btn-primary btn-md" onclick="cmv40DoSkipSync('${pid}')"
        ${canConfirm ? '' : 'disabled data-tooltip="' + confirmReason + '"'}><span data-icono="check"></span> <span data-i18n="tab3.confirmar_sync_y_continuar"></span></button>
    </div>
    <div style="margin-top:8px; font-size:11px; color:var(--text-3)">
      <span data-i18n="tab3.actual"></span> <b style="color:${delta===0?'var(--green)':'var(--orange)'}">${tr('tab3.p1_delta_frames', {p1: delta > 0 ? '+' : '', delta: delta})}</b>
      <span data-i18n="tab3.confianza"></span> <b style="color:${confOk ? 'var(--green)' : 'var(--orange)'}">${confPct}%</b>
      ${canConfirm ? ' — <b style="color:var(--green)">' + tr('tab3.listo_para_continuar') + '</b>' : ' — <b style="color:var(--orange)">' + confirmReason + '</b>'}
    </div>
  `;
  // **Un repintado no puede borrar lo que estás escribiendo.**
  //
  // Esto se repinta con cada vuelta del poll —el gráfico se recarga, el Δ y
  // la confianza pueden cambiar— y reemplazar el `innerHTML` devolvía las
  // cuatro casillas de la corrección a cero a los dos segundos de teclear.
  // Reportado el 2026-09-23.
  //
  // Dos medidas, y hacen falta las dos: no repintar cuando el HTML es el
  // mismo —que es el caso normal y ahorra el parpadeo— y, cuando sí cambia,
  // devolver lo tecleado con su foco y su cursor. Es exactamente lo que ya
  // se hace con el scroll del log y con los `<details>` del panel.
  if (htmlControles !== project._syncControlesHTML) {
    const escrito = anclajeDeFormulario(container);
    container.innerHTML = htmlControles;
    project._syncControlesHTML = htmlControles;
    restaurarAnclajeDeFormulario(container, escrito);
  }
  // El Δ esperado se recalcula SIEMPRE, repinte o no: si no, tras restaurar
  // lo tecleado el resumen se quedaría con el número de la vuelta anterior.
  _cmv40UpdateExpectedDelta(pid, delta);
}

/** Lo que el usuario ha escrito en las cuatro casillas de la matriz. */
function _cmv40OpsDeSync(pid) {
  const n = (k) => parseInt(
    document.getElementById(`cmv40-${k}-${pid}`)?.value) || 0;
  return {
    quitarInicio:   n('remove'),
    quitarFinal:    n('remove-fin'),
    duplicarInicio: n('duplicate'),
    duplicarFinal:  n('duplicate-fin'),
  };
}

function _cmv40UpdateExpectedDelta(pid, currentDelta) {
  const o = _cmv40OpsDeSync(pid);
  const r = o.quitarInicio + o.quitarFinal;
  const d = o.duplicarInicio + o.duplicarFinal;
  // Aplicar remove reduce delta; duplicate lo aumenta. El SITIO no cambia la
  // cuenta —quitar un frame es uno menos, esté donde esté— pero sí el
  // resultado: por eso lo elige el usuario y no se infiere.
  const expected = currentDelta - r + d;
  const el = document.getElementById(`cmv40-expected-delta-${pid}`);
  if (!el) return;
  const sign = expected > 0 ? '+' : '';
  const color = expected === 0 ? 'var(--green)' : 'var(--orange)';
  el.innerHTML = `<span style="color:${color}">${tr('tab3.sign_expected_frames', {sign: sign, expected: expected})}</span>`;
}

/** Cambia el rango visible del chart y pide esa ventana al servidor.
 *
 *  Antes el frontend se traía la película entera —24 MB para un UHD— y
 *  filtraba en cliente. Ahora el backend reduce a cubos (min y max por cubo,
 *  no la media: un promedio se come los picos que delatan el desfase) y sirve
 *  la ventana pedida, así que un zoom fino recibe el dato EXACTO en vez de
 *  filtrar una muestra gruesa. */
// ── El zoom del gráfico de sincronización ───────────────────────────────────
//
// Era una fila de presets que siempre encuadraban **desde el frame 0**: para
// mirar un corte del minuto 48 había que escribir los dos números de frame a
// mano y volver a escribirlos en cuanto querías afinar. Desde el 2026-09-23:
//
//  · se **arrastra sobre el gráfico** para encuadrar ese tramo, y se puede
//    volver a arrastrar dentro — sub-selecciones sucesivas hasta el segundo;
//  · los presets **centran en el punto medio de la vista actual**, no en el
//    principio de la película: pedir 30 min desde el minuto 48 enseña del 33
//    al 63, que es lo que uno quiere al alejarse para ver dónde estaba;
//  · el rango se escribe en **tiempo** (`h:mm:ss`), que es como se mira una
//    película, y no en número de frame;
//  · el suelo es **un segundo**: por debajo el gráfico son dos docenas de
//    frames y ya no hay forma de leer una curva.

/** Zoom máximo: por debajo de un segundo no queda curva que mirar. */
const CMV40_ZOOM_MIN_SEG = 1;

/** `h:mm:ss` de un frame. Segundos porque es la unidad del zoom fino. */
function _cmv40FrameATiempo(frame, fps) {
  const t = Math.max(0, Math.round((frame || 0) / (fps || 23.976)));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60).toString().padStart(2, '0');
  const sg = Math.floor(t % 60).toString().padStart(2, '0');
  return `${h}:${m}:${sg}`;
}

/** `1:02:03` · `2:03` · `45` → frame. `null` si no se entiende. */
function _cmv40TiempoAFrame(txt, fps) {
  const partes = String(txt || '').trim().split(':').map(x => x.trim());
  if (!partes.length || partes.some(x => x === '' || !/^\d+$/.test(x))) return null;
  if (partes.length > 3) return null;
  const n = partes.map(Number);
  while (n.length < 3) n.unshift(0);
  return Math.round((n[0] * 3600 + n[1] * 60 + n[2]) * (fps || 23.976));
}

/** Encaja una ventana dentro de la película, respetando el zoom máximo.
 *
 *  Si el centro pedido deja la ventana fuera por un extremo, se **desplaza**
 *  en vez de recortarse: pedir 30 min centrado en el minuto 2 tiene que
 *  seguir enseñando 30 min, los primeros, y no 17.
 */
function _cmv40Encuadrar(centro, span, total, fps) {
  const minimo = Math.max(2, Math.round(CMV40_ZOOM_MIN_SEG * (fps || 23.976)));
  const ancho = Math.max(minimo, Math.min(Math.round(span), total));
  let start = Math.round(centro - ancho / 2);
  if (start < 0) start = 0;
  if (start + ancho > total) start = Math.max(0, total - ancho);
  return { start, end: Math.min(total, start + ancho) };
}

function _cmv40DatosDelZoom(pid) {
  const project = openCMv40Projects.find(p => p.id === pid);
  if (!project || !project.syncData) return null;
  const d = project.syncData;
  const fps = project.session.source_fps || 23.976;
  const total = d.source_frames || d.target_frames || 0;
  const r = project.chartRange || { start: 0, end: total };
  return { project, fps, total, r };
}

/** Un preset, centrado en lo que se está mirando. `segundos=null` → todo. */
function _cmv40ZoomPreset(pid, segundos) {
  const z = _cmv40DatosDelZoom(pid);
  if (!z) return;
  if (segundos === null) { _cmv40SetRange(pid, 0, z.total); return; }
  const centro = (z.r.start + z.r.end) / 2;
  const { start, end } = _cmv40Encuadrar(centro, segundos * z.fps, z.total, z.fps);
  _cmv40SetRange(pid, start, end);
}

/** Alejarse al doble, sin perder el centro. La vuelta de una sub-selección. */
function _cmv40ZoomFuera(pid) {
  const z = _cmv40DatosDelZoom(pid);
  if (!z) return;
  const centro = (z.r.start + z.r.end) / 2;
  const { start, end } = _cmv40Encuadrar(centro, (z.r.end - z.r.start) * 2,
                                         z.total, z.fps);
  _cmv40SetRange(pid, start, end);
}

/** Los dos campos de tiempo del encuadre manual. */
function _cmv40AplicarRangoDeTiempos(pid) {
  const z = _cmv40DatosDelZoom(pid);
  if (!z) return;
  const a = _cmv40TiempoAFrame(
    document.getElementById(`cmv40-range-start-${pid}`)?.value, z.fps);
  const b = _cmv40TiempoAFrame(
    document.getElementById(`cmv40-range-end-${pid}`)?.value, z.fps);
  if (a === null || b === null) {
    showToast(tr('tab3.zoom_tiempo_no_valido'), 'warning');
    _renderCMv40SyncControls(z.project);   // devuelve los valores buenos
    return;
  }
  if (b <= a) {
    showToast(tr('tab3.el_frame_final_debe_ser_mayor'), 'warning');
    _renderCMv40SyncControls(z.project);
    return;
  }
  const { start, end } = _cmv40Encuadrar((a + b) / 2, b - a, z.total, z.fps);
  _cmv40SetRange(pid, start, end);
}

/** El encuadre con el que se abre el gráfico: **la película entera**.
 *
 *  Eran los primeros 30 s, «la zona típica donde hay logos y desfases». Con
 *  el zoom por selección eso deja de ser el sitio donde hay que empezar: se
 *  abre con todo delante, se ve dónde está la diferencia y se arrastra sobre
 *  ella. Al revés —abrir con un recorte que el usuario no pidió— la primera
 *  pregunta que hay que contestar es «¿y el resto?». Decisión del usuario,
 *  2026-09-23.
 *
 *  De paso, es **el encuadre que la primera petición ya trae**: un
 *  `GET /sync-data` sin rango devuelve la película entera reducida a cubos,
 *  así que abrir en «Todo» no cuesta una segunda vuelta al servidor.
 *
 *  Vive en una función porque lo leían DOS sitios con la constante escrita
 *  en cada uno, que es como se acaba con dos defaults distintos.
 */
function _cmv40RangoPorDefecto(totalFrames) {
  return { start: 0, end: totalFrames };
}

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

async function cmv40DoResetSync(pid) {
  showConfirm(
    tr('tab3.descartar_correccion'),
    tr('tab3.se_borrara_la_correccion_aplicada_y'),
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
        showToast(tr('tab3.correccion_descartada'), 'info');
      }
    },
    tr('tab3.descartar_correccion_2'),
  );
}

/** **La corrección se puede aplicar en los DOS extremos.**
 *
 *  Eran dos casillas —quitar al inicio, duplicar el primer frame— porque el
 *  desfase típico es un logo de estudio que el BD trae y la versión de
 *  streaming no. Pero hay másters donde lo que sobra o falta está al FINAL
 *  (créditos, un fundido más largo), y con las dos casillas de antes la
 *  única forma de cuadrar el frame count era quitando por delante — que
 *  cuadra el número y **desplaza toda la película**. Reportado por el
 *  usuario el 2026-09-23.
 *
 *  `dovi_tool editor` ya lo admitía: `remove` es un rango cualquiera y
 *  `duplicate` lleva su `source`/`offset`. Lo que faltaba era ofrecerlo.
 *
 *  **Y por eso desaparece el auto-relleno.** Antes la casilla venía con el
 *  Δ ya escrito, porque con un solo sitio posible el número lo determinaba
 *  todo. Con dos extremos hay infinitas combinaciones que dan el mismo Δ y
 *  la app **no puede saber cuál es la correcta**: rellenar una por su cuenta
 *  sería adivinar, y adivinar aquí desplaza la película entera. Las cuatro
 *  casillas nacen a cero, el desfase se anuncia arriba y lo reparte quien
 *  está mirando el gráfico.
 */
async function cmv40DoApplySync(pid) {
  const o = _cmv40OpsDeSync(pid);
  const remove = o.quitarInicio + o.quitarFinal;
  const dup = o.duplicarInicio + o.duplicarFinal;
  if (remove === 0 && dup === 0) {
    showToast(tr('tab3.indica_un_valor_para_eliminar_o'), 'warning');
    return;
  }
  // El total del target AHORA, que es sobre lo que el editor cuenta: tras
  // una corrección previa ya no es el de la sesión original.
  const project = openCMv40Projects.find(p => p.id === pid);
  const T = project?.session?.target_frame_count
            || project?.syncData?.target_frames || 0;
  if ((o.quitarFinal || o.duplicarFinal) && !T) {
    showToast(tr('tab3.sync_sin_total_no_hay_final'), 'warning');
    return;
  }
  const config = {};
  const quitar = [];
  if (o.quitarInicio > 0) quitar.push(`0-${o.quitarInicio - 1}`);
  // Por el final se cuenta hacia atrás desde el último frame. Se resta
  // primero lo que se quita por delante para que los dos rangos no se
  // solapen cuando el target es corto.
  if (o.quitarFinal > 0) {
    const fin = T - 1;
    const ini = Math.max(o.quitarInicio, T - o.quitarFinal);
    if (ini <= fin) quitar.push(`${ini}-${fin}`);
  }
  if (quitar.length) config.remove = quitar;
  const duplicar = [];
  if (o.duplicarInicio > 0) {
    duplicar.push({ source: 0, offset: 0, length: o.duplicarInicio });
  }
  // Duplicar el ÚLTIMO frame: se copia `T-1` y se inserta detrás de él.
  if (o.duplicarFinal > 0) {
    duplicar.push({ source: T - 1, offset: T, length: o.duplicarFinal });
  }
  if (duplicar.length) config.duplicate = duplicar;
  const data = await apiFetch(`/api/cmv40/${pid}/apply-sync`, {
    method: 'POST',
    body: JSON.stringify({ editor_config: config }),
  });
  if (data) {
    showToast(tr('tab3.correccion_aplicada_nuevo', {p1: data.sync_delta > 0 ? '+' : '', sync_delta: data.sync_delta}), 'success');
    if (project) {
      project.syncData = null;  // forzar recarga
      _cmv40AssignSession(project, data);
      if (!project.expandedPhases) project.expandedPhases = {};
      project.expandedPhases['D'] = true;  // mantener la fase D visible
      _updateCMv40Panel(project);
      // Las cuatro casillas vuelven a cero al repintarse, que es lo que hay
      // que hacer tras aplicar: el desfase que queda es OTRO y se anuncia
      // arriba con el número nuevo.
    }
  }
}

async function cmv40DoSkipSync(pid) {
  const data = await _cmv40PostFase(`/api/cmv40/${pid}/mark-synced`);
  if (data) {
    showToast(tr('tab3.toast_sync_confirmado'), 'success');
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
  if (!project.chartRange) project.chartRange = _cmv40RangoPorDefecto(totalFrames);
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
    ctx.fillText(`f ${frame.toLocaleString(localeActual())}`, x, H - 8);
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
  ctx.fillText(tr('ui.rpu_target_cmv4_0'), padding.left + 30, 12);
  ctx.strokeStyle = '#ef4444';
  ctx.lineWidth = 1.5;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  ctx.moveTo(padding.left + 180, 8);
  ctx.lineTo(padding.left + 196, 8);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = 'rgba(255,255,255,0.8)';
  // **El canvas se pinta con `fillText`, así que `data-i18n` no lo alcanza
  // y el texto tiene que venir ya resuelto de `tr()`.** Esta leyenda se
  // quedó en castellano con la app en inglés hasta que el usuario la vio
  // (2026-09-23); el guard no la cazó porque `origen` viaja como nombre de
  // PARÁMETRO (`{origen}`) en el catálogo inglés y el vocabulario lo daba
  // por palabra inglesa. Ver `vocabulario_solo_castellano`.
  //
  // Las dos claves son las que YA usaba el modal de creación
  // (`ui.mkv_origen_cmv2_9` / `ui.rpu_target_cmv4_0`): añadir un par propio
  // dejaba la misma frase con dos traducciones al catalán —una con el
  // apóstrofo recto y otra con el tipográfico— y eso lo caza un guard.
  ctx.fillText(tr('ui.mkv_origen_cmv2_9'), padding.left + 202, 12);
  // Info de rango prominente (arriba a la derecha)
  const startSec = start / FPS, endSec = end / FPS;
  const fmtTime = (s) => {
    const mm = Math.floor(s / 60), ss = Math.floor(s % 60).toString().padStart(2, '0');
    return `${mm}:${ss}`;
  };
  ctx.textAlign = 'right';
  ctx.fillStyle = 'rgba(255,255,255,0.9)';
  ctx.font = '11px sans-serif';
  ctx.fillText(tr('tab3.rango_de_a', { desde: fmtTime(startSec), hasta: fmtTime(endSec) }),
               W - padding.right, 14);
  ctx.fillStyle = 'rgba(255,255,255,0.5)';
  ctx.font = '10px sans-serif';
  ctx.fillText(tr('tab3.rango_frames_de_total', {
    n: (end - start).toLocaleString(localeActual()),
    total: totalFrames.toLocaleString(localeActual()),
    fps: FPS.toFixed(2),
  }), W - padding.right, 28);
  ctx.textAlign = 'left';

  // ── Arrastrar para encuadrar ──────────────────────────────────────────
  //
  // El zoom por presets solo servía para mirar el principio, y afinar en el
  // minuto 48 obligaba a escribir números de frame. Arrastrando se encuadra
  // el tramo que se está viendo, y dentro del resultado se puede volver a
  // arrastrar: sub-selecciones hasta el suelo de un segundo.
  //
  // La banda se pinta sobre el canvas ya dibujado en vez de repintarlo todo:
  // redibujar la gráfica entera en cada `mousemove` con 1.500 puntos se nota.
  // Para borrarla al mover se guarda una foto del canvas al empezar.
  const marcoDeSeleccion = (x0, x1) => {
    if (!project._zoomFoto) return;
    ctx.putImageData(project._zoomFoto, 0, 0);
    const a = Math.min(x0, x1), b = Math.max(x0, x1);
    ctx.fillStyle = 'rgba(59,130,246,0.18)';
    ctx.fillRect(a, padding.top, b - a, plotH);
    ctx.strokeStyle = 'rgba(59,130,246,0.9)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(a + 0.5, padding.top); ctx.lineTo(a + 0.5, padding.top + plotH);
    ctx.moveTo(b - 0.5, padding.top); ctx.lineTo(b - 0.5, padding.top + plotH);
    ctx.stroke();
  };
  const xDelEvento = (e) => {
    const rect = canvas.getBoundingClientRect();
    return Math.max(padding.left,
                    Math.min(padding.left + plotW,
                             (e.clientX - rect.left) * (W / rect.width)));
  };
  const frameDeX = (x) => Math.round(start + ((x - padding.left) / plotW) * rangeSpan);

  canvas.style.cursor = 'crosshair';
  // El `mouseup` se escucha en `window` y **solo mientras se arrastra**:
  // soltar el botón fuera del gráfico —lo normal al llegar al borde— dejaba
  // la selección pegada y el siguiente clic la daba por buena. Registrarlo
  // en cada render, en cambio, apilaba un oyente por repintado.
  const alSoltar = (e) => {
    if (project._zoomX0 == null) return;
    const x0 = project._zoomX0, x1 = xDelEvento(e);
    project._zoomX0 = null;
    project._zoomFoto = null;
    // Un clic sin arrastrar no es una selección: 6 px de holgura para que
    // pulsar sobre la gráfica no encuadre un instante de nada.
    if (Math.abs(x1 - x0) < 6) { _renderCMv40Chart(project); return; }
    const a = frameDeX(Math.min(x0, x1)), b = frameDeX(Math.max(x0, x1));
    const z = _cmv40Encuadrar((a + b) / 2, b - a, totalFrames, FPS);
    _cmv40SetRange(pid, z.start, z.end);
  };
  canvas.onmousedown = (e) => {
    if (e.button !== 0) return;
    project._zoomFoto = ctx.getImageData(0, 0, W, H);
    project._zoomX0 = xDelEvento(e);
    window.addEventListener('mouseup', alSoltar, { once: true });
    e.preventDefault();
  };

  // Hover handler
  canvas.onmousemove = (e) => {
    const rect = canvas.getBoundingClientRect();
    const scaleX = W / rect.width;
    const mx = (e.clientX - rect.left) * scaleX;
    if (project._zoomX0 != null) { marcoDeSeleccion(project._zoomX0, xDelEvento(e)); return; }
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
      tooltip.innerHTML = `Frame ${absFrame.toLocaleString(localeActual())} (${mm}:${ss})<br>
        <span style="color:#ef4444">${tr('tab3.origen_p1_pq', {p1: (d.src_maxcll || 0).toFixed(0)})}</span><br>
        <span style="color:#3b82f6">${tr('tab3.target_p1_pq', {p1: (d.tgt_maxcll || 0).toFixed(0)})}</span>`;
    }
  };
  canvas.onmouseleave = () => {
    const tooltip = document.getElementById(`cmv40-chart-tooltip-${project.id}`);
    if (tooltip) tooltip.style.display = 'none';
  };
}

// ── Vista de detalle para el modal de trabajo ────────────────────────────────
// La fase CMv4.0 ya tenía la vista más completa de la app —el overlay de
// ejecución—, pero se abría SOLA y tapaba el panel. Aquí el mismo contenido se
// abre a petición desde la columna de trabajo.

/** El contexto que la timeline recibe cuando se pinta DESDE EL MODAL.
 *
 *  Con el proyecto abierto hay que reusar el suyo —guarda cachés que el
 *  render escribe, como `_resolvedStartedMs`— pero sin marcarlo a él como
 *  terminado: su panel puede estar mirando el mismo pipeline en vivo. Un
 *  objeto que DELEGA en el proyecto lee sus campos y sus cachés y se queda
 *  con lo suyo propio; copiarlo con spread rompería la caché, y mutarlo
 *  congelaría el panel.
 */
function _cmv40CtxTimeline(s, project, a) {
  const ctx = project ? Object.create(project) : { session: s };
  ctx.terminal = !!(a && a.terminal);
  // El total de la izquierda es EL MISMO que el de la tarjeta de la columna:
  // los dos contestan «cuánto queda de la conversión» y salen del mismo
  // `job_pct` calibrado. Sin esto el modal caía al escalonado por fases —«6/10
  // · 60 %» mientras la tarjeta decía 24 %—, que es la queja de la que salió
  // todo esto: varias cifras distintas para la misma pregunta.
  //
  // Va en el ctx y no en el proyecto (`Object.create` lo sombrea) para no
  // pisarle el suyo, que llega por el WS.
  if (a && a.pct != null && a.pct_medido) ctx._jobPct = a.pct;
  return ctx;
}

registrarDetalleDeTrabajo('cmv40', async (a) => {
  // Esperando respuesta, el detalle es el PROYECTO: lo que hace falta es
  // contestar, y eso se hace en sus cards. Devolver null es el contrato del
  // armazón para «ya lo he enseñado yo».
  if ((a.historial || {}).estado === 'esperando') {
    abrirProyectoCMv40Para(a.id);
    return null;
  }
  const est = {};
  const s = await apiFetch(`/api/cmv40/${a.id}`,
                           { silent: true, estado: est }).catch(() => null);
  // **Un fallo de red o un timeout no es un proyecto borrado.** Con el pool
  // del NAS saturado esta petición tarda, y entonces este adaptador seguía
  // devolviendo `cartel` (el icono de respaldo) y `cuerpo` («Todavía no hay
  // líneas de log»), los dos con valor: el guard del armazón los daba por
  // buenos y la vista degradada SUSTITUÍA a la completa. Lo que se veía es
  // el modal perdiendo su columna lateral durante un minuto y volviendo
  // solo. Reportado el 2026-09-23 durante una Fase C.
  if (!s && est.status !== 404) return { sinDatos: true };
  const project = openCMv40Projects.find(p => p.session && p.session.id === a.id);
  return {
    // Un 404 aquí sí significa que el proyecto se borró: su log vivía en
    // `/config/cmv40/{id}.log` y se fue con él.
    sinDetalle: s ? '' : 'borrado',
    titulo: s?.output_mkv_name || a.que,
    // El de SALIDA, que es lo que se está produciendo. El de origen ya está
    // dicho por la cartela.
    sub: s?.output_mkv_name || '',
    // El overlay marcaba con 🤖 que la cadena avanza sola. Es información:
    // dice si al terminar esta fase arrancará la siguiente.
    autoTag: s?.auto_pipeline ? 'Auto · ' : '',
    cartel: cartelDeTmdb(s?.tmdb_info, s?.source_mkv_name, icono('curva', 'ico-xl')),
    // La timeline con las fases y sus tiempos: es LA vista de este pipeline y
    // la tenía el overlay de ejecución. Se reusa tal cual —misma función que
    // pinta la del panel— para que las dos digan exactamente lo mismo.
    //
    // Va como FUNCIÓN, no como cadena: `_cmv40UpdateTimelineIncremental`
    // actualiza en sitio en vez de reemplazar el DOM, que es lo que evita que
    // el scroll salte al principio y que la animación del icono de la fase en
    // curso se reinicie en cada tick. Su comentario ya lo decía; lo perdimos
    // al pasar por el modal común.
    lateral: s
      ? (el) => _cmv40UpdateTimelineIncremental(el, s, _cmv40CtxTimeline(s, project, a))
      : '',
    // La tira de pasos de la cabecera sobra teniendo la timeline al lado, que
    // dice lo mismo y mejor.
    pasos: [],
    conLog: true,
    cuerpo: _trabajoLogHTML(s?.output_log),
  };
});


// El pre-flight tiene su propio modal, así que devuelve null: es el contrato
// del armazón para «ya lo he enseñado yo». Sin esto, la columna listaba la
// validación en «En paralelo» y no había forma de volver a ella.
/** El «Detalle» de un trabajo CMv4.0 que espera respuesta LLEVA AL PROYECTO.
 *
 *  Lo que hace falta ahí no es leer el log: es contestar —el ACK de una
 *  degradación, la revisión del sync, elegir el target—, y esas acciones viven
 *  en las cards del panel. Abrir el modal del log dejaba al usuario mirando
 *  dos mil líneas y teniendo que ir a la pestaña a mano.
 *
 *  El pre-flight es la excepción y tiene su propio registro: su decisión se
 *  toma en su modal, no en el panel.
 */
async function abrirProyectoCMv40Para(sid) {
  const s = await apiFetch(`/api/cmv40/${sid}`, { silent: true })
    .catch(() => null);
  if (!s) {
    showToast(tr('tab3.ese_proyecto_ya_no_esta'), 'info');
    return;
  }
  switchTab(3);
  openCMv40Project(s);
}


registrarDetalleDeTrabajo('preflight', async (a) => {
  abrirPreflightCMv40(a.sobre || a.id);
  return null;
});


// Mismo motivo que en las otras dos pestañas: el puesto en la cola de una fase
// se ve en la tarjeta del proyecto, no solo en la columna de la derecha.
alCambiarTrabajos(() => {
  if (document.getElementById('cmv40-sidebar-list')) _renderCMv40Sidebar();
});


/** Lo que hay que apagar en el frontend cuando se cancela un job CMv4.0.
 *
 *  El auto-pipeline tiene un poller cada 4 s que mira `running_phase`; el
 *  cancel lo deja a null y la fase sigue en `created`, así que el poller
 *  interpreta «hay que empezar» y **vuelve a lanzar el pre-flight**. Se vio en
 *  el NAS al cancelar la Fase A. `_autoChaining` es justo la marca de «esta
 *  cadena la pidió alguien», y cancelar es decir que ya no.
 */
function cmv40TrasCancelar(sessionId) {
  const p = (typeof openCMv40Projects !== 'undefined' ? openCMv40Projects : [])
    .find(x => x.session && x.session.id === sessionId);
  if (!p) return;
  p._autoChaining = false;
  p._lastAutoFiredFor = null;
  p.autoContinue = false;
}


// ── El pre-flight, con su modal ──────────────────────────────────────────
//
// El pre-flight decide SI va a haber trabajo: valida que el MKV origen tiene
// Dolby Vision, obtiene el bin target y comprueba que aporta CMv4.0 y que su
// L8 no es sintético. Hasta que pasa, no se encola nada.
//
// El veredicto llegaba en diferido —se cerraba el asistente y el motivo
// aparecía después como un banner en el panel—, así que había que estar
// mirando ese proyecto para enterarse. Con el modal se ve en el momento y, si
// falla, con los motivos delante.
//
// **Sigue siendo interactivo, no encolado**, y eso es deliberado: mediana 9 s
// sobre los 91 pre-flights del NAS. Un modal síncrono es una decisión de
// interfaz; encolarlo dejaría al usuario mirando «esperando turno» detrás de
// una conversión de 40 minutos.
//
// **El encolado de la Fase A se queda en el backend**, en el `finally` del
// pre-flight. Este modal observa; no decide. Moverlo aquí reproduciría la
// familia de bugs de los dos disparadores.

let _cmv40PfSesion = null;      // id del proyecto que se está validando
let _cmv40PfPolling = false;

const _CMV40_PF_INTERVALO_MS = 700;

function _cmv40PfSet(id, txt) {
  const el = document.getElementById(id);
  if (el) el.textContent = txt;
}


/** «el 10/09/2026 a las 09:12», o '' si no hay fecha que enseñar. */
function _cmv40PfCuando(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || isNaN(d)) return '';
  return d.toLocaleDateString(localeActual(), { day: '2-digit', month: '2-digit',
                                         year: 'numeric' })
       + ' a las '
       + d.toLocaleTimeString(localeActual(), { hour: '2-digit', minute: '2-digit' });
}

/** El veredicto: `{clase, titulo, cuerpo, motivos[]}` o null si sigue. */
/** El veredicto: `{clase, titulo, cuerpo, motivos[]}` o null si sigue.
 *
 *  **Sale del relato.** Antes encadenaba seis condiciones sobre cuatro campos
 *  de la sesión (`preflight_user_choice`, `error_message`,
 *  `preflight_decision`, `target_preflight_ok`) y la ficha tenía su propia
 *  versión de la misma cascada. `situacion` y `decision` la resuelven una vez
 *  en el servidor, que es el único que tiene el estado real.
 */
function _cmv40PfVeredicto(s, trabajo) {
  const r = s?.relato;
  if (!r) return null;
  const d = r.decision || {};

  // Lo que pasó DESPUÉS manda sobre la decisión: un proyecto que el usuario
  // paró no se titula «Se inyecta el RPU igualmente» por lo que contestó
  // veinte minutos antes. La ficha ya lo daba por cancelado y el modal decía
  // otra cosa — la misma discrepancia que este bloque venía a quitar.
  if (r.situacion === 'cancelado') {
    return {clase: 'aviso', titulo: r.situacion_rotulo || '',
            cuerpo: r.porque || '', motivos: _cmv40PfMotivosDelLog(s)};
  }
  // Una decisión ya tomada CIERRA la pregunta, aunque el proyecto siga
  // corriendo: sin esto el modal volvía a ofrecer los dos botones al
  // reabrirlo, pidiendo algo que el usuario ya había contestado.
  if (d.estado === 'tomada') {
    return {clase: 'ok', titulo: d.titulo || '',
            cuerpo: trabajo || r.porque || tr('tab3.el_trabajo_continua_en_segundo_plano'),
            motivos: []};
  }
  if (r.situacion === 'detenido_por_error') {
    return {clase: 'error', titulo: tr('tab3.el_bin_no_sirve_para_este'),
            cuerpo: s.error_message || '', motivos: _cmv40PfMotivosDelLog(s)};
  }
  if (d.estado === 'pendiente') {
    return {clase: 'aviso', titulo: d.titulo || '',
            cuerpo: d.porque || r.porque || '', motivos: _cmv40PfMotivosDelLog(s)};
  }
  if (s.target_preflight_ok) {
    // Solo lo que las filas NO dicen ya: dónde ha quedado el trabajo.
    return {clase: 'ok', titulo: tr('tab3.validacion_superada'),
            cuerpo: trabajo || tr('tab3.el_trabajo_continua_en_segundo_plano'),
            motivos: []};
  }
  return null;
}

/** Las líneas `[Pre-flight]` del log: son los motivos, ya escritos. */
function _cmv40PfMotivosDelLog(s) {
  return ((s && s.output_log) || [])
    .filter(l => l.includes('[Pre-flight]') || l.includes('🛑'))
    .slice(-12);
}

/** Dónde ha quedado el trabajo tras un pre-flight que pasa. */
async function _cmv40PfDondeQuedo(pid) {
  const t = await apiFetch('/api/trabajos', { silent: true }).catch(() => null);
  if (!t) return '';
  if ((t.activo?.sobre || t.activo?.id) === pid) return tr('tab3.la_fase_a_ya_esta_en');
  const enCola = (t.cola || []).find(j => (j.sobre || j.id) === pid);
  if (enCola) return tr('tab3.la_fase_a_esta_en_la', {posicion: enCola.posicion});
  return tr('tab3.el_trabajo_continua_en_segundo_plano');
}

/** Las conclusiones del pre-flight, una fila por comprobación.
 *
 *  Se rellenan según avanza, y ése es el punto: un porcentaje solo dice
 *  cuánto falta; esto dice QUÉ ha verificado y con qué dato. Cuando falla,
 *  la fila que falla es la explicación.
 */
/** Las conclusiones del pre-flight, una fila por comprobación.
 *
 *  **Ya no se derivan aquí.** Eran ~85 líneas que leían diez campos de la
 *  sesión para reconstruir lo que el servidor ya sabía, y la ficha hacía su
 *  propia versión de lo mismo: por eso las dos podían contar —y contaban—
 *  cosas distintas del mismo proyecto. Hoy las dos pintan `relato.hechos`,
 *  que es UNA lista, así que no pueden discrepar porque no hay dos cálculos.
 *
 *  La decisión va como última fila: es la única conclusión del pre-flight que
 *  no sale de un análisis, y reabrir el modal tiene que decir qué se
 *  contestó en vez de volver a preguntarlo.
 */
function _cmv40PfChecks(s) {
  const r = s?.relato;
  if (!r) return [];                       // sin relato no se inventa nada
  const filas = r.hechos.map(h => ({
    titulo: h.que,
    valor: h.evidencia,
    // El vocabulario del relato es el de estos chips —de aquí salió— salvo
    // los dos nombres largos, que el chip abrevia.
    estado: h.estado === 'pendiente' ? 'pend'
          : h.estado === 'en_curso' ? 'curso' : h.estado,
  }));

  const d = r.decision || {};
  if (d.estado === 'pendiente') {
    filas.push({titulo: tr('tab3.titulo_decision'), valor: d.pregunta || '',
                estado: 'aviso'});
  } else if (d.estado === 'tomada') {
    const cuando = _cmv40PfCuando(d.cuando);
    const rotulo = (d.opciones || []).find(o => o.id === d.elegida)?.rotulo
                || d.elegida || '';
    filas.push({titulo: tr('tab3.titulo_decision'),
                valor: rotulo + (cuando ? ` · ${cuando}` : ''), estado: 'ok'});
  }
  return filas;
}

const _CMV40_PF_ICONO = {
  ok:    ['verde', '<path d="m7 12.5 3.2 3.2L17 8.8"/>'],
  aviso: ['naranja', '<path d="M12 8v5"/><circle cx="12" cy="16.5" r=".9" fill="currentColor"/>'],
  duda:  ['azul', '<path d="M9.6 9.4a2.5 2.5 0 1 1 2.9 3v1.4"/><circle cx="12.4" cy="16.8" r=".9" fill="currentColor"/>'],
  fallo: ['rojo', '<path d="m8.5 8.5 7 7M15.5 8.5l-7 7"/>'],
};

/** El chip de estado, con la familia SVG de la aplicación (no emoji). */
function _cmv40PfChip(estado, tam = 'icono-chip-sm') {
  const [color, path] = _CMV40_PF_ICONO[estado] || _CMV40_PF_ICONO.duda;
  return `<span class="icono-chip icono-${color} ${tam}">`
       + `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"`
       + ` stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"`
       + ` aria-hidden="true">${path}</svg></span>`;
}

function _cmv40PfChecksHTML(filas) {
  return filas.map(f => {
    const icono = f.estado === 'curso'
      ? iconoDeEstado('corriendo', 'icono-chip-sm')
      : f.estado === 'pend'
      ? '<span class="trabajo-paso-punto"></span>'
      : _cmv40PfChip(f.estado);
    return `<div class="cmv40-pf-check ${f.estado}">
      ${icono}
      <div class="cmv40-pf-check-txt">
        <div class="cmv40-pf-check-t">${escHtml(f.titulo)}</div>
        <div class="cmv40-pf-check-v">${escHtml(f.valor)}</div>
      </div>
    </div>`;
  }).join('');
}

function _cmv40PfPintar(s, veredicto) {
  const prog = s?.last_progress || {};
  // La cabecera es la PELÍCULA, con la misma cartela que el modal de trabajo.
  // El estado y el veredicto no van aquí: son lo que se está haciendo, y eso
  // se cuenta en el cuerpo.
  const cartel = (typeof cartelDeTmdb === 'function')
    ? cartelDeTmdb(s?.tmdb_info, s?.source_mkv_name || s?.output_mkv_name, icono('curva', 'ico-xl'))
    : null;
  _cmv40PfSet('cmv40-pf-titulo', cartel?.titulo || tr('tab3.proyecto_cmv4_0'));
  _cmv40PfSet('cmv40-pf-sub', cartel?.meta || s?.output_mkv_name || '');
  const poster = document.getElementById('cmv40-pf-poster');
  if (poster) {
    poster.innerHTML = cartel?.url
      ? `<img src="${escHtml(cartel.url)}" alt="" loading="lazy">`
      : `<span>${cartel?.icono || icono('curva', 'ico-xl')}</span>`;
  }
  // El estado encabeza el cuerpo, junto a lo que lo justifica.
  _cmv40PfSet('cmv40-pf-estado', veredicto ? veredicto.titulo : tr('ui.validacion_previa'));
  const est = document.getElementById('cmv40-pf-estado');
  if (est) est.className = 'cmv40-pf-seccion' + (veredicto ? ' ' + veredicto.clase : '');

  const checks = document.getElementById('cmv40-pf-checks');
  if (checks) checks.innerHTML = _cmv40PfChecksHTML(_cmv40PfChecks(s));

  // La barra desaparece con el veredicto: ya no hay nada que medir, y dejarla
  // a media asta sugeriría que sigue.
  const wrap = document.getElementById('cmv40-pf-barra-wrap');
  if (wrap) wrap.style.display = veredicto ? 'none' : '';
  const barra = document.getElementById('cmv40-pf-barra');
  if (barra) barra.style.width = `${Math.round(prog.pct || 0)}%`;
  _cmv40PfSet('cmv40-pf-paso', prog.label || tr('ui.iniciando'));
  _cmv40PfSet('cmv40-pf-pct', prog.pct == null ? '' : `${Math.round(prog.pct)} %`);

  const caja = document.getElementById('cmv40-pf-veredicto');
  if (caja) {
    // El banner solo si aporta algo que las filas no digan ya. Repetir palabra
    // por palabra el valor de la fila que falla es ruido justo donde hay que
    // leer con calma.
    const yaDicho = _cmv40PfChecks(s)
      .some(f => veredicto && f.valor === veredicto.cuerpo);
    caja.innerHTML = (!veredicto || yaDicho) ? '' : `
      <div class="cmv40-pf-banner ${veredicto.clase}">${escHtml(veredicto.cuerpo)}</div>`;
  }

  // El registro: siempre disponible, desplegado solo cuando hace falta leerlo.
  const det = document.getElementById('cmv40-pf-detalle');
  const log = document.getElementById('cmv40-pf-log');
  const lineas = _cmv40PfMotivosDelLog(s);
  if (det) det.style.display = lineas.length ? '' : 'none';
  if (det && veredicto && veredicto.clase !== 'ok') det.open = true;
  if (log) {
    const ancla = anclajeDeLog(log);
    log.innerHTML = lineas.map(l =>
      `<div class="log-line ${typeof _classifyLogLine === 'function'
        ? _classifyLogLine(l) : ''}">${escHtml(l)}</div>`).join('');
    restaurarAnclajeDeLog(log, ancla);
  }
  _cmv40PfPintarPie(s, veredicto);
}

function _cmv40PfPintarPie(s, veredicto) {
  const pie = document.getElementById('cmv40-pf-pie');
  if (!pie) return;
  const pid = _cmv40PfSesion;
  // Cerrar NO cancela: la validación sigue y el veredicto queda en el panel.
  const cerrar = `<button class="btn btn-ghost btn-sm"
      onclick="cerrarPreflightCMv40()" data-i18n="ui.cerrar"></button>`;
  if (!veredicto) {
    pie.innerHTML = `
      <button class="btn btn-danger btn-sm" onclick="cancelarPreflightCMv40()" data-i18n-tip="tab3.detiene_la_validacion_el_proyecto_se">
        <span data-i18n="tab3.detener"></span></button>${cerrar}`;
    return;
  }
  if (veredicto.clase === 'ok') { pie.innerHTML = cerrar; return; }
  // NO hay «cambiar de RPU»: con el pre-flight detenido la sesión se queda en
  // `created`, y la card de Fase B arranca en `source_analyzed` (`startsFrom`),
  // así que sale bloqueada. Para probar otro bin hay que crear el proyecto de
  // nuevo. Un botón que lleva a una card que no se puede abrir es peor que no
  // tenerlo: promete una salida que no existe.
  //
  // Los dos que sí funcionan reusan los endpoints del panel.
  const decidir = veredicto.clase === 'aviso' ? `
    <button class="btn btn-ghost btn-sm" onclick="_cmv40PfForzar('${pid}')" data-i18n="tab3.inyectar_igualmente" data-i18n-tip="tab3.inyectar_el_rpu_pese_a_la"></button>
    <button class="btn btn-primary btn-sm" onclick="_cmv40PfMantener('${pid}')" data-i18n="tab3.mantener_el_mkv" data-i18n-tip="tab3.cerrar_el_proyecto_sin_procesar_el"></button>` : '';
  pie.innerHTML = decidir + cerrar;
}

/** Abre el modal y polea hasta el veredicto. */
async function abrirPreflightCMv40(pid) {
  _cmv40PfSesion = pid;
  // El modal se abre ANTES de pintarlo. Al revés, un fallo del render lo
  // dejaba sin abrir y sin rastro: el `throw` viaja dentro de una función
  // async, así que no lo caza ni el listener de errores de la página.
  openModal('cmv40-preflight-modal');
  try { _cmv40PfPintar(null, null); } catch (e) { console.error('[pf]', e); }
  if (_cmv40PfPolling) return;
  _cmv40PfPolling = true;
  try {
    // Chained-await, no setInterval: garantiza una sola petición en vuelo y
    // con ella el orden de las respuestas. Es el patrón del perfil de
    // luminancia, y está ahí por un bug de respuestas cruzadas.
    while (_cmv40PfSesion === pid
           && document.getElementById('cmv40-preflight-modal')
                ?.classList.contains('open')) {
      const s = await apiFetch(`/api/cmv40/${pid}`, { silent: true })
        .catch(() => null);
      let veredicto = _cmv40PfVeredicto(s, null);
      if (veredicto?.clase === 'ok') {
        veredicto = _cmv40PfVeredicto(s, await _cmv40PfDondeQuedo(pid));
      }
      if (_cmv40PfSesion !== pid) break;
      try { _cmv40PfPintar(s, veredicto); } catch (e) { console.error('[pf]', e); }
      // Mientras corre no hay veredicto; en cuanto lo hay, el modal se queda
      // quieto esperando al usuario.
      if (veredicto) break;
      await new Promise(r => setTimeout(r, _CMV40_PF_INTERVALO_MS));
    }
  } finally {
    _cmv40PfPolling = false;
  }
}

function cerrarPreflightCMv40() {
  _cmv40PfSesion = null;
  closeModal('cmv40-preflight-modal');
}

function cancelarPreflightCMv40() {
  const pid = _cmv40PfSesion;
  if (!pid) return;
  showConfirm(
    tr('tab3.detener_la_validacion'),
    tr('tab3.el_proyecto_se_queda_creado_y') + ' '
    + tr('tab3.rpu_cuando_quieras'),
    async () => {
      await apiFetch(`/api/cmv40/${pid}/cancel`, { method: 'POST' });
      if (typeof cmv40TrasCancelar === 'function') cmv40TrasCancelar(pid);
      cerrarPreflightCMv40();
      refrescarWorkbar();
    },
    tr('tab3.si_detenerla'));
}

/** Aplica al proyecto abierto la sesión que devuelve el endpoint. */
function _cmv40PfAplicar(pid, data) {
  const p = openCMv40Projects.find(x => x.session && x.session.id === pid);
  if (!p || !data) return p || null;
  _cmv40AssignSession(p, data);
  _updateCMv40Panel(p);
  return p;
}

async function _cmv40PfMantener(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}/accept-keep`, { method: 'POST' });
  cerrarPreflightCMv40();
  _cmv40PfAplicar(pid, data);
  await refreshCMv40Sidebar();
}

async function _cmv40PfForzar(pid) {
  const data = await apiFetch(`/api/cmv40/${pid}/override-recommendation`,
                              { method: 'POST' });
  cerrarPreflightCMv40();
  const p = _cmv40PfAplicar(pid, data);
  // **Aquí NO se dispara la fase.** `override-recommendation` ya despacha la
  // siguiente cuando `auto_pipeline` está activo, así que pedirla también
  // desde el frontend daba un 409 del guard de duplicados —un toast rojo de
  // «ya hay una fase en curso»— mientras el trabajo se encolaba igual. Con
  // auto desactivado tampoco se lanza: ahí el usuario lanza las fases a mano,
  // y forzar es levantar el freno, no pulsar el acelerador.
  //
  // Sí se limpia el dedup del poller: `preflight_decision` era su condición
  // de parada y ya no está, así que lo que venga después es legítimo.
  if (p) p._lastAutoFiredFor = null;
}



// ═══════════════════════════════════════════════════════════════════
//  RECORDATORIO DE LA DONACIÓN A DOVITOOLS
// ═══════════════════════════════════════════════════════════════════
//
// La app trae el enlace del repositorio, pero el repositorio es de otra
// persona y su acceso va por donación. En vez de ignorar esa puerta, la app
// la reconoce: lleva la cuenta de los bins descargados y cada N lo recuerda.
//
// Dos límites que definen el diseño:
//   · **nunca sale si el usuario puso su enlace** — quien lo tiene, donó. Lo
//     decide el backend (`repo_de_la_app`), no el frontend;
//   · **no bloquea nada**. Es un recordatorio, no un peaje: se cierra con
//     «Ahora no» y el trabajo sigue exactamente igual.
//
// Se comprueba al ENTRAR en la pestaña y no con un poller: el dato cambia
// solo cuando se descarga un bin, y quien descarga bins pasa por aquí.

/** Una vez por carga de página: entrar y salir del tab no lo repite. */
let _avisoDonacionMostrado = false;

/**
 * Mira si toca recordar la donación, y lo enseña si toca.
 * Silencioso ante cualquier fallo: perder el recordatorio es un
 * inconveniente; un toast rojo al entrar en la pestaña, no.
 */
async function comprobarAvisoDonacion() {
  if (_avisoDonacionMostrado) return;
  let d;
  try {
    d = await apiFetch('/api/cmv40/repo-donacion', { silent: true });
  } catch (e) { return; }
  if (!d || !d.avisar) return;
  _avisoDonacionMostrado = true;
  const cuenta = document.getElementById('donacion-cuenta');
  if (cuenta) {
    cuenta.textContent =
      tr(d.descargas === 1 ? 'tab3.llevas_n_rpu_descargado' : 'tab3.llevas_n_rpus_descargados', {n: d.descargas}) + ' '
      + tr('tab3.del_repositorio_dovitools');
  }
  openModal('dovitools-donacion-modal');
}

/**
 * Cierra el recordatorio y reinicia la cuenta hacia el siguiente.
 *
 * El «visto» se manda en las TRES salidas —donar, poner mi enlace y ahora
 * no—: la alternativa sería no marcarlo al posponer, y entonces el aviso
 * volvería a salir en la siguiente descarga en vez de dentro de otros N.
 * Un recordatorio que reaparece enseguida deja de leerse y pasa a molestar,
 * que es justo lo contrario de lo que se busca.
 */
function cerrarAvisoDonacion() {
  closeModal('dovitools-donacion-modal');
  apiFetch('/api/cmv40/repo-donacion/visto', { method: 'POST', silent: true })
    .catch(() => {});
}
