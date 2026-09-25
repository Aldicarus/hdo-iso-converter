"""El relato: una sola respuesta a «qué está pasando», para las cinco superficies.

Un job de CMv4.0 le habla al usuario por cinco sitios y hasta ahora cada uno
derivaba su propia idea del estado. Lo que el usuario vio el 2026-09-19, con
dos proyectos suyos delante:

  · uno que él decidió inyectar y otro que pasó de largo por tener L8 real
    enseñaban **el mismo rótulo** en la ficha, «Análisis pendiente»;
  · y al cancelarlos, la ficha no mencionaba la cancelación por ninguna parte
    — el hecho más importante del proyecto vivía solo en el log.

Este módulo fija que el relato distingue esos casos, y que lo hace **sobre la
sesión y nada más**: es puro, así que sus combinaciones se recorren en
milisegundos igual que las 48 de `cmv40_strategy`.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_relato -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

import relato  # noqa: E402


def _texto(clave: str) -> str:
    """El castellano de una clave del servidor. Ver `catalogo_servidor_es`."""
    from frontend_sources import catalogo_servidor_es
    return catalogo_servidor_es()[clave]
from models import CMv40Session, CMv40PhaseRecord, DoviInfo  # noqa: E402


def cancelada(fase: str) -> CMv40PhaseRecord:
    """Una entrada de historial como la que deja el cancel."""
    from datetime import datetime, timezone
    return CMv40PhaseRecord(phase=fase, status="cancelled",
                            started_at=datetime.now(timezone.utc))
from phases import tab1_relato, tab2_relato  # noqa: E402
from phases.cmv40_relato import (ETAPAS, porque_de_fase,  # noqa: E402
                                 resolver)


def sesion(**campos) -> CMv40Session:
    base = dict(id="cmv40_t_1700000000", source_mkv_path="/x/a.mkv",
                source_mkv_name="a.mkv", output_mkv_name="a.mkv")
    base.update(campos)
    return CMv40Session(**base)


def bin_analizado(clase: str, **extra) -> dict:
    """Lo que el pre-flight deja escrito tras analizar el bin."""
    return dict(
        target_dv_info=DoviInfo(profile=7, el_type="FEL", cm_version="v4.0",
                                frame_count=222274),
        source_preflight_ok=True,
        target_l8_classification=clase,
        target_l8_max_delta=41 if clase == "tone_mapping" else 328,
        target_l8_unique_count=2,
        pending_target_file_name="Pulp.Fiction.1994.bin",
        **extra)


class TestLosDosProyectosQueContabanLoMismo(unittest.TestCase):
    """El caso reportado, con sus dos sesiones."""

    def test_el_que_decidiste_y_el_que_paso_de_largo_no_se_confunden(self):
        decidido = sesion(**bin_analizado("tone_mapping"),
                          preflight_user_choice="inject",
                          preflight_user_choice_at="2026-09-19T11:38:42Z",
                          running_phase="analyze_source")
        de_largo = sesion(**bin_analizado("real"), preflight_decision="ok",
                          running_phase="analyze_source")

        a, b = resolver(decidido), resolver(de_largo)
        self.assertEqual(a["decision"]["estado"], relato.DECISION_TOMADA)
        self.assertEqual(a["decision"]["elegida"], "inject")
        self.assertEqual(b["decision"]["estado"], relato.DECISION_NO_PROCEDE)
        self.assertNotEqual(a["porque"], b["porque"],
                            "los dos proyectos siguen contando lo mismo")

    def test_la_decision_pendiente_trae_la_pregunta_y_las_dos_salidas(self):
        s = sesion(**bin_analizado("tone_mapping"),
                   preflight_decision="ask_tone_mapping",
                   preflight_message="El bin no trae ajustes…")
        r = resolver(s)
        self.assertEqual(r["situacion"], relato.ESPERANDO_DECISION)
        self.assertEqual(r["decision"]["estado"], relato.DECISION_PENDIENTE)
        self.assertEqual([o["id"] for o in r["decision"]["opciones"]],
                         ["keep", "inject"])
        self.assertTrue(r["porque"], "no dice por qué te está preguntando")


class TestLaCancelacionSeVe(unittest.TestCase):
    """Era el punto más claro: el log lo contaba y la ficha no."""

    def _cancelado(self):
        return sesion(
            **bin_analizado("tone_mapping"), preflight_user_choice="inject",
            phase_history=[cancelada("analyze_source")])

    def test_la_situacion_lo_dice(self):
        self.assertEqual(resolver(self._cancelado())["situacion"],
                         relato.CANCELADO)

    def test_y_dice_en_qué_etapa_se_quedo(self):
        """Al cancelar, `phase` vuelve a `created`: preguntarle a la sesión
        contesta «Validación previa» de un trabajo que murió extrayendo el
        HEVC. Lo destapó el prototipo sobre el job real, no leer el código."""
        r = resolver(self._cancelado())
        self.assertEqual(r["etapa"]["id"], "analyze_source")
        self.assertIn("Fase A", r["etapa"]["rotulo"])

    def test_una_fase_corriendo_manda_sobre_un_cancelado_viejo(self):
        s = self._cancelado()
        s.running_phase = "inject"
        r = resolver(s)
        self.assertEqual(r["situacion"], relato.EN_MARCHA)
        self.assertEqual(r["etapa"]["id"], "inject")


class TestLaSituacionEsUnaYExcluyente(unittest.TestCase):

    def test_cada_estado_da_su_situacion(self):
        casos = [
            (sesion(), relato.PREPARANDO),
            (sesion(running_phase="extract"), relato.EN_MARCHA),
            (sesion(error_message="petó"), relato.DETENIDO_POR_ERROR),
            (sesion(phase="done"), relato.TERMINADO),
            (sesion(archived=True), relato.ARCHIVADO),
            (sesion(preflight_decision="ask_tone_mapping"),
             relato.ESPERANDO_DECISION),
        ]
        for s, esperada in casos:
            with self.subTest(esperada=esperada):
                self.assertEqual(resolver(s)["situacion"], esperada)

    def test_la_cola_se_distingue_de_estar_preparando(self):
        s = sesion()
        self.assertEqual(resolver(s, en_cola=None)["situacion"],
                         relato.PREPARANDO)
        self.assertEqual(resolver(s, en_cola={"posicion": 2})["situacion"],
                         relato.ESPERANDO_TURNO)

    def test_archivado_gana_a_un_error_arrastrado(self):
        """El orden ES la decisión: en un proyecto archivado ya no hay nada
        que resolver, así que decir «detenido por error» sería pedir una
        acción que no existe."""
        s = sesion(archived=True, error_message="algo viejo")
        self.assertEqual(resolver(s)["situacion"], relato.ARCHIVADO)

    def test_todas_las_situaciones_del_vocabulario_son_alcanzables(self):
        """Una situación que nadie produce es vocabulario muerto que la UI
        tendría que pintar igual.

        Se recorren **los tres** resolutores: el vocabulario es común, así que
        una palabra que solo produjera una pestaña la pagarían las otras dos
        en forma de tabla de pintado con una fila que nunca se usa.
        """
        vistas = {resolver(s)["situacion"] for s in (
            sesion(), sesion(running_phase="extract"),
            sesion(error_message="x"), sesion(phase="done"),
            sesion(archived=True), sesion(preflight_decision="ask_tone_mapping"),
            sesion(phase_history=[cancelada("inject")]),
        )}
        vistas.add(resolver(sesion(), en_cola={"posicion": 1})["situacion"])
        vistas |= {tab1_relato.resolver(s)["situacion"] for s in (
            {"id": "x"}, {"id": "x", "status": "running"},
            {"id": "x", "status": "queued"}, {"id": "x", "status": "error"},
            {"id": "x", "status": "done"},
            {"id": "x", "last_cancelled_at": "2026-09-19T10:00:00Z"},
        )}
        vistas |= {tab2_relato.resolver(t)["situacion"] for t in (
            {"existe": False},
            {"existe": True, "tiene_extendido": True},
            {"existe": True, "tiene_basico": True},
            {"existe": True},
        )}
        self.assertEqual(vistas, set(relato.SITUACIONES))


class TestLosHechosSonLaMismaLista(unittest.TestCase):
    """`hechos` es lo que pintan el checklist del modal y el resumen de la
    ficha. Antes eran dos derivaciones de los mismos campos."""

    def test_van_de_pendiente_a_resuelto_segun_avanza(self):
        vacio = {h["id"]: h["estado"] for h in resolver(sesion())["hechos"]}
        lleno = {h["id"]: h["estado"]
                 for h in resolver(sesion(**bin_analizado("real")))["hechos"]}
        self.assertEqual(vacio["bin_cmv40"], relato.HECHO_PENDIENTE)
        self.assertEqual(lleno["bin_cmv40"], relato.HECHO_OK)
        self.assertEqual(lleno["bin_colorista"], relato.HECHO_OK)

    def test_el_tercer_veredicto_es_un_aviso_no_un_fallo(self):
        h = {x["id"]: x for x in
             resolver(sesion(**bin_analizado("tone_mapping")))["hechos"]}
        self.assertEqual(h["bin_colorista"]["estado"], relato.HECHO_AVISO)
        self.assertIn("41", h["bin_colorista"]["evidencia"])

    def test_un_hecho_pendiente_no_inventa_evidencia(self):
        """El prototipo escribía «Perfil None» como evidencia de un bin sin
        analizar. Una cifra inventada con pinta de dato es peor que el hueco.
        """
        h = {x["id"]: x for x in resolver(sesion())["hechos"]}
        self.assertEqual(h["bin_cmv40"]["evidencia"], "")
        self.assertNotIn("None", str(h))

    def test_ni_siquiera_con_un_dv_info_a_medias(self):
        """`DoviInfo.profile` vale **0** por defecto, así que un análisis que
        no llegó a leer el perfil deja el objeto puesto y vacío. Sin el guard
        eso se pinta como «Perfil 0» debajo de «El bin aporta CMv4.0»."""
        h = {x["id"]: x
             for x in resolver(sesion(target_dv_info=DoviInfo()))["hechos"]}
        self.assertEqual(h["bin_cmv40"]["evidencia"], "")
        self.assertNotIn("Perfil 0", str(h))

    def test_los_ids_son_estables(self):
        """La UI ancla en ellos, y los tests también."""
        self.assertEqual([h["id"] for h in resolver(sesion())["hechos"]],
                         ["origen_dv", "bin_obtenido", "bin_cmv40",
                          "bin_colorista"])


class TestCadaFaseDiceDeDondeViene(unittest.TestCase):
    """El punto 7 del mapa: ninguna fase se refería a lo medido antes.

    La regla del proyecto prohíbe PROMETER la fase siguiente —nació de
    promesas que quedaban colgando al cancelar— pero no dice nada de mirar
    atrás, y nadie lo hacía.
    """

    def _plan(self, s):
        from phases.cmv40_strategy import resolve_plan
        return resolve_plan(s)

    def test_la_fase_a_cuenta_lo_que_dejo_el_pre_flight(self):
        s = sesion(**bin_analizado("real"))
        t = porque_de_fase(s, "analyze_source", plan=self._plan(s))
        self.assertIn("Perfil 7 FEL", t)        # el dato
        self.assertIn("colorista", t)           # y el veredicto

    def test_la_fase_b_cuenta_lo_que_leyo_la_fase_a(self):
        from models import DoviInfo
        s = sesion(source_dv_info=DoviInfo(profile=7, el_type="FEL",
                                           cm_version="v2.9", frame_count=1000))
        self.assertIn("CM v2.9", porque_de_fase(s, "target_rpu_drive"))

    def test_sin_dato_no_se_dice_nada(self):
        """Una frase vacía de contenido cada vez que arranca una fase es
        ruido, y el log de un job largo ya tiene bastante."""
        self.assertEqual(porque_de_fase(sesion(), "analyze_source"), "")
        self.assertEqual(porque_de_fase(sesion(), "target_rpu_drive"), "")

    def test_la_fase_c_se_ancla_en_lo_que_RAMIFICA(self):
        """No en `drop_in` sino en `needs_demux`, que es sobre lo que la fase
        decide. Si la explicación y la decisión salen de campos distintos
        pueden contar cosas distintas — el fallo que cerró `cmv40_strategy`.
        """
        from phases.cmv40_strategy import resolve_plan
        # `p8` es el caso que los separa: no va por drop-in **y** tampoco
        # tiene capas que separar, así que anclar en `drop_in` haría decir
        # «hay que recomponer el RPU» de una fase que no hace nada. Medido
        # sobre la matriz: 16 de las 48 combinaciones discrepan, todas P8.
        for wf, tipo, trust in (("p7_fel", "trusted_p7_fel_final", True),
                                ("p7_fel", "generic", False),
                                ("p7_mel", "trusted_p7_mel_final", True),
                                ("p8", "trusted_p8_source", True),
                                ("p8", "generic", False)):
            s = sesion(source_workflow=wf, target_type=tipo,
                       target_trust_ok=trust)
            plan = resolve_plan(s)
            t = porque_de_fase(s, "extract", plan=plan)
            # Contra la CLAVE que tocaba, no contra una palabra suelta de la
            # frase: la redacción se reescribió al registro de la app y este
            # test se puso en rojo sin que la decisión cambiara.
            esperada = _texto('relato.porque_fase_c_merge'
                              if plan.extract.needs_demux
                              else 'relato.porque_fase_c_dropin')
            with self.subTest(wf=wf, demux=plan.extract.needs_demux):
                self.assertEqual(t, esperada)

    def test_la_fase_f_distingue_sync_revisada_de_omitida(self):
        omitida = sesion(phases_skipped=["sync_verification_pause"])
        revisada = sesion(sync_delta=0)
        self.assertEqual(porque_de_fase(omitida, "inject"),
                         _texto('relato.porque_fase_f_sin_revisar'))
        self.assertEqual(porque_de_fase(revisada, "inject"),
                         _texto('relato.porque_fase_f_sync_ok'))

    def test_todas_las_fases_del_orquestador_tienen_su_linea(self):
        """Si una fase se queda sin justificación, el hilo se corta ahí — y
        el orquestador la emite para TODAS, así que el hueco sería mudo."""
        from routers.cmv40 import _FASE_CORTA
        from phases.cmv40_strategy import resolve_plan
        s = sesion(**bin_analizado("real"),
                   source_dv_info=__import__("models").DoviInfo(
                       profile=7, el_type="FEL", cm_version="v2.9",
                       frame_count=1000),
                   source_workflow="p7_fel",
                   target_type="trusted_p7_fel_final", target_trust_ok=True,
                   output_mkv_name="x.mkv")
        plan = resolve_plan(s)
        sin = [f for f in _FASE_CORTA
               if f != "correct_sync"                 # Fase E se repite dentro de D
               and not porque_de_fase(s, f, plan=plan)]
        self.assertEqual(sin, [])


class TestElSiguienteEsSoloDeInterfaz(unittest.TestCase):

    def test_hay_siguiente_mientras_el_trabajo_avanza(self):
        r = resolver(sesion(running_phase="analyze_source"))
        self.assertTrue(r["siguiente"])

    def test_pero_no_cuando_el_trabajo_esta_parado(self):
        """Prometer la etapa siguiente de algo detenido es la promesa
        colgando que la regla del proyecto prohíbe desde que una fase
        anunciaba la próxima y al cancelar quedaba dicha."""
        for s in (sesion(preflight_decision="ask_tone_mapping"),
                  sesion(error_message="x"), sesion(phase="done"),
                  sesion(archived=True)):
            with self.subTest(sit=resolver(s)["situacion"]):
                self.assertEqual(resolver(s)["siguiente"], "")


class TestElSiguienteNoLlegaAlLog(unittest.TestCase):
    """El guard: `siguiente` es un rótulo de interfaz y el log no puede
    escribirlo. Se comprueba sobre el fuente porque es una prohibición, no un
    comportamiento — no hay ejecución que la pueda enseñar."""

    def test_ningun_emisor_de_log_lee_siguiente(self):
        malos = []
        for ruta in (APP_DIR / "phases").glob("cmv40*.py"):
            src = ruta.read_text(encoding="utf-8")
            for n, linea in enumerate(src.splitlines(), 1):
                if '"siguiente"' in linea and ("log" in linea or "_log" in linea):
                    malos.append(f"{ruta.name}:{n}")
        self.assertEqual(malos, [])


class TestElRegistroNoConoceNingunaPestana(unittest.TestCase):

    def test_una_pestana_sin_resolutor_devuelve_none(self):
        self.assertIsNone(relato.resolver("✏️ pestaña inventada", sesion()))

    def test_un_resolutor_que_peta_no_tumba_la_peticion(self):
        """Quedarse sin relato es un inconveniente; no poder abrir el
        proyecto, no. Mismo criterio que el resto de textos derivados."""
        def revienta(_s, **kw):
            raise RuntimeError("boom")
        relato.registrar("tab_de_prueba", revienta)
        self.addCleanup(relato._resolutores.pop, "tab_de_prueba", None)
        self.assertIsNone(relato.resolver("tab_de_prueba", sesion()))


class TestNadieTraduceAlImportar(unittest.TestCase):
    """Una tabla de rótulos en el ámbito del módulo se evalúa UNA vez y
    congela el idioma del arranque del contenedor. Apareció nueve veces."""

    def test_las_etapas_son_ids_no_textos(self):
        for etapa in ETAPAS:
            self.assertRegex(etapa, r"^[a-z_]+$",
                             "una etapa lleva texto traducible dentro")


class TestElEndpointLoSirve(unittest.TestCase):
    """Las funciones puras no prueban el cableado: el relato tiene que llegar
    al frontend por el mismo sitio y con la misma forma que `plan`."""

    @classmethod
    def setUpClass(cls):
        from api_harness import ApiTestCase
        cls.caso = type("_C", (ApiTestCase,), {"runTest": lambda s: None})()
        cls.caso.setUpClass()
        cls.caso.setUp()

    @classmethod
    def tearDownClass(cls):
        cls.caso.doCleanups()

    def test_viaja_en_el_detalle_del_proyecto(self):
        sid = self.caso.crear_sesion(
            sid="cmv40_relato", phase="created",
            preflight_decision="ask_tone_mapping",
            target_l8_classification="tone_mapping",
            target_l8_max_delta=41, target_l8_unique_count=2,
            preflight_message="El bin no trae ajustes…")
        d = self.caso.client.get(f"/api/cmv40/{sid}?include_log=false").json()

        self.assertIn("relato", d, "el endpoint no sirve el relato")
        r = d["relato"]
        self.assertEqual(r["situacion"], relato.ESPERANDO_DECISION)
        self.assertEqual(r["decision"]["estado"], relato.DECISION_PENDIENTE)
        self.assertEqual(r["etapa"]["total"], len(ETAPAS))
        self.assertTrue(r["hechos"])

    def test_no_se_persiste(self):
        """Como `plan`: se calcula al servir. Si entrara en el modelo,
        Pydantic lo ignoraría al cargar y el primer save lo borraría — con
        los proyectos del /config de un usuario eso no tiene vuelta atrás."""
        sid = self.caso.crear_sesion(sid="cmv40_relato_2", phase="created")
        self.caso.client.get(f"/api/cmv40/{sid}")
        crudo = self.caso.leer_sesion(sid).model_dump()
        self.assertNotIn("relato", crudo)


class TestElOrquestadorEmiteElPorque(unittest.TestCase):
    """Que la función exista no basta: hay que ejecutar el SITIO que la usa.

    Es la lección del 2026-09-19 por la mañana — un test que llamaba al
    helper en vez de al sitio que lo llama dejó pasar la mutación entera. El
    porqué se emite en `_run_cmv40_phase_locked`, en un solo punto, para que
    ninguna fase pueda quedarse sin él.
    """

    def _correr_una_fase(self, session):
        import asyncio
        from routers import cmv40
        from api_harness import ApiTestCase

        caso = type("_C", (ApiTestCase,), {"runTest": lambda s: None})()
        caso.setUpClass(); caso.setUp()
        self.addCleanup(caso.doCleanups)

        import storage
        storage.save_cmv40_session(session)
        emitidas = []
        orig = cmv40._cmv40_log

        async def _espia(s, msg):
            emitidas.append(msg)
        cmv40._cmv40_log = _espia
        self.addCleanup(setattr, cmv40, "_cmv40_log", orig)

        async def _coro(log_cb, proc_cb):
            return None

        asyncio.run(cmv40._run_cmv40_phase_locked(
            session, "analyze_source", _coro, "source_analyzed",
            asyncio.Lock()))
        return emitidas

    def test_la_fase_abre_contando_de_donde_viene(self):
        s = sesion(**bin_analizado("real"))
        emitidas = self._correr_una_fase(s)
        porque = [l for l in emitidas if "↩" in l]
        self.assertTrue(porque, "la fase arranca sin decir de dónde viene")
        self.assertIn("[Fase A]", porque[0],
                      "la línea no se lee como una más de la fase")
        self.assertIn("Perfil 7 FEL", porque[0])

    def test_va_detras_del_banner_de_arranque(self):
        """Primero «de qué fase hablamos» y luego «por qué»; al revés se lee
        como el cierre de la fase anterior."""
        s = sesion(**bin_analizado("real"))
        emitidas = self._correr_una_fase(s)
        banner = next(i for i, l in enumerate(emitidas) if "━━━" in l)
        porque = next(i for i, l in enumerate(emitidas) if "↩" in l)
        self.assertGreater(porque, banner)

    def test_sin_nada_que_contar_no_se_emite_una_linea_vacia(self):
        emitidas = self._correr_una_fase(sesion())
        self.assertEqual([l for l in emitidas if "↩" in l], [])


class TestElLogNoLlevaEstructurasDePython(unittest.TestCase):
    """El despachador volcaba el valor de RETORNO de la fase al log.

    Producía dos líneas sin hora y sin prefijo de fase —una con una ruta y
    otra con el `repr` de un diccionario— porque se metían directamente en el
    buffer, saltándose `_cmv40_log`. En un log que el usuario lee para
    entender qué ha pasado, eso es ruido con pinta de dato.
    """

    def test_el_valor_de_retorno_de_una_fase_no_acaba_en_el_log(self):
        import asyncio
        from routers import cmv40
        from models import CMv40Session

        s = CMv40Session(id="cmv40_x", source_mkv_path="/a.mkv",
                         source_mkv_name="a.mkv")

        async def _runner_que_devuelve_algo(session, log_cb, proc_cb):
            return {"profile": 7, "el_type": "FEL", "output_path": "/x.mkv"}

        import phases.cmv40_pipeline as pipeline
        orig = getattr(pipeline, "run_phase_h_validate")
        pipeline.run_phase_h_validate = _runner_que_devuelve_algo
        self.addCleanup(setattr, pipeline, "run_phase_h_validate", orig)
        cmv40._cmv40_log_buffer.pop(s.id, None)

        coro, _ = cmv40._cmv40_construir_fase(s, "validate", {})

        async def _nada(_m):
            return None
        asyncio.run(coro(_nada, lambda _p: None))

        buffer = cmv40._cmv40_log_buffer.get(s.id) or []
        self.assertEqual(
            [l for l in buffer if "profile" in l], [],
            "el diccionario de vuelta de la fase ha acabado en el log")


class TestNadieVuelveADerivarloAMano(unittest.TestCase):
    """El guard: la regla vive en el servidor y el JS la LEE.

    Es el mismo patrón que `TestNoQuedanReplicas` con `force_interactive`, y
    por el mismo motivo: una réplica de una regla del backend se desincroniza
    en silencio. La mañana del 2026-09-19 se arreglaron tres defectos de esa
    familia uno a uno; esto es lo que impide el cuarto.

    **Las excepciones van por FUNCIÓN y con su motivo.** Hay una: el guard del
    auto-pipeline, que es control de flujo y no relato — decide si volver a
    disparar una fase, y tiene que funcionar aunque el relato no se haya
    podido componer.
    """

    #: función → por qué puede leer los campos crudos
    EXENTAS = {
        "_cmv40MaybeAutoAdvance":
            "control de flujo, no relato: decide si re-disparar el pre-flight "
            "y debe seguir funcionando aunque el relato falle",
        "_cmv40GateBloque5":
            "el bloque ⑤ de la card 🛡️ Validaciones es uno de los volcados de "
            "diagnóstico que CLAUDE.md exime por función: enseña el valor "
            "CRUDO del campo, para leerlo contra el log, no para contar nada",
        "_cmv40EsperaDecision":
            "LEE el relato y sólo cae a los campos crudos cuando no llega —el "
            "summary del sidebar, una sesión cacheada de antes—. Es el mismo "
            "patrón de respaldo que `_cmv40Trust` con el plan, y por el mismo "
            "motivo: sin él, un panel sin relato ofrecería las fases de un "
            "proyecto que está esperando una decisión, que es el bug que esta "
            "función arregla",
    }
    CAMPOS = ("preflight_user_choice", "preflight_decision")

    def _funciones(self, src: str):
        """(nombre, cuerpo) de cada `function X(...) {...}` de primer nivel."""
        import re
        for m in re.finditer(r"^function (\w+)\(", src, re.M):
            nombre, i = m.group(1), m.start()
            prof, abierto = 0, False
            for j in range(i, len(src)):
                if src[j] == "{":
                    prof += 1; abierto = True
                elif src[j] == "}":
                    prof -= 1
                    if abierto and prof == 0:
                        yield nombre, src[i:j + 1]
                        break

    def test_las_superficies_leen_el_relato(self):
        # Por la FUNCIÓN, no por el nombre del fichero: si mañana el modal
        # se va a su propia pieza, el guard la sigue. Leer `tab3.js` por su
        # ruta pasaría en verde vigilando el vacío — lo prohíbe
        # `TestNadieLeeUnaPiezaSuelta`.
        from frontend_sources import pieza_de
        _, src = pieza_de("_cmv40PfChecks")
        malas = []
        for nombre, cuerpo in self._funciones(src):
            if nombre in self.EXENTAS:
                continue
            # Los comentarios explican el cambio y citan los campos; lo que se
            # persigue es el CÓDIGO que los lee.
            codigo = "\n".join(l for l in cuerpo.splitlines()
                                if not l.lstrip().startswith(("//", "*", "/*")))
            for campo in self.CAMPOS:
                if f"s.{campo}" in codigo or f"session.{campo}" in codigo:
                    malas.append(f"{nombre} lee s.{campo} a mano")
        self.assertEqual(sorted(malas), [], "\n  · ".join([""] + sorted(malas)))

    def test_cada_exencion_sigue_existiendo(self):
        """Una exención que ya no corresponde a código real parece cobertura
        y no cubre nada."""
        # Por la FUNCIÓN, no por el nombre del fichero: si mañana el modal
        # se va a su propia pieza, el guard la sigue. Leer `tab3.js` por su
        # ruta pasaría en verde vigilando el vacío — lo prohíbe
        # `TestNadieLeeUnaPiezaSuelta`.
        from frontend_sources import pieza_de
        _, src = pieza_de("_cmv40PfChecks")
        nombres = {n for n, _ in self._funciones(src)}
        self.assertEqual(set(self.EXENTAS) - nombres, set())

    def test_cada_exencion_lleva_su_motivo(self):
        for nombre, motivo in self.EXENTAS.items():
            self.assertGreater(len(motivo), 20, nombre)


if __name__ == "__main__":
    unittest.main()
