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
        self.assertEqual(counts[PID_COMPLETE], 0, "HEAD-only debería no ver la completa tardía")
        self.assertGreater(counts[PID_FORCED], 0, "el forzado del inicio sí debe verse")

    def test_distributed_counts_late_complete(self):
        # Distribuido: los tramos del final capturan la completa.
        counts = self._count(sample_segments=4)
        self.assertGreater(counts[PID_COMPLETE], 0, "la completa tardía debe contarse")
        self.assertGreater(
            counts[PID_COMPLETE], counts[PID_FORCED] * 3,
            "la completa debe superar de lejos al forzado (ratio >3×)",
        )

    def test_alignment_after_seek(self):
        # Los tramos 2..N arrancan en offsets no alineados a 192; el conteo solo
        # es correcto si _find_alignment reencuentra la frontera de paquete.
        counts = self._count(sample_segments=4)
        # PID_COMPLETE aparece en packets pares de [500,1000) = 250 eventos en
        # todo el fichero. Con 4 tramos cubriendo ~40% repartido, esperamos una
        # fracción no trivial (>20) — imposible sin realineación tras seek.
        self.assertGreater(counts[PID_COMPLETE], 20)


class TestCadaTramoSeRealinea(unittest.TestCase):
    """Con todos los paquetes del mismo PID, lo leído en los tramos es
    exactamente lo que hay que contar. `test_alignment_after_seek` pedía
    «más de 20» y aguantaba que la realineación devolviera siempre 0."""

    def test_se_cuentan_casi_todos_los_paquetes_leidos(self):
        # 4.999 y no 5.000: con 5.000 los cuatro saltos caían en múltiplos
        # exactos de 192 bytes, no había nada que realinear y una realineación
        # rota pasaba en verde. La condición se comprueba abajo.
        paquetes = 4999
        tmp = tempfile.NamedTemporaryFile(suffix=".m2ts", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        with open(tmp.name, "wb") as f:
            for _ in range(paquetes):
                f.write(_make_packet(PID_COMPLETE))
        presupuesto = paquetes * PACKET_SIZE * 4 // 10
        tramo = presupuesto // 4
        span = paquetes * PACKET_SIZE - tramo
        saltos = [(span * i) // 3 for i in range(1, 4)]
        self.assertTrue(all(x % PACKET_SIZE for x in saltos),
                        f"los saltos caen en frontera de paquete: {saltos}")
        counts = asyncio.run(count_pgs_packets_ts_parse(
            tmp.name, sample_bytes=presupuesto, pid_list=[PID_COMPLETE],
            sample_segments=4))
        leidos = presupuesto // PACKET_SIZE
        # Cada tramo pierde a lo sumo el paquete partido por el seek.
        self.assertGreaterEqual(counts[PID_COMPLETE], leidos - 8, counts)


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
            self.assertEqual(counts[PID_COMPLETE], 100)
            self.assertEqual(counts[PID_FORCED], 5)
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()


class TestScanVectorizado(unittest.IsolatedAsyncioTestCase):
    """El conteo por PID debe dar exactamente lo mismo que el recorrido
    paquete a paquete, que es lo que había antes.

    El bucle byte a byte costaba ~60 s de CPU sobre el presupuesto de 4 GB
    (21 M de paquetes, dos llamadas a función y accesos a dict por cada uno)
    y se llevaba la mayor parte del análisis inicial de un disco. La versión
    nueva hace el mismo trabajo con slicing extendido + translate + Counter,
    que corren en C.
    """

    def _fake_m2ts(self, path, pid_plan, packet_size=192):
        """Escribe un TS sintético: pid_plan es una lista de PIDs, un paquete
        por entrada."""
        sync_off = 4 if packet_size == 192 else 0
        with open(path, "wb") as f:
            for pid in pid_plan:
                pkt = bytearray(packet_size)
                pkt[sync_off] = 0x47
                pkt[sync_off + 1] = (pid >> 8) & 0x1F
                pkt[sync_off + 2] = pid & 0xFF
                f.write(pkt)

    async def test_mismos_conteos_que_el_recorrido_lento(self):
        import tempfile, os
        from phases.phase_a import count_pgs_packets_ts_parse
        # Mezcla realista: vídeo, audio, tres subtítulos con volúmenes muy
        # distintos (completo, alternativo, forzado disperso) y basura.
        plan = ([0x1011] * 500 + [0x1100] * 200
                + [0x1200] * 137 + [0x1201] * 41 + [0x1202] * 3
                + [0x1FFF] * 60)
        plan.sort(key=lambda _: 0)  # estable: mantiene el orden de arriba
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.m2ts")
            self._fake_m2ts(p, plan)
            got = await count_pgs_packets_ts_parse(
                p, pid_list=[0x1200, 0x1201, 0x1202])
        # El contrato es {PID: conteo}: quién es quién lo decide el asignador
        self.assertEqual(got, {0x1200: 137, 0x1201: 41, 0x1202: 3})

    async def test_pid_ausente_del_muestreo_queda_a_cero(self):
        """Un forzado muy disperso puede no aparecer; debe salir con 0, que
        es lo que phase_b interpreta como 'usa el patrón estructural'."""
        import tempfile, os
        from phases.phase_a import count_pgs_packets_ts_parse
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.m2ts")
            self._fake_m2ts(p, [0x1200] * 50)
            got = await count_pgs_packets_ts_parse(
                p, pid_list=[0x1200, 0x12A3])
        self.assertEqual(got, {0x1200: 50, 0x12A3: 0})

    async def test_paquetes_desalineados_caen_al_recorrido_lento(self):
        """Si algún paquete no empieza por 0x47 hay que mirarlos uno a uno:
        la vía rápida asume alineación perfecta."""
        import tempfile, os
        from phases.phase_a import count_pgs_packets_ts_parse
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.m2ts")
            self._fake_m2ts(p, [0x1200] * 40)
            # Rompemos el sync de un paquete del medio
            with open(p, "r+b") as f:
                f.seek(20 * 192 + 4)
                f.write(b"\x00")
            got = await count_pgs_packets_ts_parse(p, pid_list=[0x1200])
        # 39 válidos: el corrupto no se cuenta, el resto sí
        self.assertEqual(got[0x1200], 39)

    async def test_sin_pid_list_usa_el_rango_por_defecto(self):
        import tempfile, os
        from phases.phase_a import count_pgs_packets_ts_parse
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.m2ts")
            self._fake_m2ts(p, [0x1200] * 10 + [0x12FF] * 5 + [0x1300] * 7)
            got = await count_pgs_packets_ts_parse(p)
        # 0x1300 queda fuera del rango PGS (0x1200-0x12FF).
        self.assertEqual(got, {0x1200: 10, 0x12FF: 5})

    async def test_paquetes_de_188_bytes(self):
        """TS estándar, sin la cabecera BDAV de 4 bytes."""
        import tempfile, os
        from phases.phase_a import count_pgs_packets_ts_parse
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.ts")
            self._fake_m2ts(p, [0x1200] * 30 + [0x1201] * 12, packet_size=188)
            got = await count_pgs_packets_ts_parse(p, pid_list=[0x1200, 0x1201])
        self.assertEqual(got, {0x1200: 30, 0x1201: 12})
