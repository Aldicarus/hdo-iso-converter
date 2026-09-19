"""El relato de un rip (Tab 1): qué pasa, dónde está y qué se estableció.

Mismo objeto que `cmv40_relato` y por el mismo motivo: las superficies que le
hablan al usuario —la tarjeta del sidebar, el banner del proyecto, el icono de
la sub-pestaña, los filtros y la columna de trabajo— derivaban cada una por su
cuenta en qué estado está el proyecto, y ninguna de las cinco sabía de las
otras.

Los dos defectos que eso producía, verificados sobre el `/config` del NAS:

- **Un rip cancelado es indistinguible de uno que nunca se lanzó.** Al
  cancelar, `status` vuelve a `pending`, `error_message` se limpia y NO se
  apila un `ExecutionRecord` —decisión deliberada: el proyecto queda listo
  para relanzarse—, así que la ficha no menciona la cancelación por ninguna
  parte. Es el mismo H4 que se arregló en Tab 3.
- **Y si esa cancelación interrumpe una RE-ejecución, la tarjeta miente.**
  `_sessionExecStatus` leía `execution_history[-1]`, que en ese caso es la
  pasada BUENA de ayer: la tarjeta se queda en «Completado» en verde mientras
  el `except` del pipeline ha borrado el MKV que esa pasada produjo. No hay
  ningún caso así en las 73 sesiones del NAS —el usuario no ha cancelado
  nunca una re-ejecución— pero la ausencia mide la disciplina del usuario, no
  la demanda.

Es puro: sin disco y sin red. Lo que no puede saber por sí mismo —si la
sesión está en la cola y qué fase corre ahora mismo— se lo pasa el router,
igual que `cmv40_relato` recibe el `plan` ya resuelto.
"""
from __future__ import annotations

import relato
from i18n import t as tr

# ── Las cinco etapas de un rip, en orden ────────────────────────────────────
#
# Son exactamente las que el pipeline marca con `_mark_phase` (y por tanto las
# que acaban en `phase_elapsed`) más el análisis, que ocurre al crear el
# proyecto y es la etapa 1 para el usuario aunque no forme parte de la
# ejecución. La validación del MKV final NO es una etapa: no se marca, y lo
# que aporta —si el fichero salió como se esperaba— es un HECHO.
ETAPAS = ("analisis", "origen", "extraer", "escribir", "cerrar")

#: la fase que marca el pipeline → la etapa que el usuario conoce.
_FASE_A_ETAPA = {
    "mount": "origen", "extract": "extraer",
    "write": "escribir", "unmount": "cerrar",
}


def rotulo_de_etapa(etapa: str) -> str:
    """«Extraer las pistas al MKV». La clave se compone del id.

    Una tabla de rótulos en el ámbito del módulo se evaluaría al importar y
    congelaría el idioma del arranque del contenedor — lo que vigila
    `TestNadieTraduceAlImportar`.
    """
    return tr(f"relato.rip_etapa_{etapa}")


def _campo(s, nombre, defecto=None):
    """El campo, venga de un `Session` o del dict del summary.

    El sidebar se pinta con `list_sessions_summary`, que devuelve dicts
    cacheados; la ficha, con el modelo. Aceptar los dos evita reconstruir 73
    `Session` en cada listado solo para poder contar lo mismo en los dos
    sitios.
    """
    if isinstance(s, dict):
        return s.get(nombre, defecto)
    return getattr(s, nombre, defecto)


def _situacion(s, en_cola) -> str:
    """Excluyentes y en orden fijo. El orden ES la decisión."""
    estado = _campo(s, "status") or "pending"
    if estado == "running":
        return relato.EN_MARCHA
    if estado == "queued" or en_cola:
        return relato.ESPERANDO_TURNO
    if estado == "error":
        return relato.DETENIDO_POR_ERROR
    if estado == "done":
        return relato.TERMINADO
    # `pending` son DOS cosas distintas y hasta ahora se veían igual: nunca
    # ejecutado, o ejecutado y parado por ti. Lo distingue `last_cancelled_at`,
    # que se limpia al arrancar la ejecución siguiente.
    if _campo(s, "last_cancelled_at"):
        return relato.CANCELADO
    return relato.PREPARANDO


def _etapa(s, situacion: str, fase_en_curso: str) -> str:
    """En qué etapa está — o hasta dónde llegó.

    Con el trabajo en marcha manda la fase que el pipeline está marcando
    AHORA (la trae el router desde `_rip_progress`, que es memoria del
    proceso). Sin ella, la etapa se deduce del desenlace.
    """
    if situacion == relato.EN_MARCHA and fase_en_curso in _FASE_A_ETAPA:
        return _FASE_A_ETAPA[fase_en_curso]
    if situacion in (relato.TERMINADO, relato.DETENIDO_POR_ERROR):
        return "cerrar"
    # Cancelado o sin empezar: el análisis es lo único que consta hecho.
    return "analisis"


def _dv(s) -> tuple[str, str]:
    """(estado del hecho, evidencia) del Dolby Vision del disco.

    `has_fel` viaja en la sesión y está también en el summary; el perfil y la
    CM version viven en `bdinfo_result`, que el summary vacía — de ahí que la
    evidencia sea más corta en el sidebar que en la ficha. No es un problema:
    la tarjeta no pinta evidencias.
    """
    bd = _campo(s, "bdinfo_result") or {}
    vts = (bd.get("video_tracks") if isinstance(bd, dict)
           else getattr(bd, "video_tracks", None)) or []
    dv = None
    for vt in vts:
        dv = vt.get("dovi") if isinstance(vt, dict) else getattr(vt, "dovi", None)
        if dv:
            break
    perfil = (dv.get("profile") if isinstance(dv, dict)
              else getattr(dv, "profile", None)) if dv else None
    if perfil:
        el = (dv.get("el_type") if isinstance(dv, dict)
              else getattr(dv, "el_type", "")) or ""
        cm = (dv.get("cm_version") if isinstance(dv, dict)
              else getattr(dv, "cm_version", "")) or ""
        trozos = [tr('relato.perfil_p', p=f"{perfil}{(' ' + el) if el else ''}")]
        if cm:
            trozos.append(f"CM {cm}")
        return relato.HECHO_OK, " · ".join(trozos)
    if _campo(s, "has_fel"):
        # Sin `dovi` en la pista, `_detect_fel` solo sabe que hay capa de
        # mejora: decir «FEL» aquí sería afirmar lo que el análisis no
        # confirmó, que es lo que la tarjeta del panel ya evita.
        return relato.HECHO_AVISO, tr('relato.rip_dv_sin_confirmar')
    return relato.HECHO_AVISO, tr('relato.rip_sin_dv')


def _hechos(s, situacion: str) -> list[dict]:
    """Lo que el análisis estableció y lo que la ejecución comprobó."""
    incluidas = _campo(s, "included_tracks") or []
    def _tipo(t):
        return (t.get("type") if isinstance(t, dict) else getattr(t, "type", "")) or ""
    audio = sum(1 for t in incluidas if _tipo(t) == "audio")
    subs = sum(1 for t in incluidas if _tipo(t) == "subtitle")
    caps = _campo(s, "chapters") or []
    def _auto(c):
        return not (c.get("name_custom") if isinstance(c, dict)
                    else getattr(c, "name_custom", False))
    del_disco = any(not _auto(c) for c in caps)

    estado_dv, evidencia_dv = _dv(s)
    hechos = [
        relato.hecho("dolby_vision", tr('relato.rip_hecho_dv'),
                     estado_dv, evidencia_dv),
        relato.hecho(
            "pistas", tr('relato.rip_hecho_pistas'),
            relato.HECHO_OK if incluidas else relato.HECHO_FALLO,
            tr('relato.rip_n_audio_n_subs', audio=audio, subs=subs)
            if incluidas else ""),
        relato.hecho(
            "capitulos", tr('relato.rip_hecho_capitulos'),
            relato.HECHO_OK if caps else relato.HECHO_AVISO,
            (tr('relato.rip_capitulos_del_disco', n=len(caps)) if del_disco
             else tr('relato.rip_capitulos_generados', n=len(caps)))
            if caps else tr('relato.rip_sin_capitulos')),
    ]
    # La validación solo consta cuando ha habido una ejecución que llegó al
    # final. Antes, «done» y «done con avisos» se veían exactamente igual: el
    # recuento vivía en el log y no llegaba a ninguna superficie.
    if situacion == relato.TERMINADO:
        avisos = _campo(s, "last_validation_warnings") or 0
        hechos.append(relato.hecho(
            "validacion", tr('relato.rip_hecho_validacion'),
            relato.HECHO_AVISO if avisos else relato.HECHO_OK,
            _avisos(avisos) if avisos
            else tr('relato.rip_validacion_limpia')))
    return hechos


def _avisos(n: int) -> str:
    """«1 discrepancia» / «3 discrepancias», en dos claves.

    El truco del sufijo de una letra (`{p2}` = 's'/'') no vale aquí: el
    plural de *discrepancy* es *discrepancies* y el de *discrepància* es
    *discrepàncies*, así que ninguno de los dos se forma añadiendo una letra.
    """
    return (tr('relato.rip_validacion_avisos_uno') if n == 1
            else tr('relato.rip_validacion_avisos_varios', n=n))


def _porque(s, situacion: str) -> str:
    """Por qué el trabajo está donde está — mirando ATRÁS, nunca adelante."""
    if situacion == relato.CANCELADO:
        return tr('relato.rip_porque_cancelado')
    if situacion == relato.DETENIDO_POR_ERROR:
        return tr('relato.rip_porque_error')
    if situacion == relato.ESPERANDO_TURNO:
        return tr('relato.rip_porque_en_cola')
    if situacion == relato.TERMINADO:
        return (tr('relato.rip_porque_terminado_avisos')
                if (_campo(s, "last_validation_warnings") or 0)
                else tr('relato.rip_porque_terminado'))
    if situacion == relato.PREPARANDO:
        return tr('relato.rip_porque_preparando')
    return ""


def resolver(s, *, en_cola=None, fase_en_curso: str = "") -> dict:
    """El relato de un rip. Acepta el modelo o el dict del summary."""
    situacion = _situacion(s, en_cola)
    etapa = _etapa(s, situacion, fase_en_curso or "")
    hechos = _hechos(s, situacion)
    idx = ETAPAS.index(etapa)
    return {
        "situacion": situacion,
        "situacion_rotulo": tr(f"relato.rip_situacion_{situacion}"),
        "etapas": [{"id": e, "letra": "", "rotulo": rotulo_de_etapa(e)}
                   for e in ETAPAS],
        "etapa": {"id": etapa, "rotulo": rotulo_de_etapa(etapa),
                  "indice": idx + 1, "total": len(ETAPAS), "porque": ""},
        "porque": _porque(s, situacion),
        # Un rip no le pregunta nada al usuario: la selección de pistas se
        # edita, no se contesta. La clave está para que las tres pestañas
        # tengan la misma forma y el JS no ramifique por pestaña.
        "decision": {"estado": relato.DECISION_NO_PROCEDE},
        "hechos": hechos,
        "siguiente": (rotulo_de_etapa(ETAPAS[idx + 1])
                      if situacion == relato.EN_MARCHA and idx + 1 < len(ETAPAS)
                      else ""),
    }
