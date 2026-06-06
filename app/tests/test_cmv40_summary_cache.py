"""Tests del cache por-fichero del summary CMv4.0 (sidebar de Tab 3).

Verifica que list_cmv40_sessions_summary:
  - descarta output_log / phase_history (payload ligero),
  - reutiliza el dict cacheado cuando el fichero no cambió (no relee disco),
  - relee SOLO el fichero modificado y conserva los demás,
  - refleja altas y bajas de sesiones y purga el cache interno.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_summary_cache -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Forzar CONFIG_DIR a un tempdir ANTES de importar storage
_TMP_CONFIG = tempfile.mkdtemp(prefix="cmv40_summary_test_")
os.environ["CONFIG_DIR"] = _TMP_CONFIG

from storage import (  # noqa: E402
    list_cmv40_sessions_summary,
    CMV40_DIR,
    _cmv40_summary_by_file,
)


def _write_session(sid: str, *, bump_mtime: float = 0.0, **extra) -> Path:
    CMV40_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "id": sid,
        "output_log": ["línea de log"] * 200,
        "phase_history": [{"phase": "x", "status": "done"}],
        "phase": "created",
        "running_phase": None,
    }
    data.update(extra)
    p = CMV40_DIR / f"{sid}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    if bump_mtime:
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + bump_mtime))
    return p


def _by_id(lst):
    return {d["id"]: d for d in lst}


class TestCmv40SummaryCache(unittest.TestCase):

    def setUp(self):
        if CMV40_DIR.exists():
            for p in CMV40_DIR.glob("*.json"):
                p.unlink()
        _cmv40_summary_by_file.clear()

    def test_strips_heavy_fields(self):
        _write_session("cmv40_a")
        res = list_cmv40_sessions_summary()
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["output_log"], [])
        self.assertEqual(res[0]["phase_history"], [])
        self.assertEqual(res[0]["phase"], "created")

    def test_cache_hit_reuses_same_dict(self):
        _write_session("cmv40_a")
        r1 = list_cmv40_sessions_summary()
        r2 = list_cmv40_sessions_summary()
        # Sin cambios en disco → el dict cacheado se reutiliza (no se relee).
        self.assertIs(r1[0], r2[0])

    def test_modification_rereads_only_changed(self):
        _write_session("cmv40_a")
        _write_session("cmv40_b")
        r1 = _by_id(list_cmv40_sessions_summary())
        # Modifica solo 'a' (contenido distinto + mtime bump determinista).
        _write_session("cmv40_a", bump_mtime=10.0, phase="done")
        r2 = _by_id(list_cmv40_sessions_summary())
        self.assertIsNot(r1["cmv40_a"], r2["cmv40_a"])  # 'a' releído
        self.assertEqual(r2["cmv40_a"]["phase"], "done")
        self.assertIs(r1["cmv40_b"], r2["cmv40_b"])     # 'b' intacto, cacheado

    def test_new_file_appears(self):
        _write_session("cmv40_a")
        self.assertEqual(len(list_cmv40_sessions_summary()), 1)
        _write_session("cmv40_b")
        res = list_cmv40_sessions_summary()
        self.assertEqual(len(res), 2)
        self.assertIn("cmv40_b", _by_id(res))

    def test_delete_purges_cache(self):
        _write_session("cmv40_a")
        _write_session("cmv40_b")
        list_cmv40_sessions_summary()
        self.assertIn("cmv40_a.json", _cmv40_summary_by_file)
        (CMV40_DIR / "cmv40_a.json").unlink()
        res = list_cmv40_sessions_summary()
        self.assertEqual(len(res), 1)
        # El cache interno purga la entrada del fichero borrado.
        self.assertNotIn("cmv40_a.json", _cmv40_summary_by_file)

    def test_corrupt_file_skipped(self):
        _write_session("cmv40_ok")
        CMV40_DIR.mkdir(parents=True, exist_ok=True)
        (CMV40_DIR / "cmv40_bad.json").write_text("{not valid json", encoding="utf-8")
        res = list_cmv40_sessions_summary()
        # La corrupta se omite; la buena sigue.
        self.assertEqual(_by_id(res).keys(), {"cmv40_ok"})


if __name__ == "__main__":
    unittest.main()
