"""`_run_pipeline`, el orquestador de Tab 1, ejecutado.

341 líneas y complejidad 49, sin un solo test que lo ejecutara. Decide la ruta
(directa vs intermedio+propedit), gestiona el ciclo de vida del origen, marca
los tiempos por fase, valida el resultado y —lo más delicado— **reintenta con
el M2TS principal cuando mkvmerge aborta sobre el playlist**.

Ese reintento es el arreglo del crash de Avatar Fuego y Ceniza (2025) y vive
dentro de un `while True`. Que reintente es la mitad del contrato; la otra
mitad es que reintente **UNA sola vez** y que, si el origen ya era un M2TS, no
vuelva a entrar. Un bucle que se equivoque ahí no falla: se queda dando vueltas
sobre un disco de 90 GB.

Los tests usan un origen `bdmv_folder`, que en `Source` es un no-op: así se
ejercita el orquestador entero sin montar nada.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_orquestador_tab1 -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import FakeToolbox  # noqa: E402
from models import (  # noqa: E402
    Chapter, IncludedAudioTrack, IncludedSubtitleTrack, RawAudioTrack,
    RawSubtitleTrack, Session,
)

ASSERTION = ("mkvmerge: ../../src/merge/generic_packetizer.cpp:123: "
             "Assertion 'file_names.size() == play_items.size()' failed.")

VIDEO = {"type": "video", "codec": "HEVC/H.265/MPEG-H",
         "dimensions": "3840x2160", "fps": 23.976}
AUDIO_ES = {"type": "audio", "codec": "TrueHD Atmos", "language": "spa",
            "channels": 8, "track_name": "Castellano TrueHD Atmos 7.1",
            "default": True}
AUDIO_EN = {"type": "audio", "codec": "DTS-HD Master Audio", "language": "eng",
            "channels": 6, "track_name": "Inglés DTS-HD MA 5.1"}


def _audio(language, codec, label, *, position=0, ch=0):
    return IncludedAudioTrack(
        position=position,
        raw=RawAudioTrack(codec=codec, language=language, bitrate_kbps=0,
                          description=f"{ch}.0" if ch else ""),
        language_literal=label.split()[0], codec_literal=label, label=label,
        flag_default=(position == 0), selection_reason="test",
    )


class OrquestadorCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import main
        import storage
        from phases import phase_d, phase_e

        self.main, self.storage = main, storage
        self.tmp = Path(tempfile.mkdtemp(prefix="orq_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = self.tmp / "config"
        self.trabajo = self.tmp / "tmp"
        self.salida = self.tmp / "output"
        for d in (self.config, self.trabajo, self.salida):
            d.mkdir(parents=True)

        # Un BDMV de verdad: `Source` no monta nada con este tipo.
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
            (main, "TMP_DIR", str(self.trabajo)),
            (phase_e, "OUTPUT_DIR", str(self.salida)),
            (phase_d, "MIN_MPLS_SIZE", 200),
        ]
        originales = [(m, a, getattr(m, a)) for m, a, _ in parches]
        for m, a, v in parches:
            setattr(m, a, v)
        self.addCleanup(lambda: [setattr(m, a, v) for m, a, v in originales])
        # El cache de summary sobrevive entre tests y vería otro tmpdir.
        for nombre in ("_sessions_summary_by_file",):
            cache = getattr(storage, nombre, None)
            if isinstance(cache, dict):
                cache.clear()

    def _sesion(self, *, incluidos=None, **kw):
        if incluidos is None:
            # Las dos pistas de audio del disco, en su orden natural: sin
            # reordenación → ruta con intermedio.
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

    @property
    def muxes(self):
        """Invocaciones de mkvmerge que son un mux (no un --identify)."""
        return [c for c in self.tb.find("mkvmerge")
                if c.opt("-o") and "--identify" not in c.argv]


class TestLasDosRutas(OrquestadorCase):

    async def test_sin_reordenacion_va_por_el_intermedio(self):
        s = await self._correr(self._sesion())
        self.assertEqual(s.status, "done", s.error_message)
        self.assertTrue(self.tb.ran("mkvpropedit"),
                        "la ruta con intermedio edita cabeceras in-place")
        self.assertTrue(self.main.Path(s.output_mkv_path).exists(), s.output_mkv_path)
        self.assertIn("Ruta con intermedio", "\n".join(s.output_log))

    async def test_con_pistas_excluidas_va_por_la_ruta_directa(self):
        """Solo el castellano: se excluyó el inglés → un solo mkvmerge."""
        s = self._sesion(incluidos=[
            _audio("Spanish", "Dolby TrueHD/Atmos Audio",
                   "Castellano TrueHD Atmos 7.1", ch=8)])
        s = await self._correr(s)
        self.assertEqual(s.status, "done", s.error_message)
        self.assertFalse(self.tb.ran("mkvpropedit"),
                         "la ruta directa no usa mkvpropedit")
        self.assertEqual(len(self.muxes), 1, self.tb.calls)
        self.assertIn("Ruta directa", "\n".join(s.output_log))

    async def test_deja_el_mkv_en_output_y_lo_apunta_en_la_sesion(self):
        s = await self._correr(self._sesion())
        self.assertEqual(Path(s.output_mkv_path).parent, self.salida)
        self.assertEqual(Path(s.output_mkv_path).name, "Peli (2024) [DV FEL].mkv")

    async def test_registra_la_ejecucion_en_el_historial(self):
        s = await self._correr(self._sesion())
        self.assertEqual(len(s.execution_history), 1)
        registro = s.execution_history[0]
        self.assertEqual(registro.status, "done")
        self.assertTrue(registro.output_log, "el registro guarda su log")


class TestElReintentoConElM2ts(OrquestadorCase):
    """El arreglo del crash de Avatar, y sus dos límites."""

    async def test_reintenta_con_el_m2ts_y_termina_bien(self):
        # mkvmerge aborta sobre el playlist y funciona sobre el m2ts, igual
        # que en el disco real.
        self.tb.fail_when_arg("mkvmerge", ".mpls", stderr=ASSERTION + "\n")
        s = await self._correr(self._sesion())
        self.assertEqual(s.status, "done", s.error_message)
        log = "\n".join(s.output_log)
        self.assertIn("M2TS", log)
        # El mux que produjo el resultado leyó el m2ts, no el mpls.
        exitosos = [c for c in self.muxes if any(".m2ts" in a for a in c.argv)]
        self.assertTrue(exitosos, self.tb.calls)

    async def test_reintenta_UNA_sola_vez(self):
        """Si mkvmerge aborta también con el m2ts, tiene que rendirse en vez de
        dar vueltas. El reintento vive en un `while True`."""
        self.tb.fail_when_arg("mkvmerge", "0", stderr=ASSERTION + "\n")
        s = await self._correr(self._sesion())
        self.assertEqual(s.status, "error")
        self.assertIn("ni desde el playlist ni desde el M2TS", s.error_message)
        # Dos intentos de mux: el playlist y el m2ts. Ni uno más.
        self.assertLessEqual(len(self.muxes), 2, self.tb.calls)

    async def test_un_origen_m2ts_no_reintenta(self):
        """Ya era lectura directa: no hay alternativa que probar."""
        s = self._sesion(source_type="m2ts", source_path=str(self.m2ts),
                         iso_path=str(self.m2ts))
        self.tb.fail_when_arg("mkvmerge", ".m2ts", stderr=ASSERTION + "\n")
        s = await self._correr(s)
        self.assertEqual(s.status, "error")
        self.assertEqual(len(self.muxes), 1,
                         "con origen m2ts no debe haber segundo intento")

    async def test_borra_el_intermedio_parcial_antes_de_reintentar(self):
        """Un intermedio a medias del intento fallido son decenas de GB."""
        self.tb.fail_when_arg("mkvmerge", ".mpls", stderr=ASSERTION + "\n")
        await self._correr(self._sesion())
        sobrantes = list(self.trabajo.glob("*_intermediate.mkv"))
        self.assertEqual(sobrantes, [], f"quedó un intermedio: {sobrantes}")


class TestFallos(OrquestadorCase):

    async def test_un_fallo_deja_la_sesion_en_error_con_mensaje(self):
        self.tb.fail("mkvmerge", "*", rc=2, stderr="algo fue mal\n")
        s = await self._correr(self._sesion())
        self.assertEqual(s.status, "error")
        self.assertTrue(s.error_message)
        self.assertEqual(s.execution_history[-1].status, "error")

    async def test_una_sesion_que_no_existe_no_revienta(self):
        await self.main._run_pipeline("no_existe_esta_sesion")

    async def test_el_log_lleva_los_marcadores_del_panel(self):
        """El parser del panel de cola busca `[Origen]` y el cierre."""
        s = await self._correr(self._sesion())
        log = "\n".join(s.output_log)
        self.assertIn("[Origen]", log)
        self.assertIn("Origen cerrado", log)


if __name__ == "__main__":
    unittest.main()


class TestParcialesQueSeQuedan(OrquestadorCase):
    """Un mkvmerge que aborta a mitad del mux deja el fichero a medias.

    Lo encontró una mutación: quitar el borrado del intermedio parcial en el
    reintento NO rompía ningún test, y al mirar por qué salió que ese borrado
    es **inalcanzable** — `intermediate_mkv` solo se asigna con lo que DEVUELVE
    `run_phase_d`, así que cuando la fase falla la variable sigue en None y no
    hay nada que borrar. Lo mismo en el manejador de error del pipeline.

    Consecuencia: un `*_intermediate.mkv` a medias (decenas de GB en un UHD) se
    quedaba en /mnt/tmp para siempre — el barrido de huérfanos tampoco cubre ese
    patrón. Y en la ruta directa, un `.mkv` parcial se quedaba en **/mnt/output**
    con el nombre definitivo, indistinguible de un rip terminado.

    El arreglo es que cada fase limpie SU salida, y solo si la creó ella: si el
    fichero ya existía antes, borrarlo destruiría el resultado de una ejecución
    anterior.
    """

    async def test_el_intermedio_parcial_no_se_queda_en_tmp(self):
        # mkvmerge escribe y DESPUÉS aborta, como en el disco real.
        self.tb.fail_when_arg("mkvmerge", ".mpls", stderr=ASSERTION + "\n",
                              tras_producir=True)
        s = await self._correr(self._sesion())
        self.assertEqual(s.status, "done", s.error_message)
        sobrantes = list(self.trabajo.glob("*_intermediate.mkv"))
        self.assertEqual(sobrantes, [], f"quedó un intermedio parcial: {sobrantes}")

    # Solo el mux lleva `--gui-mode`; el `--identify` no. Fallar por ese
    # argumento deja funcionar la identificación de pistas, que es lo que
    # decide la ruta: sin eso el track map salía vacío, el pipeline se iba por
    # la ruta con intermedio y el test miraba un /mnt/output que nunca se
    # tocaba — pasaba en verde sin probar nada (visto por mutación).
    SOLO_EL_MUX = "--gui-mode"

    def _sesion_ruta_directa(self):
        """Una sola pista incluida de las dos del disco → hay exclusión → ruta
        directa, sin intermedio."""
        return self._sesion(incluidos=[
            _audio("Spanish", "Dolby TrueHD/Atmos Audio",
                   "Castellano TrueHD Atmos 7.1", ch=8)])

    async def test_el_mkv_parcial_no_se_queda_en_output(self):
        """Ruta directa: el parcial llevaría el nombre definitivo, en el
        directorio de salida del usuario."""
        self.tb.fail_when_arg("mkvmerge", self.SOLO_EL_MUX, rc=2,
                              stderr="petó a mitad\n", tras_producir=True)
        s = await self._correr(self._sesion_ruta_directa())
        self.assertEqual(s.status, "error")
        self.assertEqual(list(self.salida.glob("*.mkv")), [],
                         "quedó un MKV parcial en output")

    async def test_no_borra_un_mkv_que_ya_estaba(self):
        """Si el fichero existía antes de la fase, no es nuestro parcial: puede
        ser el resultado bueno de una ejecución anterior. mkvmerge falla ANTES
        de escribir, así que el de antes sigue intacto."""
        previo = self.salida / "Peli (2024) [DV FEL].mkv"
        previo.write_bytes(b"rip anterior que no hay que perder")
        self.tb.fail_when_arg("mkvmerge", self.SOLO_EL_MUX, rc=2,
                              stderr="petó antes de abrir\n")
        s = await self._correr(self._sesion_ruta_directa())
        self.assertEqual(s.status, "error")
        self.assertTrue(previo.exists(), "se ha borrado un MKV que ya estaba")
        self.assertEqual(previo.read_bytes(), b"rip anterior que no hay que perder")
