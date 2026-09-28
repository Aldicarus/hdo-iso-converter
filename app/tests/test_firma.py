"""La marca de procedencia: que se escriba, que se reconozca y que aguante.

`firma` resuelve una pregunta muy concreta —«¿este MKV salió de esta app?»— y
tiene tres modos de fallo, los tres silenciosos:

  · **que no se escriba.** Los tres puntos de salida (las dos rutas de Fase E
    y la Fase H de CMv4.0) producen el fichero y devuelven su ruta; si la
    llamada a `firmar` desaparece, el MKV sale perfecto y sin marca, y nada
    falla. Por eso los tests de aquí **ejecutan las fases** en vez de buscar
    la llamada en el fuente;
  · **que se rompa con una edición propia.** Lo que más le pasa a un MKV de
    esta app es que su dueño lo abra en Tab 2 y le cambie el título o el
    nombre de una pista. Los rasgos que se firman están elegidos para
    sobrevivir a eso, y es lo que fija `test_sobrevive_a_una_edicion_de_tab2`;
  · **que diga sí de más.** Un veredicto falso positivo es peor que ninguno,
    porque la pregunta que responde es de identidad.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_firma -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import firma  # noqa: E402
from cmv40_harness import (  # noqa: E402
    CollectingLog, FakeToolbox, PhaseTestCase, RpuProps, make_session,
    write_artifacts,
)
from models import (  # noqa: E402
    Chapter, IncludedAudioTrack, IncludedSubtitleTrack, RawAudioTrack,
    RawSubtitleTrack, Session,
)


def _datos(duracion_ns=7200_000_000_000, codecs=("HEVC/H.265/MPEG-H", "TrueHD Atmos"),
           uid=None, titulo="Peli (2024)", nombres=("", "")):
    """Un `mkvmerge -J` de mentira, con lo justo que `firma` mira."""
    propiedades = {"title": titulo, "duration": duracion_ns}
    if uid is not None:
        propiedades["segment_uid"] = uid
    return {
        "container": {"properties": propiedades},
        "tracks": [{"type": "video" if i == 0 else "audio", "codec": c,
                    "properties": {"track_name": nombres[i] if i < len(nombres) else ""}}
                   for i, c in enumerate(codecs)],
    }


# ══════════════════════════════════════════════════════════════════════
#  Los rasgos que se firman
# ══════════════════════════════════════════════════════════════════════

class TestLosRasgos(unittest.TestCase):

    def test_el_titulo_no_entra(self):
        """Es lo que Tab 2 edita: si entrara, renombrar rompería la marca."""
        self.assertEqual(firma.rasgos_de(_datos(titulo="Peli (2024)")),
                         firma.rasgos_de(_datos(titulo="Otro nombre cualquiera")))

    def test_los_nombres_de_pista_no_entran(self):
        self.assertEqual(firma.rasgos_de(_datos(nombres=("", ""))),
                         firma.rasgos_de(_datos(nombres=("Vídeo", "Castellano TrueHD Atmos 7.1"))))

    def test_la_duracion_si_entra(self):
        self.assertNotEqual(firma.rasgos_de(_datos(duracion_ns=7200_000_000_000)),
                            firma.rasgos_de(_datos(duracion_ns=7201_000_000_000)))

    def test_los_codecs_si_entran(self):
        self.assertNotEqual(firma.rasgos_de(_datos(codecs=("HEVC", "TrueHD Atmos"))),
                            firma.rasgos_de(_datos(codecs=("HEVC", "AC-3"))))

    def test_el_numero_de_pistas_si_entra(self):
        """Dos MKV con los mismos codecs pero distinto reparto no son el mismo."""
        self.assertNotEqual(firma.rasgos_de(_datos(codecs=("HEVC", "AC-3"))),
                            firma.rasgos_de(_datos(codecs=("HEVC", "AC-3", "AC-3"))))

    def test_sin_duracion_no_hay_rasgos(self):
        self.assertEqual(firma.rasgos_de(_datos(duracion_ns=0)), "")

    def test_sin_pistas_no_hay_rasgos(self):
        self.assertEqual(firma.rasgos_de({"container": {"properties": {"duration": 1}}}), "")

    def test_un_json_cualquiera_no_revienta(self):
        for basura in ({}, {"container": None}, {"tracks": "no es una lista"},
                       {"container": {"properties": {"duration": "no es un número"}}}):
            self.assertEqual(firma.rasgos_de(basura), "", basura)


# ══════════════════════════════════════════════════════════════════════
#  El veredicto
# ══════════════════════════════════════════════════════════════════════

class TestElVeredicto(unittest.TestCase):

    def test_reconoce_su_propia_firma(self):
        d = _datos()
        self.assertTrue(firma.lleva_nuestra_firma(_datos(uid=firma.firma_de(d))))

    def test_un_uid_ajeno_no_cuela(self):
        self.assertFalse(firma.lleva_nuestra_firma(_datos(uid="43e74fd0b0eefa1a4e83a8ea1dd88f75")))

    def test_sin_uid_no_consta(self):
        self.assertFalse(firma.lleva_nuestra_firma(_datos()))

    def test_la_firma_de_otro_contenido_no_cuela(self):
        """El punto: la marca va atada AL CONTENIDO, no es una constante que
        se pueda copiar de un MKV a otro."""
        otra = firma.firma_de(_datos(duracion_ns=1234_000_000_000))
        self.assertFalse(firma.lleva_nuestra_firma(_datos(uid=otra)))

    def test_el_formato_del_uid_se_normaliza(self):
        """mkvmerge lo da en hex pelado y minúsculas, pero el prefijo `0x` y
        las mayúsculas son igual de válidos en un `SegmentUID`."""
        esperada = firma.firma_de(_datos())
        for variante in (esperada, esperada.upper(), "0x" + esperada, "0X" + esperada.upper()):
            self.assertTrue(firma.lleva_nuestra_firma(_datos(uid=variante)), variante)

    def test_sin_rasgos_no_se_afirma_nada(self):
        """Un MKV que no se puede caracterizar no se puede reconocer, y eso
        NO es «es mío»: con la firma vacía, un uid vacío coincidiría."""
        sin_rasgos = {"container": {"properties": {"segment_uid": ""}}, "tracks": []}
        self.assertEqual(firma.firma_de(sin_rasgos), "")
        self.assertFalse(firma.lleva_nuestra_firma(sin_rasgos))

    def test_la_firma_mide_128_bits(self):
        """Los que caben en un `SegmentUID` de Matroska."""
        self.assertEqual(len(firma.firma_de(_datos())), 32)
        self.assertEqual(len(bytes.fromhex(firma.firma_de(_datos()))), 16)


# ══════════════════════════════════════════════════════════════════════
#  Escribirla: `firmar` contra los binarios falsos
# ══════════════════════════════════════════════════════════════════════

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


class FirmaConArnesTestCase(unittest.IsolatedAsyncioTestCase):
    """tmpdir + binarios falsos + OUTPUT_DIR de Fase E redirigido."""

    def setUp(self):
        # El prefijo NO puede llevar «firma» ni «marca»: la ruta del tmpdir sale
        # en el log y dispararía un falso positivo en el test que comprueba
        # que la marca no se anuncia.
        self.tmp = Path(tempfile.mkdtemp(prefix="hdo_test_"))
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

        self.mpls = self.disco / "BDMV" / "PLAYLIST" / "00800.mpls"
        self.mpls.write_bytes(b"\x00" * 4096)
        self.log = CollectingLog()

        self.tb.define_mkv(self.mpls.name, tracks=[
            {"type": "video", "codec": "HEVC/H.265/MPEG-H",
             "dimensions": "3840x2160", "fps": 23.976},
            {"type": "audio", "codec": "TrueHD Atmos", "language": "spa", "channels": 8},
            {"type": "subtitles", "codec": "HDMV PGS", "language": "spa"},
        ])

    def _sesion(self, **kw):
        base = {
            "id": "Peli_2024_1700000000",
            "iso_path": str(self.tmp / "Peli.iso"),
            "mkv_name": "Peli (2024) [DV FEL].mkv",
            "included_tracks": [
                _audio("Spanish", "Dolby TrueHD/Atmos Audio",
                       "Castellano TrueHD Atmos 7.1", flag_default=True, ch=8),
                _sub("Spanish", "forced", "Castellano Forzados (PGS)",
                     flag_forced=True, position=1),
            ],
            "chapters": [Chapter(number=1, timestamp="00:00:00.000", name="Capítulo 01")],
        }
        base.update(kw)
        return Session(**base)

    async def _es_nuestro(self, mkv_path) -> bool:
        """El camino REAL de lectura: `mkvmerge -J` + el veredicto."""
        datos = await firma._identificar(str(mkv_path))
        self.assertIsNotNone(datos, "mkvmerge -J no devolvió nada")
        return firma.lleva_nuestra_firma(datos)


class TestFirmarUnFichero(FirmaConArnesTestCase):

    async def _mkv(self, nombre="suelto.mkv"):
        destino = self.tmp / nombre
        await self.d.run_phase_d(str(self.disco), tmp_dir=str(self.tmp),
                                 log_callback=self.log)
        # El intermedio que Fase D deja, renombrado a lo que el test quiera.
        intermedio = next(self.tmp.glob("*_intermediate.mkv"))
        intermedio.rename(destino)
        return destino

    async def test_un_mkv_recien_muxeado_no_lleva_marca(self):
        mkv = await self._mkv()
        self.assertFalse(await self._es_nuestro(mkv))

    async def test_y_ese_mkv_SI_tiene_un_uid(self):
        """La premisa del test de arriba, y hay que fijarla aparte.

        mkvmerge emite un `SegmentUID` SIEMPRE, aleatorio. Si el binario
        falso devolviera `""` el test anterior pasaría igual, pero por el
        motivo equivocado —por no haber uid en vez de por ser otro— y
        dejaría de cubrir el falso positivo, que es lo que importa.
        """
        mkv = await self._mkv()
        datos = await firma._identificar(str(mkv))
        uid = datos["container"]["properties"].get("segment_uid")
        self.assertTrue(uid, "el mkvmerge falso no emite SegmentUID")
        self.assertNotEqual(uid, firma.firma_de(datos))

    async def test_firmar_lo_marca(self):
        mkv = await self._mkv()
        self.assertTrue(await firma.firmar(str(mkv)))
        self.assertTrue(await self._es_nuestro(mkv))

    async def test_firmar_es_idempotente(self):
        """Fase H firma también al revalidar un MKV ya existente."""
        mkv = await self._mkv()
        await firma.firmar(str(mkv))
        primera = (await firma._identificar(str(mkv)))["container"]["properties"]["segment_uid"]
        await firma.firmar(str(mkv))
        segunda = (await firma._identificar(str(mkv)))["container"]["properties"]["segment_uid"]
        self.assertEqual(primera, segunda)

    async def test_sobrevive_a_una_edicion_de_tab2(self):
        """El caso más frecuente de todos: el dueño le cambia el título y el
        nombre de una pista desde Tab 2, que por debajo es `mkvpropedit`.

        Con el título dentro de los rasgos esto se rompía, y es la razón de
        que los rasgos sean los tres que son.
        """
        from models import MkvEditRequest, MkvEditTrack
        from phases import mkv_analyze
        mkv = await self._mkv()
        await firma.firmar(str(mkv))
        await mkv_analyze.apply_mkv_edits(MkvEditRequest(
            file_path=str(mkv),
            title="Otro título completamente distinto",
            audio_tracks=[MkvEditTrack(id=1, name="Castellano DD 5.1",
                                       flag_default=False)],
        ))
        self.assertTrue(await self._es_nuestro(mkv))

    async def test_un_fichero_que_no_existe_no_lanza(self):
        self.assertFalse(await firma.firmar(str(self.tmp / "no_existe.mkv")))

    async def test_si_mkvmerge_falla_no_lanza(self):
        mkv = await self._mkv()
        self.tb.fail_when_arg("mkvmerge", "-J")
        self.assertFalse(await firma.firmar(str(mkv)))

    async def test_si_mkvpropedit_falla_no_lanza(self):
        mkv = await self._mkv()
        self.tb.fail_when_arg("mkvpropedit", "segment-uid", rc=2)
        self.assertFalse(await firma.firmar(str(mkv)))

    async def test_el_codigo_1_de_mkvpropedit_son_avisos(self):
        """Y los cambios SÍ se escriben, así que no es un fallo. Mismo
        criterio que la ruta propedit de Fase E, que también usa `>= 2`."""
        mkv = await self._mkv()
        self.tb.fail_when_arg("mkvpropedit", "segment-uid", rc=1)
        self.assertTrue(await firma.firmar(str(mkv)))


# ══════════════════════════════════════════════════════════════════════
#  Los puntos de salida: las fases, ejecutadas
# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
#  El punto de salida de Tab 1: el orquestador, ejecutado
# ══════════════════════════════════════════════════════════════════════

VIDEO = {"type": "video", "codec": "HEVC/H.265/MPEG-H",
         "dimensions": "3840x2160", "fps": 23.976}
AUDIO_ES = {"type": "audio", "codec": "TrueHD Atmos", "language": "spa", "channels": 8}
AUDIO_EN = {"type": "audio", "codec": "DTS-HD Master Audio", "language": "eng", "channels": 6}


class OrquestadorFirmaCase(unittest.IsolatedAsyncioTestCase):
    """`_run_pipeline` con un BDMV de mentira, que `Source` no monta."""

    def setUp(self):
        import paths
        import storage
        from phases import phase_d, phase_e
        from routers import tab1

        self.main, self.storage = tab1, storage
        # Los locks del save throttled viven en un dict de MÓDULO y cada test
        # trae su propio event loop: uno que se cierre con el `_bg_save`
        # pendiente deja el lock tomado y atado a un loop muerto. Solo se ve
        # en CI, donde la minor es 3.10 y el lock se ata al construirse.
        tab1._session_save_locks.clear()
        tab1._session_save_throttle.clear()

        self.tmp = Path(tempfile.mkdtemp(prefix="hdo_orq_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = self.tmp / "config"
        self.trabajo = self.tmp / "tmp"
        self.salida = self.tmp / "output"
        for d in (self.config, self.trabajo, self.salida):
            d.mkdir(parents=True)

        self.bdmv = self.tmp / "Peli (2024)"
        (self.bdmv / "BDMV" / "PLAYLIST").mkdir(parents=True)
        (self.bdmv / "BDMV" / "STREAM").mkdir(parents=True)
        self.mpls = self.bdmv / "BDMV" / "PLAYLIST" / "00800.mpls"
        self.mpls.write_bytes(b"\x00" * 4096)
        self.m2ts = self.bdmv / "BDMV" / "STREAM" / "00044.m2ts"
        self.m2ts.write_bytes(b"\x00" * (8 * 1024 * 1024))

        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)
        for nombre in (self.mpls.name, self.m2ts.name):
            self.tb.define_mkv(nombre, tracks=[VIDEO, AUDIO_ES, AUDIO_EN],
                               duration_s=7200.0)

        parches = [
            (storage, "CONFIG_DIR", self.config),
            (paths, "CONFIG_DIR", self.config),
            (paths, "TMP_DIR", str(self.trabajo)),
            (phase_e, "OUTPUT_DIR", str(self.salida)),
            (phase_d, "MIN_MPLS_SIZE", 200),
        ]
        originales = [(m, a, getattr(m, a)) for m, a, _ in parches]
        for m, a, v in parches:
            setattr(m, a, v)
        self.addCleanup(lambda: [setattr(m, a, v) for m, a, v in originales])
        for nombre in ("_sessions_summary_by_file",):
            cache = getattr(storage, nombre, None)
            if isinstance(cache, dict):
                cache.clear()

    def _sesion(self, *, incluidos=None, **kw):
        if incluidos is None:
            # Las dos del disco en su orden natural: sin reordenación, o sea
            # la ruta con intermedio.
            incluidos = [
                _audio("Spanish", "Dolby TrueHD/Atmos Audio",
                       "Castellano TrueHD Atmos 7.1", position=0, ch=8),
                _audio("English", "DTS-HD Master Audio",
                       "Inglés DTS-HD MA 5.1", position=1, ch=6),
            ]
        base = dict(
            id="Peli_2024_1700000000",
            iso_path=str(self.bdmv),
            source_type="bdmv_folder",
            source_path=str(self.bdmv),
            mkv_name="Peli (2024) [DV FEL].mkv",
            status="queued",
            included_tracks=incluidos,
            chapters=[Chapter(number=1, timestamp="00:00:00.000", name="Capítulo 01")],
        )
        base.update(kw)
        s = Session(**base)
        self.storage.save_session(s)
        return s

    async def _correr(self, s):
        await self.main._run_pipeline(s.id)
        return self.storage.load_session(s.id)

    async def _es_nuestro(self, mkv_path) -> bool:
        datos = await firma._identificar(str(mkv_path))
        self.assertIsNotNone(datos, "mkvmerge -J no devolvió nada")
        return firma.lleva_nuestra_firma(datos)


class TestElRipFirmaSuSalida(OrquestadorFirmaCase):
    """Tab 1 entrega su MKV por `_run_pipeline`, y hay DOS rutas de salida.

    La directa (con reordenación o pistas excluidas) escribe el MKV final con
    un solo mkvmerge; la de intermedio lo mueve tras editarlo con mkvpropedit.
    La marca va en el orquestador justo porque las dos pasan por ahí: puesta
    dentro de Fase E habría que ponerla dos veces, y la tercera ruta que
    alguien añada mañana saldría sin ella.
    """

    async def test_la_ruta_con_intermedio_firma(self):
        s = await self._correr(self._sesion())
        self.assertEqual(s.status, "done")
        self.assertTrue(await self._es_nuestro(s.output_mkv_path))

    async def test_la_ruta_directa_firma(self):
        """Una sola pista incluida de las dos del disco → ruta directa."""
        s = await self._correr(self._sesion(incluidos=[
            _audio("Spanish", "Dolby TrueHD/Atmos Audio",
                   "Castellano TrueHD Atmos 7.1", position=0, ch=8),
        ]))
        self.assertEqual(s.status, "done")
        self.assertTrue(await self._es_nuestro(s.output_mkv_path))

    async def test_un_fallo_al_firmar_no_tumba_el_rip(self):
        """Cuarenta minutos de trabajo no se tiran por una marca invisible."""
        self.tb.fail_when_arg("mkvpropedit", "segment-uid", rc=2)
        s = await self._correr(self._sesion())
        self.assertEqual(s.status, "done")
        self.assertTrue(Path(s.output_mkv_path).exists())
        self.assertFalse(await self._es_nuestro(s.output_mkv_path))

    async def test_se_firma_ANTES_de_validar(self):
        """Así la verificación final mira el fichero exacto que se le queda
        al usuario, y no una versión anterior de él.

        El orden se comprueba sobre las invocaciones registradas y no sobre
        el veredicto: montar un escenario que valide sin discrepancias
        probaría otra cosa —que el arnés declara bien el MKV de salida— y
        dejaría de mirar esto en cuanto alguien lo retocara.
        """
        s = await self._correr(self._sesion())
        final = Path(s.output_mkv_path).name
        llamadas = self.tb.calls
        firmas = [i for i, c in enumerate(llamadas)
                  if c.binary == "mkvpropedit"
                  and any("segment-uid" in a for a in c.argv)]
        validaciones = [i for i, c in enumerate(llamadas)
                        if c.binary == "mkvmerge" and "-J" in c.argv
                        and any(final in a for a in c.argv)]
        self.assertTrue(firmas, "nadie firmó el MKV final")
        self.assertTrue(validaciones, "nadie validó el MKV final")
        self.assertLess(firmas[0], validaciones[-1],
                        "se validó antes de firmar: la verificación miró un "
                        "fichero que ya no es el que se entrega")

    async def test_la_marca_no_se_anuncia_en_el_log(self):
        """Una marca discreta que se anuncia deja de serlo, y el usuario no
        tiene ninguna decisión que tomar sobre ella."""
        s = await self._correr(self._sesion())
        texto = "\n".join(s.execution_history[-1].output_log).lower()
        for palabra in ("firma", "firmad", "segment-uid", "segment_uid",
                        "procedencia", "huella"):
            self.assertNotIn(palabra, texto, f"el log menciona «{palabra}»")


# ══════════════════════════════════════════════════════════════════════
#  El tercer punto de salida: la Fase H de CMv4.0
# ══════════════════════════════════════════════════════════════════════

FRAMES = 1000
SRC_FEL = RpuProps(profile=7, el_type="FEL", cm_version="v2.9", frames=FRAMES)
TGT_P7_FEL_V40 = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                          frames=FRAMES, has_l8=True)
INJ_FEL_V40 = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                       frames=FRAMES, has_l8=True)


class TestFaseHFirmaSuSalida(PhaseTestCase):
    """Tab 3 entrega su MKV por la Fase H, y en DOS ramas.

    La normal renombra el `.mkv.tmp` y la de `already_renamed` revalida un
    fichero que ya estaba en su sitio —que es además por donde un proyecto
    anterior a la marca la gana—. Cubrir solo una dejaría upgrades sin marca.

    Y hay un orden que importa: el `SegmentUID` viaja DENTRO del fichero, así
    que firmar antes del rename y no después dejaría la marca en un fichero
    que ya no existe. Es la misma trampa que el arnés documenta con los
    sidecars indexados por ruta.
    """

    def _sesion(self):
        self.tb.define_rpu("RPU_source.bin", **SRC_FEL.as_dict())
        self.tb.define_rpu("RPU_target.bin", **TGT_P7_FEL_V40.as_dict())
        write_artifacts(self.wd, "RPU_source.bin", props=SRC_FEL)
        write_artifacts(self.wd, "RPU_target.bin", props=TGT_P7_FEL_V40)
        write_artifacts(self.wd, "source_injected.hevc", props=INJ_FEL_V40)
        write_artifacts(self.wd, "source.mkv")
        session = make_session(
            self.wd, source_workflow="p7_fel",
            target_type="trusted_p7_fel_final", target_trust_ok=True,
            trust_override="auto",
            source_frame_count=FRAMES, target_frame_count=FRAMES,
        )
        self.tb.define_media(Path(session.source_mkv_path).name,
                             duration=7200.0, frames=FRAMES)
        return session

    def _stage_tmp(self, session):
        tmp = self.output_dir / f"{session.output_mkv_name}.tmp"
        write_artifacts(self.output_dir, tmp.name, props=INJ_FEL_V40)
        self.tb.define_media(tmp.name, duration=7200.0, frames=FRAMES)
        return tmp

    async def _es_nuestro(self, mkv_path) -> bool:
        datos = await firma._identificar(str(mkv_path))
        self.assertIsNotNone(datos, "mkvmerge -J no devolvió nada")
        return firma.lleva_nuestra_firma(datos)

    async def test_la_rama_normal_firma_el_mkv_final(self):
        from phases.cmv40_pipeline import run_phase_h_validate
        session = self._sesion()
        self._stage_tmp(session)
        await run_phase_h_validate(session, self.log)
        final = self.output_dir / session.output_mkv_name
        self.assertTrue(final.exists(), "Fase H no dejó el MKV final")
        self.assertTrue(await self._es_nuestro(final))

    async def test_la_marca_va_sobre_el_nombre_DEFINITIVO(self):
        """No sobre el `.mkv.tmp`: si se firmara antes del rename, la marca
        se quedaría atada a un nombre que deja de existir."""
        from phases.cmv40_pipeline import run_phase_h_validate
        session = self._sesion()
        tmp = self._stage_tmp(session)
        await run_phase_h_validate(session, self.log)
        self.assertFalse(tmp.exists(), "el .tmp debería haberse renombrado")
        final = self.output_dir / session.output_mkv_name
        self.assertTrue(await self._es_nuestro(final))

    async def test_revalidar_un_mkv_ya_existente_tambien_lo_firma(self):
        """La rama `already_renamed`: es como un proyecto anterior a la marca
        la consigue, sin migrar nada ni rehacer el upgrade."""
        from phases.cmv40_pipeline import run_phase_h_validate
        session = self._sesion()
        final = self.output_dir / session.output_mkv_name
        write_artifacts(self.output_dir, final.name, props=INJ_FEL_V40)
        self.tb.define_media(final.name, duration=7200.0, frames=FRAMES)
        self.assertFalse(await self._es_nuestro(final))
        await run_phase_h_validate(session, self.log)
        self.assertTrue(await self._es_nuestro(final))

    async def test_un_fallo_al_firmar_no_tumba_la_fase(self):
        from phases.cmv40_pipeline import run_phase_h_validate
        session = self._sesion()
        self._stage_tmp(session)
        self.tb.fail_when_arg("mkvpropedit", "segment-uid", rc=2)
        result = await run_phase_h_validate(session, self.log)
        self.assertEqual(result["cm_version"], "v4.0")
        final = self.output_dir / session.output_mkv_name
        self.assertTrue(final.exists())
        self.assertFalse(await self._es_nuestro(final))


# ══════════════════════════════════════════════════════════════════════
#  Leerla: Tab 2, que es por donde el usuario la ve
# ══════════════════════════════════════════════════════════════════════

class TestTab2LoReconoce(unittest.IsolatedAsyncioTestCase):
    """`analyze_mkv` es el único lector de la marca en producción.

    Escribirla sin leerla dejaría la función invisible, y es justo el sitio
    donde el usuario va a preguntar: abre el MKV en Tab 2 para verle la
    radiografía DV+HDR y ahí mismo tiene la respuesta.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hdo_tab2_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)

        from phases import mkv_analyze
        self.mod = mkv_analyze
        self._orig_out = mkv_analyze.OUTPUT_DIR
        mkv_analyze.OUTPUT_DIR = str(self.tmp)
        self.addCleanup(lambda: setattr(mkv_analyze, "OUTPUT_DIR", self._orig_out))

        self.mkv = self.tmp / "Peli (2024).mkv"
        write_artifacts(self.tmp, self.mkv.name)
        self.tb.define_mkv(self.mkv.name, title="Peli (2024)", duration_s=7200.0)
        self.tb.define_pgs_packets(self.mkv.name, {})
        self.tb.define_mediainfo(self.mkv.name)
        self.tb.define_media(self.mkv.name, duration=7200.0, frames=172800)

    async def analizar(self):
        return await self.mod.analyze_mkv(str(self.mkv), use_cache=False)

    async def test_un_mkv_ajeno_no_consta(self):
        self.assertFalse((await self.analizar()).hecho_con_esta_app)

    async def test_un_mkv_firmado_se_reconoce(self):
        self.assertTrue(await firma.firmar(str(self.mkv)))
        self.assertTrue((await self.analizar()).hecho_con_esta_app)

    async def test_la_version_del_cache_subio(self):
        """Sin el bump, los MKV ya analizados dirían «no consta» para
        siempre: el bloque `basic` cacheado no trae el campo nuevo y
        Pydantic lo rellenaría con su default."""
        self.assertGreaterEqual(self.mod.CACHE_VERSION_BASIC, 3)


if __name__ == "__main__":
    unittest.main()
