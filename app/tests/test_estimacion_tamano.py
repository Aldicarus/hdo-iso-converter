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
import json
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.phase_b import (  # noqa: E402
    FACTOR_CONTENEDOR, TOPE_AUDIO_SOBRE_BASE, TOPE_MBPS_FISICO,
    estimate_output_size_bytes,
)

# En su propio directorio y no en `fixtures_discos/`: ese lo recorre entero
# `test_golden_discos_reales` con un `glob`, y un JSON con otra forma le
# revienta el setUpClass.
GOLDEN = Path(__file__).parent / "fixtures_estimacion" / "golden_estimacion_tamano.json"

GB = 1_000_000_000


def _mediainfo(file_size, audios, bitrates=None):
    """`audios` = lista de StreamSize en bytes (None = MediaInfo no lo dio).
    `bitrates` = lista paralela de BitRate en bps (None = tampoco lo dio)."""
    pistas = [{"@type": "General", "FileSize": str(file_size)},
              {"@type": "Video", "Format": "HEVC"}]
    for i, ss in enumerate(audios):
        t = {"@type": "Audio", "Format": "AC-3"}
        if ss is not None:
            t["StreamSize"] = str(ss)
        br = (bitrates or [None] * len(audios))[i]
        if br is not None:
            t["BitRate"] = str(br)
        pistas.append(t)
    return types.SimpleNamespace(raw_json={"media": {"track": pistas}})


def _pista(lang, codec, desc):
    return types.SimpleNamespace(language=lang, codec=codec, description=desc)


def _incluida(lang, codec, desc):
    return types.SimpleNamespace(track_type="audio", raw=_pista(lang, codec, desc))


def _sesion(*, file_size, audios_bytes, origen, incluidas, duracion=6500.0,
            con_mediainfo=True, bitrates=None, playlist_size=None):
    bdinfo = types.SimpleNamespace(
        duration_seconds=duracion,
        audio_tracks=list(origen),
        mediainfo_result=(_mediainfo(file_size, audios_bytes, bitrates)
                          if con_mediainfo else None),
        mkvmerge_raw=({"container": {"properties": {"playlist_size": playlist_size}}}
                      if playlist_size else None),
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


class TestLaBaseEsElPlaylistNoElM2ts(unittest.TestCase):
    """Un m2ts puede contener más de lo que su playlist reproduce, y lo que
    acaba en el MKV es lo segundo.

    Medido sobre el corpus del NAS: 16 de 44 discos difieren más de un 2%.
    Minions & Monsters tiene un m2ts de 91,4 GB del que el playlist usa 70,0,
    y el MKV salió de 64,0 — con `FileSize` la estimación daba 81 GB (+26,6%)
    y con `playlist_size` da 61,9 (−3,2%).
    """

    def test_manda_el_playlist_cuando_los_dos_estan(self):
        con = _sesion(file_size=91_435_266_048, playlist_size=70_001_713_152,
                      audios_bytes=TAMANOS, origen=ORIGEN, incluidas=INCLUIDAS)
        sin = _sesion(file_size=91_435_266_048,
                      audios_bytes=TAMANOS, origen=ORIGEN, incluidas=INCLUIDAS)
        self.assertLess(estimate_output_size_bytes(con),
                        estimate_output_size_bytes(sin))
        self.assertLess(estimate_output_size_bytes(con), 70_001_713_152)

    def test_sin_playlist_size_se_usa_el_m2ts(self):
        """Discos donde mkvmerge no lo da — el comportamiento de siempre."""
        s = _sesion(file_size=65_394_561_024, audios_bytes=TAMANOS,
                    origen=ORIGEN, incluidas=INCLUIDAS)
        self.assertIsNotNone(estimate_output_size_bytes(s))

    def test_el_playlist_puede_ser_MAYOR_que_el_m2ts(self):
        """Seamless branching: el playlist encadena varios m2ts y `FileSize`
        solo describe el principal. Caso real: Avatar Fuego y Ceniza, 102,7 GB
        de m2ts contra 104,2 de playlist."""
        s = _sesion(file_size=102_660_298_752, playlist_size=104_240_510_976,
                    audios_bytes=TAMANOS, origen=ORIGEN, incluidas=INCLUIDAS,
                    duracion=11_841.0)
        est = estimate_output_size_bytes(s)
        self.assertIsNotNone(est)
        self.assertGreater(est, 102_660_298_752 * 0.85)

    def test_el_tope_de_bitrate_se_mide_sobre_la_base_nueva(self):
        """Los episodios de Juego de Tronos tenían m2ts de hasta 98 GB con
        playlists de ~32: con el m2ts el bitrate implícito era imposible y no
        se estimaba nada; con el playlist sale un valor normal y sí se puede."""
        s = _sesion(file_size=97_390_718_976, playlist_size=31_936_094_208,
                    audios_bytes=[a // 4 for a in TAMANOS], origen=ORIGEN,
                    incluidas=INCLUIDAS, duracion=55 * 60)
        self.assertIsNotNone(estimate_output_size_bytes(s))


class TestElAudioSinStreamSize(unittest.TestCase):
    """MediaInfo no siempre da `StreamSize` — falta en 18 de las 44 sesiones
    del NAS, y ahí no se estimaba nada. `BitRate` sí suele estar (12 de esas
    18) y sobre las 250 pistas donde están los dos el error mediano de
    `BitRate × duración` es +0,0%."""

    DUR = 5399.0

    def test_se_cae_al_bitrate_cuando_falta_el_streamsize(self):
        s = _sesion(file_size=70_001_713_152, duracion=self.DUR,
                    audios_bytes=[None] * 5,
                    bitrates=[448_000, 640_000, 768_000, 448_000, 384_000],
                    origen=ORIGEN, incluidas=INCLUIDAS)
        self.assertIsNotNone(estimate_output_size_bytes(s))

    def test_el_streamsize_tiene_preferencia(self):
        """Es una medida, no una extrapolación."""
        con_ss = _sesion(file_size=65_394_561_024, duracion=self.DUR,
                         audios_bytes=TAMANOS,
                         bitrates=[9_000_000] * 5,   # absurdo a propósito
                         origen=ORIGEN, incluidas=INCLUIDAS)
        solo_ss = _sesion(file_size=65_394_561_024, duracion=self.DUR,
                          audios_bytes=TAMANOS, origen=ORIGEN, incluidas=INCLUIDAS)
        self.assertEqual(estimate_output_size_bytes(con_ss),
                         estimate_output_size_bytes(solo_ss))

    def test_sin_streamsize_NI_bitrate_se_sigue_ocultando(self):
        """Caso real: Avatar Fuego y Ceniza, una de sus siete pistas no trae
        ninguno de los dos. El vídeo sale por diferencia, así que sin todos
        los tamaños la resta miente."""
        s = _sesion(file_size=70_001_713_152, duracion=self.DUR,
                    audios_bytes=[None] * 5,
                    bitrates=[448_000, None, 768_000, 448_000, 384_000],
                    origen=ORIGEN, incluidas=INCLUIDAS)
        self.assertIsNone(estimate_output_size_bytes(s))

    def test_un_audio_desproporcionado_no_pasa(self):
        """La red para que un BitRate disparatado no llegue al chip: el audio
        es el 5,4% del fichero en la mediana y el 13,7% en el peor caso
        medido, así que por encima del 25% el dato no describe un disco."""
        base = 65_394_561_024
        gordo = int(base * TOPE_AUDIO_SOBRE_BASE / 4) + 1
        s = _sesion(file_size=base, audios_bytes=[gordo] * 5,
                    origen=ORIGEN, incluidas=INCLUIDAS)
        self.assertIsNone(estimate_output_size_bytes(s))


def _sesion_de_golden(f):
    """Reconstruye la sesión con lo que la estimación consume del disco real."""
    pistas = [{"@type": "General", "FileSize": f["file_size"]}] if f["file_size"] else []
    for a in f["audios_mediainfo"]:
        t = {"@type": "Audio"}
        t.update({k: v for k, v in a.items() if v is not None})
        pistas.append(t)
    bdinfo = types.SimpleNamespace(
        duration_seconds=f["duration"],
        audio_tracks=[_pista(*o) for o in f["origen"]],
        mediainfo_result=types.SimpleNamespace(raw_json={"media": {"track": pistas}}),
        mkvmerge_raw=({"container": {"properties": {"playlist_size": f["playlist_size"]}}}
                      if f["playlist_size"] else None),
    )
    return types.SimpleNamespace(
        bdinfo_result=bdinfo,
        included_tracks=[_incluida(*i) for i in f["incluidas"]],
    )


@unittest.skipUnless(GOLDEN.exists(), "sin golden de discos reales")
class TestGoldenDiscosReales(unittest.TestCase):
    """La estimación contra los discos del NAS, con el MKV real como juez.

    Las fixtures son el recorte de sesiones de verdad: `FileSize`,
    `playlist_size`, duración, los tamaños/bitrates de audio de MediaInfo y el
    emparejamiento de pistas. Nada inventado — si un caso de aquí falla, hay un
    disco real que se estimaría distinto.

    Los `real_bytes` de los tres rips del 2026-09-04 están medidos sobre el
    fichero recién escrito en /mnt/output. Ojo con el de Mandalorian: en la
    biblioteca convive el rip viejo (78,8 GB), que NO es este.
    """

    @classmethod
    def setUpClass(cls):
        cls.datos = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_cada_disco_da_lo_esperado(self):
        for sid, f in self.datos.items():
            with self.subTest(disco=sid):
                self.assertEqual(estimate_output_size_bytes(_sesion_de_golden(f)),
                                 f["esperado"])

    def test_el_error_contra_el_mkv_real_cabe_en_el_10_por_ciento(self):
        medidos = 0
        for sid, f in self.datos.items():
            if not f.get("real_bytes") or not f.get("esperado"):
                continue
            medidos += 1
            err = abs(f["esperado"] - f["real_bytes"]) / f["real_bytes"]
            with self.subTest(disco=sid):
                self.assertLess(err, 0.10, f"{err*100:.1f}% de error")
        self.assertGreaterEqual(medidos, 6, "el golden perdió discos medibles")

    def test_los_que_antes_se_ocultaban_ahora_salen(self):
        """El_padrino y Minions no tenían chip: el primero por falta de
        StreamSize, el segundo por eso y por la base equivocada."""
        for sid in ("El_padrino_1972_1779447082",
                    "Minions___Monsters_2026_1788523016"):
            with self.subTest(disco=sid):
                self.assertIsNotNone(self.datos[sid]["esperado"])

    def test_avatar_se_sigue_ocultando_y_es_correcto(self):
        """Una de sus siete pistas no trae StreamSize ni BitRate."""
        self.assertIsNone(self.datos["Avatar_Fuego_y_ceniza_2025_1782679231"]["esperado"])
