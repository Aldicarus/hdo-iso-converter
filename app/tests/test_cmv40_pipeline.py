"""Tests de helpers del pipeline CMv4.0 (Tab 3).

Cubren la regresión de Proyecto Salvación (2026-08): el demux de un UHD
largo superaba el timeout FIJO de 900s y moría a mitad, dejando un BL.hevc
truncado que el reintento reutilizaba → MKV corrupto. El fix:

  - `_adaptive_timeout`: timeout proporcional a la estimación (no fijo).
  - `_demux_output_reusable`: solo reutiliza el demux si terminó (marcador).

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_pipeline -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Forzar CONFIG_DIR a un tempdir ANTES de importar (storage lo lee al import).
os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp(prefix="cmv40_pipeline_test_"))

from phases.cmv40_pipeline import (  # noqa: E402
    _adaptive_timeout,
    _demux_output_reusable,
)


class TestAdaptiveTimeout(unittest.TestCase):
    def test_scales_with_estimate(self):
        # 990s estimados (caso Proyecto Salvación) → 3× = 2970, muy por encima
        # del antiguo tope fijo de 900s que causaba el timeout.
        self.assertEqual(_adaptive_timeout(990.0), 2970)

    def test_floor_applies_for_small_estimates(self):
        # Estimaciones minúsculas (sesión sin ancla de ffmpeg) → suelo.
        self.assertEqual(_adaptive_timeout(10.0), 1800)
        self.assertEqual(_adaptive_timeout(0.0), 1800)

    def test_custom_floor(self):
        self.assertEqual(_adaptive_timeout(100.0, floor_s=1200), 1200)
        self.assertEqual(_adaptive_timeout(500.0, floor_s=1200), 1500)

    def test_never_below_old_fixed_for_large_movies(self):
        # Para cualquier peli cuya estimación superaba los 900s, el timeout
        # adaptativo es estrictamente mayor → no puede reproducirse el bug.
        for est in (901, 990, 1500, 3000):
            self.assertGreater(_adaptive_timeout(est), 900)


class TestDemuxOutputReusable(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="demux_reuse_"))
        self.bl = self.tmp / "BL.hevc"
        self.el = self.tmp / "EL.hevc"
        self.marker = self.tmp / ".demux_done"

    def test_not_reusable_without_marker(self):
        # BL/EL presentes pero SIN marcador = demux truncado (timeout/kill).
        self.bl.write_bytes(b"partial")
        self.el.write_bytes(b"partial")
        self.assertFalse(_demux_output_reusable(self.bl, self.el, self.marker))

    def test_reusable_with_marker(self):
        self.bl.write_bytes(b"data")
        self.el.write_bytes(b"data")
        self.marker.write_text("225177")
        self.assertTrue(_demux_output_reusable(self.bl, self.el, self.marker))

    def test_not_reusable_if_bl_missing(self):
        self.el.write_bytes(b"data")
        self.marker.write_text("225177")
        self.assertFalse(_demux_output_reusable(self.bl, self.el, self.marker))

    def test_not_reusable_if_el_missing(self):
        self.bl.write_bytes(b"data")
        self.marker.write_text("225177")
        self.assertFalse(_demux_output_reusable(self.bl, self.el, self.marker))


if __name__ == "__main__":
    unittest.main()
