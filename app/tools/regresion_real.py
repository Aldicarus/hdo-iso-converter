"""Regresión real: lo que la app deriva de ficheros DE VERDAD, con las
herramientas de la imagen en la que corre.

Los fakes de la suite fijan la FORMA de lo que emiten las herramientas
(`test_contrato_de_los_fakes`), pero no pueden decir si una versión nueva de
mkvmerge, MediaInfo o dovi_tool cambia lo que la app saca de un disco real:
el orden de las pistas que ve, el nombre comercial con el que decide el
Atmos, los paquetes que escribe un mux o el RPU que deja un merge. Esto lo
mide: se ejecuta en la imagen vieja y en la nueva sobre los mismos ficheros,
y `comparar` dice qué cambió.

Lo que captura cada tipo de caso (todo con las funciones de la app, no con
réplicas suyas):

  tab1      `analyze_iso` (Fase A + B) sobre un ISO, una carpeta BDMV o un
            m2ts y, con `rip`, el pipeline entero (`_run_pipeline`: Fase D/E,
            la validación final y la firma). Del MKV que sale: su estructura,
            un resumen por flujo de cada paquete (contenido y tiempos), el
            análisis de «Consultar / Editar MKV» y una edición con
            `apply_mkv_edits` sobre una copia.
  fase_ab   la Fase A + B de Tab 1 sobre un MKV por la vía del m2ts suelto:
            las reglas de audio con pistas que ningún disco del NAS trae.
  tab2      `analyze_mkv` sin caché.
  rpu       `info --summary` parseado y `quality_desde_rpu` (combos L8/L2 y
            perfil de luminancia) de un RPU completo ya extraído.
  dovi      una ventana real de dos MKV del mismo título (el de origen y el
            CMv4.0): `extract-rpu`, `demux`, `_ensure_profile8_rpu`,
            `_merge_cmv40_into_p7` en sus dos políticas, `inject-rpu`, `mux`
            y `editor` con quitar/duplicar, cada artefacto con su sha256.
  tab4      un proyecto de «Multiplexar MKV» ya hecho: `leer_fuente` de las
            dos fuentes, `mux_plan.plan` con la misma selección y un
            multiplexado acotado a una ventana con el argv de `mux_plan.argv`.
  tab4_completo  el mismo proyecto multiplexado entero, con sus tres fases y
            la comprobación final (la aceptación: es caro).
  huella    la estructura y el resumen por flujo de un MKV que ya existe
            (para comparar un `tab4_completo` con el que dejó la versión
            anterior).

La estructura y los paquetes se leen con ffprobe/ffmpeg, que NO cambian entre
las dos imágenes: así una diferencia es del fichero y no del lector.

La lista de casos vive en el NAS, no en el repositorio (son ficheros de la
biblioteca del usuario). Uso, dentro de un contenedor de la imagen a medir:

    docker run --rm \\
      -v <isos>:/mnt/isos:ro -v <biblioteca>:/mnt/library:ro \\
      -v <output>:/mnt/output_real:ro -v <tmp>:/mnt/tmp_real:ro \\
      -v <config>:/config_real:ro -v <tmp>/regresion:/regresion \\
      -e TMDB_APP_KEY= -e TMDB_API_KEY= \\
      --entrypoint python3 <imagen> -u /regresion/regresion_real.py \\
      capturar --casos /regresion/casos.json --base /regresion/<etiqueta>

    python3 app/tools/regresion_real.py comparar vieja.json nueva.json

Solo lee los ficheros de los casos: todo lo que escribe va a `--base`.
"""
import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

APP = Path(os.environ.get("REGRESION_APP", "/app"))

# Lo que cambia en cada ejecución sin decir nada del fichero: fechas, el id
# de una sesión o de un proyecto (lleva la hora), logs con hora. Fuera de la
# comparación. Ojo: `id` solo cuando es una cadena —el de una sesión—: el de
# una pista es un entero y es justo lo que hay que comparar (una
# renumeración de pistas no se vería).
VOLATILES = {
    "created_at", "updated_at", "analyzed_at", "analysis_log", "output_log",
    "execution_history", "execution_started_at", "execution_finished_at",
    "last_cancelled_at", "relato", "session_id", "segment_uid",
    "phase_history", "last_executed",
}
# Las salidas crudas de las herramientas: cambian con la versión por
# definición. Se guardan aparte (con su sha256) para explicar una diferencia,
# no para provocarla.
CRUDOS = {"mkvmerge_raw", "mediainfo_raw", "raw_json", "raw_summary"}


# ═══════════════════════════════════════════════════════════════════════════
#  Captura
# ═══════════════════════════════════════════════════════════════════════════

class Contexto:
    def __init__(self, base: Path):
        self.base = base
        self.crudo = base / "crudo"
        self.trabajo = base / "trabajo"
        for d in (self.crudo, self.trabajo):
            d.mkdir(parents=True, exist_ok=True)
        self.caso = ""
        #: Las salidas crudas del caso en curso, nombre → sha256. Van a su
        #: propia sección del resultado, FUERA de lo que se compara.
        self.crudos: dict = {}

    def guardar_crudo(self, nombre: str, contenido) -> str:
        """Guarda una salida cruda y devuelve su sha256, calculado con la
        carpeta de trabajo ya neutralizada: dos capturas con bases distintas
        no pueden diferir por eso."""
        carpeta = self.crudo / self.caso
        carpeta.mkdir(parents=True, exist_ok=True)
        if isinstance(contenido, (dict, list)):
            texto = json.dumps(contenido, ensure_ascii=False, indent=1, sort_keys=True)
        else:
            texto = str(contenido)
        texto = texto.replace(str(self.base), "<BASE>")
        (carpeta / nombre).write_text(texto, encoding="utf-8")
        sha = hashlib.sha256(texto.encode("utf-8")).hexdigest()
        self.crudos[nombre] = sha
        return sha

    def normalizar(self, x, ruta: str = ""):
        """Dump comparable: sin volátiles, con los crudos fuera y con las
        rutas de trabajo neutralizadas."""
        if hasattr(x, "model_dump"):
            x = x.model_dump(mode="json")
        if isinstance(x, dict):
            out = {}
            for k, v in x.items():
                if k in VOLATILES or (k == "id" and isinstance(v, str)):
                    continue
                if k in CRUDOS:
                    if v:
                        nombre = (ruta + "." + k).strip(".").replace("/", "_") + ".json"
                        self.guardar_crudo(nombre, v)
                    continue
                out[k] = self.normalizar(v, ruta + "." + str(k))
            return out
        if isinstance(x, (list, tuple)):
            return [self.normalizar(v, ruta + f"[{i}]") for i, v in enumerate(x)]
        if isinstance(x, str) and str(self.base) in x:
            return x.replace(str(self.base), "<BASE>")
        if isinstance(x, float):
            return round(x, 6)
        return x


def _sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for trozo in iter(lambda: f.read(8 << 20), b""):
            h.update(trozo)
    return h.hexdigest()


def _correr(cmd: list, tope: float = 3600, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          timeout=tope, cwd=cwd)


def estructura(mkv: Path) -> dict:
    """La estructura del MKV según ffprobe: flujos, banderas, idiomas,
    nombres, capítulos y la configuración Dolby Vision. Sin las etiquetas de
    estadísticas, que llevan la versión del mkvmerge que las escribió."""
    r = _correr(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                 "-show_chapters", "-of", "json", mkv], tope=900)
    d = json.loads(r.stdout or "{}")
    flujos = []
    for s in d.get("streams", []):
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        lateral = s.get("side_data_list") or []
        flujos.append({
            "tipo": s.get("codec_type"), "codec": s.get("codec_name"),
            "perfil": s.get("profile"), "ancho": s.get("width"), "alto": s.get("height"),
            "pix_fmt": s.get("pix_fmt"),
            "color": [s.get("color_transfer"), s.get("color_primaries"), s.get("color_space")],
            "r_frame_rate": s.get("r_frame_rate"), "avg_frame_rate": s.get("avg_frame_rate"),
            "canales": s.get("channels"), "disposicion_canales": s.get("channel_layout"),
            "muestreo": s.get("sample_rate"),
            "banderas": sorted(k for k, v in (s.get("disposition") or {}).items() if v),
            "idioma": tags.get("language"), "nombre": tags.get("title"),
            "frames_etiqueta": next((v for k, v in tags.items()
                                     if k.startswith("number_of_frames")), None),
            "duracion_etiqueta": next((v for k, v in tags.items()
                                       if k.startswith("duration")), None),
            # El side data CON sus valores —la configuración Dolby Vision, la
            # luz de contenido (MaxCLL/MaxFALL) y el mastering display de la
            # cabecera—: con solo el tipo, dos MKV con otro MaxCLL salían iguales.
            "lateral": sorted((dict(x) for x in lateral),
                              key=lambda x: json.dumps(x, sort_keys=True)),
        })
    fmt = d.get("format") or {}
    ftags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    return {
        "flujos": flujos,
        "duracion": fmt.get("duration"),
        "titulo": ftags.get("title"),
        "capitulos": [{"inicio": c.get("start_time"), "fin": c.get("end_time"),
                       "nombre": (c.get("tags") or {}).get("title")}
                      for c in d.get("chapters", [])],
        "cues": _cues(mkv),
    }


def _cues(mkv: Path) -> list:
    """Cuántas entradas de índice (Cues) lleva cada pista. Los paquetes se
    leen en secuencia y no dicen nada del índice, que es lo que usa un
    reproductor para saltar: un mux que lo escribiera mal tendría los mismos
    paquetes. Es un dato del fichero; mkvmerge solo lo lee."""
    try:
        j = json.loads(_correr(["mkvmerge", "-J", mkv], tope=600).stdout or "{}")
    except Exception:
        return []
    return [(t.get("type"), (t.get("properties") or {}).get("num_index_entries"))
            for t in j.get("tracks", [])]


def paquetes(mkv: Path, carpeta: Path, tope: float = 4 * 3600) -> dict:
    """Un resumen por flujo de TODOS los paquetes: cuántos, cuántos bytes,
    primer y último pts, y dos sha256 — el del contenido (el md5 de cada
    paquete) y el de los tiempos (dts, pts, duración). Separados, para que
    una diferencia diga si cambió lo que hay dentro o cuándo suena.

    La salida de ffmpeg va a un fichero y no a una tubería: leyendo stdout
    hasta el final antes que stderr, un MKV que hiciera escupir errores a
    ffmpeg llenaba la tubería de stderr y los dos procesos se quedaban
    esperándose (revisión de Codex del 2026-10-28)."""
    salida = carpeta / "framemd5.txt"
    errores = carpeta / "framemd5.err"
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(mkv),
           "-map", "0:v?", "-map", "0:a?", "-map", "0:s?",
           "-c", "copy", "-f", "framemd5", "-"]
    try:
        with open(salida, "w") as fo, open(errores, "w") as fe:
            rc = subprocess.run(cmd, stdout=fo, stderr=fe, timeout=tope).returncode
    except subprocess.TimeoutExpired:
        rc = "tope"
    bases, por = {}, {}
    for linea in open(salida, encoding="utf-8", errors="replace"):
        if linea.startswith("#tb"):
            ind, _, tb = linea[3:].partition(":")
            bases[ind.strip()] = tb.strip()
            continue
        if linea.startswith("#") or not linea.strip():
            continue
        partes = [x.strip() for x in linea.split(",")]
        if len(partes) < 6:
            continue
        ind, dts, pts, dur, tam, md5 = partes[:6]
        f = por.get(ind)
        if f is None:
            f = por[ind] = {"n": 0, "bytes": 0, "primer_pts": pts, "ultimo_pts": pts,
                            "duracion_total": 0, "_c": hashlib.sha256(),
                            "_t": hashlib.sha256()}
        f["n"] += 1
        f["bytes"] += int(tam)
        f["ultimo_pts"] = pts
        f["duracion_total"] += int(dur)
        f["_c"].update(md5.encode())
        f["_t"].update(f"{dts},{pts},{dur};".encode())
    err = errores.read_text(encoding="utf-8", errors="replace")
    salida.unlink(missing_ok=True)
    errores.unlink(missing_ok=True)
    out = {}
    for ind, f in por.items():
        out[ind] = {k: v for k, v in f.items() if not k.startswith("_")}
        out[ind]["base_de_tiempos"] = bases.get(ind)
        out[ind]["sha_contenido"] = f["_c"].hexdigest()
        out[ind]["sha_tiempos"] = f["_t"].hexdigest()
    return {"flujos": out, "rc": rc, "error": err.strip()[-500:]}


def huella_salida(mkv: Path, ctx: "Contexto", con_paquetes: bool = True) -> dict:
    d = {"tamano": mkv.stat().st_size, "estructura": estructura(mkv)}
    if con_paquetes:
        d["paquetes"] = paquetes(mkv, ctx.trabajo)
    return d


async def caso_tab1(c: dict, ctx: Contexto) -> dict:
    from models import AnalyzeRequest
    from routers import tab1 as T1
    from storage import load_session
    from phases.mkv_analyze import analyze_mkv

    payload = await T1.analyze_iso(AnalyzeRequest(source_type=c.get("source_type", "m2ts"),
                                                  source_path=c["origen"]))
    sid = payload["id"]
    d = {"analisis": ctx.normalizar(payload, "analisis")}
    if c.get("mantener_todas"):
        # «Mantener todas» en audio y subtítulos: las reglas descartan lo que
        # no es castellano ni VO, y una demo con el audio sin idioma saldría
        # sin audio — el mux no pasaría por ningún paquete TrueHD.
        payload = await T1.reapply_rules(sid, T1.ReapplyModeRequest(
            audio_mode="keep_all", subtitle_mode="keep_all"))
        d["analisis_todas"] = ctx.normalizar(payload, "analisis_todas")
    if not c.get("rip", True):
        return d
    await T1._run_pipeline(sid)
    s = load_session(sid)
    d["rip"] = {"status": s.status, "error": s.error_message,
                "avisos_validacion": s.last_validation_warnings,
                "mkv": Path(s.output_mkv_path).name if s.output_mkv_path else None}
    if s.output_log:
        ctx.guardar_crudo("rip.log", "\n".join(s.output_log))
    salida = Path(s.output_mkv_path) if s.output_mkv_path else None
    if not salida or not salida.exists():
        return d
    d["salida"] = huella_salida(salida, ctx)
    analisis = await analyze_mkv(str(salida), use_cache=False)
    d["salida_tab2"] = ctx.normalizar(analisis, "salida_tab2")
    d["edicion"] = await _editar_copia(salida, analisis, ctx)
    return d


async def _editar_copia(salida: Path, analisis, ctx: Contexto) -> dict:
    """Una edición de «Consultar / Editar MKV» sobre una copia: título,
    nombre y bandera de la primera pista de audio, forzado de la primera de
    subtítulos y los capítulos renombrados."""
    from models import Chapter, MkvEditRequest, MkvEditTrack
    from phases.mkv_analyze import apply_mkv_edits

    copia = ctx.trabajo / f"{ctx.caso}_editado.mkv"
    shutil.copyfile(salida, copia)
    audio = [t for t in analisis.tracks if t.type == "audio"]
    subs = [t for t in analisis.tracks if t.type == "subtitles"]
    capitulos = [Chapter(number=i + 1, timestamp=ch.timestamp,
                         name=f"Regresión {i + 1:02d}", name_custom=True)
                 for i, ch in enumerate(analisis.chapters)]
    req = MkvEditRequest(
        file_path=str(copia), title="Regresión real",
        audio_tracks=([MkvEditTrack(id=audio[0].id, name="Audio renombrado",
                                    flag_default=True)] if audio else []),
        subtitle_tracks=([MkvEditTrack(id=subs[0].id, name="Subtítulo renombrado",
                                       flag_forced=not subs[0].flag_forced)] if subs else []),
        chapters=capitulos or None,
    )
    res = await apply_mkv_edits(req)
    d = {"resultado": ctx.normalizar(res, "edicion"),
         "salida": huella_salida(copia, ctx, con_paquetes=False)}
    copia.unlink(missing_ok=True)
    return d


async def caso_fase_ab(c: dict, ctx: Contexto) -> dict:
    """La Fase A + B de Tab 1 sobre un MKV, por la vía del m2ts suelto.

    No es un flujo de la app —Tab 1 no abre MKV—, pero es la única forma de
    pasar por las reglas de audio con pistas reales que no están en ningún
    disco del NAS (un DTS:X, un DTS-HD HRA): mkvmerge -J, MediaInfo y la
    muestra de Dolby Vision leen un MKV igual que un m2ts, y lo que se mide
    es qué pista elige la Fase B con lo que la Fase A dedujo de ellas."""
    from phases.phase_a import run_full_analysis_for_m2ts
    from phases.phase_b import apply_rules
    bdinfo, _caps = await run_full_analysis_for_m2ts(c["mkv"])
    reglas = apply_rules(bdinfo, c["mkv"], "audio dcp" in c["mkv"].lower())
    return {"bdinfo": ctx.normalizar(bdinfo, "bdinfo"),
            "reglas": ctx.normalizar(reglas, "reglas")}


async def caso_tab2(c: dict, ctx: Contexto) -> dict:
    from phases.mkv_analyze import analyze_mkv
    r = await analyze_mkv(c["mkv"], use_cache=False)
    return {"analisis": ctx.normalizar(r, "analisis")}


async def _summary(rpu: Path, ctx: Contexto, nombre: str) -> dict:
    from phases.phase_a import _parse_dovi_summary
    r = _correr(["dovi_tool", "info", "-i", rpu, "--summary"], tope=1800)
    ctx.guardar_crudo(f"{nombre}.summary.txt", r.stdout + r.stderr)
    if r.returncode != 0:
        return {"rc": r.returncode, "error": (r.stderr or r.stdout).strip()[-300:]}
    d = _parse_dovi_summary(r.stdout).model_dump(mode="json")
    d.pop("raw_summary", None)
    return {"rc": 0, "summary": d}


async def caso_rpu(c: dict, ctx: Contexto) -> dict:
    from phases.mkv_analyze import quality_desde_rpu
    rpu = Path(c["bin"])
    tmp = ctx.trabajo / ctx.caso
    tmp.mkdir(parents=True, exist_ok=True)
    payload = await quality_desde_rpu(rpu, tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    return {"sha256": _sha256(rpu), "info": await _summary(rpu, ctx, "rpu"),
            "quality": ctx.normalizar(payload, "quality")}


def _ventana_hevc(mkv: str, desde: float, segundos: float, salida: Path) -> dict:
    r = _correr(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(desde), "-i", mkv,
                 "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "hevc_mp4toannexb",
                 "-t", str(segundos), "-f", "hevc", salida], tope=1800)
    return {"rc": r.returncode, "sha256": _sha256(salida) if salida.exists() else None,
            "error": r.stderr.strip()[-300:]}


def _paso_dovi(cmd: list, salida: Path, d: dict, clave: str, cwd=None) -> bool:
    r = _correr(["dovi_tool", *cmd], tope=1800, cwd=cwd)
    d[clave] = {"rc": r.returncode,
                "sha256": _sha256(salida) if salida.exists() else None,
                "error": (r.stderr or "").strip()[-300:] if r.returncode else ""}
    return r.returncode == 0 and salida.exists()


async def caso_dovi(c: dict, ctx: Contexto) -> dict:
    from phases import cmv40_pipeline as P
    wd = ctx.trabajo / ctx.caso
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True)
    desde, seg = float(c.get("desde", 600)), float(c.get("segundos", 30))
    d = {}
    src, tgt = wd / "source.hevc", wd / "target.hevc"
    d["ventana_origen"] = _ventana_hevc(c["origen"], desde, seg, src)
    d["ventana_objetivo"] = _ventana_hevc(c["objetivo"], desde, seg, tgt)
    rs, rt = wd / "RPU_source.bin", wd / "RPU_target.bin"
    if _paso_dovi(["extract-rpu", "-i", src, "-o", rs], rs, d, "extract_origen"):
        d["info_origen"] = await _summary(rs, ctx, "rpu_origen")
    if _paso_dovi(["extract-rpu", "-i", tgt, "-o", rt], rt, d, "extract_objetivo"):
        d["info_objetivo"] = await _summary(rt, ctx, "rpu_objetivo")
    bl, el = wd / "BL.hevc", wd / "EL.hevc"
    _paso_dovi(["demux", src, "--bl-out", bl, "--el-out", el], el, d, "demux_el")
    d["demux_bl"] = {"sha256": _sha256(bl) if bl.exists() else None}
    if rs.exists():
        p8 = await P._ensure_profile8_rpu(rs, wd)
        d["profile8"] = {"es_otro_fichero": p8 != rs,
                         "sha256": _sha256(p8) if p8 and p8.exists() else None}
        if p8 and p8.exists() and p8 != rs:
            d["profile8"]["info"] = await _summary(p8, ctx, "rpu_profile8")
    if rs.exists() and rt.exists():
        for nombre, conservar in (("merge_fel", False), ("merge_conservador", True)):
            out = wd / f"RPU_{nombre}.bin"
            try:
                await P._merge_cmv40_into_p7(rs, rt, out,
                                             conservar_niveles_del_disco=conservar)
                d[nombre] = {"sha256": _sha256(out), "info": await _summary(out, ctx, nombre)}
            except Exception as e:      # el merge que falla ES el dato
                d[nombre] = {"error": f"{type(e).__name__}: {e}"[:300]}
        merged = wd / "RPU_merge_fel.bin"
        if merged.exists() and el.exists():
            el_inj = wd / "EL_injected.hevc"
            if _paso_dovi(["inject-rpu", "-i", el, "--rpu-in", merged, "-o", el_inj],
                          el_inj, d, "inject_el") and bl.exists():
                dual = wd / "DV_dual.hevc"
                _paso_dovi(["mux", "--bl", bl, "--el", el_inj, "-o", dual], dual, d, "mux")
        if rt.exists():
            n = (d.get("info_objetivo") or {}).get("summary", {}).get("frame_count") or 0
            if n > 20:
                cfg = wd / "editor.json"
                cfg.write_text(json.dumps({"remove": ["0-4"], "duplicate": [
                    {"source": n - 8, "offset": n - 8, "length": 3}]}))
                ed = wd / "RPU_editado.bin"
                if _paso_dovi(["editor", "-i", rt, "-j", cfg, "-o", ed], ed, d, "editor"):
                    d["editor"]["info"] = await _summary(ed, ctx, "rpu_editado")
    shutil.rmtree(wd, ignore_errors=True)
    return d


def _proyecto_real(ruta: str):
    from models import MuxProject
    return MuxProject(**json.loads(Path(ruta).read_text(encoding="utf-8")))


async def _proyecto_releido(c: dict, ctx: Contexto):
    """El proyecto guardado, con las dos fuentes leídas OTRA VEZ con las
    herramientas de esta imagen y la selección de entonces."""
    from phases import mux_pipeline as MP
    real = _proyecto_real(c["proyecto"])
    fuentes = [await MP.leer_fuente(f.ruta) for f in real.fuentes]
    p = real.model_copy(deep=True)
    p.fuentes = fuentes
    p.id = "regresion_" + ctx.caso
    p.status = "pending"
    p.historial = []
    p.validacion = None
    p.output_mkv_path = None
    p.error_message = None
    p.trabajo_propio = None
    return real, p


def con_ventana(cmd: list, desde: str, hasta: str) -> list:
    """El argv de un multiplexado acotado a [desde, hasta]: `--split parts:`
    justo detrás de `-o salida`, entre las opciones globales. Delante de una
    fuente mkvmerge lo tomaría igual —es global—, pero detrás de `-o` es
    donde no se puede confundir con una opción de la fuente siguiente."""
    i = cmd.index("-o") + 2
    return cmd[:i] + ["--split", f"parts:{desde}-{hasta}"] + cmd[i:]


async def caso_tab4(c: dict, ctx: Contexto) -> dict:
    from phases import mux_pipeline as MP, mux_plan
    _real, p = await _proyecto_releido(c, ctx)
    plan = mux_plan.plan(p)
    d = {"fuentes": ctx.normalizar(p.fuentes, "fuentes"),
         "plan": ctx.normalizar(plan, "plan")}
    if not plan.get("puede_multiplexar") or not c.get("ventana"):
        return d
    wd = ctx.trabajo / ctx.caso
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True)
    xml, _caps = await MP._capitulos_xml(p, plan, wd)
    salida = wd / "ventana.mkv"
    cmd = con_ventana(mux_plan.argv(p, plan, str(salida), xml), *c["ventana"])
    r = _correr(cmd, tope=3600)
    hechos = sorted(wd.glob("ventana*.mkv"))
    d["ventana"] = {"rc": r.returncode, "ficheros": len(hechos)}
    if hechos:
        d["ventana"]["salida"] = huella_salida(hechos[0], ctx)
    shutil.rmtree(wd, ignore_errors=True)
    return d


async def caso_tab4_completo(c: dict, ctx: Contexto) -> dict:
    """El multiplexado entero con las tres fases, a la carpeta de trabajo de
    la regresión, y su huella (para compararla con la del MKV que dejó la
    versión anterior)."""
    from phases import mux_pipeline as MP, mux_plan
    _real, p = await _proyecto_releido(c, ctx)
    plan = mux_plan.plan(p)
    lineas: list[str] = []

    async def log(linea: str):
        lineas.append(linea)

    d = {"plan": ctx.normalizar(plan, "plan")}
    try:
        await MP.fase_a_comprobar_origenes(p, plan, log)
        trabajo, caps = await MP.fase_b_multiplexar(p, plan, log)
        resultado = await MP.fase_c_comprobar_y_guardar(p, plan, trabajo, log,
                                                       capitulos_ns=caps)
        d["fase_c"] = ctx.normalizar(resultado, "fase_c")
    except Exception as e:
        d["error"] = f"{type(e).__name__}: {e}"[:500]
    ctx.guardar_crudo("multiplexado.log", "\n".join(lineas))
    final = MP.ruta_final(p)
    if final.exists():
        d["salida"] = huella_salida(final, ctx)
        if not c.get("conservar"):
            final.unlink()
    return d


async def caso_huella(c: dict, ctx: Contexto) -> dict:
    return {"salida": huella_salida(Path(c["mkv"]), ctx, con_paquetes=c.get("paquetes", True))}


CASOS = {"tab1": caso_tab1, "fase_ab": caso_fase_ab, "tab2": caso_tab2, "rpu": caso_rpu, "dovi": caso_dovi,
         "tab4": caso_tab4, "tab4_completo": caso_tab4_completo, "huella": caso_huella}


def _versiones() -> dict:
    out = {}
    for b, a in (("dovi_tool", "--version"), ("mkvmerge", "--version"),
                 ("mediainfo", "--version"), ("ffmpeg", "-version")):
        try:
            lineas = _correr([b, a], tope=30).stdout.strip().splitlines()
            out[b] = lineas[-1 if b == "mediainfo" else 0] if lineas else ""
        except Exception as e:
            out[b] = f"<{e}>"
    out["python"] = sys.version.split()[0]
    try:
        out["os"] = Path("/etc/os-release").read_text().split("PRETTY_NAME=")[1].splitlines()[0].strip('"')
    except Exception:
        out["os"] = ""
    return out


def _preparar_entorno(base: Path) -> None:
    """Los directorios de la app, a la carpeta de la regresión ANTES de
    importar nada: se resuelven al importar (ver `paths.py`). El origen de
    Tab 1 se valida contra ISOS_DIR, y los casos viven bajo /mnt."""
    for clave, sub in (("CONFIG_DIR", "config"), ("TMP_DIR", "tmp"), ("OUTPUT_DIR", "output")):
        d = base / sub
        d.mkdir(parents=True, exist_ok=True)
        os.environ[clave] = str(d)
    os.environ["ISOS_DIR"] = "/mnt"
    os.environ.setdefault("HDO_IDIOMA", "es")
    # TMDb fuera: la ficha cambiaría el nombre del MKV según lo que conteste
    # la red ese día, y eso no es de las herramientas.
    os.environ["TMDB_APP_KEY"] = ""
    os.environ["TMDB_API_KEY"] = ""
    sys.path.insert(0, str(APP))


def capturar(args) -> int:
    base = Path(args.base)
    base.mkdir(parents=True, exist_ok=True)
    _preparar_entorno(base)
    casos = json.loads(Path(args.casos).read_text(encoding="utf-8"))
    if args.solo:
        casos = [c for c in casos if c["id"] in args.solo]
    salida = Path(args.salida or (str(base) + ".json"))
    resultado = {"versiones": _versiones(), "casos": {}}
    if salida.exists() and args.seguir:
        resultado = retomar(salida, resultado["versiones"])
    asyncio.run(_capturar_todos(casos, resultado, salida, Contexto(base), args.seguir))
    fallidos = [cid for cid, c in resultado["casos"].items() if "excepcion" in c]
    if fallidos:
        print(f"\n{len(fallidos)} casos con excepción: {', '.join(fallidos)}", flush=True)
    return 1 if fallidos else 0


def retomar(salida: Path, versiones: dict) -> dict:
    """La captura que `--seguir` retoma, si es de ESTAS herramientas.

    Retomar con otras mezclaría resultados de dos imágenes bajo las
    versiones de la última, y la comparación podría dar «igual» sin haber
    ejecutado nada (revisión de Codex del 2026-10-28)."""
    previo = json.loads(salida.read_text(encoding="utf-8"))
    if previo.get("versiones") != versiones:
        raise SystemExit("la captura que se quiere retomar es de otras herramientas:\n"
                         f"  antes  {previo.get('versiones')}\n"
                         f"  ahora  {versiones}")
    return previo


async def _capturar_todos(casos, resultado, salida: Path, ctx: Contexto, seguir: bool) -> None:
    """Todos los casos en UN bucle de eventos: la app guarda cerrojos de
    asyncio en variables de módulo, y en Python 3.10 —la imagen vieja— un
    cerrojo queda atado al bucle en el que se creó."""
    for c in casos:
        hecho = resultado["casos"].get(c["id"])
        if seguir and hecho and "excepcion" not in hecho:
            continue
        ctx.caso = c["id"]
        ctx.crudos = {}
        print(f"[{time.strftime('%H:%M:%S')}] {c['id']} ({c['tipo']})…", flush=True)
        t0 = time.monotonic()
        try:
            derivado = await CASOS[c["tipo"]](c, ctx)
            resultado["casos"][c["id"]] = {"tipo": c["tipo"], "derivado": derivado}
        except Exception as e:
            resultado["casos"][c["id"]] = {"tipo": c["tipo"],
                                           "excepcion": f"{type(e).__name__}: {e}",
                                           "traza": traceback.format_exc()[-3000:]}
        caso = resultado["casos"][c["id"]]
        caso["crudo"] = dict(ctx.crudos)
        caso["segundos"] = round(time.monotonic() - t0, 1)
        salida.write_text(json.dumps(resultado, ensure_ascii=False, indent=1, sort_keys=True),
                          encoding="utf-8")
        print(f"    {caso['segundos']} s"
              + ("  ⚠ " + caso["excepcion"] if "excepcion" in caso else ""), flush=True)


# ═══════════════════════════════════════════════════════════════════════════
#  Comparación
# ═══════════════════════════════════════════════════════════════════════════

def _diferencias(a, b, ruta: str, out: list) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append((f"{ruta}.{k}", "<no estaba>", b[k]))
            elif k not in b:
                out.append((f"{ruta}.{k}", a[k], "<ya no está>"))
            else:
                _diferencias(a[k], b[k], f"{ruta}.{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((f"{ruta} (longitud)", len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            _diferencias(x, y, f"{ruta}[{i}]", out)
    elif a != b:
        out.append((ruta, a, b))


def _corto(v, n: int = 140) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= n else s[:n] + "…"


def fallos(x, ruta: str = "") -> list:
    """Dónde falló algo DENTRO de un caso que terminó: un paso con `rc`
    distinto de 0, un `status` de error, un multiplexado que no dejó fichero.
    Dos capturas que fallan igual son «iguales», y una regresión que da por
    buena la igualdad de dos fallos no prueba nada (revisión de Codex del
    2026-10-28): se cuentan aparte."""
    out = []
    if isinstance(x, dict):
        rc = x.get("rc")
        if rc not in (None, 0):
            out.append(f"{ruta} rc={rc}" + (f" ({x['error'][:120]})" if x.get("error") else ""))
        if x.get("status") == "error":
            out.append(f"{ruta} status=error")
        if "ficheros" in x and not x.get("ficheros"):
            out.append(f"{ruta} sin fichero de salida")
        for k, v in x.items():
            out.extend(fallos(v, f"{ruta}.{k}" if ruta else str(k)))
    elif isinstance(x, list):
        for i, v in enumerate(x):
            out.extend(fallos(v, f"{ruta}[{i}]"))
    return out


def comparar(args) -> int:
    va = json.loads(Path(args.vieja).read_text(encoding="utf-8"))
    vb = json.loads(Path(args.nueva).read_text(encoding="utf-8"))
    print("Versiones")
    for k in sorted(set(va["versiones"]) | set(vb["versiones"])):
        print(f"  {k:10} {va['versiones'].get(k, '')}  →  {vb['versiones'].get(k, '')}")
    hay = n_fallos = 0
    for cid in sorted(set(va["casos"]) | set(vb["casos"])):
        a, b = va["casos"].get(cid), vb["casos"].get(cid)
        if not a or not b:
            # Un caso que falta es el peor falso verde: el que desaparece puede
            # ser justo el que fallaba.
            print(f"\n■ {cid}: solo en {'la nueva' if b else 'la vieja'}")
            hay += 1
            continue
        for lado, x in (("vieja", a), ("nueva", b)):
            if "excepcion" in x:
                print(f"\n■ {cid}: la {lado} falló — {x['excepcion']}")
                hay += 1
        if "derivado" not in a or "derivado" not in b:
            continue
        da, db = a["derivado"], b["derivado"]
        if a["tipo"] != b["tipo"]:
            # `huella` (el MKV que dejó la versión anterior) contra
            # `tab4_completo` (el que deja la nueva): lo comparable es la
            # salida; el plan y la comprobación solo existen en uno.
            print(f"\n■ {cid}: {a['tipo']} → {b['tipo']}: se compara solo la salida")
            da, db = {"salida": da.get("salida")}, {"salida": db.get("salida")}
        for lado, x in (("vieja", da), ("nueva", db)):
            f = fallos(x)
            if f:
                n_fallos += len(f)
                print(f"\n■ {cid}: fallos en la {lado}:")
                for linea in f[: args.max]:
                    print(f"   ⚠ {linea}")
        difs: list = []
        _diferencias(da, db, cid, difs)
        crudos = sorted(k for k in set(a.get("crudo", {})) | set(b.get("crudo", {}))
                        if a.get("crudo", {}).get(k) != b.get("crudo", {}).get(k))
        nota = f" · {len(crudos)} salidas crudas distintas" if crudos else ""
        if difs:
            hay += len(difs)
            print(f"\n■ {cid} ({a['tipo']}, {a.get('segundos')} s → {b.get('segundos')} s): "
                  f"{len(difs)} diferencias{nota}")
            for ruta, x, y in difs[: args.max]:
                print(f"   {ruta}\n      {_corto(x)}\n    → {_corto(y)}")
            if len(difs) > args.max:
                print(f"   … y {len(difs) - args.max} más")
        else:
            print(f"\n■ {cid}: igual ({a.get('segundos')} s → {b.get('segundos')} s){nota}")
    print(f"\n{hay} diferencias y {n_fallos} fallos en total")
    return 1 if hay or n_fallos else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="orden", required=True)
    c = sub.add_parser("capturar")
    c.add_argument("--casos", required=True)
    c.add_argument("--base", required=True)
    c.add_argument("--salida")
    c.add_argument("--solo", nargs="*")
    c.add_argument("--seguir", action="store_true",
                   help="retoma una captura: salta los casos ya hechos sin excepción")
    k = sub.add_parser("comparar")
    k.add_argument("vieja")
    k.add_argument("nueva")
    k.add_argument("--max", type=int, default=40)
    args = ap.parse_args(argv)
    return capturar(args) if args.orden == "capturar" else comparar(args)


if __name__ == "__main__":
    sys.exit(main())
