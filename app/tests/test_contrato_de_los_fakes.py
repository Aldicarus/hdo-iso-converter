"""Contrato de los binarios falsos con las herramientas de verdad.

La regla de los fakes fieles —«antes de escribir un binario falso, comprobar
el formato real»— se cumplía a mano y una vez: lo que el fake emitía se
copiaba de una salida real y a partir de ahí nadie volvía a mirarla. La
auditoría de la suite del 2026-10-17 encontró seis casos donde el fake
inventaba en vez de copiar, y con la captura de `fixtures_contrato/` salieron
cuatro más que ningún test podía ver:

  · `ffprobe … stream=nb_frames` sobre un MKV real devuelve `N/A,` —en JSON
    la clave ni aparece— y el fake devolvía el número. La comprobación
    estricta de frames de la Fase H solo existía en los tests.
  · el export por niveles no emitía NUNCA filas de `level2`, sacaba `level8`
    vacío aunque el summary del mismo RPU dijera «L8 trims», y a `level6` le
    faltaba `min_display_mastering_luminance`;
  · y el summary de un P8 dice `Profile: 8`, no `Profile: 8.1`.

Este módulo fija el contrato contra las salidas CAPTURADAS: lo que el fake
emite tiene que tener la forma de lo que emite el real, y el parser de
producción tiene que sacar lo mismo de las dos. Las capturas salen de
`app/tools/capturar_contrato.py` y son de las versiones del Dockerfile
(dovi_tool 2.3.3, mkvmerge 88, MediaInfo 21.09, ffmpeg n7.1.5): al subir una,
se recaptura.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_contrato_de_los_fakes -v
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
APP_DIR = TESTS.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TESTS))

from cmv40_harness import FakeToolbox, RpuProps, write_artifacts  # noqa: E402

FIX = TESTS / "fixtures_contrato"
REAL = json.loads((FIX / "herramientas_reales.json").read_text(encoding="utf-8"))
J_REAL = json.loads((FIX / "mkvmerge_J_mkv.json").read_text(encoding="utf-8"))
MI_REAL = json.loads((FIX / "mediainfo_mkv.json").read_text(encoding="utf-8"))


def forma(linea: str, *, decimales: bool = False) -> str:
    """La forma de una línea: sin números, con los espacios colapsados (el
    real alinea columnas: `Lsize=   20614KiB`) y con una lista de targets
    reducida a uno (el real de Supergirl trae un target L8 y el fake cuatro:
    eso es un dato, no un formato).

    Con `decimales`, `8.1` y `8` NO son la misma forma: en el summary el
    número de decimales es formato (`Profile: 8` frente a `Profile: 8.1`, que
    es lo que escribía el fake); en ffmpeg no, porque `fps=` lleva un decimal
    o ninguno según el valor."""
    s = re.sub(r"\d+", "N", linea) if decimales else re.sub(r"\d+(\.\d+)?", "N", linea)
    s = re.sub(r"\b(FEL|MEL)\b", "XEL", s)
    s = re.sub(r"=\s+", "=", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"N nits(, N nits)+", "N nits", s)


class ConFakes(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="contrato_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp / "tb").install()
        self.addCleanup(self.tb.uninstall)

    def correr(self, *argv) -> subprocess.CompletedProcess:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=60)


class TestElSummaryDeDoviTool(ConFakes):

    CASOS = {
        # fichero capturado → las props con las que el fake lo describe
        "RPU_source.bin": dict(profile=7, el_type="MEL", cm_version="v2.9", has_l8=False),
        "RPU_target.bin": dict(profile=7, el_type="MEL", cm_version="v4.0", has_l8=True),
        "RPU_merged_p81.bin": dict(profile=8, el_type="", cm_version="v4.0", has_l8=True),
    }
    COMUNES = dict(frames=155506, scenes=2414, l1_max_cll=1000.6, l1_max_fall=138.55,
                   l6_max_cll=371, l6_max_fall=162, l5=(276, 276, 0, 0))

    def _parse(self, texto):
        from phases.phase_a import _parse_dovi_summary
        d = _parse_dovi_summary(texto).model_dump()
        d.pop("raw_summary")
        return d

    def test_el_parser_lee_el_formato_real(self):
        """Lo que el pipeline saca de cada summary real, fijado: si una
        versión nueva de dovi_tool cambia el formato, esto lo dice antes que
        el NAS."""
        esperado = {
            "RPU_source.bin": (7, "MEL", "v2.9", False),
            "RPU_target.bin": (7, "MEL", "v4.0", True),
            "RPU_merged_p81.bin": (8, "", "v4.0", True),
        }
        for nombre, (perfil, el, cm, l8) in esperado.items():
            with self.subTest(nombre):
                d = self._parse(REAL["dovi_summary"][nombre])
                self.assertEqual((d["profile"], d["el_type"], d["cm_version"], d["has_l8"]),
                                 (perfil, el, cm, l8))
                self.assertEqual(d["frame_count"], 155506)
                self.assertEqual(d["scene_count"], 2414)
                self.assertEqual((d["l5_top"], d["l5_bottom"]), (276, 276))
                self.assertEqual((d["l6_max_cll"], d["l6_max_fall"]), (371, 162))
                self.assertAlmostEqual(d["l1_max_cll"], 1000.6)

    def _summaries_del_fake(self, props):
        """Los DOS pintores del summary: `RpuProps.to_summary` (lo que el
        arnés escribe en los artefactos) y el `dovi_tool info` del binario."""
        rpu = self.tmp / "x.bin"
        write_artifacts(self.tmp, rpu.name, props=RpuProps(**props))
        r = self.correr("dovi_tool", "info", "-i", str(rpu), "--summary")
        self.assertEqual(r.returncode, 0, r.stderr)
        return {"to_summary": RpuProps(**props).to_summary(), "binario": r.stdout}

    def test_cada_linea_del_fake_tiene_una_forma_que_el_real_escribe(self):
        reales = {forma(l, decimales=True)
                  for s in REAL["dovi_summary"].values() for l in s.splitlines()}
        for nombre, props in self.CASOS.items():
            for pintor, texto in self._summaries_del_fake({**self.COMUNES, **props}).items():
                with self.subTest(nombre, pintor=pintor):
                    ajenas = [l for l in texto.splitlines()
                              if forma(l, decimales=True) not in reales]
                    self.assertEqual(ajenas, [], "líneas que dovi_tool no escribe")

    def test_el_parser_saca_lo_mismo_del_fake_que_del_real(self):
        for nombre, props in self.CASOS.items():
            real = self._parse(REAL["dovi_summary"][nombre])
            for pintor, texto in self._summaries_del_fake({**self.COMUNES, **props}).items():
                fake = self._parse(texto)
                # La lista de targets L8 es un dato del RPU (uno en Supergirl,
                # cuatro en el fake), no un formato.
                for k in ("l8_trim_nits", "l8_trim_count"):
                    real.pop(k, None), fake.pop(k, None)
                with self.subTest(nombre, pintor=pintor):
                    self.assertEqual({k: v for k, v in fake.items() if real[k] != v}, {},
                                     "campos que el parser lee distinto")


class TestElRecuentoDeFramesDeFfprobe(ConFakes):

    def test_la_etiqueta_de_mkvmerge_es_el_recuento_exacto(self):
        from phases.cmv40_pipeline import _frames_de_ffprobe
        j = REAL["ffprobe_frames_json"]
        self.assertEqual(_frames_de_ffprobe(json.dumps(j["mkv_tab3_p8"])), 155506)
        self.assertEqual(_frames_de_ffprobe(json.dumps(j["mkv_tab1_fel"])), 181037)
        self.assertIsNone(_frames_de_ffprobe(json.dumps(j["hevc_crudo"])))

    def test_la_etiqueta_con_idioma_tambien_cuenta(self):
        """Con la pista en un idioma distinto de `und`, ffmpeg pone el sufijo:
        `NUMBER_OF_FRAMES-eng`."""
        from phases.cmv40_pipeline import _frames_de_ffprobe
        st = {"streams": [{"tags": {"NUMBER_OF_FRAMES-eng": "1234"}}]}
        self.assertEqual(_frames_de_ffprobe(json.dumps(st)), 1234)

    def test_basura_no_es_un_recuento(self):
        from phases.cmv40_pipeline import _frames_de_ffprobe
        for salida in ("", "N/A,\n", "{}", '{"streams": []}',
                       '{"streams": [{"tags": {"NUMBER_OF_FRAMES": "N/A"}}]}',
                       '{"streams": [{"nb_frames": "0"}]}'):
            with self.subTest(salida):
                self.assertIsNone(_frames_de_ffprobe(salida))

    def test_el_fake_responde_con_la_forma_real(self):
        from phases.cmv40_pipeline import _frames_de_ffprobe
        mkv, hevc = self.tmp / "Peli.mkv", self.tmp / "BL.hevc"
        for f in (mkv, hevc):
            f.write_bytes(b"x")
        self.tb.define_media(mkv.name, frames=155506)
        self.tb.define_media(hevc.name, frames=155506)
        args = ("-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=nb_frames:stream_tags", "-of", "json")
        j_mkv = json.loads(self.correr("ffprobe", *args, str(mkv)).stdout)
        j_hevc = json.loads(self.correr("ffprobe", *args, str(hevc)).stdout)
        real = REAL["ffprobe_frames_json"]
        self.assertEqual(set(j_mkv), set(real["mkv_tab3_p8"]))
        self.assertLessEqual(set(j_mkv["streams"][0]), set(real["mkv_tab3_p8"]["streams"][0]))
        self.assertNotIn("nb_frames", j_mkv["streams"][0], "un MKV real no trae nb_frames")
        self.assertEqual(_frames_de_ffprobe(json.dumps(j_mkv)), 155506)
        self.assertEqual(j_hevc, real["hevc_crudo"])
        csv = ("-v", "error", "-select_streams", "v:0", "-show_entries", "stream=nb_frames",
               "-of", "csv=p=0")
        self.assertEqual(self.correr("ffprobe", *csv, str(mkv)).stdout, REAL["ffprobe_nb_frames_csv"])
        self.assertEqual(self.correr("ffprobe", *csv, str(hevc)).stdout,
                         REAL["ffprobe_nb_frames_csv_hevc"])
        dur = self.correr("ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(mkv)).stdout
        self.assertEqual(forma(dur), forma(REAL["ffprobe_duration_csv"]))


class TestMkvmergeIdentify(ConFakes):

    def _tipos(self, d):
        return {k: type(v).__name__ for k, v in d.items()}

    def test_lo_que_el_fake_emite_existe_en_el_real_con_su_tipo(self):
        mkv = self.tmp / "Peli.mkv"
        mkv.write_bytes(b"x")
        self.tb.define_mkv(mkv.name)
        fake = json.loads(self.correr("mkvmerge", "-J", str(mkv)).stdout)
        self.assertLessEqual(set(fake), set(J_REAL))
        cr, cf = (self._tipos(x["container"]["properties"]) for x in (J_REAL, fake))
        self.assertEqual({k: v for k, v in cf.items() if cr.get(k) != v}, {})
        for tipo in ("video", "audio", "subtitles"):
            r = next(t for t in J_REAL["tracks"] if t["type"] == tipo)
            f = next(t for t in fake["tracks"] if t["type"] == tipo)
            with self.subTest(tipo):
                self.assertLessEqual(set(f), set(r))
                pr, pf = self._tipos(r["properties"]), self._tipos(f["properties"])
                self.assertEqual({k: v for k, v in pf.items() if pr.get(k) != v}, {},
                                 "propiedades que el real no emite, o con otro tipo")


class TestMediaInfo(ConFakes):

    #: Lo que el fake emite y este MKV real no, con su motivo.
    SOLO_EN_OTRO_CONTENEDOR = {
        ("Text", "Width"): "la resolución del PGS: MediaInfo la da en un m2ts, que es donde la lee la Fase A",
        ("Text", "Height"): "ídem",
    }

    def test_lo_que_el_fake_emite_tiene_la_forma_real(self):
        mkv = self.tmp / "Peli.mkv"
        mkv.write_bytes(b"x")
        self.tb.define_mediainfo(mkv.name)
        fake = json.loads(self.correr("mediainfo", "--Output=JSON", str(mkv)).stdout)
        for tipo in ("General", "Video", "Audio", "Text"):
            r = next(t for t in MI_REAL["media"]["track"] if t["@type"] == tipo)
            f = next(t for t in fake["media"]["track"] if t["@type"] == tipo)
            with self.subTest(tipo):
                ajenas = [k for k in f if k not in r and (tipo, k) not in self.SOLO_EN_OTRO_CONTENEDOR]
                self.assertEqual(ajenas, [])
                self.assertEqual({k: (r[k], f[k]) for k in f if k in r and forma(str(r[k])) != forma(str(f[k]))},
                                 {}, "valores con otra forma (la unidad, sobre todo)")

    def test_el_parser_de_nits_lee_el_valor_real(self):
        from phases.phase_a import nits_de_mediainfo
        v = next(t for t in MI_REAL["media"]["track"] if t["@type"] == "Video")
        self.assertEqual((nits_de_mediainfo(v["MaxCLL"]), nits_de_mediainfo(v["MaxFALL"])), (371, 162))

    def test_las_excepciones_siguen_haciendo_falta(self):
        mkv = self.tmp / "Peli.mkv"
        mkv.write_bytes(b"x")
        self.tb.define_mediainfo(mkv.name)
        fake = json.loads(self.correr("mediainfo", "--Output=JSON", str(mkv)).stdout)
        for tipo, k in self.SOLO_EN_OTRO_CONTENEDOR:
            f = next(t for t in fake["media"]["track"] if t["@type"] == tipo)
            self.assertIn(k, f, f"la excepción {tipo}.{k} ya no corresponde a nada")


class TestElProgresoDeFfmpeg(ConFakes):

    def test_el_real_y_el_fake_dan_lo_mismo_a_los_parsers(self):
        from phases.cmv40_pipeline import _FFMPEG_SIZE_RE, _FFMPEG_TIME_RE
        [real] = REAL["ffmpeg_lineas_de_progreso"]
        self.assertEqual(_FFMPEG_SIZE_RE.search(real).groups(), ("20614", "KiB"))
        self.assertEqual(_FFMPEG_TIME_RE.search(real).groups(), ("00", "00", "04.04"))

        mkv = self.tmp / "Peli.mkv"
        write_artifacts(self.tmp, mkv.name, props=RpuProps(profile=8, el_type="", cm_version="v4.0"))
        r = self.correr("ffmpeg", "-nostdin", "-hide_banner", "-i", str(mkv), "-map", "0:v:0",
                        "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb", "-t", "4", "-f", "hevc",
                        "-y", str(self.tmp / "o.hevc"))
        lineas = [l for l in r.stderr.splitlines() if l.startswith("frame=")]
        self.assertTrue(lineas, r.stderr)
        self.assertEqual(forma(lineas[-1]), forma(real), "la última línea (Lsize=) no tiene la forma real")
        for l in lineas:
            with self.subTest(l):
                self.assertTrue(_FFMPEG_SIZE_RE.search(l) and _FFMPEG_TIME_RE.search(l))


class TestElExportPorNiveles(ConFakes):

    NIVELES = ("level1", "level2", "level3", "level5", "level6", "level8", "level9", "level11")

    def _export(self, **niveles):
        rpu = self.tmp / "RPU_target.bin"
        write_artifacts(self.tmp, rpu.name, props=RpuProps(
            profile=7, el_type="MEL", cm_version="v4.0", frames=50, scenes=5, has_l8=True))
        if niveles:
            self.tb.define_rpu_levels(rpu.name, **niveles)
        out = self.tmp / "lv"
        out.mkdir(exist_ok=True)
        r = self.correr("dovi_tool", "export", "-i", str(rpu), "-f", "json",
                        "-d", f"scenes={out}/scenes.txt", "--levels",
                        ",".join(f"{lv}={out}/{lv}.json" for lv in self.NIVELES))
        self.assertEqual(r.returncode, 0, r.stderr)
        filas = {lv: json.loads((out / f"{lv}.json").read_text()) for lv in self.NIVELES}
        return filas, (out / "scenes.txt").read_text()

    def test_cada_nivel_con_las_claves_y_tipos_del_real(self):
        filas, _ = self._export(l3_combos=3, l9_primary=0, l11_content_type=1)
        reales = REAL["dovi_export_levels"]["ficheros"]
        for lv in self.NIVELES:
            real = reales[lv]["primeros"][0]
            with self.subTest(lv):
                self.assertTrue(filas[lv], f"el fake no emite ninguna fila de {lv}")
                f = filas[lv][0]
                self.assertEqual(set(f), set(real))
                self.assertEqual({k: type(v).__name__ for k, v in f.items()},
                                 {k: type(v).__name__ for k, v in real.items()})

    def test_un_rpu_con_l8_trae_filas_de_l8_y_de_l2_sin_declararlas(self):
        """El summary del mismo RPU dice «L2 trims» y «L8 trims»: un export
        vacío lo contradecía, y los combos L2 salían vacíos en toda la suite."""
        filas, _ = self._export()
        self.assertTrue(filas["level8"])
        self.assertTrue(filas["level2"])
        self.assertEqual(filas["level6"][0]["min_display_mastering_luminance"], 1)

    def test_el_export_y_el_summary_del_mismo_rpu_coinciden(self):
        """En el real salen del mismo RPU: Supergirl da L5 276/276 y L6
        371/162 en las dos salidas. El export del fake tenía L5 y L6 fijos,
        así que un RPU declarado con otros valores se contradecía consigo
        mismo (revisión de Codex del 2026-10-17)."""
        from phases.phase_a import _parse_dovi_summary
        real_l5 = REAL["dovi_export_levels"]["ficheros"]["level5"]["primeros"][0]
        real_l6 = REAL["dovi_export_levels"]["ficheros"]["level6"]["primeros"][0]
        real_s = _parse_dovi_summary(REAL["dovi_summary"]["RPU_target.bin"])
        self.assertEqual((real_l5["active_area_top_offset"], real_l6["max_content_light_level"]),
                         (real_s.l5_top, real_s.l6_max_cll))
        for l5, l6 in (((276, 276, 0, 0), (371, 162)), ((0, 0, 0, 0), (1000, 400)),
                       ((20, 22, 0, 0), (4000, 1200))):
            with self.subTest(l5=l5, l6=l6):
                rpu = self.tmp / "R.bin"
                props = RpuProps(profile=7, el_type="MEL", cm_version="v4.0", frames=20,
                                 has_l8=True, l5=l5, l6_max_cll=l6[0], l6_max_fall=l6[1])
                write_artifacts(self.tmp, rpu.name, props=props)
                s = _parse_dovi_summary(self.correr("dovi_tool", "info", "-i", str(rpu),
                                                    "--summary").stdout)
                out = self.tmp / "x"
                out.mkdir(exist_ok=True)
                self.correr("dovi_tool", "export", "-i", str(rpu), "-f", "json", "--levels",
                            f"level5={out}/l5.json,level6={out}/l6.json")
                f5 = json.loads((out / "l5.json").read_text())[0]
                f6 = json.loads((out / "l6.json").read_text())[0]
                self.assertEqual((f5["active_area_top_offset"], f5["active_area_bottom_offset"],
                                  f5["active_area_left_offset"], f5["active_area_right_offset"]),
                                 (s.l5_top, s.l5_bottom, s.l5_left, s.l5_right))
                self.assertEqual((f6["max_content_light_level"], f6["max_frame_average_light_level"]),
                                 (s.l6_max_cll, s.l6_max_fall))

    def test_las_escenas_son_un_frame_por_linea(self):
        _, escenas = self._export()
        real = REAL["dovi_export_levels"]["ficheros"]["scenes"]["primeras"]
        self.assertTrue(all(l.isdigit() for l in real))
        self.assertTrue(escenas.splitlines() and all(l.isdigit() for l in escenas.splitlines()))


class TestLasCapturasSonDeLasVersionesDelDockerfile(unittest.TestCase):
    """Un contrato con otra versión no es un contrato: si el Dockerfile sube
    una herramienta, hay que recapturar."""

    def test_dovi_tool(self):
        df = (APP_DIR.parent / "docker" / "Dockerfile").read_text(encoding="utf-8")
        m = re.search(r"DOVI_TOOL_VERSION=([\d.]+)", df)
        self.assertIsNotNone(m)
        self.assertIn(m.group(1), REAL["versiones"]["dovi_tool"])

    def test_ffmpeg(self):
        df = (APP_DIR.parent / "docker" / "Dockerfile").read_text(encoding="utf-8")
        m = re.search(r"FFMPEG_BUILD=ffmpeg-(n[\d.]+-\d+-g[0-9a-f]+)", df)
        self.assertIsNotNone(m, "no se encuentra el pin de ffmpeg en el Dockerfile")
        self.assertIn(m.group(1), REAL["versiones"]["ffmpeg"])
        self.assertIn(m.group(1), REAL["versiones"]["ffprobe"])


if __name__ == "__main__":
    unittest.main()
