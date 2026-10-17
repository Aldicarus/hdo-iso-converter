"""La caché de Tab 2 guarda lo que se midió, y solo eso.

La auditoría con Codex del 2026-10-17 (paquete P4) encontró varias formas de
que la caché de `/config/mkv_audits/` acabara sirviendo algo que el fichero ya
no es, o una ausencia que nadie comprobó — y ninguna daba error:

  · un análisis que coincidía con un «Aplicar cambios» guardaba los metadatos
    de ANTES bajo la huella de DESPUÉS, y se servían indefinidamente;
  · «Forzar reanálisis» con la invalidación fallando servía la caché con 200;
  · un bloque con la versión vigente que el modelo ya no acepta se reanalizaba
    en cada apertura sin repararse;
  · un `mkvextract` agotado se cacheaba como «sin capítulos», y un sniff DV
    caído como «sin Dolby Vision»;
  · tras editar, la entrada vieja se quedaba: dos tarjetas para el mismo MKV.

Y del mismo análisis: el recuento de frames salía de la duración del
CONTENEDOR por un fps redondeado, dos HEVC se daban por FEL aunque dovi_tool
dijera MEL, el pre-flight de espacio exigía el HEVC que el pipe no escribe, y
dos consultas con tope abandonaban el subproceso vivo al agotarse.

`POST /api/mkv/apply` no validaba la ruta contra los roots, y editaba in situ
un MKV de `/mnt/isos`, que es de solo lectura.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_la_cache_de_tab2_no_miente -v
"""
import asyncio
import collections
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
from cmv40_harness import FakeToolbox, RpuProps, write_artifacts  # noqa: E402


class Tab2Case(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.tb = FakeToolbox(self.tmp / "tb").install()
        self.addCleanup(self.tb.uninstall)
        self.mkv = self.output_dir / "Peli (2024).mkv"
        write_artifacts(self.output_dir, self.mkv.name)
        self.tb.define_mkv(self.mkv.name)
        self.tb.define_pgs_packets(self.mkv.name, {4: 120, 5: 4500})
        self.tb.define_mediainfo(self.mkv.name)
        from phases import mkv_analyze
        self.m = mkv_analyze

    def abrir(self, **extra):
        r = self.client.post("/api/mkv/analyze",
                             json={"file_path": str(self.mkv), **extra})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def identificaciones(self) -> int:
        return len([c for c in self.tb.find("mkvmerge") if "-J" in c.argv])

    def entradas(self) -> list[Path]:
        d = self.config_dir / "mkv_audits"
        return sorted(d.glob("*.json")) if d.exists() else []


class TestUnAnalisisQueCoincideConUnaEdicion(Tab2Case):
    """#7: lo medido describe el fichero de ANTES; guardarlo bajo la huella
    del editado lo servía en cada apertura."""

    def _analizar(self, editar: bool):
        async def _paso(step):
            if editar and step == "mediainfo":
                # Lo que hace un «Aplicar cambios» concurrente: mkvpropedit
                # reescribe la cabecera mientras corre MediaInfo.
                subprocess.run(["mkvpropedit", str(self.mkv), "--edit", "info",
                                "--set", "title=Editado"], check=True,
                               capture_output=True)

        async def _todo():
            r = await self.m.analyze_mkv(str(self.mkv), progress_callback=_paso)
            self.m.persist_mkv_basic_to_cache(str(self.mkv), r)
        asyncio.run(_todo())

    def test_sin_edicion_se_guarda(self):
        self._analizar(editar=False)
        self.assertEqual(len(self.entradas()), 1)

    def test_con_una_edicion_a_mitad_no_se_guarda(self):
        self._analizar(editar=True)
        self.assertEqual(self.entradas(), [],
                         "se guardó lo de antes bajo la huella del fichero editado")


class TestForzarReanalisis(Tab2Case):
    """#9"""

    def test_con_la_invalidacion_fallando_reanaliza_igual(self):
        self.abrir()
        antes = self.identificaciones()
        import storage
        with mock.patch.object(storage, "invalidate_mkv_cache_by_path",
                               side_effect=OSError(13, "Permission denied")):
            self.abrir(force_refresh=True)
        self.assertEqual(self.identificaciones(), antes + 1,
                         "«forzar reanálisis» sirvió la caché")


class TestUnaCacheQueElModeloNoAcepta(Tab2Case):
    """#10: se reanaliza UNA vez y se repara."""

    def test_se_repara_en_la_primera_apertura(self):
        import storage
        fp = storage.compute_mkv_fingerprint(str(self.mkv))
        storage.write_mkv_cache_basic(
            fp, self.m.CACHE_VERSION_BASIC, self.m.CACHE_VERSION_QUALITY,
            {"file_path": str(self.mkv), "file_name": self.mkv.name,
             "tracks": "esto no es una lista"},
            original_file_path=str(self.mkv))
        self.abrir()
        self.assertEqual(self.identificaciones(), 1)
        self.abrir()
        self.assertEqual(self.identificaciones(), 1,
                         "la caché rota no se reescribió: se reanaliza en cada apertura")


class TestLoQueNoSePudoLeerNoSeCachea(Tab2Case):
    """#11: «no se pudo medir» no es «no hay»."""

    def test_un_mkv_sin_capitulos_si_se_cachea(self):
        datos = self.abrir()
        self.assertEqual(datos["chapters"], [])
        self.assertEqual(len(self.entradas()), 1)

    def test_si_mkvextract_falla_no_se_cachea(self):
        self.tb.define_chapters(self.mkv.name, [("00:00:00.000", "Uno")])
        self.tb.fail_when_arg("mkvextract", "chapters")
        datos = self.abrir()
        self.assertEqual(datos["chapters"], [], "la respuesta sigue saliendo")
        self.assertEqual(self.entradas(), [],
                         "un mkvextract caído quedó cacheado como «sin capítulos»")

    def test_si_dovi_tool_falla_al_medir_no_se_cachea(self):
        self.tb.fail("dovi_tool", "info")
        datos = self.abrir()
        self.assertIsNone(datos["dovi"])
        self.assertEqual(self.entradas(), [],
                         "un sniff DV caído quedó cacheado como «sin Dolby Vision»")


class TestEditarNoDejaDosTarjetas(Tab2Case):
    """Sospecha de la auditoría, reproducida: tras «Aplicar cambios» la
    entrada del contenido viejo se quedaba y el reanálisis creaba otra."""

    def test_una_sola_entrada_tras_editar_y_reabrir(self):
        self.abrir()
        r = self.client.post("/api/mkv/apply", json={
            "file_path": str(self.mkv),
            "audio_tracks": [{"id": 2, "name": "Castellano editado"}],
            "subtitle_tracks": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.abrir()
        self.assertEqual(len(self.entradas()), 1, [p.name for p in self.entradas()])
        recientes = self.client.get("/api/mkv/recientes").json()["recientes"]
        self.assertEqual(len(recientes), 1, "dos tarjetas para el mismo MKV")


class TestApplyValidaLaRuta(Tab2Case):
    """La ruta del body llegaba tal cual a mkvpropedit."""

    def test_un_mkv_fuera_de_los_roots_no_se_edita(self):
        write_artifacts(self.tmp, "fuera.mkv")
        r = self.client.post("/api/mkv/apply", json={
            "file_path": str(self.tmp / "fuera.mkv"),
            "audio_tracks": [{"id": 2, "name": "x"}], "subtitle_tracks": []})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.tb.find("mkvpropedit"), [])

    def test_un_enlace_que_sale_de_los_roots_tampoco(self):
        write_artifacts(self.tmp, "fuera.mkv")
        enlace = self.output_dir / "enlace.mkv"
        enlace.symlink_to(self.tmp / "fuera.mkv")
        r = self.client.post("/api/mkv/apply", json={
            "file_path": str(enlace),
            "audio_tracks": [{"id": 2, "name": "x"}], "subtitle_tracks": []})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.tb.find("mkvpropedit"), [])


class TestLosDescargadosSonDeSoloLectura(Tab2Case):
    """N2: `/mnt/isos` va `ro`; editar ahí in situ no puede funcionar."""

    def setUp(self):
        super().setUp()
        self.desc = self.isos_dir / "Descargada (2024).mkv"
        write_artifacts(self.isos_dir, self.desc.name)

    def _aplicar(self, **extra):
        return self.client.post("/api/mkv/apply", json={
            "file_path": str(self.desc),
            "audio_tracks": [{"id": 2, "name": "x"}], "subtitle_tracks": [],
            **extra})

    def test_sin_copia_no_se_edita_in_situ(self):
        r = self._aplicar()
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.tb.find("mkvpropedit"), [])

    def test_con_copia_se_encola_la_copia(self):
        import queue_manager as qm
        r = self._aplicar(copy_to_output=True)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([t[0] for t in self.trabajos_encolados],
                         [qm.TIPO_COPIA_BIBLIOTECA])


class TestElRecuentoDeFrames(Tab2Case):
    """#12: el audio puede alargar el contenedor, y el fps iba redondeado."""

    def _frames(self, **video):
        self.tb.define_mkv(self.mkv.name, duration_s=7202.0, tracks=[
            {"type": "video", "codec": "HEVC/H.265/MPEG-H",
             "dimensions": "3840x2160", "fps": 24.0, **video},
            {"type": "audio", "codec": "TrueHD Atmos", "language": "spa", "channels": 8}])
        r = asyncio.run(self.m.analyze_mkv(str(self.mkv), use_cache=False))
        return [t.frame_count for t in r.tracks if t.type == "video"][0]

    def test_la_etiqueta_de_mkvmerge_es_el_recuento(self):
        self.assertEqual(self._frames(frames=172_801), 172_801)

    def test_sin_etiqueta_cuenta_la_duracion_de_la_pista(self):
        # 7.200 s de vídeo a 24 fps; el contenedor dura 7.202 por el audio.
        self.assertEqual(self._frames(track_duration_s=7200.0), 172_800)


class TestDosHevcNoSonUnaFel(Tab2Case):
    """#13: con la capa MEDIDA, manda dovi_tool."""

    def _has_fel(self, el_type):
        write_artifacts(self.output_dir, self.mkv.name, props=RpuProps(
            profile=7, el_type=el_type, cm_version="v2.9"))
        r = asyncio.run(self.m.analyze_mkv(str(self.mkv), use_cache=False))
        self.assertIsNotNone(r.dovi)
        self.assertEqual(r.dovi.el_type, el_type)
        return r.has_fel

    def test_un_mel_no_se_anuncia_como_fel(self):
        self.assertFalse(self._has_fel("MEL"))

    def test_un_fel_sigue_siendo_fel(self):
        self.assertTrue(self._has_fel("FEL"))


_Uso = collections.namedtuple("_Uso", "total used free")


class TestElPreflightDeEspacio(unittest.IsolatedAsyncioTestCase):
    """#8: solo la reserva escribe el HEVC; el pipe no."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="espacio_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp).install()
        self.addCleanup(self.tb.uninstall)
        from phases import mkv_analyze
        self.m = mkv_analyze
        trabajo = self.tmp / "work"
        trabajo.mkdir()
        self._base_original = orig = mkv_analyze._quality_workdir_base
        mkv_analyze._quality_workdir_base = lambda: str(trabajo)
        self.addCleanup(setattr, mkv_analyze, "_quality_workdir_base", orig)
        self.mkv = self.tmp / "Peli (2024).mkv"
        write_artifacts(self.tmp, self.mkv.name, props=RpuProps(
            profile=7, el_type="FEL", cm_version="v4.0", frames=50, has_l8=True))
        self.tb.define_mkv(self.mkv.name)
        self.tb.define_media(self.mkv.name, duration=7200.0, frames=50)
        # Sin un byte libre: cualquier exigencia de espacio falla.
        lleno = mock.patch("shutil.disk_usage", return_value=_Uso(1, 1, 0))
        lleno.start()
        self.addCleanup(lleno.stop)

    async def test_por_el_pipe_no_se_exige_el_hevc(self):
        r = await self.m.analyze_rpu_quality_for_mkv(str(self.mkv))
        self.assertEqual(r.get("quality_total_frames_rpu"), 50, r)

    async def test_por_la_reserva_si(self):
        from phases import cmv40_pipeline

        async def _sin_pipe(*a, **k):
            return False
        with mock.patch.object(cmv40_pipeline, "_ffmpeg_extract_rpu_piped", _sin_pipe):
            with self.assertRaises(RuntimeError):
                await self.m.analyze_rpu_quality_for_mkv(str(self.mkv))
        self.assertFalse(any(".hevc" in a for c in self.tb.find("ffmpeg") for a in c.argv),
                         "la reserva empezó a escribir el HEVC sin espacio")

    def test_un_tmp_dir_inservible_se_avisa(self):
        """Caer al temporal del sistema en silencio es razonable en un Mac y
        un montaje roto en el NAS: que quede en el log."""
        no_dir = self.tmp / "soy_un_fichero"
        no_dir.write_text("x")
        with mock.patch.object(self.m, "TMP_DIR", str(no_dir / "sub")), \
                self.assertLogs(self.m._logger, level="WARNING"):
            self.assertIsNone(self._base_original())


class TestUnTopeAgotadoMataElProceso(unittest.IsolatedAsyncioTestCase):
    """#21: `wait_for` cancela la espera, no el proceso."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tope_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tb = FakeToolbox(self.tmp).install()
        self.addCleanup(self.tb.uninstall)
        from phases import mkv_analyze
        self.m = mkv_analyze
        self.procs = []
        real = asyncio.create_subprocess_exec

        async def _espia(*a, **k):
            p = await real(*a, **k)
            self.procs.append(p)
            return p
        parche = mock.patch.object(asyncio, "create_subprocess_exec", _espia)
        parche.start()
        self.addCleanup(parche.stop)

    async def test_la_duracion(self):
        self.tb.retrasar_cuando_arg("ffprobe", "format=duration", 20)
        with mock.patch.object(self.m, "_TOPE_DURACION_S", 0.3):
            self.assertEqual(await self.m._probe_duration_seconds(str(self.tmp / "x.mkv")), 0.0)
        self.assertEqual(len(self.procs), 1)
        self.assertIsNotNone(self.procs[0].returncode, "el ffprobe sigue vivo")

    async def test_el_frame_de_l254(self):
        from models import DoviInfo
        rpu = self.tmp / "RPU.bin"
        write_artifacts(self.tmp, rpu.name)
        self.tb.retrasar_cuando_arg("dovi_tool", "-f", 20)
        with mock.patch.object(self.m, "_TOPE_INFO_FRAME_S", 0.3):
            await self.m._marcar_l254(DoviInfo(), str(rpu))
        self.assertTrue(self.procs)
        self.assertTrue(all(p.returncode is not None for p in self.procs),
                        "el dovi_tool info sigue vivo")


if __name__ == "__main__":
    unittest.main()
