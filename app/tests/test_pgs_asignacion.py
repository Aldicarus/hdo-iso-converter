"""El emparejamiento de los conteos PGS con las pistas de subtítulo.

Era un `zip(subtitle_tracks, sorted_counts)` copiado en los tres puntos de
entrada del análisis, y `zip` trunca en silencio por la lista corta. La lista
de conteos puede venir corta de verdad: la inicialización a 0 de todos los
PIDs solo ocurre cuando el MPLS aporta la lista, y `run_full_analysis_for_m2ts`
va SIEMPRE sin MPLS. Un PGS que el muestreo no vio no sale con cero — no sale —
y desplaza a todos los que van detrás.

Lo que se prueba aquí es que ese desplazamiento ya no puede ocurrir, y el
precio: cuando no se puede garantizar el emparejamiento no se asigna nada y
phase_b cae al patrón estructural, que es lo que ya hace cuando el conteo falla
entero.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import RawSubtitleTrack  # noqa: E402
from phases.phase_a import asignar_packet_counts  # noqa: E402


def subs(*idiomas, forzados=()):
    """Pistas tal y como las construye `_build_subtitle_tracks`: idioma en
    inglés y bitrate sintético (1.0 = posición de forzado en el disco)."""
    return [
        RawSubtitleTrack(language=idio, bitrate_kbps=1.0 if i in forzados else 30.0,
                         description="")
        for i, idio in enumerate(idiomas)
    ]


def mpls(*pares):
    return [{"pid": pid, "language": code} for pid, code in pares]


class TestCasoNormal(unittest.TestCase):
    def test_longitudes_iguales_asigna_en_orden_ascendente(self):
        pistas = subs("English", "English", "Spanish", "Spanish")
        ok, motivo = asignar_packet_counts(
            pistas, {0x1200: 12000, 0x1201: 300, 0x1202: 5000, 0x1203: 200})
        self.assertTrue(ok, motivo)
        self.assertEqual([p.packet_count for p in pistas], [12000, 300, 5000, 200])
        self.assertIn("ascendente", motivo)

    def test_los_ceros_del_mpls_se_asignan_como_datos(self):
        """Con lista del MPLS todos los PIDs vienen inicializados a 0, así que
        un forzado que el muestreo perdió llega como 0 y NO desplaza. phase_b
        lo trata con el patrón estructural (caso Obsession 2025)."""
        pistas = subs("Spanish", "Spanish")
        ok, _ = asignar_packet_counts(
            pistas, {0x12A0: 4516, 0x12A3: 0},
            mpls(*[(0x12A0, "spa"), (0x12A3, "spa")]))
        self.assertTrue(ok)
        self.assertEqual([p.packet_count for p in pistas], [4516, 0])


class TestElDesplazamiento(unittest.TestCase):
    """El bug. Sin MPLS, un PGS que el muestreo no vio desaparece de la lista."""

    def test_falta_un_pid_no_asigna_nada(self):
        pistas = subs("English", "English", "Spanish", "Spanish")
        # el forzado inglés (0x1201) no acumuló un solo paquete en la muestra
        ok, motivo = asignar_packet_counts(
            pistas, {0x1200: 12000, 0x1202: 5000, 0x1203: 200})
        self.assertFalse(ok)
        self.assertEqual([p.packet_count for p in pistas], [0, 0, 0, 0],
                         "no debe asignar nada: cualquier reparto estaría desplazado")
        self.assertIn("3 conteos para 4 pistas", motivo)

    def test_el_desplazamiento_habria_invertido_la_clasificacion(self):
        """Documenta el daño concreto: con el zip() antiguo, el forzado inglés
        recibía el conteo del completo español (→ 'completo') y el completo
        español el del forzado (→ 'forzado')."""
        pistas = subs("English", "English", "Spanish", "Spanish")
        cuentas = {0x1200: 12000, 0x1202: 5000, 0x1203: 200}
        # lo que hacía el zip() de antes:
        desplazado = list(zip(pistas, [cuentas[k] for k in sorted(cuentas)]))
        self.assertEqual([c for _, c in desplazado], [12000, 5000, 200])
        self.assertEqual(len(desplazado), 3, "y la cuarta pista se quedaba fuera")
        # lo que hace ahora:
        ok, _ = asignar_packet_counts(pistas, cuentas)
        self.assertFalse(ok)

    def test_sobran_conteos_tampoco_asigna(self):
        """El TS parser puede contar un PID del rango de reserva que mkvmerge
        no lista como pista."""
        pistas = subs("English", "Spanish")
        ok, motivo = asignar_packet_counts(
            pistas, {0x1200: 9000, 0x1201: 400, 0x12FF: 7})
        self.assertFalse(ok)
        self.assertIn("3 conteos para 2 pistas", motivo)


class TestOrdenDelMpls(unittest.TestCase):
    """El log del parser ya reconocía que el orden del MPLS puede no ser el
    ascendente (`order = pid_list if has_pid_list else sorted(...)`), pero la
    asignación usaba `sorted()` igualmente."""

    def test_si_el_ascendente_no_cuadra_por_idioma_manda_el_mpls(self):
        pistas = subs("Spanish", "English")          # mkvmerge: es, en
        cuentas = {0x1200: 12000, 0x1201: 5000}      # ascendente: 0x1200, 0x1201
        # …pero el MPLS dice que 0x1201 es el español y va primero
        ok, motivo = asignar_packet_counts(
            pistas, cuentas, mpls(*[(0x1201, "spa"), (0x1200, "eng")]))
        self.assertTrue(ok, motivo)
        self.assertEqual([p.packet_count for p in pistas], [5000, 12000])
        self.assertIn("MPLS", motivo)

    def test_si_el_ascendente_cuadra_no_se_toca(self):
        """Lo que hoy funciona no cambia: el ascendente es preferente."""
        pistas = subs("English", "Spanish")
        ok, motivo = asignar_packet_counts(
            pistas, {0x1200: 12000, 0x1201: 5000},
            mpls(*[(0x1200, "eng"), (0x1201, "spa")]))
        self.assertTrue(ok)
        self.assertEqual([p.packet_count for p in pistas], [12000, 5000])
        self.assertIn("ascendente", motivo)

    def test_sin_idiomas_utiles_se_queda_en_el_ascendente(self):
        """Un MPLS con códigos vacíos o 'und' no puede desempatar."""
        pistas = subs("English", "Spanish")
        ok, motivo = asignar_packet_counts(
            pistas, {0x1200: 12000, 0x1201: 5000},
            mpls(*[(0x1201, ""), (0x1200, "und")]))
        self.assertTrue(ok)
        self.assertEqual([p.packet_count for p in pistas], [12000, 5000])
        self.assertIn("ascendente", motivo)


class TestDegradacion(unittest.TestCase):
    def test_sin_conteos_no_falla(self):
        pistas = subs("English")
        self.assertEqual(asignar_packet_counts(pistas, {})[0], False)
        self.assertEqual(asignar_packet_counts([], {0x1200: 5})[0], False)

    def test_no_asignar_deja_intacto_el_patron_estructural(self):
        """La señal de la que depende phase_b (y phase_e) cuando no hay
        conteos es `bitrate_kbps`. No asignar no la toca."""
        pistas = subs("Spanish", "Spanish", forzados=(1,))
        asignar_packet_counts(pistas, {0x1200: 4516})   # longitudes no cuadran
        self.assertEqual([p.bitrate_kbps for p in pistas], [30.0, 1.0])


if __name__ == "__main__":
    unittest.main()
