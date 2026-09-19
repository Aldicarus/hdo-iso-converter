"""El relato de un proyecto CMv4.0: puro, sin IO, sobre la sesión.

Contesta las cuatro preguntas que tiene el usuario en cualquier momento —
**¿qué pasa ahora?**, **¿dónde estoy?**, **¿por qué estoy aquí?** y **¿qué se
decidió?**— en un solo objeto, para que las cinco superficies que le hablan
(modal del pre-flight, log, card de análisis, cards de fase y columna de
trabajo) cuenten lo mismo por construcción y no por disciplina.

Lo que arregla, con la evidencia del 2026-09-19 delante:

- **Dos proyectos en estados opuestos enseñaban el mismo rótulo.** Uno que el
  usuario decidió inyectar y otro que pasó de largo por tener L8 real: los dos
  decían «Análisis pendiente», porque la card leía `recommended_action`, que
  vale `unknown` hasta que la Fase A puebla el L2 del origen. `situacion` y
  `decision.estado` los distinguen.
- **La ficha no decía que lo habías cancelado.** Al cancelar, `phase` vuelve a
  `created` y `recommended_action` queda vacío: el hecho más importante que le
  había pasado al proyecto vivía solo en el log.
- **La misma fase tenía cuatro nombres.** `[Fase A]` en el log, «Fase A —
  Analizar MKV origen» en la ficha, «Fase A — Analizando el MKV origen» en la
  columna y `analyze_source` por dentro. Aquí hay UNO.

Es puro a propósito, como `cmv40_strategy`: sin disco y sin red, así que sus
combinaciones se recorren en un test en milisegundos.
"""
from __future__ import annotations

import relato
from i18n import t as tr

# ── Las ocho etapas, en orden, con su rótulo ────────────────────────────────
#
# La letra se conserva —es como el usuario y el log las nombran— y se le pega
# el nombre de lo que hace. Decisión del usuario el 2026-09-19 entre las tres
# formas posibles. **No hay Fase E**: aplicar la corrección de sync es parte
# de la D y se repite dentro de ella, así que como etapa contaría dos veces.
ETAPAS = (
    "preflight", "analyze_source", "target_rpu", "extract",
    "sync", "inject", "remux", "validate",
)

#: etapa → la letra con la que el usuario la conoce. El pre-flight no lleva:
#: no es una de las fases con letra del pipeline. Es la MISMA tabla que las
#: cards del panel usaban para inventarse sus títulos —había cuatro variantes
#: del rótulo de la Fase A— y ahora sale de aquí.
LETRAS = {
    "preflight": "", "analyze_source": "A", "target_rpu": "B",
    "extract": "C", "sync": "D", "inject": "F", "remux": "G",
    "validate": "H",
}

# `running_phase` → etapa. Fase B tiene tres variantes según de dónde venga el
# bin y Fase E cae dentro de la D, que es donde el usuario está mirando.
_RUNNING_A_ETAPA = {
    "preflight": "preflight",
    "analyze_source": "analyze_source",
    "target_rpu_path": "target_rpu",
    "target_rpu_drive": "target_rpu",
    "target_rpu_mkv": "target_rpu",
    "extract": "extract",
    "sync_correct": "sync",
    "correct_sync": "sync",
    "inject": "inject",
    "remux": "remux",
    "validate": "validate",
}

# `phase` (la última COMPLETADA) → cuántas etapas quedan atrás.
_PHASE_A_INDICE = {
    "created": 0, "source_analyzed": 2, "target_provided": 3,
    "extracted": 4, "sync_verified": 5, "injected": 6,
    "remuxed": 7, "validated": 8, "done": 8,
}


def rotulo_de_etapa(etapa: str) -> str:
    """«Fase A · Analizar el MKV origen». La clave se compone del id.

    Una tabla de rótulos en el ámbito del módulo se evaluaría UNA vez, al
    importar, y congelaría el idioma del arranque del contenedor — el fallo
    que `TestNadieTraduceAlImportar` vigila y que ya apareció nueve veces.
    """
    return tr(f"relato.etapa_{etapa}")


def _situacion(session, en_cola) -> str:
    """Excluyentes y en orden fijo. El orden ES la decisión.

    Un proyecto archivado que arrastra un `error_message` es «archivado»: ya
    no hay nada que resolver. Y un cancelado con una fase corriendo es «en
    marcha», porque lo que el usuario necesita saber es que algo se mueve.
    """
    if getattr(session, "archived", False):
        return relato.ARCHIVADO
    if session.phase == "done":
        return relato.TERMINADO
    if session.running_phase:
        return relato.EN_MARCHA
    if session.error_message:
        return relato.DETENIDO_POR_ERROR
    if en_cola:
        return relato.ESPERANDO_TURNO
    decision = session.preflight_decision or ""
    if decision and decision != "ok":
        return relato.ESPERANDO_DECISION
    if _ultima_cancelada(session):
        return relato.CANCELADO
    return relato.PREPARANDO


def _ultima_cancelada(session):
    """La última entrada del historial, si acabó cancelada."""
    hist = list(getattr(session, "phase_history", None) or [])
    if not hist:
        return None
    ultima = hist[-1]
    estado = getattr(ultima, "status", None) or (
        ultima.get("status") if isinstance(ultima, dict) else None)
    return ultima if estado == "cancelled" else None


def _etapa(session, situacion: str) -> str:
    """En qué etapa está — o en cuál se quedó.

    Con `cancelado` hay que mirar el historial: el cancel devuelve `phase` a
    su valor anterior, así que preguntarle a la sesión dice «Validación
    previa» de un trabajo que murió extrayendo el HEVC. Lo destapó el
    prototipo sobre el job real, no la lectura del código.
    """
    if situacion == relato.CANCELADO:
        ultima = _ultima_cancelada(session)
        nombre = getattr(ultima, "phase", None) or (
            ultima.get("phase") if isinstance(ultima, dict) else None)
        if nombre in _RUNNING_A_ETAPA:
            return _RUNNING_A_ETAPA[nombre]
    if session.running_phase in _RUNNING_A_ETAPA:
        return _RUNNING_A_ETAPA[session.running_phase]
    idx = _PHASE_A_INDICE.get(session.phase or "created", 0)
    return ETAPAS[min(idx, len(ETAPAS) - 1)]


def _decision(session) -> dict:
    """Qué se te preguntó, qué contestaste y cuándo — o que no hay nada.

    Los tres campos que lo deciden —`preflight_decision`, `recommended_action`
    y `preflight_user_choice`— siguen persistidos y donde estaban. Lo que
    cambia es que dejan de leerse sueltos: aquí se resuelven una vez.
    """
    elegida = session.preflight_user_choice or ""
    if not elegida and session.output_workflow == "keep_cmv29":
        # Proyectos cerrados ANTES de que existiera `preflight_user_choice`:
        # ese workflow solo lo escribe `accept-keep`, así que identifica la
        # decisión igual de bien. Sin esto el modal vuelve a ofrecer los dos
        # botones de algo que el usuario ya cerró hace meses.
        elegida = "keep"
    pendiente = (session.preflight_decision or "") not in ("", "ok")
    if not pendiente and not elegida:
        return {"estado": relato.DECISION_NO_PROCEDE}
    comun = {
        "pregunta": tr('relato.pregunta_inyectar_o_mantener'),
        "opciones": [
            {"id": "keep",   "rotulo": tr('relato.opcion_mantener')},
            {"id": "inject", "rotulo": tr('relato.opcion_inyectar')},
        ],
    }
    if elegida:
        return {**comun, "estado": relato.DECISION_TOMADA, "elegida": elegida,
                "cuando": session.preflight_user_choice_at or "",
                # El titular de lo que pasó, para que reabrir el modal diga
                # qué se contestó en vez de volver a preguntarlo.
                "titulo": tr('relato.titulo_se_mantiene' if elegida == "keep"
                             else 'relato.titulo_se_inyecta')}
    # Los dos motivos de parada NO son el mismo: con el bin sintético la app
    # recomienda mantener; con el tercer veredicto dice que depende de tu
    # reproductor y no se moja. Compartir titular era la mitad del mensaje
    # que el usuario no podía entender.
    return {**comun, "estado": relato.DECISION_PENDIENTE,
            "porque": session.preflight_message or "",
            "titulo": tr('relato.titulo_lo_decides_tu'
                         if session.preflight_decision == "ask_tone_mapping"
                         else 'relato.titulo_bin_sin_ajustes')}


def _dv(info) -> str:
    """«Perfil 7 FEL · CM v4.0 · 222.274 frames», o '' si no hay nada.

    El guard del principio no es cosmético: sin él, un bin todavía sin
    analizar producía «Perfil None» como evidencia de un hecho pendiente.
    """
    if not info or not getattr(info, "profile", None):
        return ""
    el = getattr(info, "el_type", "") or ""
    cm = getattr(info, "cm_version", "") or ""
    frames = getattr(info, "frame_count", 0) or 0
    trozos = [tr('relato.perfil_p', p=f"{info.profile}{(' ' + el) if el else ''}")]
    if cm:
        trozos.append(f"CM {cm}")
    if frames:
        trozos.append(tr('relato.n_frames', n=f"{frames:,}".replace(",", ".")))
    return " · ".join(trozos)


def _hechos(session) -> list[dict]:
    """Lo comprobado hasta ahora, con el dato que lo sostiene.

    Es la MISMA lista que pinta el checklist del modal y el resumen de la
    ficha. Antes eran dos derivaciones de los mismos campos, y por eso podían
    —y solían— discrepar.
    """
    src = session.source_dv_info
    tgt = session.target_dv_info
    hechos = [
        relato.hecho(
            "origen_dv", tr('relato.hecho_origen_dv'),
            relato.HECHO_OK if (src or session.source_preflight_ok)
            else relato.HECHO_PENDIENTE,
            _dv(src) or (tr('relato.rpu_en_los_primeros_30_s')
                         if session.source_preflight_ok else "")),
        relato.hecho(
            "bin_obtenido", tr('relato.hecho_bin_obtenido'),
            relato.HECHO_OK if tgt else relato.HECHO_PENDIENTE,
            session.pending_target_file_name
            or (session.target_rpu_path or "").split("/")[-1]),
    ]
    # El fallo del pre-flight por CM version es lo que tiene que leerse EN la
    # fila que falla: mandar al usuario al banner de error para enterarse de
    # cuál de las cuatro comprobaciones no pasó es lo que hacía el modal
    # antes de tener checklist.
    fallo_cm = bool(session.error_message) and bool(
        _RE_CM.search(session.error_message or ""))
    es_v40 = bool(tgt) and (tgt.cm_version or "") == "v4.0"
    dv_tgt = _dv(tgt)
    if dv_tgt and tgt is not None:
        dv_tgt += " · " + tr('relato.l8_presente' if getattr(tgt, "has_l8", False)
                             else 'relato.sin_l8')
    hechos.append(relato.hecho(
        "bin_cmv40", tr('relato.hecho_bin_cmv40'),
        relato.HECHO_FALLO if fallo_cm
        else relato.HECHO_OK if es_v40
        else relato.HECHO_AVISO if tgt else relato.HECHO_PENDIENTE,
        session.error_message if fallo_cm else dv_tgt))

    clase = session.target_l8_classification or ""
    estado_l8 = {"real": relato.HECHO_OK,
                 "tone_mapping": relato.HECHO_AVISO,
                 "default": relato.HECHO_AVISO,
                 "indeterminate": relato.HECHO_DUDA}.get(
                     clase, relato.HECHO_PENDIENTE)
    hechos.append(relato.hecho("bin_colorista", tr('relato.hecho_bin_colorista'),
                               estado_l8, _evidencia_l8(session, clase)))
    return hechos


# «no aporta CMv4.0», «CM v2.9»… — lo que distingue un fallo de esa
# comprobación de cualquier otro error del pre-flight.
_RE_CM = __import__("re").compile(r"CMv4\.0|CM v", __import__("re").I)


def _evidencia_l8(session, clase: str) -> str:
    """El veredicto del L8 y los tres números que lo sostienen.

    El veredicto va DELANTE porque es la respuesta; los números detrás,
    porque son la prueba. Es la misma regla con la que se reescribieron los
    textos el 2026-09-19.
    """
    if not clase:
        # Cuando el pre-flight aborta, la fila que se quedó sin respuesta
        # tiene que decirlo: si no, se lee como «pendiente» de algo que ya no
        # va a pasar.
        return tr('relato.no_se_llego_a_comprobar') if session.error_message else ""
    tier = {"full": "FULL", "core_rich": "CORE+", "core": "CORE"}.get(
        session.target_l8_quality_tier or "", "")
    veredicto = {
        "real": (tr('relato.l8_si_calidad', tier=tier) if tier
                 else tr('relato.l8_si')),
        "tone_mapping": tr('relato.l8_solo_automatico'),
        "default": tr('relato.l8_sin_ajustes'),
        "indeterminate": tr('relato.l8_no_concluyente'),
    }.get(clase, clase)
    trozos = [veredicto]
    if session.target_l8_max_delta:
        trozos.append(tr('relato.intensidad_max',
                         delta=session.target_l8_max_delta))
    if session.target_l8_unique_count:
        trozos.append(tr('relato.n_ajustes', n=session.target_l8_unique_count))
    neutros = session.target_l8_neutral_frames_pct
    if neutros is not None:
        trozos.append(tr('relato.con_ajuste_en_el_pct',
                         pct=round((1.0 - neutros) * 100)))
    return " · ".join(trozos)


def _marcar_el_que_se_esta_haciendo(hechos: list[dict], situacion: str) -> None:
    """Con el trabajo en marcha, el primer hecho sin resolver es el de ahora.

    Lo calculaba el JS del modal. Aquí lo ve también la ficha, que es la mitad
    del encargo: sin esto, una superficie sabe en qué comprobación va y la
    otra no. Y una lista entera en gris es lo que hace que un checklist no
    parezca vivo.
    """
    if situacion != relato.EN_MARCHA:
        return
    for h in hechos:
        if h["estado"] == relato.HECHO_PENDIENTE:
            h["estado"] = relato.HECHO_EN_CURSO
            return


def _porque(session, situacion: str, plan) -> str:
    """Por qué el trabajo está donde está — mirando ATRÁS, nunca adelante.

    La regla del proyecto prohíbe prometer lo que hará la fase siguiente
    (nació de promesas que quedaban colgando al cancelar). Justificar lo ya
    decidido con lo ya medido no es predecir: es lo que faltaba.
    """
    clase = session.target_l8_classification or ""
    if situacion == relato.ESPERANDO_DECISION:
        return tr('relato.porque_esperando_decision')
    if situacion == relato.CANCELADO:
        return tr('relato.porque_cancelado')
    if situacion == relato.DETENIDO_POR_ERROR:
        return tr('relato.porque_error')
    if session.preflight_user_choice == "inject" and clase != "real":
        return tr('relato.porque_forzaste')
    if clase == "real" and plan is not None and plan.drop_in:
        return tr('relato.porque_drop_in')
    if clase == "real":
        return tr('relato.porque_merge')
    return ""


def resolver(session, *, en_cola=None, plan=None) -> dict:
    """El relato completo. `plan` se pasa ya resuelto para no calcularlo dos
    veces en la misma petición — el endpoint ya lo tiene en la mano."""
    situacion = _situacion(session, en_cola)
    etapa = _etapa(session, situacion)
    hechos = _hechos(session)
    _marcar_el_que_se_esta_haciendo(hechos, situacion)
    idx = ETAPAS.index(etapa)
    # `siguiente` es SOLO para la interfaz, que se repinta y se corrige sola.
    # En el log sería la promesa colgando que la regla del proyecto prohíbe.
    siguiente = ""
    if situacion in (relato.EN_MARCHA, relato.PREPARANDO) and idx + 1 < len(ETAPAS):
        siguiente = rotulo_de_etapa(ETAPAS[idx + 1])
    return {
        "situacion": situacion,
        # Las ocho, para que las cards del panel y la tira de fases pinten
        # los MISMOS rótulos que el log y la columna de trabajo.
        "etapas": [{"id": e, "letra": LETRAS.get(e, ""),
                    "rotulo": rotulo_de_etapa(e)} for e in ETAPAS],
        # El rótulo lo pone el servidor para que la ficha, la columna de
        # trabajo y el modal digan lo mismo — que es de lo que iba todo esto.
        "situacion_rotulo": tr(f"relato.situacion_{situacion}"),
        "etapa": {"id": etapa, "rotulo": rotulo_de_etapa(etapa),
                  "indice": idx + 1, "total": len(ETAPAS),
                  # El MISMO texto que el log escribe al arrancar la fase, no
                  # una segunda redacción: que las dos superficies cuenten lo
                  # mismo era el encargo.
                  "porque": porque_de_fase(session, etapa, plan=plan)},
        "porque": _porque(session, situacion, plan),
        "decision": _decision(session),
        "hechos": hechos,
        "siguiente": siguiente,
    }


# ── La justificación: de dónde viene la fase que va a empezar ───────────────
#
# La regla del proyecto prohíbe PROMETER la fase siguiente —nació de promesas
# que quedaban colgando al cancelar— pero no dice nada de mirar atrás, y nadie
# lo hacía: cada fase abría con su `📋 Plan` sin referirse a lo que se había
# medido antes. Eso es lo que el usuario describió como «cada fase corre de
# manera independiente sin justificar lo que hace».
#
# El texto sale de AQUÍ y no de cada fase, por lo mismo que el `📋 Plan` de
# las que ramifican sale de `cmv40_strategy`: si la explicación y el dato no
# son el mismo objeto, se separan. Y lo emite el orquestador en un solo sitio,
# así que ninguna fase puede quedarse sin ella.

def porque_de_fase(session, phase_name: str, plan=None) -> str:
    """El hecho que la fase anterior dejó establecido y que ésta usa.

    Devuelve "" cuando no hay nada que contar: una frase vacía de contenido
    cada vez que arranca una fase es ruido, y el log de un job largo ya tiene
    bastante.
    """
    etapa = _RUNNING_A_ETAPA.get(phase_name, "")
    if not etapa and phase_name in ETAPAS:
        # El relato pregunta por el id de etapa; el orquestador, por el
        # `running_phase`. Aceptar los dos evita una segunda tabla.
        etapa = phase_name

    if etapa == "analyze_source":
        tgt = session.target_dv_info
        if not tgt or not tgt.profile:
            return ""
        clase = session.target_l8_classification or ""
        veredicto = {
            "real": tr('relato.porque_fase_a_real'),
            "tone_mapping": tr('relato.porque_fase_a_tone_mapping'),
            "default": tr('relato.porque_fase_a_default'),
        }.get(clase, "")
        return tr('relato.porque_fase_a', dv=_dv(tgt), veredicto=veredicto)

    if etapa == "target_rpu":
        src = session.source_dv_info
        return tr('relato.porque_fase_b', dv=_dv(src)) if src and src.profile else ""

    if etapa == "extract":
        if plan is None:
            return ""
        # Se ancla en lo que la fase RAMIFICA (`needs_demux`), no en
        # `drop_in`: si la explicación y la decisión no salen del mismo campo
        # pueden contar cosas distintas, que es el fallo que `cmv40_strategy`
        # vino a cerrar («Te van a matar», 2026-08-15).
        return tr('relato.porque_fase_c_merge' if plan.extract.needs_demux
                  else 'relato.porque_fase_c_dropin')

    if etapa == "inject":
        if "sync_verification_pause" in (session.phases_skipped or []) or (
                plan is not None and plan.inputs.skip_sync_review):
            return tr('relato.porque_fase_f_sin_revisar')
        if session.sync_delta == 0:
            return tr('relato.porque_fase_f_sync_ok')
        return tr('relato.porque_fase_f_sync_corregido', delta=session.sync_delta)

    if etapa == "remux":
        return tr('relato.porque_fase_g')

    if etapa == "validate":
        return tr('relato.porque_fase_h', nombre=session.output_mkv_name or "")

    return ""
