"""Tres arreglos que salieron del mismo job: Drive (2011), 2026-09-25.

El proyecto quedó pidiendo confirmación por divergencias, el usuario la
aceptó, y con eso se saltó la Fase D — que es justo la que corrige el desfase
de −426 frames que ese mismo análisis había detectado. De ahí salieron tres
cosas distintas:

1. **Aceptar la degradación no es aceptar el desfase.** Los gates
   `ack_required` piden aceptar algo que la Fase D NO puede arreglar (el
   grading diverge); un gate `sync_review` dice literalmente lo contrario.
   Aceptar lo primero se llevaba lo segundo por delante.

2. **Un L6 ausente no es «pico 0 nits».** El gate hacía `or 0` y comparaba la
   ausencia contra el valor del otro lado, produciendo una divergencia igual
   a ese valor. Medido: 9 de 40 proyectos tienen el source sin L6, y en los 2
   donde el target sí lo trae el gate disparaba. La prueba limpia es El Conde
   de Montecristo — L1 IDÉNTICO en los dos (1000,6), o sea el mismo grading,
   y el L6 marcando 1000 nits de «divergencia».

3. **El L5 se comparaba posición contra posición.** Con Δ −426 el frame `f`
   de cada lado es un fotograma distinto de la película a partir del corte,
   así que la comparación deja de medir lo que dice medir. Ahora se prueban
   los dos anclajes que este pipeline ya documenta —lo que falta está al
   principio o al final— y gana el que más cuerpo hace coincidir.

Los tres son funciones puras o casi, así que se prueban con los valores
reales del proyecto y sin disco.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_sync_y_l6 -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from phases.cmv40_strategy import (WorkflowInputs,  # noqa: E402
                                   hay_sync_por_revisar)
from phases.cmv40_pipeline import (_analizar_l5, _comparar_l5,  # noqa: E402
                                   L5_NEUTRO)

# Los gates tal como quedaron en Drive (2011).
GATES_DRIVE = {
    "frames": {"ok": False, "bd": 144683, "target": 144257,
               "critical": True, "severity": "sync_review"},
    "cm_version": {"ok": True, "critical": True, "severity": "ok"},
    "has_l8": {"ok": True, "critical": True, "severity": "ok"},
    "l5_div": {"ok": True, "px_max": 2, "critical": True, "severity": "ok"},
    "l6_div": {"ok": False, "nits_diff": 611, "critical": False,
               "severity": "ack_required"},
    "l1_div": {"ok": False, "pct_diff": 50.73, "critical": False,
               "severity": "ack_required"},
}


class TestAceptarLaDegradacionNoAceptaElDesfase(unittest.TestCase):

    def test_con_un_gate_sync_review_no_se_salta_la_fase_d(self):
        i = WorkflowInputs(user_acknowledged=True,
                           sync_pendiente=hay_sync_por_revisar(GATES_DRIVE))
        self.assertFalse(i.skip_sync_review)

    def test_sin_el_desfase_el_ack_sigue_saltandola(self):
        # Lo que el ACK sí autoriza: una divergencia que la Fase D no arregla.
        gates = {k: v for k, v in GATES_DRIVE.items() if k != "frames"}
        i = WorkflowInputs(user_acknowledged=True,
                           sync_pendiente=hay_sync_por_revisar(gates))
        self.assertTrue(i.skip_sync_review)

    def test_ni_con_los_gates_en_verde_se_salta_si_queda_sync(self):
        # `trust_ok` y un sync pendiente no deberían convivir, pero la regla
        # no depende de que eso se cumpla.
        i = WorkflowInputs(target_trust_ok=True, sync_pendiente=True)
        self.assertFalse(i.skip_sync_review)

    def test_force_interactive_sigue_mandando(self):
        i = WorkflowInputs(target_trust_ok=True, user_acknowledged=True,
                           trust_override="force_interactive")
        self.assertFalse(i.skip_sync_review)


class TestQueCuentaComoSyncPorRevisar(unittest.TestCase):

    def test_solo_los_gates_sync_review_sin_pasar(self):
        self.assertTrue(hay_sync_por_revisar(GATES_DRIVE))

    def test_un_sync_review_que_pasa_no_cuenta(self):
        self.assertFalse(hay_sync_por_revisar(
            {"frames": {"ok": True, "severity": "sync_review"}}))

    def test_un_ack_required_no_es_un_sync_pendiente(self):
        # Es la distinción entera: uno lo arregla la Fase D y el otro no.
        self.assertFalse(hay_sync_por_revisar(
            {"l6_div": {"ok": False, "severity": "ack_required"}}))

    def test_sin_gates_no_hay_nada_pendiente(self):
        for vacio in (None, {}, "", []):
            with self.subTest(repr(vacio)):
                self.assertFalse(hay_sync_por_revisar(vacio))


class TestElL6AusenteNoEsCero(unittest.TestCase):
    """Sobre `_evaluate_trust_gates`, con los dos RPU en la mano."""

    def _gates(self, src_l6, tgt_l6, src_l1=1000.0, tgt_l1=1000.0):
        from models import DoviInfo
        from phases.cmv40_pipeline import _evaluate_trust_gates
        comun = dict(profile=7, el_type="FEL", rpu_present=True)
        src = DoviInfo(cm_version="v2.9", l6_max_cll=src_l6,
                       l1_max_cll=src_l1, **comun)
        tgt = DoviInfo(cm_version="v4.0", has_l8=True, l6_max_cll=tgt_l6,
                       l1_max_cll=tgt_l1, **comun)
        gates, _ = _evaluate_trust_gates(src, tgt, 1000, 1000)
        return gates

    def test_el_caso_del_conde_de_montecristo(self):
        # Mismo grading (L1 idéntico) y el gate pedía aceptar 1000 nits.
        g = self._gates(0, 1000, 1000.6, 1000.6)["l6_div"]
        self.assertNotEqual(g["severity"], "ack_required")
        self.assertTrue(g["ok"])
        self.assertTrue(g.get("incomparable"))

    def test_lo_dice_en_vez_de_callarlo(self):
        # Que falte no es inocuo: es que no se puede contrastar.
        g = self._gates(0, 611)["l6_div"]
        self.assertEqual(g["severity"], "warn")
        self.assertTrue(g["why"])

    def test_si_falta_en_el_bin_tambien(self):
        g = self._gates(1000, 0)["l6_div"]
        self.assertTrue(g.get("incomparable"))
        self.assertEqual(g["severity"], "warn")

    def test_si_falta_en_los_dos_no_hay_divergencia(self):
        g = self._gates(0, 0)["l6_div"]
        self.assertEqual(g["severity"], "ok")
        self.assertFalse(g.get("incomparable"))

    def test_y_una_divergencia_DE_VERDAD_sigue_pidiendo_confirmacion(self):
        # El gate no se ha desactivado: con los dos declarados, manda.
        g = self._gates(1000, 4000)["l6_div"]
        self.assertEqual(g["severity"], "ack_required")
        self.assertEqual(g["nits_diff"], 3000)


class TestElL5SeAlineaAntesDeComparar(unittest.TestCase):
    """Dos RPU con el mismo contenido y 426 frames de desfase al principio."""

    N = 2000
    D = -426          # el target tiene 426 frames menos

    def _series(self):
        # Una active area que cambia a mitad de película: si se comparan
        # desalineados, el tramo distinto sale como divergencia.
        src = {f: ((0, 0, 0, 0) if f < self.N // 2 else (140, 140, 0, 0))
               for f in range(self.N)}
        # El mismo contenido, sin los primeros 426 frames.
        tgt = {f + self.D: v for f, v in src.items() if f + self.D >= 0}
        return src, tgt

    def test_sin_alinear_la_comparacion_diverge(self):
        src, tgt = self._series()
        crudo = _comparar_l5(src, tgt, self.N, 24.0, 0)
        self.assertLess(crudo["body_coverage"], 0.95,
                        "el fixture no reproduce el desfase")

    def test_alineado_coincide(self):
        src, tgt = self._series()
        bien = _comparar_l5(src, tgt, self.N, 24.0, self.D)
        self.assertGreater(bien["body_coverage"], 0.99)

    def test_y_el_analisis_escoge_el_bueno(self):
        src, tgt = self._series()
        _, _, cmp = _analizar_l5(src, tgt, self.N, self.N + self.D,
                                 self.N, 24.0)
        self.assertEqual(cmp["desplazamiento"], self.D)
        self.assertGreater(cmp["body_coverage"], 0.99)

    def test_si_lo_que_falta_esta_al_FINAL_el_anclaje_es_el_cero(self):
        """El otro caso que este pipeline documenta: créditos recortados.

        Sin probar los DOS anclajes, alinear por el desfase desplazaría una
        película que ya estaba alineada.
        """
        src = {f: ((0, 0, 0, 0) if f < self.N // 2 else (140, 140, 0, 0))
               for f in range(self.N)}
        tgt = {f: v for f, v in src.items() if f < self.N + self.D}
        _, _, cmp = _analizar_l5(src, tgt, self.N, self.N + self.D,
                                 self.N, 24.0)
        self.assertEqual(cmp["desplazamiento"], 0)
        # La cobertura NO es perfecta, y tiene que no serlo: los 426 frames
        # que al target le faltan no coinciden con nada. Lo que se afirma es
        # que gana el anclaje correcto —medido, 0,82 contra 0,76—, no que el
        # desfase desaparezca.
        peor = _comparar_l5(src, tgt, self.N, 24.0, self.D)
        self.assertGreater(cmp["body_coverage"], peor["body_coverage"])

    def test_sin_desfase_no_se_desplaza_nada(self):
        src, tgt = self._series()
        _, _, cmp = _analizar_l5(src, tgt, self.N, self.N, self.N, 24.0)
        self.assertEqual(cmp["desplazamiento"], 0)

    def test_si_ningun_anclaje_alinea_la_cobertura_sigue_baja(self):
        # Dos másters de verdad distintos: el gate tiene que seguir
        # pidiendo confirmación, no aprobar por haber probado dos veces.
        src = {f: (0, 0, 0, 0) for f in range(self.N)}
        tgt = {f: (200, 200, 0, 0) for f in range(self.N + self.D)}
        _, _, cmp = _analizar_l5(src, tgt, self.N, self.N + self.D,
                                 self.N, 24.0)
        self.assertLess(cmp["body_coverage"], 0.5)


if __name__ == "__main__":
    unittest.main()
