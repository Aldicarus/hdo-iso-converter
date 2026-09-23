"""Los ocho puntos del 2026-09-23, los que se prueban en el servidor.

Salieron de mirar un job CMv4.0 real. Aquí están los cuatro que se pueden
ejecutar; los otros cuatro son de pintado y van con los guards de su
pestaña.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_backlog_2026_09_23 -v
"""
import signal
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import PhaseTestCase, make_session, write_artifacts, RpuProps  # noqa: E402


class TestCancelarNoEsFallar(PhaseTestCase):
    """**Punto 8.** Cancelar la Fase G dejaba el job en error.

    Al cancelar se le manda un SIGTERM al proceso y el `returncode` llega
    NEGATIVO (-15). Los 36 sitios del pipeline que comprueban el código lo
    tomaban por un fallo, así que la fase acababa en «Fase remux FALLÓ:
    mkvmerge falló (código -15)» con su banner rojo — cuando lo que había
    pasado es que el usuario le dio a cancelar.

    Se arregla en `_run`/`_run_streaming` y no en los 36 sitios: el
    predicado de cancelación ya se consulta ANTES de cada subproceso, y
    consultarlo también DESPUÉS convierte la muerte por señal en
    `CMv40Cancelled`, que el orquestador registra como cancelada y sin
    tocar `error_message`.
    """

    def _preparar(self):
        session = make_session(self.wd)
        props = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                         frames=1000, has_l8=True)
        for art in ("BL.hevc", "EL_injected.hevc", "RPU_target.bin",
                    "source.hevc", "source_injected.hevc", "EL.hevc"):
            write_artifacts(self.wd, art, props=props)
        self.tb.define_rpu("RPU_target.bin", **props.as_dict())
        self.tb.define_media(Path(session.source_mkv_path).name,
                             duration=7200.0, frames=1000)
        return session

    async def test_un_subproceso_muerto_por_la_cancelacion_es_CANCELACION(self):
        from phases.cmv40_pipeline import (run_phase_g_remux, CMv40Cancelled,
                                           set_cancel_check)
        session = self._preparar()
        # El caso REAL: el usuario cancela MIENTRAS mkvmerge corre. El
        # predicado tiene que ser falso antes de lanzarlo y cierto después;
        # con `lambda: True` saltaría el chequeo PREVIO, que existe desde
        # siempre, y este test pasaría sin tocar el arreglo.
        def cancelado_durante():
            return any(c.binary == "mkvmerge" for c in self.tb.calls)
        set_cancel_check(cancelado_durante)
        self.tb.fail_when_arg("mkvmerge", "--no-video", senal=signal.SIGTERM)
        with self.assertRaises(CMv40Cancelled):
            await run_phase_g_remux(session, log_callback=self.log)

    async def test_y_un_fallo_de_VERDAD_sigue_siendo_un_fallo(self):
        """El guard no puede tragarse los fallos reales: sin cancelación
        pedida, un mkvmerge que revienta sigue siendo un error."""
        from phases.cmv40_pipeline import (run_phase_g_remux, CMv40Cancelled,
                                           set_cancel_check)
        session = self._preparar()
        set_cancel_check(lambda: False)
        self.tb.fail_when_arg("mkvmerge", "--no-video", rc=2)
        with self.assertRaises(Exception) as ctx:
            await run_phase_g_remux(session, log_callback=self.log)
        self.assertNotIsInstance(ctx.exception, CMv40Cancelled)


class TestElSeparadorDelLogEsCoherente(unittest.TestCase):
    """**Punto 4.** `━━━ Inicio fase: remux ━━━` decía la CLAVE INTERNA.

    Ni el número de fase ni el nombre que usa el resto de la aplicación: el
    log hablaba de `remux` mientras las cards, la timeline y el relato
    decían «Fase G». Y los dos pre-flight se llamaban los dos «preflight»,
    así que parecía el mismo separador duplicado.
    """

    @classmethod
    def setUpClass(cls):
        from routers.cmv40 import _separador_de_fase
        cls.sep = staticmethod(_separador_de_fase)

    def test_cada_fase_dice_su_letra(self):
        from routers.cmv40 import _separador_de_fase
        for fase, letra in (("analyze_source", "Fase A"), ("extract", "Fase C"),
                            ("correct_sync", "Fase E"), ("inject", "Fase F"),
                            ("remux", "Fase G"), ("validate", "Fase H"),
                            ("target_rpu_drive", "Fase B")):
            with self.subTest(fase=fase):
                s = _separador_de_fase(fase)
                self.assertIn(letra, s)
                self.assertNotIn(fase, s, "sigue saliendo la clave interna")

    def test_el_marcador_sigue_siendo_el_contrato(self):
        """`━━━` lo usa `_CMV40_LOG_FORCE_PERSIST_MARKERS` para forzar el
        guardado y el frontend para colorear: el prefijo no se toca."""
        from routers.cmv40 import _separador_de_fase
        s = _separador_de_fase("remux")
        self.assertTrue(s.startswith("━━━ ") and s.endswith(" ━━━"))

    def test_los_dos_preflight_no_se_llaman_igual(self):
        """No basta con que la FUNCIÓN sepa distinguirlos: hay que
        comprobar que los dos emisores le piden nombres distintos. Con el
        test sobre la función, apuntar los dos al mismo id pasaba en
        verde — lo destapó una mutación."""
        import re
        from routers.cmv40 import _separador_de_fase
        src = (APP_DIR / "routers" / "cmv40.py").read_text(encoding="utf-8")
        pedidos = set(re.findall(r'_separador_de_fase\("(preflight_\w+)"\)', src))
        self.assertEqual(len(pedidos), 2,
                         f"los dos pre-flight piden {pedidos}: en el log se "
                         f"leen como el mismo separador duplicado")
        a, b = (_separador_de_fase(p) for p in sorted(pedidos))
        self.assertNotEqual(a, b)

    def test_una_fase_sin_nombre_no_se_queda_sin_separador(self):
        from routers.cmv40 import _separador_de_fase
        self.assertIn("inventada", _separador_de_fase("inventada"))


class TestLaEtiquetaDelLogEsLaFaseEnCurso(unittest.TestCase):
    """**Punto 3.** Líneas de Fase A mientras corría la B.

    Diez helpers del pipeline se comparten entre fases y llevaban la
    etiqueta escrita a mano: `_medir_niveles_del_export` decía `[Fase A]` y
    la Fase B lo llama; `_export_rpu_frames` decía `[Fase C]` y la Fase E
    regenera el volcado.
    """

    def test_ningun_helper_lleva_la_etiqueta_escrita_a_mano(self):
        import ast
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        arbol = ast.parse(src)
        # Los docstrings de este módulo EXPLICAN el arreglo y citan la
        # etiqueta; lo que se juzga es lo que se EMITE, así que se recorren
        # las constantes de cadena y se descartan los docstrings.
        docs = set()
        for n in ast.walk(arbol):
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
                d = ast.get_docstring(n, clean=False)
                if d is not None:
                    docs.add(d)
        sueltas = [n.value for n in ast.walk(arbol)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and n.value.startswith("[Fase ") and n.value not in docs]
        self.assertEqual(sueltas, [], (
            "\nla etiqueta la pone la fase en curso (`_et()`), no el sitio "
            "que escribe la línea: un helper compartido miente en cuanto lo "
            "llama otra fase"))

    def test_sin_fase_declarada_no_se_inventa_ninguna(self):
        """El perfil de luminancia de Tab 2 llama a
        `_ffmpeg_extract_rpu_piped`, y ahí un `[Fase A]` ya era falso."""
        from phases.cmv40_pipeline import _et, set_fase_en_curso
        set_fase_en_curso("")
        self.assertEqual(_et(), "")

    def test_con_fase_declarada_la_dice(self):
        from phases.cmv40_pipeline import _et, set_fase_en_curso
        set_fase_en_curso("Fase C")
        self.assertEqual(_et(), "[Fase C] ")
        set_fase_en_curso("")

    def test_cada_fase_declara_la_suya(self):
        import re
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        for fn in ("run_phase_a_analyze_source", "run_phase_c_extract",
                   "run_phase_e_correct_sync", "run_phase_f_inject",
                   "run_phase_g_remux", "run_phase_h_validate"):
            with self.subTest(fase=fn):
                i = src.index(f"async def {fn}(")
                self.assertIn("set_fase_en_curso(", src[i:i + 2600],
                              f"{fn} no declara su etiqueta")


if __name__ == "__main__":
    unittest.main()
