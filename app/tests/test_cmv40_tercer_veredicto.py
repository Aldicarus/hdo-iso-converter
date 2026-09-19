"""El tercer veredicto, de punta a punta: se explica, se detiene y se decide.

`tone_mapping` —el bin no trae trims de colorista pero sí L3/L9/L11 reales
del análisis de Dolby— se añadió el 2026-09-19 al clasificador y al badge de
la card, y **no se cableó en ninguna de las puertas por las que pasa un
job**. Lo que el usuario vio en un job real (Pulp Fiction, maxΔ 41 con 485
combos L3):

  * el pre-flight no emitía veredicto: la única huella del tercer estado era
    su identificador en MAYÚSCULAS al final de la línea de combos, porque las
    tres ramas del veredicto eran `real`, `indeterminate` y `default`;
  * el pipeline encadenaba sin preguntar, así que el único veredicto que
    existe *para que decida el usuario* era el único que nunca se le ofrecía;
  * y `GET /api/cmv40/{id}` servía `default` porque el rearmado del análisis
    no copiaba los dos campos L3 — o sea que el listado decía `tone_mapping`
    y el panel `default` para el mismo proyecto, a la vez.

Cada test de aquí ejecuta el código de verdad: la helper del pre-flight con
su clasificador, y los endpoints por HTTP. Un `assertIn` sobre el fuente
habría pasado en verde con el comportamiento invertido.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_tercer_veredicto -v
"""
import asyncio
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
from models import L8Combo  # noqa: E402
from phases.rpu_analyze import RpuAnalysis, _l8_delta_maxima  # noqa: E402


def _combos(*specs) -> list:
    return [L8Combo(target_display_index=1,
                    trim_slope=c.get("slope", 2048), trim_offset=c.get("off", 2048),
                    trim_power=c.get("pow", 2048), trim_chroma_weight=2048,
                    trim_saturation_gain=c.get("sat", 2048), ms_weight=0,
                    target_mid_contrast=c.get("mid"), clip_trim=c.get("clip"),
                    occurrence_count=c.get("n", 1000))
            for c in specs]


def bin_pulp_fiction() -> RpuAnalysis:
    """`Pulp.Fiction.1994.BD Retail P7 FEL (cmv4.0 restored).bin`, medido.

    Dos combos con el mismo trim (slope 2039, offset 2089, power 2034) →
    maxΔ 41, bajo el umbral de 50; y 485 combos L3 en 182.078 frames. El bin
    del job del 2026-09-19 que destapó todo esto.
    """
    a = RpuAnalysis()
    a.total_frames = a.frames_with_cmv40 = 222274
    a.scene_cuts = 1177
    a.l8_combos = _combos(
        {"slope": 2039, "off": 2089, "pow": 2034, "n": 170179},
        {"slope": 2039, "off": 2089, "pow": 2034, "mid": 2048, "clip": 2048,
         "n": 51689})
    a.l8_unique_count = 2
    a.l8_max_delta = _l8_delta_maxima(a.l8_combos)
    a.l8_target_indices = [1]
    a.l3_unique_count, a.l3_frames = 485, 182078
    a.l2_unique_count = 1605
    a.l2_target_pqs = [2081, 2851, 3079]
    return a


def bin_con_colorista() -> RpuAnalysis:
    """`Evil.Dead.Burn.2026…bin`: dos combos, pero `power −184` y `sat −328`."""
    a = RpuAnalysis()
    a.total_frames = a.frames_with_cmv40 = 159030
    a.scene_cuts = 2585
    a.l8_combos = _combos({"n": 88},
                          {"pow": 1864, "sat": 1720, "n": 158942})
    a.l8_unique_count = 2
    a.l8_max_delta = _l8_delta_maxima(a.l8_combos)
    a.l3_unique_count, a.l3_frames = 1, 88
    a.l2_unique_count = 300
    return a


class _PreflightBase(ApiTestCase):
    """Ejecuta la helper real del pre-flight con el análisis del bin dado."""

    def correr_preflight(self, analisis, **campos):
        import storage
        from phases import rpu_analyze

        sid = self.crear_sesion(sid="cmv40_tv", phase="created", **campos)
        s = storage.load_cmv40_session(sid)

        async def _falso(_path):
            return analisis
        orig = rpu_analyze.analyze_rpu_combos
        rpu_analyze.analyze_rpu_combos = _falso
        self.addCleanup(setattr, rpu_analyze, "analyze_rpu_combos", orig)

        lineas = []
        async def _log(t):
            lineas.append(t)

        avanzar = asyncio.run(
            self.cmv40._cmv40_preflight_analyze_target(s, _log))
        return s, avanzar, lineas


class TestElPreFlightSeDetieneYLoExplica(_PreflightBase):

    def test_el_bin_de_tone_mapping_para_el_pipeline(self):
        """Lo pidió el usuario el 2026-09-19: parar y preguntar, como
        «default». Antes encadenaba Fase A sin ofrecer la decisión."""
        s, avanzar, _ = self.correr_preflight(bin_pulp_fiction())

        self.assertIs(avanzar, False, "el pipeline no debe encadenar solo")
        self.assertEqual(s.target_l8_classification, "tone_mapping")
        self.assertEqual(s.preflight_decision, "ask_tone_mapping")
        self.assertIs(s.target_preflight_ok, False)

    def test_el_veredicto_se_escribe_con_el_numero_que_decide(self):
        """Sin la rama, el tercer estado no emitía NI UNA línea: lo único que
        el usuario leía era `clasificación: TONE_MAPPING` al final de la línea
        de combos. Y el número que decide desde la recalibración es el maxΔ,
        no el conteo: dos combos pueden ser retail (Δ 606) o generados (Δ 0)."""
        _, _, lineas = self.correr_preflight(bin_pulp_fiction())
        veredicto = [l for l in lineas if "🟡" in l]

        self.assertTrue(veredicto, "el tercer veredicto no emite veredicto")
        self.assertIn("41", veredicto[0], "falta la desviación medida")
        self.assertIn("50", veredicto[0], "falta el umbral contra el que se mide")

    def test_el_cierre_dice_que_queda_esperando_una_respuesta(self):
        _, _, lineas = self.correr_preflight(bin_pulp_fiction())
        self.assertTrue(any("⏸" in l for l in lineas),
                        "el pre-flight no dice que se ha detenido a preguntar")
        # Y NO el cierre del bin sintético, que dice otra cosa: que el bin no
        # tiene un L8 trabajado y que la recomendación es no procesar.
        self.assertFalse(any("🛑" in l for l in lineas))

    def test_la_recomendacion_persistida_ofrece_las_dos_salidas(self):
        """`accept-keep` y `override-recommendation` exigen los dos
        `recommended_action == 'keep'`. Sin esto los botones contestan 400."""
        s, _, _ = self.correr_preflight(bin_pulp_fiction())
        self.assertEqual(s.recommended_action, "keep")
        self.assertTrue(s.recommended_action_label)
        self.assertIn("485", s.recommended_action_reason,
                      "el motivo no cuenta lo que el bin SÍ aporta")

    def test_un_bin_con_colorista_sigue_pasando_de_largo(self):
        """La contraprueba: el cambio no puede parar lo que antes pasaba."""
        s, avanzar, lineas = self.correr_preflight(bin_con_colorista())
        self.assertIs(avanzar, True)
        self.assertEqual(s.target_l8_classification, "real")
        self.assertEqual(s.preflight_decision, "")
        self.assertTrue(any("🟢" in l for l in lineas))


class TestElPanelSirveElMismoVeredictoQueElListado(_PreflightBase):
    """El bug mudo: dos lecturas del mismo proyecto, dos veredictos."""

    def test_el_get_re_deriva_con_los_campos_l3(self):
        s, _, _ = self.correr_preflight(bin_pulp_fiction())
        import storage
        storage.save_cmv40_session(s)

        r = self.client.get(f"/api/cmv40/{s.id}?include_log=false")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["target_l8_classification"], "tone_mapping",
                         "el panel sirve un veredicto distinto del medido")
        self.assertEqual(d["target_l8_max_delta"], 41)

    def test_lo_persistido_y_lo_re_derivado_coinciden(self):
        """El listado sirve lo PERSISTIDO y el panel lo re-deriva, así que un
        pre-flight que guarde un veredicto distinto del que el criterio
        calcula deja las dos vistas peleadas. (Un proyecto analizado con un
        criterio ANTERIOR sí puede diferir hasta que se abra: eso es
        deliberado — «se corrigen solos al abrirlos» — y no es este caso.)"""
        s, _, _ = self.correr_preflight(bin_pulp_fiction())
        import storage
        storage.save_cmv40_session(s)

        panel = self.client.get(f"/api/cmv40/{s.id}?include_log=false").json()
        listado = self.client.get("/api/cmv40").json()["sessions"]
        fila = next(x for x in listado if x.get("id") == s.id)
        self.assertEqual(fila["target_l8_classification"],
                         panel["target_l8_classification"])


class TestLasDosSalidasFuncionanDesdeEseEstado(_PreflightBase):
    """De nada sirve enseñar la decisión si los botones contestan 400."""

    def _preparar(self):
        s, _, _ = self.correr_preflight(bin_pulp_fiction())
        import storage
        storage.save_cmv40_session(s)
        return s.id

    def test_mantener_cierra_el_proyecto(self):
        sid = self._preparar()
        r = self.client.post(f"/api/cmv40/{sid}/accept-keep")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["phase"], "done")
        self.assertEqual(d["output_workflow"], "keep_cmv29")
        self.assertEqual(d["preflight_user_choice"], "keep")

    def test_inyectar_igualmente_desbloquea_el_pipeline(self):
        sid = self._preparar()
        r = self.client.post(f"/api/cmv40/{sid}/override-recommendation")
        self.assertEqual(r.status_code, 200)
        d = self.client.get(f"/api/cmv40/{sid}?include_log=false").json()
        self.assertEqual(d["preflight_user_choice"], "inject")
        self.assertNotEqual(d["preflight_decision"], "ask_tone_mapping")

    def test_tras_forzar_la_recomendacion_vuelve_a_ser_la_ruta(self):
        """Si siguiera diciendo «keep», la card volvería a ofrecer una
        decisión ya tomada y el re-derivado del GET la resucitaría."""
        sid = self._preparar()
        self.client.post(f"/api/cmv40/{sid}/override-recommendation")
        d = self.client.get(f"/api/cmv40/{sid}?include_log=false").json()
        self.assertNotEqual(d.get("recommended_action"), "keep")


if __name__ == "__main__":
    unittest.main()
