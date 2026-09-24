# -*- coding: utf-8 -*-
"""La ficha técnica de un MKV: lo que MediaInfo daba y no se leía.

De los 400 campos que MediaInfo devuelve sobre un MKV real se guardaban
136 — el 34 % — y lo que faltaba era justo lo que cita un análisis de un
UHD: con qué es compatible el stream, qué perfil DV declara el
contenedor, el perfil del códec, la señal de color completa y los
identificadores de IMDb y TMDb, que vienen escritos DENTRO del fichero.

La fixture es el recorte de un `mediainfo --Output=JSON` de verdad
—Drive (2011), P7 FEL— y no una maqueta: los campos emparejados de
Dolby Vision llegan con la barra pegada (`'dvhe.07 / '`) y eso sólo se
ve con el formato real delante. Es la regla de los fakes fieles, la que
destapó que el `MaxCLL` de MediaInfo trae unidad.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_ficha_tecnica_mkv -v
"""
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from models import MkvTrackInfo  # noqa: E402
from phases import mkv_analyze as MA  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures_mediainfo" / "drive_2011_dv_fel.json"


def _raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TestLosHelpersLeenElFormatoReal(unittest.TestCase):

    def test_las_pistas_salen_ordenadas_por_stream(self):
        """Por `StreamOrder`, no por el orden de la lista.

        Es el mismo criterio que el enriquecimiento de audio ya usaba, y
        por el mismo motivo: emparejar por posición metía el bitrate en
        la pista de al lado. **El caso se construye desordenado a
        propósito**: con una pista de cada tipo el orden no se puede
        observar y quitar el `sorted` pasaría en verde.
        """
        self.assertEqual(len(MA._pistas_raw(_raw(), "Video")), 1)
        self.assertEqual(MA._pistas_raw(_raw(), "General")[0]["Format"], "Matroska")
        revuelto = {"media": {"track": [
            {"@type": "Audio", "StreamOrder": "3", "Title": "tercera"},
            {"@type": "Audio", "StreamOrder": "1", "Title": "primera"},
            {"@type": "Audio", "StreamOrder": "2", "Title": "segunda"},
        ]}}
        self.assertEqual([t["Title"] for t in MA._pistas_raw(revuelto, "Audio")],
                         ["primera", "segunda", "tercera"])
        self.assertEqual(MA._pistas_raw(None, "Video"), [])
        self.assertEqual(MA._pistas_raw({}, "Audio"), [])

    def test_el_campo_dv_llega_emparejado_y_hay_que_partirlo(self):
        """`'dvhe.07 / '` — la segunda mitad es la del SMPTE ST 2086.

        Con Dolby Vision y HDR10 a la vez MediaInfo emite los campos
        emparejados por ` / `. Sin partir, el valor llega con la barra y
        el espacio pegados detrás.
        """
        v = MA._pistas_raw(_raw(), "Video")[0]
        self.assertEqual(v["HDR_Format_Profile"], "dvhe.07 / ")
        self.assertEqual(MA._parte_dv(v["HDR_Format_Profile"]), "dvhe.07")
        self.assertEqual(MA._parte_dv(v["HDR_Format_Settings"]), "BL+EL+RPU")
        self.assertEqual(MA._parte_dv(None), "")
        self.assertEqual(MA._parte_dv("sin barra"), "sin barra")

    def test_el_entero_aguanta_lo_que_venga(self):
        self.assertEqual(MA._entero("64066324"), 64066324)
        self.assertEqual(MA._entero("6034.487000000"), 6034)
        for malo in (None, "", "N/A", {}):
            self.assertEqual(MA._entero(malo), 0)


class TestElContenedor(unittest.TestCase):

    def setUp(self):
        self.c = MA._contenedor_de(_raw(), 60976993150)

    def test_los_identificadores_vienen_DENTRO_del_fichero(self):
        """Y son exactos, al contrario que un match por el nombre.

        Hoy la ficha de TMDb se resuelve parseando el nombre del fichero
        y comparando con difuso; esto lo escribió quien muxeó.
        """
        self.assertEqual(self.c.imdb_id, "tt0780504")
        self.assertEqual(self.c.tmdb_id, "movie/64690")

    def test_el_resto_de_la_ficha(self):
        self.assertEqual(self.c.format, "Matroska")
        self.assertEqual(self.c.format_version, "4")
        self.assertEqual(self.c.title, "Drive (2011)")
        self.assertEqual(self.c.overall_bitrate_kbps, 80837)
        self.assertEqual(self.c.overall_bitrate_mode, "VBR")
        self.assertIn("mkvmerge", self.c.encoded_application)
        self.assertTrue(self.c.is_streamable)

    def test_sin_track_general_no_se_inventa_un_contenedor(self):
        self.assertIsNone(MA._contenedor_de({"media": {"track": []}}, 1))
        self.assertIsNone(MA._contenedor_de(None, 1))


class TestElHdrQueElFicheroDECLARA(unittest.TestCase):
    """Lo que dice el contenedor, que no es lo que la app deducía.

    `hdr_format` se derivaba de la curva de transferencia, así que este
    disco —Dolby Vision Profile 7 con capa de mejora— quedaba descrito
    como «HDR10» y nada más.
    """

    def setUp(self):
        from models import HdrMetadata
        self.h = HdrMetadata()
        MA._volcar_hdr_declarado(self.h, MA._pistas_raw(_raw(), "Video")[0])

    def test_con_que_es_compatible(self):
        """La pregunta práctica: «¿esto lo reproduce mi equipo?»."""
        self.assertEqual(self.h.hdr_format_compatibility, "Blu-ray / HDR10")

    def test_el_formato_completo_y_el_perfil_declarado(self):
        self.assertEqual(self.h.hdr_format_raw, "Dolby Vision / SMPTE ST 2086")
        self.assertEqual(self.h.dv_profile_string, "dvhe.07")
        self.assertEqual(self.h.dv_level, "06")
        self.assertEqual(self.h.dv_layers, "BL+EL+RPU")

    def test_la_senal_de_color_entera(self):
        """Antes sólo se enseñaban los primarios."""
        self.assertEqual(self.h.chroma_subsampling, "4:2:0")
        self.assertEqual(self.h.colour_range, "Limited")
        self.assertEqual(self.h.matrix_coefficients, "BT.2020 non-constant")

    def test_y_el_master_donde_se_hizo_el_grade(self):
        self.assertEqual(self.h.mastering_display_primaries, "Display P3")
        self.assertIn("1000", self.h.mastering_display_luminance)

    def test_un_track_sin_nada_no_revienta(self):
        from models import HdrMetadata
        h = HdrMetadata()
        MA._volcar_hdr_declarado(h, {})
        self.assertEqual(h.hdr_format_compatibility, "")
        self.assertIsNone(h.max_cll)


class TestLaFichaDeCadaPista(unittest.TestCase):

    def _pista(self, tipo, mkv_type):
        t = MkvTrackInfo(id=0, type=mkv_type, codec="x")
        MA._ficha_de_pista(t, MA._pistas_raw(_raw(), tipo)[0], 60976993150)
        return t

    def test_el_video_trae_perfil_nivel_y_tier(self):
        v = self._pista("Video", "video")
        self.assertEqual(v.format_profile, "Main 10")
        self.assertEqual(v.format_level, "5.1")
        self.assertEqual(v.format_tier, "High")
        self.assertEqual(v.framerate_mode, "CFR")

    def test_el_porcentaje_contesta_en_que_se_va_el_fichero(self):
        """Sin él hay que dividir a mano ocho cifras por otras ocho."""
        v = self._pista("Video", "video")
        self.assertEqual(v.stream_size_bytes, 48325925490)
        self.assertAlmostEqual(v.stream_size_pct, 79.25, places=1)

    def test_el_audio_trae_su_layout_legible_y_su_modo(self):
        a = self._pista("Audio", "audio")
        self.assertEqual(a.bitrate_mode, "VBR")
        self.assertIn("Front: L C R", a.channel_positions)
        self.assertGreater(a.stream_size_pct, 0)

    def test_un_track_sin_esos_campos_no_revienta(self):
        t = MkvTrackInfo(id=0, type="subtitles", codec="x")
        MA._ficha_de_pista(t, {}, 0)
        self.assertEqual(t.format_profile, "")
        self.assertEqual(t.stream_size_pct, 0.0)
        self.assertEqual(t.delay_ms, 0.0)


class TestElTercerVeredictoLlegaATabDos(unittest.TestCase):
    """`tone_mapping` existía en Tab 3 y esta pestaña no lo conocía.

    `classify_l8` dejó de devolver «indeterminate» hace meses, así que la
    rama que Tab 2 tenía para él era inalcanzable y un bin sin trims de
    colorista pero con el análisis de Dolby caía al `else`: veredicto
    GRIS «ambiguo» sobre un fichero perfectamente descrito. Es lo que
    hacía imposible concluir nada mirando la card.
    """

    NUMEROS = {
        "l8_unique_count": 1, "l2_unique_count": 40, "l3_unique_count": 900,
        "l8_max_delta": 12, "l8_neutral_pct": 0.9, "scene_cuts": 1200,
        "l2_target_pqs": 3, "total_frames_rpu": 100000,
        "frames_with_cmv40": 100000, "l8_frames_sig_pct": 0.0,
        "l8_has_mid_contrast": False, "l8_has_clip_trim": False,
        "l3_frames": 100000,
    }

    def _textos(self, clasificacion):
        return MA._textos_de_calidad(self.NUMEROS, clasificacion, "",
                                     False, {"has_l11": True})

    def test_tone_mapping_tiene_su_propio_veredicto(self):
        t = self._textos("tone_mapping")
        self.assertEqual(t["quality_verdict_color"], "yellow")
        self.assertNotEqual(t["quality_verdict_text"], "")
        self.assertTrue(t["quality_provenance_hints"],
                        "un veredicto sin ninguna pista no explica nada")

    def test_y_ya_no_se_lee_como_ambiguo(self):
        gris = self._textos("indeterminate")["quality_verdict_text"]
        self.assertNotEqual(self._textos("tone_mapping")["quality_verdict_text"], gris)

    def test_los_otros_dos_siguen_donde_estaban(self):
        self.assertEqual(self._textos("default")["quality_verdict_color"], "red")
        self.assertEqual(self._textos("real")["quality_verdict_color"], "yellow")

    def test_ninguna_rama_de_indeterminate_sigue_viva_en_el_codigo(self):
        """`classify_l8` sólo devuelve real / tone_mapping / default.

        Una rama por una clasificación que nadie emite parece cobertura y
        no cubre nada — y mientras estuvo ahí tapó que faltaba la del
        tercer veredicto.
        """
        from phases import rpu_analyze
        src = Path(rpu_analyze.__file__).read_text(encoding="utf-8")
        self.assertNotIn('return "indeterminate"', src)
        vivas = [l for l in Path(MA.__file__).read_text(encoding="utf-8").splitlines()
                 if 'classification == "indeterminate"' in l]
        self.assertEqual(vivas, [])


class TestLaCacheSeInvalida(unittest.TestCase):

    def test_el_analisis_basico_sube_de_version(self):
        """Un bloque de la versión anterior no trae los campos nuevos, y
        el render los leería como «no presentes» en vez de «no medidos»."""
        self.assertGreaterEqual(MA.CACHE_VERSION_BASIC, 2)

    def test_pero_NO_se_lleva_por_delante_la_auditoria(self):
        """Los dos bloques se versionan por separado, y aquí está el
        motivo: el básico son ~50 s y el extendido son **10 minutos** de
        `extract-rpu` por MKV. Subir la versión del uno no puede costar
        el otro — sería pagar un re-análisis de diez minutos por haber
        añadido unos campos de MediaInfo.
        """
        import json
        import tempfile
        import storage
        fp = {"sha256_1mb": "a" * 64, "size_bytes": 10, "mtime_ns": 1}
        with tempfile.TemporaryDirectory() as d:
            original = storage.MKV_AUDIT_DIR
            try:
                storage.MKV_AUDIT_DIR = Path(d)
                ruta = Path(d) / f"{fp['sha256_1mb']}.json"
                ruta.write_text(json.dumps({
                    "fingerprint": fp,
                    "versions": {"basic": 1, "quality": MA.CACHE_VERSION_QUALITY},
                    "basic": {"file_name": "x.mkv"},
                    "quality": {"quality_classification": "real"},
                }), encoding="utf-8")
                leido = storage.read_mkv_cache(fp, MA.CACHE_VERSION_BASIC,
                                               MA.CACHE_VERSION_QUALITY)
            finally:
                storage.MKV_AUDIT_DIR = original
        self.assertIsNone(leido["basic"], "el básico viejo sí se descarta")
        self.assertIsNotNone(leido["quality"],
                             "la auditoría de 10 min NO se puede perder")


if __name__ == "__main__":
    unittest.main()
