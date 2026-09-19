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
from models import CMv40Session, CMv40PhaseRecord, DoviInfo  # noqa: E402


def cancelada(fase: str) -> CMv40PhaseRecord:
    """Una entrada de historial como la que deja el cancel."""
    from datetime import datetime, timezone
    return CMv40PhaseRecord(phase=fase, status="cancelled",
                            started_at=datetime.now(timezone.utc))
from phases.cmv40_relato import ETAPAS, resolver  # noqa: E402


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
        tendría que pintar igual."""
        vistas = {resolver(s)["situacion"] for s in (
            sesion(), sesion(running_phase="extract"),
            sesion(error_message="x"), sesion(phase="done"),
            sesion(archived=True), sesion(preflight_decision="ask_tone_mapping"),
            sesion(phase_history=[cancelada("inject")]),
        )}
        vistas.add(resolver(sesion(), en_cola={"posicion": 1})["situacion"])
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


if __name__ == "__main__":
    unittest.main()
