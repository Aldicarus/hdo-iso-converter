"""
El plan de «Combinar MKV», ejecutado sobre el caso real.

Las fuentes son las pistas de los dos ficheros que lo originaron (Toy Story 5,
2026-10-25): un remux P7 FEL a 23,976 fps con 48 pistas y un WEB-DL a 24 fps
con su pista DCP, capturadas con `mkvmerge -J` en el NAS y saneadas
(`fixtures_mux/`). La medida es la que dio la versión final sobre esos mismos
ficheros: otro ritmo, 1000/1001 más 5,49 ppm, siete de siete puntos.

Lo que se fija es lo que decide si el MKV sale bien: qué línea de tiempo
manda, qué se retima, qué `--sync` lleva cada pista y cuándo NO se combina.
"""
import json
import sys
import unittest
from fractions import Fraction
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from models import (Chapter, MuxFuente, MuxPista, MuxProject,  # noqa: E402
                    MuxSincronia)
from phases import mux_pipeline, mux_plan, mux_sync  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures_mux"

NOMBRE_VIDEO = "Toy.Story.5.2026.UHD.BluRay.REMUX.DV.P7.HDR.MULTi [CMv4 FULL].mkv"
NOMBRE_RESTO = "Toy Story 5 (2026)[Rotulado Castellano][Audio DCP 7.1.4] [iT WEB-DL 2160p].mkv"

# La medida real (prueba_mux_sync2 sobre el NAS): α = 0,999006491.
ALFA_REAL = Fraction(0.999006491).limit_denominator(10_000_000)


def fuente(fixture: str, nombre: str, *, dv=None, capitulos=0) -> MuxFuente:
    j = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    pistas = mux_pipeline.pistas_de_j(j)

    class _Dovi:
        profile, el_type, cm_version = dv or (0, "", "")
    video = mux_pipeline._resumen_de_video(pistas, None, _Dovi() if dv else None)
    huella = {"sha256_1mb": nombre, "size_bytes": 1, "mtime_ns": 0}
    caps = [Chapter(number=i + 1, timestamp=f"00:{i * 10:02d}:00.000",
                    name=f"Capítulo {i + 1:02d}") for i in range(capitulos)]
    return MuxFuente(ruta=f"/mnt/isos/{nombre}", nombre=nombre, huella=huella,
                     duracion_s=j["container"]["properties"]["duration"] / 1e9,
                     video=video, pistas=pistas, capitulos=caps)


def proyecto(sincronia=None, **kw) -> MuxProject:
    v = fuente("remux_p7_fel_23976.mkv.json", NOMBRE_VIDEO, dv=(7, "FEL", "v4.0"),
               capitulos=8)
    r = fuente("webdl_dcp_24.mkv.json", NOMBRE_RESTO, capitulos=3)
    p = mux_pipeline.proyecto_nuevo("mux_prueba", v, r)
    if sincronia is not None:
        p.sincronia = sincronia
    for k, val in kw.items():
        setattr(p, k, val)
    return p


def medida(veredicto, alfa=Fraction(1), beta_ms=0, p=None, **kw):
    p = p or proyecto()
    return MuxSincronia(estado="medida", veredicto=veredicto,
                        alfa_num=alfa.numerator, alfa_den=alfa.denominator,
                        beta_ms=beta_ms, n_validos=7, n_puntos=7,
                        huellas=[f.huella for f in p.fuentes], **kw)


def con_medida(veredicto, alfa=Fraction(1), beta_ms=0, **kw) -> MuxProject:
    p = proyecto()
    p.sincronia = medida(veredicto, alfa, beta_ms, p=p, **kw)
    return p


def valor(cmd, opcion):
    """Todos los valores de una opción del argv, en orden."""
    return [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == opcion]


class TestLaSeleccionPorDefecto(unittest.TestCase):

    def test_del_video_solo_el_video_y_del_otro_todo_lo_demas(self):
        p = proyecto()
        self.assertEqual([(x.fuente, x.id) for x in p.pistas],
                         [(1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6)])
        self.assertEqual(p.capitulos_de, "resto")
        self.assertEqual(p.adjuntos, [])     # el JPG del grupo no se copia

    def test_conserva_nombres_y_marcas_del_otro(self):
        p = proyecto()
        dcp = p.pistas[0]
        self.assertEqual(dcp.nombre, "Castellano TrueHD Atmos 7.1-DCP")
        self.assertTrue(dcp.por_defecto)

    def test_sin_audio_en_el_otro_se_toma_el_del_video(self):
        v = fuente("remux_p7_fel_23976.mkv.json", NOMBRE_VIDEO, dv=(7, "FEL", "v4.0"))
        r = fuente("webdl_dcp_24.mkv.json", NOMBRE_RESTO)
        r.pistas = [x for x in r.pistas if x.tipo != "audio"]
        sel = mux_plan.seleccion_por_defecto([v, r])
        audios = [x for x in sel["pistas"] if x.fuente == 0]
        self.assertEqual(len(audios), 18)
        self.assertTrue(all(x.fuente == 1 for x in sel["pistas"] if x.id in (5, 6) and x.fuente == 1))


class TestElNombre(unittest.TestCase):

    def test_sale_el_nombre_que_el_usuario_escribio_a_mano(self):
        self.assertEqual(proyecto().nombre_salida,
                         "Toy Story 5 (2026) [DV FEL] [Audio DCP] [CMv4 FULL].mkv")

    def test_sin_fel_ni_dcp_no_hay_tags_inventados(self):
        p = proyecto()
        p.fuentes[0].video.dv_el = "MEL"
        p.fuentes[0].nombre = "Toy Story 5 (2026).mkv"
        p.fuentes[1].nombre = "Toy Story 5 (2026) WEB-DL.mkv"
        p.pistas = [x for x in p.pistas if "DCP" not in x.nombre]
        self.assertEqual(mux_plan.nombre_por_defecto(p), "Toy Story 5 (2026).mkv")


class TestLaLineaDeTiempo(unittest.TestCase):

    def test_el_caso_real_retima_el_video_al_ritmo_medido(self):
        p = con_medida(mux_sync.OTRO_RITMO, ALFA_REAL, beta_ms=3)
        plan = mux_plan.plan(p)
        self.assertTrue(plan["puede_combinar"], plan["motivos"])
        self.assertEqual(plan["linea"], mux_plan.LINEA_DEL_RESTO)
        cadencia = Fraction(*plan["retimar_video"])
        self.assertAlmostEqual(float(cadencia), 23.999868, places=5)
        # β = 3 ms es ruido: nada se retrasa.
        self.assertEqual(plan["sync"], {})
        cmd = mux_plan.argv(p, plan, "/mnt/output/x.mkv.tmp")
        self.assertIn("0:41666896ns", valor(cmd, "--default-duration"))
        self.assertNotIn("--fix-bitstream-timing-information", cmd)

    def test_con_la_cadencia_exacta_se_escribe_24p(self):
        p = con_medida(mux_sync.OTRO_RITMO, Fraction(1000, 1001))
        cmd = mux_plan.argv(p, mux_plan.plan(p), "/x.tmp")
        self.assertEqual(valor(cmd, "--default-duration"), ["0:24p"])

    def test_con_retraso_al_retimar_se_mueve_lo_del_otro(self):
        # α y β a la vez: el vídeo y lo del MKV del vídeo van a α·t; lo del
        # otro, que está en α·t + β, se adelanta β. Mezclar los dos orígenes
        # dejaba los subtítulos del vídeo β tarde (revisión de Codex).
        p = con_medida(mux_sync.OTRO_RITMO, Fraction(1000, 1001), beta_ms=2000)
        p.pistas.append(MuxPista(fuente=0, id=22, nombre="Castellano SRT"))
        plan = mux_plan.plan(p)
        self.assertEqual(plan["sync"]["1:1"], {"retraso_ms": -2000, "factor": None})
        self.assertEqual(plan["sync"]["0:22"], {"retraso_ms": 0, "factor": [1000, 1001]})
        cmd = mux_plan.argv(p, plan, "/x.tmp")
        self.assertIn("22:0,1000/1001", valor(cmd, "--sync"))
        self.assertIn("1:-2000", valor(cmd, "--sync"))

    def test_mismo_ritmo_con_un_logo_mas_largo(self):
        p = con_medida(mux_sync.RETRASO, beta_ms=2350)
        plan = mux_plan.plan(p)
        self.assertIsNone(plan["retimar_video"])
        self.assertEqual(plan["linea"], mux_plan.LINEA_DEL_VIDEO)
        self.assertEqual({k: v["retraso_ms"] for k, v in plan["sync"].items()},
                         {f"1:{i}": -2350 for i in range(1, 7)})
        self.assertEqual(plan["capitulos"]["retraso_ms"], -2350)
        self.assertTrue(mux_plan.capitulos_necesitan_xml(plan))

    def test_audio_solo_del_video_estira_lo_del_otro(self):
        # Del otro solo los subtítulos: manda el vídeo y los subtítulos del
        # otro se llevan a su línea, t = (t_otro − β)/α.
        p = con_medida(mux_sync.OTRO_RITMO, Fraction(1000, 1001), beta_ms=1001)
        p.pistas = [MuxPista(fuente=0, id=1, nombre="Inglés TrueHD", por_defecto=True),
                    MuxPista(fuente=1, id=5, nombre="Castellano")]
        plan = mux_plan.plan(p)
        self.assertTrue(plan["puede_combinar"], plan["motivos"])
        self.assertIsNone(plan["retimar_video"])
        self.assertEqual(plan["sync"]["1:5"], {"retraso_ms": -1002, "factor": [1001, 1000]})

    def test_audio_de_los_dos_con_ritmos_distintos_no_se_combina(self):
        p = con_medida(mux_sync.OTRO_RITMO, Fraction(1000, 1001))
        p.pistas.append(MuxPista(fuente=0, id=1, nombre="Inglés TrueHD"))
        plan = mux_plan.plan(p)
        self.assertFalse(plan["puede_combinar"])
        self.assertIn("audio_de_dos_ritmos", [m["id"] for m in plan["motivos"]])
        # Y aceptar el riesgo NO lo desbloquea: no hay plan que lo arregle.
        p.acepta_riesgo = True
        self.assertFalse(mux_plan.plan(p)["puede_combinar"])

    def test_un_video_sin_cadencia_fija_no_se_retima(self):
        """Retimar escribe UNA duración de frame para todo el vídeo. Sobre un
        VFR, o con la BL y la EL en pistas con duraciones distintas, algún
        tramo quedaría al ritmo equivocado: no se combina, ni aceptando el
        riesgo. Y con el mismo ritmo no hay nada que retimar, así que el VFR
        no estorba."""
        casos = {
            "vfr": lambda p: setattr(p.fuentes[0].video, "modo_cadencia", "VFR"),
            "sin_duracion": lambda p: [setattr(v, "default_duration_ns", 0)
                                       for v in p.fuentes[0].pistas if v.tipo == "video"],
            "dos_duraciones": lambda p: p.fuentes[0].pistas.insert(1, p.fuentes[0].pistas[0]
                                         .model_copy(update={"id": 99,
                                                             "default_duration_ns": 41666667})),
        }
        for nombre, romper in casos.items():
            with self.subTest(caso=nombre):
                p = con_medida(mux_sync.OTRO_RITMO, Fraction(1000, 1001))
                self.assertTrue(mux_plan.plan(p)["puede_combinar"])
                romper(p)
                plan = mux_plan.plan(p)
                self.assertIn("video_no_retimable", [m["id"] for m in plan["motivos"]])
                p.acepta_riesgo = True
                self.assertFalse(mux_plan.plan(p)["puede_combinar"])
        p = con_medida(mux_sync.RETRASO, beta_ms=2350)
        p.fuentes[0].video.modo_cadencia = "VFR"
        self.assertTrue(mux_plan.plan(p)["puede_combinar"])

    def test_el_modo_de_cadencia_sale_de_mediainfo(self):
        j = json.loads((FIXTURES / "remux_p7_fel_23976.mkv.json").read_text(encoding="utf-8"))
        pistas = mux_pipeline.pistas_de_j(j)
        mi = {"media": {"track": [{"@type": "Video", "FrameRate_Mode": "vfr"}]}}
        self.assertEqual(mux_pipeline._resumen_de_video(pistas, mi, None).modo_cadencia, "VFR")
        self.assertEqual(mux_pipeline._resumen_de_video(pistas, None, None).modo_cadencia, "")

    def test_con_el_mismo_ritmo_el_audio_de_los_dos_si_vale(self):
        p = con_medida(mux_sync.EN_SINCRONIA)
        p.pistas.append(MuxPista(fuente=0, id=1, nombre="Inglés TrueHD"))
        self.assertTrue(mux_plan.plan(p)["puede_combinar"])

    def test_lo_que_no_cuadra_se_acepta_o_no_se_combina(self):
        for veredicto, extra in ((mux_sync.MONTAJE_DISTINTO, {"salto_entre": [2797, 3652]}),
                                 (mux_sync.DERIVA, {"pendiente_ppm": 40.0}),
                                 (mux_sync.NO_CONCLUYENTE, {})):
            with self.subTest(veredicto=veredicto):
                p = con_medida(veredicto, **extra)
                self.assertFalse(mux_plan.plan(p)["puede_combinar"])
                p.acepta_riesgo = True
                self.assertTrue(mux_plan.plan(p)["puede_combinar"])

    def test_una_medida_de_otros_ficheros_no_vale(self):
        p = con_medida(mux_sync.EN_SINCRONIA)
        p.fuentes[1].huella = {"sha256_1mb": "otro", "size_bytes": 1}
        self.assertIn("medida_vieja", [m["id"] for m in mux_plan.plan(p)["motivos"]])

    def test_midiendo_o_sin_medir_no_se_combina(self):
        p = proyecto()
        self.assertIn("sin_medida", [m["id"] for m in mux_plan.plan(p)["motivos"]])
        p.sincronia = MuxSincronia(estado="midiendo")
        plan = mux_plan.plan(p)
        self.assertIn("midiendo", [m["id"] for m in plan["motivos"]])
        p.acepta_riesgo = True
        self.assertFalse(mux_plan.plan(p)["puede_combinar"])

    def test_en_marcha_cuenta_salvo_para_quien_la_ejecuta(self):
        p = con_medida(mux_sync.EN_SINCRONIA)
        p.status = "running"
        self.assertFalse(mux_plan.plan(p)["puede_combinar"])
        self.assertTrue(mux_plan.plan(p, ignorar_estado=True)["puede_combinar"])

    def test_sin_audio_no_se_combina(self):
        p = con_medida(mux_sync.EN_SINCRONIA)
        p.pistas = [x for x in p.pistas if x.id in (5, 6)]
        self.assertIn("sin_audio", [m["id"] for m in mux_plan.plan(p)["motivos"]])


class TestElArgv(unittest.TestCase):

    def test_dos_entradas_con_el_orden_del_proyecto(self):
        p = con_medida(mux_sync.EN_SINCRONIA)
        cmd = mux_plan.argv(p, mux_plan.plan(p), "/out.mkv.tmp")
        self.assertEqual(cmd[:4], ["mkvmerge", "--gui-mode", "-o", "/out.mkv.tmp"])
        i_video = cmd.index(p.fuentes[0].ruta)
        i_resto = cmd.index(p.fuentes[1].ruta)
        self.assertLess(i_video, i_resto)
        primera, segunda = cmd[:i_video], cmd[i_video:i_resto]
        self.assertIn("--no-audio", primera)
        self.assertEqual(valor(primera, "--video-tracks"), ["0"])
        self.assertIn("--no-video", segunda)
        self.assertEqual(valor(segunda, "--audio-tracks"), ["1,2,3,4"])
        self.assertEqual(valor(segunda, "--subtitle-tracks"), ["5,6"])
        self.assertIn("--no-global-tags", segunda)
        self.assertEqual(valor(cmd, "--track-order"), ["0:0,1:1,1:2,1:3,1:4,1:5,1:6"])

    def test_cada_pista_lleva_su_nombre_y_sus_marcas(self):
        p = con_medida(mux_sync.EN_SINCRONIA)
        p.pistas[0].nombre = "Castellano TrueHD Atmos 7.1 (DCP 9.1.6)"
        p.pistas[4].forzada = True
        cmd = mux_plan.argv(p, mux_plan.plan(p), "/x.tmp")
        self.assertIn("1:Castellano TrueHD Atmos 7.1 (DCP 9.1.6)", valor(cmd, "--track-name"))
        self.assertIn("5:yes", valor(cmd, "--forced-track"))
        self.assertIn("1:yes", valor(cmd, "--default-track"))
        self.assertIn("2:no", valor(cmd, "--default-track"))

    def test_los_capitulos_se_piden_a_un_solo_sitio(self):
        p = con_medida(mux_sync.EN_SINCRONIA)
        cmd = mux_plan.argv(p, mux_plan.plan(p), "/x.tmp")
        i_resto = cmd.index(p.fuentes[1].ruta)
        self.assertIn("--no-chapters", cmd[:cmd.index(p.fuentes[0].ruta)])
        self.assertNotIn("--no-chapters", cmd[cmd.index(p.fuentes[0].ruta):i_resto])
        con_xml = mux_plan.argv(p, mux_plan.plan(p), "/x.tmp", "/caps.xml")
        self.assertEqual(valor(con_xml, "--chapters"), ["/caps.xml"])
        self.assertEqual(con_xml.count("--no-chapters"), 2)

    def test_un_adjunto_elegido_se_pide_y_los_demas_no(self):
        p = con_medida(mux_sync.EN_SINCRONIA)
        p.adjuntos = [{"fuente": 0, "id": 1}]
        cmd = mux_plan.argv(p, mux_plan.plan(p), "/x.tmp")
        self.assertEqual(valor(cmd, "--attachments"), ["1"])
        self.assertEqual(cmd.count("--no-attachments"), 1)


class TestLosCapitulos(unittest.TestCase):

    XML = """<?xml version="1.0"?>
<Chapters><EditionEntry>
<ChapterAtom><ChapterTimeStart>00:00:00.000000000</ChapterTimeStart>
<ChapterDisplay><ChapterString>Logo</ChapterString><ChapterLanguage>spa</ChapterLanguage></ChapterDisplay></ChapterAtom>
<ChapterAtom><ChapterTimeStart>00:00:05.000000000</ChapterTimeStart><ChapterTimeEnd>00:10:00.000000000</ChapterTimeEnd>
<ChapterDisplay><ChapterString>Empieza</ChapterString></ChapterDisplay></ChapterAtom>
<ChapterAtom><ChapterTimeStart>01:40:00.000000000</ChapterTimeStart>
<ChapterDisplay><ChapterString>Créditos</ChapterString></ChapterDisplay></ChapterAtom>
</EditionEntry></Chapters>"""

    def test_un_retraso_mueve_todos_y_conserva_lo_demas(self):
        xml = mux_plan.mapear_capitulos_xml(self.XML, None, 1500)
        self.assertEqual(mux_plan.capitulos_mapeados(xml),
                         [1_500_000_000, 6_500_000_000, 6_001_500_000_000])
        self.assertIn("Créditos", xml)
        self.assertIn("<ChapterLanguage>spa</ChapterLanguage>", xml)

    def test_lo_que_cae_antes_del_cero_no_deja_dos_capitulos_en_el_cero(self):
        # Un logo 6 s más largo en el otro release: «Logo» y «Empieza» caen
        # antes del cero. Queda uno solo, el último, en el cero.
        xml = mux_plan.mapear_capitulos_xml(self.XML, None, -6000)
        inicios = mux_plan.capitulos_mapeados(xml)
        self.assertEqual(inicios[0], 0)
        self.assertEqual(len(inicios), 2)
        self.assertIn("Empieza", xml)
        self.assertNotIn("Logo", xml)

    def test_lo_que_cae_despues_del_final_se_quita(self):
        xml = mux_plan.mapear_capitulos_xml(self.XML, None, 0, duracion_final_s=5000.0)
        self.assertNotIn("Créditos", xml)

    def test_el_factor_estira_y_el_fin_imposible_se_quita(self):
        xml = mux_plan.mapear_capitulos_xml(self.XML, Fraction(1000, 1001), 0)
        self.assertEqual(mux_plan.capitulos_mapeados(xml)[1], 4_995_004_995)
        estrecho = mux_plan.mapear_capitulos_xml(
            self.XML.replace("00:10:00.000000000", "00:00:05.500000000"), None, -5600)
        self.assertNotIn("ChapterTimeEnd", estrecho)


class TestTamanoYComprobacion(unittest.TestCase):

    def test_el_tamano_es_la_suma_de_lo_elegido(self):
        p = proyecto()
        esperado = sum(x.bytes for x in p.fuentes[0].pistas if x.tipo == "video")
        esperado += sum(o.bytes for o in p.fuentes[1].pistas if o.tipo != "video")
        self.assertEqual(mux_plan.tamano_estimado(p), int(esperado * 1.005))

    def test_una_pista_sin_tamano_cancela_la_cuenta(self):
        p = proyecto()
        p.fuentes[1].pistas[2].bytes = 0
        self.assertIsNone(mux_plan.tamano_estimado(p))

    def test_la_comprobacion_ve_un_nombre_cambiado_y_una_pista_de_menos(self):
        p = con_medida(mux_sync.EN_SINCRONIA)
        plan = mux_plan.plan(p)
        esperadas = mux_plan.pistas_esperadas(p, plan)
        j = {"tracks": [{"type": e["tipo"], "codec": e["codec"], "properties": {
            "language": e["idioma"], "track_name": e["nombre"],
            "default_track": e.get("por_defecto", False),
            "forced_track": e.get("forzada", False),
            "default_duration": e.get("default_duration_ns"),
            "tag_number_of_frames": str(e.get("frames") or "")}} for e in esperadas]}
        self.assertTrue(all(c["ok"] for c in mux_plan.comprobar_pistas(esperadas, j)))
        j["tracks"][3]["properties"]["track_name"] = "otro"
        malos = [c for c in mux_plan.comprobar_pistas(esperadas, j) if not c["ok"]]
        self.assertEqual([(c["id"], c["pista"]) for c in malos], [("nombre", 4)])
        j["tracks"].pop()
        self.assertEqual(mux_plan.comprobar_pistas(esperadas, j)[0]["id"], "numero_de_pistas")
        self.assertFalse(mux_plan.comprobar_pistas(esperadas, j)[0]["ok"])

    def test_las_estadisticas_ajenas_no_dan_frames(self):
        self.assertEqual(mux_pipeline._frames_fiables(
            {"tag_number_of_frames": "100", "tag__statistics_writing_app": "mkvmerge v88"}), 100)
        self.assertEqual(mux_pipeline._frames_fiables(
            {"tag_number_of_frames": "100", "tag__statistics_writing_app": "HandBrake"}), 0)


if __name__ == "__main__":
    unittest.main()
