"""
El plan de «Multiplexar MKV», ejecutado sobre el caso real y en las DOS
direcciones.

Las fuentes son las pistas de los dos ficheros que lo originaron (Toy Story 5,
2026-10-25): un remux P7 FEL a 23,976 fps con 48 pistas (A) y un WEB-DL a 24
fps con su pista DCP (B), capturadas con `mkvmerge -J` en el NAS y saneadas
(`fixtures_mux/`). La medida es la que dio la versión final sobre esos mismos
ficheros: velocidades distintas, 1000/1001 más 5,49 ppm, siete de siete
puntos.

Desde el rediseño (2026-10-27) A y B no tienen papeles: el vídeo puede salir
de cualquiera de los dos, así que cada regla se comprueba con el vídeo de A
y con el de B. La comprobación que más vale es `TestTodoCaeEnSuSitio`: un
mismo sonido, puesto en las dos fuentes con la medida, tiene que acabar en el
MISMO instante del MKV final que el fotograma que lo acompaña, sea cual sea el
lado del vídeo, el del audio, la velocidad y el desfase.
"""
import json
import sys
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from models import (MuxAdjunto, MuxCapitulo, MuxCapituloOrigen,  # noqa: E402
                    MuxFuente, MuxPista, MuxPistaOrigen, MuxProject,
                    MuxSincronia)
from phases import mux_pipeline, mux_plan, mux_sync  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures_mux"

NOMBRE_A = "Toy.Story.5.2026.UHD.BluRay.REMUX.DV.P7.HDR.MULTi [CMv4 FULL].mkv"
NOMBRE_B = "Toy Story 5 (2026)[Rotulado Castellano][Audio DCP 7.1.4] [iT WEB-DL 2160p].mkv"

# La medida real (prueba_mux_sync2 sobre el NAS): α = 0,999006491.
ALFA_REAL = Fraction(0.999006491).limit_denominator(10_000_000)

CAPS_A = [(0, "Logo"), (600, "A 2"), (1200, "A 3"), (1800, "A 4"),
          (2400, "A 5"), (3000, "A 6"), (3600, "A 7"), (6000, "Créditos A")]
CAPS_B = [(0, "Inicio"), (2000, "B 2"), (6100, "B final")]


def fuente(fixture: str, nombre: str, *, dv=None, capitulos=()) -> MuxFuente:
    j = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    pistas = mux_pipeline.pistas_de_j(j)

    class _Dovi:
        profile, el_type, cm_version = dv or (0, "", "")
    video = mux_pipeline._resumen_de_video(pistas, None, _Dovi() if dv else None)
    huella = {"sha256_1mb": nombre, "size_bytes": 1, "mtime_ns": 0}
    caps = [MuxCapituloOrigen(edicion=0, indice=i, inicio_ns=int(t * 1e9), nombre=n)
            for i, (t, n) in enumerate(capitulos)]
    adjuntos = [MuxAdjunto(id=a["id"], nombre=a.get("file_name") or "",
                           mime=a.get("content_type") or "", tamano=a.get("size") or 0)
                for a in j.get("attachments") or []]
    return MuxFuente(ruta=f"/mnt/isos/{nombre}", nombre=nombre, huella=huella,
                     duracion_s=j["container"]["properties"]["duration"] / 1e9,
                     video=video, pistas=pistas, capitulos=caps, adjuntos=adjuntos)


def proyecto(**kw) -> MuxProject:
    a = fuente("remux_p7_fel_23976.mkv.json", NOMBRE_A, dv=(7, "FEL", "v4.0"),
               capitulos=CAPS_A)
    b = fuente("webdl_dcp_24.mkv.json", NOMBRE_B, capitulos=CAPS_B)
    p = mux_pipeline.proyecto_nuevo("mux_prueba", a, b)
    for k, val in kw.items():
        setattr(p, k, val)
    return p


def medida(p, veredicto=mux_sync.OTRO_RITMO, alfa=ALFA_REAL, beta_ms=3, **kw):
    return MuxSincronia(estado="medida", veredicto=veredicto,
                        alfa_num=alfa.numerator, alfa_den=alfa.denominator,
                        beta_ms=beta_ms, n_validos=7, n_puntos=7,
                        huellas=[f.huella for f in p.fuentes], **kw)


def elegir(p, *claves, nombre=True):
    """Pone en el proyecto las pistas `(fuente, id)`, con el nombre y las
    marcas de su origen, y lo normaliza como lo haría el PUT."""
    origen = {(i, o.id): o for i, f in enumerate(p.fuentes) for o in f.pistas}
    p.pistas = mux_plan.normalizar_pistas(p, [
        MuxPista(fuente=f, id=i, nombre=(origen[(f, i)].nombre or "") if nombre else "",
                 por_defecto=origen[(f, i)].por_defecto, forzada=origen[(f, i)].forzada)
        for f, i in claves])
    return p


def capitulos(p, *claves):
    p.capitulos = [MuxCapitulo(fuente=f, edicion=0, indice=i) for f, i in claves]
    return p


def valor(cmd, opcion):
    """Todos los valores de una opción del argv, en orden."""
    return [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == opcion]


def tramo_de(cmd, ruta):
    """Las opciones que van delante de una entrada (hasta la anterior)."""
    fin = cmd.index(ruta)
    rutas = [i for i, a in enumerate(cmd[:fin]) if a.startswith("/mnt/")]
    return cmd[(rutas[-1] + 1) if rutas else 0:fin]


A_VIDEO = (0, 0)
B_VIDEO = (1, 0)
A_TRUEHD, A_SPA, A_COMENTARIO = (0, 1), (0, 3), (0, 18)
A_SUB_SRT_ES = (0, 22)
B_DCP, B_DDP_ES, B_CAT, B_ENG = (1, 1), (1, 2), (1, 3), (1, 4)
B_SUB_ES, B_SUB_EN = (1, 5), (1, 6)


class TestNaceSinNadaElegido(unittest.TestCase):
    """Decisión del usuario (2026-10-27): el proyecto empieza vacío y se va
    sección a sección; lo que se quede vacío se avisa antes de multiplexar."""

    def test_no_hay_nada_elegido(self):
        p = proyecto()
        self.assertEqual((p.pistas, p.capitulos, p.adjuntos), ([], [], []))
        self.assertEqual(p.esquema, 2)

    def test_no_se_puede_multiplexar_y_dice_que_falta(self):
        plan = mux_plan.plan(proyecto())
        self.assertFalse(plan["puede_multiplexar"])
        self.assertEqual([m["id"] for m in plan["motivos"]], ["sin_video", "sin_audio"])
        self.assertFalse(plan["necesita_sincronia"])
        self.assertIsNone(plan["video_de"])

    def test_avisa_de_las_secciones_vacias_que_tienen_algo(self):
        avisos = mux_plan.plan(proyecto())["avisos"]
        self.assertEqual([a["params"]["seccion"] for a in avisos],
                         ["subtitulos", "capitulos", "otros"])

    def test_una_seccion_sin_nada_que_elegir_no_avisa(self):
        p = proyecto()
        for f in p.fuentes:
            f.adjuntos = []
        self.assertNotIn("otros", [a["params"]["seccion"]
                                   for a in mux_plan.plan(p)["avisos"]])

    def test_elegir_algo_quita_el_aviso_de_su_seccion(self):
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD, B_SUB_ES), (0, 0))
        p.adjuntos = [{"fuente": 0, "id": 1}]
        self.assertEqual(mux_plan.plan(p)["avisos"], [])

    def test_sin_ningun_video_no_se_crea(self):
        a = fuente("remux_p7_fel_23976.mkv.json", NOMBRE_A)
        b = fuente("webdl_dcp_24.mkv.json", NOMBRE_B)
        for f in (a, b):
            f.pistas = [x for x in f.pistas if x.tipo != "video"]
        with self.assertRaises(RuntimeError):
            mux_pipeline.proyecto_nuevo("x", a, b)


class TestCuandoHaceFaltaLaMedida(unittest.TestCase):

    def test_todo_de_un_mkv_no_la_necesita(self):
        for v, audio, sub in ((A_VIDEO, A_TRUEHD, A_SUB_SRT_ES), (B_VIDEO, B_DCP, B_SUB_ES)):
            with self.subTest(fuente=v[0]):
                p = capitulos(elegir(proyecto(), v, audio, sub), (v[0], 1))
                plan = mux_plan.plan(p)
                self.assertFalse(plan["necesita_sincronia"])
                self.assertTrue(plan["puede_multiplexar"], plan["motivos"])
                self.assertEqual(plan["sync"], {})

    def test_los_adjuntos_del_otro_no_tienen_tiempo(self):
        p = elegir(proyecto(), A_VIDEO, A_TRUEHD)
        p.adjuntos = [{"fuente": 0, "id": 1}]
        p.fuentes[1].adjuntos = [MuxAdjunto(id=7, nombre="font.ttf", mime="font/ttf", tamano=10)]
        p.adjuntos.append({"fuente": 1, "id": 7})
        self.assertFalse(mux_plan.necesita_sincronia(p))

    def test_algo_con_tiempo_del_otro_si_la_necesita(self):
        for extra in (B_DCP, B_SUB_ES):
            with self.subTest(extra=extra):
                p = elegir(proyecto(), A_VIDEO, A_TRUEHD, extra)
                self.assertTrue(mux_plan.necesita_sincronia(p))
                self.assertIn("sin_medida", [m["id"] for m in mux_plan.plan(p)["motivos"]])
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD), (1, 1))
        self.assertTrue(mux_plan.necesita_sincronia(p))

    def test_un_capitulo_que_no_existe_no_cuenta(self):
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD), (1, 99))
        self.assertFalse(mux_plan.necesita_sincronia(p))

    def test_midiendo_espera_y_aceptar_el_riesgo_no_lo_salta(self):
        p = elegir(proyecto(), A_VIDEO, B_DCP)
        p.sincronia = MuxSincronia(estado="midiendo")
        self.assertIn("midiendo", [m["id"] for m in mux_plan.plan(p)["motivos"]])
        p.acepta_riesgo = True
        self.assertFalse(mux_plan.plan(p)["puede_multiplexar"])

    def test_una_medida_de_otros_ficheros_no_vale(self):
        p = elegir(proyecto(), A_VIDEO, B_DCP)
        p.sincronia = medida(p, mux_sync.EN_SINCRONIA, Fraction(1), 0)
        p.fuentes[1].huella = {"sha256_1mb": "otro", "size_bytes": 1}
        self.assertIn("medida_vieja", [m["id"] for m in mux_plan.plan(p)["motivos"]])


class TestLaLineaDeTiempo(unittest.TestCase):
    """Los cuatro casos de la regla, con el vídeo de cada lado."""

    def test_el_caso_real_ajusta_la_velocidad_del_video_de_a(self):
        p = elegir(proyecto(), A_VIDEO, B_DCP, B_SUB_ES)
        p.sincronia = medida(p)
        plan = mux_plan.plan(p)
        self.assertTrue(plan["puede_multiplexar"], plan["motivos"])
        self.assertEqual((plan["video_de"], plan["linea_de"]), (0, 1))
        self.assertAlmostEqual(float(Fraction(*plan["velocidad_video"]["a"])), 23.999868, places=5)
        self.assertEqual(Fraction(*plan["velocidad_video"]["de"]), Fraction(24000, 1001))
        # β = 3 ms es ruido: lo de B no se mueve.
        self.assertEqual(plan["sync"], {})
        cmd = mux_plan.argv(p, plan, "/mnt/output/x.mkv.tmp")
        self.assertEqual(valor(cmd, "--default-duration"), ["0:41666896ns"])
        self.assertNotIn("--fix-bitstream-timing-information", cmd)

    def test_al_reves_el_video_de_b_va_mas_lento(self):
        """Toy Story con los papeles cambiados: el vídeo del WEB-DL (24 fps)
        con el audio del remux. El vídeo pasa a 24·α ≈ 23,976 fps."""
        p = elegir(proyecto(), B_VIDEO, A_TRUEHD, A_SPA)
        p.sincronia = medida(p)
        plan = mux_plan.plan(p)
        self.assertTrue(plan["puede_multiplexar"], plan["motivos"])
        self.assertEqual((plan["video_de"], plan["linea_de"]), (1, 0))
        self.assertEqual(Fraction(*plan["velocidad_video"]["a"]), 24 * ALFA_REAL)
        self.assertAlmostEqual(float(24 * ALFA_REAL), 23.976156, places=5)
        cmd = mux_plan.argv(p, plan, "/x.tmp")
        self.assertEqual(valor(cmd, "--default-duration"), ["0:41708104ns"])

    def test_con_la_cadencia_exacta_se_escribe_la_estandar(self):
        p = elegir(proyecto(), A_VIDEO, B_DCP)
        p.sincronia = medida(p, alfa=Fraction(1000, 1001), beta_ms=0)
        self.assertEqual(valor(mux_plan.argv(p, mux_plan.plan(p), "/x"), "--default-duration"),
                         ["0:24p"])
        p = elegir(proyecto(), B_VIDEO, A_TRUEHD)
        p.sincronia = medida(p, alfa=Fraction(1000, 1001), beta_ms=0)
        self.assertEqual(valor(mux_plan.argv(p, mux_plan.plan(p), "/x"), "--default-duration"),
                         ["0:24000/1001p"])

    def test_el_video_nunca_lleva_sync(self):
        """Con el vídeo en la lista de pistas, un `--sync` con factor encima
        del `--default-duration` lo estiraría dos veces (revisión de Codex del
        plan, 2026-10-27)."""
        for sel in ((A_VIDEO, B_DCP, A_SUB_SRT_ES), (B_VIDEO, A_TRUEHD, B_SUB_ES)):
            with self.subTest(sel=sel):
                p = elegir(proyecto(), *sel)
                p.sincronia = medida(p, alfa=Fraction(1000, 1001), beta_ms=2000)
                plan = mux_plan.plan(p)
                v = plan["video_de"]
                self.assertNotIn(f"{v}:0", plan["sync"])
                self.assertTrue(plan["sync"])        # algo se mueve, y no es el vídeo
                cmd = mux_plan.argv(p, plan, "/x")
                self.assertFalse([s for s in valor(tramo_de(cmd, p.fuentes[v].ruta), "--sync")
                                  if s.startswith("0:")])

    def test_con_la_velocidad_ajustada_lo_del_mkv_del_video_se_estira(self):
        p = elegir(proyecto(), A_VIDEO, A_SUB_SRT_ES, B_DCP)
        p.sincronia = medida(p, alfa=Fraction(1000, 1001), beta_ms=2000)
        plan = mux_plan.plan(p)
        self.assertEqual(plan["sync"]["0:22"], {"retraso_ms": 0, "factor": [1000, 1001]})
        self.assertEqual(plan["sync"]["1:1"], {"retraso_ms": -2000, "factor": None})

    def test_el_audio_del_mkv_del_video_manda_y_lo_otro_se_lleva_a_su_linea(self):
        # Vídeo y audio de A, subtítulos de B: t = (t_B − β)/α.
        p = elegir(proyecto(), A_VIDEO, A_TRUEHD, B_SUB_ES)
        p.sincronia = medida(p, alfa=Fraction(1000, 1001), beta_ms=2000)
        plan = mux_plan.plan(p)
        self.assertIsNone(plan["velocidad_video"])
        self.assertEqual(plan["sync"], {"1:5": {"retraso_ms": -2002, "factor": [1001, 1000]}})
        # Vídeo y audio de B, subtítulos de A: t = α·t_A + β.
        p = elegir(proyecto(), B_VIDEO, B_DCP, A_SUB_SRT_ES)
        p.sincronia = medida(p, alfa=Fraction(1000, 1001), beta_ms=2000)
        plan = mux_plan.plan(p)
        self.assertIsNone(plan["velocidad_video"])
        self.assertEqual(plan["sync"], {"0:22": {"retraso_ms": 2000, "factor": [1000, 1001]}})

    def test_misma_velocidad_con_un_logo_mas_largo(self):
        p = elegir(proyecto(), A_VIDEO, B_DCP, B_SUB_ES)
        p.sincronia = medida(p, mux_sync.RETRASO, Fraction(1), 2350)
        plan = mux_plan.plan(p)
        self.assertEqual({k: v["retraso_ms"] for k, v in plan["sync"].items()},
                         {"1:1": -2350, "1:5": -2350})
        # Al revés, lo que se mueve es lo de A y con el signo contrario.
        p = elegir(proyecto(), B_VIDEO, A_TRUEHD, A_SUB_SRT_ES)
        p.sincronia = medida(p, mux_sync.RETRASO, Fraction(1), 2350)
        plan = mux_plan.plan(p)
        self.assertEqual({k: v["retraso_ms"] for k, v in plan["sync"].items()},
                         {"0:1": 2350, "0:22": 2350})
        self.assertIsNone(plan["velocidad_video"])

    def test_audio_de_los_dos_con_velocidades_distintas_no_se_multiplexa(self):
        for sel in ((A_VIDEO, A_TRUEHD, B_DCP), (B_VIDEO, A_TRUEHD, B_DCP)):
            with self.subTest(sel=sel):
                p = elegir(proyecto(), *sel)
                p.sincronia = medida(p, alfa=Fraction(1000, 1001))
                plan = mux_plan.plan(p)
                self.assertIn("audio_de_dos_ritmos", [m["id"] for m in plan["motivos"]])
                p.acepta_riesgo = True
                self.assertFalse(mux_plan.plan(p)["puede_multiplexar"])

    def test_con_la_misma_velocidad_el_audio_de_los_dos_si_vale(self):
        p = elegir(proyecto(), A_VIDEO, A_TRUEHD, B_DCP)
        p.sincronia = medida(p, mux_sync.EN_SINCRONIA, Fraction(1), 0)
        self.assertTrue(mux_plan.plan(p)["puede_multiplexar"])

    def test_un_video_sin_cadencia_fija_no_se_ajusta(self):
        """Ajustar la velocidad escribe UNA duración de frame para todo el
        vídeo. Solo estorba cuando hay que ajustarla, y es el vídeo ELEGIDO el
        que tiene que poder: con el de B, un VFR en A no importa."""
        casos = {
            "vfr": lambda f: setattr(f.video, "modo_cadencia", "VFR"),
            "sin_duracion": lambda f: [setattr(v, "default_duration_ns", 0)
                                       for v in f.pistas if v.tipo == "video"],
            "dos_duraciones": lambda f: f.pistas.insert(1, f.pistas[0].model_copy(
                update={"id": 99, "default_duration_ns": 41666667})),
        }
        for nombre, romper in casos.items():
            for v, audio in ((0, B_DCP), (1, A_TRUEHD)):
                with self.subTest(caso=nombre, video=v):
                    p = elegir(proyecto(), (v, 0), audio)
                    if nombre == "dos_duraciones":
                        romper(p.fuentes[v])
                        p = elegir(p, (v, 0), (v, 99), audio)
                    else:
                        romper(p.fuentes[v])
                    p.sincronia = medida(p, alfa=Fraction(1000, 1001))
                    plan = mux_plan.plan(p)
                    self.assertIn("video_sin_velocidad_fija", [m["id"] for m in plan["motivos"]])
                    p.acepta_riesgo = True
                    self.assertFalse(mux_plan.plan(p)["puede_multiplexar"])
                    # El otro MKV estropeado no importa.
                    p2 = elegir(proyecto(), (v, 0), audio)
                    romper(p2.fuentes[1 - v])
                    p2.sincronia = medida(p2, alfa=Fraction(1000, 1001))
                    self.assertNotIn("video_sin_velocidad_fija",
                                     [m["id"] for m in mux_plan.plan(p2)["motivos"]])

    def test_lo_que_no_cuadra_se_acepta_o_no_se_multiplexa(self):
        for veredicto, extra in ((mux_sync.MONTAJE_DISTINTO, {"salto_entre": [2797, 3652]}),
                                 (mux_sync.DERIVA, {"pendiente_ppm": 40.0}),
                                 (mux_sync.NO_CONCLUYENTE, {})):
            with self.subTest(veredicto=veredicto):
                p = elegir(proyecto(), A_VIDEO, B_DCP)
                p.sincronia = medida(p, veredicto, Fraction(1), 0, **extra)
                self.assertFalse(mux_plan.plan(p)["puede_multiplexar"])
                p.acepta_riesgo = True
                self.assertTrue(mux_plan.plan(p)["puede_multiplexar"])

    def test_en_marcha_cuenta_salvo_para_quien_lo_ejecuta(self):
        p = elegir(proyecto(), A_VIDEO, A_TRUEHD)
        p.status = "running"
        self.assertFalse(mux_plan.plan(p)["puede_multiplexar"])
        self.assertTrue(mux_plan.plan(p, ignorar_estado=True)["puede_multiplexar"])

    def test_video_de_los_dos_no_se_multiplexa(self):
        p = MuxProject(**{**proyecto().model_dump(), "pistas": [
            MuxPista(fuente=0, id=0), MuxPista(fuente=1, id=0), MuxPista(fuente=0, id=1)]})
        plan = mux_plan.plan(p)
        self.assertIn("video_de_las_dos", [m["id"] for m in plan["motivos"]])
        self.assertIsNone(plan["video_de"])


class TestElRetrasoAMano(unittest.TestCase):
    """Es «el retraso de B respecto a A» (positivo: lo de B suena más tarde),
    y se aplica tal cual: el umbral de ruido es para lo MEDIDO."""

    def _plan(self, v, audio, ms):
        p = elegir(proyecto(), v, audio)
        p.sincronia = medida(p, mux_sync.NO_CONCLUYENTE, Fraction(1), 0)
        p.acepta_riesgo = True
        p.retraso_manual_ms = ms
        return mux_plan.plan(p)

    def test_con_el_video_de_a_se_retrasa_lo_de_b(self):
        self.assertEqual(self._plan(A_VIDEO, B_DCP, 250)["sync"],
                         {"1:1": {"retraso_ms": 250, "factor": None}})

    def test_con_el_video_de_b_se_adelanta_lo_de_a(self):
        self.assertEqual(self._plan(B_VIDEO, A_TRUEHD, 250)["sync"],
                         {"0:1": {"retraso_ms": -250, "factor": None}})

    def test_por_debajo_de_diez_ms_tambien_se_aplica(self):
        self.assertEqual(self._plan(A_VIDEO, B_DCP, 9)["sync"]["1:1"]["retraso_ms"], 9)

    def test_lo_medido_por_debajo_de_diez_ms_es_ruido(self):
        p = elegir(proyecto(), A_VIDEO, B_DCP)
        p.sincronia = medida(p, mux_sync.RETRASO, Fraction(1), 9)
        self.assertEqual(mux_plan.plan(p)["sync"], {})


def _final_de(plan, fuente, tid, t):
    """Dónde acaba en el MKV final lo que en `fuente` está en `t` (s).

    El vídeo: el fotograma de `t` a la velocidad nueva (si la hay). Lo demás:
    `t·factor + retraso`, como aplica mkvmerge `--sync TID:d,o/p`."""
    if fuente == plan["video_de"] and tid == 0:
        vel = plan["velocidad_video"]
        return t * (Fraction(*vel["de"]) / Fraction(*vel["a"])) if vel else t
    s = plan["sync"].get(f"{fuente}:{tid}")
    if not s:
        return t
    f = Fraction(*s["factor"]) if s["factor"] else Fraction(1)
    return t * f + Fraction(s["retraso_ms"], 1000)


class TestTodoCaeEnSuSitio(unittest.TestCase):
    """La comprobación que vale por todas: un sonido que en A está en `T` y
    en B en `α·T + β` tiene que caer en el MKV final en el mismo instante que
    el fotograma que lo acompaña, en las dos direcciones y con cualquier
    reparto que el plan admita. Es lo que demuestra que las fórmulas de
    `_mapeo_v_a_o` (y su inversa) son las buenas."""

    ALFAS = (Fraction(1), Fraction(1000, 1001), Fraction(1001, 1000), Fraction(24, 25), ALFA_REAL)
    BETAS = (0, 2000, -1500, 37)
    T = Fraction(3000)            # el segundo 3000 de A

    def test_el_sonido_y_su_fotograma_coinciden(self):
        casos = 0
        for alfa in self.ALFAS:
            for beta in self.BETAS:
                for v in (0, 1):
                    o = 1 - v
                    audio_de_o = (o, 1)       # la primera pista de audio de cada uno
                    audio_de_v = (v, 1)
                    for audio, sub in ((audio_de_o, (v, 22 if v == 0 else 5)),
                                       (audio_de_v, (o, 22 if o == 0 else 5))):
                        with self.subTest(alfa=alfa, beta=beta, video=v, audio=audio[0]):
                            p = elegir(proyecto(), (v, 0), audio, sub)
                            veredicto = (mux_sync.OTRO_RITMO if alfa != 1 else
                                         mux_sync.RETRASO if beta else mux_sync.EN_SINCRONIA)
                            p.sincronia = medida(p, veredicto, alfa, beta)
                            plan = mux_plan.plan(p)
                            self.assertTrue(plan["puede_multiplexar"], plan["motivos"])
                            t_en = {0: self.T, 1: alfa * self.T + Fraction(beta, 1000)}
                            video = _final_de(plan, v, 0, t_en[v])
                            for f, tid in (audio, sub):
                                lo_otro = _final_de(plan, f, tid, t_en[f])
                                # 1 ms del redondeo del retraso, y 10 ms del
                                # umbral de ruido de la medida (β = 37 no lo es).
                                self.assertLessEqual(abs(float(lo_otro - video)), 0.0011,
                                                     (f, tid, float(video), float(lo_otro)))
                            casos += 1
        self.assertEqual(casos, len(self.ALFAS) * len(self.BETAS) * 4)

    def test_los_capitulos_siguen_el_mismo_mapeo(self):
        for v, audio in ((0, B_DCP), (1, A_TRUEHD)):
            with self.subTest(video=v):
                p = capitulos(elegir(proyecto(), (v, 0), audio), (0, 1), (1, 1))
                alfa, beta = Fraction(1000, 1001), 2000
                p.sincronia = medida(p, alfa=alfa, beta_ms=beta)
                plan = mux_plan.plan(p)
                t_a = Fraction(CAPS_A[1][0])                  # 600 s en A
                t_b = Fraction(CAPS_B[1][0])                  # 2000 s en B
                lista = {(x["fuente"], x["indice"]): x for x in plan["capitulos"]["lista"]}
                # El de A en 600 s: su sonido está en B en α·600 + β.
                for (f, i), t in (((0, 1), t_a), ((1, 1), t_b)):
                    m = plan["capitulos"]["mapeos"][str(f)]
                    fa = Fraction(*m["factor"]) if m["factor"] else Fraction(1)
                    esperado = t * fa + Fraction(m["retraso_ms"], 1000)
                    self.assertAlmostEqual(lista[(f, i)]["inicio_ns"] / 1e9, float(esperado), places=6)
                # y el de A cae donde su fotograma
                video_t = (t_a if v == 0 else alfa * t_a + Fraction(beta, 1000))
                self.assertAlmostEqual(lista[(0, 1)]["inicio_ns"] / 1e9,
                                       float(_final_de(plan, v, 0, video_t)), places=2)


class TestLosCapitulos(unittest.TestCase):

    def test_se_mezclan_los_de_los_dos_en_orden(self):
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD, B_SUB_ES), (1, 1), (0, 0), (0, 2))
        p.sincronia = medida(p, mux_sync.RETRASO, Fraction(1), 2000)
        lista = mux_plan.plan(p)["capitulos"]["lista"]
        self.assertEqual([(x["fuente"], x["indice"]) for x in lista], [(0, 0), (0, 2), (1, 1)])
        self.assertEqual(lista[2]["inicio_ns"], 1_998_000_000_000)

    def test_lo_que_cae_antes_del_cero_se_rescata_si_no_hay_otro(self):
        # El «Inicio» de B (0 s) con −2 s queda antes del cero: pasa al cero.
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD, B_SUB_ES), (1, 0), (1, 1))
        p.sincronia = medida(p, mux_sync.RETRASO, Fraction(1), 2000)
        lista = mux_plan.plan(p)["capitulos"]["lista"]
        self.assertEqual([(x["indice"], x["inicio_ns"], x["queda"]) for x in lista],
                         [(0, 0, "al_cero"), (1, 1_998_000_000_000, "ok")])
        # Con uno de A en el cero, el de B sobra.
        p = capitulos(p, (0, 0), (1, 0), (1, 1))
        lista = mux_plan.plan(p)["capitulos"]["lista"]
        queda = {(x["fuente"], x["indice"]): x["queda"] for x in lista}
        self.assertEqual(queda[(1, 0)], "fuera")
        self.assertEqual(queda[(0, 0)], "ok")

    def test_de_varios_antes_del_cero_se_rescata_el_ultimo(self):
        # Con 2001 s de retraso, «Inicio» (−2001 s) y «B 2» (−1 s) caen antes
        # del cero: el que abre la película es el último de ellos.
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD, B_SUB_ES), (1, 0), (1, 1), (1, 2))
        p.sincronia = medida(p, mux_sync.RETRASO, Fraction(1), 2_001_000)
        lista = mux_plan.plan(p)["capitulos"]["lista"]
        # Lo que queda fuera conserva su tiempo (negativo), así que va primero.
        self.assertEqual([(x["indice"], x["queda"]) for x in lista],
                         [(0, "fuera"), (1, "al_cero"), (2, "ok")])
        self.assertEqual(lista[1]["inicio_ns"], 0)

    def test_lo_que_pasa_del_final_se_quita(self):
        # «B final» en 6100 s pasa del vídeo de A (146545 frames a 23,976 =
        # 6112 s)… pero no del de A acortado a la velocidad de B.
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD, B_SUB_ES), (1, 2))
        p.sincronia = medida(p, mux_sync.RETRASO, Fraction(1), -20_000)
        self.assertEqual(mux_plan.plan(p)["capitulos"]["lista"][0]["queda"], "fuera")

    def test_sin_frames_la_duracion_tambien_se_escala(self):
        p = elegir(proyecto(), A_VIDEO, B_DCP)
        p.sincronia = medida(p, alfa=Fraction(1000, 1001), beta_ms=0)
        plan = mux_plan.plan(p)
        con = mux_plan.duracion_final(p, plan)
        p.fuentes[0].video.frames = 0
        sin = mux_plan.duracion_final(p, plan)
        self.assertAlmostEqual(con, 146545 / 24, places=3)
        self.assertAlmostEqual(sin, p.fuentes[0].duracion_s * 1000 / 1001, places=3)

    def test_todos_los_de_una_fuente_sin_mover_van_tal_cual(self):
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD), *[(0, i) for i in range(8)])
        plan = mux_plan.plan(p)
        self.assertEqual(mux_plan.capitulos_nativos(p, plan), 0)
        cmd = mux_plan.argv(p, plan, "/x")
        self.assertNotIn("--no-chapters", tramo_de(cmd, p.fuentes[0].ruta))
        self.assertNotIn("--chapters", cmd)
        # Uno menos, o la fuente estirada: se componen.
        self.assertIsNone(mux_plan.capitulos_nativos(capitulos(p, (0, 0)), plan))
        p = capitulos(elegir(proyecto(), A_VIDEO, B_DCP), *[(0, i) for i in range(8)])
        p.sincronia = medida(p, alfa=Fraction(1000, 1001), beta_ms=0)
        self.assertIsNone(mux_plan.capitulos_nativos(p, mux_plan.plan(p)))

    def test_tal_cual_no_si_alguno_cae_fuera_del_video(self):
        """Los capítulos de A con el vídeo de B, más corto: el último cae
        detrás del final, y copiarlos tal cual lo metía igual — y la Fase C lo
        esperaba (revisión de Codex del 2026-10-27)."""
        p = capitulos(elegir(proyecto(), B_VIDEO, B_DCP), *[(0, i) for i in range(8)])
        p.fuentes[0].capitulos[7].inicio_ns = 6_200_000_000_000
        p.sincronia = medida(p, mux_sync.EN_SINCRONIA, Fraction(1), 0)
        plan = mux_plan.plan(p)
        self.assertEqual(plan["capitulos"]["lista"][-1]["queda"], "fuera")
        self.assertIsNone(mux_plan.capitulos_nativos(p, plan))
        self.assertEqual(len(mux_plan.capitulos_esperados(p, plan)), 7)

    XML = """<?xml version="1.0"?>
<Chapters><EditionEntry><EditionUID>11</EditionUID>
<ChapterAtom><ChapterUID>1</ChapterUID><ChapterTimeStart>00:00:00.000000000</ChapterTimeStart>
<ChapterDisplay><ChapterString>Inicio</ChapterString><ChapterLanguage>spa</ChapterLanguage></ChapterDisplay>
<ChapterDisplay><ChapterString>Start</ChapterString><ChapterLanguage>eng</ChapterLanguage></ChapterDisplay></ChapterAtom>
<ChapterAtom><ChapterUID>2</ChapterUID><ChapterTimeStart>00:33:20.000000000</ChapterTimeStart>
<ChapterTimeEnd>00:33:21.000000000</ChapterTimeEnd>
<ChapterDisplay><ChapterString>B 2</ChapterString></ChapterDisplay>
<ChapterAtom><ChapterUID>21</ChapterUID><ChapterTimeStart>00:33:20.500000000</ChapterTimeStart>
<ChapterDisplay><ChapterString>Dentro</ChapterString></ChapterDisplay>
<ChapterAtom><ChapterTimeStart>00:33:20.700000000</ChapterTimeStart>
<ChapterDisplay><ChapterString>Nieto</ChapterString></ChapterDisplay></ChapterAtom>
<ChapterAtom><ChapterTimeStart>03:00:00.000000000</ChapterTimeStart>
<ChapterDisplay><ChapterString>Nieto fuera</ChapterString></ChapterDisplay></ChapterAtom></ChapterAtom>
<ChapterAtom><ChapterUID>22</ChapterUID><ChapterTimeStart>02:00:00.000000000</ChapterTimeStart>
<ChapterDisplay><ChapterString>Fuera</ChapterString></ChapterDisplay></ChapterAtom></ChapterAtom>
<ChapterAtom><ChapterUID>1</ChapterUID><ChapterTimeStart>01:41:40.000000000</ChapterTimeStart>
<ChapterDisplay><ChapterString>B final</ChapterString></ChapterDisplay></ChapterAtom>
</EditionEntry></Chapters>"""

    def test_el_xml_se_lee_con_sus_anidados_y_sus_idiomas(self):
        caps = mux_plan.capitulos_de_xml(self.XML)
        self.assertEqual([(c["indice"], c["inicio_ns"], c["nombre"], c["hijos"]) for c in caps],
                         [(0, 0, "Inicio", 0), (1, 2_000_000_000_000, "B 2", 4),
                          (2, 6_100_000_000_000, "B final", 0)])
        self.assertEqual(caps[0]["idioma"], "spa")

    def test_al_componer_se_quitan_los_uid_y_se_recortan_los_anidados(self):
        """Un UID repetido hace que mkvmerge 88 se coma un capítulo sin avisar
        (medido en el NAS, 2026-10-27): aquí el de «B final» repite el 1."""
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD, B_SUB_ES), (1, 0), (1, 1), (1, 2), (0, 3))
        p.sincronia = medida(p, mux_sync.RETRASO, Fraction(1), 2000)
        # A aporta su capítulo 3, que en su XML es el cuarto de primer nivel.
        p.fuentes[0].capitulos[3].inicio_ns = 2_000_000_000_000
        plan = mux_plan.plan(p)
        xml_a = ("<Chapters><EditionEntry>" + "".join(
            f"<ChapterAtom><ChapterUID>{i + 1}</ChapterUID><ChapterTimeStart>{t}</ChapterTimeStart>"
            f"<ChapterDisplay><ChapterString>{n}</ChapterString></ChapterDisplay></ChapterAtom>"
            for i, (t, n) in enumerate((("00:00:00.000000000", "Logo"), ("00:10:00.000000000", "A 2"),
                                        ("00:20:00.000000000", "A 3"), ("00:33:20.000000000", "A 4"))))
            + "</EditionEntry></Chapters>")
        compuesto, esperados = mux_plan.componer_capitulos_xml(
            {0: xml_a, 1: self.XML}, p, plan, mux_plan.duracion_final(p, plan))
        raiz = ET.fromstring(compuesto)
        self.assertEqual(len(raiz.findall("EditionEntry")), 1)
        for uid in ("ChapterUID", "EditionUID"):
            self.assertEqual(list(raiz.iter(uid)), [])
        atomos = raiz.find("EditionEntry").findall("ChapterAtom")
        nombres = [a.find("ChapterDisplay").findtext("ChapterString") for a in atomos]
        # «Inicio» de B (−2 s) pasa al cero; «B 2» (2000 − 2 = 1998 s) va
        # antes que «A 4», que no se mueve (2000 s).
        self.assertEqual(nombres, ["Inicio", "B 2", "A 4", "B final"])
        self.assertEqual(esperados, [0, 1_998_000_000_000, 2_000_000_000_000, 6_098_000_000_000])
        # Los dos idiomas del primero siguen ahí.
        self.assertEqual(len(atomos[0].findall("ChapterDisplay")), 2)
        # El anidado de dentro se mueve igual; el que pasa del final se quita.
        b2 = atomos[1]
        hijos = b2.findall("ChapterAtom")
        self.assertEqual([h.find("ChapterDisplay").findtext("ChapterString") for h in hijos], ["Dentro"])
        self.assertEqual(hijos[0].findtext("ChapterTimeStart"), "00:33:18.500000000")
        self.assertEqual(b2.findtext("ChapterTimeEnd"), "00:33:19.000000000")
        # Y lo mismo un nivel más abajo (revisión de Codex: el recorte era
        # solo del primer nivel).
        nietos = hijos[0].findall("ChapterAtom")
        self.assertEqual([(n.find("ChapterDisplay").findtext("ChapterString"),
                           n.findtext("ChapterTimeStart")) for n in nietos],
                         [("Nieto", "00:33:18.700000000")])
        self.assertNotIn("Nieto fuera", compuesto)

    def test_al_componer_se_conserva_la_marca_de_oculto(self):
        """Un capítulo oculto elegido sigue oculto: se copia de su XML con lo
        que lleva, y solo se tocan los tiempos y los UID."""
        xml = self.XML.replace("<ChapterUID>2</ChapterUID>",
                               "<ChapterUID>2</ChapterUID><ChapterFlagHidden>1</ChapterFlagHidden>")
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD), (1, 1))
        p.sincronia = medida(p, mux_sync.EN_SINCRONIA, Fraction(1), 0)
        plan = mux_plan.plan(p)
        compuesto, _ = mux_plan.componer_capitulos_xml({1: xml}, p, plan, mux_plan.duracion_final(p, plan))
        atomo = ET.fromstring(compuesto).find("EditionEntry").find("ChapterAtom")
        self.assertEqual(atomo.findtext("ChapterFlagHidden"), "1")

    def test_un_capitulo_que_ya_no_esta_donde_se_leyo_para_todo(self):
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD), (1, 1))
        p.sincronia = medida(p, mux_sync.EN_SINCRONIA, Fraction(1), 0)
        plan = mux_plan.plan(p)
        with self.assertRaises(RuntimeError):
            mux_plan.componer_capitulos_xml({1: self.XML.replace("00:33:20.000", "00:33:25.000")},
                                            p, plan, 6000)


class TestLaSeleccion(unittest.TestCase):

    def test_normalizar_conserva_el_nombre_y_las_marcas(self):
        p = proyecto()
        out = mux_plan.normalizar_pistas(p, [
            MuxPista(fuente=1, id=5, nombre="Signs", por_defecto=False, forzada=True),
            MuxPista(fuente=0, id=0, nombre="Vídeo", por_defecto=True, forzada=False)])
        self.assertEqual([(q.fuente, q.id, q.nombre, q.por_defecto, q.forzada) for q in out],
                         [(0, 0, "Vídeo", True, False), (1, 5, "Signs", False, True)])

    def test_normalizar_ordena_por_tipo_y_deja_una_por_defecto(self):
        p = proyecto()
        pistas = [MuxPista(fuente=1, id=5, por_defecto=True), MuxPista(fuente=0, id=1, por_defecto=True),
                  MuxPista(fuente=1, id=1, por_defecto=True), MuxPista(fuente=0, id=0),
                  MuxPista(fuente=0, id=1), MuxPista(fuente=1, id=6, por_defecto=True),
                  MuxPista(fuente=1, id=77)]
        out = mux_plan.normalizar_pistas(p, pistas)
        self.assertEqual([(x.fuente, x.id) for x in out], [(0, 0), (0, 1), (1, 1), (1, 5), (1, 6)])
        self.assertEqual([x.por_defecto for x in out], [False, True, False, True, False])

    def test_todo_de_un_lado_en_el_video_cambia_el_video(self):
        p = elegir(proyecto(), A_VIDEO, A_TRUEHD)
        p.pistas[0].nombre = "mi vídeo"
        out = mux_plan.aplicar_patron(p, "video", "todo_b")["pistas"]
        self.assertEqual([(x.fuente, x.id) for x in out], [(1, 0), (0, 1)])
        # Volver al de A no recuerda el nombre: es otra pista.
        p.pistas = out
        self.assertEqual(mux_plan.aplicar_patron(p, "video", "todo_a")["pistas"][0].nombre,
                         "HEVC DV P7 FEL CMv4.0")

    def test_todo_y_nada_tocan_solo_su_seccion_y_su_lado(self):
        p = elegir(proyecto(), A_VIDEO, A_TRUEHD, B_SUB_ES)
        p.pistas[1].nombre = "Inglés renombrado"
        out = mux_plan.aplicar_patron(p, "audio", "todo_b")["pistas"]
        self.assertEqual([(x.fuente, x.id) for x in out],
                         [A_VIDEO, A_TRUEHD, B_DCP, B_DDP_ES, B_CAT, B_ENG, B_SUB_ES])
        self.assertEqual(out[1].nombre, "Inglés renombrado")
        p.pistas = out
        out = mux_plan.aplicar_patron(p, "audio", "nada_a")["pistas"]
        self.assertEqual([(x.fuente, x.id) for x in out],
                         [A_VIDEO, B_DCP, B_DDP_ES, B_CAT, B_ENG, B_SUB_ES])

    def test_mis_idiomas_de_los_dos_sin_comentarios(self):
        p = elegir(proyecto(), A_VIDEO)
        idiomas = mux_plan.idiomas_del_usuario("es", "en")
        out = mux_plan.aplicar_patron(p, "audio", "mis_idiomas", idiomas)["pistas"]
        self.assertEqual([(x.fuente, x.id) for x in out[1:]],
                         [A_TRUEHD, (0, 2), A_SPA, B_DCP, B_DDP_ES, B_ENG])
        self.assertNotIn(A_COMENTARIO, [(x.fuente, x.id) for x in out])

    def test_mis_idiomas_en_catalan_incluye_el_catalan_y_el_castellano(self):
        idiomas = mux_plan.idiomas_del_usuario("ca", "en")
        for c in ("cat", "ca", "spa", "es", "eng", "en"):
            self.assertIn(c, idiomas)
        self.assertNotIn("fre", idiomas)

    def test_la_vo_sale_de_tmdb(self):
        idiomas = mux_plan.idiomas_del_usuario("es", "fr")
        self.assertIn("fre", idiomas)
        self.assertIn("fra", idiomas)

    def test_solo_forzados(self):
        p = proyecto()
        p.fuentes[1].pistas[5].forzada = True                 # B_SUB_ES
        p.fuentes[0].pistas[19].nombre = "English Forced"     # id 19, SRT inglés
        out = mux_plan.aplicar_patron(elegir(p, A_VIDEO), "subtitulos", "solo_forzados")["pistas"]
        self.assertEqual([(x.fuente, x.id) for x in out], [A_VIDEO, (0, 19), B_SUB_ES])

    def test_las_fuentes_de_los_ass_elegidos(self):
        p = proyecto()
        p.fuentes[1].pistas.append(MuxPistaOrigen(id=7, tipo="subtitles", codec="SubStationAlpha",
                                                  codec_id="S_TEXT/ASS", idioma="spa"))
        p.fuentes[1].adjuntos = [MuxAdjunto(id=1, nombre="Arial.ttf", mime="font/ttf", tamano=10),
                                 MuxAdjunto(id=2, nombre="cover.jpg", mime="image/jpeg", tamano=10)]
        sin_ass = mux_plan.aplicar_patron(elegir(p, A_VIDEO, B_SUB_ES), "otros", "fuentes_de_los_ass")
        self.assertEqual(sin_ass["adjuntos"], [])
        con = mux_plan.aplicar_patron(elegir(p, A_VIDEO, (1, 7)), "otros", "fuentes_de_los_ass")
        self.assertEqual(con["adjuntos"], [{"fuente": 1, "id": 1}])

    def test_los_capitulos_por_lado(self):
        p = proyecto()
        out = mux_plan.aplicar_patron(p, "capitulos", "todo_b")["capitulos"]
        self.assertEqual([(c.fuente, c.indice) for c in out], [(1, 0), (1, 1), (1, 2)])
        p.capitulos = out + [MuxCapitulo(fuente=0, indice=3)]
        out = mux_plan.aplicar_patron(p, "capitulos", "nada_b")["capitulos"]
        self.assertEqual([(c.fuente, c.indice) for c in out], [(0, 3)])

    def test_un_patron_que_no_existe(self):
        with self.assertRaises(ValueError):
            mux_plan.aplicar_patron(proyecto(), "video", "mis_idiomas")
        with self.assertRaises(ValueError):
            mux_plan.aplicar_patron(proyecto(), "cosas", "todo_a")


class TestElNombre(unittest.TestCase):

    def test_con_el_video_de_a_y_el_audio_dcp_de_b(self):
        p = elegir(proyecto(), A_VIDEO, B_DCP)
        self.assertEqual(mux_plan.nombre_por_defecto(p),
                         "Toy Story 5 (2026) [DV FEL] [Audio DCP] [CMv4 FULL].mkv")

    def test_los_tags_salen_de_lo_elegido_no_de_los_ficheros(self):
        # El vídeo de B no lleva Dolby Vision, y el audio de A no es el DCP.
        self.assertEqual(mux_plan.nombre_por_defecto(elegir(proyecto(), B_VIDEO, A_TRUEHD)),
                         "Toy Story 5 (2026).mkv")
        # Nada elegido: ni FEL ni DCP.
        self.assertEqual(mux_plan.nombre_por_defecto(proyecto()), "Toy Story 5 (2026).mkv")

    def test_la_ficha_de_tmdb_manda_y_se_sanea(self):
        p = elegir(proyecto(), A_VIDEO, A_TRUEHD)
        self.assertEqual(mux_plan.nombre_por_defecto(p, "Cara a cara/Face Off", "1997"),
                         "Cara a cara-Face Off (1997) [DV FEL] [CMv4 FULL].mkv")


class TestElArgv(unittest.TestCase):

    def test_solo_entran_las_fuentes_que_aportan(self):
        p = elegir(proyecto(), B_VIDEO, B_DCP, B_SUB_ES)
        plan = mux_plan.plan(p)
        cmd = mux_plan.argv(p, plan, "/x")
        self.assertNotIn(p.fuentes[0].ruta, cmd)
        # El puesto en la lista de entradas, no el índice de la fuente.
        self.assertEqual(valor(cmd, "--track-order"), ["0:0,0:1,0:5"])

    def test_dos_entradas_y_el_orden_del_proyecto(self):
        p = elegir(proyecto(), B_VIDEO, A_TRUEHD, B_SUB_ES, A_SUB_SRT_ES)
        p.sincronia = medida(p, mux_sync.EN_SINCRONIA, Fraction(1), 0)
        plan = mux_plan.plan(p)
        cmd = mux_plan.argv(p, plan, "/out.mkv.tmp")
        self.assertEqual(cmd[:4], ["mkvmerge", "--gui-mode", "-o", "/out.mkv.tmp"])
        de_a, de_b = tramo_de(cmd, p.fuentes[0].ruta), tramo_de(cmd, p.fuentes[1].ruta)
        self.assertIn("--no-video", de_a)
        self.assertEqual(valor(de_a, "--audio-tracks"), ["1"])
        self.assertEqual(valor(de_a, "--subtitle-tracks"), ["22"])
        self.assertEqual(valor(de_b, "--video-tracks"), ["0"])
        self.assertIn("--no-audio", de_b)
        # Las etiquetas globales, solo del MKV del vídeo.
        self.assertIn("--no-global-tags", de_a)
        self.assertNotIn("--no-global-tags", de_b)
        self.assertEqual(valor(cmd, "--track-order"), ["1:0,0:1,1:5,0:22"])

    def test_cada_pista_lleva_su_nombre_y_sus_marcas_tambien_el_video(self):
        p = elegir(proyecto(), A_VIDEO, B_DCP, B_SUB_ES)
        p.pistas[0].nombre = "Vídeo HDR"
        p.pistas[0].por_defecto = True
        p.pistas[1].nombre = "Castellano TrueHD Atmos 7.1 (DCP 9.1.6)"
        p.pistas[2].forzada = True
        p.sincronia = medida(p)
        cmd = mux_plan.argv(p, mux_plan.plan(p), "/x.tmp")
        self.assertIn("0:Vídeo HDR", valor(cmd, "--track-name"))
        self.assertIn("1:Castellano TrueHD Atmos 7.1 (DCP 9.1.6)", valor(cmd, "--track-name"))
        self.assertIn("5:yes", valor(cmd, "--forced-track"))
        self.assertEqual(valor(tramo_de(cmd, p.fuentes[0].ruta), "--default-track"), ["0:yes"])

    def test_los_capitulos_compuestos_van_aparte(self):
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD, B_SUB_ES), (0, 0), (1, 1))
        p.sincronia = medida(p, mux_sync.EN_SINCRONIA, Fraction(1), 0)
        con_xml = mux_plan.argv(p, mux_plan.plan(p), "/x.tmp", "/caps.xml")
        self.assertEqual(valor(con_xml, "--chapters"), ["/caps.xml"])
        self.assertEqual(con_xml.count("--no-chapters"), 2)

    def test_un_adjunto_elegido_se_pide_y_los_demas_no(self):
        p = elegir(proyecto(), A_VIDEO, A_TRUEHD)
        p.adjuntos = [{"fuente": 0, "id": 1}]
        cmd = mux_plan.argv(p, mux_plan.plan(p), "/x.tmp")
        self.assertEqual(valor(cmd, "--attachments"), ["1"])
        self.assertNotIn("--no-attachments", cmd)

    def test_los_subtitulos_con_factor_lo_llevan_en_su_sync(self):
        """El vídeo y el audio de B con los subtítulos de A, a velocidades
        distintas: el subtítulo se lleva a la línea de B CON el factor, o a la
        hora llegaría 3,6 s tarde (revisión de Codex del 2026-10-27)."""
        p = elegir(proyecto(), B_VIDEO, B_DCP, A_SUB_SRT_ES)
        p.sincronia = medida(p, alfa=Fraction(1000, 1001), beta_ms=0)
        plan = mux_plan.plan(p)
        s = plan["sync"]["0:22"]
        self.assertTrue(s.get("factor"), s)
        cmd = mux_plan.argv(p, plan, "/x.tmp")
        self.assertEqual(valor(tramo_de(cmd, p.fuentes[0].ruta), "--sync"),
                         [f"22:{int(s['retraso_ms'])},{s['factor'][0]}/{s['factor'][1]}"])

    def test_se_pide_el_adjunto_elegido_y_no_otro(self):
        p = elegir(proyecto(), A_VIDEO, A_TRUEHD)
        p.fuentes[0].adjuntos.append(MuxAdjunto(id=2, nombre="font.ttf", mime="font/ttf", tamano=10))
        p.adjuntos = [{"fuente": 0, "id": 2}]
        self.assertEqual(valor(mux_plan.argv(p, mux_plan.plan(p), "/x.tmp"), "--attachments"), ["2"])

    def test_el_titulo_editado_va_al_contenedor(self):
        p = elegir(proyecto(), A_VIDEO, A_TRUEHD)
        p.nombre_salida = "Peli.mkv"
        p.titulo = "  Edición doblada  "
        self.assertEqual(valor(mux_plan.argv(p, mux_plan.plan(p), "/x.tmp"), "--title"),
                         ["Edición doblada"])
        p.titulo = ""
        self.assertEqual(valor(mux_plan.argv(p, mux_plan.plan(p), "/x.tmp"), "--title"), ["Peli"])

    def test_una_fuente_que_solo_aporta_capitulos_no_entra_con_el_xml(self):
        p = capitulos(elegir(proyecto(), A_VIDEO, A_TRUEHD), (1, 1))
        p.sincronia = medida(p, mux_sync.EN_SINCRONIA, Fraction(1), 0)
        plan = mux_plan.plan(p)
        self.assertEqual(mux_plan.entradas(p, plan, con_xml=True), [0])
        self.assertEqual(mux_plan.entradas(p, plan, con_xml=False), [0, 1])


class TestTamanoYComprobacion(unittest.TestCase):

    def test_el_tamano_es_la_suma_de_lo_elegido_video_y_adjuntos_incluidos(self):
        p = elegir(proyecto(), B_VIDEO, A_TRUEHD, B_SUB_ES)
        p.adjuntos = [{"fuente": 0, "id": 1}]
        esperado = (18276543581 + 3187362308 + 43119 + 97192)
        self.assertEqual(mux_plan.tamano_estimado(p), int(esperado * 1.005))

    def test_sin_nada_o_con_una_pista_sin_tamano_no_hay_cifra(self):
        self.assertIsNone(mux_plan.tamano_estimado(proyecto()))
        p = elegir(proyecto(), A_VIDEO, B_DCP)
        p.fuentes[1].pistas[1].bytes = 0
        self.assertIsNone(mux_plan.tamano_estimado(p))

    def _salida(self, esperadas):
        return {"tracks": [{"type": e["tipo"], "codec": e["codec"], "properties": {
            "language": e["idioma"], "track_name": e["nombre"],
            "default_track": e["por_defecto"], "forced_track": e["forzada"],
            "default_duration": e.get("default_duration_ns"),
            "tag_number_of_frames": str(e.get("frames") or "")}} for e in esperadas]}

    def test_el_video_se_comprueba_entero_con_la_velocidad_nueva(self):
        p = elegir(proyecto(), A_VIDEO, B_DCP, B_SUB_ES)
        p.sincronia = medida(p)
        plan = mux_plan.plan(p)
        esperadas = mux_plan.pistas_esperadas(p, plan)
        self.assertEqual(esperadas[0]["default_duration_ns"], 41666896)
        j = self._salida(esperadas)
        self.assertTrue(all(c["ok"] for c in mux_plan.comprobar_pistas(esperadas, j)))
        j["tracks"][0]["properties"]["default_duration"] = 41708333
        j["tracks"][0]["properties"]["track_name"] = "otro"
        malos = sorted((c["id"], c["pista"]) for c in mux_plan.comprobar_pistas(esperadas, j)
                       if not c["ok"])
        self.assertEqual(malos, [("cadencia", 1), ("nombre", 1)])

    def test_una_pista_de_menos_es_un_fallo_por_si_sola(self):
        p = elegir(proyecto(), A_VIDEO, B_DCP, B_SUB_ES)
        esperadas = mux_plan.pistas_esperadas(p, mux_plan.plan(p))
        j = self._salida(esperadas)
        j["tracks"].pop()
        checks = mux_plan.comprobar_pistas(esperadas, j)
        self.assertEqual([(c["id"], c["ok"]) for c in checks], [("numero_de_pistas", False)])

    def test_las_estadisticas_ajenas_no_dan_frames(self):
        self.assertEqual(mux_pipeline._frames_fiables(
            {"tag_number_of_frames": "100", "tag__statistics_writing_app": "mkvmerge v88"}), 100)
        self.assertEqual(mux_pipeline._frames_fiables(
            {"tag_number_of_frames": "100", "tag__statistics_writing_app": "HandBrake"}), 0)

    def test_una_pista_rara_no_se_puede_elegir(self):
        pistas = mux_pipeline.pistas_de_j({"tracks": [
            {"id": 0, "type": "video", "codec": "HEVC", "properties": {}},
            {"id": 1, "type": "buttons", "codec": "HDMV IG", "properties": {}}]})
        self.assertEqual([x.tipo for x in pistas], ["video"])


if __name__ == "__main__":
    unittest.main()
