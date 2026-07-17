"""Regresión del muestreo distribuido de paquetes PGS (Fase A).

`count_pgs_packets_ts_parse` leía solo los primeros `sample_bytes` desde el
inicio del m2ts. En un UHD de ~100 GB eso son ~5 min del arranque, y una pista
completa con poca actividad al principio (típico: subtítulos españoles) quedaba
con una cuenta parecida a la de un forzado → phase_b los confundía por ratio
(caso real: La Momia 2026, ES completo 1096 vs forzado 988 = 1.1×).

El fix reparte el mismo presupuesto de bytes en `sample_segments` tramos
equiespaciados a lo largo de TODO el fichero. Este test construye un m2ts BDAV
sintético donde:

  - la pista "completa" tiene sus eventos SOLO en la segunda mitad del fichero,
  - la pista "forzada" tiene unos pocos eventos al principio.

Con muestreo HEAD-only (sample_segments=1) la completa queda a 0 (bug). Con
muestreo distribuido (sample_segments=4) se cuenta correctamente y supera de
lejos al forzado.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_pgs_sampling -v
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.phase_a import count_pgs_packets_ts_parse  # noqa: E402

PACKET_SIZE = 192          # BDAV: 4 bytes ATC + 188 bytes TS
SYNC_OFF = 4
PID_COMPLETE = 0x1200
PID_FORCED = 0x1201
PID_NULL = 0x1FFF          # relleno AV (no se cuenta)


def _make_packet(pid: int) -> bytes:
    """Construye un paquete BDAV de 192 bytes con el PID dado."""
    pkt = bytearray(PACKET_SIZE)
    pkt[SYNC_OFF] = 0x47
    pkt[SYNC_OFF + 1] = (pid >> 8) & 0x1F
    pkt[SYNC_OFF + 2] = pid & 0xFF
    pkt[SYNC_OFF + 3] = 0x10  # payload only, sin adaptation field
    return bytes(pkt)


def _build_synthetic_m2ts(path: str, total_packets: int = 1000) -> None:
    """Escribe un m2ts BDAV sintético.

    - PID_COMPLETE: eventos solo en la 2ª mitad (packets pares >= 500).
    - PID_FORCED:   pocos eventos al principio (packets 0,10,20,30,40).
    - resto:        packets null (relleno).
    """
    with open(path, "wb") as f:
        for i in range(total_packets):
            if i >= total_packets // 2 and i % 2 == 0:
                pid = PID_COMPLETE
            elif i < 50 and i % 10 == 0:
                pid = PID_FORCED
            else:
                pid = PID_NULL
            f.write(_make_packet(pid))


class TestDistributedPgsSampling(unittest.TestCase):
    def setUp(self):
        self.total_packets = 1000
        self.tmp = tempfile.NamedTemporaryFile(suffix=".m2ts", delete=False)
        self.tmp.close()
        _build_synthetic_m2ts(self.tmp.name, self.total_packets)
        self.file_size = os.path.getsize(self.tmp.name)
        # Presupuesto = 40% del fichero → fuerza muestreo (file_size > sample_bytes).
        self.sample_bytes = (self.total_packets * PACKET_SIZE) * 4 // 10

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _count(self, sample_segments):
        return asyncio.run(count_pgs_packets_ts_parse(
            self.tmp.name,
            sample_bytes=self.sample_bytes,
            pid_list=[PID_COMPLETE, PID_FORCED],
            sample_segments=sample_segments,
        ))

    def test_head_only_misses_late_complete(self):
        # HEAD-only: la completa (2ª mitad) no aparece en la muestra del inicio.
        counts = self._count(sample_segments=1)
        self.assertEqual(counts[0], 0, "HEAD-only debería no ver la completa tardía")
        self.assertGreater(counts[1], 0, "el forzado del inicio sí debe verse")

    def test_distributed_counts_late_complete(self):
        # Distribuido: los tramos del final capturan la completa.
        counts = self._count(sample_segments=4)
        self.assertGreater(counts[0], 0, "la completa tardía debe contarse")
        self.assertGreater(
            counts[0], counts[1] * 3,
            "la completa debe superar de lejos al forzado (ratio >3×)",
        )

    def test_alignment_after_seek(self):
        # Los tramos 2..N arrancan en offsets no alineados a 192; el conteo solo
        # es correcto si _find_alignment reencuentra la frontera de paquete.
        counts = self._count(sample_segments=4)
        # PID_COMPLETE aparece en packets pares de [500,1000) = 250 eventos en
        # todo el fichero. Con 4 tramos cubriendo ~40% repartido, esperamos una
        # fracción no trivial (>20) — imposible sin realineación tras seek.
        self.assertGreater(counts[0], 20)


class TestSequentialSmallFile(unittest.TestCase):
    """Si el fichero cabe entero en el presupuesto → un solo tramo secuencial."""

    def test_full_file_fits_budget(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".m2ts", delete=False)
        tmp.close()
        try:
            _build_synthetic_m2ts(tmp.name, total_packets=400)
            counts = asyncio.run(count_pgs_packets_ts_parse(
                tmp.name,
                sample_bytes=10 * 1024 * 1024,  # holgado, cabe entero
                pid_list=[PID_COMPLETE, PID_FORCED],
                sample_segments=4,
            ))
            # 400 packets: completa en pares [200,400) = 100; forzado 5.
            self.assertEqual(counts[0], 100)
            self.assertEqual(counts[1], 5)
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
