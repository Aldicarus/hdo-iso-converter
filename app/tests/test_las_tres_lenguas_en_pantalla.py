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
  "target_l8_unique_combos": 2251, "target_l8_scene_cuts": 1101,
  "target_l8_neutral_pct": 12.5, "target_preflight_ok": True,
  "awaiting_critical_ack": True,
  "critical_gate_failures": [{"gate": "l5_div", "severity": "ack_required",
                              "detail": "L5 body divergence 41.2%"}],
  "user_acknowledged_degradation": False, "pipeline_aborted": False,
  "trust_gates": [{"gate": "frames", "severity": "ok", "detail": "243552 = 243552"},
                  {"gate": "cm_version", "severity": "ok", "detail": "v4.0"},
                  {"gate": "has_l8", "severity": "ok", "detail": "present"},
                  {"gate": "l5_div", "severity": "ack_required", "detail": "41.2%"},
                  {"gate": "l6_div", "severity": "warn", "detail": "60 nits"}],
  "target_l5_refinement": {"body_divergence_pct": 41.2, "body_coverage": 0.974,
      "body_total": 190021, "body_divergent": 78288, "largest_run_frames": 8566,
      "largest_run_seconds": 357.2, "zones": {"intro": [12, 9501],
      "body": [78288, 190021], "outro": [3, 9501]}, "tgt_variable_l5": True,
      "src_variable_l5": False, "histogram": [["0/0/0/0", 111733], ["0/140/0/140", 78288]],
      "runs": [{"inicio": 10, "fin": 8576, "frames": 8566}]},
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


_SONDA = ("<script>window.__errores=[];"
          "window.addEventListener('error',e=>window.__errores.push("
          "(e.message||'')+' @ '+(e.filename||'').split('/').pop()+':'+e.lineno));"
          "window.fetch=()=>new Promise(()=>{});"
          "window.WebSocket=function(){this.close=()=>{};this.send=()=>{};};</script>")

_CUERPO = """
(function () {
  const S = %s, T1 = %s;
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
                                     S.source_dv_info, T1.bdinfo_result.video_tracks[0].hdr,
                                     T1.bdinfo_result.video_tracks[0]),
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
    cuerpo = _CUERPO % (json.dumps(SESION_CMV40), json.dumps(SESION_TAB1))
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


# El contenido de la card 🛡️ Validaciones se queda en castellano por decisión
# del usuario: es el detalle técnico de los trust gates —`cuerpo 97,4%`,
# `VARIABLE · 0,0/0,0`, los tramos del L5— que se lee contra el log del
# pipeline y contra la hoja de DoviTools, las dos en inglés. Sus CABECERAS sí
# están traducidas, que es lo que permite navegarla.
EN_CASTELLANO_A_PROPOSITO = {
    "tab3·gates_bc": "los cinco bloques del detalle de los trust gates",
    "tab3·gates_gh": "el detalle de las validaciones de Fase G/H",
}


def _palabras(cat: dict) -> set:
    return {w.lower() for v in cat.values()
            for w in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñü]{4,}", v)}


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


if __name__ == "__main__":
    unittest.main()
