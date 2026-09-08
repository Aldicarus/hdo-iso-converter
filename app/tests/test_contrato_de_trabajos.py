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

CAMPOS = {"id", "tab", "tipo", "que", "fase", "fase_label", "fase_n",
          "fases_total", "pct", "pct_medido", "segundos", "eta_s",
          "eta_fuente", "cancelable"}


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
        self.assertEqual(p["fase_label"], "Extrayendo las pistas")
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
        self.assertEqual(p["pct"], 55)
        self.assertEqual(p["fase_label"], "Inyectando el RPU")
        self.assertEqual(p["eta_s"], 300)
        self.assertEqual(p["detalle"], "cmv40")

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

    def test_el_tope_de_recientes_se_acota(self):
        r = self.client.get("/api/trabajos?recientes=99999")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
