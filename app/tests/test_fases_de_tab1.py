"""Las fases D y E de Tab 1, ejecutadas.

`run_phase_d` (extracción a un MKV intermedio) y `run_phase_e_direct` (el MKV
final con selección, orden, nombres, flags y capítulos en una pasada) no tenían
ningún test que las ejecutara. Lo que hay *dentro* sí está cubierto
—`_match_tracks_to_source`, `_check_reordering` y `_propedit_track_edits` en
`test_track_mapping`, y los predicados del fallback en
`test_playlist_fallback`— pero la orquestación no: qué argv acaba recibiendo
mkvmerge, qué se traduce al log y cómo se reacciona a cada forma de fallo.

Y ahí está el historial de bugs de Tab 1:

  · **el crash de mkvmerge con playlists UHD multi-segmento** (Avatar Fuego y
    Ceniza, 2025): aborta con SIGABRT, `returncode` llega como **-6**, y el
    chequeo `>= 2` que había NO capturaba los códigos negativos. El flujo
    seguía hasta el `stat()` del output inexistente y reventaba con un
    críptico "[Errno 2] No such file or directory";
  · **`Progress: XX%`** es un contrato del parser del panel de cola: la fase
    traduce el `#GUI#progress` de `--gui-mode` y descarta el resto de `#GUI#`.
    Si eso se rompe, la barra deja de moverse sin que nada falle;
  · **el output que no existe**: mkvmerge puede salir con 0 y no haber escrito
    nada. Hay un guard explícito.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_fases_de_tab1 -v
"""
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import CollectingLog, FakeToolbox  # noqa: E402
from models import (  # noqa: E402
    Chapter, IncludedAudioTrack, IncludedSubtitleTrack, RawAudioTrack,
    RawSubtitleTrack, Session,
)

ASSERTION = ("mkvmerge: ../../src/merge/generic_packetizer.cpp:123: "
             "Assertion 'file_names.size() == play_items.size()' failed.")

# La señal con la que se mata el binario falso para simular un crash.
#
# **SIGTERM a propósito, NO SIGABRT ni SIGSEGV.** Lo que el código bajo prueba
# necesita es un `returncode` NEGATIVO (murió por señal), y cualquier señal
# vale para eso. Pero en macOS un proceso que muere con SIGABRT o SIGSEGV
# dispara el informador de fallos del sistema: cada test dejaba un
# `Python-*.ips` en ~/Library/Logs/DiagnosticReports y una notificación de
# "Python se ha cerrado inesperadamente". Pasar la suite generaba una ráfaga de
# avisos que no significaban nada.
SENAL_DE_CRASH = signal.SIGTERM


def _audio(language, codec, label, *, flag_default=False, position=0, ch=0):
    return IncludedAudioTrack(
        position=position,
        raw=RawAudioTrack(codec=codec, language=language, bitrate_kbps=0,
                          description=f"{ch}.0" if ch else ""),
        language_literal=label.split()[0], codec_literal=label, label=label,
        flag_default=flag_default, selection_reason="test",
    )


def _sub(language, subtitle_type, label, *, flag_forced=False, position=0):
    return IncludedSubtitleTrack(
        position=position,
        raw=RawSubtitleTrack(language=language,
                             bitrate_kbps=1.0 if subtitle_type == "forced" else 30.0,
                             description="", packet_count=0),
        language_literal=label.split()[0], subtitle_type=subtitle_type,
        label=label, flag_default=False, flag_forced=flag_forced,
        selection_reason="test",
    )


class FaseTestCase(unittest.IsolatedAsyncioTestCase):
    """tmpdir + binarios falsos + OUTPUT_DIR redirigido."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tab1_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.trabajo = self.tmp / "tmp"
        self.salida = self.tmp / "output"
        self.disco = self.tmp / "bd" / "Peli_2024_1"
        (self.disco / "BDMV" / "PLAYLIST").mkdir(parents=True)
        for d in (self.trabajo, self.salida):
            d.mkdir(parents=True)

        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)

        from phases import phase_d, phase_e
        self.d, self.e = phase_d, phase_e
        self._orig_out = phase_e.OUTPUT_DIR
        phase_e.OUTPUT_DIR = str(self.salida)
        self.addCleanup(lambda: setattr(phase_e, "OUTPUT_DIR", self._orig_out))

        # Un MPLS con tamaño suficiente para pasar el umbral de find_main_mpls.
        self.mpls = self.disco / "BDMV" / "PLAYLIST" / "00800.mpls"
        self.mpls.write_bytes(b"\x00" * 4096)
        self.log = CollectingLog()

    def _sesion(self, **kw):
        base = {
            "id": "Peli_2024_1700000000",
            "iso_path": str(self.tmp / "Peli.iso"),
            "mkv_name": "Peli (2024) [DV FEL].mkv",
        }
        base.update(kw)
        return Session(**base)


# ══════════════════════════════════════════════════════════════════════
#  Fase D — extracción al intermedio
# ══════════════════════════════════════════════════════════════════════

class TestFaseD(FaseTestCase):

    async def _correr(self, **kw):
        return await self.d.run_phase_d(
            str(self.disco), tmp_dir=str(self.trabajo),
            log_callback=self.log, **kw)

    async def test_extrae_el_mpls_principal_al_intermedio(self):
        salida = await self._correr()
        self.assertTrue(Path(salida).exists(), salida)
        self.assertTrue(salida.endswith("_intermediate.mkv"), salida)
        llamada = self.tb.one("mkvmerge")
        self.assertIn("--gui-mode", llamada.argv)
        self.assertEqual(llamada.opt_name("-o"), Path(salida).name)
        self.assertIn(str(self.mpls), llamada.argv)

    async def test_con_source_path_no_busca_el_mpls_principal(self):
        """Modo serie (un MPLS concreto) y modo m2ts suelto."""
        m2ts = self.disco / "BDMV" / "STREAM" / "00044.m2ts"
        m2ts.parent.mkdir(parents=True, exist_ok=True)
        m2ts.write_bytes(b"\x00" * 1024)
        salida = await self._correr(source_path=str(m2ts))
        self.assertIn(str(m2ts), self.tb.one("mkvmerge").argv)
        self.assertIn("00044_intermediate.mkv", salida,
                      "el stem del intermedio sale del origen")

    async def test_traduce_el_progreso_al_contrato_del_panel(self):
        """`#GUI#progress 45%` → `Progress: 45%`, y el resto de `#GUI#` fuera."""
        await self._correr()
        progresos = [l for l in self.log.lines if l.startswith("Progress: ")]
        self.assertTrue(progresos, self.log.lines)
        self.assertIn("Progress: 100%", progresos)
        self.assertEqual([l for l in self.log.lines if l.startswith("#GUI#")], [],
                         "las líneas de control #GUI# no deben llegar al log")

    async def test_el_sigabrt_del_playlist_se_reconoce(self):
        """EL BUG DE AVATAR. Aborta con SIGABRT (returncode -6) y el chequeo
        `>= 2` no lo capturaba."""
        self.tb.fail("mkvmerge", "*", stderr=ASSERTION + "\n",
                     senal=SENAL_DE_CRASH)
        with self.assertRaises(self.d.MkvmergePlaylistError):
            await self._correr()

    async def test_un_codigo_de_salida_anomalo_lanza(self):
        self.tb.fail("mkvmerge", "*", rc=2, stderr="algo fue mal\n")
        with self.assertRaises(RuntimeError) as ctx:
            await self._correr()
        self.assertIn("código 2", str(ctx.exception))

    async def test_un_codigo_negativo_por_senal_se_reporta_COMO_CRASH(self):
        """Sin la línea del assertion es un crash genérico. Lo que importa no es
        que lance —el guard del output ausente también lo haría— sino QUÉ dice.

        Con el `>= 2` antiguo, un `returncode` negativo pasaba de largo y el
        fallo se enmascaraba: lo que llegaba al usuario era "no generó el MKV",
        que apunta al sitio equivocado. El mensaje tiene que nombrar el código
        de salida."""
        self.tb.fail("mkvmerge", "*", stderr="boom\n", senal=SENAL_DE_CRASH)
        with self.assertRaises(RuntimeError) as ctx:
            await self._correr()
        self.assertNotIsInstance(ctx.exception, self.d.MkvmergePlaylistError)
        self.assertIn("de forma anómala", str(ctx.exception),
                      f"el crash se ha enmascarado: {ctx.exception}")
        self.assertIn(str(-int(SENAL_DE_CRASH)), str(ctx.exception))

    async def test_el_codigo_1_son_avisos_y_no_aborta(self):
        """mkvmerge sale con 1 por warnings no fatales; el MKV es válido."""
        salida = await self._correr()
        self.assertTrue(Path(salida).exists())

    async def test_si_mkvmerge_no_escribe_nada_lanza(self):
        """Sale con 0 y no genera el intermedio. Sin el guard, el `stat()` de
        después revienta con "[Errno 2] No such file or directory"."""
        self.tb.mkvmerge_sin_salida()
        with self.assertRaises(RuntimeError) as ctx:
            await self._correr()
        self.assertIn("no generó el MKV intermedio", str(ctx.exception))

    async def test_sin_ficheros_mpls_lanza(self):
        self.mpls.unlink()
        with self.assertRaises(RuntimeError):
            await self._correr()

    async def test_registra_el_comando_completo(self):
        await self._correr()
        self.assertTrue(self.log.says("$ mkvmerge"), self.log.lines)
        self.assertTrue(self.log.says("📋"), "falta el plan de la fase")
        self.assertTrue(self.log.says("🎯"), "falta el resultado de la fase")


# ══════════════════════════════════════════════════════════════════════
#  Fase E — el MKV final, en una pasada
# ══════════════════════════════════════════════════════════════════════

class TestFaseEDirecta(FaseTestCase):

    def setUp(self):
        super().setUp()
        # El disco: vídeo + dos audios + dos subs, como los ve mkvmerge -J.
        self.tb.define_mkv(self.mpls.name, tracks=[
            {"type": "video", "codec": "HEVC/H.265/MPEG-H",
             "dimensions": "3840x2160", "fps": 23.976},
            {"type": "audio", "codec": "TrueHD Atmos", "language": "spa", "channels": 8},
            {"type": "audio", "codec": "DTS-HD Master Audio", "language": "eng", "channels": 6},
            {"type": "subtitles", "codec": "HDMV PGS", "language": "spa"},
            {"type": "subtitles", "codec": "HDMV PGS", "language": "eng"},
        ])

    def _sesion_completa(self):
        return self._sesion(
            included_tracks=[
                _audio("Spanish", "Dolby TrueHD/Atmos Audio",
                       "Castellano TrueHD Atmos 7.1", flag_default=True, ch=8),
                _audio("English", "DTS-HD Master Audio",
                       "Inglés DTS-HD MA 5.1", position=1, ch=6),
                _sub("Spanish", "forced", "Castellano Forzados (PGS)",
                     flag_forced=True, position=2),
                _sub("English", "complete", "Inglés Completos (PGS)", position=3),
            ],
            chapters=[Chapter(number=1, timestamp="00:00:00.000", name="Capítulo 01"),
                      Chapter(number=2, timestamp="00:10:00.000", name="Capítulo 02")],
        )

    async def _correr(self, session=None, source=None):
        return await self.e.run_phase_e_direct(
            session or self._sesion_completa(),
            source or str(self.mpls),
            log_callback=self.log)

    async def test_escribe_el_mkv_final_en_output(self):
        salida = await self._correr()
        self.assertEqual(Path(salida).parent, self.salida)
        self.assertEqual(Path(salida).name, "Peli (2024) [DV FEL].mkv")
        self.assertTrue(Path(salida).exists())

    async def test_crea_los_subdirectorios_del_modo_serie(self):
        """`mkv_name` de serie lleva ruta: sin `mkdir(parents=True)` mkvmerge
        falla porque el directorio padre no existe."""
        s = self._sesion_completa()
        s.mkv_name = "Serie (2024)/Season 01/Serie (2024) - S01E01 - Piloto.mkv"
        salida = await self._correr(session=s)
        self.assertTrue(Path(salida).exists(), salida)
        self.assertTrue((self.salida / "Serie (2024)" / "Season 01").is_dir())

    async def test_el_titulo_del_contenedor_sale_del_nombre_sin_extension(self):
        await self._correr()
        self.assertEqual(self.tb.find("mkvmerge", None)[-1].opt("--title"),
                         "Peli (2024) [DV FEL]")

    async def test_pasa_los_capitulos_como_xml(self):
        await self._correr()
        mux = self.tb.find("mkvmerge")[-1]
        chapters = mux.opt("--chapters")
        self.assertIsNotNone(chapters, mux.argv)
        self.assertTrue(chapters.endswith(".xml"), chapters)

    async def test_el_xml_de_capitulos_se_borra_al_terminar(self):
        await self._correr()
        self.assertEqual(list(Path(tempfile.gettempdir()).glob("*chapters*.xml")), [],
                         "el XML temporal de capítulos no se ha borrado")

    async def test_sin_capitulos_no_pasa_la_opcion(self):
        s = self._sesion_completa()
        s.chapters = []
        await self._correr(session=s)
        self.assertIsNone(self.tb.find("mkvmerge")[-1].opt("--chapters"))

    async def test_el_orden_de_pistas_pone_el_video_primero(self):
        await self._correr()
        orden = self.tb.find("mkvmerge")[-1].opt("--track-order")
        self.assertIsNotNone(orden)
        self.assertTrue(orden.startswith("0:0"), orden)
        # 1 vídeo + 2 audios + 2 subs
        self.assertEqual(len(orden.split(",")), 5, orden)

    async def test_el_sigabrt_del_playlist_se_reconoce(self):
        self.tb.fail("mkvmerge", "*", stderr=ASSERTION + "\n",
                     senal=SENAL_DE_CRASH)
        with self.assertRaises(self.e.MkvmergePlaylistError):
            await self._correr()

    async def test_un_codigo_negativo_por_senal_se_reporta_COMO_CRASH(self):
        """Igual que en la Fase D: el mensaje tiene que nombrar el código, no
        derivar al guard del output ausente."""
        self.tb.fail("mkvmerge", "*", stderr="boom\n", senal=SENAL_DE_CRASH)
        with self.assertRaises(RuntimeError) as ctx:
            await self._correr()
        self.assertIn("de forma anómala", str(ctx.exception),
                      f"el crash se ha enmascarado: {ctx.exception}")

    async def test_si_mkvmerge_no_escribe_nada_lanza(self):
        """Puede salir con 0 y no haber generado el fichero. Sin el guard, el
        `stat()` de después revienta con un críptico "[Errno 2] No such file"
        en vez de decir qué pasó."""
        self.tb.mkvmerge_sin_salida()
        with self.assertRaises(RuntimeError) as ctx:
            await self._correr()
        self.assertIn("no generó el MKV final", str(ctx.exception))

    async def test_traduce_el_progreso(self):
        await self._correr()
        self.assertIn("Progress: 100%",
                      [l for l in self.log.lines if l.startswith("Progress: ")])


if __name__ == "__main__":
    unittest.main()
