"""Tests para la abstracción Source — detección de tipo y validación
path-traversal.

Ejecutar:
    cd app && python3 -m unittest tests.test_source_abstraction -v
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from phases.iso_mount import Source, SourceError, safe_source_path  # noqa: E402


class TestSourceDetectType(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.root = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_detect_iso_file(self):
        iso = self.root / "Movie.iso"
        iso.touch()
        self.assertEqual(Source.detect_type(str(iso)), "iso")

    def test_detect_iso_case_insensitive(self):
        iso = self.root / "Movie.ISO"
        iso.touch()
        self.assertEqual(Source.detect_type(str(iso)), "iso")

    def test_detect_m2ts_file(self):
        m2ts = self.root / "00800.m2ts"
        m2ts.touch()
        self.assertEqual(Source.detect_type(str(m2ts)), "m2ts")

    def test_detect_bdmv_folder(self):
        folder = self.root / "Movie (2024)"
        (folder / "BDMV" / "PLAYLIST").mkdir(parents=True)
        self.assertEqual(Source.detect_type(str(folder)), "bdmv_folder")

    def test_detect_bdmv_folder_lowercase(self):
        # Algunos discos legacy usan bdmv/playlist en minúsculas
        folder = self.root / "OldMovie"
        (folder / "PLAYLIST").mkdir(parents=True)
        self.assertEqual(Source.detect_type(str(folder)), "bdmv_folder")

    def test_raises_when_path_missing(self):
        with self.assertRaises(SourceError) as ctx:
            Source.detect_type(str(self.root / "missing.iso"))
        self.assertIn("no encontrado", str(ctx.exception).lower())

    def test_raises_when_unknown_extension(self):
        bad = self.root / "file.mkv"
        bad.touch()
        with self.assertRaises(SourceError) as ctx:
            Source.detect_type(str(bad))
        self.assertIn("extensión", str(ctx.exception).lower())

    def test_raises_when_folder_without_bdmv(self):
        folder = self.root / "RandomFolder"
        folder.mkdir()
        with self.assertRaises(SourceError) as ctx:
            Source.detect_type(str(folder))
        self.assertIn("BDMV", str(ctx.exception))


class TestSafeSourcePath(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.root = self.tmpdir
        # Crear subestructura: root/Movie.iso, root/sub/Movie2.iso
        (Path(self.root) / "Movie.iso").touch()
        (Path(self.root) / "sub").mkdir()
        (Path(self.root) / "sub" / "Movie2.iso").touch()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_relative_path_resolves_under_root(self):
        result = safe_source_path("Movie.iso", self.root)
        self.assertTrue(result.startswith(str(Path(self.root).resolve())))
        self.assertTrue(result.endswith("Movie.iso"))

    def test_relative_subdirectory(self):
        result = safe_source_path("sub/Movie2.iso", self.root)
        self.assertTrue(result.endswith("Movie2.iso"))

    def test_absolute_path_under_root_ok(self):
        abs_path = str(Path(self.root) / "Movie.iso")
        result = safe_source_path(abs_path, self.root)
        self.assertTrue(result.endswith("Movie.iso"))

    def test_rejects_path_traversal(self):
        # ../../etc/passwd
        with self.assertRaises(SourceError):
            safe_source_path("../../etc/passwd", self.root)

    def test_rejects_absolute_path_outside_root(self):
        with self.assertRaises(SourceError):
            safe_source_path("/etc/passwd", self.root)

    def test_rejects_root_traversal_with_dots(self):
        with self.assertRaises(SourceError):
            safe_source_path("sub/../../etc", self.root)


class TestSourceOpen(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.root = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_open_iso_does_not_mount_until_aenter(self):
        # Sin __aenter__ no se llama a mount_iso (que requeriría
        # privilegios). Source.open solo crea la instancia.
        iso = self.root / "Movie.iso"
        iso.touch()
        src = asyncio.run(Source.open(str(iso)))
        self.assertEqual(src.source_type, "iso")
        self.assertIsNone(src.bdmv_root)
        self.assertFalse(src._mounted)

    def test_open_bdmv_folder_sets_bdmv_root_immediately(self):
        folder = self.root / "Movie (2024)"
        (folder / "BDMV" / "PLAYLIST").mkdir(parents=True)
        src = asyncio.run(Source.open(str(folder)))
        self.assertEqual(src.source_type, "bdmv_folder")
        self.assertEqual(src.bdmv_root, str(folder))

    def test_open_m2ts_single(self):
        m2ts = self.root / "00800.m2ts"
        m2ts.touch()
        src = asyncio.run(Source.open(str(m2ts)))
        self.assertEqual(src.source_type, "m2ts")
        self.assertEqual(src.m2ts_paths, [str(m2ts)])

    def test_open_m2ts_multi(self):
        paths = []
        for i in range(3):
            p = self.root / f"0080{i}.m2ts"
            p.touch()
            paths.append(str(p))
        src = asyncio.run(Source.open(paths[0], m2ts_paths=paths))
        self.assertEqual(src.source_type, "m2ts")
        self.assertEqual(len(src.m2ts_paths), 3)


if __name__ == "__main__":
    unittest.main()
