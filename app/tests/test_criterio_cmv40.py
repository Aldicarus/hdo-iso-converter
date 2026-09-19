"""El criterio de calidad CMv4.0, anclado a bins REALES medidos.

Recalibrado el 2026-09-18 tras un estudio sobre el repo DoviTools: 40 bins
(18 retail, 22 generados por análisis) más dos pares controlados —mismo
título con las dos procedencias, que es el experimento limpio porque el
título deja de ser una variable.

## Por qué la magnitud y no el conteo

Dolby: *«L8 is not generated and must fully come from the colorist»*. Así
que un L8 que no se aparta del neutro es, por construcción, lo que
`cm_analyze` produciría sobre tu propio disco — procesarlo no aporta nada.

    generados por análisis (n=19) ..  maxΔ  0 – 30   · mid/clip 0 de 17
    másters con colorista (n=11) ..  maxΔ  126 – 2.046 · mid/clip 11 de 18

Ni un solo solape, con el umbral de 50 en medio. En la tanda de cierre los
17 generados dieron maxΔ **exactamente 0** con 1-2 combos y L3 de 773 a
3.226: la firma de `cm_analyze`, que analiza el contenido (L1, L3) y no
inventa trims (L8). Ninguno traía L3 identidad, así que ninguno es de
avdvplus.

El conteo de combos NO separa (Dogma: 2 combos y maxΔ 606; un generado: 2
combos y maxΔ 0) y con él se perdían **5 de 18** retail. El % de frames
neutros tampoco (retail al 81 %, generados al 33 %).

## Lo que NO decide, y está medido

L3 acierta el 57 % —azar— con mediana mayor en los generados. L2 el 65 %,
y encima un generado hereda el L2 rico de tu propio disco. L1 el 65 %.

Cada test lleva el bin real del que salen sus números.
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from models import L8Combo  # noqa: E402
from phases.rpu_analyze import (  # noqa: E402
    RpuAnalysis, classify_l8, delta_l8_de, _l8_delta_maxima,
    L8_REAL_MINIMAL_SIGNIFICANT_DELTA,
)


def bin_real(*combos, frames=100000, l3=0, l2=0, cortes=0) -> RpuAnalysis:
    """Un RpuAnalysis con los combos L8 tal como salen del export."""
    a = RpuAnalysis()
    a.frames_with_cmv40 = frames
    a.l8_unique_count = len(combos)
    a.l3_unique_count = l3
    a.l2_unique_count = l2
    a.scene_cuts = cortes
    a.l8_combos = [
        L8Combo(target_display_index=c.get("idx", 1),
                trim_slope=c.get("slope", 2048), trim_offset=c.get("off", 2048),
                trim_power=c.get("pow", 2048), trim_chroma_weight=c.get("chroma", 2048),
                trim_saturation_gain=c.get("sat", 2048), ms_weight=c.get("ms", 0),
                target_mid_contrast=c.get("mid"), clip_trim=c.get("clip"),
                occurrence_count=c.get("n", frames))
        for c in combos
    ]
    a.l8_max_delta = _l8_delta_maxima(a.l8_combos)
    return a


class TestLosBinsRealesMedidos(unittest.TestCase):
    """Cada caso es un bin del repo, con sus valores tal cual se midieron."""

    def test_posesion_infernal_dos_combos_pero_trims_fuertes(self):
        """`Evil.Dead.Burn.2026.UHD-BD_P7 MEL_(retail cmv4.0 restored).bin`

        159.030 frames, 2.585 cortes. DOS combos L8, y uno lleva
        `power=1864` y `sat=1720` en 158.942 frames → maxΔ 328. Su L3 es
        un único combo en 88 frames: residual.

        El criterio viejo (combos <= 2 -> sintético) lo descartaba. Es el
        caso que motivó la recalibración.
        """
        a = bin_real({"pow": 1864, "sat": 1720, "n": 158942},
                     {"idx": 25, "n": 158942},
                     frames=159030, l3=1, l2=3, cortes=2585)
        self.assertEqual(a.l8_max_delta, 328)
        self.assertEqual(classify_l8(a)[0], "real")

    def test_dogma_dos_combos_y_delta_606(self):
        """`Dogma.1999.US-BD Retail P7 to P8.bin` — 2 combos, maxΔ 606."""
        a = bin_real({"slope": 2048 - 606, "clip": 1442}, {"n": 10})
        self.assertEqual(classify_l8(a)[0], "real")

    def test_bridesmaids_el_par_controlado(self):
        """Mismo título, las dos procedencias. 3 combos el retail y 2 el
        generado: el conteo casi no separa, la magnitud sí (225 vs 30)."""
        retail = bin_real({"slope": 2048 + 225}, {}, {})
        generado = bin_real({"slope": 2048 + 30}, {})
        self.assertEqual(classify_l8(retail)[0], "real")
        self.assertEqual(classify_l8(generado)[0], "default")

    def test_devil_wears_prada_2_el_otro_par(self):
        """7 combos el retail (maxΔ 126) contra 2 el generado (maxΔ 0)."""
        retail = bin_real(*([{"slope": 2048 + 126}] + [{}] * 6))
        generado = bin_real({}, {})
        self.assertEqual(classify_l8(retail)[0], "real")
        self.assertEqual(classify_l8(generado)[0], "default")

    def test_avatar_fire_and_ash_un_combo_neutro(self):
        """Retail que la regla descarta con razón: su L8 no trae trims."""
        self.assertEqual(classify_l8(bin_real({}))[0], "default")

    def test_pans_labyrinth_sin_bloques_l8(self):
        """`P5 to P8`: cero bloques L8. No hay nada que transferir."""
        a = RpuAnalysis()
        a.frames_with_cmv40 = 0
        self.assertEqual(classify_l8(a)[0], "default")

    def test_un_generado_con_trims_manuales_si_pasa(self):
        """`Annihilation … ONLY Generated OSC T3 (MANUAL TRIM…)`: 135 combos
        y `clip_trim`. El nombre dice que le añadieron trims a mano, así
        que inyectarlo es lo correcto — no es un falso positivo."""
        a = bin_real({"slope": 2048 + 400, "clip": 1900}, *([{}] * 134))
        self.assertEqual(classify_l8(a)[0], "real")


class TestElUmbralYSuMargen(unittest.TestCase):

    def test_el_umbral_es_el_que_ya_estaba_calibrado(self):
        self.assertEqual(L8_REAL_MINIMAL_SIGNIFICANT_DELTA, 50)

    def test_el_margen_medido_cae_a_los_dos_lados(self):
        """Generado más fuerte: 30. Retail con L8 real más flojo: 126."""
        self.assertEqual(classify_l8(bin_real({"slope": 2048 + 30}))[0], "default")
        self.assertEqual(classify_l8(bin_real({"slope": 2048 + 126}))[0], "real")

    def test_el_signo_del_trim_da_igual(self):
        """Los trims reales van en los dos sentidos: Posesión infernal los
        tiene NEGATIVOS (power 1864, sat 1720)."""
        for d in (+300, -300):
            self.assertEqual(classify_l8(bin_real({"slope": 2048 + d}))[0], "real")

    def test_mid_contrast_y_clip_trim_cuentan_como_trim(self):
        """Son CMv4.0-only y el análisis no los rellena: 11/18 retail los
        traen, 1/22 generados."""
        self.assertEqual(classify_l8(bin_real({"mid": 2121}))[0], "real")
        self.assertEqual(classify_l8(bin_real({"clip": 1901}))[0], "real")
        # …pero presentes A 2048 no son trabajo (audit #14).
        self.assertEqual(classify_l8(bin_real({"mid": 2048, "clip": 2048}))[0], "default")


class TestElContrato(unittest.TestCase):

    def test_solo_dos_veredictos(self):
        vistos = set()
        for d in (0, 1, 30, 50, 51, 126, 2046):
            for n in (1, 2, 3, 400):
                vistos.add(classify_l8(bin_real(*([{"slope": 2048 + d}] + [{}] * (n - 1))))[0])
        self.assertEqual(vistos, {"real", "default"})

    def test_la_magnitud_se_deriva_si_el_campo_no_viene(self):
        """Un `RpuAnalysis` se construye por cuatro vías. Si el criterio
        dependiera solo del campo, las que no lo fijan darían 0 y TODO
        saldría sintético, sin un error."""
        a = bin_real({"slope": 2048 + 400})
        a.l8_max_delta = 0          # como si viniera de otro parser
        self.assertEqual(delta_l8_de(a), 400)
        self.assertEqual(classify_l8(a)[0], "real")

    def test_ms_weight_no_es_un_trim(self):
        """Su neutro es 0, no 2048. Contarlo daba maxΔ 2048 en cualquier
        bin y volvía el criterio inútil."""
        self.assertEqual(_l8_delta_maxima(bin_real({"ms": 0}).l8_combos), 0)
        self.assertEqual(classify_l8(bin_real({"ms": 0}))[0], "default")


if __name__ == "__main__":
    unittest.main()
