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


def _sin_comentarios(src: str) -> str:
    """El fuente sin las líneas de comentario.

    Los comentarios de este repo citan el nombre de lo que se acaba de
    quitar —«Sin `_workbarPasaFiltro`: …»—, así que un `assertNotIn` sobre
    el fuente crudo se dispara con su propia explicación.
    """
    return "\n".join(l for l in src.splitlines()
                      if not l.strip().startswith(("//", "*", "/*")))


def _codigo(nombre: str) -> str:
    """El cuerpo de una función, sin sus comentarios."""
    return _sin_comentarios(_fn(nombre))


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
  grab('_cmv40RangoPorDefecto'),
  'return { _cmv40Encuadrar, _cmv40FrameATiempo, _cmv40TiempoAFrame,'
  + ' _cmv40RangoPorDefecto, CMV40_ZOOM_MIN_SEG };',
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

    def test_el_grafico_se_abre_con_la_pelicula_ENTERA(self):
        """Eran los primeros 30 s, y abrir con un recorte que nadie pidió
        deja «¿y el resto?» como primera pregunta. Decisión del usuario."""
        [r] = self._llamar(("_cmv40RangoPorDefecto", [self.TOTAL]))
        self.assertEqual((r["start"], r["end"]), (0, self.TOTAL))

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

    def test_el_encuadre_inicial_se_define_en_UN_sitio(self):
        """Lo leían dos funciones con la constante escrita en cada una, que
        es como se acaba con dos defaults distintos."""
        for fn in ("_renderCMv40SyncControls", "_renderCMv40Chart"):
            with self.subTest(fn):
                cuerpo = _codigo(fn)
                self.assertIn("_cmv40RangoPorDefecto(totalFrames)", cuerpo)
                self.assertNotIn("30 * FPS", cuerpo)


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
        """Y conserva los `<details>`: las dos cosas las hace el helper."""
        self.assertIn("pintarSiCambia(container",
                      _codigo("_renderCMv40ActivePhase"))
        self.assertIn("anclajeDeDetalles", _codigo("pintarSiCambia"))


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
#  7 · La corrección del sync, por los DOS extremos
# ════════════════════════════════════════════════════════════════════

@unittest.skipUnless(NODE, "node no disponible")
class TestElSyncSeCorrigePorLosDosExtremos(unittest.TestCase):
    """Eran dos casillas y las dos tocaban el PRINCIPIO.

    El desfase típico es un logo de estudio que el BD trae y la versión de
    streaming no, y por eso el formulario ofrecía «quitar al inicio» y
    «duplicar el primer frame». Pero hay másters donde lo que sobra o falta
    está al final —créditos, un fundido más largo—, y ahí cuadrar el frame
    count por delante **cuadra el número y desplaza la película entera**.
    Reportado por el usuario el 2026-09-23.
    """

    _DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.JS_CONCAT, 'utf8');
function grab(n) {
  const i = src.indexOf('function ' + n + '(');
  const async_ = src.slice(Math.max(0, i - 6), i) === 'async ';
  let d = 0, ab = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { d++; ab = true; }
    else if (src[j] === '}') {
      d--;
      if (ab && d === 0) return (async_ ? 'async ' : '') + src.slice(i, j + 1);
    }
  }
  throw new Error('sin cerrar: ' + n);
}
const caso = JSON.parse(fs.readFileSync(0, 'utf8'));
// DOM mínimo: las cuatro casillas con lo que el usuario escribió.
globalThis.document = { getElementById: (id) => {
  // Las largas PRIMERO: con `remove` delante, `cmv40-remove-fin-p1` casa
  // con `remove` y las dos casillas leen el mismo valor. La app no tiene el
  // problema —usa `getElementById` exacto— pero el arnés sí.
  const m = id.match(/^cmv40-(remove-fin|remove|duplicate-fin|duplicate)-/);
  return m ? { value: String(caso.ops[m[1]] ?? 0) } : null;
} };
globalThis.showToast = (msg) => { globalThis.__toast = msg; };
globalThis.tr = (k) => k;
globalThis.openCMv40Projects = [{
  id: 'p1',
  session: { target_frame_count: caso.total },
  syncData: { target_frames: caso.total },
  expandedPhases: {},
}];
globalThis.apiFetch = async (url, opts) => {
  globalThis.__enviado = JSON.parse(opts.body).editor_config;
  return null;                       // corta el flujo tras el POST
};
const api = new Function([
  // `_cmv40ConfigDeSync` salió del handler el 2026-09-25 para poder
  // probarla sola: sin traerla, el handler muere con un ReferenceError.
  grab('_cmv40OpsDeSync'), grab('_cmv40ConfigDeSync'), grab('cmv40DoApplySync'),
  'return { cmv40DoApplySync };',
].join('\n'))();
(async () => {
  await api.cmv40DoApplySync('p1');
  process.stdout.write(JSON.stringify(
    {cfg: globalThis.__enviado ?? null, toast: globalThis.__toast ?? null}));
})();
"""

    TOTAL = 1000

    def _aplicar(self, **ops):
        r = subprocess.run(
            argv_node(self._DRIVER),
            env={**os.environ, "JS_CONCAT": js_en_disco()},
            input=json.dumps({"ops": ops, "total": self.TOTAL}),
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        return json.loads(r.stdout)

    def test_quitar_al_inicio_sigue_siendo_el_primer_rango(self):
        self.assertEqual(self._aplicar(**{"remove": 10})["cfg"],
                         {"remove": ["0-9"]})

    def test_quitar_al_final_cuenta_hacia_atras_desde_el_ultimo(self):
        """Lo que no se podía hacer: el rango va al FINAL del target."""
        self.assertEqual(self._aplicar(**{"remove-fin": 10})["cfg"],
                         {"remove": ["990-999"]})

    def test_duplicar_al_final_copia_el_ULTIMO_frame(self):
        self.assertEqual(self._aplicar(**{"duplicate-fin": 3})["cfg"],
                         {"duplicate": [{"source": 999, "offset": 1000,
                                         "length": 3}]})

    def test_duplicar_al_inicio_sigue_copiando_el_primero(self):
        self.assertEqual(self._aplicar(**{"duplicate": 3})["cfg"],
                         {"duplicate": [{"source": 0, "offset": 0,
                                         "length": 3}]})

    def test_los_dos_extremos_a_la_vez_no_se_solapan(self):
        """Con un target corto, los dos rangos podrían pisarse."""
        cfg = self._aplicar(**{"remove": 4, "remove-fin": 4})["cfg"]
        self.assertEqual(cfg["remove"], ["0-3", "996-999"])

    def test_y_con_un_target_diminuto_tampoco(self):
        r = subprocess.run(
            argv_node(self._DRIVER),
            env={**os.environ, "JS_CONCAT": js_en_disco()},
            input=json.dumps({"ops": {"remove": 6, "remove-fin": 6},
                              "total": 8}),
            capture_output=True, text=True, timeout=30)
        cfg = json.loads(r.stdout)["cfg"]
        # El rango del final arranca detrás de lo que ya se quita por
        # delante: sin el `max`, saldría «2-7» y se solaparía con «0-5».
        self.assertEqual(cfg["remove"], ["0-5", "6-7"])

    def test_sin_saber_el_total_no_se_corrige_por_el_final(self):
        """Inventarse el último frame es peor que decir que no se puede."""
        r = subprocess.run(
            argv_node(self._DRIVER),
            env={**os.environ, "JS_CONCAT": js_en_disco()},
            input=json.dumps({"ops": {"remove-fin": 5}, "total": 0}),
            capture_output=True, text=True, timeout=30)
        out = json.loads(r.stdout)
        self.assertIsNone(out["cfg"], "no se debe mandar nada")
        self.assertEqual(out["toast"], "tab3.sync_sin_total_no_hay_final")

    def test_las_cuatro_a_cero_no_hacen_nada(self):
        out = self._aplicar()
        self.assertIsNone(out["cfg"])
        self.assertEqual(out["toast"], "tab3.indica_un_valor_para_eliminar_o")


class TestYaNoSeAdivinaDondeVaLaCorreccion(unittest.TestCase):
    """El auto-relleno se fue con las dos casillas, y tenía que irse.

    Con un solo sitio posible, el número determinaba la corrección entera y
    prerrellenar era un atajo. Con dos extremos hay infinitas combinaciones
    que dan el mismo Δ y la app **no puede saber cuál es la correcta**:
    rellenar una por su cuenta sería adivinar, y adivinar aquí desplaza la
    película. Lo que hace en su lugar es decir cuántos frames sobran o
    faltan y que el sitio lo elige quien mira el gráfico.
    """

    def test_las_casillas_nacen_a_cero(self):
        cuerpo = _codigo("_renderCMv40SyncControls")
        self.assertNotIn("delta > 0 ? delta : 0", cuerpo)
        self.assertIn('value="0"', cuerpo)

    def test_y_el_desfase_se_ANUNCIA(self):
        cuerpo = _codigo("_renderCMv40SyncControls")
        for clave in ("tab3.sync_sobran_frames", "tab3.sync_faltan_frames"):
            with self.subTest(clave):
                self.assertIn(clave, cuerpo)

    def test_el_aviso_solo_sale_si_hay_desfase(self):
        """Con Δ=0 no hay nada que avisar y el bloque estorba."""
        self.assertIn("delta === 0 ? ''", _codigo("_renderCMv40SyncControls"))

    def test_el_gate_de_avance_sigue_siendo_del_backend(self):
        """Lo que impide continuar con los frames descuadrados no cambia:
        `sync_gate` lo resuelve el servidor y `mark-synced` da 409."""
        cuerpo = _codigo("_renderCMv40SyncControls")
        self.assertIn("d.sync_gate", cuerpo)
        self.assertIn("canConfirm", cuerpo)


@unittest.skipUnless(NODE, "node no disponible")
class TestUnRepintadoNoBorraLoQueEstasEscribiendo(unittest.TestCase):
    """Las cuatro casillas del sync volvían a cero a los dos segundos.

    El panel se repinta con cada vuelta del poll y reemplazar el
    `innerHTML` devuelve los campos a su valor de plantilla. **Antes no se
    notaba** porque la casilla se auto-rellenaba con el Δ: el repintado la
    dejaba en el mismo número. Al quitar el auto-relleno —que había que
    quitarlo, porque con dos extremos la app no puede adivinar dónde va la
    corrección— el borrado quedó a la vista. Reportado el 2026-09-23.

    Es la tercera vez que aparece la misma trampa: el scroll del log, los
    `<details>` del panel y ahora lo tecleado.
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
// DOM mínimo: campos con id, valor, dataset y foco.
globalThis.CSS = { escape: (s) => s };
function campo(id, valor, tocado) {
  return { id, value: valor, dataset: tocado ? {tocado: '1'} : {},
           selectionStart: 1, selectionEnd: 1,
           focus() { globalThis.__foco = this.id; },
           setSelectionRange() {} };
}
function caja(campos) {
  return { _c: campos,
           querySelectorAll: () => campos.filter(c => c.id),
           querySelector: (sel) => campos.find(c => '#' + c.id === sel) || null };
}
globalThis.document = { activeElement: null };
const api = new Function([grab('anclajeDeFormulario'),
  grab('restaurarAnclajeDeFormulario'), grab('marcarTocado'),
  'return { anclajeDeFormulario, restaurarAnclajeDeFormulario, marcarTocado };',
].join('\n'))();

const salida = {};
// El usuario escribe en dos de las cuatro casillas.
const a = campo('cmv40-remove-p1', '0', false);
const b = campo('cmv40-remove-fin-p1', '24', true);
const c = campo('cmv40-duplicate-p1', '0', false);
const nombre = campo('cmv40-output-name-p1', 'lo que escribí', true);
globalThis.document.activeElement = b;
const ancla = api.anclajeDeFormulario(caja([a, b, c, nombre]));
salida.guardados = Object.keys(ancla).sort();
// El repintado: campos NUEVOS, con el valor de plantilla.
const a2 = campo('cmv40-remove-p1', '0', false);
const b2 = campo('cmv40-remove-fin-p1', '0', false);
const c2 = campo('cmv40-duplicate-p1', '0', false);
const n2 = campo('cmv40-output-name-p1', 'el del servidor', false);
api.restaurarAnclajeDeFormulario(caja([a2, b2, c2, n2]), ancla);
salida.tras = [a2.value, b2.value, c2.value, n2.value];
salida.foco = globalThis.__foco || null;
salida.siguenTocados = [b2.dataset.tocado, a2.dataset.tocado || null];
// Y sin nada tocado no se ancla nada: el servidor manda.
salida.sinTocar = api.anclajeDeFormulario(caja([campo('x', '1', false)]));
// `marcarTocado` es lo que pone la marca.
const m = campo('y', '', false); api.marcarTocado(m);
salida.marca = m.dataset.tocado;
process.stdout.write(JSON.stringify(salida));
"""

    def _correr(self):
        r = subprocess.run(argv_node(self._DRIVER),
                           env={**os.environ, "JS_CONCAT": js_en_disco()},
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        return json.loads(r.stdout)

    def test_lo_tecleado_sobrevive_al_repintado(self):
        out = self._correr()
        self.assertEqual(out["tras"][1], "24", "la casilla volvió a cero")
        self.assertEqual(out["tras"][3], "lo que escribí")

    def test_y_el_foco_y_el_cursor_tambien(self):
        """Conservar el valor y perder el teclado es el mismo problema."""
        self.assertEqual(self._correr()["foco"], "cmv40-remove-fin-p1")

    def test_lo_que_NO_has_tocado_lo_manda_el_servidor(self):
        """Un campo que el servidor repinta con un valor nuevo —el nombre
        tras un renombrado— tiene que poder cambiar."""
        out = self._correr()
        self.assertEqual(out["guardados"],
                         ["cmv40-output-name-p1", "cmv40-remove-fin-p1"])
        self.assertIsNone(out["sinTocar"])

    def test_la_marca_la_pone_marcarTocado(self):
        self.assertEqual(self._correr()["marca"], "1")

    def test_el_formulario_del_sync_usa_las_dos_medidas(self):
        self.assertIn("pintarSiCambia(container",
                      _codigo("_renderCMv40SyncControls"))
        cuerpo = _codigo("pintarSiCambia")
        self.assertIn("anclajeDeFormulario(el)", cuerpo)
        self.assertIn("restaurarAnclajeDeFormulario", cuerpo)

    def test_y_las_casillas_avisan_de_que_las_tocan(self):
        cuerpo = _codigo("_renderCMv40SyncControls")
        self.assertIn("marcarTocado(this)", cuerpo)

    def test_el_nombre_del_mkv_de_salida_igual(self):
        """Éste NO pasa por `pintarSiCambia` —pinta varias zonas, no una—
        así que lleva el anclaje a mano, con la restauración al final."""
        cuerpo = _codigo("_renderCMv40Info")
        self.assertIn("anclajeDeFormulario(container)", cuerpo)
        self.assertIn("restaurarAnclajeDeFormulario", cuerpo)
        self.assertIn("marcarTocado(this)", cuerpo)

    def test_y_al_guardar_deja_de_estar_pendiente(self):
        """Si no, el valor tecleado ganaría para siempre al del servidor."""
        self.assertIn("delete campo.dataset.tocado",
                      _codigo("_cmv40SaveOutputName"))


# ════════════════════════════════════════════════════════════════════
#  8 · Una línea del historial es UNA ejecución, no el proyecto
# ════════════════════════════════════════════════════════════════════

class TestElLogDeUnaEjecucionCancelada(unittest.TestCase):
    """Cancelar una fase, relanzarla, y abrir la cancelada.

    El log de un proyecto CMv4.0 es UNO —`/config/cmv40/{id}.log`, al que se
    añade— y una línea del historial es UNA ejecución. Así que la entrada
    cancelada enseñaba el log de la que está corriendo ahora: la cabecera
    decía «cancelado» y el cuerpo escribía en vivo, con la barra parada.
    Reportado el 2026-09-23.

    El recorte va en el SERVIDOR porque es el único sitio donde los dos
    husos coinciden: el prefijo de cada línea es `[HH:MM:SS]` en hora LOCAL
    del contenedor, sin fecha, y el historial guarda UTC.
    """

    @staticmethod
    def _iso(h, m, sg, dia=23):
        import datetime as dt
        local = dt.datetime(2026, 9, dia, h, m, sg).astimezone()
        return local.astimezone(dt.timezone.utc).isoformat()

    def setUp(self):
        from routers.cmv40 import recortar_log_por_tiempo
        self.rec = recortar_log_por_tiempo
        # Dos ejecuciones en el mismo fichero: la cancelada y la de ahora.
        self.log = [
            "[17:58:20] ━━━ Fase F ━━━",
            "[18:05:00] inyectando",
            "  traceback sin prefijo",
            "[18:27:50] 🛑 Cancelado",
            "[21:45:00] ━━━ Fase A ━━━",
            "[21:50:42] frame=186207 fps=667",
        ]

    def test_solo_salen_las_lineas_de_ESA_ejecucion(self):
        out = self.rec(self.log, self._iso(17, 58, 15), self._iso(18, 27, 56))
        self.assertEqual(len(out), 4)
        self.assertIn("🛑 Cancelado", out[-1])
        self.assertTrue(all("frame=" not in l for l in out),
                        "se coló la ejecución de ahora")

    def test_una_linea_sin_prefijo_va_con_la_anterior(self):
        """Un traceback no lleva hora y pertenece a lo de arriba."""
        out = self.rec(self.log, self._iso(17, 58, 15), self._iso(18, 27, 56))
        self.assertIn("  traceback sin prefijo", out)
        # Y si la anterior queda fuera, ella también.
        fuera = self.rec(self.log, self._iso(21, 40, 0), self._iso(22, 0, 0))
        self.assertNotIn("  traceback sin prefijo", fuera)

    def test_una_fase_puede_cruzar_la_medianoche(self):
        """La hora no trae fecha: cuando RETROCEDE, ha cambiado el día."""
        nocturno = ["[23:58:00] antes", "[00:02:00] después",
                    "[00:30:00] muy después"]
        out = self.rec(nocturno, self._iso(23, 57, 0), self._iso(0, 5, 0, 24))
        self.assertEqual(out, ["[23:58:00] antes", "[00:02:00] después"])

    def test_sin_fechas_legibles_se_devuelve_el_log_entero(self):
        """Enseñar de más es un inconveniente; enseñar vacío parecería que
        no pasó nada."""
        self.assertEqual(self.rec(self.log, "no es una fecha", ""), self.log)
        self.assertEqual(self.rec(self.log, "", ""), self.log)

    def test_y_si_la_ventana_no_casa_con_ninguna_linea_tambien(self):
        """Un log sin prefijos —o de otro día— no puede dejarse en blanco."""
        vacio = self.rec(self.log, self._iso(3, 0, 0, 22), self._iso(4, 0, 0, 22))
        self.assertEqual(vacio, self.log)

    def test_el_margen_recoge_el_separador_de_la_fase(self):
        """La línea del historial se escribe en el `finally`, unos
        milisegundos después de la última; y el `━━━` puede caer justo
        antes del `inicio`."""
        out = self.rec(self.log, self._iso(17, 58, 22), self._iso(18, 27, 48))
        self.assertIn("[17:58:20] ━━━ Fase F ━━━", out)
        self.assertIn("[18:27:50] 🛑 Cancelado", out)


class TestElModalDeUnaEntradaTerminalNoSeHacePasarPorViva(unittest.TestCase):

    def test_pide_log_desde_y_log_hasta(self):
        from frontend_sources import pieza_de
        _f, src = pieza_de("_cmv40CtxTimeline")
        i = src.index("registrarDetalleDeTrabajo('cmv40'")
        j = src.index("\n});", i)
        cuerpo = src[i:j]
        self.assertIn("log_desde", cuerpo)
        self.assertIn("log_hasta", cuerpo)
        self.assertIn("a.terminal && h.inicio", cuerpo)

    def test_y_la_timeline_no_dibuja_la_fase_que_corre_ahora(self):
        """Sería el mismo desajuste que el log: cabecera «cancelado» y una
        fase latiendo debajo."""
        from frontend_sources import pieza_de
        _f, src = pieza_de("_cmv40CtxTimeline")
        i = src.index("registrarDetalleDeTrabajo('cmv40'")
        j = src.index("\n});", i)
        self.assertIn("a.terminal ? { ...s, running_phase: null", src[i:j])


@unittest.skipUnless(NODE, "node no disponible")
class TestElTopeDelLogEsPorTRABAJO(unittest.TestCase):
    """«Ver el log entero» es una decisión sobre ESTE trabajo.

    Si el flag se quedara puesto, abrir después uno con veinte mil líneas de
    `frame=…` pintaría las veinte mil — que es justo lo que el tope existe
    para evitar.
    """

    _PREAMBULO = """
globalThis.window = globalThis;
function escHtml(t) { return String(t == null ? '' : t); }
function tr(k) { return k; }
function iconoDeTrabajo() { return '<svg/>'; }
function iconoDeEstado() { return '<svg/>'; }
function icono() { return '<svg/>'; }
"""

    _STUBS = """
const _workbarDetalles = {};
let workbarEstado = { activo: null, cola: [] };
function openModal() {}
function closeModal() {}
function cerrarModalDeTrabajo() { _trabajoModalParar(); }
function _trabajoModalConResumen(a, vista) { return vista; }
function _trabajoModalPinta() {}
"""

    def test_al_abrir_otro_trabajo_el_tope_vuelve(self):
        from frontend_sources import maquinaria_del_modal_de_trabajo
        guion = """
        registrarDetalleDeTrabajo('prueba', async () => ({ cuerpo: 'x' }));
        (async () => {
          await _trabajoModalAbrir({id: 'a', sobre: 'a', detalle: 'prueba',
                                    que: 'uno', tipo: 'fase_cmv40'});
          _trabajoModalParar();
          verLogEntero();                       // el usuario lo despliega
          const tras = _trabajoLogSinTope;
          await _trabajoModalAbrir({id: 'b', sobre: 'b', detalle: 'prueba',
                                    que: 'dos', tipo: 'fase_cmv40'});
          _trabajoModalParar();
          console.log(JSON.stringify({tras, siguiente: _trabajoLogSinTope}));
        })();
        """
        fuente = (self._PREAMBULO + maquinaria_del_modal_de_trabajo()
                  + _fn("registrarDetalleDeTrabajo") + self._STUBS + guion)
        r = subprocess.run(argv_node(fuente), capture_output=True, text=True,
                           timeout=40)
        if r.returncode != 0:
            raise AssertionError(f"node falló:\n{r.stderr[-1200:]}")
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(out["tras"], "«ver entero» no llegó a activarse")
        self.assertFalse(out["siguiente"],
                         "el flag se pegó al trabajo siguiente")


@unittest.skipUnless(NODE, "node no disponible")
class TestLaFirmaDelRepintadoViveEnElElemento(unittest.TestCase):
    """El guard de «no repintar si no cambió» dejó un panel EN BLANCO.

    La primera versión guardaba la firma en el proyecto
    (`project._panelHTML`). Cuando el repintado del PADRE recrea el
    elemento —el panel entero se reescribe y con él el `<div>` de los
    controles del sync—, el nuevo nace vacío mientras la firma sigue
    diciendo «esto ya está pintado». Resultado medido el 2026-09-23: un
    proyecto que aplica la corrección, el servidor contesta
    `sync_gate.ok = true` y el panel no enseña ni el gráfico ni el botón de
    continuar. El job quedaba bloqueado con todo correcto por detrás.

    Con la firma en `dataset`, un elemento recreado no la trae y se pinta.
    Es el mismo motivo por el que el badge de trust compara
    `dataset.estado` y no `innerHTML`.
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
globalThis.CSS = { escape: (s) => s };
globalThis.document = { activeElement: null };
function nuevoDiv() {
  return { dataset: {}, innerHTML: '',
           querySelectorAll: () => [], querySelector: () => null };
}
const api = new Function([
  grab('_hashCorto'), grab('anclajeDeDetalles'), grab('_claveDeDetalle'),
  grab('restaurarAnclajeDeDetalles'), grab('anclajeDeFormulario'),
  grab('restaurarAnclajeDeFormulario'), grab('pintarSiCambia'),
  'return { pintarSiCambia };',
].join('\n'))();

const r = {};
const a = nuevoDiv();
r.primera  = api.pintarSiCambia(a, '<p>uno</p>');
r.repetida = api.pintarSiCambia(a, '<p>uno</p>');
r.cambia   = api.pintarSiCambia(a, '<p>dos</p>');
r.contenido = a.innerHTML;
// EL CASO DEL BUG: el padre recrea el elemento y se pide el MISMO html.
const b = nuevoDiv();
r.recreado = api.pintarSiCambia(b, '<p>dos</p>');
r.contenidoRecreado = b.innerHTML;
// Y sin elemento no revienta.
r.sinElemento = api.pintarSiCambia(null, '<p>x</p>');
process.stdout.write(JSON.stringify(r));
"""

    def _correr(self):
        r = subprocess.run(argv_node(self._DRIVER),
                           env={**os.environ, "JS_CONCAT": js_en_disco()},
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError(f"node falló: {r.stderr[-1500:]}")
        return json.loads(r.stdout)

    def test_pinta_la_primera_vez_y_no_la_segunda(self):
        out = self._correr()
        self.assertTrue(out["primera"])
        self.assertFalse(out["repetida"], "repintó sin que cambiara nada")
        self.assertTrue(out["cambia"])
        self.assertEqual(out["contenido"], "<p>dos</p>")

    def test_UN_ELEMENTO_RECREADO_SE_PINTA_aunque_el_html_sea_el_mismo(self):
        """El caso exacto del job bloqueado."""
        out = self._correr()
        self.assertTrue(out["recreado"],
                        "el elemento nuevo se quedó vacío: es el bug")
        self.assertEqual(out["contenidoRecreado"], "<p>dos</p>")

    def test_sin_elemento_no_revienta(self):
        self.assertFalse(self._correr()["sinElemento"])


class TestNadieVuelveAGuardarLaFirmaFueraDelDOM(unittest.TestCase):
    """El guard: una firma en el proyecto es el bug de vuelta."""

    def test_los_tres_repintados_pasan_por_el_helper(self):
        """Y NINGUNO escribe el `innerHTML` a mano.

        Con un `assertIn` a secas no basta: `_renderCMv40SyncControls`
        tiene dos ramas —la editable y la de solo lectura— así que
        devolver UNA de ellas al `innerHTML` crudo deja la otra llamada
        en su sitio y el guard pasa en verde. Lo que hay que fijar es la
        ausencia, no la presencia.
        """
        for fn in ("_renderCMv40ActivePhase", "_renderCMv40SyncControls"):
            with self.subTest(fn):
                cuerpo = _codigo(fn)
                self.assertIn("pintarSiCambia(container", cuerpo)
                self.assertNotIn("container.innerHTML =", cuerpo)

    def test_y_ninguno_guarda_la_firma_en_el_proyecto(self):
        from frontend_sources import js_completo
        js = _sin_comentarios(js_completo())
        for aguja in ("_panelHTML", "_syncControlesHTML"):
            with self.subTest(aguja):
                self.assertNotIn(aguja, js)


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
