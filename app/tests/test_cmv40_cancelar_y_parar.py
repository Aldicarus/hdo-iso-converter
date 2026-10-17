"""Cancelar es cancelar, y parar el contenedor no deja nada a medias.

Fallos de la auditoría completa con Codex del 2026-10-17 (paquetes P1 y P2),
reproducidos antes de arreglarlos:

  · **Cancelar durante el demux de la Fase C acababa como un FALLO.**
    `_run_with_time_estimate` no consultaba el predicado de cancelación tras
    el rc -15 del SIGTERM, así que la fase quedaba «FALLÓ» con su banner rojo
    y el reintento chocaba con el 409 del error pendiente.
  · **Tres runners dejaban su proceso vivo al cancelar la TAREA**: el pipe
    ffmpeg|extract-rpu de la Fase A, `_run_with_time_estimate` y el `export`
    de `rpu_analyze`. Lanzados con `start_new_session`, quedan fuera del
    grupo del padre.
  · **El ticker de `_run_streaming` no se paraba** si cancelaban la tarea
    mientras esperaba salida: un bucle sin fin emitiendo el progreso de una
    fase ya terminada.
  · **Una parada ordenada (`CancelledError`) no entraba en ninguna rama** del
    wrapper de fase: registro en `running` para siempre, historial
    «esperando» y el recovery sin nada que limpiar.
  · **Un fallo no apuntaba lo que había durado**: 40 minutos de fase fallida
    desaparecían del total del historial.
  · **El GET de un proyecto con un dato malformado daba 500**: los `except`
    que debían conservar el texto anterior registraban `session.session_id`,
    un atributo que no existe.
  · **El pre-flight no era cancelable de verdad**: sin el predicado instalado,
    cancelar entre dos subprocesos no hacía nada y con uno vivo acababa en
    error; si el trabajo terminaba bien tras pulsar Cancelar, la Fase A se
    encadenaba igual; y un fallo al guardar el arranque dejaba la actividad
    apuntada para siempre.
  · **Tras Cancelar, la corrutina de la fase sigue un rato** (borra parciales,
    guarda la sesión) con `running_phase` ya limpio: limpiar el proyecto en
    ese hueco hacía `rmtree` por debajo.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_cancelar_y_parar -v
"""
import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
from cmv40_harness import PhaseTestCase, RpuProps, make_session, write_artifacts  # noqa: E402
from phases import cmv40_pipeline as pipeline  # noqa: E402


def _vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        estado = Path(f"/proc/{pid}/stat").read_text().split()[2]
        return estado != "Z"
    except OSError:
        import subprocess
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True)
        return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


async def _esperar_muerte(pid: int) -> bool:
    for _ in range(60):
        if not _vivo(pid):
            return True
        await asyncio.sleep(0.05)
    return False


def _guion_que_duerme(pidf: Path) -> list[str]:
    return [sys.executable, "-c",
            f"import os, time; open({str(pidf)!r}, 'w').write(str(os.getpid())); "
            "time.sleep(30)"]


async def _pid_de(pidf: Path) -> int:
    for _ in range(200):
        if pidf.exists() and pidf.read_text():
            return int(pidf.read_text())
        await asyncio.sleep(0.02)
    raise AssertionError("el proceso no llegó a arrancar")


SRC = RpuProps(profile=7, el_type="FEL", cm_version="v2.9", frames=1000)
TGT = RpuProps(profile=7, el_type="FEL", cm_version="v4.0", frames=1000, has_l8=True)


class TestCancelarElDemuxEsCancelar(PhaseTestCase):

    async def test_el_sigterm_de_cancelar_no_es_un_fallo(self):
        write_artifacts(self.wd, "source.hevc", props=SRC)
        pipeline._marcar_source_hevc(self.wd, True)     # extracción terminada
        write_artifacts(self.wd, "RPU_source.bin", props=SRC)
        write_artifacts(self.wd, "RPU_target.bin", props=TGT)
        self.tb.define_rpu("RPU_source.bin", **SRC.as_dict())
        self.tb.define_rpu("RPU_target.bin", **TGT.as_dict())
        self.tb.fail("dovi_tool", "demux", senal=signal.SIGTERM)
        s = make_session(self.wd, target_type="generic", target_trust_ok=False)
        llamadas = {"n": 0}

        def pred():
            llamadas["n"] += 1
            return llamadas["n"] >= 2       # el usuario cancela con el demux ya en marcha
        pipeline.set_cancel_check(pred)
        self.addCleanup(pipeline.set_cancel_check, None)
        with self.assertRaises(pipeline.CMv40Cancelled):
            await pipeline.run_phase_c_extract(s, self.log)
        self.assertFalse((self.wd / "BL.hevc").exists(), "quedó un BL a medias")


class TestLaTareaCanceladaNoDejaProcesos(PhaseTestCase):

    async def test_run_with_time_estimate(self):
        pidf = self.tmp / "pid"
        tarea = asyncio.create_task(pipeline._run_with_time_estimate(
            _guion_que_duerme(pidf), estimated_s=60, timeout=600))
        pid = await _pid_de(pidf)
        tarea.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tarea
        self.assertTrue(await _esperar_muerte(pid),
                        "el demux sigue escribiendo tras cancelar la tarea")

    async def test_el_pipe_de_la_fase_a_mata_los_dos_extremos(self):
        self.tb.define_media("source.mkv", duration=600.0)
        write_artifacts(self.tmp, "source.mkv", props=SRC)
        self.tb.retrasar_cuando_arg("ffmpeg", "pipe:1", 30)
        procs = []
        tarea = asyncio.create_task(pipeline._ffmpeg_extract_rpu_piped(
            str(self.tmp / "source.mkv"), self.wd / "RPU.bin",
            hevc_out=None, duration=600.0, proc_callback=procs.append))
        for _ in range(200):
            if len(procs) >= 2:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(len(procs), 2, "el pipe no llegó a arrancar")
        await asyncio.sleep(0.2)
        tarea.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tarea
        for p in procs:
            self.assertTrue(await _esperar_muerte(p.pid),
                            f"{p.pid} sigue vivo tras cancelar la tarea")

    async def test_el_export_de_rpu_analyze(self):
        from phases import rpu_analyze
        write_artifacts(self.wd, "RPU.bin", props=TGT)
        self.tb.retrasar_cuando_arg("dovi_tool", "export", 30)
        procs = []
        tarea = asyncio.create_task(rpu_analyze._run_export(
            self.wd / "RPU.bin", self.wd / "out.json", timeout=600,
            register_proc=procs.append))
        for _ in range(200):
            if procs:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(procs, "el export no llegó a arrancar")
        await asyncio.sleep(0.3)
        tarea.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tarea
        self.assertTrue(await _esperar_muerte(procs[0].pid),
                        "el export sigue corriendo tras cancelar la tarea")


class TestElPipeRespetaCancelar(PhaseTestCase):

    def setUp(self):
        super().setUp()
        self.tb.define_media("source.mkv", duration=600.0)
        write_artifacts(self.tmp, "source.mkv", props=SRC)
        self.addCleanup(pipeline.set_cancel_check, None)

    async def test_ya_cancelado_no_arranca_nada(self):
        pipeline.set_cancel_check(lambda: True)
        with self.assertRaises(pipeline.CMv40Cancelled):
            await pipeline._ffmpeg_extract_rpu_piped(
                str(self.tmp / "source.mkv"), self.wd / "RPU.bin",
                hevc_out=None, duration=600.0)
        self.assertFalse(self.tb.ran("ffmpeg"), "arrancó el pipe estando cancelado")

    async def test_el_sigterm_de_cancelar_no_es_un_fallo_del_pipe(self):
        # Con un fallo del pipe, el caller rehace por el camino en dos pasos;
        # con una cancelación tiene que parar.
        estado = {"cancelado": False}
        pipeline.set_cancel_check(lambda: estado["cancelado"])
        self.tb.fail("dovi_tool", "extract-rpu", senal=signal.SIGTERM)

        async def _cancelar_pronto():
            await asyncio.sleep(0.05)
            estado["cancelado"] = True
        asyncio.create_task(_cancelar_pronto())
        self.tb.retrasar_cuando_arg("ffmpeg", "pipe:1", 0.3)
        with self.assertRaises(pipeline.CMv40Cancelled):
            await pipeline._ffmpeg_extract_rpu_piped(
                str(self.tmp / "source.mkv"), self.wd / "RPU.bin",
                hevc_out=None, duration=600.0)


class TestElTickerNoSobreviveALaTarea(unittest.IsolatedAsyncioTestCase):

    async def test_cancelar_run_streaming_para_el_ticker(self):
        d = Path(tempfile.mkdtemp())
        pidf = d / "pid"
        tarea = asyncio.create_task(pipeline._run_streaming(
            _guion_que_duerme(pidf),
            progress_ctx={"time_estimate_s": 100.0, "label": "x"}))
        await _pid_de(pidf)
        tarea.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tarea
        await asyncio.sleep(0.2)
        vivos = [t for t in asyncio.all_tasks()
                 if not t.done() and getattr(t.get_coro(), "__name__", "") == "_ticker"]
        for t in vivos:
            t.cancel()
        self.assertEqual(vivos, [], "el ticker sigue emitiendo tras cancelar la fase")


class _ConFasesDeVerdad(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.ejecutar_fases_de_verdad()
        self.despachos = []

        async def _no_despachar(session_id):
            self.despachos.append(session_id)
        orig = self.cmv40._cmv40_dispatch_next_phase
        self.cmv40._cmv40_dispatch_next_phase = _no_despachar
        self.addCleanup(setattr, self.cmv40, "_cmv40_dispatch_next_phase", orig)

    def _historial(self):
        f = self.config_dir / "historial.jsonl"
        if not f.exists():
            return []
        return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


class TestUnaParadaOrdenadaNoDejaLaFaseColgada(_ConFasesDeVerdad):

    def test_cancelled_error_deja_la_fase_como_el_recovery(self):
        sid = self.crear_sesion(phase="injected")
        r = self.cmv40

        async def coro(log_cb, proc_cb):
            await asyncio.sleep(30)

        async def main():
            t = asyncio.create_task(r._run_cmv40_phase(
                self.leer_sesion(sid), "remux", coro, "remuxed"))
            await asyncio.sleep(0.2)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        asyncio.run(main())
        s = self.leer_sesion(sid)
        self.assertIsNone(s.running_phase)
        self.assertEqual(s.phase, "injected")
        self.assertEqual(s.phase_history[-1].status, "error",
                         "el registro se queda en `running` para siempre")
        self.assertTrue(s.error_message, "sin error, el historial lo anota como «esperando»")
        estados = [h.get("estado") for h in self._historial() if h.get("id") == sid]
        self.assertNotIn("esperando", estados)
        self.assertIn("error", estados)


class TestUnFalloApuntaLoQueDuro(_ConFasesDeVerdad):

    def test_elapsed_seconds_en_el_fallo(self):
        sid = self.crear_sesion(phase="injected")
        r = self.cmv40

        async def coro(log_cb, proc_cb):
            await asyncio.sleep(0.15)
            raise RuntimeError("falla")
        asyncio.run(r._run_cmv40_phase(self.leer_sesion(sid), "remux", coro, "remuxed"))
        rec = self.leer_sesion(sid).phase_history[-1]
        self.assertEqual(rec.status, "error")
        self.assertIsNotNone(rec.elapsed_seconds, "el tiempo de la fase fallida se pierde")
        self.assertGreaterEqual(rec.elapsed_seconds, 0.1)


class TestElGetNoRevientaConUnDatoMalo(ApiTestCase):

    def test_blockers_que_no_es_una_lista(self):
        sid = self.crear_sesion(
            phase="created",
            sheet_recommendation={"status": "recommended", "blockers": 123})
        r = self.client.get(f"/api/cmv40/{sid}?include_log=false")
        self.assertEqual(r.status_code, 200, r.text[:300])


class TestElPreflightSeCancelaDeVerdad(_ConFasesDeVerdad):

    def _correr(self, sid):
        r = self.cmv40
        s = self.leer_sesion(sid)
        lock = r._get_cmv40_phase_lock(sid)
        pipeline.set_cancel_check(None)
        asyncio.run(r._cmv40_correr_preflight(s, lock, "path", rpu_path="/x.bin"))

    def _parchear(self, nombre, fn):
        orig = getattr(pipeline, nombre)
        setattr(pipeline, nombre, fn)
        self.addCleanup(setattr, pipeline, nombre, orig)

    def test_cancelar_entre_dos_pasos_para_y_no_es_un_error(self):
        sid = self.crear_sesion(phase="created", auto_pipeline=True,
                                pending_target_kind="path",
                                pending_target_rpu_path="/x.bin")
        r = self.cmv40
        siguiente = []

        async def _source(session, **k):
            r._cmv40_cancel_flags[sid] = True       # llega el Cancelar
            pipeline.raise_if_cancelled()           # el siguiente subproceso

        async def _target(session, rpu_path, log_cb):
            siguiente.append("target")
        self._parchear("preflight_source", _source)
        self._parchear("preflight_target_path", _target)
        self._correr(sid)
        s = self.leer_sesion(sid)
        self.assertEqual(siguiente, [], "el pre-flight siguió tras Cancelar")
        self.assertFalse(s.error_message, "cancelar acabó como un error del pre-flight")
        self.assertFalse(s.target_preflight_ok)
        self.assertEqual(self.despachos, [])

    def test_si_acaba_bien_tras_cancelar_no_encadena_la_fase_a(self):
        sid = self.crear_sesion(phase="created", auto_pipeline=True,
                                pending_target_kind="path",
                                pending_target_rpu_path="/x.bin")
        r = self.cmv40

        async def _nada(*a, **k):
            return None

        async def _analizar(session, log_cb):
            r._cmv40_cancel_flags[sid] = True       # Cancelar, con el trabajo ya hecho
            return True
        self._parchear("preflight_source", _nada)
        self._parchear("preflight_target_path", _nada)
        orig = r._cmv40_preflight_analyze_target
        r._cmv40_preflight_analyze_target = _analizar
        self.addCleanup(setattr, r, "_cmv40_preflight_analyze_target", orig)

        async def main():
            await r._cmv40_correr_preflight(
                self.leer_sesion(sid), r._get_cmv40_phase_lock(sid), "path",
                rpu_path="/x.bin")
            await asyncio.sleep(0.05)
        asyncio.run(main())
        self.assertEqual(self.despachos, [], "encadenó la Fase A tras pulsar Cancelar")

    def test_un_fallo_al_guardar_el_arranque_no_deja_actividad_fantasma(self):
        import workload
        sid = self.crear_sesion(phase="created", pending_target_kind="path",
                                pending_target_rpu_path="/x.bin")
        r = self.cmv40

        async def _falla(*a, **k):
            raise OSError("No space left on device")
        orig = r._save_cmv40_session_async
        r._save_cmv40_session_async = _falla
        self.addCleanup(setattr, r, "_save_cmv40_session_async", orig)
        try:
            self._correr(sid)
        except Exception:
            pass
        self.addCleanup(r._cmv40_activas.pop, sid, None)
        self.assertNotIn(sid, r._cmv40_activas, "el proyecto sigue «en curso» para siempre")
        self.assertNotIn(sid, [t.clave for t in workload.en_curso()],
                         "el hueco de trabajo se queda ocupado")


class TestNoSeLimpiaPorDebajoDeUnaFaseQueTermina(_ConFasesDeVerdad):

    def test_tras_cancelar_con_la_fase_aun_viva(self):
        sid = self.crear_sesion(phase="injected")
        r = self.cmv40
        resultado = {}

        async def main():
            soltar = asyncio.Event()

            async def coro(log_cb, proc_cb):
                await soltar.wait()

            t = asyncio.create_task(r._run_cmv40_phase(
                self.leer_sesion(sid), "remux", coro, "remuxed"))
            await asyncio.sleep(0.1)
            await r.cmv40_cancel(sid)
            try:
                await r.cmv40_cleanup(sid)
                resultado["cleanup"] = 200
            except Exception as e:      # HTTPException
                resultado["cleanup"] = getattr(e, "status_code", repr(e))
            soltar.set()
            await t
        asyncio.run(main())
        self.assertEqual(resultado["cleanup"], 409,
                         "se limpió el workdir con la fase todavía terminando")

    def test_con_el_lock_tomado_y_sin_copia_viva(self):
        # El hueco final: la fase ya soltó su copia viva y está guardando.
        sid = self.crear_sesion(phase="injected")
        r = self.cmv40
        resultado = {}

        async def main():
            async with r._get_cmv40_phase_lock(sid):
                try:
                    await r.cmv40_cleanup(sid)
                    resultado["cleanup"] = 200
                except Exception as e:
                    resultado["cleanup"] = getattr(e, "status_code", repr(e))
        asyncio.run(main())
        self.assertEqual(resultado["cleanup"], 409)


if __name__ == "__main__":
    unittest.main()
