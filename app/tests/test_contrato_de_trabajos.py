"""Una sola forma para «qué está pasando», sea el trabajo que sea.

Los cinco tipos de trabajo pesado medían su progreso de cinco maneras
distintas, y **el del rip no existía en el servidor**: la barra se parseaba del
log en el navegador, así que cerrar la pestaña la borraba. Una columna que
vigile el trabajo de toda la aplicación —la misma en las tres pestañas— habría
necesitado cinco renderizadores, y con el rip no habría funcionado.

`trabajos.py` define el diccionario común y un registro de adaptadores: cada
pestaña aporta el suyo. Lo que este fichero fija:

- **Los cinco tipos producen los mismos campos.** Es la razón de ser del
  módulo; si uno se queda corto, la columna se rompe justo con ese trabajo.
- **`pct_medido` distingue una barra real de un hueco.** Es la regla que el
  repo ya defiende para el progreso de CMv4.0: las constantes de tipo
  `elapsed / constante` envejecen y un job de 26 min llegó a anunciar 49.
- **Un adaptador que falla no puede tumbar la columna.** Quedarse sin el
  porcentaje es un inconveniente; quedarse sin saber que hay algo corriendo,
  no.
- **El ETA del rip es MEDIDO, no un modelo.** Medido sobre los 42 rips del
  NAS, `extract` es el 100 % del tiempo total, así que el porcentaje de
  mkvmerge sirve para el ETA global. Un modelo por tamaño saldría malo: el
  ritmo va de 38 a 253 MB/s, un error del 173 % en el peor caso.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_contrato_de_trabajos -v
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
import queue_manager as qm  # noqa: E402
import trabajos  # noqa: E402
import workload  # noqa: E402

CAMPOS = {"id", "sobre", "tab", "tipo", "que", "titulo", "poster",
          "fase", "fase_label", "paso", "chips", "fase_n",
          "fases_total", "pct", "pct_medido", "segundos", "eta_s",
          "eta_fuente",
          # El progreso de la FASE en curso, cuando el trabajo tiene dos
          # niveles de verdad. Los campos de arriba son SIEMPRE los del
          # trabajo entero; ver `test_dos_niveles_de_progreso`.
          "fase_progreso",
          "cancelable"}


def _t(tipo, clave="k", tab="rip", **kw):
    return qm.TrabajoEnCola(tab=tab, tipo=tipo, clave=clave,
                            que=f"{tipo} de X", datos=kw.pop("datos", {}))


class TestLaFormaEsUna(unittest.TestCase):

    def setUp(self):
        self._orig = dict(trabajos._adaptadores)
        self.addCleanup(lambda: (trabajos._adaptadores.clear(),
                                 trabajos._adaptadores.update(self._orig)))

    def test_sin_adaptador_igual_sale_la_forma_completa(self):
        """Una pestaña que no registre el suyo no puede dejar la columna en
        blanco: se dice qué es y de dónde viene."""
        trabajos._adaptadores.clear()
        p = trabajos.progreso_de(_t("tipo_raro", "k1"))
        self.assertEqual(set(p), CAMPOS)
        self.assertEqual(p["id"], "k1")
        self.assertIsNone(p["pct"])
        self.assertFalse(p["pct_medido"])

    def test_un_adaptador_que_revienta_no_tumba_la_columna(self):
        def _malo(trabajo):
            raise RuntimeError("boom")
        trabajos.registrar("x", _malo)
        p = trabajos.progreso_de(_t("x", "k1"))
        self.assertEqual(p["id"], "k1")
        self.assertIsNone(p["pct"])

    def test_un_adaptador_que_devuelve_None_tampoco(self):
        trabajos.registrar("x", lambda t: None)
        self.assertEqual(trabajos.progreso_de(_t("x"))["pct"], None)

    def test_el_adaptador_no_puede_inventarse_campos(self):
        """Si pudiera, la columna tendría que saber de cada tipo — que es
        exactamente lo que este módulo evita."""
        trabajos.registrar("x", lambda t: {"pct": 50, "campo_inventado": 1})
        p = trabajos.progreso_de(_t("x"))
        self.assertNotIn("campo_inventado", p)
        self.assertEqual(p["pct"], 50)

    def test_detalle_si_pasa_porque_dice_qué_modal_abrir(self):
        trabajos.registrar("x", lambda t: {"detalle": "rip"})
        self.assertEqual(trabajos.progreso_de(_t("x"))["detalle"], "rip")


class TestElEtaPorPorcentaje(unittest.TestCase):
    """Es una MEDIDA: sale del tiempo que costó el trozo ya hecho."""

    def test_la_mitad_en_un_minuto_es_un_minuto_mas(self):
        self.assertEqual(trabajos.eta_por_porcentaje(60, 50), 60)

    def test_un_cuarto_en_un_minuto_son_tres(self):
        self.assertEqual(trabajos.eta_por_porcentaje(60, 25), 180)

    def test_por_debajo_del_1_por_ciento_no_dice_nada(self):
        """Dividir por 0,5 da un número enorme que parece un dato."""
        self.assertIsNone(trabajos.eta_por_porcentaje(60, 0.5))
        self.assertIsNone(trabajos.eta_por_porcentaje(60, 0))
        self.assertIsNone(trabajos.eta_por_porcentaje(60, None))

    def test_al_100_lo_que_queda_no_es_tiempo(self):
        self.assertIsNone(trabajos.eta_por_porcentaje(60, 100))

    def test_sin_tiempo_transcurrido_tampoco(self):
        self.assertIsNone(trabajos.eta_por_porcentaje(0, 50))


class TestElNombreDeUnTrabajoSeEntiende(unittest.IsolatedAsyncioTestCase):
    """Lo que se lee en la columna no puede llevar claves del código.

    Decía «Fase analyze_source de X.mkv»: el identificador interno del
    pipeline, en pantalla y encima repetido, porque el nombre humano de la
    fase ya va al lado en `fase_label`.
    """

    # Las claves internas del pipeline. Ninguna puede acabar en un `que`.
    _CLAVES = ("analyze_source", "target_rpu_path", "target_rpu_drive",
               "target_rpu_mkv", "correct_sync", "preflight", "keep_l8_default",
               "restore_dropin", "trusted_p7_fel_final")

    def _revisar(self, que: str):
        for clave in self._CLAVES:
            self.assertNotIn(clave, que, f"«{que}» lleva la clave {clave}")
        self.assertTrue(que[:1].isupper(),
                        f"«{que}» debería empezar en mayúscula")

    async def test_una_fase_cmv40_encolada(self):
        import queue_manager as qm
        from models import CMv40Session
        from routers import cmv40
        vistos = []

        async def espia(trabajo, a_la_cabeza=False):
            vistos.append(trabajo)

        previa, cmv40.queue_manager.encolar = cmv40.queue_manager.encolar, espia
        self.addCleanup(setattr, cmv40.queue_manager, "encolar", previa)
        s = CMv40Session(id="p1", source_mkv_path="/x.mkv",
                         source_mkv_name="x.mkv",
                         output_mkv_name="El padrino (1972) [CMv4].mkv")
        await cmv40._cmv40_encolar_fase(s, "analyze_source")
        self.assertEqual(len(vistos), 1)
        self._revisar(vistos[0].que)
        self.assertIn("El padrino", vistos[0].que)

    def test_el_rip_por_su_atajo(self):
        import queue_manager as qm
        t = qm.TrabajoEnCola(tab="rip", tipo=qm.TIPO_RIP, clave="peli_1",
                             que="Conversión a MKV · Dune (2024).mkv")
        self._revisar(t.que)

    def test_ningun_que_del_codigo_lleva_una_clave_interna(self):
        """El barrido: cualquier `que=` de los routers, mirado tal cual."""
        import re
        from pathlib import Path as _P
        raiz = _P(__file__).resolve().parents[1]
        malos = []
        for rel in ("routers/tab1.py", "routers/tab2.py", "routers/cmv40.py",
                    "queue_manager.py"):
            for linea in (raiz / rel).read_text(encoding="utf-8").splitlines():
                m = re.search(r'que\s*=\s*\(?f?"([^"]*)"', linea)
                if not m:
                    continue
                texto = m.group(1)
                for clave in self._CLAVES:
                    if clave in texto:
                        malos.append(f"{rel}: {texto}")
        self.assertEqual(malos, [])


class TestLosCincoTiposProducenLoMismo(ApiTestCase):
    """Ejecutando los adaptadores reales de los tres routers."""

    def setUp(self):
        super().setUp()
        workload.limpiar()
        self.addCleanup(workload.limpiar)

    def _progreso(self, trabajo) -> dict:
        p = trabajos.progreso_de(trabajo)
        self.assertEqual(set(p) - {"detalle"}, CAMPOS,
                         f"el adaptador de {trabajo.tipo} no da la forma común")
        return p

    def test_rip(self):
        from routers import tab1
        tab1._rip_progress_reset("peli_1", "Peli (2024).mkv")
        tab1._rip_progress_fase("extract")
        tab1._rip_progress_pct(40)
        p = self._progreso(_t(qm.TIPO_RIP, "peli_1"))
        self.assertEqual(p["fase"], "extract")
        self.assertEqual(p["fase_label"], "Fase B — Extracción de pistas")
        self.assertEqual(p["fase_n"], 2)
        self.assertEqual(p["fases_total"], 4)
        self.assertEqual(p["pct"], 40)
        self.assertTrue(p["pct_medido"])
        self.assertEqual(p["eta_fuente"], "medido")
        self.assertEqual(p["detalle"], "rip")

    def test_el_rip_de_OTRA_sesion_no_se_confunde(self):
        """El singleton es uno; si no se comprueba la clave, la columna
        atribuiría el progreso del rip anterior al que acaba de arrancar."""
        from routers import tab1
        tab1._rip_progress_reset("peli_1", "Peli.mkv")
        tab1._rip_progress_pct(40)
        self.assertIsNone(tab1._rip_adaptador(_t(qm.TIPO_RIP, "otra_peli")))

    def test_el_porcentaje_del_rip_no_sobrevive_al_cambio_de_fase(self):
        """El % es de mkvmerge y vive dentro de la extracción. Arrastrarlo
        dejaría la barra clavada al 100 % durante el cierre del origen."""
        from routers import tab1
        tab1._rip_progress_reset("peli_1", "Peli.mkv")
        tab1._rip_progress_fase("extract")
        tab1._rip_progress_pct(100)
        tab1._rip_progress_fase("unmount")
        p = self._progreso(_t(qm.TIPO_RIP, "peli_1"))
        self.assertIsNone(p["pct"])
        self.assertFalse(p["pct_medido"])

    def test_serie(self):
        from routers import tab1
        import time
        tab1._series_create_progress.update({
            "running": True, "total": 4, "completed": ["a", "b"], "failed": [],
            "current_index": 3, "current_episode_step": "pgs",
            "current_episode_title": "Ep 3",
            "_desde": time.monotonic() - 120,
        })
        self.addCleanup(lambda: tab1._series_create_progress.update({"running": False}))
        p = self._progreso(_t(qm.TIPO_SERIE, "serie:X:1"))
        self.assertEqual(p["pct"], 50, "2 de 4 episodios terminados")
        self.assertTrue(p["pct_medido"], "son episodios TERMINADOS, no interpolación")
        self.assertEqual(p["eta_fuente"], "modelo")
        self.assertGreater(p["eta_s"], 0)
        self.assertIn("Ep 3", p["fase_label"])

    def test_la_serie_en_cola_no_pinta_barra(self):
        from routers import tab1
        tab1._series_create_progress.update({
            "running": True, "total": 4, "completed": [], "failed": [],
            "current_episode_step": "en_cola"})
        self.addCleanup(lambda: tab1._series_create_progress.update({"running": False}))
        p = self._progreso(_t(qm.TIPO_SERIE, "serie:X:1"))
        self.assertEqual(p["pct"], 0)
        self.assertFalse(p["pct_medido"])

    def test_analisis_extendido(self):
        from routers import tab2
        tab2._mkv_quality_state.update({
            "active": True, "audit_id": "aud1", "step": "ffmpeg",
            "step_label": "Extrayendo el RPU…", "global_pct": 30,
            "elapsed_s": 180})
        p = self._progreso(_t(qm.TIPO_ANALISIS_EXTENDIDO, "aud1", tab="mkv"))
        self.assertEqual(p["pct"], 30)
        self.assertTrue(p["pct_medido"])
        self.assertEqual(p["eta_s"], 420)
        self.assertEqual(p["eta_fuente"], "medido")

    def test_los_dos_extremos_del_pipe_son_LA_MISMA_fase(self):
        """`ffmpeg` y `extract_rpu` son los dos extremos del mismo pipe, y
        `en_cola` no es una fase sino la espera previa. Numerando la lista de
        pasos tal cual, la fase 1 salía como la 2: el modal enseñaba la
        extracción terminada y los combos en curso cuando iba por la
        extracción."""
        from routers import tab2
        for paso in ("ffmpeg", "extract_rpu"):
            with self.subTest(paso=paso):
                tab2._mkv_quality_state.update({
                    "active": True, "audit_id": "aud1", "step": paso,
                    "global_pct": 30, "elapsed_s": 60})
                p = self._progreso(
                    _t(qm.TIPO_ANALISIS_EXTENDIDO, "aud1", tab="mkv"))
                self.assertEqual(p["fase_n"], 1, "es la primera de dos")
                self.assertEqual(p["fases_total"], 2)
                self.assertEqual(p["fase_label"], "Fase A — Extracción del RPU")
        tab2._mkv_quality_state.update({"step": "combos"})
        p = self._progreso(_t(qm.TIPO_ANALISIS_EXTENDIDO, "aud1", tab="mkv"))
        self.assertEqual(p["fase_n"], 2)
        self.assertEqual(p["fase_label"],
                         "Fase B — Combos y perfil de luminancia")

    def test_esperando_turno_no_es_la_fase_uno(self):
        from routers import tab2
        tab2._mkv_quality_state.update({
            "active": True, "audit_id": "aud1", "step": "en_cola"})
        p = self._progreso(_t(qm.TIPO_ANALISIS_EXTENDIDO, "aud1", tab="mkv"))
        self.assertEqual(p["fase_n"], 0)

    def test_el_analisis_de_OTRO_audit_no_se_confunde(self):
        from routers import tab2
        tab2._mkv_quality_state.update({"active": True, "audit_id": "aud1"})
        self.assertIsNone(tab2._analisis_adaptador(
            _t(qm.TIPO_ANALISIS_EXTENDIDO, "aud2", tab="mkv")))

    def test_copia_de_biblioteca(self):
        from routers import tab2
        tab2._mkv_apply_state.update({
            "active": True, "step": "copying", "pct": 60,
            "eta_s": 90, "elapsed_s": 135})
        p = self._progreso(_t(qm.TIPO_COPIA_BIBLIOTECA, "apply:X", tab="mkv"))
        self.assertEqual(p["pct"], 60)
        self.assertTrue(p["pct_medido"])
        self.assertEqual(p["eta_s"], 90, "el de bytes/segundo, no extrapolado del %")
        self.assertEqual(p["eta_fuente"], "medido")

    def test_fase_cmv40(self):
        from routers import cmv40
        sid = self.crear_sesion(sid="cmv40_p", phase="extracted")
        import storage
        s = storage.load_cmv40_session(sid)
        s.running_phase = "inject"
        s.last_progress = {"pct": 55, "label": "Inyectando el RPU", "eta_s": 300}
        storage.save_cmv40_session(s)
        p = self._progreso(_t(qm.TIPO_FASE_CMV40, sid, tab="cmv40",
                              datos={"fase": "inject"}))
        # El pct es el del PROCESO, no el de la fase: un turno de cola es el
        # proyecto entero. La Fase F pesa 0,28 del job, así que al 55 % de la
        # fase el trabajo va por el 15 %. El 55 no se enseña en ninguna parte:
        # sería una medida de verdad, pero de otra cosa.
        self.assertEqual(p["pct"], 15)
        self.assertNotEqual(p["pct"], 55)
        # La fase y el PASO dentro de ella son dos cosas, y las dos se ven.
        # Colapsarlas dejaba de decir en qué fase del pipeline va el proyecto,
        # que es la mitad de la información.
        self.assertEqual(p["fase_label"], "Fase F — Inyectando el RPU en la EL")
        self.assertEqual(p["paso"], "Inyectando el RPU")
        self.assertEqual(p["detalle"], "cmv40")

    def _fase_n(self, sid, fase):
        import storage
        self.crear_sesion(sid=sid, phase="extracted")
        s = storage.load_cmv40_session(sid)
        s.running_phase = fase
        storage.save_cmv40_session(s)
        return self._progreso(_t(qm.TIPO_FASE_CMV40, sid, tab="cmv40",
                                 datos={"fase": fase}))

    def test_los_puntitos_son_OCHO_y_la_letra_cuadra(self):
        """El usuario cuenta ocho fases —A a H— y los puntitos eran siete: la
        D no estaba, así que la E se anunciaba como «4 de 7» y las tres
        siguientes también con la letra cambiada."""
        for fase, n in (("analyze_source", 1), ("target_rpu_path", 2),
                        ("extract", 3), ("correct_sync", 5),
                        ("inject", 6), ("remux", 7), ("validate", 8)):
            with self.subTest(fase=fase):
                p = self._fase_n(f"cmv40_n_{fase}", fase)
                self.assertEqual(p["fase_n"], n)
                self.assertEqual(p["fases_total"], 8)

    def test_la_D_ocupa_su_sitio_aunque_no_la_ejecute_nadie(self):
        """Es la revisión visual del sync: una parada, no un trabajo. Si no
        contara, la E ocuparía su hueco."""
        from routers.cmv40 import _CMV40_ESTACIONES
        self.assertEqual(_CMV40_ESTACIONES["sync_review"], 4)
        self.assertEqual(_CMV40_ESTACIONES["correct_sync"], 5)

    def test_las_tres_formas_de_dar_el_bin_son_LA_MISMA_fase(self):
        """El bin puede venir de la carpeta, del repo del Drive o de otro MKV.
        Con solo la primera en la lista, un proyecto que lo baja del repo —el
        camino por defecto— caía fuera y la tarjeta decía «– de 8» con los
        ocho puntos apagados."""
        for fase in ("target_rpu_path", "target_rpu_drive", "target_rpu_mkv"):
            with self.subTest(fase=fase):
                self.assertEqual(self._fase_n(f"cmv40_b_{fase}", fase)["fase_n"], 2)

    def test_y_el_de_la_FASE_viaja_aparte(self):
        """Los dos hacen falta: el modal enseña el del trabajo bajo la
        cartela y el de la fase pegado al log, que es lo que se está leyendo.
        Mandar solo uno obliga al que sobra a mentir — ha pasado en las dos
        direcciones (ver `test_dos_niveles_de_progreso`)."""
        import storage
        sid = self.crear_sesion(sid="cmv40_dos", phase="extracted")
        s = storage.load_cmv40_session(sid)
        s.running_phase = "inject"
        storage.save_cmv40_session(s)
        storage.write_cmv40_progress(sid, {"pct": 55, "eta_s": 300,
                                           "label": "Inyectando el RPU"})
        p = self._progreso(_t(qm.TIPO_FASE_CMV40, sid, tab="cmv40",
                              datos={"fase": "inject"}))
        self.assertEqual(p["pct"], 15, "arriba, el del trabajo")
        f = p["fase_progreso"]
        self.assertEqual(f["pct"], 55, "aparte, el de la fase")
        self.assertTrue(f["pct_medido"])
        self.assertEqual(f["eta_s"], 300)
        # Del ritmo real de la fase, no de un reparto por pesos: el
        # «(aprox.)» que la UI escribe al lado del restante es del total, y
        # este no lo lleva.
        self.assertEqual(f["eta_fuente"], "medido")

    def test_y_sin_medida_de_la_fase_tampoco_se_finge(self):
        import storage
        sid = self.crear_sesion(sid="cmv40_dos2", phase="extracted")
        s = storage.load_cmv40_session(sid)
        s.running_phase = "validate"
        s.last_progress = None
        storage.save_cmv40_session(s)
        f = self._progreso(_t(qm.TIPO_FASE_CMV40, sid, tab="cmv40"))["fase_progreso"]
        self.assertIsNone(f["pct"])
        self.assertFalse(f["pct_medido"])

    def test_los_otros_tipos_NO_lo_llevan(self):
        """Un trabajo de un solo nivel lo deja a None y la UI cae a los campos
        de arriba, que para él son la misma cosa. Rellenarlo con una copia
        sería inventar una distinción que no existe."""
        from routers import tab2
        tab2._mkv_quality_state.update(
            {"active": True, "audit_id": "aud1", "step": "ffmpeg"})
        p = self._progreso(_t(qm.TIPO_ANALISIS_EXTENDIDO, "aud1", tab="mkv"))
        self.assertIsNone(p["fase_progreso"])

    def test_el_progreso_sale_del_SIDECAR_no_del_json(self):
        """`last_progress` vive en `{id}.progress` desde que se sacó del JSON
        (eran 0,86 MB reescritos cada 20 s). El adaptador leía solo el modelo,
        que en una sesión escrita por OTRO proceso viene vacío: el pct salía
        `None` siempre y las siete fases se anunciaban «sin medir» con la
        medición perfectamente hecha al otro lado."""
        import storage
        sid = self.crear_sesion(sid="cmv40_side", phase="extracted")
        s = storage.load_cmv40_session(sid)
        s.running_phase = "extract"
        s.last_progress = None          # como llega de disco
        storage.save_cmv40_session(s)
        storage.write_cmv40_progress(sid, {"pct": 63, "eta_s": 120,
                                           "label": "Demuxing BL/EL"})
        p = self._progreso(_t(qm.TIPO_FASE_CMV40, sid, tab="cmv40",
                              datos={"fase": "extract"}))
        # El `paso` es lo que se lee del sidecar sin pasar por el cálculo del
        # total, así que es lo que prueba que el sidecar SE LEE. El pct de la
        # fase ya no se enseña tal cual: ver el comentario de `test_fase_cmv40`.
        self.assertEqual(p["paso"], "Demuxing BL/EL")

    def test_una_fase_cmv40_sin_progreso_no_finge(self):
        """`extract-rpu` escribe el RPU de golpe al cerrar: ese tramo NO es
        medible y el pipeline no manda pct. Aquí no se inventa."""
        from routers import cmv40
        import storage
        sid = self.crear_sesion(sid="cmv40_p", phase="extracted")
        s = storage.load_cmv40_session(sid)
        s.running_phase = "validate"
        s.last_progress = None
        storage.save_cmv40_session(s)
        p = self._progreso(_t(qm.TIPO_FASE_CMV40, sid, tab="cmv40"))
        self.assertIsNone(p["pct"])
        self.assertFalse(p["pct_medido"])
        self.assertIsNone(p["eta_fuente"])


class TestElEndpoint(ApiTestCase):

    def setUp(self):
        super().setUp()
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        cola = self.main.queue_manager
        self._cola = (list(cola._queue), cola._running)
        self.addCleanup(lambda: (cola._queue.__setitem__(slice(None), self._cola[0]),
                                 setattr(cola, "_running", self._cola[1])))

    def test_casa_libre(self):
        r = self.client.get("/api/trabajos").json()
        self.assertIsNone(r["activo"])
        self.assertEqual(r["cola"], [])
        self.assertEqual(r["interactivo"], [])

    def test_el_activo_sale_con_su_progreso(self):
        from routers import tab1
        cola = self.main.queue_manager
        cola._running = _t(qm.TIPO_RIP, "peli_1")
        tab1._rip_progress_reset("peli_1", "Peli (2024).mkv")
        tab1._rip_progress_fase("extract")
        tab1._rip_progress_pct(25)
        r = self.client.get("/api/trabajos").json()
        self.assertEqual(r["activo"]["pct"], 25)
        self.assertEqual(r["activo"]["tab"], "rip")

    def test_la_cola_sale_en_orden_y_con_posicion(self):
        cola = self.main.queue_manager
        cola._queue = [_t(qm.TIPO_RIP, "a"), _t(qm.TIPO_FASE_CMV40, "b", tab="cmv40")]
        r = self.client.get("/api/trabajos").json()
        self.assertEqual([(j["id"], j["posicion"]) for j in r["cola"]],
                         [("a", 1), ("b", 2)])

    def test_lo_interactivo_sale_aparte(self):
        """No tiene fases ni barra, pero explica por qué el NAS va cargado."""
        workload.registrar("abrir-1", workload.TAB_MKV, "apertura de un MKV",
                           workload.CLASE_INTERACTIVO)
        r = self.client.get("/api/trabajos").json()
        self.assertEqual([t["que"] for t in r["interactivo"]],
                         ["apertura de un MKV"])
        self.assertIsNone(r["activo"], "lo interactivo no es 'el activo'")

    def test_lo_diferido_no_se_cuela_en_interactivo(self):
        """`activo` sale de la cola; el registro de workload también lo tiene
        apuntado, y contarlo dos veces lo pintaría dos veces."""
        workload.registrar("rip-1", workload.TAB_RIP, "rip de Peli")
        r = self.client.get("/api/trabajos").json()
        self.assertEqual(r["interactivo"], [])

    def test_lo_sincrono_va_CONTADO_y_no_lleva_tarjeta(self):
        """Lo que dura lo que la petición no se pinta: el usuario lo tiene
        delante en su modal, y los de 0-3 s parpadearían contra el poll."""
        workload.registrar("abrir-1", workload.TAB_MKV,
                           "Apertura de un MKV · Supergirl (2026)",
                           workload.CLASE_INTERACTIVO, en_columna=False)
        r = self.client.get("/api/trabajos").json()
        self.assertEqual(r["interactivo"], [])
        self.assertEqual(r["consultas"]["n"], 1)
        self.assertEqual(r["consultas"]["nombres"],
                         ["Apertura de un MKV · Supergirl (2026)"])

    def test_lo_que_sobrevive_a_la_peticion_SI_lleva_tarjeta(self):
        """Los pre-flight: contestan al instante y siguen en una task, así que
        si cierras su modal la columna es el único sitio donde volver."""
        workload.registrar("pf-1", workload.TAB_CMV40,
                           "Validación previa · Sinners (2025)",
                           workload.CLASE_INTERACTIVO,
                           detalle="preflight", cancelable=True,
                           titulo="Sinners (2025)")
        r = self.client.get("/api/trabajos").json()
        self.assertEqual([t["titulo"] for t in r["interactivo"]],
                         ["Sinners (2025)"])
        self.assertEqual(r["consultas"]["n"], 0)

    def test_una_consulta_sigue_contando_para_la_contencion(self):
        """No se deja de registrar: sólo se deja de pintar.

        De `hay_contencion` salen `_adaptive_timeout` y el modelo de ETA, así
        que «quitarlo de la columna» nunca puede significar «no apuntarlo».
        """
        workload.registrar("abrir-2", workload.TAB_MKV, "Apertura de un MKV",
                           workload.CLASE_INTERACTIVO, en_columna=False)
        self.assertTrue(workload.hay_contencion())
        act = self.client.get("/api/activity").json()
        self.assertIn("Apertura de un MKV", [t["que"] for t in act["trabajos"]])

    def test_el_tope_de_recientes_se_acota(self):
        r = self.client.get("/api/trabajos?recientes=99999")
        self.assertEqual(r.status_code, 200)

    def test_dice_cuantas_veces_ha_cambiado_el_historial(self):
        """La columna pide el poll con `recientes=0` y carga el historial
        aparte; esto es lo que le dice cuándo tiene que hacerlo."""
        import historial
        r = self.client.get("/api/trabajos?recientes=0").json()
        self.assertEqual(r["recientes"], [])
        self.assertEqual(r["historial_rev"], historial.revision())


if __name__ == "__main__":
    unittest.main()


class TestCadaTipoAportaSusEtiquetas(ApiTestCase):
    """El `chips` lo llena cada adaptador con lo que distingue a SU tipo. La
    columna no sabe qué es un rip, así que no puede deducirlo."""

    def test_el_rip_dice_de_qué_origen_sale(self):
        """Un rip de un ISO, de una carpeta BDMV y de un m2ts suelto se ven
        igual y no hacen lo mismo: los dos últimos no montan nada."""
        from routers import tab1
        tab1._rip_progress_reset("s1", "Peli", "Carpeta BDMV")
        self.addCleanup(tab1._rip_progress_reset, "", "")
        p = trabajos.progreso_de(qm.TrabajoEnCola(
            tab="rip", tipo=qm.TIPO_RIP, clave="s1", que="x"))
        self.assertEqual(p["chips"], ["Carpeta BDMV"])

    def test_una_conversion_dice_su_ruta_y_si_encadena_sola(self):
        """Drop-in y merge no se parecen —uno sustituye el RPU entero y el
        otro lo transfiere frame a frame— y eso decide si el trabajo son
        quince minutos o cuarenta."""
        import storage
        from routers import cmv40
        sid = self.crear_sesion(sid="cmv40_chips", phase="extracted")
        s = storage.load_cmv40_session(sid)
        s.running_phase = "inject"
        s.auto_pipeline = True
        s.source_workflow = "p7_fel"
        s.target_type = "trusted_p7_fel_final"
        s.target_trust_ok = True
        storage.save_cmv40_session(s)
        p = trabajos.progreso_de(qm.TrabajoEnCola(
            tab="cmv40", tipo=qm.TIPO_FASE_CMV40, clave=sid, que="x",
            datos={"fase": "inject"}))
        self.assertEqual(p["chips"], ["Drop-in", "Auto"])

    def test_lo_encolado_dice_QUE_fase_espera(self):
        """No tiene adaptador —no está corriendo— así que sale de lo que se
        guardó al encolarlo."""
        self.assertEqual(
            trabajos.chips_de_lo_encolado({"datos": {"fase": "extract"}}),
            ["Fase C"])
        self.assertEqual(trabajos.chips_de_lo_encolado({}), [])
