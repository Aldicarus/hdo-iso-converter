"""El criterio para avanzar de la Fase D lo aplica el backend, no la UI.

`app.js` calculaba `canConfirm = delta === 0 && confOk` y deshabilitaba el
botón, mientras `POST /mark-synced` aceptaba cualquier cosa. Era la única copia
de la regla: un `app.js` viejo en caché o una llamada a mano se la saltaban sin
dejar rastro — la misma familia de bugs que se cerró moviendo el plan de
workflows al backend.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_sync_gate -v
"""
import json
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
from phases.cmv40_pipeline import (  # noqa: E402
    SYNC_CONFIDENCE_THRESHOLD, evaluate_sync_gate,
)


def _volcado(n=400, desalineado=0, source_frames=1000, target_frames=1000):
    """per_frame_data con curvas iguales (o desplazadas `desalineado`)."""
    import math
    curva = [int(500 + 400 * math.sin(i / 11.0)) for i in range(n + 200)]
    return {
        "source_frames": source_frames,
        "target_frames": target_frames,
        "data": [{"frame": i,
                  "src_maxcll": curva[i],
                  "tgt_maxcll": curva[i + desalineado]} for i in range(n)],
    }


class TestElCriterio(unittest.TestCase):
    """`evaluate_sync_gate`: función pura, las cuatro combinaciones."""

    def test_delta_cero_y_curvas_alineadas_pasa(self):
        g = evaluate_sync_gate(_volcado(), sync_delta=0)
        self.assertTrue(g["ok"], g)
        self.assertEqual(g["reason"], "")

    def test_delta_distinto_de_cero_no_pasa(self):
        g = evaluate_sync_gate(_volcado(), sync_delta=40)
        self.assertFalse(g["ok"])
        self.assertIn("diferencia de frames", g["reason"])
        self.assertIn("+40", g["reason"])

    def test_confianza_baja_no_pasa_aunque_el_delta_sea_cero(self):
        g = evaluate_sync_gate(_volcado(desalineado=17), sync_delta=0)
        self.assertFalse(g["confidence_ok"], g)
        self.assertFalse(g["ok"])
        self.assertIn("umbral", g["reason"])

    def test_el_delta_se_deduce_del_volcado_si_no_se_pasa(self):
        g = evaluate_sync_gate(_volcado(source_frames=1000, target_frames=1040))
        self.assertEqual(g["delta"], 40)
        self.assertFalse(g["ok"])

    def test_el_umbral_es_uno_solo(self):
        """El 0.85 estaba escrito a mano en `compute_sync_confidence` y como
        '85%' en el texto de la UI. Ahora sale de la constante."""
        self.assertEqual(evaluate_sync_gate(_volcado(), 0)["threshold_pct"],
                         int(SYNC_CONFIDENCE_THRESHOLD * 100))


class TestMarkSyncedLoAplica(ApiTestCase):

    def _con_volcado(self, sid, **kw):
        wd = Path(self.leer_sesion(sid).artifacts_dir)
        (wd / "per_frame_data.json").write_text(json.dumps(_volcado(**kw)))

    def test_confirmar_con_delta_pendiente_devuelve_409(self):
        """EL BUG: esto avanzaba la fase igualmente."""
        sid = self.crear_sesion(phase="extracted", target_frame_count=1040,
                                sync_delta=40, target_trust_ok=False,
                                target_type="generic")
        self._con_volcado(sid)
        r = self.client.post(f"/api/cmv40/{sid}/mark-synced")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("diferencia de frames", r.json()["detail"])
        self.assertEqual(self.leer_sesion(sid).phase, "extracted",
                         "la fase no debe avanzar si el criterio no se cumple")

    def test_confirmar_con_confianza_baja_devuelve_409(self):
        sid = self.crear_sesion(phase="extracted", sync_delta=0,
                                target_trust_ok=False, target_type="generic")
        self._con_volcado(sid, desalineado=17)
        r = self.client.post(f"/api/cmv40/{sid}/mark-synced")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("umbral", r.json()["detail"])

    def test_confirmar_cumpliendo_el_criterio_avanza(self):
        sid = self.crear_sesion(phase="extracted", sync_delta=0,
                                target_trust_ok=False, target_type="generic")
        self._con_volcado(sid)
        r = self.client.post(f"/api/cmv40/{sid}/mark-synced")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.leer_sesion(sid).phase, "sync_verified")

    def test_force_es_la_salida_explicita(self):
        """El caso legítimo (grading que diverge de verdad, validado a ojo)
        antes no tenía salida: el botón estaba deshabilitado y punto."""
        sid = self.crear_sesion(phase="extracted", sync_delta=40,
                                target_trust_ok=False, target_type="generic")
        self._con_volcado(sid)
        r = self.client.post(f"/api/cmv40/{sid}/mark-synced?force=true")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.leer_sesion(sid).phase, "sync_verified")

    def test_no_se_aplica_cuando_la_fase_d_se_omite(self):
        """Con target trusted la Fase D no se muestra: nadie ha mirado el
        gráfico porque no había que mirarlo, y el volcado puede no existir."""
        sid = self.crear_sesion(phase="extracted", sync_delta=40,
                                target_trust_ok=True, trust_override="auto",
                                target_type="trusted_p7_fel_final")
        r = self.client.post(f"/api/cmv40/{sid}/mark-synced")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.leer_sesion(sid).phase, "sync_verified")

    def test_sin_volcado_no_bloquea(self):
        """Sin `per_frame_data.json` no hay criterio que aplicar; bloquear por
        un fichero ausente sería peor que dejar pasar (Fase F valida el frame
        count de todas formas)."""
        sid = self.crear_sesion(phase="extracted", sync_delta=0,
                                target_trust_ok=False, target_type="generic")
        r = self.client.post(f"/api/cmv40/{sid}/mark-synced")
        self.assertEqual(r.status_code, 200, r.text)


class TestSyncDataExponeElGate(ApiTestCase):

    def test_sync_data_devuelve_sync_gate(self):
        """La UI lo LEE de aquí en vez de re-derivarlo."""
        sid = self.crear_sesion(phase="extracted", sync_delta=0,
                                target_trust_ok=False, target_type="generic")
        wd = Path(self.leer_sesion(sid).artifacts_dir)
        (wd / "per_frame_data.json").write_text(json.dumps(_volcado()))
        r = self.client.get(f"/api/cmv40/{sid}/sync-data")
        self.assertEqual(r.status_code, 200, r.text)
        gate = r.json().get("sync_gate")
        self.assertIsNotNone(gate, "sync-data debe traer sync_gate")
        self.assertTrue(gate["ok"], gate)
        self.assertEqual(gate["threshold_pct"], 85)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
