# -*- coding: utf-8 -*-
"""El brillo de un target L8 sale de su ÍNDICE, no de un campo de brillo.

Lo destapó una duda del usuario (2026-09-24): «la escala L8 de los
niveles del RPU no sé muy bien a qué se refiere». No se refería a nada:
pintaba el neutro del `trim_slope` convertido a nits.

**Un bloque L8 no lleva ningún campo de brillo.** Verificado contra
dovi_tool 2.3.3 sobre un RPU real, la cabecera del export es:

    frame,length,target_display_index,trim_slope,trim_offset,trim_power,
    trim_chroma_weight,trim_saturation_gain,ms_weight

`luminance` buscaba `target_max_pq` —que es un campo de L2— y caía a
`trim_slope`. Como el neutro del trim es 2048 y `pq_code_to_nits(2048)`
son 92 nits, la pantalla enseñaba «92 nits» como si fuera una pantalla
de destino. Medido sobre el NAS: Backrooms decía `[92]` y El padrino
`[97, 244]`, cuando sus índices reales son 1 y 48 — o sea 100 y 1000.

Y el mismo RPU se resolvía BIEN por el otro camino
(`mkv_analyze._enrich_dovi_from_json_export`, que sí usa el índice), así
que la radiografía llegó a enseñar dos listas distintas de lo mismo en
dos bloques contiguos. De ahí que la tabla sea una sola.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_target_de_l8 -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from phases.luminance import (payload_de_luminancia,  # noqa: E402
                              perfil_desde_niveles, pq_code_to_nits)
from phases.rpu_analyze import L8_NITS_POR_INDICE  # noqa: E402

#: Las columnas REALES de un export de level8, con los índices que trae
#: El padrino en el NAS. Ni una sola columna de brillo, que es el asunto.
L8_REAL = [
    {"frame": 0, "length": 10, "target_display_index": 1, "trim_slope": 2048,
     "trim_offset": 2048, "trim_power": 2048, "trim_chroma_weight": 2048,
     "trim_saturation_gain": 2048, "ms_weight": 2048},
    {"frame": 0, "length": 10, "target_display_index": 48, "trim_slope": 1257,
     "trim_offset": 2015, "trim_power": 995, "trim_chroma_weight": 2048,
     "trim_saturation_gain": 2048, "ms_weight": 2048},
]


class TestElTargetSaleDelIndice(unittest.TestCase):

    def test_los_indices_reales_dan_los_nits_reales(self):
        p = perfil_desde_niveles({"level8": L8_REAL})
        self.assertEqual(sorted(p["l8_indices"]), [1, 48])
        nits = sorted(L8_NITS_POR_INDICE[i] for i in p["l8_indices"])
        self.assertEqual(nits, [100, 1000])

    def test_y_NO_el_neutro_del_trim(self):
        """92 nits es `pq_code_to_nits(2048)`, o sea el trim sin tocar.

        Es el número que la pantalla enseñaba, y el que hacía la pregunta
        del usuario imposible de contestar.
        """
        self.assertEqual(round(pq_code_to_nits(2048)), 92)
        p = perfil_desde_niveles({"level8": L8_REAL})
        nits = {L8_NITS_POR_INDICE[i] for i in p["l8_indices"]}
        self.assertNotIn(92, nits)

    def test_un_bloque_l8_no_tiene_ningun_campo_de_brillo(self):
        """El guard de la premisa: si algún día lo tuviera, este test
        avisa de que la tabla de índices dejó de ser la única vía."""
        for fila in L8_REAL:
            for campo in ("target_max_pq", "target_mid_pq", "target_min_pq"):
                self.assertNotIn(campo, fila)

    def test_un_indice_desconocido_no_se_inventa(self):
        """Antes que un brillo inventado, un target de menos."""
        p = perfil_desde_niveles({"level8": [
            {"target_display_index": 999, "trim_slope": 2048}]})
        self.assertEqual(p["l8_indices"], {999})
        self.assertNotIn(999, L8_NITS_POR_INDICE)

    def test_sin_indice_la_fila_se_salta(self):
        p = perfil_desde_niveles({"level8": [
            {"target_display_index": 0, "trim_slope": 1000},
            {"trim_slope": 1000},
        ]})
        self.assertEqual(p["l8_indices"], set())


class TestLaTablaEsUNA(unittest.TestCase):
    """Tenerla dos veces fue el bug: un camino acertaba y el otro no."""

    def test_mkv_analyze_usa_la_de_rpu_analyze(self):
        from phases import mkv_analyze
        self.assertIs(mkv_analyze._L8_NITS_POR_INDICE, L8_NITS_POR_INDICE)

    def test_y_luminance_tambien(self):
        from phases import luminance
        self.assertIs(luminance.L8_NITS_POR_INDICE, L8_NITS_POR_INDICE)

    def test_los_targets_de_dolby_estan(self):
        self.assertEqual(sorted(set(L8_NITS_POR_INDICE.values())),
                         [100, 350, 600, 1000, 2000, 4000])


class TestLoQueLLEGAALAPANTALLA(unittest.TestCase):
    """El perfil es el paso intermedio; lo que se pinta es el payload.

    Los tests de arriba miran `l8_indices` y el mapeo por separado, y la
    mutación que devuelve el neutro del trim vive en la línea que los
    junta — así que pasaban en verde con el bug puesto. Es la diferencia
    entre probar las piezas y probar el resultado.
    """

    #: mínimo para que el payload se componga: una serie L1 y los L8
    NIVELES = {
        "level1": [{"frame": i, "min_pq": 10, "max_pq": 2500, "avg_pq": 1000}
                   for i in range(10)],
        "level8": L8_REAL,
    }

    def test_las_pantallas_que_se_pintan_son_100_y_1000(self):
        refs = payload_de_luminancia(self.NIVELES)["references"]
        self.assertEqual(refs["l8_trim_nits_full"], [100, 1000])

    def test_y_nunca_los_92_nits_del_neutro(self):
        refs = payload_de_luminancia(self.NIVELES)["references"]
        self.assertNotIn(92, refs["l8_trim_nits_full"])

    def test_un_indice_desconocido_no_llega_a_la_pantalla(self):
        niveles = dict(self.NIVELES, level8=[
            {"target_display_index": 999, "trim_slope": 2048}])
        refs = payload_de_luminancia(niveles)["references"]
        self.assertEqual(refs["l8_trim_nits_full"], [])


if __name__ == "__main__":
    unittest.main()
