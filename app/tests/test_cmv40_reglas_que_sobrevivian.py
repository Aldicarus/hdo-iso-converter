"""Reglas de CMv4.0 que sobrevivían a la suite entera mutadas.

La auditoría de la suite con Codex (2026-10-17) aplicó a la vez 38
mutaciones y los 3.117 tests siguieron en verde. Las de las fases G y H están
en `test_cmv40_fase_h_mira_el_mkv`; estas son las del resto del pipeline, cada
una con el caso que la distingue. El patrón que se repetía: los tests
comprobaban que algo PASA, rara vez la frontera en la que deja de pasar —un
umbral a 0,70 en vez de 0,85, o 300 nits en vez de 200, daba el mismo verde—,
o comparaban `abs()` donde el signo es el dato.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_reglas_que_sobrevivian -v
"""
import math
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import PhaseTestCase, RpuProps, make_session, write_artifacts  # noqa: E402
from models import DoviInfo, L8Combo  # noqa: E402
from phases import cmv40_pipeline as pipeline  # noqa: E402
from phases.cmv40_pipeline import (  # noqa: E402
    L5_NEUTRO, _comparar_l5, _evaluate_trust_gates, _veredicto_l5,
    compute_sync_confidence, detect_sync_offset, evaluate_sync_gate,
)

FRAMES = 1000


def _curva(n, seed=0):
    return [int(400 + 380 * math.sin((i + seed) / 37.0)
                    + 300 * math.sin((i + seed) / 7.3)) for i in range(n)]


def _pfd(src, tgt):
    n = max(len(src), len(tgt))
    return {"data": [{"frame": i,
                      "src_maxcll": src[i] if i < len(src) else 0,
                      "tgt_maxcll": tgt[i] if i < len(tgt) else 0}
                     for i in range(n)],
            "source_frames": len(src), "target_frames": len(tgt)}


# ── Fase D: el criterio para avanzar ────────────────────────────────────

class TestElUmbralDeConfianzaEsEl85(unittest.TestCase):

    def test_una_correlacion_de_080_no_basta(self):
        src = _curva(2000)
        ruido = _curva(2000, seed=500)
        tgt = [int(s + 1.5 * r) for s, r in zip(src, ruido)]
        r = compute_sync_confidence(_pfd(src, tgt))
        self.assertTrue(0.70 <= r["pearson"] < 0.85,
                        f"el fixture no cae entre 0,70 y 0,85: {r['pearson']}")
        self.assertFalse(r["threshold_ok"])

    def test_identicas_pasan(self):
        src = _curva(2000)
        self.assertTrue(compute_sync_confidence(_pfd(src, list(src)))["threshold_ok"])


class TestUnFrameDeDesfaseNoSeConfirma(unittest.TestCase):

    def test_delta_1(self):
        src = _curva(2000)
        g = evaluate_sync_gate(_pfd(src, list(src)), sync_delta=1)
        self.assertFalse(g["ok"])

    def test_delta_0(self):
        src = _curva(2000)
        self.assertTrue(evaluate_sync_gate(_pfd(src, list(src)), sync_delta=0)["ok"])


class TestElSignoDelDesfase(unittest.TestCase):
    """El signo ES el dato: dice qué serie lleva los frames de más. Los tests
    de `test_cmv40_sync_offset` comparaban `abs(offset)` y aguantaban que se
    invirtiera. CLAUDE.md: un logo de 72 frames en el BD da −72."""

    OFFSET = 72

    def test_frames_de_mas_en_el_bd_son_negativos(self):
        contenido = _curva(3000)
        src = [900] * self.OFFSET + contenido
        tgt = contenido + [900] * self.OFFSET
        self.assertEqual(detect_sync_offset(_pfd(src, tgt))["offset"], -self.OFFSET)

    def test_frames_de_mas_en_el_bin_son_positivos(self):
        contenido = _curva(3000)
        src = contenido + [900] * self.OFFSET
        tgt = [900] * self.OFFSET + contenido
        self.assertEqual(detect_sync_offset(_pfd(src, tgt))["offset"], self.OFFSET)


class TestUnaSolaSeriePlanaNoSeMide(unittest.TestCase):
    """Con una de las dos sin variación todos los desfases empatan y ganaría
    el primero: no se puede medir, y se dice."""

    def test_el_bin_plano(self):
        r = detect_sync_offset(_pfd(_curva(3000), [500] * 3000))
        self.assertEqual(r["confidence"], 0)
        self.assertEqual(r["offset"], 0)

    def test_el_bd_plano(self):
        r = detect_sync_offset(_pfd([500] * 3000, _curva(3000)))
        self.assertEqual(r["confidence"], 0)


# ── Gates de la Fase B ──────────────────────────────────────────────────

def _gates(**tgt):
    base = dict(profile=7, el_type="FEL", rpu_present=True, l1_max_cll=1000.0,
                l6_max_cll=1000, l5_top=276, l5_bottom=276, l5_left=0, l5_right=0)
    src = DoviInfo(cm_version="v2.9", **base)
    tgt_info = DoviInfo(cm_version="v4.0", has_l8=True, **dict(base, **tgt))
    gates, _ = _evaluate_trust_gates(src, tgt_info, FRAMES, FRAMES)
    return gates


class TestLasFronterasDeLosGates(unittest.TestCase):

    def test_l6_a_250_nits_pide_ack(self):
        self.assertEqual(_gates(l6_max_cll=1250)["l6_div"]["severity"], "ack_required")

    def test_l6_a_200_nits_es_un_aviso(self):
        self.assertEqual(_gates(l6_max_cll=1200)["l6_div"]["severity"], "warn")

    def test_l1_a_un_30_por_ciento_pide_ack(self):
        g = _gates(l1_max_cll=700.0)["l1_div"]
        self.assertEqual(g["severity"], "ack_required")
        self.assertAlmostEqual(g["pct_diff"], 30.0)

    def test_l1_a_un_10_por_ciento_es_un_aviso(self):
        self.assertEqual(_gates(l1_max_cll=900.0)["l1_div"]["severity"], "warn")


class TestElL5FrameAFrame(unittest.TestCase):

    def test_cincuenta_pixeles_divergen(self):
        """El umbral de un frame divergente son 30 px."""
        src = {f: (276, 276, 0, 0) for f in range(1000)}
        tgt = {f: (226, 226, 0, 0) for f in range(1000)}
        cmp = _comparar_l5(src, tgt, 1000, 24.0)
        self.assertLess(cmp["body_coverage"], 0.01, cmp["body_coverage"])

    def test_veinte_pixeles_no(self):
        src = {f: (276, 276, 0, 0) for f in range(1000)}
        tgt = {f: (256, 256, 0, 0) for f in range(1000)}
        self.assertGreater(_comparar_l5(src, tgt, 1000, 24.0)["body_coverage"], 0.99)

    def test_un_cuerpo_que_coincide_en_un_60_por_ciento_pide_ack(self):
        """Por debajo del 70 % de cobertura: aunque ningún tramo llegue a los
        40 s, son cientos de transiciones con otro encuadre."""
        total, fps = 20000, 24.0
        src = {f: L5_NEUTRO for f in range(total)}
        tgt = {f: ((140, 140, 0, 0) if (f % 250) < 100 else L5_NEUTRO)
               for f in range(total)}
        cmp = _comparar_l5(src, tgt, total, fps)
        self.assertTrue(0.5 < cmp["body_coverage"] < 0.7, cmp["body_coverage"])
        self.assertLess(cmp["segundos_cuerpo_max"], 40)
        severidad, ok, _ = _veredicto_l5(cmp)
        self.assertEqual((severidad, ok), ("ack_required", False))


# ── L8: lo que decide si un bin trae trabajo de colorista ───────────────

class TestLaMagnitudDelL8(unittest.TestCase):

    @staticmethod
    def _combo(**trims):
        base = dict(target_display_index=1, trim_slope=2048, trim_offset=2048,
                    trim_power=2048, trim_chroma_weight=2048,
                    trim_saturation_gain=2048, ms_weight=0)
        base.update(trims)
        return L8Combo(**base)

    def test_el_chroma_weight_cuenta(self):
        from phases.rpu_analyze import _l8_delta_maxima
        self.assertEqual(_l8_delta_maxima([self._combo(trim_chroma_weight=2168)]), 120)

    def test_cincuenta_exactos_no_son_trabajo_de_colorista(self):
        """El criterio es `maxΔ > 50`: cincuenta es aún el neutro."""
        from phases.rpu_analyze import RpuAnalysis, _clasificacion_de_l8
        self.assertNotEqual(_clasificacion_de_l8(RpuAnalysis(l8_max_delta=50)), "real")
        self.assertEqual(_clasificacion_de_l8(RpuAnalysis(l8_max_delta=51)), "real")


# ── Fases A, B, C, E y F, ejecutadas ────────────────────────────────────

SRC_FEL = RpuProps(profile=7, el_type="FEL", cm_version="v2.9", frames=FRAMES)
SRC_P8 = RpuProps(profile=8, el_type="", cm_version="v2.9", frames=FRAMES)
TGT_FEL = RpuProps(profile=7, el_type="FEL", cm_version="v4.0", frames=FRAMES, has_l8=True)
TGT_P8 = RpuProps(profile=8, el_type="", cm_version="v4.0", frames=FRAMES, has_l8=True)


class FaseCase(PhaseTestCase):

    def preparar(self, *, workflow="p7_fel", target_type="generic", trust=False,
                 source=SRC_FEL, target=TGT_FEL, artefactos=()):
        self.tb.define_rpu("RPU_source.bin", **source.as_dict())
        self.tb.define_rpu("RPU_target.bin", **target.as_dict())
        write_artifacts(self.wd, "RPU_source.bin", props=source)
        write_artifacts(self.wd, "RPU_target.bin", props=target)
        for n in artefactos:
            write_artifacts(self.wd, n, props=source)
        s = make_session(self.wd, source_workflow=workflow, target_type=target_type,
                         target_trust_ok=trust, source_frame_count=FRAMES,
                         target_frame_count=FRAMES)
        write_artifacts(self.wd, "source.mkv", props=source)
        self.tb.define_media("source.mkv", duration=7200.0, frames=FRAMES)
        return s

    def editor_del_merge(self):
        [c] = [c for c in self.tb.find("dovi_tool", "editor")
               if c.json_args.get("allow_cmv4_transfer")]
        return c.json_args


class TestElMergeUsaElBinCorregido(FaseCase):

    async def test_con_rpu_synced_el_donante_es_el_corregido(self):
        """Corregir el sync en la Fase D/E y mezclar después el bin SIN
        corregir anula la corrección sin que nada falle."""
        from phases.cmv40_pipeline import run_phase_f_inject
        s = self.preparar(artefactos=("BL.hevc", "EL.hevc"))
        write_artifacts(self.wd, "RPU_synced.bin", props=TGT_FEL)
        self.tb.define_rpu("RPU_synced.bin", **TGT_FEL.as_dict())
        await run_phase_f_inject(s, self.log)
        self.assertTrue(self.editor_del_merge()["source_rpu"].endswith("RPU_synced.bin"),
                        self.editor_del_merge()["source_rpu"])

    async def test_un_source_p8_conserva_su_l1_l2_y_l6(self):
        """Solo un source FEL recibe L1/L2/L6 del bin; un P8 no tiene capa
        de mejora y su L1 describe sus propios píxeles."""
        from phases.cmv40_pipeline import run_phase_f_inject
        s = self.preparar(workflow="p8", target_type="generic",
                          source=SRC_P8, target=TGT_P8, artefactos=("source.hevc",))
        await run_phase_f_inject(s, self.log)
        self.assertEqual(self.editor_del_merge()["rpu_levels"], [3, 8, 9, 11, 254])


class TestElProfile8MiraElRpuQueSeInyecta(FaseCase):

    async def test_convierte_el_mezclado_aunque_el_bin_ya_sea_p8(self):
        """`_ensure_profile8_rpu` decide con el RPU que recibe. Preguntándole
        al bin, un merge que hereda el Profile 7 del source salía sin
        convertir: el MKV se anunciaba dual-layer sin capa de mejora."""
        from phases.cmv40_pipeline import _ensure_profile8_rpu
        write_artifacts(self.wd, "RPU_target.bin", props=TGT_P8)
        self.tb.define_rpu("RPU_target.bin", **TGT_P8.as_dict())
        mezclado = RpuProps(profile=7, el_type="MEL", cm_version="v4.0",
                            frames=FRAMES, has_l8=True)
        write_artifacts(self.wd, "RPU_merged.bin", props=mezclado)
        self.tb.define_rpu("RPU_merged.bin", **mezclado.as_dict())
        sale = await _ensure_profile8_rpu(self.wd / "RPU_merged.bin", self.wd, self.log)
        self.assertTrue(sale.name.endswith("_p81.bin"), sale.name)


class TestLaFaseEEscribeBien(FaseCase):

    async def test_no_deja_el_temporal(self):
        """El RPU corregido se escribe en un temporal y se SUSTITUYE: copiarlo
        dejaba el temporal al lado y no era atómico."""
        from phases.cmv40_pipeline import run_phase_e_correct_sync
        s = self.preparar()
        await run_phase_e_correct_sync(s, {"remove": ["0-9"]}, self.log)
        self.assertTrue((self.wd / "RPU_synced.bin").exists())
        self.assertFalse((self.wd / "RPU_synced.tmp.bin").exists())

    async def test_duplicar_al_final_copia_el_ultimo_frame(self):
        from phases.cmv40_pipeline import run_phase_e_correct_sync
        s = self.preparar()
        await run_phase_e_correct_sync(
            s, {"duplicate": [{"source": FRAMES - 1, "offset": FRAMES, "length": 5}]},
            self.log)
        [c] = self.tb.find("dovi_tool", "editor")
        self.assertEqual(c.json_args["duplicate"][0]["source"], FRAMES - 1)


class TestLaFaseBPideElAck(FaseCase):

    async def test_un_l6_divergente_deja_el_proyecto_esperando_la_confirmacion(self):
        from phases.cmv40_pipeline import _analyze_target_rpu
        tgt = RpuProps(profile=7, el_type="FEL", cm_version="v4.0", frames=FRAMES,
                       has_l8=True, l6_max_cll=4000, l5=(276, 276, 0, 0))
        self.tb.define_rpu("RPU_target.bin", **tgt.as_dict())
        write_artifacts(self.wd, "RPU_target.bin", props=tgt)
        s = make_session(self.wd, target_type="", target_trust_ok=False,
                         source_dv_info=DoviInfo(profile=7, el_type="FEL",
                                                 cm_version="v2.9", l6_max_cll=1000,
                                                 l1_max_cll=1000.6, l5_top=276,
                                                 l5_bottom=276))
        await _analyze_target_rpu(s, self.wd / "RPU_target.bin", self.log)
        self.assertTrue(s.awaiting_critical_ack)
        self.assertIn("l6_div", [f["gate"] for f in s.critical_gate_failures])


class TestLaFaseAMiraElNombreAntesDeGastar(FaseCase):

    async def test_un_nombre_ocupado_para_antes_del_ffmpeg(self):
        """La Fase H se niega a sobrescribir, y se enteraba al final: un job
        real gastó 40 minutos para morir con «Ya existe un MKV». Lo vigilaba
        un test que buscaba la llamada en el fuente, y seguía en verde con
        un `if False:` delante."""
        from phases.cmv40_pipeline import run_phase_a_analyze_source
        s = self.preparar()
        (self.output_dir / s.output_mkv_name).write_bytes(b"\x00" * 64)
        with self.assertRaises(RuntimeError):
            await run_phase_a_analyze_source(s, log_callback=self.log)
        self.assertFalse(self.tb.ran("ffmpeg"), "ha empezado a extraer igualmente")


class TestElDemuxNoLlevaUnTimeoutFijoPequeno(FaseCase):

    async def test_el_timeout_del_demux_escala(self):
        """Proyecto Salvación: demux estimado ~990 s contra un tope de 900 s.
        El guard de `test_pipeline_timeouts` lee el fuente y aguantaba un
        `min(timeout, 300)` añadido detrás: aquí se mira el timeout con el
        que se lanza el proceso."""
        from phases.cmv40_pipeline import run_phase_c_extract
        s = self.preparar(artefactos=("source.hevc",))
        real = pipeline._run_with_time_estimate
        vistos = []

        async def _espia(cmd, *a, **kw):
            if "demux" in cmd:
                vistos.append(kw.get("timeout"))
            return await real(cmd, *a, **kw)

        with mock.patch.object(pipeline, "_run_with_time_estimate", _espia):
            await run_phase_c_extract(s, self.log)
        self.assertTrue(vistos, "no se ha lanzado ningún demux")
        self.assertGreaterEqual(min(vistos), 1800)


if __name__ == "__main__":
    unittest.main()
