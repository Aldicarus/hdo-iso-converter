"""Guard: nada pesado dentro de una corrutina sin pasar por un thread.

Este servidor es un único proceso asyncio de un solo worker que además
supervisa tres pipelines de vídeo. Cualquier trabajo síncrono largo dentro de
una corrutina PARA el bucle, y con él el reader del subproceso en marcha: el
pipe de ffmpeg se llena, ffmpeg se detiene y el usuario ve el log congelado.
Está documentado en CLAUDE.md con el objetivo de <50 ms p95.

Casos reales que este guard existe para que no vuelvan:

  · el perfil de luminancia hacía `json.load` de un volcado de 1,12 GB y
    después recorría 243.552 frames — 2,4 s de bucle parado en un Mac, ×3-4 en
    el NAS;
  · `GET /sync-data` parseaba y re-serializaba 24,1 MB en cada petición;
  · `cmv40_cleanup_preview` cargaba las 88 sesiones enteras y recorría sus
    workdirs con `rglob`;
  · `cmv40_cleanup` hacía `rmtree` de 250-400 GB.

El análisis es estático (AST) y por eso conservador: solo mira llamadas que
están en el cuerpo de una `async def`, y NO cuenta las que viven dentro de una
función síncrona anidada (el patrón normal para `asyncio.to_thread(_helper)`).
Las excepciones van en `_EXENTAS` con su motivo — la idea es que añadir una
cueste una línea y una explicación, no que sea invisible.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_event_loop_limpio -v
"""
import ast
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]

_FICHEROS = ("main.py", "routers/tab2.py", "routers/cmv40.py",
             "phases/mkv_analyze.py", "phases/luminance.py")

# Llamadas cuyo coste escala con el TAMAÑO del dato: un volcado de RPU, un
# árbol de artefactos de 400 GB, un JSON de 24 MB.
_PESADAS = {
    "load": {"json", "_json"},          # json.load de un fichero grande
    "rmtree": {"shutil", "_sh", "_shutil", "_cmv40_shutil", "_shutil_so"},
    "copytree": {"shutil", "_sh", "_shutil", "_cmv40_shutil"},
    "copyfileobj": {"shutil", "_sh", "_shutil"},
    "rglob": None,                       # cualquier receptor (Path)
    "walk": {"os"},
}

# (fichero, función, llamada) → motivo por el que se acepta. Vacío a
# propósito: si hace falta añadir una, que cueste escribir el motivo.
_EXENTAS: dict[tuple[str, str, str], str] = {}


def _receptor(func: ast.AST) -> str | None:
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


def _hallazgos(ruta: Path, etiqueta: str) -> list[tuple[str, str, str]]:
    arbol = ast.parse(ruta.read_text())
    fuera = []
    for fn in ast.walk(arbol):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        # Todo lo que cuelgue de una def síncrona anidada está, por
        # construcción, destinado a `asyncio.to_thread` (o es un callback).
        en_helper = set()
        for sub in ast.walk(fn):
            if isinstance(sub, ast.FunctionDef):
                for x in ast.walk(sub):
                    en_helper.add(id(x))
        for nodo in ast.walk(fn):
            if not isinstance(nodo, ast.Call) or id(nodo) in en_helper:
                continue
            f = nodo.func
            nombre = f.attr if isinstance(f, ast.Attribute) else (
                f.id if isinstance(f, ast.Name) else None)
            if nombre not in _PESADAS:
                continue
            receptores = _PESADAS[nombre]
            if receptores is not None and _receptor(f) not in receptores:
                continue
            fuera.append((etiqueta, fn.name, nombre))
    return fuera


class TestSinTrabajoPesadoEnElBucle(unittest.TestCase):

    def test_no_hay_llamadas_pesadas_nuevas_en_corrutinas(self):
        nuevas = []
        for rel in _FICHEROS:
            for hallazgo in _hallazgos(APP_DIR / rel, rel):
                if hallazgo not in _EXENTAS:
                    nuevas.append(hallazgo)
        self.assertEqual(
            nuevas, [],
            "trabajo pesado dentro de una corrutina — envuélvelo en "
            "`asyncio.to_thread(...)` o añade la excepción a _EXENTAS con su "
            f"motivo:\n  " + "\n  ".join(f"{f} :: {fn}() → {c}" for f, fn, c in nuevas))

    def test_las_exenciones_siguen_existiendo(self):
        """Si una exención deja de aplicar (porque se arregló), hay que
        borrarla: si no, el guard deja de vigilar ese sitio para siempre."""
        vivos = set()
        for rel in _FICHEROS:
            vivos.update(_hallazgos(APP_DIR / rel, rel))
        muertas = [k for k in _EXENTAS if k not in vivos]
        self.assertEqual(muertas, [],
                         "exenciones que ya no corresponden a ningún código; "
                         f"bórralas de _EXENTAS: {muertas}")


class TestLosSitiosArregladosSiguenEnUnThread(unittest.TestCase):
    """Los cuatro de la Tanda 2, comprobados por su forma: la llamada pesada
    tiene que estar dentro de una def síncrona que se pasa a `to_thread`."""

    def _fuente(self, rel: str) -> str:
        return (APP_DIR / rel).read_text()

    def test_el_analisis_extendido(self):
        """El pipeline combinado hace el parseo y las dos agregaciones en
        threads: son 243.552 frames por nivel."""
        src = self._fuente("phases/mkv_analyze.py")
        for llamada in ("await asyncio.to_thread(analysis_desde_paths, utiles)",
                        "await asyncio.to_thread(cargar_niveles, utiles)",
                        "await asyncio.to_thread(payload_de_luminancia, niveles)"):
            self.assertIn(llamada, src)

    def test_las_series_del_chart_de_sincronizacion(self):
        src = self._fuente("routers/cmv40.py")
        self.assertIn("await asyncio.to_thread(\n        _cmv40_pfd_cargar", src)

    def test_el_recuento_del_preview_de_limpieza(self):
        src = self._fuente("routers/cmv40.py")
        self.assertIn("await asyncio.to_thread(_recolectar)", src)

    def test_el_borrado_de_los_workdirs(self):
        src = self._fuente("routers/cmv40.py")
        self.assertIn("await asyncio.to_thread(_medir_y_borrar, wd)", src)
        self.assertIn("await asyncio.to_thread(_cmv40_shutil.rmtree, wd", src)


if __name__ == "__main__":
    unittest.main()
