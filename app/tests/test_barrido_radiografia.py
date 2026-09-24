# -*- coding: utf-8 -*-
"""El barrido: cada número de la radiografía contra lo que puede ser.

La auditoría de Tab 2 miró ESTRUCTURA —qué bloques hay, qué campos se
capturan, qué rama no se ejecuta— y no VALORES, así que se le escaparon
tres bugs que el usuario encontró usando la app. Los tres se cazan con
el mismo barrido mecánico, y ninguno exige saber de Dolby Vision:

  · un target de pantalla que no está en la tabla de Dolby;
  · dos caminos que calculan el mismo dato y no coinciden;
  · un texto con un hueco sin rellenar.

Este módulo convierte ese barrido en tests, para que la próxima vez lo
diga la suite y no el usuario.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_barrido_radiografia -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from phases.luminance import payload_de_luminancia  # noqa: E402
from phases.rpu_analyze import (L8_NITS_POR_INDICE,  # noqa: E402
                                PRIMARIES_POR_INDICE, rellenar_l9_l11)

#: Los únicos brillos de pantalla que Dolby define como target.
TARGETS_DOLBY = {100, 350, 600, 1000, 2000, 4000}


class TestUnTargetSoloPuedeSerDeDolby(unittest.TestCase):
    """Si sale un número fuera de esta lista, viene de otro campo.

    Es lo que pasaba: el target salía del `trim_slope`, cuyo neutro
    (2048) convertido da 92 nits — y 92 no es un target de nada.
    """

    def test_la_tabla_solo_produce_targets_validos(self):
        self.assertTrue(set(L8_NITS_POR_INDICE.values()) <= TARGETS_DOLBY)

    def test_el_payload_tampoco_inventa_otros(self):
        niveles = {"level1": [{"frame": 0, "min_pq": 1, "max_pq": 2500, "avg_pq": 900}],
                   "level8": [{"target_display_index": i, "trim_slope": 1234}
                              for i in L8_NITS_POR_INDICE]}
        nits = payload_de_luminancia(niveles)["references"]["l8_trim_nits_full"]
        self.assertTrue(set(nits) <= TARGETS_DOLBY, f"fuera de la tabla: {nits}")

    def test_y_un_indice_inventado_no_produce_nada(self):
        niveles = {"level1": [{"frame": 0, "min_pq": 1, "max_pq": 2500, "avg_pq": 900}],
                   "level8": [{"target_display_index": 7777, "trim_slope": 2048}]}
        self.assertEqual(
            payload_de_luminancia(niveles)["references"]["l8_trim_nits_full"], [])


class TestLosDosCaminosDelTargetL8Coinciden(unittest.TestCase):
    """`mkv_analyze` lo saca del índice y `luminance` del perfil.

    Mientras uno usaba el índice y el otro el `trim_slope`, la pantalla
    enseñaba «100 · 600 · 1001» arriba y «97 · 244» abajo del MISMO
    fichero. Dos formas de calcular lo mismo tienen que dar lo mismo.
    """

    def test_sobre_los_indices_reales_de_el_padrino(self):
        indices = [1, 48]
        por_perfil = payload_de_luminancia({
            "level1": [{"frame": 0, "min_pq": 1, "max_pq": 2500, "avg_pq": 900}],
            "level8": [{"target_display_index": i, "trim_slope": 2048}
                       for i in indices],
        })["references"]["l8_trim_nits_full"]
        por_indice = sorted({L8_NITS_POR_INDICE[i] for i in indices})
        self.assertEqual(por_perfil, por_indice)
        self.assertEqual(por_perfil, [100, 1000])


class TestUnMasterUHDNoSeGradeaEnBT709(unittest.TestCase):
    """El `source_primary_index` 0 no se puede presentar como «BT.709».

    Medido: vale 0 en el 100 % de los frames de todos los RPU del NAS, y
    en los dos MKV donde la radiografía lo enseñaba decía «BT.709»
    mientras MediaInfo leía «Display P3» del mastering display. BT.709 es
    el gamut de HD y SDR: en un máster UHD HDR no puede ser.
    """

    class _Dovi:
        l9_primaries = ""
        l11_content_type = ""
        has_l9 = False
        has_l11 = False

    def test_el_indice_cero_no_declara_primarios(self):
        dv = self._Dovi()
        rellenar_l9_l11({"level9": [{"source_primary_index": 0}]}, dv)
        self.assertEqual(dv.l9_primaries, "")

    def test_pero_el_bloque_SI_esta(self):
        """Distinguir «ausente» de «cero» sigue importando: lo que
        cambia es qué se enseña, no qué se detecta."""
        dv = self._Dovi()
        rellenar_l9_l11({"level9": [{"source_primary_index": 0}]}, dv)
        self.assertTrue(dv.has_l9)

    def test_y_un_indice_de_verdad_si_se_lee(self):
        for idx, esperado in ((9, "BT.2020"), (12, "DCI-P3 D65")):
            dv = self._Dovi()
            rellenar_l9_l11({"level9": [{"source_primary_index": idx}]}, dv)
            self.assertEqual(dv.l9_primaries, esperado)

    def test_ningun_indice_produce_BT709(self):
        """El guard de la clase entera: si alguien devuelve el mapeo,
        vuelve el «Masterizado en BT.709» de un UHD HDR."""
        self.assertNotIn("BT.709", PRIMARIES_POR_INDICE.values())

    def test_y_el_titular_cae_al_mastering_display(self):
        from phases.mkv_lectura import lectura_de
        a = {"hdr": {"hdr_format": "HDR10",
                     "mastering_display_primaries": "Display P3",
                     "mastering_display_luminance": "min: 0.0001 cd/m2, max: 1000 cd/m2"},
             "dovi": {"profile": 7, "el_type": "FEL", "l9_primaries": ""}}
        master = [f for f in lectura_de(a) if f["rotulo"] == "El máster"][0]
        self.assertIn("Display P3", master["texto"])
        self.assertNotIn("BT.709", master["texto"])


class TestNingunTextoSaleConUnHueco(unittest.TestCase):
    """Un paréntesis vacío es una interpolación que no se rellenó.

    «usa los controles exclusivos de CMv4.0 ()» — el criterio había
    cambiado y el texto seguía describiendo el anterior.
    """

    HUECOS = ("()", "( )", "[]", "{}", " ()", "«»", ": .", "— .")

    def _motivos(self):
        from phases.rpu_analyze import motivo_de_l8, tier_de_l8
        base = {"frames_with_cmv40": 1000, "scene_cuts": 100,
                "l8_unique_count": 0, "l8_neutral_pct": 0.0,
                "l8_has_mid_contrast": False, "l8_has_clip_trim": False,
                "l2_unique_count": 0, "l2_target_pqs": 0,
                "l3_unique_count": 0, "l3_frames": 0,
                "l8_max_delta": 0, "l8_frames_sig_pct": 0.0}
        salidas = []
        # Las combinaciones que de verdad se dan: con y sin cada flag,
        # con pocos y muchos combos, en las tres clasificaciones.
        for clas in ("real", "tone_mapping", "default"):
            for combos, delta in ((0, 0), (2, 328), (3, 12), (210, 606)):
                for mid in (False, True):
                    for clip in (False, True):
                        n = dict(base, l8_unique_count=combos, l8_max_delta=delta,
                                 l8_has_mid_contrast=mid, l8_has_clip_trim=clip,
                                 l3_unique_count=900 if clas == "tone_mapping" else 0)
                        salidas.append(motivo_de_l8(n, clas))
                        salidas.extend(x for x in tier_de_l8(n, clas) if x)
        return salidas

    def test_ninguno_de_los_motivos_deja_un_hueco(self):
        malos = [t for t in self._motivos()
                 for h in self.HUECOS if h in t]
        self.assertEqual(malos, [], f"{len(malos)} textos con hueco: {malos[:3]}")

    def test_y_el_barrido_mira_algo(self):
        """Con la lista de combinaciones vacía pasaría en verde."""
        self.assertGreater(len(self._motivos()), 40)


if __name__ == "__main__":
    unittest.main()
