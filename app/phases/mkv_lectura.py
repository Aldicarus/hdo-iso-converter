# -*- coding: utf-8 -*-
"""La lectura del MKV: tres frases que interpretan lo que el análisis midió.

La radiografía enseñaba ~60 números repartidos en seis bloques y ni una
frase que los juntara. El encargo del usuario (2026-09-24) fue «como leer
un análisis de un UHD, pero automático»: qué es este fichero, cómo está
masterizado y cómo se ve.

**Es puro y NO se persiste.** Se compone al servir, como `session.plan` y
como el relato, y por el mismo motivo: el veredicto ya se guardó una vez
como texto y un análisis hecho en castellano se servía en castellano para
siempre. Todo lo de aquí sale de números que la caché sí tiene.

Tres decisiones que salieron de mirar los MKV del NAS, no de suponer:

· **La conclusión de la luz exige que haya luz que describir.** Tres de
  los siete MKV con perfil —Avatar, Backrooms, Drive— tienen la serie L1
  con UN SOLO valor en sus 240 puntos. No es un fallo del análisis:
  comprobado sobre el disco, Drive alterna entre dos códigos PQ (194 y
  236 nits) y la reducción por máximo la deja plana. En esos discos una
  frase sobre la distribución sería inventada, así que se dice lo que
  pasa: el máster etiqueta el L1 con un valor único.
· **La coherencia pico/declarado sólo se opina si hay con qué.** Cuatro
  de los siete no traen MaxCLL en el SEI.
· **Sin análisis extendido no hay frase de luz**, porque el pico del
  sniff de 30 s no describe la película — es el defecto que ya costó un
  banner al revés.
"""
from __future__ import annotations

from i18n import t

#: Un pico que se aparta del máster declarado por debajo de este factor
#: es un máster etiquetado conservador; por encima del otro, un RPU que
#: promete más de lo que el máster da. Son los umbrales que la radiografía
#: ya usaba para su aviso de divergencia, y se reutilizan a propósito:
#: dos criterios para la misma pregunta acaban diciendo cosas distintas.
RATIO_CONSERVADOR = 0.5
RATIO_GENEROSO = 2.0

#: Por encima de esto, el pico y el máster se consideran coincidentes.
#: 1000 contra 1001 —el caso de Pulp Fiction— no es una divergencia.
RATIO_COINCIDE = 0.9

#: Frames cuyo pico supera este brillo. Es el corte que la mini-card de
#: la radiografía ya usa para su tercer cubo.
NITS_ALTO = 300


def _frase(rotulo: str, texto: str, conclusion: str = "") -> dict:
    return {"rotulo": rotulo, "texto": texto, "conclusion": conclusion}


def _que_es(dv: dict, hdr: dict) -> dict:
    """Qué formato es y con qué se reproduce."""
    perfil = dv.get("profile")
    el = (dv.get("el_type") or "").strip()
    compat = (hdr.get("hdr_format_compatibility") or "").strip()
    # La BASE sobre la que viaja el Dolby Vision, que es `hdr_format`
    # («HDR10», «HLG»). `hdr_format_raw` no sirve aquí: es el literal de
    # MediaInfo y ya empieza por «Dolby Vision», así que la frase salía
    # diciendo «Dolby Vision Profile 7 FEL sobre Dolby Vision / SMPTE ST
    # 2086». El literal completo tiene su sitio en la ficha.
    base = (hdr.get("hdr_format") or "").strip()

    if perfil:
        texto = t("lectura.que_es_dv", perfil=perfil,
                  capa=f" {el}" if el else "",
                  base=base or "HDR10")
    else:
        texto = t("lectura.que_es_sin_dv", base=base or "—")

    partes = [texto]
    if compat:
        partes.append(t("lectura.que_es_compat", compat=compat))

    # El perfil que el contenedor DECLARA contra el que dovi_tool lee del
    # RPU. Cuando no coinciden manda el declarado, que es lo que mira un
    # reproductor — es la firma del MKV anunciado dual-layer sin capa de
    # mejora.
    declarado = (hdr.get("dv_profile_string") or "").strip().lower()
    medido = f"dvhe.0{perfil}" if perfil else ""
    conclusion = ""
    if declarado and medido and declarado != medido:
        conclusion = t("lectura.que_es_discrepa",
                       declarado=declarado, medido=medido)
    elif el.upper() == "FEL":
        conclusion = t("lectura.que_es_fel")
    return _frase(t("lectura.rotulo_que_es"), " ".join(partes), conclusion)


def _el_master(dv: dict, hdr: dict, q: dict) -> dict:
    """Dónde se hizo el grade y cuánto trabajo lleva encima."""
    primarios = (dv.get("l9_primaries")
                 or hdr.get("mastering_display_primaries") or "").strip()
    pico_master = _pico_declarado(dv, hdr)
    partes = []
    if primarios and pico_master:
        partes.append(t("lectura.master_prim_nits",
                        primarios=primarios, nits=pico_master))
    elif primarios:
        partes.append(t("lectura.master_prim", primarios=primarios))
    elif pico_master:
        partes.append(t("lectura.master_nits", nits=pico_master))

    # El grading: el veredicto que el análisis extendido ya decidió, más
    # el número que lo sostiene. Cuál es depende de si hay L8.
    l8 = q.get("quality_l8_unique_count") or 0
    l2 = q.get("quality_l2_unique_count") or 0
    objetivos = len(q.get("quality_l2_target_pqs") or [])
    if l8:
        partes.append(t("lectura.master_grading_l8",
                        combos=f"{l8:,}".replace(",", "."),
                        delta=q.get("quality_l8_max_delta") or 0))
    elif l2:
        partes.append(t("lectura.master_grading_l2",
                        combos=f"{l2:,}".replace(",", "."),
                        objetivos=objetivos))
    elif not q:
        partes.append(t("lectura.master_sin_analisis"))

    return _frase(t("lectura.rotulo_master"), " ".join(partes),
                  (q.get("quality_verdict_text") or "").strip())


def _pico_declarado(dv: dict, hdr: dict) -> int:
    """El pico del máster: L6 del RPU, y si no, el del SEI."""
    refs = dv.get("l1_references") or {}
    if refs.get("l6_master_max_nits"):
        return int(refs["l6_master_max_nits"])
    lum = hdr.get("mastering_display_luminance") or ""
    import re
    m = re.search(r"max:\s*([\d.]+)", lum)
    return int(round(float(m.group(1)))) if m else 0


def _la_luz(dv: dict, hdr: dict) -> dict | None:
    """Lo que el RPU dice frame a frame. None si no se ha medido."""
    stats = dv.get("l1_stats") or {}
    serie = dv.get("per_scene_max_cll") or []
    if not stats.get("peak") or not serie:
        return None

    pico = int(stats["peak"])
    distintos = len(set(serie))
    if distintos <= 1:
        # Tres de los siete MKV medidos. No es un fallo del análisis: el
        # máster alterna entre pocos códigos PQ y la reducción por máximo
        # deja la serie plana. Describir una distribución aquí sería
        # inventarla.
        return _frase(t("lectura.rotulo_luz"),
                      t("lectura.luz_constante", nits=pico),
                      t("lectura.luz_constante_conclusion"))

    total = stats.get("total") or 0
    altos = stats.get("bucket_high") or 0
    pct_alto = round(altos * 100 / total) if total else 0
    texto = t("lectura.luz_variable", pico=pico,
              mediana=int(stats.get("p50") or 0),
              pct=pct_alto, nits=NITS_ALTO)

    declarado = _pico_declarado(dv, hdr)
    conclusion = ""
    if declarado:
        ratio = pico / declarado
        if ratio < RATIO_CONSERVADOR:
            conclusion = t("lectura.luz_conservador",
                           pico=pico, declarado=declarado)
        elif ratio > RATIO_GENEROSO:
            conclusion = t("lectura.luz_generoso",
                           pico=pico, declarado=declarado)
        elif ratio >= RATIO_COINCIDE:
            conclusion = t("lectura.luz_coincide", declarado=declarado)
    return _frase(t("lectura.rotulo_luz"), texto, conclusion)


def lectura_de(analisis: dict) -> list[dict] | None:
    """Las tres frases, o None si no hay ni Dolby Vision ni HDR que leer.

    Recibe el `MkvAnalysisResult` ya serializado —es lo que el endpoint
    tiene delante— y no el modelo, para que el módulo no dependa de
    Pydantic y se pueda ejercitar con un dict en un test.
    """
    if not isinstance(analisis, dict):
        return None
    dv = analisis.get("dovi") or {}
    hdr = analisis.get("hdr") or {}
    if not dv and not hdr:
        return None
    # La auditoría vive dentro de `dovi` (el análisis la re-inyecta ahí al
    # servir el cache), así que los dos leen del mismo sitio.
    q = {k: v for k, v in dv.items() if k.startswith("quality_")}
    frases = [_que_es(dv, hdr), _el_master(dv, hdr, q)]
    luz = _la_luz(dv, hdr)
    if luz:
        frases.append(luz)
    return frases
