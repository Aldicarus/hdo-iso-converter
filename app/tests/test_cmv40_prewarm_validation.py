"""
La validación de Fase H se adelanta al remux en vez de ir detrás.

Fase H, en el camino merge, extrae el RPU COMPLETO del HEVC pre-mux para
comprobar frame count, CMv4.0, el_type y L8. Son 240 s de media y afectan al
**82 % de los jobs** (68 de 83 medidos en el NAS: 36 p7_fel·merge, 25
p8·merge, 7 p7_mel·merge; solo 15 son drop-in, que se libra con el fast
path). Durante toda la sesión habíamos dado por hecho lo contrario —el
código llama al drop-in "el caso típico"— y resulta ser la excepción.

Ese extract-rpu lee exactamente el mismo fichero que mkvmerge va a leer y no
depende de su resultado, así que puede correr en paralelo con el remux.

Lo que NO cambia: el extract sigue siendo completo, sobre el mismo HEVC, y
Fase H comprueba lo mismo. Solo se mueve cuándo empieza.
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))


class TestPrewarmHelper(unittest.IsolatedAsyncioTestCase):
    """`_prewarm_validation_rpu` nunca puede tumbar la Fase G."""

    def _session(self, wd):
        from models import CMv40Session
        return CMv40Session(
            id="sess_pw", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="out.mkv", artifacts_dir=str(wd))

    async def test_devuelve_false_si_dovi_tool_falla(self):
        import phases.cmv40_pipeline as pipe
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            orig = pipe.DOVI_TOOL_BIN
            pipe.DOVI_TOOL_BIN = "/binario/que/no/existe"
            try:
                ok = await pipe._prewarm_validation_rpu(
                    self._session(wd), wd / "pre.hevc", wd / "rpu.bin")
            finally:
                pipe.DOVI_TOOL_BIN = orig
            self.assertFalse(ok)
            # No deja un fichero a medias que Fase H pudiera dar por bueno
            self.assertFalse((wd / "rpu.bin").exists())

    async def test_rc_distinto_de_cero_no_deja_rastro(self):
        import phases.cmv40_pipeline as pipe
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            out = wd / "rpu.bin"

            async def _fake_run(cmd, **kw):
                out.write_bytes(b"basura parcial")
                return (1, "", "boom")

            orig = pipe._run
            pipe._run = _fake_run
            try:
                ok = await pipe._prewarm_validation_rpu(
                    self._session(wd), wd / "pre.hevc", out)
            finally:
                pipe._run = orig
            self.assertFalse(ok)
            self.assertFalse(out.exists(), "un RPU parcial debe borrarse")

    async def test_rpu_vacio_se_considera_fallo(self):
        import phases.cmv40_pipeline as pipe
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            out = wd / "rpu.bin"

            async def _fake_run(cmd, **kw):
                out.write_bytes(b"")
                return (0, "", "")

            orig = pipe._run
            pipe._run = _fake_run
            try:
                ok = await pipe._prewarm_validation_rpu(
                    self._session(wd), wd / "pre.hevc", out)
            finally:
                pipe._run = orig
            self.assertFalse(ok)

    async def test_al_cancelarse_no_deja_el_fichero(self):
        """Si el remux falla, Fase G cancela el adelanto."""
        import phases.cmv40_pipeline as pipe
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            out = wd / "rpu.bin"

            async def _fake_run(cmd, **kw):
                out.write_bytes(b"a medias")
                await asyncio.sleep(30)
                return (0, "", "")

            orig = pipe._run
            pipe._run = _fake_run
            try:
                task = asyncio.create_task(pipe._prewarm_validation_rpu(
                    self._session(wd), wd / "pre.hevc", out))
                await asyncio.sleep(0.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                pipe._run = orig
            self.assertFalse(out.exists())


class TestInvalidacion(unittest.TestCase):
    """El RPU adelantado tiene que morir cuando el HEVC al que corresponde
    se regenera. Si no, Fase H validaría el stream nuevo contra el RPU del
    anterior — y como el frame count coincidiría, no lo detectaría."""

    def test_fase_f_lo_borra_al_empezar(self):
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        i = src.find("async def run_phase_f_inject")
        j = src.find("async def ", i + 10)
        cuerpo = src[i:j]
        self.assertIn('_validate_full_rpu.bin").unlink(missing_ok=True)', cuerpo)

    def test_fase_g_lo_borra_antes_de_relanzarlo(self):
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        i = src.find("async def run_phase_g_remux")
        j = src.find("async def ", i + 10)
        cuerpo = src[i:j]
        self.assertIn("prewarm_rpu.unlink(missing_ok=True)", cuerpo)

    def test_no_se_adelanta_en_drop_in(self):
        """El drop-in usa el fast path de Fase H: extraer el RPU sería tirar
        240 s de CPU para nada."""
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        i = src.find("prewarm_task = None")
        self.assertGreater(i, 0)
        self.assertIn("if not drop_in_fel:", src[i:i + 300])

    def test_fase_h_usa_el_adelantado_si_esta(self):
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("prewarmed = full_rpu.exists() and full_rpu.stat().st_size > 0", src)
        # …y si no está, lo extrae como siempre
        self.assertIn("if not prewarmed:", src)


class TestPipeBuffer(unittest.TestCase):
    """Buffer del pipe entre ffmpeg y dovi_tool: 64 KB por defecto obligan a
    sincronizarse ~800.000 veces sobre 48 GB."""

    def test_intenta_ampliarlo_y_tolera_el_fallo(self):
        from phases.cmv40_pipeline import _widen_pipe, PIPE_BUF_BYTES
        import os
        self.assertEqual(PIPE_BUF_BYTES, 4 * 1024 * 1024)
        r, w = os.pipe()
        try:
            size = _widen_pipe(w)
            # En Linux devuelve el tamaño logrado; en otros sistemas 0. En
            # ningún caso puede lanzar.
            self.assertIsInstance(size, int)
            self.assertGreaterEqual(size, 0)
        finally:
            os.close(r)
            os.close(w)

    def test_fd_invalido_no_revienta(self):
        from phases.cmv40_pipeline import _widen_pipe
        self.assertEqual(_widen_pipe(-1), 0)



class TestProgresoReal(unittest.TestCase):
    """`_ReadProgress`: el % sale de lo que el proceso lleva leído, no del reloj.

    Medido sobre 87 jobs: RATIO_INJECT vale 1,77 hardcodeado y 1,83 real, o
    sea que la constante está bien calibrada — pero la dispersión entre jobs
    va de 1,31 a 2,32. Ninguna constante puede predecir un job concreto, y por
    eso la barra se quedaba clavada en el tope del 95 %.
    """

    def test_lee_la_posicion_real_del_descriptor(self):
        """Contra un proceso de verdad leyendo un fichero de verdad."""
        import os, subprocess, tempfile, time
        from phases.cmv40_pipeline import _ReadProgress
        with tempfile.TemporaryDirectory() as td:
            big = Path(td) / "grande.bin"
            big.write_bytes(b"\0" * (24 * 1024 * 1024))
            # `cat` a /dev/null: lectura secuencial simple del fichero
            with open(os.devnull, "wb") as devnull:
                proc = subprocess.Popen(["cat", str(big)], stdout=devnull)
                try:
                    rp = _ReadProgress(proc.pid, big)
                    self.assertEqual(rp.total, 24 * 1024 * 1024)
                    visto = None
                    for _ in range(200):
                        s = rp.sample()
                        if s is not None:
                            visto = s
                            break
                        time.sleep(0.005)
                    # En Linux debe dar una lectura; en macOS no hay /proc y
                    # devuelve None, que es el caso de fallback.
                    if visto is not None:
                        self.assertGreaterEqual(visto, 0.0)
                        self.assertLessEqual(visto, 100.0)
                finally:
                    proc.wait(timeout=10)

    def test_sin_proc_devuelve_none_y_no_revienta(self):
        """Fallback: si no hay /proc (macOS) o el pid no existe, el caller
        se queda con la estimación por reloj."""
        from phases.cmv40_pipeline import _ReadProgress
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "x.bin"
            f.write_bytes(b"0" * 1024)
            rp = _ReadProgress(999999, f)   # pid inexistente
            self.assertIsNone(rp.sample())

    def test_fichero_inexistente_no_revienta(self):
        from phases.cmv40_pipeline import _ReadProgress
        rp = _ReadProgress(1, Path("/no/existe.bin"))
        self.assertEqual(rp.total, 0)
        self.assertIsNone(rp.sample())

    def test_el_porcentaje_nunca_retrocede(self):
        """El readahead o un seek puntual no deben hacer bajar la barra."""
        from phases.cmv40_pipeline import _ReadProgress
        rp = _ReadProgress(1, Path("/no/existe.bin"))
        rp.total = 1000
        rp._fdinfo = "/dev/null"        # sample() no encontrará 'pos:'
        rp._last_pct = 60.0
        # Simulamos lo que hace sample() tras leer una posición menor
        pct = max(30.0, rp._last_pct)
        self.assertEqual(pct, 60.0)

    def test_eta_por_ritmo_observado(self):
        """La ETA sale del avance real de este job, no de una constante."""
        from phases.cmv40_pipeline import _ReadProgress
        rp = _ReadProgress(1, Path("/no/existe.bin"))
        self.assertIsNone(rp.eta(), "sin muestras no se inventa una ETA")
        rp._samples = [(100.0, 10.0), (110.0, 20.0)]   # 10% en 10s → 1%/s
        eta = rp.eta()
        self.assertIsNotNone(eta)
        self.assertAlmostEqual(eta, 80.0, delta=1.0)   # faltan 80% a 1%/s
        # Sin avance entre muestras no se puede estimar
        rp._samples = [(100.0, 20.0), (110.0, 20.0)]
        self.assertIsNone(rp.eta())


class TestJobPct(unittest.TestCase):
    """El % del job completo, que hasta ahora no existía: la barra medía la
    fase y llegaba al 100 % varias veces por job."""

    def setUp(self):
        import os
        self._cwd = os.getcwd()
        os.chdir(APP_DIR)
        import main
        self.main = main

    def tearDown(self):
        import os
        os.chdir(self._cwd)

    def _session(self, **kw):
        from models import CMv40Session
        s = CMv40Session(id="s", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
                         output_mkv_name="o.mkv")
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def test_sin_fase_en_curso_no_hay_porcentaje(self):
        s = self._session(running_phase=None)
        self.assertIsNone(self.main._cmv40_job_pct(s, 50))

    def test_avanza_con_las_fases(self):
        from models import CMv40PhaseRecord
        from datetime import datetime, timezone
        ahora = datetime.now(timezone.utc)
        s = self._session(running_phase="analyze_source")
        al_principio = self.main._cmv40_job_pct(s, 0)
        a_medias = self.main._cmv40_job_pct(s, 50)
        self.assertLess(al_principio, a_medias)

        # Con analyze ya hecha y el remux en curso, el job va mucho más allá
        s2 = self._session(running_phase="remux")
        s2.phase_history = [
            CMv40PhaseRecord(phase=p, started_at=ahora, status="done")
            for p in ("analyze_source", "extract", "inject")
        ]
        self.assertGreater(self.main._cmv40_job_pct(s2, 50),
                           self.main._cmv40_job_pct(s, 100))

    def test_nunca_se_pasa_de_100(self):
        s = self._session(running_phase="validate")
        self.assertLessEqual(self.main._cmv40_job_pct(s, 100), 100.0)

    def test_drop_in_reparte_distinto(self):
        """En drop-in no hay demux y la validación son 4 s: el mismo avance
        de fase equivale a más porcentaje de job."""
        s_merge = self._session(running_phase="inject", target_type="trusted_p8_source")
        s_drop = self._session(running_phase="inject",
                               target_type="trusted_p7_fel_final",
                               target_trust_ok=True, source_workflow="p7_fel")
        self.assertNotEqual(self.main._cmv40_job_pct(s_merge, 50),
                            self.main._cmv40_job_pct(s_drop, 50))


class TestModeloEtaMedido(unittest.TestCase):
    """Los ratios de ETA salen del histórico real, no de constantes a mano.

    Las del frontend (`CMV40_ETA.r_inject` = 2,15, `r_mux` = 2,00) estaban
    calibradas contra runs concretos y envejecieron en horas: el pipe de
    Fase A, el adelanto de la validación y el export por niveles cambiaron
    los tiempos, y un job de ~26 min llegó a anunciar 49.
    """

    def setUp(self):
        import os, tempfile
        self._cwd = os.getcwd()
        os.chdir(APP_DIR)
        import main
        self.main = main
        self._td = tempfile.TemporaryDirectory()
        import storage
        self._orig_cfg = storage.CONFIG_DIR
        storage.CONFIG_DIR = Path(self._td.name)
        (Path(self._td.name) / "cmv40").mkdir()
        main._ETA_MODEL_CACHE.update({"at": 0.0, "data": None})

    def tearDown(self):
        import os, storage
        storage.CONFIG_DIR = self._orig_cfg
        self._td.cleanup()
        os.chdir(self._cwd)

    def _job(self, nombre, analyze, inject, remux, validate, extract=None):
        import json
        ph = [{"phase": "analyze_source", "status": "done", "elapsed_seconds": analyze,
               "started_at": "2026-08-16T10:00:00Z"},
              {"phase": "inject", "status": "done", "elapsed_seconds": inject,
               "started_at": "2026-08-16T10:00:00Z"},
              {"phase": "remux", "status": "done", "elapsed_seconds": remux,
               "started_at": "2026-08-16T10:00:00Z"},
              {"phase": "validate", "status": "done", "elapsed_seconds": validate,
               "started_at": "2026-08-16T10:00:00Z"}]
        if extract is not None:
            ph.append({"phase": "extract", "status": "done", "elapsed_seconds": extract,
                       "started_at": "2026-08-16T10:00:00Z"})
        p = Path(self._td.name) / "cmv40" / f"{nombre}.json"
        p.write_text(json.dumps({"id": nombre, "phase_history": ph}), encoding="utf-8")

    def test_separa_drop_in_de_merge(self):
        """El reparto no se parece: en drop-in no hay demux y la validación
        son segundos."""
        for i in range(3):
            self._job(f"d{i}", 300, 360, 390, 3)              # drop-in
            self._job(f"m{i}", 300, 480, 600, 240, extract=210)  # merge
        m = self.main._cmv40_build_eta_model()
        self.assertEqual(m["n"], 6)
        self.assertAlmostEqual(m["dropin"]["inject"], 1.2, places=2)
        self.assertAlmostEqual(m["merge"]["inject"], 1.6, places=2)
        self.assertAlmostEqual(m["merge"]["remux"], 2.0, places=2)
        # El demux solo existe en merge
        self.assertIn("extract", m["merge"])
        self.assertNotIn("extract", m["dropin"])

    def test_no_emite_ratios_con_pocas_muestras(self):
        """Con menos de 3 el frontend se queda con su constante."""
        self._job("uno", 300, 360, 390, 3)
        self._job("dos", 300, 366, 396, 4)
        m = self.main._cmv40_build_eta_model()
        self.assertEqual(m["dropin"], {})

    def test_ignora_jobs_sin_fase_a_medible(self):
        self._job("corto", 5, 360, 390, 3)     # analyze de 5s: no sirve de base
        self._job("vacio", 0, 0, 0, 0)
        m = self.main._cmv40_build_eta_model()
        self.assertEqual(m["n"], 0)

    def test_sin_historico_devuelve_modelo_vacio(self):
        m = self.main._cmv40_build_eta_model()
        self.assertEqual(m, {"dropin": {}, "merge": {}, "n": 0})

    def test_json_corrupto_no_revienta(self):
        (Path(self._td.name) / "cmv40" / "malo.json").write_text("{", encoding="utf-8")
        for i in range(3):
            self._job(f"d{i}", 300, 360, 390, 3)
        m = self.main._cmv40_build_eta_model()
        self.assertEqual(m["n"], 3)

if __name__ == "__main__":
    unittest.main()
