"""
phases/mux_sync.py — Cuánto va desfasado un MKV respecto a otro, MEDIDO por
el audio.

Para qué existe
───────────────
La pestaña «Combinar MKV» junta el vídeo de un MKV con el audio y los
subtítulos de otro. Las dos fuentes casi nunca son el mismo fichero de origen
—un remux de Blu-ray y un WEB-DL—, así que lo que se reproduce en el segundo
X de una puede estar en el X±algo de la otra. El caso que dio origen a esto
(Toy Story 5, 2026-10-25) es el peor y el más instructivo:

  · el remux va a **23,976 fps** y el WEB-DL de HDO a **24,000 fps** (lo
    retimaron a la cadencia de cine para casar una pista DCP);
  · mezclados tal cual, el audio del WEB-DL se adelanta un 0,1 % del tiempo:
    −0,30 s en el minuto 5, −3,00 s en el 50, **−5,76 s en el 97**;
  · mirando las duraciones no se ve nada claro (el WEB-DL dura 1 min 46 s
    menos por los créditos) y los capítulos tampoco lo dirían.

Lo único que lo distingue es **medir el contenido**: la misma película suena
igual en los dos ficheros, así que la envolvente de su audio se puede
correlar y el desfase sale como el retardo del pico.

Cómo se mide
────────────
  1. Una pista de audio de cada MKV (mejor si son del mismo idioma: el mismo
     doblaje da picos de contraste 3-5×; entre idiomas también funciona, por
     la música y los efectos, con menos contraste).
  2. Ventanas de 30 s en siete puntos de la película. ffmpeg decodifica cada
     una a mono y 4 kHz, y aquí se reduce a una envolvente de 100 Hz: la
     log-energía en bloques de 10 ms, su subida positiva (dónde arranca un
     sonido) y la media restada. Eso no depende del códec, del nivel ni de la
     mezcla, que es lo que cambia entre un TrueHD y un DD+.
  3. **La envolvente del otro se lleva a la escala de tiempo del primero**
     antes de correlar. Si van a ritmos distintos (23,976 contra 24), lo que
     en uno ocupa 30 s en el otro ocupa 29,97: sin reescalar, los extremos de
     la ventana se separan 30 ms y la medida depende de dónde caiga el sonido
     dentro de ella; con PAL serían 1,2 s (revisión de Codex del plan).
  4. Correlación cruzada normalizada —centrada en cada tramo— por FFT. **En
     Python puro**: numpy no está en la imagen y no compensa añadirlo para
     esto; la búsqueda ancha (±60 s) son 32.768 puntos y unos segundos.
  5. Dos puntos «ancla» con búsqueda ancha dicen qué ritmo siguen los dos MKV
     —el mismo, el que declaran sus cadencias, u otro que nadie declara—, y
     el resto se busca cerca de donde ese ritmo los pone. Un punto que no se
     encuentra cerca se reintenta lejos: puede ser un corte de montaje.
  6. Con los puntos, la línea de tiempo: d(T) = (α − 1)·T + β.

Lo que NO hace
──────────────
No decide qué se hace con la medida: eso es `mux_plan`. Aquí solo se mide y
se describe lo medido (`veredicto`), con los números a la vista. Tampoco
escribe en el log: devuelve datos, y quien la llama cuenta lo que haga falta.
"""
from __future__ import annotations

import array
import asyncio
import cmath
import logging
import math
from fractions import Fraction

_logger = logging.getLogger(__name__)

FFMPEG_BIN = "ffmpeg"

# ── Los parámetros de la medida ─────────────────────────────────────────────
# Frecuencia de la decodificación. 4 kHz conserva lo que define un arranque
# de sonido (voz, golpes, música) y cuesta la mitad que 8 kHz; medido sobre
# Toy Story 5, los dos dan el mismo desfase con 0,2 ms de diferencia.
SR_HZ = 4000
# Muestras por bloque de la envolvente: 40 a 4 kHz = 10 ms = 100 Hz.
MUESTRAS_POR_BLOQUE = 40
ENVOLVENTE_HZ = SR_HZ // MUESTRAS_POR_BLOQUE
# Lo que dura la ventana del MKV de referencia.
VENTANA_S = 30.0
# Cuánto se busca a cada lado en el otro. Las anclas buscan lejos porque no
# se sabe nada (dos estudios con logos distintos pueden llevarse decenas de
# segundos); los demás ya saben dónde mirar y solo afinan.
MARGEN_ANCHO_S = 60.0
MARGEN_S = 6.0
# Dónde se mide, como fracción de la duración común. Siete y repartidos hasta
# el 88 %: un corte de escena entre dos puntos solo se ve si hay un punto a
# cada lado, y con cinco quedaban 20 minutos ciegos al final (revisión de
# Codex del plan, 2026-10-25). El último 12 % se evita porque los créditos
# son lo que más cambia entre dos versiones de la misma película.
PUNTOS = (0.05, 0.18, 0.32, 0.46, 0.60, 0.74, 0.88)
# Los dos puntos que se miden con búsqueda ancha antes que el resto: con dos
# desfases separados en el tiempo se sabe ya qué ritmo siguen los dos MKV.
ANCLAS = (0, 3)

# ── Cuándo un punto vale ────────────────────────────────────────────────────
# Correlación normalizada mínima del pico. En el caso real, del mismo idioma
# salieron 0,25-0,45; entre idiomas distintos, 0,11-0,29.
R_MINIMO = 0.08
# Cuánto más alto tiene que ser el pico que el segundo mejor. Es lo que
# distingue un pico de verdad de un parecido casual: en el caso real fue 3-5×
# en el mismo idioma y 1,2-3,6× entre doblajes distintos, donde UN punto se
# fue 110 ms con un contraste de 1,38.
CONTRASTE_MINIMO = 1.5
# El lóbulo del propio pico: lo que queda a menos de esto es el MISMO máximo
# visto con la resolución de la envolvente. El «segundo pico» es el mayor
# máximo local fuera de él — también cerca: dos golpes parecidos a 300 ms
# son justo la ambigüedad que hay que ver (con medio segundo de exclusión no
# se veía; revisión de Codex del plan).
LOBULO_S = 0.05
# Con menos de estos puntos válidos no se concluye nada.
PUNTOS_MINIMOS = 5

# ── Cuándo los puntos cuentan una historia coherente ────────────────────────
# Dispersión máxima de los residuos (máx − mín) para dar el desfase por
# constante: con 40, cada punto queda a ±20 ms del retraso que se aplica, que
# está por debajo de lo que se nota en los labios. Con 25 el caso real salía
# como «deriva» (revisión de Codex del plan: −1,2 / −7,3 / +2,1 / +7,6 /
# +32,0 ms en el prototipo).
DISPERSION_MAX_MS = 40.0
# Por debajo de esto un desfase es ruido de la medida, no un retraso.
RETRASO_MINIMO_MS = 10.0
# Si el ritmo medido está a menos de esto de la relación de cadencias, se usa
# la exacta (1000/1001 en vez de 0,999000 y pico): 2 ppm son 22 ms en tres
# horas, por debajo de la dispersión.
AJUSTE_PPM = 2.0
# Lo que el ritmo medido puede separarse del declarado para seguir siendo
# «el declarado, medido con precisión». Más allá no lo explican las
# cadencias y es una deriva.
OTRO_RITMO_MAX_PPM = 200.0

# Los veredictos. Describen lo MEDIDO; qué hacer con ello lo decide el plan.
EN_SINCRONIA = "en_sincronia"          # mismo ritmo, desfase despreciable
RETRASO = "retraso"                    # mismo ritmo, desfase constante
OTRO_RITMO = "otro_ritmo"              # cadencias distintas, explicado por ellas
DERIVA = "deriva"                      # se separan a un ritmo que nadie declara
MONTAJE_DISTINTO = "montaje_distinto"  # salto: escenas añadidas o quitadas
NO_CONCLUYENTE = "no_concluyente"      # no hay puntos suficientes de fiar

VEREDICTOS = (EN_SINCRONIA, RETRASO, OTRO_RITMO, DERIVA, MONTAJE_DISTINTO,
              NO_CONCLUYENTE)


# ═══════════════════════════════════════════════════════════════════════════
#  Cadencias
# ═══════════════════════════════════════════════════════════════════════════

# Las cadencias estándar, como fracciones exactas. mkvmerge da la duración
# de un frame en ns redondeada (41708333 para 24000/1001), y con el float se
# perdería lo que hace falta para escribir `--sync TID:0,1000/1001`.
_CADENCIAS = (Fraction(24000, 1001), Fraction(24), Fraction(25),
              Fraction(30000, 1001), Fraction(30), Fraction(48),
              Fraction(50), Fraction(60000, 1001), Fraction(60))


def cadencia_de(default_duration_ns) -> Fraction | None:
    """La cadencia exacta de una pista de vídeo a partir de su
    `default_duration` (ns por frame), o None si no se sabe.

    Se busca la estándar más cercana con una tolerancia de 1,5 ns por frame
    —el redondeo de mkvmerge—; si no es ninguna, se devuelve la fracción tal
    cual, que también se puede escribir en el argv.
    """
    try:
        dd = int(default_duration_ns)
    except (TypeError, ValueError):
        return None
    if dd <= 0:
        return None
    for c in _CADENCIAS:
        if abs(1_000_000_000 / c - dd) <= 1.5:
            return c
    return Fraction(1_000_000_000, dd).limit_denominator(100_000)


def texto_de_cadencia(c: Fraction | None) -> str:
    """«23.976» · «24» · «25», como lo diría alguien: sin el 1001."""
    if c is None:
        return "?"
    v = float(c)
    return f"{v:.3f}".rstrip("0").rstrip(".") if v % 1 else str(int(v))


# ═══════════════════════════════════════════════════════════════════════════
#  La señal
# ═══════════════════════════════════════════════════════════════════════════

def envolvente(muestras) -> list[float]:
    """La envolvente de arranques de un trozo de audio mono, a 100 Hz.

    Log-energía por bloque de 10 ms, su subida positiva y la media restada.
    La subida —y no la energía a secas— porque es lo que comparten dos
    mezclas distintas de la misma película: el instante en que algo empieza a
    sonar. El logaritmo la hace insensible al nivel.
    """
    n = MUESTRAS_POR_BLOQUE
    energias = []
    for i in range(0, len(muestras) - n + 1, n):
        acc = 0
        for v in muestras[i:i + n]:
            acc += v * v
        energias.append(math.log10(acc / n + 1.0))
    if not energias:
        return []
    subidas = [0.0] + [max(0.0, energias[i] - energias[i - 1])
                       for i in range(1, len(energias))]
    media = sum(subidas) / len(subidas)
    return [x - media for x in subidas]


def reescalar(señal: list[float], factor: float) -> list[float]:
    """`señal` muestreada en `k·factor` (interpolación lineal).

    Para llevar la envolvente del otro MKV a la escala de tiempo del primero:
    si el otro va a un ritmo α, lo que en el primero está u segundos después
    del inicio de la ventana, en el otro está α·u después. `reescalar(eb, α)`
    devuelve la envolvente del otro indexada en segundos del primero.
    """
    if factor == 1 or not señal:
        return list(señal)
    n = int((len(señal) - 1) / factor) + 1
    out = []
    for k in range(n):
        x = k * factor
        i = int(x)
        if i + 1 >= len(señal):
            out.append(señal[-1])
            continue
        f = x - i
        out.append(señal[i] * (1 - f) + señal[i + 1] * f)
    return out


def _fft(x: list, inversa: bool = False) -> list:
    """FFT radix-2 iterativa. `len(x)` tiene que ser potencia de dos."""
    n = len(x)
    x = list(x)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            x[i], x[j] = x[j], x[i]
    largo = 2
    while largo <= n:
        paso = cmath.exp((2j if inversa else -2j) * math.pi / largo)
        mitad = largo // 2
        for i in range(0, n, largo):
            w = 1
            for k in range(mitad):
                u = x[i + k]
                v = x[i + k + mitad] * w
                x[i + k] = u + v
                x[i + k + mitad] = u - v
                w *= paso
        largo <<= 1
    if inversa:
        return [v / n for v in x]
    return x


def correlar(ref: list[float], otro: list[float]) -> tuple[float, float, float]:
    """Dónde está `ref` dentro de `otro`.

    Devuelve `(desplazamiento, r, contraste)`:
      · `desplazamiento`, en muestras de envolvente y con decimales (pico
        interpolado), desde el principio de `otro`;
      · `r`, la correlación normalizada del pico (−1..1);
      · `contraste`, `r` entre el mayor máximo local fuera del lóbulo del
        pico (cerca o lejos: un eco a 300 ms es tan ambiguo como uno a 30 s).

    La correlación es la de Pearson de `ref` con cada tramo de `otro`: el
    tramo se centra en SU media (con sumas acumuladas, O(n)). Centrar `otro`
    con la media de la ventana entera penalizaba un tramo tranquilo junto a
    uno muy activo, que es donde suele estar el diálogo (revisión de Codex).
    """
    m, n_otro = len(ref), len(otro)
    if m == 0 or n_otro < m:
        return 0.0, 0.0, 0.0
    media_ref = sum(ref) / m
    ref = [v - media_ref for v in ref]
    norma_ref = math.sqrt(sum(v * v for v in ref))
    if norma_ref == 0:
        return 0.0, 0.0, 0.0
    n = 1
    while n < m + n_otro:
        n <<= 1
    fa = _fft(ref + [0.0] * (n - m))
    fb = _fft(list(otro) + [0.0] * (n - n_otro))
    cruzada = _fft([b * a.conjugate() for a, b in zip(fa, fb)], inversa=True)

    s1, s2 = [0.0], [0.0]
    for v in otro:
        s1.append(s1[-1] + v)
        s2.append(s2[-1] + v * v)
    valores = []
    for k in range(0, n_otro - m + 1):
        suma = s1[k + m] - s1[k]
        energia = (s2[k + m] - s2[k]) - suma * suma / m
        # Con `ref` centrada, Σ ref·(otro − media del tramo) = Σ ref·otro.
        valores.append(cruzada[k].real / (norma_ref * math.sqrt(energia))
                       if energia > 1e-12 else 0.0)
    mejor = max(range(len(valores)), key=valores.__getitem__)
    r = valores[mejor]
    lobulo = max(1, int(LOBULO_S * ENVOLVENTE_HZ))
    segundo = 0.0
    for i in range(1, len(valores) - 1):
        if abs(i - mejor) <= lobulo:
            continue
        v = valores[i]
        if v > segundo and v >= valores[i - 1] and v >= valores[i + 1]:
            segundo = v
    fraccion = 0.0
    if 0 < mejor < len(valores) - 1:
        y0, y1, y2 = valores[mejor - 1], valores[mejor], valores[mejor + 1]
        den = y0 - 2 * y1 + y2
        if den:
            fraccion = max(-0.5, min(0.5, 0.5 * (y0 - y2) / den))
    contraste = (r / segundo) if segundo > 0 else (99.0 if r > 0 else 0.0)
    return mejor + fraccion, r, contraste


def punto_valido(r: float, contraste: float) -> bool:
    """¿Este pico se sostiene?"""
    return r >= R_MINIMO and contraste >= CONTRASTE_MINIMO


# ═══════════════════════════════════════════════════════════════════════════
#  De los puntos a la línea de tiempo
# ═══════════════════════════════════════════════════════════════════════════

def _mediana(xs: list[float]) -> float:
    s = sorted(xs)
    k = len(s) // 2
    return s[k] if len(s) % 2 else (s[k - 1] + s[k]) / 2


def _centro(xs: list[float]) -> float:
    """El punto medio del rango: el retraso que deja MENOR el peor error.

    Es lo que importa en una sincronía de labios —que ningún tramo se vaya
    de los ±20 ms—, y no la mediana: con los residuos alternando ±19 ms la
    mediana elegía −19 y dejaba un punto a 38 ms. Los puntos raros ya los
    quitó la validez del pico y la dispersión los acota."""
    return (max(xs) + min(xs)) / 2


def residuos(puntos: list[dict], alfa) -> list[float]:
    """d(T) − (α − 1)·T, en ms."""
    a = float(alfa) - 1.0
    return [p["desfase_ms"] - a * p["t_a"] * 1000.0 for p in puntos]


def _recta(puntos: list[dict]) -> tuple[float, list[float]]:
    """La pendiente (ms por segundo) de la recta que mejor explica los puntos
    y lo que se separa cada uno de ella."""
    xs = [p["t_a"] for p in puntos]
    ys = [p["desfase_ms"] for p in puntos]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    pendiente = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx) if sxx else 0.0
    return pendiente, [y - (my + pendiente * (x - mx)) for x, y in zip(xs, ys)]


def ajustar(puntos: list[dict], alfa_anclas=None, *, alfa_declarada=None) -> dict:
    """La línea de tiempo que explican los puntos medidos.

    `puntos`: `[{t_a, desfase_ms, valido, …}]`, con `desfase_ms` = (dónde
    está en el otro) − (dónde está en el del vídeo) de lo mismo.

    El ritmo se AJUSTA a los puntos (recta de mínimos cuadrados), no se toma
    de las cadencias declaradas: en el caso real el WEB-DL va 5,5 ppm más
    lento de lo que 1000/1001 diría —34 ms a lo largo de la película—, y
    retimar a 24 fps exactos dejaba ±17 ms que la medida sí ve. Se ajusta a
    la declarada (o a 1) solo si la diferencia es menor de `AJUSTE_PPM`.

    Veredictos:
      · los puntos no están en una recta → `montaje_distinto` (un salto);
      · con α = 1 cuadran (dispersión ≤ 40 ms) → `en_sincronia` / `retraso`;
      · α cerca de la relación de cadencias declaradas → `otro_ritmo`;
      · α distinto de 1 sin cadencias que lo expliquen → `deriva`.

    `alfa_anclas` no decide nada aquí (decidió con qué escala se midió); se
    acepta por compatibilidad de llamada.
    """
    validos = [p for p in puntos if p.get("valido")]
    base = {"n_validos": len(validos), "n_puntos": len(puntos),
            "alfa_num": 1, "alfa_den": 1, "beta_ms": 0, "dispersion_ms": None,
            "pendiente_ppm": None, "salto_entre": None}
    if len(validos) < PUNTOS_MINIMOS:
        return {**base, "veredicto": NO_CONCLUYENTE}

    declarada = Fraction(alfa_declarada) if alfa_declarada is not None else Fraction(1)
    pendiente, libres = _recta(validos)            # ms por segundo
    if max(libres) - min(libres) > DISPERSION_MAX_MS:
        # Ninguna recta: hay un salto. El mayor entre dos puntos consecutivos
        # dice DÓNDE cambia el montaje — que es lo que el usuario puede ir a
        # comprobar con sus ojos.
        # El ritmo y el retraso que se devuelven son los de las cadencias
        # declaradas: es lo que se aplicaría si el usuario acepta el riesgo,
        # y con un salto en medio es lo único que tiene sentido.
        res = residuos(validos, declarada)
        saltos = [(abs(res[i + 1] - res[i]), i) for i in range(len(res) - 1)]
        _, i = max(saltos)
        return {**base, "veredicto": MONTAJE_DISTINTO,
                "alfa_num": declarada.numerator, "alfa_den": declarada.denominator,
                "beta_ms": round(_centro(res)),
                "dispersion_ms": round(max(libres) - min(libres), 1),
                "pendiente_ppm": round(pendiente * 1000.0, 1),
                "salto_entre": [round(validos[i]["t_a"]), round(validos[i + 1]["t_a"])]}

    def resultado(alfa, veredicto):
        res = residuos(validos, alfa)
        beta = _centro(res)
        if veredicto == RETRASO and abs(beta) < RETRASO_MINIMO_MS:
            veredicto = EN_SINCRONIA
        return {**base, "veredicto": veredicto,
                "alfa_num": alfa.numerator, "alfa_den": alfa.denominator,
                "beta_ms": round(beta), "dispersion_ms": round(max(res) - min(res), 1),
                "pendiente_ppm": round(pendiente * 1000.0, 1)}

    # El mismo ritmo, si con él cuadran: no se mueve nada más que un retraso.
    r1 = residuos(validos, 1)
    if max(r1) - min(r1) <= DISPERSION_MAX_MS:
        return resultado(Fraction(1), RETRASO)
    medida = Fraction(1 + pendiente / 1000.0).limit_denominator(10_000_000)
    ppm = abs(float(medida) - float(declarada)) * 1e6
    if declarada != 1 and ppm <= AJUSTE_PPM:
        return resultado(declarada, OTRO_RITMO)
    if declarada != 1 and ppm <= OTRO_RITMO_MAX_PPM:
        return resultado(medida, OTRO_RITMO)
    return resultado(medida, DERIVA)


def elegir_ritmo(anclas: list[dict], alfa_declarada) -> Fraction:
    """Qué ritmo siguen los dos MKV, a partir de las anclas.

    `anclas`: `[{t_a, por_ritmo: {alfa: punto}}]` — cada ancla medida con
    cada ritmo candidato. Con dos anclas válidas, la pendiente entre ellas
    dice el ritmo: se elige el candidato que la explica (1 o el de las
    cadencias declaradas) y, si ninguno, el que dicen las propias anclas. Con
    menos de dos, el de las cadencias: es lo mejor que se sabe.
    """
    candidatos = [Fraction(1)]
    if alfa_declarada is not None and Fraction(alfa_declarada) != 1:
        candidatos.append(Fraction(alfa_declarada))
    mejor = None
    for alfa in candidatos:
        pts = [a["por_ritmo"].get(alfa) for a in anclas]
        if not all(p and p.get("valido") for p in pts) or len(pts) < 2:
            continue
        (p1, p2) = pts[0], pts[-1]
        dt = p2["t_a"] - p1["t_a"]
        if dt <= 0:
            continue
        pendiente = (p2["desfase_ms"] - p1["desfase_ms"]) / dt      # ms/s
        # Lo que el candidato se equivoca ENTRE las dos anclas, en ms: es lo
        # que se puede comparar con la dispersión. Una pendiente sola, con
        # ±10 ms de ruido por punto, daría derivas que no existen.
        error_ms = abs(pendiente - (float(alfa) - 1.0) * 1000.0) * dt
        if mejor is None or error_ms < mejor[0]:
            mejor = (error_ms, alfa, pendiente)
    if mejor is None:
        return candidatos[-1]
    error_ms, alfa, pendiente = mejor
    # Ninguno de los dos ritmos con explicación casa: las anclas dicen otro.
    if error_ms > DISPERSION_MAX_MS:
        return Fraction(1 + pendiente / 1000.0).limit_denominator(1_000_000)
    return alfa


def posiciones(duracion_comun_s: float) -> list[float]:
    """Los segundos (en el tiempo del MKV del vídeo) donde arranca cada
    ventana."""
    return [max(0.0, round(f * duracion_comun_s - VENTANA_S / 2, 3))
            for f in PUNTOS]


# ═══════════════════════════════════════════════════════════════════════════
#  La pareja de pistas
# ═══════════════════════════════════════════════════════════════════════════

def _es_comentario(p: dict) -> bool:
    nombre = (p.get("nombre") or "").lower()
    return bool(p.get("comentario")) or "comment" in nombre or "comentari" in nombre


def pareja_de_referencia(audio_a: list[dict], audio_b: list[dict],
                         preferidos: tuple[str, ...] | list[str] = ()) -> tuple[int, int] | None:
    """Qué pista de cada MKV se compara: `(id_a, id_b)`, o None si a alguno le
    falta audio.

    Mejor del mismo idioma (el mismo doblaje da los picos más nítidos), sin
    comentarios del director y con más canales. Si no comparten idioma, la
    principal de cada uno: correlan por la música y los efectos, que en un
    doblaje son los mismos.

    De los idiomas que comparten, primero los de `preferidos` (ISO 639-2, en
    orden): el mismo idioma no es el mismo doblaje —el castellano de un disco
    y el de un WEB-DL suelen ser dos grabaciones, r ≈ 0,08 medido con Toy
    Story 5— y la versión original sí lo es. Sin preferencia manda el orden
    de A, que con A y B sin papeles depende de qué fichero se eligió primero:
    con el WEB-DL como A salía su doblaje, y la medida no concluía (prueba
    real del 2026-10-27).
    """
    def utiles(lista):
        return [p for p in lista if not _es_comentario(p)] or list(lista)

    a, b = utiles(audio_a), utiles(audio_b)
    if not a or not b:
        return None

    def clave(p):
        return (not p.get("por_defecto"), -(p.get("canales") or 0), p.get("id", 0))

    def idioma(p):
        return (p.get("idioma") or "und").lower()

    comunes = [l for l in dict.fromkeys(idioma(p) for p in sorted(a, key=clave))
               if l != "und" and any(idioma(q) == l for q in b)]
    if comunes:
        orden = {x: i for i, x in enumerate(preferidos or ())}
        comunes.sort(key=lambda x: orden.get(x, len(orden)))
        l = comunes[0]
        pa = sorted((p for p in a if idioma(p) == l), key=clave)[0]
        pb = sorted((p for p in b if idioma(p) == l), key=clave)[0]
        return pa["id"], pb["id"]
    return sorted(a, key=clave)[0]["id"], sorted(b, key=clave)[0]["id"]


# ═══════════════════════════════════════════════════════════════════════════
#  La medida, con ffmpeg
# ═══════════════════════════════════════════════════════════════════════════

class MedidaCancelada(Exception):
    """La medida se paró a petición: no es un fallo."""


# Una ventana de 30-150 s se decodifica en segundos; el tope existe para que
# un ffmpeg colgado no deje la medida esperando para siempre. Es una operación
# ACOTADA (ver la regla de timeouts): no escala con la película.
_TOPE_DECODIFICAR_S = 300


async def decodificar(ruta: str, audio_n: int, desde_s: float, duracion_s: float,
                      al_lanzar=None) -> array.array:
    """PCM mono de 16 bits a `SR_HZ` de un trozo de la `audio_n`-ésima pista de
    audio del fichero (`-map 0:a:N`: cuenta solo las de audio, en el orden
    del fichero, que es el mismo en que las numera mkvmerge).

    `al_lanzar` recibe el proceso, para poder matarlo si cancelan.
    """
    desde_s = max(0.0, desde_s)
    proc = await asyncio.create_subprocess_exec(
        FFMPEG_BIN, "-nostdin", "-v", "error",
        "-ss", f"{desde_s:.3f}", "-t", f"{duracion_s:.3f}",
        "-i", ruta, "-map", f"0:a:{audio_n}",
        "-ac", "1", "-ar", str(SR_HZ), "-f", "s16le", "-acodec", "pcm_s16le",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    if al_lanzar:
        al_lanzar(proc)
    try:
        salida, err = await asyncio.wait_for(proc.communicate(),
                                             timeout=_TOPE_DECODIFICAR_S)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg rc={proc.returncode}: "
                           f"{err.decode('utf-8', 'replace')[:300]}")
    muestras = array.array("h")
    muestras.frombytes(salida[: len(salida) // 2 * 2])
    return muestras


def _punto(t_a, desde_b, envol_a, envol_b, alfa) -> dict:
    """Un punto ya decodificado: `envol_b` empieza en `desde_b` (tiempo del
    otro MKV) y se reescala por α antes de correlar."""
    a = float(alfa)
    desplazamiento, r, contraste = correlar(envol_a, reescalar(envol_b, a))
    # `desplazamiento` está en muestras de la envolvente reescalada, o sea en
    # segundos del MKV del vídeo; en el otro, eso son α veces más.
    t_b = desde_b + a * desplazamiento / ENVOLVENTE_HZ
    return {"t_a": round(t_a, 3), "t_b": round(t_b, 3),
            "desfase_ms": round((t_b - t_a) * 1000.0, 1),
            "r": round(r, 3), "contraste": round(min(contraste, 99.0), 2),
            "valido": punto_valido(r, contraste)}


async def medir_punto(ruta_a: str, audio_a: int, ruta_b: str, audio_b: int,
                      t_a: float, centro_b: float, margen_s: float, ritmos,
                      al_lanzar=None) -> dict:
    """Un punto: la ventana del MKV del vídeo en `t_a` buscada en el otro
    alrededor de `centro_b`. Devuelve `{alfa: punto}` para cada ritmo de
    `ritmos`: la decodificación es la cara y se hace una vez."""
    sa = await decodificar(ruta_a, audio_a, t_a, VENTANA_S, al_lanzar)
    desde_b = max(0.0, centro_b - margen_s)
    sb = await decodificar(ruta_b, audio_b, desde_b,
                           VENTANA_S + (centro_b - desde_b) + margen_s, al_lanzar)

    # La correlación es CPU pura: a un thread, que el bucle de eventos no se
    # quede parado los segundos que tarda la FFT de una búsqueda ancha.
    def _calcular():
        ea, eb = envolvente(sa), envolvente(sb)
        return {Fraction(a): _punto(t_a, desde_b, ea, eb, a) for a in ritmos}
    return await asyncio.to_thread(_calcular)


def _fallido(t_a: float, error: str) -> dict:
    return {"t_a": round(t_a, 3), "t_b": None, "desfase_ms": None, "r": 0.0,
            "contraste": 0.0, "valido": False, "error": error[:200]}


async def medir(ruta_a: str, audio_a: int, ruta_b: str, audio_b: int, *,
                duracion_a_s: float, cadencia_a: Fraction | None,
                cadencia_b: Fraction | None, al_avanzar=None, al_lanzar=None,
                cancelado=None) -> dict:
    """Mide la línea de tiempo del otro MKV respecto al del vídeo.

    `audio_a`/`audio_b` son la posición de la pista entre las de AUDIO de su
    fichero (ver `decodificar`). `al_avanzar(hechos, total)` se llama tras
    cada punto; `cancelado()` se consulta antes de cada uno y, si devuelve
    True, se lanza `MedidaCancelada` — entre dos puntos es donde se puede
    parar sin dejar nada a medias.

    Devuelve el ajuste de `ajustar` más los puntos, para que se puedan
    enseñar: un veredicto sin sus números no se puede contrastar.
    """
    declarada = (Fraction(cadencia_a) / Fraction(cadencia_b)
                 if cadencia_a and cadencia_b else Fraction(1))
    ritmos = [Fraction(1)] + ([declarada] if declarada != 1 else [])
    ts = posiciones(duracion_a_s)
    total = len(ts)
    hechos = 0

    def avanzar():
        nonlocal hechos
        hechos += 1
        if al_avanzar:
            al_avanzar(hechos, total)

    def parar_si_cancelan():
        if cancelado and cancelado():
            raise MedidaCancelada()

    # ── 1. Las anclas: búsqueda ancha, con cada ritmo candidato ──────────
    anclas = []
    for i in ANCLAS:
        parar_si_cancelan()
        t = ts[i]
        try:
            # El centro, con el ritmo declarado: si mienten, el margen ancho
            # lo absorbe (60 s a cada lado frente a 6 s de deriva al final).
            por_ritmo = await medir_punto(ruta_a, audio_a, ruta_b, audio_b, t,
                                          float(declarada) * t, MARGEN_ANCHO_S,
                                          ritmos, al_lanzar)
        except RuntimeError as e:
            _logger.warning("[mux] ancla de sincronía en %.0f s sin medida: %s", t, e)
            por_ritmo = {}
        anclas.append({"t_a": t, "i": i, "por_ritmo": por_ritmo})
        avanzar()
    alfa = elegir_ritmo(anclas, declarada)

    puntos: list[dict | None] = [None] * total
    for a in anclas:
        p = a["por_ritmo"].get(alfa)
        puntos[a["i"]] = p if p else _fallido(a["t_a"], "sin_medida")
    beta_s = None
    validas = [puntos[a["i"]] for a in anclas if puntos[a["i"]].get("valido")]
    if validas:
        beta_s = _mediana([(p["t_b"] - float(alfa) * p["t_a"]) for p in validas])

    # ── 2. El resto: cerca de donde el ritmo elegido los pone ────────────
    for i, t in enumerate(ts):
        if puntos[i] is not None:
            continue
        parar_si_cancelan()
        p = None
        for margen in ((MARGEN_S, MARGEN_ANCHO_S) if beta_s is not None
                       else (MARGEN_ANCHO_S,)):
            centro = float(alfa) * t + (beta_s or 0.0)
            try:
                p = (await medir_punto(ruta_a, audio_a, ruta_b, audio_b, t, centro,
                                       margen, [alfa], al_lanzar))[alfa]
            except RuntimeError as e:
                p = _fallido(t, str(e))
            if p.get("valido"):
                break
        puntos[i] = p
        avanzar()

    medidos = [p for p in puntos if p.get("desfase_ms") is not None]
    ajuste = ajustar(medidos, alfa, alfa_declarada=declarada)
    return {**ajuste, "puntos": puntos}


# ═══════════════════════════════════════════════════════════════════════════
#  La comprobación del MKV ya combinado
# ═══════════════════════════════════════════════════════════════════════════

# Lo que puede desviarse el MKV final de lo planeado. Por encima de unos
# 45 ms un desfase de labios se empieza a notar; 40 deja la medida dentro.
RESIDUO_FINAL_MAX_MS = 40.0
# Puntos de la comprobación final, entre los de la medida: así cubren tramos
# que la medida no miró, y un signo cambiado o un factor mal escrito se ven
# en cualquiera.
PUNTOS_FINALES = (0.12, 0.39, 0.67, 0.81)


async def comprobar(ruta_ref: str, audio_ref: int, ruta_final: str,
                    audio_final: int, *, duracion_s: float, alfa,
                    al_lanzar=None, puntos=None) -> dict:
    """¿El audio del MKV final está donde el plan dice que tiene que estar?

    Compara una pista del MKV final con la de referencia del MKV del vídeo.
    Si el vídeo se retimó (`alfa` ≠ 1), lo esperado es d(T) = (α − 1)·T —el
    contenido del segundo T del original está en α·T del final—; si no, cero.
    Devuelve `{ok, puntos, residuo_max_ms}`; `ok` es None si no se pudo medir
    nada, que no es lo mismo que «está mal».
    """
    alfa = Fraction(alfa)
    a = float(alfa)
    fracciones = puntos or PUNTOS_FINALES
    puntos = []
    for f in fracciones:
        t = max(0.0, f * duracion_s - VENTANA_S / 2)
        try:
            p = (await medir_punto(ruta_ref, audio_ref, ruta_final, audio_final,
                                   t, a * t, MARGEN_S, [alfa], al_lanzar))[alfa]
        except RuntimeError as e:
            p = _fallido(t, str(e))
        if p.get("desfase_ms") is not None:
            p["esperado_ms"] = round((a - 1.0) * t * 1000.0, 1)
            p["residuo_ms"] = round(p["desfase_ms"] - p["esperado_ms"], 1)
        puntos.append(p)
    validos = [p for p in puntos if p.get("valido")]
    if len(validos) < 2:
        return {"ok": None, "puntos": puntos, "residuo_max_ms": None}
    peor = max(abs(p["residuo_ms"]) for p in validos)
    return {"ok": peor <= RESIDUO_FINAL_MAX_MS, "puntos": puntos,
            "residuo_max_ms": round(peor, 1)}
