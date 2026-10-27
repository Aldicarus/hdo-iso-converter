"""
phases/mux_plan.py — Qué se hace con dos MKV para multiplexarlos: la línea de
tiempo, los capítulos, el nombre de salida, los patrones de selección y el
argv de mkvmerge.

Es PURO —sin E/S ni subprocesos— por lo mismo que `cmv40_strategy`: así se
puede recorrer cada caso en un test de milisegundos, y la interfaz y el
pipeline leen el MISMO plan en vez de derivarlo cada uno por su cuenta.
No escribe textos: devuelve identificadores y números, y la interfaz los
traduce (`tab4.motivo_<id>`…). Un plan con frases dentro se congelaría en el
idioma del servidor.

Las dos fuentes, A y B, no tienen papeles: de cada una se elige lo que va al
MKV final. Lo que decide la línea de tiempo es DE DÓNDE sale cada cosa.

La regla que manda: la línea de tiempo del AUDIO
────────────────────────────────────────────────
El MKV final tiene una sola línea de tiempo. Las pistas se pueden mover
(`--sync TID:d`), y las de texto y el vídeo también se pueden estirar
(`--sync TID:d,o/p`, `--default-duration`) sin recodificar nada. El audio NO:
estirar sus marcas de tiempo sin remuestrear deja huecos o solapes entre
tramas, y un TrueHD Atmos no se recodifica.

La medida es siempre B respecto a A: el sonido que en A está en `t_A` está
en B en `t_B = α·t_A + β`. Sea **v** el MKV del que sale el vídeo y **o** el
otro; el mapeo de v a o es `t_o = f_vo·t_v + d_vo`, con `(f_vo, d_vo) =
(α, β)` si v = A y `(1/α, −β/α)` si v = B. Con eso:

  · de o no sale nada con tiempo (ni audio, ni subtítulos, ni capítulos) →
    no hace falta la medida;
  · misma velocidad (α = 1) → manda v; lo de o se mueve lo medido;
  · velocidades distintas y el audio sale de v → manda v; lo de o se lleva a
    su línea (`t_v = (t_o − d_vo) / f_vo`);
  · velocidades distintas y todo el audio sale de o → manda o: la VELOCIDAD
    del vídeo se ajusta (su duración de frame, `--default-duration`), lo de v
    con tiempo se estira con él (`f_vo`) y lo de o se mueve `−d_vo`. Es el
    caso que lo originó (Toy Story 5, remux a 23,976 + WEB-DL a 24): Dolby
    Vision P7 FEL intacto y residuo de +1 / +8 ms;
  · velocidades distintas y audio de LOS DOS → no hay plan posible sin
    recodificar: uno de los dos audios iría a otra velocidad. Se dice y no se
    multiplexa.

**El vídeo nunca lleva `--sync`**: solo cambia su duración de frame. Con el
vídeo en la lista de pistas, un `--sync` con factor encima del
`--default-duration` lo estiraría dos veces (revisión de Codex del plan,
2026-10-27).

mkvmerge aplica `--sync TID:d,o/p` como t·(o/p) + d (factor primero,
desplazamiento después), y así se escribe aquí.
"""
from __future__ import annotations

import copy
import re
from fractions import Fraction

from phases import mux_sync

MKVMERGE_BIN = "mkvmerge"

# Las secciones de la pestaña, en el orden en que se pintan. Son ids: la
# interfaz los traduce.
SECCIONES = ("video", "audio", "subtitulos", "capitulos", "otros")
_TIPO_DE_SECCION = {"video": "video", "audio": "audio", "subtitulos": "subtitles"}
_ORDEN_DE_TIPO = {"video": 0, "audio": 1, "subtitles": 2}

# Los motivos por los que no se puede multiplexar todavía. Identificadores: el
# texto lo pone la interfaz (`tab4.motivo_<id>`, con los `params` que lleven);
# el servidor tiene su forma corta (`mux.motivo_corto_<id>`) para el 409 y la
# explicación del relato (`relato.mux_porque_<id>`).
MOTIVOS = (
    "sin_video",                 # no se ha elegido ningún vídeo
    "video_de_las_dos",          # vídeo de los dos MKV (el PUT no lo deja)
    "sin_audio",                 # el MKV final no llevaría ninguna pista de audio
    "midiendo",                  # la medida de sincronía está en marcha
    "sin_medida",                # no hay medida (o falló, o se canceló)
    "medida_vieja",              # la medida es de otros ficheros
    "audio_de_dos_ritmos",       # velocidades distintas y audio de los dos MKV
    "deriva",                    # se separan a un ritmo que nadie declara
    "montaje_distinto",          # salto: escenas añadidas o quitadas
    "no_concluyente",            # la medida no dio puntos suficientes
    "video_sin_velocidad_fija",  # hay que ajustar la velocidad y no es fija
    "en_marcha",                 # ya se está multiplexando (o espera turno)
)

# Los que el usuario puede pasar por alto con «multiplexar igualmente». Los que
# no están aquí no tienen salida que no sea cambiar algo: audio de dos
# velocidades da un MKV en el que la mitad de las pistas se desincroniza, y eso
# no lo arregla aceptar el riesgo.
SALTABLES = frozenset({"sin_medida", "medida_vieja", "deriva",
                       "montaje_distinto", "no_concluyente"})

# Los patrones de cada sección. `todo_a`/`nada_a` (y los de B) son los de la
# cabecera de cada columna; los demás, los de la sección entera.
PATRONES = {
    "video": ("todo_a", "todo_b", "nada_a", "nada_b"),
    "audio": ("todo_a", "todo_b", "nada_a", "nada_b", "mis_idiomas"),
    "subtitulos": ("todo_a", "todo_b", "nada_a", "nada_b", "mis_idiomas", "solo_forzados"),
    "capitulos": ("todo_a", "todo_b", "nada_a", "nada_b"),
    "otros": ("todo_a", "todo_b", "nada_a", "nada_b", "fuentes_de_los_ass"),
}

# Tipos MIME de una fuente tipográfica. Un subtítulo ASS/SSA las necesita y
# sin ellas el reproductor cae a una genérica.
_MIME_DE_FUENTE = ("font/", "application/x-truetype-font",
                   "application/vnd.ms-opentype", "application/x-font",
                   "application/font-sfnt")

# Un capítulo que, ya mapeado, cae a menos de esto del cero cuenta como el que
# abre la película (y no se rescata otro de antes).
_CERO_NS = 500_000_000


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


def video_ajustable(fuente) -> bool:
    """Si al vídeo de esta fuente se le puede ajustar la velocidad.

    Ajustarla es escribir UNA duración de frame para todo el vídeo
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


def _es_comentario(o) -> bool:
    nombre = (o.nombre or "").lower()
    return (bool(o.comentario) or "comment" in nombre or "comentari" in nombre
            or (o.idioma or "").lower() == "qad")


def _es_forzado(o) -> bool:
    nombre = (o.nombre or "").lower()
    return bool(o.forzada) or "forz" in nombre or "forced" in nombre


def duracion_de_video(fuente) -> float:
    """Lo que dura el vídeo: frames / cadencia si se saben, y si no el
    contenedor (que puede alargarse por un subtítulo o un audio más largo)."""
    v = fuente.video
    if v.frames and v.cadencia_num:
        return v.frames * v.cadencia_den / v.cadencia_num
    return fuente.duracion_s


# ═══════════════════════════════════════════════════════════════════════════
#  Qué sale de cada fuente
# ═══════════════════════════════════════════════════════════════════════════

def _elegidas(proyecto, tipo: str | None = None) -> list:
    """Las `MuxPista` elegidas que existen en su fuente (y de ese tipo)."""
    claves = _por_clave(proyecto)
    return [p for p in proyecto.pistas
            if (o := claves.get((p.fuente, p.id))) is not None
            and (tipo is None or o.tipo == tipo)]


def _fuentes_de(proyecto, tipo: str) -> set[int]:
    return {p.fuente for p in _elegidas(proyecto, tipo)}


def fuentes_del_video(proyecto) -> set[int]:
    return _fuentes_de(proyecto, "video")


def fuente_del_video(proyecto) -> int | None:
    """La fuente del vídeo elegido, o None si no hay ninguno (o hay de las
    dos, que el PUT no deja guardar)."""
    fuentes = fuentes_del_video(proyecto)
    return next(iter(fuentes)) if len(fuentes) == 1 else None


def _capitulo_origen(proyecto, c):
    if not (0 <= c.fuente < len(proyecto.fuentes)):
        return None
    return next((x for x in proyecto.fuentes[c.fuente].capitulos
                 if x.edicion == c.edicion and x.indice == c.indice), None)


def _capitulos_elegidos(proyecto) -> list[tuple]:
    """`[(MuxCapitulo, MuxCapituloOrigen)]` de lo elegido que existe."""
    out = []
    for c in proyecto.capitulos:
        o = _capitulo_origen(proyecto, c)
        if o is not None:
            out.append((c, o))
    return out


def _fuentes_con_tiempo(proyecto) -> set[int]:
    """De qué fuentes sale algo que vive en la línea de tiempo: audio,
    subtítulos o capítulos. Los adjuntos no tienen tiempo, y unos capítulos
    elegidos de una lista vacía no cuentan (revisión de Codex)."""
    return (_fuentes_de(proyecto, "audio") | _fuentes_de(proyecto, "subtitles")
            | {c.fuente for c, _ in _capitulos_elegidos(proyecto)})


def necesita_sincronia(proyecto) -> bool:
    """¿Hace falta la medida para multiplexar lo elegido?

    Solo si algo con tiempo sale del MKV que NO da el vídeo. Todo de A, o el
    vídeo y el audio de A con los adjuntos de B, no necesita medir nada. Sin
    un vídeo único, la medida hace falta si hay cosas con tiempo de los dos.
    """
    con_tiempo = _fuentes_con_tiempo(proyecto)
    v = fuente_del_video(proyecto)
    if v is None:
        return len(con_tiempo) > 1
    return bool(con_tiempo - {v})


# ═══════════════════════════════════════════════════════════════════════════
#  La línea de tiempo
# ═══════════════════════════════════════════════════════════════════════════

def _mapeo_v_a_o(alfa: Fraction, beta_ms: Fraction, v: int) -> tuple[Fraction, Fraction]:
    """`(f, d)` con `t_o = f·t_v + d` (d en ms), a partir de la medida
    `t_B = α·t_A + β`."""
    if v == 0:
        return alfa, beta_ms
    return 1 / alfa, -beta_ms / alfa


def _huellas_iguales(a, b) -> bool:
    """Misma huella de contenido (SHA del primer MB + tamaño). La fecha no
    cuenta: mover un fichero no lo cambia. La comparación es la de la caché
    de Tab 2, que está centralizada para que no haya dos versiones de ella."""
    if not a or not b:
        return False
    from storage import _fingerprint_content_match
    return _fingerprint_content_match(a, b)


def _entero(x) -> int:
    return int(round(float(x)))


def plan(proyecto, *, ignorar_estado: bool = False) -> dict:
    """La línea de tiempo del MKV final y si se puede multiplexar ya.

    Devuelve:

      · `video_de`: la fuente del vídeo (0, 1 o None); `audio_de`: de cuáles
        sale audio; `necesita_sincronia`; `linea_de`: qué MKV manda;
      · `alfa` (`[num, den]`) y `beta_ms`: lo que se aplica de la medida;
      · `velocidad_video`: `{de, a}` (cadencias `[num, den]`) si se ajusta;
      · `sync`: `{"F:ID": {retraso_ms, factor}}` de cada pista que se mueve
        o se estira — nunca el vídeo;
      · `capitulos`: `{lista, mapeos}`: los elegidos ya llevados a la línea
        final, en orden, con lo que queda fuera marcado;
      · `avisos`: secciones con elementos y nada elegido (no bloquean);
      · `motivos`: `[{id, params, saltable}]` de lo que impide multiplexar;
      · `puede_multiplexar`: sin motivos, o con todos saltables y el riesgo
        aceptado.

    `ignorar_estado` es para el runner: el proyecto que se está multiplexando
    está `running`, y ese motivo («ya está en marcha») es para quien quiere
    lanzarlo otra vez, no para la fase que lo comprueba.
    """
    s = proyecto.sincronia
    motivos: list[dict] = []

    def motivo(mid: str, **params):
        motivos.append({"id": mid, "params": params, "saltable": mid in SALTABLES})

    videos = fuentes_del_video(proyecto)
    v = next(iter(videos)) if len(videos) == 1 else None
    if not videos:
        motivo("sin_video")
    elif len(videos) > 1:
        motivo("video_de_las_dos")
    audio_de = _fuentes_de(proyecto, "audio")
    if not audio_de:
        motivo("sin_audio")
    if proyecto.status in ("queued", "running") and not ignorar_estado:
        motivo("en_marcha")
    necesita = necesita_sincronia(proyecto)

    alfa = Fraction(1)
    beta_ms = Fraction(0)
    veredicto = ""
    if necesita:
        manual = False
        if s is None or s.estado in ("error", "cancelada"):
            motivo("sin_medida")
            manual = True
        elif s.estado == "midiendo":
            motivo("midiendo")
        else:
            veredicto = s.veredicto
            huellas = [f.huella for f in proyecto.fuentes]
            if len(s.huellas) != 2 or not all(_huellas_iguales(a, b)
                                              for a, b in zip(s.huellas, huellas)):
                motivo("medida_vieja")
            alfa = Fraction(s.alfa_num or 1, s.alfa_den or 1)
            beta_ms = Fraction(int(s.beta_ms or 0))
            if veredicto == mux_sync.EN_SINCRONIA:
                beta_ms = Fraction(0)
            elif veredicto == mux_sync.DERIVA:
                # Van a velocidades distintas que sus cadencias no explican. El
                # plan es el mismo que con velocidades distintas —seguir al
                # audio—, pero se pide aceptarlo: una diferencia que nadie
                # declara es rara, y antes de cambiarle la velocidad a un vídeo
                # por ella conviene que el usuario la vea.
                motivo("deriva", ppm=s.pendiente_ppm)
            elif veredicto == mux_sync.MONTAJE_DISTINTO:
                motivo("montaje_distinto",
                       desde=(s.salto_entre or [None, None])[0],
                       hasta=(s.salto_entre or [None, None])[1])
            elif veredicto == mux_sync.NO_CONCLUYENTE:
                motivo("no_concluyente", validos=s.n_validos, total=s.n_puntos)
                alfa = Fraction(1)
                manual = True
        if manual:
            # Lo que el usuario escribe se aplica tal cual: el umbral de ruido
            # es para lo MEDIDO (un retraso manual de 9 ms se anulaba;
            # revisión de Codex).
            beta_ms = Fraction(-(proyecto.retraso_manual_ms or 0))
        elif abs(beta_ms) < mux_sync.RETRASO_MINIMO_MS:
            # Por debajo de 10 ms un desfase medido es ruido, y moverlo no
            # arregla nada: con la velocidad cambiada el ajuste deja un β de
            # 3 ms en el caso real, que no es un retraso.
            beta_ms = Fraction(0)
    elif s is not None and s.estado == "medida":
        veredicto = s.veredicto

    cads = [cadencia(f) for f in proyecto.fuentes]
    sync: dict[str, dict] = {}
    mapeos: dict[int, tuple[Fraction | None, Fraction]] = {}
    velocidad = None
    linea_de = v

    claves = _por_clave(proyecto)

    def mover(fuente: int, retraso_ms: Fraction, factor: Fraction | None):
        if factor is not None and factor == 1:
            factor = None
        for p in _elegidas(proyecto):
            o = claves[(p.fuente, p.id)]
            if p.fuente != fuente or o.tipo == "video":
                continue
            if _entero(retraso_ms) or factor is not None:
                sync[f"{p.fuente}:{p.id}"] = {
                    "retraso_ms": _entero(retraso_ms),
                    "factor": [factor.numerator, factor.denominator] if factor is not None else None}
        mapeos[fuente] = (factor, retraso_ms)

    if v is not None and necesita:
        o = 1 - v
        f_vo, d_vo = _mapeo_v_a_o(alfa, beta_ms, v)
        f_ov, d_ov = 1 / f_vo, -d_vo / f_vo
        if alfa == 1:
            # La misma velocidad: manda v, y lo de o se mueve lo medido.
            mover(o, d_ov, None)
        elif audio_de == {0, 1}:
            motivo("audio_de_dos_ritmos",
                   a=mux_sync.texto_de_cadencia(cads[0]),
                   b=mux_sync.texto_de_cadencia(cads[1]))
        elif audio_de <= {v}:
            # Manda el audio del MKV del vídeo: lo de o se lleva a su línea.
            mover(o, d_ov, f_ov)
        else:
            # Todo el audio es de o: el vídeo pasa a SU velocidad. El contenido
            # del segundo t de v queda en f_vo·t, y lo de o, que estaba en
            # f_vo·t + d_vo, se mueve −d_vo. La velocidad final es la medida, no
            # la declarada por o: en el caso real son 24,00013 fps y no 24 (ver
            # `mux_sync.ajustar`).
            linea_de = o
            if not video_ajustable(proyecto.fuentes[v]):
                motivo("video_sin_velocidad_fija")
            if cads[v]:
                velocidad = (cads[v], cads[v] / f_vo)
            mover(o, -d_vo, None)
            mover(v, Fraction(0), f_vo)

    duracion = duracion_final(proyecto, {"video_de": v, "velocidad_video": (
        {"de": _par(velocidad[0]), "a": _par(velocidad[1])} if velocidad else None)})
    lista = capitulos_finales(proyecto, mapeos, duracion)

    puede = (not motivos) or (proyecto.acepta_riesgo
                              and all(m["saltable"] for m in motivos))
    return {
        "video_de": v,
        "audio_de": sorted(audio_de),
        "necesita_sincronia": necesita,
        "linea_de": linea_de,
        "veredicto": veredicto,
        "alfa": [alfa.numerator, alfa.denominator],
        "beta_ms": _entero(beta_ms),
        "cadencias": [_par(c) for c in cads],
        "velocidad_video": ({"de": _par(velocidad[0]), "a": _par(velocidad[1])}
                            if velocidad else None),
        "sync": sync,
        "capitulos": {
            "lista": lista,
            "mapeos": {str(f): {"factor": _par(fa) if fa not in (None, 1) else None,
                                "retraso_ms": _entero(r)}
                       for f, (fa, r) in sorted(mapeos.items())},
        },
        "avisos": avisos_de_secciones(proyecto),
        "motivos": motivos,
        "puede_multiplexar": puede,
    }


def _par(f: Fraction | None):
    return [f.numerator, f.denominator] if f is not None else None


def _fr(par) -> Fraction | None:
    return Fraction(par[0], par[1]) if par else None


def duracion_final(proyecto, plan_: dict) -> float | None:
    """Lo que durará el vídeo del MKV final, en segundos: los frames del
    vídeo elegido a la velocidad final, o su duración escalada si no se saben
    los frames (revisión de Codex: sin frames, la duración original dejaba
    fuera capítulos que sí caben). None sin vídeo."""
    v = plan_.get("video_de")
    if v is None or not (0 <= v < len(proyecto.fuentes)):
        return None
    fuente = proyecto.fuentes[v]
    original = duracion_de_video(fuente)
    vel = plan_.get("velocidad_video")
    if vel:
        de, a = _fr(vel["de"]), _fr(vel["a"])
        if de and a:
            return float(original * de / a)
    return original


def avisos_de_secciones(proyecto) -> list[dict]:
    """Las secciones donde hay algo que elegir y no se ha elegido nada.

    No bloquean: no llevar subtítulos puede ser lo que se quiere. Pero el
    proyecto nace sin nada elegido y se va sección a sección, así que antes de
    multiplexar se pregunta por lo que se haya quedado vacío, para no tener que
    repetir un multiplexado de veinte minutos por un descuido (decisión del
    usuario, 2026-10-27). El vídeo y el audio vacíos ya son motivos.
    """
    out = []
    hay_subs = any(_pistas(f, "subtitles") for f in proyecto.fuentes)
    if hay_subs and not _elegidas(proyecto, "subtitles"):
        out.append({"id": "seccion_vacia", "params": {"seccion": "subtitulos"}})
    if any(f.capitulos for f in proyecto.fuentes) and not _capitulos_elegidos(proyecto):
        out.append({"id": "seccion_vacia", "params": {"seccion": "capitulos"}})
    if any(f.adjuntos for f in proyecto.fuentes) and not adjuntos_validos(proyecto):
        out.append({"id": "seccion_vacia", "params": {"seccion": "otros"}})
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  La selección: normalizar y aplicar patrones
# ═══════════════════════════════════════════════════════════════════════════

def _pista_de(fuente: int, o):
    from models import MuxPista
    return MuxPista(fuente=fuente, id=o.id, nombre=o.nombre or "",
                    por_defecto=bool(o.por_defecto), forzada=bool(o.forzada))


def normalizar_pistas(proyecto, pistas: list) -> list:
    """La lista de pistas como la quiere el MKV final.

      · sin pistas que no existan ni repetidas (se queda la primera);
      · en el orden de tipos vídeo → audio → subtítulos, sin cambiar el
        orden dentro de cada uno: un reproductor que enumera por posición no
        los mezcla, y así llega al `--track-order`;
      · una sola marca «por defecto» por tipo: la primera que la lleve. Dos
        audios por defecto y el reproductor elige el primero, que no tiene
        por qué ser el que se quería.
    """
    claves = _por_clave(proyecto)
    vistas, validas = set(), []
    for p in pistas:
        k = (p.fuente, p.id)
        if k in vistas or k not in claves:
            continue
        vistas.add(k)
        validas.append(p)
    validas.sort(key=lambda p: _ORDEN_DE_TIPO[claves[(p.fuente, p.id)].tipo])
    con_defecto = set()
    out = []
    for p in validas:
        tipo = claves[(p.fuente, p.id)].tipo
        q = p.model_copy()
        if q.por_defecto:
            if tipo in con_defecto:
                q.por_defecto = False
            con_defecto.add(tipo)
        out.append(q)
    return out


def normalizar_capitulos(proyecto, capitulos: list) -> list:
    vistos, out = set(), []
    for c in capitulos:
        k = (c.fuente, c.edicion, c.indice)
        if k in vistos or _capitulo_origen(proyecto, c) is None:
            continue
        vistos.add(k)
        out.append(c)
    return out


def adjuntos_validos(proyecto, adjuntos: list | None = None) -> list[dict]:
    validos = {(i, a.id) for i, f in enumerate(proyecto.fuentes) for a in f.adjuntos}
    vistos, out = set(), []
    for a in (proyecto.adjuntos if adjuntos is None else adjuntos):
        k = (a.get("fuente"), a.get("id"))
        if k in vistos or k not in validos:
            continue
        vistos.add(k)
        out.append({"fuente": k[0], "id": k[1]})
    return out


# ISO 639-2 → 639-1 de los idiomas que más salen en un disco, para casar el
# `original_language` de TMDb (dos letras) y el perfil de la app con las
# pistas, que traen las dos formas (`idioma` y la subetiqueta de `idioma_ietf`).
_ISO639_1 = {
    "spa": "es", "eng": "en", "cat": "ca", "fre": "fr", "fra": "fr", "ger": "de",
    "deu": "de", "ita": "it", "por": "pt", "jpn": "ja", "kor": "ko", "chi": "zh",
    "zho": "zh", "rus": "ru", "dut": "nl", "nld": "nl", "swe": "sv", "nor": "no",
    "dan": "da", "fin": "fi", "pol": "pl", "tur": "tr", "gre": "el", "ell": "el",
    "heb": "he", "hin": "hi", "tha": "th", "cze": "cs", "ces": "cs", "hun": "hu",
    "eus": "eu", "baq": "eu", "glg": "gl", "ara": "ar",
}


def idiomas_del_usuario(idioma_app: str | None, lengua_original: str | None) -> set[str]:
    """Los idiomas del patrón «Mis idiomas»: los del perfil de la app
    (`phase_b.IDIOMAS_DEL_PERFIL`, el mismo que decide las pistas de Tab 1) y
    la versión original, la de TMDb o el inglés si no se sabe. En las dos
    formas, ISO 639-2 y 639-1, porque las pistas traen las dos."""
    from phases.phase_a import ISO639_TO_ENGLISH
    from phases.phase_b import idiomas_preferidos
    nombres = {n.lower() for n in idiomas_preferidos(idioma_app)}
    codigos = {c for c, n in ISO639_TO_ENGLISH.items() if n.lower() in nombres}
    vo = (lengua_original or "en").lower()
    codigos |= {c for c, dos in _ISO639_1.items() if dos == vo}
    return codigos | {_ISO639_1[c] for c in codigos if c in _ISO639_1} | {vo}


def _habla(o, idiomas: set[str]) -> bool:
    codigo = (o.idioma or "").lower()
    ietf = (o.idioma_ietf or "").lower().split("-")[0]
    return codigo in idiomas or (bool(ietf) and ietf in idiomas)


def aplicar_patron(proyecto, seccion: str, patron: str,
                   idiomas: set[str] | frozenset = frozenset()) -> dict:
    """Lo que queda elegido en la sección tras aplicarle un patrón.

    Devuelve el campo entero que cambia (`pistas`, `capitulos` o
    `adjuntos`), ya normalizado. Las demás secciones no se tocan, y lo que ya
    estaba elegido conserva su nombre, sus marcas y su sitio: un patrón añade
    al final de su tipo. Lanza `ValueError` con un patrón que no existe.

    · `todo_a` / `todo_b`: añade todo lo de esa fuente (en el vídeo, lo
      CAMBIA: el vídeo sale de un solo MKV);
    · `nada_a` / `nada_b`: quita lo de esa fuente;
    · `mis_idiomas`: la sección pasa a ser lo de los idiomas del usuario, de
      los dos MKV, sin comentarios ni audiodescripción;
    · `solo_forzados`: la sección pasa a ser los subtítulos forzados;
    · `fuentes_de_los_ass`: los adjuntos pasan a ser las fuentes tipográficas
      de los MKV cuyos subtítulos ASS van elegidos.
    """
    if patron not in PATRONES.get(seccion, ()):
        raise ValueError(f"{seccion}:{patron}")
    fuente = {"a": 0, "b": 1}.get(patron[-1]) if patron[:5] in ("todo_", "nada_") else None

    if seccion in _TIPO_DE_SECCION:
        tipo = _TIPO_DE_SECCION[seccion]
        claves = _por_clave(proyecto)
        actuales = normalizar_pistas(proyecto, proyecto.pistas)
        del_tipo = [p for p in actuales if claves[(p.fuente, p.id)].tipo == tipo]
        resto = [p for p in actuales if claves[(p.fuente, p.id)].tipo != tipo]
        if patron.startswith("todo_"):
            nuevas = [] if tipo == "video" else list(del_tipo)
            if tipo == "video":
                # El que ya estaba elegido de esa misma fuente conserva su
                # nombre y sus marcas.
                nuevas = [p for p in del_tipo if p.fuente == fuente]
            ya = {(p.fuente, p.id) for p in nuevas}
            nuevas += [_pista_de(fuente, o) for o in _pistas(proyecto.fuentes[fuente], tipo)
                       if (fuente, o.id) not in ya]
        elif patron.startswith("nada_"):
            nuevas = [p for p in del_tipo if p.fuente != fuente]
        else:
            if patron == "mis_idiomas":
                quiere = lambda o: _habla(o, set(idiomas)) and not _es_comentario(o)
            else:   # solo_forzados
                quiere = _es_forzado
            por_clave = {(p.fuente, p.id): p for p in del_tipo}
            candidatas = [(i, o) for i, f in enumerate(proyecto.fuentes)
                          for o in _pistas(f, tipo) if quiere(o)]
            # Las que ya estaban, en su orden; detrás, las nuevas.
            nuevas = [p for p in del_tipo if any((i, o.id) == (p.fuente, p.id)
                                                 for i, o in candidatas)]
            nuevas += [_pista_de(i, o) for i, o in candidatas if (i, o.id) not in por_clave]
        return {"pistas": normalizar_pistas(proyecto, resto + nuevas)}

    if seccion == "capitulos":
        from models import MuxCapitulo
        actuales = normalizar_capitulos(proyecto, proyecto.capitulos)
        if patron.startswith("todo_"):
            ya = {(c.fuente, c.edicion, c.indice) for c in actuales}
            actuales += [MuxCapitulo(fuente=fuente, edicion=x.edicion, indice=x.indice)
                         for x in proyecto.fuentes[fuente].capitulos
                         if (fuente, x.edicion, x.indice) not in ya]
        else:
            actuales = [c for c in actuales if c.fuente != fuente]
        return {"capitulos": normalizar_capitulos(proyecto, actuales)}

    # seccion == "otros"
    actuales = adjuntos_validos(proyecto)
    if patron.startswith("todo_"):
        ya = {(a["fuente"], a["id"]) for a in actuales}
        actuales += [{"fuente": fuente, "id": a.id} for a in proyecto.fuentes[fuente].adjuntos
                     if (fuente, a.id) not in ya]
    elif patron.startswith("nada_"):
        actuales = [a for a in actuales if a["fuente"] != fuente]
    else:   # fuentes_de_los_ass
        claves = _por_clave(proyecto)
        con_ass = {p.fuente for p in _elegidas(proyecto, "subtitles")
                   if _es_ass(claves[(p.fuente, p.id)])}
        actuales = [{"fuente": i, "id": a.id} for i in sorted(con_ass)
                    for a in proyecto.fuentes[i].adjuntos if _es_fuente_tipografica(a)]
    return {"adjuntos": adjuntos_validos(proyecto, actuales)}


# ═══════════════════════════════════════════════════════════════════════════
#  El nombre del MKV final
# ═══════════════════════════════════════════════════════════════════════════

_TAG_CMV4 = re.compile(r"\[(CMv4(?:\.0)?(?: (?:FULL|CORE\+?))?)\]", re.I)


def nombre_por_defecto(proyecto, titulo: str = "", anio: str = "") -> str:
    """El nombre con la regla de siempre (`phase_b.build_mkv_name`).

    Título y año: los que se le pasen (la ficha de TMDb, en el idioma de la
    app) o, si no, los del nombre de A y después los de B. Ya no hay un «MKV
    del doblaje» del que fiarse para el título en castellano.

    Los tags salen de LO ELEGIDO, no de los ficheros (revisión de Codex): el
    `[DV FEL]` y el `[CMv4 …]`, del MKV del que sale el vídeo; el `[Audio
    DCP]`, de una pista de audio elegida que lo diga o del nombre de un MKV
    del que sale audio. Por eso el nombre se recalcula al cambiar la
    selección, salvo que el usuario lo haya editado.
    """
    from phases.phase_b import _sanitize_for_path, build_mkv_name
    from services.cmv40_recommend import parse_mkv_filename
    if not titulo:
        # El mismo parser que nombra los trabajos de la columna: entiende los
        # nombres con puntos («Toy.Story.5.2026.UHD…») y corta los tags.
        for f in proyecto.fuentes:
            t, a = parse_mkv_filename(f.nombre)
            if t:
                titulo, anio = t, (anio or (str(a) if a else ""))
                break
    claves = _por_clave(proyecto)
    v = fuente_del_video(proyecto)
    fv = proyecto.fuentes[v] if v is not None else None
    audio = [(p.fuente, claves[(p.fuente, p.id)]) for p in _elegidas(proyecto, "audio")]
    dcp = (any("dcp" in (o.nombre or "").lower() for _, o in audio)
           or any("dcp" in proyecto.fuentes[f].nombre.lower() for f in {f for f, _ in audio}))
    fel = fv is not None and (fv.video.dv_el or "").upper() == "FEL"
    # Saneado: un título de TMDb puede traer «/» («Face/Off») y acabaría como
    # un subdirectorio de Output.
    nombre = build_mkv_name(_sanitize_for_path(titulo or proyecto.fuentes[0].nombre),
                            anio or "0000", fel, dcp)
    m = _TAG_CMV4.search(fv.nombre) if fv is not None else None
    if m:
        nombre = nombre[:-4] + f" [{m.group(1)}].mkv"
    return nombre


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


def _mapear_ns(ns: int, factor: Fraction | None, retraso_ms) -> int:
    f = factor or Fraction(1)
    return round(Fraction(ns) * f + Fraction(retraso_ms) * 1_000_000)


def capitulos_finales(proyecto, mapeos: dict, duracion_s: float | None) -> list[dict]:
    """Los capítulos elegidos llevados a la línea de tiempo del MKV final, en
    orden: `[{fuente, edicion, indice, nombre, inicio_ns, queda}]`.

    `queda` es `ok`, `al_cero` o `fuera`. Lo que el mapeo deja fuera de la
    película se decide aquí, sobre la lista YA mezclada (revisión de Codex del
    plan del 2026-10-25):

      · lo que caería después del final del vídeo, fuera;
      · lo que caería ANTES del cero es de un tramo que el MKV final no tiene
        (el logo más largo del otro release): fuera todo menos el último de
        ellos, que pasa a empezar en el cero si no hay ya otro ahí — así la
        película sigue empezando en un capítulo.
    """
    fin_ns = int(duracion_s * 1e9) if duracion_s else None
    filas = []
    for c, o in _capitulos_elegidos(proyecto):
        factor, retraso = mapeos.get(c.fuente, (None, 0))
        filas.append({"fuente": c.fuente, "edicion": c.edicion, "indice": c.indice,
                      "nombre": o.nombre, "inicio_ns": _mapear_ns(o.inicio_ns, factor, retraso),
                      "queda": "ok"})
    filas.sort(key=lambda x: (x["inicio_ns"], x["fuente"], x["edicion"], x["indice"]))
    antes = [x for x in filas if x["inicio_ns"] < 0]
    en_cero = any(0 <= x["inicio_ns"] < _CERO_NS for x in filas)
    rescatado = antes[-1] if antes and not en_cero else None
    for x in filas:
        if x is rescatado:
            x["inicio_ns"], x["queda"] = 0, "al_cero"
        elif x["inicio_ns"] < 0 or (fin_ns is not None and x["inicio_ns"] >= fin_ns):
            x["queda"] = "fuera"
    filas.sort(key=lambda x: (x["inicio_ns"], x["fuente"], x["edicion"], x["indice"]))
    return filas


def capitulos_nativos(proyecto, plan_: dict) -> int | None:
    """La fuente cuyos capítulos pueden ir TAL CUAL (los copia mkvmerge), o
    None si hay que componerlos.

    Solo cuando se eligen todos los de una fuente, de ninguna otra, esa
    fuente no se mueve ni se estira y ninguno cae fuera del vídeo: entonces
    copiarlos es lo más fiel —sus ediciones, sus UID, su orden— y no hace
    falta reescribir nada. Con los capítulos de un MKV y el vídeo de otro más
    corto, el último puede caer detrás del final: copiado tal cual entraba
    igual, y la Fase C lo esperaba (revisión de Codex del 2026-10-27).
    """
    elegidos = _capitulos_elegidos(proyecto)
    fuentes = {c.fuente for c, _ in elegidos}
    if len(fuentes) != 1:
        return None
    f = next(iter(fuentes))
    if len(elegidos) != len(proyecto.fuentes[f].capitulos):
        return None
    caps = plan_.get("capitulos") or {}
    m = caps.get("mapeos", {}).get(str(f))
    if m and (m.get("factor") or m.get("retraso_ms")):
        return None
    if any(x["queda"] != "ok" for x in caps.get("lista") or []):
        return None
    return f


def capitulos_de_xml(xml: str) -> list[dict]:
    """Los capítulos de PRIMER nivel de un XML de Matroska, de todas sus
    ediciones: `[{edicion, indice, inicio_ns, fin_ns, nombre, idioma, oculto,
    hijos}]`. Es lo que la pestaña enseña de cada fuente y lo que la Fase C
    compara con el MKV final."""
    import xml.etree.ElementTree as ET
    raiz = ET.fromstring(xml)
    out = []
    for e, edicion in enumerate(raiz.findall("EditionEntry")):
        for i, atomo in enumerate(edicion.findall("ChapterAtom")):
            inicio = (atomo.findtext("ChapterTimeStart") or "").strip()
            if not inicio:
                continue
            fin = (atomo.findtext("ChapterTimeEnd") or "").strip()
            display = atomo.find("ChapterDisplay")
            out.append({
                "edicion": e, "indice": i,
                "inicio_ns": _ns_de(inicio),
                "fin_ns": _ns_de(fin) if fin else None,
                "nombre": ((display.findtext("ChapterString") if display is not None else "") or "").strip(),
                "idioma": ((display.findtext("ChapterLanguage") if display is not None else "") or "").strip(),
                "oculto": (atomo.findtext("ChapterFlagHidden") or "0").strip() == "1",
                "hijos": len(list(atomo.iter("ChapterAtom"))) - 1,
            })
    return out


def _ns_firmado(texto: str) -> int:
    """Como `_ns_de`, pero admite el «-» que se pone a un tiempo que el mapeo
    lleva antes del cero (`_texto_firmado`)."""
    t = (texto or "").strip()
    return -_ns_de(t[1:]) if t.startswith("-") else _ns_de(t)


def _texto_firmado(ns: int) -> str:
    return _texto_de(ns) if ns >= 0 else "-" + _texto_de(-ns)


def _recortar_atomo(atomo, fin_ns: int | None) -> None:
    """Dentro de un capítulo ya mapeado, quita los anidados que caen fuera
    del vídeo —antes del cero o desde el final— y los `ChapterTimeEnd` que
    quedan antes de su inicio (es opcional). Recursivo: un anidado de un
    anidado se recorta igual (revisión de Codex)."""
    for hijo in list(atomo.findall("ChapterAtom")):
        inicio = _ns_firmado(hijo.findtext("ChapterTimeStart") or "00:00:00")
        if inicio < 0 or (fin_ns is not None and inicio >= fin_ns):
            atomo.remove(hijo)
        else:
            _recortar_atomo(hijo, fin_ns)
    fin = atomo.find("ChapterTimeEnd")
    if fin is not None and (_ns_firmado(fin.text) <= _ns_firmado(atomo.findtext("ChapterTimeStart"))):
        atomo.remove(fin)


_UIDS = ("ChapterUID", "ChapterSegmentUID", "ChapterSegmentEditionUID", "EditionUID")


def componer_capitulos_xml(xmls: dict[int, str], proyecto, plan_: dict,
                           duracion_s: float | None) -> tuple[str, list[int]]:
    """El XML de capítulos del MKV final con lo elegido de las dos fuentes, y
    los inicios (ns) de primer nivel que tiene que traer.

    Cada capítulo elegido se copia de SU XML con todo lo que lleva —nombres
    en varios idiomas, anidados, la marca de oculto— y solo se tocan los
    tiempos, con el mapeo de su fuente. Tres cosas que no son obvias:

      · **se quitan los UID** (`ChapterUID`, `EditionUID`) y las referencias
        a otros segmentos: mkvmerge pone UID nuevos a lo que no los trae, y un
        UID repetido —un WEB-DL suele copiar los capítulos del disco— hace
        que se COMA un capítulo sin decir nada (medido con la v88 del NAS,
        2026-10-27);
      · los anidados se recortan igual que los de primer nivel: los que caen
        fuera del vídeo se quitan (revisión de Codex);
      · una sola edición, en el orden de la línea final.

    Lanza `RuntimeError` si un capítulo elegido ya no está donde se leyó: el
    fichero ha cambiado (la Fase A lo habría cazado por la huella).
    """
    import xml.etree.ElementTree as ET
    fin_ns = int(duracion_s * 1e9) if duracion_s else None
    mapeos = {int(k): (_fr(m.get("factor")), m.get("retraso_ms") or 0)
              for k, m in ((plan_.get("capitulos") or {}).get("mapeos") or {}).items()}
    lista = [x for x in (plan_.get("capitulos") or {}).get("lista") or []
             if x["queda"] != "fuera"]
    arboles = {f: ET.fromstring(x) for f, x in xmls.items()}
    origenes = {(c.fuente, c.edicion, c.indice): o for c, o in _capitulos_elegidos(proyecto)}
    raiz = ET.Element("Chapters")
    edicion = ET.SubElement(raiz, "EditionEntry")
    esperados = []
    for x in lista:
        clave = (x["fuente"], x["edicion"], x["indice"])
        try:
            atomo = (arboles[x["fuente"]].findall("EditionEntry")[x["edicion"]]
                     .findall("ChapterAtom")[x["indice"]])
        except (KeyError, IndexError):
            raise RuntimeError("capitulo:%d:%d:%d" % clave) from None
        origen = origenes.get(clave)
        if origen is None or _ns_de(atomo.findtext("ChapterTimeStart") or "00:00:00") != origen.inicio_ns:
            raise RuntimeError("capitulo:%d:%d:%d" % clave)
        nuevo = copy.deepcopy(atomo)
        for padre in list(nuevo.iter()):
            for hijo in list(padre):
                if hijo.tag in _UIDS:
                    padre.remove(hijo)
        factor, retraso = mapeos.get(x["fuente"], (None, 0))
        for nodo in nuevo.iter():
            if nodo.tag in ("ChapterTimeStart", "ChapterTimeEnd") and (nodo.text or "").strip():
                nodo.text = _texto_firmado(_mapear_ns(_ns_de(nodo.text), factor, retraso))
        # El de primer nivel, donde lo dejó la lista: un rescatado al cero
        # empieza en el cero aunque su tiempo mapeado fuera negativo.
        nuevo.find("ChapterTimeStart").text = _texto_de(x["inicio_ns"])
        _recortar_atomo(nuevo, fin_ns)
        edicion.append(nuevo)
        esperados.append(x["inicio_ns"])
    return ET.tostring(raiz, encoding="unicode"), esperados


def capitulos_esperados(proyecto, plan_: dict) -> list[int]:
    """Los inicios (ns) de primer nivel que tiene que traer el MKV final."""
    nativos = capitulos_nativos(proyecto, plan_)
    if nativos is not None:
        return sorted(c.inicio_ns for c in proyecto.fuentes[nativos].capitulos)
    return [x["inicio_ns"] for x in (plan_.get("capitulos") or {}).get("lista") or []
            if x["queda"] != "fuera"]


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


def entradas(proyecto, plan_: dict, con_xml: bool = False) -> list[int]:
    """Las fuentes que aportan algo, en su orden: son las entradas de
    mkvmerge. Una que no aporta nada no se le pasa —mkvmerge leería su
    cabecera para no copiar nada— y por eso el `--track-order` usa el puesto
    de cada una en ESTA lista, no su índice. Con los capítulos en un XML
    compuesto, una fuente que solo aporta capítulos ya no hace falta."""
    elegidas = {p.fuente for p in _elegidas(proyecto)}
    caps = set() if con_xml else {c.fuente for c, _ in _capitulos_elegidos(proyecto)}
    adj = {a["fuente"] for a in adjuntos_validos(proyecto)}
    return [i for i in range(len(proyecto.fuentes)) if i in (elegidas | caps | adj)]


def argv(proyecto, plan_: dict, salida: str, capitulos_xml: str | None = None) -> list[str]:
    """El comando de mkvmerge que produce el MKV final.

    Las fuentes que aportan algo, en orden. Cada una lleva sus pistas
    elegidas —el vídeo con su duración de frame nueva si se ajusta la
    velocidad—, sus capítulos solo si van tal cual, sus adjuntos elegidos y
    las etiquetas globales solo la del vídeo (las del otro describen OTRO
    release: su IMDb puede coincidir, su grupo y sus notas no). Cada pista
    lleva su nombre y sus marcas explícitas, y su `--sync` si se mueve.
    """
    cmd = [MKVMERGE_BIN, "--gui-mode", "-o", salida,
           "--title", titulo_del_contenedor(proyecto)]
    if capitulos_xml:
        cmd += ["--chapters", capitulos_xml]
    claves = _por_clave(proyecto)
    v = plan_.get("video_de")
    nativos = None if capitulos_xml else capitulos_nativos(proyecto, plan_)
    adjuntos = adjuntos_validos(proyecto)
    puesto = {}
    for n, i in enumerate(entradas(proyecto, plan_, con_xml=bool(capitulos_xml))):
        puesto[i] = n
        fuente = proyecto.fuentes[i]
        elegidas = [p for p in proyecto.pistas if p.fuente == i and (i, p.id) in claves]
        de = lambda tipo: [p.id for p in elegidas if claves[(i, p.id)].tipo == tipo]
        videos, audio, subs = de("video"), de("audio"), de("subtitles")
        cmd += ["--video-tracks", ",".join(map(str, videos))] if videos else ["--no-video"]
        if videos and plan_.get("velocidad_video"):
            # Solo el contenedor: mkvmerge 88 NO reescribe el VUI del HEVC ni
            # con `--fix-bitstream-timing-information` (medido en la BL y en
            # la EL de un P7 FEL, 2026-10-25), así que el bitstream sigue
            # declarando su cadencia de origen. Los reproductores de MKV se
            # guían por las marcas del contenedor, y la Fase G de CMv4.0 pasa
            # la duración de frame del contenedor para que un remux posterior
            # no deshaga esto.
            for vid in videos:
                cmd += ["--default-duration",
                        f"{vid}:{_cadencia_arg(plan_['velocidad_video']['a'])}"]
        cmd += ["--audio-tracks", ",".join(map(str, audio))] if audio else ["--no-audio"]
        cmd += ["--subtitle-tracks", ",".join(map(str, subs))] if subs else ["--no-subtitles"]
        if nativos != i:
            cmd += ["--no-chapters"]
        adj = [a["id"] for a in adjuntos if a["fuente"] == i]
        cmd += ["--attachments", ",".join(map(str, adj))] if adj else ["--no-attachments"]
        if i != v:
            cmd += ["--no-global-tags"]
        for p in elegidas:
            cmd += ["--track-name", f"{p.id}:{p.nombre}",
                    "--default-track", f"{p.id}:{_flag(p.por_defecto)}",
                    "--forced-track", f"{p.id}:{_flag(p.forzada)}"]
            s = plan_["sync"].get(f"{i}:{p.id}")
            if s and claves[(i, p.id)].tipo != "video":
                cmd += ["--sync", _sync_arg(p.id, s)]
        cmd.append(fuente.ruta)
    orden = [f"{puesto[p.fuente]}:{p.id}" for p in proyecto.pistas
             if (p.fuente, p.id) in claves and p.fuente in puesto]
    if orden:
        cmd += ["--track-order", ",".join(orden)]
    return cmd


# ═══════════════════════════════════════════════════════════════════════════
#  Lo que se espera del MKV final, y cuánto ocupará
# ═══════════════════════════════════════════════════════════════════════════

def tamano_estimado(proyecto) -> int | None:
    """Los bytes del MKV final: la suma de lo que ocupa cada pista elegida
    —el vídeo incluido— y cada adjunto elegido, más un 0,5 % de contenedor.

    No es un modelo: mkvmerge copia los flujos tal cual, así que es
    contabilidad. **Una pista sin tamaño conocido cancela la cuenta** —es la
    regla del chip de Tab 1—, porque una suma a la que le falta un sumando
    tiene pinta de dato y no lo es. Sin nada elegido, tampoco hay cifra.
    """
    claves = _por_clave(proyecto)
    elegidas = _elegidas(proyecto)
    if not elegidas:
        return None
    total = 0
    for p in elegidas:
        o = claves[(p.fuente, p.id)]
        if not o.bytes:
            return None
        total += o.bytes
    adjuntos = {(i, a.id): a.tamano for i, f in enumerate(proyecto.fuentes) for a in f.adjuntos}
    total += sum(adjuntos.get((a["fuente"], a["id"]), 0) for a in adjuntos_validos(proyecto))
    return int(total * 1.005)


def pistas_esperadas(proyecto, plan_: dict) -> list[dict]:
    """Las pistas que el MKV final tiene que traer, en orden, con lo que se
    puede comprobar de cada una en su `mkvmerge -J`."""
    claves = _por_clave(proyecto)
    vel = plan_.get("velocidad_video")
    out = []
    for p in _elegidas(proyecto):
        o = claves[(p.fuente, p.id)]
        e = {"tipo": o.tipo, "codec": o.codec, "idioma": o.idioma or "und",
             "nombre": p.nombre, "por_defecto": p.por_defecto,
             "forzada": p.forzada, "fuente": p.fuente, "id": p.id}
        if o.tipo == "video":
            e["default_duration_ns"] = (duracion_de_frame_ns(_fr(vel["a"])) if vel
                                        else o.default_duration_ns)
            e["frames"] = o.frames
        out.append(e)
    return out


def comprobar_pistas(esperadas: list[dict], salida_j: dict) -> list[dict]:
    """Lo que dice el `mkvmerge -J` del MKV final contra lo esperado.

    Una comprobación por pista y aspecto, con lo esperado y lo leído. Un
    número de pistas distinto es un fallo por sí solo: emparejar por
    posición con una de más o de menos compararía cada pista con su vecina.
    El vídeo se comprueba entero —nombre y marcas también—: ahora es una
    pista que se elige y se edita como las demás.
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
                 ("codec", e["codec"], r.get("codec")),
                 ("idioma", e["idioma"], p.get("language") or "und"),
                 ("nombre", e["nombre"], p.get("track_name") or ""),
                 ("por_defecto", e["por_defecto"], bool(p.get("default_track"))),
                 ("forzada", e["forzada"], bool(p.get("forced_track")))]
        if e["tipo"] == "video":
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
