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

from frontend_sources import argv_node, js_en_disco, pintar_en  # noqa: E402

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
            input=json.dumps(casos), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return pintar_en(json.loads(proc.stdout))

    def fila(self, filas, titulo_contiene):
        for f in filas:
            if titulo_contiene.lower() in (f.get("titulo") or "").lower():
                return f
        self.fail(f"no hay fila «{titulo_contiene}» en {[f['titulo'] for f in filas]}")


class TestElModalNoAdelantaConclusiones(_Base):

    def test_la_recomendacion_sale_en_gris_hasta_que_hay_clasificacion(self):
        """El servidor manda `recommended_action_label` desde el primer poll
        —para un proyecto recién creado es «Mantener MKV actual», porque el
        bin aún no está validado—, así que la fila salía en ámbar como una
        conclusión antes de mirar el bin."""
        filas = self.evaluar([{"fn": "checks", "s": {
            "running_phase": "preflight",
            "recommended_action": "keep",
            "recommended_action_label": "Mantener MKV actual",
        }}])[0]
        self.assertEqual(self.fila(filas, "Recomendaci")["estado"], "pend")

    def test_y_aparece_aunque_el_servidor_no_mande_nada(self):
        """La fila es un PASO del checklist: si solo existe cuando ya hay
        respuesta, el usuario no sabe que queda ese paso."""
        filas = self.evaluar([{"fn": "checks", "s": {"running_phase": "preflight"}}])[0]
        self.fila(filas, "Recomendaci")

    def test_con_el_bin_clasificado_la_fila_concluye(self):
        filas = self.evaluar([{"fn": "checks", "s": TONE_MAPPING}])[0]
        f = self.fila(filas, "Recomendaci")
        self.assertEqual(f["estado"], "aviso")
        self.assertIn("decides", f["valor"])


class TestLaFilaDelL8HablaCastellano(_Base):

    def test_no_se_escribe_el_identificador_en_crudo(self):
        f = self.fila(self.evaluar([{"fn": "checks", "s": TONE_MAPPING}])[0],
                      "L8")
        self.assertNotIn("tone_mapping", f["valor"],
                         "la fila escribe el identificador interno")

    def test_lleva_el_numero_que_decide(self):
        """Desde la recalibración manda el maxΔ, no el conteo: dos combos
        pueden ser un retail (Δ 606) o un generado (Δ 0)."""
        f = self.fila(self.evaluar([{"fn": "checks", "s": TONE_MAPPING}])[0], "L8")
        self.assertIn("41", f["valor"])
        self.assertEqual(f["estado"], "aviso")


class TestElVeredictoDistingueLosDosMotivosDeParada(_Base):

    def test_tone_mapping_no_dice_que_el_bin_no_sirve(self):
        v = self.evaluar([{"fn": "veredicto", "s": TONE_MAPPING}])[0]
        self.assertEqual(v["clase"], "aviso")
        self.assertIn("decides", v["titulo"].lower())

    def test_el_bin_sintetico_conserva_su_veredicto(self):
        s = {**TONE_MAPPING, "target_l8_classification": "default",
             "preflight_decision": "keep_l8_default"}
        v = self.evaluar([{"fn": "veredicto", "s": s}])[0]
        self.assertEqual(v["clase"], "aviso")
        self.assertNotIn("decides", v["titulo"].lower())


class TestLaCardOfreceYRegistraLaDecision(_Base):

    def test_salen_las_dos_salidas(self):
        """Era el punto 3: el único veredicto que existe para que decida el
        usuario era el único sin ningún sitio donde decidirlo."""
        html = self.evaluar([{"fn": "card", "s": TONE_MAPPING}])[0]
        self.assertIn("cmv40AcceptKeep", html)
        self.assertIn("cmv40OverrideRecommendation", html)

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
        self.assertIn("Decidiste inyectar", html)

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


class TestElCssQueLoSostiene(unittest.TestCase):
    """Las dos reglas sin las que el JS de arriba no cambia nada en pantalla."""

    def setUp(self):
        self.css = (APP_DIR / "static" / "style.css").read_text(encoding="utf-8")

    def test_el_ambar_esta_en_root(self):
        raiz = self.css[self.css.index(":root"):self.css.index("}", self.css.index(":root"))]
        for v in ("--amber-dim", "--amber-text", "--amber-border"):
            self.assertIn(v, raiz, f"{v} no está en :root — fuera de alcance no pinta")

    def test_la_fase_bloqueada_apaga_los_lanzadores(self):
        self.assertRegex(
            self.css, r"\.fase-bloqueada\s+\.btn-primary\s*\{[^}]*pointer-events:\s*none")


if __name__ == "__main__":
    unittest.main()
