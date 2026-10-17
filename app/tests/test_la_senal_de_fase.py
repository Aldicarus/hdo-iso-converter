"""El spinner de la timeline de CMv4.0 sigue a la fase en los tres idiomas.

El WS de un proyecto CMv4.0 enciende y apaga el spinner de la fase en curso
sin esperar al GET (que con el NAS cargado tarda 30-60 s). Lo hacía leyendo
PROSA del log, la clase de fallo que `test_el_paso_no_se_adivina` existe para
impedir:

  · el arranque buscaba `━━━ Inicio fase: X ━━━`. Con el log en inglés eso
    es «Phase start:», y desde el 2026-09-23 la cabecera lleva el nombre
    traducido de la fase: no casaba en NINGÚN idioma;
  · el cierre buscaba «completada en» y «FALLÓ», así que solo funcionaba en
    castellano.

Hoy el arranque sale del `fase` que el servidor pone en el `§§PROGRESS§§`
(viaja solo por WS y no se traduce) y el cierre del prefijo `✓ Fase ` /
`✗ Fase `, que se concatena fuera del catálogo. El test no compone las
líneas a mano: el servidor ejecuta una fase de verdad en cada idioma, se
captura lo que manda por el WS, y eso pasa por la función real del frontend
en node. La revisión de Codex del 2026-10-17 lo destapó como sospecha sobre un
guard que buscaba la aguja `"FALLÓ: {"`, que ya no existía en el código.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_la_senal_de_fase -v
"""
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
APP_DIR = TESTS.parent
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TESTS))

from frontend_sources import argv_node, pieza_de  # noqa: E402

NODE = shutil.which("node")
IDIOMAS = ("es", "en", "ca")


def _funcion(fuente: str, nombre: str) -> str:
    """El texto de `function nombre(…) {…}`, contando llaves."""
    ini = fuente.index(f"function {nombre}(")
    i = fuente.index("{", ini)
    prof = 0
    while True:
        c = fuente[i]
        if c == "{":
            prof += 1
        elif c == "}":
            prof -= 1
            if prof == 0:
                return fuente[ini:i + 1]
        i += 1


class _WsFalso:
    def __init__(self):
        self.lineas = []

    async def send_text(self, texto):
        self.lineas.append(texto)


async def _lineas_del_ws(idioma: str) -> list[str]:
    """Lo que el WS manda durante una fase buena y una que falla."""
    import i18n
    import storage
    from models import CMv40Session
    from routers import cmv40 as r
    tmp = Path(tempfile.mkdtemp(prefix="senal_fase_"))
    try:
        (tmp / "cmv40").mkdir()
        parches = [mock.patch.object(i18n, "idioma_activo", lambda: idioma),
                   mock.patch.object(storage, "CONFIG_DIR", tmp),
                   mock.patch.object(storage, "CMV40_DIR", tmp / "cmv40")]
        for p in parches:
            p.start()
        try:
            s = CMv40Session(id=f"senal_{idioma}", source_mkv_path="/x.mkv",
                             source_mkv_name="x.mkv", output_mkv_name="Peli.mkv",
                             artifacts_dir=str(tmp), phase="target_provided")
            storage.save_cmv40_session(s)
            ws = _WsFalso()
            r._cmv40_ws_connections[s.id] = [ws]
            r._cmv40_log_buffer.pop(s.id, None)

            async def _bien(log_cb, proc_cb):
                await log_cb('§§PROGRESS§§{"pct": 10, "label": "demux"}')
                await log_cb("[Fase C] una línea cualquiera")

            async def _mal(log_cb, proc_cb):
                await log_cb('§§PROGRESS§§{"pct": 5, "label": "inject"}')
                raise RuntimeError("dovi_tool rc=137")

            await r._run_cmv40_phase(s, "extract", lambda a, b: _bien(a, b), "extracted")
            await r._run_cmv40_phase(s, "inject", lambda a, b: _mal(a, b), "injected")
            for _ in range(20):
                await asyncio.sleep(0)
            return ws.lineas
        finally:
            r._cmv40_ws_connections.pop(f"senal_{idioma}", None)
            r._cmv40_log_buffer.pop(f"senal_{idioma}", None)
            for p in reversed(parches):
                p.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(NODE, "node no disponible")
class TestLaSenalDeFaseEnLosTresIdiomas(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.lineas = {i: asyncio.run(_lineas_del_ws(i)) for i in IDIOMAS}
        _, fuente = pieza_de("_cmv40SenalDeFase")
        js = (_funcion(fuente, "_cmv40ParseProgress") + "\n"
              + _funcion(fuente, "_cmv40SenalDeFase") + "\n"
              + "const entrada = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
              + "const out = {};\n"
              + "for (const [k, ls] of Object.entries(entrada)) "
              + "out[k] = ls.map(l => [l, _cmv40SenalDeFase(l)]);\n"
              + "console.log(JSON.stringify(out));\n")
        r = subprocess.run(argv_node(js), input=json.dumps(cls.lineas),
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        cls.senales = json.loads(r.stdout)

    def test_el_servidor_habla_el_idioma_pedido(self):
        """Si las tres capturas fueran castellanas, el test no probaría nada."""
        cierres = {i: next(l for l in self.lineas[i] if "✗ Fase" in l) for i in IDIOMAS}
        self.assertIn("FALLÓ", cierres["es"])
        self.assertIn("FAILED", cierres["en"])
        self.assertIn("HA FALLAT", cierres["ca"])

    def test_el_arranque_dice_que_fase_corre(self):
        for i in IDIOMAS:
            with self.subTest(idioma=i):
                inicios = [s["inicio"] for _, s in self.senales[i] if s and "inicio" in s]
                self.assertEqual(inicios[:1], ["extract"])
                self.assertIn("inject", inicios)

    def test_el_cierre_se_ve_bien_y_mal(self):
        for i in IDIOMAS:
            with self.subTest(idioma=i):
                fines = [l for l, s in self.senales[i] if s and s.get("fin")]
                self.assertEqual(len(fines), 2, fines)
                self.assertTrue(any("✓ Fase" in l for l in fines))
                self.assertTrue(any("✗ Fase" in l for l in fines))

    def test_lo_demas_no_es_senal(self):
        for i in IDIOMAS:
            with self.subTest(idioma=i):
                for l, s in self.senales[i]:
                    if "━━━" in l or "[Fase C]" in l:
                        self.assertIsNone(s, l)

    def test_un_progreso_sin_fase_no_inventa_una(self):
        js = (_funcion(pieza_de("_cmv40SenalDeFase")[1], "_cmv40ParseProgress")
              + _funcion(pieza_de("_cmv40SenalDeFase")[1], "_cmv40SenalDeFase")
              + "console.log(JSON.stringify(["
              + "_cmv40SenalDeFase('[1] §§PROGRESS§§{\"pct\":3}'),"
              + "_cmv40SenalDeFase('§§PROGRESS§§{\"pct\":3,\"fase\":\"<b>\"}'),"
              + "_cmv40SenalDeFase('[Fase C] ✓ BL extraído'),"
              + "_cmv40SenalDeFase(null)]));")
        r = subprocess.run(argv_node(js), capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(r.stdout), [None, None, None, None], r.stderr)


class TestElProgresoLlevaLaFase(unittest.TestCase):

    def test_con_fase_en_curso_y_sin_ella(self):
        from models import CMv40Session
        from routers import cmv40 as r
        s = CMv40Session(id="x", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
                         output_mkv_name="o.mkv")
        s.running_phase = "remux"
        msg, _ = r._cmv40_augment_progress(s, '§§PROGRESS§§{"pct": 40, "label": "mux"}')
        self.assertEqual(json.loads(msg[len("§§PROGRESS§§"):])["fase"], "remux")
        s.running_phase = None
        msg, _ = r._cmv40_augment_progress(s, '§§PROGRESS§§{"pct": 40, "label": "mux"}')
        self.assertNotIn("fase", json.loads(msg[len("§§PROGRESS§§"):]))


if __name__ == "__main__":
    unittest.main()
