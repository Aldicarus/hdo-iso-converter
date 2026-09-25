"""Cuando el recuento cuadra pero las curvas no, la app ofrece el arreglo.

Caso real (Drive 2011, 2026-09-25): el usuario corrigió el desfase de −426
frames, el Δ quedó en 0 y el botón «Confirmar sync» seguía apagado. Lo que
faltaba no era recuento sino ALINEAMIENTO: Pearson 3 %, y la correlación
diciendo que desplazando 29 frames sube al 92,7 %. En pantalla eso eran tres
datos que no se cruzaban solos —«después de aplicar: 0 frames», el botón
bloqueado y un «Offset detectado: +29»— y la salida era traducir ese +29 a
mano a dos casillas de la matriz.

**Esto NO es el auto-relleno que se retiró.** Aquel escribía el Δ en una
casilla y adivinaba: con dos extremos hay infinitas combinaciones que dan el
mismo Δ. Aquí el Δ ya es cero y lo que falta es un desplazamiento, que tiene
una sola forma —se quita por un extremo lo que se repone por el otro— y una
dirección que la correlación ya midió. Y aun así lo pulsa el usuario: la
matriz queda escrita y la ve antes de aplicar.

El signo sale de `detect_sync_offset`, que compara `src[i]` con
`tgt[i + offset]`: positivo es que el target va ADELANTADO.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_desplazamiento -v
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

from frontend_sources import argv_node, js_completo, motor_i18n  # noqa: E402

NODE = shutil.which("node")


def _fn(nombre: str) -> str:
    js = js_completo()
    for marca in (f"\nfunction {nombre}(", f"\nasync function {nombre}("):
        i = js.find(marca)
        if i != -1:
            return js[i + 1:js.index("\n}\n", i + 1) + 3]
    raise AssertionError(f"no se encuentra `{nombre}`")


_DRIVER = """
'use strict';
const campos = {};
globalThis.document = { getElementById: id => (campos[id] = campos[id]
  || { value: '0', dataset: {} }) };
globalThis.marcarTocado = el => { el.dataset.tocado = '1'; };
globalThis._cmv40UpdateExpectedDelta = () => {};

__CUERPO__

const leer = () => ({
  quitarInicio:    campos['cmv40-remove-p1'].value,
  quitarFinal:     campos['cmv40-remove-fin-p1'].value,
  duplicarInicio:  campos['cmv40-duplicate-p1'].value,
  duplicarFinal:   campos['cmv40-duplicate-fin-p1'].value,
  tocados: Object.entries(campos)
    .filter(([, v]) => v.dataset.tocado).map(([k]) => k).sort(),
});
const out = {};
_cmv40RellenarDesplazamiento('p1', 29);
out.adelantado = leer();
for (const k in campos) { campos[k].value = '0'; delete campos[k].dataset.tocado; }
_cmv40RellenarDesplazamiento('p1', -29);
out.retrasado = leer();
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node no disponible")
class TestElDesplazamientoNoCambiaElRecuento(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        guion = motor_i18n() + "\n" + _DRIVER.replace(
            "__CUERPO__", _fn("_cmv40RellenarDesplazamiento"))
        r = subprocess.run(argv_node(guion), capture_output=True, text=True,
                           timeout=60)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        cls.m = json.loads(r.stdout)

    def test_el_target_adelantado_se_recorta_por_delante(self):
        # src[i] ↔ tgt[i+29]: sobra por delante, falta por detrás.
        self.assertEqual(self.m["adelantado"],
                         {"quitarInicio": 29, "quitarFinal": 0,
                          "duplicarInicio": 0, "duplicarFinal": 29,
                          "tocados": self.m["adelantado"]["tocados"]})

    def test_y_el_retrasado_al_reves(self):
        self.assertEqual(
            {k: v for k, v in self.m["retrasado"].items() if k != "tocados"},
            {"quitarInicio": 0, "quitarFinal": 29,
             "duplicarInicio": 29, "duplicarFinal": 0})

    def test_lo_que_se_quita_se_repone(self):
        # Es la propiedad entera: el Δ no se mueve, sólo el contenido.
        for caso in ("adelantado", "retrasado"):
            with self.subTest(caso):
                c = self.m[caso]
                quitados = c["quitarInicio"] + c["quitarFinal"]
                puestos = c["duplicarInicio"] + c["duplicarFinal"]
                self.assertEqual(quitados, puestos)

    def test_las_cuatro_casillas_quedan_marcadas(self):
        # Sin la marca, el repintado del poll las devuelve a cero a los dos
        # segundos — el fallo que `anclajeDeFormulario` arregló en su día.
        self.assertEqual(self.m["adelantado"]["tocados"], [
            "cmv40-duplicate-fin-p1", "cmv40-duplicate-p1",
            "cmv40-remove-fin-p1", "cmv40-remove-p1"])


class TestCuandoSeOfrece(unittest.TestCase):
    """La condición vive en `_renderCMv40SyncControls`; se comprueba el
    criterio, que es lo que decide si el usuario ve la salida."""

    CASOS = [
        # (delta, canConfirm, offset, confianza, se_ofrece)
        (0, False, 29, 0.93, True),    # el caso de Drive
        (0, True, 29, 0.93, False),    # ya se puede confirmar: no estorbar
        (-426, False, 29, 0.93, False),  # primero cuadrar el recuento
        (0, False, 0, 0.93, False),    # no hay desplazamiento que aplicar
        (0, False, 29, 0.2, False),    # la correlación no está segura
    ]

    def _aplica(self, delta, can, offset, conf):
        return bool(delta == 0 and not can and abs(offset) > 0 and conf >= 0.5)

    def test_el_criterio(self):
        for delta, can, offset, conf, esperado in self.CASOS:
            with self.subTest(delta=delta, can=can, offset=offset, conf=conf):
                self.assertEqual(self._aplica(delta, can, offset, conf),
                                 esperado)

    @unittest.skipUnless(NODE, "node no disponible")
    def test_y_el_render_usa_ESE_criterio(self):
        """Ejecutando la expresión real del render, no una copia."""
        src = _fn("_renderCMv40SyncControls")
        i = src.index("const desplazamiento =")
        expr = src[i:src.index(";", i) + 1]
        guion = "\n".join([
            "'use strict';",
            "const casos = " + json.dumps(self.CASOS) + ";",
            "const out = casos.map(([delta, canConfirm, off, conf]) => {",
            "  const suggested = {offset: off, confidence: conf};",
            "  " + expr,
            "  return desplazamiento;",
            "});",
            "process.stdout.write(JSON.stringify(out));",
        ])
        r = subprocess.run(argv_node(guion), capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        for (delta, can, off, conf, esperado), val in zip(self.CASOS,
                                                          json.loads(r.stdout)):
            with self.subTest(delta=delta, off=off, conf=conf):
                self.assertEqual(bool(val), esperado)
                if esperado:
                    self.assertEqual(val, off)


if __name__ == "__main__":
    unittest.main()
