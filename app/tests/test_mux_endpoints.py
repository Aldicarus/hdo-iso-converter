"""«Combinar MKV» por HTTP y con los binarios falsos: de crear a combinar.

Los dos orígenes reproducen el caso real con números redondos: un remux P7 FEL
a 23,976 fps y un WEB-DL a 24 fps con su pista DCP. El `ffmpeg` falso suena
cada uno en su línea de tiempo (`define_linea_de_tiempo`), así que la medida de
sincronía y la comprobación final se EJECUTAN, y una combinación con el signo
cambiado o sin retimar el vídeo se ve como se vería en el NAS.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_mux_endpoints -v
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
VIDEO = "Peli.2024.UHD.BluRay.REMUX.DV.P7 [CMv4 FULL].mkv"
RESTO = "Peli (2024)[Rotulado Castellano][Audio DCP 7.1.4] [WEB-DL 2160p].mkv"


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

    def _origenes(self, *, beta_resto=0.25, alfa_resto=1000 / 1001):
        tb = self.tb
        self.video = self.isos_dir / VIDEO
        self.resto = self.isos_dir / RESTO
        for f in (self.video, self.resto):
            f.write_bytes(f.name.encode() * 64)
        tb.define_mkv(self.video.name, duration_s=FRAMES / FPS_BD, tracks=[
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "dimensions": "3840x2160",
             "fps": FPS_BD, "frames": FRAMES, "bytes": 5_000_000, "track_name": "HEVC DV P7 FEL CMv4.0"},
            {"id": 1, "type": "audio", "codec": "TrueHD Atmos", "language": "eng",
             "language_ietf": "en", "channels": 8, "default": True, "bytes": 300_000},
            {"id": 2, "type": "audio", "codec": "AC-3", "language": "spa", "language_ietf": "es-419",
             "channels": 6, "bytes": 40_000},
            {"id": 3, "type": "subtitles", "codec": "SubRip/SRT", "language": "eng",
             "codec_id": "S_TEXT/UTF8", "bytes": 5_000},
        ], attachments=[{"id": 1, "file_name": "cover.jpg", "content_type": "image/jpeg", "size": 900}])
        tb.define_mkv(self.resto.name, duration_s=FRAMES / 24.0, tracks=[
            {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "dimensions": "3840x2076",
             "fps": 24.0, "frames": FRAMES, "bytes": 2_000_000},
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
        tb.define_chapters(self.resto.name, [("00:00:00.000", "Inicio"), ("00:30:00.000", "Medio"),
                                             ("01:50:00.000", "Final")])
        tb.define_rpu(self.video.name, profile=7, el_type="FEL", cm_version="v4.0", frames=FRAMES)
        tb.define_linea_de_tiempo(self.video.name, semilla=11)
        tb.define_linea_de_tiempo(self.resto.name, alfa=alfa_resto, beta_s=beta_resto, semilla=11)

    # ── ayudas ──
    def crear(self, **body):
        r = self.client.post("/api/mux/create", json={"video": str(self.video),
                                                      "resto": str(self.resto), **body})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def con_medida(self, pid, **campos):
        from models import MuxSincronia
        p = self.storage.load_mux_project(pid)
        base = dict(estado="medida", veredicto="otro_ritmo", alfa_num=1000, alfa_den=1001,
                    beta_ms=250, n_validos=7, n_puntos=7, huellas=[f.huella for f in p.fuentes])
        base.update(campos)
        p.sincronia = MuxSincronia(**base)
        self.storage.save_mux_project(p)
        return p

    def combinar(self, pid, durante=None):
        r = self.client.post(f"/api/mux/{pid}/ejecutar")
        self.assertEqual(r.status_code, 200, r.text)

        async def _correr():
            tarea = asyncio.create_task(self.mux._combinar(pid))
            if durante:
                await durante()
            await asyncio.wait_for(tarea, 120)
        asyncio.run(_correr())
        return self.storage.load_mux_project(pid)


class TestCrear(MuxCase):

    def test_lee_los_dos_y_propone_el_caso_real(self):
        p = self.crear()
        self.assertEqual(p["nombre_salida"],
                         "Peli (2024) [DV FEL] [Audio DCP] [CMv4 FULL].mkv")
        self.assertEqual([(x["fuente"], x["id"]) for x in p["pistas"]],
                         [(1, 1), (1, 2), (1, 3), (1, 4)])
        self.assertEqual(p["capitulos_de"], "resto")
        self.assertEqual(p["adjuntos"], [], "la carátula del release no se copia")
        v = p["fuentes"][0]["video"]
        self.assertEqual((v["dv_profile"], v["dv_el"], v["dv_cm"]), (7, "FEL", "v4.0"))
        self.assertEqual((v["cadencia_num"], v["cadencia_den"]), (24000, 1001))
        self.assertEqual(p["fuentes"][0]["pistas"][2]["idioma_ietf"], "es-419")
        self.assertEqual(self.medidas_pedidas, [p["id"]], "la medida arranca sola")
        self.assertIn("sin_medida", [m["id"] for m in p["plan"]["motivos"]])

    def test_fuera_de_los_tres_sitios_no(self):
        fuera = self.tmp / "otro.mkv"
        fuera.write_bytes(b"x")
        r = self.client.post("/api/mux/create", json={"video": str(fuera), "resto": str(self.resto)})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/mux/create", json={
            "video": str(self.isos_dir / ".." / "isos" / ".." / ".." / "otro.mkv"), "resto": str(self.resto)})
        self.assertEqual(r.status_code, 400)

    def test_el_mismo_fichero_dos_veces_no(self):
        r = self.client.post("/api/mux/create", json={"video": str(self.video), "resto": str(self.video)})
        self.assertEqual(r.status_code, 400)

    def test_solo_mkv(self):
        iso = self.isos_dir / "Peli.iso"
        iso.write_bytes(b"x")
        r = self.client.post("/api/mux/create", json={"video": str(iso), "resto": str(self.resto)})
        self.assertEqual(r.status_code, 400)

    def test_un_mkv_sin_video_como_video_no(self):
        self.tb.define_mkv(self.video.name, tracks=[
            {"id": 0, "type": "audio", "codec": "AC-3", "language": "eng", "channels": 2}])
        r = self.client.post("/api/mux/create", json={"video": str(self.video), "resto": str(self.resto)})
        self.assertEqual(r.status_code, 400)

    def test_el_listado_trae_el_relato_sin_lo_pesado(self):
        p = self.crear()
        r = self.client.get("/api/mux")
        self.assertEqual(r.status_code, 200)
        [item] = r.json()["proyectos"]
        self.assertEqual(item["id"], p["id"])
        self.assertEqual(item["fuentes"][0]["pistas"], [])
        self.assertEqual(item["fuentes"][0]["n_pistas"], 4)
        self.assertEqual(item["relato"]["situacion"], "esperando_decision")


class TestEditar(MuxCase):

    def test_cambia_orden_nombres_y_marcas(self):
        p = self.crear()
        pistas = [{"fuente": 0, "id": 1, "nombre": "Inglés TrueHD Atmos 7.1", "por_defecto": False},
                  {"fuente": 1, "id": 1, "nombre": "Castellano TrueHD Atmos 7.1 (DCP 9.1.6)",
                   "por_defecto": True}]
        r = self.client.put(f"/api/mux/{p['id']}", json={"pistas": pistas, "capitulos_de": "video"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual([(x["fuente"], x["id"]) for x in d["pistas"]], [(0, 1), (1, 1)])
        self.assertEqual(d["capitulos_de"], "video")

    def test_una_pista_que_no_existe_o_de_video_no(self):
        p = self.crear()
        for pista in ({"fuente": 1, "id": 99}, {"fuente": 0, "id": 0}, {"fuente": 2, "id": 1}):
            with self.subTest(pista=pista):
                r = self.client.put(f"/api/mux/{p['id']}", json={"pistas": [pista]})
                self.assertEqual(r.status_code, 400)
        r = self.client.put(f"/api/mux/{p['id']}", json={
            "pistas": [{"fuente": 1, "id": 1}, {"fuente": 1, "id": 1}]})
        self.assertEqual(r.status_code, 400)

    def test_el_nombre_no_sale_de_output(self):
        p = self.crear()
        r = self.client.put(f"/api/mux/{p['id']}", json={"nombre_salida": "../biblioteca/x.mkv"})
        self.assertEqual(r.status_code, 400)
        r = self.client.put(f"/api/mux/{p['id']}", json={"nombre_salida": "Otro nombre"})
        self.assertEqual(r.json()["nombre_salida"], "Otro nombre.mkv")
        self.assertTrue(r.json()["nombre_manual"])

    def test_cambiar_la_pareja_vuelve_a_medir(self):
        p = self.crear()
        self.medidas_pedidas.clear()
        r = self.client.put(f"/api/mux/{p['id']}", json={"pareja": [2, 1]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.medidas_pedidas, [p["id"]])
        r = self.client.put(f"/api/mux/{p['id']}", json={"pareja": [3, 1]})
        self.assertEqual(r.status_code, 400, "la 3 del vídeo es un subtítulo")


class TestEjecutar(MuxCase):

    def test_sin_medida_no_se_encola(self):
        p = self.crear()
        r = self.client.post(f"/api/mux/{p['id']}/ejecutar")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.trabajos_encolados, [])

    def test_con_medida_se_encola_y_no_se_edita(self):
        p = self.crear()
        self.con_medida(p["id"])
        r = self.client.post(f"/api/mux/{p['id']}/ejecutar")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([t[:2] for t in self.trabajos_encolados],
                         [(self.qm.TIPO_COMBINAR, p["id"])])
        self.assertEqual(r.json()["status"], "queued")
        self.assertEqual(self.client.post(f"/api/mux/{p['id']}/ejecutar").status_code, 409)
        self.assertEqual(self.client.put(f"/api/mux/{p['id']}", json={"titulo": "x"}).status_code, 409)

    def test_un_mkv_que_ya_existe_no_se_pisa(self):
        p = self.crear()
        self.con_medida(p["id"])
        (self.output_dir / p["nombre_salida"]).write_bytes(b"el bueno")
        r = self.client.post(f"/api/mux/{p['id']}/ejecutar")
        self.assertEqual(r.status_code, 409)

    def test_audio_de_dos_ritmos_no_se_encola_ni_aceptando(self):
        p = self.crear()
        self.con_medida(p["id"])
        pistas = [dict(x, fuente=x["fuente"], id=x["id"]) for x in p["pistas"]]
        pistas.append({"fuente": 0, "id": 1, "nombre": "Inglés TrueHD"})
        self.client.put(f"/api/mux/{p['id']}", json={"pistas": pistas, "acepta_riesgo": True})
        r = self.client.post(f"/api/mux/{p['id']}/ejecutar")
        self.assertEqual(r.status_code, 409)


class TestCombinar(MuxCase):

    def test_el_caso_real_sale_retimado_sincronizado_y_firmado(self):
        p = self.crear()
        self.con_medida(p["id"])
        q = self.combinar(p["id"])
        self.assertEqual(q.status, "done", (q.error_message, q.validacion))
        final = self.output_dir / q.nombre_salida
        self.assertTrue(final.exists())
        self.assertFalse(Path(str(final) + ".tmp").exists())
        mux = [c for c in self.tb.find("mkvmerge") if c.has("-o")][0]
        self.assertEqual(mux.opt("--default-duration"), "0:24p")
        self.assertEqual(mux.opt("--sync"), "1:-250")
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
        # La línea apunta a SU ejecución: un proyecto combinado dos veces
        # tiene dos logs, y el detalle de la vieja no puede enseñar el nuevo.
        self.assertEqual(linea["ref_log"], f"mux:{p['id']}#1")
        self.assertEqual(q.historial[-1].run_number, 1)

    def test_sin_retimar_el_video_la_fase_c_lo_caza(self):
        # Un mkvmerge que no aplicara la cadencia: frames y nombre bien, pero
        # el vídeo dura lo que a 23,976 y el audio va a 24. Es el MKV que el
        # usuario hizo a mano con la GUI.
        p = self.crear()
        self.con_medida(p["id"])
        self.tb._scenario["default_duration_ignorada"] = True
        self.tb._flush()
        q = self.combinar(p["id"])
        self.assertEqual(q.status, "error")
        fallos = {c["id"] for c in q.validacion["checks"] if c.get("ok") is False}
        self.assertIn("duracion_del_video", fallos)
        self.assertTrue(Path(str(self.output_dir / q.nombre_salida) + ".tmp").exists(),
                        "un MKV que no cuadra se queda para revisarlo")

    def test_un_desfase_con_el_signo_cambiado_no_pasa(self):
        # La medida dice −250 donde el otro va +250: el plan lo aplica al revés
        # y el audio queda medio segundo fuera. La comprobación final lo mide.
        p = self.crear()
        self.con_medida(p["id"], beta_ms=-250)
        q = self.combinar(p["id"])
        self.assertEqual(q.status, "error")
        sinc = [c for c in q.validacion["checks"] if c["id"] == "sincronia"][0]
        self.assertFalse(sinc["ok"])
        self.assertGreater(float(sinc["real"].split()[0]), 400)

    def test_con_el_riesgo_aceptado_la_sincronia_es_un_aviso(self):
        p = self.crear()
        self.con_medida(p["id"], veredicto="montaje_distinto", beta_ms=-250,
                        salto_entre=[1000, 2000])
        self.client.put(f"/api/mux/{p['id']}", json={"acepta_riesgo": True})
        q = self.combinar(p["id"])
        self.assertEqual(q.status, "done", q.error_message)
        sinc = [c for c in q.validacion["checks"] if c["id"] == "sincronia"][0]
        self.assertTrue(sinc["aviso"])
        self.assertIsNone(sinc["ok"])

    def test_una_pista_desfasada_dentro_de_su_release_se_avisa(self):
        # El castellano del otro va 600 ms tarde DENTRO de su propio fichero:
        # la referencia (inglés) cuadra y la combinación es la que pedía el
        # plan, pero eso hay que decirlo.
        self.tb.define_linea_de_tiempo(self.resto.name, alfa=1000 / 1001, beta_s=0.25,
                                       semilla=11, pistas={0: {"alfa": 1000 / 1001, "beta_s": 0.85}})
        p = self.crear()
        self.con_medida(p["id"])
        q = self.combinar(p["id"])
        self.assertEqual(q.status, "done", q.error_message)
        avisos = [c for c in q.validacion["checks"] if c["id"] == "sincronia_pista" and c.get("aviso")]
        self.assertEqual([c["pista"] for c in avisos], [2], q.validacion["checks"])

    def test_un_origen_que_cambio_no_se_combina(self):
        p = self.crear()
        self.con_medida(p["id"])
        self.resto.write_bytes(b"otro contenido" * 100)
        q = self.combinar(p["id"])
        self.assertEqual(q.status, "error")
        self.assertIn(RESTO, q.error_message)
        self.assertFalse(any(c.has("-o") for c in self.tb.find("mkvmerge")),
                         "ni se lanza el mux")

    def test_cancelar_a_mitad_del_mux_no_deja_nada(self):
        p = self.crear()
        self.con_medida(p["id"])
        self.tb.retrasar_cuando_arg("mkvmerge", "--gui-mode", 8, parcial=True)
        tmp = Path(str(self.output_dir / p["nombre_salida"]) + ".tmp")

        async def _cancelar_en_el_mux():
            for _ in range(200):
                if self.mux._procesos.get(p["id"]) and tmp.exists():
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(tmp.exists(), "el parcial existe antes de cancelar")
            r = await self.mux.mux_cancelar(p["id"])
            self.assertTrue(r["ok"])
        q = self.combinar(p["id"], durante=_cancelar_en_el_mux)
        self.assertEqual(q.status, "pending")
        self.assertIsNotNone(q.last_cancelled_at)
        self.assertFalse(tmp.exists())
        self.assertEqual(q.historial[-1].status, "cancelled")


class TestMedida(MuxCase):

    def test_mide_el_ritmo_y_el_desfase_de_verdad(self):
        p = self.crear()
        from routers import mux
        estado = {"procs": [], "cancelar": False, "progreso": [0, 7]}
        asyncio.run(mux._medir(p["id"], estado))
        q = self.storage.load_mux_project(p["id"])
        s = q.sincronia
        self.assertEqual(s.estado, "medida", s.error)
        self.assertEqual(s.veredicto, "otro_ritmo", [x.model_dump() for x in s.puntos])
        self.assertEqual(Fraction(s.alfa_num, s.alfa_den), Fraction(1000, 1001))
        self.assertAlmostEqual(s.beta_ms, 250, delta=10)
        self.assertEqual(s.n_validos, 7)
        plan = self.client.get(f"/api/mux/{p['id']}").json()["plan"]
        self.assertTrue(plan["puede_combinar"], plan["motivos"])
        self.assertEqual(plan["retimar_video"], [24, 1])

    def test_cancelar_la_medida(self):
        p = self.crear()
        from routers import mux
        estado = {"procs": [], "cancelar": True, "progreso": [0, 7]}
        asyncio.run(mux._medir(p["id"], estado))
        self.assertEqual(self.storage.load_mux_project(p["id"]).sincronia.estado, "cancelada")


class TestArranque(MuxCase):

    def test_lo_que_quedo_a_medias(self):
        p = self.crear()
        q = self.con_medida(p["id"])
        q.status = "running"
        self.storage.save_mux_project(q)
        tmp = Path(str(self.output_dir / q.nombre_salida) + ".tmp")
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
