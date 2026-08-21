"""El módulo de luminancia: un solo consumidor del export del RPU.

Había dos parsers del mismo volcado: el de `rpu_analyze` (plano, con tests) y
otro inline en el endpoint del perfil de luminancia (anidado, sin tests) — más
`_rpus_from_levels`, que RECONSTRUÍA la forma anidada desde el export plano solo
para alimentar al segundo. Medido con 243.552 frames, esa reconstrucción costaba
3,1 s y un pico de 450 MB de RAM, y el árbol se navegaba después en el event
loop.

Ahora el volcado anidado se adapta al formato plano (`niveles_desde_volcado`) y
desemboca en el mismo consumidor. La regla del proyecto —un solo parser de ese
JSON— vuelve a cumplirse.

El pipeline que produce el RPU ya no vive aquí: el perfil se calcula junto a la
auditoría de calidad, compartiendo la extracción, y eso se cubre en
`test_mkv_quality_pipeline.TestAnalisisExtendido`. El camino del pipe sin
fichero de salida, en `test_pipe_fase_a.TestProgresoSinFicheroDeSalida`.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_luminance -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))


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
        from phases import luminance
        self.lum = luminance

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
        r = self.lum.perfil_desde_niveles(self.PLANO)
        self.assertEqual(len(r["cll"]), 2)
        self.assertEqual(len(r["fall"]), 2)
        self.assertEqual(len(r["min"]), 2)
        self.assertEqual(r["raw_max_pq"], 2200)
        # nits crecientes con el max_pq
        self.assertLess(r["cll"][0], r["cll"][1])

    def test_l8_se_queda_con_el_max_pq_mas_alto_por_target(self):
        r = self.lum.perfil_desde_niveles(self.PLANO)
        self.assertEqual(r["l8_por_target"], {1: 2500})

    def test_l6_se_lee_del_primer_registro_y_min_va_en_diezmilesimas(self):
        r = self.lum.perfil_desde_niveles(self.PLANO)
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
        r = self.lum.perfil_desde_niveles(malo)
        self.assertEqual(len(r["cll"]), 1, "solo el coherente debe contar")
        self.assertEqual(r["raw_max_pq"], 2081)

    def test_sin_l1_no_hay_series(self):
        r = self.lum.perfil_desde_niveles({"level6": self.PLANO["level6"]})
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
        niveles = self.lum.niveles_desde_volcado(self._volcado_anidado())
        desde_anidado = self.lum.perfil_desde_niveles(niveles)
        desde_plano = self.lum.perfil_desde_niveles(self.PLANO)
        for clave in ("cll", "fall", "min", "l8_por_target", "l2_targets_pq",
                      "l6", "raw_max_pq"):
            self.assertEqual(desde_anidado[clave], desde_plano[clave], clave)

    def test_el_volcado_con_bloques_colgando_del_contenedor(self):
        """Forma (b): `{"level1": {...}}` directamente bajo el contenedor, sin
        `ext_metadata_blocks`."""
        rpus = [{"vdr_dm_data": {"cmv29_metadata": {
            "level1": {"min_pq": 7, "avg_pq": 900, "max_pq": 2081}}}}]
        r = self.lum.perfil_desde_niveles(self.lum.niveles_desde_volcado(rpus))
        self.assertEqual(len(r["cll"]), 1)
        self.assertEqual(r["raw_max_pq"], 2081)

    def test_el_volcado_con_level_numerico(self):
        """Forma (a): lista de bloques con un campo `level` entero."""
        rpus = [{"vdr_dm_data": {"cmv29_metadata": {"ext_metadata_blocks": [
            {"level": 1, "min_pq": 7, "avg_pq": 900, "max_pq": 2081}]}}}]
        r = self.lum.perfil_desde_niveles(self.lum.niveles_desde_volcado(rpus))
        self.assertEqual(len(r["cll"]), 1)

    def test_un_volcado_basura_no_revienta(self):
        for basura in ([], [None], [{}], [{"vdr_dm_data": None}],
                       [{"vdr_dm_data": {"cmv29_metadata": "no soy dict"}}]):
            r = self.lum.perfil_desde_niveles(
                self.lum.niveles_desde_volcado(basura))
            self.assertEqual(r["cll"], [], basura)
