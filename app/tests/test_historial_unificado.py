"""El historial transversal: una línea por trabajo, en las tres pestañas.

Cada pestaña guardaba lo suyo con su forma y en su sitio —un `ExecutionRecord`
dentro de la sesión de Tab 1, un `CMv40PhaseRecord` dentro de la de Tab 3— y
**Tab 2 no guardaba nada**: un análisis extendido de diez minutos no dejaba
rastro de haber existido en cuanto se cerraba el modal. Los dos primeros
además viven DENTRO de la sesión, así que responder «qué ha pasado hoy» exigía
abrir las 130 sesiones del `/config` y ordenarlas a mano.

Tres decisiones que este fichero fija:

1. **No puede tumbar un trabajo.** Perder una línea del historial es un
   inconveniente; que un rip de 40 minutos muera al terminar porque `/config`
   está lleno, no.
2. **Una línea a medias no se lleva por delante el resto.** El fichero se
   escribe con `append` y un `kill -9` a mitad la deja cortada.
3. **Se anota en el `finally`**, así que las tres salidas cuentan — y las dos
   que no son el camino feliz son justo las que uno mira después.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_historial_unificado -v
"""
import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
import historial  # noqa: E402
import paths  # noqa: E402


class HistorialCase(unittest.TestCase):
    """Con un /config aislado: si no, los tests escriben en el real."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hist_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = paths.CONFIG_DIR
        paths.CONFIG_DIR = self.tmp
        self.addCleanup(lambda: setattr(paths, "CONFIG_DIR", self._orig))

    def anotar(self, **kw):
        base = dict(id="s1", tab=historial.TAB_RIP, tipo=historial.TIPO_RIP,
                    que="rip de Peli (2024)",
                    inicio=datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc))
        base.update(kw)
        historial.anotar(**base)


class TestAnotarYLeer(HistorialCase):

    def test_una_linea_por_trabajo(self):
        self.anotar()
        self.anotar(id="s2")
        self.assertEqual(len(historial.leer()), 2)

    def test_el_mas_reciente_primero(self):
        self.anotar(id="viejo")
        self.anotar(id="nuevo")
        self.assertEqual([t["id"] for t in historial.leer()], ["nuevo", "viejo"])

    def test_calcula_la_duracion(self):
        ini = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
        self.anotar(inicio=ini, fin=ini + timedelta(minutes=6, seconds=12))
        self.assertEqual(historial.leer()[0]["segundos"], 372.0)

    def test_sin_fin_usa_ahora(self):
        self.anotar(fin=None)
        self.assertIsNotNone(historial.leer()[0]["fin"])

    def test_un_reloj_al_reves_no_da_negativo(self):
        ini = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
        self.anotar(inicio=ini, fin=ini - timedelta(minutes=5))
        self.assertEqual(historial.leer()[0]["segundos"], 0.0)

    def test_guarda_el_error(self):
        self.anotar(estado="error", error="dovi_tool se cayó")
        t = historial.leer()[0]
        self.assertEqual(t["estado"], "error")
        self.assertEqual(t["error"], "dovi_tool se cayó")

    def test_el_ref_log_dice_donde_mirar_no_copia_el_log(self):
        """El log de una fase CMv4.0 son ~2.000 líneas y ya vive en su
        fichero; duplicarlo aquí multiplicaría el historial por mil."""
        self.anotar(ref_log="cmv40:proj_1")
        t = historial.leer()[0]
        self.assertEqual(t["ref_log"], "cmv40:proj_1")
        self.assertNotIn("output_log", t)

    def test_el_limite_se_respeta(self):
        for i in range(10):
            self.anotar(id=f"s{i}")
        self.assertEqual(len(historial.leer(limite=3)), 3)

    def test_sin_fichero_devuelve_lista_vacia(self):
        self.assertEqual(historial.leer(), [])


class TestNoPuedeTumbarUnTrabajo(HistorialCase):

    def test_un_config_que_no_se_puede_escribir_no_lanza(self):
        paths.CONFIG_DIR = Path("/no/existe/y/no/se/puede/crear")
        self.anotar()      # si esto lanza, se lleva por delante el rip

    def test_un_dato_no_serializable_no_lanza(self):
        self.anotar(que=object())   # type: ignore[arg-type]
        self.assertEqual(historial.leer(), [])

    def test_y_lo_de_antes_sigue_ahí(self):
        self.anotar(id="bueno")
        self.anotar(que=object())   # type: ignore[arg-type]
        self.assertEqual([t["id"] for t in historial.leer()], ["bueno"])


class TestUnaLineaAMediasNoSeLlevaElResto(HistorialCase):

    def test_la_ultima_linea_cortada_se_salta(self):
        self.anotar(id="bueno1")
        self.anotar(id="bueno2")
        with historial.ruta().open("a", encoding="utf-8") as fh:
            fh.write('{"id": "cort')          # kill -9 a mitad de escritura
        self.assertEqual([t["id"] for t in historial.leer()],
                         ["bueno2", "bueno1"])

    def test_una_linea_en_blanco_tampoco_estorba(self):
        self.anotar(id="bueno")
        with historial.ruta().open("a", encoding="utf-8") as fh:
            fh.write("\n\n")
        self.assertEqual(len(historial.leer()), 1)

    def test_una_linea_que_no_es_un_objeto_se_ignora(self):
        with historial.ruta().open("w", encoding="utf-8") as fh:
            fh.write('"solo un string"\n[1, 2]\n')
        self.assertEqual(historial.leer(), [])


class TestRotacion(HistorialCase):
    """Un registro son ~236 bytes; el tope real (5 MB) son ~22.000."""

    def _tope(self, bytes_: int):
        orig = historial.TOPE_BYTES
        historial.TOPE_BYTES = bytes_
        self.addCleanup(lambda: setattr(historial, "TOPE_BYTES", orig))

    def test_al_pasar_el_tope_empieza_un_fichero_nuevo(self):
        self._tope(600)                       # rota tras el tercero
        for i in range(5):
            self.anotar(id=f"s{i}")
        self.assertTrue(historial.ruta().with_suffix(".jsonl.1").exists())

    def test_la_generacion_anterior_se_sigue_leyendo(self):
        """Si el lector mirara solo el fichero actual, justo después de rotar
        el historial parecería vacío."""
        self._tope(600)
        for i in range(5):
            self.anotar(id=f"s{i}")
        ids = [t["id"] for t in historial.leer()]
        self.assertEqual(sorted(ids), [f"s{i}" for i in range(5)],
                         "la generación rotada se perdió")

    def test_solo_se_guarda_una_generacion(self):
        """Es para mirar qué ha pasado, no un archivo histórico: con el tope
        real eso son año y medio de trabajos, y lo anterior se va."""
        self._tope(600)
        for i in range(40):
            self.anotar(id=f"s{i}")
        self.assertFalse(historial.ruta().with_suffix(".jsonl.1.1").exists())
        self.assertNotIn("s0", [t["id"] for t in historial.leer()])


class TestLaRevisionDiceQueCambio(HistorialCase):
    """El contador que la columna de trabajo mira para saber si recargar.

    Su señal era «cambió lo que está en marcha», y eso solo acierta con las
    líneas NUEVAS: cuando el usuario contestó «mantener el MKV», la línea del
    pre-flight pasó de «requiere decisión» a terminada sin que se moviera la
    cola —seguía corriendo un rip— y la tarjeta se quedó pidiendo una decisión
    ya tomada, con su botón de decidirla. Así que la señal tiene que ser el
    cambio en sí, no un síntoma suyo.
    """

    def test_una_linea_nueva_lo_mueve(self):
        antes = historial.revision()
        self.anotar()
        self.assertNotEqual(historial.revision(), antes)

    def test_resolver_una_espera_TAMBIEN(self):
        """El caso del pre-flight contestado, que es el que fallaba."""
        self.anotar(estado=historial.ESTADO_ESPERANDO)
        antes = historial.revision()
        self.assertTrue(historial.resolver_espera(
            "s1", nuevo_estado=historial.ESTADO_HECHO))
        self.assertNotEqual(historial.revision(), antes)

    def test_y_quitarla_de_la_lista(self):
        self.anotar()
        antes = historial.revision()
        self.assertTrue(historial.borrar("s1", historial.leer()[0]["inicio"]))
        self.assertNotEqual(historial.revision(), antes)

    def test_lo_que_no_cambia_nada_no_lo_mueve(self):
        """Si no, la columna recargaría el historial en cada vuelta."""
        self.anotar()
        antes = historial.revision()
        self.assertFalse(historial.resolver_espera("otro", nuevo_estado="done"))
        self.assertEqual(historial.revision(), antes)

    def test_un_fallo_al_escribir_tampoco(self):
        """No se anunció un cambio que no llegó a ocurrir."""
        paths.CONFIG_DIR = Path("/no/existe/y/no/se/puede/crear")
        antes = historial.revision()
        self.anotar()
        self.assertEqual(historial.revision(), antes)


class TestUnTrabajoCanceladoDiceQueLoParaste(HistorialCase):
    """Cancelar es un final abrupto y se cuenta como tal, sea del tipo que sea.

    De los cinco tipos, solo el análisis extendido daba el motivo —y a costa
    de mentir: se marcaba como `error`—. Los otros cuatro se quedaban con su
    icono y nada más, así que un rip que paraste y uno que se murió solo se
    distinguían abriendo el detalle. El estado sigue siendo `cancelled`, que
    no es lo mismo que fallar; lo que se unifica es que haya un porqué.
    """

    def test_sin_motivo_se_rellena_el_de_siempre(self):
        self.anotar(estado=historial.ESTADO_CANCELADO)
        self.assertEqual(historial.leer()[0]["error"],
                         historial.MOTIVO_CANCELADO)

    def test_pero_si_lo_trae_manda_el_suyo(self):
        self.anotar(estado=historial.ESTADO_CANCELADO,
                    error="Cancelado al quedarse sin espacio")
        self.assertIn("espacio", historial.leer()[0]["error"])

    def test_y_no_se_le_inventa_uno_a_lo_que_termina_bien(self):
        self.anotar(estado=historial.ESTADO_HECHO)
        self.assertIsNone(historial.leer()[0]["error"])

    def test_se_rellena_para_CUALQUIER_tipo(self):
        """El sitio es `anotar` y no cada llamada justamente para que un tipo
        nuevo no pueda olvidarse."""
        tipos = (historial.TIPO_RIP, historial.TIPO_FASE_CMV40,
                 historial.TIPO_PREFLIGHT, historial.TIPO_ANALISIS_EXTENDIDO,
                 historial.TIPO_COPIA_BIBLIOTECA)
        for i, tipo in enumerate(tipos):
            self.anotar(id=f"t{i}", tipo=tipo,
                        estado=historial.ESTADO_CANCELADO)
        self.assertEqual([t["error"] for t in historial.leer()],
                         [historial.MOTIVO_CANCELADO] * len(tipos))


class TestQuitarUnaEntrada(HistorialCase):
    """Un trabajo terminado se puede quitar de la lista.

    El fichero es append-only, así que quitar una línea obliga a reescribirlo.
    Se hace en el único sitio en que eso es aceptable: a petición del usuario,
    sobre un fichero con tope de 5 MB y con `.tmp` + `os.replace`.
    """

    def test_borra_la_linea_y_deja_el_resto(self):
        self.anotar(id="a", que="uno")
        self.anotar(id="b", que="dos")
        self.anotar(id="c", que="tres")
        ini = historial.leer()[1]["inicio"]
        self.assertTrue(historial.borrar("b", ini))
        self.assertEqual([t["id"] for t in historial.leer()], ["c", "a"])

    def test_la_clave_es_id_MAS_inicio(self):
        """Una sesión re-ejecutada deja varias líneas con el mismo id, y borrar
        «la de Dune» tendría que decidir cuál. `inicio` es lo único que las
        distingue."""
        self.anotar(id="s1", inicio=datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc))
        self.anotar(id="s1", inicio=datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc))
        historial.borrar("s1", "2026-09-07T10:00:00+00:00")
        quedan = historial.leer()
        self.assertEqual(len(quedan), 1)
        self.assertEqual(quedan[0]["inicio"], "2026-09-07T12:00:00+00:00")

    def test_lo_que_no_esta_devuelve_False(self):
        """Para que el endpoint dé 404 y la UI no diga «borrado» sobre una
        entrada que sigue ahí."""
        self.anotar(id="a")
        self.assertFalse(historial.borrar("a", "2020-01-01T00:00:00+00:00"))
        self.assertFalse(historial.borrar("fantasma", historial.leer()[0]["inicio"]))
        self.assertEqual(len(historial.leer()), 1)

    def test_una_linea_ilegible_se_conserva(self):
        """No es de nadie, y aprovechar un borrado para tirarla sería perder
        datos por la puerta de atrás."""
        self.anotar(id="a")
        self.anotar(id="b")
        f = historial.ruta()
        f.write_text(f.read_text(encoding="utf-8") + "{roto\n", encoding="utf-8")
        ini = next(t["inicio"] for t in historial.leer() if t["id"] == "a")
        historial.borrar("a", ini)
        self.assertIn("{roto", f.read_text(encoding="utf-8"))

    def test_tambien_busca_en_la_generacion_rotada(self):
        """`leer` mira las dos, así que borrar tiene que hacer lo mismo: si no,
        una entrada borrada justo después de rotar seguiría apareciendo."""
        self.anotar(id="viejo")
        f = historial.ruta()
        f.rename(f.with_suffix(f.suffix + ".1"))
        self.anotar(id="nuevo")
        ini = next(t["inicio"] for t in historial.leer() if t["id"] == "viejo")
        self.assertTrue(historial.borrar("viejo", ini))
        self.assertEqual([t["id"] for t in historial.leer()], ["nuevo"])

    def test_no_deja_el_tmp_por_ahi(self):
        self.anotar(id="a")
        historial.borrar("a", historial.leer()[0]["inicio"])
        self.assertEqual(list(historial.ruta().parent.glob("*.tmp")), [])


class TestUnaSolaLineaPorTrabajo(HistorialCase):
    """Un proyecto CMv4.0 es UN trabajo que atraviesa siete fases.

    Cada fase dejaba su propia línea, así que una película llenaba el
    historial con siete «Fase X terminada» y —peor— un proyecto parado
    esperando respuesta se leía igual que uno acabado. Ahora la línea es del
    proyecto y se reescribe según su estado.
    """

    def _pendiente(self, id="p1"):
        historial.registrar_estado(
            id=id, tab=historial.TAB_CMV40, tipo=historial.TIPO_FASE_CMV40,
            que="Upgrade CMv4.0 · El padrino (1972)",
            inicio=datetime.now(timezone.utc),
            estado=historial.ESTADO_ESPERANDO)

    def test_avanzar_de_estado_no_deja_dos_lineas(self):
        self._pendiente()
        historial.registrar_estado(
            id="p1", tab=historial.TAB_CMV40, tipo=historial.TIPO_FASE_CMV40,
            que="Upgrade CMv4.0 · El padrino (1972)",
            inicio=datetime.now(timezone.utc), estado=historial.ESTADO_HECHO)
        t = historial.leer()
        self.assertEqual(len(t), 1, t)
        self.assertEqual(t[0]["estado"], historial.ESTADO_HECHO)

    def test_una_ya_cerrada_NO_se_reescribe(self):
        """Si el usuario rehace una fase de un proyecto terminado, se abre
        otra línea: es lo mismo que hace Tab 1 con una sesión re-ejecutada."""
        historial.registrar_estado(
            id="p1", tab=historial.TAB_CMV40, tipo=historial.TIPO_FASE_CMV40,
            que="Upgrade CMv4.0 · X", inicio=datetime.now(timezone.utc),
            estado=historial.ESTADO_HECHO)
        historial.registrar_estado(
            id="p1", tab=historial.TAB_CMV40, tipo=historial.TIPO_FASE_CMV40,
            que="Upgrade CMv4.0 · X", inicio=datetime.now(timezone.utc),
            estado=historial.ESTADO_HECHO)
        self.assertEqual(len(historial.leer()), 2)

    def test_al_volver_a_ejecutarse_la_linea_se_retira(self):
        """Mientras corre, el sitio donde se ve es «En curso»."""
        self._pendiente()
        self.assertTrue(historial.quitar_sin_cerrar("p1"))
        self.assertEqual(historial.leer(), [])

    def test_pero_no_se_lleva_las_cerradas(self):
        self.anotar(id="p1", estado=historial.ESTADO_CANCELADO, que="antes")
        self._pendiente("p1")
        historial.quitar_sin_cerrar("p1")
        self.assertEqual([t["estado"] for t in historial.leer()],
                         [historial.ESTADO_CANCELADO])

    def test_ni_las_de_otros_proyectos(self):
        self._pendiente("p1")
        self._pendiente("p2")
        historial.quitar_sin_cerrar("p1")
        self.assertEqual([t["id"] for t in historial.leer()], ["p2"])

    def test_el_tiempo_puede_ser_el_de_PROCESO_y_no_el_de_reloj(self):
        """Un proyecto puede pasar tres días esperando una respuesta, y eso
        no es lo que ha costado convertirlo."""
        hace_rato = datetime.now(timezone.utc) - timedelta(days=3)
        historial.registrar_estado(
            id="p1", tab=historial.TAB_CMV40, tipo=historial.TIPO_FASE_CMV40,
            que="Upgrade CMv4.0 · X", inicio=hace_rato,
            estado=historial.ESTADO_HECHO, segundos=2100)
        self.assertEqual(historial.leer()[0]["segundos"], 2100)


class TestTab1LoAlimenta(HistorialCase):
    """Ejecutando `_append_execution_record`, no leyendo su fuente."""

    def _sesion(self, **kw):
        from models import Session
        base = dict(id="peli_2024_1", iso_path="/mnt/isos/peli.iso",
                    mkv_name="Peli (2024) [DV FEL].mkv", status="done")
        base.update(kw)
        return Session(**base)

    def _correr(self, session):
        from routers import tab1
        ini = datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)
        session.execution_started_at = ini
        tab1._append_execution_record(
            session,
            {"mount": ini, "extract": ini + timedelta(seconds=5)},
            {"mount": ini + timedelta(seconds=5),
             "extract": ini + timedelta(minutes=20)},
            # Lo mismo que hace el orquestador en su `finally`.
            cancelado=(session.status == "pending"))
        return historial.leer()

    def test_un_rip_terminado_deja_su_linea(self):
        t = self._correr(self._sesion())[0]
        self.assertEqual(t["tab"], historial.TAB_RIP)
        self.assertEqual(t["tipo"], historial.TIPO_RIP)
        self.assertIn("Peli (2024)", t["que"])
        self.assertEqual(t["estado"], "done")

    def test_un_rip_fallido_tambien_y_con_su_error(self):
        s = self._sesion(status="error", error_message="mkvmerge abortó")
        t = self._correr(s)[0]
        self.assertEqual(t["estado"], "error")
        self.assertEqual(t["error"], "mkvmerge abortó")

    def test_una_conversion_CANCELADA_deja_su_linea(self):
        """Se vio en el NAS: cancelar un ISO → MKV y no encontrarlo en
        «Recientes». El guard decía literalmente «no registrar cancelaciones»
        y es anterior al historial transversal, cuyo punto es justo el
        contrario: cuentan las tres salidas, y la cancelada y la que falla son
        las que uno mira después."""
        s = self._sesion(status="pending")   # cancelar devuelve a pending
        t = self._correr(s)[0]
        self.assertEqual(t["estado"], "cancelled",
                         "cancelar no es fallar: el estado lo distingue")
        self.assertIn("Peli (2024)", t["que"])
        # Y lleva el motivo, que es lo que la tarjeta enseña debajo. Sin él,
        # cuatro de los cinco tipos se quedaban con su icono y nada más.
        self.assertEqual(t["error"], historial.MOTIVO_CANCELADO)

    def test_pero_NO_entra_en_el_historial_del_proyecto(self):
        """Son dos cosas: el del proyecto lista sus ejecuciones, y una
        tentativa abortada no es una — el proyecto vuelve a `pending`, listo
        para relanzarse."""
        s = self._sesion(status="pending")
        self._correr(s)
        self.assertEqual(s.execution_history, [])

    def test_y_no_apunta_a_una_ejecucion_que_no_existe(self):
        s = self._sesion(status="pending")
        self.assertIsNone(self._correr(s)[0]["ref_log"])

    def test_una_terminada_si_entra_en_las_dos(self):
        s = self._sesion()
        t = self._correr(s)[0]
        self.assertEqual(len(s.execution_history), 1)
        self.assertEqual(t["estado"], "done")

    def test_el_ref_log_apunta_a_ESTA_ejecucion(self):
        """Una sesión re-ejecutada tiene varios logs; el de la sesión a secas
        no distingue cuál."""
        s = self._sesion()
        self._correr(s)
        self._correr(s)
        refs = [t["ref_log"] for t in historial.leer()]
        self.assertEqual(refs, ["sesion:peli_2024_1#2", "sesion:peli_2024_1#1"])


class TestTab3LoAlimenta(unittest.IsolatedAsyncioTestCase):
    """Ejecutando una fase de verdad, con sus tres salidas.

    La línea es del PROYECTO, no de la fase: tras la fase el proyecto se queda
    quieto esperando la siguiente acción, así que lo que se anota es su
    estado. El detalle por fase vive en `phase_history`."""

    def setUp(self):
        import storage
        from models import CMv40Session
        self.tmp = Path(tempfile.mkdtemp(prefix="hist_cmv40_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cmv40_dir = self.tmp / "cmv40"
        cmv40_dir.mkdir(parents=True)
        self._orig = (storage.CONFIG_DIR, storage.CMV40_DIR, paths.CONFIG_DIR)
        storage.CONFIG_DIR, storage.CMV40_DIR = self.tmp, cmv40_dir
        paths.CONFIG_DIR = self.tmp
        self.addCleanup(lambda: (
            setattr(storage, "CONFIG_DIR", self._orig[0]),
            setattr(storage, "CMV40_DIR", self._orig[1]),
            setattr(paths, "CONFIG_DIR", self._orig[2])))
        self.session = CMv40Session(
            id="cmv40_hist", source_mkv_path="/x.mkv", source_mkv_name="x.mkv",
            output_mkv_name="Predator.mkv", artifacts_dir=str(self.tmp),
            phase="extracted")
        storage.save_cmv40_session(self.session)

    async def _fase(self, cuerpo):
        from routers import cmv40 as r

        def _factory(log_cb, proc_cb):
            return cuerpo()

        await r._run_cmv40_phase(self.session, "inject", _factory, "injected")
        return historial.leer()

    async def test_una_fase_completada(self):
        async def _ok():
            return None
        t = (await self._fase(_ok))[0]
        self.assertEqual(t["tab"], historial.TAB_CMV40)
        self.assertEqual(t["tipo"], historial.TIPO_FASE_CMV40)
        # El PROYECTO, no la fase: sin esto una película dejaba siete líneas.
        self.assertIn("Upgrade CMv4.0", t["que"])
        self.assertNotIn("Fase F", t["que"])
        # La PELÍCULA, no el fichero: ni extensión ni los tags que la propia
        # app le añade al nombre de salida.
        self.assertIn("Predator", t["que"])
        self.assertNotIn(".mkv", t["que"])
        self.assertEqual(t["titulo"], "Predator")
        self.assertNotIn("inject", t["que"])
        # Y el estado es el del proyecto: la fase salió bien, pero el proyecto
        # se queda esperando la siguiente acción.
        self.assertEqual(t["estado"], historial.ESTADO_ESPERANDO)
        self.assertEqual(t["ref_log"], "cmv40:cmv40_hist")

    async def test_solo_UNA_linea_por_muchas_fases(self):
        """Dos fases seguidas del mismo proyecto no dejan dos líneas."""
        async def _ok():
            return None
        await self._fase(_ok)
        t = await self._fase(_ok)
        self.assertEqual(len(t), 1, t)

    async def test_una_fase_que_falla(self):
        async def _boom():
            raise RuntimeError("dovi_tool se cayó")
        t = (await self._fase(_boom))[0]
        self.assertEqual(t["estado"], "error")
        self.assertEqual(t["error"], "dovi_tool se cayó")

    async def test_una_fase_cancelada(self):
        """Va en el `finally` justamente para esto: las dos salidas que no son
        el camino feliz son las que uno mira después."""
        from phases.cmv40_pipeline import CMv40Cancelled

        async def _cancel():
            raise CMv40Cancelled()
        t = (await self._fase(_cancel))[0]
        self.assertEqual(t["estado"], "cancelled")

    async def test_la_duracion_es_la_de_la_fase(self):
        async def _lento():
            await asyncio.sleep(0.05)
        t = (await self._fase(_lento))[0]
        self.assertGreater(t["segundos"], 0)


class TestTab2LoAlimenta(ApiTestCase):
    """Lo NUEVO del bloque: esta pestaña no guardaba nada.

    Se ejecutan los endpoints de verdad, con el trabajo pesado reventando a
    propósito — que es la salida por la que pasa el `finally` sin necesitar
    ni ffmpeg ni decenas de GB.
    """

    def setUp(self):
        super().setUp()
        f = historial.ruta()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("", encoding="utf-8")
        self.mkv = self.output_dir / "Peli.mkv"
        self.mkv.write_bytes(b"x" * 4096)

    def test_el_analisis_extendido_deja_su_linea(self):
        """Lo escribe el RUNNER, no el endpoint: desde la cola única el POST
        solo encola y responde."""
        import asyncio
        import queue_manager as qm
        from phases import mkv_analyze
        from routers import tab2

        async def _revienta(*a, **k):
            raise RuntimeError("extract-rpu falló")
        orig = mkv_analyze.analyze_rpu_quality_for_mkv
        mkv_analyze.analyze_rpu_quality_for_mkv = _revienta
        self.addCleanup(lambda: setattr(
            mkv_analyze, "analyze_rpu_quality_for_mkv", orig))

        r = self.client.post("/api/mkv/quality-audit",
                             json={"file_path": str(self.mkv)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("queued"), r.text)
        self.assertEqual(historial.leer(), [],
                         "encolar no es trabajar: todavía no hay nada que anotar")

        # Y ahora el turno.
        encolados = [t for t in self.trabajos_encolados
                     if t[0] == qm.TIPO_ANALISIS_EXTENDIDO]
        self.assertEqual(len(encolados), 1, self.trabajos_encolados)
        tipo, clave, datos, _ = encolados[0]
        asyncio.run(tab2._runner_analisis_extendido(
            qm.TrabajoEnCola(tab="mkv", tipo=tipo, clave=clave, datos=datos)))

        t = historial.leer()
        self.assertEqual(len(t), 1, "un análisis extendido no dejó rastro")
        self.assertEqual(t[0]["tab"], historial.TAB_MKV)
        self.assertEqual(t[0]["tipo"], historial.TIPO_ANALISIS_EXTENDIDO)
        self.assertIn("Peli", t[0]["que"])
        self.assertNotIn(".mkv", t[0]["que"])
        self.assertEqual(t[0]["estado"], "error")

    def test_la_copia_desde_biblioteca_deja_su_linea(self):
        from routers import tab2
        src = self.library_dir / "DesdeBiblioteca.mkv"
        src.write_bytes(b"x" * 4096)

        async def _revienta(*a, **k):
            raise RuntimeError("disco lleno")
        orig = tab2._mkv_copy_to_output_with_progress
        tab2._mkv_copy_to_output_with_progress = _revienta
        self.addCleanup(lambda: setattr(
            tab2, "_mkv_copy_to_output_with_progress", orig))

        r = self.client.post("/api/mkv/apply",
                             json={"file_path": str(src), "copy_to_output": True,
                                   "audio_tracks": [], "subtitle_tracks": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("queued"), r.text)
        self.assertEqual(historial.leer(), [],
                         "encolar no es trabajar: todavía no hay nada que anotar")

        # Y ahora el turno.
        import asyncio
        import queue_manager as qm
        encolados = [t for t in self.trabajos_encolados
                     if t[0] == qm.TIPO_COPIA_BIBLIOTECA]
        self.assertEqual(len(encolados), 1, self.trabajos_encolados)
        tipo, clave, datos, _ = encolados[0]
        asyncio.run(tab2._runner_copia_biblioteca(
            qm.TrabajoEnCola(tab="mkv", tipo=tipo, clave=clave, datos=datos)))

        t = historial.leer()
        self.assertEqual(len(t), 1, "la copia no dejó rastro")
        self.assertEqual(t[0]["tipo"], historial.TIPO_COPIA_BIBLIOTECA)
        self.assertIn("DesdeBiblioteca", t[0]["que"])
        # Ni en minúscula ni con la ruta interna del destino, que es como
        # estaba: «copia de X.mkv a /mnt/output».
        self.assertNotIn(".mkv", t[0]["que"])
        self.assertNotIn("/mnt/", t[0]["que"])
        self.assertTrue(t[0]["que"][:1].isupper(), t[0]["que"])


class TestElEndpoint(ApiTestCase):

    def setUp(self):
        super().setUp()
        # `ApiTestCase` ya redirige `paths.CONFIG_DIR` al tmpdir.
        f = historial.ruta()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("", encoding="utf-8")

    def _anotar(self, **kw):
        base = dict(id="s1", tab=historial.TAB_RIP, tipo=historial.TIPO_RIP,
                    que="rip de Peli (2024)",
                    inicio=datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc))
        base.update(kw)
        historial.anotar(**base)

    def test_vacio(self):
        self.assertEqual(self.client.get("/api/historial").json(),
                         {"trabajos": []})

    def test_devuelve_lo_anotado_del_mas_reciente_al_mas_antiguo(self):
        self._anotar(id="viejo")
        self._anotar(id="nuevo")
        r = self.client.get("/api/historial").json()
        self.assertEqual([t["id"] for t in r["trabajos"]], ["nuevo", "viejo"])

    def test_las_tres_pestanas_caben_en_la_misma_forma(self):
        self._anotar(id="r1", tab=historial.TAB_RIP, tipo=historial.TIPO_RIP)
        self._anotar(id="m1", tab=historial.TAB_MKV,
                     tipo=historial.TIPO_ANALISIS_EXTENDIDO, que="análisis de X")
        self._anotar(id="c1", tab=historial.TAB_CMV40,
                     tipo=historial.TIPO_FASE_CMV40, que="Fase C de Y")
        trabajos = self.client.get("/api/historial").json()["trabajos"]
        self.assertEqual({t["tab"] for t in trabajos}, {"rip", "mkv", "cmv40"})
        for t in trabajos:
            self.assertEqual(
                set(t), {"id", "tab", "tipo", "que", "titulo", "poster",
                         "inicio", "fin",
                         "segundos", "estado", "error", "ref_log"})

    def test_el_limite_se_acota_por_arriba(self):
        """Sin tope, un `?limite=10000000` es una petición de leerlo todo."""
        self._anotar()
        r = self.client.get("/api/historial?limite=99999")
        self.assertEqual(r.status_code, 200)

    def test_los_tab_id_son_los_mismos_que_los_de_activity(self):
        """El dashboard no debería traducir entre dos vocabularios."""
        import workload
        self.assertEqual(
            {historial.TAB_RIP, historial.TAB_MKV, historial.TAB_CMV40},
            set(workload.TAB_IDS.values()))


if __name__ == "__main__":
    unittest.main()
