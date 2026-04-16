"""
qts.py — Integración con la API QTS File Station (QNAP)

Responsabilidad:
  Montar y desmontar ISOs en el host QNAP usando la API REST de
  QTS File Station, de modo que el contenedor Docker pueda acceder
  al contenido del BDMV como un directorio normal sin privilegios
  especiales (sin SYS_ADMIN ni loop devices).

─────────────────────────────────────────────────────────────────────
FLUJO DE MONTAJE
─────────────────────────────────────────────────────────────────────

  1. mount_iso(iso_path)  → authLogin.cgi  → obtiene SID
                          → mount_iso API  → QTS monta el ISO como
                            shared folder en /share/{nombre}/
  2. (proceso: BDInfoCLI y mkvmerge leen desde /share/{nombre}/)
  3. unmount_iso(iso_path) → unmount_iso API → siempre en finally

─────────────────────────────────────────────────────────────────────
CONFIGURACIÓN (variables de entorno)
─────────────────────────────────────────────────────────────────────

  QNAP_HOST       — Hostname/IP del NAS (ej: '192.168.1.100')
  QNAP_PORT       — Puerto HTTP de QTS (default: '8080')
  QNAP_USER       — Usuario QTS con permisos de montaje (ej: 'admin')
  QNAP_PASS       — Contraseña del usuario QTS
  SHARE_BASE      — Directorio base donde Docker ve los shares montados
                    (default: '/share' — debe ser volumen en compose)
  QNAP_ISO_SHARE  — Ruta NAS del directorio de ISOs tal como la ve QTS
                    (ej: '/share/CACHEDEV1_DATA/isos')
                    Permite traducir la ruta Docker al path QTS.

─────────────────────────────────────────────────────────────────────
RESPUESTA DE mount_iso
─────────────────────────────────────────────────────────────────────

QTS devuelve XML con <status>N</status>:
  1 → montado con éxito
  2 → ya estaba montado (idempotente)
  otros → error

El share montado queda accesible en:
  /share/{nombre_sin_extension}/  (ruta Docker vía SHARE_BASE)

Ref: QTS File Station API v2 — func=mount_iso / unmount_iso
"""
import asyncio
import base64
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib import parse, request

# ── Configuración desde variables de entorno ─────────────────────────────────

QNAP_HOST      = os.environ.get("QNAP_HOST", "")
QNAP_PORT      = int(os.environ.get("QNAP_PORT", "8080"))
QNAP_USER      = os.environ.get("QNAP_USER", "")
QNAP_PASS      = os.environ.get("QNAP_PASS", "")
SHARE_BASE     = os.environ.get("SHARE_BASE", "/share")
QNAP_ISO_SHARE = os.environ.get("QNAP_ISO_SHARE", "/share/CACHEDEV1_DATA/isos")
# ↑ Ruta del directorio de ISOs en el NAS (vista por QTS, no por Docker).
#   Necesaria para traducir /mnt/isos/pelicula.iso → /share/CACHEDEV1_DATA/isos/pelicula.iso


# ══════════════════════════════════════════════════════════════════════
#  API PÚBLICA
# ══════════════════════════════════════════════════════════════════════

def is_configured() -> bool:
    """Devuelve True si las variables de entorno QTS están todas definidas."""
    return bool(QNAP_HOST and QNAP_USER and QNAP_PASS)


async def mount_iso(docker_iso_path: str) -> str:
    """
    Monta el ISO a través de la API QTS File Station.

    Args:
        docker_iso_path: Ruta al ISO tal como la ve Docker (ej: '/mnt/isos/Movie.iso').

    Returns:
        Ruta del share montado en Docker (ej: '/share/Movie').
        BDInfoCLI y mkvmerge leen desde esta ruta.

    Raises:
        RuntimeError: Si QTS no está configurado o el montaje falla.
    """
    if not is_configured():
        raise RuntimeError(
            "QTS no configurado. Define QNAP_HOST, QNAP_USER y QNAP_PASS en el entorno Docker."
        )
    return await asyncio.to_thread(_mount_iso_sync, docker_iso_path)


async def unmount_iso(docker_iso_path: str) -> None:
    """
    Desmonta el ISO en QTS.

    Seguro de llamar en un bloque ``finally``: no lanza excepciones
    (un share huérfano es preferible a ocultar el error del proceso principal).

    Args:
        docker_iso_path: Ruta al ISO en Docker (mismo valor que en mount_iso).
    """
    if not is_configured():
        return
    await asyncio.to_thread(_unmount_iso_sync, docker_iso_path)


async def test_connection() -> dict:
    """
    Comprueba la conectividad con el NAS QTS.

    Returns:
        Dict con ``{"ok": bool, "host": str, "error": str}``
    """
    if not is_configured():
        return {"ok": False, "host": QNAP_HOST or "(no configurado)", "error": "Variables de entorno no definidas"}
    try:
        sid = await asyncio.to_thread(_get_sid)
        return {"ok": True, "host": QNAP_HOST, "error": ""}
    except Exception as e:
        return {"ok": False, "host": QNAP_HOST, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════
#  IMPLEMENTACIÓN INTERNA (síncrona, ejecutada en thread pool)
# ══════════════════════════════════════════════════════════════════════

def _get_sid() -> str:
    """
    Autentica contra QTS y devuelve el SID de sesión.

    QTS devuelve XML con <authSid>SID</authSid>.
    La contraseña se envía en base64 (requisito de la API QTS).
    """
    pwd_b64 = base64.b64encode(QNAP_PASS.encode()).decode()
    url = (
        f"http://{QNAP_HOST}:{QNAP_PORT}/cgi-bin/authLogin.cgi"
        f"?user={parse.quote(QNAP_USER)}&pwd={parse.quote(pwd_b64)}"
    )
    with request.urlopen(url, timeout=10) as resp:
        body = resp.read().decode("utf-8", errors="replace")

    try:
        root = ET.fromstring(body)
        # QTS puede devolver <authSid> directamente o anidado
        sid = root.findtext("authSid") or root.findtext(".//authSid")
        if not sid:
            raise RuntimeError(f"No se encontró authSid en la respuesta de QTS: {body[:200]}")
        return sid
    except ET.ParseError:
        raise RuntimeError(f"Respuesta inesperada de authLogin.cgi: {body[:200]}")


def _nas_iso_path(docker_iso_path: str) -> str:
    """
    Convierte la ruta Docker del ISO (dentro de /mnt/isos) a la ruta NAS
    que QTS necesita para mount_iso.

    Ejemplo:
      docker: /mnt/isos/subdir/Movie (2025).iso
      NAS:    /share/CACHEDEV1_DATA/isos/subdir/Movie (2025).iso
    """
    isos_dir = os.environ.get("ISOS_DIR", "/mnt/isos")
    rel = os.path.relpath(docker_iso_path, isos_dir)
    return str(Path(QNAP_ISO_SHARE) / rel)


def _mount_iso_sync(docker_iso_path: str) -> str:
    """
    Llama a mount_iso en la API QTS y devuelve la ruta del share montado en Docker.
    """
    sid      = _get_sid()
    nas_path = _nas_iso_path(docker_iso_path)
    iso_stem = Path(docker_iso_path).stem   # nombre sin extensión

    url = (
        f"http://{QNAP_HOST}:{QNAP_PORT}/cgi-bin/filemanager/utilRequest.cgi"
        f"?func=mount_iso"
        f"&source_path={parse.quote(nas_path)}"
        f"&dest_path="
        f"&sid={sid}"
    )
    with request.urlopen(url, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")

    try:
        root   = ET.fromstring(body)
        status = root.findtext("status") or root.findtext(".//status") or ""
        if status not in ("1", "2"):
            raise RuntimeError(
                f"QTS mount_iso devolvió status={status!r}. "
                f"Respuesta completa: {body[:300]}"
            )
    except ET.ParseError:
        raise RuntimeError(f"Respuesta inesperada de mount_iso: {body[:200]}")

    # El share montado es accesible en SHARE_BASE/{nombre_sin_extension}/
    return str(Path(SHARE_BASE) / iso_stem)


def _unmount_iso_sync(docker_iso_path: str) -> None:
    """
    Llama a unmount_iso en la API QTS. Silencia todas las excepciones.
    """
    try:
        sid      = _get_sid()
        nas_path = _nas_iso_path(docker_iso_path)
        url = (
            f"http://{QNAP_HOST}:{QNAP_PORT}/cgi-bin/filemanager/utilRequest.cgi"
            f"?func=unmount_iso"
            f"&source_path={parse.quote(nas_path)}"
            f"&sid={sid}"
        )
        with request.urlopen(url, timeout=15) as resp:
            resp.read()
    except Exception:
        pass  # Los errores de desmontaje son no-fatales
