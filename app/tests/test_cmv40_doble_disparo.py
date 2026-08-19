"""Dos defensas contra tirar 40 minutos de pipeline a la basura.

Las dos salen del mismo job (John Wick 4, 2026-08-17), que gastó 826 s de
Fase A + 851 s de inject + 755 s de remux y murió en Fase H:

    [13:11:16] ✓ Fase remux completada en 754.7s
    [13:11:19] ✗ Fase validate FALLÓ: Ya existe un MKV con ese nombre:
               John Wick. Chapter 4 (2023) [DV FEL] [CMv4 FULL].mkv
    [13:11:20] ━━━ Inicio fase: validate ━━━      ← otra vez, 1,2 s después
    [13:11:20] ✗ Fase validate FALLÓ: (lo mismo)

  1. El nombre de destino estaba ocupado por el job anterior. Comprobable en
     el segundo cero; se comprobaba al final.
  2. Fase H se disparó dos veces. La siguiente fase la lanzan el orquestador
     del backend Y el frontend, y el guard del frontend mira un snapshot que
     puede ser anterior al error.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

_orig_cwd = None


def setUpModule():
    """main.py monta StaticFiles('static') con ruta relativa: solo importa con
    el cwd en app/, y la suite se lanza desde la raíz del repo."""
    global _orig_cwd
    _orig_cwd = os.getcwd()
    os.chdir(APP_DIR)


def tearDownModule():
    if _orig_cwd:
        os.chdir(_orig_cwd)


def _sesion(**kw):
    from models import CMv40Session
    base = dict(id="s1", source_mkv_path="/x/origen.mkv",
                source_mkv_name="origen.mkv", output_mkv_name="salida.mkv")
    base.update(kw)
    return CMv40Session(**base)


class TestNombreDeSalidaOcupado(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.out = Path(self._td.name)

    async def test_aborta_si_el_fichero_ya_existe(self):
        import phases.cmv40_pipeline as pipe
        (self.out / "salida.mkv").write_bytes(b"0" * 1000)
        with mock.patch.object(pipe, "OUTPUT_DIR", self.out):
            with self.assertRaises(RuntimeError) as ctx:
                await pipe.check_output_name_free(_sesion())
        msg = str(ctx.exception)
        self.assertIn("salida.mkv", msg)
        # El mensaje tiene que decir qué hacer, no solo que falla
        self.assertIn("Renombra", msg)

    async def test_pasa_si_el_nombre_esta_libre(self):
        import phases.cmv40_pipeline as pipe
        dicho = []

        async def _log(m):
            dicho.append(m)

        with mock.patch.object(pipe, "OUTPUT_DIR", self.out):
            await pipe.check_output_name_free(_sesion(), _log)
        self.assertTrue(any("libre" in m for m in dicho), dicho)

    async def test_no_borra_ni_renombra_el_fichero_ajeno(self):
        """Puede ser una versión que el usuario quiere conservar."""
        import phases.cmv40_pipeline as pipe
        f = self.out / "salida.mkv"
        f.write_bytes(b"0" * 1000)
        with mock.patch.object(pipe, "OUTPUT_DIR", self.out):
            with self.assertRaises(RuntimeError):
                await pipe.check_output_name_free(_sesion())
        self.assertTrue(f.exists())
        self.assertEqual(f.stat().st_size, 1000)
        self.assertEqual(list(self.out.iterdir()), [f])

    def test_se_comprueba_al_empezar_la_fase_a(self):
        """Junto al espacio en disco: las dos son "¿podrá terminar esto?"."""
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        i = src.index("await check_disk_space_preflight(session, log_callback)")
        self.assertIn("await check_output_name_free(session, log_callback)",
                      src[i:i + 200])


class TestGuardDeErrorSinResolver(unittest.TestCase):
    def test_lanza_409_con_error_pendiente(self):
        import main
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            main._cmv40_guard_no_pending_error(
                _sesion(error_message="Ya existe un MKV con ese nombre: x.mkv"))
        self.assertEqual(ctx.exception.status_code, 409)
        # El detalle repite el motivo: el frontend lo muestra tal cual
        self.assertIn("Ya existe un MKV", ctx.exception.detail)

    def test_deja_pasar_sin_error(self):
        import main
        main._cmv40_guard_no_pending_error(_sesion())      # no debe lanzar

    def test_todos_los_endpoints_que_arrancan_fase_lo_tienen(self):
        """Si uno se queda fuera, la duplicación vuelve solo por esa ruta."""
        src = (APP_DIR / "main.py").read_text(encoding="utf-8")
        endpoints = ("cmv40_analyze_source", "cmv40_target_path",
                     "cmv40_target_from_drive", "cmv40_target_from_mkv",
                     "cmv40_extract", "cmv40_apply_sync", "cmv40_inject",
                     "cmv40_remux", "cmv40_validate")
        for nombre in endpoints:
            i = src.index(f"async def {nombre}(")
            cabecera = src[i:i + 700]
            self.assertIn("_cmv40_guard_no_pending_error(session)", cabecera,
                          f"{nombre} puede re-dispararse tras un error")

    # El guard debe aplicarse ANTES de lanzar la fase. Se comprobaba aquí
    # midiendo la posición de "asyncio.create_task" dentro del texto del
    # endpoint; al unificar el boilerplate en `_cmv40_launch_phase` ese
    # `create_task` dejó de estar en el endpoint y el test reventó pese a
    # seguir el comportamiento intacto. Ahora lo cubre por ejecución
    # `test_cmv40_endpoints.TestGuardDeErrorPendiente`, que comprueba que con
    # un error pendiente NINGUNA fase llega a lanzarse.

    def test_clear_error_sigue_existiendo_para_reintentar(self):
        """El guard solo es aceptable si hay forma de quitarlo."""
        src = (APP_DIR / "main.py").read_text(encoding="utf-8")
        self.assertIn("async def cmv40_clear_error(", src)


if __name__ == "__main__":
    unittest.main()
