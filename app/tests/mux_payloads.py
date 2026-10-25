"""Proyectos de «Combinar MKV» de ejemplo, hechos por el backend (no es un test).

Los usan los tests que pintan la pestaña en Chrome: `test_las_tres_lenguas_en_pantalla`
(que la lee en las tres lenguas) y `test_tab4_combinar` (que ejecuta sus flujos).
Un solo constructor para los dos, por lo mismo que `frontend_sources`: dos copias
de la forma de un proyecto acaban describiendo dos proyectos distintos.
"""
import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]


def proyectos_mux() -> dict:
    """Los proyectos de «Combinar MKV» que pinta la sonda, hechos por el BACKEND.

    Las pistas son las reales de `fixtures_mux/` —el caso de Toy Story, un
    remux con 48 pistas y un WEB-DL con 7—, y el plan y el relato salen de
    `mux_plan` y `mux_relato` como al servir. Escribirlos a mano sería una
    segunda copia de la forma, y una fixture que no corresponde al modelo
    deja al guard vigilando el vacío.

    Lo que el usuario lee va en INGLÉS por lo mismo que el resto de fixtures:
    los nombres de fichero y de pista, la ficha y el RELATO, que el servidor
    escribe en el idioma de la app (se genera con `idioma_activo` en `en`).
    Así un castellano en la pantalla inglesa solo puede venir de la app.
    """
    cache = proyectos_mux.__dict__.setdefault("cache", {})
    if cache:
        return cache
    from datetime import datetime, timedelta, timezone
    from fractions import Fraction
    from unittest import mock
    sys.path.insert(0, str(APP_DIR))
    import i18n
    from models import Chapter, MuxAdjunto, MuxEjecucion, MuxFuente, MuxPunto, MuxSincronia
    from phases import mux_pipeline, mux_plan, mux_relato, mux_sync

    fix = APP_DIR / "tests" / "fixtures_mux"
    nombres_b = {1: "Spanish TrueHD Atmos 7.1-DCP", 2: "Spanish DD+ 7.1",
                 3: "Catalan DD+ 5.1", 4: "English DD+ 5.1",
                 5: "Spanish Full [Signs]", 6: "English Full (PGS)"}

    def fuente(fichero, nombre, *, dv, renombrar=None, caps=0):
        j = json.loads((fix / fichero).read_text(encoding="utf-8"))
        for t in j["tracks"]:
            if renombrar and t["id"] in renombrar:
                t["properties"]["track_name"] = renombrar[t["id"]]
        pistas = mux_pipeline.pistas_de_j(j)

        class _Dovi:
            profile, el_type, cm_version = dv or (0, "", "")
        mi = {"media": {"track": [{"@type": "Video", "FrameRate_Mode": "CFR",
                                   "HDR_Format": "Dolby Vision / SMPTE ST 2086"}]}}
        video = mux_pipeline._resumen_de_video(pistas, mi, _Dovi() if dv else None)
        capitulos = [Chapter(number=i + 1, timestamp=f"00:{i * 10:02d}:00.000",
                             name=f"Chapter {i + 1:02d}") for i in range(caps)]
        adjuntos = [MuxAdjunto(id=int(a["id"]), nombre=a.get("file_name") or "",
                               mime=a.get("content_type") or "", tamano=int(a.get("size") or 0))
                    for a in j.get("attachments") or []]
        return MuxFuente(ruta=f"/mnt/library/Movies/{nombre}", nombre=nombre,
                         tamano=78_400_000_000 if dv else 31_900_000_000,
                         huella={"sha256_1mb": nombre, "size_bytes": 1, "mtime_ns": 0},
                         duracion_s=j["container"]["properties"]["duration"] / 1e9,
                         video=video, pistas=pistas, capitulos=capitulos, adjuntos=adjuntos)

    def proyecto(pid):
        a = fuente("remux_p7_fel_23976.mkv.json",
                   "Blade.Runner.2049.2017.UHD.BluRay.REMUX.DV.P7.mkv",
                   dv=(7, "FEL", "v4.0"), caps=8)
        b = fuente("webdl_dcp_24.mkv.json",
                   "Blade Runner 2049 (2017) [WEB-DL 2160p DV TrueHD Atmos 7.1].mkv",
                   dv=None, renombrar=nombres_b, caps=12)
        p = mux_pipeline.proyecto_nuevo(pid, a, b)
        p.tmdb_info = {"title": "Blade Runner 2049", "original_title": "Blade Runner 2049",
                       "year": 2017, "runtime_minutes": 164, "genres": ["Science Fiction"],
                       "overview": "A young blade runner discovers a long-buried secret.",
                       "vote_average": 7.6, "vote_count": 13000, "poster_url": ""}
        return p

    def medida(p, veredicto, alfa=Fraction(1), beta=0, validos=None, salto=None):
        dur = p.fuentes[0].duracion_s
        puntos = []
        for i, f in enumerate((0.05, 0.19, 0.33, 0.47, 0.60, 0.74, 0.88)):
            t = dur * f
            d = (float(alfa) - 1) * t * 1000 + beta + (8000 if salto and t > salto[0] else 0)
            ok = validos is None or i in validos
            puntos.append(MuxPunto(t_a=round(t, 2), t_b=round(t + d / 1000, 3),
                                   desfase_ms=round(d, 1), r=0.4 + 0.05 * i,
                                   contraste=2.0 + 0.5 * i, valido=ok,
                                   error=None if ok else "low contrast"))
        return MuxSincronia(estado="medida", pista_a=1, pista_b=1, veredicto=veredicto,
                            alfa_num=Fraction(alfa).numerator, alfa_den=Fraction(alfa).denominator,
                            beta_ms=beta, dispersion_ms=4.5, pendiente_ppm=(float(alfa) - 1) * 1e6,
                            salto_entre=salto, n_validos=len(validos) if validos else 7, n_puntos=7,
                            puntos=puntos, huellas=[f.huella for f in p.fuentes],
                            medido_en=datetime.now(timezone.utc))

    def payload(p, en_cola=False, fase=""):
        plan = mux_plan.plan(p)
        d = p.model_dump(mode="json")
        d["plan"] = plan
        d["relato"] = mux_relato.resolver(p, en_cola=en_cola, fase_en_curso=fase, plan=plan)
        d["tamano_estimado"] = mux_plan.tamano_estimado(p)
        d["posicion_en_cola"] = 2 if en_cola else None
        d["midiendo"] = bool(p.sincronia and p.sincronia.estado == "midiendo")
        return d

    alfa = Fraction(1000, 1001)
    with mock.patch.object(i18n, "idioma_activo", return_value="en"):
        dec = proyecto("mux_Blade_Runner_2049_2017_1")
        dec.sincronia = medida(dec, mux_sync.NO_CONCLUYENTE, validos=[0, 4, 6])
        dec.retraso_manual_ms = 120
        hecho = proyecto("mux_Blade_Runner_2049_2017_2")
        hecho.sincronia = medida(hecho, mux_sync.OTRO_RITMO, alfa)
        hecho.status = "done"
        hecho.output_mkv_path = "/mnt/output/" + hecho.nombre_salida
        hecho.adjuntos = [{"fuente": 0, "id": 1}]
        hecho.validacion = {"fallos": 0, "checks": [
            {"id": "numero_de_pistas", "ok": True, "esperado": 7, "real": 7},
            {"id": "nombre", "pista": 2, "ok": False, "esperado": "Spanish TrueHD",
             "real": "Spanish"},
            {"id": "frames", "pista": 1, "ok": True, "esperado": 10, "real": 10},
            {"id": "duracion_del_video", "ok": True, "esperado": 6105.9, "real": 6105.9},
            {"id": "capitulos", "ok": True, "esperado": 12, "real": 12, "desvio_ms": 0.4},
            {"id": "adjuntos", "ok": True, "esperado": 1, "real": 1},
            {"id": "dolby_vision", "ok": True, "esperado": "P7 FEL v4.0", "real": "P7 FEL v4.0"},
            {"id": "sincronia", "pista": 2, "ok": True, "aviso": False,
             "esperado": "≤ 40 ms", "real": "6.2 ms"},
            {"id": "sincronia_pista", "pista": 3, "ok": None, "aviso": True,
             "esperado": "≤ 40 ms", "real": None}]}
        ini = datetime.now(timezone.utc) - timedelta(minutes=40)
        hecho.historial = [MuxEjecucion(
            run_number=1, started_at=ini, finished_at=ini + timedelta(minutes=14),
            status="done", fases={"comprobar_origenes": 4.2, "combinar": 742.0,
                                  "comprobar_y_guardar": 96.4},
            output_log=["[Pipeline] start"])]
        marcha = proyecto("mux_Blade_Runner_2049_2017_3")
        marcha.sincronia = medida(marcha, mux_sync.RETRASO, beta=2350)
        marcha.status = "running"
        cola = proyecto("mux_Blade_Runner_2049_2017_4")
        cola.sincronia = medida(cola, mux_sync.MONTAJE_DISTINTO, salto=[2797, 3652])
        cola.acepta_riesgo = True
        cola.status = "queued"
        midiendo = proyecto("mux_Blade_Runner_2049_2017_5")
        midiendo.sincronia = MuxSincronia(estado="midiendo", progreso=[3, 7])
        # El caso real (otro ritmo, el vídeo se retima) listo para combinar.
        listo = proyecto("mux_Blade_Runner_2049_2017_6")
        listo.sincronia = medida(listo, mux_sync.OTRO_RITMO, alfa)
        cache.update({"listo": payload(listo),
                      "decision": payload(dec), "hecho": payload(hecho),
                      "marcha": payload(marcha, fase="combinar"),
                      "cola": payload(cola, en_cola=True),
                      "midiendo": payload(midiendo)})
    return cache
