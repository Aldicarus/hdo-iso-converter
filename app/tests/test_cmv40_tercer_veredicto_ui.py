"""Lo que el usuario ve del tercer veredicto: modal, card y fase en curso.

Los cinco puntos que reportó el 2026-09-19 mirando un job real, y que este
fichero fija ejecutando las funciones de `tab3.js` en node:

1. la fila «Recomendación» del modal del pre-flight salía **resuelta y
   coloreada desde el primer repintado**, antes de haber analizado nada: el
   servidor rellena `recommended_action_label` siempre, y la fila solo se
   empujaba «si hay label»;
2. la fila del L8 escribía el identificador `tone_mapping` en crudo, porque
   su tabla de textos tenía `real`/`indeterminate`/`default` y caía a
   `|| clase`;
3. no había NINGÚN sitio donde tomar la decisión: los botones de la card
   estaban detrás de un `isKeep` que excluía el tercer veredicto
   explícitamente;
4. la card no dejaba rastro de que hubiera habido algo que decidir;
5. la fase en curso seguía ofreciendo su botón de lanzar — `_cmv40PhaseState`
   decide `active` mirando solo `s.phase`.

Y uno que no se veía todavía pero habría aparecido al arreglar el primero: el
badge ámbar citaba `--dv-amber-*`, que están declaradas **dentro de
`.dv-detail`** (la radiografía de Tab 2). Desde fuera de ese bloque las tres
declaraciones caen y el badge se queda sin fondo, sin color y sin borde. Una
`var()` fuera de alcance no da error.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_tercer_veredicto_ui -v
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
                              js_en_disco, pintar_en)


def catalogo_servidor() -> dict:
    """El catálogo del SERVIDOR: el relato se compone allí."""
    import json
    return json.loads((APP_DIR / "i18n" / "es.json").read_text(encoding="utf-8"))


def con_relato(sesion: dict) -> dict:
    """La sesión tal y como la sirve el endpoint, con su `relato` dentro.

    El modal y la ficha dejaron de derivar el 2026-09-19 y leen el relato.
    Alimentarlos con una sesión cruda mediría un camino que ya no existe.
    """
    from models import CMv40Session
    from phases.cmv40_relato import resolver
    from phases.cmv40_strategy import resolve_plan
    base = dict(id="cmv40_t", source_mkv_path="/x/a.mkv", source_mkv_name="a.mkv")
    obj = CMv40Session(**{**base, **{k: v for k, v in sesion.items()
                                     if k in CMv40Session.model_fields}})
    return {**sesion, "relato": resolver(obj, plan=resolve_plan(obj))}

NODE = shutil.which("node")

# Carga las siete piezas tal cual las carga el navegador y llama a la función
# que el caso pida. `vm.runInThisContext` y no `eval`: dentro de un módulo de
# node, un `eval` deja las declaraciones en el ámbito local y las funciones no
# quedan en `global`.
_DRIVER = r"""
const fs = require('fs'), vm = require('vm');
const nodo = () => ({style:{}, dataset:{}, classList:{toggle(){},add(){},remove(){},contains(){return false}},
  appendChild(){}, addEventListener(){}, setAttribute(){}, querySelectorAll: () => [],
  querySelector: () => null, innerHTML:'', textContent:''});
global.window = global;
global.document = {getElementById: () => null, querySelectorAll: () => [],
  querySelector: () => null, createElement: nodo, addEventListener(){},
  head: nodo(), body: nodo(), documentElement: nodo(), readyState: 'complete'};
global.localStorage = {getItem: () => null, setItem(){}};
global.fetch = () => new Promise(() => {});
// El catálogo se SIEMBRA, igual que en producción: el servidor sirve
// `/api/i18n/catalogo.js` como script bloqueante antes de `i18n.js`. Aquí
// carga el bundle entero, así que el motor de la app ya está dentro — meter
// además `motor_i18n()` duplica `IDIOMAS` e `IDIOMA_PREF` y node muere con
// «Identifier already declared». Sin la siembra, `tr()` devuelve la clave y
// el fallo señalaría al código, que está bien.
global.__I18N = {idioma: 'es',
                 catalogo: JSON.parse(fs.readFileSync(process.env.CATALOGO, 'utf8'))};
vm.runInThisContext(fs.readFileSync(process.env.JS_CONCAT, 'utf8'));

let entrada = '';
process.stdin.on('data', d => entrada += d);
process.stdin.on('end', () => {
  const salida = JSON.parse(entrada).map(caso => {
    const s = caso.s;
    if (caso.fn === 'checks')   return _cmv40PfChecks(s);
    if (caso.fn === 'veredicto')return _cmv40PfVeredicto(s, '');
    if (caso.fn === 'card')     return _renderCMv40RecommendationCard(s, 'pid1');
    if (caso.fn === 'cuerpo') {
      // Por el SITIO QUE LO USA, no por el helper: llamar a
      // `_cmv40FaseBodyBloqueable` a pelo deja pasar la mutación de volver a
      // `_cmv40FaseBody` en la card, que es el bug entero.
      const fase = CMV40_FASES_DEF.find(f => f.key === caso.fase);
      const estado = _cmv40PhaseState(s.phase, fase.produces, fase.startsFrom);
      return {estado: estado,
              html: _cmv40RenderFaseCard('pid1', s, fase, estado, true)};
    }
    throw new Error('caso desconocido: ' + caso.fn);
  });
  process.stdout.write(JSON.stringify(salida));
  process.exit(0);
});
"""

# El bin de Pulp Fiction tal como lo sirve el endpoint tras el arreglo.
# Los rótulos salen del CATÁLOGO, no escritos a mano: así un cambio de
# redacción no pone en rojo un test de comportamiento.
from frontend_sources import catalogo_es, catalogo_servidor_es  # noqa: E402

_SRV, _WEB = catalogo_servidor_es(), catalogo_es()
ROTULO_EN_CURSO = _SRV["relato.situacion_en_marcha"]
ROTULO_CANCELADO = _SRV["relato.situacion_cancelado"]
ROTULO_DECIDIR = _SRV["relato.titulo_lo_decides_tu"]
DECISION_INYECTAR = _WEB["tab3.decidiste_inyectar"]

TONE_MAPPING = {
    "target_l8_classification": "tone_mapping",
    "target_l8_max_delta": 41,
    "target_l8_unique_count": 2,
    "target_l8_neutral_frames_pct": 0.0,
    "target_l3_unique_count": 485,
    "target_l3_frames": 182078,
    "target_l2_unique_count": 1605,
    "source_l2_unique_count": 1605,
    "recommended_action": "keep",
    "recommended_action_label": "Lo decides tú — aporta tone-mapping, no autoría",
    "recommended_action_reason": "El bin no trae trims de colorista, pero sí L3 (485 combos).",
    "preflight_decision": "ask_tone_mapping",
    "preflight_message": "El bin no trae trims de colorista, pero sí L3 (485 combos).",
    "target_preflight_ok": False,
    "phase": "created",
}


@unittest.skipIf(NODE is None, "node no está instalado")
class _Base(unittest.TestCase):

    def evaluar(self, casos):
        proc = subprocess.run(
            argv_node(_DRIVER),
            env={**os.environ, "JS_CONCAT": js_en_disco(),
                 "CATALOGO": str(APP_DIR / "static" / "i18n" / "es.json")},
            input=json.dumps([{**c, "s": con_relato(c["s"])} for c in casos]),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return pintar_en(json.loads(proc.stdout))

    def fila(self, filas, clave, titulo=None):
        """La fila por su CLAVE del catálogo, no por un trozo de su rótulo.

        Los rótulos dejaron de nombrar niveles de la spec el 2026-09-19 —la
        fila del L8 se llama «Los ajustes del bin son trabajo de un
        colorista»— y todo test que buscara «L8» dejó de encontrar nada. La
        clave no cambia cuando cambia la redacción.
        """
        rotulo = titulo if titulo is not None else catalogo_servidor()[clave]
        for f in filas:
            if (f.get("titulo") or "") == rotulo:
                return f
        self.fail(f"no hay fila «{rotulo}» en {[f['titulo'] for f in filas]}")


class TestElModalNoAdelantaConclusiones(_Base):
    """Era el punto 1 del usuario: «el paso final de recomendación sale activo
    en lugar de oscurecido porque no ha llegado aún».

    La causa era que la fila se empujaba «si el servidor manda un rótulo», y
    el servidor manda uno SIEMPRE. Ahora el checklist son los hechos del
    relato, que nacen pendientes y se resuelven cuando hay dato, así que
    ninguna fila puede adelantarse **por construcción**: no hay una rama que
    se pueda equivocar.
    """

    def test_a_mitad_del_trabajo_nada_esta_resuelto_de_mas(self):
        filas = self.evaluar([{"fn": "checks", "s": {
            "running_phase": "preflight", "source_preflight_ok": True,
            # El servidor sigue mandando esto; ya no lo lee nadie del modal.
            "recommended_action": "keep",
            "recommended_action_label": "Mantener MKV actual",
        }}])[0]
        resueltos = [f for f in filas if f["estado"] not in ("pend", "curso")]
        self.assertEqual(len(resueltos), 1,
                         "hay filas resueltas sin dato que las sostenga")
        self.assertEqual(resueltos[0]["titulo"],
                         catalogo_servidor()["relato.hecho_origen_dv"])

    def test_exactamente_una_fila_esta_en_curso(self):
        """Una lista entera en gris es lo que hace que un checklist no
        parezca vivo; dos a la vez, que no se sepa dónde va."""
        filas = self.evaluar([{"fn": "checks", "s": {
            "running_phase": "preflight", "source_preflight_ok": True}}])[0]
        self.assertEqual(len([f for f in filas if f["estado"] == "curso"]), 1)

    def test_la_decision_solo_aparece_cuando_la_hay(self):
        sin = self.evaluar([{"fn": "checks", "s": {"running_phase": "preflight"}}])[0]
        con = self.evaluar([{"fn": "checks", "s": TONE_MAPPING}])[0]
        titulo = catalogo_es()["tab3.titulo_decision"]
        self.assertNotIn(titulo, [f["titulo"] for f in sin])
        self.assertIn(titulo, [f["titulo"] for f in con])

    def test_y_cuando_la_hay_dice_qué_se_pregunta(self):
        f = self.fila(self.evaluar([{"fn": "checks", "s": TONE_MAPPING}])[0],
                      None, titulo=catalogo_es()["tab3.titulo_decision"])
        self.assertEqual(f["estado"], "aviso")
        self.assertIn("Inyectar", f["valor"])


class TestLaFilaDelL8HablaCastellano(_Base):

    def test_no_se_escribe_el_identificador_en_crudo(self):
        f = self.fila(self.evaluar([{"fn": "checks", "s": TONE_MAPPING}])[0],
                      "relato.hecho_bin_colorista")
        self.assertNotIn("tone_mapping", f["valor"],
                         "la fila escribe el identificador interno")

    def test_lleva_el_numero_que_decide(self):
        """Desde la recalibración manda el maxΔ, no el conteo: dos combos
        pueden ser un retail (Δ 606) o un generado (Δ 0)."""
        f = self.fila(self.evaluar([{"fn": "checks", "s": TONE_MAPPING}])[0],
                      "relato.hecho_bin_colorista")
        self.assertIn("41", f["valor"])
        self.assertEqual(f["estado"], "aviso")


class TestElVeredictoDistingueLosDosMotivosDeParada(_Base):

    def test_tone_mapping_no_dice_que_el_bin_no_sirve(self):
        """Los DOS veredictos son ámbar, así que la clase no los distingue:
        lo que los separa es el titular, y son dos claves distintas."""
        v = self.evaluar([{"fn": "veredicto", "s": TONE_MAPPING}])[0]
        self.assertEqual(v["clase"], "aviso")
        self.assertEqual(v["titulo"], ROTULO_DECIDIR)

    def test_un_proyecto_cancelado_no_se_titula_con_lo_que_decidio(self):
        """Lo que pasó DESPUÉS manda. El modal decía «Se inyecta el RPU
        igualmente» de un trabajo que el usuario había parado, mientras la
        ficha lo daba por cancelado: la misma discrepancia entre las dos
        superficies que todo esto venía a quitar.

        Se afirma contra la CLAVE del catálogo y no contra la frase: el
        rótulo se reescribió al registro de la app y este test se puso en
        rojo sin que nada del comportamiento cambiara."""
        s = {**TONE_MAPPING, "preflight_decision": "",
             "preflight_user_choice": "inject",
             "phase_history": [{"phase": "analyze_source", "status": "cancelled",
                                "started_at": "2026-09-19T11:40:00Z"}]}
        v = self.evaluar([{"fn": "veredicto", "s": s}])[0]
        self.assertEqual(v["titulo"], ROTULO_CANCELADO)

    def test_el_bin_sintetico_conserva_su_veredicto(self):
        s = {**TONE_MAPPING, "target_l8_classification": "default",
             "preflight_decision": "keep_l8_default"}
        v = self.evaluar([{"fn": "veredicto", "s": s}])[0]
        self.assertEqual(v["clase"], "aviso")
        self.assertNotEqual(v["titulo"], ROTULO_DECIDIR)


class TestLaCardOfreceYRegistraLaDecision(_Base):

    def test_salen_las_dos_salidas(self):
        """Era el punto 3: el único veredicto que existe para que decida el
        usuario era el único sin ningún sitio donde decidirlo."""
        html = self.evaluar([{"fn": "card", "s": TONE_MAPPING}])[0]
        self.assertIn("cmv40AcceptKeep", html)
        self.assertIn("cmv40OverrideRecommendation", html)

    def test_sin_ruta_todavia_el_badge_dice_la_SITUACION(self):
        """El caso de los dos proyectos del 2026-09-19: uno decidido por el
        usuario y otro pasado de largo, los dos con `recommended_action` vacío
        y `recommended_action_label` = «Análisis pendiente». Enseñaban lo
        mismo. Ahora cada uno dice en qué situación está."""
        corriendo = {**TONE_MAPPING, "recommended_action": "",
                     "recommended_action_label": "Análisis pendiente",
                     "preflight_decision": "", "preflight_user_choice": "inject",
                     "running_phase": "analyze_source"}
        parado = {**corriendo, "running_phase": None,
                  "phase_history": [{"phase": "analyze_source",
                                     "status": "cancelled",
                                     "started_at": "2026-09-19T11:40:00Z"}]}
        a, b = self.evaluar([{"fn": "card", "s": corriendo},
                             {"fn": "card", "s": parado}])
        self.assertNotIn("Análisis pendiente", a)
        self.assertNotIn("Análisis pendiente", b)
        self.assertIn(ROTULO_EN_CURSO, a)
        self.assertIn(ROTULO_CANCELADO, b)

    def test_dice_que_esta_esperando(self):
        html = self.evaluar([{"fn": "card", "s": TONE_MAPPING}])[0]
        self.assertIn("cmv40-decision pendiente", html)

    def test_tras_decidir_no_vuelve_a_preguntar_y_deja_constancia(self):
        s = {**TONE_MAPPING, "preflight_user_choice": "inject",
             "preflight_user_choice_at": "2026-09-19T10:06:00+00:00",
             "recommended_action": "drop_in",
             "recommended_action_label": "Inyectar RPU CMv4.0 (rápido)"}
        html = self.evaluar([{"fn": "card", "s": s}])[0]
        self.assertNotIn("cmv40AcceptKeep", html)
        self.assertIn("cmv40-decision tomada", html)
        self.assertIn(DECISION_INYECTAR, html)

    def test_el_ambar_usa_la_paleta_y_no_las_variables_de_la_radiografia(self):
        """`--dv-amber-*` viven DENTRO de `.dv-detail`; citadas desde esta
        card las tres declaraciones caen y el badge se queda sin pintar."""
        html = self.evaluar([{"fn": "card", "s": TONE_MAPPING}])[0]
        self.assertIn("var(--amber-dim)", html)
        self.assertNotIn("--dv-amber", html)

    def test_el_chip_de_calidad_no_es_un_interrogante(self):
        """Caía al «CMv4 ?» del final de la cascada — un interrogante justo
        donde la app sabe exactamente qué es el bin."""
        html = self.evaluar([{"fn": "card", "s": TONE_MAPPING}])[0]
        self.assertNotIn("CMv4 ?", html)
        self.assertIn("solo análisis", html)

    def test_el_l3_sale_en_la_tabla_de_niveles(self):
        html = self.evaluar([{"fn": "card", "s": TONE_MAPPING}])[0]
        self.assertRegex(html, r">L3<")
        self.assertIn("485", html)


class TestLaFaseEnCursoNoOfreceSuBoton(_Base):

    def card(self, s):
        r = self.evaluar([{"fn": "cuerpo", "fase": "G", "s": s}])[0]
        self.assertEqual(r["estado"], "active",
                         "la premisa del caso: la Fase G está activa")
        return r["html"]

    def test_con_una_fase_corriendo_el_cuerpo_va_bloqueado(self):
        html = self.card({"phase": "injected", "running_phase": "remux",
                          "source_workflow": "p7_fel",
                          "target_type": "trusted_p7_fel_final",
                          "target_trust_ok": True})
        self.assertIn("fase-bloqueada", html)
        self.assertIn("cmv40DoRemux", html,
                      "el cuerpo debe seguir explicando la fase")

    def test_esperando_turno_tambien(self):
        """Encolada, el endpoint contesta 409 por el otro guard."""
        html = self.card({"phase": "injected", "cola": {"posicion": 2},
                          "source_workflow": "p7_fel"})
        self.assertIn("fase-bloqueada", html)

    def test_sin_nada_corriendo_el_boton_se_ofrece_normal(self):
        html = self.card({"phase": "injected", "source_workflow": "p7_fel"})
        self.assertNotIn("fase-bloqueada", html)
        self.assertIn("cmv40DoRemux", html)


class TestLaFichaCuentaElTrabajoNoLasHerramientas(_Base):
    """Bloque 3 del hilo. Las cards decían «dovi_tool mux combina BL.hevc +
    EL_injected.hevc en un HEVC dual-layer», que es qué binario corre y no
    qué está pasando con tu película. El detalle no se tira: se pliega."""

    def card_g(self, extra=None):
        s = {"phase": "injected", "source_workflow": "p7_fel",
             "target_type": "trusted_p7_fel_final", "target_trust_ok": True,
             **(extra or {})}
        r = self.evaluar([{"fn": "cuerpo", "fase": "G", "s": s}])[0]
        self.assertEqual(r["estado"], "active")
        return r["html"]

    def test_lo_primero_es_que_le_pasa_a_tu_pelicula(self):
        html = self.card_g()
        humano = catalogo_es()["tab3.que_pasa_fase_g"]
        self.assertIn(humano, html)
        self.assertIn("fase-que-pasa", html)

    def test_y_el_detalle_tecnico_queda_plegado(self):
        html = self.card_g()
        self.assertIn("<details", html)
        self.assertIn("fase-detalle", html)
        # El texto de herramientas sigue estando: se pliega, no se tira.
        self.assertIn("mkvmerge", html)

    def test_el_nombre_de_la_fase_sale_del_relato(self):
        """Había cuatro variantes del rótulo de la Fase A —log, card, columna
        de trabajo y el id interno— y el usuario las veía todas."""
        html = self.card_g()
        from json import loads
        rotulo = loads((APP_DIR / "i18n" / "es.json").read_text(
            encoding="utf-8"))["relato.etapa_remux"]
        self.assertIn(rotulo, html)

    def test_la_fase_en_curso_enseña_el_mismo_porque_que_el_log(self):
        html = self.card_g()
        self.assertIn("fase-porque", html)
        self.assertIn("↩", html)

    def test_una_fase_que_no_es_la_actual_no_lo_enseña(self):
        """En una pendiente sería una promesa y en una terminada, ruido."""
        s = {"phase": "injected", "source_workflow": "p7_fel",
             "target_type": "trusted_p7_fel_final", "target_trust_ok": True}
        r = self.evaluar([{"fn": "cuerpo", "fase": "H", "s": s}])[0]
        self.assertNotIn("fase-porque", r["html"])


class TestElCssQueLoSostiene(unittest.TestCase):
    """Las dos reglas sin las que el JS de arriba no cambia nada en pantalla."""

    def setUp(self):
        self.css = (APP_DIR / "static" / "style.css").read_text(encoding="utf-8")

    def test_el_ambar_esta_en_root(self):
        raiz = self.css[self.css.index(":root"):self.css.index("}", self.css.index(":root"))]
        for v in ("--amber-dim", "--amber-text", "--amber-border"):
            self.assertIn(v, raiz, f"{v} no está en :root — fuera de alcance no pinta")

    def test_el_detalle_plegado_se_distingue_del_texto_principal(self):
        """Sin estilo propio, plegar el detalle no separa nada: las dos capas
        se leerían igual y la card seguiría contando herramientas."""
        self.assertRegex(self.css, r"\.fase-detalle-cuerpo\s*\{[^}]*border-left")
        self.assertRegex(self.css, r"\.fase-que-pasa\s*\{[^}]*color:\s*var\(--text-1\)")

    def test_la_fase_bloqueada_apaga_los_lanzadores(self):
        self.assertRegex(
            self.css, r"\.fase-bloqueada\s+\.btn-primary\s*\{[^}]*pointer-events:\s*none")


if __name__ == "__main__":
    unittest.main()
