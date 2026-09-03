"""La estimación del tamaño del MKV final (Tab 1).

El pipeline **copia** los flujos sin recodificar, así que el tamaño de salida
no es una predicción: es contabilidad. Vídeo + audios seleccionados, menos la
sobrecarga del contenedor Blu-ray (BDAV son paquetes de 192 bytes, de los que
4 son ATC y otros 4 cabecera TS; Matroska añade medio por ciento).

El factor no está deducido de la teoría — sale de contrastar 18 rips reales
del NAS contra su m2ts. Y lo más importante que se fija aquí es **cuándo NO se
estima**: el caso real son dos episodios de Juego de Tronos en los que
MediaInfo midió otro m2ts (146 y 128 Mbps implícitos, físicamente imposibles)
y la cuenta salía al doble del tamaño real. Una cifra inventada con pinta de
dato es peor que un hueco.
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.phase_b import (  # noqa: E402
    FACTOR_CONTENEDOR, TOPE_MBPS_FISICO, estimate_output_size_bytes,
)

GB = 1_000_000_000


def _mediainfo(file_size, audios):
    """`audios` = lista de StreamSize en bytes (None = MediaInfo no lo dio)."""
    pistas = [{"@type": "General", "FileSize": str(file_size)},
              {"@type": "Video", "Format": "HEVC"}]
    for ss in audios:
        t = {"@type": "Audio", "Format": "AC-3"}
        if ss is not None:
            t["StreamSize"] = str(ss)
        pistas.append(t)
    return types.SimpleNamespace(raw_json={"media": {"track": pistas}})


def _pista(lang, codec, desc):
    return types.SimpleNamespace(language=lang, codec=codec, description=desc)


def _incluida(lang, codec, desc):
    return types.SimpleNamespace(track_type="audio", raw=_pista(lang, codec, desc))


def _sesion(*, file_size, audios_bytes, origen, incluidas, duracion=6500.0,
            con_mediainfo=True):
    bdinfo = types.SimpleNamespace(
        duration_seconds=duracion,
        audio_tracks=list(origen),
        mediainfo_result=_mediainfo(file_size, audios_bytes) if con_mediainfo else None,
    )
    return types.SimpleNamespace(
        bdinfo_result=bdinfo,
        included_tracks=list(incluidas),
    )


# Un disco realista: 65 GB, cinco audios, se quedan dos (Castellano + VO).
ORIGEN = [
    _pista("English", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz"),
    _pista("Spanish", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz"),
    _pista("Spanish", "Dolby Digital Audio", "5.1 / 48 kHz"),
    _pista("French", "Dolby Digital Audio", "5.1 / 48 kHz"),
    _pista("English", "Dolby Digital Audio", "2.0 / 48 kHz"),
]
TAMANOS = [372_623_944, 532_310_000, 532_319_920, 532_319_920, 159_695_976]
INCLUIDAS = [_incluida("Spanish", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz"),
             _incluida("English", "Dolby TrueHD/Atmos Audio", "7.1 / 48 kHz")]


class TestLaCuenta(unittest.TestCase):

    def test_es_video_mas_los_audios_que_sobreviven(self):
        s = _sesion(file_size=65_394_561_024, audios_bytes=TAMANOS,
                    origen=ORIGEN, incluidas=INCLUIDAS)
        video = 65_394_561_024 - sum(TAMANOS)
        esperado = int(video * FACTOR_CONTENEDOR + TAMANOS[0] + TAMANOS[1])
        self.assertEqual(estimate_output_size_bytes(s), esperado)

    def test_cae_dentro_del_10_por_ciento_del_rip_real(self):
        """Obsession 2025: m2ts de 65,39 GB → MKV real de 58,7 GB."""
        s = _sesion(file_size=65_394_561_024, audios_bytes=TAMANOS,
                    origen=ORIGEN, incluidas=INCLUIDAS)
        est = estimate_output_size_bytes(s)
        real = 58_700_000_000
        self.assertLess(abs(est - real) / real, 0.10,
                        f"estimado {est/GB:.1f} GB vs real {real/GB:.1f} GB")

    def test_incluir_mas_audios_sube_la_estimacion(self):
        pocas = _sesion(file_size=65_394_561_024, audios_bytes=TAMANOS,
                        origen=ORIGEN, incluidas=INCLUIDAS[:1])
        todas = _sesion(file_size=65_394_561_024, audios_bytes=TAMANOS,
                        origen=ORIGEN,
                        incluidas=INCLUIDAS + [
                            _incluida("Spanish", "Dolby Digital Audio", "5.1 / 48 kHz")])
        self.assertLess(estimate_output_size_bytes(pocas),
                        estimate_output_size_bytes(todas))

    def test_el_mkv_pesa_menos_que_el_m2ts(self):
        s = _sesion(file_size=65_394_561_024, audios_bytes=TAMANOS,
                    origen=ORIGEN, incluidas=INCLUIDAS)
        self.assertLess(estimate_output_size_bytes(s), 65_394_561_024)


class TestCuandoNoSeEstima(unittest.TestCase):
    """Todos estos devuelven None, y eso es la funcionalidad."""

    def test_bitrate_imposible_el_m2ts_no_es_el_del_titulo(self):
        """Juego de Tronos S01E10: m2ts de 57,9 GB para 52,7 min = 146 Mbps.
        El pico físico de un BD100 UHD son 128. MediaInfo midió otro fichero
        (el fallback de `_resolve_m2ts_from_mpls` a 'el m2ts más grande'), y
        estimando salía el doble del tamaño real."""
        s = _sesion(file_size=57_900_000_000, audios_bytes=TAMANOS,
                    origen=ORIGEN, incluidas=INCLUIDAS, duracion=52.7 * 60)
        self.assertIsNone(estimate_output_size_bytes(s))

    def test_justo_por_debajo_del_tope_si_estima(self):
        dur = 57_900_000_000 * 8 / (TOPE_MBPS_FISICO * 0.99 * 1e6)
        s = _sesion(file_size=57_900_000_000, audios_bytes=TAMANOS,
                    origen=ORIGEN, incluidas=INCLUIDAS, duracion=dur)
        self.assertIsNotNone(estimate_output_size_bytes(s))

    def test_sin_mediainfo_no_hay_de_donde(self):
        s = _sesion(file_size=0, audios_bytes=[], origen=ORIGEN,
                    incluidas=INCLUIDAS, con_mediainfo=False)
        self.assertIsNone(estimate_output_size_bytes(s))

    def test_un_audio_sin_StreamSize_invalida_la_resta(self):
        """El vídeo sale por diferencia; si falta el tamaño de una pista, la
        diferencia se lo come y el resultado sería alto de más."""
        s = _sesion(file_size=65_394_561_024,
                    audios_bytes=[TAMANOS[0], None] + TAMANOS[2:],
                    origen=ORIGEN, incluidas=INCLUIDAS)
        self.assertIsNone(estimate_output_size_bytes(s))

    def test_mediainfo_con_menos_pistas_que_mkvmerge(self):
        s = _sesion(file_size=65_394_561_024, audios_bytes=TAMANOS[:3],
                    origen=ORIGEN, incluidas=INCLUIDAS)
        self.assertIsNone(estimate_output_size_bytes(s))

    def test_una_incluida_que_no_casa_con_ninguna_del_origen(self):
        s = _sesion(file_size=65_394_561_024, audios_bytes=TAMANOS, origen=ORIGEN,
                    incluidas=[_incluida("Catalan", "DTS Audio", "5.1 / 48 kHz")])
        self.assertIsNone(estimate_output_size_bytes(s))

    def test_sin_duracion_no_se_puede_comprobar_el_bitrate(self):
        s = _sesion(file_size=65_394_561_024, audios_bytes=TAMANOS,
                    origen=ORIGEN, incluidas=INCLUIDAS, duracion=0)
        self.assertIsNone(estimate_output_size_bytes(s))

    def test_sin_bdinfo_no_revienta(self):
        self.assertIsNone(estimate_output_size_bytes(
            types.SimpleNamespace(bdinfo_result=None, included_tracks=[])))


if __name__ == "__main__":
    unittest.main()
