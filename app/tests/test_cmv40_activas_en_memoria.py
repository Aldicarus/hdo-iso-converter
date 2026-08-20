"""El punto verde del tab no toca el disco, y `cleanup-preview` tampoco lo bloquea.

`/api/cmv40-active` lo consulta el frontend **cada 5 s**, para siempre. Empezó
pidiendo `GET /api/cmv40` (569 KB y 193 ms con 88 proyectos), pasó al summary
cacheado —que sigue haciendo un `glob` más un `stat` por sesión: ~88 syscalls
cada 5 s, 1,5 millones al día— y ahora sale de memoria.

Se puede porque este proceso es el único que arranca fases y el arranque limpia
los `running_phase` huérfanos del disco. El registro lo mantienen
`_cmv40_marcar_activa` / `_cmv40_marcar_libre`, que son los únicos sitios que
tocan `session.running_phase`: si alguien vuelve a asignarlo a mano, el
registro se queda con un fantasma y el punto verde no se apaga nunca. Eso es lo
que guarda `TestNadieAsignaRunningPhaseAMano`.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_activas_en_memoria -v
"""
import re
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402


class TestPuntoVerdeSinDisco(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.cmv40._cmv40_activas.clear()
        self.addCleanup(self.cmv40._cmv40_activas.clear)

    def test_sin_nada_en_marcha_no_hay_punto(self):
        self.crear_sesion(phase="done")
        d = self.client.get("/api/cmv40-active").json()
        self.assertFalse(d["active"])
        self.assertEqual(d["ids"], [])

    def test_una_fase_en_marcha_enciende_el_punto(self):
        sid = self.crear_sesion(phase="extracted")
        s = self.leer_sesion(sid)
        self.cmv40._cmv40_marcar_activa(s, "inject")
        d = self.client.get("/api/cmv40-active").json()
        self.assertTrue(d["active"])
        self.assertEqual(d["ids"], [sid])

    def test_al_liberar_se_apaga(self):
        sid = self.crear_sesion(phase="extracted")
        s = self.leer_sesion(sid)
        self.cmv40._cmv40_marcar_activa(s, "inject")
        self.cmv40._cmv40_marcar_libre(s)
        self.assertFalse(self.client.get("/api/cmv40-active").json()["active"])

    def test_no_se_lee_ni_un_fichero(self):
        """Lo que hace que el poll de cada 5 s sea gratis."""
        import storage
        llamadas = []
        original = storage.list_cmv40_sessions_summary

        def _espia():
            llamadas.append(1)
            return original()

        storage.list_cmv40_sessions_summary = _espia
        self.addCleanup(setattr, storage, "list_cmv40_sessions_summary", original)
        self.crear_sesion(phase="done")
        self.client.get("/api/cmv40-active")
        self.assertEqual(llamadas, [], "el endpoint sigue listando sesiones del disco")

    def test_un_running_phase_fantasma_en_disco_no_enciende_el_punto(self):
        """Tras un kill -9 el JSON queda con `running_phase` puesto. La memoria
        manda: si nadie lo arrancó en ESTE proceso, no hay nada corriendo."""
        self.crear_sesion(phase="extracted", running_phase="inject")
        self.assertFalse(self.client.get("/api/cmv40-active").json()["active"])


class TestCleanupPreview(ApiTestCase):

    def setUp(self):
        super().setUp()
        self.cmv40._cmv40_activas.clear()
        self.addCleanup(self.cmv40._cmv40_activas.clear)

    def test_lista_los_proyectos_con_su_tamano(self):
        sid = self.crear_sesion(phase="done", output_mkv_name="Peli (2024).mkv")
        wd = Path(self.leer_sesion(sid).artifacts_dir)
        (wd / "BL.hevc").write_bytes(b"x" * 500)
        (wd / "EL.hevc").write_bytes(b"x" * 300)
        d = self.client.get("/api/cmv40/cleanup/preview").json()
        item = next(i for i in d["items"] if i["id"] == sid)
        self.assertEqual(item["size_bytes"], 800)
        self.assertEqual(item["files_count"], 2)
        self.assertEqual(item["state"], "done")
        self.assertTrue(item["safe_to_delete"])
        self.assertEqual(item["title"], "Peli (2024).mkv")

    def test_una_fase_en_marcha_lo_marca_como_no_borrable(self):
        sid = self.crear_sesion(phase="extracted")
        self.cmv40._cmv40_marcar_activa(self.leer_sesion(sid), "extract")
        d = self.client.get("/api/cmv40/cleanup/preview").json()
        item = next(i for i in d["items"] if i["id"] == sid)
        self.assertEqual(item["state"], "running")
        self.assertFalse(item["safe_to_delete"])
        self.assertEqual(item["running_phase"], "extract")

    def test_no_carga_las_sesiones_completas(self):
        """`list_cmv40_sessions()` trae cada JSON entero: con 88 proyectos son
        decenas de MB de log y, en un caso real, 3.914 `L2Combo` en una sola
        sesión. De todo eso aquí se usan nueve campos."""
        import storage
        llamadas = []
        original = storage.list_cmv40_sessions

        def _espia():
            llamadas.append(1)
            return original()

        storage.list_cmv40_sessions = _espia
        self.addCleanup(setattr, storage, "list_cmv40_sessions", original)
        self.crear_sesion(phase="done")
        self.client.get("/api/cmv40/cleanup/preview")
        self.assertEqual(llamadas, [], "sigue cargando las sesiones completas")

    def test_estados_archivado_y_error(self):
        a = self.crear_sesion(sid="cmv40_arch_1", phase="done", archived=True)
        e = self.crear_sesion(sid="cmv40_err_1", phase="extracted",
                              error_message="dovi_tool falló feo")
        por_id = {i["id"]: i for i in
                  self.client.get("/api/cmv40/cleanup/preview").json()["items"]}
        self.assertEqual(por_id[a]["state"], "archived")
        self.assertFalse(por_id[a]["safe_to_delete"])
        self.assertEqual(por_id[e]["state"], "error")
        self.assertIn("dovi_tool falló feo", por_id[e]["reason"])


class TestNadieAsignaRunningPhaseAMano(unittest.TestCase):
    """El registro en memoria solo se mantiene si TODAS las transiciones pasan
    por los dos helpers. Una asignación cruda deja un fantasma que no se apaga.
    """

    def test_solo_los_helpers_tocan_running_phase(self):
        """Se busca sobre el AST, no sobre el texto: en los docstrings del
        módulo se menciona `running_phase="preflight"` describiendo el flujo y
        un grep lo confunde con una asignación."""
        import ast

        src = (APP_DIR / "routers" / "cmv40.py").read_text()
        arbol = ast.parse(src)
        permitidas = {"_cmv40_marcar_activa", "_cmv40_marcar_libre"}
        crudas = []
        for fn in ast.walk(arbol):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name in permitidas:
                continue
            for nodo in ast.walk(fn):
                if not isinstance(nodo, (ast.Assign, ast.AugAssign)):
                    continue
                destinos = nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target]
                for d in destinos:
                    if isinstance(d, ast.Attribute) and d.attr == "running_phase":
                        crudas.append(f"{fn.name}() línea {nodo.lineno}")
        self.assertEqual(crudas, [],
                         "asignaciones a running_phase fuera de los helpers "
                         "(el registro en memoria se queda con un fantasma): "
                         + ", ".join(crudas))


if __name__ == "__main__":
    unittest.main()
