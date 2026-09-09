"""El pre-flight se mira mientras pasa, y su veredicto pide una respuesta.

El pre-flight decide **si va a haber trabajo**: valida que el MKV origen tiene
Dolby Vision, obtiene el bin target y comprueba que aporta CMv4.0 y que su L8
no es sintético. Hasta que pasa no se encola nada.

Su veredicto llegaba en diferido: se cerraba el asistente y el motivo aparecía
después como un banner en el panel del proyecto, que hay que estar mirando para
verlo. Con el modal se ve en el momento y, cuando falla, con los motivos
delante.

Lo que este fichero fija:

- **Los tres desenlaces se distinguen.** Abort duro (`error_message`), parada
  con recomendación (`preflight_decision != "ok"`) y OK. No son lo mismo: el
  primero no tiene arreglo desde aquí, el segundo es una decisión del usuario
  y el tercero solo informa de dónde quedó el trabajo.
- **Cerrar no es cancelar.** Mediana 9 s sobre los 91 pre-flights del NAS: si
  el usuario cierra, la validación sigue y el veredicto queda en el panel.
- **El avance es medido**, no un spinner: el pre-flight emite `§§PROGRESS§§`
  como cualquier fase. Sacar el estado de la UI de un regex sobre líneas de
  log es el acoplamiento que este repo ya ha pagado.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_preflight_modal -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
from frontend_sources import html, js_completo  # noqa: E402

NODE = shutil.which("node")
JS = js_completo()

_CHROME_CANDIDATOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
]
CHROME = next((c for c in _CHROME_CANDIDATOS if c and Path(c).exists()), None)


def _fn(nombre: str) -> str:
    i = JS.index(f"function {nombre}(")
    return JS[JS.rindex("\n", 0, i) + 1:JS.index("\n}\n", i) + 3]


def _node(guion: str) -> dict:
    r = subprocess.run([NODE, "-e", guion], capture_output=True, text=True,
                       timeout=30)
    if r.returncode != 0:
        raise AssertionError(f"node falló:\n{r.stderr[:900]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipIf(NODE is None, "node no está instalado")
class TestLosTresDesenlaces(unittest.TestCase):

    def _veredicto(self, sesion, trabajo=None) -> dict:
        guion = f"""
globalThis.escHtml = t => String(t);
{_fn('_cmv40PfMotivosDelLog')}
{_fn('_cmv40PfVeredicto')}
const v = _cmv40PfVeredicto({json.dumps(sesion)}, {json.dumps(trabajo)});
console.log(JSON.stringify({{v}}));
"""
        return _node(guion)["v"]

    def test_mientras_corre_no_hay_veredicto(self):
        self.assertIsNone(self._veredicto(
            {"running_phase": "preflight", "target_preflight_ok": False}))

    def test_abort_duro_es_un_error_sin_salida_desde_aqui(self):
        """El caso típico: el bin no aporta CMv4.0. No hay decisión que tomar
        —ese bin no sirve— así que el modal no ofrece forzar."""
        v = self._veredicto({
            "error_message": "El bin target no aporta CMv4.0 (CM v2.9).",
            "output_log": ["[Pre-flight] Perfil 8, CM v2.9, 1000 frames"]})
        self.assertEqual(v["clase"], "error")
        self.assertIn("CMv4.0", v["cuerpo"])
        self.assertTrue(v["motivos"], "los motivos son las líneas que ya escribió")

    def test_parada_con_recomendacion_NO_es_un_error(self):
        """El bin es sintético: inyectarlo no aporta. Es una decisión, no un
        fallo, y por eso va en ámbar y con las dos salidas."""
        v = self._veredicto({
            "preflight_decision": "keep_l8_default",
            "preflight_message": "2 combos únicos, 99 % de frames neutros"})
        self.assertEqual(v["clase"], "aviso")
        self.assertIn("combos", v["cuerpo"])

    def test_ok_dice_SOLO_donde_quedo_el_trabajo(self):
        """La calidad del bin ya está en su fila; repetirla debajo es ruido.
        Y `target_type` es un identificador interno: no se enseña."""
        v = self._veredicto(
            {"target_preflight_ok": True, "target_type": "trusted_p7_fel_final",
             "target_l8_quality_tier": "full"},
            "La Fase A está en la cola, en el puesto 2.")
        self.assertEqual(v["clase"], "ok")
        self.assertIn("puesto 2", v["cuerpo"])
        self.assertNotIn("trusted_p7_fel_final", v["cuerpo"])

    def test_un_error_manda_sobre_una_decision_previa(self):
        """Si el bin se descargó y falló después, lo accionable es el error."""
        v = self._veredicto({"error_message": "Descarga falló",
                             "preflight_decision": "keep_l8_default"})
        self.assertEqual(v["clase"], "error")


@unittest.skipIf(NODE is None, "node no está instalado")
class TestLasConclusiones(unittest.TestCase):
    """Lo que hace que el modal aporte y no sea un paso intermedio: una fila
    por comprobación, con el dato que la sostiene. Cuando algo falla, la fila
    que falla ES la explicación — no hay que descifrar una frase."""

    def _checks(self, sesion) -> list:
        guion = f"""
globalThis.escHtml = t => String(t);
{_fn('_cmv40PfChecks')}
console.log(JSON.stringify({{f: _cmv40PfChecks({json.dumps(sesion)})}}));
"""
        return _node(guion)["f"]

    def test_al_principio_todo_esta_pendiente_y_se_ve(self):
        f = self._checks({})
        self.assertGreaterEqual(len(f), 4)
        self.assertTrue(all(x["estado"] in ("pend", "curso") for x in f))

    def test_cada_fila_trae_el_dato_que_la_sostiene(self):
        f = self._checks({
            "source_dv_info": {"profile": 7, "el_type": "FEL",
                               "cm_version": "v2.9", "frame_count": 225177},
            "target_dv_info": {"profile": 7, "el_type": "FEL",
                               "cm_version": "v4.0", "frame_count": 225177,
                               "has_l8": True},
            "target_l8_classification": "real", "target_l8_quality_tier": "full",
            "target_l8_unique_count": 412, "target_l8_neutral_frames_pct": 0.08})
        valores = " · ".join(x["valor"] for x in f)
        self.assertIn("Perfil 7 FEL", valores)
        self.assertIn("CM v2.9", valores)
        self.assertIn("CM v4.0", valores)
        self.assertIn("L8 presente", valores)
        self.assertIn("FULL", valores)
        self.assertIn("412 combos", valores)
        self.assertIn("8 % de frames neutros", valores)
        self.assertTrue(all(x["estado"] == "ok" for x in f))

    def test_el_bin_sintetico_marca_SU_fila_en_ambar(self):
        f = self._checks({
            "source_dv_info": {"profile": 7, "cm_version": "v2.9"},
            "target_dv_info": {"profile": 7, "cm_version": "v4.0", "has_l8": True},
            "target_l8_classification": "default", "target_l8_unique_count": 2,
            "target_l8_neutral_frames_pct": 0.99})
        l8 = next(x for x in f if "L8" in x["titulo"])
        self.assertEqual(l8["estado"], "aviso")
        self.assertIn("sintético", l8["valor"])
        # Y las de antes siguen en verde: el fallo está localizado.
        self.assertEqual(f[0]["estado"], "ok")

    def test_el_origen_se_valida_con_el_SNIFF_no_con_el_analisis(self):
        """El pre-flight no analiza el origen: hace un sniff de 30 s que solo
        comprueba que hay NALs de Dolby Vision. El perfil y la CM version los
        saca la Fase A. Enganchada a `source_dv_info`, esta fila se quedaba en
        gris toda la validación porque ese campo no se llena aquí."""
        f = self._checks({"source_preflight_ok": True})
        self.assertEqual(f[0]["estado"], "ok")
        self.assertIn("30 s", f[0]["valor"])
        # Y si la Fase A ya corrió, se enseña el dato bueno.
        f2 = self._checks({"source_preflight_ok": True,
                           "source_dv_info": {"profile": 7, "el_type": "FEL",
                                              "cm_version": "v2.9"}})
        self.assertIn("Perfil 7 FEL", f2[0]["valor"])

    def test_mientras_corre_la_fila_en_curso_se_ve(self):
        """Sin esto la lista se queda entera en gris y solo se rellena al
        final, que es lo que hace que un checklist no parezca vivo."""
        f = self._checks({"running_phase": "preflight",
                          "source_preflight_ok": True})
        self.assertEqual(f[0]["estado"], "ok")
        self.assertEqual(f[1]["estado"], "curso")
        self.assertEqual(f[2]["estado"], "pend")

    def test_terminado_no_deja_ninguna_fila_girando(self):
        f = self._checks({"source_preflight_ok": True, "running_phase": ""})
        self.assertNotIn("curso", [x["estado"] for x in f])

    def test_lo_que_no_se_llego_a_comprobar_lo_dice(self):
        """Con un abort duro, dejar «Analizando los combos…» sugiere que
        sigue trabajando."""
        f = self._checks({
            "source_dv_info": {"profile": 7, "cm_version": "v2.9"},
            "target_dv_info": {"profile": 8, "cm_version": "v2.9"},
            "error_message": "El bin target no aporta CMv4.0 (CM v2.9)."})
        cm = next(x for x in f if "CMv4.0" in x["titulo"])
        self.assertEqual(cm["estado"], "fallo")
        l8 = next(x for x in f if "colorista" in x["titulo"])
        self.assertEqual(l8["valor"], "No se llegó a comprobar")

    def test_no_se_cuela_ningun_identificador_interno(self):
        """La regla del proyecto: nada de IDs técnicos en pantalla."""
        f = self._checks({
            "target_preflight_ok": True, "target_type": "trusted_p7_fel_final",
            "target_l8_classification": "real", "target_l8_quality_tier": "core_rich",
            "recommended_action": "dropin",
            "recommended_action_label": "Inyectar RPU CMv4.0 (drop-in)"})
        texto = " ".join(x["titulo"] + x["valor"] for x in f)
        for interno in ("trusted_p7_fel_final", "core_rich", "keep_l8_default",
                        "target_l8", "dropin"):
            self.assertNotIn(interno, texto)
        self.assertIn("CORE+", texto, "el tier se enseña con su nombre comercial")


@unittest.skipIf(NODE is None, "node no está instalado")
class TestElPieOfreceLoQueTocaEnCadaCaso(unittest.TestCase):

    def _pie(self, veredicto) -> str:
        guion = f"""
globalThis.escHtml = t => String(t);
const _els = {{}};
_els['cmv40-pf-pie'] = {{ innerHTML: '' }};
globalThis.document = {{ getElementById: id => _els[id] || null }};
let _cmv40PfSesion = 'p1';
{_fn('_cmv40PfPintarPie')}
_cmv40PfPintarPie({{}}, {json.dumps(veredicto)});
console.log(JSON.stringify({{ html: _els['cmv40-pf-pie'].innerHTML }}));
"""
        return _node(guion)["html"]

    def test_en_curso_ofrece_cancelar_Y_cerrar(self):
        """Son dos cosas distintas y el modal tiene que dejar hacer las dos:
        cerrar deja la validación corriendo, cancelar la para."""
        h = self._pie(None)
        self.assertIn("cancelarPreflightCMv40()", h)
        self.assertIn("cerrarPreflightCMv40()", h)

    def test_ok_solo_cierra(self):
        h = self._pie({"clase": "ok", "titulo": "", "cuerpo": "", "motivos": []})
        self.assertIn("cerrarPreflightCMv40()", h)
        self.assertNotIn("accept-keep", h)
        self.assertNotIn("cancelarPreflightCMv40()", h)

    def test_el_aviso_ofrece_las_DOS_salidas(self):
        """Mantener el MKV o inyectar igualmente: las dos existen ya como
        endpoints, y son la decisión que el veredicto pide."""
        h = self._pie({"clase": "aviso", "titulo": "", "cuerpo": "",
                       "motivos": []})
        self.assertIn("_cmv40PfMantener('p1')", h)
        self.assertIn("_cmv40PfForzar('p1')", h)
        self.assertIn("_cmv40PfCambiarTarget('p1')", h)

    def test_el_error_NO_ofrece_forzar(self):
        """Un bin sin CMv4.0 no se arregla insistiendo."""
        h = self._pie({"clase": "error", "titulo": "", "cuerpo": "",
                       "motivos": []})
        self.assertNotIn("_cmv40PfForzar", h)
        self.assertIn("_cmv40PfCambiarTarget('p1')", h)


@unittest.skipIf(NODE is None, "node no está instalado")
class TestSeVuelveDesdeLaColumna(unittest.TestCase):
    """Al cerrar el modal, la validación sigue — y hay que poder volver a ella.

    Lo interactivo se listaba en «En paralelo» sin botones, así que un
    pre-flight cuyo modal se hubiera cerrado quedaba fuera de alcance hasta que
    terminara: no había Detalle ni Cancelar como en el trabajo en curso.
    """

    def _render(self, interactivo) -> str:
        guion = f"""
globalThis.escHtml = t => String(t);
{_fn('_svg')}
{_fn('_chipIcono')}
""" + f"""
const _ICONOS_ESTADO = {{ corriendo: ['verde', '<svg/>'] }};
globalThis.iconoDeEstado = () => '<i></i>';
globalThis.iconoDeTrabajo = () => '<i></i>';
{_fn('_workbarTiempo')}
{_fn('_workbarListaHTML')}
const st = {{ activo: null, cola: [], interactivo: {json.dumps(interactivo)} }};
const html = _workbarListaHTML('En paralelo', st.interactivo, t => `
        <div class="workbar-item" data-clave="${{escHtml(t.id)}}">
          ${{escHtml(t.que || '')}}
        </div>
        ${{(t.detalle || t.cancelable) ? `
          <div class="workbar-acciones workbar-acciones-item">
            ${{t.detalle ? `<button onclick="abrirDetalleDeTrabajo('${{escHtml(t.id)}}')">Detalle</button>` : ''}}
            ${{t.cancelable ? `<button onclick="cancelarTrabajoInteractivo('${{escHtml(t.id)}}')">Cancelar</button>` : ''}}
          </div>` : ''}}`);
console.log(JSON.stringify({{ html }}));
"""
        return _node(guion)["html"]

    def test_un_preflight_ofrece_detalle_y_cancelar(self):
        h = self._render([{"id": "p1", "que": "pre-flight de Predator",
                           "segundos": 6, "detalle": "preflight",
                           "cancelable": True}])
        self.assertIn("abrirDetalleDeTrabajo('p1')", h)
        self.assertIn("cancelarTrabajoInteractivo('p1')", h)

    def test_abrir_un_MKV_no_ofrece_nada(self):
        """La mayoría de lo interactivo es navegación: dura segundos y no hay
        nada que seguir ni que parar."""
        h = self._render([{"id": "x", "que": "apertura de un MKV",
                           "segundos": 1, "detalle": "", "cancelable": False}])
        self.assertNotIn("abrirDetalleDeTrabajo", h)
        self.assertNotIn("cancelarTrabajoInteractivo", h)

    def test_la_apertura_busca_tambien_en_lo_interactivo(self):
        """`workbarEstado.activo` es lo DIFERIDO que corre; un pre-flight por
        definición no está ahí."""
        i = JS.index("function abrirDetalleDeTrabajo(")
        cuerpo = JS[i:JS.index("\n}\n", i)]
        self.assertIn("workbarEstado.interactivo", cuerpo)

    def test_su_vista_es_su_propio_modal(self):
        """Devuelve null: el contrato del armazón para «ya lo he enseñado yo».
        Sin eso, se montaría el modal genérico encima."""
        i = JS.index("registrarDetalleDeTrabajo('preflight'")
        bloque = JS[i:JS.index("});", i)]
        self.assertIn("abrirPreflightCMv40", bloque)
        self.assertIn("return null", bloque)
        j = JS.index("async function _trabajoModalAbrir(")
        self.assertIn("=== null) return", JS[j:JS.index("\n}\n", j)])


class TestLoInteractivoLoDeclaraElBackend(ApiTestCase):
    """`/api/trabajos` tiene que decir si un trabajo interactivo se puede
    abrir y parar. Sin eso la columna solo lo lista, y un pre-flight cuyo
    modal se cerró queda fuera de alcance hasta que termine."""

    def setUp(self):
        super().setUp()
        import workload
        workload.limpiar()
        self.addCleanup(workload.limpiar)

    def _interactivo(self):
        r = self.client.get("/api/trabajos")
        self.assertEqual(r.status_code, 200)
        return r.json()["interactivo"]

    def test_el_contrato_lleva_detalle_y_cancelable(self):
        import workload
        workload.registrar("p1", workload.TAB_CMV40, "pre-flight de Predator",
                           workload.CLASE_INTERACTIVO,
                           detalle="preflight", cancelable=True)
        t = self._interactivo()[0]
        self.assertEqual(t["detalle"], "preflight")
        self.assertIs(t["cancelable"], True)
        self.assertEqual(t["sobre"], "p1")

    def test_lo_que_es_navegacion_no_los_lleva(self):
        """Abrir un MKV dura segundos y no hay nada que seguir ni que parar."""
        import workload
        workload.registrar("x", workload.TAB_MKV, "apertura de un MKV",
                           workload.CLASE_INTERACTIVO)
        t = self._interactivo()[0]
        self.assertEqual(t["detalle"], "")
        self.assertIs(t["cancelable"], False)

    def test_el_preflight_SE_declara_al_registrarse(self):
        """Ejecutando el dispatcher, no leyendo su fuente: es lo único que
        garantiza que el registro real lleva los dos campos."""
        import asyncio
        import workload
        from routers import cmv40
        visto = {}
        real = workload.registrar

        def espia(clave, tab, que, clase=workload.CLASE_DIFERIDO, **kw):
            visto.update(kw)
            return real(clave, tab, que, clase, **kw)

        workload.registrar = espia
        self.addCleanup(setattr, workload, "registrar", real)

        # Los pasos reales lanzan ffmpeg y dovi_tool; aquí solo interesa cómo
        # se registra el trabajo. Sin esto, la tarea sigue viva cuando el
        # `asyncio.run` cierra el loop y el teardown escupe un traceback.
        from phases import cmv40_pipeline as pipe
        async def _nada(*a, **kw):
            return None
        for nombre in ("preflight_source", "preflight_target_path",
                       "preflight_target_drive", "preflight_target_mkv"):
            orig = getattr(pipe, nombre)
            setattr(pipe, nombre, _nada)
            self.addCleanup(setattr, pipe, nombre, orig)
        orig_an = cmv40._cmv40_preflight_analyze_target
        async def _analiza(*a, **kw):
            return True
        cmv40._cmv40_preflight_analyze_target = _analiza
        self.addCleanup(setattr, cmv40, "_cmv40_preflight_analyze_target", orig_an)

        sid = self.crear_sesion(sid="cmv40_pf", phase="created")
        import storage
        s = storage.load_cmv40_session(sid)
        s.pending_target_kind = "path"
        s.pending_target_rpu_path = "/no/existe.bin"
        storage.save_cmv40_session(s)

        async def _correr():
            await cmv40._cmv40_dispatch_preflight(s)
            # El dispatcher lanza una tarea; se le da margen a registrar.
            for _ in range(40):
                if visto:
                    break
                await asyncio.sleep(0.02)

        asyncio.run(_correr())
        self.assertEqual(visto.get("detalle"), "preflight")
        self.assertIs(visto.get("cancelable"), True)


class TestCerrarNoCancela(unittest.TestCase):
    """Mediana 9 s: cerrar el modal no puede tirar la validación, y el
    veredicto sigue quedando en el panel como hasta ahora."""

    def test_cerrar_no_llama_al_endpoint_de_cancelar(self):
        i = JS.index("function cerrarPreflightCMv40()")
        cuerpo = JS[i:JS.index("\n}\n", i)]
        self.assertNotIn("/cancel", cuerpo)
        self.assertNotIn("apiFetch", cuerpo)

    def test_cancelar_si_lo_llama_y_corta_la_cadena(self):
        i = JS.index("function cancelarPreflightCMv40()")
        cuerpo = JS[i:JS.index("\n}\n", i)]
        self.assertIn("/cancel", cuerpo)
        self.assertIn("cmv40TrasCancelar", cuerpo)
        self.assertIn("showConfirm", cuerpo, "parar algo se pregunta antes")


class TestElAvanceEsMedido(unittest.TestCase):
    """El pre-flight emite `§§PROGRESS§§` como cualquier fase.

    Antes su avance solo existía como texto en el log, y el modal habría
    tenido que sacarlo de un regex sobre líneas — el acoplamiento que este
    repo ya ha pagado con los parsers de log.
    """

    def test_el_dispatch_emite_los_pasos(self):
        src = (APP_DIR / "routers" / "cmv40.py").read_text(encoding="utf-8")
        i = src.index("async def _cmv40_dispatch_preflight(")
        j = src.index("\nasync def ", i + 10)
        cuerpo = src[i:j]
        self.assertIn("_emit_progress", cuerpo)
        # Los cuatro tramos: origen, bin, validación de CMv4.0 y combos.
        self.assertGreaterEqual(cuerpo.count("await _paso("), 4)

    def test_el_modal_lee_last_progress_no_el_log(self):
        i = JS.index("function _cmv40PfPintar(")
        cuerpo = JS[i:JS.index("\n}\n", i)]
        self.assertIn("last_progress", cuerpo)


@unittest.skipUnless(CHROME, "Chrome/Chromium no disponible")
class TestElModalSeAbreDeVerdad(unittest.TestCase):
    """El único sitio donde se ve si el marcado y el JS encajan."""

    @classmethod
    def setUpClass(cls):
        sesion = {
            "id": "p1", "output_mkv_name": "Predator (2026) [CMv4].mkv",
            "running_phase": "", "target_preflight_ok": False,
            "preflight_decision": "keep_l8_default",
            "preflight_message": "2 combos únicos, 99 % de frames neutros",
            "output_log": ["[Pre-flight] L2: 3 combos · L8: 2 combos únicos",
                           "🛑 Pre-flight: el bin no tiene un L8 trabajado real."],
            "tmdb_info": {"title": "Predator: Tierra de Ojos", "year": 2026,
                          "runtime_minutes": 107, "genres": ["Acción"],
                          "poster_url": ""},
        }
        sonda = ("<script>window.__errores=[];"
                 "window.addEventListener('error',e=>window.__errores.push("
                 "(e.message||'')+' @ '+(e.filename||'').split('/').pop()"
                 f"+':'+e.lineno));window.__S={json.dumps(sesion)};</script>")
        cuerpo = """
<pre id="__out"></pre>
<script>
(function () {
  window.apiFetch = async (url) => url.startsWith('/api/cmv40/')
    ? window.__S : {activo: null, cola: [], interactivo: [], recientes: []};
  setTimeout(async () => {
    await abrirPreflightCMv40('p1');
    await new Promise(r => setTimeout(r, 200));
    const m = document.getElementById('cmv40-preflight-modal');
    document.getElementById('__out').textContent = JSON.stringify({
      errores: window.__errores,
      abierto: m.classList.contains('open'),
      estado: document.getElementById('cmv40-pf-estado').textContent,
      subtitulo: document.getElementById('cmv40-pf-sub').textContent,
      checks: document.getElementById('cmv40-pf-checks').innerHTML,
      titulo: document.getElementById('cmv40-pf-titulo').textContent,
      veredicto: document.getElementById('cmv40-pf-veredicto').innerHTML,
      pie: document.getElementById('cmv40-pf-pie').innerHTML,
      barraOculta: document.getElementById('cmv40-pf-barra-wrap').style.display,
      log: document.getElementById('cmv40-pf-log').textContent,
      logAbierto: document.getElementById('cmv40-pf-detalle').open,
    });
  }, 1200);
})();
</script>
"""
        pagina = html().replace("</head>", sonda + "</head>")
        pagina = pagina.replace("</body>", cuerpo + "</body>")
        pagina = (pagina.replace('src="/static/', 'src="')
                        .replace('href="/static/', 'href="'))
        tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                          encoding="utf-8",
                                          dir=str(APP_DIR / "static"))
        tmp.write(pagina)
        tmp.close()
        try:
            dom = subprocess.run(
                [CHROME, "--headless", "--disable-gpu",
                 "--allow-file-access-from-files", "--dump-dom",
                 "--window-size=1400,900", "--virtual-time-budget=6000",
                 tmp.name], capture_output=True, text=True, timeout=180).stdout
        finally:
            os.unlink(tmp.name)
        m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
        if not m:
            raise unittest.SkipTest("Chrome no devolvió el volcado")
        import html as _h
        cls.d = json.loads(_h.unescape(m.group(1)))

    def test_sin_errores_de_js(self):
        self.assertEqual(self.d["errores"], [])

    def test_la_cabecera_es_la_PELICULA(self):
        """Como en el resto de modales con cartela: el título es de qué
        película se habla. El estado y el veredicto son otra cosa —lo que se
        está haciendo— y van en el cuerpo."""
        self.assertTrue(self.d["abierto"])
        self.assertEqual(self.d["titulo"], "Predator: Tierra de Ojos")
        self.assertIn("2026", self.d["subtitulo"])
        for palabra in ("L8", "Validación", "bin"):
            self.assertNotIn(palabra, self.d["titulo"])

    def test_el_veredicto_encabeza_el_CUERPO(self):
        self.assertIn("L8", self.d["estado"])
        self.assertIn("cmv40-pf-check", self.d["checks"])
        self.assertIn("cmv40-pf-banner aviso", self.d["veredicto"])
        self.assertIn("combos", self.d["veredicto"])

    def test_con_veredicto_la_barra_desaparece(self):
        """Ya no hay nada que medir; dejarla al 65 % sugeriría que sigue."""
        self.assertEqual(self.d["barraOculta"], "none")

    def test_el_registro_esta_a_mano_y_abierto_si_falla(self):
        self.assertIn("Pre-flight", self.d["log"])
        self.assertTrue(self.d["logAbierto"],
                        "con un veredicto que no es OK, el registro se despliega")

    def test_el_pie_pide_la_decision(self):
        self.assertIn("_cmv40PfMantener", self.d["pie"])
        self.assertIn("_cmv40PfForzar", self.d["pie"])


if __name__ == "__main__":
    unittest.main()
