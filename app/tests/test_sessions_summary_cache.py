"""Tests del cache por-fichero del summary de sesiones Tab 1 (sidebar).

Verifica que list_sessions_summary:
  - descarta los campos pesados (output_log, analysis_log, bdinfo_result y el
    output_log de cada ExecutionRecord) pero conserva metadata y status,
  - excluye ficheros de /config que no son sesiones (settings, caches) y JSON
    sin 'id',
  - reutiliza el dict cacheado cuando el fichero no cambió (no relee disco),
  - relee SOLO el fichero modificado y refleja altas/bajas con purga.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_sessions_summary_cache -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Forzar CONFIG_DIR a un tempdir ANTES de importar storage
_TMP_CONFIG = tempfile.mkdtemp(prefix="sessions_summary_test_")
os.environ["CONFIG_DIR"] = _TMP_CONFIG

from storage import (  # noqa: E402
    list_sessions_summary,
    CONFIG_DIR,
    _sessions_summary_by_file,
)


def _write_session(sid: str, *, bump_mtime: float = 0.0, **extra) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "id": sid,
        "mkv_name": f"{sid}.mkv",
        "status": "done",
        "output_log": ["línea de pipeline"] * 300,
        "analysis_log": ["línea de análisis"] * 50,
        "bdinfo_result": {"mkvmerge_raw": {"big": "x" * 5000}},
        "execution_history": [
            {"run_number": 1, "status": "done", "output_log": ["a", "b", "c"]},
        ],
    }
    data.update(extra)
    p = CONFIG_DIR / f"{sid}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    if bump_mtime:
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + bump_mtime))
    return p


def _by_id(lst):
    return {d["id"]: d for d in lst}


class TestSessionsSummaryCache(unittest.TestCase):

    def setUp(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        for p in CONFIG_DIR.glob("*.json"):
            p.unlink()
        _sessions_summary_by_file.clear()

    def test_strips_heavy_fields_keeps_status(self):
        _write_session("peli_a_2024_1")
        res = list_sessions_summary()
        self.assertEqual(len(res), 1)
        s = res[0]
        self.assertEqual(s["output_log"], [])
        self.assertEqual(s["analysis_log"], [])
        self.assertIsNone(s["bdinfo_result"])
        # execution_history se conserva (el sidebar usa [last].status) pero sin
        # el log pesado de cada record.
        self.assertEqual(s["execution_history"][0]["status"], "done")
        self.assertEqual(s["execution_history"][0]["output_log"], [])
        # Metadata intacta.
        self.assertEqual(s["mkv_name"], "peli_a_2024_1.mkv")

    def test_excludes_non_session_files(self):
        _write_session("peli_a_2024_1")
        (CONFIG_DIR / "settings.json").write_text("{}", encoding="utf-8")
        (CONFIG_DIR / "tmdb_cache.json").write_text('{"x": 1}', encoding="utf-8")
        res = list_sessions_summary()
        self.assertEqual(set(_by_id(res).keys()), {"peli_a_2024_1"})

    def test_json_without_id_skipped(self):
        _write_session("peli_a_2024_1")
        # Un JSON arbitrario sin 'id' (p.ej. un cache no listado) no es sesión.
        (CONFIG_DIR / "raro.json").write_text('{"foo": "bar"}', encoding="utf-8")
        res = list_sessions_summary()
        self.assertEqual(set(_by_id(res).keys()), {"peli_a_2024_1"})

    def test_cache_hit_reuses_same_dict(self):
        _write_session("peli_a_2024_1")
        r1 = list_sessions_summary()
        r2 = list_sessions_summary()
        self.assertIs(r1[0], r2[0])

    def test_modification_rereads_only_changed(self):
        _write_session("peli_a_2024_1")
        _write_session("peli_b_2024_2")
        r1 = _by_id(list_sessions_summary())
        _write_session("peli_a_2024_1", bump_mtime=10.0, status="error")
        r2 = _by_id(list_sessions_summary())
        self.assertIsNot(r1["peli_a_2024_1"], r2["peli_a_2024_1"])  # releído
        self.assertEqual(r2["peli_a_2024_1"]["status"], "error")
        self.assertIs(r1["peli_b_2024_2"], r2["peli_b_2024_2"])     # intacto

    def test_delete_purges_cache(self):
        _write_session("peli_a_2024_1")
        _write_session("peli_b_2024_2")
        list_sessions_summary()
        self.assertIn("peli_a_2024_1.json", _sessions_summary_by_file)
        (CONFIG_DIR / "peli_a_2024_1.json").unlink()
        res = list_sessions_summary()
        self.assertEqual(set(_by_id(res).keys()), {"peli_b_2024_2"})
        self.assertNotIn("peli_a_2024_1.json", _sessions_summary_by_file)


if __name__ == "__main__":
    unittest.main()
