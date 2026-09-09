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
        import paths
        import storage
        from phases import phase_d, phase_e
        from routers import tab1

        # `_run_pipeline` salió de `main.py` con el resto de Tab 1.
        self.main, self.storage = tab1, storage
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
            # `historial` resuelve su fichero desde aquí; sin el parche los
            # tests escribirían en el /config real (o fallarían al no poder).
            (paths, "CONFIG_DIR", self.config),
            (paths, "TMP_DIR", str(self.trabajo)),
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


class TestLaValidacionQueNoCuadra(OrquestadorCase):
    """Una verificación final con discrepancias tiene que dejar rastro.

    La sesión termina en `done` **a propósito**: el MKV existe y se reproduce,
    así que marcarlo `error` sería mentir en la otra dirección. Pero antes la
    discrepancia vivía **solo en el log**, y una sesión con una pista cruzada
    quedaba indistinguible de una correcta — que es justo la familia de bugs
    que ha dado el matcher de Fase E. Ahora se persiste en `error_message`, que
    en Tab 1 no pinta banner (solo se muestra con running/queued) y que el
    historial copia.
    """

    AUDIO_OTRO_IDIOMA = {"type": "audio", "codec": "TrueHD Atmos",
                         "language": "fra", "channels": 8,
                         "track_name": "Castellano TrueHD Atmos 7.1",
                         "default": True}

    async def test_una_validacion_limpia_no_deja_aviso(self):
        s = self._sesion()
        # Hay que declarar el MKV de SALIDA: sin esto el falso no sabe qué
        # contiene y la verificación encuentra discrepancias. (Todos los demás
        # tests de este fichero corrían así, con la validación fallando sin que
        # nada lo dijera — que es exactamente el hueco que se está tapando.)
        self.tb.define_mkv(s.mkv_name, duration_s=7200.0,
                           tracks=[VIDEO, AUDIO_ES, AUDIO_EN])
        salida = await self._correr(s)
        self.assertEqual(salida.status, "done")
        self.assertIsNone(salida.error_message, "avisa de algo que sí cuadra")

    async def test_con_discrepancias_queda_done_pero_con_constancia(self):
        s = self._sesion()
        # El MKV final sale con el castellano etiquetado como francés.
        self.tb.define_mkv(s.mkv_name, duration_s=7200.0,
                           tracks=[VIDEO, self.AUDIO_OTRO_IDIOMA, AUDIO_EN])
        salida = await self._correr(s)
        self.assertEqual(salida.status, "done", "el MKV existe: no es un error")
        self.assertIsNotNone(salida.error_message,
                             "la discrepancia se quedó solo en el log")
        self.assertIn("discrepancias", salida.error_message.lower())

    async def test_la_constancia_llega_al_historial(self):
        s = self._sesion()
        self.tb.define_mkv(s.mkv_name, duration_s=7200.0,
                           tracks=[VIDEO, self.AUDIO_OTRO_IDIOMA, AUDIO_EN])
        salida = await self._correr(s)
        self.assertTrue(salida.execution_history)
        registro = salida.execution_history[-1]
        self.assertEqual(registro.status, "done")
        self.assertIn("discrepancias", (registro.error_message or "").lower())


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

    async def test_el_log_va_numerado_como_las_fases_que_se_ven(self):
        """Las cuatro fases de una conversión llevan letra en la UI y el log
        usa la MISMA. Decía `[Fase D]` para lo que la columna llama la
        segunda: era la numeración interna del proyecto (A análisis, B reglas,
        D extracción, E escritura) asomando por una vista que describe UNA
        ejecución."""
        s = await self._correr(self._sesion())
        log = "\n".join(s.output_log)
        self.assertIn("[Fase A]", log, "la apertura del origen")
        self.assertIn("[Fase B]", log, "la extracción con mkvmerge")
        self.assertIn("[Fase D]", log, "el cierre del origen")
        self.assertIn("Origen cerrado", log)
        # Y no queda rastro de la numeración vieja.
        self.assertNotIn("[Fase E]", log)
        self.assertNotIn("[Origen]", log)


if __name__ == "__main__":
    unittest.main()


class TestUnaConversionCanceladaDejaRastro(OrquestadorCase):
    """Cancelar un ISO → MKV y no encontrarlo en «Recientes».

    El `finally` del pipeline decía «no registrar cancelaciones» y se saltaba
    la anotación entera. Ese guard es anterior al historial transversal, cuyo
    punto es el contrario: cuentan las tres salidas, y la cancelada y la que
    falla son justo las que uno mira después.
    """

    async def _cancelar_a_media_extraccion(self):
        """Levanta la bandera de cancelación desde dentro de la fase larga.

        `_run_pipeline` la pone a False al arrancar, así que no vale
        prepararla antes: hay que subirla mientras corre, que es lo que hace
        el endpoint cuando el usuario pulsa el botón."""
        from routers import tab1 as r1
        from phases import phase_d
        real = phase_d.run_phase_d
        sid = "Peli_2024_1700000000"

        async def cancelando(*a, **kw):
            r1._cancel_flags[sid] = True
            return await real(*a, **kw)

        phase_d.run_phase_d = cancelando
        r1.run_phase_d = cancelando
        self.addCleanup(setattr, phase_d, "run_phase_d", real)
        self.addCleanup(setattr, r1, "run_phase_d", real)
        return await self._correr(self._sesion())

    async def test_la_sesion_vuelve_a_pending_sin_error(self):
        s = await self._cancelar_a_media_extraccion()
        self.assertEqual(s.status, "pending")
        self.assertIsNone(s.error_message)

    async def test_y_el_historial_transversal_la_recoge(self):
        import historial
        s = await self._cancelar_a_media_extraccion()
        lineas = [t for t in historial.leer(50) if t["id"] == s.id]
        self.assertTrue(lineas, "la cancelación no dejó línea en el historial")
        self.assertEqual(lineas[0]["estado"], "cancelled")
        self.assertIsNone(lineas[0]["error"], "cancelar no es un error")

    async def test_pero_no_cuenta_como_ejecucion_del_proyecto(self):
        s = await self._cancelar_a_media_extraccion()
        self.assertEqual(s.execution_history, [])


class TestElLogVaSincronizadoConLaFase(OrquestadorCase):
    """Cada línea del log lleva la letra de la fase que estaba corriendo.

    No es cosmético: la columna dice «Fase B · Extracción de pistas» y el log
    decía «[Fase C]» en la ruta directa, porque ese mkvmerge lo lanza
    `run_phase_e_direct` —que vive en `phase_e.py` y arrastraba la letra de la
    numeración interna del proyecto—. Dos numeraciones para lo mismo delante
    del usuario.

    La comprobación es exhaustiva a propósito: no busca marcadores concretos,
    sino que recorre TODAS las líneas y compara la letra de cada una con la
    fase activa en ese momento. Una fase nueva o un marcador movido de sitio
    lo destapa sin tener que acordarse de este test.
    """

    _LETRA = {"mount": "A", "extract": "B", "write": "C", "unmount": "D"}

    @staticmethod
    def _colapsar(seq):
        """Quita repeticiones consecutivas: interesa el ORDEN, no cuántas."""
        salida = []
        for x in seq:
            if not salida or salida[-1] != x:
                salida.append(x)
        return salida

    async def _secuencias(self, sesion):
        """(fases que corrieron, letras que escribió el log), las dos en orden.

        No se correlaciona línea a línea porque `_run_pipeline` carga su
        propia sesión y el log crece dentro; comparar las dos secuencias
        colapsadas dice lo mismo y no depende de eso.
        """
        import re
        from routers import tab1 as r1
        fases: list[str] = []
        real = r1._rip_progress_fase

        def espia(fase):
            fases.append(self._LETRA.get(fase, "?"))
            return real(fase)

        r1._rip_progress_fase = espia
        self.addCleanup(setattr, r1, "_rip_progress_fase", real)
        s = await self._correr(sesion)
        r1._rip_progress_fase = real
        letras = [m.group(1) for l in s.output_log
                  if (m := re.search(r"\[Fase ([A-Z])\]", l))]
        return self._colapsar(fases), self._colapsar(letras), s

    async def test_ruta_con_intermedio(self):
        fases, letras, _ = await self._secuencias(self._sesion())
        self.assertEqual(letras, fases)

    async def test_ruta_directa(self):
        """La que estaba mal: un solo mkvmerge hace la extracción y los
        metadatos, y su log salía con la letra de la segunda."""
        fases, letras, _ = await self._secuencias(self._sesion(incluidos=[
            _audio("Spanish", "Dolby TrueHD/Atmos Audio",
                   "Castellano TrueHD Atmos 7.1", ch=8)]))
        self.assertEqual(letras, fases)

    async def test_la_ruta_directa_no_deja_la_fase_C_colgando(self):
        """Los metadatos los escribe ese mismo mkvmerge, así que la fase se
        ejecuta: sin marcarla, la columna la dejaba pendiente para siempre con
        el trabajo ya terminado."""
        _, _, s = await self._secuencias(self._sesion(incluidos=[
            _audio("Spanish", "Dolby TrueHD/Atmos Audio",
                   "Castellano TrueHD Atmos 7.1", ch=8)]))
        log = "\n".join(s.output_log)
        self.assertIn("[Fase C]", log)
        self.assertIn("misma pasada", log)

    async def test_las_cuatro_letras_salen_y_en_orden(self):
        fases, letras, _ = await self._secuencias(self._sesion())
        self.assertEqual(fases, ["A", "B", "C", "D"])
        self.assertEqual(letras, ["A", "B", "C", "D"])


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
