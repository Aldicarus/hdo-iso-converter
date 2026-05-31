"""Tests del cache de análisis de MKV (Tab 2).

Cubre fingerprint triple, roundtrip de cache basic + quality, invalidación
por bump de versión, invalidación por cambio del fichero (mtime/size/SHA),
y persistencia independiente de bloques basic vs quality dentro del mismo
fichero de cache.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_mkv_cache -v

O directamente:
    cd app && python3 -m unittest tests.test_mkv_cache -v
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Permite ejecutar el test sin instalar el paquete
sys.path.insert(0, str(Path(__file__).parent.parent))

# Forzar CONFIG_DIR a un tempdir ANTES de importar storage
_TMP_CONFIG = tempfile.mkdtemp(prefix="mkv_cache_test_")
os.environ["CONFIG_DIR"] = _TMP_CONFIG

from storage import (  # noqa: E402
    compute_mkv_fingerprint,
    read_mkv_cache,
    write_mkv_cache_basic,
    write_mkv_cache_quality,
    invalidate_mkv_cache,
    invalidate_mkv_cache_by_path,
    list_mkv_audit_entries,
    MKV_AUDIT_DIR,
    _mkv_audit_path,
)


def _make_fake_mkv(content: bytes, mtime_offset: float = 0.0) -> Path:
    """Crea un fichero temporal con contenido específico para fingerprint testing."""
    f = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False)
    f.write(content)
    f.close()
    p = Path(f.name)
    if mtime_offset:
        # Settea mtime explícitamente para tests deterministas
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + mtime_offset))
    return p


# ── Fingerprint ──────────────────────────────────────────────────────

class TestFingerprint(unittest.TestCase):

    def test_stable_for_same_file(self):
        """Mismo fichero leído dos veces → mismo fingerprint completo."""
        p = _make_fake_mkv(b"x" * 2_000_000)  # 2 MB
        try:
            fp1 = compute_mkv_fingerprint(str(p))
            fp2 = compute_mkv_fingerprint(str(p))
            self.assertEqual(fp1, fp2)
            self.assertIn("sha256_1mb", fp1)
            self.assertIn("size_bytes", fp1)
            self.assertIn("mtime_ns", fp1)
        finally:
            p.unlink(missing_ok=True)

    def test_changes_with_content_in_first_mb(self):
        """Cambio en el primer 1 MB → SHA distinto."""
        p1 = _make_fake_mkv(b"A" * 2_000_000)
        p2 = _make_fake_mkv(b"B" * 2_000_000)
        try:
            fp1 = compute_mkv_fingerprint(str(p1))
            fp2 = compute_mkv_fingerprint(str(p2))
            self.assertNotEqual(fp1["sha256_1mb"], fp2["sha256_1mb"])
        finally:
            p1.unlink(missing_ok=True); p2.unlink(missing_ok=True)

    def test_changes_with_size(self):
        """Mismo prefijo 1 MB pero size distinto → fingerprint distinto."""
        # Si tu fichero es <= 1MB y el otro es 2MB pero con los mismos
        # primeros bytes, el SHA del primer MB puede coincidir pero el size
        # debe distinguirlos.
        prefix = b"X" * 1_048_576  # exactamente 1 MB
        p1 = _make_fake_mkv(prefix)
        p2 = _make_fake_mkv(prefix + b"Y" * 1000)
        try:
            fp1 = compute_mkv_fingerprint(str(p1))
            fp2 = compute_mkv_fingerprint(str(p2))
            # SHA del primer MB es idéntico
            self.assertEqual(fp1["sha256_1mb"], fp2["sha256_1mb"])
            # Size diferente
            self.assertNotEqual(fp1["size_bytes"], fp2["size_bytes"])
            # Como fingerprints completos, distintos
            self.assertNotEqual(fp1, fp2)
        finally:
            p1.unlink(missing_ok=True); p2.unlink(missing_ok=True)

    def test_changes_with_mtime(self):
        """Mismo contenido y size pero mtime distinto → fingerprint distinto.

        Este es el caso de mkvpropedit editando metadatos in-place donde
        las posiciones del primer MB y el size pueden quedar idénticas
        (caso edge teórico — en la práctica suele cambiar algún byte).
        El mtime garantiza la invalidación."""
        p = _make_fake_mkv(b"Z" * 2_000_000)
        try:
            fp1 = compute_mkv_fingerprint(str(p))
            # Forzar mtime distinto
            st = p.stat()
            os.utime(p, (st.st_atime, st.st_mtime + 10))
            fp2 = compute_mkv_fingerprint(str(p))
            self.assertEqual(fp1["sha256_1mb"], fp2["sha256_1mb"])
            self.assertEqual(fp1["size_bytes"], fp2["size_bytes"])
            self.assertNotEqual(fp1["mtime_ns"], fp2["mtime_ns"])
        finally:
            p.unlink(missing_ok=True)

    def test_missing_file_returns_none(self):
        fp = compute_mkv_fingerprint("/nonexistent/file.mkv")
        self.assertIsNone(fp)


# ── Roundtrip basic ──────────────────────────────────────────────────

class TestCacheBasicRoundtrip(unittest.TestCase):

    def setUp(self):
        self.p = _make_fake_mkv(b"basic" * 200_000)  # ~1 MB
        self.fp = compute_mkv_fingerprint(str(self.p))

    def tearDown(self):
        self.p.unlink(missing_ok=True)
        # Limpiar cache file para que cada test arranque limpio
        if self.fp:
            cache_path = _mkv_audit_path(self.fp["sha256_1mb"])
            cache_path.unlink(missing_ok=True)

    def test_write_then_read_returns_same_payload(self):
        payload = {
            "file_path": "/fake/movie.mkv",
            "file_name": "movie.mkv",
            "duration_seconds": 7200.5,
            "tracks": [{"id": 0, "type": "video"}],
            "analysis_log": ["[12:00:00] [Análisis MKV] Identificando…"],
        }
        write_mkv_cache_basic(self.fp, 1, 1, payload)
        cached = read_mkv_cache(self.fp, 1, 1)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["basic"], payload)
        self.assertIsNone(cached["quality"])

    def test_read_returns_none_when_no_cache(self):
        cached = read_mkv_cache(self.fp, 1, 1)
        self.assertIsNone(cached)

    def test_read_returns_basic_none_when_version_mismatch(self):
        """Bumpear CACHE_VERSION_BASIC → bloque basic se considera obsoleto."""
        payload = {"file_path": "/fake/movie.mkv", "tracks": []}
        write_mkv_cache_basic(self.fp, 1, 1, payload)
        # Leer con versión basic distinta
        cached = read_mkv_cache(self.fp, 2, 1)
        self.assertIsNotNone(cached)  # el fichero existe
        self.assertIsNone(cached["basic"])  # pero el bloque obsoleto

    def test_read_returns_none_when_fingerprint_mismatch(self):
        """Si el MKV cambia (mtime/size/SHA), el cache se descarta."""
        payload = {"file_path": "/fake/movie.mkv", "tracks": []}
        write_mkv_cache_basic(self.fp, 1, 1, payload)
        # Modificar el fingerprint a mano (simular cambio del MKV)
        modified_fp = dict(self.fp)
        modified_fp["mtime_ns"] = self.fp["mtime_ns"] + 1_000_000_000
        cached = read_mkv_cache(modified_fp, 1, 1)
        self.assertIsNone(cached)


# ── Roundtrip quality ─────────────────────────────────────────────────

class TestCacheQualityRoundtrip(unittest.TestCase):

    def setUp(self):
        self.p = _make_fake_mkv(b"quality" * 200_000)
        self.fp = compute_mkv_fingerprint(str(self.p))

    def tearDown(self):
        self.p.unlink(missing_ok=True)
        if self.fp:
            _mkv_audit_path(self.fp["sha256_1mb"]).unlink(missing_ok=True)

    def test_write_quality_preserves_existing_basic(self):
        """Si ya hay basic cacheado, escribir quality NO debe borrar basic."""
        basic_payload = {"file_path": "/fake/movie.mkv", "tracks": [], "duration_seconds": 100}
        quality_payload = {"classification": "real", "tier": "full"}
        write_mkv_cache_basic(self.fp, 1, 1, basic_payload)
        write_mkv_cache_quality(self.fp, 1, 1, quality_payload)
        cached = read_mkv_cache(self.fp, 1, 1)
        self.assertEqual(cached["basic"], basic_payload)
        self.assertEqual(cached["quality"], quality_payload)

    def test_write_basic_preserves_existing_quality(self):
        """Re-escribir basic (re-análisis) NO debe perder quality cacheado."""
        basic_payload_v1 = {"file_path": "/fake/movie.mkv", "tracks": [], "version": "v1"}
        basic_payload_v2 = {"file_path": "/fake/movie.mkv", "tracks": [], "version": "v2"}
        quality_payload = {"classification": "real", "tier": "core"}
        write_mkv_cache_basic(self.fp, 1, 1, basic_payload_v1)
        write_mkv_cache_quality(self.fp, 1, 1, quality_payload)
        # Re-escribir basic — quality debe sobrevivir
        write_mkv_cache_basic(self.fp, 1, 1, basic_payload_v2)
        cached = read_mkv_cache(self.fp, 1, 1)
        self.assertEqual(cached["basic"], basic_payload_v2)
        self.assertEqual(cached["quality"], quality_payload)

    def test_quality_version_bump_invalidates_only_quality(self):
        """Bump CACHE_VERSION_QUALITY → quality obsoleto, basic intacto."""
        basic_payload = {"file_path": "/fake/movie.mkv", "tracks": []}
        quality_payload = {"classification": "real"}
        write_mkv_cache_basic(self.fp, 1, 1, basic_payload)
        write_mkv_cache_quality(self.fp, 1, 1, quality_payload)
        cached = read_mkv_cache(self.fp, 1, 2)  # quality_version bumped
        self.assertEqual(cached["basic"], basic_payload)
        self.assertIsNone(cached["quality"])


# ── Invalidación ─────────────────────────────────────────────────────

class TestInvalidate(unittest.TestCase):

    def setUp(self):
        self.p = _make_fake_mkv(b"inval" * 200_000)
        self.fp = compute_mkv_fingerprint(str(self.p))

    def tearDown(self):
        self.p.unlink(missing_ok=True)
        if self.fp:
            _mkv_audit_path(self.fp["sha256_1mb"]).unlink(missing_ok=True)

    def test_invalidate_existing_cache(self):
        write_mkv_cache_basic(self.fp, 1, 1, {"x": 1})
        cached = read_mkv_cache(self.fp, 1, 1)
        self.assertIsNotNone(cached)
        removed = invalidate_mkv_cache(self.fp["sha256_1mb"])
        self.assertTrue(removed)
        cached = read_mkv_cache(self.fp, 1, 1)
        self.assertIsNone(cached)

    def test_invalidate_nonexistent_returns_false(self):
        removed = invalidate_mkv_cache("nonexistent_sha")
        self.assertFalse(removed)

    def test_invalidate_by_path(self):
        """invalidate_mkv_cache_by_path recalcula fingerprint y borra."""
        write_mkv_cache_basic(self.fp, 1, 1, {"x": 1})
        cached_before = read_mkv_cache(self.fp, 1, 1)
        self.assertIsNotNone(cached_before)
        removed = invalidate_mkv_cache_by_path(str(self.p))
        self.assertTrue(removed)
        cached_after = read_mkv_cache(self.fp, 1, 1)
        self.assertIsNone(cached_after)

    def test_invalidate_by_path_missing_file(self):
        """Si el path no existe, devuelve False sin lanzar."""
        result = invalidate_mkv_cache_by_path("/nonexistent/movie.mkv")
        self.assertFalse(result)


# ── Edge cases ───────────────────────────────────────────────────────

class TestEdgeCases(unittest.TestCase):

    def test_read_with_empty_fingerprint(self):
        """read con fingerprint vacío/None devuelve None sin lanzar."""
        self.assertIsNone(read_mkv_cache({}, 1, 1))
        self.assertIsNone(read_mkv_cache(None, 1, 1))

    def test_read_corrupt_cache_returns_none(self):
        """Si el fichero de cache es JSON inválido, no peta — devuelve None."""
        MKV_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        fake_fp = {"sha256_1mb": "deadbeef", "size_bytes": 100, "mtime_ns": 0}
        cache_path = _mkv_audit_path(fake_fp["sha256_1mb"])
        cache_path.write_text("{ not valid json")
        try:
            result = read_mkv_cache(fake_fp, 1, 1)
            self.assertIsNone(result)
        finally:
            cache_path.unlink(missing_ok=True)


class TestOriginalFilePathPersistence(unittest.TestCase):
    """write_mkv_cache_basic/quality persisten original_file_path top-level,
    necesario para que el scan de huérfanos pueda detectar caches cuyo MKV
    ya no existe en disco."""

    def setUp(self):
        self.p = _make_fake_mkv(b"orig" * 200_000)
        self.fp = compute_mkv_fingerprint(str(self.p))

    def tearDown(self):
        self.p.unlink(missing_ok=True)
        if self.fp:
            _mkv_audit_path(self.fp["sha256_1mb"]).unlink(missing_ok=True)

    def test_basic_persists_original_file_path(self):
        write_mkv_cache_basic(self.fp, 1, 1, {"x": 1}, original_file_path=str(self.p))
        import json as _json
        data = _json.loads(_mkv_audit_path(self.fp["sha256_1mb"]).read_text())
        self.assertEqual(data.get("original_file_path"), str(self.p))

    def test_quality_persists_original_file_path(self):
        write_mkv_cache_quality(self.fp, 1, 1, {"x": 1}, original_file_path=str(self.p))
        import json as _json
        data = _json.loads(_mkv_audit_path(self.fp["sha256_1mb"]).read_text())
        self.assertEqual(data.get("original_file_path"), str(self.p))

    def test_write_basic_preserves_existing_orig_path(self):
        """Si quality se escribió antes con un path, basic re-escrito sin
        path explícito debe preservar el path existente."""
        write_mkv_cache_quality(self.fp, 1, 1, {"x": 1}, original_file_path=str(self.p))
        write_mkv_cache_basic(self.fp, 1, 1, {"y": 2})  # sin original_file_path
        import json as _json
        data = _json.loads(_mkv_audit_path(self.fp["sha256_1mb"]).read_text())
        self.assertEqual(data.get("original_file_path"), str(self.p))


class TestListMkvAuditEntries(unittest.TestCase):
    """list_mkv_audit_entries devuelve metadata top-level + flags por
    cada fichero. Usado por el scan de huérfanos."""

    def setUp(self):
        # Tres MKVs con tres tipos de cache distintos
        self.p_ok = _make_fake_mkv(b"OK" * 200_000)
        self.p_orphan = _make_fake_mkv(b"ORPHAN" * 200_000)
        self.p_basura = _make_fake_mkv(b"BASURA" * 200_000)
        self.fp_ok = compute_mkv_fingerprint(str(self.p_ok))
        self.fp_orphan = compute_mkv_fingerprint(str(self.p_orphan))
        self.fp_basura = compute_mkv_fingerprint(str(self.p_basura))

    def tearDown(self):
        for p in (self.p_ok, self.p_orphan, self.p_basura):
            try: p.unlink(missing_ok=True)
            except Exception: pass
        for fp in (self.fp_ok, self.fp_orphan, self.fp_basura):
            if fp:
                _mkv_audit_path(fp["sha256_1mb"]).unlink(missing_ok=True)

    def test_empty_when_no_caches(self):
        # Borrar cualquier residuo previo
        for entry in list_mkv_audit_entries():
            try: Path(entry["cache_path"]).unlink()
            except Exception: pass
        self.assertEqual(list_mkv_audit_entries(), [])

    def test_lists_basic_only_entry(self):
        write_mkv_cache_basic(self.fp_ok, 1, 1, {"file_path": "ok.mkv"},
                              original_file_path=str(self.p_ok))
        entries = [e for e in list_mkv_audit_entries() if e.get("fingerprint_sha") == self.fp_ok["sha256_1mb"]]
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertTrue(e["basic_present"])
        self.assertFalse(e["quality_present"])
        self.assertEqual(e["original_file_path"], str(self.p_ok))
        self.assertEqual(e["versions"], {"basic": 1})

    def test_lists_quality_summary(self):
        write_mkv_cache_quality(self.fp_basura, 1, 1, {
            "quality_total_frames_rpu": 0,  # ← BASURA
            "quality_classification": "default",
        }, original_file_path=str(self.p_basura))
        entries = [e for e in list_mkv_audit_entries() if e.get("fingerprint_sha") == self.fp_basura["sha256_1mb"]]
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertTrue(e["quality_present"])
        self.assertEqual(e["quality_total_frames"], 0)
        self.assertEqual(e["quality_classification"], "default")

    def test_lists_corrupt_entry(self):
        """Un fichero JSON malformado aparece como corrupt=True sin pet."""
        MKV_AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        fake_path = MKV_AUDIT_DIR / "feedface.json"
        fake_path.write_text("{ not valid json")
        try:
            entries = [e for e in list_mkv_audit_entries() if "feedface" in e["cache_path"]]
            self.assertEqual(len(entries), 1)
            self.assertTrue(entries[0]["corrupt"])
            self.assertIn("error", entries[0])
        finally:
            fake_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
