"""El chart de la Fase D no se trae la película entera.

`per_frame_data.json` de un UHD son **24,1 MB** para 243.552 frames y el
endpoint los devolvía todos. Medido: 97 ms de `json.loads` + 79 ms de Pearson +
92 ms de re-serializar (~300 ms de event loop en un Mac, ~1,2 s en el NAS) y
24 MB por el cable — para pintar un canvas de ~1500 px, o sea 160 puntos por
píxel. Y el frontend lo volvía a pedir entero al recargar el proyecto.

Ahora el backend cachea las series (como `array('i')`, ~8 MB en vez de los
~250 MB que ocuparían los dicts), calcula las métricas UNA vez y sirve solo la
ventana pedida reducida a cubos.

Dos decisiones que el test fija porque son las que pueden romper el propósito
de la pantalla:

  · por cubo se emiten el MÍNIMO y el MÁXIMO, no la media — un promedio se
    come los picos que delatan un corte de escena desplazado;
  · las métricas (offset sugerido, confianza, criterio de avance) se calculan
    sobre la serie COMPLETA, no sobre la ventana: un desfase fuera del rango
    visible sigue siendo real.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_sync_ventana -v
"""
import json
import math
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402

FRAMES = 6000


def _volcado(n=FRAMES, pico_en=None):
    """Curva con forma, y opcionalmente un pico aislado en un frame concreto."""
    filas = []
    for i in range(n):
        v = int(500 + 400 * math.sin(i / 23.0))
        if pico_en is not None and i == pico_en:
            v = 4000
        filas.append({"frame": i, "src_maxcll": v, "src_maxfall": v // 3,
                      "tgt_maxcll": v, "tgt_maxfall": v // 3})
    return {"source_frames": n, "target_frames": n, "data": filas}


class VentanaTestCase(ApiTestCase):

    def _sesion(self, volcado=None, **campos):
        sid = self.crear_sesion(phase="extracted", sync_delta=0,
                               target_trust_ok=False, target_type="generic",
                               **campos)
        wd = Path(self.leer_sesion(sid).artifacts_dir)
        (wd / "per_frame_data.json").write_text(json.dumps(volcado or _volcado()))
        return sid

    def setUp(self):
        super().setUp()
        # La caché de series es de módulo y sobrevive entre tests.
        self.cmv40._PFD_CACHE.clear()
        self.addCleanup(self.cmv40._PFD_CACHE.clear)


class TestReduccionACubos(VentanaTestCase):

    def test_sin_rango_devuelve_la_pelicula_reducida(self):
        sid = self._sesion()
        r = self.client.get(f"/api/cmv40/{sid}/sync-data")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["downsampled"])
        self.assertLessEqual(len(d["data"]), self.cmv40.PFD_CUBOS_DEFECTO)
        self.assertEqual(d["total_frames"], FRAMES)
        self.assertEqual(d["range"], {"from": 0, "to": FRAMES})

    def test_un_zoom_fino_recibe_el_dato_exacto(self):
        """El preset de 30 s son ~720 frames: cabe en los cubos, así que no se
        reduce nada y el usuario ve el dato real."""
        sid = self._sesion()
        r = self.client.get(f"/api/cmv40/{sid}/sync-data?desde=0&hasta=720")
        d = r.json()
        self.assertFalse(d["downsampled"], "un zoom fino no debe reducirse")
        self.assertEqual(len(d["data"]), 720)
        self.assertEqual(d["data"][0]["frame"], 0)
        self.assertEqual(d["data"][-1]["frame"], 719)

    def test_la_ventana_respeta_el_rango_pedido(self):
        sid = self._sesion()
        r = self.client.get(f"/api/cmv40/{sid}/sync-data?desde=1000&hasta=1500")
        d = r.json()
        frames = [p["frame"] for p in d["data"]]
        self.assertGreaterEqual(min(frames), 1000)
        self.assertLess(max(frames), 1500)

    def test_cada_cubo_trae_minimo_y_maximo(self):
        sid = self._sesion()
        d = self.client.get(f"/api/cmv40/{sid}/sync-data?cubos=100").json()
        self.assertTrue(d["downsampled"])
        for punto in d["data"]:
            for clave in ("src_maxcll", "tgt_maxcll"):
                self.assertIn(clave + "_min", punto, punto)
                self.assertLessEqual(punto[clave + "_min"], punto[clave], punto)

    def test_un_pico_aislado_sobrevive_a_la_reduccion(self):
        """EL PUNTO de usar max por cubo en vez de la media: un flash de un
        solo frame es exactamente lo que delata un corte de escena desplazado,
        y un promedio sobre 60 frames lo borra."""
        sid = self._sesion(_volcado(pico_en=3000))
        d = self.client.get(f"/api/cmv40/{sid}/sync-data?cubos=100").json()
        picos = [p["src_maxcll"] for p in d["data"]]
        self.assertEqual(max(picos), 4000,
                         "la reducción se ha comido el pico de un frame")

    def test_los_cubos_se_acotan(self):
        sid = self._sesion()
        d = self.client.get(f"/api/cmv40/{sid}/sync-data?cubos=999999").json()
        self.assertLessEqual(len(d["data"]), FRAMES)
        d2 = self.client.get(f"/api/cmv40/{sid}/sync-data?cubos=1").json()
        self.assertGreaterEqual(len(d2["data"]), 50, "suelo de 50 cubos")


class TestMetricasSobreLaSerieCompleta(VentanaTestCase):

    def test_el_offset_y_la_confianza_no_dependen_de_la_ventana(self):
        sid = self._sesion()
        entera = self.client.get(f"/api/cmv40/{sid}/sync-data").json()
        trozo = self.client.get(
            f"/api/cmv40/{sid}/sync-data?desde=4000&hasta=4100").json()
        self.assertEqual(trozo["suggested_offset"], entera["suggested_offset"])
        self.assertEqual(trozo["confidence"], entera["confidence"])
        self.assertEqual(trozo["sync_gate"], entera["sync_gate"])

    def test_sigue_trayendo_el_criterio_de_avance(self):
        sid = self._sesion()
        gate = self.client.get(f"/api/cmv40/{sid}/sync-data").json()["sync_gate"]
        self.assertTrue(gate["ok"], gate)
        self.assertEqual(gate["threshold_pct"], 85)


class TestCache(VentanaTestCase):

    def test_el_volcado_se_lee_una_sola_vez(self):
        """Un cambio de zoom no debe volver a parsear 24 MB."""
        sid = self._sesion()
        lecturas = []
        original = self.cmv40._cmv40_pfd_cargar

        def _espia(session_id, pf, delta):
            r = original(session_id, pf, delta)
            lecturas.append(r["mtime_ns"])
            return r

        self.cmv40._cmv40_pfd_cargar = _espia
        self.addCleanup(setattr, self.cmv40, "_cmv40_pfd_cargar", original)
        for rango in ("", "?desde=0&hasta=720", "?desde=720&hasta=1440"):
            self.client.get(f"/api/cmv40/{sid}/sync-data{rango}")
        self.assertEqual(len(lecturas), 3, "el helper se llama en cada petición")
        # Pero solo la primera parsea: las otras salen de la caché, que se
        # detecta porque el objeto devuelto es EL MISMO.
        self.assertEqual(len(self.cmv40._PFD_CACHE), 1)

    def test_si_el_volcado_cambia_la_cache_se_invalida(self):
        """La Fase E regenera `per_frame_data.json` tras cada corrección."""
        sid = self._sesion()
        primera = self.client.get(f"/api/cmv40/{sid}/sync-data").json()
        wd = Path(self.leer_sesion(sid).artifacts_dir)
        (wd / "per_frame_data.json").write_text(json.dumps(_volcado(n=3000)))
        segunda = self.client.get(f"/api/cmv40/{sid}/sync-data").json()
        self.assertEqual(primera["total_frames"], FRAMES)
        self.assertEqual(segunda["total_frames"], 3000,
                         "la caché no se ha invalidado al cambiar el fichero")


if __name__ == "__main__":
    unittest.main()
