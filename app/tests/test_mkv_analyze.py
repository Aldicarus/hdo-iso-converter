"""
El análisis de MKV del Tab 2, ejecutado.

`analyze_mkv` (286 líneas, complejidad 53), `analyze_rpu_quality_for_mkv`
(347 / 61) y `_enrich_dovi_from_json_export` (198 / 82) no tenían **ningún**
test que las ejecutara: el mismo perfil que el pipeline CMv4.0 antes de
hacerlo testeable.

El primer test que se escribió aquí destapó un bug de producción: MediaInfo
devuelve `MaxCLL` **con unidad** (`'300 cd/m2'`), el código hacía
`int(rt["MaxCLL"])` y un `except: pass` dejaba el campo en None. Verificado
sobre los 20 MKVs cacheados del NAS: los 20 con `max_cll: None`. Esos dos
valores nunca llegaron a la radiografía DV+HDR ni a las líneas de referencia
del perfil de luminancia, pese a estar documentados como visibles.

Por eso el `mediainfo` falso del arnés emite los valores CON unidad, como el
real: un fake que diera el número pelado habría ocultado el bug.
"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import FakeToolbox, RpuProps, write_artifacts  # noqa: E402


class MkvAnalyzeCase(unittest.IsolatedAsyncioTestCase):
    """Un MKV falso con su mkvmerge, mediainfo y capítulos declarados."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mkv_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)

        from phases import mkv_analyze
        self.mod = mkv_analyze
        self._orig_out = mkv_analyze.OUTPUT_DIR
        mkv_analyze.OUTPUT_DIR = str(self.tmp)
        self.addCleanup(lambda: setattr(mkv_analyze, "OUTPUT_DIR", self._orig_out))

        self.mkv = self.tmp / "Peli (2024).mkv"
        write_artifacts(self.tmp, self.mkv.name)

    def declarar(self, **kw):
        """Declara el MKV con sus datos; kw pasa a define_mkv/define_mediainfo."""
        self.tb.define_mkv(self.mkv.name, title=kw.pop("title", "Peli (2024)"),
                           tracks=kw.pop("tracks", None),
                           duration_s=kw.pop("duration_s", 7200.0))
        # Paquetes PGS por defecto: el forzado con muchos menos que el
        # completo, que es la proporción real de un disco.
        self.tb.define_pgs_packets(self.mkv.name,
                                   kw.pop("pgs", {4: 120, 5: 4500}))
        self.tb.define_mediainfo(self.mkv.name, **kw)
        self.tb.define_media(self.mkv.name, duration=7200.0, frames=172800)

    async def analizar(self):
        return await self.mod.analyze_mkv(str(self.mkv), use_cache=False)


class TestMetadataHdr(MkvAnalyzeCase):
    """La regresión: MaxCLL/MaxFALL llegan con unidad y hay que parsearlos."""

    async def test_maxcll_y_maxfall_se_extraen_con_unidad(self):
        self.declarar(video={"maxcll": 300, "maxfall": 237})
        r = await self.analizar()
        self.assertIsNotNone(r.hdr, "debería detectar HDR10")
        self.assertEqual(r.hdr.max_cll, 300,
                         "MediaInfo da '300 cd/m2': hay que sacar el número")
        self.assertEqual(r.hdr.max_fall, 237)

    async def test_el_resto_de_la_metadata_hdr(self):
        self.declarar()
        r = await self.analizar()
        self.assertEqual(r.hdr.hdr_format, "HDR10")
        self.assertEqual(r.hdr.color_primaries, "BT.2020")
        self.assertEqual(r.hdr.transfer_characteristics, "PQ")
        self.assertEqual(r.hdr.bit_depth, 10)
        # Estos dos ya funcionaban: se asignan como cadena, sin convertir.
        self.assertIn("1000 cd/m2", r.hdr.mastering_display_luminance)
        self.assertEqual(r.hdr.mastering_display_primaries, "Display P3")

    async def test_un_mkv_sdr_no_reporta_hdr(self):
        self.declarar(video={"transfer": "BT.709", "primaries": "BT.709"})
        r = await self.analizar()
        self.assertIsNone(r.hdr)

    async def test_hlg_se_reconoce(self):
        self.declarar(video={"transfer": "HLG"})
        r = await self.analizar()
        self.assertEqual(r.hdr.hdr_format, "HLG")

    async def test_sin_maxcll_queda_a_none_sin_reventar(self):
        # MKVs sin esa metadata en el SEI son normales.
        self.declarar(video={"maxcll": None, "maxfall": None})
        r = await self.analizar()
        self.assertIsNone(r.hdr.max_cll)
        self.assertIsNone(r.hdr.max_fall)


class TestParseoDeNits(unittest.TestCase):
    """El helper, en aislamiento."""

    def valor(self, v):
        from phases.mkv_analyze import _nits_de_mediainfo
        return _nits_de_mediainfo(v)

    def test_con_unidad(self):
        self.assertEqual(self.valor("300 cd/m2"), 300)
        self.assertEqual(self.valor("1000 cd/m2"), 1000)
        self.assertEqual(self.valor(" 84 cd/m2 "), 84)

    def test_numero_pelado(self):
        self.assertEqual(self.valor("237"), 237)
        self.assertEqual(self.valor(300), 300)

    def test_valores_sin_dato(self):
        for v in (None, "", "   ", "N/A", "unknown"):
            with self.subTest(valor=v):
                self.assertIsNone(self.valor(v))


class TestIdentificacionDePistas(MkvAnalyzeCase):

    async def test_identifica_todas_las_pistas_con_sus_flags(self):
        self.declarar()
        r = await self.analizar()
        self.assertEqual(len(r.tracks), 6)
        tipos = [t.type for t in r.tracks]
        self.assertEqual(tipos.count("video"), 2)
        self.assertEqual(tipos.count("audio"), 2)
        self.assertEqual(tipos.count("subtitles"), 2)
        # El castellano por defecto y el forzado marcado
        audio_es = next(t for t in r.tracks if t.type == "audio" and t.language == "spa")
        self.assertTrue(audio_es.flag_default)
        sub_forzado = next(t for t in r.tracks if t.type == "subtitles" and t.flag_forced)
        self.assertEqual(sub_forzado.language, "spa")

    async def test_detecta_la_enhancement_layer(self):
        # Dos pistas HEVC, la segunda a 1080p → dual-layer con FEL.
        self.declarar()
        r = await self.analizar()
        self.assertTrue(r.has_fel)

    async def test_un_mkv_single_layer_no_tiene_fel(self):
        self.declarar(tracks=[
            {"type": "video", "codec": "HEVC/H.265/MPEG-H",
             "dimensions": "3840x2160", "fps": 23.976},
            {"type": "audio", "codec": "TrueHD Atmos", "language": "spa"},
        ])
        r = await self.analizar()
        self.assertFalse(r.has_fel)

    async def test_calcula_el_frame_count_del_video(self):
        # duration × fps, que es exacto para framerate constante.
        self.declarar(duration_s=3600.0)
        r = await self.analizar()
        video = next(t for t in r.tracks if t.type == "video")
        self.assertAlmostEqual(video.fps, 23.976, places=2)
        self.assertEqual(video.frame_count, round(3600.0 * video.fps))

    async def test_titulo_y_duracion_del_contenedor(self):
        self.declarar(title="Mi Peli (2024)", duration_s=5400.0)
        r = await self.analizar()
        self.assertEqual(r.title, "Mi Peli (2024)")
        self.assertEqual(r.duration_seconds, 5400.0)


class TestEnriquecimientoConMediaInfo(MkvAnalyzeCase):
    """MediaInfo aporta lo que mkvmerge no sabe: bitrate real y el nombre
    comercial del codec, que es la señal definitiva de Atmos/DTS:X."""

    async def test_bitrate_real_del_video(self):
        self.declarar(video={"bitrate": 77_805_084})
        r = await self.analizar()
        video = next(t for t in r.tracks if t.type == "video")
        self.assertEqual(video.bitrate_kbps, 77_805)

    async def test_bitrate_de_cada_audio_en_orden(self):
        # El emparejamiento va por stream_order, no por posición en la lista
        # de MediaInfo: si se hiciera por posición, el bitrate podría caer en
        # la pista equivocada.
        self.declarar(audio=[{"bitrate": 4_423_581}, {"bitrate": 4_151_159}])
        r = await self.analizar()
        audios = [t for t in r.tracks if t.type == "audio"]
        self.assertEqual(audios[0].bitrate_kbps, 4_423)
        self.assertEqual(audios[1].bitrate_kbps, 4_151)

    async def test_el_nombre_comercial_delata_atmos(self):
        self.declarar(audio=[{"commercial": "Dolby TrueHD with Dolby Atmos"},
                             {"commercial": "DTS-HD Master Audio"}])
        r = await self.analizar()
        audios = [t for t in r.tracks if t.type == "audio"]
        self.assertIn("Atmos", audios[0].format_commercial)
        self.assertNotIn("Atmos", audios[1].format_commercial)

    async def test_modo_de_compresion(self):
        self.declarar(audio=[{"compression": "Lossless"}, {"compression": "Lossy"}])
        r = await self.analizar()
        audios = [t for t in r.tracks if t.type == "audio"]
        self.assertEqual(audios[0].compression_mode, "Lossless")
        self.assertEqual(audios[1].compression_mode, "Lossy")

    async def test_si_mediainfo_no_dice_nada_el_analisis_sigue(self):
        # El enriquecimiento es opcional: sin MediaInfo hay que devolver las
        # pistas de mkvmerge, no fallar.
        self.tb.define_mkv(self.mkv.name)
        self.tb.define_media(self.mkv.name)
        r = await self.analizar()
        self.assertEqual(len(r.tracks), 6)
        self.assertIsNone(r.hdr)


class TestCapitulos(MkvAnalyzeCase):

    async def test_extrae_los_capitulos_con_su_nombre(self):
        self.declarar()
        self.tb.define_chapters(self.mkv.name, [
            ("00:00:00.000", "Apertura"),
            ("00:12:34.567", "El plan"),
            ("01:05:00.000", "Final"),
        ])
        r = await self.analizar()
        self.assertEqual(len(r.chapters), 3)
        self.assertEqual([c.name for c in r.chapters],
                         ["Apertura", "El plan", "Final"])

    async def test_un_mkv_sin_capitulos_no_falla(self):
        self.declarar()
        r = await self.analizar()
        self.assertEqual(r.chapters, [])


class TestConteoDePaquetesPgs(MkvAnalyzeCase):
    """La señal que separa un subtítulo forzado de uno completo: los forzados
    tienen muchos menos paquetes. MediaInfo no da bitrate de PGS con parse
    rápido, así que este conteo es la única vía."""

    async def test_cuenta_los_paquetes_escalando_desde_la_muestra(self):
        # No lee el MKV entero: muestrea los primeros 1200 s y extrapola por
        # `duración / 1200`. Con 7200 s de duración, el factor es 6.
        self.declarar(pgs={4: 120, 5: 4500}, duration_s=7200.0)
        r = await self.analizar()
        subs = [t for t in r.tracks if t.type == "subtitles"]
        self.assertEqual(subs[0].packet_count, 120 * 6)
        self.assertEqual(subs[1].packet_count, 4500 * 6)

    async def test_una_pelicula_corta_no_se_muestrea(self):
        # El muestreo solo aplica si la duración supera 1200 × 1.5 = 1800 s.
        # Por debajo se cuenta entero y no hay que escalar nada.
        self.declarar(pgs={4: 120, 5: 4500}, duration_s=1500.0)
        r = await self.analizar()
        subs = [t for t in r.tracks if t.type == "subtitles"]
        self.assertEqual(subs[0].packet_count, 120)
        self.assertEqual(subs[1].packet_count, 4500)

    async def test_el_forzado_tiene_muchos_menos_que_el_completo(self):
        self.declarar(pgs={4: 90, 5: 5200})
        r = await self.analizar()
        subs = [t for t in r.tracks if t.type == "subtitles"]
        forzado = next(t for t in subs if t.flag_forced)
        completo = next(t for t in subs if not t.flag_forced)
        self.assertLess(forzado.packet_count, completo.packet_count / 3)

    async def test_sin_datos_de_paquetes_el_analisis_sigue(self):
        # El conteo es best-effort: no debe tumbar el análisis.
        self.tb.define_mkv(self.mkv.name)
        self.tb.define_mediainfo(self.mkv.name)
        self.tb.define_media(self.mkv.name)
        r = await self.analizar()
        self.assertEqual(len(r.tracks), 6)

class TestPasosDeProgreso(MkvAnalyzeCase):
    """El frontend pinta un modal con estos pasos: si cambian de nombre o de
    orden, la barra se queda muda."""

    async def test_emite_los_cuatro_pasos_en_orden(self):
        self.declarar()
        pasos = []
        await self.mod.analyze_mkv(str(self.mkv), progress_callback=pasos.append,
                                  use_cache=False)
        self.assertEqual(pasos, ["identify", "mediainfo", "pgs", "dovi"])

    async def test_funciona_sin_callback(self):
        self.declarar()
        r = await self.analizar()          # no debe requerir callback
        self.assertTrue(r.tracks)


if __name__ == "__main__":
    unittest.main()
