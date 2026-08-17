"""La Fase F perdía la barra y el ETA en cuanto empezaba la segunda pasada.

`dovi_tool inject-rpu` recorre la entrada DOS veces. Medido en John Wick 4
(source.hevc de 73,2 GB, fase de 815,2 s):

    09:06:34  Processing input video for frame order info...   ← pasada 1
    09:08:38  Rewriting file with interleaved RPU NALs..       ← pasada 2
    09:20:01  ✓ Fase inject completada en 815.2s

    pasada 1 = 124 s (15 %)   ·   pasada 2 = 683 s (84 %)

Cada pasada tenía su propia señal: la posición de lectura en la primera, el
tamaño del fichero de salida en la segunda. Mezclarlas con el tope monotónico
las rompía: al acabar la pasada 1 la posición de lectura llegaba al 100 %, y
`max(pct, _last_pct)` dejaba la barra clavada ahí el resto de la fase. Con el
porcentaje congelado, `eta()` no veía avance y devolvía None, y como el
respaldo por reloj solo entraba cuando NO había porcentaje, el ETA se
esfumaba. En el log:

    (1min 4s · quedan ~0min 57s)     ← pasada 1, señal real
    (2min 4s · quedan ~0min 2s)
    (3min 5s)                        ← pasada 2: ni % ni ETA…
    (12min 10s)                      ← …durante once minutos más

La señal que sí atraviesa las dos pasadas es `rchar` de /proc: bytes leídos
acumulados, que solo crecen. Verificado contra el proceso real a mitad de la
pasada 2: rchar 108,97 GB = 73,2 (pasada 1) + 35,66 escritos, y wchar 35,66.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

GB = 1_000_000_000
ENTRADA = int(73.2 * GB)


class TestSenalQueAtraviesaLasDosPasadas(unittest.TestCase):
    def _reader(self, tmp, expected_read=0, expected_out=0):
        from phases.cmv40_pipeline import _ReadProgress
        return _ReadProgress(
            pid=1234, input_path=tmp / "source.hevc",
            output_path=tmp / "source_injected.hevc",
            expected_out=expected_out, expected_read=expected_read)

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        # La entrada existe (su tamaño es `total`); la salida aún no.
        (self.tmp / "source.hevc").write_bytes(b"")

    def test_rchar_da_progreso_continuo_en_las_dos_pasadas(self):
        r = self._reader(self.tmp, expected_read=2 * ENTRADA,
                         expected_out=ENTRADA)
        # Recorrido real: pasada 1 lee la entrada entera, pasada 2 la relee
        # mientras escribe.
        lecturas = [10 * GB, 40 * GB, 73 * GB,          # pasada 1
                    int(73.2 * GB) + 20 * GB,           # pasada 2
                    int(73.2 * GB) + 60 * GB,
                    2 * ENTRADA]
        pcts = []
        with mock.patch("phases.cmv40_pipeline._proc_rchar", side_effect=lecturas):
            for _ in lecturas:
                pcts.append(r.sample())
        self.assertEqual(pcts, sorted(pcts), pcts)
        # Al terminar la pasada 1 va por la mitad, no al 100 %
        self.assertLess(pcts[2], 55, pcts)
        self.assertGreater(pcts[2], 45, pcts)
        self.assertGreaterEqual(pcts[-1], 99.9, pcts)

    def test_el_eta_sigue_vivo_durante_la_pasada_2(self):
        """Lo que se perdía: con el % congelado, eta() devolvía None."""
        import time
        r = self._reader(self.tmp, expected_read=2 * ENTRADA,
                         expected_out=ENTRADA)
        base = time.monotonic()
        lecturas = [int(73.2 * GB) + 10 * GB, int(73.2 * GB) + 20 * GB,
                    int(73.2 * GB) + 30 * GB]
        with mock.patch("phases.cmv40_pipeline._proc_rchar", side_effect=lecturas):
            with mock.patch("phases.cmv40_pipeline.time.monotonic",
                            side_effect=[base, base + 60, base + 120]):
                for _ in lecturas:
                    r.sample()
        # 10 GB/min sobre un total de 146,4 GB: va por 103,2 → quedan 43,2 GB
        # → ~4min 20s. Lo que importa es que exista y sea del orden correcto.
        eta = r.eta()
        self.assertIsNotNone(eta)
        self.assertAlmostEqual(eta, 259, delta=15)

    def test_sin_expected_read_se_comporta_como_antes(self):
        """Los demás comandos (una sola pasada) no cambian de señal."""
        r = self._reader(self.tmp, expected_out=ENTRADA)
        (self.tmp / "source_injected.hevc").write_bytes(b"0" * 1000)
        with mock.patch("phases.cmv40_pipeline._proc_rchar",
                        side_effect=AssertionError("no debe consultarse")):
            pct = r.sample()
        self.assertIsNotNone(pct)

    def test_rchar_no_disponible_cae_a_las_señales_de_antes(self):
        r = self._reader(self.tmp, expected_read=2 * ENTRADA,
                         expected_out=ENTRADA)
        (self.tmp / "source_injected.hevc").write_bytes(b"0" * (10 * 1000))
        with mock.patch("phases.cmv40_pipeline._proc_rchar", return_value=None):
            self.assertIsNotNone(r.sample())


class TestElEtaNoDesaparece(unittest.TestCase):
    """El respaldo por reloj tiene que entrar cuando falta el ETA real, no
    solo cuando falta el porcentaje."""

    def test_run_streaming_respalda_el_eta_por_separado(self):
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        i = src.index("    async def _ticker():")
        cuerpo = src[i:src.index("    tick_task = asyncio.create_task", i)]
        # El respaldo del ETA debe ser un `if` propio, no ir dentro del
        # `if step_pct is None`
        self.assertIn("if eta is None:", cuerpo)
        self.assertNotIn("eta = max(0.0, time_est - elapsed)", cuerpo)

    def test_run_with_time_estimate_respalda_el_eta_por_separado(self):
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        i = src.index("async def _run_with_time_estimate")
        cuerpo = src[i:src.index("\nasync def ", i + 10)]
        self.assertIn("if eta is None:", cuerpo)
        self.assertNotIn("eta = max(0.0, est - elapsed)", cuerpo)

    def test_el_heartbeat_solo_anuncia_el_porcentaje_si_es_medido(self):
        """Un % venido de la estimación por reloj no puede presentarse como
        dato: en Fase F la estimación era 919 s y la fase duró 815 s."""
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        self.assertEqual(
            src.count('f"{label or \'Proceso\'} ({step_pct:.0f}%)" if real else'), 1)


class TestFaseFDeclaraLasDosPasadas(unittest.TestCase):
    def test_la_llamada_pasa_el_total_a_leer(self):
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        i = src.index('DOVI_TOOL_BIN, "inject-rpu"')
        cuerpo = src[i:i + 1400]
        self.assertIn('"expected_read_bytes"', cuerpo)
        self.assertIn('"expected_out_bytes"', cuerpo)



class TestLatidoEnFasesConProgresoExacto(unittest.IsolatedAsyncioTestCase):
    """mkvmerge da un porcentaje exacto, y era la única fase larga sin tiempos.

    Medido en el remux de John Wick 4 (813,6 s): 101 líneas de "Progress: N%"
    y ni un "lleva/quedan" en el log. El ETA sí se calculaba, pero solo viajaba
    a la barra; la línea "Progress: N%" es contrato del parser del frontend y
    no se puede ampliar. El ticker, que es quien escribe los latidos, se
    retiraba en cuanto aparecía progreso real.
    """

    async def _correr(self, guion, heartbeat_s):
        import phases.cmv40_pipeline as pipe
        import tempfile
        emitidos = []

        async def _log(msg):
            emitidos.append(msg)

        with tempfile.TemporaryDirectory() as td:
            sh = Path(td) / "falso.sh"
            sh.write_text("#!/bin/sh\n" + guion)
            sh.chmod(0o755)
            orig = pipe.HEARTBEAT_EVERY_S
            pipe.HEARTBEAT_EVERY_S = heartbeat_s
            try:
                rc = await pipe._run_streaming(
                    [str(sh)], log_callback=_log,
                    progress_ctx={"time_estimate_s": 60.0, "label": "Remuxando"})
            finally:
                pipe.HEARTBEAT_EVERY_S = orig
        return rc, emitidos

    async def test_escribe_latidos_con_pct_y_tiempo(self):
        # Más de 3,5 s de vida: el ticker espera 3 s antes del primer tick
        # para no hablar en comandos que terminan enseguida.
        guion = "".join(
            f'echo "#GUI#progress {p}%"\nsleep 0.45\n'
            for p in (5, 12, 20, 30, 40, 50, 60, 70, 80, 90, 99))
        rc, emitidos = await self._correr(guion, heartbeat_s=0.5)
        self.assertEqual(rc, 0)
        # El contrato del parser sigue intacto
        self.assertTrue(any(m.startswith("Progress: ") for m in emitidos), emitidos)
        latidos = [m for m in emitidos if "en curso…" in m]
        self.assertTrue(latidos, emitidos)
        self.assertRegex(latidos[-1], r"Remuxando \(\d+%\) en curso…")
        self.assertTrue(any("quedan" in m for m in latidos), latidos)

    async def test_no_toca_la_linea_de_progress(self):
        rc, emitidos = await self._correr(
            'echo "#GUI#progress 42%"\nsleep 0.2\n', heartbeat_s=99)
        self.assertEqual(rc, 0)
        self.assertIn("Progress: 42%", emitidos)

if __name__ == "__main__":
    unittest.main()
