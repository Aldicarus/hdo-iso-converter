"""La validación del MKV final de Tab 1, ejecutada.

`_validate_final_mkv` (214 líneas, complejidad 50) compara el MKV que acaba de
producir el pipeline contra lo que la sesión decía que debía salir, y escribe un
informe en el log. No tenía ningún test.

Es la última red antes de dar un job por bueno, y ya ha dado un falso ❌ de los
que hacen perder una tarde: su tabla ISO 639 era **un subset propio e
incompleto**, así que en un disco con catalán decía «esperado: catalan, real:
cat» aunque la pista estuviera perfectamente extraída (Avatar Fuego y Ceniza,
2025). Ahora se deriva de `ISO639_TO_ENGLISH`, y eso es lo que este test fija:
la regla del proyecto es que **cualquier normalización ISO 639 parte del mapa
canónico, nunca de un subset local**.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_validacion_final -v
"""
import shutil
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


def _audio(language, codec, label):
    return IncludedAudioTrack(
        position=0,
        raw=RawAudioTrack(codec=codec, language=language, bitrate_kbps=0,
                          description=""),
        language_literal=label.split()[0], codec_literal=label, label=label,
        flag_default=False, selection_reason="test",
    )


def _sub(language, subtitle_type, label):
    return IncludedSubtitleTrack(
        position=0,
        raw=RawSubtitleTrack(language=language, bitrate_kbps=30.0,
                             description="", packet_count=0),
        language_literal=label.split()[0], subtitle_type=subtitle_type,
        label=label, flag_default=False, flag_forced=False,
        selection_reason="test",
    )


class ValidacionCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="valida_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)

        # `_validate_final_mkv` salió de `main.py` con el resto de Tab 1.
        from routers import tab1
        self.main = tab1
        self.mkv = self.tmp / "Peli (2024).mkv"
        self.mkv.write_bytes(b"\x00" * 4096)
        self.log = CollectingLog()

    def _sesion(self, *, audios=(), subs=(), chapters=(), has_fel=False):
        return Session(
            id="Peli_2024_1700000000",
            iso_path=str(self.tmp / "Peli.iso"),
            mkv_name=self.mkv.name,
            has_fel=has_fel,
            included_tracks=list(audios) + list(subs),
            chapters=list(chapters),
        )

    def _declarar_mkv(self, tracks, capitulos=()):
        self.tb.define_mkv(self.mkv.name, tracks=tracks)
        if capitulos:
            self.tb.define_chapters(self.mkv.name, list(capitulos))

    async def _validar(self, session):
        return await self.main._validate_final_mkv(session, str(self.mkv), self.log)

    # ── formas del disco, reutilizadas ───────────────────────────────
    VIDEO = {"type": "video", "codec": "HEVC/H.265/MPEG-H",
             "dimensions": "3840x2160", "fps": 23.976}


class TestElCasoCorrecto(ValidacionCase):

    async def test_todo_cuadra(self):
        self._declarar_mkv([
            self.VIDEO,
            {"type": "audio", "codec": "TrueHD Atmos", "language": "spa",
             "track_name": "Castellano TrueHD Atmos 7.1", "default": True},
            {"type": "subtitles", "codec": "HDMV PGS", "language": "spa",
             "track_name": "Castellano Forzados (PGS)", "forced": True},
        ], capitulos=[("00:00:00.000", "Capítulo 01")])
        s = self._sesion(
            audios=[_audio("Spanish", "Dolby TrueHD/Atmos Audio",
                           "Castellano TrueHD Atmos 7.1")],
            subs=[_sub("Spanish", "forced", "Castellano Forzados (PGS)")],
            chapters=[Chapter(number=1, timestamp="00:00:00.000", name="Capítulo 01")],
        )
        self.assertTrue(await self._validar(s), self.log.text)
        self.assertTrue(self.log.says("✅ Verificación correcta"), self.log.text)


class TestElTierDelCodec(ValidacionCase):
    """La etiqueta dice una cosa y el stream es otra.

    Hasta ahora la validación comparaba idioma, flags y capítulos — nunca el
    codec. Así que un MKV con la pista llamada «Castellano TrueHD Atmos 7.1»
    y un AC-3 dentro se daba por bueno sin una sola línea en el log. Es
    exactamente lo que producen los dos bugs que ha tenido el matcher de Fase
    E (el P0 posicional y el core AC-3 subordinado del TrueHD), y esto es lo
    que hace que se vean en PRODUCCIÓN y no solo en un test.
    """

    async def test_el_core_ac3_disfrazado_de_truehd_se_caza(self):
        self._declarar_mkv([
            self.VIDEO,
            # lo que de verdad quedó dentro: el core AC-3, no el TrueHD
            {"type": "audio", "codec": "AC-3", "language": "spa", "channels": 6,
             "track_name": "Castellano TrueHD Atmos 7.1", "default": True},
        ])
        s = self._sesion(audios=[
            _audio("Spanish", "Dolby TrueHD/Atmos Audio", "Castellano TrueHD Atmos 7.1")])
        self.assertFalse(await self._validar(s), self.log.text)
        self.assertTrue(self.log.says("codec esperado: TrueHD Atmos"), self.log.text)
        self.assertTrue(self.log.says("real: DD"), self.log.text)

    async def test_dts_pelado_donde_se_esperaba_dts_hd_ma(self):
        self._declarar_mkv([
            self.VIDEO,
            {"type": "audio", "codec": "DTS", "language": "spa", "channels": 6,
             "track_name": "Castellano DTS-HD MA 5.1"},
        ])
        s = self._sesion(audios=[
            _audio("Spanish", "DTS-HD Master Audio", "Castellano DTS-HD MA 5.1")])
        self.assertFalse(await self._validar(s), self.log.text)

    async def test_el_tier_correcto_pasa(self):
        self._declarar_mkv([
            self.VIDEO,
            {"type": "audio", "codec": "TrueHD Atmos", "language": "spa",
             "channels": 8, "track_name": "Castellano TrueHD Atmos 7.1"},
        ])
        s = self._sesion(audios=[
            _audio("Spanish", "Dolby TrueHD/Atmos Audio", "Castellano TrueHD Atmos 7.1")])
        self.assertTrue(await self._validar(s), self.log.text)

    async def test_ddplus_atmos_se_distingue_de_ddplus_por_canales(self):
        """mkvmerge no distingue el Atmos de un DD+ en el nombre del codec, así
        que el tier sale de los canales — la misma heurística que phase_a."""
        self._declarar_mkv([
            self.VIDEO,
            {"type": "audio", "codec": "E-AC-3", "language": "spa", "channels": 6,
             "track_name": "Castellano DD+ Atmos 7.1"},
        ])
        s = self._sesion(audios=[
            _audio("Spanish", "Dolby Digital Plus Audio", "Castellano DD+ Atmos 7.1")])
        # la sesión espera ddplus_atmos (raw con description '7.1-Atmos')
        s.included_tracks[0].raw.description = "7.1-Atmos / 48 kHz"
        self.assertFalse(await self._validar(s), self.log.text)

    async def test_un_codec_que_no_sabemos_clasificar_no_da_falso_negativo(self):
        """PCM, FLAC y compañía caen en 'unknown'. Un desconocido NUNCA debe
        marcar el job como fallido — sería el falso ❌ del catalán otra vez."""
        self._declarar_mkv([
            self.VIDEO,
            {"type": "audio", "codec": "PCM", "language": "spa", "channels": 2,
             "track_name": "Castellano LPCM 2.0"},
        ])
        s = self._sesion(audios=[
            _audio("Spanish", "LPCM Audio", "Castellano LPCM 2.0")])
        self.assertTrue(await self._validar(s), self.log.text)


class TestLaTablaIso639(ValidacionCase):
    """El falso ❌ que costó una tarde.

    `_iso` era un subset propio de ~24 idiomas. Una pista incluida llega con
    `raw.language` en inglés ("Catalan") y el MKV la identifica por código ISO
    ("cat"); si el código no estaba en el subset, la comparación daba
    «esperado: catalan, real: cat» y el job se marcaba con discrepancias aunque
    la extracción fuera perfecta.
    """

    async def _validar_idioma(self, iso, nombre_ingles):
        self._declarar_mkv([
            self.VIDEO,
            {"type": "audio", "codec": "DTS-HD Master Audio", "language": iso,
             "track_name": f"{nombre_ingles} DTS-HD MA 5.1"},
        ])
        s = self._sesion(audios=[_audio(nombre_ingles, "DTS-HD Master Audio",
                                        f"{nombre_ingles} DTS-HD MA 5.1")])
        ok = await self._validar(s)
        return ok, self.log.text

    async def test_catalan(self):
        """El caso real: Avatar Fuego y Ceniza con pista catalana."""
        ok, texto = await self._validar_idioma("cat", "Catalan")
        self.assertTrue(ok, f"falso ❌ con el catalán:\n{texto}")

    async def test_los_habituales_y_los_raros(self):
        for iso, nombre in (("spa", "Spanish"), ("eng", "English"),
                            ("cat", "Catalan"), ("tha", "Thai"),
                            ("ell", "Greek"), ("ces", "Czech"),
                            ("jpn", "Japanese"), ("kor", "Korean")):
            with self.subTest(iso=iso):
                self.log.lines.clear()
                ok, texto = await self._validar_idioma(iso, nombre)
                self.assertTrue(ok, f"falso ❌ con {nombre} ({iso}):\n{texto}")

    def test_la_tabla_se_deriva_de_la_canonica(self):
        """Regla del proyecto: cualquier normalización ISO 639 parte de
        `ISO639_TO_ENGLISH`, nunca de un subset local. Se comprueba por
        cobertura: la tabla de la validación tiene que cubrir el mapa entero."""
        from phases.phase_a import ISO639_TO_ENGLISH
        src = (APP_DIR / "routers" / "tab1.py").read_text(encoding="utf-8")
        self.assertIn(
            "_iso = {code: name.lower() for code, name in ISO639_TO_ENGLISH.items()}",
            src,
            "la validación ha vuelto a tener su propia tabla ISO 639")
        # Los que el subset viejo NO tenía y por eso daban falso ❌.
        for iso in ("cat", "tha", "ell", "ces", "hun", "ron"):
            self.assertIn(iso, ISO639_TO_ENGLISH,
                          f"{iso} falta en el mapa canónico")

    async def test_un_idioma_de_verdad_distinto_si_da_error(self):
        """El guard tiene que seguir detectando lo que sí es un problema."""
        self._declarar_mkv([
            self.VIDEO,
            {"type": "audio", "codec": "DTS-HD Master Audio", "language": "fra"},
        ])
        s = self._sesion(audios=[_audio("Spanish", "DTS-HD Master Audio",
                                        "Castellano DTS-HD MA 5.1")])
        self.assertFalse(await self._validar(s))
        self.assertTrue(self.log.says("esperado: spanish"), self.log.text)


class TestDiscrepancias(ValidacionCase):

    async def test_falta_una_pista_de_audio(self):
        self._declarar_mkv([self.VIDEO])
        s = self._sesion(audios=[_audio("Spanish", "Dolby TrueHD/Atmos Audio",
                                        "Castellano TrueHD Atmos 7.1")])
        self.assertFalse(await self._validar(s))
        self.assertTrue(self.log.says("Falta la pista de audio"), self.log.text)

    async def test_sobra_una_pista_de_subtitulos(self):
        self._declarar_mkv([
            self.VIDEO,
            {"type": "subtitles", "codec": "HDMV PGS", "language": "spa"},
            {"type": "subtitles", "codec": "HDMV PGS", "language": "eng"},
        ])
        s = self._sesion(subs=[_sub("Spanish", "complete", "Castellano Completos (PGS)")])
        self.assertFalse(await self._validar(s))
        self.assertTrue(self.log.says("pista extra"), self.log.text)

    async def test_sin_video_falla(self):
        self._declarar_mkv([{"type": "audio", "codec": "TrueHD Atmos",
                             "language": "spa"}])
        s = self._sesion(audios=[_audio("Spanish", "Dolby TrueHD/Atmos Audio",
                                        "Castellano TrueHD Atmos 7.1")])
        self.assertFalse(await self._validar(s))
        self.assertTrue(self.log.says("no tiene pistas de vídeo"), self.log.text)

    async def test_el_fichero_que_no_existe(self):
        self.mkv.unlink()
        self.assertFalse(await self._validar(self._sesion()))
        self.assertTrue(self.log.says("no existe"), self.log.text)

    async def test_si_mkvmerge_no_puede_leerlo(self):
        self.tb.fail("mkvmerge", "*", rc=2, stderr="no es un MKV\n")
        self.assertFalse(await self._validar(self._sesion()))

    async def test_el_resumen_lista_el_diagnostico(self):
        """Cuando algo no cuadra, el log tiene que traer con qué depurarlo."""
        self._declarar_mkv([self.VIDEO])
        s = self._sesion(audios=[_audio("Spanish", "Dolby TrueHD/Atmos Audio",
                                        "Castellano TrueHD Atmos 7.1")])
        await self._validar(s)
        for pista in ("Datos para diagnóstico", "Sesión ID:", "Origen:",
                      "Audio esperado #1", "Fin del diagnóstico"):
            self.assertTrue(self.log.says(pista), f"falta «{pista}»")


class TestDolbyVisionFel(ValidacionCase):

    async def test_una_sola_pista_es_lo_correcto_con_mkvmerge_v81(self):
        """v81+ combina BL+EL en una pista; dos pistas era el comportamiento
        de v65 y rompía el DV."""
        self._declarar_mkv([self.VIDEO])
        self.assertTrue(await self._validar(self._sesion(has_fel=True)))
        self.assertTrue(self.log.says("base + enhancement combinados"), self.log.text)

    async def test_el_el_en_pista_separada_avisa_pero_no_falla(self):
        self._declarar_mkv([
            self.VIDEO,
            {"type": "video", "codec": "HEVC/H.265/MPEG-H",
             "dimensions": "1920x1080", "fps": 23.976},
        ])
        self.assertTrue(await self._validar(self._sesion(has_fel=True)))
        self.assertTrue(self.log.says("requiere mkvmerge v81+"), self.log.text)

    async def test_dos_pistas_sin_el_de_1080_si_falla(self):
        self._declarar_mkv([
            self.VIDEO,
            {"type": "video", "codec": "HEVC/H.265/MPEG-H",
             "dimensions": "3840x2160", "fps": 23.976},
        ])
        self.assertFalse(await self._validar(self._sesion(has_fel=True)))

    async def test_sin_fel_no_se_comprueba(self):
        self._declarar_mkv([self.VIDEO])
        self.assertTrue(await self._validar(self._sesion(has_fel=False)))
        self.assertFalse(self.log.says("Dolby Vision"), self.log.text)


class TestCapitulos(ValidacionCase):
    """Se cuentan EXTRAYÉNDOLOS, no leyendo `num_entries` del JSON."""

    async def test_cuenta_los_capitulos_extraidos(self):
        self._declarar_mkv([self.VIDEO], capitulos=[
            ("00:00:00.000", "Capítulo 01"), ("00:10:00.000", "Capítulo 02")])
        s = self._sesion(chapters=[
            Chapter(number=1, timestamp="00:00:00.000", name="Capítulo 01"),
            Chapter(number=2, timestamp="00:10:00.000", name="Capítulo 02")])
        self.assertTrue(await self._validar(s))
        self.assertTrue(self.log.says("Capítulos: 2"), self.log.text)

    async def test_un_desajuste_de_capitulos_avisa_pero_no_invalida(self):
        """Es un ⚠️, no un ❌: el MKV sigue siendo usable."""
        self._declarar_mkv([self.VIDEO], capitulos=[("00:00:00.000", "Capítulo 01")])
        s = self._sesion(chapters=[
            Chapter(number=1, timestamp="00:00:00.000", name="Capítulo 01"),
            Chapter(number=2, timestamp="00:10:00.000", name="Capítulo 02")])
        self.assertTrue(await self._validar(s), "un desajuste de capítulos no invalida")
        self.assertTrue(self.log.says("1 en el MKV vs 2 esperados"), self.log.text)


if __name__ == "__main__":
    unittest.main()
