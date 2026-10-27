"""«Multiplexar MKV» por HTTP y con los binarios falsos: de crear a multiplexar.

Los dos orígenes reproducen el caso real con números redondos: A, un remux P7
FEL a 23,976 fps, y B, un WEB-DL a 24 fps con su pista DCP. El `ffmpeg` falso
suena cada uno en su línea de tiempo (`define_linea_de_tiempo`), así que la
medida de sincronía y la comprobación final se EJECUTAN, y un multiplexado con
el signo cambiado o sin ajustar la velocidad del vídeo se ve como se vería en
el NAS. Desde el rediseño (2026-10-27) el vídeo puede salir de cualquiera de
los dos, y el multiplexado de punta a punta se prueba en las dos direcciones.

Ejecutar desde la raíz del repo:
    python3 app/tests/correr_suite.py test_mux_endpoints
"""
import asyncio
import json
import sys
import unittest
from fractions import Fraction
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from api_harness import ApiTestCase  # noqa: E402
from cmv40_harness import FakeToolbox  # noqa: E402

FPS_BD = 24000 / 1001
FRAMES = 172_800
# B trae unos frames más (un fundido más largo al final), como el WEB-DL del
# caso real: así se distingue la duración del vídeo de A de la del de B.
FRAMES_B = FRAMES + 17
NOMBRE_A = "Peli.2024.UHD.BluRay.REMUX.DV.P7 [CMv4 FULL].mkv"
NOMBRE_B = "Peli (2024)[Rotulado Castellano][Audio DCP 7.1.4] [WEB-DL 2160p].mkv"

# Pistas, por (fuente, id).
A_VIDEO, A_TRUEHD, A_LATINO, A_SUB, A_CASTELLANO = (0, 0), (0, 1), (0, 2), (0, 3), (0, 4)
B_VIDEO, B_DCP, B_DDP, B_INGLES, B_SUB = (1, 0), (1, 1), (1, 2), (1, 3), (1, 4)


def pista(clave, **kw):
    return {"fuente": clave[0], "id": clave[1], **kw}


class MuxCase(ApiTestCase):

    def setUp(self):
        super().setUp()
        import queue_manager as qm
        import storage
        import workload
        from routers import mux
        self.mux, self.qm, self.storage = mux, qm, storage
        workload.limpiar()
        self.addCleanup(workload.limpiar)
        for estado in (mux._medidas, mux._cancelar, mux._procesos, mux._cerrojos):
            estado.clear()
        mux._progreso.update(id="", fase="", pct=None, paso="", chips=[])
        self.tb = FakeToolbox(self.tmp / "tb").install()
        self.addCleanup(self.tb.uninstall)
        # La medida automática al crear se apunta, no se ejecuta: con el
        # TestClient, una task del endpoint muere con su petición.
        self.medidas_pedidas = []
        original = mux._lanzar_medida
        mux._lanzar_medida = lambda pid: self.medidas_pedidas.append(pid) or True
        self.addCleanup(setattr, mux, "_lanzar_medida", original)
        self._origenes()

    def _origenes(self, *, beta_b=0.25, alfa_b=1000 / 1001, videos_a=None):
        tb = self.tb
        self.a = self.isos_dir / NOMBRE_A
        self.b = self.isos_dir / NOMBRE_B
        for f in (self.a, self.b):
            f.write_bytes(f.name.encode() * 64)
        video_a = videos_a or [
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "dimensions": "3840x2160",
             "fps": FPS_BD, "frames": FRAMES, "bytes": 5_000_000,
             "track_name": "HEVC DV P7 FEL CMv4.0", "default": True}]
        tb.define_mkv(self.a.name, duration_s=FRAMES / FPS_BD, tracks=video_a + [
            {"id": 1, "type": "audio", "codec": "TrueHD Atmos", "language": "eng",
             "language_ietf": "en", "channels": 8, "default": True, "bytes": 300_000},
            {"id": 2, "type": "audio", "codec": "AC-3", "language": "spa", "language_ietf": "es-419",
             "channels": 6, "bytes": 40_000},
            {"id": 3, "type": "subtitles", "codec": "SubRip/SRT", "language": "eng",
             "codec_id": "S_TEXT/UTF8", "bytes": 5_000},
            # El castellano del disco: el mismo doblaje que el de B. El latino
            # de arriba comparte idioma y es OTRO doblaje.
            {"id": 4, "type": "audio", "codec": "AC-3", "language": "spa", "language_ietf": "es",
             "channels": 6, "bytes": 40_000, "track_name": "Castellano DD 5.1"},
        ], attachments=[{"id": 1, "file_name": "cover.jpg", "content_type": "image/jpeg", "size": 900}])
        tb.define_mkv(self.b.name, duration_s=FRAMES_B / 24.0, tracks=[
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "dimensions": "3840x2076",
             "fps": 24.0, "frames": FRAMES_B, "bytes": 2_000_000, "default": True},
            {"id": 1, "type": "audio", "codec": "TrueHD Atmos", "language": "spa", "language_ietf": "es",
             "channels": 8, "default": True, "forced": True, "bytes": 330_000,
             "track_name": "Castellano TrueHD Atmos 7.1-DCP"},
            {"id": 2, "type": "audio", "codec": "E-AC-3", "language": "spa", "language_ietf": "es",
             "channels": 8, "bytes": 48_000, "track_name": "Castellano DD+ 7.1"},
            {"id": 3, "type": "audio", "codec": "E-AC-3", "language": "eng", "language_ietf": "en",
             "channels": 6, "bytes": 58_000, "track_name": "Inglés DD+ 5.1"},
            {"id": 4, "type": "subtitles", "codec": "SubRip/SRT", "language": "spa",
             "codec_id": "S_TEXT/UTF8", "bytes": 4_000, "track_name": "Castellano Completos"},
        ])
        tb.define_chapters(self.a.name, [("00:00:00.000", "Logo"), ("00:20:00.000", "Disco 2"),
                                         ("01:40:00.000", "Disco 3")])
        tb.define_chapters(self.b.name, [("00:00:00.000", "Inicio"), ("00:30:00.000", "Medio"),
                                         ("01:50:00.000", "Final")])
        tb.define_rpu(self.a.name, profile=7, el_type="FEL", cm_version="v4.0", frames=FRAMES)
        tb.define_sin_dv(self.b.name)       # el WEB-DL es HDR10: sin RPU
        tb.define_linea_de_tiempo(self.a.name, semilla=11)
        tb.define_linea_de_tiempo(self.b.name, alfa=alfa_b, beta_s=beta_b, semilla=11)

    # ── ayudas ──
    def crear(self, **body):
        r = self.client.post("/api/mux/create", json={"a": str(self.a), "b": str(self.b), **body})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def put(self, pid, esperado=200, **body):
        r = self.client.put(f"/api/mux/{pid}", json={"esquema": 2, **body})
        self.assertEqual(r.status_code, esperado, r.text)
        return r.json()

    def patron(self, pid, seccion, patron, esperado=200):
        r = self.client.post(f"/api/mux/{pid}/patron",
                             json={"esquema": 2, "seccion": seccion, "patron": patron})
        self.assertEqual(r.status_code, esperado, r.text)
        return r.json()

    def elegir(self, pid, *claves, capitulos=None, adjuntos=None):
        body = {"pistas": [pista(c) for c in claves]}
        if capitulos is not None:
            body["capitulos"] = [{"fuente": f, "edicion": 0, "indice": i} for f, i in capitulos]
        if adjuntos is not None:
            body["adjuntos"] = [{"fuente": f, "id": i} for f, i in adjuntos]
        return self.put(pid, **body)

    def con_medida(self, pid, **campos):
        from models import MuxSincronia
        p = self.storage.load_mux_project(pid)
        base = dict(estado="medida", veredicto="otro_ritmo", alfa_num=1000, alfa_den=1001,
                    beta_ms=250, n_validos=7, n_puntos=7, huellas=[f.huella for f in p.fuentes])
        base.update(campos)
        p.sincronia = MuxSincronia(**base)
        self.storage.save_mux_project(p)
        return p

    def multiplexar(self, pid, durante=None):
        r = self.client.post(f"/api/mux/{pid}/ejecutar")
        self.assertEqual(r.status_code, 200, r.text)

        async def _correr():
            tarea = asyncio.create_task(self.mux._multiplexar(pid))
            if durante:
                await durante()
            await asyncio.wait_for(tarea, 120)
        asyncio.run(_correr())
        return self.storage.load_mux_project(pid)

    def el_mux(self):
        [mux] = [c for c in self.tb.find("mkvmerge") if c.has("-o")]
        return mux

    @staticmethod
    def valores(call, opcion):
        """Todos los valores de una opción del argv, en orden."""
        return [call.argv[i + 1] for i, a in enumerate(call.argv[:-1]) if a == opcion]

    @staticmethod
    def tramo(call, nombre):
        """Las opciones de la entrada `nombre` (por el NOMBRE: la ruta que
        llega a mkvmerge es la resuelta, en el Mac /private/var)."""
        fin = next(i for i, a in enumerate(call.argv) if Path(a).name == nombre)
        previas = [i for i, a in enumerate(call.argv[:fin]) if Path(a).suffix == ".mkv"
                   and i > 0 and call.argv[i - 1] != "-o"]
        return call.argv[(previas[-1] + 1) if previas else 0:fin]


class TestCrear(MuxCase):

    def test_lee_los_dos_y_nace_sin_nada_elegido(self):
        p = self.crear()
        self.assertEqual(p["esquema"], 2)
        self.assertEqual((p["pistas"], p["capitulos"], p["adjuntos"]), ([], [], []))
        self.assertEqual(p["nombre_salida"], "Peli (2024).mkv")
        a, b = (f["video"] for f in p["fuentes"])
        self.assertEqual((a["dv_profile"], a["dv_el"], a["dv_cm"]), (7, "FEL", "v4.0"))
        self.assertEqual((a["cadencia_num"], a["cadencia_den"]), (24000, 1001))
        self.assertEqual((b["cadencia_num"], b["cadencia_den"]), (24, 1))
        self.assertEqual(b["dv_profile"], 0)
        self.assertEqual(p["fuentes"][0]["pistas"][2]["idioma_ietf"], "es-419")
        # Los capítulos de los dos, situados en su XML para poder copiarlos.
        self.assertEqual([(c["edicion"], c["indice"], c["nombre"]) for c in p["fuentes"][1]["capitulos"]],
                         [(0, 0, "Inicio"), (0, 1, "Medio"), (0, 2, "Final")])
        self.assertEqual(self.medidas_pedidas, [p["id"]], "la medida arranca sola")
        self.assertEqual([m["id"] for m in p["plan"]["motivos"]], ["sin_video", "sin_audio"])

    def test_el_dolby_vision_se_lee_de_los_dos(self):
        # Cualquiera puede acabar dando el vídeo: sin la muestra de B, elegir
        # su vídeo se saltaría la comprobación DV (revisión de Codex).
        self.tb.define_rpu(self.b.name, profile=8, el_type="", cm_version="v4.0", frames=FRAMES_B)
        p = self.crear()
        self.assertEqual(p["fuentes"][1]["video"]["dv_profile"], 8)

    def test_fuera_de_los_tres_sitios_no(self):
        fuera = self.tmp / "otro.mkv"
        fuera.write_bytes(b"x")
        for a in (str(fuera), str(self.isos_dir / ".." / "isos" / ".." / ".." / "otro.mkv")):
            with self.subTest(a=a):
                r = self.client.post("/api/mux/create", json={"a": a, "b": str(self.b)})
                self.assertEqual(r.status_code, 400)

    def test_el_mismo_fichero_dos_veces_no(self):
        r = self.client.post("/api/mux/create", json={"a": str(self.a), "b": str(self.a)})
        self.assertEqual(r.status_code, 400)

    def test_solo_mkv(self):
        iso = self.isos_dir / "Peli.iso"
        iso.write_bytes(b"x")
        r = self.client.post("/api/mux/create", json={"a": str(iso), "b": str(self.b)})
        self.assertEqual(r.status_code, 400)

    def test_basta_con_que_uno_tenga_video(self):
        self.tb.define_mkv(self.a.name, tracks=[
            {"id": 0, "type": "audio", "codec": "AC-3", "language": "eng", "channels": 2}])
        self.assertEqual(self.crear()["fuentes"][0]["video"]["codec"], "")
        self.tb.define_mkv(self.b.name, tracks=[
            {"id": 0, "type": "audio", "codec": "AC-3", "language": "spa", "channels": 2}])
        r = self.client.post("/api/mux/create", json={"a": str(self.a), "b": str(self.b)})
        self.assertEqual(r.status_code, 400)

    def test_el_listado_trae_el_relato_sin_lo_pesado(self):
        p = self.crear()
        [item] = self.client.get("/api/mux").json()["proyectos"]
        self.assertEqual(item["id"], p["id"])
        self.assertEqual(item["fuentes"][0]["pistas"], [])
        self.assertEqual(item["fuentes"][0]["n_pistas"], 5)
        # Sin nada elegido no hay una decisión pendiente: falta elegir.
        self.assertEqual(item["relato"]["situacion"], "preparando")
        self.assertIn("vídeo", item["relato"]["porque"])
        self.assertEqual(item["plan"]["video_de"], None)

    def test_si_no_se_pueden_leer_los_capitulos_no_se_crea(self):
        """Una lectura fallida no es «no tiene capítulos»: la sección diría
        que no hay y ni saldría el aviso de sección vacía (revisión de Codex
        del 2026-10-27)."""
        # Con el 2: el 1 de MKVToolNix son avisos, y ahí los capítulos se leen.
        self.tb.fail_when_arg("mkvextract", NOMBRE_B, rc=2)
        r = self.client.post("/api/mux/create", json={"a": str(self.a), "b": str(self.b)})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn(NOMBRE_B, r.json()["detail"])
        self.assertEqual(self.storage.list_mux_projects_summary(), [])

    def test_dos_creaciones_a_la_vez_no_comparten_id(self):
        """La misma película dos veces en el mismo segundo: el id se reserva
        con el proyecto guardado, o la segunda pisaba a la primera."""
        import time
        from phases import mux_pipeline
        from routers.mux import CrearMux
        # Por el NOMBRE: la ruta que llega es la resuelta (en el Mac, /private/var).
        fuentes = {f.name: asyncio.run(mux_pipeline.leer_fuente(str(f))) for f in (self.a, self.b)}

        async def leer(ruta, al_paso=None):
            return fuentes[Path(ruta).name].model_copy(deep=True)
        original_leer, original_id = mux_pipeline.leer_fuente, self.storage.make_mux_project_id

        def id_lento(nombre):
            time.sleep(0.3)                     # las dos lo piden antes de que nadie guarde
            return original_id(nombre)
        mux_pipeline.leer_fuente, self.storage.make_mux_project_id = leer, id_lento
        self.addCleanup(setattr, mux_pipeline, "leer_fuente", original_leer)
        self.addCleanup(setattr, self.storage, "make_mux_project_id", original_id)

        async def dos():
            cuerpo = CrearMux(a=str(self.a), b=str(self.b))
            return await asyncio.gather(self.mux.mux_crear(cuerpo), self.mux.mux_crear(cuerpo))
        r1, r2 = asyncio.run(dos())
        self.assertNotEqual(r1["id"], r2["id"])
        self.assertEqual(len(self.storage.list_mux_projects_summary()), 2)

    def test_un_proyecto_del_formato_viejo_no_se_carga_ni_se_lista(self):
        p = self.crear()
        viejo = json.loads((self.storage.MUX_DIR / f"{p['id']}.json").read_text())
        viejo.pop("esquema")
        viejo["id"] = "mux_Viejo_2024_1"
        viejo["capitulos_de"] = "resto"
        (self.storage.MUX_DIR / "mux_Viejo_2024_1.json").write_text(json.dumps(viejo))
        self.assertEqual([x["id"] for x in self.client.get("/api/mux").json()["proyectos"]], [p["id"]])
        self.assertEqual(self.client.get("/api/mux/mux_Viejo_2024_1").status_code, 404)
        self.assertIsNone(self.storage.load_mux_project("mux_Viejo_2024_1"))


class TestEditar(MuxCase):

    def test_sin_el_esquema_nuevo_no_se_guarda_nada(self):
        """Una pestaña del navegador con el JS de antes del rediseño no manda
        el vídeo en la lista de pistas: guardar lo que manda lo borraría."""
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, B_DCP)
        for body in ({"pistas": [pista(B_DCP)]}, {"esquema": 1, "pistas": [pista(B_DCP)]}):
            with self.subTest(body=body):
                r = self.client.put(f"/api/mux/{p['id']}", json=body)
                self.assertEqual(r.status_code, 409, r.text)
        guardado = self.storage.load_mux_project(p["id"])
        self.assertEqual([(x.fuente, x.id) for x in guardado.pistas], [A_VIDEO, B_DCP])

    def test_elegir_el_video_y_el_audio_le_pone_nombre(self):
        p = self.crear()
        d = self.elegir(p["id"], B_DCP, A_VIDEO)
        self.assertEqual([(x["fuente"], x["id"]) for x in d["pistas"]], [A_VIDEO, B_DCP])
        self.assertEqual(d["plan"]["video_de"], 0)
        self.assertEqual(d["nombre_salida"], "Peli (2024) [DV FEL] [Audio DCP] [CMv4 FULL].mkv")

    def test_el_nombre_sigue_a_lo_elegido_salvo_si_se_edito(self):
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, B_DCP)
        # El vídeo de B no lleva Dolby Vision ni [CMv4 …], y el audio de A no
        # es el DCP: el nombre se queda sin tags.
        d = self.elegir(p["id"], B_VIDEO, A_TRUEHD)
        self.assertEqual(d["nombre_salida"], "Peli (2024).mkv")
        self.put(p["id"], nombre_salida="Mi nombre")
        d = self.elegir(p["id"], A_VIDEO, B_DCP)
        self.assertEqual(d["nombre_salida"], "Mi nombre.mkv")

    def test_el_video_sale_de_un_solo_mkv(self):
        p = self.crear()
        r = self.client.put(f"/api/mux/{p['id']}", json={
            "esquema": 2, "pistas": [pista(A_VIDEO), pista(B_VIDEO), pista(B_DCP)]})
        self.assertEqual(r.status_code, 400, r.text)

    def test_el_video_de_un_mkv_va_entero(self):
        # Una BL y su EL en pistas aparte: una sin la otra no es el mismo vídeo.
        self._origenes(videos_a=[
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "fps": FPS_BD,
             "frames": FRAMES, "bytes": 5_000_000},
            {"id": 5, "type": "video", "codec": "HEVC/H.265/MPEG-H", "fps": FPS_BD,
             "frames": FRAMES, "bytes": 1_000_000}])
        p = self.crear()
        r = self.client.put(f"/api/mux/{p['id']}", json={
            "esquema": 2, "pistas": [pista(A_VIDEO), pista(B_DCP)]})
        self.assertEqual(r.status_code, 400, r.text)
        d = self.elegir(p["id"], A_VIDEO, (0, 5), B_DCP)
        self.assertEqual([(x["fuente"], x["id"]) for x in d["pistas"]], [A_VIDEO, (0, 5), B_DCP])

    def test_una_pista_que_no_existe_o_repetida_no(self):
        p = self.crear()
        for pistas in ([pista((1, 99))], [pista((2, 1))], [pista(B_DCP), pista(B_DCP)]):
            with self.subTest(pistas=pistas):
                r = self.client.put(f"/api/mux/{p['id']}", json={"esquema": 2, "pistas": pistas})
                self.assertEqual(r.status_code, 400, r.text)

    def test_una_sola_por_defecto_de_cada_tipo(self):
        p = self.crear()
        d = self.put(p["id"], pistas=[pista(A_VIDEO, por_defecto=True),
                                      pista(B_DCP, por_defecto=True),
                                      pista(A_TRUEHD, por_defecto=True)])
        self.assertEqual([x["por_defecto"] for x in d["pistas"]], [True, True, False])

    def test_los_capitulos_se_mezclan_y_se_validan(self):
        p = self.crear()
        d = self.elegir(p["id"], A_VIDEO, A_TRUEHD, capitulos=[(0, 1), (1, 2)])
        self.assertEqual([(c["fuente"], c["indice"]) for c in d["capitulos"]], [(0, 1), (1, 2)])
        self.assertTrue(d["plan"]["necesita_sincronia"], "un capítulo de B tiene tiempo")
        for caps in ([{"fuente": 1, "edicion": 0, "indice": 9}],
                     [{"fuente": 0, "indice": 1}, {"fuente": 0, "indice": 1}]):
            with self.subTest(caps=caps):
                r = self.client.put(f"/api/mux/{p['id']}", json={"esquema": 2, "capitulos": caps})
                self.assertEqual(r.status_code, 400, r.text)

    def test_un_adjunto_que_no_existe_o_repetido_no(self):
        """Se descartaba sin decir nada, y lo guardado no era lo enviado
        (revisión de Codex)."""
        p = self.crear()
        for adj in ([{"fuente": 1, "id": 1}], [{"fuente": 0, "id": 1}, {"fuente": 0, "id": 1}]):
            with self.subTest(adj=adj):
                r = self.client.put(f"/api/mux/{p['id']}", json={"esquema": 2, "adjuntos": adj})
                self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.put(p["id"], adjuntos=[{"fuente": 0, "id": 1}])["adjuntos"],
                         [{"fuente": 0, "id": 1}])

    def test_lo_que_cambia_el_mkv_devuelve_un_terminado_a_pendiente(self):
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, A_TRUEHD)
        cambios = {"titulo": {"titulo": "Otro título"},
                   "adjuntos": {"adjuntos": [{"fuente": 0, "id": 1}]},
                   "retraso": {"retraso_manual_ms": 120},
                   "capitulos": {"capitulos": [{"fuente": 0, "indice": 0}]},
                   "pistas": {"pistas": [pista(A_VIDEO), pista(A_CASTELLANO)]},
                   "nombre": {"nombre_salida": "Otro.mkv"}}
        for nombre, body in cambios.items():
            with self.subTest(campo=nombre):
                q = self.storage.load_mux_project(p["id"])
                q.status = "done"
                self.storage.save_mux_project(q)
                self.assertEqual(self.put(p["id"], **body)["status"], "pending")
        # Lo que no cambia el MKV, no.
        q = self.storage.load_mux_project(p["id"])
        q.status = "done"
        self.storage.save_mux_project(q)
        self.assertEqual(self.put(p["id"], acepta_riesgo=True)["status"], "done")

    def test_el_nombre_no_sale_de_output(self):
        p = self.crear()
        r = self.client.put(f"/api/mux/{p['id']}", json={"esquema": 2,
                                                         "nombre_salida": "../biblioteca/x.mkv"})
        self.assertEqual(r.status_code, 400)
        d = self.put(p["id"], nombre_salida="Otro nombre")
        self.assertEqual(d["nombre_salida"], "Otro nombre.mkv")
        self.assertTrue(d["nombre_manual"])

    def test_con_una_medida_en_marcha_no_se_cambia_la_pareja(self):
        """La nueva no arrancaría y la que termina guardaría el resultado de
        las pistas de antes junto a la pareja nueva (revisión de Codex del
        2026-10-27)."""
        p = self.crear()
        antes = self.storage.load_mux_project(p["id"]).pareja
        self.mux._medidas[p["id"]] = {"procs": [], "cancelar": False, "progreso": [0, 7]}
        self.put(p["id"], esperado=409, pareja=[4, 1])
        self.assertEqual(self.storage.load_mux_project(p["id"]).pareja, antes)
        self.mux._medidas.clear()
        self.put(p["id"], pareja=[4, 1])
        self.assertEqual(self.storage.load_mux_project(p["id"]).pareja, [4, 1])
        self.assertEqual(self.medidas_pedidas.count(p["id"]), 2, "al crear y al cambiarla")

    def test_cambiar_la_pareja_vuelve_a_medir(self):
        p = self.crear()
        self.medidas_pedidas.clear()
        self.put(p["id"], pareja=[2, 1])
        self.assertEqual(self.medidas_pedidas, [p["id"]])
        r = self.client.put(f"/api/mux/{p['id']}", json={"esquema": 2, "pareja": [3, 1]})
        self.assertEqual(r.status_code, 400, "la 3 de A es un subtítulo")


class TestPatrones(MuxCase):

    def test_todo_de_un_lado_en_una_seccion(self):
        p = self.crear()
        self.elegir(p["id"], A_VIDEO)
        d = self.patron(p["id"], "audio", "todo_b")
        self.assertEqual([(x["fuente"], x["id"]) for x in d["pistas"]],
                         [A_VIDEO, B_DCP, B_DDP, B_INGLES])
        self.assertEqual(d["nombre_salida"], "Peli (2024) [DV FEL] [Audio DCP] [CMv4 FULL].mkv",
                         "el nombre sigue a la selección")
        d = self.patron(p["id"], "video", "todo_b")
        self.assertEqual([(x["fuente"], x["id"]) for x in d["pistas"]][0], B_VIDEO)

    def test_se_aplica_sobre_lo_guardado(self):
        p = self.crear()
        self.put(p["id"], pistas=[pista(A_VIDEO), pista(B_DCP, nombre="Mi DCP")])
        d = self.patron(p["id"], "audio", "todo_b")
        self.assertEqual(d["pistas"][1]["nombre"], "Mi DCP", "lo que ya estaba conserva su nombre")

    def test_mis_idiomas_con_la_vo_de_tmdb(self):
        p = self.crear()
        q = self.storage.load_mux_project(p["id"])
        q.tmdb_info = {"title": "Peli", "year": 2024, "original_language": "en"}
        self.storage.save_mux_project(q)
        d = self.patron(p["id"], "audio", "mis_idiomas")
        # Castellano e inglés, de los dos; el latino es español también.
        self.assertEqual([(x["fuente"], x["id"]) for x in d["pistas"]],
                         [A_TRUEHD, A_LATINO, A_CASTELLANO, B_DCP, B_DDP, B_INGLES])

    def test_lo_que_no_es_un_patron(self):
        p = self.crear()
        self.patron(p["id"], "audio", "solo_forzados", esperado=400)
        self.patron(p["id"], "cosas", "todo_a", esperado=400)
        r = self.client.post(f"/api/mux/{p['id']}/patron", json={"seccion": "audio", "patron": "todo_a"})
        self.assertEqual(r.status_code, 409, "sin el esquema")

    def test_un_patron_que_no_cambia_nada_no_toca_un_terminado(self):
        """«Nada de B» en una sección sin nada de B, o «Todo de A» en el vídeo
        que ya es el de A: el MKV sería el mismo, y lo terminado sigue
        terminado. Si no, cada clic de más pediría repetir el multiplexado."""
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, A_TRUEHD)
        q = self.storage.load_mux_project(p["id"])
        q.status = "done"
        self.storage.save_mux_project(q)
        self.assertEqual(self.patron(p["id"], "subtitulos", "nada_b")["status"], "done")
        self.assertEqual(self.patron(p["id"], "video", "todo_a")["status"], "done")

    def test_un_patron_devuelve_a_pendiente_un_terminado(self):
        p = self.crear()
        q = self.storage.load_mux_project(p["id"])
        q.status = "done"
        self.storage.save_mux_project(q)
        self.assertEqual(self.patron(p["id"], "otros", "todo_a")["status"], "pending")


class TestEjecutar(MuxCase):

    def test_sin_video_o_sin_audio_no_se_encola(self):
        p = self.crear()
        self.assertEqual(self.client.post(f"/api/mux/{p['id']}/ejecutar").status_code, 409)
        self.elegir(p["id"], A_VIDEO)
        self.assertEqual(self.client.post(f"/api/mux/{p['id']}/ejecutar").status_code, 409)
        self.assertEqual(self.trabajos_encolados, [])

    def test_todo_de_un_mkv_se_encola_sin_medida_y_aunque_este_midiendo(self):
        """Sin nada con tiempo del otro MKV no hay nada que medir: la medida
        en marcha no hace esperar (revisión de Codex)."""
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, A_TRUEHD, adjuntos=[(0, 1)])
        self.mux._medidas[p["id"]] = {"procs": [], "cancelar": False, "progreso": [0, 7]}
        r = self.client.post(f"/api/mux/{p['id']}/ejecutar")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([t[:2] for t in self.trabajos_encolados], [(self.qm.TIPO_COMBINAR, p["id"])])

    def test_con_algo_del_otro_hace_falta_la_medida(self):
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, A_TRUEHD, B_SUB)
        self.assertEqual(self.client.post(f"/api/mux/{p['id']}/ejecutar").status_code, 409)
        self.con_medida(p["id"], veredicto="retraso", alfa_num=1, alfa_den=1)
        self.mux._medidas[p["id"]] = {"procs": [], "cancelar": False, "progreso": [0, 7]}
        r = self.client.post(f"/api/mux/{p['id']}/ejecutar")
        self.assertEqual(r.status_code, 409, "midiendo, y la medida hace falta")
        self.mux._medidas.clear()
        self.assertEqual(self.client.post(f"/api/mux/{p['id']}/ejecutar").status_code, 200)

    def test_en_cola_no_se_edita_ni_se_aplican_patrones(self):
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, A_TRUEHD)
        self.assertEqual(self.client.post(f"/api/mux/{p['id']}/ejecutar").json()["status"], "queued")
        self.assertEqual(self.client.post(f"/api/mux/{p['id']}/ejecutar").status_code, 409)
        self.put(p["id"], esperado=409, titulo="x")
        self.patron(p["id"], "audio", "todo_b", esperado=409)

    def test_un_mkv_que_ya_existe_no_se_pisa(self):
        p = self.crear()
        d = self.elegir(p["id"], A_VIDEO, A_TRUEHD)
        (self.output_dir / d["nombre_salida"]).write_bytes(b"el bueno")
        self.assertEqual(self.client.post(f"/api/mux/{p['id']}/ejecutar").status_code, 409)

    def test_dos_proyectos_no_escriben_el_mismo_mkv(self):
        p1, p2 = self.crear(), self.crear()
        for p in (p1, p2):
            self.elegir(p["id"], A_VIDEO, A_TRUEHD)
        self.assertEqual(self.client.post(f"/api/mux/{p1['id']}/ejecutar").status_code, 200)
        self.assertEqual(self.client.post(f"/api/mux/{p2['id']}/ejecutar").status_code, 409)

    def test_audio_de_dos_velocidades_no_se_encola_ni_aceptando(self):
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, A_TRUEHD, B_DCP)
        self.con_medida(p["id"])
        self.put(p["id"], acepta_riesgo=True)
        self.assertEqual(self.client.post(f"/api/mux/{p['id']}/ejecutar").status_code, 409)


class TestContraQueSeComparaCadaAudio(MuxCase):
    """La decisión de la comprobación final, con `mux_sync.comprobar` simulado:
    qué pista del MKV del vídeo se prueba para cada audio del otro, en qué
    orden y cuándo se para. Determinista, a diferencia de la correlación de
    verdad."""

    def setUp(self):
        super().setUp()
        from phases import mux_pipeline, mux_plan, mux_sync
        self.pipe, self.plan, self.sync = mux_pipeline, mux_plan, mux_sync
        self.llamadas = []
        self.respuestas = {}
        original = mux_sync.comprobar

        async def falso(ruta_ref, audio_ref, ruta_final, audio_final, **kw):
            self.llamadas.append((Path(ruta_ref).name, audio_ref, audio_final, kw.get("alfa")))
            return self.respuestas.get((audio_ref, audio_final), self._no_concluye())
        mux_sync.comprobar = falso
        self.addCleanup(setattr, mux_sync, "comprobar", original)

    @staticmethod
    def _concluye(res=3.0):
        puntos = [{"valido": True, "residuo_ms": res}, {"valido": True, "residuo_ms": res + 1}]
        return {"ok": abs(res) + 1 <= 40, "puntos": puntos, "residuo_max_ms": abs(res) + 1}

    @staticmethod
    def _no_concluye():
        return {"ok": None, "puntos": [{"valido": True, "residuo_ms": 2.0}], "residuo_max_ms": None}

    @staticmethod
    def _dispares():
        # Dos puntos válidos que no dicen lo mismo: uno es un pico casual.
        puntos = [{"valido": True, "residuo_ms": 3.0}, {"valido": True, "residuo_ms": 950.0}]
        return {"ok": False, "puntos": puntos, "residuo_max_ms": 950.0}

    def _comprobar(self, quitar_de_a=(), anadir_a=(), seleccion=(A_VIDEO, B_DCP, B_DDP, B_INGLES)):
        p = self.crear()
        self.elegir(p["id"], *seleccion)
        self.con_medida(p["id"])
        q = self.storage.load_mux_project(p["id"])
        if quitar_de_a:
            q.fuentes[0].pistas = [x for x in q.fuentes[0].pistas if x.id not in quitar_de_a]
        q.fuentes[0].pistas.extend(anadir_a)
        checks = asyncio.run(self.pipe._comprobar_sincronia_final(
            q, self.plan.plan(q), self.tmp / "final.mkv", None, None))
        return {c["pista"]: c for c in checks}

    def _de(self, audio_final):
        return [(ref, aud) for nombre, ref, aud, _ in self.llamadas if aud == audio_final]

    # Posiciones de audio. En A: 0 inglés (la de la medida), 1 latino, 2
    # castellano. En el MKV final: 0 y 1 los castellanos de B, 2 su inglés.

    def test_la_de_la_medida_es_una_comparacion_y_estricta(self):
        # Aunque A traiga otra pista en su idioma (aquí, un segundo inglés): la
        # medida se hizo contra una, y es contra esa contra la que se juzga.
        from models import MuxPistaOrigen
        otro_ingles = MuxPistaOrigen(id=5, tipo="audio", idioma="eng", idioma_ietf="en", canales=6)
        checks = self._comprobar(anadir_a=[otro_ingles])
        self.assertEqual(self._de(2), [(0, 2)])
        self.assertIsNone(checks[4]["ok"])
        self.assertNotIn("motivo", checks[4])

    def test_primero_la_de_referencia_y_si_concluye_no_se_sigue(self):
        self.respuestas = {(0, 0): self._concluye(), (0, 1): self._concluye()}
        checks = self._comprobar()
        self.assertEqual(self._de(0) + self._de(1), [(0, 0), (0, 1)])
        self.assertEqual((checks[2]["ok"], checks[2]["contra"], checks[2]["contra_fuente"]),
                         (True, 1, 0))

    def test_si_no_concluye_se_prueba_su_idioma_la_misma_etiqueta_primero(self):
        # El castellano de A (`es`) antes que el latino (`es-419`), y en
        # cuanto uno concluye se para.
        self.respuestas = {(2, 0): self._concluye()}
        checks = self._comprobar()
        self.assertEqual(self._de(0), [(0, 0), (2, 0)])
        self.assertEqual((checks[2]["ok"], checks[2]["contra"]), (True, 4))
        self.assertNotIn("motivo", checks[2])

    def test_puntos_que_no_coinciden_no_son_una_medida(self):
        # Un pico casual en una correlación floja no es «desfasada 950 ms».
        self.respuestas = {(0, 0): self._dispares(), (2, 0): self._dispares(), (1, 0): self._dispares()}
        c = self._comprobar()[2]
        self.assertIsNone(c["ok"], c)
        self.assertTrue(c["aviso"])
        self.assertIsNone(c["real"])
        self.assertEqual(self._de(0), [(0, 0), (2, 0), (1, 0)])

    def test_un_desfase_que_todos_los_puntos_ven_si_es_una_medida(self):
        self.respuestas = {(0, 0): self._concluye(600.0)}
        c = self._comprobar()[2]
        self.assertIsNone(c["ok"], "un aviso no cuenta como fallo")
        self.assertTrue(c["aviso"])
        self.assertEqual(c["real"], "601.0 ms")
        self.assertEqual(self._de(0), [(0, 0)])

    def test_sin_otra_pista_de_su_idioma_se_dice_por_que(self):
        c = self._comprobar(quitar_de_a=(2, 4))[2]
        self.assertEqual(c.get("motivo"), "sin_mismo_doblaje", c)
        self.assertEqual(c["contra"], 1)
        self.assertEqual(self._de(0), [(0, 0)])

    def test_con_su_idioma_en_a_y_sin_medida_no_se_inventa_el_motivo(self):
        c = self._comprobar()[2]
        self.assertIsNone(c["ok"])
        self.assertNotIn("motivo", c)

    def test_al_reves_el_audio_de_a_se_compara_con_el_de_b(self):
        """Con el vídeo de B, la referencia es la pista de B de la medida y lo
        que se comprueba es el audio de A; con la velocidad ajustada, lo
        esperado es 1/α (el contenido del segundo T de B está en T/α)."""
        self.respuestas = {(2, 0): self._concluye()}
        checks = self._comprobar(seleccion=(B_VIDEO, A_TRUEHD, A_CASTELLANO))
        # La pareja de la medida: el inglés de A (1) y el de B (3, el tercer
        # audio de B: posición 2). El inglés de A va el primero del final.
        self.assertEqual(sorted(checks), [2, 3])
        ref = checks[2]
        self.assertEqual((ref["id"], ref["ok"], ref["contra"], ref["contra_fuente"]),
                         ("sincronia", True, 3, 1))
        # La de la medida es UNA comparación, contra la pista de B de la
        # pareja: tomar la pareja como si el vídeo fuera de A llegaba al mismo
        # sitio probando otras, y pasaba.
        self.assertEqual(self._de(0), [(2, 0)])
        self.assertTrue(all(nombre == NOMBRE_B for nombre, *_ in self.llamadas), self.llamadas)
        self.assertTrue(all(alfa == Fraction(1001, 1000) for *_, alfa in self.llamadas), self.llamadas)


class TestMultiplexar(MuxCase):

    def test_el_caso_real_sale_con_la_velocidad_ajustada_y_firmado(self):
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, B_DCP, B_DDP, B_INGLES, B_SUB, capitulos=[(1, 0), (1, 1), (1, 2)])
        self.con_medida(p["id"])
        q = self.multiplexar(p["id"])
        self.assertEqual(q.status, "done", (q.error_message, q.validacion))
        final = self.output_dir / q.nombre_salida
        self.assertTrue(final.exists())
        self.assertFalse(Path(str(final) + ".tmp").exists())
        mux = self.el_mux()
        self.assertEqual(mux.opt("--default-duration"), "0:24p")
        self.assertEqual(mux.opt("--sync"), "1:-250")
        self.assertTrue(mux.opt("--chapters"), "los capítulos de B se mueven: se componen")
        checks = {c["id"]: c for c in q.validacion["checks"]}
        self.assertTrue(checks["sincronia"]["ok"], checks["sincronia"])
        self.assertTrue(checks["duracion_del_video"]["ok"])
        self.assertTrue(checks["dolby_vision"]["ok"], checks["dolby_vision"])
        self.assertTrue(checks["capitulos"]["ok"], checks["capitulos"])
        self.assertTrue(self.tb.ran("mkvpropedit"), "se firma")
        import historial
        [linea] = [h for h in historial.leer(10) if h["id"] == p["id"]]
        self.assertEqual((linea["tab"], linea["tipo"], linea["estado"]),
                         ("mux", "combinar_mkv", "done"))
        # La línea apunta a SU ejecución: un proyecto multiplexado dos veces
        # tiene dos logs, y el detalle de la vieja no puede enseñar el nuevo.
        self.assertEqual(linea["ref_log"], f"mux:{p['id']}#1")
        self.assertEqual(q.historial[-1].run_number, 1)

    def test_al_reves_el_video_de_b_con_el_audio_de_a(self):
        """El caso de Toy Story con los papeles cambiados (lo que se probará
        en el NAS): el vídeo del WEB-DL pasa a 23,976 y el audio del remux se
        mueve β/α. La comprobación final lo mide contra el inglés de B."""
        self.tb.define_rpu(self.b.name, profile=8, el_type="", cm_version="v4.0", frames=FRAMES_B)
        p = self.crear()
        self.elegir(p["id"], B_VIDEO, A_TRUEHD, A_CASTELLANO, A_SUB, capitulos=[(0, 0), (0, 1)])
        self.con_medida(p["id"])
        q = self.multiplexar(p["id"])
        self.assertEqual(q.status, "done", (q.error_message, q.validacion))
        mux = self.el_mux()
        self.assertEqual(mux.opt("--default-duration"), "0:24000/1001p")
        self.assertEqual(sorted(self.valores(mux, "--sync")), ["1:250", "3:250", "4:250"])
        checks = {c["id"]: c for c in q.validacion["checks"]}
        self.assertTrue(checks["sincronia"]["ok"], checks["sincronia"])
        self.assertEqual(checks["sincronia"]["contra_fuente"], 1)
        self.assertTrue(checks["duracion_del_video"]["ok"], checks["duracion_del_video"])
        # Los frames de B a la velocidad nueva, no los de A.
        self.assertAlmostEqual(checks["duracion_del_video"]["esperado"], FRAMES_B / FPS_BD, places=2)
        self.assertTrue(checks["dolby_vision"]["ok"], checks["dolby_vision"])
        self.assertTrue(checks["dolby_vision"]["esperado"].startswith("P8"))
        self.assertTrue(checks["capitulos"]["ok"], checks["capitulos"])
        # Las etiquetas globales, del MKV del vídeo.
        self.assertIn("--no-global-tags", self.tramo(mux, NOMBRE_A))
        self.assertNotIn("--no-global-tags", self.tramo(mux, NOMBRE_B))

    def test_todo_de_un_mkv_va_tal_cual(self):
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, A_TRUEHD, A_SUB, capitulos=[(0, 0), (0, 1), (0, 2)],
                    adjuntos=[(0, 1)])
        q = self.multiplexar(p["id"])
        self.assertEqual(q.status, "done", (q.error_message, q.validacion))
        mux = self.el_mux()
        self.assertFalse([a for a in mux.argv if Path(a).name == NOMBRE_B], "B no aporta nada")
        self.assertIsNone(mux.opt("--sync"))
        self.assertIsNone(mux.opt("--default-duration"))
        self.assertIsNone(mux.opt("--chapters"), "los de A, enteros y sin mover, los copia mkvmerge")
        checks = {c["id"]: c for c in q.validacion["checks"]}
        self.assertNotIn("sincronia", checks)
        self.assertTrue(checks["adjuntos"]["ok"])

    def test_sin_ajustar_la_velocidad_la_fase_c_lo_caza(self):
        # Un mkvmerge que no aplicara la cadencia: frames y nombre bien, pero
        # el vídeo dura lo que a 23,976 y el audio va a 24. Es el MKV que el
        # usuario hizo a mano con la GUI.
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, B_DCP)
        self.con_medida(p["id"])
        self.tb._scenario["default_duration_ignorada"] = True
        self.tb._flush()
        q = self.multiplexar(p["id"])
        self.assertEqual(q.status, "error")
        fallos = {c["id"] for c in q.validacion["checks"] if c.get("ok") is False}
        self.assertIn("duracion_del_video", fallos)
        self.assertTrue(Path(str(self.output_dir / q.nombre_salida) + ".tmp").exists(),
                        "un MKV que no cuadra se queda para revisarlo")

    def test_un_desfase_con_el_signo_cambiado_no_pasa(self):
        # La medida dice −250 donde B va +250: el plan lo aplica al revés y el
        # audio queda medio segundo fuera. La comprobación final lo mide, en
        # las dos direcciones.
        for sel in ((A_VIDEO, B_DCP), (B_VIDEO, A_TRUEHD)):
            with self.subTest(video=sel[0][0]):
                p = self.crear()
                self.elegir(p["id"], *sel)
                self.con_medida(p["id"], beta_ms=-250)
                q = self.multiplexar(p["id"])
                self.assertEqual(q.status, "error")
                sinc = [c for c in q.validacion["checks"] if c["id"] == "sincronia"][0]
                self.assertFalse(sinc["ok"])
                self.assertGreater(float(sinc["real"].split()[0]), 400)
                Path(str(self.output_dir / q.nombre_salida) + ".tmp").unlink()
                self.client.delete(f"/api/mux/{p['id']}")

    def test_con_el_riesgo_aceptado_la_sincronia_es_un_aviso(self):
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, B_DCP)
        self.con_medida(p["id"], veredicto="montaje_distinto", beta_ms=-250,
                        salto_entre=[1000, 2000])
        self.put(p["id"], acepta_riesgo=True)
        q = self.multiplexar(p["id"])
        self.assertEqual(q.status, "done", q.error_message)
        sinc = [c for c in q.validacion["checks"] if c["id"] == "sincronia"][0]
        self.assertTrue(sinc["aviso"])
        self.assertIsNone(sinc["ok"])

    def test_una_pista_desfasada_dentro_de_su_release_se_avisa(self):
        # El castellano de B va 600 ms tarde DENTRO de su propio fichero: la
        # referencia (inglés) cuadra y el multiplexado es el que pedía el
        # plan, pero eso hay que decirlo.
        self.tb.define_linea_de_tiempo(self.b.name, alfa=1000 / 1001, beta_s=0.25,
                                       semilla=11, pistas={0: {"alfa": 1000 / 1001, "beta_s": 0.85}})
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, B_DCP, B_DDP, B_INGLES)
        self.con_medida(p["id"])
        q = self.multiplexar(p["id"])
        self.assertEqual(q.status, "done", q.error_message)
        avisos = [c for c in q.validacion["checks"] if c["id"] == "sincronia_pista" and c.get("aviso")]
        self.assertEqual([c["pista"] for c in avisos], [2], q.validacion["checks"])
        self.assertGreater(float(avisos[0]["real"].split()[0]), 400, avisos[0])

    def test_cada_doblaje_se_compara_con_la_de_referencia_y_concluye(self):
        # Los castellanos de B contra la de REFERENCIA de A (inglés), con los
        # cuatro puntos: correlan solo por la música y los efectos, y con
        # cuatro puntos concluyen, como en el NAS.
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, B_DCP, B_DDP, B_INGLES)
        self.con_medida(p["id"])
        q = self.multiplexar(p["id"])
        self.assertEqual(q.status, "done", q.error_message)
        pistas = {c["pista"]: c for c in q.validacion["checks"] if c["id"] == "sincronia_pista"}
        self.assertEqual(sorted(pistas), [2, 3])
        for c in pistas.values():
            self.assertTrue(c["ok"], c)
            self.assertFalse(c["aviso"], c)
            self.assertEqual((c["contra"], c["contra_fuente"]), (1, 0), c)
            self.assertLessEqual(float(c["real"].split()[0]), 40)
        del_video = lambda mapa: [x for x in self.tb.find("ffmpeg")
                                  if Path(x.opt("-i") or "").name == self.a.name
                                  and x.opt("-map") == mapa]
        self.assertTrue(del_video("0:a:0"))
        self.assertEqual(del_video("0:a:1") + del_video("0:a:2"), [],
                         "se probaron otras pistas de A sin hacer falta")

    def test_el_ffmpeg_falso_distingue_doblajes(self):
        # Lo que hace falta para que lo de arriba signifique algo: dos
        # doblajes correlan poco —solo la música y los efectos—, como en el
        # NAS (r ≈ 0,1-0,2), y el mismo doblaje mucho (r ≈ 0,7-0,9).
        from phases import mux_sync
        dur = FRAMES / FPS_BD

        def r_medio(audio_a, audio_b):
            res = asyncio.run(mux_sync.comprobar(str(self.a), audio_a, str(self.b), audio_b,
                                                 duracion_s=dur, alfa=Fraction(1000, 1001)))
            return sum(q["r"] for q in res["puntos"]) / len(res["puntos"])

        self.assertGreater(r_medio(0, 2), 0.5, "inglés contra inglés")
        self.assertLess(r_medio(0, 0), 0.25, "inglés contra castellano")
        self.assertLess(r_medio(1, 0), 0.25, "latino contra castellano: otro doblaje")
        self.assertGreater(r_medio(2, 0), 0.5, "castellano contra castellano")

    def test_un_origen_que_cambio_no_se_multiplexa(self):
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, B_DCP)
        self.con_medida(p["id"])
        self.b.write_bytes(b"otro contenido" * 100)
        q = self.multiplexar(p["id"])
        self.assertEqual(q.status, "error")
        self.assertIn(NOMBRE_B, q.error_message)
        self.assertFalse(any(c.has("-o") for c in self.tb.find("mkvmerge")), "ni se lanza el mux")

    def test_cancelar_en_la_fase_c_no_publica(self):
        """Todo de B, que no tiene Dolby Vision: la Fase C no pasa por la
        muestra ni por la sincronía, que eran los únicos sitios donde miraba
        si la habían cancelado (revisión de Codex del 2026-10-27)."""
        p = self.crear()
        d = self.elegir(p["id"], B_VIDEO, B_DCP)
        self.tb.retrasar_cuando_arg("mkvextract", ".mkv.tmp", 2)
        final = self.output_dir / d["nombre_salida"]

        async def _cancelar_en_la_fase_c():
            for _ in range(200):
                if any(".mkv.tmp" in a for c in self.tb.find("mkvextract") for a in c.argv):
                    break
                await asyncio.sleep(0.05)
            r = await self.mux.mux_cancelar(p["id"])
            self.assertTrue(r["ok"])
        q = self.multiplexar(p["id"], durante=_cancelar_en_la_fase_c)
        self.assertEqual(q.status, "pending")
        self.assertIsNotNone(q.last_cancelled_at)
        self.assertFalse(final.exists(), "se publicó un multiplexado cancelado")
        self.assertEqual(q.historial[-1].status, "cancelled")

    def test_cancelar_a_mitad_del_mux_no_deja_nada(self):
        p = self.crear()
        d = self.elegir(p["id"], A_VIDEO, B_DCP)
        self.con_medida(p["id"])
        self.tb.retrasar_cuando_arg("mkvmerge", "--gui-mode", 8, parcial=True)
        tmp = Path(str(self.output_dir / d["nombre_salida"]) + ".tmp")

        async def _cancelar_en_el_mux():
            for _ in range(200):
                if self.mux._procesos.get(p["id"]) and tmp.exists():
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(tmp.exists(), "el parcial existe antes de cancelar")
            r = await self.mux.mux_cancelar(p["id"])
            self.assertTrue(r["ok"])
        q = self.multiplexar(p["id"], durante=_cancelar_en_el_mux)
        self.assertEqual(q.status, "pending")
        self.assertIsNotNone(q.last_cancelled_at)
        self.assertFalse(tmp.exists())
        self.assertEqual(q.historial[-1].status, "cancelled")


class TestMedida(MuxCase):

    def test_mide_la_velocidad_y_el_desfase_de_verdad(self):
        p = self.crear()
        estado = {"procs": [], "cancelar": False, "progreso": [0, 7]}
        asyncio.run(self.mux._medir(p["id"], estado))
        s = self.storage.load_mux_project(p["id"]).sincronia
        self.assertEqual(s.estado, "medida", s.error)
        self.assertEqual(s.veredicto, "otro_ritmo", [x.model_dump() for x in s.puntos])
        self.assertEqual(Fraction(s.alfa_num, s.alfa_den), Fraction(1000, 1001))
        self.assertAlmostEqual(s.beta_ms, 250, delta=10)
        self.assertEqual(s.n_validos, 7)
        plan = self.elegir(p["id"], A_VIDEO, B_DCP)["plan"]
        self.assertTrue(plan["puede_multiplexar"], plan["motivos"])
        self.assertEqual(plan["velocidad_video"]["a"], [24, 1])
        plan = self.elegir(p["id"], B_VIDEO, A_TRUEHD)["plan"]
        self.assertEqual(plan["velocidad_video"]["a"], [24000, 1001])

    def test_una_medida_que_dice_otra_cosa_deja_de_ser_terminado(self):
        """El MKV terminado salió con la medida de antes; la misma medida otra
        vez no cambia nada (revisión de Codex del 2026-10-27)."""
        from models import MuxSincronia
        from phases import mux_pipeline
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, B_DCP)
        q = self.con_medida(p["id"])
        q.status = "done"
        self.storage.save_mux_project(q)
        misma = q.sincronia.model_dump()
        resultados = [MuxSincronia(**misma), MuxSincronia(**{**misma, "beta_ms": 900})]

        async def falsa(proyecto, **kw):
            return resultados.pop(0)
        original = mux_pipeline.medir_sincronia
        mux_pipeline.medir_sincronia = falsa
        self.addCleanup(setattr, mux_pipeline, "medir_sincronia", original)
        estado = lambda: {"procs": [], "cancelar": False, "progreso": [0, 7]}  # noqa: E731
        asyncio.run(self.mux._medir(p["id"], estado()))
        self.assertEqual(self.storage.load_mux_project(p["id"]).status, "done", "la misma medida")
        asyncio.run(self.mux._medir(p["id"], estado()))
        self.assertEqual(self.storage.load_mux_project(p["id"]).status, "pending")

    def test_cancelar_la_medida(self):
        p = self.crear()
        estado = {"procs": [], "cancelar": True, "progreso": [0, 7]}
        asyncio.run(self.mux._medir(p["id"], estado))
        self.assertEqual(self.storage.load_mux_project(p["id"]).sincronia.estado, "cancelada")


class TestLaFicha(MuxCase):

    def setUp(self):
        super().setUp()
        import services.tmdb as tmdb

        class _Hallada:
            tmdb_id = 99

        async def buscar(titulo, anio, limit=1):
            return [_Hallada()]

        async def detalles(tmdb_id):
            return tmdb.TmdbDetails(tmdb_id=tmdb_id, title="La película", year=2024,
                                    original_language="en")
        originales = (tmdb.is_configured, tmdb.search_movies, tmdb.fetch_details)
        tmdb.is_configured, tmdb.search_movies, tmdb.fetch_details = (lambda: True), buscar, detalles
        self.addCleanup(lambda: setattr(tmdb, "is_configured", originales[0])
                        or setattr(tmdb, "search_movies", originales[1])
                        or setattr(tmdb, "fetch_details", originales[2]))

    def test_con_la_ficha_el_nombre_lleva_su_titulo_salvo_si_se_edito(self):
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, B_DCP)
        self.assertTrue(asyncio.run(self.mux._hidratar_tmdb(p["id"])))
        q = self.storage.load_mux_project(p["id"])
        self.assertEqual(q.nombre_salida, "La película (2024) [DV FEL] [Audio DCP] [CMv4 FULL].mkv")
        self.put(p["id"], nombre_salida="El mío")
        asyncio.run(self.mux._hidratar_tmdb(p["id"]))
        self.assertEqual(self.storage.load_mux_project(p["id"]).nombre_salida, "El mío.mkv")

    def test_con_la_ficha_nueva_un_terminado_deja_de_serlo(self):
        """El nombre cambia con la ficha, y el MKV de Output tiene el de antes:
        «terminado» ya no lo describe (revisión de Codex del 2026-10-27)."""
        p = self.crear()
        self.elegir(p["id"], A_VIDEO, B_DCP)
        q = self.storage.load_mux_project(p["id"])
        q.status = "done"
        self.storage.save_mux_project(q)
        self.assertTrue(asyncio.run(self.mux._hidratar_tmdb(p["id"])))
        q = self.storage.load_mux_project(p["id"])
        self.assertTrue(q.nombre_salida.startswith("La película (2024)"), q.nombre_salida)
        self.assertEqual(q.status, "pending")


class TestArranque(MuxCase):

    def test_lo_que_quedo_a_medias(self):
        p = self.crear()
        d = self.elegir(p["id"], A_VIDEO, B_DCP)
        q = self.con_medida(p["id"])
        q.status = "running"
        self.storage.save_mux_project(q)
        tmp = Path(str(self.output_dir / d["nombre_salida"]) + ".tmp")
        tmp.write_bytes(b"a medias")
        otro = self.crear()
        o = self.storage.load_mux_project(otro["id"])
        o.status = "queued"
        from models import MuxSincronia
        o.sincronia = MuxSincronia(estado="midiendo")
        self.storage.save_mux_project(o)
        self.mux.recuperar_proyectos_interrumpidos()
        q2 = self.storage.load_mux_project(p["id"])
        o2 = self.storage.load_mux_project(otro["id"])
        self.assertEqual(q2.status, "error")
        self.assertFalse(tmp.exists())
        self.assertEqual(o2.status, "pending")
        self.assertEqual(o2.sincronia.estado, "error")


class TestColumnaDeTrabajo(MuxCase):

    def test_el_adaptador_cuenta_la_fase_y_el_porcentaje(self):
        import time
        import trabajos
        self.mux._progreso.update(id="mux_x", fase="combinar", pct=42.0, paso="",
                                  desde=time.monotonic() - 60, fase_desde=time.monotonic() - 50,
                                  chips=["Vídeo a 24 fps"])
        t = self.qm.TrabajoEnCola(tab="mux", tipo=self.qm.TIPO_COMBINAR, clave="mux_x", que="x")
        d = trabajos.progreso_de(t)
        self.assertEqual((d["fase_n"], d["fases_total"], d["pct"], d["detalle"]), (2, 3, 42, "combinar"))
        self.assertTrue(d["pct_medido"])
        self.assertEqual(d["chips"], ["Vídeo a 24 fps"])
        self.mux._progreso.update(fase="comprobar_y_guardar", pct=None)
        self.assertFalse(trabajos.progreso_de(t)["pct_medido"])


if __name__ == "__main__":
    unittest.main()
