"""«hace 258h 18 min» — una edad no se cuenta con el formateador de duraciones.

Lo reportó el usuario mirando el historial de la columna de trabajo: un
trabajo de hace diez días decía «hace 258h 18 min». El número era correcto y
no contestaba la pregunta.

La causa es la de siempre en este repo: **dos definiciones de lo mismo**.
Había dos formateadores de edad —`formatRelativeDate` (las tarjetas de las
tres columnas de proyecto) y `_workbarHace` (el historial de la columna de
trabajo)— y sólo el primero tenía escalón de días. El segundo componía la
edad con `_workbarTiempo`, que mide **duraciones**: ahí «258 h 18 min»
describe perfectamente un trabajo que tardase eso, y por eso no baja de las
horas. Nadie estaba equivocado por separado.

Hoy hay una sola: `hace()` en `core.js`, con escalones hasta los años. La
fecha exacta no se pierde — sigue en el `metaTooltip` de las tres columnas y
en la cabecera de día del historial.

Estos tests EJECUTAN la función en node. Mirar el fuente no valdría: el bug
era que una de las dos llamaba a la otra, y eso se lee bien.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))
from frontend_sources import (argv_node, catalogo_es,  # noqa: E402
                              js_en_disco, motor_en_disco, pieza_de)

NODE = shutil.which("node")

# `hace()` llega con el motor (`motor_i18n`), así que el driver sólo añade lo
# que quiere medir encima: los dos consumidores.
_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.JS_CONCAT, 'utf8');

function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('funcion no encontrada: ' + name);
  let depth = 0, abierto = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { depth++; abierto = true; }
    else if (src[j] === '}') { depth--; if (abierto && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error('funcion sin cerrar: ' + name);
}

// El motor trae `tr()` y `hace()`; llega por FICHERO porque son 130 KB y
// el tope de 128 KiB de Linux aplica también al entorno.
const bundle = [
  fs.readFileSync(process.env.MOTOR_I18N, 'utf8'),
  grab('formatRelativeDate'),
  grab('_workbarHace'),
  grab('_workbarTiempo'),
  grab('_workbarDia'),
  'return { hace, formatRelativeDate, _workbarHace, _workbarTiempo, _workbarDia };',
].join('\n');
const api = new Function(bundle)();

const casos = JSON.parse(fs.readFileSync(0, 'utf8'));
const AHORA = Date.parse('2026-09-23T18:00:00Z');
const _now = Date.now;
Date.now = () => AHORA;

const salida = casos.map(c => {
  const iso = c.hace_segundos === null
    ? c.iso
    : new Date(AHORA - c.hace_segundos * 1000).toISOString();
  return {
    hace:     api.hace(iso),
    tarjeta:  api.formatRelativeDate(iso),
    columna:  api._workbarHace(iso),
    dia:      api._workbarDia(iso),
    duracion: c.hace_segundos === null ? null
                                       : api._workbarTiempo(c.hace_segundos),
  };
});
Date.now = _now;
process.stdout.write(JSON.stringify(salida));
"""

DIA = 86400
HORA = 3600


def _casos(*items):
    return [{"hace_segundos": s, "iso": None} if not isinstance(s, str)
            else {"hace_segundos": None, "iso": s} for s in items]


@unittest.skipIf(NODE is None, "node no está instalado")
class _EnNode(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.js = js_en_disco()
        cls.motor = motor_en_disco()

    def evaluar(self, *entradas):
        env = {**os.environ, "JS_CONCAT": self.js,
               "MOTOR_I18N": self.motor}
        r = subprocess.run(argv_node(_DRIVER), input=json.dumps(_casos(*entradas)),
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        return json.loads(r.stdout)


class TestLosEscalones(_EnNode):

    def test_el_caso_reportado_son_diez_dias_no_doscientas_horas(self):
        """258 h 18 min es lo que el usuario leyó en el historial."""
        [r] = self.evaluar(258 * HORA + 18 * 60)
        self.assertEqual(r["columna"], "hace 10 días")
        self.assertEqual(r["hace"], "hace 10 días")
        # Y la duración SIGUE contándose en horas: no se ha roto el otro
        # formateador, que para un trabajo de 258 h diría justamente eso.
        self.assertEqual(r["duracion"], "258 h 18 min")

    def test_la_escala_completa(self):
        esperado = [
            (30,             "ahora mismo"),
            (59,             "ahora mismo"),
            (60,             "hace 1 min"),
            (5 * 60,         "hace 5 min"),
            (59 * 60,        "hace 59 min"),
            (HORA,           "hace 1 h"),
            (23 * HORA,      "hace 23 h"),
            (DIA,            "hace 1 día"),
            (2 * DIA,        "hace 2 días"),
            (29 * DIA,       "hace 29 días"),
            (30 * DIA,       "hace 1 mes"),
            (61 * DIA,       "hace 2 meses"),
            (364 * DIA,      "hace 11 meses"),
            (365 * DIA,      "hace 1 año"),
            (800 * DIA,      "hace 2 años"),
        ]
        rs = self.evaluar(*[s for s, _ in esperado])
        obtenido = [(s, r["hace"]) for (s, _), r in zip(esperado, rs)]
        self.assertEqual(obtenido, esperado)

    def test_ningun_escalon_se_queda_en_cero(self):
        """`floor` sobre 30,44 días daría «hace 0 meses» justo al cruzar."""
        rs = self.evaluar(30 * DIA, 31 * DIA, 365 * DIA, 366 * DIA)
        for r in rs:
            self.assertNotIn(" 0 ", r["hace"], r["hace"])

    def test_el_singular_va_en_su_propia_clave(self):
        """El plural de «dia» en catalán es «dies»: un sufijo no vale."""
        cat = catalogo_es()
        for base in ("comun.hace_dia", "comun.hace_mes", "comun.hace_anio"):
            self.assertIn(f"{base}_uno", cat)
            self.assertIn(f"{base}_varios", cat)

    def test_sin_fecha_no_se_inventa_nada(self):
        rs = self.evaluar("", "no soy una fecha")
        for r in rs:
            self.assertEqual(r["hace"], "")
            self.assertEqual(r["columna"], "")
            # La tarjeta pone el guion, que es SU decisión, no la de `hace()`.
            self.assertEqual(r["tarjeta"], "—")

    def test_una_fecha_futura_no_sale_en_negativo(self):
        [r] = self.evaluar(-5 * HORA)
        self.assertEqual(r["hace"], "ahora mismo")


class TestLasDosColumnasDicenLoMismo(_EnNode):
    """Eran dos escalas y ésa era la avería. Ahora es una."""

    def test_tarjeta_y_columna_coinciden_en_toda_la_escala(self):
        rs = self.evaluar(30, 5 * 60, 3 * HORA, DIA, 10 * DIA, 45 * DIA,
                          200 * DIA, 400 * DIA)
        for r in rs:
            self.assertEqual(r["tarjeta"], r["columna"], r)

    def test_la_tarjeta_ya_no_cae_a_una_fecha_corta(self):
        """Pasada la semana devolvía «12/09/26»; hoy sigue siendo una edad.

        La fecha exacta no se pierde: las tres columnas la llevan en
        `metaTooltip` y el historial, en su cabecera de día.
        """
        rs = self.evaluar(10 * DIA, 200 * DIA)
        for r in rs:
            self.assertTrue(r["tarjeta"].startswith("hace "), r["tarjeta"])
            self.assertNotIn("/", r["tarjeta"])


class TestLaCabeceraDeDiaHabla(_EnNode):
    """«Hoy» y «Ayer» estaban escritos a mano en el JS, en castellano."""

    def test_hoy_y_ayer_salen_del_catalogo(self):
        cat = catalogo_es()
        self.assertEqual(cat.get("workbar.dia_hoy"), "Hoy")
        self.assertEqual(cat.get("workbar.dia_ayer"), "Ayer")

    def test_y_la_funcion_los_usa(self):
        rs = self.evaluar(60, 25 * HORA)
        self.assertEqual(rs[0]["dia"], "Hoy")
        self.assertEqual(rs[1]["dia"], "Ayer")


def _cuerpo_de(funcion: str) -> str:
    """El texto de esa función, de la llave que abre a la que cierra."""
    _pieza, src = pieza_de(funcion)
    i = src.index(f"function {funcion}(")
    prof, abierto = 0, False
    for j in range(i, len(src)):
        if src[j] == "{":
            prof += 1
            abierto = True
        elif src[j] == "}":
            prof -= 1
            if abierto and prof == 0:
                return src[i:j + 1]
    raise AssertionError(f"{funcion}: no cierra")


class TestNadieVuelveATenerSuPropiaEscala(unittest.TestCase):
    """El guard: una segunda definición de «cuánto hace» es el bug de vuelta.

    No se mira la forma de `hace()` —eso lo hacen los tests de arriba, que la
    ejecutan— sino que nadie más componga una edad por su cuenta.
    """

    def test_la_edad_no_se_compone_con_el_formateador_de_duraciones(self):
        # `_workbarTiempo` mide duraciones. Si vuelve a aparecer dentro del
        # que da la edad, estamos otra vez en el bug.
        cuerpo = _cuerpo_de("_workbarHace")
        self.assertNotIn("_workbarTiempo", cuerpo,
                         "_workbarHace vuelve a componer la edad con el "
                         "formateador de DURACIONES: eso no baja de las horas")
        self.assertIn("hace(iso)", cuerpo)

    def test_solo_hay_una_funcion_que_traduzca_un_iso_a_una_edad(self):
        # La firma del bug: repartir a mano un intervalo en minutos, horas y
        # días. Sólo `hace()` puede hacerlo.
        sospechosas = [n for n in ("hace", "formatRelativeDate", "_workbarHace")
                       if "Date.now()" in _cuerpo_de(n)]
        self.assertEqual(sospechosas, ["hace"],
                         "la escala de edades tiene que vivir en un solo "
                         f"sitio; la reparten: {sospechosas}")

    def test_hace_viaja_con_el_motor_para_los_arneses(self):
        """Quince arneses de node se rompieron a la vez sin esto."""
        from frontend_sources import motor_i18n
        self.assertIn("function hace(iso)", motor_i18n())


if __name__ == "__main__":
    unittest.main()
