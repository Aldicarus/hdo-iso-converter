"""La Fase A ve el Dolby Vision aunque mkvmerge no deje hueco en los IDs.

Caso real, octubre de 2026: Apocalypse Now The Final Cut (1979), un ISO de
HDO. El log decía «FEL: false · Sin capa de mejora Dolby Vision detectada» y
«Formato: HDR10», con MediaInfo listando DOS vídeos en el m2ts. dovi_tool ni
llegó a correr.

mkvmerge combina la Enhancement Layer con la BL y la oculta, pero le deja su
ID, y los IDs van en el orden del PMT. `_detect_fel` busca el hueco en el
id 1, que es donde cae en un disco de estudio (la EL es la segunda entrada,
`StreamOrder 0-1`). En uno re-autorado la EL va la ÚLTIMA (`0-18` en Pulp
Fiction) y el hueco cae detrás de todas las pistas, donde no se ve. Medido
sobre las 84 sesiones del NAS: Pulp Fiction es la otra, y las dos son de HDO.

Lo que se fija aquí:

  · con la EL en MediaInfo y sin hueco en mkvmerge, se detecta y dovi_tool
    corre — en los tres puntos de entrada del análisis;
  · lo que dice qué capa es sigue siendo dovi_tool (FEL o MEL), y si falla,
    queda el «FEL asumido» de siempre;
  · un disco HDR10 no lanza dovi_tool;
  · un disco de estudio se detecta igual que antes, con su razón;
  · y el MaxCLL/MaxFALL de MediaInfo, que llega con unidad, se guarda.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_capa_de_mejora_dv -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import CollectingLog, FakeToolbox  # noqa: E402
from i18n import t as tr  # noqa: E402
from models import MediaInfoResult  # noqa: E402
from phases import phase_a  # noqa: E402
from phases.phase_a import capa_de_mejora_en_mediainfo  # noqa: E402
from test_seleccion_mpls_principal import _disco  # noqa: E402

PELICULA_S = 10918          # 3 h 02 min
M2TS = 20_000_000           # a escala: el fichero es disperso

# Lo que `mkvmerge -J` dio en el log de Apocalypse Now: IDs del 0 al 6 sin
# hueco, con la EL oculta detrás de la última pista.
PISTAS_REAUTORADO = [
    {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "dimensions": "3840x2160"},
    {"id": 1, "type": "audio", "codec": "DTS-HD Master Audio", "language": "spa", "channels": 6},
    {"id": 2, "type": "audio", "codec": "DTS-HD Master Audio", "language": "spa", "channels": 2},
    {"id": 3, "type": "audio", "codec": "TrueHD Atmos", "language": "eng", "channels": 8,
     "multiplexed_tracks": [3, 4]},
    {"id": 4, "type": "audio", "codec": "AC-3", "language": "eng", "channels": 6,
     "multiplexed_tracks": [3, 4]},
    {"id": 5, "type": "subtitles", "codec": "HDMV PGS", "language": "spa"},
    {"id": 6, "type": "subtitles", "codec": "HDMV PGS", "language": "spa"},
]
# El mismo disco tal como lo saca un estudio: la EL es la segunda entrada del
# PMT, se queda con el id 1 y el audio empieza en el 2.
PISTAS_ESTUDIO = [dict(p, id=p["id"] + 1) if p["type"] != "video" else p
                  for p in PISTAS_REAUTORADO]
for _p in PISTAS_ESTUDIO:
    if "multiplexed_tracks" in _p:
        _p["multiplexed_tracks"] = [4, 5]

AUDIO_MI = [
    {"commercial": "DTS-HD Master Audio", "channels": 6, "language": "es"},
    {"commercial": "DTS-HD Master Audio", "channels": 2, "language": "es"},
    {"commercial": "Dolby TrueHD with Dolby Atmos", "channels": 8, "language": "en"},
]


def _mi(*videos) -> MediaInfoResult:
    """Un MediaInfoResult con los vídeos dados como (formato, ancho, alto)."""
    pistas = [{"@type": "General"}]
    pistas += [{"@type": "Video", "Format": f, "Width": str(an), "Height": str(al)}
               for f, an, al in videos]
    return MediaInfoResult(source_path="x.m2ts", raw_json={"media": {"track": pistas}})


class TestLaRegla(unittest.TestCase):
    """La regla es la de mkvmerge: un HEVC a la mitad de resolución que otro."""

    def test_la_bl_y_la_el_de_un_uhd(self):
        self.assertTrue(capa_de_mejora_en_mediainfo(
            _mi(("HEVC", 3840, 2160), ("HEVC", 1920, 1080))))

    def test_un_uhd_hdr10_no_tiene(self):
        self.assertFalse(capa_de_mejora_en_mediainfo(_mi(("HEVC", 3840, 2160))))

    def test_un_episodio_que_lista_la_bl_dos_veces(self):
        """Los E10 de Juego de Tronos: la BL aparece dos veces en MediaInfo."""
        self.assertTrue(capa_de_mejora_en_mediainfo(_mi(
            ("HEVC", 3840, 2160), ("HEVC", 3840, 2160), ("HEVC", 1920, 1080))))

    def test_la_bl_listada_dos_veces_no_es_una_capa(self):
        """Contar vídeos no basta: un playlist de varios clips puede listar la
        BL dos veces sin que haya Enhancement Layer."""
        self.assertFalse(capa_de_mejora_en_mediainfo(
            _mi(("HEVC", 3840, 2160), ("HEVC", 3840, 2160))))

    def test_un_1080p_suelto_no_es_una_capa(self):
        self.assertFalse(capa_de_mejora_en_mediainfo(_mi(("HEVC", 1920, 1080))))

    def test_un_avc_a_1080p_no_es_la_el_de_un_hevc(self):
        self.assertFalse(capa_de_mejora_en_mediainfo(
            _mi(("HEVC", 3840, 2160), ("AVC", 1920, 1080))))

    def test_sin_mediainfo_no_se_concluye_nada(self):
        self.assertFalse(capa_de_mejora_en_mediainfo(None))
        self.assertFalse(capa_de_mejora_en_mediainfo(MediaInfoResult(source_path="x")))

    def test_una_dimension_ilegible_no_lanza(self):
        mi = _mi(("HEVC", 3840, 2160))
        mi.raw_json["media"]["track"].append({"@type": "Video", "Format": "HEVC"})
        self.assertFalse(capa_de_mejora_en_mediainfo(mi))


class _ConDisco(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="capa_dv_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)
        # La muestra de la EL va a TMP_DIR, que por defecto es /mnt/tmp.
        trabajo = self.tmp / "trabajo"
        trabajo.mkdir()
        p = mock.patch.object(phase_a, "TMP_DIR", str(trabajo))
        p.start()
        self.addCleanup(p.stop)
        self.log = CollectingLog()

    def _apocalypse(self, *, pistas=PISTAS_REAUTORADO, el=True,
                    el_type="FEL") -> Path:
        self.tb.define_mkv("00004.mpls", tracks=pistas,
                           duration_s=PELICULA_S, playlist_size=M2TS)
        self.tb.define_mediainfo("00007.m2ts", audio=AUDIO_MI,
                                 video={"el": el, "el_stream_order": "0-6"})
        self.tb.define_rpu("00007.m2ts", profile=7, el_type=el_type,
                           cm_version="v2.9")
        return _disco(self.tmp / "Apocalypse", {"00004.mpls": [("00007", PELICULA_S)]},
                      {"00007": M2TS})

    def _muestreo_la_el(self) -> bool:
        return any(c.opt("-map") == "0:v:1" for c in self.tb.find("ffmpeg"))


class TestUnDiscoReautorado(_ConDisco):

    async def test_apocalypse_now_tiene_dolby_vision(self):
        bd, _, _ = await phase_a.run_full_analysis(str(self._apocalypse()), self.log)
        self.assertTrue(self._muestreo_la_el(), "dovi_tool no llegó a correr")
        self.assertTrue(bd.has_fel)
        dovi = bd.video_tracks[0].dovi
        self.assertIsNotNone(dovi)
        self.assertEqual((dovi.profile, dovi.el_type), (7, "FEL"))
        self.assertEqual(bd.fel_reason, tr('phase_a.fel_reason_dovi', perfil=7,
                                           el_type="FEL", cm="v2.9"))

    async def test_si_es_mel_lo_dice_dovi_tool(self):
        bd, _, _ = await phase_a.run_full_analysis(
            str(self._apocalypse(el_type="MEL")), self.log)
        self.assertFalse(bd.has_fel)
        self.assertEqual(bd.video_tracks[0].dovi.el_type, "MEL")

    async def test_si_dovi_tool_falla_queda_el_fel_asumido(self):
        """El mismo estado que con el hueco: la tarjeta lo pinta como
        «dual-layer sin confirmar» porque no hay `dovi` en la BL."""
        disco = self._apocalypse()
        self.tb.fail("dovi_tool", "extract-rpu")
        bd, _, _ = await phase_a.run_full_analysis(str(disco), self.log)
        self.assertTrue(bd.has_fel)
        self.assertIsNone(bd.video_tracks[0].dovi)
        self.assertEqual(bd.fel_reason, tr('phase_a.dolby_vision_dual_layer_mediainfo'))

    async def test_un_disco_hdr10_no_lanza_dovi_tool(self):
        bd, _, _ = await phase_a.run_full_analysis(str(self._apocalypse(el=False)), self.log)
        self.assertFalse(bd.has_fel)
        self.assertFalse(self._muestreo_la_el())
        self.assertEqual(bd.fel_reason, tr('phase_a.sin_capa_de_mejora_dolby_vision_detectada'))

    async def test_tambien_en_modo_serie(self):
        disco = self._apocalypse()
        bd, _ = await phase_a.run_full_analysis_for_mpls(
            str(disco), str(disco / "BDMV" / "PLAYLIST" / "00004.mpls"), self.log)
        self.assertTrue(self._muestreo_la_el())
        self.assertEqual(bd.video_tracks[0].dovi.el_type, "FEL")

    async def test_tambien_con_un_m2ts_suelto(self):
        disco = self._apocalypse()
        self.tb.define_mkv("00007.m2ts", tracks=PISTAS_REAUTORADO, duration_s=PELICULA_S)
        bd, _ = await phase_a.run_full_analysis_for_m2ts(
            str(disco / "BDMV" / "STREAM" / "00007.m2ts"), self.log)
        self.assertTrue(self._muestreo_la_el())
        self.assertEqual(bd.video_tracks[0].dovi.el_type, "FEL")


class TestLoQueYaFuncionaba(_ConDisco):

    async def test_un_disco_de_estudio_conserva_su_razon(self):
        """Con el hueco, `_detect_fel` ya la vio y su razón es la que queda
        si dovi_tool no confirma."""
        disco = self._apocalypse(pistas=PISTAS_ESTUDIO)
        self.tb.fail("dovi_tool", "extract-rpu")
        bd, _, _ = await phase_a.run_full_analysis(str(disco), self.log)
        self.assertTrue(bd.has_fel)
        self.assertEqual(bd.fel_reason, tr('phase_a.dolby_vision_dual_layer_gap_en_ids',
                                           first_non_video=2))

    async def test_un_disco_de_estudio_sigue_confirmandose(self):
        bd, _, _ = await phase_a.run_full_analysis(
            str(self._apocalypse(pistas=PISTAS_ESTUDIO)), self.log)
        self.assertEqual(bd.video_tracks[0].dovi.el_type, "FEL")


class TestElHdr10DeMediainfo(_ConDisco):

    async def test_maxcll_y_maxfall_llegan_aunque_traigan_unidad(self):
        """MediaInfo da «1000 cd/m2»; el `int()` de antes lo dejaba en None
        en las 19 sesiones del NAS que lo traían."""
        bd, _, _ = await phase_a.run_full_analysis(str(self._apocalypse()), self.log)
        hdr = bd.video_tracks[0].hdr
        self.assertEqual((hdr.max_cll, hdr.max_fall), (1000, 400))


if __name__ == "__main__":
    unittest.main()
