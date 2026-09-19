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
                "cuando": session.preflight_user_choice_at or ""}
    return {**comun, "estado": relato.DECISION_PENDIENTE,
            "porque": session.preflight_message or ""}


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
    es_v40 = bool(tgt) and (tgt.cm_version or "") == "v4.0"
    hechos.append(relato.hecho(
        "bin_cmv40", tr('relato.hecho_bin_cmv40'),
        relato.HECHO_OK if es_v40
        else relato.HECHO_AVISO if tgt else relato.HECHO_PENDIENTE,
        _dv(tgt)))

    clase = session.target_l8_classification or ""
    estado_l8 = {"real": relato.HECHO_OK,
                 "tone_mapping": relato.HECHO_AVISO,
                 "default": relato.HECHO_AVISO,
                 "indeterminate": relato.HECHO_DUDA}.get(
                     clase, relato.HECHO_PENDIENTE)
    evidencia = ""
    if clase:
        evidencia = " · ".join(x for x in (
            tr('relato.intensidad_max', delta=session.target_l8_max_delta),
            tr('relato.n_ajustes', n=session.target_l8_unique_count),
        ) if x)
    hechos.append(relato.hecho("bin_colorista", tr('relato.hecho_bin_colorista'),
                               estado_l8, evidencia))
    return hechos


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
    idx = ETAPAS.index(etapa)
    # `siguiente` es SOLO para la interfaz, que se repinta y se corrige sola.
    # En el log sería la promesa colgando que la regla del proyecto prohíbe.
    siguiente = ""
    if situacion in (relato.EN_MARCHA, relato.PREPARANDO) and idx + 1 < len(ETAPAS):
        siguiente = rotulo_de_etapa(ETAPAS[idx + 1])
    return {
        "situacion": situacion,
        "etapa": {"id": etapa, "rotulo": rotulo_de_etapa(etapa),
                  "indice": idx + 1, "total": len(ETAPAS)},
        "porque": _porque(session, situacion, plan),
        "decision": _decision(session),
        "hechos": _hechos(session),
        "siguiente": siguiente,
    }
