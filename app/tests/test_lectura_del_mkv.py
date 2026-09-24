# -*- coding: utf-8 -*-
"""Las tres frases que interpretan un MKV.

El encargo del usuario (2026-09-24): «como leer un análisis de un UHD,
pero automático». La radiografía tenía ~60 números en seis bloques y ni
una frase que los juntara.

Los casos son los MKV reales de su NAS, con los números tal cual se
midieron — incluido el que obligó a diseñarlo distinto: **tres de los
siete con perfil de luz tienen la serie L1 con un solo valor**, y ahí
una frase sobre la distribución sería inventada.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_lectura_del_mkv -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from phases.mkv_lectura import lectura_de  # noqa: E402

HDR = {"hdr_format": "HDR10", "max_cll": 1000,
       "hdr_format_raw": "Dolby Vision / SMPTE ST 2086",
       "hdr_format_compatibility": "Blu-ray / HDR10",
       "dv_profile_string": "dvhe.07",
       "mastering_display_primaries": "BT.2020",
       "mastering_display_luminance": "min: 0.0050 cd/m2, max: 1000 cd/m2"}

PULP = {"hdr": dict(HDR), "dovi": {
    "profile": 7, "el_type": "FEL", "cm_version": "v2.9",
    "quality_l2_unique_count": 1605, "quality_l2_target_pqs": [1, 2, 3],
    "quality_l8_unique_count": 0,
    "quality_verdict_text": "Master CMv2.9 nativo — grading rico del Blu-ray UHD",
    "quality_verdict_color": "green",
    "l1_stats": {"peak": 1001, "p50": 437, "total": 222274, "bucket_high": 171151},
    "per_scene_max_cll": [100, 500, 1001],
    "l1_references": {"l6_master_max_nits": 1000}}}

APOCALYPSE = {"hdr": dict(HDR, mastering_display_primaries="Display P3"), "dovi": {
    "profile": 7, "el_type": "FEL",
    "quality_l2_unique_count": 1026, "quality_l2_target_pqs": [1, 2],
    "l1_stats": {"peak": 4082, "p50": 472, "total": 261792, "bucket_high": 183254},
    "per_scene_max_cll": [122, 900, 4082],
    "l1_references": {"l6_master_max_nits": 1000}}}

#: Drive: 240 puntos y UN solo valor. Comprobado sobre el disco — el
#: máster alterna entre dos códigos PQ y la reducción por máximo la deja
#: plana.
DRIVE = {"hdr": dict(HDR, mastering_display_primaries="Display P3"), "dovi": {
    "profile": 7, "el_type": "FEL",
    "l1_stats": {"peak": 236, "p50": 236, "total": 144683, "bucket_high": 0},
    "per_scene_max_cll": [236] * 240}}


def _por_rotulo(analisis):
    return {f["rotulo"]: f for f in (lectura_de(analisis) or [])}


class TestLasTresFrases(unittest.TestCase):

    def test_salen_las_tres_y_en_orden(self):
        rot = [f["rotulo"] for f in lectura_de(PULP)]
        self.assertEqual(rot, ["Qué es", "El máster", "La luz"])

    def test_sin_hdr_ni_dv_no_hay_lectura(self):
        self.assertIsNone(lectura_de({"hdr": None, "dovi": None}))
        self.assertIsNone(lectura_de("no soy un dict"))

    def test_sin_analisis_extendido_no_hay_frase_de_luz(self):
        """El pico del sniff de 30 s no describe la película — es el
        defecto que ya costó un banner de divergencia al revés."""
        sin = {"hdr": dict(HDR), "dovi": {"profile": 7, "el_type": "FEL",
                                          "l1_max_cll": 395.0}}
        self.assertEqual([f["rotulo"] for f in lectura_de(sin)],
                         ["Qué es", "El máster"])


class TestQueEs(unittest.TestCase):

    def test_la_base_es_HDR10_no_el_literal_de_mediainfo(self):
        """`hdr_format_raw` ya empieza por «Dolby Vision», así que la
        frase salía diciendo «Dolby Vision Profile 7 FEL sobre Dolby
        Vision / SMPTE ST 2086»."""
        texto = _por_rotulo(PULP)["Qué es"]["texto"]
        self.assertIn("Profile 7 FEL sobre HDR10", texto)
        self.assertNotIn("SMPTE", texto)

    def test_dice_con_que_se_reproduce(self):
        self.assertIn("Blu-ray / HDR10", _por_rotulo(PULP)["Qué es"]["texto"])

    def test_un_perfil_declarado_distinto_se_avisa(self):
        """Es la firma del MKV anunciado dual-layer sin capa de mejora."""
        raro = {"hdr": dict(HDR, dv_profile_string="dvhe.07"),
                "dovi": {"profile": 8, "el_type": ""}}
        c = _por_rotulo(raro)["Qué es"]["conclusion"]
        self.assertIn("dvhe.07", c)
        self.assertIn("dvhe.08", c)

    def test_y_si_coinciden_se_habla_de_la_capa(self):
        c = _por_rotulo(PULP)["Qué es"]["conclusion"]
        self.assertIn("Profile 7", c)
        self.assertNotIn("declara", c)


class TestElMaster(unittest.TestCase):

    def test_el_veredicto_es_la_conclusion(self):
        self.assertEqual(_por_rotulo(PULP)["El máster"]["conclusion"],
                         "Master CMv2.9 nativo — grading rico del Blu-ray UHD")

    def test_en_cmv29_el_numero_que_sostiene_es_el_L2(self):
        t = _por_rotulo(PULP)["El máster"]["texto"]
        self.assertIn("1.605", t)
        self.assertIn("3", t)

    def test_en_cmv40_es_el_L8_con_su_desviacion(self):
        v40 = {"hdr": dict(HDR), "dovi": {
            "profile": 7, "el_type": "FEL", "quality_l8_unique_count": 210,
            "quality_l8_max_delta": 606, "quality_l2_unique_count": 44}}
        t = _por_rotulo(v40)["El máster"]["texto"]
        self.assertIn("210", t)
        self.assertIn("606", t)
        self.assertNotIn("44", t)

    def test_sin_auditoria_lo_dice(self):
        t = _por_rotulo(DRIVE)["El máster"]["texto"]
        self.assertIn("análisis extendido", t)


class TestLaLuz(unittest.TestCase):

    def test_pulp_coincide_con_su_master(self):
        """1001 contra 1000 no es una divergencia."""
        f = _por_rotulo(PULP)["La luz"]
        self.assertIn("1001", f["texto"])
        self.assertIn("77 %", f["texto"])
        self.assertIn("coincide", f["conclusion"])

    def test_apocalypse_declara_cuatro_veces_el_master(self):
        c = _por_rotulo(APOCALYPSE)["La luz"]["conclusion"]
        self.assertIn("4082", c)
        self.assertIn("1000", c)

    def test_un_master_conservador_tambien_se_dice(self):
        conservador = {"hdr": dict(HDR), "dovi": dict(
            PULP["dovi"], l1_stats={"peak": 176, "p50": 90, "total": 1000,
                                    "bucket_high": 10},
            per_scene_max_cll=[50, 90, 176])}
        c = _por_rotulo(conservador)["La luz"]["conclusion"]
        self.assertIn("conservadora", c)

    def test_una_serie_PLANA_no_describe_una_distribucion(self):
        """Tres de los siete MKV medidos. Comprobado sobre el disco: el
        máster alterna entre dos códigos PQ (194 y 236 nits) y la
        reducción por máximo deja la serie con un único valor. Hablar
        aquí de mediana y de percentiles sería inventarlo.
        """
        f = _por_rotulo(DRIVE)["La luz"]
        self.assertIn("236", f["texto"])
        self.assertNotIn("%", f["texto"])
        self.assertNotIn("mediana", f["texto"].lower())
        self.assertIn("no varía", f["conclusion"])

    def test_sin_master_declarado_no_se_opina(self):
        """Cuatro de los siete no traen MaxCLL en el SEI."""
        sin = {"hdr": {"hdr_format": "HDR10"}, "dovi": dict(
            PULP["dovi"], l1_references={})}
        self.assertEqual(_por_rotulo(sin)["La luz"]["conclusion"], "")


class TestNoSePersiste(unittest.TestCase):

    def test_la_lectura_no_es_un_campo_del_modelo(self):
        """Se compone al servir, como `session.plan` y como el relato.

        Persistirla repetiría el error del veredicto: la caché guarda
        texto, y un análisis hecho en castellano se servía en castellano
        para siempre.
        """
        from models import MkvAnalysisResult
        self.assertNotIn("lectura", MkvAnalysisResult.model_fields)


class TestElEndpointLaSirve(unittest.TestCase):
    """Componerla y no entregarla es lo mismo que no tenerla."""

    def test_con_lectura_anade_las_frases(self):
        from routers.tab2 import _con_lectura
        datos = _con_lectura(dict(PULP))
        self.assertEqual([f["rotulo"] for f in datos["lectura"]],
                         ["Qué es", "El máster", "La luz"])

    def test_y_un_fallo_no_tumba_el_analisis(self):
        """Son tres frases: no pueden costar los diez minutos de un
        análisis extendido ya hecho."""
        from routers.tab2 import _con_lectura
        datos = _con_lectura({"hdr": {"max_cll": "no soy un número"},
                              "dovi": {"profile": 7, "l1_stats": "roto"}})
        self.assertIn("lectura", datos)

    def test_el_endpoint_de_analisis_pasa_por_ahi(self):
        """El invariante es de forma, así que se comprueba en la forma:
        ejecutar el endpoint pediría montar el MKV, sus binarios y el
        TestClient para afirmar una línea de cableado.
        """
        src = (APP_DIR / "routers" / "tab2.py").read_text(encoding="utf-8")
        i = src.index("async def analyze_mkv_endpoint")
        cuerpo = src[i:src.index("\n@router", i)]
        devoluciones = [l.strip() for l in cuerpo.splitlines()
                        if "result.model_dump()" in l]
        self.assertTrue(devoluciones, "no se encontró el retorno del análisis")
        for d in devoluciones:
            self.assertIn("_con_lectura", d,
                          f"este retorno no sirve la lectura: {d}")


if __name__ == "__main__":
    unittest.main()
