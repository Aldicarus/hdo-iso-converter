"""
luminance.py — El perfil de luminancia DV L1 de un MKV.

Vivía dentro del endpoint `POST /api/mkv/light-profile` de `main.py`: 668
líneas y una complejidad ciclomática de 170, la peor función del repositorio y
además un route handler. Aquí queda como funciones puras que se pueden ejecutar
en un test y, sobre todo, REUSAR: el análisis extendido de Tab 2 necesita este
perfil y los combos L8 del mismo RPU, y extraerlo cuesta ~97 % del tiempo total
(medido: ~650 s de extracción frente a ~7 s de export y segundos de parseo), así
que no tiene sentido pagarlo dos veces.

El único consumidor del export del RPU es `perfil_desde_niveles`, que come el
formato PLANO de `rpu_analyze.export_levels`. El volcado anidado del camino
legacy (dovi_tool anterior a 2.3.3) se ADAPTA con `niveles_desde_volcado` en vez
de parsearse aparte — la regla de "un solo parser de ese JSON" del proyecto.
"""
import logging

logger = logging.getLogger(__name__)

# Puntos de la sparkline. No tiene sentido mandar 243.000 frames al navegador
# para un gráfico de unos cientos de píxeles.
MAX_POINTS = 240


def pq_code_to_nits(code_value: float) -> float:
    """Convierte valor PQ (0-4095) a nits via SMPTE ST 2084 EOTF inverse."""
    # PQ inverse EOTF: L = 10000 * ((max(0, V^(1/m2) - c1)) / (c2 - c3 * V^(1/m2)))^(1/m1)
    # V = code_value / 4095
    v = max(0.0, min(1.0, code_value / 4095.0))
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 4096.0 * 128.0
    c1 = 3424.0 / 4096.0
    c2 = 2413.0 / 4096.0 * 32.0
    c3 = 2392.0 / 4096.0 * 32.0
    vm2 = v ** (1.0 / m2)
    num = max(0.0, vm2 - c1)
    den = c2 - c3 * vm2
    if den <= 0:
        return 0.0
    return 10000.0 * (num / den) ** (1.0 / m1)


def niveles_desde_volcado(rpus: list) -> dict[str, list]:
    """Adapta el volcado anidado de `dovi_tool export` al formato PLANO de
    `rpu_analyze.export_levels`, que es el único que se consume.

    Antes era al revés: se RECONSTRUÍA la forma anidada desde el export por
    niveles para alimentar un segundo parser del mismo JSON que vivía aquí
    dentro. Medido con 243.552 frames, esa reconstrucción costaba 4,8 s y un
    pico de 475 MB de RAM — y era la duplicación que la regla del proyecto
    prohíbe: dos parsers del mismo volcado permitieron que uno divergiera del
    formato real, y el que funcionaba resultó ser el que tenía tests.

    Este camino solo se recorre si `export --levels` no está disponible
    (dovi_tool anterior a 2.3.3), así que el coste da igual; lo que importa es
    que desemboque en el MISMO consumidor.
    """
    niveles: dict[str, list] = {}

    def _anotar(nivel: str, frame: int, blk: dict) -> None:
        fila = dict(blk)
        fila["frame"] = frame
        niveles.setdefault(nivel, []).append(fila)

    for i, rpu in enumerate(rpus):
        vdr = (rpu or {}).get("vdr_dm_data") if isinstance(rpu, dict) else None
        if not isinstance(vdr, dict):
            continue
        for contenedor in (vdr, vdr.get("cmv29_metadata"), vdr.get("cmv40_metadata")):
            if not isinstance(contenedor, dict):
                continue
            # Forma (b): {"level1": {...}} colgando del contenedor.
            for clave, valor in contenedor.items():
                if isinstance(valor, dict) and clave.lower().startswith("level"):
                    _anotar(clave.lower(), i, valor)
            # Forma (a)/(c): lista de bloques, cada uno {"Level1": {...}} o
            # con un campo `level` numérico.
            bloques = contenedor.get("ext_metadata_blocks") or contenedor.get("ext_blocks")
            if not isinstance(bloques, list):
                continue
            for item in bloques:
                if not isinstance(item, dict):
                    continue
                etiquetado = False
                for clave, valor in item.items():
                    if isinstance(valor, dict) and clave.lower().startswith("level"):
                        _anotar(clave.lower(), i, valor)
                        etiquetado = True
                if not etiquetado and isinstance(item.get("level"), int):
                    _anotar(f"level{item['level']}", i, item)
    return niveles


def perfil_desde_niveles(niveles: dict[str, list]) -> dict:
    """Extrae del export PLANO todo lo que alimenta el sparkline y sus refs.

    Recorre cada nivel una vez y en su propia forma, sin árboles que navegar:
      · L1 → las tres series por frame (peak / avg / min) en nits
      · L5 → la zona de active area de cada frame (barras dinámicas)
      · L8 → el max_pq más alto visto por target display
      · L2 → el conjunto de trim targets
      · L6 → el mastering display (nits directos, no PQ)

    `frames` es el número de frames con L1, que es la longitud de las series.
    """
    def _a_nits(v) -> int:
        v = float(v)
        if v > 4096:
            n = v
        elif v < 1:
            n = pq_code_to_nits(v * 4095)
        else:
            n = pq_code_to_nits(v)
        return int(round(n))

    cll: list[int] = []
    fall: list[int] = []
    minimos: list[int] = []
    raw_max = 0
    raw_suma = 0
    for fila in niveles.get("level1") or []:
        try:
            mx, av, mn = int(fila["max_pq"]), int(fila["avg_pq"]), int(fila["min_pq"])
        except (KeyError, TypeError, ValueError):
            continue
        # Sanity: min<=avg<=max y en rango 12-bit. El parser anterior lo
        # comprobaba porque la búsqueda a ciegas encontraba bloques hermanos
        # con campos homónimos (en BR2049 daba un peak de ~176 nits en vez de
        # ~1000). Aquí el nivel viene etiquetado, pero un registro incoherente
        # sigue sin servir.
        if not (0 <= mn <= av <= mx <= 8191):
            continue
        raw_max = max(raw_max, mx)
        raw_suma += mx
        cll.append(_a_nits(mx))
        fall.append(_a_nits(av))
        minimos.append(_a_nits(mn))

    zonas_l5: list[tuple] = []
    for fila in niveles.get("level5") or []:
        try:
            zonas_l5.append((
                int(fila.get("active_area_top_offset", 0)),
                int(fila.get("active_area_bottom_offset", 0)),
                int(fila.get("active_area_left_offset", 0)),
                int(fila.get("active_area_right_offset", 0)),
            ))
        except (TypeError, ValueError):
            continue

    l8_por_target: dict[int, int] = {}
    for fila in niveles.get("level8") or []:
        try:
            tdi = int(fila.get("target_display_index", 0))
            if not tdi:
                continue
            # `target_max_pq` es el campo bueno; los otros dos son el fallback
            # histórico para exports que no lo traen.
            pq = (int(fila.get("target_max_pq", 0))
                  or int(fila.get("target_mid_pq", 0))
                  or int(fila.get("trim_slope", 0)))
        except (TypeError, ValueError):
            continue
        if pq > l8_por_target.get(tdi, 0):
            l8_por_target[tdi] = pq

    l2_targets: set[int] = set()
    for fila in niveles.get("level2") or []:
        try:
            l2_targets.add(int(fila["target_max_pq"]))
        except (KeyError, TypeError, ValueError):
            continue

    l6 = {"max_nits": 0, "min_nits": 0.0, "max_cll": 0, "max_fall": 0}
    for fila in niveles.get("level6") or []:
        try:
            l6 = {
                "max_nits": int(fila.get("max_display_mastering_luminance", 0)),
                "min_nits": float(fila.get("min_display_mastering_luminance", 0)) / 10000.0,
                "max_cll": int(fila.get("max_content_light_level", 0)),
                "max_fall": int(fila.get("max_frame_average_light_level", 0)),
            }
        except (TypeError, ValueError):
            pass
        break   # es constante en todo el RPU: el primer registro basta

    return {
        "cll": cll, "fall": fall, "min": minimos,
        "l5_zonas": zonas_l5,
        "l8_por_target": l8_por_target,
        "l2_targets_pq": l2_targets,
        "l6": l6,
        "raw_max_pq": raw_max,
        "raw_avg_pq": (raw_suma / len(cll)) if cll else 0,
    }


def payload_de_luminancia(niveles: dict[str, list]) -> dict:
    """El resultado completo que consume la sparkline de Tab 2.

    Tres series reducidas a `MAX_POINTS` cubos, los percentiles y buckets de
    brillo, y las referencias del RPU para el overlay (trims L2, master display
    L6, zonas L5, targets L8 del film entero).

    Cada serie se reduce con el agregador que le corresponde: **máximo** para
    los picos, **media** para el avg y **mínimo** para el suelo. Reducir las
    tres con el máximo aplanaría la banda y el gráfico mentiría.
    """
    perfil = perfil_desde_niveles(niveles)
    cll = perfil["cll"]
    fall = perfil["fall"]
    minimos = perfil["min"]

    def _a_nits(v) -> int:
        v = float(v)
        if v > 4096:
            n = v
        elif v < 1:
            n = pq_code_to_nits(v * 4095)
        else:
            n = pq_code_to_nits(v)
        return int(round(n))

    # ── Referencias del RPU para el overlay del chart ────────────────
    l2_targets_nits = sorted({_a_nits(pq) for pq in perfil["l2_targets_pq"]})
    l8_trim_nits_full = sorted({_a_nits(pq) for pq in perfil["l8_por_target"].values() if pq})

    # Zonas L5 a lo largo del film: si sale más de una, el film tiene active
    # area dinámica (letterbox cambiante tipo IMAX 1.43 ↔ 2.40). Ordenadas por
    # frecuencia, la más común primero.
    from collections import Counter
    zonas = Counter(perfil["l5_zonas"])
    l5_zones_list = [{
        "top": off[0], "bottom": off[1], "left": off[2], "right": off[3],
        "frames": n,
        "pct": round(n / max(1, len(perfil["l5_zonas"])) * 100, 1),
    } for off, n in zonas.most_common()]

    # ── Percentiles y buckets ────────────────────────────────────────
    ordenados = sorted(cll)

    def _percentil(xs, p):
        if not xs:
            return 0
        i = max(0, min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1)))))
        return xs[i]

    total = max(1, len(cll))

    def _reducir(xs, agregador):
        if len(xs) <= MAX_POINTS:
            return xs
        paso = len(xs) / MAX_POINTS
        return [agregador(xs[int(i * paso):int((i + 1) * paso)] or [0])
                for i in range(MAX_POINTS)]

    def _media(seg):
        return int(round(sum(seg) / len(seg))) if seg else 0

    return {
        "per_scene_max_cll": _reducir(cll, max),
        # avg_pq por escena. El nombre dice "fall" por compatibilidad con el
        # frontend, que lo lleva así desde la primera versión.
        "per_scene_max_fall": _reducir(fall, _media) if fall else [],
        "per_scene_min": _reducir(minimos, min) if minimos else [],
        "total_frames": len(cll),
        "stats": {
            "peak": max(cll) if cll else 0,
            "p99": _percentil(ordenados, 99),
            "p95": _percentil(ordenados, 95),
            "p50": _percentil(ordenados, 50),
            "avg_of_max": int(round(sum(cll) / len(cll))) if cll else 0,
            "bucket_dim": sum(1 for v in cll if v < 100),
            "bucket_mid": sum(1 for v in cll if 100 <= v < 300),
            "bucket_high": sum(1 for v in cll if v >= 300),
            "total": total,
        },
        "references": {
            "l2_trim_targets_nits": l2_targets_nits,     # ej. [100, 600, 1000]
            "l6_master_max_nits": perfil["l6"]["max_nits"],
            "l6_master_min_nits": perfil["l6"]["min_nits"],
            "l6_max_cll": perfil["l6"]["max_cll"],
            "l6_max_fall": perfil["l6"]["max_fall"],
            "l5_zones": l5_zones_list,
            # L8 del film COMPLETO: sustituye al sample de `DoviInfo.l8_trim_nits`,
            # que solo mira los primeros frames y se pierde los targets que
            # aparecen a mitad de película.
            "l8_trim_nits_full": l8_trim_nits_full,
        },
        # Para el log del job, no para la UI.
        "_raw": {"max_pq": perfil["raw_max_pq"], "avg_pq": perfil["raw_avg_pq"]},
    }
