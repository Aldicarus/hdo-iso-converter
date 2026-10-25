"""El relato de una combinación (Tab 4): qué pasa, dónde está y qué se midió.

Mismo objeto que `tab1_relato` y `cmv40_relato`, y por el mismo motivo: la
tarjeta de la columna izquierda, la cabecera del proyecto, la sub-pestaña y
la columna de trabajo cuentan lo mismo porque lo leen de aquí.

Lo propio de esta pestaña es que hay una decisión que puede tocarle al
usuario ANTES de combinar: la medida de sincronía puede decir que los dos MKV
no son el mismo montaje, que no concluye, o que el audio sale de dos ritmos
distintos. Eso es `esperando_decision`, que en un rip no existe.

Es puro: lo que no puede saber por sí mismo —si está en la cola, la fase que
corre ahora y el plan— se lo pasa el router.
"""
from __future__ import annotations

import relato
from i18n import t as tr
from phases import mux_sync

ETAPAS = ("leer", "sincronia", "comprobar_origenes", "combinar",
          "comprobar_y_guardar")

#: la fase que marca el pipeline → la etapa que el usuario conoce.
_FASE_A_ETAPA = {"comprobar_origenes": "comprobar_origenes",
                 "combinar": "combinar",
                 "comprobar_y_guardar": "comprobar_y_guardar"}

# Los motivos que NO son una decisión del usuario sino algo que pasa solo.
_SIN_DECISION = {"midiendo", "en_marcha"}


def rotulo_de_etapa(etapa: str) -> str:
    """La clave se compone del id: una tabla de rótulos en el ámbito del
    módulo se evaluaría al importar y congelaría el idioma."""
    return tr(f"relato.mux_etapa_{etapa}")


def _campo(p, nombre, defecto=None):
    if isinstance(p, dict):
        return p.get(nombre, defecto)
    return getattr(p, nombre, defecto)


def _sincronia(p) -> dict:
    s = _campo(p, "sincronia")
    if s is None:
        return {}
    return s if isinstance(s, dict) else s.model_dump()


def _situacion(p, en_cola, plan: dict | None) -> str:
    """Excluyentes y en orden fijo. El orden ES la decisión."""
    estado = _campo(p, "status") or "pending"
    if estado == "running":
        return relato.EN_MARCHA
    if estado == "queued" or en_cola:
        return relato.ESPERANDO_TURNO
    if estado == "error":
        return relato.DETENIDO_POR_ERROR
    if estado == "done":
        return relato.TERMINADO
    if _campo(p, "last_cancelled_at"):
        return relato.CANCELADO
    motivos = [m for m in (plan or {}).get("motivos") or []
               if m.get("id") not in _SIN_DECISION]
    if motivos and not (plan or {}).get("puede_combinar"):
        return relato.ESPERANDO_DECISION
    return relato.PREPARANDO


def _etapa(p, situacion: str, fase_en_curso: str) -> str:
    if situacion == relato.EN_MARCHA and fase_en_curso in _FASE_A_ETAPA:
        return _FASE_A_ETAPA[fase_en_curso]
    if situacion in (relato.TERMINADO, relato.DETENIDO_POR_ERROR):
        return "comprobar_y_guardar"
    if _sincronia(p).get("estado") == "medida":
        return "sincronia"
    return "leer"


def _hecho_de_sincronia(p) -> dict:
    s = _sincronia(p)
    estado = s.get("estado")
    if not s or estado in ("error", "cancelada"):
        return relato.hecho("sincronia", tr('relato.mux_hecho_sincronia'),
                            relato.HECHO_PENDIENTE, tr('relato.mux_sin_medida'))
    if estado == "midiendo":
        hechos, total = (s.get("progreso") or [0, 0])[:2] or [0, 0]
        return relato.hecho("sincronia", tr('relato.mux_hecho_sincronia'),
                            relato.HECHO_EN_CURSO,
                            tr('relato.mux_midiendo_n_de', n=hechos, total=total))
    v = s.get("veredicto") or ""
    estado_h = {mux_sync.EN_SINCRONIA: relato.HECHO_OK,
                mux_sync.RETRASO: relato.HECHO_OK,
                mux_sync.OTRO_RITMO: relato.HECHO_OK,
                mux_sync.DERIVA: relato.HECHO_DUDA,
                mux_sync.MONTAJE_DISTINTO: relato.HECHO_FALLO,
                mux_sync.NO_CONCLUYENTE: relato.HECHO_DUDA}.get(v, relato.HECHO_DUDA)
    return relato.hecho("sincronia", tr('relato.mux_hecho_sincronia'), estado_h,
                        tr(f'relato.mux_veredicto_{v}') if v else "")


def _hechos(p, situacion: str) -> list[dict]:
    fuentes = _campo(p, "fuentes") or []
    video = (_campo(fuentes[0], "video") if fuentes else None) or {}
    perfil = _campo(video, "dv_profile") or 0
    el = _campo(video, "dv_el") or ""
    cm = _campo(video, "dv_cm") or ""
    pistas = _campo(p, "pistas") or []
    hechos = [
        relato.hecho(
            "dolby_vision", tr('relato.mux_hecho_dv'),
            relato.HECHO_OK if perfil else relato.HECHO_AVISO,
            (" · ".join(x for x in (tr('relato.perfil_p', p=f"{perfil}{(' ' + el) if el else ''}"),
                                    f"CM {cm}" if cm else "") if x))
            if perfil else tr('relato.rip_sin_dv')),
        relato.hecho("pistas", tr('relato.mux_hecho_pistas'),
                     relato.HECHO_OK if pistas else relato.HECHO_FALLO,
                     tr('relato.mux_n_pistas', n=len(pistas))),
        _hecho_de_sincronia(p),
    ]
    if situacion == relato.TERMINADO:
        val = _campo(p, "validacion") or {}
        fallos = val.get("fallos") or 0
        avisos = sum(1 for c in val.get("checks") or [] if c.get("aviso"))
        hechos.append(relato.hecho(
            "validacion", tr('relato.rip_hecho_validacion'),
            relato.HECHO_OK if not (fallos or avisos) else relato.HECHO_AVISO,
            tr('relato.rip_validacion_limpia') if not (fallos or avisos)
            else tr('relato.mux_validacion_con_avisos', n=avisos + fallos)))
    return hechos


def _porque(p, situacion: str, plan: dict | None) -> str:
    """Por qué está donde está — mirando ATRÁS, nunca adelante."""
    if situacion == relato.CANCELADO:
        return tr('relato.rip_porque_cancelado')
    if situacion == relato.DETENIDO_POR_ERROR:
        return tr('relato.rip_porque_error')
    if situacion == relato.ESPERANDO_TURNO:
        return tr('relato.rip_porque_en_cola')
    if situacion == relato.TERMINADO:
        return tr('relato.mux_porque_terminado')
    if situacion == relato.ESPERANDO_DECISION:
        primero = next((m for m in (plan or {}).get("motivos") or []
                        if m.get("id") not in _SIN_DECISION), None)
        return tr(f"relato.mux_porque_{primero['id']}") if primero else ""
    if _sincronia(p).get("estado") == "midiendo":
        return tr('relato.mux_porque_midiendo')
    return tr('relato.mux_porque_preparando')


def resolver(p, *, en_cola=None, fase_en_curso: str = "", plan: dict | None = None) -> dict:
    """El relato de una combinación. Acepta el modelo o el dict del summary."""
    situacion = _situacion(p, en_cola, plan)
    etapa = _etapa(p, situacion, fase_en_curso or "")
    idx = ETAPAS.index(etapa)
    decision = {"estado": relato.DECISION_NO_PROCEDE}
    if situacion == relato.ESPERANDO_DECISION:
        decision = {"estado": relato.DECISION_PENDIENTE}
    elif _campo(p, "acepta_riesgo"):
        decision = {"estado": relato.DECISION_TOMADA}
    return {
        "situacion": situacion,
        "situacion_rotulo": tr(f"relato.mux_situacion_{situacion}"),
        "etapas": [{"id": e, "letra": "", "rotulo": rotulo_de_etapa(e)} for e in ETAPAS],
        "etapa": {"id": etapa, "rotulo": rotulo_de_etapa(etapa),
                  "indice": idx + 1, "total": len(ETAPAS), "porque": ""},
        "porque": _porque(p, situacion, plan),
        "decision": decision,
        "hechos": _hechos(p, situacion),
        "siguiente": (rotulo_de_etapa(ETAPAS[idx + 1])
                      if situacion == relato.EN_MARCHA and idx + 1 < len(ETAPAS)
                      else ""),
    }
