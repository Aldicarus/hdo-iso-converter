"""Lo que el corte de `main.py` por pestañas podría romper en silencio.

Mover 1.266 líneas de `main.py` a `routers/tab2.py` no cambia ninguna URL
—eso lo vigila `test_rutas_no_cambian` contra un golden— pero sí hay dos
cosas que un movimiento de código puede romper sin que nada se queje:

1. **El progreso compartido.** `GET /api/analyze/progress` alimenta DOS
   modales: el de "Analizando disco" del Tab 1 y el de "Abriendo MKV" del
   Tab 2. Mientras los dos escritores vivían en el mismo módulo bastaba una
   global y un `global _analyze_progress`. Con el Tab 2 en otro fichero, ese
   `global` habría rebindeado un nombre PROPIO del router: el Tab 2 seguiría
   escribiendo su progreso, el endpoint seguiría respondiendo 200, y el modal
   se quedaría en blanco. Ni un error, ni un log. Por eso el estado vive en
   `analysis_progress` y estos tests comprueban los dos extremos del cable.

2. **El recovery del apply.** Corría en el import de `main`; ahora es una
   función pública del router que `main` llama al arrancar. Si esa llamada
   se cae, un contenedor que se reinicia a mitad de una copia deja el `.mkv`
   parcial en `/mnt/output` y el estado en `active=True` — con lo que el
   siguiente intento choca con el 409 de "ya existe" sobre un fichero que
   no sirve para nada. Se comprueba arrancando la app de verdad en otro
   proceso, que es la única forma de observar el import.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_split_por_pestanas -v
"""
import json
import os
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402


class TestProgresoCompartido(ApiTestCase):
    """Los dos extremos del cable, cada uno por separado."""

    def test_el_callback_de_tab2_escribe_en_el_estado_compartido(self):
        """Extremo escritor. El endpoint de Tab 2 construye su
        `_mkv_progress_callback` y lo pasa a `analyze_mkv`; lo que ese callback
        escriba tiene que aterrizar en el módulo compartido, no en una global
        del router."""
        import analysis_progress
        from routers import tab2

        visto = {}

        async def analyze_falso(path, progress_callback=None, pgs_progress_callback=None):
            await progress_callback("mediainfo")
            # Leído DENTRO del análisis: es el único momento en que el paso
            # intermedio existe. Si el router escribiera en un dict propio,
            # aquí se vería todavía el valor inicial.
            visto.update(analysis_progress.leer())
            raise RuntimeError("corta aquí: solo se mira el progreso")

        mkv = self.output_dir / "Peli.mkv"
        mkv.write_bytes(b"x")
        with unittest.mock.patch.object(tab2, "analyze_mkv", analyze_falso):
            self.client.post("/api/mkv/analyze", json={"file_path": str(mkv)})

        self.assertEqual(visto.get("step"), "mediainfo", visto)
        self.assertIs(visto.get("done"), False, visto)

    def test_el_endpoint_de_tab1_lee_lo_que_escribio_tab2(self):
        """Extremo lector. `/api/analyze/progress` vive en `main` y tiene que
        devolver lo que acaba de escribir el router de Tab 2."""
        import analysis_progress

        antes = self.client.get("/api/analyze/progress").json()
        self.assertIs(antes["done"], False, "arranca sin análisis en curso")

        analysis_progress.fijar(step="pgs", done=False, pct=42.5, eta_s=13)
        r = self.client.get("/api/analyze/progress")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"step": "pgs", "done": False,
                                    "pct": 42.5, "eta_s": 13})

    def test_el_analisis_de_tab2_deja_el_progreso_cerrado(self):
        """Un análisis que termina tiene que dejar `done=True`, o el modal se
        queda girando para siempre."""
        from routers import tab2

        async def analyze_falso(path, progress_callback=None, pgs_progress_callback=None):
            raise RuntimeError("el pipeline falló")

        mkv = self.output_dir / "Peli.mkv"
        mkv.write_bytes(b"x")
        with unittest.mock.patch.object(tab2, "analyze_mkv", analyze_falso):
            r = self.client.post("/api/mkv/analyze", json={"file_path": str(mkv)})
        self.assertEqual(r.status_code, 500)

        estado = self.client.get("/api/analyze/progress").json()
        self.assertIs(estado["done"], True, "incluso al fallar")
        self.assertIn("el pipeline falló", estado.get("error", ""))


class TestRecoveryDelApplyAlArrancar(unittest.TestCase):
    """Arranca la app en otro proceso: es la única forma de ver el import."""

    def _arrancar(self, config_dir: Path, output_dir: Path) -> subprocess.CompletedProcess:
        entorno = {
            **os.environ,
            "CONFIG_DIR": str(config_dir),
            "OUTPUT_DIR": str(output_dir),
            "ISOS_DIR": str(config_dir / "isos"),
            "LIBRARY_DIR": str(config_dir / "library"),
            "TMP_DIR": str(config_dir / "tmp"),
            "DEV_MODE": "0",
            "PYTHONPATH": str(APP_DIR),
        }
        return subprocess.run([sys.executable, "-c", "import main"],
                              capture_output=True, text=True, timeout=120,
                              env=entorno, cwd=str(APP_DIR))

    def test_al_arrancar_se_borra_el_mkv_a_medias_y_el_estado_queda_en_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config"
            output = Path(td) / "output"
            for d in (config, output, config / "isos", config / "library",
                      config / "tmp"):
                d.mkdir(parents=True)
            parcial = output / "A medio copiar.mkv"
            parcial.write_bytes(b"y" * 4096)
            (config / "mkv_apply_state.json").write_text(json.dumps({
                "active": True, "step": "copying", "file_name": "A medio copiar.mkv",
                "src_path": "/mnt/library/A medio copiar.mkv",
                "dst_path": str(parcial), "bytes_copied": 4096,
                "total_bytes": 70_000_000_000,
            }))

            r = self._arrancar(config, output)
            self.assertEqual(r.returncode, 0, r.stderr[-3000:])

            self.assertFalse(parcial.exists(),
                             "el destino parcial tiene que desaparecer al arrancar")
            estado = json.loads((config / "mkv_apply_state.json").read_text())
            self.assertIs(estado["active"], False)
            self.assertEqual(estado["step"], "error")
            self.assertIn("interrumpida", estado.get("error", "").lower())

    def test_sin_nada_interrumpido_el_arranque_no_toca_el_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config"
            output = Path(td) / "output"
            for d in (config, output, config / "isos", config / "library",
                      config / "tmp"):
                d.mkdir(parents=True)
            intacto = output / "Peli buena.mkv"
            intacto.write_bytes(b"z" * 2048)

            r = self._arrancar(config, output)
            self.assertEqual(r.returncode, 0, r.stderr[-3000:])
            self.assertTrue(intacto.exists(), "sin estado que recuperar, nada se borra")


class TestCadaRouterSeRecuperaSolo(unittest.TestCase):
    """Las tres recuperaciones de arranque, y que `main` las llame.

    Cada router expone la suya como función pública porque el recovery tiene
    que vivir junto al estado que protege: el de CMv4.0 es lo que hace segura
    la fuente de verdad EN MEMORIA de qué proyectos tienen fase en marcha, y
    tenerlo en otro fichero es como se desincronizan estas cosas.
    """

    def test_los_tres_routers_exponen_su_recuperacion(self):
        from routers import cmv40, tab1, tab2
        for mod, nombre in ((tab1, "recuperar_sesiones_interrumpidas"),
                            (tab2, "recuperar_apply_interrumpido"),
                            (cmv40, "recuperar_sesiones_interrumpidas")):
            self.assertTrue(callable(getattr(mod, nombre, None)),
                            f"{mod.__name__} no expone {nombre}")

    def test_main_las_llama_todas_al_arrancar(self):
        """Comprobado sobre el AST: son llamadas a nivel de módulo, así que
        corren en el import y no hay forma de observarlas dos veces en el
        mismo proceso."""
        import ast
        arbol = ast.parse((APP_DIR / "main.py").read_text())
        llamadas = set()
        for nodo in arbol.body:
            if (isinstance(nodo, ast.Expr) and isinstance(nodo.value, ast.Call)
                    and isinstance(nodo.value.func, ast.Attribute)
                    and isinstance(nodo.value.func.value, ast.Name)):
                llamadas.add(f"{nodo.value.func.value.id}.{nodo.value.func.attr}")
        for esperada in ("_tab1_routes.recuperar_sesiones_interrumpidas",
                         "_tab2_routes.recuperar_apply_interrumpido",
                         "_cmv40_routes.recuperar_sesiones_interrumpidas"):
            self.assertIn(esperada, llamadas,
                          "main tiene que llamarla en el arranque")

    def test_main_no_se_queda_con_codigo_de_ninguna_pestana(self):
        """Lo que queda en `main` es la aplicación, no una pestaña. Un endpoint
        de pestaña ahí es una regresión del corte."""
        import ast
        arbol = ast.parse((APP_DIR / "main.py").read_text())
        rutas = []
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in nodo.decorator_list:
                if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                        and deco.args and isinstance(deco.args[0], ast.Constant)):
                    rutas.append(deco.args[0].value)
        de_pestana = [r for r in rutas if isinstance(r, str) and (
            r.startswith("/api/mkv/") or r.startswith("/api/cmv40")
            or r.startswith("/api/sessions") or r.startswith("/api/queue")
            or r in ("/api/analyze", "/api/disc-probe", "/api/sources"))]
        self.assertEqual(de_pestana, [], "esas rutas van en su router")


class TestElBloqueDevSigueRegistrando(unittest.TestCase):
    """`/api/dev/simulate` está DENTRO de un `if DEV_MODE:`, o sea indentado.

    El corte cambió los decoradores `@app.` por `@router.` y el de este
    endpoint no empieza en columna 0: se quedó apuntando a un `app` que en el
    router no existe. Como con DEV_MODE=0 el bloque no se ejecuta, ni el
    golden de rutas ni los 940 tests lo habrían visto — solo un `NameError`
    al arrancar en desarrollo.
    """

    def test_con_dev_mode_la_ruta_existe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [sys.executable, "-c",
                 "import main; print([x.path for x in main.app.routes "
                 "if x.path == '/api/dev/simulate'])"],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "PYTHONPATH": str(APP_DIR), "DEV_MODE": "1",
                     "CONFIG_DIR": td, "ISOS_DIR": td, "OUTPUT_DIR": td,
                     "LIBRARY_DIR": td, "TMP_DIR": td},
                cwd=str(APP_DIR))
            self.assertEqual(r.returncode, 0, r.stderr[-3000:])
            self.assertIn("/api/dev/simulate", r.stdout)


class TestLaDependenciaVaEnUnSoloSentido(unittest.TestCase):
    """`main` importa los routers; ningún router importa `main`.

    Es lo que permite mover un endpoint sin arrastrar el resto de la app, y lo
    que hace que un test pueda cargar el router solo. Un `import main` desde un
    router crearía un ciclo que Python resuelve a veces —según quién se importe
    primero— y falla en el resto.
    """

    def test_ningun_router_importa_main(self):
        import ast
        for fichero in sorted((APP_DIR / "routers").glob("*.py")):
            arbol = ast.parse(fichero.read_text())
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Import):
                    nombres = [a.name.split(".")[0] for a in nodo.names]
                elif isinstance(nodo, ast.ImportFrom):
                    nombres = [(nodo.module or "").split(".")[0]]
                else:
                    continue
                self.assertNotIn("main", nombres,
                                 f"{fichero.name}:{nodo.lineno} importa main")

    def test_cada_router_se_puede_cargar_solo(self):
        """Sin `main` en `sys.modules`, para que el ciclo no pase inadvertido
        por el orden de importación de la suite. Y con rutas: un router que
        cargue pero no registre nada sería un import vacío."""
        for nombre, minimo in (("tab1", 20), ("tab2", 12), ("cmv40", 30)):
            with self.subTest(router=nombre):
                r = subprocess.run(
                    [sys.executable, "-c",
                     f"import sys; from routers import {nombre} as m; "
                     "assert 'main' not in sys.modules, 'arrastró main'; "
                     "print(len(m.router.routes))"],
                    capture_output=True, text=True, timeout=120,
                    env={**os.environ, "PYTHONPATH": str(APP_DIR)},
                    cwd=str(APP_DIR))
                self.assertEqual(r.returncode, 0, r.stderr[-3000:])
                self.assertGreaterEqual(int(r.stdout.strip()), minimo)


if __name__ == "__main__":
    unittest.main()
