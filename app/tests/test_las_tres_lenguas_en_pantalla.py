"""Los paneles reales, con datos reales, en los tres idiomas.

Este fichero existe porque la suite entera pasaba en verde mientras el usuario
veía, en dos minutos de uso, **claves crudas** (`tab3.fase_h`), **rótulos en
castellano con la app en inglés** (`FASE A`) y un modal a medio traducir. El
diagnóstico de por qué: los guards se escribieron alrededor de lo que el
extractor sabía mirar —cadenas sueltas, `index.html`, el servidor— y las
capturas de Chrome que había nunca llegaron a un panel de proyecto CON DATOS.
Un modal vacío no enseña nada.

Así que aquí se mide lo que el usuario ve:

- se monta el `index.html` real con `window.__I18N` sembrado, que es el mismo
  arranque de producción (el script bloqueante de `/api/i18n/catalogo.js`);
- se construye un proyecto CMv4.0 en mitad del pipeline —con gates críticos
  pendientes, refinamiento L5, recomendación de la hoja y artefactos— y el
  panel de proyecto de Tab 1 con sus pistas incluidas, descartadas y sus
  capítulos;
- se renderizan los paneles y se lee el TEXTO y los ATRIBUTOS resultantes;
- y se exige, en las tres lenguas, que no se vea ninguna clave, ningún
  `undefined`, ningún `⟦⟧` y ni un error de JS — y en inglés, ninguna palabra
  que solo exista en el catálogo castellano.

Los títulos de las fixtures van en inglés a propósito (`Blade Runner 2049`):
así cualquier castellano en la pantalla inglesa viene de la app y no del dato.

Se salta sin Chrome. Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_las_tres_lenguas_en_pantalla -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import html, semilla_catalogo  # noqa: E402

_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]
CHROME = next((c for c in _CANDIDATOS if c and Path(c).exists()), None)

SESION_CMV40 = {
  "id": "c1", "source_mkv_name": "Blade Runner 2049 (2017) [DV FEL].mkv",
  "output_mkv_name": "Blade Runner 2049 (2017) [DV FEL][CMv4 FULL].mkv",
  "phase": "sync_verified", "running_phase": None, "archived": False,
  "error_message": "", "auto_pipeline": True, "source_workflow": "p7_fel",
  "target_type": "trusted_p8_source", "target_trust_ok": False,
  "trust_override": "auto", "output_workflow": "restore_merge",
  "source_frame_count": 243552, "target_frame_count": 243552, "source_fps": 23.976,
  "sync_delta": 0, "sync_config": {"steps": [], "total_removed": 0, "total_duplicated": 0},
  "phases_skipped": ["sync_verification_pause"],
  "source_dv_info": {"profile": 7, "el_type": "FEL", "cm_version": "v2.9",
                     "has_l8": False, "l5": "0/0/0/0", "l6_max": 1000, "l1_max": 1000},
  "target_dv_info": {"profile": 8, "el_type": None, "cm_version": "v4.0",
                     "has_l8": True, "l5": "0/0/0/0", "l6_max": 1000, "l1_max": 1000},
  "target_l8_classification": "real", "target_l8_quality_tier": "full",
  "target_l8_unique_count": 1132, "target_l8_scene_cuts": 1101,
  "target_l8_neutral_frames_pct": 0.001, "target_preflight_ok": True,
  "target_l8_has_mid_contrast": True, "target_l8_has_clip_trim": True,
  "target_l8_quality_label": "CMv4 FULL",
  "target_l8_quality_description": "CMv4.0 FULL master",
  "target_l2_unique_count": 4621, "source_l2_unique_count": 4621,
  "target_l2_target_pqs": [2081, 3079], "source_l2_target_pqs": [2081, 3079],
  "l2_comparison": "identical",
  "target_frames_analyzed": 141336, "source_frames_analyzed": 141336,
  "source_video_codec": "HEVC", "source_duration_seconds": 5904.0,
  "source_file_size_bytes": 78000000000, "sync_offset_detected": 0,
  "target_rpu_source": "drive",
  "target_rpu_path": "The.Super.Mario.Galaxy.Movie.2026.UHD_P7 FEL.bin",
  "recommended_action": "drop_in",
  "recommended_action_label": "Inject the CMv4.0 RPU (fast)",
  "recommended_action_reason": "Profiles match and L2 is identical.",
  "preflight_decision": "", "preflight_message": "",
  "compat_warning": "", "last_progress": {"pct": 42.0, "label": "demux"},
  "awaiting_critical_ack": True,
  "critical_gate_failures": [{"gate": "l5_div", "severity": "ack_required",
                              "detail": "L5 body divergence 41.2%"}],
  "user_acknowledged_degradation": False, "pipeline_aborted": False,
  # La forma REAL que lee `_cmv40GateBloque3`: un dict por gate, no una
  # lista. Con el nombre de antes (`trust_gates`) el bloque ③ no pintaba
  # NADA y el guard pasaba en verde vigilando el vacío — es lo que dejó
  # fuera «umbral exacto», «presente» y «crítico».
  #
  # `why` se deja AUSENTE a propósito: cuando el backend lo manda sustituye
  # al `tr()`, y es texto persistido en la sesión (misma familia que el
  # veredicto de la hoja). Aquí se quiere ejercitar el camino del catálogo.
  "target_trust_gates": {
      "frames": {"ok": True, "bd": 141336, "target": 141336,
                 "severity": "ok", "critical": True},
      "cm_version": {"ok": True, "value": "v4.0", "severity": "ok",
                     "critical": True},
      "has_l8": {"ok": True, "severity": "ok", "critical": True},
      "l5_div": {"ok": True, "px_max": 0, "soft_px": 5, "severity": "ok",
                 "critical": True, "sampled_method": "per_frame_completo",
                 "body_coverage": 0.9974},
      "l6_div": {"ok": True, "nits_diff": 0, "threshold": 50,
                 "severity": "ok", "critical": False},
      "l1_div": {"ok": True, "pct_diff": 0, "threshold_pct": 5,
                 "severity": "ok", "critical": False},
  },
  "sheet_recommendation": {"verdict": "caveats", "status_label": "With caveats",
      "dv_source": "retail", "sync_offset": 16, "notes": "cmv4.0 bloc restored",
      "section": "feasible", "rows": [], "blockers": ["p8_only"],
      "title": "Blade Runner 2049", "year": 2017},
  "phase_history": [{"phase": "analyze_source", "status": "done", "seconds": 826},
                    {"phase": "target_rpu_drive", "status": "done", "seconds": 3},
                    {"phase": "extract", "status": "done", "seconds": 1204}],
  "artifacts": {"source.hevc": 62000000000, "BL.hevc": 55000000000,
                "EL.hevc": 7000000000, "RPU_source.bin": 12000000,
                "RPU_target.bin": 13000000},
  "output_log": ["━━━ Fase A ━━━", "✓ Fase A completada"],
  "updated_at": "2026-09-16T08:00:00Z", "created_at": "2026-09-15T08:00:00Z",
  "tmdb_info": {"poster_url": "https://image.tmdb.org/t/p/w342/br.jpg",
                "title": "Blade Runner 2049", "year": 2017, "overview": "A young blade runner.",
                "genres": ["Science Fiction"], "rating": 7.6, "vote_count": 12000,
                "runtime": 164},
  "plan": {"drop_in": False, "trust_effective": False, "target_needs_merge": True,
           "skip_sync_review": False,
           "extract": {"needs_demux": True}, "inject": {"needs_merge": True,
           "needs_profile8": False, "hevc_input": "EL.hevc",
           "hevc_output": "EL_injected.hevc", "plan_text": "merge CMv4.0"},
           "remux": {"needs_dovi_mux": True, "video_track_name": "P7 FEL CMv4.0"},
           "validate": {"fast_path": False}, "inputs": {"skip_sync_review": False}},
}

SESION_TAB1 = {
  "id": "s1", "mkv_name": "Blade Runner 2049 (2017) [DV FEL] [Audio DCP].mkv",
  "status": "done", "media_type": "movie", "source_type": "iso",
  "iso_path": "Blade Runner 2049.iso", "source_path": "Blade Runner 2049.iso",
  "estimated_size_bytes": 64000000000, "has_fel": True, "audio_dcp": True,
  "execution_history": [{"status": "done", "run": 1, "seconds": 2100,
                         "phases": {"extract": 2050}, "output_log": ["ok"]}],
  "updated_at": "2026-09-16T08:00:00Z", "last_executed": "2026-09-16T07:00:00Z",
  "tmdb_info": {"poster_url": "https://image.tmdb.org/t/p/w342/br.jpg",
                "title": "Blade Runner 2049", "year": 2017},
  "audio_tracks": [
    {"included": True, "label": "Spanish TrueHD Atmos 7.1 (DCP 9.1.6)",
     "flag_default": True, "orig_pos": 1, "selection_reason": "best quality",
     "raw": {"codec": "TrueHD Atmos", "language": "Spanish", "description": "7.1 / 48 kHz",
             "format_commercial": "Dolby TrueHD with Dolby Atmos",
             "channel_layout": "L R C LFE Lss Rss Lrs Rrs", "bitrate_kbps": 4500,
             "compression_mode": "Lossless"}},
    {"included": False, "label": "French DD 5.1", "flag_default": False, "orig_pos": 4,
     "discard_reason": "language not included", "inferred_subtitle_type": None,
     "raw": {"codec": "AC-3", "language": "French", "description": "5.1 / 48 kHz",
             "format_commercial": "Dolby Digital", "bitrate_kbps": 640,
             "compression_mode": "Lossy"}}],
  "subtitle_tracks": [
    {"included": True, "label": "Spanish Forced (PGS)", "flag_default": True,
     "flag_forced": True, "subtitle_type": "forced", "orig_pos": 6,
     "selection_reason": "spanish forced subs",
     "raw": {"codec": "PGS", "language": "Spanish", "packet_count": 248,
             "bitrate_kbps": 1.0}}],
  "chapters": [{"number": 1, "name": "Chapter 01", "timestamp": "00:00:00.000",
                "name_custom": False},
               {"number": 2, "name": "Chapter 02", "timestamp": "00:10:00.000",
                "name_custom": False}],
  "bdinfo_result": {"main_mpls": "00800.mpls", "main_m2ts": "00800.m2ts",
     "duration_seconds": 9840, "video_tracks": [{"codec": "HEVC",
     "resolution": "3840x2160", "bitrate_kbps": 58000, "hdr": {"max_cll": 1000,
     "max_fall": 400, "primaries": "BT.2020", "transfer": "PQ", "bit_depth": 10,
     "mastering_display_luminance": "1000/0.005",
     "mastering_display_primaries": "Display P3"}}],
     "dovi": {"profile": 7, "el_type": "FEL", "cm_version": "v2.9"}},
}


# El `DoviInfo` de un MKV YA auditado, que es la ficha de Tab 2 que el
# usuario reportó primero. Los textos van en inglés a propósito: los escribe
# el servidor y aquí se quiere ver qué añade el FRONTEND.
DOVI_TAB2 = {
  "profile": 7, "el_type": "FEL", "cm_version": "v2.9", "rpu_present": True,
  "has_l1": True, "has_l2": True, "has_l5": True, "has_l6": True,
  "has_l8": False, "has_l9": True, "has_l10": False, "has_l11": False,
  "has_l4": False, "has_l254": False,
  "l1_max_cll": 1000, "l1_max_fall": 400,
  "l2_target_nits": [100, 600], "l5_active_area": "0/0/0/0",
  "l6_max_cll": 1000, "l6_max_fall": 400, "l9_primaries": "Display P3",
  "l11_content_type": None, "scene_count": 1101, "frame_count": 141336,
  "quality_classification": "real", "quality_tier": "",
  "quality_tier_label": "CMv2.9 CORE",
  "quality_tier_description": "Standard release grade",
  "quality_verdict_text": "Standard CMv2.9 — basic master trims",
  "quality_verdict_color": "yellow",
  "quality_reason": "L2 with 1026 unique combos over 2 target_pqs.",
  "quality_provenance_hints": ["Pure CMv2.9 RPU — original Blu-ray"],
  "quality_total_frames_rpu": 141336, "quality_frames_with_cmv40": 0,
  "quality_scene_cuts": 1101, "quality_l2_unique_count": 1026,
  "quality_l2_target_pqs": [2081, 3079], "quality_l8_unique_count": 0,
  "quality_l8_neutral_pct": 0.0, "quality_l8_has_mid_contrast": False,
  "quality_l8_has_clip_trim": False,
  "l1_stats": {"total": 141336, "peak": 1000, "p99": 940, "p95": 700,
               "p50": 120, "avg_of_max": 180, "bucket_dim": 90000,
               "bucket_mid": 40000, "bucket_high": 11336},
  "l1_references": {"l5_zones": [{"zone": "0/0/0/0", "frames": 141336}],
                    "l2_targets": [100, 600], "l6": {"max_cll": 1000}},
  # Lista PLANA de nits por escena: es lo que `_rgrfSparklineSvg`
  # consume. Con pares `[i, v]` el SVG sale entero a `NaN`.
  "per_scene_max_cll": [120, 300, 900, 450, 80, 1000, 210, 660, 95, 330],
}


# ── La columna de trabajo, ⚙︎ Configuración, la consulta rápida y el file
#    browser estaban a CERO pantallas medidas. Un trabajo con la forma que
#    `trabajos.py` emite, y los payloads de los otros tres.
TRABAJO = {
  "id": "c1", "sobre": "c1", "tab": "cmv40", "tipo": "cmv40_fase",
  "que": "CMv4.0 upgrade · Blade Runner 2049", "titulo": "Blade Runner 2049",
  "poster": "https://image.tmdb.org/t/p/w92/br.jpg",
  "fase": "extract", "fase_label": "Phase C · Extract BL/EL", "paso": "Demuxing BL/EL",
  "chips": ["merge", "auto"], "fase_n": 3, "fases_total": 7,
  "pct": 42.0, "pct_medido": True, "segundos": 1204, "eta_s": 900,
  "eta_fuente": "medido", "cancelable": True,
  "fase_progreso": {"pct": 61.0, "pct_medido": True, "segundos": 300,
                    "eta_s": 180, "eta_fuente": "medido"},
}
TRABAJO_COLA = {**TRABAJO, "id": "c2", "sobre": "c2", "pct": None,
                "pct_medido": False, "eta_s": None, "eta_fuente": None,
                "segundos": 0, "cancelable": False}
RECIENTE = {"id": "r1", "tab": "rip", "tipo": "rip",
            "que": "MKV conversion · Blade Runner 2049",
            "titulo": "Blade Runner 2049", "poster": "",
            "inicio": "2026-09-17T08:00:00Z", "fin": "2026-09-17T08:35:00Z",
            "segundos": 2100, "estado": "done", "error": None, "ref_log": None}

AJUSTES = {
  "tmdb": {"configured": True, "source": "default", "last4": None},
  "google": {"configured": False, "source": "none", "last4": None},
  "sheet_url": "https://docs.google.com/spreadsheets/d/x",
  "sheet_is_default": True,
  "drive_folder": {"configured": True, "source": "default", "url": "https://drive.google.com/x"},
  "idioma": {"activo": "en", "disponibles": ["es", "en", "ca"]},
  "aviso_fin": True, "aviso_sonido": False,
  "update_ignored_version": "",
}

# El payload de la consulta rápida: recomendación de la hoja + bins del repo
# + candidatos de TMDb. Todo en inglés, como el resto de las fixtures.
LOOKUP_REC = {
  "status": "recommended", "verdict_label": "Feasible",
  "verdict_detail": "The sheet confirms the CMv4.0 block can be restored.",
  "input_title": "Blade Runner 2049", "input_year": 2017,
  "match_title": "Blade Runner 2049", "match_year": 2017,
  "match_source": "tmdb", "match_confidence": 1.0, "tmdb_configured": True,
  "rows": [{"feasible": True, "section": "feasible", "dv_source": "retail",
            "sync_offset": "(+16)", "sync_offset_frames": 16,
            "notes": "cmv4.0 bloc can be restored", "blockers": [],
            "blocker_labels": [], "applies_to_our_workflow": True,
            "match_confidence": 1.0}],
  "rows_omitted": 0, "feasible_row_count": 1, "infeasible_row_count": 0,
  "blockers": [], "blockers_apply_to_our_workflow": False,
  "primary_section": "feasible", "feasible": True, "dv_source": "retail",
  "sync_offset": "(+16)", "sync_offset_frames": 16,
  "notes": "cmv4.0 bloc can be restored", "sheet_rows_loaded": 828,
  "sheet_source": "api", "google_configured": False,
}
LOOKUP_REPO = {"configured": True, "candidates": [
  {"name": "Blade Runner 2049 2017 UHD BD_P7 FEL.bin", "id": "f1",
   "size": 13000000, "score": 0.97, "predicted_type": "trusted_p7_fel_final",
   "provenance": "retail", "folder": "BR2049"}]}
LOOKUP_TMDB = {"title": "Blade Runner 2049", "year": 2017, "tmdb_id": 335984,
               "poster_url": "https://image.tmdb.org/t/p/w342/br.jpg",
               "overview": "A young blade runner.", "runtime": 164,
               "genres": ["Science Fiction"], "rating": 7.6}


_SONDA = ("<script>window.__errores=[];"
          "window.addEventListener('error',e=>window.__errores.push("
          "(e.message||'')+' @ '+(e.filename||'').split('/').pop()+':'+e.lineno));"
          "window.fetch=()=>new Promise(()=>{});"
          "window.WebSocket=function(){this.close=()=>{};this.send=()=>{};};</script>")

_CUERPO = """
(function () {
  const S = %s, T1 = %s, DV = %s, X = %s;
  const salida = {errores: [], pantallas: {}, fallos: {}, constantes: {}};
  const host = document.createElement('div');
  host.id = '__host'; document.body.appendChild(host);

  const leer = el => ({
    texto: el.innerText || el.textContent || '',
    atributos: [...el.querySelectorAll(
        '[data-tooltip],[title],[placeholder],[aria-label]')]
      .flatMap(e => [e.getAttribute('data-tooltip'), e.getAttribute('title'),
                     e.getAttribute('placeholder'), e.getAttribute('aria-label')])
      .filter(Boolean),
  });

  // ── El panel de proyecto de Tab 1, construido de verdad.
  //
  // Los renders escriben en ids PREFIJADOS por el proyecto activo (`E()`), así
  // que sin el panel montado y sin `switchSubTab` no hay nada que medir: es lo
  // que hacía que las capturas anteriores no vieran ni una pista.
  try {
    const cont = document.getElementById('subtab-main');
    if (cont) {
      openProjects.push({id: T1.id, session: T1, name: T1.mkv_name});
      createProjectPanel({id: T1.id});
      switchSubTab(T1.id);
      const panel = document.getElementById('panel-project-' + T1.id);
      const pistas = T1.audio_tracks.concat(T1.subtitle_tracks);
      renderIncludedTracks(pistas);
      renderDiscardedTracks(pistas);
      renderChapters(T1.chapters, false, '');
      pintarTextos(panel);
      salida.pantallas['tab1·panel_de_proyecto'] = leer(panel);
    }
  } catch (e) { salida.fallos['tab1·panel_de_proyecto'] = String(e && e.message || e); }

  const CASOS = {
    'tab3·info':            () => _renderCMv40Info(S, 'c1'),
    'tab3·hoja':            () => _renderCMv40SheetCard(S, 'c1'),
    'tab3·recomendacion':   () => _renderCMv40RecommendationCard(S, 'c1'),
    'tab3·tira_de_fases':   () => _renderCMv40PhaseStrip(S, 'c1'),
    'tab3·banner_ack':      () => _cmv40RenderCriticalAckBanner('c1', S),
    'tab3·gates_bc':        () => _cmv40RenderGateCardBC('c1', S, true),
    'tab3·gates_gh':        () => _cmv40RenderGateCardGH('c1', S, true),
    'tab3·fase_a':          () => _cmv40FaseABody('c1', S),
    'tab3·fase_b':          () => _cmv40FaseBBody('c1', S),
    'tab3·fase_c':          () => _cmv40FaseCBody('c1', S),
    'tab3·fase_d':          () => _cmv40FaseDBody('c1', S),
    'tab3·fase_f':          () => _cmv40FaseFBody('c1', S),
    'tab3·fase_g':          () => _cmv40FaseGBody('c1', S),
    'tab3·fase_h':          () => _cmv40FaseHBody('c1', S),
    'tab3·timeline':        () => _cmv40RenderTimeline(S, {id: 'c1', session: S,
                                     expandedPhases: {}}),
    'tab2·mastering':       () => _rgrfMasteringChain(
                                     DV, T1.bdinfo_result.video_tracks[0].hdr,
                                     T1.bdinfo_result.video_tracks[0]),
    // La ficha del MKV que el usuario reportó PRIMERO: el veredicto de la
    // auditoría, el perfil de luminancia y sus percentiles.
    'tab2·auditoria':       () => _rgrfQualityAuditCard(DV, false),
    'tab2·stats_l1':        () => _rgrfL1StatsCard(DV.l1_stats,
                                     T1.bdinfo_result.video_tracks[0].hdr),
    'tab2·sparkline':       () => _rgrfSparklineSvg(DV.per_scene_max_cll, 1000, 5904,
                                     {references: DV.l1_references}),
    'tab2·l5':              () => _rgrfL5Svg(DV),
    'tab2·gamut':           () => _rgrfGamutSvg(DV.l9_primaries, null),
    'tab2·distribucion':    () => _rgrfDistributionSvg(DV.per_scene_max_cll),
    // ── Los cuatro ficheros que estaban a CERO pantallas medidas.
    'workbar·activo':       () => _workbarActivoHTML(X.act),
    'workbar·tarjeta':      () => _workbarTarjeta(X.cola, {ref: 'cola:c2'}),
    'workbar·reciente':     () => _workbarTarjeta(X.rec, {ref: 'rec:r1'}),
    'workbar·chips':        () => _workbarChips(X.act),
    'workbar·descripcion':  () => _workbarDescripcion(X.act),
    // ⚙︎ Configuración escribe en los ids de `index.html`, no devuelve
    // marcado: se llama y se lee el modal entero, que es lo que se ve.
    'settings·panel':       () => { _renderSettings(X.aj); renderAvisoFinSettings();
                                    const e = document.getElementById('settings-modal');
                                    return e ? e.innerHTML : ''; },
    // `_cmv40LookupTagMeta` devuelve un OBJETO; lo que se lee es su `label`.
    'modals·lookup_res':    () => { const c = document.createElement('div');
                                    _cmv40LookupRenderResults(c, X.lrec, X.lrepo, X.ltmdb);
                                    return c.innerHTML; },
    'modals·lookup_tag':    () => ['trusted_p7_fel_final', 'trusted_p7_mel_final',
                                   'trusted_p8_source', 'generic', 'incompatible']
                                  .map(t => (_cmv40LookupTagMeta(t) || {}).label || '')
                                  .join(' · '),
    'modals·lookup_pipe':   () => _cmv40LookupPipelineSummary(
                                    'trusted_p7_fel_final', 'retail'),
    'browser·roots':        () => { _fileBrowser.roots = ROOTS_MKV;
                                    _renderFileBrowserRoots();
                                    const e = document.getElementById('file-browser-roots');
                                    return e ? e.innerHTML : ''; },
  };
  for (const [nombre, fn] of Object.entries(CASOS)) {
    try {
      const h = fn();
      host.innerHTML = (typeof h === 'string') ? h : '';
      pintarTextos(host);
      salida.pantallas[nombre] = leer(host);
    } catch (e) { salida.fallos[nombre] = String(e && e.message || e); }
  }

  // La vista previa del pipeline del modal de creación vive en una CONSTANTE
  // de módulo con `tr()` dentro: se resuelve al parsear, que es exactamente
  // lo que el catálogo bloqueante hace posible. Aquí se leen sus textos.
  try {
    salida.constantes.pipeline_preview = JSON.stringify(_CMV40_PIPELINE_PREVIEW);
  } catch (e) { salida.fallos['constante·pipeline_preview'] = String(e); }

  salida.errores = window.__errores || [];
  document.getElementById('__out').textContent = JSON.stringify(salida);
})();
"""


def _pintar(idioma: str) -> dict:
    import html as H
    cuerpo = _CUERPO % (json.dumps(SESION_CMV40), json.dumps(SESION_TAB1),
                        json.dumps(DOVI_TAB2), json.dumps({
                            "act": TRABAJO, "cola": TRABAJO_COLA,
                            "rec": RECIENTE, "aj": AJUSTES,
                            "lrec": LOOKUP_REC, "lrepo": LOOKUP_REPO,
                            "ltmdb": LOOKUP_TMDB}))
    pagina = html().replace("</head>", _SONDA + semilla_catalogo(idioma) + "</head>")
    pagina = pagina.replace(
        "</body>", f'<pre id="__out"></pre><script>{cuerpo}</script></body>')
    pagina = (pagina.replace('src="/static/', 'src="')
                    .replace('href="/static/', 'href="'))
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                      encoding="utf-8", dir=str(APP_DIR / "static"))
    tmp.write(pagina)
    tmp.close()
    try:
        dom = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--allow-file-access-from-files",
             "--dump-dom", "--window-size=1400,1100", "--virtual-time-budget=8000",
             tmp.name], capture_output=True, text=True, timeout=180).stdout
    finally:
        os.unlink(tmp.name)
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        raise unittest.SkipTest("Chrome no devolvió el volcado")
    return json.loads(H.unescape(m.group(1)))


# La card 🛡️ Validaciones **ya no está exenta**, y la lista está VACÍA.
#
# Lo estuvo por la decisión «interfaz sí, diagnóstico no», y el resultado
# medido fue lo contrario de lo que esa decisión pretendía: los títulos de
# los gates y sus explicaciones sí se tradujeron (salen de `tr()`), así que
# lo único que quedaba en castellano eran el `umbral`, el `presente` y el
# chip `crítico` — la card a medias, que es justo lo que CLAUDE.md dice que
# es peor que cualquiera de las dos opciones. Y la exención tapaba de paso
# los bloques ②, ④ y ⑤ enteros.
#
# Una exención por PANTALLA es demasiado gruesa para este guard: cubre
# cientos de cadenas de golpe. Si algún día hace falta eximir algo de aquí,
# que sea por cadena y con su motivo.
EN_CASTELLANO_A_PROPOSITO: dict[str, str] = {}


def _palabras(cat: dict) -> set:
    return {w.lower() for v in cat.values()
            for w in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{4,}", v)}


class TestElFixtureCorrespondeAlModelo(unittest.TestCase):
    """Un campo que el modelo no tiene CIEGA el guard, y en silencio.

    El render está lleno de `if (s.target_trust_gates)`, así que una clave
    mal escrita en el fixture no da ningún error: el bloque simplemente no
    se pinta y el test pasa en verde vigilando el vacío. Medido el
    2026-09-17, cuatro de las 41 claves del fixture de CMv4.0 no existían
    en `CMv40Session` —`trust_gates` por `target_trust_gates`,
    `target_l8_unique_combos` por `target_l8_unique_count`,
    `target_l8_neutral_pct` por `target_l8_neutral_frames_pct` y
    `target_l5_refinement`— y por eso los bloques ② y ③ de la card de
    Validaciones no se renderizaban. El usuario los leía en castellano en
    su pantalla mientras la suite decía OK.

    Este test es la red: un renombrado del modelo rompe el fixture en voz
    alta. No se comprueba al revés (que el fixture cubra los 80 campos):
    muchos no se pintan en ninguna parte y exigirlo sería ruido.
    """

    # Campos que el ENDPOINT añade al `model_dump()` y que el frontend lee
    # como si fueran de la sesión. No están en el modelo a propósito.
    DEL_ENDPOINT = {
        "plan": "lo resuelve `cmv40_strategy.resolve_plan` al servir",
        "artifacts": "lo calcula `_cmv40_scan_artifacts`",
        "estimated_size_bytes": "calculado y NO persistido (ver CLAUDE.md)",
        "audio_tracks": "la sonda se las pasa a `renderIncludedTracks` a mano",
        "subtitle_tracks": "ídem",
    }

    def test_ninguna_clave_del_fixture_falta_del_modelo(self):
        sys.path.insert(0, str(APP_DIR))
        from models import CMv40Session, Session
        fuera = []
        for nombre, fixture, modelo in (("CMv40Session", SESION_CMV40, CMv40Session),
                                        ("Session", SESION_TAB1, Session)):
            for k in sorted(fixture):
                if k in modelo.model_fields or k in self.DEL_ENDPOINT:
                    continue
                fuera.append(f"{nombre}.{k}")
        self.assertEqual(fuera, [], (
            f"\n{len(fuera)} clave(s) del fixture que el modelo no tiene. El "
            f"render las ignora con un `if`, así que el bloque no se pinta y "
            f"este fichero deja de medir nada:\n  · " + "\n  · ".join(fuera)))

    def test_cada_exencion_del_endpoint_corresponde_a_algo_real(self):
        """Una entrada que ya no se usa parece cobertura y no cubre nada."""
        usadas = set(SESION_CMV40) | set(SESION_TAB1)
        fantasma = sorted(k for k in self.DEL_ENDPOINT if k not in usadas)
        self.assertEqual(fantasma, [], f"\nsobran: {fantasma}")


@unittest.skipUnless(CHROME, "sin Chrome")
class TestLasPantallasRealesEnLosTresIdiomas(unittest.TestCase):

    CLAVE = re.compile(
        r"\b(?:tab[123]|core|ui|comun|settings|workbar|cmv40_modals|browser)"
        r"\.[a-z][a-z0-9_.]{3,}\b")
    RARO = re.compile(r"undefined|NaN|\[object |⟦")

    @classmethod
    def setUpClass(cls):
        base = APP_DIR / "static" / "i18n"
        es = json.loads((base / "es.json").read_text(encoding="utf-8"))
        en = json.loads((base / "en.json").read_text(encoding="utf-8"))
        cls.solo_es = _palabras(es) - _palabras(en)
        cls.vistas = {l: _pintar(l) for l in ("es", "en", "ca")}

    def _todo(self, idioma: str):
        """Cada pantalla, con su texto y sus atributos en una sola cadena."""
        v = self.vistas[idioma]
        for nombre, p in v["pantallas"].items():
            yield nombre, p["texto"] + " ⁞ " + " ⁞ ".join(p["atributos"])
        for nombre, txt in v["constantes"].items():
            yield f"constante·{nombre}", txt

    def test_las_pantallas_se_pintan_sin_fallar(self):
        """Una pantalla que lanza no enseña nada, y eso es lo peor que puede
        pasarle a un panel: el usuario ve un hueco, no un error."""
        for idioma in ("es", "en", "ca"):
            with self.subTest(idioma=idioma):
                self.assertEqual(self.vistas[idioma]["fallos"], {})
                self.assertEqual(self.vistas[idioma]["errores"], [])
                self.assertGreaterEqual(len(self.vistas[idioma]["pantallas"]), 15)

    def test_no_se_ve_ninguna_clave_del_catalogo(self):
        """`tab3.fase_h` en pantalla es lo primero que el usuario reportó."""
        for idioma in ("es", "en", "ca"):
            malas = []
            for nombre, txt in self._todo(idioma):
                claves = sorted(set(self.CLAVE.findall(txt)))
                if claves:
                    malas.append(f"{nombre}: {claves[:5]}")
            with self.subTest(idioma=idioma):
                self.assertEqual(malas, [], "\n  · ".join([""] + malas))

    def test_no_se_ve_ningun_undefined_ni_hueco_sin_resolver(self):
        for idioma in ("es", "en", "ca"):
            malas = [f"{n}: {sorted(set(self.RARO.findall(t)))}"
                     for n, t in self._todo(idioma) if self.RARO.search(t)]
            with self.subTest(idioma=idioma):
                self.assertEqual(malas, [], "\n  · ".join([""] + malas))

    def test_con_la_app_en_ingles_no_queda_castellano_en_pantalla(self):
        """El criterio es un dato, no una lista: una palabra de cuatro letras
        que está en el catálogo castellano y no en el inglés es castellano."""
        malas = []
        for nombre, txt in self._todo("en"):
            if nombre in EN_CASTELLANO_A_PROPOSITO:
                continue
            pal = sorted({w.lower() for w in
                          re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{4,}", txt)}
                         & self.solo_es)
            if pal:
                malas.append(f"{nombre}: {pal[:8]}")
        self.assertEqual(malas, [], (
            "\ncastellano en pantalla con la app en inglés:\n  · "
            + "\n  · ".join(malas)))

    # ── El criterio NO circular ──────────────────────────────────
    #
    # El criterio del vocabulario es CIRCULAR, y este no lo es.
    #    # «una palabra que está en el catálogo castellano y no en el inglés» solo
    # puede ver palabras que **alguna cadena ya traducida** contiene. Un
    # literal cableado que nunca pasó por el catálogo es invisible: medido el
    # 2026-09-17, `exacto`, `omitidas` y `contiguo` estaban en la pantalla
    # inglesa del usuario y ningún guard los veía. Es la misma circularidad
    # que tenía el guard de huecos.
    #    # El criterio de aquí no depende del catálogo castellano: una palabra que
    # aparece en el render INGLÉS **y** en el render CASTELLANO de la misma
    # pantalla, que ningún texto inglés del catálogo contiene y que no viene
    # del dato de la fixture, solo puede ser un literal cableado. Medido: 27
    # candidatos, 19 fugas reales y 8 identificadores, que van arriba con su
    # motivo.
    #    #

    @classmethod
    def _palabras_del_dato(cls) -> set:
        """Lo que viene de la FIXTURE no es una fuga de la app.

        Tienen que estar TODAS las fixtures: con solo dos de ellas, el
        `paso` de la columna de trabajo («Demuxing BL/EL») se denunciaba
        como si fuera un literal cableado.
        """
        crudo = {str(i): json.dumps(x, ensure_ascii=False) for i, x in enumerate(
            (SESION_CMV40, SESION_TAB1, DOVI_TAB2, TRABAJO, TRABAJO_COLA,
             RECIENTE, AJUSTES, LOOKUP_REC, LOOKUP_REPO, LOOKUP_TMDB))}
        return _palabras(crudo)

    def test_ninguna_palabra_sobrevive_al_cambio_de_idioma(self):
        base = APP_DIR / "static" / "i18n"
        srv = APP_DIR / "i18n"
        en_cat = _palabras(json.loads((base / "en.json").read_text(encoding="utf-8")))
        en_cat |= _palabras(json.loads((srv / "en.json").read_text(encoding="utf-8")))
        dato = self._palabras_del_dato()
        es = dict(self._todo("es"))
        malas = []
        for nombre, txt in self._todo("en"):
            iguales = (_palabras({"x": txt}) & _palabras({"x": es.get(nombre, "")})
                       - en_cat - dato - set(NI_TRADUCIBLE_NI_FUGA))
            if iguales:
                malas.append(f"{nombre}: {sorted(iguales)[:8]}")
        self.assertEqual(malas, [], (
            "\npalabras que NO cambian al pasar de castellano a inglés y que "
            "ningún texto inglés del catálogo contiene — o son un literal "
            "cableado, o van en NI_TRADUCIBLE_NI_FUGA con su motivo:\n  · "
            + "\n  · ".join(malas)))



# Palabras que el criterio B (abajo) señala y NO son fugas, con el motivo.
# Va por PALABRA y no por pantalla: una exención por pantalla tapa cientos de
# cadenas de golpe, que es lo que hizo la de la card de Validaciones.
NI_TRADUCIBLE_NI_FUGA = {
    # Nombres del catálogo de glifos (`GLIFOS` en core.js) y claves de objeto
    # que salen en el `JSON.stringify` de la vista previa del pipeline. Son
    # identificadores, no texto: nadie los lee en pantalla.
    "caja": "nombre de glifo", "diana": "nombre de glifo",
    "icon": "clave de objeto", "warn": "clave de objeto",
    "blurb": "clave de objeto", "autoendsat": "clave de objeto",
    # El codec de los subtítulos Blu-ray se llama así en las tres lenguas.
    "presentation": "«Presentation Graphics», el nombre del codec PGS",
    "graphics": "ídem",
    # `LANGUAGE_MAP`: los literales de pista de la spec, que acaban en el
    # nombre de las pistas del MKV. Decisión escrita en CLAUDE.md; cambia con
    # el bloque de selección de pistas, no con la traducción.
    "catal": "«Català», el nombre de la lengua en su propia lengua — el "
               "selector de ⚙︎ Configuración enseña los tres así",
    # El NOMBRE DE PISTA que acaba dentro del MKV, que es el literal de la
    # spec y describe el fichero, no la interfaz. Lo escribe
    # `phase_b._language_literal` y no cambia de idioma a propósito: verlo
    # traducido en pantalla mentiría sobre lo que lleva el MKV. El nombre
    # del idioma que sí es interfaz —el de un motivo de descarte o el chip
    # de una pista del origen— va por `phase_b.nombre_de_idioma` y por
    # `langLiteral`, y esos sí siguen el idioma.
    "castellano": "literal de pista de la spec; es el nombre DENTRO del MKV",
    "inglés": "ídem",
}


# Palabras función castellanas que **no pueden** aparecer en la pantalla
# inglesa. El criterio del vocabulario pide cuatro letras, así que un «3 de 7»
# —el paso de la columna de trabajo, vivo hasta el 2026-09-17— es invisible
# para los otros dos criterios.
#
# El conjunto está CURADO por medición, no copiado de una lista de stop-words:
# se probó con 44 palabras y las tres que dan falsos positivos se quitaron —
# «no» y «son» son inglés, y «el» es la Enhancement Layer del Dolby Vision,
# que sale en nueve pantallas. Con las 21 que quedan, la única aparición en
# toda la app era la de verdad.
FUNCION_CASTELLANA = (
    "de", "del", "los", "las", "una", "con", "por", "para", "más", "hay",
    "está", "están", "desde", "hasta", "entre", "cada", "pero", "cuando",
    "donde", "aún", "todavía",
)


@unittest.skipUnless(CHROME, "sin Chrome")
class TestNingunaPalabraFuncionCastellanaEnLaPantallaInglesa(
        unittest.TestCase):
    """El tercer criterio, y el que caza lo corto.

    Los otros dos miran palabras de cuatro letras o más: uno contra el
    vocabulario del catálogo y otro contra el render castellano. Ninguno
    puede ver «3 de 7», que es lo que la columna de trabajo enseñaba en
    inglés. Una preposición castellana en la pantalla inglesa no tiene
    ninguna lectura inocente.
    """

    @classmethod
    def setUpClass(cls):
        cls.vista = _pintar("en")

    def test_ni_una(self):
        pat = {w: re.compile(r"(?<![\w'])" + w + r"(?![\w'])", re.I)
               for w in FUNCION_CASTELLANA}
        malas = []
        for nombre, p in self.vista["pantallas"].items():
            txt = p["texto"] + " ⁞ " + " ⁞ ".join(p["atributos"])
            hits = sorted(w for w, r in pat.items() if r.search(txt))
            if hits:
                malas.append(f"{nombre}: {hits}")
        self.assertEqual(malas, [], (
            "\npalabra función castellana en la pantalla INGLESA — es un "
            "literal cableado, y los otros dos criterios no lo ven porque "
            "tiene menos de cuatro letras:\n  · " + "\n  · ".join(malas)))


class TestLaCoberturaDeLaSondaNoBaja(unittest.TestCase):
    """Los tres criterios son buenos; lo que fallaba era CUÁNTO miran.

    El 2026-09-17 el usuario reportó fallos en Tab 3 que la suite no veía, y
    el diagnóstico fue de cobertura: la sonda llamaba a 26 de las 208
    funciones que pintan marcado. Dicho así es injusto —una función cubierta
    arrastra a las que llama—, así que lo que se mide aquí es el **cierre
    transitivo**: si la sonda llama a `_cmv40GateBloque3`, el
    `_cmv40GateFilaHtml` que esa usa también se está midiendo.

    El número no es un objetivo: es un trinquete. Una función nueva que
    pinte marcado y que nadie llame desde la sonda hace fallar el test, y
    eso obliga a decidir —añadirla o escribir por qué no— en vez de
    descubrirlo cuando el usuario la lee en el otro idioma.

    Los 61 huecos de hoy están agrupados: el panel de edición de Tab 2
    (`_renderMkvEditPanel`, `_renderMkvTracks`, `_renderMkvChapterList`,
    `_renderMkvRecientes`…), el modal de creación de CMv4.0 y la ayuda
    (`_cmv40New*`, `_cmv40Help*`, `_cmv40Lookup*`) y la tabla de episodios
    de una serie (`_renderSeriesEpisodesTable`).
    """

    HUECOS_MAXIMOS = 61

    def _censo(self):
        from frontend_sources import rutas
        cuerpo, emite = {}, set()
        for r in rutas():
            if not str(r).endswith(".js"):
                continue
            src = Path(r).read_text(encoding="utf-8")
            pos = [(m.start(), m.group(1)) for m in re.finditer(
                r"^(?:async )?function ([A-Za-z_][\w]*)\s*\(", src, re.M)]
            for k, (i, fn) in enumerate(pos):
                j = pos[k + 1][0] if k + 1 < len(pos) else len(src)
                cuerpo[fn] = src[i:j]
                if re.search(r"return\s*`?\s*<|innerHTML\s*=", src[i:j]):
                    emite.add(fn)
        sonda = Path(__file__).read_text(encoding="utf-8")
        alcanzado = {fn for fn in cuerpo
                     if re.search(r"\b" + re.escape(fn) + r"\s*\(", sonda)}
        frontera = list(alcanzado)
        while frontera:
            fn = frontera.pop()
            for otra in re.findall(r"\b([A-Za-z_][\w]*)\s*\(",
                                   cuerpo.get(fn, "")):
                if otra in cuerpo and otra not in alcanzado:
                    alcanzado.add(otra)
                    frontera.append(otra)
        return emite, emite & alcanzado

    def test_no_aparece_ninguna_pantalla_nueva_sin_medir(self):
        emite, cubiertas = self._censo()
        huecos = sorted(emite - cubiertas)
        self.assertLessEqual(len(huecos), self.HUECOS_MAXIMOS, (
            f"\n{len(huecos)} funciones pintan marcado y la sonda no llega a "
            f"ellas (el tope es {self.HUECOS_MAXIMOS}). Añádela a `CASOS` o "
            f"baja el tope con el motivo:\n  · " + "\n  · ".join(huecos[:12])))

    def test_el_tope_esta_ajustado(self):
        """Un tope holgado deja entrar pantallas sin medir sin que nada
        avise. Se baja al cerrar un hueco, y este test lo obliga."""
        emite, cubiertas = self._censo()
        huecos = len(emite - cubiertas)
        self.assertEqual(huecos, self.HUECOS_MAXIMOS, (
            f"\nquedan {huecos} huecos y el tope dice {self.HUECOS_MAXIMOS}: "
            f"ajusta `HUECOS_MAXIMOS`"))


class TestLaListaDeNoTraducibleNoSeQuedaVieja(unittest.TestCase):
    """Una exención que ya no corresponde a nada parece cobertura."""

    def test_cada_palabra_sigue_apareciendo_en_el_codigo_o_en_el_dato(self):
        from frontend_sources import rutas
        fuente = " ".join(Path(r).read_text(encoding="utf-8") for r in rutas())
        fuente += json.dumps(SESION_CMV40, ensure_ascii=False)
        fuente += json.dumps(SESION_TAB1, ensure_ascii=False)
        bajo = fuente.lower()
        fantasma = sorted(w for w in NI_TRADUCIBLE_NI_FUGA if w not in bajo)
        self.assertEqual(fantasma, [], f"\nya no aparecen: {fantasma}")


if __name__ == "__main__":
    unittest.main()
