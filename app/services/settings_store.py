"""
settings_store.py — Settings persistentes editables desde la UI.

Persiste en `/config/app_settings.json`. Los valores guardados aquí GANAN
sobre variables de entorno, para que el usuario pueda cambiar secretos
(TMDb API key…) sin reconstruir el contenedor.

Campos soportados:
  - tmdb_api_key: str          — opcional. La app trae la suya (ver
                                 `clave_tmdb_de_la_app`), así que esto es un
                                 override deliberado, no un requisito
  - google_api_key: str        — opcional, habilita listado+descarga de RPUs
                                 del repositorio de REC_9999 en Google Drive
  - cmv40_drive_folder_url: str — URL (o ID) de la carpeta Drive del repo de
                                 REC_9999. El acceso requiere donación previa
                                 (ver UI). Tratada como secret (no devuelta
                                 cruda al frontend) porque compartirla equivale
                                 a regalar acceso pagado.
  - cmv40_sheet_url: str       — URL del Google Sheet de recomendaciones.
                                 Tiene default público (sheet oficial DoviTools)
                                 pero se puede override. NO es secret.

Los secretos NUNCA se devuelven crudos en el endpoint GET — solo se
envía `{configured: bool, last4: str}`. POST permite setear, vaciar o
dejar sin cambios (omitiendo la clave).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from threading import Lock
from typing import Any

_logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
SETTINGS_PATH = CONFIG_DIR / "app_settings.json"

# Default público del sheet de DoviTools (pestaña GRADE CHECK).
# El usuario puede overrideearlo en Configuración.
DEFAULT_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "15i0a84uiBtWiHZ5CXZZ7wygLFXwYOd84/edit?gid=828864432"
)

# ── El idioma de la app ────────────────────────────────────────────────
#
# Global y no por petición: la app no tiene usuarios, ni auth, ni cookies, ni
# mira `Accept-Language`. Es un aparato de una instalación, así que el idioma
# vive aquí como cualquier otro ajuste.
#
# El frontend guarda además una copia en `localStorage` para pintar la primera
# pantalla sin esperar a una petición; el selector escribe las dos y
# `reconciliarIdioma()` arregla el caso de un `app_settings.json` editado a
# mano. Este de aquí es la fuente de verdad, y es quien decide en qué idioma
# escribe el servidor sus propios mensajes.
IDIOMAS = ("es", "en", "ca")
IDIOMA_POR_DEFECTO = "es"


def get_idioma() -> str:
    """Prioridad: settings.json > `HDO_IDIOMA` > castellano."""
    with _lock:
        stored = _load().get("idioma", "").strip().lower()
    if stored in IDIOMAS:
        return stored
    env = os.environ.get("HDO_IDIOMA", "").strip().lower()
    if env in IDIOMAS:
        return env
    return IDIOMA_POR_DEFECTO


def update_idioma(new_value: str | None) -> None:
    """`None` = no tocar. Un valor que no es de la lista se ignora.

    Se ignora en vez de lanzar porque esto lo llama el endpoint de ajustes con
    lo que venga del cliente, y un idioma inventado no debe tumbar el guardado
    de las otras cuatro claves que van en el mismo POST.
    """
    if new_value is None:
        return
    v = new_value.strip().lower()
    if v not in IDIOMAS:
        _logger.warning("[settings] idioma no soportado, se ignora: %r", new_value)
        return
    _update_field("idioma", v)
    # Los catálogos del backend se cachean por idioma: sin esto, el servidor
    # seguiría escribiendo en el anterior hasta reiniciar.
    try:
        from i18n import limpiar_cache
        limpiar_cache()
    except Exception:
        pass


# ── La clave de TMDb con la que la app funciona sin configurar nada ────
#
# La app se distribuye con una clave de TMDb dada de alta para ella. La ficha
# de la película, el mapeo de episodios de una serie y la traducción ES→EN no
# son un extra: son cómo se usa la app, y pedir que cada usuario se diera de
# alta en TMDb antes de poder crear su primer proyecto convertía un detalle en
# un trámite. Configurar una clave propia sigue estando, pero pasa a ser una
# decisión deliberada.
#
# **La clave NO está en el repositorio.** Se hornea en la imagen al construirla
# (`ARG TMDB_APP_KEY` en el Dockerfile ← secreto del workflow de GHCR), que es
# como se instala la app: `docker compose pull`. En el código no hay ninguna
# constante que descodificar, y rotarla no deja rastro en el historial de git.
#
# Lo que esto NO hace, y conviene tenerlo claro: la imagen de GHCR es pública,
# así que quien la baje y mire su entorno tiene la clave igual. No la vuelve
# difícil de obtener — la quita del sitio donde se mira, que es GitHub, y de
# los rastreadores que peinan repos buscando 32 hexadecimales al lado de
# `api_key`. Una credencial dentro de software distribuido no se puede
# esconder: la app tiene que poder usarla, luego puede obtenerla cualquiera
# que tenga la app. Lo que hace aceptable el canje es que la consecuencia está
# acotada —es solo lectura de un catálogo público, no está ligada a ninguna
# cuenta de usuario— y que la salida está construida: si la revocan, el
# usuario pone la suya y el botón «Probar» con el campo vacío se lo diagnostica.
#
# **El volumen no era el motivo de nada, y está medido**: sobre el
# `tmdb_cache.json` de una instalación real con cinco meses de uso intensivo
# (abr-sep 2026) son **511 peticiones que no salieron de caché**, 43 el día
# peor. TMDb admite ~50 por SEGUNDO y no tiene cuota diaria, y todo lo que se
# pide se cachea 30 días en disco. Ni mil usuarios al ritmo del peor día
# llegarían a 0,5 req/s.
#
# Es una variable PROPIA y no `TMDB_API_KEY` a propósito: esa última es la del
# usuario, y mezclarlas haría que ⚙︎ Configuración anunciara la clave de la
# app como «desde .env» — o sea, como algo que el usuario puso.
ENV_CLAVE_TMDB_DE_LA_APP = "TMDB_APP_KEY"


def clave_tmdb_de_la_app() -> str:
    """La clave que viaja con la app, o `""` si este build no trae ninguna.

    Devolver vacío no es un error: un fork que compile sin pasar el build arg
    se comporta exactamente como antes de que esto existiera —TMDb queda sin
    configurar hasta que el usuario pegue una clave— y la UI lo dice con el
    mismo aviso de siempre.
    """
    return os.environ.get(ENV_CLAVE_TMDB_DE_LA_APP, "").strip()


# ── El repositorio DoviTools que trae la app ───────────────────────────
#
# Misma vía que la clave de TMDb: **no está en el repositorio**, la hornea el
# build desde un secreto (`ARG CMV40_DRIVE_FOLDER_APP`). Aquí solo vive el
# nombre de la variable.
#
# Verificado antes de incluirlo (2026-09-14): el enlace es **genérico**, no
# personal. Una petición anónima a la carpeta —sin sesión de Google ni
# credencial— devuelve HTTP 200 con el nombre de la carpeta y los `.bin`
# listados, o sea que está compartida como «cualquiera con el enlace» y el ID
# es el de la carpeta, igual para todos. Lo que el manual describe («dona,
# manda tu correo y te dan acceso») es una puerta SOCIAL, no técnica.
#
# Y esa puerta es de otro, así que la app la reconoce en vez de ignorarla: al
# usar el enlace incluido lleva la cuenta de los bins descargados y cada
# `DESCARGAS_POR_AVISO` recuerda la donación. **Solo cuando se usa el enlace
# de la app**: quien ha puesto el suyo ya donó, y no se le molesta más.
#
# Dos cosas que NO resuelve incluirlo, y que están escritas donde toca:
#   · la pestaña Repo **sigue necesitando la Google API key**, que sí es de
#     cada uno (cuota por proyecto de Cloud, y Google desactiva las que
#     encuentra filtradas). El enlace es el paso fácil; la key es el difícil;
#   · el autor puede cerrar la carpeta a permiso por cuenta cuando quiera —es
#     un clic— y ese día la función deja de funcionar para todos. El riesgo se
#     asume a sabiendas.
ENV_DRIVE_FOLDER_DE_LA_APP = "CMV40_DRIVE_FOLDER_APP"

# Cada cuántos bins descargados con el enlace de la app se recuerda la
# donación. Se cuentan DESCARGAS y no peticiones a la API: el listado se
# cachea 24 h, así que «100 peticiones» pueden ser meses o una tarde, mientras
# que un bin descargado es exactamente la unidad de valor que el usuario
# recibe del autor.
DESCARGAS_POR_AVISO = 20


def carpeta_drive_de_la_app() -> str:
    """El repo que viaja con la app, o `""` si este build no trae ninguno.

    Vacío es un estado válido: la pestaña Repo queda como estaba antes de que
    esto existiera, con la explicación del paywall y el campo para pegar el
    enlace propio.
    """
    return os.environ.get(ENV_DRIVE_FOLDER_DE_LA_APP, "").strip()


# ── Parseo de URLs de Google ────────────────────────────────────────────

_DRIVE_FOLDER_RE = re.compile(r"/folders/([A-Za-z0-9_\-]{10,})")
_SHEET_ID_RE     = re.compile(r"/spreadsheets/d/([A-Za-z0-9_\-]{10,})")
_SHEET_GID_RE    = re.compile(r"[#?&]gid=(\d+)")


def parse_drive_folder_id(value: str) -> str:
    """Acepta URL completa de Drive o ID pelado. Devuelve el ID o ''."""
    v = (value or "").strip()
    if not v:
        return ""
    m = _DRIVE_FOLDER_RE.search(v)
    if m:
        return m.group(1)
    # Si no hay path /folders/, asumimos que es el ID suelto si encaja.
    if re.fullmatch(r"[A-Za-z0-9_\-]{10,}", v):
        return v
    return ""


def parse_sheet_id_gid(value: str) -> tuple[str, str]:
    """Extrae (sheet_id, gid) de una URL de Google Sheets.
    Devuelve ('', '') si no se puede parsear."""
    v = (value or "").strip()
    if not v:
        return "", ""
    m_id = _SHEET_ID_RE.search(v)
    m_gid = _SHEET_GID_RE.search(v)
    sid = m_id.group(1) if m_id else ""
    gid = m_gid.group(1) if m_gid else "0"  # gid=0 = primera pestaña por defecto
    return sid, gid

_lock = Lock()
_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    if SETTINGS_PATH.exists():
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _cache = raw
            else:
                _cache = {}
        except Exception as e:
            _logger.warning("app_settings.json corrupto: %s — usando defaults", e)
            _cache = {}
    else:
        _cache = {}
    return _cache


def _save(data: dict[str, Any]) -> None:
    global _cache
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_PATH)
        _cache = data
    except OSError as e:
        _logger.error("No pude persistir app_settings.json: %s", e)
        raise


# ── Getters/setters genéricos para campos no-secret ─────────────────────

def get_settings_value(key: str, default: Any = None) -> Any:
    """Lee un campo arbitrario del store. Para secrets usa los getters
    dedicados (que también consideran env como fallback)."""
    with _lock:
        return _load().get(key, default)


def set_settings_value(key: str, value: Any) -> None:
    """Escribe un campo arbitrario en el store con persistencia atómica."""
    with _lock:
        data = dict(_load())
        data[key] = value
        _save(data)


# ── Getters con fallback a env ──────────────────────────────────────────

def get_tmdb_api_key() -> str:
    """Prioridad: settings.json > TMDB_API_KEY env > la clave de la app.

    La del usuario gana a la del despliegue, y las dos a la de la app: quien
    configura una clave lo hace para usarla. Y borrar la propia no deja la app
    sin TMDb, devuelve a la de la app — que es lo que hace el botón «Vaciar
    todo» de ⚙︎, igual que «Restaurar default» con el sheet.
    """
    with _lock:
        stored = _load().get("tmdb_api_key", "").strip()
    if stored:
        return stored
    env = os.environ.get("TMDB_API_KEY", "").strip()
    if env:
        return env
    return clave_tmdb_de_la_app()


def get_google_api_key() -> str:
    """Prioridad: settings.json > GOOGLE_API_KEY env > vacío."""
    with _lock:
        stored = _load().get("google_api_key", "").strip()
    if stored:
        return stored
    return os.environ.get("GOOGLE_API_KEY", "").strip()


def get_cmv40_drive_folder_url() -> str:
    """Prioridad: settings.json > `CMV40_DRIVE_FOLDER_URL` > el de la app."""
    with _lock:
        stored = _load().get("cmv40_drive_folder_url", "").strip()
    if stored:
        return stored
    env = os.environ.get("CMV40_DRIVE_FOLDER_URL", "").strip()
    if env:
        return env
    return carpeta_drive_de_la_app()


def usando_el_repo_de_la_app() -> bool:
    """¿El repo activo sale del enlace que trae la app, sin que nadie pusiera otro?

    Es lo que decide si se recuerda la donación, y lo que mira es **de dónde
    sale el enlace**, no a qué carpeta apunta. La distinción importa porque
    quien dona recibe... el mismo enlace: la carpeta es pública y única, así
    que comparar IDs daría «es el de la app» también para el donante que lo
    pegó, y se le estaría recordando para siempre una donación que ya hizo.

    Lo que distingue a un donante no es su carpeta — es que se molestó en
    pegarla. O sea, exactamente `source == "default"` del estado público.
    """
    if not carpeta_drive_de_la_app():
        return False
    with _lock:
        if _load().get("cmv40_drive_folder_url", "").strip():
            return False
    return not (os.environ.get("CMV40_DRIVE_FOLDER_URL", "").strip()
                or os.environ.get("CMV40_DRIVE_FOLDER_ID", "").strip())


def get_cmv40_drive_folder_id() -> str:
    """ID del folder Drive extraído de la URL configurada (o env). Vacío si no."""
    raw = get_cmv40_drive_folder_url()
    if not raw:
        # Back-compat: env var legacy CMV40_DRIVE_FOLDER_ID si alguien lo tenía
        legacy = os.environ.get("CMV40_DRIVE_FOLDER_ID", "").strip()
        if legacy:
            return legacy
        return ""
    return parse_drive_folder_id(raw)


def get_cmv40_sheet_url() -> str:
    """URL cruda del sheet configurado. Fallback al default público."""
    with _lock:
        stored = _load().get("cmv40_sheet_url", "").strip()
    if stored:
        return stored
    env = os.environ.get("CMV40_SHEET_URL", "").strip()
    if env:
        return env
    return DEFAULT_SHEET_URL


def get_cmv40_sheet_id_gid() -> tuple[str, str]:
    """(sheet_id, gid) del sheet configurado. Fallback al default público."""
    return parse_sheet_id_gid(get_cmv40_sheet_url())


# ── API pública consumida por main.py ───────────────────────────────────

def _status_for(stored: str, env: str, por_defecto: str = "") -> dict[str, Any]:
    """El estado de una clave, sin exponerla. `por_defecto` es la que trae la
    app: cuenta como configurada, pero **no se manda el `last4`** — el usuario
    no la ha puesto, y una cola de cuatro caracteres solo invita a confundirla
    con la suya.
    """
    effective = stored or env or por_defecto
    if stored:
        source = "settings"
    elif env:
        source = "env"
    elif por_defecto:
        source = "default"
    else:
        source = "none"
    return {
        "configured": bool(effective),
        "source": source,
        "last4": effective[-4:] if (effective and source != "default") else "",
        "is_default": source == "default",
    }


def get_public_settings() -> dict[str, Any]:
    """Devuelve el estado de config sin exponer secretos crudos.

    `cmv40_drive_folder_url` se trata como secret (el acceso es de pago,
    compartirlo es regalar acceso) → solo devolvemos configured+last4.
    `cmv40_sheet_url` NO es secret; devolvemos la URL cruda para que el
    usuario vea qué está usando y pueda restaurar el default.
    """
    with _lock:
        stored = _load()
    drive_url_stored = stored.get("cmv40_drive_folder_url", "").strip()
    drive_url_env    = os.environ.get("CMV40_DRIVE_FOLDER_URL", "").strip()
    drive_legacy_env = os.environ.get("CMV40_DRIVE_FOLDER_ID", "").strip()
    drive_de_la_app  = carpeta_drive_de_la_app()
    drive_effective  = (drive_url_stored or drive_url_env or drive_legacy_env
                        or drive_de_la_app)
    drive_folder_id  = parse_drive_folder_id(drive_effective) if drive_effective else ""
    if drive_url_stored:
        drive_source = "settings"
    elif drive_url_env or drive_legacy_env:
        drive_source = "env"
    elif drive_de_la_app:
        drive_source = "default"
    else:
        drive_source = "none"

    sheet_url_stored = stored.get("cmv40_sheet_url", "").strip()
    sheet_url_env    = os.environ.get("CMV40_SHEET_URL", "").strip()
    sheet_effective  = sheet_url_stored or sheet_url_env or DEFAULT_SHEET_URL
    sheet_id, sheet_gid = parse_sheet_id_gid(sheet_effective)

    return {
        "tmdb": _status_for(
            stored.get("tmdb_api_key", "").strip(),
            os.environ.get("TMDB_API_KEY", "").strip(),
            clave_tmdb_de_la_app(),
        ),
        "google": _status_for(
            stored.get("google_api_key", "").strip(),
            os.environ.get("GOOGLE_API_KEY", "").strip(),
        ),
        "drive_folder": {
            "configured": bool(drive_folder_id),
            "source": drive_source,
            # Igual que con la clave de TMDb: lo que no ha puesto el usuario
            # no lleva cola de caracteres, que solo invita a confundirlo con
            # lo suyo. El `folder_id_last6` sí se manda — el ID de una carpeta
            # pública no es un secreto y ayuda a ver que es la de siempre.
            "last4": drive_effective[-4:] if (drive_effective and drive_source != "default") else "",
            "folder_id_last6": drive_folder_id[-6:] if drive_folder_id else "",
            "is_default": drive_source == "default",
        },
        "dovitools": estado_donacion_dovitools(),
        # El idioma NO es un secreto: va en crudo, como el sheet.
        "idioma": {
            "activo": get_idioma(),
            "disponibles": list(IDIOMAS),
            "por_defecto": IDIOMA_POR_DEFECTO,
        },
        "sheet": {
            "configured": bool(sheet_id),
            "source": "settings" if sheet_url_stored else ("env" if sheet_url_env else "default"),
            "url": sheet_effective,           # NO es secret
            "default_url": DEFAULT_SHEET_URL,
            "sheet_id_last6": sheet_id[-6:] if sheet_id else "",
            "gid": sheet_gid,
            "is_default": sheet_effective == DEFAULT_SHEET_URL,
        },
    }


# ── El recordatorio de la donación a DoviTools ─────────────────────────

def registrar_descarga_de_bin() -> None:
    """Suma un bin descargado, y solo si se usó el repo que trae la app.

    Lo llama `rec999_drive.download_file`, que es **el único sitio por el que
    baja un bin**: las dos rutas que existen (el pre-flight y la Fase B) pasan
    por ahí, y Fase B además reutiliza lo que ya bajó el pre-flight, así que
    contar aquí es contar descargas reales y no intentos.

    No puede tumbar una descarga: se traga cualquier error. Perder una cuenta
    es un inconveniente; que falle un bin ya descargado por no poder escribir
    en `/config`, no.
    """
    if not usando_el_repo_de_la_app():
        return
    try:
        with _lock:
            data = dict(_load())
            data["dovitools_descargas"] = int(data.get("dovitools_descargas", 0)) + 1
            _save(data)
    except Exception as e:
        _logger.warning("[settings] no se pudo contar la descarga del bin: %s", e)


def estado_donacion_dovitools() -> dict[str, Any]:
    """Cuántos bins van con el repo de la app y si toca recordar la donación."""
    if not usando_el_repo_de_la_app():
        # Con repo propio no se avisa nunca: ese usuario ya donó.
        return {"repo_de_la_app": False, "descargas": 0, "avisar": False,
                "cada": DESCARGAS_POR_AVISO}
    with _lock:
        data = _load()
        descargas = int(data.get("dovitools_descargas", 0) or 0)
        avisado_en = int(data.get("dovitools_avisado_en", 0) or 0)
    return {
        "repo_de_la_app": True,
        "descargas": descargas,
        "avisar": descargas - avisado_en >= DESCARGAS_POR_AVISO,
        "cada": DESCARGAS_POR_AVISO,
    }


def marcar_donacion_avisada() -> None:
    """El usuario ya ha visto el recordatorio: el contador vuelve a empezar.

    Se guarda el CONTADOR del momento y no un booleano, para que la cuenta
    siga siendo el total histórico de bins y el siguiente aviso salga otros
    `DESCARGAS_POR_AVISO` después.
    """
    with _lock:
        data = dict(_load())
        data["dovitools_avisado_en"] = int(data.get("dovitools_descargas", 0) or 0)
        _save(data)


def _update_field(field: str, new_value: str | None) -> None:
    """`None` = no tocar, `""` = borrar, otra cosa = setear."""
    if new_value is None:
        return
    with _lock:
        data = dict(_load())
        if new_value == "":
            data.pop(field, None)
        else:
            data[field] = new_value.strip()
        _save(data)


def update_tmdb_api_key(new_value: str | None) -> None:
    _update_field("tmdb_api_key", new_value)


def update_google_api_key(new_value: str | None) -> None:
    if new_value is None:
        return
    _update_field("google_api_key", new_value)
    # Al cambiar la Google key, invalidamos la caché de la hoja para que
    # el próximo fetch intente Sheets API v4 (o vuelva a CSV si se borró).
    _invalidate_sheet_cache()
    _invalidate_drive_cache()


def update_cmv40_drive_folder_url(new_value: str | None) -> None:
    if new_value is None:
        return
    _update_field("cmv40_drive_folder_url", new_value)
    _invalidate_drive_cache()


def update_cmv40_sheet_url(new_value: str | None) -> None:
    if new_value is None:
        return
    _update_field("cmv40_sheet_url", new_value)
    _invalidate_sheet_cache()


def _invalidate_sheet_cache() -> None:
    try:
        from services.rec999_sheet import invalidate_cache as _inv_sheet
        _inv_sheet()
    except Exception:
        pass


def _invalidate_drive_cache() -> None:
    try:
        import services.rec999_drive as _drv
        _drv._cache_files = None
        _drv._cache_fetched_at = 0.0
    except Exception:
        pass
