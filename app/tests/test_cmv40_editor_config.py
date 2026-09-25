"""El `editor_config` que se le pasa a `dovi_tool editor`.

Lo que aquí se calcula mal no da un resultado torcido: da un `dovi_tool`
que se niega a correr y una Fase E muerta. Pasó el 2026-09-25 con Drive
(2011), justo con la combinación que genera el botón de desplazar:

    ✗ Fase correct_sync FALLÓ: dovi_tool editor falló: Error: invalid
      duplicate: DuplicateMetadata { source: 144682, offset: 144683, length: 29 }

**`dovi_tool` aplica los `remove` ANTES que los `duplicate`.** Con
T = 144683, quitar 29 por delante deja 144654 frames (0..144653), así que
`source: 144682` cae fuera. El rango del `remove` final ya descontaba lo
quitado por delante —por el mismo motivo, y está documentado—; lo que
faltaba era hacerlo también con el duplicado.

Se prueba la función pura, que por eso se extrajo del handler: el handler
hace `fetch` y no se puede invocar sin media pestaña montada.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_editor_config -v
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from frontend_sources import argv_node, js_completo  # noqa: E402

NODE = shutil.which("node")
T_DRIVE = 144683


def _config(ops: dict, total: int) -> dict:
    """Ejecuta `_cmv40ConfigDeSync` en node con esas casillas."""
    js = js_completo()
    i = js.index("\nfunction _cmv40ConfigDeSync(")
    fn = js[i + 1:js.index("\n}\n", i + 1) + 3]
    base = {"quitarInicio": 0, "quitarFinal": 0,
            "duplicarInicio": 0, "duplicarFinal": 0, **ops}
    guion = (fn + f"\nprocess.stdout.write(JSON.stringify("
                  f"_cmv40ConfigDeSync({json.dumps(base)}, {total})));")
    r = subprocess.run(argv_node(guion), capture_output=True, text=True,
                       timeout=60)
    if r.returncode != 0:
        raise AssertionError(f"node falló: {r.stderr[-800:]}")
    return json.loads(r.stdout)


def _aplicar(cfg: dict, total: int) -> tuple[bool, int]:
    """Simula lo que hace `dovi_tool`: (¿lo acepta?, frames que quedan).

    Los `remove` primero —es la regla que el error enseñó— y después los
    `duplicate`, validados contra lo que queda. Los rangos no se pueden
    solapar: quitar dos veces el mismo frame no significa nada y `dovi_tool`
    lo rechaza.
    """
    tocados: set[int] = set()
    for r in cfg.get("remove", []):
        ini, fin = (int(x) for x in r.split("-"))
        if ini < 0 or fin >= total or ini > fin:
            return False, 0
        rango = set(range(ini, fin + 1))
        if rango & tocados:
            return False, 0      # solape entre rangos
        tocados |= rango
    quedan = total - len(tocados)
    for d in cfg.get("duplicate", []):
        if not (0 <= d["source"] < quedan) or not (0 <= d["offset"] <= quedan):
            return False, 0
        quedan += d["length"]
    return True, quedan


def _valido(cfg: dict, total: int) -> bool:
    return _aplicar(cfg, total)[0]


@unittest.skipUnless(NODE, "node no disponible")
class TestElCasoDeDrive(unittest.TestCase):
    """Quitar 29 por delante y reponer 29 por detrás: el desplazamiento."""

    def setUp(self):
        self.cfg = _config({"quitarInicio": 29, "duplicarFinal": 29}, T_DRIVE)

    def test_dovi_tool_lo_aceptaria(self):
        self.assertTrue(_valido(self.cfg, T_DRIVE), self.cfg)

    def test_los_indices_cuentan_sobre_lo_que_QUEDA(self):
        # 144683 − 29 = 144654 frames, o sea 0..144653.
        self.assertEqual(self.cfg["duplicate"],
                         [{"source": 144653, "offset": 144654, "length": 29}])

    def test_y_el_recuento_no_se_mueve(self):
        quitados = sum(int(r.split("-")[1]) - int(r.split("-")[0]) + 1
                       for r in self.cfg["remove"])
        puestos = sum(d["length"] for d in self.cfg["duplicate"])
        self.assertEqual(quitados, puestos)


@unittest.skipUnless(NODE, "node no disponible")
class TestLasDemasCombinaciones(unittest.TestCase):

    CASOS = [
        {"quitarInicio": 29},
        {"quitarFinal": 29},
        {"duplicarInicio": 29},
        {"duplicarFinal": 29},
        {"quitarInicio": 29, "duplicarFinal": 29},     # desplazar hacia atrás
        {"duplicarInicio": 29, "quitarFinal": 29},     # y hacia delante
        {"quitarInicio": 10, "quitarFinal": 10},
        {"duplicarInicio": 10, "duplicarFinal": 10},
        {"quitarInicio": 5, "quitarFinal": 5,
         "duplicarInicio": 5, "duplicarFinal": 5},     # las cuatro a la vez
        # Con un target corto estos DOS rangos se pisan si no se descuenta
        # lo de delante: 0-29 y 20-39 sobre 40 frames.
        {"quitarInicio": 30, "quitarFinal": 20},
    ]

    def test_ninguna_produce_un_config_que_dovi_tool_rechace(self):
        for ops in self.CASOS:
            with self.subTest(**ops):
                self.assertTrue(_valido(_config(ops, T_DRIVE), T_DRIVE),
                                _config(ops, T_DRIVE))

    def test_tambien_con_un_target_corto(self):
        # Donde los dos rangos de `remove` se solapaban, que es el caso que
        # ya estaba contemplado.
        for ops in self.CASOS:
            with self.subTest(**ops):
                self.assertTrue(_valido(_config(ops, 40), 40),
                                _config(ops, 40))

    def test_el_duplicado_final_va_AL_FINAL(self):
        """Que `dovi_tool` lo acepte no significa que esté bien puesto.

        Con `offset` corto el duplicado se cuela en medio: los frames se
        insertan antes del final y la película queda cortada por ahí.
        """
        for ops in self.CASOS:
            if not ops.get("duplicarFinal"):
                continue
            with self.subTest(**ops):
                cfg = _config(ops, T_DRIVE)
                ok, _ = _aplicar({"remove": cfg.get("remove", [])}, T_DRIVE)
                self.assertTrue(ok)
                # Cuántos frames hay justo antes de insertar el último
                # bloque: los que quedan tras quitar, más lo duplicado al
                # inicio si lo hubo.
                _, antes = _aplicar({
                    "remove": cfg.get("remove", []),
                    "duplicate": [d for d in cfg["duplicate"]
                                  if d is not cfg["duplicate"][-1]],
                }, T_DRIVE)
                ultimo = cfg["duplicate"][-1]
                self.assertEqual(ultimo["offset"], antes,
                                 "el duplicado final no se inserta al final")
                self.assertEqual(ultimo["source"], antes - 1,
                                 "no copia el ÚLTIMO frame")

    def test_el_recuento_final_es_el_que_la_matriz_promete(self):
        for ops in self.CASOS:
            with self.subTest(**ops):
                cfg = _config(ops, T_DRIVE)
                ok, quedan = _aplicar(cfg, T_DRIVE)
                self.assertTrue(ok, cfg)
                esperado = (T_DRIVE - ops.get("quitarInicio", 0)
                            - ops.get("quitarFinal", 0)
                            + ops.get("duplicarInicio", 0)
                            + ops.get("duplicarFinal", 0))
                self.assertEqual(quedan, esperado, cfg)

    def test_sin_remove_el_final_sigue_siendo_el_ultimo(self):
        # Lo que hacía antes, y que sigue valiendo cuando no hay nada que
        # quitar: la corrección sólo cambia los casos que fallaban.
        cfg = _config({"duplicarFinal": 29}, T_DRIVE)
        self.assertEqual(cfg["duplicate"],
                         [{"source": T_DRIVE - 1, "offset": T_DRIVE,
                           "length": 29}])


if __name__ == "__main__":
    unittest.main()
