"""
La Fase H debe ser idempotente: un segundo disparo no puede marcar error.

Bug real (2026-08-14, "Cómo entrenar a tu dragón"): la fase se ejecutó dos
veces con 2 s de diferencia. La primera validó y renombró .mkv.tmp → .mkv; la
segunda no encontró el .tmp y abortó con "MKV final no existe — ejecuta Fase G
primero", dejando el proyecto con error_message pese a tener el MKV correcto
de 76 GB en su sitio. Le había pasado a 2 de 67 jobs.

El lock por sesión de `_run_cmv40_phase` no cubre esto: solo protege mientras
la fase corre, y el disparo tardío llega después del rename.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import phases.cmv40_pipeline as pipeline  # noqa: E402
from models import CMv40Session  # noqa: E402


class TestResolveValidationTarget(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / "output"
        self.wd = self.root / "workdir"
        self.out.mkdir()
        self.wd.mkdir()
        self._orig_out = pipeline.OUTPUT_DIR
        pipeline.OUTPUT_DIR = self.out
        self.session = CMv40Session(
            id="s1", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="Peli (2025) [CMv4 FULL].mkv",
            artifacts_dir=str(self.wd),
        )

    def tearDown(self):
        pipeline.OUTPUT_DIR = self._orig_out
        self._tmp.cleanup()

    def _touch(self, path: Path):
        path.write_bytes(b"0" * 32)
        return path

    def test_caso_normal_usa_el_tmp(self):
        tmp = self._touch(self.out / f"{self.session.output_mkv_name}.tmp")
        path, already = pipeline.resolve_validation_target(self.session, self.wd)
        self.assertEqual(path, tmp)
        self.assertFalse(already)

    def test_proyecto_legacy_usa_output_del_workdir(self):
        legacy = self._touch(self.wd / "output.mkv")
        path, already = pipeline.resolve_validation_target(self.session, self.wd)
        self.assertEqual(path, legacy)
        self.assertFalse(already)

    def test_segundo_disparo_revalida_el_fichero_final(self):
        # El .tmp ya no está: la ejecución anterior lo renombró.
        final = self._touch(self.out / self.session.output_mkv_name)
        path, already = pipeline.resolve_validation_target(self.session, self.wd)
        self.assertEqual(path, final, "debe validar el MKV final, no fallar")
        self.assertTrue(already, "debe señalar que el rename ya estaba hecho")

    def test_el_tmp_tiene_prioridad_sobre_el_final(self):
        # Si existen ambos, el .tmp es el recién producido por Fase G: gana.
        tmp = self._touch(self.out / f"{self.session.output_mkv_name}.tmp")
        self._touch(self.out / self.session.output_mkv_name)
        path, already = pipeline.resolve_validation_target(self.session, self.wd)
        self.assertEqual(path, tmp)
        self.assertFalse(already)

    def test_sin_ningun_fichero_sigue_siendo_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            pipeline.resolve_validation_target(self.session, self.wd)
        msg = str(ctx.exception)
        self.assertIn("ejecuta Fase G primero", msg)
        # El mensaje debe nombrar las tres rutas buscadas, para diagnóstico.
        self.assertIn(".tmp", msg)
        self.assertIn("output.mkv", msg)
        self.assertIn(self.session.output_mkv_name, msg)


if __name__ == "__main__":
    unittest.main()
