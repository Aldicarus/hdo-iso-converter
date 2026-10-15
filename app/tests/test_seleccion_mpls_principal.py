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

  · un MPLS que dura menos de la mitad que el más largo no es el título,
    tenga las pistas que tenga — y la duración es la de sus TRAMOS: un trozo
    de 34 s del m2ts de la película no «pesa» como la película;
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
    _clips_del_mpls, _duracion_de_segmentos, _hms, _segmentos_del_mpls,
    elegir_mpls_principal, m2ts_principal_del_mpls, run_mkvmerge_identify,
)

# Tamaños a escala (los ficheros son dispersos, pero no hace falta pedirle
# 97 GB al disco de CI). Lo que decide la elección es la DURACIÓN de los
# tramos del MPLS; el tamaño solo decide qué m2ts mira MediaInfo.
PELICULA = 20_000_000
CLIP_TRAILER = 60_000
PELICULA_S = 8340      # 2 h 19 min
TRAILER_S = 99         # cada uno de los seis clips del tráiler: 9 min 54 s
TICKS = 45000


# ── MPLS sintéticos ─────────────────────────────────────────────────────

def _u16(v: int) -> bytes:
    return int(v).to_bytes(2, "big")


def _u32(v: int) -> bytes:
    return int(v).to_bytes(4, "big")


def _playitem(clip: str, in_ticks: int = 0, out_ticks: int = TICKS * 60) -> bytes:
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


def _tramo(entrada) -> tuple[str, int, int]:
    """`"00000"` (un minuto), `("00000", segundos)` o `("00000", in, out)`."""
    if isinstance(entrada, str):
        return entrada, 0, TICKS * 60
    if len(entrada) == 2:
        return entrada[0], 0, round(entrada[1] * TICKS)
    return entrada[0], round(entrada[1] * TICKS), round(entrada[2] * TICKS)


def _mpls(entradas: list) -> bytes:
    playlist = bytearray(_u32(0) + _u16(0) + _u16(len(entradas)) + _u16(0))
    playlist += b"".join(_playitem(*_tramo(e)) for e in entradas)
    playlist[0:4] = _u32(len(playlist) - 4)
    marks = _u32(2) + _u16(0)
    header = bytearray(40)
    header[0:4] = b"MPLS"
    header[4:8] = b"0200"
    header[8:12] = _u32(40)
    header[12:16] = _u32(40 + len(playlist))
    return bytes(header) + bytes(playlist) + marks


def _disco(raiz: Path, playlists: dict[str, list],
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

def _cand(nombre: str, audios: int, segundos: float | None = None,
          duracion: float = 0) -> dict:
    """Un candidato como lo arma `_identificar_candidatos`: `segundos` es el
    `playlist_duration` de mkvmerge y `duracion`, lo leído del binario."""
    props = {} if segundos is None else {"playlist_duration": int(segundos * 1e9)}
    return {"path": Path(nombre), "duracion": duracion,
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
            _cand("00018.mpls", 2, 594.7),
            _cand("00000.mpls", 2, PELICULA_S),
        ]), "00000.mpls")

    def test_un_trozo_de_la_pelicula_no_compite_con_ella(self):
        """The Mandalorian and Grogu: `00001.mpls` reproduce del segundo 11,7
        al 45,2 del MISMO m2ts que la película. Midiendo bytes «pesaba» como
        ella; con una pista de audio más, ganaba."""
        self.assertEqual(_elegido([
            _cand("00001.mpls", 3, 33.5),
            _cand("00800.mpls", 2, 7925.4),
        ]), "00800.mpls")

    def test_un_extra_con_mas_audios_tampoco_gana(self):
        """Un extra con pista de comentarios no se convierte en la película
        por tener una pista más."""
        self.assertEqual(_elegido([
            _cand("00020.mpls", 4, 1500),
            _cand("00800.mpls", 3, 8000),
        ]), "00800.mpls")

    def test_dos_versiones_de_la_pelicula_siguen_compitiendo(self):
        """Montaje del cine y extendido duran parecido: gana el de más
        audios, como siempre — y no el más largo."""
        self.assertEqual(_elegido([
            _cand("00801.mpls", 3, 9200),
            _cand("00800.mpls", 4, 8000),
        ]), "00800.mpls")

    def test_a_igualdad_se_conserva_el_desempate_de_antes(self):
        """Mismos audios y los dos elegibles: el primero de la lista, que es
        el .mpls más grande — lo que elegía la regla vieja."""
        self.assertEqual(_elegido([
            _cand("00800.mpls", 3, 8000),
            _cand("00801.mpls", 3, 9200),
        ]), "00800.mpls")

    def test_lo_leido_del_binario_manda_sobre_mkvmerge(self):
        """mkvmerge suma los PlayItems sin deduplicar: un bucle de 50 vueltas
        le parece el más largo. El binario cuenta cada tramo una vez."""
        self.assertEqual(_elegido([
            _cand("00099.mpls", 2, 50 * 400, duracion=400),
            _cand("00000.mpls", 2, PELICULA_S, duracion=PELICULA_S),
        ]), "00000.mpls")

    def test_sin_ninguna_duracion_es_la_regla_de_siempre(self):
        self.assertEqual(_elegido([
            _cand("00018.mpls", 2), _cand("00000.mpls", 2),
            _cand("00001.mpls", 1),
        ]), "00018.mpls")

    def test_sin_audio_no_hay_titulo(self):
        self.assertIsNone(_elegido([_cand("00000.mpls", 0, PELICULA_S)]))

    def test_uno_sin_audio_no_cuenta_como_el_mas_largo(self):
        """Un MPLS sin audio larguísimo (un ángulo, el fondo de un menú) no
        deja fuera a la película por la vía de la duración."""
        self.assertEqual(_elegido([
            _cand("00999.mpls", 0, 10 * PELICULA_S),
            _cand("00000.mpls", 2, PELICULA_S),
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

    def test_los_tramos_con_sus_tiempos(self):
        f = self.tmp / "x.mpls"
        f.write_bytes(_mpls([("00001", 11.7, 45.2), ("00002", 600)]))
        self.assertEqual(_segmentos_del_mpls(str(f)),
                         [("00001", round(11.7 * TICKS), round(45.2 * TICKS)),
                          ("00002", 0, 600 * TICKS)])

    def test_un_tramo_en_bucle_cuenta_una_vez(self):
        """Un playlist que repite un tramo —el fondo de un menú en bucle— no
        dura más por repetirlo. Contándolo cada vez, cien vueltas a un clip
        de un minuto durarían como una película."""
        self.assertEqual(_duracion_de_segmentos([("00050", 0, 60 * TICKS)] * 100), 60)

    def test_un_trozo_del_m2ts_dura_lo_que_dura_el_trozo(self):
        """Lo que reproduce un playlist es su tramo, no el fichero entero."""
        self.assertAlmostEqual(_duracion_de_segmentos(
            [("00001", round(11.7 * TICKS), round(45.2 * TICKS))]), 33.5, places=2)

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
        playlists = {"00000.mpls": [("00000", PELICULA_S)]}
        m2ts = {"00000": PELICULA}
        for i in range(n_trailers):
            clips = [f"{100 + 6 * i + k:05d}" for k in range(6)]
            playlists[f"{18 + i:05d}.mpls"] = [(c, TRAILER_S) for c in clips]
            m2ts.update({c: CLIP_TRAILER for c in clips})
            self.tb.define_mkv(f"{18 + i:05d}.mpls", tracks=PISTAS_TRAILER,
                               duration_s=6 * TRAILER_S,
                               playlist_size=6 * CLIP_TRAILER)
        self.tb.define_mkv("00000.mpls", tracks=PISTAS_PELICULA,
                           duration_s=PELICULA_S, playlist_size=PELICULA)
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
            "phase_a.titulo_elegido_menor_que_otro",
            mpls="00018.mpls", dur_titulo=_hms(6 * TRAILER_S),
            ref="00000.mpls", dur_ref=_hms(PELICULA_S))
        self.assertIn(esperado, self.log.lines)

    async def test_el_aviso_mira_la_pelicula_entera_no_su_mayor_segmento(self):
        """Película troceada en diez segmentos (seamless branching) e
        ilegible para mkvmerge. El tráiler no llega a la mitad de la
        película, pero sí a la mitad de su segmento mayor: con el m2ts más
        grande como referencia, no se avisaba."""
        segmentos = [f"{k:05d}" for k in range(10)]
        disco = _disco(self.tmp / "Troceada",
                       {"00800.mpls": [(c, PELICULA_S / 10) for c in segmentos],
                        "00018.mpls": [("00100", 600)]},
                       {**{c: PELICULA // 10 for c in segmentos},
                        "00100": int(PELICULA / 10 * 0.75)})
        self.tb.define_mkv("00018.mpls", tracks=PISTAS_TRAILER, duration_s=600,
                           playlist_size=int(PELICULA / 10 * 0.75))
        self.tb.fail_when_arg("mkvmerge", "00800.mpls", rc=2)
        _, mpls = await run_mkvmerge_identify(str(disco), self.log)
        self.assertEqual(Path(mpls).name, "00018.mpls")
        self.assertTrue(
            any("00800.mpls" in l and "⚠️" in l for l in self.log.lines),
            self.log.text)

    async def test_un_bucle_no_deja_fuera_a_la_pelicula(self):
        """El fondo de un menú con música: un tramo repetido cincuenta veces.
        mkvmerge suma los PlayItems sin deduplicar, así que para él dura más
        del doble que la película; midiendo con eso, la película no pasaba
        el filtro de la mitad."""
        disco = _disco(self.tmp / "Bucle",
                       {"00000.mpls": [("00000", PELICULA_S)],
                        "00099.mpls": [("00099", 400)] * 50},
                       {"00000": PELICULA, "00099": PELICULA // 20})
        self.tb.define_mkv("00000.mpls", tracks=PISTAS_PELICULA,
                           duration_s=PELICULA_S, playlist_size=PELICULA)
        self.tb.define_mkv("00099.mpls", tracks=PISTAS_TRAILER,
                           duration_s=50 * 400.0,
                           playlist_size=50 * (PELICULA // 20))
        _, mpls = await run_mkvmerge_identify(str(disco), self.log)
        self.assertEqual(Path(mpls).name, "00000.mpls")

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

    async def test_el_trozo_de_the_mandalorian(self):
        """El caso real: `00001.mpls` reproduce 33,5 s del mismo m2ts que la
        película y aquí trae una pista de audio más. Midiendo bytes ganaba
        él; midiendo lo que dura, ni compite ni sale como «otra versión»."""
        disco = _disco(self.tmp / "Mandalorian",
                       {"00800.mpls": [("00001", 11.7, 7937.1)],
                        "00001.mpls": [("00001", 11.7, 45.2)]},
                       {"00001": PELICULA})
        self.tb.define_mkv("00800.mpls", tracks=PISTAS_PELICULA,
                           duration_s=7925.4, playlist_size=PELICULA)
        self.tb.define_mkv("00001.mpls", tracks=PISTAS_PELICULA + [
            {"type": "audio", "codec": "AC-3", "language": "eng", "channels": 2}],
            duration_s=33.5, playlist_size=PELICULA)
        avisos = []
        _, mpls = await run_mkvmerge_identify(str(disco), self.log, avisos=avisos)
        self.assertEqual(Path(mpls).name, "00800.mpls")
        self.assertEqual(avisos, [])

    async def test_un_disco_normal_no_avisa(self):
        disco = _disco(self.tmp / "Normal",
                       {"00800.mpls": [("00800", PELICULA_S)],
                        "00801.mpls": [("00801", 120)]},
                       {"00800": PELICULA, "00801": CLIP_TRAILER})
        self.tb.define_mkv("00800.mpls", tracks=PISTAS_PELICULA,
                           duration_s=PELICULA_S, playlist_size=PELICULA)
        self.tb.define_mkv("00801.mpls", tracks=PISTAS_TRAILER,
                           duration_s=120.0, playlist_size=CLIP_TRAILER)
        _, mpls = await run_mkvmerge_identify(str(disco), self.log)
        self.assertEqual(Path(mpls).name, "00800.mpls")
        self.assertFalse(self.log.says("⚠️"), self.log.text)


# ── Avisos: se avisa, no se corrige ────────────────────────────────────

class TestAvisosDelTitulo(unittest.IsolatedAsyncioTestCase):
    """Lo que la Fase A no puede resolver sola se apunta en
    `bdinfo_result.avisos_titulo` y el usuario decide. Ninguno de estos
    tests cambia QUÉ se elige: solo qué se dice."""

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mpls_sel_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)
        self.log = CollectingLog()

    async def _avisos(self, disco) -> tuple[str, list[dict]]:
        avisos = []
        _, mpls = await run_mkvmerge_identify(str(disco), self.log, avisos=avisos)
        return Path(mpls).name, avisos

    def _version(self, nombre: str, minutos: float) -> None:
        self.tb.define_mkv(nombre, tracks=PISTAS_PELICULA,
                           duration_s=minutos * 60)

    async def test_dos_montajes_de_la_pelicula(self):
        """Cine y extendido: los dos pasan el filtro y la regla elige uno.
        El aviso nombra el otro con su duración."""
        disco = _disco(self.tmp / "Montajes",
                       {"00800.mpls": [("00010", 2700), ("00011", 2700), ("00012", 2820)],
                        "00801.mpls": [("00010", 2700), ("00011", 2700),
                                       ("00013", 1020), ("00012", 2820)]},
                       {"00010": PELICULA // 3, "00011": PELICULA // 3,
                        "00012": PELICULA // 3, "00013": PELICULA // 8})
        self._version("00800.mpls", 137)
        self._version("00801.mpls", 154)
        elegido, avisos = await self._avisos(disco)
        versiones = [a for a in avisos if a["tipo"] == "varias_versiones"]
        self.assertEqual(len(versiones), 1, avisos)
        self.assertEqual(versiones[0]["mpls"], elegido)
        otra = {"00800.mpls", "00801.mpls"} - {elegido}
        self.assertEqual([o["mpls"] for o in versiones[0]["otras"]], list(otra))
        self.assertTrue(self.log.says("⚠️"), self.log.text)

    async def test_unos_segundos_de_diferencia_no_son_otra_version(self):
        """Un aviso legal o unos créditos localizados no son otro montaje."""
        disco = _disco(self.tmp / "Creditos",
                       {"00800.mpls": [("00010", 8200), ("00011", 30)],
                        "00801.mpls": [("00010", 8200), ("00012", 60)]},
                       {"00010": PELICULA, "00011": CLIP_TRAILER,
                        "00012": CLIP_TRAILER})
        self._version("00800.mpls", 137)
        self._version("00801.mpls", 137.5)
        _, avisos = await self._avisos(disco)
        self.assertEqual(avisos, [])

    async def test_playlists_senuelo(self):
        """Los mismos segmentos en otro orden, tres veces: el disco esconde
        la película. La elección no cambia; se avisa."""
        orden = ["00010", "00011", "00012", "00013"]
        playlists = {"00800.mpls": orden}
        for i, perm in enumerate((["00011", "00010", "00012", "00013"],
                                  ["00010", "00012", "00011", "00013"],
                                  ["00013", "00012", "00011", "00010"])):
            playlists[f"{900 + i:05d}.mpls"] = perm
        disco = _disco(self.tmp / "Ofuscado", playlists,
                       {c: PELICULA // 4 for c in orden})
        self._version("00800.mpls", 137)
        elegido, avisos = await self._avisos(disco)
        self.assertEqual(elegido, "00800.mpls")
        self.assertEqual([a for a in avisos if a["tipo"] == "playlists_ofuscados"],
                         [{"tipo": "playlists_ofuscados", "mpls": "00800.mpls", "n": 3}])

    async def test_dos_senuelos_no_bastan(self):
        orden = ["00010", "00011", "00012"]
        disco = _disco(self.tmp / "Dos",
                       {"00800.mpls": orden,
                        "00900.mpls": ["00011", "00010", "00012"],
                        "00901.mpls": ["00012", "00011", "00010"]},
                       {c: PELICULA // 3 for c in orden})
        self._version("00800.mpls", 137)
        _, avisos = await self._avisos(disco)
        self.assertEqual(avisos, [])

    async def test_un_disco_normal_no_trae_avisos(self):
        disco = _disco(self.tmp / "Normal",
                       {"00800.mpls": [("00800", 8220)], "00801.mpls": [("00801", 120)]},
                       {"00800": PELICULA, "00801": CLIP_TRAILER})
        self._version("00800.mpls", 137)
        self.tb.define_mkv("00801.mpls", tracks=PISTAS_TRAILER, duration_s=120)
        _, avisos = await self._avisos(disco)
        self.assertEqual(avisos, [])

    async def test_los_avisos_quedan_en_la_sesion(self):
        """`run_full_analysis` los deja en `bdinfo_result`, que es lo que
        persiste y lo que la interfaz lee. Con el MPLS de la película
        ilegible, el elegido es el tráiler y lo dice."""
        playlists = {"00000.mpls": [("00000", PELICULA_S)],
                     "00018.mpls": [("00100", 594)]}
        disco = _disco(self.tmp / "Divergente", playlists,
                       {"00000": PELICULA, "00100": CLIP_TRAILER})
        self.tb.define_mkv("00018.mpls", tracks=PISTAS_TRAILER, duration_s=594)
        self.tb.fail_when_arg("mkvmerge", "00000.mpls", rc=2)
        bdinfo, _, _ = await phase_a.run_full_analysis(str(disco), self.log)
        self.assertEqual(bdinfo.avisos_titulo, [{
            "tipo": "titulo_menor", "mpls": "00018.mpls",
            "segundos_titulo": 594, "ref": "00000.mpls",
            "segundos_ref": PELICULA_S}])


class TestElReintentoConMasTiempo(unittest.IsolatedAsyncioTestCase):
    """Con el NAS cargado, el `mkvmerge -J` de la película puede pasar de
    30 s. Quedarse fuera por eso es elegir otra cosa: se le da un segundo
    intento más largo. A un tráiler no se le espera."""

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mpls_sel_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)
        self.log = CollectingLog()
        self.disco = _disco(self.tmp / "Cargado",
                            {"00000.mpls": [("00000", PELICULA_S)],
                             "00018.mpls": [("00100", 594)]},
                            {"00000": PELICULA, "00100": CLIP_TRAILER})
        self.tb.define_mkv("00000.mpls", tracks=PISTAS_PELICULA, duration_s=8340)
        self.tb.define_mkv("00018.mpls", tracks=PISTAS_TRAILER, duration_s=594.7)

    def _se_agota_la_primera_vez(self, *nombres):
        """`_mkvmerge_j` que se agota en el primer intento con esos MPLS.
        El agotamiento se SIMULA: depender del reloj haría el test frágil
        bajo carga, y lo que se prueba es la decisión de reintentar."""
        real = phase_a._mkvmerge_j
        self.llamadas = []

        async def _espia(path, timeout=None):
            self.llamadas.append((Path(path).name, timeout))
            veces = sum(1 for n, _ in self.llamadas if n == Path(path).name)
            if Path(path).name in nombres and veces == 1:
                return None, True
            return await real(path, timeout)

        p = mock.patch.object(phase_a, "_mkvmerge_j", _espia)
        p.start()
        self.addCleanup(p.stop)

    async def test_la_pelicula_se_reintenta_y_se_elige(self):
        self._se_agota_la_primera_vez("00000.mpls")
        _, mpls = await run_mkvmerge_identify(str(self.disco), self.log)
        self.assertEqual(Path(mpls).name, "00000.mpls")
        self.assertIn(("00000.mpls", phase_a.IDENTIFY_TIMEOUT_REINTENTO),
                      self.llamadas)
        self.assertTrue(self.log.says("00000.mpls"), self.log.text)

    async def test_un_trailer_que_se_agota_no_se_reintenta(self):
        self._se_agota_la_primera_vez("00018.mpls")
        _, mpls = await run_mkvmerge_identify(str(self.disco), self.log)
        self.assertEqual(Path(mpls).name, "00000.mpls")
        self.assertEqual([n for n, _ in self.llamadas].count("00018.mpls"), 1)

    async def test_agotarse_de_verdad_se_distingue_de_fallar(self):
        """El `True` de `_mkvmerge_j` sale del timeout real, no del fake:
        un mkvmerge que no contesta a tiempo."""
        lento = self.tmp / "mkvmerge_lento"
        lento.write_text("#!/bin/sh\nsleep 5\n")
        lento.chmod(0o755)
        with mock.patch.object(phase_a, "MKVMERGE_BIN", str(lento)):
            data, agotado = await phase_a._mkvmerge_j("x.mpls", timeout=0.2)
        self.assertEqual((data, agotado), (None, True))
        self.tb.fail_when_arg("mkvmerge", "00018.mpls", rc=2)
        data, agotado = await phase_a._mkvmerge_j(
            str(self.disco / "BDMV" / "PLAYLIST" / "00018.mpls"))
        self.assertEqual((data, agotado), (None, False))


# ── Modo serie: la misma criba ──────────────────────────────────────────

class TestLosEpisodiosEntranEnLaCriba(unittest.IsolatedAsyncioTestCase):
    """El modo serie cribaba igual —los 20 .mpls más grandes— y un episodio
    es un clip, o sea un .mpls pequeño. Detrás de veinte playlists de
    navegación de muchos clips no llegaba a la lista de la que el usuario
    elige, y nada lo decía."""

    async def test_un_episodio_con_mpls_pequeno_no_se_pierde(self):
        tmp = Path(tempfile.mkdtemp(prefix="mpls_sel_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tb = FakeToolbox(tmp)
        tb.install()
        self.addCleanup(tb.uninstall)
        playlists = {f"{900 + i:05d}.mpls": [f"{500 + i:05d}"] * 8
                     for i in range(21)}
        m2ts = {f"{500 + i:05d}": CLIP_TRAILER for i in range(21)}
        for ep in ("00801", "00802", "00803"):
            playlists[f"{ep}.mpls"] = [(ep, 45 * 60)]
            m2ts[ep] = PELICULA // 4
            tb.define_mkv(f"{ep}.mpls", tracks=PISTAS_PELICULA,
                          duration_s=45 * 60.0, playlist_size=PELICULA // 4)
        disco = _disco(tmp / "Serie", playlists, m2ts)
        candidatos = await phase_a.identify_episode_candidates(str(disco))
        self.assertEqual([c["mpls_name"] for c in candidatos],
                         ["00801.mpls", "00802.mpls", "00803.mpls"])


class TestUnaCabeceraCompartidaNoFusionaLosEpisodios(unittest.IsolatedAsyncioTestCase):
    """Cada episodio abre con el mismo clip —la cabecera de un anime—. El
    m2ts «del episodio» era el PRIMERO de su `playlist_file`, o sea la
    cabecera para todos: el dedupe los fusionaba como variantes del mismo
    título y el filtro de tamaño medía la cabecera."""

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mpls_sel_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)
        playlists, m2ts = {}, {"00700": CLIP_TRAILER}
        for ep in ("00801", "00802", "00803"):
            playlists[f"{ep}.mpls"] = [("00700", 90), (ep, 45 * 60 - 90)]
            m2ts[ep] = PELICULA // 4
            self.tb.define_mkv(f"{ep}.mpls", tracks=PISTAS_PELICULA,
                               duration_s=45 * 60.0)
        self.disco = _disco(self.tmp / "Anime", playlists, m2ts)

    async def test_el_mkvmerge_falso_da_la_cabecera_primero(self):
        """La premisa: sin esto el test de abajo no probaría nada."""
        data = await phase_a._run_mkvmerge_j(
            str(self.disco / "BDMV" / "PLAYLIST" / "00802.mpls"))
        primero = data["container"]["properties"]["playlist_file"][0]
        self.assertEqual(Path(primero).name, "00700.m2ts")

    async def test_salen_los_tres_episodios(self):
        candidatos = await phase_a.identify_episode_candidates(str(self.disco))
        self.assertEqual([c["mpls_name"] for c in candidatos],
                         ["00801.mpls", "00802.mpls", "00803.mpls"])
        self.assertEqual([Path(c["m2ts_resolved_path"]).name for c in candidatos],
                         ["00801.m2ts", "00802.m2ts", "00803.m2ts"])


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
