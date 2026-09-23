# -*- coding: utf-8 -*-
"""Los seis temas del 2026-09-23 por la tarde, los que se pueden ejecutar.

Salieron de probar la app tras el despliegue de la mañana. Aquí están los
que tienen comportamiento que medir; el esqueleto del modal y el formato de
la card de luminancia se miden en Chrome, con los guards de su pestaña.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_backlog_2026_09_23_tarde -v
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

from frontend_sources import (argv_node, js_en_disco,  # noqa: E402
                              maquinaria_del_historial, motor_en_disco,
                              pieza_de)

NODE = shutil.which("node")


def _fn(nombre: str) -> str:
    """El fuente de una función, de donde esté declarada."""
    _pieza, src = pieza_de(nombre)
    i = src.index(f"function {nombre}(")
    ini = src.rindex("\n", 0, i) + 1
    prof, abierto = 0, False
    for j in range(i, len(src)):
        if src[j] == "{":
            prof += 1
            abierto = True
        elif src[j] == "}":
            prof -= 1
            if abierto and prof == 0:
                return src[ini:j + 1]
    raise AssertionError(f"{nombre}: no cierra")


def _codigo(nombre: str) -> str:
    """El fuente sin las líneas de comentario.

    Los comentarios de este repo citan el nombre de lo que se acaba de
    quitar —«Sin `_workbarPasaFiltro`: …»—, así que un `assertNotIn` sobre
    el fuente crudo se dispara con su propia explicación.
    """
    return "\n".join(l for l in _fn(nombre).splitlines()
                      if not l.strip().startswith(("//", "*", "/*")))


# ════════════════════════════════════════════════════════════════════
#  2 · El histórico se filtra en el SERVIDOR
# ════════════════════════════════════════════════════════════════════

class TestElHistorialFiltraSobreElTodo(unittest.TestCase):
    """El filtro se aplicaba sobre las 25 líneas cargadas, no sobre el fichero.

    Síntoma que reportó el usuario: buscar algo que existe devolvía vacío,
    «Ver más» no parecía hacer nada y tras varios clics aparecía una
    coincidencia — cada clic traía 25 registros más y casi todos se
    descartaban otra vez en el navegador.
    """

    def setUp(self):
        import tempfile
        import historial
        self.tmp = tempfile.mkdtemp()
        self._ruta_real = historial.ruta
        historial.ruta = lambda: Path(self.tmp) / "historial.jsonl"
        self.historial = historial

    def tearDown(self):
        self.historial.ruta = self._ruta_real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _escribir(self, filas):
        with open(self.historial.ruta(), "w", encoding="utf-8") as f:
            for fila in filas:
                f.write(json.dumps(fila) + "\n")

    def _muchos(self, n=120):
        # La aguja va al FONDO del fichero, o sea entre las más antiguas: es
        # justo la que un límite de 25 sobre las recientes no alcanza.
        filas = [{"id": "aguja", "tab": "cmv40", "tipo": "x",
                  "que": "Predator Badlands", "inicio": "2026-01-01T00:00:00Z",
                  "estado": "done"}]
        filas += [{"id": f"p{i}", "tab": "rip", "tipo": "x",
                   "que": f"Otra cosa {i}", "inicio": "2026-02-01T00:00:00Z",
                   "estado": "done"} for i in range(n)]
        self._escribir(filas)

    def test_la_encuentra_aunque_este_fuera_de_la_primera_pagina(self):
        self._muchos()
        trabajos, hay_mas = self.historial.buscar(25, q="predator")
        self.assertEqual([t["id"] for t in trabajos], ["aguja"])
        self.assertFalse(hay_mas)

    def test_sin_el_filtro_en_el_servidor_no_saldria(self):
        """El contraste: las 25 primeras no la contienen."""
        self._muchos()
        primeras, _ = self.historial.buscar(25)
        self.assertNotIn("aguja", [t["id"] for t in primeras])

    def test_busca_sin_acentos_y_sin_mayusculas(self):
        self._escribir([{"id": "a", "tab": "rip", "tipo": "x",
                         "que": "La Momia — Edición Especial",
                         "inicio": "2026-01-01T00:00:00Z", "estado": "done"}])
        for aguja in ("momia", "MOMIA", "edicion", "Edición"):
            with self.subTest(aguja):
                self.assertEqual(len(self.historial.buscar(25, q=aguja)[0]), 1)

    def test_el_pill_de_pestana_filtra_por_id_y_no_por_rotulo(self):
        """El rótulo cambia con el idioma; en el fichero está el id."""
        self._escribir([
            {"id": "a", "tab": "rip", "tipo": "x", "que": "uno",
             "inicio": "2026-01-01T00:00:00Z", "estado": "done"},
            {"id": "b", "tab": "cmv40", "tipo": "x", "que": "dos",
             "inicio": "2026-01-02T00:00:00Z", "estado": "done"},
        ])
        self.assertEqual([t["id"] for t in self.historial.buscar(25, tab="cmv40")[0]],
                         ["b"])

    def test_hay_mas_lo_dice_el_servidor_y_no_el_recuento(self):
        """Con el total múltiplo exacto del paso, contar falla.

        Es el caso que deja el botón «Ver más» ofreciendo una página vacía:
        vienen 25 de 25, el cliente deduce que hay más y no hay ninguna.
        """
        self._escribir([{"id": f"p{i}", "tab": "rip", "tipo": "x",
                         "que": f"x{i}", "inicio": "2026-01-01T00:00:00Z",
                         "estado": "done"} for i in range(25)])
        trabajos, hay_mas = self.historial.buscar(25)
        self.assertEqual(len(trabajos), 25)
        self.assertFalse(hay_mas, "no quedaba ninguna detrás")
        self.assertTrue(self.historial.buscar(24)[1])

    def test_leer_sigue_devolviendo_una_lista(self):
        """La firma de siempre no cambia: la usan diez sitios."""
        self._escribir([{"id": "a", "tab": "rip", "tipo": "x", "que": "u",
                         "inicio": "2026-01-01T00:00:00Z", "estado": "done"}])
        self.assertIsInstance(self.historial.leer(), list)


@unittest.skipUnless(NODE, "node no disponible")
class TestLaColumnaPideElFiltroAlServidor(unittest.TestCase):

    _DRIVER = r"""
const fs = require('fs');
const pedidas = [];
globalThis.apiFetch = async (url) => { pedidas.push(url);
  return { trabajos: [], hay_mas: false }; };
globalThis.document = { getElementById: (id) =>
  id === 'workbar-search' ? { value: globalThis.__q || '' } : null };
globalThis.workbarEstado = { recientes: [] };
globalThis._workbarRenderHistorial = () => {};
globalThis.normalizeSearch = (s) => (s || '').toLowerCase();
eval(fs.readFileSync(process.env.HISTORIAL, 'utf8'));
(async () => {
  await _workbarCargarHistorial();            // sin filtro
  globalThis.__q = 'predator';
  _workbarTopeHistorial = 75;                 // el usuario dio a «ver más»
  await _workbarCargarHistorial();            // con filtro
  _workbarFiltroTab = 'cmv40';
  await _workbarCargarHistorial();
  console.log(JSON.stringify({ pedidas, tope: _workbarTopeHistorial }));
})();
"""

    def _correr(self):
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                        encoding="utf-8")
        f.write(maquinaria_del_historial())
        f.close()
        r = subprocess.run(argv_node(self._DRIVER),
                           env={**os.environ, "HISTORIAL": f.name},
                           capture_output=True, text=True, timeout=30)
        os.unlink(f.name)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1200:]}")
        return json.loads(r.stdout)

    def test_el_filtro_viaja_en_la_peticion(self):
        out = self._correr()
        self.assertNotIn("q=", out["pedidas"][0])
        self.assertIn("q=predator", out["pedidas"][1])
        self.assertIn("tab=cmv40", out["pedidas"][2])

    def test_el_hay_mas_sale_de_la_respuesta(self):
        """Con `hay_mas: false` y 0 trabajos, el botón no puede quedarse."""
        cuerpo = _codigo("_workbarCargarHistorial")
        self.assertIn("r.hay_mas", cuerpo)
        self.assertNotIn(">= _workbarTopeHistorial", cuerpo)

    def test_al_cambiar_el_filtro_se_vuelve_al_paso_uno(self):
        cuerpo = _fn("_workbarRefiltrarHistorial")
        self.assertIn("_workbarTopeHistorial = _WORKBAR_HISTORIAL_PASO", cuerpo)

    def test_el_render_ya_no_vuelve_a_filtrar(self):
        """Filtrar dos veces no quita nada, pero esconde dónde se decide."""
        self.assertNotIn("_workbarPasaFiltro", _codigo("_workbarRenderHistorial"))


# ════════════════════════════════════════════════════════════════════
#  4 · El zoom del gráfico de sincronización
# ════════════════════════════════════════════════════════════════════

@unittest.skipUnless(NODE, "node no disponible")
class TestElZoomDelGraficoDeSync(unittest.TestCase):
    """Presets que centran, suelo de un segundo y tiempo en vez de frames."""

    _DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.JS_CONCAT, 'utf8');
function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('no encontrada: ' + name);
  let d = 0, ab = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { d++; ab = true; }
    else if (src[j] === '}') { d--; if (ab && d === 0) return src.slice(i, j + 1); }
  }
  throw new Error('sin cerrar: ' + name);
}
const api = new Function([
  fs.readFileSync(process.env.MOTOR_I18N, 'utf8'),
  src.slice(src.indexOf('const CMV40_ZOOM_MIN_SEG'),
            src.indexOf(';', src.indexOf('const CMV40_ZOOM_MIN_SEG')) + 1),
  grab('_cmv40Encuadrar'), grab('_cmv40FrameATiempo'), grab('_cmv40TiempoAFrame'),
  'return { _cmv40Encuadrar, _cmv40FrameATiempo, _cmv40TiempoAFrame, CMV40_ZOOM_MIN_SEG };',
].join('\n'))();
const casos = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(casos.map(c => api[c.fn](...c.args))));
"""

    def _llamar(self, *casos):
        r = subprocess.run(
            argv_node(self._DRIVER),
            env={**os.environ, "JS_CONCAT": js_en_disco(),
                 "MOTOR_I18N": motor_en_disco()},
            input=json.dumps([{"fn": f, "args": a} for f, a in casos]),
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        return json.loads(r.stdout)

    FPS = 24
    TOTAL = 24 * 60 * 120          # dos horas

    def test_un_preset_centra_en_lo_que_estas_mirando(self):
        """30 min desde el minuto 48 → del 33 al 63, no del 0 al 30."""
        centro = 48 * 60 * self.FPS
        [r] = self._llamar(("_cmv40Encuadrar",
                            [centro, 30 * 60 * self.FPS, self.TOTAL, self.FPS]))
        self.assertEqual(r["start"] / self.FPS / 60, 33)
        self.assertEqual(r["end"] / self.FPS / 60, 63)

    def test_en_los_extremos_se_desplaza_en_vez_de_recortarse(self):
        """Pedir 30 min en el minuto 2 sigue enseñando 30 min."""
        [a, b] = self._llamar(
            ("_cmv40Encuadrar", [2 * 60 * self.FPS, 30 * 60 * self.FPS,
                                 self.TOTAL, self.FPS]),
            ("_cmv40Encuadrar", [119 * 60 * self.FPS, 30 * 60 * self.FPS,
                                 self.TOTAL, self.FPS]))
        self.assertEqual((a["start"], a["end"] - a["start"]),
                         (0, 30 * 60 * self.FPS))
        self.assertEqual(b["end"], self.TOTAL)
        self.assertEqual(b["end"] - b["start"], 30 * 60 * self.FPS)

    def test_el_suelo_es_un_segundo(self):
        """Por debajo no queda curva que mirar."""
        [r] = self._llamar(("_cmv40Encuadrar",
                            [1000, 3, self.TOTAL, self.FPS]))
        self.assertGreaterEqual(r["end"] - r["start"], self.FPS)

    def test_pedir_mas_que_la_pelicula_da_la_pelicula(self):
        [r] = self._llamar(("_cmv40Encuadrar",
                            [1000, self.TOTAL * 5, self.TOTAL, self.FPS]))
        self.assertEqual((r["start"], r["end"]), (0, self.TOTAL))

    def test_el_encuadre_se_escribe_en_tiempo(self):
        [a, b, c] = self._llamar(
            ("_cmv40FrameATiempo", [0, self.FPS]),
            ("_cmv40FrameATiempo", [83 * self.FPS, self.FPS]),
            ("_cmv40FrameATiempo", [(3723) * self.FPS, self.FPS]))
        self.assertEqual([a, b, c], ["0:00:00", "0:01:23", "1:02:03"])

    def test_y_se_lee_en_las_tres_formas(self):
        vals = self._llamar(
            ("_cmv40TiempoAFrame", ["1:02:03", self.FPS]),
            ("_cmv40TiempoAFrame", ["2:03", self.FPS]),
            ("_cmv40TiempoAFrame", ["45", self.FPS]),
            ("_cmv40TiempoAFrame", ["", self.FPS]),
            ("_cmv40TiempoAFrame", ["mañana", self.FPS]),
            ("_cmv40TiempoAFrame", ["1:2:3:4", self.FPS]))
        self.assertEqual(vals, [3723 * self.FPS, 123 * self.FPS, 45 * self.FPS,
                                None, None, None])

    _DRIVER_PRESET = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.JS_CONCAT, 'utf8');
function grab(n) {
  const i = src.indexOf('function ' + n + '(');
  let d = 0, ab = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { d++; ab = true; }
    else if (src[j] === '}') { d--; if (ab && d === 0) return src.slice(i, j + 1); }
  }
  throw new Error('sin cerrar: ' + n);
}
const caso = JSON.parse(fs.readFileSync(0, 'utf8'));
let puesto = null;
const api = new Function([
  fs.readFileSync(process.env.MOTOR_I18N, 'utf8'),
  src.slice(src.indexOf('const CMV40_ZOOM_MIN_SEG'),
            src.indexOf(';', src.indexOf('const CMV40_ZOOM_MIN_SEG')) + 1),
  'const openCMv40Projects = ' + JSON.stringify([caso.project]) + ';',
  'function _cmv40SetRange(pid, start, end) { globalThis.__puesto = {start, end}; }',
  grab('_cmv40Encuadrar'), grab('_cmv40DatosDelZoom'),
  grab('_cmv40ZoomPreset'), grab('_cmv40ZoomFuera'),
  'return { _cmv40ZoomPreset, _cmv40ZoomFuera };',
].join('\n'))();
api[caso.fn]('p1', caso.arg);
process.stdout.write(JSON.stringify(globalThis.__puesto));
"""

    def _preset(self, fn, arg, start, end):
        caso = {"fn": fn, "arg": arg, "project": {
            "id": "p1", "chartRange": {"start": start, "end": end},
            "session": {"source_fps": self.FPS},
            "syncData": {"source_frames": self.TOTAL}}}
        r = subprocess.run(
            argv_node(self._DRIVER_PRESET),
            env={**os.environ, "JS_CONCAT": js_en_disco(),
                 "MOTOR_I18N": motor_en_disco()},
            input=json.dumps(caso), capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        return json.loads(r.stdout)

    def test_el_preset_EJECUTADO_centra_en_la_vista_actual(self):
        """Mirando el minuto 48, «30 min» tiene que dar del 33 al 63.

        Este test ejecuta `_cmv40ZoomPreset`, no solo el encuadre: el bug
        estaba en QUÉ centro se le pasaba, y con `_cmv40Encuadrar` a secas
        seguía pasando en verde.
        """
        m = self.FPS * 60
        r = self._preset("_cmv40ZoomPreset", 30 * 60, 47 * m, 49 * m)
        self.assertEqual((r["start"] / m, r["end"] / m), (33, 63))

    def test_todo_es_todo_y_no_centra_nada(self):
        m = self.FPS * 60
        r = self._preset("_cmv40ZoomPreset", None, 47 * m, 49 * m)
        self.assertEqual((r["start"], r["end"]), (0, self.TOTAL))

    def test_alejarse_dobla_el_ancho_sin_mover_el_centro(self):
        m = self.FPS * 60
        r = self._preset("_cmv40ZoomFuera", None, 40 * m, 44 * m)
        self.assertEqual((r["start"] / m, r["end"] / m), (38, 46))

    def test_ida_y_vuelta(self):
        for seg in (0, 1, 59, 60, 3599, 3600, 7199):
            [txt] = self._llamar(("_cmv40FrameATiempo", [seg * self.FPS, self.FPS]))
            [fr] = self._llamar(("_cmv40TiempoAFrame", [txt, self.FPS]))
            with self.subTest(seg=seg):
                self.assertEqual(fr, seg * self.FPS)


class TestElGraficoSeEncuadraArrastrando(unittest.TestCase):
    """El arrastre no se puede medir sin un navegador, pero sí su cableado.

    Lo que aquí se fija son las dos trampas que costaron una reescritura: el
    `mouseup` tiene que escucharse en `window` —soltar fuera del gráfico es
    lo normal— y **solo mientras se arrastra**, o cada repintado apila un
    oyente más.
    """

    def test_el_mouseup_se_registra_al_empezar_a_arrastrar(self):
        cuerpo = _codigo("_renderCMv40Chart")
        i = cuerpo.index("canvas.onmousedown")
        j = cuerpo.index("};", i)
        self.assertIn("window.addEventListener('mouseup'", cuerpo[i:j],
                      "el oyente tiene que nacer dentro del mousedown")
        self.assertIn("once: true", cuerpo[i:j])

    def test_y_no_en_cada_repintado(self):
        """Fuera del `mousedown` no puede quedar ningún registro suelto."""
        cuerpo = _codigo("_renderCMv40Chart")
        i = cuerpo.index("canvas.onmousedown")
        self.assertNotIn("window.addEventListener('mouseup'", cuerpo[:i])

    def test_un_clic_sin_arrastrar_no_encuadra_nada(self):
        self.assertIn("< 6", _codigo("_renderCMv40Chart"))


# ════════════════════════════════════════════════════════════════════
#  5 · Fase D
# ════════════════════════════════════════════════════════════════════

class TestLaFaseDSeCierraAlPasarDeFase(unittest.TestCase):
    """`sync_verified` ES «el usuario ya confirmó», no «todavía puede editar».

    Con `>` el formulario de corrección seguía vivo durante la Fase F:
    campos de frames, «Aplicar corrección», «Volver al original» y
    «Confirmar», todos pulsables cuando el RPU ya se está inyectando.
    """

    def test_el_corte_incluye_sync_verified(self):
        cuerpo = _codigo("_renderCMv40SyncControls")
        self.assertIn("readOnly  = phaseIdx >= dDoneIdx", cuerpo)

    def test_el_orden_de_fases_pone_sync_verified_donde_se_espera(self):
        """Si `sync_verified` dejara de ser el destino de `mark-synced`, el
        corte de arriba apuntaría a otra cosa."""
        import re
        src = Path(APP_DIR / "routers" / "cmv40.py").read_text(encoding="utf-8")
        i = src.index("async def cmv40_mark_synced")
        j = src.index("\n@router.", i)
        self.assertTrue(re.search(r"session\.phase = CMv40Phase\.SYNC_VERIFIED",
                                  src[i:j]))


@unittest.skipUnless(NODE, "node no disponible")
class TestElGraficoSeRefrescaAlTerminarLaCorreccion(unittest.TestCase):
    """De «necesita decisión» a «Confirmar» activo pasaban más de 20 s.

    La Fase E regenera `per_frame_data.json` y de ahí salen el Δ, la
    confianza y el `sync_gate` que habilita el botón. La copia en memoria se
    invalidaba al PULSAR «Aplicar» —cuando la fase aún no había corrido— así
    que seguía siendo la de antes hasta que algo más la tirara.
    """

    _DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.JS_CONCAT, 'utf8');
function grab(n) {
  const i = src.indexOf('function ' + n + '(');
  let d = 0, ab = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { d++; ab = true; }
    else if (src[j] === '}') { d--; if (ab && d === 0) return src.slice(i, j + 1); }
  }
  throw new Error('sin cerrar: ' + n);
}
const api = new Function([
  'const CMV40_WS_SILENCE_FOR_REST_PROGRESS_MS = 99999;',
  'function _cmv40UpdateProgressUI() {}',
  'function _cmv40RehydratePendingTarget() {}',
  grab('_cmv40AssignSession'),
  'return { _cmv40AssignSession };',
].join('\n'))();
const casos = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(casos.map(c => {
  const project = { id: 'p1', session: c.antes, syncData: { data: [1, 2, 3] } };
  api._cmv40AssignSession(project, c.despues);
  return project.syncData === null;
})));
"""

    def _invalidado(self, *casos):
        r = subprocess.run(
            argv_node(self._DRIVER),
            env={**os.environ, "JS_CONCAT": js_en_disco()},
            input=json.dumps(casos), capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        return json.loads(r.stdout)

    def test_al_terminar_la_fase_E_el_volcado_se_tira(self):
        """EJECUTA `_cmv40AssignSession`: el flanco de bajada de la fase.

        No basta con buscar la cadena en el fuente — envolverla en un
        `if (false && …)` la deja intacta y el bug vuelve.
        """
        [r] = self._invalidado({
            "antes": {"running_phase": "correct_sync", "phase": "extracted"},
            "despues": {"running_phase": None, "phase": "extracted"}})
        self.assertTrue(r, "el gráfico y el gate se quedaban con el dato viejo")

    def test_pero_no_al_terminar_cualquier_otra(self):
        """Tirarlo siempre pediría 24 MB de volcado tras cada fase."""
        rs = self._invalidado(
            {"antes": {"running_phase": "inject", "phase": "sync_verified"},
             "despues": {"running_phase": None, "phase": "injected"}},
            {"antes": {"running_phase": None, "phase": "extracted"},
             "despues": {"running_phase": "correct_sync", "phase": "extracted"}})
        self.assertEqual(rs, [False, False])

    def test_ni_mientras_la_fase_sigue_corriendo(self):
        [r] = self._invalidado({
            "antes": {"running_phase": "correct_sync", "phase": "extracted"},
            "despues": {"running_phase": "correct_sync", "phase": "extracted"}})
        self.assertFalse(r)


@unittest.skipUnless(NODE, "node no disponible")
class TestUnRepintadoNoCierraLoQueAbriste(unittest.TestCase):
    """El `<details>` del JSON aplicado se cerraba solo a los 2-3 s.

    Con un job en marcha el panel se repinta, y reemplazar el `innerHTML`
    recrea los `<details>` cerrados. Es la misma trampa que el scroll del
    log, y la misma solución.
    """

    _DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.JS_CONCAT, 'utf8');
function grab(n) {
  const i = src.indexOf('function ' + n + '(');
  let d = 0, ab = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { d++; ab = true; }
    else if (src[j] === '}') { d--; if (ab && d === 0) return src.slice(i, j + 1); }
  }
}
// DOM mínimo: un contenedor con `<details>` que se pueden abrir y un
// `innerHTML` que los reconstruye cerrados, como hace el navegador.
function hacerDetalle(clave, sumKey) {
  return { tagName: 'DETAILS', open: false, dataset: { detalle: clave },
           _sum: sumKey,
           querySelector: function (sel) {
             return sel.includes('summary')
               ? { dataset: sumKey ? { i18n: sumKey } : {}, textContent: sumKey || '' }
               : null;
           } };
}
function hacerCaja(detalles) {
  return { _d: detalles, querySelectorAll: function () { return this._d; } };
}
const api = new Function([grab('_claveDeDetalle'), grab('anclajeDeDetalles'),
  grab('restaurarAnclajeDeDetalles'),
  'return { anclajeDeDetalles, restaurarAnclajeDeDetalles };'].join('\n'))();

const salida = {};
// Dos «detalle técnico» distintos (misma clave de traducción) y uno propio.
let a = hacerDetalle(undefined, 'tab3.detalle_tecnico');
let b = hacerDetalle(undefined, 'tab3.detalle_tecnico');
let c = hacerDetalle('sync-json', 'tab3.correccion_ver_json');
b.open = true; c.open = true;
const abiertos = api.anclajeDeDetalles(hacerCaja([a, b, c]));
salida.cuantos = abiertos.size;
// El repintado: nodos NUEVOS, todos cerrados.
let a2 = hacerDetalle(undefined, 'tab3.detalle_tecnico');
let b2 = hacerDetalle(undefined, 'tab3.detalle_tecnico');
let c2 = hacerDetalle('sync-json', 'tab3.correccion_ver_json');
api.restaurarAnclajeDeDetalles(hacerCaja([a2, b2, c2]), abiertos);
salida.tras = [a2.open, b2.open, c2.open];
// Y si el panel pierde una card, el que quede no se abre por el de al lado.
let solo = hacerDetalle(undefined, 'tab3.detalle_tecnico');
api.restaurarAnclajeDeDetalles(hacerCaja([solo]), abiertos);
salida.soloElPrimero = solo.open;
process.stdout.write(JSON.stringify(salida));
"""

    def test_lo_abierto_sigue_abierto_tras_el_repintado(self):
        r = subprocess.run(argv_node(self._DRIVER),
                           env={**os.environ, "JS_CONCAT": js_en_disco()},
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1200:]}")
        out = json.loads(r.stdout)
        self.assertEqual(out["cuantos"], 2)
        self.assertEqual(out["tras"], [False, True, True])
        # El primero no estaba abierto: si la clave fuera solo la de
        # traducción, el segundo «detalle técnico» lo habría abierto.
        self.assertFalse(out["soloElPrimero"])

    def test_el_panel_no_se_repinta_si_no_ha_cambiado(self):
        cuerpo = _codigo("_renderCMv40ActivePhase")
        self.assertIn("html !== project._panelHTML", cuerpo)
        self.assertIn("anclajeDeDetalles(container)", cuerpo)


# ════════════════════════════════════════════════════════════════════
#  1 · El esqueleto del modal dibuja lo que va a aparecer
# ════════════════════════════════════════════════════════════════════

@unittest.skipUnless(NODE, "node no disponible")
class TestElEsqueletoTieneLaFormaDeLoQueViene(unittest.TestCase):
    """Eran rectángulos grises repartidos por el medio.

    Tres defectos en la captura del usuario: un bloque de cartela DENTRO de
    la timeline —donde van las fases, no la cartela—, diez líneas que no
    llegaban a la mitad del alto del log, y el «Recuperando del servidor…»
    en 11 px arriba a la izquierda. Y la cartela, que no depende del
    servidor, esperando igual.
    """

    _PREAMBULO = """
globalThis.window = globalThis;
function escHtml(t) { return String(t == null ? '' : t); }
function tr(k) { return k; }
function iconoDeTrabajo() { return '<svg data-tipo/>'; }
function iconoDeEstado() { return '<svg data-estado/>'; }
function icono() { return '<svg/>'; }
"""

    _STUBS = """
const _workbarDetalles = {};
let workbarEstado = { activo: null, cola: [] };
const registro = { vistas: [] };
function openModal() {}
function closeModal() {}
function cerrarModalDeTrabajo() { _trabajoModalParar(); }
function _trabajoModalConResumen(a, vista) { return vista; }
function _trabajoModalPinta(a, vista) { registro.vistas.push(vista || {}); }
"""

    def _abrir(self, trabajo):
        from frontend_sources import maquinaria_del_modal_de_trabajo
        guion = """
        registrarDetalleDeTrabajo('prueba', async () => ({ lateral: 'X', cuerpo: 'Y' }));
        (async () => {
          await _trabajoModalAbrir(%s);
          _trabajoModalParar();
          console.log(JSON.stringify(registro));
        })();
        """ % json.dumps(trabajo)
        fuente = (self._PREAMBULO + maquinaria_del_modal_de_trabajo()
                  + _fn("registrarDetalleDeTrabajo") + self._STUBS + guion)
        r = subprocess.run(argv_node(fuente), capture_output=True, text=True,
                           timeout=40)
        if r.returncode != 0:
            raise AssertionError(f"node falló:\n{r.stderr[-1200:]}")
        return json.loads(r.stdout.strip().splitlines()[-1])["vistas"][0]

    TRABAJO = {"id": "p1", "sobre": "p1", "detalle": "prueba",
               "que": "Fase C de Predator", "tipo": "fase_cmv40",
               "titulo": "Predator Badlands (2025)",
               "poster": "https://img/w92/x.jpg", "fases_total": 10}

    def test_la_caratula_no_espera_al_servidor(self):
        """`titulo` y `poster` viajan en `/api/trabajos` desde que se encoló."""
        v = self._abrir(self.TRABAJO)
        self.assertEqual(v["cartel"]["titulo"], "Predator Badlands (2025)")
        self.assertEqual(v["cartel"]["url"], "https://img/w92/x.jpg")

    def test_sin_titulo_no_se_inventa_una_cartela(self):
        v = self._abrir({**self.TRABAJO, "titulo": "", "poster": ""})
        self.assertIsNone(v["cartel"])

    def test_la_columna_usa_el_marcado_REAL_de_la_timeline(self):
        """Con clases propias, al llegar el detalle todo saltaba de sitio."""
        v = self._abrir(self.TRABAJO)
        for clase in ("cmv40-running-timeline", "cmv40-tl-header",
                      "cmv40-tl-step", "cmv40-tl-rail", "cmv40-tl-body"):
            with self.subTest(clase):
                self.assertIn(clase, v["lateral"])
        self.assertNotIn("wb-esq-cartel", v["lateral"],
                         "la cartela no va dentro de la timeline")

    def test_y_tantas_filas_como_fases_tenga_el_trabajo(self):
        diez = self._abrir(self.TRABAJO)
        cinco = self._abrir({**self.TRABAJO, "fases_total": 5})
        # Por el raíl y no por `cmv40-tl-step`: el `<ul>` se llama
        # `cmv40-tl-steps` y lo contiene como subcadena.
        self.assertEqual(diez["lateral"].count("cmv40-tl-rail"), 10)
        self.assertEqual(cinco["lateral"].count("cmv40-tl-rail"), 5)

    def test_un_trabajo_sin_fases_no_deja_la_columna_pelada(self):
        v = self._abrir({**self.TRABAJO, "fases_total": 0})
        self.assertGreaterEqual(v["lateral"].count("cmv40-tl-rail"), 3)

    def test_el_cuerpo_llena_el_hueco_del_log(self):
        """Diez líneas se quedaban a media altura; se generan de más y se
        recortan con `overflow:hidden`, que es lo que no depende del alto
        de la ventana."""
        v = self._abrir(self.TRABAJO)
        self.assertIn("cmv40-log", v["cuerpo"])
        self.assertGreaterEqual(v["cuerpo"].count("wb-esq-linea"), 30)

    def test_el_aviso_es_una_cabecera_y_no_una_etiqueta_en_una_esquina(self):
        v = self._abrir(self.TRABAJO)
        self.assertIn("wb-esq-aviso", v["cuerpo"])
        self.assertIn("cmv40-running-spinner", v["cuerpo"])


class TestElEsqueletoSeRecortaYNoSeDesborda(unittest.TestCase):
    """La parte que vive en el CSS, y la trampa de especificidad.

    `.trabajo-modal-principal .cmv40-log` trae `overflow-y: auto` y viene
    DESPUÉS en el fichero, así que un `.wb-esqueleto-cuerpo` a secas pierde
    y las cuarenta líneas se vuelven scrollables — cuarenta líneas de gris.
    """

    @classmethod
    def setUpClass(cls):
        cls.css = (APP_DIR / "static" / "style.css").read_text(encoding="utf-8")

    def test_el_hueco_del_log_se_recorta(self):
        i = self.css.index(".wb-esqueleto-cuerpo {")
        regla = self.css[self.css.rindex("\n", 0, i):self.css.index("}", i)]
        self.assertIn("overflow: hidden", regla)
        self.assertIn("flex: 1 1 auto", regla)

    def test_y_gana_a_la_regla_del_modal(self):
        i = self.css.index(".wb-esqueleto-cuerpo {")
        selector = self.css[self.css.rindex("\n\n", 0, i):i]
        self.assertIn(".trabajo-modal-principal .cmv40-log.wb-esqueleto-cuerpo",
                      selector)

    def test_las_filas_de_la_columna_tambien(self):
        i = self.css.index(".wb-esqueleto-filas {")
        regla = self.css[i:self.css.index("}", i)]
        self.assertIn("overflow: hidden", regla)


# ════════════════════════════════════════════════════════════════════
#  3 y 6 · Lo que se ha retirado sigue retirado
# ════════════════════════════════════════════════════════════════════

class TestLoRetiradoNoVuelveSolo(unittest.TestCase):
    """Un botón que se quita deja funciones, endpoint y claves detrás.

    Media retirada es peor que ninguna: el endpoint huérfano parece
    cobertura y las claves sueltas engordan los tres catálogos.
    """

    @classmethod
    def setUpClass(cls):
        from frontend_sources import js_completo
        cls.js = js_completo()
        cls.py = "\n".join(p.read_text(encoding="utf-8")
                           for p in (APP_DIR).rglob("*.py")
                           if "tests" not in str(p))

    def test_el_comparador_de_luminancia_no_deja_rastro(self):
        for aguja in ("abrirComparadorLuminancia", "quitarComparacionLuminancia",
                      "_mkvTablaComparacionHtml", "compareSeries"):
            with self.subTest(aguja):
                self.assertNotIn(aguja, self.js)

    def test_ni_su_endpoint(self):
        self.assertNotIn("light-profile-cached", self.py)
        self.assertNotIn("light-profile-cached", self.js)

    def test_el_boton_de_cambiar_pelicula_se_fue_y_el_de_buscar_se_queda(self):
        """Son dos cosas distintas: sin ficha no hay carátula, y eso se
        arregla; con ficha, cambiarla no cambia nada del job."""
        cuerpo = _codigo("botonDeFicha")
        self.assertNotIn("core.cambiar_pelicula", cuerpo)
        self.assertIn("core.buscar_pelicula", cuerpo)

    def test_y_ninguna_clave_retirada_sigue_en_los_catalogos(self):
        for idioma in ("es", "en", "ca"):
            cat = json.loads((APP_DIR / "static" / "i18n" / f"{idioma}.json")
                             .read_text(encoding="utf-8"))
            for k in ("core.cambiar_pelicula", "tab2.comparar_con",
                      "tab2.quitar_comparacion", "tab2.re_analizar_si_el_mkv_cambio"):
                with self.subTest(idioma=idioma, clave=k):
                    self.assertNotIn(k, cat)


if __name__ == "__main__":
    unittest.main()
