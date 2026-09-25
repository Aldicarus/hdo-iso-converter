"""Una serie de L1 plana no se puede correlacionar, y hay que decirlo.

El RPU del MKV de Drive (2011) —descargado, no ripeado— tiene el L1 de
relleno: **2 valores distintos en 144.683 frames**, MaxCLL 235,66 y MaxFALL
2,43 nits sobre un máster declarado de 1000. `dovi_tool` lo confirma con
«Scene/shot count: 2». Es el único así de los 21 proyectos del NAS, donde la
mediana son 1512 escenas.

Con una serie constante la correlación de Pearson no es aplicable: divide por
las desviaciones típicas y el resultado es ruido numérico. El código ya
trataba la varianza EXACTAMENTE cero como caso aparte, pero con dos valores
distintos no entraba y devolvía un «3 %» que se lee como desalineación cuando
lo cierto es que no hay con qué medir. Y con el Δ ya en 0, el proyecto se
quedaba sin ningún camino: el botón de confirmar apagado y nada más.

El corte son **0,01 de desviación típica relativa**, medido sobre las series
del NAS: la plana da 0,0016 y las normales 0,072 y 0,109 — factor 44 entre
los dos grupos, con margen de 6× por cada lado.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_serie_plana -v
"""
import math
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from phases.cmv40_pipeline import (compute_sync_confidence,  # noqa: E402
                                   evaluate_sync_gate)


def _volcado(src, tgt):
    return {
        "source_frames": len(src), "target_frames": len(tgt),
        "data": [{"frame": i, "src_maxcll": a, "tgt_maxcll": b}
                 for i, (a, b) in enumerate(zip(src, tgt))],
    }


def _onda(n, centro=2600, amplitud=200):
    """Una serie con la variación de una película de verdad (σ/media ≈ 0,05)."""
    return [round(centro + amplitud * math.sin(i / 7.0)) for i in range(n)]


def _drive(n):
    """La del MKV de Drive: 2442 casi siempre, 2359 de higos a brevas."""
    return [2359 if i % 400 == 0 else 2442 for i in range(n)]


class TestSeDetectaLaSeriePlana(unittest.TestCase):

    def test_la_de_drive_no_es_correlacionable(self):
        c = compute_sync_confidence(_volcado(_drive(4000), _onda(4000)))
        self.assertEqual(c["rating"], "no_variance")
        self.assertEqual(c["confidence_pct"], 0)

    def test_y_dice_CUAL_de_las_dos(self):
        # Es lo que dice dónde mirar: el problema está en el MKV, no en el bin.
        c = compute_sync_confidence(_volcado(_drive(4000), _onda(4000)))
        self.assertEqual(c["lado_plano"], "source")
        d = compute_sync_confidence(_volcado(_onda(4000), _drive(4000)))
        self.assertEqual(d["lado_plano"], "target")

    def test_dos_series_normales_se_miden_como_siempre(self):
        c = compute_sync_confidence(_volcado(_onda(4000), _onda(4000)))
        self.assertNotEqual(c["rating"], "no_variance")
        self.assertGreater(c["confidence_pct"], 85)

    def test_el_motivo_explica_que_no_es_desalineacion(self):
        c = compute_sync_confidence(_volcado(_drive(4000), _onda(4000)))
        self.assertIn("plano", c["reason"].lower())

    def test_la_constante_exacta_sigue_cubierta(self):
        # El caso que ya estaba: varianza CERO.
        plana = [2442] * 4000
        c = compute_sync_confidence(_volcado(plana, _onda(4000)))
        self.assertEqual(c["rating"], "no_variance")


class TestElUmbralSepara(unittest.TestCase):
    """0,0016 contra 0,072: el corte al 1 % deja 6× de margen por lado."""

    def _sigma_relativa(self, serie):
        m = sum(serie) / len(serie)
        return (sum((x - m) ** 2 for x in serie) / len(serie)) ** 0.5 / m

    def test_la_de_drive_queda_por_debajo(self):
        self.assertLess(self._sigma_relativa(_drive(4000)), 0.01)

    def test_y_una_normal_muy_por_encima(self):
        self.assertGreater(self._sigma_relativa(_onda(4000)), 0.01)

    def test_una_serie_apenas_variable_tambien_es_plana(self):
        # Ruido de ±2 sobre 2442: muchos valores distintos, ninguna señal.
        # El criterio por número de valores distintos fallaría aquí.
        ruido = [2442 + (i % 5) - 2 for i in range(4000)]
        self.assertLess(self._sigma_relativa(ruido), 0.01)
        c = compute_sync_confidence(_volcado(ruido, _onda(4000)))
        self.assertEqual(c["rating"], "no_variance")


class TestElGateOfreceLaSalida(unittest.TestCase):

    def _gate(self, delta, src, tgt):
        v = _volcado(src, tgt)
        return evaluate_sync_gate(v, sync_delta=delta)

    def test_con_el_recuento_cuadrado_se_marca_como_no_medible(self):
        g = self._gate(0, _drive(4000), _onda(4000))
        self.assertTrue(g["no_medible"])
        self.assertFalse(g["ok"], "no se aprueba solo: lo decide el usuario")

    def test_el_motivo_ya_no_habla_de_un_umbral(self):
        # «Confianza 3 % inferior al umbral 85 %» describe una desalineación
        # que no existe y manda a corregir un sync que está bien.
        g = self._gate(0, _drive(4000), _onda(4000))
        self.assertNotIn("85", g["reason"])

    def test_con_el_recuento_descuadrado_lo_primero_es_el_recuento(self):
        g = self._gate(-426, _drive(4000), _onda(4000))
        self.assertFalse(g["no_medible"])
        self.assertFalse(g["delta_ok"])

    def test_y_una_desalineacion_DE_VERDAD_no_se_marca_asi(self):
        # Dos series con variación que no correlacionan: eso sí es un
        # problema de sync y tiene que seguir diciéndolo.
        otra = _onda(4000, centro=2600, amplitud=200)[::-1]
        g = self._gate(0, _onda(4000), otra)
        self.assertFalse(g["no_medible"])
        self.assertFalse(g["ok"])

    def test_y_lo_alineado_sigue_pasando(self):
        g = self._gate(0, _onda(4000), _onda(4000))
        self.assertTrue(g["ok"])
        self.assertFalse(g["no_medible"])


if __name__ == "__main__":
    unittest.main()
