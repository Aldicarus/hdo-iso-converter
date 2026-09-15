"""
i18n.py — El texto que el servidor le escribe al usuario, en tres idiomas.

Son ~364 frases: las líneas del log, los `detail` de las `HTTPException`, los
`RuntimeError` que llegan a un banner, las etiquetas de progreso y el `que` con
el que un trabajo se anuncia en la columna. Todo lo demás del backend
—docstrings, comentarios, nombres— **no se traduce**: no lo ve nadie.

## Por qué el idioma es global y no viaja en la petición

La app no tiene usuarios, ni auth, ni cookies, ni mira `Accept-Language`: es un
aparato de una instalación. Así que el idioma es un ajuste en
`app_settings.json`, igual que la clave de TMDb, y se lee de ahí. Eso evita
toda la fontanería de contexto por petición —`contextvars`, un parámetro de más
en catorce firmas— que en una app multiusuario sería obligatoria y aquí no
compra nada.

## El log se traduce AL ESCRIBIR, no al mostrar

Una línea de log se escribe una vez y se persiste en `/config/cmv40/{id}.log`.
Traducir al mostrar exigiría guardar eventos estructurados (código + valores) y
rehacer el formato del fichero, el WS y el visor. Traducir al escribir es
envolver el literal donde está, y el precio es que un trabajo viejo queda en el
idioma que hubiera entonces — que es exactamente lo que se decidió aceptar: lo
persistido no se migra.

Corolario: **los markers no se traducen.** `━━━`, `✓ Fase`, `📋 Plan`,
`§§PROGRESS§§`… son claves del parser del frontend y de
`_CMV40_LOG_FORCE_PERSIST_MARKERS`. Se traduce lo que va detrás, y el marker
queda fuera de la cadena traducible. Lo vigila
`test_registro_de_la_traduccion.py`.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

IDIOMAS = ("es", "en", "ca")
IDIOMA_POR_DEFECTO = "es"

CATALOGOS_DIR = Path(__file__).resolve().parent / "i18n"

_cache: dict[str, dict[str, str]] = {}
# Claves pedidas que no existen, para que el guard de la suite las vea. Se
# acumulan en vez de avisar una por una: una fase emite cientos de líneas.
_ausentes: set[str] = set()

_PARAM = re.compile(r"\{(\w+)\}")


def _catalogo(idioma: str) -> dict[str, str]:
    """El catálogo, cargado una vez. Un fichero ilegible no rompe nada."""
    if idioma in _cache:
        return _cache[idioma]
    ruta = CATALOGOS_DIR / f"{idioma}.json"
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        if not isinstance(datos, dict):
            raise ValueError("el catálogo no es un objeto")
    except Exception as e:
        # Sin catálogo se cae al castellano, y si el que falla ES el castellano
        # se sigue con un diccionario vacío: `t()` devuelve la clave, que se ve
        # y se caza, en vez de tumbar el arranque por un fichero.
        _logger.warning("[i18n] catálogo %s ilegible (%s)", idioma, e)
        datos = {}
    _cache[idioma] = datos
    return datos


def idioma_activo() -> str:
    """El idioma configurado. `es` si no hay nada o el valor no es válido."""
    try:
        from services.settings_store import get_idioma
        v = (get_idioma() or "").strip().lower()
    except Exception:
        return IDIOMA_POR_DEFECTO
    return v if v in IDIOMAS else IDIOMA_POR_DEFECTO


def t(clave: str, /, **params: Any) -> str:
    """El texto de `clave` en el idioma activo, con sus parámetros.

    Los parámetros van con nombre, nunca por posición: el orden de las palabras
    cambia entre lenguas y una plantilla posicional obliga a que no cambie.

    Una clave que no existe devuelve **la clave**, no cadena vacía: así el
    hueco se ve en pantalla y en el log en vez de desaparecer, que es el fallo
    que nadie reporta. Si falta en el idioma activo pero está en castellano, se
    usa el castellano — media traducción es mejor que un hueco.
    """
    cat = _catalogo(idioma_activo())
    txt = cat.get(clave)
    if txt is None and idioma_activo() != IDIOMA_POR_DEFECTO:
        txt = _catalogo(IDIOMA_POR_DEFECTO).get(clave)
    if txt is None:
        _ausentes.add(clave)
        return clave
    if params:
        def _sub(m: re.Match) -> str:
            nombre = m.group(1)
            return str(params[nombre]) if nombre in params else m.group(0)
        txt = _PARAM.sub(_sub, txt)
    return txt


def hay_texto(clave: str) -> bool:
    """¿Existe la clave? Para ramas que deciden sin ensuciar `_ausentes`."""
    return clave in _catalogo(idioma_activo()) or clave in _catalogo(IDIOMA_POR_DEFECTO)


def claves_ausentes() -> list[str]:
    """Las claves pedidas que no existían. Lo lee el guard de la suite."""
    return sorted(_ausentes)


def limpiar_cache() -> None:
    """Olvida los catálogos. Para los tests y para un cambio de idioma en vivo."""
    _cache.clear()
    _ausentes.clear()
