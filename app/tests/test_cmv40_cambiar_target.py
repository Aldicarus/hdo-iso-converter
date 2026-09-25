"""«Cambiar target» deja el proyecto listo para escoger otro, no lo rehace.

El proyecto de Drive quedó parado pidiendo confirmación por divergencias. Al
pulsar «Cambiar target» procesó algo durante unos segundos y volvió al MISMO
estado, sin llegar a ofrecer el selector. Reportado el 2026-09-25, con el
`phase_history` enseñándolo: `target_rpu_drive` a las 06:22 y otra vez a las
07:32, la segunda de 5,7 s.

El reset borraba el target PROVISTO —`target_rpu_path`, el trust, el ACK— y
dejaba el target ELEGIDO (`pending_target_*`) intacto. Y el orquestador, al
ver `source_analyzed` con un pending puesto, vuelve a proveer **el mismo
bin**: los gates fallan igual y el proyecto regresa a la misma decisión. O
sea, el reset no tenía efecto y «Cambiar target» era un bucle.

Lo correcto lo dice el propio orquestador en su comentario: «Si NO hay
pending_target, pausa — Fase B requiere acción manual del usuario».

Lo que este fichero fija:

- tras el reset no queda NADA del target: ni el provisto ni el elegido;
- y por tanto el orquestador no vuelve a proveer nada — que es lo que
  convierte el reset en una operación con efecto;
- lo de aguas arriba se conserva: el análisis del MKV origen no se repite
  por cambiar de bin.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_cambiar_target -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402

# El estado real de Drive (2011) cuando el usuario pulsó el botón.
PARADO = dict(
    phase="target_provided",
    source_dv_info={"profile": 7, "el_type": "FEL", "cm_version": "v2.9"},
    source_frame_count=100_000,
    target_rpu_path="drive://1s53tSRg.../Drive.2011.UHD.bin",
    target_rpu_source="drive",
    target_type="trusted_p7_fel_final",
    target_trust_ok=False,
    target_preflight_ok=True,
    awaiting_critical_ack=True,
    critical_gate_failures=[
        {"gate": "l6_div", "severity": "ack_required", "nits_diff": 611},
        {"gate": "l1_div", "severity": "ack_required"},
    ],
    # Lo que el usuario escogió al crear el proyecto y no se limpiaba.
    pending_target_kind="repo",
    pending_target_file_id="1s53tSRg",
    pending_target_file_name="Drive.2011.UHD.bin",
    auto_pipeline=True,
)


class CambiarTargetCase(ApiTestCase):

    def _parado(self):
        return self.crear_sesion("cmv40_Drive_2011_1790316997", **PARADO)

    def _tras_cambiar(self):
        sid = self._parado()
        r = self.client.post(f"/api/cmv40/{sid}/reset-to/source_analyzed")
        self.assertEqual(r.status_code, 200, r.text)
        from storage import load_cmv40_session
        return load_cmv40_session(sid)


class TestNoQuedaNadaDelTarget(CambiarTargetCase):

    def test_ni_el_provisto_ni_su_veredicto(self):
        s = self._tras_cambiar()
        self.assertEqual(s.target_rpu_path, "")
        self.assertEqual(s.target_type, "generic")
        self.assertFalse(s.target_trust_ok)
        self.assertFalse(s.awaiting_critical_ack)
        self.assertEqual(s.critical_gate_failures, [])

    def test_ni_el_ELEGIDO(self):
        # Era lo que faltaba: con esto puesto, el orquestador vuelve a
        # proveer el mismo bin y el reset no sirve de nada.
        s = self._tras_cambiar()
        self.assertEqual(s.pending_target_kind, "")
        self.assertEqual(s.pending_target_file_id, "")
        self.assertEqual(s.pending_target_file_name, "")
        self.assertEqual(s.pending_target_rpu_path, "")
        self.assertEqual(s.pending_target_source_mkv_path, "")

    def test_la_vista_agrupadora_lo_confirma(self):
        # `session.pending_target` es lo que lee el código nuevo; si algún
        # campo se escapara de la limpieza, aquí se vería.
        pt = self._tras_cambiar().pending_target
        self.assertFalse(any([pt.kind, pt.rpu_path, pt.file_id,
                              pt.file_name, pt.source_mkv_path]), pt)


class TestElProyectoQuedaEsperandoAlUsuario(CambiarTargetCase):

    def test_rebobina_a_source_analyzed(self):
        self.assertEqual(self._tras_cambiar().phase, "source_analyzed")

    def test_y_el_orquestador_no_provee_nada(self):
        # La regla del propio orquestador: sin pending target, pausa. Es lo
        # que hace que el usuario llegue a ver el selector.
        s = self._tras_cambiar()
        self.assertFalse(s.pending_target_kind,
                         "con esto puesto, `_cmv40_dispatch_next_phase` "
                         "vuelve a lanzar la Fase B sola")


class TestLoDeAguasArribaSeConserva(CambiarTargetCase):

    def test_el_analisis_del_mkv_origen_no_se_repite(self):
        # Cambiar de bin no invalida el análisis del disco: repetirlo serían
        # ~5 min de extracción para nada.
        s = self._tras_cambiar()
        self.assertEqual(s.source_frame_count, 100_000)
        self.assertTrue(s.source_dv_info)


if __name__ == "__main__":
    unittest.main()
