"""Cancelar una fase no deja procesos huérfanos.

Dos agujeros que salieron de verificar una sospecha de la auditoría del
2026-10-17:

  · **`_run` no mataba su subproceso al cancelarse la tarea.** El
    `CancelledError` salía del `await` y el proceso —lanzado con
    `start_new_session`, así que fuera del grupo del padre— seguía corriendo.
  · **La Fase G solo cancelaba el `extract-rpu` adelantado si mkvmerge salía
    con error.** Si el mux LANZABA —el usuario cancela, un timeout—, la tarea
    seguía viva leyendo el HEVC completo minutos después de terminar la fase,
    con el hueco de trabajo ya liberado. Y como no se registra en los
    procesos activos, el botón Cancelar tampoco la alcanzaba.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_cancelar_no_deja_procesos -v
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import PhaseTestCase, RpuProps, make_session, write_artifacts  # noqa: E402
from phases import cmv40_pipeline as pipeline  # noqa: E402


def _vivo(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # Un zombi aún responde a kill 0 hasta que lo recogen: se mira su estado.
    try:
        estado = Path(f"/proc/{pid}/stat").read_text().split()[2]
        return estado != "Z"
    except OSError:
        import subprocess
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True)
        return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


class TestRunMataSuProcesoAlCancelarse(unittest.IsolatedAsyncioTestCase):

    async def test_el_proceso_no_sobrevive_a_la_tarea(self):
        d = tempfile.mkdtemp()
        pidf = Path(d) / "pid"
        guion = (f"import os, time; open({str(pidf)!r}, 'w').write(str(os.getpid())); "
                 "time.sleep(30)")
        tarea = asyncio.create_task(pipeline._run([sys.executable, "-c", guion], timeout=60))
        for _ in range(100):
            if pidf.exists() and pidf.read_text():
                break
            await asyncio.sleep(0.05)
        pid = int(pidf.read_text())
        tarea.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tarea
        for _ in range(40):
            if not _vivo(pid):
                break
            await asyncio.sleep(0.05)
        self.assertFalse(_vivo(pid), "el subproceso sigue vivo tras cancelar la tarea")


FRAMES = 1000
INJ = RpuProps(profile=7, el_type="FEL", cm_version="v4.0", frames=FRAMES, has_l8=True)


class TestLaFaseGNoDejaElAdelantoVivo(PhaseTestCase):

    async def test_si_el_mux_lanza_el_adelanto_se_cancela_y_se_espera(self):
        from phases.cmv40_pipeline import CMv40Cancelled, run_phase_g_remux
        for n in ("BL.hevc", "EL_injected.hevc", "RPU_target.bin"):
            write_artifacts(self.wd, n, props=INJ)
        write_artifacts(self.wd, "source.mkv")
        s = make_session(self.wd, source_workflow="p7_fel", target_type="generic",
                         target_trust_ok=False)
        estado = {"empezado": False, "cancelado": False, "terminado": False}

        async def _adelanto(*a, **kw):
            estado["empezado"] = True
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                estado["cancelado"] = True
                raise
            finally:
                estado["terminado"] = True
            return True

        real = pipeline._run_streaming

        async def _mux_que_cancelan(cmd, *a, **kw):
            if cmd and cmd[0] == pipeline.MKVMERGE_BIN:
                # El mux corre un rato —el adelanto ya está trabajando— y
                # entonces lo cancelan.
                await asyncio.sleep(0.05)
                raise CMv40Cancelled()
            return await real(cmd, *a, **kw)

        with mock.patch.object(pipeline, "_prewarm_validation_rpu", _adelanto), \
             mock.patch.object(pipeline, "_run_streaming", _mux_que_cancelan):
            with self.assertRaises(CMv40Cancelled):
                await run_phase_g_remux(s, self.log)
        self.assertTrue(estado["empezado"], "el escenario no ha lanzado el adelanto")
        self.assertTrue(estado["cancelado"], "el extract-rpu adelantado sigue en marcha")
        self.assertTrue(estado["terminado"], "se ha cancelado pero nadie lo ha esperado")


if __name__ == "__main__":
    unittest.main()
