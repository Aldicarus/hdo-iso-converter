"""La Fase A elige la PELÍCULA, no un tráiler que empata con ella.

Caso real, octubre de 2026: Divergente (2014), de un disco «Custom U-HD». La
Fase A eligió `00018.mpls` —9 min 55 s, AVC 1080p, dos AC-3 2.0 en inglés— en
vez de la película (`00000.m2ts`, 97,1 GB, HEVC con Dolby Vision, DTS-HD MA
7.1 en castellano e inglés). El MKV habría salido sin castellano, sin DV y con
un capítulo.

La regla era «los 10 .mpls más grandes; el de más pistas de audio; a igualdad,
el primero». El tamaño de un .mpls mide cuántos clips y pistas lista, no lo que
dura, así que con dos audios en cada lado ganaba el tráiler. Y de propina,
MediaInfo analizaba el m2ts más grande del DISCO —la película— y pegaba sus
datos a las pistas del tráiler: un AC-3 2.0 salía etiquetado «DTS-HD MA 2.0».

Lo que se fija aquí:

  · un MPLS que reproduce menos de la mitad que el mayor no es el título,
    tenga las pistas que tenga;
  · la película entra en la criba aunque su .mpls sea de los pequeños;
  · si el MPLS de la película no se puede leer, la Fase A lo DICE;
  · MediaInfo describe el título elegido, no el m2ts más grande del disco;
  · «Restaurar capítulos del disco» lee el MPLS de la sesión, no otro;
  · y en los discos donde la regla vieja acertaba, nada cambia.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_seleccion_mpls_principal -v
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

from api_harness import ApiTestCase  # noqa: E402
from cmv40_harness import CollectingLog, FakeToolbox  # noqa: E402
from i18n import t as tr  # noqa: E402
from phases import phase_a  # noqa: E402
from phases.phase_a import (  # noqa: E402
    _clips_del_mpls, _contenido_de_clips, elegir_mpls_principal,
    m2ts_principal_del_mpls, run_mkvmerge_identify,
)

# Tamaños a escala (los ficheros son dispersos, pero no hace falta pedirle
# 97 GB al disco de CI): lo que decide son las proporciones.
PELICULA = 20_000_000
CLIP_TRAILER = 60_000


# ── MPLS sintéticos ─────────────────────────────────────────────────────

def _u16(v: int) -> bytes:
    return int(v).to_bytes(2, "big")


def _u32(v: int) -> bytes:
    return int(v).to_bytes(4, "big")


def _playitem(clip: str, in_ticks: int = 0, out_ticks: int = 45000 * 60) -> bytes:
    """PlayItem mínimo, con el layout que leen `_clips_del_mpls` y
    `_parse_mpls_marks_binary`: el clip en +0, IN en +12 y OUT en +16."""
    body = bytearray()
    body += clip.encode("ascii")  # +0  Clip_Information_file_name (5)
    body += b"M2TS"               # +5  Clip_codec_identifier
    body += _u16(0)               # +9  flags (is_multi_angle=0)
    body += bytes([0])            # +11 ref_to_STC_id
    body += _u32(in_ticks)        # +12 IN_time
    body += _u32(out_ticks)       # +16 OUT_time
    body += bytes(12)             # UO_mask(8) + flags + still_mode + still_time
    return _u16(len(body)) + bytes(body)


def _mpls(clips: list[str]) -> bytes:
    playlist = bytearray(_u32(0) + _u16(0) + _u16(len(clips)) + _u16(0))
    playlist += b"".join(_playitem(c) for c in clips)
    playlist[0:4] = _u32(len(playlist) - 4)
    marks = _u32(2) + _u16(0)
    header = bytearray(40)
    header[0:4] = b"MPLS"
    header[4:8] = b"0200"
    header[8:12] = _u32(40)
    header[12:16] = _u32(40 + len(playlist))
    return bytes(header) + bytes(playlist) + marks


def _disco(raiz: Path, playlists: dict[str, list[str]],
           m2ts: dict[str, int]) -> Path:
    """Un disco extraído: `BDMV/PLAYLIST/*.mpls` y `BDMV/STREAM/*.m2ts`."""
    (raiz / "BDMV" / "PLAYLIST").mkdir(parents=True)
    (raiz / "BDMV" / "STREAM").mkdir(parents=True)
    for nombre, clips in playlists.items():
        (raiz / "BDMV" / "PLAYLIST" / nombre).write_bytes(_mpls(clips))
    for clip, tam in m2ts.items():
        with open(raiz / "BDMV" / "STREAM" / f"{clip}.m2ts", "wb") as f:
            f.truncate(tam)
    return raiz


# Lo que `mkvmerge -J` vio en el log de Divergente, y lo que habría visto en
# la película (la tabla de MediaInfo del mismo log).
PISTAS_TRAILER = [
    {"type": "video", "codec": "AVC/H.264/MPEG-4p10", "dimensions": "1920x1080"},
    {"type": "audio", "codec": "AC-3", "language": "eng", "channels": 2},
    {"type": "audio", "codec": "AC-3", "language": "eng", "channels": 2},
    {"type": "subtitles", "codec": "HDMV PGS", "language": "eng"},
    {"type": "subtitles", "codec": "HDMV PGS", "language": "eng"},
]
PISTAS_PELICULA = [
    {"type": "video", "codec": "HEVC/H.265/MPEG-H", "dimensions": "3840x2160"},
    {"type": "audio", "codec": "DTS-HD Master Audio", "language": "spa", "channels": 8},
    {"type": "audio", "codec": "DTS-HD Master Audio", "language": "eng", "channels": 8},
    {"type": "subtitles", "codec": "HDMV PGS", "language": "spa"},
    {"type": "subtitles", "codec": "HDMV PGS", "language": "eng"},
]


# ── La regla, sin disco ─────────────────────────────────────────────────

def _cand(nombre: str, audios: int, tam: int | None = None,
          contenido: int = 0) -> dict:
    props = {} if tam is None else {"playlist_size": tam}
    return {"path": Path(nombre), "contenido": contenido,
            "data": {"container": {"properties": props},
                     "tracks": [{"type": "audio"}] * audios}}


def _elegido(candidatos) -> str | None:
    c = elegir_mpls_principal(candidatos)
    return c["path"].name if c else None


class TestLaRegla(unittest.TestCase):

    def test_divergente_el_trailer_no_gana_el_empate(self):
        """Los dos con dos audios, el tráiler primero en la lista (su .mpls
        es más grande). La regla vieja elegía el tráiler."""
        self.assertEqual(_elegido([
            _cand("00018.mpls", 2, 1_619_625_984),
            _cand("00000.mpls", 2, 97_100_000_000),
        ]), "00000.mpls")

    def test_un_extra_con_mas_audios_tampoco_gana(self):
        """Un extra con pista de comentarios no se convierte en la película
        por tener una pista más."""
        self.assertEqual(_elegido([
            _cand("00020.mpls", 4, 3_000_000_000),
            _cand("00800.mpls", 3, 70_000_000_000),
        ]), "00800.mpls")

    def test_dos_versiones_de_la_pelicula_siguen_compitiendo(self):
        """Montaje del cine y extendido reproducen casi lo mismo: gana el de
        más audios, como siempre — y no el que más reproduce."""
        self.assertEqual(_elegido([
            _cand("00801.mpls", 3, 64_000_000_000),
            _cand("00800.mpls", 4, 60_000_000_000),
        ]), "00800.mpls")

    def test_a_igualdad_se_conserva_el_desempate_de_antes(self):
        """Mismos audios y los dos elegibles: el primero de la lista, que es
        el .mpls más grande — lo que elegía la regla vieja."""
        self.assertEqual(_elegido([
            _cand("00800.mpls", 3, 60_000_000_000),
            _cand("00801.mpls", 3, 64_000_000_000),
        ]), "00800.mpls")

    def test_sin_playlist_size_vale_el_contenido_leido_del_binario(self):
        self.assertEqual(_elegido([
            _cand("00018.mpls", 2, contenido=1_600_000_000),
            _cand("00000.mpls", 2, contenido=97_000_000_000),
        ]), "00000.mpls")

    def test_sin_ningun_tamano_es_la_regla_de_siempre(self):
        self.assertEqual(_elegido([
            _cand("00018.mpls", 2), _cand("00000.mpls", 2),
            _cand("00001.mpls", 1),
        ]), "00018.mpls")

    def test_sin_audio_no_hay_titulo(self):
        self.assertIsNone(_elegido([_cand("00000.mpls", 0, 9_000_000_000)]))

    def test_uno_sin_audio_no_cuenta_como_el_mayor(self):
        """Un MPLS sin audio enorme (un ángulo, un loop de menú) no deja
        fuera a la película por la vía del tamaño."""
        self.assertEqual(_elegido([
            _cand("00999.mpls", 0, 200_000_000_000),
            _cand("00000.mpls", 2, 60_000_000_000),
        ]), "00000.mpls")


class TestLeerElMplsBinario(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mpls_sel_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_los_clips_en_orden(self):
        f = self.tmp / "x.mpls"
        f.write_bytes(_mpls(["00012", "00013", "00012"]))
        self.assertEqual(_clips_del_mpls(str(f)), ["00012", "00013", "00012"])

    def test_lo_que_no_es_un_mpls(self):
        f = self.tmp / "x.mpls"
        f.write_bytes(b"no soy un mpls" * 10)
        self.assertIsNone(_clips_del_mpls(str(f)))
        self.assertIsNone(_clips_del_mpls(str(self.tmp / "no_existe.mpls")))

    def test_un_clip_en_bucle_cuenta_una_vez(self):
        """Un playlist que repite un clip —el fondo de un menú en bucle— no
        reproduce más contenido por repetirlo. Contándolo cada vez, cien
        vueltas a un clip pequeño pesarían como una película."""
        disco = _disco(self.tmp / "d", {}, {"00050": CLIP_TRAILER})
        self.assertEqual(
            _contenido_de_clips(disco / "BDMV" / "STREAM", ["00050"] * 100),
            CLIP_TRAILER)

    def test_mediainfo_describe_el_titulo_elegido_no_el_disco(self):
        """El m2ts que se le pasa a MediaInfo es el mayor de los que
        reproduce ESE MPLS. Con el más grande del disco, los datos de la
        película acababan pegados a las pistas del tráiler."""
        disco = _disco(self.tmp / "d",
                       {"00018.mpls": ["00010", "00011"], "00000.mpls": ["00000"]},
                       {"00000": PELICULA, "00010": CLIP_TRAILER,
                        "00011": CLIP_TRAILER * 2})
        self.assertEqual(
            Path(m2ts_principal_del_mpls(str(disco / "BDMV/PLAYLIST/00018.mpls"))).name,
            "00011.m2ts")
        self.assertEqual(
            Path(m2ts_principal_del_mpls(str(disco / "BDMV/PLAYLIST/00000.mpls"))).name,
            "00000.m2ts")


# ── run_mkvmerge_identify, con mkvmerge falso ──────────────────────────

class TestRunMkvmergeIdentify(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mpls_sel_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)
        self.log = CollectingLog()

    def _divergente(self, n_trailers: int = 1):
        """La película en un MPLS de un solo clip —un .mpls pequeño— y uno o
        varios tráilers de seis clips cada uno, con .mpls más grandes."""
        playlists = {"00000.mpls": ["00000"]}
        m2ts = {"00000": PELICULA}
        for i in range(n_trailers):
            clips = [f"{100 + 6 * i + k:05d}" for k in range(6)]
            playlists[f"{18 + i:05d}.mpls"] = clips
            m2ts.update({c: CLIP_TRAILER for c in clips})
            self.tb.define_mkv(f"{18 + i:05d}.mpls", tracks=PISTAS_TRAILER,
                               duration_s=594.7,
                               playlist_size=6 * CLIP_TRAILER)
        self.tb.define_mkv("00000.mpls", tracks=PISTAS_PELICULA,
                           duration_s=8340.0, playlist_size=PELICULA)
        return _disco(self.tmp / "Divergente", playlists, m2ts)

    async def test_divergente_elige_la_pelicula(self):
        disco = self._divergente()
        _, mpls = await run_mkvmerge_identify(str(disco), self.log)
        self.assertEqual(Path(mpls).name, "00000.mpls")
        self.assertFalse(self.log.says("⚠️"), self.log.text)

    async def test_la_pelicula_entra_aunque_su_mpls_sea_de_los_pequenos(self):
        """Once tráilers con .mpls más grandes que el de la película: la
        criba vieja (los 10 .mpls más grandes) ni la miraba."""
        disco = self._divergente(n_trailers=11)
        _, mpls = await run_mkvmerge_identify(str(disco), self.log)
        self.assertEqual(Path(mpls).name, "00000.mpls")

    async def test_si_la_pelicula_no_se_puede_leer_lo_dice(self):
        """mkvmerge no sabe leer el MPLS de la película: lo mejor que queda
        es el tráiler, y la Fase A tiene que avisar en vez de seguir callada."""
        disco = self._divergente()
        self.tb.fail_when_arg("mkvmerge", "00000.mpls", rc=2)
        _, mpls = await run_mkvmerge_identify(str(disco), self.log)
        self.assertEqual(Path(mpls).name, "00018.mpls")
        esperado = "[Fase A] " + tr(
            "phase_a.titulo_elegido_menor_que_el_m2ts",
            mpls="00018.mpls", gb_titulo=format(6 * CLIP_TRAILER / 1e9, ".1f"),
            m2ts="00000.m2ts", gb_m2ts=format(PELICULA / 1e9, ".1f"))
        self.assertIn(esperado, self.log.lines)

    async def test_si_mkvmerge_muere_por_senal_tampoco_cuenta(self):
        """Un `returncode` negativo no es «salió bien», aunque mkvmerge haya
        llegado a escribir el JSON: con el chequeo `>= 2` de antes, ese JSON
        se daba por bueno."""
        import signal
        disco = self._divergente()
        self.tb.fail_when_arg("mkvmerge", "00000.mpls", senal=signal.SIGTERM,
                              tras_producir=True)
        _, mpls = await run_mkvmerge_identify(str(disco), self.log)
        self.assertEqual(Path(mpls).name, "00018.mpls")

    async def test_mediainfo_analiza_el_m2ts_del_titulo_elegido(self):
        """Ejecuta la Fase A entera. Con la película ilegible, el elegido es
        el tráiler y MediaInfo tiene que describir el TRÁILER: con el m2ts
        más grande del disco, el AC-3 2.0 heredaba el «DTS-HD MA» de la
        película."""
        disco = self._divergente()
        self.tb.fail_when_arg("mkvmerge", "00000.mpls", rc=2)
        await phase_a.run_full_analysis(str(disco), self.log)
        analizado = Path(self.tb.one("mediainfo").argv[-1]).name
        self.assertNotEqual(analizado, "00000.m2ts")
        self.assertIn(analizado, {f"{100 + k:05d}.m2ts" for k in range(6)})

    async def test_mediainfo_en_un_disco_normal_sigue_siendo_la_pelicula(self):
        disco = self._divergente()
        await phase_a.run_full_analysis(str(disco), self.log)
        self.assertEqual(Path(self.tb.one("mediainfo").argv[-1]).name, "00000.m2ts")

    async def test_un_disco_normal_no_avisa(self):
        disco = _disco(self.tmp / "Normal",
                       {"00800.mpls": ["00800"], "00801.mpls": ["00801"]},
                       {"00800": PELICULA, "00801": CLIP_TRAILER})
        self.tb.define_mkv("00800.mpls", tracks=PISTAS_PELICULA,
                           duration_s=8340.0, playlist_size=PELICULA)
        self.tb.define_mkv("00801.mpls", tracks=PISTAS_TRAILER,
                           duration_s=120.0, playlist_size=CLIP_TRAILER)
        _, mpls = await run_mkvmerge_identify(str(disco), self.log)
        self.assertEqual(Path(mpls).name, "00800.mpls")
        self.assertFalse(self.log.says("⚠️"), self.log.text)


# ── Restaurar capítulos: el MPLS de la sesión ──────────────────────────

class TestRestaurarCapitulosLeeElMplsDeLaSesion(ApiTestCase):
    """`reset-chapters` volvía a elegir el MPLS en vez de usar el de la
    sesión. En una serie restauraba los capítulos del título «más de
    película» del disco; con la regla nueva, en una sesión creada con la
    vieja, los de otro título."""

    def setUp(self):
        super().setUp()
        self.disco = _disco(self.isos_dir / "Divergente",
                            {"00000.mpls": ["00000"], "00018.mpls": ["00010"],
                             "00801.mpls": ["00801"]},
                            {"00000": PELICULA, "00010": CLIP_TRAILER,
                             "00801": CLIP_TRAILER})
        self.leidos = []

        def _capitulos(mpls_path):
            self.leidos.append(Path(mpls_path).name)
            return [{"number": 1, "timestamp": "00:00:00.000",
                     "name": "Capítulo 01", "name_custom": False}]

        async def _no_debe_elegir(*a, **kw):
            raise AssertionError("no tenía que volver a elegir el MPLS")

        for attr, nuevo in (("parse_mpls_chapters", _capitulos),
                            ("run_mkvmerge_identify", _no_debe_elegir)):
            p = mock.patch.object(phase_a, attr, nuevo)
            p.start()
            self.addCleanup(p.stop)

    def _pedir_restauracion(self, **campos) -> None:
        sid = self.crear_sesion_tab1(source_type="bdmv_folder",
                                     iso_path=str(self.disco), **campos)
        r = self.client.post(f"/api/sessions/{sid}/reset-chapters")
        self.assertEqual(r.status_code, 200, r.text)

    def test_pelicula_usa_el_mpls_que_eligio_la_fase_a(self):
        from models import BDInfoResult
        self._pedir_restauracion(bdinfo_result=BDInfoResult(
            video_tracks=[], audio_tracks=[], subtitle_tracks=[],
            duration_seconds=594.7, has_fel=False, vo_language="English",
            main_mpls="00018.mpls"))
        self.assertEqual(self.leidos, ["00018.mpls"])

    def test_episodio_usa_el_mpls_del_episodio(self):
        self._pedir_restauracion(media_type="series", mpls_path="00801.mpls",
                        season_number=1, episode_number=1)
        self.assertEqual(self.leidos, ["00801.mpls"])


if __name__ == "__main__":
    unittest.main()
