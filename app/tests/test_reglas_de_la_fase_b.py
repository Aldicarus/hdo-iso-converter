"""Cuatro reglas de la Fase B que ningún test fijaba.

La auditoría de la suite con Codex (2026-10-17) propuso mutaciones de las
reglas de selección de pistas y estas cuatro pasaban la suite entera:

  · invertir el orden de calidad del audio (elegir la PEOR pista del idioma);
  · invertir forzado y completo cuando no hay conteo PGS (el camino de
    respaldo por bitrate sintético);
  · fijar la VO de emergencia a inglés en un disco sin inglés ni castellano;
  · quitar la red de los subtítulos completos en inglés.

Las cuatro cambian el contenido del MKV sin que nada falle, así que cada una
lleva aquí el caso que la distingue.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_reglas_de_la_fase_b -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import i18n  # noqa: E402
from models import RawAudioTrack, RawSubtitleTrack  # noqa: E402
from phases.phase_b import (  # noqa: E402
    _detect_vo_language, _select_audio_tracks, _select_subtitle_tracks,
)


def en_castellano():
    return mock.patch.object(i18n, "idioma_activo", lambda: "es")


def audio(lang, codec, ch="5.1", kbps=640, comercial=""):
    return RawAudioTrack(language=lang, codec=codec, description=f"{ch} / 48 kHz",
                         bitrate_kbps=kbps, format_commercial=comercial)


def sub(lang, *, paquetes=0, forzado_por_estructura=False):
    """Bitrate sintético de la Fase A: 1.0 = posición de forzado en el disco."""
    return RawSubtitleTrack(language=lang, description="",
                            bitrate_kbps=1.0 if forzado_por_estructura else 30.0,
                            packet_count=paquetes)


class TestElAudioDeMayorCalidad(unittest.TestCase):

    def test_de_cada_idioma_entra_la_mejor(self):
        disco = [audio("English", "Dolby Digital Audio", kbps=640),
                 audio("English", "Dolby TrueHD/Atmos Audio", "7.1", 4500,
                       "Dolby TrueHD with Dolby Atmos"),
                 audio("Spanish", "Dolby Digital Audio", kbps=640),
                 audio("Spanish", "DTS-HD Master Audio", kbps=3000)]
        with en_castellano():
            incluidas, descartadas = _select_audio_tracks(disco, "English", False)
        codecs = {t.raw.language: t.raw.codec for t in incluidas}
        self.assertEqual(codecs, {"Spanish": "DTS-HD Master Audio",
                                  "English": "Dolby TrueHD/Atmos Audio"})
        self.assertEqual(sorted(d.raw.codec for d in descartadas),
                         ["Dolby Digital Audio", "Dolby Digital Audio"])

    def test_a_igual_codec_gana_el_de_mas_bitrate(self):
        disco = [audio("Spanish", "Dolby Digital Audio", kbps=448),
                 audio("Spanish", "Dolby Digital Audio", kbps=640),
                 audio("English", "Dolby Digital Audio", kbps=640)]
        with en_castellano():
            incluidas, _ = _select_audio_tracks(disco, "English", False)
        castellano = [t for t in incluidas if t.raw.language == "Spanish"]
        self.assertEqual([t.raw.bitrate_kbps for t in castellano], [640])


class TestSinConteoPgsMandaLaEstructura(unittest.TestCase):
    """Sin conteos (el muestreo PGS falló entero), el respaldo es el bitrate
    sintético de la Fase A: menos de 3 kbps es la posición de un forzado."""

    def test_el_forzado_es_el_de_la_posicion_de_forzado(self):
        disco = [sub("English"), sub("Spanish"),
                 sub("English", forzado_por_estructura=True),
                 sub("Spanish", forzado_por_estructura=True)]
        with en_castellano():
            incluidas, _ = _select_subtitle_tracks(disco, "English")
        por_tipo = {(t.raw.language, t.subtitle_type): t.raw.bitrate_kbps
                    for t in incluidas}
        self.assertEqual(por_tipo[("Spanish", "forced")], 1.0,
                         "el forzado castellano ha recibido la pista completa")
        self.assertEqual(por_tipo[("Spanish", "complete")], 30.0)
        self.assertEqual(por_tipo[("English", "complete")], 30.0)


class TestLaVoDeEmergencia(unittest.TestCase):

    def test_sin_ingles_ni_castellano_es_el_idioma_de_la_primera_pista(self):
        disco = [audio("Japanese", "Dolby TrueHD Audio"),
                 audio("French", "Dolby Digital Audio")]
        vo, aviso = _detect_vo_language(disco)
        self.assertEqual(vo, "japanese")
        self.assertTrue(aviso, "una VO adivinada tiene que avisar")

    def test_con_ingles_es_ingles(self):
        vo, aviso = _detect_vo_language([audio("French", "Dolby Digital Audio"),
                                         audio("English", "Dolby Digital Audio")])
        self.assertEqual((vo, aviso), ("english", ""))


class TestLaRedDeSubtitulosEnIngles(unittest.TestCase):
    """Con una VO que no es inglés, los completos en inglés entran igual: es
    lo que el usuario entiende si no habla la lengua original."""

    def test_un_disco_en_japones_lleva_los_completos_ingleses(self):
        disco = [sub("Japanese", paquetes=7000), sub("Spanish", paquetes=6000),
                 sub("English", paquetes=6500), sub("Spanish", paquetes=300)]
        with en_castellano():
            incluidas, descartadas = _select_subtitle_tracks(disco, "Japanese")
        self.assertIn(("English", "complete"),
                      [(t.raw.language, t.subtitle_type) for t in incluidas])
        self.assertNotIn("English", [d.raw.language for d in descartadas])

    def test_con_vo_inglesa_no_se_duplica(self):
        disco = [sub("English", paquetes=7000), sub("Spanish", paquetes=6000)]
        with en_castellano():
            incluidas, _ = _select_subtitle_tracks(disco, "English")
        ingleses = [t for t in incluidas if t.raw.language == "English"]
        self.assertEqual(len(ingleses), 1)


if __name__ == "__main__":
    unittest.main()
