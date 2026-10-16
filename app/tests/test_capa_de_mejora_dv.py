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
    """Un MediaInfoResult con los vídeos dados como (formato, ancho, alto) o
    (formato, ancho, alto, PID). El PID va en `ID`, en decimal, como el real."""
    pistas = [{"@type": "General"}]
    for f, an, al, *pid in videos:
        p = {"@type": "Video", "Format": f, "Width": str(an), "Height": str(al)}
        if pid:
            p["ID"] = str(pid[0])
        pistas.append(p)
    return MediaInfoResult(source_path="x.m2ts", raw_json={"media": {"track": pistas}})


def _hay_capa(mi, pids=frozenset({4113}), hevc=1) -> bool:
    """La regla con lo que lista mkvmerge en un UHD normal: solo la BL."""
    return capa_de_mejora_en_mediainfo(mi, pids_listados=set(pids) if pids is not None else None,
                                       hevc_listados=hevc)


class TestLaRegla(unittest.TestCase):
    """La EL es un HEVC a la mitad de resolución que otro, y que mkvmerge NO
    lista: solo oculta la pista que empareja como Dolby Vision."""

    def test_la_bl_y_la_el_de_un_uhd(self):
        self.assertTrue(_hay_capa(_mi(("HEVC", 3840, 2160, 4113), ("HEVC", 1920, 1080, 4117))))

    def test_un_uhd_hdr10_no_tiene(self):
        self.assertFalse(_hay_capa(_mi(("HEVC", 3840, 2160, 4113))))

    def test_un_episodio_que_lista_la_bl_dos_veces(self):
        """Juego de Tronos y La Momia, tal como los da MediaInfo en el NAS: un
        «AVC 1920x1080» con el MISMO ID que la BL, además de la EL."""
        self.assertTrue(_hay_capa(_mi(("HEVC", 3840, 2160, 4113), ("AVC", 1920, 1080, 4113),
                                      ("HEVC", 1920, 1080, 4117))))

    def test_la_bl_listada_dos_veces_no_es_una_capa(self):
        """Contar vídeos no basta: un playlist de varios clips puede listar la
        BL dos veces sin que haya Enhancement Layer."""
        self.assertFalse(_hay_capa(_mi(("HEVC", 3840, 2160), ("HEVC", 3840, 2160)), pids=None))

    def test_un_1080p_suelto_no_es_una_capa(self):
        self.assertFalse(_hay_capa(_mi(("HEVC", 1920, 1080, 4113))))

    def test_un_avc_a_1080p_no_es_la_el_de_un_hevc(self):
        self.assertFalse(_hay_capa(_mi(("HEVC", 3840, 2160, 4113), ("AVC", 1920, 1080, 6912))))

    def test_un_hevc_secundario_sin_dolby_vision_no_es_una_capa(self):
        """El falso positivo que encontró la revisión externa: un HEVC a la
        mitad de resolución que mkvmerge LISTA. No lo ha emparejado, así que no
        lleva configuración Dolby Vision — y con la resolución sola salía EL."""
        self.assertFalse(_hay_capa(_mi(("HEVC", 1920, 1080, 4113), ("HEVC", 960, 540, 6912)),
                                   pids={4113, 6912}, hevc=2))

    def test_sin_pids_se_cuentan_los_hevc(self):
        """Si mkvmerge no da PIDs, oculta algo cuando MediaInfo ve más HEVC."""
        mi = _mi(("HEVC", 3840, 2160), ("HEVC", 1920, 1080))
        self.assertTrue(_hay_capa(mi, pids=None, hevc=1))
        self.assertFalse(_hay_capa(mi, pids=None, hevc=2))

    def test_una_el_sin_id_tambien_se_cuenta(self):
        """Con PIDs en mkvmerge pero sin `ID` en MediaInfo, el recuento."""
        mi = _mi(("HEVC", 3840, 2160), ("HEVC", 1920, 1080))
        self.assertTrue(_hay_capa(mi, pids={4113}, hevc=1))
        self.assertFalse(_hay_capa(mi, pids={4113, 4117}, hevc=2))

    def test_un_mismo_pid_cuenta_una_vez(self):
        """Con la EL sin `ID` se cuentan HEVC, y una BL que MediaInfo repita con
        su mismo PID es UNA pista: contarla dos veces la daría por oculta."""
        mi = _mi(("HEVC", 3840, 2160, 4113), ("HEVC", 3840, 2160, 4113),
                 ("HEVC", 1920, 1080))
        self.assertFalse(_hay_capa(mi, pids={4113, 4117}, hevc=2))

    def test_sin_mediainfo_no_se_concluye_nada(self):
        self.assertFalse(_hay_capa(None))
        self.assertFalse(_hay_capa(MediaInfoResult(source_path="x")))

    def test_una_dimension_ilegible_no_lanza(self):
        mi = _mi(("HEVC", 3840, 2160, 4113))
        mi.raw_json["media"]["track"].append({"@type": "Video", "Format": "HEVC"})
        self.assertFalse(_hay_capa(mi))


class TestElPidDeLaMuestra(unittest.TestCase):
    """La muestra para dovi_tool sale del PID de la EL, no del segundo vídeo."""

    def _bd(self, mi, mkvmerge_pids=(4113,), otras=()):
        """Un análisis con la BL de mkvmerge y su JSON: los vídeos con los PIDs
        dados y `otras` pistas como (tipo, PID)."""
        from models import BDInfoResult, VideoTrack
        pistas = [{"type": "video", "properties": {"number": n}} for n in mkvmerge_pids]
        pistas += [{"type": t, "properties": {"number": n}} for t, n in otras]
        return BDInfoResult(
            video_tracks=[VideoTrack(codec="MPEG-H HEVC Video", bitrate_kbps=0,
                                     description="2160p")],
            audio_tracks=[], subtitle_tracks=[], duration_seconds=0,
            has_fel=False, vo_language="English",
            mediainfo_result=mi, mkvmerge_raw={"tracks": pistas})

    def test_es_el_de_la_el(self):
        mi = _mi(("HEVC", 3840, 2160, 4113), ("HEVC", 1920, 1080, 4117))
        self.assertEqual(phase_a.pid_de_la_capa_de_mejora(self._bd(mi)), 4117)

    def test_no_se_confunde_con_otro_video_delante(self):
        mi = _mi(("HEVC", 3840, 2160, 4113), ("AVC", 1920, 1080, 6912),
                 ("HEVC", 1920, 1080, 4117))
        self.assertEqual(phase_a.pid_de_la_capa_de_mejora(self._bd(mi, (4113, 6912))), 4117)

    def test_una_el_que_mkvmerge_lista_tambien_da_su_pid(self):
        """La vía de `_detect_fel` con la EL como pista aparte. Descartarla por
        estar listada devolvía la muestra al segundo vídeo, que aquí es el AVC
        — lo encontró la segunda revisión externa."""
        mi = _mi(("HEVC", 3840, 2160, 4113), ("AVC", 1920, 1080, 6912),
                 ("HEVC", 1920, 1080, 4117))
        self.assertEqual(
            phase_a.pid_de_la_capa_de_mejora(self._bd(mi, (4113, 6912, 4117))), 4117)

    def test_los_pids_de_audio_no_cuentan(self):
        """Solo se comparan los vídeos de mkvmerge."""
        mi = _mi(("HEVC", 3840, 2160, 4113), ("HEVC", 1920, 1080, 4117))
        bd = self._bd(mi, otras=[("audio", 4117)])
        self.assertEqual(phase_a.pids_de_mkvmerge(bd), {4113})
        self.assertEqual(phase_a.pid_de_la_capa_de_mejora(bd), 4117)

    def test_sin_id_no_hay_pid(self):
        mi = _mi(("HEVC", 3840, 2160), ("HEVC", 1920, 1080))
        self.assertIsNone(phase_a.pid_de_la_capa_de_mejora(self._bd(mi)))

    def test_sin_mediainfo_no_hay_pid(self):
        self.assertIsNone(phase_a.pid_de_la_capa_de_mejora(self._bd(None)))


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
                    el_type="FEL", video=None) -> Path:
        self.tb.define_mkv("00004.mpls", tracks=pistas,
                           duration_s=PELICULA_S, playlist_size=M2TS)
        self.tb.define_mediainfo("00007.m2ts", audio=AUDIO_MI,
                                 video=video or {"el": el, "el_stream_order": "0-6"})
        self.tb.define_rpu("00007.m2ts", profile=7, el_type=el_type,
                           cm_version="v2.9")
        return _disco(self.tmp / "Apocalypse", {"00004.mpls": [("00007", PELICULA_S)]},
                      {"00007": M2TS})

    def _muestras(self) -> list[str]:
        """El `-map` de cada muestra que se sacó para dovi_tool."""
        return [c.opt("-map") for c in self.tb.find("ffmpeg") if c.opt("-map")]

    def _muestreo_la_el(self) -> bool:
        """La muestra salió del PID de la EL (4117 en el MediaInfo falso, como
        en los 81 discos del NAS)."""
        return "0:i:4117" in self._muestras()


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
        # Que se INTENTÓ: sin esto, el test pasaba igual si dovi_tool no
        # llegaba a lanzarse, porque el estado final es el mismo.
        self.assertTrue(self._muestreo_la_el())
        self.assertTrue(self.tb.find("dovi_tool", "extract-rpu"))
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


class TestLaMuestraSaleDeLaEl(_ConDisco):
    """`-map 0:v:1` era el segundo vídeo, que es la EL solo si hay dos."""

    # Un disco con un vídeo secundario AVC delante de la EL, que va al final
    # del PMT como en los re-autorados. mkvmerge lista el AVC (no tiene
    # Dolby Vision que emparejar) y oculta la EL.
    PISTAS = ([PISTAS_REAUTORADO[0],
               {"id": 1, "type": "video", "codec": "MPEG-4p10/AVC/H.264",
                "dimensions": "1920x1080", "number": 6912}]
              + [dict(p, id=p["id"] + 1) for p in PISTAS_REAUTORADO[1:]])
    for _p in PISTAS:
        if "multiplexed_tracks" in _p:
            _p["multiplexed_tracks"] = [4, 5]
    VIDEO = {"el": True, "el_stream_order": "0-8",
             "extra": [{"format": "AVC", "width": 1920, "height": 1080,
                        "id": 6912, "stream_order": "0-1"}]}

    async def test_con_otro_video_delante_dovi_tool_confirma_la_el(self):
        bd, _, _ = await phase_a.run_full_analysis(
            str(self._apocalypse(pistas=self.PISTAS, video=self.VIDEO)), self.log)
        self.assertEqual(self._muestras(), ["0:i:4117"])
        self.assertEqual(bd.video_tracks[0].dovi.el_type, "FEL")

    async def test_con_otro_hevc_delante_tambien(self):
        """Entre dos HEVC el códec no delata el error: lo que sale mal es que
        el stream elegido no lleva RPU."""
        pistas = [dict(p) for p in self.PISTAS]
        pistas[1].update(codec="HEVC/H.265/MPEG-H", dimensions="1280x720")
        video = {**self.VIDEO, "extra": [{"format": "HEVC", "width": 1280, "height": 720,
                                          "id": 6912, "stream_order": "0-1"}]}
        bd, _, _ = await phase_a.run_full_analysis(
            str(self._apocalypse(pistas=pistas, video=video)), self.log)
        self.assertEqual(self._muestras(), ["0:i:4117"])
        self.assertEqual(bd.video_tracks[0].dovi.el_type, "FEL")

    async def test_con_la_el_listada_por_mkvmerge_y_otro_video_delante(self):
        """La vía de `_detect_fel` con la EL como pista aparte: también por PID."""
        pistas = ([dict(p) for p in self.PISTAS[:2]]
                  + [{"id": 2, "type": "video", "codec": "HEVC/H.265/MPEG-H",
                      "dimensions": "1920x1080", "number": 4117}]
                  + [dict(p, id=p["id"] + 1) for p in self.PISTAS[2:]])
        for p in pistas:
            if "multiplexed_tracks" in p:
                p["multiplexed_tracks"] = [5, 6]
        bd, _, _ = await phase_a.run_full_analysis(
            str(self._apocalypse(pistas=pistas, video=self.VIDEO)), self.log)
        self.assertEqual(self._muestras(), ["0:i:4117"])
        self.assertEqual(bd.video_tracks[0].dovi.el_type, "FEL")

    async def test_tambien_con_la_el_detectada_por_el_hueco(self):
        """La pista es la misma la vea quien la vea: un disco de estudio
        también se muestrea por PID."""
        bd, _, _ = await phase_a.run_full_analysis(
            str(self._apocalypse(pistas=PISTAS_ESTUDIO)), self.log)
        self.assertEqual(self._muestras(), ["0:i:4117"])
        self.assertEqual(bd.video_tracks[0].dovi.el_type, "FEL")


class TestUnHevcSecundarioSinDolbyVision(_ConDisco):
    """El falso positivo de la revisión externa, en el análisis entero."""

    async def test_no_se_toma_por_una_capa_de_mejora(self):
        pistas = [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H",
             "dimensions": "1920x1080", "number": 4113},
            {"id": 1, "type": "video", "codec": "HEVC/H.265/MPEG-H",
             "dimensions": "960x540", "number": 6912},
        ] + [dict(p, id=p["id"] + 1) for p in PISTAS_REAUTORADO[1:]]
        for p in pistas:
            if "multiplexed_tracks" in p:
                p["multiplexed_tracks"] = [4, 5]
        video = {"width": 1920, "height": 1080,
                 "extra": [{"format": "HEVC", "width": 960, "height": 540,
                            "id": 6912, "stream_order": "0-1"}]}
        bd, _, _ = await phase_a.run_full_analysis(
            str(self._apocalypse(pistas=pistas, video=video)), self.log)
        self.assertFalse(bd.has_fel)
        self.assertEqual(self._muestras(), [], "dovi_tool no debía lanzarse")
        self.assertEqual(bd.fel_reason, tr('phase_a.sin_capa_de_mejora_dolby_vision_detectada'))


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
