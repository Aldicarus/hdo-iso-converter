"""Los binarios falsos respetan las opciones que cambian el resultado.

La auditoría completa con Codex del 2026-10-17 midió que la suite no veía
casi nada fuera del catálogo de mutaciones: de 80 mutaciones nuevas
sobrevivían 79. La causa común eran opciones que los fakes IGNORABAN:

  · `ffmpeg` solo validaba `-map` y el códec sobre un m2ts declarado: sobre
    un MKV aceptaba cualquier `-map` y daba por `copy` una recodificación,
    así que quitar los siete `-c:v copy` del pipeline CMv4.0 —días de
    libx265 en el NAS y el RPU perdido— sobrevivía a 222 tests;
  · `ffmpeg` ignoraba `-t`, y quitar el `-t 30` de un muestreo —volcar el
    HEVC entero para leer 720 frames— no cambiaba nada;
  · `ffprobe` sacaba los frames del escenario, no del fichero: un mux que
    truncara el vídeo contestaba la película entera;
  · `mkvpropedit` solo entendía `segment-uid` y `title`: los nombres, los
    flags y los capítulos de Tab 2 no se veían después, y una `EditionEntry`
    vacía —que el real (v88 del NAS y v102) rechaza con código 2— pasaba;
  · el `editor` de `dovi_tool` copiaba siempre lo mismo del donante, sin
    mirar `rpu_levels`: nadie podía ver de dónde salía el L1 de un merge.

Cada test fija UNA de esas opciones contra el comportamiento real.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_los_fakes_respetan_las_opciones -v
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))

from cmv40_harness import FakeToolbox, RpuProps, write_artifacts  # noqa: E402


class ConFakes(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="opciones_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp / "tb").install()
        self.addCleanup(self.tb.uninstall)

    def correr(self, *argv):
        return subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=60)

    def mkv(self, nombre="peli.mkv", **props):
        p = self.tmp / nombre
        write_artifacts(self.tmp, nombre, props=RpuProps(**props))
        return p


class TestFfmpegSobreUnMkv(ConFakes):

    def _extraer(self, *opciones):
        out = self.tmp / "v.hevc"
        r = self.correr("ffmpeg", "-y", "-i", self.mkv(), *opciones, "-f", "hevc", out)
        return r, out

    def test_un_map_que_no_existe_falla(self):
        r, _ = self._extraer("-map", "0:v:7", "-c:v", "copy")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("matches no streams", r.stderr)

    def test_con_copy_el_rpu_viaja(self):
        r, out = self._extraer("-map", "0:v:0", "-c:v", "copy")
        self.assertEqual(r.returncode, 0, r.stderr)
        e = self.correr("dovi_tool", "extract-rpu", out, "-o", self.tmp / "r.bin")
        self.assertEqual(e.returncode, 0, e.stderr)

    def test_recodificar_pierde_el_rpu(self):
        for opciones in (("-map", "0:v:0"), ("-map", "0:v:0", "-c:v", "libx265")):
            with self.subTest(opciones=opciones):
                r, out = self._extraer(*opciones)
                self.assertEqual(r.returncode, 0, r.stderr)
                e = self.correr("dovi_tool", "extract-rpu", out, "-o", self.tmp / "r.bin")
                self.assertNotEqual(e.returncode, 0, "un HEVC recodificado no lleva RPU")
                self.assertIn("No RPU", e.stderr)

    def test_un_avc_no_pasa_el_filtro_de_hevc(self):
        r, _ = self._extraer("-map", "0:v:0", "-c:v", "libx264", "-bsf:v", "hevc_mp4toannexb")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("hevc_mp4toannexb", r.stderr)


class TestFfmpegRespetaElLimite(ConFakes):

    def _ultimo_time(self, stderr):
        return [l for l in stderr.splitlines() if "time=" in l][-1].split("time=")[1].split()[0]

    def test_con_t_el_progreso_termina_en_el_limite(self):
        mkv = self.mkv(frames=172800)
        r = self.correr("ffmpeg", "-y", "-t", "30", "-i", mkv, "-map", "0:v:0", "-c:v", "copy",
                        "-f", "hevc", self.tmp / "m.hevc")
        self.assertEqual(self._ultimo_time(r.stderr), "00:00:30.00")

    def test_sin_t_el_progreso_es_la_pelicula_entera(self):
        mkv = self.mkv(frames=172800)
        r = self.correr("ffmpeg", "-y", "-i", mkv, "-map", "0:v:0", "-c:v", "copy",
                        "-f", "hevc", self.tmp / "m.hevc")
        self.assertEqual(self._ultimo_time(r.stderr), "02:00:00.00")


class TestFfprobeCuentaElContenido(ConFakes):

    def _frames(self, ruta):
        r = self.correr("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_frames:stream_tags", "-of", "json", ruta)
        tags = json.loads(r.stdout)["streams"][0].get("tags", {})
        return int(tags["NUMBER_OF_FRAMES"])

    def test_un_mux_de_un_video_corto_da_un_mkv_corto(self):
        write_artifacts(self.tmp, "BL.hevc", props=RpuProps(frames=500))
        out = self.tmp / "salida.mkv"
        r = self.correr("mkvmerge", "-o", out, self.tmp / "BL.hevc")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._frames(out), 500)

    def test_lo_que_el_escenario_declara_sigue_ganando(self):
        write_artifacts(self.tmp, "BL.hevc", props=RpuProps(frames=500))
        out = self.tmp / "salida.mkv"
        self.correr("mkvmerge", "-o", out, self.tmp / "BL.hevc")
        self.tb.define_media("salida.mkv", frames=777)
        self.assertEqual(self._frames(out), 777)


class TestMkvpropedit(ConFakes):

    def setUp(self):
        super().setUp()
        self.ruta = self.tmp / "editar.mkv"
        self.ruta.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 64)
        self.tb.define_mkv("editar.mkv", tracks=[
            {"type": "video", "codec": "HEVC/H.265/MPEG-H"},
            {"type": "audio", "codec": "TrueHD Atmos", "language": "spa", "default": True},
            {"type": "subtitles", "codec": "HDMV PGS", "language": "spa"}])
        self.tb.define_chapters("editar.mkv", [("00:00:00.000", "Uno"), ("00:10:00.000", "Dos")])

    def _xml(self, capitulos):
        atomos = "".join(
            f"<ChapterAtom><ChapterTimeStart>{ts}</ChapterTimeStart><ChapterDisplay>"
            f"<ChapterString>{n}</ChapterString></ChapterDisplay></ChapterAtom>" for ts, n in capitulos)
        p = self.tmp / "caps.xml"
        p.write_text(f"<?xml version='1.0'?><Chapters><EditionEntry>{atomos}</EditionEntry></Chapters>")
        return p

    def _j(self):
        return json.loads(self.correr("mkvmerge", "-J", self.ruta).stdout)

    def _capitulos(self):
        return self.correr("mkvextract", self.ruta, "chapters", "--simple").stdout

    def test_los_nombres_y_flags_se_ven_en_el_j(self):
        r = self.correr("mkvpropedit", self.ruta, "--edit", "track:2", "--set", "name=Castellano",
                        "--set", "flag-default=0", "--edit", "track:3", "--set", "flag-forced=1")
        self.assertEqual(r.returncode, 0, r.stderr)
        pistas = self._j()["tracks"]
        self.assertEqual(pistas[1]["properties"]["track_name"], "Castellano")
        self.assertFalse(pistas[1]["properties"]["default_track"])
        self.assertTrue(pistas[2]["properties"]["forced_track"])
        self.assertEqual(pistas[0]["properties"]["track_name"], "", "editó otra pista")

    def test_una_pista_que_no_existe_es_un_error(self):
        r = self.correr("mkvpropedit", self.ruta, "--edit", "track:9", "--set", "name=X")
        self.assertEqual(r.returncode, 2)

    def test_los_capitulos_se_reemplazan(self):
        r = self.correr("mkvpropedit", self.ruta, "--chapters", self._xml([("00:00:00.000", "Nuevo")]))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("CHAPTER01NAME=Nuevo", self._capitulos())
        self.assertNotIn("Dos", self._capitulos())
        self.assertEqual(self._j()["chapters"], [{"num_entries": 1}])

    def test_una_edicion_vacia_falla_como_el_real(self):
        r = self.correr("mkvpropedit", self.ruta, "--chapters", self._xml([]))
        self.assertEqual(r.returncode, 2)
        self.assertIn("At least one <ChapterAtom> element is needed", r.stderr)
        self.assertIn("Dos", self._capitulos(), "un error no puede tocar el fichero")

    def test_comillas_vacias_borran_los_capitulos(self):
        r = self.correr("mkvpropedit", self.ruta, "--chapters", "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._capitulos(), "")
        self.assertEqual(self._j()["chapters"], [])

    def test_sin_tocar_los_capitulos_siguen_los_del_disco(self):
        self.correr("mkvpropedit", self.ruta, "--edit", "track:2", "--set", "name=X")
        self.assertIn("CHAPTER02NAME=Dos", self._capitulos())


class TestElEditorRespetaLosNiveles(ConFakes):

    def setUp(self):
        super().setUp()
        write_artifacts(self.tmp, "RPU_source.bin", props=RpuProps(
            el_type="FEL", l1_max_cll=1000.0, l6_max_cll=1000, l5=(276, 276, 0, 0)))
        write_artifacts(self.tmp, "RPU_target.bin", props=RpuProps(
            cm_version="v4.0", has_l8=True, l1_max_cll=300.0, l6_max_cll=4000, l5=(0, 0, 0, 0)))

    def _merge(self, niveles, donante=None):
        cfg = self.tmp / "cfg.json"
        cfg.write_text(json.dumps({"allow_cmv4_transfer": True, "rpu_levels": niveles,
                                   "source_rpu": str(donante or self.tmp / "RPU_target.bin")}))
        out = self.tmp / "RPU_merged.bin"
        r = self.correr("dovi_tool", "editor", "-i", self.tmp / "RPU_source.bin", "-j", cfg, "-o", out)
        return r, out

    def test_la_lista_fel_trae_l1_y_l6_del_donante(self):
        r, out = self._merge([1, 2, 3, 6, 8, 9, 10, 11, 254])
        self.assertEqual(r.returncode, 0, r.stderr)
        p = self.tb.props_of(out)
        self.assertEqual((p.l1_max_cll, p.l6_max_cll, p.cm_version, p.has_l8), (300.0, 4000, "v4.0", True))
        self.assertEqual(tuple(p.l5), (276, 276, 0, 0), "el L5 no está en la lista")

    def test_la_lista_conservadora_conserva_l1_l5_y_l6(self):
        r, out = self._merge([3, 8, 9, 11, 254])
        self.assertEqual(r.returncode, 0, r.stderr)
        p = self.tb.props_of(out)
        self.assertEqual((p.l1_max_cll, p.l6_max_cll, tuple(p.l5)), (1000.0, 1000, (276, 276, 0, 0)))
        self.assertTrue(p.has_l8)

    def test_un_donante_que_no_existe_falla(self):
        r, _ = self._merge([3, 8, 9, 11, 254], donante=self.tmp / "no_esta.bin")
        self.assertNotEqual(r.returncode, 0)

    def test_info_de_un_frame_de_un_fichero_que_no_existe_falla(self):
        r = self.correr("dovi_tool", "info", "-i", self.tmp / "no_esta.bin", "-f", "0")
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
