"""`POST /api/disc-probe`, que se quedó sin cobertura y petó en producción.

El bug: `disc_probe` tenía una variable local llamada **`paths`** (la lista de
m2ts del payload), y `paths` es también el MÓDULO donde viven los directorios.
Python decide el ámbito de un nombre por función completa, así que esa
asignación —60 líneas MÁS ABAJO— convertía en local el `paths.ISOS_DIR` de la
validación de path-traversal, y la petición moría con
`UnboundLocalError: local variable 'paths' referenced before assignment`
**antes de tocar el disco**.

Estaba latente desde que los directorios pasaron a `paths.py`: mientras la
función leía la global `ISOS_DIR` a secas, una local llamada `paths` era
inofensiva. Lo destapó una serie real (Mandalorian) en el primer uso.

Por qué no lo cazó nada:
  · el golden de rutas comprueba que el endpoint EXISTE, no que responda;
  · el análisis de nombres libres del corte por routers no lo ve, porque
    `paths` **sí** está ligado en ese ámbito — eso es justamente el bug;
  · `test_endpoints_tab1_tab2` cubría el file browser y el whitelist de
    borrado, no `disc-probe`.

Aquí hay dos cosas: el endpoint ejecutado por HTTP en sus tres orígenes, y un
guard AST para toda la clase de bug (un local que sombrea un módulo importado
y que la función usa como atributo).

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_disc_probe_m2ts -v
"""
import ast
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402

# `disc_probe` importa de `phases.phase_a` DENTRO de la función, así que el
# parche va en el módulo de origen: en `routers.tab1` el nombre no existe.
_CANDIDATOS_M2TS = "phases.phase_a.identify_episode_candidates_from_m2ts_list"


class TestDiscProbe(ApiTestCase):
    """El endpoint, por HTTP. Los tres orígenes y los dos hints."""

    def setUp(self):
        super().setUp()
        # Un m2ts y una carpeta BDMV de verdad bajo el root de ISOs, para que
        # la validación de path-traversal pase y se llegue al cuerpo.
        (self.isos_dir / "raw").mkdir(parents=True, exist_ok=True)
        self.m2ts = []
        for n in ("00001.m2ts", "00002.m2ts"):
            p = self.isos_dir / "raw" / n
            p.write_bytes(b"\x47" + b"\x00" * 2048)
            self.m2ts.append(f"raw/{n}")

    def _probe(self, **body):
        return self.client.post("/api/disc-probe", json=body)

    def test_m2ts_multiple_en_modo_serie_no_revienta(self):
        """EL BUG. Con dos m2ts y hint 'series' se entra en la rama que
        declaraba el local `paths`; antes del arreglo esto era un 500."""
        with mock.patch(_CANDIDATOS_M2TS,
                        new=mock.AsyncMock(return_value=[
                            {"mpls_name": "00001.m2ts", "mpls_path": self.m2ts[0],
                             "duration_minutes": 42.0, "audio_track_count": 1,
                             "data": {}},
                            {"mpls_name": "00002.m2ts", "mpls_path": self.m2ts[1],
                             "duration_minutes": 41.5, "audio_track_count": 1,
                             "data": {}},
                        ])):
            r = self._probe(source_type="m2ts", m2ts_paths=self.m2ts,
                            media_type_hint="series")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["media_type"], "series")
        self.assertEqual(len(r.json()["episode_candidates"]), 2)

    def test_m2ts_multiple_en_modo_pelicula_da_400_con_explicacion(self):
        """No un 500: el usuario eligió película y trajo N ficheros."""
        r = self._probe(source_type="m2ts", m2ts_paths=self.m2ts,
                        media_type_hint="movie")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("modo serie", r.json()["detail"])

    def test_un_solo_m2ts_en_modo_pelicula(self):
        r = self._probe(source_type="m2ts", m2ts_paths=self.m2ts[:1],
                        media_type_hint="movie")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["media_type"], "movie")

    def test_sin_hint_un_m2ts_es_pelicula_y_varios_serie(self):
        """El auto-detect legacy, para frontends anteriores al toggle."""
        r = self._probe(source_type="m2ts", m2ts_paths=self.m2ts[:1])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["media_type"], "movie")

        with mock.patch(_CANDIDATOS_M2TS,
                        new=mock.AsyncMock(return_value=[
                            {"mpls_name": "00001.m2ts", "mpls_path": self.m2ts[0],
                             "duration_minutes": 42.0, "audio_track_count": 1,
                             "data": {}}])):
            r = self._probe(source_type="m2ts", m2ts_paths=self.m2ts)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["media_type"], "series")

    def test_bdmv_en_modo_pelicula_no_revienta(self):
        """La rama que el usuario tocó de verdad (Mandalorian, `bdmv_folder`).

        El local `paths` sombreaba la función COMPLETA, así que el 500 no era
        exclusivo de la rama m2ts: la línea que fallaba en producción era el
        `safe_source_path` del `else`, o sea que `disc-probe` estaba roto para
        los TRES orígenes. Sin este caso el test cubriría el arreglo pero no
        el camino reportado.
        """
        bdmv = self.isos_dir / "UNA_PELI_UHD"
        (bdmv / "BDMV" / "PLAYLIST").mkdir(parents=True)
        (bdmv / "BDMV" / "STREAM").mkdir(parents=True)
        (bdmv / "BDMV" / "PLAYLIST" / "00800.mpls").write_bytes(b"\x00" * 512)
        (bdmv / "BDMV" / "STREAM" / "00800.m2ts").write_bytes(b"\x47" + b"\x00" * 4096)

        r = self._probe(source_type="bdmv_folder", source_path="UNA_PELI_UHD",
                        media_type_hint="movie")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["media_type"], "movie")
        self.assertEqual(r.json()["source_type"], "bdmv_folder")

    def test_bdmv_de_un_solo_titulo_en_modo_serie_da_400_no_500(self):
        """Caso real: el usuario eligió Serie sobre el BD de una película.
        Tiene que explicarlo, no romperse."""
        bdmv = self.isos_dir / "OTRA_PELI_UHD"
        (bdmv / "BDMV" / "PLAYLIST").mkdir(parents=True)
        (bdmv / "BDMV" / "STREAM").mkdir(parents=True)
        with mock.patch("phases.phase_a.identify_episode_candidates",
                        new=mock.AsyncMock(return_value=[])):
            r = self._probe(source_type="bdmv_folder", source_path="OTRA_PELI_UHD",
                            media_type_hint="series")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("candidato a episodio", r.json()["detail"])

    def test_una_ruta_de_fuera_del_root_da_400(self):
        """La validación de traversal es lo PRIMERO que hace la función, y es
        justo la línea que reventaba."""
        r = self._probe(source_type="m2ts",
                        m2ts_paths=["../../etc/passwd.m2ts"],
                        media_type_hint="series")
        self.assertEqual(r.status_code, 400, r.text)

    def test_sin_origen_da_400(self):
        self.assertEqual(self._probe().status_code, 400)

    def test_el_progreso_queda_cerrado_al_terminar(self):
        """El modal "Detectando contenido" pollea esto; si se queda en
        `running` gira para siempre."""
        self._probe(source_type="m2ts", m2ts_paths=self.m2ts[:1],
                    media_type_hint="movie")
        prog = self.client.get("/api/disc-probe/progress").json()
        self.assertFalse(prog["running"], prog)


class TestNingunLocalSombreaUnModulo(unittest.TestCase):
    """El guard de la clase entera, sobre el AST.

    Un local con el nombre de un módulo importado no es un error de sintaxis
    ni un nombre libre: es un `UnboundLocalError` esperando a que alguien
    entre en esa rama. Y el daño es proporcional a la distancia — aquí la
    asignación estaba 60 líneas por debajo del uso.
    """

    # Los módulos que se referencian como `X.algo` por todo el backend. No es
    # una lista arbitraria: son los que un local podría querer llamarse igual
    # (`paths = [...]`, `storage = ...`).
    _VIGILADOS = {"paths", "workload", "analysis_progress", "storage",
                  "json", "os", "asyncio", "shutil", "time", "re"}

    def test_ninguna_funcion_del_backend_sombrea_un_modulo_que_usa(self):
        problemas = []
        for fichero in sorted(APP_DIR.rglob("*.py")):
            if "tests" in fichero.parts:
                continue
            arbol = ast.parse(fichero.read_text(encoding="utf-8"))
            importados = set()
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Import):
                    for a in nodo.names:
                        importados.add(a.asname or a.name.split(".")[0])
            candidatos = importados & self._VIGILADOS
            if not candidatos:
                continue
            for fn in ast.walk(arbol):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                ligados = {n.id for n in ast.walk(fn)
                           if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                ligados |= {a.arg for a in ast.walk(fn) if isinstance(a, ast.arg)}
                como_modulo = {n.value.id for n in ast.walk(fn)
                               if isinstance(n, ast.Attribute)
                               and isinstance(n.value, ast.Name)}
                choque = ligados & candidatos & como_modulo
                if choque:
                    problemas.append(
                        f"{fichero.relative_to(APP_DIR)}:{fn.lineno} "
                        f"{fn.name}() liga {sorted(choque)}, que también usa "
                        f"como módulo")
        self.assertEqual(problemas, [], "; ".join(problemas))


if __name__ == "__main__":
    unittest.main()
