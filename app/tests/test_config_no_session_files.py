"""
Los caches de /config no son sesiones, y no deben ensuciar el arranque.

`/config` mezcla las sesiones de Tab 1 con varios ficheros de cache y
settings. `list_sessions` los filtraba con una lista negra inline, mientras
que `list_sessions_summary` usaba la constante `_NON_SESSION_FILES` **y**
un guard por presencia de `id`. Las dos listas se desincronizaron:
`update_check_cache.json` se añadió a la constante pero no a la copia, así
que en cada arranque el log soltaba dos errores de validación de Pydantic
sobre un fichero que nunca fue una sesión.

Una lista negra siempre acaba quedándose corta —es la tercera vez—, así
que lo que se fija aquí es sobre todo el guard: un Session tiene `id`; lo
que no lo tenga se omite en silencio.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import storage  # noqa: E402


class TestFicherosDeConfigQueNoSonSesiones(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cfg_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = storage.CONFIG_DIR
        storage.CONFIG_DIR = self.tmp
        self.addCleanup(lambda: setattr(storage, "CONFIG_DIR", self._orig))
        storage._sessions_summary_by_file.clear()

    def escribir(self, nombre: str, data: dict):
        (self.tmp / nombre).write_text(json.dumps(data), encoding="utf-8")

    def sesion_valida(self, sid: str = "Peli_2024_1700000000"):
        self.escribir(f"{sid}.json", {
            "id": sid, "iso_path": "/mnt/isos/x.iso", "iso_name": "x.iso",
            "title": "Peli", "year": 2024,
        })
        return sid

    # ── el caso que motivó esto ──────────────────────────────────────

    def test_update_check_cache_no_se_toma_por_una_sesion(self):
        self.sesion_valida()
        self.escribir("update_check_cache.json", {
            "fetched_at": 1787042467, "latest": "v2.8.1",
            "checked_at": "2026-08-18T08:41:07.519201+00:00",
        })
        with self.assertLogs(storage.logger, level="WARNING") as cm:
            storage.logger.warning("centinela")   # que assertLogs no falle si no hay más
            sesiones = storage.list_sessions()
        self.assertEqual([s.id for s in sesiones], ["Peli_2024_1700000000"])
        ruido = [m for m in cm.output if "update_check_cache" in m]
        self.assertEqual(ruido, [], "un cache conocido no debe generar warnings")

    def test_los_dos_listados_filtran_lo_mismo(self):
        # La desincronización entre la lista inline y la constante es
        # justo lo que produjo el bug.
        self.sesion_valida()
        for nombre in storage._NON_SESSION_FILES:
            self.escribir(nombre, {"cualquier": "cosa"})
        completas = {s.id for s in storage.list_sessions()}
        resumidas = {s["id"] for s in storage.list_sessions_summary()}
        self.assertEqual(completas, resumidas)
        self.assertEqual(completas, {"Peli_2024_1700000000"})

    # ── el guard, que es lo que aguanta cuando la lista se queda corta ──

    def test_un_cache_nuevo_sin_id_se_omite_sin_ruido(self):
        # Simula el siguiente cache que alguien añada y olvide listar.
        self.sesion_valida()
        self.escribir("un_cache_futuro.json", {"fetched_at": 1, "datos": [1, 2]})
        with self.assertLogs(storage.logger, level="WARNING") as cm:
            storage.logger.warning("centinela")
            sesiones = storage.list_sessions()
        self.assertEqual(len(sesiones), 1)
        self.assertEqual([m for m in cm.output if "un_cache_futuro" in m], [])

    def test_una_lista_json_no_revienta_el_listado(self):
        self.sesion_valida()
        self.escribir("una_lista.json", [])  # type: ignore[arg-type]
        self.assertEqual(len(storage.list_sessions()), 1)

    # ── lo que SÍ debe seguir avisando ───────────────────────────────

    def test_una_sesion_de_verdad_corrupta_sigue_avisando(self):
        # Con 'id' pero inválida: eso sí es una sesión rota y hay que verlo.
        self.sesion_valida()
        self.escribir("rota.json", {"id": "rota", "year": "no-es-un-año"})
        with self.assertLogs(storage.logger, level="WARNING") as cm:
            sesiones = storage.list_sessions()
        self.assertEqual(len(sesiones), 1)
        self.assertTrue([m for m in cm.output if "rota.json" in m],
                        "una sesión inválida de verdad debe seguir reportándose")

    def test_json_ilegible_sigue_avisando(self):
        self.sesion_valida()
        (self.tmp / "basura.json").write_text("{no es json", encoding="utf-8")
        with self.assertLogs(storage.logger, level="WARNING") as cm:
            sesiones = storage.list_sessions()
        self.assertEqual(len(sesiones), 1)
        self.assertTrue([m for m in cm.output if "basura.json" in m])


if __name__ == "__main__":
    unittest.main()
