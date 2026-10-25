"""
phases/mux_plan.py — Qué se hace con dos MKV para combinarlos: la selección
por defecto, el nombre de salida, la línea de tiempo y el argv de mkvmerge.

Es PURO —sin E/S ni subprocesos— por lo mismo que `cmv40_strategy`: así se
puede recorrer cada combinación en un test de milisegundos, y la interfaz y
el pipeline leen el MISMO plan en vez de derivarlo cada uno por su cuenta.
No escribe textos: devuelve identificadores y números, y la interfaz los
traduce (`mux.motivo_<id>`, `mux.linea_<id>`…). Un plan con frases dentro se
congelaría en el idioma del servidor.

La regla que manda: la línea de tiempo del AUDIO
────────────────────────────────────────────────
El MKV final tiene una sola línea de tiempo. Las pistas que vienen del otro
MKV se pueden mover (`--sync TID:d`) y las de texto y el vídeo también se
pueden estirar (`--sync TID:d,o/p`, `--default-duration`) sin recodificar
nada. El audio NO: estirar sus marcas de tiempo sin remuestrear deja huecos o
solapes entre tramas, y un TrueHD Atmos no se recodifica. Así que:

  · misma cadencia → manda el MKV del vídeo; las pistas del otro se retrasan
    lo medido;
  · cadencias distintas y todo el audio del otro → manda el otro: el vídeo
    se RETIMA a su cadencia (`--default-duration VID:24p`, o la duración
    exacta de frame que mida la sincronía), y los subtítulos y capítulos del
    MKV del vídeo se estiran con él. Comprobado sobre el caso real (Toy Story
    5, remux a 23,976 + WEB-DL a 24): Dolby Vision P7 FEL intacto y residuo
    de +1 / +8 ms;
  · cadencias distintas y todo el audio del MKV del vídeo → manda el del
    vídeo, y lo que se traiga del otro (subtítulos, capítulos) se estira;
  · cadencias distintas y audio de LOS DOS → no hay plan posible sin
    recodificar: uno de los dos audios iría a otra velocidad. Se dice y no se
    combina.

mkvmerge aplica `--sync TID:d,o/p` como t·(o/p) + d (factor primero,
desplazamiento después), y así se escribe aquí.
"""
from __future__ import annotations

import re
from fractions import Fraction

from phases import mux_sync

MKVMERGE_BIN = "mkvmerge"

# Las dos situaciones de la línea de tiempo. La interfaz las cuenta con
# `tab4.linea_<id>` (catálogo del frontend).
LINEA_DEL_VIDEO = "video"     # manda el MKV del vídeo
LINEA_DEL_RESTO = "resto"     # manda el otro: el vídeo se retima

# Los motivos por los que no se puede combinar todavía. Identificadores: el
# texto lo pone la interfaz (`tab4.motivo_<id>`, con los `params` que lleven);
# el servidor tiene su forma corta (`mux.motivo_corto_<id>`) para el 409 y la
# explicación del relato (`relato.mux_porque_<id>`).
MOTIVOS = (
    "sin_audio",            # el MKV final no llevaría ninguna pista de audio
    "midiendo",             # la medida de sincronía está en marcha
    "sin_medida",           # no hay medida (o falló, o se canceló)
    "medida_vieja",         # la medida es de otros ficheros
    "audio_de_dos_ritmos",  # cadencias distintas y audio de los dos MKV
    "deriva",               # se separan a un ritmo que nadie declara
    "montaje_distinto",     # salto: escenas añadidas o quitadas
    "no_concluyente",       # la medida no dio puntos suficientes
    "video_no_retimable",   # hay que retimar y el vídeo no tiene cadencia fija
    "en_marcha",            # ya se está combinando (o espera turno)
)

# Los que el usuario puede pasar por alto con «combinar igualmente». Los que no
# están aquí no tienen salida que no sea cambiar algo: combinar audio de dos
# ritmos da un MKV en el que la mitad de las pistas se desincroniza, y eso no
# lo arregla aceptar el riesgo.
SALTABLES = frozenset({"sin_medida", "medida_vieja", "deriva",
                       "montaje_distinto", "no_concluyente"})

# Tipos MIME de una fuente tipográfica. Un subtítulo ASS/SSA las necesita y
# sin ellas el reproductor cae a una genérica.
_MIME_DE_FUENTE = ("font/", "application/x-truetype-font",
                   "application/vnd.ms-opentype", "application/x-font",
                   "application/font-sfnt")


# ═══════════════════════════════════════════════════════════════════════════
#  Lo que hay en cada fuente
# ═══════════════════════════════════════════════════════════════════════════

def _pistas(fuente, tipo: str) -> list:
    return [p for p in fuente.pistas if p.tipo == tipo]


def _por_clave(proyecto) -> dict:
    """`{(fuente, id): MuxPistaOrigen}` de las dos fuentes."""
    return {(i, p.id): p for i, f in enumerate(proyecto.fuentes) for p in f.pistas}


def cadencia(fuente) -> Fraction | None:
    v = fuente.video
    if not v.cadencia_num:
        return None
    return Fraction(v.cadencia_num, v.cadencia_den or 1)


def video_retimable(fuente) -> bool:
    """Si al vídeo de esta fuente se le puede cambiar la cadencia.

    Retimar es escribir UNA duración de frame para todo el vídeo
    (`--default-duration`), así que solo vale para una cadencia fija y la
    misma en todas sus pistas. Un VFR (`FrameRate_Mode` de MediaInfo), un
    vídeo sin duración de frame en el contenedor o una BL y una EL en pistas
    separadas con duraciones distintas quedarían con el ritmo equivocado en
    algún tramo — y eso no lo arregla aceptar el riesgo.
    """
    videos = _pistas(fuente, "video")
    if not videos or cadencia(fuente) is None:
        return False
    if (fuente.video.modo_cadencia or "").upper() == "VFR":
        return False
    duraciones = {v.default_duration_ns for v in videos}
    return len(duraciones) == 1 and 0 not in duraciones


def _es_ass(p) -> bool:
    cid = (p.codec_id or "").upper()
    return "ASS" in cid or "SSA" in cid or "substation" in (p.codec or "").lower()


def _es_fuente_tipografica(a) -> bool:
    mime = (a.mime or "").lower()
    nombre = (a.nombre or "").lower()
    return mime.startswith(_MIME_DE_FUENTE) or nombre.endswith((".ttf", ".otf", ".ttc"))


# ═══════════════════════════════════════════════════════════════════════════
#  La selección por defecto
# ═══════════════════════════════════════════════════════════════════════════

def seleccion_por_defecto(fuentes) -> dict:
    """Lo que se le propone al usuario al crear el proyecto.

    Es el caso que lo originó: del MKV del vídeo, solo el vídeo; del otro,
    todo su audio y sus subtítulos en su orden y con sus marcas, y sus
    capítulos. Si el otro no trae audio (o subtítulos), se toman los del MKV
    del vídeo: un MKV final sin audio no es lo que nadie quiere, y uno que
    solo aporta subtítulos es justo el caso de «quiero los subtítulos de ese».
    """
    from models import MuxPista
    video, resto = fuentes[0], fuentes[1]
    pistas = []
    for tipo in ("audio", "subtitles"):
        origen = 1 if _pistas(resto, tipo) else 0
        for p in _pistas(fuentes[origen], tipo):
            pistas.append(MuxPista(fuente=origen, id=p.id, nombre=p.nombre,
                                   por_defecto=p.por_defecto, forzada=p.forzada))
    capitulos_de = ("resto" if resto.capitulos
                    else "video" if video.capitulos else "ninguno")
    return {"pistas": pistas, "capitulos_de": capitulos_de,
            "adjuntos": adjuntos_por_defecto(fuentes, pistas)}


def adjuntos_por_defecto(fuentes, pistas) -> list[dict]:
    """Las fuentes tipográficas de los MKV cuyos subtítulos ASS van incluidos.

    Nada más: una carátula o el JPG de un grupo de release no aportan nada al
    MKV final, y copiarlos por defecto —que es lo que hace la GUI de
    MKVToolNix— es como se cuelan.
    """
    con_ass = {p.fuente for p in pistas
               if (orig := _buscar(fuentes, p.fuente, p.id)) and _es_ass(orig)}
    return [{"fuente": i, "id": a.id} for i in sorted(con_ass)
            for a in fuentes[i].adjuntos if _es_fuente_tipografica(a)]


def _buscar(fuentes, fuente: int, tid: int):
    if not (0 <= fuente < len(fuentes)):
        return None
    return next((p for p in fuentes[fuente].pistas if p.id == tid), None)


# ═══════════════════════════════════════════════════════════════════════════
#  El nombre del MKV final
# ═══════════════════════════════════════════════════════════════════════════

_TAG_CMV4 = re.compile(r"\[(CMv4(?:\.0)?(?: (?:FULL|CORE\+?))?)\]", re.I)


def nombre_por_defecto(proyecto, titulo: str = "", anio: str = "") -> str:
    """El nombre con la regla de siempre (`phase_b.build_mkv_name`).

    Título y año: los que se le pasen (TMDb) o, si no, los del nombre del MKV
    del audio —que en un release de un tracker español trae el título en
    castellano— y si no, los del vídeo. Tags: `[DV FEL]` si el vídeo es FEL,
    `[Audio DCP]` si lo dice una pista de audio elegida o el nombre del MKV
    del audio, y el `[CMv4 …]` que traiga el nombre del MKV del vídeo, que es
    quien lleva el RPU.
    """
    from phases.phase_b import build_mkv_name
    from services.cmv40_recommend import parse_mkv_filename
    video, resto = proyecto.fuentes[0], proyecto.fuentes[1]
    if not titulo:
        # El mismo parser que nombra los trabajos de la columna: entiende los
        # nombres con puntos («Toy.Story.5.2026.UHD…») y corta los tags.
        for f in (resto, video):
            t, a = parse_mkv_filename(f.nombre)
            if t:
                titulo, anio = t, (anio or (str(a) if a else ""))
                break
    claves = _por_clave(proyecto)
    elegidas = [claves.get((p.fuente, p.id)) for p in proyecto.pistas]
    dcp = ("dcp" in resto.nombre.lower()
           or any(o is not None and o.tipo == "audio" and "dcp" in (o.nombre or "").lower()
                  for o in elegidas))
    fel = (video.video.dv_el or "").upper() == "FEL"
    nombre = build_mkv_name(titulo or video.nombre, anio or "0000", fel, dcp)
    m = _TAG_CMV4.search(video.nombre)
    if m:
        nombre = nombre[:-4] + f" [{m.group(1)}].mkv"
    return nombre


# ═══════════════════════════════════════════════════════════════════════════
#  La línea de tiempo
# ═══════════════════════════════════════════════════════════════════════════

def _fuentes_del_audio(proyecto) -> set[int]:
    claves = _por_clave(proyecto)
    return {p.fuente for p in proyecto.pistas
            if (o := claves.get((p.fuente, p.id))) is not None and o.tipo == "audio"}


def _huellas_iguales(a, b) -> bool:
    """Misma huella de contenido (SHA del primer MB + tamaño). La fecha no
    cuenta: mover un fichero no lo cambia. La comparación es la de la caché
    de Tab 2, que está centralizada para que no haya dos versiones de ella."""
    if not a or not b:
        return False
    from storage import _fingerprint_content_match
    return _fingerprint_content_match(a, b)


def plan(proyecto, *, ignorar_estado: bool = False) -> dict:
    """La línea de tiempo del MKV final y si se puede combinar ya.

    Devuelve:

      · `linea`: `video` o `resto` — qué MKV manda;
      · `alfa`: `[num, den]`, la relación de cadencias medida (1/1 si iguales);
      · `retimar_video`: `[num, den]` de la cadencia nueva, o None;
      · `sync`: `{"F:ID": {"retraso_ms", "factor"}}` de cada pista que se
        mueve (`factor` = `[num, den]` o None);
      · `capitulos`: `{de, retraso_ms, factor}`;
      · `motivos`: `[{id, params, saltable}]` de lo que impide combinar;
      · `puede_combinar`: sin motivos, o con todos saltables y el riesgo
        aceptado.

    `ignorar_estado` es para el runner: el proyecto que se está combinando
    está `running`, y ese motivo («ya está en marcha») es para quien quiere
    lanzarlo otra vez, no para la fase que lo comprueba.
    """
    s = proyecto.sincronia
    motivos: list[dict] = []

    def motivo(mid: str, **params):
        motivos.append({"id": mid, "params": params, "saltable": mid in SALTABLES})

    audio_de = _fuentes_del_audio(proyecto)
    if not audio_de:
        motivo("sin_audio")
    if proyecto.status in ("queued", "running") and not ignorar_estado:
        motivo("en_marcha")

    alfa = Fraction(1)
    beta_ms = 0
    veredicto = ""
    if s is None or s.estado in ("error", "cancelada"):
        motivo("sin_medida")
        beta_ms = -(proyecto.retraso_manual_ms or 0)
    elif s.estado == "midiendo":
        motivo("midiendo")
    else:
        veredicto = s.veredicto
        huellas = [f.huella for f in proyecto.fuentes]
        if len(s.huellas) != 2 or not all(_huellas_iguales(a, b)
                                          for a, b in zip(s.huellas, huellas)):
            motivo("medida_vieja")
        alfa = Fraction(s.alfa_num or 1, s.alfa_den or 1)
        beta_ms = int(s.beta_ms or 0)
        if veredicto == mux_sync.EN_SINCRONIA:
            beta_ms = 0
        elif veredicto == mux_sync.DERIVA:
            # Van a ritmos distintos que sus cadencias no explican. El plan es
            # el mismo que con «otro ritmo» —seguir al audio—, pero se pide
            # aceptarlo: una diferencia que nadie declara es rara, y antes de
            # retimar un vídeo por ella conviene que el usuario la vea.
            motivo("deriva", ppm=s.pendiente_ppm)
        elif veredicto == mux_sync.MONTAJE_DISTINTO:
            motivo("montaje_distinto",
                   desde=(s.salto_entre or [None, None])[0],
                   hasta=(s.salto_entre or [None, None])[1])
        elif veredicto == mux_sync.NO_CONCLUYENTE:
            motivo("no_concluyente", validos=s.n_validos, total=s.n_puntos)
            alfa = Fraction(1)
            beta_ms = -(proyecto.retraso_manual_ms or 0)

    # Por debajo de 10 ms un desfase es ruido de la medida (`mux_sync`), y
    # moverlo no arregla nada: con el ritmo cambiado el ajuste deja un β de
    # 3 ms en el caso real, que no es un retraso.
    if abs(beta_ms) < mux_sync.RETRASO_MINIMO_MS:
        beta_ms = 0
    # El retraso manual solo cuenta donde la medida no da uno: si la hay y
    # concluye, manda la medida (`retraso_manual_ms` se queda como estaba,
    # por si se vuelve a necesitar).
    cad_video = cadencia(proyecto.fuentes[0])
    cad_resto = cadencia(proyecto.fuentes[1])
    sync: dict[str, dict] = {}
    retimar = None
    linea = LINEA_DEL_VIDEO
    cap_factor: Fraction | None = None
    cap_retraso = 0

    def mover(fuente: int, retraso_ms: int, factor: Fraction | None):
        for p in proyecto.pistas:
            if p.fuente == fuente and (retraso_ms or (factor is not None and factor != 1)):
                sync[f"{p.fuente}:{p.id}"] = {
                    "retraso_ms": int(retraso_ms),
                    "factor": ([factor.numerator, factor.denominator]
                               if factor is not None and factor != 1 else None)}

    if alfa == 1:
        # El mismo ritmo: manda el del vídeo y lo del otro se retrasa.
        mover(1, -beta_ms, None)
        if proyecto.capitulos_de == "resto":
            cap_retraso = -beta_ms
    elif audio_de == {0, 1}:
        motivo("audio_de_dos_ritmos",
               video=mux_sync.texto_de_cadencia(cad_video),
               resto=mux_sync.texto_de_cadencia(cad_resto))
    elif audio_de == {1}:
        # Manda el audio del otro: el vídeo pasa a SU cadencia. El contenido
        # del segundo t del MKV del vídeo queda en α·t, y lo del otro, que
        # estaba en α·t + β, se adelanta β.
        linea = LINEA_DEL_RESTO
        if not video_retimable(proyecto.fuentes[0]):
            motivo("video_no_retimable")
        # La cadencia final es la del vídeo llevada al ritmo MEDIDO, que no
        # tiene por qué ser la declarada por el otro: en el caso real son
        # 24,00013 fps y no 24 (ver `mux_sync.ajustar`).
        retimar = cad_video / alfa if cad_video else None
        mover(1, -beta_ms, None)
        mover(0, 0, alfa)
        if proyecto.capitulos_de == "video":
            cap_factor = alfa
        elif proyecto.capitulos_de == "resto":
            cap_retraso = -beta_ms
    else:
        # Manda el audio del MKV del vídeo: lo del otro (sin audio) se lleva a
        # su línea. t_vídeo = (t_otro − β) / α.
        inversa = 1 / alfa
        mover(1, round(-beta_ms / float(alfa)), inversa)
        if proyecto.capitulos_de == "resto":
            cap_factor = inversa
            cap_retraso = round(-beta_ms / float(alfa))

    puede = (not motivos) or (proyecto.acepta_riesgo
                              and all(m["saltable"] for m in motivos))
    return {
        "linea": linea,
        "veredicto": veredicto,
        "alfa": [alfa.numerator, alfa.denominator],
        "beta_ms": beta_ms,
        "cadencia_video": _par(cad_video),
        "cadencia_resto": _par(cad_resto),
        "retimar_video": _par(retimar),
        "sync": sync,
        "capitulos": {"de": proyecto.capitulos_de, "retraso_ms": int(cap_retraso),
                      "factor": _par(cap_factor) if cap_factor not in (None, 1) else None},
        "audio_de": sorted(audio_de),
        "motivos": motivos,
        "puede_combinar": puede,
    }


def _par(f: Fraction | None):
    return [f.numerator, f.denominator] if f is not None else None


def _fr(par) -> Fraction | None:
    return Fraction(par[0], par[1]) if par else None


# ═══════════════════════════════════════════════════════════════════════════
#  Los capítulos
# ═══════════════════════════════════════════════════════════════════════════

def _ns_de(texto: str) -> int:
    """'01:23:45.678000000' → ns."""
    h, m, s = texto.strip().split(":")
    entero, _, frac = s.partition(".")
    frac = (frac + "000000000")[:9]
    return ((int(h) * 60 + int(m)) * 60 + int(entero)) * 1_000_000_000 + int(frac)


def _texto_de(ns: int) -> str:
    ns = max(0, int(ns))
    s, frac = divmod(ns, 1_000_000_000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{frac:09d}"


def mapear_capitulos_xml(xml: str, factor: Fraction | None, retraso_ms: int,
                         duracion_final_s: float | None = None) -> str:
    """El XML de capítulos de `mkvextract` con sus tiempos llevados a la línea
    de tiempo del MKV final: t·factor + retraso.

    Se trabaja sobre el árbol y solo se tocan los tiempos: nombres, idiomas,
    ediciones, capítulos anidados y marcas viajan tal cual — por eso se hace
    sobre el XML y no sobre la lista simplificada. Lo que el mapeo deja fuera
    de la película, se decide aquí (revisión de Codex del plan):

      · los capítulos que caerían ANTES del cero son de un tramo que el MKV
        final no tiene (el logo más largo del otro release): se quita todo
        menos el último de ellos, que pasa a empezar en el cero si no hay ya
        otro ahí — así la película sigue empezando en un capítulo;
      · los que caerían después del final del vídeo, fuera;
      · un `ChapterTimeEnd` que quede antes de su inicio, fuera (es opcional).

    Si no queda ningún capítulo en una edición, se quita la edición: una
    `EditionEntry` vacía la rechaza mkvmerge (ver el arnés).
    """
    import xml.etree.ElementTree as ET
    f = factor or Fraction(1)
    d = int(retraso_ms) * 1_000_000
    fin_ns = int(duracion_final_s * 1e9) if duracion_final_s else None

    def mapear(ns: int) -> int:
        return (ns * f.numerator) // f.denominator + d

    raiz = ET.fromstring(xml)
    for edicion in list(raiz.findall("EditionEntry")):
        atomos = edicion.findall("ChapterAtom")
        nuevos = []
        for atomo in atomos:
            nodo = atomo.find("ChapterTimeStart")
            if nodo is None or not (nodo.text or "").strip():
                nuevos.append((atomo, None))
                continue
            nuevos.append((atomo, mapear(_ns_de(nodo.text))))
        antes = [(a, t) for a, t in nuevos if t is not None and t < 0]
        en_cero = any(t is not None and 0 <= t < 500_000_000 for _, t in nuevos)
        rescatado = None if en_cero or not antes else max(antes, key=lambda x: x[1])[0]
        for atomo, t in nuevos:
            if t is None:
                continue
            if (t < 0 and atomo is not rescatado) or (fin_ns is not None and t >= fin_ns):
                edicion.remove(atomo)
                continue
            for elem in atomo.iter():
                if elem.tag in ("ChapterTimeStart", "ChapterTimeEnd") and (elem.text or "").strip():
                    elem.text = _texto_de(mapear(_ns_de(elem.text)))
            inicio = atomo.find("ChapterTimeStart")
            if atomo is rescatado:
                inicio.text = _texto_de(0)
            fin_ = atomo.find("ChapterTimeEnd")
            if fin_ is not None and _ns_de(fin_.text) <= _ns_de(inicio.text):
                atomo.remove(fin_)
        if not edicion.findall("ChapterAtom"):
            raiz.remove(edicion)
    return ET.tostring(raiz, encoding="unicode")


def capitulos_mapeados(xml: str) -> list[int]:
    """Los inicios (ns) de los capítulos de primer nivel de un XML, en orden.
    Es lo que la Fase C compara con el MKV final."""
    import xml.etree.ElementTree as ET
    raiz = ET.fromstring(xml)
    out = []
    for edicion in raiz.findall("EditionEntry"):
        for atomo in edicion.findall("ChapterAtom"):
            nodo = atomo.find("ChapterTimeStart")
            if nodo is not None and (nodo.text or "").strip():
                out.append(_ns_de(nodo.text))
    return out


def capitulos_necesitan_xml(plan_: dict) -> bool:
    """¿Hay que reescribir los capítulos? Solo si se mueven o se estiran."""
    c = plan_["capitulos"]
    return c["de"] != "ninguno" and bool(c["retraso_ms"] or c["factor"])


# ═══════════════════════════════════════════════════════════════════════════
#  El argv de mkvmerge
# ═══════════════════════════════════════════════════════════════════════════

def _flag(v: bool) -> str:
    return "yes" if v else "no"


def _sync_arg(tid: int, s: dict) -> str:
    arg = f"{tid}:{int(s['retraso_ms'])}"
    if s.get("factor"):
        arg += f",{s['factor'][0]}/{s['factor'][1]}"
    return arg


def _cadencia_arg(par) -> str:
    """El valor de `--default-duration`: «24p» o «24000/1001p» si la cadencia
    es estándar, y la duración exacta de un frame en ns si no lo es (una
    cadencia medida, como 24,00013)."""
    c = Fraction(par[0], par[1])
    if c in mux_sync._CADENCIAS:
        return f"{c.numerator}p" if c.denominator == 1 else f"{c.numerator}/{c.denominator}p"
    return f"{duracion_de_frame_ns(c)}ns"


def duracion_de_frame_ns(cadencia: Fraction) -> int:
    """Lo que dura un frame, en ns, como lo escribe mkvmerge."""
    return round(1_000_000_000 * cadencia.denominator / cadencia.numerator)


def titulo_del_contenedor(proyecto) -> str:
    return (proyecto.titulo or "").strip() or re.sub(r"\.mkv$", "", proyecto.nombre_salida or "",
                                                     flags=re.I)


def argv(proyecto, plan_: dict, salida: str, capitulos_xml: str | None = None) -> list[str]:
    """El comando de mkvmerge que produce el MKV final.

    Dos entradas, en el orden de las fuentes (el `--track-order` usa esos
    índices): la del vídeo con TODAS sus pistas de vídeo —una BL+EL combinada
    o las dos por separado, van juntas— y la del resto con `--no-video`. Cada
    pista elegida lleva su nombre y sus marcas explícitas, y su `--sync` si
    se mueve.
    """
    cmd = [MKVMERGE_BIN, "--gui-mode", "-o", salida,
           "--title", titulo_del_contenedor(proyecto)]
    if capitulos_xml:
        cmd += ["--chapters", capitulos_xml]
    claves = _por_clave(proyecto)
    for i, fuente in enumerate(proyecto.fuentes):
        elegidas = [p for p in proyecto.pistas
                    if p.fuente == i and (i, p.id) in claves]
        audio = [p.id for p in elegidas if claves[(i, p.id)].tipo == "audio"]
        subs = [p.id for p in elegidas if claves[(i, p.id)].tipo == "subtitles"]
        if i == 0:
            videos = [p.id for p in _pistas(fuente, "video")]
            cmd += ["--video-tracks", ",".join(map(str, videos))] if videos else ["--no-video"]
            if plan_.get("retimar_video"):
                # Solo el contenedor: mkvmerge 88 NO reescribe el VUI del HEVC
                # ni con `--fix-bitstream-timing-information` (medido en la BL
                # y en la EL de un P7 FEL, 2026-10-25), así que el bitstream
                # sigue declarando su cadencia de origen. Los reproductores de
                # MKV se guían por las marcas del contenedor, y la Fase G de
                # CMv4.0 pasa la duración de frame del contenedor para que un
                # remux posterior no deshaga esto.
                for vid in videos:
                    cmd += ["--default-duration",
                            f"{vid}:{_cadencia_arg(plan_['retimar_video'])}"]
        else:
            cmd += ["--no-video"]
        cmd += ["--audio-tracks", ",".join(map(str, audio))] if audio else ["--no-audio"]
        cmd += ["--subtitle-tracks", ",".join(map(str, subs))] if subs else ["--no-subtitles"]
        cap = plan_["capitulos"]
        de_aqui = {0: "video", 1: "resto"}[i]
        if capitulos_xml or cap["de"] != de_aqui:
            cmd += ["--no-chapters"]
        adj = [a["id"] for a in proyecto.adjuntos if a.get("fuente") == i]
        cmd += ["--attachments", ",".join(map(str, adj))] if adj else ["--no-attachments"]
        if i == 1:
            # Las etiquetas globales del MKV del audio describen OTRO release
            # (su IMDb puede coincidir, su grupo y sus notas no).
            cmd += ["--no-global-tags"]
        for p in elegidas:
            cmd += ["--track-name", f"{p.id}:{p.nombre}",
                    "--default-track", f"{p.id}:{_flag(p.por_defecto)}",
                    "--forced-track", f"{p.id}:{_flag(p.forzada)}"]
            s = plan_["sync"].get(f"{i}:{p.id}")
            if s:
                cmd += ["--sync", _sync_arg(p.id, s)]
        cmd.append(fuente.ruta)
    orden = [f"0:{v.id}" for v in _pistas(proyecto.fuentes[0], "video")]
    orden += [f"{p.fuente}:{p.id}" for p in proyecto.pistas]
    cmd += ["--track-order", ",".join(orden)]
    return cmd


# ═══════════════════════════════════════════════════════════════════════════
#  Lo que se espera del MKV final, y cuánto ocupará
# ═══════════════════════════════════════════════════════════════════════════

def tamano_estimado(proyecto) -> int | None:
    """Los bytes del MKV final: la suma de lo que ocupa cada pista elegida,
    más el vídeo, más un 0,5 % de contenedor.

    No es un modelo: mkvmerge copia los flujos tal cual, así que es
    contabilidad. **Una pista sin tamaño conocido cancela la cuenta** —es la
    regla del chip de Tab 1—, porque una suma a la que le falta un sumando
    tiene pinta de dato y no lo es.
    """
    claves = _por_clave(proyecto)
    total = 0
    for v in _pistas(proyecto.fuentes[0], "video"):
        if not v.bytes:
            return None
        total += v.bytes
    for p in proyecto.pistas:
        o = claves.get((p.fuente, p.id))
        if o is None or not o.bytes:
            return None
        total += o.bytes
    return int(total * 1.005)


def pistas_esperadas(proyecto, plan_: dict) -> list[dict]:
    """Las pistas que el MKV final tiene que traer, en orden, con lo que se
    puede comprobar de cada una en su `mkvmerge -J`."""
    claves = _por_clave(proyecto)
    out = []
    for v in _pistas(proyecto.fuentes[0], "video"):
        dd = v.default_duration_ns
        if plan_.get("retimar_video"):
            dd = duracion_de_frame_ns(Fraction(*plan_["retimar_video"]))
        out.append({"tipo": "video", "codec": v.codec, "idioma": v.idioma or "und",
                    "nombre": v.nombre, "default_duration_ns": dd, "frames": v.frames})
    for p in proyecto.pistas:
        o = claves.get((p.fuente, p.id))
        if o is None:
            continue
        out.append({"tipo": o.tipo, "codec": o.codec, "idioma": o.idioma or "und",
                    "nombre": p.nombre, "por_defecto": p.por_defecto,
                    "forzada": p.forzada, "fuente": p.fuente, "id": p.id})
    return out


def comprobar_pistas(esperadas: list[dict], salida_j: dict) -> list[dict]:
    """Lo que dice el `mkvmerge -J` del MKV final contra lo esperado.

    Una comprobación por pista y aspecto, con lo esperado y lo leído. Un
    número de pistas distinto es un fallo por sí solo: emparejar por
    posición con una de más o de menos compararía cada pista con su vecina.
    """
    reales = salida_j.get("tracks") or []
    checks = []
    if len(reales) != len(esperadas):
        checks.append({"id": "numero_de_pistas", "ok": False,
                       "esperado": len(esperadas), "real": len(reales)})
        return checks
    checks.append({"id": "numero_de_pistas", "ok": True,
                   "esperado": len(esperadas), "real": len(reales)})
    for n, (e, r) in enumerate(zip(esperadas, reales), start=1):
        p = r.get("properties") or {}
        pares = [("tipo", e["tipo"], r.get("type")),
                 ("codec", e["codec"], r.get("codec"))]
        if e["tipo"] != "video":
            pares += [("idioma", e["idioma"], p.get("language") or "und"),
                      ("nombre", e["nombre"], p.get("track_name") or ""),
                      ("por_defecto", e["por_defecto"], bool(p.get("default_track"))),
                      ("forzada", e["forzada"], bool(p.get("forced_track")))]
        else:
            dd_real = p.get("default_duration")
            if e.get("default_duration_ns") and dd_real:
                pares.append(("cadencia", e["default_duration_ns"],
                              int(dd_real) if abs(int(dd_real) - e["default_duration_ns"]) > 2
                              else e["default_duration_ns"]))
            frames_real = p.get("tag_number_of_frames")
            if e.get("frames") and frames_real not in (None, ""):
                pares.append(("frames", e["frames"], int(frames_real)))
        for aspecto, esp, real in pares:
            checks.append({"id": aspecto, "pista": n, "ok": esp == real,
                           "esperado": esp, "real": real})
    return checks
