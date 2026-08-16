"""
Dos escrituras simultáneas al mismo JSON no pueden pisarse.

Bug real (2026-08-16, "La trama fenicia"): `_atomic_write_json` volcaba a un
.tmp de nombre determinista (`X.json.tmp`) y renombraba. Cuando el save
síncrono que marca el arranque de una fase CMv4.0 coincidía con un `_bg_save`
throttled que seguía en vuelo en el thread pool, ambos escribían el MISMO
.tmp: el primero lo renombraba y el segundo moría con

    [Errno 2] No such file or directory: '....json.tmp' -> '....json'

Esa excepción subía desde `save_cmv40_session` —que se llama FUERA del try de
la fase— hasta el `except Exception: pass` del lanzador. Resultado: la Fase A
nunca arrancó, no se escribió ni una línea de log, y la sesión quedó con
`running_phase='analyze_source'` pegado en disco.

El arreglo serializa por fichero destino. Aquí lo forzamos con muchos hilos
escribiendo a la vez.
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

# Import TARDÍO (dentro de los tests): otros módulos de la suite fijan
# os.environ["CONFIG_DIR"] antes de importar `storage`, y este fichero es el
# primero por orden alfabético — importarlo aquí arriba dejaría `storage`
# cacheado en sys.modules con el /config por defecto y rompería a los demás.


class TestAtomicWriteRace(unittest.TestCase):
    def test_escrituras_concurrentes_al_mismo_destino(self):
        from storage import _atomic_write_json
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "session.json"
            errors = []
            start = threading.Event()

            def _writer(i):
                start.wait()
                try:
                    _atomic_write_json(target, json.dumps({"n": i, "pad": "x" * 50_000}))
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=_writer, args=(i,)) for i in range(24)]
            for t in threads:
                t.start()
            start.set()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], f"escrituras concurrentes fallaron: {errors[:3]}")
            # El destino existe y contiene JSON íntegro de UNO de los escritores
            # (nunca una mezcla de dos).
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn(data["n"], range(24))
            # Y no queda ningún .tmp huérfano
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_escrituras_a_destinos_distintos_no_se_bloquean_entre_si(self):
        """El lock es por fichero: dos sesiones distintas siguen en paralelo."""
        from storage import _atomic_write_json
        with tempfile.TemporaryDirectory() as td:
            errors = []

            def _writer(i):
                try:
                    _atomic_write_json(Path(td) / f"s{i}.json", json.dumps({"n": i}))
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=_writer, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            for i in range(8):
                self.assertTrue((Path(td) / f"s{i}.json").exists())


if __name__ == "__main__":
    unittest.main()
