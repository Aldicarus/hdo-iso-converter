"""El sufijo del tiempo restante de la timeline CMv4.0 — test de COMPORTAMIENTO.

Por qué no basta con mirar el fuente: la primera versión de este aviso usaba
`!session.target_type` como señal de "el pre-flight todavía no ha clasificado
el bin". Sobre el papel se leía bien y los tests de forma pasaban, pero el
modelo inicializa ese campo a `'generic'`, así que la condición era SIEMPRE
falsa. Dos cosas quedaron muertas sin que nadie lo notara:

  - el aviso de estimación provisional no llegó a mostrarse nunca, y
  - `rutaDesconocida` tampoco se activaba, con lo que el ETA de arranque
    asumía siempre ruta merge (la cara) en vez de repartir por `share_dropin`
    — de ahí los "49 min restantes" en jobs que duraban la mitad.

El campo que sí nace vacío es `target_dv_info`, que el pre-flight rellena en
la misma línea que `target_type`. Estos tests ejecutan las funciones reales de
app.js en node contra sesiones sintéticas, y anclan el default del modelo para
que un cambio ahí no vuelva a dejar la señal muerta en silencio.
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
from frontend_sources import (argv_node, js_completo,  # noqa: E402
                              js_en_disco, motor_en_disco)

NODE = shutil.which("node")


# Extrae del app.js real las funciones implicadas y las evalúa juntas, sin
# DOM ni resto del fichero. Lee casos por stdin y devuelve resultados.
_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.JS_CONCAT, 'utf8');

function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('funcion no encontrada en el JS: ' + name);
  let depth = 0, abierto = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { depth++; abierto = true; }
    else if (src[j] === '}') { depth--; if (abierto && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error('funcion sin cerrar: ' + name);
}

const iOrder = src.indexOf('const CMV40_PHASES_ORDER');
const orderSrc = src.slice(iOrder, src.indexOf('];', iOrder) + 2);

const bundle = [
  // El motor de traducción: el rótulo del tiempo restante vive en el
  // catálogo, así que las funciones que lo pintan llaman a `tr()`. Sin esto
  // el bundle muere con «tr is not defined» — y el fallo señalaría al código,
  // que está bien. Llega por FICHERO, no por variable de entorno: son 130 KB
  // y el tope de 128 KiB de Linux aplica también al entorno.
  fs.readFileSync(process.env.MOTOR_I18N, 'utf8'),
  orderSrc,
  grab('_cmv40FmtClock'),
  grab('_cmv40BinClasificado'),
  grab('_cmv40SufijoEta'),
  grab('_cmv40TextoRestante'),
  'return { _cmv40SufijoEta, _cmv40TextoRestante };',
].join('\n');

const api = new Function(bundle)();
const casos = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(casos.map(c => ({
  sufijo: api._cmv40SufijoEta(c.s),
  texto:  api._cmv40TextoRestante(c.secs, c.s),
}))));
"""


@unittest.skipUnless(NODE, "node no disponible")
class TestSufijoEtaComportamiento(unittest.TestCase):
    def _evaluar(self, casos):
        proc = subprocess.run(
            argv_node(_DRIVER),
            env={**os.environ, "JS_CONCAT": js_en_disco(),
                 "MOTOR_I18N": motor_en_disco()},
            input=json.dumps(casos), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_preflight_en_curso_marca_la_estimacion_como_provisional(self):
        """Sesión recién creada: target_type ya viene a 'generic' por default
        del modelo, pero el bin aún no está analizado."""
        r = self._evaluar([{"secs": 1800, "s": {
            "phase": "created", "target_type": "generic", "target_dv_info": None,
        }}])[0]
        self.assertEqual(r["sufijo"], "(estimado inicial)")
        self.assertIn("(estimado inicial)", r["texto"])

    def test_bin_ya_clasificado_da_el_eta_por_bueno(self):
        r = self._evaluar([{"secs": 1800, "s": {
            "phase": "created", "target_type": "trusted_p7_fel_final",
            "target_dv_info": {"profile": 7, "cm_version": "v4.0"},
        }}])[0]
        self.assertEqual(r["sufijo"], "(auto)")

    def test_tras_los_gates_nunca_es_provisional(self):
        """A partir de target_provided la ruta es un hecho, no una predicción."""
        for fase in ("target_provided", "extracted", "injected", "remuxed"):
            r = self._evaluar([{"secs": 600, "s": {
                "phase": fase, "target_type": "generic",
                "target_dv_info": {"profile": 8},
            }}])[0]
            self.assertEqual(r["sufijo"], "(auto)", f"fase {fase}")

    def test_sin_tiempo_restante_no_se_inventa_sufijo(self):
        r = self._evaluar([{"secs": 0, "s": {
            "phase": "created", "target_type": "generic", "target_dv_info": None,
        }}])[0]
        self.assertNotIn("restantes", r["texto"])


class TestSenalDeRutaDesconocida(unittest.TestCase):
    """Ancla el contrato entre modelo y frontend: cuál de los dos campos vale
    como señal de "el pre-flight aún no ha clasificado el bin"."""

    def test_target_type_no_sirve_como_señal_nace_relleno(self):
        from models import CMv40Session
        s = CMv40Session(id="x", source_mkv_path="/a.mkv", source_mkv_name="a.mkv")
        self.assertTrue(s.target_type,
                        "si target_type pasa a nacer vacío, revisar "
                        "_cmv40BinClasificado: podría volver a usarse y sería "
                        "correcto, pero hoy la señal es target_dv_info")

    def test_target_dv_info_si_sirve_nace_vacio(self):
        from models import CMv40Session
        s = CMv40Session(id="x", source_mkv_path="/a.mkv", source_mkv_name="a.mkv")
        self.assertIsNone(s.target_dv_info)

    def test_el_preflight_rellena_ambos_a_la_vez(self):
        """Si se separan, la señal deja de ser fiable."""
        src = (APP_DIR / "phases" / "cmv40_pipeline.py").read_text(encoding="utf-8")
        i = src.find("async def _preflight_validate_bin")
        j = src.find("\nasync def ", i + 10)
        cuerpo = src[i:j if j > 0 else len(src)]
        self.assertIn("session.target_dv_info = dovi_info", cuerpo)
        self.assertIn("session.target_type = _classify_target_type(dovi_info)", cuerpo)

    def test_frontend_no_usa_target_type_como_booleano(self):
        js = js_completo()
        codigo = "\n".join(l for l in js.splitlines()
                           if not l.lstrip().startswith(("*", "//", "/*")))
        self.assertNotIn("!s.target_type", codigo)
        self.assertNotIn("!session.target_type", codigo)

    def test_ambos_consumidores_comparten_el_helper(self):
        """El sufijo y el reparto por share_dropin deben decidir con la misma
        señal: si divergen, el aviso dice una cosa y el número refleja otra."""
        js = js_completo()
        self.assertIn("const rutaDesconocida = !gatesHechos && !_cmv40BinClasificado(s);", js)
        self.assertIn("const rutaPorSaber = !_cmv40BinClasificado(s)", js)


class TestRotuloDelTiempoRestante(unittest.TestCase):
    """Las barras de progreso rotulan el tiempo que falta como "Restante".

    "ETA" es jerga en inglés y el proyecto pide los textos de UI en
    castellano. Aparecía en cinco sitios distintos (escaneo PGS de Tab 1 y
    Tab 2, phase strip de la cola, pasos de la timeline CMv4.0 y barra del
    overlay de ejecución); el guard evita que vuelva por uno solo de ellos.
    """

    _FICHEROS = ("static/index.html",)   # el JS va por `js_completo()`

    def test_no_queda_ningun_literal_eta_visible(self):
        fuentes = [(rel, (APP_DIR / rel).read_text(encoding="utf-8"))
                   for rel in self._FICHEROS] + [("el JS", js_completo())]
        for rel, texto in fuentes:
            for i, linea in enumerate(texto.splitlines(), 1):
                limpia = linea.strip()
                if limpia.startswith(("*", "//", "/*", "<!--")):
                    continue        # comentarios: ahí "ETA" es descripción
                for delim in ("`ETA ", "'ETA ", '"ETA ', ">ETA "):
                    self.assertNotIn(delim, linea, f"{rel}:{i} — {limpia[:90]}")

    def test_los_sitios_conocidos_dicen_restante(self):
        """El rótulo vive en `comun.restante_p1`, así que lo que se fija es
        que los sitios conocidos pidan ESA clave.

        Antes se contaba la frase literal en el fuente; con el catálogo eso
        pasaría a contar nada en cuanto alguien tradujera el sitio, que es
        justo lo que pasó. La clave es lo estable."""
        js = js_completo()
        self.assertEqual(js.count("tr('comun.restante_p1'"), 8)
        cat = json.loads((APP_DIR / "static" / "i18n" / "es.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(cat["comun.restante_p1"], "Restante {p1}")
        # Los seis sitios, cada uno con el dato que le toca. Lo que importa
        # es CUÁL de los dos niveles de progreso alimenta a cada uno: la
        # tarjeta y la cabecera de la timeline llevan el del TRABAJO
        # (`a.eta_s`) y el bloque del log el de la FASE (`fase.eta_s`) — ver
        # `test_dos_niveles_de_progreso`.
        for expr, veces in (
                ("{p1: _workbarTiempo(a.eta_s)}", 2),
                ("{p1: _workbarTiempo(fase.eta_s)}", 1),
                ("{p1: _cmv40FmtEta(st.etaSecs)}", 2),
                ("{p1: `${em}:${es}`}", 2),
        ):
            self.assertEqual(
                js.count(f"tr('comun.restante_p1', {expr})"), veces, expr)
        self.assertIn(
            "tr('comun.restante_p1', {p1: `${m}:${String(s).padStart(2, '0')}`})", js)


if __name__ == "__main__":
    unittest.main()
