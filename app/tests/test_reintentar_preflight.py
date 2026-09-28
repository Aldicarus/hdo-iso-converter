"""Reintentar tras un pre-flight fallido reintenta EL PRE-FLIGHT.

El bug, reportado el 2026-09-28 con Dune Parte dos: la descarga del bin del
repo DoviTools falló —Google Drive había bloqueado el fichero por cuota— y al
pulsar «Reintentar» en el banner de error arrancó la **Fase A**, doce minutos
de extracción del HEVC que nadie había pedido.

La causa es estructural y vale la pena entenderla: **el pre-flight no es una
fase del pipeline**. No está en `CMV40_FASES_DEF`, así que el banner buscaba
«la fase activa» según `session.phase`, con `created` encontraba la A y
ofrecía relanzarla. El log lo deja ver en dieciocho segundos:

    20:47:32  ✗ Fase preflight FALLÓ: Google Drive ha bloqueado…
    20:47:50  ━━━ Fase A — Analizando el MKV origen ━━━

Lo que **no** se arregla con un 409 en `analyze-source`: la Fase A analiza el
MKV origen y no necesita el bin, así que avanzar mientras la cuota de Drive se
recupera es legítimo. El defecto es que el botón hiciera algo distinto de lo
que dice.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_reintentar_preflight -v
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
from frontend_sources import argv_node  # noqa: E402


def _funcion_js(js: str, nombre: str) -> str:
    """El fuente de una función top-level del frontend.

    Se cuenta desde la llave que abre el CUERPO, no desde el nombre: la
    primera `{` podría ser de un destructuring de la firma, y contando desde
    ahí la función se corta en su propia cabecera — y el error no es legible,
    es un `SyntaxError` en la función siguiente. Mismo criterio que
    `test_radiografia_alcance._funcion`.
    """
    i = js.find(f"\nfunction {nombre}(")
    if i < 0:
        raise AssertionError(f"función no encontrada: {nombre}")
    cierre = js.index(")", i)
    ini = js.index("{", cierre)
    nivel, j = 0, ini
    while j < len(js):
        if js[j] == "{":
            nivel += 1
        elif js[j] == "}":
            nivel -= 1
            if nivel == 0:
                break
        j += 1
    return js[i + 1:j + 1]


# ══════════════════════════════════════════════════════════════════════
#  La condición, en un solo sitio
# ══════════════════════════════════════════════════════════════════════

class TestLaCondicion(unittest.TestCase):

    def _sesion(self, **kw):
        from models import CMv40Session
        base = dict(id="x", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
                    output_mkv_name="o.mkv")
        base.update(kw)
        return CMv40Session(**base)

    def test_con_target_pendiente_sin_validar_falta(self):
        from routers.cmv40 import falta_el_preflight_del_target
        self.assertTrue(falta_el_preflight_del_target(
            self._sesion(pending_target_kind="repo", target_preflight_ok=False)))

    def test_ya_validado_no_falta(self):
        from routers.cmv40 import falta_el_preflight_del_target
        self.assertFalse(falta_el_preflight_del_target(
            self._sesion(pending_target_kind="repo", target_preflight_ok=True)))

    def test_sin_target_elegido_no_falta(self):
        """Crear el proyecto sin target es válido: la Fase A corre igual y la
        Fase B pide acción manual."""
        from routers.cmv40 import falta_el_preflight_del_target
        self.assertFalse(falta_el_preflight_del_target(
            self._sesion(pending_target_kind="", target_preflight_ok=False)))

    def test_el_orquestador_usa_ESTA_condicion(self):
        """Estaba escrita a mano en el orquestador. Dos definiciones de lo
        mismo se desincronizan, y de esa familia era el bug del overlay."""
        src = (APP_DIR / "routers" / "cmv40.py").read_text(encoding="utf-8")
        self.assertNotIn("not fresh.target_preflight_ok and fresh.pending_target_kind",
                         src, "el orquestador volvió a derivarlo a mano")
        self.assertIn("falta_el_preflight_del_target(fresh)", src)


# ══════════════════════════════════════════════════════════════════════
#  El endpoint de reintento
# ══════════════════════════════════════════════════════════════════════

class TestElEndpoint(ApiTestCase):

    def _crear(self, **kw):
        base = dict(pending_target_kind="repo",
                    pending_target_file_id="1gN0DV",
                    pending_target_file_name="Dune.Part.Two…P5 to P8.bin",
                    target_preflight_ok=False,
                    error_message="Descarga del repo DoviTools falló: Google "
                                  "Drive ha bloqueado temporalmente…")
        base.update(kw)
        sid = self.crear_sesion(**base)
        return sid

    def test_lanza_el_PREFLIGHT_y_no_la_fase_A(self):
        """El bug en una línea.

        Se afirma sobre a qué DISPATCHER se llama, no sobre el espía de
        fases: ese registra cuando la task corre, y con el TestClient eso
        depende de cuándo ceda el loop — la mutación pasaba en verde.
        """
        from unittest.mock import patch
        sid = self._crear()
        llamados = []

        async def _pre(session): llamados.append("preflight")
        async def _fase(session, fase): llamados.append(f"fase:{fase}")

        with patch.object(self.cmv40, "_cmv40_dispatch_preflight", _pre), \
             patch.object(self.cmv40, "_cmv40_dispatch_phase", _fase):
            r = self.client.post(f"/api/cmv40/{sid}/retry-preflight")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("started"))
        self.assertEqual(llamados, ["preflight"],
                         f"tenía que reintentar el pre-flight, y llamó a {llamados}")

    def test_el_error_ya_esta_limpio_al_RESPONDER(self):
        """Y no solo dentro del pre-flight, que lo limpia ya en su task.

        El frontend responde al 200 con un `GET`; sin esto pillaría el banner
        del fallo anterior todavía puesto, y un banner que reaparece tras
        pulsar «Reintentar» se lee como que el reintento no ha hecho nada.

        Se mide en el INSTANTE del despacho: con el TestClient la task corre
        igual, así que comprobarlo después pasa de las dos formas — lo
        destapó la mutación.
        """
        from unittest.mock import patch
        sid = self._crear()
        visto = {}

        async def _mirar(session):
            visto["error"] = session.error_message
            visto["persistido"] = self.leer_sesion(session.id).error_message

        with patch.object(self.cmv40, "_cmv40_dispatch_preflight", _mirar):
            r = self.client.post(f"/api/cmv40/{sid}/retry-preflight")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(visto.get("error"), "el objeto llega con el error puesto")
        self.assertFalse(visto.get("persistido"),
                         "el error sigue en disco: el GET del frontend lo vería")

    def test_sin_preflight_pendiente_da_409(self):
        """Un botón que solo sabe dar un error es peor que uno que no está,
        así que la UI no lo ofrece — pero el endpoint tiene que decirlo."""
        sid = self._crear(target_preflight_ok=True)
        r = self.client.post(f"/api/cmv40/{sid}/retry-preflight")
        self.assertEqual(r.status_code, 409, r.text)

    def test_sin_target_elegido_da_409(self):
        sid = self._crear(pending_target_kind="")
        self.assertEqual(
            self.client.post(f"/api/cmv40/{sid}/retry-preflight").status_code, 409)

    def test_un_proyecto_que_no_existe_da_404(self):
        self.assertEqual(
            self.client.post("/api/cmv40/no_existe/retry-preflight").status_code, 404)

    def test_con_una_fase_en_curso_da_409(self):
        sid = self._crear(running_phase="analyze_source")
        self.assertEqual(
            self.client.post(f"/api/cmv40/{sid}/retry-preflight").status_code, 409)

    def test_el_detalle_lo_sirve_para_que_la_UI_no_lo_replique(self):
        sid = self._crear()
        d = self.client.get(f"/api/cmv40/{sid}").json()
        self.assertTrue(d["falta_preflight_del_target"])

    def test_y_dice_False_cuando_no_falta(self):
        sid = self._crear(target_preflight_ok=True)
        d = self.client.get(f"/api/cmv40/{sid}").json()
        self.assertFalse(d["falta_preflight_del_target"])


# ══════════════════════════════════════════════════════════════════════
#  El banner, que es donde el usuario pulsó
# ══════════════════════════════════════════════════════════════════════

class TestQueSeReintenta(unittest.TestCase):
    """`_cmv40QueReintentar`, evaluada en node con el estado del bug.

    Un guard sobre el fuente no serviría: los dos botones están ahí, lo que se
    comprueba es CUÁL sale. Y la decisión vive en su propia función justo para
    poder ejecutarla — la que la usa (`_renderCMv40ActivePhase`) arrastra medio
    módulo.
    """

    @classmethod
    def setUpClass(cls):
        from frontend_sources import js_completo
        cls.JS = js_completo()
        if "function _cmv40QueReintentar(" not in cls.JS:
            raise AssertionError("la función no está en el frontend")

    def _decidir(self, sesion: dict):
        guion = "\n".join([
            "const CMV40_FASES_DEF = " + json.dumps([
                {"key": "A", "produces": "source_analyzed", "startsFrom": "created",
                 "title": "Fase A — Analizar MKV origen"},
                {"key": "C", "produces": "extracted", "startsFrom": "target_provided",
                 "title": "Fase C — Extraer BL/EL"},
            ]) + ";",
            # El estado de fase, con el contrato que la función espera.
            "function _cmv40PhaseState(phase, produces, startsFrom) {"
            "  return phase === startsFrom ? 'active' : 'pending'; }",
            _funcion_js(self.JS, "_cmv40QueReintentar"),
            f"const S = {json.dumps(sesion)};",
            "process.stdout.write(JSON.stringify(_cmv40QueReintentar(S)));",
        ])
        r = subprocess.run(argv_node(guion), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return json.loads(r.stdout)

    BASE = {"phase": "created",
            "error_message": "Descarga del repo DoviTools falló: Google Drive "
                             "ha bloqueado temporalmente la descarga."}

    def test_con_el_preflight_pendiente_se_reintenta_EL_PREFLIGHT(self):
        """El bug en una línea: aquí salía `fase A`."""
        d = self._decidir(dict(self.BASE, falta_preflight_del_target=True))
        self.assertEqual(d["tipo"], "preflight")

    def test_y_gana_al_de_la_fase_aunque_haya_una_activa(self):
        """Con `phase=created` la Fase A ESTÁ activa: lo que decide es que el
        pre-flight va primero."""
        d = self._decidir(dict(self.BASE, phase="created",
                               falta_preflight_del_target=True))
        self.assertEqual(d["tipo"], "preflight")
        self.assertNotIn("key", d)

    def test_si_lo_que_fallo_es_una_FASE_se_reintenta_la_fase(self):
        """El camino de siempre no se toca."""
        d = self._decidir(dict(self.BASE, falta_preflight_del_target=False,
                               error_message="dovi_tool inject-rpu falló"))
        self.assertEqual(d["tipo"], "fase")
        self.assertEqual(d["key"], "A")

    def test_sin_fase_activa_no_ofrece_nada(self):
        """Un botón que solo sabe dar un error es peor que uno que no está."""
        d = self._decidir(dict(self.BASE, phase="done",
                               falta_preflight_del_target=False))
        self.assertIsNone(d)



class TestLaCardDeLaFaseA(unittest.TestCase):
    """El caso en el que el usuario se quedó encallado.

    El primer arreglo puso el botón sólo en el banner de error, y el banner se
    descarta con su X — que es justo lo que el usuario había hecho al pulsar
    «Reintentar» la primera vez. Resultado: `error_message` vacío, ningún
    banner, y el proyecto sin ninguna forma de volver a intentar la descarga:
    sólo «Analizar origen», que es lo que no había pedido. Reportado el
    2026-09-28 con Dune Parte dos.

    Así que el estado tiene que ser accionable donde se VE, no colgando de un
    mensaje que se puede cerrar.
    """

    @classmethod
    def setUpClass(cls):
        from frontend_sources import js_completo
        cls.JS = js_completo()

    def _card(self, sesion: dict) -> str:
        guion = "\n".join([
            "function _cmv40DosCapas(h, t) { return h + (t || ''); }",
            _funcion_js(self.JS, "_cmv40FaseABody"),
            f"const S = {json.dumps(sesion)};",
            "process.stdout.write(_cmv40FaseABody('p1', S));",
        ])
        r = subprocess.run(argv_node(guion), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return r.stdout

    def test_con_el_bin_sin_validar_ofrece_VALIDARLO(self):
        """Aunque el error se haya descartado: la condición es el estado."""
        html = self._card({"falta_preflight_del_target": True, "error_message": ""})
        self.assertIn("_cmv40RetryPreflight", html)

    def test_y_sigue_ofreciendo_analizar_el_origen(self):
        """No necesita el bin, así que adelantarlo mientras la cuota de Drive
        se recupera es una decisión legítima — y es del usuario."""
        html = self._card({"falta_preflight_del_target": True, "error_message": ""})
        self.assertIn("cmv40DoAnalyzeSource", html)

    def test_y_lo_dice_en_vez_de_dejarlo_adivinar(self):
        html = self._card({"falta_preflight_del_target": True, "error_message": ""})
        self.assertIn("tab3.el_bin_sigue_sin_validar", html)

    def test_sin_bin_pendiente_la_card_no_cambia(self):
        """El camino normal se queda como estaba: un solo botón."""
        html = self._card({"falta_preflight_del_target": False})
        self.assertIn("cmv40DoAnalyzeSource", html)
        self.assertNotIn("_cmv40RetryPreflight", html)
        self.assertNotIn("tab3.el_bin_sigue_sin_validar", html)


if __name__ == "__main__":
    unittest.main()
