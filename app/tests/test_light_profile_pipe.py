"""El perfil de luminancia extrae HEVC y RPU en UNA pasada, sin HEVC en disco.

`mkv_light_profile_endpoint` hacía `ffmpeg -f hevc fichero` y después
`dovi_tool extract-rpu fichero`. Dos problemas medidos en la Fase A, que hace
exactamente lo mismo:

  · escribe y vuelve a leer ~75 % del tamaño del MKV (~45 GB en un UHD) sobre
    el mismo pool por el que ffmpeg está leyendo el MKV — y aquí ese HEVC no
    se usa para nada más, solo interesa el RPU;
  · en serie se estorban: ffmpeg tarda 574 s limitado por DISCO con la CPU
    ociosa y `extract-rpu` 372 s limitado por CPU con el disco medio ocioso,
    ~946 s usando media máquina cada vez.

`_ffmpeg_extract_rpu_piped(..., hevc_out=None)` ya resolvía las dos cosas para
la Fase A y el pre-flight. Aquí se reutiliza.

El camino de reserva (dos pasadas) se conserva porque el helper devuelve False
sin lanzar ante cualquier problema, y también se ejercita: es el que corre si
el pipe no está disponible.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_light_profile_pipe -v
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

FRAMES = 120


class LightProfileTestCase(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import main
        self.main = main

        self.tmp = Path(tempfile.mkdtemp(prefix="lp_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.work = self.tmp / "work"
        self.output = self.tmp / "output"
        for d in (self.work, self.output):
            d.mkdir(parents=True, exist_ok=True)

        self.tb = FakeToolbox(self.tmp)
        self.tb.install()
        self.addCleanup(self.tb.uninstall)

        self.mkv = self.output / "Peli (2024).mkv"
        self.mkv.write_bytes(b"\x00" * (4 * 1024 * 1024))
        # Las props viajan MKV -> HEVC -> RPU, igual que en el NAS: se
        # declaran en el origen y el pipe las arrastra.
        self.tb.define_rpu(self.mkv.name, profile=7, el_type="FEL",
                           cm_version="v4.0", frames=FRAMES, has_l8=True)
        self.tb.define_rpu_levels("rpu.bin", l8_indices=[1])
        self.tb.define_media(self.mkv.name, duration=7200.0, frames=FRAMES)

        parches = [
            (main, "TMP_DIR", str(self.work)),
            (main, "OUTPUT_DIR_MKV", self.output),
            (main, "LIBRARY_ROOTS", {"output": self.output}),
        ]
        originales = [(m, a, getattr(m, a)) for m, a, _ in parches]
        for m, a, v in parches:
            setattr(m, a, v)
        self.addCleanup(lambda: [setattr(m, a, v) for m, a, v in originales])
        # El estado del job es un singleton de módulo y sobrevive entre tests:
        # sin limpiarlo, el segundo se encuentra `active=True` y responde 409.
        self._limpiar_singleton()
        self.addCleanup(self._limpiar_singleton)

    def _limpiar_singleton(self):
        self.main._light_profile_state.update({
            "active": False, "job_id": None, "request_id": None,
            "result": None, "error": None,
            "step": 0, "step_pct": 0, "global_pct": 0, "log_lines": [],
        })
        self.main._lp_active_proc["procs"] = []
        self.main._lp_cancel["requested_for_id"] = None

    async def analizar(self):
        return await self.main.mkv_light_profile_endpoint(
            {"file_path": str(self.mkv)})

    # ── helpers de aserción ──────────────────────────────────────────
    @property
    def ffmpeg_calls(self):
        return self.tb.find("ffmpeg")

    def hevc_en_disco(self) -> list[Path]:
        return list(self.work.rglob("*.hevc"))


class TestCaminoRapido(LightProfileTestCase):

    async def test_ffmpeg_escribe_al_pipe_y_dovi_tool_lee_de_stdin(self):
        await self.analizar()
        ff = self.tb.one("ffmpeg")
        self.assertIn("pipe:1", ff.argv,
                      f"ffmpeg no escribe al pipe: {ff.argv}")
        extract = self.tb.one("dovi_tool", "extract-rpu")
        self.assertIn("-", extract.argv,
                      f"extract-rpu no lee de stdin: {extract.argv}")

    async def test_a_ffmpeg_no_se_le_pide_escribir_ningun_hevc(self):
        """EL PUNTO. El HEVC son ~45 GB que aquí no se usan para nada.

        Se comprueba sobre el argv, no sobre el disco: el camino de reserva
        también borra el HEVC al terminar, así que "no queda ningún .hevc" es
        cierto en los dos y no distingue nada (comprobado por mutación).
        """
        await self.analizar()
        ff = self.tb.one("ffmpeg")
        escritos = [a for a in ff.argv if a.endswith(".hevc")]
        self.assertEqual(escritos, [],
                         f"ffmpeg escribe un HEVC que no hace falta: {ff.argv}")

    async def test_no_se_usa_el_muxer_tee(self):
        """`tee` es para cuando SÍ se quiere el HEVC además del pipe."""
        await self.analizar()
        ff = self.tb.one("ffmpeg")
        self.assertNotIn("tee", ff.argv, f"argv: {ff.argv}")

    async def test_no_corren_los_dos_caminos(self):
        """Si el pipe funciona, la reserva no debe ejecutarse encima. (Cada
        camino por separado usa un ffmpeg y un extract-rpu; lo que esto
        descarta es que se hagan LOS DOS.)"""
        await self.analizar()
        self.assertEqual(len(self.ffmpeg_calls), 1, self.tb.calls)
        self.assertEqual(len(self.tb.find("dovi_tool", "extract-rpu")), 1,
                         self.tb.calls)

    async def test_devuelve_el_perfil_de_luminancia(self):
        r = await self.analizar()
        self.assertTrue(r.get("per_scene_max_cll"), "serie de picos vacía")
        self.assertTrue(r.get("per_scene_max_fall"), "serie de medias vacía")
        self.assertEqual(r.get("total_frames"), FRAMES)
        # El L8 del RPU llega a las referencias del chart (era el bug de los
        # niveles que nunca aparecían).
        self.assertTrue(r["references"]["l8_trim_nits_full"], r["references"])

    async def test_el_progreso_llega_al_estado_del_modal(self):
        """El helper emite `§§PROGRESS§§` (contrato del Tab 3); el adaptador
        lo traduce al estado que pollea el modal de Tab 2."""
        await self.analizar()
        st = self.main._light_profile_state
        self.assertEqual(st["global_pct"], 100)
        self.assertIsNone(st["error"])

    async def test_el_workdir_queda_limpio(self):
        await self.analizar()
        self.assertEqual(list(self.work.glob("lightprof_*")), [],
                         "el workdir temporal no se ha borrado")


class TestCaminoDeReserva(LightProfileTestCase):

    async def _analizar_con_pipe_roto(self):
        """Simula que el pipe no está disponible: el helper devuelve False sin
        lanzar, que es su contrato para que el caller pueda recurrir a las dos
        pasadas."""
        import phases.cmv40_pipeline as pipeline
        original = pipeline._ffmpeg_extract_rpu_piped

        async def _no_disponible(*a, **k):
            return False

        pipeline._ffmpeg_extract_rpu_piped = _no_disponible
        try:
            return await self.analizar()
        finally:
            pipeline._ffmpeg_extract_rpu_piped = original

    async def test_si_el_pipe_falla_se_extrae_en_dos_pasadas(self):
        r = await self._analizar_con_pipe_roto()
        self.assertTrue(r.get("per_scene_max_cll"), "la reserva no produjo perfil")
        ff = self.tb.one("ffmpeg")
        self.assertNotIn("pipe:1", ff.argv, "la reserva escribe a fichero")
        extract = self.tb.one("dovi_tool", "extract-rpu")
        self.assertTrue(any(a.endswith(".hevc") for a in extract.argv),
                        f"la reserva lee el HEVC de disco: {extract.argv}")

    async def test_la_reserva_si_escribe_el_hevc_y_lo_borra(self):
        await self._analizar_con_pipe_roto()
        self.assertEqual(self.hevc_en_disco(), [],
                         "el HEVC de la reserva debe borrarse al terminar")


if __name__ == "__main__":
    unittest.main()


class TestUnSoloParserDelExport(unittest.TestCase):
    """`_perfil_desde_niveles` es el ÚNICO consumidor del export del RPU.

    Había dos parsers del mismo volcado: el de `rpu_analyze` (plano, con
    tests) y otro inline en el endpoint (anidado, sin tests) — más
    `_rpus_from_levels`, que RECONSTRUÍA la forma anidada desde el export
    plano solo para alimentar al segundo. Medido con 243.552 frames, esa
    reconstrucción costaba 3,1 s y un pico de 450 MB de RAM, y el árbol se
    navegaba después en el event loop.

    Ahora el volcado anidado se adapta al formato plano
    (`_niveles_desde_volcado`) y desemboca en el mismo consumidor. La regla del
    proyecto —un solo parser de ese JSON— vuelve a cumplirse.
    """

    def setUp(self):
        import main
        self.main = main

    # ── el formato plano de export_levels ────────────────────────────
    PLANO = {
        "level1": [
            {"frame": 0, "min_pq": 7, "avg_pq": 900, "max_pq": 2081},
            {"frame": 1, "min_pq": 7, "avg_pq": 950, "max_pq": 2200},
        ],
        "level2": [{"frame": 0, "target_max_pq": 2081}],
        "level5": [{"frame": 0, "active_area_top_offset": 140,
                    "active_area_bottom_offset": 140,
                    "active_area_left_offset": 0, "active_area_right_offset": 0}],
        "level6": [{"frame": 0, "max_display_mastering_luminance": 1000,
                    "min_display_mastering_luminance": 1,
                    "max_content_light_level": 300,
                    "max_frame_average_light_level": 120}],
        "level8": [{"frame": 0, "target_display_index": 1, "target_max_pq": 2081},
                   {"frame": 1, "target_display_index": 1, "target_max_pq": 2500}],
    }

    def test_las_tres_series_salen_del_l1(self):
        r = self.main._perfil_desde_niveles(self.PLANO)
        self.assertEqual(len(r["cll"]), 2)
        self.assertEqual(len(r["fall"]), 2)
        self.assertEqual(len(r["min"]), 2)
        self.assertEqual(r["raw_max_pq"], 2200)
        # nits crecientes con el max_pq
        self.assertLess(r["cll"][0], r["cll"][1])

    def test_l8_se_queda_con_el_max_pq_mas_alto_por_target(self):
        r = self.main._perfil_desde_niveles(self.PLANO)
        self.assertEqual(r["l8_por_target"], {1: 2500})

    def test_l6_se_lee_del_primer_registro_y_min_va_en_diezmilesimas(self):
        r = self.main._perfil_desde_niveles(self.PLANO)
        self.assertEqual(r["l6"]["max_nits"], 1000)
        self.assertAlmostEqual(r["l6"]["min_nits"], 0.0001)
        self.assertEqual(r["l6"]["max_cll"], 300)

    def test_un_l1_incoherente_se_descarta(self):
        """La comprobación `min <= avg <= max` venía del bug de BR2049: la
        búsqueda a ciegas encontraba bloques hermanos con campos homónimos y
        daba un peak de ~176 nits donde el real era ~1000."""
        malo = {"level1": [
            {"frame": 0, "min_pq": 900, "avg_pq": 500, "max_pq": 100},  # invertido
            {"frame": 1, "min_pq": 7, "avg_pq": 900, "max_pq": 99999},  # fuera de 12-bit
            {"frame": 2, "min_pq": 7, "avg_pq": 900, "max_pq": 2081},   # bueno
        ]}
        r = self.main._perfil_desde_niveles(malo)
        self.assertEqual(len(r["cll"]), 1, "solo el coherente debe contar")
        self.assertEqual(r["raw_max_pq"], 2081)

    def test_sin_l1_no_hay_series(self):
        r = self.main._perfil_desde_niveles({"level6": self.PLANO["level6"]})
        self.assertEqual(r["cll"], [])

    # ── el volcado anidado desemboca en lo mismo ─────────────────────
    def _volcado_anidado(self):
        """Forma (c) de `dovi_tool export`: tagged enum de serde, la variante
        más común en 2.3.x, con los bloques separados en cmv29/cmv40."""
        def frame(i, mx, avg, l8_pq):
            return {"vdr_dm_data": {
                "cmv29_metadata": {"ext_metadata_blocks": [
                    {"Level1": {"min_pq": 7, "avg_pq": avg, "max_pq": mx}},
                    {"Level2": {"target_max_pq": 2081}},
                    {"Level5": {"active_area_top_offset": 140,
                                "active_area_bottom_offset": 140,
                                "active_area_left_offset": 0,
                                "active_area_right_offset": 0}},
                    {"Level6": {"max_display_mastering_luminance": 1000,
                                "min_display_mastering_luminance": 1,
                                "max_content_light_level": 300,
                                "max_frame_average_light_level": 120}},
                ]},
                "cmv40_metadata": {"ext_metadata_blocks": [
                    {"Level8": {"target_display_index": 1, "target_max_pq": l8_pq}},
                ]},
            }}
        return [frame(0, 2081, 900, 2081), frame(1, 2200, 950, 2500)]

    def test_el_volcado_anidado_da_el_mismo_perfil_que_el_plano(self):
        """Es lo que hace que se pueda tener UN consumidor: el camino legacy
        se adapta, no se parsea aparte."""
        niveles = self.main._niveles_desde_volcado(self._volcado_anidado())
        desde_anidado = self.main._perfil_desde_niveles(niveles)
        desde_plano = self.main._perfil_desde_niveles(self.PLANO)
        for clave in ("cll", "fall", "min", "l8_por_target", "l2_targets_pq",
                      "l6", "raw_max_pq"):
            self.assertEqual(desde_anidado[clave], desde_plano[clave], clave)

    def test_el_volcado_con_bloques_colgando_del_contenedor(self):
        """Forma (b): `{"level1": {...}}` directamente bajo el contenedor, sin
        `ext_metadata_blocks`."""
        rpus = [{"vdr_dm_data": {"cmv29_metadata": {
            "level1": {"min_pq": 7, "avg_pq": 900, "max_pq": 2081}}}}]
        r = self.main._perfil_desde_niveles(self.main._niveles_desde_volcado(rpus))
        self.assertEqual(len(r["cll"]), 1)
        self.assertEqual(r["raw_max_pq"], 2081)

    def test_el_volcado_con_level_numerico(self):
        """Forma (a): lista de bloques con un campo `level` entero."""
        rpus = [{"vdr_dm_data": {"cmv29_metadata": {"ext_metadata_blocks": [
            {"level": 1, "min_pq": 7, "avg_pq": 900, "max_pq": 2081}]}}}]
        r = self.main._perfil_desde_niveles(self.main._niveles_desde_volcado(rpus))
        self.assertEqual(len(r["cll"]), 1)

    def test_un_volcado_basura_no_revienta(self):
        for basura in ([], [None], [{}], [{"vdr_dm_data": None}],
                       [{"vdr_dm_data": {"cmv29_metadata": "no soy dict"}}]):
            r = self.main._perfil_desde_niveles(
                self.main._niveles_desde_volcado(basura))
            self.assertEqual(r["cll"], [], basura)
