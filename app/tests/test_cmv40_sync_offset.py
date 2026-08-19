"""`detect_sync_offset` tiene que ver el desfase que existe para detectar.

El caso dominante en la Fase D es un logo de estudio al principio del Blu-ray
que la versión de streaming del RPU target no trae. Un logo es OSCURO, y la
ventana de análisis se recortaba buscando el primer frame con brillo **en cada
serie por separado**: eso alineaba las dos ventanas antes de correlacionarlas
y devolvía `offset 0` con **100 % de confianza**.

El contraste A/B es lo que delata el bug: con el mismo desfase, un logo
brillante SÍ se detectaba. O sea que el algoritmo estaba bien y lo que fallaba
era el recorte previo.

Es una función pura: no hace falta un disco ni un RPU real para fijarlo.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_sync_offset -v
"""
import math
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from phases.cmv40_pipeline import detect_sync_offset  # noqa: E402

OFFSET = 72          # ~3 s de logo a 24 fps
N = 2000


def _curva(n: int, seed: int = 0) -> list[int]:
    """MaxCLL con forma reconocible: dos senos de periodo distinto, como el
    encadenado de escenas de una peli real (variación por shot + por acto)."""
    return [int(400 + 380 * math.sin((i + seed) / 37.0)
                    + 300 * math.sin((i + seed) / 7.3)) for i in range(n)]


def _pfd(src: list[int], tgt: list[int]) -> dict:
    n = max(len(src), len(tgt))
    return {"data": [{"frame": i,
                      "src_maxcll": src[i] if i < len(src) else 0,
                      "tgt_maxcll": tgt[i] if i < len(tgt) else 0}
                     for i in range(n)]}


class TestLogoDeEstudio(unittest.TestCase):
    """El source (BD) trae N frames de logo que el target no tiene."""

    def test_logo_oscuro_se_detecta(self):
        """EL BUG. Un logo a 0 nits desfasa 72 frames y hay que verlo."""
        contenido = _curva(N)
        src = [0] * OFFSET + contenido
        tgt = contenido + [0] * OFFSET
        r = detect_sync_offset(_pfd(src, tgt))
        self.assertEqual(abs(r["offset"]), OFFSET,
                         f"logo oscuro de {OFFSET} frames no detectado: {r}")

    def test_logo_brillante_se_detecta(self):
        """Contraste: el mismo desfase con logo brillante ya funcionaba."""
        contenido = _curva(N)
        src = [900] * OFFSET + contenido
        tgt = contenido + [900] * OFFSET
        r = detect_sync_offset(_pfd(src, tgt))
        self.assertEqual(abs(r["offset"]), OFFSET, r)

    def test_no_inventa_desfase_cuando_estan_alineados(self):
        """Sin desfase, offset 0 — el arreglo no debe crear falsos positivos."""
        contenido = _curva(N)
        r = detect_sync_offset(_pfd(list(contenido), list(contenido)))
        self.assertEqual(r["offset"], 0, r)
        self.assertGreater(r["confidence"], 0.9, r)

    def test_tramo_negro_compartido_no_desplaza(self):
        """Si LAS DOS empiezan con 300 frames negros (apertura común), el
        recorte con origen común los salta y no aparece un desfase falso."""
        contenido = _curva(N)
        src = [0] * 300 + contenido
        tgt = [0] * 300 + contenido
        r = detect_sync_offset(_pfd(src, tgt))
        self.assertEqual(r["offset"], 0, r)


class TestRobustez(unittest.TestCase):

    def test_un_offset_extremo_no_gana_con_una_sola_muestra(self):
        """En el extremo del barrido solapaba UNA muestra, y una coincidencia
        por casualidad ganaba con RMS 0 y confianza 100 %.

        Caso construido: 300 muestras (ventana de comparación 150), dos pelis
        distintas, y `tgt[0]` puesto al valor exacto de `src[149]` — la única
        comparación que queda en offset -149 encaja perfecta. Sin solape mínimo
        el resultado era `offset=-149, confianza=100 %` sobre dos series que no
        tienen nada que ver."""
        src = _curva(300, seed=0)
        tgt = _curva(300, seed=911)          # otra peli
        tgt[0] = src[149]                    # coincidencia exacta en el extremo
        r = detect_sync_offset(_pfd(src, tgt))
        self.assertLess(abs(r["offset"]), 100,
                        f"un offset extremo ganó con una sola muestra: {r}")

    def test_series_vacias_no_revientan(self):
        r = detect_sync_offset({"data": []})
        self.assertEqual(r["offset"], 0)
        self.assertEqual(r["confidence"], 0.0)

    def test_none_en_maxcll_no_revienta(self):
        """`src_maxcll` puede llegar a None si el export perdió un frame."""
        data = [{"frame": i, "src_maxcll": None, "tgt_maxcll": None} for i in range(200)]
        r = detect_sync_offset({"data": data})
        self.assertEqual(r["offset"], 0)


if __name__ == "__main__":
    unittest.main()
